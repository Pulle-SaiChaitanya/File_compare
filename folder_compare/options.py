"""Comparison settings shared by the GUI and the engine."""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .container_records import VariableRecordRule
    from .layout import Layout

FORMATS = (
    ("Auto-detect", "auto"),
    ("Text lines (CR/LF delimited)", "text"),
    ("Fixed-length records (LRECL)", "fixed"),
    ("Variable records (RDW)", "rdw"),
    ("Raw bytes (hex view)", "binary"),
)
ENCODINGS = (
    ("Auto-detect", "auto"),
    ("EBCDIC US/Canada (cp037)", "cp037"),
    ("EBCDIC International (cp500)", "cp500"),
    ("EBCDIC Open Systems (cp1047)", "cp1047"),
    ("EBCDIC Euro (cp1140)", "cp1140"),
    ("EBCDIC Germany (cp273)", "cp273"),
    ("ASCII / Windows-1252", "cp1252"),
    ("ISO Latin-1", "latin-1"),
    ("UTF-8", "utf-8"),
)
MATCH_MODES = (
    ("By sequence (finds inserted/deleted records)", "sequence"),
    ("By record position (1st vs 1st, 2nd vs 2nd ...)", "position"),
    ("By key columns (records matched on a key)", "key"),
)
EBCDIC_CODECS = ("cp037", "cp500", "cp1047", "cp1140", "cp273")


def parse_ranges(text: str) -> list[tuple[int, int]]:
    """'1-8, 20-30, 45' -> [(1, 8), (20, 30), (45, 45)] (1-based, inclusive)."""
    out: list[tuple[int, int]] = []
    for part in re.split(r"[,;\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            raise ValueError(f"Invalid column range '{part}'. Use e.g. 1-8, 20-30.")
        start = int(m.group(1))
        end = int(m.group(2) or start)
        if start < 1 or end < start:
            raise ValueError(f"Invalid column range '{part}' (columns start at 1).")
        out.append((start, end))
    return out


def matches_patterns(rel_path: str, patterns: str) -> bool:
    pats = [p.strip().lower() for p in re.split(r"[;,\s]+", patterns) if p.strip()]
    if not pats:
        return True
    rel = rel_path.lower()
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, p) or fnmatch.fnmatch(rel, p) for p in pats)


@dataclass
class CompareOptions:
    patterns: str = ""
    fmt: str = "auto"
    encoding: str = "auto"
    lrecl: int = 0
    bdw: bool = False
    match: str = "sequence"
    key_start: int = 0
    key_length: int = 0
    key_fields: str = ""
    ignore_ranges: str = ""
    trim_trailing: bool = False
    layout_text: str = ""
    max_diff_rows: int = 2000
    max_diff_mb: int = 200
    comparison_type: str = "normal"
    schema_source: str = "existing"
    json_folder: str = ""
    copybook_folder: str = ""
    generated_json_folder: str = ""
    normal_text: bool = True
    copybook_original_storage: bool = False
    schema_data_format: str = "fixed"

    # filled by prepare(); not saved in profiles
    ranges: list = field(default_factory=list, repr=False)
    layout: Layout | None = field(default=None, repr=False)
    selected_fields: tuple[str, ...] | None = field(default=None, repr=False)
    variable_record_rule: VariableRecordRule | None = field(default=None, repr=False)
    key_ranges: tuple[tuple[int, int], ...] = field(default=(), repr=False)

    def prepare(self) -> "CompareOptions":
        """Validate settings and parse ranges/layout. Raises ValueError."""
        from .layout import parse_layout

        if self.comparison_type not in ("normal", "copybook"):
            raise ValueError("Choose Normal or Copybook/Column-Based comparison.")
        if self.schema_source not in ("existing", "generate"):
            raise ValueError("Choose Use Existing JSON or Generate JSON from Copybooks.")
        if self.schema_data_format not in ("fixed", "headered"):
            raise ValueError("Choose plain fixed or 128-byte header + 4-byte descriptor data.")
        if self.fmt not in {v for _, v in FORMATS}:
            raise ValueError(f"Unknown format '{self.fmt}'.")
        if self.match not in {v for _, v in MATCH_MODES}:
            raise ValueError(f"Unknown match mode '{self.match}'.")
        if not 0 <= self.lrecl <= 1_048_576:
            raise ValueError("Record length must be between 1 and 1,048,576.")
        if self.key_fields.strip() and self.match != "key":
            raise ValueError("Copybook key fields can only be used with By key columns matching.")
        if self.key_fields.strip() and self.comparison_type != "copybook":
            raise ValueError("Named key fields require Copybook/Column-Based comparison.")
        if self.match == "key" and not self.key_fields.strip() and not self.key_ranges and (
                self.key_start < 1 or self.key_length < 1):
            raise ValueError("Key matching needs a key start column (>=1) and key length (>=1).")
        if self.key_fields.strip():
            names = [name.strip() for name in re.split(r"[,;\r\n]+", self.key_fields) if name.strip()]
            if not names:
                raise ValueError("Enter one or more copybook key field names.")
            if len({name.casefold() for name in names}) != len(names):
                raise ValueError("A copybook key field may be selected only once.")
        if self.max_diff_rows < 1 or self.max_diff_mb < 1:
            raise ValueError("Limits must be at least 1.")
        self.ranges = parse_ranges(self.ignore_ranges)
        self.layout = parse_layout(self.layout_text) if self.comparison_type == "normal" else None
        return self

    @property
    def has_ignore_rules(self) -> bool:
        return bool(self.ranges) or self.trim_trailing

    def describe(self) -> str:
        label = lambda table, v: next((k for k, x in table if x == v), v)
        parts = [f"Format: {label(FORMATS, self.fmt)}", f"Encoding: {label(ENCODINGS, self.encoding)}"]
        if self.comparison_type == "copybook":
            source = (f"Copybooks: {self.copybook_folder} | Generated JSON: {self.generated_json_folder}"
                      if self.schema_source == "generate" else f"JSON: {self.json_folder}")
            parts.insert(0, f"Comparison: Copybook/Column-Based | {source} | "
                         f"TXT normally: {self.normal_text} | Schema data: {self.schema_data_format} | "
                         f"Original storage/fragments: {self.copybook_original_storage} | "
                         "Per-file schema overrides format/LRECL/layout")
        if self.lrecl:
            parts.append(f"LRECL: {self.lrecl}")
        if self.bdw:
            parts.append("BDW blocks")
        parts.append("Match: " + label(MATCH_MODES, self.match).split(" (")[0])
        if self.match == "key":
            parts.append("Key fields: " + self.key_fields if self.key_fields.strip()
                         else f"Key: cols {self.key_start}-{self.key_start + self.key_length - 1}")
        if self.ignore_ranges.strip():
            parts.append(f"Ignored columns: {self.ignore_ranges.strip()}")
        if self.trim_trailing:
            parts.append("Trailing spaces ignored")
        if self.layout is not None:
            parts.append(f"Field layout: {len(self.layout.fields)} fields")
        if self.patterns.strip():
            parts.append(f"File filter: {self.patterns.strip()}")
        return " | ".join(parts)

    def to_json(self) -> str:
        skip = {"ranges", "layout", "selected_fields", "variable_record_rule", "key_ranges"}
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in skip}
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "CompareOptions":
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("Settings file must contain a JSON object.")
        opts = cls()
        for f in fields(cls):
            if f.name in ("ranges", "layout", "selected_fields", "variable_record_rule", "key_ranges") \
                    or f.name not in raw:
                continue
            current = getattr(opts, f.name)
            value = raw[f.name]
            try:
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                else:
                    value = str(value)
            except (TypeError, ValueError):
                raise ValueError(f"Invalid value for '{f.name}' in settings file.") from None
            setattr(opts, f.name, value)
        return opts
