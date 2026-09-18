from __future__ import annotations

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
    if kind in {"heartbeat", "worker_heartbeat", "status_snapshot"}:
        return "", "normal"
    if kind.startswith("worker_"):
        return kind.replace("_", " ").capitalize(), "normal"
    return _payload(event, "message", kind.replace("_", " ").capitalize()), "normal"
