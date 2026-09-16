from pathlib import Path

def test_make_client_uses_configured_timeout():
    from hcs_ai.local_codex.runtime import make_client

    client = make_client({
        "lm_studio_url": "http://127.0.0.1:1234",
        "model": "qwen2.5-3b-instruct",
        "lm_timeout_seconds": 777,
    })
    assert client.timeout == 777


def test_make_client_builds_tornado_when_enabled(tmp_path: Path):
    from hcs_ai.local_codex.tornado import TornadoClient
    from hcs_ai.local_codex.runtime import make_client

    client = make_client({
        "lm_studio_url": "http://127.0.0.1:1234",
        "model": "legacy",
        "lm_timeout_seconds": 600,
        "tornado": {
            "enabled": True,
            "state_path": str(tmp_path / "tornado_state.json"),
            "log_path": str(tmp_path / "tornado.log"),
            "providers": [{
                "id": "local",
                "kind": "lm_studio",
                "base_url": "http://127.0.0.1:1234",
                "model": "qwen2.5-3b-instruct",
                "priority": 100,
                "local": True,
            }],
        },
    })

    assert isinstance(client, TornadoClient)
    assert client.provider_ids == ["local"]


def test_make_client_keeps_legacy_lm_studio_when_tornado_absent():
    from hcs_ai.local_codex.lm_client import LMStudioClient
    from hcs_ai.local_codex.runtime import make_client

    client = make_client({
        "lm_studio_url": "http://127.0.0.1:1234",
        "model": "legacy",
        "lm_timeout_seconds": 321,
    })

    assert isinstance(client, LMStudioClient)
    assert client.timeout == 321


def test_make_client_forwards_tornado_status_callback(monkeypatch):
    from hcs_ai.local_codex import runtime as main_module

    callback = lambda message: None
    captured = {}
    sentinel = object()

    def fake_from_config(config, *, status_callback=None, **kwargs):
        captured["callback"] = status_callback
        return sentinel

    monkeypatch.setattr(main_module.TornadoClient, "from_config", fake_from_config)
    client = main_module.make_client(
        {"tornado": {"enabled": True, "providers": []}},
        status_callback=callback,
    )

    assert client is sentinel
    assert captured["callback"] is callback
