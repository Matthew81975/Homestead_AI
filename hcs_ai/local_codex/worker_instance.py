from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class AlreadyRunningError(RuntimeError):
    """Raised when another mail worker owns the instance lock."""


class WorkerInstanceLock:
    """An inter-process lock that also publishes the worker PID."""

    def __init__(self, pid_path: Path):
        self.pid_path = Path(pid_path)
        self.lock_path = self.pid_path.with_suffix(".lock")
        self._file: BinaryIO | None = None

    def __enter__(self) -> "WorkerInstanceLock":
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            self._lock(handle)
        except OSError as exc:
            handle.close()
            owner = self._read_owner()
            detail = f" (PID {owner})" if owner else ""
            raise AlreadyRunningError(
                f"Local Codex mail worker is already running{detail}."
            ) from exc

        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        self._file = handle
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._file is None:
            return
        try:
            self._unlock(self._file)
        finally:
            self._file.close()
            self._file = None

    def _read_owner(self) -> str:
        try:
            return self.pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @staticmethod
    def _lock(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
