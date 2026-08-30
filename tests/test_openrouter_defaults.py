import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _cloud_defaults():
    config = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
    return config["cloud_ai"]


def test_default_config_includes_openrouter_route_without_secret():
    cloud = _cloud_defaults()
    route = next(r for r in cloud["routes"] if r["provider"] == "openrouter")

    assert cloud["enabled"] is True
    assert route["enabled"] is True
    assert route["base_url"] == "https://openrouter.ai/api/v1"
    assert route["api_key_env"] == "OPENROUTER_API_KEY"
    assert route["model"] == "openrouter/free"
    assert route["tier"] == cloud["default_tier"]
    assert "api_key" not in route
    assert not any("sk-or-" in str(value) for value in route.values())
