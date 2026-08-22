"""Stage-2 subject and actor boundaries for goal cards."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.milestones import Milestone
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, milestones_service
from vitals.services.legacy_ownership import LegacySubjectResolutionError


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader or writer does when the ownership backfill has not reached a row yet,
# which is a state the application itself can no longer create. The schema
# says so, so this module asks for the one that stood before the contract.
pytestmark = pytest.mark.pre_ownership_contract


TODAY = date(2026, 8, 20)


def _identity(legacy_owner_roots, *, system: bool = False) -> WriteIdentity:
    return WriteIdentity(
        legacy_owner_roots.subject_id,
        None if system else legacy_owner_roots.user_id,
    )


def _context(
    identity: WriteIdentity,
    *,
    legacy: bool = False,
) -> conflict_engine.ConflictWriteContext:
    return conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=TODAY,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )


async def _prepared(
    session: AsyncSession,
    identity: WriteIdentity,
    *,
    legacy: bool = False,
) -> conflict_engine.PreparedConflictWrite:
    return await conflict_engine.prepare_scoped_write(
        session,
        context=_context(identity, legacy=legacy),
    )


async def _new_identity(session: AsyncSession, slug: str) -> WriteIdentity:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    return WriteIdentity(subject.id, user.id)


async def test_scoped_create_stamps_subject_actor_and_update_retains_origin(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    row = await milestones_service.create_milestone(
        db_session,
        name="Synthetic goal",
        domain=Domain.WEIGHT.value,
        note="original",
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    assert (row.subject_id, row.actor_user_id) == (
        identity.subject_id,
        identity.actor_user_id,
    )

    original_actor = row.actor_user_id
    updated = await milestones_service.update_milestone(
        db_session,
        row.id,
        note="updated",
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    assert updated is row
    assert (row.note, row.actor_user_id) == ("updated", original_actor)


async def test_scoped_writer_capability_is_required_live_and_human(
    db_session,
    legacy_owner_roots,
    owner_write,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity)

    # A subject cannot be passed without its conflict decision at all now.
    with pytest.raises(TypeError):
        await milestones_service.create_milestone(
            db_session,
            name="missing token",
            identity=identity,
        )
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await milestones_service.create_milestone(
            db_session,
            name="wrong actor",
            identity=WriteIdentity(identity.subject_id, None),
            prepared_conflict_write=prepared,
        )

    await db_session.commit()
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await milestones_service.create_milestone(
            db_session,
            name="expired token",
            identity=identity,
            prepared_conflict_write=prepared,
        )

    system_identity = _identity(legacy_owner_roots, system=True)
    with pytest.raises(
        conflict_engine.ConflictPreparedWriteError,
        match="human actor",
    ):
        await milestones_service.create_milestone(
            db_session,
            name="system goal",
            identity=system_identity,
            prepared_conflict_write=await _prepared(
                db_session,
                system_identity,
            ),
        )


async def test_subject_reads_are_isolated_and_foreign_ids_do_not_enumerate(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    foreign = await _new_identity(db_session, "milestone-foreign")
    own = Milestone(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        name="own",
        domain=Domain.WEIGHT.value,
    )
    other = Milestone(
        subject_id=foreign.subject_id,
        actor_user_id=foreign.actor_user_id,
        name="foreign",
        domain=Domain.WEIGHT.value,
    )
    db_session.add_all([own, other])
    await db_session.commit()

    rows = await milestones_service.list_milestones(
        db_session,
        subject_id=identity.subject_id,
    )
    assert [row.id for row in rows] == [own.id]

    prepared = await _prepared(db_session, identity)
    assert await milestones_service.update_milestone(
        db_session,
        other.id,
        note="must not write",
        identity=identity,
        prepared_conflict_write=prepared,
    ) is None
    assert await milestones_service.delete_milestone(
        db_session,
        other.id,
        identity=identity,
        prepared_conflict_write=prepared,
    ) is False
    assert other.note is None
    assert await db_session.get(Milestone, other.id) is other


async def test_a_goal_without_a_subject_belongs_to_nobody(
    db_session,
    legacy_owner_roots,
):
    """A goal is somebody's. One with no subject is not this subject's, and no
    write path will adopt it into being theirs."""

    identity = _identity(legacy_owner_roots)
    legacy = Milestone(name="legacy", domain=Domain.WEIGHT.value)
    partial = Milestone(
        actor_user_id=identity.actor_user_id,
        name="partial",
        domain=Domain.WEIGHT.value,
    )
    db_session.add_all([legacy, partial])
    await db_session.commit()

    # A row with an actor but no subject is broken provenance, not merely
    # somebody else's, so it is reported rather than passed over.
    with pytest.raises(milestones_service.MilestoneOwnershipError, match="partial"):
        await milestones_service.list_milestones(
            db_session,
            subject_id=identity.subject_id,
        )
    with pytest.raises(milestones_service.MilestoneOwnershipError, match="partial"):
        await milestones_service.update_milestone(
            db_session,
            partial.id,
            note="must not adopt",
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, identity),
        )
    assert (
        await milestones_service.update_milestone(
            db_session,
            legacy.id,
            note="must not adopt",
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, identity),
        )
        is None
    )
    assert (legacy.subject_id, legacy.note) == (None, None)
    assert (partial.subject_id, partial.note) == (None, None)

    await db_session.delete(partial)
    await db_session.flush()
    # With the broken row gone, an unowned goal is simply not this subject's.
    assert await milestones_service.list_milestones(
        db_session,
        subject_id=identity.subject_id,
    ) == []


async def test_progress_propagates_subject_to_weight_measurement_scan_and_settings(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    from vitals.services import body_scan_service, modules_service, weight_service
    from vitals.services.analytics import body_metrics

    identity = _identity(legacy_owner_roots)
    seen: list[tuple[str, object, bool]] = []

    async def weights(session, **kwargs):
        del session
        seen.append(("weight", kwargs["subject_id"]))
        return [SimpleNamespace(weight_kg=88.0)]

    async def measurements(session, **kwargs):
        del session
        seen.append(("measurement", kwargs["subject_id"]))
        return [SimpleNamespace(body_fat_pct=14.0)]

    async def scans(session, **kwargs):
        del session
        seen.append(("scan", kwargs["subject_id"]))
        return [SimpleNamespace(metrics=[])]

    async def modules(session, *, subject_id=None):
        del session
        seen.append(("modules", subject_id))
        return {"body_comp": True}

    monkeypatch.setattr(weight_service, "list_active_weights", weights)
    monkeypatch.setattr(weight_service, "list_body_measurements", measurements)
    monkeypatch.setattr(body_scan_service, "list_scans", scans)
    monkeypatch.setattr(modules_service, "get_enabled_modules", modules)
    monkeypatch.setattr(body_metrics, "body_fat_pct_from_scan", lambda metrics: 15.0)

    weight_goal = Milestone(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        name="weight",
        domain=Domain.WEIGHT.value,
        target_value=82,
    )
    body_goal = Milestone(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        name="body",
        domain=Domain.BODY_COMPOSITION.value,
        target_value=12,
        target_unit="%",
    )
    db_session.add_all([weight_goal, body_goal])
    await db_session.flush()

    await milestones_service.progress(
        db_session,
        weight_goal,
        subject_id=identity.subject_id,
    )
    await milestones_service.progress(
        db_session,
        body_goal,
        subject_id=identity.subject_id,
    )
    assert set(seen) == {
        ("weight", identity.subject_id),
        ("measurement", identity.subject_id),
        ("scan", identity.subject_id),
        ("modules", identity.subject_id),
    }


async def test_exact_one_mcp_and_aggregate_scopes_close_with_second_subject(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    mcp_router = pytest.importorskip("web.routers.mcp")
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    await _new_identity(db_session, "milestone-second-subject")
    await db_session.commit()

    calls = (
        mcp_router.get_milestones,
        mcp_router.get_timeline,
        mcp_router.get_full_snapshot,
        mcp_router.export_everything,
        mcp_router.get_data_overview,
        mcp_router.generate_digest_now,
    )
    for call in calls:
        with pytest.raises(LegacySubjectResolutionError):
            await call()

    from vitals import config as config_module
    from vitals.integrations import llm_client as llm_client_module
    from vitals.services import digest_service

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: SimpleNamespace(openrouter_api_key="synthetic"),
    )
    monkeypatch.setattr(
        llm_client_module,
        "LLMClient",
        lambda: pytest.fail("digest job reached the LLM after scope rejection"),
    )
    with pytest.raises(LegacySubjectResolutionError):
        await digest_service.digest_job(session_factory)


async def test_today_and_timeline_web_reads_close_with_second_subject(
    db_session,
    legacy_owner_roots,
):
    from web.routers import timeline as timeline_router
    from web.routers import today as today_router
    from web.routers import reports as reports_router

    del legacy_owner_roots
    await _new_identity(db_session, "milestone-web-second-subject")
    await db_session.commit()

    calls = (
        lambda: today_router.today_dashboard(
            request=None,
            db=db_session,
            username="tester",
        ),
        lambda: timeline_router.timeline_feed(
            request=None,
            db=db_session,
            username="tester",
        ),
        lambda: reports_router.generate_digest_now(
            request=None,
            db=db_session,
            username="tester",
            _rl=None,
        ),
    )
    for call in calls:
        with pytest.raises(LegacySubjectResolutionError):
            await call()


async def test_whole_lake_mcp_and_digest_reject_partial_milestone_roots(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    from web.routers import mcp as mcp_router
    from web.routers import reports as reports_router

    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    db_session.add(
        Milestone(
            actor_user_id=legacy_owner_roots.user_id,
            name="partial composition root",
            domain=Domain.WEIGHT.value,
        )
    )
    await db_session.commit()

    for call in (
        mcp_router.get_full_snapshot,
        mcp_router.export_everything,
        mcp_router.get_data_overview,
        mcp_router.generate_digest_now,
    ):
        with pytest.raises(milestones_service.MilestoneOwnershipError, match="partial"):
            await call()

    with pytest.raises(milestones_service.MilestoneOwnershipError, match="partial"):
        await reports_router.generate_digest_now(
            request=None,
            db=db_session,
            username="tester",
            _rl=None,
        )

    from vitals import config as config_module
    from vitals.integrations import llm_client as llm_client_module
    from vitals.services import digest_service

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: SimpleNamespace(openrouter_api_key="synthetic"),
    )
    monkeypatch.setattr(
        llm_client_module,
        "LLMClient",
        lambda: pytest.fail("digest job reached the LLM after root rejection"),
    )
    with pytest.raises(milestones_service.MilestoneOwnershipError, match="partial"):
        await digest_service.digest_job(session_factory)


@pytest.mark.integration
async def test_postgres_concurrent_updates_wait_on_subject_governance(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = _identity(legacy_owner_roots)
    row = Milestone(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        name="concurrent",
        domain=Domain.WEIGHT.value,
    )
    db_session.add(row)
    await db_session.commit()
    row_id = row.id

    session_a = factory()
    await milestones_service.update_milestone(
        session_a,
        row_id,
        note="writer-a",
        identity=identity,
        prepared_conflict_write=await _prepared(session_a, identity),
    )

    async def writer_b() -> None:
        async with factory() as session_b:
            await milestones_service.update_milestone(
                session_b,
                row_id,
                note="writer-b",
                identity=identity,
                prepared_conflict_write=await _prepared(session_b, identity),
            )
            await session_b.commit()

    task_b = asyncio.create_task(writer_b())
    await asyncio.sleep(0.25)
    assert not task_b.done(), "writer B must wait on subject governance"
    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        persisted = await verify.get(Milestone, row_id)
    assert persisted is not None
    assert (persisted.note, persisted.actor_user_id) == (
        "writer-b",
        identity.actor_user_id,
    )
