"""Focused SQLite/PostgreSQL contracts for Stage-3R weekly-digest ownership."""
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
    DigestKind,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.milestones import WeeklyDigest
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.tenancy import IntegrationConnection
from vitals.services import weekly_digest_ownership_backfill_service as service
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
from vitals.services.weight_log_ownership_backfill_service import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)


# Every test here writes or inspects a row with no owner, which is the whole
# subject of the ownership backfill: these services exist to give such rows an
# owner. The application can no longer produce that state, so this module asks
# for the schema as it stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


_EMPTY = hashlib.sha256(b"").hexdigest()
_STAMP = datetime(2020, 1, 1, tzinfo=UTC)
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


async def _gateway(session, roots):
    row = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.AI_GATEWAY.value,
        )
    )
    assert row is not None
    return row


def _invocation(*, roots, gateway_root, purpose, status=AIInvocationStatus.SUCCEEDED):
    return AIInvocation(
        subject_id=roots.subject_id,
        # A scheduler invocation is a system boundary: it carries no actor.
        actor_user_id=None,
        raw_payload_id=None,
        platform_integration_connection_id=gateway_root.id,
        purpose=purpose.value,
        source=AIInvocationSource.SCHEDULER.value,
        model="synthetic/digest",
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


def _digest(
    *,
    on_date=date(2026, 1, 2),
    kind=DigestKind.WEEKLY.value,
    source=Source.SCHEDULER.value,
    subject_id=None,
    actor_user_id=None,
    integration_connection_id=None,
    ai_invocation_id=None,
):
    return WeeklyDigest(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        integration_connection_id=integration_connection_id,
        ai_invocation_id=ai_invocation_id,
        date=on_date,
        domain=Domain.MILESTONES.value,
        source=source,
        kind=kind,
        content="synthetic narrative",
        context_json={"weight": []},
        model="synthetic/digest",
    )


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_weekly_digest_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3R did not complete")


def test_public_contract_is_fixed():
    assert service.WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.retained_artifact.weekly_digests.v1"
    )
    assert service.WEEKLY_DIGEST_OWNERSHIP_BACKFILL_TABLES == ("weekly_digests",)
    assert [
        item.value for item in service.WeeklyDigestOwnershipBackfillStatus
    ] == ["not_started", "running", "completed"]


@pytest.mark.asyncio
async def test_history_gains_only_the_subject(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    row = _digest()
    db_session.add(row)
    await db_session.flush()
    original = (
        row.actor_user_id,
        row.integration_connection_id,
        row.ai_invocation_id,
        row.kind,
        row.content,
        row.context_json,
        row.model,
        row.updated_at,
    )

    result = await _finish(db_session)
    await db_session.refresh(row)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert (
        row.actor_user_id,
        row.integration_connection_id,
        row.ai_invocation_id,
        row.kind,
        row.content,
        row.context_json,
        row.model,
        row.updated_at,
    ) == original


@pytest.mark.asyncio
async def test_subject_funded_history_is_preserved(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    gateway = await _gateway(db_session, legacy_owner_roots)
    db_session.add(
        _digest(
            subject_id=legacy_owner_roots.subject_id,
            integration_connection_id=gateway.id,
        )
    )
    await db_session.flush()

    result = await _finish(db_session)
    assert result.completed and result.updated_rows == 0 and result.unchanged_rows == 1


@pytest.mark.asyncio
async def test_platform_funded_artifact_is_validated(
    db_session, legacy_owner_roots, platform_ai_ready
):
    await _ready(db_session, legacy_owner_roots)
    invocation = _invocation(
        roots=legacy_owner_roots,
        gateway_root=platform_ai_ready,
        purpose=AIInvocationPurpose.WEEKLY_DIGEST,
    )
    db_session.add(invocation)
    await db_session.flush()
    db_session.add(
        _digest(
            subject_id=legacy_owner_roots.subject_id,
            ai_invocation_id=invocation.id,
        )
    )
    await db_session.flush()

    result = await _finish(db_session)
    assert result.completed and result.unchanged_rows == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "partial_roots",
        "unknown_kind",
        "empty_content",
        "unsupported_source",
        "wrong_invocation_purpose",
        "unsucceeded_invocation",
    ],
)
async def test_malformed_history_fails_closed(
    db_session, legacy_owner_roots, platform_ai_ready, case
):
    await _ready(db_session, legacy_owner_roots)
    if case == "partial_roots":
        db_session.add(_digest(actor_user_id=legacy_owner_roots.user_id))
    elif case == "unknown_kind":
        row = _digest()
        row.kind = "quarterly"
        db_session.add(row)
    elif case == "empty_content":
        row = _digest()
        row.content = "   "
        db_session.add(row)
    elif case == "unsupported_source":
        db_session.add(_digest(source=Source.TELEGRAM.value))
    else:
        purpose = (
            AIInvocationPurpose.DAILY_BRIEF
            if case == "wrong_invocation_purpose"
            else AIInvocationPurpose.WEEKLY_DIGEST
        )
        status = (
            AIInvocationStatus.FAILED
            if case == "unsucceeded_invocation"
            else AIInvocationStatus.SUCCEEDED
        )
        invocation = _invocation(
            roots=legacy_owner_roots,
            gateway_root=platform_ai_ready,
            purpose=purpose,
            status=status,
        )
        db_session.add(invocation)
        await db_session.flush()
        db_session.add(
            _digest(
                subject_id=legacy_owner_roots.subject_id,
                ai_invocation_id=invocation.id,
            )
        )
    await db_session.flush()

    with pytest.raises(service.WeeklyDigestOwnershipBackfillError):
        await service.preflight_weekly_digest_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_requires_reviewed_funding(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_digest())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(
        _digest(
            on_date=date(2026, 1, 9),
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
        )
    )
    await db_session.flush()
    with pytest.raises(service.WeeklyDigestOwnershipBackfillProvenanceError):
        await service.preflight_weekly_digest_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_evidence_is_stable(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    for offset in range(5):
        db_session.add(_digest(on_date=date(2026, 2, 1 + offset)))
    await db_session.flush()

    first = await service.run_weekly_digest_ownership_backfill_batch(
        db_session, batch_size=2
    )
    assert not first.completed and first.batch_updated_rows == 2
    final = await _finish(db_session, batch_size=2)
    assert final.completed and final.updated_rows == 5
    repeat = await service.run_weekly_digest_ownership_backfill_batch(db_session)
    assert repeat.completed and repeat.batch_scanned_rows == 0
    assert repeat.ownership_checksum_after == final.ownership_checksum_after


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_are_enforced(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.WeeklyDigestOwnershipBackfillDependencyError):
        await service.preflight_weekly_digest_ownership_backfill(db_session)
    await _ready(db_session, legacy_owner_roots)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "garmin_weight_exports"
            ]
        )
    )
    with pytest.raises(service.WeeklyDigestOwnershipBackfillDependencyError):
        await service.preflight_weekly_digest_ownership_backfill(db_session)
    db_session.add(
        _checkpoint(
            GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "garmin_weight_exports"
            ],
            legacy_owner_roots.subject_id,
        )
    )
    await db_session.flush()
    for size in (0, 1001, True, "250"):
        with pytest.raises(service.WeeklyDigestOwnershipBackfillValidationError):
            await service.run_weekly_digest_ownership_backfill_batch(
                db_session, batch_size=size
            )


@pytest.mark.asyncio
async def test_retained_restore_preparation_freezes_local_artifacts(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    row = _digest()
    db_session.add(row)
    await db_session.flush()

    await service.prepare_weekly_digest_ownership_backfill_for_portability_v1_restore(
        db_session
    )
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "weekly_digests"
            ]
        )
    )
    assert checkpoint.status == "running"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (row.id, 1)

    # Preparing again preserves the retained checkpoint instead of rebasing it.
    await service.prepare_weekly_digest_ownership_backfill_for_portability_v1_restore(
        db_session
    )
    await db_session.refresh(checkpoint)
    assert checkpoint.status == "running"
    assert checkpoint.snapshot_rows == 1

    assert (await _finish(db_session)).completed
