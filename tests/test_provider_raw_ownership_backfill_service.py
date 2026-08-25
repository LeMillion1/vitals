"""Focused contracts for the fixed Stage-3D provider/raw backfill."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from tests.conftest import legacy_unenforced_write
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from vitals.enums import (
    Domain,
    IntegrationProvider,
    Source,
)
from vitals.models.garmin import GarminActivity, GarminDaily, GarminIntraday
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    MAX_PROVIDER_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE,
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
    PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES,
    ProviderRawOwnershipBackfillDependencyError,
    ProviderRawOwnershipBackfillProvenanceError,
    ProviderRawOwnershipBackfillStateError,
    ProviderRawOwnershipBackfillStatus,
    ProviderRawOwnershipBackfillValidationError,
    block_provider_raw_ownership_backfill_for_portability_v1_restore,
    preflight_provider_raw_ownership_backfill,
    run_provider_raw_ownership_backfill_batch,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots


# Every test here writes or inspects a row with no owner, which is the whole
# subject of the ownership backfill: these services exist to give such rows an
# owner. The application can no longer produce that state, so this module asks
# for the schema as it stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


PASSWORD_HASH = (
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
)
EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)


async def _roots(session):
    identity = await bootstrap_legacy_owner(
        session,
        username="provider-stage3d-owner",
        password_hash=PASSWORD_HASH,
        timezone="Asia/Almaty",
    )
    await bootstrap_legacy_resource_roots(
        session,
        subject_id=identity.subject_id,
    )
    connections = {
        row.provider: row
        for row in await session.scalars(select(IntegrationConnection))
    }
    return identity, connections


def _checkpoint(
    *,
    phase: str,
    subject_id,
    status: str = "completed",
    high_watermark: int = 0,
    snapshot_rows: int = 0,
) -> OwnershipBackfillCheckpoint:
    completed = status == "completed"
    timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return OwnershipBackfillCheckpoint(
        phase_key=phase,
        subject_id=subject_id,
        status=status,
        scan_high_watermark_id=high_watermark,
        snapshot_rows=snapshot_rows,
        last_scanned_id=high_watermark if completed else 0,
        scanned_rows=snapshot_rows if completed else 0,
        updated_rows=0,
        unchanged_rows=snapshot_rows if completed else 0,
        data_checksum_before=EMPTY_SHA256,
        data_checksum_after=EMPTY_SHA256,
        ownership_checksum_after=EMPTY_SHA256,
        # These are prerequisite fixtures, not a test of server defaults. Give
        # all lifecycle fields one stable historical timestamp so SQLite's
        # second-precision CURRENT_TIMESTAMP cannot land before an application
        # timestamp near the next-second boundary on insert or later update.
        started_at=timestamp,
        updated_at=timestamp,
        completed_at=timestamp if completed else None,
    )


async def _complete_prior_dependencies(session, *, subject_id) -> None:
    phases = (
        RAW_OWNERSHIP_BACKFILL_PHASE,
        *NORMALIZED_MANUAL_CHECKPOINT_PHASES.values(),
        *HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values(),
    )
    session.add_all(
        [_checkpoint(phase=phase, subject_id=subject_id) for phase in phases]
    )
    await session.flush()


async def _raw(
    session,
    *,
    identity,
    connection,
    domain: str,
    source: str,
    external_id: str,
    payload: dict,
    actor=True,
) -> RawPayload:
    row = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.user_id if actor else None,
        integration_connection_id=connection.id,
        domain=domain,
        source=source,
        external_id=external_id,
        payload=payload,
        processed_at=datetime(2026, 8, 20, 12, 0),
    )
    session.add(row)
    await session.flush()
    return row


async def _seed_reviewed_graph(session):
    identity, connections = await _roots(session)
    await _complete_prior_dependencies(session, subject_id=identity.subject_id)
    garmin = connections[IntegrationProvider.GARMIN.value]
    hevy = connections[IntegrationProvider.HEVY.value]
    raw_daily = await _raw(
        session,
        identity=identity,
        connection=garmin,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        external_id="daily:2026-08-01",
        payload={"calendarDate": "2026-08-01"},
    )
    raw_activity = await _raw(
        session,
        identity=identity,
        connection=garmin,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        external_id="activity:activity-1",
        payload={"activityId": "activity-1"},
        actor=False,
    )
    raw_intraday = await _raw(
        session,
        identity=identity,
        connection=garmin,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        external_id="daily:2026-08-02",
        payload={"calendarDate": "2026-08-02"},
        actor=False,
    )
    raw_hevy = await _raw(
        session,
        identity=identity,
        connection=hevy,
        domain=Domain.WORKOUTS.value,
        source=Source.HEVY_API.value,
        external_id="hevy-1",
        payload={"id": "hevy-1"},
    )
    timestamp = datetime(2026, 8, 20, 9, 15)
    daily = GarminDaily(
        date=date(2026, 8, 1),
        domain=Domain.GARMIN.value,
        source=Source.HEALTH_AUTO_EXPORT.value,
        raw_payload_id=raw_daily.id,
        actor_user_id=None,
        steps=12_345,
        created_at=timestamp,
        updated_at=timestamp,
    )
    activity = GarminActivity(
        subject_id=identity.subject_id,
        integration_connection_id=garmin.id,
        external_id="activity-1",
        date=date(2026, 8, 3),
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        raw_payload_id=raw_activity.id,
        actor_user_id=identity.user_id,
        name="Synthetic activity",
        created_at=timestamp,
        updated_at=timestamp,
    )
    intraday = GarminIntraday(
        date=date(2026, 8, 2),
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        raw_payload_id=raw_intraday.id,
        series_type="stress",
        ts=datetime(2026, 8, 2, 10, 0),
        value=21.0,
        created_at=timestamp,
        updated_at=timestamp,
    )
    workout = HevyWorkout(
        external_id="hevy-1",
        date=date(2026, 8, 4),
        domain=Domain.WORKOUTS.value,
        source=Source.HEVY_API.value,
        raw_payload_id=raw_hevy.id,
        actor_user_id=None,
        title="Synthetic workout",
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add_all([daily, activity, intraday, workout])
    await session.flush()
    # A half-migrated Hevy child graph: the child already carries the subject
    # its unowned parent does not.  That is exactly what the Stage-4
    # constraints forbid going forward, so the historical shape has to be
    # written unenforced.
    async with legacy_unenforced_write(session):
        exercise = HevyExercise(
            workout_id=workout.id,
            subject_id=identity.subject_id,
            integration_connection_id=None,
            exercise_index=0,
            title="Synthetic exercise",
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(exercise)
        await session.flush()
        session.add(
            HevySet(
                exercise_id=exercise.id,
                subject_id=None,
                integration_connection_id=None,
                set_index=0,
                set_type="normal",
                reps=8,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
    return identity, connections, (daily, activity, intraday, workout), timestamp


async def _finish(session, *, batch_size: int = 1):
    result = None
    for _ in range(20):
        result = await run_provider_raw_ownership_backfill_batch(
            session,
            batch_size=batch_size,
        )
        await session.commit()
        if result.completed:
            return result
    raise AssertionError("Stage-3D did not complete within its fixed table bound")


def test_public_catalog_and_safe_projection_are_frozen():
    assert PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE == "stage3.provider_raw_linked.v1"
    assert PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES == (
        "garmin_daily",
        "garmin_activities",
        "garmin_intraday",
        "hevy_workouts",
    )
    assert tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        *PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES,
    )
    assert set(ProviderRawOwnershipBackfillStatus) == {
        ProviderRawOwnershipBackfillStatus.NOT_STARTED,
        ProviderRawOwnershipBackfillStatus.RUNNING,
        ProviderRawOwnershipBackfillStatus.COMPLETED,
        ProviderRawOwnershipBackfillStatus.RESTORE_BLOCKED,
    }


@pytest.mark.asyncio
async def test_stop_resume_preserves_data_timestamps_and_never_writes_actor(
    db_session,
):
    identity, connections, rows, timestamp = await _seed_reviewed_graph(db_session)

    status = await preflight_provider_raw_ownership_backfill(db_session)
    assert status.status is ProviderRawOwnershipBackfillStatus.NOT_STARTED
    assert status.snapshot_rows == 4
    assert status.remaining_rows == 4

    first = await run_provider_raw_ownership_backfill_batch(
        db_session, batch_size=1
    )
    await db_session.commit()
    assert first.status is ProviderRawOwnershipBackfillStatus.RUNNING
    assert first.batch_table == "garmin_daily"
    assert first.batch_updated_rows == 1

    completed = await _finish(db_session)
    assert completed.status is ProviderRawOwnershipBackfillStatus.COMPLETED
    assert completed.updated_rows == 3
    assert completed.unchanged_rows == 1
    assert completed.remaining_rows == 0

    for row in rows:
        await db_session.refresh(row)
        expected = (
            connections[IntegrationProvider.HEVY.value].id
            if isinstance(row, HevyWorkout)
            else connections[IntegrationProvider.GARMIN.value].id
        )
        assert row.subject_id == identity.subject_id
        assert row.integration_connection_id == expected
        assert row.updated_at == timestamp
    assert rows[0].actor_user_id is None
    assert rows[1].actor_user_id == identity.user_id
    assert rows[3].actor_user_id is None

    repeat = await run_provider_raw_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert repeat.completed
    assert repeat.batch_scanned_rows == 0
    assert repeat.batch_updated_rows == 0
    assert len(
        list(
            await db_session.scalars(
                select(OwnershipBackfillCheckpoint).where(
                    OwnershipBackfillCheckpoint.phase_key.in_(
                        PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
                    )
                )
            )
        )
    ) == 4


@pytest.mark.asyncio
async def test_reverse_daily_source_bridge_is_rejected(db_session):
    identity, connections = await _roots(db_session)
    await _complete_prior_dependencies(db_session, subject_id=identity.subject_id)
    raw = await _raw(
        db_session,
        identity=identity,
        connection=connections[IntegrationProvider.GARMIN.value],
        domain=Domain.GARMIN.value,
        source=Source.HEALTH_AUTO_EXPORT.value,
        external_id="hae:2026-08-01",
        payload={"metrics": {}},
    )
    db_session.add(
        GarminDaily(
            date=date(2026, 8, 1),
            domain=Domain.GARMIN.value,
            source=Source.GARMIN_API.value,
            raw_payload_id=raw.id,
        )
    )
    await db_session.flush()
    with pytest.raises(ProviderRawOwnershipBackfillProvenanceError):
        await preflight_provider_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["actor_only", "connection_only"])
async def test_unreviewed_historical_ownership_shapes_fail_closed(
    db_session,
    shape,
):
    identity, connections = await _roots(db_session)
    await _complete_prior_dependencies(db_session, subject_id=identity.subject_id)
    garmin = connections[IntegrationProvider.GARMIN.value]
    raw = await _raw(
        db_session,
        identity=identity,
        connection=garmin,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        external_id="daily:2026-08-01",
        payload={},
        actor=False,
    )
    values = {
        "subject_id": None,
        "integration_connection_id": None,
        "actor_user_id": None,
    }
    if shape == "actor_only":
        values["actor_user_id"] = identity.user_id
    elif shape == "connection_only":
        values["integration_connection_id"] = garmin.id
    db_session.add(
        GarminDaily(
            date=date(2026, 8, 1),
            domain=Domain.GARMIN.value,
            source=Source.GARMIN_API.value,
            raw_payload_id=raw.id,
            **values,
        )
    )
    await db_session.flush()
    with pytest.raises(
        (
            ProviderRawOwnershipBackfillStateError,
            ProviderRawOwnershipBackfillProvenanceError,
        )
    ):
        await preflight_provider_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_hevy_child_foreign_or_connection_only_shape_is_rejected(db_session):
    _identity, connections, rows, _timestamp = await _seed_reviewed_graph(db_session)
    exercise = await db_session.scalar(select(HevyExercise))
    exercise.subject_id = None
    exercise.integration_connection_id = connections[IntegrationProvider.HEVY.value].id
    await db_session.flush()
    with pytest.raises(ProviderRawOwnershipBackfillStateError):
        await preflight_provider_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_root", ["raw", "connection"])
async def test_preflight_refreshes_cached_raw_and_connection_roots(
    db_session,
    cached_root,
):
    _identity, connections, rows, _timestamp = await _seed_reviewed_graph(db_session)
    raw_id = rows[0].raw_payload_id
    cached_raw = await db_session.get(RawPayload, raw_id)
    cached_connection = connections[IntegrationProvider.GARMIN.value]
    assert cached_raw.source == Source.GARMIN_API.value
    assert cached_connection.status == "legacy"

    if cached_root == "raw":
        statement = (
            update(RawPayload)
            .where(RawPayload.id == raw_id)
            .values(source=Source.HEVY_API.value)
        )
    else:
        statement = (
            update(IntegrationConnection)
            .where(IntegrationConnection.id == cached_connection.id)
            .values(status="pending")
        )
    await db_session.execute(
        statement.execution_options(synchronize_session=False)
    )
    await db_session.commit()

    if cached_root == "raw":
        assert cached_raw.source == Source.GARMIN_API.value
    else:
        assert cached_connection.status == "legacy"
    with pytest.raises(ProviderRawOwnershipBackfillProvenanceError):
        await preflight_provider_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_all_stage3_dependencies_are_required(db_session):
    identity, _connections = await _roots(db_session)
    db_session.add(
        _checkpoint(
            phase=RAW_OWNERSHIP_BACKFILL_PHASE,
            subject_id=identity.subject_id,
        )
    )
    await db_session.flush()
    with pytest.raises(ProviderRawOwnershipBackfillDependencyError):
        await preflight_provider_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_restore_block_is_atomic_status_and_apply_fails(db_session):
    identity, _connections = await _roots(db_session)
    await _complete_prior_dependencies(db_session, subject_id=identity.subject_id)
    bounds = {
        "garmin_daily": (10, 2),
        "garmin_activities": (0, 0),
        "garmin_intraday": (12, 5),
        "hevy_workouts": (0, 0),
    }
    await block_provider_raw_ownership_backfill_for_portability_v1_restore(
        db_session,
        snapshot_bounds=bounds,
    )
    await db_session.commit()

    status = await preflight_provider_raw_ownership_backfill(db_session)
    assert status.status is ProviderRawOwnershipBackfillStatus.RESTORE_BLOCKED
    assert status.tables_total == 4
    assert status.completed_tables == 2
    assert status.snapshot_rows == 7
    assert status.remaining_rows == 7
    with pytest.raises(ProviderRawOwnershipBackfillStateError):
        await run_provider_raw_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
async def test_restore_block_accepts_raw_restore_blocked_and_running_prior(
    db_session,
):
    identity, _connections = await _roots(db_session)
    raw = _checkpoint(
        phase=RAW_OWNERSHIP_BACKFILL_PHASE,
        subject_id=identity.subject_id,
        status="restore_blocked",
        high_watermark=4,
        snapshot_rows=4,
    )
    db_session.add(raw)
    phases = (
        *NORMALIZED_MANUAL_CHECKPOINT_PHASES.values(),
        *HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values(),
    )
    db_session.add_all(
        [
            _checkpoint(
                phase=phase,
                subject_id=identity.subject_id,
                status="running",
                high_watermark=1,
                snapshot_rows=1,
            )
            for phase in phases
        ]
    )
    await db_session.flush()
    await block_provider_raw_ownership_backfill_for_portability_v1_restore(
        db_session,
        snapshot_bounds={
            table: (0, 0) for table in PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES
        },
    )
    status = await preflight_provider_raw_ownership_backfill(db_session)
    assert status.status is ProviderRawOwnershipBackfillStatus.COMPLETED


@pytest.mark.asyncio
async def test_empty_restore_blocked_provider_checkpoint_is_malformed(db_session):
    identity, _connections = await _roots(db_session)
    await _complete_prior_dependencies(db_session, subject_id=identity.subject_id)
    db_session.add(
        _checkpoint(
            phase=PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "garmin_daily"
            ],
            subject_id=identity.subject_id,
            status="restore_blocked",
            high_watermark=0,
            snapshot_rows=0,
        )
    )
    await db_session.flush()
    with pytest.raises(ProviderRawOwnershipBackfillStateError):
        await preflight_provider_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_prior_checkpoint_divergent_checksum_is_rejected(db_session):
    identity, _connections = await _roots(db_session)
    await _complete_prior_dependencies(db_session, subject_id=identity.subject_id)
    phase = next(iter(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values()))
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    checkpoint.data_checksum_after = "1" * 64
    await db_session.flush()
    with pytest.raises(ProviderRawOwnershipBackfillDependencyError):
        await preflight_provider_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_completed_intraday_allows_owned_wholesale_replacement(db_session):
    identity, connections, rows, timestamp = await _seed_reviewed_graph(db_session)
    await _finish(db_session)
    old_intraday = rows[2]
    raw_id = old_intraday.raw_payload_id
    await db_session.execute(delete(GarminIntraday))
    replacement = GarminIntraday(
        subject_id=identity.subject_id,
        integration_connection_id=connections[IntegrationProvider.GARMIN.value].id,
        date=date(2026, 8, 2),
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        raw_payload_id=raw_id,
        series_type="stress",
        ts=datetime(2026, 8, 2, 10, 3),
        value=22.0,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db_session.add(replacement)
    await db_session.commit()

    status = await preflight_provider_raw_ownership_backfill(db_session)
    assert status.status is ProviderRawOwnershipBackfillStatus.COMPLETED
    assert status.rows_above_high_watermark >= 1
    repeat = await run_provider_raw_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert repeat.completed
    assert repeat.batch_scanned_rows == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_raw_link_switch_while_waiting_on_connection_fails_closed(
    db_session,
):
    identity, connections, rows, _timestamp = await _seed_reviewed_graph(db_session)
    old_connection = connections[IntegrationProvider.GARMIN.value]
    new_connection = IntegrationConnection(
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type="account",
        external_account_discriminator="synthetic-race-account-v1",
        status="active",
    )
    db_session.add(new_connection)
    await db_session.flush()
    new_raw = await _raw(
        db_session,
        identity=identity,
        connection=new_connection,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        external_id="daily:2026-08-01",
        payload={"calendarDate": "2026-08-01"},
        actor=False,
    )
    daily_id = rows[0].id
    await db_session.commit()

    assert db_session.bind is not None
    engine = db_session.bind
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as blocker:
        await blocker.execute(
            select(IntegrationConnection.id)
            .where(IntegrationConnection.id == old_connection.id)
            .with_for_update()
        )

        pid_ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def run_waiting_backfill():
            async with factory() as worker:
                pid = int(await worker.scalar(text("SELECT pg_backend_pid()")))
                pid_ready.set_result(pid)
                return await run_provider_raw_ownership_backfill_batch(
                    worker,
                    batch_size=1,
                )

        worker_task = asyncio.create_task(run_waiting_backfill())
        worker_pid = await pid_ready
        observed_lock_wait = False
        async with factory() as observer:
            for _ in range(100):
                wait_type = await observer.scalar(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity "
                        "WHERE pid = :pid"
                    ),
                    {"pid": worker_pid},
                )
                if wait_type == "Lock":
                    observed_lock_wait = True
                    break
                await asyncio.sleep(0.02)
        assert observed_lock_wait

        async with factory() as mutator:
            await mutator.execute(
                update(GarminDaily)
                .where(GarminDaily.id == daily_id)
                .values(raw_payload_id=new_raw.id)
            )
            await mutator.commit()
        await blocker.commit()

        with pytest.raises(ProviderRawOwnershipBackfillStateError):
            await worker_task

    async with factory() as verify:
        assert await verify.scalar(
            select(GarminDaily.subject_id).where(GarminDaily.id == daily_id)
        ) is None
        assert not list(
            await verify.scalars(
                select(OwnershipBackfillCheckpoint).where(
                    OwnershipBackfillCheckpoint.phase_key
                    == PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                        "garmin_daily"
                    ]
                )
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, "1", 1001])
async def test_batch_size_is_strictly_bounded(db_session, batch_size):
    with pytest.raises(ProviderRawOwnershipBackfillValidationError):
        await run_provider_raw_ownership_backfill_batch(
            db_session,
            batch_size=batch_size,
        )
    assert MAX_PROVIDER_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE == 1000
