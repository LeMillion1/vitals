"""Symmetric digest window statistics."""

from __future__ import annotations

from datetime import date as date_type
from typing import Any, Optional

from vitals.services.digest.projection.formatting import _nutrition_day_totals


def _mean(values) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 1) if present else None


_GARMIN_NUMERIC_STATS = (
    ("sleep_score", "sleep_score", 1.0),
    ("sleep_hours", "sleep_seconds", 1 / 3600),
    ("deep_sleep_hours", "deep_sleep_seconds", 1 / 3600),
    ("light_sleep_hours", "light_sleep_seconds", 1 / 3600),
    ("rem_sleep_hours", "rem_sleep_seconds", 1 / 3600),
    ("awake_hours", "awake_seconds", 1 / 3600),
    ("awake_count", "awake_count", 1.0),
    ("restless_moments", "restless_moments", 1.0),
    ("avg_sleep_stress", "avg_sleep_stress", 1.0),
    ("avg_sleep_hr", "avg_sleep_hr", 1.0),
    ("respiration_lowest", "respiration_lowest", 1.0),
    ("respiration_highest", "respiration_highest", 1.0),
    ("sleep_need_hours", "sleep_need_actual", 1 / 60),
    ("resting_hr", "resting_hr", 1.0),
    ("avg_hr", "avg_hr", 1.0),
    ("max_hr", "max_hr", 1.0),
    ("min_hr", "min_hr", 1.0),
    ("hrv_avg", "hrv_avg", 1.0),
    ("avg_respiration", "avg_respiration", 1.0),
    ("spo2_avg", "spo2_avg", 1.0),
    ("spo2_lowest", "spo2_lowest", 1.0),
    ("avg_stress", "avg_stress", 1.0),
    ("max_stress", "max_stress", 1.0),
    ("body_battery_high", "body_battery_high", 1.0),
    ("body_battery_low", "body_battery_low", 1.0),
    ("body_battery_change", "body_battery_change", 1.0),
    ("steps", "steps", 1.0),
    ("floors_climbed", "floors_climbed", 1.0),
    ("active_calories", "active_calories", 1.0),
    ("bmr_calories", "bmr_calories", 1.0),
    ("total_calories", "total_calories", 1.0),
    ("intensity_minutes_moderate", "intensity_minutes_moderate", 1.0),
    ("intensity_minutes_vigorous", "intensity_minutes_vigorous", 1.0),
    ("training_readiness", "training_readiness", 1.0),
    ("vo2max", "vo2max", 1.0),
    ("acute_load", "acute_load", 1.0),
    ("load_ratio", "load_ratio", 1.0),
)

_GARMIN_STAT_COLS = tuple(attr for _key, attr, _scale in _GARMIN_NUMERIC_STATS)


def _window_stats(start, end, garmin_rows, weights, meals, sessions, garmin_activities=()) -> dict:
    """One window reduced to the numbers worth comparing against another window.

    Symmetric on purpose: the model gets two identical shapes to subtract, rather
    than this period's rows plus an invitation to recall the last one — which is
    where a narrative starts supplying the half it doesn't have.

    Every count carries the denominator it should be read against — how many days
    actually carry numbers, not how many dates the window spans.
    """
    g = [r for r in garmin_rows if start <= r.date <= end]
    w = [x for x in weights if start <= x.date <= end]
    m = [x for x in meals if start <= x.date <= end]
    s = [x for x in sessions if start.isoformat() <= x["date"] <= end.isoformat()]
    a = [x for x in garmin_activities if start <= x.date <= end]
    meals_by_date: dict[date_type, list[Any]] = {}
    for meal in m:
        meals_by_date.setdefault(meal.date, []).append(meal)
    nutrition_days = [_nutrition_day_totals(day_meals) for day_meals in meals_by_date.values()]
    logged_days = len(nutrition_days)
    days = (end - start).days + 1
    garmin_means = {
        key: _mean(getattr(row, attr) * scale for row in g if getattr(row, attr) is not None)
        for key, attr, scale in _GARMIN_NUMERIC_STATS
    }
    sample_counts = {
        key: sum(getattr(row, attr) is not None for row in g)
        for key, attr, _scale in _GARMIN_NUMERIC_STATS
    }
    sample_counts.update(
        {
            "weight_kg": len(w),
            "volume_per_session_kg": sum(row["volume_kg"] is not None for row in s),
            "calories_per_day": sum(row["calories"] is not None for row in nutrition_days),
            "protein_per_day_g": sum(row["protein_g"] is not None for row in nutrition_days),
            "fat_per_day_g": sum(row["fat_g"] is not None for row in nutrition_days),
            "carbs_per_day_g": sum(row["carbs_g"] is not None for row in nutrition_days),
            "garmin_activity_duration_min": sum(row.duration_seconds is not None for row in a),
            "garmin_activity_distance_km": sum(row.distance_m is not None for row in a),
            "garmin_aerobic_effect": sum(row.training_effect_aerobic is not None for row in a),
            "garmin_anaerobic_effect": sum(row.training_effect_anaerobic is not None for row in a),
        }
    )
    latest_training_status = next(
        (row.training_status for row in reversed(g) if row.training_status), None
    )
    latest_hrv_status = next((row.hrv_status for row in reversed(g) if row.hrv_status), None)
    out = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        **garmin_means,
        "training_status_latest": latest_training_status,
        "hrv_status_latest": latest_hrv_status,
        # Days with numbers on them, not rows. A row is written for a date before
        # the watch has scored anything for it, so counting rows reported seven
        # Garmin days behind means that were computed from six.
        "garmin_days": sum(
            1 for r in g if any(getattr(r, c) is not None for c in _GARMIN_STAT_COLS)
        ),
        "weight_kg": _mean(x.weight_kg for x in w),
        "workouts": len(s),
        "garmin_activities": len(a),
        "garmin_activity_duration_min": (
            round(sum(row.duration_seconds or 0 for row in a) / 60, 1) or None
        ),
        "garmin_activity_distance_km": (
            round(sum(row.distance_m or 0 for row in a) / 1000, 2) or None
        ),
        "garmin_aerobic_effect": _mean(row.training_effect_aerobic for row in a),
        "garmin_anaerobic_effect": _mean(row.training_effect_anaerobic for row in a),
        # Tonnage per session, not per window. Summed over a window it inherits the
        # window's arbitrariness exactly as the count does: one session against two
        # reads as "volume down 51%" when both sessions were the same size. Per
        # session the number is a fact about training; summed it is a fact about
        # where the window edge fell. The sum is kept, one rung below.
        "volume_per_session_kg": _mean(x["volume_kg"] for x in s),
        "training_volume_kg": sum(x["volume_kg"] or 0 for x in s) or None,
        "calories_per_day": _mean(row["calories"] for row in nutrition_days),
        "protein_per_day_g": _mean(row["protein_g"] for row in nutrition_days),
        "fat_per_day_g": _mean(row["fat_g"] for row in nutrition_days),
        "carbs_per_day_g": _mean(row["carbs_g"] for row in nutrition_days),
        "nutrition_days_logged": logged_days,
        "sample_counts": sample_counts,
    }
    return out
