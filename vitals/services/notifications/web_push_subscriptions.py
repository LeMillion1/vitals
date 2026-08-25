"""Validate and store browser push subscriptions without exposing endpoints.

Push endpoints are bearer-like delivery addresses and their ``auth``/``p256dh``
values are encryption material.  They are therefore encrypted at rest and are
never returned by listing APIs or logs.  The clear SHA-256 digest exists only so
the same browser can update or revoke its own row.

The endpoint host allowlist is also a server-side request-forgery boundary.  A
browser posts the endpoint, but the server later performs a POST to it; accepting
an arbitrary HTTPS URL would let an authenticated account make the deployment
contact internal services.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserStatus
from vitals.integrations.web_push import (
    MAX_ENDPOINT_LENGTH,
    MAX_KEY_LENGTH,
    InvalidWebPushTarget,
    WebPushTarget,
    canonical_endpoint,
    validate_target,
)
from vitals.models.identity import User
from vitals.models.web_push import WebPushSubscription
from vitals.services import credential_vault_service
from vitals.utils.timeutils import now_utc

MAX_ACTIVE_SUBSCRIPTIONS_PER_USER = 10


class WebPushSubscriptionError(RuntimeError):
    """Base class for a subscription that cannot be stored or read safely."""


class InvalidWebPushSubscription(ValueError, WebPushSubscriptionError):
    """Client-supplied subscription data is malformed or unsafe."""


class SubscriptionBelongsToAnotherAccount(WebPushSubscriptionError):
    """The same browser endpoint is already bound to another local account."""


class TooManyWebPushSubscriptions(WebPushSubscriptionError):
    """An account reached the bounded active-device allowance."""


class CorruptWebPushSubscription(WebPushSubscriptionError):
    """Stored subscription ciphertext is not the authenticated value written."""


SubscriptionSecret = WebPushTarget


@dataclass(frozen=True, slots=True)
class SubscriptionGeneration:
    """Opaque identity of one encrypted credential generation."""

    key_version: int = field(repr=False)
    ciphertext_fingerprint: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class DispatchSubscription:
    """One decrypted target plus its opaque generation.

    Re-enrolling the same browser produces fresh authenticated ciphertext.  The
    fingerprint lets finalization avoid applying a late provider result to
    credentials that were refreshed after the claim committed.
    """

    target: SubscriptionSecret = field(repr=False)
    generation: SubscriptionGeneration = field(repr=False)


def _require_uuid(value: Any, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise InvalidWebPushSubscription(f"{field} must be a non-zero UUID")
    return value


def _canonical_endpoint(value: Any) -> str:
    try:
        return canonical_endpoint(value)
    except InvalidWebPushTarget as exc:
        raise InvalidWebPushSubscription(str(exc)) from exc


def validate_subscription(
    *, endpoint: Any, p256dh: Any, auth: Any
) -> SubscriptionSecret:
    try:
        return validate_target(
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
        )
    except InvalidWebPushTarget as exc:
        raise InvalidWebPushSubscription(str(exc)) from exc


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _encrypt(secret: SubscriptionSecret) -> bytes:
    return credential_vault_service.encrypt_mapping(
        {
            "endpoint": secret.endpoint,
            "p256dh": secret.p256dh,
            "auth": secret.auth,
        }
    )


def _decrypt(ciphertext: bytes) -> SubscriptionSecret:
    try:
        decoded = credential_vault_service.decrypt_mapping(ciphertext)
    except credential_vault_service.CredentialVaultCorrupt as exc:
        raise CorruptWebPushSubscription(
            "stored web push subscription failed authenticated decryption"
        ) from exc
    if not isinstance(decoded, dict) or set(decoded) != {"endpoint", "p256dh", "auth"}:
        raise CorruptWebPushSubscription(
            "stored web push subscription has an unknown shape"
        )
    try:
        return validate_subscription(
            endpoint=decoded["endpoint"],
            p256dh=decoded["p256dh"],
            auth=decoded["auth"],
        )
    except InvalidWebPushSubscription as exc:
        raise CorruptWebPushSubscription(
            "stored web push subscription is no longer valid"
        ) from exc


async def register(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    endpoint: Any,
    p256dh: Any,
    auth: Any,
) -> WebPushSubscription:
    """Create or refresh this account's exact browser subscription. Never commits."""

    user_id = _require_uuid(user_id, field="user_id")
    secret = validate_subscription(endpoint=endpoint, p256dh=p256dh, auth=auth)
    digest = endpoint_hash(secret.endpoint)
    # Lock the account first. Besides refusing inactive identities, this makes
    # the active-device limit deterministic under concurrent registrations.
    user = await session.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise InvalidWebPushSubscription("user is not an active account")
    row = await session.scalar(
        select(WebPushSubscription)
        .where(WebPushSubscription.endpoint_hash == digest)
        .with_for_update()
    )
    if row is not None and row.user_id != user_id:
        raise SubscriptionBelongsToAnotherAccount(
            "this browser push endpoint belongs to another account"
        )
    if row is None or row.revoked_at is not None:
        active_count = len(
            (
                await session.scalars(
                    select(WebPushSubscription.id).where(
                        WebPushSubscription.user_id == user_id,
                        WebPushSubscription.revoked_at.is_(None),
                    )
                )
            ).all()
        )
        if active_count >= MAX_ACTIVE_SUBSCRIPTIONS_PER_USER:
            raise TooManyWebPushSubscriptions(
                "account has reached the active browser subscription limit"
            )
    ciphertext = _encrypt(secret)
    if row is None:
        row = WebPushSubscription(
            user_id=user_id,
            endpoint_hash=digest,
            key_version=credential_vault_service.CURRENT_KEY_VERSION,
            ciphertext=ciphertext,
        )
        session.add(row)
    else:
        row.key_version = credential_vault_service.CURRENT_KEY_VERSION
        row.ciphertext = ciphertext
        row.revoked_at = None
    await session.flush()
    return row


async def load_secret(
    session: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    user_id: uuid.UUID,
    include_revoked: bool = False,
) -> SubscriptionSecret | None:
    """Load an exact account-owned endpoint, or ``None`` when unavailable."""

    subscription_id = _require_uuid(subscription_id, field="subscription_id")
    user_id = _require_uuid(user_id, field="user_id")
    predicates = [
        WebPushSubscription.id == subscription_id,
        WebPushSubscription.user_id == user_id,
    ]
    if not include_revoked:
        predicates.append(WebPushSubscription.revoked_at.is_(None))
    row = await session.scalar(select(WebPushSubscription).where(*predicates))
    if row is None:
        return None
    if row.ciphertext is None:
        return None
    try:
        return _decrypt(bytes(row.ciphertext))
    except credential_vault_service.CredentialVaultUnavailable:
        return None


async def load_for_dispatch(
    session: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    user_id: uuid.UUID,
) -> DispatchSubscription | None:
    """Lock and decrypt an active exact-account target for a dispatcher.

    Unlike the account-facing loader, an unavailable installation vault is not
    collapsed into absence: cancelling a valid outbox row because the process
    temporarily lacks its credential key would destroy the only truthful retry
    opportunity before any provider call was made.
    """

    subscription_id = _require_uuid(subscription_id, field="subscription_id")
    user_id = _require_uuid(user_id, field="user_id")
    row = await session.scalar(
        select(WebPushSubscription)
        .where(
            WebPushSubscription.id == subscription_id,
            WebPushSubscription.user_id == user_id,
            WebPushSubscription.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if row is None or row.ciphertext is None:
        return None
    ciphertext = bytes(row.ciphertext)
    return DispatchSubscription(
        target=_decrypt(ciphertext),
        generation=SubscriptionGeneration(
            key_version=row.key_version,
            ciphertext_fingerprint=hashlib.sha256(ciphertext).digest(),
        ),
    )


def _matches_dispatch(
    row: WebPushSubscription, generation: SubscriptionGeneration
) -> bool:
    if row.revoked_at is not None or row.ciphertext is None:
        return False
    return row.key_version == generation.key_version and hmac.compare_digest(
        hashlib.sha256(bytes(row.ciphertext)).digest(),
        generation.ciphertext_fingerprint,
    )


async def revoke_if_dispatch_matches(
    session: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    user_id: uuid.UUID,
    generation: SubscriptionGeneration,
    revoked_at: datetime,
) -> bool:
    """Erase only the exact credential generation that the provider rejected."""

    subscription_id = _require_uuid(subscription_id, field="subscription_id")
    user_id = _require_uuid(user_id, field="user_id")
    row = await session.scalar(
        select(WebPushSubscription)
        .where(
            WebPushSubscription.id == subscription_id,
            WebPushSubscription.user_id == user_id,
        )
        .with_for_update()
    )
    if row is None or not _matches_dispatch(row, generation):
        return False
    row.revoked_at = revoked_at
    row.last_success_at = None
    row.ciphertext = None
    await session.flush()
    return True


async def record_success_if_dispatch_matches(
    session: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    user_id: uuid.UUID,
    generation: SubscriptionGeneration,
    succeeded_at: datetime,
) -> bool:
    """Record success only for the credential generation that was contacted."""

    subscription_id = _require_uuid(subscription_id, field="subscription_id")
    user_id = _require_uuid(user_id, field="user_id")
    row = await session.scalar(
        select(WebPushSubscription)
        .where(
            WebPushSubscription.id == subscription_id,
            WebPushSubscription.user_id == user_id,
        )
        .with_for_update()
    )
    if row is None or not _matches_dispatch(row, generation):
        return False
    row.last_success_at = succeeded_at
    await session.flush()
    return True


async def revoke_by_id(
    session: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    user_id: uuid.UUID,
    revoked_at: datetime | None = None,
) -> bool:
    """Revoke an exact account-owned row without exposing its endpoint."""

    subscription_id = _require_uuid(subscription_id, field="subscription_id")
    user_id = _require_uuid(user_id, field="user_id")
    row = await session.scalar(
        select(WebPushSubscription)
        .where(
            WebPushSubscription.id == subscription_id,
            WebPushSubscription.user_id == user_id,
        )
        .with_for_update()
    )
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = revoked_at or now_utc()
    row.last_success_at = None
    row.ciphertext = None
    await session.flush()
    return True


async def revoke_endpoint(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    endpoint: Any,
) -> bool:
    """Revoke this account's matching browser endpoint. Never commits."""

    user_id = _require_uuid(user_id, field="user_id")
    clean_endpoint = _canonical_endpoint(endpoint)
    row = await session.scalar(
        select(WebPushSubscription)
        .where(
            WebPushSubscription.user_id == user_id,
            WebPushSubscription.endpoint_hash == endpoint_hash(clean_endpoint),
        )
        .with_for_update()
    )
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = now_utc()
    row.last_success_at = None
    row.ciphertext = None
    await session.flush()
    return True


async def endpoint_is_active(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    endpoint: Any,
) -> bool:
    """Whether this exact browser endpoint is active for this exact account.

    This deliberately returns only a boolean.  In particular, an endpoint owned
    by another account is indistinguishable from an unknown endpoint, so a
    shared browser cannot be used to discover which local account enabled it.
    """

    user_id = _require_uuid(user_id, field="user_id")
    clean_endpoint = _canonical_endpoint(endpoint)
    row_id = await session.scalar(
        select(WebPushSubscription.id).where(
            WebPushSubscription.user_id == user_id,
            WebPushSubscription.endpoint_hash == endpoint_hash(clean_endpoint),
            WebPushSubscription.revoked_at.is_(None),
        )
    )
    return row_id is not None


async def revoke_all(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Revoke every active browser endpoint for one account. Never commits."""

    user_id = _require_uuid(user_id, field="user_id")
    rows = (
        await session.scalars(
            select(WebPushSubscription)
            .where(
                WebPushSubscription.user_id == user_id,
                WebPushSubscription.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    stamp = now_utc()
    for row in rows:
        row.revoked_at = stamp
        row.last_success_at = None
        row.ciphertext = None
    if rows:
        await session.flush()
    return len(rows)


__all__ = [
    "CorruptWebPushSubscription",
    "DispatchSubscription",
    "InvalidWebPushSubscription",
    "MAX_ENDPOINT_LENGTH",
    "MAX_KEY_LENGTH",
    "SubscriptionBelongsToAnotherAccount",
    "SubscriptionGeneration",
    "SubscriptionSecret",
    "TooManyWebPushSubscriptions",
    "WebPushSubscriptionError",
    "endpoint_is_active",
    "endpoint_hash",
    "load_secret",
    "load_for_dispatch",
    "record_success_if_dispatch_matches",
    "register",
    "revoke_all",
    "revoke_by_id",
    "revoke_endpoint",
    "revoke_if_dispatch_matches",
    "validate_subscription",
]
