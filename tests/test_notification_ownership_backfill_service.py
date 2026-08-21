"""Focused SQLite/PostgreSQL contracts for Stage-3S notification ownership."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.raw_payload import RawPayload
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.proactive import Notification
from vitals.models.tenancy import IntegrationConnection
from vitals.services import notification_ownership_backfill_service as service
from vitals.services.body_scan_metric_ownership_backfill_service import (
    BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.body_scan_ownership_backfill_service import (
    BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.day_context_ownership_backfill_service import (
    DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.garmin_weight_export_ownership_backfill_service import (
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.genetic_variant_ownership_backfill_service import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.hevy_child_ownership_backfill_service import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.hrt_compound_ownership_backfill_service import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.lab_result_ownership_backfill_service import (
    LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.services.progress_photo_ownership_backfill_service import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.raw_ownership_backfill_service import RAW_OWNERSHIP_BACKFILL_PHASE
from vitals.services.shared_report_ownership_backfill_service import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.signal_ownership_backfill_service import (
    SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.tenancy_bootstrap import LEGACY_ACCOUNT_DISCRIMINATOR
from vitals.services.weekly_digest_ownership_backfill_service import (
    WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.weight_log_ownership_backfill_service import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)


_EMPTY = hashlib.sha256(b"").hexdigest()
_STAMP = datetime(2020, 1, 1, tzinfo=UTC)
_SENT = datetime(2026, 1, 2, 8, 0)
_PRIOR_PHASES = (
    (RAW_OWNERSHIP_BACKFILL_PHASE,)
    + tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
    + tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
)


def _checkpoint(phase: str, subject_id: uuid.UUID) -> OwnershipBackfillCheckpoint:
    return OwnershipBackfillCheckpoint(
        phase_key=phase,
        subject_id=subject_id,
        status="completed",
        scan_high_watermark_id=0,
        snapshot_rows=0,
        last_scanned_id=0,
        scanned_rows=0,
        updated_rows=0,
        unchanged_rows=0,
        data_checksum_before=_EMPTY,
        data_checksum_after=_EMPTY,
        ownership_checksum_after=_EMPTY,
        started_at=_STAMP,
        updated_at=_STAMP,
        completed_at=_STAMP,
    )


async def _ready(session, roots):
    rows = [_checkpoint(phase, roots.subject_id) for phase in _PRIOR_PHASES]
    session.add_all(rows)
    await session.flush()
    return {row.phase_key: row for row in rows}


async def _recipient(session, roots):
    row = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.RECIPIENT.value,
        )
    )
    assert row is not None
    assert row.external_account_discriminator == LEGACY_ACCOUNT_DISCRIMINATOR
    return row


async def _extra_recipient(session, roots):
    row = IntegrationConnection(
        subject_id=roots.subject_id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator=f"rotated:{uuid.uuid4()}",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(row)
    await session.flush()
    return row


def _notification(
    *,
    category="brief",
    dedupe_key=None,
    subject_id=None,
    actor_user_id=None,
    recipient_user_id=None,
    integration_connection_id=None,
    ai_invocation_id=None,
    external_id=None,
    sent_at=_SENT,
    channel=IntegrationProvider.TELEGRAM.value,
):
    return Notification(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        recipient_user_id=recipient_user_id,
        integration_connection_id=integration_connection_id,
        ai_invocation_id=ai_invocation_id,
        sent_at=sent_at,
        category=category,
        dedupe_key=dedupe_key,
        channel=channel,
        external_id=external_id,
        payload={"text": "synthetic"},
    )


async def _inbound_raw(session, roots, recipient):
    row = RawPayload(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        integration_connection_id=recipient.id,
        domain=Domain.SIGNALS.value,
        source=Source.TELEGRAM.value,
        external_id=uuid.uuid4().hex,
        payload={"text": "synthetic"},
    )
    session.add(row)
    await session.flush()
    return row


def _invocation(
    *, roots, gateway_root, raw_payload_id, status=AIInvocationStatus.SUCCEEDED
):
    return AIInvocation(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        raw_payload_id=raw_payload_id,
        platform_integration_connection_id=gateway_root.id,
        purpose=AIInvocationPurpose.QUESTION_REPLY.value,
        source=AIInvocationSource.TELEGRAM.value,
        model="synthetic/reply",
        config_version=gateway_root.config_version,
        idempotency_key=uuid.uuid4().hex,
        quota_period_start=date(2020, 1, 1),
        quota_period_end=date(2100, 1, 1),
        reserved_cost_microunits=100,
        reserved_units=500,
        charged_cost_microunits=100,
        charged_units=500,
        status=status.value,
        started_at=_STAMP,
        finished_at=_STAMP,
    )


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_notification_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3S did not complete")


def test_public_contract_is_fixed():
    assert service.NOTIFICATION_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.delivery_artifact.notifications.v1"
    )
    assert service.NOTIFICATION_OWNERSHIP_BACKFILL_TABLES == ("notifications",)
    assert [
        item.value for item in service.NotificationOwnershipBackfillStatus
    ] == ["not_started", "running", "completed"]


@pytest.mark.asyncio
async def test_history_gains_subject_recipient_and_channel(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    recipient = await _recipient(db_session, legacy_owner_roots)
    row = _notification(dedupe_key="brief:2026-01-02", external_id="4242")
    db_session.add(row)
    await db_session.flush()
    original = (
        row.sent_at,
        row.category,
        row.dedupe_key,
        row.channel,
        row.external_id,
        row.payload,
    )

    result = await _finish(db_session)
    await db_session.refresh(row)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.recipient_user_id == legacy_owner_roots.user_id
    assert row.integration_connection_id == recipient.id
    # The originating actor is never invented for a delivered message.
    assert row.actor_user_id is None
    assert (
        row.sent_at,
        row.category,
        row.dedupe_key,
        row.channel,
        row.external_id,
        row.payload,
    ) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "rotated_recipient",
        "partial_roots",
        "owned_without_recipient",
        "unsupported_category",
        "unsupported_channel",
        "unsucceeded_ai_reply",
    ],
)
async def test_malformed_history_fails_closed(
    db_session, legacy_owner_roots, platform_ai_ready, case
):
    await _ready(db_session, legacy_owner_roots)
    recipient = await _recipient(db_session, legacy_owner_roots)
    if case == "rotated_recipient":
        await _extra_recipient(db_session, legacy_owner_roots)
        db_session.add(_notification())
    elif case == "partial_roots":
        db_session.add(
            _notification(subject_id=legacy_owner_roots.subject_id)
        )
    elif case == "owned_without_recipient":
        db_session.add(
            _notification(
                subject_id=legacy_owner_roots.subject_id,
                integration_connection_id=recipient.id,
            )
        )
    elif case == "unsupported_category":
        db_session.add(_notification(category="telegram_broadcast"))
    elif case == "unsupported_channel":
        db_session.add(_notification(channel="email"))
    else:
        # A reply may not point at a paid call that never succeeded.
        raw = await _inbound_raw(db_session, legacy_owner_roots, recipient)
        invocation = _invocation(
            roots=legacy_owner_roots,
            gateway_root=platform_ai_ready,
            raw_payload_id=raw.id,
            status=AIInvocationStatus.FAILED,
        )
        db_session.add(invocation)
        await db_session.flush()
        db_session.add(
            _notification(
                category="reply",
                subject_id=legacy_owner_roots.subject_id,
                recipient_user_id=legacy_owner_roots.user_id,
                integration_connection_id=recipient.id,
                ai_invocation_id=invocation.id,
                external_id="99",
            )
        )
    await db_session.flush()

    with pytest.raises(service.NotificationOwnershipBackfillError):
        await service.preflight_notification_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_owned_rows_and_ai_replies_stay_valid(
    db_session, legacy_owner_roots, platform_ai_ready
):
    await _ready(db_session, legacy_owner_roots)
    recipient = await _recipient(db_session, legacy_owner_roots)
    db_session.add(_notification())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    raw = await _inbound_raw(db_session, legacy_owner_roots, recipient)
    invocation = _invocation(
        roots=legacy_owner_roots,
        gateway_root=platform_ai_ready,
        raw_payload_id=raw.id,
    )
    db_session.add(invocation)
    await db_session.flush()
    db_session.add(
        _notification(
            category="reply",
            sent_at=datetime(2026, 1, 3, 8, 0),
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
            recipient_user_id=legacy_owner_roots.user_id,
            integration_connection_id=recipient.id,
            ai_invocation_id=invocation.id,
            external_id="777",
        )
    )
    await db_session.flush()
    result = await service.preflight_notification_ownership_backfill(db_session)
    assert result.completed and result.rows_above_high_watermark == 1


@pytest.mark.asyncio
async def test_live_tail_requires_the_delivery_graph(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    await _recipient(db_session, legacy_owner_roots)
    db_session.add(_notification())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(_notification(sent_at=datetime(2026, 1, 4, 8, 0)))
    await db_session.flush()
    with pytest.raises(service.NotificationOwnershipBackfillStateError):
        await service.preflight_notification_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_evidence_is_stable(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    await _recipient(db_session, legacy_owner_roots)
    for offset in range(5):
        db_session.add(
            _notification(
                sent_at=datetime(2026, 2, 1 + offset, 8, 0),
                dedupe_key=f"brief:2026-02-{offset:02d}",
            )
        )
    await db_session.flush()

    first = await service.run_notification_ownership_backfill_batch(
        db_session, batch_size=2
    )
    assert not first.completed and first.batch_updated_rows == 2
    final = await _finish(db_session, batch_size=2)
    assert final.completed and final.updated_rows == 5
    repeat = await service.run_notification_ownership_backfill_batch(db_session)
    assert repeat.completed and repeat.batch_scanned_rows == 0
    assert repeat.ownership_checksum_after == final.ownership_checksum_after


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_are_enforced(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.NotificationOwnershipBackfillDependencyError):
        await service.preflight_notification_ownership_backfill(db_session)
    await _ready(db_session, legacy_owner_roots)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weekly_digests"]
        )
    )
    with pytest.raises(service.NotificationOwnershipBackfillDependencyError):
        await service.preflight_notification_ownership_backfill(db_session)
    db_session.add(
        _checkpoint(
            WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weekly_digests"],
            legacy_owner_roots.subject_id,
        )
    )
    await db_session.flush()
    for size in (0, 1001, True, "250"):
        with pytest.raises(service.NotificationOwnershipBackfillValidationError):
            await service.run_notification_ownership_backfill_batch(
                db_session, batch_size=size
            )


@pytest.mark.asyncio
async def test_retained_restore_preparation_freezes_the_delivery_log(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    await _recipient(db_session, legacy_owner_roots)
    row = _notification()
    db_session.add(row)
    await db_session.flush()

    await service.prepare_notification_ownership_backfill_for_portability_v1_restore(
        db_session
    )
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "notifications"
            ]
        )
    )
    assert checkpoint.status == "running"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (
        row.id,
        1,
    )

    # Preparing again preserves the retained checkpoint instead of rebasing it.
    await service.prepare_notification_ownership_backfill_for_portability_v1_restore(
        db_session
    )
    await db_session.refresh(checkpoint)
    assert checkpoint.status == "running" and checkpoint.snapshot_rows == 1

    assert (await _finish(db_session)).completed
