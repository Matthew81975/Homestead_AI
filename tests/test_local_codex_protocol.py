import json

import pytest

from hcs_ai.local_codex.protocol import (
    PROTOCOL_VERSION,
    Command,
    Event,
    EventFactory,
    EventLog,
    ProtocolError,
    redact,
)


def test_event_factory_emits_monotonic_sequence_and_required_envelope():
    """A missing shared sequence counter would let the GUI reorder worker events."""
    factory = EventFactory(clock=lambda: 1000.0)

    first = factory.emit("worker_started")
    second = factory.emit("log", payload={"message": "ready"})

    assert first.sequence == 1
    assert second.sequence == 2
    assert json.loads(second.to_json()) == {
        "protocol_version": 1,
        "sequence": 2,
        "timestamp": 1000.0,
        "type": "log",
        "level": "info",
        "payload": {"message": "ready"},
    }


def test_command_rejects_unknown_version_and_type():
    """Unsupported commands must never reach the worker executor."""
    with pytest.raises(ProtocolError, match="protocol_version"):
        Command.from_json(
            '{"protocol_version":2,"command_id":"c1","type":"status","payload":{}}'
        )
    with pytest.raises(ProtocolError, match="unknown command"):
        Command.from_json(
            '{"protocol_version":1,"command_id":"c1","type":"explode","payload":{}}'
        )


def test_redaction_removes_nested_secret_names_values_and_bearer_tokens():
    """Nested event payloads must not copy credentials into persistent logs."""
    value = {"password": "hunter2", "nested": ["Bearer abc123", "safe abc123 text"]}

    assert redact(value, secret_values=("abc123",)) == {
        "password": "[REDACTED]",
        "nested": ["Bearer [REDACTED]", "safe [REDACTED] text"],
    }


def test_sensitive_sections_redact_all_content_recursively_without_mutating_input():
    source = {"sections": [{"sensitive": True, "prompt": "private prompt", "private key": "private value"}],
              "public": {"sensitive": False, "message": "visible"}}
    safe = redact(source)
    assert safe == {"sections": [{"sensitive": True, "message": "[REDACTED]"}],
                    "public": {"sensitive": False, "message": "visible"}}
    assert source["sections"][0]["prompt"] == "private prompt"
    assert redact(safe) == safe


@pytest.mark.parametrize(
    ("command_type", "payload", "message"),
    [
        ("submit_task", {"workspace_id": "one"}, "prompt"),
        ("resume_task", {}, "workspace_id"),
        ("approve", {"request_id": "r1", "approved": "yes"}, "approved"),
        ("status", {"extra": True}, "payload"),
        ("shutdown", {"extra": True}, "payload"),
    ],
)
def test_command_rejects_missing_or_unexpected_type_specific_fields(command_type, payload, message):
    """A malformed command must be rejected before it is dispatched."""
    line = json.dumps(
        {
            "protocol_version": PROTOCOL_VERSION,
            "command_id": "c1",
            "type": command_type,
            "payload": payload,
        }
    )

    with pytest.raises(ProtocolError, match=message):
        Command.from_json(line)


def test_command_requires_exact_integer_version_and_nonempty_identifier():
    """Boolean versions and blank IDs must not masquerade as valid commands."""
    with pytest.raises(ProtocolError, match="protocol_version"):
        Command.from_json(
            '{"protocol_version":true,"command_id":"c1","type":"status","payload":{}}'
        )
    with pytest.raises(ProtocolError, match="command_id"):
        Command.from_json(
            '{"protocol_version":1,"command_id":" ","type":"status","payload":{}}'
        )


def test_command_rejects_non_string_type_as_a_protocol_error():
    """Malformed JSON types must not escape validation as implementation errors."""
    with pytest.raises(ProtocolError, match="unknown command"):
        Command.from_json(
            '{"protocol_version":1,"command_id":"c1","type":[],"payload":{}}'
        )


def test_event_redacts_payload_and_replaces_non_json_values():
    """An event cannot leak a secret through direct dataclass construction."""
    event = Event(
        protocol_version=1,
        sequence=1,
        timestamp=1000.0,
        type="log",
        level="info",
        payload={"api_key": "key-123", "object": object()},
    )

    assert json.loads(event.to_json())["payload"] == {
        "api_key": "[REDACTED]",
        "object": "[UNSUPPORTED]",
    }


def test_event_factory_keeps_configured_secrets_out_of_envelope_nested_keys_and_mutations():
    """An emitted event must be an immutable safe snapshot, not a mutable payload view."""
    secret = "secret-value-123"
    source = {"nested": {secret: secret, "named-secret": secret}}
    factory = EventFactory(
        clock=lambda: 1000.0,
        secret_names=("named-secret",),
        secret_values=(secret,),
    )

    with pytest.raises(ProtocolError, match="event type"):
        factory.emit(f"provider {secret}", level=f"level {secret}", payload=source)

    event = factory.emit("log", payload=source)
    source["nested"]["later"] = secret

    with pytest.raises(TypeError):
        event.payload["later"] = secret

    serialized = event.to_json()
    assert secret not in serialized
    assert json.loads(serialized) == {
        "protocol_version": 1,
        "sequence": 1,
        "timestamp": 1000.0,
        "type": "log",
        "level": "info",
        "payload": {
            "nested": {"[REDACTED]": "[REDACTED]", "named-secret": "[REDACTED]"}
        },
    }


def test_event_sanitizes_unpaired_unicode_before_ndjson_persistence(tmp_path):
    """An invalid Unicode code point cannot escape as malformed UTF-8 NDJSON."""
    event = EventFactory(clock=lambda: 1.0).emit("log", payload={"message": "bad\ud800text"})

    serialized = event.to_json()
    persisted = EventLog(tmp_path / "local-codex.log").append(event)

    assert "\ud800" not in serialized
    assert serialized.encode("utf-8")
    assert persisted == event


def test_event_rejects_unconstrained_envelope_strings():
    """Only protocol-defined event types and levels may cross the NDJSON boundary."""
    with pytest.raises(ProtocolError, match="event type") as event_type_error:
        Event(
            protocol_version=1,
            sequence=1,
            timestamp=1.0,
            type="secret-value-123",
            payload={},
        )
    assert "secret-value-123" not in str(event_type_error.value)
    with pytest.raises(ProtocolError, match="level") as level_error:
        Event(
            protocol_version=1,
            sequence=1,
            timestamp=1.0,
            type="log",
            level="secret-value-123",
            payload={},
        )
    assert "secret-value-123" not in str(level_error.value)


def test_event_log_returns_warning_without_writing_oversized_record(tmp_path):
    """A single oversized event must not break the configured log-size bound."""
    path = tmp_path / "local-codex.log"
    event = EventFactory(clock=lambda: 1.0).emit("log", payload={"message": "x" * 500})

    result = EventLog(path, max_bytes=100).append(event)

    assert result.type == "log_warning"
    assert result.level == "warning"
    assert result.sequence == event.sequence
    assert not path.exists()


def test_event_log_rejects_subclass_serialization_that_would_leak_a_secret(tmp_path):
    """Subclass overrides must not bypass the exact Event serialization policy."""
    secret = "secret-value-123"

    class SecretSerializingEvent(Event):
        def to_json(self):
            return '{"payload":{"message":"' + secret + '"}}'

    event = SecretSerializingEvent(
        protocol_version=1,
        sequence=1,
        timestamp=1.0,
        type="log",
        payload={},
    )
    path = tmp_path / "local-codex.log"

    result = EventLog(path).append(event)

    assert result.type == "log_warning"
    assert not path.exists()


def test_event_log_rejects_subclass_serialization_that_raises_runtime_error(tmp_path):
    """An overriding serializer cannot crash the caller instead of returning a warning."""
    class ExplodingEvent(Event):
        def to_json(self):
            raise RuntimeError("secret-value-123")

    event = ExplodingEvent(
        protocol_version=1,
        sequence=1,
        timestamp=1.0,
        type="log",
        payload={},
    )

    result = EventLog(tmp_path / "local-codex.log").append(event)

    assert result.type == "log_warning"


def test_event_log_rotates_before_append_and_persists_redacted_ndjson(tmp_path):
    """A full log must rotate before adding the next safe, complete NDJSON event."""
    path = tmp_path / "local-codex.log"
    event = EventFactory(clock=lambda: 1.0).emit("log", payload={"token": "secret"})
    encoded_size = len((event.to_json() + "\n").encode("utf-8"))
    path.write_text("old-entry\n", encoding="utf-8")
    log = EventLog(path, max_bytes=encoded_size, backups=2)

    persisted = log.append(event)

    assert persisted == event
    assert path.with_name("local-codex.log.1").read_text(encoding="utf-8") == "old-entry\n"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "protocol_version": 1,
        "sequence": 1,
        "timestamp": 1.0,
        "type": "log",
        "level": "info",
        "payload": {"token": "[REDACTED]"},
    }


def test_event_log_returns_safe_warning_event_when_persistence_fails(tmp_path):
    """A log-directory failure must surface safely without crashing the caller."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")
    event = EventFactory(clock=lambda: 5.0).emit("log", payload={"password": "hunter2"})

    result = EventLog(blocker / "local-codex.log").append(event)

    assert result.type == "log_warning"
    assert result.level == "warning"
    assert result.sequence == event.sequence
    assert result.payload == {"message": "Unable to persist Local Codex event log"}
