import pytest

from hcs_ai.visualization.scene import Scene, SceneEdge, SceneNode, Transform


def test_scene_accepts_engineering_and_knowledge_nodes():
    wall = SceneNode('wall-1', 'box', label='Earthbag wall', layer='structural')
    topic = SceneNode('knowledge-42', 'sphere', label='Structural Engineering', layer='knowledge')
    scene = Scene(nodes=(wall, topic))
    assert scene.get_node('wall-1') is wall
    assert scene.get_node('knowledge-42') is topic


def test_edges_must_reference_existing_nodes():
    with pytest.raises(ValueError, match='unknown node'):
        Scene(nodes=(SceneNode('a', 'sphere'),), edges=(SceneEdge('e1', 'a', 'missing'),))


def test_node_ids_are_unique():
    with pytest.raises(ValueError, match='unique'):
        Scene(nodes=(SceneNode('x', 'box'), SceneNode('x', 'sphere')))


def test_transform_defaults_are_renderer_neutral():
    node = SceneNode('x', 'box')
    assert node.transform == Transform()
    assert node.transform.position == (0.0, 0.0, 0.0)
    assert node.transform.scale == (1.0, 1.0, 1.0)


def test_metadata_is_copied_to_prevent_external_mutation():
    metadata = {'utilization': 0.42}
    node = SceneNode('wall', 'box', metadata=metadata)
    metadata['utilization'] = 0.99
    assert node.metadata['utilization'] == 0.42
