from __future__ import annotations

from typing import Any

from ...core.services import ServiceContainer
from ...core.tabs import TabDefinition
from .presentation import LOCAL_CODEX_TAB_TITLE


def build_local_codex_tab(host: Any, _frame: Any, services: ServiceContainer) -> None:
    """Attach the HCS-owned Local Codex service and build its existing UI.

    The current LocalCodexGuiMixin remains intact during the first migration
    step. Future Local Codex UI work can move into this package without
    changing the HCS shell's tab composition code.
    """

    host.local_codex_service = services.get("local_codex")
    host.build_local_codex()


LOCAL_CODEX_TAB = TabDefinition(
    tab_id="local_codex",
    title=LOCAL_CODEX_TAB_TITLE,
    frame_attr="local_codex_tab",
    builder=build_local_codex_tab,
)
