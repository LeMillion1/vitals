"""The doctor document's weight chart — pure geometry, no DB.

The failure modes worth a test are the shapes real data takes on the edges: an
owner who weighed in once in the period, a period with nothing in it at all, and
a flat week where every reading is the same number.
"""
from __future__ import annotations

import re

from vitals.analytics.share_chart import weight_svg

_VIEWBOX = re.compile(r'viewBox="0 0 (\d+) (\d+)"')


def _coords(svg: str) -> list[tuple[float, float]]:
    """Every drawn point: circle centres plus the polyline's vertices."""
    out = [
        (float(x), float(y))
        for x, y in re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)"', svg)
    ]
    for points in re.findall(r'<polyline points="([^"]+)"', svg):
        out += [
            (float(pair.split(",")[0]), float(pair.split(",")[1]))
            for pair in points.split(" ")
        ]
    return out


def test_renders_a_well_formed_svg():
    svg = weight_svg(
        [["2026-05-01", 88.4], ["2026-05-08", 87.9], ["2026-05-15", 86.6]],
        [["2026-05-08", 88.1], ["2026-05-15", 87.2]],
    )
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count("<svg") == 1
    assert "<polyline" in svg


def test_every_point_lands_inside_the_viewbox():
    svg = weight_svg(
        [["2026-01-%02d" % d, 90 - d * 0.2] for d in range(1, 29)],
        [["2026-01-%02d" % d, 90 - d * 0.15] for d in range(7, 29)],
    )
    width, height = (int(n) for n in _VIEWBOX.search(svg).groups())
    coords = _coords(svg)
    assert coords
    assert all(0 <= x <= width and 0 <= y <= height for x, y in coords)


def test_empty_input_draws_nothing():
    assert weight_svg([], []) == ""
    assert weight_svg(None, None) == ""


def test_single_reading_does_not_divide_by_zero():
    svg = weight_svg([["2026-03-04", 84.0]], [])
    assert svg.startswith("<svg")
    assert len(_coords(svg)) == 1


def test_flat_series_keeps_the_line_off_the_axis():
    """Identical readings must still get a scale — a zero span would put every
    point on the same pixel row and a division by zero in the way."""
    svg = weight_svg([["2026-02-01", 80.0], ["2026-02-08", 80.0]], [])
    ys = {y for _, y in _coords(svg)}
    assert len(ys) == 1
    height = int(_VIEWBOX.search(svg).group(2))
    assert 0 < ys.pop() < height


def test_malformed_points_are_skipped_not_raised():
    svg = weight_svg([["not-a-date", 80.0], ["2026-02-01", 80.0], ["2026-02-08", 81.0]], [])
    assert svg.startswith("<svg")
    assert len(_coords(svg)) == 2
