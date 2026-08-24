import math
import pytest

from design_platform.geometry import RectOpening, StraightWallGeometry


def test_wall_areas_subtract_openings():
    wall = StraightWallGeometry(length=6.0, height=3.0, thickness=0.4, openings=(RectOpening(left=2.0, bottom=0.0, width=1.0, height=2.1, kind='door'),))
    assert math.isclose(wall.gross_area, 18.0)
    assert math.isclose(wall.opening_area, 2.1)
    assert math.isclose(wall.net_area, 15.9)
    assert math.isclose(wall.volume, 15.9 * 0.4)


def test_opening_outside_wall_is_rejected():
    with pytest.raises(ValueError, match='inside wall bounds'):
        StraightWallGeometry(length=6.0, height=3.0, thickness=0.4, openings=(RectOpening(left=5.5, bottom=1.0, width=1.0, height=1.0),))


def test_overlapping_openings_are_rejected():
    with pytest.raises(ValueError, match='overlap'):
        StraightWallGeometry(length=6.0, height=3.0, thickness=0.4, openings=(RectOpening(left=1.0, bottom=0.5, width=2.0, height=1.5), RectOpening(left=2.0, bottom=1.0, width=2.0, height=1.5)))


def test_pier_segments_are_horizontal_solids_between_openings():
    wall = StraightWallGeometry(length=8.0, height=3.0, thickness=0.4, openings=(RectOpening(left=1.0, bottom=1.0, width=1.5, height=1.0), RectOpening(left=5.0, bottom=0.0, width=1.0, height=2.1)))
    assert wall.pier_segments() == ((0.0, 1.0), (2.5, 5.0), (6.0, 8.0))


def test_nonpositive_dimensions_are_rejected():
    with pytest.raises(ValueError, match='positive'):
        StraightWallGeometry(length=0, height=3.0, thickness=0.4)
