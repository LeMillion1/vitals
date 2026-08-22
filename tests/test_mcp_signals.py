"""MCP signals tools — the domain that explains the other fourteen was invisible
to Claude until now. Same import-skip guard as the other MCP tool tests."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from vitals.enums import Source, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import SubjectSetting
from vitals.models.signals import DayContext, Signal
from vitals.ownership import WriteIdentity
from vitals.services.legacy_ownership import LegacySubjectResolutionError

mcp_router = pytest.importorskip("web.routers.mcp")


@pytest.fixture(autouse=True)
async def _legacy_mcp_owner(legacy_owner_roots):
    """MCP v1 is attributed only after the sole owner roots exist."""


async def test_log_and_get_signal(
    db_session,
    session_factory,
    signals_module_on,
    legacy_owner_roots,
    monkeypatch,
):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    written = await mcp_router.log_signal(
        key="caffeine_late",
        kind="exposure",
        value_num=200,
        unit="mg",
        note="кофе в 22",
        at_time="22:00",
        on_date="2026-07-20",
    )
    assert written["id"] > 0
    assert written["key"] == "caffeine_late"
    assert written["at_time"] == "22:00:00"
    stored = await db_session.scalar(
        select(Signal).where(Signal.id == written["id"])
    )
    assert stored is not None
    assert (
        stored.subject_id,
        stored.actor_user_id,
        stored.integration_connection_id,
        stored.source,
    ) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        None,
        Source.MCP.value,
    )
    assert "subject_id" not in written
    assert "actor_user_id" not in written

    rows = await mcp_router.get_signals()
    assert [r["note"] for r in rows] == ["кофе в 22"]

    # Filters: kind, key (folding an alias in), and date range.
    assert await mcp_router.get_signals(kind="symptom") == []
    assert len(await mcp_router.get_signals(key="late_coffee")) == 1
    assert await mcp_router.get_signals(start_date="2026-07-21") == []


async def test_log_signal_rejects_bad_kind(
    db_session, session_factory, signals_module_on, monkeypatch
):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    result = await mcp_router.log_signal(key="headache", kind="mood")
    assert "error" in result
    assert await mcp_router.get_signals() == []


async def test_log_signal_refuses_when_module_disabled(db_session, session_factory, monkeypatch):
    """The module defaults off — a write must say so, not raise."""
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    result = await mcp_router.log_signal(key="headache", kind="symptom", value_num=4)
    assert result == {"error": "module 'signals' is disabled"}


async def test_get_day_context(
    db_session, session_factory, legacy_owner_roots, monkeypatch
):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    from datetime import date

    from vitals.services import signals_service

    await signals_service.set_day_context(
        db_session, date(2026, 7, 20), answers={"remote": True, "gym": False},
        identity=WriteIdentity(
            legacy_owner_roots.subject_id, legacy_owner_roots.user_id
        ),
    )
    await signals_service.set_day_context(
        db_session, date(2026, 7, 21), answers={"remote": False, "gym": True},
        identity=WriteIdentity(
            legacy_owner_roots.subject_id, legacy_owner_roots.user_id
        ),
    )
    await db_session.commit()

    rows = await mcp_router.get_day_context()
    assert [r["date"] for r in rows] == ["2026-07-21", "2026-07-20"]
    assert rows[0]["answers"] == {"remote": False, "gym": True}

    ranged = await mcp_router.get_day_context(start_date="2026-07-21")
    assert len(ranged) == 1


async def test_delete_signal(db_session, session_factory, signals_module_on, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    row = await mcp_router.log_signal(key="headache", kind="symptom", value_num=4)
    assert await mcp_router.delete_record("signals", row["id"]) == {
        "deleted": True, "domain": "signals", "record_id": row["id"],
    }
    assert await mcp_router.get_signals() == []


async def test_mark_signal_misparse(db_session, session_factory, signals_module_on, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    row = await mcp_router.log_signal(key="headache", kind="symptom", value_num=4)
    marked = await mcp_router.mark_signal_misparse(row["batch_id"])
    assert marked == {"marked": 1, "batch_id": row["batch_id"]}
    # The row survives — it just stops being read as a fact about the day.
    assert await mcp_router.get_signals() == []


async def test_log_day_context_keeps_the_template_guess(
    db_session,
    session_factory,
    signals_module_on,
    legacy_owner_roots,
    monkeypatch,
):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    from datetime import date

    from vitals.services import signals_service

    # The evening block parked its guess for that day before anything was said.
    await signals_service.set_day_context(
        db_session,
        date(2026, 7, 20),
        answers={},
        planned={"where": "office", "gym": False},
        source=Source.TEMPLATE.value,
        identity=WriteIdentity(
            legacy_owner_roots.subject_id, legacy_owner_roots.user_id
        ),
        merge_answers=True,
        preserve_source=True,
        planned_if_missing=True,
    )
    await db_session.commit()

    written = await mcp_router.log_day_context({"where": "remote", "gym": True}, on_date="2026-07-20")
    assert written["answers"] == {"where": "remote", "gym": True}
    assert written["planned"] == {"where": "office", "gym": False}
    stored = await db_session.scalar(
        select(DayContext).where(DayContext.date == date(2026, 7, 20))
    )
    assert stored is not None
    assert (
        stored.subject_id,
        stored.actor_user_id,
        stored.integration_connection_id,
        stored.source,
    ) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        None,
        Source.MCP.value,
    )

    rows = await mcp_router.get_day_context(start_date="2026-07-20", end_date="2026-07-20")
    assert rows[0]["answers"] == {"where": "remote", "gym": True}


async def test_log_day_context_rejects_an_unknown_answer(
    db_session, session_factory, signals_module_on, monkeypatch
):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    assert "error" in await mcp_router.log_day_context({"where": "beach"})
    assert "error" in await mcp_router.log_day_context({"mood": "good"})
    assert "error" in await mcp_router.log_day_context({})
    # Nothing half-applied.
    assert await mcp_router.get_day_context() == []


async def test_get_proactive_state(db_session, session_factory, signals_module_on, monkeypatch):
    """What the bot is set to do, and what it actually said — the half of the
    proactive layer Claude could only guess at."""
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    from vitals.models.proactive import Notification
    from vitals.services.proactive import day_plan
    from vitals.utils.timeutils import now_local

    db_session.add(
        Notification(
            sent_at=now_local(),
            category="brief",
            dedupe_key="brief:2026-07-20",
            channel="telegram",
            payload={"text": "Доброе утро"},
        )
    )
    await db_session.commit()

    state = await mcp_router.get_proactive_state()
    assert state["enabled"] is True
    assert state["prefs"]["daily_budget"] == 4
    assert set(state["week_template"]) == set(day_plan.WEEKDAYS)
    assert state["week_template"]["sat"]["where"] == "off"
    assert [n["dedupe_key"] for n in state["recent_notifications"]] == ["brief:2026-07-20"]


async def test_set_week_template_only_touches_the_days_it_names(
    db_session,
    session_factory,
    signals_module_on,
    legacy_owner_roots,
    monkeypatch,
):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    await mcp_router.set_week_template({"mon": {"where": "remote", "gym": True}})
    # Naming Tuesday must not reset Monday back to the neutral default, and giving
    # only ``gym`` must not reset that day's ``where``.
    stored = await mcp_router.set_week_template({"tue": {"gym": True}})

    assert stored["mon"] == {"where": "remote", "gym": True}
    assert stored["tue"]["gym"] is True
    assert stored["tue"]["where"] == "office"
    assert stored["sat"]["where"] == "off"  # untouched default

    scoped = await db_session.get(
        SubjectSetting,
        {
            "subject_id": legacy_owner_roots.subject_id,
            "key": "week_template",
        },
    )
    assert scoped is not None and scoped.value == stored

    state = await mcp_router.get_proactive_state()
    assert state["week_template"]["mon"]["where"] == "remote"


async def test_set_week_template_rejects_junk(
    db_session, session_factory, signals_module_on, monkeypatch
):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    assert "error" in await mcp_router.set_week_template({})
    assert "error" in await mcp_router.set_week_template({"monday": {"gym": True}})
    assert "error" in await mcp_router.set_week_template({"mon": "remote"})
    assert "error" in await mcp_router.set_week_template({"mon": {"gym": "false"}})
    assert "error" in await mcp_router.set_week_template({"mon": {"where": "moon"}})
    assert "error" in await mcp_router.set_week_template({"mon": {"load": "heavy"}})
    # Nothing half-applied: the stored template is still the neutral default.
    state = await mcp_router.get_proactive_state()
    assert state["week_template"]["mon"]["where"] == "office"


async def test_mcp_v1_signals_fail_closed_when_a_second_subject_exists(
    db_session,
    session_factory,
    signals_module_on,
    monkeypatch,
):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    second_user = User(
        username="second-mcp-subject",
        normalized_username="second-mcp-subject",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second_user)
    await db_session.flush()
    db_session.add(
        HealthSubject(
            owner_user_id=second_user.id,
            timezone="UTC",
        )
    )
    await db_session.flush()

    operations = (
        lambda: mcp_router.get_signals(),
        lambda: mcp_router.log_signal(key="headache", kind="symptom"),
        lambda: mcp_router.mark_signal_misparse("forged-batch"),
        lambda: mcp_router.get_day_context(),
        lambda: mcp_router.log_day_context({"gym": True}),
        lambda: mcp_router.delete_record("signals", 1),
    )
    for operation in operations:
        with pytest.raises(LegacySubjectResolutionError):
            await operation()
