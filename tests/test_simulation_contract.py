from hcs_ai.design_platform.simulation import PhysicsDomain, SimulationCase, SimulationLoad


def test_simulation_case_supports_multiple_physics_domains():
    case = SimulationCase(
        name="buried wall",
        domains=(PhysicsDomain.STRUCTURAL, PhysicsDomain.THERMAL, PhysicsDomain.POROUS_FLOW),
    )
    assert PhysicsDomain.STRUCTURAL in case.domains
    assert PhysicsDomain.THERMAL in case.domains
    assert PhysicsDomain.POROUS_FLOW in case.domains


def test_simulation_load_can_represent_soil_and_hydrostatic_pressure():
    soil = SimulationLoad(kind="lateral_soil_pressure", magnitude=12000.0, unit="Pa")
    water = SimulationLoad(kind="hydrostatic_pressure", magnitude=9800.0, unit="Pa")
    assert soil.kind == "lateral_soil_pressure"
    assert water.kind == "hydrostatic_pressure"
