"""Folder scanning and file classification (read-only)."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from .diff_model import DiffResult
from .options import CompareOptions, matches_patterns
from .work import ComparisonCancelled, Work

ONLY_A = "Only A"
ONLY_B = "Only B"
IDENTICAL = "Identical"
MODIFIED = "Modified"
SKIPPED = "Skipped"
STATUSES = (ONLY_A, ONLY_B, IDENTICAL, MODIFIED, SKIPPED)

CHUNK = 1024 * 1024


@dataclass
class FileResult:
    rel_path: str
    status: str
    size_a: Optional[int] = None
    size_b: Optional[int] = None
    kind: str = "text"  # text | records | binary | empty | error
    note: str = ""
    format_desc: str = ""
    first_diff: Optional[int] = None  # byte offset of the first differing byte
    diff: Optional[DiffResult] = None
    comparison_config: dict[str, object] = field(default_factory=dict)


def scan_folder(root: str, work: Work | None = None, label: str = "Scanning folders") -> dict[str, str]:
    """Map '/'-normalised relative path -> absolute path for all files."""
    files: dict[str, str] = {}
    work = work or Work()
    def scan_error(exc: OSError) -> None:
        raise exc

    work.update(label)
    for dirpath, _dirs, names in os.walk(root, onerror=scan_error):
        work.update(label, len(files))
        for name in names:
            work.update(label, len(files))
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            files[rel] = full
    return files


def file_hash(path: str, work: Work | None = None, label: str = "Hashing bytes") -> str:
    work = work or Work()
    h = hashlib.sha256()
    done = 0
    total = os.path.getsize(path)
    with open(path, "rb") as f:
        while True:
            work.update(label, done, total)
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
    return h.hexdigest()


def first_diff_offset(path_a: str, path_b: str, work: Work | None = None) -> Optional[int]:
    """Byte offset of the first difference (streaming), or None if identical."""
    offset = 0
    work = work or Work()
    total = max(os.path.getsize(path_a), os.path.getsize(path_b))
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        while True:
            work.update("Checking exact bytes", offset, total)
            ca, cb = fa.read(CHUNK), fb.read(CHUNK)
            if ca != cb:
                for i, (x, y) in enumerate(zip(ca, cb)):
                    if x != y:
                        return offset + i
                return offset + min(len(ca), len(cb))
            if not ca:
                return None
            offset += len(ca)


def _size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def compare_folders(
    folder_a: str,
    folder_b: str,
    opts: Optional[CompareOptions] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    activity: Optional[Callable[[str], None]] = None,
) -> list[FileResult]:
    opts = (opts or CompareOptions()).prepare()
    work = Work(cancelled, activity)
    if opts.comparison_type == "copybook":
        from .schema_workflow import compare_scanned, scan_inputs

        try:
            return compare_scanned(scan_inputs(folder_a, folder_b, opts, work), opts, work, progress)
        except ComparisonCancelled:
            return []
    try:
        a_files = scan_folder(folder_a, work, "Scanning Folder A")
        b_files = scan_folder(folder_b, work, "Scanning Folder B")
        work.update("Preparing file list")
    except ComparisonCancelled:
        return []
    all_paths = sorted((p for p in set(a_files) | set(b_files) if matches_patterns(p, opts.patterns)),
                       key=str.lower)
    results: list[FileResult] = []
    total = len(all_paths)

    for i, rel in enumerate(all_paths, 1):
        try:
            work.check()
            if progress:
                progress(i - 1, total, rel)
            pa, pb = a_files.get(rel), b_files.get(rel)
            if pa and not pb:
                result = FileResult(rel, ONLY_A, _size(pa), None)
            elif pb and not pa:
                result = FileResult(rel, ONLY_B, None, _size(pb))
            else:
                result = _compare_pair(rel, pa, pb, opts, work)
            work.check()
            results.append(result)
            if progress:
                progress(i, total, rel)
        except ComparisonCancelled:
            break
    return results


def _read_file(path: str, work: Work, label: str) -> bytes:
    chunks: list[bytes] = []
    done, total = 0, os.path.getsize(path)
    with open(path, "rb") as source:
        while True:
            work.update(label, done, total)
            chunk = source.read(CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            done += len(chunk)
    data = b"".join(chunks)
    work.check()
    return data


def _compare_pair(rel: str, pa: str, pb: str, opts: CompareOptions, work: Work | None = None) -> FileResult:
    from .record_diff import compare_bytes  # keeps scanning independent of the diff engine

    sa, sb = _size(pa), _size(pb)
    work = work or Work()
    res = FileResult(rel, MODIFIED, sa, sb)
    try:
        headered = opts.comparison_type == "copybook" and opts.schema_data_format == "headered"
        # A single streaming comparison establishes equality and the first offset.
        if not headered:
            res.first_diff = first_diff_offset(pa, pb, work)
        if not headered and res.first_diff is None:
            res.status = IDENTICAL
            res.kind = "empty" if sa == 0 else "records"
            return res
        if not headered and (sa == 0 or sb == 0):
            res.kind = "empty"
            res.note = "File A is empty" if sa == 0 else "File B is empty"
            return res
        limit = opts.max_diff_mb * 1024 * 1024
        if sa > limit or sb > limit:
            if headered:
                raise ValueError(f"Headered payload comparison exceeds the {opts.max_diff_mb} MB limit; "
                                 "raise the limit to compare this pair.")
            res.kind = "binary"
            res.note = (f"Files differ (first difference at byte {res.first_diff:,}). Larger than the "
                        f"{opts.max_diff_mb} MB limit for record comparison; raise it in the settings.")
            return res
        da = _read_file(pa, work, "Reading Folder A bytes")
        db = _read_file(pb, work, "Reading Folder B bytes")
        out = compare_bytes(da, db, opts, work)
        res.kind, res.format_desc, res.diff = out.kind, out.desc, out.diff
        res.note = out.note or " ".join(out.diff.notes)
        if out.diff.total == 0:
            if headered:
                res.status = IDENTICAL
                res.note = "Payload records are identical under selected rules; file/record headers excluded."
                res.note += " " + " ".join(out.diff.notes)
            elif opts.has_ignore_rules:
                res.status = IDENTICAL
                res.note = "Identical after applying the ignore rules (bytes differ only in ignored areas)"
            else:
                res.note = ("Bytes differ but records are the same "
                            "(line endings, trailing newline, BOM or encoding)")
    except (OSError, MemoryError, ValueError) as exc:
        res.kind = "error"
        res.note = f"Could not compare: {exc}"
    return res


def compare_file_pair(rel: str, path_a: str, path_b: str, opts: CompareOptions,
                      work: Work | None = None) -> FileResult:
    """Compare an explicitly matched pair using already prepared per-file options."""
    return _compare_pair(rel, path_a, path_b, opts, work)


def summarize(results: list[FileResult]) -> dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    for r in results:
        counts[r.status] += 1
    return counts
