"""Health analytics and declarative metric metadata.

The calculation modules are pure and unit-tested without DB or I/O. The chart
registry contains declarative ORM field references, but performs no queries.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional, Sequence, Tuple

Point = Tuple[date, float]


def exclude_ranges(
    points: Iterable[Point],
    ranges: Sequence[Tuple[date, Optional[date]]],
) -> list[Point]:
    """Drop points whose date falls inside any ``(start, end)`` noise range.

    ``end`` may be ``None`` (open-ended / ongoing noise period) — then every date
    >= start is excluded. Ranges are inclusive on both ends.
    """
    norm = [(s, e) for (s, e) in ranges]

    def _in_noise(d: date) -> bool:
        for start, end in norm:
            if end is None:
                if d >= start:
                    return True
            elif start <= d <= end:
                return True
        return False

    return [(d, v) for (d, v) in points if not _in_noise(d)]
