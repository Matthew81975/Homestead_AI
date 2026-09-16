"""Thread-safe GUI facade for the supervised Local Codex NDJSON worker.

Public operations enqueue work and never wait for pipe, filesystem or process I/O.
Accepted command results mean queued, not executed; worker errors remain events.
Facade sequences are global; each worker's independent sequence is validated before
publication. Critical-only queue saturation backpressures readers, not supervision.
"""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

from hcs_ai.config import ROOT, load_config
from .protocol import Command, Event, EventFactory, EventLog, ProtocolError, redact
from .workspace_registry import WorkspaceRegistry


@dataclass(frozen=True)
class CommandResult:
    accepted: bool
    code: str
    command_id: str | None = None


def _terminate_tree(pid):
    # Lazy import avoids loading the desktop/tray stack in headless consumers.
    from hcs_ai.desktop_host import terminate_process_tree
    terminate_process_tree(pid)


def _secret_settings(config):
    names = set(config.get("secret_names", ())) | set(config.get("credential_names", ()))
    values = set()
    pattern = re.compile(r"authorization|api[_-]?key|secret|password|credential|token", re.I)

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"secret_names", "credential_names"} and isinstance(item, (tuple, list)):
                    names.update(name for name in item if isinstance(name, str))
                if key == "required_envs" and isinstance(item, (tuple, list)):
                    values.update(os.environ[name] for name in item if isinstance(name, str) and os.environ.get(name))
                elif key.endswith("_env") and isinstance(item, str) and os.environ.get(item):
                    values.add(os.environ[item])
                elif (pattern.search(key) or key in names) and isinstance(item, str) and item:
                    values.add(item)
                visit(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit(item)

    visit(config)
    values.update(os.environ[name] for name in names if isinstance(name, str) and os.environ.get(name))
    return tuple(names), tuple(values)


class LocalCodexService:
    def __init__(self, *, data_root=None, process_factory=subprocess.Popen,
                 terminator=_terminate_tree, config=None, event_capacity=1000,
                 heartbeat_timeout_seconds=None, stop_grace_seconds=None,
                 restart_limit=2, restart_delay_seconds=1.0,
                 secret_names=(), secret_values=()):
        if event_capacity < 1 or restart_limit < 0 or restart_delay_seconds <= 0:
            raise ValueError("invalid service buffer/restart limits")
        self.config = deepcopy(load_config().get("local_codex", {}) if config is None else config)
        root = Path(data_root or self.config.get("data_root", "data/local_codex"))
        self.data_root = root if root.is_absolute() else ROOT / root
        self.log_dir = self.data_root / "logs"
        self.log_path = self.log_dir / "service.log"
        self.workspace_registry_path = self.data_root / "workspaces" / "workspaces.json"
        worker = self.config.get("worker", {})
        self._heartbeat_timeout = (heartbeat_timeout_seconds if heartbeat_timeout_seconds is not None
                                   else worker.get("heartbeat_timeout_seconds", 20))
        self._stop_grace = (stop_grace_seconds if stop_grace_seconds is not None
                            else worker.get("stop_grace_seconds", 10))
        if self._heartbeat_timeout <= 0 or self._stop_grace < 0:
            raise ValueError("invalid worker timeouts")
        names, values = _secret_settings(self.config)
        self._names = tuple(names) + tuple(secret_names)
        self._values = tuple(values) + tuple(secret_values)
        self._factory = EventFactory(secret_names=self._names, secret_values=self._values)
        self._log = EventLog(self.log_path, **self.config.get("logs", {}))
        self._process_factory = process_factory
        self._terminator = terminator
        self._capacity = event_capacity
        self._events = deque()
        self._condition = threading.Condition()
        self._publish_lock = threading.Lock()
        self._dropped = 0
        self._disk_warning = False
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._process = None
        self._commands = None
        self._supervisor = None
        self._want_start = False
        self._closing = False
        self._shutdown_grace = self._stop_grace
        self._deadline = None
        self._expected_exit = False
        self._pipe_broken = False
        self._last_heartbeat = 0
        self._last_diagnostic = ""
        self._restart_limit = restart_limit
        self._restart_count = 0
        self._restart_delay = restart_delay_seconds
        self._restart_at = None
        self._pending_task = None
        self._active = False
        self._workspaces = []
        self._refreshing = False
        self._snapshot = {"worker_state": "stopped", "state": "idle", "task_state": "idle",
                          "pid": None, "workspace_id": None, "task_id": None,
                          "request_id": None, "provider": None}

    @staticmethod
    def _thread(target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True, name="local-codex-service")
        thread.start()
        return thread

    def start(self):
        with self._lock:
            if self._closing:
                return CommandResult(False, "shutting_down")
            if self._process is not None or self._want_start:
                return CommandResult(True, "already_started")
            self._want_start = True
            self._snapshot["worker_state"] = "starting"
            if self._supervisor is None or not self._supervisor.is_alive():
                self._supervisor = self._thread(self._supervise)
            self._wake.set()
        self.refresh_workspaces()
        return CommandResult(True, "starting")

    def restart_worker(self):
        with self._lock:
            if self._closing:
                return CommandResult(False, "shutting_down")
            if self._process is not None:
                return CommandResult(False, "worker_running")
            self._restart_count = 0
            self._restart_at = None
        return self.start()

    def _command(self, kind, payload, *, allow_closing=False):
        command_id = uuid4().hex
        try:
            command = Command(1, command_id, kind, payload)
        except ProtocolError:
            return CommandResult(False, "invalid_command")
        with self._lock:
            if self._closing and not allow_closing:
                return CommandResult(False, "shutting_down")
            if self._process is None:
                return CommandResult(False, "worker_unavailable")
            if self._pipe_broken and kind not in {"stop_task", "shutdown"}:
                return CommandResult(False, "worker_unavailable")
            if kind in {"submit_task", "resume_task"}:
                if self._active:
                    return CommandResult(False, "busy")
                self._active = True
                self._pending_task = command_id
            try:
                self._commands.put_nowait(command)
            except queue.Full:
                if self._pending_task == command_id:
                    self._active = False
                    self._pending_task = None
                return CommandResult(False, "command_queue_full")
        return CommandResult(True, "queued", command_id)

    def submit_task(self, workspace_id, prompt):
        return self._command("submit_task", {"workspace_id": workspace_id, "prompt": prompt})

    def resume_task(self, workspace_id):
        return self._command("resume_task", {"workspace_id": workspace_id})

    def approve(self, request_id, approved):
        return self._command("approve", {"request_id": request_id, "approved": approved})

    def stop_task(self, grace_seconds=None):
        grace = self._stop_grace if grace_seconds is None else max(0, grace_seconds)
        result = self._command("stop_task", {})
        with self._lock:
            if result.accepted and (self._active or self._pipe_broken
                                    or self._snapshot["worker_state"] == "unresponsive"):
                self._deadline = time.monotonic() + grace
                self._wake.set()
        return result

    def shutdown(self, grace_seconds=None):
        """Start complete cleanup without blocking Tk.

        Send Stop then Shutdown; the supervisor terminates the full worker tree
        after grace expires. Hosts await ``status()['worker_state'] == 'stopped'``
        and ``status()['pid'] is None`` off the Tk thread before exiting.
        """
        grace = self._stop_grace if grace_seconds is None else max(0, grace_seconds)
        with self._lock:
            if self._closing:
                return CommandResult(True, "shutting_down")
            self._closing = True
            self._shutdown_grace = grace
            self._want_start = False
            self._restart_at = None
            if self._process is not None:
                self._command("stop_task", {}, allow_closing=True)
                self._command("shutdown", {}, allow_closing=True)
                self._snapshot["worker_state"] = "stopping"
                self._deadline = time.monotonic() + grace
            self._wake.set()
        return CommandResult(True, "shutting_down")

    def status(self):
        with self._lock:
            return {**self._snapshot, "active": self._active,
                    "restart_count": self._restart_count, "log_dir": str(self.log_dir)}

    def list_workspaces(self):
        with self._lock:
            return deepcopy(self._workspaces)

    def refresh_workspaces(self):
        with self._lock:
            if not self._refreshing:
                self._refreshing = True
                self._thread(self._refresh_workspaces)
        return CommandResult(True, "refreshing")

    def _refresh_workspaces(self):
        try:
            workspaces = WorkspaceRegistry.load(self.workspace_registry_path).all_enabled()
            with self._lock:
                self._workspaces = workspaces
        except FileNotFoundError:
            pass
        except Exception:
            self._notify("log_warning", "warning", {"code": "workspace_registry_unavailable"})
        finally:
            with self._lock:
                self._refreshing = False

    def poll_events(self, limit=200):
        if limit <= 0:
            return []
        with self._condition:
            events = []
            while self._events and len(events) < limit:
                events.append(self._events.popleft())
            if self._dropped and len(self._events) < self._capacity:
                dropped = self._dropped
                self._dropped = 0
                # Use the same serialized publisher so disk and GUI sequences
                # agree; the warning becomes available on a subsequent poll.
                self._notify("log_warning", "warning", {
                    "code": "verbose_logs_dropped", "count": dropped})
            self._condition.notify_all()
            return events

    def _notify(self, kind, level="info", payload=None):
        # Supervisor diagnostics must not block process cleanup on UI backpressure.
        self._thread(self._publish, kind, level, payload)

    def _publish(self, kind, level="info", payload=None):
        with self._publish_lock:
            with self._condition:
                while len(self._events) >= self._capacity:
                    verbose = next((event for event in self._events
                                    if event.type == "log" and event.level in {"info", "debug"}), None)
                    if verbose is not None:
                        self._events.remove(verbose)
                        self._dropped += 1
                        break
                    if kind == "log" and level in {"info", "debug"}:
                        self._dropped += 1
                        return
                    self._condition.wait()
                event = self._factory.emit(kind, level, payload)
                self._events.append(event)
            persisted = self._log.append(event)
            if persisted is not event:
                if not self._disk_warning:
                    self._disk_warning = True
                    self._notify("log_warning", "warning", persisted.payload)
            elif kind != "log_warning":
                self._disk_warning = False

    def _launch(self):
        try:
            process = self._process_factory(
                [sys.executable, "-m", "hcs_ai.local_codex.worker", "--data-root", str(self.data_root)],
                shell=False, cwd=str(ROOT), text=True, encoding="utf-8", errors="replace", bufsize=1,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        except Exception:
            with self._lock:
                self._snapshot["worker_state"] = "failed"
            self._notify("worker_crashed", "error", {"code": "startup_failed", "exit_code": None,
                                                      "last_diagnostic": "Unable to start Local Codex worker"})
            return
        commands = queue.Queue(maxsize=100)
        with self._lock:
            self._process = process
            self._commands = commands
            self._last_heartbeat = time.monotonic()
            self._last_diagnostic = ""
            self._expected_exit = False
            self._pipe_broken = False
            self._snapshot.update(worker_state="running", pid=process.pid)
            if self._closing:
                self._command("stop_task", {}, allow_closing=True)
                self._command("shutdown", {}, allow_closing=True)
                self._snapshot["worker_state"] = "stopping"
                # Creation may have overlapped shutdown. Its cooperative grace
                # begins once the process exists and commands can be delivered.
                self._deadline = time.monotonic() + self._shutdown_grace
        self._thread(self._write_commands, process, commands)
        self._thread(self._read_stdout, process)
        self._thread(self._read_stderr, process)

    def _write_commands(self, process, commands):
        try:
            self._write_command_loop(process, commands)
        finally:
            self._close_stream(process.stdin)

    @staticmethod
    def _close_stream(stream):
        try:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        except (OSError, ValueError):
            pass

    def _write_command_loop(self, process, commands):
        while True:
            command = commands.get()
            if command is None:
                return
            with self._lock:
                if self._process is not process:
                    return
            try:
                process.stdin.write(json.dumps({"protocol_version": command.protocol_version,
                                                "command_id": command.command_id,
                                                "type": command.type, "payload": command.payload}) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                with self._lock:
                    if self._process is process:
                        self._pipe_broken = True
                        self._snapshot["worker_state"] = "unresponsive"
                        if self._pending_task == command.command_id:
                            self._pending_task = None
                            self._active = False
                            self._snapshot.update(state="interrupted", task_state="interrupted")
                self._notify("command_error", "error", {"code": "worker_pipe_closed", "command_id": command.command_id})
                return

    def _read_stdout(self, process):
        sequence = 0
        try:
            while line := process.stdout.readline():
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict) or set(raw) != {"protocol_version", "sequence", "timestamp", "type", "level", "payload"}:
                        raise ProtocolError("invalid envelope")
                    event = Event(**raw)
                    if event.sequence <= sequence:
                        raise ProtocolError("out of order")
                except (ValueError, TypeError, OverflowError, RecursionError):
                    self._publish("protocol_warning", "warning", {"code": "invalid_worker_event"})
                    continue
                sequence = event.sequence
                self._update_event(process, event)
                self._publish(event.type, event.level, event.payload)
        except (OSError, ValueError):
            self._notify("protocol_warning", "warning", {"code": "worker_stdout_closed"})
        finally:
            self._close_stream(process.stdout)

    def _read_stderr(self, process):
        try:
            while line := process.stderr.readline():
                diagnostic = redact(line.rstrip(), secret_names=self._names, secret_values=self._values)
                with self._lock:
                    if self._process is process:
                        self._last_diagnostic = diagnostic
                self._publish("log_warning", "warning", {"code": "worker_stderr", "message": diagnostic})
        except (OSError, ValueError):
            self._notify("log_warning", "warning", {"code": "worker_stderr_closed"})
        finally:
            self._close_stream(process.stderr)

    def _update_event(self, process, event):
        payload = redact(event.payload, secret_names=self._names, secret_values=self._values)
        if not isinstance(payload, dict):
            return
        with self._lock:
            if self._process is not process:
                return
            if event.type in {"worker_started", "heartbeat", "worker_heartbeat"}:
                self._last_heartbeat = time.monotonic()
                if not self._closing and not self._pipe_broken:
                    self._snapshot["worker_state"] = "running"
            for key in ("workspace_id", "task_id", "request_id", "provider"):
                if key in payload:
                    self._snapshot[key] = redact(payload[key], secret_values=self._values)
            states = {"task_started": "working", "task_resumed": "working", "task_stopping": "stopping",
                      "task_completed": "completed", "task_blocked": "blocked", "task_failed": "failed",
                      "approval_required": "awaiting_approval", "approval_resolved": "working"}
            state = states.get(event.type, payload.get("state") if event.type == "status_snapshot" else None)
            if state is not None:
                self._snapshot.update(state=state, task_state=state)
            if event.type in {"task_started", "task_resumed"}:
                self._active = True
                self._pending_task = None
            if event.type in {"task_completed", "task_blocked", "task_failed"} or (
                    event.type == "command_error" and self._pending_task is not None
                    and payload.get("command_id") == self._pending_task):
                self._active = False
                self._pending_task = None
                if not self._closing:
                    self._deadline = None

    def _supervise(self):
        while True:
            self._wake.wait(0.02)
            self._wake.clear()
            now = time.monotonic()
            with self._lock:
                launch = self._want_start or (self._restart_at is not None and now >= self._restart_at)
                if launch:
                    self._want_start = False
                    self._restart_at = None
                process = self._process
                closing = self._closing
                deadline = self._deadline
            if launch and not closing and process is None:
                self._launch()
                continue
            if process is None:
                if closing:
                    with self._lock:
                        self._snapshot["worker_state"] = "stopped"
                    return
                continue
            code = process.poll()
            if code is not None:
                with self._lock:
                    self._process = None
                    try:
                        self._commands.put_nowait(None)
                    except queue.Full:
                        pass
                    expected = closing or deadline is not None or self._expected_exit
                    self._snapshot.update(pid=None, worker_state="stopped" if expected else "crashed")
                    if self._active:
                        self._snapshot.update(state="interrupted", task_state="interrupted")
                    self._active = False
                    self._pending_task = None
                    self._deadline = None
                    diagnostic = self._last_diagnostic
                    if not expected and self._restart_count < self._restart_limit:
                        self._restart_count += 1
                        self._restart_at = now + self._restart_delay * self._restart_count
                if not expected:
                    self._notify("worker_crashed", "error", {"exit_code": code, "last_diagnostic": diagnostic})
                else:
                    self._notify("worker_stopped", payload={"exit_code": code})
                continue
            if deadline is not None and now >= deadline:
                with self._lock:
                    self._expected_exit = True
                try:
                    self._terminator(process.pid)
                except Exception:
                    self._notify("log_warning", "error", {"code": "worker_termination_failed"})
                with self._lock:
                    # Do not spin on a failed terminator; a later explicit stop may retry.
                    self._deadline = None
                continue
            with self._lock:
                timed_out = now - self._last_heartbeat > self._heartbeat_timeout
                warn = timed_out and self._snapshot["worker_state"] == "running"
                if warn:
                    self._snapshot["worker_state"] = "unresponsive"
            if warn:
                self._notify("log_warning", "warning", {"code": "heartbeat_timeout"})
