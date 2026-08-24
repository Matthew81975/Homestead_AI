from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WallLoads:
    """Level-1 wall loads in canonical SI units.

    vertical_line_load: N/m applied uniformly along wall length.
    lateral_pressure: Pa applied uniformly to gross projected wall area.
    """

    vertical_line_load: float = 0.0
    lateral_pressure: float = 0.0

    def __post_init__(self) -> None:
        if self.vertical_line_load < 0:
            raise ValueError('vertical_line_load must be nonnegative')
        if self.lateral_pressure < 0:
            raise ValueError('lateral_pressure must be nonnegative')
