from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class SelfUpdateResult:
    status: str
    message: str
    restart_required: bool = False


class StartupWarningStore:
    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"pending": None, "sent_key": None}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("startup warning state must be an object")
        return {"pending": value.get("pending"), "sent_key": value.get("sent_key")}

    def _save(self, value: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)

    def record(self, key: str, message: str) -> None:
        value = self._load()
        if value.get("sent_key") == key:
            value["pending"] = None
        else:
            value["pending"] = {"key": key, "message": message}
        self._save(value)

    def pending(self) -> dict[str, str] | None:
        pending = self._load().get("pending")
        if not isinstance(pending, dict):
            return None
        key = pending.get("key")
        message = pending.get("message")
        if not isinstance(key, str) or not isinstance(message, str):
            return None
        return {"key": key, "message": message}

    def acknowledge(self, key: str) -> None:
        value = self._load()
        pending = value.get("pending")
        if isinstance(pending, dict) and pending.get("key") == key:
            value["pending"] = None
        value["sent_key"] = key
        self._save(value)


class SelfUpdater:
    def __init__(
        self,
        project_root: Path,
        log_path: Path,
        warning_store: StartupWarningStore,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: int = 60,
    ):
        self.project_root = Path(project_root)
        self.log_path = Path(log_path)
        self.warning_store = warning_store
        self.runner = runner
        self.timeout = timeout

    def run(self) -> SelfUpdateResult:
        raise RuntimeError("Local Codex updates are managed by HCS")
