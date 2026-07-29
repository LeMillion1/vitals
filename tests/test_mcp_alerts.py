"""MCP alert tools — Claude could see alerts but never close one, so every
conversation that dealt with an alert ended with "теперь нажми кнопку в приложении"."""
from __future__ import annotations

import pytest

mcp_router = pytest.importorskip("web.routers.mcp")

from vitals.services import alerts_service  # noqa: E402


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)


async def _raise(db_session, alert_key="test.key"):
    alert = await alerts_service.raise_alert(
        db_session,
        domain="labs",
        severity="warning",
        message="Калий низкий",
        alert_key=alert_key,
    )
    await db_session.commit()
    return alert.id


async def test_resolve_alert_removes_it_from_active(db_session):
    alert_id = await _raise(db_session)
    assert [a["id"] for a in await mcp_router.get_active_alerts()] == [alert_id]

    resolved = await mcp_router.resolve_alert(alert_id)
    assert resolved["resolved_at"] is not None
    assert await mcp_router.get_active_alerts() == []


async def test_override_alert_keeps_it_active(db_session):
    alert_id = await _raise(db_session)

    overridden = await mcp_router.override_alert(alert_id)
    assert overridden["override_at"] is not None
    # Overriding is "noted, doing it anyway" — the alert stays visible.
    assert [a["id"] for a in await mcp_router.get_active_alerts()] == [alert_id]


async def test_alert_tools_report_a_missing_id():
    assert await mcp_router.resolve_alert(9999) == {"error": "Alert 9999 not found"}
    assert await mcp_router.override_alert(9999) == {"error": "Alert 9999 not found"}
