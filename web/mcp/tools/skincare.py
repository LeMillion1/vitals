"""Skincare MCP tool registration without a router dependency."""
from __future__ import annotations

from vitals.services.skincare import queries as skincare_queries
from vitals.services.skincare import writes as skincare_writes

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Source

from vitals.services.conflicts import engine
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.utils.timeutils import today_local


@dataclass(frozen=True)
class SkincareToolDependencies:
    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    conflict_write_context: Callable[..., Awaitable[Any]]
    conflict_payload: Callable[[ConflictBlocked], dict]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]
    gated: Callable[[str], Callable[[Any], Any]]


@dataclass(frozen=True)
class RegisteredSkincareReadTools:
    get_skincare_logs: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredSkincareRoutineTools:
    log_skincare: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredSkincareObservationTools:
    log_skincare_observation: Callable[..., Awaitable[dict]]


def register_skincare_read_tools(
    server: Any,
    deps: SkincareToolDependencies,
) -> RegisteredSkincareReadTools:
    """Register the combined skincare read at its frozen surface position."""

    @server.tool()
    @deps.gated("skincare")
    async def get_skincare_logs(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """Retrieves skincare routine application logs and skin status observations.
        Each series defaults to the most recent 100 rows."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            logs = await skincare_queries.list_logs(
                session,
                subject_id=scope.subject_id,
                start=start,
                end=end,
                limit=limit,
            )
            observations = await skincare_queries.list_observations(
                session,
                subject_id=scope.subject_id,
                start=start,
                end=end,
                limit=limit,
            )

            return {
                "logs": [deps.serialize_row(row) for row in logs],
                "observations": [deps.serialize_row(row) for row in observations],
            }

    return RegisteredSkincareReadTools(get_skincare_logs=get_skincare_logs)


def register_skincare_routine_tools(
    server: Any,
    deps: SkincareToolDependencies,
) -> RegisteredSkincareRoutineTools:
    """Register the daily routine upsert at its frozen surface position."""

    @server.tool()
    @deps.gated("skincare")
    async def log_skincare(
        on_date: Optional[str] = None,
        retinoid: bool = False,
        azelaic: bool = False,
        peel: bool = False,
        niacinamide_spf: bool = False,
        moisturizer: bool = False,
        vitamin_c: bool = False,
        benzoyl_peroxide: bool = False,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Records or updates the daily skincare routine checklist (one per day, upsert).
        Boolean flags indicate which products were applied. WRITE tool — saved
        immediately. If a hard conflict rule blocks the save, returns
        ``{"blocked": true, ...}``; call again with ``override=True`` to save anyway."""
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
                row = await skincare_writes.upsert_log(
                    session,
                    on_date=parsed_date,
                    retinoid=retinoid,
                    azelaic=azelaic,
                    peel=peel,
                    niacinamide_spf=niacinamide_spf,
                    moisturizer=moisturizer,
                    vitamin_c=vitamin_c,
                    benzoyl_peroxide=benzoyl_peroxide,
                    note=note,
                    source=Source.MCP.value,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredSkincareRoutineTools(log_skincare=log_skincare)


def register_skincare_observation_tools(
    server: Any,
    deps: SkincareToolDependencies,
) -> RegisteredSkincareObservationTools:
    """Register the skin observation write at its frozen surface position."""

    @server.tool()
    @deps.gated("skincare")
    async def log_skincare_observation(
        on_date: Optional[str] = None,
        inflammation: Optional[int] = None,
        pih: Optional[int] = None,
        zone: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        """Records a skin-status observation — inflammation and PIH (post-inflammatory
        hyperpigmentation) scores, an optional face ``zone``, and a note. Distinct from
        the daily routine checklist (log_skincare). WRITE tool — saved immediately."""
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
            row = await skincare_writes.add_observation(
                session,
                on_date=parsed_date,
                inflammation=inflammation,
                pih=pih,
                zone=zone,
                note=note,
                source=Source.MCP.value,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredSkincareObservationTools(
        log_skincare_observation=log_skincare_observation,
    )


__all__ = [
    "RegisteredSkincareObservationTools",
    "RegisteredSkincareReadTools",
    "RegisteredSkincareRoutineTools",
    "SkincareToolDependencies",
    "register_skincare_observation_tools",
    "register_skincare_read_tools",
    "register_skincare_routine_tools",
]
