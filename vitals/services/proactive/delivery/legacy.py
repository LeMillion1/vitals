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

import asyncio
import logging
import uuid
from datetime import (
    datetime,
)
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.proactive import Notification
from vitals.services.proactive.preferences import codec as preference_codec
from vitals.services.proactive.preferences import legacy as preference_legacy
from vitals.services.proactive.channels import (
    Buttons,
    Notifier,
)
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

from vitals.services.proactive.delivery.contracts import (
    CATEGORY_NUDGE,
    CATEGORY_REPLY,
    INITIATIVE_CATEGORIES,
    DurableDeliveryRequiredError,
)

from vitals.services.proactive.delivery.policy import (
    NotificationOwnershipConflictError,
    ProactiveOwnershipScopeError,
    _DeliveredMessage,
    _PreparedDelivery,
    _require_ai_invocation_scope,
    _require_ownership_scope,
    _require_redacted_reply_raw_scope,
    _require_zero_subject_legacy_delivery,
    _validate_ownership,
    notification_ownership_scope,
)

from vitals.services.proactive.delivery.queries import (
    in_quiet_hours,
    sent_today,
)


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
    if ownership is not None:
        raise DurableDeliveryRequiredError(
            "owned delivery must reserve a durable intent before network I/O"
        )
    if notifier is None or not text.strip():
        return None
    legacy_transaction = await _require_zero_subject_legacy_delivery(session)
    if not isinstance(redact_journal_content, bool):
        raise TypeError("redact_journal_content must be a bool")
    if journal_raw_payload_id is not None and (
        isinstance(journal_raw_payload_id, bool)
        or not isinstance(journal_raw_payload_id, int)
        or journal_raw_payload_id < 1
    ):
        raise ValueError("journal_raw_payload_id must be a positive integer or None")
    if redact_journal_content:
        if ownership is None or category != CATEGORY_REPLY or journal_raw_payload_id is None:
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
    if dedupe_key:
        existing = await session.scalar(
            select(Notification).where(Notification.dedupe_key == dedupe_key)
        )
        if existing is not None:
            if ownership is None:
                logger.info("skipping %s: already sent (code=duplicate)", category)
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
                logger.info("skipping %s: already sent (code=duplicate)", category)
                return None
            raise NotificationOwnershipConflictError(
                "notification dedupe key belongs to another ownership scope"
            )

    at = now or now_local()
    if category in INITIATIVE_CATEGORIES:
        settings = await preference_legacy.get_prefs(session)
        if category == CATEGORY_NUDGE and in_quiet_hours(
            at.time(),
            start=preference_codec.as_time(settings["quiet_start"]),
            end=preference_codec.as_time(settings["quiet_end"]),
        ):
            logger.info("skipping %s: quiet hours (%s)", category, at.time())
            return None
        if (
            await sent_today(
                session,
                on_date=at.date(),
                ownership=ownership,
            )
            >= settings["daily_budget"]
        ):
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
        _session=session,
        _transaction=legacy_transaction,
    )


async def _transmit_prepared_delivery(
    notifier: Notifier,
    prepared: _PreparedDelivery,
) -> _DeliveredMessage | None:
    """Send a prepared message without accepting or touching a DB session."""
    if not isinstance(prepared, _PreparedDelivery):
        raise TypeError("prepared must be a _PreparedDelivery")
    if (
        not prepared._session.in_transaction()
        or prepared._session.sync_session.get_transaction() is not prepared._transaction
    ):
        raise DurableDeliveryRequiredError(
            "zero-subject compatibility proof expired before network I/O"
        )
    if notifier.channel != prepared.channel:
        raise ProactiveOwnershipScopeError("prepared delivery channel does not match the notifier")
    try:
        return _DeliveredMessage(
            external_id=await notifier.send(
                prepared.text,
                buttons=prepared.buttons,
                reply_to=prepared.reply_to,
            )
        )
    except (asyncio.CancelledError, Exception):
        # Exception strings/tracebacks can contain Telegram's token-bearing URL
        # or outbound PHI.  Even the temporary zero-subject bridge logs only an
        # allowlisted bounded code.
        logger.warning(
            "delivery failed for %s; message dropped (code=transport_error)",
            prepared.category,
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
    if prepared.ownership is not None:
        raise DurableDeliveryRequiredError("owned delivery journals must link a durable intent")
    if (
        session is not prepared._session
        or session.sync_session.get_transaction() is not prepared._transaction
    ):
        raise DurableDeliveryRequiredError(
            "zero-subject journal must share the network transaction"
        )
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
                [list(button) for button in prepared.buttons] if prepared.buttons else None
            ),
        }
    )
    row = Notification(
        subject_id=(prepared.ownership.subject_id if prepared.ownership is not None else None),
        actor_user_id=prepared.actor_user_id,
        recipient_user_id=(
            prepared.ownership.recipient_user_id if prepared.ownership is not None else None
        ),
        integration_connection_id=(
            prepared.ownership.connection_id if prepared.ownership is not None else None
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
    if ownership is not None:
        raise DurableDeliveryRequiredError("owned delivery must use the durable three-phase API")
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
