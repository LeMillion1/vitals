
"""Prepared, subject-scoped Nutrition mutations."""
from __future__ import annotations

from datetime import date as date_type
from typing import Any, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Source
from vitals.models.nutrition import DOMAIN, MealLog
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.nutrition.analytics import _sum_macros
from vitals.services.nutrition.governance import (
    _day_entity_key,
    _get_meal_for_update,
    _require_evaluation_date,
    _require_scoped_prepared_write,
)
from vitals.services.nutrition.queries import list_meals_for_date
from vitals.utils.timeutils import now_local


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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    await engine.enforce_prepared(
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    await engine.enforce_prepared(
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
