"""Pure reference-range classification for the Labs bounded context."""
from __future__ import annotations

from typing import Optional

from vitals.enums import LabFlag


CRITICAL_WIDTH_FACTOR = 0.5
CRITICAL_MARGIN = 0.30


def compute_flag(
    value: float,
    ref_low: Optional[float],
    ref_high: Optional[float],
    *,
    width_factor: float = CRITICAL_WIDTH_FACTOR,
    critical_margin: float = CRITICAL_MARGIN,
) -> Optional[str]:
    """Classify a value against a possibly one-sided reference range."""

    if ref_low is None and ref_high is None:
        return None
    width = (
        ref_high - ref_low
        if ref_low is not None and ref_high is not None
        else None
    )

    if ref_low is not None and value < ref_low:
        critical = (
            value < ref_low - width_factor * width
            if width is not None
            else value <= ref_low * (1 - critical_margin)
        )
        return LabFlag.CRITICAL_LOW.value if critical else LabFlag.LOW.value

    if ref_high is not None and value > ref_high:
        critical = (
            value > ref_high + width_factor * width
            if width is not None
            else value >= ref_high * (1 + critical_margin)
        )
        return LabFlag.CRITICAL_HIGH.value if critical else LabFlag.HIGH.value

    return LabFlag.NORMAL.value


def is_out_of_range(flag: Optional[str]) -> bool:
    return flag in (
        LabFlag.LOW.value,
        LabFlag.HIGH.value,
        LabFlag.CRITICAL_LOW.value,
        LabFlag.CRITICAL_HIGH.value,
    )


def is_critical(flag: Optional[str]) -> bool:
    return flag in (LabFlag.CRITICAL_LOW.value, LabFlag.CRITICAL_HIGH.value)


__all__ = [
    "CRITICAL_MARGIN",
    "CRITICAL_WIDTH_FACTOR",
    "compute_flag",
    "is_critical",
    "is_out_of_range",
]
