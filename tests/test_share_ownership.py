"""Commercial SharedReport ownership, public capability, and race contracts."""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.share import SharedReport
from vitals.models.weight import WeightLog
from vitals.services import identity_service, share_service
from vitals.services.shared_report_ownership_backfill_service import (
    SharedReportHistoricalBridgeState,
    SharedReportOwnershipBackfillStateError,
)
from vitals.utils.timeutils import now_local
from web.config import get_web_config


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader does when the ownership backfill has not reached a row yet, which is
# a state the application itself can no longer create. The schema says so, so
# this module asks for the one that stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


START = date(2026, 3, 1)
END = date(2026, 3, 30)


async def _prepared(session: AsyncSession) -> share_service.PreparedShareOwner:
    return await share_service.prepare_legacy_owner(
        session,
        actor_username=get_web_config().auth_username,
    )


async def _create(
    session: AsyncSession,
    prepared: share_service.PreparedShareOwner,
    *,
    title: str = "Scoped report", legacy_owner_roots,
) -> tuple[SharedReport, str]:
    if await session.scalar(select(WeightLog.id).limit(1)) is None:
        session.add(
            WeightLog(subject_id=legacy_owner_roots.subject_id,
                date=START,
                domain=Domain.WEIGHT.value,
                source="manual",
                weight_kg=88.0,
            )
        )
        await session.flush()
    return await share_service.create_report(
        session,
        title=title,
        domains=[Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        prepared_owner=prepared,
    )


def _bare_report(token: str, **roots) -> SharedReport:
    return SharedReport(
        token=token,
        password_hash="$2b$12$" + "x" * 53,
        title="Synthetic",
        domains=[Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        snapshot={"version": 1, "blocks": {"weight": {"last": {"kg": 88}}}},
        expires_at=now_local() + timedelta(days=7),
        **roots,
    )


def _bridge(
    monkeypatch,
    *,
    cursor: int,
    high_watermark: int,
    completed: bool = False,
) -> None:
    from vitals.services import shared_report_ownership_backfill_service

    async def load(session, *, subject_id):
        del session, subject_id
        return SharedReportHistoricalBridgeState(
            processed_high_watermark_id=cursor,
            snapshot_high_watermark_id=high_watermark,
            completed=completed,
        )

    monkeypatch.setattr(
        shared_report_ownership_backfill_service,
        "shared_report_historical_bridge_state",
        load,
    )


@pytest.mark.asyncio
async def test_create_stamps_subject_creator_and_keeps_ids_out_of_snapshot(
    db_session,
    legacy_owner_roots,
):
    prepared = await _prepared(db_session)
    row, _ = await _create(db_session, prepared, legacy_owner_roots=legacy_owner_roots)

    assert (row.subject_id, row.created_by_user_id, row.revoked_by_user_id) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        None,
    )
    frozen = json.dumps(row.snapshot, sort_keys=True)
    assert str(legacy_owner_roots.subject_id) not in frozen
    assert str(legacy_owner_roots.user_id) not in frozen


@pytest.mark.asyncio
async def test_owner_reads_include_only_exact_and_fully_null_history(
    db_session,
    legacy_owner_roots,
):
    prepared = await _prepared(db_session)
    exact, _ = await _create(db_session, prepared, title="Exact", legacy_owner_roots=legacy_owner_roots)
    legacy = _bare_report("fully-null-legacy")
    historical_revoked = _bare_report(
        "historical-revoked-without-actor",
        subject_id=legacy_owner_roots.subject_id,
        revoked_at=now_local(),
    )
    db_session.add_all([legacy, historical_revoked])
    await db_session.flush()

    rows = await share_service.list_reports(db_session, prepared_owner=prepared)
    assert {row.id for row in rows} == {exact.id, legacy.id, historical_revoked.id}
    assert (
        await share_service.get_report(
            db_session,
            legacy.id,
            prepared_owner=prepared,
        )
    ) is legacy


@pytest.mark.asyncio
async def test_running_bridge_exposes_migrated_prefix_and_unprocessed_legacy(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    migrated = _bare_report(
        "running-migrated-prefix",
        subject_id=legacy_owner_roots.subject_id,
    )
    unprocessed = _bare_report("running-unprocessed-legacy")
    db_session.add_all([migrated, unprocessed])
    await db_session.flush()
    _bridge(
        monkeypatch,
        cursor=migrated.id,
        high_watermark=unprocessed.id,
    )
    prepared = await _prepared(db_session)

    rows = await share_service.list_reports(db_session, prepared_owner=prepared)
    assert {row.id for row in rows} == {migrated.id, unprocessed.id}
    assert await share_service.resolve_public(db_session, migrated.token) is migrated
    assert await share_service.resolve_public(db_session, unprocessed.token) is unprocessed
    assert await share_service.register_open(db_session, unprocessed.token) is unprocessed
    assert unprocessed.opened_count == 1


@pytest.mark.asyncio
async def test_running_bridge_rejects_fully_null_processed_or_above_hwm(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    processed_but_null = _bare_report("running-null-at-cursor")
    db_session.add(processed_but_null)
    await db_session.flush()
    _bridge(
        monkeypatch,
        cursor=processed_but_null.id,
        high_watermark=processed_but_null.id,
    )
    prepared = await _prepared(db_session)

    with pytest.raises(share_service.ShareOwnershipError, match="outside"):
        await share_service.list_reports(db_session, prepared_owner=prepared)
    assert await share_service.resolve_public(
        db_session, processed_but_null.token
    ) is None

    await db_session.delete(processed_but_null)
    await db_session.flush()
    historical = _bare_report(
        "running-hwm-anchor",
        subject_id=legacy_owner_roots.subject_id,
    )
    forged_tail = _bare_report("running-forged-null-tail")
    db_session.add_all([historical, forged_tail])
    await db_session.flush()
    _bridge(
        monkeypatch,
        cursor=historical.id,
        high_watermark=historical.id,
    )

    with pytest.raises(share_service.ShareOwnershipError, match="outside"):
        await share_service.get_report(
            db_session,
            forged_tail.id,
            prepared_owner=prepared,
        )
    assert await share_service.resolve_public(db_session, forged_tail.token) is None
    assert await share_service.register_open(db_session, forged_tail.token) is None
    with pytest.raises(share_service.ShareOwnershipError, match="outside"):
        await share_service.revoke(
            db_session,
            forged_tail.id,
            prepared_owner=prepared,
        )
    with pytest.raises(share_service.ShareOwnershipError, match="outside"):
        await share_service.delete_report(
            db_session,
            forged_tail.id,
            prepared_owner=prepared,
        )


@pytest.mark.asyncio
async def test_running_unprocessed_revoke_and_delete_are_authenticated_adoptions(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    to_revoke = _bare_report("running-unprocessed-revoke")
    to_delete = _bare_report("running-unprocessed-delete")
    db_session.add_all([to_revoke, to_delete])
    await db_session.flush()
    _bridge(
        monkeypatch,
        cursor=0,
        high_watermark=to_delete.id,
    )
    prepared = await _prepared(db_session)

    assert await share_service.revoke(
        db_session,
        to_revoke.id,
        prepared_owner=prepared,
    )
    assert to_revoke.subject_id == legacy_owner_roots.subject_id
    assert to_revoke.created_by_user_id is None
    assert to_revoke.revoked_by_user_id == legacy_owner_roots.user_id
    assert await share_service.get_report(
        db_session,
        to_revoke.id,
        prepared_owner=prepared,
    ) is to_revoke
    assert await share_service.delete_report(
        db_session,
        to_delete.id,
        prepared_owner=prepared,
    )


@pytest.mark.asyncio
async def test_completed_bridge_allows_historical_and_requires_strict_live_creator(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    historical = _bare_report(
        "completed-historical",
        subject_id=legacy_owner_roots.subject_id,
    )
    live = _bare_report(
        "completed-strict-live",
        subject_id=legacy_owner_roots.subject_id,
        created_by_user_id=legacy_owner_roots.user_id,
    )
    db_session.add_all([historical, live])
    await db_session.flush()
    _bridge(
        monkeypatch,
        cursor=historical.id,
        high_watermark=historical.id,
        completed=True,
    )
    prepared = await _prepared(db_session)

    rows = await share_service.list_reports(db_session, prepared_owner=prepared)
    assert {row.id for row in rows} == {historical.id, live.id}
    assert await share_service.resolve_public(db_session, historical.token) is historical
    assert await share_service.register_open(db_session, live.token) is live

    live.created_by_user_id = None
    await db_session.flush()
    with pytest.raises(share_service.ShareOwnershipError, match="creator"):
        await share_service.list_reports(db_session, prepared_owner=prepared)
    assert await share_service.resolve_public(db_session, live.token) is None


@pytest.mark.asyncio
async def test_checkpoint_errors_fail_owner_and_purge_and_are_public_not_found(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    from vitals.services import shared_report_ownership_backfill_service

    report = _bare_report(
        "malformed-checkpoint",
        subject_id=legacy_owner_roots.subject_id,
        created_by_user_id=legacy_owner_roots.user_id,
    )
    report.expires_at = now_local() - timedelta(minutes=1)
    db_session.add(report)
    await db_session.flush()

    async def reject(session, *, subject_id):
        del session, subject_id
        raise SharedReportOwnershipBackfillStateError(
            "malformed or wrong-subject checkpoint"
        )

    monkeypatch.setattr(
        shared_report_ownership_backfill_service,
        "shared_report_historical_bridge_state",
        reject,
    )
    prepared = await _prepared(db_session)
    with pytest.raises(share_service.ShareOwnershipError, match="checkpoint"):
        await share_service.list_reports(db_session, prepared_owner=prepared)
    assert await share_service.resolve_public(db_session, report.token) is None
    assert await share_service.register_open(db_session, report.token) is None
    with pytest.raises(share_service.ShareOwnershipError, match="checkpoint"):
        await share_service.purge_expired(db_session)
    assert report.snapshot is not None


@pytest.mark.asyncio
async def test_running_bridge_purges_migrated_and_unprocessed_snapshots(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    migrated = _bare_report(
        "running-expired-migrated",
        subject_id=legacy_owner_roots.subject_id,
    )
    unprocessed = _bare_report("running-expired-unprocessed")
    migrated.expires_at = unprocessed.expires_at = (
        now_local() - timedelta(minutes=1)
    )
    db_session.add_all([migrated, unprocessed])
    await db_session.flush()
    _bridge(
        monkeypatch,
        cursor=migrated.id,
        high_watermark=unprocessed.id,
    )

    assert await share_service.purge_expired(db_session) == 2
    assert migrated.snapshot is None
    assert unprocessed.snapshot is None
    assert unprocessed.subject_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("root", ["created", "revoked"])
async def test_s_null_partial_actor_roots_fail_closed(
    db_session,
    legacy_owner_roots,
    root,
):
    prepared = await _prepared(db_session)
    kwargs = {
        f"{root}_by_user_id": legacy_owner_roots.user_id,
    }
    db_session.add(_bare_report(f"partial-{root}", **kwargs))
    await db_session.flush()

    with pytest.raises(share_service.ShareOwnershipError, match="partial"):
        await share_service.list_reports(db_session, prepared_owner=prepared)


@pytest.mark.asyncio
async def test_non_owner_actor_on_selected_subject_fails_closed(
    db_session,
    legacy_owner_roots,
):
    foreign = User(
        username="share-foreign-actor",
        normalized_username="share-foreign-actor",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(foreign)
    await db_session.flush()
    prepared = await _prepared(db_session)
    db_session.add(
        _bare_report(
            "foreign-actor",
            subject_id=legacy_owner_roots.subject_id,
            created_by_user_id=foreign.id,
        )
    )
    await db_session.flush()

    with pytest.raises(share_service.ShareOwnershipError, match="foreign"):
        await share_service.list_reports(db_session, prepared_owner=prepared)


@pytest.mark.asyncio
async def test_human_revoke_adopts_only_subject_and_preserves_unknown_creator(
    db_session,
    legacy_owner_roots,
):
    legacy = _bare_report("legacy-revoke")
    db_session.add(legacy)
    await db_session.flush()
    prepared = await _prepared(db_session)

    assert await share_service.revoke(
        db_session,
        legacy.id,
        prepared_owner=prepared,
    )
    assert legacy.subject_id == legacy_owner_roots.subject_id
    assert legacy.created_by_user_id is None
    assert legacy.revoked_by_user_id == legacy_owner_roots.user_id
    assert legacy.snapshot is None


@pytest.mark.asyncio
async def test_second_subject_fails_before_global_snapshot_reader(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    second_user = User(
        username="share-second-owner",
        normalized_username="share-second-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second_user)
    await db_session.flush()
    db_session.add(HealthSubject(owner_user_id=second_user.id, timezone="Asia/Almaty"))
    await db_session.commit()

    from vitals.services import digest_service
    from vitals.services.share_service import SharePreparedOwnerError
    from web.routers import share as share_router

    async def forbidden(*args, **kwargs):
        del args, kwargs
        pytest.fail("global snapshot reader ran after exact-one proof failed")

    monkeypatch.setattr(digest_service, "assemble_context", forbidden)
    # The refusal moved a layer down and did not go away. The legacy resolver
    # used to reject any installation holding a second subject, which took every
    # page down with it once the professional features made a second subject the
    # point; it now selects the actor's *own* record. Share still refuses,
    # because its own compatibility bridge is the one that genuinely cannot
    # decide whose snapshot an unowned report describes.
    with pytest.raises(SharePreparedOwnerError, match="exactly one"):
        await share_router.create(
            request=SimpleNamespace(
                state=SimpleNamespace(enabled_modules={"weight": True})
            ),
            title="Must not compose",
            preset=None,
            domains=[Domain.WEIGHT.value],
            period="30",
            period_start=None,
            period_end=None,
            expires_days=7,
            labs_flagged_only=False,
            note=None,
            db=db_session,
            username=get_web_config().auth_username,
            _rl=None,
        )


@pytest.mark.asyncio
async def test_prepared_owner_rejects_reuse_and_fingerprint_tampering(
    db_session,
    legacy_owner_roots,
):
    prepared = await _prepared(db_session)
    await db_session.commit()
    with pytest.raises(share_service.SharePreparedOwnerError, match="transaction"):
        await share_service.list_reports(db_session, prepared_owner=prepared)

    prepared = await _prepared(db_session)
    object.__setattr__(prepared, "_owner_user_id", legacy_owner_roots.subject_id)
    with pytest.raises(share_service.SharePreparedOwnerError, match="identity"):
        await share_service.list_reports(db_session, prepared_owner=prepared)


@pytest.mark.asyncio
async def test_prepared_owner_rejects_constructor_cross_session_savepoint_and_malformed(
    db_session,
    legacy_owner_roots,
):
    with pytest.raises(share_service.SharePreparedOwnerError, match="issued only"):
        share_service.PreparedShareOwner()

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    prepared = await _prepared(db_session)
    async with factory() as other:
        with pytest.raises(share_service.SharePreparedOwnerError, match="session"):
            await share_service.list_reports(other, prepared_owner=prepared)

    await db_session.rollback()
    savepoint = await db_session.begin_nested()
    prepared = await _prepared(db_session)
    await savepoint.rollback()
    with pytest.raises(share_service.SharePreparedOwnerError, match="savepoint"):
        await share_service.list_reports(db_session, prepared_owner=prepared)

    malformed = object.__new__(share_service.PreparedShareOwner)
    with pytest.raises(share_service.SharePreparedOwnerError, match="valid issued"):
        await share_service.list_reports(db_session, prepared_owner=malformed)


@pytest.mark.asyncio
async def test_public_corrupt_roots_are_indistinguishable_not_found(
    legacy_owner_roots,
    client,
    db_session,
):
    corrupt = _bare_report(
        "public-partial-roots",
        created_by_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(corrupt)
    await db_session.commit()

    response = await client.get(f"/r/{corrupt.token}")
    missing = await client.get("/r/does-not-exist")
    assert response.status_code == missing.status_code == 404
    assert response.text == missing.text


@pytest.mark.asyncio
async def test_revocation_actor_without_timestamp_fails_closed(
    legacy_owner_roots,
    client,
    db_session,
):
    corrupt = _bare_report(
        "public-revoker-without-timestamp",
        subject_id=legacy_owner_roots.subject_id,
        created_by_user_id=legacy_owner_roots.user_id,
        revoked_by_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(corrupt)
    await db_session.commit()

    response = await client.get(f"/r/{corrupt.token}")
    missing = await client.get("/r/does-not-exist")
    assert response.status_code == missing.status_code == 404
    assert response.text == missing.text

    prepared = await _prepared(db_session)
    with pytest.raises(share_service.ShareOwnershipError):
        await share_service.get_report(
            db_session,
            corrupt.id,
            prepared_owner=prepared,
        )


@pytest.mark.asyncio
async def test_purge_changes_snapshot_only_and_rejects_partial_roots(
    db_session,
    legacy_owner_roots,
):
    prepared = await _prepared(db_session)
    exact, _ = await _create(db_session, prepared, legacy_owner_roots=legacy_owner_roots)
    exact.expires_at = now_local() - timedelta(minutes=1)
    await db_session.commit()
    creator = exact.created_by_user_id

    assert await share_service.purge_expired(db_session) == 1
    assert exact.snapshot is None
    assert exact.subject_id == legacy_owner_roots.subject_id
    assert exact.created_by_user_id == creator
    assert exact.revoked_by_user_id is None

    corrupt = _bare_report(
        "expired-partial",
        created_by_user_id=legacy_owner_roots.user_id,
    )
    corrupt.expires_at = now_local() - timedelta(minutes=1)
    db_session.add(corrupt)
    await db_session.flush()
    with pytest.raises(share_service.ShareOwnershipError, match="partial"):
        await share_service.purge_expired(db_session)
    assert corrupt.snapshot is not None


@pytest.mark.asyncio
async def test_purge_expired_snapshot_for_suspended_owner(
    db_session,
    legacy_owner_roots,
):
    prepared = await _prepared(db_session)
    report, _ = await _create(db_session, prepared, legacy_owner_roots=legacy_owner_roots)
    report.expires_at = now_local() - timedelta(minutes=1)
    owner = await db_session.get(User, legacy_owner_roots.user_id)
    assert owner is not None
    owner.status = UserStatus.SUSPENDED.value
    await db_session.commit()

    assert await share_service.resolve_public(db_session, report.token) is None
    await db_session.rollback()
    assert await share_service.purge_expired(db_session) == 1
    assert report.snapshot is None
    assert report.subject_id == legacy_owner_roots.subject_id
    assert report.created_by_user_id == legacy_owner_roots.user_id
    assert report.revoked_by_user_id is None


@pytest.mark.asyncio
async def test_purge_fully_null_expired_snapshot_for_suspended_owner(
    db_session,
    legacy_owner_roots,
):
    report = _bare_report("expired-fully-null-suspended-owner")
    report.expires_at = now_local() - timedelta(minutes=1)
    db_session.add(report)
    owner = await db_session.get(User, legacy_owner_roots.user_id)
    assert owner is not None
    owner.status = UserStatus.SUSPENDED.value
    await db_session.commit()

    assert await share_service.purge_expired(db_session) == 1
    assert report.snapshot is None
    assert report.subject_id is None
    assert report.created_by_user_id is None
    assert report.revoked_by_user_id is None


def test_production_owner_lifecycle_has_no_bare_session_get() -> None:
    service_source = inspect.getsource(share_service)
    assert "session.get(SharedReport" not in service_source
    for function in (
        share_service.build_snapshot,
        share_service.earliest_data_date,
        share_service.create_report,
        share_service.list_reports,
        share_service.get_report,
        share_service.revoke,
        share_service.delete_report,
    ):
        assert "_owner_or_zero_subject_legacy" in inspect.getsource(function)


@pytest.mark.integration
async def test_postgres_snapshot_scope_blocks_subject_creation(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    await db_session.commit()
    holder = factory()
    prepared = await _prepared(holder)
    holder.add(
        WeightLog(subject_id=legacy_owner_roots.subject_id,
            date=START,
            domain=Domain.WEIGHT.value,
            source="manual",
            weight_kg=88.0,
        )
    )
    await holder.flush()
    await share_service.create_report(
        holder,
        title="Governance holder",
        domains=[Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        prepared_owner=prepared,
    )

    attempted = asyncio.Event()

    async def create_subject() -> None:
        async with factory() as session:
            attempted.set()
            await identity_service.acquire_identity_governance_lock(session)
            user = User(
                username="share-racing-owner",
                normalized_username="share-racing-owner",
                password_hash="$synthetic-test-hash",
                status=UserStatus.ACTIVE.value,
            )
            session.add(user)
            await session.flush()
            session.add(HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty"))
            await session.commit()

    task = asyncio.create_task(create_subject())
    await attempted.wait()
    await asyncio.sleep(0.2)
    assert not task.done(), "subject creation must wait for snapshot governance"
    await holder.rollback()
    await holder.close()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.integration
async def test_postgres_concurrent_public_opens_do_not_lose_counts(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    prepared = await _prepared(db_session)
    row, _ = await _create(db_session, prepared, legacy_owner_roots=legacy_owner_roots)
    await db_session.commit()
    token, report_id = row.token, row.id

    async def open_once() -> None:
        async with factory() as session:
            assert await share_service.register_open(session, token) is not None
            await session.commit()

    await asyncio.gather(open_once(), open_once())
    async with factory() as verify:
        stored = await verify.get(SharedReport, report_id)
        assert stored is not None
        assert stored.opened_count == 2


@pytest.mark.integration
async def test_postgres_revoke_wins_against_waiting_public_open(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    prepared = await _prepared(db_session)
    row, _ = await _create(db_session, prepared, legacy_owner_roots=legacy_owner_roots)
    await db_session.commit()
    token, report_id = row.token, row.id

    revoker = factory()
    prepared_revoke = await _prepared(revoker)
    assert await share_service.revoke(
        revoker,
        report_id,
        prepared_owner=prepared_revoke,
    )

    async def open_waiting() -> SharedReport | None:
        async with factory() as session:
            result = await share_service.register_open(session, token)
            await session.commit()
            return result

    task = asyncio.create_task(open_waiting())
    await asyncio.sleep(0.2)
    assert not task.done(), "public open must wait for the subject/revoke locks"
    await revoker.commit()
    await revoker.close()
    assert await asyncio.wait_for(task, timeout=5) is None

    async with factory() as verify:
        stored = await verify.get(SharedReport, report_id)
        assert stored is not None
        assert stored.revoked_by_user_id == legacy_owner_roots.user_id
        assert stored.opened_count == 0
        assert stored.snapshot is None


@pytest.mark.integration
async def test_postgres_public_resolution_refreshes_preloaded_revocation(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    prepared = await _prepared(db_session)
    row, _ = await _create(db_session, prepared, legacy_owner_roots=legacy_owner_roots)
    await db_session.commit()
    token, report_id = row.token, row.id

    stale = factory()
    preloaded = await stale.get(SharedReport, report_id)
    assert preloaded is not None and preloaded.snapshot is not None
    await stale.commit()

    async with factory() as writer:
        prepared_writer = await _prepared(writer)
        assert await share_service.revoke(
            writer,
            report_id,
            prepared_owner=prepared_writer,
        )
        await writer.commit()

    assert await share_service.resolve_public(stale, token) is None
    assert preloaded.snapshot is None
    await stale.rollback()
    await stale.close()


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["suspend_owner", "rotate_owner"])
async def test_postgres_public_resolution_refreshes_preloaded_identity(
    db_session,
    legacy_owner_roots,
    mutation,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    prepared = await _prepared(db_session)
    row, _ = await _create(db_session, prepared, legacy_owner_roots=legacy_owner_roots)
    replacement = None
    if mutation == "rotate_owner":
        replacement = User(
            username="share-replacement-owner",
            normalized_username="share-replacement-owner",
            password_hash="$synthetic-test-hash",
            status=UserStatus.ACTIVE.value,
        )
        db_session.add(replacement)
    await db_session.commit()

    stale = factory()
    assert await stale.get(SharedReport, row.id) is not None
    assert await stale.get(HealthSubject, legacy_owner_roots.subject_id) is not None
    preloaded_owner = await stale.get(User, legacy_owner_roots.user_id)
    assert preloaded_owner is not None and preloaded_owner.status == UserStatus.ACTIVE.value
    await stale.commit()

    async with factory() as writer:
        await identity_service.acquire_identity_governance_lock(writer)
        if mutation == "suspend_owner":
            owner = await writer.scalar(
                select(User)
                .where(User.id == legacy_owner_roots.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            assert owner is not None
            owner.status = UserStatus.SUSPENDED.value
        else:
            assert replacement is not None
            subject = await writer.scalar(
                select(HealthSubject)
                .where(HealthSubject.id == legacy_owner_roots.subject_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            assert subject is not None
            subject.owner_user_id = replacement.id
        await writer.commit()

    assert await share_service.resolve_public(stale, row.token) is None
    await stale.rollback()
    await stale.close()
