from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CheckStatus = Literal['evaluated', 'not_evaluated']


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    demand: float | None
    capacity: float | None
    utilization: float | None
    unit: str
    note: str = ''


@dataclass(frozen=True)
class PierResult:
    start: float
    end: float
    axial_force: float | None
    stress: float | None

    @property
    def width(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class WallAnalysisResult:
    self_weight: float | None
    external_vertical_force: float
    total_vertical_force: float | None
    lateral_force: float
    overturning_moment: float
    load_bearing_width: float
    piers: tuple[PierResult, ...]
    checks: dict[str, CheckResult]
    assumptions: tuple[str, ...]

    @property
    def governing_check(self) -> CheckResult | None:
        evaluated = [
            check for check in self.checks.values()
            if check.status == 'evaluated' and check.utilization is not None
        ]
        return max(evaluated, key=lambda item: item.utilization, default=None)
