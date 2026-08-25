"""Fail-closed installation configuration for browser Web Push.

The VAPID key pair identifies the installation to browser push services.  It is
platform configuration, not patient data and not an account credential, so it
lives in environment secrets.  This module validates the complete pair before
the browser is offered a public key: collecting subscriptions that the server
cannot later use would be a misleading, privacy-sensitive dead end.
"""

from __future__ import annotations

import base64
import hmac
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ENABLED_ENV = "VITALS_WEB_PUSH_ENABLED"
PUBLIC_KEY_ENV = "VITALS_WEB_PUSH_VAPID_PUBLIC_KEY"
PRIVATE_KEY_ENV = "VITALS_WEB_PUSH_VAPID_PRIVATE_KEY"
SUBJECT_ENV = "VITALS_WEB_PUSH_VAPID_SUBJECT"

_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)


class WebPushConfigurationError(RuntimeError):
    """Web Push was enabled but its installation configuration is unusable."""


@dataclass(frozen=True, slots=True)
class WebPushConfig:
    public_key: str
    private_key: str
    subject: str


def _decode_base64url(value: str, *, name: str, expected_bytes: int) -> bytes:
    if not value or len(value) > 256 or "=" in value.rstrip("="):
        raise WebPushConfigurationError(f"{name} is not valid base64url")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise WebPushConfigurationError(f"{name} is not valid base64url") from exc
    if len(decoded) != expected_bytes:
        raise WebPushConfigurationError(f"{name} has the wrong length")
    return decoded


def _enabled() -> bool:
    raw = (os.getenv(ENABLED_ENV) or "").strip().lower()
    if not raw:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise WebPushConfigurationError(f"{ENABLED_ENV} must be a boolean")


def _validate_subject(value: str) -> str:
    subject = value.strip()
    parsed = urlsplit(subject)
    if parsed.scheme == "mailto":
        if not parsed.path or "@" not in parsed.path or parsed.query or parsed.fragment:
            raise WebPushConfigurationError(f"{SUBJECT_ENV} is invalid")
        return subject
    if (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
    ):
        return subject
    raise WebPushConfigurationError(
        f"{SUBJECT_ENV} must be a mailto address or HTTPS URL"
    )


def load_config() -> WebPushConfig | None:
    """Return a fully verified VAPID configuration, or ``None`` when disabled."""

    if not _enabled():
        return None

    public_key = (os.getenv(PUBLIC_KEY_ENV) or "").strip().rstrip("=")
    private_key = (os.getenv(PRIVATE_KEY_ENV) or "").strip().rstrip("=")
    subject = _validate_subject(os.getenv(SUBJECT_ENV) or "")
    public_bytes = _decode_base64url(
        public_key, name=PUBLIC_KEY_ENV, expected_bytes=65
    )
    if public_bytes[0] != 4:
        raise WebPushConfigurationError(
            f"{PUBLIC_KEY_ENV} must be an uncompressed P-256 public key"
        )
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_bytes)
    except ValueError as exc:
        raise WebPushConfigurationError(
            f"{PUBLIC_KEY_ENV} is not a valid P-256 point"
        ) from exc

    private_bytes = _decode_base64url(
        private_key, name=PRIVATE_KEY_ENV, expected_bytes=32
    )
    scalar = int.from_bytes(private_bytes, "big")
    if not 0 < scalar < _P256_ORDER:
        raise WebPushConfigurationError(f"{PRIVATE_KEY_ENV} is not a P-256 scalar")
    derived_public = ec.derive_private_key(scalar, ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    if not hmac.compare_digest(derived_public, public_bytes):
        raise WebPushConfigurationError("VAPID public and private keys do not match")
    return WebPushConfig(
        public_key=public_key,
        private_key=private_key,
        subject=subject,
    )


__all__ = [
    "ENABLED_ENV",
    "PRIVATE_KEY_ENV",
    "PUBLIC_KEY_ENV",
    "SUBJECT_ENV",
    "WebPushConfig",
    "WebPushConfigurationError",
    "load_config",
]
