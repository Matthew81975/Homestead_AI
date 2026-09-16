from datetime import datetime, timezone

import pytest

from hcs_ai.local_codex.models import ApprovalStatus, TaskRecord, TaskStatus, WorkspaceConfig


def test_task_record_round_trip():
    task = TaskRecord(
        task_id="042",
        thread_id="thread-1",
        sender="schoolfieldmatt@gmail.com",
        subject="TASK: Maze World - fix collision",
        body="Fix collision",
        workspace_id="maze_world",
        status=TaskStatus.WAITING_CONFIRMATION,
        approval_status=ApprovalStatus.PENDING,
        created_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    restored = TaskRecord.from_dict(task.to_dict())
    assert restored == task


def test_workspace_aliases_are_normalized():
    workspace = WorkspaceConfig(
        workspace_id="maze_world",
        name="Maze World",
        aliases=["maze", "Maze_World"],
        path=r"C:\Repo\Maze",
        target_branch="development",
        test_commands=["python -m pytest"],
        allowed_run_commands=["run_maze.bat"],
        git_enabled=True,
        enabled=True,
    )
    assert workspace.normalized_aliases() == {"maze world", "maze", "maze_world"}


def test_workspace_handoff_defaults_are_disabled_without_repository():
    workspace = WorkspaceConfig(
        workspace_id="maze",
        name="Maze",
        aliases=[],
        path=r"C:\maze",
        target_branch="development",
    )

    assert workspace.github_repository is None
    assert workspace.handoff_enabled is False
    assert workspace.handoff_issue_number is None


def test_workspace_handoff_defaults_are_enabled_with_repository():
    workspace = WorkspaceConfig(
        workspace_id="maze",
        name="Maze",
        aliases=[],
        path=r"C:\maze",
        target_branch="development",
        github_repository="Matthew81975/Maze_World",
    )

    assert workspace.handoff_enabled is True


def test_workspace_handoff_can_be_explicitly_disabled():
    workspace = WorkspaceConfig(
        workspace_id="maze",
        name="Maze",
        aliases=[],
        path=r"C:\maze",
        target_branch="development",
        github_repository="Matthew81975/Maze_World",
        handoff_enabled=False,
    )

    assert workspace.handoff_enabled is False


def test_workspace_handoff_normalizes_blank_repository_to_disabled():
    workspace = WorkspaceConfig(
        workspace_id="maze",
        name="Maze",
        aliases=[],
        path=r"C:\maze",
        target_branch="development",
        github_repository="  ",
        handoff_enabled=True,
    )

    assert workspace.github_repository is None
    assert workspace.handoff_enabled is False


@pytest.mark.parametrize("repository", ["owner", "owner/repo/extra", "/repo", "owner/"])
def test_workspace_handoff_rejects_malformed_repository(repository: str):
    with pytest.raises(ValueError, match="owner/name"):
        WorkspaceConfig(
            workspace_id="maze",
            name="Maze",
            aliases=[],
            path=r"C:\maze",
            target_branch="development",
            github_repository=repository,
        )


def test_workspace_handoff_rejects_nonpositive_issue_number():
    with pytest.raises(ValueError, match="positive"):
        WorkspaceConfig(
            workspace_id="maze",
            name="Maze",
            aliases=[],
            path=r"C:\maze",
            target_branch="development",
            github_repository="Matthew81975/Maze_World",
            handoff_issue_number=0,
        )
