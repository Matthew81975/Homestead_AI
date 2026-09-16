from __future__ import annotations

import json
import os
import time
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

from . import VERSION
from .lm_client import LMStudioClient, LMStudioError, agent_action_schema
from .prompt_optimizer import PromptOptimizer


class TornadoError(LMStudioError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        category: str = "provider",
        retry_after_seconds: float | None = None,
        provider_reason: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.category = category
        self.retry_after_seconds = retry_after_seconds
        self.provider_reason = provider_reason


def _local_codex_user_agent() -> str:
    return f"LocalCodex/{VERSION}"


def _is_client_block_1010(status_code: int | None, detail: str) -> bool:
    return status_code == 403 and "1010" in str(detail).lower() and "error code" in str(detail).lower()


def _parse_retry_delay(headers, *, now: float) -> float | None:
    """Normalize common provider reset metadata into a non-negative delay."""
    if not headers:
        return None
    normalized = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    names = (
        "retry-after",
        "ratelimit-reset",
        "x-ratelimit-reset",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
    )
    for name in names:
        raw = normalized.get(name)
        if not raw:
            continue
        lower = raw.lower()
        multiplier = 1.0
        number_text = lower
        if lower.endswith("ms"):
            multiplier = 0.001
            number_text = lower[:-2]
        elif lower.endswith("s"):
            number_text = lower[:-1]
        elif lower.endswith("m"):
            multiplier = 60.0
            number_text = lower[:-1]
        elif lower.endswith("h"):
            multiplier = 3600.0
            number_text = lower[:-1]
        try:
            value = float(number_text)
        except ValueError:
            if name != "retry-after":
                continue
            try:
                parsed = parsedate_to_datetime(raw)
                value = parsed.timestamp() - now
            except (TypeError, ValueError, OverflowError):
                continue
            return max(0.0, float(value))
        if multiplier != 1.0 or any(lower.endswith(suffix) for suffix in ("ms", "s", "m", "h")):
            return max(0.0, value * multiplier)
        if value > now / 2.0:
            value -= now
        return max(0.0, value)
    return None


def _classify_failure(exc: Exception) -> TornadoError:
    """Return a TornadoError carrying the recovery category for a provider failure."""
    if isinstance(exc, TornadoError):
        if exc.category != "provider":
            return exc
        status = exc.status_code
        if _is_client_block_1010(status, str(exc)):
            exc.category = "client_block"
        elif status in (401, 403):
            exc.category = "auth"
        elif status == 429:
            exc.category = "rate_limit"
        elif status is not None and 500 <= status <= 599:
            exc.category = "transient"
        elif status == 402:
            exc.category = (
                "transient_budget"
                if exc.provider_reason == "in_flight_budget_exhausted"
                else "budget"
            )
        return exc
    text = str(exc)
    if "Engine protocol predict request failed: fetch failed" in text:
        return TornadoError(text, category="transient")
    if isinstance(exc, (error.URLError, TimeoutError, OSError)):
        return TornadoError(text, category="transient")
    return TornadoError(text, category="provider")


ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "vision": ("screenshot", "image", "visual", "vision", "ui", "screen", "vla"),
    "debugging": ("debug", "bug", "fix", "failing", "failure", "error", "traceback", "broken", "regression"),
    "review": ("review", "audit", "inspect diff", "code review", "verify", "critique"),
    "planning": ("plan", "design", "architecture", "spec", "roadmap", "brainstorm"),
    "coding": ("implement", "code", "create", "add", "refactor", "build", "modify", "write"),
}


def _task_text(messages: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            parts.append(content)
            continue
        if isinstance(payload, dict) and payload.get("task") is not None:
            parts.append(str(payload["task"]))
        else:
            parts.append(content)
    return " ".join(parts).lower()


def classify_task_role(messages: list[dict[str, str]]) -> str:
    """Classify a controller turn into one routing role using deterministic keywords."""
    text = _task_text(messages)
    if not text:
        return "general"
    scores: dict[str, int] = {}
    for role, keywords in ROLE_KEYWORDS.items():
        scores[role] = sum(1 for keyword in keywords if keyword in text)
    best_role = max(scores, key=scores.get)
    if scores[best_role] <= 0:
        return "general"
    # Deterministic precedence is insertion order above; vision/debugging outrank generic coding.
    return best_role


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    kind: str
    base_url: str
    model: str
    priority: int = 100
    timeout: float = 600.0
    local: bool = False
    api_key_env: str | None = None
    max_calls_per_session: int | None = None
    cooldown_base_seconds: float = 30.0
    roles: tuple[str, ...] = ()
    role_bonus: float = 40.0
    latency_weight: float = 0.0
    enabled: bool = True
    auto_enable_if_key_present: bool = False
    use_response_format: bool = True
    adaptive_weight: float = 30.0
    adaptive_alpha: float = 0.2
    max_tokens: int | None = None
    transient_budget_retries: int = 2
    transient_budget_default_delay_seconds: float = 2.0
    models_path: str = "/v1/models"
    chat_path: str = "/v1/chat/completions"
    model_env: str | None = None
    transient_retry_seconds: float | None = None
    rate_limit_default_retry_seconds: float | None = None
    budget_probe_seconds: float | None = None
    required_envs: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderConfig":
        return cls(
            provider_id=str(data["id"]),
            kind=str(data.get("kind", "openai_compatible")),
            base_url=os.path.expandvars(str(data["base_url"])),
            model=(
                str(os.environ.get(str(data.get("model_env")), data["model"]))
                if data.get("model_env")
                else str(data["model"])
            ),
            priority=int(data.get("priority", 100)),
            timeout=float(data.get("timeout_seconds", data.get("timeout", 600.0))),
            local=bool(data.get("local", False)),
            api_key_env=data.get("api_key_env"),
            max_calls_per_session=(
                int(data["max_calls_per_session"])
                if data.get("max_calls_per_session") is not None
                else None
            ),
            cooldown_base_seconds=float(data.get("cooldown_base_seconds", 30.0)),
            roles=tuple(str(item) for item in data.get("roles", ())),
            role_bonus=float(data.get("role_bonus", 40.0)),
            latency_weight=float(data.get("latency_weight", 0.0)),
            enabled=bool(data.get("enabled", True)),
            auto_enable_if_key_present=bool(data.get("auto_enable_if_key_present", False)),
            use_response_format=bool(data.get("use_response_format", True)),
            adaptive_weight=float(data.get("adaptive_weight", 30.0)),
            adaptive_alpha=float(data.get("adaptive_alpha", 0.2)),
            max_tokens=(
                int(data["max_tokens"])
                if data.get("max_tokens") is not None
                else (1024 if str(data.get("kind", "openai_compatible")) == "openai_compatible" else None)
            ),
            transient_budget_retries=max(0, int(data.get("transient_budget_retries", 2))),
            transient_budget_default_delay_seconds=max(
                0.0, float(data.get("transient_budget_default_delay_seconds", 2.0))
            ),
            models_path=str(data.get("models_path", "/v1/models")),
            chat_path=str(data.get("chat_path", "/v1/chat/completions")),
            model_env=(str(data["model_env"]) if data.get("model_env") else None),
            transient_retry_seconds=(
                float(data["transient_retry_seconds"])
                if data.get("transient_retry_seconds") is not None
                else None
            ),
            rate_limit_default_retry_seconds=(
                float(data["rate_limit_default_retry_seconds"])
                if data.get("rate_limit_default_retry_seconds") is not None
                else None
            ),
            budget_probe_seconds=(
                float(data["budget_probe_seconds"])
                if data.get("budget_probe_seconds") is not None
                else None
            ),
            required_envs=tuple(str(item) for item in data.get("required_envs", ())),
        )


class TornadoStateStore:
    _DEFAULTS = {
        "consecutive_failures": 0,
        "total_successes": 0,
        "total_failures": 0,
        "total_calls": 0,
        "last_error": None,
        "cooldown_until": 0.0,
        "last_latency_seconds": None,
        "role_outcomes": {},
        "last_success_at": None,
        "last_failure_at": None,
        "last_failure_category": None,
        "retry_at": 0.0,
        "budget_blocked_since": None,
        "budget_recovery_samples": [],
        "learned_budget_recovery_seconds": None,
        "learned_budget_recovery_confidence": 0.0,
    }

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"providers": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"providers": {}}
        if not isinstance(payload, dict):
            return {"providers": {}}
        payload.setdefault("providers", {})
        return payload

    def _provider(self, provider_id: str) -> dict[str, Any]:
        state = self.data.setdefault("providers", {}).setdefault(provider_id, {})
        for key, value in self._DEFAULTS.items():
            if key not in state:
                if isinstance(value, dict):
                    state[key] = dict(value)
                elif isinstance(value, list):
                    state[key] = list(value)
                else:
                    state[key] = value
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)

    def snapshot(self, provider_id: str) -> dict[str, Any]:
        return dict(self._provider(provider_id))

    def in_cooldown(self, provider_id: str, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        return float(self._provider(provider_id).get("cooldown_until", 0.0)) > now

    def retry_at(self, provider_id: str) -> float:
        return float(self._provider(provider_id).get("retry_at", 0.0) or 0.0)

    def schedule_retry(
        self,
        provider_id: str,
        *,
        category: str,
        retry_after_seconds: float,
        error_message: str | None = None,
        latency_seconds: float | None = None,
    ) -> float:
        state = self._provider(provider_id)
        now = float(self.clock())
        delay = max(0.0, float(retry_after_seconds))
        state["last_failure_at"] = now
        state["last_failure_category"] = category
        state["retry_at"] = now + delay
        if category in {"budget", "transient_budget"} and state.get("budget_blocked_since") is None:
            state["budget_blocked_since"] = now
        if error_message is not None:
            state["last_error"] = str(error_message)[:500]
        if latency_seconds is not None:
            state["last_latency_seconds"] = latency_seconds
        self.save()
        return float(state["retry_at"])

    def record_outcome(
        self,
        provider_id: str,
        task_role: str,
        success: bool,
        *,
        alpha: float = 0.2,
    ) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("adaptive alpha must be in (0, 1]")
        state = self._provider(provider_id)
        role_outcomes = state.setdefault("role_outcomes", {})
        role = role_outcomes.setdefault(
            task_role,
            {"quality": 0.5, "samples": 0, "successes": 0, "failures": 0},
        )
        observation = 1.0 if success else 0.0
        quality = float(role.get("quality", 0.5))
        role["quality"] = quality + alpha * (observation - quality)
        role["samples"] = int(role.get("samples", 0)) + 1
        if success:
            role["successes"] = int(role.get("successes", 0)) + 1
        else:
            role["failures"] = int(role.get("failures", 0)) + 1
        self.save()

    def record_success(
        self,
        provider_id: str,
        latency_seconds: float,
        *,
        budget_learning_min_samples: int = 3,
        budget_learning_alpha: float = 0.3,
    ) -> dict[str, Any]:
        if budget_learning_min_samples < 1:
            raise ValueError("budget_learning_min_samples must be at least 1")
        if not 0.0 < budget_learning_alpha <= 1.0:
            raise ValueError("budget_learning_alpha must be in (0, 1]")
        state = self._provider(provider_id)
        now = float(self.clock())
        previous_retry_at = float(state.get("retry_at", 0.0) or 0.0)
        previous_category = state.get("last_failure_category")
        recovery_elapsed = None
        blocked_since = state.get("budget_blocked_since")
        if blocked_since is not None:
            recovery_elapsed = max(0.0, now - float(blocked_since))
            samples = list(state.get("budget_recovery_samples", []))
            samples.append(recovery_elapsed)
            samples = samples[-12:]
            state["budget_recovery_samples"] = samples
            previous = state.get("learned_budget_recovery_seconds")
            learned = recovery_elapsed if previous is None else float(previous)
            learned = learned + budget_learning_alpha * (recovery_elapsed - learned)
            state["learned_budget_recovery_seconds"] = learned
            state["learned_budget_recovery_confidence"] = min(
                1.0, len(samples) / float(budget_learning_min_samples)
            )

        state["consecutive_failures"] = 0
        state["total_successes"] = int(state.get("total_successes", 0)) + 1
        state["total_calls"] = int(state.get("total_calls", 0)) + 1
        state["last_error"] = None
        state["cooldown_until"] = 0.0
        state["last_latency_seconds"] = latency_seconds
        state["last_success_at"] = now
        state["retry_at"] = 0.0
        state["last_failure_category"] = None
        state["budget_blocked_since"] = None
        self.save()
        return {
            "previous_retry_at": previous_retry_at,
            "previous_failure_category": previous_category,
            "budget_recovery_seconds": recovery_elapsed,
        }

    def record_transient(
        self,
        provider_id: str,
        error_message: str,
        *,
        latency_seconds: float | None = None,
    ) -> None:
        state = self._provider(provider_id)
        state["total_calls"] = int(state.get("total_calls", 0)) + 1
        state["last_error"] = str(error_message)[:500]
        state["last_failure_at"] = float(self.clock())
        if latency_seconds is not None:
            state["last_latency_seconds"] = latency_seconds
        self.save()

    def record_failure(
        self,
        provider_id: str,
        error_message: str,
        *,
        cooldown_base_seconds: float,
        latency_seconds: float | None = None,
        category: str = "provider",
    ) -> None:
        state = self._provider(provider_id)
        failures = int(state.get("consecutive_failures", 0)) + 1
        now = float(self.clock())
        state["consecutive_failures"] = failures
        state["total_failures"] = int(state.get("total_failures", 0)) + 1
        state["total_calls"] = int(state.get("total_calls", 0)) + 1
        state["last_error"] = str(error_message)[:500]
        state["cooldown_until"] = now + cooldown_base_seconds * (2 ** (failures - 1))
        state["last_failure_at"] = now
        state["last_failure_category"] = category
        if latency_seconds is not None:
            state["last_latency_seconds"] = latency_seconds
        self.save()


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key_env: str | None = None,
        timeout: float = 600.0,
        use_response_format: bool = True,
        max_tokens: int | None = None,
        models_path: str = "/v1/models",
        chat_path: str = "/v1/chat/completions",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.use_response_format = use_response_format
        self.max_tokens = max_tokens
        self.models_path = models_path
        self.chat_path = chat_path

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _local_codex_user_agent(),
        }
        if self.api_key_env:
            token = os.environ.get(self.api_key_env)
            if not token:
                raise TornadoError(
                    f"missing API key environment variable: {self.api_key_env}",
                    category="auth",
                )
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request_json(self, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=self._headers(),
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
            provider_reason = None
            if detail:
                try:
                    error_payload = json.loads(detail)
                except json.JSONDecodeError:
                    error_payload = None
                if isinstance(error_payload, dict):
                    error_block = error_payload.get("error")
                    if isinstance(error_block, dict):
                        metadata = error_block.get("metadata")
                        if isinstance(metadata, dict) and metadata.get("reason") is not None:
                            provider_reason = str(metadata["reason"])

            retry_after_seconds = _parse_retry_delay(exc.headers, now=time.time())

            if _is_client_block_1010(exc.code, detail):
                category = "client_block"
            elif exc.code in (401, 403):
                category = "auth"
            elif exc.code == 429:
                category = "rate_limit"
            elif 500 <= exc.code <= 599:
                category = "transient"
            elif exc.code == 402 and provider_reason == "in_flight_budget_exhausted":
                category = "transient_budget"
            elif exc.code == 402:
                category = "budget"
            else:
                category = "provider"

            message = f"HTTP {exc.code}: {exc.reason}"
            if detail:
                message += f": {detail}"
            raise TornadoError(
                message,
                status_code=exc.code,
                category=category,
                retry_after_seconds=retry_after_seconds,
                provider_reason=provider_reason,
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise TornadoError(str(exc), category="transient") from exc
        except json.JSONDecodeError as exc:
            raise TornadoError(str(exc), category="provider") from exc

    def list_models(self) -> list[str]:
        payload = self._request_json(self.models_path)
        return [item["id"] for item in payload.get("data", []) if "id" in item]

    def status_snapshot(self) -> dict[str, Any]:
        """Return read-only provider diagnostics without exposing credential values."""
        now = float(self.clock())
        providers: list[dict[str, Any]] = []
        for config, _adapter in self.providers:
            state = self.state.snapshot(config.provider_id)
            key_present = self._key_present(config) if config.api_key_env else None
            required_present = self._required_environment_present(config)
            effective_enabled = self._effectively_enabled(config)
            session_available = self._session_budget_available(config)
            cooldown_until = float(state.get("cooldown_until", 0.0) or 0.0)
            retry_at = float(state.get("retry_at", 0.0) or 0.0)
            eligible = self._eligible(config)
            if not effective_enabled:
                health_state = "disabled"
            elif not session_available:
                health_state = "session_limit"
            elif cooldown_until > now:
                health_state = "cooldown"
            elif retry_at > now:
                health_state = "waiting_retry"
            else:
                health_state = "ready"
            providers.append({
                "provider": config.provider_id,
                "model": config.model,
                "local": bool(config.local),
                "configured_enabled": bool(config.enabled),
                "auto_enable_if_key_present": bool(config.auto_enable_if_key_present),
                "api_key_env": config.api_key_env,
                "api_key_present": key_present,
                "required_envs": list(config.required_envs),
                "required_envs_present": required_present,
                "effective_enabled": effective_enabled,
                "eligible": eligible,
                "health_state": health_state,
                "session_calls": int(self.session_calls.get(config.provider_id, 0)),
                "max_calls_per_session": config.max_calls_per_session,
                "session_budget_available": session_available,
                "last_success_at": state.get("last_success_at"),
                "last_failure_at": state.get("last_failure_at"),
                "last_failure_category": state.get("last_failure_category"),
                "last_error": state.get("last_error"),
                "cooldown_until": cooldown_until,
                "cooldown_in_seconds": max(0.0, cooldown_until - now),
                "retry_at": retry_at,
                "retry_in_seconds": max(0.0, retry_at - now),
                "learned_budget_recovery_seconds": state.get("learned_budget_recovery_seconds"),
                "learned_budget_recovery_confidence": state.get("learned_budget_recovery_confidence", 0.0),
            })
        return {
            "now": now,
            "provider_count": len(providers),
            "eligible_count": sum(1 for item in providers if item["eligible"]),
            "providers": providers,
        }

    def model_available(self) -> bool:
        return self.model in self.list_models()

    def chat(self, messages: list[dict[str, str]]) -> str:
        request_payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
        }
        if self.max_tokens is not None:
            request_payload["max_tokens"] = self.max_tokens
        if self.use_response_format:
            request_payload["response_format"] = agent_action_schema()
        payload = self._request_json(
            self.chat_path,
            method="POST",
            payload=request_payload,
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TornadoError("invalid chat-completion response") from exc
        if not isinstance(content, str) or not content.strip():
            raise TornadoError("invalid chat-completion response")
        return content


class TornadoClient:
    def __init__(
        self,
        providers: list[tuple[ProviderConfig, Any]],
        *,
        state_path: Path,
        log_path: Path,
        optimizer: PromptOptimizer | None = None,
        clock=None,
        monotonic=None,
        sleeper=None,
        status_callback=None,
        wait_for_providers: bool = True,
        max_wait_poll_seconds: float = 300.0,
        transient_retry_seconds: float = 5.0,
        rate_limit_default_retry_seconds: float = 60.0,
        budget_probe_seconds: float = 1800.0,
        budget_learning_min_samples: int = 3,
        budget_learning_alpha: float = 0.3,
        local_fallback_before_wait: bool = True,
    ):
        if not providers:
            raise ValueError("Tornado requires at least one provider")
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.status_callback = status_callback
        self.wait_for_providers = bool(wait_for_providers)
        self.max_wait_poll_seconds = max(0.001, float(max_wait_poll_seconds))
        self.transient_retry_seconds = max(0.0, float(transient_retry_seconds))
        self.rate_limit_default_retry_seconds = max(0.0, float(rate_limit_default_retry_seconds))
        self.budget_probe_seconds = max(0.0, float(budget_probe_seconds))
        self.budget_learning_min_samples = max(1, int(budget_learning_min_samples))
        self.budget_learning_alpha = float(budget_learning_alpha)
        self.local_fallback_before_wait = bool(local_fallback_before_wait)
        if not 0.0 < self.budget_learning_alpha <= 1.0:
            raise ValueError("budget_learning_alpha must be in (0, 1]")

        self.providers = list(providers)
        self.provider_ids = [config.provider_id for config, _ in providers]
        self.state = TornadoStateStore(Path(state_path), clock=self.clock)
        self.log_path = Path(log_path)
        self.optimizer = optimizer or PromptOptimizer()
        self.session_calls = {config.provider_id: 0 for config, _ in providers}
        self.model = ",".join(config.model for config, _ in providers)
        self._last_route: tuple[str, str] | None = None
        self._log_provider_discovery()

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        status_callback=None,
        clock=None,
        monotonic=None,
        sleeper=None,
    ) -> "TornadoClient":
        tornado = config["tornado"]
        providers: list[tuple[ProviderConfig, Any]] = []
        for raw in tornado.get("providers", []):
            provider_config = ProviderConfig.from_dict(raw)
            if provider_config.kind == "lm_studio":
                adapter = LMStudioClient(
                    provider_config.base_url,
                    provider_config.model,
                    timeout=provider_config.timeout,
                )
            elif provider_config.kind == "openai_compatible":
                adapter = OpenAICompatibleClient(
                    provider_config.base_url,
                    provider_config.model,
                    api_key_env=provider_config.api_key_env,
                    timeout=provider_config.timeout,
                    use_response_format=provider_config.use_response_format,
                    max_tokens=provider_config.max_tokens,
                    models_path=provider_config.models_path,
                    chat_path=provider_config.chat_path,
                )
            else:
                raise ValueError(f"unsupported Tornado provider kind: {provider_config.kind}")
            providers.append((provider_config, adapter))
        return cls(
            providers,
            state_path=Path(tornado.get("state_path", "state/tornado_state.json")),
            log_path=Path(tornado.get("log_path", "logs/tornado.log")),
            status_callback=status_callback,
            clock=clock,
            monotonic=monotonic,
            sleeper=sleeper,
            wait_for_providers=bool(tornado.get("wait_for_providers", True)),
            max_wait_poll_seconds=float(tornado.get("max_wait_poll_seconds", 300)),
            transient_retry_seconds=float(tornado.get("transient_retry_seconds", 5)),
            rate_limit_default_retry_seconds=float(
                tornado.get("rate_limit_default_retry_seconds", 60)
            ),
            budget_probe_seconds=float(tornado.get("budget_probe_seconds", 1800)),
            budget_learning_min_samples=int(tornado.get("budget_learning_min_samples", 3)),
            budget_learning_alpha=float(tornado.get("budget_learning_alpha", 0.3)),
            local_fallback_before_wait=bool(tornado.get("local_fallback_before_wait", True)),
        )

    @classmethod
    def from_adapters(
        cls,
        providers: list[tuple[ProviderConfig, Any]],
        *,
        state_path: Path,
        log_path: Path,
        **kwargs,
    ) -> "TornadoClient":
        return cls(providers, state_path=state_path, log_path=log_path, **kwargs)

    def _key_present(self, config: ProviderConfig) -> bool:
        if not config.api_key_env:
            return False
        return bool(os.environ.get(config.api_key_env))

    def _required_environment_present(self, config: ProviderConfig) -> bool:
        return all(bool(os.environ.get(name)) for name in config.required_envs)

    def _effectively_enabled(self, config: ProviderConfig) -> bool:
        if not self._required_environment_present(config):
            return False
        if config.enabled:
            return True
        return bool(config.auto_enable_if_key_present and self._key_present(config))

    def _log_provider_discovery(self) -> None:
        for config, _ in self.providers:
            self._log({
                "event": "provider_discovery",
                "provider": config.provider_id,
                "api_key_env": config.api_key_env,
                "key_present": self._key_present(config),
                "configured_enabled": bool(config.enabled),
                "auto_enable_if_key_present": bool(config.auto_enable_if_key_present),
                "required_envs": list(config.required_envs),
                "required_envs_present": self._required_environment_present(config),
                "effective_enabled": self._effectively_enabled(config),
            })

    def _session_budget_available(self, config: ProviderConfig) -> bool:
        return (
            config.max_calls_per_session is None
            or self.session_calls.get(config.provider_id, 0) < config.max_calls_per_session
        )

    def _eligible(
        self,
        config: ProviderConfig,
        *,
        excluded: set[str] | None = None,
    ) -> bool:
        if excluded and config.provider_id in excluded:
            return False
        if not self._effectively_enabled(config):
            return False
        if not self._session_budget_available(config):
            return False
        now = float(self.clock())
        if self.state.in_cooldown(config.provider_id, now=now):
            return False
        if self.state.retry_at(config.provider_id) > now:
            return False
        return True

    def _adaptive_bonus(self, config: ProviderConfig, task_role: str) -> float:
        role_outcomes = self.state.snapshot(config.provider_id).get("role_outcomes", {})
        role = role_outcomes.get(task_role)
        if not isinstance(role, dict):
            return 0.0
        quality = max(0.0, min(1.0, float(role.get("quality", 0.5))))
        return float(config.adaptive_weight) * ((quality - 0.5) * 2.0)

    def _score(self, config: ProviderConfig, task_role: str = "general") -> float:
        state = self.state.snapshot(config.provider_id)
        score = float(config.priority)
        if task_role in config.roles:
            score += float(config.role_bonus)
        elif "general" in config.roles and task_role != "general":
            score += float(config.role_bonus) * 0.25
        score += self._adaptive_bonus(config, task_role)
        score -= 25.0 * int(state.get("consecutive_failures", 0))
        latency = state.get("last_latency_seconds")
        if latency is not None:
            score -= float(config.latency_weight) * float(latency)
        return score

    def _ordered_candidates(
        self,
        task_role: str = "general",
        *,
        excluded: set[str] | None = None,
    ) -> list[tuple[ProviderConfig, Any]]:
        eligible = [
            item for item in self.providers
            if self._eligible(item[0], excluded=excluded)
        ]
        return sorted(
            eligible,
            key=lambda item: self._score(item[0], task_role),
            reverse=True,
        )

    def _log(self, event: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": float(self.clock()), **event}
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def status_snapshot(self) -> dict[str, Any]:
        """Return read-only provider diagnostics without exposing credential values."""
        now = float(self.clock())
        providers: list[dict[str, Any]] = []
        for config, _adapter in self.providers:
            state = self.state.snapshot(config.provider_id)
            key_present = self._key_present(config) if config.api_key_env else None
            required_present = self._required_environment_present(config)
            effective_enabled = self._effectively_enabled(config)
            session_available = self._session_budget_available(config)
            cooldown_until = float(state.get("cooldown_until", 0.0) or 0.0)
            retry_at = float(state.get("retry_at", 0.0) or 0.0)
            eligible = self._eligible(config)
            if not effective_enabled:
                health_state = "disabled"
            elif not session_available:
                health_state = "session_limit"
            elif cooldown_until > now:
                health_state = "cooldown"
            elif retry_at > now:
                health_state = "waiting_retry"
            else:
                health_state = "ready"
            providers.append({
                "provider": config.provider_id,
                "model": config.model,
                "local": bool(config.local),
                "configured_enabled": bool(config.enabled),
                "auto_enable_if_key_present": bool(config.auto_enable_if_key_present),
                "api_key_env": config.api_key_env,
                "api_key_present": key_present,
                "required_envs": list(config.required_envs),
                "required_envs_present": required_present,
                "effective_enabled": effective_enabled,
                "eligible": eligible,
                "health_state": health_state,
                "session_calls": int(self.session_calls.get(config.provider_id, 0)),
                "max_calls_per_session": config.max_calls_per_session,
                "session_budget_available": session_available,
                "last_success_at": state.get("last_success_at"),
                "last_failure_at": state.get("last_failure_at"),
                "last_failure_category": state.get("last_failure_category"),
                "last_error": state.get("last_error"),
                "cooldown_until": cooldown_until,
                "cooldown_in_seconds": max(0.0, cooldown_until - now),
                "retry_at": retry_at,
                "retry_in_seconds": max(0.0, retry_at - now),
                "learned_budget_recovery_seconds": state.get("learned_budget_recovery_seconds"),
                "learned_budget_recovery_confidence": state.get("learned_budget_recovery_confidence", 0.0),
            })
        return {
            "now": now,
            "provider_count": len(providers),
            "eligible_count": sum(1 for item in providers if item["eligible"]),
            "providers": providers,
        }

    def _redact_provider_secrets(self, config: ProviderConfig, text: str) -> str:
        redacted = str(text)
        env_names = [config.api_key_env, *config.required_envs]
        values = {os.environ[name] for name in env_names if name and os.environ.get(name)}
        for value in sorted(values, key=len, reverse=True):
            redacted = redacted.replace(value, "[REDACTED]")
        return redacted

    def _safe_provider_failure(self, config: ProviderConfig, exc: Exception) -> TornadoError:
        """Redact before errors enter retry state, logs, diagnostics, or callers."""
        failure = _classify_failure(exc)
        return TornadoError(
            self._redact_provider_secrets(config, str(failure)),
            status_code=failure.status_code,
            category=failure.category,
            retry_after_seconds=failure.retry_after_seconds,
            provider_reason=(
                self._redact_provider_secrets(config, failure.provider_reason)
                if failure.provider_reason is not None else None
            ),
        )

    def probe_eligible_cloud(self) -> dict[str, Any]:
        """Make one tiny controller-compatible request to every currently eligible cloud lane.

        Local providers are intentionally skipped. Probe failures are recorded in Tornado health
        state, but one failing lane never prevents the remaining eligible lanes from being tested.
        Credential values are redacted from returned diagnostics, persisted state, and probe logs.
        """
        candidates = [
            (config, adapter)
            for config, adapter in self.providers
            if not config.local and self._eligible(config)
        ]
        probe_messages = [
            {
                "role": "system",
                "content": (
                    "This is a minimal Tornado connectivity probe. Return exactly one controller "
                    "JSON object and no other text."
                ),
            },
            {
                "role": "user",
                "content": (
                    'Return exactly this controller action: {"action":"finish","summary":"probe","tests":[]}'
                ),
            },
        ]
        results: list[dict[str, Any]] = []

        for config, adapter in candidates:
            start = float(self.monotonic())
            self.session_calls[config.provider_id] = self.session_calls.get(config.provider_id, 0) + 1
            try:
                response = adapter.chat(probe_messages)
                # A lane is useful to Local Codex only if it can produce a valid controller action.
                from .actions import parse_action

                parsed = parse_action(response)
                if parsed.get("action") != "finish":
                    raise TornadoError(
                        f"probe returned unexpected controller action: {parsed.get('action')}",
                        category="provider",
                    )
            except Exception as raw_exc:
                latency = max(0.0, float(self.monotonic()) - start)
                safe_failure = self._safe_provider_failure(config, raw_exc)
                safe_message = str(safe_failure)[:500]
                if safe_failure.category == "transient_budget":
                    self.state.record_transient(
                        config.provider_id,
                        safe_message,
                        latency_seconds=latency,
                    )
                    self._schedule_failure(
                        config,
                        safe_failure,
                        latency=latency,
                        count_as_failure=False,
                    )
                elif safe_failure.category in {"transient", "rate_limit", "budget"}:
                    self._schedule_failure(
                        config,
                        safe_failure,
                        latency=latency,
                        count_as_failure=True,
                    )
                else:
                    self.state.record_failure(
                        config.provider_id,
                        safe_message,
                        cooldown_base_seconds=config.cooldown_base_seconds,
                        latency_seconds=latency,
                        category=safe_failure.category,
                    )
                self._log({
                    "provider": config.provider_id,
                    "event": "probe_failure",
                    "latency_seconds": latency,
                    "category": safe_failure.category,
                    "status_code": safe_failure.status_code,
                    "error": safe_message,
                })
                results.append({
                    "provider": config.provider_id,
                    "model": config.model,
                    "ok": False,
                    "latency_seconds": latency,
                    "category": safe_failure.category,
                    "status_code": safe_failure.status_code,
                    "error": safe_message,
                })
                continue

            latency = max(0.0, float(self.monotonic()) - start)
            self.state.record_success(
                config.provider_id,
                latency,
                budget_learning_min_samples=self.budget_learning_min_samples,
                budget_learning_alpha=self.budget_learning_alpha,
            )
            self._log({
                "provider": config.provider_id,
                "event": "probe_success",
                "latency_seconds": latency,
            })
            results.append({
                "provider": config.provider_id,
                "model": config.model,
                "ok": True,
                "latency_seconds": latency,
            })

        success_count = sum(1 for item in results if item["ok"])
        return {
            "eligible_cloud_count": len(candidates),
            "attempted_count": len(results),
            "success_count": success_count,
            "failure_count": len(results) - success_count,
            "providers": results,
        }

    def model_available(self) -> bool:
        for config, adapter in self._ordered_candidates():
            try:
                if adapter.model_available():
                    return True
            except Exception as exc:
                self._log({
                    "provider": config.provider_id,
                    "event": "availability_error",
                    "error": self._redact_provider_secrets(config, str(exc))[:500],
                })
        return False

    def _retry_delay_for(
        self,
        config: ProviderConfig,
        failure: TornadoError,
        state_snapshot: dict[str, Any],
    ) -> float | None:
        category = failure.category
        explicit = failure.retry_after_seconds
        if category in {"auth", "config", "provider"}:
            return None
        if category == "rate_limit":
            if explicit is not None:
                return max(0.0, float(explicit))
            return max(
                0.0,
                float(
                    config.rate_limit_default_retry_seconds
                    if config.rate_limit_default_retry_seconds is not None
                    else self.rate_limit_default_retry_seconds
                ),
            )
        if category == "transient":
            if explicit is not None and explicit > 0:
                return max(0.0, float(explicit))
            base = float(
                config.transient_retry_seconds
                if config.transient_retry_seconds is not None
                else self.transient_retry_seconds
            )
            failures = max(1, int(state_snapshot.get("consecutive_failures", 1)))
            return min(self.max_wait_poll_seconds, max(0.0, base * (2 ** (failures - 1))))
        if category == "transient_budget":
            if explicit is not None and explicit > 0:
                return max(0.0, float(explicit))
            return max(
                0.0,
                float(
                    config.transient_retry_seconds
                    if config.transient_retry_seconds is not None
                    else self.transient_retry_seconds
                ),
            )
        if category == "budget":
            if explicit is not None:
                return max(0.0, float(explicit))
            probe = max(
                0.0,
                float(
                    config.budget_probe_seconds
                    if config.budget_probe_seconds is not None
                    else self.budget_probe_seconds
                ),
            )
            samples = state_snapshot.get("budget_recovery_samples", [])
            learned = state_snapshot.get("learned_budget_recovery_seconds")
            if (
                isinstance(samples, list)
                and len(samples) >= self.budget_learning_min_samples
                and learned is not None
            ):
                learned_delay = max(0.0, float(learned))
                self._log({
                    "provider": config.provider_id,
                    "event": "budget_recovery_prediction",
                    "predicted_seconds": learned_delay,
                    "confidence": state_snapshot.get("learned_budget_recovery_confidence", 0.0),
                    "samples": len(samples),
                })
                return min(probe, learned_delay)
            return probe
        return None

    def _schedule_failure(
        self,
        config: ProviderConfig,
        failure: TornadoError,
        *,
        latency: float,
        count_as_failure: bool,
    ) -> float | None:
        category = failure.category
        if count_as_failure:
            self.state.record_failure(
                config.provider_id,
                str(failure),
                cooldown_base_seconds=0.0,
                latency_seconds=latency,
                category=category,
            )
        snapshot = self.state.snapshot(config.provider_id)
        delay = self._retry_delay_for(config, failure, snapshot)
        if delay is None:
            return None
        retry_at = self.state.schedule_retry(
            config.provider_id,
            category=category,
            retry_after_seconds=delay,
            error_message=str(failure),
            latency_seconds=latency,
        )
        self._log({
            "provider": config.provider_id,
            "event": "provider_retry_scheduled",
            "category": category,
            "retry_at": retry_at,
            "retry_after_seconds": delay,
            "status_code": failure.status_code,
        })
        return retry_at

    def _next_retry_at(self, excluded: set[str]) -> float | None:
        now = float(self.clock())
        retry_times: list[float] = []
        for config, _ in self.providers:
            if config.provider_id in excluded:
                continue
            if not self._effectively_enabled(config):
                continue
            if not self._session_budget_available(config):
                continue
            retry_at = self.state.retry_at(config.provider_id)
            if retry_at > now:
                retry_times.append(retry_at)
        return min(retry_times) if retry_times else None

    @staticmethod
    def _format_wait(seconds: float) -> str:
        rounded = round(float(seconds), 3)
        if abs(rounded - round(rounded)) < 1e-9:
            return str(int(round(rounded)))
        return f"{rounded:g}"

    def chat(self, messages: list[dict[str, str]]) -> str:
        task_role = classify_task_role(messages)
        errors: dict[str, str] = {}
        nonretryable: set[str] = set()

        while True:
            candidates = self._ordered_candidates(task_role, excluded=nonretryable)
            if candidates:
                self._log({
                    "event": "route",
                    "task_role": task_role,
                    "selected_provider": candidates[0][0].provider_id,
                    "candidates": [
                        {
                            "provider": config.provider_id,
                            "model": config.model,
                            "score": float(self._score(config, task_role)),
                            "adaptive_bonus": float(self._adaptive_bonus(config, task_role)),
                        }
                        for config, _ in candidates
                    ],
                })

            candidate_queue = list(candidates)
            while candidate_queue:
                config, adapter = candidate_queue.pop(0)
                outbound = messages if config.local else self.optimizer.optimize(messages)
                transient_retries_used = 0
                promote_local = False

                while True:
                    start = float(self.monotonic())
                    self.session_calls[config.provider_id] = self.session_calls.get(config.provider_id, 0) + 1
                    try:
                        response = adapter.chat(outbound)
                    except Exception as raw_exc:
                        latency = max(0.0, float(self.monotonic()) - start)
                        failure = self._safe_provider_failure(config, raw_exc)
                        errors[config.provider_id] = str(failure)

                        if failure.category == "transient_budget":
                            self.state.record_transient(
                                config.provider_id,
                                str(failure),
                                latency_seconds=latency,
                            )
                            can_retry = (
                                transient_retries_used < config.transient_budget_retries
                                and self._session_budget_available(config)
                            )
                            if can_retry:
                                delay = (
                                    failure.retry_after_seconds
                                    if failure.retry_after_seconds is not None
                                    else config.transient_budget_default_delay_seconds
                                )
                                delay = max(0.0, min(float(delay), 30.0))
                                transient_retries_used += 1
                                self._log({
                                    "provider": config.provider_id,
                                    "event": "budget_retry",
                                    "latency_seconds": latency,
                                    "status_code": failure.status_code,
                                    "reason": failure.provider_reason,
                                    "retry": transient_retries_used,
                                    "max_retries": config.transient_budget_retries,
                                    "retry_after_seconds": delay,
                                })
                                if delay > 0:
                                    self.sleeper(delay)
                                continue

                            self._log({
                                "provider": config.provider_id,
                                "event": "budget_retry_exhausted",
                                "latency_seconds": latency,
                                "status_code": failure.status_code,
                                "reason": failure.provider_reason,
                                "retries_used": transient_retries_used,
                                "max_retries": config.transient_budget_retries,
                                "error": str(failure)[:500],
                            })
                            self._schedule_failure(
                                config,
                                failure,
                                latency=latency,
                                count_as_failure=False,
                            )
                            promote_local = not config.local
                            break

                        if failure.category in {"transient", "rate_limit", "budget"}:
                            self._schedule_failure(
                                config,
                                failure,
                                latency=latency,
                                count_as_failure=True,
                            )
                            event = "budget_limited" if failure.category == "budget" else "failure"
                            log_entry = {
                                "provider": config.provider_id,
                                "event": event,
                                "latency_seconds": latency,
                                "error": str(failure)[:500],
                                "category": failure.category,
                            }
                            if failure.status_code is not None:
                                log_entry["status_code"] = failure.status_code
                            self._log(log_entry)
                            promote_local = not config.local
                            break

                        self.state.record_failure(
                            config.provider_id,
                            str(failure),
                            cooldown_base_seconds=config.cooldown_base_seconds,
                            latency_seconds=latency,
                            category=failure.category,
                        )
                        self._log({
                            "provider": config.provider_id,
                            "event": "failure",
                            "latency_seconds": latency,
                            "error": str(failure)[:500],
                            "category": failure.category,
                            "status_code": failure.status_code,
                        })
                        if failure.category in {"auth", "config"}:
                            nonretryable.add(config.provider_id)
                        break

                    latency = max(0.0, float(self.monotonic()) - start)
                    before = self.state.snapshot(config.provider_id)
                    recovery = self.state.record_success(
                        config.provider_id,
                        latency,
                        budget_learning_min_samples=self.budget_learning_min_samples,
                        budget_learning_alpha=self.budget_learning_alpha,
                    )
                    self._log({
                        "provider": config.provider_id,
                        "event": "success",
                        "latency_seconds": latency,
                    })
                    if before.get("last_failure_category") is not None or float(before.get("retry_at", 0.0) or 0.0) > 0:
                        self._log({
                            "provider": config.provider_id,
                            "event": "provider_recovered",
                            "previous_category": before.get("last_failure_category"),
                        })
                    if recovery.get("budget_recovery_seconds") is not None:
                        after = self.state.snapshot(config.provider_id)
                        self._log({
                            "provider": config.provider_id,
                            "event": "budget_recovery_observed",
                            "recovery_seconds": recovery["budget_recovery_seconds"],
                            "learned_seconds": after.get("learned_budget_recovery_seconds"),
                            "confidence": after.get("learned_budget_recovery_confidence"),
                            "samples": len(after.get("budget_recovery_samples", [])),
                        })
                    self._last_route = (config.provider_id, task_role)
                    return response

                if promote_local and self.local_fallback_before_wait:
                    for index, (candidate_config, _candidate_adapter) in enumerate(candidate_queue):
                        if candidate_config.local:
                            promoted = candidate_queue.pop(index)
                            candidate_queue.insert(0, promoted)
                            self._log({
                                "event": "local_fallback_promoted",
                                "failed_provider": config.provider_id,
                                "local_provider": candidate_config.provider_id,
                                "category": failure.category,
                            })
                            break

            if not self.wait_for_providers:
                break

            retry_at = self._next_retry_at(nonretryable)
            if retry_at is None:
                break
            now = float(self.clock())
            remaining = max(0.0, retry_at - now)
            if remaining <= 0.0:
                continue
            wait_seconds = min(remaining, self.max_wait_poll_seconds)
            message = f"Tornado waiting {self._format_wait(wait_seconds)}s for provider capacity"
            if self.status_callback is not None:
                self.status_callback(message)
            self._log({
                "event": "provider_wait",
                "wait_seconds": wait_seconds,
                "next_retry_at": retry_at,
            })
            self.sleeper(wait_seconds)

        if errors:
            raise TornadoError(
                "all Tornado providers failed: "
                + "; ".join(f"{provider}: {message}" for provider, message in errors.items())
            )
        raise TornadoError("no eligible Tornado providers")

    def report_outcome(self, success: bool, *, signal: str = "controller_outcome") -> None:
        if self._last_route is None:
            return
        provider_id, task_role = self._last_route
        config = next(
            (item for item, _ in self.providers if item.provider_id == provider_id),
            None,
        )
        if config is None:
            return
        self.state.record_outcome(
            provider_id,
            task_role,
            bool(success),
            alpha=config.adaptive_alpha,
        )
        role = self.state.snapshot(provider_id).get("role_outcomes", {}).get(task_role, {})
        self._log({
            "provider": provider_id,
            "event": "outcome",
            "task_role": task_role,
            "success": bool(success),
            "signal": signal,
            "quality": role.get("quality"),
            "samples": role.get("samples"),
        })
        self._last_route = None
