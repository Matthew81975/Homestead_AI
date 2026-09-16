from pathlib import Path
import inspect

import pytest

from hcs_ai.local_codex.protocol import Event


def make_event(sequence, event_type, level="info", **payload):
    return Event(1, sequence, 1000.0, event_type, level, payload)


def test_format_provider_fallback_uses_provider_tag():
    from hcs_ai.gui_local_codex import format_local_codex_event

    text, tag = format_local_codex_event(make_event(
        4,
        "provider_fallback",
        from_provider="cloud",
        to_provider="local-lm-studio",
    ))

    assert text == "Provider fallback: cloud → local-lm-studio"
    assert tag == "provider"


def test_local_codex_tab_title_and_controls_are_declared():
    from hcs_ai.gui_local_codex import LOCAL_CODEX_TAB_TITLE, REQUIRED_CONTROL_NAMES

    assert LOCAL_CODEX_TAB_TITLE == "Local Codex"
    assert REQUIRED_CONTROL_NAMES == {
        "workspace", "refresh", "manage", "prompt", "new_task", "resume", "stop",
        "approve", "deny", "follow", "pause_scroll", "search", "clear_view", "open_log_folder",
    }


@pytest.mark.parametrize(
    ("snapshot", "enabled"),
    [
        ({"worker_state": "running", "task_state": "idle"}, {"new_task", "resume"}),
        ({"worker_state": "running", "task_state": "working"}, {"stop"}),
        ({"worker_state": "running", "task_state": "idle", "active": True}, {"stop"}),
        ({"worker_state": "running", "task_state": "awaiting_approval", "request_id": "r1"},
         {"stop", "approve", "deny"}),
        ({"worker_state": "stopping", "task_state": "stopping"}, set()),
        ({"worker_state": "crashed", "task_state": "interrupted"}, set()),
        ({"worker_state": "running", "task_state": "completed"}, {"new_task", "resume"}),
    ],
)
def test_control_enablement_tracks_service_state(snapshot, enabled):
    from hcs_ai.gui_local_codex import local_codex_control_states

    states = local_codex_control_states(snapshot)
    for name in {"new_task", "resume", "stop", "approve", "deny"}:
        assert states[name] is (name in enabled)


def test_events_are_rendered_in_sequence_order():
    from hcs_ai.gui_local_codex import ordered_local_codex_events

    events = [make_event(7, "heartbeat"), make_event(5, "task_started"), make_event(6, "log")]
    assert [event.sequence for event in ordered_local_codex_events(events)] == [5, 6, 7]


def test_search_ranges_are_case_insensitive_and_non_overlapping():
    from hcs_ai.gui_local_codex import local_codex_search_ranges

    assert local_codex_search_ranges("Alpha beta ALPHA", "alpha") == [(0, 5), (11, 16)]
    assert local_codex_search_ranges("anything", "") == []


def test_follow_requires_follow_enabled_and_scroll_not_paused():
    from hcs_ai.gui_local_codex import should_follow_local_codex_log

    assert should_follow_local_codex_log(True, False)
    assert not should_follow_local_codex_log(True, True)
    assert not should_follow_local_codex_log(False, False)


def test_clear_view_does_not_delete_persistent_log(tmp_path):
    from hcs_ai.gui_local_codex import LocalCodexGuiMixin

    log = tmp_path / "service.log"
    log.write_text("persistent\n", encoding="utf-8")

    class FakeText:
        def __init__(self):
            self.calls = []

        def configure(self, **kwargs):
            self.calls.append(("configure", kwargs))

        config = configure

        def delete(self, first, last):
            self.calls.append(("delete", first, last))

    view = type("View", (LocalCodexGuiMixin,), {})()
    view.local_codex_log = FakeText()
    view._clear_local_codex_log()

    assert log.read_text(encoding="utf-8") == "persistent\n"
    assert ("delete", "1.0", "end") in view.local_codex_log.calls


def test_gui_layer_never_imports_action_executor():
    module = Path(__file__).parents[1] / "hcs_ai" / "gui_local_codex.py"
    assert "ActionExecutor" not in module.read_text(encoding="utf-8")


def test_every_concrete_gui_layer_accepts_local_codex_service():
    from hcs_ai.gui_home import App as HomeApp
    from hcs_ai.gui_recent import App as RecentApp
    from hcs_ai.gui_diagnostics import App as DiagnosticsApp

    for app_class in (HomeApp, RecentApp, DiagnosticsApp):
        assert "local_codex_service" in inspect.signature(app_class.__init__).parameters
