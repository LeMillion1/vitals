"""Weight chart analytics and optional cross-domain overlays."""
from __future__ import annotations

from vitals.services.timeline import annotations as timeline_annotations

from vitals.services.glp1 import queries as glp1_queries

import uuid
from datetime import date as date_type, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.analytics import exclude_ranges
from vitals.analytics.regression import fit_trend, project_date_for_value
from vitals.analytics.rolling import rolling_mean_by_date
from vitals.i18n import t
from vitals.models.weight import DOMAIN

from .logs import list_active_weights
from .measurements import list_body_measurements
from .noise import noise_ranges


async def chart_series(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    goal_kg: Optional[float] = None,
    include_bia: bool = False,
    include_timeline: bool = False,
    include_glp1: bool = True,
    end: Optional[date_type] = None,
) -> dict:
    """Assemble the Weight dashboard's JSON-serialisable chart series."""
    weights = await list_active_weights(
        session,
        end=end,
        subject_id=subject_id,
    )
    raw_points = [(weight.date, weight.weight_kg) for weight in weights]

    ranges = [
        (start, range_end)
        for start, range_end in await noise_ranges(
            session,
            subject_id=subject_id,
            end=end,
        )
        if end is None or start <= end
    ]
    clean_points = exclude_ranges(raw_points, ranges)
    ma = rolling_mean_by_date(clean_points, window_days=7)

    measurements = [
        row
        for row in await list_body_measurements(
            session,
            subject_id=subject_id,
            end=end,
        )
        if end is None or row.date <= end
    ]
    lbm_points = [
        {"date": measurement.date.isoformat(), "lbm_kg": measurement.lbm_kg}
        for measurement in measurements
        if measurement.lbm_kg is not None
    ]

    weekly_delta = None
    if ma:
        last_date, last_ma = ma[-1]
        cutoff = last_date - timedelta(days=7)
        prior = [value for day, value in ma if day <= cutoff]
        if prior:
            weekly_delta = round(last_ma - prior[-1], 2)

    trend = fit_trend(raw_points, exclude=ranges)
    projection = None
    if goal_kg is not None:
        projection_date = project_date_for_value(
            raw_points,
            goal_kg,
            exclude=ranges,
        )
        if projection_date is not None:
            projection = {
                "target_kg": goal_kg,
                "date": projection_date.isoformat(),
            }

    phases = (
        await _glp1_phase_overlays(
            session,
            subject_id=subject_id,
        )
        if include_glp1
        else []
    )

    bia = None
    if include_bia:
        from vitals.services.body_scan.scans import queries as body_scan_queries

        bia = await body_scan_queries.bia_chart_points(
            session,
            subject_id=subject_id,
        )

    annotations = None
    if include_timeline:


        annotations = await timeline_annotations.overlays_for(
            session,
            subject_id=subject_id,
            domain=DOMAIN,
        )

    return {
        "raw": [
            {"date": day.isoformat(), "weight_kg": value}
            for day, value in raw_points
        ],
        "trend_ma": [
            {"date": day.isoformat(), "weight_kg": value}
            for day, value in ma
        ],
        "lbm": lbm_points,
        "noise": [
            {"start": start.isoformat(), "end": end.isoformat() if end else None}
            for start, end in ranges
        ],
        "phases": phases,
        "projection": projection,
        "trend": (
            {"slope_per_week": round(trend.slope_per_week, 3)} if trend else None
        ),
        "weekly_delta": weekly_delta,
        "bia": bia,
        "annotations": annotations,
    }


async def _glp1_phase_overlays(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> list[dict]:
    """Return GLP-1 dose phases for the Weight chart overlay."""


    phases = await glp1_queries.list_dose_phases(
        session,
        subject_id=subject_id,
    )
    return [
        {
            "start": phase.start_date.isoformat(),
            "end": phase.end_date.isoformat() if phase.end_date else None,
            "drug": phase.drug,
            "dose_mg": phase.dose_mg,
            "label": f"{phase.drug} {phase.dose_mg:g} {t('common.mg')}",
        }
        for phase in phases
    ]
