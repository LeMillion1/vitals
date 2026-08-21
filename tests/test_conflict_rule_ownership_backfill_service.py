"""Focused SQLite/PostgreSQL contracts for Stage-3G conflict-rule ownership."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, Severity, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.scoped_settings import SubjectSetting
from vitals.models.system_alert import SystemAlert
from vitals.services import conflict_catalog
from vitals.services import conflict_rule_ownership_backfill_service as service
from vitals.services.conflict_activation_service import SETTING_KEY
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


def _make_restore_blocked(checkpoint: OwnershipBackfillCheckpoint) -> None:
    checkpoint.status = "restore_blocked"
    checkpoint.scan_high_watermark_id = 1
    checkpoint.snapshot_rows = 1
    checkpoint.last_scanned_id = 0
    checkpoint.scanned_rows = 0
    checkpoint.updated_rows = 0
    checkpoint.unchanged_rows = 0
    checkpoint.data_checksum_before = _EMPTY
    checkpoint.data_checksum_after = _EMPTY
    checkpoint.ownership_checksum_after = _EMPTY
    checkpoint.completed_at = None


async def _scope(session: AsyncSession):
    owner = User(
        username="stage3g-owner",
        normalized_username="stage3g-owner",
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
        + tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    )
    checkpoints = [_checkpoint(phase, subject.id) for phase in phases]
    session.add_all(checkpoints)
    await session.flush()
    return owner, subject, {row.phase_key: row for row in checkpoints}


def _custom(*, subject_id=None, code=None, active=True, message="custom"):
    return ConflictRule(
        subject_id=subject_id,
        code=code,
        rule_type="soft_warn",
        domain_a="supplements",
        condition_a={"key": "synthetic-a"},
        domain_b="nutrition",
        condition_b={"key": "synthetic-b"},
        severity="warn",
        message=message,
        active=active,
    )


async def _finish(session: AsyncSession, *, size: int = 1000):
    for _ in range(5):
        result = await service.run_conflict_rule_ownership_backfill_batch(
            session, batch_size=size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3G did not complete")


def test_public_contract_is_fixed():
    assert (
        service.CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE
        == "stage3.mixed_catalog.conflict_rules.v1"
    )
    assert service.CONFLICT_RULE_OWNERSHIP_BACKFILL_TABLES == ("conflict_rules",)
    assert tuple(service.CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES) == (
        "conflict_rules",
    )
    assert [status.value for status in service.ConflictRuleOwnershipBackfillStatus] == [
        "not_started",
        "running",
        "completed",
    ]
    with pytest.raises(TypeError):
        service.CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_historical_custom_is_adopted_and_curated_catalog_stays_global(db_session):
    _owner, subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    custom = _custom()
    db_session.add(custom)
    await db_session.flush()
    timestamps = (custom.created_at, custom.updated_at)

    result = await _finish(db_session)

    assert result.completed and result.updated_rows == 1
    assert custom.subject_id == subject.id
    assert (custom.created_at, custom.updated_at) == timestamps
    curated = list(
        await db_session.scalars(
            select(ConflictRule).where(ConflictRule.code.is_not(None))
        )
    )
    assert curated and all(row.subject_id is None for row in curated)
    assert "subject_id" not in result.to_safe_dict()


@pytest.mark.asyncio
async def test_curated_tamper_missing_catalog_and_unknown_global_fail_closed(db_session):
    _owner, _subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    curated = await db_session.scalar(
        select(ConflictRule).where(ConflictRule.code.is_not(None)).limit(1)
    )
    curated.message = "tampered"
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillProvenanceError):
        await service.preflight_conflict_rule_ownership_backfill(db_session)
    await db_session.rollback()
    db_session.expunge_all()

    await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    curated = await db_session.scalar(
        select(ConflictRule).where(ConflictRule.code.is_not(None)).limit(1)
    )
    await db_session.delete(curated)
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillProvenanceError, match="incomplete"):
        await service.preflight_conflict_rule_ownership_backfill(db_session)
    await db_session.rollback()
    db_session.expunge_all()

    await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    db_session.add(_custom(code="unknown-global"))
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillProvenanceError):
        await service.preflight_conflict_rule_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_subject_custom_code_and_activation_pseudo_fk_are_strict(db_session):
    _owner, subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    db_session.add(_custom(subject_id=subject.id, code=" custom "))
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillProvenanceError, match="malformed code"):
        await service.preflight_conflict_rule_ownership_backfill(db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rule_type", "not-a-rule"),
        ("domain_a", "not-a-domain"),
        ("severity", "not-a-severity"),
        ("condition_a", []),
        ("params", []),
        ("message", "   "),
    ],
)
async def test_custom_engine_shape_is_strict(db_session, field, value):
    _owner, _subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    custom = _custom()
    setattr(custom, field, value)
    db_session.add(custom)
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillProvenanceError):
        await service.preflight_conflict_rule_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_conflict_alert_pseudo_fk_requires_rule_scope_and_compatible_domain(
    db_session,
):
    _owner, subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    custom = _custom(subject_id=subject.id)
    db_session.add(custom)
    await db_session.flush()
    alert = SystemAlert(
        subject_id=subject.id,
        domain=Domain.SUPPLEMENTS.value,
        severity=Severity.WARN.value,
        message="synthetic conflict",
        alert_key=f"conflict:{custom.id}",
        entity_ref="synthetic:1",
    )
    db_session.add(alert)
    await db_session.flush()
    assert (
        await service.preflight_conflict_rule_ownership_backfill(db_session)
    ).status is service.ConflictRuleOwnershipBackfillStatus.NOT_STARTED

    alert.subject_id = None
    await db_session.flush()
    assert (
        await service.preflight_conflict_rule_ownership_backfill(db_session)
    ).status is service.ConflictRuleOwnershipBackfillStatus.NOT_STARTED

    alert.subject_id = subject.id
    alert.domain = Domain.LABS.value
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillStateError, match="scope or domain"):
        await service.preflight_conflict_rule_ownership_backfill(db_session)

    alert.domain = Domain.SUPPLEMENTS.value
    alert.alert_key = "conflict:999999"
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillStateError, match="missing rule"):
        await service.preflight_conflict_rule_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_fully_unowned_historical_custom_rule_and_alert_bridge_passes(db_session):
    _owner, _subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    custom = _custom()
    db_session.add(custom)
    await db_session.flush()
    db_session.add(
        SystemAlert(
            subject_id=None,
            domain=Domain.SUPPLEMENTS.value,
            severity=Severity.WARN.value,
            message="legacy conflict",
            alert_key=f"conflict:{custom.id}",
            entity_ref="legacy:1",
        )
    )
    await db_session.flush()
    assert (
        await service.preflight_conflict_rule_ownership_backfill(db_session)
    ).status is service.ConflictRuleOwnershipBackfillStatus.NOT_STARTED
    await db_session.rollback()

    await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    db_session.add(
        SubjectSetting(
            subject_id=(await db_session.scalar(select(HealthSubject.id))),
            key=SETTING_KEY,
            value={"v": 1, "disabled_codes": ["not-in-yaml"]},
        )
    )
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillStateError, match="references"):
        await service.preflight_conflict_rule_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_live_tail_unowned_custom_rejected_without_progress(db_session):
    _owner, _subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    db_session.add(_custom())
    await db_session.flush()
    first = await service.run_conflict_rule_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert not first.completed
    tail = _custom()
    db_session.add(tail)
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillProvenanceError):
        await service.run_conflict_rule_ownership_backfill_batch(
            db_session, batch_size=1000
        )
    checkpoint = await db_session.get(
        OwnershipBackfillCheckpoint,
        service.CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["conflict_rules"],
    )
    assert tail.subject_id is None
    assert checkpoint.scanned_rows == first.scanned_rows


@pytest.mark.asyncio
async def test_completed_custom_business_edit_allowed_but_ownership_drift_rejected(db_session):
    _owner, subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    custom = _custom()
    db_session.add(custom)
    await db_session.flush()
    await _finish(db_session)

    custom.message = "reviewed business edit"
    custom.active = False
    await db_session.flush()
    assert (await service.preflight_conflict_rule_ownership_backfill(db_session)).completed

    custom.subject_id = None
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillStateError):
        await service.preflight_conflict_rule_ownership_backfill(db_session)
    assert subject.id is not None


@pytest.mark.asyncio
async def test_completed_evidence_allows_curated_delete_and_catalog_resync(db_session):
    _owner, _subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    custom = _custom()
    db_session.add(custom)
    await db_session.flush()
    await _finish(db_session)
    original_high = await db_session.scalar(select(ConflictRule.id).order_by(ConflictRule.id.desc()).limit(1))
    curated = await db_session.scalar(
        select(ConflictRule)
        .where(ConflictRule.code.is_not(None), ConflictRule.id < original_high)
        .order_by(ConflictRule.id)
        .limit(1)
    )
    old_id = curated.id
    await db_session.delete(curated)
    await db_session.flush()
    await conflict_catalog.sync_catalog(db_session)
    replacement = await db_session.scalar(
        select(ConflictRule).where(ConflictRule.code == curated.code)
    )
    assert replacement.id != old_id
    assert (await service.preflight_conflict_rule_ownership_backfill(db_session)).completed


@pytest.mark.asyncio
async def test_dependency_modes_and_restore_pair_algebra(db_session):
    _owner, _subject, checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    raw = checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE]
    _make_restore_blocked(raw)
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillDependencyError, match="reset"):
        await service.preflight_conflict_rule_ownership_backfill(db_session)

    high = await db_session.scalar(select(ConflictRule.id).order_by(ConflictRule.id.desc()).limit(1))
    count = len(list(await db_session.scalars(select(ConflictRule.id))))
    await service.reset_conflict_rule_backfill_for_portability_v1_restore(
        db_session,
        snapshot_bounds={"conflict_rules": (high, count)},
    )
    status = await service.preflight_conflict_rule_ownership_backfill(db_session)
    assert status.status is service.ConflictRuleOwnershipBackfillStatus.RUNNING

    exercises = checkpoints[
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_exercises"]
    ]
    sets = checkpoints[HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"]]
    exercises.status = "completed"
    _make_restore_blocked(sets)
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillDependencyError, match="Stage-3E"):
        await service.preflight_conflict_rule_ownership_backfill(db_session)

    await db_session.rollback()
    _owner, _subject, checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    _make_restore_blocked(checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE])
    provider = checkpoints[next(iter(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()))]
    provider.scan_high_watermark_id = 1
    provider.snapshot_rows = 1
    provider.last_scanned_id = 1
    provider.scanned_rows = 1
    provider.unchanged_rows = 1
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillDependencyError, match="Stage-3D"):
        await service.reset_conflict_rule_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds={"conflict_rules": (1, 1)}
        )


@pytest.mark.asyncio
async def test_reset_bounds_are_exact_and_empty_completes(db_session):
    _owner, _subject, _checkpoints = await _scope(db_session)
    for bounds in ({}, {"conflict_rules": [0, 0]}, {"conflict_rules": (0, 1)}):
        with pytest.raises(service.ConflictRuleOwnershipBackfillValidationError):
            await service.reset_conflict_rule_backfill_for_portability_v1_restore(
                db_session, snapshot_bounds=bounds
            )
    await service.reset_conflict_rule_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"conflict_rules": (0, 0)}
    )
    checkpoint = await db_session.get(
        OwnershipBackfillCheckpoint,
        service.CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["conflict_rules"],
    )
    assert checkpoint.status == "completed"


@pytest.mark.asyncio
async def test_empty_completed_raw_allows_restore_algebra_only_after_trusted_reset(
    db_session,
):
    _owner, _subject, checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    normalized = checkpoints[next(iter(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values()))]
    normalized.status = "running"
    normalized.scan_high_watermark_id = 1
    normalized.snapshot_rows = 1
    normalized.last_scanned_id = 0
    normalized.scanned_rows = 0
    normalized.unchanged_rows = 0
    normalized.completed_at = None
    compound = checkpoints[
        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compounds"]
    ]
    compound.status = "running"
    compound.scan_high_watermark_id = 1
    compound.snapshot_rows = 1
    compound.last_scanned_id = 0
    compound.scanned_rows = 0
    compound.unchanged_rows = 0
    compound.completed_at = None
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillDependencyError, match="completed"):
        await service.preflight_conflict_rule_ownership_backfill(db_session)

    high = await db_session.scalar(
        select(ConflictRule.id).order_by(ConflictRule.id.desc()).limit(1)
    )
    count = len(list(await db_session.scalars(select(ConflictRule.id))))
    await service.reset_conflict_rule_backfill_for_portability_v1_restore(
        db_session, snapshot_bounds={"conflict_rules": (high, count)}
    )
    assert (
        await service.preflight_conflict_rule_ownership_backfill(db_session)
    ).status is service.ConflictRuleOwnershipBackfillStatus.RUNNING


@pytest.mark.asyncio
async def test_restore_reset_rejects_nonempty_completed_raw_even_when_all_prior_complete(
    db_session,
):
    _owner, _subject, checkpoints = await _scope(db_session)
    raw = checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE]
    raw.scan_high_watermark_id = 1
    raw.snapshot_rows = 1
    raw.last_scanned_id = 1
    raw.scanned_rows = 1
    raw.unchanged_rows = 1
    await db_session.flush()
    with pytest.raises(service.ConflictRuleOwnershipBackfillDependencyError, match="exactly empty"):
        await service.reset_conflict_rule_backfill_for_portability_v1_restore(
            db_session, snapshot_bounds={"conflict_rules": (1, 1)}
        )


@pytest.mark.asyncio
async def test_scans_are_keyset_paged(db_session):
    _owner, subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    db_session.add_all([_custom(subject_id=subject.id) for _ in range(1002)])
    await db_session.flush()
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", record)
    try:
        await _finish(db_session)
        await service.preflight_conflict_rule_ownership_backfill(db_session)
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", record)
    scans = [
        statement
        for statement in statements
        if "from conflict_rules" in statement
        and "conflict_rules.id >" in statement
        and "count(" not in statement
    ]
    assert len(scans) >= 2
    assert all(" limit " in statement for statement in scans)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["classification", "disappearance", "active"])
async def test_postgres_projection_races_fail_without_ownership_or_progress(
    db_session,
    monkeypatch,
    race,
):
    _owner, _subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    custom = _custom()
    db_session.add(custom)
    await db_session.commit()
    row_id = custom.id
    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    projected = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(writer_done.wait(), timeout=5)

    monkeypatch.setattr(service, "_after_rule_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_conflict_rule_ownership_backfill_batch(
                    session, batch_size=1000
                )
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()
            return None

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(projected.wait(), timeout=5)
        async with factory() as writer:
            if race == "classification":
                await writer.execute(
                    update(ConflictRule)
                    .where(ConflictRule.id == row_id)
                    .values(code="now-classified-custom")
                )
            elif race == "active":
                await writer.execute(
                    update(ConflictRule)
                    .where(ConflictRule.id == row_id)
                    .values(active=False)
                )
            else:
                await writer.execute(
                    delete(ConflictRule).where(ConflictRule.id == row_id)
                )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=5)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert isinstance(error, service.ConflictRuleOwnershipBackfillStateError)
    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            service.CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "conflict_rules"
            ],
        )
        assert checkpoint is None
        row = await verify.get(ConflictRule, row_id)
        if row is not None:
            assert row.subject_id is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_alert_domain_switch_is_rechecked_after_rule_lock(
    db_session,
    monkeypatch,
):
    _owner, subject, _checkpoints = await _scope(db_session)
    await conflict_catalog.sync_catalog(db_session)
    custom = _custom(subject_id=subject.id)
    db_session.add(custom)
    await db_session.flush()
    alert = SystemAlert(
        subject_id=subject.id,
        domain=Domain.SUPPLEMENTS.value,
        severity=Severity.WARN.value,
        message="synthetic conflict",
        alert_key=f"conflict:{custom.id}",
        entity_ref="race:1",
    )
    db_session.add(alert)
    await db_session.commit()
    alert_id = alert.id
    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    rules_locked = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        rules_locked.set()
        await asyncio.wait_for(writer_done.wait(), timeout=5)

    monkeypatch.setattr(service, "_after_conflict_alert_rules_locked_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_conflict_rule_ownership_backfill_batch(
                    session, batch_size=1000
                )
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()
            return None

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(rules_locked.wait(), timeout=5)
        async with factory() as writer:
            await writer.execute(
                update(SystemAlert)
                .where(SystemAlert.id == alert_id)
                .values(domain=Domain.LABS.value)
            )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=5)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert isinstance(error, service.ConflictRuleOwnershipBackfillStateError)
    async with factory() as verify:
        assert await verify.get(
            OwnershipBackfillCheckpoint,
            service.CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "conflict_rules"
            ],
        ) is None
