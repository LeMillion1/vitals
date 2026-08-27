"""Proactive-state MCP adapter without a router dependency."""
from __future__ import annotations

from vitals.services.proactive.delivery import queries as delivery_queries

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from vitals.services.proactive import channels
from vitals.services.proactive.preferences import queries as preference_queries


@dataclass(frozen=True)
class ProactiveToolDependencies:
    get_session_factory: Callable[[], Any]
    actor_username: Callable[[Any], Awaitable[str]]
    serialize_row: Callable[[Any], dict]


@dataclass(frozen=True)
class RegisteredProactiveTools:
    get_proactive_state: Callable[..., Awaitable[dict]]


def register_proactive_tools(
    server: Any,
    deps: ProactiveToolDependencies,
) -> RegisteredProactiveTools:
    """Register proactive-state reporting at its frozen position."""

    @server.tool()
    async def get_proactive_state(limit: int = 10) -> dict:
        """Retrieves the state of the proactive layer: its settings (brief time, daily
        message budget, which nudge categories are allowed) and the last messages it
        actually sent. Read this before explaining why something did or didn't arrive.
        READ tool — the settings are read-only here; retiming or muting the layer is
        done in Settings, by the owner."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            ownership = await channels.resolve_legacy_channel_ownership(
                session,
                actor_username=await deps.actor_username(session),
            )
            preference_scope = await preference_queries.resolve_legacy_preferences_scope(
                session,
                actor_username=await deps.actor_username(session),
            )
            sent = list(
                reversed(
                    await delivery_queries.recent_sent(
                        session,
                        limit=limit,
                        ownership=ownership,
                    )
                )
            )
            return {
                "enabled": True,
                "prefs": (
                    await preference_queries.get_preferences_bundle(
                        session,
                        scope=preference_scope,
                        actor_username=await deps.actor_username(session),
                    )
                ).as_flat_dict(),
                "recent_notifications": [
                    deps.serialize_row(notification) for notification in sent
                ],
            }

    return RegisteredProactiveTools(get_proactive_state=get_proactive_state)


__all__ = [
    "ProactiveToolDependencies",
    "RegisteredProactiveTools",
    "register_proactive_tools",
]
