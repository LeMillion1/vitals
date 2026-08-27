
"""Subject-scoped reads and conflict projections for the GLP-1 domain."""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import t
from vitals.models.glp1 import DOMAIN, DosePhase, Injection, SideEffect
from vitals.services.conflicts import engine
from vitals.utils.timeutils import today_local

_ACTIVE_ENTITY_PREFIX = "glp1-active"


def _active_entity_key(on_date: date_type) -> str:
    return f"{_ACTIVE_ENTITY_PREFIX}:{on_date.isoformat()}"

def _subject_scope(model, subject_id: uuid.UUID):
    return model.subject_id == subject_id

async def list_injections(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
    has_note: bool = False,
    limit: int | None = None,
) -> Sequence[Injection]:
    stmt = select(Injection)
    stmt = stmt.where(_subject_scope(Injection, subject_id))
    if start is not None:
        stmt = stmt.where(Injection.date >= start)
    if end is not None:
        stmt = stmt.where(Injection.date <= end)
    if has_note:
        stmt = stmt.where(Injection.note.is_not(None), Injection.note != "")
    stmt = stmt.order_by(Injection.date.desc(), Injection.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()

async def last_injection(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> Optional[Injection]:
    rows = await list_injections(
        session,
        subject_id=subject_id,
        limit=1,
    )
    return rows[0] if rows else None

def site_frequency(injections: Sequence[Injection]) -> dict[str, int]:
    """How many times each body-map site has been used — feeds the rotation
    mini-map (I1) so the owner can see at a glance which sites are overdue for
    reuse. Pure function over already-fetched rows, no extra query."""
    counts: dict[str, int] = {}
    for inj in injections:
        if inj.site:
            counts[inj.site] = counts.get(inj.site, 0) + 1
    return counts

async def list_dose_phases(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> Sequence[DosePhase]:
    stmt = select(DosePhase).where(DosePhase.domain == DOMAIN)
    stmt = stmt.where(_subject_scope(DosePhase, subject_id))
    result = await session.execute(
        stmt.order_by(DosePhase.start_date, DosePhase.id)
    )
    return result.scalars().all()

async def active_dose_phase(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    subject_id: uuid.UUID,
) -> Optional[DosePhase]:
    """The phase covering ``on_date`` (today by default): start <= date and
    (end is null or date <= end). The newest matching phase wins."""
    day = on_date or today_local()
    return await session.scalar(
        select(DosePhase)
        .where(
            DosePhase.domain == DOMAIN,
            _subject_scope(DosePhase, subject_id),
            DosePhase.start_date <= day,
            or_(DosePhase.end_date.is_(None), DosePhase.end_date >= day),
        )
        .order_by(DosePhase.start_date.desc(), DosePhase.id.desc())
        .limit(1)
    )

async def legacy_unowned_present(session: AsyncSession) -> bool:
    """Mirror of this module's widening in :func:`resolve_active_scoped`.

    Kept beside it so the two change together: the engine skips its
    sole-subject proof when every probe says no, so a probe that missed a row
    its resolver would still adopt is the one way this goes wrong.
    """

    found = await session.scalar(
        select(DosePhase.id)
        .where(DosePhase.subject_id.is_(None),
            DosePhase.actor_user_id.is_(None),)
        .limit(1)
    )
    return found is not None

async def resolve_active_scoped(
    session: AsyncSession,
    *,
    scope: engine.ConflictScope,
) -> list[dict]:
    """Resolve the current dose phase inside one subject boundary."""

    subject_scope = DosePhase.subject_id == scope.subject_id
    if scope.include_legacy_unowned:
        subject_scope = or_(
            subject_scope,
            and_(
                DosePhase.subject_id.is_(None),
                DosePhase.actor_user_id.is_(None),
            ),
        )
    phase = await session.scalar(
        select(DosePhase)
        .where(
            DosePhase.domain == DOMAIN,
            DosePhase.start_date <= scope.evaluation_date,
            or_(
                DosePhase.end_date.is_(None),
                DosePhase.end_date >= scope.evaluation_date,
            ),
            subject_scope,
        )
        .order_by(DosePhase.start_date.desc(), DosePhase.id.desc())
        .limit(1)
    )
    if phase is None:
        return []
    return [
        {
            engine.CONFLICT_ENTITY_KEY: _active_entity_key(
                scope.evaluation_date
            ),
            "drug": phase.drug,
            "dose_mg": phase.dose_mg,
            "active": True,
        }
    ]

async def dose_phase_overlays(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> list[dict]:
    """Phases shaped for the weight chart's GLP-1 colour overlay."""
    phases = await list_dose_phases(
        session,
        subject_id=subject_id,
    )
    return [
        {
            "start": p.start_date.isoformat(),
            "end": p.end_date.isoformat() if p.end_date else None,
            "drug": p.drug,
            "dose_mg": p.dose_mg,
            "label": f"{p.drug} {p.dose_mg:g} {t('common.mg')}",
        }
        for p in phases
    ]

async def list_side_effects(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
    limit: int | None = None,
) -> Sequence[SideEffect]:
    stmt = select(SideEffect)
    stmt = stmt.where(_subject_scope(SideEffect, subject_id))
    if start is not None:
        stmt = stmt.where(SideEffect.date >= start)
    if end is not None:
        stmt = stmt.where(SideEffect.date <= end)
    stmt = stmt.order_by(SideEffect.date.desc(), SideEffect.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()
