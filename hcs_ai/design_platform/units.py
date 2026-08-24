from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str


_FACTORS: dict[str, dict[str, float]] = {
    'length': {'m': 1.0, 'cm': 0.01, 'mm': 0.001, 'ft': 0.3048, 'in': 0.0254},
    'force': {'N': 1.0, 'kN': 1000.0, 'lbf': 4.4482216152605, 'kip': 4448.2216152605},
    'density': {'kg/m3': 1.0, 'kg/m^3': 1.0, 'pcf': 16.01846337396014, 'lb/ft3': 16.01846337396014, 'lb/ft^3': 16.01846337396014},
    'pressure': {'Pa': 1.0, 'kPa': 1000.0, 'MPa': 1_000_000.0, 'psf': 47.880281079000005, 'psi': 6894.757293168, 'ksi': 6_894_757.293168},
}


def _factor(unit: str, dimension: str) -> float:
    try:
        table = _FACTORS[dimension]
    except KeyError as exc:
        raise ValueError(f'unknown dimension: {dimension}') from exc
    try:
        return table[unit]
    except KeyError as exc:
        raise ValueError(f'unit {unit!r} is not valid for dimension {dimension!r}') from exc


def to_si(quantity: Quantity, dimension: str) -> float:
    return float(quantity.value) * _factor(quantity.unit, dimension)


def from_si(value: float, unit: str, dimension: str) -> float:
    return float(value) / _factor(unit, dimension)
