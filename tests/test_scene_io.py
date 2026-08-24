from hcs_ai.visualization.scene import Scene, SceneEdge, SceneNode, Transform
from hcs_ai.visualization.scene_io import scene_from_dict, scene_to_dict


def test_scene_round_trip_preserves_nodes_edges_and_metadata():
    original = Scene(
        nodes=(
            SceneNode('a', 'box', label='Wall', layer='structural', transform=Transform(position=(1, 2, 3), scale=(4, 5, 6)), metadata={'u': 0.42}),
            SceneNode('b', 'sphere', label='Node', layer='knowledge'),
        ),
        edges=(SceneEdge('e', 'a', 'b', relationship='related_to', metadata={'confidence': 0.8}),),
    )
    restored = scene_from_dict(scene_to_dict(original))
    assert restored.nodes[0].node_id == 'a'
    assert restored.nodes[0].transform.position == (1.0, 2.0, 3.0)
    assert restored.nodes[0].metadata['u'] == 0.42
    assert restored.edges[0].relationship == 'related_to'
