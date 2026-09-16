from __future__ import annotations

import os
from pathlib import Path

from .state import TaskJournal


class WorkspaceViolation(ValueError):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def resolve_safe(self, relative_path: str) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            candidate = raw.resolve()
        else:
            candidate = (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation(f"path escapes workspace: {relative_path}") from exc
        return candidate

    IGNORED_DIRS = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }

    def list_files(self, relative_path: str = ".", recursive: bool = True) -> list[str]:
        base = self.resolve_safe(relative_path)
        if not base.exists():
            raise FileNotFoundError(relative_path)
        if base.is_file():
            return [base.relative_to(self.root).as_posix()]

        if not recursive:
            entries: list[str] = []
            for path in base.iterdir():
                if path.name in self.IGNORED_DIRS:
                    continue
                rel = path.relative_to(self.root).as_posix()
                entries.append(rel + "/" if path.is_dir() else rel)
            return sorted(entries)

        files: list[str] = []
        for current_root, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name not in self.IGNORED_DIRS]
            current = Path(current_root)
            for filename in filenames:
                path = current / filename
                files.append(path.relative_to(self.root).as_posix())
        return sorted(files)

    def read_file(self, relative_path: str) -> str:
        return self.resolve_safe(relative_path).read_text(encoding="utf-8")

    def search_text(self, text: str, relative_path: str = ".") -> list[dict[str, object]]:
        matches: list[dict[str, object]] = []
        for name in self.list_files(relative_path):
            path = self.resolve_safe(name)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if text in line:
                    matches.append({"path": name, "line": line_number, "text": line})
        return matches

    def _prepare_write(self, relative_path: str, journal: TaskJournal, dry_run: bool) -> Path:
        if dry_run:
            raise PermissionError("writes are disabled in dry-run mode")
        path = self.resolve_safe(relative_path)
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        journal.snapshot_file(relative_path, original)
        if relative_path not in journal.files_changed:
            journal.files_changed.append(relative_path)
            journal.save()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_file(self, relative_path: str, contents: str, journal: TaskJournal, dry_run: bool) -> None:
        path = self._prepare_write(relative_path, journal, dry_run)
        path.write_text(contents, encoding="utf-8")

    def replace_text(
        self,
        relative_path: str,
        old: str,
        new: str,
        journal: TaskJournal,
        dry_run: bool,
    ) -> None:
        current = self.read_file(relative_path)
        if current.count(old) != 1:
            raise ValueError("replace_text requires exactly one match")
        path = self._prepare_write(relative_path, journal, dry_run)
        path.write_text(current.replace(old, new, 1), encoding="utf-8")
