import json
from pathlib import Path

from hcs_ai.local_codex.migration import discover_legacy_install, migrate_legacy_install
from hcs_ai.local_codex.state import TaskJournal
from hcs_ai.local_codex.task_queue import TaskQueue
from hcs_ai.local_codex.tornado import TornadoStateStore
from hcs_ai.local_codex.workspace_registry import WorkspaceRegistry


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def make_legacy(path: Path, *, version: str = "2.10.8", complete: bool = False) -> Path:
    path.mkdir(parents=True)
    (path / "VERSION").write_text(version, encoding="utf-8")
    write_json(
        path / "config.json",
        {
            "workspace": "Z:/disconnected/project",
            "model": "legacy-model",
            "max_failed_actions": 7,
            "tornado": {
                "enabled": True,
                "providers": [{"id": "local", "model": "legacy-model"}],
            },
        },
    )
    if complete:
        write_json(
            path / "config" / "workspaces.json",
            {
                "workspaces": [
                    {
                        "workspace_id": "offline-drive",
                        "name": "Offline drive",
                        "aliases": ["offline"],
                        "path": "Z:/disconnected/project",
                        "target_branch": "main",
                    }
                ]
            },
        )
        write_json(
            path / "config" / "email.json",
            {"account": "operator@example.test", "trusted_sender": "operator@example.test"},
        )
        write_json(path / "state" / "tasks.json", {"tasks": {}, "workspace_locks": {}})
        write_json(
            path / "logs" / "email" / "task_1.json",
            {
                "task": "continue safely",
                "workspace": "Z:/disconnected/project",
                "model": "legacy-model",
                "status": "paused",
            },
        )
        write_json(path / "state" / "tornado_state.json", {"local": {"total_calls": 3}})
    return path


def snapshot_tree(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def task_record(task_id: str, *, subject: str) -> dict:
    return {
        "task_id": task_id,
        "thread_id": None,
        "sender": "operator@example.test",
        "subject": subject,
        "body": "do the work",
        "workspace_id": "offline-drive",
        "status": "queued",
        "approval_status": "not_requested",
        "created_at": "2026-09-15T00:00:00+00:00",
    }


def test_discovery_selects_highest_valid_semantic_version(tmp_path):
    """Choosing a lower valid standalone version would silently discard newer settings."""
    make_legacy(tmp_path / "Local_Codex_Agent_v2.9.9", version="2.9.9")
    newest = make_legacy(tmp_path / "Local_Codex_Agent_v2.10.8", version="2.10.8")
    (tmp_path / "Local_Codex_Agent_v9.0.0").mkdir()

    assert discover_legacy_install(list(tmp_path.iterdir())) == newest


def test_migration_is_idempotent_and_never_modifies_source(tmp_path):
    """A second startup must retain the standalone installation as a rollback copy."""
    source = make_legacy(tmp_path / "legacy", complete=True)
    before = snapshot_tree(source)

    first = migrate_legacy_install(
        candidates=[source],
        data_root=tmp_path / "data",
        local_config_path=tmp_path / "config.json",
        clock=lambda: 1000.0,
    )
    second = migrate_legacy_install(
        candidates=[source],
        data_root=tmp_path / "data",
        local_config_path=tmp_path / "config.json",
        clock=lambda: 2000.0,
    )

    assert first.skipped is False
    assert set(first.imported_categories) >= {"preferences", "workspaces", "tasks", "journals"}
    assert second.skipped is True
    assert snapshot_tree(source) == before
    receipt = read_json(tmp_path / "data" / "local_codex" / "migration" / "receipt.json")
    assert receipt["completed_at"] == 1000.0
    assert receipt["status"] == "completed"


def test_absent_source_creates_a_completed_empty_receipt(tmp_path):
    """No standalone install must leave the new HCS subsystem usable and not retry forever."""
    result = migrate_legacy_install(
        candidates=[tmp_path / "missing"],
        data_root=tmp_path / "data",
        local_config_path=tmp_path / "config.json",
        clock=lambda: 10.0,
    )

    assert result.source_path is None
    assert result.skipped is False
    assert result.imported_categories == []
    assert read_json(tmp_path / "data" / "local_codex" / "migration" / "receipt.json")["status"] == "completed"


def test_partial_and_corrupt_categories_do_not_block_valid_imports(tmp_path):
    """A broken workspace registry must not prevent independent email settings from migrating."""
    source = make_legacy(tmp_path / "legacy", complete=True)
    (source / "config" / "workspaces.json").write_text("{broken", encoding="utf-8")

    result = migrate_legacy_install(
        candidates=[source],
        data_root=tmp_path / "data",
        local_config_path=tmp_path / "config.json",
    )

    assert "workspaces" not in result.imported_categories
    assert "email" in result.imported_categories
    assert any("workspaces" in warning for warning in result.warnings)
    assert read_json(tmp_path / "config.json")["local_codex"]["email"]["account"] == "operator@example.test"


def test_migration_preserves_disconnected_registered_paths(tmp_path):
    """Migration must retain a registered drive path even while that drive is offline."""
    source = make_legacy(tmp_path / "legacy", complete=True)

    migrate_legacy_install(
        candidates=[source],
        data_root=tmp_path / "data",
        local_config_path=tmp_path / "config.json",
    )

    workspaces = read_json(tmp_path / "data" / "local_codex" / "workspaces" / "workspaces.json")
    assert workspaces["workspaces"][0]["path"] == "Z:/disconnected/project"


def test_migration_preserves_existing_explicit_hcs_preferences(tmp_path):
    """Legacy import must not reverse an existing HCS choice during a nested merge."""
    source = make_legacy(tmp_path / "legacy")
    write_json(
        tmp_path / "config.json",
        {"local_codex": {"tornado": {"state_path": "keep/state.json", "enabled": False}, "ui": {"follow": True}}},
    )

    migrate_legacy_install(
        candidates=[source],
        data_root=tmp_path / "data",
        local_config_path=tmp_path / "config.json",
    )

    migrated = read_json(tmp_path / "config.json")["local_codex"]
    assert migrated["ui"] == {"follow": True}
    assert migrated["tornado"]["enabled"] is False
    assert migrated["tornado"]["state_path"] == "keep/state.json"
    assert migrated["tornado"]["providers"] == [{"id": "local", "model": "legacy-model"}]


def test_migration_never_persists_secret_values_in_state_or_receipt(tmp_path):
    """A secret embedded in any legacy category must not cross into HCS-owned files."""
    secret = "do-not-copy-this-secret"
    source = make_legacy(tmp_path / "legacy", complete=True)
    core = read_json(source / "config.json")
    core["password"] = secret
    core["tornado"]["providers"][0]["api_key"] = secret
    core["tornado"]["providers"][0]["api_key_env"] = "LEGACY_API_KEY"
    write_json(source / "config.json", core)
    write_json(source / "config" / "email.json", {"account": "operator@example.test", "smtp_secret": secret})
    write_json(source / "state" / "tasks.json", {"tasks": {"one": {"token": secret}}, "workspace_locks": {}})
    write_json(source / "logs" / "email" / "task_1.json", {"task": f"use {secret}", "password": secret})

    migrate_legacy_install(
        candidates=[source],
        data_root=tmp_path / "data",
        local_config_path=tmp_path / "config.json",
    )

    destination = tmp_path / "data" / "local_codex"
    persisted = "\n".join(item.read_text(encoding="utf-8") for item in destination.rglob("*") if item.is_file())
    assert secret not in persisted
    config = read_json(tmp_path / "config.json")
    assert secret not in json.dumps(config)
    assert config["local_codex"]["tornado"]["providers"][0]["api_key_env"] == "LEGACY_API_KEY"


def test_later_category_secret_redacts_earlier_copied_journal(tmp_path):
    """Writing journals before learning another category's credential leaks cross-file values."""
    secret = "shared-credential-value"
    source = make_legacy(tmp_path / "legacy", complete=True)
    write_json(
        source / "logs" / "email" / "task_1.json",
        {"task": f"do not expose {secret}", "workspace": "Z:/legacy", "model": "legacy", "status": "working"},
    )
    write_json(source / "state" / "tornado_state.json", {"api_key": secret})

    migrate_legacy_install(
        candidates=[source],
        data_root=tmp_path / "data",
        local_config_path=tmp_path / "config.json",
    )

    copied = read_json(tmp_path / "data" / "local_codex" / "tasks" / "journals" / "email" / "task_1.json")
    assert secret not in copied["task"]


def test_migration_removes_hyphenated_configured_credential_names(tmp_path):
    """A hyphenated credential name must redact the matching normalized key."""
    secret = "nonstandard-credential-value"
    source = make_legacy(tmp_path / "legacy")
    core = read_json(source / "config.json")
    core["credential_names"] = ["merchant-key-material"]
    core["merchant_key_material"] = secret
    write_json(source / "config.json", core)

    migrate_legacy_install(
        candidates=[source], data_root=tmp_path / "data", local_config_path=tmp_path / "config.json"
    )

    assert secret not in (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "merchant_key_material" not in read_json(tmp_path / "config.json")["local_codex"]


def test_migration_only_commits_runtime_loadable_categories(tmp_path):
    """Outer JSON containers are insufficient when runtime loaders reject their entries."""
    source = make_legacy(tmp_path / "legacy", complete=True)
    write_json(
        source / "config" / "workspaces.json",
        {"workspaces": [{"workspace_id": None, "name": "bad", "aliases": [], "path": "Z:/bad", "target_branch": "main"}]},
    )
    write_json(source / "state" / "tasks.json", {"tasks": {"bad": {"status": "unknown"}}, "workspace_locks": {}})
    write_json(source / "logs" / "email" / "task_1.json", {"task": "missing runtime fields"})
    write_json(source / "state" / "tornado_state.json", {"providers": "not-an-object"})

    result = migrate_legacy_install(
        candidates=[source], data_root=tmp_path / "data", local_config_path=tmp_path / "config.json"
    )

    assert not {"workspaces", "tasks", "journals", "tornado_state"}.intersection(result.imported_categories)
    assert not (tmp_path / "data" / "local_codex" / "workspaces" / "workspaces.json").exists()
    assert not (tmp_path / "data" / "local_codex" / "tasks" / "tasks.json").exists()
    assert not (tmp_path / "data" / "local_codex" / "tasks" / "journals" / "email" / "task_1.json").exists()


def test_migration_outputs_are_loadable_by_runtime_components(tmp_path):
    """Every persisted state category must be accepted by the component that will load it."""
    source = make_legacy(tmp_path / "legacy", complete=True)
    write_json(source / "state" / "tasks.json", {"tasks": {"legacy": task_record("legacy", subject="legacy")}, "workspace_locks": {}})

    migrate_legacy_install(
        candidates=[source], data_root=tmp_path / "data", local_config_path=tmp_path / "config.json"
    )

    destination = tmp_path / "data" / "local_codex"
    WorkspaceRegistry.load(destination / "workspaces" / "workspaces.json")
    TaskQueue.load(destination / "tasks" / "tasks.json")
    TaskJournal.load(destination / "tasks" / "journals" / "email" / "task_1.json")
    TornadoStateStore(destination / "tornado" / "tornado_state.json").snapshot("local")


def test_migration_merges_existing_hcs_state_without_replacing_collisions(tmp_path):
    """A damaged receipt retry must retain HCS-owned records before adding legacy records."""
    source = make_legacy(tmp_path / "legacy", complete=True)
    source_workspaces = read_json(source / "config" / "workspaces.json")
    source_workspaces["workspaces"].append(
        {"workspace_id": "legacy-new", "name": "Legacy new", "aliases": ["legacy"], "path": "Y:/legacy", "target_branch": "main"}
    )
    write_json(source / "config" / "workspaces.json", source_workspaces)
    write_json(
        source / "state" / "tasks.json",
        {"tasks": {"shared": task_record("shared", subject="legacy shared"), "legacy": task_record("legacy", subject="legacy only")}, "workspace_locks": {"offline-drive": "shared"}},
    )
    write_json(source / "logs" / "email" / "task_1.json", {"task": "legacy", "workspace": "Z:/legacy", "model": "legacy", "status": "working"})
    write_json(source / "logs" / "email" / "task_2.json", {"task": "legacy new", "workspace": "Y:/legacy", "model": "legacy", "status": "working"})
    write_json(source / "state" / "tornado_state.json", {"providers": {"shared": {"total_calls": 1}, "legacy": {"total_calls": 2}}})

    root = tmp_path / "data" / "local_codex"
    write_json(root / "workspaces" / "workspaces.json", {"workspaces": [{"workspace_id": "offline-drive", "name": "HCS", "aliases": ["hcs"], "path": "X:/hcs", "target_branch": "main"}]})
    write_json(root / "tasks" / "tasks.json", {"tasks": {"shared": task_record("shared", subject="HCS shared")}, "workspace_locks": {"offline-drive": "shared"}})
    write_json(root / "tasks" / "journals" / "email" / "task_1.json", {"task": "HCS", "workspace": "X:/hcs", "model": "hcs", "status": "working"})
    write_json(root / "tornado" / "tornado_state.json", {"providers": {"shared": {"total_calls": 9}, "hcs": {"total_calls": 4}}})
    write_json(root / "migration" / "receipt.json", {"status": "interrupted"})

    migrate_legacy_install(
        candidates=[source], data_root=tmp_path / "data", local_config_path=tmp_path / "config.json"
    )

    workspaces = read_json(root / "workspaces" / "workspaces.json")["workspaces"]
    assert {item["workspace_id"] for item in workspaces} == {"offline-drive", "legacy-new"}
    assert next(item for item in workspaces if item["workspace_id"] == "offline-drive")["name"] == "HCS"
    tasks = read_json(root / "tasks" / "tasks.json")["tasks"]
    assert tasks["shared"]["subject"] == "HCS shared"
    assert tasks["legacy"]["subject"] == "legacy only"
    assert read_json(root / "tasks" / "journals" / "email" / "task_1.json")["task"] == "HCS"
    assert (root / "tasks" / "journals" / "email" / "task_2.json").exists()
    providers = read_json(root / "tornado" / "tornado_state.json")["providers"]
    assert providers["shared"]["total_calls"] == 9
    assert providers["legacy"]["total_calls"] == 2


def test_unreadable_hcs_config_is_preserved_and_migration_stays_incomplete(tmp_path):
    """A corrupt HCS config cannot safely be replaced just to finish the migration receipt."""
    source = make_legacy(tmp_path / "legacy", complete=True)
    local_config = tmp_path / "config.json"
    local_config.write_text("{not json", encoding="utf-8")
    before = local_config.read_bytes()

    result = migrate_legacy_install(candidates=[source], data_root=tmp_path / "data", local_config_path=local_config)

    assert result.skipped is False
    assert local_config.read_bytes() == before
    assert not (tmp_path / "data" / "local_codex" / "migration" / "receipt.json").exists()


def test_discovery_expands_home_candidate_and_checks_siblings(tmp_path, monkeypatch):
    """Configured exact paths must expand home and still select the newest sibling install."""
    monkeypatch.setenv("HOME", str(tmp_path))
    older = make_legacy(tmp_path / "Local_Codex_Agent_v2.9.9", version="2.9.9")
    newest = make_legacy(tmp_path / "Local_Codex_Agent_v2.10.8", version="2.10.8")

    assert discover_legacy_install([Path("~/Local_Codex_Agent_v2.9.9")]) == newest
    assert older.exists()


def test_discovery_checks_siblings_when_exact_configured_install_is_absent(tmp_path):
    """A removed configured version must not hide remaining standalone sibling installs."""
    make_legacy(tmp_path / "Local_Codex_Agent_v2.9.9", version="2.9.9")
    newest = make_legacy(tmp_path / "Local_Codex_Agent_v2.11.0", version="2.11.0")

    assert discover_legacy_install([tmp_path / "Local_Codex_Agent_v2.10.8"]) == newest


def test_corrupt_existing_destination_leaves_migration_retryable_until_repaired(tmp_path):
    """A preserved corrupt destination cannot receive a completed receipt before a retry succeeds."""
    source = make_legacy(tmp_path / "legacy", complete=True)
    root = tmp_path / "data" / "local_codex"
    workspaces_path = root / "workspaces" / "workspaces.json"
    workspaces_path.parent.mkdir(parents=True)
    workspaces_path.write_text("{corrupt", encoding="utf-8")
    before = workspaces_path.read_bytes()

    first = migrate_legacy_install(
        candidates=[source], data_root=tmp_path / "data", local_config_path=tmp_path / "config.json"
    )

    receipt = root / "migration" / "receipt.json"
    assert first.skipped is False
    assert workspaces_path.read_bytes() == before
    assert not receipt.exists()

    workspaces_path.unlink()
    second = migrate_legacy_install(
        candidates=[source], data_root=tmp_path / "data", local_config_path=tmp_path / "config.json"
    )

    assert second.skipped is False
    assert read_json(receipt)["status"] == "completed"
    WorkspaceRegistry.load(workspaces_path)


def test_malformed_completed_receipt_degrades_without_crashing(tmp_path):
    """A syntactically valid receipt with wrong field types must not abort startup."""
    receipt = tmp_path / "data" / "local_codex" / "migration" / "receipt.json"
    write_json(receipt, {"status": "completed", "source_path": 7, "source_version": 8, "imported_categories": 9, "warnings": None})

    result = migrate_legacy_install(candidates=[], data_root=tmp_path / "data", local_config_path=tmp_path / "config.json")

    assert result.skipped is True
    assert result.source_path is None
    assert result.source_version is None
    assert result.imported_categories == []
    assert result.warnings == []
