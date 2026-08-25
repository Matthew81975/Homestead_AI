from pathlib import Path

from hcs_ai.diagnostics import DiagnosticsService


def test_event_filter_redaction_and_export(tmp_path: Path):
    service = DiagnosticsService(log_dir=tmp_path / "logs")
    service.capture_diagnostic_payloads = True
    service.emit(
        "ERROR",
        "Models",
        "load",
        "backend rejected model",
        http_status=400,
        context={"token": "secret", "safe": "value"},
        diagnostic_payload={"authorization": "Bearer nope", "body": "bad model path"},
    )

    assert len(service.events(severities=["ERROR"], subsystem="Models", search="rejected")) == 1

    normal = tmp_path / "normal.jsonl"
    service.export(normal, include_payloads=False)
    text = normal.read_text(encoding="utf-8")
    assert "diagnostic_payload" not in text
    assert "secret" not in text
    assert "<redacted>" in text

    diagnostic = tmp_path / "diagnostic.jsonl"
    service.export(diagnostic, include_payloads=True)
    text = diagnostic.read_text(encoding="utf-8")
    assert "bad model path" in text
    assert "Bearer nope" not in text


def test_debug_events_require_development_mode():
    service = DiagnosticsService()
    service.emit("DEBUG", "HTTP", "/health", "quiet debug event")
    service.emit("INFO", "System", "startup", "ordinary event")
    assert [event.severity for event in service.events()] == ["INFO"]

    service.development_mode = True
    service.emit("DEBUG", "HTTP", "/health", "visible debug event")
    assert [event.severity for event in service.events()] == ["INFO", "DEBUG"]


def test_telemetry_snapshot_updates_from_events():
    service = DiagnosticsService()
    service.emit(
        "INFO",
        "LLM",
        "complete",
        "response complete",
        elapsed_seconds=1.25,
        model="tiny.gguf",
        output_tokens_per_second=22.5,
        prompt_tokens_per_second=40.0,
        http_status=200,
    )
    snap = service.telemetry()
    assert snap.last_response_seconds == 1.25
    assert snap.active_model == "tiny.gguf"
    assert snap.output_tokens_per_second == 22.5
    assert snap.prompt_tokens_per_second == 40.0
    assert snap.last_http_status == 200


def test_rotation_keeps_at_most_requested_sessions(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for i in range(12):
        path = log_dir / f"hcs-session-20260825-1200{i:02d}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        path.touch()
    DiagnosticsService(log_dir=log_dir, keep_sessions=10)
    assert len(list(log_dir.glob("hcs-session-*.jsonl"))) <= 10
