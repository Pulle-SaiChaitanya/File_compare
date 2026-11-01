"""Self-contained HTML report generation."""
from __future__ import annotations

import datetime as _dt
import json
from html import escape
from typing import TYPE_CHECKING

from .core import FileResult, IDENTICAL, MODIFIED, ONLY_A, ONLY_B, SKIPPED, STATUSES, summarize

if TYPE_CHECKING:
    from .schema_workflow import ScanItem

_CSS_CLASS = {ONLY_A: "onlya", ONLY_B: "onlyb", IDENTICAL: "same", MODIFIED: "mod", SKIPPED: "skipped"}

_STYLE = """
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f5f6f8;color:#222}
header{padding:16px 24px;background:#263238;color:#fff}
header h1{margin:0 0 6px;font-size:20px}
header div{font-size:12px;opacity:.85;word-break:break-all}
.bar{position:sticky;top:0;z-index:5;background:#fff;padding:10px 24px;border-bottom:1px solid #ddd;
 display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.card{border:2px solid;border-radius:6px;padding:6px 12px;cursor:pointer;user-select:none;font-size:13px;background:#fff}
.card b{font-size:18px;margin-right:4px}.card.off{opacity:.35}
.onlya{--c:#c62828;--bg:#fdecea}.onlyb{--c:#1565c0;--bg:#e3f2fd}
.same{--c:#2e7d32;--bg:#e8f5e9}.mod{--c:#ef6c00;--bg:#fff3e0}
.skipped{--c:#616161;--bg:#eee}
.validation{background:#fff;padding:12px;margin-bottom:16px}
.validation table{border-collapse:collapse;width:100%;font-size:12px;table-layout:fixed}
.validation th,.validation td{border:1px solid #ddd;padding:5px;word-break:break-all;text-align:left}
.validation tr.ready{background:#e8f5e9}.validation tr.invalid{background:#fdecea}
.card{border-color:var(--c);color:var(--c)}
#q{flex:1;min-width:220px;padding:7px 10px;border:1px solid #bbb;border-radius:4px;font-size:14px}
#count{font-size:13px;color:#555}
main{padding:16px 24px}
details.f{background:#fff;border-left:6px solid var(--c);margin:6px 0;border-radius:3px;box-shadow:0 1px 2px #0002}
details.f>summary{padding:8px 12px;cursor:pointer;background:var(--bg);display:flex;gap:12px;align-items:center}
.tag{font-size:11px;font-weight:600;color:#fff;background:var(--c);padding:2px 8px;border-radius:10px;white-space:nowrap}
.path{font-family:Consolas,monospace;font-size:13px;flex:1;word-break:break-all}
.meta{font-size:12px;color:#555;white-space:nowrap}
.body{padding:8px 12px;font-size:13px}
table.d{border-collapse:collapse;width:100%;font-family:Consolas,monospace;font-size:12px;table-layout:fixed}
table.d td{padding:1px 6px;vertical-align:top;white-space:pre-wrap;word-break:break-all;border-bottom:1px solid #f0f0f0}
td.n{width:48px;text-align:right;color:#888;background:#fafafa;user-select:none}
tr.added td.b{background:#d4f5d8}tr.removed td.a{background:#fbd5d5}
tr.changed td.a{background:#fde7c4}tr.changed td.b{background:#fde7c4}
tr.skip td{background:#eef;color:#667;text-align:center;font-style:italic}
.legend span{display:inline-block;padding:1px 8px;margin-right:6px;font-size:12px;border-radius:3px}
mark{background:#ffd54f;color:#000;padding:0}
tr.detail td{background:#fffde7;color:#5d4037;font-size:11px;padding:2px 6px 4px 70px}
tr.hd th{background:#eceff1;font-size:11px;text-align:left;padding:2px 6px}
.note{margin:4px 0;color:#455a64;font-size:12px}.note.warn{color:#c62828;font-weight:600}
.hidden{display:none}
.field-controls{margin:10px 0;padding:8px;background:#f5f6f8}
.field-choices{max-height:180px;overflow:auto;display:flex;flex-wrap:wrap;gap:6px 16px}
.field-choices label{font-size:12px}
.field-scroll{overflow:auto;max-height:65vh;margin:8px 0}
table.fields{border-collapse:separate;border-spacing:0;font:12px Consolas,monospace}
table.fields th,table.fields td{border:1px solid #ddd;padding:5px;min-width:130px;max-width:400px;
 white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top}
table.fields th{background:#eceff1;position:sticky;top:0;z-index:1}
table.fields .record-id{min-width:90px}
table.fields td.changed{background:#fde7c4}
table.fields td.added{background:#d4f5d8}
table.fields td.removed{background:#fbd5d5}
.field-pager{display:flex;gap:10px;align-items:center;margin:8px 0}
"""

_SCRIPT = """
const active=new Set(%s);
const q=document.getElementById('q'),items=[...document.querySelectorAll('details.f')];
const cache=new WeakMap();
function text(el){let t=cache.get(el);if(t===undefined){t=el.textContent.toLowerCase();cache.set(el,t);}return t;}
function apply(){
 const s=q.value.trim().toLowerCase();let n=0;
 for(const el of items){
  const ok=active.has(el.dataset.status)&&(!s||el.dataset.path.includes(s)||text(el).includes(s));
  el.classList.toggle('hidden',!ok);if(ok)n++;}
 document.getElementById('count').textContent=n+' of '+items.length+' shown';}
document.querySelectorAll('.card').forEach(c=>c.onclick=()=>{
 const s=c.dataset.status;if(active.has(s)){active.delete(s);c.classList.add('off');}
 else{active.add(s);c.classList.remove('off');}apply();});
const validationRows=[...document.querySelectorAll('tr[data-validation]')];
const validationFilter=document.getElementById('validation-filter');
function applyValidation(){
 const s=q.value.trim().toLowerCase(),status=validationFilter?validationFilter.value:'';
 for(const row of validationRows){
  row.classList.toggle('hidden',(status&&row.dataset.validation!==status)||(s&&!text(row).includes(s)));}}
q.oninput=()=>{apply();applyValidation();};
if(validationFilter)validationFilter.onchange=applyValidation;
document.getElementById('exp').onclick=()=>items.forEach(e=>{if(!e.classList.contains('hidden'))e.open=true;});
document.getElementById('col').onclick=()=>items.forEach(e=>e.open=false);
apply();
applyValidation();
"""

_FIELD_SCRIPT = """
function initFields(view){
 if(view.dataset.ready)return;
 view.dataset.ready='true';
 const data=JSON.parse(view.querySelector('.field-data').textContent);
 const choices=view.querySelector('.field-choices'),table=view.querySelector('table.fields');
 const selected=new Set(data.fields.map((_,i)=>i)),size=25;
 let pageIndex=0;
 const node=(tag,value)=>{const el=document.createElement(tag);if(value!==undefined)el.textContent=value;return el;};
 const checks=data.fields.map((f,i)=>{
  const label=node('label'),check=node('input');check.type='checkbox';check.checked=true;
  check.onchange=()=>{if(check.checked)selected.add(i);else selected.delete(i);render();};
  label.append(check,document.createTextNode(f.name+' (cols '+f.start+'-'+(f.start+f.length-1)+')'));
  choices.append(label);return check;
 });
 function render(){
  const indices=data.fields.map((_,i)=>i).filter(i=>selected.has(i));
  const head=node('thead'),hr=node('tr');
  for(const name of ['Change','A record','B record','Side'])hr.append(node('th',name));
  for(const i of indices){
   const f=data.fields[i],th=node('th',f.name);
   th.title='Columns '+f.start+'-'+(f.start+f.length-1)+' | '+f.type;
   hr.append(th);
  }
  head.append(hr);
  const body=node('tbody');
  for(const r of data.rows.slice(pageIndex*size,(pageIndex+1)*size)){
   for(const side of [0,1]){
    const tr=node('tr');
    for(const value of [r.kind,r.a??'-',r.b??'-',side===0?'A':'B']){
     const td=node('td',value);td.className='record-id';tr.append(td);
    }
    for(const i of indices){
     const v=r.values[i],td=node('td',v[side]);
     if(v[2])td.className=r.kind;
     tr.append(td);
    }
    body.append(tr);
   }
  }
  table.replaceChildren(head,body);
  const total=data.rows.length,start=total?pageIndex*size+1:0;
  view.querySelector('.field-page').textContent=start+'-'+Math.min((pageIndex+1)*size,total)
   +' of '+total+' retained differing records; '+indices.length+' of '+data.fields.length+' columns visible';
  view.querySelector('.field-prev').disabled=pageIndex===0;
  view.querySelector('.field-next').disabled=(pageIndex+1)*size>=total;
 }
 view.querySelector('.field-prev').onclick=()=>{pageIndex--;render();};
 view.querySelector('.field-next').onclick=()=>{pageIndex++;render();};
 view.querySelector('.field-all').onclick=()=>{checks.forEach((c,i)=>{c.checked=true;selected.add(i);});render();};
 view.querySelector('.field-none').onclick=()=>{checks.forEach(c=>c.checked=false);selected.clear();render();};
 view.querySelector('.field-search').oninput=e=>{
  const query=e.target.value.toLowerCase();
  [...choices.children].forEach(label=>label.hidden=!label.textContent.toLowerCase().includes(query));
 };
 render();
}
document.querySelectorAll('details.f').forEach(section=>{
 section.addEventListener('toggle',()=>{
  if(section.open)section.querySelectorAll('.field-view').forEach(initFields);
 });
});
"""


def _field_table(res: FileResult) -> str:
    d = res.diff
    data = {
        "fields": [{"name": f.name, "start": f.start + 1, "length": f.length, "type": f.ftype}
                   for f in d.fields],
        "rows": [{"kind": r.kind, "a": r.no_a, "b": r.no_b,
                  "values": [[v.text_a, v.text_b, v.changed] for v in r.field_values]}
                 for r in d.rows if r.kind in ("added", "removed", "changed")],
    }
    # Prevent field values from closing the inert JSON script element.
    payload = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        '<section class="field-view"><h3>Record fields</h3>'
        '<p>Folder A and B appear on separate rows under the same field headings. '
        'Coloured cells indicate differences used by the comparison. '
        'Unchecked columns are hidden only: results and counts do not change. '
        'Search still includes hidden columns. Only retained differing records are shown; '
        'the existing detail limit applies. Raw records below include context and any unmapped bytes.</p>'
        '<details class="field-controls"><summary>Show / hide columns</summary>'
        '<input class="field-search" type="search" aria-label="Find column" placeholder="Find column...">'
        '<button type="button" class="field-all">Show all columns</button> '
        '<button type="button" class="field-none">Hide all columns</button>'
        '<div class="field-choices"></div></details>'
        '<div class="field-pager"><button type="button" class="field-prev">Previous</button>'
        '<span class="field-page"></span><button type="button" class="field-next">Next</button></div>'
        '<div class="field-scroll"><table class="fields" aria-label="Record field differences"></table></div>'
        '<noscript>Enable JavaScript for the field table; raw differences remain available below.</noscript>'
        f'<script type="application/json" class="field-data">{payload}</script></section>'
    )


def _fmt_size(n) -> str:
    return "-" if n is None else f"{n:,} B"


def _marked(text: str, marks: list) -> str:
    """HTML-escape text, wrapping the [start, end) ranges in <mark>."""
    if not marks:
        return escape(text)
    out, pos = [], 0
    for s, e in marks:
        s, e = max(s, pos), min(e, len(text))
        if s >= e:
            continue
        out.append(escape(text[pos:s]))
        out.append(f"<mark>{escape(text[s:e])}</mark>")
        pos = e
    out.append(escape(text[pos:]))
    return "".join(out)


def _diff_table(res: FileResult) -> str:
    d = res.diff
    rows = []
    for r in d.rows:
        if r.kind == "skip":
            rows.append(f'<tr class="skip"><td colspan="4">{escape(r.text_a)}</td></tr>')
            continue
        na = "" if r.no_a is None else r.no_a
        nb = "" if r.no_b is None else r.no_b
        # trailing padding is trimmed for display only; highlight ranges are clipped to match
        rows.append(
            f'<tr class="{r.kind}"><td class="n">{na}</td><td class="a">{_marked(r.text_a.rstrip(" "), r.marks_a)}</td>'
            f'<td class="n">{nb}</td><td class="b">{_marked(r.text_b.rstrip(" "), r.marks_b)}</td></tr>'
        )
        if r.detail:
            lines = "<br>".join(escape(x) for x in r.detail)
            rows.append(f'<tr class="detail"><td colspan="4">{lines}</td></tr>')
    unit = d.unit.lower()
    info = (f'{d.added:,} added, {d.removed:,} removed, {d.changed:,} changed {unit}s'
            f' (A: {escape(d.encoding_a)}, B: {escape(d.encoding_b)})')
    extra = "".join(f'<div class="note">{escape(n)}</div>' for n in d.notes)
    if d.truncated:
        extra += (f'<div class="note warn">Only the first {len(d.rows):,} rows are shown. '
                  'Raise "Max differences shown per file" in the settings to see more.</div>')
    raw_table = (
        '<table class="d"><colgroup><col style="width:64px"><col><col style="width:64px"><col></colgroup>'
        f'<tr class="hd"><th class="n">A {unit}</th><th>File A</th><th class="n">B {unit}</th><th>File B</th></tr>'
        + "".join(rows) + "</table>"
    )
    if d.fields:
        raw_table = (_field_table(res) + '<details><summary>Raw record differences and field details</summary>'
                     + raw_table + '</details>')
    return (
        '<div class="legend"><span style="background:#d4f5d8">added</span>'
        '<span style="background:#fbd5d5">removed</span>'
        '<span style="background:#fde7c4">changed</span><mark>differing bytes</mark> '
        f'{info}</div>{extra}'
        + raw_table
    )


def _file_section(res: FileResult) -> str:
    cls = _CSS_CLASS[res.status]
    meta = f"A: {_fmt_size(res.size_a)} | B: {_fmt_size(res.size_b)}"
    if res.diff and res.diff.total:
        meta += f" | +{res.diff.added:,} -{res.diff.removed:,} ~{res.diff.changed:,}"
    body = ""
    if res.format_desc:
        body += f'<div class="note">Compared as: {escape(res.format_desc)}</div>'
    if res.first_diff is not None and res.status == MODIFIED:
        body += f'<div class="note">First differing byte: offset {res.first_diff:,} (0x{res.first_diff:X})</div>'
    if res.note:
        body += f'<div class="note">{escape(res.note)}</div>'
    if res.diff and res.diff.total:
        body += _diff_table(res)
    elif res.diff and res.diff.notes:
        body += "".join(f'<div class="note">{escape(n)}</div>' for n in res.diff.notes)
    kind = f" [{res.kind}]" if res.kind in ("binary", "empty", "error") else ""
    body_html = f'<div class="body">{body}</div>' if body else ""
    return (
        f'<details class="f {cls}" data-status="{escape(res.status)}" '
        f'data-path="{escape(res.rel_path.lower())}"><summary>'
        f'<span class="tag">{res.status}</span><span class="path">{escape(res.rel_path)}{kind}</span>'
        f'<span class="meta">{meta}</span></summary>{body_html}</details>'
    )


def _validation_section(items: list[ScanItem]) -> str:
    from .schema_workflow import READY, VALIDATION_STATUSES

    counts = {status: sum(item.status == status for item in items) for status in VALIDATION_STATUSES}
    summary = " | ".join(f"{status}: {count}" for status, count in counts.items())
    options = '<option value="">All validation statuses</option>' + "".join(
        f'<option value="{escape(status)}">{escape(status)}</option>' for status in VALIDATION_STATUSES)
    rows = []
    for item in items:
        cells = (item.basename, item.status, "; ".join(item.paths_a), "; ".join(item.paths_b),
                 "; ".join(item.definitions), item.note)
        cls = "ready" if item.status == READY else "invalid"
        rows.append(f'<tr class="{cls}" data-validation="{escape(item.status)}">'
                    + "".join(f"<td>{escape(cell)}</td>" for cell in cells) + "</tr>")
    return ('<section class="validation"><h2>Validation / Scan</h2>'
            f'<p>{escape(summary)}</p><select id="validation-filter">{options}</select>'
            '<p>Ready means eligible for comparison, not Identical. Invalid/missing matches are skipped '
            'in Copybook mode. Normal mode retains Only A / Only B results.</p>'
            '<table><thead><tr><th>Base filename / path</th><th>Status</th><th>Folder A</th>'
            '<th>Folder B</th><th>JSON / Copybook</th><th>Details</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></section>")


def _configuration_section(configuration: dict[str, object] | None) -> str:
    if not configuration:
        return ""
    rows = []
    for label, key in (
        ("File A name", "file_a"), ("File B name", "file_b"),
        ("Comparison type", "comparison_type"), ("Record layout", "record_layout"),
        ("Key column(s)", "key_columns"),
        ("Columns included in comparison", "included_columns"),
        ("Columns excluded from comparison", "excluded_columns"),
    ):
        value = configuration.get(key, "Not applicable")
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value) or "(none)"
        rows.append(f"<tr><th>{escape(label)}</th><td>{escape(str(value))}</td></tr>")
    return ('<section class="validation"><h2>Comparison Configuration</h2>'
            '<table><tbody>' + "".join(rows) + "</tbody></table></section>")


def build_report(folder_a: str, folder_b: str, results: list[FileResult], settings: str = "",
                 validation: list[ScanItem] | None = None,
                 configuration: dict[str, object] | None = None) -> str:
    counts = summarize(results)
    cards = "".join(
        f'<div class="card {_CSS_CLASS[s]}" data-status="{s}"><b>{counts[s]}</b>{s}</div>'
        for s in STATUSES
    )
    sections = "".join(_file_section(r) for r in results)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active = "[" + ",".join(f'"{s}"' for s in STATUSES) + "]"
    settings_html = f"<div>Settings: {escape(settings)}</div>" if settings else ""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<title>Folder Comparison Report</title>'
        f"<style>{_STYLE}</style></head><body>"
        f"<header><h1>Folder Comparison Report</h1><div>A: {escape(folder_a)}</div>"
        f"<div>B: {escape(folder_b)}</div>{settings_html}"
        f"<div>Generated {stamp} | {len(results)} files</div></header>"
        f'<div class="bar">{cards}<input id="q" type="search" placeholder="Search path or content...">'
        '<button id="exp">Expand all</button><button id="col">Collapse all</button>'
        '<span id="count"></span></div>'
        f"<main>{_configuration_section(configuration)}"
        f"{_validation_section(validation) if validation else ''}{sections}</main>"
        f"<script>{_SCRIPT % active}{_FIELD_SCRIPT}</script></body></html>"
    )


def write_report(path: str, folder_a: str, folder_b: str, results: list[FileResult],
                 settings: str = "", validation: list[ScanItem] | None = None,
                 configuration: dict[str, object] | None = None) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_report(folder_a, folder_b, results, settings, validation, configuration))
