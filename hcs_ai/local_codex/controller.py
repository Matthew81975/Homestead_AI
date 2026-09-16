from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass

from .actions import ActionValidationError, parse_action
from .state import AgentStatus, TaskJournal
from .lm_client import LMStudioError


def build_system_prompt(*, include_project_control: bool = True, include_tornado_status: bool = False) -> str:
    actions = [
        '{"action":"list_files","path":".","recursive":false}',
        '{"action":"read_file","path":"relative/path"}',
        '{"action":"search_text","text":"literal search text","path":"."}',
        '{"action":"write_file","path":"relative/path","content":"complete file text"}',
        '{"action":"replace_text","path":"relative/path","old":"exact old text","new":"replacement text"}',
        '{"action":"run_command","command":"allowed command"}',
        '{"action":"git_status"}',
        '{"action":"git_diff"}',
    ]
    if include_tornado_status:
        actions.append('{"action":"tornado_status"}')
        actions.append('{"action":"tornado_probe"}')
    if include_project_control:
        actions.extend([
            '{"action":"project_state"}',
            '{"action":"project_ui"}',
            '{"action":"project_command","command":"name","arguments":{}}',
        ])
    actions.append('{"action":"finish","summary":"what was done","tests":[]}')
    action_text = "\n".join(actions)
    tornado_guidance = ""
    if include_tornado_status:
        tornado_guidance = (
            "Use tornado_status for Tornado provider diagnostics, health, credential-presence, cooldown, and retry information.\n"
            "Use tornado_probe only when the task explicitly asks to live-test eligible Tornado cloud lanes; it makes one tiny API request per eligible cloud lane and consumes a small amount of quota.\n"
            "tornado_status and tornado_probe never modify workspace/project files; never use project_command for Tornado diagnostics.\n"
            "Reporting-only tornado_status/tornado_probe tasks complete deterministically after the successful diagnostic action.\n"
        )
    project_control_guidance = ""
    if include_project_control:
        project_control_guidance = (
            "project_state and project_ui are read-only observation actions; use them to inspect the running application.\n"
            "project_command is only for changing or interacting with the running application.\n"
            "Only use project_command names advertised by project_ui; never invent command names.\n"
            "For read-only inspection tasks, do not use project_command; use project_state and project_ui, then finish.\n"
            "Successful Project Control observations are retained in project_observations even after they leave recent_steps.\n"
            "When observation_complete.complete is true for a read-only inspection task, use the retained observations and finish.\n"
        )
    return (
        "You are a local coding agent operating through a restricted controller.\n"
        "Return exactly one JSON object per turn and no other text.\n"
        "Never invent file contents, command output, test output, or Git state.\n"
        "You do not have direct filesystem or shell access.\n"
        "Allowed actions and exact schemas (use these key names):\n"
        f"{action_text}\n"
        "search_text uses text, not query or pattern.\n"
        f"{tornado_guidance}"
        f"{project_control_guidance}"
        "Use relative workspace paths only.\n"
        "Never request git commit, git push, or package installation.\n"
        "Inspect relevant files before editing.\n"
        "After code edits, run relevant tests and inspect git_diff before finish.\n"
        "For documentation/text-only edits, inspect git_diff; do not execute the document as a test.\n"
        "Prefer minimal, targeted changes.\n"
        "If a tool action fails, use the returned result to correct the next action."
    )


@dataclass
class ControllerResult:
    status: AgentStatus
    summary: str = ""


class AgentController:
    def __init__(self, client, executor, journal: TaskJournal, max_failed_actions: int, status_callback=None, cancel_event=None):
        self.client = client
        self.executor = executor
        self.journal = journal
        self.max_failed_actions = max_failed_actions
        self.status_callback = status_callback or (lambda message: None)
        self.cancel_event = cancel_event
        self.seen_diff = any(
            step.get("action", {}).get("action") == "git_diff"
            and step.get("result", {}).get("ok")
            for step in journal.steps
        )
        self.last_failed_action_key = None
        self.repeated_failed_action = None
        self.last_successful_inspection_key = None
        self.project_observations_since_command: set[str] = set()
        for step in reversed(journal.steps):
            action_name = step.get("action", {}).get("action")
            if action_name == "project_command" and step.get("result", {}).get("ok"):
                break
            if (
                action_name in {"project_state", "project_ui"}
                and step.get("result", {}).get("ok")
            ):
                self.project_observations_since_command.add(action_name)
        self.pause_requested = False
        self.pending_instructions: list[str] = []
        self.seen_passing_tests = any(
            item.get("ok") for item in journal.tests_run
        ) or any(
            step.get("action", {}).get("action") == "run_command"
            and "pytest" in step.get("action", {}).get("command", "").lower()
            and step.get("result", {}).get("ok")
            for step in journal.steps
        )

    def _status(self, message: str) -> None:
        self.status_callback(message)

    def _report_model_outcome(self, success: bool, signal: str) -> None:
        reporter = getattr(self.client, "report_outcome", None)
        if callable(reporter):
            reporter(bool(success), signal=signal)

    def _compact_value(self, value):
        if isinstance(value, str) and len(value) > 1800:
            return value[:1800] + "...[truncated]"
        if isinstance(value, list):
            items = [self._compact_value(item) for item in value[:50]]
            if len(value) > 50:
                items.append(f"...[{len(value) - 50} more items truncated]")
            return items
        if isinstance(value, dict):
            return {key: self._compact_value(item) for key, item in value.items()}
        return value

    def _project_observation_context(self) -> tuple[dict[str, object], list[str]]:
        observations: dict[str, object] = {}
        advertised_commands: set[str] = set()
        for step in reversed(self.journal.steps):
            action = step.get("action", {})
            result = step.get("result", {})
            action_name = action.get("action")
            if action_name == "project_command" and result.get("ok"):
                break
            if not result.get("ok"):
                continue
            if action_name == "project_state" and "project_state" not in observations:
                observations["project_state"] = result.get("state", {})
            elif action_name == "project_ui" and "project_ui" not in observations:
                ui = result.get("ui", {})
                observations["project_ui"] = ui
                if isinstance(ui, dict):
                    controls = ui.get("controls", [])
                    if isinstance(controls, list):
                        for item in controls:
                            if isinstance(item, dict):
                                command = item.get("command")
                                if isinstance(command, str) and command:
                                    advertised_commands.add(command)
        return observations, sorted(advertised_commands)

    def _latest_tornado_status_context(self):
        for step in reversed(self.journal.steps):
            action = step.get("action", {})
            result = step.get("result", {})
            if action.get("action") == "tornado_status" and result.get("ok"):
                return result.get("tornado", {})
        return None

    @staticmethod
    def _compact_tornado_status_context(status):
        if not isinstance(status, dict):
            return {}
        providers = []
        for item in status.get("providers", []):
            if not isinstance(item, dict):
                continue
            key_present = item.get("api_key_present")
            required_present = bool(item.get("required_envs_present", True))
            credentials_present = required_present and key_present is not False
            providers.append({
                "provider": item.get("provider"),
                "model": item.get("model"),
                "eligible": bool(item.get("eligible")),
                "health_state": item.get("health_state"),
                "credentials_present": credentials_present,
                "retry_in_seconds": float(item.get("retry_in_seconds", 0.0) or 0.0),
            })
        return {
            "provider_count": int(status.get("provider_count", len(providers)) or 0),
            "eligible_count": int(status.get("eligible_count", 0) or 0),
            "providers": providers,
        }

    @staticmethod
    def _is_tornado_reporting_task(task: str) -> bool:
        text = " ".join(str(task).lower().split())
        if "tornado" not in text:
            return False
        mutation_terms = (
            "implement", "modify", "edit", "change code", "write code", "fix code",
            "refactor", "build feature", "add feature", "delete", "rename",
        )
        if any(term in text for term in mutation_terms):
            return False
        return any(term in text for term in (
            "status", "report", "provider", "lane", "health", "diagnostic", "probe", "live-test", "test every",
        ))

    @staticmethod
    def _format_optional(value) -> str:
        return "none" if value is None else str(value)

    @classmethod
    def _format_tornado_status_report(cls, status) -> str:
        if not isinstance(status, dict):
            return "Tornado status unavailable."
        providers = status.get("providers", [])
        lines = [
            f"Tornado status: {int(status.get('eligible_count', 0) or 0)}/{int(status.get('provider_count', len(providers)) or 0)} eligible"
        ]
        for item in providers:
            if not isinstance(item, dict):
                continue
            key_present = item.get("api_key_present")
            required_present = bool(item.get("required_envs_present", True))
            if key_present is None and required_present:
                credentials = "n/a"
            elif key_present is True and required_present:
                credentials = "present"
            else:
                credentials = "missing"
            recovery = item.get("learned_budget_recovery_seconds")
            confidence = float(item.get("learned_budget_recovery_confidence", 0.0) or 0.0)
            lines.append(
                "- {provider} | model={model} | credentials={credentials} | eligible={eligible} | "
                "health={health} | last_success={last_success} | last_failure={last_failure} | "
                "failure_category={category} | cooldown={cooldown:g}s | retry={retry:g}s | "
                "learned_recovery={recovery} | recovery_confidence={confidence:g}".format(
                    provider=item.get("provider"),
                    model=item.get("model"),
                    credentials=credentials,
                    eligible="yes" if item.get("eligible") else "no",
                    health=item.get("health_state"),
                    last_success=cls._format_optional(item.get("last_success_at")),
                    last_failure=cls._format_optional(item.get("last_failure_at")),
                    category=cls._format_optional(item.get("last_failure_category")),
                    cooldown=float(item.get("cooldown_in_seconds", 0.0) or 0.0),
                    retry=float(item.get("retry_in_seconds", 0.0) or 0.0),
                    recovery=cls._format_optional(recovery),
                    confidence=confidence,
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _format_tornado_probe_report(probe) -> str:
        if not isinstance(probe, dict):
            return "Tornado probe unavailable."
        attempted = int(probe.get("attempted_count", 0) or 0)
        success = int(probe.get("success_count", 0) or 0)
        lines = [f"Tornado probe: {success}/{attempted} succeeded"]
        for item in probe.get("providers", []):
            if not isinstance(item, dict):
                continue
            line = (
                f"- {item.get('provider')} | model={item.get('model')} | "
                f"{'OK' if item.get('ok') else 'FAILED'} | "
                f"latency={float(item.get('latency_seconds', 0.0) or 0.0):.3f}s"
            )
            if not item.get("ok"):
                line += (
                    f" | category={item.get('category') or 'unknown'}"
                    f" | error={item.get('error') or 'unknown error'}"
                )
            lines.append(line)
        return "\n".join(lines)

    def _recent_steps_context(self):
        recent = []
        for step in self.journal.steps[-3:]:
            action = step.get("action", {})
            result = step.get("result", {})
            if action.get("action") == "tornado_status" and result.get("ok"):
                status = result.get("tornado", {})
                recent.append({
                    "action": dict(action),
                    "result": {
                        "ok": True,
                        "tornado": {
                            "provider_count": int(status.get("provider_count", 0) or 0),
                            "eligible_count": int(status.get("eligible_count", 0) or 0),
                        },
                    },
                })
            elif action.get("action") == "tornado_probe" and result.get("ok"):
                probe = result.get("probe", {})
                recent.append({
                    "action": dict(action),
                    "result": {
                        "ok": True,
                        "probe": {
                            "attempted_count": int(probe.get("attempted_count", 0) or 0),
                            "success_count": int(probe.get("success_count", 0) or 0),
                            "failure_count": int(probe.get("failure_count", 0) or 0),
                        },
                    },
                })
            else:
                recent.append(self._compact_value(step))
        return recent

    def _messages(self) -> list[dict[str, str]]:
        include_project_control = getattr(self.executor, "project_control", None) is not None
        include_tornado_status = getattr(self.executor, "tornado_client", None) is not None
        context = {
            "task": self.journal.task,
            "files_changed": self.journal.files_changed,
            "recent_steps": self._recent_steps_context(),
            "tests_run": self._compact_value(self.journal.tests_run[-2:]),
            "repeated_failed_action": self.repeated_failed_action,
            "pending_instructions": list(self.pending_instructions),
        }
        if include_tornado_status:
            tornado_context = self._latest_tornado_status_context()
            if tornado_context is not None:
                context["tornado_status"] = self._compact_tornado_status_context(tornado_context)
                context["tornado_status_complete"] = {
                    "complete": True,
                    "hint": (
                        "Tornado diagnostics are already available. For a reporting-only Tornado status task, "
                        "use tornado_status now and return finish as the next action. Do not call tornado_status again, "
                        "do not inspect files, and do not run project commands."
                    ),
                }
        if include_project_control:
            project_observations, advertised_project_commands = self._project_observation_context()
            observations_complete = {"project_state", "project_ui"}.issubset(project_observations)
            context.update({
                "project_observations": self._compact_value(project_observations),
                "advertised_project_commands": advertised_project_commands,
                "observation_complete": {
                    "complete": observations_complete,
                    "hint": (
                        "Both Project Control observations are available. For a read-only inspection task, "
                        "use these retained observations and finish now; do not issue project_command or reread them."
                        if observations_complete
                        else "Read project_state and project_ui as needed before concluding a Project Control inspection."
                    ),
                },
            })
        return [
            {"role": "system", "content": build_system_prompt(
                include_project_control=include_project_control,
                include_tornado_status=include_tornado_status,
            )},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]


    def set_pause_requested(self, requested: bool) -> None:
        self.pause_requested = bool(requested)

    def add_instruction(self, text: str) -> None:
        value = text.strip()
        if value:
            self.pending_instructions.append(value)

    def _record_failure(self, reason: str) -> None:
        self.journal.record_failure()
        self.journal.record_step(
            {"action": "controller_error"},
            {"ok": False, "error": reason},
        )

    @staticmethod
    def _file_requires_tests(path: str) -> bool:
        test_required_suffixes = {
            ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx",
            ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
            ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1",
        }
        return Path(path).suffix.lower() in test_required_suffixes

    def _completion_allowed(self) -> bool:
        if not self.journal.files_changed:
            return True
        if not self.seen_diff:
            return False
        requires_tests = any(
            self._file_requires_tests(path)
            for path in self.journal.files_changed
        )
        return self.seen_passing_tests if requires_tests else True

    def _ensure_bootstrap(self) -> None:
        if self.journal.steps:
            return
        self._status("Workspace bootstrap: listing top-level files...")
        bootstrap_action = {"action": "list_files", "path": ".", "recursive": False}
        try:
            bootstrap_result = self.executor.execute(bootstrap_action)
        except (
            ActionValidationError,
            KeyError,
            ValueError,
            PermissionError,
            FileNotFoundError,
        ) as exc:
            self._record_failure(f"workspace bootstrap failed: {exc}")
            return

        self.journal.record_step(bootstrap_action, bootstrap_result)
        count = len(bootstrap_result.get("files", [])) if bootstrap_result.get("ok") else 0
        self._status(f"Workspace bootstrap: {count} entries loaded.")
        if bootstrap_result.get("ok"):
            self.journal.clear_failures()
        else:
            self.journal.record_failure()

    def run_one_step(self) -> ControllerResult:
        if self.journal.status not in {AgentStatus.PAUSED, AgentStatus.BLOCKED}:
            self.journal.status = AgentStatus.WORKING
            self.journal.save()

        self._ensure_bootstrap()

        if self.pause_requested:
            self.journal.status = AgentStatus.PAUSED
            self.journal.save()
            return ControllerResult(AgentStatus.PAUSED)

        if self.journal.consecutive_failures >= self.max_failed_actions:
            self.journal.status = AgentStatus.BLOCKED
            self.journal.save()
            return ControllerResult(AgentStatus.BLOCKED)

        self._status("Waiting for LM Studio...")
        try:
            raw = self.client.chat(self._messages())
        except LMStudioError as exc:
            reason = f"LM Studio error: {exc}"
            self._record_failure(reason)
            self._status(reason)
            self.journal.status = AgentStatus.BLOCKED
            self.journal.save()
            return ControllerResult(AgentStatus.BLOCKED)

        self._status("LM Studio response received.")
        self.pending_instructions.clear()

        try:
            action = parse_action(raw)
            detail = action.get("path") or action.get("command") or action.get("text") or ""
            suffix = f" — {detail}" if detail else ""
            self._status(f"Action: {action['action']}{suffix}")
            action_key = json.dumps(action, sort_keys=True)
            guarded_inspections = {"list_files", "read_file", "search_text", "git_status", "git_diff", "project_state", "project_ui", "tornado_status", "tornado_probe"}
            if (
                action["action"] in {"project_state", "project_ui"}
                and action["action"] in self.project_observations_since_command
            ):
                self.repeated_failed_action = {
                    "action": action,
                    "hint": (
                        "You already performed this Project Control observation since the last successful "
                        "project_command. Use the retained result in project_observations and choose a different "
                        "next action. For read-only inspection tasks, finish after project_state and project_ui."
                    ),
                }
                self._report_model_outcome(False, "repeated_action")
                self._record_failure("repeated project observation rejected")
                if self.journal.consecutive_failures >= self.max_failed_actions:
                    self.journal.status = AgentStatus.BLOCKED
                    self.journal.save()
                    return ControllerResult(AgentStatus.BLOCKED)
                return ControllerResult(AgentStatus.WORKING)
            if (
                action["action"] in guarded_inspections
                and action_key == self.last_successful_inspection_key
            ):
                self.repeated_failed_action = {
                    "action": action,
                    "hint": (
                        "You repeated the exact same successful inspection. "
                        "Use the result already present in recent_steps and choose a different next action."
                    ),
                }
                self._report_model_outcome(False, "repeated_action")
                self._record_failure("repeated successful inspection rejected")
                if self.journal.consecutive_failures >= self.max_failed_actions:
                    self.journal.status = AgentStatus.BLOCKED
                    self.journal.save()
                    return ControllerResult(AgentStatus.BLOCKED)
                return ControllerResult(AgentStatus.WORKING)
            result = self.executor.execute(action)
        except (
            ActionValidationError,
            KeyError,
            ValueError,
            PermissionError,
            FileNotFoundError,
        ) as exc:
            self._report_model_outcome(False, "invalid_action")
            self._record_failure(str(exc))
            if self.journal.consecutive_failures >= self.max_failed_actions:
                self.journal.status = AgentStatus.BLOCKED
                self.journal.save()
                return ControllerResult(AgentStatus.BLOCKED)
            return ControllerResult(AgentStatus.WORKING)

        self.journal.record_step(action, result)
        if action["action"] != "finish":
            self._report_model_outcome(
                bool(result.get("ok")),
                "action_succeeded" if result.get("ok") else "action_failed",
            )
        if result.get("ok"):
            self._status("Result: ok")
        else:
            self._status(f"Result: {result.get('error', 'failed')}")

        if result.get("ok"):
            self.journal.clear_failures()
            self.last_failed_action_key = None
            self.repeated_failed_action = None
            if action["action"] in guarded_inspections:
                self.last_successful_inspection_key = json.dumps(action, sort_keys=True)
            else:
                self.last_successful_inspection_key = None
            if action["action"] in {"project_state", "project_ui"}:
                self.project_observations_since_command.add(action["action"])
            elif action["action"] == "project_command":
                self.project_observations_since_command.clear()
        else:
            action_key = json.dumps(action, sort_keys=True)
            if action_key == self.last_failed_action_key:
                self.repeated_failed_action = {
                    "action": action,
                    "hint": (
                        "You repeated the exact same failed action. "
                        "Do not retry it unchanged. Use the latest successful "
                        "list_files/search results and choose an existing path."
                    ),
                }
            else:
                self.repeated_failed_action = None
            self.last_failed_action_key = action_key
            self.journal.record_failure()

        if (
            result.get("ok")
            and action["action"] in {"tornado_status", "tornado_probe"}
            and self._is_tornado_reporting_task(self.journal.task)
            and not self.journal.files_changed
        ):
            if action["action"] == "tornado_status":
                summary = self._format_tornado_status_report(result.get("tornado", {}))
            else:
                summary = self._format_tornado_probe_report(result.get("probe", {}))
            self.journal.status = AgentStatus.READY_FOR_APPROVAL
            self.journal.save()
            return ControllerResult(AgentStatus.READY_FOR_APPROVAL, summary)

        if action["action"] == "git_diff" and result.get("ok"):
            self.seen_diff = True

        if action["action"] == "run_command":
            if "pytest" in action.get("command", "").lower() and result.get("ok"):
                self.seen_passing_tests = True

        if action["action"] == "finish":
            if not self._completion_allowed():
                self._report_model_outcome(False, "finish_rejected")
                self._record_failure(
                    "finish rejected: changed files require git diff, and code changes require passing tests"
                )
                if self.journal.consecutive_failures >= self.max_failed_actions:
                    self.journal.status = AgentStatus.BLOCKED
                    self.journal.save()
                    return ControllerResult(AgentStatus.BLOCKED)
                return ControllerResult(AgentStatus.WORKING)

            self._report_model_outcome(bool(result.get("ok")), "action_succeeded" if result.get("ok") else "action_failed")
            self.journal.status = AgentStatus.READY_FOR_APPROVAL
            self.journal.save()
            return ControllerResult(
                AgentStatus.READY_FOR_APPROVAL,
                result.get("summary", ""),
            )

        if self.journal.consecutive_failures >= self.max_failed_actions:
            self.journal.status = AgentStatus.BLOCKED
            self.journal.save()
            return ControllerResult(AgentStatus.BLOCKED)

        return ControllerResult(AgentStatus.WORKING)

    def run(self) -> ControllerResult:
        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                self.journal.status = AgentStatus.INTERRUPTED
                self.journal.save()
                return ControllerResult(AgentStatus.INTERRUPTED, "Task cancelled.")
            result = self.run_one_step()
            if result.status is not AgentStatus.WORKING:
                return result
