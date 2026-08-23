"""Focused SQLite/PostgreSQL contracts for Stage-3Q Garmin outbox ownership."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.garmin import (
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_SENT,
    GarminWeightExport,
)
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.tenancy import IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.services import garmin_weight_export_ownership_backfill_service as service
from vitals.services.body_scan_metric_ownership_backfill_service import (
    BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.body_scan_ownership_backfill_service import (
    BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
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
from vitals.services.tenancy_bootstrap import LEGACY_ACCOUNT_DISCRIMINATOR
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
_NAIVE = datetime(2026, 1, 2, 7, 30)
_PRIOR_PHASES = (
    (RAW_OWNERSHIP_BACKFILL_PHASE,)
    + tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
    + tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
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
    """Return the Garmin account the legacy resource bootstrap already created."""

    row = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.ACCOUNT.value,
        )
    )
    assert row is not None
    assert row.external_account_discriminator == LEGACY_ACCOUNT_DISCRIMINATOR
    return row


async def _extra_garmin_account(session, roots, *, discriminator):
    row = IntegrationConnection(
        subject_id=roots.subject_id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=discriminator,
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(row)
    await session.flush()
    return row


async def _drop_garmin_accounts(session, roots):
    await session.execute(
        delete(IntegrationConnection).where(
            IntegrationConnection.subject_id == roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.ACCOUNT.value,
        )
    )


async def _weight(session, roots, *, on_date=date(2026, 1, 2), owned=True):
    row = WeightLog(
        subject_id=roots.subject_id if owned else None,
        date=on_date,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=81.5,
        superseded=False,
    )
    session.add(row)
    await session.flush()
    return row


def _export(
    *,
    on_date=date(2026, 1, 2),
    weight_log_id=None,
    subject_id=None,
    integration_connection_id=None,
    requested_by_user_id=None,
    status=WEIGHT_EXPORT_PENDING,
):
    return GarminWeightExport(
        subject_id=subject_id,
        integration_connection_id=integration_connection_id,
        requested_by_user_id=requested_by_user_id,
        weight_log_id=weight_log_id,
        date=on_date,
        weight_kg=81.5,
        measured_at=_NAIVE,
        status=status,
        attempts=0,
        remote_owned=False,
    )


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_garmin_weight_export_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3Q did not complete")


def test_public_contract_is_fixed():
    assert service.GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.provider_outbox.garmin_weight_exports.v1"
    )
    assert service.GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_TABLES == (
        "garmin_weight_exports",
    )
    assert [
        item.value for item in service.GarminWeightExportOwnershipBackfillStatus
    ] == ["not_started", "running", "completed", "restore_blocked"]


@pytest.mark.asyncio
async def test_history_gains_subject_and_the_reviewed_destination(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    account = await _garmin_account(db_session, legacy_owner_roots)
    weight = await _weight(db_session, legacy_owner_roots)
    row = _export(weight_log_id=weight.id)
    db_session.add(row)
    await db_session.flush()
    original = (
        row.date,
        row.weight_kg,
        row.measured_at,
        row.status,
        row.attempts,
        row.remote_owned,
        row.updated_at,
    )

    result = await _finish(db_session)
    await db_session.refresh(row)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.integration_connection_id == account.id
    # The requesting actor is never invented.
    assert row.requested_by_user_id is None
    assert (
        row.date,
        row.weight_kg,
        row.measured_at,
        row.status,
        row.attempts,
        row.remote_owned,
        row.updated_at,
    ) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "no_account",
        "rotated_account",
        "non_legacy_discriminator",
        "partial_roots",
        "unowned_weight_log",
        "owned_without_destination",
        "unsupported_status",
    ],
)
async def test_malformed_history_fails_closed(db_session, legacy_owner_roots, case):
    await _ready(db_session, legacy_owner_roots)
    if case == "no_account":
        await _drop_garmin_accounts(db_session, legacy_owner_roots)
        db_session.add(_export())
    elif case == "rotated_account":
        await _extra_garmin_account(
            db_session, legacy_owner_roots, discriminator=f"rotated:{uuid.uuid4()}"
        )
        db_session.add(_export())
    elif case == "non_legacy_discriminator":
        await _drop_garmin_accounts(db_session, legacy_owner_roots)
        await _extra_garmin_account(
            db_session, legacy_owner_roots, discriminator=f"rotated:{uuid.uuid4()}"
        )
        db_session.add(_export())
    elif case == "partial_roots":
        db_session.add(_export(subject_id=legacy_owner_roots.subject_id))
    elif case == "unowned_weight_log":
        weight = await _weight(db_session, legacy_owner_roots, owned=False)
        db_session.add(_export(weight_log_id=weight.id))
    elif case == "owned_without_destination":
        db_session.add(
            _export(
                subject_id=legacy_owner_roots.subject_id,
                requested_by_user_id=legacy_owner_roots.user_id,
            )
        )
    elif case == "unsupported_status":
        row = _export()
        row.status = "teleported"
        db_session.add(row)
    else:
        db_session.add(_export())
    await db_session.flush()

    with pytest.raises(service.GarminWeightExportOwnershipBackfillError):
        await service.preflight_garmin_weight_export_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_requires_the_full_destination_graph(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    account = await _garmin_account(db_session, legacy_owner_roots)
    db_session.add(_export())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(_export(on_date=date(2026, 1, 3)))
    await db_session.flush()
    with pytest.raises(service.GarminWeightExportOwnershipBackfillStateError):
        await service.preflight_garmin_weight_export_ownership_backfill(db_session)
    await db_session.rollback()

    await _ready(db_session, legacy_owner_roots)
    assert account is not None


@pytest.mark.asyncio
async def test_live_owned_rows_stay_valid(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    account = await _garmin_account(db_session, legacy_owner_roots)
    db_session.add(_export())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    weight = await _weight(db_session, legacy_owner_roots, on_date=date(2026, 1, 4))
    db_session.add(
        _export(
            on_date=date(2026, 1, 4),
            weight_log_id=weight.id,
            subject_id=legacy_owner_roots.subject_id,
            integration_connection_id=account.id,
            requested_by_user_id=legacy_owner_roots.user_id,
            status=WEIGHT_EXPORT_SENT,
        )
    )
    # A delete intent legitimately outlives its local weight log.
    db_session.add(
        _export(
            on_date=date(2026, 1, 5),
            subject_id=legacy_owner_roots.subject_id,
            integration_connection_id=account.id,
            status=WEIGHT_EXPORT_DELETE_PENDING,
        )
    )
    await db_session.flush()
    result = await service.preflight_garmin_weight_export_ownership_backfill(
        db_session
    )
    assert result.completed and result.rows_above_high_watermark == 2


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_evidence_is_stable(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    await _garmin_account(db_session, legacy_owner_roots)
    for offset in range(5):
        db_session.add(_export(on_date=date(2026, 2, 1 + offset)))
    await db_session.flush()

    first = await service.run_garmin_weight_export_ownership_backfill_batch(
        db_session, batch_size=2
    )
    assert not first.completed and first.batch_updated_rows == 2
    final = await _finish(db_session, batch_size=2)
    assert final.completed and final.updated_rows == 5
    repeat = await service.run_garmin_weight_export_ownership_backfill_batch(
        db_session
    )
    assert repeat.completed and repeat.batch_scanned_rows == 0
    assert repeat.ownership_checksum_after == final.ownership_checksum_after


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_are_enforced(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.GarminWeightExportOwnershipBackfillDependencyError):
        await service.preflight_garmin_weight_export_ownership_backfill(db_session)
    await _ready(db_session, legacy_owner_roots)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "body_scan_metrics"
            ]
        )
    )
    with pytest.raises(service.GarminWeightExportOwnershipBackfillDependencyError):
        await service.preflight_garmin_weight_export_ownership_backfill(db_session)
    db_session.add(
        _checkpoint(
            BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "body_scan_metrics"
            ],
            legacy_owner_roots.subject_id,
        )
    )
    await db_session.flush()
    for size in (0, 1001, True, "250"):
        with pytest.raises(
            service.GarminWeightExportOwnershipBackfillValidationError
        ):
            await service.run_garmin_weight_export_ownership_backfill_batch(
                db_session, batch_size=size
            )


@pytest.mark.asyncio
async def test_portability_block_records_destination_loss(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    await _garmin_account(db_session, legacy_owner_roots)
    db_session.add(_export())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    await (
        service.block_garmin_weight_export_ownership_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds={"garmin_weight_exports": (11, 1)}
        )
    )
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "garmin_weight_exports"
            ]
        )
    )
    assert checkpoint.status == "restore_blocked"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (11, 1)

    with pytest.raises(service.GarminWeightExportOwnershipBackfillStateError):
        await service.run_garmin_weight_export_ownership_backfill_batch(
            db_session, batch_size=250
        )


@pytest.mark.asyncio
async def test_empty_portability_replacement_completes_exactly(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    await (
        service.block_garmin_weight_export_ownership_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds={"garmin_weight_exports": (0, 0)}
        )
    )
    result = await service.preflight_garmin_weight_export_ownership_backfill(
        db_session
    )
    assert result.completed and result.snapshot_rows == 0

    for bounds in ({"weight_logs": (1, 1)}, {"garmin_weight_exports": (0, 2)}):
        with pytest.raises(
            service.GarminWeightExportOwnershipBackfillValidationError
        ):
            await (
                service
                .block_garmin_weight_export_ownership_backfill_for_portability_v1_restore(
                    db_session, snapshot_bounds=bounds
                )
            )
