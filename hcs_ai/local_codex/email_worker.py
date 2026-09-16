from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Callable
from uuid import uuid4

from .git_workflow import GitWorkflow
from .github_handoff import HandoffDelivery, HandoffEvent
from .actions import detect_interactive_launch_intent
from .mail_gateway import extract_freeform_instruction, is_new_task, is_trusted_sender, parse_control
from .models import ApprovalStatus, MailMessage, TaskControl, TaskRecord, TaskStatus
from .state import AgentStatus


class EmailWorker:
    def __init__(
        self,
        *,
        mail_gateway,
        registry,
        queue,
        git_workflow: GitWorkflow,
        controller_factory: Callable,
        trusted_sender: str,
        task_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
        handoff_client=None,
        handoff_warning_store=None,
        startup_warning_store=None,
    ):
        self.mail_gateway = mail_gateway
        self.registry = registry
        self.queue = queue
        self.git_workflow = git_workflow
        self.controller_factory = controller_factory
        self.trusted_sender = trusted_sender
        self.task_id_factory = task_id_factory or (lambda: uuid4().hex[:8])
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.handoff_client = handoff_client
        self.handoff_warning_store = handoff_warning_store
        self.startup_warning_store = startup_warning_store
        self.controllers: dict[str, object] = {}

    @staticmethod
    def _handoff_error_text(error: str | None) -> str:
        text = " ".join((error or "Unknown GitHub handoff error").split())
        text = re.sub(
            r"(?i)\b(token|password|secret|authorization)\s*[:=]\s*\S+",
            r"\1=[redacted]",
            text,
        )
        return text[:500]

    def _publish_handoff(
        self,
        task: TaskRecord,
        workspace,
        state: str,
        blocker: str | None = None,
        next_action: str | None = None,
    ) -> None:
        if (
            self.handoff_client is None
            or not workspace.handoff_enabled
            or not workspace.github_repository
        ):
            return

        tests = tuple(
            f"{item.get('command', 'test')}: {'passed' if item.get('ok') else 'failed'}"
            for item in task.tests_run
        )
        commits = tuple(
            str(task.finalization[key])
            for key in ("committed_sha", "merged_sha")
            if task.finalization.get(key)
        )
        event = HandoffEvent(
            workspace_id=workspace.workspace_id,
            workspace_name=workspace.name,
            repository=workspace.github_repository,
            task_id=task.task_id,
            subject=task.subject,
            state=state,
            occurred_at=task.updated_at or self.now(),
            task_branch=task.task_branch,
            target_branch=task.target_branch or workspace.target_branch,
            changed_files=tuple(task.changed_files),
            commits=commits,
            tests=tests,
            uncommitted_changes=(False if state in {"completed", "cancelled"} else bool(task.changed_files)),
            blocker=blocker,
            next_action=next_action,
        )
        try:
            delivery = self.handoff_client.publish(
                event,
                configured_issue_number=workspace.handoff_issue_number,
            )
        except Exception as exc:
            delivery = HandoffDelivery(False, error=str(exc))
        if delivery.ok:
            return

        if self.handoff_warning_store is not None:
            try:
                if self.handoff_warning_store.contains(event.key):
                    return
            except (OSError, ValueError):
                pass

        self.mail_gateway.send(
            self.trusted_sender,
            f"Handoff warning: {task.subject} [{task.task_id}]",
            (
                "Coding continued, but Alexandria could not post the GitHub handoff.\n"
                f"Handoff state: {state}\n"
                f"Error: {self._handoff_error_text(delivery.error)}"
            ),
            in_reply_to=task.thread_id,
            references=[task.thread_id] if task.thread_id else [],
        )
        if self.handoff_warning_store is not None:
            self.handoff_warning_store.add(event.key)

    def _thread_ids(self, message: MailMessage) -> list[str]:
        return [message.in_reply_to or "", *message.references]

    def _find_task_for_reply(self, message: MailMessage) -> TaskRecord | None:
        return self.queue.find_by_thread(self._thread_ids(message))

    def _resolve_workspace(self, message: MailMessage):
        combined = f"{message.subject}\n{message.body}"
        return self.registry.resolve_explicit(combined) or self.registry.infer(combined)

    def _task_slug(self, task: TaskRecord) -> str:
        subject = task.subject
        if subject.lower().startswith("task:"):
            subject = subject[5:]
        return subject.strip() or "task"

    def _confirmation_body(self, task: TaskRecord, workspace) -> str:
        tests = ", ".join(workspace.test_commands) or "none configured"
        launch = "Yes" if task.allow_interactive_launch else "No"
        return (
            f"Task ID: {task.task_id}\n"
            f"Workspace: {workspace.name}\n"
            f"Target branch: {workspace.target_branch}\n"
            f"Request: {task.body}\n"
            f"Likely tests: {tests}\n"
            f"Interactive launch requested: {launch}\n\n"
            "Alexandria has not started work yet.\n"
            "Reply APPROVE to start, REJECT to cancel, or reply with revised instructions.\n"
            "This confirmation expires in 24 hours."
        )

    def _send_confirmation(self, task: TaskRecord) -> None:
        workspace = self.registry.get(task.workspace_id)
        self.mail_gateway.send(
            self.trusted_sender,
            f"Re: {task.subject} [{task.task_id}]",
            self._confirmation_body(task, workspace),
            in_reply_to=task.thread_id,
            references=[task.thread_id] if task.thread_id else [],
        )
        self.queue.mark_confirmation_sent(task.task_id, self.now())

    def _send_available_confirmations(self) -> None:
        for workspace in self.registry.all_enabled():
            task = self.queue.next_confirmable(workspace.workspace_id)
            if task is not None:
                self._send_confirmation(task)

    def _new_task(self, message: MailMessage) -> None:
        workspace = self._resolve_workspace(message)
        task = TaskRecord(
            task_id=self.task_id_factory(),
            thread_id=message.message_id,
            sender=message.sender,
            subject=message.subject,
            body=message.body,
            workspace_id=workspace.workspace_id if workspace else None,
            status=TaskStatus.QUEUED,
            approval_status=ApprovalStatus.NOT_REQUESTED,
            created_at=self.now(),
            allow_interactive_launch=detect_interactive_launch_intent(f"{message.subject} {message.body}"),
        )
        self.queue.enqueue(task)
        if workspace is None:
            self.mail_gateway.send(
                self.trusted_sender,
                f"Re: {message.subject} [{task.task_id}]",
                "I could not confidently identify a registered workspace. Reply with the workspace name; no work has started.",
                in_reply_to=message.message_id,
                references=[message.message_id] if message.message_id else [],
            )

    def _start_task(self, task: TaskRecord) -> None:
        workspace = self.registry.get(task.workspace_id)
        try:
            branch = self.git_workflow.create_task_branch(
                workspace,
                task.task_id,
                self._task_slug(task),
            )
        except Exception as exc:
            task.status = TaskStatus.BLOCKED
            task.diff_summary = f"Branch setup blocked: {exc}"
            self.queue.save()
            self._publish_handoff(
                task,
                workspace,
                "blocked",
                blocker=str(exc),
                next_action="Review the branch setup failure or reply CANCEL to release the workspace.",
            )
            self.mail_gateway.send(
                self.trusted_sender,
                f"Blocked: {task.subject} [{task.task_id}]",
                f"Task could not start: {exc}",
                in_reply_to=task.thread_id,
                references=[task.thread_id] if task.thread_id else [],
            )
            return

        task.task_branch = branch.task_branch
        task.target_branch = branch.target_branch
        task.expected_target_sha = branch.expected_target_sha
        self.queue.activate(task.task_id)
        self._publish_handoff(
            task,
            workspace,
            "working",
            next_action="Alexandria is implementing the approved task.",
        )
        controller = self.controller_factory(task, workspace)
        journal_path = getattr(getattr(controller, "journal", None), "path", None)
        if journal_path is not None:
            task.journal_path = str(journal_path)
            self.queue.save()
        self.controllers[task.task_id] = controller

    def _handle_pending_reply(self, task: TaskRecord, message: MailMessage) -> None:
        control = parse_control(message)
        if control is TaskControl.APPROVE:
            try:
                self.queue.approve(task.task_id, self.now())
            except RuntimeError as exc:
                self.mail_gateway.send(
                    self.trusted_sender,
                    f"Approval expired: {task.subject} [{task.task_id}]",
                    str(exc),
                    in_reply_to=task.thread_id,
                    references=[task.thread_id] if task.thread_id else [],
                )
                return
            self._start_task(task)
            return
        if control is TaskControl.REJECT:
            self.queue.reject(task.task_id, self.now())
            workspace = self.registry.get(task.workspace_id)
            self._publish_handoff(
                task,
                workspace,
                "cancelled",
                next_action="No work started; the approval request was rejected.",
            )
            return

        instruction = extract_freeform_instruction(message)
        if instruction:
            task.body = instruction
            task.approval_status = ApprovalStatus.NOT_REQUESTED
            task.status = TaskStatus.QUEUED
            task.confirmation_sent_at = None
            task.updated_at = self.now()
            self.queue.save()

    def _handle_workspace_reply(self, task: TaskRecord, message: MailMessage) -> None:
        instruction = extract_freeform_instruction(message)
        workspace = self.registry.resolve_explicit(instruction or "")
        if workspace is None:
            names = ", ".join(item.name for item in self.registry.all_enabled())
            self.mail_gateway.send(
                self.trusted_sender,
                f"Workspace needed: {task.subject} [{task.task_id}]",
                f"I still could not match that reply. Reply with one registered workspace name: {names}.",
                in_reply_to=task.thread_id,
                references=[task.thread_id] if task.thread_id else [],
            )
            return

        task.workspace_id = workspace.workspace_id
        task.updated_at = self.now()
        self.queue.save()

    def _handle_active_reply(self, task: TaskRecord, message: MailMessage) -> None:
        controller = self.controllers.get(task.task_id)
        control = parse_control(message)
        if control is TaskControl.PAUSE:
            if controller is not None:
                controller.set_pause_requested(True)
            self.queue.pause(task.task_id)
            workspace = self.registry.get(task.workspace_id)
            self._publish_handoff(
                task,
                workspace,
                "paused",
                next_action="Wait for Matthew to reply RESUME or CANCEL.",
            )
            return
        if control is TaskControl.RESUME:
            if task.status is TaskStatus.PAUSED:
                self.queue.resume(task.task_id)
                workspace = self.registry.get(task.workspace_id)
                self._publish_handoff(
                    task,
                    workspace,
                    "working",
                    next_action="Alexandria resumed the task.",
                )
            if controller is not None:
                controller.set_pause_requested(False)
            return
        if control is TaskControl.CANCEL:
            self.queue.cancel(task.task_id)
            self.controllers.pop(task.task_id, None)
            workspace = self.registry.get(task.workspace_id)
            self._publish_handoff(
                task,
                workspace,
                "cancelled",
                next_action="Workspace ownership is released; preserved changes may be reviewed later.",
            )
            self.mail_gateway.send(
                self.trusted_sender,
                f"Cancelled: {task.subject} [{task.task_id}]",
                "Task stopped. All uncommitted workspace changes were preserved. Nothing was reverted automatically.",
                in_reply_to=task.thread_id,
                references=[task.thread_id] if task.thread_id else [],
            )
            return

        instruction = extract_freeform_instruction(message)
        if instruction and controller is not None:
            controller.add_instruction(instruction)
            task.freeform_instructions.append(instruction)
            task.updated_at = self.now()
            self.queue.save()

    def _final_commit_message(self, task: TaskRecord) -> str:
        subject = task.subject
        if subject.lower().startswith("task:"):
            subject = subject[5:]
        clean = " ".join(subject.split()) or f"task {task.task_id}"
        return f"alexandria: {clean[:72]}"

    def _send_final_approval(self, task: TaskRecord, workspace, summary: str) -> None:
        changed = "\n".join(f"- {name}" for name in task.changed_files) or "- none"
        tests = "\n".join(
            f"- {item.get('command', 'test')}: {'PASS' if item.get('ok') else 'FAIL'}"
            for item in task.tests_run
        ) or "- none required"
        body = (
            f"Final approval required for task {task.task_id}.\n"
            f"Workspace: {workspace.name}\n"
            f"Task branch: {task.task_branch}\n"
            f"Target branch: {task.target_branch}\n"
            f"Summary: {summary or task.body}\n\n"
            f"Files changed:\n{changed}\n\n"
            f"Tests:\n{tests}\n\n"
            f"Diff summary:\n{task.diff_summary or 'No diff summary available'}\n\n"
            f"Proposed commit message: {self._final_commit_message(task)}\n\n"
            "Reply APPROVE to commit, push, merge safely, and push the target branch. "
            "Reply REJECT to preserve the current task branch without finalizing."
        )
        self.mail_gateway.send(
            self.trusted_sender,
            f"Final approval: {task.subject} [{task.task_id}]",
            body,
            in_reply_to=task.thread_id,
            references=[task.thread_id] if task.thread_id else [],
        )

    def _handle_final_reply(self, task: TaskRecord, message: MailMessage) -> None:
        control = parse_control(message)
        if control is TaskControl.REJECT:
            workspace = self.registry.get(task.workspace_id)
            task.status = TaskStatus.BLOCKED
            task.updated_at = self.now()
            self.queue.save()
            self._publish_handoff(
                task,
                workspace,
                "blocked",
                blocker="Matthew rejected finalization.",
                next_action="Review the preserved task branch or reply CANCEL to release the workspace.",
            )
            self.mail_gateway.send(
                self.trusted_sender,
                f"Finalization rejected: {task.subject} [{task.task_id}]",
                "Finalization was rejected. The task branch and uncommitted/committed state were preserved; no merge or target push was performed.",
                in_reply_to=task.thread_id,
                references=[task.thread_id] if task.thread_id else [],
            )
            return
        if control is not TaskControl.APPROVE:
            return

        workspace = self.registry.get(task.workspace_id)
        task.status = TaskStatus.FINALIZING
        task.updated_at = self.now()
        self.queue.save()
        result = self.git_workflow.finalize(
            task,
            workspace,
            self._final_commit_message(task),
            checkpoint=lambda updated: self.queue.save(),
        )
        if result.blocked_reason:
            task.status = TaskStatus.BLOCKED
            task.updated_at = self.now()
            self.queue.save()
            self._publish_handoff(
                task,
                workspace,
                "blocked",
                blocker=result.blocked_reason,
                next_action="Review the finalization failure or reply CANCEL to release the workspace.",
            )
            self.mail_gateway.send(
                self.trusted_sender,
                f"Finalization blocked: {task.subject} [{task.task_id}]",
                f"Finalization stopped safely: {result.blocked_reason}",
                in_reply_to=task.thread_id,
                references=[task.thread_id] if task.thread_id else [],
            )
            return

        task.status = TaskStatus.DONE
        task.updated_at = self.now()
        task.finalization.update({
            "committed_sha": result.committed_sha,
            "merged_sha": result.merged_sha,
            "commit_created": bool(result.committed_sha),
            "task_branch_pushed": bool(result.task_branch_pushed),
            "merge_completed": bool(result.merged_sha),
            "target_pushed": bool(result.target_pushed),
        })
        if task.workspace_id:
            self.queue.release_workspace(task.workspace_id)
        self.queue.save()
        self.controllers.pop(task.task_id, None)
        self._publish_handoff(
            task,
            workspace,
            "completed",
            next_action="No action required; workspace ownership is released.",
        )
        self.mail_gateway.send(
            self.trusted_sender,
            f"Completed: {task.subject} [{task.task_id}]",
            (
                f"Task {task.task_id} completed successfully.\n"
                f"Commit: {result.committed_sha}\n"
                f"Merged commit: {result.merged_sha}\n"
                f"Target branch pushed: {result.target_pushed}"
            ),
            in_reply_to=task.thread_id,
            references=[task.thread_id] if task.thread_id else [],
        )

    def recover(self) -> None:
        self._send_startup_warning()
        self.queue.rebuild_workspace_locks()
        for task in self.queue.tasks.values():
            if not task.workspace_id:
                continue
            if task.status in {TaskStatus.ACTIVE, TaskStatus.PAUSED}:
                workspace = self.registry.get(task.workspace_id)
                controller = self.controller_factory(task, workspace)
                if task.status is TaskStatus.PAUSED:
                    controller.set_pause_requested(True)
                self.controllers[task.task_id] = controller
                self._publish_handoff(
                    task,
                    workspace,
                    "recovered",
                    next_action=(
                        "Task recovered in paused state; wait for RESUME or CANCEL."
                        if task.status is TaskStatus.PAUSED
                        else "Task recovered after restart and Alexandria will continue."
                    ),
                )
            elif task.status is TaskStatus.FINALIZING:
                workspace = self.registry.get(task.workspace_id)
                result = self.git_workflow.finalize(
                    task,
                    workspace,
                    self._final_commit_message(task),
                    checkpoint=lambda updated: self.queue.save(),
                )
                if result.blocked_reason:
                    task.status = TaskStatus.BLOCKED
                    task.updated_at = self.now()
                    self.queue.save()
                    self._publish_handoff(
                        task,
                        workspace,
                        "blocked",
                        blocker=result.blocked_reason,
                        next_action="Review the recovered finalization failure or reply CANCEL.",
                    )
                else:
                    task.status = TaskStatus.DONE
                    task.updated_at = self.now()
                    task.finalization.update({
                        "committed_sha": result.committed_sha,
                        "merged_sha": result.merged_sha,
                        "commit_created": bool(result.committed_sha),
                        "task_branch_pushed": bool(result.task_branch_pushed),
                        "merge_completed": bool(result.merged_sha),
                        "target_pushed": bool(result.target_pushed),
                    })
                    self.queue.release_workspace(task.workspace_id)
                    self.queue.save()
                    self._publish_handoff(
                        task,
                        workspace,
                        "completed",
                        next_action="No action required; recovered finalization completed.",
                    )

    def _send_startup_warning(self) -> None:
        if self.startup_warning_store is None:
            return
        try:
            pending = self.startup_warning_store.pending()
            if pending is None:
                return
            self.mail_gateway.send(
                self.trusted_sender,
                "Local Codex self-update warning",
                (
                    f"{pending['message']}\n\n"
                    "Alexandria started normally. No local files were overwritten."
                ),
            )
            self.startup_warning_store.acknowledge(pending["key"])
        except (OSError, RuntimeError, ValueError):
            return

    def process_message(self, message: MailMessage) -> None:
        if not is_trusted_sender(message, self.trusted_sender):
            return

        if is_new_task(message) and not message.in_reply_to:
            self._new_task(message)
            return

        task = self._find_task_for_reply(message)
        if task is None:
            return

        if task.status is TaskStatus.QUEUED and task.workspace_id is None:
            self._handle_workspace_reply(task, message)
            return
        if task.status is TaskStatus.WAITING_CONFIRMATION:
            self._handle_pending_reply(task, message)
            return
        if task.status in {TaskStatus.ACTIVE, TaskStatus.PAUSED, TaskStatus.BLOCKED}:
            self._handle_active_reply(task, message)
            return
        if task.status is TaskStatus.READY_FOR_FINAL_APPROVAL:
            self._handle_final_reply(task, message)

    def poll_once(self) -> None:
        for message in self.mail_gateway.poll():
            try:
                self.process_message(message)
            finally:
                self.mail_gateway.mark_processed(message.message_id)
        self.queue.expire_confirmations(self.now())
        self._send_available_confirmations()

    def tick_tasks(self) -> None:
        for task_id, controller in list(self.controllers.items()):
            task = self.queue.tasks.get(task_id)
            if task is None or task.status is not TaskStatus.ACTIVE:
                continue
            result = controller.run_one_step()
            if result.status is AgentStatus.PAUSED:
                if task.status is TaskStatus.ACTIVE:
                    self.queue.pause(task_id)
                    workspace = self.registry.get(task.workspace_id)
                    self._publish_handoff(
                        task,
                        workspace,
                        "paused",
                        next_action="Wait for Matthew to reply RESUME or CANCEL.",
                    )
                continue
            if result.status is AgentStatus.BLOCKED:
                task.status = TaskStatus.BLOCKED
                task.updated_at = self.now()
                self.queue.save()
                failure_reason = next(
                    (
                        step.get("result", {}).get("error")
                        for step in reversed(getattr(controller.journal, "steps", []))
                        if not step.get("result", {}).get("ok")
                        and step.get("result", {}).get("error")
                    ),
                    "No detailed failure reason was recorded.",
                )
                workspace = self.registry.get(task.workspace_id)
                self._publish_handoff(
                    task,
                    workspace,
                    "blocked",
                    blocker=failure_reason,
                    next_action="Review the failure or reply CANCEL to release the workspace.",
                )
                self.mail_gateway.send(
                    self.trusted_sender,
                    f"Blocked: {task.subject} [{task.task_id}]",
                    (
                        "The coding task is blocked. The workspace remains reserved and all "
                        "current changes were preserved for review.\n\n"
                        f"Reason: {failure_reason}\n\n"
                        "Reply CANCEL to release the workspace while preserving the task record."
                    ),
                    in_reply_to=task.thread_id,
                    references=[task.thread_id] if task.thread_id else [],
                )
                continue
            if result.status is AgentStatus.READY_FOR_APPROVAL:
                task.status = TaskStatus.READY_FOR_FINAL_APPROVAL
                task.changed_files = list(getattr(controller.journal, "files_changed", []))
                task.tests_run = list(getattr(controller.journal, "tests_run", []))
                diff_steps = [
                    step for step in getattr(controller.journal, "steps", [])
                    if step.get("action", {}).get("action") == "git_diff"
                    and step.get("result", {}).get("ok")
                ]
                if diff_steps:
                    task.diff_summary = diff_steps[-1]["result"].get("stdout", "diff inspected") or "diff inspected"
                self.queue.save()
                workspace = self.registry.get(task.workspace_id)
                self._publish_handoff(
                    task,
                    workspace,
                    "review-ready",
                    next_action="Matthew must reply APPROVE to finalize or REJECT to preserve the branch.",
                )
                self._send_final_approval(task, workspace, result.summary)
