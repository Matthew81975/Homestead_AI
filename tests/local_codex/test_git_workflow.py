import subprocess
from pathlib import Path

import pytest

from hcs_ai.local_codex.git_workflow import GitWorkflow
from hcs_ai.local_codex.models import WorkspaceConfig


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "branch", "-M", "development")
    return repo


def workspace(repo: Path) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace_id="test",
        name="Test",
        aliases=[],
        path=str(repo),
        target_branch="development",
        test_commands=["python -m pytest"],
        allowed_run_commands=[],
        git_enabled=True,
        enabled=True,
    )


def test_creates_alexandria_task_branch(tmp_path: Path):
    repo = init_repo(tmp_path)
    workflow = GitWorkflow()
    ctx = workflow.create_task_branch(workspace(repo), "042", "fix-platform-collision")
    assert ctx.task_branch == "alexandria/042-fix-platform-collision"
    assert git(repo, "branch", "--show-current") == ctx.task_branch
    assert ctx.expected_target_sha == git(repo, "rev-parse", "development")


def test_task_branch_creation_requires_clean_worktree(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="clean worktree"):
        GitWorkflow().create_task_branch(workspace(repo), "042", "test")


def test_branch_name_is_sanitized(tmp_path: Path):
    repo = init_repo(tmp_path)
    ctx = GitWorkflow().create_task_branch(workspace(repo), "42", "Fix Weird / Thing!")
    assert ctx.task_branch == "alexandria/42-fix-weird-thing"

from datetime import datetime, timezone
from hcs_ai.local_codex.models import ApprovalStatus, TaskRecord, TaskStatus


def init_repo_with_remote(tmp_path: Path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    repo = init_repo(tmp_path)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "development")
    return repo, remote


def ready_task(task_id: str, ctx, *, tests_ok=True, diff_summary="diff inspected") -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        thread_id="thread",
        sender="schoolfieldmatt@gmail.com",
        subject="TASK: test",
        body="change file",
        workspace_id="test",
        status=TaskStatus.READY_FOR_FINAL_APPROVAL,
        approval_status=ApprovalStatus.APPROVED,
        created_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        task_branch=ctx.task_branch,
        target_branch=ctx.target_branch,
        expected_target_sha=ctx.expected_target_sha,
        tests_run=[{"command": "python -m pytest", "ok": tests_ok}],
        diff_summary=diff_summary,
    )


def test_finalize_commits_pushes_merges_and_pushes_target(tmp_path: Path):
    repo, remote = init_repo_with_remote(tmp_path)
    wf = GitWorkflow()
    ws = workspace(repo)
    ctx = wf.create_task_branch(ws, "42", "change-file")
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")
    task = ready_task("42", ctx)

    result = wf.finalize(task, ws, "feat: change file")

    assert result.blocked_reason is None
    assert result.committed_sha
    assert result.task_branch_pushed is True
    assert result.merged_sha
    assert result.target_pushed is True
    assert git(repo, "branch", "--show-current") == "development"
    assert git(repo, "show", "development:file.txt") == "changed"


def test_finalize_blocks_when_tests_failed(tmp_path: Path):
    repo, remote = init_repo_with_remote(tmp_path)
    wf = GitWorkflow()
    ws = workspace(repo)
    ctx = wf.create_task_branch(ws, "42", "change-file")
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")
    task = ready_task("42", ctx, tests_ok=False)

    result = wf.finalize(task, ws, "feat: change file")

    assert result.blocked_reason == "required tests have not passed"
    assert git(repo, "status", "--porcelain") != ""


def test_finalize_blocks_when_diff_not_inspected(tmp_path: Path):
    repo, remote = init_repo_with_remote(tmp_path)
    wf = GitWorkflow()
    ws = workspace(repo)
    ctx = wf.create_task_branch(ws, "42", "change-file")
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")
    task = ready_task("42", ctx, diff_summary="")

    result = wf.finalize(task, ws, "feat: change file")

    assert result.blocked_reason == "git diff has not been inspected"


def test_target_divergence_blocks_merge_after_task_push(tmp_path: Path):
    repo, remote = init_repo_with_remote(tmp_path)
    wf = GitWorkflow()
    ws = workspace(repo)
    ctx = wf.create_task_branch(ws, "42", "change-file")
    (repo / "file.txt").write_text("task\n", encoding="utf-8")
    task = ready_task("42", ctx)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True, text=True)
    git(other, "config", "user.email", "other@example.com")
    git(other, "config", "user.name", "Other User")
    git(other, "checkout", "development")
    (other / "other.txt").write_text("advance\n", encoding="utf-8")
    git(other, "add", "other.txt")
    git(other, "commit", "-m", "advance target")
    git(other, "push", "origin", "development")

    result = wf.finalize(task, ws, "feat: task")

    assert result.blocked_reason == "target branch diverged from expected commit"
    assert result.task_branch_pushed is True


def test_finalize_is_idempotent_after_task_branch_was_already_pushed(tmp_path: Path):
    repo, remote = init_repo_with_remote(tmp_path)
    wf = GitWorkflow()
    ws = workspace(repo)
    ctx = wf.create_task_branch(ws, "42", "change-file")
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")
    task = ready_task("42", ctx)

    checkpoints = []
    def stop_after_push(updated_task):
        checkpoints.append(dict(updated_task.finalization))
        if updated_task.finalization.get("task_branch_pushed") and not updated_task.finalization.get("target_verified"):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        wf.finalize(task, ws, "feat: change file", checkpoint=stop_after_push)

    assert task.finalization["commit_created"] is True
    assert task.finalization["task_branch_pushed"] is True

    result = wf.finalize(task, ws, "feat: change file")

    assert result.blocked_reason is None
    assert result.target_pushed is True
    assert git(repo, "show", "development:file.txt") == "changed"
