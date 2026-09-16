import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

from hcs_ai.local_codex.protocol import Event
from hcs_ai.local_codex.service import LocalCodexService


class ReadPipe:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self):
        return self.lines.get()

    def feed(self, line):
        self.lines.put(line + "\n")

    def close(self):
        self.lines.put("")


class FakeProcess:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = ReadPipe()
        self.stderr = ReadPipe()
        self.pid = 12345
        self.returncode = None

    def poll(self):
        return self.returncode

    def exit(self, code=0):
        self.returncode = code
        self.stdout.close()
        self.stderr.close()

    def emit(self, sequence, kind, **payload):
        self.stdout.feed(Event(1, sequence, 1000, kind, payload=payload).to_json())

    def commands(self):
        return [json.loads(line) for line in self.stdin.getvalue().splitlines()]


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def collect(service, count):
    events = []
    wait_until(lambda: (events.extend(service.poll_events()) or len(events) >= count))
    return events


@pytest.fixture
def setup_service(tmp_path):
    services = []
    processes = []

    def make(**kwargs):
        process = FakeProcess()
        processes.append(process)
        calls = []

        def launch(*args, **options):
            calls.append((args, options))
            return process

        service = LocalCodexService(data_root=tmp_path, process_factory=kwargs.pop("process_factory", launch),
                                    terminator=kwargs.pop("terminator", lambda pid: process.exit(-9)),
                                    restart_limit=kwargs.pop("restart_limit", 0), **kwargs)
        services.append(service)
        service.start()
        wait_until(lambda: service.status()["pid"] is not None)
        return service, process, calls

    yield make
    for service in services:
        service.shutdown(grace_seconds=0)
    for process in processes:
        process.exit()


def test_service_sends_typed_submit_and_preserves_event_order(setup_service, tmp_path):
    service, process, calls = setup_service()
    process.emit(1, "worker_started", state="idle")
    result = service.submit_task("maze", "Fix portal collision")
    assert result.accepted
    wait_until(lambda: process.commands())
    command = process.commands()[0]
    assert command["type"] == "submit_task"
    assert command["protocol_version"] == 1
    assert command["payload"] == {"workspace_id": "maze", "prompt": "Fix portal collision"}
    process.emit(2, "task_started", state="working", workspace_id="maze", command_id=result.command_id)
    assert [event.sequence for event in collect(service, 2)] == [1, 2]
    args, options = calls[0]
    assert args[0][1:] == ["-m", "hcs_ai.local_codex.worker", "--data-root", str(tmp_path)]
    assert options["shell"] is False
    assert options["text"] is True and options["bufsize"] == 1
    assert Path(options["cwd"]).resolve() == Path(__file__).parents[1].resolve()


def test_busy_validation_resume_and_approval_are_typed(setup_service):
    service, process, _ = setup_service()
    assert service.submit_task("", "prompt").code == "invalid_command"
    first = service.resume_task("maze")
    assert first.accepted
    assert service.submit_task("maze", "second").code == "busy"
    process.emit(1, "command_error", command_id=first.command_id, code="no_resumable_task")
    collect(service, 1)
    assert service.submit_task("maze", "new").accepted
    assert service.approve("request-1", True).accepted
    assert service.approve("request-1", "yes").code == "invalid_command"
    wait_until(lambda: len(process.commands()) == 3)
    assert [command["type"] for command in process.commands()] == ["resume_task", "submit_task", "approve"]


def test_bad_versions_malformed_and_replayed_events_are_safe(setup_service):
    service, process, _ = setup_service()
    process.stdout.feed('broken api_key=NEVER-ECHO')
    process.stdout.feed('{"protocol_version":2,"sequence":1,"timestamp":1,"type":"log"}')
    process.emit(2, "worker_started")
    process.emit(2, "task_started")
    process.emit(1, "task_started")
    process.emit(3, "task_completed")
    events = collect(service, 6)
    assert [event.type for event in events] == ["protocol_warning", "protocol_warning", "worker_started",
                                               "protocol_warning", "protocol_warning", "task_completed"]
    assert [event.sequence for event in events] == list(range(1, 7))
    assert "NEVER-ECHO" not in str([event.to_dict() for event in events])


@pytest.mark.parametrize("malformed", [
    '{"protocol_version":1,"sequence":2,"timestamp":' + str(10 ** 400)
    + ',"type":"log","level":"info","payload":{"message":"NEVER-ECHO"}}',
    '{"protocol_version":1,"sequence":2,"timestamp":1,"type":"log","level":"info","payload":'
    + '[' * 2000 + '"NEVER-ECHO"' + ']' * 2000 + '}',
    '{"protocol_version":1,"sequence":2,"timestamp":1,"type":"log","level":"info","payload":'
    + '[' * 700 + '"NEVER-ECHO"' + ']' * 700 + '}',
], ids=["overflowing_timestamp", "decoder_recursion", "event_validation_recursion"])
def test_extreme_event_input_warns_and_reader_accepts_following_heartbeat(setup_service, malformed):
    service, process, _ = setup_service()
    process.emit(1, "worker_started")
    process.stdout.feed(malformed)
    process.emit(2, "heartbeat", state="idle")
    events = collect(service, 3)
    assert [event.type for event in events] == ["worker_started", "protocol_warning", "heartbeat"]
    assert [event.sequence for event in events] == [1, 2, 3]
    assert "NEVER-ECHO" not in "".join(event.to_json() for event in events)
    assert service.status()["worker_state"] == "running"


def test_stderr_and_events_are_redacted_before_queue_and_disk(setup_service, tmp_path):
    service, process, _ = setup_service(secret_values=("private-key",))
    process.stderr.feed("failure private-key Bearer other-key")
    process.emit(1, "log", message="private-key", api_key="arbitrary")
    events = collect(service, 2)
    text = "".join(event.to_json() for event in events)
    assert "private-key" not in text and "other-key" not in text and "arbitrary" not in text
    assert "[REDACTED]" in text
    wait_until(lambda: service.log_path.exists())
    assert "private-key" not in service.log_path.read_text()


def test_queue_pressure_drops_only_verbose_logs_and_coalesces_warning(setup_service):
    service, process, _ = setup_service(event_capacity=4)
    for sequence in range(1, 30):
        process.emit(sequence, "log", message=str(sequence))
    process.emit(30, "approval_required", request_id="review")
    process.emit(31, "action_result", result="done")
    process.emit(32, "task_completed")
    time.sleep(0.1)
    events = collect(service, 5)
    kinds = [event.type for event in events]
    assert "approval_required" in kinds and "action_result" in kinds and "task_completed" in kinds
    assert kinds.count("log_warning") == 1
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)


def test_heartbeat_timeout_reports_without_killing_and_recovers(setup_service):
    killed = []
    service, process, _ = setup_service(heartbeat_timeout_seconds=0.06, terminator=killed.append)
    process.emit(1, "worker_started")
    wait_until(lambda: service.status()["worker_state"] == "unresponsive")
    assert killed == []
    process.emit(2, "heartbeat", state="idle")
    wait_until(lambda: service.status()["worker_state"] == "running")


def test_crash_restart_is_bounded_and_never_replays_task(setup_service, tmp_path):
    processes = [FakeProcess() for _ in range(3)]
    launches = []

    def launch(*args, **kwargs):
        process = processes[len(launches)]
        launches.append(process)
        return process

    service, _, _ = setup_service(process_factory=launch, restart_limit=1, restart_delay_seconds=0.05,
                                  terminator=lambda pid: [process.exit() for process in processes])
    journal = tmp_path / "journal.json"
    journal.write_text("preserve")
    assert service.submit_task("maze", "once").accepted
    wait_until(lambda: processes[0].commands())
    processes[0].stderr.feed("safe diagnostic")
    time.sleep(0.03)
    processes[0].exit(7)
    events = collect(service, 2)
    crash = next(event for event in events if event.type == "worker_crashed")
    assert crash.payload["exit_code"] == 7
    assert "safe diagnostic" in crash.payload["last_diagnostic"]
    wait_until(lambda: len(launches) == 2)
    assert processes[1].commands() == []
    processes[1].exit(8)
    wait_until(lambda: service.status()["worker_state"] == "crashed")
    time.sleep(0.12)
    assert len(launches) == 2
    assert service.restart_worker().accepted
    wait_until(lambda: len(launches) == 3)
    assert journal.read_text() == "preserve"
    assert processes[2].commands() == []


def test_stop_and_shutdown_force_tree_only_after_grace_and_are_nonblocking(setup_service):
    killed = []
    service, process, _ = setup_service(terminator=lambda pid: (killed.append(pid), process.exit(-9)))
    assert service.submit_task("maze", "work").accepted
    started = time.monotonic()
    assert service.stop_task(grace_seconds=0.1).accepted
    assert time.monotonic() - started < 0.05
    wait_until(lambda: len(process.commands()) == 2)
    assert process.commands()[-1]["type"] == "stop_task"
    assert killed == []
    wait_until(lambda: killed)
    assert killed == [process.pid]
    service.shutdown(grace_seconds=0)
    service.shutdown(grace_seconds=0)
    assert killed == [process.pid]


def test_cooperative_shutdown_and_cooperative_stop_do_not_terminate(setup_service):
    killed = []
    service, process, _ = setup_service(terminator=killed.append)
    service.submit_task("maze", "work")
    service.stop_task(grace_seconds=0.1)
    process.emit(1, "task_blocked", state="blocked")
    collect(service, 1)
    time.sleep(0.12)
    assert killed == []
    service.shutdown(grace_seconds=0.1)
    wait_until(lambda: process.commands()[-1]["type"] == "shutdown")
    assert [command["type"] for command in process.commands()][-2:] == ["stop_task", "shutdown"]
    process.exit(0)
    wait_until(lambda: service.status()["worker_state"] == "stopped")
    assert killed == []


def test_workspace_refresh_is_cached_nonblocking_and_reports_corruption(setup_service, tmp_path):
    service, _, _ = setup_service()
    registry = tmp_path / "workspaces" / "workspaces.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"workspaces": [{"workspace_id": "maze", "path": str(tmp_path),
                                                    "name": "Maze", "aliases": [], "target_branch": "main"}]}))
    service.refresh_workspaces()
    wait_until(lambda: service.list_workspaces())
    assert service.list_workspaces()[0].workspace_id == "maze"
    registry.write_text("broken-secret")
    service.refresh_workspaces()
    events = collect(service, 1)
    assert any(event.type == "log_warning" for event in events)
    assert service.list_workspaces()[0].workspace_id == "maze"
    assert "broken-secret" not in str(events)


def test_stalled_stdin_cannot_block_public_methods(setup_service):
    release = threading.Event()
    service, process, _ = setup_service()

    class StalledPipe:
        def write(self, line):
            release.wait(2)

        def flush(self):
            pass

    process.stdin = StalledPipe()
    try:
        started = time.monotonic()
        service.submit_task("maze", "work")
        service.status()
        service.poll_events()
        service.shutdown(grace_seconds=0.01)
        assert time.monotonic() - started < 0.1
        wait_until(lambda: process.poll() is not None)
    finally:
        release.set()


def test_critical_only_backpressure_preserves_events_and_cannot_block_shutdown(setup_service):
    service, process, _ = setup_service(event_capacity=2)
    process.emit(1, "worker_started")
    process.emit(2, "action_result")
    process.emit(3, "approval_required")
    time.sleep(0.05)
    service.shutdown(grace_seconds=0)
    wait_until(lambda: process.poll() is not None)
    events = collect(service, 3)
    assert [event.type for event in events[:3]] == ["worker_started", "action_result", "approval_required"]


def test_forced_stop_is_expected_exit_without_auto_restart(setup_service):
    service, process, calls = setup_service(restart_limit=2, restart_delay_seconds=0.03)
    service.submit_task("maze", "work")
    service.stop_task(grace_seconds=0)
    wait_until(lambda: process.poll() is not None)
    wait_until(lambda: service.status()["pid"] is None)
    assert service.status()["worker_state"] == "stopped"
    time.sleep(0.1)
    assert len(calls) == 1
    assert all(event.type != "worker_crashed" for event in service.poll_events())


def test_unrelated_command_error_does_not_clear_active_task(setup_service):
    service, process, _ = setup_service()
    service.submit_task("maze", "work")
    process.emit(1, "task_started", state="working")
    process.emit(2, "command_error", code="invalid_command")
    collect(service, 2)
    assert service.submit_task("maze", "second").code == "busy"


def test_snapshot_never_exposes_known_secrets(setup_service):
    service, process, _ = setup_service(secret_values=("private-key",))
    process.emit(1, "status_snapshot", state="private-key", workspace_id="private-key", provider="private-key")
    collect(service, 1)
    assert "private-key" not in str(service.status())


def test_startup_failure_is_typed_and_does_not_escape_public_start(tmp_path):
    def fail(*args, **kwargs):
        raise OSError("hidden-secret")

    service = LocalCodexService(data_root=tmp_path, process_factory=fail)
    try:
        assert service.start().accepted
        event = collect(service, 1)[0]
        assert event.type == "worker_crashed"
        assert event.payload["code"] == "startup_failed"
        assert "hidden-secret" not in event.to_json()
        assert service.status()["worker_state"] == "failed"
    finally:
        service.shutdown(grace_seconds=0)


def test_broken_stdin_releases_pending_submission_and_reports_safe_error(setup_service):
    service, process, _ = setup_service()

    class BrokenPipe:
        def write(self, line):
            raise BrokenPipeError("hidden-secret")

    process.stdin = BrokenPipe()
    result = service.submit_task("maze", "work")
    event = collect(service, 1)[0]
    assert event.type == "command_error"
    assert event.payload["command_id"] == result.command_id
    assert "hidden-secret" not in event.to_json()
    assert service.status()["active"] is False
    assert service.status()["worker_state"] == "unresponsive"


def test_configured_environment_and_sensitive_prompt_are_redacted(setup_service, monkeypatch):
    monkeypatch.setenv("SERVICE_SECRET", "environment-value")
    service, process, _ = setup_service(config={"secret_names": ["SERVICE_SECRET"]})
    process.emit(1, "log", message="environment-value")
    process.emit(2, "log", prompt="private prompt", sensitive=True)
    events = collect(service, 2)
    text = "".join(event.to_json() for event in events)
    assert "environment-value" not in text
    assert "private prompt" not in text


def test_disk_failure_preserves_events_with_one_coalesced_warning(setup_service, tmp_path):
    service, process, _ = setup_service()
    service.log_dir.write_text("not a directory")
    for sequence in range(1, 6):
        process.emit(sequence, "action_result", result=sequence)
    events = collect(service, 6)
    time.sleep(0.05)
    events += service.poll_events()
    assert sum(event.type == "action_result" for event in events) == 5
    assert sum(event.type == "log_warning" for event in events) == 1


def test_shutdown_during_slow_spawn_still_forces_the_eventual_process(tmp_path):
    release = threading.Event()
    entered = threading.Event()
    process = FakeProcess()
    killed = []

    def launch(*args, **kwargs):
        entered.set()
        release.wait(2)
        return process

    service = LocalCodexService(data_root=tmp_path, process_factory=launch,
                                terminator=lambda pid: (killed.append(pid), process.exit(-9)))
    service.start()
    assert entered.wait(1)
    service.shutdown(grace_seconds=0)
    release.set()
    wait_until(lambda: killed)
    wait_until(lambda: service.status()["worker_state"] == "stopped")
    assert killed == [process.pid]
    assert service.status()["pid"] is None


def test_shutdown_during_slow_spawn_preserves_cooperative_grace(tmp_path):
    release = threading.Event()
    entered = threading.Event()
    terminated = threading.Event()
    process = FakeProcess()
    launch_returned = []
    killed_at = []

    def launch(*args, **kwargs):
        entered.set()
        release.wait(2)
        launch_returned.append(time.monotonic())
        return process

    def terminate(pid):
        killed_at.append(time.monotonic())
        process.exit(-9)
        terminated.set()

    service = LocalCodexService(data_root=tmp_path, process_factory=launch, terminator=terminate)
    try:
        service.start()
        assert entered.wait(1)
        service.shutdown(grace_seconds=0.2)
        # The process can only receive its cooperative commands after creation.
        assert not terminated.wait(0.05)
        release.set()
        wait_until(lambda: len(process.commands()) == 2)
        assert [command["type"] for command in process.commands()] == ["stop_task", "shutdown"]
        assert service.status()["worker_state"] == "stopping"
        assert not terminated.wait(0.08)
        assert terminated.wait(1)
        assert killed_at[0] - launch_returned[0] >= 0.2
        wait_until(lambda: service.status()["worker_state"] == "stopped")
    finally:
        release.set()
        process.exit()
        service.shutdown(grace_seconds=0)


def test_real_worker_start_status_and_cooperative_shutdown(tmp_path):
    processes = []

    def launch(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        processes.append(process)
        return process

    service = LocalCodexService(data_root=tmp_path, restart_limit=0, process_factory=launch)
    try:
        service.start()
        events = collect(service, 1)
        assert events[0].type == "worker_started"
        assert service.status()["worker_state"] == "running"
        assert service.status()["pid"] is not None
        service.shutdown(grace_seconds=2)
        wait_until(lambda: service.status()["worker_state"] == "stopped")
        assert service.status()["pid"] is None
        wait_until(lambda: all(stream.closed for stream in
                               (processes[0].stdin, processes[0].stdout, processes[0].stderr)))
    finally:
        service.shutdown(grace_seconds=0)


def test_drop_warning_cannot_overtake_previous_event_on_disk(setup_service, monkeypatch):
    from hcs_ai.local_codex.protocol import EventLog

    entered = threading.Event()
    release = threading.Event()
    append = EventLog.append

    def delayed_append(log, event):
        if event.sequence == 5:
            entered.set()
            release.wait(2)
        return append(log, event)

    monkeypatch.setattr(EventLog, "append", delayed_append)
    service, process, _ = setup_service(event_capacity=4)
    try:
        for sequence in range(1, 6):
            process.emit(sequence, "log", message=str(sequence))
        assert entered.wait(1)
        service.poll_events()
        time.sleep(0.05)
    finally:
        release.set()

    def persisted_sequences():
        return [json.loads(line)["sequence"] for line in service.log_path.read_text().splitlines()]

    wait_until(lambda: len(persisted_sequences()) >= 6)
    sequences = persisted_sequences()
    assert sequences == sorted(sequences)


def test_stop_can_terminate_unresponsive_idle_worker(setup_service):
    service, process, _ = setup_service(heartbeat_timeout_seconds=0.03)
    wait_until(lambda: service.status()["worker_state"] == "unresponsive")
    assert service.stop_task(grace_seconds=0).accepted
    wait_until(lambda: process.poll() is not None)
    wait_until(lambda: service.status()["worker_state"] == "stopped")


def test_shutdown_default_terminator_stops_real_descendant_tree(tmp_path, monkeypatch):
    import psutil

    # Some executor sandboxes expose a host /proc under a different PID namespace.
    # Never send signals through psutil unless it can identify this interpreter.
    try:
        same_namespace = os.path.samefile(psutil.Process().exe(), sys.executable)
    except (psutil.Error, OSError):
        same_namespace = False
    if not same_namespace:
        pytest.skip("psutil process namespace does not identify the running interpreter")
    monkeypatch.setenv("PYSTRAY_BACKEND", "dummy")
    processes = []
    script = (
        "import json, signal, subprocess, sys, time\n"
        "if hasattr(signal, 'SIGCHLD'): signal.signal(signal.SIGCHLD, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(json.dumps({'protocol_version': 1, 'sequence': 1, 'timestamp': 1, "
        "'type': 'worker_started', 'level': 'info', 'payload': {'child_pid': child.pid}}), flush=True)\n"
        "time.sleep(120)\n"
    )

    def launch(args, **kwargs):
        process = subprocess.Popen([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    service = LocalCodexService(data_root=tmp_path, process_factory=launch, restart_limit=0)
    child_pid = None
    try:
        service.start()
        child_pid = collect(service, 1)[0].payload["child_pid"]
        service.shutdown(grace_seconds=0.05)
        wait_until(lambda: service.status()["worker_state"] == "stopped", timeout=12)
        assert processes[0].poll() is not None
        assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
    finally:
        service.shutdown(grace_seconds=0)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(3)
        if child_pid is not None and psutil.pid_exists(child_pid):
            try:
                psutil.Process(child_pid).kill()
            except psutil.NoSuchProcess:
                pass
