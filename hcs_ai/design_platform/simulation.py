from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PhysicsDomain(str, Enum):
    STRUCTURAL = "structural"
    CONTACT = "contact"
    SOIL_STRUCTURE = "soil_structure"
    THERMAL = "thermal"
    THERMO_MECHANICAL = "thermo_mechanical"
    POROUS_FLOW = "porous_flow"
    FLUID = "fluid"


@dataclass(frozen=True)
class SimulationLoad:
    kind: str
    magnitude: float
    unit: str
    metadata: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("simulation load kind must not be empty")
        if self.magnitude < 0:
            raise ValueError("simulation load magnitude must be nonnegative")
        if not self.unit:
            raise ValueError("simulation load unit must not be empty")


@dataclass(frozen=True)
class SimulationCase:
    name: str
    domains: tuple[PhysicsDomain, ...]
    loads: tuple[SimulationLoad, ...] = ()
    solver: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("simulation case name must not be empty")
        if not self.domains:
            raise ValueError("simulation case must include at least one physics domain")
