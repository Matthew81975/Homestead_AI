from hcs_ai.visualization.layers import LayerState
from hcs_ai.visualization.views import OrthographicView, ViewPlane


def test_plan_view_uses_xy_plane_and_layer_states():
    view = OrthographicView.plan().with_layer("plumbing", LayerState.VISIBLE).with_layer("framing", LayerState.GHOSTED)
    assert view.plane is ViewPlane.XY
    assert view.layers.state("plumbing") is LayerState.VISIBLE
    assert view.layers.state("framing") is LayerState.GHOSTED


def test_elevation_and_section_views_have_distinct_planes():
    assert OrthographicView.elevation().plane is ViewPlane.XZ
    assert OrthographicView.section().plane is ViewPlane.YZ


def test_view_can_use_named_preset():
    view = OrthographicView.from_preset("mep", plane=ViewPlane.XY)
    assert view.layers.state("electrical") is LayerState.VISIBLE
    assert view.layers.state("architecture") is LayerState.GHOSTED
