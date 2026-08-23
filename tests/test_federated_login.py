"""A valid provider login is not an account, and must not become one.

The OIDC boundary decides whether a token is genuine. This decides whether the
person it describes may have a session here — a different question, and the one
where a self-hosted health record either stays self-hosted or quietly becomes
open to anybody who can register with the provider.

Provisioning is closed. The single exception is the one-time binding that lets
an installation which predates federated login recognise its existing owner,
and these tests are mostly about the ways that exception must not be usable.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vitals.enums import UserStatus
from vitals.models.identity import User, UserFederatedIdentity
from vitals.services.federated_login_service import (
    BootstrapRefused,
    FederatedLoginError,
    InactiveAccount,
    UnknownFederatedIdentity,
    resolve_federated_user,
)

ISSUER = "https://idp.example.test"
OWNER_SUBJECT = "provider-subject-owner"


async def _user(db_session, label: str, *, status=UserStatus.ACTIVE) -> User:
    user = User(
        username=label,
        normalized_username=label,
        password_hash=None,
        status=status.value,
    )
    db_session.add(user)
    await db_session.flush()
    return user


# ── The closed door ──────────────────────────────────────────────────────────

async def test_an_unknown_identity_gets_no_account(db_session):
    """The property the whole module exists for.

    Anybody can register with the provider. That must not be a way in here.
    """

    await _user(db_session, "owner")
    with pytest.raises(UnknownFederatedIdentity):
        await resolve_federated_user(
            db_session, issuer=ISSUER, subject="a-stranger"
        )
    assert await db_session.scalar(
        __import__("sqlalchemy").select(
            __import__("sqlalchemy").func.count()
        ).select_from(UserFederatedIdentity)
    ) == 0


async def test_an_unknown_identity_and_an_exhausted_bootstrap_read_the_same(
    db_session,
):
    """A stranger learns nothing about whether this installation has an owner."""

    owner = await _user(db_session, "owner")
    db_session.add(
        UserFederatedIdentity(
            user_id=owner.id, issuer=ISSUER, subject=OWNER_SUBJECT
        )
    )
    await db_session.flush()

    with pytest.raises(UnknownFederatedIdentity) as stranger:
        await resolve_federated_user(
            db_session, issuer=ISSUER, subject="a-stranger"
        )
    with pytest.raises(UnknownFederatedIdentity) as exhausted:
        await resolve_federated_user(
            db_session,
            issuer=ISSUER,
            subject="another-stranger",
            bootstrap_subject="another-stranger",
        )
    # One kind of refusal for both, so a caller has nothing to branch on and
    # cannot turn the difference into an answer. The messages differ and go to
    # the log; the route below renders one response for either.
    assert isinstance(stranger.value, UnknownFederatedIdentity)
    assert isinstance(exhausted.value, UnknownFederatedIdentity)


async def test_the_same_subject_from_another_issuer_is_a_stranger(db_session):
    """The identity is the pair, not the subject.

    Subjects are opaque and only unique inside their issuer; accepting one
    without checking where it came from would let any provider mint anybody.
    """

    owner = await _user(db_session, "owner")
    db_session.add(
        UserFederatedIdentity(
            user_id=owner.id, issuer=ISSUER, subject=OWNER_SUBJECT
        )
    )
    await db_session.flush()

    with pytest.raises(UnknownFederatedIdentity):
        await resolve_federated_user(
            db_session,
            issuer="https://another-idp.example.test",
            subject=OWNER_SUBJECT,
        )


async def test_a_blank_issuer_or_subject_is_refused(db_session):
    for issuer, subject in ((ISSUER, "   "), ("  ", OWNER_SUBJECT), ("", "")):
        with pytest.raises(FederatedLoginError):
            await resolve_federated_user(
                db_session, issuer=issuer, subject=subject
            )


# ── A recognised identity ────────────────────────────────────────────────────

async def test_a_linked_identity_resolves_to_its_user(db_session):
    owner = await _user(db_session, "owner")
    db_session.add(
        UserFederatedIdentity(
            user_id=owner.id, issuer=ISSUER, subject=OWNER_SUBJECT
        )
    )
    await db_session.flush()

    when = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)
    resolved = await resolve_federated_user(
        db_session, issuer=ISSUER, subject=OWNER_SUBJECT, authenticated_at=when
    )
    assert resolved.id == owner.id
    assert resolved.last_login_at is not None

    link = await db_session.scalar(
        __import__("sqlalchemy").select(UserFederatedIdentity)
    )
    # SQLite has no timezone-aware storage, so compare the instant rather than
    # the object. On PostgreSQL both sides are aware and this still holds.
    stored = link.last_authenticated_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == when


async def test_a_suspended_account_cannot_log_in_through_the_provider(db_session):
    """Suspension is Vitals' decision and the provider does not know about it."""

    owner = await _user(db_session, "owner", status=UserStatus.SUSPENDED)
    db_session.add(
        UserFederatedIdentity(
            user_id=owner.id, issuer=ISSUER, subject=OWNER_SUBJECT
        )
    )
    await db_session.flush()

    with pytest.raises(InactiveAccount):
        await resolve_federated_user(
            db_session, issuer=ISSUER, subject=OWNER_SUBJECT
        )


# ── The one-time binding ─────────────────────────────────────────────────────

async def test_the_operator_named_subject_binds_the_existing_owner_once(db_session):
    owner = await _user(db_session, "owner")

    resolved = await resolve_federated_user(
        db_session,
        issuer=ISSUER,
        subject=OWNER_SUBJECT,
        bootstrap_subject=OWNER_SUBJECT,
    )
    assert resolved.id == owner.id

    # A second identity cannot use the same door, even if an operator left the
    # setting in place.
    with pytest.raises(UnknownFederatedIdentity):
        await resolve_federated_user(
            db_session,
            issuer=ISSUER,
            subject="somebody-else",
            bootstrap_subject="somebody-else",
        )


async def test_the_binding_needs_exactly_one_existing_user(db_session):
    """With two users there is no "the owner" to bind, and guessing is not an option."""

    await _user(db_session, "first")
    await _user(db_session, "second")

    with pytest.raises(BootstrapRefused, match="exactly one"):
        await resolve_federated_user(
            db_session,
            issuer=ISSUER,
            subject=OWNER_SUBJECT,
            bootstrap_subject=OWNER_SUBJECT,
        )


async def test_the_binding_refuses_once_any_identity_is_linked(db_session):
    owner = await _user(db_session, "owner")
    db_session.add(
        UserFederatedIdentity(
            user_id=owner.id, issuer=ISSUER, subject="already-linked"
        )
    )
    await db_session.flush()

    with pytest.raises(BootstrapRefused, match="already has a federated identity"):
        await resolve_federated_user(
            db_session,
            issuer=ISSUER,
            subject=OWNER_SUBJECT,
            bootstrap_subject=OWNER_SUBJECT,
        )


async def test_a_subject_the_operator_did_not_name_cannot_bind(db_session):
    """Configuring the door does not leave it open to whoever arrives first."""

    await _user(db_session, "owner")

    with pytest.raises(UnknownFederatedIdentity):
        await resolve_federated_user(
            db_session,
            issuer=ISSUER,
            subject="not-the-configured-one",
            bootstrap_subject=OWNER_SUBJECT,
        )


async def test_no_bootstrap_configured_means_no_binding(db_session):
    await _user(db_session, "owner")
    with pytest.raises(UnknownFederatedIdentity):
        await resolve_federated_user(
            db_session, issuer=ISSUER, subject=OWNER_SUBJECT
        )
