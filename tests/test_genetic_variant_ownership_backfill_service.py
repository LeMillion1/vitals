"""Focused SQLite/PostgreSQL contracts for Stage-3N genetic-variant ownership."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from vitals.enums import Domain, Source
from vitals.models.genetics import GeneticVariant
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.services import genetic_variant_ownership_backfill_service as service
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
    + tuple(LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
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


def _variant(
    *,
    source=Source.MANUAL.value,
    raw_payload_id=None,
    subject_id=None,
    actor_user_id=None,
    gene="HFE",
    rsid=None,
):
    return GeneticVariant(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        domain=Domain.GENETICS.value,
        source=source,
        gene=gene,
        rsid=rsid,
        genotype="CG",
        marker="hemochromatosis_carrier",
        impact="carrier",
        impact_domain=Domain.SUPPLEMENTS.value,
        interpretation="synthetic",
        action_notes="synthetic",
        raw_payload_id=raw_payload_id,
    )


def _raw(*, roots, actor=False, unowned=False, connection_id=None):
    return RawPayload(
        subject_id=None if unowned else roots.subject_id,
        actor_user_id=roots.user_id if actor else None,
        integration_connection_id=connection_id,
        file_asset_id=None,
        domain=Domain.GENETICS.value,
        source=Source.VCF_IMPORT.value,
        external_id=f"vcf:{uuid.uuid4().hex}",
        payload={"variants": [["rs1800562", "C", "G", "CG"]]},
        processed_at=_RAW_STAMP,
    )


async def _finish(session, *, batch_size=250):
    for _ in range(20):
        result = await service.run_genetic_variant_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3N did not complete")


def test_public_contract_is_fixed():
    assert (
        service.GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE
        == "stage3.raw_linked_facts.genetic_variants.v1"
    )
    assert service.GENETIC_VARIANT_OWNERSHIP_BACKFILL_TABLES == ("genetic_variants",)
    assert tuple(service.GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        "genetic_variants",
    )
    assert service.DEFAULT_GENETIC_VARIANT_OWNERSHIP_BACKFILL_BATCH_SIZE == 250
    assert service.MAX_GENETIC_VARIANT_OWNERSHIP_BACKFILL_BATCH_SIZE == 1000
    assert [
        item.value for item in service.GeneticVariantOwnershipBackfillStatus
    ] == ["not_started", "running", "completed"]


@pytest.mark.asyncio
async def test_legacy_manual_variant_gains_only_subject(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    row = _variant()
    db_session.add(row)
    await db_session.flush()
    original = (
        row.actor_user_id,
        row.raw_payload_id,
        row.gene,
        row.rsid,
        row.genotype,
        row.marker,
        row.interpretation,
        row.updated_at,
    )

    result = await _finish(db_session)
    await db_session.refresh(row)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert (
        row.actor_user_id,
        row.raw_payload_id,
        row.gene,
        row.rsid,
        row.genotype,
        row.marker,
        row.interpretation,
        row.updated_at,
    ) == original
    assert "subject_id" not in result.to_safe_dict()


@pytest.mark.asyncio
async def test_vcf_history_keeps_its_durable_batch(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots)
    db_session.add(raw)
    await db_session.flush()
    row = _variant(
        source=Source.VCF_IMPORT.value, raw_payload_id=raw.id, rsid="rs1800562"
    )
    db_session.add(row)
    await db_session.flush()

    result = await _finish(db_session)

    assert result.completed and result.updated_rows == 1
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.actor_user_id is None and row.raw_payload_id == raw.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "partial_roots",
        "manual_with_raw",
        "vcf_without_raw",
        "raw_with_connection",
        "owned_fact_unowned_raw",
        "blank_gene",
        "duplicate_rsid",
    ],
)
async def test_malformed_history_fails_closed(db_session, legacy_owner_roots, case):
    await _ready(db_session, legacy_owner_roots)
    if case == "partial_roots":
        db_session.add(_variant(actor_user_id=legacy_owner_roots.user_id))
    elif case == "manual_with_raw":
        raw = _raw(roots=legacy_owner_roots)
        db_session.add(raw)
        await db_session.flush()
        db_session.add(_variant(raw_payload_id=raw.id))
    elif case == "vcf_without_raw":
        db_session.add(_variant(source=Source.VCF_IMPORT.value))
    elif case == "raw_with_connection":
        from vitals.enums import (
            IntegrationConnectionStatus,
            IntegrationConnectionType,
            IntegrationProvider,
        )
        from vitals.models.tenancy import IntegrationConnection

        connection = IntegrationConnection(
            subject_id=legacy_owner_roots.subject_id,
            provider=IntegrationProvider.OPENROUTER.value,
            connection_type=IntegrationConnectionType.AI_GATEWAY.value,
            external_account_discriminator=f"synthetic:{uuid.uuid4()}",
            status=IntegrationConnectionStatus.LEGACY.value,
        )
        db_session.add(connection)
        await db_session.flush()
        raw = _raw(roots=legacy_owner_roots, connection_id=connection.id)
        db_session.add(raw)
        await db_session.flush()
        db_session.add(
            _variant(source=Source.VCF_IMPORT.value, raw_payload_id=raw.id)
        )
    elif case == "owned_fact_unowned_raw":
        raw = _raw(roots=legacy_owner_roots, unowned=True)
        db_session.add(raw)
        await db_session.flush()
        db_session.add(
            _variant(
                source=Source.VCF_IMPORT.value,
                raw_payload_id=raw.id,
                subject_id=legacy_owner_roots.subject_id,
            )
        )
    elif case == "blank_gene":
        db_session.add(_variant(gene="   "))
    else:
        # Two rows for one rsID. The scoped `(S, rsid)` key does not serialize
        # them while neither carries a subject, which is exactly the shape the
        # backfill has to refuse before it can adopt either.
        db_session.add_all(
            [_variant(rsid="rs1800562"), _variant(rsid="rs1800562", gene="HFE2")]
        )
    await db_session.flush()

    with pytest.raises(service.GeneticVariantOwnershipBackfillError):
        await service.preflight_genetic_variant_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_requires_explicit_ownership(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_variant())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    db_session.add(_variant(gene="MTHFR"))
    await db_session.flush()
    with pytest.raises(service.GeneticVariantOwnershipBackfillStateError):
        await service.preflight_genetic_variant_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_evidence_is_stable(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    for index in range(5):
        db_session.add(_variant(gene=f"GENE{index}", rsid=f"rs{index}"))
    await db_session.flush()

    first = await service.run_genetic_variant_ownership_backfill_batch(
        db_session, batch_size=2
    )
    assert not first.completed and first.batch_updated_rows == 2
    final = await _finish(db_session, batch_size=2)
    assert final.completed and final.updated_rows == 5
    repeat = await service.run_genetic_variant_ownership_backfill_batch(db_session)
    assert repeat.completed and repeat.batch_scanned_rows == 0
    assert repeat.data_checksum_before == final.data_checksum_before
    assert repeat.ownership_checksum_after == final.ownership_checksum_after


@pytest.mark.asyncio
async def test_dependencies_and_batch_size_are_enforced(
    db_session, legacy_owner_roots
):
    with pytest.raises(service.GeneticVariantOwnershipBackfillDependencyError):
        await service.preflight_genetic_variant_ownership_backfill(db_session)
    await _ready(db_session, legacy_owner_roots)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["lab_results"]
        )
    )
    with pytest.raises(service.GeneticVariantOwnershipBackfillDependencyError):
        await service.preflight_genetic_variant_ownership_backfill(db_session)
    db_session.add(
        _checkpoint(
            LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["lab_results"],
            legacy_owner_roots.subject_id,
        )
    )
    await db_session.flush()
    for size in (0, 1001, True, "250"):
        with pytest.raises(service.GeneticVariantOwnershipBackfillValidationError):
            await service.run_genetic_variant_ownership_backfill_batch(
                db_session, batch_size=size
            )


@pytest.mark.asyncio
async def test_restore_reset_rebases_the_exact_checkpoint(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_variant())
    await db_session.flush()
    assert (await _finish(db_session)).completed

    await service.reset_genetic_variant_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"genetic_variants": (7, 3)}
    )
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "genetic_variants"
            ]
        )
    )
    assert checkpoint.status == "running"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (7, 3)

    for bounds in (
        {"signals": (1, 1)},
        {"genetic_variants": (0, 2)},
        {"genetic_variants": 7},
    ):
        with pytest.raises(service.GeneticVariantOwnershipBackfillValidationError):
            await (
                service
                .reset_genetic_variant_ownership_backfill_for_portability_v1_restore(
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

    await service.reset_genetic_variant_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"genetic_variants": (9, 1)}
    )
    imported = _variant(subject_id=legacy_owner_roots.subject_id)
    imported.id = 9
    db_session.add(imported)
    await db_session.flush()
    assert (
        await service.preflight_genetic_variant_ownership_backfill(db_session)
    ).status.value == "running"
    recompleted = await _finish(db_session)
    assert recompleted.completed and recompleted.updated_rows == 0
    assert imported.actor_user_id is None

    await db_session.execute(delete(GeneticVariant))
    await service.reset_genetic_variant_ownership_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"genetic_variants": (0, 0)}
    )
    assert (
        await service.preflight_genetic_variant_ownership_backfill(db_session)
    ).completed


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["row", "raw"])
async def test_postgres_projected_graph_races_fail_without_checkpoint(
    db_session, legacy_owner_roots, monkeypatch, race
):
    import asyncio

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    await _ready(db_session, legacy_owner_roots)
    raw = _raw(roots=legacy_owner_roots)
    db_session.add(raw)
    await db_session.flush()
    row = _variant(
        source=Source.VCF_IMPORT.value, raw_payload_id=raw.id, rsid="rs1800562"
    )
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

    monkeypatch.setattr(service, "_after_genetic_variants_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_genetic_variant_ownership_backfill_batch(session)
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
                    update(GeneticVariant)
                    .where(GeneticVariant.id == row.id)
                    .values(interpretation="race")
                )
            else:
                # Mutate a non-FK projected field: writing an owner-referencing
                # FK would block on the governance lock the batch already holds.
                await writer.execute(
                    update(RawPayload)
                    .where(RawPayload.id == raw.id)
                    .values(external_id="vcf:race")
                )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=15)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert isinstance(error, service.GeneticVariantOwnershipBackfillStateError)
    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            service.GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "genetic_variants"
            ],
        )
        assert checkpoint is None
