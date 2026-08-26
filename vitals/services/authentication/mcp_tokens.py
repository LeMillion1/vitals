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
from collections.abc import Iterable
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import AccessScope, PolicyAction, PolicyResourceType
from vitals.enums import Domain, UserStatus
from vitals.models.identity import HealthSubject, McpAccessToken, McpAccessTokenScope, User

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
    subject_id: uuid.UUID
    relationship_id: uuid.UUID | None
    consent_grant_id: uuid.UUID | None
    consent_version: int | None
    scopes: frozenset[AccessScope]


_OWNER_DOMAIN_ACTIONS = (
    PolicyAction.READ,
    PolicyAction.LIST,
    PolicyAction.SEARCH,
    PolicyAction.CREATE,
    PolicyAction.UPDATE,
    PolicyAction.DELETE,
)


def owner_scopes() -> frozenset[AccessScope]:
    """The concrete capabilities offered to an owner on today's MCP surface.

    Materialized into each token at issuance. Adding a domain later does not
    widen an already-issued credential, even though a newly approved connector
    can receive the new domain.
    """

    domains = frozenset(
        AccessScope(
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=domain.value,
            action=action,
        )
        for domain in Domain
        if domain is not Domain.SYSTEM
        for action in _OWNER_DOMAIN_ACTIONS
    )
    surfaces = frozenset(
        {
            AccessScope(PolicyResourceType.ARTIFACT, "health_profile", PolicyAction.READ),
            AccessScope(PolicyResourceType.ARTIFACT, "weekly_digest", PolicyAction.READ),
            AccessScope(PolicyResourceType.ARTIFACT, "weekly_digest", PolicyAction.LIST),
            AccessScope(PolicyResourceType.ARTIFACT, "weekly_digest", PolicyAction.CREATE),
            AccessScope(PolicyResourceType.ARTIFACT, "safety_alert", PolicyAction.READ),
            AccessScope(PolicyResourceType.ARTIFACT, "safety_alert", PolicyAction.UPDATE),
            AccessScope(PolicyResourceType.OPERATION, "conflict.check", PolicyAction.READ),
            AccessScope(PolicyResourceType.OPERATION, "modules", PolicyAction.READ),
            AccessScope(PolicyResourceType.OPERATION, "modules", PolicyAction.UPDATE),
            AccessScope(PolicyResourceType.OPERATION, "proactive", PolicyAction.READ),
            AccessScope(PolicyResourceType.OPERATION, "record.export", PolicyAction.EXPORT),
            AccessScope(PolicyResourceType.OPERATION, "garmin.sync", PolicyAction.SYNC),
            AccessScope(PolicyResourceType.OPERATION, "hevy.sync", PolicyAction.SYNC),
        }
    )
    return domains | surfaces


def _scope_claim(scope: AccessScope) -> str:
    return f"{scope.resource_type.value}:{scope.resource_key}:{scope.action.value}"


def _claims(scopes: Iterable[AccessScope]) -> list[str]:
    return sorted(_scope_claim(scope) for scope in scopes)


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
    subject_id: uuid.UUID,
    scopes: Iterable[AccessScope] | None = None,
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

    from vitals.services.access_resolution import resolve_access_context

    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise McpTokenError("a connector token must name one health subject")
    from vitals.persistence.rls import bind_session_subject

    # Binding narrows the reads below; it grants nothing. The access context is
    # still what proves whether this account owns or has consent for the record.
    await bind_session_subject(session, subject_id)
    context = await resolve_access_context(
        session,
        user_id=user.id,
        subject_id=subject_id,
    )
    relationship = None
    if context.subject_owner_user_id == user.id:
        allowed_scopes = owner_scopes()
    else:
        relationship = context.relationship_grant
        if relationship is None or not relationship.active:
            raise McpTokenError(
                "cross-subject connector access needs one active care relationship"
            )
        allowed_scopes = relationship.scopes

    try:
        granted_scopes = frozenset(allowed_scopes if scopes is None else scopes)
    except TypeError as exc:
        raise McpTokenError("connector scopes must be an iterable") from exc
    if not granted_scopes or any(
        not isinstance(scope, AccessScope) for scope in granted_scopes
    ):
        raise McpTokenError("a connector token needs at least one exact scope")
    if not granted_scopes.issubset(allowed_scopes):
        raise McpTokenError("requested connector scopes exceed current consent")

    now = await _now(session)
    expires_at = now + lifetime
    if relationship is not None:
        expires_at = min(expires_at, relationship.expires_at)
        if expires_at <= now:
            raise McpTokenError("the selected consent has expired")
    record = McpAccessToken(
        user_id=user.id,
        subject_id=subject_id,
        relationship_id=relationship.relationship_id if relationship else None,
        consent_grant_id=relationship.consent_grant_id if relationship else None,
        consent_version=relationship.consent_version if relationship else None,
        client_id=client_id,
        client_name=client_name,
        audience=audience,
        issued_at=now,
        expires_at=expires_at,
    )
    record.scopes = [
        McpAccessTokenScope(
            subject_id=subject_id,
            resource_type=scope.resource_type.value,
            resource_key=scope.resource_key,
            action=scope.action.value,
        )
        for scope in sorted(
            granted_scopes,
            key=lambda value: (
                value.resource_type.value,
                value.resource_key,
                value.action.value,
            ),
        )
    ]
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
        "health_subject": str(subject_id),
        "relationship": str(relationship.relationship_id) if relationship else None,
        "consent_grant": str(relationship.consent_grant_id) if relationship else None,
        "consent_version": relationship.consent_version if relationship else None,
        "scopes": _claims(granted_scopes),
    }
    return payload, record


async def verify(
    session: AsyncSession,
    *,
    payload: dict[str, Any],
    token: str,
    expected_client_id: str,
    expected_audience: str,
    expected_issuer: str,
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
    claimed_client_id = payload.get("client_id")
    if not isinstance(claimed_client_id, str) or not claimed_client_id:
        return None

    # The configured identifier is the legacy confidential connector. Client
    # ID Metadata Documents add a second, public shape whose HTTPS URL is the
    # identifier. Registry-backed tokens for that shape are accepted below only
    # when the signed claim exactly matches the durable row created at issuance.
    # An old token without a registry row can only be the configured client:
    # metadata clients have never existed without a ``jti`` binding.
    from vitals.services.authentication import oauth_clients

    is_metadata_client = oauth_clients.looks_like_a_metadata_url(claimed_client_id)
    if claimed_client_id != expected_client_id and not is_metadata_client:
        return None

    username = payload.get("username")
    if not isinstance(username, str) or not username:
        # An identity-less token predates the subject seam as well as this
        # table. ``_mcp_actor_username`` decides what to do about that; here it
        # only has to be recognisable.
        username = ""

    audience = payload.get("aud")
    issuer = payload.get("iss")
    raw_jti = payload.get("jti")

    if raw_jti is None and claimed_client_id != expected_client_id:
        return None

    if raw_jti is None:
        # Pre-registry credentials had neither binding claim. Preserve that
        # documented adoption path, but never adopt a token that does name a
        # different installation or resource.
        if audience is not None and audience != expected_audience:
            return None
        if issuer is not None and issuer != expected_issuer:
            return None
    elif audience != expected_audience or issuer != expected_issuer:
        # Registry-backed tokens are issued with both claims. Missing is not a
        # legacy shape here: accepting it would let a valid signed payload shed
        # precisely the installation binding these claims exist to provide.
        return None

    now = await _now(session)

    if raw_jti is not None:
        try:
            claimed_subject = uuid.UUID(str(payload.get("health_subject")))
        except (ValueError, AttributeError, TypeError):
            return None
        from vitals.persistence.rls import RlsSessionError, bind_session_subject

        try:
            await bind_session_subject(session, claimed_subject)
        except RlsSessionError:
            return None

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
        record = await session.scalar(
            select(McpAccessToken)
            .options(selectinload(McpAccessToken.scopes))
            .where(McpAccessToken.id == jti)
        )
        if record is None:
            # A signature this server made for a row that no longer exists is a
            # token from before a restore, or one whose row was deleted. Neither
            # is a credential.
            return None

    # For a registry-backed credential the row, not the current installation
    # setting, is the client registration it was actually issued under. This is
    # what lets metadata-URL clients work while preventing a signed payload from
    # changing one URL into another (or into the static client) after issuance.
    if record.client_id != claimed_client_id:
        return None

    if record.revoked_at is not None:
        return None
    if _as_utc(record.expires_at) <= now:
        return None

    user = await session.get(User, record.user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        return None

    if record.subject_id is None:
        return None

    persisted_scopes = frozenset(
        AccessScope(
            resource_type=PolicyResourceType(scope.resource_type),
            resource_key=scope.resource_key,
            action=PolicyAction(scope.action),
        )
        for scope in record.scopes
    )
    if not persisted_scopes:
        return None

    if raw_jti is not None:
        if payload.get("sub") != str(record.user_id):
            return None
        if payload.get("health_subject") != str(record.subject_id):
            return None
        expected_relationship = (
            str(record.relationship_id) if record.relationship_id else None
        )
        expected_consent = (
            str(record.consent_grant_id) if record.consent_grant_id else None
        )
        if payload.get("relationship") != expected_relationship:
            return None
        if payload.get("consent_grant") != expected_consent:
            return None
        if payload.get("consent_version") != record.consent_version:
            return None
        if payload.get("scopes") != _claims(persisted_scopes):
            return None

    if record.relationship_id is None:
        owner_id = await session.scalar(
            select(HealthSubject.owner_user_id).where(
                HealthSubject.id == record.subject_id
            )
        )
        if owner_id != record.user_id:
            return None
    else:
        from vitals.services.access_resolution import (
            AccessResolutionError,
            resolve_access_context,
        )

        try:
            context = await resolve_access_context(
                session,
                user_id=record.user_id,
                subject_id=record.subject_id,
                evaluated_at=now,
            )
        except AccessResolutionError:
            return None
        relationship = context.relationship_grant
        if (
            relationship is None
            or not relationship.active
            or relationship.relationship_id != record.relationship_id
            or relationship.consent_grant_id != record.consent_grant_id
            or relationship.consent_version != record.consent_version
            or not persisted_scopes.issubset(relationship.scopes)
        ):
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
        subject_id=record.subject_id,
        relationship_id=record.relationship_id,
        consent_grant_id=record.consent_grant_id,
        consent_version=record.consent_version,
        scopes=persisted_scopes,
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

    subject_id = await session.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == user.id)
    )
    if subject_id is None:
        return None

    from vitals.persistence.rls import bind_session_subject

    await bind_session_subject(session, subject_id)
    existing = await session.scalar(
        select(McpAccessToken)
        .options(selectinload(McpAccessToken.scopes))
        .where(
            McpAccessToken.id == key,
            McpAccessToken.subject_id == subject_id,
        )
    )
    if existing is not None:
        return existing
    granted_scopes = owner_scopes()

    issued = _as_utc(signed_at) if signed_at else now
    record = McpAccessToken(
        id=key,
        user_id=user.id,
        subject_id=subject_id,
        client_id=str(payload.get("client_id") or "unknown"),
        audience=audience,
        issued_at=issued,
        expires_at=issued + TOKEN_LIFETIME,
        adopted=True,
    )
    record.scopes = [
        McpAccessTokenScope(
            subject_id=subject_id,
            resource_type=scope.resource_type.value,
            resource_key=scope.resource_key,
            action=scope.action.value,
        )
        for scope in granted_scopes
    ]
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        # Two concurrent first uses of the same old token. Whoever lost the race
        # reads the row the winner wrote.
        await session.rollback()
        return await session.scalar(
            select(McpAccessToken)
            .options(selectinload(McpAccessToken.scopes))
            .where(
                McpAccessToken.id == key,
                McpAccessToken.subject_id == subject_id,
            )
        )
    return record


async def revoke(
    session: AsyncSession, *, user_id: uuid.UUID, jti: uuid.UUID
) -> McpAccessToken:
    """Disconnect one connector. Never commits.

    Marked rather than deleted, for the reason every other revocation in this
    codebase is: "this could read my record until March" is part of a history
    nothing else records.
    """

    record = await session.scalar(
        select(McpAccessToken).where(
            McpAccessToken.id == jti,
            McpAccessToken.user_id == user_id,
        )
    )
    if record is None or record.user_id != user_id:
        # Not "you may not touch this": somebody probing ids learns nothing.
        raise TokenNotFound("no such connector")
    if record.revoked_at is not None:
        raise McpTokenError("this connector is already disconnected")
    record.revoked_at = await _now(session)
    await session.flush()
    return record


async def revoke_all_live(session: AsyncSession) -> int:
    """Disconnect every still-usable connector after authority rotation.

    Installation-wide client configuration is a trust root rather than a
    personal preference.  Changing its client identifier makes every token
    minted for the previous identifier unverifiable, so their durable rows must
    stop claiming that those connectors remain active.  Rows stay in history
    and the caller owns the surrounding transaction.
    """

    at = await _now(session)
    rows = list(
        (
            await session.scalars(
                select(McpAccessToken).where(
                    McpAccessToken.revoked_at.is_(None),
                    McpAccessToken.expires_at > at,
                )
            )
        ).all()
    )
    for row in rows:
        row.revoked_at = at
    await session.flush()
    return len(rows)


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
    "owner_scopes",
    "revoke",
    "revoke_all_live",
    "verify",
]
