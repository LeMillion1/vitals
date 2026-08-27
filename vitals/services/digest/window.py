"""Digest reporting windows and coverage metadata."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type, timedelta
from typing import Any, Optional, Sequence

from vitals.utils.timeutils import today_local

CONTEXT_SCHEMA_VERSION = 2
REPORT_MODE_CLOSED = "closed_period"
REPORT_MODE_BRIEF = "daily_brief"
MIN_PERIOD_DAYS = 1
MAX_PERIOD_DAYS = 90
@dataclass(frozen=True)
class ReportWindow:
    """One authoritative set of date boundaries for every context query."""

    report_date: date_type
    period_start: date_type
    period_end: date_type
    previous_start: date_type
    previous_end: date_type
    period_days: int
    mode: str


def report_window(
    *,
    on_date: Optional[date_type] = None,
    period_days: int = 7,
    mode: str = REPORT_MODE_CLOSED,
    max_period_days: int = MAX_PERIOD_DAYS,
) -> ReportWindow:
    """Validate and resolve the report window without touching the database.

    A period report contains completed days. A daily brief is the one explicit
    exception: it is a current-day snapshot, and its caller opts into that mode
    instead of overloading ``period_days == 1`` with two meanings.
    """
    if isinstance(period_days, bool) or not isinstance(period_days, int):
        raise ValueError("period_days must be an integer")
    if not MIN_PERIOD_DAYS <= period_days <= max_period_days:
        raise ValueError(
            f"period_days must be between {MIN_PERIOD_DAYS} and {max_period_days}"
        )
    if mode not in {REPORT_MODE_CLOSED, REPORT_MODE_BRIEF}:
        raise ValueError(f"unsupported report mode: {mode}")
    if mode == REPORT_MODE_BRIEF and period_days != 1:
        raise ValueError("daily_brief mode requires period_days=1")

    local_today = today_local()
    report_date = on_date or local_today
    if report_date > local_today:
        raise ValueError("on_date cannot be in the future")

    period_end = report_date
    if mode == REPORT_MODE_CLOSED and report_date == local_today:
        period_end -= timedelta(days=1)
    period_start = period_end - timedelta(days=period_days - 1)
    previous_end = period_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days - 1)
    return ReportWindow(
        report_date=report_date,
        period_start=period_start,
        period_end=period_end,
        previous_start=previous_start,
        previous_end=previous_end,
        period_days=period_days,
        mode=mode,
    )


def _period_name(on_date: date_type, window: ReportWindow) -> Optional[str]:
    if window.period_start <= on_date <= window.period_end:
        return "current"
    if window.previous_start <= on_date <= window.previous_end:
        return "previous"
    return None


def _coverage(
    *,
    module: str,
    enabled: bool,
    dates: Sequence[date_type] = (),
    window: ReportWindow,
    rows: Optional[int] = None,
    truncated: bool = False,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Describe what a block could see, so absence is not guessed from null."""
    dated = list(dates)
    current_rows = sum(
        1 for value in dated if window.period_start <= value <= window.period_end
    )
    previous_rows = sum(
        1 for value in dated if window.previous_start <= value <= window.previous_end
    )
    total_rows = len(dated) if rows is None else rows
    latest_date = max(dated) if dated else None
    out: dict[str, Any] = {
        "module": module,
        "enabled": enabled,
        "status": "disabled" if not enabled else ("available" if total_rows else "empty"),
        "rows": total_rows,
        "current_rows": current_rows,
        "previous_rows": previous_rows,
        "first_date": min(dated).isoformat() if dated else None,
        "last_date": latest_date.isoformat() if latest_date else None,
        "freshness_days": (
            (window.period_end - latest_date).days if latest_date else None
        ),
        "truncated": bool(truncated),
    }
    if extra:
        out.update(extra)
    return out
