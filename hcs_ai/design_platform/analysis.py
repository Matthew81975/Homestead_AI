from __future__ import annotations

from typing import Protocol

from .geometry import StraightWallGeometry
from .loads import WallLoads
from .mass_walls import EngineeringValue
from .results import CheckResult, PierResult, WallAnalysisResult
from .units import Quantity, to_si

GRAVITY = 9.80665


class MassWallAssembly(Protocol):
    density: EngineeringValue | None
    compressive_strength: EngineeringValue | None
    shear_strength: EngineeringValue | None
    friction_coefficient: EngineeringValue | None


def analyze_straight_mass_wall(
    wall: StraightWallGeometry,
    assembly: MassWallAssembly,
    loads: WallLoads,
) -> WallAnalysisResult:
    density = _value_si(assembly.density, 'density')
    self_weight = None if density is None else wall.volume * density * GRAVITY
    external_vertical = loads.vertical_line_load * wall.length
    total_vertical = None if self_weight is None else self_weight + external_vertical

    lateral_force = loads.lateral_pressure * wall.gross_area
    overturning_moment = lateral_force * wall.height / 2.0

    segments = wall.pier_segments()
    load_bearing_width = sum(end - start for start, end in segments)
    bearing_area = load_bearing_width * wall.thickness
    piers = _pier_results(segments, wall.thickness, total_vertical, load_bearing_width)

    checks: dict[str, CheckResult] = {}
    compressive_strength = _value_si(assembly.compressive_strength, 'pressure')
    if compressive_strength is None:
        checks['compression'] = _not_evaluated('compression', 'N', 'compressive strength is unknown')
    elif total_vertical is None:
        checks['compression'] = _not_evaluated('compression', 'N', 'vertical demand is unknown because wall density is unknown')
    else:
        compression_capacity = compressive_strength * bearing_area
        checks['compression'] = _evaluated(
            'compression', total_vertical, compression_capacity, 'N',
            'Level-1 axial capacity uses total pier width times wall thickness.'
        )

    friction = _dimensionless(assembly.friction_coefficient)
    if friction is None:
        checks['sliding'] = _not_evaluated('sliding', 'N', 'friction coefficient is unknown')
    elif total_vertical is None:
        checks['sliding'] = _not_evaluated('sliding', 'N', 'sliding resistance is unknown because wall density is unknown')
    else:
        sliding_capacity = friction * total_vertical
        checks['sliding'] = _evaluated(
            'sliding', lateral_force, sliding_capacity, 'N',
            'Level-1 sliding resistance is friction coefficient times total vertical force.'
        )

    if total_vertical is None:
        checks['overturning'] = _not_evaluated(
            'overturning', 'N*m', 'restoring moment is unknown because wall density is unknown'
        )
    elif total_vertical > 0:
        overturning_capacity = total_vertical * wall.thickness / 2.0
        checks['overturning'] = _evaluated(
            'overturning', overturning_moment, overturning_capacity, 'N*m',
            'Restoring moment uses total vertical force acting at mid-thickness.'
        )
    else:
        checks['overturning'] = _not_evaluated(
            'overturning', 'N*m', 'no stabilizing vertical force is present'
        )

    if total_vertical is None:
        checks['eccentricity'] = _not_evaluated(
            'eccentricity', 'm', 'eccentricity is unknown because wall density is unknown'
        )
    elif total_vertical > 0:
        eccentricity = overturning_moment / total_vertical
        checks['eccentricity'] = _evaluated(
            'eccentricity', eccentricity, wall.thickness / 6.0, 'm',
            'Middle-third criterion: resultant eccentricity <= thickness/6.'
        )
    else:
        checks['eccentricity'] = _not_evaluated(
            'eccentricity', 'm', 'eccentricity is undefined without vertical force'
        )

    assumptions = (
        'All internal calculations use SI units.',
        'Level-1 model applies uniform lateral pressure to gross projected wall area.',
        'Openings define vertical load-carrying pier width by their horizontal projection; this is intentionally conservative for window openings.',
        'Vertical load is distributed among piers in proportion to pier width.',
        'Wall is treated as straight and prismatic with constant thickness.',
    )

    return WallAnalysisResult(
        self_weight=self_weight,
        external_vertical_force=external_vertical,
        total_vertical_force=total_vertical,
        lateral_force=lateral_force,
        overturning_moment=overturning_moment,
        load_bearing_width=load_bearing_width,
        piers=piers,
        checks=checks,
        assumptions=assumptions,
    )


def _pier_results(
    segments: tuple[tuple[float, float], ...],
    thickness: float,
    total_vertical: float | None,
    total_width: float,
) -> tuple[PierResult, ...]:
    results: list[PierResult] = []
    for start, end in segments:
        width = end - start
        force = None if total_vertical is None else (0.0 if total_width == 0 else total_vertical * width / total_width)
        area = width * thickness
        stress = None if force is None else (0.0 if area == 0 else force / area)
        results.append(PierResult(start=start, end=end, axial_force=force, stress=stress))
    return tuple(results)


def _value_si(value: EngineeringValue | None, dimension: str) -> float | None:
    if value is None:
        return None
    return to_si(Quantity(value.value, value.unit), dimension)


def _dimensionless(value: EngineeringValue | None) -> float | None:
    if value is None:
        return None
    if value.unit not in ('', '1', 'dimensionless'):
        raise ValueError(f'expected dimensionless engineering value, got {value.unit!r}')
    return value.value


def _evaluated(name: str, demand: float, capacity: float, unit: str, note: str) -> CheckResult:
    if capacity < 0:
        raise ValueError(f'{name} capacity cannot be negative')
    if capacity == 0:
        utilization = float('inf') if demand > 0 else 0.0
    else:
        utilization = demand / capacity
    return CheckResult(name, 'evaluated', demand, capacity, utilization, unit, note)


def _not_evaluated(name: str, unit: str, note: str) -> CheckResult:
    return CheckResult(name, 'not_evaluated', None, None, None, unit, note)
