
"""Prepared, subject-scoped writes for injections, dose phases and effects."""
from __future__ import annotations

import uuid
from datetime import date as date_type, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, InjectionSite, Source
from vitals.models.glp1 import DOMAIN, DosePhase, Injection, SideEffect
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine

_INJECTION_SITES = frozenset(site.value for site in InjectionSite)
_ACTIVE_ENTITY_PREFIX = "glp1-active"


def _active_entity_key(on_date: date_type) -> str:
    return f"{_ACTIVE_ENTITY_PREFIX}:{on_date.isoformat()}"

def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> engine.ConflictWriteContext:
    if identity is None or prepared is None:
        raise engine.ConflictPreparedWriteError(
            "scoped GLP-1 writes require identity and a prepared conflict write"
        )
    return engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )

def _require_evaluation_date(
    context: engine.ConflictWriteContext,
    on_date: date_type,
) -> None:
    if context.evaluation_date != on_date:
        raise engine.ConflictPreparedWriteError(
            "GLP-1 write date does not match prepared conflict evaluation date"
        )

def _subject_scope(model, subject_id: uuid.UUID):
    return model.subject_id == subject_id

async def _owned_row_for_update(
    session: AsyncSession,
    model,
    row_id: int,
    *,
    subject_id: uuid.UUID,
):
    stmt = select(model).where(
        model.id == row_id,
        _subject_scope(model, subject_id),
    )
    return await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )

def _validate_injection(
    *, drug: str, dose_mg: float, site: Optional[str]
) -> tuple[str, Optional[str]]:
    """Sanitise write-path inputs before they touch the DB. The GLP-1 write tools
    are reachable from MCP (an LLM), which bypasses the HTML form entirely — so a
    hallucinated ``dose_mg=-5`` or a garbage ``site`` must be rejected here, not
    left to surface as a raw DB IntegrityError or, worse, to land in the data lake.

    ``drug`` stays free-text (real GLP-1 agonists are broader than the two-value
    enum) but must be non-empty; ``site`` must be a known ``InjectionSite`` or null.
    Returns the cleaned ``(drug, site)``.
    """
    clean_drug = (drug or "").strip()
    if not clean_drug:
        raise ValueError("drug is required")
    if dose_mg is None or dose_mg <= 0:
        raise ValueError("dose_mg must be a positive number")
    clean_site = (site or "").strip() or None
    if clean_site is not None and clean_site not in _INJECTION_SITES:
        raise ValueError(f"unknown injection site: {site!r}")
    return clean_drug, clean_site

async def log_injection(
    session: AsyncSession,
    *,
    on_date: date_type,
    drug: str,
    dose_mg: float,
    site: Optional[str] = None,
    note: Optional[str] = None,
    source: str = Source.MANUAL.value,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Injection:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    drug, site = _validate_injection(drug=drug, dose_mg=dose_mg, site=site)
    proposed = {"drug": drug, "dose_mg": dose_mg}
    await engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.GLP1,
        proposed_state=proposed,
        override=override,
        entity_ref=f"injection:{on_date.isoformat()}",
    )
    row = Injection(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=on_date,
        domain=DOMAIN,
        source=source,
        drug=drug,
        dose_mg=dose_mg,
        site=site,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row

async def get_injection_for_update(
    session: AsyncSession,
    injection_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Injection]:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    return await _owned_row_for_update(
        session,
        Injection,
        injection_id,
        subject_id=identity.subject_id,
    )

async def update_injection(
    session: AsyncSession,
    injection_id: int,
    *,
    on_date: date_type,
    drug: str,
    dose_mg: float,
    site: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Injection]:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    row = await _owned_row_for_update(
        session,
        Injection,
        injection_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    drug, site = _validate_injection(drug=drug, dose_mg=dose_mg, site=site)
    # Run the same conflict-engine gate as log_injection so editing a shot can't
    # slip past a cross-domain block that a fresh log would have caught.
    proposed = {"drug": drug, "dose_mg": dose_mg}
    await engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.GLP1,
        proposed_state=proposed,
        override=override,
        entity_ref=f"injection:{on_date.isoformat()}",
    )
    row.date = on_date
    row.drug = drug
    row.dose_mg = dose_mg
    row.site = site
    row.note = note
    await session.flush()
    return row

async def update_injection_note(
    session: AsyncSession,
    injection_id: int,
    *,
    note: str,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Injection]:
    row = await get_injection_for_update(
        session,
        injection_id,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    if row is None:
        return None
    if row.subject_id is None:
        row.subject_id = identity.subject_id
    row.note = note
    await session.flush()
    return row

async def delete_injection(
    session: AsyncSession,
    injection_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _owned_row_for_update(
        session,
        Injection,
        injection_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True

async def add_dose_phase(
    session: AsyncSession,
    *,
    start_date: date_type,
    drug: str,
    dose_mg: float,
    end_date: Optional[date_type] = None,
    note: Optional[str] = None,
    source: str = Source.MANUAL.value,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> DosePhase:
    """Add a dose phase while keeping open-ended phases chronologically bounded.

    A newer phase closes older open phases at the preceding boundary; a repeated
    same-day phase turns the previous row into a one-day historical interval. A
    back-dated phase is capped before the earliest newer open phase, so commit
    order cannot leave two current doses.
    """
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, start_date)
    drug, _site = _validate_injection(drug=drug, dose_mg=dose_mg, site=None)
    if end_date is not None and end_date < start_date:
        raise ValueError("end_date must not be before start_date")

    open_stmt = select(DosePhase).where(
        DosePhase.domain == DOMAIN,
        DosePhase.end_date.is_(None),
    )
    open_stmt = (
        open_stmt.where(_subject_scope(DosePhase, identity.subject_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    open_phases = list(
        await session.scalars(open_stmt.order_by(DosePhase.start_date, DosePhase.id))
    )

    proposed = {"drug": drug, "dose_mg": dose_mg, "active": True}
    await engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.GLP1,
        proposed_state=proposed,
        override=override,
        entity_ref=f"dose_phase:{start_date.isoformat()}",
        replace_entity_key=_active_entity_key(start_date),
    )

    phase_end_date = end_date
    if end_date is None:
        # A back-dated phase may race with, or simply be entered after, a newer
        # open phase.  In that ordering there is nothing older to close, so cap
        # the new phase immediately before the earliest newer one.  The subject
        # root lock held by the prepared write makes this projection stable on
        # PostgreSQL and leaves exactly the newest phase open regardless of
        # commit order.
        newer_starts = [
            open_phase.start_date
            for open_phase in open_phases
            if open_phase.start_date > start_date
        ]
        if newer_starts:
            phase_end_date = min(newer_starts) - timedelta(days=1)
        for open_phase in open_phases:
            if open_phase.start_date <= start_date:
                open_phase.end_date = (
                    start_date
                    if open_phase.start_date == start_date
                    else start_date - timedelta(days=1)
                )

    phase = DosePhase(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=DOMAIN,
        source=source,
        start_date=start_date,
        end_date=phase_end_date,
        drug=drug,
        dose_mg=dose_mg,
        note=note,
    )
    session.add(phase)
    await session.flush()
    return phase

async def delete_dose_phase(
    session: AsyncSession,
    phase_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _owned_row_for_update(
        session,
        DosePhase,
        phase_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True

async def log_side_effect(
    session: AsyncSession,
    *,
    on_date: date_type,
    effect_type: str,
    severity: int,
    note: Optional[str] = None,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> SideEffect:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    row = SideEffect(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=on_date,
        domain=DOMAIN,
        source=source,
        effect_type=effect_type,
        severity=severity,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row

async def delete_side_effect(
    session: AsyncSession,
    effect_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _owned_row_for_update(
        session,
        SideEffect,
        effect_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
