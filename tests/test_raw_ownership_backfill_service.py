"""Focused SQLite contracts for the Stage-3A raw ownership backfill."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.services.raw_ownership_backfill_service import (
    MAX_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE,
    RAW_OWNERSHIP_BACKFILL_PHASE,
    RawOwnershipBackfillDuplicateError,
    RawOwnershipBackfillIdentityError,
    RawOwnershipBackfillMappingError,
    RawOwnershipBackfillStateError,
    RawOwnershipBackfillStatus,
    RawOwnershipBackfillValidationError,
    block_raw_ownership_backfill_for_portability_v1_restore,
    preflight_raw_ownership_backfill,
    run_raw_ownership_backfill_batch,
)
from vitals.services.tenancy_bootstrap import LEGACY_ACCOUNT_DISCRIMINATOR
from vitals.services import raw_ownership_backfill_service as backfill_service

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_FETCHED_AT = datetime(2026, 8, 20, 12, 34, 56, 123456)


async def _scope(session, *, slug: str = "owner"):
    owner = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    connections: dict[IntegrationProvider, IntegrationConnection] = {}
    types = {
        IntegrationProvider.GARMIN: IntegrationConnectionType.ACCOUNT,
        IntegrationProvider.HEVY: IntegrationConnectionType.ACCOUNT,
        IntegrationProvider.OPENROUTER: IntegrationConnectionType.AI_GATEWAY,
        IntegrationProvider.TELEGRAM: IntegrationConnectionType.RECIPIENT,
    }
    for provider, connection_type in types.items():
        connection = IntegrationConnection(
            subject_id=subject.id,
            provider=provider.value,
            connection_type=connection_type.value,
            external_account_discriminator=LEGACY_ACCOUNT_DISCRIMINATOR,
            status=IntegrationConnectionStatus.LEGACY.value,
        )
        session.add(connection)
        connections[provider] = connection
    await session.flush()
    return owner, subject, connections


def _raw(domain: Domain, source: Source, external_id: str, **roots) -> RawPayload:
    return RawPayload(
        domain=domain.value,
        source=source.value,
        external_id=external_id,
        fetched_at=_FETCHED_AT,
        payload={"synthetic": external_id, "nested": {"value": 1}},
        **roots,
    )


async def _checkpoint_count(session) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(OwnershipBackfillCheckpoint)
        )
        or 0
    )


@pytest.mark.asyncio
async def test_preflight_batches_resume_map_roots_and_complete_idempotently(db_session):
    _owner, subject, connections = await _scope(db_session)
    rows = [
        _raw(Domain.GARMIN, Source.GARMIN_API, "g-1"),
        _raw(Domain.WORKOUTS, Source.HEVY_API, "h-1"),
        _raw(Domain.SIGNALS, Source.TELEGRAM, "t-1"),
        _raw(Domain.LABS, Source.LAB_PARSER, "lab-1"),
        _raw(Domain.GENETICS, Source.VCF_IMPORT, "vcf-1"),
        _raw(Domain.BODY_COMPOSITION, Source.MCP, "mcp-1"),
    ]
    db_session.add_all(rows)
    await db_session.flush()

    preflight = await preflight_raw_ownership_backfill(db_session)
    assert preflight.status is RawOwnershipBackfillStatus.NOT_STARTED
    assert preflight.snapshot_rows == 6
    assert preflight.remaining_rows == 6
    assert preflight.scanned_rows == 0
    assert await _checkpoint_count(db_session) == 0
    assert all(row.subject_id is None for row in rows)
    assert "subject_id" not in preflight.to_safe_dict()
    assert "scan_high_watermark_id" not in preflight.to_safe_dict()
    assert "last_scanned_id" not in preflight.to_safe_dict()

    first = await run_raw_ownership_backfill_batch(db_session, batch_size=2)
    second = await run_raw_ownership_backfill_batch(db_session, batch_size=2)
    final = await run_raw_ownership_backfill_batch(db_session, batch_size=2)

    assert first.status is RawOwnershipBackfillStatus.RUNNING
    assert second.status is RawOwnershipBackfillStatus.RUNNING
    assert final.status is RawOwnershipBackfillStatus.COMPLETED
    assert (
        first.batch_scanned_rows,
        second.batch_scanned_rows,
        final.batch_scanned_rows,
    ) == (2, 2, 2)
    assert final.scanned_rows == final.updated_rows == 6
    assert final.snapshot_rows == 6
    assert final.unchanged_rows == final.remaining_rows == 0
    assert final.data_checksum_before == final.data_checksum_after
    assert len(final.data_checksum_after) == 64
    assert len(final.ownership_checksum_after) == 64

    assert (
        rows[0].integration_connection_id
        == connections[IntegrationProvider.GARMIN].id
    )
    assert rows[1].integration_connection_id == connections[IntegrationProvider.HEVY].id
    assert (
        rows[2].integration_connection_id
        == connections[IntegrationProvider.TELEGRAM].id
    )
    assert (
        rows[3].integration_connection_id
        == connections[IntegrationProvider.OPENROUTER].id
    )
    assert rows[4].integration_connection_id is None
    assert rows[5].integration_connection_id is None
    assert all(row.subject_id == subject.id for row in rows)
    assert all(row.actor_user_id is None for row in rows)

    repeated = await run_raw_ownership_backfill_batch(db_session, batch_size=2)
    assert repeated.status is RawOwnershipBackfillStatus.COMPLETED
    assert repeated.batch_scanned_rows == repeated.batch_updated_rows == 0
    assert repeated.ownership_checksum_after == final.ownership_checksum_after
    safe = repeated.to_safe_dict()
    assert "subject_id" not in safe
    assert "scan_high_watermark_id" not in safe
    assert "last_scanned_id" not in safe


@pytest.mark.asyncio
async def test_checksum_chains_match_for_one_batch_and_many_batches(db_session):
    await _scope(db_session)
    db_session.add_all(
        [
            _raw(Domain.GARMIN, Source.GARMIN_API, "g-1"),
            _raw(Domain.WORKOUTS, Source.HEVY_API, "h-1"),
            _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1"),
        ]
    )
    await db_session.commit()

    one = await run_raw_ownership_backfill_batch(db_session, batch_size=3)
    one_digests = (
        one.data_checksum_before,
        one.data_checksum_after,
        one.ownership_checksum_after,
    )
    await db_session.rollback()

    result = None
    for _ in range(3):
        result = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert result is not None
    assert result.status is RawOwnershipBackfillStatus.COMPLETED
    assert (
        result.data_checksum_before,
        result.data_checksum_after,
        result.ownership_checksum_after,
    ) == one_digests


@pytest.mark.asyncio
async def test_max_batch_materializes_at_most_one_json_raw_per_select(db_session):
    await _scope(db_session)
    db_session.add_all(
        [
            _raw(Domain.GENETICS, Source.VCF_IMPORT, f"v-{index}")
            for index in range(3)
        ]
    )
    await db_session.flush()

    assert db_session.bind is not None
    payload_select_limits: list[int | None] = []

    def record_payload_select(
        _connection,
        _cursor,
        statement,
        _parameters,
        context,
        _executemany,
    ):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "raw_payloads.payload" in statement
        ):
            limit_clause = context.compiled.statement._limit_clause
            payload_select_limits.append(
                getattr(limit_clause, "value", None)
            )

    sync_engine = db_session.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", record_payload_select)
    try:
        result = await run_raw_ownership_backfill_batch(
            db_session,
            batch_size=MAX_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE,
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", record_payload_select)

    assert result.status is RawOwnershipBackfillStatus.COMPLETED
    assert result.batch_scanned_rows == 3
    assert len(payload_select_limits) >= 6
    assert set(payload_select_limits) == {1}


@pytest.mark.asyncio
async def test_run_is_flush_only_and_rollback_restores_raws_and_checkpoint(db_session):
    await _scope(db_session)
    raw = _raw(Domain.GARMIN, Source.GARMIN_API, "g-1")
    db_session.add(raw)
    await db_session.commit()
    raw_id = raw.id

    await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert raw.subject_id is not None
    assert await _checkpoint_count(db_session) == 1
    await db_session.rollback()

    refreshed = await db_session.get(RawPayload, raw_id)
    assert refreshed is not None
    assert refreshed.subject_id is None
    assert refreshed.integration_connection_id is None
    assert await _checkpoint_count(db_session) == 0


@pytest.mark.asyncio
async def test_duplicate_nonnull_external_id_fails_but_null_ids_do_not(db_session):
    await _scope(db_session)
    db_session.add_all(
        [
            _raw(Domain.GARMIN, Source.GARMIN_API, "same"),
            _raw(Domain.GARMIN, Source.GARMIN_API, "same"),
        ]
    )
    await db_session.flush()

    with pytest.raises(RawOwnershipBackfillDuplicateError):
        await run_raw_ownership_backfill_batch(db_session, batch_size=10)
    await db_session.rollback()

    await _scope(db_session, slug="owner-null")
    db_session.add_all(
        [
            _raw(Domain.GENETICS, Source.VCF_IMPORT, None),
            _raw(Domain.GENETICS, Source.VCF_IMPORT, None),
        ]
    )
    await db_session.flush()
    result = await run_raw_ownership_backfill_batch(db_session, batch_size=10)
    assert result.status is RawOwnershipBackfillStatus.COMPLETED


@pytest.mark.asyncio
async def test_required_legacy_connection_and_unknown_pair_fail_closed(db_session):
    _owner, subject, connections = await _scope(db_session)
    rotated = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator="rotated-garmin-v2",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    raw = _raw(Domain.GARMIN, Source.GARMIN_API, "g-1")
    db_session.add_all([rotated, raw])
    await db_session.flush()
    result = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert result.status is RawOwnershipBackfillStatus.COMPLETED
    assert raw.integration_connection_id == connections[IntegrationProvider.GARMIN].id
    assert raw.integration_connection_id != rotated.id
    await db_session.rollback()

    _owner, _subject, connections = await _scope(
        db_session,
        slug="missing-legacy-owner",
    )
    connections[
        IntegrationProvider.GARMIN
    ].external_account_discriminator = "rotated-garmin-v2"
    db_session.add(_raw(Domain.GARMIN, Source.GARMIN_API, "g-missing"))
    await db_session.flush()
    with pytest.raises(RawOwnershipBackfillMappingError, match="missing"):
        await preflight_raw_ownership_backfill(db_session)
    await db_session.rollback()

    await _scope(db_session, slug="unknown-owner")
    db_session.add(_raw(Domain.WEIGHT, Source.MANUAL, "unreviewed"))
    await db_session.flush()
    with pytest.raises(RawOwnershipBackfillMappingError, match="unreviewed"):
        await run_raw_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
async def test_retired_legacy_connection_is_inferred_for_historical_raw(db_session):
    _owner, subject, connections = await _scope(db_session)
    legacy = connections[IntegrationProvider.GARMIN]
    legacy.status = IntegrationConnectionStatus.RETIRED.value
    legacy.retired_at = datetime.now(timezone.utc)
    rotated = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator="rotated-garmin-v2",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    raw = _raw(Domain.GARMIN, Source.GARMIN_API, "g-retired-history")
    db_session.add_all([rotated, raw])
    await db_session.flush()

    result = await run_raw_ownership_backfill_batch(db_session, batch_size=1)

    assert result.status is RawOwnershipBackfillStatus.COMPLETED
    assert raw.integration_connection_id == legacy.id
    assert raw.integration_connection_id != rotated.id


@pytest.mark.asyncio
async def test_second_subject_and_partial_roots_fail_closed(db_session):
    owner, subject, _connections = await _scope(db_session)
    other = User(
        username="other",
        normalized_username="other",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(HealthSubject(owner_user_id=other.id, timezone="Asia/Almaty"))
    db_session.add(_raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1"))
    await db_session.flush()
    with pytest.raises(RawOwnershipBackfillIdentityError):
        await preflight_raw_ownership_backfill(db_session)
    await db_session.rollback()

    owner, subject, _connections = await _scope(db_session, slug="partial-owner")
    db_session.add(
        _raw(
            Domain.GENETICS,
            Source.VCF_IMPORT,
            "partial",
            actor_user_id=owner.id,
        )
    )
    await db_session.flush()
    with pytest.raises(RawOwnershipBackfillStateError, match="partial"):
        await run_raw_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
async def test_unowned_row_above_resumed_high_watermark_fails(db_session):
    await _scope(db_session)
    first = _raw(Domain.GARMIN, Source.GARMIN_API, "g-1")
    second = _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1")
    db_session.add_all([first, second])
    await db_session.commit()

    running = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert running.status is RawOwnershipBackfillStatus.RUNNING
    await db_session.commit()

    appended = _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-after")
    db_session.add(appended)
    await db_session.commit()
    with pytest.raises(RawOwnershipBackfillStateError, match="high-water"):
        await run_raw_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["connectionless", "provider", "parser"])
async def test_actorless_row_above_high_watermark_fails_live_contract(
    db_session,
    shape,
):
    _owner, subject, connections = await _scope(db_session)
    db_session.add(_raw(Domain.GENETICS, Source.VCF_IMPORT, "v-before"))
    completed = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert completed.status is RawOwnershipBackfillStatus.COMPLETED
    await db_session.commit()

    if shape == "provider":
        appended = _raw(
            Domain.GARMIN,
            Source.GARMIN_API,
            "g-subject-connection-after",
            subject_id=subject.id,
            integration_connection_id=connections[IntegrationProvider.GARMIN].id,
        )
    elif shape == "parser":
        appended = _raw(
            Domain.LABS,
            Source.LAB_PARSER,
            "lab-subject-connection-after",
            subject_id=subject.id,
            integration_connection_id=(
                connections[IntegrationProvider.OPENROUTER].id
            ),
        )
    else:
        appended = _raw(
            Domain.GENETICS,
            Source.VCF_IMPORT,
            "v-subject-only-after",
            subject_id=subject.id,
        )
    db_session.add(appended)
    await db_session.commit()

    with pytest.raises(RawOwnershipBackfillStateError, match="live ownership"):
        await run_raw_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
async def test_resumed_run_does_not_repeat_full_snapshot_scan(
    db_session, monkeypatch
):
    await _scope(db_session)
    db_session.add_all(
        [
            _raw(Domain.GARMIN, Source.GARMIN_API, "g-1"),
            _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1"),
        ]
    )
    await db_session.flush()
    first = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert first.status is RawOwnershipBackfillStatus.RUNNING

    original_full_scan = backfill_service._scan_and_validate_snapshot

    async def _unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("resumed runs must not rescan the full raw snapshot")

    monkeypatch.setattr(
        backfill_service, "_scan_and_validate_snapshot", _unexpected_full_scan
    )
    completed = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert completed.status is RawOwnershipBackfillStatus.COMPLETED
    monkeypatch.setattr(
        backfill_service,
        "_scan_and_validate_snapshot",
        original_full_scan,
    )
    repeated = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert repeated.status is RawOwnershipBackfillStatus.COMPLETED
    assert repeated.batch_scanned_rows == 0


@pytest.mark.asyncio
async def test_completed_status_and_apply_reject_current_ownership_drift(db_session):
    owner, _subject, _connections = await _scope(db_session)
    raw = _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1")
    db_session.add(raw)
    await db_session.flush()
    completed = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert completed.status is RawOwnershipBackfillStatus.COMPLETED
    await db_session.commit()

    await db_session.execute(
        update(RawPayload)
        .where(RawPayload.id == raw.id)
        .values(actor_user_id=owner.id)
    )
    await db_session.commit()

    with pytest.raises(RawOwnershipBackfillStateError, match="checksum"):
        await preflight_raw_ownership_backfill(db_session)
    with pytest.raises(RawOwnershipBackfillStateError, match="checksum"):
        await run_raw_ownership_backfill_batch(db_session, batch_size=1)


@pytest.mark.asyncio
async def test_completed_data_checksum_remains_point_in_time_evidence(db_session):
    await _scope(db_session)
    raw = _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1")
    db_session.add(raw)
    await db_session.flush()
    completed = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert completed.status is RawOwnershipBackfillStatus.COMPLETED
    original_data_checksum = completed.data_checksum_after
    await db_session.commit()

    await db_session.execute(
        update(RawPayload)
        .where(RawPayload.id == raw.id)
        .values(payload={"synthetic": "legitimate-post-completion-refresh"})
    )
    await db_session.commit()

    status = await preflight_raw_ownership_backfill(db_session)
    repeated = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert status.status is RawOwnershipBackfillStatus.COMPLETED
    assert repeated.status is RawOwnershipBackfillStatus.COMPLETED
    assert repeated.data_checksum_after == original_data_checksum


@pytest.mark.asyncio
async def test_checkpoint_prefix_count_drift_fails_before_resume(db_session):
    await _scope(db_session)
    rows = [
        _raw(Domain.GARMIN, Source.GARMIN_API, "g-1"),
        _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1"),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    first_id = rows[0].id
    await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    await db_session.commit()

    first = await db_session.get(RawPayload, first_id)
    assert first is not None
    await db_session.delete(first)
    await db_session.commit()
    with pytest.raises(RawOwnershipBackfillStateError, match="prefix count"):
        await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    with pytest.raises(RawOwnershipBackfillStateError, match="prefix count"):
        await preflight_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_checkpoint_unscanned_tail_deletion_fails_before_completion(db_session):
    await _scope(db_session)
    rows = [
        _raw(Domain.GARMIN, Source.GARMIN_API, "g-1"),
        _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1"),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    tail_id = rows[1].id

    first = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert first.status is RawOwnershipBackfillStatus.RUNNING
    assert first.snapshot_rows == 2
    await db_session.commit()

    tail = await db_session.get(RawPayload, tail_id)
    assert tail is not None
    await db_session.delete(tail)
    await db_session.commit()

    with pytest.raises(RawOwnershipBackfillStateError, match="snapshot count"):
        await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    with pytest.raises(RawOwnershipBackfillStateError, match="snapshot count"):
        await preflight_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["data", "ownership"])
async def test_finalization_rejects_processed_prefix_checksum_drift(
    db_session,
    drift,
):
    owner, _subject, _connections = await _scope(db_session)
    rows = [
        _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1"),
        _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-2"),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    first_id = rows[0].id

    first = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert first.status is RawOwnershipBackfillStatus.RUNNING
    await db_session.commit()

    if drift == "data":
        await db_session.execute(
            update(RawPayload)
            .where(RawPayload.id == first_id)
            .values(payload={"synthetic": "changed-between-batches"})
        )
        error_match = "raw data changed"
    else:
        await db_session.execute(
            update(RawPayload)
            .where(RawPayload.id == first_id)
            .values(actor_user_id=owner.id)
        )
        error_match = "raw ownership changed"
    await db_session.commit()

    with pytest.raises(RawOwnershipBackfillStateError, match=error_match):
        await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    await db_session.rollback()

    checkpoint = await db_session.get(
        OwnershipBackfillCheckpoint,
        RAW_OWNERSHIP_BACKFILL_PHASE,
    )
    assert checkpoint is not None
    assert checkpoint.status == RawOwnershipBackfillStatus.RUNNING.value
    assert checkpoint.scanned_rows == 1


@pytest.mark.asyncio
async def test_historical_parser_actor_and_file_roots_must_be_paired(db_session):
    owner, subject, connections = await _scope(db_session)
    db_session.add(
        _raw(
            Domain.LABS,
            Source.LAB_PARSER,
            "legacy-lab",
            subject_id=subject.id,
            actor_user_id=owner.id,
            integration_connection_id=connections[IntegrationProvider.OPENROUTER].id,
        )
    )
    await db_session.flush()
    with pytest.raises(RawOwnershipBackfillMappingError, match="partial historical"):
        await preflight_raw_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_preflight_refreshes_dirty_identity_state_without_flushing_it(db_session):
    owner, _subject, _connections = await _scope(db_session)
    owner_id = owner.id
    await db_session.commit()
    await db_session.execute(
        update(User)
        .where(User.id == owner_id)
        .values(status=UserStatus.SUSPENDED.value)
    )
    await db_session.commit()

    owner.status = UserStatus.ACTIVE.value
    assert owner in db_session.dirty
    with pytest.raises(RawOwnershipBackfillIdentityError):
        await preflight_raw_ownership_backfill(db_session)
    assert owner.status == UserStatus.ACTIVE.value
    assert owner in db_session.dirty
    assert db_session.is_modified(owner, include_collections=False)
    await db_session.rollback()
    persisted = await db_session.scalar(select(User.status).where(User.id == owner_id))
    assert persisted == UserStatus.SUSPENDED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_concurrent_batches_serialize_and_scan_each_row_once(db_session):
    _owner, subject, _connections = await _scope(db_session)
    rows = [
        _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1"),
        _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-2"),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    raw_ids = [row.id for row in rows]

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    both_ready = asyncio.Event()
    arrivals = 0

    async def worker():
        nonlocal arrivals
        async with factory() as session:
            arrivals += 1
            if arrivals == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=5)
            result = await run_raw_ownership_backfill_batch(session, batch_size=1)
            await session.commit()
            return result

    results = await asyncio.wait_for(
        asyncio.gather(worker(), worker()),
        timeout=10,
    )
    assert {result.status for result in results} == {
        RawOwnershipBackfillStatus.RUNNING,
        RawOwnershipBackfillStatus.COMPLETED,
    }
    assert [result.batch_scanned_rows for result in results] == [1, 1]
    assert [result.batch_updated_rows for result in results] == [1, 1]

    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            RAW_OWNERSHIP_BACKFILL_PHASE,
        )
        persisted = list(
            await verify.scalars(
                select(RawPayload)
                .where(RawPayload.id.in_(raw_ids))
                .order_by(RawPayload.id)
            )
        )

    assert checkpoint is not None
    assert checkpoint.status == RawOwnershipBackfillStatus.COMPLETED.value
    assert checkpoint.last_scanned_id == checkpoint.scan_high_watermark_id
    assert checkpoint.scanned_rows == checkpoint.updated_rows == 2
    assert checkpoint.unchanged_rows == 0
    assert len(persisted) == 2
    assert all(row.subject_id == subject.id for row in persisted)
    assert all(row.integration_connection_id is None for row in persisted)

    data_digest = _EMPTY_SHA256
    ownership_digest = _EMPTY_SHA256
    for row in persisted:
        data_digest = backfill_service._extend_checksum(
            data_digest,
            backfill_service._data_envelope(row),
        )
        ownership_digest = backfill_service._extend_checksum(
            ownership_digest,
            backfill_service._ownership_envelope(row),
        )
    assert checkpoint.data_checksum_before == data_digest
    assert checkpoint.data_checksum_after == data_digest
    assert checkpoint.ownership_checksum_after == ownership_digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "batch_size",
    [False, 0, -1, MAX_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE + 1, "1"],
)
async def test_batch_size_validation_is_typed_and_does_not_mutate(
    db_session, batch_size
):
    await _scope(db_session)
    with pytest.raises(RawOwnershipBackfillValidationError):
        await run_raw_ownership_backfill_batch(db_session, batch_size=batch_size)
    assert await _checkpoint_count(db_session) == 0


@pytest.mark.asyncio
async def test_v1_restore_blocks_completed_checkpoint_and_rollback_restores_it(
    db_session,
):
    _owner, subject, _connections = await _scope(db_session)
    db_session.add(_raw(Domain.GENETICS, Source.VCF_IMPORT, "v-1"))
    completed = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert completed.status is RawOwnershipBackfillStatus.COMPLETED
    other = OwnershipBackfillCheckpoint(
        phase_key="stage3.other.v1",
        subject_id=subject.id,
        status="completed",
        scan_high_watermark_id=0,
        snapshot_rows=0,
        last_scanned_id=0,
        scanned_rows=0,
        updated_rows=0,
        unchanged_rows=0,
        data_checksum_before=_EMPTY_SHA256,
        data_checksum_after=_EMPTY_SHA256,
        ownership_checksum_after=_EMPTY_SHA256,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(other)
    await db_session.commit()
    other_phase = other.phase_key

    blocked = await block_raw_ownership_backfill_for_portability_v1_restore(
        db_session,
        scan_high_watermark_id=17,
        snapshot_rows=2,
    )
    assert blocked.status is RawOwnershipBackfillStatus.RESTORE_BLOCKED
    assert blocked.snapshot_rows == 2
    checkpoint = await db_session.get(
        OwnershipBackfillCheckpoint, RAW_OWNERSHIP_BACKFILL_PHASE
    )
    assert checkpoint is not None
    assert checkpoint.status == "restore_blocked"
    assert checkpoint.scan_high_watermark_id == 17
    assert checkpoint.snapshot_rows == 2
    assert checkpoint.last_scanned_id == 0
    assert checkpoint.scanned_rows == checkpoint.updated_rows == 0
    assert checkpoint.unchanged_rows == 0
    assert checkpoint.completed_at is None
    assert await db_session.get(OwnershipBackfillCheckpoint, other_phase) is other

    await db_session.rollback()
    restored = await db_session.get(
        OwnershipBackfillCheckpoint, RAW_OWNERSHIP_BACKFILL_PHASE
    )
    assert restored is not None
    assert restored.status == "completed"
    assert await db_session.get(OwnershipBackfillCheckpoint, other_phase) is not None


@pytest.mark.asyncio
async def test_v1_restore_api_completes_empty_snapshot_and_rejects_invalid_bounds(
    db_session,
):
    await _scope(db_session)
    invalid_bounds = [
        (-1, 0),
        (False, 0),
        ("3", 0),
        (2**63, 0),
        (1, -1),
        (1, False),
        (1, "1"),
        (1, 2),
        (2**63 - 1, 2**63),
    ]
    for high_watermark, snapshot_rows in invalid_bounds:
        with pytest.raises(RawOwnershipBackfillValidationError):
            await block_raw_ownership_backfill_for_portability_v1_restore(
                db_session,
                scan_high_watermark_id=high_watermark,
                snapshot_rows=snapshot_rows,
            )
    assert await _checkpoint_count(db_session) == 0

    result = await block_raw_ownership_backfill_for_portability_v1_restore(
        db_session,
        scan_high_watermark_id=0,
        snapshot_rows=0,
    )
    assert result.status is RawOwnershipBackfillStatus.COMPLETED
    assert result.snapshot_rows == 0
    checkpoint = await db_session.get(
        OwnershipBackfillCheckpoint, RAW_OWNERSHIP_BACKFILL_PHASE
    )
    assert checkpoint is not None
    assert checkpoint.status == RawOwnershipBackfillStatus.COMPLETED.value
    assert checkpoint.scan_high_watermark_id == 0
    assert checkpoint.snapshot_rows == 0
    assert checkpoint.completed_at is not None
    assert checkpoint.data_checksum_before == _EMPTY_SHA256
    assert checkpoint.data_checksum_after == _EMPTY_SHA256
    assert checkpoint.ownership_checksum_after == _EMPTY_SHA256


@pytest.mark.asyncio
async def test_v1_same_id_replacement_stays_blocked_from_ordinary_apply(db_session):
    _owner, subject, _connections = await _scope(db_session)
    original = _raw(Domain.GENETICS, Source.VCF_IMPORT, "v-before-restore")
    db_session.add(original)
    completed = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert completed.status is RawOwnershipBackfillStatus.COMPLETED
    await db_session.commit()
    raw_id = original.id
    db_session.expunge(original)

    await block_raw_ownership_backfill_for_portability_v1_restore(
        db_session,
        scan_high_watermark_id=raw_id,
        snapshot_rows=1,
    )
    await db_session.execute(delete(RawPayload).where(RawPayload.id == raw_id))
    replacement = _raw(
        Domain.GARMIN,
        Source.GARMIN_API,
        "g-after-restore",
        subject_id=subject.id,
    )
    replacement.id = raw_id
    db_session.add(replacement)
    await db_session.commit()

    status = await preflight_raw_ownership_backfill(db_session)
    assert status.status is RawOwnershipBackfillStatus.RESTORE_BLOCKED
    assert status.snapshot_rows == status.remaining_rows == 1
    assert status.to_safe_dict()["status"] == "restore_blocked"
    with pytest.raises(RawOwnershipBackfillStateError, match="trusted recovery"):
        await run_raw_ownership_backfill_batch(db_session, batch_size=1)

    persisted = await db_session.get(RawPayload, raw_id)
    assert persisted is not None
    assert persisted.integration_connection_id is None
