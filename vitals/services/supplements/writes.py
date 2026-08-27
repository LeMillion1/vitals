"""Prepared, subject-scoped Supplement catalog mutations."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Source
from vitals.models.supplements import DOMAIN, Supplement
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.supplements.governance import (
    _get_supplement_for_update,
    _proposed,
    _require_scoped_prepared_write,
)
from vitals.services.supplements.parsing import _parse_slot
from vitals.services.supplements.queries import get_supplement


async def add_supplement(
    session: AsyncSession,
    *,
    name: str,
    key: Optional[str] = None,
    dose: Optional[str] = None,
    timing: Optional[str] = None,
    evidence: Optional[str] = None,
    active: bool = True,
    contraindications: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Supplement:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if key:
        resolved_key = key
    else:
        # Keep catalog/YAML loading out of paths that supply an explicit key.
        from vitals.services.conflicts import catalog

        resolved_key = catalog.normalize_ingredient(name)
    proposed = _proposed(resolved_key, active, _parse_slot(timing))
    await engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.SUPPLEMENTS,
        proposed_state=proposed,
        override=override,
        entity_ref=f"supplement:{resolved_key}",
    )
    row = Supplement(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=DOMAIN,
        source=source,
        name=name,
        key=resolved_key,
        dose=dose,
        timing=timing,
        evidence=evidence,
        active=active,
        contraindications=contraindications,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def update_supplement(
    session: AsyncSession,
    supplement_id: int,
    *,
    name: str,
    key: Optional[str] = None,
    dose: Optional[str] = None,
    timing: Optional[str] = None,
    evidence: Optional[str] = None,
    active: bool = True,
    contraindications: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Supplement]:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_supplement_for_update(
        session,
        supplement_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    if key:
        resolved_key = key
    else:
        # Keep catalog/YAML loading out of paths that supply an explicit key.
        from vitals.services.conflicts import catalog

        resolved_key = catalog.normalize_ingredient(name)
    proposed = _proposed(resolved_key, active, _parse_slot(timing))
    await engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.SUPPLEMENTS,
        proposed_state=proposed,
        override=override,
        entity_ref=f"supplement:{resolved_key}",
        replace_entity_key=str(row.id),
    )
    row.name = name
    row.key = resolved_key
    row.dose = dose
    row.timing = timing
    row.evidence = evidence
    row.active = active
    row.contraindications = contraindications
    row.note = note
    await session.flush()
    return row


async def set_active(
    session: AsyncSession,
    supplement_id: int,
    active: bool,
    *,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Supplement]:
    """Toggle a catalog row's active flag — runs the conflict check so activating
    a contraindicated supplement surfaces the block/override flow."""
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_supplement_for_update(
        session,
        supplement_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    if active:
        proposed = _proposed(row.key, True, _parse_slot(row.timing))
        await engine.enforce_prepared(
            session,
            prepared=prepared_conflict_write,
            domain=Domain.SUPPLEMENTS,
            proposed_state=proposed,
            override=override,
            entity_ref=f"supplement:{row.key}",
            replace_entity_key=str(row.id),
        )
    row.active = active
    await session.flush()
    return row


async def delete_supplement(
    session: AsyncSession,
    supplement_id: int,
    *,
    identity: WriteIdentity,
) -> bool:
    row = await get_supplement(
        session,
        supplement_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
