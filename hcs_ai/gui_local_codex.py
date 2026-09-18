"""Backward-compatible Local Codex GUI imports.

The Local Codex tab implementation now lives under
`hcs_ai.tabs.local_codex`. Keep this module as a stable import path while
older HCS layers and external callers migrate.
"""

from .tabs.local_codex import (
    LOCAL_CODEX_TAB_TITLE,
    REQUIRED_CONTROL_NAMES,
    LocalCodexControllerMixin,
    LocalCodexGuiMixin,
    LocalCodexViewMixin,
    format_local_codex_event,
    local_codex_control_states,
    local_codex_search_ranges,
    ordered_local_codex_events,
    should_follow_local_codex_log,
)

__all__ = [
    "LOCAL_CODEX_TAB_TITLE",
    "REQUIRED_CONTROL_NAMES",
    "LocalCodexControllerMixin",
    "LocalCodexGuiMixin",
    "LocalCodexViewMixin",
    "format_local_codex_event",
    "local_codex_control_states",
    "local_codex_search_ranges",
    "ordered_local_codex_events",
    "should_follow_local_codex_log",
]
