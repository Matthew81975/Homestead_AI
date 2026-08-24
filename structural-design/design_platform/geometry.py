from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RectOpening:
    left: float
    bottom: float
    width: float
    height: float
    kind: str = 'opening'

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError('opening width and height must be positive')
        if self.left < 0 or self.bottom < 0:
            raise ValueError('opening coordinates must be nonnegative')

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def top(self) -> float:
        return self.bottom + self.height

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass(frozen=True)
class StraightWallGeometry:
    length: float
    height: float
    thickness: float
    openings: tuple[RectOpening, ...] = ()

    def __post_init__(self) -> None:
        if self.length <= 0 or self.height <= 0 or self.thickness <= 0:
            raise ValueError('wall dimensions must be positive')
        for opening in self.openings:
            if opening.right > self.length or opening.top > self.height:
                raise ValueError('openings must remain inside wall bounds')
        for i, first in enumerate(self.openings):
            for second in self.openings[i + 1:]:
                if _rectangles_overlap(first, second):
                    raise ValueError('openings may not overlap')

    @property
    def gross_area(self) -> float:
        return self.length * self.height

    @property
    def opening_area(self) -> float:
        return sum(opening.area for opening in self.openings)

    @property
    def net_area(self) -> float:
        return self.gross_area - self.opening_area

    @property
    def volume(self) -> float:
        return self.net_area * self.thickness

    def pier_segments(self) -> tuple[tuple[float, float], ...]:
        spans = sorted((o.left, o.right) for o in self.openings)
        if not spans:
            return ((0.0, self.length),)
        merged: list[list[float]] = []
        for left, right in spans:
            if not merged or left > merged[-1][1]:
                merged.append([left, right])
            else:
                merged[-1][1] = max(merged[-1][1], right)
        piers: list[tuple[float, float]] = []
        cursor = 0.0
        for left, right in merged:
            if left > cursor:
                piers.append((cursor, left))
            cursor = max(cursor, right)
        if cursor < self.length:
            piers.append((cursor, self.length))
        return tuple(piers)


def _rectangles_overlap(a: RectOpening, b: RectOpening) -> bool:
    horizontal = a.left < b.right and b.left < a.right
    vertical = a.bottom < b.top and b.bottom < a.top
    return horizontal and vertical
