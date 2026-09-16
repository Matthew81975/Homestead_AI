from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any
from enum import Enum

from .state import TaskJournal
from .workspace import Workspace
from .project_control import ProjectControlError



def detect_interactive_launch_intent(text: str) -> bool:
    value = " ".join(text.strip().lower().split())
    if not value:
        return False
    negative_patterns = (
        r"\bdo not (?:run|launch|start|open)\b",
        r"\bdon't (?:run|launch|start|open)\b",
        r"\bwithout (?:running|launching|starting|opening)\b",
    )
    if any(re.search(pattern, value) for pattern in negative_patterns):
        return False
    positive = re.search(
        r"\b(?:run|launch|start|open)\s+(?:the\s+)?(?:program|application|app|game|project|maze world|[\w.-]+\.py|[\w.-]+\.bat)\b",
        value,
    )
    return positive is not None

class ActionValidationError(ValueError):
    pass


class CommandClass(str, Enum):
    TEST = "test"
    INSPECTION = "inspection"
    LAUNCH = "launch"
    OTHER_ALLOWED = "other_allowed"


ACTION_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "list_files": (),
    "read_file": ("path",),
    "search_text": ("text",),
    "write_file": ("path", "content"),
    "replace_text": ("path", "old", "new"),
    "run_command": ("command",),
    "git_status": (),
    "git_diff": (),
    "project_state": (),
    "project_ui": (),
    "project_command": ("command",),
    "tornado_status": (),
    "tornado_probe": (),
    "finish": (),
}


def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    result = dict(action)
    if result.get("action") == "search_text" and "text" not in result and isinstance(result.get("query"), str):
        result["text"] = result.pop("query")
    if result.get("action") in {"project_state", "project_ui", "tornado_status", "tornado_probe"}:
        return {"action": result["action"]}
    return result


def validate_action(action: dict[str, Any]) -> dict[str, Any]:
    name = action.get("action")
    if not isinstance(name, str):
        raise ActionValidationError("action field is required")
    if name not in ACTION_REQUIRED_FIELDS:
        raise ActionValidationError(f"unknown action: {name}")
    for field in ACTION_REQUIRED_FIELDS[name]:
        value = action.get(field)
        if not isinstance(value, str) or not value:
            raise ActionValidationError(f"{name} requires field '{field}'")
    if name == "project_command" and "arguments" in action and not isinstance(action["arguments"], dict):
        raise ActionValidationError("project_command field 'arguments' must be an object")
    return action


def parse_action(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ActionValidationError("model response must be text")
    raw = raw.strip()
    bare_no_arg_actions = {"list_files", "git_status", "git_diff", "project_state", "project_ui", "tornado_status", "tornado_probe"}
    if raw in bare_no_arg_actions:
        return validate_action({"action": raw})

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActionValidationError("model response is not valid JSON") from exc

    if not isinstance(value, dict):
        raise ActionValidationError("model response must be one JSON object")

    action: dict[str, Any] | None = None
    if isinstance(value.get("action"), str):
        action = value
    else:
        name = value.get("name")
        arguments = value.get("arguments")
        if isinstance(name, str) and isinstance(arguments, dict):
            action = {"action": name, **arguments}
        elif len(value) == 1:
            name, arguments = next(iter(value.items()))
            if isinstance(name, str) and isinstance(arguments, dict):
                action = {"action": name, **arguments}

    if action is None:
        raise ActionValidationError("action field is required")
    return validate_action(_normalize_action(action))


class CommandPolicy:
    SIMPLE_GIT = {"status", "diff", "log", "branch", "rev-parse", "ls-files", "show"}
    _GIT_READ_ONLY_OPTIONS = {
        "status": {"--short", "-s", "--branch", "-b"},
        "diff": {"--cached", "--staged", "--stat", "--name-only", "--name-status", "--check"},
        "log": {"--oneline", "--decorate", "--stat", "--name-only", "--name-status"},
        "branch": {"--all", "-a", "--verbose", "-v", "-vv"},
        "rev-parse": {"--show-toplevel", "--is-inside-work-tree", "--show-prefix", "--git-dir"},
        "ls-files": {"--stage", "-s", "--cached", "--modified", "--deleted", "--others", "--exclude-standard"},
        "show": {"--stat", "--name-only", "--name-status", "--oneline"},
    }

    def __init__(self, approved_batch_files: set[str] | None = None):
        self.approved_batch_files = approved_batch_files or set()

    def classify(self, command: str) -> CommandClass:
        argv = shlex.split(command, posix=os.name != "nt")
        if not argv:
            raise ActionValidationError("empty command")
        head = argv[0].lower()
        if head == "pytest" or (head == "python" and len(argv) >= 3 and argv[1] == "-m" and argv[2] == "pytest"):
            return CommandClass.TEST
        if head == "git":
            return CommandClass.INSPECTION
        if head.endswith(".bat"):
            return CommandClass.LAUNCH
        if head == "python" and len(argv) >= 2 and argv[1].lower().endswith(".py"):
            return CommandClass.LAUNCH
        return CommandClass.OTHER_ALLOWED

    @classmethod
    def _validate_git_inspection(cls, argv: list[str]) -> None:
        """Allow only explicitly supported Git commands that cannot mutate or write output."""
        if len(argv) < 2 or argv[1].lower() not in cls.SIMPLE_GIT:
            raise ActionValidationError("git subcommand not allowed")

        subcommand = argv[1].lower()
        arguments = argv[2:]
        options = cls._GIT_READ_ONLY_OPTIONS[subcommand]

        if subcommand == "rev-parse":
            if arguments == ["--verify", "HEAD"]:
                return
            if all(argument in options for argument in arguments):
                return
            raise ActionValidationError("git arguments not allowed for inspection")

        if subcommand == "show":
            revision_count = 0
            for argument in arguments:
                if argument in options:
                    continue
                if argument.startswith("-"):
                    raise ActionValidationError("git arguments not allowed for inspection")
                revision_count += 1
            if revision_count <= 1:
                return
            raise ActionValidationError("git arguments not allowed for inspection")

        if subcommand == "log":
            for argument in arguments:
                if argument in options or re.fullmatch(r"-\d+", argument):
                    continue
                if argument == "-n":
                    continue
                if argument.isdigit() and "-n" in arguments:
                    continue
                raise ActionValidationError("git arguments not allowed for inspection")
            return

        if all(argument in options for argument in arguments):
            return
        raise ActionValidationError("git arguments not allowed for inspection")

    def validate(self, command: str) -> list[str]:
        argv = shlex.split(command, posix=os.name != "nt")
        if not argv:
            raise ActionValidationError("empty command")

        head = argv[0].lower()
        if head == "git":
            self._validate_git_inspection(argv)
            return argv

        if head == "pytest":
            return argv

        if head == "python":
            if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "pytest":
                return argv
            if len(argv) >= 2 and argv[1].lower().endswith(".py"):
                return argv
            raise ActionValidationError("python invocation not allowed")

        if head.endswith(".bat"):
            name = Path(head).name.lower()
            allowed = {item.lower() for item in self.approved_batch_files}
            if name in allowed:
                return argv
            raise ActionValidationError("batch file not allowed")

        raise ActionValidationError("command not on allowlist")


class ActionExecutor:
    def __init__(
        self,
        workspace: Workspace,
        journal: TaskJournal,
        dry_run: bool,
        command_policy: CommandPolicy | None = None,
        allow_interactive_launch: bool = False,
        project_control=None,
        tornado_client=None,
    ):
        self.workspace = workspace
        self.journal = journal
        self.dry_run = dry_run
        self.command_policy = command_policy or CommandPolicy()
        self.allow_interactive_launch = allow_interactive_launch
        self.project_control = project_control
        self.tornado_client = tornado_client

    def _run_command(self, command: str) -> dict[str, Any]:
        argv = self.command_policy.validate(command)
        command_class = self.command_policy.classify(command)
        if command_class is CommandClass.LAUNCH and not self.allow_interactive_launch:
            raise PermissionError("interactive launch requires explicit task intent")

        if argv[0].lower() == "python" and len(argv) >= 2 and argv[1].lower().endswith(".py"):
            self.workspace.resolve_safe(argv[1])

        if argv[0].lower().endswith(".bat"):
            if self.dry_run:
                raise PermissionError("batch files are disabled in dry-run mode")
            batch_path = self.workspace.resolve_safe(argv[0])
            if not batch_path.is_file():
                raise FileNotFoundError(argv[0])
            if os.name != "nt":
                raise ActionValidationError("approved .bat files can run only on Windows")
            run_argv = ["cmd.exe", "/d", "/s", "/c", str(batch_path), *argv[1:]]
        else:
            run_argv = argv

        completed = subprocess.run(
            run_argv,
            cwd=self.workspace.root,
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
        result = {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-12000:],
            "stderr": completed.stderr[-12000:],
        }
        if "pytest" in command.lower():
            self.journal.tests_run.append({"command": command, **result})
            self.journal.save()
        return result

    def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        name = action["action"]

        if name == "list_files":
            path = action.get("path", ".")
            try:
                return {"ok": True, "files": self.workspace.list_files(path, recursive=action.get("recursive", True))}
            except FileNotFoundError:
                return {
                    "ok": False,
                    "error": f"path does not exist: {path}",
                    "hint": "Use path '.' to inspect the workspace root.",
                }
        if name == "read_file":
            path = action["path"]
            try:
                return {"ok": True, "content": self.workspace.read_file(path)}
            except FileNotFoundError:
                return {
                    "ok": False,
                    "error": "path not found",
                    "path": path,
                    "hint": "Use list_files and choose an existing file path.",
                }
        if name == "search_text":
            path = action.get("path", ".")
            try:
                return {
                    "ok": True,
                    "matches": self.workspace.search_text(action["text"], path),
                }
            except FileNotFoundError:
                return {
                    "ok": False,
                    "error": "path not found",
                    "path": path,
                    "hint": "Use list_files and choose an existing path from that result.",
                }
        if name == "write_file":
            self.workspace.write_file(
                action["path"], action["content"], journal=self.journal, dry_run=self.dry_run
            )
            return {"ok": True}
        if name == "replace_text":
            self.workspace.replace_text(
                action["path"],
                action["old"],
                action["new"],
                journal=self.journal,
                dry_run=self.dry_run,
            )
            return {"ok": True}
        if name == "tornado_status":
            if self.tornado_client is None or not callable(getattr(self.tornado_client, "status_snapshot", None)):
                return {"ok": False, "error": "Tornado diagnostics are not available"}
            return {"ok": True, "tornado": self.tornado_client.status_snapshot()}
        if name == "tornado_probe":
            if self.tornado_client is None or not callable(getattr(self.tornado_client, "probe_eligible_cloud", None)):
                return {"ok": False, "error": "Tornado provider probing is not available"}
            return {"ok": True, "probe": self.tornado_client.probe_eligible_cloud()}
        if name in {"project_state", "project_ui", "project_command"} and self.project_control is None:
            return {"ok": False, "error": "project control API is not configured"}
        if name in {"project_state", "project_ui", "project_command"}:
            try:
                if name == "project_state":
                    return {"ok": True, "state": self.project_control.get_state()}
                if name == "project_ui":
                    return {"ok": True, "ui": self.project_control.get_ui_state()}

                ui = self.project_control.get_ui_state()
                controls = ui.get("controls", []) if isinstance(ui, dict) else []
                available_commands = sorted({
                    item.get("command")
                    for item in controls
                    if isinstance(item, dict) and isinstance(item.get("command"), str) and item.get("command")
                })
                if action["command"] not in available_commands:
                    return {
                        "ok": False,
                        "error": "project command is not advertised by the running application",
                        "command": action["command"],
                        "available_commands": available_commands,
                    }

                result = self.project_control.send_command(
                    action["command"], action.get("arguments", {}),
                )
                if isinstance(result, dict) and "ok" in result:
                    return result
                return {"ok": True, "result": result}
            except ProjectControlError as exc:
                return {"ok": False, "error": str(exc)}
        if name == "run_command":
            return self._run_command(action["command"])
        if name == "git_status":
            return self._run_command("git status --short")
        if name == "git_diff":
            return self._run_command("git diff")
        if name == "finish":
            return {
                "ok": True,
                "summary": action.get("summary", ""),
                "tests": action.get("tests", []),
            }
        raise ActionValidationError(f"unknown action: {name}")
