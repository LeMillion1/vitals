"""Supplement regimen projected into the cross-domain conflict engine."""

from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.supplements import Supplement
from vitals.services.conflicts import engine
from vitals.services.supplements.parsing import _parse_slot


async def legacy_unowned_present(session: AsyncSession) -> bool:
    """Mirror of this module's widening in :func:`resolve_active_scoped`.

    Kept beside it so the two change together: the engine skips its
    sole-subject proof when every probe says no, so a probe that missed a row
    its resolver would still adopt is the one way this goes wrong.
    """

    found = await session.scalar(
        select(Supplement.id)
        .where(
            Supplement.subject_id.is_(None),
            Supplement.actor_user_id.is_(None),
        )
        .limit(1)
    )
    return found is not None


async def resolve_active_scoped(
    session: AsyncSession,
    *,
    scope: engine.ConflictScope,
) -> list[dict]:
    """Conflict resolver restricted to one explicit subject boundary.

    The conflict engine still offers a fully-unowned bridge to its callers, and
    a resolver has to honour the scope it is handed. This is the last place in
    the module that can see a row with no subject; it goes when the bridge does.
    """

    subject_scope = Supplement.subject_id == scope.subject_id
    if scope.include_legacy_unowned:
        subject_scope = or_(
            subject_scope,
            and_(
                Supplement.subject_id.is_(None),
                Supplement.actor_user_id.is_(None),
            ),
        )
    rows = await session.scalars(select(Supplement).where(subject_scope))
    return [
        {
            engine.CONFLICT_ENTITY_KEY: str(row.id),
            "key": row.key,
            "active": row.active,
            "name": row.name,
            "timing_slot": _parse_slot(row.timing),
        }
        for row in rows
    ]
