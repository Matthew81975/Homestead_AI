from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ValueBasis = Literal['measured', 'sourced', 'assumed', 'calibrated', 'unknown']


@dataclass(frozen=True)
class EngineeringValue:
    value: float
    unit: str
    source: str | None = None
    basis: ValueBasis = 'unknown'

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError('engineering values must be nonnegative')


@dataclass(frozen=True)
class EarthbagAssembly:
    density: EngineeringValue | None = None
    compressive_strength: EngineeringValue | None = None
    shear_strength: EngineeringValue | None = None
    friction_coefficient: EngineeringValue | None = None
    bag_width: float | None = None
    course_height: float | None = None
    reinforcement_description: str | None = None
    skin_description: str | None = None

    def __post_init__(self) -> None:
        for name in ('bag_width', 'course_height'):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f'{name} must be positive when provided')


@dataclass(frozen=True)
class RubbleAssembly:
    density: EngineeringValue | None = None
    compressive_strength: EngineeringValue | None = None
    shear_strength: EngineeringValue | None = None
    friction_coefficient: EngineeringValue | None = None
    void_ratio: float | None = None
    mortared: bool | None = None
    confinement_description: str | None = None
    facing_description: str | None = None
    tie_description: str | None = None

    def __post_init__(self) -> None:
        if self.void_ratio is not None and not 0.0 <= self.void_ratio < 1.0:
            raise ValueError('void_ratio must satisfy 0 <= void_ratio < 1')
