"""GLP-1 MCP tool registration without router or ORM dependencies."""
from __future__ import annotations

from vitals.services.glp1 import queries as glp1_queries
from vitals.services.glp1 import writes as glp1_writes

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Source

from vitals.services.conflicts import engine
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.utils.timeutils import today_local


@dataclass(frozen=True)
class Glp1ToolDependencies:
    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    conflict_write_context: Callable[..., Awaitable[Any]]
    conflict_payload: Callable[[ConflictBlocked], dict]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]
    gated: Callable[[str], Callable[[Any], Any]]


@dataclass(frozen=True)
class RegisteredGlp1ReadTools:
    get_glp1_logs: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredGlp1InjectionTools:
    log_glp1: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredGlp1MaintenanceTools:
    update_glp1: Callable[..., Awaitable[dict]]
    log_side_effect: Callable[..., Awaitable[dict]]
    add_dose_phase: Callable[..., Awaitable[dict]]


def register_glp1_read_tools(
    server: Any,
    deps: Glp1ToolDependencies,
) -> RegisteredGlp1ReadTools:
    """Register the GLP-1 aggregate read at its frozen surface position."""

    @server.tool()
    @deps.gated("glp1")
    async def get_glp1_logs(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """Retrieves GLP-1 injection logs, active dosage phases, and recorded side
        effects. Injections/side effects default to the most recent 100."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            scope_kwargs = {"subject_id": scope.subject_id}
            injections = await glp1_queries.list_injections(
                session,
                start=start,
                end=end,
                limit=limit,
                **scope_kwargs,
            )
            phases = sorted(
                await glp1_queries.list_dose_phases(session, **scope_kwargs),
                key=lambda phase: (phase.start_date, phase.id),
                reverse=True,
            )
            effects = await glp1_queries.list_side_effects(
                session,
                start=start,
                end=end,
                limit=limit,
                **scope_kwargs,
            )

            return {
                "injections": [deps.serialize_row(row) for row in injections],
                "dose_phases": [deps.serialize_row(row) for row in phases],
                "side_effects": [deps.serialize_row(row) for row in effects],
            }

    return RegisteredGlp1ReadTools(get_glp1_logs=get_glp1_logs)


def register_glp1_injection_tools(
    server: Any,
    deps: Glp1ToolDependencies,
) -> RegisteredGlp1InjectionTools:
    """Register the GLP-1 injection write at its frozen surface position."""

    @server.tool()
    @deps.gated("glp1")
    async def log_glp1(
        drug: str,
        dose_mg: float,
        on_date: Optional[str] = None,
        site: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Records a GLP-1 injection (drug name, dose in mg, optional injection site).
        WRITE tool — saved immediately. If a hard conflict rule blocks the save,
        returns ``{"blocked": true, ...}``; call again with ``override=True`` to save
        anyway."""
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
                row = await glp1_writes.log_injection(
                    session,
                    on_date=parsed_date,
                    drug=drug,
                    dose_mg=dose_mg,
                    site=site,
                    note=note,
                    source=Source.MCP.value,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            except ValueError as exc:
                # An LLM bypasses the HTML form, so bad input comes back as a clean
                # error instead of an opaque database failure.
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredGlp1InjectionTools(log_glp1=log_glp1)


def register_glp1_maintenance_tools(
    server: Any,
    deps: Glp1ToolDependencies,
) -> RegisteredGlp1MaintenanceTools:
    """Register late GLP-1 update, side-effect, and phase writes in order."""

    @server.tool()
    @deps.gated("glp1")
    async def update_glp1(
        injection_id: int,
        drug: Optional[str] = None,
        dose_mg: Optional[float] = None,
        on_date: Optional[str] = None,
        site: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Edits an existing GLP-1 injection by ID. Only the fields you pass are
        changed; ``on_date`` left out keeps the injection's own date. Runs the same
        conflict gate as a fresh log — on a hard block returns ``{"blocked": true,
        ...}``; retry with ``override=True``. WRITE tool."""
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
            current = await glp1_writes.get_injection_for_update(
                session,
                injection_id,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
            if current is None:
                return {"error": f"Injection {injection_id} not found"}
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
                "drug": current.drug if drug is None else drug,
                "dose_mg": current.dose_mg if dose_mg is None else dose_mg,
                "site": current.site if site is None else site,
                "note": current.note if note is None else note,
            }
            try:
                row = await glp1_writes.update_injection(
                    session,
                    injection_id,
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
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("glp1")
    async def log_side_effect(
        effect_type: str,
        severity: int,
        on_date: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        """Records a GLP-1 side effect (e.g. "nausea") with a severity 1–5 for a date
        (default today). WRITE tool — saved immediately."""
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
            row = await glp1_writes.log_side_effect(
                session,
                on_date=parsed_date,
                effect_type=effect_type,
                severity=severity,
                note=note,
                source=Source.MCP.value,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("glp1")
    async def add_dose_phase(
        start_date: str,
        drug: str,
        dose_mg: float,
        end_date: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Adds a GLP-1 dose phase (a period on a given drug + dose, overlaid on the
        weight chart). Open-ended phases are bounded at adjacent phase starts so only
        the newest one remains current. WRITE tool."""
        session_factory = deps.get_session_factory()
        parsed_start = deps.parse_date(start_date, field="start_date")
        parsed_end = deps.parse_date(end_date, field="end_date")
        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(
                session,
                evaluation_date=parsed_start,
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            try:
                row = await glp1_writes.add_dose_phase(
                    session,
                    start_date=parsed_start,
                    drug=drug,
                    dose_mg=dose_mg,
                    end_date=parsed_end,
                    note=note,
                    source=Source.MCP.value,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            except ValueError as exc:
                return {"error": str(exc)}
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredGlp1MaintenanceTools(
        update_glp1=update_glp1,
        log_side_effect=log_side_effect,
        add_dose_phase=add_dose_phase,
    )


__all__ = [
    "Glp1ToolDependencies",
    "RegisteredGlp1InjectionTools",
    "RegisteredGlp1MaintenanceTools",
    "RegisteredGlp1ReadTools",
    "register_glp1_injection_tools",
    "register_glp1_maintenance_tools",
    "register_glp1_read_tools",
]
