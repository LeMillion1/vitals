"""Milestones MCP tool registration without router or ORM imports."""
from __future__ import annotations

from vitals.services.milestones import goals as milestone_goals
from vitals.services.milestones import progress as milestone_progress
from vitals.services.milestones import queries as milestone_queries

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Domain, MilestoneStatus

from vitals.services.conflicts import engine


_MILESTONE_STATUSES = {status.value for status in MilestoneStatus}


@dataclass(frozen=True)
class MilestoneToolDependencies:
    """Router-owned identity, date parsing, and serialization seams."""

    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    conflict_write_context: Callable[..., Awaitable[Any]]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredMilestoneTools:
    get_milestones: Callable[..., Awaitable[list[dict]]]
    create_milestone: Callable[..., Awaitable[dict]]
    update_milestone: Callable[..., Awaitable[dict]]


def register_milestone_tools(
    server: Any,
    deps: MilestoneToolDependencies,
) -> RegisteredMilestoneTools:
    """Register the frozen Milestones surface in its existing order."""

    @server.tool()
    async def get_milestones(status: Optional[str] = None) -> list[dict]:
        """Returns goal cards with live progress (current value, remaining, days left)
        computed for weight/body-comp goals. Optionally filtered by ``status`` (active,
        achieved, missed, paused). Read-only."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            rows = await milestone_queries.list_milestones(
                session,
                status=status,
                subject_id=scope.subject_id,
            )
            return [
                await milestone_progress.progress(
                    session,
                    milestone,
                    subject_id=scope.subject_id,
                )
                for milestone in rows
            ]

    @server.tool()
    async def create_milestone(
        name: str,
        domain: str = Domain.WEIGHT.value,
        target_value: Optional[float] = None,
        target_unit: Optional[str] = None,
        deadline: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        """Creates a goal card (e.g. "reach 85 kg by 2026-12-31"). ``domain`` is the
        related health area (weight, glp1, labs, body_comp, ...); ``deadline`` is
        YYYY-MM-DD. WRITE tool — saved immediately."""
        session_factory = deps.get_session_factory()
        parsed_deadline = deps.parse_date(deadline, field="deadline")
        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(session)
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            row = await milestone_goals.create_milestone(
                session,
                name=name,
                domain=domain,
                target_value=target_value,
                target_unit=target_unit,
                deadline=parsed_deadline,
                note=note,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    async def update_milestone(
        milestone_id: int,
        name: Optional[str] = None,
        domain: Optional[str] = None,
        target_value: Optional[float] = None,
        target_unit: Optional[str] = None,
        deadline: Optional[str] = None,
        status: Optional[str] = None,
        note: Optional[str] = None,
        clear_fields: Optional[list[str]] = None,
    ) -> dict:
        """Updates a goal card by ID. Only the fields you pass are changed. Use
        ``status`` to mark a goal achieved/missed/paused/active. To remove an
        optional value, name it in ``clear_fields`` (target_value, target_unit,
        deadline, or note). WRITE tool."""
        nullable_fields = {"target_value", "target_unit", "deadline", "note"}
        clear = set(clear_fields or ())
        unknown = clear.difference(nullable_fields)
        if unknown:
            return {
                "error": "clear_fields contains unknown fields: "
                + ", ".join(sorted(unknown))
            }
        supplied = {
            "target_value": target_value,
            "target_unit": target_unit,
            "deadline": deadline,
            "note": note,
        }
        overlapping = sorted(
            field for field in clear if supplied[field] is not None
        )
        if overlapping:
            return {
                "error": "fields cannot be set and cleared together: "
                + ", ".join(overlapping)
            }
        if status is not None and status not in _MILESTONE_STATUSES:
            return {
                "error": f"Unknown status '{status}'. Use: "
                + ", ".join(sorted(_MILESTONE_STATUSES))
            }

        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(session)
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            kwargs: dict = {}
            if name is not None:
                kwargs["name"] = name
            if domain is not None:
                kwargs["domain"] = domain
            if target_value is not None:
                kwargs["target_value"] = target_value
            if target_unit is not None:
                kwargs["target_unit"] = target_unit
            if deadline is not None:
                kwargs["deadline"] = deps.parse_date(deadline, field="deadline")
            if status is not None:
                kwargs["status"] = status
            if note is not None:
                kwargs["note"] = note
            for field in clear:
                kwargs[field] = None
            row = await milestone_goals.update_milestone(
                session,
                milestone_id,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
                **kwargs,
            )
            if row is None:
                return {"error": f"Milestone {milestone_id} not found"}
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredMilestoneTools(
        get_milestones=get_milestones,
        create_milestone=create_milestone,
        update_milestone=update_milestone,
    )


__all__ = [
    "MilestoneToolDependencies",
    "RegisteredMilestoneTools",
    "register_milestone_tools",
]
