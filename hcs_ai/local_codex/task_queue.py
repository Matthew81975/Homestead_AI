from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ApprovalStatus, TaskRecord, TaskStatus


class TaskQueue:
    def __init__(self, path: Path, tasks: dict[str, TaskRecord] | None = None, locks: dict[str, str] | None = None):
        self.path = path
        self.tasks = tasks or {}
        self.workspace_locks = locks or {}

    @classmethod
    def load(cls, path: Path) -> "TaskQueue":
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = {
            task_id: TaskRecord.from_dict(task_data)
            for task_id, task_data in data.get("tasks", {}).items()
        }
        locks = dict(data.get("workspace_locks", {}))
        return cls(path, tasks, locks)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "tasks": {task_id: task.to_dict() for task_id, task in self.tasks.items()},
            "workspace_locks": self.workspace_locks,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def save(self) -> None:
        self._save()

    def get(self, task_id: str) -> TaskRecord:
        return self.tasks[task_id]

    def find_by_thread(self, message_ids: list[str]) -> TaskRecord | None:
        wanted = {value for value in message_ids if value}
        for task in self.tasks.values():
            if task.thread_id and task.thread_id in wanted:
                return task
        return None


    def enqueue(self, task: TaskRecord) -> None:
        if task.task_id in self.tasks:
            raise ValueError(f"duplicate task id: {task.task_id}")
        if task.status not in {TaskStatus.QUEUED, TaskStatus.WAITING_CONFIRMATION}:
            task.status = TaskStatus.QUEUED
        self.tasks[task.task_id] = task
        self._save()

    def can_activate(self, task_id: str) -> bool:
        task = self.get(task_id)
        if not task.workspace_id:
            return False
        owner = self.workspace_locks.get(task.workspace_id)
        return owner is None or owner == task_id

    def activate(self, task_id: str) -> None:
        task = self.get(task_id)
        if not task.workspace_id:
            raise ValueError("task has no workspace")
        if not self.can_activate(task_id):
            raise RuntimeError(f"workspace is busy: {task.workspace_id}")
        self.workspace_locks[task.workspace_id] = task_id
        task.status = TaskStatus.ACTIVE
        task.updated_at = datetime.now(timezone.utc)
        self._save()

    def next_confirmable(self, workspace_id: str) -> TaskRecord | None:
        if workspace_id in self.workspace_locks:
            return None
        reserved_statuses = {TaskStatus.WAITING_CONFIRMATION}
        if any(
            task.workspace_id == workspace_id and task.status in reserved_statuses
            for task in self.tasks.values()
        ):
            return None
        candidates = [
            task for task in self.tasks.values()
            if task.workspace_id == workspace_id
            and task.status is TaskStatus.QUEUED
            and task.approval_status in {ApprovalStatus.NOT_REQUESTED, ApprovalStatus.EXPIRED}
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda task: (task.created_at, task.task_id))[0]

    def mark_confirmation_sent(self, task_id: str, sent_at: datetime) -> None:
        task = self.get(task_id)
        task.status = TaskStatus.WAITING_CONFIRMATION
        task.approval_status = ApprovalStatus.PENDING
        task.confirmation_sent_at = sent_at
        task.updated_at = sent_at
        self._save()

    def approve(self, task_id: str, approved_at: datetime) -> None:
        task = self.get(task_id)
        if task.approval_status is not ApprovalStatus.PENDING:
            raise RuntimeError("task is not waiting for approval")
        if task.confirmation_sent_at is None or approved_at >= task.confirmation_sent_at + timedelta(hours=24):
            task.approval_status = ApprovalStatus.EXPIRED
            task.status = TaskStatus.QUEUED
            self._save()
            raise RuntimeError("task approval has expired")
        task.approval_status = ApprovalStatus.APPROVED
        task.approved_at = approved_at
        task.approval_history.append({"decision": "approved", "at": approved_at.isoformat()})
        self._save()

    def reject(self, task_id: str, rejected_at: datetime | None = None) -> None:
        task = self.get(task_id)
        when = rejected_at or datetime.now(timezone.utc)
        task.status = TaskStatus.REJECTED
        task.approval_status = ApprovalStatus.REJECTED
        task.approval_history.append({"decision": "rejected", "at": when.isoformat()})
        if task.workspace_id and self.workspace_locks.get(task.workspace_id) == task_id:
            self.workspace_locks.pop(task.workspace_id, None)
        self._save()

    def pause(self, task_id: str) -> None:
        task = self.get(task_id)
        if task.status is not TaskStatus.ACTIVE:
            raise RuntimeError("only active tasks can be paused")
        task.status = TaskStatus.PAUSED
        task.updated_at = datetime.now(timezone.utc)
        self._save()

    def resume(self, task_id: str) -> None:
        task = self.get(task_id)
        if task.status is not TaskStatus.PAUSED:
            raise RuntimeError("only paused tasks can be resumed")
        if not self.can_activate(task_id):
            raise RuntimeError("workspace is busy")
        if task.workspace_id:
            self.workspace_locks[task.workspace_id] = task_id
        task.status = TaskStatus.ACTIVE
        task.updated_at = datetime.now(timezone.utc)
        self._save()

    def cancel(self, task_id: str) -> None:
        task = self.get(task_id)
        task.status = TaskStatus.CANCELLED
        task.updated_at = datetime.now(timezone.utc)
        if task.workspace_id and self.workspace_locks.get(task.workspace_id) == task_id:
            self.workspace_locks.pop(task.workspace_id, None)
        self._save()

    def release_workspace(self, workspace_id: str) -> None:
        self.workspace_locks.pop(workspace_id, None)
        self._save()

    def rebuild_workspace_locks(self) -> None:
        self.workspace_locks = {}
        locking_statuses = {
            TaskStatus.ACTIVE,
            TaskStatus.PAUSED,
            TaskStatus.READY_FOR_FINAL_APPROVAL,
            TaskStatus.FINALIZING,
            TaskStatus.BLOCKED,
        }
        for task in sorted(self.tasks.values(), key=lambda item: (item.created_at, item.task_id)):
            if task.workspace_id and task.status in locking_statuses:
                self.workspace_locks.setdefault(task.workspace_id, task.task_id)
        self._save()

    def expire_confirmations(self, now: datetime) -> list[str]:
        expired: list[str] = []
        for task in self.tasks.values():
            if (
                task.status is TaskStatus.WAITING_CONFIRMATION
                and task.approval_status is ApprovalStatus.PENDING
                and task.confirmation_sent_at is not None
                and now >= task.confirmation_sent_at + timedelta(hours=24)
            ):
                task.status = TaskStatus.QUEUED
                task.approval_status = ApprovalStatus.EXPIRED
                task.updated_at = now
                expired.append(task.task_id)
        if expired:
            self._save()
        return expired
