import json
from threading import Event

import pytest

from hcs_ai.local_codex.lm_client import LMStudioClient
from hcs_ai.local_codex.runtime import build_controller, make_client, run_task
from hcs_ai.local_codex.state import AgentStatus, TaskJournal
from hcs_ai.local_codex.workspace import WorkspaceViolation


@pytest.fixture
def task(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    config = {
        "workspace": str(workspace),
        "lm_studio_url": "http://127.0.0.1:1234",
        "model": "test-model",
        "max_failed_actions": 3,
    }
    journal = TaskJournal.new("Inspect files", str(workspace), "test-model", tmp_path / "logs")
    return config, journal


@pytest.mark.parametrize("with_callback", [False, True])
def test_runtime_returns_status_and_routes_progress_without_printing(task, tmp_path, monkeypatch, capsys, with_callback):
    config, journal = task
    monkeypatch.setattr(LMStudioClient, "chat", lambda self, messages: '{"action":"finish","summary":"inspected","tests":[]}')
    messages = []
    reviews = []
    status = run_task(
        config, journal, workspaces_config_path=tmp_path / "missing.json",
        status_callback=messages.append if with_callback else None,
        approval_callback=lambda request: reviews.append(request) or True,
    )
    assert status is AgentStatus.DONE
    saved = TaskJournal.load(journal.path)
    assert saved.status is AgentStatus.DONE
    assert saved.commit_approved is False
    assert saved.push_approved is False
    assert reviews == [{"kind": "task_review", "summary": "Inspect files"}]
    assert saved.steps[-1]["result"]["summary"] == "inspected"
    assert bool(messages) is with_callback
    assert capsys.readouterr().out == ""


def test_runtime_forwards_cancellation_and_saves_stopped_status(task, tmp_path, monkeypatch):
    config, journal = task
    event = Event()
    event.set()
    monkeypatch.setattr(LMStudioClient, "chat", lambda *args: pytest.fail("cancelled runtime must not call model"))
    assert run_task(config, journal, cancel_event=event, workspaces_config_path=tmp_path / "missing.json") is AgentStatus.INTERRUPTED
    assert TaskJournal.load(journal.path).status is AgentStatus.INTERRUPTED
    assert journal.steps == []


def test_build_controller_keeps_dry_run_and_workspace_safety(task, tmp_path):
    config, journal = task
    controller = build_controller(config, journal, dry_run=True, workspaces_config_path=tmp_path / "missing.json")
    with pytest.raises(PermissionError, match="dry-run"):
        controller.executor.execute({"action": "write_file", "path": "note.txt", "content": "no"})
    with pytest.raises(WorkspaceViolation):
        controller.executor.execute({"action": "read_file", "path": "../outside.txt"})
    with pytest.raises(ValueError):
        controller.executor.execute({"action": "run_command", "command": "git push"})
    assert list(controller.executor.workspace.root.iterdir()) == []


def test_build_controller_uses_matching_enabled_registry_project_control(task, tmp_path):
    config, journal = task
    registry = tmp_path / "workspaces.json"
    registry.write_text(json.dumps({"workspaces": [{
        "workspace_id": "repo", "name": "Repo", "aliases": [],
        "path": config["workspace"], "target_branch": "main",
        "control_api_url": "http://127.0.0.1:8766",
    }]}), encoding="utf-8")
    controller = build_controller(config, journal, workspaces_config_path=registry)
    assert controller.executor.project_control.base_url == "http://127.0.0.1:8766"
    assert controller.executor.workspace.root == (tmp_path / "repo").resolve()
    assert controller.executor.tornado_client is None


def test_build_controller_wires_tornado_diagnostics_and_status_callback(task, tmp_path):
    config, journal = task
    config["tornado"] = {
        "enabled": True,
        "state_path": str(tmp_path / "tornado_state.json"),
        "log_path": str(tmp_path / "tornado.log"),
        "providers": [{
            "id": "local", "kind": "lm_studio", "local": True,
            "base_url": "http://127.0.0.1:1234", "model": "test-model",
        }],
    }
    callback = lambda message: None
    controller = build_controller(config, journal, workspaces_config_path=tmp_path / "missing.json", status_callback=callback)
    assert controller.executor.tornado_client is controller.client
    assert controller.client.provider_ids == ["local"]
    assert controller.client.status_callback is callback
    assert controller.status_callback is callback
