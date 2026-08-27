
"""Skincare state projected into the cross-domain conflict engine."""
from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.skincare import SkincareLog
from vitals.services.conflicts import engine
from vitals.services.skincare.governance import FLAGS, _day_entity_key


async def legacy_unowned_present(session: AsyncSession) -> bool:
    """Mirror of this module's widening in :func:`resolve_today_scoped`.

    Kept beside it so the two change together: the engine skips its
    sole-subject proof when every probe says no, so a probe that missed a row
    its resolver would still adopt is the one way this goes wrong.
    """

    found = await session.scalar(
        select(SkincareLog.id)
        .where(SkincareLog.subject_id.is_(None),
            SkincareLog.actor_user_id.is_(None),)
        .limit(1)
    )
    return found is not None

async def resolve_today_scoped(
    session: AsyncSession,
    *,
    scope: engine.ConflictScope,
) -> list[dict]:
    """Resolve the selected subject's checklist on its evaluation day.

    The conflict engine still offers a fully-unowned bridge to its callers, and
    a resolver has to honour the scope it is handed. This is the last place in
    the module that can see a row with no subject; it goes when the bridge does.
    """

    subject_scope = SkincareLog.subject_id == scope.subject_id
    if scope.include_legacy_unowned:
        subject_scope = or_(
            subject_scope,
            and_(
                SkincareLog.subject_id.is_(None),
                SkincareLog.actor_user_id.is_(None),
            ),
        )
    rows = list(
        await session.scalars(
            select(SkincareLog)
            .where(
                SkincareLog.date == scope.evaluation_date,
                subject_scope,
            )
            .order_by(SkincareLog.id.desc())
            .limit(2)
        )
    )
    if len(rows) > 1:
        raise engine.ConflictScopeError(
            "multiple skincare logs match one subject and evaluation date"
        )
    if not rows:
        return []
    return [
        {
            engine.CONFLICT_ENTITY_KEY: _day_entity_key(
                scope.evaluation_date
            ),
            **{flag: getattr(rows[0], flag) for flag in FLAGS},
        }
    ]
