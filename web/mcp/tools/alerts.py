"""Safety-alert MCP tools without router or ORM dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from vitals.services import legacy_subject_alerts


@dataclass(frozen=True)
class AlertToolDependencies:
    get_session_factory: Callable[[], Any]
    legacy_alert_owner: Callable[[Any], Awaitable[Any]]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredAlertTools:
    get_active_alerts: Callable[..., Awaitable[list[dict]]]
    resolve_alert: Callable[..., Awaitable[dict]]
    override_alert: Callable[..., Awaitable[dict]]


def register_alert_tools(
    server: Any,
    deps: AlertToolDependencies,
) -> RegisteredAlertTools:
    """Register the frozen safety-alert surface in its existing order."""

    @server.tool()
    async def get_active_alerts() -> list[dict]:
        """Returns currently active warning alerts and conflict notifications."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            ownership = await deps.legacy_alert_owner(session)
            alerts = await legacy_subject_alerts.list_active(
                session,
                ownership=ownership,
            )
            return [deps.serialize_row(alert) for alert in alerts]

    @server.tool()
    async def resolve_alert(alert_id: int) -> dict:
        """Marks one alert resolved — it disappears from ``get_active_alerts`` and
        from the dashboard. Use it once the thing the alert is about has actually been
        dealt with in the conversation, so the discussion and the closing are the same
        step instead of leaving the owner a button to press afterwards. WRITE tool."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            ownership = await deps.legacy_alert_owner(session)
            row = await legacy_subject_alerts.resolve(
                session,
                alert_id,
                ownership=ownership,
            )
            if row is None:
                return {"error": f"Alert {alert_id} not found"}
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    async def override_alert(alert_id: int) -> dict:
        """Marks a blocking alert overridden — "noted, doing it anyway". The alert
        stays active and visible; only the block it represents stops being treated as
        unanswered. For resolving it instead, use ``resolve_alert``. WRITE tool."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            ownership = await deps.legacy_alert_owner(session)
            row = await legacy_subject_alerts.override(
                session,
                alert_id,
                ownership=ownership,
            )
            if row is None:
                return {"error": f"Alert {alert_id} not found"}
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredAlertTools(
        get_active_alerts=get_active_alerts,
        resolve_alert=resolve_alert,
        override_alert=override_alert,
    )


__all__ = ["AlertToolDependencies", "RegisteredAlertTools", "register_alert_tools"]
