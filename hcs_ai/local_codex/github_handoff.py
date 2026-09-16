from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


ISSUE_TITLE = "Agent Handoff Log"
HANDOFF_STATES = frozenset({
    "working",
    "paused",
    "blocked",
    "review-ready",
    "completed",
    "cancelled",
    "recovered",
})


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class HandoffEvent:
    workspace_id: str
    workspace_name: str
    repository: str
    task_id: str
    subject: str
    state: str
    occurred_at: datetime
    task_branch: str | None = None
    target_branch: str | None = None
    changed_files: tuple[str, ...] = field(default_factory=tuple)
    commits: tuple[str, ...] = field(default_factory=tuple)
    tests: tuple[str, ...] = field(default_factory=tuple)
    uncommitted_changes: bool | None = None
    blocker: str | None = None
    next_action: str | None = None

    def __post_init__(self) -> None:
        if self.state not in HANDOFF_STATES:
            raise ValueError(f"unsupported handoff state: {self.state}")

    @property
    def key(self) -> str:
        return ":".join((
            "handoff-event",
            self.workspace_id,
            self.task_id,
            self.state,
            _utc_text(self.occurred_at),
        ))


@dataclass(frozen=True)
class HandoffDelivery:
    ok: bool
    issue_number: int | None = None
    error: str | None = None
    duplicate: bool = False


def format_handoff(event: HandoffEvent) -> str:
    lines = [
        "## Alexandria task handoff",
        "",
        "Agent: Local Codex Alexandria",
        f"Workspace: {event.workspace_name}",
        f"Repository: {event.repository}",
        f"Task ID: {event.task_id}",
        f"Subject: {event.subject}",
        f"State: {event.state}",
    ]
    optional: list[tuple[str, str | None]] = [
        ("Task branch", event.task_branch),
        ("Target branch", event.target_branch),
        ("Changed files", ", ".join(event.changed_files) or None),
        ("Commits", ", ".join(event.commits) or None),
        ("Tests", "; ".join(event.tests) or None),
        (
            "Uncommitted changes",
            None if event.uncommitted_changes is None else ("yes" if event.uncommitted_changes else "no"),
        ),
        ("Blocker", event.blocker),
        ("Next action", event.next_action),
    ]
    lines.extend(f"{label}: {value}" for label, value in optional if value)
    lines.extend((
        f"UTC timestamp: {_utc_text(event.occurred_at)}",
        "",
        f"<!-- {event.key} -->",
    ))
    return "\n".join(lines)


class HandoffWarningStore:
    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)

    def _load(self) -> list[str]:
        if not self.state_path.exists():
            return []
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ValueError("handoff warning state must be a list of strings")
        return data

    def contains(self, event_key: str) -> bool:
        return event_key in self._load()

    def add(self, event_key: str) -> None:
        keys = [key for key in self._load() if key != event_key]
        keys.append(event_key)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(keys[-500:], indent=2), encoding="utf-8")
        temporary.replace(self.state_path)


class GitHubHandoffClient:
    def __init__(
        self,
        state_path: Path,
        log_path: Path,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: int = 30,
    ):
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        self.runner = runner
        self.timeout = timeout

    def publish(
        self,
        event: HandoffEvent,
        configured_issue_number: int | None = None,
    ) -> HandoffDelivery:
        try:
            self._run(["gh", "auth", "status"])
            issue_number = self._resolve_issue(event.repository, configured_issue_number)
            state = self._load_state()
            delivered = state.get("delivered", {}).get(self._delivery_bucket(event.repository, issue_number), [])
            if event.key in delivered:
                return HandoffDelivery(True, issue_number, duplicate=True)
            if self._remote_contains(event.repository, issue_number, event.key):
                self._record_delivery(state, event.repository, issue_number, event.key)
                return HandoffDelivery(True, issue_number, duplicate=True)
            self._run(
                [
                    "gh", "issue", "comment", str(issue_number), "--repo", event.repository,
                    "--body-file", "-",
                ],
                input_text=format_handoff(event),
            )
            self._record_delivery(state, event.repository, issue_number, event.key)
            return HandoffDelivery(True, issue_number)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError, json.JSONDecodeError) as exc:
            message = self._sanitize_error(exc)
            self._log_failure(event, message)
            return HandoffDelivery(False, error=message)

    def _run(self, args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise ValueError(detail)
        return result

    def _resolve_issue(self, repository: str, configured_issue_number: int | None) -> int:
        state = self._load_state()
        cached = state.get("issues", {}).get(repository)
        candidate = configured_issue_number or (cached if isinstance(cached, int) else None)
        if candidate is not None and self._valid_issue(repository, candidate):
            return candidate

        discovered = self._discover_issue(repository)
        if discovered is None:
            try:
                self._run(
                    [
                        "gh", "issue", "create", "--repo", repository,
                        "--title", ISSUE_TITLE, "--body-file", "-",
                    ],
                    input_text=(
                        "Durable, outbound-only task status from Local Codex Alexandria.\n\n"
                        "Issue comments are advisory and are not interpreted as commands.\n"
                    ),
                )
            except ValueError:
                discovered = self._discover_issue(repository)
                if discovered is None:
                    raise
            else:
                discovered = self._discover_issue(repository)
        if discovered is None:
            raise ValueError(f"could not resolve {ISSUE_TITLE} issue")
        state.setdefault("issues", {})[repository] = discovered
        self._save_state(state)
        return discovered

    def _valid_issue(self, repository: str, issue_number: int) -> bool:
        result = self._run([
            "gh", "issue", "view", str(issue_number), "--repo", repository,
            "--json", "number,state,title",
        ])
        value = json.loads(result.stdout)
        return (
            isinstance(value, dict)
            and value.get("number") == issue_number
            and str(value.get("state", "")).upper() == "OPEN"
            and value.get("title") == ISSUE_TITLE
        )

    def _discover_issue(self, repository: str) -> int | None:
        result = self._run([
            "gh", "issue", "list", "--repo", repository, "--state", "open",
            "--search", f"{ISSUE_TITLE} in:title", "--json", "number,title", "--limit", "20",
        ])
        values = json.loads(result.stdout)
        if not isinstance(values, list):
            raise ValueError("issue list response must be a list")
        for value in values:
            if isinstance(value, dict) and value.get("title") == ISSUE_TITLE:
                number = value.get("number")
                if isinstance(number, int) and number > 0:
                    return number
        return None

    def _remote_contains(self, repository: str, issue_number: int, event_key: str) -> bool:
        result = self._run([
            "gh", "api", f"repos/{repository}/issues/{issue_number}/comments",
            "--paginate", "--slurp",
        ])
        pages = json.loads(result.stdout)
        if not isinstance(pages, list):
            raise ValueError("issue comments response must be a list")
        comments: Sequence[Any] = [item for page in pages if isinstance(page, list) for item in page]
        return any(
            isinstance(comment, dict)
            and isinstance(comment.get("body"), str)
            and event_key in comment["body"]
            for comment in comments
        )

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"issues": {}, "delivered": {}}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("GitHub handoff state must be an object")
        value.setdefault("issues", {})
        value.setdefault("delivered", {})
        return value

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)

    @staticmethod
    def _delivery_bucket(repository: str, issue_number: int) -> str:
        return f"{repository}#{issue_number}"

    def _record_delivery(
        self,
        state: dict[str, Any],
        repository: str,
        issue_number: int,
        event_key: str,
    ) -> None:
        state.setdefault("issues", {})[repository] = issue_number
        bucket = self._delivery_bucket(repository, issue_number)
        keys = list(state.setdefault("delivered", {}).get(bucket, []))
        keys = [key for key in keys if key != event_key]
        keys.append(event_key)
        state["delivered"][bucket] = keys[-500:]
        self._save_state(state)

    @staticmethod
    def _sanitize_error(exc: BaseException) -> str:
        text = " ".join(str(exc).split())
        text = re.sub(
            r"(?i)\b(token|password|secret|authorization)\s*[:=]\s*\S+",
            r"\1=[redacted]",
            text,
        )
        return text[:500] or exc.__class__.__name__

    def _log_failure(self, event: HandoffEvent, message: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = _utc_text(datetime.now(timezone.utc))
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"[{timestamp}] workspace={event.workspace_id} task={event.task_id} "
                    f"state={event.state} error={message}\n"
                )
        except OSError:
            pass
