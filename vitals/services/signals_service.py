"""Signals service — capture free text, keep the original, fold keys on read.

Three things this module is responsible for, in the order they matter:

1. **Nothing is lost.** ``ingest_text`` writes the incoming message to
   ``raw_payloads`` *before* it tries to parse it. If the parser throws, times
   out, or returns junk, the raw row is already committed-able — the message is
   still there to re-parse later. This is the same data-lake rule the Garmin/Hevy
   importers follow, just with the LLM as the "upstream API".
2. **One phrase can be several facts.** «Голова раскалывается, спал 4 часа, кофе
   в 22» is three rows. They share a ``batch_id``, which is the unit the "не то"
   button cancels — a model that misread the sentence usually misread all of it,
   and picking one row out of three inside Telegram is misery. Pinpoint edits
   live on the ``/signals`` page instead.
3. **Keys stay free — for now.** The owner's call: collect a month of real
   phrasings, then consolidate (прогон 7). ``KEY_ALIASES`` is what keeps that
   from being a migration: keys are stored as the parser wrote them and folded to
   a canonical name **on read**, so adding an alias silently fixes every chart and
   export at once. Nothing here needs to know the final registry.

``misparse`` rows are excluded from chart/analysis reads but never deleted: they
are the actual evidence the key registry gets built from.
"""
from __future__ import annotations

import inspect
from datetime import date as date_type, time as time_type
from typing import Awaitable, Callable, Optional, Sequence, Union
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, SignalKind, Source
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import DayContext, Signal
from vitals.services.raw_payload_service import upsert_raw_payload
from vitals.utils.timeutils import now_local, today_local

DOMAIN = Domain.SIGNALS.value

_VALID_KINDS = {k.value for k in SignalKind}

# ── Шов 4: key aliases ────────────────────────────────────────────────────────
# alias (as some parse once wrote it) → canonical key. Applied on **read** only.
# Grow this dict during the shake-out month; at the end of прогон 7 the registry
# gets closed and the parser is forbidden from inventing new keys. Until then the
# stored value is left exactly as written so the drift stays visible.
KEY_ALIASES: dict[str, str] = {
    "sleepy": "sleepiness",
    "sleepy_af": "sleepiness",
    "drowsiness": "sleepiness",
    "head_ache": "headache",
    "headaches": "headache",
    "coffee_late": "caffeine_late",
    "late_coffee": "caffeine_late",
    "energy": "energy_level",
}


def normalize_key(key: str) -> str:
    """Canonical form of a stored key. Cheap slug hygiene + the alias table."""
    slug = slug_key(key)
    return KEY_ALIASES.get(slug, slug)


def slug_key(key: str) -> str:
    """Write-side hygiene: lowercase, trimmed, spaces/dashes → underscore.

    Keeps the column tidy without deciding anything about *which* key it is —
    that is ``normalize_key``'s job, and it happens on read.
    """
    return "_".join(str(key).strip().lower().replace("-", " ").split())


def _stored_keys_for(canonical: str) -> set[str]:
    """Every stored spelling that folds to ``canonical`` — for WHERE key IN (…)."""
    canonical = normalize_key(canonical)
    return {canonical} | {a for a, c in KEY_ALIASES.items() if c == canonical}


# ── Capture ───────────────────────────────────────────────────────────────────
Parser = Callable[[str], Union[Sequence[dict], Awaitable[Sequence[dict]]]]


async def store_raw_text(
    session: AsyncSession,
    *,
    text: str,
    external_id: Optional[str] = None,
    source: str = Source.TELEGRAM.value,
) -> RawPayload:
    """Park the incoming message in ``raw_payloads`` before anything can fail.

    ``external_id`` is the channel's own message id when there is one, so a
    webhook retry refreshes the same raw row instead of adding a duplicate.
    """
    return await upsert_raw_payload(
        session,
        domain=DOMAIN,
        source=source,
        external_id=external_id or uuid4().hex,
        payload={"text": text, "received_at": now_local().isoformat(timespec="seconds")},
    )


def _coerce_item(item: dict) -> Optional[dict]:
    """Validate one parsed fact. ``None`` = unusable, drop it (raw still has it).

    The parser is an LLM, so this is a trust boundary: a bad ``kind`` or a missing
    ``key`` means the row would be unreadable downstream, and inventing a default
    would quietly poison the charts.
    """
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in _VALID_KINDS:
        return None
    key = slug_key(item.get("key") or "")
    if not key:
        return None

    value_num = item.get("value_num")
    try:
        value_num = float(value_num) if value_num is not None else None
    except (TypeError, ValueError):
        value_num = None

    at_time = item.get("at_time")
    if isinstance(at_time, str):
        try:
            at_time = time_type.fromisoformat(at_time)
        except ValueError:
            at_time = None
    elif not isinstance(at_time, time_type):
        at_time = None

    unit = item.get("unit")
    note = item.get("note")
    return {
        "kind": kind,
        "key": key[:64],
        "value_num": value_num,
        "unit": str(unit)[:16] if unit else None,
        "note": str(note) if note else None,
        "at_time": at_time,
    }


async def create_signals(
    session: AsyncSession,
    *,
    items: Sequence[dict],
    on_date: Optional[date_type] = None,
    source: str = Source.TELEGRAM.value,
    raw_id: Optional[int] = None,
    batch_id: Optional[str] = None,
) -> list[Signal]:
    """Persist a parsed batch. Every row of one message shares a ``batch_id``."""
    on_date = on_date or today_local()
    batch_id = batch_id or uuid4().hex
    rows: list[Signal] = []
    for item in items:
        fields = _coerce_item(item)
        if fields is None:
            continue
        row = Signal(
            date=on_date,
            domain=DOMAIN,
            source=source,
            raw_id=raw_id,
            batch_id=batch_id,
            **fields,
        )
        session.add(row)
        rows.append(row)
    if rows:
        await session.flush()
    return rows


async def ingest_text(
    session: AsyncSession,
    *,
    text: str,
    parse: Parser,
    external_id: Optional[str] = None,
    on_date: Optional[date_type] = None,
    source: str = Source.TELEGRAM.value,
) -> list[Signal]:
    """The one entry point for incoming free text (D6 + D7).

    Raw first, parse second — deliberately in that order and in *this* function
    rather than at the call site, so no future caller can get the order wrong.
    A parser that raises costs the batch, never the message.
    """
    raw = await store_raw_text(session, text=text, external_id=external_id, source=source)

    try:
        parsed = parse(text)
        if inspect.isawaitable(parsed):
            parsed = await parsed
    except Exception:
        # The message survives in raw_payloads; a re-parse sweep can pick it up
        # (``processed_at IS NULL``). Nothing to salvage here.
        return []

    return await create_signals(
        session,
        items=parsed or [],
        on_date=on_date,
        source=source,
        raw_id=raw.id,
    )


async def delete_signal(session: AsyncSession, signal_id: int) -> bool:
    """Pinpoint removal from the ``/signals`` page — the counterpart of the "не то"
    button, which cancels a whole batch.

    A real delete, unlike ``misparse``: this is for a row that is simply *wrong*
    and shouldn't feed the key registry either. The raw message stays in
    ``raw_payloads`` regardless, so nothing said is ever lost.
    """
    row = await session.get(Signal, signal_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def mark_misparse(session: AsyncSession, batch_id: str) -> int:
    """"Не то" — drop the whole batch out of charts, keep the rows and the raw text."""
    result = await session.execute(
        update(Signal).where(Signal.batch_id == batch_id).values(misparse=True)
    )
    await session.flush()
    return result.rowcount or 0


# ── Read ──────────────────────────────────────────────────────────────────────
async def list_signals(
    session: AsyncSession,
    *,
    key: Optional[str] = None,
    kind: Optional[str] = None,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    include_misparse: bool = False,
    limit: int = 200,
) -> list[Signal]:
    """Newest first. ``key`` matches every stored spelling that folds to it."""
    stmt = select(Signal).where(Signal.domain == DOMAIN)
    if not include_misparse:
        stmt = stmt.where(Signal.misparse.is_(False))
    if key is not None:
        stmt = stmt.where(Signal.key.in_(_stored_keys_for(key)))
    if kind is not None:
        stmt = stmt.where(Signal.kind == kind)
    if start is not None:
        stmt = stmt.where(Signal.date >= start)
    if end is not None:
        stmt = stmt.where(Signal.date <= end)
    stmt = stmt.order_by(Signal.date.desc(), Signal.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def key_frequency(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    include_misparse: bool = True,
) -> list[tuple[str, int]]:
    """``[(canonical_key, count)]``, most-used first — the прогон-7 raw material.

    Counts default to **including** misparses: the point of this list is to see
    what the parser actually emits, mistakes included.
    """
    stmt = select(Signal.key, func.count()).where(Signal.domain == DOMAIN)
    if not include_misparse:
        stmt = stmt.where(Signal.misparse.is_(False))
    if start is not None:
        stmt = stmt.where(Signal.date >= start)
    if end is not None:
        stmt = stmt.where(Signal.date <= end)
    stmt = stmt.group_by(Signal.key)

    # Aliases fold in Python — the DB groups the stored spellings, we merge them.
    # A month of one person's messages is a few hundred rows; nothing to optimize.
    folded: dict[str, int] = {}
    for stored_key, count in (await session.execute(stmt)).all():
        canonical = normalize_key(stored_key)
        folded[canonical] = folded.get(canonical, 0) + count
    return sorted(folded.items(), key=lambda kv: (-kv[1], kv[0]))


# ── Day context ───────────────────────────────────────────────────────────────
async def set_day_context(
    session: AsyncSession,
    on_date: date_type,
    *,
    answers: dict,
    planned: Optional[dict] = None,
    source: str = Source.MANUAL.value,
) -> DayContext:
    """Upsert the day's context — the latest answer wins, nothing is versioned.

    ``planned`` (what the week template had guessed) is kept alongside so the
    template can later learn from where it was wrong; passing ``None`` leaves any
    existing guess in place rather than erasing it.
    """
    row = await get_day_context(session, on_date)
    if row is None:
        row = DayContext(date=on_date, domain=DOMAIN, source=source, answers=answers)
        session.add(row)
    else:
        row.answers = answers
        row.source = source
    if planned is not None:
        row.planned = planned
    await session.flush()
    return row


async def get_day_context(
    session: AsyncSession, on_date: date_type
) -> Optional[DayContext]:
    result = await session.execute(select(DayContext).where(DayContext.date == on_date))
    return result.scalars().first()
