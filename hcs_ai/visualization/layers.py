from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .scene import Scene, SceneNode


class LayerState(str, Enum):
    VISIBLE = "visible"
    GHOSTED = "ghosted"
    HIDDEN = "hidden"


DEFAULT_LAYERS = (
    "architecture", "openings", "structure", "framing", "foundation", "earthwork",
    "drainage", "plumbing", "electrical", "low_voltage", "hvac", "energy",
    "insulation", "finishes", "dimensions", "annotations", "analysis",
)


@dataclass(frozen=True)
class LayerRegistry:
    states: tuple[tuple[str, LayerState], ...]

    @classmethod
    def default(cls) -> "LayerRegistry":
        return cls(tuple((name, LayerState.VISIBLE) for name in DEFAULT_LAYERS))

    def has(self, layer: str) -> bool:
        return any(name == layer for name, _ in self.states)

    def state(self, layer: str) -> LayerState:
        for name, state in self.states:
            if name == layer:
                return state
        return LayerState.VISIBLE

    def with_state(self, layer: str, state: LayerState) -> "LayerRegistry":
        updated = dict(self.states)
        updated[layer] = state
        return LayerRegistry(tuple(updated.items()))

    def isolate(self, layers: tuple[str, ...] | list[str]) -> "LayerRegistry":
        keep = set(layers)
        names = {name for name, _ in self.states} | keep
        return LayerRegistry(tuple(
            (name, LayerState.VISIBLE if name in keep else LayerState.HIDDEN)
            for name in names
        ))


def filter_scene(scene: Scene, registry: LayerRegistry) -> Scene:
    kept_nodes: list[SceneNode] = []
    kept_ids: set[str] = set()
    for node in scene.nodes:
        state = registry.state(node.layer)
        if state is LayerState.HIDDEN or not node.visible:
            continue
        if state is LayerState.GHOSTED:
            metadata = dict(node.metadata)
            metadata["layer_state"] = LayerState.GHOSTED.value
            node = replace(node, metadata=metadata)
        kept_nodes.append(node)
        kept_ids.add(node.node_id)

    kept_edges = tuple(
        edge for edge in scene.edges
        if edge.source_id in kept_ids and edge.target_id in kept_ids
    )
    return Scene(nodes=tuple(kept_nodes), edges=kept_edges)
