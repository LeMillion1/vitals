"""GLP-1 Protocol service (Phase 2).

Owns the GLP-1 domain:

  * **Injections** — CRUD over the shot log (date, drug, dose, body-map site).
  * **Dose phases** — date ranges of "on dose X" that paint the weight chart
    overlay and bound the plateau check. Adding a new open-ended phase closes the
    previous open one the day before (the timeline has no gaps/overlaps).
  * **Side effects** — symptom log graded 1-5.
  * **Plateau detection** — once the current dose has run ``PLATEAU_MIN_DAYS``,
    if the noise-excluded weight trend over the phase is flatter than
    ``PLATEAU_SLOPE_THRESHOLD`` we raise a passive ``warn`` alert (no
    auto-escalation — the product is a navigator, the dose decision is the user's).

Mutating fns run the conflict-engine override plumbing so the override UX stays
wired end-to-end, consistent with the weight service.
"""
from __future__ import annotations

import uuid
from datetime import date as date_type, timedelta
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, InjectionSite, Severity, Source
from vitals.i18n import t
from vitals.models.glp1 import DOMAIN, DosePhase, Injection, SideEffect
from vitals.ownership import WriteIdentity
from vitals.services import (
    alerts_service,
    conflict_engine,
    modules_service,
    weight_service,
)
from vitals.services.analytics.regression import fit_trend
from vitals.utils.timeutils import today_local

PLATEAU_ALERT_KEY = "glp1.plateau"

# A dose must have run at least this long before a plateau call is meaningful
# (early water-weight swings on a new dose aren't a plateau).
PLATEAU_MIN_DAYS = 14
# Weekly slope (kg/week) at or above which the trend counts as stalled. The
# trend is computed over the current phase with noise ranges excluded; a value
# of -0.1 means "losing less than 100 g/week" is treated as a plateau.
PLATEAU_SLOPE_THRESHOLD = -0.1

_INJECTION_SITES = frozenset(s.value for s in InjectionSite)
_ACTIVE_ENTITY_PREFIX = "glp1-active"


def _active_entity_key(on_date: date_type) -> str:
    return f"{_ACTIVE_ENTITY_PREFIX}:{on_date.isoformat()}"


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: conflict_engine.PreparedConflictWrite,
) -> conflict_engine.ConflictWriteContext:
    if identity is None or prepared is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped GLP-1 writes require identity and a prepared conflict write"
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


# ── Injections ────────────────────────────────────────────────────────────────
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Injection:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    drug, site = _validate_injection(drug=drug, dose_mg=dose_mg, site=site)
    proposed = {"drug": drug, "dose_mg": dose_mg}
    await conflict_engine.enforce_prepared(
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


async def get_injection_for_update(
    session: AsyncSession,
    injection_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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


def site_frequency(injections: Sequence[Injection]) -> dict[str, int]:
    """How many times each body-map site has been used — feeds the rotation
    mini-map (I1) so the owner can see at a glance which sites are overdue for
    reuse. Pure function over already-fetched rows, no extra query."""
    counts: dict[str, int] = {}
    for inj in injections:
        if inj.site:
            counts[inj.site] = counts.get(inj.site, 0) + 1
    return counts


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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
    await conflict_engine.enforce_prepared(
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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


# ── Dose phases ───────────────────────────────────────────────────────────────
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
    phases = await list_dose_phases(
        session,
        subject_id=subject_id,
    )
    match: Optional[DosePhase] = None
    for p in phases:
        if p.start_date <= day and (p.end_date is None or day <= p.end_date):
            if match is None or p.start_date >= match.start_date:
                match = p
    return match


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
    scope: conflict_engine.ConflictScope,
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
            conflict_engine.CONFLICT_ENTITY_KEY: _active_entity_key(
                scope.evaluation_date
            ),
            "drug": phase.drug,
            "dose_mg": phase.dose_mg,
            "active": True,
        }
    ]


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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
    await conflict_engine.enforce_prepared(
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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


# ── Side effects ──────────────────────────────────────────────────────────────
async def log_side_effect(
    session: AsyncSession,
    *,
    on_date: date_type,
    effect_type: str,
    severity: int,
    note: Optional[str] = None,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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


async def delete_side_effect(
    session: AsyncSession,
    effect_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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


# ── Plateau detection ─────────────────────────────────────────────────────────
async def evaluate_plateau(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    on_date: Optional[date_type] = None,
    scope: conflict_engine.ConflictScope | None = None,
) -> Optional[dict]:
    """Pure read: is the current dose plateaued? Returns a context dict
    (drug, dose, days_on_dose, slope_per_week) when a plateau is detected on the
    current phase, else ``None``. Writes nothing.

    A plateau is a fact about one person's dose and one person's weight trend.
    ``scope`` carries that subject on the conflict path; a composition caller
    that has no conflict decision passes ``subject_id`` directly.
    """

    if scope is not None and scope.subject_id != subject_id:
        raise conflict_engine.ConflictPreparedWriteError(
            "plateau subject does not match the prepared conflict scope"
        )
    today = scope.evaluation_date if scope is not None else (on_date or today_local())
    if on_date is not None and on_date != today:
        raise conflict_engine.ConflictPreparedWriteError(
            "plateau date does not match the prepared conflict scope"
        )
    phase = await active_dose_phase(
        session,
        on_date=today,
        subject_id=subject_id,
    )
    if phase is None:
        return None

    days_on_dose = (today - phase.start_date).days
    if days_on_dose < PLATEAU_MIN_DAYS:
        return None

    weights = await weight_service.list_active_weights(
        session,
        start=phase.start_date,
        end=today,
        subject_id=subject_id,
    )
    points = [(w.date, w.weight_kg) for w in weights]
    ranges = await weight_service._noise_ranges(
        session,
        subject_id=subject_id,
        start=phase.start_date,
        end=today,
    )
    trend = fit_trend(points, exclude=ranges)
    if trend is None:
        return None

    if trend.slope_per_week >= PLATEAU_SLOPE_THRESHOLD:
        return {
            "drug": phase.drug,
            "dose_mg": phase.dose_mg,
            "days_on_dose": days_on_dose,
            "slope_per_week": round(trend.slope_per_week, 3),
        }
    return None


async def refresh_plateau_alert(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[object]:
    """Raise a ``note`` alert while the current dose is plateaued; resolve it once
    progress resumes (or the dose changes). Idempotent — safe on every dashboard
    load / scheduler tick. Respects same-day dismissal like the noise alert."""
    write_context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if on_date is not None:
        _require_evaluation_date(write_context, on_date)
    plateau = await evaluate_plateau(
        session,
        on_date=on_date,
        subject_id=write_context.identity.subject_id,
        scope=write_context.scope,
    )

    system_identity = WriteIdentity(write_context.identity.subject_id, None)
    alert_context = alerts_service.HealthAlertContext(system_identity)
    alert_bridge = (
        alerts_service.LegacyAlertBridge.FULLY_UNOWNED
        if write_context.scope.include_legacy_unowned
        else alerts_service.LegacyAlertBridge.REJECT
    )
    if plateau is not None:
        if await alerts_service.was_scoped_dismissed_today(
            session,
            context=alert_context,
            alert_key=PLATEAU_ALERT_KEY,
            entity_ref="",
            on_date=write_context.evaluation_date,
            legacy_bridge=alert_bridge,
        ):
            return None
        message = t(
            "alert.glp1_plateau",
            drug=plateau["drug"],
            dose=plateau["dose_mg"],
            days=plateau["days_on_dose"],
            slope=plateau["slope_per_week"],
        )
        return await alerts_service.raise_scoped_alert(
            session,
            context=alert_context,
            domain=Domain.GLP1,
            severity=Severity.NOTE,
            message=message,
            alert_key=PLATEAU_ALERT_KEY,
            legacy_bridge=alert_bridge,
        )
    return await alerts_service.resolve_scoped_by_key(
        session,
        context=alert_context,
        alert_key=PLATEAU_ALERT_KEY,
        legacy_bridge=alert_bridge,
    )


# ── Scheduler job ─────────────────────────────────────────────────────────────
async def plateau_job(
    session_factory, redis=None, *, subject_id: uuid.UUID
) -> None:
    """Daily plateau check (registered in vitals/scheduler/jobs.py). Runs the same
    refresh the dashboard does, so the alert is fresh even without a page load."""
    async with session_factory() as session:
        today = today_local()
        context = await conflict_engine.resolve_subject_conflict_write_context(
            session,
            subject_id=subject_id,
            evaluation_date=today,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=context,
        )
        enabled = await modules_service.get_enabled_modules(
            session,
            redis,
            subject_id=context.identity.subject_id,
        )
        if not enabled.get("glp1", False):
            await session.commit()
            return

        from vitals.services.language_service import get_language
        from vitals.i18n import current_lang
        from vitals.models.identity import HealthSubject

        owner_user_id = await session.scalar(
            select(HealthSubject.owner_user_id).where(
                HealthSubject.id == context.identity.subject_id
            )
        )
        lang = await get_language(session, redis, user_id=owner_user_id)
        current_lang.set(lang)

        await refresh_plateau_alert(
            session,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
        await session.commit()
