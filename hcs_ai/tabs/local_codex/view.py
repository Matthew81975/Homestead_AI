from __future__ import annotations

import os
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk

from .controller import LocalCodexControllerMixin
from .presentation import should_follow_local_codex_log


class LocalCodexViewMixin:
    """Tk view construction and display-only behavior for the Local Codex tab."""

    def build_local_codex(self):
        self._local_codex_workspace_ids = {}
        self._local_codex_last_sequence = 0
        self.local_codex_follow = tk.BooleanVar(value=True)
        self.local_codex_pause = tk.BooleanVar(value=False)
        self.local_codex_worker_status = tk.StringVar(value="Worker: unavailable")
        self.local_codex_task_status = tk.StringVar(value="Task: idle")
        self.local_codex_provider_status = tk.StringVar(value="Provider: —")

        workspace_row = ttk.Frame(self.local_codex_tab)
        workspace_row.pack(fill="x", padx=10, pady=(10, 5))
        ttk.Label(workspace_row, text="Workspace:").pack(side="left")
        self.local_codex_workspace = ttk.Combobox(workspace_row, state="readonly", width=34)
        self.local_codex_workspace.pack(side="left", fill="x", expand=True, padx=6)
        self.local_codex_refresh = ttk.Button(
            workspace_row, text="Refresh", command=self._refresh_local_codex_workspaces
        )
        self.local_codex_refresh.pack(side="left")
        self.local_codex_manage = ttk.Button(
            workspace_row, text="Manage…", command=self._manage_local_codex_workspaces
        )
        self.local_codex_manage.pack(side="left", padx=(6, 0))

        prompt_box = ttk.LabelFrame(self.local_codex_tab, text="Task prompt")
        prompt_box.pack(fill="x", padx=10, pady=5)
        self.local_codex_prompt = tk.Text(prompt_box, height=5, wrap="word", undo=True)
        self.local_codex_prompt.pack(fill="x", padx=6, pady=6)

        actions = ttk.Frame(self.local_codex_tab)
        actions.pack(fill="x", padx=10, pady=5)
        self.local_codex_new_task = ttk.Button(actions, text="New Task", command=self._submit_local_codex_task)
        self.local_codex_resume = ttk.Button(actions, text="Resume", command=self._resume_local_codex_task)
        self.local_codex_stop = ttk.Button(actions, text="Stop", command=self._stop_local_codex_task)
        for widget in (self.local_codex_new_task, self.local_codex_resume, self.local_codex_stop):
            widget.pack(side="left", padx=(0, 6))
        for variable in (
            self.local_codex_worker_status, self.local_codex_task_status, self.local_codex_provider_status
        ):
            ttk.Label(actions, textvariable=variable).pack(side="left", padx=(12, 0))

        self.local_codex_approval_strip = ttk.Frame(self.local_codex_tab)
        self.local_codex_approval_strip.pack(fill="x", padx=10, pady=(0, 5))
        self.local_codex_approval_text = ttk.Label(
            self.local_codex_approval_strip, text="No approval waiting"
        )
        self.local_codex_approval_text.pack(side="left", fill="x", expand=True)
        self.local_codex_approve = ttk.Button(
            self.local_codex_approval_strip, text="Approve", command=lambda: self._resolve_local_codex_approval(True)
        )
        self.local_codex_deny = ttk.Button(
            self.local_codex_approval_strip, text="Deny", command=lambda: self._resolve_local_codex_approval(False)
        )
        self.local_codex_approve.pack(side="right")
        self.local_codex_deny.pack(side="right", padx=6)

        toolbar = ttk.Frame(self.local_codex_tab)
        toolbar.pack(fill="x", padx=10, pady=(0, 4))
        self.local_codex_follow_control = ttk.Checkbutton(
            toolbar, text="Follow", variable=self.local_codex_follow
        )
        self.local_codex_follow_control.pack(side="left")
        self.local_codex_pause_scroll = ttk.Checkbutton(
            toolbar, text="Pause Scroll", variable=self.local_codex_pause
        )
        self.local_codex_pause_scroll.pack(side="left", padx=6)
        ttk.Label(toolbar, text="Search:").pack(side="left", padx=(12, 4))
        self.local_codex_search = ttk.Entry(toolbar, width=24)
        self.local_codex_search.pack(side="left", fill="x", expand=True)
        self.local_codex_search.bind("<KeyRelease>", self._highlight_local_codex_search)
        self.local_codex_clear_view = ttk.Button(
            toolbar, text="Clear View", command=self._clear_local_codex_log
        )
        self.local_codex_clear_view.pack(side="left", padx=6)
        self.local_codex_open_log_folder = ttk.Button(
            toolbar, text="Open Log Folder", command=self._open_local_codex_log_folder
        )
        self.local_codex_open_log_folder.pack(side="left")

        log_frame = ttk.Frame(self.local_codex_tab)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.local_codex_log = tk.Text(log_frame, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.local_codex_log.yview)
        self.local_codex_log.configure(yscrollcommand=scroll.set)
        self.local_codex_log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        colors = {
            "normal": {}, "action": {"foreground": "#4f86c6"},
            "success": {"foreground": "#238636"}, "warning": {"foreground": "#9a6700"},
            "provider": {"foreground": "#8250df"}, "error": {"foreground": "#cf222e"},
            "search_match": {"background": "#fff59d", "foreground": "#111111"},
        }
        for tag, options in colors.items():
            self.local_codex_log.tag_configure(tag, **options)

        self._refresh_local_codex_workspaces()
        self._update_local_codex_controls()
        self.after(100, self._poll_local_codex_events)

    def _append_local_codex_log(self, text, tag="normal"):
        self.local_codex_log.configure(state="normal")
        self.local_codex_log.insert("end", str(text).rstrip() + "\n", tag)
        self.local_codex_log.configure(state="disabled")
        self._highlight_local_codex_search()
        if should_follow_local_codex_log(self.local_codex_follow.get(), self.local_codex_pause.get()):
            self.local_codex_log.see("end")

    def _highlight_local_codex_search(self, _event=None):
        query = self.local_codex_search.get()
        self.local_codex_log.tag_remove("search_match", "1.0", "end")
        if not query:
            return
        start = "1.0"
        while True:
            found = self.local_codex_log.search(query, start, stopindex="end", nocase=True)
            if not found:
                return
            finish = f"{found}+{len(query)}c"
            self.local_codex_log.tag_add("search_match", found, finish)
            start = finish

    def _clear_local_codex_log(self):
        self.local_codex_log.configure(state="normal")
        self.local_codex_log.delete("1.0", "end")
        self.local_codex_log.configure(state="disabled")

    @staticmethod
    def _show_local_codex_warning(message):
        messagebox.showwarning("Local Codex", message)

    @staticmethod
    def _show_local_codex_info(message):
        messagebox.showinfo("Local Codex", message)

    @staticmethod
    def _open_local_codex_path(path):
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(path))
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Local Codex", str(exc))


class LocalCodexGuiMixin(LocalCodexViewMixin, LocalCodexControllerMixin):
    """Compatibility composition for the HCS GUI inheritance stack."""
