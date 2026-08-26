"""Administrator-approved registration is a bounded, redacted browser flow."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from vitals.enums import RegistrationRequestStatus, UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserFederatedIdentity, UserRole
from vitals.models.registration import RegistrationRequest
from vitals.services.authentication import admission
from vitals.services.authentication import registration as registration_policy
from vitals.services.authentication.admission import console as console_service
from web.auth import create_federated_session
from web.config import SESSION_COOKIE

ISSUER = "https://idp.example.test"


async def _user(db_session, slug: str, *, admin: bool = False) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-registration-request-hash",
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


def _configure_oidc(monkeypatch, *, issuer: str = ISSUER) -> None:
    monkeypatch.setenv("VITALS_OIDC_ISSUER", issuer)
    monkeypatch.setenv("VITALS_OIDC_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("VITALS_OIDC_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setenv(
        "VITALS_OIDC_REDIRECT_URL",
        "https://vitals.example.test/auth/callback",
    )
    monkeypatch.setenv("VITALS_PUBLIC_URL", "https://vitals.example.test")


async def _enable(db_session, monkeypatch) -> None:
    monkeypatch.setenv(registration_policy.REGISTRATION_UNLOCK_ENV, "1")
    await registration_policy.set_stored_mode(
        db_session,
        registration_policy.RegistrationMode.ADMIN_APPROVED,
    )


async def _request(db_session, *, suffix: str, issuer: str = ISSUER):
    return await admission.submit_request(
        db_session,
        issuer=issuer,
        subject=f"request-subject-{suffix}",
        verified_email=f"Private.{suffix}@Example.Test",
        email_verified=True,
        preferred_username=f"private-preferred-{suffix}",
    )


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


async def test_operator_sees_only_redacted_request_and_approves_member(
    client, db_session, monkeypatch, legacy_owner_roots
):
    _configure_oidc(monkeypatch)
    await _enable(db_session, monkeypatch)
    admin = await _user(db_session, "request-ui-admin", admin=True)
    row = await _request(db_session, suffix="approval")
    request_id = row.id
    await db_session.commit()
    _sign_in(client, admin)

    page = await client.get("/settings/platform/registration")
    assert page.status_code == 200
    assert "p***@example.test" in page.text
    assert str(request_id) in page.text
    for private in (
        "Private.approval@Example.Test",
        "request-subject-approval",
        "private-preferred-approval",
        ISSUER,
    ):
        assert private not in page.text
    approve_action = (
        f"/settings/platform/registration/requests/{request_id}/approve"
    )
    reject_action = f"/settings/platform/registration/requests/{request_id}/reject"
    assert f'action="{approve_action}" method="POST" hx-boost="false"' in page.text
    assert f'action="{reject_action}" method="POST" hx-boost="false"' in page.text
    assert f'aria-describedby="request-reference-{request_id}"' in page.text

    response = await client.post(approve_action, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("?decided=approved")
    db_session.expire_all()
    stored = await db_session.get(RegistrationRequest, request_id)
    assert stored.status == RegistrationRequestStatus.APPROVED.value
    link = await db_session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "request-subject-approval"
        )
    )
    assert link is not None
    assert await db_session.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == link.user_id)
    ) is not None


async def test_previous_provider_request_can_only_be_rejected(
    client, db_session, monkeypatch
):
    _configure_oidc(monkeypatch)
    await _enable(db_session, monkeypatch)
    admin = await _user(db_session, "previous-provider-admin", admin=True)
    row = await _request(
        db_session,
        suffix="previous-provider",
        issuer="https://old-idp.example.test",
    )
    request_id = row.id
    await db_session.commit()
    _sign_in(client, admin)

    page = await client.get("/settings/platform/registration")
    approve_action = (
        f"/settings/platform/registration/requests/{request_id}/approve"
    )
    reject_action = f"/settings/platform/registration/requests/{request_id}/reject"
    assert approve_action not in page.text
    assert reject_action in page.text
    refused = await client.post(approve_action, follow_redirects=False)
    assert refused.status_code == 303
    assert refused.headers["location"].endswith("?error=request_refused")
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity)
    ) == 0

    rejected = await client.post(
        reject_action,
        data={"reason": "Provider configuration changed."},
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    assert rejected.headers["location"].endswith("?decided=rejected")
    db_session.expire_all()
    stored = await db_session.get(RegistrationRequest, request_id)
    assert stored.status == RegistrationRequestStatus.REJECTED.value


async def test_request_can_be_rejected_but_not_approved_after_closure(
    client, db_session, monkeypatch
):
    _configure_oidc(monkeypatch)
    await _enable(db_session, monkeypatch)
    admin = await _user(db_session, "closed-request-admin", admin=True)
    row = await _request(db_session, suffix="closed")
    request_id = row.id
    await db_session.commit()
    await registration_policy.set_stored_mode(
        db_session,
        registration_policy.RegistrationMode.DISABLED,
    )
    await db_session.commit()
    _sign_in(client, admin)

    page = await client.get("/settings/platform/registration")
    assert str(request_id) in page.text
    approve_action = (
        f"/settings/platform/registration/requests/{request_id}/approve"
    )
    reject_action = f"/settings/platform/registration/requests/{request_id}/reject"
    assert approve_action not in page.text
    refused = await client.post(approve_action, follow_redirects=False)
    assert refused.headers["location"].endswith("?error=request_refused")
    rejected = await client.post(
        reject_action,
        data={"reason": "Registration is closed."},
        follow_redirects=False,
    )
    assert rejected.headers["location"].endswith("?decided=rejected")


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_request_decisions_require_recent_authentication(
    client, db_session, monkeypatch, action
):
    _configure_oidc(monkeypatch)
    await _enable(db_session, monkeypatch)
    admin = await _user(db_session, f"stale-request-{action}", admin=True)
    row = await _request(db_session, suffix=f"stale-{action}")
    request_id = row.id
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
        f"/settings/platform/registration/requests/{request_id}/{action}",
        data={"reason": "Not used for approve."},
        headers={
            "Accept": "text/html",
            "Referer": "http://test/settings/platform/registration",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/start?")
    db_session.expire_all()
    assert (
        await db_session.get(RegistrationRequest, request_id)
    ).status == RegistrationRequestStatus.PENDING.value


async def test_request_console_pages_independently_without_private_claims(
    db_session, monkeypatch
):
    await _enable(db_session, monkeypatch)
    admin = await _user(db_session, "request-console-admin", admin=True)
    first = await _request(db_session, suffix="console-first")
    second = await _request(db_session, suffix="console-second")
    monkeypatch.setattr(console_service, "CONSOLE_PAGE_SIZE", 1)

    first_page = await admission.registration_console(
        db_session,
        actor_user_id=admin.id,
        request_page=1,
        current_oidc_issuer=ISSUER,
    )
    second_page = await admission.registration_console(
        db_session,
        actor_user_id=admin.id,
        request_page=2,
        current_oidc_issuer=ISSUER,
    )
    assert first_page.total_live_requests == 2
    assert first_page.request_page_count == 2
    assert first_page.request_has_next is True
    assert second_page.request_has_previous is True
    assert first_page.requests[0].reference != second_page.requests[0].reference
    for entry in first_page.requests + second_page.requests:
        assert entry.masked_email.startswith("p***@")
        assert entry.account_kind == "member"
        assert entry.provider_current is True
        for private_attribute in (
            "issuer",
            "subject",
            "verified_email",
            "normalized_verified_email",
            "preferred_username",
            "review_note",
        ):
            assert not hasattr(entry, private_attribute)
    assert {first.id, second.id} == {
        first_page.requests[0].request_id,
        second_page.requests[0].request_id,
    }


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_non_operator_cannot_probe_request_decisions(
    client, db_session, monkeypatch, action
):
    _configure_oidc(monkeypatch)
    await _enable(db_session, monkeypatch)
    member = await _user(db_session, f"request-outsider-{action}")
    row = await _request(db_session, suffix=f"protected-{action}")
    request_id = row.id
    await db_session.commit()
    _sign_in(client, member)

    response = await client.post(
        f"/settings/platform/registration/requests/{request_id}/{action}",
        data={"reason": "unauthorized"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    db_session.expire_all()
    assert (
        await db_session.get(RegistrationRequest, request_id)
    ).status == RegistrationRequestStatus.PENDING.value
