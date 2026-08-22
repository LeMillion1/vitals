"""What comes back from the channel: button taps, replies, and free text.

Three inbound shapes, deliberately three code paths:

  * **a tap** (``callback_query``) — the "не то" undo, and the day-context answers
    the evening block will ask for. Stateless: the button carries
    everything needed in its payload, so a tap works days later.
  * **a question** — either a reply to one of our messages or a plain «почему hrv
    просел?». Answered from the message replied to — or, when nothing was
    replied to, the last few messages we sent — *plus* the context the last brief
    was built on, and nothing else. This is still not a second chat with the
    data: deep questions belong in Claude.ai over MCP, which has 69 tools and a
    better model. Here the model sees the tail of the conversation, the day's
    numbers, and is told to invent nothing.
  * **anything else typed** — free text into ``signals`` (raw first, always),
    followed by an echo of what was understood plus one "не то" button.

Idempotency: Telegram retries a webhook until it gets a 200, so the same
``update_id`` can arrive several times. Every update is keyed into
``raw_payloads`` as ``tg:<update_id>``, and an update whose key is already there
is dropped — which reuses the data-lake table that has to hold the message
anyway, instead of a second bookkeeping store that could disagree with it.

``handle_text`` takes text, not a Telegram update (C8): adding voice notes later
is one transcription step *in front of* this pipeline, not a rewrite of it.
"""
from __future__ import annotations

import json
import logging
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import date as date_type, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import and_, func, or_, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    DigestKind,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    NotificationDeliveryErrorCode,
    NotificationDeliveryStatus,
    Severity,
    SignalKind,
    Source,
)
from vitals.i18n import t
from vitals.integrations.llm_client import LLMClient
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import Signal
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.services import (
    ai_gateway_service,
    alerts_service,
    digest_service,
    signals_service,
)
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.proactive import (
    day_plan,
    delivery,
    prefs,
    question_ai_service,
    signal_ai_service,
)
from vitals.services.proactive.channels import (
    BoundNotifier,
    BoundNotifierResolver,
    Notifier,
)
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.utils.timeutils import now_local, now_utc, to_local_naive

logger = logging.getLogger(__name__)

DOMAIN = Domain.SIGNALS.value
SOURCE = Source.TELEGRAM.value

# Button payloads (Telegram caps callback_data at 64 bytes, both fit comfortably).
CB_MISPARSE = "mis:"   # mis:<batch_id>
CB_CONTEXT = "ctx:"    # ctx:<iso date>:<key>:<value>

# The answer to any slash command, ``/start`` included. One reply for all of them
# on purpose: this bot has no command surface — everything it does is either
# initiated by it or written in plain words.
COMMAND_REPLY = (
    "Утром приношу разбор, вечером — итог дня.\n\n"
    "Пиши обычным текстом, как есть: «голова раскалывается», «кофе в 22», "
    "«спать хочу пиздец» — запишу и учту в разборах.\n\n"
    "Вес, уколы и еду вноси в приложении — здесь их нет."
)

# How many already-used keys the parser is shown. Reusing an existing key is the
# only thing keeping the open registry from drifting into 60 near-synonyms before
# the registry can be consolidated; the cap keeps the prompt small.
_KNOWN_KEYS_LIMIT = 40

_PARSER_SYSTEM = signal_ai_service.PARSER_SYSTEM

_REPLY_SYSTEM = """\
Ты отвечаешь на вопрос владельца дашборда здоровья.
Перед тобой могут быть последние сообщения самого бота (по порядку, последнее —
внизу) и JSON с данными последнего разбора дня. Короткий вопрос без пояснений
почти всегда про то, что бот только что написал — сначала ищи ответ там, и
только потом в JSON. Отвечай по-русски, коротко (2-4 предложения); числа бери
только из этих двух источников.
Если ответа в них нет — так и скажи. Никаких выдуманных чисел.\
"""

_REPLY_MAX_TOKENS = 800
# How far back the "what did you just say" context reaches. Three covers an echo
# followed by a nudge that landed in between; more starts pulling in yesterday.
_CONTEXT_MESSAGES = 3
_NO_LLM_REPLY = "Сейчас не отвечу — модель недоступна. Загляни в приложение."
_PARSER_PENDING_REPLY = "Сохранил как есть — разобрать не смог. Посмотрю позже."
_NO_SIGNAL_FACTS_REPLY = (
    "Записал. Фактов для графиков тут не нашёл — "
    "если что-то важное, скажи прямо."
)
# The recovery cursor contains only an opaque subject UUID and raw integer id.
# Paid/in-flight invocation gaps are queried independently of this cursor; the
# cursor exists solely so a long history of ordinary Telegram facts
# cannot keep an older pre-reservation question outside a fixed newest-N window.
_QUESTION_RECOVERY_CURSOR_PREFIX = "question-reply-recovery:cursor:"
_DELIVERY_RECOVERY_CURSOR_PREFIX = "raw-delivery-recovery:cursor:"
_QUESTION_RECOVERY_PAGE_SIZE = 100
_QUESTION_RECOVERY_SCAN_LIMIT = 1000
_QUESTION_RECOVERY_WORK_LIMIT = 20
# The day's numbers as JSON, capped: the brief's context grows a field per module
# and the prompt is paid for by the token.
_DAY_FACTS_LIMIT = 4000

# A question typed on its own has to be told apart from a fact, and asking the
# model which it is costs a call on every message. So: a question mark, or an
# opening question word. Matched as a *word*, never as a prefix — «что-то тошнит»
# is a symptom, not a question about «что».
_QUESTION_WORDS = frozenset({"почему", "что", "чем", "как", "сколько", "когда", "зачем"})

# Before this hour a message still belongs to the day that is ending: «кофе в 2»
# written at 00:30 is about the evening just spent, and filing it under the fresh
# calendar date buries it in tomorrow's brief while tonight's never sees it.
_DAY_ROLLS_OVER_AT = 4


def looks_like_question(text: str) -> bool:
    """Is this asked *of* the bot rather than told to it?"""
    stripped = text.strip()
    if stripped.endswith("?"):
        return True
    words = stripped.lower().replace(",", " ").split()
    if not words:
        return False
    return words[0] in _QUESTION_WORDS or words[:2] == ["стоит", "ли"]


def conversation_day(now: Optional[datetime] = None) -> date_type:
    """Which day a message written *now* is talking about."""
    moment = now or now_local()
    return (
        moment.date() - timedelta(days=1)
        if moment.hour < _DAY_ROLLS_OVER_AT
        else moment.date()
    )


def _telegram_message_day(message: dict) -> date_type | None:
    """Map Telegram's immutable original message timestamp to the health day."""

    value = message.get("date")
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
        if timestamp <= 0:
            return None
        moment = to_local_naive(
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return conversation_day(moment) if moment is not None else None


# ── Telegram update shape (the only place that knows it) ──────────────────────
def chat_id_of(update: dict) -> Optional[str]:
    """The chat an update came from, whichever shape it arrived in."""
    for holder in (
        update.get("message"),
        update.get("edited_message"),
        (update.get("callback_query") or {}).get("message"),
    ):
        chat = (holder or {}).get("chat") or {}
        if chat.get("id") is not None:
            return str(chat["id"])
    return None


def is_private_recipient_update(update: dict, expected_recipient_id: str) -> bool:
    """Accept only a private chat authored by the configured recipient."""

    try:
        expected_id = int(expected_recipient_id)
    except (TypeError, ValueError):
        return False
    if expected_id <= 0:
        return False
    callback = update.get("callback_query") or {}
    holder = (
        callback.get("message")
        if callback
        else update.get("message") or update.get("edited_message")
    )
    if not isinstance(holder, dict):
        return False
    chat = holder.get("chat") or {}
    sender = (callback.get("from") if callback else holder.get("from")) or {}
    try:
        chat_id = int(chat.get("id"))
        sender_id = int(sender.get("id"))
    except (TypeError, ValueError):
        return False
    return chat.get("type") == "private" and chat_id == sender_id == expected_id


class DurableInboundProcessingError(RuntimeError):
    """Processing failed after the complete update was committed to the lake."""


class InboundOwnershipError(RuntimeError):
    """The resolved subject/recipient/connection graph is inconsistent."""


_LIVE_TELEGRAM_CONNECTION_STATUSES = {
    IntegrationConnectionStatus.LEGACY.value,
    IntegrationConnectionStatus.ACTIVE.value,
}
_HISTORICAL_CONNECTION_STATUSES = (
    _LIVE_TELEGRAM_CONNECTION_STATUSES
    | {
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }
)


@dataclass(frozen=True, slots=True)
class _RawClaim:
    raw: RawPayload
    created: bool


@dataclass(frozen=True, slots=True)
class _ClaimedRawDelivery:
    category: str
    status: str
    ai_invocation_id: uuid.UUID | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _RecoverableDeliveryCandidate:
    updated_at: datetime
    intent_id: uuid.UUID
    raw_payload_id: int


@dataclass(frozen=True, slots=True)
class _DeliveredRaw:
    journal: Notification
    notifier: BoundNotifier


@dataclass(slots=True)
class _RawDeliveryRecoveryProgress:
    claimed_work: bool = False


class _RawRecoveryState(Enum):
    UNCLAIMED = "unclaimed"
    AUTHORITATIVE = "authoritative"
    RECOVERED = "recovered"


def _require_parser_alert_context(
    context: alerts_service.ProviderAlertContext,
    *,
    subject_id: uuid.UUID,
) -> None:
    if not isinstance(context, alerts_service.ProviderAlertContext):
        raise InboundOwnershipError(
            "parser alert context must be a ProviderAlertContext"
        )
    if context.identity.subject_id != subject_id:
        raise InboundOwnershipError("parser alert context belongs to another subject")
    if context.identity.actor_user_id is not None:
        raise InboundOwnershipError("parser alert context must be actorless")
    if context.provider is not IntegrationProvider.OPENROUTER:
        raise InboundOwnershipError("parser alert context must use OpenRouter")


async def _validate_parser_alert_connection(
    session: AsyncSession,
    *,
    context: alerts_service.ProviderAlertContext,
    subject_id: uuid.UUID,
) -> None:
    """Validate the exact frozen OpenRouter root before a parser network await."""

    _require_parser_alert_context(context, subject_id=subject_id)
    connection = (
        await session.execute(
            select(
                IntegrationConnection.subject_id,
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
            ).where(
                IntegrationConnection.id == context.integration_connection_id
            )
        )
    ).one_or_none()
    if connection is None:
        raise InboundOwnershipError("parser OpenRouter connection does not exist")
    connection_subject, provider, connection_type, status = connection
    if connection_subject != subject_id:
        raise InboundOwnershipError(
            "parser OpenRouter connection belongs to another subject"
        )
    if provider != IntegrationProvider.OPENROUTER.value:
        raise InboundOwnershipError("parser connection is not OpenRouter")
    if connection_type != IntegrationConnectionType.AI_GATEWAY.value:
        raise InboundOwnershipError("parser connection is not an AI gateway")
    known = {status.value for status in IntegrationConnectionStatus}
    if status not in known:
        raise InboundOwnershipError("parser connection lifecycle is unknown")
    if status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }:
        raise InboundOwnershipError(f"{status} OpenRouter connection cannot parse")


async def _reconcile_parser_alert_best_effort(
    session: AsyncSession,
    *,
    context: alerts_service.ProviderAlertContext | None,
    outcome: signals_service.ParserOutcome,
) -> None:
    """Reconcile after durable parsing state; never unwind raw or Signal facts."""

    if context is None or outcome.attempted == 0:
        return
    try:
        if outcome.failures:
            await alerts_service.raise_scoped_alert(
                session,
                context=context,
                domain=Domain.SIGNALS,
                severity=Severity.WARN,
                message=t("alert.signal_parser_failed"),
                alert_key=signals_service.PARSER_FAILED_ALERT_KEY,
                legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
            )
        else:
            resolution_context = await _parser_alert_resolution_context(
                session,
                context=context,
            )
            await alerts_service.resolve_scoped_by_key(
                session,
                context=resolution_context,
                alert_key=signals_service.PARSER_FAILED_ALERT_KEY,
                legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
            )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.warning(
            "could not reconcile the OpenRouter signal-parser alert "
            "(code=alert_reconcile_failed)",
        )


async def _parser_alert_resolution_context(
    session: AsyncSession,
    *,
    context: alerts_service.ProviderAlertContext,
) -> alerts_service.ProviderAlertContext:
    """Select the exact current or historical OpenRouter C for parser recovery."""

    # The current schema still has one global active (key, entity) slot. Serialize
    # its ownership projection with subject creation and keep this transaction
    # open into the scoped resolver's re-entrant governance -> S -> C -> row locks.
    await acquire_identity_governance_lock(session)
    rows = list(
        await session.execute(
            select(
                SystemAlert.subject_id,
                SystemAlert.integration_connection_id,
            )
            .where(
                SystemAlert.alert_key == signals_service.PARSER_FAILED_ALERT_KEY,
                SystemAlert.entity_ref == "",
                SystemAlert.resolved_at.is_(None),
            )
            .limit(2)
        )
    )
    if not rows:
        return context
    if len(rows) != 1:
        raise InboundOwnershipError("parser alert ownership is ambiguous")
    alert_subject_id, alert_connection_id = rows[0]
    if alert_subject_id is None and alert_connection_id is None:
        return context
    if alert_subject_id != context.identity.subject_id:
        raise InboundOwnershipError("parser alert belongs to another subject")
    if alert_connection_id is None:
        raise InboundOwnershipError("parser alert has a partial ownership root")
    if alert_connection_id == context.integration_connection_id:
        return context

    connection = (
        await session.execute(
            select(
                IntegrationConnection.subject_id,
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
            ).where(IntegrationConnection.id == alert_connection_id)
        )
    ).one_or_none()
    if connection is None:
        raise InboundOwnershipError("historical parser connection does not exist")
    connection_subject, provider, connection_type, status = connection
    if connection_subject != context.identity.subject_id:
        raise InboundOwnershipError(
            "historical parser connection belongs to another subject"
        )
    if provider != IntegrationProvider.OPENROUTER.value:
        raise InboundOwnershipError("historical parser connection is not OpenRouter")
    if connection_type != IntegrationConnectionType.AI_GATEWAY.value:
        raise InboundOwnershipError(
            "historical parser connection is not an AI gateway"
        )
    if status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }:
        raise InboundOwnershipError(
            "historical parser connection cannot resolve alerts"
        )
    return alerts_service.ProviderAlertContext(
        identity=context.identity,
        provider=IntegrationProvider.OPENROUTER,
        integration_connection_id=alert_connection_id,
    )


def _owned_or_legacy_raw_scope(ownership: ProactiveOwnershipContext):
    owned = RawPayload.subject_id == ownership.subject_id
    if ownership.include_legacy_unowned:
        owned = or_(
            owned,
            and_(
                RawPayload.subject_id.is_(None),
                RawPayload.actor_user_id.is_(None),
                RawPayload.integration_connection_id.is_(None),
                RawPayload.file_asset_id.is_(None),
            ),
        )
    return owned


async def _lock_subject_root(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
) -> HealthSubject:
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == ownership.subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subject is None:
        raise InboundOwnershipError("Telegram subject does not exist")
    if subject.owner_user_id != ownership.recipient_user_id:
        raise InboundOwnershipError("Telegram recipient is not the subject owner")
    return subject


async def _load_telegram_connection(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    subject_id: uuid.UUID,
    allow_historical: bool,
    lock: bool = True,
) -> IntegrationConnection:
    stmt = select(IntegrationConnection).where(
        IntegrationConnection.id == connection_id
    )
    if lock:
        stmt = stmt.with_for_update()
    connection = await session.scalar(
        stmt.execution_options(populate_existing=True)
    )
    if connection is None:
        raise InboundOwnershipError("Telegram connection does not exist")
    if connection.subject_id != subject_id:
        raise InboundOwnershipError("Telegram connection belongs to another subject")
    if (
        connection.provider != IntegrationProvider.TELEGRAM.value
        or connection.connection_type != IntegrationConnectionType.RECIPIENT.value
    ):
        raise InboundOwnershipError("connection is not a Telegram recipient")
    known = {status.value for status in IntegrationConnectionStatus}
    if connection.status not in known:
        raise InboundOwnershipError("Telegram connection lifecycle is unknown")
    allowed = (
        _HISTORICAL_CONNECTION_STATUSES
        if allow_historical
        else _LIVE_TELEGRAM_CONNECTION_STATUSES
    )
    if connection.status not in allowed:
        operation = "historical provenance" if allow_historical else "ingest"
        raise InboundOwnershipError(
            f"{connection.status} Telegram connection cannot {operation}"
        )
    return connection


async def _validate_raw_root(
    session: AsyncSession,
    raw: RawPayload,
    *,
    ownership: ProactiveOwnershipContext,
    lock_connection: bool = True,
    allow_subject_adopted_unowned: bool = False,
    allow_historical_null_actor_connection: bool = False,
) -> None:
    if raw.domain != DOMAIN or raw.source != SOURCE:
        raise InboundOwnershipError(
            "Telegram raw has mismatched domain or source"
        )
    if raw.subject_id is None:
        if not ownership.include_legacy_unowned or any(
            value is not None
            for value in (
                raw.actor_user_id,
                raw.integration_connection_id,
                raw.file_asset_id,
            )
        ):
            raise InboundOwnershipError("Telegram raw has invalid legacy roots")
        return
    if raw.file_asset_id is not None:
        raise InboundOwnershipError("Telegram raw cannot reference a file asset")
    if raw.subject_id != ownership.subject_id:
        raise InboundOwnershipError("Telegram raw belongs to another subject")
    if raw.actor_user_id is None and raw.integration_connection_id is None:
        if not (
            allow_subject_adopted_unowned
            and ownership.include_legacy_unowned
        ):
            raise InboundOwnershipError(
                "subject-adopted Telegram raw requires the exact-one bridge"
            )
        subject_ids = list(
            await session.scalars(
                select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
            )
        )
        if subject_ids != [ownership.subject_id]:
            raise InboundOwnershipError(
                "subject-adopted Telegram raw requires exactly one subject"
            )
        return
    if raw.actor_user_id is None:
        if not (
            allow_historical_null_actor_connection
            and ownership.include_legacy_unowned
        ):
            raise InboundOwnershipError(
                "Telegram raw has partial actor/connection roots"
            )
        subject_ids = list(
            await session.scalars(
                select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
            )
        )
        if subject_ids != [ownership.subject_id]:
            raise InboundOwnershipError(
                "historical actorless Telegram raw requires exactly one subject"
            )
        assert raw.integration_connection_id is not None
        await _load_telegram_connection(
            session,
            connection_id=raw.integration_connection_id,
            subject_id=ownership.subject_id,
            allow_historical=True,
            lock=lock_connection,
        )
        return
    if raw.actor_user_id != ownership.recipient_user_id:
        raise InboundOwnershipError(
            "owned Telegram raw actor is not the subject owner"
        )
    if raw.integration_connection_id is None:
        raise InboundOwnershipError("owned Telegram raw has no recipient connection")
    await _load_telegram_connection(
        session,
        connection_id=raw.integration_connection_id,
        subject_id=ownership.subject_id,
        allow_historical=True,
        lock=lock_connection,
    )


def _raw_storage_update(update: dict) -> dict:
    """Keep the inbound message, but never duplicate prior bot output in raw.

    Telegram embeds the complete replied-to message in a new update. For a
    platform-AI answer that would copy the memory-only completion into
    ``raw_payloads`` on the owner's next reply. The pipeline needs only the
    immutable Telegram message id to resolve its already-authorized Notification
    context, so nested reply bodies are deliberately reduced to that id. Callback
    envelopes likewise keep their opaque callback data and message identity, not
    the bot-authored rendered message.
    """

    if not isinstance(update, dict):
        raise TypeError("Telegram update must be a dict")
    stored = deepcopy(update)
    for key in ("message", "edited_message"):
        message = stored.get(key)
        if not isinstance(message, dict):
            continue
        replied = message.get("reply_to_message")
        replied_from = (
            replied.get("from")
            if isinstance(replied, dict) and isinstance(replied.get("from"), dict)
            else {}
        )
        # In the private owner chat Telegram supplies ``from.is_bot`` on the
        # embedded message. Preserve user-authored reply context raw-first; only
        # bot output can contain the deliberately memory-only AI completion.
        if isinstance(replied, dict) and replied_from.get("is_bot") is True:
            message_id = replied.get("message_id")
            message["reply_to_message"] = (
                {"message_id": message_id}
                if message_id is not None
                else {}
            )
    callback = stored.get("callback_query")
    if isinstance(callback, dict) and isinstance(callback.get("message"), dict):
        message = callback["message"]
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        callback["message"] = {
            "message_id": message.get("message_id"),
            "date": message.get("date"),
            "chat": {"id": chat.get("id"), "type": chat.get("type")},
        }
    return stored


async def _claim_update_raw(
    session: AsyncSession,
    *,
    external_id: str,
    payload: dict,
    ownership: ProactiveOwnershipContext,
) -> _RawClaim:
    """Atomically claim one subject/update id and commit the full envelope.

    The subject lock closes PostgreSQL's absent-row race before the connection
    and raw locks. The lookup is subject-wide so a retry remains a no-op after a
    connection rotation. Existing rows are never refreshed or reprocessed.
    """

    stored_payload = _raw_storage_update(payload)
    await _lock_subject_root(session, ownership=ownership)
    await _load_telegram_connection(
        session,
        connection_id=ownership.connection_id,
        subject_id=ownership.subject_id,
        allow_historical=False,
    )
    rows = list(
        await session.scalars(
            select(RawPayload)
            .where(
                _owned_or_legacy_raw_scope(ownership),
                RawPayload.domain == DOMAIN,
                RawPayload.source == SOURCE,
                RawPayload.external_id == external_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(rows) > 1:
        raise InboundOwnershipError("ambiguous Telegram update claim")
    if rows:
        raw = rows[0]
        await _validate_raw_root(
            session,
            raw,
            ownership=ownership,
            allow_historical_null_actor_connection=True,
        )
        await session.commit()
        return _RawClaim(raw=raw, created=False)

    raw = RawPayload(
        subject_id=ownership.subject_id,
        actor_user_id=ownership.recipient_user_id,
        integration_connection_id=ownership.connection_id,
        domain=DOMAIN,
        source=SOURCE,
        external_id=external_id,
        payload=stored_payload,
        fetched_at=now_local(),
    )
    session.add(raw)
    await session.flush()
    await session.commit()
    return _RawClaim(raw=raw, created=True)


async def handle_update(
    session: AsyncSession,
    update: dict,
    *,
    notifier: Optional[Notifier],
    parse: Optional[signals_service.Parser] = None,
    ownership: ProactiveOwnershipContext,
    parser_alert_context: alerts_service.ProviderAlertContext | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> None:
    """Entry point for one Telegram update. Safe to call twice with the same one."""
    update_id = update.get("update_id")
    external_id = f"tg:{update_id}" if update_id is not None else None
    if not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext")
    callback = update.get("callback_query")
    message = update.get("message") or update.get("edited_message") or {}
    edited = "edited_message" in update
    text = (message.get("text") or "").strip()
    if external_id is None or (not callback and not text):
        return

    parked = False
    try:
        claim = await _claim_update_raw(
            session,
            external_id=external_id,
            payload=update,
            ownership=ownership,
        )
        # A durable claim means later failures must be retried/recovered rather
        # than silently acknowledged, whether this is its first webhook or a
        # duplicate used to resume a question after a process crash.
        parked = True
        if not claim.created:
            enabled = await prefs.bot_enabled(
                session, subject_id=ownership.subject_id, strict=True
            )
            await session.commit()
            if enabled and not callback:
                await _recover_claimed_text(
                    session,
                    raw=claim.raw,
                    notifier=notifier,
                    ownership=ownership,
                    parse=parse,
                    parser_alert_context=parser_alert_context,
                    notifier_resolver=notifier_resolver,
                )
            return

        if edited:
            try:
                await _supersede_edited_message(
                    session,
                    claim.raw,
                    ownership=ownership,
                    allow_historical_null_actor_connection=True,
                )
            except signals_service.RawPayloadAlreadyProcessedError:
                return

        enabled = await prefs.bot_enabled(
            session,
            subject_id=ownership.subject_id,
            strict=True,
        )
        # A strict scoped-setting read may use the legacy governance bridge.
        # Close that read transaction before any parser or Telegram network await.
        await session.commit()
        if not enabled:
            if callback or text.startswith("/"):
                await _mark_raw_processed(
                    session,
                    claim.raw,
                    ownership=ownership,
                )
            logger.info(
                "stored inbound update without active processing: "
                "the signals module is switched off"
            )
            return

        if callback:
            await _handle_callback(
                session,
                callback,
                notifier=notifier,
                external_id=None,
                ownership=ownership,
                raw=claim.raw,
            )
            return

        await handle_text(
            session,
            text,
            notifier=notifier,
            external_id=None,
            message_id=message.get("message_id"),
            reply_to_message_id=(message.get("reply_to_message") or {}).get(
                "message_id"
            ),
            parse=parse,
            on_date=_telegram_message_day(message),
            ownership=ownership,
            raw=claim.raw,
            edited=False,
            parser_alert_context=parser_alert_context,
            notifier_resolver=notifier_resolver,
        )
    except Exception as exc:
        if parked:
            raise DurableInboundProcessingError(
                "Telegram update failed after durable raw capture"
            ) from exc
        raise


# ── Taps ──────────────────────────────────────────────────────────────────────
async def _mark_subject_batch_misparse(
    session: AsyncSession,
    batch_id: str,
    *,
    ownership: ProactiveOwnershipContext,
) -> int:
    """Undo a Telegram batch across recipient-connection rotation."""

    await _lock_subject_root(session, ownership=ownership)
    owned = Signal.subject_id == ownership.subject_id
    if ownership.include_legacy_unowned:
        owned = or_(
            owned,
            and_(
                Signal.subject_id.is_(None),
                Signal.actor_user_id.is_(None),
                Signal.integration_connection_id.is_(None),
            ),
        )
    rows = list(
        await session.scalars(
            select(Signal)
            .where(owned, Signal.batch_id == batch_id)
            .with_for_update()
        )
    )
    for row in rows:
        if row.source != SOURCE:
            raise InboundOwnershipError("undo target is not a Telegram signal")
        if row.subject_id is None:
            continue
        if row.actor_user_id not in {None, ownership.recipient_user_id}:
            raise InboundOwnershipError("Telegram signal belongs to another actor")
        if row.integration_connection_id is None:
            raise InboundOwnershipError("owned Telegram signal has no connection")
        await _load_telegram_connection(
            session,
            connection_id=row.integration_connection_id,
            subject_id=ownership.subject_id,
            allow_historical=True,
        )
    return await signals_service.mark_misparse(
        session,
        batch_id,
        subject_id=ownership.subject_id,
        integration_connection_id=None,
    )


async def _handle_callback(
    session: AsyncSession,
    callback: dict,
    *,
    notifier: Optional[Notifier],
    external_id: Optional[str],
    ownership: ProactiveOwnershipContext,
    raw: RawPayload | None = None,
    historical_connection: bool = False,
) -> None:
    data = str(callback.get("data") or "")
    callback_id = str(callback.get("id") or "")

    # The tap itself is data too — which button, when — so it lands in the lake
    # like everything else, and doubles as this update's idempotency record.
    if raw is None and external_id:
        claim = await _claim_update_raw(
            session,
            external_id=external_id,
            payload={"callback_query": callback},
            ownership=ownership,
        )
        if not claim.created:
            return
        raw = claim.raw

    toast = ""
    answered: Optional[tuple[date_type, str]] = None
    if data.startswith(CB_MISPARSE):
        batch_id = data[len(CB_MISPARSE):]
        changed = await _mark_subject_batch_misparse(
            session,
            batch_id,
            ownership=ownership,
        )
        # The rows stay, flagged: they are the material the key registry gets
        # built from — real mistakes, not remembered ones.
        toast = "Убрал из графиков" if changed else "Уже убрано"
    elif data.startswith(CB_CONTEXT):
        answered = await _apply_context(
            session,
            data,
            ownership=ownership,
            historical_connection=historical_connection,
        )
        toast = "Записал" if answered is not None else ""

    if raw is not None:
        raw.processed_at = now_local()
        await session.flush()

    # The callback action and terminal raw marker are durable before Telegram
    # network awaits. This also releases Subject/C/data locks for live updates.
    await session.commit()

    if notifier is not None and callback_id:
        # Acknowledged first: Telegram spins on the button until this lands, and
        # the redraw below is a second round-trip the spinner shouldn't wait for.
        try:
            await notifier.answer_callback(callback_id, toast)
        except Exception:
            logger.warning(
                "could not acknowledge Telegram callback (code=callback_failed)"
            )

    if notifier is not None and answered is not None:
        await _redraw(
            session,
            callback,
            *answered,
            notifier=notifier,
            ownership=ownership,
        )


async def _redraw(
    session: AsyncSession,
    callback: dict,
    on_date: date_type,
    key: str,
    *,
    notifier: Notifier,
    ownership: ProactiveOwnershipContext,
) -> None:
    """The message that asked now says what was answered.

    Without it a tap leaves nothing but a grey toast: the line still reads out the
    template's guess and the same keyboard still sits under it, which looks like
    "не нажалось" and gets tapped again. Rebuilt from the day, not from the tap —
    two taps in a row must not each drop the other's answer.
    """
    message = callback.get("message") or {}
    message_id = message.get("message_id")
    text = message.get("text") or ""
    if not message_id or not text:
        return

    answers, answered = await day_plan.resolve(
        session,
        on_date,
        subject_id=ownership.subject_id,
    )
    buttons = day_plan.exception_buttons(
        answers, on_date, answered, day_plan.questions_for(key)
    )
    try:
        await notifier.edit(
            str(message_id),
            day_plan.redraw(text, answers, has_buttons=bool(buttons)),
            buttons=buttons or None,
        )
    except Exception:
        # The answer is already stored; a channel that refused the edit (the
        # message is old, or nothing actually changed) is worth a log, not a
        # failed update Telegram would then retry for hours.
        logger.warning("could not redraw Telegram message (code=edit_failed)")


async def _apply_context(
    session: AsyncSession,
    data: str,
    *,
    ownership: ProactiveOwnershipContext,
    historical_connection: bool = False,
) -> Optional[tuple[date_type, str]]:
    """``ctx:<iso date>:<key>:<value>`` → merge one answer into that day's context.

    Returns the day answered and the question answered (``None`` if the payload
    was rejected). The redraw needs both: the day to rebuild the answers from,
    and the question to know which of the two keyboards this tap came off —
    the evening sends a recap one and a plan one, and rebuilding the wrong set
    would hang tomorrow's buttons under today's question.

    The date rides in the payload rather than being "today": the evening block
    asks about *tomorrow*, and a tap that lands after midnight must still answer
    the day it was asked about. Merging (and keeping the template's guess beside
    the answer) is ``day_plan``'s job — here we only decode the payload.

    The decoded pair is checked against the question registry: Telegram keeps old
    keyboards tappable forever, so a button sent before a question was renamed or
    dropped would otherwise write a key nothing reads back.
    """
    try:
        _, iso_date, key, value = data.split(":", 3)
        on_date = date_type.fromisoformat(iso_date)
    except ValueError:
        logger.warning("unparseable Telegram context payload (code=invalid_callback)")
        return None

    question = day_plan.QUESTIONS_BY_KEY.get(key)
    answer = day_plan.decode(value)
    if question is None or answer not in question.labels:
        logger.warning(
            "Telegram context payload outside registry (code=invalid_callback)"
        )
        return None

    await day_plan.record_answer(
        session,
        on_date,
        key,
        answer,
        identity=ownership.owner_action(),
        integration_connection_id=ownership.connection_id,
        source=Source.TELEGRAM.value,
        allow_historical_connection=historical_connection,
    )
    return on_date, key


# ── Text ──────────────────────────────────────────────────────────────────────
def _message_from_raw(raw: RawPayload) -> tuple[dict, bool]:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    message = payload.get("message")
    if isinstance(message, dict):
        return message, False
    edited = payload.get("edited_message")
    if isinstance(edited, dict):
        return edited, True
    return {}, False


def _text_from_raw(raw: RawPayload) -> Optional[str]:
    """Project text without teaching the signals service Telegram's wire shape."""

    message, _edited = _message_from_raw(raw)
    if message:
        return str(message.get("text") or "")
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    # Compatibility with rows parked by the channel-neutral pre-claim path.
    return str(payload.get("text") or "") if "text" in payload else None


def _day_from_raw(raw: RawPayload) -> date_type | None:
    message, _edited = _message_from_raw(raw)
    return _telegram_message_day(message) if message else None


def _callback_from_raw(raw: RawPayload) -> Optional[dict]:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    callback = payload.get("callback_query")
    if isinstance(callback, dict):
        return callback
    # Compatibility with the previous callback-only raw envelope.
    return payload if "data" in payload else None


async def _raw_text_is_signal_candidate(
    session: AsyncSession,
    raw: RawPayload,
    *,
    ownership: ProactiveOwnershipContext,
) -> bool:
    """Apply the live command/question/reply classifier during recovery."""

    message, _edited = _message_from_raw(raw)
    text = str((_text_from_raw(raw) or "")).strip()
    if text.startswith("/"):
        return False
    reply_message = message.get("reply_to_message")
    if reply_message is None:
        reply_message = {}
    if not isinstance(reply_message, dict):
        raise InboundOwnershipError(
            "Telegram recovery reply provenance is invalid"
        )
    reply_id = reply_message.get("message_id")
    answered = (
        await delivery.find_sent(
            session,
            str(reply_id),
            ownership=ownership,
        )
        if reply_id is not None
        else None
    )
    to_evening = (
        answered is not None
        and answered.category == delivery.CATEGORY_EVENING
    )
    return to_evening or (
        answered is None and not looks_like_question(text)
    )


async def _lock_pending_raw_for_completion(
    session: AsyncSession,
    raw: RawPayload,
    *,
    ownership: ProactiveOwnershipContext,
    allow_subject_adopted_unowned: bool = False,
    allow_historical_null_actor_connection: bool = False,
) -> RawPayload:
    """Lock S -> historical C -> raw and reject a superseded stale instance."""

    await _lock_subject_root(session, ownership=ownership)
    candidates = list(
        await session.scalars(
            select(RawPayload)
            .where(
                _owned_or_legacy_raw_scope(ownership),
                RawPayload.domain == DOMAIN,
                RawPayload.source == SOURCE,
                RawPayload.id != raw.id,
            )
            .order_by(RawPayload.id)
        )
    )
    later = [
        candidate
        for candidate in _same_message_versions(raw, candidates)
        if _is_prior_message_version(raw, candidate)
    ]
    connection_ids = sorted(
        {
            candidate.integration_connection_id
            for candidate in [raw, *later]
            if candidate.integration_connection_id is not None
        },
        key=str,
    )
    for connection_id in connection_ids:
        await _load_telegram_connection(
            session,
            connection_id=connection_id,
            subject_id=ownership.subject_id,
            allow_historical=True,
        )
    locked_rows = list(
        await session.scalars(
            select(RawPayload)
            .where(
                RawPayload.id.in_(
                    [raw.id, *(candidate.id for candidate in later)]
                )
            )
            .order_by(RawPayload.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    locked = next(
        (candidate for candidate in locked_rows if candidate.id == raw.id),
        None,
    )
    if locked is None:
        raise InboundOwnershipError("Telegram raw does not exist")
    for candidate in locked_rows:
        await _validate_raw_root(
            session,
            candidate,
            ownership=ownership,
            lock_connection=False,
            allow_subject_adopted_unowned=allow_subject_adopted_unowned,
            allow_historical_null_actor_connection=(
                allow_historical_null_actor_connection
            ),
        )
    if locked.processed_at is not None:
        raise signals_service.RawPayloadAlreadyProcessedError(
            "Telegram raw was already processed or superseded"
        )
    if later:
        locked.processed_at = now_local()
        await session.flush()
        raise signals_service.RawPayloadAlreadyProcessedError(
            "Telegram raw has a newer logical message version"
        )
    return locked


async def _mark_raw_processed(
    session: AsyncSession,
    raw: RawPayload,
    *,
    ownership: ProactiveOwnershipContext,
    allow_subject_adopted_unowned: bool = False,
    allow_historical_null_actor_connection: bool = False,
    commit_success: bool = True,
) -> bool:
    try:
        locked = await _lock_pending_raw_for_completion(
            session,
            raw,
            ownership=ownership,
            allow_subject_adopted_unowned=allow_subject_adopted_unowned,
            allow_historical_null_actor_connection=(
                allow_historical_null_actor_connection
            ),
        )
    except signals_service.RawPayloadAlreadyProcessedError:
        # Release the root locks; a newer edit already owns the terminal state.
        await session.commit()
        return False
    locked.processed_at = now_local()
    await session.flush()
    if commit_success:
        # Commands/questions must not become facts if the later outbound call fails.
        await session.commit()
    return True


def _telegram_update_sequence(raw: RawPayload) -> int | None:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    value = payload.get("update_id")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _telegram_message_identity(raw: RawPayload) -> tuple[str | None, str] | None:
    message, _edited = _message_from_raw(raw)
    message_id = message.get("message_id")
    if message_id is None:
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = chat.get("id")
    return (str(chat_id) if chat_id is not None else None, str(message_id))


def _is_prior_message_version(candidate: RawPayload, current: RawPayload) -> bool:
    candidate_sequence = _telegram_update_sequence(candidate)
    current_sequence = _telegram_update_sequence(current)
    if candidate_sequence is not None and current_sequence is not None:
        return candidate_sequence < current_sequence
    # Historical synthetic rows did not retain Telegram's outer update id. Their
    # insertion order is the only stable ordering available for compatibility.
    return candidate.id < current.id


async def _raw_delivery_is_current(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    ai_invocation_id: uuid.UUID | None,
    ai_purpose: AIInvocationPurpose | None,
    expected_processed: bool,
    reject_misparse: bool,
) -> bool:
    """Lock one logical message and validate its exact delivery provenance."""

    if isinstance(raw_payload_id, bool) or not isinstance(raw_payload_id, int):
        raise InboundOwnershipError("delivery raw id is invalid")
    if ai_invocation_id is not None and not isinstance(ai_invocation_id, uuid.UUID):
        raise InboundOwnershipError("delivery invocation id is invalid")
    if (ai_invocation_id is None) != (ai_purpose is None):
        raise InboundOwnershipError("delivery invocation purpose is inconsistent")
    if not isinstance(expected_processed, bool) or not isinstance(reject_misparse, bool):
        raise TypeError("delivery raw state expectations must be bools")
    await acquire_identity_governance_lock(session)
    await _lock_subject_root(session, ownership=ownership)
    raw = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == raw_payload_id)
        .execution_options(populate_existing=True)
    )
    if raw is None:
        raise InboundOwnershipError("delivery raw does not exist")
    candidates = list(
        await session.scalars(
            select(RawPayload)
            .where(
                _owned_or_legacy_raw_scope(ownership),
                RawPayload.domain == DOMAIN,
                RawPayload.source == SOURCE,
            )
            .order_by(RawPayload.id)
        )
    )
    versions = _same_message_versions(raw, candidates)
    connection_ids = sorted(
        {
            candidate.integration_connection_id
            for candidate in [raw, *versions]
            if candidate.integration_connection_id is not None
        }
        | {ownership.connection_id},
        key=str,
    )
    for connection_id in connection_ids:
        await _load_telegram_connection(
            session,
            connection_id=connection_id,
            subject_id=ownership.subject_id,
            allow_historical=connection_id != ownership.connection_id,
        )
    locked = list(
        await session.scalars(
            select(RawPayload)
            .where(RawPayload.id.in_({raw.id, *(row.id for row in versions)}))
            .order_by(RawPayload.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    current = next((row for row in locked if row.id == raw.id), None)
    if current is None:
        raise InboundOwnershipError("delivery raw disappeared")
    for candidate in locked:
        await _validate_raw_root(
            session,
            candidate,
            ownership=ownership,
            lock_connection=False,
            allow_subject_adopted_unowned=True,
            allow_historical_null_actor_connection=True,
        )
    if any(_is_prior_message_version(current, row) for row in locked):
        return False
    if (current.processed_at is not None) != expected_processed:
        return False
    if ai_invocation_id is not None:
        invocation = (
            await session.execute(
                select(
                    AIInvocation.subject_id,
                    AIInvocation.actor_user_id,
                    AIInvocation.raw_payload_id,
                    AIInvocation.purpose,
                    AIInvocation.source,
                    AIInvocation.status,
                )
                .where(AIInvocation.id == ai_invocation_id)
                .with_for_update()
            )
        ).one_or_none()
        if invocation is None:
            raise InboundOwnershipError("delivery invocation does not exist")
        subject_id, actor_id, linked_raw_id, purpose, source, status = invocation
        allowed_statuses = {
            AIInvocationStatus.SUCCEEDED.value,
            AIInvocationStatus.FAILED.value,
            AIInvocationStatus.AMBIGUOUS.value,
        }
        if ai_purpose is AIInvocationPurpose.QUESTION_REPLY:
            allowed_statuses.add(AIInvocationStatus.CANCELLED.value)
        if (
            subject_id != ownership.subject_id
            or actor_id != ownership.recipient_user_id
            or linked_raw_id != raw_payload_id
            or purpose != ai_purpose.value
            or source != AIInvocationSource.TELEGRAM.value
            or status not in allowed_statuses
        ):
            raise InboundOwnershipError("delivery invocation provenance is invalid")
    if not reject_misparse:
        return True
    stale_fact = await session.scalar(
        select(Signal.id).where(
            Signal.raw_id == raw_payload_id,
            Signal.misparse.is_(True),
        ).limit(1)
    )
    return stale_fact is None


async def _deliver_owned_raw(
    session: AsyncSession,
    notifier: Notifier | None,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    text: str,
    category: str,
    idempotency_key: str,
    ai_invocation_id: uuid.UUID | None = None,
    legacy_dedupe_key: str | None = None,
    buttons=None,
    reply_to: str | None = None,
    redact_journal_content: bool = False,
    is_current: Callable[[], Awaitable[bool]],
    rearm_stale_before: datetime | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
    preparation_scope: delivery.DeliveryPreparationScope | None = None,
    recovery_progress: _RawDeliveryRecoveryProgress | None = None,
) -> _DeliveredRaw | None:
    """Perform one raw-backed delivery with no database transaction on I/O."""

    if preparation_scope is not None and rearm_stale_before is not None:
        raise InboundOwnershipError(
            "stale delivery recovery cannot consume a fresh preparation scope"
        )
    if recovery_progress is not None and rearm_stale_before is None:
        raise InboundOwnershipError(
            "delivery recovery progress requires a stale re-arm"
        )
    if notifier is None and rearm_stale_before is None:
        if preparation_scope is not None:
            await session.rollback()
            raise InboundOwnershipError(
                "scoped delivery requires an exact bound notifier"
            )
        await session.commit()
        return None
    if notifier is not None and not isinstance(notifier, BoundNotifier):
        raise InboundOwnershipError("owned delivery requires an exact bound notifier")

    prepare = (
        delivery.prepare_delivery_intent
        if rearm_stale_before is None
        else delivery.rearm_stale_raw_delivery_intent
    )
    prepare_kwargs = {}
    if rearm_stale_before is not None:
        prepare_kwargs["stale_before"] = rearm_stale_before
    elif preparation_scope is not None:
        prepare_kwargs["preparation_scope"] = preparation_scope
    prepared = await prepare(
        session,
        notifier,
        text=text,
        category=category,
        idempotency_key=idempotency_key,
        legacy_dedupe_key=legacy_dedupe_key,
        buttons=buttons,
        reply_to=reply_to,
        ownership=ownership,
        raw_payload_id=raw_payload_id,
        ai_invocation_id=ai_invocation_id,
        redact_journal_content=redact_journal_content,
        **prepare_kwargs,
    )
    if prepared is None and preparation_scope is not None:
        # Reply/echo continuations have no quiet-hours or budget rejection. None
        # therefore means either an exact authoritative claim/journal already
        # exists or the module was disabled under the frozen scope. In both cases
        # the domain terminal marker must commit: rolling it back would resurrect
        # the raw on re-enable and could create a later send against policy.
        if not await prefs.bot_enabled(
            session,
            subject_id=ownership.subject_id,
            strict=True,
        ):
            current_raw = await session.get(
                RawPayload,
                raw_payload_id,
                populate_existing=True,
            )
            if current_raw is None:
                raise InboundOwnershipError(
                    "disabled scoped delivery raw disappeared"
                )
            current_raw.processed_at = current_raw.processed_at or now_local()
            await session.flush()
        await session.commit()
        return None
    if prepared is not None and not await is_current():
        # T1 and its new PENDING row disappear together.
        await session.rollback()
        return None
    await session.commit()
    if recovery_progress is not None and prepared is not None:
        recovery_progress.claimed_work = True
    if prepared is None:
        return None

    if notifier_resolver is None:
        from vitals.services.proactive import channels

        notifier_resolver = channels.resolve_legacy_bound_notifier
    dispatched_notifier: BoundNotifier | None = None

    def _resolve_current_notifier(binding, credential_ref):
        nonlocal dispatched_notifier
        candidate = notifier_resolver(binding, credential_ref)
        if candidate is not None and not isinstance(candidate, BoundNotifier):
            raise InboundOwnershipError(
                "delivery resolver returned an unbound notifier"
            )
        dispatched_notifier = candidate
        return candidate

    lease = await delivery.start_delivery_dispatch(
        session,
        prepared,
        notifier_resolver=_resolve_current_notifier,
    )
    if lease is not None and not await is_current():
        # Roll back DISPATCHING so the committed durable claim remains PENDING.
        await session.rollback()
        return None
    await session.commit()
    if lease is None:
        return None

    completion = await delivery.dispatch_delivery(lease)
    for attempt in range(2):
        try:
            journal = await delivery.finalize_delivery(session, completion)
            await session.commit()
            if journal is None:
                return None
            if dispatched_notifier is None:
                raise InboundOwnershipError(
                    "sent delivery lost its resolved notifier"
                )
            return _DeliveredRaw(journal=journal, notifier=dispatched_notifier)
        except Exception:
            await session.rollback()
            if attempt == 0:
                continue

    # A commit can succeed server-side and still raise to the client. Read back
    # only the exact validated terminal claim; the network capability is already
    # consumed and is never recreated or dispatched again.
    try:
        claim = await delivery.delivery_claim_for_raw(
            session,
            raw_payload_id=raw_payload_id,
            category=category,
            ownership=ownership,
        )
        if claim is not None and claim.status == NotificationDeliveryStatus.SENT.value:
            journal = await session.scalar(
                select(Notification).where(
                    Notification.delivery_intent_id == claim.id
                )
            )
            if journal is None:
                raise InboundOwnershipError(
                    "sent delivery claim has no linked journal"
                )
            await session.commit()
            if dispatched_notifier is None:
                raise InboundOwnershipError(
                    "sent delivery lost its resolved notifier"
                )
            return _DeliveredRaw(journal=journal, notifier=dispatched_notifier)
        if (
            claim is not None
            and claim.status == NotificationDeliveryStatus.AMBIGUOUS.value
        ):
            await session.commit()
            return None
        await session.rollback()
    except Exception:
        await session.rollback()
    raise InboundOwnershipError("delivery finalization could not be confirmed") from None


async def _deliver_owned_signal_echo(
    session: AsyncSession,
    notifier: Notifier | None,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    text: str,
    processed: bool,
    ai_invocation_id: uuid.UUID | None,
    buttons=None,
    reply_to: str | None = None,
    rearm_stale_before: datetime | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
    preparation_scope: delivery.DeliveryPreparationScope | None = None,
    recovery_progress: _RawDeliveryRecoveryProgress | None = None,
) -> Notification | None:
    async def _is_current() -> bool:
        return await _raw_delivery_is_current(
            session,
            ownership=ownership,
            raw_payload_id=raw_payload_id,
            ai_invocation_id=ai_invocation_id,
            ai_purpose=(
                AIInvocationPurpose.SIGNAL_PARSE
                if ai_invocation_id is not None
                else None
            ),
            expected_processed=processed,
            reject_misparse=True,
        )

    delivered = await _deliver_owned_raw(
        session,
        notifier,
        ownership=ownership,
        raw_payload_id=raw_payload_id,
        text=text,
        category=delivery.CATEGORY_ECHO,
        idempotency_key=delivery.make_delivery_idempotency_key(
            "telegram-signal-echo",
            raw_payload_id,
        ),
        ai_invocation_id=ai_invocation_id,
        buttons=buttons,
        reply_to=reply_to,
        is_current=_is_current,
        rearm_stale_before=rearm_stale_before,
        notifier_resolver=notifier_resolver,
        preparation_scope=preparation_scope,
        recovery_progress=recovery_progress,
    )
    if delivered is None:
        return None
    journal = delivered.journal

    try:
        still_current = await _is_current()
        await session.commit()
    except Exception:
        await session.rollback()
        still_current = False
        logger.warning(
            "could not revalidate a delivered signal echo (code=revalidation_failed)",
        )
    if still_current:
        return journal
    try:
        await delivered.notifier.edit(
            journal.external_id,
            t("telegram.signal_echo_superseded"),
            buttons=None,
        )
    except Exception:
        logger.warning(
            "could not neutralize a superseded signal echo (code=edit_failed)",
        )
    return journal


def _signal_echo_payload(
    *,
    processed: bool,
    rows: tuple[Signal, ...] | list[Signal],
) -> tuple[str, list[tuple[str, str]] | None]:
    if not processed:
        return _PARSER_PENDING_REPLY, None
    if not rows:
        return _NO_SIGNAL_FACTS_REPLY, None
    return (
        render_echo(list(rows)),
        [("не то", f"{CB_MISPARSE}{rows[0].batch_id}")],
    )


async def _platform_signal_t1_state(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    invocation_id: uuid.UUID,
) -> str:
    """Read back a failed/ambiguous composed T1 without recreating provider I/O."""

    claim = await delivery.delivery_claim_for_raw(
        session,
        raw_payload_id=raw_payload_id,
        category=delivery.CATEGORY_ECHO,
        ownership=ownership,
    )
    raw = await session.get(RawPayload, raw_payload_id, populate_existing=True)
    if raw is None:
        raise InboundOwnershipError("platform signal completion raw disappeared")
    await _validate_raw_root(
        session,
        raw,
        ownership=ownership,
        lock_connection=False,
        allow_historical_null_actor_connection=True,
    )
    invocation = (
        await session.execute(
            select(
                AIInvocation.subject_id,
                AIInvocation.actor_user_id,
                AIInvocation.raw_payload_id,
                AIInvocation.purpose,
                AIInvocation.source,
                AIInvocation.status,
            ).where(AIInvocation.id == invocation_id)
        )
    ).one_or_none()
    if invocation is None:
        raise InboundOwnershipError("platform signal invocation disappeared")
    subject_id, actor_id, linked_raw_id, purpose, source, status = invocation
    if (
        subject_id != ownership.subject_id
        or actor_id != ownership.recipient_user_id
        or linked_raw_id != raw_payload_id
        or purpose != AIInvocationPurpose.SIGNAL_PARSE.value
        or source != AIInvocationSource.TELEGRAM.value
        or status not in {item.value for item in AIInvocationStatus}
    ):
        raise InboundOwnershipError(
            "platform signal completion provenance is invalid"
        )
    signal_count = int(
        await session.scalar(
            select(func.count()).select_from(Signal).where(
                Signal.raw_id == raw_payload_id
            )
        )
        or 0
    )
    if status == AIInvocationStatus.DISPATCHING.value:
        if claim is None and raw.processed_at is None and signal_count == 0:
            return "retryable"
        if claim is not None:
            # A concurrent no-claim duplicate may have won the one bounded
            # parser-pending echo while this provider call was in flight.
            return "authoritative"
        raise InboundOwnershipError(
            "dispatching signal invocation has unexpected domain state"
        )
    if status in {
        AIInvocationStatus.SUCCEEDED.value,
        AIInvocationStatus.FAILED.value,
        AIInvocationStatus.AMBIGUOUS.value,
    }:
        if claim is not None:
            if claim.ai_invocation_id != invocation_id:
                raise InboundOwnershipError(
                    "platform signal intent has different AI provenance"
                )
            return "authoritative"
        if raw.processed_at is not None:
            # The completion committed as stale after a newer edit/domain winner.
            return "authoritative"
        raise InboundOwnershipError(
            "terminal signal invocation has no atomic delivery claim"
        )
    raise InboundOwnershipError(
        "platform signal invocation has non-terminal completion state"
    )


async def _persist_and_deliver_platform_signal(
    session: AsyncSession,
    *,
    prepared_parse: signal_ai_service.PreparedSignalParse,
    completion,
    notifier: Notifier | None,
    ownership: ProactiveOwnershipContext,
    message_id: Any | None,
    notifier_resolver: BoundNotifierResolver | None,
) -> None:
    """Retry the same memory-only AI completion once; never call its provider twice."""

    if notifier is not None and not isinstance(notifier, BoundNotifier):
        raise InboundOwnershipError(
            "owned delivery requires an exact bound notifier"
        )
    invocation_id = completion.invocation_id
    raw_payload_id = prepared_parse.raw_payload_id
    for attempt in range(2):
        try:
            preparation_scope = (
                await delivery.lock_delivery_preparation_scope(
                    session,
                    notifier,
                    category=delivery.CATEGORY_ECHO,
                    ownership=ownership,
                )
                if notifier is not None
                else None
            )
            parse_result = await signal_ai_service.persist_signal_parse(
                session,
                prepared_parse,
                completion,
            )
            if parse_result.stale or notifier is None:
                await session.commit()
                return
            echo_text, buttons = _signal_echo_payload(
                processed=parse_result.processed,
                rows=parse_result.signals,
            )
            terminal_invocation_id = (
                parse_result.invocation_id
                if parse_result.status
                in {
                    AIInvocationStatus.SUCCEEDED,
                    AIInvocationStatus.FAILED,
                    AIInvocationStatus.AMBIGUOUS,
                }
                else None
            )
            await _deliver_owned_signal_echo(
                session,
                notifier,
                ownership=ownership,
                raw_payload_id=raw_payload_id,
                text=echo_text,
                processed=parse_result.processed,
                ai_invocation_id=terminal_invocation_id,
                buttons=buttons,
                reply_to=str(message_id) if message_id else None,
                notifier_resolver=notifier_resolver,
                preparation_scope=preparation_scope,
            )
            return
        except Exception:
            await session.rollback()
            try:
                state = await _platform_signal_t1_state(
                    session,
                    ownership=ownership,
                    raw_payload_id=raw_payload_id,
                    invocation_id=invocation_id,
                )
            except Exception:
                await session.rollback()
                raise InboundOwnershipError(
                    "platform signal completion outcome could not be confirmed"
                ) from None
            await session.rollback()
            if state == "authoritative":
                return
            if state == "retryable" and attempt == 0:
                continue
            raise InboundOwnershipError(
                "platform signal completion could not be committed"
            ) from None


def _same_message_versions(
    raw: RawPayload,
    candidates: list[RawPayload],
) -> list[RawPayload]:
    identity = _telegram_message_identity(raw)
    if identity is None:
        return []
    return [
        candidate
        for candidate in candidates
        if _telegram_message_identity(candidate) == identity
    ]


async def _supersede_edited_message(
    session: AsyncSession,
    raw: RawPayload,
    *,
    ownership: ProactiveOwnershipContext,
    allow_subject_adopted_unowned: bool = False,
    allow_historical_null_actor_connection: bool = False,
) -> int:
    """Deactivate facts from earlier versions of the same Telegram message."""

    message, edited = _message_from_raw(raw)
    message_id = message.get("message_id")
    if not edited or message_id is None:
        return 0

    await _lock_subject_root(session, ownership=ownership)
    candidates = list(
        await session.scalars(
            select(RawPayload)
            .where(
                _owned_or_legacy_raw_scope(ownership),
                RawPayload.domain == DOMAIN,
                RawPayload.source == SOURCE,
                RawPayload.id != raw.id,
            )
            .order_by(RawPayload.id)
        )
    )
    versions = _same_message_versions(raw, candidates)
    prior = [
        candidate for candidate in versions if _is_prior_message_version(candidate, raw)
    ]
    later = [
        candidate for candidate in versions if _is_prior_message_version(raw, candidate)
    ]
    prior_ids = [candidate.id for candidate in prior]
    connection_ids = sorted(
        {
            candidate.integration_connection_id
            for candidate in [raw, *prior, *later]
            if candidate.integration_connection_id is not None
        },
        key=str,
    )
    for connection_id in connection_ids:
        await _load_telegram_connection(
            session,
            connection_id=connection_id,
            subject_id=ownership.subject_id,
            allow_historical=True,
        )
    locked = list(
        await session.scalars(
            select(RawPayload)
            .where(
                RawPayload.id.in_(
                    [raw.id, *prior_ids, *(candidate.id for candidate in later)]
                )
            )
            .order_by(RawPayload.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    current = next((candidate for candidate in locked if candidate.id == raw.id), None)
    if current is None:
        raise InboundOwnershipError("edited Telegram raw disappeared")
    for candidate in locked:
        await _validate_raw_root(
            session,
            candidate,
            ownership=ownership,
            lock_connection=False,
            allow_subject_adopted_unowned=allow_subject_adopted_unowned,
            allow_historical_null_actor_connection=(
                allow_historical_null_actor_connection
            ),
        )
    if current.processed_at is not None or later:
        # A later edit already superseded this version while it was waiting, or
        # was durably claimed first because Telegram deliveries overlapped.
        current.processed_at = current.processed_at or now_local()
        await session.flush()
        await session.commit()
        raise signals_service.RawPayloadAlreadyProcessedError(
            "edited Telegram raw was superseded by a later version"
        )
    if not prior_ids:
        # Release the subject/current-raw locks before the parser/network.
        await session.commit()
        return 0
    timestamp = now_local()
    for candidate in locked:
        if candidate.id == current.id:
            continue
        candidate.processed_at = candidate.processed_at or timestamp
    result = await session.execute(
        sql_update(Signal)
        .where(
            Signal.raw_id.in_(prior_ids),
            or_(
                Signal.subject_id == ownership.subject_id,
                and_(
                    ownership.include_legacy_unowned,
                    Signal.subject_id.is_(None),
                    Signal.actor_user_id.is_(None),
                    Signal.integration_connection_id.is_(None),
                ),
            ),
        )
        .values(misparse=True)
    )
    await session.flush()
    changed = result.rowcount or 0
    # Release S→C→raw locks before parser/Telegram network awaits. The current
    # edited raw is already durable, so a later failure can safely recover.
    await session.commit()
    return changed


async def handle_text(
    session: AsyncSession,
    text: str,
    *,
    notifier: Optional[Notifier],
    external_id: Optional[str] = None,
    message_id: Optional[Any] = None,
    reply_to_message_id: Optional[Any] = None,
    parse: Optional[signals_service.Parser] = None,
    on_date: Optional[date_type] = None,
    ownership: ProactiveOwnershipContext | None = None,
    raw: RawPayload | None = None,
    edited: bool = False,
    parser_alert_context: alerts_service.ProviderAlertContext | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> None:
    """The channel-agnostic entry point (C8): already text, whatever produced it."""
    if ownership is not None and not isinstance(
        ownership, ProactiveOwnershipContext
    ):
        raise TypeError("ownership must be a ProactiveOwnershipContext or None")
    if ownership is None:
        raise InboundOwnershipError(
            "an incoming message belongs to somebody; name the subject"
        )
    owner_identity = ownership.owner_action()
    connection_id = (
        ownership.connection_id if ownership is not None else None
    )
    if raw is not None:
        if ownership is None:
            raise TypeError("a claimed raw requires proactive ownership")
        await _validate_raw_root(
            session,
            raw,
            ownership=ownership,
            lock_connection=False,
        )
        if edited:
            # An edit can turn a fact into a command or question. Supersede the
            # old normalized batch before classifying the replacement text.
            try:
                await _supersede_edited_message(
                    session,
                    raw,
                    ownership=ownership,
                )
            except signals_service.RawPayloadAlreadyProcessedError:
                return
    # A slash command is addressed to the bot, not a fact about the day. Caught
    # before anything else because ``/start`` is the very first thing anyone sends
    # a new bot: capturing it costs a model call and answers "разобрать не смог",
    # which reads as broken on the first ever message.
    if text.startswith("/"):
        if ownership is None:
            # Preserve the zero-subject injected compatibility path. It has no
            # durable delivery authority and therefore keeps the former local
            # raw+direct-send transaction behavior.
            if raw is not None:
                raise TypeError("a claimed raw requires proactive ownership")
            raw = await signals_service.store_raw_text(
                session,
                text=text,
                external_id=external_id,
                source=SOURCE,
                processed=True,
                identity=owner_identity,
                integration_connection_id=connection_id,
            )
            await delivery.send(
                session,
                notifier,
                text=COMMAND_REPLY,
                category=delivery.CATEGORY_REPLY,
                reply_to=str(message_id) if message_id else None,
                ownership=None,
            )
        else:
            if notifier is not None and not isinstance(notifier, BoundNotifier):
                raise InboundOwnershipError(
                    "owned delivery requires an exact bound notifier"
                )
            if raw is None:
                raw = await signals_service.store_raw_text(
                    session,
                    text=text,
                    external_id=external_id,
                    source=SOURCE,
                    processed=False,
                    identity=owner_identity,
                    integration_connection_id=connection_id,
                )
                # Raw-first remains durable before any composed outbound T1.
                await session.commit()

            preparation_scope = await delivery.lock_delivery_preparation_scope(
                session,
                notifier,
                category=delivery.CATEGORY_REPLY,
                ownership=ownership,
            )
            if preparation_scope is None:
                await session.rollback()
                return
            if not await _mark_raw_processed(
                session,
                raw,
                ownership=ownership,
                commit_success=False,
            ):
                return

            async def _command_is_current() -> bool:
                return await _raw_delivery_is_current(
                    session,
                    ownership=ownership,
                    raw_payload_id=raw.id,
                    ai_invocation_id=None,
                    ai_purpose=None,
                    expected_processed=True,
                    reject_misparse=False,
                )

            await _deliver_owned_raw(
                session,
                notifier,
                ownership=ownership,
                raw_payload_id=raw.id,
                text=COMMAND_REPLY,
                category=delivery.CATEGORY_REPLY,
                idempotency_key=delivery.make_delivery_idempotency_key(
                    "telegram-command-reply",
                    raw.id,
                ),
                reply_to=str(message_id) if message_id else None,
                is_current=_command_is_current,
                notifier_resolver=notifier_resolver,
                preparation_scope=preparation_scope,
            )
        return

    answered = (
        await delivery.find_sent(
            session,
            str(reply_to_message_id),
            ownership=ownership,
        )
        if reply_to_message_id is not None
        else None
    )
    # The evening block *asks* «как день?», so a reply to it is an answer, not a
    # question — «норм, а ты как?» falls through to capture, question mark and
    # all. Everything else that replies to us, and anything typed as a question
    # on its own, is asked *of* us: «почему hrv просел?» answered with «фактов
    # для графиков тут не нашёл» is the single most broken-looking thing the bot
    # could say.
    to_evening = answered is not None and answered.category == delivery.CATEGORY_EVENING
    if not to_evening and (answered is not None or looks_like_question(text)):
        # A question is data too, and this is also what stops a webhook retry
        # from paying for a second model call on the same question. Marked done
        # in the same breath: a question is not a message waiting to be parsed
        # into signals, so the re-parse sweep must not pick it up and turn «почему
        # пульс низкий?» into a symptom row.
        if raw is None:
            raw = await signals_service.store_raw_text(
                session,
                text=text,
                external_id=external_id,
                source=SOURCE,
                processed=True,
                identity=owner_identity,
                integration_connection_id=connection_id,
            )
        await _answer_reply(
            session,
            text,
            answered,
            notifier=notifier,
            message_id=message_id,
            ownership=ownership,
            raw=raw,
            notifier_resolver=notifier_resolver,
        )
        return

    if parse is None and ownership is not None:
        if raw is None:
            raw = await signals_service.store_raw_text(
                session,
                text=text,
                external_id=external_id,
                source=SOURCE,
                identity=owner_identity,
                integration_connection_id=connection_id,
            )
        if notifier is None:
            # A platform-funded parse is useful here only when its terminal
            # result can atomically claim the exact outbound occurrence. Keep
            # the raw pending so a later authenticated delivery endpoint can
            # recover it without paying for an answer that cannot be sent.
            await session.commit()
            return
        # Classification/raw reads above intentionally precede AI authorization.
        # Close them so T1 begins with governance -> S -> owner -> C -> raw.
        await session.commit()
        prepared_parse = await signal_ai_service.prepare_live_signal_parse(
            session,
            ownership=ownership,
            raw_payload_id=raw.id,
            on_date=on_date or conversation_day(),
        )
        await session.commit()
        parse_result: signal_ai_service.SignalParseResult
        if prepared_parse.fallback is signal_ai_service.SignalParseFallback.ALREADY_PROCESSED:
            return
        if prepared_parse.fallback is signal_ai_service.SignalParseFallback.PENDING:
            # Another authorized worker already owns the provider lease, or a
            # scheduler-owned PREPARED reservation is waiting for its recovery
            # path. An ai=NULL echo here would win raw/category uniqueness and
            # prevent that invocation's terminal T1 from linking its delivery.
            await session.commit()
            return
        if prepared_parse.dispatchable:
            try:
                lease = await signal_ai_service.start_signal_dispatch(
                    session,
                    prepared_parse,
                )
                await session.commit()
            except ai_gateway_service.AIGatewayConfigurationError:
                await session.rollback()
                try:
                    await signal_ai_service.cancel_prepared_signal_parse(
                        session,
                        prepared_parse,
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                parse_result = signal_ai_service.SignalParseResult(
                    invocation_id=None,
                    status=None,
                    processed=False,
                    stale=False,
                    fallback=signal_ai_service.SignalParseFallback.NOT_CONFIGURED,
                )
            else:
                completion = await signal_ai_service.render_signal_parse(
                    prepared_parse,
                    lease,
                )
                await _persist_and_deliver_platform_signal(
                    session,
                    prepared_parse=prepared_parse,
                    completion=completion,
                    notifier=notifier,
                    ownership=ownership,
                    message_id=message_id,
                    notifier_resolver=notifier_resolver,
                )
                try:
                    await signal_ai_service.reconcile_signal_parser_alert(
                        session,
                        ownership=ownership,
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logger.warning(
                        "could not reconcile the platform signal-parser alert "
                        "(code=alert_reconcile_failed)",
                    )
                return
        else:
            parse_result = signal_ai_service.SignalParseResult(
                invocation_id=prepared_parse.invocation_id,
                status=prepared_parse.reservation_status,
                processed=False,
                stale=False,
                fallback=prepared_parse.fallback,
            )
        if parse_result.stale:
            return
        echo_text, buttons = _signal_echo_payload(
            processed=parse_result.processed,
            rows=parse_result.signals,
        )
        terminal_invocation_id = (
            parse_result.invocation_id
            if parse_result.status
            in {
                AIInvocationStatus.SUCCEEDED,
                AIInvocationStatus.FAILED,
                AIInvocationStatus.AMBIGUOUS,
            }
            else None
        )
        await _deliver_owned_signal_echo(
            session,
            notifier,
            ownership=ownership,
            raw_payload_id=prepared_parse.raw_payload_id,
            text=echo_text,
            processed=parse_result.processed,
            ai_invocation_id=terminal_invocation_id,
            buttons=buttons,
            reply_to=str(message_id) if message_id else None,
            notifier_resolver=notifier_resolver,
        )
        try:
            await signal_ai_service.reconcile_signal_parser_alert(
                session,
                ownership=ownership,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning(
                "could not reconcile the platform signal-parser alert "
                "(code=alert_reconcile_failed)",
            )
        return

    parser = parse
    if parser is None:
        if ownership is None:
            raise InboundOwnershipError(
                "the parser's vocabulary is one person's; name the subject"
            )
        parser = make_signal_parser(
            await known_keys(session, subject_id=ownership.subject_id)
        )
    if parser_alert_context is not None and ownership is None:
        raise InboundOwnershipError(
            "parser alert context requires proactive ownership"
        )
    if (
        ownership is not None
        and notifier is not None
        and not isinstance(notifier, BoundNotifier)
    ):
        raise InboundOwnershipError(
            "owned delivery requires an exact bound notifier"
        )

    async def _before_live_parse(
        parse_session: AsyncSession,
        _raw: RawPayload,
    ) -> None:
        if parser_alert_context is not None:
            assert ownership is not None
            await _validate_parser_alert_connection(
                parse_session,
                context=parser_alert_context,
                subject_id=ownership.subject_id,
            )
        # Raw ownership and the exact parser C are frozen. Release every read
        # transaction before the OpenRouter adapter (or injected parser) awaits.
        await parse_session.commit()

    outcome = signals_service.ParserOutcome()
    claimed_raw = raw is not None
    preparation_scope: delivery.DeliveryPreparationScope | None = None
    if raw is None:
        raw = await signals_service.store_raw_text(
            session,
            text=text,
            external_id=external_id,
            source=SOURCE,
            identity=owner_identity,
            integration_connection_id=connection_id,
        )

    async def _before_signal_normalize(
        normalize_session: AsyncSession,
        normalize_raw: RawPayload,
    ) -> None:
        nonlocal preparation_scope
        if ownership is not None and notifier is not None:
            preparation_scope = await delivery.lock_delivery_preparation_scope(
                normalize_session,
                notifier,
                category=delivery.CATEGORY_ECHO,
                ownership=ownership,
            )
            if preparation_scope is None:
                raise InboundOwnershipError(
                    "signal delivery preparation scope is unavailable"
                )
        if claimed_raw:
            assert ownership is not None
            await _lock_pending_raw_for_completion(
                normalize_session,
                normalize_raw,
                ownership=ownership,
            )

    try:
        rows = await signals_service.ingest_stored_text(
            session,
            raw=raw,
            text=text,
            parse=parser,
            on_date=on_date or conversation_day(),
            source=SOURCE,
            identity=owner_identity,
            integration_connection_id=connection_id,
            before_parse=_before_live_parse,
            before_normalize=(
                _before_signal_normalize
                if ownership is not None
                else None
            ),
            parser_outcome=outcome,
        )
    except signals_service.RawPayloadAlreadyProcessedError:
        await session.commit()
        return

    parser_pending = not rows and raw.processed_at is None

    echo_text, buttons = _signal_echo_payload(
        processed=not parser_pending,
        rows=rows,
    )

    if ownership is None:
        await session.commit()
        await delivery.send(
            session,
            notifier,
            text=echo_text,
            category=delivery.CATEGORY_ECHO,
            buttons=buttons,
            reply_to=str(message_id) if message_id else None,
            ownership=None,
        )
        await _reconcile_parser_alert_best_effort(
            session,
            context=parser_alert_context,
            outcome=outcome,
        )
        return

    await _deliver_owned_signal_echo(
        session,
        notifier,
        ownership=ownership,
        raw_payload_id=raw.id,
        text=echo_text,
        processed=not parser_pending,
        ai_invocation_id=None,
        buttons=buttons,
        reply_to=str(message_id) if message_id else None,
        notifier_resolver=notifier_resolver,
        preparation_scope=preparation_scope,
    )
    await _reconcile_parser_alert_best_effort(
        session,
        context=parser_alert_context,
        outcome=outcome,
    )


async def _answer_reply(
    session: AsyncSession,
    question: str,
    answered,
    *,
    notifier: Optional[Notifier],
    message_id: Optional[Any],
    ownership: ProactiveOwnershipContext | None,
    raw: RawPayload | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> None:
    """``answered`` is the message being replied to; for a question typed on its
    own the last few things we said stand in for it."""
    # Compatibility for zero-subject injected parsers: this intentionally keeps
    # the former dependency-injection seam and has no platform-AI authority.
    if ownership is None:
        if answered is not None:
            context = (answered.payload or {}).get("text") or ""
        else:
            context = "\n\n".join(
                text
                for row in await delivery.recent_sent(
                    session,
                    limit=_CONTEXT_MESSAGES,
                    ownership=None,
                )
                if (text := (row.payload or {}).get("text"))
            )
        facts = await _day_facts(session, ownership=None)
        await session.commit()
        try:
            text = await answer_reply(question, context, facts)
        except Exception:
            logger.warning("could not answer a reply (code=provider_error)")
            text = _NO_LLM_REPLY
        prepared_delivery = await delivery._prepare_delivery(
            session,
            notifier,
            text=text or _NO_LLM_REPLY,
            category=delivery.CATEGORY_REPLY,
            reply_to=str(message_id) if message_id else None,
            ownership=None,
        )
        await session.commit()
        if prepared_delivery is None:
            return
        assert notifier is not None
        delivered = await delivery._transmit_prepared_delivery(notifier, prepared_delivery)
        if delivered is not None:
            await delivery._journal_prepared_delivery(
                session, prepared_delivery, external_id=delivered.external_id
            )
            await session.commit()
        return

    if raw is None:
        raise InboundOwnershipError("owned question replies require their claimed raw")
    raw_payload_id = raw.id
    # Every durable state owns this raw/category occurrence. Check it before any
    # paid AI work, and never reconstruct or retransmit a persisted claim.
    await session.commit()
    if await delivery.delivery_claim_for_raw(
        session,
        raw_payload_id=raw_payload_id,
        category=delivery.CATEGORY_REPLY,
        ownership=ownership,
    ) is not None:
        await session.commit()
        return
    await session.commit()
    # A reply has no durable artifact other than the journal.  Never spend a
    # platform-funded attempt when there is no channel on which it can appear.
    if notifier is None:
        if raw.processed_at is None:
            await _mark_raw_processed(session, raw, ownership=ownership)
        await session.commit()
        return
    if not isinstance(notifier, BoundNotifier):
        raise InboundOwnershipError("owned question requires an exact bound notifier")
    # Materialize all composition/delivery reads before T1.  The next transaction
    # acquires governance -> S -> owner -> current Telegram C -> raw, so no
    # notification/digest read lock can invert that order.
    if await question_ai_service.delivery_is_journaled(
        session, raw_payload_id=raw_payload_id, ownership=ownership
    ):
        await session.commit()
        return
    if answered is not None:
        context = (answered.payload or {}).get("text") or ""
    else:
        context = "\n\n".join(
            text
            for row in await delivery.recent_sent(
                session,
                limit=_CONTEXT_MESSAGES,
                ownership=ownership,
            )
            if (text := (row.payload or {}).get("text"))
        )
    facts = await _day_facts(session, ownership=ownership)
    await session.commit()
    result: question_ai_service.QuestionReplyResult | None = None
    prepared_question = None
    try:
        # T1: current subject/owner/Telegram/raw and the in-memory context
        # snapshot are bound to one deterministic reservation before commit.
        prepared_question = await question_ai_service.prepare_live_question_reply(
            session,
            ownership=ownership,
            raw_payload_id=raw_payload_id,
            context=context,
            facts=facts,
        )
        await session.commit()
    except ai_gateway_service.AIQuotaExceededError:
        await session.rollback()
    except question_ai_service.QuestionAIStaleError:
        await session.rollback()
        return
    except (
        ai_gateway_service.AIGatewayConfigurationError,
        question_ai_service.QuestionAIInputError,
    ):
        await session.rollback()
    else:
        if prepared_question.dispatchable:
            try:
                # T2 is deliberately a fresh authorization/charge transaction.
                lease = await question_ai_service.start_question_dispatch(
                    session, prepared_question
                )
                await session.commit()
            except (
                ai_gateway_service.AIGatewayConfigurationError,
                question_ai_service.QuestionAIModuleDisabledError,
            ):
                await session.rollback()
                # A reservation which could not begin has no paid response and
                # may never be rebound to a changed root/model policy.
                try:
                    cancelled = await question_ai_service.cancel_prepared_question_reply(
                        session, prepared_question
                    )
                    await session.commit()
                    result = question_ai_service.QuestionReplyResult(
                        invocation_id=cancelled.id,
                        status=AIInvocationStatus.CANCELLED,
                    )
                except Exception:
                    await session.rollback()
            except ai_gateway_service.AIGatewayError:
                await session.rollback()
            else:
                completion = await question_ai_service.render_question_reply(
                    prepared_question, lease
                )
                try:
                    # T3 finalizes sanitized accounting before any delivery work.
                    result = await question_ai_service.persist_question_reply(
                        session, prepared_question, completion
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logger.warning("could not finalize Telegram question reply")
        else:
            # Recovery never sends a reconstructed successful answer: the answer
            # was not persisted and an inherited PREPARED capability is not safe
            # to dispatch after conversation context may have changed.
            if (
                prepared_question.invocation_id is not None
                and await question_ai_service.invocation_is_journaled(
                    session,
                    invocation_id=prepared_question.invocation_id,
                    ownership=ownership,
                )
            ):
                await session.commit()
                return
            result = question_ai_service.recovered_terminal_result(prepared_question)
            await session.commit()

    if (
        result is None
        and prepared_question is not None
        and prepared_question.reservation_status
        in {AIInvocationStatus.PREPARED, AIInvocationStatus.DISPATCHING}
    ):
        # An inherited capability cannot be safely dispatched, and neither state
        # is a terminal result that delivery may represent.  Gateway
        # reconciliation eventually changes it to CANCELLED/AMBIGUOUS.
        return

    if result is not None and result.stale:
        return
    if prepared_question is None:
        # A local configuration/quota/input failure rolled T1 back, including
        # its atomic processed marker. Persist that classification before the
        # deterministic no-AI fallback so signal recovery cannot reinterpret the
        # question as a health fact.
        pending_raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_payload_id)
            .execution_options(populate_existing=True)
        )
        if pending_raw is None:
            raise InboundOwnershipError("question raw disappeared before fallback")
        if pending_raw.processed_at is None:
            await _mark_raw_processed(
                session,
                pending_raw,
                ownership=ownership,
            )
        await session.commit()
    if result is not None and result.status is AIInvocationStatus.SUCCEEDED:
        text = result.text or _NO_LLM_REPLY
    else:
        # Configuration/quota/cancelled paths have no terminal invocation;
        # failed/ambiguous paths do, and are journaled with it below.
        text = _NO_LLM_REPLY
    invocation_id = (
        result.invocation_id
        if result is not None
        and result.status
        in {
            AIInvocationStatus.SUCCEEDED,
            AIInvocationStatus.FAILED,
            AIInvocationStatus.AMBIGUOUS,
            AIInvocationStatus.CANCELLED,
        }
        else None
    )
    async def _question_is_current() -> bool:
        return await _raw_delivery_is_current(
            session,
            ownership=ownership,
            raw_payload_id=raw_payload_id,
            ai_invocation_id=invocation_id,
            ai_purpose=(
                AIInvocationPurpose.QUESTION_REPLY
                if invocation_id is not None
                else None
            ),
            expected_processed=True,
            reject_misparse=False,
        )

    delivered = await _deliver_owned_raw(
        session,
        notifier,
        ownership=ownership,
        raw_payload_id=raw_payload_id,
        text=text,
        category=delivery.CATEGORY_REPLY,
        idempotency_key=question_ai_service.delivery_dedupe_key(raw_payload_id),
        legacy_dedupe_key=question_ai_service.legacy_delivery_dedupe_key(
            raw_payload_id
        ),
        reply_to=str(message_id) if message_id else None,
        ai_invocation_id=invocation_id,
        redact_journal_content=True,
        is_current=_question_is_current,
        notifier_resolver=notifier_resolver,
    )
    if delivered is None:
        return
    journal = delivered.journal

    # The exact physical send is terminal and journaled before a later edit can
    # neutralize it. The journal remains the redacted original-send record.
    try:
        still_current = await _question_is_current()
        still_enabled = await prefs.bot_enabled(
            session,
            subject_id=ownership.subject_id,
            strict=True,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        still_enabled = False
        still_current = False
        logger.warning(
            "could not revalidate a delivered Telegram question reply"
        )
    if not (still_enabled and still_current):
        try:
            await delivered.notifier.edit(
                journal.external_id,
                t("telegram.question_reply_withdrawn"),
                buttons=None,
            )
        except Exception:
            # The durable outbound-intent/withdrawal guarantee is PR09. Avoid
            # traceback logging because a transport exception may retain the
            # original memory-only answer request.
            logger.warning(
                "could not withdraw a stale Telegram question reply"
            )


async def _claimed_raw_delivery(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    ownership: ProactiveOwnershipContext,
) -> _ClaimedRawDelivery | None:
    claims: list[NotificationDeliveryIntent] = []
    for category in (delivery.CATEGORY_REPLY, delivery.CATEGORY_ECHO):
        claim = await delivery.delivery_claim_for_raw(
            session,
            raw_payload_id=raw_payload_id,
            category=category,
            ownership=ownership,
        )
        if claim is not None:
            claims.append(claim)
    if len(claims) > 1:
        raise InboundOwnershipError(
            "Telegram raw has conflicting reply and echo delivery claims"
        )
    if not claims:
        return None
    claim = claims[0]
    return _ClaimedRawDelivery(
        category=claim.category,
        status=claim.status,
        ai_invocation_id=claim.ai_invocation_id,
        error_code=claim.error_code,
    )


def _raw_reply_target(raw: RawPayload) -> str | None:
    message, _edited = _message_from_raw(raw)
    message_id = message.get("message_id")
    return str(message_id) if message_id is not None else None


async def _recovered_question_is_current(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    ai_invocation_id: uuid.UUID | None,
) -> bool:
    return await _raw_delivery_is_current(
        session,
        ownership=ownership,
        raw_payload_id=raw_payload_id,
        ai_invocation_id=ai_invocation_id,
        ai_purpose=(
            AIInvocationPurpose.QUESTION_REPLY
            if ai_invocation_id is not None
            else None
        ),
        expected_processed=True,
        reject_misparse=False,
    )


async def _withdraw_recovered_question_if_stale(
    session: AsyncSession,
    notifier: BoundNotifier,
    journal: Notification,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    ai_invocation_id: uuid.UUID | None,
) -> None:
    try:
        still_current = await _recovered_question_is_current(
            session,
            ownership=ownership,
            raw_payload_id=raw_payload_id,
            ai_invocation_id=ai_invocation_id,
        )
        still_enabled = await prefs.bot_enabled(
            session,
            subject_id=ownership.subject_id,
            strict=True,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        still_current = False
        still_enabled = False
        logger.warning(
            "could not revalidate a recovered Telegram question reply "
            "(code=revalidation_failed)"
        )
    if still_current and still_enabled:
        return
    try:
        await notifier.edit(
            journal.external_id,
            t("telegram.question_reply_withdrawn"),
            buttons=None,
        )
    except Exception:
        logger.warning(
            "could not withdraw a recovered Telegram question reply "
            "(code=edit_failed)"
        )


async def _validate_recovered_signal_rows(
    session: AsyncSession,
    *,
    raw: RawPayload,
    ownership: ProactiveOwnershipContext,
) -> list[Signal]:
    rows = list(
        await session.scalars(
            select(Signal)
            .where(Signal.raw_id == raw.id)
            .order_by(Signal.id)
            .execution_options(populate_existing=True)
        )
    )
    batch_ids = {row.batch_id for row in rows}
    if len(batch_ids) > 1:
        raise InboundOwnershipError("recovered signal echo has multiple batches")
    if any(
        row.subject_id != ownership.subject_id
        or row.actor_user_id not in {None, ownership.recipient_user_id}
        or row.integration_connection_id != raw.integration_connection_id
        or row.domain != Domain.SIGNALS.value
        or row.source != SOURCE
        or row.misparse
        for row in rows
    ):
        raise InboundOwnershipError("recovered signal echo provenance is invalid")
    return rows


async def _recover_stale_raw_delivery(
    session: AsyncSession,
    *,
    raw: RawPayload,
    claim: _ClaimedRawDelivery,
    notifier: Notifier | None,
    ownership: ProactiveOwnershipContext,
    stale_before: datetime,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> bool:
    """Re-arm one stale pre-network claim from deterministic durable domain state."""

    if notifier is not None and not isinstance(notifier, BoundNotifier):
        raise InboundOwnershipError("delivery recovery requires an exact bound notifier")
    recoverable_cancel_codes = {
        NotificationDeliveryErrorCode.STALE_PENDING.value,
        NotificationDeliveryErrorCode.SCOPE_INVALID.value,
    }
    if claim.status not in {
        NotificationDeliveryStatus.PENDING.value,
        NotificationDeliveryStatus.CANCELLED.value,
    } or (
        claim.status == NotificationDeliveryStatus.CANCELLED.value
        and claim.error_code not in recoverable_cancel_codes
    ):
        await session.commit()
        return False

    raw = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == raw.id)
        .execution_options(populate_existing=True)
    )
    if raw is None:
        raise InboundOwnershipError("delivery recovery raw disappeared")
    await _validate_raw_root(session, raw, ownership=ownership)
    message, _edited = _message_from_raw(raw)
    raw_text = str(message.get("text") or "").strip()
    reply_to = _raw_reply_target(raw)
    ai_invocation_id = claim.ai_invocation_id
    recovery_progress = _RawDeliveryRecoveryProgress()

    if claim.category == delivery.CATEGORY_REPLY:
        if raw_text.startswith("/"):
            if ai_invocation_id is not None:
                raise InboundOwnershipError("command delivery has AI provenance")

            async def _command_is_current() -> bool:
                return await _raw_delivery_is_current(
                    session,
                    ownership=ownership,
                    raw_payload_id=raw.id,
                    ai_invocation_id=None,
                    ai_purpose=None,
                    expected_processed=True,
                    reject_misparse=False,
                )

            await session.commit()
            delivered = await _deliver_owned_raw(
                session,
                notifier,
                ownership=ownership,
                raw_payload_id=raw.id,
                text=COMMAND_REPLY,
                category=delivery.CATEGORY_REPLY,
                idempotency_key=delivery.make_delivery_idempotency_key(
                    "telegram-command-reply",
                    raw.id,
                ),
                reply_to=reply_to,
                is_current=_command_is_current,
                rearm_stale_before=stale_before,
                notifier_resolver=notifier_resolver,
                recovery_progress=recovery_progress,
            )
            journal = delivered.journal if delivered is not None else None
        else:
            reply_message = message.get("reply_to_message")
            reply_message = reply_message if isinstance(reply_message, dict) else {}
            reply_id = reply_message.get("message_id")
            answered = (
                await delivery.find_sent(
                    session,
                    str(reply_id),
                    ownership=ownership,
                )
                if reply_id is not None
                else None
            )
            if (
                answered is not None
                and answered.category == delivery.CATEGORY_EVENING
            ) or (answered is None and not looks_like_question(raw_text)):
                raise InboundOwnershipError(
                    "question delivery raw no longer classifies as a question"
                )

            async def _question_is_current() -> bool:
                return await _recovered_question_is_current(
                    session,
                    ownership=ownership,
                    raw_payload_id=raw.id,
                    ai_invocation_id=ai_invocation_id,
                )

            await session.commit()
            delivered = await _deliver_owned_raw(
                session,
                notifier,
                ownership=ownership,
                raw_payload_id=raw.id,
                text=_NO_LLM_REPLY,
                category=delivery.CATEGORY_REPLY,
                idempotency_key=question_ai_service.delivery_dedupe_key(raw.id),
                legacy_dedupe_key=question_ai_service.legacy_delivery_dedupe_key(
                    raw.id
                ),
                reply_to=reply_to,
                ai_invocation_id=ai_invocation_id,
                redact_journal_content=True,
                is_current=_question_is_current,
                rearm_stale_before=stale_before,
                notifier_resolver=notifier_resolver,
                recovery_progress=recovery_progress,
            )
            journal = delivered.journal if delivered is not None else None
            if delivered is not None:
                await _withdraw_recovered_question_if_stale(
                    session,
                    delivered.notifier,
                    delivered.journal,
                    ownership=ownership,
                    raw_payload_id=raw.id,
                    ai_invocation_id=ai_invocation_id,
                )
    elif claim.category == delivery.CATEGORY_ECHO:
        if not await _raw_text_is_signal_candidate(
            session,
            raw,
            ownership=ownership,
        ):
            raise InboundOwnershipError(
                "echo delivery raw no longer classifies as a signal"
            )
        processed = raw.processed_at is not None
        rows = await _validate_recovered_signal_rows(
            session,
            raw=raw,
            ownership=ownership,
        )
        if rows and not processed:
            raise InboundOwnershipError("pending signal raw already has normalized facts")
        if not processed:
            echo_text = _PARSER_PENDING_REPLY
            buttons = None
        elif not rows:
            echo_text = _NO_SIGNAL_FACTS_REPLY
            buttons = None
        else:
            echo_text = render_echo(rows)
            buttons = [("не то", f"{CB_MISPARSE}{rows[0].batch_id}")]
        await session.commit()
        journal = await _deliver_owned_signal_echo(
            session,
            notifier,
            ownership=ownership,
            raw_payload_id=raw.id,
            text=echo_text,
            processed=processed,
            ai_invocation_id=ai_invocation_id,
            buttons=buttons,
            reply_to=reply_to,
            rearm_stale_before=stale_before,
            notifier_resolver=notifier_resolver,
            recovery_progress=recovery_progress,
        )
    else:
        raise InboundOwnershipError("raw delivery recovery category is invalid")

    if journal is not None:
        return True
    return recovery_progress.claimed_work


async def _recover_existing_raw_delivery(
    session: AsyncSession,
    *,
    raw: RawPayload,
    notifier: Notifier | None,
    ownership: ProactiveOwnershipContext,
    stale_before: datetime | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> tuple[bool, bool]:
    claim = await _claimed_raw_delivery(
        session,
        raw_payload_id=raw.id,
        ownership=ownership,
    )
    await session.commit()
    if claim is None:
        return False, False
    if claim.status not in {
        NotificationDeliveryStatus.PENDING.value,
        NotificationDeliveryStatus.CANCELLED.value,
    }:
        return True, False
    recovered = await _recover_stale_raw_delivery(
        session,
        raw=raw,
        claim=claim,
        notifier=notifier,
        ownership=ownership,
        notifier_resolver=notifier_resolver,
        stale_before=(
            stale_before
            if stale_before is not None
            else now_utc().astimezone(timezone.utc) - delivery.PENDING_STALE_AFTER
        ),
    )
    return True, recovered


async def _raw_recovery_state(
    session: AsyncSession,
    *,
    raw: RawPayload,
    notifier: Optional[Notifier],
    ownership: ProactiveOwnershipContext,
    stale_before: datetime | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> _RawRecoveryState:
    claimed, recovered = await _recover_existing_raw_delivery(
        session,
        raw=raw,
        notifier=notifier,
        ownership=ownership,
        stale_before=stale_before,
        notifier_resolver=notifier_resolver,
    )
    if claimed:
        return (
            _RawRecoveryState.RECOVERED
            if recovered
            else _RawRecoveryState.AUTHORITATIVE
        )
    # A stable journal is a completed lineage, not recovery work. Validate it
    # before classification but do not consume the per-run work budget; this is
    # essential when Redis is unavailable and every scan starts from raw id 0.
    if await question_ai_service.delivery_is_journaled(
        session,
        raw_payload_id=raw.id,
        ownership=ownership,
    ):
        await session.commit()
        return _RawRecoveryState.AUTHORITATIVE
    current = await session.get(RawPayload, raw.id, populate_existing=True)
    if current is None:
        raise InboundOwnershipError("Telegram recovery raw disappeared")
    await _validate_raw_root(
        session,
        current,
        ownership=ownership,
        lock_connection=False,
        allow_historical_null_actor_connection=True,
    )
    return _RawRecoveryState.UNCLAIMED


async def _recover_unclaimed_question(
    session: AsyncSession,
    *,
    raw: RawPayload,
    notifier: Optional[Notifier],
    ownership: ProactiveOwnershipContext,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> bool:
    message, _edited = _message_from_raw(raw)
    question = (message.get("text") or "").strip()
    if not question or question.startswith("/"):
        return False
    if await question_ai_service.raw_is_superseded(
        session, subject_id=ownership.subject_id, raw_payload_id=raw.id
    ):
        await session.commit()
        return False
    reply_message = message.get("reply_to_message")
    if reply_message is None:
        reply_message = {}
    if not isinstance(reply_message, dict):
        raise InboundOwnershipError(
            "Telegram recovery reply provenance is invalid"
        )
    reply_to = reply_message.get("message_id")
    answered = (
        await delivery.find_sent(session, str(reply_to), ownership=ownership)
        if reply_to is not None
        else None
    )
    if answered is not None and answered.category == delivery.CATEGORY_EVENING:
        return False
    if answered is None and not looks_like_question(question):
        return False
    await _answer_reply(
        session,
        question,
        answered,
        notifier=notifier,
        message_id=message.get("message_id"),
        ownership=ownership,
        raw=raw,
        notifier_resolver=notifier_resolver,
    )
    return True


async def _recover_claimed_question(
    session: AsyncSession,
    *,
    raw: RawPayload,
    notifier: Optional[Notifier],
    ownership: ProactiveOwnershipContext,
    stale_before: datetime | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> bool:
    """Resume only question lineage while preserving the historical test seam."""

    state = await _raw_recovery_state(
        session,
        raw=raw,
        notifier=notifier,
        ownership=ownership,
        stale_before=stale_before,
        notifier_resolver=notifier_resolver,
    )
    if state is not _RawRecoveryState.UNCLAIMED:
        return state is _RawRecoveryState.RECOVERED
    return await _recover_unclaimed_question(
        session,
        raw=raw,
        notifier=notifier,
        ownership=ownership,
        notifier_resolver=notifier_resolver,
    )


async def _signal_invocation_gap(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    ownership: ProactiveOwnershipContext,
) -> tuple[str, uuid.UUID | None]:
    rows = list(
        await session.scalars(
            select(AIInvocation)
            .where(
                AIInvocation.raw_payload_id == raw_payload_id,
                AIInvocation.purpose == AIInvocationPurpose.SIGNAL_PARSE.value,
            )
            .order_by(AIInvocation.created_at, AIInvocation.id)
            .execution_options(populate_existing=True)
        )
    )
    live = 0
    for row in rows:
        if (
            row.subject_id != ownership.subject_id
            or row.raw_payload_id != raw_payload_id
            or row.status not in {item.value for item in AIInvocationStatus}
            or not (
                (
                    row.source == AIInvocationSource.TELEGRAM.value
                    and row.actor_user_id == ownership.recipient_user_id
                )
                or (
                    row.source == AIInvocationSource.SCHEDULER.value
                    and row.actor_user_id is None
                )
            )
        ):
            raise InboundOwnershipError(
                "signal recovery invocation provenance is invalid"
            )
        if row.status in {
            AIInvocationStatus.PREPARED.value,
            AIInvocationStatus.DISPATCHING.value,
        }:
            live += 1
    if live > 1:
        raise InboundOwnershipError(
            "signal raw has multiple live parser invocations"
        )
    if not rows:
        return "none", None
    latest = rows[-1]
    if latest.source != AIInvocationSource.TELEGRAM.value:
        return "scheduler", None
    if latest.status == AIInvocationStatus.DISPATCHING.value:
        return "dispatching", latest.id
    if latest.status == AIInvocationStatus.PREPARED.value:
        return "prepared", latest.id
    if latest.status in {
        AIInvocationStatus.SUCCEEDED.value,
        AIInvocationStatus.FAILED.value,
        AIInvocationStatus.AMBIGUOUS.value,
    }:
        return "terminal", latest.id
    return "cancelled", latest.id


async def _recover_claimed_text(
    session: AsyncSession,
    *,
    raw: RawPayload,
    notifier: Optional[Notifier],
    ownership: ProactiveOwnershipContext,
    parse: signals_service.Parser | None = None,
    parser_alert_context: alerts_service.ProviderAlertContext | None = None,
    recover_unclaimed_signals: bool = True,
    stale_before: datetime | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> bool:
    """Recover one canonical stored raw without trusting the retry envelope."""

    state = await _raw_recovery_state(
        session,
        raw=raw,
        notifier=notifier,
        ownership=ownership,
        stale_before=stale_before,
        notifier_resolver=notifier_resolver,
    )
    if state is not _RawRecoveryState.UNCLAIMED:
        return state is _RawRecoveryState.RECOVERED

    raw = await session.get(RawPayload, raw.id, populate_existing=True)
    if raw is None or raw.processed_at is not None:
        if raw is None:
            await session.commit()
            return False
    message, edited = _message_from_raw(raw)
    text = str(message.get("text") or "").strip()
    if not text:
        await session.commit()
        return False
    if text.startswith("/"):
        if raw.processed_at is not None:
            await session.commit()
            return False
        is_signal = False
    else:
        is_signal = await _raw_text_is_signal_candidate(
            session,
            raw,
            ownership=ownership,
        )
    if text.startswith("/") or is_signal:
        gap_state, terminal_invocation_id = await _signal_invocation_gap(
            session,
            raw_payload_id=raw.id,
            ownership=ownership,
        )
        if not is_signal and gap_state != "none":
            raise InboundOwnershipError(
                "command raw has signal-parser invocation provenance"
            )
        if is_signal and gap_state == "dispatching":
            # The paid provider call is still in flight. Creating an ai=NULL
            # pending echo here would win raw/category uniqueness and make its
            # terminal T3 impossible to commit.
            await session.commit()
            return False
        if is_signal and gap_state == "terminal":
            assert terminal_invocation_id is not None
            rows = await _validate_recovered_signal_rows(
                session,
                raw=raw,
                ownership=ownership,
            )
            invocation_status = await session.scalar(
                select(AIInvocation.status).where(
                    AIInvocation.id == terminal_invocation_id
                )
            )
            if (
                invocation_status == AIInvocationStatus.SUCCEEDED.value
                and raw.processed_at is None
            ):
                raise InboundOwnershipError(
                    "successful signal invocation has a pending raw"
                )
            if raw.processed_at is not None and not rows and invocation_status in {
                AIInvocationStatus.FAILED.value,
                AIInvocationStatus.AMBIGUOUS.value,
            }:
                await session.commit()
                return False
            echo_text, buttons = _signal_echo_payload(
                processed=raw.processed_at is not None,
                rows=rows,
            )
            await session.commit()
            await _deliver_owned_signal_echo(
                session,
                notifier,
                ownership=ownership,
                raw_payload_id=raw.id,
                text=echo_text,
                processed=raw.processed_at is not None,
                ai_invocation_id=terminal_invocation_id,
                buttons=buttons,
                reply_to=_raw_reply_target(raw),
                notifier_resolver=notifier_resolver,
            )
            return True
        if is_signal and not recover_unclaimed_signals:
            # The scheduled signal-reparse pipeline owns ordinary pending facts.
            # This scan only repairs an exact terminal AI/no-intent gap; otherwise
            # a long fact backlog would consume the question work budget.
            await session.commit()
            return False
        if is_signal and raw.processed_at is not None:
            # No live Telegram AI lineage means this is historical terminal
            # domain state, not a queue for a retroactive echo.
            await session.commit()
            return False
        if raw.actor_user_id is None and raw.integration_connection_id is not None:
            # A Stage-3A historical root is scheduler-recovery provenance. A
            # duplicate live webhook may discover it, but must not turn that
            # discovery into a live, owner-attributed provider reservation.
            await session.commit()
            return False
        # Classification above is read-only. Start the composed path with a fresh
        # root so its preparation scope is acquired before raw/domain locks.
        await session.commit()
        reply_message = message.get("reply_to_message")
        if reply_message is None:
            reply_message = {}
        if not isinstance(reply_message, dict):
            raise InboundOwnershipError(
                "Telegram recovery reply provenance is invalid"
            )
        await handle_text(
            session,
            text,
            notifier=notifier,
            message_id=message.get("message_id"),
            reply_to_message_id=reply_message.get("message_id"),
            parse=parse,
            on_date=_day_from_raw(raw),
            ownership=ownership,
            raw=raw,
            edited=edited,
            parser_alert_context=parser_alert_context,
            notifier_resolver=notifier_resolver,
        )
        return True
    return await _recover_unclaimed_question(
        session,
        raw=raw,
        notifier=notifier,
        ownership=ownership,
        notifier_resolver=notifier_resolver,
    )


def _question_recovery_cursor_key(subject_id: uuid.UUID) -> str:
    return f"{_QUESTION_RECOVERY_CURSOR_PREFIX}{subject_id}"


async def _load_question_recovery_cursor(
    redis,
    subject_id: uuid.UUID,
) -> tuple[int, bool]:
    if redis is None:
        return 0, False
    try:
        value = await redis.get(_question_recovery_cursor_key(subject_id))
        if isinstance(value, bytes):
            value = value.decode("ascii")
        cursor = int(value or 0)
        return (cursor if cursor >= 0 else 0), True
    except (TypeError, ValueError, UnicodeError):
        logger.warning("invalid Telegram question recovery cursor; restarting scan")
        return 0, False
    except Exception:
        logger.warning("could not read Telegram question recovery cursor")
        return 0, False


async def _store_question_recovery_cursor(
    redis,
    subject_id: uuid.UUID,
    cursor: int,
) -> bool:
    if redis is None:
        return False
    try:
        await redis.set(_question_recovery_cursor_key(subject_id), str(cursor))
        return True
    except Exception:
        # The current worker must stop treating this cursor as durable. It will
        # continue its in-memory keyset walk without the per-run scan cap, so a
        # repeatedly failing Redis write cannot strand pre-invocation raws.
        logger.warning("could not persist Telegram question recovery cursor")
        return False


def _delivery_recovery_cursor_key(subject_id: uuid.UUID) -> str:
    return f"{_DELIVERY_RECOVERY_CURSOR_PREFIX}{subject_id}"


def _delivery_cursor_time(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise InboundOwnershipError("delivery recovery timestamp is invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _load_delivery_recovery_cursor(
    redis,
    subject_id: uuid.UUID,
) -> tuple[tuple[datetime, uuid.UUID] | None, bool]:
    if redis is None:
        return None, False
    try:
        value = await redis.get(_delivery_recovery_cursor_key(subject_id))
        if isinstance(value, bytes):
            value = value.decode("ascii")
        if value in {None, ""}:
            return None, True
        encoded_time, encoded_id = json.loads(value)
        cursor_time = datetime.fromisoformat(encoded_time)
        cursor_id = uuid.UUID(encoded_id)
        return (_delivery_cursor_time(cursor_time), cursor_id), True
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        logger.warning("invalid raw delivery recovery cursor; restarting scan")
        return None, False
    except Exception:
        logger.warning("could not read raw delivery recovery cursor")
        return None, False


async def _store_delivery_recovery_cursor(
    redis,
    subject_id: uuid.UUID,
    cursor: tuple[datetime, uuid.UUID] | None,
) -> bool:
    if redis is None:
        return False
    value = ""
    if cursor is not None:
        value = json.dumps(
            [_delivery_cursor_time(cursor[0]).isoformat(), cursor[1].hex],
            separators=(",", ":"),
        )
    try:
        await redis.set(_delivery_recovery_cursor_key(subject_id), value)
        return True
    except Exception:
        logger.warning("could not persist raw delivery recovery cursor")
        return False


async def _recoverable_raw_delivery_candidates(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    stale_before: datetime,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[_RecoverableDeliveryCandidate]:
    """Select one keyset page of never-dispatched raw delivery claims."""

    if stale_before.tzinfo is None or stale_before.utcoffset() is None:
        raise ValueError("delivery recovery cutoff must be timezone-aware")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("delivery recovery page limit must be between 1 and 1000")
    recoverable_cancel_codes = {
        NotificationDeliveryErrorCode.STALE_PENDING.value,
        NotificationDeliveryErrorCode.SCOPE_INVALID.value,
    }
    predicates = [
        NotificationDeliveryIntent.subject_id == ownership.subject_id,
        NotificationDeliveryIntent.recipient_user_id
        == ownership.recipient_user_id,
        NotificationDeliveryIntent.channel == IntegrationProvider.TELEGRAM.value,
        NotificationDeliveryIntent.raw_payload_id.is_not(None),
        NotificationDeliveryIntent.category.in_(
            (delivery.CATEGORY_REPLY, delivery.CATEGORY_ECHO)
        ),
        or_(
            and_(
                NotificationDeliveryIntent.status
                == NotificationDeliveryStatus.PENDING.value,
                NotificationDeliveryIntent.updated_at < stale_before,
            ),
            and_(
                NotificationDeliveryIntent.status
                == NotificationDeliveryStatus.CANCELLED.value,
                NotificationDeliveryIntent.error_code.in_(recoverable_cancel_codes),
                NotificationDeliveryIntent.completed_at < stale_before,
                NotificationDeliveryIntent.updated_at < stale_before,
            ),
        ),
    ]
    if after is not None:
        after_time, after_id = after
        after_time = _delivery_cursor_time(after_time)
        predicates.append(
            or_(
                NotificationDeliveryIntent.updated_at > after_time,
                and_(
                    NotificationDeliveryIntent.updated_at == after_time,
                    NotificationDeliveryIntent.id > after_id,
                ),
            )
        )
    candidates = list(
        await session.execute(
            select(
                NotificationDeliveryIntent.updated_at,
                NotificationDeliveryIntent.id,
                NotificationDeliveryIntent.raw_payload_id,
            )
            .where(*predicates)
            .order_by(
                NotificationDeliveryIntent.updated_at,
                NotificationDeliveryIntent.id,
            )
            .limit(limit)
        )
    )
    result: list[_RecoverableDeliveryCandidate] = []
    for updated_at, intent_id, raw_payload_id in candidates:
        if (
            not isinstance(intent_id, uuid.UUID)
            or isinstance(raw_payload_id, bool)
            or not isinstance(raw_payload_id, int)
            or raw_payload_id < 1
        ):
            raise InboundOwnershipError(
                "delivery recovery candidate identity is invalid"
            )
        result.append(
            _RecoverableDeliveryCandidate(
                updated_at=_delivery_cursor_time(updated_at),
                intent_id=intent_id,
                raw_payload_id=raw_payload_id,
            )
        )
    return result


async def _unjournaled_question_invocation_raw_ids(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
) -> list[int]:
    """Find every bounded paid/in-flight gap independently of the scan cursor."""

    journal_exists = (
        select(Notification.id)
        .where(Notification.ai_invocation_id == AIInvocation.id)
        .correlate(AIInvocation)
        .exists()
    )
    rows = list(
        await session.execute(
            select(
                AIInvocation.raw_payload_id,
                AIInvocation.actor_user_id,
                AIInvocation.source,
                AIInvocation.status,
            )
            .where(
                AIInvocation.subject_id == ownership.subject_id,
                AIInvocation.purpose == AIInvocationPurpose.QUESTION_REPLY.value,
                ~journal_exists,
            )
            .order_by(AIInvocation.created_at, AIInvocation.id)
            .limit(_QUESTION_RECOVERY_SCAN_LIMIT)
        )
    )
    ranked: list[tuple[int, int]] = []
    seen: set[int] = set()
    terminal = {
        AIInvocationStatus.SUCCEEDED.value,
        AIInvocationStatus.FAILED.value,
        AIInvocationStatus.AMBIGUOUS.value,
        AIInvocationStatus.CANCELLED.value,
    }
    for raw_payload_id, actor_user_id, source, status in rows:
        if (
            isinstance(raw_payload_id, bool)
            or not isinstance(raw_payload_id, int)
            or raw_payload_id < 1
            or actor_user_id != ownership.recipient_user_id
            or source != AIInvocationSource.TELEGRAM.value
            or status not in {item.value for item in AIInvocationStatus}
        ):
            raise InboundOwnershipError(
                "question recovery invocation provenance is invalid"
            )
        if raw_payload_id in seen:
            raise InboundOwnershipError(
                "question raw has multiple recovery invocations"
            )
        seen.add(raw_payload_id)
        priority = 0 if status in terminal else 1
        ranked.append((priority, raw_payload_id))
    ranked.sort()
    return [raw_id for _priority, raw_id in ranked]


async def _run_question_recovery_raw(
    session_factory,
    *,
    raw_payload_id: int,
    stale_before: datetime | None = None,
    notifier_resolver: BoundNotifierResolver | None = None,
    module_enabled: bool | None = None,
) -> bool:
    """Resolve fresh roots for one candidate and keep failures isolated."""

    from vitals.services.proactive import channels

    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=None,
        )
        if module_enabled is None:
            module_enabled = await prefs.bot_enabled(
                session,
                subject_id=ownership.subject_id,
                strict=True,
            )
        elif not isinstance(module_enabled, bool):
            raise TypeError("module_enabled must be a bool or None")
        raw = await session.get(RawPayload, raw_payload_id)
        if raw is None:
            await session.commit()
            return False
        try:
            notifier = await channels.build_legacy_bound_notifier(
                session,
                ownership,
            )
            if notifier is None and module_enabled:
                await session.commit()
                return False
            if not module_enabled:
                claimed, recovered = await _recover_existing_raw_delivery(
                    session,
                    raw=raw,
                    notifier=notifier,
                    ownership=ownership,
                    stale_before=stale_before,
                    notifier_resolver=notifier_resolver,
                )
                return claimed and recovered
            return await _recover_claimed_text(
                session,
                raw=raw,
                notifier=notifier,
                ownership=ownership,
                recover_unclaimed_signals=False,
                stale_before=stale_before,
                notifier_resolver=notifier_resolver,
            )
        except (
            InboundOwnershipError,
            question_ai_service.QuestionAIError,
            delivery.DeliveryError,
            delivery.NotificationOwnershipConflictError,
            delivery.ProactiveOwnershipScopeError,
            channels.NotifierBindingError,
        ):
            await session.rollback()
            logger.warning(
                "skipping invalid Telegram question recovery candidate"
            )
            return False


async def question_reply_recovery_job(
    session_factory,
    redis=None,
    *,
    notifier_resolver: BoundNotifierResolver | None = None,
) -> None:
    """Bounded durable recovery for claimed Telegram questions.

    The worker never re-dispatches an inherited PREPARED invocation and never
    calls a provider for DISPATCHING. Gateway reconciliation moves those states
    to CANCELLED/AMBIGUOUS; a later pass emits the one deduped fallback where
    appropriate. Raw rows with no invocation are safe to classify afresh because
    no T1 prompt snapshot was ever committed.
    """

    from vitals.services.proactive import channels

    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session, actor_username=None
        )
        endpoint_available = (
            await channels.build_legacy_bound_notifier(session, ownership) is not None
        )
        module_enabled = await prefs.bot_enabled(
            session, subject_id=ownership.subject_id, strict=True
        )
        if module_enabled and not endpoint_available:
            await session.commit()
            return
        stale_before = (
            now_utc().astimezone(timezone.utc) - delivery.PENDING_STALE_AFTER
        )
        invocation_gap_ids = (
            await _unjournaled_question_invocation_raw_ids(
                session,
                ownership=ownership,
            )
            if module_enabled
            else []
        )
        raw_high_water_id = (
            await session.scalar(
                select(func.max(RawPayload.id)).where(
                    RawPayload.subject_id == ownership.subject_id,
                    RawPayload.actor_user_id == ownership.recipient_user_id,
                    RawPayload.domain == DOMAIN,
                    RawPayload.source == SOURCE,
                )
            )
            if module_enabled
            else None
        )
        await session.commit()

    work = 0
    processed_ids: set[int] = set()
    delivery_cursor, delivery_cursor_persistent = (
        await _load_delivery_recovery_cursor(redis, ownership.subject_id)
    )
    delivery_scanned = 0
    while (
        work < _QUESTION_RECOVERY_WORK_LIMIT
        and (
            not delivery_cursor_persistent
            or delivery_scanned < _QUESTION_RECOVERY_SCAN_LIMIT
        )
    ):
        page_limit = _QUESTION_RECOVERY_PAGE_SIZE
        if delivery_cursor_persistent:
            page_limit = min(
                page_limit,
                _QUESTION_RECOVERY_SCAN_LIMIT - delivery_scanned,
            )
        async with session_factory() as session:
            page = await _recoverable_raw_delivery_candidates(
                session,
                ownership=ownership,
                stale_before=stale_before,
                after=delivery_cursor,
                limit=page_limit,
            )
            await session.commit()
        if not page:
            if delivery_cursor_persistent:
                delivery_cursor = None
                delivery_cursor_persistent = await _store_delivery_recovery_cursor(
                    redis,
                    ownership.subject_id,
                    None,
                )
            break
        stopped_for_work = False
        for candidate in page:
            delivery_cursor = (candidate.updated_at, candidate.intent_id)
            delivery_scanned += 1
            raw_id = candidate.raw_payload_id
            if raw_id not in processed_ids:
                processed_ids.add(raw_id)
                if await _run_question_recovery_raw(
                    session_factory,
                    raw_payload_id=raw_id,
                    stale_before=stale_before,
                    notifier_resolver=notifier_resolver,
                    module_enabled=module_enabled,
                ):
                    work += 1
            if work >= _QUESTION_RECOVERY_WORK_LIMIT:
                stopped_for_work = True
                break
        if delivery_cursor_persistent:
            delivery_cursor_persistent = await _store_delivery_recovery_cursor(
                redis,
                ownership.subject_id,
                delivery_cursor,
            )
        if stopped_for_work:
            break
        if len(page) < page_limit:
            if delivery_cursor_persistent:
                delivery_cursor = None
                delivery_cursor_persistent = await _store_delivery_recovery_cursor(
                    redis,
                    ownership.subject_id,
                    None,
                )
            break
    if not module_enabled:
        return
    for raw_id in invocation_gap_ids:
        if work >= _QUESTION_RECOVERY_WORK_LIMIT:
            break
        if raw_id in processed_ids:
            continue
        processed_ids.add(raw_id)
        if await _run_question_recovery_raw(
            session_factory,
            raw_payload_id=raw_id,
            notifier_resolver=notifier_resolver,
        ):
            work += 1

    # A process may die after raw capture but before T1 atomically classifies and
    # reserves the question. Walk every Telegram raw by a persistent opaque
    # cursor instead of repeatedly looking only at the newest N rows. Redis loss
    # merely restarts this scan; all paid/in-flight gaps above remain DB-backed.
    cursor, cursor_persistent = await _load_question_recovery_cursor(
        redis,
        ownership.subject_id,
    )
    if raw_high_water_id is None:
        return
    if cursor > raw_high_water_id:
        cursor = 0
        cursor_persistent = await _store_question_recovery_cursor(
            redis,
            ownership.subject_id,
            cursor,
        )
    scanned = 0
    while (
        work < _QUESTION_RECOVERY_WORK_LIMIT
        and (
            not cursor_persistent
            or scanned < _QUESTION_RECOVERY_SCAN_LIMIT
        )
    ):
        async with session_factory() as session:
            page = list(
                await session.scalars(
                    select(RawPayload.id)
                    .where(
                        RawPayload.subject_id == ownership.subject_id,
                        RawPayload.actor_user_id == ownership.recipient_user_id,
                        RawPayload.domain == DOMAIN,
                        RawPayload.source == SOURCE,
                        RawPayload.id > cursor,
                        RawPayload.id <= raw_high_water_id,
                    )
                    .order_by(RawPayload.id)
                    .limit(_QUESTION_RECOVERY_PAGE_SIZE)
                )
            )
            await session.commit()
        if not page:
            break
        for raw_id in page:
            cursor = raw_id
            scanned += 1
            if raw_id in processed_ids:
                continue
            if await _run_question_recovery_raw(
                session_factory,
                raw_payload_id=raw_id,
                notifier_resolver=notifier_resolver,
            ):
                work += 1
                if work >= _QUESTION_RECOVERY_WORK_LIMIT:
                    break
        if cursor_persistent:
            cursor_persistent = await _store_question_recovery_cursor(
                redis,
                ownership.subject_id,
                cursor,
            )
        if len(page) < _QUESTION_RECOVERY_PAGE_SIZE:
            break


async def _day_facts(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext | None = None,
) -> str:
    """The numbers behind the latest brief, as JSON — or ``""`` if there is none.

    Without it the model answers «почему hrv просел?» from the prose of one
    message and nothing else: it cannot see the HRV it is being asked about, so
    the honest answer is always "в тексте этого нет". The brief already assembled
    the day and stored the context it was built from, so this reads *that* rather
    than assembling a second, subtly different picture of the same day.
    """
    if ownership is None:
        digest = await digest_service.latest_digest(
            session,
            kind=DigestKind.DAILY_BRIEF.value,
        )
    else:
        prepared_owner = await digest_service.prepare_digest_owner_for_identity(
            session,
            identity=ownership.system_action(),
            owner_user_id=ownership.recipient_user_id,
        )
        digest = await digest_service.latest_digest(
            session,
            kind=DigestKind.DAILY_BRIEF.value,
            prepared_owner=prepared_owner,
        )
    if digest is None or not digest.context_json:
        return ""
    try:
        return json.dumps(digest.context_json, ensure_ascii=False, default=str)[:_DAY_FACTS_LIMIT]
    except (TypeError, ValueError):
        logger.warning("brief context is not serialisable (code=invalid_context)")
        return ""


def _fmt_num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def render_echo(rows) -> str:
    """What was understood: his own words, the key it was filed under, the value.

    The key used to be left out on purpose — `голова раскалывается → headache 4/5`
    reads like the bot answering in a language he did not use. In practice the
    reverse was worse. The number alone is not checkable: «спать хочу → 3/5» and
    «спать хочу → 3/5» look identical whether the second one went under
    ``sleepiness`` or quietly opened a 61st synonym for it, and the key registry
    the open-vocabulary parser is supposed to converge on drifts unwatched until
    ``/signals`` is opened on purpose. The key is the half that says *where* the
    row landed, which is exactly what an echo is for.
    """
    lines = []
    for row in rows:
        bits = [signals_service.normalize_key(row.key)]
        if row.value_num is not None:
            number = _fmt_num(row.value_num)
            if row.unit:
                bits.append(f"{number} {row.unit}")
            elif row.kind in (SignalKind.STATE.value, SignalKind.SYMPTOM.value):
                bits.append(f"{number}/5")
            else:
                bits.append(number)
        if row.at_time is not None:
            bits.append(f"в {row.at_time.strftime('%H:%M')}")
        parsed = " ".join(bits)
        # No note (the parser found a fact but quoted nothing) leaves the key to
        # name the row on its own — no «→» with nothing on its left.
        lines.append(f"• {row.note} → {parsed}" if row.note else f"• {parsed}")
    return "Записал:\n" + "\n".join(lines)


# ── LLM ───────────────────────────────────────────────────────────────────────
async def known_keys(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> list[str]:
    """The vocabulary the parser is reminded of, most-used first."""
    stats = await signals_service.key_frequency(
        session,
        subject_id=subject_id,
    )
    return [stat.key for stat in stats][:_KNOWN_KEYS_LIMIT]


async def _replay_pending_callbacks(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
) -> int:
    """Recover durable taps whose original request rolled back after capture."""

    base = select(RawPayload).where(
        _owned_or_legacy_raw_scope(ownership),
        RawPayload.domain == DOMAIN,
        RawPayload.source == SOURCE,
        RawPayload.processed_at.is_(None),
        or_(
            RawPayload.payload["callback_query"].as_string().is_not(None),
            RawPayload.payload["data"].as_string().is_not(None),
        ),
    )
    recovered = 0
    last_id = 0
    high_water_id = await session.scalar(
        select(func.max(RawPayload.id)).where(
            _owned_or_legacy_raw_scope(ownership),
            RawPayload.domain == DOMAIN,
            RawPayload.source == SOURCE,
            RawPayload.processed_at.is_(None),
            or_(
                RawPayload.payload["callback_query"].as_string().is_not(None),
                RawPayload.payload["data"].as_string().is_not(None),
            ),
        )
    )
    while recovered < signals_service.REPARSE_BATCH:
        if high_water_id is None:
            break
        page = list(
            await session.scalars(
                base.where(
                    RawPayload.id > last_id,
                    RawPayload.id <= high_water_id,
                )
                .order_by(RawPayload.id)
                .limit(signals_service.REPARSE_BATCH)
            )
        )
        if not page:
            break
        for raw in page:
            last_id = raw.id
            callback = _callback_from_raw(raw)
            if callback is None:
                continue
            try:
                raw = await _lock_pending_raw_for_completion(
                    session,
                    raw,
                    ownership=ownership,
                    allow_historical_null_actor_connection=True,
                )
                replay_ownership = ownership
                if raw.subject_id is not None:
                    replay_ownership = ProactiveOwnershipContext(
                        subject_id=ownership.subject_id,
                        recipient_user_id=ownership.recipient_user_id,
                        connection_id=raw.integration_connection_id,
                        include_legacy_unowned=ownership.include_legacy_unowned,
                    )
                data = str(callback.get("data") or "")
                if data.startswith(CB_MISPARSE):
                    await _mark_subject_batch_misparse(
                        session,
                        data[len(CB_MISPARSE):],
                        ownership=replay_ownership,
                    )
                elif data.startswith(CB_CONTEXT):
                    await _apply_context(
                        session,
                        data,
                        ownership=replay_ownership,
                        historical_connection=True,
                    )
                raw.processed_at = now_local()
                await session.flush()
                await session.commit()
                recovered += 1
            except signals_service.RawPayloadAlreadyProcessedError:
                await session.commit()
            except Exception:
                await session.rollback()
                logger.warning(
                    "could not replay stored Telegram callback "
                    "(code=callback_recovery_failed)",
                )
            if recovered >= signals_service.REPARSE_BATCH:
                break
        if len(page) < signals_service.REPARSE_BATCH:
            break
    # Close the final read transaction too; no subject/raw lock may span the LLM
    # text-recovery phase that follows.
    await session.commit()
    return recovered


async def _reparse_pending_platform(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
) -> list[Signal]:
    """Recover bounded pending Telegram facts through the platform AI gateway."""

    await _replay_pending_callbacks(session, ownership=ownership)
    made: list[Signal] = []
    attempted = 0
    after_id: int | None = None
    high_water_id = await signal_ai_service.signal_recovery_high_water_id(
        session,
        ownership=ownership,
    )
    await session.commit()
    while (
        high_water_id is not None
        and attempted < signals_service.REPARSE_BATCH
    ):
        candidate_ids = await signal_ai_service.pending_signal_recovery_ids(
            session,
            ownership=ownership,
            limit=signals_service.REPARSE_BATCH,
            after_id=after_id,
            through_id=high_water_id,
        )
        # Candidate discovery is deliberately unlocked. Each raw begins again
        # with governance and authoritative root locks below.
        await session.commit()
        if not candidate_ids:
            break
        after_id = candidate_ids[-1]
        for raw_id in candidate_ids:
            try:
                await acquire_identity_governance_lock(session)
                await _lock_subject_root(session, ownership=ownership)
                raw = await session.scalar(
                    select(RawPayload)
                    .where(RawPayload.id == raw_id)
                    .execution_options(populate_existing=True)
                )
                if raw is None:
                    await session.rollback()
                    continue
                raw = await _lock_pending_raw_for_completion(
                    session,
                    raw,
                    ownership=ownership,
                    allow_subject_adopted_unowned=True,
                    allow_historical_null_actor_connection=True,
                )
                await _supersede_edited_message(
                    session,
                    raw,
                    ownership=ownership,
                    allow_subject_adopted_unowned=True,
                    allow_historical_null_actor_connection=True,
                )
                try:
                    signal_ai_service.validate_signal_raw_input(raw)
                except signal_ai_service.SignalAIValidationError:
                    raw.processed_at = now_local()
                    await session.flush()
                    await session.commit()
                    attempted += 1
                    continue
                if not await _raw_text_is_signal_candidate(
                    session,
                    raw,
                    ownership=ownership,
                ):
                    await _mark_raw_processed(
                        session,
                        raw,
                        ownership=ownership,
                        allow_subject_adopted_unowned=True,
                        allow_historical_null_actor_connection=True,
                    )
                    await session.commit()
                    attempted += 1
                    continue
                attempted += 1
                await session.commit()

                prepared = await signal_ai_service.prepare_signal_recovery(
                    session,
                    ownership=ownership,
                    raw_payload_id=raw_id,
                )
                await session.commit()
                if not prepared.dispatchable:
                    if (
                        prepared.fallback
                        is signal_ai_service.SignalParseFallback.INPUT_TOO_LARGE
                    ):
                        current = await session.get(RawPayload, raw_id)
                        if current is not None:
                            await _mark_raw_processed(
                                session,
                                current,
                                ownership=ownership,
                                allow_subject_adopted_unowned=True,
                                allow_historical_null_actor_connection=True,
                            )
                    continue
                try:
                    lease = await signal_ai_service.start_signal_dispatch(
                        session,
                        prepared,
                    )
                    await session.commit()
                except ai_gateway_service.AIGatewayConfigurationError:
                    await session.rollback()
                    try:
                        await signal_ai_service.cancel_prepared_signal_parse(
                            session,
                            prepared,
                        )
                        await session.commit()
                    except Exception:
                        await session.rollback()
                    continue
                completion = await signal_ai_service.render_signal_parse(
                    prepared,
                    lease,
                )
                result = await signal_ai_service.persist_signal_parse(
                    session,
                    prepared,
                    completion,
                )
                await session.commit()
                made.extend(result.signals)
            except signals_service.RawPayloadAlreadyProcessedError:
                await session.commit()
            except Exception:
                await session.rollback()
                logger.warning(
                    "platform signal recovery failed "
                    "(code=signal_recovery_failed)",
                )
            if attempted >= signals_service.REPARSE_BATCH:
                break
        if len(candidate_ids) < signals_service.REPARSE_BATCH:
            break
    try:
        await signal_ai_service.reconcile_signal_parser_alert(
            session,
            ownership=ownership,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.warning(
            "could not reconcile the platform signal-parser alert "
            "(code=alert_reconcile_failed)",
        )
    return made


async def reparse_pending(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext | None = None,
    parse: signals_service.Parser | None = None,
    parser_alert_context: alerts_service.ProviderAlertContext | None = None,
) -> list:
    """Give the messages the parser choked on one more go (R3).

    Lives next to the parser because that is what it needs; called from the
    morning-brief job rather than from a schedule of its own, so a recovered row
    is in the lake *before* the brief reads it.
    """
    if ownership is not None and parse is None:
        return await _reparse_pending_platform(
            session,
            ownership=ownership,
        )
    if ownership is not None:
        await _replay_pending_callbacks(session, ownership=ownership)
    if parse is None and ownership is not None and parser_alert_context is None:
        raise InboundOwnershipError(
            "production signal recovery requires an OpenRouter alert context"
        )
    if parser_alert_context is not None and ownership is None:
        raise InboundOwnershipError(
            "parser alert context requires proactive ownership"
        )
    if ownership is None:
        raise InboundOwnershipError(
            "signal recovery reads one person's messages; name the subject"
        )
    parser = parse or make_signal_parser(
        await known_keys(session, subject_id=ownership.subject_id)
    )
    if parser_alert_context is not None:
        assert ownership is not None
        await _validate_parser_alert_connection(
            session,
            context=parser_alert_context,
            subject_id=ownership.subject_id,
        )
    # Known-key and provider-root reads must not span the first parser await.
    await session.commit()

    async def _before_reparse(
        reparse_session: AsyncSession,
        raw: RawPayload,
    ) -> None:
        if ownership is None:
            await reparse_session.commit()
            return
        await _validate_raw_root(
            reparse_session,
            raw,
            ownership=ownership,
            lock_connection=False,
            allow_historical_null_actor_connection=True,
        )
        await _supersede_edited_message(
            reparse_session,
            raw,
            ownership=ownership,
            allow_historical_null_actor_connection=True,
        )
        if not await _raw_text_is_signal_candidate(
            reparse_session,
            raw,
            ownership=ownership,
        ):
            await _mark_raw_processed(
                reparse_session,
                raw,
                ownership=ownership,
                allow_historical_null_actor_connection=True,
            )
            raise signals_service.RawPayloadAlreadyProcessedError(
                "Telegram command/question raw is terminal without parsing"
            )
        if parser_alert_context is not None:
            await _validate_parser_alert_connection(
                reparse_session,
                context=parser_alert_context,
                subject_id=ownership.subject_id,
            )
        # Classification and exact roots are frozen for this attempt. The parser
        # receives no session and no Telegram/provider lock survives its await.
        await reparse_session.commit()

    async def _before_normalize(
        reparse_session: AsyncSession,
        raw: RawPayload,
    ) -> None:
        if ownership is None:
            return
        await _lock_pending_raw_for_completion(
            reparse_session,
            raw,
            ownership=ownership,
            allow_historical_null_actor_connection=True,
        )

    async def _after_normalize(
        reparse_session: AsyncSession,
        _raw: RawPayload,
    ) -> None:
        await reparse_session.commit()

    outcome = signals_service.ParserOutcome()
    rows = await signals_service.reparse_unparsed(
        session,
        parse=parser,
        subject_id=ownership.subject_id,
        # Historical Telegram raws remain part of their subject after recipient
        # connection rotation; each row validates and copies its own provenance.
        integration_connection_id=None,
        text_from_raw=_text_from_raw if ownership is not None else None,
        date_from_raw=_day_from_raw if ownership is not None else None,
        before_parse=_before_reparse,
        before_normalize=_before_normalize if ownership is not None else None,
        after_normalize=_after_normalize if ownership is not None else None,
        parser_outcome=outcome,
        allow_historical_null_actor_connection=(ownership is not None),
    )
    # Every terminal raw/Signal mutation wins durability before alert state. A
    # failed reconciliation is logged and retried by a later parser attempt.
    await session.commit()
    await _reconcile_parser_alert_best_effort(
        session,
        context=parser_alert_context,
        outcome=outcome,
    )
    return rows


def make_signal_parser(known: Optional[list[str]] = None) -> signals_service.Parser:
    """Build the parser handed to ``ingest_text``.

    A factory, not a bare function, because the prompt carries the keys already in
    use — the parser is only as consistent as the vocabulary it is reminded of.
    Injected as a parameter everywhere downstream, so tests never touch a network.
    """
    vocabulary = ", ".join(known or []) or "пока пусто"
    system = (
        f"{_PARSER_SYSTEM}\n\n"
        f"Уже использованные ключи — переиспользуй подходящий, новый заводи только "
        f"если ни один не подходит: {vocabulary}"
    )

    async def _parse(text: str) -> list[dict]:
        result = await LLMClient().extract_json(text, system=system)
        if not isinstance(result, dict) or "signals" not in result:
            raise ValueError("signal parser response must contain a signals list")
        items = result["signals"]
        if not isinstance(items, list):
            raise ValueError("signal parser signals must be a list")
        return items

    return _parse


async def answer_reply(question: str, context: str, facts: str = "") -> str:
    """Answer using the message replied to and the day the brief was built on.

    Either half can be empty — a question typed on its own has no message behind
    it, and a day with no brief yet has no numbers — so the prompt only carries
    what exists rather than a labelled hole the model might fill in.
    """
    parts = []
    if context:
        parts.append(f"Последние сообщения бота:\n{context}")
    if facts:
        parts.append(f"Данные последнего разбора дня (JSON):\n{facts}")
    parts.append(f"Вопрос:\n{question}")
    return await LLMClient().complete_text(
        "\n\n".join(parts), system=_REPLY_SYSTEM, max_tokens=_REPLY_MAX_TOKENS
    )
