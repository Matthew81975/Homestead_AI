from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from hcs_ai.local_codex.controller import ControllerResult
from hcs_ai.local_codex.email_worker import EmailWorker
from hcs_ai.local_codex.github_handoff import HandoffDelivery, HandoffWarningStore
from hcs_ai.local_codex.git_workflow import BranchContext, FinalizeResult
from hcs_ai.local_codex.models import ApprovalStatus, MailMessage, TaskStatus, WorkspaceConfig
from hcs_ai.local_codex.self_update import StartupWarningStore
from hcs_ai.local_codex.task_queue import TaskQueue
from hcs_ai.local_codex.workspace_registry import WorkspaceRegistry
from hcs_ai.local_codex.state import AgentStatus


class FakeMail:
    def __init__(self):
        self.inbox = []
        self.sent = []
        self.processed = set()
        self.counter = 0

    def poll(self):
        items = list(self.inbox)
        self.inbox.clear()
        return items

    def mark_processed(self, message_id):
        self.processed.add(message_id)

    def send(self, to, subject, body, *, in_reply_to=None, references=None):
        self.counter += 1
        message_id = f"<sent-{self.counter}@example>"
        self.sent.append(SimpleNamespace(
            to=to, subject=subject, body=body,
            in_reply_to=in_reply_to, references=references or [], message_id=message_id,
        ))
        return message_id


class FakeGit:
    def __init__(self, create_error=None, finalize_result=None):
        self.create_calls = []
        self.finalize_calls = []
        self.create_error = create_error
        self.finalize_result = finalize_result

    def create_task_branch(self, workspace, task_id, slug):
        self.create_calls.append((workspace.workspace_id, task_id, slug))
        if self.create_error is not None:
            raise self.create_error
        return BranchContext(
            task_branch=f"alexandria/{task_id}-task",
            target_branch=workspace.target_branch,
            expected_target_sha="abc123",
        )

    def finalize(self, task, workspace, commit_message, checkpoint=None):
        self.finalize_calls.append((task.task_id, workspace.workspace_id, commit_message))
        if checkpoint is not None:
            checkpoint(task)
        return self.finalize_result or FinalizeResult(
            committed_sha="commit1",
            task_branch_pushed=True,
            merged_sha="merge1",
            target_pushed=True,
        )


class FakeController:
    def __init__(self, results=None):
        self.results = list(results or [ControllerResult(AgentStatus.WORKING)])
        self.pause_requested = False
        self.instructions = []
        self.journal = SimpleNamespace(
            files_changed=[],
            tests_run=[],
            steps=[],
        )

    def set_pause_requested(self, value):
        self.pause_requested = value

    def add_instruction(self, text):
        self.instructions.append(text)

    def run_one_step(self):
        if self.pause_requested:
            return ControllerResult(AgentStatus.PAUSED)
        return self.results.pop(0) if self.results else ControllerResult(AgentStatus.WORKING)


class FakeHandoff:
    def __init__(self, queue, deliveries=None):
        self.queue = queue
        self.events = []
        self.saved_statuses = []
        self.deliveries = list(deliveries or [])

    def publish(self, event, configured_issue_number=None):
        self.events.append((event, configured_issue_number))
        restored = TaskQueue.load(self.queue.path)
        self.saved_statuses.append(restored.get(event.task_id).status)
        return self.deliveries.pop(0) if self.deliveries else HandoffDelivery(True, 7)


def make_registry(tmp_path: Path):
    return WorkspaceRegistry([
        WorkspaceConfig(
            workspace_id="maze_world",
            name="Maze World",
            aliases=["maze"],
            path=str((tmp_path / "maze").resolve()),
            target_branch="development",
            test_commands=["python -m pytest"],
            allowed_run_commands=["run_maze.bat"],
            git_enabled=True,
            enabled=True,
            github_repository="Matthew81975/Maze_World",
        ),
        WorkspaceConfig(
            workspace_id="hcs",
            name="HCS",
            aliases=["homestead ai"],
            path=str((tmp_path / "hcs").resolve()),
            target_branch="main",
            test_commands=["python -m pytest"],
            allowed_run_commands=[],
            git_enabled=True,
            enabled=True,
            github_repository="Matthew81975/Homestead_AI",
        ),
    ])


def task_mail(subject="TASK: Maze World", body="Inspect files", message_id="<task1@example>"):
    return MailMessage(
        sender="schoolfieldmatt@gmail.com",
        subject=subject,
        body=body,
        message_id=message_id,
    )


def reply_mail(root_id, body, message_id="<reply@example>"):
    return MailMessage(
        sender="schoolfieldmatt@gmail.com",
        subject="Re: task",
        body=body,
        message_id=message_id,
        in_reply_to="<sent-1@example>",
        references=[root_id, "<sent-1@example>"],
    )


def make_worker(
    tmp_path: Path,
    controller=None,
    *,
    handoff_deliveries=None,
    git=None,
    startup_warning_store=None,
):
    mail = FakeMail()
    queue = TaskQueue.load(tmp_path / "tasks.json")
    git = git or FakeGit()
    handoff = FakeHandoff(queue, handoff_deliveries)
    controllers = []
    def controller_factory(task, workspace):
        value = controller or FakeController()
        controllers.append(value)
        return value
    worker = EmailWorker(
        mail_gateway=mail,
        registry=make_registry(tmp_path),
        queue=queue,
        git_workflow=git,
        controller_factory=controller_factory,
        trusted_sender="schoolfieldmatt@gmail.com",
        task_id_factory=lambda: "042",
        now=lambda: datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
        handoff_client=handoff,
        handoff_warning_store=HandoffWarningStore(tmp_path / "handoff_warnings.json"),
        startup_warning_store=startup_warning_store,
    )
    return worker, mail, queue, git, controllers, handoff


def test_task_waits_for_confirmation_before_execution(tmp_path: Path):
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path)
    mail.inbox.append(task_mail())

    worker.poll_once()

    task = queue.get("042")
    assert task.status is TaskStatus.WAITING_CONFIRMATION
    assert task.approval_status is ApprovalStatus.PENDING
    assert task.workspace_id == "maze_world"
    assert controllers == []
    assert "Workspace: Maze World" in mail.sent[-1].body


def test_untrusted_sender_is_never_enqueued(tmp_path: Path):
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path)
    bad = task_mail()
    bad.sender = "attacker@example.com"
    mail.inbox.append(bad)

    worker.poll_once()

    assert queue.tasks == {}
    assert bad.message_id in mail.processed


def test_workspace_reply_assigns_unresolved_task_and_requests_approval(tmp_path: Path):
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path)
    mail.inbox.append(task_mail(subject="TASK: Fix rendering", body="Please fix it"))
    worker.poll_once()

    task = queue.get("042")
    assert task.workspace_id is None
    assert task.status is TaskStatus.QUEUED

    mail.inbox.append(reply_mail("<task1@example>", "Maze\u200b\u00a0World"))
    worker.poll_once()

    task = queue.get("042")
    assert task.workspace_id == "maze_world"
    assert task.status is TaskStatus.WAITING_CONFIRMATION
    assert task.approval_status is ApprovalStatus.PENDING
    assert "Workspace: Maze World" in mail.sent[-1].body
    assert controllers == []


def test_only_one_task_per_workspace_waits_for_confirmation(tmp_path: Path):
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path)
    mail.inbox.append(task_mail(message_id="<first@example>"))
    worker.poll_once()
    worker.task_id_factory = lambda: "043"
    mail.inbox.append(task_mail(message_id="<second@example>"))
    worker.poll_once()

    waiting = [
        task for task in queue.tasks.values()
        if task.status is TaskStatus.WAITING_CONFIRMATION
    ]
    queued = [
        task for task in queue.tasks.values()
        if task.status is TaskStatus.QUEUED
    ]
    assert len(waiting) == 1
    assert len(queued) == 1
    assert len(mail.sent) == 1


def test_cancel_releases_blocked_workspace(tmp_path: Path):
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path)
    mail.inbox.append(task_mail())
    worker.poll_once()
    task = queue.get("042")
    queue.activate(task.task_id)
    task.status = TaskStatus.BLOCKED
    queue.save()

    mail.inbox.append(reply_mail("<task1@example>", "CANCEL"))
    worker.poll_once()

    assert task.status is TaskStatus.CANCELLED
    assert "maze_world" not in queue.workspace_locks
    assert handoff.events[-1][0].state == "cancelled"
    assert handoff.saved_statuses[-1] is TaskStatus.CANCELLED


def test_approve_starts_task_and_creates_branch(tmp_path: Path):
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path)
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE"))

    worker.poll_once()

    task = queue.get("042")
    assert task.status is TaskStatus.ACTIVE
    assert task.task_branch == "alexandria/042-task"
    assert len(git.create_calls) == 1
    assert len(controllers) == 1
    event, issue_number = handoff.events[-1]
    assert event.state == "working"
    assert event.task_branch == "alexandria/042-task"
    assert event.target_branch == "development"
    assert issue_number is None
    assert handoff.saved_statuses[-1] is TaskStatus.ACTIVE


def test_live_pause_resume_and_instruction_route_to_controller(tmp_path: Path):
    controller = FakeController()
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path, controller=controller)
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve@example>"))
    worker.poll_once()

    mail.inbox.append(reply_mail("<task1@example>", "PAUSE", "<pause@example>"))
    worker.poll_once()
    assert queue.get("042").status is TaskStatus.PAUSED
    assert controller.pause_requested is True
    assert handoff.events[-1][0].state == "paused"
    assert handoff.saved_statuses[-1] is TaskStatus.PAUSED

    mail.inbox.append(reply_mail("<task1@example>", "RESUME", "<resume@example>"))
    worker.poll_once()
    assert queue.get("042").status is TaskStatus.ACTIVE
    assert controller.pause_requested is False

    mail.inbox.append(reply_mail("<task1@example>", "Do not modify tests.", "<instruction@example>"))
    worker.poll_once()
    assert controller.instructions[-1] == "Do not modify tests."
    assert [event.state for event, _ in handoff.events] == ["working", "paused", "working"]


def test_cancel_preserves_task_and_releases_workspace(tmp_path: Path):
    controller = FakeController()
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path, controller=controller)
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve@example>"))
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "CANCEL", "<cancel@example>"))

    worker.poll_once()

    assert queue.get("042").status is TaskStatus.CANCELLED
    assert "maze_world" not in queue.workspace_locks
    assert "preserved" in mail.sent[-1].body.lower()
    assert handoff.events[-1][0].state == "cancelled"


def test_ready_controller_sends_final_approval_summary(tmp_path: Path):
    controller = FakeController([ControllerResult(AgentStatus.READY_FOR_APPROVAL, "Updated help text")])
    controller.journal.files_changed = ["README.md"]
    controller.journal.steps = [
        {"action": {"action": "git_diff"}, "result": {"ok": True, "stdout": "diff -- README.md"}}
    ]
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path, controller=controller)
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve@example>"))
    worker.poll_once()

    worker.tick_tasks()

    task = queue.get("042")
    assert task.status is TaskStatus.READY_FOR_FINAL_APPROVAL
    assert task.diff_summary == "diff -- README.md"
    assert "Final approval" in mail.sent[-1].body
    assert "README.md" in mail.sent[-1].body
    assert "alexandria/042-task" in mail.sent[-1].body
    event = handoff.events[-1][0]
    assert event.state == "review-ready"
    assert event.changed_files == ("README.md",)
    assert handoff.saved_statuses[-1] is TaskStatus.READY_FOR_FINAL_APPROVAL


def test_final_approve_triggers_git_finalize_and_marks_done(tmp_path: Path):
    controller = FakeController([ControllerResult(AgentStatus.READY_FOR_APPROVAL, "Done")])
    controller.journal.files_changed = ["README.md"]
    controller.journal.steps = [
        {"action": {"action": "git_diff"}, "result": {"ok": True, "stdout": "diff -- README.md"}}
    ]
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path, controller=controller)
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve@example>"))
    worker.poll_once()
    worker.tick_tasks()

    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<final-approve@example>"))
    worker.poll_once()

    assert len(git.finalize_calls) == 1
    assert queue.get("042").status is TaskStatus.DONE
    assert "maze_world" not in queue.workspace_locks
    assert "completed" in mail.sent[-1].body.lower()
    event = handoff.events[-1][0]
    assert event.state == "completed"
    assert event.commits == ("commit1", "merge1")
    assert event.uncommitted_changes is False
    assert handoff.saved_statuses[-1] is TaskStatus.DONE


def test_start_approve_never_calls_git_finalize(tmp_path: Path):
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path)
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve@example>"))

    worker.poll_once()

    assert git.finalize_calls == []
    assert queue.get("042").status is TaskStatus.ACTIVE


def test_recover_recreates_controller_for_active_task(tmp_path: Path):
    controller = FakeController()
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path, controller=controller)
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve@example>"))
    worker.poll_once()
    worker.controllers.clear()

    worker.recover()

    assert "042" in worker.controllers
    assert queue.workspace_locks["maze_world"] == "042"
    assert handoff.events[-1][0].state == "recovered"
    assert handoff.saved_statuses[-1] is TaskStatus.ACTIVE


def test_full_email_task_lifecycle(tmp_path: Path):
    controller = FakeController([ControllerResult(AgentStatus.READY_FOR_APPROVAL, "Finished safely")])
    controller.journal.files_changed = ["README.md"]
    controller.journal.steps = [
        {"action": {"action": "git_diff"}, "result": {"ok": True, "stdout": "diff -- README.md"}}
    ]
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path, controller=controller)

    mail.inbox.append(task_mail())
    worker.poll_once()
    assert queue.get("042").status is TaskStatus.WAITING_CONFIRMATION

    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve@example>"))
    worker.poll_once()
    assert queue.get("042").status is TaskStatus.ACTIVE
    assert queue.get("042").task_branch.startswith("alexandria/042-")

    worker.tick_tasks()
    assert queue.get("042").status is TaskStatus.READY_FOR_FINAL_APPROVAL
    assert "Final approval" in mail.sent[-1].body

    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<final@example>"))
    worker.poll_once()
    assert queue.get("042").status is TaskStatus.DONE
    assert git.finalize_calls
    assert "completed" in mail.sent[-1].body.lower()

    restored = TaskQueue.load(tmp_path / "tasks.json")
    assert restored.get("042").status is TaskStatus.DONE
    assert restored.get("042").task_branch.startswith("alexandria/042-")


def test_blocked_controller_keeps_workspace_reserved_and_notifies_user(tmp_path: Path):
    controller = FakeController([ControllerResult(AgentStatus.BLOCKED, "")])
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path, controller=controller)
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve-block@example>"))
    worker.poll_once()

    worker.tick_tasks()

    assert queue.get("042").status is TaskStatus.BLOCKED
    assert queue.workspace_locks["maze_world"] == "042"
    assert "blocked" in mail.sent[-1].subject.lower()
    assert "Reply CANCEL" in mail.sent[-1].body
    event = handoff.events[-1][0]
    assert event.state == "blocked"
    assert event.blocker == "No detailed failure reason was recorded."
    assert handoff.saved_statuses[-1] is TaskStatus.BLOCKED


def test_branch_creation_failure_publishes_saved_blocked_event(tmp_path: Path):
    worker, mail, queue, git, controllers, handoff = make_worker(
        tmp_path, git=FakeGit(create_error=RuntimeError("dirty worktree")),
    )
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE"))

    worker.poll_once()

    assert queue.get("042").status is TaskStatus.BLOCKED
    assert handoff.events[-1][0].state == "blocked"
    assert handoff.events[-1][0].blocker == "dirty worktree"
    assert handoff.saved_statuses[-1] is TaskStatus.BLOCKED


def test_final_rejection_publishes_blocked_event(tmp_path: Path):
    controller = FakeController([ControllerResult(AgentStatus.READY_FOR_APPROVAL, "Ready")])
    worker, mail, queue, git, controllers, handoff = make_worker(tmp_path, controller=controller)
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve@example>"))
    worker.poll_once()
    worker.tick_tasks()
    mail.inbox.append(reply_mail("<task1@example>", "REJECT", "<reject@example>"))

    worker.poll_once()

    assert queue.get("042").status is TaskStatus.BLOCKED
    assert handoff.events[-1][0].state == "blocked"
    assert "rejected" in handoff.events[-1][0].blocker.lower()


def test_finalization_failure_publishes_blocked_event(tmp_path: Path):
    controller = FakeController([ControllerResult(AgentStatus.READY_FOR_APPROVAL, "Ready")])
    git = FakeGit(finalize_result=FinalizeResult(blocked_reason="target branch moved"))
    worker, mail, queue, git, controllers, handoff = make_worker(
        tmp_path, controller=controller, git=git,
    )
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<approve@example>"))
    worker.poll_once()
    worker.tick_tasks()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE", "<final@example>"))

    worker.poll_once()

    assert queue.get("042").status is TaskStatus.BLOCKED
    assert handoff.events[-1][0].state == "blocked"
    assert handoff.events[-1][0].blocker == "target branch moved"


def test_handoff_failure_warns_once_per_event_without_blocking_task(tmp_path: Path):
    failure = HandoffDelivery(False, error="gh unavailable; token=secret")
    worker, mail, queue, git, controllers, handoff = make_worker(
        tmp_path, handoff_deliveries=[failure, failure],
    )
    mail.inbox.append(task_mail())
    worker.poll_once()
    mail.inbox.append(reply_mail("<task1@example>", "APPROVE"))
    worker.poll_once()
    task = queue.get("042")
    event = handoff.events[-1][0]

    worker._publish_handoff(task, worker.registry.get("maze_world"), "working")

    warnings = [item for item in mail.sent if item.subject.startswith("Handoff warning:")]
    assert task.status is TaskStatus.ACTIVE
    assert len(warnings) == 1
    assert "coding continued" in warnings[0].body.lower()
    assert "working" in warnings[0].body
    assert "secret" not in warnings[0].body
    assert event.key in (tmp_path / "handoff_warnings.json").read_text(encoding="utf-8")


def test_recover_sends_and_acknowledges_pending_self_update_warning(tmp_path: Path):
    warnings = StartupWarningStore(tmp_path / "startup_warnings.json")
    warnings.record("dirty-worktree", "Self-update skipped: uncommitted changes.")
    worker, mail, queue, git, controllers, handoff = make_worker(
        tmp_path,
        startup_warning_store=warnings,
    )

    worker.recover()
    worker.recover()

    sent = [item for item in mail.sent if item.subject == "Local Codex self-update warning"]
    assert len(sent) == 1
    assert "uncommitted changes" in sent[0].body
    assert warnings.pending() is None
