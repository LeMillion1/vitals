"""A signed cookie is not the same thing as a live session.

The signature proves the cookie was issued here and has not been altered. It
proves nothing about whether the account still exists, is still active, or has
had every session revoked since — a cookie signed last month verifies exactly as
well as one signed a minute ago. These tests pin the checks that close that gap,
and the property that makes them worth having: after a revocation, a cookie that
still verifies stops working.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from vitals.enums import UserStatus
from vitals.models.identity import User
from vitals.services.session_service import (
    LiveSession,
    SessionRejected,
    confirm_session,
    revoke_all_sessions,
)


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


async def test_a_current_session_is_confirmed(db_session):
    user = await _user(db_session, "owner")
    live = await confirm_session(
        db_session, user_id=user.id, session_version=user.session_version
    )
    assert (live.user_id, live.username) == (user.id, "owner")


async def test_revoking_invalidates_a_cookie_that_still_verifies(db_session):
    """The point of the whole mechanism.

    Nothing about the cookie changes — it is still correctly signed and still
    within its TTL. What changes is the account it names, and that is enough.
    """

    user = await _user(db_session, "owner")
    issued_version = user.session_version

    await confirm_session(
        db_session, user_id=user.id, session_version=issued_version
    )

    new_version = await revoke_all_sessions(db_session, user_id=user.id)
    assert new_version == issued_version + 1

    with pytest.raises(SessionRejected, match="revoked"):
        await confirm_session(
            db_session, user_id=user.id, session_version=issued_version
        )


async def test_a_session_issued_after_the_revocation_still_works(db_session):
    """Revocation ends the sessions that exist, not the ability to log in again."""

    user = await _user(db_session, "owner")
    new_version = await revoke_all_sessions(db_session, user_id=user.id)
    live = await confirm_session(
        db_session, user_id=user.id, session_version=new_version
    )
    assert live.session_version == new_version


async def test_a_suspended_account_cannot_continue_an_existing_session(db_session):
    """Suspension has to take effect now, not when the cookie expires."""

    user = await _user(db_session, "owner")
    version = user.session_version
    user.status = UserStatus.SUSPENDED.value
    await db_session.flush()

    with pytest.raises(SessionRejected):
        await confirm_session(db_session, user_id=user.id, session_version=version)


async def test_a_session_naming_nobody_is_refused(db_session):
    with pytest.raises(SessionRejected):
        await confirm_session(
            db_session, user_id=uuid.uuid4(), session_version=1
        )
    with pytest.raises(SessionRejected):
        await confirm_session(
            db_session, user_id="not-a-uuid", session_version=1
        )


async def test_every_refusal_reads_the_same_from_outside(db_session):
    """Suspended, revoked and non-existent are one answer to whoever asks.

    Distinguishing them would let somebody holding a stale cookie learn whether
    the account exists and what state it is in.
    """

    user = await _user(db_session, "owner")
    version = user.session_version
    await revoke_all_sessions(db_session, user_id=user.id)

    raised = []
    for user_id, session_version in (
        (user.id, version),        # revoked
        (uuid.uuid4(), 1),         # no such user
    ):
        with pytest.raises(SessionRejected) as caught:
            await confirm_session(
                db_session, user_id=user_id, session_version=session_version
            )
        raised.append(caught.value)

    user.status = UserStatus.SUSPENDED.value
    await db_session.flush()
    with pytest.raises(SessionRejected) as caught:
        await confirm_session(
            db_session, user_id=user.id, session_version=user.session_version
        )
    raised.append(caught.value)

    # One exception type for all three. The differing message is for the log;
    # the caller has nothing to branch on and so cannot leak the distinction.
    assert {type(error) for error in raised} == {SessionRejected}


async def test_revoking_a_user_that_does_not_exist_is_refused(db_session):
    with pytest.raises(SessionRejected, match="no such user"):
        await revoke_all_sessions(db_session, user_id=uuid.uuid4())


# ── Freshness, which is what a step-up asks about ────────────────────────────

def test_a_recent_authentication_is_fresh():
    live = LiveSession(
        user_id=uuid.uuid4(),
        username="owner",
        session_version=1,
        authenticated_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    assert live.is_fresh(within_seconds=300)


def test_an_old_authentication_is_not_fresh():
    live = LiveSession(
        user_id=uuid.uuid4(),
        username="owner",
        session_version=1,
        authenticated_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    assert not live.is_fresh(within_seconds=300)


def test_no_recorded_authentication_is_never_fresh():
    """Absence of evidence is not evidence of a recent login."""

    live = LiveSession(
        user_id=uuid.uuid4(),
        username="owner",
        session_version=1,
        authenticated_at=None,
    )
    assert not live.is_fresh(within_seconds=300)
    assert not live.is_fresh(within_seconds=10**9)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_concurrent_revocations_do_not_lose_one(db_session):
    """Two revocations must produce two increments, not one.

    A read-modify-write would let both read the same version and write the same
    value, leaving one caller believing it had revoked something it had not.
    The increment is computed in the database for exactly that reason.
    """

    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    user = await _user(db_session, "concurrent")
    start = user.session_version
    await db_session.commit()

    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )

    async def revoke() -> int:
        async with factory() as session:
            version = await revoke_all_sessions(session, user_id=user.id)
            await session.commit()
            return version

    first, second = await asyncio.gather(revoke(), revoke())
    assert {first, second} == {start + 1, start + 2}
