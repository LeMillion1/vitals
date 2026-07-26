"""What comes back from the channel: button taps, replies, and free text.

Three inbound shapes, deliberately three code paths:

  * **a tap** (``callback_query``) — the "не то" undo, and the day-context answers
    the evening block will ask for (прогон 4). Stateless: the button carries
    everything needed in its payload, so a tap works days later.
  * **a reply to one of our messages** — answered from the context that message
    was built on, and nothing else. This is *not* a second chat with the data:
    deep questions belong in Claude.ai over MCP, which has 69 tools and a better
    model. Here the model sees one message and is told to invent nothing.
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

import logging
from datetime import date as date_type
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, SignalKind, Source
from vitals.integrations.llm_client import LLMClient
from vitals.models.raw_payload import RawPayload
from vitals.services import signals_service
from vitals.services.proactive import day_plan, delivery
from vitals.services.proactive.channels import Notifier
from vitals.services.raw_payload_service import upsert_raw_payload
from vitals.utils.timeutils import today_local

logger = logging.getLogger(__name__)

DOMAIN = Domain.SIGNALS.value
SOURCE = Source.TELEGRAM.value

# Button payloads (Telegram caps callback_data at 64 bytes, both fit comfortably).
CB_MISPARSE = "mis:"   # mis:<batch_id>
CB_CONTEXT = "ctx:"    # ctx:<iso date>:<key>:<value>

# How many already-used keys the parser is shown. Reusing an existing key is the
# only thing keeping the open registry from drifting into 60 near-synonyms before
# прогон 7 can consolidate it; the cap keeps the prompt small.
_KNOWN_KEYS_LIMIT = 40

_PARSER_SYSTEM = """\
Ты разбираешь короткие сообщения владельца дашборда здоровья на отдельные факты.

Верни JSON вида {"signals": [...]}. Каждый элемент:
- kind: "state" | "symptom" | "exposure"
    state — то, что есть всегда и имеет интенсивность («энергии ноль», «спать хочу»)
    symptom — то, что случилось и имеет тяжесть («голова раскалывается», «тошнит»)
    exposure — то, что человек принял или сделал («кофе в 22», «выпил два бокала»)
- key: короткий английский слаг (sleepiness, headache, caffeine_late, alcohol)
- value_num: 1-5 для state/symptom (5 — максимум), либо количество для exposure
- unit: единица для exposure ("mg", "ml", "min"), иначе null
- at_time: "HH:MM", если время названо или однозначно следует из фразы, иначе null
- note: кусок исходной фразы, из которого взят этот факт

Одно сообщение может дать несколько фактов — верни их все.
События дня (болезнь, поездка, смена протокола) сюда НЕ идут — для них есть
отдельный раздел, пропускай их.
Если фактов нет (болтовня, вопрос, благодарность) — верни {"signals": []}.
Ничего не выдумывай: чего нет в сообщении, того нет в ответе.\
"""

_REPLY_SYSTEM = """\
Ты отвечаешь на вопрос владельца по сообщению, которое бот прислал ему раньше.
Отвечай по-русски, коротко (2-4 предложения), только по приведённому тексту.
Если ответа в тексте нет — так и скажи. Никаких выдуманных чисел.\
"""

_REPLY_MAX_TOKENS = 800
_NO_LLM_REPLY = "Сейчас не отвечу — модель недоступна. Загляни в приложение."


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


async def _already_handled(session: AsyncSession, external_id: str) -> bool:
    result = await session.execute(
        select(RawPayload.id).where(
            RawPayload.domain == DOMAIN,
            RawPayload.source == SOURCE,
            RawPayload.external_id == external_id,
        )
    )
    return result.scalars().first() is not None


async def handle_update(
    session: AsyncSession,
    update: dict,
    *,
    notifier: Optional[Notifier],
    parse: Optional[signals_service.Parser] = None,
) -> None:
    """Entry point for one Telegram update. Safe to call twice with the same one."""
    update_id = update.get("update_id")
    external_id = f"tg:{update_id}" if update_id is not None else None
    if external_id and await _already_handled(session, external_id):
        logger.info("ignoring repeated update %s", external_id)
        return

    callback = update.get("callback_query")
    if callback:
        await _handle_callback(session, callback, notifier=notifier, external_id=external_id)
        return

    message = update.get("message") or update.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    if not text:
        # Photos, stickers, voice notes: nothing to capture yet (voice arrives as
        # a transcription step in front of handle_text, not as a branch here).
        return

    await handle_text(
        session,
        text,
        notifier=notifier,
        external_id=external_id,
        message_id=message.get("message_id"),
        reply_to_message_id=(message.get("reply_to_message") or {}).get("message_id"),
        parse=parse,
    )


# ── Taps ──────────────────────────────────────────────────────────────────────
async def _handle_callback(
    session: AsyncSession,
    callback: dict,
    *,
    notifier: Optional[Notifier],
    external_id: Optional[str],
) -> None:
    data = str(callback.get("data") or "")
    callback_id = str(callback.get("id") or "")

    # The tap itself is data too — which button, when — so it lands in the lake
    # like everything else, and doubles as this update's idempotency record.
    if external_id:
        await upsert_raw_payload(
            session, domain=DOMAIN, source=SOURCE, external_id=external_id, payload=callback
        )

    toast = ""
    if data.startswith(CB_MISPARSE):
        batch_id = data[len(CB_MISPARSE):]
        changed = await signals_service.mark_misparse(session, batch_id)
        # The rows stay, flagged: they are the material прогон 7 builds the key
        # registry from — real mistakes, not remembered ones.
        toast = "Убрал из графиков" if changed else "Уже убрано"
    elif data.startswith(CB_CONTEXT):
        toast = "Записал" if await _apply_context(session, data) else ""

    if notifier is not None and callback_id:
        try:
            await notifier.answer_callback(callback_id, toast)
        except Exception:
            logger.warning("could not acknowledge tap %s", callback_id, exc_info=True)


async def _apply_context(session: AsyncSession, data: str) -> bool:
    """``ctx:<iso date>:<key>:<value>`` → merge one answer into that day's context.

    The date rides in the payload rather than being "today": the evening block
    asks about *tomorrow*, and a tap that lands after midnight must still answer
    the day it was asked about. Merging (and keeping the template's guess beside
    the answer) is ``day_plan``'s job — here we only decode the payload.
    """
    try:
        _, iso_date, key, value = data.split(":", 3)
        on_date = date_type.fromisoformat(iso_date)
    except ValueError:
        logger.warning("unparseable context payload: %s", data)
        return False

    await day_plan.record_answer(session, on_date, key, _coerce_answer(value))
    return True


def _coerce_answer(value: str) -> Any:
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return value


# ── Text ──────────────────────────────────────────────────────────────────────
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
) -> None:
    """The channel-agnostic entry point (C8): already text, whatever produced it."""
    if reply_to_message_id is not None:
        answered = await delivery.find_sent(session, str(reply_to_message_id))
        # The evening block *asks* «как день?», so a reply to it is an answer, not
        # a question — it falls through to capture. Every other message of ours is
        # something he might ask about.
        if answered is not None and answered.category != delivery.CATEGORY_EVENING:
            # A question is data too, and this is also what stops a webhook retry
            # from paying for a second model call on the same question.
            await signals_service.store_raw_text(
                session, text=text, external_id=external_id, source=SOURCE
            )
            await _answer_reply(session, text, answered, notifier=notifier, message_id=message_id)
            return

    known = [key for key, _ in await signals_service.key_frequency(session)][:_KNOWN_KEYS_LIMIT]
    rows = await signals_service.ingest_text(
        session,
        text=text,
        parse=parse or make_signal_parser(known),
        external_id=external_id,
        on_date=on_date or today_local(),
        source=SOURCE,
    )

    if not rows:
        await delivery.send(
            session,
            notifier,
            text="Сохранил как есть — разобрать не смог. Посмотрю позже.",
            category=delivery.CATEGORY_ECHO,
            reply_to=str(message_id) if message_id else None,
        )
        return

    await delivery.send(
        session,
        notifier,
        text=render_echo(rows),
        category=delivery.CATEGORY_ECHO,
        buttons=[("не то", f"{CB_MISPARSE}{rows[0].batch_id}")],
        reply_to=str(message_id) if message_id else None,
    )


async def _answer_reply(
    session: AsyncSession,
    question: str,
    answered,
    *,
    notifier: Optional[Notifier],
    message_id: Optional[Any],
) -> None:
    context = (answered.payload or {}).get("text") or ""
    try:
        text = await answer_reply(question, context)
    except Exception:
        logger.warning("could not answer a reply", exc_info=True)
        text = _NO_LLM_REPLY
    await delivery.send(
        session,
        notifier,
        text=text or _NO_LLM_REPLY,
        category=delivery.CATEGORY_REPLY,
        reply_to=str(message_id) if message_id else None,
    )


def _fmt_num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def render_echo(rows) -> str:
    """What was understood, in his own words next to the key it became.

    Showing both halves is the point: he can tell at a glance whether the parse
    matched, and the drifting key is visible the day it drifts — which is exactly
    what the прогон-7 consolidation needs and what a pretty label would hide.
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
        lines.append(f"• {row.note} → {parsed}" if row.note else f"• {parsed}")
    return "Записал:\n" + "\n".join(lines)


# ── LLM ───────────────────────────────────────────────────────────────────────
def make_signal_parser(known_keys: Optional[list[str]] = None) -> signals_service.Parser:
    """Build the parser handed to ``ingest_text``.

    A factory, not a bare function, because the prompt carries the keys already in
    use — the parser is only as consistent as the vocabulary it is reminded of.
    Injected as a parameter everywhere downstream, so tests never touch a network.
    """
    known = ", ".join(known_keys or []) or "пока пусто"
    system = (
        f"{_PARSER_SYSTEM}\n\n"
        f"Уже использованные ключи — переиспользуй подходящий, новый заводи только "
        f"если ни один не подходит: {known}"
    )

    async def _parse(text: str) -> list[dict]:
        result = await LLMClient().extract_json(text, system=system)
        items = result.get("signals")
        return items if isinstance(items, list) else []

    return _parse


async def answer_reply(question: str, context: str) -> str:
    """Answer a reply using only the message it replied to."""
    prompt = f"Сообщение бота:\n{context}\n\nВопрос:\n{question}"
    return await LLMClient().complete_text(
        prompt, system=_REPLY_SYSTEM, max_tokens=_REPLY_MAX_TOKENS
    )
