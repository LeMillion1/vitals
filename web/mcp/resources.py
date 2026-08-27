"""MCP resources and prompt registration."""

from __future__ import annotations

from vitals.services.digest import ownership as digest_ownership
from vitals.services.digest import queries as digest_queries

from dataclasses import dataclass
from typing import Any, Awaitable, Callable



@dataclass(frozen=True)
class ResourceDependencies:
    get_session_factory: Callable[[], Any]
    actor_username: Callable[..., Awaitable[str]]
    get_user_profile: Callable[[], Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredResources:
    profile_resource: Callable[..., Awaitable[dict]]
    latest_digest_resource: Callable[..., Awaitable[dict]]
    weekly_review: Callable[..., Awaitable[str]]


def register_resources(server: Any, deps: ResourceDependencies) -> RegisteredResources:
    """Register the two frozen resources and weekly-review prompt once."""

    @server.resource("vitals://profile")
    async def profile_resource() -> dict:
        """The user's physical profile, goals, and program — attachable as lightweight
        context without spending a tool call."""
        return await deps.get_user_profile()

    @server.resource("vitals://digest/latest")
    async def latest_digest_resource() -> dict:
        """The most recent weekly AI digest (narrative + date) for conversation
        continuity."""
        async with deps.get_session_factory()() as session:
            owner = await digest_ownership.prepare_digest_owner(
                session,
                actor_username=await deps.actor_username(session),
            )
            row = await digest_queries.latest_digest(
                session,
                prepared_owner=owner,
            )
            if row is None:
                return {"error": "No digests yet"}
            return {
                "date": row.date.isoformat(),
                "content": row.content,
                "model": row.model,
            }

    @server.prompt()
    async def weekly_review() -> str:
        """A ready-made prompt that drives a full cross-domain weekly review."""
        return (
            "Review my last 7 days across every domain. First call get_full_snapshot "
            "for the aligned cross-domain picture (weight trend, GLP-1 state, recent "
            "labs, activity/recovery, workouts, nutrition, skincare, goals). Then pull "
            "get_trend for weight and any lab marker that looks off. Summarize what "
            "changed, call out cross-domain correlations (e.g. sleep vs training load, "
            "dose changes vs side effects), surface anything from get_active_alerts, and "
            "give at most three concrete, non-alarmist suggestions. This is decision "
            "support, not medical advice."
        )

    return RegisteredResources(
        profile_resource=profile_resource,
        latest_digest_resource=latest_digest_resource,
        weekly_review=weekly_review,
    )


__all__ = ["RegisteredResources", "ResourceDependencies", "register_resources"]
