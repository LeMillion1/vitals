"""Whether a browser session is still one this installation honours.

A signed cookie proves it was issued here and has not been altered. It does not
prove the account still exists, is still active, or has not had every session
revoked since — a cookie signed last month is as valid as one signed a minute
ago, and that is precisely the property you do not want after a suspension or a
stolen laptop.

``session_version`` is the revocation lever. It rides in the cookie and lives on
the user; bumping the row invalidates every session ever issued for that person,
without a server-side session store to grow, expire and go stale. The cost is
one indexed read per request, which is the same read the request was going to
make anyway.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserStatus
from vitals.models.identity import User


class SessionRejected(RuntimeError):
    """This session may not continue. The reason is for the log, not the browser."""


@dataclass(frozen=True, slots=True)
class LiveSession:
    """A session confirmed against the database on this request."""

    user_id: uuid.UUID
    username: str
    session_version: int
    authenticated_at: datetime | None

    def is_fresh(self, *, within_seconds: int) -> bool:
        """Whether the provider authenticated recently enough for a step-up.

        A session with no recorded authentication time is never fresh. That is
        the safe reading: absence of evidence is not evidence that somebody
        proved who they were in the last five minutes.
        """

        if self.authenticated_at is None:
            return False
        age = datetime.now(timezone.utc) - self.authenticated_at
        return age.total_seconds() <= within_seconds


async def confirm_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_version: int,
    authenticated_at: datetime | None = None,
) -> LiveSession:
    """Confirm a decoded cookie against the account it names.

    Raises rather than returning ``None`` so a caller cannot forget to check.
    Every refusal reads the same from outside — the browser is simply not
    authenticated — because distinguishing "suspended" from "revoked" from "no
    such user" tells whoever is holding the cookie something about the account.
    """

    if not isinstance(user_id, uuid.UUID):
        raise SessionRejected("session names no usable user")

    row = (
        await session.execute(
            select(User.id, User.username, User.status, User.session_version).where(
                User.id == user_id
            )
        )
    ).one_or_none()
    if row is None:
        raise SessionRejected("session names a user that no longer exists")

    resolved_id, username, status, current_version = row
    if status != UserStatus.ACTIVE.value:
        raise SessionRejected("session belongs to an account that is not active")
    if current_version != session_version:
        raise SessionRejected("session was revoked")

    return LiveSession(
        user_id=resolved_id,
        username=username,
        session_version=current_version,
        authenticated_at=authenticated_at,
    )


async def revoke_all_sessions(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Invalidate every session this user holds anywhere, and return the new version.

    One statement, computed in the database, so two concurrent revocations
    cannot read the same version and both write the same increment — which
    would leave one of them believing it had revoked something it had not.
    """

    if not isinstance(user_id, uuid.UUID):
        raise SessionRejected("user_id must be a UUID")
    new_version = await session.scalar(
        update(User)
        .where(User.id == user_id)
        .values(session_version=User.session_version + 1)
        .returning(User.session_version)
    )
    if new_version is None:
        raise SessionRejected("no such user to revoke")
    await session.flush()
    return int(new_version)


__all__ = [
    "LiveSession",
    "SessionRejected",
    "confirm_session",
    "revoke_all_sessions",
]
