"""Focused SQLite/PostgreSQL contracts for Stage-3M lab-result ownership."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    LabFlag,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.labs import LabResult
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.services import lab_result_ownership_backfill_service as service
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
    + tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
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


async def _gateway(session, roots, *, status=IntegrationConnectionStatus.LEGACY.value):
    row = IntegrationConnection(
        subject_id=roots.subject_id,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"synthetic:{uuid.uuid4()}",
        status=status,
        retired_at=(
            _STAMP if status == IntegrationConnectionStatus.RETIRED.value else None
        ),
    )
    session.add(row)
    await session.flush()
    return row


async def _document(session, roots, *, storage_ref, uploader=True):
    row = FileAsset(
        subject_id=roots.subject_id,
        uploaded_by_user_id=roots.user_id if uploader else None,
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
        storage_ref=storage_ref,
        media_type="application/pdf",
    )
    session.add(row)
    await session.flush()
    return row


def _result(
    *,
    on_date=date(2026, 1, 2),
    source=Source.MANUAL.value,
    raw_payload_id=None,
    subject_id=None,
    actor_user_id=None,
    marker="ferritin",
    value=45.0,
):
    return LabResult(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        date=on_date,
        domain=Domain.LABS.value,
        source=source,
        marker=marker,
        value=value,
        unit="ng/mL",
        ref_low=30.0,
        ref_high=400.0,
        flag=LabFlag.NORMAL.value,
        lab_name="Synthetic Lab",
        raw_payload_id=raw_payload_id,
        note="synthetic",
    )


def _raw(
    *,
    roots,
    connection=None,
    file_asset=None,
    actor=False,
    source=Source.LAB_PARSER.value,
    external_id=None,
    unowned=False,
):
    return RawPayload(
        subject_id=None if unowned else roots.subject_id,
        actor_user_id=roots.user_id if actor else None,
        integration_connection_id=connection.id if connection is not None else None,
        file_asset_id=file_asset.id if file_asset is not None else None,
        domain=Domain.LABS.value,
        source=source,
        external_id=external_id or uuid.uuid4().hex,
        payload={"results": [{"marker": "ferritin", "value": 45.0}]},
        processed_at=_RAW_STAMP,
    )


def _invocation(
    *,
    roots,
    gateway_root,
    raw_payload_id,
    status=AIInvocationStatus.SUCCEEDED.value,
    subject_id=None,
):
    return AIInvocation(
        subject_id=subject_id or roots.subject_id,
        actor_user_id=roots.user_id,
        raw_payload_id=raw_payload_id,
        platform_integration_connection_id=gateway_root.id,
        purpose=AIInvocationPurpose.LAB_DOCUMENT_PARSE.value,
        source=AIInvocationSource.WEB.value,
        model="synthetic/lab-parse",
        config_version=gateway_root.config_version,
        idempotency_key=uuid.uuid4().hex,
        quota_period_start=date(2020, 1, 1),
        quota_period_end=date(2100, 1, 1),
        reserved_cost_microunits=100,
        reserved_units=500,
        charged_cost_microunits=100,
        charged_units=500,
        status=status,
        started_at=_STAMP,
        finished_at=_STAMP,
    )


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_lab_result_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3M did not complete")


def test_public_contract_is_fixed():
    assert (
        service.LAB_RESULT_OWNERSHIP_BACKFILL_PHASE
        == "stage3.raw_linked_facts.lab_results.v1"
    )
    assert service.LAB_RESULT_OWNERSHIP_BACKFILL_TABLES == ("lab_results",)
    assert tuple(service.LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        "lab_results",
    )
    assert service.DEFAULT_LAB_RESULT_OWNERSHIP_BACKFILL_BATCH_SIZE == 250
    assert service.MAX_LAB_RESULT_OWNERSHIP_BACKFILL_BATCH_SIZE == 1000
    assert [item.value for item in service.LabResultOwnershipBackfillStatus] == [
        "not_started",
        "running",
        "completed",
    ]
    with pytest.raises(TypeError):
        service.LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_legacy_manual_result_gains_only_subject(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    row = _result()
    db_session.add(row)
    await db_session.flush()
    original = (
        row.actor_user_id,
        row.raw_payload_id,
        row.marker,
        row.value,
        row.unit,
        row.ref_low,
        row.ref_high,
        row.flag,
        row.lab_name,
        row.note,
        row.updated_at,
    )

    result = await _finish(db_session)
    await db_session.refresh(row)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert (
        row.actor_user_id,
        row.raw_payload_id,
        row.marker,
        row.value,
        row.unit,
        row.ref_low,
        row.ref_high,
        row.flag,
        row.lab_name,
        row.note,
        row.updated_at,
    ) == original
    assert "subject_id" not in result.to_safe_dict()


@pytest.mark.asyncio
async def test_subject_funded_parser_history_is_preserved(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    gateway = await _gateway(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, connection=gateway)
    db_session.add(raw)
    await db_session.flush()
    row = _result(source=Source.LAB_PARSER.value, raw_payload_id=raw.id)
    db_session.add(row)
    await db_session.flush()

    result = await _finish(db_session)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    # The gateway root stays on the raw payload; the fact gains no actor.
    assert row.actor_user_id is None
    assert raw.integration_connection_id == gateway.id


@pytest.mark.asyncio
async def test_platform_parser_chain_is_validated(
    db_session, legacy_owner_roots, platform_ai_ready
):
    await _ready(db_session, legacy_owner_roots)
    storage_ref = f"lab/{uuid.uuid4().hex}.pdf"
    document = await _document(
        db_session, legacy_owner_roots, storage_ref=storage_ref
    )
    raw = _raw(
        roots=legacy_owner_roots,
        file_asset=document,
        actor=True,
        external_id=storage_ref,
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        _invocation(
            roots=legacy_owner_roots,
            gateway_root=platform_ai_ready,
            raw_payload_id=raw.id,
        )
    )
    db_session.add(
        _result(
            source=Source.LAB_PARSER.value,
            raw_payload_id=raw.id,
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
        )
    )
    await db_session.flush()

    result = await _finish(db_session)
    assert result.completed and result.updated_rows == 0 and result.unchanged_rows == 1


@pytest.mark.asyncio
async def test_restored_fileless_parser_history_stays_valid(
    db_session, legacy_owner_roots
):
    """Backup v1 strips the raw C/F roots; the restored shape must revalidate."""

    await _ready(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots)
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        _result(
            source=Source.LAB_PARSER.value,
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
        "document_storage_mismatch",
        "manual_raw_source_mismatch",
        "manual_raw_with_gateway",
        "owned_fact_unowned_raw",
        "blank_marker",
        "unknown_flag",
    ],
)
async def test_malformed_history_fails_closed(db_session, legacy_owner_roots, case):
    await _ready(db_session, legacy_owner_roots)
    if case == "partial_roots":
        db_session.add(_result(actor_user_id=legacy_owner_roots.user_id))
    elif case == "document_storage_mismatch":
        document = await _document(
            db_session, legacy_owner_roots, storage_ref=f"lab/{uuid.uuid4().hex}.pdf"
        )
        raw = _raw(
            roots=legacy_owner_roots,
            file_asset=document,
            actor=True,
            external_id="lab/a-different-object.pdf",
        )
        db_session.add(raw)
        await db_session.flush()
        db_session.add(
            _result(
                source=Source.LAB_PARSER.value,
                raw_payload_id=raw.id,
                subject_id=legacy_owner_roots.subject_id,
                actor_user_id=legacy_owner_roots.user_id,
            )
        )
    elif case == "manual_raw_source_mismatch":
        raw = _raw(roots=legacy_owner_roots, source=Source.LAB_PARSER.value)
        db_session.add(raw)
        await db_session.flush()
        db_session.add(_result(raw_payload_id=raw.id))
    elif case == "manual_raw_with_gateway":
        gateway = await _gateway(db_session, legacy_owner_roots)
        raw = _raw(
            roots=legacy_owner_roots,
            connection=gateway,
            source=Source.MANUAL.value,
        )
        db_session.add(raw)
        await db_session.flush()
        db_session.add(_result(raw_payload_id=raw.id))
    elif case == "owned_fact_unowned_raw":
        raw = _raw(roots=legacy_owner_roots, unowned=True)
        db_session.add(raw)
        await db_session.flush()
        db_session.add(
            _result(
                source=Source.LAB_PARSER.value,
                raw_payload_id=raw.id,
                subject_id=legacy_owner_roots.subject_id,
            )
        )
    elif case == "blank_marker":
        db_session.add(_result(marker="   "))
    else:
        row = _result()
        row.flag = "sideways"
        db_session.add(row)
    await db_session.flush()

    with pytest.raises(service.LabResultOwnershipBackfillError):
        await service.preflight_lab_result_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_subject_funded_history_cannot_also_claim_a_platform_parse(
    db_session, legacy_owner_roots, platform_ai_ready
):
    await _ready(db_session, legacy_owner_roots)
    gateway = await _gateway(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, connection=gateway)
    db_session.add(raw)
    await db_session.flush()
    db_session.add(_result(source=Source.LAB_PARSER.value, raw_payload_id=raw.id))
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(
        _invocation(
            roots=legacy_owner_roots,
            gateway_root=platform_ai_ready,
            raw_payload_id=raw.id,
        )
    )
    await db_session.flush()
    with pytest.raises(service.LabResultOwnershipBackfillProvenanceError):
        await service.preflight_lab_result_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_requires_explicit_ownership(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_result())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(_result(on_date=date(2026, 1, 3)))
    await db_session.flush()
    with pytest.raises(service.LabResultOwnershipBackfillStateError):
        await service.preflight_lab_result_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_evidence_is_stable(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    for offset in range(5):
        db_session.add(_result(on_date=date(2026, 2, 1 + offset)))
    await db_session.flush()

    first = await service.run_lab_result_ownership_backfill_batch(
        db_session, batch_size=2
    )
    assert not first.completed and first.batch_updated_rows == 2
    final = await _finish(db_session, batch_size=2)
    assert final.completed and final.updated_rows == 5
    repeat = await service.run_lab_result_ownership_backfill_batch(db_session)
    assert repeat.completed and repeat.batch_scanned_rows == 0
    assert repeat.data_checksum_before == final.data_checksum_before
    assert repeat.ownership_checksum_after == final.ownership_checksum_after


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_are_enforced(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.LabResultOwnershipBackfillDependencyError):
        await service.preflight_lab_result_ownership_backfill(db_session)
    await _ready(db_session, legacy_owner_roots)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weight_logs"]
        )
    )
    with pytest.raises(service.LabResultOwnershipBackfillDependencyError):
        await service.preflight_lab_result_ownership_backfill(db_session)
    db_session.add(
        _checkpoint(
            WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weight_logs"],
            legacy_owner_roots.subject_id,
        )
    )
    await db_session.flush()
    for size in (0, 1001, True, "250"):
        with pytest.raises(service.LabResultOwnershipBackfillValidationError):
            await service.run_lab_result_ownership_backfill_batch(
                db_session, batch_size=size
            )


@pytest.mark.asyncio
async def test_restore_reset_rebases_the_exact_checkpoint(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_result())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    await service.reset_lab_result_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"lab_results": (7, 3)}
    )
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["lab_results"]
        )
    )
    assert checkpoint.status == "running"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (7, 3)
    assert checkpoint.last_scanned_id == 0 and checkpoint.scanned_rows == 0

    for bounds in ({"signals": (1, 1)}, {"lab_results": (0, 2)}, {"lab_results": 7}):
        with pytest.raises(service.LabResultOwnershipBackfillValidationError):
            await (
                service.reset_lab_result_ownership_backfill_for_portability_v1_restore(
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
    retained = tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    for phase in blocked:
        _set_nonempty_restore(checkpoints[phase], status="restore_blocked")
    for phase in set(_PRIOR_PHASES) - set(blocked) - set(retained):
        _set_nonempty_restore(checkpoints[phase], status="running")
    await db_session.flush()

    await service.reset_lab_result_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"lab_results": (9, 1)}
    )
    imported = _result(subject_id=legacy_owner_roots.subject_id)
    imported.id = 9
    db_session.add(imported)
    await db_session.flush()
    assert (
        await service.preflight_lab_result_ownership_backfill(db_session)
    ).status.value == "running"
    recompleted = await _finish(db_session)
    assert recompleted.completed and recompleted.updated_rows == 0
    assert imported.actor_user_id is None

    await db_session.execute(delete(LabResult))
    await service.reset_lab_result_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"lab_results": (0, 0)}
    )
    assert (
        await service.preflight_lab_result_ownership_backfill(db_session)
    ).completed


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["row", "raw", "connection"])
async def test_postgres_projected_graph_races_fail_without_checkpoint(
    db_session, legacy_owner_roots, monkeypatch, race
):
    import asyncio

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    await _ready(db_session, legacy_owner_roots)
    gateway = await _gateway(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots, connection=gateway)
    db_session.add(raw)
    await db_session.flush()
    row = _result(source=Source.LAB_PARSER.value, raw_payload_id=raw.id)
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

    monkeypatch.setattr(service, "_after_lab_results_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_lab_result_ownership_backfill_batch(session)
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
                    update(LabResult)
                    .where(LabResult.id == row.id)
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
                    .where(IntegrationConnection.id == gateway.id)
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

    assert isinstance(error, service.LabResultOwnershipBackfillStateError)
    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            service.LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["lab_results"],
        )
        assert checkpoint is None
