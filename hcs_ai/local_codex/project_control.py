from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error, request
from urllib.parse import urlparse


class ProjectControlError(RuntimeError):
    """Raised when a project control endpoint cannot be used safely."""


@dataclass(frozen=True)
class ProjectScreenshot:
    image_bytes: bytes
    media_type: str


class ProjectControlClient:
    """Client for a project's deterministic control/observation API."""

    def __init__(
        self,
        base_url: str,
        *,
        opener: Callable[..., Any] = request.urlopen,
        timeout: float = 10.0,
        discovery_path: Path | None = None,
    ) -> None:
        value = base_url.strip().rstrip("/")
        if not value:
            raise ValueError("base_url is required")
        self.base_url = value
        self.opener = opener
        self.timeout = timeout
        self.discovery_path = Path(discovery_path) if discovery_path is not None else None

    def _discovered_base_url(self) -> str | None:
        path = self.discovery_path
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload.get("url")
        except (OSError, json.JSONDecodeError, AttributeError):
            return None
        if not isinstance(value, str):
            return None
        value = value.strip().rstrip("/")
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or port is None
        ):
            return None
        return value

    def _url(self, path: str) -> str:
        base_url = self._discovered_base_url() or self.base_url
        return f"{base_url}{path}"

    @staticmethod
    def _content_type(response) -> str:
        headers = getattr(response, "headers", {}) or {}
        if hasattr(headers, "get_content_type"):
            return headers.get_content_type()
        value = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
        return value.split(";", 1)[0].strip().lower()

    def _open(self, req: request.Request):
        try:
            return self.opener(req, timeout=self.timeout)
        except (error.URLError, TimeoutError, OSError) as exc:
            raise ProjectControlError(f"project control request failed: {exc}") from exc

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(self._url(path), data=data, headers=headers, method=method)
        with self._open(req) as response:
            body = response.read()
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectControlError(f"invalid JSON from {path}") from exc
        if not isinstance(decoded, dict):
            raise ProjectControlError(f"expected JSON object from {path}")
        return decoded

    def get_state(self) -> dict[str, Any]:
        return self._request_json("/v1/state")

    def get_ui_state(self) -> dict[str, Any]:
        return self._request_json("/v1/ui")

    def send_command(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = command.strip()
        if not name:
            raise ValueError("command is required")
        return self._request_json(
            "/v1/command",
            method="POST",
            payload={"command": name, "arguments": arguments or {}},
        )

    def get_screenshot(self) -> ProjectScreenshot:
        req = request.Request(
            self._url("/v1/screenshot"),
            headers={"Accept": "image/*, application/json"},
            method="GET",
        )
        with self._open(req) as response:
            body = response.read()
            media_type = self._content_type(response)

        if media_type.startswith("image/"):
            return ProjectScreenshot(image_bytes=body, media_type=media_type)

        try:
            decoded = json.loads(body.decode("utf-8"))
            encoded = decoded["image_base64"]
            decoded_media_type = decoded.get("media_type", "image/png")
            image_bytes = base64.b64decode(encoded, validate=True)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ProjectControlError("invalid screenshot response") from exc
        return ProjectScreenshot(image_bytes=image_bytes, media_type=decoded_media_type)
