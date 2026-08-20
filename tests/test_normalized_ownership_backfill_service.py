"""Focused SQLite contracts for the Stage-3B normalized ownership backfill."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from vitals.enums import Domain, Source, UserStatus
from vitals.models.hrt import HrtCompound, HrtCycle, HrtCycleItem
from vitals.models.identity import HealthSubject, User
from vitals.models.labs import LabMarker
from vitals.models.milestones import Milestone
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.skincare import SkincareLog
from vitals.models.timeline import Annotation
from vitals.services import normalized_ownership_backfill_service as backfill_service
from vitals.services.normalized_ownership_backfill_service import (
    MAX_NORMALIZED_OWNERSHIP_BACKFILL_BATCH_SIZE,
    NORMALIZED_MANUAL_BACKFILL_PHASE,
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
    NORMALIZED_MANUAL_TABLES,
    NormalizedOwnershipBackfillDependencyError,
    NormalizedOwnershipBackfillProvenanceError,
    NormalizedOwnershipBackfillStateError,
    NormalizedOwnershipBackfillStatus,
    NormalizedOwnershipBackfillValidationError,
    preflight_normalized_ownership_backfill,
    reset_normalized_manual_backfill_for_portability_v1_restore,
    run_normalized_ownership_backfill_batch,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


async def _scope(session, *, slug: str = "owner"):
    owner = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    return owner, subject


def _checkpoint(
    *,
    phase: str,
    subject_id: uuid.UUID,
    status: str,
    high_watermark: int = 0,
    snapshot_rows: int = 0,
) -> OwnershipBackfillCheckpoint:
    completed = status == "completed"
    scanned = snapshot_rows if completed else 0
    cursor = high_watermark if completed else 0
    timestamp = datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC)
    return OwnershipBackfillCheckpoint(
        phase_key=phase,
        subject_id=subject_id,
        status=status,
        scan_high_watermark_id=high_watermark,
        snapshot_rows=snapshot_rows,
        last_scanned_id=cursor,
        scanned_rows=scanned,
        updated_rows=0,
        unchanged_rows=scanned,
        data_checksum_before=_EMPTY_SHA256,
        data_checksum_after=_EMPTY_SHA256,
        ownership_checksum_after=_EMPTY_SHA256,
        started_at=timestamp,
        updated_at=timestamp,
        completed_at=timestamp if completed else None,
    )


async def _ready_scope(session):
    owner, subject = await _scope(session)
    session.add(
        _checkpoint(
            phase=RAW_OWNERSHIP_BACKFILL_PHASE,
            subject_id=subject.id,
            status="completed",
        )
    )
    await session.flush()
    return owner, subject


def _cycle(**ownership) -> HrtCycle:
    return HrtCycle(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        kind="course",
        start_date=date(2026, 8, 1),
        name="synthetic",
        **ownership,
    )


def _empty_bounds() -> dict[str, tuple[int, int]]:
    return {table_name: (0, 0) for table_name in NORMALIZED_MANUAL_TABLES}


async def _finish_all(session, *, batch_size: int = 1000):
    result = None
    for _ in range(len(NORMALIZED_MANUAL_TABLES) + 5):
        result = await run_normalized_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("fixed catalog did not complete")


@pytest.mark.asyncio
async def test_dependency_is_required_and_preflight_is_read_only(db_session):
    await _scope(db_session)
    db_session.add(_cycle())
    await db_session.flush()

    with pytest.raises(NormalizedOwnershipBackfillDependencyError):
        await preflight_normalized_ownership_backfill(db_session)
    assert (
        int(
            await db_session.scalar(
                select(func.count()).select_from(OwnershipBackfillCheckpoint)
            )
            or 0
        )
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "restore_blocked"])
async def test_ordinary_api_rejects_noncompleted_raw_dependency(db_session, status):
    _owner, subject = await _scope(db_session)
    db_session.add(
        _checkpoint(
            phase=RAW_OWNERSHIP_BACKFILL_PHASE,
            subject_id=subject.id,
            status=status,
            high_watermark=1 if status == "restore_blocked" else 0,
            snapshot_rows=1 if status == "restore_blocked" else 0,
        )
    )
    await db_session.flush()

    with pytest.raises(NormalizedOwnershipBackfillDependencyError):
        await run_normalized_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
async def test_foreign_raw_dependency_projection_fails_closed():
    from vitals.services import normalized_ownership_backfill_service as service

    subject_id = uuid.uuid4()
    projection = service._CheckpointProjection(
        RAW_OWNERSHIP_BACKFILL_PHASE,
        uuid.uuid4(),
        "completed",
        0,
        0,
        0,
        0,
        0,
        0,
        _EMPTY_SHA256,
        _EMPTY_SHA256,
        _EMPTY_SHA256,
        datetime(2026, 8, 21),
    )
    with pytest.raises(NormalizedOwnershipBackfillDependencyError):
        service._require_completed_dependency(
            projection,
            subject_id=subject_id,
        )


@pytest.mark.asyncio
async def test_empty_completed_stage3a_allows_bounded_flush_only_backfill(db_session):
    _owner, subject = await _ready_scope(db_session)
    row = _cycle()
    db_session.add(row)
    await db_session.commit()
    row_id = row.id
    updated_at = row.updated_at

    before = await preflight_normalized_ownership_backfill(db_session)
    assert before.status is NormalizedOwnershipBackfillStatus.NOT_STARTED
    assert before.tables_total == len(NORMALIZED_MANUAL_TABLES) == 17
    assert before.snapshot_rows == before.remaining_rows == 1
    assert before.completed_tables == 0
    assert "subject_id" not in before.to_safe_dict()

    result = await run_normalized_ownership_backfill_batch(
        db_session, batch_size=1
    )
    db_session.expire(row)
    await db_session.refresh(row)
    assert result.batch_table == "hrt_cycles"
    assert result.batch_scanned_rows == result.batch_updated_rows == 1
    assert row.subject_id == subject.id
    assert row.actor_user_id is None
    assert row.updated_at == updated_at
    assert result.data_checksum_before == result.data_checksum_after
    assert "batch_table" in result.to_safe_dict()
    await db_session.rollback()

    restored = await db_session.get(HrtCycle, row_id)
    assert restored is not None
    assert restored.subject_id is None
    assert (
        await db_session.get(
            OwnershipBackfillCheckpoint,
            NORMALIZED_MANUAL_CHECKPOINT_PHASES["hrt_cycles"],
        )
        is None
    )


@pytest.mark.asyncio
async def test_one_batch_advances_only_first_incomplete_table_and_is_idempotent(
    db_session,
):
    await _ready_scope(db_session)
    first = await run_normalized_ownership_backfill_batch(db_session, batch_size=2)
    assert first.batch_table == "hrt_cycles"
    assert first.completed_tables == 1
    assert first.batch_scanned_rows == 0

    second = await run_normalized_ownership_backfill_batch(db_session, batch_size=2)
    assert second.batch_table == "hrt_cycle_templates"
    assert second.completed_tables == 2

    final = await _finish_all(db_session)
    assert final.status is NormalizedOwnershipBackfillStatus.COMPLETED
    assert final.completed_tables == final.tables_total == 17
    repeated = await run_normalized_ownership_backfill_batch(
        db_session, batch_size=2
    )
    assert repeated.completed
    assert repeated.batch_scanned_rows == repeated.batch_updated_rows == 0


@pytest.mark.asyncio
async def test_ordinary_batch_scans_only_target_and_appended_tails(
    db_session,
    monkeypatch,
):
    await _ready_scope(db_session)
    db_session.add_all([_cycle(), _cycle()])
    db_session.add_all(
        [
            Annotation(
                date=date(2026, 8, day),
                domain=Domain.TIMELINE.value,
                source=Source.MANUAL.value,
                kind="note",
                title=f"synthetic-{day}",
            )
            for day in range(1, 8)
        ]
    )
    await db_session.flush()

    scanned: list[tuple[str, int]] = []
    original = backfill_service._scan_table

    async def tracked_scan(*args, spec, start_after=0, **kwargs):
        scanned.append((spec.name, start_after))
        return await original(
            *args,
            spec=spec,
            start_after=start_after,
            **kwargs,
        )

    monkeypatch.setattr(backfill_service, "_scan_table", tracked_scan)
    result = await run_normalized_ownership_backfill_batch(
        db_session,
        batch_size=1,
    )

    assert result.batch_table == "hrt_cycles"
    assert scanned == [("hrt_cycles", 2)]


@pytest.mark.asyncio
async def test_historical_owned_shapes_and_mcp_are_preserved(db_session):
    owner, subject = await _ready_scope(db_session)
    actorless = _cycle(subject_id=subject.id)
    actor_owned = _cycle(subject_id=subject.id, actor_user_id=owner.id)
    actor_owned.source = Source.MCP.value
    db_session.add_all([actorless, actor_owned])
    await db_session.flush()

    result = await run_normalized_ownership_backfill_batch(
        db_session, batch_size=10
    )
    assert result.batch_updated_rows == 0
    assert result.batch_unchanged_rows == 2
    assert actorless.actor_user_id is None
    assert actor_owned.actor_user_id == owner.id
    assert actor_owned.source == Source.MCP.value


@pytest.mark.asyncio
async def test_partial_foreign_and_bad_provenance_fail_without_adoption(db_session):
    owner, subject = await _ready_scope(db_session)
    partial = _cycle(actor_user_id=owner.id)
    db_session.add(partial)
    await db_session.flush()
    with pytest.raises(NormalizedOwnershipBackfillStateError, match="partial"):
        await run_normalized_ownership_backfill_batch(db_session, batch_size=1)
    assert partial.subject_id is None
    await db_session.rollback()

    owner, subject = await _ready_scope(db_session)
    bad = _cycle()
    bad.domain = Domain.SYSTEM.value
    db_session.add(bad)
    await db_session.flush()
    with pytest.raises(NormalizedOwnershipBackfillProvenanceError):
        await preflight_normalized_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_above_high_watermark_requires_exact_live_actor(db_session):
    owner, subject = await _ready_scope(db_session)
    owner_id = owner.id
    subject_id = subject.id
    db_session.add(_cycle())
    await db_session.flush()
    await run_normalized_ownership_backfill_batch(db_session, batch_size=1)
    await db_session.commit()

    appended = _cycle(subject_id=subject_id)
    db_session.add(appended)
    await db_session.flush()
    with pytest.raises(NormalizedOwnershipBackfillStateError, match="high-water"):
        await run_normalized_ownership_backfill_batch(db_session, batch_size=1)
    await db_session.rollback()

    appended = _cycle(subject_id=subject_id, actor_user_id=owner_id)
    db_session.add(appended)
    await db_session.flush()
    result = await run_normalized_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert result.batch_table == "hrt_cycle_templates"


@pytest.mark.asyncio
async def test_lab_marker_allows_reviewed_actorless_live_seed(db_session):
    _owner, subject = await _ready_scope(db_session)
    db_session.add(
        _checkpoint(
            phase=NORMALIZED_MANUAL_CHECKPOINT_PHASES["lab_markers"],
            subject_id=subject.id,
            status="completed",
        )
    )
    db_session.add(
        LabMarker(
            domain=Domain.LABS.value,
            name="Synthetic actorless seed",
            subject_id=subject.id,
            actor_user_id=None,
        )
    )
    await db_session.flush()

    result = await preflight_normalized_ownership_backfill(db_session)
    assert result.rows_above_high_watermark == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["annotation", "milestone"])
async def test_system_domain_is_forbidden_for_user_classification_tables(
    db_session, model
):
    await _ready_scope(db_session)
    if model == "annotation":
        row = Annotation(
            date=date(2026, 8, 21),
            domain=Domain.SYSTEM.value,
            source=Source.MANUAL.value,
            kind="note",
            title="synthetic",
        )
    else:
        row = Milestone(
            domain=Domain.SYSTEM.value,
            name="synthetic",
        )
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(NormalizedOwnershipBackfillProvenanceError):
        await preflight_normalized_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_completed_snapshot_allows_later_business_data_edits(db_session):
    await _ready_scope(db_session)
    row = _cycle()
    db_session.add(row)
    await db_session.flush()
    completed = await run_normalized_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert completed.completed_tables == 1

    row.name = "changed after completion"
    await db_session.flush()
    result = await preflight_normalized_ownership_backfill(db_session)
    assert result.completed_tables == 1
    assert result.data_checksum_before == completed.data_checksum_before
    assert result.data_checksum_after == completed.data_checksum_after


@pytest.mark.asyncio
async def test_completed_snapshot_rejects_later_actor_provenance_drift(
    db_session,
):
    owner, _subject = await _ready_scope(db_session)
    row = _cycle()
    db_session.add(row)
    await db_session.flush()
    await run_normalized_ownership_backfill_batch(db_session, batch_size=1)

    row.actor_user_id = owner.id
    await db_session.flush()
    with pytest.raises(
        NormalizedOwnershipBackfillStateError,
        match="ownership changed",
    ):
        await preflight_normalized_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_group_finalization_detects_cross_table_maintenance_drift(
    db_session,
):
    await _ready_scope(db_session)
    row = _cycle()
    db_session.add(row)
    await db_session.flush()
    completed = await run_normalized_ownership_backfill_batch(
        db_session,
        batch_size=1,
    )
    assert completed.completed_tables == 1

    row.name = "changed before the group maintenance window closed"
    await db_session.flush()
    with pytest.raises(NormalizedOwnershipBackfillStateError, match="data changed"):
        await _finish_all(db_session)


@pytest.mark.asyncio
async def test_cross_table_child_and_compound_gates_are_read_only(db_session):
    owner, subject = await _ready_scope(db_session)
    cycle = _cycle()
    db_session.add(cycle)
    await db_session.flush()
    # Both database engines enforce the child subject FK in the full suite.
    # The transition deliberately accepts a nullable historical child without
    # rewriting it; foreign persisted subjects are rejected by the DB and the
    # service's defense-in-depth gate.
    child = HrtCycleItem(
        cycle_id=cycle.id,
        compound_key="synthetic",
        unit="mg",
        schedule=[],
        subject_id=None,
    )
    db_session.add(child)
    await db_session.flush()
    await preflight_normalized_ownership_backfill(db_session)
    assert child.subject_id is None
    await db_session.rollback()

    owner, subject = await _ready_scope(db_session)
    compound = HrtCompound(
        domain=Domain.HRT.value,
        source=Source.SYSTEM.value,
        key="synthetic",
        name="Synthetic",
        compound_class="other",
        route="oral",
        subject_id=subject.id,
        actor_user_id=owner.id,
    )
    db_session.add(compound)
    await db_session.flush()
    from vitals.models.hrt import HrtDose

    db_session.add(
        HrtDose(
            date=date(2026, 8, 2),
            domain=Domain.HRT.value,
            source=Source.MANUAL.value,
            compound_id=compound.id,
            compound_key=compound.key,
            dose=1,
            unit="mg",
        )
    )
    await db_session.flush()
    with pytest.raises(NormalizedOwnershipBackfillStateError, match="globally"):
        await preflight_normalized_ownership_backfill(db_session)
    assert compound.subject_id == subject.id


@pytest.mark.asyncio
async def test_duplicate_future_scoped_keys_fail_closed(db_session):
    await _ready_scope(db_session)
    db_session.add_all(
        [
            SkincareLog(
                date=date(2026, 8, 1),
                domain=Domain.SKINCARE.value,
                source=Source.MANUAL.value,
            ),
            SkincareLog(
                date=date(2026, 8, 1),
                domain=Domain.SKINCARE.value,
                source=Source.MANUAL.value,
            ),
        ]
    )
    await db_session.flush()
    with pytest.raises(NormalizedOwnershipBackfillStateError, match="duplicate"):
        await preflight_normalized_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_restore_reset_exact_catalog_bounds_flush_only_and_rollback(db_session):
    _owner, subject = await _ready_scope(db_session)
    bounds = _empty_bounds()
    bounds["hrt_cycles"] = (5, 2)
    await reset_normalized_manual_backfill_for_portability_v1_restore(
        db_session,
        snapshot_bounds=bounds,
    )

    checkpoints = list(
        await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
                )
            )
        )
    )
    assert len(checkpoints) == 17
    by_phase = {checkpoint.phase_key: checkpoint for checkpoint in checkpoints}
    cycle = by_phase[NORMALIZED_MANUAL_CHECKPOINT_PHASES["hrt_cycles"]]
    assert cycle.status == "running"
    assert (cycle.scan_high_watermark_id, cycle.snapshot_rows) == (5, 2)
    empty = by_phase[NORMALIZED_MANUAL_CHECKPOINT_PHASES["supplements"]]
    assert empty.status == "completed"
    assert empty.completed_at is not None
    assert empty.subject_id == subject.id

    await db_session.rollback()
    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(OwnershipBackfillCheckpoint)
                .where(
                    OwnershipBackfillCheckpoint.phase_key.in_(
                        tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
                    )
                )
            )
            or 0
        )
        == 0
    )


@pytest.mark.asyncio
async def test_restore_reset_accepts_restore_blocked_raw_dependency(db_session):
    _owner, subject = await _scope(db_session)
    db_session.add(
        _checkpoint(
            phase=RAW_OWNERSHIP_BACKFILL_PHASE,
            subject_id=subject.id,
            status="restore_blocked",
            high_watermark=3,
            snapshot_rows=2,
        )
    )
    await db_session.flush()
    await reset_normalized_manual_backfill_for_portability_v1_restore(
        db_session,
        snapshot_bounds=_empty_bounds(),
    )
    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(OwnershipBackfillCheckpoint)
                .where(
                    OwnershipBackfillCheckpoint.phase_key.in_(
                        tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
                    )
                )
            )
            or 0
        )
        == 17
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda bounds: bounds.pop("supplements"),
        lambda bounds: bounds.__setitem__("extra", (0, 0)),
        lambda bounds: bounds.__setitem__("hrt_cycles", (True, 1)),
        lambda bounds: bounds.__setitem__("hrt_cycles", (1 << 31, 1)),
        lambda bounds: bounds.__setitem__("hrt_cycles", (1, 2)),
        lambda bounds: bounds.__setitem__("hrt_cycles", (2, 0)),
    ],
)
@pytest.mark.asyncio
async def test_restore_reset_rejects_invalid_bounds_before_writes(db_session, mutate):
    await _ready_scope(db_session)
    bounds = _empty_bounds()
    mutate(bounds)
    with pytest.raises(NormalizedOwnershipBackfillValidationError):
        await reset_normalized_manual_backfill_for_portability_v1_restore(
            db_session,
            snapshot_bounds=bounds,
        )
    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(OwnershipBackfillCheckpoint)
                .where(
                    OwnershipBackfillCheckpoint.phase_key.like(
                        f"{NORMALIZED_MANUAL_BACKFILL_PHASE}.%"
                    )
                )
            )
            or 0
        )
        == 0
    )


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, 1001])
@pytest.mark.asyncio
async def test_batch_size_is_bounded(db_session, batch_size):
    await _ready_scope(db_session)
    with pytest.raises(NormalizedOwnershipBackfillValidationError):
        await run_normalized_ownership_backfill_batch(
            db_session, batch_size=batch_size
        )
    assert MAX_NORMALIZED_OWNERSHIP_BACKFILL_BATCH_SIZE == 1000


def test_fixed_catalog_phase_mapping_is_complete_immutable_and_bounded():
    assert tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES) == NORMALIZED_MANUAL_TABLES
    assert all(
        phase == f"{NORMALIZED_MANUAL_BACKFILL_PHASE}.{table_name}"
        and len(phase) <= 64
        for table_name, phase in NORMALIZED_MANUAL_CHECKPOINT_PHASES.items()
    )
    with pytest.raises(TypeError):
        NORMALIZED_MANUAL_CHECKPOINT_PHASES["extra"] = "forbidden"  # type: ignore[index]
