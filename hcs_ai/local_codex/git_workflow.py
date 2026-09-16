from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import WorkspaceConfig


@dataclass(frozen=True)
class BranchContext:
    task_branch: str
    target_branch: str
    expected_target_sha: str


@dataclass(frozen=True)
class FinalizeResult:
    committed_sha: str | None = None
    task_branch_pushed: bool = False
    merged_sha: str | None = None
    target_pushed: bool = False
    blocked_reason: str | None = None


class GitWorkflow:
    def _run(self, path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            capture_output=True,
            text=True,
            shell=False,
        )
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "git command failed"
            raise RuntimeError(message)
        return result

    def _slug(self, value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug[:60] or "task"

    def current_head(self, path: Path, branch: str = "HEAD") -> str:
        return self._run(path, "rev-parse", branch).stdout.strip()

    def create_task_branch(
        self,
        workspace: WorkspaceConfig,
        task_id: str,
        slug: str,
    ) -> BranchContext:
        path = Path(workspace.path)
        if not workspace.git_enabled:
            raise RuntimeError("Git is disabled for this workspace")

        status = self._run(path, "status", "--porcelain").stdout.strip()
        if status:
            raise RuntimeError("task branch creation requires a clean worktree")

        target = workspace.target_branch
        target_check = self._run(path, "rev-parse", "--verify", target, check=False)
        if target_check.returncode != 0:
            raise RuntimeError(f"target branch does not exist: {target}")

        expected_sha = target_check.stdout.strip()
        task_branch = f"alexandria/{task_id}-{self._slug(slug)}"

        existing = self._run(path, "rev-parse", "--verify", task_branch, check=False)
        if existing.returncode == 0:
            raise RuntimeError(f"task branch already exists: {task_branch}")

        self._run(path, "checkout", target)
        self._run(path, "checkout", "-b", task_branch)

        return BranchContext(
            task_branch=task_branch,
            target_branch=target,
            expected_target_sha=expected_sha,
        )

    def _checkpoint(self, task, key: str, value, callback=None) -> None:
        task.finalization[key] = value
        if callback is not None:
            callback(task)

    def _is_ancestor(self, path: Path, ancestor: str, descendant: str) -> bool:
        result = self._run(path, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
        return result.returncode == 0

    def finalize(self, task, workspace: WorkspaceConfig, commit_message: str, checkpoint=None) -> FinalizeResult:
        path = Path(workspace.path)
        if task.status.value not in {"ready_for_final_approval", "finalizing"}:
            return FinalizeResult(blocked_reason="task is not ready for final approval")
        if task.tests_run and not all(bool(item.get("ok")) for item in task.tests_run):
            return FinalizeResult(blocked_reason="required tests have not passed")
        code_suffixes = {
            ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".cc",
            ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1",
        }
        if any(Path(name).suffix.lower() in code_suffixes for name in task.changed_files) and not task.tests_run:
            return FinalizeResult(blocked_reason="required tests have not passed")
        if not task.diff_summary.strip():
            return FinalizeResult(blocked_reason="git diff has not been inspected")
        if not task.task_branch or not task.target_branch or not task.expected_target_sha:
            return FinalizeResult(blocked_reason="task branch context is incomplete")

        committed_sha = task.finalization.get("committed_sha")
        if not task.finalization.get("commit_created"):
            current = self._run(path, "branch", "--show-current").stdout.strip()
            if current != task.task_branch:
                checkout = self._run(path, "checkout", task.task_branch, check=False)
                if checkout.returncode != 0:
                    return FinalizeResult(blocked_reason="could not checkout task branch")

            self._run(path, "add", "-A")
            staged = self._run(path, "diff", "--cached", "--quiet", check=False)
            if staged.returncode == 1:
                commit = self._run(path, "commit", "-m", commit_message, check=False)
                if commit.returncode != 0:
                    return FinalizeResult(blocked_reason="git commit failed")
            elif staged.returncode != 0:
                return FinalizeResult(blocked_reason="could not inspect staged changes")

            committed_sha = self.current_head(path)
            if committed_sha == task.expected_target_sha:
                return FinalizeResult(blocked_reason="no task changes to commit")
            self._checkpoint(task, "committed_sha", committed_sha, checkpoint)
            self._checkpoint(task, "commit_created", True, checkpoint)
        else:
            committed_sha = committed_sha or self.current_head(path, task.task_branch)

        if not task.finalization.get("task_branch_pushed"):
            push_task = self._run(path, "push", "-u", "origin", task.task_branch, check=False)
            if push_task.returncode != 0:
                return FinalizeResult(committed_sha=committed_sha, blocked_reason="task branch push failed")
            self._checkpoint(task, "task_branch_pushed", True, checkpoint)

        if not task.finalization.get("target_verified"):
            fetch = self._run(path, "fetch", "origin", task.target_branch, check=False)
            if fetch.returncode != 0:
                return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, blocked_reason="could not fetch target branch")
            remote_target = self._run(path, "rev-parse", f"origin/{task.target_branch}", check=False)
            if remote_target.returncode != 0:
                return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, blocked_reason="could not resolve remote target branch")
            remote_sha = remote_target.stdout.strip()
            # If recovery occurs after a successful target push, remote may already be the merged commit.
            local_target_now = self._run(path, "rev-parse", task.target_branch, check=False)
            local_target_sha = local_target_now.stdout.strip() if local_target_now.returncode == 0 else ""
            already_merged_remote = (
                remote_sha == local_target_sha
                and local_target_sha != task.expected_target_sha
                and self._is_ancestor(path, task.task_branch, task.target_branch)
            )
            if remote_sha != task.expected_target_sha and not already_merged_remote:
                return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, blocked_reason="target branch diverged from expected commit")
            self._checkpoint(task, "target_verified", True, checkpoint)

        merged_sha = task.finalization.get("merged_sha")
        if not task.finalization.get("merge_completed"):
            local_target = self._run(path, "rev-parse", task.target_branch, check=False)
            if local_target.returncode != 0:
                return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, blocked_reason="local target branch missing")
            local_target_sha = local_target.stdout.strip()

            if local_target_sha != task.expected_target_sha:
                if self._is_ancestor(path, task.task_branch, task.target_branch):
                    merged_sha = local_target_sha
                    self._checkpoint(task, "merged_sha", merged_sha, checkpoint)
                    self._checkpoint(task, "merge_completed", True, checkpoint)
                else:
                    return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, blocked_reason="local target branch diverged from expected commit")
            else:
                checkout_target = self._run(path, "checkout", task.target_branch, check=False)
                if checkout_target.returncode != 0:
                    return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, blocked_reason="could not checkout target branch")
                merge = self._run(path, "merge", "--no-ff", task.task_branch, "-m", f"Merge {task.task_branch}", check=False)
                if merge.returncode != 0:
                    self._run(path, "merge", "--abort", check=False)
                    self._run(path, "checkout", task.task_branch, check=False)
                    return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, blocked_reason="merge conflict or merge failure")
                merged_sha = self.current_head(path)
                self._checkpoint(task, "merged_sha", merged_sha, checkpoint)
                self._checkpoint(task, "merge_completed", True, checkpoint)
        else:
            merged_sha = merged_sha or self.current_head(path, task.target_branch)

        if not task.finalization.get("target_pushed"):
            fetch = self._run(path, "fetch", "origin", task.target_branch, check=False)
            if fetch.returncode != 0:
                return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, merged_sha=merged_sha, blocked_reason="could not fetch target branch")
            remote_sha = self._run(path, "rev-parse", f"origin/{task.target_branch}").stdout.strip()
            if remote_sha != merged_sha:
                if remote_sha != task.expected_target_sha:
                    return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, merged_sha=merged_sha, blocked_reason="target branch diverged before push")
                checkout_target = self._run(path, "checkout", task.target_branch, check=False)
                if checkout_target.returncode != 0:
                    return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, merged_sha=merged_sha, blocked_reason="could not checkout target branch")
                push_target = self._run(path, "push", "origin", task.target_branch, check=False)
                if push_target.returncode != 0:
                    return FinalizeResult(committed_sha=committed_sha, task_branch_pushed=True, merged_sha=merged_sha, blocked_reason="target branch push failed")
            self._checkpoint(task, "target_pushed", True, checkpoint)

        return FinalizeResult(
            committed_sha=str(committed_sha),
            task_branch_pushed=True,
            merged_sha=str(merged_sha),
            target_pushed=True,
        )
