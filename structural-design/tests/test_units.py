import math
import pytest

from design_platform.units import Quantity, to_si, from_si


def test_length_converts_feet_to_meters():
    assert math.isclose(to_si(Quantity(10, 'ft'), 'length'), 3.048)


def test_length_converts_inches_to_meters():
    assert math.isclose(to_si(Quantity(16, 'in'), 'length'), 0.4064)


def test_force_converts_lbf_to_newtons():
    assert math.isclose(to_si(Quantity(100, 'lbf'), 'force'), 444.82216152605)


def test_density_converts_pcf_to_kg_m3():
    assert math.isclose(to_si(Quantity(100, 'pcf'), 'density'), 1601.846337396257, rel_tol=1e-12)


def test_pressure_converts_psf_to_pa():
    assert math.isclose(to_si(Quantity(40, 'psf'), 'pressure'), 1915.2112431600002, rel_tol=1e-12)


def test_round_trip_conversion():
    si = to_si(Quantity(24, 'ft'), 'length')
    assert math.isclose(from_si(si, 'ft', 'length'), 24.0)


def test_dimension_mismatch_is_rejected():
    with pytest.raises(ValueError, match='not valid for dimension'):
        to_si(Quantity(10, 'lbf'), 'length')
