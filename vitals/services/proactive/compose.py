"""Шов 3 — a message is a list of blocks; text happens once, at the very end.

The deterministic blocks are built here by code, from the same cross-domain
context the weekly digest assembles. The model contributes exactly **one** block
— the interpretation — and it is appended by :mod:`brief`, not by this module.

That split is the whole point:

  * the model stays silent or OpenRouter is down → that one block is simply
    absent and everything else still goes out (the fallback the weekly digest
    never had);
  * changing what the brief *contains* is reordering or dropping blocks, without
    touching the model or the delivery channel.

Numbers reach the model already computed, so there is nothing for it to get
wrong; the header prints them itself rather than trusting prose about them.

Text is Russian and inline, like :mod:`inbound` — the bot has one reader. The
web UI keeps going through ``i18n``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from typing import Iterable, Optional

# Block kinds (also the priority ladder below).
KIND_GARMIN = "garmin"
KIND_WEIGHT = "weight"
KIND_DAY = "day"        # what kind of day it is — see ``day_plan``
KIND_ASK = "ask"        # a question to the owner, answered in free text
KIND_NARRATIVE = "narrative"

# Protocol stays out of the brief entirely (B2): no doses, no compounds, no
# injection schedule, no supplements. Nothing here is advisable day-to-day, it
# would fight the weekly digest for the same conclusions, and the most sensitive
# rows in the lake have no business travelling through Telegram. The digest still
# sees all of it — this filter is only applied on the way into a brief.
PROTOCOL_KEYS = ("glp1", "hrt", "supplements")

# How stale the newest Garmin row may be and still count as "there is data".
# ``latest_daily`` happily returns a row from three weeks ago, so an empty-day
# check that only looked for *a* row would never fire.
FRESH_DAYS = 1

_RECOVERY_KEYS = (
    "sleep_score",
    "hrv_avg",
    "resting_hr",
    "body_battery_high",
)


@dataclass(frozen=True)
class Block:
    """One piece of a message. ``priority`` orders the render, low first."""

    kind: str
    text: str
    priority: int = 50


def render(blocks: Iterable[Block]) -> str:
    """Blocks → the string that actually gets sent. The only place text is joined."""
    ordered = sorted(blocks, key=lambda b: b.priority)
    return "\n\n".join(b.text.strip() for b in ordered if b.text and b.text.strip())


def strip_protocol(ctx: dict) -> dict:
    """The brief's view of the context — everything except the protocol."""
    return {k: v for k, v in ctx.items() if k not in PROTOCOL_KEYS}


def is_empty_day(ctx: dict, *, on_date: date_type) -> bool:
    """No sleep and nothing new → there is no brief worth sending (B7).

    Silence is more honest than "нет данных" three mornings in a row; the web
    gets a passive ``info`` alert instead so the gap is still visible.
    """
    garmin = ctx.get("garmin") or {}
    row_date = _parse_date(garmin.get("date"))
    if row_date is None or (on_date - row_date).days > FRESH_DAYS:
        return True
    return all(garmin.get(key) is None for key in _RECOVERY_KEYS)


def header_blocks(ctx: dict) -> list[Block]:
    """The deterministic header (B2): recovery numbers, then weight and its trend."""
    blocks: list[Block] = []

    garmin = ctx.get("garmin") or {}
    parts = [
        label + " " + _num(garmin[key])
        for key, label in (
            ("sleep_score", "Сон"),
            ("hrv_avg", "HRV"),
            ("resting_hr", "Пульс покоя"),
            ("body_battery_high", "Body Battery"),
        )
        if garmin.get(key) is not None
    ]
    if parts:
        blocks.append(Block(KIND_GARMIN, " · ".join(parts), 10))

    weight = ctx.get("weight") or {}
    wparts = []
    if weight.get("latest_kg") is not None:
        wparts.append(f"Вес {_num(weight['latest_kg'])} кг")
    slope = weight.get("trend_kg_per_week")
    if slope is not None:
        wparts.append(f"тренд {slope:+.2f} кг/нед")
    if wparts:
        line = " · ".join(wparts)
        # A noise marker means the scale is lying in a known direction, so the
        # trend must never be printed bare next to it — that is the one number in
        # the header that can mislead while being technically correct.
        marker = (weight.get("noise_markers") or [None])[0]
        if marker and slope is not None:
            reason = (marker.get("reason") or "").strip()
            line += f"\n⚠️ тренд зашумлён{f': {reason}' if reason else ''}"
        blocks.append(Block(KIND_WEIGHT, line, 20))

    return blocks


def _num(value) -> str:
    """``86.0 → "86"``, ``86.1 → "86.1"`` — no trailing-zero noise in a message."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _parse_date(value) -> Optional[date_type]:
    try:
        return date_type.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
