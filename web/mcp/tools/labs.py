"""Labs MCP tool registration without a reverse dependency on the router."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Domain, Source
from vitals.services.data_lake import raw_payloads
from vitals.services.conflicts import engine
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.services.labs import alerts as lab_alerts
from vitals.services.labs import ingestion as lab_ingestion
from vitals.services.labs import results as lab_results
from vitals.utils.timeutils import now_local, today_local


@dataclass(frozen=True)
class LabsToolDependencies:
    """Router-owned request, identity, and serialization seams."""

    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    conflict_write_context: Callable[..., Awaitable[Any]]
    conflict_payload: Callable[[ConflictBlocked], dict]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredLabsTools:
    get_lab_results: Callable[..., Awaitable[list[dict]]]
    log_lab_result: Callable[..., Awaitable[dict]]
    update_lab_result: Callable[..., Awaitable[dict]]
    log_lab_results: Callable[..., Awaitable[dict]]


def register_labs_tools(server: Any, deps: LabsToolDependencies) -> RegisteredLabsTools:
    """Register the frozen Labs surface in its existing order."""

    @server.tool()
    async def get_lab_results(
        marker: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Retrieves lab results (biomarker, value, unit, reference range, computed
        out-of-range flag), optionally filtered by marker name and/or date range
        (YYYY-MM-DD). Defaults to the most recent 100 rows across all markers."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            results = await lab_results.list_results(
                session,
                marker=marker,
                start=start,
                end=end,
                limit=limit,
                subject_id=scope.subject_id,
            )
            return [deps.serialize_row(result) for result in results]

    @server.tool()
    async def log_lab_result(
        marker: str,
        value: float,
        on_date: Optional[str] = None,
        unit: Optional[str] = None,
        ref_low: Optional[float] = None,
        ref_high: Optional[float] = None,
        lab_name: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Records a single lab marker value (one biomarker from a blood/urine test).
        The out-of-range flag is computed automatically; a range left out here falls
        back to the marker's catalog range if one is already on file. WRITE tool —
        saved immediately. Defaults: on_date = today. A hard conflict rule (e.g. a
        hyperkalemic potassium result while a potassium supplement is active) returns
        ``{"blocked": true, ...}``; retry with ``override=True`` to save anyway."""
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
                raw = await raw_payloads.upsert_owned_raw_payload(
                    session,
                    identity=conflict_context.identity,
                    integration_connection_id=None,
                    file_asset_id=None,
                    domain=Domain.LABS.value,
                    source=Source.MCP.value,
                    external_id=f"mcp:{uuid.uuid4().hex}",
                    payload={
                        "date": parsed_date.isoformat(),
                        "marker": marker,
                        "value": value,
                        "unit": unit,
                        "ref_low": ref_low,
                        "ref_high": ref_high,
                        "lab_name": lab_name,
                        "note": note,
                        "override": override,
                    },
                )
                row = await lab_results.add_result(
                    session,
                    on_date=parsed_date,
                    marker=marker,
                    value=value,
                    unit=unit,
                    ref_low=ref_low,
                    ref_high=ref_high,
                    lab_name=lab_name,
                    note=note,
                    source=Source.MCP.value,
                    raw_payload_id=raw.id,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
                raw.processed_at = now_local()
                await session.flush()
                await lab_alerts.refresh_alerts(
                    session,
                    subject_id=conflict_context.identity.subject_id,
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
    async def update_lab_result(
        result_id: int,
        value: Optional[float] = None,
        marker: Optional[str] = None,
        on_date: Optional[str] = None,
        unit: Optional[str] = None,
        ref_low: Optional[float] = None,
        ref_high: Optional[float] = None,
        lab_name: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Corrects an existing lab result by ID — a mistyped value, a range read off
        the wrong column. Only the fields you pass are changed; the out-of-range flag
        is recomputed and the alerts derived from it refreshed. Use this instead of
        delete + re-add: a measurement is never thrown away here. WRITE tool."""
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
            current = await lab_results.get_result_for_update(
                session,
                result_id,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
            if current is None:
                return {"error": f"Lab result {result_id} not found"}
            final_date = parsed_date or current.date
            if final_date != conflict_context.evaluation_date:
                conflict_context = await deps.conflict_write_context(
                    session,
                    evaluation_date=final_date,
                )
                prepared = await engine.prepare_scoped_write(
                    session,
                    context=conflict_context,
                )
            try:
                row = await lab_results.update_result(
                    session,
                    result_id,
                    on_date=parsed_date,
                    marker=marker,
                    value=value,
                    unit=unit,
                    ref_low=ref_low,
                    ref_high=ref_high,
                    lab_name=lab_name,
                    note=note,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            if row is None:
                return {"error": f"Lab result {result_id} not found"}
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    async def log_lab_results(
        results: list[dict],
        on_date: Optional[str] = None,
        lab_name: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Records every marker from one lab report at once (e.g. a full blood panel
        read from a photo/PDF shared in the conversation) — the natural way to push a
        whole report in one call instead of calling log_lab_result per marker.

        Each item in ``results`` is ``{"marker": str, "value": number, "unit": str?,
        "ref_low": number?, "ref_high": number?}``. Identical (date, marker, value)
        rows are deduped, so retrying a call is safe. The verbatim payload is kept in
        raw_payloads, same as a document uploaded through the web UI. WRITE tool —
        saved immediately. Defaults: on_date = today. A hard conflict rule on any
        marker in the panel returns ``{"blocked": true, ...}`` and saves nothing;
        retry with ``override=True`` to save the whole panel anyway."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, today_local(), field="on_date")

        async with session_factory() as session:
            extracted = {
                "date": parsed_date.isoformat(),
                "lab_name": lab_name,
                "results": results,
            }
            conflict_context = await deps.conflict_write_context(
                session,
                evaluation_date=parsed_date,
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            raw = await raw_payloads.upsert_owned_raw_payload(
                session,
                identity=conflict_context.identity,
                integration_connection_id=None,
                file_asset_id=None,
                domain=Domain.LABS.value,
                source=Source.MCP.value,
                external_id=f"mcp:{uuid.uuid4().hex}",
                payload=extracted,
            )
            try:
                summary = await lab_ingestion.ingest_structured_results(
                    session,
                    extracted,
                    raw_payload=raw,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                    override=override,
                )
                await lab_alerts.refresh_alerts(
                    session,
                    subject_id=conflict_context.identity.subject_id,
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
            return {
                "created": summary["created"],
                "skipped": summary["skipped"],
                "results": [
                    await deps.serialize_written(session, result)
                    for result in summary["results"]
                ],
            }

    return RegisteredLabsTools(
        get_lab_results=get_lab_results,
        log_lab_result=log_lab_result,
        update_lab_result=update_lab_result,
        log_lab_results=log_lab_results,
    )


__all__ = ["LabsToolDependencies", "RegisteredLabsTools", "register_labs_tools"]
