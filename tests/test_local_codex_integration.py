import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


def command(command_id, kind, payload=None):
    return json.dumps({
        "protocol_version": 1,
        "command_id": command_id,
        "type": kind,
        "payload": payload or {},
    })


def test_real_worker_status_fake_task_order_and_clean_shutdown(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_root = tmp_path / "local_codex"
    registry = data_root / "workspaces" / "workspaces.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"workspaces": [{
        "workspace_id": "test", "name": "Test", "aliases": ["test"],
        "path": str(workspace), "target_branch": "main",
    }]}), encoding="utf-8")

    process = subprocess.Popen(
        [sys.executable, "-m", "hcs_ai.local_codex.worker", "--data-root", str(data_root), "--test-mode"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True)
    reader.start()

    def next_event(timeout=5):
        return json.loads(lines.get(timeout=timeout))

    def send(line):
        process.stdin.write(line + "\n")
        process.stdin.flush()

    try:
        events = [next_event()]
        assert events[0]["type"] == "worker_started"

        send(command("status-1", "status"))
        events.append(next_event())
        assert events[-1]["type"] == "status_snapshot"

        send(command("task-1", "submit_task", {"workspace_id": "test", "prompt": "integration"}))
        deadline = time.monotonic() + 5
        while not any(event["type"] == "task_completed" for event in events):
            events.append(next_event(max(0.1, deadline - time.monotonic())))

        send(command("shutdown-1", "shutdown"))
        while not any(event["type"] == "worker_stopped" for event in events):
            events.append(next_event())
        process.stdin.close()
        assert process.wait(timeout=5) == 0

        assert [event["sequence"] for event in events] == sorted(
            event["sequence"] for event in events
        )
        event_types = [event["type"] for event in events]
        assert event_types.index("task_started") < event_types.index("log") < event_types.index("task_completed")
        assert event_types[-2:] == ["worker_stopping", "worker_stopped"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
