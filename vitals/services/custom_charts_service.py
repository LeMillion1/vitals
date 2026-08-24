"""Custom chart builder — saved configurations.

Storage: one ``app_settings`` row, ``key='custom_charts'``, ``value`` a JSON
array of chart configs (not an object — this key just happens to hold a list).
Redis (``settings:custom_charts``) is a read-through cache; the DB is the
source of truth. Same shape as ``modules_service``/``ui_version_service``:
``_sanitize()`` never raises, projecting arbitrary stored data onto a clean
shape so a corrupt row degrades to an empty list instead of 500-ing.

A chart config::

    {
      "id": "9f3a1c2b7e4d",
      "name": "Вес и стресс",
      "normalize": false,
      "series": [
        {"domain": "weight", "metric_key": "weight.weight_kg", "param": null,
         "label": null, "color_slot": 0},
        ...
      ]
    }
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.analytics import chart_registry
from vitals.services.scoped_settings_service import (
    ScopedSettingKey,
    SettingScope,
    get_scoped_setting,
    update_scoped_setting,
)

logger = logging.getLogger(__name__)

# The legacy app_settings key the scoped read still falls back to.
SETTINGS_KEY = "custom_charts"
REDIS_KEY = "settings:custom_charts"
REDIS_TTL = 300

MAX_SERIES_PER_CHART = 8   # matches the 8-slot categorical palette
MAX_CHARTS = 50


def cache_key(subject_id: uuid.UUID) -> str:
    """Return the UUID-namespaced cache key.

    One cache entry per person: a shared key would serve one subject's chart
    list to the next request from another.
    """

    return f"{REDIS_KEY}:{subject_id}"


class ChartConfigError(ValueError):
    """Raised when a chart config fails validation (unknown metric, missing
    param, bad name/series count). Routers map this to a 4xx redirect."""


def _sanitize_series(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, item in enumerate(raw[:MAX_SERIES_PER_CHART]):
        if not isinstance(item, dict) or not item.get("metric_key"):
            continue
        out.append({
            "domain": item.get("domain"),
            "metric_key": item.get("metric_key"),
            "param": item.get("param"),
            "label": item.get("label"),
            "color_slot": item.get("color_slot") if isinstance(item.get("color_slot"), int) else i,
        })
    return out


def _sanitize(raw: Any) -> list[dict]:
    """Project arbitrary stored data onto a clean list of chart configs.

    Drops entries missing id/name/series, caps series-per-chart and
    charts-per-list. Unknown ``metric_key`` values are KEPT here (only
    resolution at read time drops them) so a disabled module's charts still
    list — they'll just resolve to fewer series."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:MAX_CHARTS]:
        if not isinstance(item, dict):
            continue
        chart_id = item.get("id")
        name = item.get("name")
        series = _sanitize_series(item.get("series"))
        if not chart_id or not name or not series:
            continue
        out.append({
            "id": str(chart_id),
            "name": str(name),
            "normalize": bool(item.get("normalize", False)),
            "series": series,
        })
    return out


async def list_charts(
    session: AsyncSession,
    redis: Optional[Redis] = None,
    *,
    subject_id: uuid.UUID,
) -> list[dict]:
    """Resolve one subject's saved chart list. Never raises — falls back to ``[]``.

    Order: Redis cache → their scoped setting → ``[]``. The scoped read still
    falls back to the legacy ``app_settings`` row on its own, so pre-backfill
    installations keep their charts without a bridge here.
    """
    redis_key = cache_key(subject_id)
    if redis is not None:
        try:
            cached = await redis.get(redis_key)
            if cached:
                return _sanitize(json.loads(cached))
        except Exception:
            logger.warning(
                "custom_charts: Redis read failed; falling through to DB", exc_info=True
            )

    try:
        raw = await get_scoped_setting(
            session,
            scope=SettingScope.SUBJECT,
            key=ScopedSettingKey.CUSTOM_CHARTS,
            scope_id=subject_id,
            default=[],
        )
        if isinstance(raw, list):
            charts = _sanitize(raw)
            await prime_cache(redis, charts, subject_id=subject_id)
            return charts
        logger.warning(
            "custom_charts: subject setting is not an array (%s); using []",
            type(raw).__name__,
        )
        return []
    except Exception:
        logger.warning("custom_charts: DB read failed; using []", exc_info=True)

    return []


async def get_chart(
    session: AsyncSession,
    chart_id: str,
    redis: Optional[Redis] = None,
    *,
    subject_id: uuid.UUID,
) -> Optional[dict]:
    charts = await list_charts(session, redis, subject_id=subject_id)
    for c in charts:
        if c["id"] == chart_id:
            return c
    return None


def _validate_series(series: list[dict]) -> None:
    if not series:
        raise ChartConfigError("a chart needs at least one series")
    if len(series) > MAX_SERIES_PER_CHART:
        raise ChartConfigError(f"a chart can have at most {MAX_SERIES_PER_CHART} series")
    for s in series:
        metric_key = s.get("metric_key")
        if not metric_key:
            raise ChartConfigError("every series needs a metric_key")
        try:
            field = chart_registry.get(metric_key)
        except KeyError:
            raise ChartConfigError(f"unknown metric_key '{metric_key}'") from None
        has_param = bool(s.get("param"))
        if field.param_kind != "none" and not has_param:
            raise ChartConfigError(f"metric '{metric_key}' requires a param")
        if field.param_kind == "none" and has_param:
            raise ChartConfigError(f"metric '{metric_key}' does not take a param")


async def create_chart(
    session: AsyncSession,
    *,
    name: str,
    series: list[dict],
    normalize: bool = False,
    redis: Optional[Redis] = None,
    subject_id: uuid.UUID,
) -> dict:
    """Validate and append a new chart config. Flushes (caller commits).

    Raises ``ChartConfigError`` on any validation failure — nothing is
    persisted in that case."""
    name = (name or "").strip()
    if not name:
        raise ChartConfigError("chart name is required")
    if len(name) > 80:
        raise ChartConfigError("chart name must be at most 80 characters")
    _validate_series(series)

    new_chart = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "normalize": bool(normalize),
        "series": [
            {
                "domain": s.get("domain"),
                "metric_key": s["metric_key"],
                "param": s.get("param"),
                "label": s.get("label"),
                "color_slot": idx,
            }
            for idx, s in enumerate(series[:MAX_SERIES_PER_CHART])
        ],
    }
    def _append(raw: Any) -> list[dict]:
        current = _sanitize(raw)
        if len(current) >= MAX_CHARTS:
            raise ChartConfigError(
                f"at most {MAX_CHARTS} custom charts are allowed"
            )
        return [*current, new_chart]

    updated = await update_scoped_setting(
        session,
        scope=SettingScope.SUBJECT,
        key=ScopedSettingKey.CUSTOM_CHARTS,
        scope_id=subject_id,
        default=[],
        update=_append,
    )
    await prime_cache(redis, updated, subject_id=subject_id)
    return new_chart


async def delete_chart(
    session: AsyncSession,
    chart_id: str,
    redis: Optional[Redis] = None,
    *,
    subject_id: uuid.UUID,
) -> bool:
    """Remove one chart by id. Returns False if it wasn't found."""
    removed = False

    def _remove(raw: Any) -> list[dict]:
        nonlocal removed
        current = _sanitize(raw)
        remaining = [c for c in current if c["id"] != chart_id]
        removed = len(remaining) != len(current)
        return remaining

    remaining = await update_scoped_setting(
        session,
        scope=SettingScope.SUBJECT,
        key=ScopedSettingKey.CUSTOM_CHARTS,
        scope_id=subject_id,
        default=[],
        update=_remove,
    )
    if removed:
        await prime_cache(redis, remaining, subject_id=subject_id)
    return removed


async def prime_cache(
    redis: Optional[Redis],
    charts: list[dict],
    *,
    subject_id: uuid.UUID,
) -> None:
    """Write-through the resolved list into Redis. Best-effort (logged on fail)."""
    if redis is None:
        return
    try:
        await redis.set(
            cache_key(subject_id),
            json.dumps(_sanitize(charts)),
            ex=REDIS_TTL,
        )
    except Exception:
        logger.warning("custom_charts: Redis prime failed", exc_info=True)
