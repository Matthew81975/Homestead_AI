from types import SimpleNamespace

import pytest

import hcs_ai.desktop_host as desktop_host


class FakeLocalCodex:
    def __init__(self, order=None, pid=321):
        self.order = order if order is not None else []
        self.pid = pid
        self.start_calls = 0
        self.shutdown_calls = 0

    def start(self):
        self.start_calls += 1
        self.order.append("local_codex_start")

    def shutdown(self, grace_seconds=None):
        self.shutdown_calls += 1
        self.order.append("local_codex")

    def status(self):
        return {"pid": self.pid, "worker_state": "running" if self.pid else "stopped"}


class FakeApp:
    def __init__(self, order, local_codex_service=None, services=None):
        self.order = order
        self.services = services
        self.local_codex_service = (
            local_codex_service
            if local_codex_service is not None
            else (services.get("local_codex") if services is not None else None)
        )
        self.logs = []

    def _append_local_codex_log(self, message, tag):
        self.logs.append((message, tag))

    def after(self, _delay, callback):
        callback()

    def destroy(self):
        self.order.append("ui")

    def withdraw(self):
        pass

    def deiconify(self):
        pass

    def lift(self):
        pass

    def focus_force(self):
        pass


def configured(enabled=True):
    return {
        "app": {"version": "0.11.0"},
        "local_codex": {
            "enabled": enabled,
            "legacy_candidates": ["../Local_Codex_Agent_v2.10.8"],
        },
    }


def test_migration_runs_before_service_is_constructed(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(desktop_host, "ROOT", tmp_path)
    monkeypatch.setattr(desktop_host, "load_config", lambda: configured())
    monkeypatch.setattr(
        desktop_host,
        "migrate_legacy_install",
        lambda **kwargs: order.append("migration"),
    )
    monkeypatch.setattr(
        desktop_host,
        "LocalCodexService",
        lambda **kwargs: order.append("service") or FakeLocalCodex(order),
    )

    host = desktop_host.DesktopHost()

    assert order == ["migration", "service"]
    assert host.local_codex is not None


def test_hiding_window_keeps_local_codex_running(monkeypatch):
    monkeypatch.setattr(desktop_host, "load_config", lambda: configured(False))
    host = desktop_host.DesktopHost()
    local_codex = FakeLocalCodex()
    host.local_codex = local_codex
    host.app = FakeApp([])

    host.hide_window()

    assert local_codex.shutdown_calls == 0


def test_full_exit_stops_local_codex_before_hcs_server_and_ui(monkeypatch):
    order = []
    monkeypatch.setattr(desktop_host, "load_config", lambda: configured(False))
    monkeypatch.setattr(desktop_host, "terminate_process_tree", lambda pid: order.append("server" if pid == 100 else "worker"))
    host = desktop_host.DesktopHost()
    host.local_codex = FakeLocalCodex(order, pid=None)
    host.server = SimpleNamespace(pid=100, poll=lambda: None)
    host.app = FakeApp(order)

    host.exit()

    assert order == ["local_codex", "server", "ui"]


def test_surviving_worker_tree_uses_existing_process_tree_terminator(monkeypatch):
    order = []
    monkeypatch.setattr(desktop_host, "load_config", lambda: configured(False))
    monkeypatch.setattr(desktop_host, "terminate_process_tree", lambda pid: order.append(("terminate", pid)))
    host = desktop_host.DesktopHost()
    host.local_codex = FakeLocalCodex(order, pid=444)

    host._stop_children()

    assert order == ["local_codex", ("terminate", 444)]


def test_shutdown_is_idempotent(monkeypatch):
    monkeypatch.setattr(desktop_host, "load_config", lambda: configured(False))
    host = desktop_host.DesktopHost()
    host.local_codex = FakeLocalCodex()
    host.app = FakeApp([])

    host.exit()
    host.exit()

    assert host.local_codex.shutdown_calls == 1


def test_run_starts_worker_after_server_is_ready_and_passes_service_to_app(monkeypatch):
    order = []
    monkeypatch.setattr(desktop_host, "load_config", lambda: configured(False))
    host = desktop_host.DesktopHost()
    host.local_codex = FakeLocalCodex(order, pid=None)
    host.start_server = lambda: order.append("server_start")
    host.wait_for_server = lambda: order.append("server_ready") or (
        "http://127.0.0.1:8000", {"version": "0.11.0"}
    )

    class RunApp(FakeApp):
        def __init__(self, local_codex_service=None, services=None):
            super().__init__(order, local_codex_service, services)
            order.append("app")

        def title(self, _value):
            pass

        def protocol(self, *_args):
            pass

        def mainloop(self):
            host.exiting = True

    monkeypatch.setattr(desktop_host, "App", RunApp)
    monkeypatch.setattr(host, "build_tray", lambda: None)
    monkeypatch.setattr(host, "_show_window", lambda: None)

    host.run()

    assert order[:4] == ["server_start", "server_ready", "local_codex_start", "app"]
    assert host.app.services is host.services
    assert host.app.local_codex_service is host.local_codex


def test_local_codex_startup_failure_leaves_hcs_gui_usable(monkeypatch):
    monkeypatch.setattr(desktop_host, "load_config", lambda: configured(False))
    host = desktop_host.DesktopHost()

    class BrokenLocalCodex(FakeLocalCodex):
        def start(self):
            raise RuntimeError("worker unavailable")

    host.local_codex = BrokenLocalCodex(pid=None)
    host.start_server = lambda: None
    host.wait_for_server = lambda: ("http://127.0.0.1:8000", {"version": "0.11.0"})
    created = []

    class RunApp(FakeApp):
        def __init__(self, local_codex_service=None, services=None):
            super().__init__(created, local_codex_service, services)
            created.append("created")

        def title(self, _value):
            pass

        def protocol(self, *_args):
            pass

        def mainloop(self):
            host.exiting = True

    monkeypatch.setattr(desktop_host, "App", RunApp)
    monkeypatch.setattr(host, "build_tray", lambda: None)
    monkeypatch.setattr(host, "_show_window", lambda: None)

    host.run()

    assert created == ["created"]
    assert host.app.local_codex_service is host.local_codex
    assert host.app.logs == [("Local Codex startup warning: worker unavailable", "warning")]


def test_gui_construction_failure_stops_started_children(monkeypatch):
    order = []
    monkeypatch.setattr(desktop_host, "load_config", lambda: configured(False))
    host = desktop_host.DesktopHost()
    host.local_codex = FakeLocalCodex(order, pid=None)
    host.start_server = lambda: order.append("server_start")
    host.wait_for_server = lambda: ("http://127.0.0.1:8000", {"version": "0.11.0"})
    host._stop_children = lambda: order.append("children_stopped")

    def broken_app(*, local_codex_service=None, services=None):
        order.append("app_failed")
        raise RuntimeError("GUI unavailable")

    monkeypatch.setattr(desktop_host, "App", broken_app)

    with pytest.raises(RuntimeError, match="GUI unavailable"):
        host.run()

    assert order == ["server_start", "local_codex_start", "app_failed", "children_stopped"]
