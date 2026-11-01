"""Preflight matching and per-file schema orchestration; reuses the existing engine."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .core import FileResult, SKIPPED, compare_file_pair, scan_folder
from .options import CompareOptions, matches_patterns
from .schema import Schema, load_schema, parse_copybooks, read_definition, save_schema
from .work import ComparisonCancelled, Work
from .container_records import VariableRecordRule, validate_headered_file

READY = "Ready"
VALIDATION_STATUSES = (READY, "Missing A", "Missing B", "Missing JSON", "Missing Copybook",
                       "Invalid Schema", "Duplicate Match")
VARIABLE_LAYOUTS = {
    "fund-array": (2000, 225, 99),
    "loan-info": (100, 53, 500),
    "relationship-info": (200, 400, 50),
}


class RecordLayoutSelectionRequired(Exception):
    """Multiple validated schemas need an explicit selection before comparison."""


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _key_components(schema: Schema, names_text: str):
    names = [name.strip() for name in re.split(r"[,;\r\n]+", names_text) if name.strip()]
    components = []
    for name in names:
        if re.fullmatch(r"\d+(?:\s*-\s*\d+)?", name):
            range_match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", name)
            start = int(range_match.group(1))
            end = int(range_match.group(2) or start)
            if start < 1 or end < start or end > schema.record_length:
                raise ValueError(f"Copybook key range '{name}' is outside record length "
                                 f"{schema.record_length}.")
            components.append((start - 1, end - start + 1, f"columns {start}-{end}"))
            continue
        matches = [field for field in schema.fields
                   if field.name.casefold() == name.casefold()
                   or field.name.rsplit(".", 1)[-1].casefold() == name.casefold()]
        if len(matches) != 1:
            raise ValueError(f"Copybook key field '{name}' "
                             f"{'is ambiguous' if matches else 'was not found'} in this layout.")
        field = matches[0]
        components.append((field.start - 1, field.length,
                           f"{field.name} (cols {field.start}-{field.start + field.length - 1})"))
    return components


def _resolve_key_ranges(schema: Schema, opts: CompareOptions) -> tuple[tuple[int, int], ...]:
    if not opts.key_fields.strip():
        return ()
    return tuple((start, length) for start, length, _ in _key_components(schema, opts.key_fields))


@dataclass
class ScanItem:
    basename: str
    status: str
    paths_a: list[str] = field(default_factory=list)
    paths_b: list[str] = field(default_factory=list)
    definitions: list[str] = field(default_factory=list)
    note: str = ""
    schema: Schema | None = None
    normal: bool = False
    json_path: str = ""
    schemas: tuple[Schema, ...] = ()

    @property
    def paths_note(self) -> str:
        return (f"A: {', '.join(self.paths_a) or '(missing)'} | "
                f"B: {', '.join(self.paths_b) or '(missing)'} | "
                f"Definition: {', '.join(self.definitions) or '(none)'}")


def _inside(path: str, folder: str) -> bool:
    path = os.path.normcase(os.path.realpath(path))
    folder = os.path.normcase(os.path.realpath(folder))
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:
        return False


def _index(root: str, label: str, work: Work, errors: list[ScanItem],
           patterns: str = "", extension: str = "") -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    def scan_error(exc: OSError) -> None:
        errors.append(ScanItem(str(exc.filename or root), "Invalid Schema",
                               note=f"{label}: unable to scan: {exc}"))

    if not os.path.isdir(root):
        errors.append(ScanItem(label, "Invalid Schema", note=f"{label}: folder does not exist: {root}"))
        return out
    count = 0
    for directory, _dirs, names in os.walk(root, onerror=scan_error):
        work.update(f"Scanning {label}", count)
        for name in sorted(names, key=str.casefold):
            work.check()
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if extension and Path(name).suffix.casefold() != extension:
                continue
            if patterns and not matches_patterns(rel, patterns):
                continue
            out.setdefault(Path(name).stem.casefold(), []).append(path)
            count += 1
    return out


def scan_inputs(folder_a: str, folder_b: str, opts: CompareOptions, work: Work | None = None) -> list[ScanItem]:
    """Validate all data basenames. Read-only: JSON generation occurs only during Compare."""
    work = work or Work()
    errors: list[ScanItem] = []
    a = _index(folder_a, "Folder A", work, errors, opts.patterns)
    b = _index(folder_b, "Folder B", work, errors, opts.patterns)
    keys = sorted(set(a) | set(b))
    needs_schema = any(not opts.normal_text or any(Path(path).suffix.casefold() != ".txt"
                                                  for path in a.get(key, []) + b.get(key, []))
                       for key in keys)
    generate = opts.schema_source == "generate"
    source = opts.copybook_folder if generate else opts.json_folder
    definitions = (_index(source, "Copybooks" if generate else "JSON", work, errors,
                          extension=".cpy" if generate else ".json") if needs_schema else {})
    outputs = {}
    output_problem = ""
    if generate and needs_schema:
        if not os.path.isdir(opts.generated_json_folder):
            output_problem = "Select an existing, separate generated JSON output folder."
        elif any(_inside(opts.generated_json_folder, root) for root in (folder_a, folder_b, source)):
            output_problem = "Generated JSON output must be outside Folder A, Folder B and the copybook folder."
        else:
            outputs = _index(opts.generated_json_folder, "Generated JSON", work, errors, extension=".json")
    items = list(errors)
    for number, key in enumerate(keys, 1):
        work.update("Validating file schemas", number, len(keys))
        pa, pb, defs = a.get(key, []), b.get(key, []), definitions.get(key, [])
        normal = opts.normal_text and bool(pa or pb) and all(
            Path(path).suffix.casefold() == ".txt" for path in pa + pb)
        item = ScanItem(key, READY, pa, pb, [] if normal else defs, normal=normal)
        problems = []
        if len(pa) > 1 or len(pb) > 1 or (not normal and (len(defs) > 1 or len(outputs.get(key, [])) > 1)):
            problems.append("Duplicate Match")
        if not pa:
            problems.append("Missing A")
        if not pb:
            problems.append("Missing B")
        if not normal and not defs:
            problems.append("Missing Copybook" if generate else "Missing JSON")
        if problems:
            item.status, item.note = problems[0], "; ".join(problems) + ". Skipped; no arbitrary match selected."
            items.append(item)
            continue
        if normal:
            item.note = "TXT pair: Normal text comparison, without a copybook."
        else:
            try:
                if output_problem:
                    raise ValueError(output_problem)
                schemas = (parse_copybooks(read_definition(defs[0]), work, opts.copybook_original_storage)
                           if generate else (load_schema(defs[0]),))
                compatible = []
                schema_errors = []
                for schema in schemas:
                    try:
                        if opts.key_fields.strip():
                            _resolve_key_ranges(schema, opts)
                        elif opts.match == "key" and opts.key_start + opts.key_length - 1 > schema.record_length:
                            raise ValueError("Key columns extend past the schema record length.")
                        data_note = _validate_data_pair(pa[0], pb[0], schema, opts, work)
                        compatible.append((schema, data_note))
                    except ValueError as exc:
                        schema_errors.append(str(exc))
                if not compatible:
                    raise ValueError("; ".join(schema_errors))
                item.schemas = tuple(schema for schema, _ in compatible)
                item.schema = item.schemas[0] if len(item.schemas) == 1 else None
                if generate:
                    item.json_path = (outputs[key][0] if outputs.get(key) else
                                      str(Path(opts.generated_json_folder) / (Path(defs[0]).stem + ".json")))
                    if os.path.exists(item.json_path) and load_schema(item.json_path) not in item.schemas:
                        raise ValueError("Existing generated JSON differs; choose another output folder.")
                else:
                    item.json_path = defs[0]
                if len(compatible) == 1:
                    schema, data_note = compatible[0]
                    variable = (_variable_record_rule(schema)
                                if opts.schema_data_format == "headered" else None)
                    record_desc = (f"Variable records (maximum {schema.record_length} bytes)"
                                   if variable else f"Fixed {schema.record_length}-byte records")
                    item.note = f"{record_desc}; {len(schema.fields)} fields."
                    item.note += data_note + " " + " ".join(schema.notes)
                else:
                    layouts = ", ".join(
                        f"{schema.fields[0].name.split('.', 1)[0]} "
                        f"({schema.record_length} bytes, {len(schema.fields)} fields)"
                        for schema, _ in compatible)
                    item.note = f"Choose one of {len(compatible)} matching record layouts: {layouts}."
                if generate:
                    item.note += f" JSON will be generated/reused at: {item.json_path}"
            except (OSError, ValueError, UnicodeError, RecursionError) as exc:
                item.status, item.note = "Invalid Schema", str(exc)
                item.schema = None
                item.schemas = ()
        items.append(item)
    return items


def _variable_record_rule(schema: Schema) -> VariableRecordRule | None:
    """Recognize only the three supplied descriptor-framed variable layouts."""
    present = [marker for marker in VARIABLE_LAYOUTS
               if any(marker in field.name.casefold() for field in schema.fields)]
    matched = [(marker, VARIABLE_LAYOUTS[marker]) for marker in present
               if schema.record_length == (VARIABLE_LAYOUTS[marker][0] +
                                           VARIABLE_LAYOUTS[marker][1] * VARIABLE_LAYOUTS[marker][2])]
    if not matched:
        if present:
            raise ValueError("Supported variable-record copybook maximum length does not match its layout.")
        return None
    if len(matched) != 1:
        raise ValueError("Copybook matches multiple supported variable-record layouts.")
    marker, (base_length, stride, max_occurs) = matched[0]
    key_fields = [field for field in schema.fields if field.name.casefold().endswith(".key-occurs")]
    if len(key_fields) != 1 or key_fields[0].type != "zoned" or key_fields[0].length != 3:
        raise ValueError(f"{marker}: expected one 3-byte DISPLAY KEY-OCCURS field.")
    occurrence_fields = [field for field in schema.fields
                         if re.search(r"\(1\)(?:\.|$)", field.name)
                         and marker in field.name.casefold()]
    if not occurrence_fields:
        raise ValueError(f"{marker}: no first occurrence was found in the copybook.")
    first = occurrence_fields[0]
    second_name = re.sub(r"\(1\)", "(2)", first.name, count=1)
    second = next((field for field in schema.fields if field.name == second_name), None)
    if second is None or first.start - 1 != base_length or second.start - first.start != stride:
        raise ValueError(f"{marker}: copybook occurrence offsets do not match the supported layout.")
    occurrence_pattern = re.escape(first.name).replace(r"\(1\)", r"\((\d+)\)", 1)
    occurrences = [int(match.group(1)) for field in schema.fields
                   if (match := re.fullmatch(occurrence_pattern, field.name))]
    if max(occurrences, default=0) != max_occurs:
        raise ValueError(f"{marker}: copybook occurrence count does not match the supported layout.")
    if schema.record_length != base_length + max_occurs * stride:
        raise ValueError(f"{marker}: copybook maximum length does not match its occurrence layout.")
    return VariableRecordRule(key_fields[0].start - 1, key_fields[0].length,
                              base_length, stride, max_occurs)


def _validate_data_pair(a: str, b: str, schema: Schema, opts: CompareOptions, work: Work) -> str:
    counts = []
    variable = _variable_record_rule(schema) if opts.schema_data_format == "headered" else None
    for label, path in (("A", a), ("B", b)):
        if opts.schema_data_format == "headered":
            counts.append(f"{label}: {validate_headered_file(path, schema.record_length, work, variable):,} "
                          "payload records")
        elif os.path.getsize(path) % schema.record_length:
            raise ValueError(f"{label}: data size is not a multiple of record_length {schema.record_length}. "
                             "If this is a headered export, select the explicit headered data format.")
    suffix = " Variable descriptor lengths validated against KEY-OCCURS." if variable else ""
    return (" " + "; ".join(counts) + ". File/record headers are excluded." + suffix) if counts else ""


def scan_normal_inputs(folder_a: str, folder_b: str, opts: CompareOptions, work: Work) -> list[ScanItem]:
    a = scan_folder(folder_a, work, "Scanning Folder A")
    b = scan_folder(folder_b, work, "Scanning Folder B")
    items = []
    for path in sorted(set(a) | set(b), key=str.casefold):
        work.check()
        if not matches_patterns(path, opts.patterns):
            continue
        status = "Missing A" if path not in a else "Missing B" if path not in b else READY
        items.append(ScanItem(path, status, [a[path]] if path in a else [],
                              [b[path]] if path in b else [], normal=True,
                              note="Normal relative-path comparison; no schema required."))
    return items


def compare_scanned(
    items: list[ScanItem], opts: CompareOptions, work: Work | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    select_columns: Callable[[ScanItem, Schema], tuple[str, ...] | None] | None = None,
    select_schema: Callable[[ScanItem, tuple[Schema, ...]], Schema | None] | None = None,
) -> list[FileResult]:
    """Skip invalid matches explicitly, and give the unchanged engine a layout per Ready file."""
    work = work or Work()
    results = []
    for number, item in enumerate(items, 1):
        try:
            work.check()
            if progress:
                progress(number - 1, len(items), item.basename)
            if item.status != READY:
                results.append(FileResult(item.basename, SKIPPED, kind="error",
                                          note=f"{item.status}: {item.note} | {item.paths_note}"))
            else:
                try:
                    pair_opts = replace(opts)
                    if item.normal:
                        pair_opts.fmt, pair_opts.lrecl, pair_opts.layout = "text", 0, None
                        pair_opts.schema_data_format = "fixed"
                    else:
                        schema = item.schema
                        if schema is None and len(item.schemas) > 1:
                            if select_schema is None:
                                raise RecordLayoutSelectionRequired()
                            schema = select_schema(item, item.schemas)
                            if schema is None:
                                raise ComparisonCancelled()
                        if schema is None:
                            raise ValueError("Select one validated record layout before comparison.")
                        if item.schemas and schema not in item.schemas:
                            raise ValueError("Selected record layout is outside the validated schemas.")
                        pair_opts.variable_record_rule = (
                            _variable_record_rule(schema) if opts.schema_data_format == "headered" else None
                        )
                        pair_opts.key_ranges = _resolve_key_ranges(schema, opts)
                        selected_fields = (select_columns(item, schema)
                                           if select_columns is not None else
                                           tuple(field.name for field in schema.fields))
                        if selected_fields is None:
                            raise ComparisonCancelled()
                        selected = set(selected_fields)
                        known = {field.name for field in schema.fields}
                        if not selected <= known:
                            raise ValueError("Column selection contains fields outside the validated schema.")
                        pair_opts.selected_fields = tuple(
                            field.name for field in schema.fields if field.name in selected)
                        included_ranges = [(field.start - 1, field.start - 1 + field.length)
                                           for field in schema.fields if field.name in selected]
                        included_ranges.sort()
                        excluded = []
                        position = 0
                        for start, end in included_ranges:
                            if position < start:
                                excluded.append((position, start))
                            position = max(position, end)
                        if position < schema.record_length:
                            excluded.append((position, schema.record_length))
                        pair_opts.ranges = _merge_ranges([
                            *pair_opts.ranges, *((start + 1, end) for start, end in excluded)
                        ])
                        key_names = []
                        key_end = opts.key_start - 1 + opts.key_length
                        if opts.match == "key" and opts.key_fields.strip():
                            key_names = [description for _, _, description in
                                         _key_components(schema, opts.key_fields)]
                        elif opts.match == "key":
                            key_start, key_end = opts.key_start - 1, opts.key_start - 1 + opts.key_length
                            key_names = [
                                f"{field.name} (cols {field.start}-{field.start + field.length - 1})"
                                for field in schema.fields
                                if field.start - 1 < key_end and field.start - 1 + field.length > key_start
                            ]
                        item_config = {
                            "file_a": os.path.basename(item.paths_a[0]),
                            "file_b": os.path.basename(item.paths_b[0]),
                            "comparison_type": "Copybook/Column-Based",
                            "record_layout": schema.fields[0].name.split(".", 1)[0],
                            "key_columns": (", ".join(key_names) if key_names else
                                            f"columns {opts.key_start}-{key_end} (not mapped)"
                                            if opts.match == "key" else "Not used"),
                            "included_columns": [name for name in pair_opts.selected_fields],
                            "excluded_columns": [field.name for field in schema.fields
                                                 if field.name not in selected],
                        }
                        item.note += f" Compared columns: {len(pair_opts.selected_fields)} of {len(schema.fields)}."
                        if opts.schema_source == "generate":
                            work.update("Generating reusable JSON")
                            save_schema(item.json_path, schema)
                            schema = load_schema(item.json_path)
                            variable = (_variable_record_rule(schema)
                                        if opts.schema_data_format == "headered" else None)
                            record_desc = (f"Variable records (maximum {schema.record_length} bytes)"
                                           if variable else f"Fixed {schema.record_length}-byte records")
                            item.note = (f"Using generated/reused JSON: {item.json_path} | "
                                         f"{record_desc}; {len(schema.fields)} fields.")
                        # Recheck sizes immediately before comparison, including identical/empty pairs.
                        _validate_data_pair(item.paths_a[0], item.paths_b[0], schema, opts, work)
                        pair_opts.fmt, pair_opts.lrecl = "fixed", schema.record_length
                        pair_opts.layout = schema.to_layout()
                        item.note += " " + " ".join(schema.notes)
                    result = compare_file_pair(item.basename, item.paths_a[0], item.paths_b[0], pair_opts, work)
                    result.note = f"{result.note} | {item.paths_note}".strip(" |")
                    result.comparison_config = item_config if not item.normal else {}
                    if not item.normal:
                        result.format_desc = (f"Schema: {item.json_path} | " +
                                              (result.format_desc or
                                               f"Copybook maximum {pair_opts.lrecl}-byte records"))
                    if result.kind == "error":
                        result.status = SKIPPED
                    results.append(result)
                except RecordLayoutSelectionRequired:
                    results.append(FileResult(
                        item.basename, SKIPPED, kind="error",
                        note=f"Multiple record layouts require selection before comparison. | {item.paths_note}"))
                except (OSError, ValueError, UnicodeError) as exc:
                    item.status, item.note = "Invalid Schema", str(exc)
                    results.append(FileResult(item.basename, SKIPPED, kind="error",
                                              note=f"Invalid Schema: {exc} | {item.paths_note}"))
            work.check()
            if progress:
                progress(number, len(items), item.basename)
        except ComparisonCancelled:
            break
    return results
