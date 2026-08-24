"""Focused SQLite/PostgreSQL contracts for Stage-3F HRT catalog ownership."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, Source, UserStatus
from vitals.models.hrt import (
    HrtCompound,
    HrtCompoundComponent,
    HrtCycle,
    HrtCycleItem,
    HrtDose,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.services import hrt_catalog
from vitals.services import hrt_compound_ownership_backfill_service as service
from vitals.services.hevy_child_ownership_backfill_service import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.hrt_compound_ownership_backfill_service import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE,
    HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES,
    MAX_HRT_COMPOUND_OWNERSHIP_BACKFILL_BATCH_SIZE,
    HrtCompoundOwnershipBackfillDependencyError,
    HrtCompoundOwnershipBackfillProvenanceError,
    HrtCompoundOwnershipBackfillStateError,
    HrtCompoundOwnershipBackfillValidationError,
    preflight_hrt_compound_ownership_backfill,
    reset_hrt_compound_backfill_for_portability_v1_restore,
    run_hrt_compound_ownership_backfill_batch,
)
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.raw_ownership_backfill_service import RAW_OWNERSHIP_BACKFILL_PHASE


_EMPTY = hashlib.sha256(b"").hexdigest()
_STAMP = datetime(2020, 1, 1, tzinfo=UTC)


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


def _make_restore_blocked(
    checkpoint: OwnershipBackfillCheckpoint, *, high: int = 1, count: int = 1
) -> None:
    checkpoint.status = "restore_blocked"
    checkpoint.scan_high_watermark_id = high
    checkpoint.snapshot_rows = count
    checkpoint.last_scanned_id = 0
    checkpoint.scanned_rows = 0
    checkpoint.updated_rows = 0
    checkpoint.unchanged_rows = 0
    checkpoint.data_checksum_before = _EMPTY
    checkpoint.data_checksum_after = _EMPTY
    checkpoint.ownership_checksum_after = _EMPTY
    checkpoint.completed_at = None


async def _scope(session):
    owner = User(
        username="stage3f-owner",
        normalized_username="stage3f-owner",
        password_hash="$synthetic",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    phases = (
        (RAW_OWNERSHIP_BACKFILL_PHASE,)
        + tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
        + tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
        + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    )
    session.add_all([_checkpoint(phase, subject.id) for phase in phases])
    await session.flush()
    return owner, subject


def _custom(*, subject_id=None, actor_user_id=None, key="custom_blend"):
    return HrtCompound(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        key=key,
        name="Synthetic custom",
        compound_class="peptide",
        route="subcutaneous",
    )


async def _finish(session, *, size=1000):
    for _ in range(5):
        result = await run_hrt_compound_ownership_backfill_batch(
            session, batch_size=size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3F did not complete")


def test_public_contract_is_fixed():
    assert HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE == "stage3.mixed_catalog.hrt.v1"
    assert HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES == (
        "hrt_compounds",
        "hrt_compound_components",
    )
    assert tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES
    )
    with pytest.raises(TypeError):
        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_historical_custom_graph_is_adopted_and_curated_graph_stays_global(
    db_session,
):
    _owner, subject = await _scope(db_session)
    await hrt_catalog.sync_catalog(db_session)
    custom = _custom()
    custom.components.append(HrtCompoundComponent(ester="synthetic", mg=25.0))
    db_session.add(custom)
    await db_session.flush()
    timestamps = (custom.created_at, custom.updated_at, custom.components[0].updated_at)

    final = await _finish(db_session)
    assert final.completed and final.updated_rows == 2
    assert custom.subject_id == subject.id and custom.actor_user_id is None
    assert custom.components[0].subject_id == subject.id
    assert (custom.created_at, custom.updated_at, custom.components[0].updated_at) == timestamps
    curated = await db_session.scalar(
        select(HrtCompound).where(HrtCompound.key == "sustanon_250")
    )
    assert curated is not None
    assert curated.subject_id is None and curated.actor_user_id is None
    assert all(component.subject_id is None for component in curated.components)
    assert "subject_id" not in final.to_safe_dict()


@pytest.mark.asyncio
async def test_scalar_and_component_catalog_tamper_fail_before_mutation(db_session):
    _owner, _subject = await _scope(db_session)
    await hrt_catalog.sync_catalog(db_session)
    curated = await db_session.scalar(
        select(HrtCompound).where(HrtCompound.key == "sustanon_250")
    )
    curated.conc_mg_ml = 999
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillProvenanceError, match="catalog integrity"):
        await preflight_hrt_compound_ownership_backfill(db_session)
    await db_session.rollback()

    await _scope(db_session)
    await hrt_catalog.sync_catalog(db_session)
    curated = await db_session.scalar(
        select(HrtCompound).where(HrtCompound.key == "sustanon_250")
    )
    curated.components[0].mg += 1
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillProvenanceError, match="components"):
        await preflight_hrt_compound_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_custom_curated_key_collision_and_unknown_source_fail(db_session):
    _owner, _subject = await _scope(db_session)
    await hrt_catalog.sync_catalog(db_session)
    curated = await db_session.scalar(
        select(HrtCompound).where(HrtCompound.key == "testosterone_enanthate")
    )
    curated.source = Source.MANUAL.value
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillProvenanceError, match="collides"):
        await preflight_hrt_compound_ownership_backfill(db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", " custom", "custom ", "Custom", "custom/key"])
async def test_custom_key_must_be_a_canonical_trimmed_slug(db_session, key):
    _owner, _subject = await _scope(db_session)
    db_session.add(_custom(key=key))
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillProvenanceError, match="canonical slug"):
        await preflight_hrt_compound_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_consumer_fk_and_snapshot_key_must_match(db_session):
    owner, subject = await _scope(db_session)
    custom = _custom()
    db_session.add(custom)
    await db_session.flush()
    db_session.add(
        HrtDose(
            subject_id=subject.id,
            actor_user_id=owner.id,
            date=date(2026, 8, 21),
            domain=Domain.HRT.value,
            source=Source.MANUAL.value,
            compound_id=custom.id,
            compound_key="wrong_snapshot",
            dose=1,
            unit="mg",
        )
    )
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillStateError, match="snapshot key"):
        await run_hrt_compound_ownership_backfill_batch(db_session, batch_size=1)
    assert custom.subject_id is None


@pytest.mark.asyncio
async def test_live_tail_requires_exact_subject_and_actor(db_session):
    owner, subject = await _scope(db_session)
    initial = _custom(key="initial")
    db_session.add(initial)
    await db_session.flush()
    await run_hrt_compound_ownership_backfill_batch(db_session, batch_size=1)
    live = _custom(key="live")
    db_session.add(live)
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillStateError, match="live"):
        await run_hrt_compound_ownership_backfill_batch(db_session, batch_size=1)
    live.subject_id = subject.id
    live.actor_user_id = owner.id
    await db_session.flush()
    assert (await _finish(db_session)).completed


@pytest.mark.asyncio
async def test_completed_custom_business_edit_allowed_but_ownership_drift_rejected(
    db_session,
    unenforced_legacy_write,
):
    _owner, subject = await _scope(db_session)
    custom = _custom()
    custom.components.append(HrtCompoundComponent(ester="custom", mg=12))
    db_session.add(custom)
    await db_session.flush()
    await _finish(db_session)
    custom.name = "Reviewed later name"
    custom.components[0].mg = 13
    await db_session.flush()
    assert (await preflight_hrt_compound_ownership_backfill(db_session)).completed
    # A current PostgreSQL write cannot drift the parent away from its stamped
    # component because revision 0046 enforces their composite ownership FK.
    # Seed the pre-constraint corruption explicitly so the service-level
    # completed-check still proves it fails closed on historical data.
    async with unenforced_legacy_write(db_session):
        custom.subject_id = None
    with pytest.raises(HrtCompoundOwnershipBackfillStateError):
        await preflight_hrt_compound_ownership_backfill(db_session)
    assert subject.id is not None


@pytest.mark.asyncio
async def test_partial_own_group_and_missing_dependency_fail_closed(db_session):
    _owner, subject = await _scope(db_session)
    db_session.add(
        _checkpoint(
            HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compounds"],
            subject.id,
        )
    )
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillStateError, match="partial"):
        await preflight_hrt_compound_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_checkpoint_data_evidence_must_match_for_dependencies_and_own_group(
    db_session,
):
    _owner, subject = await _scope(db_session)
    dependency = await db_session.get(
        OwnershipBackfillCheckpoint,
        NORMALIZED_MANUAL_CHECKPOINT_PHASES["supplements"],
    )
    dependency.data_checksum_after = "0" * 64
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillDependencyError, match="divergent"):
        await preflight_hrt_compound_ownership_backfill(db_session)
    await db_session.rollback()

    await _scope(db_session)
    custom = _custom()
    db_session.add(custom)
    await db_session.flush()
    await run_hrt_compound_ownership_backfill_batch(db_session, batch_size=1)
    checkpoint = await db_session.get(
        OwnershipBackfillCheckpoint,
        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compounds"],
    )
    checkpoint.data_checksum_after = "0" * 64
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillStateError, match="divergent"):
        await preflight_hrt_compound_ownership_backfill(db_session)
    assert subject.id is not None


@pytest.mark.asyncio
async def test_impossible_checkpoint_pair_and_empty_parent_restore_are_rejected(
    db_session,
):
    _owner, subject = await _scope(db_session)
    db_session.add_all(
        [
            _checkpoint(
                HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compounds"],
                subject.id,
                status="running",
                high=1,
                count=1,
            ),
            _checkpoint(
                HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                    "hrt_compound_components"
                ],
                subject.id,
                high=1,
                count=1,
            ),
        ]
    )
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillStateError, match="unless exactly empty"):
        await preflight_hrt_compound_ownership_backfill(db_session)
    with pytest.raises(HrtCompoundOwnershipBackfillValidationError, match="empty"):
        await reset_hrt_compound_backfill_for_portability_v1_restore(
            db_session,
            snapshot_bounds={
                "hrt_compounds": (0, 0),
                "hrt_compound_components": (3, 1),
            },
        )
    await db_session.rollback()
    _owner, _subject = await _scope(db_session)
    await db_session.execute(
        delete(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key
            == HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"]
        )
    )
    with pytest.raises(HrtCompoundOwnershipBackfillDependencyError):
        await preflight_hrt_compound_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_restore_mode_and_exact_two_table_reset(db_session):
    _owner, subject = await _scope(db_session)
    raw = await db_session.get(OwnershipBackfillCheckpoint, RAW_OWNERSHIP_BACKFILL_PHASE)
    raw.status = "restore_blocked"
    raw.scan_high_watermark_id = raw.snapshot_rows = 1
    raw.last_scanned_id = raw.scanned_rows = raw.unchanged_rows = 0
    raw.completed_at = None
    for phase in PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values():
        cp = await db_session.get(OwnershipBackfillCheckpoint, phase)
        cp.status = "restore_blocked"
        cp.scan_high_watermark_id = cp.snapshot_rows = 1
        cp.last_scanned_id = cp.scanned_rows = cp.unchanged_rows = 0
        cp.completed_at = None
    for phase in HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values():
        cp = await db_session.get(OwnershipBackfillCheckpoint, phase)
        cp.status = "restore_blocked"
        cp.scan_high_watermark_id = cp.snapshot_rows = 1
        cp.last_scanned_id = cp.scanned_rows = cp.unchanged_rows = 0
        cp.completed_at = None
    await db_session.flush()
    with pytest.raises(HrtCompoundOwnershipBackfillDependencyError, match="reset"):
        await preflight_hrt_compound_ownership_backfill(db_session)
    await reset_hrt_compound_backfill_for_portability_v1_restore(
        db_session,
        snapshot_bounds={"hrt_compounds": (7, 2), "hrt_compound_components": (0, 0)},
    )
    checkpoints = {
        row.phase_key: row
        for row in await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
                )
            )
        )
    }
    assert set(checkpoints) == set(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    assert checkpoints[HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compounds"]].status == "running"
    assert checkpoints[HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compound_components"]].status == "completed"
    assert subject.id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["reverse", "blocked_nonempty_child"])
async def test_stage3e_dependency_pair_is_enforced_at_every_boundary(
    db_session,
    shape,
):
    _owner, subject = await _scope(db_session)
    raw = await db_session.get(
        OwnershipBackfillCheckpoint, RAW_OWNERSHIP_BACKFILL_PHASE
    )
    _make_restore_blocked(raw)
    exercise = await db_session.get(
        OwnershipBackfillCheckpoint,
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_exercises"],
    )
    sets = await db_session.get(
        OwnershipBackfillCheckpoint,
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"],
    )
    if shape == "reverse":
        _make_restore_blocked(sets)
    else:
        _make_restore_blocked(exercise)
        sets.scan_high_watermark_id = 2
        sets.snapshot_rows = 1
        sets.last_scanned_id = 2
        sets.scanned_rows = 1
        sets.updated_rows = 0
        sets.unchanged_rows = 1
    db_session.add_all(
        [
            _checkpoint(
                HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compounds"],
                subject.id,
            ),
            _checkpoint(
                HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                    "hrt_compound_components"
                ],
                subject.id,
            ),
        ]
    )
    await db_session.flush()

    for boundary in ("preflight", "apply", "reset"):
        with pytest.raises(HrtCompoundOwnershipBackfillDependencyError):
            if boundary == "preflight":
                await preflight_hrt_compound_ownership_backfill(db_session)
            elif boundary == "apply":
                await run_hrt_compound_ownership_backfill_batch(
                    db_session, batch_size=1
                )
            else:
                await reset_hrt_compound_backfill_for_portability_v1_restore(
                    db_session,
                    snapshot_bounds={
                        "hrt_compounds": (0, 0),
                        "hrt_compound_components": (0, 0),
                    },
                )


@pytest.mark.asyncio
async def test_dirty_component_and_consumer_are_not_overwritten_by_core_scans(
    db_session,
):
    owner, subject = await _scope(db_session)
    first = _custom(key="dirty_first")
    second = _custom(key="dirty_second")
    component = HrtCompoundComponent(compound=first, ester="persisted", mg=10)
    dose = HrtDose(
        subject_id=subject.id,
        actor_user_id=owner.id,
        date=date(2026, 8, 21),
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        compound_id=None,
        compound_key=first.key,
        dose=1,
        unit="mg",
    )
    db_session.add_all([first, second, component])
    await db_session.flush()
    dose.compound_id = first.id
    db_session.add(dose)
    await db_session.flush()

    component.mg = 999
    component.compound_id = second.id
    dose.compound_id = second.id
    dose.compound_key = "dirty_unflushed_snapshot"
    assert db_session.is_modified(component)
    assert db_session.is_modified(dose)

    result = await preflight_hrt_compound_ownership_backfill(db_session)
    assert result.status.value == "not_started"
    assert (component.mg, component.compound_id) == (999, second.id)
    assert (dose.compound_id, dose.compound_key) == (
        second.id,
        "dirty_unflushed_snapshot",
    )
    assert db_session.is_modified(component)
    assert db_session.is_modified(dose)


@pytest.mark.asyncio
async def test_mutating_apply_does_not_refresh_dirty_component_medical_state(
    db_session,
):
    owner, subject = await _scope(db_session)
    custom = _custom(key="dirty_apply")
    second = _custom(key="dirty_apply_second")
    component = HrtCompoundComponent(compound=custom, ester="persisted", mg=10)
    dose = HrtDose(
        subject_id=subject.id,
        actor_user_id=owner.id,
        date=date(2026, 8, 22),
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        compound_id=None,
        compound_key=custom.key,
        dose=1,
        unit="mg",
    )
    db_session.add_all([custom, second])
    await db_session.flush()
    dose.compound_id = custom.id
    db_session.add(dose)
    await db_session.flush()
    component.mg = 777
    dose.compound_id = second.id
    dose.compound_key = second.key

    first = await run_hrt_compound_ownership_backfill_batch(
        db_session, batch_size=1000
    )
    assert first.completed_tables == 1
    assert component.mg == 777
    assert (dose.compound_id, dose.compound_key) == (second.id, second.key)
    final = await run_hrt_compound_ownership_backfill_batch(
        db_session, batch_size=1000
    )
    assert final.completed
    assert component.mg == 777
    assert component.subject_id == subject.id
    assert (dose.compound_id, dose.compound_key) == (second.id, second.key)


@pytest.mark.asyncio
async def test_batch_size_is_bounded(db_session):
    for value in (True, 0, MAX_HRT_COMPOUND_OWNERSHIP_BACKFILL_BATCH_SIZE + 1):
        with pytest.raises(HrtCompoundOwnershipBackfillValidationError):
            await run_hrt_compound_ownership_backfill_batch(
                db_session, batch_size=value  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_large_graph_queries_use_fixed_keyset_pages(db_session, monkeypatch):
    owner, subject = await _scope(db_session)
    monkeypatch.setattr(service, "_PAGE_SIZE", 2)
    # Exercise both the fixed YAML/system graph (including its four-component
    # blend) and a larger custom/consumer graph under the tiny test page.
    await hrt_catalog.sync_catalog(db_session)
    roots = [
        _custom(key=f"bounded_{index}")
        for index in range(5)
    ]
    for index, root in enumerate(roots):
        root.components.append(
            HrtCompoundComponent(ester=f"ester_{index}", mg=float(index + 1))
        )
        db_session.add(root)
    await db_session.flush()
    for index, root in enumerate(roots):
        db_session.add(
            HrtDose(
                subject_id=subject.id,
                actor_user_id=owner.id,
                date=date(2026, 8, 10 + index),
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                compound_id=root.id,
                compound_key=root.key,
                dose=1,
                unit="mg",
            )
        )
    await db_session.flush()

    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", record)
    try:
        assert (await _finish(db_session, size=1000)).completed
        assert (await preflight_hrt_compound_ownership_backfill(db_session)).completed
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", record)

    paged_patterns = (
        "from hrt_compounds where hrt_compounds.id >",
        "from hrt_compound_components where hrt_compound_components.id >",
        "from hrt_doses where hrt_doses.id >",
    )
    for pattern in paged_patterns:
        matching = [
            statement
            for statement in statements
            if pattern in statement and "count(" not in statement
        ]
        assert len(matching) >= 2
        assert all(" limit " in statement for statement in matching)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_parent_classification_switch_fails_without_progress(
    db_session,
    monkeypatch,
):
    _owner, _subject = await _scope(db_session)
    custom = _custom()
    db_session.add(custom)
    await db_session.commit()
    root_id = custom.id
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)
    projected = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(writer_done.wait(), timeout=5)

    monkeypatch.setattr(service, "_after_compound_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await run_hrt_compound_ownership_backfill_batch(session, batch_size=1)
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(projected.wait(), timeout=5)
        async with factory() as writer:
            await writer.execute(
                update(HrtCompound).where(HrtCompound.id == root_id).values(key="switched")
            )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=5)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert isinstance(error, HrtCompoundOwnershipBackfillStateError)
    async with factory() as verify:
        row = await verify.get(HrtCompound, root_id)
        assert row.key == "switched" and row.subject_id is None
        assert not list(
            await verify.scalars(
                select(OwnershipBackfillCheckpoint).where(
                    OwnershipBackfillCheckpoint.phase_key.in_(
                        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
                    )
                )
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["switch", "disappear"])
async def test_postgres_component_parent_races_fail_without_progress(
    db_session,
    monkeypatch,
    race,
):
    owner, subject = await _scope(db_session)
    first = _custom(subject_id=subject.id, actor_user_id=owner.id, key="first")
    second = _custom(subject_id=subject.id, actor_user_id=owner.id, key="second")
    component = HrtCompoundComponent(compound=first, ester="x", mg=1)
    db_session.add_all([first, second, component])
    await db_session.flush()
    await run_hrt_compound_ownership_backfill_batch(db_session, batch_size=10)
    await db_session.commit()
    component_id, second_id = component.id, second.id
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)
    parents_locked = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        parents_locked.set()
        await asyncio.wait_for(writer_done.wait(), timeout=5)

    monkeypatch.setattr(service, "_after_component_parents_locked_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await run_hrt_compound_ownership_backfill_batch(session, batch_size=1)
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(parents_locked.wait(), timeout=5)
        async with factory() as writer:
            if race == "switch":
                await writer.execute(
                    update(HrtCompoundComponent)
                    .where(HrtCompoundComponent.id == component_id)
                    .values(compound_id=second_id)
                )
            else:
                await writer.execute(
                    delete(HrtCompoundComponent).where(HrtCompoundComponent.id == component_id)
                )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=5)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert isinstance(error, HrtCompoundOwnershipBackfillStateError)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["dose", "cycle"])
async def test_postgres_consumer_fk_or_snapshot_race_fails_without_progress(
    db_session,
    monkeypatch,
    consumer,
):
    owner, subject = await _scope(db_session)
    first, second = _custom(key="first"), _custom(key="second")
    db_session.add_all([first, second])
    await db_session.flush()
    if consumer == "dose":
        fact = HrtDose(
            subject_id=subject.id,
            actor_user_id=owner.id,
            date=date(2026, 8, 21),
            domain=Domain.HRT.value,
            source=Source.MANUAL.value,
            compound_id=first.id,
            compound_key=first.key,
            dose=1,
            unit="mg",
        )
    else:
        cycle = HrtCycle(
            subject_id=subject.id,
            actor_user_id=owner.id,
            domain=Domain.HRT.value,
            source=Source.MANUAL.value,
            kind="course",
            start_date=date(2026, 8, 1),
        )
        fact = HrtCycleItem(
            subject_id=subject.id,
            cycle=cycle,
            compound_id=first.id,
            compound_key=first.key,
            unit="mg",
            schedule=[{"dose": 1, "interval_days": 1}],
        )
    db_session.add(fact)
    await db_session.commit()
    fact_id, second_id = fact.id, second.id
    model = HrtDose if consumer == "dose" else HrtCycleItem
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)
    roots_locked = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        roots_locked.set()
        await asyncio.wait_for(writer_done.wait(), timeout=5)

    monkeypatch.setattr(service, "_after_compound_roots_locked_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await run_hrt_compound_ownership_backfill_batch(session, batch_size=1)
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(roots_locked.wait(), timeout=5)
        async with factory() as writer:
            await writer.execute(
                update(model)
                .where(model.id == fact_id)
                .values(compound_id=second_id, compound_key="second")
            )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=5)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert isinstance(error, HrtCompoundOwnershipBackfillStateError)
