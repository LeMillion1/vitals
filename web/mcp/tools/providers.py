"""Garmin and Hevy MCP reads/syncs without delivery-layer ORM queries."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.integrations.hevy_client import HevyAPIError, HevyNotConfigured
from vitals.services.garmin import jobs as garmin_jobs
from vitals.services.garmin import queries as garmin_queries
from vitals.services.hevy import jobs as hevy_jobs
from vitals.services.hevy import queries as hevy_queries
from vitals.utils.timeutils import today_local


logger = logging.getLogger(__name__)

INTRADAY_POINT_CAP = 5000
SYNC_DAILY_LIMIT = 3
_SLEEP_DETAIL_COLUMNS = ("sleep_stages", "breathing_events")


@dataclass(frozen=True)
class ProviderToolDependencies:
    get_session_factory: Callable[[], Any]
    get_redis_client: Callable[[], Any]
    parse_date: Callable[..., Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    actor_username: Callable[..., Awaitable[str]]
    serialize_row: Callable[[Any], dict]
    gated: Callable[[str], Callable[[Any], Any]]
    intraday_point_cap: Callable[[], int]
    sync_daily_limit: Callable[[], int]


@dataclass(frozen=True)
class RegisteredGarminReadTools:
    get_garmin_metrics: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredHevyReadTools:
    get_hevy_workouts: Callable[..., Awaitable[list[dict]]]


@dataclass(frozen=True)
class RegisteredGarminSyncTools:
    sync_garmin: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredHevySyncTools:
    sync_hevy: Callable[..., Awaitable[dict]]


def fold_sleep_detail(row: dict) -> dict:
    """Swap each present sleep-detail column for a count and retrieval hint."""
    for name in _SLEEP_DETAIL_COLUMNS:
        value = row.get(name)
        if value:
            row[name] = f"{len(value)} entries — call again with sleep_detail=True"
    return row


async def spend_sync_quota(
    deps: ProviderToolDependencies,
    bucket: str,
    limit: Optional[int] = None,
) -> Optional[dict]:
    """Count one call against today's quota, failing open if Redis is unavailable."""
    resolved_limit = deps.sync_daily_limit() if limit is None else limit
    key = f"mcp:sync_quota:{bucket}:{today_local().isoformat()}"
    try:
        redis = deps.get_redis_client()
        used = await redis.incr(key)
        if used == 1:
            await redis.expire(key, 86400)
    except Exception:
        logger.warning(
            "sync quota backend unavailable for %s; allowing",
            bucket,
            exc_info=True,
        )
        return None
    if used > resolved_limit:
        return {
            "error": (
                f"{bucket} has already run {resolved_limit} times today, which is "
                "the daily cap for on-demand syncs. The scheduled sync keeps "
                "running regardless; the quota resets at midnight."
            )
        }
    return None


def register_garmin_read_tools(
    server: Any,
    deps: ProviderToolDependencies,
) -> RegisteredGarminReadTools:
    """Register the Garmin read at its frozen early surface position."""

    @server.tool()
    async def get_garmin_metrics(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        intraday: bool = False,
        sleep_detail: bool = False,
    ) -> dict:
        """Retrieves daily Garmin recovery/sleep scores and recorded activity sessions.
        Each series defaults to the most recent 100 rows.

        Set ``intraday=True`` to also get the curves behind the daily summaries, as
        ``intraday: {series_type: [{ts, value}]}``. Two families of series:

          * the whole day — ``stress``, ``body_battery``, ``heart_rate`` (a sample
            every ~2–3 minutes, so ~480 points per series per day);
          * the night — ``sleep_hr``, ``sleep_spo2``, ``sleep_respiration``,
            ``sleep_stress``, ``sleep_bb``, ``sleep_hrv``, ``sleep_movement``
            (~2000 points across the seven).

        A night's samples are dated to the daily row they belong to (the morning of
        waking), including the ones recorded the previous evening, so one night reads
        as one date.

        Off by default because it is orders of magnitude more data than the daily
        rows: use it to answer *when* something happened (a stress spike, a Body
        Battery drain, an SpO2 dip and which sleep stage it fell in), always with a
        narrow start_date/end_date window. The response caps at 5000 points and sets
        ``intraday_truncated`` to true when the window held more than that.

        The night's *stage* timeline is not a series — it's ``sleep_stages`` on the
        daily row (``[{start, end, stage}]``, stage being deep/light/rem/awake), next
        to ``breathing_events``. Both are folded to a count by default and returned in
        full with ``sleep_detail=True`` — a separate switch from ``intraday`` so that
        reading one night's hypnogram doesn't drag every curve along with it. Ask for
        it with a narrow window when the question is about the shape of a night.
        """
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            daily = await garmin_queries.list_daily(
                session,
                subject_id=scope.subject_id,
                start=start,
                end=end,
                limit=limit,
            )
            activities = await garmin_queries.list_activities(
                session,
                subject_id=scope.subject_id,
                start=start,
                end=end,
                limit=limit,
            )

            rows = [deps.serialize_row(row) for row in daily]
            if not sleep_detail:
                rows = [fold_sleep_detail(row) for row in rows]
            result = {
                "daily_recovery": rows,
                "activities": [deps.serialize_row(row) for row in activities],
            }

            if intraday:
                cap = deps.intraday_point_cap()
                points = await garmin_queries.list_intraday(
                    session,
                    subject_id=scope.subject_id,
                    start=start,
                    end=end,
                    limit=cap + 1,
                )
                result["intraday_truncated"] = len(points) > cap
                series: dict[str, list[dict]] = {}
                for point in points[:cap]:
                    series.setdefault(point.series_type, []).append(
                        {"ts": point.ts.isoformat(), "value": point.value}
                    )
                result["intraday"] = series

            return result

    return RegisteredGarminReadTools(get_garmin_metrics=get_garmin_metrics)


def register_hevy_read_tools(
    server: Any,
    deps: ProviderToolDependencies,
) -> RegisteredHevyReadTools:
    """Register the Hevy read at its frozen early surface position."""

    @server.tool()
    async def get_hevy_workouts(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Retrieves Hevy strength training workouts, including exercises, sets,
        weights, and reps. Defaults to the most recent 100 workouts."""
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            workouts = await hevy_queries.list_workouts(
                session,
                subject_id=scope.subject_id,
                start=start,
                end=end,
                limit=limit,
            )
            serialized = []
            for workout in workouts:
                workout_payload = deps.serialize_row(workout)
                workout_payload["exercises"] = []
                for exercise in workout.exercises:
                    exercise_payload = deps.serialize_row(exercise)
                    exercise_payload["sets"] = [
                        deps.serialize_row(hevy_set) for hevy_set in exercise.sets
                    ]
                    workout_payload["exercises"].append(exercise_payload)
                serialized.append(workout_payload)
            return serialized

    return RegisteredHevyReadTools(get_hevy_workouts=get_hevy_workouts)


def register_garmin_sync_tools(
    server: Any,
    deps: ProviderToolDependencies,
) -> RegisteredGarminSyncTools:
    """Register the Garmin interactive sync at its frozen late position."""

    @server.tool()
    async def sync_garmin(days: int = 2) -> dict:
        """Pulls fresh Garmin data now — daily metrics plus activities for the last
        ``days`` (default 2: yesterday and today; up to 30 to fill a longer gap).

        Use it when the data looks stale or a day is missing, not before every read:
        the scheduler already polls several times a day. Capped at 3 calls a day.
        Returns ``{days, activities, error}``; an auth/MFA/throttle failure comes back
        as ``error`` (and raises an alert) rather than as an exception."""
        spent = await spend_sync_quota(deps, "sync_garmin")
        if spent:
            return spent

        summary = await garmin_jobs.sync_now_for_actor(
            deps.get_session_factory(),
            deps.get_redis_client(),
            days=max(1, min(int(days), 30)),
            actor_username=await deps.actor_username(),
        )
        if summary is None:
            return {"error": "Garmin is not configured — no credentials in settings"}
        return summary

    return RegisteredGarminSyncTools(sync_garmin=sync_garmin)


def register_hevy_sync_tools(
    server: Any,
    deps: ProviderToolDependencies,
) -> RegisteredHevySyncTools:
    """Register the Hevy interactive sync at its frozen late position."""

    @server.tool()
    @deps.gated("hevy")
    async def sync_hevy() -> dict:
        """Pulls the latest Hevy workouts now. Same rules as ``sync_garmin``: for a gap
        in the data, not for routine reads (the scheduler syncs every 6 hours), capped
        at 3 calls a day. Returns ``{fetched, created, updated, skipped}``."""
        spent = await spend_sync_quota(deps, "sync_hevy")
        if spent:
            return spent

        try:
            summary = await hevy_jobs.sync_now_for_actor(
                deps.get_session_factory(),
                deps.get_redis_client(),
                actor_username=await deps.actor_username(),
            )
        except (HevyNotConfigured, HevyAPIError) as exc:
            return {"error": f"Hevy sync failed: {exc}"}
        if summary is None:
            return {"error": "Hevy is not configured — no API key in settings"}
        return summary

    return RegisteredHevySyncTools(sync_hevy=sync_hevy)


__all__ = [
    "INTRADAY_POINT_CAP",
    "ProviderToolDependencies",
    "RegisteredGarminReadTools",
    "RegisteredGarminSyncTools",
    "RegisteredHevyReadTools",
    "RegisteredHevySyncTools",
    "SYNC_DAILY_LIMIT",
    "fold_sleep_detail",
    "register_garmin_read_tools",
    "register_garmin_sync_tools",
    "register_hevy_read_tools",
    "register_hevy_sync_tools",
    "spend_sync_quota",
]
