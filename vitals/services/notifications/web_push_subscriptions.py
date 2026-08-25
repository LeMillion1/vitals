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

import base64
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserStatus
from vitals.models.identity import User
from vitals.models.web_push import WebPushSubscription
from vitals.services import credential_vault_service
from vitals.utils.timeutils import now_utc

MAX_ENDPOINT_LENGTH = 4096
MAX_KEY_LENGTH = 256
MAX_ACTIVE_SUBSCRIPTIONS_PER_USER = 10

# Reviewed browser push services.  Subdomains are accepted only for the two
# providers that allocate region-specific delivery hosts.
_EXACT_PUSH_HOSTS = frozenset(
    {
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "push.services.mozilla.com",
        "web.push.apple.com",
    }
)
_PUSH_HOST_SUFFIXES = (
    ".notify.windows.com",
    ".push.apple.com",
)


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


@dataclass(frozen=True, slots=True)
class SubscriptionSecret:
    endpoint: str
    p256dh: str
    auth: str

    def as_webpush_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }


def _require_uuid(value: Any, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise InvalidWebPushSubscription(f"{field} must be a non-zero UUID")
    return value


def _decode_base64url(value: Any, *, field: str, expected_bytes: int) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_KEY_LENGTH:
        raise InvalidWebPushSubscription(f"{field} is invalid")
    if "=" in value.rstrip("="):
        raise InvalidWebPushSubscription(f"{field} is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise InvalidWebPushSubscription(f"{field} is invalid") from exc
    if len(decoded) != expected_bytes:
        raise InvalidWebPushSubscription(f"{field} is invalid")
    return value.rstrip("=")


def _canonical_endpoint(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidWebPushSubscription("endpoint must be a string")
    endpoint = value.strip()
    if not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH:
        raise InvalidWebPushSubscription("endpoint is invalid")
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidWebPushSubscription("endpoint is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.hostname
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise InvalidWebPushSubscription("endpoint is invalid")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidWebPushSubscription("endpoint host is invalid") from exc
    if hostname not in _EXACT_PUSH_HOSTS and not any(
        hostname.endswith(suffix) and hostname != suffix[1:]
        for suffix in _PUSH_HOST_SUFFIXES
    ):
        raise InvalidWebPushSubscription("endpoint host is not an approved push service")
    return endpoint


def validate_subscription(
    *, endpoint: Any, p256dh: Any, auth: Any
) -> SubscriptionSecret:
    clean_endpoint = _canonical_endpoint(endpoint)
    clean_p256dh = _decode_base64url(
        p256dh, field="p256dh", expected_bytes=65
    )
    decoded_public_key = base64.urlsafe_b64decode(
        clean_p256dh + "=" * (-len(clean_p256dh) % 4)
    )
    if decoded_public_key[0] != 4:
        raise InvalidWebPushSubscription("p256dh is not an uncompressed P-256 key")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), decoded_public_key
        )
    except ValueError as exc:
        raise InvalidWebPushSubscription("p256dh is not a valid P-256 point") from exc
    clean_auth = _decode_base64url(auth, field="auth", expected_bytes=16)
    return SubscriptionSecret(
        endpoint=clean_endpoint,
        p256dh=clean_p256dh,
        auth=clean_auth,
    )


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
    "InvalidWebPushSubscription",
    "SubscriptionBelongsToAnotherAccount",
    "SubscriptionSecret",
    "TooManyWebPushSubscriptions",
    "WebPushSubscriptionError",
    "endpoint_is_active",
    "endpoint_hash",
    "load_secret",
    "register",
    "revoke_all",
    "revoke_endpoint",
    "validate_subscription",
]
