"""Focused SQLite/PostgreSQL contracts for Stage-3P body-scan metric ownership."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select

from vitals.enums import Domain, Source
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.services import body_scan_metric_ownership_backfill_service as service
from vitals.services.body_scan_ownership_backfill_service import (
    BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.day_context_ownership_backfill_service import (
    DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
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


async def _scan(session, roots, *, owned=True, on_date=date(2026, 1, 2)):
    row = BodyScan(
        subject_id=roots.subject_id if owned else None,
        actor_user_id=None,
        date=on_date,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MANUAL.value,
        device="InBody 770",
        note="synthetic",
    )
    session.add(row)
    await session.flush()
    return row


def _metric(*, scan_id, subject_id=None, metric_key="skeletal_muscle_mass"):
    return BodyScanMetric(
        subject_id=subject_id,
        scan_id=scan_id,
        metric_key=metric_key,
        label="Скелетная мышечная масса",
        value=38.4,
        unit="kg",
        ref_low=33.0,
        ref_high=41.0,
        segment=None,
        category="composition",
    )


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_body_scan_metric_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3P did not complete")


def test_public_contract_is_fixed():
    assert service.BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.inherited_children.body_scan_metrics.v1"
    )
    assert service.BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_TABLES == (
        "body_scan_metrics",
    )
    assert service.DEFAULT_BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_BATCH_SIZE == 250
    assert [
        item.value for item in service.BodyScanMetricOwnershipBackfillStatus
    ] == ["not_started", "running", "completed"]


@pytest.mark.asyncio
async def test_child_inherits_only_the_reviewed_parent_subject(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    scan = await _scan(db_session, legacy_owner_roots)
    metric = _metric(scan_id=scan.id)
    db_session.add(metric)
    await db_session.flush()
    original = (
        metric.scan_id,
        metric.metric_key,
        metric.label,
        metric.value,
        metric.unit,
        metric.ref_low,
        metric.ref_high,
        metric.category,
        metric.updated_at,
    )

    result = await _finish(db_session)
    await db_session.refresh(metric)

    assert result.completed and result.updated_rows == 1
    assert metric.subject_id == legacy_owner_roots.subject_id
    assert (
        metric.scan_id,
        metric.metric_key,
        metric.label,
        metric.value,
        metric.unit,
        metric.ref_low,
        metric.ref_high,
        metric.category,
        metric.updated_at,
    ) == original
    assert "subject_id" not in result.to_safe_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["unowned_parent", "blank_metric_key", "non_finite_value"]
)
async def test_malformed_history_fails_closed(db_session, legacy_owner_roots, case):
    await _ready(db_session, legacy_owner_roots)
    if case == "unowned_parent":
        scan = await _scan(db_session, legacy_owner_roots, owned=False)
        db_session.add(_metric(scan_id=scan.id))
    elif case == "blank_metric_key":
        scan = await _scan(db_session, legacy_owner_roots)
        db_session.add(_metric(scan_id=scan.id, metric_key="   "))
    else:
        scan = await _scan(db_session, legacy_owner_roots)
        metric = _metric(scan_id=scan.id)
        metric.value = float("inf")
        db_session.add(metric)
    await db_session.flush()

    with pytest.raises(service.BodyScanMetricOwnershipBackfillError):
        await service.preflight_body_scan_metric_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_requires_the_inherited_subject(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    scan = await _scan(db_session, legacy_owner_roots)
    db_session.add(_metric(scan_id=scan.id))
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(_metric(scan_id=scan.id, metric_key="phase_angle"))
    await db_session.flush()
    with pytest.raises(service.BodyScanMetricOwnershipBackfillStateError):
        await service.preflight_body_scan_metric_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_evidence_is_stable(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    scan = await _scan(db_session, legacy_owner_roots)
    for index in range(5):
        db_session.add(_metric(scan_id=scan.id, metric_key=f"metric_{index}"))
    await db_session.flush()

    first = await service.run_body_scan_metric_ownership_backfill_batch(
        db_session, batch_size=2
    )
    assert not first.completed and first.batch_updated_rows == 2
    final = await _finish(db_session, batch_size=2)
    assert final.completed and final.updated_rows == 5
    repeat = await service.run_body_scan_metric_ownership_backfill_batch(db_session)
    assert repeat.completed and repeat.batch_scanned_rows == 0
    assert repeat.ownership_checksum_after == final.ownership_checksum_after


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_are_enforced(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.BodyScanMetricOwnershipBackfillDependencyError):
        await service.preflight_body_scan_metric_ownership_backfill(db_session)
    await _ready(db_session, legacy_owner_roots)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["body_scans"]
        )
    )
    with pytest.raises(service.BodyScanMetricOwnershipBackfillDependencyError):
        await service.preflight_body_scan_metric_ownership_backfill(db_session)
    db_session.add(
        _checkpoint(
            BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["body_scans"],
            legacy_owner_roots.subject_id,
        )
    )
    await db_session.flush()
    for size in (0, 1001, True, "250"):
        with pytest.raises(service.BodyScanMetricOwnershipBackfillValidationError):
            await service.run_body_scan_metric_ownership_backfill_batch(
                db_session, batch_size=size
            )


@pytest.mark.asyncio
async def test_restore_reset_rebases_the_exact_checkpoint(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    scan = await _scan(db_session, legacy_owner_roots)
    db_session.add(_metric(scan_id=scan.id))
    await db_session.flush()
    assert (await _finish(db_session)).completed

    await (
        service.reset_body_scan_metric_ownership_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds={"body_scan_metrics": (7, 3)}
        )
    )
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "body_scan_metrics"
            ]
        )
    )
    assert checkpoint.status == "running"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (7, 3)

    for bounds in ({"signals": (1, 1)}, {"body_scan_metrics": (0, 2)}):
        with pytest.raises(
            service.BodyScanMetricOwnershipBackfillValidationError
        ):
            await (
                service
                .reset_body_scan_metric_ownership_backfill_for_portability_v1_restore(
                    db_session, snapshot_bounds=bounds
                )
            )
