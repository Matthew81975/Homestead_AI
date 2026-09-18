from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .services import ServiceContainer


TabBuilder = Callable[[Any, Any, ServiceContainer], None]


@dataclass(frozen=True)
class TabDefinition:
    """Declarative contract for one top-level HCS tab."""

    tab_id: str
    title: str
    frame_attr: str
    builder: TabBuilder

    def __post_init__(self) -> None:
        if not self.tab_id.strip():
            raise ValueError("tab_id must not be empty")
        if not self.title.strip():
            raise ValueError("tab title must not be empty")
        if not self.frame_attr.strip():
            raise ValueError("frame_attr must not be empty")
        if not callable(self.builder):
            raise TypeError("tab builder must be callable")


class TabRegistry:
    """Ordered registry used by the HCS shell to compose independently owned tabs."""

    def __init__(self, definitions: Iterable[TabDefinition] = ()):
        self._definitions: list[TabDefinition] = []
        self._by_id: dict[str, TabDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: TabDefinition) -> TabDefinition:
        if definition.tab_id in self._by_id:
            raise ValueError(f"tab already registered: {definition.tab_id}")
        self._definitions.append(definition)
        self._by_id[definition.tab_id] = definition
        return definition

    def get(self, tab_id: str) -> TabDefinition:
        return self._by_id[tab_id]

    def definitions(self) -> tuple[TabDefinition, ...]:
        return tuple(self._definitions)

    def ids(self) -> tuple[str, ...]:
        return tuple(item.tab_id for item in self._definitions)

    def __iter__(self):
        return iter(self._definitions)

    def __len__(self) -> int:
        return len(self._definitions)


def method_tab(tab_id: str, title: str, frame_attr: str, method_name: str) -> TabDefinition:
    """Adapt an existing App.build_* method to the modular tab contract.

    This is the migration bridge for legacy HCS tabs. Individual tabs can later
    replace this adapter with their own module-owned builder without changing
    the shell or registry.
    """

    def build(host: Any, _frame: Any, _services: ServiceContainer) -> None:
        getattr(host, method_name)()

    return TabDefinition(
        tab_id=tab_id,
        title=title,
        frame_attr=frame_attr,
        builder=build,
    )
