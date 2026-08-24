from __future__ import annotations

from hcs_ai.design_platform.geometry import StraightWallGeometry
from hcs_ai.design_platform.results import WallAnalysisResult

from .scene import Scene, SceneNode, Transform


def mass_wall_to_scene(
    wall_id: str,
    wall: StraightWallGeometry,
    result: WallAnalysisResult | None = None,
) -> Scene:
    if not wall_id:
        raise ValueError('wall_id must not be empty')

    metadata: dict[str, object] = {
        'length_m': wall.length,
        'height_m': wall.height,
        'thickness_m': wall.thickness,
        'gross_area_m2': wall.gross_area,
        'net_area_m2': wall.net_area,
        'opening_count': len(wall.openings),
    }
    if result is not None:
        governing = result.governing_check
        metadata.update({
            'self_weight_N': result.self_weight,
            'total_vertical_force_N': result.total_vertical_force,
            'lateral_force_N': result.lateral_force,
            'governing_check': governing.name if governing else None,
            'governing_utilization': governing.utilization if governing else None,
        })

    nodes: list[SceneNode] = [
        SceneNode(
            node_id=wall_id,
            primitive='box',
            label=wall_id,
            layer='structure',
            transform=Transform(
                position=(wall.length / 2.0, wall.height / 2.0, 0.0),
                scale=(wall.length, wall.height, wall.thickness),
            ),
            metadata=metadata,
        )
    ]

    for index, opening in enumerate(wall.openings):
        nodes.append(SceneNode(
            node_id=f'{wall_id}:opening:{index}',
            primitive='void_box',
            label=opening.kind,
            layer='openings',
            transform=Transform(
                position=(opening.left + opening.width / 2.0, opening.bottom + opening.height / 2.0, 0.0),
                scale=(opening.width, opening.height, wall.thickness * 1.05),
            ),
            metadata={'kind': opening.kind, 'host_id': wall_id},
        ))

    if result is not None:
        for index, pier in enumerate(result.piers):
            nodes.append(SceneNode(
                node_id=f'{wall_id}:pier:{index}',
                primitive='analysis_box',
                label=f'Pier {index + 1}',
                layer='analysis',
                transform=Transform(
                    position=((pier.start + pier.end) / 2.0, wall.height / 2.0, 0.0),
                    scale=(pier.width, wall.height, wall.thickness * 1.01),
                ),
                metadata={
                    'host_id': wall_id,
                    'axial_force_N': pier.axial_force,
                    'stress_Pa': pier.stress,
                },
            ))

    return Scene(nodes=tuple(nodes))
