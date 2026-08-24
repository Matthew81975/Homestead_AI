import pytest

from hcs_ai.visualization.panda3d_renderer import (
    Panda3DUnavailable, Panda3DViewer, panda3d_available,
    scene_to_panda_position, scene_to_panda_scale,
)


def test_scene_coordinate_conversion_preserves_y_up_convention():
    assert scene_to_panda_position((1.0, 2.0, 3.0)) == (1.0, 3.0, 2.0)
    assert scene_to_panda_scale((4.0, 5.0, 6.0)) == (4.0, 6.0, 5.0)


def test_panda3d_is_optional_until_viewer_is_opened():
    assert isinstance(panda3d_available(), bool)
    if not panda3d_available():
        with pytest.raises(Panda3DUnavailable):
            Panda3DViewer()
