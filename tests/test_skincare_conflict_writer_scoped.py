"""Frozen subject-scoped writer contract for Skincare surfaces."""

from __future__ import annotations

from vitals.services.skincare import conflicts as skincare_conflicts
from vitals.services.skincare import queries as skincare_queries
from vitals.services.skincare import writes as skincare_writes

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, RuleType, Severity, Source, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.models.skincare import (
    SkincareLog,
    SkincareObservation,
    SkincareProduct,
)
from vitals.models.system_alert import SystemAlert
from vitals.ownership import WriteIdentity

from vitals.services.conflicts import engine


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader or writer does when the ownership backfill has not reached a row yet,
# which is a state the application itself can no longer create. The schema
# says so, so this module asks for the one that stood before the contract.
pytestmark = pytest.mark.pre_ownership_contract


EVALUATION_DATE = date(2026, 8, 20)
OTHER_DATE = date(2026, 8, 19)


async def _identity(session: AsyncSession, slug: str) -> WriteIdentity:
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


def _context(
    identity: WriteIdentity,
    *,
    on_date: date = EVALUATION_DATE,
    legacy_bridge: bool = False,
) -> engine.ConflictWriteContext:
    return engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=(
            engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy_bridge
            else engine.LegacyConflictBridge.REJECT
        ),
    )


async def _prepared(
    session: AsyncSession,
    context: engine.ConflictWriteContext,
):
    return await engine.prepare_scoped_write(session, context=context)


async def _legacy_context(db_session, *, on_date=EVALUATION_DATE):
    context = await engine.resolve_legacy_conflict_write_context(
        db_session,
        actor_username="tester",
        evaluation_date=on_date,
    )
    return context, await _prepared(db_session, context)


def _register_skincare_resolver() -> None:
    engine.register_domain_resolver(
        Domain.SKINCARE.value,
        skincare_conflicts.resolve_today_scoped,
    )


async def _blocking_rule(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> ConflictRule:
    rule = ConflictRule(
        subject_id=subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.SKINCARE.value,
        condition_a={"retinoid": True},
        domain_b=Domain.SKINCARE.value,
        condition_b={"peel": True},
        severity=Severity.BLOCK.value,
        message="Synthetic scoped skincare conflict.",
        active=True,
    )
    session.add(rule)
    await session.commit()
    return rule


async def test_prepared_manual_writes_stamp_subject_actor_and_source(db_session):
    identity = await _identity(db_session, "skin-manual")
    context = _context(identity)
    prepared = await _prepared(db_session, context)

    log = await skincare_writes.upsert_log(
        db_session,
        on_date=EVALUATION_DATE,
        moisturizer=True,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    observation = await skincare_writes.add_observation(
        db_session,
        on_date=EVALUATION_DATE,
        inflammation=2,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    product = await skincare_writes.add_product(
        db_session,
        name="Synthetic serum",
        type="serum",
        identity=identity,
        prepared_conflict_write=prepared,
    )

    assert (log.subject_id, log.actor_user_id, log.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MANUAL.value,
    )
    assert (observation.subject_id, observation.actor_user_id, observation.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MANUAL.value,
    )
    assert (product.subject_id, product.actor_user_id) == (
        identity.subject_id,
        identity.actor_user_id,
    )


@pytest.mark.parametrize("writer", ["log", "observation"])
async def test_dated_writes_reject_prepared_evaluation_date_mismatch(
    db_session,
    writer,
):
    identity = await _identity(db_session, f"skin-date-{writer}")
    prepared = await _prepared(db_session, _context(identity, on_date=OTHER_DATE))

    with pytest.raises(engine.ConflictPreparedWriteError, match="date"):
        if writer == "log":
            await skincare_writes.upsert_log(
                db_session,
                on_date=EVALUATION_DATE,
                identity=identity,
                prepared_conflict_write=prepared,
            )
        else:
            await skincare_writes.add_observation(
                db_session,
                on_date=EVALUATION_DATE,
                identity=identity,
                prepared_conflict_write=prepared,
            )

    model = SkincareLog if writer == "log" else SkincareObservation
    assert await db_session.scalar(select(func.count()).select_from(model)) == 0


async def test_writes_reject_identity_mismatch_missing_and_stale_capabilities(
    db_session,
    owner_write,
):
    identity = await _identity(db_session, "skin-token")
    context = _context(identity)
    prepared = await _prepared(db_session, context)
    mismatched = WriteIdentity(identity.subject_id, uuid.uuid4())

    with pytest.raises(engine.ConflictPreparedWriteError):
        await skincare_writes.upsert_log(
            db_session,
            on_date=EVALUATION_DATE,
            identity=mismatched,
            prepared_conflict_write=prepared,
        )
    # A subject cannot be passed without its conflict decision at all now.
    with pytest.raises(TypeError):
        await skincare_writes.add_observation(
            db_session,
            on_date=EVALUATION_DATE,
            identity=identity,
        )
    with pytest.raises(TypeError):
        await skincare_writes.add_product(
            db_session,
            name="Unprepared",
            type="cream",
            identity=identity,
        )

    await db_session.commit()
    with pytest.raises(engine.ConflictPreparedWriteError):
        await skincare_writes.upsert_log(
            db_session,
            on_date=EVALUATION_DATE,
            identity=identity,
            prepared_conflict_write=prepared,
        )


async def test_day_replacement_marker_excludes_prior_checklist_state(
    db_session,
):
    identity = await _identity(db_session, "skin-replacement")
    context = _context(identity)
    first = await skincare_writes.upsert_log(
        db_session,
        on_date=EVALUATION_DATE,
        retinoid=True,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, context),
    )
    await db_session.commit()
    await _blocking_rule(db_session, identity.subject_id)
    _register_skincare_resolver()

    updated = await skincare_writes.upsert_log(
        db_session,
        on_date=EVALUATION_DATE,
        retinoid=False,
        peel=True,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, context),
    )

    assert updated.id == first.id
    assert (updated.retinoid, updated.peel) == (False, True)
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0


async def test_hard_block_is_write_free_and_override_attributes_alert(db_session):
    identity = await _identity(db_session, "skin-conflict")
    rule = await _blocking_rule(db_session, identity.subject_id)
    _register_skincare_resolver()
    context = _context(identity)

    with pytest.raises(engine.ConflictBlocked):
        await skincare_writes.upsert_log(
            db_session,
            on_date=EVALUATION_DATE,
            retinoid=True,
            peel=True,
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, context),
        )

    assert await db_session.scalar(select(func.count()).select_from(SkincareLog)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0

    row = await skincare_writes.upsert_log(
        db_session,
        on_date=EVALUATION_DATE,
        retinoid=True,
        peel=True,
        override=True,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, context),
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )

    assert row.subject_id == identity.subject_id
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (
        identity.subject_id,
        None,
    )
    assert alert.entity_ref == f"skincare:{EVALUATION_DATE.isoformat()}"
    assert alert.overridden_by_user_id == identity.actor_user_id
    assert alert.override_at is not None


async def test_subjects_are_isolated_for_same_day_reads_notes_and_deletes(db_session):
    first = await _identity(db_session, "skin-a")
    second = await _identity(db_session, "skin-b")
    first_context = _context(first)
    second_context = _context(second)
    first_prepared = await _prepared(db_session, first_context)
    second_prepared = await _prepared(db_session, second_context)

    first_log = await skincare_writes.upsert_log(
        db_session,
        on_date=EVALUATION_DATE,
        note="A private note",
        identity=first,
        prepared_conflict_write=first_prepared,
    )
    second_log = await skincare_writes.upsert_log(
        db_session,
        on_date=EVALUATION_DATE,
        note="B private note",
        identity=second,
        prepared_conflict_write=second_prepared,
    )
    first_observation = await skincare_writes.add_observation(
        db_session,
        on_date=EVALUATION_DATE,
        zone="A-zone",
        identity=first,
        prepared_conflict_write=first_prepared,
    )
    second_observation = await skincare_writes.add_observation(
        db_session,
        on_date=EVALUATION_DATE,
        zone="B-zone",
        identity=second,
        prepared_conflict_write=second_prepared,
    )
    first_product = await skincare_writes.add_product(
        db_session,
        name="A product",
        type="serum",
        identity=first,
        prepared_conflict_write=first_prepared,
    )
    second_product = await skincare_writes.add_product(
        db_session,
        name="B product",
        type="cream",
        identity=second,
        prepared_conflict_write=second_prepared,
    )

    assert list(await skincare_queries.list_logs(db_session, subject_id=first.subject_id)) == [first_log]
    assert list(await skincare_queries.list_logs(db_session, subject_id=second.subject_id)) == [second_log]
    assert list(await skincare_queries.list_observations(db_session, subject_id=first.subject_id)) == [first_observation]
    assert list(await skincare_queries.list_products(db_session, subject_id=second.subject_id)) == [second_product]

    assert await skincare_writes.update_log_note(
        db_session,
        first_log.id,
        note="forged",
        identity=second,
        prepared_conflict_write=second_prepared,
    ) is None
    assert await skincare_writes.delete_log(
        db_session,
        first_log.id,
        identity=second,
        prepared_conflict_write=second_prepared,
    ) is False
    assert await skincare_writes.delete_observation(
        db_session,
        first_observation.id,
        identity=second,
        prepared_conflict_write=second_prepared,
    ) is False
    assert await skincare_writes.update_product(
        db_session,
        first_product.id,
        name="forged",
        type="cream",
        identity=second,
        prepared_conflict_write=second_prepared,
    ) is None
    assert await skincare_writes.delete_product(
        db_session,
        first_product.id,
        identity=second,
        prepared_conflict_write=second_prepared,
    ) is False
    assert (first_log.note, first_product.name) == ("A private note", "A product")

    assert await skincare_writes.update_log_note(
        db_session,
        first_log.id,
        note="A updated note",
        identity=first,
        prepared_conflict_write=first_prepared,
    ) is first_log
    assert await skincare_writes.delete_log(
        db_session,
        first_log.id,
        identity=first,
        prepared_conflict_write=first_prepared,
    ) is True
    assert await skincare_writes.delete_observation(
        db_session,
        first_observation.id,
        identity=first,
        prepared_conflict_write=first_prepared,
    ) is True
    assert await skincare_writes.delete_product(
        db_session,
        first_product.id,
        identity=first,
        prepared_conflict_write=first_prepared,
    ) is True
    assert second_observation in await skincare_queries.list_observations(
        db_session,
        subject_id=second.subject_id,
    )






async def test_duplicate_subject_day_is_an_explicit_scope_error(db_session):
    identity = await _identity(db_session, "skin-ambiguous")
    db_session.add_all(
        [
            SkincareLog(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                date=EVALUATION_DATE,
                domain=Domain.SKINCARE.value,
                source=Source.MANUAL.value,
            ),
            SkincareLog(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                date=EVALUATION_DATE,
                domain=Domain.SKINCARE.value,
                source=Source.MCP.value,
            ),
        ]
    )
    await db_session.flush()

    with pytest.raises(engine.ConflictScopeError, match="multiple"):
        await skincare_queries.get_log(
            db_session,
            EVALUATION_DATE,
            subject_id=identity.subject_id,
        )
    with pytest.raises(engine.ConflictScopeError, match="multiple"):
        await skincare_conflicts.resolve_today_scoped(
            db_session,
            scope=_context(identity).scope,
        )
    with pytest.raises(engine.ConflictScopeError, match="multiple"):
        await skincare_writes.upsert_log(
            db_session,
            on_date=EVALUATION_DATE,
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, _context(identity)),
        )


async def test_destructive_seed_rejects_commercial_identity_without_mutation(
    db_session,
    legacy_owner_roots,
):
    from scripts.seed_skincare import seed_skincare

    existing = SkincareLog(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        date=EVALUATION_DATE,
        domain=Domain.SKINCARE.value,
        source=Source.MANUAL.value,
    )
    db_session.add(existing)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="cannot run after commercial identity"):
        await seed_skincare(db_session)

    assert await db_session.scalar(select(func.count()).select_from(SkincareLog)) == 1


async def test_web_writes_are_owned_manual_and_deletes_reject_partial_roots(
    auth_client,
    db_session,
    legacy_owner_roots,
):
    assert (
        await auth_client.post(
            "/skincare/log",
            data={"date": EVALUATION_DATE.isoformat(), "moisturizer": "true"},
        )
    ).status_code == 303
    assert (
        await auth_client.post(
            "/skincare/observation",
            data={"date": EVALUATION_DATE.isoformat(), "inflammation": "2"},
        )
    ).status_code == 303
    assert (
        await auth_client.post(
            "/skincare/product/save",
            data={"name": "Web product", "type": "cream", "active": "true"},
        )
    ).status_code == 303

    log = await db_session.scalar(select(SkincareLog))
    observation = await db_session.scalar(select(SkincareObservation))
    product = await db_session.scalar(select(SkincareProduct))
    assert log is not None and observation is not None and product is not None
    assert (log.subject_id, log.actor_user_id, log.source) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        Source.MANUAL.value,
    )
    assert (observation.subject_id, observation.actor_user_id, observation.source) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        Source.MANUAL.value,
    )
    assert (product.subject_id, product.actor_user_id) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )

    partial = SkincareProduct(
        actor_user_id=legacy_owner_roots.user_id,
        name="Partial hidden product",
        type="serum",
    )
    db_session.add(partial)
    await db_session.commit()
    response = await auth_client.post(f"/skincare/product/{partial.id}/delete")
    assert response.status_code == 303
    assert await db_session.get(SkincareProduct, partial.id) is partial


async def test_web_skincare_routes_obey_optional_module_gate(auth_client):
    await auth_client.post(
        "/settings/modules",
        data={"module": "skincare", "enabled": "false"},
    )

    page = await auth_client.get("/skincare", headers={"Accept": "text/html"})
    write = await auth_client.post(
        "/skincare/log",
        data={"date": EVALUATION_DATE.isoformat()},
        headers={"Accept": "text/html"},
    )
    assert (page.status_code, page.headers["location"]) == (303, "/weight")
    assert write.status_code == 404
    assert write.json() == {"detail": "Module 'skincare' is disabled"}


async def test_mcp_skincare_provenance_scoped_reads_notes_and_delete(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    from vitals.services import modules_service
    from web.routers import mcp as mcp_router

    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    await modules_service.set_module_enabled(
        db_session,
        key="skincare",
        enabled=True,
        subject_id=legacy_owner_roots.subject_id,
    )
    await db_session.commit()

    written_log = await mcp_router.log_skincare(
        on_date=EVALUATION_DATE.isoformat(),
        moisturizer=True,
        note="MCP note",
    )
    written_observation = await mcp_router.log_skincare_observation(
        on_date=EVALUATION_DATE.isoformat(),
        inflammation=2,
        zone="cheeks",
    )
    log = await db_session.get(SkincareLog, written_log["id"])
    observation = await db_session.get(
        SkincareObservation,
        written_observation["id"],
    )
    assert log is not None and observation is not None
    assert (log.subject_id, log.actor_user_id, log.source) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        Source.MCP.value,
    )
    assert (observation.subject_id, observation.actor_user_id, observation.source) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        Source.MCP.value,
    )

    partial_log = SkincareLog(
        actor_user_id=legacy_owner_roots.user_id,
        date=OTHER_DATE,
        domain=Domain.SKINCARE.value,
        source=Source.MANUAL.value,
        note="hidden partial note",
    )
    partial_observation = SkincareObservation(
        actor_user_id=legacy_owner_roots.user_id,
        date=OTHER_DATE,
        domain=Domain.SKINCARE.value,
        source=Source.MANUAL.value,
    )
    db_session.add_all([partial_log, partial_observation])
    await db_session.commit()

    listing = await mcp_router.get_skincare_logs()
    notes = await mcp_router.get_notes(domain="skincare")
    assert [row["id"] for row in listing["logs"]] == [log.id]
    assert [row["id"] for row in listing["observations"]] == [observation.id]
    assert [row["id"] for row in notes] == [log.id]
    assert await mcp_router.log_note(
        "skincare",
        partial_log.id,
        "forged",
    ) == {"error": f"skincare record {partial_log.id} not found"}
    assert await mcp_router.delete_record(
        "skincare_observation",
        partial_observation.id,
    ) == {
        "deleted": False,
        "domain": "skincare_observation",
        "record_id": partial_observation.id,
    }
    updated = await mcp_router.log_note(
        "skincare",
        log.id,
        "updated through MCP",
    )
    assert updated["note"] == "updated through MCP"
    assert await mcp_router.delete_record(
        "skincare_observation",
        observation.id,
    ) == {
        "deleted": True,
        "domain": "skincare_observation",
        "record_id": observation.id,
    }
    await db_session.refresh(log)
    assert (log.note, log.source, log.actor_user_id) == (
        "updated through MCP",
        Source.MCP.value,
        legacy_owner_roots.user_id,
    )


async def test_mcp_skincare_reads_writes_notes_and_deletes_are_gated(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    from web.routers import mcp as mcp_router

    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    error = {"error": "module 'skincare' is disabled"}

    assert await mcp_router.get_skincare_logs() == error
    assert await mcp_router.log_skincare(on_date=EVALUATION_DATE.isoformat()) == error
    assert await mcp_router.log_skincare_observation(
        on_date=EVALUATION_DATE.isoformat()
    ) == error
    assert await mcp_router.log_note("skincare", 1, "hidden") == error
    assert await mcp_router.get_notes(domain="skincare") == [error]
    assert await mcp_router.delete_record("skincare_observation", 1) == error
    assert await db_session.scalar(select(func.count()).select_from(SkincareLog)) == 0


@pytest.mark.integration
async def test_postgres_concurrent_same_subject_first_upserts_leave_one_row(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    context = _context(identity)
    await db_session.commit()

    async def upsert(*, retinoid: bool, moisturizer: bool) -> None:
        async with factory() as session:
            await skincare_writes.upsert_log(
                session,
                on_date=EVALUATION_DATE,
                retinoid=retinoid,
                moisturizer=moisturizer,
                identity=identity,
                prepared_conflict_write=await _prepared(session, context),
            )
            await session.commit()

    await asyncio.gather(
        upsert(retinoid=True, moisturizer=False),
        upsert(retinoid=False, moisturizer=True),
    )

    async with factory() as verify:
        rows = list(
            await verify.scalars(
                select(SkincareLog).where(
                    SkincareLog.subject_id == identity.subject_id,
                    SkincareLog.date == EVALUATION_DATE,
                )
            )
        )
    assert len(rows) == 1
    assert (rows[0].retinoid, rows[0].moisturizer) in {
        (True, False),
        (False, True),
    }


@pytest.mark.integration
async def test_postgres_legacy_bridge_write_serializes_against_subject_creation(
    db_session,
    legacy_owner_roots,
):
    from vitals.services.identity_service import acquire_identity_governance_lock

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    bridge_locked = asyncio.Event()
    mutation_attempted = asyncio.Event()
    await db_session.commit()

    async def legacy_write() -> None:
        async with factory() as session:
            context = await engine.resolve_legacy_conflict_write_context(
                session,
                actor_username="tester",
                evaluation_date=EVALUATION_DATE,
            )
            bridge_locked.set()
            await asyncio.wait_for(mutation_attempted.wait(), timeout=5)
            await skincare_writes.upsert_log(
                session,
                on_date=EVALUATION_DATE,
                moisturizer=True,
                identity=context.identity,
                prepared_conflict_write=await _prepared(session, context),
            )
            await session.commit()

    async def create_second_subject() -> None:
        await asyncio.wait_for(bridge_locked.wait(), timeout=5)
        async with factory() as session:
            mutation_attempted.set()
            await acquire_identity_governance_lock(session)
            user = User(
                username="skin-race-second",
                normalized_username="skin-race-second",
                password_hash="$synthetic-test-hash",
                status=UserStatus.ACTIVE.value,
            )
            session.add(user)
            await session.flush()
            session.add(HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty"))
            await session.commit()

    await asyncio.gather(legacy_write(), create_second_subject())

    async with factory() as verify:
        assert await verify.scalar(select(func.count()).select_from(HealthSubject)) == 2
        row = await verify.scalar(select(SkincareLog))
        assert row is not None
        assert row.subject_id == legacy_owner_roots.subject_id


@pytest.mark.integration
async def test_postgres_destructive_seed_serializes_before_identity_creation(
    db_session,
    monkeypatch,
):
    from scripts import seed_skincare as seed_module
    from vitals.services.identity_service import (
        acquire_identity_governance_lock as real_governance_lock,
    )

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    seed_locked = asyncio.Event()
    identity_attempted = asyncio.Event()
    original_seed_lock = seed_module.acquire_identity_governance_lock

    async def observed_seed_lock(session: AsyncSession) -> None:
        await original_seed_lock(session)
        seed_locked.set()
        await asyncio.wait_for(identity_attempted.wait(), timeout=5)

    monkeypatch.setattr(
        seed_module,
        "acquire_identity_governance_lock",
        observed_seed_lock,
    )
    await db_session.commit()

    async def run_seed() -> None:
        async with factory() as session:
            await seed_module.seed_skincare(session)
            await session.commit()

    async def create_identity() -> None:
        await asyncio.wait_for(seed_locked.wait(), timeout=5)
        async with factory() as session:
            identity_attempted.set()
            await real_governance_lock(session)
            assert (
                await session.scalar(select(func.count()).select_from(SkincareLog))
                == 19
            )
            user = User(
                username="skin-seed-race",
                normalized_username="skin-seed-race",
                password_hash="$synthetic-test-hash",
                status=UserStatus.ACTIVE.value,
            )
            session.add(user)
            await session.flush()
            session.add(HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty"))
            await session.commit()

    await asyncio.wait_for(
        asyncio.gather(run_seed(), create_identity()),
        timeout=10,
    )

    async with factory() as verify:
        assert await verify.scalar(select(func.count()).select_from(HealthSubject)) == 1
        assert await verify.scalar(select(func.count()).select_from(SkincareLog)) == 19

async def test_a_skincare_row_without_a_subject_belongs_to_nobody(
    db_session,
    legacy_owner_roots,
    owner_write,
):
    """The domain has no adoption bridge left: an unowned row stays unowned and
    stays invisible, and a partial-root row is never mistaken for one."""

    unowned = SkincareLog(
        date=EVALUATION_DATE,
        domain=Domain.SKINCARE.value,
        source=Source.MANUAL.value,
        note="legacy",
    )
    db_session.add(unowned)
    await db_session.flush()

    assert list(
        await skincare_queries.list_logs(
            db_session, subject_id=owner_write.subject_id
        )
    ) == []
    written = await skincare_writes.upsert_log(
        db_session,
        on_date=EVALUATION_DATE,
        note="mine",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(EVALUATION_DATE),
    )
    assert written is not unowned
    assert written.subject_id == owner_write.subject_id
    assert unowned.subject_id is None and unowned.note == "legacy"
