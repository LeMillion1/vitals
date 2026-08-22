"""Focused SQLite/PostgreSQL contracts for Stage-3J signal ownership."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import Signal
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import signal_ownership_backfill_service as service
from vitals.services import signals_service
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.day_context_ownership_backfill_service import (
    DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
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
_RAW_STAMP = datetime(2020, 1, 1)
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
    rows = [_checkpoint(phase, roots.subject_id) for phase in _PRIOR_PHASES]
    session.add_all(rows)
    await session.flush()
    return {row.phase_key: row for row in rows}


async def _recipient(session, roots, *, status="active"):
    row = IntegrationConnection(
        subject_id=roots.subject_id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator=f"synthetic:{uuid.uuid4()}",
        status=status,
        retired_at=_STAMP if status == IntegrationConnectionStatus.RETIRED.value else None,
    )
    session.add(row)
    await session.flush()
    return row


def _signal(
    *,
    source=Source.TELEGRAM.value,
    raw_id=None,
    batch_id=None,
    subject_id=None,
    actor_user_id=None,
    integration_connection_id=None,
):
    return Signal(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        integration_connection_id=integration_connection_id,
        date=date(2026, 1, 2),
        domain=Domain.SIGNALS.value,
        source=source,
        kind="symptom",
        key="headache",
        value_num=3.0,
        note="synthetic",
        raw_id=raw_id,
        batch_id=batch_id or uuid.uuid4().hex,
        misparse=False,
    )


def _raw(*, roots, connection=None, actor=True):
    return RawPayload(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id if actor else None,
        integration_connection_id=connection.id if connection is not None else None,
        file_asset_id=None,
        domain=Domain.SIGNALS.value,
        source=Source.TELEGRAM.value,
        external_id=uuid.uuid4().hex,
        payload={"text": "synthetic"},
        processed_at=_RAW_STAMP,
    )


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_signal_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3J did not complete")


def test_public_contract_is_fixed():
    assert service.SIGNAL_OWNERSHIP_BACKFILL_PHASE == "stage3.channel_optional.signals.v1"
    assert service.SIGNAL_OWNERSHIP_BACKFILL_TABLES == ("signals",)
    assert tuple(service.SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == ("signals",)
    assert service.DEFAULT_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE == 250
    assert service.MAX_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE == 1000
    assert [item.value for item in service.SignalOwnershipBackfillStatus] == [
        "not_started", "running", "completed"
    ]
    with pytest.raises(TypeError):
        service.SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_legacy_rawless_rows_gain_only_subject(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    row = _signal()
    db_session.add(row)
    await db_session.flush()
    original = (
        row.actor_user_id, row.integration_connection_id, row.raw_id, row.source,
        row.kind, row.key, row.value_num, row.note, row.batch_id, row.updated_at,
    )

    result = await _finish(db_session)
    await db_session.refresh(row)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert (
        row.actor_user_id, row.integration_connection_id, row.raw_id, row.source,
        row.kind, row.key, row.value_num, row.note, row.batch_id, row.updated_at,
    ) == original
    assert "subject_id" not in result.to_safe_dict()


@pytest.mark.asyncio
async def test_linked_history_preserves_fact_roots_and_validates_raw(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    recipient = await _recipient(
        db_session, legacy_owner_roots, status=IntegrationConnectionStatus.RETIRED.value
    )
    raw = _raw(roots=legacy_owner_roots, connection=recipient, actor=False)
    db_session.add(raw)
    await db_session.flush()
    row = _signal(raw_id=raw.id)
    db_session.add(row)
    await db_session.flush()

    result = await _finish(db_session)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.actor_user_id is None and row.integration_connection_id is None
    assert raw.integration_connection_id == recipient.id


@pytest.mark.asyncio
async def test_legacy_number_kind_is_preserved_but_live_tail_is_strict(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    legacy = _signal()
    legacy.kind = "number"
    db_session.add(legacy)
    await db_session.flush()
    assert (await _finish(db_session)).completed
    assert legacy.kind == "number"

    live = _signal(
        source=Source.MCP.value,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )
    live.kind = "number"
    db_session.add(live)
    await db_session.flush()
    with pytest.raises(service.SignalOwnershipBackfillProvenanceError):
        await service.preflight_signal_ownership_backfill(db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pending", "raw_actor_only", "partial_fact_pair", "rawless_fact_roots"])
async def test_historical_telegram_pair_algebra_is_exact(
    db_session, legacy_owner_roots, case
):
    await _ready(db_session, legacy_owner_roots)
    recipient = await _recipient(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, connection=recipient)
    if case == "pending":
        raw.processed_at = None
    elif case == "raw_actor_only":
        raw.integration_connection_id = None
    db_session.add(raw)
    await db_session.flush()
    if case == "partial_fact_pair":
        row = _signal(
            raw_id=raw.id,
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
        )
    elif case == "rawless_fact_roots":
        row = _signal(
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
            integration_connection_id=recipient.id,
        )
    else:
        row = _signal(raw_id=raw.id)
    db_session.add(row)
    await db_session.flush()
    with pytest.raises(service.SignalOwnershipBackfillProvenanceError):
        await service.preflight_signal_ownership_backfill(db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["partial", "mcp_raw", "wrong_raw", "split_batch", "split_raw"])
async def test_malformed_provenance_fails_closed(db_session, legacy_owner_roots, case):
    await _ready(db_session, legacy_owner_roots)
    if case == "partial":
        db_session.add(_signal(actor_user_id=legacy_owner_roots.user_id))
    elif case == "mcp_raw":
        raw = _raw(roots=legacy_owner_roots)
        db_session.add(raw)
        await db_session.flush()
        db_session.add(_signal(source=Source.MCP.value, raw_id=raw.id))
    elif case == "wrong_raw":
        raw = _raw(roots=legacy_owner_roots)
        raw.domain = Domain.LABS.value
        db_session.add(raw)
        await db_session.flush()
        db_session.add(_signal(raw_id=raw.id))
    elif case == "split_batch":
        batch = uuid.uuid4().hex
        db_session.add_all([_signal(batch_id=batch), _signal(batch_id=batch, source=Source.MCP.value)])
    else:
        raw = _raw(roots=legacy_owner_roots)
        db_session.add(raw)
        await db_session.flush()
        db_session.add_all([_signal(raw_id=raw.id), _signal(raw_id=raw.id)])
    await db_session.flush()

    with pytest.raises(service.SignalOwnershipBackfillError):
        await service.preflight_signal_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_is_strict_and_historical_connection_may_retire(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_signal())
    await db_session.flush()
    await _finish(db_session)

    recipient = await _recipient(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, connection=recipient)
    db_session.add(raw)
    await db_session.flush()
    live = _signal(
        raw_id=raw.id,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=recipient.id,
    )
    db_session.add(live)
    await db_session.flush()
    assert (await service.preflight_signal_ownership_backfill(db_session)).completed

    recipient.status = IntegrationConnectionStatus.RETIRED.value
    recipient.retired_at = _STAMP
    await db_session.flush()
    assert (await service.preflight_signal_ownership_backfill(db_session)).completed

    bad = _signal(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(bad)
    await db_session.flush()
    with pytest.raises(service.SignalOwnershipBackfillProvenanceError):
        await service.preflight_signal_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_completed_checkpoint_is_volatile_for_update_and_delete(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    row = _signal()
    db_session.add(row)
    await db_session.flush()
    await _finish(db_session)
    row.note = "corrected"
    row.misparse = True
    await db_session.flush()
    assert (await service.preflight_signal_ownership_backfill(db_session)).completed
    await db_session.delete(row)
    await db_session.flush()
    assert (await service.preflight_signal_ownership_backfill(db_session)).completed


@pytest.mark.asyncio
async def test_stop_resume_is_idempotent_and_finalization_detects_data_drift(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    rows = [_signal(), _signal()]
    db_session.add_all(rows)
    await db_session.flush()

    first = await service.run_signal_ownership_backfill_batch(db_session, batch_size=1)
    assert first.status is service.SignalOwnershipBackfillStatus.RUNNING
    assert first.scanned_rows == 1 and first.updated_rows == 1
    again = await service.preflight_signal_ownership_backfill(db_session)
    assert again.scanned_rows == 1 and again.remaining_rows == 1

    rows[0].note = "drift"
    await db_session.flush()
    with pytest.raises(service.SignalOwnershipBackfillStateError, match="finalization"):
        await service.run_signal_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
async def test_completed_run_is_idempotent_and_live_mcp_tail_is_valid(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_signal())
    await db_session.flush()
    first = await _finish(db_session)
    second = await service.run_signal_ownership_backfill_batch(db_session)
    assert first.completed and second.completed
    assert second.batch_scanned_rows == second.batch_updated_rows == 0

    live = _signal(
        source=Source.MCP.value,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(live)
    await db_session.flush()
    preflight = await service.preflight_signal_ownership_backfill(db_session)
    assert preflight.completed and preflight.rows_above_high_watermark == 1


@pytest.mark.asyncio
async def test_completed_accepts_late_actorless_reparse_only_from_frozen_raw(
    db_session, legacy_owner_roots
):
    recipient = await _recipient(db_session, legacy_owner_roots)
    frozen_raw = _raw(roots=legacy_owner_roots, connection=recipient, actor=False)
    db_session.add(frozen_raw)
    await db_session.flush()
    checkpoints = await _ready(db_session, legacy_owner_roots)
    raw_checkpoint = checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE]
    raw_checkpoint.scan_high_watermark_id = frozen_raw.id
    raw_checkpoint.snapshot_rows = 1
    raw_checkpoint.last_scanned_id = frozen_raw.id
    raw_checkpoint.scanned_rows = 1
    raw_checkpoint.unchanged_rows = 1
    db_session.add(_signal())
    await db_session.flush()
    await _finish(db_session)

    late = _signal(
        raw_id=frozen_raw.id,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=None,
        integration_connection_id=recipient.id,
    )
    ordinary_mcp = _signal(
        source=Source.MCP.value,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )
    owner_raw = _raw(roots=legacy_owner_roots, connection=recipient, actor=True)
    db_session.add(owner_raw)
    await db_session.flush()
    ordinary_telegram = _signal(
        raw_id=owner_raw.id,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=recipient.id,
    )
    db_session.add_all([late, ordinary_mcp, ordinary_telegram])
    await db_session.flush()

    result = await service.preflight_signal_ownership_backfill(db_session)
    assert result.completed and result.rows_above_high_watermark == 3

    too_new_raw = _raw(roots=legacy_owner_roots, connection=recipient, actor=False)
    db_session.add(too_new_raw)
    await db_session.flush()
    db_session.add(
        _signal(
            raw_id=too_new_raw.id,
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=None,
            integration_connection_id=recipient.id,
        )
    )
    await db_session.flush()
    with pytest.raises(service.SignalOwnershipBackfillProvenanceError):
        await service.preflight_signal_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_completed_rejects_actorless_tail_from_frozen_s_only_raw(
    db_session, legacy_owner_roots
):
    frozen_raw = _raw(roots=legacy_owner_roots, actor=False)
    db_session.add(frozen_raw)
    await db_session.flush()
    checkpoints = await _ready(db_session, legacy_owner_roots)
    raw_checkpoint = checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE]
    raw_checkpoint.scan_high_watermark_id = frozen_raw.id
    raw_checkpoint.snapshot_rows = 1
    raw_checkpoint.last_scanned_id = frozen_raw.id
    raw_checkpoint.scanned_rows = 1
    raw_checkpoint.unchanged_rows = 1
    db_session.add(_signal())
    await db_session.flush()
    await _finish(db_session)
    db_session.add(
        _signal(
            raw_id=frozen_raw.id,
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=None,
            integration_connection_id=None,
        )
    )
    await db_session.flush()
    with pytest.raises(service.SignalOwnershipBackfillProvenanceError):
        await service.preflight_signal_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_stale_identity_map_is_updated_without_dirtying_other_fields(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    row = _signal()
    db_session.add(row)
    await db_session.flush()
    row_id = row.id
    assert await db_session.get(Signal, row_id) is row
    await _finish(db_session)
    assert row.subject_id == legacy_owner_roots.subject_id
    assert not db_session.is_modified(row)


@pytest.mark.asyncio
async def test_bounded_root_projection(db_session, legacy_owner_roots, monkeypatch):
    await _ready(db_session, legacy_owner_roots)
    for _ in range(3):
        recipient = await _recipient(db_session, legacy_owner_roots)
        raw = _raw(roots=legacy_owner_roots, connection=recipient)
        db_session.add(raw)
        await db_session.flush()
        db_session.add(_signal(raw_id=raw.id))
    await db_session.flush()
    sizes = []
    original_raws = service._project_raws
    original_connections = service._project_connections

    async def tracked_raws(session, ids):
        sizes.append(len(ids))
        assert len(ids) <= 2
        return await original_raws(session, ids)

    async def tracked_connections(session, ids):
        sizes.append(len(ids))
        assert len(ids) <= 2
        return await original_connections(session, ids)

    monkeypatch.setattr(service, "_PAGE_SIZE", 2)
    monkeypatch.setattr(service, "_project_raws", tracked_raws)
    monkeypatch.setattr(service, "_project_connections", tracked_connections)
    assert (await _finish(db_session, batch_size=2)).completed
    assert sizes and max(sizes) == 2


def _set_nonempty_restore(row, *, status):
    row.status = status
    row.scan_high_watermark_id = row.snapshot_rows = 1
    row.last_scanned_id = row.scanned_rows = row.updated_rows = row.unchanged_rows = 0
    row.completed_at = None


@pytest.mark.asyncio
async def test_portability_reset_nonempty_and_empty(db_session, legacy_owner_roots):
    checkpoints = await _ready(db_session, legacy_owner_roots)
    rb = (
        (RAW_OWNERSHIP_BACKFILL_PHASE,)
        + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    )
    running = set(_PRIOR_PHASES) - set(rb)
    for phase in rb:
        _set_nonempty_restore(checkpoints[phase], status="restore_blocked")
    for phase in running:
        _set_nonempty_restore(checkpoints[phase], status="running")
    await db_session.flush()

    await service.reset_signal_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"signals": (7, 1)}
    )
    imported = _signal(subject_id=legacy_owner_roots.subject_id)
    imported.id = 7
    db_session.add(imported)
    await db_session.flush()
    assert (await service.preflight_signal_ownership_backfill(db_session)).status.value == "running"
    assert (await _finish(db_session)).completed

    await db_session.execute(delete(Signal))
    await service.reset_signal_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"signals": (0, 0)}
    )
    assert (await service.preflight_signal_ownership_backfill(db_session)).completed
    with pytest.raises(service.SignalOwnershipBackfillValidationError):
        await service.reset_signal_ownership_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds={"signals": (1, 0)}
        )


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_fail_closed(db_session, legacy_owner_roots):
    with pytest.raises(service.SignalOwnershipBackfillDependencyError):
        await service.preflight_signal_ownership_backfill(db_session)
    await _ready(db_session, legacy_owner_roots)
    with pytest.raises(service.SignalOwnershipBackfillValidationError):
        await service.run_signal_ownership_backfill_batch(db_session, batch_size=True)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["row", "raw", "connection"])
async def test_postgres_projected_graph_races_fail_without_checkpoint(
    db_session, legacy_owner_roots, monkeypatch, race
):
    await _ready(db_session, legacy_owner_roots)
    recipient = await _recipient(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, connection=recipient)
    db_session.add(raw)
    await db_session.flush()
    row = _signal(raw_id=raw.id)
    db_session.add(row)
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)
    projected = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(writer_done.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_signals_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_signal_ownership_backfill_batch(session)
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()
            return None

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(projected.wait(), timeout=15)
        async with factory() as writer:
            if race == "row":
                await writer.execute(update(Signal).where(Signal.id == row.id).values(note="race"))
            elif race == "raw":
                await writer.execute(update(RawPayload).where(RawPayload.id == raw.id).values(actor_user_id=None))
            else:
                await writer.execute(
                    update(IntegrationConnection)
                    .where(IntegrationConnection.id == recipient.id)
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

    assert isinstance(error, service.SignalOwnershipBackfillStateError)
    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            service.SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["signals"],
        )
        assert checkpoint is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_writer_lands_strict_tail_during_frozen_batch(
    db_session, legacy_owner_roots, monkeypatch
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_signal())
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)
    projected = asyncio.Event()
    release = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(release.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_signals_projection_for_test", pause)

    async def backfill_worker():
        async with factory() as session:
            result = await service.run_signal_ownership_backfill_batch(session)
            await session.commit()
            return result

    async def writer_worker():
        async with factory() as session:
            rows = await signals_service.create_signals(
                session,
                items=[{"kind": "state", "key": "energy", "value_num": 3}],
                source=Source.MCP.value,
                identity=WriteIdentity(
                    subject_id=legacy_owner_roots.subject_id,
                    actor_user_id=legacy_owner_roots.user_id,
                ),
            )
            await session.commit()
            return rows[0].id

    backfill = asyncio.create_task(backfill_worker())
    await asyncio.wait_for(projected.wait(), timeout=15)
    writer = asyncio.create_task(writer_worker())
    release.set()
    assert (await asyncio.wait_for(backfill, timeout=15)).completed
    new_id = await asyncio.wait_for(writer, timeout=15)
    async with factory() as verify:
        live = await verify.get(Signal, new_id)
        assert live.subject_id == legacy_owner_roots.subject_id
        assert live.actor_user_id == legacy_owner_roots.user_id
        assert live.source == Source.MCP.value
