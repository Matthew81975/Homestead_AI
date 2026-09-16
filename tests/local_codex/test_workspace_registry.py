import json
from pathlib import Path

import pytest

from hcs_ai.local_codex.workspace_registry import WorkspaceRegistry


def write_registry(path: Path, workspaces: list[dict]) -> None:
    path.write_text(json.dumps({"workspaces": workspaces}), encoding="utf-8")


def base_workspace(tmp_path: Path, **overrides):
    data = {
        "workspace_id": "maze_world",
        "name": "Maze World",
        "aliases": ["maze", "mazeworld"],
        "path": str((tmp_path / "Maze").resolve()),
        "target_branch": "development",
        "test_commands": ["python -m pytest"],
        "allowed_run_commands": ["run_maze.bat"],
        "git_enabled": True,
        "enabled": True,
    }
    data.update(overrides)
    return data


def test_registry_resolves_alias(tmp_path: Path):
    path = tmp_path / "workspaces.json"
    write_registry(path, [base_workspace(tmp_path)])
    registry = WorkspaceRegistry.load(path)
    assert registry.resolve_explicit("TASK: Maze World — fix collision").workspace_id == "maze_world"
    assert registry.resolve_explicit("TASK: maze — inspect").workspace_id == "maze_world"


def test_registry_loads_github_handoff_fields(tmp_path: Path):
    path = tmp_path / "workspaces.json"
    write_registry(path, [base_workspace(
        tmp_path,
        github_repository="Matthew81975/Maze_World",
        handoff_enabled=True,
        handoff_issue_number=7,
    )])

    workspace = WorkspaceRegistry.load(path).get("maze_world")

    assert workspace.github_repository == "Matthew81975/Maze_World"
    assert workspace.handoff_enabled is True
    assert workspace.handoff_issue_number == 7


def test_registry_rejects_duplicate_aliases(tmp_path: Path):
    path = tmp_path / "workspaces.json"
    write_registry(path, [
        base_workspace(tmp_path, workspace_id="a", name="Alpha", aliases=["shared"], path=str((tmp_path/"a").resolve())),
        base_workspace(tmp_path, workspace_id="b", name="Beta", aliases=["shared"], path=str((tmp_path/"b").resolve())),
    ])
    with pytest.raises(ValueError, match="duplicate alias"):
        WorkspaceRegistry.load(path)


def test_registry_rejects_relative_workspace_path(tmp_path: Path):
    path = tmp_path / "workspaces.json"
    write_registry(path, [base_workspace(tmp_path, path="relative/path")])
    with pytest.raises(ValueError, match="absolute"):
        WorkspaceRegistry.load(path)


def test_infer_requires_exactly_one_workspace_match(tmp_path: Path):
    path = tmp_path / "workspaces.json"
    write_registry(path, [
        base_workspace(tmp_path),
        base_workspace(tmp_path, workspace_id="hcs", name="HCS", aliases=["homestead ai"], path=str((tmp_path/"HCS").resolve())),
    ])
    registry = WorkspaceRegistry.load(path)
    assert registry.infer("Please inspect the Maze World renderer").workspace_id == "maze_world"
    assert registry.infer("Compare Maze World and HCS") is None
    assert registry.infer("Fix the project") is None


def test_is_registered_path_does_not_accept_arbitrary_path(tmp_path: Path):
    path = tmp_path / "workspaces.json"
    workspace_path = (tmp_path / "Maze").resolve()
    write_registry(path, [base_workspace(tmp_path, path=str(workspace_path))])
    registry = WorkspaceRegistry.load(path)
    assert registry.is_registered_path(workspace_path) is True
    assert registry.is_registered_path((tmp_path / "Other").resolve()) is False


def test_workspace_config_accepts_optional_project_control_endpoint(tmp_path: Path):
    path = tmp_path / "workspaces.json"
    path.write_text(json.dumps({
        "workspaces": [{
            "workspace_id": "maze_world",
            "name": "Maze World",
            "aliases": ["maze"],
            "path": str(tmp_path / "maze"),
            "target_branch": "main",
            "control_api_url": "http://127.0.0.1:8765",
        }]
    }), encoding="utf-8")

    registry = WorkspaceRegistry.load(path)

    assert registry.get("maze_world").control_api_url == "http://127.0.0.1:8765"


def test_registry_accepts_utf8_bom(tmp_path: Path):
    path = tmp_path / "workspaces.json"
    payload = json.dumps({"workspaces": [base_workspace(tmp_path)]})
    path.write_text(payload, encoding="utf-8-sig")

    registry = WorkspaceRegistry.load(path)

    assert registry.get("maze_world").name == "Maze World"
