"""Nutrition domain service — meal logging with macro tracking.

CRUD over ``MealLog`` (multiple entries per day), plus daily/period summaries
with on-track checks against configurable protein/calorie goals.
"""
from __future__ import annotations

import uuid
from datetime import date as date_type, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import Config
from vitals.enums import Domain, Source
from vitals.models.nutrition import DOMAIN, MealLog
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine
from vitals.utils.timeutils import now_local, today_local


# ── Goals helper ─────────────────────────────────────────────────────────────

def get_goals(cfg: Config) -> dict[str, Any]:
    return {
        "protein_target_g": cfg.nutrition_protein_target_g,
        "calories_min": cfg.nutrition_calories_min,
        "calories_max": cfg.nutrition_calories_max,
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────

async def log_meal(
    session: AsyncSession,
    *,
    on_date: date_type,
    name: str,
    eaten_at=None,
    calories: Optional[float] = None,
    protein_g: Optional[float] = None,
    fat_g: Optional[float] = None,
    carbs_g: Optional[float] = None,
    note: Optional[str] = None,
    source: str = Source.MANUAL.value,
    override: bool = False,
    identity: WriteIdentity | None = None,
) -> MealLog:
    await conflict_engine.enforce(
        session,
        Domain.NUTRITION.value,
        {"name": name, "calories": calories},
        override=override,
        entity_ref=f"meal:{on_date.isoformat()}",
    )
    if eaten_at is None:
        eaten_at = now_local().time()
    row = MealLog(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        date=on_date,
        domain=DOMAIN,
        source=source,
        name=name,
        eaten_at=eaten_at,
        calories=calories,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def update_meal(
    session: AsyncSession,
    meal_id: int,
    *,
    on_date: date_type,
    name: str,
    eaten_at=None,
    calories: Optional[float] = None,
    protein_g: Optional[float] = None,
    fat_g: Optional[float] = None,
    carbs_g: Optional[float] = None,
    note: Optional[str] = None,
    identity: WriteIdentity | None = None,
) -> Optional[MealLog]:
    stmt = select(MealLog).where(MealLog.id == meal_id)
    if identity is not None:
        stmt = stmt.where(MealLog.subject_id == identity.subject_id)
    row = await session.scalar(stmt.with_for_update())
    if row is None:
        return None
    row.date = on_date
    row.name = name
    row.eaten_at = eaten_at
    row.calories = calories
    row.protein_g = protein_g
    row.fat_g = fat_g
    row.carbs_g = carbs_g
    row.note = note
    await session.flush()
    return row


async def delete_meal(
    session: AsyncSession,
    meal_id: int,
    *,
    identity: WriteIdentity | None = None,
) -> bool:
    stmt = select(MealLog).where(MealLog.id == meal_id)
    if identity is not None:
        stmt = stmt.where(MealLog.subject_id == identity.subject_id)
    row = await session.scalar(stmt.with_for_update())
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


# ── Queries ──────────────────────────────────────────────────────────────────

async def list_meals_for_date(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID | None = None,
    include_unowned_legacy: bool = False,
) -> Sequence[MealLog]:
    """List one day's meals inside an exact subject boundary.

    ``include_unowned_legacy`` is a temporary pre-backfill bridge. It may be
    enabled only by a boundary that has independently proved the installation
    still contains exactly one health subject.
    """
    stmt = select(MealLog).where(MealLog.date == on_date)
    if subject_id is not None:
        subject_scope = MealLog.subject_id == subject_id
        if include_unowned_legacy:
            subject_scope = or_(
                subject_scope,
                and_(
                    MealLog.subject_id.is_(None),
                    MealLog.actor_user_id.is_(None),
                ),
            )
        stmt = stmt.where(subject_scope)
    result = await session.execute(
        stmt.order_by(MealLog.eaten_at.asc().nulls_last(), MealLog.id)
    )
    return result.scalars().all()


async def list_meals(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    subject_id: uuid.UUID | None = None,
    include_unowned_legacy: bool = False,
) -> Sequence[MealLog]:
    """List meals, optionally in one exact subject scope.

    The unowned-row bridge has the same sole-subject precondition as
    :func:`list_meals_for_date` and must disappear after ownership backfill.
    """
    stmt = select(MealLog)
    if subject_id is not None:
        subject_scope = MealLog.subject_id == subject_id
        if include_unowned_legacy:
            subject_scope = or_(subject_scope, MealLog.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    if start is not None:
        stmt = stmt.where(MealLog.date >= start)
    if end is not None:
        stmt = stmt.where(MealLog.date <= end)
    stmt = stmt.order_by(MealLog.date.desc(), MealLog.eaten_at.asc().nulls_last(), MealLog.id)
    result = await session.execute(stmt)
    return result.scalars().all()


# ── Summaries ────────────────────────────────────────────────────────────────

def _sum_macros(meals: Sequence[MealLog]) -> dict[str, float]:
    return {
        "calories": sum(m.calories or 0 for m in meals),
        "protein_g": sum(m.protein_g or 0 for m in meals),
        "fat_g": sum(m.fat_g or 0 for m in meals),
        "carbs_g": sum(m.carbs_g or 0 for m in meals),
    }


# Atwater energy factors (kcal per gram of each macronutrient).
_KCAL_PER_G = {"protein_g": 4.0, "fat_g": 9.0, "carbs_g": 4.0}


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


# ── Conflict-engine resolver ──────────────────────────────────────────────────

async def resolve_today(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_unowned_legacy: bool = False,
) -> list[dict]:
    """Conflict-engine resolver: today's macro totals as a single match item —
    lets a rule reference e.g. {"calories": {"$gt": 4000}} against the running
    daily total, not just the one meal being logged right now."""
    meals = await list_meals_for_date(
        session,
        today_local(),
        subject_id=subject_id,
        include_unowned_legacy=include_unowned_legacy,
    )
    return [_sum_macros(meals)]


async def resolve_today_scoped(
    session: AsyncSession,
    *,
    scope: conflict_engine.ConflictScope,
) -> list[dict]:
    """Conflict resolver for one subject and one subject-local calendar day."""

    meals = await list_meals_for_date(
        session,
        scope.evaluation_date,
        subject_id=scope.subject_id,
        include_unowned_legacy=scope.include_legacy_unowned,
    )
    return [_sum_macros(meals)]


# ── Scheduler job ────────────────────────────────────────────────────────────

async def day_end_job(session_factory, redis=None) -> None:
    """Once-daily check (registered in vitals/scheduler/jobs.py, 23:00 local) for
    nutrition rules that need a *complete* day's totals — e.g. the very-low-
    calorie/protein GLP-1 warnings, which would false-positive off a partial
    running total if evaluated live on every meal save (see log_meal's
    ``enforce`` call, which never passes ``include_day_end``). By 23:00 the
    day's logged totals are effectively final.

    Uses ``enforce_day_end`` (not plain ``enforce``) so a rule that stops
    matching on a later, better day also gets its alert cleared automatically
    — not just raised."""
    async with session_factory() as session:
        await conflict_engine.enforce_day_end(
            session,
            Domain.NUTRITION.value,
            entity_ref=f"meal:{today_local().isoformat()}",
        )
        await session.commit()


def _on_track(totals: dict[str, float], goals: dict[str, Any]) -> dict[str, bool]:
    cal = totals["calories"]
    return {
        "calories": goals["calories_min"] <= cal <= goals["calories_max"],
        "protein": totals["protein_g"] >= goals["protein_target_g"],
    }


async def daily_summary(
    session: AsyncSession,
    on_date: date_type,
    cfg: Config,
    *,
    subject_id: uuid.UUID | None = None,
    include_unowned_legacy: bool = False,
) -> dict[str, Any]:
    meals = await list_meals_for_date(
        session,
        on_date,
        subject_id=subject_id,
        include_unowned_legacy=include_unowned_legacy,
    )
    totals = _sum_macros(meals)
    goals = get_goals(cfg)
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
    cfg: Config,
    *,
    subject_id: uuid.UUID | None = None,
    include_unowned_legacy: bool = False,
) -> dict[str, Any]:
    meals = await list_meals(
        session,
        start=start,
        end=end,
        subject_id=subject_id,
        include_unowned_legacy=include_unowned_legacy,
    )
    totals = _sum_macros(meals)
    goals = get_goals(cfg)

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
