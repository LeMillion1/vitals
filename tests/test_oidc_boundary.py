"""Every check the OIDC boundary makes, and an attack that each one stops.

Nothing downstream re-validates a login, so a check that is missing here is
missing everywhere. These tests are written as the attack rather than as the
happy path: a token signed by the wrong key, addressed to another client, from a
provider claiming to be somebody else, replayed with a stale nonce, or redeemed
without the verifier that bound it.

The provider is real rather than mocked at the signature layer — an RSA key pair
and genuinely signed JWTs — because a mock that returns "valid" teaches nothing
about whether the validation works.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from vitals.services.authentication.oidc import (
    OidcConfigurationError,
    OidcDiscoveryError,
    OidcProtocolError,
    OidcProvider,
    OidcSettings,
    OidcTokenError,
    new_pkce_pair,
)

ISSUER = "https://idp.example.test"
CLIENT_ID = "vitals"
REDIRECT = "https://vitals.example.test/auth/callback"


class FakeProvider:
    """An OIDC provider with real keys, that can be told to misbehave."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.issuer = ISSUER
        self.metadata_issuer = ISSUER
        self.token_status = 200
        self.claim_overrides: dict = {}
        self.sign_with_other_key = False
        self.omit_id_token = False
        self.pkce_methods = ["S256"]
        self.signing_algorithms = ["RS256"]

    # ── what the provider publishes ──────────────────────────────────────
    @property
    def jwks(self) -> dict:
        numbers = self.key.public_key().public_numbers()
        from jwt.utils import to_base64url_uint

        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "test-key",
                    "use": "sig",
                    "alg": "RS256",
                    "n": to_base64url_uint(numbers.n).decode(),
                    "e": to_base64url_uint(numbers.e).decode(),
                }
            ]
        }

    @property
    def document(self) -> dict:
        return {
            "issuer": self.metadata_issuer,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/keys",
            "end_session_endpoint": f"{ISSUER}/logout",
            "code_challenge_methods_supported": self.pkce_methods,
            "id_token_signing_alg_values_supported": self.signing_algorithms,
        }

    def id_token(self, **overrides) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "sub": "provider-subject-1",
            "aud": CLIENT_ID,
            "exp": now + 300,
            "iat": now,
            "auth_time": now,
            "nonce": overrides.pop("_nonce", "expected-nonce"),
            "email": "owner@example.test",
            "email_verified": True,
            "acr": "urn:mace:incommon:iap:silver",
            "amr": ["pwd", "otp"],
        }
        claims.update(self.claim_overrides)
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not _ABSENT}
        signer = self.other_key if self.sign_with_other_key else self.key
        return jwt.encode(claims, signer, algorithm="RS256", headers={"kid": "test-key"})

    # ── the transport ────────────────────────────────────────────────────
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=self.document)
        if path.endswith("/keys"):
            return httpx.Response(200, json=self.jwks)
        if path.endswith("/token"):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            body = dict(
                item.split("=", 1) for item in request.content.decode().split("&")
            )
            self.last_token_request = body
            payload = {"access_token": "at", "token_type": "Bearer"}
            if not self.omit_id_token:
                payload["id_token"] = self._pending_id_token
            return httpx.Response(200, json=payload)
        return httpx.Response(404)


class _Absent:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


_ABSENT = _Absent()


@pytest.fixture
def provider(monkeypatch):
    """A configured :class:`OidcProvider` talking to the fake over a transport."""

    fake = FakeProvider()
    transport = httpx.MockTransport(fake.handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    subject = OidcProvider(
        OidcSettings(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret="s3cret",
            redirect_url=REDIRECT,
        )
    )
    return subject, fake


async def test_logout_url_uses_discovery_and_the_registered_client(provider):
    from urllib.parse import parse_qs, urlsplit

    oidc, _fake = provider
    target = await oidc.end_session_url(
        post_logout_redirect_uri="https://vitals.example.test/"
    )
    assert target is not None
    parsed = urlsplit(target)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{ISSUER}/logout"
    assert parse_qs(parsed.query) == {
        "client_id": [CLIENT_ID],
        "post_logout_redirect_uri": ["https://vitals.example.test/"],
    }


async def _login(subject, fake, **overrides):
    request = await subject.begin_login()
    fake._pending_id_token = fake.id_token(_nonce=request.nonce, **overrides)
    return await subject.complete_login(
        code="the-code",
        code_verifier=request.code_verifier,
        expected_nonce=request.nonce,
    )


# ── Configuration is refused before anything reaches the network ─────────────

def test_an_issuer_that_is_not_https_is_refused():
    for bad in ("http://idp.example.test", "ftp://idp", ""):
        with pytest.raises(OidcConfigurationError):
            OidcSettings(
                issuer=bad,
                client_id=CLIENT_ID,
                client_secret="s",
                redirect_url=REDIRECT,
            )


def test_localhost_over_http_is_allowed_for_development():
    settings = OidcSettings(
        issuer="http://localhost:8080",
        client_id=CLIENT_ID,
        client_secret="s",
        redirect_url=REDIRECT,
    )
    assert settings.issuer.startswith("http://localhost")


@pytest.mark.parametrize(
    "bad_issuer",
    (
        "http://localhost.evil.test",
        "http://localhost@evil.test",
        "http://localhost:bad-port",
        "https://idp.example.test:",
        "https://idp.example.test:0",
        "https://idp.example.test:65536",
        "https://user@idp.example.test",
        "https://idp.example.test?tenant=one",
        "https://idp.example.test#fragment",
        "https://idp.example.test/a/../b",
        "https://idp.example.test/a/%2e%2e/b",
        "https://idp.example.test\\@evil.test",
        " https://idp.example.test",
    ),
)
def test_ambiguous_or_credentialed_issuer_is_refused(bad_issuer):
    with pytest.raises(OidcConfigurationError):
        OidcSettings(
            issuer=bad_issuer,
            client_id=CLIENT_ID,
            client_secret="s",
            redirect_url=REDIRECT,
        )


@pytest.mark.parametrize(
    "bad_redirect",
    (
        "http://vitals.example.test/auth/callback",
        "http://localhost.evil.test/auth/callback",
        "http://localhost@evil.test/auth/callback",
        "https://user@vitals.example.test/auth/callback",
        "https://vitals.example.test/not-the-callback",
        "https://vitals.example.test/auth/callback?next=/",
        "https://vitals.example.test/auth/callback#fragment",
        "https://vitals.example.test:bad/auth/callback",
        "https://vitals.example.test:/auth/callback",
        "https://vitals.example.test:0/auth/callback",
    ),
)
def test_unsafe_or_inexact_redirect_url_is_refused(bad_redirect):
    with pytest.raises(OidcConfigurationError):
        OidcSettings(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret="s",
            redirect_url=bad_redirect,
        )


def test_exact_localhost_callback_is_allowed_for_development():
    settings = OidcSettings(
        issuer="http://localhost:8080",
        client_id=CLIENT_ID,
        client_secret="s",
        redirect_url="http://localhost:8000/auth/callback",
    )

    assert settings.redirect_url == "http://localhost:8000/auth/callback"


def test_https_issuer_may_use_a_tenant_path_and_valid_nondefault_port():
    settings = OidcSettings(
        issuer="https://idp.example.test:8443/realms/vitals",
        client_id=CLIENT_ID,
        client_secret="s",
        redirect_url=REDIRECT,
    )

    assert settings.issuer.endswith("/realms/vitals")


def test_dropping_the_openid_scope_is_refused():
    with pytest.raises(OidcConfigurationError, match="openid"):
        OidcSettings(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret="s",
            redirect_url=REDIRECT,
            scopes=("profile",),
        )


# ── PKCE ─────────────────────────────────────────────────────────────────────

def test_the_pkce_challenge_is_the_hash_of_the_verifier_not_the_verifier():
    import base64
    import hashlib

    verifier, challenge = new_pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert challenge == expected
    assert challenge != verifier, "a plain challenge binds the code to nobody"


def test_every_login_request_is_unique():
    """Reused state or nonce would let one login's callback complete another."""

    from vitals.services.authentication.oidc import new_pkce_pair as pair

    verifiers = {pair()[0] for _ in range(50)}
    assert len(verifiers) == 50


# ── Discovery ────────────────────────────────────────────────────────────────

async def test_a_provider_describing_a_different_issuer_is_refused(provider):
    subject, fake = provider
    fake.metadata_issuer = "https://somebody-else.example.test"
    with pytest.raises(OidcDiscoveryError, match="different issuer"):
        await subject.metadata()


async def test_a_provider_without_s256_pkce_is_refused(provider):
    """``plain`` binds a code to nobody, so a provider offering only it is one
    this boundary declines to talk to rather than downgrades for."""

    subject, fake = provider
    fake.pkce_methods = ["plain"]
    with pytest.raises(OidcDiscoveryError, match="S256"):
        await subject.metadata()


# ── The token ────────────────────────────────────────────────────────────────

async def test_a_valid_login_yields_the_issuer_and_subject_pair(provider):
    subject, fake = provider
    identity = await _login(subject, fake)
    assert (identity.issuer, identity.subject) == (ISSUER, "provider-subject-1")
    assert identity.amr == ("pwd", "otp")
    assert identity.email == "owner@example.test"
    assert identity.authenticated_at is not None


@pytest.mark.parametrize("claim", ["false", "true", 1, 0, None])
async def test_only_json_true_verifies_an_email_claim(provider, claim):
    """A truthy malformed claim must never become an invitation proof."""

    subject, fake = provider
    identity = await _login(subject, fake, email_verified=claim)
    assert identity.email_verified is False


async def test_a_token_signed_by_another_key_is_refused(provider):
    subject, fake = provider
    fake.sign_with_other_key = True
    with pytest.raises(OidcTokenError):
        await _login(subject, fake)


async def test_a_token_for_another_client_is_refused(provider):
    """A valid token, correctly signed, addressed to somebody else."""

    subject, fake = provider
    with pytest.raises(OidcTokenError):
        await _login(subject, fake, aud="a-different-client")


async def test_a_token_authorized_for_another_party_is_refused(provider):
    """``azp`` names the party even when our client id is in the audience."""

    subject, fake = provider
    with pytest.raises(OidcTokenError, match="different party"):
        await _login(subject, fake, aud=[CLIENT_ID, "other"], azp="other")


async def test_a_token_from_another_issuer_is_refused(provider):
    subject, fake = provider
    with pytest.raises(OidcTokenError):
        await _login(subject, fake, iss="https://elsewhere.example.test")


async def test_a_replayed_token_with_the_wrong_nonce_is_refused(provider):
    """The nonce is what binds a token to *this* browser's login request."""

    subject, fake = provider
    request = await subject.begin_login()
    fake._pending_id_token = fake.id_token(_nonce="a-nonce-from-another-login")
    with pytest.raises(OidcTokenError, match="nonce"):
        await subject.complete_login(
            code="the-code",
            code_verifier=request.code_verifier,
            expected_nonce=request.nonce,
        )


async def test_an_expired_token_is_refused(provider):
    subject, fake = provider
    with pytest.raises(OidcTokenError):
        await _login(subject, fake, exp=int(time.time()) - 3600)


async def test_a_token_without_a_subject_is_refused(provider):
    subject, fake = provider
    with pytest.raises(OidcTokenError):
        await _login(subject, fake, sub="   ")


# ── Mix-up: the authorization response's own issuer ──────────────────────────

def test_a_callback_naming_another_issuer_is_refused(provider):
    subject, _ = provider
    with pytest.raises(OidcProtocolError, match="different issuer"):
        subject.check_response_issuer("https://attacker.example.test")


def test_a_callback_naming_the_right_issuer_passes(provider):
    subject, _ = provider
    subject.check_response_issuer(ISSUER)


def test_a_callback_omitting_the_issuer_is_tolerated(provider):
    """RFC 9207 is recent; its absence is not itself evidence of an attack.

    The ID token's own ``iss`` is still checked, so omission costs the extra
    layer rather than the boundary.
    """

    subject, _ = provider
    subject.check_response_issuer(None)


# ── Freshness, for step-up ───────────────────────────────────────────────────

async def test_an_old_authentication_is_refused_when_freshness_is_demanded(provider):
    subject, fake = provider
    request = await subject.begin_login()
    fake._pending_id_token = fake.id_token(
        _nonce=request.nonce, auth_time=int(time.time()) - 7200
    )
    with pytest.raises(OidcTokenError, match="longer ago"):
        await subject.complete_login(
            code="c",
            code_verifier=request.code_verifier,
            expected_nonce=request.nonce,
            max_age_seconds=300,
        )


async def test_demanding_freshness_from_a_provider_that_reports_none_is_refused(
    provider,
):
    """Silence is not proof of a recent login."""

    subject, fake = provider
    request = await subject.begin_login()
    fake._pending_id_token = fake.id_token(_nonce=request.nonce, auth_time=_ABSENT)
    with pytest.raises(OidcTokenError, match="no auth_time"):
        await subject.complete_login(
            code="c",
            code_verifier=request.code_verifier,
            expected_nonce=request.nonce,
            max_age_seconds=300,
        )


# ── The token endpoint ───────────────────────────────────────────────────────

async def test_a_refused_code_does_not_leak_the_providers_error(provider):
    subject, fake = provider
    fake.token_status = 400
    request = await subject.begin_login()
    with pytest.raises(OidcProtocolError) as caught:
        await subject.complete_login(
            code="c", code_verifier=request.code_verifier, expected_nonce=request.nonce
        )
    assert "invalid_grant" not in str(caught.value)


async def test_a_response_without_an_id_token_is_refused(provider):
    subject, fake = provider
    fake.omit_id_token = True
    request = await subject.begin_login()
    with pytest.raises(OidcProtocolError, match="no id_token"):
        await subject.complete_login(
            code="c", code_verifier=request.code_verifier, expected_nonce=request.nonce
        )


async def test_the_verifier_is_sent_to_the_token_endpoint(provider):
    """PKCE is only a defence if the verifier actually leaves the client."""

    subject, fake = provider
    request = await subject.begin_login()
    fake._pending_id_token = fake.id_token(_nonce=request.nonce)
    await subject.complete_login(
        code="c", code_verifier=request.code_verifier, expected_nonce=request.nonce
    )
    assert fake.last_token_request["code_verifier"] == request.code_verifier
    assert fake.last_token_request["grant_type"] == "authorization_code"
    assert fake.last_token_request["client_id"] == CLIENT_ID
    assert fake.last_token_request["client_secret"] == "s3cret"


# ── The two classic JWT holes ────────────────────────────────────────────────

async def test_an_unsigned_token_is_refused(provider):
    """``alg: none`` is not a signature, whatever the header calls it.

    The oldest JWT vulnerability there is, and it survives because a library
    asked to "verify" a token will happily do so against an algorithm that
    verifies nothing unless the caller pins the list.
    """

    subject, fake = provider
    request = await subject.begin_login()
    claims = {
        "iss": ISSUER,
        "sub": "provider-subject-1",
        "aud": CLIENT_ID,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "nonce": request.nonce,
    }
    fake._pending_id_token = jwt.encode(
        claims, key="", algorithm="none", headers={"kid": "test-key"}
    )
    with pytest.raises(OidcTokenError):
        await subject.complete_login(
            code="c", code_verifier=request.code_verifier, expected_nonce=request.nonce
        )


async def test_a_token_signed_with_the_client_secret_is_refused(provider):
    """HMAC would make the shared secret a verification key.

    Anyone holding the client secret — which is a configuration value, not a
    signing key — could then mint tokens this boundary accepts. Pinning the
    algorithms to the asymmetric family is what prevents the confusion.
    """

    subject, fake = provider
    request = await subject.begin_login()
    claims = {
        "iss": ISSUER,
        "sub": "provider-subject-1",
        "aud": CLIENT_ID,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "nonce": request.nonce,
    }
    fake._pending_id_token = jwt.encode(
        claims, key="s3cret", algorithm="HS256", headers={"kid": "test-key"}
    )
    with pytest.raises(OidcTokenError):
        await subject.complete_login(
            code="c", code_verifier=request.code_verifier, expected_nonce=request.nonce
        )


async def test_an_ambiguous_key_set_without_a_key_id_is_refused(provider):
    """Two keys and no ``kid`` means picking one, which means accepting either."""

    subject, fake = provider
    request = await subject.begin_login()
    fake._pending_id_token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "s",
            "aud": CLIENT_ID,
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "nonce": request.nonce,
        },
        fake.key,
        algorithm="RS256",
    )
    from jwt.utils import to_base64url_uint

    second = fake.other_key.public_key().public_numbers()
    original = FakeProvider.jwks.fget

    def two_keys(self):
        key_set = original(self)
        key_set["keys"].append(
            {
                "kty": "RSA",
                "kid": "second-key",
                "use": "sig",
                "alg": "RS256",
                "n": to_base64url_uint(second.n).decode(),
                "e": to_base64url_uint(second.e).decode(),
            }
        )
        return key_set

    FakeProvider.jwks = property(two_keys)
    try:
        with pytest.raises(OidcTokenError, match="key id"):
            await subject.complete_login(
                code="c",
                code_verifier=request.code_verifier,
                expected_nonce=request.nonce,
            )
    finally:
        FakeProvider.jwks = property(original)
