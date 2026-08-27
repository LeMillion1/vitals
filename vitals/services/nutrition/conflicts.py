
"""Nutrition state projected into the cross-domain conflict engine."""
from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.nutrition import MealLog
from vitals.services.conflicts import engine
from vitals.services.nutrition.analytics import _sum_macros
from vitals.services.nutrition.governance import _day_entity_key


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
    scope: engine.ConflictScope,
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
            engine.CONFLICT_ENTITY_KEY: _day_entity_key(
                scope.evaluation_date
            ),
            **_sum_macros(meals),
        }
    ]
