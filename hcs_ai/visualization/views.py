from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .layers import LayerRegistry, LayerState
from .presets import build_preset


class ViewPlane(str, Enum):
    XY = "xy"
    XZ = "xz"
    YZ = "yz"


@dataclass(frozen=True)
class OrthographicView:
    plane: ViewPlane
    layers: LayerRegistry
    cut_offset: float = 0.0

    @classmethod
    def plan(cls) -> "OrthographicView":
        return cls(ViewPlane.XY, LayerRegistry.default())

    @classmethod
    def elevation(cls) -> "OrthographicView":
        return cls(ViewPlane.XZ, LayerRegistry.default())

    @classmethod
    def section(cls) -> "OrthographicView":
        return cls(ViewPlane.YZ, LayerRegistry.default())

    @classmethod
    def from_preset(cls, name: str, *, plane: ViewPlane) -> "OrthographicView":
        return cls(plane, build_preset(name))

    def with_layer(self, layer: str, state: LayerState) -> "OrthographicView":
        return replace(self, layers=self.layers.with_state(layer, state))

    def at_cut(self, offset: float) -> "OrthographicView":
        return replace(self, cut_offset=float(offset))
