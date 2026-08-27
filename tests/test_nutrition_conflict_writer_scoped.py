"""Focused contracts for the subject-scoped Nutrition conflict writer."""

from __future__ import annotations

from vitals.services.nutrition import conflicts as nutrition_conflicts
from vitals.services.nutrition import jobs as nutrition_jobs
from vitals.services.nutrition import writes as nutrition_writes

from tests.job_runner import run_job_for_every_subject

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, RuleType, Severity, Source, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.models.nutrition import MealLog
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


def _context(
    legacy_owner_roots,
    *,
    on_date: date = EVALUATION_DATE,
    actor_user_id: uuid.UUID | None | object = ...,
) -> engine.ConflictWriteContext:
    actor = (
        legacy_owner_roots.user_id
        if actor_user_id is ...
        else actor_user_id
    )
    return engine.ConflictWriteContext(
        identity=WriteIdentity(legacy_owner_roots.subject_id, actor),
        evaluation_date=on_date,
    )


async def _prepared(db_session, context):
    return await engine.prepare_scoped_write(
        db_session,
        context=context,
    )


async def _seed_rule(
    db_session,
    legacy_owner_roots,
    *,
    nutrition_condition: dict,
    message: str = "Synthetic scoped nutrition conflict.",
    day_end_only: bool = False,
) -> ConflictRule:
    rule = ConflictRule(
        subject_id=legacy_owner_roots.subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.LABS.value,
        condition_a={"marker": "synthetic-risk"},
        domain_b=Domain.NUTRITION.value,
        condition_b=nutrition_condition,
        severity=Severity.BLOCK.value,
        message=message,
        params={"day_end_only": True} if day_end_only else None,
        active=True,
    )
    db_session.add(rule)
    await db_session.commit()
    return rule


def _register_resolvers() -> None:
    async def labs(session, *, scope):
        del session, scope
        return [{"marker": "synthetic-risk"}]

    engine.register_domain_resolver(Domain.LABS.value, labs)
    engine.register_domain_resolver(
        Domain.NUTRITION.value,
        nutrition_conflicts.resolve_today_scoped,
    )


async def _owned_meal(
    db_session,
    legacy_owner_roots,
    *,
    on_date: date = EVALUATION_DATE,
    name: str = "baseline",
    calories: float | None = None,
    source: str = Source.MANUAL.value,
) -> MealLog:
    context = _context(legacy_owner_roots, on_date=on_date)
    return await nutrition_writes.log_meal(
        db_session,
        on_date=on_date,
        name=name,
        calories=calories,
        source=source,
        identity=context.identity,
        prepared_conflict_write=await _prepared(db_session, context),
    )


async def test_create_evaluates_post_write_daily_aggregate(
    db_session,
    legacy_owner_roots,
):
    await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"calories": {"$gt": 1000}},
    )
    _register_resolvers()
    await _owned_meal(db_session, legacy_owner_roots, calories=700)
    await db_session.commit()
    context = _context(legacy_owner_roots)

    with pytest.raises(engine.ConflictBlocked):
        await nutrition_writes.log_meal(
            db_session,
            on_date=EVALUATION_DATE,
            name="would cross the threshold",
            calories=400,
            identity=context.identity,
            prepared_conflict_write=await _prepared(db_session, context),
        )

    assert await db_session.scalar(
        select(func.count()).select_from(MealLog)
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0


async def test_create_evaluates_per_meal_name_rule(
    db_session,
    legacy_owner_roots,
):
    await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"name": "steak"},
    )
    _register_resolvers()
    context = _context(legacy_owner_roots)

    with pytest.raises(engine.ConflictBlocked):
        await nutrition_writes.log_meal(
            db_session,
            on_date=EVALUATION_DATE,
            name="steak",
            calories=600,
            identity=context.identity,
            prepared_conflict_write=await _prepared(db_session, context),
        )

    assert await db_session.scalar(
        select(func.count()).select_from(MealLog)
    ) == 0


async def test_same_day_update_replaces_old_daily_total_without_double_counting(
    db_session,
    legacy_owner_roots,
):
    await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"calories": {"$gt": 1000}},
    )
    _register_resolvers()
    row = await _owned_meal(db_session, legacy_owner_roots, calories=600)
    await db_session.commit()
    context = _context(legacy_owner_roots)

    result = await nutrition_writes.update_meal(
        db_session,
        row.id,
        on_date=EVALUATION_DATE,
        name="updated",
        calories=700,
        identity=context.identity,
        prepared_conflict_write=await _prepared(db_session, context),
    )

    assert result is row
    assert row.calories == 700
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0


async def test_move_date_evaluates_the_destination_day_and_is_write_free_on_block(
    db_session,
    legacy_owner_roots,
):
    await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"calories": {"$gt": 1000}},
    )
    _register_resolvers()
    moving = await _owned_meal(
        db_session,
        legacy_owner_roots,
        on_date=OTHER_DATE,
        calories=600,
    )
    await _owned_meal(db_session, legacy_owner_roots, calories=500)
    await db_session.commit()
    context = _context(legacy_owner_roots)

    with pytest.raises(engine.ConflictBlocked):
        await nutrition_writes.update_meal(
            db_session,
            moving.id,
            on_date=EVALUATION_DATE,
            name="move",
            calories=600,
            identity=context.identity,
            prepared_conflict_write=await _prepared(db_session, context),
        )

    assert moving.date == OTHER_DATE
    assert moving.calories == 600
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0


async def test_prepared_date_mismatch_is_rejected_before_any_write(
    db_session,
    legacy_owner_roots,
):
    _register_resolvers()
    context = _context(legacy_owner_roots, on_date=OTHER_DATE)

    with pytest.raises(engine.ConflictPreparedWriteError):
        await nutrition_writes.log_meal(
            db_session,
            on_date=EVALUATION_DATE,
            name="wrong day",
            identity=context.identity,
            prepared_conflict_write=await _prepared(db_session, context),
        )

    assert await db_session.scalar(
        select(func.count()).select_from(MealLog)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0


async def test_block_is_write_free_and_override_stamps_subject_actor_and_alert(
    db_session,
    legacy_owner_roots,
):
    rule = await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"name": "steak"},
    )
    _register_resolvers()
    context = _context(legacy_owner_roots)
    prepared = await _prepared(db_session, context)

    with pytest.raises(engine.ConflictBlocked):
        await nutrition_writes.log_meal(
            db_session,
            on_date=EVALUATION_DATE,
            name="steak",
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
    assert await db_session.scalar(select(func.count()).select_from(MealLog)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0

    row = await nutrition_writes.log_meal(
        db_session,
        on_date=EVALUATION_DATE,
        name="steak",
        override=True,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    assert (row.subject_id, row.actor_user_id) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (
        legacy_owner_roots.subject_id,
        None,
    )
    assert alert.entity_ref == f"meal:{EVALUATION_DATE.isoformat()}"
    assert alert.overridden_by_user_id == legacy_owner_roots.user_id
    assert alert.override_at is not None


async def test_update_refreshes_stale_locked_row_before_daily_delta(
    db_session,
    legacy_owner_roots,
):
    await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"calories": {"$gt": 1000}},
    )
    _register_resolvers()
    row = await _owned_meal(db_session, legacy_owner_roots, calories=100)
    await db_session.commit()
    await db_session.execute(
        update(MealLog).where(MealLog.id == row.id).values(calories=600),
        execution_options={"synchronize_session": False},
    )
    assert row.calories == 100
    context = _context(legacy_owner_roots)

    result = await nutrition_writes.update_meal(
        db_session,
        row.id,
        on_date=EVALUATION_DATE,
        name="fresh update",
        calories=700,
        identity=context.identity,
        prepared_conflict_write=await _prepared(db_session, context),
    )

    assert result is row
    assert row.calories == 700


async def test_foreign_partial_and_legacy_roots_obey_bridge_contract(
    db_session,
    legacy_owner_roots,
):
    foreign_user = User(
        username="foreign-nutrition-owner",
        normalized_username="foreign-nutrition-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(foreign_user)
    await db_session.flush()
    foreign_subject = HealthSubject(
        owner_user_id=foreign_user.id,
        timezone="Asia/Almaty",
    )
    db_session.add(foreign_subject)
    await db_session.flush()
    foreign = MealLog(
        subject_id=foreign_subject.id,
        actor_user_id=foreign_user.id,
        domain=Domain.NUTRITION.value,
        source=Source.MANUAL.value,
        date=EVALUATION_DATE,
        name="foreign",
    )
    partial = MealLog(
        actor_user_id=legacy_owner_roots.user_id,
        domain=Domain.NUTRITION.value,
        source=Source.MANUAL.value,
        date=EVALUATION_DATE,
        name="partial",
    )
    legacy = MealLog(
        domain=Domain.NUTRITION.value,
        source=Source.MANUAL.value,
        date=EVALUATION_DATE,
        name="legacy",
    )
    db_session.add_all([foreign, partial, legacy])
    await db_session.commit()
    exact_context = _context(legacy_owner_roots)
    assert await nutrition_writes.update_meal(
        db_session,
        foreign.id,
        on_date=EVALUATION_DATE,
        name="forged",
        identity=exact_context.identity,
        prepared_conflict_write=await _prepared(db_session, exact_context),
    ) is None
    assert foreign.name == "foreign"
    await db_session.delete(foreign)
    await db_session.flush()
    await db_session.delete(foreign_subject)
    await db_session.flush()
    await db_session.delete(foreign_user)
    await db_session.commit()
    context = await engine.resolve_legacy_conflict_write_context(
        db_session,
        actor_username="tester",
        evaluation_date=EVALUATION_DATE,
    )
    prepared = await _prepared(db_session, context)

    # Nutrition is closed: adoption on write is gone, so a row belonging to
    # nobody is simply out of scope rather than claimable, and a partial row
    # with an actor and no subject stays exactly as unreachable as it was.
    assert await nutrition_writes.update_meal(
        db_session,
        partial.id,
        on_date=EVALUATION_DATE,
        name="forged",
        identity=context.identity,
        prepared_conflict_write=prepared,
    ) is None
    assert partial.name == "partial"

    assert await nutrition_writes.update_meal(
        db_session,
        legacy.id,
        on_date=EVALUATION_DATE,
        name="adopted",
        identity=context.identity,
        prepared_conflict_write=prepared,
    ) is None
    assert legacy.name == "legacy"
    assert legacy.subject_id is None


async def test_prepared_mismatch_and_committed_capability_are_rejected(
    db_session,
    legacy_owner_roots,
):
    context = _context(legacy_owner_roots)
    prepared = await _prepared(db_session, context)
    mismatched = WriteIdentity(context.identity.subject_id, uuid.uuid4())

    with pytest.raises(engine.ConflictPreparedWriteError):
        await nutrition_writes.log_meal(
            db_session,
            on_date=EVALUATION_DATE,
            name="mismatch",
            identity=mismatched,
            prepared_conflict_write=prepared,
        )
    await db_session.commit()
    with pytest.raises(engine.ConflictPreparedWriteError):
        await nutrition_writes.log_meal(
            db_session,
            on_date=EVALUATION_DATE,
            name="committed token",
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
    assert await db_session.scalar(select(func.count()).select_from(MealLog)) == 0


async def test_day_end_job_uses_system_actor_and_exact_subject_date(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    rule = await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"calories": {"$lt": 800}},
        day_end_only=True,
    )
    _register_resolvers()
    await _owned_meal(
        db_session,
        legacy_owner_roots,
        on_date=EVALUATION_DATE,
        calories=200,
    )
    await _owned_meal(
        db_session,
        legacy_owner_roots,
        on_date=OTHER_DATE,
        calories=2000,
    )
    await db_session.commit()
    monkeypatch.setattr(nutrition_jobs, "today_local", lambda: EVALUATION_DATE)
    original = engine.resolve_subject_conflict_write_context
    captured = {}

    async def capture_context(session, *, subject_id, evaluation_date=None):
        captured["subject_id"] = subject_id
        captured["evaluation_date"] = evaluation_date
        context = await original(
            session,
            subject_id=subject_id,
            evaluation_date=evaluation_date,
        )
        captured["identity"] = context.identity
        return context

    monkeypatch.setattr(
        engine,
        "resolve_subject_conflict_write_context",
        capture_context,
    )

    await run_job_for_every_subject(nutrition_jobs.day_end_job, session_factory)

    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    # The job names the subject it is running for rather than asking for "the
    # sole one", and still acts as the system: no human actor is attributed.
    assert captured == {
        "subject_id": legacy_owner_roots.subject_id,
        "evaluation_date": EVALUATION_DATE,
        "identity": WriteIdentity(legacy_owner_roots.subject_id, None),
    }
    assert alert is not None
    assert (alert.subject_id, alert.overridden_by_user_id) == (
        legacy_owner_roots.subject_id,
        None,
    )
    assert alert.entity_ref == f"meal:{EVALUATION_DATE.isoformat()}"


async def test_web_create_and_update_block_then_override_with_human_provenance(
    auth_client,
    db_session,
    legacy_owner_roots,
):
    rule = await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"name": "steak"},
    )
    _register_resolvers()

    blocked_create = await auth_client.post(
        "/nutrition/meal",
        data={"date": EVALUATION_DATE.isoformat(), "name": "steak"},
    )
    assert blocked_create.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(MealLog)) == 0

    overridden_create = await auth_client.post(
        "/nutrition/meal",
        data={
            "date": EVALUATION_DATE.isoformat(),
            "name": "steak",
            "override": "true",
        },
    )
    assert overridden_create.status_code == 303
    created = await db_session.scalar(select(MealLog))
    assert created is not None
    assert (created.subject_id, created.actor_user_id, created.source) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        Source.MANUAL.value,
    )

    safe = await _owned_meal(db_session, legacy_owner_roots, name="salad")
    await db_session.commit()
    blocked_update = await auth_client.post(
        "/nutrition/meal",
        data={
            "id": str(safe.id),
            "date": EVALUATION_DATE.isoformat(),
            "name": "steak",
        },
    )
    assert blocked_update.status_code == 409
    await db_session.refresh(safe)
    assert safe.name == "salad"

    overridden_update = await auth_client.post(
        "/nutrition/meal",
        data={
            "id": str(safe.id),
            "date": EVALUATION_DATE.isoformat(),
            "name": "steak",
            "override": "true",
        },
    )
    assert overridden_update.status_code == 303
    await db_session.refresh(safe)
    assert (safe.name, safe.subject_id, safe.actor_user_id) == (
        "steak",
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    alerts = list(
        await db_session.scalars(
            select(SystemAlert).where(
                SystemAlert.alert_key == f"conflict:{rule.id}"
            )
        )
    )
    assert alerts and all(
        alert.overridden_by_user_id == legacy_owner_roots.user_id
        for alert in alerts
    )


async def test_mcp_create_and_partial_update_conflicts_preserve_mcp_provenance(
    db_session,
    session_factory,
    legacy_owner_roots,
    all_modules_on,
    monkeypatch,
):
    mcp_router = pytest.importorskip("web.routers.mcp")
    rule = await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"name": "steak"},
    )
    _register_resolvers()
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    blocked_create = await mcp_router.log_meal(
        name="steak",
        on_date=EVALUATION_DATE.isoformat(),
    )
    assert blocked_create["blocked"] is True
    assert await db_session.scalar(select(func.count()).select_from(MealLog)) == 0

    safe = await mcp_router.log_meal(
        name="salad",
        calories=640,
        protein_g=42,
        note="keep me",
        on_date=EVALUATION_DATE.isoformat(),
    )
    blocked_update = await mcp_router.update_meal(
        safe["id"],
        name="steak",
    )
    assert blocked_update["blocked"] is True
    current = await db_session.get(MealLog, safe["id"])
    assert current is not None and current.name == "salad"

    overridden = await mcp_router.update_meal(
        safe["id"],
        name="steak",
        override=True,
    )
    assert overridden["name"] == "steak"
    await db_session.refresh(current)
    assert (
        current.calories,
        current.protein_g,
        current.note,
        current.source,
        current.subject_id,
        current.actor_user_id,
    ) == (
        640,
        42,
        "keep me",
        Source.MCP.value,
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    assert alert is not None
    assert alert.overridden_by_user_id == legacy_owner_roots.user_id

    overridden_create = await mcp_router.log_meal(
        name="steak",
        calories=500,
        on_date=EVALUATION_DATE.isoformat(),
        override=True,
    )
    created = await db_session.get(MealLog, overridden_create["id"])
    assert created is not None
    assert (created.source, created.subject_id, created.actor_user_id) == (
        Source.MCP.value,
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )


async def test_mcp_nutrition_notes_reject_partial_roots_and_preserve_provenance(
    db_session,
    session_factory,
    legacy_owner_roots,
    all_modules_on,
    monkeypatch,
):
    mcp_router = pytest.importorskip("web.routers.mcp")
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    owned = await _owned_meal(
        db_session,
        legacy_owner_roots,
        name="owned note",
        source=Source.MCP.value,
    )
    partial = MealLog(
        actor_user_id=legacy_owner_roots.user_id,
        domain=Domain.NUTRITION.value,
        source=Source.MANUAL.value,
        date=EVALUATION_DATE,
        name="partial note",
        note="must stay hidden",
    )
    db_session.add(partial)
    await db_session.commit()

    updated = await mcp_router.log_note(
        domain="nutrition",
        record_id=owned.id,
        note="scoped note",
    )
    rejected = await mcp_router.log_note(
        domain="nutrition",
        record_id=partial.id,
        note="forged",
    )
    notes = await mcp_router.get_notes(domain="nutrition")

    assert updated["note"] == "scoped note"
    assert rejected == {"error": f"nutrition record {partial.id} not found"}
    await db_session.refresh(owned)
    await db_session.refresh(partial)
    assert (owned.note, owned.source, owned.actor_user_id) == (
        "scoped note",
        Source.MCP.value,
        legacy_owner_roots.user_id,
    )
    assert partial.note == "must stay hidden"
    assert [row["id"] for row in notes] == [owned.id]


@pytest.mark.integration
async def test_postgres_same_subject_concurrent_creates_serialize_daily_threshold(
    db_session,
    legacy_owner_roots,
):
    await _seed_rule(
        db_session,
        legacy_owner_roots,
        nutrition_condition={"calories": {"$gt": 1000}},
    )
    _register_resolvers()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    context = _context(legacy_owner_roots)

    async def create(name: str) -> str:
        async with factory() as session:
            prepared = await engine.prepare_scoped_write(
                session,
                context=context,
            )
            try:
                await nutrition_writes.log_meal(
                    session,
                    on_date=EVALUATION_DATE,
                    name=name,
                    calories=600,
                    identity=context.identity,
                    prepared_conflict_write=prepared,
                )
            except engine.ConflictBlocked:
                await session.rollback()
                return "blocked"
            await session.commit()
            return "saved"

    assert sorted(await asyncio.gather(create("one"), create("two"))) == [
        "blocked",
        "saved",
    ]
    async with factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(MealLog)
            .where(MealLog.subject_id == legacy_owner_roots.subject_id)
        ) == 1
