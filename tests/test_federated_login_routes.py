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
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.utils import to_base64url_uint

from web.config import (
    OIDC_HANDOFF_COOKIE,
    PENDING_2FA_COOKIE,
    REGISTRATION_ADMISSION_COOKIE,
    REGISTRATION_REQUEST_COOKIE,
    SESSION_COOKIE,
)

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
                    "end_session_endpoint": f"{ISSUER}/logout",
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
        ("VITALS_PUBLIC_URL", "https://vitals.example.test"),
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


async def _account_invitation(db_session, legacy_owner_roots, monkeypatch, *, email):
    from vitals.enums import RegistrationAccountKind
    from vitals.models.identity import User
    from vitals.services.authentication import admission, registration

    monkeypatch.setenv(registration.REGISTRATION_UNLOCK_ENV, "1")
    await registration.set_stored_mode(
        db_session,
        registration.RegistrationMode.INVITE_ONLY,
    )
    owner = await db_session.get(User, legacy_owner_roots.user_id)
    issued = await admission.issue_invitation(
        db_session,
        actor_user_id=owner.id,
        email=email,
        account_kind=RegistrationAccountKind.MEMBER,
    )
    await db_session.commit()
    return issued


async def _enable_admin_approval(db_session, monkeypatch) -> None:
    from vitals.services.authentication import registration

    monkeypatch.setenv(registration.REGISTRATION_UNLOCK_ENV, "1")
    await registration.set_stored_mode(
        db_session,
        registration.RegistrationMode.ADMIN_APPROVED,
    )
    await db_session.commit()


async def _set_owner_federated_cookie(client, db_session, legacy_owner_roots):
    from vitals.models.identity import User
    from web.auth import create_federated_session

    user = await db_session.get(User, legacy_owner_roots.user_id)
    assert user is not None
    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username=user.username,
            user_id=user.id,
            session_version=user.session_version,
            authenticated_at=int(time.time()),
            subject_id=legacy_owner_roots.subject_id,
        ),
    )


async def _open_request_status(client, callback, *, expected_status: int):
    """Follow the clean signed handoff without preserving OAuth parameters."""

    assert callback.status_code == 303
    assert callback.headers["location"] == "/auth/registration-request"
    assert "no-store" in callback.headers["cache-control"]
    assert callback.cookies.get(REGISTRATION_REQUEST_COOKIE)
    page = await client.get(callback.headers["location"], follow_redirects=False)
    assert page.status_code == expected_status
    return page


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


async def test_step_up_forces_provider_login_and_rejects_stale_authentication(
    client, federated
):
    from urllib.parse import parse_qs, urlsplit

    response = await client.get(
        "/auth/start?step_up=true&next=/settings/access",
        follow_redirects=False,
    )
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["prompt"] == ["login"]
    federated.pending_claims.update(
        {
            "nonce": query["nonce"][0],
            "auth_time": int(time.time()) - 3600,
        }
    )

    refused = await client.get(
        f"/auth/callback?code=the-code&state={query['state'][0]}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert refused.status_code == 401
    assert not refused.cookies.get(SESSION_COOKIE)


async def test_sensitive_support_action_redirects_a_stale_session_to_step_up(
    client, federated, db_session, legacy_owner_roots
):
    from web.auth import create_federated_session
    from vitals.models.identity import User

    user = await db_session.get(User, legacy_owner_roots.user_id)
    assert user is not None
    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username=user.username,
            user_id=user.id,
            session_version=user.session_version,
            authenticated_at=int(time.time()) - 3600,
            subject_id=legacy_owner_roots.subject_id,
        ),
    )
    response = await client.post(
        f"/settings/access/{uuid.uuid4()}/approve",
        headers={
            "Accept": "text/html",
            "Referer": "http://test/settings/access",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    target = response.headers["location"]
    assert target.startswith("/auth/start?")
    assert "step_up=true" in target
    assert "next=%2Fsettings%2Faccess" in target


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


# ── Invitation handoff ──────────────────────────────────────────────────────

_EXCHANGE_HEADERS = {
    "Origin": "http://test",
    "Sec-Fetch-Site": "same-origin",
}


async def test_invitation_landing_scrubs_before_any_other_script(
    client, federated
):
    response = await client.get("/register/invite#raw-secret")

    assert response.status_code == 200
    assert "raw-secret" not in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert "script-src 'nonce-" in csp
    assert "unsafe-inline" not in csp.split("style-src", 1)[0]
    assert "cloudflare" not in csp
    source = response.text
    nonce = source.split('<script nonce="', 1)[1].split('"', 1)[0]
    assert f"script-src 'nonce-{nonce}'" in csp
    assert source.index("history.replaceState") < source.index("fetch(")
    assert source.index("if (!scrubbed)") < source.index("fetch(")
    assert source.count("<script") == 1
    assert "base.html" not in source
    assert "app.js" not in source


async def test_invitation_exchange_requires_a_same_origin_browser(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="origin-bound@example.test",
    )
    attempts = (
        {},
        {"Origin": "http://test", "Sec-Fetch-Site": "same-site"},
        {"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )
    for index, headers in enumerate(attempts):
        response = await client.post(
            "/register/invite/exchange",
            json={"token": issued.token},
            headers=headers,
        )
        assert response.status_code == 403
        if index < 2:
            assert response.json() == {"ok": False}
        else:
            # The global CSRF middleware rejects an explicitly cross-site
            # request before the route's stricter same-origin contract runs.
            assert response.content == b"Cross-site request refused."
        assert REGISTRATION_ADMISSION_COOKIE not in response.cookies


async def test_invitation_exchange_mints_only_an_opaque_short_lived_cookie(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from web.admission_handoff import read_invitation_claim

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="cookie-proof@example.test",
    )
    response = await client.post(
        "/register/invite/exchange",
        json={"token": issued.token},
        headers=_EXCHANGE_HEADERS,
    )

    assert response.status_code == 200
    claim = response.cookies[REGISTRATION_ADMISSION_COOKIE]
    assert read_invitation_claim(claim) == issued.invitation.id
    cookie_header = response.headers["set-cookie"]
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header
    assert "Path=/auth" in cookie_header
    assert "Max-Age=600" in cookie_header
    for private in (
        issued.token,
        issued.invitation.token_digest,
        "cookie-proof@example.test",
    ):
        assert private not in cookie_header
        assert private not in response.text


async def test_invitation_exchange_ends_every_previous_local_login_handle(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from web.auth import (
        create_oidc_handoff,
        create_pending_2fa,
        create_session,
    )

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="shared-device@example.test",
    )
    client.cookies.set(
        SESSION_COOKIE,
        create_session("previous-person"),
        domain="test.local",
        path="/",
    )
    client.cookies.set(
        PENDING_2FA_COOKIE,
        create_pending_2fa("previous-person"),
        domain="test.local",
        path="/",
    )
    client.cookies.set(
        OIDC_HANDOFF_COOKIE,
        create_oidc_handoff(
            state="old-state",
            nonce="old-nonce",
            code_verifier="old-verifier",
            next_url="/",
        ),
        domain="test.local",
        path="/",
    )

    response = await client.post(
        "/register/invite/exchange",
        json={"token": issued.token},
        headers=_EXCHANGE_HEADERS,
    )

    assert response.status_code == 200
    set_cookie = response.headers.get_list("set-cookie")
    assert any(value.startswith(f"{SESSION_COOKIE}=") for value in set_cookie)
    assert any(value.startswith(f"{PENDING_2FA_COOKIE}=") for value in set_cookie)
    assert any(value.startswith(f"{OIDC_HANDOFF_COOKIE}=") for value in set_cookie)
    assert client.cookies.get(SESSION_COOKIE) is None
    assert client.cookies.get(PENDING_2FA_COOKIE) is None
    assert client.cookies.get(OIDC_HANDOFF_COOKIE) is None

    # Cancelling the fresh provider ceremony cannot reveal the previous
    # person's health data again.
    state, _ = await _start(client, federated)
    refused = await client.get(
        f"/auth/callback?error=access_denied&state={state}",
        follow_redirects=False,
    )
    assert refused.status_code == 401
    protected = await client.get("/today", follow_redirects=False)
    assert protected.status_code in {302, 303, 401}
    if protected.status_code in {302, 303}:
        assert protected.headers["location"].startswith("/login")


def test_invitation_claim_codec_rejects_tampering_extension_and_expiry(monkeypatch):
    from itsdangerous import TimestampSigner, URLSafeTimedSerializer

    from web.admission_handoff import (
        create_invitation_claim,
        read_invitation_claim,
    )
    from web.config import REGISTRATION_ADMISSION_TTL, get_web_config

    invitation_id = uuid.uuid4()
    claim = create_invitation_claim(invitation_id)
    signed_value, signature = claim.rsplit(".", 1)
    replacement = "A" if signature[0] != "A" else "B"
    tampered = f"{signed_value}.{replacement}{signature[1:]}"
    assert read_invitation_claim(tampered) is None

    serializer = URLSafeTimedSerializer(
        get_web_config().session_secret,
        salt="vitals-registration-admission",
    )
    extended = serializer.dumps(
        {
            "v": 1,
            "type": "registration_invitation",
            "invitation_id": str(invitation_id),
            "email": "must-not-be-accepted@example.test",
        }
    )
    assert read_invitation_claim(extended) is None

    get_timestamp = TimestampSigner.get_timestamp
    monkeypatch.setattr(
        TimestampSigner,
        "get_timestamp",
        lambda self: get_timestamp(self) + REGISTRATION_ADMISSION_TTL + 1,
    )
    assert read_invitation_claim(claim) is None


async def test_invitation_exchange_refusals_are_uniform_and_bounded(
    client, federated
):
    responses = []
    for body in (
        {"token": "not-issued"},
        {"token": " x "},
        {"token": "x" * 513},
        {"token": False},
        {"wrong": "shape"},
    ):
        responses.append(
            await client.post(
                "/register/invite/exchange",
                json=body,
                headers=_EXCHANGE_HEADERS,
            )
        )
    assert {(response.status_code, response.content) for response in responses} == {
        (401, b'{"ok":false}')
    }

    oversized = await client.post(
        "/register/invite/exchange",
        content=b"x" * 1025,
        headers={**_EXCHANGE_HEADERS, "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413


async def test_invitation_forces_fresh_provider_login_even_with_a_local_session(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from urllib.parse import parse_qs, urlsplit

    from web.auth import create_session, read_oidc_handoff

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="fresh-login@example.test",
    )
    exchange = await client.post(
        "/register/invite/exchange",
        json={"token": issued.token},
        headers=_EXCHANGE_HEADERS,
    )
    assert exchange.status_code == 200
    client.cookies.set(SESSION_COOKIE, create_session("tester"))

    response = await client.get("/auth/start", follow_redirects=False)
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["prompt"] == ["login"]
    assert query["max_age"] == ["900"]
    handoff = read_oidc_handoff(response.cookies[OIDC_HANDOFF_COOKIE])
    assert handoff["admission_type"] == "registration_invitation"
    assert handoff["invitation_id"] == issued.invitation.id
    assert issued.token not in response.headers["location"]
    assert issued.token not in response.cookies[OIDC_HANDOFF_COOKIE]


async def test_pre_cutover_cookie_cannot_skip_or_enter_oidc_mode(
    client, federated, db_session
):
    from starlette.requests import Request

    from web.auth import create_session, read_session
    from web.deps import NotAuthenticated, require_auth

    token = create_session("legacy-owner")
    assert read_session(token) is None

    request = Request(
        {
            "type": "http",
            "headers": [
                (b"cookie", f"{SESSION_COOKIE}={token}".encode("ascii")),
            ],
        }
    )
    with pytest.raises(NotAuthenticated):
        await require_auth(request, db_session)

    client.cookies.set(SESSION_COOKIE, token)
    response = await client.get("/auth/start", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"{ISSUER}/authorize?")


async def test_step_up_ignores_an_invitation_claim(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from urllib.parse import parse_qs, urlsplit

    from web.auth import read_oidc_handoff

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="step-up-isolated@example.test",
    )
    assert (
        await client.post(
            "/register/invite/exchange",
            json={"token": issued.token},
            headers=_EXCHANGE_HEADERS,
        )
    ).status_code == 200

    response = await client.get(
        "/auth/start?step_up=true&next=/settings/access",
        follow_redirects=False,
    )

    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["prompt"] == ["login"]
    assert query["max_age"] == ["900"]
    handoff = read_oidc_handoff(response.cookies[OIDC_HANDOFF_COOKIE])
    assert handoff["admission_type"] is None
    assert handoff["invitation_id"] is None
    assert client.cookies.get(REGISTRATION_ADMISSION_COOKIE)


async def test_invitation_handoff_creates_one_member_and_session(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import select

    from vitals.enums import RegistrationInvitationStatus, UserRoleName
    from vitals.models.identity import HealthSubject, UserFederatedIdentity, UserRole
    from vitals.models.registration import RegistrationInvitation

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="new-member@example.test",
    )
    invitation_id = issued.invitation.id
    exchange = await client.post(
        "/register/invite/exchange",
        json={"token": issued.token},
        headers=_EXCHANGE_HEADERS,
    )
    assert exchange.status_code == 200
    federated.pending_claims.update(
        {
            "sub": "invited-member-subject",
            "email": "NEW-MEMBER@example.test",
            "email_verified": True,
            "preferred_username": "invited-member",
        }
    )
    state, _handoff = await _start(client, federated)

    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.cookies.get(SESSION_COOKIE)
    assert response.cookies.get(OIDC_HANDOFF_COOKIE) in (None, "")
    assert response.cookies.get(REGISTRATION_ADMISSION_COOKIE) in (None, "")
    db_session.expire_all()
    invitation = await db_session.get(RegistrationInvitation, invitation_id)
    assert invitation.status == RegistrationInvitationStatus.CONSUMED.value
    link = await db_session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "invited-member-subject"
        )
    )
    role = await db_session.scalar(
        select(UserRole).where(UserRole.user_id == link.user_id)
    )
    record = await db_session.scalar(
        select(HealthSubject).where(HealthSubject.owner_user_id == link.user_id)
    )
    assert role.role == UserRoleName.MEMBER.value
    assert record is not None


async def test_linked_identity_signs_in_without_consuming_an_invitation(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from vitals.enums import RegistrationInvitationStatus
    from vitals.models.registration import RegistrationInvitation

    # First bind the configured bootstrap identity normally.
    state, _ = await _start(client, federated)
    first = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert first.status_code == 303

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="unused-linked-invite@example.test",
    )
    invitation_id = issued.invitation.id
    assert (
        await client.post(
            "/register/invite/exchange",
            json={"token": issued.token},
            headers=_EXCHANGE_HEADERS,
        )
    ).status_code == 200
    state, _ = await _start(client, federated)

    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.cookies.get(SESSION_COOKIE)
    db_session.expire_all()
    invitation = await db_session.get(RegistrationInvitation, invitation_id)
    assert invitation.status == RegistrationInvitationStatus.PENDING.value


async def test_bootstrap_identity_does_not_consume_or_duplicate_an_invitation(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.enums import RegistrationInvitationStatus
    from vitals.models.identity import User, UserFederatedIdentity
    from vitals.models.registration import RegistrationInvitation

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="unused-bootstrap-invite@example.test",
    )
    invitation_id = issued.invitation.id
    assert (
        await client.post(
            "/register/invite/exchange",
            json={"token": issued.token},
            headers=_EXCHANGE_HEADERS,
        )
    ).status_code == 200
    state, _ = await _start(client, federated)

    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.expire_all()
    invitation = await db_session.get(RegistrationInvitation, invitation_id)
    assert invitation.status == RegistrationInvitationStatus.PENDING.value
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1
    link = await db_session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == OWNER_SUBJECT
        )
    )
    assert link.user_id == legacy_owner_roots.user_id


async def test_invitation_mode_closure_after_exchange_refuses_without_graph(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.enums import RegistrationInvitationStatus
    from vitals.models.identity import UserFederatedIdentity
    from vitals.models.registration import RegistrationInvitation
    from vitals.services.authentication import registration

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="closed-during-oidc@example.test",
    )
    invitation_id = issued.invitation.id
    assert (
        await client.post(
            "/register/invite/exchange",
            json={"token": issued.token},
            headers=_EXCHANGE_HEADERS,
        )
    ).status_code == 200
    await registration.set_stored_mode(
        db_session,
        registration.RegistrationMode.DISABLED,
    )
    await db_session.commit()
    federated.pending_claims.update(
        {
            "sub": "closed-during-oidc-subject",
            "email": "closed-during-oidc@example.test",
            "email_verified": True,
        }
    )
    state, _ = await _start(client, federated)

    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert not response.cookies.get(SESSION_COOKIE)
    assert client.cookies.get(REGISTRATION_ADMISSION_COOKIE)
    db_session.expire_all()
    invitation = await db_session.get(RegistrationInvitation, invitation_id)
    assert invitation.status == RegistrationInvitationStatus.PENDING.value
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "closed-during-oidc-subject"
        )
    ) == 0


async def test_invitation_revocation_after_exchange_refuses_without_graph(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.models.identity import User, UserFederatedIdentity
    from vitals.services.authentication import admission

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="revoked-during-oidc@example.test",
    )
    assert (
        await client.post(
            "/register/invite/exchange",
            json={"token": issued.token},
            headers=_EXCHANGE_HEADERS,
        )
    ).status_code == 200
    owner = await db_session.get(User, legacy_owner_roots.user_id)
    await admission.revoke_invitation(
        db_session,
        invitation_id=issued.invitation.id,
        actor_user_id=owner.id,
    )
    await db_session.commit()
    federated.pending_claims.update(
        {
            "sub": "revoked-during-oidc-subject",
            "email": "revoked-during-oidc@example.test",
            "email_verified": True,
        }
    )
    state, _ = await _start(client, federated)

    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "revoked-during-oidc-subject"
        )
    ) == 0


async def test_spent_invitation_claim_cannot_open_a_second_identity(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.models.identity import UserFederatedIdentity

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="one-account-only@example.test",
    )
    exchange = await client.post(
        "/register/invite/exchange",
        json={"token": issued.token},
        headers=_EXCHANGE_HEADERS,
    )
    saved_claim = exchange.cookies[REGISTRATION_ADMISSION_COOKIE]
    federated.pending_claims.update(
        {
            "sub": "first-invited-subject",
            "email": "one-account-only@example.test",
            "email_verified": True,
        }
    )
    state, _ = await _start(client, federated)
    first = await client.get(
        f"/auth/callback?code=first-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert first.status_code == 303
    # The test dependency shares one session; production commits at each HTTP
    # boundary, so make the first successful callback durable before replay.
    await db_session.commit()

    client.cookies.clear()
    client.cookies.set(
        REGISTRATION_ADMISSION_COOKIE,
        saved_claim,
        domain="test.local",
        path="/auth",
    )
    federated.pending_claims.update(
        {
            "sub": "second-invited-subject",
            "email": "one-account-only@example.test",
            "email_verified": True,
        }
    )
    state, _ = await _start(client, federated)
    replay = await client.get(
        f"/auth/callback?code=second-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert replay.status_code == 401
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity).where(
            UserFederatedIdentity.subject.in_(
                {"first-invited-subject", "second-invited-subject"}
            )
        )
    ) == 1


async def test_admission_refusal_rolls_back_a_partially_flushed_identity(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.models.identity import UserFederatedIdentity
    from vitals.services.authentication import admission

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="rollback-admission@example.test",
    )
    assert (
        await client.post(
            "/register/invite/exchange",
            json={"token": issued.token},
            headers=_EXCHANGE_HEADERS,
        )
    ).status_code == 200
    federated.pending_claims.update(
        {
            "sub": "partially-flushed-subject",
            "email": "rollback-admission@example.test",
            "email_verified": True,
        }
    )

    async def refuse_after_flush(session, **_kwargs):
        session.add(
            UserFederatedIdentity(
                user_id=legacy_owner_roots.user_id,
                issuer=ISSUER,
                subject="partially-flushed-subject",
            )
        )
        await session.flush()
        raise admission.AdmissionRefused(
            "this admission proof does not open an account"
        )

    monkeypatch.setattr(admission, "consume_invitation_claim", refuse_after_flush)
    state, _ = await _start(client, federated)
    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "partially-flushed-subject"
        )
    ) == 0


async def test_callback_requires_the_same_signed_invitation_claim(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="browser-bound@example.test",
    )
    assert (
        await client.post(
            "/register/invite/exchange",
            json={"token": issued.token},
            headers=_EXCHANGE_HEADERS,
        )
    ).status_code == 200
    federated.pending_claims.update(
        {
            "sub": "browser-bound-subject",
            "email": "browser-bound@example.test",
            "email_verified": True,
        }
    )
    state, _ = await _start(client, federated)
    client.cookies.delete(REGISTRATION_ADMISSION_COOKIE)
    client.cookies.set(
        REGISTRATION_ADMISSION_COOKIE,
        "not-a-signed-claim",
        path="/auth",
    )

    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert not response.cookies.get(SESSION_COOKIE)
    assert response.cookies.get(REGISTRATION_ADMISSION_COOKIE) in (None, "")


@pytest.mark.parametrize(
    "claims",
    [
        {"email": "wrong@example.test", "email_verified": True},
        {"email": "bound@example.test", "email_verified": False},
    ],
)
async def test_invitation_callback_refuses_wrong_or_unverified_address_without_graph(
    client,
    federated,
    db_session,
    legacy_owner_roots,
    monkeypatch,
    claims,
):
    from sqlalchemy import func, select

    from vitals.enums import RegistrationInvitationStatus
    from vitals.models.identity import UserFederatedIdentity
    from vitals.models.registration import RegistrationInvitation

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="bound@example.test",
    )
    invitation_id = issued.invitation.id
    assert (
        await client.post(
            "/register/invite/exchange",
            json={"token": issued.token},
            headers=_EXCHANGE_HEADERS,
        )
    ).status_code == 200
    federated.pending_claims.update({"sub": "refused-invite-subject", **claims})
    state, _ = await _start(client, federated)

    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert not response.cookies.get(SESSION_COOKIE)
    db_session.expire_all()
    invitation = await db_session.get(RegistrationInvitation, invitation_id)
    assert invitation.status == RegistrationInvitationStatus.PENDING.value
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "refused-invite-subject"
        )
    ) == 0


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


async def test_callback_persists_only_the_providers_verified_email_claim(
    client, federated, db_session, legacy_owner_roots
):
    """Care invitations use the current validated claim, not profile input."""

    from vitals.models.identity import User

    federated.pending_claims.update(
        {"email": "Doctor@Example.TEST", "email_verified": True}
    )
    state, _ = await _start(client, federated)
    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert response.status_code == 303

    db_session.expire_all()
    user = await db_session.get(User, legacy_owner_roots.user_id)
    assert user.email == "Doctor@Example.TEST"
    assert user.normalized_email == "doctor@example.test"
    assert user.email_verified_at is not None


async def test_a_late_claim_refusal_rolls_back_the_bootstrap_link(
    client, federated, db_session, legacy_owner_roots
):
    """A handled 401 must not commit identity mutations made before refusal."""

    from sqlalchemy import select

    from vitals.models.identity import User, UserFederatedIdentity
    from vitals.utils.timeutils import now_utc

    conflicting = User(
        username="mailbox-holder",
        normalized_username="mailbox-holder",
        email="shared@example.test",
        normalized_email="shared@example.test",
        email_verified_at=now_utc(),
        password_hash="!locked",
        status="active",
    )
    db_session.add(conflicting)
    await db_session.commit()

    federated.pending_claims.update(
        {"email": "SHARED@example.test", "email_verified": True}
    )
    state, _ = await _start(client, federated)
    response = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert response.status_code == 401

    db_session.expire_all()
    assert await db_session.scalar(select(UserFederatedIdentity.id)) is None
    owner = await db_session.get(User, legacy_owner_roots.user_id)
    assert owner.email is None


async def test_logout_ends_both_the_local_and_provider_sessions(
    client, federated, db_session, legacy_owner_roots
):
    from urllib.parse import parse_qs, urlsplit

    state, _ = await _start(client, federated)
    signed_in = await client.get(
        f"/auth/callback?code=the-code&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert signed_in.cookies.get(SESSION_COOKIE)

    response = await client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{ISSUER}/logout"
    assert parse_qs(target.query) == {
        "client_id": [CLIENT_ID],
        "post_logout_redirect_uri": ["https://vitals.example.test/"],
    }
    assert response.cookies.get(SESSION_COOKIE) in (None, "")
    del db_session, legacy_owner_roots


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


async def test_admin_approved_login_creates_only_one_waiting_request(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.models.identity import User, UserFederatedIdentity
    from vitals.models.registration import RegistrationRequest
    from web.auth import create_session

    await _enable_admin_approval(db_session, monkeypatch)
    claims = {
        "sub": "approval-applicant",
        "email": "Private.Applicant@example.test",
        "email_verified": True,
        "preferred_username": "untrusted-public-name",
    }
    federated.pending_claims = claims
    state, _ = await _start(client, federated)

    response = await client.get(
        f"/auth/callback?code=c&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    page = await _open_request_status(client, response, expected_status=202)
    assert "no-store" in page.headers["cache-control"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "script-src 'nonce-" in page.headers["content-security-policy"]
    assert "noindex" in page.headers["x-robots-tag"]
    assert "htmx-history-cache" in page.text
    assert page.cookies.get(REGISTRATION_REQUEST_COOKIE) in (None, "")
    assert not response.cookies.get(SESSION_COOKIE)
    row = await db_session.scalar(select(RegistrationRequest))
    assert row is not None and str(row.id) in page.text
    for private in (
        "Private.Applicant@example.test",
        "approval-applicant",
        "untrusted-public-name",
        ISSUER,
    ):
        assert private not in page.text
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity)
    ) == 0

    fresh_session = create_session("fresh-session-after-status")
    client.cookies.set(SESSION_COOKIE, fresh_session)
    spent = await client.get(
        "/auth/registration-request",
        follow_redirects=False,
    )
    assert spent.status_code == 401
    assert client.cookies.get(SESSION_COOKIE) == fresh_session
    client.cookies.delete(SESSION_COOKIE)

    federated.pending_claims = claims
    state, _ = await _start(client, federated)
    repeated = await client.get(
        f"/auth/callback?code=c2&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    repeated_page = await _open_request_status(
        client,
        repeated,
        expected_status=202,
    )
    assert str(row.id) in repeated_page.text
    assert await db_session.scalar(
        select(func.count()).select_from(RegistrationRequest)
    ) == 1


async def test_admin_approval_deployment_gate_cannot_be_bypassed_by_callback(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.models.registration import RegistrationRequest
    from vitals.services.authentication import registration

    await _enable_admin_approval(db_session, monkeypatch)
    monkeypatch.delenv(registration.REGISTRATION_UNLOCK_ENV)
    federated.pending_claims = {
        "sub": "locked-admin-approval-applicant",
        "email": "locked@example.test",
        "email_verified": True,
    }
    state, _ = await _start(client, federated)

    response = await client.get(
        f"/auth/callback?code=c&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert not response.cookies.get(SESSION_COOKIE)
    assert await db_session.scalar(
        select(func.count()).select_from(RegistrationRequest)
    ) == 0


async def test_request_is_not_acknowledged_before_its_commit(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.models.registration import RegistrationRequest

    await _enable_admin_approval(db_session, monkeypatch)
    federated.pending_claims = {
        "sub": "commit-failure-applicant",
        "email": "commit-failure@example.test",
        "email_verified": True,
    }
    state, _ = await _start(client, federated)
    real_commit = db_session.commit

    async def fail_commit():
        raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(db_session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        await client.get(
            f"/auth/callback?code=c&state={state}&iss={ISSUER}",
            follow_redirects=False,
        )
    await db_session.rollback()
    monkeypatch.setattr(db_session, "commit", real_commit)
    assert await db_session.scalar(
        select(func.count()).select_from(RegistrationRequest)
    ) == 0


async def test_registration_request_status_requires_its_signed_handoff(client):
    client.cookies.set(REGISTRATION_REQUEST_COOKIE, "not-a-signed-status")

    response = await client.get(
        "/auth/registration-request",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.cookies.get(REGISTRATION_REQUEST_COOKIE) in (None, "")


async def test_rejected_applicant_sees_closed_state_without_review_details(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import select

    from vitals.models.identity import User
    from vitals.models.registration import RegistrationRequest
    from vitals.services.authentication import admission

    await _enable_admin_approval(db_session, monkeypatch)
    claims = {
        "sub": "rejected-applicant",
        "email": "rejected@example.test",
        "email_verified": True,
    }
    federated.pending_claims = claims
    state, _ = await _start(client, federated)
    waiting = await client.get(
        f"/auth/callback?code=c&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert waiting.status_code == 303
    assert waiting.headers["location"] == "/auth/registration-request"
    assert waiting.cookies.get(REGISTRATION_REQUEST_COOKIE)
    request_row = await db_session.scalar(select(RegistrationRequest))
    reviewer = await db_session.get(User, legacy_owner_roots.user_id)
    private_reason = "Internal review detail that must stay private."
    await admission.reject_request(
        db_session,
        request_id=request_row.id,
        reviewer_user_id=reviewer.id,
        reason=private_reason,
    )
    await db_session.commit()

    federated.pending_claims = claims
    state, _ = await _start(client, federated)
    closed = await client.get(
        f"/auth/callback?code=c2&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    closed_page = await _open_request_status(client, closed, expected_status=200)
    assert str(request_row.id) in closed_page.text
    assert private_reason not in closed_page.text
    assert claims["email"] not in closed_page.text
    assert claims["sub"] not in closed_page.text
    assert not closed.cookies.get(SESSION_COOKIE)


async def test_stale_invitation_callback_never_falls_back_to_account_request(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.models.registration import RegistrationRequest
    from vitals.services.authentication import registration

    issued = await _account_invitation(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        email="stale-invitation@example.test",
    )
    exchange = await client.post(
        "/register/invite/exchange",
        json={"token": issued.token},
        headers=_EXCHANGE_HEADERS,
    )
    assert exchange.status_code == 200
    federated.pending_claims.update(
        {
            "sub": "stale-invitation-subject",
            "email": "stale-invitation@example.test",
            "email_verified": True,
        }
    )
    state, _ = await _start(client, federated)
    await registration.set_stored_mode(
        db_session,
        registration.RegistrationMode.ADMIN_APPROVED,
    )
    await db_session.commit()

    response = await client.get(
        f"/auth/callback?code=c&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert not response.cookies.get(SESSION_COOKIE)
    assert await db_session.scalar(
        select(func.count()).select_from(RegistrationRequest)
    ) == 0


async def test_approved_request_becomes_a_normal_member_login(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import select

    from vitals.models.identity import HealthSubject, User, UserFederatedIdentity
    from vitals.models.registration import RegistrationRequest
    from vitals.services.authentication import admission

    await _enable_admin_approval(db_session, monkeypatch)
    claims = {
        "sub": "approved-applicant",
        "email": "approved-applicant@example.test",
        "email_verified": True,
        "preferred_username": "approved-applicant",
    }
    federated.pending_claims = claims
    state, _ = await _start(client, federated)
    waiting = await client.get(
        f"/auth/callback?code=c&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    await _open_request_status(client, waiting, expected_status=202)
    request_row = await db_session.scalar(select(RegistrationRequest))
    reviewer = await db_session.get(User, legacy_owner_roots.user_id)
    await admission.approve_request(
        db_session,
        request_id=request_row.id,
        reviewer_user_id=reviewer.id,
        expected_issuer=ISSUER,
    )
    await db_session.commit()

    federated.pending_claims = claims
    state, _ = await _start(client, federated)
    assert client.cookies.get(REGISTRATION_REQUEST_COOKIE) is None
    admitted = await client.get(
        f"/auth/callback?code=c2&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert admitted.status_code == 303
    assert admitted.cookies.get(SESSION_COOKIE)
    spent_status = await client.get(
        "/auth/registration-request",
        follow_redirects=False,
    )
    assert spent_status.status_code == 401
    assert client.cookies.get(SESSION_COOKIE) == admitted.cookies.get(SESSION_COOKIE)
    link = await db_session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "approved-applicant"
        )
    )
    assert link is not None
    assert await db_session.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == link.user_id)
    ) is not None


async def test_step_up_and_failed_bootstrap_never_submit_account_requests(
    client, federated, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.enums import UserStatus
    from vitals.models.identity import User
    from vitals.models.registration import RegistrationRequest

    await _enable_admin_approval(db_session, monkeypatch)
    federated.pending_claims = {
        "sub": "step-up-stranger",
        "email": "step-up@example.test",
        "email_verified": True,
    }
    started = await client.get("/auth/start?step_up=true", follow_redirects=False)
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(started.headers["location"]).query)
    federated.pending_claims["nonce"] = query["nonce"][0]
    step_up = await client.get(
        f"/auth/callback?code=c&state={query['state'][0]}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert step_up.status_code == 401

    db_session.add(
        User(
            username="second-bootstrap-user",
            normalized_username="second-bootstrap-user",
            password_hash="$synthetic-hash",
            status=UserStatus.ACTIVE.value,
        )
    )
    await db_session.commit()
    federated.pending_claims = {
        "sub": OWNER_SUBJECT,
        "email": "bootstrap@example.test",
        "email_verified": True,
    }
    state, _ = await _start(client, federated)
    bootstrap = await client.get(
        f"/auth/callback?code=c2&state={state}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert bootstrap.status_code == 401
    assert await db_session.scalar(
        select(func.count()).select_from(RegistrationRequest)
    ) == 0


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
    # The OIDC cutover removed the password endpoint. A refusal must therefore
    # offer one working OIDC retry instead of the legacy form whose POST is 404.
    assert 'action="/login"' not in bodies[0]
    assert 'name="password"' not in bodies[0]
    assert bodies[0].count("data-oidc-retry") == 1
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


@pytest.mark.parametrize(
    ("next_url", "retry_target", "must_not_leak"),
    [
        ("/labs?tab=latest", "/auth/start?next=%2Flabs%3Ftab%3Dlatest", None),
        ("https://attacker.example/steal", "/auth/start?next=%2F", "attacker.example"),
    ],
)
async def test_a_refusal_retries_only_to_a_safe_local_destination(
    client,
    federated,
    next_url,
    retry_target,
    must_not_leak,
):
    from urllib.parse import parse_qs, urlsplit

    started = await client.get(
        "/auth/start",
        params={"next": next_url},
        follow_redirects=False,
    )
    query = parse_qs(urlsplit(started.headers["location"]).query)
    federated.pending_claims["nonce"] = query["nonce"][0]

    refused = await client.get(
        "/auth/callback",
        params={
            "code": "c",
            "state": query["state"][0],
            "iss": "https://attacker.example.test",
        },
        follow_redirects=False,
    )

    assert refused.status_code == 401
    assert f'href="{retry_target}"' in refused.text
    assert refused.text.count("data-oidc-retry") == 1
    if must_not_leak is not None:
        assert must_not_leak not in refused.text


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
    auth_client, federated, db_session, legacy_owner_roots, path
):
    """Authenticated, so the 404 is the cutover rather than the session guard.

    An anonymous request gets 401 first, which is correct and proves nothing
    about whether the route still exists.
    """

    await _set_owner_federated_cookie(
        auth_client,
        db_session,
        legacy_owner_roots,
    )
    response = await auth_client.post(path, data={"code": "123456"})
    assert response.status_code == 404, path


async def test_after_the_cutover_changing_a_local_password_is_gone(
    auth_client,
    federated,
    db_session,
    legacy_owner_roots,
):
    await _set_owner_federated_cookie(
        auth_client,
        db_session,
        legacy_owner_roots,
    )

    response = await auth_client.post(
        "/settings/password",
        data={
            "old_password": "old-password",
            "new_password": "new-password",
            "new_password_confirm": "new-password",
        },
    )

    assert response.status_code == 404


async def test_after_the_cutover_the_settings_page_stops_offering_sign_in(
    auth_client, federated, db_session, legacy_owner_roots
):
    """Every route behind that card is 404; the card should not still be there.

    Rendering it offered a password change and a second-factor enrolment that
    could not happen. Worse, the page read the 2FA state unconditionally — so a
    half-finished enrolment from before the cutover still reads as ``pending``,
    and its live secret and QR were painted onto a page whose buttons could no
    longer act on them.
    """

    from vitals.models.app_settings import AppSetting
    from vitals.services.authentication import legacy_two_factor as twofa_service

    # A pre-cutover enrolment nobody ever finished.
    db_session.add(
        AppSetting(
            key=twofa_service.SETTINGS_KEY,
            value='{"secret": "SYNTHETICSECRET234567", "confirmed": false}',
        )
    )
    await db_session.commit()

    await _set_owner_federated_cookie(
        auth_client,
        db_session,
        legacy_owner_roots,
    )

    response = await auth_client.get("/settings", headers={"Accept": "text/html"})
    assert response.status_code == 200
    body = response.text

    assert "SYNTHETICSECRET234567" not in body
    for control in (
        '/settings/password"',
        "/settings/2fa/start",
        "/settings/2fa/enable",
        "/settings/2fa/disable",
    ):
        assert control not in body, control


async def test_before_the_cutover_the_sign_in_card_is_still_there(
    auth_client, db_session, legacy_owner_roots
):
    """The gate is new; what it hides has to keep working without it."""

    response = await auth_client.get("/settings", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert '/settings/password"' in response.text


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


def test_the_setup_document_names_the_variables_the_code_actually_reads():
    """A runbook that names a variable nothing reads is a runbook that fails.

    The operator follows the document; the document is therefore the thing that
    has to be checked against the code rather than trusted to have kept up.
    """

    import inspect
    from pathlib import Path

    import web.config

    document = (
        Path(__file__).resolve().parent.parent / "docs" / "OIDC_SETUP.md"
    ).read_text()
    source = inspect.getsource(web.config)

    for name in (
        "VITALS_OIDC_ISSUER",
        "VITALS_OIDC_CLIENT_ID",
        "VITALS_OIDC_CLIENT_SECRET",
        "VITALS_OIDC_REDIRECT_URL",
        "VITALS_OIDC_BOOTSTRAP_SUBJECT",
    ):
        assert name in source, f"{name} is documented but nothing reads it"
        assert name in document, f"{name} is read but nothing documents it"


@pytest.mark.parametrize(
    "only_name",
    (
        "VITALS_OIDC_ISSUER",
        "VITALS_OIDC_CLIENT_ID",
        "VITALS_OIDC_CLIENT_SECRET",
        "VITALS_OIDC_REDIRECT_URL",
    ),
)
def test_a_partial_oidc_cutover_fails_closed(monkeypatch, only_name):
    from web.config import OIDC_REQUIRED_ENV, get_web_config

    for name in OIDC_REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(only_name, "configured")

    with pytest.raises(RuntimeError, match="OIDC configuration is incomplete"):
        get_web_config()


def test_an_unsafe_oidc_url_fails_during_configuration_load(monkeypatch):
    from web.config import get_web_config

    for name, value in (
        ("VITALS_OIDC_ISSUER", "http://localhost.evil.test"),
        ("VITALS_OIDC_CLIENT_ID", "configured-client"),
        ("VITALS_OIDC_CLIENT_SECRET", "configured-secret"),
        ("VITALS_OIDC_REDIRECT_URL", REDIRECT),
    ):
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="OIDC configuration is invalid"):
        get_web_config()


def test_oidc_callback_must_use_the_public_origin(monkeypatch):
    from web.config import get_web_config

    for name, value in (
        ("VITALS_OIDC_ISSUER", ISSUER),
        ("VITALS_OIDC_CLIENT_ID", "configured-client"),
        ("VITALS_OIDC_CLIENT_SECRET", "configured-secret"),
        ("VITALS_OIDC_REDIRECT_URL", "https://evil.test/auth/callback"),
        ("VITALS_PUBLIC_URL", "https://vitals.example.test"),
    ):
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="must use the VITALS_PUBLIC_URL origin"):
        get_web_config()


def test_oidc_origin_comparison_treats_default_https_port_semantically(monkeypatch):
    from web.config import get_web_config

    for name, value in (
        ("VITALS_OIDC_ISSUER", ISSUER),
        ("VITALS_OIDC_CLIENT_ID", "configured-client"),
        ("VITALS_OIDC_CLIENT_SECRET", "configured-secret"),
        ("VITALS_OIDC_REDIRECT_URL", REDIRECT),
        ("VITALS_PUBLIC_URL", "https://vitals.example.test:443"),
    ):
        monkeypatch.setenv(name, value)

    assert get_web_config().oidc_enabled is True


def test_oidc_configuration_does_not_require_legacy_credentials(monkeypatch):
    from web.config import get_web_config

    for name, value in (
        ("VITALS_OIDC_ISSUER", ISSUER),
        ("VITALS_OIDC_CLIENT_ID", "configured-client"),
        ("VITALS_OIDC_CLIENT_SECRET", "configured-secret"),
        ("VITALS_OIDC_REDIRECT_URL", REDIRECT),
        ("VITALS_PUBLIC_URL", "https://vitals.example.test"),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("VITALS_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("VITALS_AUTH_PASSWORD_HASH", raising=False)

    config = get_web_config()

    assert config.oidc_enabled is True
    assert config.auth_username == ""
    assert config.auth_password_hash == ""


def test_password_mode_still_requires_legacy_username(monkeypatch):
    from web.config import OIDC_REQUIRED_ENV, get_web_config

    for name in OIDC_REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("VITALS_AUTH_USERNAME", raising=False)

    with pytest.raises(RuntimeError, match="VITALS_AUTH_USERNAME is not set"):
        get_web_config()


def test_the_compose_file_keeps_the_provider_behind_a_profile():
    """Bringing the provider up is the cutover, so it must be a decision.

    A service that starts with ``docker compose up`` would make the cutover a
    side effect of a routine restart.
    """

    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
    )
    for name in (
        "vitals_idp_config_check",
        "vitals_idp_config_render",
        "vitals_idp_bootstrap_prepare",
        "vitals_idp_db",
        "vitals_idp_db_provision",
        "vitals_idp_init",
        "vitals_idp_setup",
        "vitals_idp_bootstrap_seal",
        "vitals_idp_db_grants",
        "vitals_idp",
        "vitals_idp_login",
        "vitals_idp_gateway",
        "vitals_idp_backup",
    ):
        assert compose["services"][name]["profiles"] == ["idp"], name

    assert compose["services"]["vitals_idp_db"]["depends_on"][
        "vitals_idp_config_check"
    ]["condition"] == "service_completed_successfully"

    assert compose["services"]["vitals_idp"]["healthcheck"]["test"] == [
        "CMD",
        "/app/zitadel",
        "ready",
    ]
    assert compose["services"]["vitals_idp_login"]["healthcheck"]["test"] == [
        "CMD",
        "/bin/sh",
        "-c",
        "node /app/healthcheck.mjs http://localhost:3000/ui/v2/login/healthy",
    ]
    assert compose["services"]["vitals_idp_backup"]["depends_on"] == {
        "vitals_idp_gateway": {"condition": "service_healthy"}
    }

    # Its own volume, so restoring the health store and restoring the identity
    # store are separate decisions.
    assert "vitals_idp_pgdata" in compose["volumes"]
    assert (
        compose["services"]["vitals_idp_db"]["volumes"][0].split(":")[0]
        == "vitals_idp_pgdata"
    )


def test_the_inactive_provider_profile_does_not_require_provider_secrets():
    """Compose interpolates profiles before deciding which ones to start.

    A required-value expression in an inactive service therefore breaks even
    ``docker compose ps`` on installations that deliberately did not configure
    an identity provider. Runtime validation remains the selected profile's
    responsibility: Postgres and ZITADEL both refuse their empty secrets.
    """

    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "docker-compose.yml"
    ).read_text()

    for name in (
        "VITALS_IDP_MASTERKEY_FILE",
        "VITALS_IDP_DB_ADMIN_PASSWORD_FILE",
        "VITALS_IDP_DB_SERVICE_PASSWORD_FILE",
        "VITALS_IDP_DB_BACKUP_PASSWORD_FILE",
        "VITALS_IDP_ADMIN_PASSWORD_FILE",
    ):
        assert f"${{{name}:?" not in source
        assert f"${{{name}:-" in source


def test_the_provider_profile_replaces_the_known_first_admin_password():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    source = (root / "docker-compose.yml").read_text()
    compose = yaml.safe_load(source)
    service = compose["services"]["vitals_idp_config_render"]

    assert "idp_admin_password" in service["secrets"]
    assert service["environment"]["VITALS_IDP_ADMIN_PASSWORD_FILE"] == (
        "/run/secrets/idp_admin_password"
    )
    setup = compose["services"]["vitals_idp_setup"]
    assert "idp_admin_password" not in setup.get("secrets", [])
    assert "ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORD" not in setup["environment"]
    assert "Password1!" not in source
    for document in (
        root / ".env.idp.example",
        root / "docs" / "OIDC_SETUP.md",
    ):
        assert "VITALS_IDP_ADMIN_PASSWORD_FILE" in document.read_text()


def test_the_provider_profile_does_not_advertise_orphan_registration():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    environment = compose["services"]["vitals_idp_setup"]["environment"]

    assert environment[
        "ZITADEL_DEFAULTINSTANCE_LOGINPOLICY_ALLOWREGISTER"
    ] == "false"

    runbook = (root / "docs" / "OIDC_SETUP.md").read_text()
    assert "allow_register=false" in runbook
    assert "already existed" in runbook
