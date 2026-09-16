import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from hcs_ai.local_codex import runtime as task_runtime
from hcs_ai.local_codex.protocol import Command, EventLog
from hcs_ai.local_codex.state import AgentStatus, TaskJournal
from hcs_ai.local_codex.worker import WorkerRuntime


def command(kind, payload=None):
    return Command(1, f"command-{kind}", kind, payload or {})


def events(worker):
    return [json.loads(line) for line in worker.output_stream.getvalue().splitlines()]


def wait_for(worker, kind):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        found = [event for event in events(worker) if event["type"] == kind]
        if found:
            return found[-1]
        threading.Event().wait(0.005)
    pytest.fail(f"missing {kind}: {events(worker)}")


class BlockingRuntime:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def run_task(self, config, journal, *, dry_run, workspaces_config_path,
                 status_callback, approval_callback, cancel_event):
        self.calls.append((config, journal, cancel_event))
        self.entered.set()
        while not self.release.wait(0.005):
            if cancel_event.is_set():
                journal.status = AgentStatus.INTERRUPTED
                journal.save()
                return AgentStatus.INTERRUPTED
        journal.status = AgentStatus.DONE
        journal.save()
        return AgentStatus.DONE


@pytest.fixture
def setup_worker(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    root = tmp_path / "data" / "local_codex"
    registry = root / "workspaces" / "workspaces.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"workspaces": [{
        "workspace_id": "one", "name": "Project", "aliases": [],
        "path": str(workspace), "target_branch": "main",
    }]}))
    config = {"local_codex": {"model": "test-model", "max_failed_actions": 3,
                               "lm_studio_url": "http://localhost:1234/v1"}}
    monkeypatch.setattr("hcs_ai.config.load_config", lambda: config)
    workers = []

    def make(runtime=None, **kwargs):
        worker = WorkerRuntime(
            io.StringIO(), io.StringIO(), data_root=root,
            runtime_factory=lambda: runtime or task_runtime,
            clock=lambda: 1000.0, heartbeat_seconds=kwargs.pop("heartbeat_seconds", 30),
            **kwargs,
        )
        workers.append(worker)
        return worker

    yield make, root, workspace, config
    for worker in workers:
        worker.handle(command("shutdown"))
        thread = getattr(worker, "task_thread", None)
        if thread:
            thread.join(3)


def test_worker_rejects_second_task_as_busy_without_starting_it(setup_worker):
    make, _, _, _ = setup_worker
    runtime = BlockingRuntime()
    worker = make(runtime)
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "first"}))
    assert runtime.entered.wait(2)
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "second"}))
    assert [event["type"] for event in events(worker)][:2] == ["task_started", "command_error"]
    assert events(worker)[-1]["payload"]["code"] == "busy"
    assert len(runtime.calls) == 1


def test_submit_uses_registered_workspace_and_owned_journal(setup_worker):
    make, root, workspace, _ = setup_worker
    runtime = BlockingRuntime()
    worker = make(runtime)
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "first"}))
    assert runtime.entered.wait(2)
    config, journal, _ = runtime.calls[0]
    assert config["workspace"] == str(workspace)
    assert journal.task == "first"
    assert journal.path.parent == root / "tasks" / "journals"
    assert config["tornado"]["state_path"] == str(root / "tornado" / "tornado_state.json")
    runtime.release.set()
    assert wait_for(worker, "task_completed")["payload"]["state"] == "completed"


def test_unknown_workspace_rejected_without_starting_task(setup_worker):
    make, _, _, _ = setup_worker
    runtime = BlockingRuntime()
    worker = make(runtime)
    worker.handle(command("submit_task", {"workspace_id": "../outside", "prompt": "first"}))
    assert events(worker)[-1]["payload"]["code"] == "workspace_not_found"
    assert not runtime.calls


def test_status_stays_responsive_during_task(setup_worker):
    make, _, _, _ = setup_worker
    worker = make(BlockingRuntime())
    worker.handle(command("status"))
    assert events(worker)[-1]["payload"]["state"] == "idle"
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "first"}))
    worker.handle(command("status"))
    snapshot = events(worker)[-1]
    assert snapshot["type"] == "status_snapshot"
    assert snapshot["payload"]["state"] == "working"
    assert snapshot["payload"]["workspace_id"] == "one"


def test_stop_is_cooperative_and_resume_preserves_journal(setup_worker):
    make, _, _, _ = setup_worker
    runtime = BlockingRuntime()
    worker = make(runtime)
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "first"}))
    assert runtime.entered.wait(2)
    _, journal, cancellation = runtime.calls[0]
    journal.record_step({"action": "read_file", "path": "a"}, {"ok": True})
    worker.handle(command("stop_task"))
    assert cancellation.is_set()
    terminal = wait_for(worker, "task_blocked")
    assert terminal["payload"]["status"] == "interrupted"
    assert TaskJournal.load(journal.path).status is AgentStatus.INTERRUPTED
    worker.task_thread.join(2)
    runtime.entered.clear()
    worker.handle(command("resume_task", {"workspace_id": "one"}))
    assert runtime.entered.wait(2)
    resumed = runtime.calls[-1][1]
    assert resumed.path == journal.path
    assert resumed.steps == [{"action": {"action": "read_file", "path": "a"}, "result": {"ok": True}}]
    assert wait_for(worker, "task_resumed")["payload"]["state"] == "working"


def test_resume_after_restart_selects_matching_nested_journal(setup_worker):
    make, root, workspace, _ = setup_worker
    journal = TaskJournal.new("resume me", str(workspace), "old", root / "tasks" / "journals" / "email")
    journal.status = AgentStatus.INTERRUPTED
    journal.save()
    TaskJournal.new("other workspace", "/other", "old", root / "tasks" / "journals")
    runtime = BlockingRuntime()
    worker = make(runtime)
    worker.handle(command("resume_task", {"workspace_id": "one"}))
    assert runtime.entered.wait(2)
    assert runtime.calls[0][1].path == journal.path


def test_resume_without_journal_is_typed_error(setup_worker):
    make, _, _, _ = setup_worker
    worker = make(BlockingRuntime())
    worker.handle(command("resume_task", {"workspace_id": "one"}))
    assert events(worker)[-1]["payload"]["code"] == "no_resumable_task"


def test_unsupported_approval_kind_is_rejected_without_git_effects(setup_worker):
    make, root, workspace, _ = setup_worker
    worker = make()
    worker.state = "working"
    worker.journal = TaskJournal.new("review", str(workspace), "test", root / "tasks" / "journals")
    result = []
    request_thread = threading.Thread(
        target=lambda: result.append(worker._request_approval({"kind": "git_commit"})),
    )

    try:
        request_thread.start()
        request_thread.join(0.2)
        assert not request_thread.is_alive()
        assert result == [False]
        assert worker.state == "working"
        assert worker._request_id is None
        assert not any(event["type"] == "approval_required" for event in events(worker))
        assert worker.journal.commit_approved is False
        assert worker.journal.push_approved is False
    finally:
        worker.cancel_event.set()
        with worker._approval_condition:
            worker._approval_condition.notify_all()
        request_thread.join(1)


@pytest.mark.parametrize("approved, terminal, status", [(True, "task_completed", "done"), (False, "task_blocked", "blocked")])
def test_approval_matches_current_request_and_never_grants_git_authority(setup_worker, monkeypatch, approved, terminal, status):
    make, _, _, _ = setup_worker

    class Controller:
        def run(self):
            return type("Result", (), {"status": AgentStatus.READY_FOR_APPROVAL})()

    monkeypatch.setattr(task_runtime, "build_controller", lambda **kwargs: Controller())
    worker = make()
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "review"}))
    request = wait_for(worker, "approval_required")["payload"]["request_id"]
    worker.handle(command("approve", {"request_id": "stale", "approved": True}))
    assert events(worker)[-1]["payload"]["code"] == "approval_mismatch"
    worker.handle(command("status"))
    assert events(worker)[-1]["payload"]["state"] == "awaiting_approval"
    worker.handle(command("approve", {"request_id": request, "approved": approved}))
    wait_for(worker, terminal)
    worker.task_thread.join(2)
    journal = TaskJournal.load(worker.journal.path)
    assert journal.status.value == status
    assert journal.commit_approved is False
    assert journal.push_approved is False
    worker.handle(command("approve", {"request_id": request, "approved": approved}))
    assert events(worker)[-1]["payload"]["code"] == "approval_mismatch"


def test_stop_wakes_approval_wait_without_resolving_as_approved(setup_worker, monkeypatch):
    make, _, _, _ = setup_worker

    class Controller:
        def run(self):
            return type("Result", (), {"status": AgentStatus.READY_FOR_APPROVAL})()

    monkeypatch.setattr(task_runtime, "build_controller", lambda **kwargs: Controller())
    worker = make()
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "review"}))
    wait_for(worker, "approval_required")
    worker.handle(command("stop_task"))
    assert wait_for(worker, "task_blocked")["payload"]["status"] == "interrupted"
    assert not any(event["type"] == "task_completed" for event in events(worker))


def test_task_exception_is_redacted_and_protocol_survives(setup_worker, monkeypatch):
    make, root, _, config = setup_worker
    monkeypatch.setenv("TEST_PROVIDER_KEY", "hidden-provider-value")
    config["local_codex"]["tornado"] = {"providers": [{"api_key_env": "TEST_PROVIDER_KEY"}]}

    class BrokenRuntime:
        def run_task(self, *args, status_callback, **kwargs):
            status_callback("message hidden-provider-value")
            raise RuntimeError("failed hidden-provider-value")

    worker = make(BrokenRuntime())
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "first"}))
    wait_for(worker, "task_failed")
    worker.handle(command("status"))
    assert events(worker)[-1]["payload"]["state"] == "failed"
    assert "hidden-provider-value" not in worker.output_stream.getvalue()
    assert "hidden-provider-value" not in (root / "logs" / "local-codex.log").read_text()
    assert "[REDACTED]" in worker.output_stream.getvalue()


def test_worker_redacts_sensitive_prompt_sections_before_stdout_and_disk(setup_worker):
    make, root, _, _ = setup_worker
    worker = make()
    worker.emit("log", payload={"sensitive": True, "prompt": "private prompt"})
    assert "private prompt" not in worker.output_stream.getvalue()
    assert "private prompt" not in (root / "logs" / "local-codex.log").read_text()
    assert "[REDACTED]" in worker.output_stream.getvalue()


def test_shutdown_cancels_task_and_rejects_further_start(setup_worker):
    make, _, _, _ = setup_worker
    runtime = BlockingRuntime()
    worker = make(runtime)
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "first"}))
    assert runtime.entered.wait(2)
    worker.handle(command("shutdown"))
    assert runtime.calls[0][2].is_set()
    wait_for(worker, "worker_stopped")
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "second"}))
    assert events(worker)[-1]["payload"]["code"] == "shutting_down"
    assert len(runtime.calls) == 1


def test_malformed_input_does_not_end_protocol_loop(setup_worker):
    make, _, _, _ = setup_worker
    worker = make()
    worker.input_stream = io.StringIO("not json\n" + json.dumps({"protocol_version": 1, "command_id": "s", "type": "status", "payload": {}}) + "\n")
    worker.run()
    assert [event["type"] for event in events(worker)] == [
        "worker_started", "command_error", "status_snapshot", "worker_stopping", "worker_stopped",
    ]
    assert events(worker)[1]["payload"]["code"] == "invalid_command"


def test_heartbeat_emits_while_stdin_is_blocked(setup_worker):
    make, _, _, _ = setup_worker
    worker = make(heartbeat_seconds=0.01)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd) as reader, os.fdopen(write_fd, "w") as writer:
        worker.input_stream = reader
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        heartbeat = wait_for(worker, "worker_heartbeat")
        assert heartbeat["payload"]["state"] == "idle"
        writer.write(json.dumps({"protocol_version": 1, "command_id": "x", "type": "shutdown", "payload": {}}) + "\n")
        writer.flush()
        thread.join(2)
        assert not thread.is_alive()


def test_events_remain_monotonic_and_parseable_across_threads(setup_worker):
    make, _, _, _ = setup_worker

    class ChattyRuntime:
        def run_task(self, *args, status_callback, **kwargs):
            threads = [threading.Thread(target=lambda: [status_callback("hello") for _ in range(30)]) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            return AgentStatus.DONE

    worker = make(ChattyRuntime())
    worker.handle(command("submit_task", {"workspace_id": "one", "prompt": "first"}))
    for _ in range(30):
        worker.handle(command("status"))
    wait_for(worker, "task_completed")
    emitted = events(worker)
    assert [event["sequence"] for event in emitted] == list(range(1, len(emitted) + 1))
    assert len([event for event in emitted if event["type"] == "log"]) == 120


def test_log_persistence_warning_uses_a_new_monotonic_sequence(setup_worker, monkeypatch):
    make, _, _, _ = setup_worker
    worker = make()
    monkeypatch.setattr(worker._log, "append", lambda event: EventLog._warning(event))

    worker.emit("log", payload={"message": "not persisted"})

    emitted = events(worker)
    assert [event["type"] for event in emitted] == ["log", "log_warning"]
    assert [event["sequence"] for event in emitted] == [1, 2]


def test_module_headless_stdout_is_only_protocol_v1(tmp_path):
    lines = [json.dumps({"protocol_version": 1, "command_id": kind, "type": kind, "payload": {}}) for kind in ("status", "shutdown")]
    result = subprocess.run(
        [sys.executable, "-m", "hcs_ai.local_codex.worker", "--data-root", str(tmp_path / "local_codex")],
        input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=10,
        env={**os.environ, "DISPLAY": ""},
    )
    assert result.returncode == 0, result.stderr
    emitted = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["type"] for event in emitted] == ["worker_started", "status_snapshot", "worker_stopping", "worker_stopped"]
    assert all(event["protocol_version"] == 1 for event in emitted)
    assert result.stderr == ""
