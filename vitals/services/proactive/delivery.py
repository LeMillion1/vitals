"""The gate every outgoing message passes through: may this be sent, and was it.

Five rules, in the order they're checked:

1. **No channel** → nothing happens (the app before the bot exists).
2. **Module off** → nothing happens either: switching ``signals`` off in Settings
   is the emergency switch, and it has to silence the bot without a deploy.
3. **Dedupe.** A ``dedupe_key`` that's already in the journal means this exact
   message went out; a re-run of the job is a no-op, not a second ping.
4. **Quiet hours** hold back *nudges* — the bot's own idea of a good moment. The
   brief and the evening block go out at a time the owner typed by hand into the
   same settings card, so silencing them by quiet hours is one field quietly
   cancelling another with no way to see which won.
5. **The daily budget** (also from the settings card) covers all three
   self-initiated categories — the brief, the evening block, nudges.

   Answers to the owner (``reply``, ``echo``) are deliberately exempt. Counting
   them would mean that after the fourth thing you logged, the bot stops replying
   to you — which reads as a broken bot, not as a budget. This is the single
   easiest rule in the whole feature to get wrong, so it lives in one ``frozenset``
   right here rather than at each call site.

A send that fails at the transport is logged and swallowed: the caller's DB work
(the signals it just parsed, the digest it just stored) must not be rolled back
because Telegram had a bad minute, and an un-sent message writes no journal row,
so it costs nothing from the budget either.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date as date_type, datetime, time as time_type
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.proactive import Notification
from vitals.models.tenancy import IntegrationConnection
from vitals.services.proactive import prefs
from vitals.services.proactive.channels import Buttons, Notifier
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

# Categories. Only the first three are the bot talking first.
CATEGORY_BRIEF = "brief"
CATEGORY_EVENING = "evening"
CATEGORY_NUDGE = "nudge"
CATEGORY_REPLY = "reply"
CATEGORY_ECHO = "echo"
# A send the owner asked for from the web ("Отправить тестовое"): it exists to
# catch broken formatting, so it must go out even when today's brief already did,
# and it is not the bot talking first — hence off-budget and outside quiet hours.
CATEGORY_TEST = "test"

INITIATIVE_CATEGORIES = frozenset({CATEGORY_BRIEF, CATEGORY_EVENING, CATEGORY_NUDGE})
HISTORICAL_RECIPIENT_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }
)

# Fallbacks only — the live values come from ``prefs`` (the settings card), which
# is why they are read per send rather than captured at import.
DAILY_BUDGET = prefs.DEFAULTS["daily_budget"]
QUIET_START = prefs.as_time(prefs.DEFAULTS["quiet_start"])
QUIET_END = prefs.as_time(prefs.DEFAULTS["quiet_end"])


def _validate_ownership(
    ownership: ProactiveOwnershipContext | None,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    if ownership is not None and not isinstance(
        ownership, ProactiveOwnershipContext
    ):
        raise TypeError("ownership must be a ProactiveOwnershipContext or None")
    if actor_user_id is not None and not isinstance(actor_user_id, uuid.UUID):
        raise TypeError("actor_user_id must be a UUID or None")
    if ownership is None and actor_user_id is not None:
        raise ValueError("actor_user_id requires explicit proactive ownership")
    if (
        ownership is not None
        and actor_user_id is not None
        and actor_user_id != ownership.recipient_user_id
    ):
        raise ValueError("proactive delivery actor must be the recipient user")


def notification_ownership_scope(
    ownership: ProactiveOwnershipContext | None,
    *,
    connection_scoped: bool = True,
):
    if ownership is None:
        return None
    connection_filters = [
        IntegrationConnection.id == Notification.integration_connection_id,
        IntegrationConnection.subject_id == ownership.subject_id,
        IntegrationConnection.connection_type
        == IntegrationConnectionType.RECIPIENT.value,
        IntegrationConnection.status.in_(HISTORICAL_RECIPIENT_STATUSES),
        IntegrationConnection.provider == Notification.channel,
    ]
    if connection_scoped:
        connection_filters.append(
            IntegrationConnection.id == ownership.connection_id
        )
    valid_connection = (
        select(IntegrationConnection.id)
        .where(*connection_filters)
        .correlate(Notification)
        .exists()
    )
    owned = and_(
        Notification.subject_id == ownership.subject_id,
        Notification.recipient_user_id == ownership.recipient_user_id,
        or_(
            Notification.actor_user_id.is_(None),
            Notification.actor_user_id == ownership.recipient_user_id,
        ),
        valid_connection,
    )
    if ownership.include_legacy_unowned:
        owned = or_(
            owned,
            and_(
                Notification.subject_id.is_(None),
                Notification.actor_user_id.is_(None),
                Notification.recipient_user_id.is_(None),
                Notification.integration_connection_id.is_(None),
            ),
        )
    return owned


def _scoped_notification_query(
    query,
    ownership: ProactiveOwnershipContext | None,
    *,
    connection_scoped: bool = True,
):
    scope = notification_ownership_scope(
        ownership,
        connection_scoped=connection_scoped,
    )
    if scope is None:
        return query
    return query.where(scope)


class NotificationOwnershipConflictError(RuntimeError):
    """A global legacy dedupe key is already owned by another scope."""


class ProactiveOwnershipScopeError(ValueError):
    """A delivery context does not resolve to the legacy owner/channel graph."""


async def _require_ownership_scope(
    session: AsyncSession,
    ownership: ProactiveOwnershipContext,
    *,
    channel: str,
) -> None:
    """Revalidate the legacy recipient and channel before network delivery.

    Owner-as-recipient is a Stage-2 compatibility invariant.  A future care-team
    delivery model must replace it with an explicit recipient/access binding.
    Historical reads may retain inactive provenance, but a live delivery is
    allowed only through a legacy-compatible or active recipient connection.
    """

    subject = (
        await session.execute(
            select(HealthSubject.owner_user_id, User.status)
            .join(User, User.id == HealthSubject.owner_user_id)
            .where(HealthSubject.id == ownership.subject_id)
        )
    ).one_or_none()
    if subject is None:
        raise ProactiveOwnershipScopeError(
            "proactive delivery subject or owner does not exist"
        )
    owner_user_id, owner_status = subject
    if owner_user_id != ownership.recipient_user_id:
        raise ProactiveOwnershipScopeError(
            "proactive recipient is not the legacy subject owner"
        )
    if owner_status != UserStatus.ACTIVE.value:
        raise ProactiveOwnershipScopeError(
            "proactive recipient identity is not active"
        )

    if not isinstance(channel, str) or not channel.strip():
        raise ProactiveOwnershipScopeError("proactive delivery channel is invalid")

    connection = (
        await session.execute(
            select(
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
            ).where(
                IntegrationConnection.id == ownership.connection_id,
                IntegrationConnection.subject_id == ownership.subject_id,
            )
        )
    ).one_or_none()
    if connection is None:
        raise ProactiveOwnershipScopeError(
            "proactive delivery connection does not match the subject"
        )
    provider, connection_type, status = connection
    if provider != channel:
        raise ProactiveOwnershipScopeError(
            "proactive notifier channel does not match its connection provider"
        )
    if connection_type != IntegrationConnectionType.RECIPIENT.value:
        raise ProactiveOwnershipScopeError(
            "proactive delivery connection is not a recipient binding"
        )
    known_statuses = {item.value for item in IntegrationConnectionStatus}
    if status not in known_statuses:
        raise ProactiveOwnershipScopeError(
            "proactive delivery connection has unknown lifecycle state"
        )
    if status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }:
        raise ProactiveOwnershipScopeError(
            "inactive proactive delivery connection cannot send"
        )


def in_quiet_hours(
    at: time_type, *, start: time_type = QUIET_START, end: time_type = QUIET_END
) -> bool:
    """Is ``at`` inside the quiet window? Handles a window that wraps midnight,
    because the settings card lets the owner set exactly that."""
    if start == end:
        return False
    if start < end:
        return start <= at < end
    return at >= start or at < end


async def sent_today(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    ownership: ProactiveOwnershipContext | None = None,
) -> int:
    """How much of today's budget is spent (self-initiated messages only)."""
    on_date = on_date or now_local().date()
    _validate_ownership(ownership)
    query = (
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.category.in_(INITIATIVE_CATEGORIES),
            func.date(Notification.sent_at) == on_date,
        )
    )
    result = await session.execute(
        _scoped_notification_query(
            query,
            ownership,
            connection_scoped=False,
        )
    )
    return result.scalar() or 0


async def find_sent(
    session: AsyncSession,
    external_id: str,
    *,
    ownership: ProactiveOwnershipContext | None = None,
) -> Optional[Notification]:
    """The journal row for a message id — how an incoming reply finds the context
    it is replying to."""
    _validate_ownership(ownership)
    query = _scoped_notification_query(
        select(Notification).where(
            Notification.external_id == str(external_id)
        ),
        ownership,
        connection_scoped=False,
    )
    result = await session.execute(query.order_by(Notification.id.desc()))
    return result.scalars().first()


async def recent_sent(
    session: AsyncSession,
    *,
    limit: int = 3,
    ownership: ProactiveOwnershipContext | None = None,
) -> list[Notification]:
    """The last few messages we sent, oldest first — the context a question typed
    without Telegram's Reply has to be read against.

    Almost nothing is typed as a reply on mobile, so «что за ключ странный на
    второе» arrived with no message attached and was answered against the morning
    brief's JSON: the bot could not see the echo it had sent a minute earlier and
    guessed the owner meant the 2nd of the month.
    """
    _validate_ownership(ownership)
    query = _scoped_notification_query(
        select(Notification),
        ownership,
        connection_scoped=False,
    )
    result = await session.execute(
        query.order_by(Notification.id.desc()).limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def already_sent(
    session: AsyncSession,
    dedupe_key: str,
    *,
    ownership: ProactiveOwnershipContext | None = None,
) -> bool:
    _validate_ownership(ownership)
    query = _scoped_notification_query(
        select(Notification.id).where(Notification.dedupe_key == dedupe_key),
        ownership,
        connection_scoped=False,
    )
    result = await session.execute(query)
    return result.scalars().first() is not None


async def send(
    session: AsyncSession,
    notifier: Optional[Notifier],
    *,
    text: str,
    category: str,
    dedupe_key: Optional[str] = None,
    buttons: Optional[Buttons] = None,
    reply_to: Optional[str] = None,
    now: Optional[datetime] = None,
    ownership: ProactiveOwnershipContext | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Optional[Notification]:
    """Send if allowed, and journal what was sent. ``None`` = nothing went out."""
    if notifier is None:
        return None
    if not text.strip():
        return None
    _validate_ownership(ownership, actor_user_id=actor_user_id)
    if ownership is not None:
        await _require_ownership_scope(
            session,
            ownership,
            channel=notifier.channel,
        )
    # The emergency switch. Checked here because *every* outgoing message —
    # brief, evening block, nudge, echo, reply, the test send from /reports —
    # passes through this one function; a guard per job would leak the ones that
    # aren't jobs.
    if not await prefs.bot_enabled(
        session,
        subject_id=(ownership.subject_id if ownership is not None else None),
    ):
        logger.info("skipping %s: the signals module is switched off", category)
        return None

    if dedupe_key:
        existing = await session.scalar(
            select(Notification).where(Notification.dedupe_key == dedupe_key)
        )
        if existing is not None:
            if ownership is None:
                logger.info("skipping %s: already sent (%s)", category, dedupe_key)
                return None
            valid_existing = await session.scalar(
                select(Notification.id).where(
                    Notification.id == existing.id,
                    notification_ownership_scope(
                        ownership,
                        connection_scoped=False,
                    ),
                )
            )
            if valid_existing is not None:
                logger.info("skipping %s: already sent (%s)", category, dedupe_key)
                return None
            # A global key with invalid/foreign roots is a conflict, not evidence
            # that this recipient was already notified. Fail before the network.
            raise NotificationOwnershipConflictError(
                "notification dedupe key belongs to another ownership scope"
            )

    now = now or now_local()
    if category in INITIATIVE_CATEGORIES:
        settings = await prefs.get_prefs(session)
        budget = settings["daily_budget"]
        # Nudges only: a brief scheduled for 09:00 inside a 02:00-10:00 quiet
        # window must still arrive. Both times came from the same card, and the
        # one he set for the brief is the more specific instruction.
        if category == CATEGORY_NUDGE and in_quiet_hours(
            now.time(),
            start=prefs.as_time(settings["quiet_start"]),
            end=prefs.as_time(settings["quiet_end"]),
        ):
            logger.info("skipping %s: quiet hours (%s)", category, now.time())
            return None
        if await sent_today(
            session,
            on_date=now.date(),
            ownership=ownership,
        ) >= budget:
            logger.info("skipping %s: daily budget of %s used", category, budget)
            return None

    try:
        external_id = await notifier.send(text, buttons=buttons, reply_to=reply_to)
    except Exception:
        logger.warning("delivery failed for %s; message dropped", category, exc_info=True)
        return None

    row = Notification(
        subject_id=ownership.subject_id if ownership is not None else None,
        actor_user_id=actor_user_id,
        recipient_user_id=(
            ownership.recipient_user_id if ownership is not None else None
        ),
        integration_connection_id=(
            ownership.connection_id if ownership is not None else None
        ),
        sent_at=now,
        category=category,
        dedupe_key=dedupe_key,
        channel=notifier.channel,
        external_id=external_id or None,
        payload={"text": text, "buttons": [list(b) for b in buttons] if buttons else None},
    )
    session.add(row)
    await session.flush()
    return row
