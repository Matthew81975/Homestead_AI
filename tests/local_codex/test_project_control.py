import base64
import json
from io import BytesIO

import pytest

from hcs_ai.local_codex.project_control import ProjectControlClient, ProjectControlError


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RecordingOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def json_response(payload):
    return FakeResponse(json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})


def test_get_state_uses_versioned_state_endpoint():
    opener = RecordingOpener([json_response({"running": True, "scene": "spawn"})])
    client = ProjectControlClient("http://127.0.0.1:8765", opener=opener, timeout=7)

    result = client.get_state()

    assert result == {"running": True, "scene": "spawn"}
    request, timeout = opener.requests[0]
    assert request.full_url == "http://127.0.0.1:8765/v1/state"
    assert request.get_method() == "GET"
    assert timeout == 7


def test_get_ui_state_returns_structured_ui_tree():
    opener = RecordingOpener([json_response({"focused": "start", "controls": [{"id": "start", "enabled": True}]})])
    client = ProjectControlClient("http://localhost:8765/", opener=opener)

    result = client.get_ui_state()

    assert result["focused"] == "start"
    assert result["controls"][0]["id"] == "start"
    assert opener.requests[0][0].full_url == "http://localhost:8765/v1/ui"


def test_send_command_posts_deterministic_command_payload():
    opener = RecordingOpener([json_response({"ok": True, "result": {"room": 4}})])
    client = ProjectControlClient("http://localhost:8765", opener=opener)

    result = client.send_command("goto_room", {"room_id": 4})

    assert result == {"ok": True, "result": {"room": 4}}
    request, _ = opener.requests[0]
    assert request.full_url == "http://localhost:8765/v1/command"
    assert request.get_method() == "POST"
    assert json.loads(request.data.decode("utf-8")) == {
        "command": "goto_room",
        "arguments": {"room_id": 4},
    }
    assert request.headers["Content-type"] == "application/json"


def test_get_screenshot_accepts_binary_image_response():
    png = b"\x89PNG\r\n\x1a\nabc"
    opener = RecordingOpener([FakeResponse(png, headers={"Content-Type": "image/png"})])
    client = ProjectControlClient("http://localhost:8765", opener=opener)

    shot = client.get_screenshot()

    assert shot.image_bytes == png
    assert shot.media_type == "image/png"
    assert opener.requests[0][0].full_url == "http://localhost:8765/v1/screenshot"


def test_get_screenshot_accepts_json_base64_response():
    png = b"png-bytes"
    opener = RecordingOpener([
        json_response({"media_type": "image/png", "image_base64": base64.b64encode(png).decode("ascii")})
    ])
    client = ProjectControlClient("http://localhost:8765", opener=opener)

    shot = client.get_screenshot()

    assert shot.image_bytes == png
    assert shot.media_type == "image/png"


def test_invalid_json_is_wrapped_as_project_control_error():
    opener = RecordingOpener([FakeResponse(b"not-json", headers={"Content-Type": "application/json"})])
    client = ProjectControlClient("http://localhost:8765", opener=opener)

    with pytest.raises(ProjectControlError, match="invalid JSON"):
        client.get_state()
