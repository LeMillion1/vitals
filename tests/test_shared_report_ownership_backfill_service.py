"""Focused SQLite/PostgreSQL contracts for Stage-3K shared reports."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import UserStatus
from vitals.models.identity import User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.share import SharedReport
from vitals.services import share_service
from vitals.operations.ownership import shared_report as service
from vitals.operations.ownership.conflict_rule import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hevy_child import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hrt_compound import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.progress_photo import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.raw import RAW_OWNERSHIP_BACKFILL_PHASE
from vitals.utils.timeutils import now_local


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
)


def _checkpoint(
    phase: str,
    subject_id: uuid.UUID,
    *,
    status: str = "completed",
    high: int = 0,
    count: int = 0,
) -> OwnershipBackfillCheckpoint:
    completed = status == "completed"
    return OwnershipBackfillCheckpoint(
        phase_key=phase,
        subject_id=subject_id,
        status=status,
        scan_high_watermark_id=high,
        snapshot_rows=count,
        last_scanned_id=high if completed else 0,
        scanned_rows=count if completed else 0,
        updated_rows=0,
        unchanged_rows=count if completed else 0,
        data_checksum_before=_EMPTY,
        data_checksum_after=_EMPTY,
        ownership_checksum_after=_EMPTY,
        started_at=_STAMP,
        updated_at=_STAMP,
        completed_at=_STAMP if completed else None,
    )


def _set_nonempty_running(checkpoint: OwnershipBackfillCheckpoint) -> None:
    checkpoint.status = "running"
    checkpoint.scan_high_watermark_id = checkpoint.snapshot_rows = 1
    checkpoint.last_scanned_id = checkpoint.scanned_rows = 0
    checkpoint.updated_rows = checkpoint.unchanged_rows = 0
    checkpoint.data_checksum_before = _EMPTY
    checkpoint.data_checksum_after = _EMPTY
    checkpoint.ownership_checksum_after = _EMPTY
    checkpoint.completed_at = None


def _set_nonempty_completed(checkpoint: OwnershipBackfillCheckpoint) -> None:
    checkpoint.status = "completed"
    checkpoint.scan_high_watermark_id = checkpoint.snapshot_rows = 1
    checkpoint.last_scanned_id = checkpoint.scanned_rows = 1
    checkpoint.updated_rows = 0
    checkpoint.unchanged_rows = 1
    checkpoint.data_checksum_before = _EMPTY
    checkpoint.data_checksum_after = _EMPTY
    checkpoint.ownership_checksum_after = _EMPTY
    checkpoint.completed_at = _STAMP


def _set_restore_blocked(checkpoint: OwnershipBackfillCheckpoint) -> None:
    _set_nonempty_running(checkpoint)
    checkpoint.status = "restore_blocked"


async def _ready(session, roots):
    checkpoints = [_checkpoint(phase, roots.subject_id) for phase in _PRIOR_PHASES]
    session.add_all(checkpoints)
    await session.flush()
    return {row.phase_key: row for row in checkpoints}


def _report(token: str, **roots) -> SharedReport:
    values = dict(
        token=token,
        password_hash="$2b$12$" + "x" * 53,
        title="Synthetic retained report",
        preset="doctor",
        domains=["labs", "weight"],
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        labs_flagged_only=True,
        note="private note",
        snapshot={"version": 1, "blocks": {"labs": [{"value": 1.25}]}},
        expires_at=now_local() + timedelta(days=30),
    )
    values.update(roots)
    return SharedReport(**values)


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_shared_report_ownership_backfill_batch(
            session,
            batch_size=batch_size,
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3K did not complete")


def test_public_contract_is_fixed():
    assert service.SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.retained_artifact.shared_reports.v1"
    )
    assert service.SHARED_REPORT_OWNERSHIP_BACKFILL_TABLES == ("shared_reports",)
    assert tuple(service.SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        "shared_reports",
    )
    assert [status.value for status in service.SharedReportOwnershipBackfillStatus] == [
        "not_started",
        "running",
        "completed",
    ]
    with pytest.raises(TypeError):
        service.SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_absent_checkpoint_bridge_state_is_exact_sentinel(
    db_session, legacy_owner_roots
):
    state = await service.shared_report_historical_bridge_state(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert state == service.SharedReportHistoricalBridgeState(0, 0, False)
    with pytest.raises(service.SharedReportOwnershipBackfillValidationError):
        await service.shared_report_historical_bridge_state(
            db_session,
            subject_id="not-a-uuid",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_legacy_report_gains_only_subject_and_preserves_artifact(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    row = _report("legacy-preserved")
    db_session.add(row)
    await db_session.flush()
    original = tuple(getattr(row, field) for field in service._DATA_FIELDS)

    result = await _finish(db_session)
    await db_session.refresh(row)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.created_by_user_id is None and row.revoked_by_user_id is None
    assert tuple(getattr(row, field) for field in service._DATA_FIELDS) == original
    safe = result.to_safe_dict()
    assert "subject_id" not in safe
    assert "token" not in safe and "snapshot" not in safe


@pytest.mark.asyncio
async def test_running_bridge_exposes_only_processed_cursor(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    rows = [_report(f"cursor-{index}") for index in range(3)]
    db_session.add_all(rows)
    await db_session.flush()

    result = await service.run_shared_report_ownership_backfill_batch(
        db_session,
        batch_size=1,
    )
    state = await service.shared_report_historical_bridge_state(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
    )

    assert result.status is service.SharedReportOwnershipBackfillStatus.RUNNING
    assert state == service.SharedReportHistoricalBridgeState(
        rows[0].id,
        rows[-1].id,
        False,
    )
    assert state.historical_subject_id == legacy_owner_roots.subject_id
    other_subject_state = await service.shared_report_historical_bridge_state(
        db_session,
        subject_id=uuid.uuid4(),
    )
    assert other_subject_state == state
    assert (
        other_subject_state.historical_subject_id
        == legacy_owner_roots.subject_id
    )
    assert rows[0].subject_id == legacy_owner_roots.subject_id
    assert rows[1].subject_id is None


@pytest.mark.asyncio
async def test_historical_actor_shapes_are_preserved(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    owned = _report(
        "historical-owned",
        subject_id=legacy_owner_roots.subject_id,
        created_by_user_id=legacy_owner_roots.user_id,
    )
    revoked = _report(
        "historical-revoked",
        subject_id=legacy_owner_roots.subject_id,
        revoked_by_user_id=legacy_owner_roots.user_id,
        revoked_at=now_local(),
        snapshot=None,
    )
    actorless_revoked = _report(
        "historical-actorless-revoked",
        subject_id=legacy_owner_roots.subject_id,
        revoked_at=now_local(),
        snapshot=None,
    )
    db_session.add_all([owned, revoked, actorless_revoked])
    await db_session.flush()

    result = await _finish(db_session)

    assert result.completed and result.unchanged_rows == 3
    assert owned.created_by_user_id == legacy_owner_roots.user_id
    assert revoked.created_by_user_id is None
    assert revoked.revoked_by_user_id == legacy_owner_roots.user_id
    assert actorless_revoked.revoked_by_user_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["partial", "foreign", "revoker_without_time"])
async def test_invalid_historical_roots_fail_closed(
    db_session, legacy_owner_roots, shape
):
    await _ready(db_session, legacy_owner_roots)
    if shape == "partial":
        row = _report(
            "bad-partial",
            created_by_user_id=legacy_owner_roots.user_id,
        )
    elif shape == "foreign":
        foreign = User(
            username=f"foreign-{uuid.uuid4()}",
            normalized_username=f"foreign-{uuid.uuid4()}",
            password_hash="synthetic-not-a-real-secret",
            status=UserStatus.ACTIVE.value,
        )
        db_session.add(foreign)
        await db_session.flush()
        row = _report(
            "bad-foreign",
            subject_id=legacy_owner_roots.subject_id,
            created_by_user_id=foreign.id,
        )
    else:
        row = _report(
            "bad-revoker",
            subject_id=legacy_owner_roots.subject_id,
            revoked_by_user_id=legacy_owner_roots.user_id,
        )
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(service.SharedReportOwnershipBackfillError):
        await service.preflight_shared_report_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_requires_owner_creator(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    first = _report("hwm")
    db_session.add(first)
    await db_session.flush()
    await service.run_shared_report_ownership_backfill_batch(db_session, batch_size=1)

    bad = _report("bad-live", subject_id=legacy_owner_roots.subject_id)
    db_session.add(bad)
    await db_session.flush()
    with pytest.raises(service.SharedReportOwnershipBackfillProvenanceError):
        await service.preflight_shared_report_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_completed_checkpoint_accepts_supported_artifact_volatility(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    old = _report("volatile-old")
    removed = _report("volatile-delete")
    db_session.add_all([old, removed])
    await db_session.flush()
    await _finish(db_session, batch_size=1)

    old.opened_count = 4
    old.last_opened_at = now_local()
    old.revoked_at = now_local()
    old.revoked_by_user_id = legacy_owner_roots.user_id
    old.snapshot = None
    await db_session.delete(removed)
    live = _report(
        "volatile-live",
        subject_id=legacy_owner_roots.subject_id,
        created_by_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(live)
    await db_session.flush()

    result = await service.preflight_shared_report_ownership_backfill(db_session)
    state = await service.shared_report_historical_bridge_state(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert result.completed and result.rows_above_high_watermark == 1
    assert state.completed
    assert state.processed_high_watermark_id == state.snapshot_high_watermark_id


@pytest.mark.asyncio
async def test_running_finalization_rejects_processed_artifact_drift(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    first = _report("drift-first")
    second = _report("drift-second")
    db_session.add_all([first, second])
    await db_session.flush()
    await service.run_shared_report_ownership_backfill_batch(db_session, batch_size=1)
    first.title = "changed after processing"
    await db_session.flush()

    with pytest.raises(service.SharedReportOwnershipBackfillStateError):
        await service.run_shared_report_ownership_backfill_batch(
            db_session,
            batch_size=1,
        )


@pytest.mark.asyncio
async def test_restore_prepare_creates_retained_witness_without_bounds(
    db_session, legacy_owner_roots
):
    checkpoints = await _ready(db_session, legacy_owner_roots)
    raw = checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE]
    raw.status = "restore_blocked"
    raw.scan_high_watermark_id = raw.snapshot_rows = 1
    raw.last_scanned_id = raw.scanned_rows = raw.updated_rows = raw.unchanged_rows = 0
    raw.completed_at = None
    row = _report("retained-witness")
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(service.SharedReportOwnershipBackfillDependencyError):
        await service.preflight_shared_report_ownership_backfill(db_session)
    await service.prepare_shared_report_ownership_backfill_for_portability_v1_restore(
        db_session
    )
    result = await service.preflight_shared_report_ownership_backfill(db_session)

    assert result.status is service.SharedReportOwnershipBackfillStatus.RUNNING
    assert result.snapshot_rows == 1
    assert row.subject_id is None


@pytest.mark.asyncio
async def test_restore_prepare_empty_is_exact_completed(db_session, legacy_owner_roots):
    checkpoints = await _ready(db_session, legacy_owner_roots)
    raw = checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE]
    raw.status = "restore_blocked"
    raw.scan_high_watermark_id = raw.snapshot_rows = 1
    raw.last_scanned_id = raw.scanned_rows = raw.updated_rows = raw.unchanged_rows = 0
    raw.completed_at = None

    await service.prepare_shared_report_ownership_backfill_for_portability_v1_restore(
        db_session
    )
    result = await service.preflight_shared_report_ownership_backfill(db_session)
    assert result.completed and result.snapshot_rows == 0




@pytest.mark.asyncio
async def test_restore_rejects_empty_parent_with_nonempty_child_progression(
    db_session, legacy_owner_roots
):
    checkpoints = await _ready(db_session, legacy_owner_roots)
    _set_restore_blocked(checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE])
    db_session.add(_report("bad-restore-progression"))
    await db_session.flush()
    await service.prepare_shared_report_ownership_backfill_for_portability_v1_restore(
        db_session
    )
    components = checkpoints[
        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
            "hrt_compound_components"
        ]
    ]
    _set_nonempty_running(components)
    await db_session.flush()

    with pytest.raises(service.SharedReportOwnershipBackfillDependencyError):
        await service.preflight_shared_report_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_dependency_and_batch_validation_fail_closed(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.SharedReportOwnershipBackfillDependencyError):
        await service.preflight_shared_report_ownership_backfill(db_session)
    with pytest.raises(service.SharedReportOwnershipBackfillValidationError):
        await service.run_shared_report_ownership_backfill_batch(
            db_session,
            batch_size=True,
        )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["data_change", "row_delete"])
async def test_postgres_projected_report_races_roll_back_without_progress(
    db_session, legacy_owner_roots, monkeypatch, race
):
    await _ready(db_session, legacy_owner_roots)
    row = _report("pg-projected-race")
    db_session.add(row)
    await db_session.commit()
    row_id = row.id
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    projected = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(writer_done.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_shared_report_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_shared_report_ownership_backfill_batch(
                    session,
                    batch_size=1000,
                )
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()
            return None

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(projected.wait(), timeout=15)
        async with factory() as writer:
            if race == "data_change":
                await writer.execute(
                    update(SharedReport)
                    .where(SharedReport.id == row_id)
                    .values(opened_count=8)
                )
            else:
                await writer.execute(
                    delete(SharedReport).where(SharedReport.id == row_id)
                )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=15)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert isinstance(error, service.SharedReportOwnershipBackfillStateError)
    async with factory() as verify:
        checkpoint = await verify.get(OwnershipBackfillCheckpoint, service._PHASE_KEY)
        assert checkpoint is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_public_open_serializes_on_governance_lock(
    db_session, legacy_owner_roots, monkeypatch
):
    await _ready(db_session, legacy_owner_roots)
    row = _report("pg-open-serialization")
    db_session.add(row)
    await db_session.commit()
    token = row.token
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    projected = asyncio.Event()
    release = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(release.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_shared_report_projection_for_test", pause)

    async def backfill_worker():
        async with factory() as session:
            result = await service.run_shared_report_ownership_backfill_batch(
                session,
                batch_size=1000,
            )
            await session.commit()
            return result

    async def open_worker():
        async with factory() as session:
            opened = await share_service.register_open(session, token)
            await session.commit()
            return opened.id if opened is not None else None

    backfill = asyncio.create_task(backfill_worker())
    await asyncio.wait_for(projected.wait(), timeout=15)
    opener = asyncio.create_task(open_worker())
    await asyncio.sleep(0.1)
    assert not opener.done()
    release.set()
    result = await asyncio.wait_for(backfill, timeout=15)
    opened_id = await asyncio.wait_for(opener, timeout=15)

    assert result.completed and opened_id is not None
    async with factory() as verify:
        stored = await verify.get(SharedReport, opened_id)
        assert stored.subject_id == legacy_owner_roots.subject_id
        assert stored.opened_count == 1
