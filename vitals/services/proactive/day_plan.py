"""What kind of day this is — the week template, the evening block, the answers.

Vitals knows everything about the body and nothing about the **day**. Without
"remote / gym / heavy day" every piece of advice collapses into "спал плохо —
отдохни". This module is that missing half, and it is deliberately cheap:

  * **The week template** is one ``app_settings`` row, exactly like
    ``enabled_modules``: weekday → the answers a weekday can actually predict. It
    is a *guess*, never a fact — it fills in the day until the owner says
    otherwise. Questions a calendar cannot know (how heavy the day is) are marked
    ``in_template=False`` and only ever asked.
  * **The evening block** runs at **23:45**, not at midnight: past 00:00
    "tomorrow" means a different day than the one being planned, and the message
    would silently ask about the wrong date.
  * **Every button carries its own date**, so a tap that lands after midnight
    still answers the day it was asked about. That is why the callback payload is
    ``ctx:<iso date>:<key>:<value>`` and not "today".

``answers`` (what he said) and ``planned`` (what the template guessed) live side
by side on the same row: the gap between them is the only material a template
could ever learn from, and it is only collectable if the guess is written down at
the time it was made — hence :func:`record_plan`, which runs even on evenings
when nothing is ever tapped.

The question set is a small registry, data-driven like the rest of the project's
registries (``conflict_rules``, ``body_metrics``): adding a fourth question is one
tuple entry, and everything — the summary line, the buttons, the sanitizer, the
settings form — follows from it.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date as date_type, timedelta
from typing import Any, Collection, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.app_settings import AppSetting
from vitals.models.signals import DayContext
from vitals.ownership import WriteIdentity
from vitals.services import signals_service
from vitals.services.proactive import compose
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.services.scoped_settings_service import (
    ScopedSettingKey,
    SettingScope,
    get_scoped_setting,
    set_scoped_setting,
    update_scoped_setting,
)
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
    in_template: bool = True  # can a weekday predict this in advance?
    # Answered about the day that just *ended*, not the one about to start. The
    # two are different questions wearing the same shape, and asking a
    # retrospective one in advance produces a guess dressed as an answer.
    retrospective: bool = False


# Order here is the order the summary line reads and the buttons render.
QUESTIONS: tuple[Question, ...] = (
    Question(
        "where",
        "office",
        {"office": "в офисе", "remote": "удалёнка", "off": "выходной"},
    ),
    Question("gym", False, {True: "зал", False: "без зала"}),
    # Not a plan — an outcome. Where he'll be and whether he'll train are known in
    # advance; how heavy the day turned out is knowable only once it has. Asking
    # it in the morning ("каким будет сегодня?") or the night before ("каким будет
    # завтра?") is asking him to predict the one thing nobody predicts, so it is
    # asked in the evening about the day just spent — which is also where «Как
    # день?» already asks it in words.
    Question(
        "load",
        "normal",
        {"light": "лёгкий день", "normal": "обычный день", "heavy": "тяжёлый день"},
        in_template=False,
        retrospective=True,
    ),
)

QUESTIONS_BY_KEY = {q.key: q for q in QUESTIONS}
TEMPLATE_QUESTIONS: tuple[Question, ...] = tuple(q for q in QUESTIONS if q.in_template)
# The two halves of the day, split by when they can honestly be answered. Every
# keyboard is built from one of them: Telegram attaches buttons to a *message*,
# not to a line, so a message that mixes both makes «тяжёлый день» and «удалёнка»
# look like answers to the same question.
PLAN_QUESTIONS: tuple[Question, ...] = tuple(q for q in QUESTIONS if not q.retrospective)
RECAP_QUESTIONS: tuple[Question, ...] = tuple(q for q in QUESTIONS if q.retrospective)


def questions_for(key: str) -> tuple[Question, ...]:
    """The keyboard a tap on ``key`` came from — so a redraw rebuilds that one."""
    question = QUESTIONS_BY_KEY.get(key)
    return RECAP_QUESTIONS if question is not None and question.retrospective else PLAN_QUESTIONS

WEEKEND: tuple[str, ...] = ("sat", "sun")

# Neutral except for the one thing the calendar does know: Saturday and Sunday
# are not office days. Guessing a gym schedule from nothing would just be a wrong
# answer pre-filled. The owner edits this in Settings, and until then
# the exception buttons are one tap away.
DEFAULT_DAY: dict[str, Any] = {q.key: q.default for q in TEMPLATE_QUESTIONS}
DEFAULT_TEMPLATE: dict[str, dict] = {
    day: {**DEFAULT_DAY, "where": "off" if day in WEEKEND else "office"}
    for day in WEEKDAYS
}


# ── The template ──────────────────────────────────────────────────────────────
def _sanitize_day(raw: Any, weekday: str) -> dict[str, Any]:
    """One weekday's answers, projected onto the template questions.

    Unknown keys are dropped and an out-of-registry value falls back to that
    weekday's default — this is a hand-editable JSON blob, so it is a trust
    boundary. Questions outside the template (``load``) are dropped here too:
    they belong to the day, not to the weekday.
    """
    day = dict(DEFAULT_TEMPLATE[weekday])
    if isinstance(raw, dict):
        for question in TEMPLATE_QUESTIONS:
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
    return {day: _sanitize_day(source.get(day), day) for day in WEEKDAYS}


async def get_week_template(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
) -> dict[str, dict]:
    """The stored template, or the neutral default.

    Legacy unscoped reads keep their historical soft fallback. Subject-scoped
    reads intentionally propagate ownership/bridge errors so a second subject
    cannot fall through to the installation-wide template.

    No Redis cache on purpose: this is read twice a day by two jobs.
    """
    if subject_id is not None:
        value = await get_scoped_setting(
            session,
            scope=SettingScope.SUBJECT,
            key=ScopedSettingKey.WEEK_TEMPLATE,
            subject_id=subject_id,
            default=None,
        )
        return sanitize_template(value)
    try:
        row = await session.get(AppSetting, SETTINGS_KEY)
    except Exception:
        logger.warning("week template: DB read failed; using defaults", exc_info=True)
        return sanitize_template(None)
    return sanitize_template(row.value if row is not None else None)


async def set_week_template(
    session: AsyncSession,
    template: Any,
    *,
    subject_id: uuid.UUID | None = None,
) -> dict[str, dict]:
    """Store the template (sanitized). Flushes; the caller commits."""
    clean = sanitize_template(template)
    if subject_id is not None:
        return await set_scoped_setting(
            session,
            scope=SettingScope.SUBJECT,
            key=ScopedSettingKey.WEEK_TEMPLATE,
            subject_id=subject_id,
            value=clean,
        )
    row = await session.get(AppSetting, SETTINGS_KEY)
    if row is None:
        session.add(AppSetting(key=SETTINGS_KEY, value=clean))
    else:
        row.value = clean  # a new dict, so SQLAlchemy sees the change
    await session.flush()
    return clean


async def update_week_template(
    session: AsyncSession,
    patch: dict[str, dict],
    *,
    subject_id: uuid.UUID,
) -> dict[str, dict]:
    """Atomically merge a partial template into its subject-scoped value."""

    def _merge(current: Any) -> dict[str, dict]:
        merged = sanitize_template(current)
        for day, values in patch.items():
            merged[day] = {**merged[day], **values}
        return sanitize_template(merged)

    return await update_scoped_setting(
        session,
        scope=SettingScope.SUBJECT,
        key=ScopedSettingKey.WEEK_TEMPLATE,
        subject_id=subject_id,
        default=None,
        update=_merge,
    )


def guess_for(template: dict[str, dict], on_date: date_type) -> dict[str, Any]:
    """What the template expects that date to be."""
    return dict(template.get(WEEKDAYS[on_date.weekday()], DEFAULT_DAY))


# ── The day's answers ─────────────────────────────────────────────────────────
async def resolve(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> tuple[dict[str, Any], set[str]]:
    """``(answers, answered)`` — his answer if there is one, else the guess, plus
    the set of questions he *actually* answered.

    Answered is per question, not per day. One tap says nothing about the
    other two questions, and a single flag for the whole day made "иду в зал"
    look like an answer to "где ты" and "как день" as well — which is how the
    keyboard used to disappear after the first tap.

    The set is empty when nothing was answered, so a caller that only cares
    whether *anything* was said can still read it as a boolean.

    An existing row with empty ``answers`` is *not* an answer: that is the row the
    evening block writes to park its guess.

    The unanswered half of the day still comes from the guess he was correcting
    (``planned`` — what the template said *when it asked*, which is what he saw).
    Falling back to the bare defaults here would let a single "иду в зал" quietly
    cancel the template's "удалёнка".
    """
    row = await signals_service.get_day_context(
        session,
        on_date,
        subject_id=subject_id,
    )
    guess = (
        dict(row.planned)
        if row is not None and row.planned
        else guess_for(
            await get_week_template(session, subject_id=subject_id),
            on_date,
        )
    )
    answers = dict(row.answers) if row is not None and row.answers else {}
    return {**guess, **answers}, set(answers)


async def record_answer(
    session: AsyncSession,
    on_date: date_type,
    key: str,
    value: Any,
    *,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None = None,
    allow_historical_connection: bool = False,
) -> DayContext:
    """One tap → one answer merged into that day, the guess preserved beside it."""
    # The service applies this guess only if no plan exists after it has acquired
    # the subject lock. Computing it eagerly is harmless; deciding whether to
    # write it here would race a concurrent evening plan.
    planned = guess_for(
        await get_week_template(session, subject_id=identity.subject_id),
        on_date,
    )
    return await signals_service.set_day_context(
        session,
        on_date,
        answers={key: value},
        planned=planned,
        source=source,
        identity=identity,
        integration_connection_id=integration_connection_id,
        merge_answers=True,
        planned_if_missing=True,
        allow_historical_connection=allow_historical_connection,
    )


async def record_plan(
    session: AsyncSession,
    on_date: date_type,
    planned: dict,
    *,
    ownership: ProactiveOwnershipContext,
) -> DayContext:
    """Park the template's guess for a day, without touching any answer."""
    return await signals_service.set_day_context(
        session,
        on_date,
        answers={},
        planned=planned,
        source=Source.TEMPLATE.value,
        identity=ownership.system_action(),
        integration_connection_id=None,
        merge_answers=True,
        preserve_source=True,
        planned_if_missing=True,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────
# The one line a tap changes, in each message that carries the buttons. Kept as
# data because :func:`redraw` has to find it again inside a message that was sent
# hours ago: a tap knows which day it answers, not which message it came from.
LINE_TOMORROW = "Завтра: "
LINE_TODAY = "Сегодня: "
LINE_TODAY_PLANNED = "Сегодня по шаблону: "
# Says what the keyboard is, because Telegram renders it under the *whole*
# message with nothing tying it to the line it belongs to. Only ever printed над
# a plan keyboard: those buttons really are corrections to the line above them.
# The recap keyboard needs no hint — «Как день?» sits directly on top of it.
HINT_FIX = "Не так? Поправь кнопками ниже."

# The evening's open question. A constant because the recap keyboard is glued to
# it: the buttons are the one-tap version of this exact sentence.
ASK_DAY = "Как день? Напиши пару слов — запишу."

# «по шаблону» is what the line says while the day is still a guess. A tap is the
# owner speaking, so the redrawn line stops calling his answer a template's.
_REDRAWN_PREFIX = {
    LINE_TOMORROW: LINE_TOMORROW,
    LINE_TODAY: LINE_TODAY,
    LINE_TODAY_PLANNED: LINE_TODAY,
}


def describe(answers: dict) -> str:
    """``{"where": "remote", "gym": False}`` → "удалёнка · без зала".

    Only what is actually known: a question with no answer prints nothing rather
    than its default. Otherwise every unanswered day would read "обычный день" —
    a guess stated as fact, about the one thing the template deliberately does
    not guess.
    """
    words = []
    for question in QUESTIONS:
        if question.key not in answers:
            continue
        word = question.labels.get(answers[question.key])
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


def exception_buttons(
    current: dict,
    on_date: date_type,
    answered: Collection[str] = (),
    questions: tuple[Question, ...] = PLAN_QUESTIONS,
) -> list[tuple[str, str]]:
    """One button per *other* answer — corrections, not a questionnaire.

    Re-asking what the template already knows would be taps a day for a guess
    that is right most of the time; offering only the exceptions makes the common
    case zero taps. A question with no answer at all (``load``, which no weekday
    predicts) has no "other" to be — so every one of its options is offered.

    ``answered`` drops the questions he has already spoken for. Only those:
    the keyboard used to vanish whole on the first tap, which left the other two
    questions with no way to be answered at all.

    ``questions`` is which half of the day this keyboard is about — the plan for a
    day ahead, or the recap of one just finished. It defaults to the plan because
    that is every caller but the evening recap.
    """
    buttons: list[tuple[str, str]] = []
    for question in questions:
        if question.key in answered:
            continue
        value = current.get(question.key)
        for option, label in question.labels.items():
            if option != value:
                buttons.append((label, f"ctx:{on_date.isoformat()}:{question.key}:{encode(option)}"))
    return buttons


def day_block(day: Optional[dict]) -> Optional[compose.Block]:
    """The brief's one line about today. ``None`` when there is nothing to say."""
    if not day:
        return None
    line = describe(day.get("answers") or {})
    if not line:
        return None
    prefix = LINE_TODAY if day.get("source") == Source.MANUAL.value else LINE_TODAY_PLANNED
    return compose.Block(compose.KIND_DAY, f"{prefix}{line}", 30)


def buttons_from_context(day: Optional[dict], on_date: date_type):
    """Exception buttons for a brief — one per plan question still unanswered.

    ``answered`` is stored alongside the answers in the brief's ``context_json``
    precisely so this can be rebuilt later from the stored row, without a second
    trip to the day-context table.

    Plan questions only, by way of ``exception_buttons``' default. The morning is
    the wrong moment to ask how heavy the day will be — that is the evening's
    question about the day it can actually see.
    """
    if not day:
        return None
    return (
        exception_buttons(
            day.get("answers") or {}, on_date, day.get("answered") or ()
        )
        or None
    )


def redraw(text: str, answers: dict, *, has_buttons: bool) -> str:
    """An already-sent message, with its day line telling the truth again.

    Only that one line is touched. A tap changes what the day *is*, not the step
    count or the «Как день?» above it, and rebuilding the whole message would mean
    re-fetching the Garmin row it was composed from — for a line the answer is
    already sitting in. The hint about the buttons leaves with the last button.
    """
    lines = []
    for line in text.split("\n"):
        prefix = next((p for p in _REDRAWN_PREFIX if line.startswith(p)), None)
        if prefix is not None:
            lines.append(_REDRAWN_PREFIX[prefix] + describe(answers))
        elif line == HINT_FIX and not has_buttons:
            continue
        else:
            lines.append(line)
    return "\n".join(lines)


def summary_line(garmin) -> str:
    """The day in the numbers that are actually about *today*.

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


def plan_dedupe_key(on_date: date_type) -> str:
    """The second evening message keys off the same date, separately — so a
    replayed job re-sends neither, and a budget that stopped the first one does
    not make the second look like it was already sent."""
    return f"evening-plan:{on_date.isoformat()}"


async def _complete_prepared_delivery(session_factory, prepared_delivery) -> None:
    """Finish one committed T1 capability without spanning provider I/O."""

    from vitals.services.proactive import channels, delivery

    async with session_factory() as session:
        dispatch_lease = await delivery.start_delivery_dispatch(
            session,
            prepared_delivery,
            notifier_resolver=channels.resolve_legacy_bound_notifier,
        )
        await session.commit()
    if dispatch_lease is None:
        return

    completion = await delivery.dispatch_delivery(dispatch_lease)
    for finalize_try in range(2):
        async with session_factory() as session:
            try:
                await delivery.finalize_delivery(session, completion)
                await session.commit()
                return
            except Exception:
                await session.rollback()
                if finalize_try:
                    raise


# ── The 23:45 job ─────────────────────────────────────────────────────────────
async def evening_job(session_factory, redis=None) -> None:
    """Close today, then ask about tomorrow — in that order, as two messages.

    Two, not one, because the two halves ask about different days and Telegram
    attaches a keyboard to a *message*, not to a line. Merged, «тяжёлый день» and
    «удалёнка» sit in one flat list and nothing can say which question either
    answers — the earlier wording fix papered over exactly this. The price is
    one more slot of the daily budget; the budget is a knob in Settings, and a
    message nobody can answer is worth less than the slot it costs.

    No Garmin pull in front of this one (unlike the brief): the 22:00 poll is
    recent enough for a day summary, and every extra pull is a login the account
    doesn't need to spend. No model call either — both messages are code.
    """
    from vitals.services import garmin_service
    from vitals.services.proactive import channels, delivery

    today = today_local()
    tomorrow = today + timedelta(days=1)
    recap_legacy_key = dedupe_key(today)
    recap_delivery_key = delivery.make_delivery_idempotency_key(
        "evening",
        today,
    )
    plan_legacy_key = plan_dedupe_key(today)
    plan_delivery_key = delivery.make_delivery_idempotency_key(
        "evening-plan",
        today,
    )

    # Compose from one committed domain snapshot before reserving delivery.
    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=None,
        )
        recap_answers, recap_answered = await resolve(
            session,
            today,
            subject_id=ownership.subject_id,
        )
        recap_blocks = [
            compose.Block(
                compose.KIND_DAY,
                summary_line(await garmin_service.get_daily(session, today)),
                10,
            ),
            compose.Block(compose.KIND_ASK, ASK_DAY, 20),
        ]
        recap_text = compose.render(recap_blocks)
        recap_buttons = exception_buttons(
            recap_answers,
            today,
            recap_answered,
            RECAP_QUESTIONS,
        ) or None
        await session.commit()

    # ── Today, in hindsight ───────────────────────────────────────────────
    # The recap keyboard is the one-tap version of «Как день?» directly above
    # it, about the day that just ended — the only moment that question can
    # be answered rather than guessed.
    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=None,
        )
        bound_notifier = await channels.build_legacy_bound_notifier(
            session,
            ownership,
        )
        prepared_recap = await delivery.prepare_delivery_intent(
            session,
            bound_notifier,
            text=recap_text,
            category=delivery.CATEGORY_EVENING,
            idempotency_key=recap_delivery_key,
            legacy_dedupe_key=recap_legacy_key,
            buttons=recap_buttons,
            ownership=ownership,
        )
        await session.commit()
    if prepared_recap is not None:
        await _complete_prepared_delivery(session_factory, prepared_recap)

    # Do not let the plan leapfrog an uncertain/cancelled/no-channel recap.
    # Existing legacy and durable SENT journals both satisfy this exact gate.
    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=None,
        )
        recap_journal = await delivery.confirmed_delivery_journal(
            session,
            idempotency_key=recap_delivery_key,
            legacy_dedupe_key=recap_legacy_key,
            category=delivery.CATEGORY_EVENING,
            ownership=ownership,
        )
        await session.commit()
    if recap_journal is None:
        return

    # ── Tomorrow, as planned ──────────────────────────────────────────────
    # Persist the domain plan in its own transaction before delivery T1.
    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=None,
        )
        planned = guess_for(
            await get_week_template(
                session,
                subject_id=ownership.subject_id,
            ),
            tomorrow,
        )
        await record_plan(
            session,
            tomorrow,
            planned,
            ownership=ownership,
        )

        answers, answered = await resolve(
            session,
            tomorrow,
            subject_id=ownership.subject_id,
        )
        buttons = exception_buttons(answers, tomorrow, answered) or None
        tomorrow_line = f"{LINE_TOMORROW}{describe(answers)}"
        if buttons:
            tomorrow_line += f"\n{HINT_FIX}"
        await session.commit()

    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=None,
        )
        bound_notifier = await channels.build_legacy_bound_notifier(
            session,
            ownership,
        )
        prepared_plan = await delivery.prepare_delivery_intent(
            session,
            bound_notifier,
            text=tomorrow_line,
            category=delivery.CATEGORY_EVENING,
            idempotency_key=plan_delivery_key,
            legacy_dedupe_key=plan_legacy_key,
            buttons=buttons,
            ownership=ownership,
        )
        await session.commit()
    if prepared_plan is not None:
        await _complete_prepared_delivery(session_factory, prepared_plan)
