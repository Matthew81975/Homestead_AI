import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hcs_ai.local_codex.tornado import (
    OpenAICompatibleClient,
    ProviderConfig,
    TornadoClient,
    TornadoError,
    TornadoStateStore,
)


class FakeProvider:
    def __init__(self, provider_id, replies=None, error=None, available=True, local=True):
        self.provider_id = provider_id
        self.replies = list(replies or [])
        self.error = error
        self.available = available
        self.local = local
        self.calls = []

    def model_available(self):
        return self.available

    def chat(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.replies.pop(0) if self.replies else '{"action":"git_status"}'


def provider_config(
    provider_id,
    *,
    priority=100,
    local=True,
    budget=None,
    cooldown=60,
    transient_budget_retries=2,
):
    return ProviderConfig(
        provider_id=provider_id,
        kind="lm_studio" if local else "openai_compatible",
        base_url="http://example.invalid",
        model="model",
        priority=priority,
        timeout=10,
        local=local,
        api_key_env=None,
        max_calls_per_session=budget,
        cooldown_base_seconds=cooldown,
        transient_budget_retries=transient_budget_retries,
    )


def test_highest_priority_healthy_provider_is_used(tmp_path: Path):
    low = FakeProvider("low", replies=['{"action":"list_files"}'])
    high = FakeProvider("high", replies=['{"action":"git_status"}'])
    client = TornadoClient.from_adapters(
        [(provider_config("low", priority=10), low), (provider_config("high", priority=50), high)],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    result = client.chat([{"role": "user", "content": "hello"}])

    assert result == '{"action":"git_status"}'
    assert len(high.calls) == 1
    assert low.calls == []


def test_failure_rotates_to_next_provider_and_persists_cooldown(tmp_path: Path):
    first = FakeProvider("first", error=RuntimeError("rate limited"))
    second = FakeProvider("second", replies=['{"action":"git_diff"}'])
    state_path = tmp_path / "state.json"
    client = TornadoClient.from_adapters(
        [(provider_config("first", priority=100, cooldown=30), first), (provider_config("second", priority=50), second)],
        state_path=state_path,
        log_path=tmp_path / "tornado.log",
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_diff"}'

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["providers"]["first"]["consecutive_failures"] == 1
    assert state["providers"]["first"]["cooldown_until"] > time.time()
    assert state["providers"]["second"]["total_successes"] == 1


def test_provider_in_cooldown_is_skipped_after_restart(tmp_path: Path):
    state_path = tmp_path / "state.json"
    store = TornadoStateStore(state_path)
    store.record_failure("first", "boom", cooldown_base_seconds=60)

    first = FakeProvider("first", replies=['{"action":"finish"}'])
    second = FakeProvider("second", replies=['{"action":"git_status"}'])
    client = TornadoClient.from_adapters(
        [(provider_config("first", priority=100), first), (provider_config("second", priority=50), second)],
        state_path=state_path,
        log_path=tmp_path / "tornado.log",
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_status"}'
    assert first.calls == []
    assert len(second.calls) == 1


def test_session_budget_excludes_provider_after_budget_is_spent(tmp_path: Path):
    first = FakeProvider("first", replies=['{"action":"git_status"}'])
    second = FakeProvider("second", replies=['{"action":"git_diff"}'])
    client = TornadoClient.from_adapters(
        [(provider_config("first", priority=100, budget=1), first), (provider_config("second", priority=50), second)],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    assert client.chat([{"role": "user", "content": "one"}]) == '{"action":"git_status"}'
    assert client.chat([{"role": "user", "content": "two"}]) == '{"action":"git_diff"}'


def test_cloud_provider_receives_optimized_prompt_but_local_does_not(tmp_path: Path):
    local = FakeProvider("local", error=RuntimeError("offline"), local=True)
    cloud = FakeProvider("cloud", replies=['{"action":"git_status"}'], local=False)
    client = TornadoClient.from_adapters(
        [(provider_config("local", priority=100, local=True), local), (provider_config("cloud", priority=50, local=False), cloud)],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )
    messages = [
        {"role": "system", "content": "system\n"},
        {"role": "user", "content": '{\n  "task": "x"\n}'},
    ]

    client.chat(messages)

    assert local.calls[0] == messages
    assert cloud.calls[0][0] == messages[0]
    assert cloud.calls[0][1]["content"] == '{"task":"x"}'


def test_all_provider_failures_raise_tornado_error(tmp_path: Path):
    one = FakeProvider("one", error=RuntimeError("one down"))
    two = FakeProvider("two", error=RuntimeError("two down"))
    client = TornadoClient.from_adapters(
        [(provider_config("one", priority=2), one), (provider_config("two", priority=1), two)],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    with pytest.raises(TornadoError, match="one.*two|two.*one"):
        client.chat([{"role": "user", "content": "hello"}])


class AuthCaptureHandler(BaseHTTPRequestHandler):
    authorization = None

    def do_GET(self):
        AuthCaptureHandler.authorization = self.headers.get("Authorization")
        body = json.dumps({"data": [{"id": "cloud-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_openai_compatible_client_reads_bearer_token_from_environment(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), AuthCaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TORNADO_TEST_KEY", "secret-value")
    try:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="cloud-model",
            api_key_env="TORNADO_TEST_KEY",
            timeout=5,
        )
        assert client.model_available()
        assert AuthCaptureHandler.authorization == "Bearer secret-value"
    finally:
        server.shutdown()



def role_provider_config(provider_id, *, priority=100, roles=(), role_bonus=40, latency_weight=0.0):
    return ProviderConfig(
        provider_id=provider_id,
        kind="openai_compatible",
        base_url="http://example.invalid",
        model="model",
        priority=priority,
        timeout=10,
        local=False,
        api_key_env=None,
        max_calls_per_session=None,
        cooldown_base_seconds=60,
        roles=tuple(roles),
        role_bonus=role_bonus,
        latency_weight=latency_weight,
    )


def test_task_classifier_detects_debugging_from_controller_task_json():
    from hcs_ai.local_codex.tornado import classify_task_role

    role = classify_task_role([
        {"role": "system", "content": "controller"},
        {"role": "user", "content": '{"task":"Fix the portal collision bug and failing test"}'},
    ])

    assert role == "debugging"


def test_task_classifier_detects_visual_work():
    from hcs_ai.local_codex.tornado import classify_task_role

    role = classify_task_role([
        {"role": "user", "content": "Inspect the screenshot and verify the UI visually"},
    ])

    assert role == "vision"


def test_role_affinity_can_beat_higher_base_priority(tmp_path: Path):
    general = FakeProvider("general", replies=['{"action":"list_files"}'])
    debugger = FakeProvider("debugger", replies=['{"action":"git_status"}'])
    client = TornadoClient.from_adapters(
        [
            (role_provider_config("general", priority=100, roles=("general",)), general),
            (role_provider_config("debugger", priority=80, roles=("debugging",), role_bonus=40), debugger),
        ],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    result = client.chat([
        {"role": "user", "content": '{"task":"Debug the failing collision test"}'},
    ])

    assert result == '{"action":"git_status"}'
    assert len(debugger.calls) == 1
    assert general.calls == []


def test_latency_penalty_prefers_faster_peer(tmp_path: Path):
    state_path = tmp_path / "state.json"
    store = TornadoStateStore(state_path)
    store._provider("slow")["last_latency_seconds"] = 12.0
    store._provider("fast")["last_latency_seconds"] = 1.0
    store.save()

    slow = FakeProvider("slow", replies=['{"action":"list_files"}'])
    fast = FakeProvider("fast", replies=['{"action":"git_status"}'])
    client = TornadoClient.from_adapters(
        [
            (role_provider_config("slow", priority=100, roles=("coding",), latency_weight=2.0), slow),
            (role_provider_config("fast", priority=100, roles=("coding",), latency_weight=2.0), fast),
        ],
        state_path=state_path,
        log_path=tmp_path / "tornado.log",
    )

    result = client.chat([
        {"role": "user", "content": '{"task":"Implement a new parser"}'},
    ])

    assert result == '{"action":"git_status"}'
    assert len(fast.calls) == 1
    assert slow.calls == []


def test_routing_log_records_role_and_scores(tmp_path: Path):
    provider = FakeProvider("coder", replies=['{"action":"git_status"}'])
    log_path = tmp_path / "tornado.log"
    client = TornadoClient.from_adapters(
        [(role_provider_config("coder", roles=("coding",)), provider)],
        state_path=tmp_path / "state.json",
        log_path=log_path,
    )

    client.chat([{"role": "user", "content": '{"task":"Implement the feature"}'}])

    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    route = next(item for item in entries if item["event"] == "route")
    assert route["task_role"] == "coding"
    assert route["selected_provider"] == "coder"
    assert route["candidates"][0]["provider"] == "coder"
    assert isinstance(route["candidates"][0]["score"], float)


def test_disabled_provider_is_never_selected(tmp_path: Path):
    disabled_config = ProviderConfig(
        provider_id="disabled",
        kind="openai_compatible",
        base_url="http://example.invalid",
        model="model",
        priority=999,
        local=False,
        enabled=False,
    )
    enabled = FakeProvider("enabled", replies=['{"action":"git_status"}'])
    disabled = FakeProvider("disabled", replies=['{"action":"list_files"}'])
    client = TornadoClient.from_adapters(
        [
            (disabled_config, disabled),
            (role_provider_config("enabled", priority=1), enabled),
        ],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_status"}'
    assert disabled.calls == []



class ChatCaptureHandler(BaseHTTPRequestHandler):
    request_payload = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        ChatCaptureHandler.request_payload = json.loads(self.rfile.read(length).decode("utf-8"))
        body = json.dumps({"choices": [{"message": {"content": '{"action":"git_status"}'}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_openai_compatible_client_can_omit_response_format():
    server = HTTPServer(("127.0.0.1", 0), ChatCaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="cloud-model",
            timeout=5,
            use_response_format=False,
        )
        assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_status"}'
        assert "response_format" not in ChatCaptureHandler.request_payload
    finally:
        server.shutdown()



def test_provider_config_from_dict_reads_task_routing_fields():
    config = ProviderConfig.from_dict({
        "id": "cloud",
        "kind": "openai_compatible",
        "base_url": "https://example.invalid",
        "model": "model",
        "roles": ["coding", "review"],
        "role_bonus": 55,
        "latency_weight": 0.25,
        "enabled": False,
        "use_response_format": False,
    })

    assert config.roles == ("coding", "review")
    assert config.role_bonus == 55.0
    assert config.latency_weight == 0.25
    assert config.enabled is False
    assert config.use_response_format is False


def test_state_store_records_role_outcomes_with_bounded_ewma(tmp_path: Path):
    store = TornadoStateStore(tmp_path / "state.json")

    store.record_outcome("coder", "coding", True, alpha=0.2)
    first = store.snapshot("coder")["role_outcomes"]["coding"]
    assert first["samples"] == 1
    assert first["quality"] == pytest.approx(0.6)

    store.record_outcome("coder", "coding", False, alpha=0.2)
    second = store.snapshot("coder")["role_outcomes"]["coding"]
    assert second["samples"] == 2
    assert second["quality"] == pytest.approx(0.48)


def test_adaptive_quality_bonus_can_change_provider_order(tmp_path: Path):
    state_path = tmp_path / "state.json"
    store = TornadoStateStore(state_path)
    for _ in range(6):
        store.record_outcome("proven", "coding", True, alpha=0.2)
        store.record_outcome("weak", "coding", False, alpha=0.2)

    proven = FakeProvider("proven", replies=['{"action":"git_status"}'])
    weak = FakeProvider("weak", replies=['{"action":"list_files"}'])
    client = TornadoClient.from_adapters(
        [
            (ProviderConfig(
                provider_id="weak", kind="openai_compatible", base_url="http://example.invalid",
                model="weak", priority=110, local=False, roles=("coding",), adaptive_weight=35.0,
            ), weak),
            (ProviderConfig(
                provider_id="proven", kind="openai_compatible", base_url="http://example.invalid",
                model="proven", priority=90, local=False, roles=("coding",), adaptive_weight=35.0,
            ), proven),
        ],
        state_path=state_path,
        log_path=tmp_path / "tornado.log",
    )

    result = client.chat([{"role": "user", "content": '{"task":"Implement the parser"}'}])

    assert result == '{"action":"git_status"}'
    assert len(proven.calls) == 1
    assert weak.calls == []


def test_report_outcome_updates_last_selected_provider_role(tmp_path: Path):
    provider = FakeProvider("coder", replies=['{"action":"git_status"}'])
    client = TornadoClient.from_adapters(
        [(ProviderConfig(
            provider_id="coder", kind="openai_compatible", base_url="http://example.invalid",
            model="coder", priority=100, local=False, roles=("coding",), adaptive_weight=30.0,
        ), provider)],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    client.chat([{"role": "user", "content": '{"task":"Implement the feature"}'}])
    client.report_outcome(False, signal="action_failed")

    role = client.state.snapshot("coder")["role_outcomes"]["coding"]
    assert role["samples"] == 1
    assert role["quality"] < 0.5


def test_adaptive_bonus_is_bounded_by_configured_weight(tmp_path: Path):
    state_path = tmp_path / "state.json"
    store = TornadoStateStore(state_path)
    store._provider("coder")["role_outcomes"] = {
        "coding": {"quality": 1.0, "samples": 1000, "successes": 1000, "failures": 0}
    }
    store.save()
    config = ProviderConfig(
        provider_id="coder", kind="openai_compatible", base_url="http://example.invalid",
        model="coder", priority=100, local=False, roles=("coding",), adaptive_weight=25.0,
    )
    client = TornadoClient.from_adapters(
        [(config, FakeProvider("coder"))],
        state_path=state_path,
        log_path=tmp_path / "tornado.log",
    )

    assert client._adaptive_bonus(config, "coding") == pytest.approx(25.0)


def test_route_log_exposes_adaptive_bonus(tmp_path: Path):
    state_path = tmp_path / "state.json"
    store = TornadoStateStore(state_path)
    store.record_outcome("coder", "coding", True, alpha=0.2)
    provider = FakeProvider("coder", replies=['{"action":"git_status"}'])
    log_path = tmp_path / "tornado.log"
    config = ProviderConfig(
        provider_id="coder", kind="openai_compatible", base_url="http://example.invalid",
        model="coder", priority=100, local=False, roles=("coding",), adaptive_weight=30.0,
    )
    client = TornadoClient.from_adapters(
        [(config, provider)], state_path=state_path, log_path=log_path
    )

    client.chat([{"role": "user", "content": '{"task":"Implement the feature"}'}])

    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    route = next(item for item in entries if item["event"] == "route")
    assert route["candidates"][0]["adaptive_bonus"] > 0


def test_provider_config_reads_auto_enable_if_key_present():
    config = ProviderConfig.from_dict({
        "id": "cloud",
        "kind": "openai_compatible",
        "base_url": "https://example.invalid",
        "model": "model",
        "enabled": False,
        "api_key_env": "CLOUD_API_KEY",
        "auto_enable_if_key_present": True,
    })

    assert config.enabled is False
    assert config.auto_enable_if_key_present is True


def test_disabled_cloud_provider_auto_enables_when_key_is_present(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLOUD_API_KEY", "super-secret-value")
    cloud = FakeProvider("cloud", replies=['{"action":"git_status"}'])
    local = FakeProvider("local", replies=['{"action":"list_files"}'])
    cloud_config = ProviderConfig(
        provider_id="cloud",
        kind="openai_compatible",
        base_url="https://example.invalid",
        model="cloud-model",
        priority=200,
        local=False,
        api_key_env="CLOUD_API_KEY",
        enabled=False,
        auto_enable_if_key_present=True,
    )
    client = TornadoClient.from_adapters(
        [(cloud_config, cloud), (provider_config("local", priority=10), local)],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_status"}'
    assert len(cloud.calls) == 1
    assert local.calls == []


def test_disabled_cloud_provider_stays_disabled_without_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CLOUD_API_KEY", raising=False)
    cloud = FakeProvider("cloud", replies=['{"action":"git_status"}'])
    local = FakeProvider("local", replies=['{"action":"list_files"}'])
    cloud_config = ProviderConfig(
        provider_id="cloud",
        kind="openai_compatible",
        base_url="https://example.invalid",
        model="cloud-model",
        priority=200,
        local=False,
        api_key_env="CLOUD_API_KEY",
        enabled=False,
        auto_enable_if_key_present=True,
    )
    client = TornadoClient.from_adapters(
        [(cloud_config, cloud), (provider_config("local", priority=10), local)],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"list_files"}'
    assert cloud.calls == []
    assert len(local.calls) == 1


def test_provider_discovery_log_records_key_name_and_presence_not_secret(tmp_path: Path, monkeypatch):
    secret = "do-not-log-this-secret"
    monkeypatch.setenv("CLOUD_API_KEY", secret)
    config = ProviderConfig(
        provider_id="cloud",
        kind="openai_compatible",
        base_url="https://example.invalid",
        model="cloud-model",
        local=False,
        api_key_env="CLOUD_API_KEY",
        enabled=False,
        auto_enable_if_key_present=True,
    )
    log_path = tmp_path / "tornado.log"

    TornadoClient.from_adapters(
        [(config, FakeProvider("cloud"))],
        state_path=tmp_path / "state.json",
        log_path=log_path,
    )

    text = log_path.read_text(encoding="utf-8")
    assert "CLOUD_API_KEY" in text
    assert '"key_present": true' in text
    assert secret not in text


class PaymentRequiredHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({
            "error": {
                "message": "This request requires more credits, or fewer max_tokens."
            }
        }).encode("utf-8")
        self.send_response(402, "Payment Required")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_openai_compatible_client_sends_configured_max_tokens():
    ChatCaptureHandler.request_payload = None
    server = HTTPServer(("127.0.0.1", 0), ChatCaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="cloud-model",
            timeout=5,
            use_response_format=False,
            max_tokens=1024,
        )
        client.chat([{"role": "user", "content": "hello"}])
        assert ChatCaptureHandler.request_payload["max_tokens"] == 1024
    finally:
        server.shutdown()


def test_provider_config_reads_max_tokens():
    config = ProviderConfig.from_dict({
        "id": "cloud",
        "kind": "openai_compatible",
        "base_url": "https://example.invalid",
        "model": "model",
        "max_tokens": 1536,
    })

    assert config.max_tokens == 1536


def test_http_402_is_classified_as_budget_error():
    server = HTTPServer(("127.0.0.1", 0), PaymentRequiredHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="cloud-model",
            timeout=5,
            use_response_format=False,
            max_tokens=1024,
        )
        with pytest.raises(TornadoError) as exc_info:
            client.chat([{"role": "user", "content": "hello"}])

        assert exc_info.value.status_code == 402
        assert exc_info.value.category == "budget"
        assert "fewer max_tokens" in str(exc_info.value)
    finally:
        server.shutdown()


def test_budget_error_logs_distinct_event_and_falls_back(tmp_path: Path):
    budget_error = TornadoError("HTTP 402: Payment Required", status_code=402, category="budget")
    cloud = FakeProvider("cloud", error=budget_error, local=False)
    local = FakeProvider("local", replies=['{"action":"git_status"}'], local=True)
    log_path = tmp_path / "tornado.log"
    client = TornadoClient.from_adapters(
        [
            (provider_config("cloud", priority=100, local=False), cloud),
            (provider_config("local", priority=50, local=True), local),
        ],
        state_path=tmp_path / "state.json",
        log_path=log_path,
    )

    result = client.chat([{"role": "user", "content": "hello"}])

    assert result == '{"action":"git_status"}'
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    budget_entry = next(item for item in entries if item.get("provider") == "cloud" and item["event"] == "budget_limited")
    assert budget_entry["status_code"] == 402
    assert not any(
        item.get("provider") == "cloud" and item["event"] == "failure"
        for item in entries
    )


def test_shipped_openrouter_profile_caps_output_tokens():
    config = json.loads((Path(__file__).parent / "fixtures" / "tornado_config.json").read_text(encoding="utf-8"))
    provider = next(
        item for item in config["tornado"]["providers"]
        if item["id"] == "openrouter-latest"
    )

    assert provider["max_tokens"] == 1024


def test_openai_compatible_provider_defaults_to_safe_output_cap():
    config = ProviderConfig.from_dict({
        "id": "cloud",
        "kind": "openai_compatible",
        "base_url": "https://example.invalid",
        "model": "model",
    })

    assert config.max_tokens == 1024


class InFlightBudgetHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({
            "error": {
                "message": "This request would exceed your available credits given your current in-flight requests.",
                "code": 402,
                "metadata": {
                    "reason": "in_flight_budget_exhausted",
                    "limit_source": "openrouter_in_flight_budget",
                },
            }
        }).encode("utf-8")
        self.send_response(402, "Payment Required")
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "0.25")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_in_flight_budget_402_is_retryable_and_captures_retry_after():
    server = HTTPServer(("127.0.0.1", 0), InFlightBudgetHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="cloud-model",
            timeout=5,
            use_response_format=False,
            max_tokens=1024,
        )
        with pytest.raises(TornadoError) as exc_info:
            client.chat([{"role": "user", "content": "hello"}])

        assert exc_info.value.status_code == 402
        assert exc_info.value.category == "transient_budget"
        assert exc_info.value.provider_reason == "in_flight_budget_exhausted"
        assert exc_info.value.retry_after_seconds == pytest.approx(0.25)
    finally:
        server.shutdown()


class SequencedProvider(FakeProvider):
    def __init__(self, provider_id, outcomes, *, local=False):
        super().__init__(provider_id, local=local)
        self.outcomes = list(outcomes)

    def chat(self, messages):
        self.calls.append(messages)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.parametrize("category", ["transient", "rate_limit", "transient_budget", "budget"])
def test_local_fallback_before_wait_promotes_eligible_local_after_recoverable_cloud_failure(
    tmp_path: Path,
    category: str,
):
    sleepers = []
    failure = TornadoError("capacity unavailable", category=category)
    first_cloud = SequencedProvider("first-cloud", [failure], local=False)
    second_cloud = FakeProvider("second-cloud", replies=['{"action":"git_diff"}'], local=False)
    local = FakeProvider("local", replies=['{"action":"git_status"}'], local=True)
    log_path = tmp_path / "tornado.log"
    client = TornadoClient.from_adapters(
        [
            (
                provider_config(
                    "first-cloud",
                    priority=100,
                    local=False,
                    transient_budget_retries=0,
                ),
                first_cloud,
            ),
            (provider_config("second-cloud", priority=90, local=False), second_cloud),
            (provider_config("local", priority=80, local=True), local),
        ],
        state_path=tmp_path / "state.json",
        log_path=log_path,
        sleeper=sleepers.append,
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_status"}'
    assert len(first_cloud.calls) == 1
    assert second_cloud.calls == []
    assert len(local.calls) == 1
    assert sleepers == []
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(
        entry == {
            "ts": entry["ts"],
            "event": "local_fallback_promoted",
            "failed_provider": "first-cloud",
            "local_provider": "local",
            "category": category,
        }
        for entry in entries
    )
    assert not any(entry["event"] == "provider_wait" for entry in entries)


def test_local_fallback_before_wait_can_preserve_cloud_precedence_when_disabled(tmp_path: Path):
    first_cloud = SequencedProvider(
        "first-cloud",
        [TornadoError("capacity unavailable", category="transient")],
        local=False,
    )
    second_cloud = FakeProvider("second-cloud", replies=['{"action":"git_diff"}'], local=False)
    local = FakeProvider("local", replies=['{"action":"git_status"}'], local=True)
    client = TornadoClient.from_adapters(
        [
            (provider_config("first-cloud", priority=100, local=False), first_cloud),
            (provider_config("second-cloud", priority=90, local=False), second_cloud),
            (provider_config("local", priority=80, local=True), local),
        ],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
        local_fallback_before_wait=False,
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_diff"}'
    assert len(first_cloud.calls) == 1
    assert len(second_cloud.calls) == 1
    assert local.calls == []


def test_local_fallback_before_wait_continues_to_next_cloud_after_promoted_local_fails(
    tmp_path: Path,
):
    first_cloud = SequencedProvider(
        "first-cloud",
        [TornadoError("capacity unavailable", category="transient")],
        local=False,
    )
    local = FakeProvider("local", error=RuntimeError("local unavailable"), local=True)
    second_cloud = FakeProvider("second-cloud", replies=['{"action":"git_diff"}'], local=False)
    client = TornadoClient.from_adapters(
        [
            (provider_config("first-cloud", priority=100, local=False), first_cloud),
            (provider_config("second-cloud", priority=90, local=False), second_cloud),
            (provider_config("local", priority=80, local=True), local),
        ],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_diff"}'
    assert len(first_cloud.calls) == 1
    assert len(local.calls) == 1
    assert len(second_cloud.calls) == 1


def test_local_fallback_before_wait_promotes_remaining_eligible_local_lane(tmp_path: Path):
    first_cloud = SequencedProvider(
        "first-cloud",
        [TornadoError("capacity unavailable", category="transient")],
        local=False,
    )
    second_cloud = FakeProvider("second-cloud", replies=['{"action":"git_diff"}'], local=False)
    ineligible_local = FakeProvider("ineligible-local", replies=['{"action":"list_files"}'], local=True)
    eligible_local = FakeProvider("eligible-local", replies=['{"action":"git_status"}'], local=True)
    log_path = tmp_path / "tornado.log"
    client = TornadoClient.from_adapters(
        [
            (provider_config("first-cloud", priority=100, local=False), first_cloud),
            (provider_config("second-cloud", priority=90, local=False), second_cloud),
            (provider_config("ineligible-local", priority=85, local=True, budget=0), ineligible_local),
            (provider_config("eligible-local", priority=80, local=True), eligible_local),
        ],
        state_path=tmp_path / "state.json",
        log_path=log_path,
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_status"}'
    assert len(first_cloud.calls) == 1
    assert second_cloud.calls == []
    assert ineligible_local.calls == []
    assert len(eligible_local.calls) == 1
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(
        entry["event"] == "local_fallback_promoted"
        and entry["failed_provider"] == "first-cloud"
        and entry["local_provider"] == "eligible-local"
        for entry in entries
    )


def test_local_fallback_before_wait_defaults_enabled_and_reads_config_override(tmp_path: Path):
    config = {
        "tornado": {
            "state_path": str(tmp_path / "state.json"),
            "log_path": str(tmp_path / "tornado.log"),
            "providers": [
                {
                    "id": "local",
                    "kind": "lm_studio",
                    "base_url": "http://127.0.0.1:1234",
                    "model": "model",
                    "local": True,
                }
            ],
        }
    }

    assert TornadoClient.from_config(config).local_fallback_before_wait is True
    config["tornado"]["local_fallback_before_wait"] = False
    assert TornadoClient.from_config(config).local_fallback_before_wait is False


def test_transient_budget_retries_same_provider_before_fallback(tmp_path: Path, monkeypatch):
    waits = []
    monkeypatch.setattr(time, "sleep", lambda seconds: waits.append(seconds))
    transient = TornadoError(
        "HTTP 402: Payment Required",
        status_code=402,
        category="transient_budget",
        retry_after_seconds=0.25,
        provider_reason="in_flight_budget_exhausted",
    )
    cloud = SequencedProvider(
        "cloud",
        [transient, '{"action":"git_status"}'],
        local=False,
    )
    local = FakeProvider("local", replies=['{"action":"list_files"}'], local=True)
    log_path = tmp_path / "tornado.log"
    client = TornadoClient.from_adapters(
        [
            (provider_config("cloud", priority=100, local=False), cloud),
            (provider_config("local", priority=50, local=True), local),
        ],
        state_path=tmp_path / "state.json",
        log_path=log_path,
    )

    result = client.chat([{"role": "user", "content": "hello"}])

    assert result == '{"action":"git_status"}'
    assert len(cloud.calls) == 2
    assert local.calls == []
    assert waits == [pytest.approx(0.25)]
    state = client.state.snapshot("cloud")
    assert state["consecutive_failures"] == 0
    assert state["total_failures"] == 0
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(item["event"] == "budget_retry" and item["provider"] == "cloud" for item in entries)


def test_transient_budget_falls_back_only_after_retry_budget_exhausted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    transient = TornadoError(
        "HTTP 402: Payment Required",
        status_code=402,
        category="transient_budget",
        retry_after_seconds=0.0,
        provider_reason="in_flight_budget_exhausted",
    )
    cloud = SequencedProvider("cloud", [transient, transient, transient], local=False)
    local = FakeProvider("local", replies=['{"action":"git_status"}'], local=True)
    log_path = tmp_path / "tornado.log"
    client = TornadoClient.from_adapters(
        [
            (provider_config("cloud", priority=100, local=False), cloud),
            (provider_config("local", priority=50, local=True), local),
        ],
        state_path=tmp_path / "state.json",
        log_path=log_path,
    )

    result = client.chat([{"role": "user", "content": "hello"}])

    assert result == '{"action":"git_status"}'
    assert len(cloud.calls) == 3
    assert len(local.calls) == 1
    state = client.state.snapshot("cloud")
    assert state["consecutive_failures"] == 0
    assert state["total_failures"] == 0
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(
        item["event"] == "budget_retry_exhausted" and item["provider"] == "cloud"
        for item in entries
    )

class CustomPathCaptureHandler(BaseHTTPRequestHandler):
    paths = []
    request_payload = None

    def do_GET(self):
        CustomPathCaptureHandler.paths.append(self.path)
        body = json.dumps({"data": [{"id": "override-model"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        CustomPathCaptureHandler.paths.append(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        CustomPathCaptureHandler.request_payload = json.loads(self.rfile.read(length).decode("utf-8"))
        body = json.dumps({"choices": [{"message": {"content": '{"action":"git_status"}'}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_provider_config_expands_environment_variables_in_base_url(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    config = ProviderConfig.from_dict({
        "id": "cloudflare",
        "kind": "openai_compatible",
        "base_url": "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        "model": "@cf/zai-org/glm-4.7-flash",
        "api_key_env": "CLOUDFLARE_API_TOKEN",
        "required_envs": ["CLOUDFLARE_ACCOUNT_ID"],
    })

    assert config.base_url == "https://api.cloudflare.com/client/v4/accounts/acct-123/ai/v1"
    assert config.required_envs == ("CLOUDFLARE_ACCOUNT_ID",)


def test_provider_with_missing_required_environment_is_not_effectively_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token-present")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    config = ProviderConfig.from_dict({
        "id": "cloudflare",
        "kind": "openai_compatible",
        "base_url": "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        "model": "@cf/zai-org/glm-4.7-flash",
        "api_key_env": "CLOUDFLARE_API_TOKEN",
        "required_envs": ["CLOUDFLARE_ACCOUNT_ID"],
        "enabled": False,
        "auto_enable_if_key_present": True,
    })
    client = TornadoClient.from_adapters(
        [(config, SequencedProvider("cloudflare", ['{"action":"finish"}']))],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    assert client._effectively_enabled(config) is False


def test_provider_config_reads_custom_paths_model_env_and_retry_overrides(monkeypatch):
    monkeypatch.setenv("CLOUD_MODEL", "override-model")
    config = ProviderConfig.from_dict({
        "id": "cloud",
        "kind": "openai_compatible",
        "base_url": "https://example.invalid",
        "model": "default-model",
        "model_env": "CLOUD_MODEL",
        "models_path": "/catalog/models",
        "chat_path": "/openai/chat/completions",
        "transient_retry_seconds": 7,
        "rate_limit_default_retry_seconds": 90,
        "budget_probe_seconds": 2400,
    })

    assert config.model == "override-model"
    assert config.model_env == "CLOUD_MODEL"
    assert config.models_path == "/catalog/models"
    assert config.chat_path == "/openai/chat/completions"
    assert config.transient_retry_seconds == 7.0
    assert config.rate_limit_default_retry_seconds == 90.0
    assert config.budget_probe_seconds == 2400.0


def test_openai_compatible_client_uses_custom_paths():
    CustomPathCaptureHandler.paths = []
    CustomPathCaptureHandler.request_payload = None
    server = HTTPServer(("127.0.0.1", 0), CustomPathCaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="override-model",
            timeout=5,
            use_response_format=False,
            models_path="/catalog/models",
            chat_path="/openai/chat/completions",
        )
        assert client.model_available()
        assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_status"}'
        assert CustomPathCaptureHandler.paths == ["/catalog/models", "/openai/chat/completions"]
    finally:
        server.shutdown()


def test_retry_after_numeric_is_normalized_to_seconds():
    from hcs_ai.local_codex.tornado import _parse_retry_delay
    assert _parse_retry_delay({"Retry-After": "12"}, now=1_800_000_000.0) == pytest.approx(12.0)


def test_retry_after_http_date_is_normalized_to_seconds():
    from email.utils import formatdate
    from hcs_ai.local_codex.tornado import _parse_retry_delay
    now = 1_800_000_000.0
    value = formatdate(now + 60.0, usegmt=True)
    assert _parse_retry_delay({"Retry-After": value}, now=now) == pytest.approx(60.0)


def test_x_rate_limit_reset_epoch_is_normalized_to_seconds():
    from hcs_ai.local_codex.tornado import _parse_retry_delay
    assert _parse_retry_delay({"X-RateLimit-Reset": "1800000060"}, now=1_800_000_000.0) == pytest.approx(60.0)


def test_x_rate_limit_reset_duration_suffix_is_normalized_to_seconds():
    from hcs_ai.local_codex.tornado import _parse_retry_delay
    now = 1_800_000_000.0
    assert _parse_retry_delay({"X-RateLimit-Reset-Requests": "1500ms"}, now=now) == pytest.approx(1.5)
    assert _parse_retry_delay({"X-RateLimit-Reset-Tokens": "2m"}, now=now) == pytest.approx(120.0)


def test_malformed_reset_metadata_is_ignored():
    from hcs_ai.local_codex.tornado import _parse_retry_delay
    assert _parse_retry_delay({"Retry-After": "not-a-time"}, now=1_800_000_000.0) is None


def test_failure_classification_maps_http_and_lm_studio_failures():
    from hcs_ai.local_codex.lm_client import LMStudioError
    from hcs_ai.local_codex.tornado import _classify_failure

    assert _classify_failure(TornadoError("HTTP 429", status_code=429)).category == "rate_limit"
    assert _classify_failure(TornadoError("HTTP 503", status_code=503)).category == "transient"
    assert _classify_failure(TornadoError("bad key", status_code=401)).category == "auth"
    assert _classify_failure(TornadoError("forbidden", status_code=403)).category == "auth"
    assert _classify_failure(
        LMStudioError('HTTP 400: Bad Request: {"error":"Engine protocol predict request failed: fetch failed"}')
    ).category == "transient"

class MutableClock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def time(self):
        return self.now


def test_legacy_state_gets_retry_state_defaults_lazily(tmp_path: Path):
    state_path = tmp_path / "legacy.json"
    state_path.write_text(json.dumps({
        "providers": {
            "p": {
                "consecutive_failures": 2,
                "total_successes": 1,
                "total_failures": 2,
                "total_calls": 3,
                "last_error": "old",
                "cooldown_until": 0.0,
                "last_latency_seconds": 1.2,
                "role_outcomes": {},
            }
        }
    }), encoding="utf-8")

    store = TornadoStateStore(state_path)
    state = store.snapshot("p")

    assert state["last_success_at"] is None
    assert state["last_failure_at"] is None
    assert state["last_failure_category"] is None
    assert state["retry_at"] == 0.0
    assert state["budget_blocked_since"] is None
    assert state["budget_recovery_samples"] == []
    assert state["learned_budget_recovery_seconds"] is None
    assert state["learned_budget_recovery_confidence"] == 0.0
    assert state["consecutive_failures"] == 2


def test_retry_state_schedule_uses_injected_clock(tmp_path: Path):
    clock = MutableClock(1000.0)
    store = TornadoStateStore(tmp_path / "state.json", clock=clock.time)

    store.schedule_retry("p", category="transient", retry_after_seconds=5)

    assert store.retry_at("p") == pytest.approx(1005.0)
    state = store.snapshot("p")
    assert state["last_failure_at"] == pytest.approx(1000.0)
    assert state["last_failure_category"] == "transient"


def test_budget_blocked_since_stays_anchored_across_failed_probes(tmp_path: Path):
    clock = MutableClock(1000.0)
    store = TornadoStateStore(tmp_path / "state.json", clock=clock.time)

    store.schedule_retry("p", category="budget", retry_after_seconds=100)
    clock.now = 1100.0
    store.schedule_retry("p", category="budget", retry_after_seconds=100)

    assert store.snapshot("p")["budget_blocked_since"] == pytest.approx(1000.0)


def test_recovery_learning_reaches_full_confidence_after_minimum_samples(tmp_path: Path):
    clock = MutableClock(1000.0)
    store = TornadoStateStore(tmp_path / "state.json", clock=clock.time)

    for index, elapsed in enumerate((100.0, 120.0, 110.0), start=1):
        start = clock.now
        store.schedule_retry("p", category="budget", retry_after_seconds=elapsed)
        clock.now = start + elapsed
        store.record_success(
            "p",
            latency_seconds=1.0,
            budget_learning_min_samples=3,
            budget_learning_alpha=0.3,
        )
        state = store.snapshot("p")
        assert state["budget_blocked_since"] is None
        assert len(state["budget_recovery_samples"]) == index
        if index < 3:
            assert state["learned_budget_recovery_confidence"] < 1.0

    state = store.snapshot("p")
    assert state["learned_budget_recovery_seconds"] > 0
    assert state["learned_budget_recovery_confidence"] == pytest.approx(1.0)
    assert state["retry_at"] == 0.0
    assert state["last_failure_category"] is None

class FakeTime:
    def __init__(self, now=1000.0):
        self.now = float(now)
        self.sleeps = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


def _client_with_fake_time(tmp_path, providers, fake_time, **kwargs):
    return TornadoClient.from_adapters(
        providers,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
        clock=fake_time.time,
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
        **kwargs,
    )


def test_failover_happens_before_waiting(tmp_path: Path):
    fake_time = FakeTime()
    first = SequencedProvider("first", [TornadoError("temporary", category="transient")])
    second = SequencedProvider("second", ['{"action":"git_status"}'])
    client = _client_with_fake_time(
        tmp_path,
        [(provider_config("first", priority=100), first), (provider_config("second", priority=50), second)],
        fake_time,
    )

    assert client.chat([{"role": "user", "content": "hello"}]) == '{"action":"git_status"}'
    assert fake_time.sleeps == []
    assert len(second.calls) == 1


def test_wait_and_recover_continues_same_chat(tmp_path: Path):
    fake_time = FakeTime()
    provider = SequencedProvider(
        "p",
        [TornadoError("temporary", category="transient"), '{"action":"finish"}'],
    )
    client = _client_with_fake_time(
        tmp_path,
        [(provider_config("p"), provider)],
        fake_time,
        transient_retry_seconds=5,
    )
    messages = [{"role": "user", "content": "same prompt"}]

    assert client.chat(messages) == '{"action":"finish"}'
    assert fake_time.sleeps == [pytest.approx(5.0)]
    assert provider.calls == [messages, messages]


def test_rate_limit_explicit_reset_and_default_are_respected(tmp_path: Path):
    explicit_time = FakeTime()
    explicit = SequencedProvider(
        "explicit",
        [TornadoError("429", status_code=429, category="rate_limit", retry_after_seconds=120), '{"action":"finish"}'],
    )
    client = _client_with_fake_time(
        tmp_path / "explicit",
        [(provider_config("explicit"), explicit)],
        explicit_time,
        rate_limit_default_retry_seconds=60,
        max_wait_poll_seconds=300,
    )
    assert client.chat([{"role": "user", "content": "x"}]) == '{"action":"finish"}'
    assert explicit_time.sleeps == [pytest.approx(120.0)]

    default_time = FakeTime()
    default = SequencedProvider(
        "default",
        [TornadoError("429", status_code=429, category="rate_limit"), '{"action":"finish"}'],
    )
    client = _client_with_fake_time(
        tmp_path / "default",
        [(provider_config("default"), default)],
        default_time,
        rate_limit_default_retry_seconds=60,
    )
    assert client.chat([{"role": "user", "content": "x"}]) == '{"action":"finish"}'
    assert default_time.sleeps == [pytest.approx(60.0)]


def test_transient_backoff_is_exponential(tmp_path: Path):
    fake_time = FakeTime()
    provider = SequencedProvider(
        "p",
        [
            TornadoError("temp1", category="transient"),
            TornadoError("temp2", category="transient"),
            '{"action":"finish"}',
        ],
    )
    client = _client_with_fake_time(
        tmp_path,
        [(provider_config("p"), provider)],
        fake_time,
        transient_retry_seconds=5,
        max_wait_poll_seconds=300,
    )

    assert client.chat([{"role": "user", "content": "x"}]) == '{"action":"finish"}'
    assert fake_time.sleeps == [pytest.approx(5.0), pytest.approx(10.0)]


def test_unknown_budget_uses_slow_probe_interval(tmp_path: Path):
    fake_time = FakeTime()
    provider = SequencedProvider(
        "p",
        [TornadoError("credits", status_code=402, category="budget"), '{"action":"finish"}'],
    )
    client = _client_with_fake_time(
        tmp_path,
        [(provider_config("p", local=False), provider)],
        fake_time,
        budget_probe_seconds=1800,
        max_wait_poll_seconds=300,
    )

    assert client.chat([{"role": "user", "content": "x"}]) == '{"action":"finish"}'
    assert sum(fake_time.sleeps) == pytest.approx(1800.0)
    assert all(seconds <= 300.0 for seconds in fake_time.sleeps)


def test_explicit_budget_reset_wins_over_learned_prediction(tmp_path: Path):
    fake_time = FakeTime()
    state_path = tmp_path / "state.json"
    store = TornadoStateStore(state_path, clock=fake_time.time)
    state = store._provider("p")
    state["budget_recovery_samples"] = [90.0, 100.0, 110.0]
    state["learned_budget_recovery_seconds"] = 100.0
    state["learned_budget_recovery_confidence"] = 1.0
    store.save()
    provider = SequencedProvider(
        "p",
        [TornadoError("credits", status_code=402, category="budget", retry_after_seconds=600), '{"action":"finish"}'],
    )
    client = TornadoClient.from_adapters(
        [(provider_config("p", local=False), provider)],
        state_path=state_path,
        log_path=tmp_path / "tornado.log",
        clock=fake_time.time,
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
        budget_probe_seconds=1800,
        budget_learning_min_samples=3,
        max_wait_poll_seconds=300,
    )

    assert client.chat([{"role": "user", "content": "x"}]) == '{"action":"finish"}'
    assert sum(fake_time.sleeps) == pytest.approx(600.0)


@pytest.mark.parametrize("category", ["auth", "config"])
def test_nonrecoverable_categories_do_not_wait(tmp_path: Path, category):
    fake_time = FakeTime()
    provider = SequencedProvider("p", [TornadoError("nope", category=category)])
    client = _client_with_fake_time(tmp_path, [(provider_config("p"), provider)], fake_time)

    with pytest.raises(TornadoError):
        client.chat([{"role": "user", "content": "x"}])
    assert fake_time.sleeps == []


def test_session_call_cap_is_not_bypassed_by_waiting(tmp_path: Path):
    fake_time = FakeTime()
    provider = SequencedProvider("p", [TornadoError("temporary", category="transient")])
    client = _client_with_fake_time(
        tmp_path,
        [(provider_config("p", budget=1), provider)],
        fake_time,
    )

    with pytest.raises(TornadoError):
        client.chat([{"role": "user", "content": "x"}])
    assert fake_time.sleeps == []


def test_wait_for_providers_false_preserves_immediate_failure(tmp_path: Path):
    fake_time = FakeTime()
    provider = SequencedProvider("p", [TornadoError("temporary", category="transient")])
    client = _client_with_fake_time(
        tmp_path,
        [(provider_config("p"), provider)],
        fake_time,
        wait_for_providers=False,
    )

    with pytest.raises(TornadoError):
        client.chat([{"role": "user", "content": "x"}])
    assert fake_time.sleeps == []
    assert client.state.retry_at("p") == pytest.approx(1005.0)


def test_retry_schedule_persists_across_restart(tmp_path: Path):
    fake_time = FakeTime()
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "tornado.log"
    first = SequencedProvider("p", [TornadoError("temporary", category="transient")])
    client_a = TornadoClient.from_adapters(
        [(provider_config("p"), first)],
        state_path=state_path,
        log_path=log_path,
        clock=fake_time.time,
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
        wait_for_providers=False,
        transient_retry_seconds=5,
    )
    with pytest.raises(TornadoError):
        client_a.chat([{"role": "user", "content": "x"}])

    second = SequencedProvider("p", ['{"action":"finish"}'])
    client_b = TornadoClient.from_adapters(
        [(provider_config("p"), second)],
        state_path=state_path,
        log_path=log_path,
        clock=fake_time.time,
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
        transient_retry_seconds=5,
    )
    assert client_b.chat([{"role": "user", "content": "x"}]) == '{"action":"finish"}'
    assert fake_time.sleeps == [pytest.approx(5.0)]
    assert len(second.calls) == 1


def test_wait_status_and_recovery_events_are_emitted(tmp_path: Path):
    fake_time = FakeTime()
    statuses = []
    provider = SequencedProvider(
        "p",
        [TornadoError("temporary", category="transient"), '{"action":"finish"}'],
    )
    log_path = tmp_path / "tornado.log"
    client = TornadoClient.from_adapters(
        [(provider_config("p"), provider)],
        state_path=tmp_path / "state.json",
        log_path=log_path,
        clock=fake_time.time,
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
        status_callback=statuses.append,
        transient_retry_seconds=5,
    )

    client.chat([{"role": "user", "content": "secret prompt text"}])

    assert statuses == ["Tornado waiting 5s for provider capacity"]
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    events = {entry["event"] for entry in entries}
    assert {"provider_retry_scheduled", "provider_wait", "provider_recovered"} <= events
    assert "secret prompt text" not in log_path.read_text(encoding="utf-8")


def test_shipped_provider_catalog_prioritizes_free_capacity_and_locks_paid_profiles():
    config = json.loads((Path(__file__).parent / "fixtures" / "tornado_config.json").read_text(encoding="utf-8"))
    providers = {item["id"]: item for item in config["tornado"]["providers"]}

    free_profiles = {
        "openrouter-free",
        "groq-oss-120b",
        "groq-oss-20b",
        "groq-qwen3.8-27b",
        "groq-qwen3.6-27b",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "cloudflare-glm-4.7-flash",
        "cohere-command-a-plus",
        "alibaba-qwen3.6-plus",
        "alibaba-qwen3.7-max",
        "alibaba-qwen3.8-max",
        "huggingface-gpt-oss-120b",
        "nvidia-nemotron-3-ultra",
        "cerebras-gpt-oss-120b",
        "mistral-large",
    }
    assert free_profiles <= providers.keys()

    for provider_id in free_profiles:
        provider = providers[provider_id]
        assert provider["enabled"] is False
        assert provider["auto_enable_if_key_present"] is True
        assert provider["api_key_env"]
        assert int(provider.get("max_tokens", 1024)) <= 1024
        assert provider.get("model_env")
        serialized = json.dumps(provider).lower()
        assert "bearer " not in serialized
        assert "sk-" not in serialized

    paid_or_prepaid_profiles = {
        "openai-terra",
        "openrouter-latest",
        "together-gpt-oss-120b",
        "fireworks-gpt-oss-120b",
        "deepinfra-deepseek-v3",
        "sambanova-gpt-oss-120b",
    }
    assert paid_or_prepaid_profiles <= providers.keys()
    for provider_id in paid_or_prepaid_profiles:
        provider = providers[provider_id]
        assert provider["enabled"] is False
        assert provider["auto_enable_if_key_present"] is False

    assert providers["openrouter-free"]["model"] == "openrouter/free"
    assert providers["cloudflare-glm-4.7-flash"]["required_envs"] == ["CLOUDFLARE_ACCOUNT_ID"]



def test_shipped_provider_catalog_has_resilience_defaults():
    config = json.loads((Path(__file__).parent / "fixtures" / "tornado_config.json").read_text(encoding="utf-8"))
    tornado = config["tornado"]
    assert tornado["wait_for_providers"] is True
    assert tornado["max_wait_poll_seconds"] == 300
    assert tornado["transient_retry_seconds"] == 5
    assert tornado["rate_limit_default_retry_seconds"] == 60
    assert tornado["budget_probe_seconds"] == 1800
    assert tornado["budget_learning_min_samples"] == 3
    assert tornado["budget_learning_alpha"] == 0.3


def test_learned_budget_prediction_requires_minimum_samples(tmp_path: Path):
    fake_time = FakeTime()
    state_path = tmp_path / "state.json"
    store = TornadoStateStore(state_path, clock=fake_time.time)
    state = store._provider("p")
    state["budget_recovery_samples"] = [90.0, 110.0]
    state["learned_budget_recovery_seconds"] = 100.0
    state["learned_budget_recovery_confidence"] = 2 / 3
    store.save()
    provider = SequencedProvider(
        "p",
        [TornadoError("credits", status_code=402, category="budget"), '{"action":"finish"}'],
    )
    client = TornadoClient.from_adapters(
        [(provider_config("p", local=False), provider)],
        state_path=state_path,
        log_path=tmp_path / "tornado.log",
        clock=fake_time.time,
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
        budget_probe_seconds=1800,
        budget_learning_min_samples=3,
        max_wait_poll_seconds=300,
    )

    assert client.chat([{"role": "user", "content": "x"}]) == '{"action":"finish"}'
    assert sum(fake_time.sleeps) == pytest.approx(1800.0)


def test_trusted_budget_prediction_shortens_probe_and_logs_learning_events(tmp_path: Path):
    fake_time = FakeTime()
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "tornado.log"
    store = TornadoStateStore(state_path, clock=fake_time.time)
    state = store._provider("p")
    state["budget_recovery_samples"] = [90.0, 100.0, 110.0]
    state["learned_budget_recovery_seconds"] = 100.0
    state["learned_budget_recovery_confidence"] = 1.0
    store.save()
    provider = SequencedProvider(
        "p",
        [TornadoError("credits", status_code=402, category="budget"), '{"action":"finish"}'],
    )
    client = TornadoClient.from_adapters(
        [(provider_config("p", local=False), provider)],
        state_path=state_path,
        log_path=log_path,
        clock=fake_time.time,
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
        budget_probe_seconds=1800,
        budget_learning_min_samples=3,
        max_wait_poll_seconds=300,
    )

    assert client.chat([{"role": "user", "content": "x"}]) == '{"action":"finish"}'
    assert sum(fake_time.sleeps) == pytest.approx(100.0)
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    events = {entry["event"] for entry in entries}
    assert "budget_recovery_prediction" in events
    assert "budget_recovery_observed" in events


def test_status_snapshot_reports_provider_health_without_secret_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STATUS_TEST_KEY", "super-secret-token")
    monkeypatch.setenv("STATUS_ACCOUNT", "account-123")
    now = 1000.0
    config = ProviderConfig(
        provider_id="cloud-status",
        kind="openai_compatible",
        base_url="http://example.invalid",
        model="status-model",
        priority=50,
        timeout=10,
        local=False,
        api_key_env="STATUS_TEST_KEY",
        enabled=False,
        auto_enable_if_key_present=True,
        required_envs=("STATUS_ACCOUNT",),
    )
    client = TornadoClient.from_adapters(
        [(config, FakeProvider("cloud-status", local=False))],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
        clock=lambda: now,
    )
    client.state.schedule_retry(
        "cloud-status",
        category="rate_limit",
        retry_after_seconds=60,
        error_message="rate limited",
    )

    snapshot = client.status_snapshot()

    assert snapshot["provider_count"] == 1
    lane = snapshot["providers"][0]
    assert lane["provider"] == "cloud-status"
    assert lane["model"] == "status-model"
    assert lane["configured_enabled"] is False
    assert lane["effective_enabled"] is True
    assert lane["api_key_env"] == "STATUS_TEST_KEY"
    assert lane["api_key_present"] is True
    assert lane["required_envs"] == ["STATUS_ACCOUNT"]
    assert lane["required_envs_present"] is True
    assert lane["eligible"] is False
    assert lane["health_state"] == "waiting_retry"
    assert lane["retry_at"] == 1060.0
    assert lane["retry_in_seconds"] == 60.0
    assert lane["last_failure_category"] == "rate_limit"
    assert lane["last_error"] == "rate limited"
    assert "super-secret-token" not in json.dumps(snapshot)
    assert "account-123" not in json.dumps(snapshot)


@pytest.mark.parametrize("content", [None, "", "   "])
def test_openai_compatible_client_rejects_null_or_blank_content(content):
    client = OpenAICompatibleClient(
        base_url="http://example.invalid",
        model="cloud-model",
        api_key_env=None,
    )
    client._request_json = lambda *args, **kwargs: {
        "choices": [{"message": {"content": content}}]
    }

    with pytest.raises(TornadoError, match="invalid chat-completion response"):
        client.chat([{"role": "user", "content": "hello"}])


def test_probe_eligible_cloud_tests_each_eligible_cloud_lane_once_and_skips_local(tmp_path: Path, monkeypatch):
    local = FakeProvider("local", replies=['{"action":"finish","summary":"probe","tests":[]}'], local=True)
    cloud_ok = FakeProvider("cloud-ok", replies=['{"action":"finish","summary":"probe","tests":[]}'], local=False)
    cloud_fail = FakeProvider("cloud-fail", error=TornadoError("HTTP 401", status_code=401, category="auth"), local=False)
    monkeypatch.setenv("PROBE_OK_KEY", "ok-secret")
    monkeypatch.setenv("PROBE_FAIL_KEY", "fail-secret")

    def cloud_config(provider_id, key_env):
        return ProviderConfig(
            provider_id=provider_id,
            kind="openai_compatible",
            base_url="http://example.invalid",
            model=f"{provider_id}-model",
            local=False,
            api_key_env=key_env,
            enabled=False,
            auto_enable_if_key_present=True,
            max_calls_per_session=10,
        )

    client = TornadoClient.from_adapters(
        [
            (provider_config("local", local=True), local),
            (cloud_config("cloud-ok", "PROBE_OK_KEY"), cloud_ok),
            (cloud_config("cloud-fail", "PROBE_FAIL_KEY"), cloud_fail),
        ],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    result = client.probe_eligible_cloud()

    assert result["eligible_cloud_count"] == 2
    assert result["attempted_count"] == 2
    assert result["success_count"] == 1
    assert result["failure_count"] == 1
    assert local.calls == []
    assert len(cloud_ok.calls) == 1
    assert len(cloud_fail.calls) == 1
    assert len(cloud_ok.calls[0]) <= 2
    by_provider = {item["provider"]: item for item in result["providers"]}
    assert by_provider["cloud-ok"]["ok"] is True
    assert by_provider["cloud-fail"]["ok"] is False
    assert by_provider["cloud-fail"]["category"] == "auth"


def test_probe_redacts_credential_values_from_errors_and_logs(tmp_path: Path, monkeypatch):
    secret = "super-secret-probe-token"
    monkeypatch.setenv("PROBE_SECRET", secret)
    leaking = FakeProvider(
        "cloud",
        error=TornadoError(f"authorization failed for {secret}", status_code=401, category="auth"),
        local=False,
    )
    config = ProviderConfig(
        provider_id="cloud",
        kind="openai_compatible",
        base_url="http://example.invalid",
        model="cloud-model",
        local=False,
        api_key_env="PROBE_SECRET",
        enabled=False,
        auto_enable_if_key_present=True,
    )
    client = TornadoClient.from_adapters(
        [(config, leaking)],
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "tornado.log",
    )

    result = client.probe_eligible_cloud()

    serialized = json.dumps(result)
    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert secret not in (tmp_path / "tornado.log").read_text(encoding="utf-8")
    assert secret not in (tmp_path / "state.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("operation", ["chat", "controller", "availability", "probe"])
@pytest.mark.parametrize("category", ["provider", "auth", "transient", "rate_limit", "budget", "transient_budget"])
def test_provider_failures_redact_credentials_at_every_output_boundary(tmp_path, monkeypatch, operation, category):
    from hcs_ai.local_codex.actions import ActionExecutor
    from hcs_ai.local_codex.controller import AgentController
    from hcs_ai.local_codex.state import AgentStatus, TaskJournal
    from hcs_ai.local_codex.workspace import Workspace

    token = "private-provider-token"
    account = "private-provider-account"
    monkeypatch.setenv("FAILURE_TEST_KEY", token)
    monkeypatch.setenv("FAILURE_TEST_ACCOUNT", account)
    failure = TornadoError(
        f"provider rejected {token} for {account}", category=category,
        provider_reason=f"credential {token}, account {account}",
    )

    class LeakingProvider(FakeProvider):
        def model_available(self):
            raise failure

    config = ProviderConfig(
        provider_id="cloud", kind="openai_compatible", base_url="http://example.invalid",
        model="test", api_key_env="FAILURE_TEST_KEY", required_envs=("FAILURE_TEST_ACCOUNT",),
        transient_budget_retries=1, transient_budget_default_delay_seconds=0,
    )
    events = []
    client = TornadoClient.from_adapters(
        [(config, LeakingProvider("cloud", error=failure, local=False))],
        state_path=tmp_path / "state.json", log_path=tmp_path / "tornado.log",
        wait_for_providers=False, status_callback=events.append,
    )
    if operation == "availability":
        assert client.model_available() is False
    elif operation == "probe":
        events.append(json.dumps(client.probe_eligible_cloud()))
    elif operation == "controller":
        journal = TaskJournal.new("Inspect files", str(tmp_path), "test", tmp_path / "journals")
        result = AgentController(client, ActionExecutor(Workspace(tmp_path), journal, True), journal, 3, status_callback=events.append).run()
        assert result.status is AgentStatus.BLOCKED
        events.append(journal.path.read_text(encoding="utf-8"))
    else:
        with pytest.raises(TornadoError) as raised:
            client.chat([{"role": "user", "content": "Inspect files"}])
        events.append(str(raised.value))

    artifacts = events + [
        json.dumps(client.status_snapshot()),
        (tmp_path / "tornado.log").read_text(encoding="utf-8"),
    ]
    if (tmp_path / "state.json").exists():
        artifacts.append((tmp_path / "state.json").read_text(encoding="utf-8"))
    for output in artifacts:
        assert token not in output
        assert account not in output
    assert "[REDACTED]" in "\n".join(artifacts)


class UserAgentCaptureHandler(BaseHTTPRequestHandler):
    user_agent = None

    def do_GET(self):
        UserAgentCaptureHandler.user_agent = self.headers.get("User-Agent")
        body = json.dumps({"data": [{"id": "cloud-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_openai_compatible_client_sends_local_codex_user_agent():
    server = HTTPServer(("127.0.0.1", 0), UserAgentCaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="cloud-model",
            timeout=5,
        )
        assert client.model_available()
        assert UserAgentCaptureHandler.user_agent is not None
        assert UserAgentCaptureHandler.user_agent.startswith("LocalCodex/")
    finally:
        server.shutdown()


class Cloudflare1010Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"error code: 1010"
        self.send_response(403, "Forbidden")
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_cloudflare_1010_is_classified_as_client_block_not_auth():
    server = HTTPServer(("127.0.0.1", 0), Cloudflare1010Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="cloud-model",
            timeout=5,
        )
        with pytest.raises(TornadoError) as exc_info:
            client.model_available()
        assert exc_info.value.status_code == 403
        assert exc_info.value.category == "client_block"
    finally:
        server.shutdown()


def test_shipped_mistral_free_lane_uses_small_model():
    config = json.loads((Path(__file__).parent / "fixtures" / "tornado_config.json").read_text(encoding="utf-8"))
    providers = {item["id"]: item for item in config["tornado"]["providers"]}
    assert providers["mistral-large"]["model"] == "mistral-small-latest"
