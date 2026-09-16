from pathlib import Path
import subprocess

import pytest

from hcs_ai.local_codex.actions import ActionExecutor, ActionValidationError, CommandPolicy, parse_action
from hcs_ai.local_codex.state import TaskJournal
from hcs_ai.local_codex.workspace import Workspace


def test_parse_action_requires_json():
    assert parse_action('{"action":"git_status"}') == {"action": "git_status"}
    with pytest.raises(ActionValidationError):
        parse_action("inspect git status")




def test_parse_action_rejects_search_text_without_text_with_clear_error():
    with pytest.raises(ActionValidationError, match="search_text requires field 'text'"):
        parse_action('{"action":"search_text","path":"."}')


def test_parse_action_normalizes_search_text_query_alias():
    assert parse_action('{"action":"search_text","path":".","query":"Tornado"}') == {
        "action": "search_text",
        "path": ".",
        "text": "Tornado",
    }


def test_parse_action_rejects_read_file_without_path_with_clear_error():
    with pytest.raises(ActionValidationError, match="read_file requires field 'path'"):
        parse_action('{"action":"read_file"}')

@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        "git log -1",
        "git branch",
        "git rev-parse --show-toplevel",
        "git ls-files",
        "git show HEAD",
        "pytest",
        "pytest tests/test_maze.py -v",
        "python -m pytest",
    ],
)
def test_allowlist_accepts_safe_commands(command: str):
    assert CommandPolicy().validate(command)


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git commit -m nope",
        "git reset --hard",
        "git clean -fd",
        "powershell Get-ChildItem",
        "cmd /c dir",
        "pip install requests",
        "python -c \"import os; os.remove('x')\"",
    ],
)
def test_allowlist_rejects_unsafe_commands(command: str):
    with pytest.raises(ActionValidationError):
        CommandPolicy().validate(command)


@pytest.mark.parametrize("command", [
    "git branch -D disposable", "git branch -d disposable", "git branch disposable",
    "git branch -m renamed", "git branch --edit-description", "git branch --set-upstream-to=other",
    "git diff --output=../outside.patch", "git diff --output ../outside.patch",
    "git log --output=../outside.patch", "git show --output=../outside.patch",
    "git diff --no-index ../outside.txt file.txt", "git diff --ext-diff", "git show --textconv",
    "git ls-files --with-tree=HEAD", "git rev-parse --resolve-git-dir ../outside",
])
def test_git_inspection_rejects_unsupported_and_mutating_arguments(command):
    with pytest.raises(ActionValidationError):
        CommandPolicy().validate(command)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("command", ["git branch -D disposable", "git diff --output=../outside.patch"])
def test_git_inspection_cannot_delete_branches_or_write_outside_workspace(tmp_path, dry_run, command):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout

    git("init")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "initial")
    git("branch", "disposable")
    journal = TaskJournal.new("Inspect git", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(Workspace(root), journal, dry_run=dry_run)

    with pytest.raises(ActionValidationError):
        executor.execute({"action": "run_command", "command": command})
    assert "disposable" in git("branch", "--list")
    assert not (tmp_path / "outside.patch").exists()


@pytest.mark.parametrize("command", [
    "git status --short", "git diff --stat", "git diff --cached --name-only",
    "git log --oneline -5", "git log -n 2", "git branch --all --verbose",
    "git rev-parse --verify HEAD", "git ls-files --stage", "git show --stat HEAD",
])
def test_git_inspection_supports_read_only_argument_forms(command):
    assert CommandPolicy().validate(command)


def test_executor_reads_file(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("hello", encoding="utf-8")
    journal = TaskJournal.new("read", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(Workspace(root), journal, dry_run=False)

    result = executor.execute({"action": "read_file", "path": "main.py"})

    assert result == {"ok": True, "content": "hello"}


def test_dry_run_blocks_approved_batch_file(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "run_maze.bat").write_text("@echo off", encoding="utf-8")
    journal = TaskJournal.new("batch", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(
        Workspace(root), journal, dry_run=True, command_policy=CommandPolicy({"run_maze.bat"})
    )
    with pytest.raises(PermissionError):
        executor.execute({"action": "run_command", "command": "run_maze.bat"})


def test_parse_action_accepts_bare_safe_no_arg_action():
    assert parse_action("list_files") == {"action": "list_files"}


def test_parse_action_normalizes_single_key_action_object():
    assert parse_action('{"read_file":{"path":"main.py"}}') == {
        "action": "read_file",
        "path": "main.py",
    }


def test_parse_action_normalizes_name_arguments_shape():
    assert parse_action('{"name":"read_file","arguments":{"path":"main.py"}}') == {
        "action": "read_file",
        "path": "main.py",
    }


def test_parse_action_rejects_bare_action_that_needs_arguments():
    with pytest.raises(ActionValidationError):
        parse_action("read_file")


def test_list_files_missing_path_returns_recoverable_result(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("read", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(Workspace(root), journal, dry_run=True)

    result = executor.execute({"action": "list_files", "path": "src"})

    assert result["ok"] is False
    assert "does not exist" in result["error"]
    assert result["hint"] == "Use path '.' to inspect the workspace root."


def test_executor_passes_recursive_flag_to_workspace(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("hello", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("x = 1", encoding="utf-8")
    journal = TaskJournal.new("list", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(Workspace(root), journal, dry_run=False)

    result = executor.execute({"action": "list_files", "path": ".", "recursive": False})

    assert result == {"ok": True, "files": ["main.py", "pkg/"]}


def test_missing_search_path_returns_structured_tool_error(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("search", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(Workspace(root), journal, dry_run=False)

    result = executor.execute({
        "action": "search_text",
        "path": "maze_world/rendering.py",
        "text": "platform",
    })

    assert result["ok"] is False
    assert result["error"] == "path not found"
    assert result["path"] == "maze_world/rendering.py"
    assert "list_files" in result["hint"]


def test_missing_read_path_returns_structured_tool_error(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("read", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(Workspace(root), journal, dry_run=False)

    result = executor.execute({
        "action": "read_file",
        "path": "maze_world/rendering.py",
    })

    assert result["ok"] is False
    assert result["error"] == "path not found"
    assert result["path"] == "maze_world/rendering.py"

from hcs_ai.local_codex.actions import CommandClass


def test_run_maze_is_classified_as_launch():
    policy = CommandPolicy({"run_maze.bat"})
    assert policy.classify("run_maze.bat") is CommandClass.LAUNCH


def test_python_script_is_classified_as_launch():
    assert CommandPolicy().classify("python main.py") is CommandClass.LAUNCH


def test_pytest_is_classified_as_test():
    assert CommandPolicy().classify("python -m pytest") is CommandClass.TEST


def test_launch_blocked_without_explicit_intent(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "run_maze.bat").write_text("@echo off", encoding="utf-8")
    journal = TaskJournal.new("edit docs", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(
        Workspace(root),
        journal,
        dry_run=False,
        command_policy=CommandPolicy({"run_maze.bat"}),
        allow_interactive_launch=False,
    )
    with pytest.raises(PermissionError, match="explicit task intent"):
        executor.execute({"action": "run_command", "command": "run_maze.bat"})

from hcs_ai.local_codex.actions import detect_interactive_launch_intent


@pytest.mark.parametrize("text", [
    "Run Maze World and verify the window opens.",
    "Launch the application after the change.",
    "Start the program and inspect the result.",
])
def test_detects_explicit_interactive_launch_intent(text):
    assert detect_interactive_launch_intent(text) is True


@pytest.mark.parametrize("text", [
    "Run the tests and inspect git diff.",
    "Do not run the program.",
    "Fix the launch button code without opening the app.",
])
def test_does_not_infer_launch_intent_from_unrelated_text(text):
    assert detect_interactive_launch_intent(text) is False


class FakeProjectControl:
    def __init__(self):
        self.commands = []

    def get_state(self):
        return {"running": True, "room": 3}

    def get_ui_state(self):
        return {"focused": "resume", "controls": [{"id": "resume", "command": "click_control"}]}

    def send_command(self, command, arguments):
        self.commands.append((command, arguments))
        return {"ok": True, "result": {"accepted": command}}


def test_project_state_action_reads_structured_runtime_state(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("observe", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(Workspace(root), journal, dry_run=False, project_control=FakeProjectControl())

    result = executor.execute({"action": "project_state"})

    assert result == {"ok": True, "state": {"running": True, "room": 3}}


def test_project_ui_action_reads_structured_ui_state(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("observe", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(Workspace(root), journal, dry_run=False, project_control=FakeProjectControl())

    result = executor.execute({"action": "project_ui"})

    assert result["ok"] is True
    assert result["ui"]["focused"] == "resume"


def test_project_command_uses_deterministic_control_api(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("interact", str(root), "test", tmp_path / "logs")
    control = FakeProjectControl()
    executor = ActionExecutor(Workspace(root), journal, dry_run=False, project_control=control)

    result = executor.execute({
        "action": "project_command",
        "command": "click_control",
        "arguments": {"control_id": "resume"},
    })

    assert result == {"ok": True, "result": {"accepted": "click_control"}}
    assert control.commands == [("click_control", {"control_id": "resume"})]


def test_project_actions_fail_recoverably_when_api_not_configured(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("observe", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(Workspace(root), journal, dry_run=False)

    result = executor.execute({"action": "project_state"})

    assert result == {"ok": False, "error": "project control API is not configured"}


class RaisingProjectControl:
    def get_state(self):
        from hcs_ai.local_codex.project_control import ProjectControlError
        raise ProjectControlError("project control request failed: offline")

    def get_ui_state(self):
        from hcs_ai.local_codex.project_control import ProjectControlError
        raise ProjectControlError("project control request failed: offline")

    def send_command(self, command, arguments):
        from hcs_ai.local_codex.project_control import ProjectControlError
        raise ProjectControlError("project control request failed: offline")


@pytest.mark.parametrize(
    "action",
    [
        {"action": "project_state"},
        {"action": "project_ui"},
        {"action": "project_command", "command": "turn", "arguments": {"yaw_delta": 5}},
    ],
)
def test_project_control_connection_failure_is_recoverable(tmp_path: Path, action):
    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("observe", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(
        Workspace(root), journal, dry_run=False, project_control=RaisingProjectControl()
    )

    result = executor.execute(action)

    assert result == {
        "ok": False,
        "error": "project control request failed: offline",
    }


def test_project_command_rejects_unadvertised_command(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("interact", str(root), "test", tmp_path / "logs")

    class AdvertisedControl(FakeProjectControl):
        def get_ui_state(self):
            return {
                "controls": [
                    {"command": "turn", "arguments": {"yaw_delta": "number"}},
                    {"command": "quit", "arguments": {}},
                ]
            }

    control = AdvertisedControl()
    executor = ActionExecutor(Workspace(root), journal, dry_run=False, project_control=control)

    result = executor.execute({
        "action": "project_command",
        "command": "inspect_world_state",
        "arguments": {},
    })

    assert result == {
        "ok": False,
        "error": "project command is not advertised by the running application",
        "command": "inspect_world_state",
        "available_commands": ["quit", "turn"],
    }
    assert control.commands == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"action":"project_state","path":"."}', {"action": "project_state"}),
        ('{"action":"project_ui","path":"/ui/main.py"}', {"action": "project_ui"}),
        ('{"action":"project_state","command":"inspect_state"}', {"action": "project_state"}),
        ('{"action":"project_ui","text":"anything"}', {"action": "project_ui"}),
    ],
)
def test_project_observation_actions_ignore_extraneous_fields_from_small_model(raw, expected):
    assert parse_action(raw) == expected



def test_tornado_status_action_is_read_only_and_returns_diagnostics(tmp_path: Path):
    class FakeTornado:
        def status_snapshot(self):
            return {"provider_count": 1, "providers": [{"provider": "local"}]}

    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("Inspect Tornado", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(
        Workspace(root), journal, dry_run=False, tornado_client=FakeTornado()
    )

    result = executor.execute({"action": "tornado_status"})

    assert result == {
        "ok": True,
        "tornado": {"provider_count": 1, "providers": [{"provider": "local"}]},
    }


def test_parse_tornado_status_discards_irrelevant_small_model_fields():
    action = parse_action('{"action":"tornado_status","path":"."}')
    assert action == {"action": "tornado_status"}


def test_parse_action_rejects_none_as_normal_validation_error():
    with pytest.raises(ActionValidationError, match="model response must be text"):
        parse_action(None)  # type: ignore[arg-type]


def test_tornado_probe_action_calls_tornado_diagnostics_probe(tmp_path: Path):
    class FakeTornado:
        def probe_eligible_cloud(self):
            return {"eligible_cloud_count": 1, "success_count": 1, "providers": [{"provider": "cloud", "ok": True}]}

    root = tmp_path / "repo"
    root.mkdir()
    journal = TaskJournal.new("Probe Tornado", str(root), "test", tmp_path / "logs")
    executor = ActionExecutor(
        Workspace(root), journal, dry_run=False, tornado_client=FakeTornado()
    )

    result = executor.execute({"action": "tornado_probe"})

    assert result == {
        "ok": True,
        "probe": {"eligible_cloud_count": 1, "success_count": 1, "providers": [{"provider": "cloud", "ok": True}]},
    }


def test_parse_tornado_probe_discards_irrelevant_small_model_fields():
    action = parse_action('{"action":"tornado_probe","path":"."}')
    assert action == {"action": "tornado_probe"}
