"""Connector tokens that name what they are for, and can be taken back.

The token is still a signed value rather than an opaque handle, so the ordinary
path validates it without a database round trip for the signature. What this adds
is the half a signature cannot carry:

**Binding.** ``aud`` says which resource the token was minted for and ``iss``
says who minted it, both checked on every request. A token issued for one
installation replayed against another is refused by the audience rather than by
luck, and that is the whole point of an audience — the alternative is a
credential whose only scope is "some Vitals somewhere".

**Revocation.** ``jti`` names a row, and the row says whether it is still good.
Before this the only way to withdraw an issued connector token was rotating
``VITALS_SESSION_SECRET``, which also invalidates every web session: "disconnect
the laptop I lost" and "sign the whole household out" were one operation, so in
practice neither happened.

**Adoption.** A token minted before this table existed carries no ``jti`` at all.
It keeps working — breaking every live connector on upgrade would be its own
defect — and the first time it is presented, a row is recorded for it from the
signature's own timestamp. From that moment it is listable and revocable like
any other, and marked ``adopted`` so the person reading their connections can
see which predate the guarantee.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserStatus
from vitals.models.identity import McpAccessToken, User

TOKEN_TYPE = "mcp_access_token"

#: What ``/oauth/token`` advertises in ``expires_in`` and what the signature is
#: checked against. Long because the connector on the other side has no refresh
#: flow; safe to be long *because* it is now revocable, which is the trade this
#: module exists to make.
TOKEN_LIFETIME = timedelta(days=365)


class McpTokenError(RuntimeError):
    """Base for a refusal to mint or to withdraw."""


class TokenNotFound(McpTokenError):
    """No such connector, or not one this account may act on."""


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """A presented token, after everything about it has been checked."""

    jti: uuid.UUID
    user_id: uuid.UUID
    username: str
    client_id: str
    audience: str


def audience_for(public_url: str) -> str:
    """The resource identifier a token is minted for.

    ``/mcp`` rather than the bare origin: the audience names the protected
    resource, and this installation also serves a website, an external JSON API
    and an OAuth authorization server from the same origin. A token whose
    audience is the origin would be a token for all of them.
    """

    return f"{public_url.rstrip('/')}/mcp"


def _legacy_key(token: str) -> uuid.UUID:
    """A stable id for a token that was minted without one.

    Derived from the token itself so the same credential adopts the same row on
    every request, and derived by hash so the row does not become a copy of the
    secret — an operator reading this table still cannot use what they find.
    """

    digest = hashlib.sha256(f"legacy-mcp-token:{token}".encode("utf-8")).digest()
    return uuid.UUID(bytes=digest[:16], version=5)


async def _now(session: AsyncSession) -> datetime:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stamp = await session.scalar(select(func.clock_timestamp()))
        if stamp is not None:
            return (
                stamp
                if stamp.tzinfo is not None
                else stamp.replace(tzinfo=timezone.utc)
            )
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def issue(
    session: AsyncSession,
    *,
    username: str,
    client_id: str,
    audience: str,
    issuer: str,
    client_name: str | None = None,
    lifetime: timedelta = TOKEN_LIFETIME,
) -> tuple[dict[str, Any], McpAccessToken]:
    """Record a connector and return the payload to sign for it. Never commits.

    The payload is returned rather than signed here: signing belongs to
    ``web.auth``, which owns the serializer and its salt, and a service that
    reached for it would be a second place that knows how a token is made.
    """

    from vitals.services.identity_service import normalize_username

    lookup = normalize_username(username).lookup_key
    user = await session.scalar(
        select(User).where(
            User.normalized_username == lookup,
            User.status == UserStatus.ACTIVE.value,
        )
    )
    if user is None:
        raise McpTokenError("a connector token needs an active account behind it")

    now = await _now(session)
    record = McpAccessToken(
        user_id=user.id,
        client_id=client_id,
        client_name=client_name,
        audience=audience,
        issued_at=now,
        expires_at=now + lifetime,
    )
    session.add(record)
    await session.flush()

    payload = {
        "type": TOKEN_TYPE,
        # ``sub`` is the account's stable id; ``username`` stays beside it
        # because the subject seam resolves by name and a rename must not
        # silently repoint a live connector at somebody else's record.
        "sub": str(user.id),
        "username": user.username,
        "client_id": client_id,
        "aud": audience,
        "iss": issuer,
        "jti": str(record.id),
    }
    return payload, record


async def verify(
    session: AsyncSession,
    *,
    payload: dict[str, Any],
    token: str,
    expected_client_id: str,
    expected_audience: str,
    signed_at: datetime | None = None,
) -> VerifiedToken | None:
    """Everything about a presented token except its signature.

    The caller has already checked that. What is left is what a signature cannot
    say: that the token was minted for this resource, by this client, for an
    account that still exists, and that nobody has since taken it back.

    Returns ``None`` for every failure. A caller that could tell "revoked" from
    "wrong audience" from "suspended" would be handing a probe three answers.
    """

    if payload.get("type") != TOKEN_TYPE:
        return None
    if payload.get("client_id") != expected_client_id:
        return None

    username = payload.get("username")
    if not isinstance(username, str) or not username:
        # An identity-less token predates the subject seam as well as this
        # table. ``_mcp_actor_username`` decides what to do about that; here it
        # only has to be recognisable.
        username = ""

    audience = payload.get("aud")
    raw_jti = payload.get("jti")

    if audience is not None and audience != expected_audience:
        # Minted for a different resource. Refused by the audience rather than
        # by whether this installation happens to share a signing secret with
        # the one that issued it.
        return None

    now = await _now(session)

    if raw_jti is None:
        record = await _adopt(
            session,
            token=token,
            payload=payload,
            audience=expected_audience,
            username=username,
            signed_at=signed_at,
            now=now,
        )
        if record is None:
            return None
    else:
        try:
            jti = uuid.UUID(str(raw_jti))
        except (ValueError, AttributeError, TypeError):
            return None
        record = await session.get(McpAccessToken, jti)
        if record is None:
            # A signature this server made for a row that no longer exists is a
            # token from before a restore, or one whose row was deleted. Neither
            # is a credential.
            return None

    if record.revoked_at is not None:
        return None
    if _as_utc(record.expires_at) <= now:
        return None

    user = await session.get(User, record.user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        return None

    today = now.date()
    if record.last_used_on is None or _as_utc(record.last_used_on).date() != today:
        record.last_used_on = now

    return VerifiedToken(
        jti=record.id,
        user_id=record.user_id,
        username=username or user.username,
        client_id=record.client_id,
        audience=record.audience,
    )


async def _adopt(
    session: AsyncSession,
    *,
    token: str,
    payload: dict[str, Any],
    audience: str,
    username: str,
    signed_at: datetime | None,
    now: datetime,
) -> McpAccessToken | None:
    """Record a token minted before this table existed, on its first use.

    Its issue time comes from the signature, which carries one — so the row is
    truthful about when the connector was actually authorized rather than about
    when somebody first noticed it.
    """

    from vitals.services.identity_service import normalize_username

    key = _legacy_key(token)
    existing = await session.get(McpAccessToken, key)
    if existing is not None:
        return existing

    if not username:
        return None
    lookup = normalize_username(username).lookup_key
    user = await session.scalar(
        select(User).where(
            User.normalized_username == lookup,
            User.status == UserStatus.ACTIVE.value,
        )
    )
    if user is None:
        return None

    issued = _as_utc(signed_at) if signed_at else now
    record = McpAccessToken(
        id=key,
        user_id=user.id,
        client_id=str(payload.get("client_id") or "unknown"),
        audience=audience,
        issued_at=issued,
        expires_at=issued + TOKEN_LIFETIME,
        adopted=True,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        # Two concurrent first uses of the same old token. Whoever lost the race
        # reads the row the winner wrote.
        await session.rollback()
        return await session.get(McpAccessToken, key)
    return record


async def revoke(
    session: AsyncSession, *, user_id: uuid.UUID, jti: uuid.UUID
) -> McpAccessToken:
    """Disconnect one connector. Never commits.

    Marked rather than deleted, for the reason every other revocation in this
    codebase is: "this could read my record until March" is part of a history
    nothing else records.
    """

    record = await session.get(McpAccessToken, jti)
    if record is None or record.user_id != user_id:
        # Not "you may not touch this": somebody probing ids learns nothing.
        raise TokenNotFound("no such connector")
    if record.revoked_at is not None:
        raise McpTokenError("this connector is already disconnected")
    record.revoked_at = await _now(session)
    await session.flush()
    return record


async def list_for_user(
    session: AsyncSession, *, user_id: uuid.UUID
) -> Sequence[McpAccessToken]:
    """Every connector ever authorized by this account, newest first."""

    result = await session.execute(
        select(McpAccessToken)
        .where(McpAccessToken.user_id == user_id)
        .order_by(McpAccessToken.issued_at.desc())
    )
    return list(result.scalars().all())


def is_live(record: McpAccessToken, *, at: datetime) -> bool:
    return record.revoked_at is None and _as_utc(record.expires_at) > at


__all__ = [
    "McpTokenError",
    "TOKEN_LIFETIME",
    "TOKEN_TYPE",
    "TokenNotFound",
    "VerifiedToken",
    "audience_for",
    "is_live",
    "issue",
    "list_for_user",
    "revoke",
    "verify",
]
