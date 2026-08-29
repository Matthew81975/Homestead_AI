from __future__ import annotations

from dataclasses import dataclass
import os

import httpx


@dataclass
class ProviderFailure(Exception):
    kind: str
    message: str
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        return self.message


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error)
        return str(error)
    except Exception:
        return response.text.strip() or f"HTTP {response.status_code}"


def classify_http_failure(response: httpx.Response) -> ProviderFailure:
    status = int(response.status_code)
    if status == 429:
        kind = "rate_limit"
    elif status in (401, 403):
        kind = "auth"
    elif 500 <= status <= 599:
        kind = "capacity"
    else:
        kind = "request"
    return ProviderFailure(kind, _error_message(response), _retry_after(response))


class OpenAICompatibleProvider:
    def __init__(self, provider: dict):
        self.provider = dict(provider)

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        key_name = str(self.provider.get("api_key_env") or "").strip()
        api_key = os.environ.get(key_name, "") if key_name else ""
        if not api_key:
            raise ProviderFailure("auth", f"API key environment variable is not set: {key_name}")

        base_url = str(self.provider["base_url"]).rstrip("/")
        model = str(self.provider["model"])
        payload = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = float(self.provider.get("timeout_seconds", 120))
        try:
            response = httpx.post(
                base_url + "/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderFailure("timeout", "Cloud provider timed out.") from exc
        except httpx.RequestError as exc:
            raise ProviderFailure("connectivity", str(exc)) from exc

        if not response.is_success:
            raise classify_http_failure(response)

        body = response.json()
        message = body["choices"][0]["message"]
        return {
            "text": message.get("content", ""),
            "message": message,
            "usage": body.get("usage") or {},
            "tool_calls": message.get("tool_calls") or [],
            "provider": self.provider.get("provider") or self.provider["id"],
            "model": model,
            "raw": body,
        }
