from __future__ import annotations

import json
from urllib import error, request


class LMStudioError(RuntimeError):
    pass


def agent_action_schema() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "agent_action",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_files",
                            "read_file",
                            "search_text",
                            "write_file",
                            "replace_text",
                            "run_command",
                            "git_status",
                            "git_diff",
                            "project_state",
                            "project_ui",
                            "project_command",
                            "finish",
                        ],
                    },
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "text": {"type": "string"},
                    "content": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "command": {"type": "string"},
                    "arguments": {"type": "object"},
                    "summary": {"type": "string"},
                    "tests": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    }


class LMStudioClient:
    def __init__(self, base_url: str, model: str, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _request_json(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                detail = exc.read(2000).decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            message = f"HTTP {exc.code}: {exc.reason}"
            if detail:
                message = f"{message}: {detail}"
            raise LMStudioError(message) from exc
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LMStudioError(str(exc)) from exc

    def list_models(self) -> list[str]:
        payload = self._request_json("/v1/models")
        return [item["id"] for item in payload.get("data", [])]

    def model_available(self) -> bool:
        return self.model in self.list_models()

    def chat(self, messages: list[dict[str, str]]) -> str:
        action_schema = agent_action_schema()
        payload = self._request_json(
            "/v1/chat/completions",
            method="POST",
            payload={
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
                "response_format": action_schema,
            },
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LMStudioError("invalid chat-completion response") from exc
        if not isinstance(content, str) or not content.strip():
            raise LMStudioError("invalid chat-completion response")
        return content
