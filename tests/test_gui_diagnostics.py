import json
from urllib.error import HTTPError
from io import BytesIO

from hcs_ai.gui_diagnostics import concise_http_reason, format_status_text
from hcs_ai.diagnostics import TelemetrySnapshot


def test_concise_http_reason_extracts_fastapi_detail():
    body = json.dumps({"detail": "Model file could not be loaded: bad architecture"})
    assert concise_http_reason(400, body) == "Model file could not be loaded: bad architecture"


def test_concise_http_reason_falls_back_to_status_and_body():
    assert concise_http_reason(500, "plain failure") == "HTTP 500: plain failure"


def test_status_text_is_compact_when_development_mode_off():
    snap = TelemetrySnapshot(
        state="Ready",
        active_model="tiny.gguf",
        last_response_seconds=1.234,
        output_tokens_per_second=22.5,
    )
    text = format_status_text(snap, development_mode=False)
    assert "Ready" in text
    assert "tiny.gguf" in text
    assert "1.23 s" in text
    assert "22.5 tok/s" in text
    assert "HTTP" not in text


def test_status_text_expands_when_development_mode_on():
    snap = TelemetrySnapshot(
        state="Busy",
        backend_state="running",
        backend_port=8765,
        active_model="tiny.gguf",
        last_response_seconds=2.0,
        prompt_tokens_per_second=31.0,
        output_tokens_per_second=15.0,
        last_http_status=400,
        active_subsystem="Models",
        active_operation="load",
        last_error="bad model",
        diagnostic_payload_capture=True,
    )
    text = format_status_text(snap, development_mode=True)
    assert "8765" in text
    assert "HTTP 400" in text
    assert "Models.load" in text
    assert "payloads ON" in text
    assert "bad model" in text
