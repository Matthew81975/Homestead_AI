from __future__ import annotations

from .layers import LayerRegistry, LayerState


def build_preset(name: str) -> LayerRegistry:
    key = name.strip().lower()
    registry = LayerRegistry.default()

    if key == "structural":
        registry = registry.isolate(("structure", "framing", "foundation", "analysis"))
        return registry.with_state("architecture", LayerState.GHOSTED)

    if key == "mep":
        registry = registry.isolate(("plumbing", "electrical", "low_voltage", "hvac", "drainage"))
        return registry.with_state("architecture", LayerState.GHOSTED)

    if key == "coordination":
        return registry.isolate((
            "architecture", "structure", "framing", "foundation", "drainage",
            "plumbing", "electrical", "low_voltage", "hvac", "energy",
        ))

    if key == "all":
        return registry

    raise ValueError(f"unknown visualization preset: {name!r}")
