import json
from pathlib import Path

import hcs_ai


ROOT = Path(__file__).parents[1]


def test_release_versions_and_manifest_include_local_codex():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "0.11.0"
    assert hcs_ai.__version__ == "0.11.0"
    config = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
    assert config["app"]["version"] == "0.11.0"
    manifest = json.loads((ROOT / "update_manifest.json").read_text(encoding="utf-8"))
    paths = set(manifest["files"])
    assert "hcs_ai/local_codex/worker.py" in paths
    assert "hcs_ai/gui_local_codex.py" in paths
    assert "RELEASE_NOTES_v0.11.0.txt" in paths


def test_manifest_ships_every_tracked_local_codex_python_file():
    manifest = json.loads((ROOT / "update_manifest.json").read_text(encoding="utf-8"))
    shipped = set(manifest["files"])
    local_codex_files = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "hcs_ai" / "local_codex").glob("*.py")
    }
    assert local_codex_files <= shipped


def test_installer_and_updater_preserve_machine_local_state():
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    updater = (ROOT / "update_hcs.ps1").read_text(encoding="utf-8")
    assert "HCS-AI v0.11.0 installer" in installer
    assert 'Test-Path "config.json"' in installer
    assert "data/local_codex" not in json.dumps(
        json.loads((ROOT / "update_manifest.json").read_text(encoding="utf-8"))["files"]
    )
    assert "config.json" not in json.dumps(
        json.loads((ROOT / "update_manifest.json").read_text(encoding="utf-8"))["files"]
    )
    assert "update_manifest.json" in updater


def test_release_documentation_covers_operation_rollback_and_smoke_test():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    notes = (ROOT / "RELEASE_NOTES_v0.11.0.txt").read_text(encoding="utf-8")
    smoke = (ROOT / "docs" / "LOCAL_CODEX_WINDOWS_SMOKE_TEST.md").read_text(encoding="utf-8")
    for phrase in (
        "first migration", "Local Codex tab", "Clear View", "Stop", "Resume",
        "approval", "tray", "full exit", "rollback",
    ):
        assert phrase.casefold() in readme.casefold()
    assert "2.10.8" in notes
    assert "one task" in notes.casefold()
    for phrase in (
        "migration receipt", "workspace", "LM Studio", "fallback", "approval",
        "Stop", "Resume", "crash", "tray", "full exit", "Open Log Folder", "update",
    ):
        assert phrase.casefold() in smoke.casefold()
