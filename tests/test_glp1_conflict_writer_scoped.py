"""Focused subject-scoped writer contract for the GLP-1 domain."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, RuleType, Severity, Source, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.glp1 import DosePhase, Injection, SideEffect
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.system_alert import SystemAlert
from vitals.models.weight import NoiseMarker, WeightLog
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, glp1_service, modules_service


EVALUATION_DATE = date(2026, 8, 20)
OTHER_DATE = date(2026, 8, 19)


def _context(
    identity: WriteIdentity,
    *,
    on_date: date = EVALUATION_DATE,
    legacy_bridge: bool = False,
) -> conflict_engine.ConflictWriteContext:
    return conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy_bridge
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )


async def _prepared(session: AsyncSession, context):
    return await conflict_engine.prepare_scoped_write(session, context=context)


async def _identity(session: AsyncSession, slug: str) -> WriteIdentity:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    return WriteIdentity(subject.id, user.id)


async def _legacy_context(session: AsyncSession, *, on_date=EVALUATION_DATE):
    context = await conflict_engine.resolve_legacy_conflict_write_context(
        session,
        actor_username="tester",
        evaluation_date=on_date,
    )
    return context, await _prepared(session, context)


async def _blocking_rule(
    session: AsyncSession,
    identity: WriteIdentity,
) -> ConflictRule:
    rule = ConflictRule(
        subject_id=identity.subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.LABS.value,
        condition_a={"marker": "synthetic-risk"},
        domain_b=Domain.GLP1.value,
        condition_b={"drug": "semaglutide"},
        severity=Severity.BLOCK.value,
        message="Synthetic scoped GLP-1 conflict.",
        active=True,
    )
    session.add(rule)
    await session.commit()
    return rule


def _register_resolvers() -> None:
    async def labs(session, *, scope):
        del session, scope
        return [{"marker": "synthetic-risk"}]

    conflict_engine.register_domain_resolver(Domain.LABS.value, labs)
    conflict_engine.register_domain_resolver(
        Domain.GLP1.value,
        glp1_service.resolve_active_scoped,
    )


async def test_all_writers_stamp_subject_actor_and_requested_source(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    context = _context(identity)
    prepared = await _prepared(db_session, context)

    injections = [
        await glp1_service.log_injection(
            db_session,
            on_date=EVALUATION_DATE,
            drug="semaglutide",
            dose_mg=0.5,
            source=source.value,
            identity=identity,
            prepared_conflict_write=prepared,
        )
        for source in (Source.MANUAL, Source.MCP)
    ]
    phases = [
        await glp1_service.add_dose_phase(
            db_session,
            start_date=EVALUATION_DATE,
            end_date=EVALUATION_DATE,
            drug="semaglutide",
            dose_mg=0.5,
            source=source.value,
            identity=identity,
            prepared_conflict_write=prepared,
        )
        for source in (Source.MANUAL, Source.MCP)
    ]
    effects = [
        await glp1_service.log_side_effect(
            db_session,
            on_date=EVALUATION_DATE,
            effect_type="nausea",
            severity=2,
            source=source.value,
            identity=identity,
            prepared_conflict_write=prepared,
        )
        for source in (Source.MANUAL, Source.MCP)
    ]

    for rows in (injections, phases, effects):
        assert [row.source for row in rows] == [
            Source.MANUAL.value,
            Source.MCP.value,
        ]
        assert all(
            (row.subject_id, row.actor_user_id)
            == (identity.subject_id, identity.actor_user_id)
            for row in rows
        )


async def test_prepared_identity_and_date_fail_before_target_read(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    prepared = await _prepared(db_session, _context(identity, on_date=OTHER_DATE))
    target_reads = 0

    async def target_probe(*args, **kwargs):
        nonlocal target_reads
        target_reads += 1
        raise AssertionError("target row must not be read")

    monkeypatch.setattr(glp1_service, "_owned_row_for_update", target_probe)

    with pytest.raises(conflict_engine.ConflictPreparedWriteError, match="date"):
        await glp1_service.update_injection(
            db_session,
            1,
            on_date=EVALUATION_DATE,
            drug="semaglutide",
            dose_mg=0.5,
            identity=identity,
            prepared_conflict_write=prepared,
        )
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await glp1_service.delete_injection(
            db_session,
            1,
            identity=WriteIdentity(identity.subject_id, uuid.uuid4()),
            prepared_conflict_write=prepared,
        )
    assert target_reads == 0


async def test_conflict_block_is_write_free_and_override_is_human_attributed(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    rule = await _blocking_rule(db_session, identity)
    _register_resolvers()
    prepared = await _prepared(db_session, _context(identity))

    with pytest.raises(conflict_engine.ConflictBlocked):
        await glp1_service.log_injection(
            db_session,
            on_date=EVALUATION_DATE,
            drug="semaglutide",
            dose_mg=0.5,
            identity=identity,
            prepared_conflict_write=prepared,
        )
    assert await db_session.scalar(select(func.count()).select_from(Injection)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0

    row = await glp1_service.log_injection(
        db_session,
        on_date=EVALUATION_DATE,
        drug="semaglutide",
        dose_mg=0.5,
        override=True,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    assert row.subject_id == identity.subject_id
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (
        identity.subject_id,
        None,
    )
    assert alert.entity_ref == f"injection:{EVALUATION_DATE.isoformat()}"
    assert alert.overridden_by_user_id == identity.actor_user_id
    assert alert.override_at is not None


async def test_blocked_phase_does_not_close_existing_phase(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    baseline_context = _context(identity, on_date=OTHER_DATE)
    baseline = await glp1_service.add_dose_phase(
        db_session,
        start_date=OTHER_DATE,
        drug="tirzepatide",
        dose_mg=2.5,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, baseline_context),
    )
    await db_session.commit()
    await _blocking_rule(db_session, identity)
    _register_resolvers()

    with pytest.raises(conflict_engine.ConflictBlocked):
        await glp1_service.add_dose_phase(
            db_session,
            start_date=EVALUATION_DATE,
            drug="semaglutide",
            dose_mg=0.5,
            identity=identity,
            prepared_conflict_write=await _prepared(
                db_session,
                _context(identity),
            ),
        )

    await db_session.refresh(baseline)
    assert baseline.end_date is None
    assert await db_session.scalar(select(func.count()).select_from(DosePhase)) == 1
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0


async def test_reads_updates_notes_and_deletes_are_strictly_subject_scoped(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    foreign_identity = await _identity(db_session, "foreign-glp1")
    owned = Injection(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=EVALUATION_DATE,
        domain=Domain.GLP1.value,
        source=Source.MCP.value,
        drug="semaglutide",
        dose_mg=0.5,
        note="old",
    )
    foreign = Injection(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        date=EVALUATION_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        drug="tirzepatide",
        dose_mg=2.5,
    )
    owned_phase = DosePhase(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=EVALUATION_DATE,
        drug="semaglutide",
        dose_mg=0.5,
    )
    foreign_phase = DosePhase(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=EVALUATION_DATE,
        drug="tirzepatide",
        dose_mg=2.5,
    )
    owned_effect = SideEffect(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=EVALUATION_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        effect_type="nausea",
        severity=2,
    )
    foreign_effect = SideEffect(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        date=EVALUATION_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        effect_type="fatigue",
        severity=3,
    )
    db_session.add_all(
        [owned, foreign, owned_phase, foreign_phase, owned_effect, foreign_effect]
    )
    await db_session.commit()

    assert [row.id for row in await glp1_service.list_injections(
        db_session, subject_id=identity.subject_id
    )] == [owned.id]
    assert [row.id for row in await glp1_service.list_dose_phases(
        db_session, subject_id=identity.subject_id
    )] == [owned_phase.id]
    assert [row.id for row in await glp1_service.list_side_effects(
        db_session, subject_id=identity.subject_id
    )] == [owned_effect.id]

    context = _context(identity)
    assert await glp1_service.update_injection(
        db_session,
        foreign.id,
        on_date=EVALUATION_DATE,
        drug="forged",
        dose_mg=9,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, context),
    ) is None
    updated = await glp1_service.update_injection_note(
        db_session,
        owned.id,
        note="scoped note",
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, context),
    )
    assert updated is owned
    assert (owned.note, owned.source, owned.actor_user_id) == (
        "scoped note",
        Source.MCP.value,
        identity.actor_user_id,
    )
    assert await glp1_service.delete_injection(
        db_session,
        foreign.id,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, context),
    ) is False
    assert await glp1_service.delete_dose_phase(
        db_session,
        foreign_phase.id,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, context),
    ) is False
    assert await glp1_service.delete_side_effect(
        db_session,
        foreign_effect.id,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, context),
    ) is False
    assert foreign.drug == "tirzepatide"


async def test_no_glp1_reader_or_writer_reaches_an_unowned_row(
    db_session,
    legacy_owner_roots,
):
    full_injection = Injection(
        date=EVALUATION_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        drug="semaglutide",
        dose_mg=0.5,
    )
    partial_injection = Injection(
        actor_user_id=legacy_owner_roots.user_id,
        date=EVALUATION_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        drug="tirzepatide",
        dose_mg=2.5,
    )
    full_phase = DosePhase(
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=EVALUATION_DATE,
        drug="semaglutide",
        dose_mg=0.5,
    )
    partial_phase = DosePhase(
        actor_user_id=legacy_owner_roots.user_id,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=EVALUATION_DATE,
        drug="tirzepatide",
        dose_mg=2.5,
    )
    full_effect = SideEffect(
        date=EVALUATION_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        effect_type="nausea",
        severity=2,
    )
    partial_effect = SideEffect(
        actor_user_id=legacy_owner_roots.user_id,
        date=EVALUATION_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        effect_type="fatigue",
        severity=3,
    )
    db_session.add_all(
        [
            full_injection,
            partial_injection,
            full_phase,
            partial_phase,
            full_effect,
            partial_effect,
        ]
    )
    await db_session.commit()
    context, prepared = await _legacy_context(db_session)

    assert [row.id for row in await glp1_service.list_injections(
        db_session,
        subject_id=context.identity.subject_id,
    )] == []
    assert [row.id for row in await glp1_service.list_dose_phases(
        db_session,
        subject_id=context.identity.subject_id,
    )] == []
    assert [row.id for row in await glp1_service.list_side_effects(
        db_session,
        subject_id=context.identity.subject_id,
    )] == []

    # Adoption on write went with the bridge, so an unowned row stays unowned
    # and unedited rather than being claimed by the first writer to touch it.
    assert await glp1_service.update_injection(
        db_session,
        full_injection.id,
        on_date=EVALUATION_DATE,
        drug="semaglutide",
        dose_mg=1,
        identity=context.identity,
        prepared_conflict_write=prepared,
    ) is None
    assert full_injection.subject_id is None
    assert await glp1_service.delete_injection(
        db_session,
        partial_injection.id,
        identity=context.identity,
        prepared_conflict_write=prepared,
    ) is False
    assert await glp1_service.delete_dose_phase(
        db_session,
        partial_phase.id,
        identity=context.identity,
        prepared_conflict_write=prepared,
    ) is False
    assert await glp1_service.delete_side_effect(
        db_session,
        partial_effect.id,
        identity=context.identity,
        prepared_conflict_write=prepared,
    ) is False


async def test_open_phase_auto_close_never_crosses_subjects(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    foreign_identity = await _identity(db_session, "foreign-phase-owner")
    old = DosePhase(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=OTHER_DATE,
        drug="semaglutide",
        dose_mg=0.25,
    )
    foreign = DosePhase(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=OTHER_DATE,
        drug="tirzepatide",
        dose_mg=2.5,
    )
    db_session.add_all([old, foreign])
    await db_session.commit()

    await glp1_service.add_dose_phase(
        db_session,
        start_date=EVALUATION_DATE,
        drug="semaglutide",
        dose_mg=0.5,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, _context(identity)),
    )

    assert old.end_date == EVALUATION_DATE - timedelta(days=1)
    assert foreign.end_date is None


async def test_backdated_open_phase_stops_before_newer_open_phase(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    newer = await glp1_service.add_dose_phase(
        db_session,
        start_date=EVALUATION_DATE,
        drug="semaglutide",
        dose_mg=0.5,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, _context(identity)),
    )
    await db_session.commit()

    older = await glp1_service.add_dose_phase(
        db_session,
        start_date=OTHER_DATE,
        drug="semaglutide",
        dose_mg=0.25,
        identity=identity,
        prepared_conflict_write=await _prepared(
            db_session,
            _context(identity, on_date=OTHER_DATE),
        ),
    )

    assert older.end_date == EVALUATION_DATE - timedelta(days=1)
    assert newer.end_date is None


async def test_repeated_same_day_phase_leaves_only_newest_row_open(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    first = await glp1_service.add_dose_phase(
        db_session,
        start_date=EVALUATION_DATE,
        drug="semaglutide",
        dose_mg=0.25,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, _context(identity)),
    )
    await db_session.commit()

    second = await glp1_service.add_dose_phase(
        db_session,
        start_date=EVALUATION_DATE,
        drug="semaglutide",
        dose_mg=0.5,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, _context(identity)),
    )

    assert first.end_date == EVALUATION_DATE
    assert second.end_date is None
    active = await glp1_service.active_dose_phase(
        db_session,
        on_date=EVALUATION_DATE,
        subject_id=identity.subject_id,
    )
    assert active is second


async def test_plateau_uses_scoped_phase_weights_noise_and_typed_health_alert(
    db_session,
    legacy_owner_roots,
):
    today = EVALUATION_DATE
    start = today - timedelta(days=18)
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    foreign_identity = await _identity(db_session, "foreign-plateau-owner")
    own_phase = DosePhase(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=start,
        drug="semaglutide",
        dose_mg=0.5,
    )
    foreign_phase = DosePhase(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=today - timedelta(days=5),
        drug="tirzepatide",
        dose_mg=10,
    )
    own_weights = [
        WeightLog(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            date=day,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            weight_kg=88,
            superseded=False,
        )
        for day in (start, today)
    ]
    own_raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        external_id="owned-weight-raw",
        payload={"weight_kg": 88},
    )
    db_session.add(own_raw)
    await db_session.flush()
    own_weights[0].raw_payload_id = own_raw.id
    foreign_weights = [
        WeightLog(
            subject_id=foreign_identity.subject_id,
            actor_user_id=foreign_identity.actor_user_id,
            date=day,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            weight_kg=value,
            superseded=False,
        )
        for day, value in (
            (start + timedelta(days=4), 110),
            (start + timedelta(days=10), 60),
        )
    ]
    foreign_noise = NoiseMarker(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        start_date=start,
        end_date=today,
        reason="foreign-only noise",
    )
    db_session.add_all(
        [own_phase, foreign_phase, foreign_noise, *own_weights, *foreign_weights]
    )
    await db_session.commit()
    context = _context(identity, on_date=today)
    prepared = await _prepared(db_session, context)

    plateau = await glp1_service.evaluate_plateau(
        db_session,
        subject_id=identity.subject_id,
        scope=context.scope,
    )
    assert plateau is not None
    assert (plateau["drug"], plateau["dose_mg"], plateau["days_on_dose"]) == (
        "semaglutide",
        0.5,
        18,
    )

    alert = await glp1_service.refresh_plateau_alert(
        db_session,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (
        identity.subject_id,
        None,
    )
    assert (alert.domain, alert.severity, alert.alert_key) == (
        Domain.GLP1.value,
        Severity.NOTE.value,
        glp1_service.PLATEAU_ALERT_KEY,
    )
    assert (alert.overridden_by_user_id, alert.resolved_by_user_id) == (None, None)

    own_weights[-1].weight_kg = 84
    await db_session.flush()
    resolved = await glp1_service.refresh_plateau_alert(
        db_session,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    assert resolved is alert
    assert alert.resolved_at is not None
    assert alert.resolved_by_user_id is None


async def test_plateau_rejects_weight_linked_to_foreign_raw_provenance(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    foreign_identity = await _identity(db_session, "foreign-weight-raw-owner")
    raw = RawPayload(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        external_id="foreign-weight-raw",
        payload={"weight_kg": 88},
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add_all(
        [
            DosePhase(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                domain=Domain.GLP1.value,
                source=Source.MANUAL.value,
                start_date=EVALUATION_DATE - timedelta(days=18),
                drug="semaglutide",
                dose_mg=0.5,
            ),
            WeightLog(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                raw_payload_id=raw.id,
                date=EVALUATION_DATE,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=88,
                superseded=False,
            ),
        ]
    )
    await db_session.commit()

    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await glp1_service.evaluate_plateau(
            db_session,
            subject_id=identity.subject_id,
            scope=_context(identity).scope,
        )


async def test_plateau_rejects_legacy_weight_linked_to_partial_raw_provenance(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    raw = RawPayload(
        actor_user_id=identity.actor_user_id,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        external_id="partial-weight-raw",
        payload={"weight_kg": 88},
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add_all(
        [
            DosePhase(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                domain=Domain.GLP1.value,
                source=Source.MANUAL.value,
                start_date=EVALUATION_DATE - timedelta(days=18),
                drug="semaglutide",
                dose_mg=0.5,
            ),
            WeightLog(
                raw_payload_id=raw.id,
                date=EVALUATION_DATE,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=88,
                superseded=False,
            ),
        ]
    )
    await db_session.commit()

    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await glp1_service.evaluate_plateau(
            db_session,
            subject_id=identity.subject_id,
            scope=_context(identity, legacy_bridge=True).scope,
        )


async def test_plateau_job_is_noop_when_subject_module_is_disabled(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    await modules_service.set_module_enabled(
        db_session,
        key="glp1",
        enabled=False,
        subject_id=legacy_owner_roots.subject_id,
    )
    await db_session.commit()
    calls = 0

    async def refresh_probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("disabled GLP-1 job must not reconcile alerts")

    monkeypatch.setattr(glp1_service, "refresh_plateau_alert", refresh_probe)

    await glp1_service.plateau_job(session_factory)

    assert calls == 0
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0


def test_phase_form_keeps_post_fallback_when_javascript_is_unavailable():
    template = (
        Path(__file__).parents[1] / "web" / "templates" / "glp1" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'action="/glp1/phase" method="POST"' in template


async def test_web_boundaries_stamp_manual_block_override_and_hide_partial_roots(
    auth_client,
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    rule = await _blocking_rule(db_session, identity)
    _register_resolvers()

    blocked = await auth_client.post(
        "/glp1/injection",
        data={
            "date": EVALUATION_DATE.isoformat(),
            "drug": "semaglutide",
            "dose_mg": "0.5",
        },
    )
    assert blocked.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(Injection)) == 0

    overridden = await auth_client.post(
        "/glp1/injection",
        data={
            "date": EVALUATION_DATE.isoformat(),
            "drug": "semaglutide",
            "dose_mg": "0.5",
            "override": "true",
        },
    )
    assert overridden.status_code == 303
    assert (
        await auth_client.post(
            "/glp1/phase",
            data={
                "start_date": EVALUATION_DATE.isoformat(),
                "end_date": EVALUATION_DATE.isoformat(),
                "drug": "tirzepatide",
                "dose_mg": "2.5",
            },
        )
    ).status_code == 303
    assert (
        await auth_client.post(
            "/glp1/side-effect",
            data={
                "date": EVALUATION_DATE.isoformat(),
                "effect_type": "nausea",
                "severity": "2",
            },
        )
    ).status_code == 303

    injection = await db_session.scalar(select(Injection))
    phase = await db_session.scalar(select(DosePhase))
    effect = await db_session.scalar(select(SideEffect))
    assert injection is not None and phase is not None and effect is not None
    for row in (injection, phase, effect):
        assert (row.subject_id, row.actor_user_id, row.source) == (
            identity.subject_id,
            identity.actor_user_id,
            Source.MANUAL.value,
        )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    assert alert is not None
    assert alert.overridden_by_user_id == identity.actor_user_id

    partial_injection = Injection(
        actor_user_id=identity.actor_user_id,
        date=OTHER_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        drug="hidden-partial-drug",
        dose_mg=9,
    )
    partial_phase = DosePhase(
        actor_user_id=identity.actor_user_id,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=OTHER_DATE,
        drug="hidden-partial-phase",
        dose_mg=9,
    )
    partial_effect = SideEffect(
        actor_user_id=identity.actor_user_id,
        date=OTHER_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        effect_type="hidden-partial-effect",
        severity=3,
    )
    db_session.add_all([partial_injection, partial_phase, partial_effect])
    await db_session.commit()

    page = await auth_client.get("/glp1", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "hidden-partial" not in page.text
    assert (
        await auth_client.post(f"/glp1/injection/{partial_injection.id}/delete")
    ).status_code == 303
    assert (
        await auth_client.post(f"/glp1/phase/{partial_phase.id}/delete")
    ).status_code == 303
    assert (
        await auth_client.post(f"/glp1/side-effect/{partial_effect.id}/delete")
    ).status_code == 303
    assert await db_session.get(Injection, partial_injection.id) is partial_injection
    assert await db_session.get(DosePhase, partial_phase.id) is partial_phase
    assert await db_session.get(SideEffect, partial_effect.id) is partial_effect


async def test_mcp_boundaries_stamp_mcp_scope_reads_notes_deletes_and_conflicts(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    from web.routers import mcp as mcp_router

    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    await modules_service.set_module_enabled(
        db_session,
        key="glp1",
        enabled=True,
        subject_id=identity.subject_id,
    )
    await db_session.commit()

    written_injection = await mcp_router.log_glp1(
        drug="tirzepatide",
        dose_mg=2.5,
        on_date=EVALUATION_DATE.isoformat(),
        note="keep this MCP note",
    )
    written_phase = await mcp_router.add_dose_phase(
        start_date=EVALUATION_DATE.isoformat(),
        end_date=EVALUATION_DATE.isoformat(),
        drug="tirzepatide",
        dose_mg=2.5,
    )
    written_effect = await mcp_router.log_side_effect(
        effect_type="nausea",
        severity=2,
        on_date=EVALUATION_DATE.isoformat(),
    )
    injection = await db_session.get(Injection, written_injection["id"])
    phase = await db_session.get(DosePhase, written_phase["id"])
    effect = await db_session.get(SideEffect, written_effect["id"])
    assert injection is not None and phase is not None and effect is not None
    for row in (injection, phase, effect):
        assert (row.subject_id, row.actor_user_id, row.source) == (
            identity.subject_id,
            identity.actor_user_id,
            Source.MCP.value,
        )

    partial_injection = Injection(
        actor_user_id=identity.actor_user_id,
        date=OTHER_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        drug="partial-injection",
        dose_mg=9,
        note="hidden partial note",
    )
    partial_phase = DosePhase(
        actor_user_id=identity.actor_user_id,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        start_date=OTHER_DATE,
        drug="partial-phase",
        dose_mg=9,
    )
    partial_effect = SideEffect(
        actor_user_id=identity.actor_user_id,
        date=OTHER_DATE,
        domain=Domain.GLP1.value,
        source=Source.MANUAL.value,
        effect_type="partial-effect",
        severity=3,
    )
    db_session.add_all([partial_injection, partial_phase, partial_effect])
    await db_session.commit()

    listing = await mcp_router.get_glp1_logs()
    assert [row["id"] for row in listing["injections"]] == [injection.id]
    assert [row["id"] for row in listing["dose_phases"]] == [phase.id]
    assert [row["id"] for row in listing["side_effects"]] == [effect.id]
    assert await mcp_router.update_glp1(
        partial_injection.id,
        dose_mg=8,
    ) == {"error": f"Injection {partial_injection.id} not found"}
    assert await mcp_router.log_note(
        "glp1",
        partial_injection.id,
        "forged",
    ) == {"error": f"glp1 record {partial_injection.id} not found"}
    notes = await mcp_router.get_notes(domain="glp1")
    assert [row["id"] for row in notes] == [injection.id]

    updated = await mcp_router.update_glp1(injection.id, dose_mg=3)
    assert (updated["dose_mg"], updated["note"]) == (3, "keep this MCP note")
    noted = await mcp_router.log_note("glp1", injection.id, "updated MCP note")
    assert noted["note"] == "updated MCP note"
    await db_session.refresh(injection)
    assert (injection.source, injection.actor_user_id, injection.note) == (
        Source.MCP.value,
        identity.actor_user_id,
        "updated MCP note",
    )

    for domain, row in (
        ("glp1", partial_injection),
        ("glp1_dose_phase", partial_phase),
        ("glp1_side_effect", partial_effect),
    ):
        assert await mcp_router.delete_record(domain, row.id) == {
            "deleted": False,
            "domain": domain,
            "record_id": row.id,
        }

    rule = await _blocking_rule(db_session, identity)
    _register_resolvers()
    blocked = await mcp_router.log_glp1(
        drug="semaglutide",
        dose_mg=0.5,
        on_date=EVALUATION_DATE.isoformat(),
    )
    assert blocked["blocked"] is True
    overridden = await mcp_router.log_glp1(
        drug="semaglutide",
        dose_mg=0.5,
        on_date=EVALUATION_DATE.isoformat(),
        override=True,
    )
    saved = await db_session.get(Injection, overridden["id"])
    assert saved is not None
    assert (saved.subject_id, saved.actor_user_id, saved.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MCP.value,
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    assert alert is not None
    assert alert.overridden_by_user_id == identity.actor_user_id


@pytest.mark.integration
async def test_postgres_same_subject_concurrent_phases_leave_one_open_phase(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    await db_session.commit()

    async def create(start_date: date, dose_mg: float) -> None:
        async with factory() as session:
            context = _context(identity, on_date=start_date)
            await glp1_service.add_dose_phase(
                session,
                start_date=start_date,
                drug="semaglutide",
                dose_mg=dose_mg,
                identity=identity,
                prepared_conflict_write=await _prepared(session, context),
            )
            await session.commit()

    await asyncio.gather(
        create(OTHER_DATE, 0.25),
        create(EVALUATION_DATE, 0.5),
    )

    async with factory() as verify:
        rows = list(
            await verify.scalars(
                select(DosePhase)
                .where(DosePhase.subject_id == identity.subject_id)
                .order_by(DosePhase.start_date)
            )
        )
    assert len(rows) == 2
    assert sum(row.end_date is None for row in rows) == 1
    assert rows[-1].end_date is None


@pytest.mark.integration
async def test_postgres_same_day_concurrent_phases_leave_one_open_phase(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    await db_session.commit()

    async def create(dose_mg: float) -> None:
        async with factory() as session:
            context = _context(identity)
            await glp1_service.add_dose_phase(
                session,
                start_date=EVALUATION_DATE,
                drug="semaglutide",
                dose_mg=dose_mg,
                identity=identity,
                prepared_conflict_write=await _prepared(session, context),
            )
            await session.commit()

    await asyncio.gather(create(0.25), create(0.5))

    async with factory() as verify:
        rows = list(
            await verify.scalars(
                select(DosePhase)
                .where(DosePhase.subject_id == identity.subject_id)
                .order_by(DosePhase.id)
            )
        )
    assert len(rows) == 2
    assert sum(row.end_date is None for row in rows) == 1
    assert [row.end_date for row in rows if row.end_date is not None] == [
        EVALUATION_DATE
    ]


@pytest.mark.integration
async def test_postgres_legacy_phase_write_serializes_subject_governance(
    db_session,
    legacy_owner_roots,
):
    from vitals.services.identity_service import acquire_identity_governance_lock

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    bridge_locked = asyncio.Event()
    subject_write_attempted = asyncio.Event()
    await db_session.commit()

    async def legacy_phase_write() -> None:
        async with factory() as session:
            context = await conflict_engine.resolve_legacy_conflict_write_context(
                session,
                actor_username="tester",
                evaluation_date=EVALUATION_DATE,
            )
            bridge_locked.set()
            await asyncio.wait_for(subject_write_attempted.wait(), timeout=5)
            await glp1_service.add_dose_phase(
                session,
                start_date=EVALUATION_DATE,
                drug="semaglutide",
                dose_mg=0.5,
                identity=context.identity,
                prepared_conflict_write=await _prepared(session, context),
            )
            await session.commit()

    async def create_second_subject() -> None:
        await asyncio.wait_for(bridge_locked.wait(), timeout=5)
        async with factory() as session:
            subject_write_attempted.set()
            await acquire_identity_governance_lock(session)
            user = User(
                username="glp1-race-second",
                normalized_username="glp1-race-second",
                password_hash="$synthetic-test-hash",
                status=UserStatus.ACTIVE.value,
            )
            session.add(user)
            await session.flush()
            session.add(HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty"))
            await session.commit()

    await asyncio.wait_for(
        asyncio.gather(legacy_phase_write(), create_second_subject()),
        timeout=10,
    )

    async with factory() as verify:
        assert await verify.scalar(
            select(func.count()).select_from(HealthSubject)
        ) == 2
        phase = await verify.scalar(select(DosePhase))
        assert phase is not None
        assert phase.subject_id == legacy_owner_roots.subject_id
