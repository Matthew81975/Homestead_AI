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


def _set_all_keys(monkeypatch):
    for name in ("A", "B", "C", "D"):
        monkeypatch.setenv(name, "test-key")


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
    _set_all_keys(monkeypatch)
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
    _set_all_keys(monkeypatch)
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
    _set_all_keys(monkeypatch)
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
    _set_all_keys(monkeypatch)
    monkeypatch.setattr(cloud_router, "load_config", _cfg)
    monkeypatch.setattr(cloud_router, "provider_factory", FakeProvider)
    cloud_router.reset_runtime_state()
    cloud_router.approve_tier_change("task-4", "medium")
    FakeProvider.outcomes = {"a": "x", "b": "x", "c": "x", "d": "continued"}
    result = cloud_router.chat("task-4", [{"role": "user", "content": "continue"}])
    assert result["tier"] == "medium"
    assert result["text"] == "continued"


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


def test_update_manifest_ships_cloud_modules():
    import json
    from pathlib import Path

    manifest = json.loads(Path("update_manifest.json").read_text(encoding="utf-8"))
    assert "hcs_ai/cloud_provider.py" in manifest["files"]
    assert "hcs_ai/cloud_router.py" in manifest["files"]


def test_default_config_contains_no_api_secret():
    from pathlib import Path

    text = Path("config.default.json").read_text(encoding="utf-8").lower()
    assert "sk-" not in text
    assert '"api_key":' not in text


def test_model_inventory_never_returns_secret_value(monkeypatch):
    import json

    monkeypatch.setattr(cloud_router, "load_config", _cfg)
    monkeypatch.setenv("A", "DO_NOT_LEAK_ME")
    cloud_router.reset_runtime_state()
    payload = json.dumps(cloud_router.model_inventory("task-secret"))
    assert "DO_NOT_LEAK_ME" not in payload


def test_cloud_status_counts_only_credential_ready_routes(monkeypatch):
    monkeypatch.setattr(cloud_router, "load_config", _cfg)
    monkeypatch.setenv("A", "ready")
    monkeypatch.delenv("B", raising=False)
    monkeypatch.delenv("C", raising=False)
    monkeypatch.delenv("D", raising=False)
    cloud_router.reset_runtime_state()
    status = cloud_router.cloud_status()
    assert status["configured_routes"] == 4
    assert status["healthy_routes"] == 1


def test_chat_skips_route_with_missing_credential(monkeypatch):
    monkeypatch.setattr(cloud_router, "load_config", _cfg)
    monkeypatch.setattr(cloud_router, "provider_factory", FakeProvider)
    monkeypatch.delenv("A", raising=False)
    monkeypatch.setenv("B", "ready")
    monkeypatch.delenv("C", raising=False)
    monkeypatch.delenv("D", raising=False)
    cloud_router.reset_runtime_state()
    FakeProvider.outcomes = {
        "a": "should not be used",
        "b": "usable",
        "c": "should not be used",
        "d": "should not be used",
    }
    result = cloud_router.chat("task-key-filter", [{"role": "user", "content": "hi"}])
    assert result["provider"] == "p2"
    assert result["text"] == "usable"
