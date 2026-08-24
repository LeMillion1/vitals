"""Focused SQLite contracts for the Stage-3C HRT-child backfill."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, Source, UserStatus
from vitals.models.hrt import (
    HrtCompound,
    HrtCycle,
    HrtCycleItem,
    HrtCycleTemplate,
    HrtCycleTemplateItem,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.services import (
    conflict_engine,
    hrt_catalog,
    hrt_child_ownership_backfill_service as backfill_service,
)
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    HRT_CHILD_OWNERSHIP_BACKFILL_PHASE,
    HRT_CHILD_OWNERSHIP_BACKFILL_TABLES,
    MAX_HRT_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE,
    HrtChildOwnershipBackfillDependencyError,
    HrtChildOwnershipBackfillIdentityError,
    HrtChildOwnershipBackfillProvenanceError,
    HrtChildOwnershipBackfillStateError,
    HrtChildOwnershipBackfillStatus,
    HrtChildOwnershipBackfillValidationError,
    preflight_hrt_child_ownership_backfill,
    reset_hrt_child_backfill_for_portability_v1_restore,
    run_hrt_child_ownership_backfill_batch,
)
from vitals.services.hrt_cycle_service import list_cycles
from vitals.services.hrt_service import resolve_active_scoped
from vitals.services.hrt_template_service import list_templates
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)


# Every test here writes or inspects a row with no owner, which is the whole
# subject of the ownership backfill: these services exist to give such rows an
# owner. The application can no longer produce that state, so this module asks
# for the schema as it stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _checkpoint(
    *,
    phase: str,
    subject_id: uuid.UUID,
    status: str = "completed",
    high_watermark: int = 0,
    snapshot_rows: int = 0,
) -> OwnershipBackfillCheckpoint:
    completed = status == "completed"
    timestamp = datetime(2020, 1, 1, 1, 2, 3, tzinfo=UTC)
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
        data_checksum_before=_EMPTY_SHA256,
        data_checksum_after=_EMPTY_SHA256,
        ownership_checksum_after=_EMPTY_SHA256,
        started_at=timestamp,
        updated_at=timestamp,
        completed_at=timestamp if completed else None,
    )


async def _scope(session, *, normalized_status: str = "completed"):
    owner = User(
        username="stage3c-owner",
        normalized_username="stage3c-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    session.add(
        _checkpoint(
            phase=RAW_OWNERSHIP_BACKFILL_PHASE,
            subject_id=subject.id,
        )
    )
    for phase in NORMALIZED_MANUAL_CHECKPOINT_PHASES.values():
        session.add(
            _checkpoint(
                phase=phase,
                subject_id=subject.id,
                status=normalized_status,
            )
        )
    await session.flush()
    return owner, subject


async def _parents(session, *, owner, subject, owned: bool = True):
    ownership = (
        {"subject_id": subject.id, "actor_user_id": None} if owned else {}
    )
    cycle = HrtCycle(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        kind="course",
        start_date=date(2026, 8, 1),
        name="synthetic cycle",
        **ownership,
    )
    template = HrtCycleTemplate(
        domain=Domain.HRT.value,
        source=Source.MCP.value,
        kind="course",
        name="synthetic template",
        **ownership,
    )
    session.add_all([cycle, template])
    await session.flush()
    for table_name, parent_id in (
        ("hrt_cycles", cycle.id),
        ("hrt_cycle_templates", template.id),
    ):
        checkpoint = await session.get(
            OwnershipBackfillCheckpoint,
            NORMALIZED_MANUAL_CHECKPOINT_PHASES[table_name],
        )
        assert checkpoint is not None
        checkpoint.scan_high_watermark_id = parent_id
        checkpoint.snapshot_rows = 1
        checkpoint.last_scanned_id = parent_id
        checkpoint.scanned_rows = 1
        checkpoint.updated_rows = 0
        checkpoint.unchanged_rows = 1
    await session.flush()
    return cycle, template


def _cycle_item(cycle, **ownership):
    return HrtCycleItem(
        cycle_id=cycle.id,
        compound_key="historical-free-text",
        unit="mg",
        start_offset_days=2,
        schedule=[{"dose": 10.0, "interval_days": 2}],
        note="synthetic child",
        **ownership,
    )


def _template_item(template, **ownership):
    return HrtCycleTemplateItem(
        template_id=template.id,
        compound_key="historical-free-text",
        unit="mg",
        start_offset_days=3,
        schedule=[{"dose": 12.0, "interval_days": 3}],
        note="synthetic template child",
        **ownership,
    )


async def _finish(session, *, batch_size: int = 1):
    for _ in range(10):
        result = await run_hrt_child_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("fixed HRT child catalog did not complete")


def _empty_bounds():
    return {name: (0, 0) for name in HRT_CHILD_OWNERSHIP_BACKFILL_TABLES}


def test_fixed_catalog_and_checkpoint_mapping_are_exact_and_immutable():
    assert HRT_CHILD_OWNERSHIP_BACKFILL_TABLES == (
        "hrt_cycle_items",
        "hrt_cycle_template_items",
    )
    assert tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        HRT_CHILD_OWNERSHIP_BACKFILL_TABLES
    )
    assert all(
        phase == f"{HRT_CHILD_OWNERSHIP_BACKFILL_PHASE}.{table_name}"
        and len(phase) <= 64
        for table_name, phase in (
            HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.items()
        )
    )
    with pytest.raises(TypeError):
        HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["extra"] = "bad"  # type: ignore[index]


@pytest.mark.asyncio
async def test_all_stage3b_dependencies_are_required_and_preflight_is_read_only(
    db_session,
):
    _owner, subject = await _scope(db_session)
    await db_session.execute(
        OwnershipBackfillCheckpoint.__table__.delete().where(
            OwnershipBackfillCheckpoint.phase_key
            == NORMALIZED_MANUAL_CHECKPOINT_PHASES["supplements"]
        )
    )
    before = int(
        await db_session.scalar(
            select(func.count()).select_from(OwnershipBackfillCheckpoint)
        )
        or 0
    )
    with pytest.raises(HrtChildOwnershipBackfillDependencyError):
        await preflight_hrt_child_ownership_backfill(db_session)
    assert (
        int(
            await db_session.scalar(
                select(func.count()).select_from(OwnershipBackfillCheckpoint)
            )
            or 0
        )
        == before
    )
    assert subject.id is not None


@pytest.mark.asyncio
async def test_running_stage3b_dependency_blocks_ordinary_apply(db_session):
    await _scope(db_session, normalized_status="running")
    with pytest.raises(HrtChildOwnershipBackfillDependencyError):
        await run_hrt_child_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
async def test_bounded_stop_resume_preserves_data_timestamps_and_unlocks_strict_consumers(
    db_session,
):
    owner, subject = await _scope(db_session)
    cycle, template = await _parents(
        db_session, owner=owner, subject=subject
    )
    first = _cycle_item(cycle)
    second = _cycle_item(cycle)
    template_child = _template_item(template)
    db_session.add_all([first, second, template_child])
    await db_session.commit()
    ids = (first.id, second.id, template_child.id)
    subject_id = subject.id
    timestamps = (first.updated_at, second.updated_at, template_child.updated_at)
    schedules = (first.schedule, second.schedule, template_child.schedule)

    preflight = await preflight_hrt_child_ownership_backfill(db_session)
    assert preflight.status is HrtChildOwnershipBackfillStatus.NOT_STARTED
    assert preflight.snapshot_rows == preflight.remaining_rows == 3
    assert "subject_id" not in preflight.to_safe_dict()

    one = await run_hrt_child_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert one.batch_table == "hrt_cycle_items"
    assert one.batch_scanned_rows == one.batch_updated_rows == 1
    await db_session.commit()

    two = await run_hrt_child_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert two.batch_table == "hrt_cycle_items"
    await db_session.commit()
    three = await run_hrt_child_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert three.batch_table == "hrt_cycle_template_items"
    assert three.completed
    await db_session.commit()

    db_session.expire_all()
    rows = [
        await db_session.get(HrtCycleItem, ids[0]),
        await db_session.get(HrtCycleItem, ids[1]),
        await db_session.get(HrtCycleTemplateItem, ids[2]),
    ]
    assert all(row is not None and row.subject_id == subject_id for row in rows)
    assert tuple(row.updated_at for row in rows) == timestamps
    assert tuple(row.schedule for row in rows) == schedules
    assert list(await list_cycles(db_session, subject_id=subject_id)) == [cycle]
    assert list(await list_templates(db_session, subject_id=subject_id)) == [
        template
    ]

    repeated = await run_hrt_child_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert repeated.completed
    assert repeated.batch_scanned_rows == repeated.batch_updated_rows == 0


@pytest.mark.asyncio
async def test_flush_only_batch_rolls_back_row_and_checkpoint(db_session):
    owner, subject = await _scope(db_session)
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject
    )
    child = _cycle_item(cycle)
    db_session.add(child)
    await db_session.commit()
    child_id = child.id

    await run_hrt_child_ownership_backfill_batch(db_session, batch_size=1)
    await db_session.rollback()
    restored = await db_session.get(HrtCycleItem, child_id)
    assert restored is not None and restored.subject_id is None
    assert await db_session.get(
        OwnershipBackfillCheckpoint,
        HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_cycle_items"],
    ) is None


@pytest.mark.asyncio
async def test_null_foreign_or_bad_parent_fails_without_child_mutation(db_session):
    owner, subject = await _scope(db_session)
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject, owned=False
    )
    child = _cycle_item(cycle)
    db_session.add(child)
    await db_session.flush()
    with pytest.raises(HrtChildOwnershipBackfillStateError, match="parent"):
        await preflight_hrt_child_ownership_backfill(db_session)
    assert child.subject_id is None
    await db_session.rollback()

    owner, subject = await _scope(db_session)
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject
    )
    cycle.domain = Domain.SYSTEM.value
    child = _cycle_item(cycle)
    db_session.add(child)
    await db_session.flush()
    with pytest.raises(HrtChildOwnershipBackfillProvenanceError):
        await preflight_hrt_child_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_parent_appended_after_stage3b_requires_exact_live_actor(db_session):
    owner, subject = await _scope(db_session)
    cycle = HrtCycle(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        kind="course",
        start_date=date(2026, 8, 1),
        subject_id=subject.id,
        actor_user_id=None,
    )
    db_session.add(cycle)
    await db_session.flush()
    db_session.add(_cycle_item(cycle, subject_id=subject.id))
    await db_session.flush()
    with pytest.raises(HrtChildOwnershipBackfillStateError, match="live.*actor"):
        await preflight_hrt_child_ownership_backfill(db_session)

    cycle.actor_user_id = owner.id
    await db_session.flush()
    result = await preflight_hrt_child_ownership_backfill(db_session)
    assert result.snapshot_rows == 1


@pytest.mark.asyncio
async def test_foreign_child_subject_fails_closed(
    db_session, unenforced_legacy_write
):
    owner, subject = await _scope(db_session)
    foreign_owner = User(
        username="foreign",
        normalized_username="foreign",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(foreign_owner)
    await db_session.flush()
    foreign_subject = HealthSubject(
        owner_user_id=foreign_owner.id, timezone="Asia/Almaty"
    )
    db_session.add(foreign_subject)
    await db_session.flush()
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject
    )
    # Revision 0046 prevents this mismatch on every new PostgreSQL write. The
    # backfill still has to reject a row that predates that constraint, so seed
    # it through the suite's explicit historical-data boundary.
    async with unenforced_legacy_write(db_session):
        db_session.add(_cycle_item(cycle, subject_id=foreign_subject.id))
    # Exact-one identity validation fails before the row can be considered.
    with pytest.raises(HrtChildOwnershipBackfillIdentityError) as exc_info:
        await preflight_hrt_child_ownership_backfill(db_session)
    assert "subject" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_above_high_watermark_requires_exact_live_child_ownership(db_session):
    owner, subject = await _scope(db_session)
    cycle, template = await _parents(
        db_session, owner=owner, subject=subject
    )
    db_session.add(_cycle_item(cycle))
    await db_session.flush()
    first = await run_hrt_child_ownership_backfill_batch(
        db_session, batch_size=10
    )
    assert first.completed_tables == 1
    cycle_id = cycle.id
    template_id = template.id
    subject_id = subject.id
    await db_session.commit()

    appended = _cycle_item(cycle)
    db_session.add(appended)
    await db_session.flush()
    with pytest.raises(HrtChildOwnershipBackfillStateError, match="high-water"):
        await run_hrt_child_ownership_backfill_batch(db_session, batch_size=1)
    await db_session.rollback()

    cycle = await db_session.get(HrtCycle, cycle_id)
    template = await db_session.get(HrtCycleTemplate, template_id)
    assert cycle is not None and template is not None
    db_session.add(_cycle_item(cycle, subject_id=subject_id))
    db_session.add(_template_item(template))
    await db_session.flush()
    result = await run_hrt_child_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert result.batch_table == "hrt_cycle_template_items"


@pytest.mark.asyncio
async def test_linked_compound_is_a_read_only_secondary_fail_closed_gate(db_session):
    owner, subject = await _scope(db_session)
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject
    )
    legacy_custom = HrtCompound(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        key="legacy_custom",
        name="Legacy custom",
        compound_class="peptide",
        route="subcutaneous",
    )
    db_session.add(legacy_custom)
    await db_session.flush()
    child = _cycle_item(cycle)
    child.compound_id = legacy_custom.id
    child.compound_key = legacy_custom.key
    db_session.add(child)
    await db_session.flush()
    await run_hrt_child_ownership_backfill_batch(db_session, batch_size=1)
    assert legacy_custom.subject_id is None
    assert legacy_custom.actor_user_id is None
    await db_session.rollback()

    owner, subject = await _scope(db_session)
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject
    )
    compound = HrtCompound(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        key="actual",
        name="Actual",
        compound_class="peptide",
        route="subcutaneous",
    )
    db_session.add(compound)
    await db_session.flush()
    child = _cycle_item(cycle)
    child.compound_id = compound.id
    child.compound_key = "mismatch"
    db_session.add(child)
    await db_session.flush()
    with pytest.raises(HrtChildOwnershipBackfillStateError, match="snapshot key"):
        await preflight_hrt_child_ownership_backfill(db_session)
    assert compound.subject_id is None


@pytest.mark.asyncio
async def test_live_tail_requires_exact_custom_compound_and_strict_resolver_accepts_it(
    db_session,
):
    owner, subject = await _scope(db_session)
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject
    )
    custom = HrtCompound(
        domain=Domain.HRT.value,
        source=Source.MCP.value,
        key="live_custom",
        name="Live custom",
        compound_class="peptide",
        route="subcutaneous",
    )
    db_session.add_all([custom, _cycle_item(cycle)])
    await db_session.flush()
    first = await run_hrt_child_ownership_backfill_batch(
        db_session, batch_size=10
    )
    assert first.completed_tables == 1

    appended = _cycle_item(cycle, subject_id=subject.id)
    appended.compound_id = custom.id
    appended.compound_key = custom.key
    db_session.add(appended)
    await db_session.flush()
    with pytest.raises(HrtChildOwnershipBackfillStateError, match="unowned custom"):
        await preflight_hrt_child_ownership_backfill(db_session)

    custom.subject_id = subject.id
    await db_session.flush()
    with pytest.raises(HrtChildOwnershipBackfillStateError, match="exact actor"):
        await preflight_hrt_child_ownership_backfill(db_session)

    custom.actor_user_id = owner.id
    await db_session.flush()
    status = await preflight_hrt_child_ownership_backfill(db_session)
    assert status.rows_above_high_watermark == 1
    resolved = await resolve_active_scoped(
        db_session,
        scope=conflict_engine.ConflictScope(
            subject_id=subject.id,
            evaluation_date=date(2026, 8, 21),
        ),
    )
    assert any(
        item[conflict_engine.CONFLICT_ENTITY_KEY]
        == f"cycle_item:{appended.id}"
        and item["compound_key"] == custom.key
        for item in resolved
    )


@pytest.mark.asyncio
async def test_system_compound_requires_full_checked_in_catalog_integrity(db_session):
    owner, subject = await _scope(db_session)
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject
    )
    await hrt_catalog.sync_catalog(db_session)
    compound = await db_session.scalar(
        select(HrtCompound).where(
            HrtCompound.key == "testosterone_enanthate"
        )
    )
    assert compound is not None
    child = _cycle_item(cycle)
    child.compound_id = compound.id
    child.compound_key = compound.key
    db_session.add(child)
    compound.conc_mg_ml = 999
    await db_session.flush()

    with pytest.raises(
        HrtChildOwnershipBackfillProvenanceError,
        match="catalog integrity",
    ):
        await preflight_hrt_child_ownership_backfill(db_session)
    assert child.subject_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutated_fk", ["cycle_id", "compound_id"])
async def test_child_fk_tuple_is_revalidated_after_root_locks(
    db_session,
    monkeypatch,
    mutated_fk,
):
    owner, subject = await _scope(db_session)
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject
    )
    alternate_cycle = HrtCycle(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind="course",
        start_date=date(2026, 8, 2),
        name="alternate cycle",
    )
    compounds = [
        HrtCompound(
            domain=Domain.HRT.value,
            source=Source.MANUAL.value,
            subject_id=subject.id,
            actor_user_id=owner.id,
            key=f"lock_compound_{index}",
            name=f"Lock compound {index}",
            compound_class="peptide",
            route="subcutaneous",
        )
        for index in (1, 2)
    ]
    db_session.add_all([alternate_cycle, *compounds])
    await db_session.flush()
    child = _cycle_item(cycle)
    if mutated_fk == "compound_id":
        child.compound_id = compounds[0].id
        child.compound_key = compounds[0].key
    db_session.add(child)
    await db_session.flush()

    original_load = backfill_service._load_full_child
    mutated = False

    async def _mutating_load(session, *, spec, row_id, for_update):
        nonlocal mutated
        if not mutated and spec.name == "hrt_cycle_items" and row_id == child.id:
            mutated = True
            replacement = (
                alternate_cycle.id
                if mutated_fk == "cycle_id"
                else compounds[1].id
            )
            await session.execute(
                update(HrtCycleItem)
                .where(HrtCycleItem.id == child.id)
                .values({mutated_fk: replacement})
            )
            await session.flush()
        return await original_load(
            session,
            spec=spec,
            row_id=row_id,
            for_update=for_update,
        )

    monkeypatch.setattr(backfill_service, "_load_full_child", _mutating_load)
    with pytest.raises(
        HrtChildOwnershipBackfillStateError,
        match="link changed after root locking",
    ):
        await run_hrt_child_ownership_backfill_batch(
            db_session, batch_size=1
        )
    assert mutated
    assert await db_session.scalar(
        select(HrtCycleItem.subject_id).where(HrtCycleItem.id == child.id)
    ) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_child_fk_race_fails_before_backfill_progress(
    db_session,
    monkeypatch,
):
    owner, subject = await _scope(db_session)
    cycle, _template = await _parents(
        db_session, owner=owner, subject=subject
    )
    alternate_cycle = HrtCycle(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind="course",
        start_date=date(2026, 8, 2),
        name="concurrent alternate cycle",
    )
    child = _cycle_item(cycle)
    db_session.add_all([alternate_cycle, child])
    await db_session.commit()
    child_id = child.id
    alternate_cycle_id = alternate_cycle.id

    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    roots_locked = asyncio.Event()
    writer_committed = asyncio.Event()
    original_lock = backfill_service._lock_compounds_for_links
    paused = False

    async def _pausing_lock(session, *, spec, links, for_update):
        nonlocal paused
        await original_lock(
            session,
            spec=spec,
            links=links,
            for_update=for_update,
        )
        if not paused and spec.name == "hrt_cycle_items" and links:
            paused = True
            roots_locked.set()
            await asyncio.wait_for(writer_committed.wait(), timeout=5)

    monkeypatch.setattr(
        backfill_service,
        "_lock_compounds_for_links",
        _pausing_lock,
    )

    async def _worker_a():
        async with factory() as session:
            try:
                await run_hrt_child_ownership_backfill_batch(
                    session, batch_size=1
                )
            except Exception as exc:  # returned for exact assertion below
                await session.rollback()
                return exc
            await session.commit()
            return None

    task_a = asyncio.create_task(_worker_a())
    error = None
    try:
        await asyncio.wait_for(roots_locked.wait(), timeout=5)
        async with factory() as session_b:
            await session_b.execute(
                update(HrtCycleItem)
                .where(HrtCycleItem.id == child_id)
                .values(cycle_id=alternate_cycle_id)
            )
            await asyncio.wait_for(session_b.commit(), timeout=5)
        writer_committed.set()
        error = await asyncio.wait_for(task_a, timeout=5)
    finally:
        writer_committed.set()
        if not task_a.done():
            task_a.cancel()
            await asyncio.gather(task_a, return_exceptions=True)

    assert isinstance(error, HrtChildOwnershipBackfillStateError)
    assert "link changed after root locking" in str(error)
    async with factory() as verification:
        persisted = (
            await verification.execute(
                select(
                    HrtCycleItem.cycle_id,
                    HrtCycleItem.subject_id,
                ).where(HrtCycleItem.id == child_id)
            )
        ).one()
        assert tuple(persisted) == (alternate_cycle_id, None)
        assert await verification.get(
            OwnershipBackfillCheckpoint,
            HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "hrt_cycle_items"
            ],
        ) is None


@pytest.mark.asyncio
async def test_completed_snapshot_allows_business_edits_but_rejects_ownership_drift(
    db_session,
):
    owner, subject = await _scope(db_session)
    cycle, template = await _parents(
        db_session, owner=owner, subject=subject
    )
    child = _cycle_item(cycle)
    db_session.add(child)
    await db_session.flush()
    await _finish(db_session, batch_size=10)
    child.note = "legitimate later edit"
    await db_session.flush()
    status = await preflight_hrt_child_ownership_backfill(db_session)
    assert status.completed
    repeated = await run_hrt_child_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert repeated.completed and repeated.batch_scanned_rows == 0

    await db_session.execute(
        update(HrtCycleItem)
        .where(HrtCycleItem.id == child.id)
        .values(subject_id=None)
    )
    await db_session.flush()
    with pytest.raises(HrtChildOwnershipBackfillStateError):
        await preflight_hrt_child_ownership_backfill(db_session)
    with pytest.raises(HrtChildOwnershipBackfillStateError):
        await run_hrt_child_ownership_backfill_batch(
            db_session, batch_size=1
        )
    assert template.id is not None


@pytest.mark.asyncio
async def test_group_finalization_detects_cross_table_data_drift(db_session):
    owner, subject = await _scope(db_session)
    cycle, template = await _parents(
        db_session, owner=owner, subject=subject
    )
    first = _cycle_item(cycle)
    second = _template_item(template)
    db_session.add_all([first, second])
    await db_session.flush()
    result = await run_hrt_child_ownership_backfill_batch(
        db_session, batch_size=10
    )
    assert result.completed_tables == 1
    first.note = "changed before the maintenance window closed"
    await db_session.flush()
    with pytest.raises(HrtChildOwnershipBackfillStateError, match="data changed"):
        await run_hrt_child_ownership_backfill_batch(db_session, batch_size=10)


@pytest.mark.asyncio
async def test_final_table_and_group_rehash_lock_rows_and_require_data_checksum(
    db_session, monkeypatch
):
    owner, subject = await _scope(db_session)
    cycle, template = await _parents(
        db_session, owner=owner, subject=subject
    )
    db_session.add_all([_cycle_item(cycle), _template_item(template)])
    await db_session.flush()
    calls: list[tuple[str, bool, bool]] = []
    original = backfill_service._verify_final_snapshot

    async def tracked(*args, spec, for_update, require_data_checksum, **kwargs):
        calls.append((spec.name, for_update, require_data_checksum))
        return await original(
            *args,
            spec=spec,
            for_update=for_update,
            require_data_checksum=require_data_checksum,
            **kwargs,
        )

    monkeypatch.setattr(backfill_service, "_verify_final_snapshot", tracked)
    completed = await _finish(db_session, batch_size=10)
    assert completed.completed
    assert {name for name, _locked, _data in calls} == set(
        HRT_CHILD_OWNERSHIP_BACKFILL_TABLES
    )
    assert all(locked and require_data for _name, locked, require_data in calls)


@pytest.mark.asyncio
async def test_portability_reset_uses_exact_bounds_flush_only_and_accepts_running_stage3b(
    db_session,
):
    _owner, subject = await _scope(db_session, normalized_status="running")
    bounds = _empty_bounds()
    bounds["hrt_cycle_items"] = (5, 2)
    await reset_hrt_child_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds=bounds
    )
    checkpoints = list(
        await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
                )
            )
        )
    )
    assert len(checkpoints) == 2
    by_phase = {row.phase_key: row for row in checkpoints}
    populated = by_phase[
        HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_cycle_items"]
    ]
    assert populated.status == "running"
    assert (populated.scan_high_watermark_id, populated.snapshot_rows) == (5, 2)
    empty = by_phase[
        HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
            "hrt_cycle_template_items"
        ]
    ]
    assert empty.status == "completed" and empty.completed_at is not None
    assert empty.subject_id == subject.id
    await db_session.rollback()
    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(OwnershipBackfillCheckpoint)
                .where(
                    OwnershipBackfillCheckpoint.phase_key.like(
                        f"{HRT_CHILD_OWNERSHIP_BACKFILL_PHASE}.%"
                    )
                )
            )
            or 0
        )
        == 0
    )


@pytest.mark.asyncio
async def test_portability_reset_locks_but_does_not_trust_replaced_legacy_graph(
    db_session,
):
    owner, subject = await _scope(db_session, normalized_status="running")
    cycle, template = await _parents(
        db_session, owner=owner, subject=subject, owned=False
    )
    db_session.add_all([_cycle_item(cycle), _template_item(template)])
    await db_session.flush()

    bounds = {
        "hrt_cycle_items": (7, 1),
        "hrt_cycle_template_items": (9, 1),
    }
    await reset_hrt_child_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds=bounds
    )
    rows = list(
        await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
                )
            )
        )
    )
    assert len(rows) == 2 and all(row.status == "running" for row in rows)
    assert cycle.subject_id is None and template.subject_id is None


@pytest.mark.asyncio
async def test_portability_reset_accepts_restore_blocked_raw_dependency(db_session):
    _owner, subject = await _scope(db_session, normalized_status="running")
    raw = await db_session.get(
        OwnershipBackfillCheckpoint, RAW_OWNERSHIP_BACKFILL_PHASE
    )
    assert raw is not None
    raw.status = "restore_blocked"
    raw.scan_high_watermark_id = 3
    raw.snapshot_rows = 2
    raw.last_scanned_id = 0
    raw.scanned_rows = raw.updated_rows = raw.unchanged_rows = 0
    raw.completed_at = None
    raw.started_at = datetime(2020, 1, 1, tzinfo=UTC)
    await db_session.flush()
    await reset_hrt_child_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds=_empty_bounds()
    )
    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(OwnershipBackfillCheckpoint)
                .where(
                    OwnershipBackfillCheckpoint.phase_key.in_(
                        tuple(
                            HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
                        )
                    )
                )
            )
            or 0
        )
        == 2
    )
    assert subject.id is not None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda bounds: bounds.pop("hrt_cycle_items"),
        lambda bounds: bounds.__setitem__("extra", (0, 0)),
        lambda bounds: bounds.__setitem__("hrt_cycle_items", (True, 1)),
        lambda bounds: bounds.__setitem__("hrt_cycle_items", (1 << 31, 1)),
        lambda bounds: bounds.__setitem__("hrt_cycle_items", (1, 2)),
        lambda bounds: bounds.__setitem__("hrt_cycle_items", (2, 0)),
    ],
)
@pytest.mark.asyncio
async def test_portability_reset_rejects_invalid_bounds_before_writes(
    db_session, mutate
):
    await _scope(db_session)
    bounds = _empty_bounds()
    mutate(bounds)
    with pytest.raises(HrtChildOwnershipBackfillValidationError):
        await reset_hrt_child_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds=bounds
        )
    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(OwnershipBackfillCheckpoint)
                .where(
                    OwnershipBackfillCheckpoint.phase_key.like(
                        f"{HRT_CHILD_OWNERSHIP_BACKFILL_PHASE}.%"
                    )
                )
            )
            or 0
        )
        == 0
    )


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, 1001])
@pytest.mark.asyncio
async def test_batch_size_is_strictly_bounded(db_session, batch_size):
    await _scope(db_session)
    with pytest.raises(HrtChildOwnershipBackfillValidationError):
        await run_hrt_child_ownership_backfill_batch(
            db_session, batch_size=batch_size
        )
    assert MAX_HRT_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE == 1000
