from __future__ import annotations

from typing import Any

from ...core.services import ServiceContainer
from ...core.tabs import TabDefinition
from .presentation import LOCAL_CODEX_TAB_TITLE


def build_local_codex_tab(host: Any, _frame: Any, services: ServiceContainer) -> None:
    """Attach the HCS-owned Local Codex service and build its feature-owned UI.

    The shell knows only the tab contract and shared service container; Local
    Codex view/controller details remain inside this feature package.
    """

    host.local_codex_service = services.get("local_codex")
    host.build_local_codex()


LOCAL_CODEX_TAB = TabDefinition(
    tab_id="local_codex",
    title=LOCAL_CODEX_TAB_TITLE,
    frame_attr="local_codex_tab",
    builder=build_local_codex_tab,
)
