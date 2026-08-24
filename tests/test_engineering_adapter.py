from hcs_ai.design_platform import (
    EngineeringValue, EarthbagAssembly, RectOpening, StraightWallGeometry,
    WallLoads, analyze_straight_mass_wall,
)
from hcs_ai.visualization.engineering_adapter import mass_wall_to_scene


def test_mass_wall_scene_contains_wall_opening_and_analysis_piers():
    wall = StraightWallGeometry(
        length=6.0, height=3.0, thickness=0.4,
        openings=(RectOpening(left=2.0, bottom=0.0, width=1.0, height=2.1, kind='door'),),
    )
    assembly = EarthbagAssembly(
        density=EngineeringValue(1800.0, 'kg/m3'),
        compressive_strength=EngineeringValue(1_000_000.0, 'Pa'),
        friction_coefficient=EngineeringValue(0.5, '1'),
    )
    result = analyze_straight_mass_wall(wall, assembly, WallLoads(lateral_pressure=300.0))
    scene = mass_wall_to_scene('wall-A', wall, result)
    assert scene.get_node('wall-A').layer == 'structural'
    assert scene.get_node('wall-A:opening:0').primitive == 'void_box'
    assert scene.get_node('wall-A:pier:0').layer == 'analysis'
    assert scene.get_node('wall-A').metadata['governing_check'] == result.governing_check.name


def test_mass_wall_scene_coordinates_use_x_length_y_height_z_thickness():
    wall = StraightWallGeometry(length=6.0, height=3.0, thickness=0.4)
    scene = mass_wall_to_scene('wall-A', wall)
    node = scene.get_node('wall-A')
    assert node.transform.position == (3.0, 1.5, 0.0)
    assert node.transform.scale == (6.0, 3.0, 0.4)
