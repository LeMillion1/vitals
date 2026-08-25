"""PHI-free Web Push transport and its pinned upstream protocol boundary."""

from __future__ import annotations

import base64
import asyncio
import json
from dataclasses import dataclass
from importlib.metadata import version

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from vitals.integrations.web_push import (
    CARE_MESSAGE_WAKEUP,
    InvalidWebPushTarget,
    SEND_TIMEOUT_SECONDS,
    WAKEUP_TTL_SECONDS,
    WebPushClient,
    WebPushProtocolError,
    WebPushProviderOutcome,
    WebPushTarget,
    WebPushTransportError,
    validate_target,
)
from vitals.services.notifications.web_push_config import WebPushConfig


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _config() -> WebPushConfig:
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    scalar = private.private_numbers().private_value.to_bytes(32, "big")
    return WebPushConfig(
        public_key=_b64(public),
        private_key=_b64(scalar),
        subject="mailto:operator@example.test",
    )


def _subscription(*, endpoint: str | None = None) -> WebPushTarget:
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return validate_target(
        endpoint=endpoint
        or "https://fcm.googleapis.com/fcm/send/synthetic-token",
        p256dh=_b64(public),
        auth=_b64(b"a" * 16),
    )


@dataclass
class _Response:
    status: object
    reason: str = "Synthetic"
    closed: bool = False
    text_called: bool = False

    def close(self) -> None:
        self.closed = True

    async def text(self) -> str:
        self.text_called = True
        return "provider body that must never escape"


class _ProviderFailure(Exception):
    def __init__(self, response=None):
        self.response = response
        super().__init__("secret endpoint and provider body")


async def test_client_sends_only_the_fixed_generic_payload_and_reviewed_options():
    seen = {}

    async def sender(**kwargs):
        seen.update(kwargs)
        return _Response(201)

    subscription = _subscription()
    result = await WebPushClient(_config(), sender=sender).send_care_message_wakeup(
        subscription
    )

    assert result.outcome is WebPushProviderOutcome.ACCEPTED
    assert result.status_code == 201
    assert seen["data"] == '{"kind":"care_message","v":1}'
    assert json.loads(seen["data"]) == {"kind": "care_message", "v": 1}
    assert seen["content_encoding"] == "aes128gcm"
    assert seen["timeout"] == SEND_TIMEOUT_SECONDS
    assert seen["ttl"] == WAKEUP_TTL_SECONDS
    assert seen["verbose"] is False
    assert seen["headers"] is None
    assert seen["vapid_claims"] == {"sub": "mailto:operator@example.test"}
    assert seen["subscription_info"] == subscription.as_webpush_dict()
    forbidden = {
        "subject_id",
        "thread_id",
        "message_id",
        "recipient_user_id",
        "patient",
        "author",
        "body",
        "title",
        "filename",
        "url",
    }
    assert forbidden.isdisjoint(json.loads(CARE_MESSAGE_WAKEUP))


@pytest.mark.parametrize("status", [200, 201, 202])
async def test_success_statuses_are_accepted(status):
    async def sender(**_kwargs):
        return _Response(status)

    result = await WebPushClient(_config(), sender=sender).send_care_message_wakeup(
        _subscription()
    )
    assert result.outcome is WebPushProviderOutcome.ACCEPTED
    assert result.status_code == status


@pytest.mark.parametrize("status", [404, 410])
async def test_expired_provider_endpoint_is_bounded_gone_outcome(status):
    async def sender(**_kwargs):
        raise _ProviderFailure(_Response(status))

    result = await WebPushClient(_config(), sender=sender).send_care_message_wakeup(
        _subscription()
    )
    assert result.outcome is WebPushProviderOutcome.GONE
    assert result.status_code == status


@pytest.mark.parametrize("status", [300, 307, 400, 401, 403, 413, 429, 499])
async def test_provider_rejection_exposes_only_status_and_bounded_outcome(status):
    async def sender(**_kwargs):
        raise _ProviderFailure(_Response(status))

    result = await WebPushClient(_config(), sender=sender).send_care_message_wakeup(
        _subscription()
    )
    assert result.outcome is WebPushProviderOutcome.REJECTED
    assert result.status_code == status
    assert "provider body" not in repr(result)


@pytest.mark.parametrize("status", [500, 502, 503, 599])
async def test_provider_server_failure_is_ambiguous_and_never_retried(status):
    calls = 0

    async def sender(**_kwargs):
        nonlocal calls
        calls += 1
        raise _ProviderFailure(_Response(status))

    result = await WebPushClient(_config(), sender=sender).send_care_message_wakeup(
        _subscription()
    )
    assert result.outcome is WebPushProviderOutcome.AMBIGUOUS
    assert result.status_code == status
    assert calls == 1


async def test_transport_exception_is_sanitized_and_suppresses_secret_cause():
    async def sender(**_kwargs):
        raise _ProviderFailure()

    with pytest.raises(WebPushTransportError) as caught:
        await WebPushClient(_config(), sender=sender).send_care_message_wakeup(
            _subscription()
        )
    assert str(caught.value) == "web push transport failed"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "endpoint" not in str(caught.value)


async def test_invalid_provider_status_suppresses_body_bearing_exception():
    async def sender(**_kwargs):
        raise _ProviderFailure(_Response(204))

    with pytest.raises(WebPushProtocolError) as caught:
        await WebPushClient(_config(), sender=sender).send_care_message_wakeup(
            _subscription()
        )
    assert str(caught.value) == "web push provider returned an invalid status"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "provider body" not in str(caught.value)


@pytest.mark.parametrize("status", [None, True, 99, 600, "201"])
async def test_malformed_sender_response_fails_closed(status):
    async def sender(**_kwargs):
        return _Response(status)

    with pytest.raises(WebPushProtocolError):
        await WebPushClient(_config(), sender=sender).send_care_message_wakeup(
            _subscription()
        )


async def test_cancellation_propagates_without_becoming_a_retryable_error():
    async def sender(**_kwargs):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await WebPushClient(_config(), sender=sender).send_care_message_wakeup(
            _subscription()
        )


async def test_target_is_revalidated_immediately_before_network_io():
    calls = 0
    valid = _subscription()
    unsafe = WebPushTarget(
        endpoint="https://127.0.0.1/internal",
        p256dh=valid.p256dh,
        auth=valid.auth,
    )

    async def sender(**_kwargs):
        nonlocal calls
        calls += 1
        return _Response(201)

    with pytest.raises(InvalidWebPushTarget):
        await WebPushClient(_config(), sender=sender).send_care_message_wakeup(unsafe)
    assert calls == 0


async def test_each_send_gets_fresh_vapid_claims():
    claims_before_mutation = []

    async def sender(**kwargs):
        claims_before_mutation.append(dict(kwargs["vapid_claims"]))
        kwargs["vapid_claims"]["aud"] = "https://provider.invalid"
        kwargs["vapid_claims"]["exp"] = 1
        return _Response(201)

    client = WebPushClient(_config(), sender=sender)
    await client.send_care_message_wakeup(_subscription())
    await client.send_care_message_wakeup(_subscription())
    assert claims_before_mutation == [
        {"sub": "mailto:operator@example.test"},
        {"sub": "mailto:operator@example.test"},
    ]


async def test_windows_raw_headers_do_not_leak_to_other_providers():
    calls = []

    async def sender(**kwargs):
        calls.append(kwargs)
        return _Response(201)

    client = WebPushClient(_config(), sender=sender)
    await client.send_care_message_wakeup(
        _subscription(endpoint="https://db5.notify.windows.com/w/synthetic")
    )
    await client.send_care_message_wakeup(_subscription())

    assert calls[0]["headers"] == {
        "Content-Type": "application/octet-stream",
        "X-WNS-Type": "wns/raw",
    }
    assert calls[1]["headers"] is None


def test_secret_reprs_are_redacted():
    config = _config()
    target = _subscription()
    assert config.private_key not in repr(config)
    assert target.endpoint not in repr(target)
    assert target.p256dh not in repr(target)
    assert target.auth not in repr(target)


async def test_pinned_pywebpush_encrypts_signs_and_refuses_redirects(
    monkeypatch,
):
    """Exercise the real SDK through our no-network, no-redirect adapter."""

    import aiohttp
    import pywebpush  # noqa: F401 — import before replacing its annotated session type

    class Session:
        def __init__(self):
            self.calls = []
            self.responses = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, endpoint, **kwargs):
            self.calls.append((endpoint, kwargs))
            response = _Response(201)
            self.responses.append(response)
            return response

    config = _config()
    subscription = _subscription()
    session = Session()
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    result = await WebPushClient(config).send_care_message_wakeup(
        subscription
    )

    assert version("pywebpush") == "2.4.0"
    assert result.outcome is WebPushProviderOutcome.ACCEPTED
    assert len(session.calls) == 1
    endpoint, request = session.calls[0]
    assert endpoint == subscription.endpoint
    assert request["allow_redirects"] is False
    assert session.responses[0].closed is True
    assert session.responses[0].text_called is False
    assert CARE_MESSAGE_WAKEUP.encode() not in request["data"]
    assert request["headers"]["Content-Encoding"] == "aes128gcm"
    assert request["headers"]["Authorization"].startswith("vapid t=")
