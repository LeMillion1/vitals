"""Whole-lake Stage-4 ownership validation contracts."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select

from tests.conftest import legacy_unenforced_write
from vitals.enums import Domain, Source
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.hrt import HrtCompound, HrtCompoundComponent
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.weight import WeightLog
from vitals.services import ownership_validation_service as service


_EMPTY = hashlib.sha256(b"").hexdigest()
_STAMP = datetime(2020, 1, 1, tzinfo=UTC)


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


async def _stage3_completed(session, roots):
    rows = [
        _checkpoint(phase, roots.subject_id) for phase in service.STAGE3_PHASES
    ]
    session.add_all(rows)
    await session.flush()
    return {row.phase_key: row for row in rows}


def _weight(*, subject_id, on_date=date(2026, 1, 2)):
    return WeightLog(
        subject_id=subject_id,
        date=on_date,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=81.5,
        superseded=False,
    )


def test_public_contract_is_fixed():
    assert service.OWNERSHIP_VALIDATION_PHASE == "stage4.whole_lake_validation.v1"
    assert [
        item.value for item in service.OwnershipValidationStatus
    ] == ["not_started", "completed"]
    assert set(service.SUBJECT_EQUALITY_CONSTRAINTS) == {
        "body_scan_metrics",
        "hevy_exercises",
        "hevy_sets",
        "hrt_compound_components",
        "hrt_cycle_items",
        "hrt_cycle_template_items",
    }
    with pytest.raises(TypeError):
        service.SUBJECT_EQUALITY_CONSTRAINTS["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_every_registered_table_is_classified(db_session, legacy_owner_roots):
    """A newly added table must be validated the moment it exists."""

    await _stage3_completed(db_session, legacy_owner_roots)
    result = await service.preflight_ownership_validation(db_session)
    assert result.tables_total > 40
    assert result.checks_total > result.tables_total
    assert result.violations_total == 0


@pytest.mark.asyncio
async def test_proved_lake_records_reviewed_evidence(db_session, legacy_owner_roots):
    await _stage3_completed(db_session, legacy_owner_roots)
    db_session.add(_weight(subject_id=legacy_owner_roots.subject_id))
    await db_session.flush()

    before = await service.preflight_ownership_validation(db_session)
    assert not before.completed
    assert before.status.value == "not_started"

    recorded = await service.run_ownership_validation(db_session)
    assert recorded.completed
    assert recorded.graph_checksum == before.graph_checksum
    assert recorded.violations_total == 0

    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == service.OWNERSHIP_VALIDATION_PHASE
        )
    )
    assert checkpoint is not None
    assert checkpoint.status == "completed"
    assert checkpoint.updated_rows == 0
    assert checkpoint.ownership_checksum_after == recorded.graph_checksum

    again = await service.preflight_ownership_validation(db_session)
    assert again.completed and again.graph_checksum == recorded.graph_checksum


@pytest.mark.asyncio
async def test_new_data_invalidates_recorded_evidence(db_session, legacy_owner_roots):
    await _stage3_completed(db_session, legacy_owner_roots)
    assert (await service.run_ownership_validation(db_session)).completed

    db_session.add(_weight(subject_id=legacy_owner_roots.subject_id))
    await db_session.flush()

    stale = await service.preflight_ownership_validation(db_session)
    assert not stale.completed
    assert stale.status.value == "not_started"


@pytest.mark.asyncio
async def test_required_subject_gap_fails_closed(db_session, legacy_owner_roots):
    await _stage3_completed(db_session, legacy_owner_roots)
    db_session.add(_weight(subject_id=None))
    await db_session.flush()

    with pytest.raises(service.OwnershipValidationViolation):
        await service.preflight_ownership_validation(db_session)


@pytest.mark.asyncio
async def test_foreign_subject_fails_closed(db_session, legacy_owner_roots):
    await _stage3_completed(db_session, legacy_owner_roots)
    # A foreign subject is impossible to write once the database enforces its
    # references, and validation still refuses history that carries one.
    async with legacy_unenforced_write(db_session):
        db_session.add(_weight(subject_id=uuid.uuid4()))

    with pytest.raises(service.OwnershipValidationViolation):
        await service.preflight_ownership_validation(db_session)


@pytest.mark.asyncio
async def test_child_parent_subject_mismatch_fails_closed(
    db_session, legacy_owner_roots
):
    await _stage3_completed(db_session, legacy_owner_roots)
    scan = BodyScan(
        subject_id=legacy_owner_roots.subject_id,
        date=date(2026, 1, 2),
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MANUAL.value,
    )
    db_session.add(scan)
    await db_session.flush()
    # A child whose subject disagrees with its scan predates the Stage-4
    # constraints; validation still has to find it.
    async with legacy_unenforced_write(db_session):
        db_session.add(
            BodyScanMetric(
                subject_id=None,
                scan_id=scan.id,
                metric_key="phase_angle",
                label="Phase Angle",
                value=6.0,
                category="score",
            )
        )

    with pytest.raises(service.OwnershipValidationViolation):
        await service.preflight_ownership_validation(db_session)


@pytest.mark.asyncio
async def test_incomplete_stage3_blocks_validation(db_session, legacy_owner_roots):
    with pytest.raises(service.OwnershipValidationDependencyError):
        await service.preflight_ownership_validation(db_session)

    checkpoints = await _stage3_completed(db_session, legacy_owner_roots)
    blocked = checkpoints[service.STAGE3_PHASES[0]]
    blocked.status = "restore_blocked"
    blocked.scan_high_watermark_id = blocked.snapshot_rows = 1
    blocked.last_scanned_id = blocked.scanned_rows = blocked.unchanged_rows = 0
    blocked.completed_at = None
    await db_session.flush()

    with pytest.raises(service.OwnershipValidationDependencyError):
        await service.preflight_ownership_validation(db_session)


@pytest.mark.asyncio
async def test_missing_stage3_phase_blocks_validation(db_session, legacy_owner_roots):
    await _stage3_completed(db_session, legacy_owner_roots)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key == service.STAGE3_PHASES[-1]
        )
    )
    with pytest.raises(service.OwnershipValidationDependencyError):
        await service.preflight_ownership_validation(db_session)


@pytest.mark.asyncio
async def test_validation_requires_the_sole_reviewed_subject(db_session):
    with pytest.raises(service.OwnershipValidationIdentityError):
        await service.preflight_ownership_validation(db_session)


@pytest.mark.asyncio
async def test_curated_catalog_children_may_have_no_subject(
    db_session, legacy_owner_roots
):
    await _stage3_completed(db_session, legacy_owner_roots)
    # A curated catalog compound belongs to the platform, not to a subject, and
    # its components inherit exactly that: no subject at all.
    compound = HrtCompound(
        subject_id=None,
        key="synthetic-blend",
        name="Synthetic blend",
        compound_class="ester_blend",
        route="im",
    )
    db_session.add(compound)
    await db_session.flush()
    db_session.add(
        HrtCompoundComponent(
            subject_id=None,
            compound_id=compound.id,
            ester="propionate",
            mg=30.0,
        )
    )
    await db_session.flush()

    result = await service.preflight_ownership_validation(db_session)
    assert result.violations_total == 0


@pytest.mark.asyncio
async def test_owned_child_of_curated_catalog_parent_fails_closed(
    db_session, legacy_owner_roots
):
    await _stage3_completed(db_session, legacy_owner_roots)
    compound = HrtCompound(
        subject_id=None,
        key="synthetic-blend",
        name="Synthetic blend",
        compound_class="ester_blend",
        route="im",
    )
    db_session.add(compound)
    await db_session.flush()
    # Claiming a curated catalog parent for one subject is exactly the crossing
    # the Stage-4 constraints forbid, so validation has to find it too.
    async with legacy_unenforced_write(db_session):
        db_session.add(
            HrtCompoundComponent(
                subject_id=legacy_owner_roots.subject_id,
                compound_id=compound.id,
                ester="propionate",
                mg=30.0,
            )
        )

    with pytest.raises(service.OwnershipValidationViolation):
        await service.preflight_ownership_validation(db_session)
