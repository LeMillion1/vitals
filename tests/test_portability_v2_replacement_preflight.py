"""Control-state preparation for a portability-v2 replace transaction."""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    Severity,
    Source,
    SupportAccessMode,
    SupportAccessStatus,
    SupportRepairStatus,
    UserStatus,
)
from vitals.models.ai import AIInvocation
from vitals.models.garmin import (
    WEIGHT_EXPORT_CHECKING,
    WEIGHT_EXPORT_CONFLICT,
    WEIGHT_EXPORT_DELETED,
    WEIGHT_EXPORT_DELETE_CHECKING,
    WEIGHT_EXPORT_DELETE_FAILED,
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_MATCHED,
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_SENT,
    WEIGHT_EXPORT_SKIPPED,
    WEIGHT_EXPORT_UNVERIFIED,
    GarminWeightExport,
)
from vitals.models.identity import HealthSubject, SupportAccessGrant, User
from vitals.models.proactive import NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.support_repair import SupportRepairAction
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.models.weight import BodyMeasurement
from vitals.services.portability.replacement_preflight import (
    ReplacementPreflightError,
    prepare_replacement_preflight,
)


NOW = datetime(2026, 8, 25, 10, tzinfo=UTC)
PERIOD_START = date(2020, 1, 1)
PERIOD_END = date(2100, 1, 1)


async def _connection_id(db_session, subject_id, provider):
    value = await db_session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == provider,
        )
    )
    assert value is not None
    return value


async def _other_subject(db_session, suffix: str):
    owner = User(
        username=f"preflight-other-{suffix}",
        normalized_username=f"preflight-other-{suffix}",
        password_hash="$synthetic-preflight",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(owner)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name="Synthetic preflight other",
        timezone="UTC",
    )
    db_session.add(subject)
    await db_session.flush()
    return owner, subject


def _alert(*, subject_id, key: str, resolved: bool = False):
    return SystemAlert(
        subject_id=subject_id,
        domain=Domain.WEIGHT.value,
        severity=Severity.WARN.value,
        message="synthetic control alert",
        alert_key=key,
        entity_ref="synthetic",
        resolved_at=NOW.replace(tzinfo=None) if resolved else None,
    )


async def _repair_roots(db_session, roots):
    grantee = User(
        username=f"preflight-support-{uuid.uuid4().hex}",
        normalized_username=f"preflight-support-{uuid.uuid4().hex}",
        password_hash="$synthetic-preflight",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(grantee)
    await db_session.flush()
    grant = SupportAccessGrant(
        subject_id=roots.subject_id,
        granted_to_user_id=grantee.id,
        approved_by_user_id=roots.user_id,
        mode=SupportAccessMode.REPAIR.value,
        status=SupportAccessStatus.ACTIVE.value,
        reason="Synthetic replacement preflight grant",
        approved_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    measurement = BodyMeasurement(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        date=date(2026, 8, 20),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        waist_cm=90.0,
        body_fat_pct=18.0,
    )
    db_session.add_all((grant, measurement))
    await db_session.flush()
    return grantee, grant, measurement


def _repair_action(*, roots, grantee, grant, measurement, status):
    reviewed = status != SupportRepairStatus.PROPOSED
    executed = status in {SupportRepairStatus.EXECUTED, SupportRepairStatus.REVERTED}
    reverted = status == SupportRepairStatus.REVERTED
    return SupportRepairAction(
        subject_id=roots.subject_id,
        support_access_grant_id=grant.id,
        proposed_by_user_id=grantee.id,
        target_body_measurement_id=measurement.id,
        target_date=measurement.date,
        status=status.value,
        idempotency_key=uuid.uuid4(),
        proposed_at=NOW,
        execute_before=NOW + timedelta(hours=1),
        before_body_fat_pct=18.0,
        target_updated_at_at_proposal=measurement.updated_at,
        reviewed_by_user_id=roots.user_id if reviewed else None,
        reviewed_at=NOW + timedelta(minutes=1) if reviewed else None,
        executed_by_user_id=grantee.id if executed else None,
        executed_at=NOW + timedelta(minutes=2) if executed else None,
        target_updated_at_after_execute=(measurement.updated_at if executed else None),
        reverted_by_user_id=roots.user_id if reverted else None,
        reverted_at=NOW + timedelta(minutes=3) if reverted else None,
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("subject_id", "subject", "replacement_subject_invalid"),
        ("subject_id", uuid.UUID(int=0), "replacement_subject_invalid"),
        ("actor_user_id", None, "replacement_actor_invalid"),
        ("actor_user_id", uuid.UUID(int=0), "replacement_actor_invalid"),
    ],
)
async def test_invalid_identifiers_fail_with_stable_phi_free_codes(
    db_session,
    legacy_owner_roots,
    field,
    value,
    code,
):
    values = {
        "subject_id": legacy_owner_roots.subject_id,
        "actor_user_id": legacy_owner_roots.user_id,
    }
    values[field] = value

    with pytest.raises(ReplacementPreflightError) as raised:
        await prepare_replacement_preflight(db_session, **values)

    assert raised.value.code == code
    assert str(legacy_owner_roots.subject_id) not in str(raised.value)


async def test_missing_subject_and_actor_fail_closed(db_session, legacy_owner_roots):
    with pytest.raises(ReplacementPreflightError) as missing_subject:
        await prepare_replacement_preflight(
            db_session,
            subject_id=uuid.uuid4(),
            actor_user_id=legacy_owner_roots.user_id,
        )
    assert missing_subject.value.code == "replacement_subject_not_found"

    with pytest.raises(ReplacementPreflightError) as missing_actor:
        await prepare_replacement_preflight(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=uuid.uuid4(),
        )
    assert missing_actor.value.code == "replacement_actor_not_found"


@pytest.mark.parametrize(
    "status",
    [
        WEIGHT_EXPORT_PENDING,
        WEIGHT_EXPORT_CHECKING,
        WEIGHT_EXPORT_SENT,
        WEIGHT_EXPORT_FAILED,
        WEIGHT_EXPORT_CONFLICT,
        WEIGHT_EXPORT_UNVERIFIED,
        WEIGHT_EXPORT_DELETE_PENDING,
        WEIGHT_EXPORT_DELETE_CHECKING,
        WEIGHT_EXPORT_DELETE_FAILED,
        "future_state",
    ],
)
async def test_every_nonterminal_or_unknown_garmin_export_blocks_replace(
    db_session,
    legacy_owner_roots,
    status,
):
    connection_id = await _connection_id(
        db_session,
        legacy_owner_roots.subject_id,
        "garmin",
    )
    db_session.add(
        GarminWeightExport(
            subject_id=legacy_owner_roots.subject_id,
            integration_connection_id=connection_id,
            date=date(2026, 8, 20),
            weight_kg=80.0,
            measured_at=NOW.replace(tzinfo=None),
            status=status,
        )
    )
    await db_session.commit()

    with pytest.raises(ReplacementPreflightError) as raised:
        await prepare_replacement_preflight(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
        )

    assert raised.value.code == "replacement_garmin_export_nonterminal"


async def test_only_terminal_garmin_history_is_accepted_and_preserved(
    db_session,
    legacy_owner_roots,
):
    connection_id = await _connection_id(
        db_session,
        legacy_owner_roots.subject_id,
        "garmin",
    )
    rows = [
        GarminWeightExport(
            subject_id=legacy_owner_roots.subject_id,
            integration_connection_id=connection_id,
            date=date(2026, 8, 20 + offset),
            weight_kg=80.0,
            measured_at=NOW.replace(tzinfo=None),
            status=status,
        )
        for offset, status in enumerate(
            (WEIGHT_EXPORT_MATCHED, WEIGHT_EXPORT_SKIPPED, WEIGHT_EXPORT_DELETED)
        )
    ]
    db_session.add_all(rows)
    await db_session.commit()

    plan = await prepare_replacement_preflight(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )

    assert plan.preserved_terminal_garmin_export_ids == tuple(row.id for row in rows)
    assert [row.status for row in rows] == [
        WEIGHT_EXPORT_MATCHED,
        WEIGHT_EXPORT_SKIPPED,
        WEIGHT_EXPORT_DELETED,
    ]


@pytest.mark.parametrize(
    "status",
    (SupportRepairStatus.PROPOSED, SupportRepairStatus.APPROVED),
)
async def test_open_support_repairs_block_without_detaching_or_resolving(
    db_session,
    legacy_owner_roots,
    status,
):
    grantee, grant, measurement = await _repair_roots(db_session, legacy_owner_roots)
    action = _repair_action(
        roots=legacy_owner_roots,
        grantee=grantee,
        grant=grant,
        measurement=measurement,
        status=status,
    )
    alert = _alert(
        subject_id=legacy_owner_roots.subject_id,
        key=f"replacement.open-repair.{status.value}",
    )
    db_session.add_all((action, alert))
    await db_session.commit()

    with pytest.raises(ReplacementPreflightError) as raised:
        await prepare_replacement_preflight(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
        )

    assert raised.value.code == "replacement_support_repair_open"
    assert action.target_body_measurement_id == measurement.id
    assert alert.resolved_at is None


async def test_terminal_repairs_are_detached_and_outer_rollback_restores_targets(
    db_session,
    legacy_owner_roots,
):
    grantee, grant, measurement = await _repair_roots(db_session, legacy_owner_roots)
    actions = [
        _repair_action(
            roots=legacy_owner_roots,
            grantee=grantee,
            grant=grant,
            measurement=measurement,
            status=status,
        )
        for status in (
            SupportRepairStatus.DECLINED,
            SupportRepairStatus.EXECUTED,
            SupportRepairStatus.STALE,
            SupportRepairStatus.REVERTED,
        )
    ]
    db_session.add_all(actions)
    await db_session.commit()
    action_ids = tuple(sorted((row.id for row in actions), key=str))
    measurement_id = measurement.id

    plan = await prepare_replacement_preflight(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )

    assert plan.preserved_terminal_repair_action_ids == action_ids
    assert plan.detached_terminal_repair_count == len(actions)
    assert all(row.target_body_measurement_id is None for row in actions)

    repeated = await prepare_replacement_preflight(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )
    assert repeated.preserved_terminal_repair_action_ids == action_ids
    assert repeated.detached_terminal_repair_count == 0

    await db_session.rollback()
    restored = tuple(
        await db_session.scalars(
            select(SupportRepairAction)
            .where(SupportRepairAction.id.in_(action_ids))
            .order_by(SupportRepairAction.id)
        )
    )
    assert all(row.target_body_measurement_id == measurement_id for row in restored)


async def test_retention_alert_resolution_and_report_are_scoped_flush_only(
    db_session,
    legacy_owner_roots,
    platform_ai_ready,
):
    telegram_id = await _connection_id(
        db_session,
        legacy_owner_roots.subject_id,
        "telegram",
    )
    raw_ai = RawPayload(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=telegram_id,
        domain="signals",
        source=Source.TELEGRAM.value,
        external_id="replacement-ai",
        payload={"synthetic": True},
    )
    raw_delivery = RawPayload(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=telegram_id,
        domain="signals",
        source=Source.TELEGRAM.value,
        external_id="replacement-delivery",
        payload={"synthetic": True},
    )
    raw_unreferenced = RawPayload(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=telegram_id,
        domain="signals",
        source=Source.TELEGRAM.value,
        external_id="replacement-unreferenced",
        payload={"synthetic": True},
    )
    db_session.add_all((raw_ai, raw_delivery, raw_unreferenced))
    await db_session.flush()
    invocation = AIInvocation(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        raw_payload_id=raw_ai.id,
        platform_integration_connection_id=platform_ai_ready.id,
        purpose=AIInvocationPurpose.QUESTION_REPLY.value,
        source=AIInvocationSource.TELEGRAM.value,
        model="synthetic/preflight",
        config_version=platform_ai_ready.config_version,
        idempotency_key="replacement-preflight",
        quota_period_start=PERIOD_START,
        quota_period_end=PERIOD_END,
        reserved_cost_microunits=1,
        reserved_units=1,
        status=AIInvocationStatus.PREPARED.value,
    )
    intent = NotificationDeliveryIntent(
        subject_id=legacy_owner_roots.subject_id,
        recipient_user_id=legacy_owner_roots.user_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=telegram_id,
        raw_payload_id=raw_delivery.id,
        category="reply",
        channel="telegram",
        idempotency_key="d" * 64,
        policy_at=NOW,
        policy_date=NOW.date(),
        status="pending",
    )
    _, foreign_subject = await _other_subject(db_session, "scope")
    active = [
        _alert(
            subject_id=legacy_owner_roots.subject_id,
            key=f"replacement.active.{index}",
        )
        for index in range(2)
    ]
    already_resolved = _alert(
        subject_id=legacy_owner_roots.subject_id,
        key="replacement.resolved",
        resolved=True,
    )
    foreign = _alert(
        subject_id=foreign_subject.id,
        key="replacement.foreign",
    )
    platform = _alert(subject_id=None, key="replacement.platform")
    db_session.add_all((invocation, intent, *active, already_resolved, foreign, platform))
    await db_session.commit()
    retained_ids = tuple(sorted((raw_ai.id, raw_delivery.id)))
    active_ids = tuple(row.id for row in active)
    unresolved_id = raw_unreferenced.id

    plan = await prepare_replacement_preflight(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )

    assert plan.retained_raw_payload_ids == retained_ids
    assert plan.resolved_system_alert_ids == active_ids
    assert all(row.resolved_at == plan.database_now for row in active)
    assert all(row.resolved_by_user_id == legacy_owner_roots.user_id for row in active)
    assert already_resolved.resolved_at == NOW.replace(tzinfo=None)
    assert foreign.resolved_at is None
    assert platform.resolved_at is None
    with pytest.raises(FrozenInstanceError):
        plan.detached_terminal_repair_count = 9

    await db_session.execute(
        delete(RawPayload).where(
            RawPayload.id.in_((*retained_ids, unresolved_id)),
            RawPayload.id.not_in(plan.retained_raw_payload_ids),
        )
    )
    assert await db_session.get(RawPayload, raw_ai.id) is not None
    assert await db_session.get(RawPayload, raw_delivery.id) is not None
    assert await db_session.get(RawPayload, unresolved_id) is None

    await db_session.rollback()
    restored_alerts = tuple(
        await db_session.scalars(
            select(SystemAlert).where(SystemAlert.id.in_(active_ids)).order_by(SystemAlert.id)
        )
    )
    assert all(row.resolved_at is None for row in restored_alerts)
    assert await db_session.get(RawPayload, unresolved_id) is not None
