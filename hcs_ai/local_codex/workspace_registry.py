from __future__ import annotations

import json
import re
from pathlib import Path

from .models import WorkspaceConfig


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _looks_absolute(path_text: str) -> bool:
    path = Path(path_text)
    if path.is_absolute():
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", path_text))


def _contains_alias(text: str, alias: str) -> bool:
    normalized_text = _normalize(text)
    normalized_alias = _normalize(alias)
    if not normalized_alias:
        return False
    pattern = rf"(?<![\w]){re.escape(normalized_alias)}(?![\w])"
    return re.search(pattern, normalized_text, flags=re.IGNORECASE) is not None


class WorkspaceRegistry:
    def __init__(self, workspaces: list[WorkspaceConfig]):
        self._workspaces = {workspace.workspace_id: workspace for workspace in workspaces}
        self._aliases: dict[str, WorkspaceConfig] = {}
        for workspace in workspaces:
            if not workspace.enabled:
                continue
            for alias in workspace.normalized_aliases():
                existing = self._aliases.get(alias)
                if existing is not None and existing.workspace_id != workspace.workspace_id:
                    raise ValueError(f"duplicate alias: {alias}")
                self._aliases[alias] = workspace

    @classmethod
    def load(cls, path: Path) -> "WorkspaceRegistry":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        items = raw.get("workspaces")
        if not isinstance(items, list):
            raise ValueError("workspaces must be a list")

        workspaces: list[WorkspaceConfig] = []
        ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("workspace entry must be an object")
            workspace = WorkspaceConfig(**item)
            if not workspace.workspace_id.strip():
                raise ValueError("workspace_id is required")
            if workspace.workspace_id in ids:
                raise ValueError(f"duplicate workspace id: {workspace.workspace_id}")
            ids.add(workspace.workspace_id)
            if not _looks_absolute(workspace.path):
                raise ValueError(f"workspace path must be absolute: {workspace.path}")
            workspaces.append(workspace)
        return cls(workspaces)

    def get(self, workspace_id: str) -> WorkspaceConfig:
        workspace = self._workspaces[workspace_id]
        if not workspace.enabled:
            raise KeyError(workspace_id)
        return workspace

    def resolve_explicit(self, text: str) -> WorkspaceConfig | None:
        matches = {
            workspace.workspace_id: workspace
            for alias, workspace in self._aliases.items()
            if _contains_alias(text, alias)
        }
        if len(matches) == 1:
            return next(iter(matches.values()))
        return None

    def infer(self, text: str) -> WorkspaceConfig | None:
        return self.resolve_explicit(text)

    def is_registered_path(self, path: Path) -> bool:
        candidate = path.resolve()
        for workspace in self._workspaces.values():
            if not workspace.enabled:
                continue
            try:
                configured = Path(workspace.path).resolve()
            except OSError:
                continue
            if configured == candidate:
                return True
        return False

    def all_enabled(self) -> list[WorkspaceConfig]:
        return [workspace for workspace in self._workspaces.values() if workspace.enabled]
