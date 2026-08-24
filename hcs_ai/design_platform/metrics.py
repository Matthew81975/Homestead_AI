from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class SpatialMetrics:
    footprint_area: float = 0.0
    gross_floor_area: float = 0.0
    usable_floor_area: float = 0.0
    enclosed_volume: float = 0.0
    usable_volume: float = 0.0
    conditioned_volume: float = 0.0
    envelope_area: float = 0.0
    below_grade_volume: float = 0.0
    material_volume: float = 0.0
    exterior_perimeter: float = 0.0
    roof_area: float = 0.0
    foundation_area: float = 0.0
    excavation_volume: float = 0.0
    fill_volume: float = 0.0
    wall_surface_area: float = 0.0
    opening_area: float = 0.0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = float(getattr(self, item.name))
            if value < 0:
                raise ValueError(f'{item.name} must be nonnegative')

    @property
    def space_efficiency(self) -> float | None:
        if self.gross_floor_area <= 0:
            return None
        return self.usable_floor_area / self.gross_floor_area

    @property
    def envelope_efficiency(self) -> float | None:
        if self.envelope_area <= 0:
            return None
        return self.usable_volume / self.envelope_area

    @property
    def conditioned_fraction(self) -> float | None:
        if self.usable_volume <= 0:
            return None
        return self.conditioned_volume / self.usable_volume

    @property
    def below_grade_fraction(self) -> float | None:
        if self.enclosed_volume <= 0:
            return None
        return self.below_grade_volume / self.enclosed_volume


def aggregate_metrics(items: tuple[SpatialMetrics, ...] | list[SpatialMetrics]) -> SpatialMetrics:
    totals = {
        item.name: sum(float(getattr(metrics, item.name)) for metrics in items)
        for item in fields(SpatialMetrics)
    }
    return SpatialMetrics(**totals)
