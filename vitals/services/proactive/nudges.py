"""Шов 2 — nudges as data: a list of specs, one engine that walks it.

A nudge is four things: when it applies (``condition``), what it says
(``render``), how often it may repeat (``cooldown_h``), and which switch turns it
off (``category``, wired to Settings in прогон 6). The engine knows none of them
— it walks the registry, checks the cooldown, asks the condition, and hands the
text to :mod:`delivery`, which owns quiet hours and the daily budget.

    Adding a nudge is one tuple entry. Removing one is deleting it. The engine
    does not change either way — the same trick already carrying ``conflict_rules``
    and ``body_metrics``.

**Conditions run on a schedule, so they must be honest about "not yet".** Every
one here checks the clock itself: a nudge about the day still being salvageable
is wrong at 23:50 and pointless at 09:00. And every one of them stays silent on
missing data — no steps row means the watch hasn't synced, not that he sat still.

Deliberately **not** here: injections, supplements, protocol. The owner's call,
recorded in the plan; the weekly digest is where the protocol gets discussed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.proactive import Notification
from vitals.services.proactive import channels, delivery
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

# Categories = the toggles the settings card will render (прогон 6). Grouping
# rather than one switch per nudge: "не пиши мне про активность" is the decision
# a person actually makes.
CATEGORY_ACTIVITY = "activity"
CATEGORY_NUTRITION = "nutrition"
CATEGORY_DATA = "data"

# Below this the evening walk is a real suggestion; above it he's basically there
# and a ping is noise. Not a настройка until it's ever wrong.
STEPS_TARGET = 8000
# Under this gap the protein reminder is pedantry — one more meal covers it anyway.
PROTEIN_MIN_GAP_G = 25.0
# Two calendar days without a row means the watch stopped syncing, not that a
# single poll failed.
GARMIN_SILENT_DAYS = 2


@dataclass(frozen=True)
class NudgeSpec:
    """One rule. ``condition`` may stash what it computed into ``ctx`` — it runs
    first and ``render`` reads the same dict, so neither has to query twice."""

    key: str
    category: str
    condition: Callable[[AsyncSession, dict], Awaitable[bool]]
    render: Callable[[dict], str]
    cooldown_h: int = 24


# ── The conditions ────────────────────────────────────────────────────────────
async def _steps_short(session: AsyncSession, ctx: dict) -> bool:
    """Evening, and today's steps are still well short of the target.

    Fires from 18:00: earlier is not yet news, and there has to be enough evening
    left to do something about it. Depends on the light pulse (N3) for a step
    count that isn't hours stale.
    """
    from vitals.services import garmin_service

    if not 18 <= ctx["now"].hour < 22:
        return False
    row = await garmin_service.get_daily(session, ctx["today"])
    steps = getattr(row, "steps", None)
    if not steps:  # no row, or a watch that hasn't synced a single step today
        return False
    ctx["steps"] = steps
    return steps < STEPS_TARGET


async def _protein_short(session: AsyncSession, ctx: dict) -> bool:
    """Protein is behind with the day still open.

    16:00–21:00 — late enough that the total means something, early enough that
    dinner can fix it. Requires at least *something* logged: an empty log is a
    day he didn't track, and nagging about that is a different (unwanted) product.
    """
    from vitals.config import load_config
    from vitals.services import nutrition_service

    if not 16 <= ctx["now"].hour < 21:
        return False
    meals = await nutrition_service.list_meals_for_date(session, ctx["today"])
    if not meals:
        return False
    eaten = sum(m.protein_g or 0 for m in meals)
    if not eaten:
        return False
    target = load_config().nutrition_protein_target_g
    ctx["protein_eaten"] = eaten
    ctx["protein_target"] = target
    return target - eaten >= PROTEIN_MIN_GAP_G


async def _garmin_silent(session: AsyncSession, ctx: dict) -> bool:
    """No Garmin row for two days — the data lake has stopped filling.

    Never fires when there is no Garmin data at all: that's an integration that
    was never set up, not one that broke.
    """
    from vitals.services import garmin_service

    row = await garmin_service.latest_daily(session, before_or_on=ctx["today"])
    if row is None:
        return False
    gap = (ctx["today"] - row.date).days
    if gap < GARMIN_SILENT_DAYS:
        return False
    ctx["garmin_last_date"] = row.date
    ctx["garmin_gap_days"] = gap
    return True


# ── The registry (N2) ─────────────────────────────────────────────────────────
NUDGES: tuple[NudgeSpec, ...] = (
    NudgeSpec(
        key="steps_short",
        category=CATEGORY_ACTIVITY,
        condition=_steps_short,
        render=lambda ctx: (
            f"Шагов сегодня {ctx['steps']} из {STEPS_TARGET}. "
            "Вечерняя прогулка закрывает разницу."
        ),
    ),
    NudgeSpec(
        key="protein_short",
        category=CATEGORY_NUTRITION,
        condition=_protein_short,
        render=lambda ctx: (
            f"Белок {ctx['protein_eaten']:g} из {ctx['protein_target']:g} г — "
            f"ещё {ctx['protein_target'] - ctx['protein_eaten']:g} г, и день закрыт."
        ),
    ),
    NudgeSpec(
        key="garmin_silent",
        category=CATEGORY_DATA,
        condition=_garmin_silent,
        render=lambda ctx: (
            f"Garmin молчит с {ctx['garmin_last_date'].strftime('%d.%m')} "
            f"({ctx['garmin_gap_days']} дня). Часы не синхронизировались."
        ),
    ),
)


# ── The engine ────────────────────────────────────────────────────────────────
def dedupe_key(key: str, now: datetime) -> str:
    """Hour-granular: the cooldown is what spaces nudges out, this only stops the
    same nudge going twice when a job run is replayed."""
    return f"nudge:{key}:{now:%Y-%m-%dT%H}"


async def last_sent_at(session: AsyncSession, key: str) -> Optional[datetime]:
    result = await session.execute(
        select(Notification.sent_at)
        .where(Notification.dedupe_key.like(f"nudge:{key}:%"))
        .order_by(Notification.sent_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def run(
    session: AsyncSession,
    notifier: Any,
    *,
    now: Optional[datetime] = None,
    today: Optional[date_type] = None,
) -> list[Notification]:
    """Walk the registry once. Returns the journal rows for what actually went out.

    A condition that raises is skipped, not fatal: one broken rule must not take
    the other nudges — or the job — down with it. Does not commit.
    """
    now = now or now_local()
    ctx: dict = {"now": now, "today": today or now.date()}

    sent: list[Notification] = []
    for spec in NUDGES:
        last = await last_sent_at(session, spec.key)
        if last is not None and now - last < timedelta(hours=spec.cooldown_h):
            continue
        try:
            if not await spec.condition(session, ctx):
                continue
            text = spec.render(ctx)
        except Exception:  # noqa: BLE001
            logger.warning("nudge %s failed to evaluate; skipped", spec.key, exc_info=True)
            continue

        row = await delivery.send(
            session,
            notifier,
            text=text,
            category=delivery.CATEGORY_NUDGE,
            dedupe_key=dedupe_key(spec.key, now),
            now=now,
        )
        if row is not None:
            sent.append(row)
    return sent


async def nudges_job(session_factory, redis=None) -> None:
    """Hourly. Cheap by design — most runs evaluate three conditions and send
    nothing, and the ones that would send are still subject to the daily budget
    (which the brief and the evening block have already taken two of).

    No channel configured → not even a DB session: this is the state the app is
    in before the bot exists, and it must cost nothing."""
    notifier = channels.build_notifier()
    if notifier is None:
        return
    async with session_factory() as session:
        await run(session, notifier)
        await session.commit()
