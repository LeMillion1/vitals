"""Date-window and data-coverage queries for shared reports."""

from __future__ import annotations

from datetime import date as date_type, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.share.ownership import PreparedShareOwner, _owner_or_zero_subject_legacy
from vitals.utils.timeutils import today_local

def window_for(days: int) -> tuple[date_type, date_type]:
    """``days`` **complete** days, ending yesterday.

    Counting back from today would hand the document a day with no sleep in it
    and, before dinner, no food either — and then average over it.
    """
    end = today_local() - timedelta(days=1)
    return end - timedelta(days=max(days, 1) - 1), end


def clamp_window(start: date_type, end: date_type) -> tuple[date_type, date_type]:
    """A window the reader picked, trimmed to days that are actually over."""
    yesterday = today_local() - timedelta(days=1)
    end = min(end, yesterday)
    return min(start, end), end


def default_period(days: int = 90) -> tuple[date_type, date_type]:
    """What the custom-range inputs open on."""
    return window_for(days)


async def earliest_data_date(
    session: AsyncSession,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> Optional[date_type]:
    """The oldest dated row in any domain a report can carry — what "all time"
    means. Nine cheap ``MIN()`` reads on one form submit, so a report that says
    it covers everything starts where the record actually starts rather than at
    some round number of years ago."""
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)

    from sqlalchemy import func

    from vitals.models.body_scan import BodyScan
    from vitals.models.garmin import GarminDaily
    from vitals.models.glp1 import Injection
    from vitals.models.hevy import HevyWorkout
    from vitals.models.hrt import HrtDose
    from vitals.models.labs import LabResult
    from vitals.models.nutrition import MealLog
    from vitals.models.weight import WeightLog

    models = (
        WeightLog,
        LabResult,
        GarminDaily,
        HevyWorkout,
        MealLog,
        HrtDose,
        Injection,
        BodyScan,
    )
    found = []
    for model in models:
        stmt = select(func.min(model.date))
        if owner is not None:
            stmt = stmt.where(model.subject_id == owner.identity.subject_id)
        value = (await session.execute(stmt)).scalar()
        if value is not None:
            found.append(value)
    return min(found) if found else None
