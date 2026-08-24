"""Bearer credentials for the read-only external API, one record each.

The endpoint they open used to be authorized by ``VITALS_EXTERNAL_API_TOKEN``:
one string for the whole installation, resolving its subject from whoever the
``.env`` file named as the owner. On a single-user machine that is a per-subject
token by accident. With a second person in the database it is a credential with
no boundary — its holder reads a record nobody granted them, and nothing about
the token says whose data came back. It was the last ``.env``-owner read left on
a data path.

Three rules hold the replacement together:

**The secret exists once.** It is returned by :func:`issue` and never stored —
only its SHA-256 is. There is no screen, and no query, that can show it again,
which is deliberate: a credential an operator can read back out of the database
is a credential an operator can use.

**Only the record's owner may mint one.** Not a professional in care, not a
platform administrator. Handing out a long-lived key to somebody's health data
is not something a support grant should be able to do quietly, and a doctor's
consent is to read within the app rather than to issue credentials.

**Authentication is a fresh question every time.** :func:`authenticate` re-reads
status, expiry and the owner's account state on each call, so revoking a token
or suspending an account takes effect on the next request rather than eventually.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import ExternalApiTokenStatus, UserStatus
from vitals.models.identity import ExternalApiToken, HealthSubject, User

#: 32 bytes of urlsafe randomness. Long enough that guessing is not a strategy
#: and short enough to paste into another app's configuration by hand.
TOKEN_BYTES = 32

DEFAULT_LIFETIME = timedelta(days=90)
MAX_LIFETIME = timedelta(days=365)

#: How many live credentials one record may have at once. Not a security
#: boundary — it is a legibility one. A list of thirty indistinguishable secrets
#: is a list nobody revokes from with any confidence.
MAX_LIVE_TOKENS = 10


class ExternalApiTokenError(RuntimeError):
    """Base for every refusal this module makes."""


class NotTheSubjectOwner(ExternalApiTokenError):
    """Only the person whose record it is may issue or revoke a credential."""


class TooManyTokens(ExternalApiTokenError):
    """This record already holds as many live credentials as it may."""


class TokenNotFound(ExternalApiTokenError):
    """No such credential, or not one this actor may act on."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """The one moment the secret exists outside the holder's configuration."""

    record: ExternalApiToken
    #: Shown once and never again. Not stored anywhere, including here after the
    #: caller lets go of it.
    secret: str


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


async def _now(session: AsyncSession) -> datetime:
    """The wall clock, from the database, advancing inside a transaction.

    ``now()`` is the transaction's start time in PostgreSQL, and this table's
    expiry is compared against its creation by a check constraint — two reads of
    "now" that disagree would make a legitimate ninety-day token look impossible.
    """

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stamp = await session.scalar(select(func.clock_timestamp()))
        if stamp is not None:
            return (
                stamp
                if stamp.tzinfo is not None
                else stamp.replace(tzinfo=timezone.utc)
            )
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _require_owner(
    session: AsyncSession, *, user_id: uuid.UUID, subject_id: uuid.UUID
) -> None:
    with session.no_autoflush:
        owner = await session.scalar(
            select(HealthSubject.owner_user_id).where(HealthSubject.id == subject_id)
        )
    if owner is None or owner != user_id:
        raise NotTheSubjectOwner(
            "only the person whose record it is may issue a credential for it"
        )


async def issue(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    label: str,
    lifetime: timedelta = DEFAULT_LIFETIME,
) -> IssuedToken:
    """Mint one credential and hand back the only copy of its secret.

    Never commits. The secret is returned rather than stored: this is the one
    moment it exists outside whatever configuration the holder pastes it into.
    """

    cleaned = (label or "").strip()
    if not cleaned:
        raise ExternalApiTokenError(
            "a credential needs a label: a list of indistinguishable secrets is "
            "one nobody can revoke from with any confidence"
        )
    if len(cleaned) > 120:
        raise ExternalApiTokenError("a label is at most 120 characters")
    if lifetime <= timedelta(0) or lifetime > MAX_LIFETIME:
        raise ExternalApiTokenError(
            f"a credential lasts between one second and {MAX_LIFETIME}"
        )

    await _require_owner(session, user_id=owner_user_id, subject_id=subject_id)
    now = await _now(session)

    live = await session.scalar(
        select(func.count())
        .select_from(ExternalApiToken)
        .where(
            ExternalApiToken.subject_id == subject_id,
            ExternalApiToken.status == ExternalApiTokenStatus.ACTIVE.value,
            ExternalApiToken.expires_at > now,
        )
    )
    if (live or 0) >= MAX_LIVE_TOKENS:
        raise TooManyTokens(
            f"this record already holds {MAX_LIVE_TOKENS} live credentials; "
            "revoke one before issuing another"
        )

    secret = secrets.token_urlsafe(TOKEN_BYTES)
    record = ExternalApiToken(
        subject_id=subject_id,
        issued_by_user_id=owner_user_id,
        label=cleaned,
        token_hash=_hash(secret),
        status=ExternalApiTokenStatus.ACTIVE.value,
        created_at=now,
        expires_at=now + lifetime,
    )
    session.add(record)
    await session.flush()
    return IssuedToken(record=record, secret=secret)


async def revoke(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    token_id: uuid.UUID,
) -> ExternalApiToken:
    """Stop one credential now. Never commits.

    Marked rather than deleted: "this dashboard could read my weight until
    March" is part of a record of who saw what, and a table that forgets its
    revocations cannot answer it.
    """

    record = await session.scalar(
        select(ExternalApiToken)
        .where(ExternalApiToken.id == token_id)
        .with_for_update()
    )
    if record is None:
        raise TokenNotFound("no such credential")
    try:
        await _require_owner(
            session, user_id=owner_user_id, subject_id=record.subject_id
        )
    except NotTheSubjectOwner:
        # Not "you may not touch this": somebody probing ids learns nothing
        # about whether one exists.
        raise TokenNotFound("no such credential") from None
    if record.status != ExternalApiTokenStatus.ACTIVE.value:
        raise ExternalApiTokenError("this credential is already revoked")

    record.status = ExternalApiTokenStatus.REVOKED.value
    record.revoked_at = await _now(session)
    await session.flush()
    return record


async def list_for_subject(
    session: AsyncSession, *, subject_id: uuid.UUID
) -> Sequence[ExternalApiToken]:
    """Every credential ever issued for this record, newest first.

    Revoked and lapsed included: the question this list answers is "what can
    read my data, and what could", and only the first half is not an answer.
    """

    result = await session.execute(
        select(ExternalApiToken)
        .where(ExternalApiToken.subject_id == subject_id)
        .order_by(ExternalApiToken.created_at.desc())
    )
    return list(result.scalars().all())


async def authenticate(
    session: AsyncSession, *, presented: str
) -> ExternalApiToken | None:
    """Whose record this bearer token opens, or ``None``.

    Asked fresh on every request, and on more than the token: an account that
    was suspended five minutes ago must stop authorizing immediately, and a
    credential outliving its owner's access is the shape of exactly the bug this
    module replaces.

    Looked up by hash, so a token nobody presented correctly is a row nobody
    matched — there is no string comparison against a stored secret here because
    there is no stored secret.
    """

    if not presented:
        return None
    now = await _now(session)
    record = await session.scalar(
        select(ExternalApiToken)
        .join(HealthSubject, HealthSubject.id == ExternalApiToken.subject_id)
        .join(User, User.id == HealthSubject.owner_user_id)
        .where(
            ExternalApiToken.token_hash == _hash(presented),
            ExternalApiToken.status == ExternalApiTokenStatus.ACTIVE.value,
            ExternalApiToken.expires_at > now,
            User.status == UserStatus.ACTIVE.value,
        )
    )
    if record is None:
        return None

    # Coarse on purpose: it answers "is this one still in use" before somebody
    # revokes it. A precise access log belongs in ``audit_events``, not in a
    # column written on every read of a dashboard that polls.
    today = now.date()
    if record.last_used_on is None or _as_utc(record.last_used_on).date() != today:
        record.last_used_on = now
    return record


def is_live(record: ExternalApiToken, *, at: datetime) -> bool:
    """Whether this row still authorizes anything, for a screen to say so."""

    return (
        record.status == ExternalApiTokenStatus.ACTIVE.value
        and _as_utc(record.expires_at) > at
    )


__all__ = [
    "DEFAULT_LIFETIME",
    "ExternalApiTokenError",
    "IssuedToken",
    "MAX_LIFETIME",
    "MAX_LIVE_TOKENS",
    "NotTheSubjectOwner",
    "TokenNotFound",
    "TooManyTokens",
    "authenticate",
    "is_live",
    "issue",
    "list_for_subject",
    "revoke",
]
