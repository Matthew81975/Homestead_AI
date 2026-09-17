import json

from hcs_ai.local_codex.project_control import ProjectControlClient


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RecordingOpener:
    def __init__(self):
        self.urls = []

    def __call__(self, req, timeout):
        self.urls.append(req.full_url)
        return FakeResponse({"running": True})


def test_discovery_file_overrides_configured_url_at_request_time(tmp_path):
    discovery = tmp_path / ".maze_world_control.json"
    discovery.write_text(
        json.dumps({"url": "http://127.0.0.1:54321", "pid": 1234}),
        encoding="utf-8",
    )
    opener = RecordingOpener()
    client = ProjectControlClient(
        "http://127.0.0.1:8766",
        opener=opener,
        discovery_path=discovery,
    )

    assert client.get_state() == {"running": True}
    assert opener.urls == ["http://127.0.0.1:54321/v1/state"]


def test_non_loopback_discovery_url_is_ignored(tmp_path):
    discovery = tmp_path / ".maze_world_control.json"
    discovery.write_text(
        json.dumps({"url": "https://example.com:54321", "pid": 1234}),
        encoding="utf-8",
    )
    opener = RecordingOpener()
    client = ProjectControlClient(
        "http://127.0.0.1:8766",
        opener=opener,
        discovery_path=discovery,
    )

    client.get_state()

    assert opener.urls == ["http://127.0.0.1:8766/v1/state"]


def test_malformed_discovery_file_is_ignored(tmp_path):
    discovery = tmp_path / ".maze_world_control.json"
    discovery.write_text("not-json", encoding="utf-8")
    opener = RecordingOpener()
    client = ProjectControlClient(
        "http://127.0.0.1:8766",
        opener=opener,
        discovery_path=discovery,
    )

    client.get_state()

    assert opener.urls == ["http://127.0.0.1:8766/v1/state"]
