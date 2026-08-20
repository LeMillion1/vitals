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
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, time as time_type
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject, User
from vitals.models.proactive import Notification
from vitals.models.raw_payload import RawPayload
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


@dataclass(frozen=True, slots=True)
class _PreparedDelivery:
    """Policy-approved message that may cross the network without a DB session."""

    text: str = field(repr=False)
    category: str
    dedupe_key: str | None
    buttons: tuple[tuple[str, str], ...] | None
    reply_to: str | None
    sent_at: datetime
    channel: str
    ownership: ProactiveOwnershipContext | None
    actor_user_id: uuid.UUID | None
    ai_invocation_id: uuid.UUID | None
    redact_journal_content: bool
    journal_raw_payload_id: int | None

    def __reduce__(self):
        raise TypeError("_PreparedDelivery is not pickleable")


@dataclass(frozen=True, slots=True)
class _DeliveredMessage:
    """Successful transport result, including channels without a message id."""

    external_id: str | None


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


async def _require_ai_invocation_scope(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext | None,
    category: str,
    ai_invocation_id: uuid.UUID | None,
) -> int | None:
    if ai_invocation_id is None:
        return None
    if not isinstance(ai_invocation_id, uuid.UUID):
        raise TypeError("ai_invocation_id must be a UUID or None")
    if ownership is None or category not in {CATEGORY_ECHO, CATEGORY_REPLY}:
        raise ProactiveOwnershipScopeError(
            "AI delivery provenance requires an owned reply or echo"
        )
    expected_purpose = (
        AIInvocationPurpose.SIGNAL_PARSE
        if category == CATEGORY_ECHO
        else AIInvocationPurpose.QUESTION_REPLY
    )
    allowed_statuses = {
        AIInvocationStatus.SUCCEEDED.value,
        AIInvocationStatus.FAILED.value,
        AIInvocationStatus.AMBIGUOUS.value,
    }
    if expected_purpose is AIInvocationPurpose.QUESTION_REPLY:
        # A reply reservation cancelled before provider I/O still owns the one
        # deterministic fallback journal for its raw question.
        allowed_statuses.add(AIInvocationStatus.CANCELLED.value)
    row = (
        await session.execute(
            select(
                AIInvocation.subject_id,
                AIInvocation.actor_user_id,
                AIInvocation.purpose,
                AIInvocation.source,
                AIInvocation.status,
                AIInvocation.raw_payload_id,
            ).where(AIInvocation.id == ai_invocation_id)
        )
    ).one_or_none()
    if row is None:
        raise ProactiveOwnershipScopeError("AI delivery invocation does not exist")
    subject_id, actor_user_id, purpose, source, status, raw_payload_id = row
    if (
        subject_id != ownership.subject_id
        or actor_user_id != ownership.recipient_user_id
        or purpose != expected_purpose.value
        or source != AIInvocationSource.TELEGRAM.value
        or status not in allowed_statuses
        or raw_payload_id is None
    ):
        raise ProactiveOwnershipScopeError("AI delivery invocation provenance is invalid")
    return raw_payload_id


async def _require_redacted_reply_raw_scope(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    invocation_raw_payload_id: int | None,
) -> None:
    """Prove the JSON journal marker points at the exact owned Telegram raw."""

    row = (
        await session.execute(
            select(
                RawPayload.subject_id,
                RawPayload.actor_user_id,
                RawPayload.file_asset_id,
                RawPayload.domain,
                RawPayload.source,
                RawPayload.processed_at,
                IntegrationConnection.subject_id,
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
            )
            .join(
                IntegrationConnection,
                IntegrationConnection.id == RawPayload.integration_connection_id,
            )
            .where(RawPayload.id == raw_payload_id)
        )
    ).one_or_none()
    if row is None:
        raise ProactiveOwnershipScopeError(
            "redacted reply raw provenance does not exist"
        )
    (
        raw_subject_id,
        raw_actor_user_id,
        raw_file_asset_id,
        raw_domain,
        raw_source,
        raw_processed_at,
        connection_subject_id,
        connection_provider,
        connection_type,
        connection_status,
    ) = row
    if (
        raw_subject_id != ownership.subject_id
        or raw_actor_user_id != ownership.recipient_user_id
        or raw_file_asset_id is not None
        or raw_domain != Domain.SIGNALS.value
        or raw_source != Source.TELEGRAM.value
        or raw_processed_at is None
        or connection_subject_id != ownership.subject_id
        or connection_provider != IntegrationProvider.TELEGRAM.value
        or connection_type != IntegrationConnectionType.RECIPIENT.value
        or connection_status not in HISTORICAL_RECIPIENT_STATUSES
        or (
            invocation_raw_payload_id is not None
            and invocation_raw_payload_id != raw_payload_id
        )
    ):
        raise ProactiveOwnershipScopeError(
            "redacted reply raw provenance is invalid"
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


async def _prepare_delivery(
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
    ai_invocation_id: uuid.UUID | None = None,
    redact_journal_content: bool = False,
    journal_raw_payload_id: int | None = None,
) -> _PreparedDelivery | None:
    """Apply delivery policy without calling the transport or mutating state.

    A scheduler can commit the caller-owned read transaction after this returns,
    call :func:`_transmit_prepared_delivery`, then journal the successful send in
    a new transaction. That is the safe seam for jobs which must never keep a
    database transaction open across a network await.
    """
    if notifier is None or not text.strip():
        return None
    if not isinstance(redact_journal_content, bool):
        raise TypeError("redact_journal_content must be a bool")
    if journal_raw_payload_id is not None and (
        isinstance(journal_raw_payload_id, bool)
        or not isinstance(journal_raw_payload_id, int)
        or journal_raw_payload_id < 1
    ):
        raise ValueError("journal_raw_payload_id must be a positive integer or None")
    if redact_journal_content:
        if (
            ownership is None
            or category != CATEGORY_REPLY
            or journal_raw_payload_id is None
        ):
            raise ProactiveOwnershipScopeError(
                "redacted delivery journals require an owned raw-backed reply"
            )
    elif journal_raw_payload_id is not None:
        raise ProactiveOwnershipScopeError(
            "journal_raw_payload_id requires redacted journal content"
        )
    _validate_ownership(ownership, actor_user_id=actor_user_id)
    if ownership is not None:
        await _require_ownership_scope(
            session,
            ownership,
            channel=notifier.channel,
        )
    invocation_raw_payload_id = await _require_ai_invocation_scope(
        session,
        ownership=ownership,
        category=category,
        ai_invocation_id=ai_invocation_id,
    )
    if redact_journal_content:
        assert ownership is not None and journal_raw_payload_id is not None
        await _require_redacted_reply_raw_scope(
            session,
            ownership=ownership,
            raw_payload_id=journal_raw_payload_id,
            invocation_raw_payload_id=invocation_raw_payload_id,
        )
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
            raise NotificationOwnershipConflictError(
                "notification dedupe key belongs to another ownership scope"
            )

    at = now or now_local()
    if category in INITIATIVE_CATEGORIES:
        settings = await prefs.get_prefs(session)
        if category == CATEGORY_NUDGE and in_quiet_hours(
            at.time(),
            start=prefs.as_time(settings["quiet_start"]),
            end=prefs.as_time(settings["quiet_end"]),
        ):
            logger.info("skipping %s: quiet hours (%s)", category, at.time())
            return None
        if await sent_today(
            session,
            on_date=at.date(),
            ownership=ownership,
        ) >= settings["daily_budget"]:
            logger.info(
                "skipping %s: daily budget of %s used",
                category,
                settings["daily_budget"],
            )
            return None

    return _PreparedDelivery(
        text=text,
        category=category,
        dedupe_key=dedupe_key,
        buttons=tuple(buttons) if buttons else None,
        reply_to=reply_to,
        sent_at=at,
        channel=notifier.channel,
        ownership=ownership,
        actor_user_id=actor_user_id,
        ai_invocation_id=ai_invocation_id,
        redact_journal_content=redact_journal_content,
        journal_raw_payload_id=journal_raw_payload_id,
    )


async def _transmit_prepared_delivery(
    notifier: Notifier,
    prepared: _PreparedDelivery,
) -> _DeliveredMessage | None:
    """Send a prepared message without accepting or touching a DB session."""
    if not isinstance(prepared, _PreparedDelivery):
        raise TypeError("prepared must be a _PreparedDelivery")
    if notifier.channel != prepared.channel:
        raise ProactiveOwnershipScopeError(
            "prepared delivery channel does not match the notifier"
        )
    try:
        return _DeliveredMessage(
            external_id=await notifier.send(
                prepared.text,
                buttons=prepared.buttons,
                reply_to=prepared.reply_to,
            )
        )
    except Exception:
        if prepared.redact_journal_content:
            # Transport exceptions can embed the outbound request body. The AI
            # answer is intentionally memory-only, so log a bounded code only.
            logger.warning(
                "delivery failed for %s; message dropped (code=transport_error)",
                prepared.category,
            )
        else:
            logger.warning(
                "delivery failed for %s; message dropped",
                prepared.category,
                exc_info=True,
            )
        return None


async def _journal_prepared_delivery(
    session: AsyncSession,
    prepared: _PreparedDelivery,
    *,
    external_id: str | None,
) -> Notification:
    """Persist a successful prepared send; flush only, caller commits."""
    if not isinstance(prepared, _PreparedDelivery):
        raise TypeError("prepared must be a _PreparedDelivery")
    _validate_ownership(
        prepared.ownership,
        actor_user_id=prepared.actor_user_id,
    )
    if prepared.ownership is not None:
        await _require_ownership_scope(
            session,
            prepared.ownership,
            channel=prepared.channel,
        )
    invocation_raw_payload_id = await _require_ai_invocation_scope(
        session,
        ownership=prepared.ownership,
        category=prepared.category,
        ai_invocation_id=prepared.ai_invocation_id,
    )
    if prepared.redact_journal_content:
        assert prepared.ownership is not None
        assert prepared.journal_raw_payload_id is not None
        await _require_redacted_reply_raw_scope(
            session,
            ownership=prepared.ownership,
            raw_payload_id=prepared.journal_raw_payload_id,
            invocation_raw_payload_id=invocation_raw_payload_id,
        )
    payload = (
        {
            "content_redacted": True,
            "raw_payload_id": prepared.journal_raw_payload_id,
        }
        if prepared.redact_journal_content
        else {
            "text": prepared.text,
            "buttons": (
                [list(button) for button in prepared.buttons]
                if prepared.buttons
                else None
            ),
        }
    )
    row = Notification(
        subject_id=(
            prepared.ownership.subject_id
            if prepared.ownership is not None
            else None
        ),
        actor_user_id=prepared.actor_user_id,
        recipient_user_id=(
            prepared.ownership.recipient_user_id
            if prepared.ownership is not None
            else None
        ),
        integration_connection_id=(
            prepared.ownership.connection_id
            if prepared.ownership is not None
            else None
        ),
        sent_at=prepared.sent_at,
        category=prepared.category,
        dedupe_key=prepared.dedupe_key,
        channel=prepared.channel,
        external_id=external_id or None,
        ai_invocation_id=prepared.ai_invocation_id,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    return row


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
    ai_invocation_id: uuid.UUID | None = None,
) -> Optional[Notification]:
    """Send if allowed, and journal what was sent. ``None`` = nothing went out."""
    prepared = await _prepare_delivery(
        session,
        notifier,
        text=text,
        category=category,
        dedupe_key=dedupe_key,
        buttons=buttons,
        reply_to=reply_to,
        now=now,
        ownership=ownership,
        actor_user_id=actor_user_id,
        ai_invocation_id=ai_invocation_id,
    )
    if prepared is None:
        return None
    assert notifier is not None
    delivered = await _transmit_prepared_delivery(notifier, prepared)
    if delivered is None:
        return None
    return await _journal_prepared_delivery(
        session,
        prepared,
        external_id=delivered.external_id,
    )
