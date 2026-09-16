import json
import threading

import pytest
from http.server import BaseHTTPRequestHandler, HTTPServer

from hcs_ai.local_codex.lm_client import LMStudioClient, LMStudioError


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {"data": [{"id": "qwen2.5-3b-instruct"}]}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = {"choices": [{"message": {"content": '{"action":"git_status"}'}}]}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_server():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_model_available():
    server = start_server()
    try:
        client = LMStudioClient(
            f"http://127.0.0.1:{server.server_port}",
            "qwen2.5-3b-instruct",
        )
        assert client.model_available()
    finally:
        server.shutdown()


def test_chat_returns_content():
    server = start_server()
    try:
        client = LMStudioClient(
            f"http://127.0.0.1:{server.server_port}",
            "qwen2.5-3b-instruct",
        )
        assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_status"}'
    finally:
        server.shutdown()

class CaptureHandler(BaseHTTPRequestHandler):
    last_payload = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        CaptureHandler.last_payload = json.loads(self.rfile.read(length).decode("utf-8"))
        payload = {"choices": [{"message": {"content": '{"action":"list_files"}'}}]}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_chat_requests_structured_action_schema():
    server = HTTPServer(("127.0.0.1", 0), CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = LMStudioClient(
            f"http://127.0.0.1:{server.server_port}",
            "qwen2.5-3b-instruct",
        )
        client.chat([{"role": "user", "content": "inspect repo"}])
        response_format = CaptureHandler.last_payload["response_format"]
        assert response_format["type"] == "json_schema"
        schema = response_format["json_schema"]["schema"]
        assert schema["required"] == ["action"]
        assert "list_files" in schema["properties"]["action"]["enum"]
        assert schema["additionalProperties"] is False
    finally:
        server.shutdown()


def test_default_timeout_allows_slow_local_inference():
    client = LMStudioClient("http://127.0.0.1:1234", "model")
    assert client.timeout == 600.0


class BadRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = b'{"error":{"message":"context length exceeded"}}'
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_http_error_includes_lm_studio_response_body():
    server = HTTPServer(("127.0.0.1", 0), BadRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = LMStudioClient(
            f"http://127.0.0.1:{server.server_port}",
            "test-model",
        )
        try:
            client.chat([{"role": "user", "content": "hello"}])
        except LMStudioError as exc:
            assert "HTTP 400" in str(exc)
            assert "context length exceeded" in str(exc)
        else:
            raise AssertionError("expected LMStudioError")
    finally:
        server.shutdown()


@pytest.mark.parametrize("content", [None, "", "   "])
def test_chat_rejects_null_or_blank_content(content):
    client = LMStudioClient("http://127.0.0.1:1", "test-model")
    client._request_json = lambda *args, **kwargs: {
        "choices": [{"message": {"content": content}}]
    }

    with pytest.raises(LMStudioError, match="invalid chat-completion response"):
        client.chat([{"role": "user", "content": "hello"}])
