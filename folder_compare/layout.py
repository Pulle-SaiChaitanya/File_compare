"""Record layouts: a simple COBOL copybook or CSV column definitions.

Used only to show field-level differences; it never affects Identical/Modified.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

_TYPE_ALIASES = {
    "char": "char", "x": "char", "text": "char", "string": "char",
    "zoned": "zoned", "display": "zoned", "9": "zoned",
    "packed": "packed", "comp-3": "packed", "comp3": "packed", "pd": "packed",
    "binary": "binary", "comp": "binary", "comp-4": "binary", "comp-5": "binary", "int": "binary",
    "ubinary": "ubinary", "uint": "ubinary",
    "float": "float", "hex": "hex", "raw": "hex",
}


@dataclass
class Field:
    name: str
    start: int  # 0-based byte offset
    length: int
    ftype: str = "char"  # char | zoned | packed | binary | ubinary | float | hex
    decimals: int = 0

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass
class Layout:
    fields: list[Field]
    length: int
    warnings: list[str] = field(default_factory=list)


def parse_layout(text: str) -> Optional[Layout]:
    """Parse a copybook or CSV layout. Returns None for blank text; raises ValueError."""
    text = (text or "").strip()
    if not text:
        return None
    first = next((ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith(("#", "*"))), "")
    if "," in first and not re.search(r"\bPIC(TURE)?\b", text, re.I):
        layout = _parse_csv(text)
    else:
        layout = _parse_copybook(text)
    if not layout.fields:
        raise ValueError("The layout does not define any fields.")
    return layout


# ---------------------------------------------------------------- CSV layout

def _parse_csv(text: str) -> Layout:
    fields_: list[Field] = []
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            raise ValueError(f"Layout line {no}: expected name,start,length[,type[,decimals]].")
        try:
            start, length = int(parts[1]), int(parts[2])
        except ValueError:
            if not fields_:
                continue  # header row
            raise ValueError(f"Layout line {no}: start and length must be numbers.") from None
        if start < 1 or length < 1:
            raise ValueError(f"Layout line {no}: start and length must be 1 or more.")
        ftype = "char"
        if len(parts) > 3 and parts[3]:
            ftype = _TYPE_ALIASES.get(parts[3].lower(), "")
            if not ftype:
                raise ValueError(f"Layout line {no}: unknown type '{parts[3]}'. "
                                 "Use char, zoned, packed, binary, ubinary, float or hex.")
        decimals = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        fields_.append(Field(parts[0], start - 1, length, ftype, decimals))
    length = max((f.end for f in fields_), default=0)
    return Layout(fields_, length)


# ------------------------------------------------------------ COBOL copybook

@dataclass
class _Node:
    level: int
    name: str
    pic: str = ""
    usage: str = ""
    occurs: int = 0
    redefines: bool = False
    separate: bool = False
    children: list["_Node"] = field(default_factory=list)


_USAGE_RE = re.compile(
    r"\b(COMP-[1-5]|COMPUTATIONAL-[1-5]|COMPUTATIONAL|COMP|BINARY|PACKED-DECIMAL|DISPLAY|INDEX|POINTER)\b")
_KEYWORDS = {"PIC", "PICTURE", "USAGE", "OCCURS", "REDEFINES", "COMP", "COMP-1", "COMP-2", "COMP-3",
             "COMP-4", "COMP-5", "BINARY", "PACKED-DECIMAL", "VALUE", "VALUES", "SYNC", "SYNCHRONIZED",
             "DISPLAY", "SIGN", "BLANK", "JUSTIFIED", "JUST"}


def _clean_copybook(text: str) -> str:
    lines = text.splitlines()
    nonblank = [ln for ln in lines if ln.strip()]
    fixed = bool(nonblank) and all(len(ln) > 6 and all(c.isdigit() or c == " " for c in ln[:6])
                                   for ln in nonblank)
    out = []
    for ln in lines:
        if fixed:
            if len(ln) > 6 and ln[6] in "*/":
                continue
            ln = ln[6:72]
        elif ln.strip().startswith("*"):
            continue
        out.append(ln.split("*>")[0])
    joined = " ".join(out)
    return re.sub(r"'[^']*'|\"[^\"]*\"", "''", joined)  # drop VALUE literals (may contain periods)


def _parse_copybook(text: str) -> Layout:
    warnings: list[str] = []
    stack: list[_Node] = []
    roots: list[_Node] = []
    for stmt in re.split(r"\.(?:\s+|$)", _clean_copybook(text)):
        tokens = stmt.split()
        if not tokens or not tokens[0].isdigit():
            continue
        level = int(tokens[0])
        if level in (66, 88):
            continue
        if len(tokens) > 1 and tokens[1].upper() not in _KEYWORDS:
            name, body = tokens[1], " ".join(tokens[2:]).upper()
        else:
            name, body = "FILLER", " ".join(tokens[1:]).upper()
        node = _Node(level, name)
        node.redefines = bool(re.search(r"\bREDEFINES\b", body))
        m = re.search(r"\bOCCURS\s+(\d+)(?:\s+TO\s+(\d+))?", body)
        if m:
            node.occurs = int(m.group(2) or m.group(1))
            if "DEPENDING" in body:
                warnings.append(f"{name}: OCCURS DEPENDING ON uses the maximum count ({node.occurs}).")
        m = re.search(r"\bPIC(?:TURE)?\s+(?:IS\s+)?(\S+)", body)
        node.pic = m.group(1) if m else ""
        m = _USAGE_RE.search(body)
        node.usage = m.group(1) if m else ""
        node.separate = "SEPARATE" in body
        if re.search(r"\bSYNC(HRONIZED)?\b", body):
            warnings.append(f"{name}: SYNC alignment slack bytes are not modelled; offsets may be off.")
        if level == 77:
            continue
        while stack and stack[-1].level >= level:
            stack.pop()
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)

    top = [r for r in roots if r.level == 1] or roots
    if len(top) > 1:
        warnings.append("Multiple 01-level records found; only the first is used.")
    if not top:
        raise ValueError("No data items were found in the copybook.")
    out: list[Field] = []
    size = _emit(top[0], 0, "", out, warnings)
    return Layout(out, size, warnings)


def _expand_pic(pic: str) -> str:
    return re.sub(r"(.)\((\d+)\)", lambda m: m.group(1) * int(m.group(2)), pic.upper())


def _elementary(node: _Node) -> Optional[tuple[int, str, int]]:
    usage = node.usage.replace("COMPUTATIONAL", "COMP")
    if usage == "COMP-1":
        return 4, "float", 0
    if usage == "COMP-2":
        return 8, "float", 0
    if not node.pic:
        return None
    pic = _expand_pic(node.pic)
    signed = pic.startswith("S")
    digits = pic.count("9")
    decimals = pic.partition("V")[2].count("9")
    chars = len(pic.replace("S", "").replace("V", "").replace("P", ""))
    if usage == "PACKED-DECIMAL" or usage == "COMP-3":
        return digits // 2 + 1, "packed", decimals
    if usage in ("COMP", "COMP-4", "COMP-5", "BINARY"):
        size = 2 if digits <= 4 else 4 if digits <= 9 else 8
        return size, "binary" if signed else "ubinary", decimals
    if set(pic) <= set("9SVP"):
        if node.separate:
            return chars + 1, "char", 0
        return chars, "zoned", decimals
    return chars, "char", 0


def _emit(node: _Node, offset: int, suffix: str, out: list[Field], warnings: list[str]) -> int:
    pos = offset
    for i in range(max(node.occurs, 1)):
        sfx = suffix + (f"({i + 1})" if node.occurs else "")
        elem = _elementary(node)
        if elem is not None and not node.children:
            size, ftype, dec = elem
            out.append(Field(node.name + sfx, pos, size, ftype, dec))
            pos += size
            continue
        for child in node.children:
            if child.redefines:
                warnings.append(f"{child.name}: REDEFINES ignored (overlays existing storage).")
                continue
            pos += _emit(child, pos, sfx, out, warnings)
    return pos - offset


# ------------------------------------------------------------- field decoding

def _fmt_number(value: int, decimals: int, negative: bool) -> str:
    digits = str(value).rjust(decimals + 1, "0")
    if decimals:
        digits = digits[:-decimals] + "." + digits[-decimals:]
    return ("-" if negative and value else "") + digits


def decode_field(raw: bytes, f: Field, decode_text: Callable[[bytes], str]) -> str:
    """Human-readable value of a field; falls back to hex when the bytes are not valid."""
    if not raw:
        return "(missing)"
    hex_ = raw.hex().upper()
    try:
        if f.ftype == "char":
            return "'" + decode_text(raw).rstrip(" \u00b7") + "'"
        if f.ftype == "packed":
            nibbles = [n for b in raw for n in (b >> 4, b & 15)]
            sign, digits = nibbles[-1], nibbles[:-1]
            if sign < 0xA or any(d > 9 for d in digits):
                return f"invalid packed [{hex_}]"
            return _fmt_number(int("".join(map(str, digits)) or "0"), f.decimals, sign in (0xB, 0xD))
        if f.ftype == "zoned":
            digits = [b & 15 for b in raw]
            if any(d > 9 for d in digits):
                return f"invalid zoned [{hex_}]"
            return _fmt_number(int("".join(map(str, digits))), f.decimals, (raw[-1] >> 4) in (0xD, 0x7))
        if f.ftype in ("binary", "ubinary"):
            value = int.from_bytes(raw, "big", signed=f.ftype == "binary")
            return _fmt_number(abs(value), f.decimals, value < 0)
    except ValueError:
        return f"[{hex_}]"
    return hex_
