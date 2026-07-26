"""The morning brief: the first thing the product ever says on its own.

Assembly is the composer's job (:mod:`compose`); this module owns the two things
around it — the model's single paragraph, and what happens when there is nothing
worth saying.

  * **The model can fail and the brief still arrives.** Any exception from the LLM
    (no key, no balance, upstream down, blank completion) drops the narrative
    block and nothing else. B4.
  * **An empty day is silence, not a brief.** No fresh Garmin row and no recovery
    numbers → nothing is sent and a passive ``info`` alert shows the gap in the
    web instead. B7.

The brief is stored in ``weekly_digests`` with ``kind='daily_brief'`` (B5), so
/reports shows it and MCP can read the history, without a second table.

Sending is deliberately *not* done here: :func:`generate_brief` builds and
persists, and the caller decides what to do with it — the job sends it under the
budget, the "Собрать бриф" button shows it in the web and sends nothing, the
"Отправить тестовое" button sends one off-budget copy. One function, three
callers, no flags.
"""
from __future__ import annotations

import json
import logging
from datetime import date as date_type
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import DigestKind, Domain, Severity, Source
from vitals.i18n import t
from vitals.models.milestones import DOMAIN as DIGEST_DOMAIN, WeeklyDigest
from vitals.services import alerts_service, digest_service
from vitals.services.proactive import compose, day_plan
from vitals.utils.timeutils import today_local

logger = logging.getLogger(__name__)

EMPTY_DAY_ALERT_KEY = "brief_empty_day"

# Short by design: the header already carries every number, so a long tail would
# only restate them. Enough headroom that a reasoning model's thinking tokens
# don't eat the visible answer (the bug that truncated the weekly digest in prod).
_BRIEF_MAX_TOKENS = 2000

BRIEF_SYSTEM = """\
Ты пишешь короткий утренний разбор для владельца дашборда здоровья Vitals.

Пользователь — молодой парень, который разбирается в теме (рекомпозиция, силовые,
Garmin). Базовые понятия объяснять не надо.

РОЛЬ: напарник, который шарит. Не врач, не коуч. Прямо, без воды, без паники.

ЗАДАЧА: 2-3 предложения. Что сегодня с организмом и что с этим делать сегодня.
Числа пользователь уже видит в шапке сообщения — не пересказывай их, объясняй.
Если данных мало — скажи это одним предложением и не тяни.

Блок `day` — что за день сегодня (удалёнка, зал, нагрузка). Если его `source` =
"template", это догадка шаблона недели, а не ответ пользователя: учитывай мягко,
не утверждай как факт.

ОГРАНИЧЕНИЯ (нарушение = баг):
- Опирайся ТОЛЬКО на JSON. Ничего не выдумывай, новых чисел не вводи.
- Никаких заголовков, списков и разметки — обычный текст, его читают в мессенджере.
- Язык: русский.\
"""


# ── Assembly ──────────────────────────────────────────────────────────────────
async def build_context(
    session: AsyncSession, *, on_date: Optional[date_type] = None
) -> dict:
    """Today's cross-domain snapshot, minus the protocol (B2), plus the day (E4).

    The day context is the difference between "спал плохо — отдохни" and advice
    that knows there is a gym session and a heavy workday ahead, so it goes into
    the model's JSON as well as onto the header line.
    """
    ctx = await digest_service.assemble_context(session, on_date=on_date, period_days=1)
    ctx = compose.strip_protocol(ctx)
    answers, answered = await day_plan.resolve(session, on_date or today_local())
    ctx["day"] = {
        "answers": answers,
        # His answer or the template's guess — the model is told which, so it can
        # hedge on a guess instead of asserting it.
        "source": Source.MANUAL.value if answered else Source.TEMPLATE.value,
    }
    return ctx


def build_prompt(ctx: dict) -> str:
    return (
        "Данные за сегодня (JSON):\n\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n\nНапиши утренний разбор: 2-3 предложения."
    )


async def narrative(llm: Any, ctx: dict) -> str:
    """The model's one block. Returns "" on any failure — never raises (B4)."""
    try:
        return await llm.complete_text(
            build_prompt(ctx),
            model=getattr(llm, "brief_model", None),
            system=BRIEF_SYSTEM,
            max_tokens=_BRIEF_MAX_TOKENS,
        )
    except Exception:
        logger.warning("brief narrative unavailable; sending the header alone", exc_info=True)
        return ""


async def generate_brief(
    session: AsyncSession,
    llm: Any,
    *,
    on_date: Optional[date_type] = None,
    source: str = Source.MANUAL.value,
) -> Optional[WeeklyDigest]:
    """Build the brief and store it. ``None`` = empty day, nothing built (B7)."""
    on_date = on_date or today_local()
    ctx = await build_context(session, on_date=on_date)
    if compose.is_empty_day(ctx, on_date=on_date):
        logger.info("no brief for %s: no sleep and nothing new", on_date)
        return None

    blocks = compose.header_blocks(ctx)
    day = day_plan.day_block(ctx.get("day"))
    if day is not None:
        blocks.append(day)
    tail = await narrative(llm, ctx)
    if tail:
        blocks.append(compose.Block(compose.KIND_NARRATIVE, tail, 90))

    row = WeeklyDigest(
        date=on_date,
        domain=DIGEST_DOMAIN,
        source=source,
        kind=DigestKind.DAILY_BRIEF.value,
        content=compose.render(blocks),
        context_json=ctx,
        model=getattr(llm, "brief_model", None) if tail else None,
    )
    session.add(row)
    await session.flush()
    return row


def dedupe_key(on_date: date_type) -> str:
    """One brief per day, enforced in the delivery journal rather than hoped for."""
    return f"brief:{on_date.isoformat()}"


# ── Scheduler job ─────────────────────────────────────────────────────────────
async def brief_job(session_factory, redis=None) -> None:
    """The 11:00 brief (B6).

    Pulls Garmin first, on its own, instead of hoping the poll schedule happened
    to run this morning — last night's sleep is the whole point of the message.
    A Garmin failure is not a reason to stay quiet: the brief goes out with
    whatever is in the lake.
    """
    from vitals.integrations.llm_client import LLMClient
    from vitals.services import garmin_service
    from vitals.services.language_service import get_language
    from vitals.i18n import current_lang
    from vitals.services.proactive import channels, delivery

    try:
        await garmin_service.sync_job(session_factory, redis)
    except Exception:
        logger.warning("garmin sync before the brief failed; using stored data", exc_info=True)

    notifier = channels.build_notifier()
    async with session_factory() as session:
        current_lang.set(await get_language(session, redis))

        today = today_local()
        row = await generate_brief(session, LLMClient(), source=Source.SCHEDULER.value)
        if row is None:
            await alerts_service.raise_alert(
                session,
                domain=Domain.SYSTEM.value,
                severity=Severity.INFO.value,
                message=t("alert.brief_empty_day"),
                alert_key=EMPTY_DAY_ALERT_KEY,
            )
            await session.commit()
            return

        await delivery.send(
            session,
            notifier,
            text=row.content,
            category=delivery.CATEGORY_BRIEF,
            dedupe_key=dedupe_key(today),
            # Nothing answered for today → the header shows the template's guess
            # and the buttons are how it gets corrected in one tap (E4).
            buttons=day_plan.buttons_from_context((row.context_json or {}).get("day"), today),
        )
        await alerts_service.resolve_by_key(session, alert_key=EMPTY_DAY_ALERT_KEY)
        await session.commit()
