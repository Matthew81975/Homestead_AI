from __future__ import annotations

from pathlib import Path

from .presentation import (
    format_local_codex_event,
    local_codex_control_states,
    ordered_local_codex_events,
)


class LocalCodexControllerMixin:
    """Local Codex service coordination without Tk widget construction."""

    def _local_codex_service(self):
        services = getattr(self, "services", None)
        if services is not None:
            service = services.get("local_codex")
            if service is not None:
                return service
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
            self._show_local_codex_warning(result.code.replace("_", " ").capitalize())

    def _submit_local_codex_task(self):
        service = self._local_codex_service()
        workspace_id = self._selected_local_codex_workspace()
        prompt = self.local_codex_prompt.get("1.0", "end-1c").strip()
        if service is None or not workspace_id or not prompt:
            self._show_local_codex_info("Select a workspace and enter a task prompt.")
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

    def _manage_local_codex_workspaces(self):
        service = self._local_codex_service()
        if service is not None:
            self._open_local_codex_path(Path(service.workspace_registry_path).parent)

    def _open_local_codex_log_folder(self):
        service = self._local_codex_service()
        if service is not None:
            self._open_local_codex_path(Path(service.status()["log_dir"]))
