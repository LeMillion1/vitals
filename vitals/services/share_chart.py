"""The doctor document's one chart — weight, drawn as inline SVG by hand.

Pure functions, no DB, no dependencies. Two reasons it isn't Chart.js like the
rest of the app: a ``<canvas>`` regularly prints blank (the chart is the first
thing to vanish from a PDF), and the downloaded ``.html`` has to open offline
from a double-click, which would mean inlining ~200 KB of library into every
copy. Two hundred lines of arithmetic buy both.

Colours are literal hex, not CSS variables: this markup travels into a file that
has no stylesheet of its own and gets printed on paper.
"""
from __future__ import annotations

import math
from datetime import date as date_type
from typing import Iterable, Optional, Sequence

Point = Sequence  # (iso_date: str, value: float) — a list after a JSON round-trip

_LINE = "#b45309"       # moving average — the line to read
_DOTS = "#a8a29e"       # individual weigh-ins — context, not the signal
_AXIS = "#d6d3d1"
_TEXT = "#57534e"
_GRID = "#ededeb"


def _nice_step(span: float) -> float:
    """A round step that lands roughly four gridlines across ``span``."""
    if span <= 0:
        return 1.0
    raw = span / 4
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _parse(points: Optional[Iterable[Point]]) -> list[tuple[int, float]]:
    """[(iso, value)] → [(ordinal, value)], sorted, junk dropped.

    A snapshot is JSON, so a point arrives as a two-element list; anything that
    doesn't look like one is skipped rather than raising — a chart is not worth
    a 500 on the one page a doctor is looking at.
    """
    out: list[tuple[int, float]] = []
    for item in points or ():
        try:
            day, value = item[0], item[1]
            out.append((date_type.fromisoformat(str(day)).toordinal(), float(value)))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    out.sort()
    return out


def _fmt(value: float) -> str:
    """Axis label: no trailing ".0" on a whole number of kilograms."""
    return f"{value:g}"


def weight_svg(
    points: Optional[Iterable[Point]],
    ma_points: Optional[Iterable[Point]] = None,
    *,
    width: int = 680,
    height: int = 240,
) -> str:
    """Weigh-ins plus their moving average as one standalone ``<svg>`` string.

    Empty input renders nothing at all (the caller drops the whole section);
    a single reading renders as one dot rather than dividing by a zero span.
    """
    raw = _parse(points)
    ma = _parse(ma_points)
    if not raw and not ma:
        return ""

    pad_l, pad_r, pad_t, pad_b = 46, 12, 12, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [v for _, v in raw] + [v for _, v in ma]
    lo, hi = min(values), max(values)
    if hi - lo < 0.5:  # a flat week must not become a straight line on the axis
        lo, hi = lo - 0.5, hi + 0.5
    step = _nice_step(hi - lo)
    y_lo = math.floor(lo / step) * step
    y_hi = math.ceil(hi / step) * step
    if y_hi == y_lo:
        y_hi = y_lo + step

    days = [d for d, _ in raw] + [d for d, _ in ma]
    x_lo, x_hi = min(days), max(days)
    x_span = x_hi - x_lo

    def sx(ordinal: int) -> float:
        if x_span == 0:
            return pad_l + plot_w / 2
        return pad_l + (ordinal - x_lo) / x_span * plot_w

    def sy(value: float) -> float:
        return pad_t + (y_hi - value) / (y_hi - y_lo) * plot_h

    parts: list[str] = [
        f'<svg class="doc-chart" viewBox="0 0 {width} {height}" '
        f'width="100%" preserveAspectRatio="xMidYMid meet" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
    ]

    # Y gridlines + labels.
    ticks = int(round((y_hi - y_lo) / step))
    for i in range(ticks + 1):
        value = y_lo + i * step
        y = sy(value)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="{_GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'font-size="10" fill="{_TEXT}">{_fmt(value)}</text>'
        )

    # X labels — at most five dates, spaced so short labels never collide.
    label_days = sorted({d for d in days})
    if len(label_days) > 5:
        stride = (len(label_days) - 1) / 4
        label_days = [label_days[round(i * stride)] for i in range(5)]
    for ordinal in label_days:
        d = date_type.fromordinal(ordinal)
        parts.append(
            f'<text x="{sx(ordinal):.1f}" y="{height - 8}" text-anchor="middle" '
            f'font-size="10" fill="{_TEXT}">{d.day:02d}.{d.month:02d}</text>'
        )
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" '
        f'y2="{pad_t + plot_h}" stroke="{_AXIS}" stroke-width="1"/>'
    )

    for ordinal, value in raw:
        parts.append(
            f'<circle cx="{sx(ordinal):.1f}" cy="{sy(value):.1f}" r="2" fill="{_DOTS}"/>'
        )

    if len(ma) >= 2:
        path = " ".join(f"{sx(d):.1f},{sy(v):.1f}" for d, v in ma)
        parts.append(
            f'<polyline points="{path}" fill="none" stroke="{_LINE}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        )
    elif len(ma) == 1:
        d, v = ma[0]
        parts.append(f'<circle cx="{sx(d):.1f}" cy="{sy(v):.1f}" r="3" fill="{_LINE}"/>')

    parts.append("</svg>")
    return "".join(parts)
