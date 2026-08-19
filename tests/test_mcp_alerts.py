"""MCP alert tools — Claude could see alerts but never close one, so every
conversation that dealt with an alert ended with "теперь нажми кнопку в приложении"."""
from __future__ import annotations

import pytest
from sqlalchemy import select

mcp_router = pytest.importorskip("web.routers.mcp")

from vitals.models.system_alert import SystemAlert  # noqa: E402
from vitals.models.tenancy import IntegrationConnection  # noqa: E402
from vitals.enums import (  # noqa: E402
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.services import alerts_service  # noqa: E402


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, monkeypatch, legacy_owner_roots):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)


async def _raise(db_session, alert_key="labs.out_of_range"):
    alert = await alerts_service.raise_alert(
        db_session,
        domain="labs",
        severity="warning",
        message="Калий низкий",
        alert_key=alert_key,
    )
    await db_session.commit()
    return alert.id


async def test_resolve_alert_removes_it_from_active(
    db_session, legacy_owner_roots
):
    alert_id = await _raise(db_session)
    assert [a["id"] for a in await mcp_router.get_active_alerts()] == [alert_id]

    resolved = await mcp_router.resolve_alert(alert_id)
    assert resolved["resolved_at"] is not None
    row = await db_session.get(SystemAlert, alert_id)
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.resolved_by_user_id == legacy_owner_roots.user_id
    assert await mcp_router.get_active_alerts() == []


async def test_override_alert_keeps_it_active(db_session, legacy_owner_roots):
    alert_id = await _raise(db_session)

    overridden = await mcp_router.override_alert(alert_id)
    assert overridden["override_at"] is not None
    row = await db_session.get(SystemAlert, alert_id)
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.overridden_by_user_id == legacy_owner_roots.user_id
    # Overriding is "noted, doing it anyway" — the alert stays visible.
    assert [a["id"] for a in await mcp_router.get_active_alerts()] == [alert_id]


async def test_alert_tools_report_a_missing_id():
    assert await mcp_router.resolve_alert(9999) == {"error": "Alert 9999 not found"}
    assert await mcp_router.override_alert(9999) == {"error": "Alert 9999 not found"}


async def test_alert_tools_include_provider_roots_but_exclude_platform(
    db_session,
    legacy_owner_roots,
):
    provider = await alerts_service.raise_alert(
        db_session,
        domain="garmin",
        severity="warning",
        message="Garmin authentication failed",
        alert_key="garmin.auth",
    )
    platform = await alerts_service.raise_alert(
        db_session,
        domain="system",
        severity="warning",
        message="Maintenance job failed",
        alert_key="scheduler.job_failed:share_purge",
    )
    await db_session.commit()

    assert [row["id"] for row in await mcp_router.get_active_alerts()] == [
        provider.id
    ]

    resolved = await mcp_router.resolve_alert(provider.id)
    assert resolved["resolved_at"] is not None
    await db_session.refresh(provider)
    connection_id = await db_session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.ACCOUNT.value,
        )
    )
    assert provider.subject_id == legacy_owner_roots.subject_id
    assert provider.integration_connection_id == connection_id
    assert provider.resolved_by_user_id == legacy_owner_roots.user_id

    assert await mcp_router.resolve_alert(platform.id) == {
        "error": f"Alert {platform.id} not found"
    }
    await db_session.refresh(platform)
    assert platform.resolved_at is None
