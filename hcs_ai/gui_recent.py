from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .gui import api
from .gui_home import App as HomeApp
from . import model_manager, prompt_functions, telemetry


RECENT_TAB_TITLES = ("Models", "Prompt Functions")


def format_telemetry(item: dict | None) -> str:
    if not item:
        return "Model performance: no measurements yet"
    model = Path(str(item.get("model") or "unknown")).name
    gen = item.get("generation_tokens_per_second")
    prompt = item.get("prompt_tokens_per_second")
    parts = [f"Model: {model}"]
    parts.append(f"{float(gen):.1f} tok/s" if gen is not None else "output tok/s: n/a")
    parts.append(f"{float(prompt):.1f} prompt tok/s" if prompt is not None else "prompt tok/s: n/a")
    return " | ".join(parts)


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "?"
    n = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


class App(HomeApp):
    """HCS GUI with model management, performance telemetry, and Prompt Functions."""

    def __init__(self):
        super().__init__()
        self.title("HCS-AI v0.10 — Models + Prompt Functions")
        notebook = next(
            (child for child in self.winfo_children() if isinstance(child, ttk.Notebook)),
            None,
        )
        if notebook is None:
            raise RuntimeError("HCS top-level notebook was not found.")

        self.models_tab = ttk.Frame(notebook)
        self.prompt_functions_tab = ttk.Frame(notebook)
        notebook.add(self.models_tab, text=RECENT_TAB_TITLES[0])
        notebook.add(self.prompt_functions_tab, text=RECENT_TAB_TITLES[1])
        self._build_models_tab()
        self._build_prompt_functions_tab()

        self.performance_status = ttk.Label(self.chat_tab, text=format_telemetry(telemetry.latest()))
        self.performance_status.pack(anchor="w", padx=8, pady=(0, 8))
        self.after(1200, self._refresh_performance_label)

    def send(self):
        msg = self.prompt.get().strip()
        if not msg:
            return
        self.prompt.delete(0, "end")
        self.append_chat("You", msg)
        self.status.config(text="Generating...")
        # Process geometry/paint work without allowing nested Tk callbacks during a request.
        self.update_idletasks()
        try:
            out = api(
                "POST", "/chat",
                {"message": msg, "history": self.history[-12:], "use_kb": self.use_kb.get()},
            )
            text = out.get("text", "")
            self.append_chat("HCS-AI", text)
            self.history += [
                {"role": "user", "content": msg},
                {"role": "assistant", "content": text},
            ]
            if out.get("tool_results"):
                self.append_chat("Tools", json.dumps(out["tool_results"], indent=2)[:5000])
            self._refresh_performance_label()
            self.status.config(text="Connected")
        except Exception as exc:
            self.append_chat("Error", str(exc))
            self.status.config(text="Request failed")

    def _refresh_performance_label(self):
        if hasattr(self, "performance_status"):
            try:
                self.performance_status.config(text=format_telemetry(telemetry.latest()))
            except Exception as exc:
                self.performance_status.config(text=f"Model performance unavailable: {exc}")

    def _build_models_tab(self):
        discover = ttk.LabelFrame(self.models_tab, text="Discover GGUF models — Hugging Face")
        discover.pack(fill="both", expand=True, padx=8, pady=8)
        bar = ttk.Frame(discover)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Label(bar, text="Search:").pack(side="left")
        self.model_search_query = tk.StringVar(value="Qwen3 GGUF")
        entry = ttk.Entry(bar, textvariable=self.model_search_query)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self.search_models())
        ttk.Button(bar, text="Search Internet", command=self.search_models).pack(side="left")
        ttk.Button(bar, text="Download Selected", command=self.download_selected_model).pack(side="left", padx=(6, 0))

        cols = ("repo", "file", "quant", "size", "downloads")
        self.model_search_tree = ttk.Treeview(discover, columns=cols, show="headings", height=8)
        for col, title, width in (
            ("repo", "Repository", 230), ("file", "GGUF file", 330),
            ("quant", "Quant", 90), ("size", "Size", 90), ("downloads", "Downloads", 90),
        ):
            self.model_search_tree.heading(col, text=title)
            self.model_search_tree.column(col, width=width)
        self.model_search_tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.model_search_results: dict[str, dict] = {}

        local = ttk.LabelFrame(self.models_tab, text="Downloaded models")
        local.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        toolbar = ttk.Frame(local)
        toolbar.pack(fill="x", padx=6, pady=6)
        ttk.Button(toolbar, text="Refresh", command=self.refresh_local_models).pack(side="left")
        ttk.Button(toolbar, text="Import GGUF", command=self.import_model).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Load / Set Default", command=self.load_selected_model).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Unload", command=lambda: self._model_engine_action("stop")).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Delete", command=self.delete_selected_model).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Open Model Folder", command=self.open_models_folder).pack(side="left", padx=4)
        self.model_status = ttk.Label(toolbar, text="")
        self.model_status.pack(side="right")

        local_cols = ("active", "name", "quant", "size", "gen", "prompt", "fit")
        self.local_model_tree = ttk.Treeview(local, columns=local_cols, show="headings", height=8)
        for col, title, width in (
            ("active", "Active", 55), ("name", "Model", 300), ("quant", "Quant", 80),
            ("size", "Size", 85), ("gen", "Output tok/s", 95), ("prompt", "Prompt tok/s", 100),
            ("fit", "RAM", 80),
        ):
            self.local_model_tree.heading(col, text=title)
            self.local_model_tree.column(col, width=width)
        self.local_model_tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.local_model_records: dict[str, dict] = {}
        self.after(900, self.refresh_local_models)

    def search_models(self):
        query = self.model_search_query.get().strip()
        if not query:
            return
        self.model_status.config(text="Searching...")
        def work():
            try:
                rows = model_manager.search_huggingface(query, limit=20)
                self.after(0, lambda: self._show_model_search(rows))
            except Exception as exc:
                self.after(0, lambda m=str(exc): messagebox.showerror("Model search", m))
                self.after(0, lambda: self.model_status.config(text="Search failed"))
        threading.Thread(target=work, daemon=True).start()

    def _show_model_search(self, rows):
        self.model_search_tree.delete(*self.model_search_tree.get_children())
        self.model_search_results = {}
        for index, item in enumerate(rows):
            iid = f"m{index}"
            self.model_search_results[iid] = item
            self.model_search_tree.insert(
                "", "end", iid=iid,
                values=(
                    item.get("repo_id", ""), item.get("filename", ""), item.get("quantization") or "",
                    _human_bytes(item.get("size_bytes")), item.get("downloads") or "",
                ),
            )
        self.model_status.config(text=f"{len(rows)} GGUF file(s) found")

    def download_selected_model(self):
        selected = self.model_search_tree.selection()
        if not selected:
            messagebox.showinfo("Models", "Select a GGUF file to download.")
            return
        item = self.model_search_results[selected[0]]
        self.model_status.config(text="Downloading...")
        def work():
            try:
                result = model_manager.download_model(item["repo_id"], item["filename"])
                self.after(0, self.refresh_local_models)
                self.after(0, lambda: self.model_status.config(text=f"Downloaded {Path(result['path']).name}"))
            except Exception as exc:
                self.after(0, lambda m=str(exc): messagebox.showerror("Model download", m))
                self.after(0, lambda: self.model_status.config(text="Download failed"))
        threading.Thread(target=work, daemon=True).start()

    def refresh_local_models(self):
        try:
            rows = model_manager.list_local_models()
            self.local_model_tree.delete(*self.local_model_tree.get_children())
            self.local_model_records = {}
            for index, item in enumerate(rows):
                iid = f"l{index}"
                self.local_model_records[iid] = item
                perf = item.get("performance") or {}
                compat = item.get("compatibility") or {}
                self.local_model_tree.insert(
                    "", "end", iid=iid,
                    values=(
                        "YES" if item.get("active") else "", item.get("name", ""),
                        item.get("quantization") or "", _human_bytes(item.get("size_bytes")),
                        f"{perf['avg_generation_tps']:.1f}" if perf.get("avg_generation_tps") is not None else "",
                        f"{perf['avg_prompt_tps']:.1f}" if perf.get("avg_prompt_tps") is not None else "",
                        "OK" if compat.get("likely_fits_total_ram") else "Large",
                    ),
                )
            self.model_status.config(text=f"{len(rows)} local model(s)")
        except Exception as exc:
            self.model_status.config(text=f"Local models unavailable: {exc}")

    def _selected_local_model(self):
        selected = self.local_model_tree.selection()
        return self.local_model_records.get(selected[0]) if selected else None

    def import_model(self):
        path = filedialog.askopenfilename(title="Import GGUF model", filetypes=[("GGUF", "*.gguf")])
        if not path:
            return
        try:
            model_manager.import_local_model(path)
            self.refresh_local_models()
        except Exception as exc:
            messagebox.showerror("Import model", str(exc))

    def load_selected_model(self):
        item = self._selected_local_model()
        if not item:
            messagebox.showinfo("Models", "Select a downloaded model first.")
            return
        try:
            api("POST", "/inference/config", {"model_path": item["path"], "auto_start": True}, timeout=180)
            self.after(700, self.refresh_local_models)
            self.after(700, self._refresh_performance_label)
        except Exception as exc:
            messagebox.showerror("Load model", str(exc))

    def _model_engine_action(self, action):
        try:
            api("POST", f"/inference/{action}", {})
            self.after(500, self.refresh_local_models)
        except Exception as exc:
            messagebox.showerror("Model engine", str(exc))

    def delete_selected_model(self):
        item = self._selected_local_model()
        if not item:
            return
        if not messagebox.askyesno("Delete model", f"Delete {item['name']} from HCS?"):
            return
        try:
            model_manager.delete_model(item["path"])
            self.refresh_local_models()
        except Exception as exc:
            messagebox.showerror("Delete model", str(exc))

    def open_models_folder(self):
        path = model_manager.models_dir()
        try:
            os.startfile(str(path))
        except AttributeError:
            messagebox.showinfo("Models folder", str(path))

    def _build_prompt_functions_tab(self):
        top = ttk.Frame(self.prompt_functions_tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Function:").pack(side="left")
        self.prompt_function_name = tk.StringVar(value="example")
        ttk.Entry(top, textvariable=self.prompt_function_name, width=28).pack(side="left", padx=6)
        ttk.Button(top, text="New", command=self.new_prompt_function).pack(side="left")
        ttk.Button(top, text="Save", command=self.save_prompt_function).pack(side="left", padx=4)
        ttk.Button(top, text="Refresh Library", command=self.refresh_prompt_functions).pack(side="left", padx=4)
        ttk.Button(top, text="History", command=self.show_prompt_function_history).pack(side="left", padx=4)
        ttk.Button(top, text="Run", command=self.run_prompt_function).pack(side="right")

        pane = ttk.Panedwindow(self.prompt_functions_tab, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        library = ttk.Frame(pane)
        editor = ttk.Frame(pane)
        pane.add(library, weight=1)
        pane.add(editor, weight=4)

        self.prompt_function_tree = ttk.Treeview(library, columns=("name", "version"), show="headings")
        self.prompt_function_tree.heading("name", text="Name")
        self.prompt_function_tree.heading("version", text="Version")
        self.prompt_function_tree.column("name", width=170)
        self.prompt_function_tree.column("version", width=55)
        self.prompt_function_tree.pack(fill="both", expand=True)
        self.prompt_function_tree.bind("<Double-1>", lambda _e: self.load_selected_prompt_function())
        self.prompt_function_records = {}

        ttk.Label(editor, text="Prompt Function source (.hpf) — safe Python-like DSL").pack(anchor="w")
        self.prompt_function_source = tk.Text(editor, wrap="none", undo=True, height=18)
        self.prompt_function_source.pack(fill="both", expand=True, pady=(4, 6))
        self.prompt_function_source.tag_configure("keyword", foreground="#7a3e9d")
        self.prompt_function_source.bind("<KeyRelease>", lambda _e: self._highlight_prompt_source())

        inputs = ttk.LabelFrame(editor, text="Test inputs — JSON object")
        inputs.pack(fill="x")
        self.prompt_function_inputs = tk.Text(inputs, height=4, wrap="word")
        self.prompt_function_inputs.pack(fill="x", padx=6, pady=6)
        self.prompt_function_inputs.insert("1.0", '{"text": "Explain why cover crops improve soil."}')

        output = ttk.LabelFrame(editor, text="Result + execution trace")
        output.pack(fill="both", expand=True, pady=(6, 0))
        self.prompt_function_output = tk.Text(output, height=10, wrap="word", state="disabled")
        self.prompt_function_output.pack(fill="both", expand=True, padx=6, pady=6)

        self.new_prompt_function()
        self.after(1000, self.refresh_prompt_functions)

    def new_prompt_function(self):
        source = '''def main(text):\n    summary = ask("Summarize this accurately and concisely", text)\n    if len(summary) > 1200:\n        return ask("Shorten this while preserving the important facts", summary)\n    return summary\n'''
        self.prompt_function_source.delete("1.0", "end")
        self.prompt_function_source.insert("1.0", source)
        self._highlight_prompt_source()

    def _highlight_prompt_source(self):
        text = self.prompt_function_source
        text.tag_remove("keyword", "1.0", "end")
        import re
        source = text.get("1.0", "end-1c")
        for match in re.finditer(r"\b(def|return|if|else|True|False|None)\b", source):
            start = f"1.0+{match.start()}c"
            end = f"1.0+{match.end()}c"
            text.tag_add("keyword", start, end)

    def refresh_prompt_functions(self):
        try:
            rows = prompt_functions.list_scripts()
            self.prompt_function_tree.delete(*self.prompt_function_tree.get_children())
            self.prompt_function_records = {}
            for row in rows:
                iid = str(row["id"])
                self.prompt_function_records[iid] = row
                self.prompt_function_tree.insert("", "end", iid=iid, values=(row["name"], row["version"]))
        except Exception as exc:
            messagebox.showerror("Prompt Functions", str(exc))

    def load_selected_prompt_function(self):
        selected = self.prompt_function_tree.selection()
        if not selected:
            return
        row = self.prompt_function_records[selected[0]]
        try:
            script = prompt_functions.get_script(row["name"])
            self.prompt_function_name.set(script["name"])
            self.prompt_function_source.delete("1.0", "end")
            self.prompt_function_source.insert("1.0", script["source"])
            self._highlight_prompt_source()
        except Exception as exc:
            messagebox.showerror("Prompt Functions", str(exc))

    def save_prompt_function(self):
        try:
            out = prompt_functions.save_script(
                self.prompt_function_name.get(), self.prompt_function_source.get("1.0", "end-1c")
            )
            self.refresh_prompt_functions()
            self._set_prompt_output({"saved": out["name"], "version": out["version"]})
        except Exception as exc:
            messagebox.showerror("Save Prompt Function", str(exc))

    def run_prompt_function(self):
        try:
            inputs = json.loads(self.prompt_function_inputs.get("1.0", "end-1c") or "{}")
            if not isinstance(inputs, dict):
                raise ValueError("Test inputs must be a JSON object.")
        except Exception as exc:
            messagebox.showerror("Prompt Function inputs", str(exc))
            return
        source = self.prompt_function_source.get("1.0", "end-1c")
        self._set_prompt_output({"status": "Running..."})
        def work():
            try:
                out = prompt_functions.run_script(source, inputs)
                self.after(0, lambda: self._set_prompt_output(out))
                self.after(0, self._refresh_performance_label)
            except Exception as exc:
                self.after(0, lambda m=str(exc): self._set_prompt_output({"error": m}))
        threading.Thread(target=work, daemon=True).start()

    def show_prompt_function_history(self):
        name = self.prompt_function_name.get().strip()
        if not name:
            return
        try:
            rows = prompt_functions.history(name)
            self._set_prompt_output({"name": name, "revisions": rows})
        except Exception as exc:
            messagebox.showerror("Prompt Function history", str(exc))

    def _set_prompt_output(self, value):
        self.prompt_function_output.config(state="normal")
        self.prompt_function_output.delete("1.0", "end")
        self.prompt_function_output.insert("1.0", json.dumps(value, indent=2, default=str))
        self.prompt_function_output.config(state="disabled")
