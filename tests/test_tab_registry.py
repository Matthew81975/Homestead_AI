from types import SimpleNamespace

import pytest

from hcs_ai.core.services import ServiceContainer, ServiceNotFoundError
from hcs_ai.core.tabs import TabDefinition, TabRegistry, method_tab
from hcs_ai.tabs.local_codex import LOCAL_CODEX_TAB


def test_service_container_registers_optional_and_required_services():
    local = object()
    services = ServiceContainer({"local_codex": local})

    assert services.get("local_codex") is local
    assert services.get("missing") is None
    assert services.require("local_codex") is local
    assert services.names() == ("local_codex",)

    with pytest.raises(ServiceNotFoundError):
        services.require("missing")


def test_service_container_rejects_accidental_replacement():
    services = ServiceContainer({"shared": object()})

    with pytest.raises(ValueError, match="already registered"):
        services.register("shared", object())


def test_tab_registry_preserves_order_and_rejects_duplicate_ids():
    noop = lambda *_args: None
    first = TabDefinition("first", "First", "first_tab", noop)
    second = TabDefinition("second", "Second", "second_tab", noop)
    registry = TabRegistry([first, second])

    assert registry.ids() == ("first", "second")
    assert registry.get("second") is second

    with pytest.raises(ValueError, match="already registered"):
        registry.register(TabDefinition("first", "Again", "again_tab", noop))


def test_method_tab_adapts_existing_builder_without_knowing_implementation():
    calls = []

    class Host:
        def build_example(self):
            calls.append("built")

    definition = method_tab("example", "Example", "example_tab", "build_example")
    definition.builder(Host(), object(), ServiceContainer())

    assert calls == ["built"]


def test_local_codex_tab_gets_service_from_shared_container():
    local_codex = object()

    class Host:
        local_codex_service = None

        def build_local_codex(self):
            self.built = True

    host = Host()
    frame = SimpleNamespace()
    services = ServiceContainer({"local_codex": local_codex})
    setattr(host, LOCAL_CODEX_TAB.frame_attr, frame)

    LOCAL_CODEX_TAB.builder(host, frame, services)

    assert host.local_codex_tab is frame
    assert host.local_codex_service is local_codex
    assert host.built is True
