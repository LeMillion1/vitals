"""Skincare service (Phase 3).

The evening checklist upsert is the conflict-engine hot path: its boolean flags
*are* the proposed state, so "retinoid + peel same evening" (a same-domain rule)
and "active isotretinoin → no peel" (supplements ↔ skincare) both evaluate off
the checklist being saved. :func:`resolve_today` exposes today's checklist to
rules triggered from *other* domains (e.g. activating isotretinoin today).
"""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Source
from vitals.models.skincare import (
    DOMAIN,
    SkincareLog,
    SkincareObservation,
    SkincareProduct,
)
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine

_FLAGS = (
    "retinoid", "azelaic", "peel", "niacinamide_spf", "moisturizer",
    "vitamin_c", "benzoyl_peroxide",
)
_DAY_ENTITY_PREFIX = "skincare-day"


def _day_entity_key(on_date: date_type) -> str:
    return f"{_DAY_ENTITY_PREFIX}:{on_date.isoformat()}"


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: conflict_engine.PreparedConflictWrite,
) -> conflict_engine.ConflictWriteContext:
    """Bind one skincare write to its subject and its conflict decision."""

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
            "skincare write date does not match prepared conflict evaluation date"
        )


def _subject_scope(model, subject_id: uuid.UUID):
    """A routine and the skin it is written about belong to one person."""

    return model.subject_id == subject_id


# ── Checklist log ─────────────────────────────────────────────────────────────
async def _get_log(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
    for_update: bool,
) -> Optional[SkincareLog]:
    stmt = (
        select(SkincareLog)
        .where(SkincareLog.date == on_date)
        .where(_subject_scope(SkincareLog, subject_id))
    )
    stmt = stmt.order_by(SkincareLog.id).limit(2)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    rows = list(await session.scalars(stmt))
    if len(rows) > 1:
        raise conflict_engine.ConflictScopeError(
            "multiple skincare logs match one subject and date"
        )
    return rows[0] if rows else None


async def get_log(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> Optional[SkincareLog]:
    return await _get_log(
        session,
        on_date,
        subject_id=subject_id,
        for_update=False,
    )


async def upsert_log(
    session: AsyncSession,
    *,
    on_date: date_type,
    retinoid: bool = False,
    azelaic: bool = False,
    peel: bool = False,
    niacinamide_spf: bool = False,
    moisturizer: bool = False,
    vitamin_c: bool = False,
    benzoyl_peroxide: bool = False,
    note: Optional[str] = None,
    source: str = Source.MANUAL.value,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> SkincareLog:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    proposed = {
        "retinoid": retinoid,
        "azelaic": azelaic,
        "peel": peel,
        "niacinamide_spf": niacinamide_spf,
        "moisturizer": moisturizer,
        "vitamin_c": vitamin_c,
        "benzoyl_peroxide": benzoyl_peroxide,
    }
    row = await _get_log(
        session,
        on_date,
        subject_id=identity.subject_id,
        for_update=True,
    )
    await conflict_engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.SKINCARE,
        proposed_state=proposed,
        override=override,
        entity_ref=f"skincare:{on_date.isoformat()}",
        replace_entity_key=_day_entity_key(on_date),
    )
    if row is None:
        row = SkincareLog(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            date=on_date,
            domain=DOMAIN,
            source=source,
        )
        session.add(row)
    row.retinoid = retinoid
    row.azelaic = azelaic
    row.peel = peel
    row.niacinamide_spf = niacinamide_spf
    row.moisturizer = moisturizer
    row.vitamin_c = vitamin_c
    row.benzoyl_peroxide = benzoyl_peroxide
    if note is not None:
        row.note = note
    await session.flush()
    return row


async def list_logs(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
    has_note: bool = False,
    limit: int | None = None,
) -> Sequence[SkincareLog]:
    stmt = select(SkincareLog).where(_subject_scope(SkincareLog, subject_id))
    if start is not None:
        stmt = stmt.where(SkincareLog.date >= start)
    if end is not None:
        stmt = stmt.where(SkincareLog.date <= end)
    if has_note:
        stmt = stmt.where(SkincareLog.note.is_not(None), SkincareLog.note != "")
    stmt = stmt.order_by(SkincareLog.date.desc(), SkincareLog.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def _get_owned_row_for_update(
    session: AsyncSession,
    model,
    row_id: int,
    *,
    subject_id: uuid.UUID,
):
    stmt = (
        select(model)
        .where(model.id == row_id)
        .where(_subject_scope(model, subject_id))
    )
    return await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )


async def delete_log(
    session: AsyncSession,
    log_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_owned_row_for_update(
        session,
        SkincareLog,
        log_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def update_log_note(
    session: AsyncSession,
    log_id: int,
    *,
    note: str,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[SkincareLog]:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_owned_row_for_update(
        session,
        SkincareLog,
        log_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    row.note = note
    await session.flush()
    return row


# ── Observations ──────────────────────────────────────────────────────────────
async def add_observation(
    session: AsyncSession,
    *,
    on_date: date_type,
    inflammation: Optional[int] = None,
    pih: Optional[int] = None,
    zone: Optional[str] = None,
    note: Optional[str] = None,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> SkincareObservation:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    row = SkincareObservation(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=on_date,
        domain=DOMAIN,
        source=source,
        inflammation=inflammation,
        pih=pih,
        zone=zone,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def list_observations(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
    limit: int | None = None,
) -> Sequence[SkincareObservation]:
    stmt = select(SkincareObservation).where(_subject_scope(SkincareObservation, subject_id))
    if start is not None:
        stmt = stmt.where(SkincareObservation.date >= start)
    if end is not None:
        stmt = stmt.where(SkincareObservation.date <= end)
    stmt = stmt.order_by(
        SkincareObservation.date.desc(),
        SkincareObservation.id.desc(),
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def delete_observation(
    session: AsyncSession,
    observation_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_owned_row_for_update(
        session,
        SkincareObservation,
        observation_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


# ── Conflict-engine resolver ──────────────────────────────────────────────────
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
    scope: conflict_engine.ConflictScope,
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
        raise conflict_engine.ConflictScopeError(
            "multiple skincare logs match one subject and evaluation date"
        )
    if not rows:
        return []
    return [
        {
            conflict_engine.CONFLICT_ENTITY_KEY: _day_entity_key(
                scope.evaluation_date
            ),
            **{flag: getattr(rows[0], flag) for flag in _FLAGS},
        }
    ]


# ── Skincare Products CRUD ───────────────────────────────────────────────────
async def list_products(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    active_only: bool = False,
    limit: int | None = None,
) -> Sequence[SkincareProduct]:
    stmt = select(SkincareProduct).where(_subject_scope(SkincareProduct, subject_id))
    if active_only:
        stmt = stmt.where(SkincareProduct.active.is_(True))
    stmt = stmt.order_by(SkincareProduct.active.desc(), SkincareProduct.name)
    if limit is not None:
        if limit < 1:
            raise ValueError("skincare product limit must be positive")
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def add_product(
    session: AsyncSession,
    *,
    name: str,
    type: str,
    active_ingredient: Optional[str] = None,
    description: Optional[str] = None,
    usage_instructions: Optional[str] = None,
    default_time: str = "evening",
    schedule_days: Sequence[int] = (),
    active: bool = True,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> SkincareProduct:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = SkincareProduct(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        name=name,
        type=type,
        active_ingredient=active_ingredient,
        description=description,
        usage_instructions=usage_instructions,
        default_time=default_time,
        schedule_days=list(schedule_days),
        active=active,
    )
    session.add(row)
    await session.flush()
    return row


async def update_product(
    session: AsyncSession,
    product_id: int,
    *,
    name: str,
    type: str,
    active_ingredient: Optional[str] = None,
    description: Optional[str] = None,
    usage_instructions: Optional[str] = None,
    default_time: str = "evening",
    schedule_days: Sequence[int] = (),
    active: bool = True,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[SkincareProduct]:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_owned_row_for_update(
        session,
        SkincareProduct,
        product_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    if row.subject_id is None and identity is not None:
        row.subject_id = identity.subject_id
    row.name = name
    row.type = type
    row.active_ingredient = active_ingredient
    row.description = description
    row.usage_instructions = usage_instructions
    row.default_time = default_time
    row.schedule_days = list(schedule_days)
    row.active = active
    await session.flush()
    return row


async def delete_product(
    session: AsyncSession,
    product_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_owned_row_for_update(
        session,
        SkincareProduct,
        product_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
