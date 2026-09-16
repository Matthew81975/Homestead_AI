from pathlib import Path

from hcs_ai.local_codex.state import AgentStatus, TaskJournal


def test_new_journal_defaults(tmp_path: Path):
    journal = TaskJournal.new(
        task="Inspect Maze World",
        workspace=r"C:\repo",
        model="qwen2.5-3b-instruct",
        log_dir=tmp_path,
    )
    assert journal.status is AgentStatus.WORKING
    assert journal.steps == []
    assert journal.files_changed == []
    assert journal.tests_run == []
    assert journal.consecutive_failures == 0
    assert journal.commit_approved is False
    assert journal.push_approved is False


def test_save_and_load_round_trip(tmp_path: Path):
    journal = TaskJournal.new(
        task="Inspect Maze World",
        workspace=r"C:\repo",
        model="qwen2.5-3b-instruct",
        log_dir=tmp_path,
    )
    journal.record_step({"action": "git_status"}, {"ok": True})
    journal.save()
    loaded = TaskJournal.load(journal.path)
    assert loaded.task == journal.task
    assert loaded.steps == journal.steps
    assert loaded.status is AgentStatus.WORKING


def test_snapshot_preserves_first_original(tmp_path: Path):
    journal = TaskJournal.new(
        task="Edit",
        workspace=r"C:\repo",
        model="qwen2.5-3b-instruct",
        log_dir=tmp_path,
    )
    journal.snapshot_file("main.py", "original")
    journal.snapshot_file("main.py", "changed")
    assert journal.original_files["main.py"] == "original"


def test_find_resumable_journals(tmp_path):
    from hcs_ai.local_codex.state import AgentStatus, TaskJournal, find_resumable_journals

    working = TaskJournal.new("work", r"C:\repo", "model", tmp_path)
    working.status = AgentStatus.INTERRUPTED
    working.save()

    done = TaskJournal.new("done", r"C:\repo", "model", tmp_path)
    done.status = AgentStatus.DONE
    done.save()

    found = find_resumable_journals(tmp_path)
    assert [item.task for item in found] == ["work"]


def test_new_journals_are_unique_even_with_same_clock_tick(tmp_path, monkeypatch):
    import hcs_ai.local_codex.state as state

    class FixedDateTime:
        @classmethod
        def now(cls):
            class Fixed:
                def strftime(self, fmt):
                    return "2026-09-06_154600_000000"
            return Fixed()

    monkeypatch.setattr(state, "datetime", FixedDateTime)

    first = TaskJournal.new("one", r"C:\\repo", "model", tmp_path)
    second = TaskJournal.new("two", r"C:\\repo", "model", tmp_path)

    assert first.path != second.path
    assert first.path.exists()
    assert second.path.exists()
