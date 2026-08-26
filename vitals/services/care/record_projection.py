"""Strictly authorized projection for the care-team record screen.

The general digest assembler is intentionally a full cross-domain report: its
core modules are always read and it builds report-only joins even when a caller
later keeps one block.  A care screen has the opposite contract.  Patient
consent or a support grant is an exact allowlist, so a domain outside it must
not even be queried and then discarded.

This module therefore owns the small, visual summaries rendered by
``care/_record.html``.  Every loader is selected only after both the policy and
the patient's module setting agree, and the returned ``loaded_domains`` is the
single source for the support disclosure audit.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date as date_type
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.analytics import exclude_ranges
from vitals.analytics.regression import fit_trend
from vitals.analytics.rolling import rolling_mean_by_date
from vitals.access import (
    AccessContext,
    AccessRequest,
    PolicyAction,
    PolicyResourceType,
    is_allowed,
)
from vitals.enums import Domain
from vitals.services import digest_service
from vitals.utils.timeutils import subject_timezone


_WEIGHT_HISTORY_LIMIT = 400
_WEIGHT_NOISE_LIMIT = 100
_LAB_MARKER_LIMIT = 200
_CATALOG_LIMIT = 100
_SKINCARE_OBSERVATION_LIMIT = 200
_GENETICS_LIMIT = 100
_HRT_ITEM_LIMIT = 50


@dataclass(frozen=True, slots=True)
class _Section:
    key: str
    domain: Domain
    module: str


SECTIONS: tuple[_Section, ...] = (
    _Section("weight", Domain.WEIGHT, "weight"),
    _Section("labs", Domain.LABS, "labs"),
    _Section("body_comp", Domain.BODY_COMPOSITION, "body_comp"),
    _Section("nutrition", Domain.NUTRITION, "nutrition"),
    _Section("hrt", Domain.HRT, "hrt"),
    _Section("glp1", Domain.GLP1, "glp1"),
    _Section("supplements", Domain.SUPPLEMENTS, "supplements"),
    _Section("skincare", Domain.SKINCARE, "skincare"),
    _Section("genetics", Domain.GENETICS, "genetics"),
    _Section("garmin", Domain.GARMIN, "garmin"),
    _Section("hevy", Domain.WORKOUTS, "hevy"),
)
CARE_DOMAINS: tuple[Domain, ...] = tuple(section.domain for section in SECTIONS)


def enabled_care_domains(enabled_modules: Mapping[str, bool]) -> tuple[Domain, ...]:
    """Domains this installation can actually render to a professional."""

    return tuple(
        section.domain
        for section in SECTIONS
        if bool(enabled_modules.get(section.module, False))
    )


@dataclass(frozen=True, slots=True)
class RecordProjection:
    record: Mapping[str, Any]
    coverage: Mapping[str, Mapping[str, Any]]
    period: Mapping[str, Any]
    withheld_domains: tuple[str, ...]
    loaded_domains: tuple[str, ...]
    restricted: bool


@dataclass(frozen=True, slots=True)
class _LoadedSection:
    value: Any
    row_count: int
    dates: tuple[date_type, ...] = ()
    truncated: bool = False
    current_rows: int | None = None
    coverage_extra: Mapping[str, Any] | None = None


Loader = Callable[
    [AsyncSession, uuid.UUID, digest_service.ReportWindow], Awaitable[_LoadedSection]
]


def _mean(values: Sequence[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return round(sum(present) / len(present), 1) if present else None


def _coverage(
    *, section: _Section, loaded: _LoadedSection, window: digest_service.ReportWindow
) -> dict[str, Any]:
    dates = loaded.dates
    latest = max(dates) if dates else None
    value = {
        "module": section.module,
        "enabled": True,
        "status": "available" if loaded.row_count else "empty",
        "rows": loaded.row_count,
        "current_rows": (
            loaded.current_rows
            if loaded.current_rows is not None
            else sum(window.period_start <= day <= window.period_end for day in dates)
        ),
        "previous_rows": sum(
            window.previous_start <= day <= window.previous_end for day in dates
        ),
        "first_date": min(dates).isoformat() if dates else None,
        "last_date": latest.isoformat() if latest else None,
        "freshness_days": (window.period_end - latest).days if latest else None,
        "truncated": loaded.truncated,
    }
    if loaded.coverage_extra:
        value.update(loaded.coverage_extra)
    return value


def _may_read(context: AccessContext, domain: Domain) -> bool:
    return is_allowed(
        context,
        AccessRequest(
            subject_id=context.subject_id,
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=domain.value,
            action=PolicyAction.READ,
        ),
    )


async def _load_weight(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    from vitals.services import weight_service

    history = await weight_service.care_weight_history(
        session,
        subject_id=subject_id,
        end=window.period_end,
        history_limit=_WEIGHT_HISTORY_LIMIT,
        noise_limit=_WEIGHT_NOISE_LIMIT,
    )
    rows = list(history.rows)
    markers = history.noise_markers
    points = [(row.date, row.weight_kg) for row in rows]
    ranges = [(marker.start_date, marker.end_date) for marker in markers]
    clean_points = [] if history.noise_truncated else exclude_ranges(points, ranges)
    moving_average = rolling_mean_by_date(clean_points, window_days=7)
    trend = None if history.noise_truncated else fit_trend(points, exclude=ranges)
    latest = rows[-1] if rows else None
    latest_ma = moving_average[-1] if moving_average else None
    return _LoadedSection(
        value={
            "latest_kg": latest.weight_kg if latest else None,
            "latest_date": latest.date.isoformat() if latest else None,
            "ma7_kg": latest_ma[1] if latest_ma else None,
            "trend_kg_per_week": (
                round(trend.slope_per_week, 3) if trend else None
            ),
        },
        row_count=len(rows),
        dates=tuple(row.date for row in rows),
        truncated=history.history_truncated or history.noise_truncated,
        coverage_extra={
            "history_limit": _WEIGHT_HISTORY_LIMIT,
            "noise_limit": _WEIGHT_NOISE_LIMIT,
            "noise_truncated": history.noise_truncated,
        },
    )


async def _load_labs(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    from vitals.services import labs_service

    page = await labs_service.bounded_latest_results_by_marker(
        session,
        end=window.period_end,
        marker_limit=_LAB_MARKER_LIMIT,
        subject_id=subject_id,
    )
    rows = list(page.rows)
    flagged = [
        {
            "marker": row.marker,
            "value": row.value,
            "unit": row.unit,
            "flag": row.flag,
            "date": row.date.isoformat(),
            "ref_low": row.ref_low,
            "ref_high": row.ref_high,
        }
        for row in rows
        if labs_service.is_out_of_range(row.flag)
        and 0 <= (window.period_end - row.date).days <= 14
    ]
    return _LoadedSection(
        value={"out_of_range": flagged},
        row_count=len(rows),
        dates=tuple(row.date for row in rows),
        truncated=page.truncated,
        coverage_extra={"marker_limit": _LAB_MARKER_LIMIT},
    )


async def _load_body_comp(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    from vitals.services import body_scan_service

    row = await body_scan_service.latest_scan(
        session,
        subject_id=subject_id,
        before_or_on=window.period_end,
    )
    return _LoadedSection(
        value={"date": row.date.isoformat(), "device": row.device} if row else None,
        row_count=int(row is not None),
        dates=(row.date,) if row else (),
    )


async def _load_nutrition(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    from vitals.models.nutrition import MealLog

    daily = (
        await session.execute(
            select(
                MealLog.date,
                func.count(MealLog.id),
                func.sum(MealLog.calories),
                func.sum(MealLog.protein_g),
                func.count(MealLog.calories),
                func.count(MealLog.protein_g),
            )
            .where(
                MealLog.subject_id == subject_id,
                MealLog.domain == Domain.NUTRITION.value,
                MealLog.date >= window.period_start,
                MealLog.date <= window.period_end,
            )
            .group_by(MealLog.date)
            .order_by(MealLog.date)
        )
    ).all()
    calories = [row[2] for row in daily]
    protein = [row[3] for row in daily]
    meal_count = sum(int(row[1]) for row in daily)
    calorie_samples = sum(int(row[4]) for row in daily)
    protein_samples = sum(int(row[5]) for row in daily)
    return _LoadedSection(
        value={
            "avg_calories_per_day": _mean(calories),
            "avg_protein_per_day_g": _mean(protein),
            "days_with_logs": len(daily),
            "metric_samples": {
                "calories": calorie_samples,
                "protein_g": protein_samples,
            },
        },
        row_count=meal_count,
        dates=tuple(row[0] for row in daily),
        current_rows=meal_count,
        coverage_extra={
            "metric_samples": {
                "calories": calorie_samples,
                "protein_g": protein_samples,
            }
        },
    )


async def _load_hrt(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    from vitals.services import hrt_cycle_service

    cycle = await hrt_cycle_service.care_active_cycle(
        session,
        on_date=window.period_end,
        subject_id=subject_id,
        item_limit=_HRT_ITEM_LIMIT,
    )
    value = None
    if cycle is not None:
        value = {
            "cycle": {
                "name": cycle.name,
                "start_date": cycle.start_date.isoformat(),
                "compounds": list(cycle.compounds),
            }
        }
    return _LoadedSection(
        value=value,
        row_count=int(cycle is not None),
        dates=(cycle.start_date,) if cycle else (),
        truncated=bool(cycle and cycle.compounds_truncated),
        coverage_extra={"compound_limit": _HRT_ITEM_LIMIT},
    )


async def _load_glp1(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    from vitals.services import glp1_service

    phase = await glp1_service.active_dose_phase(
        session, on_date=window.period_end, subject_id=subject_id
    )
    return _LoadedSection(
        value={"drug": phase.drug, "dose_mg": phase.dose_mg} if phase else None,
        row_count=int(phase is not None),
        dates=(phase.start_date,) if phase else (),
    )


async def _load_supplements(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    del window
    from vitals.services import supplements_service

    rows = list(
        await supplements_service.list_supplements(
            session,
            subject_id=subject_id,
            active_only=True,
            limit=_CATALOG_LIMIT + 1,
        )
    )
    truncated = len(rows) > _CATALOG_LIMIT
    rows = rows[:_CATALOG_LIMIT]
    return _LoadedSection(
        value=[{"name": row.name, "dose": row.dose} for row in rows] or None,
        row_count=len(rows),
        truncated=truncated,
        coverage_extra={"catalog_limit": _CATALOG_LIMIT},
    )


async def _load_skincare(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    from vitals.services import skincare_service

    products = list(
        await skincare_service.list_products(
            session,
            subject_id=subject_id,
            active_only=True,
            limit=_CATALOG_LIMIT + 1,
        )
    )
    observations = list(
        await skincare_service.list_observations(
            session,
            subject_id=subject_id,
            start=window.period_start,
            end=window.period_end,
            limit=_SKINCARE_OBSERVATION_LIMIT + 1,
        )
    )
    products_truncated = len(products) > _CATALOG_LIMIT
    observations_truncated = len(observations) > _SKINCARE_OBSERVATION_LIMIT
    products = products[:_CATALOG_LIMIT]
    observations = observations[:_SKINCARE_OBSERVATION_LIMIT]
    return _LoadedSection(
        value={
            "active_products": len(products),
            "recent_observations": [
                {
                    "date": row.date.isoformat(),
                    "inflammation": row.inflammation,
                    "pih": row.pih,
                }
                for row in observations
            ],
        },
        row_count=len(products) + len(observations),
        dates=tuple(row.date for row in observations),
        truncated=products_truncated or observations_truncated,
        coverage_extra={
            "catalog_limit": _CATALOG_LIMIT,
            "observation_limit": _SKINCARE_OBSERVATION_LIMIT,
            "products_truncated": products_truncated,
            "observations_truncated": observations_truncated,
        },
    )


async def _load_genetics(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    del window
    from vitals.services import genetics_service

    page = await genetics_service.bounded_variants(
        session,
        subject_id=subject_id,
        limit=_GENETICS_LIMIT,
    )
    rows = page.rows
    return _LoadedSection(
        value=[{"marker": row.marker, "gene": row.gene} for row in rows] or None,
        row_count=len(rows),
        truncated=page.truncated,
        coverage_extra={"variant_limit": _GENETICS_LIMIT},
    )


async def _load_garmin(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    from vitals.models.garmin import GarminDaily
    from vitals.services import garmin_service

    reported = or_(
        GarminDaily.sleep_score.is_not(None),
        GarminDaily.sleep_seconds.is_not(None),
        GarminDaily.resting_hr.is_not(None),
        GarminDaily.hrv_avg.is_not(None),
        GarminDaily.body_battery_high.is_not(None),
        GarminDaily.avg_stress.is_not(None),
        GarminDaily.steps.is_not(None),
        GarminDaily.active_calories.is_not(None),
    )
    latest = await garmin_service.latest_daily(
        session, subject_id=subject_id, before_or_on=window.period_end
    )
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(GarminDaily)
            .where(
                GarminDaily.subject_id == subject_id,
                GarminDaily.date <= window.period_end,
                reported,
            )
        )
        or 0
    )
    return _LoadedSection(
        value={
            "advice": garmin_service.recovery_advice(latest),
            "total_days_logged": total,
        },
        row_count=total,
        dates=(latest.date,) if latest else (),
    )


async def _load_hevy(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_service.ReportWindow
) -> _LoadedSection:
    from vitals.models.hevy import HevyWorkout

    current_count, latest = (
        await session.execute(
            select(
                func.count().filter(HevyWorkout.date >= window.period_start),
                func.max(HevyWorkout.date),
            ).where(
                HevyWorkout.subject_id == subject_id,
                HevyWorkout.date <= window.period_end,
            )
        )
    ).one()
    count = int(current_count or 0)
    return _LoadedSection(
        value={
            "total_workouts": count,
            "last_workout": latest.isoformat() if latest else None,
        },
        row_count=max(count, int(latest is not None)),
        dates=(latest,) if latest else (),
    )


_LOADERS: Mapping[str, Loader] = {
    "weight": _load_weight,
    "labs": _load_labs,
    "body_comp": _load_body_comp,
    "nutrition": _load_nutrition,
    "hrt": _load_hrt,
    "glp1": _load_glp1,
    "supplements": _load_supplements,
    "skincare": _load_skincare,
    "genetics": _load_genetics,
    "garmin": _load_garmin,
    "hevy": _load_hevy,
}


async def assemble_record_projection(
    session: AsyncSession,
    *,
    context: AccessContext,
    enabled_modules: Mapping[str, bool],
    subject_timezone_name: str,
    on_date: date_type | None = None,
    period_days: int = 7,
) -> RecordProjection:
    """Read only domains allowed by both policy and module configuration."""

    with subject_timezone(subject_timezone_name):
        window = digest_service.report_window(
            on_date=on_date,
            period_days=period_days,
        )
    record: dict[str, Any] = {}
    coverage: dict[str, Mapping[str, Any]] = {}
    withheld: list[str] = []
    loaded_domains: list[str] = []
    restricted = False

    for section in SECTIONS:
        enabled = bool(enabled_modules.get(section.module, False))
        allowed = _may_read(context, section.domain)
        if enabled and not allowed:
            restricted = True
            if context.support_grant is None:
                withheld.append(section.domain.value)
        if not enabled or not allowed:
            continue

        loaded = await _LOADERS[section.key](session, context.subject_id, window)
        record[section.key] = loaded.value
        coverage[section.key] = _coverage(
            section=section, loaded=loaded, window=window
        )
        loaded_domains.append(section.domain.value)

    return RecordProjection(
        record=record,
        coverage=coverage,
        period={
            "report_date": window.report_date.isoformat(),
            "period_days": window.period_days,
            "mode": window.mode,
            "period_start": window.period_start.isoformat(),
            "period_end": window.period_end.isoformat(),
            "previous_start": window.previous_start.isoformat(),
            "previous_end": window.previous_end.isoformat(),
        },
        withheld_domains=tuple(withheld),
        loaded_domains=tuple(loaded_domains),
        restricted=restricted,
    )


__all__ = [
    "CARE_DOMAINS",
    "RecordProjection",
    "SECTIONS",
    "assemble_record_projection",
    "enabled_care_domains",
]
