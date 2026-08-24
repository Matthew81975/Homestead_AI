from __future__ import annotations

from collections.abc import Mapping

from .scene import Scene, SceneEdge, SceneNode, Transform


def scene_to_dict(scene: Scene) -> dict:
    return {
        'nodes': [
            {
                'node_id': node.node_id,
                'primitive': node.primitive,
                'label': node.label,
                'layer': node.layer,
                'transform': {
                    'position': list(node.transform.position),
                    'rotation': list(node.transform.rotation),
                    'scale': list(node.transform.scale),
                },
                'metadata': dict(node.metadata),
                'selectable': node.selectable,
                'visible': node.visible,
            }
            for node in scene.nodes
        ],
        'edges': [
            {
                'edge_id': edge.edge_id,
                'source_id': edge.source_id,
                'target_id': edge.target_id,
                'relationship': edge.relationship,
                'metadata': dict(edge.metadata),
            }
            for edge in scene.edges
        ],
    }


def scene_from_dict(data: Mapping[str, object]) -> Scene:
    nodes = tuple(_node_from_dict(item) for item in data.get('nodes', []))
    edges = tuple(_edge_from_dict(item) for item in data.get('edges', []))
    return Scene(nodes=nodes, edges=edges)


def _node_from_dict(data: Mapping[str, object]) -> SceneNode:
    transform_data = dict(data.get('transform') or {})
    return SceneNode(
        node_id=str(data['node_id']),
        primitive=str(data['primitive']),
        label=str(data.get('label') or ''),
        layer=str(data.get('layer') or 'default'),
        transform=Transform(
            position=_vec3(transform_data.get('position'), (0.0, 0.0, 0.0)),
            rotation=_vec3(transform_data.get('rotation'), (0.0, 0.0, 0.0)),
            scale=_vec3(transform_data.get('scale'), (1.0, 1.0, 1.0)),
        ),
        metadata=dict(data.get('metadata') or {}),
        selectable=bool(data.get('selectable', True)),
        visible=bool(data.get('visible', True)),
    )


def _edge_from_dict(data: Mapping[str, object]) -> SceneEdge:
    return SceneEdge(
        edge_id=str(data['edge_id']),
        source_id=str(data['source_id']),
        target_id=str(data['target_id']),
        relationship=str(data.get('relationship') or 'related'),
        metadata=dict(data.get('metadata') or {}),
    )


def _vec3(value: object, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if value is None:
        return default
    items = list(value)  # type: ignore[arg-type]
    if len(items) != 3:
        raise ValueError('vector must contain exactly 3 values')
    return tuple(float(item) for item in items)  # type: ignore[return-value]
