"""Headless, single-task Local Codex worker with a versioned NDJSON boundary.

``data_root`` is the Local Codex directory itself, normally
``ROOT/data/local_codex`` (not the parent ``ROOT/data``).
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import os
from pathlib import Path
import re
import sys
import threading
import time
from uuid import uuid4

from . import runtime
from .protocol import Command, EventFactory, EventLog, ProtocolError
from .state import AgentStatus, TaskJournal
from .workspace_registry import WorkspaceRegistry


_TRANSITIONS = {
    "idle": {"working"},
    "working": {"awaiting_approval", "stopping", "completed", "blocked", "failed"},
    "awaiting_approval": {"working", "stopping", "blocked", "failed"},
    "stopping": {"blocked", "failed"},
    "completed": {"working"},
    "blocked": {"working"},
    "failed": {"working"},
}
_SECRET_KEY = re.compile(r"authorization|api[_-]?key|secret|password|credential|token", re.I)


class _TestRuntime:
    """Deterministic subprocess seam used only with the explicit CLI test flag."""

    @staticmethod
    def run_task(*_args, status_callback, **_kwargs):
        status_callback("Test runtime completed")
        return AgentStatus.DONE


def _configured_secrets(config):
    """Collect configured literal credentials and referenced environment values."""
    names = set(config.get("secret_names", ())) | set(config.get("credential_names", ()))
    values = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"secret_names", "credential_names"} and isinstance(item, list):
                    names.update(name for name in item if isinstance(name, str))
                if key == "required_envs" and isinstance(item, (list, tuple)):
                    values.update(os.environ[name] for name in item if isinstance(name, str) and os.environ.get(name))
                elif key.endswith("_env") and isinstance(item, str):
                    if os.environ.get(item):
                        values.add(os.environ[item])
                elif (_SECRET_KEY.search(key) or key in names) and isinstance(item, str) and item:
                    values.add(item)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(config)
    # Explicit names can reference environment credentials as well as JSON keys.
    values.update(os.environ[name] for name in names if isinstance(name, str) and os.environ.get(name))
    return tuple(names), tuple(values)


class WorkerRuntime:
    def __init__(self, input_stream, output_stream, *, data_root,
                 runtime_factory=lambda: runtime, clock=time.time, heartbeat_seconds=5.0):
        from hcs_ai.config import load_config

        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.data_root = Path(data_root)
        self.runtime_factory = runtime_factory
        self.heartbeat_seconds = heartbeat_seconds
        self.config = deepcopy(load_config().get("local_codex", {}))
        names, values = _configured_secrets(self.config)
        self._events = EventFactory(clock=clock, secret_names=names, secret_values=values)
        log_config = self.config.get("logs", {})
        self._log = EventLog(self.data_root / "logs" / "local-codex.log",
                             max_bytes=log_config.get("max_bytes", 2_000_000),
                             backups=log_config.get("backups", 5))
        self._output_lock = threading.Lock()
        self._lock = threading.RLock()
        self._approval_condition = threading.Condition(self._lock)
        self._stopped = threading.Event()
        self._shutdown_requested = False
        self._active = False
        self.state = "idle"
        self.workspace_id = None
        self.task_id = None
        self.journal = None
        self.task_thread = None
        self.cancel_event = threading.Event()
        self._request_id = None
        self._approval_answer = None

    def emit(self, kind, *, level="info", payload=None):
        """Allocate, persist, and publish under one lock; retain lifecycle on disk failure."""
        with self._output_lock:
            event = self._events.emit(kind, level, payload)
            persisted = self._log.append(event)
            self.output_stream.write(event.to_json() + "\n")
            if persisted.type == "log_warning" and event.type != "log_warning":
                warning = self._events.emit("log_warning", "warning", persisted.payload)
                self.output_stream.write(warning.to_json() + "\n")
            self.output_stream.flush()
            return event

    def _transition(self, state):
        if state not in _TRANSITIONS[self.state]:
            raise ValueError(f"invalid task state transition: {self.state} -> {state}")
        self.state = state

    def _snapshot(self):
        return {"state": self.state, "workspace_id": self.workspace_id,
                "task_id": self.task_id, "request_id": self._request_id}

    def _error(self, code, command_id=None, message=None):
        self.emit("command_error", level="error", payload={
            "code": code, "command_id": command_id,
            "message": message or code.replace("_", " "),
        })

    def handle(self, command):
        """Handle a validated command without waiting for a task/model call."""
        with self._lock:
            if not isinstance(command, Command):
                self._error("invalid_command")
                return
            if command.type == "status":
                self.emit("status_snapshot", payload={**self._snapshot(), "command_id": command.command_id})
            elif command.type == "shutdown":
                self._shutdown(command.command_id)
            elif self._shutdown_requested:
                self._error("shutting_down", command.command_id)
            elif command.type in {"submit_task", "resume_task"}:
                if self._active:
                    self._error("busy", command.command_id)
                    return
                self._start(command)
            elif command.type == "stop_task":
                if not self._active:
                    self._error("no_active_task", command.command_id)
                else:
                    self._stop(command.command_id)
            elif command.type == "approve":
                self._approve(command)

    def _start(self, command):
        registry_path = self.data_root / "workspaces" / "workspaces.json"
        try:
            workspace = WorkspaceRegistry.load(registry_path).get(command.payload["workspace_id"])
        except (KeyError, FileNotFoundError):
            self._error("workspace_not_found", command.command_id)
            return
        except Exception:
            self._error("workspace_unavailable", command.command_id)
            return
        try:
            config = deepcopy(self.config)
            config["workspace"] = workspace.path
            config.setdefault("model", "local-model")
            config.setdefault("lm_studio_url", "http://127.0.0.1:1234/v1")
            config.setdefault("max_failed_actions", 3)
            tornado = config.setdefault("tornado", {})
            tornado["state_path"] = str(self.data_root / "tornado" / "tornado_state.json")
            tornado["log_path"] = str(self.data_root / "logs" / "tornado.log")
            journal_root = self.data_root / "tasks" / "journals"
            if command.type == "resume_task":
                journal = self._resumable_journal(journal_root, workspace.path)
                if journal is None:
                    self._error("no_resumable_task", command.command_id)
                    return
                journal.status = AgentStatus.WORKING
                journal.save()
            else:
                journal = TaskJournal.new(command.payload["prompt"], workspace.path, config["model"], journal_root)
        except Exception:
            self._error("task_setup_failed", command.command_id)
            return
        self.journal = journal
        self.workspace_id = workspace.workspace_id
        self.task_id = journal.path.stem
        self.cancel_event = threading.Event()
        self._request_id = None
        self._approval_answer = None
        self._active = True
        self._transition("working")
        self.emit("task_resumed" if command.type == "resume_task" else "task_started",
                  payload={**self._snapshot(), "command_id": command.command_id})
        self.task_thread = threading.Thread(
            target=self._execute, args=(config, journal, registry_path), daemon=True,
            name="local-codex-task",
        )
        self.task_thread.start()

    @staticmethod
    def _resumable_journal(root, workspace_path):
        candidates = []
        for path in root.rglob("task_*.json"):
            try:
                journal = TaskJournal.load(path)
                if Path(journal.workspace).resolve() == Path(workspace_path).resolve() and journal.status in {
                    AgentStatus.WORKING, AgentStatus.TESTING, AgentStatus.INTERRUPTED,
                    AgentStatus.BLOCKED, AgentStatus.PAUSED, AgentStatus.READY_FOR_APPROVAL,
                }:
                    candidates.append((path.stat().st_mtime_ns, str(path), journal))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return max(candidates, key=lambda entry: entry[:2])[2] if candidates else None

    def _execute(self, config, journal, registry_path):
        error = None
        try:
            result = self.runtime_factory().run_task(
                config, journal, dry_run=bool(config.get("dry_run", False)),
                workspaces_config_path=registry_path,
                status_callback=lambda message: self.emit("log", payload={"task_id": self.task_id, "message": message}),
                approval_callback=self._request_approval, cancel_event=self.cancel_event,
            )
            status = AgentStatus(result)
        except Exception as exc:
            status = AgentStatus.FAILED
            error = str(exc)
        with self._lock:
            if self.cancel_event.is_set() and status != AgentStatus.FAILED:
                status = AgentStatus.INTERRUPTED
            if status == AgentStatus.DONE:
                state, kind = "completed", "task_completed"
            elif status == AgentStatus.FAILED:
                state, kind = "failed", "task_failed"
            else:
                state, kind = "blocked", "task_blocked"
            self._request_id = None
            journal.status = status
            try:
                journal.save()
            except Exception:
                state, kind = "failed", "task_failed"
                status = AgentStatus.FAILED
                error = "Unable to persist task journal"
            self._transition(state)
            payload = {**self._snapshot(), "status": status.value}
            if error is not None:
                payload["message"] = error
            self.emit(kind, level="error" if state == "failed" else "info", payload=payload)
            self._active = False
            if self._shutdown_requested:
                self._finish_shutdown()

    def _request_approval(self, request):
        """Wait only on the task thread, for task review (never commit/push authority)."""
        with self._approval_condition:
            if not isinstance(request, dict) or request.get("kind") != "task_review":
                return False
            if self.cancel_event.is_set():
                return False
            self._transition("awaiting_approval")
            self._request_id = uuid4().hex
            self._approval_answer = None
            self.emit("approval_required", payload={
                **self._snapshot(), "kind": "task_review", "request": request,
            })
            self._approval_condition.wait_for(lambda: self._approval_answer is not None or self.cancel_event.is_set())
            return bool(self._approval_answer) and not self.cancel_event.is_set()

    def _approve(self, command):
        if self.state != "awaiting_approval" or command.payload["request_id"] != self._request_id:
            self._error("approval_mismatch", command.command_id)
            return
        request_id = self._request_id
        self._approval_answer = command.payload["approved"]
        self._request_id = None
        self._transition("working")
        self.emit("approval_resolved", payload={
            **self._snapshot(), "request_id": request_id,
            "approved": self._approval_answer, "command_id": command.command_id,
        })
        self._approval_condition.notify_all()

    def _stop(self, command_id=None):
        if self.state != "stopping":
            self._transition("stopping")
            self._request_id = None
            self.emit("task_stopping", payload={**self._snapshot(), "command_id": command_id})
        self.cancel_event.set()
        self._approval_condition.notify_all()

    def _shutdown(self, command_id=None):
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        self.emit("worker_stopping", payload={**self._snapshot(), "command_id": command_id})
        if self._active:
            self._stop(command_id)
        else:
            self._finish_shutdown()

    def _finish_shutdown(self):
        if not self._stopped.is_set():
            self.emit("worker_stopped", payload=self._snapshot())
            self._stopped.set()

    def _heartbeat(self):
        while not self._stopped.wait(self.heartbeat_seconds):
            with self._lock:
                if not self._stopped.is_set():
                    self.emit("worker_heartbeat", payload=self._snapshot())

    def run(self):
        self.emit("worker_started", payload=self._snapshot())
        heartbeat = threading.Thread(target=self._heartbeat, daemon=True, name="local-codex-heartbeat")
        heartbeat.start()
        try:
            while not self._shutdown_requested:
                line = self.input_stream.readline()
                if not line:
                    break
                try:
                    command = Command.from_json(line)
                except ProtocolError:
                    self._error("invalid_command")
                    continue
                self.handle(command)
        finally:
            with self._lock:
                self._shutdown()
            # Cooperative exit keeps the journal intact. The owning service may
            # forcibly terminate the process after its configured grace period.
            if self.task_thread is not None:
                self.task_thread.join()
            heartbeat.join()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--test-mode", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        runtime_factory = (lambda: _TestRuntime) if args.test_mode else (lambda: runtime)
        WorkerRuntime(
            sys.stdin, sys.stdout, data_root=args.data_root, runtime_factory=runtime_factory
        ).run()
    except Exception:
        # Never print exception text: provider failures may include credentials.
        print("Local Codex worker process failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
