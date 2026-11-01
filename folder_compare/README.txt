Folder Compare
==============

Run from the project directory:
    python -m folder_compare
Existing executables are older snapshots and do NOT include the latest
sample-format fixes. Use the Python command above while troubleshooting.

Select Folder A and Folder B, choose settings, then click Compare.
For mainframe DAT files, choose the actual record format, encoding and
LRECL when known; the .dat extension alone does not specify these.
Auto-detection is a best-effort guess, not a substitute for file metadata.

While running:
- The animated bar indicates an active operation, not a percentage.
- The status shows completed files, current stage, elapsed time and
  byte/record counters where available. Counters reset for each stage.
- If worker updates stop for 3 seconds, the time since its last update
  is shown. Elapsed time and animation alone do not prove worker progress.
- Cancel is checked during scanning, byte reads, detection, splitting,
  alignment and comparison. It cannot interrupt an operating-system read
  already in progress (for example a stalled network share) or a single
  built-in allocation/sort. Wait for the cancellation confirmation.
- Cancelled results include completed files only; the unfinished file is
  excluded. Reports saved from that run are labelled PARTIAL.

Sequence matching finds inserted/deleted records. Highly repetitive data
can make alignment expensive. Alignment has a 5-second cooperative deadline,
a 2,000,000-candidate work budget and a 2,000,000-middle-record size limit.
Checks occur inside the matching loop. If a limit is reached, the unmatched
region is compared by position and the report includes a warning. This is
not a 5-second limit on the entire file comparison.

Use "By record position" when records must remain in the same order and
position. Use "By key columns" for a known record key; duplicate keys are
paired in occurrence order, not searched for the closest match.
For Copybook/Column-Based comparisons, enter one or more comma-separated
copybook field names or 1-based byte ranges in "Copybook key fields / ranges"
to build a composite key. Components are concatenated in the entered order;
exact or unique unqualified field names are accepted. Existing Key start /
length matching remains available when this setting is blank. Duplicate
composite keys are counted in comparison notes.
The row limit limits report detail, not the number of records compared.
Long text lines use whole-line rather than character-level highlighting to
avoid expensive character alignment.

Exact byte comparison establishes equality before detailed comparison.
Ignore rules can intentionally classify byte-different records as identical.
The detail-size limit applies per file; record lists and matching indexes
need additional memory. Raising it significantly can exhaust memory.

Input files are opened read-only. Save HTML reports outside both input
folders. Report generation runs in a background worker; large reports can
still take time. Reports use the folder paths captured when Compare began,
even if the folder boxes are subsequently edited.

Standard-library regression tests (run from project directory):
    python -m unittest discover -s tests -p "test_*.py" -v

Comparison Type
---------------
Normal (default) retains the existing relative-path, text/record/binary
comparison logic and all its settings. No JSON or copybook is required.

Copybook/Column-Based adds per-file schemas:
1. Select Folder A / Folder B and Comparison Type = Copybook/Column-Based.
2. Select either:
   - Use Existing JSON: choose the folder containing record schema JSON files.
   - Generate JSON from Copybooks: choose a copybook folder and a separate,
     existing JSON output folder outside A, B and the copybook folder.
3. Click Validate / Scan. This is read-only, including in Generate mode.
   Review the Validation / Scan tab before comparing.
4. Click Compare. It always rescans using the captured settings, so a previous
   scan cannot accidentally authorize changed folders/settings. Ready copybook
   pairs generate/reuse JSON and then compare with that JSON layout.
5. Before each Ready copybook pair is compared, choose its comparison columns.
   The dialog starts with all fields selected; select/unselect controls and
   Cancel comparison are available. Each file gets a fresh selection.
6. Save Individual HTML Reports. Choose an output folder (default:
   `reports` beside Folder A). One HTML report is written per result. Duplicate
   basenames are disambiguated with their relative paths. Existing same-name
   reports are replaced. Each report includes its own counts and configuration.
   Validation-only scans remain a single validation report.

Column inclusion changes comparison, unlike HTML display checkboxes:
unselected copybook fields are normalized out before the existing record
matching/diff engine runs. They cannot create Modified results or changed
record counts. Missing/extra records and key matching still use the existing
matching rules. The per-report configuration lists included and excluded
columns. Normal mode and TXT bypass do not show the selection dialog.

HTML field-column view
----------------------
When a record layout is available from JSON, copybook or the existing layout
settings, differing records are also displayed under named field columns.
Each record pair has an A row and a B row. Changed cells are orange, added
cells green, and removed cells red. Existing ignore rules still apply.
Numeric fields use the existing typed decoder and include their raw hex.
Headers show payload column positions and storage type on hover.

Expand a file in the HTML, then open "Show / hide columns". Uncheck fields
you do not want to see. "Find column" searches the checkbox list; Show all
and Hide all affect only that report. Duplicate names remain separate columns.
These HTML checkboxes are DISPLAY ONLY: hidden fields still participate in
comparison and search. Comparison inclusion is chosen separately in the
pre-comparison dialog for each copybook file pair.
Selections last while the report page is open and reset when it is reloaded.

The table contains differing records only, respects the existing detail limit,
and shows 25 record pairs per page to keep wide layouts responsive. Scroll
horizontally for more fields; use Previous / Next for more retained records.
"Raw record differences and field details" retains the original view,
including unchanged context, byte highlights and changes outside named fields.
Normal text/binary reports without a record layout retain their existing view.
The field controls need JavaScript; all code is embedded for offline use.

Matching in Copybook mode
------------------------
Folders are scanned recursively. Match by the filename WITHOUT its final
extension, case-insensitively, regardless of relative directory:
    A\nested\ACCOUNT.dat
    B\other\account.bin
    Schemas\Account.json
or  Copybooks\ACCOUNT.cpy -> Output\ACCOUNT.json

The existing file filter applies to data files in A and B, not definitions.
Only .json definitions / .cpy copybooks are indexed in their selected folders.
Different files sharing a basename in any one participating folder, including
different subfolders/extensions, are Duplicate Match. No arbitrary first match
is used. A matching definition is needed for each non-TXT data basename.
Unused definitions without data files are not comparison inputs.

By default, pairs whose extensions are both .txt are routed through the
existing text engine and do not require schemas. Uncheck "Compare .txt pairs
normally" to require schemas for TXT too. Other schema-less files are skipped,
not silently treated as text. Use Normal mode for arbitrary schema-less files.

Validation statuses:
    Ready / Missing A / Missing B / Missing JSON / Missing Copybook /
    Invalid Schema / Duplicate Match
If multiple requirements are missing, the Details column lists all of them.
Duplicates take priority over missing matches. Invalid/missing pairs become
Skipped in Copybook comparison; they are not counted as Identical or Modified.
Normal mode keeps its existing Only A / Only B behavior.

Each schema supplies a maximum record length and field offsets; it overrides
the global Format, Record length and manually edited Field layout for that
pair. Encoding, sequence/position/key matching, ignore rules and detail limits
still apply. Select the correct EBCDIC/ASCII encoding in the existing settings.
Fixed data sizes must be multiples of the schema length. Empty data is allowed.
The explicitly supported TYPE30PV, TYPE35LN and TYPE50AD headered samples use
per-record lengths derived from KEY-OCCURS; their copybook lengths remain maxima.
This mode does not infer a record format from the copybook. Plain fixed data
does not remove headers/newlines. A separate explicit headered format is
described below. Use Normal mode's RDW/text settings for those formats.

Field comparison uses byte columns and the existing record alignment/diff
engine, with named decoded field explanations. All record bytes are compared,
including FILLER and gaps, unless the existing ignore rules say otherwise.
It is not a numeric-equivalence comparison or a COBOL data-value validator.

Generated JSON
--------------
Schema/copybook and data inputs are never rewritten. Generated schemas use
the copybook basename in the selected output folder. An identical existing
schema is reused without writing it again. A different/invalid existing JSON
is reported as Invalid Schema; it is never overwritten. Choose another output
folder to generate a replacement. Output failures are reported per file.
Reports must also be saved outside the selected schema/copybook/output folders.

JSON contract (schema_version 1)
------------------------------
Starts and lengths are BYTE columns, starts are 1-based. Example:

{
  "schema_version": 1,
  "record_format": "fixed",
  "record_length": 8,
  "fields": [
    {"name": "ACCOUNT.ID", "start": 1, "length": 2,
     "pic": "X(2)", "type": "char"},
    {"name": "ACCOUNT.AMOUNT", "start": 3, "length": 3,
     "pic": "S9(3)V99", "type": "packed", "decimals": 2, "signed": true},
    {"name": "ACCOUNT.COUNT", "start": 6, "length": 2,
     "pic": "S9(4)", "type": "binary", "decimals": 0, "signed": true},
    {"name": "ACCOUNT.FILLER-4", "start": 8, "length": 1,
     "pic": "X", "type": "char"}
  ]
}

Required: schema_version, record_length, nonempty fields; each field must
contain name, start, length, pic, type. record_format defaults to "fixed".
Types: char, zoned, packed, binary (signed), ubinary (unsigned), hex.
decimals/signed are inferred from PIC if omitted, and checked if supplied.
Hex columns use pic="" and an explicit length. Names must be unique;
qualify repeated names by group or occurrence. Overlaps, out-of-bounds fields,
unsupported types and inconsistent PIC/storage metadata are Invalid Schema.
Gaps are allowed; they remain byte-compared and reported as "(no field)".
Settings-profile JSON files are not record schemas.

Copybook parser support and limits
---------------------------------
The new strict parser is separate from the legacy optional layout editor.
It supports a single 01 record, nested groups, common free/fixed-format
source, inline/full-line comments, X/A/9/S/V pictures and repetitions,
DISPLAY, COMP/BINARY/COMP-4, COMP-3/PACKED-DECIMAL, fixed OCCURS (including
nested groups), FILLER, signed numeric and implied-decimal fields.
Fields get qualified names and expanded occurrence indexes. FILLER occupies
storage and receives unique names. Binary storage uses big-endian 2/4/8-byte
mainframe conventions; computational PICs are limited to 18 digits.

Not a full COBOL compiler. REDEFINES/overlays (unless the explicit original
storage policy below is enabled), general variable OCCURS/DEPENDING ON,
SYNC padding, separate/explicit SIGN clauses, COMP-1/2 floating point,
edited/scaled/national pictures, COPY/REPLACE, pointer/index items, level
66/77, continuation/debug source lines and unsupported storage clauses are
rejected, not guessed. COMP-5 uses the same 2/4/8-byte PIC digit thresholds
as COMP. Multiple active 01 layouts are parsed separately; when more than
one matches the data files, select the intended layout before comparison.
VALUE supports a single literal or figurative constant before or after PIC;
level-88 conditions consume no storage, including VALUE THRU ranges. Expand
external COPY directives first.
National codepages/compiler options/record headers are not inferred.
Choose or provide an explicit supported JSON layout when conversion is not
possible, and verify offsets against the team's actual compiler listing.
Numeric display decoding retains the existing engine's mainframe zoned
decimal conventions; unusual ASCII overpunch variants are not inferred.

Schema version 1 allows up to 10,000 expanded fields, 1 MiB per record and
4 MiB per definition file. Storage rules, declaration parsing and layout
conversion are isolated for future extensions; unsupported features can be
added without changing the existing comparison engine.

Troubleshooting supplied tagged copybooks and headered exports
--------------------------------------------------------------
The supplied Source and Target files have a validated 128-byte file header,
then 4-byte descriptors followed by record payloads. These are NOT plain fixed
files or standard mainframe RDW files. TYPE20PL records are 3,000 bytes;
TYPE30PV, TYPE35LN and TYPE50AD are descriptor-framed variable records. Their
lengths are validated as 2,000 + KEY-OCCURS * 225, 100 + KEY-OCCURS * 53,
and 200 + KEY-OCCURS * 400 bytes, respectively. Their copybook lengths (24,275,
26,600 and 20,200 bytes) remain the maximum layouts. Records are zero-padded
to a 4-byte boundary between descriptors. The samples use ASCII-family display
bytes, not EBCDIC. Select ISO Latin-1 for the supplied samples.

The supplied old JSON is a name-to-length map totalling 3,008 bytes, without
verified PIC/type/offset metadata. It is not a valid schema. Do not use it to
split the data or simply add schema_version to it. The script now explains
this error and recommends regeneration to a NEW output folder.

For these files:
1. Run python -m folder_compare from the project directory.
2. Select folder_compare\Source and folder_compare\Target as A and B.
3. Choose Copybook/Column-Based.
4. Choose Generate JSON from Copybooks; select folder_compare\COPY.
5. Select a separate JSON output folder (not the supplied JSON folder).
6. Enable "Copybook fragments / REDEFINES: validate overlays, compare
   original storage only".
7. Schema data format = "128-byte file header + 4-byte record descriptors".
8. Encoding = ISO Latin-1. Validate / Scan, review details, then Compare.
Alternatively use corrected generated JSON via Use Existing JSON, retaining
the same headered data format and encoding. Generated schema notes preserve
the copybook policy decisions for future comparisons.

The optional original-storage parser policy permits copybook fragments with
no active 01 declaration, wrapping them in a synthetic 01 COPYBOOK-RECORD.
Commented-out 01 declarations remain comments. Fixed-format source now
recognizes alphanumeric change tags in columns 1-6, comment indicators in
column 7, and source columns 8-72. It supports fractional PIC SV9(n)/V9(n).

REDEFINES in this policy must reference an earlier sibling and must fit
within that original item's storage. Overlay declarations are parsed and
sized for validation, but only the original fields occupy the generated
schema and appear as comparison fields. Alternate view selection and
overlays that enlarge storage are still unsupported. Decisions are recorded
in JSON notes, validation details and reports. Strict policy remains default.

The explicit headered reader validates magic 3000007C in a complete 128-byte
header and a 4-byte big-endian descriptor for EVERY record. Descriptor top
nibble must be 4 and its low 28 bits must equal the fixed schema payload length,
or (for the three named layouts) the length calculated from that record's
KEY-OCCURS value. Variable records are checked against the copybook maximum and
zero alignment padding. Truncated headers/descriptors/payloads, other flag
values, invalid KEY-OCCURS values, length mismatches and trailing junk cause
the pair to be skipped with a reason. No heuristic header stripping occurs.
This reader supports only the observed framing, not every vendor's indexed or
variable file format.

Comparison excludes file-header metadata and record descriptors by explicit
selection. A/B byte sizes still show physical file sizes; record columns and
counts refer to payloads. Header-only differences therefore give Identical
payloads, with an explicit note. Normal comparison is unchanged.

Sequence matching uses exact whole records as anchors. If nearly every
record changes, matching by position can show cascaded changes after
insertions/deletions. For policy-keyed matching, the sample's company/policy
columns 1-18 are unique within each supplied file; use that key only if it
is the intended business identity. The tool does not silently select a key
or ignore changing dates.
