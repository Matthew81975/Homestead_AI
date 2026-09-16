from datetime import datetime, timedelta, timezone
from pathlib import Path

from hcs_ai.local_codex.models import ApprovalStatus, TaskRecord, TaskStatus
from hcs_ai.local_codex.task_queue import TaskQueue


def make_task(task_id: str, workspace_id: str, status=TaskStatus.QUEUED) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        thread_id=f"thread-{task_id}",
        sender="schoolfieldmatt@gmail.com",
        subject=f"TASK: {workspace_id}",
        body="Do thing",
        workspace_id=workspace_id,
        status=status,
        approval_status=ApprovalStatus.NOT_REQUESTED,
        created_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )


def test_one_active_task_per_workspace(tmp_path: Path):
    queue = TaskQueue.load(tmp_path / "tasks.json")
    queue.enqueue(make_task("1", "maze_world"))
    queue.enqueue(make_task("2", "maze_world"))
    queue.activate("1")
    assert queue.can_activate("1") is True
    assert queue.can_activate("2") is False


def test_different_workspaces_can_be_active(tmp_path: Path):
    queue = TaskQueue.load(tmp_path / "tasks.json")
    queue.enqueue(make_task("1", "maze_world"))
    queue.enqueue(make_task("2", "hcs"))
    queue.activate("1")
    queue.activate("2")
    assert queue.get("1").status is TaskStatus.ACTIVE
    assert queue.get("2").status is TaskStatus.ACTIVE


def test_busy_workspace_task_not_confirmable_until_released(tmp_path: Path):
    queue = TaskQueue.load(tmp_path / "tasks.json")
    queue.enqueue(make_task("1", "maze_world"))
    queue.enqueue(make_task("2", "maze_world"))
    queue.activate("1")
    assert queue.next_confirmable("maze_world") is None
    queue.release_workspace("maze_world")
    assert queue.next_confirmable("maze_world").task_id == "2"


def test_confirmation_expires_at_24_hours(tmp_path: Path):
    queue = TaskQueue.load(tmp_path / "tasks.json")
    queue.enqueue(make_task("42", "maze_world"))
    sent = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    queue.mark_confirmation_sent("42", sent)
    assert queue.expire_confirmations(sent + timedelta(hours=23, minutes=59)) == []
    assert queue.expire_confirmations(sent + timedelta(hours=24)) == ["42"]
    assert queue.get("42").approval_status is ApprovalStatus.EXPIRED


def test_queue_persists_across_reload(tmp_path: Path):
    path = tmp_path / "tasks.json"
    queue = TaskQueue.load(path)
    queue.enqueue(make_task("42", "maze_world"))
    queue.mark_confirmation_sent("42", datetime(2026, 9, 7, 12, tzinfo=timezone.utc))
    restored = TaskQueue.load(path)
    assert restored.get("42").status is TaskStatus.WAITING_CONFIRMATION
    assert restored.get("42").workspace_id == "maze_world"


def test_pause_resume_cancel_preserve_task_record(tmp_path: Path):
    queue = TaskQueue.load(tmp_path / "tasks.json")
    queue.enqueue(make_task("42", "maze_world"))
    queue.activate("42")
    queue.pause("42")
    assert queue.get("42").status is TaskStatus.PAUSED
    queue.resume("42")
    assert queue.get("42").status is TaskStatus.ACTIVE
    queue.cancel("42")
    assert queue.get("42").status is TaskStatus.CANCELLED


def test_rebuild_workspace_locks_from_persisted_active_states(tmp_path: Path):
    path = tmp_path / "tasks.json"
    queue = TaskQueue.load(path)
    active = make_task("1", "maze_world")
    paused = make_task("2", "hcs")
    queue.enqueue(active)
    queue.enqueue(paused)
    queue.activate("1")
    queue.activate("2")
    queue.pause("2")

    restored = TaskQueue.load(path)
    restored.workspace_locks = {}
    restored.rebuild_workspace_locks()

    assert restored.workspace_locks == {"maze_world": "1", "hcs": "2"}
