"""The rail's status card — today's numbers, not today's plumbing.

The first version of this card reported how *fresh* each source was ("Labs · 99
days ago"), which is true every single day and useful on none of them. What the
chrome should answer without opening a page is "where am I right now": this
morning's weight and the week's direction, last night's sleep, today's intake,
the last session. Freshness only earns a line when a source has actually gone
quiet — then the number is replaced by how long it has been missing, which is the
one time that fact is worth the space.

Four small reads, one per domain, all against indexed date columns, so this is
cheap enough to run per page render (``web.deps.load_nav_status`` calls it for
HTML GETs only). Like ``modules_service.get_enabled_modules`` it NEVER raises —
the chrome must render even when a domain is unreadable, and a domain that
throws simply loses its row.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import decimal, plural, t
from vitals.services.modules_service import CORE_KEYS
from vitals.utils.timeutils import today_local

logger = logging.getLogger(__name__)

# Days a source may go quiet before its row reports the silence instead of a
# number. Garmin syncs nightly; a training week with three rest days is normal.
_QUIET_AFTER = {"recovery": 2, "workouts": 5}


@dataclass(frozen=True)
class StatRow:
    key: str            # i18n suffix — stat.<key>
    value: str          # the headline number, already formatted
    sub: str = ""       # quiet note on the right (delta, target, secondary)
    tone: str = ""      # '' | 'good' | 'bad' | 'warn'


def _ago(days: int) -> str:
    """"today" / "yesterday" / "N days ago"."""
    if days <= 0:
        return t("sync.today")
    if days == 1:
        return t("sync.yesterday")
    word = plural(days, t("sync.day_one"), t("sync.day_few"), t("sync.day_many"))
    return t("sync.days_ago", n=days, word=word)


def _enabled(em: dict[str, bool], key: str) -> bool:
    return bool(em.get(key, key in CORE_KEYS))


async def _weight_row(session: AsyncSession, subject_id) -> Optional[StatRow]:
    """Latest weight, and the week's direction beside it — the direction is the
    reason to look, not the number."""
    from vitals.services import weight_service

    today = today_local()
    logs = await weight_service.list_active_weights(
        session, start=today - timedelta(days=21), subject_id=subject_id
    )
    if not logs:
        return None
    latest = logs[-1]
    # Nearest reading at least a week older than the latest one — "a week ago"
    # has to survive gaps, and the day before yesterday is not a week.
    earlier = [w for w in logs if (latest.date - w.date).days >= 7]
    sub, tone = "", ""
    if earlier:
        delta = latest.weight_kg - earlier[-1].weight_kg
        # U+2212 minus, not a hyphen: at 11px a hyphen next to a digit reads as
        # a dash in the label, and the sign is the whole point of this note.
        sub = decimal(f"{delta:+.1f}".replace("-", "−"))
        tone = "good" if delta < 0 else ("bad" if delta > 0 else "")
    return StatRow(
        key="weight",
        value=f"{decimal(f'{latest.weight_kg:.1f}')} {t('common.kg')}",
        sub=sub,
        tone=tone,
    )


async def _recovery_row(session: AsyncSession, subject_id) -> Optional[StatRow]:
    """Last night's sleep, with training readiness as the note."""
    from vitals.services import garmin_service

    row = await garmin_service.latest_daily(session, subject_id=subject_id)
    if row is None:
        return None
    gap = (today_local() - row.date).days
    if gap > _QUIET_AFTER["recovery"]:
        return StatRow(key="recovery", value=_ago(gap), tone="warn")
    if not row.sleep_seconds:
        return None
    hours, minutes = divmod(row.sleep_seconds // 60, 60)
    sub = (
        t("stat.readiness", n=row.training_readiness)
        if row.training_readiness is not None
        else ""
    )
    return StatRow(key="recovery", value=f"{hours}:{minutes:02d}", sub=sub)


async def _nutrition_row(session: AsyncSession, subject_id) -> Optional[StatRow]:
    """Today's intake against the ceiling, protein beside it."""
    from vitals.services import nutrition_service

    summary = await nutrition_service.daily_summary(
        session, today_local(), subject_id=subject_id
    )
    if not summary["meal_count"]:
        return None
    totals, goals = summary["totals"], summary["goals"]
    calories = round(totals["calories"])
    ceiling = goals.get("calories_max")
    over = bool(ceiling and calories > ceiling)
    return StatRow(
        key="nutrition",
        value=f"{calories}{' / ' + str(round(ceiling)) if ceiling else ''} {t('common.kcal')}",
        sub=t("stat.protein", n=round(totals["protein_g"]), unit=t("common.g")),
        tone="bad" if over else "",
    )


async def _workouts_row(session: AsyncSession, subject_id) -> Optional[StatRow]:
    """When the last session was — the only workout fact worth a nav rail."""
    from vitals.services import hevy_service

    last = await hevy_service.latest_workout_date(session)
    if last is None:
        return None
    gap = (today_local() - last).days
    return StatRow(
        key="workouts",
        value=_ago(gap),
        tone="warn" if gap > _QUIET_AFTER["workouts"] else "",
    )


# Row order = reading order in the card. Each entry is (module key that gates it,
# builder); a builder returning None means "nothing to say yet", not an error.
_ROWS = (
    ("weight", _weight_row),
    ("garmin", _recovery_row),
    ("nutrition", _nutrition_row),
    ("hevy", _workouts_row),
)


async def rail_stats(
    session: AsyncSession,
    enabled: Optional[dict[str, bool]] = None,
    *,
    subject_id,
) -> list[StatRow]:
    """Today's readout for every enabled domain, in display order. Never raises.

    The card is one person's day, so every row is built inside their scope.
    """
    em = enabled or {}
    rows: list[StatRow] = []
    for module_key, build in _ROWS:
        if not _enabled(em, module_key):
            continue
        try:
            row = await build(session, subject_id)
        except Exception:
            logger.warning("nav status: %s row failed", module_key, exc_info=True)
            continue
        if row is not None:
            rows.append(row)
    return rows
