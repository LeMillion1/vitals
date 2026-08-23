"""Focused SQLite/PostgreSQL contracts for Stage-3L weight-log ownership."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select, update

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.services import weight_log_ownership_backfill_service as service
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
from vitals.services.shared_report_ownership_backfill_service import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)


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
    + tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
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


async def _connection(
    session,
    roots,
    *,
    provider=IntegrationProvider.GARMIN.value,
    connection_type=IntegrationConnectionType.ACCOUNT.value,
    status=IntegrationConnectionStatus.LEGACY.value,
):
    row = IntegrationConnection(
        subject_id=roots.subject_id,
        provider=provider,
        connection_type=connection_type,
        external_account_discriminator=f"synthetic:{uuid.uuid4()}",
        status=status,
        retired_at=(
            _STAMP if status == IntegrationConnectionStatus.RETIRED.value else None
        ),
    )
    session.add(row)
    await session.flush()
    return row


def _weight(
    *,
    on_date=date(2026, 1, 2),
    source=Source.MANUAL.value,
    raw_payload_id=None,
    subject_id=None,
    actor_user_id=None,
    integration_connection_id=None,
    superseded=False,
    weight_kg=81.5,
):
    return WeightLog(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        integration_connection_id=integration_connection_id,
        date=on_date,
        domain=Domain.WEIGHT.value,
        source=source,
        weight_kg=weight_kg,
        raw_payload_id=raw_payload_id,
        note="synthetic",
        superseded=superseded,
    )


def _raw(
    *,
    roots,
    connection=None,
    actor=False,
    domain=Domain.GARMIN.value,
    source=Source.GARMIN_API.value,
):
    return RawPayload(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id if actor else None,
        integration_connection_id=connection.id if connection is not None else None,
        file_asset_id=None,
        domain=domain,
        source=source,
        external_id=uuid.uuid4().hex,
        payload={"weight": 81.5},
        processed_at=_RAW_STAMP,
    )


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_weight_log_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3L did not complete")


def test_public_contract_is_fixed():
    assert (
        service.WEIGHT_LOG_OWNERSHIP_BACKFILL_PHASE
        == "stage3.channel_optional.weight_logs.v1"
    )
    assert service.WEIGHT_LOG_OWNERSHIP_BACKFILL_TABLES == ("weight_logs",)
    assert tuple(service.WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        "weight_logs",
    )
    assert service.DEFAULT_WEIGHT_LOG_OWNERSHIP_BACKFILL_BATCH_SIZE == 250
    assert service.MAX_WEIGHT_LOG_OWNERSHIP_BACKFILL_BATCH_SIZE == 1000
    assert [item.value for item in service.WeightLogOwnershipBackfillStatus] == [
        "not_started",
        "running",
        "completed",
    ]
    with pytest.raises(TypeError):
        service.WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_legacy_manual_row_gains_only_subject(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    row = _weight()
    db_session.add(row)
    await db_session.flush()
    original = (
        row.actor_user_id,
        row.integration_connection_id,
        row.raw_payload_id,
        row.source,
        row.weight_kg,
        row.note,
        row.superseded,
        row.updated_at,
    )

    result = await _finish(db_session)
    await db_session.refresh(row)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert (
        row.actor_user_id,
        row.integration_connection_id,
        row.raw_payload_id,
        row.source,
        row.weight_kg,
        row.note,
        row.superseded,
        row.updated_at,
    ) == original
    assert "subject_id" not in result.to_safe_dict()


@pytest.mark.asyncio
async def test_provider_history_keeps_null_channel_and_validates_raw(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    connection = await _connection(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, connection=connection)
    db_session.add(raw)
    await db_session.flush()
    row = _weight(source=Source.GARMIN_API.value, raw_payload_id=raw.id)
    db_session.add(row)
    await db_session.flush()

    result = await _finish(db_session)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    # The connection stays on the raw payload; the fact is never given a C it
    # did not persist.
    assert row.integration_connection_id is None and row.actor_user_id is None
    assert raw.integration_connection_id == connection.id


@pytest.mark.asyncio
async def test_body_scan_history_requires_exclusive_parser_provenance(
    db_session, legacy_owner_roots, platform_ai_ready
):
    await _ready(db_session, legacy_owner_roots)
    gateway = await _connection(
        db_session,
        legacy_owner_roots,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
    )
    raw = _raw(
        roots=legacy_owner_roots,
        connection=gateway,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(_weight(source=Source.BODY_SCAN.value, raw_payload_id=raw.id))
    await db_session.flush()

    assert (await _finish(db_session)).completed

    db_session.add(
        AIInvocation(
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
            raw_payload_id=raw.id,
            platform_integration_connection_id=platform_ai_ready.id,
            purpose=AIInvocationPurpose.BODY_SCAN_PARSE.value,
            source=AIInvocationSource.WEB.value,
            model="synthetic/body-scan",
            config_version=platform_ai_ready.config_version,
            idempotency_key=uuid.uuid4().hex,
            quota_period_start=date(2020, 1, 1),
            quota_period_end=date(2100, 1, 1),
            reserved_cost_microunits=100,
            reserved_units=500,
            charged_cost_microunits=100,
            charged_units=500,
            status=AIInvocationStatus.SUCCEEDED.value,
            started_at=_STAMP,
            finished_at=_STAMP,
        )
    )
    await db_session.flush()
    with pytest.raises(service.WeightLogOwnershipBackfillError):
        await service.preflight_weight_log_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_restored_mcp_body_composition_lineage_stays_valid(
    db_session, legacy_owner_roots
):
    """Backup v1 strips the raw actor; the restored lineage must still validate."""

    await _ready(db_session, legacy_owner_roots)
    raw = _raw(
        roots=legacy_owner_roots,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MCP.value,
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        _weight(
            source=Source.BODY_SCAN.value,
            raw_payload_id=raw.id,
            subject_id=legacy_owner_roots.subject_id,
        )
    )
    await db_session.flush()

    result = await _finish(db_session)
    assert result.completed and result.updated_rows == 0 and result.unchanged_rows == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "partial_roots",
        "manual_connection",
        "manual_raw_domain",
        "owned_fact_unowned_raw",
        "foreign_raw_subject",
        "duplicate_active_date",
        "out_of_range_mass",
    ],
)
async def test_malformed_history_fails_closed(db_session, legacy_owner_roots, case):
    await _ready(db_session, legacy_owner_roots)
    if case == "partial_roots":
        db_session.add(_weight(actor_user_id=legacy_owner_roots.user_id))
    elif case == "manual_connection":
        connection = await _connection(db_session, legacy_owner_roots)
        db_session.add(
            _weight(
                subject_id=legacy_owner_roots.subject_id,
                integration_connection_id=connection.id,
            )
        )
    elif case == "manual_raw_domain":
        raw = _raw(roots=legacy_owner_roots)
        db_session.add(raw)
        await db_session.flush()
        db_session.add(_weight(raw_payload_id=raw.id))
    elif case == "owned_fact_unowned_raw":
        raw = _raw(roots=legacy_owner_roots)
        raw.subject_id = None
        db_session.add(raw)
        await db_session.flush()
        db_session.add(
            _weight(
                source=Source.GARMIN_API.value,
                raw_payload_id=raw.id,
                subject_id=legacy_owner_roots.subject_id,
            )
        )
    elif case == "foreign_raw_subject":
        raw = _raw(roots=legacy_owner_roots)
        raw.subject_id = None
        db_session.add(raw)
        await db_session.flush()
        connection = await _connection(db_session, legacy_owner_roots)
        await db_session.execute(
            update(RawPayload.__table__)
            .where(RawPayload.__table__.c.id == raw.id)
            .values(integration_connection_id=connection.id)
        )
        db_session.add(
            _weight(source=Source.GARMIN_API.value, raw_payload_id=raw.id)
        )
    elif case == "duplicate_active_date":
        # Two active rows on one date. The scoped key does not serialize them
        # while neither carries a subject, which is exactly the shape the
        # backfill has to refuse before it can adopt either.
        db_session.add_all([_weight(), _weight(weight_kg=82.0)])
    else:
        db_session.add(_weight(weight_kg=8.0))
    await db_session.flush()

    with pytest.raises(service.WeightLogOwnershipBackfillError):
        await service.preflight_weight_log_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_requires_explicit_ownership(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_weight())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(_weight(on_date=date(2026, 1, 3)))
    await db_session.flush()
    with pytest.raises(service.WeightLogOwnershipBackfillStateError):
        await service.preflight_weight_log_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_owned_rows_and_supersession_stay_valid(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_weight())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(
        _weight(
            on_date=date(2026, 1, 4),
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
            superseded=True,
        )
    )
    db_session.add(
        _weight(
            on_date=date(2026, 1, 4),
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
            source=Source.MCP.value,
        )
    )
    await db_session.flush()
    result = await service.preflight_weight_log_ownership_backfill(db_session)
    assert result.completed and result.rows_above_high_watermark == 2


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_evidence_is_stable(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    for offset in range(5):
        db_session.add(_weight(on_date=date(2026, 2, 1 + offset)))
    await db_session.flush()

    first = await service.run_weight_log_ownership_backfill_batch(
        db_session, batch_size=2
    )
    assert not first.completed and first.batch_updated_rows == 2
    final = await _finish(db_session, batch_size=2)
    assert final.completed and final.updated_rows == 5
    repeat = await service.run_weight_log_ownership_backfill_batch(db_session)
    assert repeat.completed and repeat.batch_scanned_rows == 0
    assert repeat.data_checksum_before == final.data_checksum_before
    assert repeat.ownership_checksum_after == final.ownership_checksum_after


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_are_enforced(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.WeightLogOwnershipBackfillDependencyError):
        await service.preflight_weight_log_ownership_backfill(db_session)
    checkpoints = await _ready(db_session, legacy_owner_roots)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key == RAW_OWNERSHIP_BACKFILL_PHASE
        )
    )
    with pytest.raises(service.WeightLogOwnershipBackfillDependencyError):
        await service.preflight_weight_log_ownership_backfill(db_session)
    db_session.add(
        _checkpoint(RAW_OWNERSHIP_BACKFILL_PHASE, legacy_owner_roots.subject_id)
    )
    await db_session.flush()
    assert checkpoints
    for size in (0, 1001, True, "250"):
        with pytest.raises(service.WeightLogOwnershipBackfillValidationError):
            await service.run_weight_log_ownership_backfill_batch(
                db_session, batch_size=size
            )


@pytest.mark.asyncio
async def test_restore_reset_rebases_the_exact_checkpoint(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_weight())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    await service.reset_weight_log_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"weight_logs": (7, 3)}
    )
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weight_logs"]
        )
    )
    assert checkpoint.status == "running"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (7, 3)
    assert checkpoint.last_scanned_id == 0 and checkpoint.scanned_rows == 0

    for bounds in ({"signals": (1, 1)}, {"weight_logs": (0, 2)}, {"weight_logs": 7}):
        with pytest.raises(service.WeightLogOwnershipBackfillValidationError):
            await (
                service.reset_weight_log_ownership_backfill_for_portability_v1_restore(
                    db_session, snapshot_bounds=bounds
                )
            )


def _set_nonempty_restore(row, *, status):
    row.status = status
    row.scan_high_watermark_id = row.snapshot_rows = 1
    row.last_scanned_id = row.scanned_rows = row.updated_rows = row.unchanged_rows = 0
    row.completed_at = None


@pytest.mark.asyncio
async def test_restore_mode_accepts_the_reviewed_prior_shapes(
    db_session, legacy_owner_roots
):
    checkpoints = await _ready(db_session, legacy_owner_roots)
    blocked = (
        (RAW_OWNERSHIP_BACKFILL_PHASE,)
        + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    )
    # Stage 3K is excluded from backup v1, so it stays a retained completed
    # checkpoint rather than a rebased one.
    retained = tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    for phase in blocked:
        _set_nonempty_restore(checkpoints[phase], status="restore_blocked")
    for phase in set(_PRIOR_PHASES) - set(blocked) - set(retained):
        _set_nonempty_restore(checkpoints[phase], status="running")
    await db_session.flush()

    await service.reset_weight_log_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"weight_logs": (9, 1)}
    )
    imported = _weight(subject_id=legacy_owner_roots.subject_id)
    imported.id = 9
    db_session.add(imported)
    await db_session.flush()
    assert (
        await service.preflight_weight_log_ownership_backfill(db_session)
    ).status.value == "running"
    recompleted = await _finish(db_session)
    assert recompleted.completed and recompleted.updated_rows == 0
    assert imported.actor_user_id is None
    assert imported.integration_connection_id is None

    await db_session.execute(delete(WeightLog))
    await service.reset_weight_log_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"weight_logs": (0, 0)}
    )
    assert (
        await service.preflight_weight_log_ownership_backfill(db_session)
    ).completed


@pytest.mark.asyncio
async def test_restore_mode_rejects_a_blocked_stage3k_checkpoint(
    db_session, legacy_owner_roots
):
    checkpoints = await _ready(db_session, legacy_owner_roots)
    for phase in SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values():
        _set_nonempty_restore(checkpoints[phase], status="restore_blocked")
    _set_nonempty_restore(
        checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE], status="restore_blocked"
    )
    await db_session.flush()
    with pytest.raises(service.WeightLogOwnershipBackfillDependencyError):
        await (
            service.reset_weight_log_ownership_backfill_for_portability_v1_restore(
                db_session, snapshot_bounds={"weight_logs": (0, 0)}
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["row", "raw", "connection"])
async def test_postgres_projected_graph_races_fail_without_checkpoint(
    db_session, legacy_owner_roots, monkeypatch, race
):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    await _ready(db_session, legacy_owner_roots)
    connection = await _connection(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, connection=connection)
    db_session.add(raw)
    await db_session.flush()
    row = _weight(source=Source.GARMIN_API.value, raw_payload_id=raw.id)
    db_session.add(row)
    await db_session.commit()
    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    projected = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(writer_done.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_weight_logs_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_weight_log_ownership_backfill_batch(session)
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
                await writer.execute(
                    update(WeightLog)
                    .where(WeightLog.id == row.id)
                    .values(note="race")
                )
            elif race == "raw":
                await writer.execute(
                    update(RawPayload)
                    .where(RawPayload.id == raw.id)
                    .values(integration_connection_id=None)
                )
            else:
                await writer.execute(
                    update(IntegrationConnection)
                    .where(IntegrationConnection.id == connection.id)
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

    assert isinstance(error, service.WeightLogOwnershipBackfillStateError)
    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            service.WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weight_logs"],
        )
        assert checkpoint is None
