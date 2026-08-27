"""Health-profile MCP tool registration without a router dependency."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from vitals.config import load_config
from vitals.services.profile import health as health_profile_service


@dataclass(frozen=True)
class ProfileToolDependencies:
    get_session_factory: Callable[[], Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]


def register_profile_tool(server: Any, deps: ProfileToolDependencies):
    """Register the frozen subject profile tool."""

    @server.tool()
    async def get_user_profile() -> dict:
        """Returns the user's physical profile, active goals, and program overview.

        Every field here used to come from ``.env``, which describes the
        installation rather than a person — so this tool answered with the owner's
        body no matter whose record the caller was scoped to. It reads the subject's
        own row now, and the timezone comes from ``health_subjects`` for the same
        reason: a profile assembled from process-wide values is a profile about
        nobody.
        """
        async with deps.get_session_factory()() as session:
            scope = await deps.conflict_scope(session)
            projection = await health_profile_service.get_profile_projection(
                session,
                subject_id=scope.subject_id,
            )
        profile = projection.profile
        return {
            "height_cm": profile.height_cm,
            "sex": profile.sex,
            "age": profile.age,
            "timezone": str(projection.timezone or load_config().timezone),
            "goals": list(profile.goals),
            "program": profile.program,
        }

    return get_user_profile


__all__ = ["ProfileToolDependencies", "register_profile_tool"]
