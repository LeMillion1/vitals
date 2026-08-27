"""Subject-scoped Hevy queries and read-model projections."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.analytics.progression import (
    ProgressionConfig,
    ProgressionVerdict,
    SessionResult,
    evaluate_progression,
)
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.services.hevy.normalization import _WORKING_SET_TYPES


@dataclass(frozen=True, slots=True)
class WorkoutWindowSummary:
    """Column-minimal workout coverage shared by clinical projections."""

    current_count: int
    latest_date: date_type | None


async def list_workouts(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    limit: int = 50,
) -> Sequence[HevyWorkout]:
    stmt = (
        select(HevyWorkout)
        .where(HevyWorkout.subject_id == subject_id)
        .options(selectinload(HevyWorkout.exercises).selectinload(HevyExercise.sets))
        .order_by(HevyWorkout.date.desc(), HevyWorkout.start_time.desc())
    )
    if start is not None:
        stmt = stmt.where(HevyWorkout.date >= start)
    if end is not None:
        stmt = stmt.where(HevyWorkout.date <= end)
    result = await session.execute(stmt.limit(limit))
    return result.scalars().all()


def workout_summary(workout: HevyWorkout) -> dict:
    """One session the way a reader needs it: when, what, how much work.

    Tonnage counts working sets only — warm-ups add kilograms without adding
    training, which is the same line the progression engine already draws.
    """

    volume = 0.0
    working_sets = 0
    exercise_details = []
    for exercise in workout.exercises:
        exercise_volume = 0.0
        exercise_sets = 0
        weights: list[float] = []
        reps: list[int] = []
        rpes: list[float] = []
        for hevy_set in exercise.sets:
            if hevy_set.set_type not in _WORKING_SET_TYPES:
                continue
            working_sets += 1
            exercise_sets += 1
            if hevy_set.weight_kg and hevy_set.reps:
                set_volume = hevy_set.weight_kg * hevy_set.reps
                volume += set_volume
                exercise_volume += set_volume
            if hevy_set.weight_kg is not None:
                weights.append(hevy_set.weight_kg)
            if hevy_set.reps is not None:
                reps.append(hevy_set.reps)
            if hevy_set.rpe is not None:
                rpes.append(hevy_set.rpe)
        exercise_details.append(
            {
                "title": exercise.title,
                "working_sets": exercise_sets,
                "volume_kg": round(exercise_volume) or None,
                "top_weight_kg": max(weights) if weights else None,
                "total_reps": sum(reps) if reps else None,
                "mean_rpe": round(sum(rpes) / len(rpes), 1) if rpes else None,
            }
        )
    return {
        "date": workout.date.isoformat(),
        "title": workout.title,
        "program": workout.program,
        "start_time": workout.start_time.isoformat() if workout.start_time else None,
        "duration_min": (
            round(workout.duration_seconds / 60) if workout.duration_seconds else None
        ),
        "working_sets": working_sets,
        "volume_kg": round(volume) or None,
        "exercises": [exercise.title for exercise in workout.exercises],
        "exercise_details": exercise_details,
    }


async def workout_count(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    since: Optional[date_type] = None,
) -> int:
    stmt = (
        select(func.count())
        .select_from(HevyWorkout)
        .where(HevyWorkout.subject_id == subject_id)
    )
    if since is not None:
        stmt = stmt.where(HevyWorkout.date >= since)
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def workout_window_summary(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type,
    end: date_type,
) -> WorkoutWindowSummary:
    """Count workouts in ``start..end`` and retain the latest through ``end``."""

    current_count, latest = (
        await session.execute(
            select(
                func.count().filter(HevyWorkout.date >= start),
                func.max(HevyWorkout.date),
            ).where(
                HevyWorkout.subject_id == subject_id,
                HevyWorkout.date <= end,
            )
        )
    ).one()
    return WorkoutWindowSummary(
        current_count=int(current_count or 0),
        latest_date=latest,
    )


async def latest_workout_date(
    session: AsyncSession, *, subject_id: uuid.UUID
) -> Optional[date_type]:
    result = await session.execute(
        select(func.max(HevyWorkout.date)).where(
            HevyWorkout.subject_id == subject_id
        )
    )
    return result.scalar()


async def exercise_catalog(
    session: AsyncSession, *, subject_id: uuid.UUID
) -> list[dict]:
    """Distinct exercises seen across this person's workouts."""

    result = await session.execute(
        select(
            HevyExercise.exercise_template_id,
            HevyExercise.title,
            func.count(func.distinct(HevyExercise.workout_id)).label("sessions"),
            func.max(HevyWorkout.date).label("last_date"),
        )
        .join(HevyWorkout, HevyExercise.workout_id == HevyWorkout.id)
        .where(
            HevyExercise.exercise_template_id.is_not(None),
            HevyWorkout.subject_id == subject_id,
        )
        .group_by(HevyExercise.exercise_template_id, HevyExercise.title)
        .order_by(func.max(HevyWorkout.date).desc())
    )
    return [
        {
            "exercise_template_id": template_id,
            "title": title,
            "sessions": int(sessions),
            "last_date": last_date.isoformat() if last_date else None,
        }
        for template_id, title, sessions, last_date in result.all()
    ]


async def _exercise_sessions(
    session: AsyncSession, exercise_template_id: str, *, subject_id: uuid.UUID
) -> list[tuple[date_type, list[HevySet], Optional[str]]]:
    """Return each matching session oldest first with working sets and notes."""

    result = await session.execute(
        select(HevyWorkout.date, HevyExercise.id, HevyExercise.notes)
        .join(HevyExercise, HevyExercise.workout_id == HevyWorkout.id)
        .where(
            HevyExercise.exercise_template_id == exercise_template_id,
            HevyWorkout.subject_id == subject_id,
        )
        .order_by(HevyWorkout.date)
    )
    rows = result.all()
    sessions: list[tuple[date_type, list[HevySet], Optional[str]]] = []
    for on_date, exercise_id, notes in rows:
        set_result = await session.execute(
            select(HevySet)
            .where(HevySet.exercise_id == exercise_id)
            .order_by(HevySet.set_index)
        )
        sets = [
            hevy_set
            for hevy_set in set_result.scalars().all()
            if hevy_set.set_type in _WORKING_SET_TYPES
        ]
        if sets:
            sessions.append((on_date, sets, notes))
    return sessions


def _top_weight_session(
    on_date: date_type, sets: list[HevySet]
) -> Optional[SessionResult]:
    """Reduce working sets to the progression engine's session shape."""

    weighted = [
        hevy_set
        for hevy_set in sets
        if hevy_set.weight_kg is not None and hevy_set.reps is not None
    ]
    if not weighted:
        return None
    top = max(hevy_set.weight_kg for hevy_set in weighted)
    reps = [
        hevy_set.reps for hevy_set in weighted if hevy_set.weight_kg == top
    ]
    return SessionResult(on_date=on_date, weight_kg=top, reps=reps)


async def working_weight_series(
    session: AsyncSession, exercise_template_id: str, *, subject_id: uuid.UUID
) -> list[dict]:
    """Top working weight per session over time."""

    sessions = await _exercise_sessions(
        session, exercise_template_id, subject_id=subject_id
    )
    series: list[dict] = []
    for on_date, sets, _notes in sessions:
        session_result = _top_weight_session(on_date, sets)
        if session_result is not None:
            series.append(
                {
                    "date": on_date.isoformat(),
                    "weight_kg": session_result.weight_kg,
                    "top_reps": (
                        max(session_result.reps) if session_result.reps else None
                    ),
                    "sets": len(session_result.reps),
                }
            )
    return series


async def progression_for_exercise(
    session: AsyncSession,
    exercise_template_id: str,
    config: Optional[ProgressionConfig] = None,
    *,
    subject_id: uuid.UUID,
) -> Optional[ProgressionVerdict]:
    """Return the progression verdict for one exercise from its history."""

    sessions = await _exercise_sessions(
        session, exercise_template_id, subject_id=subject_id
    )
    results = [
        session_result
        for on_date, sets, _notes in sessions
        if (session_result := _top_weight_session(on_date, sets)) is not None
    ]
    return evaluate_progression(results, config or ProgressionConfig())


async def latest_notes(
    session: AsyncSession, exercise_template_id: str, *, subject_id: uuid.UUID
) -> Optional[str]:
    """Return the most recent technique note recorded for an exercise."""

    sessions = await _exercise_sessions(
        session, exercise_template_id, subject_id=subject_id
    )
    for _date, _sets, notes in reversed(sessions):
        if notes:
            return notes
    return None


__all__ = [
    "WorkoutWindowSummary",
    "_exercise_sessions",
    "_top_weight_session",
    "exercise_catalog",
    "latest_notes",
    "latest_workout_date",
    "list_workouts",
    "progression_for_exercise",
    "working_weight_series",
    "workout_count",
    "workout_summary",
    "workout_window_summary",
]
