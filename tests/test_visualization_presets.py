from hcs_ai.visualization.presets import build_preset
from hcs_ai.visualization.layers import LayerState


def test_structural_preset_shows_structure_and_ghosts_architecture():
    preset = build_preset("structural")
    assert preset.state("structure") is LayerState.VISIBLE
    assert preset.state("framing") is LayerState.VISIBLE
    assert preset.state("foundation") is LayerState.VISIBLE
    assert preset.state("architecture") is LayerState.GHOSTED
    assert preset.state("plumbing") is LayerState.HIDDEN


def test_mep_preset_shows_plumbing_electrical_hvac():
    preset = build_preset("mep")
    assert preset.state("plumbing") is LayerState.VISIBLE
    assert preset.state("electrical") is LayerState.VISIBLE
    assert preset.state("hvac") is LayerState.VISIBLE
    assert preset.state("architecture") is LayerState.GHOSTED


def test_coordination_preset_keeps_main_disciplines_visible():
    preset = build_preset("coordination")
    for layer in ("architecture", "structure", "framing", "plumbing", "electrical", "hvac", "drainage"):
        assert preset.state(layer) is LayerState.VISIBLE
