"""Tkinter GUI for the folder compare tool."""
from __future__ import annotations

import os
import queue
import re
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

from .core import (IDENTICAL, MODIFIED, ONLY_A, ONLY_B, SKIPPED, STATUSES, compare_folders,
                   summarize)
from .html_report import write_report
from .options import CompareOptions
from .schema import Schema
from .settings_panel import SettingsPanel
from .schema_workflow import (READY, ScanItem, compare_scanned, scan_inputs, scan_normal_inputs)
from .work import ComparisonCancelled, Work

_COLORS = {ONLY_A: "#fdecea", ONLY_B: "#e3f2fd", IDENTICAL: "#e8f5e9", MODIFIED: "#fff3e0",
           SKIPPED: "#eeeeee"}


@dataclass
class _ColumnSelectionRequest:
    item: ScanItem
    schema: Schema
    completed: threading.Event
    selected: tuple[str, ...] | None = None


@dataclass
class _SchemaSelectionRequest:
    item: ScanItem
    schemas: tuple[Schema, ...]
    completed: threading.Event
    selected: Schema | None = None


def _inside(path: str, folder: str) -> bool:
    path, folder = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(folder))
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(folder)]) == os.path.abspath(folder)
    except ValueError:  # different drives
        return False


def _report_filenames(results) -> list[str]:
    parts = [result.rel_path.replace("\\", "/").split("/") for result in results]
    stems = [os.path.splitext(path[-1])[0] for path in parts]
    counts: dict[str, int] = {}
    for stem in stems:
        counts[stem.casefold()] = counts.get(stem.casefold(), 0) + 1
    names = []
    used: set[str] = set()
    for path, stem in zip(parts, stems):
        candidate = stem
        if counts[stem.casefold()] > 1:
            relative = "_".join(path[:-1] + [path[-1]])
            candidate += "__" + relative
        candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", candidate).strip(" .") or "comparison"
        candidate = candidate[:180].rstrip(" .") or "comparison"
        filename = candidate + ".html"
        suffix = 2
        while filename.casefold() in used:
            filename = f"{candidate}_{suffix}.html"
            suffix += 1
        used.add(filename.casefold())
        names.append(filename)
    return names


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Folder Compare")
        self.geometry("1040x860")
        self.var_a = tk.StringVar()
        self.var_b = tk.StringVar()
        self.var_status = tk.StringVar(value="Select two folders.")
        self.results = []
        self.scan_items: list[ScanItem] = []
        self.validation_only = False
        self.opts = CompareOptions()
        self.queue: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.compared_roots = ("", "")
        self.definition_roots: tuple[str, ...] = ()
        self.partial = False
        self.started = 0.0
        self.last_activity = 0.0
        self.activity = ""
        self.file_progress = ""
        self._selection_window: tk.Toplevel | None = None
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        for row, (label, var) in enumerate((("Folder A:", self.var_a), ("Folder B:", self.var_b))):
            ttk.Label(top, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(top, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6)
            ttk.Button(top, text="Browse...", command=lambda v=var: self._browse(v)).grid(row=row, column=2)
        top.columnconfigure(1, weight=1)

        self.settings = SettingsPanel(self)
        self.settings.pack(fill="x", padx=8, pady=(0, 4))

        btns = ttk.Frame(self, padding=(8, 0))
        btns.pack(fill="x")
        self.btn_scan = ttk.Button(btns, text="Validate / Scan", command=self.validate)
        self.btn_scan.pack(side="left", padx=(0, 6))
        self.btn_compare = ttk.Button(btns, text="Compare", command=self.start)
        self.btn_compare.pack(side="left")
        self.btn_cancel = ttk.Button(btns, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_report = ttk.Button(btns, text="Save Individual HTML Reports...", command=self.save_report,
                                     state="disabled")
        self.btn_report.pack(side="left")
        self.progress = ttk.Progressbar(btns, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        self.var_path = tk.StringVar()
        ttk.Label(self, textvariable=self.var_path, padding=(8, 4), wraplength=1000).pack(fill="x")
        ttk.Label(self, textvariable=self.var_status, padding=(8, 0), wraplength=1000).pack(fill="x")

        self.summary = ttk.Frame(self, padding=8)
        self.summary.pack(fill="x")
        self.sum_labels = {}
        for s in STATUSES:
            lbl = tk.Label(self.summary, text=f"{s}: 0", bg=_COLORS[s], padx=10, pady=4, relief="groove")
            lbl.pack(side="left", padx=4)
            self.sum_labels[s] = lbl

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8)
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Comparison results")
        cols = ("status", "path", "size_a", "size_b", "format", "note")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, w in zip(cols, (80, 330, 80, 80, 230, 300)):
            self.tree.heading(c, text=c.replace("_", " ").title())
            self.tree.column(c, width=w, anchor="w")
        for s, color in _COLORS.items():
            self.tree.tag_configure(s, background=color)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        scan_frame = ttk.Frame(self.notebook)
        self.notebook.add(scan_frame, text="Validation / Scan")
        scan_cols = ("basename", "status", "a", "b", "definition", "note")
        self.scan_tree = ttk.Treeview(scan_frame, columns=scan_cols, show="headings")
        for col, title, width in zip(scan_cols, ("Base filename", "Validation status", "Folder A file(s)",
                                                "Folder B file(s)", "JSON / Copybook", "Details"),
                                    (150, 140, 180, 180, 180, 350)):
            self.scan_tree.heading(col, text=title)
            self.scan_tree.column(col, width=width)
        self.scan_tree.tag_configure("ready", background="#e8f5e9")
        self.scan_tree.tag_configure("invalid", background="#fdecea")
        vscroll = ttk.Scrollbar(scan_frame, orient="vertical", command=self.scan_tree.yview)
        hscroll = ttk.Scrollbar(scan_frame, orient="horizontal", command=self.scan_tree.xview)
        self.scan_tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        self.scan_tree.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        scan_frame.rowconfigure(0, weight=1)
        scan_frame.columnconfigure(0, weight=1)

    def _browse(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(initialdir=var.get() or None)
        if path:
            var.set(os.path.normpath(path))

    def start(self) -> None:
        self._start_operation(False)

    def validate(self) -> None:
        self._start_operation(True)

    def _start_operation(self, validation_only: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        a, b = self.var_a.get().strip(), self.var_b.get().strip()
        if not (os.path.isdir(a) and os.path.isdir(b)):
            messagebox.showerror("Folder Compare", "Both Folder A and Folder B must be existing folders.")
            return
        try:
            opts = self.settings.get_options()
        except ValueError as exc:
            messagebox.showerror("Folder Compare - settings", str(exc))
            return
        self.opts = opts
        self.validation_only = validation_only
        self.compared_roots = (os.path.abspath(a), os.path.abspath(b))
        self.definition_roots = tuple(os.path.abspath(path) for path in
                                      (opts.json_folder, opts.copybook_folder, opts.generated_json_folder)
                                      if path) if opts.comparison_type == "copybook" else ()
        self.partial = False
        self.tree.delete(*self.tree.get_children())
        self.results = []
        self.scan_items = []
        self.scan_tree.delete(*self.scan_tree.get_children())
        for s in STATUSES:
            self.sum_labels[s].config(text=f"{s}: 0")
        self.cancel.clear()
        self.btn_compare.config(state="disabled")
        self.btn_scan.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.btn_report.config(state="disabled")
        self.started = self.last_activity = time.monotonic()
        self.activity = "Starting comparison"
        self.file_progress = "Scanning folders"
        self.var_path.set("")
        self.progress.config(mode="indeterminate")
        self.progress.start(30)
        self._show_activity()
        self.worker = threading.Thread(target=self._run, args=(a, b, opts), daemon=True)
        self.worker.start()
        self.after(50, self._poll)

    def _run(self, a: str, b: str, opts: CompareOptions) -> None:
        try:
            if opts.comparison_type == "copybook" or self.validation_only:
                work = Work(self.cancel.is_set, lambda stage: self.queue.put(("activity", stage)))
                items = (scan_inputs(a, b, opts, work) if opts.comparison_type == "copybook" else
                         scan_normal_inputs(a, b, opts, work))
                self.queue.put(("scan", items))
                work.check()
                if self.validation_only:
                    self.queue.put(("scan_done",))
                    return
                res = compare_scanned(items, opts, work,
                                      lambda i, n, p: self.queue.put(("progress", i, n, p)),
                                      self._request_column_selection,
                                      self._request_schema_selection)
                self.queue.put(("done", res))
                return
            res = compare_folders(
                a, b, opts,
                progress=lambda i, n, p: self.queue.put(("progress", i, n, p)),
                cancelled=self.cancel.is_set,
                activity=lambda stage: self.queue.put(("activity", stage)),
            )
            self.queue.put(("done", res))
        except ComparisonCancelled:
            self.queue.put(("done", []))
        except Exception as exc:  # surfaced in the UI thread
            self.queue.put(("error", str(exc)))

    def _poll(self) -> None:
        try:
            for _ in range(200):
                msg = self.queue.get_nowait()
                if msg[0] == "progress":
                    _, i, n, p = msg
                    self.file_progress = f"{i:,}/{n:,} files completed"
                    self.var_path.set(f"File: {p}")
                    self.last_activity = time.monotonic()
                elif msg[0] == "activity":
                    self.activity = msg[1]
                    self.last_activity = time.monotonic()
                elif msg[0] == "scan":
                    self.scan_items = msg[1]
                elif msg[0] == "scan_done":
                    self._finish_scan()
                    return
                elif msg[0] == "column_selection":
                    self._show_column_selection(msg[1])
                elif msg[0] == "schema_selection":
                    self._show_schema_selection(msg[1])
                elif msg[0] == "done":
                    self._finish(msg[1])
                    return
                else:
                    self._reset_buttons()
                    self.var_status.set("Error")
                    messagebox.showerror("Folder Compare", msg[1])
                    return
        except queue.Empty:
            pass
        self._show_activity()
        self.after(50, self._poll)

    def _request_column_selection(self, item: ScanItem, schema: Schema) -> tuple[str, ...] | None:
        request = _ColumnSelectionRequest(item, schema, threading.Event())
        self.queue.put(("activity", f"Waiting for column selection: {item.basename}"))
        self.queue.put(("column_selection", request))
        while not request.completed.wait(0.1):
            if self.cancel.is_set():
                return None
        return request.selected

    def _request_schema_selection(self, item: ScanItem,
                                  schemas: tuple[Schema, ...]) -> Schema | None:
        request = _SchemaSelectionRequest(item, schemas, threading.Event())
        self.queue.put(("activity", f"Waiting for record layout selection: {item.basename}"))
        self.queue.put(("schema_selection", request))
        while not request.completed.wait(0.1):
            if self.cancel.is_set():
                return None
        return request.selected

    def _show_schema_selection(self, request: _SchemaSelectionRequest) -> None:
        window = tk.Toplevel(self)
        self._selection_window = window
        window.title(f"Select record layout - {request.item.basename}")
        window.transient(self)
        window.geometry("680x420")
        window.minsize(480, 300)
        ttk.Label(window, text="This copybook defines multiple record layouts. Choose the one "
                               "that represents the compared files.", wraplength=640, padding=10).pack(fill="x")
        tree = ttk.Treeview(window, columns=("length", "fields"), show="tree headings", selectmode="browse")
        tree.heading("#0", text="Record layout")
        tree.heading("length", text="Record length")
        tree.heading("fields", text="Fields")
        tree.column("#0", width=360, anchor="w")
        tree.column("length", width=120, anchor="e")
        tree.column("fields", width=100, anchor="e")
        tree.pack(fill="both", expand=True, padx=10, pady=5)
        for index, schema in enumerate(request.schemas):
            name = schema.fields[0].name.split(".", 1)[0]
            tree.insert("", "end", iid=str(index), text=name,
                        values=(f"{schema.record_length:,} bytes", f"{len(schema.fields):,}"))
        footer = ttk.Frame(window, padding=10)
        footer.pack(fill="x")
        continue_button = ttk.Button(footer, text="Use selected layout", state="disabled")
        continue_button.pack(side="right")

        def finish(cancelled: bool = False) -> None:
            if request.completed.is_set():
                return
            selected = tree.selection()
            request.selected = (None if cancelled or not selected else
                                request.schemas[int(selected[0])])
            if cancelled or request.selected is None:
                self.cancel.set()
            request.completed.set()
            self._selection_window = None
            window.destroy()

        tree.bind("<<TreeviewSelect>>", lambda _event: continue_button.config(
            state="normal" if tree.selection() else "disabled"))
        continue_button.config(command=finish)
        ttk.Button(footer, text="Cancel comparison", command=lambda: finish(True)).pack(
            side="right", padx=8)
        window.protocol("WM_DELETE_WINDOW", lambda: finish(True))
        window.grab_set()

    def _show_column_selection(self, request: _ColumnSelectionRequest) -> None:
        window = tk.Toplevel(self)
        self._selection_window = window
        window.title(f"Select columns - {request.item.basename}")
        window.transient(self)
        window.geometry("600x650")
        window.minsize(420, 350)
        ttk.Label(window, text=f"Folder A: {os.path.basename(request.item.paths_a[0])}\n"
                               f"Folder B: {os.path.basename(request.item.paths_b[0])}",
                  padding=10).pack(fill="x")
        ttk.Label(window, text="All columns are selected by default. Only checked columns "
                               "will participate in mismatch comparison.",
                  wraplength=560, padding=(10, 0, 10, 8)).pack(fill="x")
        controls = ttk.Frame(window, padding=(10, 0))
        controls.pack(fill="x")
        variables: list[tk.BooleanVar] = []
        container = ttk.Frame(window, padding=10)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)

        def update_scroll_region(event: tk.Event) -> None:
            if event.widget is content:
                canvas.configure(scrollregion=canvas.bbox("all"))

        content.bind("<Configure>", update_scroll_region)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(content_window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def scroll_columns(event: tk.Event) -> str:
            if getattr(event, "num", None) == 4:
                units = -1
            elif getattr(event, "num", None) == 5:
                units = 1
            else:
                delta = getattr(event, "delta", 0)
                units = -1 if delta > 0 else 1 if delta < 0 else 0
                if abs(delta) >= 120:
                    units *= max(1, abs(delta) // 120)
            if units:
                canvas.yview_scroll(units, "units")
            return "break"

        scroll_targets = [canvas, content]
        for field in request.schema.fields:
            var = tk.BooleanVar(value=True)
            variables.append(var)
            checkbox = ttk.Checkbutton(content, text=f"{field.name}  (cols {field.start}-"
                                                      f"{field.start + field.length - 1}, {field.type})",
                                       variable=var)
            checkbox.pack(anchor="w")
            scroll_targets.append(checkbox)
        for target in scroll_targets:
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                target.bind(sequence, scroll_columns, add="+")
        ttk.Button(controls, text="Select All",
                   command=lambda: [var.set(True) for var in variables]).pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="Unselect All",
                   command=lambda: [var.set(False) for var in variables]).pack(side="left")
        footer = ttk.Frame(window, padding=10)
        footer.pack(fill="x")

        def finish(cancelled: bool = False) -> None:
            if request.completed.is_set():
                return
            request.selected = (None if cancelled else
                                tuple(field.name for field, var in zip(request.schema.fields, variables)
                                      if var.get()))
            if cancelled:
                self.cancel.set()
            request.completed.set()
            self._selection_window = None
            window.destroy()

        ttk.Button(footer, text="Continue / Compare", command=finish).pack(side="right")
        ttk.Button(footer, text="Cancel comparison", command=lambda: finish(True)).pack(side="right", padx=8)
        window.protocol("WM_DELETE_WINDOW", lambda: finish(True))
        window.grab_set()

    def _close(self) -> None:
        self.cancel.set()
        if self._selection_window and self._selection_window.winfo_exists():
            self._selection_window.destroy()
        self.destroy()

    def _show_activity(self) -> None:
        now = time.monotonic()
        elapsed = int(now - self.started)
        prefix = "Cancelling... " if self.cancel.is_set() else ""
        quiet = int(now - self.last_activity)
        waiting = (f" | Last worker update {quiet}s ago"
                   if quiet >= 3 and not self.activity.startswith("Waiting for column selection:") else "")
        self.var_status.set(f"{prefix}{self.file_progress} | Elapsed {elapsed // 60:02d}:{elapsed % 60:02d}"
                            f" | {self.activity}{waiting}")

    def _cancel(self) -> None:
        self.cancel.set()
        self.btn_cancel.config(state="disabled")
        self._show_activity()

    def _reset_buttons(self) -> None:
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.btn_compare.config(state="normal")
        self.btn_scan.config(state="normal")
        self.btn_cancel.config(state="disabled")

    def _finish(self, results) -> None:
        self.results = results
        self.partial = self.cancel.is_set()
        self.btn_cancel.config(state="disabled")
        self.progress.stop()
        self.var_status.set("Loading completed results...")
        if self.scan_items:
            self._display_scan()
        self.notebook.select(0)
        self._display_results(0)

    def _display_scan(self) -> None:
        self.scan_tree.delete(*self.scan_tree.get_children())
        for item in self.scan_items:
            self.scan_tree.insert("", "end", tags=("ready" if item.status == READY else "invalid",),
                                  values=(item.basename, item.status, "; ".join(item.paths_a),
                                          "; ".join(item.paths_b), "; ".join(item.definitions), item.note))

    def _finish_scan(self) -> None:
        self.partial = self.cancel.is_set()
        self._display_scan()
        self.notebook.select(1)
        self._reset_buttons()
        ready = sum(item.status == READY for item in self.scan_items)
        self.var_status.set(f"Validation: {ready:,} Ready; {len(self.scan_items) - ready:,} invalid/missing. "
                            "No files compared or JSON written. Compare will rescan before processing.")
        self.btn_report.config(state="normal" if self.scan_items else "disabled")

    def _display_results(self, start: int) -> None:
        for r in self.results[start:start + 200]:
            fmt = lambda n: "" if n is None else f"{n:,}"
            self.tree.insert("", "end", tags=(r.status,),
                             values=(r.status, r.rel_path, fmt(r.size_a), fmt(r.size_b),
                                     r.format_desc, r.note))
        if start + 200 < len(self.results):
            self.after(1, self._display_results, start + 200)
            return
        self._reset_buttons()
        counts = summarize(self.results)
        for s in STATUSES:
            self.sum_labels[s].config(text=f"{s}: {counts[s]}")
        elapsed = time.monotonic() - self.started
        self.var_status.set(("Cancelled - partial results; unfinished file excluded. " if self.partial else "Done. ")
                            + f"{len(self.results):,} files in {elapsed:.1f}s.")
        self.btn_report.config(state="normal" if self.results or self.scan_items else "disabled")

    def save_report(self) -> None:
        a, b = self.compared_roots
        default_dir = os.path.dirname(a)
        reports_dir = os.path.join(default_dir, "reports")
        initial_dir = default_dir
        if not any(_inside(reports_dir, root) for root in (a, b) + self.definition_roots):
            try:
                os.makedirs(reports_dir, exist_ok=True)
                initial_dir = reports_dir
            except OSError as exc:
                messagebox.showerror("Folder Compare", f"Could not prepare the reports folder:\n{exc}")
                return
        directory = filedialog.askdirectory(
            parent=self, title="Select folder for individual HTML reports", initialdir=initial_dir,
            mustexist=True)
        if not directory:
            return
        if any(_inside(directory, root) for root in (a, b) + self.definition_roots):
            messagebox.showwarning("Folder Compare", "Choose a folder outside Folder A and Folder B "
                                   "and the schema/copybook folders so inputs are not modified.")
            return
        settings = self.opts.describe()
        if self.validation_only:
            settings = "VALIDATION ONLY - no files compared. " + settings
        if self.partial:
            settings = "CANCELLED - PARTIAL RESULTS ONLY. " + settings
        self.btn_compare.config(state="disabled")
        self.btn_scan.config(state="disabled")
        self.btn_report.config(state="disabled")
        self.progress.config(mode="indeterminate")
        self.progress.start(30)
        self.started = time.monotonic()

        def save() -> None:
            try:
                if self.validation_only:
                    path = os.path.join(directory, "folder_compare_validation.html")
                    write_report(path, a, b, self.results, settings, self.scan_items)
                    paths = [path]
                else:
                    paths = []
                    for result, filename in zip(self.results, _report_filenames(self.results)):
                        config = dict(result.comparison_config)
                        if not config:
                            config = self._report_configuration(result)
                        validation = []
                        if self.scan_items:
                            validation = [item for item in self.scan_items
                                          if item.basename.casefold() in
                                          {result.rel_path.casefold(),
                                           os.path.basename(result.rel_path).casefold()}]
                        path = os.path.join(directory, filename)
                        write_report(path, a, b, [result], settings, validation, config)
                        paths.append(path)
                self.queue.put(("report_done", paths))
            except Exception as exc:  # surfaced in the UI thread
                self.queue.put(("report_error", str(exc)))

        self.worker = threading.Thread(target=save, daemon=True)
        self.worker.start()
        self._poll_report()

    def _report_configuration(self, result) -> dict[str, object]:
        path_a = path_b = result.rel_path
        matching = next((item for item in self.scan_items
                         if item.basename.casefold() in
                         {result.rel_path.casefold(), os.path.basename(result.rel_path).casefold()}), None)
        if matching:
            path_a = matching.paths_a[0] if matching.paths_a else "(missing)"
            path_b = matching.paths_b[0] if matching.paths_b else "(missing)"
        elif result.status == ONLY_A:
            path_b = "(missing)"
        elif result.status == ONLY_B:
            path_a = "(missing)"
        return {
            "file_a": os.path.basename(path_a) if path_a != "(missing)" else path_a,
            "file_b": os.path.basename(path_b) if path_b != "(missing)" else path_b,
            "comparison_type": ("Copybook/Column-Based" if self.opts.comparison_type == "copybook"
                                and matching and not matching.normal else "Normal"),
            "key_columns": (self.opts.key_fields if self.opts.key_fields.strip() else
                            f"columns {self.opts.key_start}-{self.opts.key_start + self.opts.key_length - 1}"
                            if self.opts.match == "key" else "Not used"),
            "included_columns": "Not applicable",
            "excluded_columns": "Not applicable",
        }

    def _poll_report(self) -> None:
        try:
            kind, value = self.queue.get_nowait()
        except queue.Empty:
            self.var_status.set(f"Writing HTML report... Elapsed {time.monotonic() - self.started:.0f}s")
            self.after(100, self._poll_report)
            return
        self._reset_buttons()
        self.btn_report.config(state="normal")
        if kind == "report_error":
            self.var_status.set("Report could not be saved.")
            messagebox.showerror("Folder Compare", f"Could not write report:\n{value}")
        else:
            self.var_status.set(f"Saved {len(value)} individual report(s) in: {os.path.dirname(value[0])}"
                                if value else "No comparison reports to save.")
            if value:
                webbrowser.open(f"file:///{value[0].replace(os.sep, '/')}")


def main() -> None:
    App().mainloop()
