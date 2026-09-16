from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


class AgentStatus(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    TESTING = "testing"
    READY_FOR_APPROVAL = "ready_for_approval"
    COMMIT_APPROVAL = "commit_approval"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"


@dataclass
class TaskJournal:
    task: str
    workspace: str
    model: str
    path: Path
    status: AgentStatus = AgentStatus.WORKING
    steps: list[dict[str, Any]] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    tests_run: list[dict[str, Any]] = field(default_factory=list)
    last_action: dict[str, Any] | None = None
    original_files: dict[str, str] = field(default_factory=dict)
    consecutive_failures: int = 0
    commit_approved: bool = False
    push_approved: bool = False

    @classmethod
    def new(cls, task: str, workspace: str, model: str, log_dir: Path) -> "TaskJournal":
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        path = log_dir / f"task_{stamp}_{uuid4().hex[:8]}.json"
        journal = cls(task=task, workspace=workspace, model=model, path=path)
        journal.save()
        return journal

    @classmethod
    def load(cls, path: Path) -> "TaskJournal":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = AgentStatus(data["status"])
        data["path"] = path
        return cls(**data)

    def save(self) -> None:
        data = asdict(self)
        data.pop("path")
        data["status"] = self.status.value
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def record_step(self, action: dict[str, Any], result: dict[str, Any]) -> None:
        self.last_action = action
        self.steps.append({"action": action, "result": result})
        self.save()

    def snapshot_file(self, relative_path: str, contents: str) -> None:
        self.original_files.setdefault(relative_path, contents)
        self.save()

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.save()

    def clear_failures(self) -> None:
        self.consecutive_failures = 0
        self.save()


def find_resumable_journals(log_dir: Path) -> list[TaskJournal]:
    result: list[TaskJournal] = []
    for path in sorted(log_dir.glob("task_*.json"), reverse=True):
        journal = TaskJournal.load(path)
        if journal.status in {
            AgentStatus.WORKING,
            AgentStatus.TESTING,
            AgentStatus.INTERRUPTED,
            AgentStatus.BLOCKED,
        }:
            result.append(journal)
    return result
