from hcs_ai.visualization.layers import LayerRegistry, LayerState, filter_scene
from hcs_ai.visualization.scene import Scene, SceneNode


def test_default_registry_contains_building_disciplines():
    registry = LayerRegistry.default()
    for layer in (
        "architecture", "openings", "structure", "framing", "foundation", "earthwork",
        "drainage", "plumbing", "electrical", "low_voltage", "hvac", "energy",
        "insulation", "finishes", "dimensions", "annotations", "analysis",
    ):
        assert registry.has(layer)


def test_hidden_layer_removes_nodes_from_view():
    scene = Scene(nodes=(
        SceneNode("wall", "box", layer="architecture"),
        SceneNode("pipe", "cylinder", layer="plumbing"),
    ))
    registry = LayerRegistry.default().with_state("plumbing", LayerState.HIDDEN)
    visible = filter_scene(scene, registry)
    assert [node.node_id for node in visible.nodes] == ["wall"]


def test_ghosted_layer_marks_nodes_without_changing_canonical_scene():
    scene = Scene(nodes=(SceneNode("stud", "box", layer="framing"),))
    registry = LayerRegistry.default().with_state("framing", LayerState.GHOSTED)
    visible = filter_scene(scene, registry)
    assert visible.nodes[0].metadata["layer_state"] == "ghosted"
    assert "layer_state" not in scene.nodes[0].metadata


def test_isolate_preset_hides_all_other_layers():
    registry = LayerRegistry.default().isolate(("plumbing", "framing"))
    assert registry.state("plumbing") is LayerState.VISIBLE
    assert registry.state("framing") is LayerState.VISIBLE
    assert registry.state("electrical") is LayerState.HIDDEN
