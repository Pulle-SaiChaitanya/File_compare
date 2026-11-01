"""Detects file structure (text / fixed / RDW / raw) and splits files into records."""
from __future__ import annotations

import codecs
import math
from collections import Counter
from dataclasses import dataclass, field
from functools import reduce
from typing import Callable

from . import ebcdic
from .options import EBCDIC_CODECS, CompareOptions
from .work import Work

ebcdic.register()
SAMPLE = 65536
MAX_GUESS = 8192  # longest record length considered by auto-detection
SMALL_TEXT = 256  # no-newline files up to this size are treated as one text line


class RecordError(Exception):
    """The data does not follow the expected record structure."""


@dataclass
class Plan:
    fmt: str  # text | fixed | rdw | binary
    codec: str
    lrecl: int = 0
    bdw: bool = False
    desc: str = ""
    notes: list[str] = field(default_factory=list)
    candidates: list[tuple[int, float]] = field(default_factory=list)
    confident: bool = True

    @property
    def ebcdic(self) -> bool:
        return self.codec in EBCDIC_CODECS

    @property
    def pad(self) -> bytes:
        return b"\x40" if self.ebcdic else b" "


# ------------------------------------------------------------------ encoding

_NONPRINT = {i: "\u00b7" for i in (*range(0, 32), *range(127, 160))}
_EBCDIC_TEXT = bytes([0x40, 0x4B, 0x5C, 0x60, 0x61, 0x6B]) + b"".join(
    bytes(range(a, b)) for a, b in ((0x81, 0x8A), (0x91, 0x9A), (0xA2, 0xAA), (0xC1, 0xCA),
                                    (0xD1, 0xDA), (0xE2, 0xEA), (0xF0, 0xFA)))
_ASCII_TEXT = bytes(range(0x20, 0x7F)) + b"\r\n\t"
_CONTROL = bytes(b for b in range(32) if b not in (9, 10, 12, 13)) + b"\x7f"


def make_decoder(codec: str) -> Callable[[bytes], str]:
    """Bytes -> printable text (control characters shown as a middle dot)."""
    def decode(raw: bytes) -> str:
        return raw.decode(codec, "replace").translate(_NONPRINT)
    return decode


def _count_in(sample: bytes, allowed: bytes) -> int:
    return len(sample) - len(sample.translate(None, allowed))


def _scores(sample: bytes) -> tuple[float, float]:
    n = len(sample) or 1
    return _count_in(sample, _EBCDIC_TEXT) / n, _count_in(sample, _ASCII_TEXT) / n


def detect_single_byte_codec(sample: bytes) -> str:
    sample = sample[:SAMPLE]
    eb, asc = _scores(sample)
    if eb > asc or (eb == asc and sample.count(b"\x40") > sample.count(b" ")):
        return "cp037"
    return "latin-1"


def _utf8_ok(head: bytes) -> bool:
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head)
        return True
    except UnicodeDecodeError:
        return False


def pick_text_codec(heads: list[bytes]) -> str:
    for codec in ("utf-8", "cp1252"):
        try:
            for h in heads:
                codecs.getincrementaldecoder(codec)().decode(h[:8_000_000])
            return codec
        except UnicodeDecodeError:
            continue
    return "latin-1"


def looks_delimited(data: bytes, size: int) -> bool:
    """True for ordinary text with line breaks (not EBCDIC / binary records)."""
    head = data[:SAMPLE]
    if not head:
        return True
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    if b"\x00" in head or _count_in(head, _CONTROL) > len(head) * 0.01:
        return False
    eb, asc = _scores(head)
    if eb > asc:
        return False
    if b"\n" in head:
        return asc >= 0.7 or _utf8_ok(head)
    return size <= SMALL_TEXT and asc >= 0.9


# ----------------------------------------------------------------- splitting

def split_text(data: bytes, work: Work | None = None) -> list[bytes]:
    work = work or Work()
    work.check()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        data = data.decode("utf-16", "replace").encode("utf-8")
    elif data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for i, ln in enumerate(lines):
        if i % 1024 == 0:
            work.update("Preparing text lines", i, len(lines))
        if ln.endswith(b"\r"):
            lines[i] = ln[:-1]
    return lines


def parse_rdw(data: bytes, bdw: bool = False, limit: int = 0, partial_ok: bool = False,
              work: Work | None = None) -> list[bytes]:
    """Split RECFM=V/VB data (4-byte RDW, optional 4-byte BDW) into record payloads."""
    n, pos = len(data), 0
    recs: list[bytes] = []
    zero = b"\0\0"
    work = work or Work()
    work.check()
    while pos < n and not (limit and len(recs) >= limit):
        block_end = n
        if bdw:
            if pos + 4 > n:
                if partial_ok:
                    break
                raise RecordError(f"Truncated BDW at offset {pos}")
            blen = int.from_bytes(data[pos:pos + 2], "big")
            if blen < 8 or data[pos + 2:pos + 4] != zero:
                raise RecordError(f"Invalid block descriptor (BDW) at offset {pos}")
            block_end = pos + blen
            pos += 4
        stop = min(block_end, n)
        while pos < stop and not (limit and len(recs) >= limit):
            if len(recs) % 1024 == 0:
                work.update("Reading RDW records", pos, n)
            if pos + 4 > stop:
                if partial_ok and block_end > n:
                    return recs
                raise RecordError(f"Truncated record descriptor (RDW) at offset {pos}")
            rlen = int.from_bytes(data[pos:pos + 2], "big")
            if rlen < 4 or data[pos + 2:pos + 4] != zero or pos + rlen > block_end:
                raise RecordError(f"Invalid record descriptor (RDW) at offset {pos}")
            if pos + rlen > n:
                if partial_ok:
                    return recs
                raise RecordError(f"Record at offset {pos} runs past end of file")
            recs.append(data[pos + 4:pos + rlen])
            pos += rlen
        if bdw:
            pos = block_end
    return recs


def split_records(data: bytes, plan: Plan, work: Work | None = None) -> list[bytes]:
    work = work or Work()
    work.check()
    if plan.fmt == "text":
        return split_text(data, work)
    if plan.fmt == "fixed":
        step = plan.lrecl
        out = []
        for i in range(0, len(data), step):
            if len(out) % 1024 == 0:
                work.update("Splitting fixed records", i, len(data))
            out.append(data[i:i + step])
        return out
    if plan.fmt == "rdw":
        return parse_rdw(data, plan.bdw, work=work)
    raise RecordError(f"Unsupported format {plan.fmt}")


# ---------------------------------------------------------- LRECL detection

def guess_lrecl(samples: list[tuple[bytes, int]], work: Work | None = None) -> tuple[int, float, list[tuple[int, float]]]:
    """Guess a fixed record length: (lrecl, score 0..1, ranked candidates).

    The true length divides every file size. At that length each byte column
    holds similar values (padding, constants, record types), so we score the
    average chance that two records agree in a column and keep the best length.
    """
    work = work or Work()
    sizes = [s for _, s in samples if s > 0]
    if not sizes:
        return 0, 0.0, []
    g = reduce(math.gcd, sizes)
    cands = [d for d in range(2, min(g, MAX_GUESS) + 1) if g % d == 0]
    head = max((d for d, _ in samples), key=len)[:131072 if len(cands) <= 60 else 32768]
    scored: list[tuple[int, float]] = []
    for min_rows in (3, 2):
        scored = []
        for i, d in enumerate(cands):
            work.update("Detecting record length", i, len(cands))
            if len(head) // d >= min_rows:
                scored.append((d, _column_agreement(head, d, work)))
        if scored:
            break
    if not scored:
        return (g, 0.2, [(g, 0.2)]) if 2 <= g <= MAX_GUESS else (0, 0.0, [])
    best_d, best_s = max(scored, key=lambda t: t[1])
    # Multiples of the true length look as consistent as the length itself, so take the
    # smallest length that is practically as good; real sub-multiples score clearly lower.
    limit = 1.1 * (1 - best_s) + 0.0015
    best_d = min((d for d, s in scored if 1 - s <= limit and d >= 8 and best_d % d == 0), default=best_d)
    ranked = sorted(scored, key=lambda t: -t[1])[:6]
    return best_d, dict(scored)[best_d], ranked


def _column_agreement(head: bytes, d: int, work: Work | None = None) -> float:
    """Unbiased probability that two records have the same byte in a column (0..1)."""
    rows = len(head) // d
    block = head[:rows * d]
    work = work or Work()
    same = 0
    for c in range(d):
        if c % 64 == 0:
            work.check()
        same += sum(k * (k - 1) for k in Counter(block[c::d]).values())
    return same / (d * rows * (rows - 1))


# ------------------------------------------------------------------- analyse

def _describe(plan: Plan) -> str:
    enc = plan.codec.upper() if plan.ebcdic else plan.codec
    kind = "EBCDIC " if plan.ebcdic else ""
    if plan.fmt == "text":
        return f"Text lines ({plan.codec})"
    if plan.fmt == "fixed":
        return f"Fixed {plan.lrecl}-byte records, {kind}{enc}"
    if plan.fmt == "rdw":
        return f"Variable (RDW{'+BDW' if plan.bdw else ''}) records, {kind}{enc}"
    return f"Raw bytes (16 per row), {kind}{enc}"


def analyze(samples: list[tuple[bytes, int]], opts: CompareOptions, work: Work | None = None) -> Plan:
    """Decide how to read the files. samples = [(leading bytes, full size), ...]."""
    work = work or Work()
    work.check()
    heads = [d for d, _ in samples]
    notes: list[str] = []
    candidates: list[tuple[int, float]] = []
    confident = True
    layout_len = opts.layout.length if opts.layout is not None else 0
    lrecl = opts.lrecl or 0
    bdw = opts.bdw
    fmt = opts.fmt

    if fmt == "auto":
        if lrecl:
            fmt = "fixed"
        elif all(looks_delimited(d, s) for d, s in samples):
            fmt = "text"
        else:
            fmt = ""
            for try_bdw in ((True,) if bdw else (True, False)):  # BDW first: RDW parsing would swallow a whole block
                try:
                    if all(parse_rdw(d, try_bdw, 2000, len(d) < s, work) or not s for d, s in samples):
                        fmt, bdw = "rdw", try_bdw
                        break
                except RecordError:
                    continue
            if not fmt:
                fmt = "fixed"

    if opts.encoding != "auto":
        codec = opts.encoding
    elif fmt == "text":
        # UTF-16 files are re-encoded as UTF-8 when split, so they are shown as UTF-8
        codec = "utf-8" if any(h.startswith((b"\xff\xfe", b"\xfe\xff")) for h in heads) else pick_text_codec(heads)
    else:
        codec = detect_single_byte_codec(b"".join(h[:SAMPLE] for h in heads))

    if fmt == "fixed":
        if not lrecl and layout_len and all(s % layout_len == 0 for _, s in samples):
            lrecl = layout_len
        if not lrecl:
            lrecl, score, candidates = guess_lrecl(samples, work)
            if not lrecl or score < 0.35 or (lrecl < 8 and score < 0.8):
                confident = False
                notes.append("Record length could not be determined reliably; compared as raw bytes. "
                             "Enter the record length (LRECL) in the settings for a record-level compare.")
                fmt = "binary"
            else:
                notes.append(f"Record length {lrecl} was auto-detected (score {score:.2f}).")
        if fmt == "fixed" and any(s % lrecl for _, s in samples):
            notes.append(f"File size is not a multiple of {lrecl}; the last record is partial.")

    plan = Plan(fmt, codec, lrecl, bdw, notes=notes, candidates=candidates, confident=confident)
    plan.desc = _describe(plan)
    work.check()
    return plan


def binary_plan(plan: Plan, reason: str) -> Plan:
    out = Plan("binary", plan.codec, notes=[*plan.notes, reason], confident=False)
    out.desc = _describe(out)
    return out
