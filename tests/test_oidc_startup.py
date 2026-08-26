"""OIDC process startup must prove a reachable durable local identity."""
from __future__ import annotations

import pytest

from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import (
    HealthSubject,
    User,
    UserFederatedIdentity,
    UserRole,
)
from vitals.services.authentication.startup import (
    OidcStartupStateError,
    validate_oidc_startup_state,
)

ISSUER = "https://idp.example.test"
OTHER_ISSUER = "https://old-idp.example.test"
BOOTSTRAP_SUBJECT = "provider-owner-subject"


async def _user(
    db_session,
    name: str = "owner",
    *,
    status: UserStatus = UserStatus.ACTIVE,
    with_roles: bool = True,
) -> User:
    user = User(
        username=name,
        normalized_username=name,
        password_hash=None,
        status=status.value,
    )
    db_session.add(user)
    await db_session.flush()
    if with_roles:
        for role in (UserRoleName.MEMBER, UserRoleName.PLATFORM_SUPERADMIN):
            db_session.add(UserRole(user_id=user.id, role=role.value))
        await db_session.flush()
    return user


async def _subject(db_session, owner: User) -> HealthSubject:
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=owner.username,
        timezone="Asia/Almaty",
    )
    db_session.add(subject)
    await db_session.flush()
    return subject


async def test_unlinked_oidc_startup_accepts_exact_owner_bootstrap(db_session):
    owner = await _user(db_session)
    await _subject(db_session, owner)

    await validate_oidc_startup_state(
        db_session,
        issuer=ISSUER,
        bootstrap_subject=BOOTSTRAP_SUBJECT,
    )


@pytest.mark.parametrize("bootstrap_subject", ("", "   "))
async def test_unlinked_oidc_startup_requires_explicit_subject(
    db_session,
    bootstrap_subject,
):
    owner = await _user(db_session)
    await _subject(db_session, owner)

    with pytest.raises(OidcStartupStateError, match="BOOTSTRAP_SUBJECT"):
        await validate_oidc_startup_state(
            db_session,
            issuer=ISSUER,
            bootstrap_subject=bootstrap_subject,
        )


async def test_unlinked_oidc_startup_rejects_missing_owner_subject(db_session):
    await _user(db_session)

    with pytest.raises(OidcStartupStateError, match="one health subject"):
        await validate_oidc_startup_state(
            db_session,
            issuer=ISSUER,
            bootstrap_subject=BOOTSTRAP_SUBJECT,
        )


async def test_unlinked_oidc_startup_rejects_missing_owner_roles(db_session):
    owner = await _user(db_session, with_roles=False)
    await _subject(db_session, owner)

    with pytest.raises(OidcStartupStateError, match="platform_superadmin"):
        await validate_oidc_startup_state(
            db_session,
            issuer=ISSUER,
            bootstrap_subject=BOOTSTRAP_SUBJECT,
        )


async def test_unlinked_oidc_startup_rejects_ambiguous_users(db_session):
    first = await _user(db_session, "first")
    await _subject(db_session, first)
    await _user(db_session, "second")

    with pytest.raises(OidcStartupStateError, match="exactly one existing user"):
        await validate_oidc_startup_state(
            db_session,
            issuer=ISSUER,
            bootstrap_subject=BOOTSTRAP_SUBJECT,
        )


async def test_linked_oidc_startup_needs_no_bootstrap_subject(db_session):
    owner = await _user(db_session)
    await _subject(db_session, owner)
    db_session.add(
        UserFederatedIdentity(
            user_id=owner.id,
            issuer=ISSUER,
            subject=BOOTSTRAP_SUBJECT,
        )
    )
    await db_session.flush()

    await validate_oidc_startup_state(
        db_session,
        issuer=ISSUER,
        bootstrap_subject="",
    )


async def test_linked_oidc_startup_rejects_stale_wrong_bootstrap_subject(db_session):
    owner = await _user(db_session)
    await _subject(db_session, owner)
    db_session.add(
        UserFederatedIdentity(
            user_id=owner.id,
            issuer=ISSUER,
            subject=BOOTSTRAP_SUBJECT,
        )
    )
    await db_session.flush()

    with pytest.raises(OidcStartupStateError, match="does not match"):
        await validate_oidc_startup_state(
            db_session,
            issuer=ISSUER,
            bootstrap_subject="wrong-owner-subject",
        )


async def test_oidc_startup_rejects_provider_switch_without_binding(db_session):
    owner = await _user(db_session)
    await _subject(db_session, owner)
    db_session.add(
        UserFederatedIdentity(
            user_id=owner.id,
            issuer=OTHER_ISSUER,
            subject=BOOTSTRAP_SUBJECT,
        )
    )
    await db_session.flush()

    with pytest.raises(OidcStartupStateError, match="does not match"):
        await validate_oidc_startup_state(
            db_session,
            issuer=ISSUER,
            bootstrap_subject=BOOTSTRAP_SUBJECT,
        )


async def test_oidc_startup_rejects_only_inactive_link(db_session):
    owner = await _user(db_session, status=UserStatus.SUSPENDED)
    await _subject(db_session, owner)
    db_session.add(
        UserFederatedIdentity(
            user_id=owner.id,
            issuer=ISSUER,
            subject=BOOTSTRAP_SUBJECT,
        )
    )
    await db_session.flush()

    with pytest.raises(OidcStartupStateError, match="no active platform"):
        await validate_oidc_startup_state(
            db_session,
            issuer=ISSUER,
            bootstrap_subject="",
        )
