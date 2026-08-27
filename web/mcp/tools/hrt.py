"""HRT MCP tool registration without a reverse dependency on the router."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Source
from vitals.services.conflicts import engine
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.services.hrt import cycles, records
from vitals.utils.timeutils import today_local


@dataclass(frozen=True)
class HrtToolDependencies:
    """Router-owned module gate, identity, and serialization seams."""

    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    conflict_write_context: Callable[..., Awaitable[Any]]
    conflict_payload: Callable[[ConflictBlocked], dict]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]
    gated: Callable[[str], Callable[[Any], Any]]


@dataclass(frozen=True)
class RegisteredHrtTools:
    get_hrt_logs: Callable[..., Awaitable[dict]]
    log_hrt_dose: Callable[..., Awaitable[dict]]
    add_hrt_cycle: Callable[..., Awaitable[dict]]
    add_hrt_cycle_item: Callable[..., Awaitable[dict]]
    update_hrt_dose: Callable[..., Awaitable[dict]]
    log_hrt_side_effect: Callable[..., Awaitable[dict]]
    close_hrt_cycle: Callable[..., Awaitable[dict]]
    get_hrt_cycles: Callable[..., Awaitable[dict]]


def register_hrt_tools(server: Any, deps: HrtToolDependencies) -> RegisteredHrtTools:
    """Register the frozen HRT surface in its existing order."""

    @server.tool()
    @deps.gated("hrt")
    async def get_hrt_logs(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """Retrieves HRT/TRT dose administrations, side effects, and the active cycle
        with its per-compound plan. Doses/side effects default to the most recent 100.
        READ tool."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            scope_kwargs = {"subject_id": scope.subject_id}
            doses = await records.list_doses(
                session,
                start=start,
                end=end,
                limit=limit,
                **scope_kwargs,
            )
            effects = await records.list_side_effects(
                session,
                start=start,
                end=end,
                limit=limit,
                **scope_kwargs,
            )
            active = await cycles.active_cycle(session, **scope_kwargs)
            active_cycle = None
            if active is not None:
                active_cycle = deps.serialize_row(active)
                active_cycle["items"] = [
                    deps.serialize_row(item) for item in active.items
                ]

            return {
                "doses": [deps.serialize_row(dose) for dose in doses],
                "side_effects": [deps.serialize_row(effect) for effect in effects],
                "active_cycle": active_cycle,
            }

    @server.tool()
    @deps.gated("hrt")
    async def log_hrt_dose(
        compound_key: str,
        dose: Optional[float] = None,
        unit: Optional[str] = None,
        volume_ml: Optional[float] = None,
        concentration_mg_ml: Optional[float] = None,
        on_date: Optional[str] = None,
        brand: Optional[str] = None,
        lab: Optional[str] = None,
        batch: Optional[str] = None,
        site: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Records an HRT/TRT administration. ``compound_key`` is a catalog slug (e.g.
        'testosterone_enanthate'). Give either ``dose`` (in ``unit`` — mg/iu/mcg) or a
        ``volume_ml`` with ``concentration_mg_ml`` (or the catalog concentration) to
        compute mg. Grey-market ``brand``/``lab``/``batch`` are optional. WRITE tool —
        on a hard block returns ``{"blocked": true, ...}``; retry with
        ``override=True``."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, today_local(), field="on_date")

        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(
                session,
                evaluation_date=parsed_date,
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            try:
                row = await records.log_dose(
                    session,
                    compound_key=compound_key,
                    on_date=parsed_date,
                    dose=dose,
                    unit=unit,
                    volume_ml=volume_ml,
                    concentration_mg_ml=concentration_mg_ml,
                    brand=brand,
                    lab=lab,
                    batch=batch,
                    site=site,
                    note=note,
                    override=override,
                    source=Source.MCP.value,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("hrt")
    async def add_hrt_cycle(
        kind: str,
        start_date: Optional[str] = None,
        name: Optional[str] = None,
        end_date: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        """Starts an HRT cycle (``kind``: course | pct — put nuance like TRT/blast/
        cruise in ``name``). An open-ended cycle closes the previous open one. WRITE
        tool. Add compounds with ``add_hrt_cycle_item``."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, today_local(), field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(
                session,
                evaluation_date=start,
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            try:
                cycle = await cycles.add_cycle(
                    session,
                    kind=kind,
                    start_date=start,
                    name=name,
                    end_date=end,
                    note=note,
                    source=Source.MCP.value,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, cycle)

    @server.tool()
    @deps.gated("hrt")
    async def add_hrt_cycle_item(
        cycle_id: int,
        compound_key: str,
        schedule: Optional[list] = None,
        dose: Optional[float] = None,
        interval_days: Optional[float] = None,
        duration_days: Optional[int] = None,
        start_offset_days: Optional[int] = None,
        unit: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        """Adds a compound plan to a cycle. Pass a full ``schedule`` (a list of
        segments — flat ``{dose, interval_days, duration_days}`` or a linear ramp
        ``{dose_start, dose_end, step, step_every_days, interval_days, duration_days}``)
        for titration/ramps, or the simple ``dose``+``interval_days`` for one flat
        segment. ``start_offset_days`` delays the compound's grid relative to the
        cycle start (week 5 → 28) for staggered courses. WRITE tool."""
        if not schedule:
            if dose is None or interval_days is None:
                return {"error": "provide schedule, or both dose and interval_days"}
            segment: dict = {"dose": dose, "interval_days": interval_days}
            if duration_days:
                segment["duration_days"] = int(duration_days)
            schedule = [segment]

        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(
                session,
                evaluation_date=today_local(),
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            try:
                item = await cycles.add_cycle_item(
                    session,
                    cycle_id,
                    compound_key=compound_key,
                    schedule=schedule,
                    unit=unit,
                    start_offset_days=int(start_offset_days or 0),
                    note=note,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            if item is None:
                return {"error": f"cycle {cycle_id} not found"}
            await session.commit()
            return await deps.serialize_written(session, item)

    @server.tool()
    @deps.gated("hrt")
    async def update_hrt_dose(
        dose_id: int,
        compound_key: Optional[str] = None,
        dose: Optional[float] = None,
        unit: Optional[str] = None,
        volume_ml: Optional[float] = None,
        concentration_mg_ml: Optional[float] = None,
        on_date: Optional[str] = None,
        brand: Optional[str] = None,
        lab: Optional[str] = None,
        batch: Optional[str] = None,
        site: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Updates a recorded HRT/TRT administration by ID. Only the fields you pass are
        changed; everything left out keeps its stored value, including the dose's own
        date. A new ``volume_ml`` or ``concentration_mg_ml`` without a ``dose`` recomputes
        the mg. WRITE tool — on a hard block returns ``{"blocked": true, ...}``; retry
        with ``override=True``."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, field="on_date")

        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(
                session,
                evaluation_date=parsed_date or today_local(),
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            current = await records.get_dose_for_update(
                session,
                dose_id,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
            if current is None:
                return {"error": f"HRT dose {dose_id} not found"}
            final_date = current.date if parsed_date is None else parsed_date
            if conflict_context.evaluation_date != final_date:
                conflict_context = engine.ConflictWriteContext(
                    identity=conflict_context.identity,
                    evaluation_date=final_date,
                    legacy_bridge=conflict_context.legacy_bridge,
                )
                prepared = await engine.prepare_scoped_write(
                    session,
                    context=conflict_context,
                )
            merged = {
                "compound_key": (
                    current.compound_key if compound_key is None else compound_key
                ),
                "dose": current.dose if dose is None else dose,
                "unit": current.unit if unit is None else unit,
                "volume_ml": current.volume_ml if volume_ml is None else volume_ml,
                "concentration_mg_ml": (
                    current.concentration_mg_ml
                    if concentration_mg_ml is None
                    else concentration_mg_ml
                ),
                "brand": current.brand if brand is None else brand,
                "lab": current.lab if lab is None else lab,
                "batch": current.batch if batch is None else batch,
                "site": current.site if site is None else site,
                "note": current.note if note is None else note,
            }
            # A new volume or concentration is a request to recompute the mg, and an
            # explicit dose wins over both — so carrying the stored one forward here
            # would silently ignore what the call actually changed.
            if dose is None and (
                volume_ml is not None or concentration_mg_ml is not None
            ):
                merged["dose"] = None
            try:
                row = await records.update_dose(
                    session,
                    dose_id,
                    on_date=final_date,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                    **merged,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("hrt")
    async def log_hrt_side_effect(
        effect_type: str,
        severity: int,
        on_date: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        """Records an HRT/TRT side effect (e.g. "акне", "отёки") with a severity 1–5 for
        a date (default today). Distinct from ``log_side_effect``, which belongs to
        GLP-1. WRITE tool — saved immediately."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, today_local(), field="on_date")

        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(
                session,
                evaluation_date=parsed_date,
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            try:
                row = await records.log_side_effect(
                    session,
                    on_date=parsed_date,
                    effect_type=effect_type,
                    severity=severity,
                    note=note,
                    source=Source.MCP.value,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("hrt")
    async def close_hrt_cycle(
        cycle_id: int,
        end_date: Optional[str] = None,
    ) -> dict:
        """Closes an HRT cycle by giving it an end date (default today). WRITE tool."""
        session_factory = deps.get_session_factory()
        end = deps.parse_date(end_date, today_local(), field="end_date")

        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(
                session,
                evaluation_date=end,
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            try:
                cycle = await cycles.close_cycle(
                    session,
                    cycle_id,
                    end_date=end,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            if cycle is None:
                return {"error": f"cycle {cycle_id} not found"}
            await session.commit()
            return await deps.serialize_written(session, cycle)

    @server.tool()
    @deps.gated("hrt")
    async def get_hrt_cycles() -> dict:
        """Lists all HRT cycles (newest first) with their per-compound plans. READ tool."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            cycle_rows = await cycles.list_cycles(
                session,
                subject_id=scope.subject_id,
            )
            out = []
            for cycle in cycle_rows:
                row = deps.serialize_row(cycle)
                row["items"] = [deps.serialize_row(item) for item in cycle.items]
                out.append(row)
            return {"cycles": out}

    return RegisteredHrtTools(
        get_hrt_logs=get_hrt_logs,
        log_hrt_dose=log_hrt_dose,
        add_hrt_cycle=add_hrt_cycle,
        add_hrt_cycle_item=add_hrt_cycle_item,
        update_hrt_dose=update_hrt_dose,
        log_hrt_side_effect=log_hrt_side_effect,
        close_hrt_cycle=close_hrt_cycle,
        get_hrt_cycles=get_hrt_cycles,
    )


__all__ = ["HrtToolDependencies", "RegisteredHrtTools", "register_hrt_tools"]
