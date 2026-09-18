from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ServiceNotFoundError(KeyError):
    """Raised when a required HCS service is not registered."""


class ServiceContainer:
    """Small dependency container shared by independently developed HCS tabs.

    Tabs receive services through this object instead of reaching into the
    desktop host or importing another tab's implementation.
    """

    def __init__(self, initial: Mapping[str, Any] | None = None):
        self._services: dict[str, Any] = {}
        if initial:
            for name, service in initial.items():
                self.register(name, service)

    @staticmethod
    def _normalize_name(name: str) -> str:
        value = str(name).strip()
        if not value:
            raise ValueError("service name must not be empty")
        return value

    def register(self, name: str, service: Any, *, replace: bool = False) -> Any:
        key = self._normalize_name(name)
        if key in self._services and not replace and self._services[key] is not service:
            raise ValueError(f"service already registered: {key}")
        self._services[key] = service
        return service

    def get(self, name: str, default: Any = None) -> Any:
        return self._services.get(self._normalize_name(name), default)

    def require(self, name: str) -> Any:
        key = self._normalize_name(name)
        if key not in self._services:
            raise ServiceNotFoundError(key)
        return self._services[key]

    def names(self) -> tuple[str, ...]:
        return tuple(self._services)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._services

    def __len__(self) -> int:
        return len(self._services)
