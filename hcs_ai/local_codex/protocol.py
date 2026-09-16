"""Versioned, safe NDJSON primitives for the HCS Local Codex worker boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any, Callable, Mapping
from types import MappingProxyType


PROTOCOL_VERSION = 1

_REDACTED = "[REDACTED]"
_UNSUPPORTED = "[UNSUPPORTED]"
_SECRET_KEY_RE = re.compile(
    r"authorization|api[_-]?key|secret|password|credential|^token$|"
    r"(?:^|[_-])(?:access|refresh|auth|bearer)[_-]?token$",
    re.IGNORECASE,
)
_BEARER_TOKEN_RE = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)
_COMMAND_TYPES = frozenset(
    {"submit_task", "resume_task", "stop_task", "approve", "status", "shutdown"}
)
_EVENT_TYPES = frozenset(
    {
        "action_result",
        "action_started",
        "approval_required",
        "approval_resolved",
        "command_error",
        "heartbeat",
        "log",
        "log_warning",
        "protocol_warning",
        "provider_fallback",
        "provider_recovered",
        "provider_selected",
        "provider_wait",
        "status_snapshot",
        "task_blocked",
        "task_completed",
        "task_failed",
        "task_resumed",
        "task_started",
        "task_stopping",
        "test_result",
        "worker_crashed",
        "worker_heartbeat",
        "worker_started",
        "worker_stopped",
        "worker_stopping",
    }
)
_EVENT_LEVELS = frozenset({"debug", "info", "warning", "error", "critical", "success"})


class ProtocolError(ValueError):
    """Raised when data cannot safely cross the worker protocol boundary."""


def _normalized_secret_names(secret_names: tuple[str, ...]) -> set[str]:
    return {name.casefold() for name in secret_names if isinstance(name, str) and name}


def _redact_text(value: str, secret_values: tuple[str, ...]) -> str:
    result = value
    for secret in sorted(
        {item for item in secret_values if isinstance(item, str) and item}, key=len, reverse=True
    ):
        result = result.replace(secret, _REDACTED)
    result = _BEARER_TOKEN_RE.sub(f"Bearer {_REDACTED}", result)
    try:
        result.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return _UNSUPPORTED
    return result


def redact(value: Any, *, secret_names: tuple[str, ...] = (), secret_values: tuple[str, ...] = ()) -> Any:
    """Return JSON-safe data without known credential names, values, or bearer tokens.

    Unknown objects deliberately become a marker instead of ``str(value)``: object
    representations can include credentials and are not a safe logging fallback.
    A mapping marked ``sensitive: true`` is an opaque protected section: replace
    all its keys/content, including prompt text, with an idempotent safe marker.
    """
    names = _normalized_secret_names(secret_names)

    def safe(item: Any) -> Any:
        if isinstance(item, Mapping):
            if item.get("sensitive") is True:
                return {"sensitive": True, "message": _REDACTED}
            result: dict[str, Any] = {}
            for raw_key, raw_value in item.items():
                raw_key_text = raw_key if isinstance(raw_key, str) else _UNSUPPORTED
                key = _redact_text(raw_key_text, secret_values)
                if raw_key_text.casefold() in names or _SECRET_KEY_RE.search(raw_key_text):
                    result[key] = _REDACTED
                else:
                    result[key] = safe(raw_value)
            return result
        if isinstance(item, (list, tuple)):
            return [safe(element) for element in item]
        if isinstance(item, str):
            return _redact_text(item, secret_values)
        if item is None or isinstance(item, bool) or isinstance(item, int):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else _UNSUPPORTED
        return _UNSUPPORTED

    return safe(value)


def _freeze_json(value: Any) -> Any:
    """Freeze a sanitized JSON tree so event data cannot change after emission."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _is_json_safe(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_safe(item) for key, item in value.items())
    return False


def _require_nonempty_string(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{field_name} must be a nonempty string")


def _validate_command_payload(command_type: str, payload: dict[str, Any]) -> None:
    expected_fields = {
        "submit_task": {"workspace_id", "prompt"},
        "resume_task": {"workspace_id"},
        "stop_task": set(),
        "approve": {"request_id", "approved"},
        "status": set(),
        "shutdown": set(),
    }[command_type]
    actual_fields = set(payload)
    if actual_fields != expected_fields:
        missing = expected_fields - actual_fields
        unexpected = actual_fields - expected_fields
        if missing:
            raise ProtocolError(
                f"{command_type} payload missing required field: {sorted(missing)[0]}"
            )
        raise ProtocolError(
            f"{command_type} payload contains unexpected field: {sorted(unexpected)[0]}"
        )
    if command_type == "submit_task":
        _require_nonempty_string(payload["workspace_id"], "workspace_id")
        _require_nonempty_string(payload["prompt"], "prompt")
    elif command_type == "resume_task":
        _require_nonempty_string(payload["workspace_id"], "workspace_id")
    elif command_type == "approve":
        _require_nonempty_string(payload["request_id"], "request_id")
        if type(payload["approved"]) is not bool:
            raise ProtocolError("approved must be a boolean")


@dataclass(frozen=True)
class Command:
    protocol_version: int
    command_id: str
    type: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol_version: {self.protocol_version!r}")
        _require_nonempty_string(self.command_id, "command_id")
        if not isinstance(self.type, str) or self.type not in _COMMAND_TYPES:
            raise ProtocolError(f"unknown command type: {self.type!r}")
        if not isinstance(self.payload, dict):
            raise ProtocolError("payload must be an object")
        if not _is_json_safe(self.payload):
            raise ProtocolError("payload must contain JSON-safe data")
        _validate_command_payload(self.type, self.payload)

    @classmethod
    def from_json(cls, line: str) -> "Command":
        if not isinstance(line, str):
            raise ProtocolError("command line must be JSON text")
        try:
            raw = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProtocolError("invalid command JSON") from exc
        if not isinstance(raw, dict):
            raise ProtocolError("command envelope must be an object")
        required_fields = {"protocol_version", "command_id", "type", "payload"}
        if set(raw) != required_fields:
            missing = required_fields - set(raw)
            unexpected = set(raw) - required_fields
            if missing:
                raise ProtocolError(f"command missing required field: {sorted(missing)[0]}")
            raise ProtocolError(f"command contains unexpected field: {sorted(unexpected)[0]}")
        return cls(**raw)


@dataclass(frozen=True)
class Event:
    protocol_version: int
    sequence: int
    timestamp: float
    type: str
    level: str = "info"
    payload: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol_version: {self.protocol_version!r}")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ProtocolError("sequence must be a positive integer")
        if isinstance(self.timestamp, bool) or not isinstance(self.timestamp, (int, float)):
            raise ProtocolError("timestamp must be a finite number")
        if not math.isfinite(float(self.timestamp)):
            raise ProtocolError("timestamp must be a finite number")
        _require_nonempty_string(self.type, "event type")
        _require_nonempty_string(self.level, "level")
        if self.type not in _EVENT_TYPES:
            raise ProtocolError("unknown event type")
        if self.level not in _EVENT_LEVELS:
            raise ProtocolError("unknown event level")
        object.__setattr__(
            self,
            "type",
            _redact_text(self.type, ()),
        )
        object.__setattr__(
            self,
            "level",
            _redact_text(self.level, ()),
        )
        object.__setattr__(
            self,
            "payload",
            _freeze_json(
                redact(
                    {} if self.payload is None else self.payload,
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "type": _redact_text(self.type, ()),
            "level": _redact_text(self.level, ()),
            "payload": redact(self.payload),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class EventFactory:
    """Create ordered events safely from concurrent worker threads."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        secret_names: tuple[str, ...] = (),
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self._clock = clock
        self._secret_names = secret_names
        self._secret_values = secret_values
        self._sequence = 0
        self._lock = Lock()

    def emit(self, type: str, level: str = "info", payload: Any = None) -> Event:
        with self._lock:
            sequence = self._sequence + 1
            event = Event(
                protocol_version=PROTOCOL_VERSION,
                sequence=sequence,
                timestamp=float(self._clock()),
                type=redact(type, secret_values=self._secret_values),
                level=redact(level, secret_values=self._secret_values),
                payload=redact(
                    {} if payload is None else payload,
                    secret_names=self._secret_names,
                    secret_values=self._secret_values,
                ),
            )
            self._sequence = sequence
            return event


class EventLog:
    """Append safe protocol events to a bounded NDJSON file.

    ``append`` returns the persisted event, or a replacement warning event when
    disk I/O is unavailable.  Reusing the original sequence lets callers publish
    one event per emission while preserving monotonic ordering.
    """

    def __init__(self, path: str | Path, *, max_bytes: int = 2_000_000, backups: int = 5) -> None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if type(backups) is not int or backups < 0:
            raise ValueError("backups must be a non-negative integer")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = Lock()

    def _rotate(self) -> None:
        if self.backups == 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    @staticmethod
    def _warning(event: Event) -> Event:
        return Event(
            protocol_version=event.protocol_version,
            sequence=event.sequence,
            timestamp=event.timestamp,
            type="log_warning",
            level="warning",
            payload={"message": "Unable to persist Local Codex event log"},
        )

    def append(self, event: Event) -> Event:
        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        if type(event) is not Event:
            return self._warning(event)
        try:
            with self._lock:
                encoded = (event.to_json() + "\n").encode("utf-8", "strict")
                if len(encoded) > self.max_bytes:
                    return self._warning(event)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(encoded) > self.max_bytes:
                    self._rotate()
                with self.path.open("ab") as stream:
                    stream.write(encoded)
        except (OSError, UnicodeError, TypeError, ValueError):
            return self._warning(event)
        return event
