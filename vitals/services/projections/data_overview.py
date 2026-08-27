"""Subject-scoped coverage projection for every persisted health domain."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models import (
    Annotation,
    BodyMeasurement,
    BodyScan,
    DosePhase,
    GarminActivity,
    GarminDaily,
    GarminIntraday,
    GeneticVariant,
    HevyWorkout,
    HrtCycle,
    HrtDose,
    HrtSideEffect,
    Injection,
    LabResult,
    MealLog,
    Milestone,
    NoiseMarker,
    SideEffect,
    SkincareLog,
    SkincareObservation,
    Supplement,
    WeightLog,
    WeeklyDigest,
)


async def project_data_overview(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> dict:
    """Return exact per-domain counts and date coverage for one subject."""
    dated = [
        ("weight", WeightLog, WeightLog.date),
        ("measurements", BodyMeasurement, BodyMeasurement.date),
        ("body_scans", BodyScan, BodyScan.date),
        ("glp1_injections", Injection, Injection.date),
        ("side_effects", SideEffect, SideEffect.date),
        ("garmin_daily", GarminDaily, GarminDaily.date),
        ("garmin_activities", GarminActivity, GarminActivity.date),
        ("garmin_intraday", GarminIntraday, GarminIntraday.date),
        ("workouts", HevyWorkout, HevyWorkout.date),
        ("labs", LabResult, LabResult.date),
        ("nutrition", MealLog, MealLog.date),
        ("skincare_logs", SkincareLog, SkincareLog.date),
        ("skincare_observations", SkincareObservation, SkincareObservation.date),
        ("weekly_digests", WeeklyDigest, WeeklyDigest.date),
        ("timeline", Annotation, Annotation.date),
        ("noise_markers", NoiseMarker, NoiseMarker.start_date),
        ("hrt_doses", HrtDose, HrtDose.date),
        ("hrt_side_effects", HrtSideEffect, HrtSideEffect.date),
        ("hrt_cycles", HrtCycle, HrtCycle.start_date),
    ]
    count_only = [
        ("supplements", Supplement),
        ("genetics", GeneticVariant),
        ("milestones", Milestone),
        ("dose_phases", DosePhase),
    ]

    overview: dict = {}
    for name, model, date_col in dated:
        cols = [func.count(), func.min(date_col), func.max(date_col)]
        updated_col = getattr(model, "updated_at", None)
        if updated_col is not None:
            cols.append(func.max(updated_col))
        row = (
            await session.execute(
                select(*cols).where(model.subject_id == subject_id)
            )
        ).one()
        entry = {
            "count": row[0],
            "earliest": row[1].isoformat() if row[1] else None,
            "latest": row[2].isoformat() if row[2] else None,
        }
        if updated_col is not None:
            entry["last_updated"] = row[3].isoformat() if row[3] else None
        overview[name] = entry

    for name, model in count_only:
        count = (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(model.subject_id == subject_id)
            )
        ).scalar_one()
        overview[name] = {"count": count}

    return overview


__all__ = ["project_data_overview"]
