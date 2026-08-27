"""Labs writes go through the conflict gate, and a result can be corrected.

Labs was the last write path reading the conflict engine in a passive, alert-only
way, while the catalog carries a hard rule for it (an active potassium supplement
plus a hyperkalemic potassium result). And a mistyped value could only be fixed by
deleting the row and re-adding it — the one thing this project promises not to do
to a measurement.
"""
from __future__ import annotations

from datetime import date

import pytest

from vitals.models.conflict_rule import ConflictRule
from vitals.services import labs_service, supplements_service
from vitals.services.conflicts import registrations
from vitals.services.conflicts.engine import ConflictBlocked

mcp_router = pytest.importorskip("web.routers.mcp")

DAY = date(2026, 7, 1)


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, monkeypatch, legacy_owner_roots):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)


async def _potassium_hard_rule(session, owner_write):
    """The catalog's real lab_safety block, added directly so the test doesn't
    depend on the whole rule catalog being synced."""
    registrations.register_all_resolvers()
    session.add(
        ConflictRule(
            rule_type="hard_block",
            domain_a="supplements", condition_a={"key": "potassium", "active": True},
            domain_b="labs", condition_b={"marker": "Калий", "value": {"$gt": 5.0}},
            severity="block",
            message="Калий в крови высокий на фоне добавки калия",
            active=True,
        )
    )
    await supplements_service.add_supplement(session, name="Калий", key="potassium", identity=owner_write.identity, prepared_conflict_write=await owner_write.write())
    await session.commit()


# ── The gate ──────────────────────────────────────────────────────────────────
async def test_add_result_blocks_on_a_hard_rule(db_session, owner_write):
    await _potassium_hard_rule(db_session, owner_write)

    with pytest.raises(ConflictBlocked):
        await labs_service.add_result(
            db_session,
            on_date=DAY,
            marker="Калий",
            value=5.5,
            ref_low=3.5,
            ref_high=5.1,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(DAY),
        )


async def test_add_result_saves_with_override(db_session, owner_write):
    await _potassium_hard_rule(db_session, owner_write)

    row = await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="Калий",
        value=5.5,
        ref_low=3.5,
        ref_high=5.1,
        override=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    assert row.id is not None
    assert row.flag == "high"


async def test_add_result_still_saves_when_nothing_conflicts(db_session, owner_write):
    row = await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="Калий",
        value=4.2,
        ref_low=3.5,
        ref_high=5.1,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    assert row.flag == "normal"


async def test_log_lab_result_tool_reports_the_block(session_factory, owner_write):
    async with session_factory() as session:
        await _potassium_hard_rule(session, owner_write)

    blocked = await mcp_router.log_lab_result(
        marker="Калий", value=5.5, on_date="2026-07-01", ref_low=3.5, ref_high=5.1
    )
    assert blocked["blocked"] is True
    assert blocked["violations"]

    saved = await mcp_router.log_lab_result(
        marker="Калий", value=5.5, on_date="2026-07-01",
        ref_low=3.5, ref_high=5.1, override=True,
    )
    assert saved["value"] == 5.5


async def test_log_lab_results_batch_reports_the_block(session_factory, owner_write):
    async with session_factory() as session:
        await _potassium_hard_rule(session, owner_write)

    blocked = await mcp_router.log_lab_results(
        results=[{"marker": "Калий", "value": 5.5, "ref_low": 3.5, "ref_high": 5.1}],
        on_date="2026-07-01",
    )
    assert blocked["blocked"] is True


# ── Editing a result ──────────────────────────────────────────────────────────
async def test_update_result_recomputes_the_flag(db_session, owner_write):
    row = await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="Ферритин",
        value=700,
        ref_low=30,
        ref_high=400,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await db_session.commit()
    assert row.flag == "critical_high"

    updated = await labs_service.update_result(
        db_session,
        row.id,
        value=200,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    assert updated.value == 200
    assert updated.flag == "normal"
    # Untouched fields survive the edit.
    assert updated.date == DAY
    assert (updated.ref_low, updated.ref_high) == (30, 400)


async def test_update_result_refreshes_alerts(db_session, owner_write):
    from vitals.services import alerts_service

    row = await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="Ферритин",
        value=700,
        ref_low=30,
        ref_high=400,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await labs_service.refresh_alerts(
        db_session,
        on_date=DAY,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()
    active = await alerts_service.list_active(db_session, subject_id=owner_write.subject_id)
    assert any(a.alert_key == labs_service.OUT_OF_RANGE_KEY for a in active)

    await labs_service.update_result(
        db_session,
        row.id,
        value=200,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await db_session.commit()

    active = await alerts_service.list_active(db_session, subject_id=owner_write.subject_id)
    assert not any(a.alert_key == labs_service.OUT_OF_RANGE_KEY for a in active)


async def test_update_lab_result_tool(session_factory):
    created = await mcp_router.log_lab_result(
        marker="Ферритин", value=700, on_date="2026-07-01", ref_low=30, ref_high=400,
        note="из отчёта",
    )

    updated = await mcp_router.update_lab_result(created["id"], value=200)
    assert updated["value"] == 200
    assert updated["flag"] == "normal"
    assert updated["date"] == "2026-07-01"
    assert updated["note"] == "из отчёта"

    assert await mcp_router.update_lab_result(9999, value=1) == {
        "error": "Lab result 9999 not found"
    }


async def test_update_lab_result_rejects_an_implausible_value(session_factory):
    created = await mcp_router.log_lab_result(marker="Ферритин", value=100, on_date="2026-07-01")
    result = await mcp_router.update_lab_result(created["id"], value=1e12)
    assert "error" in result
