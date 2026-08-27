
"""Prepared, subject-scoped Skincare mutations."""
from __future__ import annotations

from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Source
from vitals.models.skincare import DOMAIN, SkincareLog, SkincareObservation, SkincareProduct
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.skincare.governance import (
    _day_entity_key,
    _get_log,
    _get_owned_row_for_update,
    _require_evaluation_date,
    _require_scoped_prepared_write,
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    await engine.enforce_prepared(
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

async def delete_log(
    session: AsyncSession,
    log_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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

async def delete_observation(
    session: AsyncSession,
    observation_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
