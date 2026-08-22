"""Focused SQLite/PostgreSQL contracts for Stage-3O body-scan ownership."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.body_scan import BodyScan
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.services import body_scan_ownership_backfill_service as service
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
    + tuple(SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
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
    checkpoints = [_checkpoint(phase, roots.subject_id) for phase in _PRIOR_PHASES]
    session.add_all(checkpoints)
    await session.flush()
    return {checkpoint.phase_key: checkpoint for checkpoint in checkpoints}


async def _gateway(session, roots):
    row = IntegrationConnection(
        subject_id=roots.subject_id,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"synthetic:{uuid.uuid4()}",
        status=IntegrationConnectionStatus.LEGACY.value,
    )
    session.add(row)
    await session.flush()
    return row


def _raw(
    *, roots, connection=None, file_asset=None, actor=False,
    source=Source.BODY_SCAN.value, external_id=None, unowned=False,
):
    return RawPayload(
        subject_id=None if unowned else roots.subject_id,
        actor_user_id=roots.user_id if actor else None,
        integration_connection_id=connection.id if connection is not None else None,
        file_asset_id=file_asset.id if file_asset is not None else None,
        domain=Domain.BODY_COMPOSITION.value,
        source=source,
        external_id=external_id or uuid.uuid4().hex,
        payload={"metrics": []},
        processed_at=_RAW_STAMP,
    )


def _scan(
    *,
    file_key=None,
    source=Source.BODY_SCAN.value,
    raw_payload_id=None,
    subject_id=None,
    actor_user_id=None,
    file_asset_id=None,
    on_date=date(2026, 1, 2),
):
    return BodyScan(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        file_asset_id=file_asset_id,
        date=on_date,
        domain=Domain.BODY_COMPOSITION.value,
        source=source,
        device="InBody 770",
        file_key=file_key,
        raw_payload_id=raw_payload_id,
        note="synthetic",
    )


async def _finish(session, *, batch_size=250):
    for _ in range(10):
        result = await service.run_body_scan_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3O did not complete")


def test_public_contract_is_fixed():
    assert service.BODY_SCAN_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.file_backed.body_scans.v1"
    )
    assert service.BODY_SCAN_OWNERSHIP_BACKFILL_TABLES == ("body_scans",)
    assert [
        status.value for status in service.BodyScanOwnershipBackfillStatus
    ] == ["not_started", "running", "completed", "restore_blocked"]
    with pytest.raises(TypeError):
        service.BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_sheet_backed_history_gains_subject_and_actorless_placeholder(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    gateway = await _gateway(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, connection=gateway)
    db_session.add(raw)
    await db_session.flush()
    scan = _scan(file_key="body/deadbeef.png", raw_payload_id=raw.id)
    db_session.add(scan)
    await db_session.flush()
    original = (scan.device, scan.file_key, scan.note, scan.updated_at)

    result = await _finish(db_session)
    await db_session.refresh(scan)

    assert result.completed and result.updated_rows == 1
    assert scan.subject_id == legacy_owner_roots.subject_id
    assert scan.actor_user_id is None
    assert (scan.device, scan.file_key, scan.note, scan.updated_at) == original

    asset = await db_session.get(FileAsset, scan.file_asset_id)
    assert asset is not None
    assert asset.subject_id == legacy_owner_roots.subject_id
    assert asset.uploaded_by_user_id is None
    assert asset.purpose == FileAssetPurpose.BODY_SCAN_DOCUMENT.value
    assert asset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value
    assert asset.storage_ref == "body/deadbeef.png"
    assert asset.status == FileAssetStatus.LEGACY_PLACEHOLDER.value
    assert asset.sha256_hex is None and asset.byte_size is None


@pytest.mark.asyncio
async def test_sheetless_history_gains_subject_without_a_file_root(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, source=Source.MCP.value, actor=True)
    db_session.add(raw)
    await db_session.flush()
    structured = _scan(source=Source.MCP.value, raw_payload_id=raw.id)
    manual = _scan(source=Source.MANUAL.value, on_date=date(2026, 1, 3))
    db_session.add_all([structured, manual])
    await db_session.flush()

    result = await _finish(db_session)

    assert result.completed and result.updated_rows == 2
    for row in (structured, manual):
        assert row.subject_id == legacy_owner_roots.subject_id
        assert row.actor_user_id is None
        assert row.file_asset_id is None
    assert (
        await db_session.scalar(select(FileAsset).limit(1))
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "body/deadbeef.png",
        "uploads/synthetic-sheet.jpg",
        "synthetic-sheet.pdf",
    ],
)
async def test_reviewed_sheet_keys_are_accepted(db_session, legacy_owner_roots, key):
    await _ready(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots)
    db_session.add(raw)
    await db_session.flush()
    db_session.add(_scan(file_key=key, raw_payload_id=raw.id))
    await db_session.flush()
    assert (await _finish(db_session)).completed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unsafe_key",
        "duplicate_key",
        "partial_roots",
        "manual_with_file",
        "mcp_with_file",
        "existing_asset_conflict",
        "owned_scan_unowned_raw",
        "gateway_raw_with_platform_invocation",
    ],
)
async def test_malformed_history_fails_closed(
    db_session, legacy_owner_roots, platform_ai_ready, case
):
    await _ready(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots)
    db_session.add(raw)
    await db_session.flush()
    if case == "unsafe_key":
        db_session.add(_scan(file_key="../secret.png", raw_payload_id=raw.id))
    elif case == "duplicate_key":
        db_session.add_all(
            [
                _scan(file_key="body/same.png", raw_payload_id=raw.id),
                _scan(file_key="body/same.png", on_date=date(2026, 1, 3)),
            ]
        )
    elif case == "partial_roots":
        db_session.add(
            _scan(
                file_key="body/partial.png",
                raw_payload_id=raw.id,
                actor_user_id=legacy_owner_roots.user_id,
            )
        )
    elif case == "manual_with_file":
        db_session.add(_scan(file_key="body/manual.png", source=Source.MANUAL.value))
    elif case == "mcp_with_file":
        db_session.add(
            _scan(
                file_key="body/mcp.png",
                source=Source.MCP.value,
                raw_payload_id=raw.id,
            )
        )
    elif case == "existing_asset_conflict":
        db_session.add(
            FileAsset(
                subject_id=legacy_owner_roots.subject_id,
                uploaded_by_user_id=None,
                purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
                storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
                storage_ref="body/conflict.png",
            )
        )
        db_session.add(_scan(file_key="body/conflict.png", raw_payload_id=raw.id))
    elif case == "owned_scan_unowned_raw":
        unowned = _raw(roots=legacy_owner_roots, unowned=True)
        db_session.add(unowned)
        await db_session.flush()
        db_session.add(
            _scan(
                source=Source.MCP.value,
                raw_payload_id=unowned.id,
                subject_id=legacy_owner_roots.subject_id,
            )
        )
    else:
        gateway = await _gateway(db_session, legacy_owner_roots)
        gateway_raw = _raw(roots=legacy_owner_roots, connection=gateway)
        db_session.add(gateway_raw)
        await db_session.flush()
        db_session.add(
            AIInvocation(
                subject_id=legacy_owner_roots.subject_id,
                actor_user_id=legacy_owner_roots.user_id,
                raw_payload_id=gateway_raw.id,
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
        db_session.add(
            _scan(file_key="body/mixed.png", raw_payload_id=gateway_raw.id)
        )
    await db_session.flush()

    with pytest.raises(service.BodyScanOwnershipBackfillError):
        await service.preflight_body_scan_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_stop_resume_and_processed_bound(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots)
    db_session.add(raw)
    await db_session.flush()
    db_session.add_all(
        [
            _scan(file_key="body/first.png", raw_payload_id=raw.id),
            _scan(file_key="body/second.png", on_date=date(2026, 1, 3)),
        ]
    )
    await db_session.flush()

    first = await service.run_body_scan_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert not first.completed and first.batch_updated_rows == 1
    processed = await service.body_scan_historical_processed_bound(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    first_id = await db_session.scalar(select(BodyScan.id).order_by(BodyScan.id))
    assert processed == first_id

    final = await _finish(db_session)
    assert final.completed and final.updated_rows == 2
    repeat = await service.run_body_scan_ownership_backfill_batch(
        db_session, batch_size=250
    )
    assert repeat.completed and repeat.batch_scanned_rows == 0
    assert repeat.ownership_checksum_after == final.ownership_checksum_after


@pytest.mark.asyncio
async def test_live_tail_requires_explicit_ownership(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_scan(source=Source.MANUAL.value))
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(_scan(source=Source.MANUAL.value, on_date=date(2026, 1, 9)))
    await db_session.flush()
    with pytest.raises(service.BodyScanOwnershipBackfillStateError):
        await service.preflight_body_scan_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_are_enforced(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.BodyScanOwnershipBackfillDependencyError):
        await service.preflight_body_scan_ownership_backfill(db_session)
    await _ready(db_session, legacy_owner_roots)
    for size in (0, 1001, True, "250"):
        with pytest.raises(service.BodyScanOwnershipBackfillValidationError):
            await service.run_body_scan_ownership_backfill_batch(
                db_session, batch_size=size
            )


@pytest.mark.asyncio
async def test_portability_block_retires_outgoing_and_validates_imported_shape(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots)
    db_session.add(raw)
    await db_session.flush()
    outgoing = _scan(file_key="body/outgoing.png", raw_payload_id=raw.id)
    db_session.add(outgoing)
    await db_session.flush()
    assert (await _finish(db_session)).completed
    retired_asset_id = outgoing.file_asset_id
    assert retired_asset_id is not None

    await service.block_body_scan_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"body_scans": (11, 1)}
    )
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["body_scans"]
        )
    )
    assert checkpoint.status == "restore_blocked"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (11, 1)

    retired = await db_session.get(FileAsset, retired_asset_id)
    assert retired.status == FileAssetStatus.DELETED.value
    assert retired.deleted_at is not None and retired.purged_at is None

    # A blocked phase refuses to advance until a provenance-bearing restore.
    with pytest.raises(service.BodyScanOwnershipBackfillStateError):
        await service.run_body_scan_ownership_backfill_batch(
            db_session, batch_size=250
        )


@pytest.mark.asyncio
async def test_empty_portability_replacement_completes_exactly(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    await service.block_body_scan_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"body_scans": (0, 0)}
    )
    result = await service.preflight_body_scan_ownership_backfill(db_session)
    assert result.completed and result.snapshot_rows == 0

    for bounds in ({"progress_photos": (1, 1)}, {"body_scans": (0, 2)}):
        with pytest.raises(service.BodyScanOwnershipBackfillValidationError):
            await (
                service.block_body_scan_ownership_backfill_for_portability_v1_restore(
                    db_session, snapshot_bounds=bounds
                )
            )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["scan_key", "scan_delete", "duplicate_scan"])
async def test_postgres_projected_graph_races_fail_without_checkpoint(
    db_session, legacy_owner_roots, monkeypatch, race
):
    import asyncio

    from sqlalchemy import delete, update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    await _ready(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots)
    db_session.add(raw)
    await db_session.flush()
    scan = _scan(file_key="body/race.png", raw_payload_id=raw.id)
    db_session.add(scan)
    await db_session.commit()
    row_id = scan.id
    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    projected = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(writer_done.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_graph_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_body_scan_ownership_backfill_batch(
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
            if race == "scan_key":
                await writer.execute(
                    update(BodyScan)
                    .where(BodyScan.id == row_id)
                    .values(file_key="body/switched.png")
                )
            elif race == "scan_delete":
                await writer.execute(delete(BodyScan).where(BodyScan.id == row_id))
            else:
                writer.add(
                    _scan(
                        file_key="body/race.png",
                        source=Source.MANUAL.value,
                        on_date=date(2026, 1, 5),
                    )
                )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=15)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert isinstance(error, service.BodyScanOwnershipBackfillError)
    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            service.BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["body_scans"],
        )
        assert checkpoint is None
