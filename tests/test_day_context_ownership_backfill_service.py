"""Focused SQLite/PostgreSQL contracts for Stage-3I day-context ownership."""
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
)
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.signals import DayContext
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import day_context_ownership_backfill_service as service
from vitals.services import signals_service
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
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


async def _ready(session, roots):
    checkpoints = [_checkpoint(phase, roots.subject_id) for phase in _PRIOR_PHASES]
    session.add_all(checkpoints)
    await session.flush()
    return {row.phase_key: row for row in checkpoints}


def _legacy(
    *,
    on_date: date = date(2026, 1, 2),
    source: str = Source.MANUAL.value,
    answers: object | None = None,
    planned: object | None = None,
    subject_id=None,
    actor_user_id=None,
    integration_connection_id=None,
) -> DayContext:
    return DayContext(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        integration_connection_id=integration_connection_id,
        date=on_date,
        domain=Domain.SIGNALS.value,
        source=source,
        answers={"remote": True} if answers is None else answers,
        planned=planned,
    )


async def _recipient(session, roots, *, status="active"):
    connection = IntegrationConnection(
        subject_id=roots.subject_id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator=f"synthetic:{uuid.uuid4()}",
        status=status,
        retired_at=(
            _STAMP
            if status == IntegrationConnectionStatus.RETIRED.value
            else None
        ),
    )
    session.add(connection)
    await session.flush()
    return connection


async def _finish(session, *, batch_size=250):
    for _ in range(10):
        result = await service.run_day_context_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3I did not complete")


def test_public_contract_is_fixed():
    assert service.DAY_CONTEXT_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.channel_optional.day_context.v1"
    )
    assert service.DAY_CONTEXT_OWNERSHIP_BACKFILL_TABLES == ("day_context",)
    assert tuple(service.DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        "day_context",
    )
    assert [status.value for status in service.DayContextOwnershipBackfillStatus] == [
        "not_started",
        "running",
        "completed",
    ]
    with pytest.raises(TypeError):
        service.DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_legacy_rows_gain_only_subject_and_preserve_data(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    row = _legacy(planned={"gym": False})
    db_session.add(row)
    await db_session.flush()
    original = (
        row.actor_user_id,
        row.integration_connection_id,
        row.date,
        row.domain,
        row.source,
        row.answers,
        row.planned,
        row.created_at,
        row.updated_at,
    )

    result = await _finish(db_session)
    await db_session.refresh(row)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert (
        row.actor_user_id,
        row.integration_connection_id,
        row.date,
        row.domain,
        row.source,
        row.answers,
        row.planned,
        row.created_at,
        row.updated_at,
    ) == original
    assert "subject_id" not in result.to_safe_dict()


@pytest.mark.asyncio
async def test_historical_owned_actor_and_retired_recipient_are_preserved(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    recipient = await _recipient(
        db_session, legacy_owner_roots, status=IntegrationConnectionStatus.RETIRED.value
    )
    row = _legacy(
        source=Source.TELEGRAM.value,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=recipient.id,
    )
    db_session.add(row)
    await db_session.flush()

    result = await _finish(db_session)

    assert result.completed and result.unchanged_rows == 1
    assert row.actor_user_id == legacy_owner_roots.user_id
    assert row.integration_connection_id == recipient.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda row, roots: setattr(row, "actor_user_id", roots.user_id), service.DayContextOwnershipBackfillStateError),
        (lambda row, roots: setattr(row, "domain", Domain.SYSTEM.value), service.DayContextOwnershipBackfillProvenanceError),
        (lambda row, roots: setattr(row, "source", Source.SYSTEM.value), service.DayContextOwnershipBackfillProvenanceError),
        (lambda row, roots: setattr(row, "answers", []), service.DayContextOwnershipBackfillProvenanceError),
        (lambda row, roots: setattr(row, "planned", []), service.DayContextOwnershipBackfillProvenanceError),
    ],
)
async def test_partial_roots_and_bad_provenance_fail_closed(
    db_session, legacy_owner_roots, mutate, error
):
    await _ready(db_session, legacy_owner_roots)
    row = _legacy()
    mutate(row, legacy_owner_roots)
    db_session.add(row)
    await db_session.flush()
    with pytest.raises(error):
        await service.preflight_day_context_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_requires_exact_source_actor_recipient_shape(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    first = _legacy()
    db_session.add(first)
    await db_session.flush()
    await service.run_day_context_ownership_backfill_batch(db_session, batch_size=1)

    bad = _legacy(
        on_date=date(2026, 1, 3),
        source=Source.TELEGRAM.value,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(bad)
    await db_session.flush()
    with pytest.raises(service.DayContextOwnershipBackfillProvenanceError):
        await service.preflight_day_context_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_completed_live_tail_accepts_mcp_overwrite_preserving_telegram_recipient(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_legacy())
    await db_session.flush()
    await _finish(db_session)
    recipient = await _recipient(db_session, legacy_owner_roots)
    identity = WriteIdentity(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )
    live = await signals_service.set_day_context(
        db_session,
        date(2026, 1, 3),
        answers={"telegram": True},
        source=Source.TELEGRAM.value,
        identity=identity,
        integration_connection_id=recipient.id,
    )
    await signals_service.set_day_context(
        db_session,
        live.date,
        answers={"mcp": True},
        source=Source.MCP.value,
        identity=identity,
    )

    result = await service.preflight_day_context_ownership_backfill(db_session)

    assert result.completed and result.rows_above_high_watermark == 1
    assert live.source == Source.MCP.value
    assert live.actor_user_id == legacy_owner_roots.user_id
    assert live.integration_connection_id == recipient.id

    recipient.status = IntegrationConnectionStatus.DISABLED.value
    await db_session.flush()
    assert (await service.preflight_day_context_ownership_backfill(db_session)).completed
    recipient.status = IntegrationConnectionStatus.RETIRED.value
    recipient.retired_at = _STAMP
    await db_session.flush()
    assert (await service.preflight_day_context_ownership_backfill(db_session)).completed


@pytest.mark.asyncio
async def test_full_lock_scan_pages_connection_materialization(
    db_session, legacy_owner_roots, monkeypatch
):
    await _ready(db_session, legacy_owner_roots)
    rows = []
    for day in (2, 3, 4):
        recipient = await _recipient(db_session, legacy_owner_roots)
        rows.append(
            _legacy(
                on_date=date(2026, 1, day),
                source=Source.TELEGRAM.value,
                subject_id=legacy_owner_roots.subject_id,
                actor_user_id=legacy_owner_roots.user_id,
                integration_connection_id=recipient.id,
            )
        )
    db_session.add_all(rows)
    await db_session.flush()
    projected_sizes: list[int] = []
    original = service._project_connections

    async def tracked(session, connection_ids):
        projected_sizes.append(len(connection_ids))
        assert len(connection_ids) <= 2
        return await original(session, connection_ids)

    monkeypatch.setattr(service, "_PAGE_SIZE", 2)
    monkeypatch.setattr(service, "_project_connections", tracked)

    result = await _finish(db_session, batch_size=2)

    assert result.completed
    assert projected_sizes
    assert max(projected_sizes) == 2
    assert 1 in projected_sizes


@pytest.mark.asyncio
async def test_stop_resume_and_finalization_detects_frozen_data_drift(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    rows = [_legacy(on_date=date(2026, 1, day)) for day in (2, 3)]
    db_session.add_all(rows)
    await db_session.flush()
    first = await service.run_day_context_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert first.status is service.DayContextOwnershipBackfillStatus.RUNNING
    rows[0].answers = {"changed": True}
    await db_session.flush()
    with pytest.raises(service.DayContextOwnershipBackfillStateError, match="finalization"):
        await service.run_day_context_ownership_backfill_batch(
            db_session, batch_size=1
        )


@pytest.mark.asyncio
async def test_completed_rows_allow_in_place_overwrite_but_not_frozen_deletion(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    row = _legacy()
    db_session.add(row)
    await db_session.flush()
    await _finish(db_session)

    row.answers = {"remote": False, "load": "heavy"}
    row.source = Source.TEMPLATE.value
    row.actor_user_id = None
    await db_session.flush()
    assert (await service.preflight_day_context_ownership_backfill(db_session)).completed

    await db_session.delete(row)
    await db_session.flush()
    with pytest.raises(service.DayContextOwnershipBackfillStateError, match="cardinality"):
        await service.preflight_day_context_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_nonempty_portability_reset_accepts_imported_subject_only_rows(
    db_session, legacy_owner_roots
):
    checkpoints = await _ready(db_session, legacy_owner_roots)
    restore_nonempty = (
        (RAW_OWNERSHIP_BACKFILL_PHASE,)
        + tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
        + tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    )
    restore_blocked = (
        tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    )
    for phase in restore_nonempty:
        row = checkpoints[phase]
        row.status = "running"
        row.scan_high_watermark_id = row.snapshot_rows = 1
        row.completed_at = None
    checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE].status = "restore_blocked"
    for phase in restore_blocked:
        row = checkpoints[phase]
        row.status = "restore_blocked"
        row.scan_high_watermark_id = row.snapshot_rows = 1
        row.completed_at = None
    await db_session.flush()

    await service.reset_day_context_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"day_context": (7, 1)}
    )
    imported = _legacy(subject_id=legacy_owner_roots.subject_id)
    imported.id = 7
    db_session.add(imported)
    await db_session.flush()

    preflight = await service.preflight_day_context_ownership_backfill(db_session)
    assert preflight.status is service.DayContextOwnershipBackfillStatus.RUNNING
    final = await _finish(db_session)
    assert final.completed and final.updated_rows == 0 and final.unchanged_rows == 1


@pytest.mark.asyncio
async def test_empty_portability_reset_completes_and_bounds_are_strict(
    db_session, legacy_owner_roots
):
    checkpoints = await _ready(db_session, legacy_owner_roots)
    for phase, row in checkpoints.items():
        if phase in (
            (RAW_OWNERSHIP_BACKFILL_PHASE,)
            + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
            + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
            + tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        ):
            # Empty COMPLETED is the exact canonical empty restore state.
            continue
    await service.reset_day_context_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"day_context": (0, 0)}
    )
    assert (await service.preflight_day_context_ownership_backfill(db_session)).completed
    with pytest.raises(service.DayContextOwnershipBackfillValidationError):
        await service.reset_day_context_ownership_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds={"day_context": (1, 0)}
        )


@pytest.mark.asyncio
async def test_restore_rejects_partially_advanced_prior_running_checkpoint(
    db_session, legacy_owner_roots
):
    checkpoints = await _ready(db_session, legacy_owner_roots)
    for phase in (
        (RAW_OWNERSHIP_BACKFILL_PHASE,)
        + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    ):
        row = checkpoints[phase]
        row.status = "restore_blocked"
        row.scan_high_watermark_id = row.snapshot_rows = 1
        row.completed_at = None
    normalized = checkpoints[next(iter(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values()))]
    normalized.status = "running"
    normalized.scan_high_watermark_id = normalized.snapshot_rows = 2
    normalized.last_scanned_id = normalized.scanned_rows = normalized.updated_rows = 1
    normalized.unchanged_rows = 0
    normalized.completed_at = None
    await db_session.flush()

    with pytest.raises(service.DayContextOwnershipBackfillDependencyError):
        await service.reset_day_context_ownership_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds={"day_context": (1, 1)}
        )


@pytest.mark.asyncio
async def test_dependency_and_batch_validation_fail_closed(db_session, legacy_owner_roots):
    with pytest.raises(service.DayContextOwnershipBackfillDependencyError):
        await service.preflight_day_context_ownership_backfill(db_session)
    with pytest.raises(service.DayContextOwnershipBackfillValidationError):
        await service.run_day_context_ownership_backfill_batch(
            db_session, batch_size=True
        )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["row_change", "row_delete", "connection_change"])
async def test_postgres_projected_graph_races_roll_back_without_progress(
    db_session, legacy_owner_roots, monkeypatch, race
):
    await _ready(db_session, legacy_owner_roots)
    connection = None
    if race == "connection_change":
        connection = await _recipient(db_session, legacy_owner_roots)
        row = _legacy(
            source=Source.TELEGRAM.value,
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
            integration_connection_id=connection.id,
        )
    else:
        row = _legacy()
    db_session.add(row)
    await db_session.commit()
    row_id = row.id
    connection_id = connection.id if connection is not None else None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)
    projected = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(writer_done.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_day_context_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_day_context_ownership_backfill_batch(
                    session, batch_size=1000
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
            if race == "row_change":
                await writer.execute(
                    update(DayContext)
                    .where(DayContext.id == row_id)
                    .values(answers={"raced": True})
                )
            elif race == "row_delete":
                await writer.execute(delete(DayContext).where(DayContext.id == row_id))
            else:
                await writer.execute(
                    update(IntegrationConnection)
                    .where(IntegrationConnection.id == connection_id)
                    .values(status=IntegrationConnectionStatus.DISABLED.value)
                )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=15)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert isinstance(error, service.DayContextOwnershipBackfillStateError)
    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            service.DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["day_context"],
        )
        assert checkpoint is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_writer_serializes_on_subject_lock(
    db_session, legacy_owner_roots, monkeypatch
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_legacy())
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)
    projected = asyncio.Event()
    release = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(release.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_day_context_projection_for_test", pause)

    async def backfill_worker():
        async with factory() as session:
            result = await service.run_day_context_ownership_backfill_batch(
                session, batch_size=1000
            )
            await session.commit()
            return result

    async def writer_worker():
        async with factory() as session:
            row = await signals_service.set_day_context(
                session,
                date(2026, 1, 3),
                answers={"live": True},
                source=Source.MCP.value,
                identity=WriteIdentity(
                    subject_id=legacy_owner_roots.subject_id,
                    actor_user_id=legacy_owner_roots.user_id,
                ),
            )
            await session.commit()
            return row.id

    backfill = asyncio.create_task(backfill_worker())
    await asyncio.wait_for(projected.wait(), timeout=15)
    writer = asyncio.create_task(writer_worker())
    await asyncio.sleep(0.1)
    assert not writer.done()
    release.set()
    result = await asyncio.wait_for(backfill, timeout=15)
    new_id = await asyncio.wait_for(writer, timeout=15)

    assert result.completed
    async with factory() as verify:
        live = await verify.get(DayContext, new_id)
        assert live.subject_id == legacy_owner_roots.subject_id
        assert live.actor_user_id == legacy_owner_roots.user_id
        assert live.source == Source.MCP.value
