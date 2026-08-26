import time

from hcs_ai import server


def test_server_startup_does_not_wait_for_model(monkeypatch):
    calls = []

    monkeypatch.setattr(server, "init_db", lambda: calls.append("db"))
    monkeypatch.setattr(server, "audit", lambda *args, **kwargs: calls.append("audit"))

    def slow_auto_start():
        time.sleep(0.5)
        calls.append("model")

    monkeypatch.setattr(server.engine, "auto_start", slow_auto_start)

    started = time.perf_counter()
    server.startup()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert calls[:2] == ["db", "audit"]


def test_inference_status_exposes_startup_phase_and_error(monkeypatch):
    monkeypatch.setattr(server.engine, "status", lambda: {
        "running": False,
        "ready": False,
        "phase": "failed",
        "error": "model failed to load",
    })

    result = server.inference_status()

    assert result["phase"] == "failed"
    assert result["error"] == "model failed to load"
