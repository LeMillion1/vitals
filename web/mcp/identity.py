"""Request-local identity projection for the MCP delivery boundary."""

from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass

from mcp.server.auth.middleware.auth_context import get_access_token

from vitals.access import AccessScope, PolicyAction, PolicyResourceType
from web.mcp.errors import McpActorUnresolved


# Direct in-process callers and tests can override request identity.  A real MCP
# request always gets its identity from the verified SDK access token.
MCP_ACTOR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitals_mcp_actor", default=None
)

ANONYMOUS_TOKEN = "\x00anonymous-connector-token"


@dataclass(frozen=True, slots=True)
class McpGrantBinding:
    """The already-verified patient and capabilities exposed by the MCP SDK."""

    user_id: uuid.UUID
    subject_id: uuid.UUID
    scopes: frozenset[AccessScope]


def current_grant_binding() -> McpGrantBinding | None:
    """Return the request grant, no request, or fail closed on malformed claims."""

    try:
        token = get_access_token()
    except Exception:  # pragma: no cover - no request context at all
        return None
    if token is None:
        return None
    claims = token.claims or {}
    try:
        user_id = uuid.UUID(str(claims["sub"]))
        subject_id = uuid.UUID(str(claims["health_subject"]))
        scopes = frozenset(
            AccessScope(
                resource_type=PolicyResourceType(resource_type),
                resource_key=resource_key,
                action=PolicyAction(action),
            )
            for value in token.scopes
            for resource_type, resource_key, action in [value.split(":", 2)]
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise McpActorUnresolved(
            "this connector token has no valid subject-scoped grant"
        ) from None
    if not scopes:
        raise McpActorUnresolved("this connector token grants no capabilities")
    return McpGrantBinding(
        user_id=user_id,
        subject_id=subject_id,
        scopes=scopes,
    )


def current_actor() -> str | None:
    """Return the named request actor, anonymous-token sentinel, or no request."""

    override = MCP_ACTOR.get()
    if override is not None:
        return override
    try:
        token = get_access_token()
    except Exception:  # pragma: no cover - no request context at all
        return None
    if token is None:
        return None
    named = token.subject or (token.claims or {}).get("username")
    return named if isinstance(named, str) and named else ANONYMOUS_TOKEN


__all__ = [
    "ANONYMOUS_TOKEN",
    "MCP_ACTOR",
    "McpGrantBinding",
    "current_actor",
    "current_grant_binding",
]
