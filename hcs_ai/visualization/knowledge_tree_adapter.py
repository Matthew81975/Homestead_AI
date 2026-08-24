from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from .scene import Scene, SceneEdge, SceneNode, Transform


def knowledge_tree_to_scene(rows: Iterable[Mapping[str, object]]) -> Scene:
    items = [dict(row) for row in rows]
    by_id = {int(item['id']): item for item in items}
    depths = {node_id: _depth(node_id, by_id) for node_id in by_id}

    levels: dict[int, list[int]] = {}
    for node_id, depth in depths.items():
        levels.setdefault(depth, []).append(node_id)
    for node_ids in levels.values():
        node_ids.sort()

    positions: dict[int, tuple[float, float, float]] = {}
    for depth, node_ids in sorted(levels.items()):
        count = len(node_ids)
        radius = (depth * 4.0) if count == 1 else max(3.0, depth * 4.0)
        for index, node_id in enumerate(node_ids):
            if count == 1 and depth == 0:
                x = z = 0.0
            else:
                angle = 2.0 * math.pi * index / count
                x = radius * math.cos(angle)
                z = radius * math.sin(angle)
            positions[node_id] = (x, -2.0 * depth, z)

    nodes = tuple(
        SceneNode(
            node_id=f'knowledge:{node_id}',
            primitive='sphere',
            label=str(by_id[node_id].get('canonical_name') or node_id),
            layer='knowledge',
            transform=Transform(position=positions[node_id]),
            metadata={
                key: value for key, value in by_id[node_id].items()
                if key not in {'id', 'parent_id', 'canonical_name'}
            },
        )
        for node_id in sorted(by_id)
    )

    edges: list[SceneEdge] = []
    for node_id in sorted(by_id):
        parent_id = by_id[node_id].get('parent_id')
        if parent_id is None:
            continue
        parent = int(parent_id)
        if parent not in by_id:
            continue
        edges.append(SceneEdge(
            edge_id=f'knowledge-edge:{parent}:{node_id}',
            source_id=f'knowledge:{parent}',
            target_id=f'knowledge:{node_id}',
            relationship='parent_of',
        ))

    return Scene(nodes=nodes, edges=tuple(edges))


def _depth(node_id: int, by_id: Mapping[int, Mapping[str, object]]) -> int:
    depth = 0
    current = node_id
    seen: set[int] = set()
    while True:
        if current in seen:
            raise ValueError('knowledge tree contains a parent cycle')
        seen.add(current)
        parent = by_id[current].get('parent_id')
        if parent is None:
            return depth
        parent_id = int(parent)
        if parent_id not in by_id:
            return depth
        current = parent_id
        depth += 1
