"""Bounded query and row-formatting helpers for digest projections."""

from __future__ import annotations

import uuid
from typing import Any, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.profile import health as health_profile_service
from vitals.services.digest.window import ReportWindow, _period_name


async def _bounded_scalars(session: AsyncSession, stmt, limit: int) -> tuple[list[Any], bool]:
    """Execute ``limit + 1`` so every output cap is observable in coverage."""
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    return rows[:limit], len(rows) > limit


_GARMIN_DAILY_FIELDS = (
    "sleep_seconds",
    "sleep_score",
    "deep_sleep_seconds",
    "light_sleep_seconds",
    "rem_sleep_seconds",
    "awake_seconds",
    "awake_count",
    "restless_moments",
    "avg_sleep_stress",
    "avg_sleep_hr",
    "spo2_lowest",
    "respiration_lowest",
    "respiration_highest",
    "body_battery_change",
    "breathing_disruption",
    "sleep_need_actual",
    "resting_hr",
    "avg_hr",
    "max_hr",
    "min_hr",
    "hrv_avg",
    "hrv_status",
    "avg_respiration",
    "spo2_avg",
    "avg_stress",
    "max_stress",
    "body_battery_high",
    "body_battery_low",
    "steps",
    "floors_climbed",
    "active_calories",
    "bmr_calories",
    "total_calories",
    "intensity_minutes_moderate",
    "intensity_minutes_vigorous",
    "training_readiness",
    "vo2max",
    "training_status",
    "acute_load",
    "load_ratio",
)


def _garmin_daily_row(row) -> dict[str, Any]:
    out = {"date": row.date.isoformat(), "source": row.source}
    out.update({key: getattr(row, key) for key in _GARMIN_DAILY_FIELDS})
    out["sleep_start"] = row.sleep_start.isoformat() if row.sleep_start else None
    out["sleep_end"] = row.sleep_end.isoformat() if row.sleep_end else None
    out["sleep_hours"] = (
        round(row.sleep_seconds / 3600, 2) if row.sleep_seconds is not None else None
    )
    return out


def _garmin_activity_row(row) -> dict[str, Any]:
    return {
        "date": row.date.isoformat(),
        "external_id": row.external_id,
        "activity_type": row.activity_type,
        "name": row.name,
        "start_time": row.start_time.isoformat() if row.start_time else None,
        "duration_min": (
            round(row.duration_seconds / 60, 1) if row.duration_seconds is not None else None
        ),
        "distance_km": (round(row.distance_m / 1000, 3) if row.distance_m is not None else None),
        "calories": row.calories,
        "avg_hr": row.avg_hr,
        "max_hr": row.max_hr,
        "elevation_gain_m": row.elevation_gain_m,
        "avg_power": row.avg_power,
        "training_effect_aerobic": row.training_effect_aerobic,
        "training_effect_anaerobic": row.training_effect_anaerobic,
        "hr_zone_seconds": row.hr_zone_seconds,
        "source": row.source,
    }


_SKINCARE_FLAGS = (
    "retinoid",
    "azelaic",
    "peel",
    "niacinamide_spf",
    "moisturizer",
    "vitamin_c",
    "benzoyl_peroxide",
)


def _skincare_log_row(row, window: ReportWindow) -> dict[str, Any]:
    return {
        "date": row.date.isoformat(),
        "period": _period_name(row.date, window),
        "applied": [key for key in _SKINCARE_FLAGS if getattr(row, key)],
        "note": row.note,
        "source": row.source,
    }


_NUTRITION_FIELDS = (
    ("calories", "calories"),
    ("protein_g", "protein_g"),
    ("fat_g", "fat_g"),
    ("carbs_g", "carbs_g"),
)


def _nutrition_day_totals(meals: Sequence[Any]) -> dict[str, Optional[float]]:
    """Sum only recorded nutrients; an unfilled macro is not a measured zero."""
    out: dict[str, Optional[float]] = {}
    for key, attr in _NUTRITION_FIELDS:
        values = [getattr(meal, attr) for meal in meals if getattr(meal, attr) is not None]
        out[key] = round(sum(values), 1) if values else None
    return out


async def _subject_profile(session: AsyncSession, *, subject_id: uuid.UUID) -> dict[str, Any]:
    """The age, sex, height, programme and goals of *this* person.

    These five used to come from ``.env``, which names nobody: one set for the
    whole process, put into every patient's weekly digest, doctor's report and
    share link as though it were theirs. They were omitted outright for a while,
    which cost the owner five fields and was a placeholder rather than an
    answer. They are subject-scoped state now, and a subject who has not filled
    them in gets nulls — the same shape, meaning "not said" rather than
    "somebody else's".
    """

    profile = await health_profile_service.get_profile(session, subject_id=subject_id)
    return profile.as_report_profile()
