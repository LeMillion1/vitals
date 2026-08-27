"""Pure vendor-shape normalization helpers for the Hevy bounded context."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from vitals.utils.timeutils import to_local_naive


# Only these set types are "working sets" that drive progression / top-weight.
_WORKING_SET_TYPES = {"normal", "failure"}


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse a Hevy ISO-8601 timestamp into a naive local datetime."""

    if not value:
        return None
    if isinstance(value, datetime):
        return to_local_naive(value)
    try:
        text = str(value).replace("Z", "+00:00")
        return to_local_naive(datetime.fromisoformat(text))
    except (ValueError, TypeError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _map_program(raw_workout: dict) -> Optional[str]:
    """Best-effort training-program tag from the workout title."""

    title = (raw_workout.get("title") or "").lower()
    for token, label in (
        ("program a", "A"),
        ("program b", "B"),
        ("day a", "A"),
        ("day b", "B"),
    ):
        if token in title:
            return label
    return None


__all__ = [
    "_WORKING_SET_TYPES",
    "_float_or_none",
    "_int_or_none",
    "_map_program",
    "_parse_dt",
]
