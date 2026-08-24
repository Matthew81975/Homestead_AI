from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class Transform:
    position: Vec3 = (0.0, 0.0, 0.0)
    rotation: Vec3 = (0.0, 0.0, 0.0)
    scale: Vec3 = (1.0, 1.0, 1.0)

    def __post_init__(self) -> None:
        for name in ('position', 'rotation', 'scale'):
            value = getattr(self, name)
            if len(value) != 3:
                raise ValueError(f'{name} must contain exactly 3 values')
        if any(component <= 0 for component in self.scale):
            raise ValueError('scale components must be positive')


@dataclass(frozen=True)
class SceneNode:
    node_id: str
    primitive: str
    label: str = ''
    layer: str = 'default'
    transform: Transform = field(default_factory=Transform)
    metadata: Mapping[str, object] = field(default_factory=dict)
    selectable: bool = True
    visible: bool = True

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError('node_id must not be empty')
        if not self.primitive:
            raise ValueError('primitive must not be empty')
        object.__setattr__(self, 'metadata', MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class SceneEdge:
    edge_id: str
    source_id: str
    target_id: str
    relationship: str = 'related'
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.edge_id or not self.source_id or not self.target_id:
            raise ValueError('edge_id, source_id, and target_id must not be empty')
        object.__setattr__(self, 'metadata', MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class Scene:
    nodes: tuple[SceneNode, ...] = ()
    edges: tuple[SceneEdge, ...] = ()

    def __post_init__(self) -> None:
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError('scene node ids must be unique')
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError('scene edge ids must be unique')
        known = set(node_ids)
        for edge in self.edges:
            if edge.source_id not in known or edge.target_id not in known:
                raise ValueError(f'edge {edge.edge_id!r} references unknown node')

    def get_node(self, node_id: str) -> SceneNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)
