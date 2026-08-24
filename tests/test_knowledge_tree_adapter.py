from hcs_ai.visualization.knowledge_tree_adapter import knowledge_tree_to_scene


def test_knowledge_tree_scene_has_nodes_and_parent_edges():
    rows = [
        {'id': 1, 'parent_id': None, 'canonical_name': 'Engineering', 'review_status': 'accepted'},
        {'id': 2, 'parent_id': 1, 'canonical_name': 'Structural Engineering', 'review_status': 'accepted'},
        {'id': 3, 'parent_id': 2, 'canonical_name': 'Trusses', 'review_status': 'accepted'},
    ]
    scene = knowledge_tree_to_scene(rows)
    assert scene.get_node('knowledge:1').label == 'Engineering'
    assert scene.get_node('knowledge:2').layer == 'knowledge'
    assert len(scene.edges) == 2
    assert scene.edges[0].relationship == 'parent_of'


def test_knowledge_tree_layout_is_deterministic_and_depth_separated():
    rows = [
        {'id': 10, 'parent_id': None, 'canonical_name': 'Science'},
        {'id': 11, 'parent_id': 10, 'canonical_name': 'Physics'},
    ]
    first = knowledge_tree_to_scene(rows)
    second = knowledge_tree_to_scene(list(reversed(rows)))
    assert first.get_node('knowledge:10').transform.position == second.get_node('knowledge:10').transform.position
    assert first.get_node('knowledge:11').transform.position == second.get_node('knowledge:11').transform.position
    assert first.get_node('knowledge:10').transform.position[1] > first.get_node('knowledge:11').transform.position[1]
