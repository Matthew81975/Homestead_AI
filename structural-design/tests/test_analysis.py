import math

from design_platform.analysis import analyze_straight_mass_wall
from design_platform.geometry import RectOpening, StraightWallGeometry
from design_platform.loads import WallLoads
from design_platform.mass_walls import EngineeringValue, EarthbagAssembly

G = 9.80665


def _assembly():
    return EarthbagAssembly(density=EngineeringValue(1800.0, 'kg/m3', basis='assumed'), compressive_strength=EngineeringValue(1_000_000.0, 'Pa', basis='sourced'), friction_coefficient=EngineeringValue(0.5, '1', basis='assumed'))


def test_solid_wall_matches_hand_calculation():
    wall = StraightWallGeometry(length=4.0, height=3.0, thickness=0.4)
    loads = WallLoads(vertical_line_load=10_000.0, lateral_pressure=500.0)
    result = analyze_straight_mass_wall(wall, _assembly(), loads)
    expected_self_weight = 4.0 * 3.0 * 0.4 * 1800.0 * G
    expected_vertical = expected_self_weight + 10_000.0 * 4.0
    expected_lateral = 500.0 * 12.0
    expected_moment = expected_lateral * 1.5
    assert math.isclose(result.self_weight, expected_self_weight)
    assert math.isclose(result.total_vertical_force, expected_vertical)
    assert math.isclose(result.lateral_force, expected_lateral)
    assert math.isclose(result.overturning_moment, expected_moment)
    compression = result.checks['compression']
    assert compression.status == 'evaluated'
    assert math.isclose(compression.capacity, 1_000_000.0 * 4.0 * 0.4)
    assert math.isclose(compression.utilization, expected_vertical / compression.capacity)
    sliding = result.checks['sliding']
    assert math.isclose(sliding.capacity, 0.5 * expected_vertical)
    assert math.isclose(sliding.utilization, expected_lateral / sliding.capacity)
    eccentricity = result.checks['eccentricity']
    assert math.isclose(eccentricity.demand, expected_moment / expected_vertical)
    assert math.isclose(eccentricity.capacity, 0.4 / 6.0)


def test_opening_creates_piers_and_reduces_load_bearing_width():
    wall = StraightWallGeometry(length=4.0, height=3.0, thickness=0.4, openings=(RectOpening(left=1.0, bottom=1.0, width=1.0, height=1.0, kind='window'),))
    result = analyze_straight_mass_wall(wall, _assembly(), WallLoads())
    assert result.load_bearing_width == 3.0
    assert len(result.piers) == 2
    assert result.piers[0].start == 0.0
    assert result.piers[0].end == 1.0
    assert result.piers[1].start == 2.0
    assert result.piers[1].end == 4.0
    assert math.isclose(result.checks['compression'].capacity, 1_000_000.0 * 3.0 * 0.4)


def test_unknown_properties_produce_not_evaluated_checks():
    wall = StraightWallGeometry(length=4.0, height=3.0, thickness=0.4)
    assembly = EarthbagAssembly(density=EngineeringValue(1800.0, 'kg/m3'))
    result = analyze_straight_mass_wall(wall, assembly, WallLoads(lateral_pressure=500.0))
    assert result.checks['compression'].status == 'not_evaluated'
    assert result.checks['compression'].capacity is None
    assert result.checks['sliding'].status == 'not_evaluated'
    assert result.checks['sliding'].utilization is None


def test_assumptions_are_exposed():
    wall = StraightWallGeometry(length=4.0, height=3.0, thickness=0.4)
    result = analyze_straight_mass_wall(wall, _assembly(), WallLoads(lateral_pressure=500.0))
    assert any('uniform lateral pressure' in item for item in result.assumptions)
    assert any('pier width' in item for item in result.assumptions)
