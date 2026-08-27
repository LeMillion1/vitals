"""Timeline MCP tool registration without a router dependency."""

from __future__ import annotations

from vitals.services.timeline import annotations as timeline_annotations
from vitals.services.timeline import events as timeline_events

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Source

from vitals.utils.timeutils import today_local


@dataclass(frozen=True)
class TimelineToolDependencies:
    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    legacy_owner: Callable[[Any], Awaitable[Any]]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]
    gated: Callable[[str], Callable[[Callable[..., Any]], Callable[..., Any]]]


@dataclass(frozen=True)
class RegisteredTimelineTools:
    get_timeline: Callable[..., Awaitable[list[dict]]]
    log_event: Callable[..., Awaitable[dict]]
    update_event: Callable[..., Awaitable[dict]]


def register_timeline_tools(
    server: Any,
    deps: TimelineToolDependencies,
) -> RegisteredTimelineTools:
    """Register the three frozen Timeline tools in their existing order."""

    @server.tool()
    async def get_timeline(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        domain: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Retrieves the cross-domain event feed — manual annotations (trips,
        illness, protocol changes) plus derived events (GLP-1 dose changes, lab
        draws, BIA scans, achieved milestones, noisy weight periods), newest first.
        Optionally filtered by date range (YYYY-MM-DD) and/or domain (weight, glp1,
        garmin, workouts, labs, nutrition, skincare, supplements, genetics,
        body_comp, or "timeline" for global flags)."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")
        domains = [domain] if domain else None

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            events = await timeline_events.list_events(
                session,
                subject_id=scope.subject_id,
                domains=domains,
                start=start,
                end=end,
                limit=limit,
            )
            return [event.to_dict() for event in events]

    @server.tool()
    @deps.gated("timeline")
    async def log_event(
        title: str,
        on_date: Optional[str] = None,
        end_date: Optional[str] = None,
        kind: str = "note",
        domain: str = "timeline",
        note: Optional[str] = None,
    ) -> dict:
        """Records a manual Timeline annotation — a flag shown on every chart and
        in the event feed (a trip, an illness, a protocol change, a free-form
        note). ``kind`` is one of: life_event, illness, travel, protocol_change,
        note. ``domain`` scopes the flag to one chart (weight, glp1, ...) or
        "timeline" (default) to show it on every chart. ``end_date`` makes it a
        range (e.g. a week-long trip); omit it for a single-day event. WRITE tool —
        saved immediately. No-op with an error if the timeline module is disabled."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, today_local(), field="on_date")
        parsed_end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            ownership = await deps.legacy_owner(session)
            row = await timeline_annotations.create_annotation(
                session,
                title=title,
                on_date=parsed_date,
                end_date=parsed_end,
                kind=kind,
                domain=domain,
                note=note,
                source=Source.MCP.value,
                identity=ownership.owner_action(),
            )
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("timeline")
    async def update_event(
        event_id: int,
        title: Optional[str] = None,
        on_date: Optional[str] = None,
        end_date: Optional[str] = None,
        kind: Optional[str] = None,
        domain: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        """Updates a manual Timeline annotation by ID — the ``id`` of a row from
        ``get_timeline`` whose source is manual (derived events are computed and
        cannot be edited). Only the fields you pass are changed; everything left out
        keeps its stored value, including the event's own date. WRITE tool."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, field="on_date")
        parsed_end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            ownership = await deps.legacy_owner(session)
            current = await timeline_annotations.get_annotation(
                session,
                event_id,
                subject_id=ownership.subject_id,
            )
            if current is None:
                return {"error": f"Event {event_id} not found"}
            merged = {
                "title": current.title if title is None else title,
                "date": current.date if parsed_date is None else parsed_date,
                "end_date": current.end_date if parsed_end is None else parsed_end,
                "kind": current.kind if kind is None else kind,
                "domain": current.domain if domain is None else domain,
                "note": current.note if note is None else note,
            }
            row = await timeline_annotations.update_annotation(
                session,
                event_id,
                on_date=merged.pop("date"),
                identity=ownership.owner_action(),
                **merged,
            )
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredTimelineTools(
        get_timeline=get_timeline,
        log_event=log_event,
        update_event=update_event,
    )


__all__ = [
    "RegisteredTimelineTools",
    "TimelineToolDependencies",
    "register_timeline_tools",
]
