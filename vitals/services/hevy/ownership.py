"""Ownership and provenance invariants for Hevy ingestion."""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.hevy import DOMAIN, HevyExercise, HevySet, HevyWorkout
from vitals.models.identity import HealthSubject
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity


class HevyOwnershipError(Exception):
    """Base class for fail-closed owned Hevy ingestion errors."""


class HevyOwnershipValidationError(HevyOwnershipError):
    """The caller did not supply the strict typed S/A/C contract."""


class HevyOwnershipReferenceError(HevyOwnershipError):
    """The requested connection is not a usable Hevy account for the subject."""


class HevyOwnershipInactiveConnectionError(HevyOwnershipReferenceError):
    """A non-active provenance root cannot authorize a fresh provider fetch."""


class HevyOwnershipConflictError(HevyOwnershipError):
    """Persisted provenance conflicts with the requested ownership scope."""


class HevyOwnershipAmbiguityError(HevyOwnershipConflictError):
    """More than one row is eligible for an owned workout lookup/adoption."""


class HevyRawPayloadInvariantError(HevyOwnershipConflictError):
    """A raw payload cannot safely produce a workout in the requested scope."""


def _validate_owned_scope(
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> None:
    if not isinstance(identity, WriteIdentity):
        raise HevyOwnershipValidationError("identity must be a WriteIdentity")
    if not isinstance(integration_connection_id, uuid.UUID):
        raise HevyOwnershipValidationError(
            "integration_connection_id must be a UUID"
        )


async def _require_owned_hevy_connection(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    allow_retired: bool,
    for_update: bool = False,
) -> IntegrationConnection:
    """Validate the provider root before a fetch or normalized-row mutation."""

    _validate_owned_scope(
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    statement = select(IntegrationConnection).where(
        IntegrationConnection.id == integration_connection_id
    )
    if for_update:
        statement = statement.with_for_update()
    with session.no_autoflush:
        connection = await session.scalar(statement)
    if connection is None:
        raise HevyOwnershipReferenceError("Hevy connection does not exist")
    if connection.subject_id != identity.subject_id:
        raise HevyOwnershipReferenceError(
            "Hevy connection belongs to another subject"
        )
    if (
        connection.provider != IntegrationProvider.HEVY.value
        or connection.connection_type != IntegrationConnectionType.ACCOUNT.value
    ):
        raise HevyOwnershipReferenceError(
            "connection is not a Hevy account provenance root"
        )
    known_statuses = {status.value for status in IntegrationConnectionStatus}
    if connection.status not in known_statuses:
        raise HevyOwnershipReferenceError(
            "Hevy connection has an unknown lifecycle state"
        )
    allowed_statuses = {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }
    if allow_retired:
        allowed_statuses.update(
            {
                IntegrationConnectionStatus.DISABLED.value,
                IntegrationConnectionStatus.RETIRED.value,
            }
        )
    if connection.status not in allowed_statuses:
        raise HevyOwnershipInactiveConnectionError(
            f"Hevy connection status {connection.status!r} cannot authorize this operation"
        )
    return connection


async def _lock_owned_hevy_scope(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    allow_retired: bool,
) -> IntegrationConnection:
    """Serialize owned writes in the shared Subject -> Connection lock order."""

    _validate_owned_scope(
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    with session.no_autoflush:
        subject_id = await session.scalar(
            select(HealthSubject.id)
            .where(HealthSubject.id == identity.subject_id)
            .with_for_update()
        )
    if subject_id is None:
        raise HevyOwnershipReferenceError("identity subject does not exist")
    return await _require_owned_hevy_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=allow_retired,
        for_update=True,
    )


async def _require_single_subject_legacy_adoption(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> None:
    """Keep S/C-null workout adoption behind the single-subject bridge."""

    with session.no_autoflush:
        subject_ids = list(
            await session.scalars(
                select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
            )
        )
    if subject_ids != [subject_id]:
        raise HevyOwnershipConflictError(
            "unowned legacy Hevy workout cannot be adopted after multi-subject "
            "activation"
        )


async def _resolve_owned_workout(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    external_id: str,
) -> tuple[Optional[HevyWorkout], bool]:
    """Return an exact row or one safely adoptable nullable legacy row.

    A Hevy workout id is unique inside the account it came from, so the lookup
    is scoped by connection; a row that has not been adopted yet carries no
    connection and is still a candidate for the one now claiming it. The boolean
    says whether the returned root still needs ownership adoption. Rows in a
    foreign subject or connection scope are never returned.
    """

    with session.no_autoflush:
        rows = list(
            await session.scalars(
                select(HevyWorkout)
                .where(
                    HevyWorkout.external_id == external_id,
                    or_(
                        HevyWorkout.integration_connection_id
                        == integration_connection_id,
                        HevyWorkout.integration_connection_id.is_(None),
                    ),
                )
                .order_by(HevyWorkout.id)
                .with_for_update()
            )
        )

    exact = [
        row
        for row in rows
        if row.subject_id == identity.subject_id
        and row.integration_connection_id == integration_connection_id
    ]
    compatible_legacy = [
        row
        for row in rows
        if row not in exact
        and row.subject_id in {None, identity.subject_id}
        and row.integration_connection_id in {None, integration_connection_id}
    ]
    if len(exact) > 1:
        raise HevyOwnershipAmbiguityError(
            "multiple workouts match the exact subject/connection/external scope"
        )
    if exact:
        if compatible_legacy:
            raise HevyOwnershipAmbiguityError(
                "both exact and compatible legacy workouts match the external id"
            )
        return exact[0], False

    if len(compatible_legacy) > 1:
        raise HevyOwnershipAmbiguityError(
            "multiple compatible legacy workouts match the external id"
        )
    if compatible_legacy:
        if len(rows) != 1:
            raise HevyOwnershipAmbiguityError(
                "legacy workout adoption conflicts with another ownership scope"
            )
        legacy = compatible_legacy[0]
        if legacy.subject_id is None and legacy.integration_connection_id is None:
            await _require_single_subject_legacy_adoption(
                session,
                subject_id=identity.subject_id,
            )
        return legacy, True

    if rows:
        raise HevyOwnershipConflictError(
            "Hevy external id in this connection belongs to another subject"
        )
    return None, False


def _validate_owned_raw_payload(
    raw_row: RawPayload,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    external_id: str,
) -> dict:
    if raw_row.subject_id != identity.subject_id:
        raise HevyRawPayloadInvariantError(
            "raw payload belongs to another subject"
        )
    if raw_row.integration_connection_id != integration_connection_id:
        raise HevyRawPayloadInvariantError(
            "raw payload belongs to another integration connection"
        )
    if raw_row.domain != DOMAIN or raw_row.source != Source.HEVY_API.value:
        raise HevyRawPayloadInvariantError(
            "raw payload is not a Hevy workouts payload"
        )
    if raw_row.external_id != external_id:
        raise HevyRawPayloadInvariantError(
            "raw payload external id does not match the workout"
        )
    if raw_row.file_asset_id is not None:
        raise HevyRawPayloadInvariantError(
            "Hevy account payload cannot reference a file asset"
        )
    if not isinstance(raw_row.payload, dict):
        raise HevyRawPayloadInvariantError("Hevy raw payload must be an object")
    payload_external_id = str(raw_row.payload.get("id") or "").strip()
    if not payload_external_id or payload_external_id != external_id:
        raise HevyRawPayloadInvariantError(
            "Hevy payload id does not match its raw external id"
        )
    return raw_row.payload


def _validate_workout_raw_link(
    workout: HevyWorkout,
    *,
    raw_payload_id: int,
) -> None:
    if workout.raw_payload_id not in {None, raw_payload_id}:
        raise HevyRawPayloadInvariantError(
            "workout already references a different raw payload"
        )


async def _preflight_workout_raw_link(
    session: AsyncSession,
    *,
    workout: HevyWorkout | None,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    external_id: str,
) -> None:
    """Reject an incompatible normalized link before strict raw refresh mutates it."""

    if workout is None or workout.raw_payload_id is None:
        return
    with session.no_autoflush:
        linked = await session.scalar(
            select(RawPayload).where(RawPayload.id == workout.raw_payload_id)
        )
    if linked is None:
        raise HevyRawPayloadInvariantError(
            "workout raw payload no longer exists"
        )
    if linked.subject_id not in {None, identity.subject_id} or (
        linked.integration_connection_id not in {None, integration_connection_id}
    ):
        raise HevyRawPayloadInvariantError(
            "workout raw payload has incompatible ownership"
        )
    if (
        linked.domain != DOMAIN
        or linked.source != Source.HEVY_API.value
        or linked.external_id != external_id
        or linked.file_asset_id is not None
        or not isinstance(linked.payload, dict)
        or str(linked.payload.get("id") or "").strip() != external_id
    ):
        raise HevyRawPayloadInvariantError(
            "workout raw payload has incompatible provenance"
        )


def _owned_child_scope(
    model: type[HevyExercise] | type[HevySet],
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID,
    include_unowned_legacy: bool,
):
    exact = and_(
        model.subject_id == subject_id,
        model.integration_connection_id == integration_connection_id,
    )
    if not include_unowned_legacy:
        return exact
    return and_(
        or_(model.subject_id == subject_id, model.subject_id.is_(None)),
        or_(
            model.integration_connection_id == integration_connection_id,
            model.integration_connection_id.is_(None),
        ),
    )


async def _owned_children_need_adoption(
    session: AsyncSession,
    *,
    workout: HevyWorkout,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> bool:
    """Validate an existing graph and report compatible nullable child roots."""

    exercises = list(
        await session.scalars(
            select(HevyExercise)
            .where(HevyExercise.workout_id == workout.id)
            .order_by(HevyExercise.id)
            .with_for_update()
        )
    )
    needs_adoption = False
    for exercise in exercises:
        if exercise.subject_id not in {None, identity.subject_id} or (
            exercise.integration_connection_id
            not in {None, integration_connection_id}
        ):
            raise HevyOwnershipConflictError(
                "workout contains an exercise from an incompatible ownership scope"
            )
        needs_adoption = needs_adoption or exercise.subject_id is None or (
            exercise.integration_connection_id is None
        )

    if not exercises:
        return needs_adoption
    sets = list(
        await session.scalars(
            select(HevySet)
            .where(HevySet.exercise_id.in_([row.id for row in exercises]))
            .order_by(HevySet.id)
            .with_for_update()
        )
    )
    for hevy_set in sets:
        if hevy_set.subject_id not in {None, identity.subject_id} or (
            hevy_set.integration_connection_id
            not in {None, integration_connection_id}
        ):
            raise HevyOwnershipConflictError(
                "exercise contains a set from an incompatible ownership scope"
            )
        needs_adoption = needs_adoption or hevy_set.subject_id is None or (
            hevy_set.integration_connection_id is None
        )
    return needs_adoption


async def _adopt_owned_workout_children(
    session: AsyncSession,
    *,
    workout: HevyWorkout,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> None:
    """Fill compatible nullable child roots without rebuilding unchanged facts."""

    exercises = list(
        await session.scalars(
            select(HevyExercise).where(HevyExercise.workout_id == workout.id)
        )
    )
    exercise_ids = [row.id for row in exercises]
    sets = (
        list(
            await session.scalars(
                select(HevySet).where(HevySet.exercise_id.in_(exercise_ids))
            )
        )
        if exercise_ids
        else []
    )
    for child in [*exercises, *sets]:
        if child.subject_id is None:
            child.subject_id = identity.subject_id
        if child.integration_connection_id is None:
            child.integration_connection_id = integration_connection_id
    await session.flush()


async def _delete_owned_workout_children(
    session: AsyncSession,
    *,
    workout: HevyWorkout,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    include_unowned_legacy: bool,
) -> None:
    """Validate and delete only children inherited by this owned root."""

    exercises = list(
        await session.execute(
            select(
                HevyExercise.id,
                HevyExercise.subject_id,
                HevyExercise.integration_connection_id,
            )
            .where(HevyExercise.workout_id == workout.id)
            .order_by(HevyExercise.id)
        )
    )
    exercise_scope = _owned_child_scope(
        HevyExercise,
        subject_id=identity.subject_id,
        integration_connection_id=integration_connection_id,
        include_unowned_legacy=include_unowned_legacy,
    )
    allowed_exercise_ids = {
        row_id
        for row_id, subject_id, connection_id in exercises
        if (
            subject_id == identity.subject_id
            and connection_id == integration_connection_id
        )
        or (
            include_unowned_legacy
            and subject_id in {None, identity.subject_id}
            and connection_id in {None, integration_connection_id}
        )
    }
    if {row_id for row_id, _subject_id, _connection_id in exercises} != (
        allowed_exercise_ids
    ):
        raise HevyOwnershipConflictError(
            "workout contains an exercise from an incompatible ownership scope"
        )
    if not allowed_exercise_ids:
        return

    sets = list(
        await session.execute(
            select(
                HevySet.id,
                HevySet.subject_id,
                HevySet.integration_connection_id,
            )
            .where(HevySet.exercise_id.in_(allowed_exercise_ids))
            .order_by(HevySet.id)
        )
    )
    set_scope = _owned_child_scope(
        HevySet,
        subject_id=identity.subject_id,
        integration_connection_id=integration_connection_id,
        include_unowned_legacy=include_unowned_legacy,
    )
    allowed_set_ids = {
        row_id
        for row_id, subject_id, connection_id in sets
        if (
            subject_id == identity.subject_id
            and connection_id == integration_connection_id
        )
        or (
            include_unowned_legacy
            and subject_id in {None, identity.subject_id}
            and connection_id in {None, integration_connection_id}
        )
    }
    if {row_id for row_id, _subject_id, _connection_id in sets} != allowed_set_ids:
        raise HevyOwnershipConflictError(
            "exercise contains a set from an incompatible ownership scope"
        )

    if allowed_set_ids:
        await session.execute(
            HevySet.__table__.delete().where(
                HevySet.id.in_(allowed_set_ids),
                set_scope,
            )
        )
    await session.execute(
        HevyExercise.__table__.delete().where(
            HevyExercise.id.in_(allowed_exercise_ids),
            exercise_scope,
        )
    )
    await session.flush()


__all__ = [
    "HevyOwnershipAmbiguityError",
    "HevyOwnershipConflictError",
    "HevyOwnershipError",
    "HevyOwnershipInactiveConnectionError",
    "HevyOwnershipReferenceError",
    "HevyOwnershipValidationError",
    "HevyRawPayloadInvariantError",
    "_adopt_owned_workout_children",
    "_delete_owned_workout_children",
    "_lock_owned_hevy_scope",
    "_owned_child_scope",
    "_owned_children_need_adoption",
    "_preflight_workout_raw_link",
    "_require_owned_hevy_connection",
    "_require_single_subject_legacy_adoption",
    "_resolve_owned_workout",
    "_validate_owned_raw_payload",
    "_validate_owned_scope",
    "_validate_workout_raw_link",
]
