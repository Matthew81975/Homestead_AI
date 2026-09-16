"""Tkinter control surface for the HCS-owned Local Codex service."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Iterable


LOCAL_CODEX_TAB_TITLE = "Local Codex"
REQUIRED_CONTROL_NAMES = {
    "workspace", "refresh", "manage", "prompt", "new_task", "resume", "stop",
    "approve", "deny", "follow", "pause_scroll", "search", "clear_view", "open_log_folder",
}


def ordered_local_codex_events(events: Iterable):
    return sorted(events, key=lambda event: event.sequence)


def local_codex_search_ranges(text: str, query: str):
    needle = str(query or "").casefold()
    if not needle:
        return []
    haystack = str(text or "").casefold()
    ranges = []
    start = 0
    while (found := haystack.find(needle, start)) >= 0:
        ranges.append((found, found + len(needle)))
        start = found + len(needle)
    return ranges


def should_follow_local_codex_log(follow: bool, paused: bool):
    return bool(follow and not paused)


def local_codex_control_states(snapshot):
    worker = snapshot.get("worker_state", "stopped")
    task = snapshot.get("task_state", snapshot.get("state", "idle"))
    ready = worker == "running"
    active = bool(snapshot.get("active")) or task in {"working", "awaiting_approval"}
    approval = ready and task == "awaiting_approval" and bool(snapshot.get("request_id"))
    return {
        "new_task": ready and not active,
        "resume": ready and not active,
        "stop": ready and active,
        "approve": approval,
        "deny": approval,
    }


def _payload(event, key, default=""):
    value = event.payload.get(key, default)
    return str(value if value is not None else default)


def format_local_codex_event(event):
    """Return concise human-readable log text and a restrained Tk tag."""
    kind = event.type
    payload = event.payload
    if kind == "provider_fallback":
        return (
            f"Provider fallback: {_payload(event, 'from_provider', 'unknown')} → "
            f"{_payload(event, 'to_provider', 'unknown')}",
            "provider",
        )
    if kind == "provider_selected":
        return f"Provider selected: {_payload(event, 'provider', 'unknown')}", "provider"
    if kind == "provider_recovered":
        return f"Provider recovered: {_payload(event, 'provider', 'unknown')}", "provider"
    if kind == "provider_wait":
        return f"Provider wait: {_payload(event, 'message', _payload(event, 'provider', 'capacity'))}", "warning"
    if kind in {"task_started", "task_resumed"}:
        verb = "started" if kind == "task_started" else "resumed"
        return f"Task {verb}: {_payload(event, 'task_id', 'pending')}", "action"
    if kind == "task_stopping":
        return "Stopping task…", "warning"
    if kind == "task_completed":
        return f"Task completed: {_payload(event, 'status', 'done')}", "success"
    if kind in {"task_failed", "task_blocked"}:
        label = "failed" if kind == "task_failed" else "blocked"
        return f"Task {label}: {_payload(event, 'message', _payload(event, 'status', label))}", "error"
    if kind == "approval_required":
        summary = _payload(event, "summary", _payload(event, "message", "Review required"))
        return f"Approval required: {summary}", "warning"
    if kind == "approval_resolved":
        decision = "approved" if payload.get("approved") else "denied"
        return f"Approval {decision}", "success" if payload.get("approved") else "warning"
    if kind == "action_started":
        return f"Action: {_payload(event, 'action', _payload(event, 'message', 'started'))}", "action"
    if kind in {"action_result", "test_result"}:
        ok = payload.get("success", payload.get("passed", True))
        message = _payload(event, "message", _payload(event, "status", kind.replace("_", " ")))
        return message, "success" if ok else "error"
    if kind in {"worker_crashed", "command_error"}:
        return _payload(event, "message", _payload(event, "code", kind.replace("_", " "))), "error"
    if kind in {"log_warning", "protocol_warning"}:
        return _payload(event, "message", _payload(event, "code", "Local Codex warning")), "warning"
    if kind == "log":
        tag = {"success": "success", "warning": "warning", "error": "error", "critical": "error"}.get(
            event.level, "normal"
        )
        return _payload(event, "message", str(dict(payload))), tag
    if kind.startswith("worker_"):
        return kind.replace("_", " ").capitalize(), "normal"
    if kind in {"heartbeat", "status_snapshot"}:
        return "", "normal"
    return _payload(event, "message", kind.replace("_", " ").capitalize()), "normal"


class LocalCodexGuiMixin:
    """Behavior-only mixin; all worker communication goes through the service."""

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

    def _local_codex_service(self):
        return getattr(self, "local_codex_service", None)

    def _refresh_local_codex_workspaces(self):
        service = self._local_codex_service()
        if service is None:
            return
        service.refresh_workspaces()
        self.after(100, self._sync_local_codex_workspaces)

    def _sync_local_codex_workspaces(self):
        service = self._local_codex_service()
        if service is None:
            return
        workspaces = service.list_workspaces()
        labels = []
        mapping = {}
        for workspace in workspaces:
            if isinstance(workspace, dict):
                name = workspace.get("name") or "Workspace"
                workspace_id = workspace.get("workspace_id")
            else:
                name = getattr(workspace, "name", None) or "Workspace"
                workspace_id = getattr(workspace, "workspace_id", None)
            if not workspace_id:
                continue
            label = f"{name} ({workspace_id})"
            labels.append(label)
            mapping[label] = workspace_id
        self._local_codex_workspace_ids = mapping
        self.local_codex_workspace.configure(values=labels)
        if labels and not self.local_codex_workspace.get():
            self.local_codex_workspace.set(labels[0])

    def _selected_local_codex_workspace(self):
        return self._local_codex_workspace_ids.get(self.local_codex_workspace.get())

    def _show_local_codex_result(self, result):
        if result is not None and not result.accepted:
            messagebox.showwarning("Local Codex", result.code.replace("_", " ").capitalize())

    def _submit_local_codex_task(self):
        service = self._local_codex_service()
        workspace_id = self._selected_local_codex_workspace()
        prompt = self.local_codex_prompt.get("1.0", "end-1c").strip()
        if service is None or not workspace_id or not prompt:
            messagebox.showinfo("Local Codex", "Select a workspace and enter a task prompt.")
            return
        self._show_local_codex_result(service.submit_task(workspace_id, prompt))

    def _resume_local_codex_task(self):
        service = self._local_codex_service()
        workspace_id = self._selected_local_codex_workspace()
        if service is not None and workspace_id:
            self._show_local_codex_result(service.resume_task(workspace_id))

    def _stop_local_codex_task(self):
        service = self._local_codex_service()
        if service is not None:
            self._show_local_codex_result(service.stop_task())

    def _resolve_local_codex_approval(self, approved):
        service = self._local_codex_service()
        request_id = service.status().get("request_id") if service is not None else None
        if request_id:
            self._show_local_codex_result(service.approve(request_id, approved))

    def _update_local_codex_controls(self, snapshot=None):
        service = self._local_codex_service()
        snapshot = snapshot or (service.status() if service is not None else {})
        states = local_codex_control_states(snapshot)
        for name in ("new_task", "resume", "stop", "approve", "deny"):
            widget = getattr(self, f"local_codex_{name}")
            widget.configure(state="normal" if states[name] else "disabled")
        worker = snapshot.get("worker_state", "unavailable")
        task = snapshot.get("task_state", snapshot.get("state", "idle"))
        self.local_codex_worker_status.set(f"Worker: {worker}")
        self.local_codex_task_status.set(f"Task: {task}")
        self.local_codex_provider_status.set(f"Provider: {snapshot.get('provider') or '—'}")
        request_id = snapshot.get("request_id") if task == "awaiting_approval" else None
        self.local_codex_approval_text.configure(
            text=f"Approval waiting: {request_id}" if request_id else "No approval waiting"
        )

    def _poll_local_codex_events(self):
        service = self._local_codex_service()
        if service is not None:
            for event in ordered_local_codex_events(service.poll_events()):
                if event.sequence <= self._local_codex_last_sequence:
                    continue
                self._local_codex_last_sequence = event.sequence
                text, tag = format_local_codex_event(event)
                if text:
                    self._append_local_codex_log(text, tag)
            self._update_local_codex_controls(service.status())
            self._sync_local_codex_workspaces()
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

    def _manage_local_codex_workspaces(self):
        service = self._local_codex_service()
        if service is not None:
            self._open_local_path(Path(service.workspace_registry_path).parent)

    def _open_local_codex_log_folder(self):
        service = self._local_codex_service()
        if service is not None:
            self._open_local_path(Path(service.status()["log_dir"]))

    @staticmethod
    def _open_local_path(path):
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(path))
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Local Codex", str(exc))
