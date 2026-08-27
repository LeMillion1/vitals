
"""Nutrition goals, macro calculations and period summaries."""
from __future__ import annotations

import uuid
from datetime import date as date_type, timedelta
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.nutrition import MealLog
from vitals.services import health_profile_service
from vitals.services.nutrition.queries import list_meals, list_meals_for_date

_KCAL_PER_G = {"protein_g": 4.0, "fat_g": 9.0, "carbs_g": 4.0}


async def get_goals(
    session: AsyncSession, *, subject_id: uuid.UUID
) -> dict[str, Any]:
    """This person's targets, not the installation's.

    They came from ``.env``, which meant every patient's progress bar and every
    "you are short on protein" line was measured against one person's numbers.
    Unlike the body geometry beside them these keep a default when nobody has
    said: a target is a goal rather than a fact about a body, and a sane
    starting one is honest where inventing a height is not.
    """

    profile = await health_profile_service.get_profile(
        session, subject_id=subject_id
    )
    return profile.as_nutrition_goals()

def _sum_macros(meals: Sequence[MealLog]) -> dict[str, float]:
    return {
        "calories": sum(m.calories or 0 for m in meals),
        "protein_g": sum(m.protein_g or 0 for m in meals),
        "fat_g": sum(m.fat_g or 0 for m in meals),
        "carbs_g": sum(m.carbs_g or 0 for m in meals),
    }

def macro_energy_shares(totals: dict[str, float]) -> dict[str, float]:
    """Share (%) of macro-derived energy from protein/fat/carbs.

    Drives the intake card's composition bar. Uses Atwater factors so the split
    reflects *calories* from each macro, not raw grams (fat reads far heavier
    per gram). Returns zeros when there's no macro data, so the bar collapses
    cleanly on an empty day rather than dividing by zero.
    """
    energy = {k: (totals.get(k) or 0) * f for k, f in _KCAL_PER_G.items()}
    total = sum(energy.values())
    if total <= 0:
        return {"protein": 0.0, "fat": 0.0, "carbs": 0.0}
    return {
        "protein": round(energy["protein_g"] / total * 100, 1),
        "fat": round(energy["fat_g"] / total * 100, 1),
        "carbs": round(energy["carbs_g"] / total * 100, 1),
    }

def _on_track(totals: dict[str, float], goals: dict[str, Any]) -> dict[str, bool]:
    cal = totals["calories"]
    return {
        "calories": goals["calories_min"] <= cal <= goals["calories_max"],
        "protein": totals["protein_g"] >= goals["protein_target_g"],
    }

async def daily_summary(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> dict[str, Any]:
    meals = await list_meals_for_date(
        session,
        on_date,
        subject_id=subject_id,
    )
    totals = _sum_macros(meals)
    goals = await get_goals(session, subject_id=subject_id)
    return {
        "date": on_date.isoformat(),
        "totals": totals,
        "meal_count": len(meals),
        "goals": goals,
        "on_track": _on_track(totals, goals),
    }

async def nutrition_summary(
    session: AsyncSession,
    start: date_type,
    end: date_type,
    *,
    subject_id: uuid.UUID,
) -> dict[str, Any]:
    meals = await list_meals(
        session,
        start=start,
        end=end,
        subject_id=subject_id,
    )
    totals = _sum_macros(meals)
    goals = await get_goals(session, subject_id=subject_id)

    per_day: dict[date_type, list[MealLog]] = {}
    for m in meals:
        per_day.setdefault(m.date, []).append(m)

    daily = []
    d = start
    while d <= end:
        day_meals = per_day.get(d, [])
        day_totals = _sum_macros(day_meals)
        daily.append({
            "date": d.isoformat(),
            "meal_count": len(day_meals),
            **day_totals,
        })
        d += timedelta(days=1)

    days_with_logs = sum(1 for dm in daily if dm["meal_count"] > 0)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "totals": totals,
        "meal_count": len(meals),
        "days_with_logs": days_with_logs,
        "per_day": daily,
        "goals": goals,
        "on_track": _on_track(totals, goals) if days_with_logs == 1 else None,
    }
