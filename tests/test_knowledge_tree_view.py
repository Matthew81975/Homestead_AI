import pytest

from hcs_ai.visualization.panda3d_renderer import Panda3DUnavailable
from hcs_ai.visualization.knowledge_tree_view import launch_knowledge_tree_view


def test_launch_knowledge_tree_view_builds_scene_and_launches(monkeypatch):
    launched = []
    monkeypatch.setattr('hcs_ai.visualization.knowledge_tree_view.panda3d_available', lambda: True)
    monkeypatch.setattr('hcs_ai.visualization.knowledge_tree_view.launch_scene', lambda scene, title: launched.append((scene, title)) or 'proc')
    rows = [{'id': 1, 'parent_id': None, 'canonical_name': 'Engineering'}]
    assert launch_knowledge_tree_view(rows) == 'proc'
    assert launched[0][0].nodes[0].label == 'Engineering'
    assert launched[0][1] == 'HCS Knowledge Tree'


def test_launch_knowledge_tree_view_reports_missing_renderer(monkeypatch):
    monkeypatch.setattr('hcs_ai.visualization.knowledge_tree_view.panda3d_available', lambda: False)
    with pytest.raises(Panda3DUnavailable):
        launch_knowledge_tree_view([{'id': 1, 'parent_id': None, 'canonical_name': 'Engineering'}])
