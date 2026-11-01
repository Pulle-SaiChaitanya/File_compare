"""Record/line diff engine: aligns records, finds differences, explains them."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Iterator, Optional
from dataclasses import replace

from . import records
from .diff_model import CONTEXT, DiffResult, DiffRow, FieldValue
from .layout import Layout, decode_field
from .options import CompareOptions
from .work import Work

ALIGN_LIMIT = 2_000_000  # max records in the non-matching middle for sequence alignment
ALIGN_SECONDS = 5
ALIGN_WORK = 2_000_000  # candidate matches across all alignment subproblems
MAX_DETAIL_LINES = 40
HEX_SHOWN = 24  # bytes of hex shown per difference


@dataclass
class Outcome:
    diff: DiffResult
    kind: str  # text | records | binary
    desc: str
    note: str = ""


def compare_bytes(da: bytes, db: bytes, opts: CompareOptions, work: Work | None = None) -> Outcome:
    work = work or Work()
    if opts.comparison_type == "copybook" and opts.schema_data_format == "headered":
        from .container_records import unpack_headered

        variable_records = opts.variable_record_rule is not None
        da = unpack_headered(da, opts.lrecl, work, opts.variable_record_rule)
        db = unpack_headered(db, opts.lrecl, work, opts.variable_record_rule)
        opts = replace(opts, schema_data_format="fixed", fmt="fixed", variable_record_rule=None)
        out = compare_bytes(da, db, opts, work)
        out.diff.notes.insert(0, "Payload-only comparison: validated 128-byte file headers and 4-byte "
                              "record descriptors are excluded; positions refer to payload columns.")
        if variable_records:
            out.diff.notes.insert(1, "Variable payloads are validated against KEY-OCCURS and padded to "
                                  "the copybook maximum for field comparison.")
        return out
    work.update("Detecting record format")
    plan = records.analyze([(da, len(da)), (db, len(db))], opts, work)
    if plan.fmt != "binary":
        try:
            work.update("Splitting Folder A records")
            ra = records.split_records(da, plan, work)
            work.update("Splitting Folder B records")
            rb = records.split_records(db, plan, work)
        except records.RecordError as exc:
            plan = records.binary_plan(plan, f"{exc}; compared as raw bytes instead.")
        else:
            return _compare_records(ra, rb, plan, opts, work)
    return _compare_binary(da, db, plan, opts, work)


# --------------------------------------------------------------- helpers

def _make_norm(opts: CompareOptions, pad: bytes) -> Callable[[bytes], bytes]:
    ranges = [(s - 1, e) for s, e in opts.ranges]
    strip = pad + b"\0"
    trim = opts.trim_trailing
    if not ranges and not trim:
        return lambda r: r

    def norm(rec: bytes) -> bytes:
        if ranges:
            buf = None
            for s, e in ranges:
                if s < len(rec):
                    buf = buf if buf is not None else bytearray(rec)
                    end = min(e, len(rec))
                    buf[s:end] = bytes(end - s)
            if buf is not None:
                rec = bytes(buf)
        return rec.rstrip(strip) if trim else rec
    return norm


def _diff_positions(a: bytes, b: bytes) -> list[int]:
    common = min(len(a), len(b))
    pos: list[int] = []
    for blk in range(0, common, 256):
        sa, sb = a[blk:blk + 256], b[blk:blk + 256]
        if sa != sb:
            pos.extend(blk + i for i, (x, y) in enumerate(zip(sa, sb)) if x != y)
    pos.extend(range(common, max(len(a), len(b))))
    return pos


def _to_ranges(positions: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for p in positions:
        if out and out[-1][1] == p:
            out[-1] = (out[-1][0], p + 1)
        else:
            out.append((p, p + 1))
    return out


def _hex(raw: bytes) -> str:
    text = raw[:HEX_SHOWN].hex().upper()
    return text + ("..." if len(raw) > HEX_SHOWN else "")


def _cols(start: int, end: int) -> str:
    return f"col {start + 1}" if end - start == 1 else f"cols {start + 1}-{end}"


def _char_marks(ta: str, tb: str) -> tuple[list, list]:
    if len(ta) * len(tb) > 65536:
        # Whole-line highlighting avoids quadratic character alignment on long repetitive lines.
        return [(0, len(ta))], [(0, len(tb))]
    ma, mb = [], []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if tag != "equal":
            if i2 > i1:
                ma.append((i1, i2))
            if j2 > j1:
                mb.append((j1, j2))
    return ma, mb


# ----------------------------------------------------------- record diff

class _Collector:
    """Collects changes plus a few unchanged rows around them, hiding long unchanged runs."""

    def __init__(self, make_row: Callable, unit: str, limit: int, context: int = CONTEXT) -> None:
        self.rows: list[DiffRow] = []
        self.make_row, self.unit, self.limit, self.ctx = make_row, unit, limit, context
        self.pending: deque = deque()
        self.hidden = 0
        self.after = 0
        self.shown = 0
        self.truncated = False

    def _skip(self) -> None:
        if self.hidden:
            self.rows.append(DiffRow("skip", text_a=f"{self.hidden:,} unchanged {self.unit}s hidden"))
            self.hidden = 0

    def context(self, ia, ib) -> None:
        if self.truncated:
            return
        if self.after > 0:
            self.rows.append(self.make_row("context", ia, ib))
            self.after -= 1
            return
        self.pending.append((ia, ib))
        if len(self.pending) > self.ctx:
            self.pending.popleft()
            self.hidden += 1

    def change(self, kind: str, ia, ib) -> None:
        if self.truncated:
            return
        if self.shown >= self.limit:
            self.truncated = True
            self._skip()
            return
        self._skip()
        while self.pending:
            self.rows.append(self.make_row("context", *self.pending.popleft()))
        self.rows.append(self.make_row(kind, ia, ib))
        self.shown += 1
        self.after = self.ctx

    def finish(self) -> list[DiffRow]:
        if not self.truncated:
            self.hidden += len(self.pending)
            self._skip()
        return self.rows


def _entries_position(na: list, nb: list) -> Iterator[tuple]:
    la, lb = len(na), len(nb)
    for i in range(max(la, lb)):
        yield (i if i < la else None, i if i < lb else None)


def _entries_sequence(na: list, nb: list, notes: list[str], work: Work) -> Iterator[tuple]:
    la, lb = len(na), len(nb)
    pre = 0
    while pre < min(la, lb) and na[pre] == nb[pre]:
        if pre % 1024 == 0:
            work.update("Finding common record prefix", pre, min(la, lb))
        pre += 1
    suf = 0
    while suf < min(la, lb) - pre and na[la - 1 - suf] == nb[lb - 1 - suf]:
        if suf % 1024 == 0:
            work.update("Finding common record suffix", suf, min(la, lb) - pre)
        suf += 1
    yield from ((i, i) for i in range(pre))
    ma, mb = na[pre:la - suf], nb[pre:lb - suf]
    blocks = _align(ma, mb, work) if ma and mb and len(ma) + len(mb) <= ALIGN_LIMIT else None
    if blocks is None and ma and mb:
        notes.append("Sequence alignment reached its time, work or size safety limit; "
                     "the unmatched region was compared by "
                     "position. Use 'By key columns' matching for a more meaningful result.")
        work.update("Alignment limit reached; comparing by position")
    if blocks is None:
        for k in range(max(len(ma), len(mb))):
            yield (pre + k if k < len(ma) else None, pre + k if k < len(mb) else None)
    else:
        ia = ib = 0
        for i, j, k in blocks:
            n = min(i - ia, j - ib)
            yield from ((pre + ia + t, pre + ib + t) for t in range(n))
            yield from ((pre + ia + t, None) for t in range(n, i - ia))
            yield from ((None, pre + ib + t) for t in range(n, j - ib))
            yield from ((pre + i + t, pre + j + t) for t in range(k))
            ia, ib = i + k, j + k
    yield from ((la - suf + k, lb - suf + k) for k in range(suf))


def _align(ma: list, mb: list, work: Work | None = None) -> Optional[list[tuple[int, int, int]]]:
    """Find matching blocks with checkpoints INSIDE the expensive matching loop."""
    work = work or Work()
    deadline = time.monotonic() + ALIGN_SECONDS
    used = 0
    index: dict[bytes, list[int]] = {}
    for j, rec in enumerate(mb):
        if j % 1024 == 0:
            work.update("Indexing records for alignment", j, len(mb))
            if time.monotonic() >= deadline:
                return None
        index.setdefault(rec, []).append(j)
    work.update("Aligning record sequences")
    queue = [(0, len(ma), 0, len(mb))]
    blocks: list[tuple[int, int, int]] = []
    while queue:
        work.check()
        if time.monotonic() >= deadline:
            return None
        alo, ahi, blo, bhi = queue.pop()
        best_i, best_j, k = alo, blo, 0
        previous: dict[int, int] = {}
        for ai in range(alo, ahi):
            if (ai - alo) % 1024 == 0:
                work.update("Aligning record sequences", used, ALIGN_WORK)
                if time.monotonic() >= deadline:
                    return None
            current: dict[int, int] = {}
            for bj in index.get(ma[ai], ()):
                used += 1
                if used >= ALIGN_WORK:
                    return None
                if used % 1024 == 0:
                    work.update("Aligning record sequences", used, ALIGN_WORK)
                    if time.monotonic() >= deadline:
                        return None
                if bj < blo:
                    continue
                if bj >= bhi:
                    break
                size = previous.get(bj - 1, 0) + 1
                current[bj] = size
                if size > k:
                    best_i, best_j, k = ai - size + 1, bj - size + 1, size
            previous = current
        i, j = best_i, best_j
        if k:
            blocks.append((i, j, k))
            if alo < i and blo < j:
                queue.append((alo, i, blo, j))
            if i + k < ahi and j + k < bhi:
                queue.append((i + k, ahi, j + k, bhi))
    blocks.sort()
    blocks.append((len(ma), len(mb), 0))
    return blocks


def _record_key(record: bytes, ranges: tuple[tuple[int, int], ...]) -> bytes:
    return b"".join(record[start:start + length] for start, length in ranges)


def _entries_key(ra: list, rb: list, start: int, length: int, work: Work,
                 ranges: tuple[tuple[int, int], ...] = (),
                 notes: list[str] | None = None) -> Iterator[tuple]:
    key_ranges = ranges or ((start, length),)

    def keyed(recs: list) -> dict:
        out: dict = {}
        for idx, rec in enumerate(recs):
            if idx % 1024 == 0:
                work.update("Indexing record keys", idx, len(recs))
            out.setdefault(_record_key(rec, key_ranges), deque()).append(idx)
        return out

    ka, kb = keyed(ra), keyed(rb)
    if notes is not None:
        duplicate_counts = []
        for label, keys in (("A", ka), ("B", kb)):
            groups = sum(len(indices) > 1 for indices in keys.values())
            records = sum(len(indices) - 1 for indices in keys.values() if len(indices) > 1)
            if records:
                duplicate_counts.append(f"{label}: {records:,} duplicate records in {groups:,} key groups")
        if duplicate_counts:
            notes.append("Duplicate composite keys detected (" + "; ".join(duplicate_counts) +
                         "); duplicate records are paired in occurrence order.")
    work.update("Sorting record keys")
    for key in sorted(set(ka) | set(kb)):
        ia, ib = ka.get(key, deque()), kb.get(key, deque())
        while ia or ib:
            yield (ia.popleft() if ia else None, ib.popleft() if ib else None)


def _compare_records(ra: list[bytes], rb: list[bytes], plan: records.Plan,
                     opts: CompareOptions, work: Work) -> Outcome:
    text_mode = plan.fmt == "text"
    decode = records.make_decoder(plan.codec)
    norm = _make_norm(opts, plan.pad)
    def normalized(recs: list[bytes]) -> list[bytes]:
        if not opts.has_ignore_rules:
            return recs
        out = []
        for i, rec in enumerate(recs):
            if i % 1024 == 0:
                work.update("Applying ignore rules", i, len(recs))
            out.append(norm(rec))
        return out

    na, nb = normalized(ra), normalized(rb)
    layout: Optional[Layout] = opts.layout if not text_mode else None
    unit = "Line" if text_mode else "Record"
    res = DiffResult(encoding_a=plan.codec, encoding_b=plan.codec, unit=unit, notes=list(plan.notes))
    if layout:
        res.notes.extend(layout.warnings)
        res.fields = list(layout.fields)
        res.included_fields = (opts.selected_fields if opts.selected_fields is not None
                               else tuple(f.name for f in layout.fields))
    included_fields = set(res.included_fields)
    by_key = opts.match == "key"
    ks, kl = opts.key_start - 1, opts.key_length
    key_ranges = opts.key_ranges or ((ks, kl),)

    def describe(a_raw: bytes, b_raw: bytes, a_norm: bytes, b_norm: bytes, ta: str, tb: str):
        if text_mode:
            ma, mb = _char_marks(ta, tb)
            return ma, mb, []
        ranges = _to_ranges(_diff_positions(a_norm, b_norm))
        lines = (_field_details(ranges, a_raw, b_raw, layout, decode) if layout
                 else [f"{_cols(s, e)}: A '{decode(a_raw[s:e][:HEX_SHOWN])}' [{_hex(a_raw[s:e])}]  ->  "
                       f"B '{decode(b_raw[s:e][:HEX_SHOWN])}' [{_hex(b_raw[s:e])}]"
                       for s, e in ranges[:MAX_DETAIL_LINES]])
        if len(lines) > MAX_DETAIL_LINES:
            lines = lines[:MAX_DETAIL_LINES] + [f"... {len(lines) - MAX_DETAIL_LINES} more"]
        return ranges, ranges, lines

    def make_row(kind: str, ia, ib) -> DiffRow:
        a_raw = ra[ia] if ia is not None else b""
        b_raw = rb[ib] if ib is not None else b""
        ta = decode(a_raw) if ia is not None else ""
        tb = decode(b_raw) if ib is not None else ""
        row = DiffRow(kind, None if ia is None else ia + 1, None if ib is None else ib + 1, ta, tb)
        if kind == "changed":
            row.marks_a, row.marks_b, row.detail = describe(a_raw, b_raw, na[ia], nb[ib], ta, tb)
        if by_key and kind != "context":
            key = _record_key((a_raw if ia is not None else b_raw), key_ranges)
            row.detail.insert(0, f"key: '{decode(key)}' [{key.hex().upper()}]")
        if layout and kind in ("added", "removed", "changed"):
            ranges = row.marks_a
            for index, f in enumerate(layout.fields):
                if index % 64 == 0:
                    work.check()
                va, vb = a_raw[f.start:f.end], b_raw[f.start:f.end]
                text_a = decode_field(va, f, decode) if ia is not None else ""
                text_b = decode_field(vb, f, decode) if ib is not None else ""
                if f.ftype != "char":
                    if va:
                        text_a += f" [{va.hex().upper()}]"
                    if vb:
                        text_b += f" [{vb.hex().upper()}]"
                changed = (f.name in included_fields and
                           (kind != "changed" or any(s < f.end and e > f.start for s, e in ranges)))
                row.field_values.append(FieldValue(text_a, text_b, changed))
        return row

    if by_key:
        entries = _entries_key(ra, rb, ks, kl, work, opts.key_ranges, res.notes)
    elif opts.match == "position":
        entries = _entries_position(na, nb)
    else:
        entries = _entries_sequence(na, nb, res.notes, work)

    coll = _Collector(make_row, unit.lower(), opts.max_diff_rows)
    work.update("Comparing records")
    for count, (ia, ib) in enumerate(entries):
        if count % 1024 == 0:
            work.update("Comparing records", count, max(len(ra), len(rb)))
        if ia is None:
            res.added += 1
            coll.change("added", ia, ib)
        elif ib is None:
            res.removed += 1
            coll.change("removed", ia, ib)
        elif na[ia] == nb[ib]:
            coll.context(ia, ib)
        else:
            res.changed += 1
            coll.change("changed", ia, ib)
    res.rows = coll.finish()
    res.truncated = coll.truncated
    work.check()
    return Outcome(res, "text" if text_mode else "records", plan.desc)


def _field_details(ranges, a_raw, b_raw, layout: Layout, decode) -> list[str]:
    lines: list[str] = []
    covered = bytearray(layout.length)
    for f in layout.fields:
        covered[f.start:f.end] = b"\1" * f.length
        if not any(s < f.end and e > f.start for s, e in ranges):
            continue
        va, vb = a_raw[f.start:f.end], b_raw[f.start:f.end]
        show_hex = f.ftype != "char"
        fmt = lambda raw: decode_field(raw, f, decode) + (f" [{_hex(raw)}]" if show_hex and raw else "")
        lines.append(f"{f.name} ({_cols(f.start, f.end)}, {f.ftype}): A {fmt(va)}  ->  B {fmt(vb)}")
    outside = [p for s, e in ranges for p in range(s, e) if p >= len(covered) or not covered[p]]
    for s, e in _to_ranges(outside)[:10]:
        lines.append(f"(no field) {_cols(s, e)}: A [{_hex(a_raw[s:e])}]  ->  B [{_hex(b_raw[s:e])}]")
    return lines


# ----------------------------------------------------------- raw bytes

def _compare_binary(da: bytes, db: bytes, plan: records.Plan, opts: CompareOptions, work: Work) -> Outcome:
    width, block = 16, 65536
    decode = records.make_decoder(plan.codec)
    res = DiffResult(encoding_a=plan.codec, encoding_b=plan.codec, unit="Offset", notes=list(plan.notes))
    if opts.has_ignore_rules:
        res.notes.append("Ignore rules apply to records and were not used for the raw byte compare.")
    common = min(len(da), len(db))
    spaced = lambda raw: " ".join(f"{b:02X}" for b in raw)

    def add(kind: str, off: int, sa: bytes, sb: bytes) -> None:
        if len(res.rows) >= opts.max_diff_rows:
            res.truncated = True
            return
        row = DiffRow(kind, off if sa else None, off if sb else None, decode(sa), decode(sb))
        if kind == "changed":
            row.marks_a = row.marks_b = _to_ranges(_diff_positions(sa, sb))
        row.detail = [f"A: {spaced(sa)}" if sa else "", f"B: {spaced(sb)}" if sb else ""]
        row.detail = [d for d in row.detail if d]
        res.rows.append(row)

    for blk in range(0, common, block):
        work.update("Comparing raw bytes", blk, max(len(da), len(db)))
        if da[blk:blk + block] == db[blk:blk + block]:
            continue
        for off in range(blk, min(blk + block, common), width):
            sa, sb = da[off:off + width], db[off:off + width]
            if sa != sb:
                res.changed += 1
                add("changed", off, sa, sb)
    for data, kind in ((da, "removed"), (db, "added")):
        if len(data) > common:
            extra = -(-(len(data) - common) // width)
            if kind == "removed":
                res.removed = extra
            else:
                res.added = extra
            for off in range(common, min(len(data), common + opts.max_diff_rows * width), width):
                if (off - common) % 16384 == 0:
                    work.update("Comparing extra bytes", off, len(data))
                add(kind, off, data[off:off + width] if kind == "removed" else b"",
                    data[off:off + width] if kind == "added" else b"")
    work.check()
    return Outcome(res, "binary", plan.desc)
