"""Tkinter settings panel and helper dialogs for the comparison options."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import records
from .layout import parse_layout
from .options import ENCODINGS, FORMATS, MATCH_MODES, CompareOptions

PREVIEW_BYTES = 4 * 1024 * 1024
_EXAMPLE_LAYOUT = (
    "# Paste a COBOL copybook, or one field per line as: name,start,length,type[,decimals]\n"
    "# Types: char, zoned, packed, binary, ubinary, hex.  Start is 1-based.\n"
    "# Example:\n"
    "# CUST-ID,1,8,char\n"
    "# NAME,9,20,char\n"
    "# AMOUNT,29,5,packed,2\n"
)


def _lookup(table, value: str, default: str) -> str:
    return next((v for label, v in table if label == value), default)


def _label(table, value: str) -> str:
    return next((label for label, v in table if v == value), table[0][0])


class SettingsPanel(ttk.LabelFrame):
    """All comparison settings. Leave everything on Auto if the file layout is unknown."""

    def __init__(self, master) -> None:
        super().__init__(master, text="Comparison settings (leave on Auto-detect if unsure)", padding=8)
        self.layout_text = ""
        s = self._vars = {
            "patterns": tk.StringVar(), "fmt": tk.StringVar(), "enc": tk.StringVar(),
            "lrecl": tk.StringVar(), "bdw": tk.BooleanVar(), "match": tk.StringVar(),
            "key_start": tk.StringVar(), "key_len": tk.StringVar(), "key_fields": tk.StringVar(),
            "ignore": tk.StringVar(),
            "trim": tk.BooleanVar(), "max_rows": tk.StringVar(), "max_mb": tk.StringVar(),
            "layout": tk.StringVar(value="none"),
            "comparison_type": tk.StringVar(), "schema_source": tk.StringVar(),
            "json_folder": tk.StringVar(), "copybook_folder": tk.StringVar(),
            "generated_json_folder": tk.StringVar(), "normal_text": tk.BooleanVar(),
            "copybook_original_storage": tk.BooleanVar(), "schema_data_format": tk.StringVar(),
        }
        self._build(s)
        self.set_options(CompareOptions())

    def _build(self, s: dict) -> None:
        def label(row, col, text):
            ttk.Label(self, text=text).grid(row=row, column=col, sticky="w", padx=(0, 4), pady=2)

        def combo(var, table, width):
            return ttk.Combobox(self, textvariable=var, values=[l for l, _ in table],
                                state="readonly", width=width)

        label(0, 0, "File filter:")
        ttk.Entry(self, textvariable=s["patterns"]).grid(row=0, column=1, columnspan=3, sticky="ew", padx=(0, 12))
        label(0, 4, "e.g. *.dat; *.txt (blank = all files)")

        label(1, 0, "Format:")
        self.cb_fmt = combo(s["fmt"], FORMATS, 30)
        self.cb_fmt.grid(row=1, column=1, sticky="w", padx=(0, 12))
        label(1, 2, "Record length:")
        self.en_lrecl = ttk.Entry(self, textvariable=s["lrecl"], width=8)
        self.en_lrecl.grid(row=1, column=3, sticky="w", padx=(0, 12))
        self.ck_bdw = ttk.Checkbutton(self, text="RDW files have 4-byte block headers (BDW)", variable=s["bdw"])
        self.ck_bdw.grid(row=1, column=4, sticky="w")

        label(2, 0, "Encoding:")
        combo(s["enc"], ENCODINGS, 30).grid(row=2, column=1, sticky="w", padx=(0, 12))
        ttk.Button(self, text="Detect / Preview a file...", command=self._detect).grid(
            row=2, column=2, columnspan=2, sticky="w")
        label(2, 4, "Shows the detected format and first records of one file")

        label(3, 0, "Match records:")
        self.cb_match = combo(s["match"], MATCH_MODES, 40)
        self.cb_match.grid(row=3, column=1, columnspan=2, sticky="w", padx=(0, 12))
        label(3, 3, "Key start / length:")
        keys = ttk.Frame(self)
        keys.grid(row=3, column=4, sticky="w")
        self.en_ks = ttk.Entry(keys, textvariable=s["key_start"], width=6)
        self.en_kl = ttk.Entry(keys, textvariable=s["key_len"], width=6)
        self.en_ks.pack(side="left")
        self.en_kl.pack(side="left", padx=4)

        label(4, 0, "Copybook key fields / ranges:")
        self.en_key_fields = ttk.Entry(self, textvariable=s["key_fields"])
        self.en_key_fields.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(0, 12))
        label(4, 4, "Names or 1-based ranges, comma-separated")

        label(5, 0, "Ignore columns:")
        ttk.Entry(self, textvariable=s["ignore"]).grid(row=5, column=1, columnspan=3, sticky="ew", padx=(0, 12))
        ttk.Checkbutton(self, text="Ignore trailing spaces / padding", variable=s["trim"]).grid(
            row=5, column=4, sticky="w")

        label(6, 0, "Field layout:")
        lay = ttk.Frame(self)
        lay.grid(row=6, column=1, columnspan=2, sticky="w")
        ttk.Button(lay, text="Edit layout...", command=self._edit_layout).pack(side="left")
        ttk.Label(lay, textvariable=s["layout"]).pack(side="left", padx=8)
        label(6, 3, "Max rows / file:")
        lim = ttk.Frame(self)
        lim.grid(row=6, column=4, sticky="w")
        ttk.Entry(lim, textvariable=s["max_rows"], width=7).pack(side="left")
        ttk.Label(lim, text="  Max size (MB):").pack(side="left")
        ttk.Entry(lim, textvariable=s["max_mb"], width=6).pack(side="left", padx=4)

        bar = ttk.Frame(self)
        bar.grid(row=7, column=0, columnspan=5, sticky="w", pady=(6, 0))
        for text, cmd in (("Save settings...", self._save), ("Load settings...", self._load),
                          ("Reset to Auto", lambda: self.set_options(CompareOptions()))):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        self.columnconfigure(3, weight=1)

        for key in ("fmt", "match"):
            s[key].trace_add("write", lambda *_: self._refresh())
        self._build_schema_controls(s)

    def _build_schema_controls(self, s: dict) -> None:
        mode = ttk.Frame(self)
        mode.grid(row=7, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        ttk.Label(mode, text="Comparison Type:").pack(side="left", padx=(0, 6))
        ttk.Combobox(mode, textvariable=s["comparison_type"], state="readonly", width=28,
                     values=("Normal", "Copybook/Column-Based")).pack(side="left")
        self.schema_frame = ttk.Frame(self)
        self.schema_frame.grid(row=8, column=0, columnspan=5, sticky="ew", pady=(4, 0))
        for col, (text, value) in enumerate((("Use Existing JSON", "existing"),
                                             ("Generate JSON from Copybooks", "generate"))):
            ttk.Radiobutton(self.schema_frame, text=text, value=value,
                            variable=s["schema_source"]).grid(row=0, column=col, sticky="w")
        self.folder_rows = {}
        for row, (key, title) in enumerate((("json_folder", "JSON folder:"),
                                           ("copybook_folder", "Copybook folder:"),
                                           ("generated_json_folder", "Generated JSON output:")), 1):
            frame = ttk.Frame(self.schema_frame)
            frame.grid(row=row, column=0, columnspan=3, sticky="ew")
            ttk.Label(frame, text=title, width=23).pack(side="left")
            ttk.Entry(frame, textvariable=s[key]).pack(side="left", fill="x", expand=True)
            ttk.Button(frame, text="Browse...", command=lambda k=key: self._browse_schema(k)).pack(side="left")
            self.folder_rows[key] = frame
        ttk.Checkbutton(self.schema_frame, variable=s["normal_text"],
                        text="Compare .txt pairs normally (no schema required)").grid(
                            row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(self.schema_frame, text="Schemas override Format, Record length and Field layout per file. "
                  "Validate/Scan is read-only; Compare generates JSON.", wraplength=950).grid(
                      row=5, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(self.schema_frame, variable=s["copybook_original_storage"],
                        text="Copybook fragments / REDEFINES: validate overlays, compare original storage only").grid(
                            row=6, column=0, columnspan=3, sticky="w")
        data = ttk.Frame(self.schema_frame)
        data.grid(row=7, column=0, columnspan=3, sticky="w")
        ttk.Label(data, text="Schema data format: ").pack(side="left")
        ttk.Combobox(data, textvariable=s["schema_data_format"], state="readonly", width=65,
                     values=("Plain fixed records", "128-byte file header + 4-byte record descriptors")).pack(side="left")
        self.schema_frame.columnconfigure(1, weight=1)
        for key in ("comparison_type", "schema_source"):
            s[key].trace_add("write", lambda *_: self._refresh_schema())

    def _browse_schema(self, key: str) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=self._vars[key].get() or None)
        if path:
            self._vars[key].set(os.path.normpath(path))

    def _refresh_schema(self) -> None:
        if self._vars["comparison_type"].get() == "Copybook/Column-Based":
            self.schema_frame.grid()
        else:
            self.schema_frame.grid_remove()
        generate = self._vars["schema_source"].get() == "generate"
        for key, frame in self.folder_rows.items():
            if (key == "json_folder") != generate:
                frame.grid()
            else:
                frame.grid_remove()

    def _refresh(self) -> None:
        fmt = _lookup(FORMATS, self._vars["fmt"].get(), "auto")
        match = _lookup(MATCH_MODES, self._vars["match"].get(), "sequence")
        self.en_lrecl.config(state="normal" if fmt in ("auto", "fixed") else "disabled")
        self.ck_bdw.config(state="normal" if fmt in ("auto", "rdw") else "disabled")
        key_state = "normal" if match == "key" else "disabled"
        self.en_ks.config(state=key_state)
        self.en_kl.config(state=key_state)
        self.en_key_fields.config(state=key_state)

    # ------------------------------------------------------------ options

    def get_options(self) -> CompareOptions:
        s = self._vars

        def num(name: str, title: str) -> int:
            text = s[name].get().strip()
            if not text:
                return 0
            if not text.isdigit():
                raise ValueError(f"{title} must be a whole number.")
            return int(text)

        opts = CompareOptions(
            comparison_type="copybook" if s["comparison_type"].get() == "Copybook/Column-Based" else "normal",
            schema_source=s["schema_source"].get(),
            json_folder=s["json_folder"].get().strip(),
            copybook_folder=s["copybook_folder"].get().strip(),
            generated_json_folder=s["generated_json_folder"].get().strip(),
            normal_text=bool(s["normal_text"].get()),
            copybook_original_storage=bool(s["copybook_original_storage"].get()),
            schema_data_format="headered" if s["schema_data_format"].get().startswith("128-byte") else "fixed",
            patterns=s["patterns"].get().strip(),
            fmt=_lookup(FORMATS, s["fmt"].get(), "auto"),
            encoding=_lookup(ENCODINGS, s["enc"].get(), "auto"),
            lrecl=num("lrecl", "Record length"),
            bdw=bool(s["bdw"].get()),
            match=_lookup(MATCH_MODES, s["match"].get(), "sequence"),
            key_start=num("key_start", "Key start"),
            key_length=num("key_len", "Key length"),
            key_fields=s["key_fields"].get().strip(),
            ignore_ranges=s["ignore"].get().strip(),
            trim_trailing=bool(s["trim"].get()),
            layout_text=self.layout_text,
            max_diff_rows=num("max_rows", "Max rows per file") or 2000,
            max_diff_mb=num("max_mb", "Max size (MB)") or 200,
        )
        return opts.prepare()

    def set_options(self, o: CompareOptions) -> None:
        s = self._vars
        s["comparison_type"].set("Copybook/Column-Based" if o.comparison_type == "copybook" else "Normal")
        s["schema_source"].set(o.schema_source)
        for key in ("json_folder", "copybook_folder", "generated_json_folder", "normal_text"):
            s[key].set(getattr(o, key))
        s["copybook_original_storage"].set(o.copybook_original_storage)
        s["schema_data_format"].set("128-byte file header + 4-byte record descriptors"
                                    if o.schema_data_format == "headered" else "Plain fixed records")
        s["patterns"].set(o.patterns)
        s["fmt"].set(_label(FORMATS, o.fmt))
        s["enc"].set(_label(ENCODINGS, o.encoding))
        s["lrecl"].set(str(o.lrecl) if o.lrecl else "")
        s["bdw"].set(o.bdw)
        s["match"].set(_label(MATCH_MODES, o.match))
        s["key_start"].set(str(o.key_start) if o.key_start else "")
        s["key_len"].set(str(o.key_length) if o.key_length else "")
        s["key_fields"].set(o.key_fields)
        s["ignore"].set(o.ignore_ranges)
        s["trim"].set(o.trim_trailing)
        s["max_rows"].set(str(o.max_diff_rows))
        s["max_mb"].set(str(o.max_diff_mb))
        self._set_layout(o.layout_text)
        self._refresh()
        self._refresh_schema()

    def _set_layout(self, text: str) -> None:
        self.layout_text = text.strip()
        try:
            lay = parse_layout(self.layout_text)
            self._vars["layout"].set("none" if lay is None else f"{len(lay.fields)} fields, {lay.length} bytes")
        except ValueError:
            self._vars["layout"].set("invalid - open the editor")

    # ----------------------------------------------------------- actions

    def _edit_layout(self) -> None:
        dlg = LayoutDialog(self, self.layout_text)
        self.wait_window(dlg)
        if dlg.result is not None:
            self._set_layout(dlg.result)

    def _detect(self) -> None:
        DetectDialog(self, self)

    def _save(self) -> None:
        try:
            opts = self.get_options()
        except ValueError as exc:
            messagebox.showerror("Settings", str(exc), parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                            filetypes=[("Settings profile", "*.json")],
                                            initialfile="folder_compare_settings.json")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(opts.to_json())
            except OSError as exc:
                messagebox.showerror("Settings", f"Could not save settings:\n{exc}", parent=self)

    def _load(self) -> None:
        path = filedialog.askopenfilename(parent=self, filetypes=[("Settings profile", "*.json"), ("All", "*.*")])
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    self.set_options(CompareOptions.from_json(f.read()))
            except (OSError, ValueError) as exc:
                messagebox.showerror("Settings", f"Could not load settings:\n{exc}", parent=self)


class LayoutDialog(tk.Toplevel):
    """Edit the optional field layout (COBOL copybook or CSV column list)."""

    def __init__(self, master, text: str) -> None:
        super().__init__(master)
        self.title("Field layout")
        self.geometry("720x520")
        self.result: str | None = None
        self.transient(master.winfo_toplevel())
        ttk.Label(self, padding=8, wraplength=680, text=(
            "Optional. A layout lets differences be reported per field (e.g. AMOUNT: 200.05 -> 999.99) "
            "and packed/zoned/binary values be decoded. It does not change which files are Identical.")
        ).pack(fill="x")
        self.text = scrolledtext.ScrolledText(self, wrap="none", font=("Consolas", 10))
        self.text.pack(fill="both", expand=True, padx=8)
        self.text.insert("1.0", text or _EXAMPLE_LAYOUT)
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, padding=8, wraplength=680).pack(fill="x")
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Load file...", command=self._load).pack(side="left")
        ttk.Button(bar, text="Check", command=self._check).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear layout", command=lambda: self._close("")).pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="OK", command=self._ok).pack(side="right", padx=6)
        self.grab_set()

    def _content(self) -> str:
        lines = [ln for ln in self.text.get("1.0", "end").splitlines()]
        return "\n".join(lines).strip()

    def _check(self) -> bool:
        try:
            lay = parse_layout(self._content())
        except ValueError as exc:
            self.status.set(f"Problem: {exc}")
            return False
        if lay is None:
            self.status.set("No layout (blank).")
        else:
            warn = ("  Notes: " + " ".join(lay.warnings)) if lay.warnings else ""
            self.status.set(f"OK: {len(lay.fields)} fields, record length {lay.length} bytes.{warn}")
        return True

    def _load(self) -> None:
        path = filedialog.askopenfilename(parent=self, filetypes=[("Layout / copybook", "*.cpy *.cbl *.txt *.csv"),
                                                                  ("All", "*.*")])
        if path:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    self.text.delete("1.0", "end")
                    self.text.insert("1.0", f.read())
            except OSError as exc:
                messagebox.showerror("Field layout", str(exc), parent=self)

    def _close(self, value: str) -> None:
        self.result = value
        self.destroy()

    def _ok(self) -> None:
        if self._check():
            content = self._content()
            self._close("" if content == _EXAMPLE_LAYOUT.strip() else content)


class DetectDialog(tk.Toplevel):
    """Inspect one file: detected structure/encoding, candidate record lengths, first records."""

    def __init__(self, master, panel: SettingsPanel) -> None:
        path = filedialog.askopenfilename(parent=master, title="Choose a file to analyse")
        super().__init__(master)
        self.panel, self.plan = panel, None
        self.title("Detect / Preview")
        self.geometry("820x560")
        self.transient(master.winfo_toplevel())
        self.text = scrolledtext.ScrolledText(self, wrap="none", font=("Consolas", 10))
        self.text.pack(fill="both", expand=True, padx=8, pady=8)
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        self.btn_apply = ttk.Button(bar, text="Use these settings", command=self._apply, state="disabled")
        self.btn_apply.pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        if not path:
            self.destroy()
            return
        self._analyse(path)

    def _write(self, line: str = "") -> None:
        self.text.insert("end", line + "\n")

    def _analyse(self, path: str) -> None:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                head = f.read(PREVIEW_BYTES)
            if not head:
                self._write("The file is empty.")
                return
            probe = CompareOptions(layout_text=self.panel.layout_text)
            plan = records.analyze([(head, size)], probe.prepare())
        except (OSError, ValueError) as exc:
            self._write(f"Could not analyse the file: {exc}")
            return
        self.plan = plan
        self._write(f"File: {path}")
        self._write(f"Size: {size:,} bytes")
        self._write(f"Detected: {plan.desc}")
        for note in plan.notes:
            self._write(f"Note: {note}")
        if plan.candidates:
            ranked = ", ".join(f"{d} ({s:.2f})" for d, s in plan.candidates)
            self._write(f"Record length candidates (score): {ranked}")
            self._write("If the detected length looks wrong, type the correct one in 'Record length'.")
        self._write("")
        decode = records.make_decoder(plan.codec)
        try:
            recs = records.split_records(head, plan) if plan.fmt != "binary" else \
                [head[i:i + 16] for i in range(0, 160, 16)]
        except records.RecordError as exc:
            self._write(f"Could not split records: {exc}")
            recs = [head[i:i + 16] for i in range(0, 160, 16)]
        self._write("First records (text view):")
        for i, rec in enumerate(recs[:8], 1):
            self._write(f"{i:>4}: {decode(rec)[:140]}")
        if recs:
            self._write("")
            self._write("First record (hex):")
            raw = recs[0][:96]
            for off in range(0, len(raw), 32):
                self._write(f"{off + 1:>5}: " + raw[off:off + 32].hex(" ").upper())
        self.btn_apply.config(state="normal")

    def _apply(self) -> None:
        plan = self.plan
        opts = CompareOptions(layout_text=self.panel.layout_text)
        try:
            current = self.panel.get_options()
            opts = current
        except ValueError:
            pass
        opts.fmt = plan.fmt
        opts.lrecl = plan.lrecl if plan.fmt == "fixed" else 0
        opts.bdw = plan.bdw
        opts.encoding = plan.codec if plan.codec in {v for _, v in ENCODINGS} else "auto"
        self.panel.set_options(opts)
        self.destroy()
