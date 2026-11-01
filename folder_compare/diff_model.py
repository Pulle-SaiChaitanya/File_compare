"""Data model shared by the diff engine and the report writer."""
from __future__ import annotations

from dataclasses import dataclass, field

from .layout import Field

CONTEXT = 3  # unchanged rows shown around each difference


@dataclass
class FieldValue:
    text_a: str
    text_b: str
    changed: bool


@dataclass
class DiffRow:
    kind: str  # added | removed | changed | context | skip
    no_a: int | None = None
    no_b: int | None = None
    text_a: str = ""
    text_b: str = ""
    marks_a: list[tuple[int, int]] = field(default_factory=list)  # [start, end) highlight ranges
    marks_b: list[tuple[int, int]] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)  # explanation lines (fields, hex, key)
    field_values: list[FieldValue] = field(default_factory=list)


@dataclass
class DiffResult:
    rows: list[DiffRow] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    changed: int = 0
    encoding_a: str = ""
    encoding_b: str = ""
    unit: str = "Line"  # Line | Record | Offset
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    fields: list[Field] = field(default_factory=list)
    included_fields: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.added + self.removed + self.changed
