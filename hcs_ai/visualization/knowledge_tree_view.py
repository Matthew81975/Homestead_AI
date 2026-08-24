from __future__ import annotations

from collections.abc import Iterable, Mapping

from .knowledge_tree_adapter import knowledge_tree_to_scene
from .launcher import launch_scene
from .panda3d_renderer import Panda3DUnavailable, panda3d_available


def launch_knowledge_tree_view(rows: Iterable[Mapping[str, object]]):
    if not panda3d_available():
        raise Panda3DUnavailable(
            'Panda3D visualization support is not installed. Install requirements-visualization.txt.'
        )
    scene = knowledge_tree_to_scene(rows)
    return launch_scene(scene, title='HCS Knowledge Tree')
