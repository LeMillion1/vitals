"""Validated async Web Push transport for one generic care-message wakeup.

This is the only layer that knows Web Push endpoint shapes, ECE/VAPID library
quirks, provider-specific headers, or HTTP outcome semantics. It deliberately
does not accept arbitrary notification content: callers may request only the
fixed, versioned care-message wakeup, so patient names, message text, filenames,
URLs, and record identifiers cannot accidentally cross the provider boundary.

Provider bodies and library exception strings may echo endpoint material. They
are never retained or exposed here: results carry only a bounded outcome and
HTTP status, while local/transport failures use constant messages and suppress
the upstream exception chain.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec

CARE_MESSAGE_WAKEUP = json.dumps(
    {"kind": "care_message", "v": 1},
    separators=(",", ":"),
    sort_keys=True,
)
SEND_TIMEOUT_SECONDS = 10.0
WAKEUP_TTL_SECONDS = 300
MAX_ENDPOINT_LENGTH = 4096
MAX_KEY_LENGTH = 256

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
_Sender = Callable[..., Awaitable[Any]]


class WebPushCredentials(Protocol):
    """The already-validated installation values the transport consumes."""

    private_key: str
    subject: str


class InvalidWebPushTarget(ValueError):
    """A target is malformed or outside the reviewed provider allowlist."""


@dataclass(frozen=True, slots=True)
class WebPushTarget:
    endpoint: str = field(repr=False)
    p256dh: str = field(repr=False)
    auth: str = field(repr=False)

    def as_webpush_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }


class WebPushProviderOutcome(StrEnum):
    """Bounded provider outcome; response bodies are never represented."""

    ACCEPTED = "accepted"
    GONE = "gone"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class WebPushProviderResult:
    outcome: WebPushProviderOutcome
    status_code: int


class WebPushTransportError(RuntimeError):
    """The call failed without a trustworthy HTTP response."""


class WebPushProtocolError(RuntimeError):
    """The sender returned a response outside the reviewed HTTP contract."""


def _decode_base64url(value: Any, *, field_name: str, expected_bytes: int) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_KEY_LENGTH:
        raise InvalidWebPushTarget(f"{field_name} is invalid")
    if "=" in value.rstrip("="):
        raise InvalidWebPushTarget(f"{field_name} is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise InvalidWebPushTarget(f"{field_name} is invalid") from exc
    if len(decoded) != expected_bytes:
        raise InvalidWebPushTarget(f"{field_name} is invalid")
    return value.rstrip("=")


def canonical_endpoint(value: Any) -> str:
    """Return a reviewed HTTPS provider endpoint or fail before network I/O."""

    if not isinstance(value, str):
        raise InvalidWebPushTarget("endpoint must be a string")
    endpoint = value.strip()
    if not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH:
        raise InvalidWebPushTarget("endpoint is invalid")
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidWebPushTarget("endpoint is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.hostname
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise InvalidWebPushTarget("endpoint is invalid")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidWebPushTarget("endpoint host is invalid") from exc
    if hostname not in _EXACT_PUSH_HOSTS and not any(
        hostname.endswith(suffix) and hostname != suffix[1:]
        for suffix in _PUSH_HOST_SUFFIXES
    ):
        raise InvalidWebPushTarget("endpoint host is not an approved push service")
    return endpoint


def validate_target(*, endpoint: Any, p256dh: Any, auth: Any) -> WebPushTarget:
    """Validate both the SSRF boundary and Web Push encryption key material."""

    clean_endpoint = canonical_endpoint(endpoint)
    clean_p256dh = _decode_base64url(
        p256dh, field_name="p256dh", expected_bytes=65
    )
    decoded_public_key = base64.urlsafe_b64decode(
        clean_p256dh + "=" * (-len(clean_p256dh) % 4)
    )
    if decoded_public_key[0] != 4:
        raise InvalidWebPushTarget("p256dh is not an uncompressed P-256 key")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), decoded_public_key
        )
    except ValueError as exc:
        raise InvalidWebPushTarget("p256dh is not a valid P-256 point") from exc
    clean_auth = _decode_base64url(auth, field_name="auth", expected_bytes=16)
    return WebPushTarget(
        endpoint=clean_endpoint,
        p256dh=clean_p256dh,
        auth=clean_auth,
    )


def _status_from_response(response: Any) -> int | None:
    status = getattr(response, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    if not 100 <= status <= 599:
        return None
    return status


def _classify(status_code: int) -> WebPushProviderResult:
    if 200 <= status_code <= 202:
        outcome = WebPushProviderOutcome.ACCEPTED
    elif status_code in {404, 410}:
        outcome = WebPushProviderOutcome.GONE
    elif 300 <= status_code <= 499:
        outcome = WebPushProviderOutcome.REJECTED
    elif 500 <= status_code <= 599:
        outcome = WebPushProviderOutcome.AMBIGUOUS
    else:
        raise WebPushProtocolError("web push sender returned an invalid status")
    return WebPushProviderResult(outcome=outcome, status_code=status_code)


class _NoRedirectSession:
    """Give pywebpush a session whose POST cannot escape the host allowlist."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def post(self, endpoint: str, **kwargs: Any) -> Any:
        kwargs["allow_redirects"] = False
        response = await self._session.post(endpoint, **kwargs)
        return _BodyDiscardingResponse(response)


class _BodyDiscardingResponse:
    """Expose status/reason to pywebpush without ever reading provider content."""

    def __init__(self, response: Any) -> None:
        self.status = getattr(response, "status", None)
        self.reason = getattr(response, "reason", "")
        close = getattr(response, "close", None)
        if callable(close):
            close()

    async def text(self) -> str:
        return ""


async def _send_without_redirects(**kwargs: Any) -> Any:
    import aiohttp
    from pywebpush import webpush_async

    async with aiohttp.ClientSession() as session:
        return await webpush_async(
            **kwargs,
            aiohttp_session=_NoRedirectSession(session),
        )


def _provider_headers(target: WebPushTarget) -> dict[str, str] | None:
    hostname = (urlsplit(target.endpoint).hostname or "").lower()
    if hostname.endswith(".notify.windows.com"):
        return {
            "Content-Type": "application/octet-stream",
            "X-WNS-Type": "wns/raw",
        }
    return None


class WebPushClient:
    """One installation's PHI-free async push transport."""

    def __init__(
        self,
        config: WebPushCredentials,
        *,
        sender: _Sender | None = None,
    ) -> None:
        self._private_key = config.private_key
        self._subject = config.subject
        self._sender = sender

    async def send_care_message_wakeup(
        self, target: WebPushTarget
    ) -> WebPushProviderResult:
        """Send the fixed generic wakeup once; never retries or logs secrets."""

        # Revalidate immediately before I/O. A target may have been constructed
        # directly, and stored ciphertext may predate today's provider policy.
        target = validate_target(
            endpoint=target.endpoint,
            p256dh=target.p256dh,
            auth=target.auth,
        )
        sender = self._sender or _send_without_redirects
        try:
            response = await sender(
                subscription_info=target.as_webpush_dict(),
                data=CARE_MESSAGE_WAKEUP,
                vapid_private_key=self._private_key,
                vapid_claims={"sub": self._subject},
                content_encoding="aes128gcm",
                timeout=SEND_TIMEOUT_SECONDS,
                ttl=WAKEUP_TTL_SECONDS,
                verbose=False,
                headers=_provider_headers(target),
            )
        except Exception as exc:
            status_code = _status_from_response(getattr(exc, "response", None))
            if status_code is not None:
                try:
                    return _classify(status_code)
                except WebPushProtocolError:
                    raise WebPushProtocolError(
                        "web push provider returned an invalid status"
                    ) from None
            raise WebPushTransportError("web push transport failed") from None

        status_code = _status_from_response(response)
        if status_code is None:
            raise WebPushProtocolError(
                "web push sender returned an invalid response"
            ) from None
        return _classify(status_code)


__all__ = [
    "CARE_MESSAGE_WAKEUP",
    "MAX_ENDPOINT_LENGTH",
    "MAX_KEY_LENGTH",
    "SEND_TIMEOUT_SECONDS",
    "WAKEUP_TTL_SECONDS",
    "InvalidWebPushTarget",
    "WebPushClient",
    "WebPushCredentials",
    "WebPushProtocolError",
    "WebPushProviderOutcome",
    "WebPushProviderResult",
    "WebPushTarget",
    "WebPushTransportError",
    "canonical_endpoint",
    "validate_target",
]
