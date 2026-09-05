"""Шов 2 — nudges as data: a list of specs, one engine that walks it.

A nudge is four things: when it applies (``condition``), what it says
(``render``), how often it may repeat (``cooldown_h``), and which switch turns it
off (``category``, a toggle on the settings card). The engine knows none of them
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

from vitals.services.proactive.delivery import contracts as delivery_contracts
from vitals.services.proactive.delivery import policy as delivery_policy
from vitals.services.proactive.delivery import queries as delivery_queries
from vitals.services.proactive.delivery import preparation as delivery_preparation
from vitals.services.proactive.delivery import dispatch as delivery_dispatch
from vitals.services.proactive.delivery import legacy as delivery_legacy

from vitals.services.nutrition import analytics as nutrition_analytics
from vitals.services.nutrition import queries as nutrition_queries
from vitals.services.modules import preferences as module_preferences

import logging
from dataclasses import dataclass
from datetime import date as date_type, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.proactive import Notification
from vitals.services.proactive import channels
from vitals.services.proactive.preferences import contracts as preference_contracts
from vitals.services.proactive.preferences import legacy as preference_legacy
from vitals.services.proactive.preferences import queries as preference_queries
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

# Categories = the toggles the settings card renders. They are defined
# in ``prefs`` — a category is a setting first — and re-exported here so a nudge
# spec reads as one thing.
CATEGORY_ACTIVITY = preference_contracts.CATEGORY_ACTIVITY
CATEGORY_NUTRITION = preference_contracts.CATEGORY_NUTRITION
CATEGORY_DATA = preference_contracts.CATEGORY_DATA

# Below this the evening walk is a real suggestion; above it he's basically there
# and a ping is noise. Not a настройка until it's ever wrong.
STEPS_TARGET = 8000
# Under this gap the protein reminder is pedantry — one more meal covers it anyway.
PROTEIN_MIN_GAP_G = 25.0
# Two calendar days without a row means the watch stopped syncing, not that a
# single poll failed.
GARMIN_SILENT_DAYS = 2
# The condition has to look up its own past sends, so the key is a constant
# rather than a literal repeated in two places that must never drift apart.
GARMIN_SILENT_KEY = "garmin_silent"


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
    from vitals.services.garmin import queries as garmin_queries

    if not 18 <= ctx["now"].hour < 22:
        return False
    ownership = ctx.get("ownership")
    if ownership is None:
        return False
    row = await garmin_queries.get_daily(
        session,
        ctx["today"],
        subject_id=ownership.subject_id,
    )
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


    if not 16 <= ctx["now"].hour < 21:
        return False
    ownership = ctx.get("ownership")
    if ownership is None:
        # A protein nudge is about one person's day; without a subject there is
        # nobody to be behind on it.
        return False
    meals = await nutrition_queries.list_meals_for_date(
        session, ctx["today"], subject_id=ownership.subject_id
    )
    if not meals:
        return False
    eaten = sum(m.protein_g or 0 for m in meals)
    if not eaten:
        return False
    goals = await nutrition_analytics.get_goals(
        session, subject_id=ownership.subject_id
    )
    target = goals["protein_target_g"]
    ctx["protein_eaten"] = eaten
    ctx["protein_target"] = target
    return target - eaten >= PROTEIN_MIN_GAP_G


async def _garmin_silent(session: AsyncSession, ctx: dict) -> bool:
    """No Garmin row for two days — the data lake has stopped filling.

    Never fires when there is no Garmin data at all: that's an integration that
    was never set up, not one that broke.

    Once per episode of silence, not once a day. The 24-hour cooldown alone
    re-sent the same sentence every morning for as long as the watch stayed on
    the charger, and there is nothing new to say until it syncs again. The
    episode is identified by the last row's date: a send that landed on or after
    the first day *this* gap could have fired was about this silence — an older
    episode's message could not have gone out that late, because by then
    ``latest_daily`` was already returning this newer row.
    """
    from vitals.services.garmin import queries as garmin_queries

    ownership = ctx.get("ownership")
    if ownership is None:
        # Whose watch went quiet? Without a subject the question has no answer,
        # and the newest row in the database is somebody else's evidence.
        return False
    row = await garmin_queries.latest_daily(
        session, subject_id=ownership.subject_id, before_or_on=ctx["today"]
    )
    if row is None:
        return False
    gap = (ctx["today"] - row.date).days
    if gap < GARMIN_SILENT_DAYS:
        return False
    # The once-per-episode check lives on the owned send path, which reads the
    # same episode start through the delivery policy clock.
    ctx["garmin_episode_start"] = row.date + timedelta(days=GARMIN_SILENT_DAYS)
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
        key=GARMIN_SILENT_KEY,
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


async def last_sent_at(
    session: AsyncSession,
    key: str,
    *,
    ownership: ProactiveOwnershipContext | None = None,
) -> Optional[datetime]:
    legacy_prefix = f"nudge:{key}:"
    escaped_prefix = (
        legacy_prefix.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    query = select(Notification.sent_at).where(
        Notification.dedupe_key.like(f"{escaped_prefix}%", escape="\\")
    )
    if ownership is not None:
        query = query.where(
            delivery_policy.notification_ownership_scope(
                ownership,
                connection_scoped=False,
            )
        )
    result = await session.execute(
        query.order_by(Notification.sent_at.desc()).limit(1)
    )
    return result.scalars().first()


async def run(
    session: AsyncSession,
    notifier: Any,
    *,
    now: Optional[datetime] = None,
    today: Optional[date_type] = None,
    ownership: ProactiveOwnershipContext | None = None,
) -> list[Notification]:
    """Walk the registry once. Returns the journal rows for what actually went out.

    A condition that raises is skipped, not fatal: one broken rule must not take
    the other nudges — or the job — down with it. The zero-subject compatibility
    path leaves commit ownership with its caller; the owned path commits each
    durable T1/T2/T3 boundary so no Telegram await spans a database transaction.
    """
    now = now or now_local()
    ctx: dict = {
        "now": now,
        "today": today or now.date(),
        "ownership": ownership,
    }
    if ownership is None:
        categories = (await preference_legacy.get_pre_identity_legacy_prefs(session))[
            "nudges"
        ]
        enabled_categories = {
            key for key, enabled in categories.items() if enabled
        }
    else:
        enabled_categories = set(
            (
                await preference_queries.get_subject_policy(
                    session,
                    subject_id=ownership.subject_id,
                )
            ).enabled_nudge_categories
        )
        enabled_modules = await module_preferences.get_enabled_modules(
            session,
            subject_id=ownership.subject_id,
        )
        if not enabled_modules.get("nutrition", False):
            # The category toggle controls whether a wanted Nutrition nudge may
            # speak. The module gate controls whether this service may inspect
            # Nutrition at all; turning the module off wins.
            enabled_categories.discard(CATEGORY_NUTRITION)

    sent: list[Notification] = []
    for spec in NUDGES:
        if spec.category not in enabled_categories:
            continue
        policy_at = None
        if ownership is not None:
            policy_at, subject_local_now = await delivery_queries.delivery_policy_clock(
                session,
                ownership=ownership,
                now=now,
            )
            ctx["now"] = subject_local_now.replace(tzinfo=None)
            ctx["today"] = today or subject_local_now.date()
        else:
            last = await last_sent_at(
                session,
                spec.key,
                ownership=None,
            )
            if last is not None and now - last < timedelta(hours=spec.cooldown_h):
                continue
        try:
            if not await spec.condition(session, ctx):
                continue
            text = spec.render(ctx)
        except Exception:  # noqa: BLE001
            logger.warning("nudge %s failed to evaluate; skipped", spec.key)
            continue

        if ownership is None:
            row = await delivery_legacy.send(
                session,
                notifier,
                text=text,
                category=delivery_contracts.CATEGORY_NUDGE,
                dedupe_key=dedupe_key(spec.key, now),
                now=now,
                ownership=None,
            )
            if row is not None:
                sent.append(row)
            continue

        assert policy_at is not None
        policy_key = delivery_contracts.make_delivery_policy_key("nudge", spec.key)
        not_before = policy_at - timedelta(hours=spec.cooldown_h)
        episode_start = ctx.pop("garmin_episode_start", None)
        if spec.key == GARMIN_SILENT_KEY and episode_start is not None:
            episode_at, _ = await delivery_queries.delivery_policy_clock(
                session,
                ownership=ownership,
                now=datetime.combine(episode_start, datetime.min.time()),
            )
            not_before = min(not_before, episode_at)
        claimed = await delivery_queries.delivery_policy_claimed_since(
            session,
            policy_key=policy_key,
            not_before=not_before,
            ownership=ownership,
            legacy_dedupe_prefix=f"nudge:{spec.key}:",
        )
        if claimed:
            await session.commit()
            continue

        bound_notifier = await channels.build_legacy_bound_notifier(
            session,
            ownership,
        )
        subject_local_now = ctx["now"]
        legacy_key = dedupe_key(spec.key, subject_local_now)
        prepared_delivery = await delivery_preparation.prepare_delivery_intent(
            session,
            bound_notifier,
            text=text,
            category=delivery_contracts.CATEGORY_NUDGE,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "nudge",
                spec.key,
                subject_local_now.date(),
                subject_local_now.hour,
            ),
            policy_key=policy_key,
            legacy_dedupe_key=legacy_key,
            now=policy_at,
            ownership=ownership,
        )
        await session.commit()
        if prepared_delivery is None:
            continue

        dispatch_lease = await delivery_dispatch.start_delivery_dispatch(
            session,
            prepared_delivery,
            now=policy_at,
            notifier_resolver=channels.resolve_legacy_bound_notifier,
        )
        await session.commit()
        if dispatch_lease is None:
            continue

        completion = await delivery_dispatch.dispatch_delivery(dispatch_lease)
        for finalize_try in range(2):
            try:
                row = await delivery_dispatch.finalize_delivery(session, completion)
                await session.commit()
                if row is not None:
                    sent.append(row)
                break
            except Exception:
                await session.rollback()
                if finalize_try:
                    raise
    return sent


async def nudges_job(session_factory, redis=None, *, subject_id) -> None:
    """Hourly. Cheap by design — most runs evaluate three conditions and send
    nothing, and the ones that would send are still subject to the daily budget
    (which the brief and the evening block have already taken two of).

    Transport availability is proved only after resolving exact S/Q/C ownership;
    the strict builder returns ``None`` without reserving or sending when the
    recipient credential is unavailable."""
    async with session_factory() as session:
        ownership = await channels.resolve_subject_channel_ownership(
            session,
            subject_id=subject_id,
        )
        try:
            await run(
                session,
                None,
                ownership=ownership,
            )
        except preference_contracts.ProactivePreferencesNotConfiguredError:
            # A newly provisioned subject opts into proactive delivery by
            # saving notification settings. Until then this is a no-op, not an
            # operational failure worth surfacing on their dashboard.
            await session.commit()
            return
        await session.commit()
