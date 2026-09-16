"""One-time, read-only migration of standalone Local Codex state into HCS."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterable

from .state import TaskJournal
from .task_queue import TaskQueue
from .workspace_registry import WorkspaceRegistry


_SEMVER_RE = re.compile(r"(?:^|[vV])(?P<version>\d+\.\d+\.\d+)(?:$|[^\d.])")
_SECRET_KEY_RE = re.compile(r"(?:^|[_-])(?:password|secret|token|api_key)(?:$|[_-])", re.IGNORECASE)


@dataclass
class MigrationResult:
    source_path: Path | None
    source_version: str | None
    imported_categories: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _version_from_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _SEMVER_RE.search(value.strip())
    return match.group("version") if match else None


def _version_for_install(path: Path) -> str | None:
    version = _version_from_text(path.name)
    if version:
        return version
    try:
        version = _version_from_text((path / "VERSION").read_text(encoding="utf-8").strip())
    except OSError:
        version = None
    if version:
        return version
    try:
        config = _read_json(path / "config.json")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return _version_from_text(config.get("version")) if isinstance(config, dict) else None


def _version_key(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def _candidate_paths(candidates: Iterable[Path]) -> list[Path]:
    """Accept configured installs and parent folders containing sibling installs."""
    found: dict[Path, None] = {}
    for raw_candidate in candidates:
        candidate = Path(raw_candidate).expanduser()
        is_exact_install = candidate.name.lower().startswith("local_codex_agent_v")
        if candidate.is_dir():
            found[candidate] = None
        scan_root = (
            candidate.parent
            if is_exact_install
            else candidate
        )
        if not scan_root.is_dir():
            continue
        try:
            for child in scan_root.iterdir():
                if child.is_dir() and child.name.lower().startswith("local_codex_agent_v"):
                    found[child] = None
        except OSError:
            continue
    return list(found)


def discover_legacy_install(candidates: Iterable[Path]) -> Path | None:
    """Return the newest valid standalone Local Codex installation, if any."""
    valid: list[tuple[tuple[int, int, int], Path]] = []
    for candidate in _candidate_paths(candidates):
        try:
            config = _read_json(candidate / "config.json")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        version = _version_for_install(candidate)
        if not isinstance(config, dict) or version is None:
            continue
        valid.append((_version_key(version), candidate))
    return max(valid, key=lambda item: item[0])[1] if valid else None


def _fill_missing(existing: dict[str, Any], legacy: dict[str, Any]) -> dict[str, Any]:
    """Retain HCS-owned choices while filling only absent values from legacy state."""
    merged = dict(existing)
    for key, value in legacy.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(value, dict) and isinstance(merged[key], dict):
            merged[key] = _fill_missing(merged[key], value)
    return merged


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _configured_credential_names(value: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(value, dict):
        return names
    for key in ("credential_names", "secret_names"):
        listed = value.get(key)
        if isinstance(listed, list):
            names.update(
                item.casefold().replace("-", "_")
                for item in listed
                if isinstance(item, str)
            )
    return names


def _is_secret_key(key: object, credential_names: set[str]) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    if normalized in credential_names:
        return True
    # Environment-variable names are references, not credential values.
    return not normalized.endswith("_env") and _SECRET_KEY_RE.search(normalized) is not None


def _secret_values(value: Any, credential_names: set[str]) -> set[str]:
    values: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if _is_secret_key(key, credential_names) and isinstance(nested, str) and nested:
                values.add(nested)
            values.update(_secret_values(nested, credential_names))
    elif isinstance(value, list):
        for nested in value:
            values.update(_secret_values(nested, credential_names))
    return values


def _without_secrets(value: Any, credential_names: set[str], secret_values: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_secrets(nested, credential_names, secret_values)
            for key, nested in value.items()
            if not _is_secret_key(key, credential_names)
        }
    if isinstance(value, list):
        return [_without_secrets(item, credential_names, secret_values) for item in value]
    if isinstance(value, str):
        safe = value
        for secret in secret_values:
            if secret:
                safe = safe.replace(secret, "[REDACTED]")
        return safe
    return value


def _load_category(path: Path, category: str, warnings: list[str]) -> Any | None:
    if not path.exists():
        warnings.append(f"{category} was not found")
        return None
    try:
        return _read_json(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        warnings.append(f"{category} could not be read")
        return None


def _load_existing_config(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    if not path.exists():
        return {}
    try:
        value = _read_json(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        warnings.append("existing HCS local configuration could not be read")
        return None
    if not isinstance(value, dict):
        warnings.append("existing HCS local configuration is not an object")
        return None
    return value


def _validated_by_loader(payload: Any, loader: Callable[[Path], Any]) -> bool:
    """Use the owning runtime loader on an isolated temporary file before persistence."""
    try:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loader(path)
    except Exception:
        return False
    return True


def _valid_workspace_registry(payload: Any) -> bool:
    return isinstance(payload, dict) and _validated_by_loader(payload, WorkspaceRegistry.load)


def _valid_task_queue(payload: Any) -> bool:
    return isinstance(payload, dict) and _validated_by_loader(payload, TaskQueue.load)


def _valid_journal(payload: Any) -> bool:
    return isinstance(payload, dict) and _validated_by_loader(payload, TaskJournal.load)


def _valid_tornado_state(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    providers = payload.get("providers")
    return providers is None or (
        isinstance(providers, dict)
        and all(isinstance(provider_id, str) and isinstance(state, dict) for provider_id, state in providers.items())
    )


def _load_existing_state(
    path: Path,
    category: str,
    validator: Callable[[Any], bool],
    warnings: list[str],
) -> Any | None:
    if not path.exists():
        return None
    value = _load_category(path, f"existing {category}", warnings)
    if value is None or not validator(value):
        warnings.append(f"existing {category} is not loadable and was preserved")
        return False
    return value


def _merge_workspaces(existing: dict[str, Any] | None, legacy: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    if existing is False:
        return None
    if existing is None:
        return legacy
    merged = _fill_missing(existing, legacy)
    merged["workspaces"] = list(existing["workspaces"])
    existing_ids = {item["workspace_id"] for item in existing["workspaces"]}
    for workspace in legacy["workspaces"]:
        if workspace["workspace_id"] in existing_ids:
            continue
        candidate = dict(merged)
        candidate["workspaces"] = [*merged["workspaces"], workspace]
        if _valid_workspace_registry(candidate):
            merged = candidate
            existing_ids.add(workspace["workspace_id"])
        else:
            warnings.append("legacy workspace conflicts with existing HCS registration")
    return merged


def _merge_task_queues(existing: dict[str, Any] | None, legacy: dict[str, Any]) -> dict[str, Any] | None:
    if existing is False:
        return None
    if existing is None:
        return legacy
    merged = _fill_missing(existing, legacy)
    merged["tasks"] = {**legacy.get("tasks", {}), **existing.get("tasks", {})}
    merged["workspace_locks"] = {**legacy.get("workspace_locks", {}), **existing.get("workspace_locks", {})}
    return merged


def _receipt_result(receipt: dict[str, Any]) -> MigrationResult:
    source = receipt.get("source_path")
    imported = receipt.get("imported_categories")
    warnings = receipt.get("warnings")
    return MigrationResult(
        source_path=Path(source) if isinstance(source, str) and source else None,
        source_version=receipt.get("source_version") if isinstance(receipt.get("source_version"), str) else None,
        imported_categories=[item for item in imported if isinstance(item, str)] if isinstance(imported, list) else [],
        warnings=[item for item in warnings if isinstance(item, str)] if isinstance(warnings, list) else [],
        skipped=True,
    )


def migrate_legacy_install(
    *,
    candidates: Iterable[Path],
    data_root: Path,
    local_config_path: Path,
    clock: Callable[[], float] = time.time,
) -> MigrationResult:
    """Copy compatible standalone state once, leaving the source entirely untouched."""
    local_root = Path(data_root) / "local_codex"
    receipt_path = local_root / "migration" / "receipt.json"
    if receipt_path.exists():
        try:
            receipt = _read_json(receipt_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            receipt = None
        if isinstance(receipt, dict) and receipt.get("status") == "completed":
            return _receipt_result(receipt)

    warnings: list[str] = []
    imported: list[str] = []
    incomplete = False
    source = discover_legacy_install(candidates)
    if source is None:
        warnings.append("No valid standalone Local Codex installation was found")
        receipt = {
            "status": "completed",
            "source_path": None,
            "source_version": None,
            "completed_at": clock(),
            "imported_categories": imported,
            "warnings": warnings,
        }
        _atomic_json_write(receipt_path, receipt)
        return MigrationResult(None, None, imported, warnings)

    version = _version_for_install(source)
    core = _load_category(source / "config.json", "preferences", warnings)
    if not isinstance(core, dict):
        # Discovery validated this file, but do not write a receipt for an interrupted read.
        return MigrationResult(source, version, imported, warnings)

    credential_names = _configured_credential_names(core)
    # Learn every configured credential value before writing any category. A
    # journal copied early must still redact a credential declared in a later
    # file, while the standalone tree remains read-only throughout.
    source_documents: list[Any] = []
    for source_json_path in source.rglob("*.json"):
        if source_json_path.is_symlink():
            continue
        try:
            source_documents.append(_read_json(source_json_path))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    for document in source_documents:
        credential_names.update(_configured_credential_names(document))
    all_secret_values: set[str] = set()
    for document in source_documents:
        all_secret_values.update(_secret_values(document, credential_names))
    existing_config = _load_existing_config(Path(local_config_path), warnings)
    if existing_config is None:
        # Do not replace an unreadable HCS-owned configuration or mark a
        # partially applied migration complete.
        return MigrationResult(source, version, imported, warnings)
    local_patch: dict[str, Any] = {}

    preferences = {key: value for key, value in core.items() if key not in {"tornado", "credential_names", "secret_names"}}
    preferences = _without_secrets(preferences, credential_names, all_secret_values)
    local_patch.update(preferences)
    imported.append("preferences")

    tornado = core.get("tornado")
    if tornado is not None:
        if isinstance(tornado, dict):
            all_secret_values.update(_secret_values(tornado, credential_names))
            local_patch["tornado"] = _without_secrets(tornado, credential_names, all_secret_values)
            imported.append("tornado")
        else:
            warnings.append("tornado configuration is not an object")

    email = _load_category(source / "config" / "email.json", "email", warnings)
    if email is not None:
        if isinstance(email, dict):
            all_secret_values.update(_secret_values(email, credential_names))
            local_patch["email"] = _without_secrets(email, credential_names, all_secret_values)
            imported.append("email")
        else:
            warnings.append("email configuration is not an object")

    workspaces = _load_category(source / "config" / "workspaces.json", "workspaces", warnings)
    if workspaces is not None:
        safe_workspaces = _without_secrets(workspaces, credential_names, all_secret_values)
        if _valid_workspace_registry(safe_workspaces):
            workspaces_path = local_root / "workspaces" / "workspaces.json"
            existing_workspaces = _load_existing_state(
                workspaces_path, "workspaces", _valid_workspace_registry, warnings
            )
            if existing_workspaces is False:
                incomplete = True
            merged_workspaces = _merge_workspaces(
                existing_workspaces,
                safe_workspaces,
                warnings,
            )
            if merged_workspaces is not None and _valid_workspace_registry(merged_workspaces):
                _atomic_json_write(workspaces_path, merged_workspaces)
                imported.append("workspaces")
            else:
                warnings.append("workspaces could not be merged safely")
        else:
            warnings.append("workspaces is not a valid registry")

    tasks = _load_category(source / "state" / "tasks.json", "tasks", warnings)
    if tasks is not None:
        safe_tasks = _without_secrets(tasks, credential_names, all_secret_values)
        if _valid_task_queue(safe_tasks):
            tasks_path = local_root / "tasks" / "tasks.json"
            existing_tasks = _load_existing_state(tasks_path, "tasks", _valid_task_queue, warnings)
            if existing_tasks is False:
                incomplete = True
            merged_tasks = _merge_task_queues(
                existing_tasks, safe_tasks
            )
            if merged_tasks is not None and _valid_task_queue(merged_tasks):
                _atomic_json_write(tasks_path, merged_tasks)
                imported.append("tasks")
            else:
                warnings.append("tasks could not be merged safely")
        else:
            warnings.append("tasks is not a valid queue")

    journal_files = list((source / "logs").rglob("task_*.json")) if (source / "logs").is_dir() else []
    if not journal_files:
        warnings.append("journals were not found")
    else:
        journal_count = 0
        for journal_path in journal_files:
            journal = _load_category(journal_path, "journal", warnings)
            if not isinstance(journal, dict):
                continue
            all_secret_values.update(_secret_values(journal, credential_names))
            safe_journal = _without_secrets(journal, credential_names, all_secret_values)
            if not _valid_journal(safe_journal):
                warnings.append("journal is not loadable")
                continue
            relative = journal_path.relative_to(source / "logs")
            destination = local_root / "tasks" / "journals" / relative
            if destination.exists():
                existing_journal = _load_category(destination, "existing journal", warnings)
                if not _valid_journal(existing_journal):
                    warnings.append("existing journal is not loadable and was preserved")
                    incomplete = True
                continue
            _atomic_json_write(destination, safe_journal)
            journal_count += 1
        if journal_count:
            imported.append("journals")
        elif journal_files:
            warnings.append("journals could not be read")

    tornado_state = _load_category(source / "state" / "tornado_state.json", "tornado state", warnings)
    if tornado_state is not None:
        safe_tornado_state = _without_secrets(tornado_state, credential_names, all_secret_values)
        if _valid_tornado_state(safe_tornado_state):
            tornado_path = local_root / "tornado" / "tornado_state.json"
            existing_tornado = _load_existing_state(
                tornado_path, "tornado state", _valid_tornado_state, warnings
            )
            if existing_tornado is False:
                incomplete = True
            if existing_tornado is not False:
                merged_tornado = (
                    safe_tornado_state
                    if existing_tornado is None
                    else _fill_missing(existing_tornado, safe_tornado_state)
                )
                if _valid_tornado_state(merged_tornado):
                    _atomic_json_write(tornado_path, merged_tornado)
                    imported.append("tornado_state")
                else:
                    warnings.append("tornado state could not be merged safely")
        else:
            warnings.append("tornado state is not loadable")

    _atomic_json_write(Path(local_config_path), _fill_missing(existing_config, {"local_codex": local_patch}))

    if incomplete:
        return MigrationResult(source, version, imported, warnings)

    receipt = {
        "status": "completed",
        "source_path": str(source),
        "source_version": version,
        "completed_at": clock(),
        "imported_categories": imported,
        "warnings": warnings,
    }
    _atomic_json_write(receipt_path, receipt)
    return MigrationResult(source, version, imported, warnings)
