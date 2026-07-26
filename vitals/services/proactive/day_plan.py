"""What kind of day this is — the week template, the evening block, the answers.

Vitals knows everything about the body and nothing about the **day**. Without
"remote / gym / heavy day" every piece of advice collapses into "спал плохо —
отдохни". This module is that missing half, and it is deliberately cheap:

  * **The week template** (E1) is one ``app_settings`` row, exactly like
    ``enabled_modules``: weekday → the same three answers. It is a *guess*, never
    a fact — it fills in the day until the owner says otherwise.
  * **The evening block** (E2) runs at **23:45**, not at midnight: past 00:00
    "tomorrow" means a different day than the one being planned, and the message
    would silently ask about the wrong date.
  * **Every button carries its own date** (E3), so a tap that lands after midnight
    still answers the day it was asked about. That is why the callback payload is
    ``ctx:<iso date>:<key>:<value>`` and not "today".

``answers`` (what he said) and ``planned`` (what the template guessed) live side
by side on the same row: the gap between them is the only material a template
could ever learn from, and it is only collectable if the guess is written down at
the time it was made — hence :func:`record_plan`, which runs even on evenings
when nothing is ever tapped.

The question set is three yes/no-ish knobs, data-driven like the rest of the
project's registries (``conflict_rules``, ``body_metrics``): adding a fourth is one
tuple entry, and everything — the summary line, the buttons, the sanitizer —
follows from it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.app_settings import AppSetting
from vitals.models.signals import DayContext
from vitals.services import signals_service
from vitals.services.proactive import compose
from vitals.utils.timeutils import today_local

logger = logging.getLogger(__name__)

SETTINGS_KEY = "week_template"  # app_settings.key

WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Question:
    """One knob of the day. ``labels`` doubles as the set of legal values —
    the summary line, the buttons and the sanitizer all read it."""

    key: str
    default: Any
    labels: dict  # value → the word used both in the line and on the button


# Order here is the order the summary line reads and the buttons render.
QUESTIONS: tuple[Question, ...] = (
    Question("remote", False, {True: "удалёнка", False: "в офисе"}),
    Question("gym", False, {True: "зал", False: "без зала"}),
    Question(
        "load",
        "normal",
        {"light": "лёгкий день", "normal": "обычный день", "heavy": "тяжёлый день"},
    ),
)

QUESTIONS_BY_KEY = {q.key: q for q in QUESTIONS}

# Neutral on every day of the week: guessing a gym schedule from nothing would
# just be a wrong answer pre-filled. The owner edits this in Settings (прогон 6),
# and until then the exception buttons are one tap away.
DEFAULT_DAY: dict[str, Any] = {q.key: q.default for q in QUESTIONS}
DEFAULT_TEMPLATE: dict[str, dict] = {day: dict(DEFAULT_DAY) for day in WEEKDAYS}


# ── The template (E1) ─────────────────────────────────────────────────────────
def _sanitize_day(raw: Any) -> dict[str, Any]:
    """One weekday's answers, projected onto the question registry.

    Unknown keys are dropped and an out-of-registry value falls back to the
    default — this is a hand-editable JSON blob, so it is a trust boundary.
    """
    day = dict(DEFAULT_DAY)
    if isinstance(raw, dict):
        for question in QUESTIONS:
            if question.key not in raw:
                continue
            value = raw[question.key]
            if isinstance(question.default, bool):
                day[question.key] = bool(value)
            elif value in question.labels:
                day[question.key] = value
    return day


def sanitize_template(raw: Any) -> dict[str, dict]:
    """Arbitrary stored data → a full 7-day template. Never raises."""
    source = raw if isinstance(raw, dict) else {}
    return {day: _sanitize_day(source.get(day)) for day in WEEKDAYS}


async def get_week_template(session: AsyncSession) -> dict[str, dict]:
    """The stored template, or the neutral default. Never raises (a broken
    settings row must not take the evening block down with it).

    No Redis cache on purpose: this is read twice a day by two jobs.
    """
    try:
        row = await session.get(AppSetting, SETTINGS_KEY)
    except Exception:
        logger.warning("week template: DB read failed; using defaults", exc_info=True)
        return sanitize_template(None)
    return sanitize_template(row.value if row is not None else None)


async def set_week_template(session: AsyncSession, template: Any) -> dict[str, dict]:
    """Store the template (sanitized). Flushes; the caller commits."""
    clean = sanitize_template(template)
    row = await session.get(AppSetting, SETTINGS_KEY)
    if row is None:
        session.add(AppSetting(key=SETTINGS_KEY, value=clean))
    else:
        row.value = clean  # a new dict, so SQLAlchemy sees the change
    await session.flush()
    return clean


def guess_for(template: dict[str, dict], on_date: date_type) -> dict[str, Any]:
    """What the template expects that date to be."""
    return dict(template.get(WEEKDAYS[on_date.weekday()], DEFAULT_DAY))


# ── The day's answers (E3/E4) ─────────────────────────────────────────────────
async def resolve(
    session: AsyncSession, on_date: date_type
) -> tuple[dict[str, Any], bool]:
    """``(answers, answered)`` — his answer if there is one, else the guess.

    An existing row with empty ``answers`` is *not* an answer: that is the row the
    evening block writes to park its guess.

    One tap answers one question, so the rest of the day still comes from the
    guess he was correcting (``planned`` — what the template said *when it asked*,
    which is what he saw). Falling back to the bare defaults here would let a
    single "иду в зал" quietly cancel the template's "удалёнка".
    """
    row = await signals_service.get_day_context(session, on_date)
    guess = (
        dict(row.planned)
        if row is not None and row.planned
        else guess_for(await get_week_template(session), on_date)
    )
    if row is not None and row.answers:
        return {**guess, **row.answers}, True
    return guess, False


async def record_answer(
    session: AsyncSession, on_date: date_type, key: str, value: Any
) -> DayContext:
    """One tap → one answer merged into that day, the guess preserved beside it."""
    row = await signals_service.get_day_context(session, on_date)
    answers = dict(row.answers or {}) if row is not None else {}
    answers[key] = value
    # Only fill the guess when there isn't one: a tap that lands days later must
    # not overwrite what the template thought at the time it asked.
    planned = None
    if row is None or not row.planned:
        planned = guess_for(await get_week_template(session), on_date)
    return await signals_service.set_day_context(
        session, on_date, answers=answers, planned=planned, source=Source.MANUAL.value
    )


async def record_plan(
    session: AsyncSession, on_date: date_type, planned: dict
) -> DayContext:
    """Park the template's guess for a day, without touching any answer."""
    row = await signals_service.get_day_context(session, on_date)
    return await signals_service.set_day_context(
        session,
        on_date,
        answers=dict(row.answers or {}) if row is not None else {},
        planned=planned,
        source=row.source if row is not None else Source.TEMPLATE.value,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────
def describe(answers: dict) -> str:
    """``{"remote": True, ...}`` → "удалёнка · без зала · обычный день"."""
    words = []
    for question in QUESTIONS:
        value = answers.get(question.key, question.default)
        word = question.labels.get(value)
        if word:
            words.append(word)
    return " · ".join(words)


def encode(value: Any) -> str:
    """Value → wire form (a callback payload, a form field). :func:`decode` back."""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def decode(value: str) -> Any:
    """Wire form → value. Used by both halves of the UI — the button taps coming
    back through Telegram and the week-template selects on the settings page —
    because ``bool("0")`` is ``True`` and getting that wrong silently inverts an
    answer."""
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return value


def exception_buttons(current: dict, on_date: date_type) -> list[tuple[str, str]]:
    """One button per *other* answer — corrections, not a questionnaire.

    Asking all three questions from scratch every evening would be five taps a day
    for a template that is right most of the time; offering only the exceptions
    makes the common case zero taps.
    """
    buttons: list[tuple[str, str]] = []
    for question in QUESTIONS:
        value = current.get(question.key, question.default)
        for option, label in question.labels.items():
            if option != value:
                buttons.append((label, f"ctx:{on_date.isoformat()}:{question.key}:{encode(option)}"))
    return buttons


def day_block(day: Optional[dict]) -> Optional[compose.Block]:
    """The brief's one line about today (E4). ``None`` when there is nothing to say."""
    if not day:
        return None
    line = describe(day.get("answers") or {})
    if not line:
        return None
    prefix = "Сегодня" if day.get("source") == Source.MANUAL.value else "Сегодня по шаблону"
    return compose.Block(compose.KIND_DAY, f"{prefix}: {line}", 30)


def buttons_from_context(day: Optional[dict], on_date: date_type):
    """Exception buttons for a brief — none once he has answered that day."""
    if not day or day.get("source") == Source.MANUAL.value:
        return None
    return exception_buttons(day.get("answers") or {}, on_date) or None


def summary_line(garmin) -> str:
    """The day in the numbers that are actually about *today* (E2).

    Recovery numbers belong to the morning brief; what closes a day is what was
    done in it. Missing metrics are simply absent — the 22:00 Garmin poll is the
    freshest data at 23:45 and it may not have everything.
    """
    if garmin is None:
        return ""
    parts = []
    if garmin.steps is not None:
        parts.append(f"{garmin.steps} шагов")
    if garmin.active_calories is not None:
        parts.append(f"{garmin.active_calories} ккал актив")
    intensity = (garmin.intensity_minutes_moderate or 0) + (
        garmin.intensity_minutes_vigorous or 0
    )
    if intensity:
        parts.append(f"{intensity} мин интенсивности")
    return "Итог дня: " + " · ".join(parts) if parts else ""


def dedupe_key(on_date: date_type) -> str:
    return f"evening:{on_date.isoformat()}"


# ── The 23:45 job (E2) ────────────────────────────────────────────────────────
async def evening_job(session_factory, redis=None) -> None:
    """Close today, ask about tomorrow.

    No Garmin pull in front of this one (unlike the brief): the 22:00 poll is
    recent enough for a day summary, and every extra pull is a login the account
    doesn't need to spend. No model call either — the whole message is code.
    """
    from vitals.services import garmin_service
    from vitals.services.proactive import channels, delivery

    notifier = channels.build_notifier()
    async with session_factory() as session:
        today = today_local()
        tomorrow = today + timedelta(days=1)

        planned = guess_for(await get_week_template(session), tomorrow)
        await record_plan(session, tomorrow, planned)

        answers, answered = await resolve(session, tomorrow)
        blocks = [
            compose.Block(compose.KIND_DAY, summary_line(await garmin_service.get_daily(session, today)), 10),
            compose.Block(
                compose.KIND_ASK, "Как день? Напиши пару слов — запишу.", 20
            ),
            compose.Block(compose.KIND_DAY, f"Завтра: {describe(answers)}", 30),
        ]

        await delivery.send(
            session,
            notifier,
            text=compose.render(blocks),
            category=delivery.CATEGORY_EVENING,
            dedupe_key=dedupe_key(today),
            buttons=exception_buttons(answers, tomorrow) if not answered else None,
        )
        await session.commit()
