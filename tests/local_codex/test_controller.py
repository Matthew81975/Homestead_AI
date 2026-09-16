import json
from pathlib import Path

from hcs_ai.local_codex.controller import AgentController, build_system_prompt
from hcs_ai.local_codex.state import AgentStatus, TaskJournal


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages):
        return self.replies.pop(0)


class FakeExecutor:
    def __init__(self, results):
        self.results = list(results)
        self.actions = []

    def execute(self, action):
        self.actions.append(action)
        return self.results.pop(0)


def make_journal(tmp_path: Path) -> TaskJournal:
    return TaskJournal.new("Inspect repo", r"C:\repo", "test-model", tmp_path)


def test_system_prompt_demands_structured_actions():
    prompt = build_system_prompt()
    assert "exactly one JSON object" in prompt
    assert "git commit" in prompt
    assert "git push" in prompt


def test_system_prompt_documents_exact_search_text_schema():
    prompt = build_system_prompt()
    assert '"action":"search_text"' in prompt
    assert '"text":"literal search text"' in prompt
    assert "search_text uses text, not query or pattern" in prompt


def test_three_invalid_responses_block(tmp_path: Path):
    journal = make_journal(tmp_path)
    controller = AgentController(
        client=FakeClient(["bad", "still bad", "bad again"]),
        executor=FakeExecutor([{"ok": True, "files": ["main.py"]}]),
        journal=journal,
        max_failed_actions=3,
    )
    result = controller.run()
    assert result.status is AgentStatus.BLOCKED
    assert journal.consecutive_failures == 3


def test_changed_files_require_tests_and_diff(tmp_path: Path):
    journal = make_journal(tmp_path)
    journal.files_changed.append("main.py")
    journal.record_step({"action": "list_files", "path": "."}, {"ok": True, "files": ["main.py"]})

    client = FakeClient(
        [
            '{"action":"finish","summary":"done","tests":[]}',
            '{"action":"git_diff"}',
            '{"action":"run_command","command":"pytest"}',
            '{"action":"finish","summary":"done","tests":["pytest"]}',
        ]
    )
    executor = FakeExecutor(
        [
            {"ok": True, "summary": "done", "tests": []},
            {"ok": True, "stdout": "diff", "stderr": "", "returncode": 0},
            {"ok": True, "stdout": "1 passed", "stderr": "", "returncode": 0},
            {"ok": True, "summary": "done", "tests": ["pytest"]},
        ]
    )

    result = AgentController(client, executor, journal, 3).run()
    assert result.status is AgentStatus.READY_FOR_APPROVAL
    assert executor.actions[-1]["action"] == "finish"


def test_read_only_task_can_finish_without_tests(tmp_path: Path):
    journal = make_journal(tmp_path)
    journal.record_step({"action": "list_files", "path": "."}, {"ok": True, "files": ["main.py"]})
    result = AgentController(
        FakeClient(['{"action":"finish","summary":"read only","tests":[]}']),
        FakeExecutor([{"ok": True, "summary": "read only", "tests": []}]),
        journal,
        3,
    ).run()
    assert result.status is AgentStatus.READY_FOR_APPROVAL


def test_controller_bootstraps_workspace_root_before_first_model_call(tmp_path: Path):
    journal = make_journal(tmp_path)
    client = FakeClient(['{"action":"finish","summary":"done","tests":[]}'])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py", "maze_world/world.py"]},
        {"ok": True, "summary": "done", "tests": []},
    ])

    result = AgentController(client, executor, journal, 3).run()

    assert executor.actions[0] == {"action": "list_files", "path": ".", "recursive": False}
    assert journal.steps[0]["action"] == {"action": "list_files", "path": ".", "recursive": False}
    assert result.status is AgentStatus.READY_FOR_APPROVAL


def test_controller_reports_progress_events(tmp_path: Path):
    journal = make_journal(tmp_path)
    events = []
    client = FakeClient(['{"action":"finish","summary":"done","tests":[]}'])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "summary": "done", "tests": []},
    ])

    result = AgentController(client, executor, journal, 3, status_callback=events.append).run()

    assert result.status is AgentStatus.READY_FOR_APPROVAL
    assert any("Workspace bootstrap" in event for event in events)
    assert any("Waiting for LM Studio" in event for event in events)
    assert any("Action: finish" in event for event in events)
    assert any("Result: ok" in event for event in events)


def test_lm_studio_error_blocks_cleanly(tmp_path: Path):
    from hcs_ai.local_codex.lm_client import LMStudioError

    class ErrorClient:
        def chat(self, messages):
            raise LMStudioError("timed out")

    journal = make_journal(tmp_path)
    events = []
    executor = FakeExecutor([{"ok": True, "files": ["main.py"]}])

    result = AgentController(
        ErrorClient(), executor, journal, 3, status_callback=events.append
    ).run()

    assert result.status is AgentStatus.BLOCKED
    assert journal.steps[-1]["result"]["error"] == "LM Studio error: timed out"
    assert any("LM Studio error" in event for event in events)


def test_messages_compact_large_tool_results(tmp_path: Path):
    journal = make_journal(tmp_path)
    journal.record_step(
        {"action": "read_file", "path": "big.py"},
        {"ok": True, "content": "x" * 20000},
    )
    controller = AgentController(FakeClient([]), FakeExecutor([]), journal, 3)

    user_message = controller._messages()[1]["content"]

    assert len(user_message) < 9000
    assert "truncated" in user_message


def test_messages_stay_below_local_model_prompt_budget(tmp_path: Path):
    journal = make_journal(tmp_path)
    for index in range(4):
        journal.record_step(
            {"action": "read_file", "path": f"large_{index}.py"},
            {"ok": True, "content": "x" * 20000},
        )
    controller = AgentController(FakeClient([]), FakeExecutor([]), journal, 3)

    user_message = controller._messages()[1]["content"]

    assert len(user_message) < 7000


def test_repeated_successful_file_read_is_not_executed_again(tmp_path: Path):
    journal = make_journal(tmp_path)
    client = FakeClient([
        '{"action":"read_file","path":"main.py"}',
        '{"action":"read_file","path":"main.py"}',
        '{"action":"finish","summary":"done","tests":[]}',
    ])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "content": "print('hello')"},
        {"ok": True, "summary": "done", "tests": []},
    ])

    result = AgentController(client, executor, journal, 3).run()

    reads = [action for action in executor.actions if action["action"] == "read_file"]
    assert reads == [{"action": "read_file", "path": "main.py"}]
    assert result.status is AgentStatus.READY_FOR_APPROVAL


def test_repeated_failed_action_warning_is_added_to_context(tmp_path: Path):
    journal = make_journal(tmp_path)

    class RecordingClient:
        def __init__(self):
            self.messages_seen = []
            self.replies = [
                '{"action":"search_text","path":"maze_world/rendering.py","text":"platform"}',
                '{"action":"search_text","path":"maze_world/rendering.py","text":"platform"}',
                '{"action":"finish","summary":"done","tests":[]}',
            ]

        def chat(self, messages):
            self.messages_seen.append(messages)
            return self.replies.pop(0)

    class RecoverableExecutor:
        def execute(self, action):
            if action["action"] == "list_files":
                return {"ok": True, "files": ["maze_world/model.py"]}
            if action["action"] == "finish":
                return {"ok": True, "summary": "done", "tests": []}
            return {
                "ok": False,
                "error": "path not found",
                "path": action["path"],
                "hint": "Use list_files and choose an existing path.",
            }

    client = RecordingClient()
    result = AgentController(
        client=client,
        executor=RecoverableExecutor(),
        journal=journal,
        max_failed_actions=3,
    ).run()

    third_context = json.loads(client.messages_seen[2][-1]["content"])
    assert third_context["repeated_failed_action"] is not None
    assert "Do not retry it unchanged" in third_context["repeated_failed_action"]["hint"]
    assert result.status is AgentStatus.READY_FOR_APPROVAL


def test_docs_only_change_requires_diff_but_not_tests(tmp_path: Path):
    journal = make_journal(tmp_path)
    journal.files_changed.append("README.md")

    client = FakeClient([
        '{"action":"git_diff"}',
        '{"action":"finish","summary":"Updated README.","tests":[]}',
    ])
    executor = FakeExecutor([
        {"ok": True, "files": ["README.md"]},
        {"ok": True, "stdout": "diff --git a/README.md b/README.md", "stderr": "", "returncode": 0},
        {"ok": True, "summary": "Updated README.", "tests": []},
    ])

    result = AgentController(client, executor, journal, 3).run()

    assert result.status is AgentStatus.READY_FOR_APPROVAL
    assert [a["action"] for a in executor.actions] == ["list_files", "git_diff", "finish"]


def test_python_change_still_requires_passing_tests(tmp_path: Path):
    journal = make_journal(tmp_path)
    journal.files_changed.append("main.py")

    client = FakeClient([
        '{"action":"git_diff"}',
        '{"action":"finish","summary":"Updated code.","tests":[]}',
        '{"action":"run_command","command":"pytest"}',
        '{"action":"finish","summary":"Updated code.","tests":["pytest"]}',
    ])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "stdout": "diff --git a/main.py b/main.py", "stderr": "", "returncode": 0},
        {"ok": True, "summary": "Updated code.", "tests": []},
        {"ok": True, "stdout": "1 passed", "stderr": "", "returncode": 0},
        {"ok": True, "summary": "Updated code.", "tests": ["pytest"]},
    ])

    result = AgentController(client, executor, journal, 3).run()

    assert result.status is AgentStatus.READY_FOR_APPROVAL
    assert any(
        a["action"] == "run_command" and a.get("command") == "pytest"
        for a in executor.actions
    )


def test_pause_stops_before_next_model_action(tmp_path: Path):
    class CountingClient:
        def __init__(self):
            self.calls = 0
        def chat(self, messages):
            self.calls += 1
            return '{"action":"finish","summary":"done","tests":[]}'

    journal = make_journal(tmp_path)
    client = CountingClient()
    executor = FakeExecutor([{"ok": True, "files": ["main.py"]}])
    controller = AgentController(client, executor, journal, 3)
    controller.set_pause_requested(True)

    result = controller.run()

    assert result.status is AgentStatus.PAUSED
    assert client.calls == 0


def test_live_instruction_enters_next_model_context(tmp_path: Path):
    journal = make_journal(tmp_path)
    controller = AgentController(FakeClient([]), FakeExecutor([]), journal, 3)
    controller.add_instruction("Do not modify tests.")

    context = json.loads(controller._messages()[-1]["content"])

    assert context["pending_instructions"] == ["Do not modify tests."]


def test_run_one_step_performs_only_one_model_action(tmp_path: Path):
    class CountingClient:
        def __init__(self):
            self.calls = 0
        def chat(self, messages):
            self.calls += 1
            return '{"action":"git_status"}'

    journal = make_journal(tmp_path)
    client = CountingClient()
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "stdout": "", "stderr": "", "returncode": 0},
    ])
    controller = AgentController(client, executor, journal, 3)

    result = controller.run_one_step()

    assert result.status is AgentStatus.WORKING
    assert client.calls == 1
    assert executor.actions[-1]["action"] == "git_status"


def test_system_prompt_advertises_project_control_actions():
    prompt = build_system_prompt()
    assert "project_state" in prompt
    assert "project_ui" in prompt
    assert "project_command" in prompt


def test_controller_reports_model_action_outcome_when_client_supports_feedback(tmp_path: Path):
    class FeedbackClient(FakeClient):
        def __init__(self, replies):
            super().__init__(replies)
            self.outcomes = []

        def report_outcome(self, success, *, signal):
            self.outcomes.append((success, signal))

    journal = make_journal(tmp_path)
    client = FeedbackClient(['{"action":"git_status"}', '{"action":"finish","summary":"done","tests":[]}'])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": False, "error": "git unavailable"},
        {"ok": True, "summary": "done", "tests": []},
    ])

    result = AgentController(client, executor, journal, 3).run()

    assert result.status is AgentStatus.READY_FOR_APPROVAL
    assert client.outcomes[0] == (False, "action_failed")
    assert client.outcomes[-1] == (True, "action_succeeded")


def test_controller_reports_rejected_finish_as_negative_model_outcome(tmp_path: Path):
    class FeedbackClient(FakeClient):
        def __init__(self, replies):
            super().__init__(replies)
            self.outcomes = []

        def report_outcome(self, success, *, signal):
            self.outcomes.append((success, signal))

    journal = make_journal(tmp_path)
    journal.files_changed.append("main.py")
    journal.record_step({"action": "list_files", "path": "."}, {"ok": True, "files": ["main.py"]})
    client = FeedbackClient([
        '{"action":"finish","summary":"too early","tests":[]}',
        '{"action":"git_diff"}',
        '{"action":"run_command","command":"pytest"}',
        '{"action":"finish","summary":"done","tests":["pytest"]}',
    ])
    executor = FakeExecutor([
        {"ok": True, "summary": "too early", "tests": []},
        {"ok": True, "stdout": "diff", "stderr": "", "returncode": 0},
        {"ok": True, "stdout": "1 passed", "stderr": "", "returncode": 0},
        {"ok": True, "summary": "done", "tests": ["pytest"]},
    ])

    result = AgentController(client, executor, journal, 3).run()

    assert result.status is AgentStatus.READY_FOR_APPROVAL
    assert client.outcomes[0] == (False, "finish_rejected")



def test_controller_hides_project_control_actions_when_unavailable(tmp_path: Path):
    journal = make_journal(tmp_path)
    executor = FakeExecutor([{"ok": True, "files": ["main.py"]}])
    executor.project_control = None
    controller = AgentController(FakeClient([]), executor, journal, 3)

    prompt = controller._messages()[0]["content"]

    assert "project_state" not in prompt
    assert "project_ui" not in prompt
    assert "project_command" not in prompt


def test_controller_advertises_project_control_actions_when_available(tmp_path: Path):
    journal = make_journal(tmp_path)
    executor = FakeExecutor([{"ok": True, "files": ["main.py"]}])
    executor.project_control = object()
    controller = AgentController(FakeClient([]), executor, journal, 3)

    prompt = controller._messages()[0]["content"]

    assert '"action":"project_state"' in prompt
    assert '"action":"project_ui"' in prompt
    assert '"action":"project_command"' in prompt


def test_system_prompt_distinguishes_project_observation_from_commands():
    prompt = build_system_prompt(include_project_control=True)

    assert "project_state and project_ui are read-only observation actions" in prompt
    assert "project_command is only for changing or interacting with the running application" in prompt
    assert "Only use project_command names advertised by project_ui" in prompt
    assert "For read-only inspection tasks, do not use project_command" in prompt


def test_repeated_successful_project_state_is_not_executed_again(tmp_path: Path):
    journal = make_journal(tmp_path)
    client = FakeClient([
        '{"action":"project_state"}',
        '{"action":"project_state"}',
        '{"action":"finish","summary":"done","tests":[]}',
    ])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "state": {"running": True}},
        {"ok": True, "summary": "done", "tests": []},
    ])
    executor.project_control = object()

    result = AgentController(client, executor, journal, 3).run()

    project_reads = [action for action in executor.actions if action["action"] == "project_state"]
    assert project_reads == [{"action": "project_state"}]
    assert result.status is AgentStatus.READY_FOR_APPROVAL


def test_repeated_successful_project_ui_is_not_executed_again(tmp_path: Path):
    journal = make_journal(tmp_path)
    client = FakeClient([
        '{"action":"project_ui"}',
        '{"action":"project_ui"}',
        '{"action":"finish","summary":"done","tests":[]}',
    ])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "ui": {"title": "Maze World"}},
        {"ok": True, "summary": "done", "tests": []},
    ])
    executor.project_control = object()

    result = AgentController(client, executor, journal, 3).run()

    project_reads = [action for action in executor.actions if action["action"] == "project_ui"]
    assert project_reads == [{"action": "project_ui"}]
    assert result.status is AgentStatus.READY_FOR_APPROVAL


def test_project_observation_cannot_be_reread_without_interaction(tmp_path: Path):
    journal = make_journal(tmp_path)
    client = FakeClient([
        '{"action":"project_state"}',
        '{"action":"project_ui"}',
        '{"action":"project_state"}',
        '{"action":"finish","summary":"done","tests":[]}',
    ])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "state": {"running": True}},
        {"ok": True, "ui": {"title": "Maze World"}},
        {"ok": True, "summary": "done", "tests": []},
    ])
    executor.project_control = object()

    result = AgentController(client, executor, journal, 3).run()

    state_reads = [action for action in executor.actions if action["action"] == "project_state"]
    ui_reads = [action for action in executor.actions if action["action"] == "project_ui"]
    assert state_reads == [{"action": "project_state"}]
    assert ui_reads == [{"action": "project_ui"}]
    assert result.status is AgentStatus.READY_FOR_APPROVAL


def test_successful_project_command_allows_fresh_project_state(tmp_path: Path):
    journal = make_journal(tmp_path)
    client = FakeClient([
        '{"action":"project_state"}',
        '{"action":"project_command","command":"turn","arguments":{"yaw_delta":5}}',
        '{"action":"project_state"}',
        '{"action":"finish","summary":"done","tests":[]}',
    ])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "state": {"yaw": 90}},
        {"ok": True, "status": "queued"},
        {"ok": True, "state": {"yaw": 95}},
        {"ok": True, "summary": "done", "tests": []},
    ])
    executor.project_control = object()

    result = AgentController(client, executor, journal, 3).run()

    state_reads = [action for action in executor.actions if action["action"] == "project_state"]
    assert state_reads == [{"action": "project_state"}, {"action": "project_state"}]
    assert result.status is AgentStatus.READY_FOR_APPROVAL


def test_project_observations_remain_in_context_after_recent_steps_eviction(tmp_path: Path):
    journal = make_journal(tmp_path)
    journal.record_step(
        {"action": "project_state"},
        {"ok": True, "state": {"current_room_id": "room_0001", "player": {"yaw": 115.08}}},
    )
    journal.record_step(
        {"action": "project_ui"},
        {
            "ok": True,
            "ui": {
                "title": "Maze World",
                "controls": [
                    {"command": "turn", "arguments": {"yaw_delta": "number"}},
                    {"command": "quit", "arguments": {}},
                ],
            },
        },
    )
    for index in range(4):
        journal.record_step(
            {"action": "search_text", "text": f"bad-{index}", "path": "."},
            {"ok": False, "error": "not useful"},
        )

    executor = FakeExecutor([])
    executor.project_control = object()
    controller = AgentController(FakeClient([]), executor, journal, 3)

    context = json.loads(controller._messages()[1]["content"])

    assert all(
        step["action"]["action"] not in {"project_state", "project_ui"}
        for step in context["recent_steps"]
    )
    assert context["project_observations"]["project_state"]["current_room_id"] == "room_0001"
    assert context["project_observations"]["project_ui"]["title"] == "Maze World"
    assert context["advertised_project_commands"] == ["quit", "turn"]
    assert context["observation_complete"]["complete"] is True
    assert "finish" in context["observation_complete"]["hint"].lower()


def test_successful_project_command_invalidates_persistent_project_observations(tmp_path: Path):
    journal = make_journal(tmp_path)
    journal.record_step(
        {"action": "project_state"},
        {"ok": True, "state": {"player": {"yaw": 90}}},
    )
    journal.record_step(
        {"action": "project_ui"},
        {"ok": True, "ui": {"controls": [{"command": "turn", "arguments": {}}]}},
    )
    journal.record_step(
        {"action": "project_command", "command": "turn", "arguments": {"yaw_delta": 5}},
        {"ok": True, "status": "queued"},
    )

    executor = FakeExecutor([])
    executor.project_control = object()
    controller = AgentController(FakeClient([]), executor, journal, 3)

    context = json.loads(controller._messages()[1]["content"])

    assert context["project_observations"] == {}
    assert context["advertised_project_commands"] == []
    assert context["observation_complete"]["complete"] is False


def test_project_repeat_guard_points_model_to_retained_observations(tmp_path: Path):
    journal = make_journal(tmp_path)

    class RecordingClient:
        def __init__(self):
            self.messages_seen = []
            self.replies = [
                '{"action":"project_state"}',
                '{"action":"project_state"}',
                '{"action":"finish","summary":"done","tests":[]}',
            ]

        def chat(self, messages):
            self.messages_seen.append(messages)
            return self.replies.pop(0)

    client = RecordingClient()
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "state": {"running": True}},
        {"ok": True, "summary": "done", "tests": []},
    ])
    executor.project_control = object()

    result = AgentController(client, executor, journal, 3).run()

    third_context = json.loads(client.messages_seen[2][-1]["content"])
    assert result.status is AgentStatus.READY_FOR_APPROVAL
    assert "project_observations" in third_context["repeated_failed_action"]["hint"]


def test_system_prompt_explains_retained_project_observation_context():
    prompt = build_system_prompt(include_project_control=True)
    assert "project_observations" in prompt
    assert "observation_complete" in prompt


def test_project_observation_context_is_omitted_when_project_control_unavailable(tmp_path: Path):
    journal = make_journal(tmp_path)
    executor = FakeExecutor([])
    executor.project_control = None
    controller = AgentController(FakeClient([]), executor, journal, 3)

    context = json.loads(controller._messages()[1]["content"])

    assert "project_observations" not in context
    assert "advertised_project_commands" not in context
    assert "observation_complete" not in context


def test_system_prompt_advertises_tornado_status_when_available():
    prompt = build_system_prompt(include_tornado_status=True)
    assert '{"action":"tornado_status"}' in prompt
    assert "Use tornado_status for Tornado provider diagnostics" in prompt
    assert "never use project_command for Tornado diagnostics" in prompt


def test_messages_include_tornado_status_only_when_executor_supports_it(tmp_path: Path):
    journal = make_journal(tmp_path)

    class ExecutorWithTornado(FakeExecutor):
        def __init__(self):
            super().__init__([])
            self.tornado_client = object()
            self.project_control = None

    controller = AgentController(FakeClient([]), ExecutorWithTornado(), journal, 3)
    prompt = controller._messages()[0]["content"]
    assert '{"action":"tornado_status"}' in prompt


def test_controller_blocks_cleanly_after_repeated_null_model_replies(tmp_path: Path):
    journal = make_journal(tmp_path)
    controller = AgentController(
        client=FakeClient([None, None, None]),
        executor=FakeExecutor([{"ok": True, "files": ["main.py"]}]),
        journal=journal,
        max_failed_actions=3,
    )

    result = controller.run()

    assert result.status is AgentStatus.BLOCKED
    assert journal.consecutive_failures == 3
    errors = [
        step["result"].get("error", "")
        for step in journal.steps
        if step["action"].get("action") == "controller_error"
    ]
    assert errors[-3:] == ["model response must be text"] * 3


def _sample_tornado_status_for_context():
    return {
        "now": 1234.5,
        "provider_count": 2,
        "eligible_count": 1,
        "providers": [
            {
                "provider": "provider-a",
                "model": "model-a",
                "eligible": True,
                "health_state": "ready",
                "api_key_present": True,
                "required_envs_present": True,
                "retry_in_seconds": 0.0,
                "configured_enabled": False,
                "auto_enable_if_key_present": True,
                "api_key_env": "SECRET_ENV_NAME",
                "session_calls": 7,
                "last_error": "very verbose error that should stay only in the journal",
                "learned_budget_recovery_seconds": 3600.0,
            },
            {
                "provider": "provider-b",
                "model": "model-b",
                "eligible": False,
                "health_state": "waiting_retry",
                "api_key_present": True,
                "required_envs_present": False,
                "retry_in_seconds": 42.0,
                "configured_enabled": False,
                "auto_enable_if_key_present": True,
                "api_key_env": "OTHER_SECRET_ENV_NAME",
                "session_calls": 3,
                "last_error": "another verbose error",
                "learned_budget_recovery_seconds": None,
            },
        ],
    }


def test_tornado_status_context_is_compact_but_journal_keeps_full_result(tmp_path: Path):
    journal = make_journal(tmp_path)
    full_status = _sample_tornado_status_for_context()
    journal.record_step(
        {"action": "tornado_status"},
        {"ok": True, "tornado": full_status},
    )

    executor = FakeExecutor([])
    executor.tornado_client = object()
    executor.project_control = None
    controller = AgentController(FakeClient([]), executor, journal, 3)

    context = json.loads(controller._messages()[1]["content"])
    compact = context["tornado_status"]

    assert compact["provider_count"] == 2
    assert compact["eligible_count"] == 1
    assert compact["providers"] == [
        {
            "provider": "provider-a",
            "model": "model-a",
            "eligible": True,
            "health_state": "ready",
            "credentials_present": True,
            "retry_in_seconds": 0.0,
        },
        {
            "provider": "provider-b",
            "model": "model-b",
            "eligible": False,
            "health_state": "waiting_retry",
            "credentials_present": False,
            "retry_in_seconds": 42.0,
        },
    ]
    assert "last_error" not in json.dumps(compact)
    assert "api_key_env" not in json.dumps(compact)
    assert journal.steps[-1]["result"]["tornado"]["providers"][0]["last_error"].startswith("very verbose")


def test_recent_steps_do_not_duplicate_full_tornado_status_payload(tmp_path: Path):
    journal = make_journal(tmp_path)
    journal.record_step(
        {"action": "tornado_status"},
        {"ok": True, "tornado": _sample_tornado_status_for_context()},
    )

    executor = FakeExecutor([])
    executor.tornado_client = object()
    executor.project_control = None
    controller = AgentController(FakeClient([]), executor, journal, 3)
    context = json.loads(controller._messages()[1]["content"])

    step = context["recent_steps"][-1]
    assert step["action"] == {"action": "tornado_status"}
    assert step["result"] == {
        "ok": True,
        "tornado": {"provider_count": 2, "eligible_count": 1},
    }


def test_tornado_status_context_explicitly_directs_reporting_task_to_finish(tmp_path: Path):
    journal = make_journal(tmp_path)
    journal.record_step(
        {"action": "tornado_status"},
        {"ok": True, "tornado": _sample_tornado_status_for_context()},
    )

    executor = FakeExecutor([])
    executor.tornado_client = object()
    executor.project_control = None
    controller = AgentController(FakeClient([]), executor, journal, 3)
    context = json.loads(controller._messages()[1]["content"])

    completion = context["tornado_status_complete"]
    assert completion["complete"] is True
    assert "finish" in completion["hint"].lower()
    assert "do not call tornado_status again" in completion["hint"].lower()


def test_tornado_status_reporting_task_completes_deterministically_without_second_model_turn(tmp_path: Path):
    journal = TaskJournal.new(
        "Use tornado_status to report every Tornado provider and model lane and finish after reporting the status.",
        r"C:\repo",
        "test-model",
        tmp_path,
    )
    status = {
        "provider_count": 2,
        "eligible_count": 1,
        "providers": [
            {
                "provider": "cloud-a",
                "model": "model-a",
                "api_key_present": True,
                "required_envs_present": True,
                "eligible": True,
                "health_state": "ready",
                "last_success_at": 123.0,
                "last_failure_at": None,
                "last_failure_category": None,
                "cooldown_in_seconds": 0.0,
                "retry_in_seconds": 0.0,
                "learned_budget_recovery_seconds": None,
                "learned_budget_recovery_confidence": 0.0,
            },
            {
                "provider": "cloud-b",
                "model": "model-b",
                "api_key_present": False,
                "required_envs_present": True,
                "eligible": False,
                "health_state": "disabled",
                "last_success_at": None,
                "last_failure_at": 100.0,
                "last_failure_category": "auth",
                "cooldown_in_seconds": 0.0,
                "retry_in_seconds": 0.0,
                "learned_budget_recovery_seconds": None,
                "learned_budget_recovery_confidence": 0.0,
            },
        ],
    }
    client = FakeClient(['{"action":"tornado_status"}'])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {"ok": True, "tornado": status},
    ])
    executor.tornado_client = object()
    executor.project_control = None

    result = AgentController(client, executor, journal, 3).run()

    assert result.status is AgentStatus.READY_FOR_APPROVAL
    assert "Tornado status: 1/2 eligible" in result.summary
    assert "cloud-a" in result.summary
    assert "credentials=present" in result.summary
    assert "cloud-b" in result.summary
    assert "credentials=missing" in result.summary
    assert journal.steps[-1]["action"] == {"action": "tornado_status"}
    assert journal.consecutive_failures == 0


def test_tornado_probe_reporting_task_completes_deterministically(tmp_path: Path):
    journal = TaskJournal.new(
        "Use tornado_probe to test every eligible Tornado cloud lane and report the results.",
        r"C:\repo",
        "test-model",
        tmp_path,
    )
    client = FakeClient(['{"action":"tornado_probe"}'])
    executor = FakeExecutor([
        {"ok": True, "files": ["main.py"]},
        {
            "ok": True,
            "probe": {
                "eligible_cloud_count": 2,
                "attempted_count": 2,
                "success_count": 1,
                "failure_count": 1,
                "providers": [
                    {"provider": "a", "model": "m1", "ok": True, "latency_seconds": 0.1},
                    {"provider": "b", "model": "m2", "ok": False, "latency_seconds": 0.2, "category": "auth", "error": "HTTP 401"},
                ],
            },
        },
    ])
    executor.tornado_client = object()
    executor.project_control = None

    result = AgentController(client, executor, journal, 3).run()

    assert result.status is AgentStatus.READY_FOR_APPROVAL
    assert "Tornado probe: 1/2 succeeded" in result.summary
    assert "a" in result.summary
    assert "b" in result.summary
    assert "HTTP 401" in result.summary
