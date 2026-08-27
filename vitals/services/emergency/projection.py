"""Eleven reviewed, column-minimal emergency record summaries.

Every loader is declared explicitly.  None selects an ORM entity, dereferences
raw/file provenance, or touches professional notes, plans or conversations.
Adding a normal care section therefore cannot expand break-glass access.
"""

from __future__ import annotations

from vitals.services.digest import window as digest_window

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
from vitals.enums import Domain
from vitals.services.garmin import advice as garmin_advice
from vitals.utils.timeutils import subject_timezone

_WEIGHT_HISTORY_LIMIT = 400
_WEIGHT_NOISE_LIMIT = 100
_LAB_MARKER_LIMIT = 200
_CATALOG_LIMIT = 100
_SKINCARE_OBSERVATION_LIMIT = 200
_GENETICS_LIMIT = 100
_HRT_ITEM_LIMIT = 50


@dataclass(frozen=True, slots=True)
class EmergencySection:
    key: str
    domain: Domain
    module: str


SECTIONS: tuple[EmergencySection, ...] = (
    EmergencySection("weight", Domain.WEIGHT, "weight"),
    EmergencySection("labs", Domain.LABS, "labs"),
    EmergencySection("body_comp", Domain.BODY_COMPOSITION, "body_comp"),
    EmergencySection("nutrition", Domain.NUTRITION, "nutrition"),
    EmergencySection("hrt", Domain.HRT, "hrt"),
    EmergencySection("glp1", Domain.GLP1, "glp1"),
    EmergencySection("supplements", Domain.SUPPLEMENTS, "supplements"),
    EmergencySection("skincare", Domain.SKINCARE, "skincare"),
    EmergencySection("genetics", Domain.GENETICS, "genetics"),
    EmergencySection("garmin", Domain.GARMIN, "garmin"),
    EmergencySection("hevy", Domain.WORKOUTS, "hevy"),
)


@dataclass(frozen=True, slots=True)
class EmergencyProjection:
    record: Mapping[str, Any]
    coverage: Mapping[str, Mapping[str, Any]]
    period: Mapping[str, Any]
    loaded_domains: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LoadedSection:
    value: Any
    row_count: int
    dates: tuple[date_type, ...] = ()
    truncated: bool = False
    current_rows: int | None = None
    coverage_extra: Mapping[str, Any] | None = None


Loader = Callable[
    [AsyncSession, uuid.UUID, digest_window.ReportWindow], Awaitable[_LoadedSection]
]


def _mean(values: Sequence[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return round(sum(present) / len(present), 1) if present else None


def _coverage(
    *,
    section: EmergencySection,
    loaded: _LoadedSection,
    window: digest_window.ReportWindow,
) -> dict[str, Any]:
    latest = max(loaded.dates) if loaded.dates else None
    value = {
        "module": section.module,
        "enabled": True,
        "status": "available" if loaded.row_count else "empty",
        "rows": loaded.row_count,
        "current_rows": (
            loaded.current_rows
            if loaded.current_rows is not None
            else sum(
                window.period_start <= day <= window.period_end
                for day in loaded.dates
            )
        ),
        "previous_rows": sum(
            window.previous_start <= day <= window.previous_end
            for day in loaded.dates
        ),
        "first_date": min(loaded.dates).isoformat() if loaded.dates else None,
        "last_date": latest.isoformat() if latest else None,
        "freshness_days": (window.period_end - latest).days if latest else None,
        "truncated": loaded.truncated,
    }
    if loaded.coverage_extra:
        value.update(loaded.coverage_extra)
    return value


async def _weight(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    from vitals.services.weight.queries import emergency_weight_history

    history = await emergency_weight_history(
        session,
        subject_id=subject_id,
        end=window.period_end,
        history_limit=_WEIGHT_HISTORY_LIMIT,
        noise_limit=_WEIGHT_NOISE_LIMIT,
    )
    rows = list(history.rows)
    markers = history.noise_markers
    noise_truncated = history.noise_truncated
    points = [(row.date, row.weight_kg) for row in rows]
    ranges = [(marker.start_date, marker.end_date) for marker in markers]
    clean = [] if noise_truncated else exclude_ranges(points, ranges)
    moving_average = rolling_mean_by_date(clean, window_days=7)
    trend = None if noise_truncated else fit_trend(points, exclude=ranges)
    latest = rows[-1] if rows else None
    latest_ma = moving_average[-1] if moving_average else None
    return _LoadedSection(
        value={
            "latest_kg": latest.weight_kg if latest else None,
            "latest_date": latest.date.isoformat() if latest else None,
            "ma7_kg": latest_ma[1] if latest_ma else None,
            "trend_kg_per_week": round(trend.slope_per_week, 3) if trend else None,
        },
        row_count=len(rows),
        dates=tuple(row.date for row in rows),
        truncated=history.history_truncated or noise_truncated,
        coverage_extra={
            "history_limit": _WEIGHT_HISTORY_LIMIT,
            "noise_limit": _WEIGHT_NOISE_LIMIT,
            "noise_truncated": noise_truncated,
        },
    )


async def _labs(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    from vitals.services.labs.results import emergency_latest_results_by_marker

    page = await emergency_latest_results_by_marker(
        session,
        subject_id=subject_id,
        end=window.period_end,
        marker_limit=_LAB_MARKER_LIMIT,
    )
    rows = page.rows
    out_of_range = {"low", "high", "critical_low", "critical_high"}
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
        if row.flag in out_of_range
        and 0 <= (window.period_end - row.date).days <= 14
    ]
    return _LoadedSection(
        value={"out_of_range": flagged},
        row_count=len(rows),
        dates=tuple(row.date for row in rows),
        truncated=page.truncated,
        coverage_extra={"marker_limit": _LAB_MARKER_LIMIT},
    )


async def _body_comp(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    from vitals.models.body_scan import BodyScan

    row = (
        await session.execute(
            select(BodyScan.date, BodyScan.device)
            .where(
                BodyScan.subject_id == subject_id,
                BodyScan.domain == Domain.BODY_COMPOSITION.value,
                BodyScan.date <= window.period_end,
            )
            .order_by(BodyScan.date.desc(), BodyScan.id.desc())
            .limit(1)
        )
    ).first()
    return _LoadedSection(
        value={"date": row.date.isoformat(), "device": row.device} if row else None,
        row_count=int(row is not None),
        dates=(row.date,) if row else (),
    )


async def _nutrition(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
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
    meal_count = sum(int(row[1]) for row in daily)
    calorie_samples = sum(int(row[4]) for row in daily)
    protein_samples = sum(int(row[5]) for row in daily)
    return _LoadedSection(
        value={
            "avg_calories_per_day": _mean([row[2] for row in daily]),
            "avg_protein_per_day_g": _mean([row[3] for row in daily]),
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


async def _hrt(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    from vitals.models.hrt import HrtCycle, HrtCycleItem

    cycle = (
        await session.execute(
            select(HrtCycle.id, HrtCycle.name, HrtCycle.start_date)
            .where(
                HrtCycle.subject_id == subject_id,
                HrtCycle.domain == Domain.HRT.value,
                HrtCycle.start_date <= window.period_end,
                or_(HrtCycle.end_date.is_(None), HrtCycle.end_date >= window.period_end),
            )
            .order_by(HrtCycle.start_date.desc(), HrtCycle.id.desc())
            .limit(1)
        )
    ).first()
    compounds: list[str] = []
    truncated = False
    if cycle is not None:
        candidates = list(
            await session.scalars(
                select(HrtCycleItem.compound_key)
                .where(
                    HrtCycleItem.subject_id == subject_id,
                    HrtCycleItem.cycle_id == cycle.id,
                )
                .order_by(HrtCycleItem.id)
                .limit(_HRT_ITEM_LIMIT + 1)
            )
        )
        truncated = len(candidates) > _HRT_ITEM_LIMIT
        compounds = candidates[:_HRT_ITEM_LIMIT]
    return _LoadedSection(
        value=(
            {
                "cycle": {
                    "name": cycle.name,
                    "start_date": cycle.start_date.isoformat(),
                    "compounds": compounds,
                }
            }
            if cycle
            else None
        ),
        row_count=int(cycle is not None),
        dates=(cycle.start_date,) if cycle else (),
        truncated=truncated,
        coverage_extra={"compound_limit": _HRT_ITEM_LIMIT},
    )


async def _glp1(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    from vitals.models.glp1 import DosePhase

    phase = (
        await session.execute(
            select(DosePhase.drug, DosePhase.dose_mg, DosePhase.start_date)
            .where(
                DosePhase.subject_id == subject_id,
                DosePhase.domain == Domain.GLP1.value,
                DosePhase.start_date <= window.period_end,
                or_(DosePhase.end_date.is_(None), DosePhase.end_date >= window.period_end),
            )
            .order_by(DosePhase.start_date.desc(), DosePhase.id.desc())
            .limit(1)
        )
    ).first()
    return _LoadedSection(
        value={"drug": phase.drug, "dose_mg": phase.dose_mg} if phase else None,
        row_count=int(phase is not None),
        dates=(phase.start_date,) if phase else (),
    )


async def _supplements(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    del window
    from vitals.models.supplements import Supplement

    candidates = (
        await session.execute(
            select(Supplement.name, Supplement.dose)
            .where(Supplement.subject_id == subject_id, Supplement.active.is_(True))
            .order_by(func.lower(Supplement.name), Supplement.id)
            .limit(_CATALOG_LIMIT + 1)
        )
    ).all()
    rows = candidates[:_CATALOG_LIMIT]
    return _LoadedSection(
        value=[{"name": row.name, "dose": row.dose} for row in rows] or None,
        row_count=len(rows),
        truncated=len(candidates) > _CATALOG_LIMIT,
        coverage_extra={"catalog_limit": _CATALOG_LIMIT},
    )


async def _skincare(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    from vitals.models.skincare import SkincareObservation, SkincareProduct

    product_count = int(
        await session.scalar(
            select(func.count(SkincareProduct.id)).where(
                SkincareProduct.subject_id == subject_id,
                SkincareProduct.active.is_(True),
            )
        )
        or 0
    )
    candidates = (
        await session.execute(
            select(
                SkincareObservation.date,
                SkincareObservation.inflammation,
                SkincareObservation.pih,
            )
            .where(
                SkincareObservation.subject_id == subject_id,
                SkincareObservation.date >= window.period_start,
                SkincareObservation.date <= window.period_end,
            )
            .order_by(SkincareObservation.date.desc(), SkincareObservation.id.desc())
            .limit(_SKINCARE_OBSERVATION_LIMIT + 1)
        )
    ).all()
    observations = candidates[:_SKINCARE_OBSERVATION_LIMIT]
    return _LoadedSection(
        value={
            "active_products": product_count,
            "recent_observations": [
                {
                    "date": row.date.isoformat(),
                    "inflammation": row.inflammation,
                    "pih": row.pih,
                }
                for row in observations
            ],
        },
        row_count=product_count + len(observations),
        dates=tuple(row.date for row in observations),
        truncated=len(candidates) > _SKINCARE_OBSERVATION_LIMIT,
        coverage_extra={"observation_limit": _SKINCARE_OBSERVATION_LIMIT},
    )


async def _genetics(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    del window
    from vitals.models.genetics import GeneticVariant

    candidates = (
        await session.execute(
            select(GeneticVariant.marker, GeneticVariant.gene)
            .where(GeneticVariant.subject_id == subject_id)
            .order_by(
                func.lower(GeneticVariant.gene),
                GeneticVariant.rsid,
                GeneticVariant.id,
            )
            .limit(_GENETICS_LIMIT + 1)
        )
    ).all()
    rows = candidates[:_GENETICS_LIMIT]
    return _LoadedSection(
        value=[{"marker": row.marker, "gene": row.gene} for row in rows] or None,
        row_count=len(rows),
        truncated=len(candidates) > _GENETICS_LIMIT,
        coverage_extra={"variant_limit": _GENETICS_LIMIT},
    )


async def _garmin(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    from vitals.services.garmin import queries as garmin_queries

    summary = await garmin_queries.recovery_summary(
        session,
        subject_id=subject_id,
        before_or_on=window.period_end,
    )
    return _LoadedSection(
        value={
            "advice": garmin_advice.recovery_advice(summary.latest),
            "total_days_logged": summary.total_days_logged,
        },
        row_count=summary.total_days_logged,
        dates=(summary.latest.date,) if summary.latest else (),
    )


async def _hevy(
    session: AsyncSession, subject_id: uuid.UUID, window: digest_window.ReportWindow
) -> _LoadedSection:
    from vitals.services.hevy import queries as hevy_queries

    summary = await hevy_queries.workout_window_summary(
        session,
        subject_id=subject_id,
        start=window.period_start,
        end=window.period_end,
    )
    return _LoadedSection(
        value={
            "total_workouts": summary.current_count,
            "last_workout": (
                summary.latest_date.isoformat() if summary.latest_date else None
            ),
        },
        row_count=max(summary.current_count, int(summary.latest_date is not None)),
        dates=(summary.latest_date,) if summary.latest_date else (),
    )


_LOADERS: Mapping[str, Loader] = {
    "weight": _weight,
    "labs": _labs,
    "body_comp": _body_comp,
    "nutrition": _nutrition,
    "hrt": _hrt,
    "glp1": _glp1,
    "supplements": _supplements,
    "skincare": _skincare,
    "genetics": _genetics,
    "garmin": _garmin,
    "hevy": _hevy,
}


async def assemble_record_projection(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    allowed_domain_keys: Sequence[str],
    enabled_modules: Mapping[str, bool],
    subject_timezone_name: str,
    on_date: date_type | None = None,
    period_days: int = 7,
) -> EmergencyProjection:
    """Query only the exact, reviewed emergency domains presented by access."""

    allowed = frozenset(allowed_domain_keys)
    supported = frozenset(section.domain.value for section in SECTIONS)
    if not allowed or allowed - supported or len(allowed) != len(allowed_domain_keys):
        raise ValueError("emergency projection requires unique reviewed domains")
    with subject_timezone(subject_timezone_name):
        window = digest_window.report_window(
            on_date=on_date,
            period_days=period_days,
        )
    record: dict[str, Any] = {}
    coverage: dict[str, Mapping[str, Any]] = {}
    loaded_domains: list[str] = []
    for section in SECTIONS:
        if section.domain.value not in allowed:
            continue
        if not bool(enabled_modules.get(section.module, False)):
            continue
        loaded = await _LOADERS[section.key](session, subject_id, window)
        record[section.key] = loaded.value
        coverage[section.key] = _coverage(
            section=section,
            loaded=loaded,
            window=window,
        )
        loaded_domains.append(section.domain.value)
    return EmergencyProjection(
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
        loaded_domains=tuple(loaded_domains),
    )


__all__ = ["EmergencyProjection", "SECTIONS", "assemble_record_projection"]
