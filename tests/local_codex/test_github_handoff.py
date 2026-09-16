import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hcs_ai.local_codex.github_handoff import (
    HANDOFF_STATES,
    GitHubHandoffClient,
    HandoffDelivery,
    HandoffEvent,
    HandoffWarningStore,
    format_handoff,
)


REPOSITORY = "Matthew81975/Maze_World"


def make_event(state: str = "blocked", *, seconds: int = 0) -> HandoffEvent:
    return HandoffEvent(
        workspace_id="maze_world",
        workspace_name="Maze World",
        repository=REPOSITORY,
        task_id="abc12345",
        subject="TASK: ramps $(not-shell)",
        state=state,
        occurred_at=datetime(2026, 9, 8, 20, 0, seconds, tzinfo=timezone.utc),
        task_branch="alexandria/abc12345-ramps",
        target_branch="development",
        changed_files=("maze_world/generation.py",),
        commits=("deadbeef",),
        tests=("python -m pytest: passed",),
        uncommitted_changes=False,
        blocker="test failed" if state == "blocked" else None,
        next_action="Review the failed test.",
    )


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class ScriptedRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return completed(args, **response)


@pytest.mark.parametrize("state", sorted(HANDOFF_STATES))
def test_formatter_supports_each_lifecycle_state(state: str):
    event = make_event(state)

    body = format_handoff(event)

    assert "Agent: Local Codex Alexandria" in body
    assert f"State: {state}" in body
    assert "Subject: TASK: ramps $(not-shell)" in body
    assert f"handoff-event:maze_world:abc12345:{state}:2026-09-08T20:00:00Z" in body
    assert "Changed files: maze_world/generation.py" in body
    assert "Uncommitted changes: no" in body


def test_event_normalizes_time_and_rejects_unknown_state():
    local_time = datetime(2026, 9, 8, 15, 0, tzinfo=timezone(timedelta(hours=-5)))
    event = HandoffEvent(
        workspace_id="maze_world", workspace_name="Maze World", repository=REPOSITORY,
        task_id="abc12345", subject="TASK: ramps", state="working", occurred_at=local_time,
    )
    assert event.key == "handoff-event:maze_world:abc12345:working:2026-09-08T20:00:00Z"

    with pytest.raises(ValueError, match="unsupported handoff state"):
        HandoffEvent(
            workspace_id="maze_world", workspace_name="Maze World", repository=REPOSITORY,
            task_id="abc12345", subject="TASK", state="queued", occurred_at=local_time,
        )


def test_existing_exact_title_issue_is_reused_and_body_uses_stdin(tmp_path: Path):
    runner = ScriptedRunner([
        {},
        {"stdout": json.dumps([{"number": 7, "title": "Agent Handoff Log"}])},
        {"stdout": json.dumps([[]])},
        {},
    ])
    client = GitHubHandoffClient(tmp_path / "state.json", tmp_path / "handoff.log", runner=runner)

    delivery = client.publish(make_event())

    assert delivery == HandoffDelivery(ok=True, issue_number=7)
    assert runner.calls[0][0] == ["gh", "auth", "status"]
    comment_args, comment_kwargs = runner.calls[-1]
    assert comment_args == ["gh", "issue", "comment", "7", "--repo", REPOSITORY, "--body-file", "-"]
    assert "$(not-shell)" in comment_kwargs["input"]
    for args, kwargs in runner.calls:
        assert isinstance(args, list)
        assert kwargs.get("shell", False) is False
        assert "$(not-shell)" not in " ".join(args)


def test_missing_issue_is_created_then_rediscovered(tmp_path: Path):
    runner = ScriptedRunner([
        {},
        {"stdout": "[]"},
        {"stdout": "https://github.com/Matthew81975/Maze_World/issues/9\n"},
        {"stdout": json.dumps([{"number": 9, "title": "Agent Handoff Log"}])},
        {"stdout": json.dumps([[]])},
        {},
    ])
    client = GitHubHandoffClient(tmp_path / "state.json", tmp_path / "handoff.log", runner=runner)

    delivery = client.publish(make_event())

    assert delivery.issue_number == 9
    assert runner.calls[2][0] == [
        "gh", "issue", "create", "--repo", REPOSITORY,
        "--title", "Agent Handoff Log", "--body-file", "-",
    ]


def test_configured_issue_is_validated_before_reuse(tmp_path: Path):
    runner = ScriptedRunner([
        {},
        {"stdout": json.dumps({"number": 12, "state": "OPEN", "title": "Agent Handoff Log"})},
        {"stdout": json.dumps([[]])},
        {},
    ])
    client = GitHubHandoffClient(tmp_path / "state.json", tmp_path / "handoff.log", runner=runner)

    delivery = client.publish(make_event(), configured_issue_number=12)

    assert delivery.issue_number == 12
    assert runner.calls[1][0][:4] == ["gh", "issue", "view", "12"]


def test_closed_cached_issue_triggers_rediscovery(tmp_path: Path):
    (tmp_path / "state.json").write_text(json.dumps({
        "issues": {REPOSITORY: 4}, "delivered": {},
    }), encoding="utf-8")
    runner = ScriptedRunner([
        {},
        {"stdout": json.dumps({"number": 4, "state": "CLOSED", "title": "Agent Handoff Log"})},
        {"stdout": json.dumps([{"number": 7, "title": "Agent Handoff Log"}])},
        {"stdout": json.dumps([[]])},
        {},
    ])
    client = GitHubHandoffClient(tmp_path / "state.json", tmp_path / "handoff.log", runner=runner)

    assert client.publish(make_event()).issue_number == 7


def test_existing_remote_event_key_suppresses_duplicate_comment(tmp_path: Path):
    event = make_event()
    runner = ScriptedRunner([
        {},
        {"stdout": json.dumps([{"number": 7, "title": "Agent Handoff Log"}])},
        {"stdout": json.dumps([[{"body": f"status\n<!-- {event.key} -->"}]])},
    ])
    client = GitHubHandoffClient(tmp_path / "state.json", tmp_path / "handoff.log", runner=runner)

    delivery = client.publish(event)

    assert delivery == HandoffDelivery(ok=True, issue_number=7, duplicate=True)
    assert len(runner.calls) == 3


def test_local_event_cache_suppresses_repeat_without_second_comment(tmp_path: Path):
    runner = ScriptedRunner([
        {}, {"stdout": json.dumps([{"number": 7, "title": "Agent Handoff Log"}])},
        {"stdout": json.dumps([[]])}, {},
        {}, {"stdout": json.dumps({"number": 7, "state": "OPEN", "title": "Agent Handoff Log"})},
    ])
    client = GitHubHandoffClient(tmp_path / "state.json", tmp_path / "handoff.log", runner=runner)
    event = make_event()

    assert client.publish(event).duplicate is False
    assert client.publish(event).duplicate is True
    assert sum(call[0][:3] == ["gh", "issue", "comment"] for call in runner.calls) == 1


@pytest.mark.parametrize("failure", [
    FileNotFoundError("gh"),
    subprocess.TimeoutExpired(["gh"], 30),
])
def test_process_failures_return_delivery_error(tmp_path: Path, failure: BaseException):
    runner = ScriptedRunner([failure])
    client = GitHubHandoffClient(tmp_path / "state.json", tmp_path / "handoff.log", runner=runner)

    delivery = client.publish(make_event())

    assert delivery.ok is False
    assert delivery.error


@pytest.mark.parametrize("response", [
    {"returncode": 1, "stderr": "not logged in"},
    {"stdout": "not-json"},
])
def test_command_and_json_failures_return_delivery_error(tmp_path: Path, response: dict):
    responses = [response] if response.get("returncode") else [{}, response]
    runner = ScriptedRunner(responses)
    client = GitHubHandoffClient(tmp_path / "state.json", tmp_path / "handoff.log", runner=runner)

    delivery = client.publish(make_event())

    assert delivery.ok is False
    assert delivery.error


def test_failure_diagnostics_redact_credential_like_values(tmp_path: Path):
    runner = ScriptedRunner([{"returncode": 1, "stderr": "token=supersecret password: hunter2"}])
    log_path = tmp_path / "handoff.log"
    client = GitHubHandoffClient(tmp_path / "state.json", log_path, runner=runner)

    delivery = client.publish(make_event())

    assert "supersecret" not in delivery.error
    assert "hunter2" not in delivery.error
    assert "supersecret" not in log_path.read_text(encoding="utf-8")
    assert "hunter2" not in log_path.read_text(encoding="utf-8")


def test_warning_store_persists_and_caps_keys(tmp_path: Path):
    path = tmp_path / "warnings.json"
    store = HandoffWarningStore(path)
    for index in range(505):
        store.add(f"event-{index}")

    restored = HandoffWarningStore(path)
    assert restored.contains("event-0") is False
    assert restored.contains("event-504") is True
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 500
