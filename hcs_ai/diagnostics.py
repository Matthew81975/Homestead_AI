from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable
import json
import re


_SECRET_KEY_RE = re.compile(r"(authorization|api[_-]?key|token|secret|password|credential)", re.I)


@dataclass(slots=True)
class DiagnosticEvent:
    timestamp: str
    severity: str
    subsystem: str
    operation: str
    message: str
    elapsed_seconds: float | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    prompt_tokens_per_second: float | None = None
    output_tokens_per_second: float | None = None
    http_method: str | None = None
    http_endpoint: str | None = None
    http_status: int | None = None
    context: dict[str, Any] = field(default_factory=dict)
    diagnostic_payload: dict[str, Any] = field(default_factory=dict)
    exception: str | None = None
    event_id: int = 0


@dataclass(slots=True)
class TelemetrySnapshot:
    state: str = "Ready"
    backend_state: str | None = None
    backend_port: int | None = None
    active_model: str | None = None
    last_response_seconds: float | None = None
    prompt_seconds: float | None = None
    prompt_tokens_per_second: float | None = None
    output_tokens_per_second: float | None = None
    last_http_status: int | None = None
    active_subsystem: str | None = None
    active_operation: str | None = None
    last_error: str | None = None
    diagnostic_payload_capture: bool = False


class DiagnosticsService:
    """Thread-safe structured diagnostics store and session logger."""

    def __init__(self, log_dir: str | Path | None = None, keep_sessions: int = 10):
        self._lock = RLock()
        self._events: list[DiagnosticEvent] = []
        self._telemetry = TelemetrySnapshot()
        self.development_mode = False
        self.capture_diagnostic_payloads = False
        self.keep_sessions = max(1, int(keep_sessions))
        self.log_dir = Path(log_dir) if log_dir else None
        self._session_path: Path | None = None
        if self.log_dir:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self._rotate_sessions()
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                self._session_path = self.log_dir / f"hcs-session-{stamp}.jsonl"
            except OSError:
                self.log_dir = None
                self._session_path = None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _rotate_sessions(self) -> None:
        if not self.log_dir:
            return
        files = sorted(self.log_dir.glob("hcs-session-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[self.keep_sessions - 1 :]:
            try:
                old.unlink()
            except OSError:
                pass

    def emit(
        self,
        severity: str,
        subsystem: str,
        operation: str,
        message: str,
        **kwargs: Any,
    ) -> DiagnosticEvent:
        severity = severity.upper()
        payload = kwargs.pop("diagnostic_payload", {}) or {}
        if not self.capture_diagnostic_payloads:
            payload = {}
        if severity == "DEBUG" and not self.development_mode:
            return DiagnosticEvent(
                timestamp=self._now(),
                severity=severity,
                subsystem=subsystem,
                operation=operation,
                message=message,
                diagnostic_payload={},
                event_id=0,
                **kwargs,
            )
        with self._lock:
            event = DiagnosticEvent(
                timestamp=self._now(),
                severity=severity,
                subsystem=subsystem,
                operation=operation,
                message=message,
                diagnostic_payload=payload,
                event_id=len(self._events) + 1,
                **kwargs,
            )
            self._events.append(event)
            self._telemetry.active_subsystem = subsystem
            self._telemetry.active_operation = operation
            if event.http_status is not None:
                self._telemetry.last_http_status = event.http_status
            if event.elapsed_seconds is not None:
                self._telemetry.last_response_seconds = event.elapsed_seconds
            if event.output_tokens_per_second is not None:
                self._telemetry.output_tokens_per_second = event.output_tokens_per_second
            if event.prompt_tokens_per_second is not None:
                self._telemetry.prompt_tokens_per_second = event.prompt_tokens_per_second
            if event.model:
                self._telemetry.active_model = event.model
            if severity == "ERROR":
                self._telemetry.last_error = message
            self._persist(event)
            return event

    def _persist(self, event: DiagnosticEvent) -> None:
        if not self._session_path:
            return
        try:
            with self._session_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(self.event_dict(event, include_payloads=True), ensure_ascii=False) + "\n")
        except OSError:
            pass

    def events(
        self,
        severities: Iterable[str] | None = None,
        subsystem: str | None = None,
        search: str | None = None,
    ) -> list[DiagnosticEvent]:
        sev = {s.upper() for s in severities} if severities else None
        needle = (search or "").casefold()
        with self._lock:
            result = list(self._events)
        if sev:
            result = [e for e in result if e.severity in sev]
        if subsystem:
            result = [e for e in result if e.subsystem == subsystem]
        if needle:
            result = [e for e in result if needle in f"{e.subsystem} {e.operation} {e.message}".casefold()]
        return result

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def update_telemetry(self, **changes: Any) -> TelemetrySnapshot:
        with self._lock:
            for key, value in changes.items():
                if hasattr(self._telemetry, key):
                    setattr(self._telemetry, key, value)
            return TelemetrySnapshot(**asdict(self._telemetry))

    def telemetry(self) -> TelemetrySnapshot:
        with self._lock:
            return TelemetrySnapshot(**asdict(self._telemetry))

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: "<redacted>" if _SECRET_KEY_RE.search(str(k)) else cls._redact(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(v) for v in value]
        return value

    @classmethod
    def event_dict(cls, event: DiagnosticEvent, include_payloads: bool = False) -> dict[str, Any]:
        data = asdict(event)
        data["context"] = cls._redact(data.get("context", {}))
        if include_payloads:
            data["diagnostic_payload"] = cls._redact(data.get("diagnostic_payload", {}))
        else:
            data.pop("diagnostic_payload", None)
        return data

    def export(
        self,
        path: str | Path,
        events: Iterable[DiagnosticEvent] | None = None,
        include_payloads: bool = False,
    ) -> Path:
        path = Path(path)
        selected = list(events if events is not None else self.events())
        if path.suffix.lower() == ".jsonl":
            text = "\n".join(json.dumps(self.event_dict(e, include_payloads), ensure_ascii=False) for e in selected)
        else:
            lines: list[str] = []
            for e in selected:
                line = f"{e.timestamp} {e.severity:<7} {e.subsystem}.{e.operation}: {e.message}"
                if e.elapsed_seconds is not None:
                    line += f" ({e.elapsed_seconds:.3f}s)"
                if e.http_status is not None:
                    line += f" [HTTP {e.http_status}]"
                lines.append(line)
                if e.exception:
                    lines.append(e.exception)
                if include_payloads and e.diagnostic_payload:
                    lines.append(json.dumps(self._redact(e.diagnostic_payload), ensure_ascii=False, indent=2))
            text = "\n".join(lines)
        path.write_text(text + ("\n" if text else ""), encoding="utf-8")
        return path


_default_service: DiagnosticsService | None = None


def get_diagnostics(log_dir: str | Path | None = None) -> DiagnosticsService:
    global _default_service
    if _default_service is None:
        _default_service = DiagnosticsService(log_dir=log_dir)
    return _default_service
