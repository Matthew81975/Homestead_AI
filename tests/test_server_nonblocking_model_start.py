import time

from hcs_ai import server_tree


def test_server_startup_does_not_wait_for_model(monkeypatch):
    calls = []

    monkeypatch.setattr(server_tree.base_server, "init_db", lambda: calls.append("db"))
    monkeypatch.setattr(server_tree.base_server, "audit", lambda *args, **kwargs: calls.append("audit"))

    def slow_auto_start():
        time.sleep(0.5)
        calls.append("model")

    monkeypatch.setattr(server_tree.base_server.engine, "auto_start", slow_auto_start)

    started = time.perf_counter()
    server_tree.nonblocking_startup()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert calls == ["db", "audit"]


def test_server_tree_replaces_blocking_startup_handler():
    assert server_tree.base_server.startup not in server_tree.app.router.on_startup
    assert server_tree.nonblocking_startup in server_tree.app.router.on_startup
