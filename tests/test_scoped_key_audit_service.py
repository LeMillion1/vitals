"""Stage-5A scoped-key cutover audit contracts."""
from __future__ import annotations

import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.schema import CreateIndex, DropIndex

from tests.conftest import legacy_unenforced_write
from vitals.enums import Domain, Source
from vitals.models.base import Base
from vitals.models.garmin import GarminDaily
from vitals.models.labs import LabMarker
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.weight import WeightLog
from vitals.scoped_keys import (
    SCOPED_KEY_REGISTRY,
    SCOPED_KEYS,
    LegacyKeyKind,
    ScopeKind,
)
from vitals.operations.ownership import validate as validation
from vitals.operations.ownership import audit as service


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader does when the ownership backfill has not reached a row yet, which is
# a state the application itself can no longer create. The schema says so, so
# this module asks for the one that stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


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


async def _stage4_completed(session, roots):
    session.add_all(
        [_checkpoint(phase, roots.subject_id) for phase in validation.STAGE3_PHASES]
    )
    await session.flush()
    recorded = await validation.run_ownership_validation(session)
    assert recorded.completed
    return recorded


def _weight(*, subject_id, on_date=date(2026, 1, 2), superseded=False):
    return WeightLog(
        subject_id=subject_id,
        date=on_date,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=81.5,
        superseded=superseded,
    )


def test_public_contract_is_fixed():
    assert service.SCOPED_KEY_AUDIT_PHASE == "stage5.scoped_key_audit.v1"
    assert [item.value for item in service.ScopedKeyAuditStatus] == [
        "not_started",
        "completed",
    ]


def test_reviewed_catalog_matches_the_live_schema():
    """Every legacy key named by the catalog must actually exist today."""

    from vitals.models.base import Base

    for spec in SCOPED_KEYS:
        table = Base.metadata.tables[spec.table]
        names = {constraint.name for constraint in table.constraints}
        names |= {index.name for index in table.indexes}
        assert spec.legacy_name not in names, spec.legacy_name
        for column in (*spec.legacy_columns, *sum(
            (index.columns for index in spec.replacements), ()
        )):
            assert column in table.columns, (spec.table, column)
        # Revision 0048 dropped the legacy key; only the replacements stand.
        for index in spec.replacements:
            assert index.name in names, index.name


def test_every_scoped_key_names_a_scope_or_is_deliberately_global():
    for spec in SCOPED_KEYS:
        assert isinstance(spec.scope, ScopeKind)
        assert isinstance(spec.legacy_kind, LegacyKeyKind)
        scoped = [
            index for index in spec.replacements if index.required_scope_column
        ]
        # A mixed catalog and the platform alert class keep one deliberately
        # global replacement; everything else must be scoped by something.
        if spec.scope in {ScopeKind.SUBJECT, ScopeKind.CONNECTION}:
            assert len(scoped) == len(spec.replacements), spec.legacy_name
        else:
            assert scoped, spec.legacy_name


@pytest.mark.asyncio
async def test_clean_lake_records_reviewed_evidence(db_session, legacy_owner_roots):
    db_session.add(_weight(subject_id=legacy_owner_roots.subject_id))
    await db_session.flush()
    await _stage4_completed(db_session, legacy_owner_roots)

    before = await service.preflight_scoped_key_audit(db_session)
    assert not before.completed
    assert before.collisions_total == 0
    assert before.unscoped_rows_total == 0
    assert before.legacy_keys_total == len(SCOPED_KEYS)
    assert before.scoped_indexes_total == sum(
        len(spec.replacements) for spec in SCOPED_KEYS
    )
    assert before.rows_inspected >= 1

    recorded = await service.run_scoped_key_audit(db_session)
    assert recorded.completed
    assert recorded.audit_checksum == before.audit_checksum

    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key == service.SCOPED_KEY_AUDIT_PHASE
        )
    )
    assert checkpoint is not None
    assert checkpoint.updated_rows == 0
    assert checkpoint.ownership_checksum_after == recorded.audit_checksum

    again = await service.preflight_scoped_key_audit(db_session)
    assert again.completed and again.audit_checksum == recorded.audit_checksum


@pytest.mark.asyncio
async def test_missing_stage4_evidence_blocks_the_audit(
    db_session, legacy_owner_roots
):
    db_session.add_all(
        [
            _checkpoint(phase, legacy_owner_roots.subject_id)
            for phase in validation.STAGE3_PHASES
        ]
    )
    await db_session.flush()

    # Stage 3 is terminal but Stage 4 was never recorded.
    with pytest.raises(service.ScopedKeyAuditDependencyError):
        await service.preflight_scoped_key_audit(db_session)


@pytest.mark.asyncio
async def test_stale_stage4_evidence_blocks_the_audit(db_session, legacy_owner_roots):
    await _stage4_completed(db_session, legacy_owner_roots)
    # Data written after Stage 4 was recorded means the proof no longer
    # describes this lake, so the cutover audit must not proceed on it.
    db_session.add(_weight(subject_id=legacy_owner_roots.subject_id))
    await db_session.flush()

    with pytest.raises(service.ScopedKeyAuditDependencyError):
        await service.run_scoped_key_audit(db_session)


@pytest.mark.asyncio
async def test_row_without_its_connection_scope_fails_closed(
    db_session, legacy_owner_roots
):
    """The check the whole audit exists for.

    Stage 4 proves ownership references never leave the reviewed roots, but a
    provider row with no connection at all still passes it. Under
    ``(integration_connection_id, date)`` such a row would keep no uniqueness
    whatsoever, so the cutover would quietly lose the rule it was replacing.
    """

    db_session.add(
        GarminDaily(
            subject_id=legacy_owner_roots.subject_id,
            integration_connection_id=None,
            date=date(2026, 2, 3),
            domain=Domain.GARMIN.value,
            source=Source.GARMIN_API.value,
        )
    )
    await db_session.flush()
    await _stage4_completed(db_session, legacy_owner_roots)

    with pytest.raises(service.ScopedKeyAuditCollision):
        await service.preflight_scoped_key_audit(db_session)


def _dialect(session) -> str:
    """The dialect this session is actually speaking.

    These cases used to pass ``"sqlite"`` literally, which is right for the
    fast suite and wrong for the integration one: the audit builds each index's
    partial predicate per dialect, and the SQLite form compares a boolean
    column to ``1`` — not an operator PostgreSQL has. The test then failed on
    the database it exists to make claims about.
    """

    return session.bind.dialect.name if session.bind is not None else "sqlite"


@asynccontextmanager
async def _without_indexes(session, *names: str):
    """Drop unique indexes the way a cutover — or a restore — leaves them.

    The schema is shared across the fast suite, so each index is recreated from
    its own metadata definition before the test returns.
    """

    indexes = [
        next(
            candidate
            for table in Base.metadata.tables.values()
            for candidate in table.indexes
            if candidate.name == name
        )
        for name in names
    ]
    for index in indexes:
        await session.execute(DropIndex(index, if_exists=True))
    try:
        yield
    finally:
        await session.rollback()
        # ``if_not_exists`` because DDL is transactional on PostgreSQL: the
        # rollback above puts the index back, and an unconditional CREATE then
        # fails with "already exists" — so this helper errored on the only
        # database production runs while working on SQLite, where the drop
        # survives the rollback. Both are handled by asking for the end state
        # rather than for the step.
        for index in indexes:
            await session.execute(CreateIndex(index, if_not_exists=True))


@pytest.mark.asyncio
async def test_collision_under_a_proposed_key_fails_closed(
    db_session, legacy_owner_roots
):
    # The installed scoped key refuses the duplicate outright. The audit guards
    # the lake that arrives without it — a restore, or a cutover being replayed
    # — so it is dropped here first.
    async with _without_indexes(db_session, "uq_lab_markers_subject_name"):
        db_session.add_all(
            [
                LabMarker(
                    subject_id=legacy_owner_roots.subject_id,
                    name="ferritin",
                    unit="ng/mL",
                ),
                LabMarker(
                    subject_id=legacy_owner_roots.subject_id,
                    name="ferritin",
                    unit="ng/mL",
                ),
            ]
        )
        await db_session.flush()

        table = Base.metadata.tables["lab_markers"]
        index = SCOPED_KEY_REGISTRY["ix_lab_markers_name"].replacements[0]
        in_scope, collisions, missing = await service._audit_index(
            db_session, table=table, index=index, dialect=_dialect(db_session)
        )
        assert in_scope == 2
        assert collisions == 1
        assert missing == 0


@pytest.mark.asyncio
async def test_two_subjects_may_share_a_date_under_the_scoped_key(
    db_session, legacy_owner_roots
):
    """The whole point of the cutover, proved before it is installed."""

    same_day = date(2026, 5, 6)
    if True:
        db_session.add(
            _weight(subject_id=legacy_owner_roots.subject_id, on_date=same_day)
        )
        async with legacy_unenforced_write(db_session):
            # A second subject is not made writable here; only the shape of its
            # row is asked about, which is exactly what the audit inspects.
            db_session.add(_weight(subject_id=uuid.uuid4(), on_date=same_day))

        table = Base.metadata.tables["weight_logs"]
        index = SCOPED_KEY_REGISTRY["uq_active_weight_per_date"].replacements[0]
        in_scope, collisions, missing = await service._audit_index(
            db_session, table=table, index=index, dialect=_dialect(db_session)
        )
        assert in_scope == 2
        # One date, two people, no collision: this is what the legacy global key
        # made impossible.
        assert collisions == 0
        assert missing == 0
