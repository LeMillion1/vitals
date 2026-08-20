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
from dataclasses import dataclass, replace
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Any, Optional

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
    Severity,
    SignalKind,
    Source,
)
from vitals.i18n import t
from vitals.integrations.llm_client import LLMClient
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject
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
from vitals.services.proactive import day_plan, delivery, prefs, signal_ai_service
from vitals.services.proactive.channels import Notifier
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.utils.timeutils import now_local, to_local_naive

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
            "could not reconcile the OpenRouter signal-parser alert",
            exc_info=True,
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
    connection = await session.scalar(stmt)
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
) -> None:
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
        await _validate_raw_root(session, raw, ownership=ownership)
        await session.commit()
        return _RawClaim(raw=raw, created=False)

    raw = RawPayload(
        subject_id=ownership.subject_id,
        actor_user_id=ownership.recipient_user_id,
        integration_connection_id=ownership.connection_id,
        domain=DOMAIN,
        source=SOURCE,
        external_id=external_id,
        payload=payload,
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
        if not claim.created:
            logger.info("ignoring repeated update %s", external_id)
            return
        parked = True

        if edited:
            try:
                await _supersede_edited_message(
                    session,
                    claim.raw,
                    ownership=ownership,
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
        include_legacy_unowned=ownership.include_legacy_unowned,
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
            logger.warning("could not acknowledge tap %s", callback_id, exc_info=True)

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
        ownership=ownership,
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
        logger.warning("could not redraw message %s", message_id, exc_info=True)


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
        logger.warning("unparseable context payload: %s", data)
        return None

    question = day_plan.QUESTIONS_BY_KEY.get(key)
    answer = day_plan.decode(value)
    if question is None or answer not in question.labels:
        logger.warning("context payload outside the question registry: %s", data)
        return None

    await day_plan.record_answer(
        session,
        on_date,
        key,
        answer,
        ownership=ownership,
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
    reply_id = (message.get("reply_to_message") or {}).get("message_id")
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
) -> bool:
    try:
        locked = await _lock_pending_raw_for_completion(
            session,
            raw,
            ownership=ownership,
            allow_subject_adopted_unowned=allow_subject_adopted_unowned,
        )
    except signals_service.RawPayloadAlreadyProcessedError:
        # Release the root locks; a newer edit already owns the terminal state.
        await session.commit()
        return False
    locked.processed_at = now_local()
    await session.flush()
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


async def _signal_echo_is_current(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    ai_invocation_id: uuid.UUID | None,
) -> bool:
    """Lock one logical message and reject an echo superseded by a later edit."""

    if isinstance(raw_payload_id, bool) or not isinstance(raw_payload_id, int):
        raise InboundOwnershipError("signal echo raw id is invalid")
    if ai_invocation_id is not None and not isinstance(ai_invocation_id, uuid.UUID):
        raise InboundOwnershipError("signal echo invocation id is invalid")
    await acquire_identity_governance_lock(session)
    await _lock_subject_root(session, ownership=ownership)
    raw = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == raw_payload_id)
        .execution_options(populate_existing=True)
    )
    if raw is None:
        raise InboundOwnershipError("signal echo raw does not exist")
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
        raise InboundOwnershipError("signal echo raw disappeared")
    for candidate in locked:
        await _validate_raw_root(
            session,
            candidate,
            ownership=ownership,
            lock_connection=False,
            allow_subject_adopted_unowned=True,
        )
    if any(_is_prior_message_version(current, row) for row in locked):
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
                ).where(AIInvocation.id == ai_invocation_id)
            )
        ).one_or_none()
        if invocation is None:
            raise InboundOwnershipError("signal echo invocation does not exist")
        subject_id, actor_id, linked_raw_id, purpose, source, status = invocation
        if (
            subject_id != ownership.subject_id
            or actor_id != ownership.recipient_user_id
            or linked_raw_id != raw_payload_id
            or purpose != AIInvocationPurpose.SIGNAL_PARSE.value
            or source != AIInvocationSource.TELEGRAM.value
            or status
            not in {
                AIInvocationStatus.SUCCEEDED.value,
                AIInvocationStatus.FAILED.value,
                AIInvocationStatus.AMBIGUOUS.value,
            }
        ):
            raise InboundOwnershipError("signal echo invocation provenance is invalid")
        if status != AIInvocationStatus.SUCCEEDED.value and current.processed_at is not None:
            return False
    elif current.processed_at is not None:
        return False
    stale_fact = await session.scalar(
        select(Signal.id).where(
            Signal.raw_id == raw_payload_id,
            Signal.misparse.is_(True),
        ).limit(1)
    )
    return stale_fact is None


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
) -> None:
    """The channel-agnostic entry point (C8): already text, whatever produced it."""
    if ownership is not None and not isinstance(
        ownership, ProactiveOwnershipContext
    ):
        raise TypeError("ownership must be a ProactiveOwnershipContext or None")
    owner_identity = ownership.owner_action() if ownership is not None else None
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
        # Stored anyway, and marked done on the spot: the raw row is what a
        # webhook retry trips over, and without it Telegram's second delivery of
        # the same ``/start`` gets a second identical answer. ``processed`` keeps
        # the re-parse sweep from feeding «/start» to the parser later.
        if raw is not None:
            if not await _mark_raw_processed(
                session,
                raw,
                ownership=ownership,
            ):
                return
        else:
            await signals_service.store_raw_text(
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
            ownership=ownership,
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
        if raw is not None:
            if not await _mark_raw_processed(
                session,
                raw,
                ownership=ownership,
            ):
                return
        else:
            await signals_service.store_raw_text(
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
                parse_result = await signal_ai_service.persist_signal_parse(
                    session,
                    prepared_parse,
                    completion,
                )
                await session.commit()
        else:
            parse_result = signal_ai_service.SignalParseResult(
                invocation_id=prepared_parse.invocation_id,
                status=prepared_parse.reservation_status,
                processed=False,
                stale=False,
                fallback=prepared_parse.fallback,
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
                "could not reconcile the platform signal-parser alert",
                exc_info=True,
            )
        if parse_result.stale:
            return
        if not parse_result.processed:
            echo_text = "Сохранил как есть — разобрать не смог. Посмотрю позже."
            buttons = None
        elif not parse_result.signals:
            echo_text = (
                "Записал. Фактов для графиков тут не нашёл — "
                "если что-то важное, скажи прямо."
            )
            buttons = None
        else:
            echo_text = render_echo(parse_result.signals)
            buttons = [
                ("не то", f"{CB_MISPARSE}{parse_result.signals[0].batch_id}")
            ]
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
        if not await _signal_echo_is_current(
            session,
            ownership=ownership,
            raw_payload_id=prepared_parse.raw_payload_id,
            ai_invocation_id=terminal_invocation_id,
        ):
            await session.commit()
            return
        prepared_delivery = await delivery._prepare_delivery(
            session,
            notifier,
            text=echo_text,
            category=delivery.CATEGORY_ECHO,
            buttons=buttons,
            reply_to=str(message_id) if message_id else None,
            ownership=ownership,
            ai_invocation_id=terminal_invocation_id,
        )
        await session.commit()
        if prepared_delivery is None:
            return
        assert notifier is not None
        delivered = await delivery._transmit_prepared_delivery(
            notifier,
            prepared_delivery,
        )
        if delivered is None:
            return
        journal_delivery = prepared_delivery
        try:
            still_current = await _signal_echo_is_current(
                session,
                ownership=ownership,
                raw_payload_id=prepared_parse.raw_payload_id,
                ai_invocation_id=terminal_invocation_id,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            still_current = False
            logger.warning(
                "could not revalidate a delivered signal echo",
                exc_info=True,
            )
        if not still_current:
            replacement_text = t("telegram.signal_echo_superseded")
            try:
                await notifier.edit(
                    delivered.external_id,
                    replacement_text,
                    buttons=None,
                )
            except Exception:
                logger.warning(
                    "could not neutralize a superseded signal echo",
                    exc_info=True,
                )
            else:
                journal_delivery = replace(
                    prepared_delivery,
                    text=replacement_text,
                    buttons=None,
                )
        await delivery._journal_prepared_delivery(
            session,
            journal_delivery,
            external_id=delivered.external_id,
        )
        await session.commit()
        return

    parser = parse
    if parser is None:
        parser = make_signal_parser(
            await known_keys(
                session,
                subject_id=(ownership.subject_id if ownership is not None else None),
                include_legacy_unowned=(
                    ownership.include_legacy_unowned
                    if ownership is not None
                    else False
                ),
            )
        )
    if parser_alert_context is not None and ownership is None:
        raise InboundOwnershipError(
            "parser alert context requires proactive ownership"
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
    if raw is None:
        raw = await signals_service.store_raw_text(
            session,
            text=text,
            external_id=external_id,
            source=SOURCE,
            identity=owner_identity,
            integration_connection_id=connection_id,
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
                (
                    lambda normalize_session, normalize_raw: (
                        _lock_pending_raw_for_completion(
                            normalize_session,
                            normalize_raw,
                            ownership=ownership,
                        )
                    )
                )
                if claimed_raw
                else None
            ),
            parser_outcome=outcome,
        )
    except signals_service.RawPayloadAlreadyProcessedError:
        await session.commit()
        return

    # Signals/raw terminal state must win durability before any Telegram send.
    # An edit waiting on the subject lock can then supersede the complete batch.
    parser_pending = not rows and raw.processed_at is None
    await session.commit()
    await _reconcile_parser_alert_best_effort(
        session,
        context=parser_alert_context,
        outcome=outcome,
    )

    if parser_pending:
        await delivery.send(
            session,
            notifier,
            text="Сохранил как есть — разобрать не смог. Посмотрю позже.",
            category=delivery.CATEGORY_ECHO,
            reply_to=str(message_id) if message_id else None,
            ownership=ownership,
        )
        return

    if not rows:
        # Not an error, and it must not read like one. The evening block asks «как
        # день?» and «весь день за компом» is a perfectly good answer that simply
        # holds no state, symptom or exposure — the schema has nowhere to put it.
        # The text is saved either way and the re-parse sweep sees it again, so
        # the honest thing to say is that it is written down.
        await delivery.send(
            session,
            notifier,
            text="Записал. Фактов для графиков тут не нашёл — если что-то важное, скажи прямо.",
            category=delivery.CATEGORY_ECHO,
            reply_to=str(message_id) if message_id else None,
            ownership=ownership,
        )
        return

    await delivery.send(
        session,
        notifier,
        text=render_echo(rows),
        category=delivery.CATEGORY_ECHO,
        buttons=[("не то", f"{CB_MISPARSE}{rows[0].batch_id}")],
        reply_to=str(message_id) if message_id else None,
        ownership=ownership,
    )


async def _answer_reply(
    session: AsyncSession,
    question: str,
    answered,
    *,
    notifier: Optional[Notifier],
    message_id: Optional[Any],
    ownership: ProactiveOwnershipContext | None,
) -> None:
    """``answered`` is the message being replied to; for a question typed on its
    own the last few things we said stand in for it."""
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
    # Raw question state and every PHI/provenance read are durable/materialized.
    # Release governance/S/owner locks before the still-legacy provider await.
    await session.commit()
    try:
        text = await answer_reply(
            question,
            context,
            facts,
        )
    except Exception:
        logger.warning("could not answer a reply (code=provider_error)")
        text = _NO_LLM_REPLY
    prepared_delivery = await delivery._prepare_delivery(
        session,
        notifier,
        text=text or _NO_LLM_REPLY,
        category=delivery.CATEGORY_REPLY,
        reply_to=str(message_id) if message_id else None,
        ownership=ownership,
    )
    # Delivery policy reads/locks are complete. Never retain their transaction
    # across Telegram; journal a successful send in a fresh transaction. This
    # preserves the known best-effort outbound race until durable claims exist.
    await session.commit()
    if prepared_delivery is None:
        return
    assert notifier is not None
    delivered = await delivery._transmit_prepared_delivery(
        notifier,
        prepared_delivery,
    )
    if delivered is None:
        return
    await delivery._journal_prepared_delivery(
        session,
        prepared_delivery,
        external_id=delivered.external_id,
    )
    await session.commit()


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
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> list[str]:
    """The vocabulary the parser is reminded of, most-used first."""
    stats = await signals_service.key_frequency(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
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
                    "could not replay stored callback raw %s",
                    raw.id,
                    exc_info=True,
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
                )
                await _supersede_edited_message(
                    session,
                    raw,
                    ownership=ownership,
                    allow_subject_adopted_unowned=True,
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
                    "platform signal recovery failed for raw %s",
                    raw_id,
                    exc_info=True,
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
            "could not reconcile the platform signal-parser alert",
            exc_info=True,
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
    parser = parse or make_signal_parser(
        await known_keys(
            session,
            subject_id=(ownership.subject_id if ownership is not None else None),
            include_legacy_unowned=(
                ownership.include_legacy_unowned
                if ownership is not None
                else False
            ),
        )
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
        )
        await _supersede_edited_message(
            reparse_session,
            raw,
            ownership=ownership,
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
        subject_id=ownership.subject_id if ownership is not None else None,
        # Historical Telegram raws remain part of their subject after recipient
        # connection rotation; each row validates and copies its own provenance.
        integration_connection_id=None,
        include_legacy_unowned=(
            ownership.include_legacy_unowned if ownership is not None else False
        ),
        text_from_raw=_text_from_raw if ownership is not None else None,
        date_from_raw=_day_from_raw if ownership is not None else None,
        before_parse=_before_reparse,
        before_normalize=_before_normalize if ownership is not None else None,
        after_normalize=_after_normalize if ownership is not None else None,
        parser_outcome=outcome,
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
