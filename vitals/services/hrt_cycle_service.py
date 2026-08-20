"""HRT cycles — protocol plans, the schedule engine, and the active-release model.

Three concerns:

  * **Cycles** — CRUD over ``HrtCycle`` + its per-compound ``HrtCycleItem`` plans.
    Adding an open-ended cycle closes the previous open one the day before, so at
    most one protocol is "current" (mirrors GLP-1 dose phases).
  * **Schedule engine** — :func:`expand_schedule` turns an item's segment list
    (flat ``{dose, interval_days, duration_days}`` or a linear ramp with
    ``dose_start``/``dose_end``/``step``) into concrete planned administrations
    off a **fixed grid anchored at the cycle start** (a late real injection never
    shifts the grid). Fractional intervals (E3.5D) round to whole calendar days.
  * **Active-release model** — :func:`release_series` sums each administration's
    exponential decay (``0.5 ** (Δdays / half_life_days)``) scaled by the
    compound's active-hormone fraction, over actual logged doses plus (optionally)
    the active cycle's future plan. Illustrative only — real levels come from Labs.
"""
from __future__ import annotations

import math
import uuid
from datetime import date as date_type, timedelta
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import CycleKind, DoseUnit, Source
from vitals.models.hrt import DOMAIN, HrtCompound, HrtCycle, HrtCycleItem, HrtDose
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, hrt_service
from vitals.utils.timeutils import today_local

# Guard against a pathological schedule (tiny interval, huge window) looping
# unboundedly — no real protocol produces this many shots in one segment.
_MAX_ADMIN_PER_SEGMENT = 100_000
_VALID_UNITS = frozenset(unit.value for unit in DoseUnit)


def _normalized_unit(value: str | None, *, default: str) -> str:
    unit = (value or default).strip().lower()
    if unit not in _VALID_UNITS:
        raise ValueError(f"unknown dose unit: {unit!r}")
    return unit


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None,
    prepared: conflict_engine.PreparedConflictWrite | None,
    include_legacy_unowned: bool,
) -> conflict_engine.ConflictWriteContext | None:
    """Validate the opaque writer before any scoped target is queried.

    Cycles do not have one conflict-evaluation date: a mutation may describe a
    whole range.  The prepared capability is therefore the governance/subject
    serialization proof for these non-conflict writes; its date is deliberately
    not compared with a cycle boundary.
    """

    if identity is None and prepared is None:
        if include_legacy_unowned:
            raise ValueError("legacy HRT compatibility requires a WriteIdentity")
        return None
    if identity is None or prepared is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped HRT cycle writes require identity and a prepared conflict write"
        )
    context = conflict_engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )
    if (
        include_legacy_unowned
        and context.legacy_bridge
        is not conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
    ):
        raise conflict_engine.ConflictPreparedWriteError(
            "legacy HRT cycle access requires a fully-unowned bridge"
        )
    return context


def _subject_scope(model, subject_id: uuid.UUID, *, include_legacy_unowned: bool):
    exact = model.subject_id == subject_id
    if not include_legacy_unowned:
        return exact
    legacy = model.subject_id.is_(None)
    if hasattr(model, "actor_user_id"):
        legacy = and_(legacy, model.actor_user_id.is_(None))
    return or_(exact, legacy)


def _row_in_scope(
    row,
    *,
    subject_id: uuid.UUID,
    include_legacy_unowned: bool,
) -> bool:
    if row.subject_id == subject_id:
        return True
    if not include_legacy_unowned or row.subject_id is not None:
        return False
    return not hasattr(row, "actor_user_id") or row.actor_user_id is None


def _validate_cycle_graph(
    cycle: HrtCycle,
    items: Sequence[HrtCycleItem],
    *,
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
) -> None:
    if subject_id is None:
        return
    if not _row_in_scope(
        cycle,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    ):
        raise conflict_engine.ConflictScopeError(
            "HRT cycle is outside the requested subject scope"
        )
    for item in items:
        if not _row_in_scope(
            item,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        ):
            raise conflict_engine.ConflictScopeError(
                "HRT cycle contains an item outside the requested subject scope"
            )


def _adopt_cycle_graph(
    cycle: HrtCycle,
    items: Sequence[HrtCycleItem],
    *,
    identity: WriteIdentity,
) -> None:
    """Fill only nullable S roots; historical actor/source never change."""

    if cycle.subject_id is None:
        cycle.subject_id = identity.subject_id
    for item in items:
        if item.subject_id is None:
            item.subject_id = identity.subject_id


async def _lock_cycle_graph(
    session: AsyncSession,
    cycle_id: int,
    *,
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
) -> tuple[HrtCycle, list[HrtCycleItem]] | None:
    stmt = select(HrtCycle).where(
        HrtCycle.id == cycle_id,
        HrtCycle.domain == DOMAIN,
    )
    if subject_id is not None:
        stmt = stmt.where(
            _subject_scope(
                HrtCycle,
                subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
        )
    elif include_legacy_unowned:
        raise ValueError("legacy HRT compatibility requires a subject_id")
    cycle = await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    if cycle is None:
        return None
    items = list(
        await session.scalars(
            select(HrtCycleItem)
            .where(HrtCycleItem.cycle_id == cycle.id)
            .order_by(HrtCycleItem.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    _validate_cycle_graph(
        cycle,
        items,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    return cycle, items


async def _lock_cycle_graph_for_item(
    session: AsyncSession,
    item_id: int,
    *,
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
) -> tuple[HrtCycle, list[HrtCycleItem], HrtCycleItem] | None:
    stmt = (
        select(HrtCycle)
        .join(HrtCycleItem, HrtCycleItem.cycle_id == HrtCycle.id)
        .where(HrtCycleItem.id == item_id, HrtCycle.domain == DOMAIN)
    )
    if subject_id is not None:
        stmt = stmt.where(
            _subject_scope(
                HrtCycle,
                subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
        )
    elif include_legacy_unowned:
        raise ValueError("legacy HRT compatibility requires a subject_id")
    cycle = await session.scalar(
        stmt.limit(1)
        .with_for_update(of=HrtCycle)
        .execution_options(populate_existing=True)
    )
    if cycle is None:
        return None
    items = list(
        await session.scalars(
            select(HrtCycleItem)
            .where(HrtCycleItem.cycle_id == cycle.id)
            .order_by(HrtCycleItem.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    _validate_cycle_graph(
        cycle,
        items,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    item = next((row for row in items if row.id == item_id), None)
    if item is None:
        return None
    return cycle, items, item


async def _resolve_scoped_compound(
    session: AsyncSession,
    key: str,
    *,
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
) -> Optional[HrtCompound]:
    if subject_id is None:
        return await hrt_service.get_compound(session, key)

    from vitals.services.hrt_catalog import load_compound_catalog

    curated_keys = tuple(dict(load_compound_catalog()))
    same_subject = HrtCompound.subject_id == subject_id
    curated_global = and_(
        HrtCompound.subject_id.is_(None),
        HrtCompound.actor_user_id.is_(None),
        HrtCompound.domain == DOMAIN,
        HrtCompound.source == Source.SYSTEM.value,
        HrtCompound.key.in_(curated_keys),
    )
    permitted = or_(same_subject, curated_global)
    if include_legacy_unowned:
        permitted = or_(
            permitted,
            and_(
                HrtCompound.subject_id.is_(None),
                HrtCompound.actor_user_id.is_(None),
            ),
        )
    compound = await session.scalar(
        select(HrtCompound)
        .where(HrtCompound.key == key, permitted)
        .order_by(HrtCompound.id)
        .limit(1)
    )
    if compound is not None:
        if compound.subject_id is None and compound.key in curated_keys:
            hrt_service._require_curated_compound_integrity(compound)
        return compound
    collision = await session.scalar(
        select(HrtCompound.id).where(HrtCompound.key == key).limit(1)
    )
    if collision is not None:
        raise conflict_engine.ConflictScopeError(
            "HRT compound belongs to another subject scope"
        )
    return None


def _source_value(source: str | Source) -> str:
    try:
        return Source(source).value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown HRT source: {source!r}") from exc


def validate_schedule(schedule: object) -> list[dict]:
    """Validate a segment list and return a normalized copy (known keys only,
    numbers coerced). Raises ``ValueError`` with a per-segment message on bad
    shape. Write paths (form, MCP, template import) all funnel through this so a
    hand-crafted JSON payload can't smuggle a malformed segment into the DB."""
    if not isinstance(schedule, (list, tuple)) or not schedule:
        raise ValueError("schedule must be a non-empty list of segments")
    out: list[dict] = []
    last_idx = len(schedule) - 1
    for idx, seg in enumerate(schedule):
        where = f"segment {idx + 1}"
        if not isinstance(seg, dict):
            raise ValueError(f"{where}: must be an object")
        clean: dict = {}
        is_flat = seg.get("dose") is not None
        is_ramp = seg.get("dose_start") is not None or seg.get("dose_end") is not None
        if is_flat == is_ramp:
            raise ValueError(
                f"{where}: give either dose (flat) or dose_start+dose_end (ramp)"
            )
        try:
            if is_flat:
                clean["dose"] = float(seg["dose"])
                if clean["dose"] <= 0:
                    raise ValueError
            else:
                clean["dose_start"] = float(seg["dose_start"])
                clean["dose_end"] = float(seg["dose_end"])
                if clean["dose_start"] <= 0 or clean["dose_end"] <= 0:
                    raise ValueError
                if seg.get("step") is not None:
                    clean["step"] = abs(float(seg["step"]))
                if seg.get("step_every_days") is not None:
                    clean["step_every_days"] = float(seg["step_every_days"])
                    if clean["step_every_days"] <= 0:
                        raise ValueError
        except (TypeError, ValueError, KeyError):
            raise ValueError(f"{where}: doses must be positive numbers") from None
        try:
            interval = float(seg.get("interval_days") or 1)
        except (TypeError, ValueError):
            raise ValueError(f"{where}: interval_days must be a positive number") from None
        if interval <= 0:
            raise ValueError(f"{where}: interval_days must be a positive number")
        clean["interval_days"] = interval
        duration = seg.get("duration_days")
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                raise ValueError(f"{where}: duration_days must be a positive integer") from None
            if duration <= 0:
                raise ValueError(f"{where}: duration_days must be a positive integer")
            clean["duration_days"] = duration
        elif idx != last_idx:
            raise ValueError(f"{where}: only the last segment may omit duration_days")
        out.append(clean)
    return out


# ── Schedule engine (pure) ────────────────────────────────────────────────────
def _dose_at(seg: dict, elapsed_days: float, interval: float) -> float:
    """Dose for an administration ``elapsed_days`` into its segment. Flat segments
    return a constant; ramp segments step ``dose_start`` toward ``dose_end`` by
    ``step`` every ``step_every_days`` (or every interval), clamped to the range."""
    if seg.get("dose") is not None:
        return float(seg["dose"])
    start = float(seg["dose_start"])
    finish = float(seg["dose_end"])
    step = abs(float(seg.get("step") or 0))
    every = float(seg.get("step_every_days") or interval or 1)
    if every <= 0:
        every = interval or 1.0
    n = int(elapsed_days // every) if step > 0 else 0
    direction = 1.0 if finish >= start else -1.0
    value = start + direction * step * n
    low, high = (start, finish) if start <= finish else (finish, start)
    return max(low, min(high, value))


def expand_schedule(
    schedule: Optional[Sequence[dict]],
    anchor: date_type,
    start: date_type,
    end: date_type,
) -> list[tuple[date_type, float]]:
    """Expand a segment list into ``(date, dose)`` planned administrations within
    ``[start, end]``, off a fixed grid anchored at ``anchor``. Segments run in
    order; each occupies ``duration_days`` from where the previous ended. The last
    segment may omit ``duration_days`` to run open-ended to ``end``."""
    out: list[tuple[date_type, float]] = []
    if not schedule:
        return out
    total_window = (end - anchor).days
    seg_offset = 0
    last_idx = len(schedule) - 1
    for idx, seg in enumerate(schedule):
        interval = float(seg.get("interval_days") or 1)
        if interval <= 0:
            interval = 1.0
        duration = seg.get("duration_days")
        is_last = idx == last_idx
        if not duration and not is_last:
            # A non-last open segment is malformed (later segments would have no
            # start) — skip it rather than loop forever.
            continue
        seg_span = float(duration) if duration else float(total_window - seg_offset) + 1.0
        k = 0
        while k < _MAX_ADMIN_PER_SEGMENT:
            elapsed = k * interval
            if elapsed >= seg_span:
                break
            adm_date = anchor + timedelta(days=seg_offset + int(round(elapsed)))
            if adm_date > end:
                break
            if adm_date >= start:
                out.append((adm_date, _dose_at(seg, elapsed, interval)))
            k += 1
        if duration:
            seg_offset += int(duration)
        else:
            break
    return out


def expand_item_schedule(
    item: HrtCycleItem, anchor: date_type, start: date_type, end: date_type
) -> list[tuple[date_type, float]]:
    """Expand an item's schedule off the cycle anchor shifted by the item's own
    ``start_offset_days`` — a compound may join the protocol mid-cycle (e.g.
    winstrol from week 5). Every consumer (planned overlay, release curve,
    injection reminder) goes through here, so the offset applies uniformly."""
    offset = int(item.start_offset_days or 0)
    return expand_schedule(item.schedule, anchor + timedelta(days=offset), start, end)


# ── Cycle CRUD ────────────────────────────────────────────────────────────────
async def list_cycles(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Sequence[HrtCycle]:
    stmt = select(HrtCycle).where(HrtCycle.domain == DOMAIN)
    if subject_id is not None:
        stmt = stmt.where(
            _subject_scope(
                HrtCycle,
                subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
        )
    elif include_legacy_unowned:
        raise ValueError("legacy HRT compatibility requires a subject_id")
    cycles = list(
        await session.scalars(
            stmt.order_by(HrtCycle.start_date.desc(), HrtCycle.id.desc())
            .execution_options(populate_existing=True)
        )
    )
    for cycle in cycles:
        _validate_cycle_graph(
            cycle,
            cycle.items,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
    return cycles


async def active_cycle(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Optional[HrtCycle]:
    """The cycle covering ``on_date`` (today by default). The newest match wins —
    ordered by start date then id, so a same-day supersede picks the one created
    last."""
    day = on_date or today_local()
    cycles = await list_cycles(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    for cycle in cycles:
        if cycle.start_date <= day and (cycle.end_date is None or day <= cycle.end_date):
            return cycle
    return None


async def add_cycle(
    session: AsyncSession,
    *,
    kind: str,
    start_date: date_type,
    name: Optional[str] = None,
    end_date: Optional[date_type] = None,
    note: Optional[str] = None,
    source: str | Source = Source.MANUAL.value,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> HrtCycle:
    """Create a cycle. An open-ended one closes every other still-open cycle so at
    most one protocol is current — the day before the new one starts, but never
    before the old cycle's own start (a same-day supersede clamps to the start
    date, which is why the new cycle wins the ``active_cycle`` id tie-break)."""
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
        include_legacy_unowned=include_legacy_unowned,
    )
    valid_kinds = {k.value for k in CycleKind}
    if kind not in valid_kinds:
        raise ValueError(f"kind must be one of: {', '.join(sorted(valid_kinds))}")
    if end_date is not None and end_date < start_date:
        raise ValueError("end_date cannot be before the cycle's start date")
    if end_date is None:
        stmt = (
            select(HrtCycle)
            .where(HrtCycle.domain == DOMAIN, HrtCycle.end_date.is_(None))
        )
        if identity is not None:
            stmt = stmt.where(
                _subject_scope(
                    HrtCycle,
                    identity.subject_id,
                    include_legacy_unowned=include_legacy_unowned,
                )
            )
        open_cycles = list(
            await session.scalars(
                stmt.order_by(HrtCycle.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        for open_cycle in open_cycles:
            graph = await _lock_cycle_graph(
                session,
                open_cycle.id,
                subject_id=identity.subject_id if identity is not None else None,
                include_legacy_unowned=include_legacy_unowned,
            )
            assert graph is not None
            locked_cycle, locked_items = graph
            if identity is not None:
                _adopt_cycle_graph(locked_cycle, locked_items, identity=identity)
            open_cycle = locked_cycle
            open_cycle.end_date = max(
                open_cycle.start_date, start_date - timedelta(days=1)
            )

    cycle = HrtCycle(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        domain=DOMAIN,
        source=_source_value(source),
        name=name,
        kind=kind,
        start_date=start_date,
        end_date=end_date,
        note=note,
    )
    session.add(cycle)
    await session.flush()
    return cycle


async def add_cycle_item(
    session: AsyncSession,
    cycle_id: int,
    *,
    compound_key: str,
    schedule: list[dict],
    unit: Optional[str] = None,
    start_offset_days: int = 0,
    note: Optional[str] = None,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> Optional[HrtCycleItem]:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
        include_legacy_unowned=include_legacy_unowned,
    )
    graph = await _lock_cycle_graph(
        session,
        cycle_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if graph is None:
        return None
    cycle, items = graph
    key = (compound_key or "").strip()
    if not key:
        raise ValueError("compound_key is required")
    schedule = validate_schedule(schedule)
    offset = int(start_offset_days or 0)
    if offset < 0:
        raise ValueError("start_offset_days must be >= 0")
    compound = await _resolve_scoped_compound(
        session,
        key,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if identity is not None:
        _adopt_cycle_graph(cycle, items, identity=identity)
    item = HrtCycleItem(
        subject_id=cycle.subject_id,
        cycle=cycle,
        compound_id=compound.id if compound else None,
        compound_key=key,
        unit=_normalized_unit(
            unit,
            default=(compound.dose_unit if compound else DoseUnit.MG.value),
        ),
        start_offset_days=offset,
        schedule=schedule,
        note=note,
    )
    session.add(item)
    await session.flush()
    return item


async def update_cycle_item(
    session: AsyncSession,
    item_id: int,
    *,
    schedule: Optional[list[dict]] = None,
    unit: Optional[str] = None,
    start_offset_days: Optional[int] = None,
    note: Optional[str] = None,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> Optional[HrtCycleItem]:
    """Edit an item in place — dose tweaks mid-course shouldn't require
    delete + re-add. ``None`` keeps the current value; a new ``schedule`` goes
    through the same validation as on create."""
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
        include_legacy_unowned=include_legacy_unowned,
    )
    graph = await _lock_cycle_graph_for_item(
        session,
        item_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if graph is None:
        return None
    cycle, items, item = graph
    if identity is not None:
        _adopt_cycle_graph(cycle, items, identity=identity)
    if schedule is not None:
        item.schedule = validate_schedule(schedule)
    if unit is not None:
        item.unit = _normalized_unit(unit, default=item.unit)
    if start_offset_days is not None:
        offset = int(start_offset_days)
        if offset < 0:
            raise ValueError("start_offset_days must be >= 0")
        item.start_offset_days = offset
    if note is not None:
        item.note = note or None
    await session.flush()
    return item


async def close_cycle(
    session: AsyncSession,
    cycle_id: int,
    *,
    end_date: date_type,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> Optional[HrtCycle]:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
        include_legacy_unowned=include_legacy_unowned,
    )
    graph = await _lock_cycle_graph(
        session,
        cycle_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if graph is None:
        return None
    cycle, items = graph
    if identity is not None:
        _adopt_cycle_graph(cycle, items, identity=identity)
    # An inverted range would make the cycle silently vanish from history.
    if end_date < cycle.start_date:
        raise ValueError("end_date cannot be before the cycle's start date")
    cycle.end_date = end_date
    await session.flush()
    return cycle


async def delete_cycle(
    session: AsyncSession,
    cycle_id: int,
    *,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
        include_legacy_unowned=include_legacy_unowned,
    )
    graph = await _lock_cycle_graph(
        session,
        cycle_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if graph is None:
        return False
    cycle, _items = graph
    await session.delete(cycle)
    await session.flush()
    return True


async def delete_cycle_item(
    session: AsyncSession,
    item_id: int,
    *,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
        include_legacy_unowned=include_legacy_unowned,
    )
    graph = await _lock_cycle_graph_for_item(
        session,
        item_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if graph is None:
        return False
    _cycle, _items, item = graph
    await session.delete(item)
    await session.flush()
    return True


# ── Planned administrations (from the active cycle) ───────────────────────────
async def planned_administrations(
    session: AsyncSession,
    *,
    start: date_type,
    end: date_type,
    cycle: Optional[HrtCycle] = None,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> list[dict]:
    """Planned administrations from the active cycle within ``[start, end]``, one
    entry per shot: ``{date, compound_key, unit, dose}``. Empty when no cycle is
    active. Each item is anchored at the cycle's start (fixed grid). Pass the
    already-loaded ``cycle`` when the caller has it (the dashboard does) to skip
    the re-fetch."""
    if cycle is None:
        cycle = await active_cycle(
            session,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
    if cycle is None:
        return []
    _validate_cycle_graph(
        cycle,
        cycle.items,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    window_start = max(start, cycle.start_date)
    window_end = min(end, cycle.end_date) if cycle.end_date else end
    out: list[dict] = []
    for item in cycle.items:
        for adm_date, dose in expand_item_schedule(
            item, cycle.start_date, window_start, window_end
        ):
            out.append(
                {"date": adm_date, "compound_key": item.compound_key,
                 "unit": item.unit, "dose": dose}
            )
    out.sort(key=lambda a: a["date"])
    return out


# ── Active-release model ──────────────────────────────────────────────────────
def _active_mg(dose: float, unit: str, compound: Optional[HrtCompound]) -> Optional[float]:
    """Active-hormone mg an administration contributes to the release curve, or
    ``None`` if it can't be modelled (non-mg unit, or no half-life/fraction —
    e.g. GH in IU, peptides in mcg, or a free-text compound not in the catalog)."""
    if unit != DoseUnit.MG.value or compound is None:
        return None
    if compound.half_life_hours is None or not compound.half_life_hours:
        return None
    fraction = compound.active_fraction if compound.active_fraction is not None else 1.0
    return float(dose) * float(fraction)


async def _actual_contributions(
    session: AsyncSession,
    *,
    end: date_type,
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
) -> list[tuple[date_type, float, float, str]]:
    """Actual logged doses up to ``end`` as ``(date, active_mg, half_life_days,
    compound_class)`` — only those that can be modelled (mg + known half-life)."""
    stmt = select(HrtDose).where(HrtDose.date <= end)
    if subject_id is not None:
        stmt = stmt.where(
            _subject_scope(
                HrtDose,
                subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
        )
    elif include_legacy_unowned:
        raise ValueError("legacy HRT compatibility requires a subject_id")
    doses = list(await session.scalars(stmt))
    contribs: list[tuple[date_type, float, float, str]] = []
    for dose_row in doses:
        compound = await _resolve_scoped_compound(
            session,
            dose_row.compound_key,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
        if dose_row.compound_id is not None and (
            compound is None or compound.id != dose_row.compound_id
        ):
            raise conflict_engine.ConflictScopeError(
                "HRT dose references a compound outside the subject scope"
            )
        active = _active_mg(dose_row.dose, dose_row.unit, compound)
        if active is None:
            continue
        contribs.append(
            (dose_row.date, active, compound.half_life_hours / 24.0, compound.compound_class)
        )
    return contribs


async def _planned_contributions(
    session: AsyncSession,
    *,
    start: date_type,
    end: date_type,
    cycle: Optional[HrtCycle] = None,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> list[tuple[date_type, float, float, str]]:
    """Future planned administrations (from the active cycle) as release
    contributions, resolving each item's compound for half-life/fraction."""
    if cycle is None:
        cycle = await active_cycle(
            session,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
    if cycle is None:
        return []
    _validate_cycle_graph(
        cycle,
        cycle.items,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    contribs: list[tuple[date_type, float, float, str]] = []
    for item in cycle.items:
        compound = await _resolve_scoped_compound(
            session,
            item.compound_key,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
        window_start = max(start, cycle.start_date)
        window_end = min(end, cycle.end_date) if cycle.end_date else end
        for adm_date, dose in expand_item_schedule(
            item, cycle.start_date, window_start, window_end
        ):
            active = _active_mg(dose, item.unit, compound)
            if active is None:
                continue
            contribs.append(
                (adm_date, active, compound.half_life_hours / 24.0, compound.compound_class)
            )
    return contribs


async def release_series(
    session: AsyncSession,
    *,
    start: date_type,
    end: date_type,
    step_days: int = 1,
    include_planned: bool = True,
    cycle: Optional[HrtCycle] = None,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> list[dict]:
    """Daily active-hormone-in-body estimate over ``[start, end]``. Sums the
    exponential decay of every modelable administration (actual up to ``end``,
    plus future planned from the active cycle when ``include_planned``). Returns
    ``[{date, total_mg, by_class}]`` — total plus a per-compound-class split so a
    chart can stack testosterone vs 19-nors etc. Pure read; writes nothing."""
    contribs = await _actual_contributions(
        session,
        end=end,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    if include_planned:
        today = today_local()
        for adm_date, active, hl, cls in await _planned_contributions(
            session,
            start=today + timedelta(days=1),
            end=end,
            cycle=cycle,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        ):
            contribs.append((adm_date, active, hl, cls))

    series: list[dict] = []
    day = start
    while day <= end:
        total = 0.0
        by_class: dict[str, float] = {}
        for adm_date, active, hl, cls in contribs:
            if adm_date > day or hl <= 0:
                continue
            remaining = active * math.pow(0.5, (day - adm_date).days / hl)
            total += remaining
            by_class[cls] = by_class.get(cls, 0.0) + remaining
        series.append({
            "date": day.isoformat(),
            "total_mg": round(total, 2),
            "by_class": {k: round(v, 2) for k, v in by_class.items()},
        })
        day += timedelta(days=step_days)
    return series
