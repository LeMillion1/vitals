"""Focused SQLite/PostgreSQL contracts for Stage-3E Hevy children."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.services import hevy_child_ownership_backfill_service as service
from vitals.services.hevy_child_ownership_backfill_service import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE,
    HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES,
    MAX_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE,
    HevyChildOwnershipBackfillDependencyError,
    HevyChildOwnershipBackfillProvenanceError,
    HevyChildOwnershipBackfillStateError,
    HevyChildOwnershipBackfillStatus,
    HevyChildOwnershipBackfillValidationError,
    block_hevy_child_ownership_backfill_for_portability_v1_restore,
    preflight_hevy_child_ownership_backfill,
    run_hevy_child_ownership_backfill_batch,
)
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_STAMP = datetime(2020, 1, 1, 1, 2, 3, tzinfo=UTC)


def _checkpoint(
    *,
    phase: str,
    subject_id: uuid.UUID,
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
        data_checksum_before=_EMPTY_SHA256,
        data_checksum_after=_EMPTY_SHA256,
        ownership_checksum_after=_EMPTY_SHA256,
        started_at=_STAMP,
        updated_at=_STAMP,
        completed_at=_STAMP if completed else None,
    )


async def _scope(session):
    owner = User(
        username="stage3e-owner",
        normalized_username="stage3e-owner",
        password_hash="$synthetic",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.HEVY.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator="synthetic-stage3e",
        credential_ref="test:hevy",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
    phases = (
        (RAW_OWNERSHIP_BACKFILL_PHASE,)
        + tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
        + tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    )
    session.add_all(
        [_checkpoint(phase=phase, subject_id=subject.id) for phase in phases]
    )
    await session.flush()
    return owner, subject, connection


async def _workout(
    session,
    *,
    owner,
    subject,
    connection,
    external_id: str = "workout-1",
) -> HevyWorkout:
    raw = RawPayload(
        subject_id=subject.id,
        actor_user_id=owner.id,
        integration_connection_id=connection.id,
        domain=Domain.WORKOUTS.value,
        source=Source.HEVY_API.value,
        external_id=external_id,
        payload={"id": external_id, "exercises": []},
    )
    session.add(raw)
    await session.flush()
    workout = HevyWorkout(
        subject_id=subject.id,
        actor_user_id=owner.id,
        integration_connection_id=connection.id,
        external_id=external_id,
        raw_payload_id=raw.id,
        date=date(2026, 8, 20),
        domain=Domain.WORKOUTS.value,
        source=Source.HEVY_API.value,
    )
    session.add(workout)
    await session.flush()
    checkpoint = await session.get(
        OwnershipBackfillCheckpoint,
        PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_workouts"],
    )
    checkpoint.scan_high_watermark_id = workout.id
    checkpoint.snapshot_rows = 1
    checkpoint.last_scanned_id = workout.id
    checkpoint.scanned_rows = 1
    checkpoint.unchanged_rows = 1
    await session.flush()
    return workout


async def _graph(session, *, owner, subject, connection):
    workout = await _workout(
        session,
        owner=owner,
        subject=subject,
        connection=connection,
    )
    exercise = HevyExercise(
        workout_id=workout.id,
        exercise_index=0,
        title="Bench",
        exercise_template_id="bench",
    )
    session.add(exercise)
    await session.flush()
    hevy_set = HevySet(
        exercise_id=exercise.id,
        set_index=0,
        set_type="normal",
        weight_kg=80.0,
        reps=8,
    )
    session.add(hevy_set)
    await session.flush()
    return workout, exercise, hevy_set


async def _finish(session, *, size: int = 1):
    for _ in range(10):
        result = await run_hevy_child_ownership_backfill_batch(
            session, batch_size=size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3E did not complete")


def test_fixed_public_contract_is_exact_and_safe():
    assert HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.inherited_children.hevy.v1"
    )
    assert HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES == (
        "hevy_exercises",
        "hevy_sets",
    )
    assert tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES
    )
    assert all(
        phase == f"{HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE}.{table}"
        and len(phase) <= 64
        for table, phase in HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.items()
    )
    with pytest.raises(TypeError):
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["extra"] = "bad"  # type: ignore[index]


@pytest.mark.asyncio
async def test_requires_every_prior_checkpoint_and_preflight_is_read_only(db_session):
    _owner, subject, _connection = await _scope(db_session)
    missing = PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["garmin_daily"]
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key == missing
        )
    )
    with pytest.raises(HevyChildOwnershipBackfillDependencyError):
        await preflight_hevy_child_ownership_backfill(db_session)
    assert not list(
        await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
                )
            )
        )
    )
    assert subject.id is not None


@pytest.mark.asyncio
async def test_partial_own_checkpoint_group_fails_every_service_boundary(db_session):
    _owner, subject, _connection = await _scope(db_session)
    db_session.add(
        _checkpoint(
            phase=HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "hevy_exercises"
            ],
            subject_id=subject.id,
            status="running",
        )
    )
    await db_session.flush()

    with pytest.raises(HevyChildOwnershipBackfillStateError, match="partial"):
        await preflight_hevy_child_ownership_backfill(db_session)
    with pytest.raises(HevyChildOwnershipBackfillStateError, match="partial"):
        await run_hevy_child_ownership_backfill_batch(db_session, batch_size=1)
    with pytest.raises(HevyChildOwnershipBackfillStateError, match="partial"):
        await block_hevy_child_ownership_backfill_for_portability_v1_restore(
            db_session,
            snapshot_bounds={name: (0, 0) for name in HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES},
        )


@pytest.mark.asyncio
async def test_completed_sets_cannot_precede_completed_exercises(db_session):
    _owner, subject, _connection = await _scope(db_session)
    db_session.add_all(
        [
            _checkpoint(
                phase=HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                    "hevy_exercises"
                ],
                subject_id=subject.id,
                status="running",
            ),
            _checkpoint(
                phase=HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                    "hevy_sets"
                ],
                subject_id=subject.id,
            ),
        ]
    )
    await db_session.flush()

    with pytest.raises(
        HevyChildOwnershipBackfillStateError,
        match="order is inconsistent",
    ):
        await preflight_hevy_child_ownership_backfill(db_session)
    with pytest.raises(
        HevyChildOwnershipBackfillStateError,
        match="order is inconsistent",
    ):
        await run_hevy_child_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exercise_status", "set_status"),
    [
        ("completed", "restore_blocked"),
        ("restore_blocked", "running"),
    ],
)
async def test_invalid_restore_checkpoint_order_is_rejected(
    db_session, exercise_status, set_status
):
    _owner, subject, _connection = await _scope(db_session)

    def state_checkpoint(table_name: str, status: str):
        return _checkpoint(
            phase=HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[table_name],
            subject_id=subject.id,
            status=status,
            high=1 if status == "restore_blocked" else 0,
            count=1 if status == "restore_blocked" else 0,
        )

    db_session.add_all(
        [
            state_checkpoint("hevy_exercises", exercise_status),
            state_checkpoint("hevy_sets", set_status),
        ]
    )
    await db_session.flush()
    with pytest.raises(
        HevyChildOwnershipBackfillStateError,
        match="order is inconsistent",
    ):
        await preflight_hevy_child_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_restoreblocked_exercises_allow_only_exact_empty_completed_sets(
    db_session,
):
    owner, subject, connection = await _scope(db_session)
    workout = await _workout(
        db_session,
        owner=owner,
        subject=subject,
        connection=connection,
    )
    exercise = HevyExercise(
        workout_id=workout.id,
        subject_id=subject.id,
        exercise_index=0,
        title="Portable S-only exercise",
    )
    db_session.add(exercise)
    await db_session.flush()
    db_session.add_all(
        [
            _checkpoint(
                phase=HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                    "hevy_exercises"
                ],
                subject_id=subject.id,
                status="restore_blocked",
                high=exercise.id,
                count=1,
            ),
            _checkpoint(
                phase=HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                    "hevy_sets"
                ],
                subject_id=subject.id,
            ),
        ]
    )
    await db_session.flush()

    status = await preflight_hevy_child_ownership_backfill(db_session)
    assert status.status is HevyChildOwnershipBackfillStatus.RESTORE_BLOCKED
    assert status.completed_tables == 1

    set_checkpoint = await db_session.get(
        OwnershipBackfillCheckpoint,
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"],
    )
    set_checkpoint.ownership_checksum_after = "0" * 64
    await db_session.flush()
    with pytest.raises(HevyChildOwnershipBackfillStateError, match="exactly empty"):
        await preflight_hevy_child_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_historical_graph_backfills_in_order_and_preserves_business_data(db_session):
    owner, subject, connection = await _scope(db_session)
    _workout_row, exercise, hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    exercise_created = exercise.created_at
    exercise_updated = exercise.updated_at
    set_created = hevy_set.created_at
    set_updated = hevy_set.updated_at
    db_session.expire(exercise)

    first = await run_hevy_child_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert first.batch_table == "hevy_exercises"
    assert first.batch_updated_rows == 1
    assert (exercise.subject_id, exercise.integration_connection_id) == (
        subject.id,
        connection.id,
    )
    assert (hevy_set.subject_id, hevy_set.integration_connection_id) == (
        None,
        None,
    )
    for phase in HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values():
        assert await db_session.get(OwnershipBackfillCheckpoint, phase) is not None

    final = await _finish(db_session)
    assert final.completed
    assert final.updated_rows == 2
    assert (hevy_set.subject_id, hevy_set.integration_connection_id) == (
        subject.id,
        connection.id,
    )
    assert (exercise.created_at, exercise.updated_at) == (
        exercise_created,
        exercise_updated,
    )
    assert (hevy_set.created_at, hevy_set.updated_at) == (
        set_created,
        set_updated,
    )
    safe = final.to_safe_dict()
    assert "subject_id" not in safe
    assert not any(str(subject.id) in str(value) for value in safe.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["connection_only", "foreign_connection"])
async def test_unsafe_historical_child_shapes_fail_closed(db_session, shape):
    owner, subject, connection = await _scope(db_session)
    _workout_row, exercise, _hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    if shape == "connection_only":
        exercise.integration_connection_id = connection.id
    else:
        alternate = IntegrationConnection(
            subject_id=subject.id,
            provider=IntegrationProvider.HEVY.value,
            connection_type=IntegrationConnectionType.ACCOUNT.value,
            external_account_discriminator="synthetic-foreign-child-c",
            credential_ref="test:hevy-foreign",
            status=IntegrationConnectionStatus.RETIRED.value,
            retired_at=datetime.now(UTC),
        )
        db_session.add(alternate)
        await db_session.flush()
        exercise.subject_id = subject.id
        exercise.integration_connection_id = alternate.id
    await db_session.flush()
    with pytest.raises(HevyChildOwnershipBackfillStateError):
        await run_hevy_child_ownership_backfill_batch(db_session, batch_size=1)
    assert exercise.subject_id is None or shape == "foreign_connection"


@pytest.mark.asyncio
async def test_live_tail_requires_exact_dual_write(db_session):
    owner, subject, connection = await _scope(db_session)
    workout, _exercise, _hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    await run_hevy_child_ownership_backfill_batch(db_session, batch_size=1)
    live = HevyExercise(workout_id=workout.id, exercise_index=1, title="Live")
    db_session.add(live)
    await db_session.flush()
    with pytest.raises(HevyChildOwnershipBackfillStateError, match="high-water"):
        await run_hevy_child_ownership_backfill_batch(db_session, batch_size=1)
    assert live.subject_id is None and live.integration_connection_id is None


@pytest.mark.asyncio
async def test_parent_raw_payload_tamper_fails_before_mutation(db_session):
    owner, subject, connection = await _scope(db_session)
    workout, exercise, _hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    raw = await db_session.get(RawPayload, workout.raw_payload_id)
    raw.payload = {"id": "not-the-workout"}
    await db_session.flush()
    with pytest.raises(HevyChildOwnershipBackfillProvenanceError):
        await run_hevy_child_ownership_backfill_batch(db_session, batch_size=1)
    assert exercise.subject_id is None


@pytest.mark.asyncio
async def test_stale_cached_raw_connection_cannot_hide_persisted_c_drift(db_session):
    owner, subject, connection = await _scope(db_session)
    workout, exercise, _hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    raw = await db_session.get(RawPayload, workout.raw_payload_id)
    assert raw is not None and raw.integration_connection_id == connection.id
    alternate = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.HEVY.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator="synthetic-stale-c",
        credential_ref="test:hevy-alt",
        status=IntegrationConnectionStatus.RETIRED.value,
        retired_at=datetime.now(UTC),
    )
    db_session.add(alternate)
    await db_session.flush()
    await db_session.execute(
        update(RawPayload)
        .where(RawPayload.id == raw.id)
        .values(integration_connection_id=alternate.id)
        .execution_options(synchronize_session=False)
    )
    assert raw.integration_connection_id == connection.id  # deliberately stale
    with pytest.raises(HevyChildOwnershipBackfillProvenanceError):
        await run_hevy_child_ownership_backfill_batch(db_session, batch_size=1)
    assert exercise.subject_id is None


@pytest.mark.asyncio
async def test_completed_group_accepts_exact_rebuild_but_rejects_ownership_drift(
    db_session,
):
    owner, subject, connection = await _scope(db_session)
    workout, exercise, _hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    await _finish(db_session)
    await db_session.execute(
        delete(HevySet).where(HevySet.exercise_id == exercise.id)
    )
    await db_session.execute(delete(HevyExercise).where(HevyExercise.id == exercise.id))
    replacement = HevyExercise(
        workout_id=workout.id,
        subject_id=subject.id,
        integration_connection_id=connection.id,
        exercise_index=2,
        title="Rebuilt",
    )
    db_session.add(replacement)
    await db_session.flush()
    replacement_set = HevySet(
        exercise_id=replacement.id,
        subject_id=subject.id,
        integration_connection_id=connection.id,
        set_index=0,
        set_type="normal",
        reps=5,
    )
    db_session.add(replacement_set)
    await db_session.flush()

    status = await preflight_hevy_child_ownership_backfill(db_session)
    assert status.status is HevyChildOwnershipBackfillStatus.COMPLETED
    repeat = await run_hevy_child_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert repeat.completed and repeat.batch_scanned_rows == 0

    replacement_set.integration_connection_id = None
    await db_session.flush()
    with pytest.raises(HevyChildOwnershipBackfillStateError):
        await preflight_hevy_child_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_partial_group_business_drift_is_rejected_by_final_group_rehash(db_session):
    owner, subject, connection = await _scope(db_session)
    _workout_row, exercise, hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    await run_hevy_child_ownership_backfill_batch(db_session, batch_size=1)
    exercise.title = "Changed during maintenance"
    await db_session.flush()
    set_id = hevy_set.id
    with pytest.raises(HevyChildOwnershipBackfillStateError, match="data changed"):
        async with db_session.begin_nested():
            await run_hevy_child_ownership_backfill_batch(
                db_session, batch_size=1
            )
    assert await db_session.scalar(
        select(HevySet.subject_id).where(HevySet.id == set_id)
    ) is None


@pytest.mark.asyncio
async def test_restore_block_is_exact_and_ordinary_apply_refuses(db_session):
    owner, subject, connection = await _scope(db_session)
    _workout_row, exercise, hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    await block_hevy_child_ownership_backfill_for_portability_v1_restore(
        db_session,
        snapshot_bounds={
            "hevy_exercises": (exercise.id, 1),
            "hevy_sets": (hevy_set.id, 1),
        },
    )
    status = await preflight_hevy_child_ownership_backfill(db_session)
    assert status.status is HevyChildOwnershipBackfillStatus.RESTORE_BLOCKED
    with pytest.raises(HevyChildOwnershipBackfillStateError, match="backup-v1"):
        await run_hevy_child_ownership_backfill_batch(db_session, batch_size=1)
    with pytest.raises(HevyChildOwnershipBackfillValidationError):
        await block_hevy_child_ownership_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds={"hevy_exercises": (0, 0)}
        )


@pytest.mark.asyncio
async def test_restore_rejects_restoreblocked_stage3b_or_stage3c(db_session):
    _owner, subject, _connection = await _scope(db_session)
    phase = NORMALIZED_MANUAL_CHECKPOINT_PHASES["supplements"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    checkpoint.status = "restore_blocked"
    checkpoint.scan_high_watermark_id = 1
    checkpoint.snapshot_rows = 1
    checkpoint.last_scanned_id = 0
    checkpoint.scanned_rows = 0
    checkpoint.updated_rows = 0
    checkpoint.unchanged_rows = 0
    checkpoint.completed_at = None
    await db_session.flush()
    with pytest.raises(HevyChildOwnershipBackfillDependencyError, match="3B/3C"):
        await block_hevy_child_ownership_backfill_for_portability_v1_restore(
            db_session,
            snapshot_bounds={name: (0, 0) for name in HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES},
        )
    assert subject.id is not None


@pytest.mark.asyncio
async def test_batch_size_is_strictly_bounded(db_session):
    for value in (True, 0, MAX_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE + 1):
        with pytest.raises(HevyChildOwnershipBackfillValidationError):
            await run_hevy_child_ownership_backfill_batch(
                db_session, batch_size=value  # type: ignore[arg-type]
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_exercise_workout_fk_switch_fails_before_progress(
    db_session,
    monkeypatch,
):
    owner, subject, connection = await _scope(db_session)
    workout, exercise, _hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    alternate = await _workout(
        db_session,
        owner=owner,
        subject=subject,
        connection=connection,
        external_id="workout-2",
    )
    await db_session.commit()
    exercise_id = exercise.id
    alternate_id = alternate.id
    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    roots_locked = asyncio.Event()
    writer_committed = asyncio.Event()
    paused = False

    async def _pause():
        nonlocal paused
        if paused:
            return
        paused = True
        roots_locked.set()
        await asyncio.wait_for(writer_committed.wait(), timeout=5)

    monkeypatch.setattr(service, "_after_workout_roots_locked_for_test", _pause)

    async def _worker():
        async with factory() as session:
            try:
                await run_hevy_child_ownership_backfill_batch(
                    session, batch_size=1
                )
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()
            return None

    task = asyncio.create_task(_worker())
    error = None
    try:
        await asyncio.wait_for(roots_locked.wait(), timeout=5)
        async with factory() as writer:
            await writer.execute(
                update(HevyExercise)
                .where(HevyExercise.id == exercise_id)
                .values(workout_id=alternate_id)
            )
            await asyncio.wait_for(writer.commit(), timeout=5)
        writer_committed.set()
        error = await asyncio.wait_for(task, timeout=5)
    finally:
        writer_committed.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert isinstance(error, HevyChildOwnershipBackfillStateError)
    assert "parent link changed" in str(error)
    async with factory() as verify:
        persisted = (
            await verify.execute(
                select(
                    HevyExercise.workout_id,
                    HevyExercise.subject_id,
                    HevyExercise.integration_connection_id,
                ).where(HevyExercise.id == exercise_id)
            )
        ).one()
        assert tuple(persisted) == (alternate_id, None, None)
        assert not list(
            await verify.scalars(
                select(OwnershipBackfillCheckpoint).where(
                    OwnershipBackfillCheckpoint.phase_key.in_(
                        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
                    )
                )
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_set_exercise_fk_switch_fails_without_checkpoint_progress(
    db_session,
    monkeypatch,
):
    owner, subject, connection = await _scope(db_session)
    workout, _exercise, hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    alternate = HevyExercise(
        workout_id=workout.id,
        subject_id=subject.id,
        integration_connection_id=connection.id,
        exercise_index=2,
        title="Alternate exact parent",
    )
    db_session.add(alternate)
    await db_session.flush()
    # Freeze both exercises inside the historical exercise phase.  The alternate
    # parent is therefore not a live-tail row locked earlier in the set worker's
    # transaction, allowing the FK switch to commit at the intended race point.
    await run_hevy_child_ownership_backfill_batch(db_session, batch_size=10)
    await db_session.commit()
    set_id = hevy_set.id
    alternate_id = alternate.id
    set_phase = HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"]
    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as before_session:
        before_cp = await before_session.get(OwnershipBackfillCheckpoint, set_phase)
        before_tuple = (
            before_cp.status,
            before_cp.last_scanned_id,
            before_cp.scanned_rows,
            before_cp.updated_rows,
            before_cp.unchanged_rows,
            before_cp.data_checksum_before,
            before_cp.data_checksum_after,
            before_cp.ownership_checksum_after,
        )
    parents_locked = asyncio.Event()
    writer_committed = asyncio.Event()
    paused = False

    async def _pause():
        nonlocal paused
        if paused:
            return
        paused = True
        parents_locked.set()
        await asyncio.wait_for(writer_committed.wait(), timeout=5)

    monkeypatch.setattr(service, "_after_parent_exercises_locked_for_test", _pause)

    async def _worker():
        async with factory() as session:
            try:
                await run_hevy_child_ownership_backfill_batch(
                    session, batch_size=1
                )
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()
            return None

    task = asyncio.create_task(_worker())
    error = None
    try:
        await asyncio.wait_for(parents_locked.wait(), timeout=5)
        async with factory() as writer:
            await writer.execute(
                update(HevySet)
                .where(HevySet.id == set_id)
                .values(exercise_id=alternate_id)
            )
            await asyncio.wait_for(writer.commit(), timeout=5)
        writer_committed.set()
        error = await asyncio.wait_for(task, timeout=5)
    finally:
        writer_committed.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert isinstance(error, HevyChildOwnershipBackfillStateError)
    assert "parent link changed" in str(error)
    async with factory() as verify:
        persisted = (
            await verify.execute(
                select(
                    HevySet.exercise_id,
                    HevySet.subject_id,
                    HevySet.integration_connection_id,
                ).where(HevySet.id == set_id)
            )
        ).one()
        assert tuple(persisted) == (alternate_id, None, None)
        after_cp = await verify.get(OwnershipBackfillCheckpoint, set_phase)
        after_tuple = (
            after_cp.status,
            after_cp.last_scanned_id,
            after_cp.scanned_rows,
            after_cp.updated_rows,
            after_cp.unchanged_rows,
            after_cp.data_checksum_before,
            after_cp.data_checksum_after,
            after_cp.ownership_checksum_after,
        )
        assert after_tuple == before_tuple


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_child_disappearance_after_root_lock_rolls_back_group(
    db_session,
    monkeypatch,
):
    owner, subject, connection = await _scope(db_session)
    workout, exercise, _hevy_set = await _graph(
        db_session, owner=owner, subject=subject, connection=connection
    )
    await db_session.commit()
    exercise_id = exercise.id
    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    roots_locked = asyncio.Event()
    writer_committed = asyncio.Event()
    paused = False
    replacement_id: int | None = None

    async def _pause():
        nonlocal paused
        if paused:
            return
        paused = True
        roots_locked.set()
        await asyncio.wait_for(writer_committed.wait(), timeout=5)

    monkeypatch.setattr(service, "_after_workout_roots_locked_for_test", _pause)

    async def _worker():
        async with factory() as session:
            try:
                await run_hevy_child_ownership_backfill_batch(
                    session, batch_size=1
                )
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()
            return None

    task = asyncio.create_task(_worker())
    error = None
    try:
        await asyncio.wait_for(roots_locked.wait(), timeout=5)
        async with factory() as writer:
            await writer.execute(
                delete(HevySet).where(HevySet.exercise_id == exercise_id)
            )
            await writer.execute(
                delete(HevyExercise).where(HevyExercise.id == exercise_id)
            )
            await asyncio.wait_for(writer.commit(), timeout=5)
        writer_committed.set()
        error = await asyncio.wait_for(task, timeout=5)
        # The root lock correctly serializes insertion of a replacement.  Rebuild
        # immediately after the failed frozen-ID transaction has rolled back.
        async with factory() as writer:
            replacement = HevyExercise(
                workout_id=workout.id,
                subject_id=subject.id,
                integration_connection_id=connection.id,
                exercise_index=0,
                title="Concurrent exact rebuild",
            )
            writer.add(replacement)
            await writer.flush()
            replacement_id = replacement.id
            writer.add(
                HevySet(
                    exercise_id=replacement.id,
                    subject_id=subject.id,
                    integration_connection_id=connection.id,
                    set_index=0,
                    set_type="normal",
                    reps=9,
                )
            )
            await asyncio.wait_for(writer.commit(), timeout=5)
    finally:
        writer_committed.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert isinstance(error, HevyChildOwnershipBackfillStateError)
    async with factory() as verify:
        assert await verify.get(HevyExercise, exercise_id) is None
        assert replacement_id is not None
        rebuilt = await verify.get(HevyExercise, replacement_id)
        assert rebuilt is not None
        assert (rebuilt.subject_id, rebuilt.integration_connection_id) == (
            subject.id,
            connection.id,
        )
        assert not list(
            await verify.scalars(
                select(OwnershipBackfillCheckpoint).where(
                    OwnershipBackfillCheckpoint.phase_key.in_(
                        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
                    )
                )
            )
        )
