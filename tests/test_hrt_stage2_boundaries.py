"""Stage-2 ownership contracts at the HRT web and MCP boundaries."""

from __future__ import annotations

import asyncio
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import pytest

from vitals.enums import Source, UserStatus
from vitals.models.hrt import HrtCycle, HrtCycleItem, HrtDose, HrtSideEffect
from vitals.models.identity import HealthSubject, User
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, hrt_catalog, hrt_cycle_service, modules_service


mcp_router = pytest.importorskip("web.routers.mcp")


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)


@pytest.fixture(autouse=True)
async def _enable_hrt(session_factory, legacy_owner_roots):
    async with session_factory() as session:
        await modules_service.set_module_enabled(
            session,
            key="hrt",
            enabled=True,
            subject_id=legacy_owner_roots.subject_id,
        )
        await hrt_catalog.sync_catalog(session)
        await session.commit()


async def test_mcp_hrt_roots_and_children_stamp_owner_and_mcp_source(
    db_session,
    legacy_owner_roots,
):
    dose_payload = await mcp_router.log_hrt_dose(
        compound_key="testosterone_enanthate",
        dose=125,
        unit="mg",
        on_date="2026-08-20",
    )
    effect_payload = await mcp_router.log_hrt_side_effect(
        effect_type="acne",
        severity=2,
        on_date="2026-08-20",
    )
    cycle_payload = await mcp_router.add_hrt_cycle(
        kind="course",
        start_date="2026-08-20",
    )
    item_payload = await mcp_router.add_hrt_cycle_item(
        cycle_payload["id"],
        compound_key="testosterone_enanthate",
        dose=125,
        interval_days=3.5,
    )

    dose = await db_session.get(HrtDose, dose_payload["id"])
    effect = await db_session.get(HrtSideEffect, effect_payload["id"])
    cycle = await db_session.get(HrtCycle, cycle_payload["id"])
    item = await db_session.get(HrtCycleItem, item_payload["id"])

    expected_root = (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        Source.MCP.value,
    )
    assert (dose.subject_id, dose.actor_user_id, dose.source) == expected_root
    assert (effect.subject_id, effect.actor_user_id, effect.source) == expected_root
    assert (cycle.subject_id, cycle.actor_user_id, cycle.source) == expected_root
    assert item.subject_id == legacy_owner_roots.subject_id


async def test_web_hrt_creates_manual_owned_roots(
    auth_client,
    db_session,
    legacy_owner_roots,
):
    dose_response = await auth_client.post(
        "/hrt/dose",
        data={
            "date": "2026-08-20",
            "compound_key": "testosterone_enanthate",
            "dose": "125",
            "unit": "mg",
        },
    )
    cycle_response = await auth_client.post(
        "/hrt/cycle",
        data={"kind": "course", "start_date": "2026-08-20"},
    )
    assert dose_response.status_code == 303
    assert cycle_response.status_code == 303

    dose = await db_session.scalar(select(HrtDose).order_by(HrtDose.id.desc()))
    cycle = await db_session.scalar(select(HrtCycle).order_by(HrtCycle.id.desc()))
    expected_root = (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        Source.MANUAL.value,
    )
    assert (dose.subject_id, dose.actor_user_id, dose.source) == expected_root
    assert (cycle.subject_id, cycle.actor_user_id, cycle.source) == expected_root


async def test_mcp_delete_covers_hrt_side_effect(db_session):
    payload = await mcp_router.log_hrt_side_effect(
        effect_type="fatigue",
        severity=2,
        on_date="2026-08-20",
    )

    assert await mcp_router.delete_record("hrt_side_effect", payload["id"]) == {
        "deleted": True,
        "domain": "hrt_side_effect",
        "record_id": payload["id"],
    }
    assert await db_session.get(HrtSideEffect, payload["id"]) is None


async def test_a_second_subject_writes_for_the_actor_and_not_across(
    db_session, legacy_owner_roots
):
    """The write goes through, and it goes to the right person.

    Two refusals used to stack here. The legacy resolver rejected any
    installation holding a second subject; that went first, and it selects the
    actor's own record now. The conflict engine's fully-unowned bridge then kept
    refusing a layer lower, and the reasoning it was waiting for is done: the
    bridge widens to rows nobody owns, and with none of those in the database it
    widens to nothing. Demanding a sole subject for a write that asks nothing of
    it is what took this page down.

    What has to hold instead is the assertion below — the dose lands on the
    actor's own subject, and nowhere near the second person's.
    """

    before = await db_session.scalar(select(func.count()).select_from(HrtDose))
    user = User(
        username="second-hrt-owner",
        normalized_username="second-hrt-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    other_subject = HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty")
    db_session.add(other_subject)
    await db_session.commit()
    other_subject_id = other_subject.id

    payload = await mcp_router.log_hrt_dose(
        compound_key="testosterone_enanthate",
        dose=125,
        unit="mg",
        on_date="2026-08-20",
    )

    after = await db_session.scalar(select(func.count()).select_from(HrtDose))
    assert after == before + 1
    dose = await db_session.get(HrtDose, payload["id"])
    assert dose is not None
    assert dose.subject_id == legacy_owner_roots.subject_id
    assert dose.subject_id != other_subject_id


async def test_an_unowned_rule_still_closes_the_hrt_mutation(
    db_session, legacy_owner_roots
):
    """The refusal is kept for what it was written for.

    A rule that belongs to nobody and names nothing is legacy custom state, and
    with two people nothing can say whose state it was evaluated against. The
    write stops, and the way out is the conflict-rule ownership backfill, run
    while the installation is still one person.
    """

    from vitals.models.conflict_rule import ConflictRule

    before = await db_session.scalar(select(func.count()).select_from(HrtDose))
    user = User(
        username="second-hrt-owner",
        normalized_username="second-hrt-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty"))
    db_session.add(
        ConflictRule(
            subject_id=None,
            code=None,
            rule_type="soft_warn",
            severity="warn",
            domain_a="hrt",
            condition_a={"compound_key": "testosterone_enanthate"},
            domain_b="hrt",
            condition_b={"compound_key": "testosterone_enanthate"},
            message="legacy custom rule",
            active=True,
        )
    )
    await db_session.commit()

    with pytest.raises(
        conflict_engine.ConflictLegacyBridgeError, match="exactly one matching"
    ):
        await mcp_router.log_hrt_dose(
            compound_key="testosterone_enanthate",
            dose=125,
            unit="mg",
            on_date="2026-08-20",
        )

    after = await db_session.scalar(select(func.count()).select_from(HrtDose))
    assert after == before


@pytest.mark.integration
async def test_postgres_concurrent_open_cycles_leave_one_active(
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
    await db_session.commit()

    async def create(name: str) -> None:
        async with factory() as session:
            context = conflict_engine.ConflictWriteContext(
                identity=identity,
                evaluation_date=date(2026, 8, 20),
            )
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=context,
            )
            await hrt_cycle_service.add_cycle(
                session,
                kind="course",
                start_date=context.evaluation_date,
                name=name,
                source=Source.MCP.value,
                identity=identity,
                prepared_conflict_write=prepared,
            )
            await session.commit()

    await asyncio.wait_for(
        asyncio.gather(create("concurrent-a"), create("concurrent-b")),
        timeout=10,
    )

    async with factory() as verify:
        rows = list(
            await verify.scalars(
                select(HrtCycle)
                .where(HrtCycle.subject_id == identity.subject_id)
                .order_by(HrtCycle.id)
            )
        )
    assert len(rows) == 2
    assert sum(row.end_date is None for row in rows) == 1
    assert [row.end_date for row in rows if row.end_date is not None] == [
        date(2026, 8, 20)
    ]
