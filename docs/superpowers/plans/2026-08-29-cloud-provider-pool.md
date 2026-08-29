# Cloud Provider Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Live-mode cloud AI provider pool that can spread usage across equivalent-capability routes, fail over automatically within the same capability tier, and require user approval before crossing capability tiers.

**Architecture:** Keep the existing local `hcs_ai.llm.chat()` path unchanged for Offline mode. Add an OpenAI-compatible cloud adapter plus a task-scoped router that loads local provider configuration, selects healthy same-tier routes, classifies provider failures, applies cooldowns, and returns a structured approval request when only a different-tier model can continue. `server.py` becomes the mode-aware dispatcher and `gui.py` displays the active route and handles approval prompts.

**Tech Stack:** Python 3.11, FastAPI, httpx, Tkinter/ttk, pytest, JSON configuration, environment-variable API keys.

**Spec:** `docs/superpowers/specs/2026-08-29-cloud-provider-pool-design.md`

## Global Constraints

- Offline local inference remains unchanged and separate.
- Automatic provider failover is allowed within the task's pinned capability tier.
- Prefer the same exact model before selecting another same-tier model.
- Crossing capability tiers requires explicit user approval before the next cloud request is sent.
- API keys never go in Git or status/log output.
- Initial provider support is OpenAI-compatible REST only; provider-specific adapters are out of scope for this implementation.
- Capability-tier assignments are explicit configuration metadata, not dynamically inferred.
- Routing events must expose provider/model/tier and reason without exposing credentials.

---

## File Structure

- Create `hcs_ai/cloud_provider.py`: OpenAI-compatible request adapter, normalized response, and provider-error classification.
- Create `hcs_ai/cloud_router.py`: provider/model configuration loading, task-tier pinning, weighted route selection, cooldown state, failover, and approval-required decisions.
- Modify `hcs_ai/server.py`: mode-aware chat dispatch, task identifier support, cloud status, approval endpoint.
- Modify `hcs_ai/gui.py`: send a stable task id, show cloud tier/provider/model, present cross-tier approval prompt, retry after approval.
- Modify `config.default.json`: safe provider-pool schema with no credentials.
- Modify `update_manifest.json`: ship the new cloud modules.
- Create `tests/test_cloud_provider.py`: adapter/error-classification coverage.
- Create `tests/test_cloud_router.py`: routing/failover/cooldown/tier-approval coverage.
- Create `tests/test_cloud_server.py`: Live/Offline dispatch and status/approval integration.
- Create `tests/test_cloud_gui_contract.py`: lightweight source/contract checks for task id, route display, approval flow, and Cloud Model Pool UI.
- Add `GET /ai/models` to expose grouped, secret-free model pool status for the GUI.

### Task 1: OpenAI-Compatible Cloud Provider Adapter

**Files:**
- Create: `hcs_ai/cloud_provider.py`
- Create: `tests/test_cloud_provider.py`

**Interfaces:**
- Produces: `ProviderFailure(kind: str, message: str, retry_after_seconds: float | None)`
- Produces: `OpenAICompatibleProvider(provider: dict)`
- Produces: `OpenAICompatibleProvider.complete(messages: list[dict], tools: list[dict] | None = None) -> dict`
- Produces normalized success dict with `text`, `message`, `usage`, `tool_calls`, `provider`, `model`, and `raw`.
- Consumes provider config fields `id`, `base_url`, `model`, `api_key_env`, and `timeout_seconds`.

- [ ] **Step 1: Write failing adapter tests**

Create `tests/test_cloud_provider.py` with tests that construct fake `httpx.Response` objects and verify classification without making network calls:

```python
import httpx
import pytest

from hcs_ai.cloud_provider import ProviderFailure, classify_http_failure


def _response(status, body=None, headers=None):
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    return httpx.Response(status, json=body or {}, headers=headers or {}, request=request)


@pytest.mark.parametrize(
    "status,expected",
    [(429, "rate_limit"), (500, "capacity"), (502, "capacity"), (503, "capacity"), (401, "auth"), (403, "auth")],
)
def test_classify_http_failure(status, expected):
    failure = classify_http_failure(_response(status, {"error": {"message": "boom"}}))
    assert isinstance(failure, ProviderFailure)
    assert failure.kind == expected


def test_classify_http_failure_reads_retry_after():
    failure = classify_http_failure(_response(429, headers={"Retry-After": "12"}))
    assert failure.retry_after_seconds == 12.0
```

- [ ] **Step 2: Run the adapter tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_cloud_provider.py
```

Expected: collection/import failure because `hcs_ai.cloud_provider` does not exist.

- [ ] **Step 3: Implement the minimal adapter**

Create `hcs_ai/cloud_provider.py` with:

```python
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

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
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
            "provider": self.provider["id"],
            "model": model,
            "raw": body,
        }
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_cloud_provider.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```powershell
git add hcs_ai/cloud_provider.py tests/test_cloud_provider.py
git commit -m "feat: add OpenAI-compatible cloud provider adapter"
```

### Task 2: Task-Scoped Cloud Router and Capability-Tier Failover

**Files:**
- Create: `hcs_ai/cloud_router.py`
- Create: `tests/test_cloud_router.py`
- Modify: `config.default.json`

**Interfaces:**
- Consumes: `OpenAICompatibleProvider.complete(...)` and `ProviderFailure`.
- Produces: `cloud_status() -> dict`
- Produces: `chat(task_id: str, messages: list[dict], tools: list[dict] | None = None) -> dict`
- Produces: `approve_tier_change(task_id: str, tier: str) -> dict`
- Normal cloud result contains `text`, `provider`, `model`, `tier`, `approval_required=False`.
- Approval result contains `approval_required=True`, `task_id`, `current_tier`, `proposed_tier`, `provider`, `model`, and `message`.

- [ ] **Step 1: Write failing router tests**

Create `tests/test_cloud_router.py`. Inject a provider factory so tests use deterministic fake providers:

```python
from hcs_ai import cloud_router
from hcs_ai.cloud_provider import ProviderFailure


class FakeProvider:
    outcomes = {}

    def __init__(self, route):
        self.route = route

    def complete(self, messages, tools=None):
        outcome = self.outcomes[self.route["id"]]
        if isinstance(outcome, Exception):
            raise outcome
        return {
            "text": outcome,
            "message": {"role": "assistant", "content": outcome},
            "usage": {},
            "tool_calls": [],
            "provider": self.route["provider"],
            "model": self.route["model"],
            "raw": {},
        }


def _cfg():
    return {
        "cloud_ai": {
            "enabled": True,
            "default_tier": "high",
            "cooldown_seconds": 60,
            "routes": [
                {"id": "a", "provider": "p1", "base_url": "https://a/v1", "model": "same-model", "tier": "high", "weight": 1, "enabled": True, "api_key_env": "A"},
                {"id": "b", "provider": "p2", "base_url": "https://b/v1", "model": "same-model", "tier": "high", "weight": 1, "enabled": True, "api_key_env": "B"},
                {"id": "c", "provider": "p3", "base_url": "https://c/v1", "model": "other-high", "tier": "high", "weight": 1, "enabled": True, "api_key_env": "C"},
                {"id": "d", "provider": "p4", "base_url": "https://d/v1", "model": "medium-model", "tier": "medium", "weight": 1, "enabled": True, "api_key_env": "D"},
            ],
        }
    }


def test_prefers_same_exact_model_on_failover(monkeypatch):
    monkeypatch.setattr(cloud_router, "load_config", _cfg)
    monkeypatch.setattr(cloud_router, "provider_factory", FakeProvider)
    cloud_router.reset_runtime_state()
    FakeProvider.outcomes = {
        "a": ProviderFailure("rate_limit", "limit"),
        "b": "second provider",
        "c": "same tier other model",
        "d": "lower tier",
    }
    result = cloud_router.chat("task-1", [{"role": "user", "content": "hello"}])
    assert result["provider"] == "p2"
    assert result["model"] == "same-model"
    assert result["tier"] == "high"


def test_same_tier_model_change_is_automatic(monkeypatch):
    monkeypatch.setattr(cloud_router, "load_config", _cfg)
    monkeypatch.setattr(cloud_router, "provider_factory", FakeProvider)
    cloud_router.reset_runtime_state()
    FakeProvider.outcomes = {
        "a": ProviderFailure("rate_limit", "limit"),
        "b": ProviderFailure("capacity", "busy"),
        "c": "other high model",
        "d": "medium",
    }
    result = cloud_router.chat("task-2", [{"role": "user", "content": "hello"}])
    assert result["model"] == "other-high"
    assert result["tier"] == "high"
    assert result["approval_required"] is False


def test_cross_tier_change_requires_approval(monkeypatch):
    monkeypatch.setattr(cloud_router, "load_config", _cfg)
    monkeypatch.setattr(cloud_router, "provider_factory", FakeProvider)
    cloud_router.reset_runtime_state()
    FakeProvider.outcomes = {
        "a": ProviderFailure("rate_limit", "limit"),
        "b": ProviderFailure("rate_limit", "limit"),
        "c": ProviderFailure("capacity", "busy"),
        "d": "medium",
    }
    result = cloud_router.chat("task-3", [{"role": "user", "content": "hello"}])
    assert result["approval_required"] is True
    assert result["current_tier"] == "high"
    assert result["proposed_tier"] == "medium"


def test_approved_tier_change_allows_continuation(monkeypatch):
    monkeypatch.setattr(cloud_router, "load_config", _cfg)
    monkeypatch.setattr(cloud_router, "provider_factory", FakeProvider)
    cloud_router.reset_runtime_state()
    cloud_router.approve_tier_change("task-4", "medium")
    FakeProvider.outcomes = {"a": "x", "b": "x", "c": "x", "d": "continued"}
    result = cloud_router.chat("task-4", [{"role": "user", "content": "continue"}])
    assert result["tier"] == "medium"
    assert result["text"] == "continued"
```

- [ ] **Step 2: Run router tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_cloud_router.py
```

Expected: import failure because `hcs_ai.cloud_router` does not exist.

- [ ] **Step 3: Add safe provider-pool defaults**

Replace the existing single-provider `cloud_ai` block in `config.default.json` with:

```json
"cloud_ai": {
  "enabled": false,
  "default_tier": "high",
  "cooldown_seconds": 60,
  "routes": []
}
```

Document the expected local `config.json` route shape in code comments/tests, not with real credentials. Every route must contain:

```json
{
  "id": "provider-route-id",
  "provider": "provider-name",
  "base_url": "https://provider.example/v1",
  "model": "provider-model-id",
  "tier": "high",
  "weight": 1,
  "enabled": true,
  "api_key_env": "HCS_PROVIDER_API_KEY",
  "timeout_seconds": 120
}
```

- [ ] **Step 4: Implement the router**

Create `hcs_ai/cloud_router.py` with in-memory task state and cooldowns:

```python
from __future__ import annotations

from collections import defaultdict
import time

from .config import load_config
from .cloud_provider import OpenAICompatibleProvider, ProviderFailure

provider_factory = OpenAICompatibleProvider
_task_state = {}
_cooldowns = {}
_rotation = defaultdict(int)


def reset_runtime_state():
    _task_state.clear()
    _cooldowns.clear()
    _rotation.clear()


def _routes():
    cloud = load_config().get("cloud_ai", {})
    return [
        dict(route)
        for route in cloud.get("routes", [])
        if route.get("enabled", True)
    ]


def _healthy(route, now):
    return _cooldowns.get(route["id"], 0.0) <= now


def _weighted_order(routes):
    expanded = []
    for route in routes:
        expanded.extend([route] * max(1, int(route.get("weight", 1))))
    if not expanded:
        return []
    key = "|".join(sorted(r["id"] for r in routes))
    start = _rotation[key] % len(expanded)
    _rotation[key] += 1
    ordered = expanded[start:] + expanded[:start]
    seen = set()
    unique = []
    for route in ordered:
        if route["id"] not in seen:
            seen.add(route["id"])
            unique.append(route)
    return unique


def _ordered_candidates(routes, tier, current_model, now):
    same_model = [r for r in routes if r.get("tier") == tier and r.get("model") == current_model and _healthy(r, now)]
    same_tier = [r for r in routes if r.get("tier") == tier and r.get("model") != current_model and _healthy(r, now)]
    return _weighted_order(same_model) + _weighted_order(same_tier)


def _next_other_tier(routes, current_tier, now):
    candidates = [r for r in routes if r.get("tier") != current_tier and _healthy(r, now)]
    return _weighted_order(candidates)[0] if candidates else None


def approve_tier_change(task_id: str, tier: str) -> dict:
    state = _task_state.setdefault(task_id, {})
    state["tier"] = tier
    state.pop("pending_tier", None)
    return {"ok": True, "task_id": task_id, "tier": tier}


def cloud_status() -> dict:
    cfg = load_config().get("cloud_ai", {})
    routes = _routes()
    now = time.monotonic()
    healthy = [r for r in routes if _healthy(r, now)]
    return {
        "enabled": bool(cfg.get("enabled")),
        "configured_routes": len(routes),
        "healthy_routes": len(healthy),
        "tiers": sorted({str(r.get("tier")) for r in routes if r.get("tier")}),
    }


def chat(task_id: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
    cfg = load_config().get("cloud_ai", {})
    routes = _routes()
    if not cfg.get("enabled") or not routes:
        raise RuntimeError("Cloud AI provider pool is not configured.")

    state = _task_state.setdefault(task_id, {})
    tier = state.get("tier") or str(cfg.get("default_tier") or "high")
    current_model = state.get("model")
    if not current_model:
        first = next((r for r in routes if r.get("tier") == tier), None)
        if not first:
            raise RuntimeError(f"No cloud route is configured for capability tier: {tier}")
        current_model = first["model"]
        state["model"] = current_model
        state["tier"] = tier

    now = time.monotonic()
    cooldown_default = float(cfg.get("cooldown_seconds", 60))
    errors = []

    for route in _ordered_candidates(routes, tier, current_model, now):
        try:
            result = provider_factory(route).complete(messages, tools)
            state.update({"tier": route["tier"], "provider": route["provider"], "model": route["model"]})
            return {
                **result,
                "tier": route["tier"],
                "approval_required": False,
            }
        except ProviderFailure as exc:
            errors.append({"route": route["id"], "kind": exc.kind, "message": str(exc)})
            if exc.kind == "auth":
                _cooldowns[route["id"]] = float("inf")
                continue
            if exc.kind in ("rate_limit", "capacity", "timeout", "connectivity"):
                _cooldowns[route["id"]] = now + (exc.retry_after_seconds or cooldown_default)
                continue
            raise

    replacement = _next_other_tier(routes, tier, now)
    if replacement:
        state["pending_tier"] = replacement["tier"]
        return {
            "approval_required": True,
            "task_id": task_id,
            "current_tier": tier,
            "proposed_tier": replacement["tier"],
            "provider": replacement["provider"],
            "model": replacement["model"],
            "message": (
                f"No route remains in the {tier} capability tier. "
                f"Continue with {replacement['model']} in the {replacement['tier']} tier?"
            ),
            "errors": errors,
        }

    raise RuntimeError("No healthy cloud AI route is currently available.")
```

- [ ] **Step 5: Run router tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_cloud_router.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```powershell
git add hcs_ai/cloud_router.py tests/test_cloud_router.py config.default.json
git commit -m "feat: add capability-tier cloud router"
```

### Task 3: Mode-Aware Server Dispatch, Cloud Status, and Approval API

**Files:**
- Modify: `hcs_ai/server.py`
- Create: `tests/test_cloud_server.py`

**Interfaces:**
- Consumes: `cloud_router.chat(task_id, messages, tools=None)`
- Consumes: `cloud_router.cloud_status()`
- Consumes: `cloud_router.approve_tier_change(task_id, tier)`
- Extends `ChatIn` with `task_id: str = "alexandria-default"`.
- Adds `TierApprovalIn(task_id: str, tier: str)`.
- Adds `POST /ai/approve-tier`.
- `POST /chat` dispatches Offline -> existing `llm_chat`, Live -> cloud route.
- Live response includes route metadata; Offline response remains backward-compatible.

- [ ] **Step 1: Write failing server integration tests**

Create `tests/test_cloud_server.py`:

```python
from fastapi.testclient import TestClient
import hcs_ai.server as server


client = TestClient(server.app)


def test_offline_chat_keeps_local_path(monkeypatch):
    monkeypatch.setattr(server, "_ai_mode_status", lambda: {"effective_mode": "offline"})
    monkeypatch.setattr(server, "llm_chat", lambda message, history, use_kb: {"text": "local"})
    response = client.post("/chat", json={"message": "hi", "task_id": "t1"})
    assert response.status_code == 200
    assert response.json()["text"] == "local"


def test_live_chat_uses_cloud_router(monkeypatch):
    monkeypatch.setattr(server, "_ai_mode_status", lambda: {"effective_mode": "live"})
    monkeypatch.setattr(
        server.cloud_router,
        "chat",
        lambda task_id, messages, tools=None: {
            "text": "cloud",
            "provider": "p1",
            "model": "m1",
            "tier": "high",
            "approval_required": False,
        },
    )
    response = client.post("/chat", json={"message": "hi", "history": [], "task_id": "t2"})
    body = response.json()
    assert body["text"] == "cloud"
    assert body["provider"] == "p1"
    assert body["tier"] == "high"


def test_tier_approval_endpoint(monkeypatch):
    monkeypatch.setattr(server.cloud_router, "approve_tier_change", lambda task_id, tier: {"ok": True, "task_id": task_id, "tier": tier})
    response = client.post("/ai/approve-tier", json={"task_id": "t3", "tier": "medium"})
    assert response.status_code == 200
    assert response.json()["tier"] == "medium"
```

- [ ] **Step 2: Run server tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_cloud_server.py
```

Expected: FAIL because `ChatIn` lacks `task_id`, Live dispatch is not implemented, and `/ai/approve-tier` does not exist.

- [ ] **Step 3: Implement server dispatch**

Modify imports in `hcs_ai/server.py`:

```python
from . import cloud_router
```

Extend the request models:

```python
class ChatIn(BaseModel):
    message: str
    history: list[dict] = Field(default_factory=list)
    use_kb: bool = True
    task_id: str = "alexandria-default"


class TierApprovalIn(BaseModel):
    task_id: str
    tier: str
```

Replace single-provider cloud status logic with `cloud_router.cloud_status()`, while retaining the existing Internet probe. Derive:

```python
pool = cloud_router.cloud_status()
cloud_configured = bool(pool["enabled"] and pool["configured_routes"] > 0)
live_available = bool(internet and cloud_configured and pool["healthy_routes"] > 0)
```

Include `cloud_pool: pool` in `/ai/status`.

Update `POST /chat`:

```python
@app.post("/chat")
def chat(inp: ChatIn):
    try:
        audit("chat", inp.message[:1000])
        status = _ai_mode_status()
        if status["effective_mode"] != "live":
            return llm_chat(inp.message, inp.history, inp.use_kb)

        cfg = load_config()
        messages = [{"role": "system", "content": cfg["app"]["system_prompt"]}]
        messages.extend(inp.history[-12:])
        messages.append({"role": "user", "content": inp.message})
        result = cloud_router.chat(inp.task_id, messages)
        audit(
            "cloud_route",
            json.dumps({
                "task_id": inp.task_id,
                "provider": result.get("provider"),
                "model": result.get("model"),
                "tier": result.get("tier") or result.get("current_tier"),
                "approval_required": bool(result.get("approval_required")),
            }),
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {e}")
```

Add:

```python
@app.post("/ai/approve-tier")
def ai_approve_tier(inp: TierApprovalIn):
    return cloud_router.approve_tier_change(inp.task_id, inp.tier)
```

- [ ] **Step 4: Run server tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_cloud_server.py
```

Expected: PASS.

- [ ] **Step 5: Run existing local inference tests**

Run:

```powershell
python -m pytest -q tests/test_llm_telemetry.py tests/test_engine_startup_state.py tests/test_server_nonblocking_model_start.py
```

Expected: PASS, proving Offline mode behavior remains intact.

- [ ] **Step 6: Commit Task 3**

```powershell
git add hcs_ai/server.py tests/test_cloud_server.py
git commit -m "feat: route Live mode through cloud provider pool"
```

### Task 4: Alexandria Route Display and Cross-Tier Approval Flow

**Files:**
- Modify: `hcs_ai/gui.py`
- Create: `tests/test_cloud_gui_contract.py`

**Interfaces:**
- Consumes `/ai/status` field `cloud_pool`.
- Sends `task_id` with every `/chat` request.
- Consumes cloud response fields `provider`, `model`, `tier`, `approval_required`, `proposed_tier`, `message`.
- Calls `POST /ai/approve-tier` after explicit user confirmation.

- [ ] **Step 1: Write failing GUI contract tests**

Create `tests/test_cloud_gui_contract.py`:

```python
from pathlib import Path


GUI = Path("hcs_ai/gui.py").read_text(encoding="utf-8")


def test_gui_sends_stable_cloud_task_id():
    assert '"task_id": self.cloud_task_id' in GUI


def test_gui_displays_cloud_route_metadata():
    assert "Cloud:" in GUI
    assert "cloud_route_label" in GUI


def test_gui_has_cross_tier_approval_call():
    assert '"/ai/approve-tier"' in GUI
    assert "askyesno" in GUI
```

- [ ] **Step 2: Run GUI contract tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_cloud_gui_contract.py
```

Expected: FAIL.

- [ ] **Step 3: Add a stable Alexandria task id and route label**

In `App.__init__`, initialize:

```python
import uuid
...
self.cloud_task_id = "alexandria-" + uuid.uuid4().hex
self._pending_cloud_request = None
```

In `build_chat()`, add a header label:

```python
self.cloud_route_label = ttk.Label(header, text="Cloud: —")
self.cloud_route_label.pack(side="left", padx=(4, 8))
```

Update `_apply_ai_mode_status()` so Offline displays `Cloud: —`, and Live displays current pool summary when route metadata is available.

- [ ] **Step 4: Send task id and handle approval-required responses**

In `send()`, preserve the request payload and add:

```python
payload = {
    "message": msg,
    "history": history,
    "use_kb": use_kb,
    "task_id": self.cloud_task_id,
}
self._pending_cloud_request = payload
out = api("POST", "/chat", payload)
```

In `_chat_done()`, before appending an assistant response:

```python
if out.get("approval_required"):
    self._set_thinking(False)
    approved = messagebox.askyesno(
        "Cloud model change",
        out.get("message") or (
            f"Continue by changing capability tier from "
            f"{out.get('current_tier')} to {out.get('proposed_tier')}?"
        ),
    )
    if approved:
        api(
            "POST",
            "/ai/approve-tier",
            {"task_id": self.cloud_task_id, "tier": out["proposed_tier"]},
            timeout=10,
        )
        self._retry_pending_cloud_request()
    else:
        self.append_chat("HCS-AI", "Cloud task paused. Model caliber was not changed.")
    return
```

Add `_retry_pending_cloud_request()` to resend the exact pending message/history only after approval.

Whenever a normal cloud result returns, update:

```python
if out.get("provider") and out.get("model"):
    self.cloud_route_label.config(
        text=f"Cloud: {out.get('tier', '?')} | {out['provider']} | {out['model']}"
    )
```

- [ ] **Step 5: Run GUI contract tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_cloud_gui_contract.py
```

Expected: PASS.

- [ ] **Step 6: Run existing GUI tests**

Run:

```powershell
python -m pytest -q tests/test_gui_recent.py tests/test_gui_diagnostics.py
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```powershell
git add hcs_ai/gui.py tests/test_cloud_gui_contract.py
git commit -m "feat: add cloud route status and tier approval UI"
```


### Task 5: Cloud Model Pool Inventory and GUI View

**Files:**
- Modify: `hcs_ai/cloud_router.py`
- Modify: `hcs_ai/server.py`
- Modify: `hcs_ai/gui.py`
- Modify: `tests/test_cloud_router.py`
- Modify: `tests/test_cloud_server.py`
- Modify: `tests/test_cloud_gui_contract.py`

**Interfaces:**
- Produces: `cloud_router.model_inventory(task_id: str | None = None) -> dict`.
- Adds: `GET /ai/models?task_id=<id>`.
- Inventory groups identical model ids within capability tiers and returns provider-route state without secrets.
- GUI adds a `Cloud Models` control/view showing tier, model, providers, healthy/configured count, credential-configured state, failover eligibility, and active-task marker.

- [ ] **Step 1: Write failing inventory tests**

Add router tests using deterministic routes and environment variables:

```python
def test_model_inventory_groups_same_model_across_providers(monkeypatch):
    monkeypatch.setattr(cloud_router, "load_config", _cfg)
    monkeypatch.setenv("A", "x")
    monkeypatch.delenv("B", raising=False)
    cloud_router.reset_runtime_state()
    inventory = cloud_router.model_inventory("task-1")
    high = next(t for t in inventory["tiers"] if t["tier"] == "high")
    same = next(m for m in high["models"] if m["model"] == "same-model")
    assert same["configured_routes"] == 2
    assert {p["provider"] for p in same["providers"]} == {"p1", "p2"}
    assert {p["credential_configured"] for p in same["providers"]} == {True, False}
```

Add server test:

```python
def test_ai_models_endpoint_is_secret_free(monkeypatch):
    monkeypatch.setattr(
        server.cloud_router,
        "model_inventory",
        lambda task_id=None: {
            "tiers": [{"tier": "high", "models": [{"model": "m1", "providers": [{"provider": "p1", "credential_configured": True}]}]}]
        },
    )
    response = client.get("/ai/models?task_id=t1")
    assert response.status_code == 200
    assert "api_key" not in response.text.lower()
```

Extend GUI contract test:

```python
def test_gui_has_cloud_model_pool_view():
    assert '"/ai/models' in GUI
    assert "Cloud Models" in GUI
    assert "cloud_models" in GUI
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_cloud_router.py tests/test_cloud_server.py tests/test_cloud_gui_contract.py
```

Expected: FAIL because model inventory and GUI view do not exist.

- [ ] **Step 3: Implement secret-free model inventory**

In `hcs_ai/cloud_router.py`, add `model_inventory(task_id=None)` that groups enabled routes by `tier` then `model`, reports each provider's route id, provider, healthy/cooldown state, and `credential_configured = bool(os.environ.get(api_key_env))`, plus model-level `configured_routes`, `healthy_routes`, `same_tier_failover_eligible`, and `active`.

Never return `api_key_env` values or credential contents.

- [ ] **Step 4: Expose inventory through the server**

In `hcs_ai/server.py` add:

```python
@app.get("/ai/models")
def ai_models(task_id: str | None = None):
    return cloud_router.model_inventory(task_id)
```

- [ ] **Step 5: Add the Cloud Models GUI view**

Add a `Cloud Models` button near the Live status. It opens a `Toplevel` containing a tree/list grouped by tier and model. Populate it from `GET /ai/models?task_id=<cloud_task_id>`.

Each model row must show:
- model id
- tier
- provider names
- healthy/configured routes
- failover eligibility
- active marker

Child/provider rows show provider name, route state, and `Key: Ready` or `Key: Missing`.

- [ ] **Step 6: Run tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_cloud_router.py tests/test_cloud_server.py tests/test_cloud_gui_contract.py
```

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```powershell
git add hcs_ai/cloud_router.py hcs_ai/server.py hcs_ai/gui.py tests/test_cloud_router.py tests/test_cloud_server.py tests/test_cloud_gui_contract.py
git commit -m "feat: add cloud model pool inventory"
```

### Task 6: Update Packaging and End-to-End Configuration Validation

**Files:**
- Modify: `update_manifest.json`
- Modify: `tests/test_cloud_router.py`
- Modify: `tests/test_cloud_server.py`

**Interfaces:**
- Ensures updater ships `hcs_ai/cloud_provider.py` and `hcs_ai/cloud_router.py`.
- Verifies environment-variable credential lookup and that status/audit payloads never expose secret values.

- [ ] **Step 1: Add failing packaging/security tests**

Append tests that assert:

```python
import json
from pathlib import Path


def test_update_manifest_ships_cloud_modules():
    manifest = json.loads(Path("update_manifest.json").read_text(encoding="utf-8"))
    assert "hcs_ai/cloud_provider.py" in manifest["files"]
    assert "hcs_ai/cloud_router.py" in manifest["files"]


def test_default_config_contains_no_api_secret():
    text = Path("config.default.json").read_text(encoding="utf-8").lower()
    assert "sk-" not in text
    assert '"api_key":' not in text
```

Also add a server/status test that supplies a route with `api_key_env="HCS_TEST_SECRET"`, sets the environment variable to a sentinel value such as `DO_NOT_LEAK_ME`, and asserts the sentinel does not occur in serialized `/ai/status` output.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_cloud_router.py tests/test_cloud_server.py
```

Expected: manifest test fails because the new module paths are not yet listed.

- [ ] **Step 3: Update the manifest**

Add these entries to `update_manifest.json`:

```json
"hcs_ai/cloud_provider.py",
"hcs_ai/cloud_router.py",
```

Do not add `config.json`, `.env`, or any local secret file.

- [ ] **Step 4: Run the complete cloud test set**

Run:

```powershell
python -m pytest -q tests/test_cloud_provider.py tests/test_cloud_router.py tests/test_cloud_server.py tests/test_cloud_gui_contract.py
```

Expected: PASS.

- [ ] **Step 5: Run the full regression suite**

Run:

```powershell
python -m compileall -q hcs_ai
python -m pytest -q
```

Expected: all tests pass and compileall exits 0.

- [ ] **Step 6: Commit Task 5**

```powershell
git add update_manifest.json tests/test_cloud_router.py tests/test_cloud_server.py
git commit -m "test: validate cloud pool packaging and secret safety"
```

## Manual Acceptance Test

After the implementation has passed automated tests and HCS has updated locally:

1. Add at least two OpenAI-compatible routes to local `config.json`; assign them the same tier and point each `api_key_env` at a Windows environment variable containing its key.
2. Start HCS and confirm Alexandria reports `Internet: Available` and `Cloud AI: Ready`.
3. Select Live.
4. Send a prompt and confirm the header shows `Cloud: <tier> | <provider> | <model>`.
5. Send several independent prompts and confirm healthy equivalent routes rotate according to weight.
6. Temporarily make the active route return a simulated/real rate-limit condition and confirm HCS automatically fails over to another route in the same tier.
7. Make every route in that tier unavailable while leaving a different-tier route healthy.
8. Send another prompt and confirm HCS asks before switching caliber.
9. Decline and confirm the task pauses without sending the prompt to the different-tier route.
10. Repeat, approve the change, and confirm HCS continues with the newly approved tier.
11. Switch back to Offline and confirm the local llama.cpp path still answers normally.

## Self-Review

- **Spec coverage:** provider pooling, same-model preference, same-tier automatic substitution, weighted usage spreading, cooldowns, auth handling, cross-tier approval, route visibility, Cloud Model Pool inventory/view, secret isolation, Offline preservation, logging/status, and tests are all mapped to tasks.
- **Placeholder scan:** no TBD/TODO/“implement later” instructions remain.
- **Type consistency:** router response keys and server/GUI consumers use the same `provider`, `model`, `tier`, `approval_required`, `current_tier`, and `proposed_tier` names.
