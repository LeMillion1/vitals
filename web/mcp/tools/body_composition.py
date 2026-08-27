"""Body-composition MCP tool registration without router or ORM imports."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Domain, Source
from vitals.services.data_lake import raw_payloads
from vitals.services.body_scan.scans import alerts as body_scan_alerts
from vitals.services.body_scan.scans import ingestion as body_scan_ingestion
from vitals.services.body_scan.scans import queries as body_scan_queries
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.utils.timeutils import today_local
from web.mcp.serialization import serialize_row


@dataclass(frozen=True)
class BodyCompositionToolDependencies:
    """Router-owned module gate, capability, and serialization seams."""

    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    module_enabled: Callable[[Any, str], Awaitable[bool]]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    weight_write: Callable[..., Awaitable[Any]]
    conflict_payload: Callable[[ConflictBlocked], dict]
    gated: Callable[[str], Callable[[Any], Any]]


@dataclass(frozen=True)
class RegisteredBodyCompositionTools:
    get_body_scans: Callable[..., Awaitable[list[dict]]]
    get_body_scan: Callable[..., Awaitable[dict]]
    get_body_metric_history: Callable[..., Awaitable[list[dict]]]
    log_body_scan: Callable[..., Awaitable[dict]]


def serialize_scan(scan: Any) -> dict:
    """Serialize one scan together with its eagerly loaded metric sheet."""

    payload = serialize_row(scan)
    payload["metrics"] = [serialize_row(metric) for metric in scan.metrics]
    return payload


def register_body_composition_tools(
    server: Any,
    deps: BodyCompositionToolDependencies,
) -> RegisteredBodyCompositionTools:
    """Register the frozen Body Composition surface in its existing order."""

    @server.tool()
    async def get_body_scans(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Retrieves body-composition scans (InBody / МедАсс) with every parsed metric
        (skeletal muscle, body water, visceral fat, segmental analysis, phase angle…).
        Defaults to the most recent 100 scans."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            if not await deps.module_enabled(session, "body_comp"):
                return [{"error": "module 'body_comp' is disabled"}]
            scope = await deps.conflict_scope(session)
            scan_rows = await body_scan_queries.list_scans(
                session,
                start=start,
                end=end,
                subject_id=scope.subject_id,
            )
            return [serialize_scan(scan) for scan in scan_rows[:limit]]

    @server.tool()
    async def get_body_scan(scan_id: int) -> dict:
        """Retrieves a single body-composition scan with its full metric sheet."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            if not await deps.module_enabled(session, "body_comp"):
                return {"error": "module 'body_comp' is disabled"}
            scope = await deps.conflict_scope(session)
            scan = await body_scan_queries.get_scan(
                session,
                scan_id,
                subject_id=scope.subject_id,
            )
            if scan is None:
                return {"error": f"Body scan {scan_id} not found"}
            return serialize_scan(scan)

    @server.tool()
    async def get_body_metric_history(
        metric_key: str,
        segment: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict]:
        """Time series for one body-composition metric (e.g. ``skeletal_muscle_mass``,
        ``phase_angle``, ``visceral_fat_area``), optionally for a single body segment."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")
        async with session_factory() as session:
            if not await deps.module_enabled(session, "body_comp"):
                return [{"error": "module 'body_comp' is disabled"}]
            scope = await deps.conflict_scope(session)
            return await body_scan_queries.metric_history(
                session,
                metric_key,
                segment=segment,
                start=start,
                end=end,
                subject_id=scope.subject_id,
            )

    @server.tool()
    @deps.gated("body_comp")
    async def log_body_scan(
        metrics: list[dict],
        on_date: Optional[str] = None,
        device: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Records a body-composition scan from structured metrics (no photo needed).

        Each metric is ``{"label" or "metric_key": str, "value": number, "unit": str?,
        "ref_low": number?, "ref_high": number?, "segment": str?}``. The scan's weight /
        body-fat% / LBM are bridged into the weight domain. WRITE tool — saved
        immediately. No-op with an error if the body_comp module is disabled. If a hard
        conflict rule blocks the save, returns ``{"blocked": true, ...}``; call again
        with ``override=True``."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, today_local(), field="on_date")

        async with session_factory() as session:
            extracted = {
                "date": parsed_date.isoformat(),
                "device": device,
                "note": note,
                "metrics": metrics,
                "override": override,
            }
            try:
                conflict_context, prepared_weight_write = await deps.weight_write(
                    session,
                    evaluation_date=parsed_date,
                )
                raw = await raw_payloads.upsert_owned_raw_payload(
                    session,
                    identity=conflict_context.identity,
                    integration_connection_id=None,
                    file_asset_id=None,
                    domain=Domain.BODY_COMPOSITION.value,
                    source=Source.MCP.value,
                    external_id=f"mcp:{uuid.uuid4().hex}",
                    payload=extracted,
                )
                scan = await body_scan_ingestion.ingest_structured_scan(
                    session,
                    extracted,
                    raw_payload=raw,
                    identity=conflict_context.identity,
                    prepared_weight_write=prepared_weight_write,
                    override=override,
                )
                await body_scan_alerts.refresh_alerts(
                    session,
                    subject_id=conflict_context.identity.subject_id,
                    on_date=parsed_date,
                    identity=conflict_context.identity,
                    prepared_weight_write=prepared_weight_write,
                )
            except ConflictBlocked as exc:
                await session.rollback()
                return deps.conflict_payload(exc)
            except ValueError as exc:
                await session.rollback()
                return {"error": str(exc)}
            await session.commit()
            full = await body_scan_queries.get_scan(
                session,
                scan.id,
                subject_id=conflict_context.identity.subject_id,
            )
            return serialize_scan(full) if full else {"scan_id": scan.id}

    return RegisteredBodyCompositionTools(
        get_body_scans=get_body_scans,
        get_body_scan=get_body_scan,
        get_body_metric_history=get_body_metric_history,
        log_body_scan=log_body_scan,
    )


__all__ = [
    "BodyCompositionToolDependencies",
    "RegisteredBodyCompositionTools",
    "register_body_composition_tools",
    "serialize_scan",
]
