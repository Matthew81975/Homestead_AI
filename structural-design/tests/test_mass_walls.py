import pytest

from design_platform.mass_walls import EngineeringValue, EarthbagAssembly, RubbleAssembly


def test_engineering_value_preserves_provenance():
    value = EngineeringValue(2_000_000.0, 'Pa', source='lab report p. 4', basis='measured')
    assert value.value == 2_000_000.0
    assert value.source == 'lab report p. 4'
    assert value.basis == 'measured'


def test_earthbag_missing_strengths_remain_unknown():
    assembly = EarthbagAssembly(density=EngineeringValue(1850.0, 'kg/m3', source='project test', basis='measured'), bag_width=0.40, course_height=0.15)
    assert assembly.compressive_strength is None
    assert assembly.shear_strength is None
    assert assembly.friction_coefficient is None


def test_rubble_can_store_packing_properties():
    assembly = RubbleAssembly(density=EngineeringValue(1900.0, 'kg/m3', basis='assumed'), void_ratio=0.28, mortared=False)
    assert assembly.void_ratio == 0.28
    assert assembly.mortared is False


def test_invalid_void_ratio_is_rejected():
    with pytest.raises(ValueError, match='void_ratio'):
        RubbleAssembly(void_ratio=1.2)


def test_negative_engineering_value_is_rejected():
    with pytest.raises(ValueError, match='nonnegative'):
        EngineeringValue(-1.0, 'Pa')
