"""Versioned JSON schemas and conservative copybook conversion, independent of diffing."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .layout import Field, Layout
from .work import Work

MAX_RECORD_LENGTH = 1_048_576
MAX_FIELDS = 10_000
MAX_SCHEMA_BYTES = 4 * 1024 * 1024
FIELD_TYPES = {"char", "zoned", "packed", "binary", "ubinary", "hex"}


@dataclass(frozen=True)
class SchemaField:
    name: str
    start: int  # 1-based byte column in JSON
    length: int
    pic: str
    type: str
    decimals: int = 0
    signed: bool = False


@dataclass(frozen=True)
class Schema:
    record_length: int
    fields: tuple[SchemaField, ...]
    schema_version: int = 1
    record_format: str = "fixed"
    notes: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"

    def to_layout(self) -> Layout:
        return Layout([Field(f.name, f.start - 1, f.length, f.type, f.decimals)
                       for f in self.fields], self.record_length, list(self.notes))


def _integer(value: object, title: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{title} must be an integer between {minimum} and {maximum}.")
    return value


def _pic_shape(pic: str, usage: str) -> tuple[int, str, int, bool]:
    """Storage rules are isolated here so additional COBOL usages can be added safely."""
    pic = pic.upper()
    parts = re.findall(r"([XA9SV])(?:\((\d+)\))?", pic)
    if "".join(c + (f"({n})" if n else "") for c, n in parts) != pic:
        raise ValueError(f"Unsupported PIC '{pic}' (edited/scaled/national pictures are not supported).")
    counts = []
    for char, repeat in parts:
        count = int(repeat) if repeat else 1
        if not 1 <= count <= MAX_RECORD_LENGTH or (char in "SV" and count != 1):
            raise ValueError(f"Invalid repetition in PIC '{pic}'.")
        counts.append((char, count))
    if not counts:
        raise ValueError("PIC must not be empty.")
    if all(c in "XA" for c, _ in counts):
        if usage != "DISPLAY":
            raise ValueError(f"PIC '{pic}' requires DISPLAY usage.")
        return sum(n for _, n in counts), "char", 0, False
    signed = counts[0][0] == "S"
    numeric = counts[1:] if signed else counts
    if not numeric or any(c not in "9V" for c, _ in numeric):
        raise ValueError(f"Unsupported numeric PIC '{pic}'.")
    symbols = "".join(c for c, _ in numeric)
    if not re.fullmatch(r"(?:9+(?:V9+)?|V9+)", symbols):
        raise ValueError(f"Unsupported numeric PIC '{pic}'.")
    digits = sum(n for c, n in numeric if c == "9")
    decimal_part = False
    decimals = 0
    for char, count in numeric:
        if char == "V":
            decimal_part = True
        elif decimal_part:
            decimals += count
    if usage == "DISPLAY":
        return digits, "zoned", decimals, signed
    if digits > 18:
        raise ValueError(f"PIC '{pic}': computational fields are limited to 18 digits.")
    if usage in ("COMP-3", "PACKED-DECIMAL"):
        return digits // 2 + 1, "packed", decimals, signed
    if usage in ("COMP", "COMP-4", "COMP-5", "BINARY"):
        return (2 if digits <= 4 else 4 if digits <= 9 else 8), \
            ("binary" if signed else "ubinary"), decimals, signed
    raise ValueError(f"Unsupported usage '{usage}'.")


def parse_schema(text: str) -> Schema:
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("Schema must be a JSON object, not a settings profile or array.")
    if raw and "schema_version" not in raw and all(type(value) is int for value in raw.values()):
        raise ValueError(f"Legacy name-to-length JSON ({sum(raw.values()):,} bytes) has no verified "
                         "offsets/PIC/storage metadata. Regenerate from the copybook into a NEW JSON "
                         "output folder; this file will not be overwritten.")
    version = _integer(raw.get("schema_version"), "schema_version", 1, 1)
    if raw.get("record_format", "fixed") != "fixed":
        raise ValueError("Only fixed records are supported by schema version 1.")
    length = _integer(raw.get("record_length"), "record_length", 1, MAX_RECORD_LENGTH)
    entries = raw.get("fields")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FIELDS:
        raise ValueError(f"fields must be a non-empty array with at most {MAX_FIELDS} fields.")
    out: list[SchemaField] = []
    names: set[str] = set()
    for number, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Field {number} must be an object.")
        name, pic, kind = item.get("name"), item.get("pic"), item.get("type")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Field {number}: name is required.")
        if name.casefold() in names:
            raise ValueError(f"Duplicate field name '{name}'. Use unique qualified names, including FILLER.")
        names.add(name.casefold())
        if not isinstance(pic, str) or not isinstance(kind, str) or kind not in FIELD_TYPES:
            raise ValueError(f"{name}: pic and a supported type are required.")
        if item.get("redefines"):
            raise ValueError(f"{name}: REDEFINES overlays are not supported by schema version 1.")
        start = _integer(item.get("start"), f"{name}.start", 1, length)
        size = _integer(item.get("length"), f"{name}.length", 1, length)
        usage = {"packed": "COMP-3", "binary": "COMP", "ubinary": "COMP"}.get(kind, "DISPLAY")
        shape = _pic_shape(pic, usage) if pic else None
        decimals = _integer(item.get("decimals", shape[2] if shape else 0),
                            f"{name}.decimals", 0, MAX_RECORD_LENGTH)
        signed = item.get("signed", pic.upper().startswith("S") or kind == "binary")
        if type(signed) is not bool:
            raise ValueError(f"{name}.signed must be true or false.")
        if start - 1 + size > length:
            raise ValueError(f"{name} extends past record_length {length}.")
        if pic:
            expected_size, expected_kind, expected_decimals, expected_signed = _pic_shape(pic, usage)
            if (size, kind, decimals, signed) != (expected_size, expected_kind, expected_decimals, expected_signed):
                raise ValueError(f"{name}: length/type/decimals/signed do not agree with PIC '{pic}'.")
        elif kind != "hex":
            raise ValueError(f"{name}: only hex column fields may have an empty PIC.")
        out.append(SchemaField(name, start, size, pic.upper(), kind, decimals, signed))
    ordered = sorted(out, key=lambda f: f.start)
    end = 0
    for item in ordered:
        if item.start <= end:
            raise ValueError(f"{item.name}: overlapping fields are not supported.")
        end = item.start - 1 + item.length
    notes = raw.get("notes", [])
    if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
        raise ValueError("notes must be an array of strings.")
    return Schema(length, tuple(out), version, notes=tuple(notes))


def read_definition(path: str) -> str:
    with open(path, "rb") as source:
        data = source.read(MAX_SCHEMA_BYTES + 1)
    if len(data) > MAX_SCHEMA_BYTES:
        raise ValueError(f"Definition exceeds the {MAX_SCHEMA_BYTES // 1024 // 1024} MB limit.")
    return data.decode("utf-8-sig")


def load_schema(path: str) -> Schema:
    return parse_schema(read_definition(path))


@dataclass
class _Item:
    level: int
    name: str
    pic: str = ""
    usage: str = ""
    occurs: int = 1
    repeated: bool = False
    children: list["_Item"] = field(default_factory=list)
    redefines: str = ""


def _copybook_source(text: str) -> str:
    lines = text.splitlines()

    def sequence_only(line: str) -> bool:
        return bool(re.fullmatch(r"\s*\d{6}\s*", line))

    significant = [line for line in lines if line.strip()
                   and not line.lstrip().startswith(("*", "*>"))
                   and not sequence_only(line)]
    fixed = bool(significant) and all(
        len(line) >= 7 and all(c.isalnum() or c == " " for c in line[:6])
        and line[6] in " */-Dd"
        and not re.match(r"\s*\d{2}\s+[A-Z]", line[:7], re.I)
        for line in significant)
    out = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("*") or sequence_only(line):
            continue
        if fixed:
            if len(line) < 7 or line[6] in "*/":
                continue
            if line[6] != " ":
                raise ValueError("Copybook continuation/debug indicators are not supported.")
            line = line[7:72]
        if line.lstrip().startswith("*"):
            continue
        # Mask literals before removing comments, since VALUE may contain '*>', periods or level numbers.
        line = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", "''", line)
        out.append(line.split("*>", 1)[0])
    return "\n".join(out).upper()


_DECLARATION_RE = re.compile(
    r"(?<![\w.-])(\d{2})\s+([A-Z0-9][A-Z0-9-]*)(?=\s|\.|$)")


def parse_copybook(text: str, work: Work | None = None, original_storage: bool = False) -> Schema:
    """Parse one fixed-storage record; reject unsupported storage instead of guessing offsets."""
    schemas = parse_copybooks(text, work, original_storage)
    if len(schemas) != 1:
        raise ValueError(f"Found {len(schemas)} level-01 record layouts; use parse_copybooks to select one.")
    return schemas[0]


def parse_copybooks(text: str, work: Work | None = None,
                    original_storage: bool = False) -> tuple[Schema, ...]:
    """Parse each level-01 record layout separately, or one fragment layout."""
    work = work or Work()
    source = _copybook_source(text)
    declarations = list(_DECLARATION_RE.finditer(source))
    roots = [match.start() for match in declarations if match[1] == "01"]
    if not roots:
        return (_parse_copybook_source(source, work, original_storage),)
    fragments = [match for match in declarations if match.start() < roots[0]]
    if fragments and not original_storage:
        return (_parse_copybook_source(source, work, original_storage),)
    chunks = []
    if fragments and original_storage:
        chunks.append(source[:roots[0]])
    chunks.extend(source[start:end] for start, end in
                  zip(roots, [*roots[1:], len(source)]))
    if len(chunks) == 1:
        return (_parse_copybook_source(chunks[0], work, original_storage),)
    return tuple(_parse_copybook_source(chunk, work, original_storage) for chunk in chunks)


def _parse_copybook_source(source: str, work: Work, original_storage: bool) -> Schema:
    if re.search(r"\b(?:COPY|REPLACE)\b", source):
        raise ValueError("COPY/REPLACE directives must be expanded before conversion.")
    declarations = []
    for candidate in _DECLARATION_RE.finditer(source):
        work.check()
        end = candidate.start()
        while end and source[end - 1].isspace():
            end -= 1
        start = end
        while start and (source[start - 1].isalnum() or source[start - 1] == "-"):
            start -= 1
        if source[start:end] in {"PIC", "PICTURE", "IS", "OCCURS", "TO", "VALUE", "VALUES"}:
            continue
        declarations.append(candidate)
    if not declarations or source[:declarations[0].start()].strip(" \t\r\n."):
        raise ValueError("Expected COBOL data declarations starting with a level number.")
    roots: list[_Item] = []
    stack: list[_Item] = []
    notes: list[str] = []
    for index, match in enumerate(declarations):
        work.update("Parsing copybook fields", index, len(declarations))
        level, name = int(match[1]), match[2]
        body = source[match.end():declarations[index + 1].start() if index + 1 < len(declarations) else len(source)]
        if level == 88:
            continue
        if not 1 <= level <= 49:
            raise ValueError(f"{name}: level {level} is not supported.")
        for clause in ("DEPENDING", "SYNC", "SYNCHRONIZED", "SIGN", "RENAMES",
                       "ANY", "BASED", "EXTERNAL", "NATIONAL"):
            if re.search(rf"(?<![\w-]){clause}(?![\w-])", body):
                raise ValueError(f"{name}: {clause} is not supported; no schema was generated.")
        body = body.strip().rstrip(".").strip()
        value_clause = re.search(r"\bVALUES?\b", body)
        if value_clause:
            tail = body[value_clause.end():]
            following_clause = re.search(
                r"\b(?:PIC(?:TURE)?|USAGE|OCCURS|REDEFINES|JUSTIFIED|BLANK)\b", tail)
            if following_clause and not re.match(r"\bPIC(?:TURE)?\b", tail[following_clause.start():]):
                raise ValueError(f"{name}: only a single trailing VALUE literal/figurative constant is supported.")
            value = tail[:following_clause.start() if following_clause else len(tail)].strip()
            if not re.fullmatch(r"(?:(?:IS|ARE)\s+)?(?:ALL\s+)?"
                                r"(?:''|[+-]?\d+(?:\.\d+)?|ZERO(?:S|ES)?|SPACES?|"
                                r"HIGH-VALUES?|LOW-VALUES?|QUOTES?|NULLS?)", value):
                raise ValueError(f"{name}: only a single trailing VALUE literal/figurative constant is supported.")
            body = body[:value_clause.start()] + (tail[following_clause.start():]
                                                   if following_clause else "")
            body = body.strip()
        item = _Item(level, name)
        overlay = re.search(r"\bREDEFINES\s+([A-Z][A-Z0-9-]*)", body)
        if overlay:
            if not original_storage:
                raise ValueError(f"{name}: REDEFINES requires the explicit original-storage copybook policy.")
            item.redefines = overlay[1]
            body = body[:overlay.start()] + body[overlay.end():]
        pic = re.search(r"\bPIC(?:TURE)?\s+(?:IS\s+)?([^\s.]+)", body)
        if pic:
            item.pic = pic[1]
            body = body[:pic.start()] + body[pic.end():]
        usage = re.search(r"\b(?:USAGE\s+(?:IS\s+)?)?"
                          r"(COMPUTATIONAL(?:-[1-5])?|COMP(?:-[1-5])?|BINARY|PACKED-DECIMAL|DISPLAY)\b", body)
        if usage:
            item.usage = usage[1].replace("COMPUTATIONAL", "COMP")
            body = body[:usage.start()] + body[usage.end():]
        occurs = re.search(r"\bOCCURS\s+(\d+)(?:\s+TIMES)?\b", body)
        if occurs:
            item.occurs = _integer(int(occurs[1]), f"{name}.OCCURS", 1, MAX_FIELDS)
            item.repeated = True
            body = body[:occurs.start()] + body[occurs.end():]
        body = re.sub(r"\b(?:JUSTIFIED|JUST)\s*(?:RIGHT)?\b|\bBLANK\s+WHEN\s+ZERO\b", "", body)
        if body.strip(" \t\r\n."):
            raise ValueError(f"{name}: unsupported clause '{body.strip()}'.")
        while stack and stack[-1].level >= level:
            stack.pop()
        if not stack and not roots and level != 1 and original_storage:
            wrapper = _Item(1, "COPYBOOK-RECORD")
            roots.append(wrapper)
            stack.append(wrapper)
            notes.append("Copybook fragment: supplied a synthetic 01 COPYBOOK-RECORD; commented declarations remain comments.")
        if not stack and level != 1:
            raise ValueError(f"{name}: expected an 01-level record.")
        (stack[-1].children if stack else roots).append(item)
        stack.append(item)
    if len(roots) != 1:
        raise ValueError("Exactly one 01-level record is required (split multi-record copybooks).")
    fields: list[SchemaField] = []

    def emit(item: _Item, offset: int, prefix: str, inherited_usage: str = "DISPLAY") -> int:
        usage = item.usage or inherited_usage
        pos = offset
        for occurrence in range(item.occurs):
            work.check()
            name = item.name + (f"({occurrence + 1})" if item.repeated else "")
            qualified = f"{prefix}.{name}" if prefix else name
            if item.pic:
                if item.children:
                    raise ValueError(f"{qualified}: an elementary item cannot contain children.")
                size, kind, decimals, signed = _pic_shape(item.pic, usage)
                if len(fields) >= MAX_FIELDS or pos + size > MAX_RECORD_LENGTH:
                    raise ValueError("Expanded copybook exceeds field/record length limits.")
                fields.append(SchemaField(qualified, pos + 1, size, item.pic, kind, decimals, signed))
                pos += size
            else:
                if not item.children:
                    raise ValueError(f"{qualified}: no PIC or child fields.")
                for child_index, child in enumerate(item.children, 1):
                    if child.redefines:
                        continue
                    if child.name == "FILLER":
                        child = _Item(child.level, f"FILLER-{child_index}", child.pic, child.usage,
                                      child.occurs, child.repeated, child.children)
                    pos += emit(child, pos, qualified, usage)
        return pos - offset

    def validate_overlays(item: _Item, inherited_usage: str = "DISPLAY") -> None:
        usage = item.usage or inherited_usage
        previous: dict[str, _Item] = {}
        for child in item.children:
            work.check()
            validate_overlays(child, usage)
            if child.redefines:
                target = previous.get(child.redefines)
                if target is None:
                    raise ValueError(f"{child.name}: REDEFINES target '{child.redefines}' is not an earlier sibling.")
                begin = len(fields)
                target_size = emit(target, 0, "VALIDATE", usage)
                del fields[begin:]
                overlay_size = emit(child, 0, "VALIDATE", usage)
                del fields[begin:]
                if overlay_size > target_size:
                    raise ValueError(f"{child.name}: overlay length {overlay_size} exceeds original storage {target_size}.")
                notes.append(f"{child.name} REDEFINES {child.redefines}: validated {overlay_size}-byte overlay; "
                             "only original fields are compared.")
            previous[child.name] = child

    validate_overlays(roots[0])
    length = emit(roots[0], 0, "")
    return parse_schema(Schema(length, tuple(fields), notes=tuple(notes)).to_json())


def save_schema(path: str, schema: Schema) -> None:
    """Never overwrite a different reusable schema, including an output created by another run."""
    try:
        with open(path, "x", encoding="utf-8") as target:
            target.write(schema.to_json())
    except FileExistsError:
        if load_schema(path) != schema:
            raise ValueError(f"Existing JSON differs: {Path(path).name}. Choose another output folder.")
