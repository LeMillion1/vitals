"""Integration tests for Vitals OAuth 2.0 authorization server and MCP tools."""
from __future__ import annotations

import html
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
import pytest
from sqlalchemy import select

from vitals.enums import ProfessionalKind, Source, UserRoleName, UserStatus
from vitals.models import GarminActivity, GarminDaily, HevyWorkout, LabResult, WeightLog
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import (
    CareRelationship,
    ConsentGrant,
    ConsentScope,
    ProfessionalProfile,
)
from vitals.services.care import professionals
from web.auth import (
    _get_mcp_serializer,
    _get_serializer,
    create_federated_session,
    create_session,
)
from web.config import SESSION_COOKIE

# PKCE pair used across the flow tests: CODE_CHALLENGE is the S256 of CODE_VERIFIER.
CODE_VERIFIER = "some_challenge"
CODE_CHALLENGE = "bhmDDzo_BXLob8jrOdLgvkzIe7gymOatjCthDDsvQIE"


async def _professional_in_care(db_session, legacy_owner_roots):
    professional = User(
        username="oauth-doctor",
        normalized_username="oauth-doctor",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(professional)
    await db_session.flush()
    db_session.add(
        UserRole(user_id=professional.id, role=UserRoleName.DOCTOR.value)
    )
    operator = User(
        username="oauth-professional-reviewer",
        normalized_username="oauth-professional-reviewer",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(operator)
    await db_session.flush()
    db_session.add(
        UserRole(
            user_id=operator.id,
            role=UserRoleName.PLATFORM_SUPERADMIN.value,
        )
    )
    await db_session.flush()
    profile = await professionals.submit_profile(
        db_session,
        user_id=professional.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Verified OAuth doctor",
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status="pending",
        status="verified",
    )
    relationship = CareRelationship(
        subject_id=legacy_owner_roots.subject_id,
        subject_owner_user_id=legacy_owner_roots.user_id,
        professional_user_id=professional.id,
        kind="doctor",
        status="active",
    )
    db_session.add(relationship)
    await db_session.flush()
    grant = ConsentGrant(
        relationship_id=relationship.id,
        subject_id=legacy_owner_roots.subject_id,
        version=1,
        status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db_session.add(grant)
    await db_session.flush()
    db_session.add(
        ConsentScope(
            consent_grant_id=grant.id,
            subject_id=legacy_owner_roots.subject_id,
            resource_type="domain",
            resource_key="weight",
            action="list",
        )
    )
    await db_session.commit()
    return professional, relationship, grant



async def test_oauth_metadata_discovery(client):
    """Test standard RFC 8414 metadata discovery endpoint."""
    response = await client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    data = response.json()
    assert data["issuer"] == "http://test"
    assert data["authorization_endpoint"] == "http://test/oauth/authorize"
    assert data["token_endpoint"] == "http://test/oauth/token"
    assert "code" in data["response_types_supported"]


async def test_oauth_authorize_unauthenticated_redirects(client):
    """GET /oauth/authorize redirects to /login if the browser session is unauthenticated."""
    response = await client.get(
        "/oauth/authorize?response_type=code&client_id=vitals-claude-connector"
        "&redirect_uri=https://claude.ai/callback"
        f"&code_challenge={CODE_CHALLENGE}&code_challenge_method=S256"
    )
    assert response.status_code == 302
    parsed = urlsplit(response.headers["location"])
    assert parsed.path == "/login"
    assert parse_qs(parsed.query)["next"][0].startswith("/oauth/authorize")


async def test_oauth_authorize_unauthenticated_redirect_preserves_full_query(client):
    """The `next` param must be percent-encoded as a single value — the OAuth
    query string it carries (redirect_uri, code_challenge, state...) contains
    its own '&'/'?' characters that would otherwise be parsed as separate
    top-level params on /login, truncating `next` and 422ing after login."""
    original_query = (
        "response_type=code&client_id=vitals-claude-connector"
        "&redirect_uri=https://claude.ai/callback"
        "&code_challenge=abc123&code_challenge_method=S256&state=xyz789"
    )
    response = await client.get(f"/oauth/authorize?{original_query}")
    assert response.status_code == 302

    location = response.headers["location"]
    parsed = urlsplit(location)
    assert parsed.path == "/login"
    next_value = parse_qs(parsed.query)["next"][0]
    assert next_value == f"/oauth/authorize?{original_query}"


async def test_oauth_login_redirect_reaches_authorize_with_full_params(client):
    """End-to-end: an unauthenticated visit to /oauth/authorize with PKCE +
    state, followed by a successful login, must land back on the SAME
    /oauth/authorize URL with every original param intact (not a truncated
    /oauth/authorize?response_type=code that 422s)."""
    original_query = (
        "response_type=code&client_id=vitals-claude-connector"
        "&redirect_uri=https://claude.ai/callback"
        "&code_challenge=abc123&code_challenge_method=S256&state=xyz789"
    )
    r1 = await client.get(f"/oauth/authorize?{original_query}")
    login_url = r1.headers["location"]

    r2 = await client.get(login_url)
    assert r2.status_code == 200
    next_value = parse_qs(urlsplit(login_url).query)["next"][0]
    assert next_value == f"/oauth/authorize?{original_query}"
    # Jinja HTML-escapes the hidden field's value (e.g. & -> &amp;) — correct
    # and safe; escape before comparing to the raw query string.
    assert f'value="{html.escape(next_value)}"' in r2.text

    # Credentials match conftest's TEST_USERNAME/TEST_PASSWORD, referenced as
    # literals rather than re-imported (a site-packages `tests` package can
    # shadow `tests.conftest` and break the import — see auth_client fixture).
    r3 = await client.post("/login", data={"username": "tester", "password": "password", "next": next_value})
    assert r3.status_code == 303
    assert r3.headers["location"] == f"/oauth/authorize?{original_query}"


async def test_oauth_authorize_authenticated_renders(auth_client):
    """GET /oauth/authorize renders the consent template if authenticated."""
    response = await auth_client.get(
        "/oauth/authorize?response_type=code&client_id=vitals-claude-connector&redirect_uri=https://claude.ai/callback"
        f"&code_challenge={CODE_CHALLENGE}&code_challenge_method=S256"
    )
    assert response.status_code == 200
    assert "Разрешение доступа" in response.text
    assert "Claude.ai" in response.text
    assert "Только карта выбранного человека" in response.text
    assert "разрешённые сейчас разделы и действия" in response.text
    assert 'name="subject_id"' in response.text
    assert "read-only" not in response.text


async def test_oauth_authorize_rejects_a_revoked_federated_cookie(
    client, db_session, legacy_owner_roots
):
    """A signed OIDC cookie is no longer authority after session revocation."""
    from vitals.services.authentication.sessions import revoke_all_sessions

    user = await db_session.get(User, legacy_owner_roots.user_id)
    token = create_federated_session(
        username=user.username,
        user_id=user.id,
        session_version=user.session_version,
        authenticated_at=int(datetime.now(timezone.utc).timestamp()),
        subject_id=legacy_owner_roots.subject_id,
    )
    client.cookies.set(SESSION_COOKIE, token, domain="test.local", path="/")
    original_query = (
        "response_type=code&client_id=vitals-claude-connector"
        "&redirect_uri=https://claude.ai/callback"
        f"&code_challenge={CODE_CHALLENGE}&code_challenge_method=S256"
        "&state=revoked-session"
    )

    before = await client.get(f"/oauth/authorize?{original_query}")
    assert before.status_code == 200

    await revoke_all_sessions(db_session, user_id=user.id)
    await db_session.commit()

    after = await client.get(f"/oauth/authorize?{original_query}")
    assert after.status_code == 302
    parsed = urlsplit(after.headers["location"])
    assert parsed.path == "/login"
    assert parse_qs(parsed.query)["next"] == [
        f"/oauth/authorize?{original_query}"
    ]
    assert SESSION_COOKIE not in client.cookies

    # The redirect is usable rather than a loop caused by /login seeing the
    # stale-but-signed cookie and bouncing back to the app.
    login = await client.get(after.headers["location"])
    assert login.status_code == 200


async def test_oauth_approve_rejects_a_revoked_federated_cookie_without_a_code(
    client, db_session, redis, legacy_owner_roots
):
    """Approval rechecks the live session instead of trusting an open form."""
    from vitals.services.authentication.sessions import revoke_all_sessions

    user = await db_session.get(User, legacy_owner_roots.user_id)
    token = create_federated_session(
        username=user.username,
        user_id=user.id,
        session_version=user.session_version,
        authenticated_at=int(datetime.now(timezone.utc).timestamp()),
        subject_id=legacy_owner_roots.subject_id,
    )
    client.cookies.set(SESSION_COOKIE, token, domain="test.local", path="/")
    await revoke_all_sessions(db_session, user_id=user.id)
    await db_session.commit()

    response = await client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": "https://claude.ai/callback",
            "state": "revoked-session",
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
        },
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}
    assert await redis.keys("oauth_code:*") == []


async def test_oauth_authorize_invalid_client(auth_client):
    """GET /oauth/authorize with invalid client_id shows error message."""
    response = await auth_client.get(
        "/oauth/authorize?response_type=code&client_id=wrong-client&redirect_uri=https://claude.ai/callback"
    )
    assert response.status_code == 200
    assert "Неверный client_id" in response.text


async def test_oauth_authorize_invalid_redirect_uri(auth_client):
    """GET /oauth/authorize with a redirect_uri outside the allowlist is rejected,
    not silently carried through to the consent screen or a login redirect."""
    response = await auth_client.get(
        "/oauth/authorize?response_type=code&client_id=vitals-claude-connector&redirect_uri=https://evil.com/callback"
    )
    assert response.status_code == 200
    assert "Недопустимый redirect_uri" in response.text


async def test_oauth_authorize_invalid_redirect_uri_unauthenticated(client):
    """Same rejection happens before the unauthenticated login redirect, so an
    attacker can't ride the login flow into an approval with a bad redirect_uri."""
    response = await client.get(
        "/oauth/authorize?response_type=code&client_id=vitals-claude-connector&redirect_uri=https://evil.com/callback"
    )
    assert response.status_code == 200
    assert "Недопустимый redirect_uri" in response.text


async def test_oauth_error_page_has_no_approve_form(auth_client):
    """A refused request shows only "Close": pressing Allow on an error screen
    used to post the same bad request again and answer with raw JSON."""
    response = await auth_client.get(
        "/oauth/authorize?response_type=code&client_id=vitals-claude-connector&redirect_uri=https://evil.com/callback"
    )
    assert response.status_code == 200
    assert "/oauth/authorize/approve" not in response.text
    assert "Закрыть" in response.text


async def test_oauth_authorize_shows_redirect_target(auth_client):
    """The consent screen displays the real destination domain."""
    response = await auth_client.get(
        "/oauth/authorize?response_type=code&client_id=vitals-claude-connector&redirect_uri=https://claude.ai/callback"
        f"&code_challenge={CODE_CHALLENGE}&code_challenge_method=S256"
    )
    assert response.status_code == 200
    assert "Вы будете перенаправлены на" in response.text
    assert "claude.ai" in response.text


async def test_oauth_approve_invalid_redirect_uri(auth_client):
    """POST /oauth/authorize/approve rejects a redirect_uri outside the allowlist
    even if somehow reached (defense in depth behind the GET-time check)."""
    response = await auth_client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": "https://evil.com/callback",
            "state": "x",
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 400


# ── Callback allowlist is per-host, not per-URL ───────────────────────────────
# Gemini Spark's callback carries the owner's Google account id and the MCP host
# in its path, so it differs on every installation — the allowlist matches the
# host and leaves the path alone.
GEMINI_REDIRECT = (
    "https://oauth-redirect.googleusercontent.com/r/"
    "user_bound_custom-mcp-110072519687538803290-vitals_example_com"
)


async def test_oauth_approve_accepts_per_user_google_callback(auth_client):
    """The path is not pinned: an unseen per-user Google callback still mints a code."""
    response = await auth_client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": GEMINI_REDIRECT,
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith(f"{GEMINI_REDIRECT}?code=")


@pytest.mark.parametrize("redirect_uri", [
    "http://claude.ai/callback",              # plaintext — a code would ride the wire
    "https://claude.ai@evil.com/callback",    # userinfo dressed up as the allowed host
    "https://evil.claude.ai/callback",        # subdomain is a different host, not a suffix match
    "https://claude.ai.evil.com/callback",    # allowed host as a prefix of an attacker's
])
async def test_oauth_approve_rejects_lookalike_callbacks(auth_client, redirect_uri):
    """Host matching must be exact — no scheme downgrade, no userinfo/suffix tricks."""
    response = await auth_client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": redirect_uri,
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 400


async def test_csp_form_action_allows_any_https_callback(client):
    """Chrome enforces form-action across the whole approval redirect chain,
    including hops inside the connector's own product that no allowlist here can
    predict. A host-pinned form-action swallows the "Approve" click silently, so
    the header must not pin one — redirect_allowed() is what gates the target."""
    response = await client.get("/login", headers={"Accept": "text/html"})
    csp = response.headers["Content-Security-Policy"]
    form_action = csp.split("form-action ", 1)[1].split(";", 1)[0]

    assert "https:" in form_action.split()


async def test_oauth_full_flow_and_token_exchange(auth_client, redis):
    """Test full OAuth 2.0 flow: authorize approve -> code generation -> token exchange."""
    # 1. Approve authorization
    response = await auth_client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": "https://claude.ai/callback",
            "state": "oauth-state-123",
            "code_challenge": "bhmDDzo_BXLob8jrOdLgvkzIe7gymOatjCthDDsvQIE",
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://claude.ai/callback?code=")
    assert "state=oauth-state-123" in location

    # Extract authorization code
    parts = location.split("code=")
    code = parts[1].split("&")[0]

    # Verify code details stored in Redis
    code_data_raw = await redis.get(f"oauth_code:{code}")
    assert code_data_raw is not None
    code_data = json.loads(code_data_raw)
    assert code_data["username"] == "tester"
    assert code_data["subject_id"]

    # 2. Exchange code for access token (POST /oauth/token)
    token_response = await auth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/callback",
            "client_id": "vitals-claude-connector",
            "client_secret": "test-mcp-secret",
            "code_verifier": "some_challenge",  # S256 hashes to code_challenge above
        },
    )
    assert token_response.status_code == 200
    token_data = token_response.json()
    assert token_data["token_type"] == "Bearer"
    assert "access_token" in token_data

    # Verify the code is deleted from Redis (single-use constraint)
    assert await redis.get(f"oauth_code:{code}") is None

    # Verify token signature and contents — signed with the dedicated MCP salt,
    # not the session salt (see web.auth._get_mcp_serializer).
    serializer = _get_mcp_serializer()
    payload = serializer.loads(token_data["access_token"], max_age=3600)
    assert payload["username"] == "tester"
    assert payload["client_id"] == "vitals-claude-connector"
    assert payload["type"] == "mcp_access_token"
    assert payload["health_subject"] == code_data["subject_id"]
    assert payload["scopes"]

    # The session serializer must NOT be able to verify an MCP token (different salt).
    with pytest.raises(Exception):
        _get_serializer().loads(token_data["access_token"], max_age=3600)


async def test_a_professional_authorizes_one_patient_and_consent_version(
    client, db_session, legacy_owner_roots, redis
):
    professional, relationship, grant = await _professional_in_care(
        db_session, legacy_owner_roots
    )
    client.cookies.set(SESSION_COOKIE, create_session(professional.username))
    query = (
        "/oauth/authorize?response_type=code"
        "&client_id=vitals-claude-connector"
        "&redirect_uri=https://claude.ai/callback"
        f"&code_challenge={CODE_CHALLENGE}&code_challenge_method=S256"
    )
    page = await client.get(query)
    assert page.status_code == 200
    assert "подопечный" in page.text
    assert str(legacy_owner_roots.subject_id) in page.text

    approved = await client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": "https://claude.ai/callback",
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "subject_id": str(legacy_owner_roots.subject_id),
        },
    )
    assert approved.status_code == 302
    code = approved.headers["location"].split("code=")[1].split("&")[0]
    code_data = json.loads(await redis.get(f"oauth_code:{code}"))
    assert code_data["subject_id"] == str(legacy_owner_roots.subject_id)

    exchanged = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/callback",
            "client_id": "vitals-claude-connector",
            "client_secret": "test-mcp-secret",
            "code_verifier": CODE_VERIFIER,
        },
    )
    assert exchanged.status_code == 200
    payload = _get_mcp_serializer().loads(exchanged.json()["access_token"])
    assert payload["health_subject"] == str(legacy_owner_roots.subject_id)
    assert payload["relationship"] == str(relationship.id)
    assert payload["consent_grant"] == str(grant.id)
    assert payload["consent_version"] == 1
    assert payload["scopes"] == ["domain:weight:list"]


async def test_a_suspended_profile_cannot_list_a_patient_for_oauth(
    client, db_session, legacy_owner_roots
):
    professional, _relationship, _grant = await _professional_in_care(
        db_session, legacy_owner_roots
    )
    profile = await db_session.scalar(
        select(ProfessionalProfile).where(
            ProfessionalProfile.user_id == professional.id
        )
    )
    reviewer_id = await db_session.scalar(
        select(UserRole.user_id).where(
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value
        )
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=reviewer_id,
        expected_status="verified",
        status="suspended",
        note="synthetic licence withdrawal",
    )
    await db_session.commit()

    client.cookies.set(SESSION_COOKIE, create_session(professional.username))
    response = await client.get(
        "/oauth/authorize?response_type=code"
        "&client_id=vitals-claude-connector"
        "&redirect_uri=https://claude.ai/callback"
        f"&code_challenge={CODE_CHALLENGE}&code_challenge_method=S256"
    )

    assert response.status_code == 200
    assert str(legacy_owner_roots.subject_id) not in response.text

    approved = await client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": "https://claude.ai/callback",
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "subject_id": str(legacy_owner_roots.subject_id),
        },
    )
    assert approved.status_code == 400


async def test_oauth_approval_cannot_substitute_an_unrelated_patient(
    client, db_session, legacy_owner_roots
):
    professional, _relationship, _grant = await _professional_in_care(
        db_session, legacy_owner_roots
    )
    client.cookies.set(SESSION_COOKIE, create_session(professional.username))
    response = await client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": "https://claude.ai/callback",
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "subject_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == (
        "health subject is not available for connector authorization"
    )


async def test_a_professional_with_two_patients_must_choose_one(
    client, db_session, legacy_owner_roots
):
    professional, _relationship, _grant = await _professional_in_care(
        db_session, legacy_owner_roots
    )
    second_owner = User(
        username="oauth-second-owner",
        normalized_username="oauth-second-owner",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second_owner)
    await db_session.flush()
    second_subject = HealthSubject(
        owner_user_id=second_owner.id,
        display_name="Second synthetic patient",
        timezone="UTC",
    )
    db_session.add(second_subject)
    await db_session.flush()
    second_relationship = CareRelationship(
        subject_id=second_subject.id,
        subject_owner_user_id=second_owner.id,
        professional_user_id=professional.id,
        kind="doctor",
        status="active",
    )
    db_session.add(second_relationship)
    await db_session.flush()
    second_grant = ConsentGrant(
        relationship_id=second_relationship.id,
        subject_id=second_subject.id,
        version=1,
        status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db_session.add(second_grant)
    await db_session.flush()
    db_session.add(
        ConsentScope(
            consent_grant_id=second_grant.id,
            subject_id=second_subject.id,
            resource_type="domain",
            resource_key="labs",
            action="read",
        )
    )
    await db_session.commit()

    client.cookies.set(SESSION_COOKIE, create_session(professional.username))
    page = await client.get(
        "/oauth/authorize?response_type=code"
        "&client_id=vitals-claude-connector"
        "&redirect_uri=https://claude.ai/callback"
        f"&code_challenge={CODE_CHALLENGE}&code_challenge_method=S256"
    )
    assert page.status_code == 200
    assert '<select class="v-select" id="oauth-subject"' in page.text
    assert str(legacy_owner_roots.subject_id) in page.text
    assert str(second_subject.id) in page.text

    without_choice = await client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": "https://claude.ai/callback",
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert without_choice.status_code == 400


async def test_oauth_token_missing_client_secret_rejected(auth_client, redis, monkeypatch):
    """Fail-closed: an unconfigured VITALS_MCP_CLIENT_SECRET must refuse token
    issuance rather than skipping the secret check."""
    monkeypatch.setenv("VITALS_MCP_CLIENT_SECRET", "")

    response = await auth_client.post(
        "/oauth/authorize/approve",
        data={"client_id": "vitals-claude-connector", "redirect_uri": "https://claude.ai/callback",
              "code_challenge": CODE_CHALLENGE, "code_challenge_method": "S256"},
    )
    code = response.headers["location"].split("code=")[1].split("&")[0]

    token_response = await auth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/callback",
            "client_id": "vitals-claude-connector",
        },
    )
    assert token_response.status_code == 400
    assert token_response.json()["error"] == "invalid_client"


async def test_oauth_token_wrong_client_secret_rejected(auth_client, redis):
    """A client_secret that doesn't match the configured one is rejected."""
    response = await auth_client.post(
        "/oauth/authorize/approve",
        data={"client_id": "vitals-claude-connector", "redirect_uri": "https://claude.ai/callback",
              "code_challenge": CODE_CHALLENGE, "code_challenge_method": "S256"},
    )
    code = response.headers["location"].split("code=")[1].split("&")[0]

    token_response = await auth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/callback",
            "client_id": "vitals-claude-connector",
            "client_secret": "not-the-right-secret",
        },
    )
    assert token_response.status_code == 400
    assert token_response.json()["error"] == "invalid_client"


async def test_mcp_token_rejected_as_session_cookie(client):
    """An MCP access token (dict payload) must not authenticate a normal session
    even though it's signed with the same session_secret (different salt + type
    check in read_session)."""
    mcp_token = _get_mcp_serializer().dumps({
        "username": "tester",
        "client_id": "vitals-claude-connector",
        "type": "mcp_access_token",
    })
    client.cookies.set(SESSION_COOKIE, mcp_token)
    response = await client.get("/weight", headers={"Accept": "text/html"})
    assert response.status_code == 302
    assert "/login" in response.headers["location"]


async def test_session_token_rejected_as_mcp_bearer(client):
    """A session cookie token must not authenticate as an MCP Bearer token."""
    session_token = create_session("tester")
    response = await client.get("/mcp/", headers={"Authorization": f"Bearer {session_token}"})
    assert response.status_code == 401


# Two tests stood here, both about an ASGI wrapper this module no longer has.
# ``MCPAuthMiddleware`` hand-rolled bearer validation in front of the MCP app and
# these covered its edges: a ``TypeError`` from the wrapped app becoming a 500
# rather than a hang, and a second ``http.response.start`` from a streaming
# endpoint being swallowed. The SDK owns the transport now — its own
# ``RequireAuthMiddleware`` validates the token and its own streamable-HTTP
# manager owns the response lifecycle — so both tested a layer that is gone
# rather than a behaviour that changed. What replaced them is
# ``tests/test_mcp_actor_identity.py``, against the token verifier.


async def test_mcp_initialize_over_streamable_http(
    client, session_factory, legacy_owner_roots, monkeypatch
):
    """The streamable-HTTP session manager only exists while the mounted app's own
    lifespan is running, and app.mount() never runs it — web/main.py has to forward it
    via app.state.mcp_lifespan. Drop that forwarding and the server still boots, so
    this is the only test that catches it: a real ``initialize`` over /mcp/."""
    from web.main import app
    from web.routers import mcp as mcp_router

    # The token verifier opens a session of its own — it runs below FastAPI's
    # dependency injection, so the ``client`` fixture's override does not reach
    # it. Without this it reads a database that has no tables.
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    mcp_lifespan = app.state.mcp_lifespan  # set by main.py at mount time

    token = _get_mcp_serializer().dumps({
        "username": "tester",
        "client_id": "vitals-claude-connector",
        "type": "mcp_access_token",
    })

    # The `client` fixture uses ASGITransport, which does not run lifespans — enter
    # the MCP one by hand.
    async with mcp_lifespan(app):
        r = await client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert r.status_code == 200
    assert "protocolVersion" in r.text


async def test_mcp_read_only_tools_execution(
    db_session,
    session_factory,
    legacy_owner_roots,
    owned_by_legacy_subject, *, garmin_connection_id, hevy_connection_id,
):
    """Test that the read-only MCP tools execute and return valid serializable schemas."""
    # Pre-seed some test data
    w_log = WeightLog(subject_id=legacy_owner_roots.subject_id,
        date=date(2026, 6, 15),
        weight_kg=84.5,
        domain="weight",
        source=Source.MANUAL.value,
        superseded=False,
    )
    garmin_log = GarminDaily(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id,
        date=date(2026, 6, 15),
        sleep_score=85,
        resting_hr=58,
        hrv_avg=65,
        spo2_lowest=91,
        training_status="PRODUCTIVE",
        domain="garmin",
        source=Source.GARMIN_API.value,
    )
    garmin_activity = GarminActivity(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id,
        date=date(2026, 6, 15),
        external_id="garmin-act-1",
        activity_type="running",
        name="Morning run",
        avg_hr=140,
        training_effect_aerobic=3.4,
        hr_zone_seconds=[{"zone": 1, "secs": 120.0, "low_hr": 101}],
        splits=[{"index": 1, "distance_m": 1000.0, "avg_hr": 150}],
        domain="garmin",
        source=Source.GARMIN_API.value,
    )
    workout_log = HevyWorkout(subject_id=legacy_owner_roots.subject_id, integration_connection_id=hevy_connection_id,
        date=date(2026, 6, 15),
        external_id="hevy-workout-1",
        title="Upper Body",
        domain="workouts",
        source=Source.HEVY_API.value,
    )
    lab_log = LabResult(subject_id=legacy_owner_roots.subject_id,
        date=date(2026, 6, 15),
        marker="Glucose",
        value=5.2,
        unit="mmol/L",
        domain="labs",
        source=Source.MANUAL.value,
    )
    db_session.add_all([w_log, garmin_log, garmin_activity, workout_log, lab_log])
    await db_session.commit()

    # Import mcp app tools
    from web.routers.mcp import (
        get_user_profile,
        get_weight_logs,
        get_garmin_metrics,
        get_hevy_workouts,
        get_lab_results,
    )

    # Override session dependencies so tools use the test database session
    # Note: get_session_factory in web.routers.mcp gets session_factory.
    # To mock it in tests, we patch get_session_factory to return our test session_factory fixture.
    import web.routers.mcp as mcp_router
    original_factory = mcp_router.get_session_factory
    mcp_router.get_session_factory = lambda: session_factory

    try:
        # Test get_user_profile. Inside the patched factory now, not above it:
        # the profile is read from the subject's own record rather than from
        # ``.env``, so this tool needs a database like every other one here.
        profile = await get_user_profile()
        assert profile["height_cm"] == 190.0
        assert profile["sex"] == "male"

        # Test get_weight_logs tool
        weights_data = await get_weight_logs(start_date="2026-06-10", end_date="2026-06-20")
        assert len(weights_data["weights"]) == 1
        assert weights_data["weights"][0]["weight_kg"] == 84.5

        # Test get_garmin_metrics tool
        garmin_data = await get_garmin_metrics(start_date="2026-06-10", end_date="2026-06-20")
        assert len(garmin_data["daily_recovery"]) == 1
        assert garmin_data["daily_recovery"][0]["sleep_score"] == 85
        assert garmin_data["daily_recovery"][0]["resting_hr"] == 58
        # New sleep-detail column reflected automatically via serialize_row.
        assert garmin_data["daily_recovery"][0]["spo2_lowest"] == 91
        # New training-status column reflected automatically via serialize_row.
        assert garmin_data["daily_recovery"][0]["training_status"] == "PRODUCTIVE"
        # Per-activity detail: scalar + JSONB columns serialize through.
        assert len(garmin_data["activities"]) == 1
        act = garmin_data["activities"][0]
        assert act["training_effect_aerobic"] == 3.4
        assert act["hr_zone_seconds"][0]["low_hr"] == 101
        assert act["splits"][0]["distance_m"] == 1000.0

        # Test get_hevy_workouts tool
        workouts_data = await get_hevy_workouts(start_date="2026-06-10", end_date="2026-06-20")
        assert len(workouts_data) == 1
        assert workouts_data[0]["title"] == "Upper Body"

        # Test get_lab_results tool
        labs_data = await get_lab_results()
        assert len(labs_data) == 1
        assert labs_data[0]["marker"] == "Glucose"
        assert labs_data[0]["value"] == 5.2
    finally:
        # Restore original session factory
        mcp_router.get_session_factory = original_factory


# ── Security hardening ────────────────────────────────────────────────────────
async def test_oauth_code_single_use_rejects_reuse(auth_client, redis):
    """An authorization code is consumed atomically on first exchange; a second
    exchange with the same code is rejected (invalid_grant)."""
    approve = await auth_client.post(
        "/oauth/authorize/approve",
        data={"client_id": "vitals-claude-connector",
              "redirect_uri": "https://claude.ai/callback",
              "code_challenge": CODE_CHALLENGE, "code_challenge_method": "S256"},
    )
    code = approve.headers["location"].split("code=")[1].split("&")[0]

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://claude.ai/callback",
        "client_id": "vitals-claude-connector",
        "client_secret": "test-mcp-secret",
        "code_verifier": CODE_VERIFIER,
    }
    first = await auth_client.post("/oauth/token", data=body)
    assert first.status_code == 200
    second = await auth_client.post("/oauth/token", data=body)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


async def test_mcp_query_string_token_rejected(client):
    """A valid MCP token presented via ?token=/?access_token= (not the Authorization
    header) is rejected — query-string tokens leak into proxy logs, so only the
    Bearer header is accepted."""
    token = _get_mcp_serializer().dumps({
        "username": "tester",
        "client_id": "vitals-claude-connector",
        "type": "mcp_access_token",
    })
    assert (await client.get(f"/mcp/?token={token}")).status_code == 401
    assert (await client.get(f"/mcp/?access_token={token}")).status_code == 401


# ── PKCE is mandatory, not opportunistic ──────────────────────────────────────
async def test_authorize_without_pkce_is_refused(auth_client):
    """No challenge on the consent page means the token exchange would have nothing
    to verify — refuse before a code is ever minted."""
    response = await auth_client.get(
        "/oauth/authorize?response_type=code&client_id=vitals-claude-connector"
        "&redirect_uri=https://claude.ai/callback"
    )
    assert response.status_code == 200
    assert "PKCE" in response.text


async def test_authorize_with_plain_challenge_is_refused(auth_client):
    """The metadata advertises S256 only; ``plain`` protects nothing."""
    response = await auth_client.get(
        "/oauth/authorize?response_type=code&client_id=vitals-claude-connector"
        "&redirect_uri=https://claude.ai/callback"
        f"&code_challenge={CODE_CHALLENGE}&code_challenge_method=plain"
    )
    assert response.status_code == 200
    assert "PKCE" in response.text


async def test_approve_without_pkce_is_refused(auth_client):
    """The approve endpoint is reachable on its own — a code minted there without a
    challenge would be exchangeable without a verifier."""
    response = await auth_client.post(
        "/oauth/authorize/approve",
        data={"client_id": "vitals-claude-connector",
              "redirect_uri": "https://claude.ai/callback"},
    )
    assert response.status_code == 400


async def test_token_exchange_rejects_wrong_code_verifier(auth_client):
    """The whole point of PKCE: holding the code is not enough."""
    approve = await auth_client.post(
        "/oauth/authorize/approve",
        data={"client_id": "vitals-claude-connector",
              "redirect_uri": "https://claude.ai/callback",
              "code_challenge": CODE_CHALLENGE, "code_challenge_method": "S256"},
    )
    code = approve.headers["location"].split("code=")[1].split("&")[0]

    token_response = await auth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/callback",
            "client_id": "vitals-claude-connector",
            "client_secret": "test-mcp-secret",
            "code_verifier": "not-the-verifier",
        },
    )
    assert token_response.status_code == 400
    assert token_response.json()["error"] == "invalid_grant"


async def test_token_exchange_rejects_missing_code_verifier(auth_client):
    approve = await auth_client.post(
        "/oauth/authorize/approve",
        data={"client_id": "vitals-claude-connector",
              "redirect_uri": "https://claude.ai/callback",
              "code_challenge": CODE_CHALLENGE, "code_challenge_method": "S256"},
    )
    code = approve.headers["location"].split("code=")[1].split("&")[0]

    token_response = await auth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/callback",
            "client_id": "vitals-claude-connector",
            "client_secret": "test-mcp-secret",
        },
    )
    assert token_response.status_code == 400
    assert token_response.json()["error"] == "invalid_grant"


async def test_token_exchange_rejects_code_stored_without_challenge(auth_client, redis):
    """A code that somehow reached Redis without a challenge must not fall through
    to "PKCE wasn't requested, issue the token anyway"."""
    import json as _json

    code = "code_legacy_no_pkce"
    await redis.setex(f"oauth_code:{code}", 300, _json.dumps({
        "client_id": "vitals-claude-connector",
        "redirect_uri": "https://claude.ai/callback",
        "code_challenge": None,
        "code_challenge_method": None,
        "username": "tester",
    }))

    token_response = await auth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/callback",
            "client_id": "vitals-claude-connector",
            "client_secret": "test-mcp-secret",
        },
    )
    assert token_response.status_code == 400
    assert token_response.json()["error"] == "invalid_grant"


# ── Protected-resource discovery (RFC 9728) ───────────────────────────────────
async def test_protected_resource_metadata(client):
    response = await client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    data = response.json()
    assert data["resource"] == "http://test/mcp"
    assert data["authorization_servers"] == ["http://test"]
    assert data["bearer_methods_supported"] == ["header"]


async def test_401_points_at_the_resource_metadata(client):
    """A bare ``Bearer`` leaves a fresh client guessing where tokens come from."""
    response = await client.get("/mcp/")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    # The resource-specific form (RFC 9728 §3.1), and built from the configured
    # public URL rather than from ``request.base_url``. A token's audience is
    # bound to this identifier and an inbound ``Host`` header is something an
    # attacker chooses, so the name this installation answers to is configured
    # rather than observed. ``web/routers/oauth.py`` serves both paths.
    assert (
        'resource_metadata="http://test/.well-known/oauth-protected-resource/mcp"'
        in challenge
    ), challenge
    metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert metadata.status_code == 200, "the 401 points at a document nobody serves"


async def test_oauth_state_is_url_encoded(auth_client):
    """A state value carrying reserved characters is percent-encoded in the redirect
    so it can't break out and inject extra query parameters."""
    r = await auth_client.post(
        "/oauth/authorize/approve",
        data={"client_id": "vitals-claude-connector",
              "redirect_uri": "https://claude.ai/callback",
              "state": "a b&evil=1",
              "code_challenge": CODE_CHALLENGE, "code_challenge_method": "S256"},
    )
    loc = r.headers["location"]
    assert "state=a b&evil=1" not in loc          # raw reserved chars must not leak
    assert "a+b%26evil%3D1" in loc                 # '&' and '=' are encoded
