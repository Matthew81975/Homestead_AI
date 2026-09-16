from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    QUEUED = "queued"
    WAITING_CONFIRMATION = "waiting_confirmation"
    ACTIVE = "active"
    PAUSED = "paused"
    READY_FOR_FINAL_APPROVAL = "ready_for_final_approval"
    FINALIZING = "finalizing"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    DONE = "done"


class ApprovalStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TaskControl(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


def _normalize_alias(value: str) -> str:
    return " ".join(value.strip().lower().split())


@dataclass(eq=True)
class WorkspaceConfig:
    workspace_id: str
    name: str
    aliases: list[str]
    path: str
    target_branch: str
    test_commands: list[str] = field(default_factory=list)
    allowed_run_commands: list[str] = field(default_factory=list)
    git_enabled: bool = True
    enabled: bool = True
    notes: str = ""
    auto_update: bool = False
    github_repository: str | None = None
    handoff_enabled: bool | None = None
    handoff_issue_number: int | None = None
    control_api_url: str | None = None

    def __post_init__(self) -> None:
        repository = self.github_repository.strip() if self.github_repository else ""
        if repository:
            parts = repository.split("/")
            if len(parts) != 2 or not all(parts):
                raise ValueError("github_repository must use owner/name format")
            self.github_repository = repository
        else:
            self.github_repository = None

        if self.handoff_issue_number is not None and self.handoff_issue_number < 1:
            raise ValueError("handoff_issue_number must be positive")

        if self.github_repository is None:
            self.handoff_enabled = False
        elif self.handoff_enabled is None:
            self.handoff_enabled = True

    def normalized_aliases(self) -> set[str]:
        return {_normalize_alias(self.name), *(_normalize_alias(alias) for alias in self.aliases)}


@dataclass(eq=True)
class TaskRecord:
    task_id: str
    thread_id: str | None
    sender: str
    subject: str
    body: str
    workspace_id: str | None
    status: TaskStatus
    approval_status: ApprovalStatus
    created_at: datetime
    confirmation_sent_at: datetime | None = None
    approved_at: datetime | None = None
    updated_at: datetime | None = None
    task_branch: str | None = None
    target_branch: str | None = None
    expected_target_sha: str | None = None
    changed_files: list[str] = field(default_factory=list)
    tests_run: list[dict[str, Any]] = field(default_factory=list)
    diff_summary: str = ""
    approval_history: list[dict[str, Any]] = field(default_factory=list)
    freeform_instructions: list[str] = field(default_factory=list)
    allow_interactive_launch: bool = False
    finalization: dict[str, Any] = field(default_factory=dict)
    journal_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["approval_status"] = self.approval_status.value
        for key in ("created_at", "confirmation_sent_at", "approved_at", "updated_at"):
            value = getattr(self, key)
            data[key] = value.isoformat() if value is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        values = dict(data)
        values["status"] = TaskStatus(values["status"])
        values["approval_status"] = ApprovalStatus(values["approval_status"])
        for key in ("created_at", "confirmation_sent_at", "approved_at", "updated_at"):
            value = values.get(key)
            if isinstance(value, str):
                values[key] = datetime.fromisoformat(value)
        return cls(**values)


@dataclass(eq=True)
class MailMessage:
    sender: str
    subject: str
    body: str
    message_id: str
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    sent_at: datetime | None = None
