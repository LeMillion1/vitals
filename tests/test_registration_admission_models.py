"""Persistence contracts for account-admission invitations and requests.

The services that will act on these rows come later.  These tests deliberately
exercise the ``create_all`` schema directly so an invalid lifecycle shape is
rejected even when a future caller forgets a service-level check.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from vitals.enums import (
    RegistrationAccountKind,
    RegistrationInvitationStatus,
    RegistrationRequestStatus,
    UserStatus,
)
from vitals.models.base import Base
from vitals.models.identity import User
from vitals.models.registration import RegistrationInvitation, RegistrationRequest


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _user(session: AsyncSession, slug: str) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-registration-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    return user


def _invitation_values(inviter: User | None, *, suffix: str = "base") -> dict:
    return {
        "token_digest": hashlib.sha256(suffix.encode()).hexdigest(),
        "normalized_email": f"{suffix}@example.test",
        "account_kind": RegistrationAccountKind.MEMBER.value,
        "invited_by_user_id": inviter.id if inviter is not None else None,
        "expires_at": _now() + timedelta(days=1),
    }


def _request_values(*, suffix: str = "base") -> dict:
    return {
        "issuer": "https://idp.example.test",
        "subject": f"subject-{suffix}",
        "verified_email": f"{suffix}@example.test",
        "normalized_verified_email": f"{suffix}@example.test",
        "preferred_username": f"member-{suffix}",
        "account_kind": RegistrationAccountKind.MEMBER.value,
        "expires_at": _now() + timedelta(days=1),
    }


async def test_create_all_registers_admission_tables_constraints_and_restrict_fks(
    db_session,
):
    connection = await db_session.connection()

    def _schema(sync_connection):
        inspector = inspect(sync_connection)
        tables = set(inspector.get_table_names())
        checks = {
            table: {item["name"] for item in inspector.get_check_constraints(table)}
            for table in ("registration_invitations", "registration_requests")
        }
        foreign_keys = {
            (table, tuple(item["constrained_columns"])): (
                item["referred_table"],
                (item.get("options") or {}).get("ondelete"),
            )
            for table in ("registration_invitations", "registration_requests")
            for item in inspector.get_foreign_keys(table)
        }
        return tables, checks, foreign_keys

    tables, checks, foreign_keys = await connection.run_sync(_schema)
    assert {"registration_invitations", "registration_requests"} <= tables
    assert {
        "ck_registration_invitations_token_digest",
        "ck_registration_invitations_normalized_email",
        "ck_registration_invitations_expiry",
        "ck_registration_invitations_state",
        "ck_registration_invitations_purge",
        "ck_registration_invitations_purge_time",
    } <= checks["registration_invitations"]
    assert {
        "ck_registration_requests_verified_email_pair",
        "ck_registration_requests_expiry",
        "ck_registration_requests_last_seen",
        "ck_registration_requests_state",
        "ck_registration_requests_purge",
        "ck_registration_requests_purge_time",
    } <= checks["registration_requests"]

    for column in (
        "invited_by_user_id",
        "consumed_by_user_id",
        "revoked_by_user_id",
    ):
        assert foreign_keys[("registration_invitations", (column,))] == (
            "users",
            "RESTRICT",
        )
    for column in ("reviewer_user_id", "provisioned_user_id"):
        assert foreign_keys[("registration_requests", (column,))] == (
            "users",
            "RESTRICT",
        )


async def test_pending_rows_receive_database_defaults(db_session):
    inviter = await _user(db_session, "registration-default-inviter")
    invitation = RegistrationInvitation(**_invitation_values(inviter))
    request = RegistrationRequest(**_request_values())
    db_session.add_all([invitation, request])
    await db_session.flush()

    assert invitation.status == RegistrationInvitationStatus.PENDING.value
    assert invitation.created_at is not None and invitation.updated_at is not None
    assert invitation.consumed_at is invitation.revoked_at is invitation.expired_at is None
    assert invitation.purged_at is None
    assert request.status == RegistrationRequestStatus.PENDING.value
    assert request.last_seen_at is not None
    assert request.created_at is not None and request.updated_at is not None
    assert request.reviewer_user_id is request.provisioned_user_id is None
    assert request.reviewed_at is request.review_note is request.expired_at is None
    assert request.purged_at is None


@pytest.mark.parametrize(
    "changes",
    [
        {"token_digest": "a" * 63},
        {"token_digest": "A" * 64},
        {"token_digest": "g" * 64},
        {"normalized_email": "   "},
        {"normalized_email": "x" * 321},
        {"account_kind": "platform_superadmin"},
        {"status": "unknown"},
        {"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)},
        {"status": "consumed"},
        {"status": "revoked"},
        {"status": "expired"},
    ],
)
async def test_invitation_hash_email_expiry_and_state_invariants(
    db_session, changes
):
    inviter = await _user(db_session, f"invalid-inviter-{abs(hash(str(changes)))}")
    db_session.add(
        RegistrationInvitation(**(_invitation_values(inviter) | changes))
    )
    # PostgreSQL rejects an over-width VARCHAR in the driver before the named
    # CHECK can run and asyncpg exposes that as DBAPIError; SQLite reaches the
    # CHECK and exposes IntegrityError.  Both are the database refusing the
    # persisted shape, which is the portable contract this test pins.
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_invitation_token_digest_is_unique(db_session):
    inviter = await _user(db_session, "unique-inviter")
    first = RegistrationInvitation(**_invitation_values(inviter, suffix="a"))
    db_session.add(first)
    await db_session.flush()

    db_session.add(
        RegistrationInvitation(
            **(
                _invitation_values(inviter, suffix="b")
                | {"token_digest": first.token_digest}
            )
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_two_pending_invitations_cannot_target_the_same_email_even_for_different_kinds(
    db_session,
):
    inviter = await _user(db_session, "live-email-inviter")
    first_values = _invitation_values(inviter, suffix="first-live")
    db_session.add(RegistrationInvitation(**first_values))
    await db_session.flush()

    db_session.add(
        RegistrationInvitation(
            **(
                _invitation_values(inviter, suffix="second-live")
                | {
                    "normalized_email": first_values["normalized_email"],
                    "account_kind": RegistrationAccountKind.DOCTOR.value,
                }
            )
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize("status", list(RegistrationInvitationStatus)[1:])
async def test_each_invitation_terminal_shape_is_allowed(db_session, status):
    inviter = await _user(db_session, f"terminal-inviter-{status.value}")
    actor = await _user(db_session, f"terminal-actor-{status.value}")
    now = _now() + timedelta(minutes=1)
    values = _invitation_values(inviter, suffix=status.value) | {"status": status.value}
    if status is RegistrationInvitationStatus.CONSUMED:
        values |= {"consumed_by_user_id": actor.id, "consumed_at": now}
    elif status is RegistrationInvitationStatus.REVOKED:
        values |= {"revoked_by_user_id": actor.id, "revoked_at": now}
    else:
        values |= {"expired_at": values["expires_at"] + timedelta(seconds=1)}
    invitation = RegistrationInvitation(**values)
    db_session.add(invitation)
    await db_session.flush()
    assert invitation.status == status.value


@pytest.mark.parametrize("status", list(RegistrationInvitationStatus)[1:])
async def test_purged_invitation_terminals_keep_outcome_time_but_scrub_identity(
    db_session, status
):
    now = _now() + timedelta(minutes=1)
    values = _invitation_values(None, suffix=f"purged-{status.value}") | {
        "token_digest": None,
        "normalized_email": None,
        "status": status.value,
    }
    if status is RegistrationInvitationStatus.CONSUMED:
        values["consumed_at"] = now
    elif status is RegistrationInvitationStatus.REVOKED:
        values["revoked_at"] = now
    else:
        now = values["expires_at"] + timedelta(seconds=1)
        values["expired_at"] = now
    values["purged_at"] = now
    invitation = RegistrationInvitation(**values)
    db_session.add(invitation)
    await db_session.flush()
    assert invitation.token_digest is invitation.normalized_email is None
    assert invitation.invited_by_user_id is None
    assert invitation.consumed_by_user_id is invitation.revoked_by_user_id is None
    assert invitation.purged_at is not None
    if status is RegistrationInvitationStatus.CONSUMED:
        assert invitation.consumed_at is not None
    elif status is RegistrationInvitationStatus.REVOKED:
        assert invitation.revoked_at is not None
    else:
        assert invitation.expired_at is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "consumed", "consumed_at": _now()},
        {"status": "revoked", "revoked_at": _now()},
        {"status": "expired", "expired_at": _now(), "revoked_at": _now()},
    ],
)
async def test_unpurged_invitation_terminals_require_exact_actor_shape(
    db_session, changes
):
    inviter = await _user(db_session, f"bad-terminal-{abs(hash(str(changes)))}")
    db_session.add(
        RegistrationInvitation(**(_invitation_values(inviter) | changes))
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize("retained_field", ["token", "inviter", "actor"])
async def test_invitation_purge_cannot_retain_identity_material(
    db_session, retained_field
):
    inviter = await _user(db_session, f"purge-inviter-{retained_field}")
    actor = await _user(db_session, f"purge-actor-{retained_field}")
    now = _now()
    values = _invitation_values(None, suffix=f"purge-{retained_field}") | {
        "token_digest": None,
        "normalized_email": None,
        "status": RegistrationInvitationStatus.CONSUMED.value,
        "consumed_at": now,
        "purged_at": now,
    }
    if retained_field == "token":
        values["token_digest"] = "f" * 64
    elif retained_field == "inviter":
        values["invited_by_user_id"] = inviter.id
    else:
        values["consumed_by_user_id"] = actor.id
    db_session.add(RegistrationInvitation(**values))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "changes",
    [
        {"issuer": "   "},
        {"subject": "   "},
        {"verified_email": None},
        {"normalized_verified_email": None},
        {"verified_email": "x" * 321, "normalized_verified_email": "x" * 321},
        {"preferred_username": "   "},
        {"preferred_username": "x" * 129},
        {"account_kind": "platform_superadmin"},
        {"status": "unknown"},
        {"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)},
        {"status": "approved"},
        {"status": "rejected"},
        {"status": "expired"},
    ],
)
async def test_request_identity_email_expiry_and_state_invariants(db_session, changes):
    db_session.add(RegistrationRequest(**(_request_values() | changes)))
    # See the invitation matrix above: asyncpg reports VARCHAR truncation as a
    # DBAPIError while SQLite reports the corresponding CHECK failure as an
    # IntegrityError.
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_live_request_issuer_and_subject_pair_is_unique(db_session):
    db_session.add(RegistrationRequest(**_request_values(suffix="unique")))
    await db_session.flush()
    db_session.add(
        RegistrationRequest(
            **(
                _request_values(suffix="second")
                | {
                    "issuer": "https://idp.example.test",
                    "subject": "subject-unique",
                }
            )
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_terminal_request_history_does_not_block_a_new_pending_attempt(
    db_session,
):
    reviewer = await _user(db_session, "request-history-reviewer")
    identity = {
        "issuer": "https://idp.example.test",
        "subject": "subject-returning-applicant",
    }
    rejected = _request_values(suffix="rejected-attempt") | identity | {
        "status": RegistrationRequestStatus.REJECTED.value,
        "reviewer_user_id": reviewer.id,
        "reviewed_at": _now(),
        "review_note": "Synthetic rejection",
    }
    db_session.add(RegistrationRequest(**rejected))
    await db_session.flush()

    pending = _request_values(suffix="new-attempt") | identity
    db_session.add(RegistrationRequest(**pending))
    await db_session.flush()


@pytest.mark.parametrize("status", list(RegistrationRequestStatus)[1:])
async def test_each_request_terminal_shape_is_allowed(db_session, status):
    reviewer = await _user(db_session, f"reviewer-{status.value}")
    provisioned = await _user(db_session, f"provisioned-{status.value}")
    now = _now()
    values = _request_values(suffix=status.value) | {"status": status.value}
    if status is RegistrationRequestStatus.APPROVED:
        values |= {
            "reviewer_user_id": reviewer.id,
            "reviewed_at": now,
            "provisioned_user_id": provisioned.id,
        }
    elif status is RegistrationRequestStatus.REJECTED:
        values |= {
            "reviewer_user_id": reviewer.id,
            "reviewed_at": now,
            "review_note": "Insufficient identity evidence",
        }
    else:
        values["expired_at"] = values["expires_at"] + timedelta(seconds=1)
    request = RegistrationRequest(**values)
    db_session.add(request)
    await db_session.flush()
    assert request.status == status.value


@pytest.mark.parametrize("status", list(RegistrationRequestStatus)[1:])
async def test_purged_request_terminals_keep_outcome_time_but_scrub_pii_and_users(
    db_session, status
):
    now = _now() + timedelta(minutes=1)
    values = _request_values(suffix=f"purged-{status.value}") | {
        "issuer": None,
        "subject": None,
        "verified_email": None,
        "normalized_verified_email": None,
        "preferred_username": None,
        "status": status.value,
    }
    if status in {
        RegistrationRequestStatus.APPROVED,
        RegistrationRequestStatus.REJECTED,
    }:
        values["reviewed_at"] = now
    else:
        now = values["expires_at"] + timedelta(seconds=1)
        values["expired_at"] = now
    values["purged_at"] = now
    request = RegistrationRequest(**values)
    db_session.add(request)
    await db_session.flush()
    assert request.issuer is request.subject is None
    assert request.verified_email is request.normalized_verified_email is None
    assert request.preferred_username is request.review_note is None
    assert request.reviewer_user_id is request.provisioned_user_id is None
    assert request.purged_at is not None
    if status in {
        RegistrationRequestStatus.APPROVED,
        RegistrationRequestStatus.REJECTED,
    }:
        assert request.reviewed_at is not None
    else:
        assert request.expired_at is not None


@pytest.mark.parametrize(
    "changes",
    [
        {
            "status": "approved",
            "reviewed_at": _now(),
            "review_note": "approval cannot carry a rejection reason",
        },
        {"status": "rejected", "reviewed_at": _now(), "review_note": "   "},
        {"status": "rejected", "reviewed_at": _now(), "review_note": "x" * 2001},
        {"status": "expired", "expired_at": _now(), "reviewed_at": _now()},
    ],
)
async def test_unpurged_request_terminals_require_exact_decision_shape(
    db_session, changes
):
    reviewer = await _user(db_session, f"bad-reviewer-{abs(hash(str(changes)))}")
    provisioned = await _user(
        db_session, f"bad-provisioned-{abs(hash(str(changes)))}"
    )
    if changes["status"] == "approved":
        changes = changes | {
            "reviewer_user_id": reviewer.id,
            "provisioned_user_id": provisioned.id,
        }
    elif changes["status"] == "rejected":
        changes = changes | {"reviewer_user_id": reviewer.id}
    db_session.add(RegistrationRequest(**(_request_values() | changes)))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize("retained_field", ["issuer", "email", "reviewer", "user"])
async def test_request_purge_cannot_retain_pii_or_user_links(db_session, retained_field):
    reviewer = await _user(db_session, f"purge-reviewer-{retained_field}")
    provisioned = await _user(db_session, f"purge-user-{retained_field}")
    now = _now()
    values = _request_values(suffix=f"purge-{retained_field}") | {
        "issuer": None,
        "subject": None,
        "verified_email": None,
        "normalized_verified_email": None,
        "preferred_username": None,
        "status": RegistrationRequestStatus.APPROVED.value,
        "reviewed_at": now,
        "purged_at": now,
    }
    if retained_field == "issuer":
        values |= {"issuer": "https://idp.example.test", "subject": "retained"}
    elif retained_field == "email":
        values |= {
            "verified_email": "retained@example.test",
            "normalized_verified_email": "retained@example.test",
        }
    elif retained_field == "reviewer":
        values["reviewer_user_id"] = reviewer.id
    else:
        values["provisioned_user_id"] = provisioned.id
    db_session.add(RegistrationRequest(**values))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_pending_request_cannot_be_purged(db_session):
    now = _now()
    db_session.add(
        RegistrationRequest(
            **(
                _request_values(suffix="pending-purge")
                | {
                    "issuer": None,
                    "subject": None,
                    "verified_email": None,
                    "normalized_verified_email": None,
                    "preferred_username": None,
                    "purged_at": now,
                }
            )
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_pending_invitation_cannot_be_purged(db_session):
    now = _now()
    db_session.add(
        RegistrationInvitation(
            **(
                _invitation_values(None, suffix="pending-purge")
                | {
                    "token_digest": None,
                    "normalized_email": None,
                    "purged_at": now,
                }
            )
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_sqlite_foreign_keys_reject_unknown_admission_users():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        User.__table__,
                        RegistrationInvitation.__table__,
                        RegistrationRequest.__table__,
                    ],
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                RegistrationInvitation(
                    **_invitation_values(None, suffix="unknown-inviter")
                    | {"invited_by_user_id": uuid.uuid4()}
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
    finally:
        await engine.dispose()
