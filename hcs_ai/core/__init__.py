"""Core HCS composition primitives.

This package intentionally contains framework-level code only. Feature tabs and
services should depend on these primitives rather than on one another.
"""

from .services import ServiceContainer, ServiceNotFoundError
from .tabs import TabDefinition, TabRegistry, method_tab

__all__ = [
    "ServiceContainer",
    "ServiceNotFoundError",
    "TabDefinition",
    "TabRegistry",
    "method_tab",
]
