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


_DAY_ENTITY_PREFIX = "nutrition-day"


def _day_entity_key(on_date: date_type) -> str:
    return f"{_DAY_ENTITY_PREFIX}:{on_date.isoformat()}"


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: conflict_engine.PreparedConflictWrite,
) -> conflict_engine.ConflictWriteContext:
    """Prove the write names a subject and the decision that authorized it."""

    if identity is None or prepared is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped nutrition writes require identity and a prepared conflict write"
        )
    return conflict_engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


def _require_evaluation_date(
    context: conflict_engine.ConflictWriteContext,
    on_date: date_type,
) -> None:
    if context.evaluation_date != on_date:
        raise conflict_engine.ConflictPreparedWriteError(
            "nutrition write date does not match prepared conflict evaluation date"
        )


def _meal_subject_scope(subject_id: uuid.UUID):
    return MealLog.subject_id == subject_id


def _meal_by_id_stmt(meal_id: int, *, subject_id: uuid.UUID):
    return select(MealLog).where(
        MealLog.id == meal_id,
        _meal_subject_scope(subject_id),
    )


async def _get_meal_for_update(
    session: AsyncSession,
    meal_id: int,
    *,
    subject_id: uuid.UUID,
) -> Optional[MealLog]:
    return await session.scalar(
        _meal_by_id_stmt(meal_id, subject_id=subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_meal_for_update(
    session: AsyncSession,
    meal_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[MealLog]:
    """Lock and refresh a scoped meal for a caller-side partial merge."""

    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    return await _get_meal_for_update(
        session,
        meal_id,
        subject_id=identity.subject_id,
    )


def _meal_item(
    *,
    name: str,
    calories: Optional[float],
) -> dict[str, Any]:
    # Keep the legacy per-meal predicate shape. Daily macro predicates belong
    # to the projected aggregate below; exposing one meal's protein/fat/carbs
    # would make a low-daily-total rule fire on an ordinary small meal.
    return {
        "name": name,
        "calories": calories,
    }


def _projected_day_total(
    meals: Sequence[MealLog],
    *,
    replaced: MealLog | None,
    proposed: dict[str, Any],
) -> dict[str, float]:
    totals = _sum_macros(meals)
    if replaced is not None:
        for field in ("calories", "protein_g", "fat_g", "carbs_g"):
            totals[field] -= getattr(replaced, field) or 0
    for field in ("calories", "protein_g", "fat_g", "carbs_g"):
        totals[field] += proposed.get(field) or 0
    return totals


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
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> MealLog:
    scoped_context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    proposed = _meal_item(
        name=name,
        calories=calories,
    )
    proposed_macros = {
        "calories": calories,
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carbs_g,
    }
    _require_evaluation_date(scoped_context, on_date)
    meals = await list_meals_for_date(
        session,
        on_date,
        subject_id=identity.subject_id,
    )
    projected_total = _projected_day_total(
        meals,
        replaced=None,
        proposed=proposed_macros,
    )
    await conflict_engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.NUTRITION,
        proposed_state=[proposed, projected_total],
        override=override,
        entity_ref=f"meal:{on_date.isoformat()}",
        replace_entity_key=_day_entity_key(on_date),
    )
    if eaten_at is None:
        eaten_at = now_local().time()
    row = MealLog(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
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
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[MealLog]:
    scoped_context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(scoped_context, on_date)
    row = await _get_meal_for_update(
        session,
        meal_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    proposed = _meal_item(
        name=name,
        calories=calories,
    )
    proposed_macros = {
        "calories": calories,
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carbs_g,
    }
    meals = await list_meals_for_date(
        session,
        on_date,
        subject_id=identity.subject_id,
    )
    projected_total = _projected_day_total(
        meals,
        replaced=row if row.date == on_date else None,
        proposed=proposed_macros,
    )
    await conflict_engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.NUTRITION,
        proposed_state=[proposed, projected_total],
        override=override,
        entity_ref=f"meal:{on_date.isoformat()}",
        replace_entity_key=_day_entity_key(on_date),
    )
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
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_meal_for_update(
        session,
        meal_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def update_meal_note(
    session: AsyncSession,
    meal_id: int,
    *,
    note: str,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[MealLog]:
    """Update only a meal note under the same subject lock as meal CRUD."""

    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_meal_for_update(
        session,
        meal_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    if row.subject_id is None:
        row.subject_id = identity.subject_id
    row.note = note
    await session.flush()
    return row


# ── Queries ──────────────────────────────────────────────────────────────────

async def list_meals_for_date(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> Sequence[MealLog]:
    """List one day's meals inside an exact subject boundary."""
    stmt = select(MealLog).where(MealLog.date == on_date)
    stmt = stmt.where(_meal_subject_scope(subject_id))
    result = await session.execute(
        stmt.order_by(MealLog.eaten_at.asc().nulls_last(), MealLog.id)
        .execution_options(populate_existing=True)
    )
    return result.scalars().all()


async def list_meals(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    subject_id: uuid.UUID,
    name_query: str | None = None,
    has_note: bool = False,
    limit: int | None = None,
) -> Sequence[MealLog]:
    """List meals inside one exact subject scope."""
    stmt = select(MealLog)
    stmt = stmt.where(_meal_subject_scope(subject_id))
    if start is not None:
        stmt = stmt.where(MealLog.date >= start)
    if end is not None:
        stmt = stmt.where(MealLog.date <= end)
    if name_query:
        stmt = stmt.where(MealLog.name.ilike(f"%{name_query}%"))
    if has_note:
        stmt = stmt.where(MealLog.note.isnot(None), MealLog.note != "")
    stmt = stmt.order_by(
        MealLog.date.desc(),
        MealLog.eaten_at.asc().nulls_last(),
        MealLog.id,
    )
    if limit is not None:
        stmt = stmt.limit(limit)
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

async def legacy_unowned_present(session: AsyncSession) -> bool:
    """Mirror of this module's widening in :func:`resolve_today_scoped`.

    Kept beside it so the two change together: the engine skips its
    sole-subject proof when every probe says no, so a probe that missed a row
    its resolver would still adopt is the one way this goes wrong.
    """

    found = await session.scalar(
        select(MealLog.id)
        .where(MealLog.subject_id.is_(None),
            MealLog.actor_user_id.is_(None),)
        .limit(1)
    )
    return found is not None


async def resolve_today_scoped(
    session: AsyncSession,
    *,
    scope: conflict_engine.ConflictScope,
) -> list[dict]:
    """Conflict resolver for one subject and one subject-local calendar day.

    The conflict engine still offers a fully-unowned bridge to its callers, and
    a resolver has to honour the scope it is handed. This is the last place in
    the module that can see a row with no subject; it goes when the bridge does.
    """

    subject_scope = MealLog.subject_id == scope.subject_id
    if scope.include_legacy_unowned:
        subject_scope = or_(
            subject_scope,
            and_(
                MealLog.subject_id.is_(None),
                MealLog.actor_user_id.is_(None),
            ),
        )
    meals = list(
        await session.scalars(
            select(MealLog).where(
                MealLog.date == scope.evaluation_date,
                subject_scope,
            )
        )
    )
    return [
        {
            conflict_engine.CONFLICT_ENTITY_KEY: _day_entity_key(
                scope.evaluation_date
            ),
            **_sum_macros(meals),
        }
    ]


# ── Scheduler job ────────────────────────────────────────────────────────────

async def day_end_job(
    session_factory, redis=None, *, subject_id: uuid.UUID
) -> None:
    """Once-daily check (registered in vitals/scheduler/jobs.py, 23:00 local) for
    nutrition rules that need a *complete* day's totals — e.g. the very-low-
    calorie/protein GLP-1 warnings, which would false-positive off a partial
    running total if evaluated live on every meal save (see log_meal's
    ``enforce`` call, which never passes ``include_day_end``). By 23:00 the
    day's logged totals are effectively final.

    Uses scoped day-end reconciliation (not live ``enforce``) so a rule that
    stops matching on a later, better day also gets its alert cleared
    automatically — not just raised."""
    async with session_factory() as session:
        on_date = today_local()
        context = await conflict_engine.resolve_subject_conflict_write_context(
            session,
            subject_id=subject_id,
            evaluation_date=on_date,
        )
        await conflict_engine.reconcile_day_end_scoped(
            session,
            context=context,
            domain=Domain.NUTRITION,
            entity_ref=f"meal:{on_date.isoformat()}",
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
    subject_id: uuid.UUID,
) -> dict[str, Any]:
    meals = await list_meals_for_date(
        session,
        on_date,
        subject_id=subject_id,
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
    subject_id: uuid.UUID,
) -> dict[str, Any]:
    meals = await list_meals(
        session,
        start=start,
        end=end,
        subject_id=subject_id,
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
