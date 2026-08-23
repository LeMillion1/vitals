"""The two routes a federated login passes through, and the attacks on them.

``/auth/start`` builds the authorization request and seals its secrets in a
handoff cookie. ``/auth/callback`` proves the code that comes back belongs to
that request, and only then does a session exist. Everything between those two
points is under an attacker's nose — the browser carries it, and the provider's
redirect is a cross-site navigation — so the tests here are mostly about
callbacks that should not produce a session.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.utils import to_base64url_uint

from web.config import OIDC_HANDOFF_COOKIE, SESSION_COOKIE

ISSUER = "https://idp.example.test"
CLIENT_ID = "vitals-test"
REDIRECT = "https://vitals.example.test/auth/callback"
OWNER_SUBJECT = "provider-subject-owner"


class Provider:
    """A provider with real keys, reachable only through a mock transport."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pending_claims: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                    "code_challenge_methods_supported": ["S256"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if path.endswith("/keys"):
            numbers = self.key.public_key().public_numbers()
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kty": "RSA",
                            "kid": "k1",
                            "use": "sig",
                            "alg": "RS256",
                            "n": to_base64url_uint(numbers.n).decode(),
                            "e": to_base64url_uint(numbers.e).decode(),
                        }
                    ]
                },
            )
        if path.endswith("/token"):
            now = int(time.time())
            claims = {
                "iss": ISSUER,
                "sub": OWNER_SUBJECT,
                "aud": CLIENT_ID,
                "exp": now + 300,
                "iat": now,
                "auth_time": now,
                **self.pending_claims,
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "token_type": "Bearer",
                    "id_token": jwt.encode(
                        claims, self.key, algorithm="RS256", headers={"kid": "k1"}
                    ),
                },
            )
        return httpx.Response(404)


@pytest.fixture
def federated(monkeypatch):
    """Configure OIDC, and route the provider's endpoints to the fake."""

    import web.auth
    for name, value in (
        ("VITALS_OIDC_ISSUER", ISSUER),
        ("VITALS_OIDC_CLIENT_ID", CLIENT_ID),
        ("VITALS_OIDC_CLIENT_SECRET", "s3cret"),
        ("VITALS_OIDC_REDIRECT_URL", REDIRECT),
        ("VITALS_OIDC_BOOTSTRAP_SUBJECT", OWNER_SUBJECT),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(web.auth, "_provider_cache", None, raising=False)

    provider = Provider()
    transport = httpx.MockTransport(provider.handler)
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    yield provider


async def _start(client, provider) -> tuple[str, str]:
    """Begin a login, and tell the fake which nonce a real provider would echo.

    Echoing the nonce is the provider's job — it is what binds the token to
    this browser's request — and a fake that skips it would make every login
    fail for a reason that has nothing to do with the code under test.
    """

    from urllib.parse import parse_qs, urlsplit

    response = await client.get("/auth/start", follow_redirects=False)
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    provider.pending_claims["nonce"] = query["nonce"][0]
    return query["state"][0], response.cookies[OIDC_HANDOFF_COOKIE]


# ── Starting ─────────────────────────────────────────────────────────────────

async def test_start_sends_the_browser_to_the_provider_with_pkce(
    client, federated
):
    from urllib.parse import parse_qs, urlsplit

    response = await client.get("/auth/start", follow_redirects=False)
    assert response.status_code == 303

    target = urlsplit(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{ISSUER}/authorize"
    query = parse_qs(target.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"], "no challenge means the code binds to nobody"
    assert query["state"] and query["nonce"]
    # The verifier is the one thing that must never leave in the redirect.
    assert "code_verifier" not in query


async def test_the_handoff_cookie_never_carries_the_verifier_in_the_clear(
    client, federated
):
    response = await client.get("/auth/start", follow_redirects=False)
    handoff = response.cookies[OIDC_HANDOFF_COOKIE]
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(response.headers["location"]).query)
    # Signed and compressed, so the challenge that went to the provider must
    # not simply be readable beside it.
    assert query["code_challenge"][0] not in handoff


async def test_start_is_absent_when_no_provider_is_configured(client):
    assert (await client.get("/auth/start")).status_code == 404
    assert (await client.get("/auth/callback?code=c&state=s")).status_code == 404


# ── Completing ───────────────────────────────────────────────────────────────

async def test_a_complete_login_binds_the_existing_owner_and_issues_a_session(
    client, federated, db_session, legacy_owner_roots
):
    state, _ = await _start(client, federated)
    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.cookies.get(SESSION_COOKIE)
    # The handoff is spent: one callback per login request.
    assert response.cookies.get(OIDC_HANDOFF_COOKIE) in (None, "")

    from web.auth import decode_session

    claims = decode_session(response.cookies[SESSION_COOKIE])
    assert claims is not None and claims.version == 2
    assert claims.user_id == legacy_owner_roots.user_id
    assert claims.subject_id == legacy_owner_roots.subject_id


# ── Callbacks that must not produce a session ────────────────────────────────

@pytest.mark.parametrize(
    ("description", "query"),
    [
        ("no code at all", "state={state}"),
        ("no state at all", "code=c"),
        ("the provider declined", "error=access_denied&state={state}"),
    ],
)
async def test_an_incomplete_callback_is_refused(
    client, federated, description, query
):
    state, _ = await _start(client, federated)
    response = await client.get(
        f"/auth/callback?{query.format(state=state)}", follow_redirects=False
    )
    assert response.status_code == 401, description
    assert not response.cookies.get(SESSION_COOKIE)


async def test_a_callback_with_somebody_elses_state_is_refused(
    client, federated
):
    """Login CSRF: an attacker cannot complete their login in your browser."""

    await _start(client, federated)
    response = await client.get(
        "/auth/callback?code=c&state=a-state-from-another-browser",
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert not response.cookies.get(SESSION_COOKIE)


async def test_a_callback_with_no_handoff_at_all_is_refused(client, federated):
    """A code delivered straight to the callback, with no login behind it."""

    response = await client.get(
        "/auth/callback?code=c&state=anything", follow_redirects=False
    )
    assert response.status_code == 401
    assert not response.cookies.get(SESSION_COOKIE)


async def test_a_callback_naming_another_issuer_is_refused(client, federated):
    """RFC 9207 mix-up: the response says it came from somewhere we did not ask."""

    state, _ = await _start(client, federated)
    response = await client.get(
        f"/auth/callback?code=c&state={state}&iss=https://attacker.example.test",
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert not response.cookies.get(SESSION_COOKIE)


async def test_a_token_for_an_unknown_identity_is_refused(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    """A perfectly valid login by somebody with no account here."""

    monkeypatch.delenv("VITALS_OIDC_BOOTSTRAP_SUBJECT", raising=False)
    federated.pending_claims = {"sub": "a-stranger"}

    state, _ = await _start(client, federated)
    response = await client.get(
        f"/auth/callback?code=c&state={state}", follow_redirects=False
    )
    assert response.status_code == 401
    assert not response.cookies.get(SESSION_COOKIE)


async def test_every_refusal_renders_the_same_page(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    """A stranger cannot tell which check refused them.

    "No such account", "your account is suspended" and "that token was not for
    us" are three sentences and one fact: it did not work.
    """

    bodies = []

    state, _ = await _start(client, federated)
    bodies.append(
        (await client.get(
            f"/auth/callback?code=c&state={state}&iss=https://attacker.test",
            follow_redirects=False,
        )).text
    )

    monkeypatch.delenv("VITALS_OIDC_BOOTSTRAP_SUBJECT", raising=False)
    federated.pending_claims = {"sub": "a-stranger"}
    state, _ = await _start(client, federated)
    bodies.append(
        (await client.get(
            f"/auth/callback?code=c&state={state}", follow_redirects=False
        )).text
    )

    # The strong property: byte-identical, so there is nothing to compare.
    assert bodies[0] == bodies[1]
    # And the specific things that must never appear, whichever check refused.
    for body in bodies:
        for leak in (
            "attacker.test",
            "a-stranger",
            ISSUER,
            OWNER_SUBJECT,
            "no account on this installation",
            "different issuer",
        ):
            assert leak not in body, leak


# ── The cutover ──────────────────────────────────────────────────────────────
#
# Hard, and switched by configuration rather than by deployment: while no
# provider is configured the old login still works, and setting the issuer is
# what closes it. The switch therefore happens when there is somewhere to
# switch to, which is the difference between a cutover and a lockout.


async def test_before_the_cutover_the_password_login_still_works(client):
    """Shipping this code changes nothing until a provider is configured."""

    assert (await client.get("/login")).status_code == 200
    assert (await client.get("/auth/start")).status_code == 404


async def test_after_the_cutover_the_login_page_sends_you_to_the_provider(
    client, federated
):
    response = await client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/start")


async def test_after_the_cutover_the_login_page_keeps_where_you_were_going(
    client, federated
):
    response = await client.get("/login?next=/labs", follow_redirects=False)
    assert "next=%2Flabs" in response.headers["location"]


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/login"), ("get", "/login/2fa"), ("post", "/login/2fa")],
)
async def test_after_the_cutover_the_password_paths_are_gone(
    client, federated, method, path
):
    """404, not 403: these are not things this installation has any more."""

    if method == "get":
        response = await client.get(path)
    else:
        response = await client.post(
            path, data={"username": "x", "password": "y", "code": "123456"}
        )
    assert response.status_code == 404, path


@pytest.mark.parametrize(
    "path",
    ["/settings/2fa/start", "/settings/2fa/enable", "/settings/2fa/disable"],
)
async def test_after_the_cutover_enrolling_a_second_factor_here_is_gone(
    auth_client, federated, path
):
    """Authenticated, so the 404 is the cutover rather than the session guard.

    An anonymous request gets 401 first, which is correct and proves nothing
    about whether the route still exists.
    """

    response = await auth_client.post(path, data={"code": "123456"})
    assert response.status_code == 404, path


async def test_after_the_cutover_a_stored_password_hash_is_not_a_way_in(
    client, federated
):
    """The pre-cutover owner's bcrypt hash survives in the column, unused.

    ``authenticate`` refuses before it reaches the hash, so a route that
    somehow called it — or a future one that did — still gets nothing.
    """

    from web.auth import authenticate
    from web.config import get_web_config

    cfg = get_web_config()
    assert cfg.oidc_enabled
    assert authenticate(cfg.auth_username, "the-real-password") is False
