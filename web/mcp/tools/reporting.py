"""Cross-domain reporting MCP adapters without router or ORM dependencies."""
from __future__ import annotations

from vitals.services.digest.projection import assembly as digest_projection

from dataclasses import dataclass
from datetime import date as date_type, timedelta
from typing import Any, Awaitable, Callable, Optional

from vitals.analytics import exclude_ranges
from vitals.analytics.chart_registry import get as get_metric
from vitals.analytics.regression import fit_trend, project_date_for_value
from vitals.analytics.rolling import rolling_mean_by_date
from vitals.services.charts import data as chart_data_service
from vitals.services.portability import llm_projection
from vitals.services.projections.data_overview import project_data_overview
from vitals.services.weight import noise as weight_noise
from vitals.utils.timeutils import today_local


EXPORT_DEFAULT_DAYS = 90


@dataclass(frozen=True)
class ReportingToolDependencies:
    """Router-owned scope, date, and schema-overview seams."""

    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    composition_scope: Callable[[Any], Awaitable[Any]]
    conflict_scope: Callable[[Any], Awaitable[Any]]


@dataclass(frozen=True)
class RegisteredReportingTools:
    get_full_snapshot: Callable[..., Awaitable[dict]]
    export_everything: Callable[..., Awaitable[dict]]
    get_data_overview: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredTrendTools:
    get_trend: Callable[..., Awaitable[dict]]


def register_reporting_tools(
    server: Any,
    deps: ReportingToolDependencies,
) -> RegisteredReportingTools:
    """Register snapshot, export, and overview at their frozen positions."""

    @server.tool()
    async def get_full_snapshot(
        on_date: Optional[str] = None,
        period_days: int = 7,
    ) -> dict:
        """Returns context-v2 for a closed period (1..90 days): profile, coverage,
        weight/body composition, GLP-1/HRT plans and facts, every lab result in the
        period, Garmin recovery and activities, Hevy, nutrition, skincare,
        timeline and active goals. Every dated fact is bounded by the effective
        period end. When ``on_date`` is today the closed period ends yesterday."""
        session_factory = deps.get_session_factory()
        parsed_date = deps.parse_date(on_date, field="on_date")
        async with session_factory() as session:
            scope = await deps.composition_scope(session)
            try:
                return await digest_projection.assemble_context(
                    session,
                    subject_id=scope.subject_id,
                    on_date=parsed_date,
                    period_days=period_days,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    @server.tool()
    async def export_everything(
        domains: Optional[list[str]] = None,
        since: Optional[str] = None,
    ) -> dict:
        """Returns the health history as one compact, secret-free, LLM-ready export
        grouped by domain (weight, measurements, body scans, GLP-1, HRT, labs, Garmin,
        workouts, nutrition, skincare, supplements, genetics,
        milestones, timeline). This is the way to read long-term history in a single
        call rather than paging each domain's newest-100 read tool. Read-only.

        Defaults to the **last 90 days**: the whole lake is years of daily Garmin rows
        with per-minute sleep and would fill the conversation before the question is
        asked. Widen deliberately — ``since="2020-01-01"`` (any early date) for the
        entire history, and/or ``domains=["biomarkers", "weight_history"]`` to pull a
        couple of areas in full instead of everything. Unknown domain names are
        rejected with the list of valid ones."""
        default_since = today_local() - timedelta(days=EXPORT_DEFAULT_DAYS)
        cutoff = deps.parse_date(since, default_since, field="since")

        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            scope = await deps.composition_scope(session)
            try:
                return await llm_projection.export_llm(
                    session,
                    subject_id=scope.subject_id,
                    domains=domains,
                    since=cutoff,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    @server.tool()
    async def get_data_overview() -> dict:
        """Returns a per-domain map of what data exists: row count, earliest and latest
        date, and last-updated timestamp for each domain. Call this first to orient —
        it tells you the real date coverage and density before you query a domain, so
        you don't page blindly through empty or out-of-range windows. Read-only."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            scope = await deps.composition_scope(session)
            return await project_data_overview(
                session,
                subject_id=scope.subject_id,
            )

    return RegisteredReportingTools(
        get_full_snapshot=get_full_snapshot,
        export_everything=export_everything,
        get_data_overview=get_data_overview,
    )


def register_trend_tools(
    server: Any,
    deps: ReportingToolDependencies,
) -> RegisteredTrendTools:
    """Register dynamic metric trend analytics at its frozen position."""

    @server.tool()
    async def get_trend(
        metric_key: str,
        param: Optional[str] = None,
        target: Optional[float] = None,
        rolling_window_days: int = 7,
        exclude_noise: bool = True,
    ) -> dict:
        """Computes the trend for one metric instead of returning raw rows: linear slope
        (per day and per week), the latest rolling-mean value, and — if ``target`` is
        given — the projected date the trend reaches it. For weight metrics, noise-marked
        ranges are excluded (``exclude_noise``).

        ``metric_key`` is a registry key such as ``weight.weight_kg``,
        ``weight.body_fat_pct``, ``garmin.hrv_avg``, ``nutrition.calories``, or a
        parametrized one: ``labs.marker`` (``param`` = marker name),
        ``hevy.working_weight`` (``param`` = exercise id), ``body_comp.metric``
        (``param`` = ``metric_key`` or ``metric_key:segment``). Read-only."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            try:
                field = get_metric(metric_key)
            except KeyError:
                return {"error": f"Unknown metric '{metric_key}'"}
            try:
                trend_scope = await deps.conflict_scope(session)
                raw = await chart_data_service.series_for(
                    session,
                    subject_id=trend_scope.subject_id,
                    metric_key=metric_key,
                    param=param,
                )
            except ValueError as exc:
                return {"error": str(exc)}

            points = [
                (date_type.fromisoformat(point["date"]), float(point["value"]))
                for point in raw
            ]

            noise_applied = False
            if exclude_noise and field.domain == "weight":
                scope = await deps.conflict_scope(session)
                markers = await weight_noise.list_noise_markers(
                    session,
                    subject_id=scope.subject_id,
                )
                ranges = [(marker.start_date, marker.end_date) for marker in markers]
                if ranges:
                    points = exclude_ranges(points, ranges)
                    noise_applied = True

            points = sorted(points, key=lambda point: point[0])
            if not points:
                return {
                    "metric_key": metric_key,
                    "param": param,
                    "unit": field.unit,
                    "points": 0,
                }

            trend = fit_trend(points)
            rolling = rolling_mean_by_date(points, window_days=rolling_window_days)
            result: dict = {
                "metric_key": metric_key,
                "param": param,
                "unit": field.unit,
                "points": len(points),
                "first": {
                    "date": points[0][0].isoformat(),
                    "value": points[0][1],
                },
                "last": {
                    "date": points[-1][0].isoformat(),
                    "value": points[-1][1],
                },
                "rolling_mean": {
                    "window_days": rolling_window_days,
                    "last": {
                        "date": rolling[-1][0].isoformat(),
                        "value": rolling[-1][1],
                    },
                },
                "trend": (
                    None
                    if trend is None
                    else {
                        "slope_per_day": round(trend.slope_per_day, 5),
                        "slope_per_week": round(trend.slope_per_week, 4),
                        "n": trend.n,
                    }
                ),
                "noise_excluded": noise_applied,
            }
            if target is not None:
                crossing = project_date_for_value(points, target)
                result["projection"] = {
                    "target": target,
                    "date": crossing.isoformat() if crossing else None,
                }
            return result

    return RegisteredTrendTools(get_trend=get_trend)


__all__ = [
    "RegisteredReportingTools",
    "RegisteredTrendTools",
    "ReportingToolDependencies",
    "register_reporting_tools",
    "register_trend_tools",
]
