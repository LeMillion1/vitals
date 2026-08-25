"""The operator can finish professional onboarding without database access."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from vitals.enums import (
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import AuditEvent, User, UserRole
from vitals.models.professional import ProfessionalProfile
from vitals.services.care import professionals
from web.auth import create_federated_session, create_session
from web.config import SESSION_COOKIE


async def _user(session, slug: str, *, roles=()) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role=role.value))
    await session.flush()
    return user


async def _claim(session, slug: str) -> tuple[User, ProfessionalProfile]:
    doctor = await _user(session, slug, roles=(UserRoleName.DOCTOR,))
    profile = await professionals.submit_profile(
        session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name=f"Dr {slug}",
        credential_reference=f"LIC-{slug}",
    )
    return doctor, profile


async def _admin(session, slug: str) -> User:
    return await _user(
        session, slug, roles=(UserRoleName.PLATFORM_SUPERADMIN,)
    )


def _sign_in(client, user: User) -> None:
    client.cookies.set(SESSION_COOKIE, create_session(user.username))


async def test_the_operator_sees_the_queue_and_verifies_one_profile(
    client, db_session
):
    admin = await _admin(db_session, "review-ui-admin")
    doctor, profile = await _claim(db_session, "review-ui-doctor")
    admin_id = admin.id
    profile_id = profile.id
    display_name = profile.display_name
    credential_reference = profile.credential_reference
    doctor_username = doctor.username
    await db_session.commit()
    _sign_in(client, admin)

    page = await client.get(
        "/settings/platform/professionals", headers={"Accept": "text/html"}
    )
    assert page.status_code == 200
    assert display_name in page.text
    assert credential_reference in page.text
    assert doctor_username in page.text
    assert (
        f'action="/settings/platform/professionals/{profile_id}/verify" '
        'method="POST" hx-boost="false"'
    ) in page.text
    assert (
        f'action="/settings/platform/professionals/{profile_id}/reject" '
        'method="POST" hx-boost="false"'
    ) in page.text

    response = await client.post(
        f"/settings/platform/professionals/{profile_id}/verify",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?decided=verify")

    db_session.expire_all()
    stored = await db_session.get(ProfessionalProfile, profile_id)
    assert stored.verification_status == ProfessionalVerificationStatus.VERIFIED.value
    event = await db_session.scalar(select(AuditEvent))
    assert event.actor_user_id == admin_id
    assert event.resource_id == str(profile_id)


async def test_reject_suspend_and_reinstate_are_explicit_state_changes(
    client, db_session
):
    admin = await _admin(db_session, "review-ui-lifecycle-admin")
    _rejected_user, rejected = await _claim(db_session, "review-ui-rejected")
    _active_user, active = await _claim(db_session, "review-ui-active")
    rejected_id = rejected.id
    active_id = active.id
    await db_session.commit()
    _sign_in(client, admin)

    blank_reason = await client.post(
        f"/settings/platform/professionals/{rejected_id}/reject",
        data={"note": "   "},
        follow_redirects=False,
    )
    assert blank_reason.status_code == 303
    assert blank_reason.headers["location"].endswith("?error=refused")
    rejected_response = await client.post(
        f"/settings/platform/professionals/{rejected_id}/reject",
        data={"note": "Use the number from the public register."},
        follow_redirects=False,
    )
    assert rejected_response.status_code == 303
    verified_response = await client.post(
        f"/settings/platform/professionals/{active_id}/verify",
        follow_redirects=False,
    )
    assert verified_response.status_code == 303
    verified_page = await client.get("/settings/platform/professionals")
    assert (
        f'action="/settings/platform/professionals/{active_id}/suspend" '
        'method="POST" hx-boost="false"'
    ) in verified_page.text
    suspended_response = await client.post(
        f"/settings/platform/professionals/{active_id}/suspend",
        data={"note": "Credential expired."},
        follow_redirects=False,
    )
    assert suspended_response.status_code == 303

    db_session.expire_all()
    assert (await db_session.get(ProfessionalProfile, rejected_id)).review_note == (
        "Use the number from the public register."
    )
    assert (
        await db_session.get(ProfessionalProfile, active_id)
    ).verification_status == ProfessionalVerificationStatus.SUSPENDED.value
    suspended_page = await client.get("/settings/platform/professionals")
    assert (
        f'action="/settings/platform/professionals/{active_id}/reinstate" '
        'method="POST" hx-boost="false"'
    ) in suspended_page.text

    reinstated = await client.post(
        f"/settings/platform/professionals/{active_id}/reinstate",
        follow_redirects=False,
    )
    assert reinstated.status_code == 303
    db_session.expire_all()
    restored = await db_session.get(ProfessionalProfile, active_id)
    assert restored.verification_status == ProfessionalVerificationStatus.VERIFIED.value
    assert restored.review_note is None


async def test_a_stale_form_cannot_overwrite_an_operator_decision(
    client, db_session
):
    admin = await _admin(db_session, "review-ui-stale-admin")
    _doctor, profile = await _claim(db_session, "review-ui-stale")
    profile_id = profile.id
    await db_session.commit()
    _sign_in(client, admin)

    first = await client.post(
        f"/settings/platform/professionals/{profile_id}/verify",
        follow_redirects=False,
    )
    stale = await client.post(
        f"/settings/platform/professionals/{profile_id}/reject",
        data={"note": "This stale form must not win."},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert stale.status_code == 303
    assert stale.headers["location"].endswith("?error=refused")

    db_session.expire_all()
    stored = await db_session.get(ProfessionalProfile, profile_id)
    assert stored.verification_status == ProfessionalVerificationStatus.VERIFIED.value
    assert stored.review_note is None
    assert len(list(await db_session.scalars(select(AuditEvent)))) == 1


async def test_non_operator_cannot_read_or_mutate_the_queue(client, db_session):
    member = await _user(db_session, "review-ui-member")
    _doctor, profile = await _claim(db_session, "review-ui-protected")
    profile_id = profile.id
    await db_session.commit()
    _sign_in(client, member)

    page = await client.get(
        "/settings/platform/professionals", headers={"Accept": "text/html"}
    )
    changed = await client.post(
        f"/settings/platform/professionals/{profile_id}/verify",
        follow_redirects=False,
    )
    assert page.status_code == 403
    assert changed.status_code == 403
    db_session.expire_all()
    assert (
        await db_session.get(ProfessionalProfile, profile_id)
    ).verification_status == ProfessionalVerificationStatus.PENDING.value


async def test_review_actions_require_recent_authentication(client, db_session):
    admin = await _admin(db_session, "review-ui-stale-login-admin")
    _doctor, profile = await _claim(db_session, "review-ui-stale-login")
    profile_id = profile.id
    admin_username = admin.username
    admin_id = admin.id
    admin_session_version = admin.session_version
    await db_session.commit()
    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username=admin_username,
            user_id=admin_id,
            session_version=admin_session_version,
            authenticated_at=int(datetime.now(timezone.utc).timestamp()) - 3600,
            subject_id=None,
        ),
    )

    response = await client.post(
        f"/settings/platform/professionals/{profile_id}/verify",
        headers={
            "Accept": "text/html",
            "Referer": "http://test/settings/platform/professionals",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?")
    db_session.expire_all()
    assert (
        await db_session.get(ProfessionalProfile, profile_id)
    ).verification_status == ProfessionalVerificationStatus.PENDING.value
