"""An operator can deliver account invitations without replaying their secrets."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from vitals.enums import (
    RegistrationAccountKind,
    RegistrationInvitationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import AuditEvent, User, UserRole
from vitals.models.registration import RegistrationInvitation
from vitals.services.authentication import admission
from vitals.services.authentication import registration as registration_policy
from vitals.services.authentication.admission import console as console_service
from vitals.utils.timeutils import to_local_naive
from web.auth import create_federated_session
from web.config import SESSION_COOKIE


async def _user(db_session, slug: str, *, admin: bool = False) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-registration-ui-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    if admin:
        db_session.add(
            UserRole(user_id=user.id, role=UserRoleName.PLATFORM_SUPERADMIN.value)
        )
        await db_session.flush()
    return user


def _sign_in(client, user: User) -> None:
    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username=user.username,
            user_id=user.id,
            session_version=user.session_version,
            authenticated_at=int(datetime.now(timezone.utc).timestamp()),
            subject_id=None,
        ),
    )


def _configure_oidc(monkeypatch) -> None:
    monkeypatch.setenv("VITALS_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("VITALS_OIDC_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("VITALS_OIDC_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setenv(
        "VITALS_OIDC_REDIRECT_URL",
        "https://vitals.example.test/auth/callback",
    )
    monkeypatch.setenv("VITALS_PUBLIC_URL", "https://vitals.example.test")


def _nonce(page_text: str) -> str:
    match = re.search(r'name="request_nonce" value="([A-Za-z0-9_-]+)"', page_text)
    assert match is not None
    return match.group(1)


async def _enable_invites(db_session, monkeypatch) -> None:
    monkeypatch.setenv(registration_policy.REGISTRATION_UNLOCK_ENV, "1")
    await registration_policy.set_stored_mode(
        db_session,
        registration_policy.RegistrationMode.INVITE_ONLY,
    )


async def test_operator_opens_and_pauses_public_registration_in_the_ui(
    client, db_session, monkeypatch
):
    _configure_oidc(monkeypatch)
    monkeypatch.setenv(registration_policy.REGISTRATION_UNLOCK_ENV, "1")
    admin = await _user(db_session, "registration-mode-admin", admin=True)
    await db_session.commit()
    _sign_in(client, admin)

    page = await client.get("/settings/platform/registration")
    assert page.status_code == 200
    assert 'action="/settings/platform/registration/mode"' in page.text
    assert 'name="mode" value="open"' in page.text
    assert "Configured mode" not in page.text
    assert "Настроенный режим" not in page.text

    opened = await client.post(
        "/settings/platform/registration/mode",
        data={"mode": "open"},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    assert opened.headers["location"].endswith(
        "?decided=registration_opened"
    )
    assert (
        await registration_policy.get_stored_mode(db_session)
        is registration_policy.RegistrationMode.OPEN
    )
    event = await db_session.scalar(
        select(AuditEvent)
        .where(AuditEvent.event_type == "registration.mode.changed")
        .order_by(AuditEvent.occurred_at.desc())
    )
    assert event is not None
    assert event.actor_user_id == admin.id
    assert event.metadata_json["source_surface"] == "web.platform"

    open_page = await client.get("/settings/platform/registration")
    assert 'name="mode" value="disabled"' in open_page.text
    paused = await client.post(
        "/settings/platform/registration/mode",
        data={"mode": "disabled"},
        follow_redirects=False,
    )
    assert paused.status_code == 303
    assert paused.headers["location"].endswith(
        "?decided=registration_paused"
    )
    assert (
        await registration_policy.get_stored_mode(db_session)
        is registration_policy.RegistrationMode.DISABLED
    )


async def test_public_registration_mode_change_requires_a_live_operator(
    client, db_session, monkeypatch
):
    _configure_oidc(monkeypatch)
    monkeypatch.setenv(registration_policy.REGISTRATION_UNLOCK_ENV, "1")
    member = await _user(db_session, "registration-mode-member")
    await db_session.commit()
    _sign_in(client, member)

    response = await client.post(
        "/settings/platform/registration/mode",
        data={"mode": "open"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert (
        await registration_policy.get_stored_mode(db_session)
        is registration_policy.RegistrationMode.DISABLED
    )


async def test_operator_issues_one_redacted_non_replayable_link(
    client, db_session, monkeypatch
):
    _configure_oidc(monkeypatch)
    await _enable_invites(db_session, monkeypatch)
    admin = await _user(db_session, "registration-ui-admin", admin=True)
    await db_session.commit()
    _sign_in(client, admin)

    page = await client.get("/settings/platform/registration")
    assert page.status_code == 200
    assert 'hx-boost="false"' in page.text
    assert 'action="/settings/platform/registration/invitations"' in page.text
    request_nonce = _nonce(page.text)

    response = await client.post(
        "/settings/platform/registration/invitations",
        data={
            "email": "Sensitive.Person@Example.Test",
            "account_kind": "doctor",
            "request_nonce": request_nonce,
        },
        headers={"Host": "host-header.example"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "location" not in response.headers
    match = re.search(
        r"https://vitals\.example\.test/register/invite#token=([A-Za-z0-9_-]+)",
        response.text,
    )
    assert match is not None
    token = match.group(1)
    assert "host-header.example" not in response.text
    assert "Sensitive.Person@Example.Test" not in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in response.headers["cache-control"]
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "cloudflare" not in response.headers["content-security-policy"]

    invitation = await db_session.scalar(select(RegistrationInvitation))
    assert invitation is not None
    invitation_id = invitation.id
    invitation_reference = str(invitation_id)
    assert invitation_reference in response.text
    assert invitation.token_digest == hashlib.sha256(token.encode()).hexdigest()
    assert token != invitation.token_digest
    events = list(await db_session.scalars(select(AuditEvent)))
    serialized_audit = json.dumps(
        [event.metadata_json for event in events], sort_keys=True
    )
    assert token not in serialized_audit
    assert "sensitive.person@example.test" not in serialized_audit

    later = await client.get("/settings/platform/registration")
    assert later.status_code == 200
    assert token not in later.text
    assert "Sensitive.Person@Example.Test" not in later.text
    assert "s***@example.test" in later.text
    assert invitation_reference in later.text
    assert f'aria-describedby="invitation-reference-{invitation_id}"' in later.text

    replay = await client.post(
        "/settings/platform/registration/invitations",
        data={
            "email": "Sensitive.Person@Example.Test",
            "account_kind": "doctor",
            "request_nonce": request_nonce,
        },
        follow_redirects=False,
    )
    assert replay.status_code == 303
    assert replay.headers["location"].endswith("?error=replayed")
    assert await db_session.scalar(
        select(RegistrationInvitation.id).where(
            RegistrationInvitation.status
            == RegistrationInvitationStatus.PENDING.value
        )
    ) == invitation_id

    unicode_nonce = await client.post(
        "/settings/platform/registration/invitations",
        data={
            "email": "unicode@example.test",
            "account_kind": "member",
            "request_nonce": "я" * 32,
        },
        follow_redirects=False,
    )
    assert unicode_nonce.status_code == 303
    assert unicode_nonce.headers["location"].endswith("?error=replayed")


async def test_operator_can_revoke_after_registration_is_closed(
    client, db_session, monkeypatch
):
    _configure_oidc(monkeypatch)
    await _enable_invites(db_session, monkeypatch)
    admin = await _user(db_session, "registration-revoke-admin", admin=True)
    issued = await admission.issue_invitation(
        db_session,
        actor_user_id=admin.id,
        email="revoke@example.test",
        account_kind=RegistrationAccountKind.MEMBER,
    )
    invitation_id = issued.invitation.id
    await db_session.commit()
    await registration_policy.set_stored_mode(
        db_session,
        registration_policy.RegistrationMode.DISABLED,
    )
    await db_session.commit()
    _sign_in(client, admin)

    page = await client.get("/settings/platform/registration")
    assert "Create invitation" not in page.text
    assert "r***@example.test" in page.text
    response = await client.post(
        f"/settings/platform/registration/invitations/{invitation_id}/revoke",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?decided=revoked")
    db_session.expire_all()
    assert (
        await db_session.get(RegistrationInvitation, invitation_id)
    ).status == RegistrationInvitationStatus.REVOKED.value


async def test_non_operator_cannot_read_or_probe_issuance_configuration(
    client, db_session, monkeypatch
):
    member = await _user(db_session, "registration-ui-member")
    await db_session.commit()
    _sign_in(client, member)

    page = await client.get("/settings/platform/registration")
    created = await client.post(
        "/settings/platform/registration/invitations",
        data={"email": "probe@example.test", "account_kind": "member"},
        follow_redirects=False,
    )
    assert page.status_code == 403
    assert created.status_code == 403
    assert await db_session.scalar(select(RegistrationInvitation.id)) is None


async def test_stale_authentication_cannot_issue_an_invitation(
    client, db_session, monkeypatch
):
    _configure_oidc(monkeypatch)
    await _enable_invites(db_session, monkeypatch)
    admin = await _user(db_session, "registration-stale-admin", admin=True)
    await db_session.commit()
    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username=admin.username,
            user_id=admin.id,
            session_version=admin.session_version,
            authenticated_at=int(datetime.now(timezone.utc).timestamp()) - 3600,
            subject_id=None,
        ),
    )

    response = await client.post(
        "/settings/platform/registration/invitations",
        data={"email": "stale@example.test", "account_kind": "trainer"},
        headers={
            "Accept": "text/html",
            "Referer": "http://test/settings/platform/registration",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/start?")
    assert await db_session.scalar(select(RegistrationInvitation.id)) is None


@pytest.mark.parametrize("account_kind", ["platform_superadmin", "unknown", ""])
async def test_privileged_or_unknown_account_shapes_are_refused_generically(
    client, db_session, monkeypatch, account_kind
):
    _configure_oidc(monkeypatch)
    await _enable_invites(db_session, monkeypatch)
    admin = await _user(db_session, f"registration-kind-{account_kind or 'blank'}", admin=True)
    await db_session.commit()
    _sign_in(client, admin)

    page = await client.get("/settings/platform/registration")
    request_nonce = _nonce(page.text)

    response = await client.post(
        "/settings/platform/registration/invitations",
        data={
            "email": "kind@example.test",
            "account_kind": account_kind,
            "request_nonce": request_nonce,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?error=refused")
    assert await db_session.scalar(select(RegistrationInvitation.id)) is None


async def test_console_dto_is_bounded_redacted_and_excludes_expired_rows(
    db_session, monkeypatch
):
    monkeypatch.setenv("VITALS_TIMEZONE", "America/Los_Angeles")
    admin = await _user(db_session, "registration-console-admin", admin=True)
    now = datetime.now(timezone.utc)
    rows = (
        RegistrationInvitation(
            token_digest="1" * 64,
            normalized_email="another.secret@example.test",
            account_kind=RegistrationAccountKind.TRAINER.value,
            invited_by_user_id=admin.id,
            expires_at=now + timedelta(days=2),
            created_at=now - timedelta(days=2),
        ),
        RegistrationInvitation(
            token_digest="2" * 64,
            normalized_email="alice.secret@example.test",
            account_kind=RegistrationAccountKind.MEMBER.value,
            invited_by_user_id=admin.id,
            expires_at=now + timedelta(days=1),
            created_at=now - timedelta(days=1),
        ),
        RegistrationInvitation(
            token_digest="3" * 64,
            normalized_email="expired.secret@example.test",
            account_kind=RegistrationAccountKind.DOCTOR.value,
            invited_by_user_id=admin.id,
            expires_at=now - timedelta(days=1),
            created_at=now - timedelta(days=2),
        ),
    )
    db_session.add_all(rows)
    await db_session.flush()

    console = await admission.registration_console(
        db_session,
        actor_user_id=admin.id,
    )
    assert [entry.masked_email for entry in console.invitations] == [
        "a***@example.test",
        "a***@example.test",
    ]
    assert len({entry.reference for entry in console.invitations}) == 2
    assert console.total_live_invitations == 2
    assert console.page == 1
    assert console.page_count == 1
    assert console.has_previous is False
    assert console.has_next is False
    assert console.invitations[0].expires_at == to_local_naive(rows[1].expires_at)
    assert all(not hasattr(entry, "normalized_email") for entry in console.invitations)
    assert all(not hasattr(entry, "token_digest") for entry in console.invitations)

    monkeypatch.setattr(console_service, "CONSOLE_PAGE_SIZE", 1)
    truncated = await admission.registration_console(
        db_session,
        actor_user_id=admin.id,
    )
    assert len(truncated.invitations) == 1
    assert truncated.total_live_invitations == 2
    assert truncated.page_count == 2
    assert truncated.has_next is True
    second_page = await admission.registration_console(
        db_session,
        actor_user_id=admin.id,
        page=2,
    )
    assert len(second_page.invitations) == 1
    assert second_page.has_previous is True
    assert second_page.invitations[0].reference != truncated.invitations[0].reference


async def test_console_service_rechecks_the_live_operator_role(db_session):
    member = await _user(db_session, "registration-console-member")
    with pytest.raises(admission.AdmissionForbidden):
        await admission.registration_console(
            db_session,
            actor_user_id=member.id,
        )


async def test_operator_cannot_issue_without_oidc_or_invite_mode(
    client, db_session, monkeypatch
):
    admin = await _user(db_session, "registration-unavailable-admin", admin=True)
    await _enable_invites(db_session, monkeypatch)
    await db_session.commit()
    _sign_in(client, admin)

    without_oidc = await client.post(
        "/settings/platform/registration/invitations",
        data={
            "email": "closed@example.test",
            "account_kind": "member",
            "request_nonce": "a" * 32,
        },
        follow_redirects=False,
    )
    assert without_oidc.status_code == 303
    assert without_oidc.headers["location"].endswith("?error=unavailable")

    _configure_oidc(monkeypatch)
    await registration_policy.set_stored_mode(
        db_session,
        registration_policy.RegistrationMode.DISABLED,
    )
    await db_session.commit()
    wrong_mode = await client.post(
        "/settings/platform/registration/invitations",
        data={
            "email": "closed@example.test",
            "account_kind": "member",
            "request_nonce": "b" * 32,
        },
        follow_redirects=False,
    )
    assert wrong_mode.status_code == 303
    assert wrong_mode.headers["location"].endswith("?error=refused")
    assert await db_session.scalar(select(RegistrationInvitation.id)) is None
