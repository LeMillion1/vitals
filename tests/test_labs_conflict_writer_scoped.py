"""Focused ownership and prepared-writer contract for Labs."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from vitals.enums import Domain, RuleType, Severity, Source
from vitals.models.conflict_rule import ConflictRule
from vitals.models.labs import LabMarker, LabResult
from vitals.models.raw_payload import RawPayload
from vitals.models.system_alert import SystemAlert
from vitals.ownership import WriteIdentity
from vitals.services import alerts_service, conflict_engine, labs_service


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader or writer does when the ownership backfill has not reached a row yet,
# which is a state the application itself can no longer create. The schema
# says so, so this module asks for the one that stood before the contract.
pytestmark = pytest.mark.pre_ownership_contract


RESULT_DATE = date(2026, 8, 1)
TODAY = date(2026, 8, 20)


def _identity(legacy_owner_roots) -> WriteIdentity:
    return WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )


def _context(
    identity: WriteIdentity,
    *,
    on_date: date = RESULT_DATE,
    legacy: bool = False,
) -> conflict_engine.ConflictWriteContext:
    return conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )


async def _prepared(db_session, context):
    return await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )


async def _blocking_rule(db_session, identity: WriteIdentity) -> ConflictRule:
    row = ConflictRule(
        subject_id=identity.subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.LABS.value,
        condition_a={"marker": "Synthetic risk"},
        domain_b=Domain.SUPPLEMENTS.value,
        condition_b={"key": "synthetic", "active": True},
        severity=Severity.BLOCK.value,
        message="Synthetic lab blocker.",
        active=True,
    )
    db_session.add(row)
    await db_session.commit()

    async def supplements(session, *, scope):
        del session, scope
        return [{"key": "synthetic", "active": True}]

    conflict_engine.register_domain_resolver(Domain.SUPPLEMENTS.value, supplements)
    conflict_engine.register_domain_resolver(
        Domain.LABS.value,
        labs_service.resolve_latest_scoped,
    )
    return row


async def test_scoped_add_blocks_before_marker_mutation_and_attributes_override(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    rule = await _blocking_rule(db_session, identity)
    prepared = await _prepared(db_session, _context(identity))

    with pytest.raises(conflict_engine.ConflictBlocked):
        await labs_service.add_result(
            db_session,
            on_date=RESULT_DATE,
            marker="synthetic risk",
            value=9,
            identity=identity,
            prepared_conflict_write=prepared,
        )

    assert await db_session.scalar(select(func.count()).select_from(LabResult)) == 0
    assert await db_session.scalar(select(func.count()).select_from(LabMarker)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0

    result = await labs_service.add_result(
        db_session,
        on_date=RESULT_DATE,
        marker="synthetic risk",
        value=9,
        source=Source.MCP.value,
        override=True,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    assert (result.subject_id, result.actor_user_id, result.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MCP.value,
    )
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (
        identity.subject_id,
        None,
    )
    assert alert.overridden_by_user_id == identity.actor_user_id


async def test_invalid_capability_fails_before_target_lock(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, _context(identity))
    calls = 0

    async def target_probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("target row must not be locked")

    monkeypatch.setattr(labs_service, "_get_result_for_update", target_probe)
    mismatched = WriteIdentity(identity.subject_id, uuid.uuid4())
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await labs_service.update_result(
            db_session,
            1,
            value=4,
            identity=mismatched,
            prepared_conflict_write=prepared,
        )
    # The signature now refuses a write with no capability at all, so what is
    # left to prove at runtime is a capability that does not match the writer.
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await labs_service.delete_result(
            db_session,
            1,
            identity=mismatched,
            prepared_conflict_write=prepared,
            subject_id=mismatched.subject_id,
        )
    assert calls == 0


async def test_update_replaces_exact_result_and_preserves_origin(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    identity = _identity(legacy_owner_roots)
    raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=Domain.LABS.value,
        source=Source.MCP.value,
        external_id="synthetic-mcp-lab",
        payload={"results": []},
    )
    marker = LabMarker(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=Domain.LABS.value,
        name="Ferritin",
    )
    row = LabResult(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        raw_payload_id=None,
        date=RESULT_DATE,
        domain=Domain.LABS.value,
        source=Source.MCP.value,
        marker="Ferritin",
        value=50,
        note="old",
    )
    db_session.add_all([raw, marker, row])
    await db_session.flush()
    row.raw_payload_id = raw.id
    await db_session.commit()
    prepared = await _prepared(db_session, _context(identity))
    captured = {}

    async def enforce_probe(session, **kwargs):
        del session
        captured.update(kwargs)
        return []

    monkeypatch.setattr(conflict_engine, "enforce_prepared", enforce_probe)
    updated = await labs_service.update_result(
        db_session,
        row.id,
        value=55,
        note="corrected",
        identity=identity,
        prepared_conflict_write=prepared,
    )

    assert updated is row
    assert captured["replace_entity_key"] == str(row.id)
    assert captured["proposed_state"][conflict_engine.CONFLICT_ENTITY_KEY] == str(
        row.id
    )
    assert (row.actor_user_id, row.source, row.raw_payload_id) == (
        identity.actor_user_id,
        Source.MCP.value,
        raw.id,
    )


async def test_structured_mcp_batch_requires_exact_mcp_raw_roots(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=Domain.LABS.value,
        source=Source.MCP.value,
        external_id="mcp-panel",
        payload={"results": [{"marker": "TSH", "value": 2.1}]},
    )
    db_session.add(raw)
    await db_session.flush()
    prepared = await _prepared(db_session, _context(identity))
    summary = await labs_service.ingest_structured_results(
        db_session,
        {"date": RESULT_DATE.isoformat(), "results": [{"marker": "TSH", "value": 2.1}]},
        raw_payload=raw,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    result = summary["results"][0]
    assert (result.source, result.raw_payload_id) == (Source.MCP.value, raw.id)
    assert raw.processed_at is not None

    parser_raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        external_id="not-mcp",
        payload={"results": []},
    )
    db_session.add(parser_raw)
    await db_session.flush()
    with pytest.raises(conflict_engine.ConflictRawOwnershipError, match="source"):
        await labs_service.ingest_structured_results(
            db_session,
            {"date": RESULT_DATE.isoformat(), "results": []},
            raw_payload=parser_raw,
            identity=identity,
            prepared_conflict_write=prepared,
        )


async def test_scoped_alerts_are_actorless_but_defer_is_human_attributed(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    add_prepared = await _prepared(db_session, _context(identity))
    row = await labs_service.add_result(
        db_session,
        on_date=RESULT_DATE,
        marker="Ferritin",
        value=700,
        ref_low=30,
        ref_high=400,
        identity=identity,
        prepared_conflict_write=add_prepared,
    )
    marker = await labs_service.get_marker(
        db_session,
        "Ferritin",
        subject_id=identity.subject_id,
    )
    assert marker is not None
    marker.retest_interval_days = 5
    await db_session.commit()

    refresh_prepared = await _prepared(db_session, _context(identity, on_date=TODAY))
    await labs_service.refresh_alerts(
        db_session,
        identity=identity,
        prepared_conflict_write=refresh_prepared,
        subject_id=identity.subject_id,
    )
    alerts = list(
        await db_session.scalars(
            select(SystemAlert).where(SystemAlert.subject_id == identity.subject_id)
        )
    )
    assert {alert.alert_key for alert in alerts} == {
        labs_service.OUT_OF_RANGE_KEY,
        labs_service.RETEST_DUE_KEY,
    }
    assert all(alert.resolved_by_user_id is None for alert in alerts)

    outlier = next(
        alert for alert in alerts if alert.alert_key == labs_service.OUT_OF_RANGE_KEY
    )
    await alerts_service.resolve_scoped_alert(
        db_session,
        outlier.id,
        context=alerts_service.HealthAlertContext(identity),
    )
    await labs_service.refresh_alerts(
        db_session,
        identity=identity,
        prepared_conflict_write=refresh_prepared,
        subject_id=identity.subject_id,
    )
    retest = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == labs_service.RETEST_DUE_KEY,
            SystemAlert.entity_ref == f"Ferritin:{row.id}",
            SystemAlert.resolved_at.is_(None),
        )
    )
    assert retest is not None

    await labs_service.defer_retest(
        db_session,
        "Ferritin",
        until=TODAY + timedelta(days=30),
        identity=identity,
        prepared_conflict_write=refresh_prepared,
        subject_id=identity.subject_id,
    )
    assert retest.resolved_at is not None
    assert retest.resolved_by_user_id == identity.actor_user_id


async def test_scoped_list_rejects_unowned_raw_without_bridge(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    foreign_raw = RawPayload(
        domain=Domain.LABS.value,
        source=Source.MCP.value,
        external_id="foreign",
        payload={},
    )
    db_session.add(foreign_raw)
    await db_session.flush()
    db_session.add(
        LabResult(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            raw_payload_id=foreign_raw.id,
            date=RESULT_DATE,
            domain=Domain.LABS.value,
            source=Source.MCP.value,
            marker="Unsafe",
            value=1,
        )
    )
    await db_session.flush()

    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await labs_service.list_results(
            db_session,
            subject_id=identity.subject_id,
        )


async def test_owned_reparse_requires_boundary_and_preserves_legacy_actor(
    db_session,
    legacy_owner_roots,
):
    raw = RawPayload(
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        external_id="legacy-panel",
        payload={
            "date": RESULT_DATE.isoformat(),
            "results": [{"marker": "Legacy marker", "value": 3.0}],
        },
    )
    db_session.add(raw)
    await db_session.flush()
    boundary_identity = WriteIdentity(legacy_owner_roots.subject_id, None)
    boundary = _context(boundary_identity, on_date=TODAY, legacy=True)
    prepared = await _prepared(db_session, boundary)

    assert await labs_service.reparse_owned_pending(
        db_session,
        identity=boundary_identity,
        prepared_conflict_write=prepared,
    ) == 1
    result = await db_session.scalar(
        select(LabResult).where(LabResult.raw_payload_id == raw.id)
    )
    assert result is not None
    assert (raw.subject_id, result.subject_id, result.actor_user_id) == (
        None,
        legacy_owner_roots.subject_id,
        None,
    )
    updated = await labs_service.update_result_note(
        db_session,
        result.id,
        note="legacy provenance remains writable through the bridge",
        identity=WriteIdentity(legacy_owner_roots.subject_id, None),
        prepared_conflict_write=prepared,
    )
    assert updated is result


async def test_owned_reparse_does_not_duplicate_any_existing_normalized_fact(
    db_session,
    legacy_owner_roots,
):
    raw = RawPayload(
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        external_id="legacy-already-normalized",
        payload={
            "date": RESULT_DATE.isoformat(),
            "results": [{"marker": "Existing marker", "value": 4.0}],
        },
    )
    db_session.add(raw)
    await db_session.flush()
    existing = LabResult(
        raw_payload_id=raw.id,
        date=RESULT_DATE,
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        marker="Existing marker",
        value=4.0,
    )
    db_session.add(existing)
    await db_session.flush()
    boundary_identity = WriteIdentity(legacy_owner_roots.subject_id, None)
    prepared = await _prepared(
        db_session,
        _context(boundary_identity, on_date=TODAY, legacy=True),
    )

    assert await labs_service.reparse_owned_pending(
        db_session,
        identity=boundary_identity,
        prepared_conflict_write=prepared,
    ) == 0
    rows = list(
        await db_session.scalars(
            select(LabResult).where(LabResult.raw_payload_id == raw.id)
        )
    )
    assert rows == [existing]


async def test_owned_reparse_rejects_partial_normalized_provenance(
    db_session,
    legacy_owner_roots,
):
    raw = RawPayload(
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        external_id="legacy-partial-normalized",
        payload={
            "date": RESULT_DATE.isoformat(),
            "results": [{"marker": "Partial marker", "value": 5.0}],
        },
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        LabResult(
            actor_user_id=legacy_owner_roots.user_id,
            raw_payload_id=raw.id,
            date=RESULT_DATE,
            domain=Domain.LABS.value,
            source=Source.LAB_PARSER.value,
            marker="Partial marker",
            value=5.0,
        )
    )
    await db_session.flush()
    boundary_identity = WriteIdentity(legacy_owner_roots.subject_id, None)
    prepared = await _prepared(
        db_session,
        _context(boundary_identity, on_date=TODAY, legacy=True),
    )

    with pytest.raises(
        conflict_engine.ConflictRawOwnershipError,
        match="partial normalized provenance",
    ):
        await labs_service.reparse_owned_pending(
            db_session,
            identity=boundary_identity,
            prepared_conflict_write=prepared,
        )
    assert raw.processed_at is None
