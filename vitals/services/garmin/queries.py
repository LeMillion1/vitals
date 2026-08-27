"""Subject-scoped Garmin read models and query APIs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import IntegrationProvider
from vitals.models.garmin import GarminActivity, GarminDaily, GarminIntraday
from vitals.models.tenancy import IntegrationConnection


def _garmin_subject_scope(model, subject_id: uuid.UUID):
    """Rows whose provenance connection belongs to the requested subject."""

    return and_(
        model.subject_id == subject_id,
        model.integration_connection_id.in_(
            select(IntegrationConnection.id).where(
                IntegrationConnection.subject_id == subject_id,
                IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
            )
        ),
    )


async def get_daily(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> Optional[GarminDaily]:
    result = await session.execute(
        select(GarminDaily).where(
            _garmin_subject_scope(GarminDaily, subject_id),
            GarminDaily.date == on_date,
        )
    )
    return result.scalars().first()


async def list_daily(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    limit: int = 30,
) -> Sequence[GarminDaily]:
    stmt = select(GarminDaily).where(
        _garmin_subject_scope(GarminDaily, subject_id)
    )
    if start is not None:
        stmt = stmt.where(GarminDaily.date >= start)
    if end is not None:
        stmt = stmt.where(GarminDaily.date <= end)
    stmt = stmt.order_by(GarminDaily.date.desc()).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_daily_between(
    session: AsyncSession,
    start: date_type,
    end: date_type,
    *,
    subject_id: uuid.UUID,
) -> Sequence[GarminDaily]:
    """Every day in a date range, chronological."""

    stmt = (
        select(GarminDaily)
        .where(
            _garmin_subject_scope(GarminDaily, subject_id),
            GarminDaily.date >= start,
            GarminDaily.date <= end,
        )
        .order_by(GarminDaily.date)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_nights(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    limit: int = 60,
) -> Sequence[GarminDaily]:
    """Days with a recorded sleep session, newest first."""

    result = await session.execute(
        select(GarminDaily)
        .where(
            _garmin_subject_scope(GarminDaily, subject_id),
            GarminDaily.sleep_seconds.is_not(None),
        )
        .order_by(GarminDaily.date.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def list_activities(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    limit: int = 20,
) -> Sequence[GarminActivity]:
    stmt = select(GarminActivity).where(
        _garmin_subject_scope(GarminActivity, subject_id)
    )
    if start is not None:
        stmt = stmt.where(GarminActivity.date >= start)
    if end is not None:
        stmt = stmt.where(GarminActivity.date <= end)
    stmt = stmt.order_by(
        GarminActivity.date.desc(),
        GarminActivity.start_time.desc(),
    ).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_intraday(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    series_types: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> Sequence[GarminIntraday]:
    """Intraday samples over a date window, oldest first."""

    stmt = select(GarminIntraday).where(
        _garmin_subject_scope(GarminIntraday, subject_id)
    )
    if start is not None:
        stmt = stmt.where(GarminIntraday.date >= start)
    if end is not None:
        stmt = stmt.where(GarminIntraday.date <= end)
    if series_types:
        stmt = stmt.where(GarminIntraday.series_type.in_(list(series_types)))
    stmt = stmt.order_by(GarminIntraday.ts, GarminIntraday.series_type)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def intraday_series_map(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
    series_types: Optional[Sequence[str]] = None,
) -> dict[str, list[dict]]:
    """One day's curves keyed by series type."""

    rows = await list_intraday(
        session,
        subject_id=subject_id,
        start=on_date,
        end=on_date,
        series_types=series_types,
    )
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row.series_type, []).append(
            {"ts": row.ts.isoformat(), "value": row.value}
        )
    return out


_REPORTED_DAILY_COLS = (
    GarminDaily.sleep_score,
    GarminDaily.sleep_seconds,
    GarminDaily.resting_hr,
    GarminDaily.hrv_avg,
    GarminDaily.body_battery_high,
    GarminDaily.avg_stress,
    GarminDaily.steps,
    GarminDaily.active_calories,
)


@dataclass(frozen=True, slots=True)
class RecoveryDayProjection:
    """Only the Garmin fields used by the shared recovery-advice policy."""

    date: date_type
    sleep_score: int | None
    body_battery_high: int | None
    spo2_lowest: int | None
    breathing_disruption: str | None


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    """Subject-owned reported-day coverage plus its latest recovery slice."""

    total_days_logged: int
    latest: RecoveryDayProjection | None


async def recovery_summary(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    before_or_on: date_type,
) -> RecoverySummary:
    """Return a column-minimal recovery summary with exact account provenance."""

    scope = _garmin_subject_scope(GarminDaily, subject_id)
    reported = or_(*(column.is_not(None) for column in _REPORTED_DAILY_COLS))
    total = int(
        await session.scalar(
            select(func.count(GarminDaily.id)).where(
                scope,
                GarminDaily.date <= before_or_on,
                reported,
            )
        )
        or 0
    )
    latest = (
        await session.execute(
            select(
                GarminDaily.date,
                GarminDaily.sleep_score,
                GarminDaily.body_battery_high,
                GarminDaily.spo2_lowest,
                GarminDaily.breathing_disruption,
            )
            .where(
                scope,
                GarminDaily.date <= before_or_on,
                reported,
            )
            .order_by(GarminDaily.date.desc(), GarminDaily.id.desc())
            .limit(1)
        )
    ).first()
    return RecoverySummary(
        total_days_logged=total,
        latest=(
            RecoveryDayProjection(
                date=latest.date,
                sleep_score=latest.sleep_score,
                body_battery_high=latest.body_battery_high,
                spo2_lowest=latest.spo2_lowest,
                breathing_disruption=latest.breathing_disruption,
            )
            if latest is not None
            else None
        ),
    )


async def latest_daily(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    before_or_on: Optional[date_type] = None,
) -> Optional[GarminDaily]:
    """The newest day carrying a real metric, not merely a placeholder row."""

    stmt = select(GarminDaily).where(
        _garmin_subject_scope(GarminDaily, subject_id),
        or_(*(col.is_not(None) for col in _REPORTED_DAILY_COLS)),
    )
    if before_or_on is not None:
        stmt = stmt.where(GarminDaily.date <= before_or_on)
    stmt = stmt.order_by(GarminDaily.date.desc()).limit(1)
    result = await session.execute(stmt)
    return result.scalars().first()


async def adjacent_night_dates(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> tuple[Optional[date_type], Optional[date_type]]:
    """Nearest earlier and later subject-owned dates with recorded sleep."""

    prev_result = await session.execute(
        select(GarminDaily.date)
        .where(
            _garmin_subject_scope(GarminDaily, subject_id),
            GarminDaily.sleep_seconds.is_not(None),
            GarminDaily.date < on_date,
        )
        .order_by(GarminDaily.date.desc())
        .limit(1)
    )
    next_result = await session.execute(
        select(GarminDaily.date)
        .where(
            _garmin_subject_scope(GarminDaily, subject_id),
            GarminDaily.sleep_seconds.is_not(None),
            GarminDaily.date > on_date,
        )
        .order_by(GarminDaily.date.asc())
        .limit(1)
    )
    return prev_result.scalar(), next_result.scalar()


async def daily_count(session: AsyncSession, *, subject_id: uuid.UUID) -> int:
    """Count non-placeholder subject-owned days."""

    result = await session.execute(
        select(func.count())
        .select_from(GarminDaily)
        .where(
            _garmin_subject_scope(GarminDaily, subject_id),
            or_(
                GarminDaily.sleep_score.is_not(None),
                GarminDaily.resting_hr.is_not(None),
                GarminDaily.hrv_avg.is_not(None),
            ),
        )
    )
    return int(result.scalar() or 0)
