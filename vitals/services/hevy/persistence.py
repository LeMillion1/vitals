"""Owned Hevy workout graph persistence."""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.hevy import DOMAIN, HevyExercise, HevySet, HevyWorkout
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services.hevy.normalization import (
    _float_or_none,
    _int_or_none,
    _map_program,
    _parse_dt,
)
from vitals.services.hevy.ownership import (
    HevyOwnershipConflictError,
    HevyRawPayloadInvariantError,
    _delete_owned_workout_children,
    _resolve_owned_workout,
    _validate_owned_raw_payload,
    _validate_workout_raw_link,
)
from vitals.utils.timeutils import now_local


async def _get_workout_by_external(
    session: AsyncSession, external_id: str
) -> Optional[HevyWorkout]:
    result = await session.execute(
        select(HevyWorkout).where(HevyWorkout.external_id == external_id)
    )
    return result.scalars().first()


async def _upsert_owned_workout(
    session: AsyncSession,
    *,
    raw_row: RawPayload,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    workout: Optional[HevyWorkout] = None,
    adopt_legacy: bool = False,
) -> bool:
    """Create or rebuild one workout while preserving historical attribution."""

    raw = _validate_owned_raw_payload(
        raw_row,
        identity=identity,
        integration_connection_id=integration_connection_id,
        external_id=str(raw_row.external_id),
    )
    external_id = str(raw["id"]).strip()
    if workout is None:
        workout, adopt_legacy = await _resolve_owned_workout(
            session,
            identity=identity,
            integration_connection_id=integration_connection_id,
            external_id=external_id,
        )
    elif workout.external_id != external_id:
        raise HevyOwnershipConflictError(
            "resolved workout does not match the raw external id"
        )

    created = workout is None
    if workout is None:
        workout = HevyWorkout(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            integration_connection_id=integration_connection_id,
            external_id=external_id,
            domain=DOMAIN,
        )
        session.add(workout)
    else:
        _validate_workout_raw_link(workout, raw_payload_id=raw_row.id)
        if adopt_legacy:
            if workout.subject_id not in {None, identity.subject_id} or (
                workout.integration_connection_id
                not in {None, integration_connection_id}
            ):
                raise HevyOwnershipConflictError(
                    "only a compatible nullable legacy workout can be adopted"
                )
            if workout.subject_id is None:
                workout.subject_id = identity.subject_id
            if workout.integration_connection_id is None:
                workout.integration_connection_id = integration_connection_id
            # ``actor_user_id`` is historical origin. Never replace it with the
            # actor who happened to run this migration-era refresh.
        elif (
            workout.subject_id != identity.subject_id
            or workout.integration_connection_id != integration_connection_id
        ):
            raise HevyOwnershipConflictError(
                "workout belongs to another subject or integration connection"
            )

    start = _parse_dt(raw.get("start_time"))
    end = _parse_dt(raw.get("end_time"))
    duration = int((end - start).total_seconds()) if start and end else None
    workout.date = (start or end or now_local()).date()
    workout.source = Source.HEVY_API.value
    workout.raw_payload_id = raw_row.id
    workout.title = raw.get("title")
    workout.description = raw.get("description")
    workout.start_time = start
    workout.end_time = end
    workout.duration_seconds = duration
    workout.hevy_updated_at = _parse_dt(raw.get("updated_at"))
    workout.program = _map_program(raw)
    await session.flush()

    if not created:
        await _delete_owned_workout_children(
            session,
            workout=workout,
            identity=identity,
            integration_connection_id=integration_connection_id,
            # Structural children inherit their root. Legacy-null/partial child
            # columns are safe to rebuild even when the workout root was already
            # dual-written; foreign child ownership still fails closed.
            include_unowned_legacy=True,
        )

    for ex_raw in raw.get("exercises") or []:
        if not isinstance(ex_raw, dict):
            raise HevyRawPayloadInvariantError(
                "Hevy exercise payload must be an object"
            )
        exercise = HevyExercise(
            workout_id=workout.id,
            subject_id=identity.subject_id,
            integration_connection_id=integration_connection_id,
            exercise_index=_int_or_none(ex_raw.get("index")) or 0,
            title=ex_raw.get("title") or "—",
            exercise_template_id=ex_raw.get("exercise_template_id"),
            notes=ex_raw.get("notes"),
            superset_id=_int_or_none(ex_raw.get("superset_id")),
        )
        session.add(exercise)
        await session.flush()
        for set_raw in ex_raw.get("sets") or []:
            if not isinstance(set_raw, dict):
                raise HevyRawPayloadInvariantError(
                    "Hevy set payload must be an object"
                )
            session.add(
                HevySet(
                    exercise_id=exercise.id,
                    subject_id=identity.subject_id,
                    integration_connection_id=integration_connection_id,
                    set_index=_int_or_none(set_raw.get("index")) or 0,
                    set_type=(set_raw.get("type") or "normal"),
                    weight_kg=_float_or_none(set_raw.get("weight_kg")),
                    reps=_int_or_none(set_raw.get("reps")),
                    rpe=_float_or_none(set_raw.get("rpe")),
                    distance_m=_float_or_none(set_raw.get("distance_meters")),
                    duration_seconds=_int_or_none(
                        set_raw.get("duration_seconds")
                    ),
                )
            )
    await session.flush()
    return created


__all__ = ["_get_workout_by_external", "_upsert_owned_workout"]
