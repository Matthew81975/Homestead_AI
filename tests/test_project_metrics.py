import math
import pytest

from hcs_ai.design_platform.metrics import SpatialMetrics, aggregate_metrics


def test_spatial_metrics_reports_efficiency_ratios():
    metrics = SpatialMetrics(
        footprint_area=100.0,
        gross_floor_area=180.0,
        usable_floor_area=150.0,
        enclosed_volume=540.0,
        usable_volume=450.0,
        conditioned_volume=400.0,
        envelope_area=300.0,
    )
    assert math.isclose(metrics.space_efficiency, 150.0 / 180.0)
    assert math.isclose(metrics.envelope_efficiency, 450.0 / 300.0)


def test_aggregate_metrics_sums_selection_or_project_scope():
    room_a = SpatialMetrics(
        footprint_area=40.0,
        gross_floor_area=40.0,
        usable_floor_area=35.0,
        enclosed_volume=120.0,
        usable_volume=105.0,
        conditioned_volume=105.0,
        below_grade_volume=0.0,
        material_volume=8.0,
    )
    room_b = SpatialMetrics(
        footprint_area=30.0,
        gross_floor_area=30.0,
        usable_floor_area=26.0,
        enclosed_volume=90.0,
        usable_volume=78.0,
        conditioned_volume=0.0,
        below_grade_volume=45.0,
        material_volume=6.0,
    )
    total = aggregate_metrics((room_a, room_b))
    assert total.footprint_area == 70.0
    assert total.usable_floor_area == 61.0
    assert total.usable_volume == 183.0
    assert total.conditioned_volume == 105.0
    assert total.below_grade_volume == 45.0
    assert total.material_volume == 14.0


def test_metrics_reject_negative_physical_quantities():
    with pytest.raises(ValueError, match="nonnegative"):
        SpatialMetrics(footprint_area=-1.0)


def test_zero_denominator_efficiencies_are_unknown():
    metrics = SpatialMetrics()
    assert metrics.space_efficiency is None
    assert metrics.envelope_efficiency is None
