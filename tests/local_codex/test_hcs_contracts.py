from threading import Event
from subprocess import CompletedProcess

import pytest

from hcs_ai.local_codex.actions import ActionExecutor
from hcs_ai.local_codex.controller import AgentController
from hcs_ai.local_codex.self_update import SelfUpdater, StartupWarningStore
from hcs_ai.local_codex.state import AgentStatus, TaskJournal
from hcs_ai.local_codex.tornado import _local_codex_user_agent
from hcs_ai.local_codex.workspace import Workspace


@pytest.mark.parametrize("already_cancelled", [True, False])
def test_cancel_stops_before_next_action_and_persists_journal(tmp_path, already_cancelled):
    event = Event()
    if already_cancelled:
        event.set()
    journal = TaskJournal.new("Inspect files", str(tmp_path), "test", tmp_path / "logs")
    workspace = Workspace(tmp_path)
    (tmp_path / "note.txt").write_text("unchanged", encoding="utf-8")

    class Client:
        def chat(self, messages):
            return '{"action":"read_file","path":"note.txt"}'

    def status(message):
        if message == "Result: ok":
            event.set()

    controller = AgentController(
        Client(), ActionExecutor(workspace, journal, False), journal, 3,
        status_callback=status, cancel_event=event,
    )
    result = controller.run()

    assert result.status is AgentStatus.INTERRUPTED
    saved = TaskJournal.load(journal.path)
    assert saved.status is AgentStatus.INTERRUPTED
    assert [step["action"]["action"] for step in saved.steps] == (
        [] if already_cancelled else ["list_files", "read_file"]
    )
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "unchanged"


def test_updater_is_disabled_before_any_process_or_filesystem_side_effect(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return CompletedProcess(args, 1, stdout="", stderr="not a repository")

    updater = SelfUpdater(
        project_root=tmp_path,
        log_path=tmp_path / "update.log",
        warning_store=StartupWarningStore(tmp_path / "warnings.json"),
        runner=runner,
    )
    with pytest.raises(RuntimeError, match="^Local Codex updates are managed by HCS$"):
        updater.run()
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_tornado_identifies_the_imported_runtime_version():
    assert _local_codex_user_agent() == "LocalCodex/2.10.8"
