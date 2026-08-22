"""Focused Stage-2 ownership and projected-write tests for HRT core."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from vitals.enums import Domain, RuleType, Severity, Source, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.hrt import HrtCycle, HrtDose, HrtSideEffect
from vitals.models.identity import HealthSubject, User
from vitals.models.system_alert import SystemAlert
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, hrt_catalog, hrt_reminders, hrt_service


TODAY = date(2026, 8, 20)


def _context(
    identity: WriteIdentity,
    *,
    legacy_bridge: bool = False,
) -> conflict_engine.ConflictWriteContext:
    return conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=TODAY,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy_bridge
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )


async def _prepared(session, identity, *, legacy_bridge: bool = False):
    return await conflict_engine.prepare_scoped_write(
        session,
        context=_context(identity, legacy_bridge=legacy_bridge),
    )


async def _identity(session, slug: str) -> WriteIdentity:
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


async def _add_a_rule(session, identity: WriteIdentity) -> None:
    session.add(
        ConflictRule(
            subject_id=identity.subject_id,
            rule_type=RuleType.HARD_BLOCK.value,
            domain_a=Domain.HRT.value,
            condition_a={"compound_key": "oxandrolone"},
            domain_b=Domain.LABS.value,
            condition_b={"marker": "synthetic-risk"},
            severity=Severity.BLOCK.value,
            message="Synthetic HRT replacement conflict.",
            active=True,
        )
    )
    await session.commit()


def _register_resolvers() -> None:
    async def labs(session, *, scope):
        del session, scope
        return [{"marker": "synthetic-risk"}]

    conflict_engine.register_domain_resolver(
        Domain.HRT.value,
        hrt_service.resolve_active_scoped,
    )
    conflict_engine.register_domain_resolver(Domain.LABS.value, labs)


async def _dose(session, identity: WriteIdentity, compound) -> HrtDose:
    row = HrtDose(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=TODAY,
        domain=Domain.HRT.value,
        source=Source.MCP.value,
        compound_id=compound.id,
        compound_key=compound.key,
        dose=20,
        unit="mg",
        note="original",
    )
    session.add(row)
    await session.flush()
    return row


async def test_scoped_dose_and_side_effect_crud_preserves_origin_and_scope(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    foreign_identity = await _identity(db_session, "foreign-hrt-core")
    await hrt_catalog.sync_catalog(db_session)
    compound = await hrt_service.get_compound(
        db_session,
        "oxandrolone",
        subject_id=identity.subject_id,
    )
    assert compound is not None
    owned = await _dose(db_session, identity, compound)
    foreign = await _dose(db_session, foreign_identity, compound)
    partial = HrtDose(
        actor_user_id=identity.actor_user_id,
        date=TODAY,
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        compound_key=compound.key,
        dose=10,
        unit="mg",
    )
    db_session.add(partial)
    await db_session.commit()

    assert [row.id for row in await hrt_service.list_doses(
        db_session,
        subject_id=identity.subject_id,
    )] == [owned.id]
    assert await hrt_service.get_dose_for_update(
        db_session,
        foreign.id,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    ) is None

    updated = await hrt_service.update_dose(
        db_session,
        owned.id,
        compound_key=compound.key,
        on_date=TODAY,
        dose=25,
        unit="mg",
        note="merged",
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    assert updated is owned
    assert (updated.actor_user_id, updated.source, updated.note) == (
        identity.actor_user_id,
        Source.MCP.value,
        "merged",
    )
    assert await hrt_service.delete_dose(
        db_session,
        partial.id,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    ) is False

    effect = await hrt_service.log_side_effect(
        db_session,
        on_date=TODAY,
        effect_type="acne",
        severity=2,
        source=Source.MCP.value,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    changed = await hrt_service.update_side_effect(
        db_session,
        effect.id,
        on_date=TODAY,
        effect_type="acne",
        severity=3,
        note="kept provenance",
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    assert changed is effect
    assert (effect.actor_user_id, effect.source) == (
        identity.actor_user_id,
        Source.MCP.value,
    )




async def test_scoped_write_rejects_tampered_catalog_dose_metadata(
    db_session,
    legacy_owner_roots,
    owner_write,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    await hrt_catalog.sync_catalog(db_session)
    compound = await hrt_service.get_compound(
        db_session,
        "testosterone_enanthate",
        subject_id=owner_write.subject_id,
    )
    assert compound is not None
    compound.conc_mg_ml = 999
    await db_session.commit()

    with pytest.raises(hrt_service.HrtCatalogIntegrityError, match="conc_mg_ml"):
        await hrt_service.log_dose(
            db_session,
            compound_key=compound.key,
            on_date=TODAY,
            volume_ml=1,
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, identity),
        )

    assert await db_session.scalar(select(func.count()).select_from(HrtDose)) == 0


async def test_update_replaces_only_the_exact_dose_in_conflict_snapshot(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    await hrt_catalog.sync_catalog(db_session)
    oxandrolone = await hrt_service.get_compound(
        db_session,
        "oxandrolone",
        subject_id=identity.subject_id,
    )
    assert oxandrolone is not None
    sole = await _dose(db_session, identity, oxandrolone)
    await db_session.commit()
    await _add_a_rule(db_session, identity)
    _register_resolvers()

    changed = await hrt_service.update_dose(
        db_session,
        sole.id,
        compound_key="testosterone_enanthate",
        on_date=TODAY,
        dose=100,
        unit="mg",
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    assert changed is sole
    assert (changed.compound_key, changed.source, changed.actor_user_id) == (
        "testosterone_enanthate",
        Source.MCP.value,
        identity.actor_user_id,
    )


async def test_update_keeps_another_matching_dose_in_conflict_snapshot(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    await hrt_catalog.sync_catalog(db_session)
    oxandrolone = await hrt_service.get_compound(
        db_session,
        "oxandrolone",
        subject_id=identity.subject_id,
    )
    assert oxandrolone is not None
    first = await _dose(db_session, identity, oxandrolone)
    await _dose(db_session, identity, oxandrolone)
    await db_session.commit()
    await _add_a_rule(db_session, identity)
    _register_resolvers()

    with pytest.raises(conflict_engine.ConflictBlocked):
        await hrt_service.update_dose(
            db_session,
            first.id,
            compound_key="testosterone_enanthate",
            on_date=TODAY,
            dose=100,
            unit="mg",
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, identity),
        )
    assert first.compound_key == "oxandrolone"


async def test_scoped_reminder_writes_actorless_subject_alert(
    db_session,
    legacy_owner_roots,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    foreign_identity = await _identity(db_session, "foreign-hrt-reminder")
    db_session.add_all(
        [
            HrtCycle(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                kind="course",
                start_date=TODAY,
            ),
            HrtCycle(
                subject_id=foreign_identity.subject_id,
                actor_user_id=foreign_identity.actor_user_id,
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                kind="pct",
                start_date=TODAY,
            ),
        ]
    )
    await db_session.commit()

    await hrt_reminders.refresh_labs_due(
        db_session,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == hrt_reminders.LABS_DUE_KEY)
    )
    assert alert is not None
    assert (
        alert.subject_id,
        alert.integration_connection_id,
        alert.overridden_by_user_id,
        alert.resolved_by_user_id,
    ) == (
        identity.subject_id,
        None,
        None,
        None,
    )


async def test_scoped_catalog_rejects_unknown_compound_and_freezes_activation(
    db_session,
    legacy_owner_roots,
    owner_write,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    await hrt_catalog.sync_catalog(db_session)
    with pytest.raises(ValueError, match="checked-in"):
        await hrt_service.log_dose(
            db_session,
            compound_key="custom-compound",
            on_date=TODAY,
            dose=10,
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, identity),
        )
    compound = await hrt_service.get_compound(
        db_session,
        "oxandrolone",
        subject_id=identity.subject_id,
    )
    with pytest.raises(hrt_service.HrtCompoundActivationCutoverRequiredError):
        await hrt_service.set_compound_active(
            db_session,
            compound.id,
            active=False,
            subject_id=identity.subject_id,
    )


async def test_reminders_job_noops_when_hrt_module_is_disabled(
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    del legacy_owner_roots

    async def disabled(*args, **kwargs):
        del args, kwargs
        return {"hrt": False}

    async def must_not_refresh(*args, **kwargs):
        del args, kwargs
        raise AssertionError("disabled HRT job must not read domain state")

    monkeypatch.setattr(hrt_reminders.modules_service, "get_enabled_modules", disabled)
    monkeypatch.setattr(hrt_reminders, "refresh_all", must_not_refresh)
    await hrt_reminders.reminders_job(session_factory)


async def test_invalid_prepared_date_fails_before_target_query(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    prepared = await conflict_engine.prepare_scoped_write(
        db_session,
        context=conflict_engine.ConflictWriteContext(
            identity=identity,
            evaluation_date=date(2026, 8, 19),
        ),
    )
    reads = 0

    async def probe(*args, **kwargs):
        nonlocal reads
        reads += 1
        raise AssertionError("target must not be queried")

    monkeypatch.setattr(hrt_service, "_owned_row_for_update", probe)
    with pytest.raises(conflict_engine.ConflictPreparedWriteError, match="date"):
        await hrt_service.update_dose(
            db_session,
            1,
            compound_key="oxandrolone",
            on_date=TODAY,
            dose=10,
            identity=identity,
            prepared_conflict_write=prepared,
        )
    assert reads == 0
