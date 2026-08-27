"""Resolve a verified MCP identity to one subject-scoped ownership context."""

from __future__ import annotations

from vitals.enums import IntegrationProvider
from vitals.services import identity_service
from vitals.services.legacy_ownership import (
    LegacyOwnershipContext,
    resolve_legacy_ownership_context,
    resolve_subject_ownership_context,
)
from web.config import get_web_config
from web.deps import get_session_factory
from web.mcp.errors import McpActorUnresolved
from web.mcp.identity import (
    ANONYMOUS_TOKEN,
    current_actor,
    current_grant_binding,
)


async def require_live_account(session, username: str) -> None:
    """Reject a token whose named account is no longer active."""

    async def _alive(active_session) -> bool:
        return await identity_service.is_active_username(
            active_session,
            username=username,
        )

    if session is None:
        async with get_session_factory()() as opened:
            live = await _alive(opened)
    else:
        live = await _alive(session)
    if not live:
        raise McpActorUnresolved(
            "this connector token names an account that is no longer active"
        )


async def actor_username(session=None) -> str:
    """Resolve the request actor, with exact-one legacy compatibility."""

    async def _configured_or_single_owner(active_session) -> str:
        config = get_web_config()
        if not config.oidc_enabled:
            return config.auth_username
        username = await identity_service.sole_active_subject_owner_username(
            active_session
        )
        if username is None:
            raise McpActorUnresolved(
                "this OIDC installation does not have exactly one active record "
                "owner for an unattributed legacy connector; reconnect it to mint "
                "a subject-bound token"
            )
        return username

    actor = current_actor()
    if actor is not None and actor != ANONYMOUS_TOKEN:
        await require_live_account(session, actor)
        return actor
    if actor is None:
        if session is None:
            async with get_session_factory()() as opened:
                return await _configured_or_single_owner(opened)
        return await _configured_or_single_owner(session)
    if session is None:
        async with get_session_factory()() as counting:
            multiple_subjects = await identity_service.installation_has_multiple_subjects(
                counting
            )
    else:
        multiple_subjects = await identity_service.installation_has_multiple_subjects(
            session
        )
    if multiple_subjects:
        raise McpActorUnresolved(
            "this connector token does not say whose record it is for, and this "
            "installation holds more than one. Reconnect the connector to mint a "
            "token that names its record."
        )
    if session is None:
        async with get_session_factory()() as opened:
            return await _configured_or_single_owner(opened)
    return await _configured_or_single_owner(session)


async def legacy_owner(session) -> LegacyOwnershipContext:
    """Resolve the subject and actor for one legacy MCP operation."""

    binding = current_grant_binding()
    if binding is None:
        return await resolve_legacy_ownership_context(
            session,
            actor_username=await actor_username(session),
        )

    ownership = await resolve_subject_ownership_context(
        session,
        subject_id=binding.subject_id,
    )
    from vitals.services.access_resolution import resolve_access_context

    access = await resolve_access_context(
        session,
        user_id=binding.user_id,
        subject_id=binding.subject_id,
    )
    return LegacyOwnershipContext(
        subject_id=ownership.subject_id,
        owner_user_id=ownership.owner_user_id,
        actor_user_id=binding.user_id,
        connection_ids=ownership.connection_ids,
        access=access,
    )


async def legacy_alert_owner(session) -> LegacyOwnershipContext:
    """Resolve every current provider root needed by the alert aggregate."""

    binding = current_grant_binding()
    if binding is None:
        return await resolve_legacy_ownership_context(
            session,
            actor_username=await actor_username(session),
            required_connections=tuple(IntegrationProvider),
        )
    ownership = await resolve_subject_ownership_context(
        session,
        subject_id=binding.subject_id,
        required_connections=tuple(IntegrationProvider),
    )
    return LegacyOwnershipContext(
        subject_id=ownership.subject_id,
        owner_user_id=ownership.owner_user_id,
        actor_user_id=binding.user_id,
        connection_ids=ownership.connection_ids,
        access=None,
    )


__all__ = [
    "actor_username",
    "legacy_alert_owner",
    "legacy_owner",
    "require_live_account",
]
