"""How fresh each data source is — the status card at the foot of the nav rail.

Vitals is a data lake fed by imports, so the question the chrome should answer
without opening anything is "is anything silently not arriving?". One row per
source: green while it is inside its expected cadence, amber once it is past it.

Three ``MAX(date)`` reads against already-indexed date columns, so this is cheap
enough to run per page render (``web.deps.load_nav_status`` calls it for HTML
GETs only). Like ``modules_service.get_enabled_modules`` it NEVER raises — the
chrome must render even when a source table is unreadable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.garmin import GarminDaily
from vitals.models.hevy import HevyWorkout
from vitals.models.labs import LabResult
from vitals.services.modules_service import CORE_KEYS
from vitals.utils.timeutils import today_local

logger = logging.getLogger(__name__)

# key → (module key that gates the row, newest-date column, days before amber).
# Labs get a long window on purpose: a quarterly panel is normal, a lab that is
# three months old is the point at which it is worth saying so.
_SOURCES: tuple[tuple[str, str, object, int], ...] = (
    ("garmin", "garmin", GarminDaily.date, 2),
    ("hevy", "hevy", HevyWorkout.date, 4),
    ("labs", "labs", LabResult.date, 90),
)


@dataclass(frozen=True)
class SyncRow:
    key: str                       # i18n suffix — nav.<key>
    days: Optional[int]            # days since the newest row; None = no data yet
    stale: bool                    # past this source's expected cadence


async def sync_rows(
    session: AsyncSession, enabled: Optional[dict[str, bool]] = None
) -> list[SyncRow]:
    """Freshness of every enabled source, in display order. Never raises."""
    em = enabled or {}
    today = today_local()
    rows: list[SyncRow] = []
    for key, module_key, column, window in _SOURCES:
        if not em.get(module_key, module_key in CORE_KEYS):
            continue
        try:
            newest: Optional[date_type] = (
                await session.execute(select(func.max(column)))
            ).scalar()
        except Exception:
            logger.warning("nav status: %s freshness read failed", key, exc_info=True)
            continue
        days = (today - newest).days if newest else None
        rows.append(SyncRow(key=key, days=days, stale=days is None or days > window))
    return rows
