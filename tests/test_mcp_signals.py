"""MCP signals tools — the domain that explains the other fourteen was invisible
to Claude until now. Same import-skip guard as the other MCP tool tests."""
from __future__ import annotations

import pytest

mcp_router = pytest.importorskip("web.routers.mcp")


async def test_log_and_get_signal(db_session, session_factory, signals_module_on, monkeypatch):
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


async def test_get_day_context(db_session, session_factory, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    from datetime import date

    from vitals.services import signals_service

    await signals_service.set_day_context(
        db_session, date(2026, 7, 20), answers={"remote": True, "gym": False}
    )
    await signals_service.set_day_context(
        db_session, date(2026, 7, 21), answers={"remote": False, "gym": True}
    )
    await db_session.commit()

    rows = await mcp_router.get_day_context()
    assert [r["date"] for r in rows] == ["2026-07-21", "2026-07-20"]
    assert rows[0]["answers"] == {"remote": False, "gym": True}

    ranged = await mcp_router.get_day_context(start_date="2026-07-21")
    assert len(ranged) == 1
