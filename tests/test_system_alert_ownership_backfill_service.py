"""Focused SQLite/PostgreSQL contracts for Stage-3T system-alert ownership."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.services import system_alert_ownership_backfill_service as service
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
from vitals.services.notification_ownership_backfill_service import (
    NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
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
from vitals.services.weekly_digest_ownership_backfill_service import (
    WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.weight_log_ownership_backfill_service import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)


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
    + tuple(WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
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


async def _garmin_account(session, roots):
    row = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.ACCOUNT.value,
        )
    )
    assert row is not None
    return row


def _alert(
    *,
    alert_key="weight.noisy_period_active",
    domain=Domain.WEIGHT.value,
    entity_ref="",
    subject_id=None,
    integration_connection_id=None,
    resolved_at=None,
    resolved_by_user_id=None,
):
    return SystemAlert(
        subject_id=subject_id,
        integration_connection_id=integration_connection_id,
        domain=domain,
        severity="warn",
        message="synthetic alert",
        alert_key=alert_key,
        entity_ref=entity_ref,
        resolved_at=resolved_at,
        resolved_by_user_id=resolved_by_user_id,
    )


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_system_alert_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3T did not complete")


def test_public_contract_is_fixed():
    assert service.SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.subject_optional.system_alerts.v1"
    )
    assert service.SYSTEM_ALERT_OWNERSHIP_BACKFILL_TABLES == ("system_alerts",)
    assert [
        item.value for item in service.SystemAlertOwnershipBackfillStatus
    ] == ["not_started", "running", "completed"]


@pytest.mark.asyncio
async def test_each_reviewed_class_gains_exactly_its_own_roots(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    account = await _garmin_account(db_session, legacy_owner_roots)
    health = _alert()
    conflict = _alert(alert_key="conflict:7", entity_ref="weight:1")
    provider = _alert(alert_key="garmin.auth", domain=Domain.GARMIN.value)
    platform = _alert(
        alert_key="scheduler.job_failed:raw_payload_sweep",
        domain=Domain.SYSTEM.value,
    )
    db_session.add_all([health, conflict, provider, platform])
    await db_session.flush()

    result = await _finish(db_session)
    for row in (health, conflict, provider, platform):
        await db_session.refresh(row)

    assert result.completed and result.updated_rows == 3
    assert health.subject_id == legacy_owner_roots.subject_id
    assert health.integration_connection_id is None
    assert conflict.subject_id == legacy_owner_roots.subject_id
    assert conflict.integration_connection_id is None
    assert provider.subject_id == legacy_owner_roots.subject_id
    assert provider.integration_connection_id == account.id
    # An installation-wide alert owns neither root and is never adopted.
    assert platform.subject_id is None
    assert platform.integration_connection_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unknown_key",
        "health_with_connection",
        "platform_with_subject",
        "provider_foreign_connection",
        "resolution_actor_without_resolution",
        "foreign_lifecycle_actor",
    ],
)
async def test_malformed_history_fails_closed(db_session, legacy_owner_roots, case):
    await _ready(db_session, legacy_owner_roots)
    account = await _garmin_account(db_session, legacy_owner_roots)
    if case == "unknown_key":
        db_session.add(_alert(alert_key="mystery.alarm"))
    elif case == "health_with_connection":
        db_session.add(
            _alert(
                subject_id=legacy_owner_roots.subject_id,
                integration_connection_id=account.id,
            )
        )
    elif case == "platform_with_subject":
        db_session.add(
            _alert(
                alert_key="scheduler.job_failed:share_purge",
                domain=Domain.SYSTEM.value,
                subject_id=legacy_owner_roots.subject_id,
            )
        )
    elif case == "provider_foreign_connection":
        telegram = await db_session.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
                IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
            )
        )
        db_session.add(
            _alert(
                alert_key="garmin.auth",
                domain=Domain.GARMIN.value,
                subject_id=legacy_owner_roots.subject_id,
                integration_connection_id=telegram.id,
            )
        )
    elif case == "resolution_actor_without_resolution":
        db_session.add(_alert(resolved_by_user_id=legacy_owner_roots.user_id))
    else:
        from vitals.enums import UserStatus
        from vitals.models.identity import User

        foreign = User(
            username="foreign-actor",
            normalized_username="foreign-actor",
            password_hash="$2b$04$synthetic",
            status=UserStatus.ACTIVE.value,
        )
        db_session.add(foreign)
        await db_session.flush()
        db_session.add(
            _alert(
                resolved_at=datetime(2026, 1, 2, 8, 0),
                resolved_by_user_id=foreign.id,
            )
        )
    await db_session.flush()

    with pytest.raises(service.SystemAlertOwnershipBackfillError):
        await service.preflight_system_alert_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_requires_explicit_ownership(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_alert())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(_alert(entity_ref="weight:99"))
    await db_session.flush()
    with pytest.raises(service.SystemAlertOwnershipBackfillStateError):
        await service.preflight_system_alert_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_platform_alerts_stay_unowned_after_completion(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_alert())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(
        _alert(
            alert_key="scheduler.job_failed:share_purge",
            domain=Domain.SYSTEM.value,
            entity_ref="job:1",
        )
    )
    await db_session.flush()
    result = await service.preflight_system_alert_ownership_backfill(db_session)
    assert result.completed and result.rows_above_high_watermark == 1


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_evidence_is_stable(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    for index in range(5):
        db_session.add(_alert(entity_ref=f"weight:{index}"))
    await db_session.flush()

    first = await service.run_system_alert_ownership_backfill_batch(
        db_session, batch_size=2
    )
    assert not first.completed and first.batch_updated_rows == 2
    final = await _finish(db_session, batch_size=2)
    assert final.completed and final.updated_rows == 5
    repeat = await service.run_system_alert_ownership_backfill_batch(db_session)
    assert repeat.completed and repeat.batch_scanned_rows == 0
    assert repeat.ownership_checksum_after == final.ownership_checksum_after


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_are_enforced(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.SystemAlertOwnershipBackfillDependencyError):
        await service.preflight_system_alert_ownership_backfill(db_session)
    await _ready(db_session, legacy_owner_roots)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["notifications"]
        )
    )
    with pytest.raises(service.SystemAlertOwnershipBackfillDependencyError):
        await service.preflight_system_alert_ownership_backfill(db_session)
    db_session.add(
        _checkpoint(
            NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["notifications"],
            legacy_owner_roots.subject_id,
        )
    )
    await db_session.flush()
    for size in (0, 1001, True, "250"):
        with pytest.raises(service.SystemAlertOwnershipBackfillValidationError):
            await service.run_system_alert_ownership_backfill_batch(
                db_session, batch_size=size
            )


@pytest.mark.asyncio
async def test_restore_reset_rebases_the_exact_checkpoint(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_alert())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    await service.reset_system_alert_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"system_alerts": (7, 3)}
    )
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.SYSTEM_ALERT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "system_alerts"
            ]
        )
    )
    assert checkpoint.status == "running"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (7, 3)

    for bounds in ({"notifications": (1, 1)}, {"system_alerts": (0, 2)}):
        with pytest.raises(service.SystemAlertOwnershipBackfillValidationError):
            await (
                service.reset_system_alert_ownership_backfill_for_portability_v1_restore(
                    db_session, snapshot_bounds=bounds
                )
            )


@pytest.mark.asyncio
async def test_restored_provider_alert_regains_its_connection(
    db_session, legacy_owner_roots
):
    """Backup v1 rebinds S but strips C; the phase completes the graph again."""

    await _ready(db_session, legacy_owner_roots)
    account = await _garmin_account(db_session, legacy_owner_roots)
    restored = _alert(
        alert_key="garmin.auth",
        domain=Domain.GARMIN.value,
        subject_id=legacy_owner_roots.subject_id,
    )
    db_session.add(restored)
    await db_session.flush()

    result = await _finish(db_session)
    await db_session.refresh(restored)

    assert result.completed and result.updated_rows == 1
    assert restored.integration_connection_id == account.id
