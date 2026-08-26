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
    assert calls == ["db", "audit"]
