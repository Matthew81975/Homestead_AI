from pathlib import Path

import pytest

from hcs_ai.local_codex.state import TaskJournal
from hcs_ai.local_codex.workspace import Workspace, WorkspaceViolation


def make_journal(tmp_path: Path, workspace: Path) -> TaskJournal:
    return TaskJournal.new(
        task="test",
        workspace=str(workspace),
        model="qwen2.5-3b-instruct",
        log_dir=tmp_path / "logs",
    )


def test_rejects_parent_escape(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    ws = Workspace(root)
    with pytest.raises(WorkspaceViolation):
        ws.resolve_safe("../outside.txt")


def test_write_snapshots_original(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "main.py"
    path.write_text("old", encoding="utf-8")
    journal = make_journal(tmp_path, root)
    ws = Workspace(root)

    ws.write_file("main.py", "new", journal=journal, dry_run=False)

    assert journal.original_files["main.py"] == "old"
    assert path.read_text(encoding="utf-8") == "new"


def test_dry_run_blocks_write(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("old", encoding="utf-8")
    journal = make_journal(tmp_path, root)
    ws = Workspace(root)

    with pytest.raises(PermissionError):
        ws.write_file("main.py", "new", journal=journal, dry_run=True)


def test_symlink_escape_rejected_when_supported(tmp_path: Path):
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not available")

    ws = Workspace(root)
    with pytest.raises(WorkspaceViolation):
        ws.read_file("link/secret.txt")


def test_shallow_list_includes_top_level_directories_and_ignores_venv(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("print('ok')", encoding="utf-8")
    (root / "maze_world").mkdir()
    (root / "maze_world" / "world.py").write_text("world = 1", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "noise.py").write_text("noise = 1", encoding="utf-8")

    ws = Workspace(root)
    entries = ws.list_files(".", recursive=False)

    assert entries == ["main.py", "maze_world/"]


def test_recursive_list_ignores_environment_and_cache_directories(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("print('ok')", encoding="utf-8")
    (root / "maze_world").mkdir()
    (root / "maze_world" / "world.py").write_text("world = 1", encoding="utf-8")
    for ignored in [".venv", "venv", "__pycache__", ".pytest_cache", ".git"]:
        folder = root / ignored
        folder.mkdir()
        (folder / "noise.py").write_text("noise = 1", encoding="utf-8")

    ws = Workspace(root)
    entries = ws.list_files(".", recursive=True)

    assert entries == ["main.py", "maze_world/world.py"]


def test_recursive_list_uses_portable_forward_slashes(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "maze_world").mkdir(parents=True)
    (root / "maze_world" / "world.py").write_text("x = 1", encoding="utf-8")
    ws = Workspace(root)

    files = ws.list_files(".", recursive=True)

    assert "maze_world/world.py" in files
    assert all("\\" not in item for item in files)
