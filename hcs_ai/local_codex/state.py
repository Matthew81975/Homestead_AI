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
    recovery_cycles: int = 0
    recovery_attempts: list[dict[str, Any]] = field(default_factory=list)
    recovery_strategies_attempted: list[str] = field(default_factory=list)
    last_failure_category: str | None = None
    last_recovery_reason: str | None = None
    task_nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_task_node: str | None = "root"
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

    def ensure_task_tree(self) -> None:
        if self.task_nodes:
            return
        self.task_nodes = {
            "root": {
                "id": "root",
                "prompt": self.task,
                "status": "active",
                "parent": None,
                "children": [],
            }
        }
        self.active_task_node = "root"
        self.save()

    def active_task_prompt(self) -> str:
        self.ensure_task_tree()
        if self.active_task_node is None:
            return self.task
        node = self.task_nodes.get(self.active_task_node)
        return str(node.get("prompt") if node else self.task)

    def subdivide_active_task(self, prompts: list[str]) -> list[str]:
        self.ensure_task_tree()
        if self.active_task_node is None:
            raise ValueError("no active task to subdivide")
        cleaned = [str(item).strip() for item in prompts if str(item).strip()]
        if len(cleaned) < 2:
            raise ValueError("subdivision requires at least two non-empty child tasks")

        parent_id = self.active_task_node
        parent = self.task_nodes[parent_id]
        if parent.get("children"):
            raise ValueError("active task is already subdivided")

        child_ids: list[str] = []
        for index, prompt in enumerate(cleaned, start=1):
            child_id = f"{parent_id}.{index}"
            while child_id in self.task_nodes:
                index += 1
                child_id = f"{parent_id}.{index}"
            self.task_nodes[child_id] = {
                "id": child_id,
                "prompt": prompt,
                "status": "pending",
                "parent": parent_id,
                "children": [],
            }
            child_ids.append(child_id)

        parent["children"] = child_ids
        parent["status"] = "waiting_children"
        self.task_nodes[child_ids[0]]["status"] = "active"
        self.active_task_node = child_ids[0]
        self.save()
        return child_ids

    def complete_active_task(self) -> str | None:
        self.ensure_task_tree()
        current_id = self.active_task_node
        if current_id is None:
            return None
        self.task_nodes[current_id]["status"] = "completed"

        node_id = current_id
        while True:
            node = self.task_nodes[node_id]
            parent_id = node.get("parent")
            if parent_id is None:
                self.active_task_node = None
                self.save()
                return None

            parent = self.task_nodes[parent_id]
            siblings = list(parent.get("children", []))
            position = siblings.index(node_id)
            for sibling_id in siblings[position + 1:]:
                sibling = self.task_nodes[sibling_id]
                if sibling.get("status") == "pending":
                    sibling["status"] = "active"
                    self.active_task_node = sibling_id
                    self.save()
                    return str(sibling.get("prompt", ""))

            parent["status"] = "completed"
            node_id = parent_id

    def task_progress(self) -> dict[str, Any]:
        self.ensure_task_tree()
        nodes = list(self.task_nodes.values())
        leaves = [node for node in nodes if not node.get("children")]
        completed = sum(1 for node in leaves if node.get("status") == "completed")
        total = len(leaves)
        return {
            "active_id": self.active_task_node,
            "active_prompt": self.active_task_prompt() if self.active_task_node else None,
            "completed_leaves": completed,
            "total_leaves": total,
            "fraction": 1.0 if total == 0 else completed / total,
        }

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

    def record_recovery(self, *, category: str, strategy: str, reason: str) -> None:
        self.recovery_cycles += 1
        self.last_failure_category = category
        self.last_recovery_reason = reason
        self.recovery_strategies_attempted.append(strategy)
        self.recovery_attempts.append({
            "cycle": self.recovery_cycles,
            "category": category,
            "strategy": strategy,
            "reason": reason,
            "at": datetime.now().isoformat(),
        })
        self.save()

    def clear_recovery(self) -> None:
        self.recovery_cycles = 0
        self.recovery_attempts.clear()
        self.recovery_strategies_attempted.clear()
        self.last_failure_category = None
        self.last_recovery_reason = None
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
