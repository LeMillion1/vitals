"""Hevy workouts service (module 5).

Owns the workouts domain:

  * **Sync** — pull workouts from the Hevy API, keep each full payload in
    ``raw_payloads``, and normalise the exercise→set tree into
    ``hevy_workouts`` / ``hevy_exercises`` / ``hevy_sets``. Re-sync is idempotent:
    a workout whose Hevy ``updated_at`` is unchanged is skipped; a changed one is
    re-normalised in place (children rebuilt). The upsert key is the Hevy id.
  * **Program mapping** — tag a workout with the training program it matches
    (title heuristic; overridable as routines/templates land).
  * **Progression** — per exercise, reduce the session history to the engine's
    ``SessionResult`` shape and ask ``analytics.progression`` what to do next
    (🟢 advance / 🟡 hold / 🔴 deload).
  * **Working-weight history** — per-exercise series for the dashboard charts.

The service is handed a client (tests pass a fake), never constructing one for
the network itself, keeping it unit-testable without Hevy.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date as date_type, datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, inspect as sa_inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.identity import HealthSubject
from vitals.models.hevy import DOMAIN, HevyExercise, HevySet, HevyWorkout
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import raw_payload_service
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.analytics.progression import (
    ProgressionConfig,
    ProgressionVerdict,
    SessionResult,
    evaluate_progression,
)
from vitals.utils.timeutils import now_local, to_local_naive

logger = logging.getLogger(__name__)

# Only these set types are "working sets" that drive progression / top-weight.
_WORKING_SET_TYPES = {"normal", "failure"}


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


# ── Parsing helpers ───────────────────────────────────────────────────────────
def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse a Hevy ISO-8601 timestamp into a naive **local** datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return to_local_naive(value)
    try:
        text = str(value).replace("Z", "+00:00")
        return to_local_naive(datetime.fromisoformat(text))
    except (ValueError, TypeError):
        return None


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _float_or_none(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _map_program(raw_workout: dict) -> Optional[str]:
    """Best-effort training-program tag from the workout title.

    A title like "Day A — Push" → "A". Deliberately light; richer template/routine
    matching can replace this without touching the schema (the column stays).
    """
    title = (raw_workout.get("title") or "").lower()
    for token, label in (("program a", "A"), ("program b", "B"), ("day a", "A"), ("day b", "B")):
        if token in title:
            return label
    return None


# ── Sync ──────────────────────────────────────────────────────────────────────
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


async def sync(
    session: AsyncSession,
    client: Any,
    *,
    max_pages: int = 50,
    force: bool = False,
) -> dict:
    """Fetch workouts and normalise them. Returns a summary dict
    (``fetched`` / ``created`` / ``updated`` / ``skipped``). Does not commit."""
    raw_workouts = await client.fetch_workouts(max_pages=max_pages)
    summary = {"fetched": len(raw_workouts), "created": 0, "updated": 0, "skipped": 0}

    for raw in raw_workouts:
        external_id = str(raw.get("id") or "").strip()
        if not external_id:
            summary["skipped"] += 1
            continue

        existing = await _get_workout_by_external(session, external_id)
        hevy_updated = _parse_dt(raw.get("updated_at"))
        if existing is not None and not force and existing.hevy_updated_at == hevy_updated:
            summary["skipped"] += 1
            continue

        raw_row = await raw_payload_service.upsert_raw_payload(
            session,
            domain=DOMAIN,
            source=Source.HEVY_API.value,
            external_id=external_id,
            payload=raw,
        )
        created = await _upsert_workout(session, raw, raw_payload_id=raw_row.id)
        raw_row.processed_at = now_local()
        summary["created" if created else "updated"] += 1

    await session.flush()
    return summary


async def sync_owned(
    session: AsyncSession,
    client: Any,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    max_pages: int = 50,
    force: bool = False,
) -> dict:
    """Fetch and normalize Hevy workouts inside one explicit S/A/C scope.

    The connection is validated before network I/O. Every fetched object goes
    through the strict owned raw-payload chokepoint even when its normalized row
    is unchanged, so an old S/C-null raw row is safely adopted instead of being
    bypassed by the idempotency shortcut. The caller owns commit or rollback.

    The fail-closed connection preflight is a database read before vendor I/O,
    so the caller's transaction remains open during the fetch. A later network /
    persistence split should remove that pool-pressure tradeoff before broad
    multi-user activation.
    """

    await _require_owned_hevy_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=False,
    )
    raw_workouts = await client.fetch_workouts(max_pages=max_pages)
    summary = {
        "fetched": len(raw_workouts),
        "created": 0,
        "updated": 0,
        "skipped": 0,
    }
    # Keep alert legacy adoption and provider ingestion on one canonical lock
    # order without holding the governance lock across vendor network latency.
    await acquire_identity_governance_lock(session)
    await _lock_owned_hevy_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=False,
    )

    for raw in raw_workouts:
        if not isinstance(raw, dict):
            summary["skipped"] += 1
            continue
        external_id = str(raw.get("id") or "").strip()
        if not external_id:
            summary["skipped"] += 1
            continue

        workout, adopt_legacy = await _resolve_owned_workout(
            session,
            identity=identity,
            integration_connection_id=integration_connection_id,
            external_id=external_id,
        )
        await _preflight_workout_raw_link(
            session,
            workout=workout,
            identity=identity,
            integration_connection_id=integration_connection_id,
            external_id=external_id,
        )
        children_need_adoption = (
            await _owned_children_need_adoption(
                session,
                workout=workout,
                identity=identity,
                integration_connection_id=integration_connection_id,
            )
            if workout is not None
            else False
        )
        raw_row = await raw_payload_service.upsert_owned_raw_payload(
            session,
            identity=identity,
            integration_connection_id=integration_connection_id,
            domain=DOMAIN,
            source=Source.HEVY_API.value,
            external_id=external_id,
            payload=raw,
        )
        _validate_owned_raw_payload(
            raw_row,
            identity=identity,
            integration_connection_id=integration_connection_id,
            external_id=external_id,
        )

        hevy_updated = _parse_dt(raw.get("updated_at"))
        if (
            workout is not None
            and not adopt_legacy
            and not force
            and workout.hevy_updated_at == hevy_updated
        ):
            _validate_workout_raw_link(workout, raw_payload_id=raw_row.id)
            if workout.raw_payload_id is None:
                workout.raw_payload_id = raw_row.id
            if children_need_adoption:
                await _adopt_owned_workout_children(
                    session,
                    workout=workout,
                    identity=identity,
                    integration_connection_id=integration_connection_id,
                )
            raw_row.processed_at = now_local()
            summary["skipped"] += 1
            continue

        created = await _upsert_owned_workout(
            session,
            raw_row=raw_row,
            identity=identity,
            integration_connection_id=integration_connection_id,
            workout=workout,
            adopt_legacy=adopt_legacy,
        )
        raw_row.processed_at = now_local()
        summary["created" if created else "updated"] += 1

    await session.flush()
    return summary


async def _get_workout_by_external(
    session: AsyncSession, external_id: str
) -> Optional[HevyWorkout]:
    result = await session.execute(
        select(HevyWorkout).where(HevyWorkout.external_id == external_id)
    )
    return result.scalars().first()


async def _upsert_workout(
    session: AsyncSession, raw: dict, *, raw_payload_id: int
) -> bool:
    """Create or refresh a workout + its exercise/set children. Returns True when a
    new workout row was created (False = updated in place)."""
    external_id = str(raw["id"])
    start = _parse_dt(raw.get("start_time"))
    end = _parse_dt(raw.get("end_time"))
    duration = None
    if start and end:
        duration = int((end - start).total_seconds())
    on_date = (start or end or now_local()).date()

    workout = await _get_workout_by_external(session, external_id)
    created = workout is None
    if workout is None:
        workout = HevyWorkout(external_id=external_id, domain=DOMAIN)
        session.add(workout)

    workout.date = on_date
    workout.source = Source.HEVY_API.value
    workout.raw_payload_id = raw_payload_id
    workout.title = raw.get("title")
    workout.description = raw.get("description")
    workout.start_time = start
    workout.end_time = end
    workout.duration_seconds = duration
    workout.hevy_updated_at = _parse_dt(raw.get("updated_at"))
    workout.program = _map_program(raw)
    await session.flush()

    # Rebuild children so a changed workout never leaves orphaned rows. Delete
    # sets then exercises explicitly (not relying on FK ON DELETE CASCADE, which
    # SQLite doesn't enforce by default) so the rebuild is DB-agnostic.
    if not created:
        ex_ids = (
            select(HevyExercise.id)
            .where(HevyExercise.workout_id == workout.id)
            .scalar_subquery()
        )
        await session.execute(HevySet.__table__.delete().where(HevySet.exercise_id.in_(ex_ids)))
        await session.execute(
            HevyExercise.__table__.delete().where(HevyExercise.workout_id == workout.id)
        )
        await session.flush()

    for ex_raw in raw.get("exercises") or []:
        exercise = HevyExercise(
            workout_id=workout.id,
            exercise_index=_int_or_none(ex_raw.get("index")) or 0,
            title=ex_raw.get("title") or "—",
            exercise_template_id=ex_raw.get("exercise_template_id"),
            notes=ex_raw.get("notes"),
            superset_id=_int_or_none(ex_raw.get("superset_id")),
        )
        session.add(exercise)
        await session.flush()
        for set_raw in ex_raw.get("sets") or []:
            session.add(
                HevySet(
                    exercise_id=exercise.id,
                    set_index=_int_or_none(set_raw.get("index")) or 0,
                    set_type=(set_raw.get("type") or "normal"),
                    weight_kg=_float_or_none(set_raw.get("weight_kg")),
                    reps=_int_or_none(set_raw.get("reps")),
                    rpe=_float_or_none(set_raw.get("rpe")),
                    distance_m=_float_or_none(set_raw.get("distance_meters")),
                    duration_seconds=_int_or_none(set_raw.get("duration_seconds")),
                )
            )
    await session.flush()
    return created


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


async def reparse_from_raw(session: AsyncSession, raw_row: RawPayload) -> None:
    """Re-derive a Hevy workout straight from its stored raw payload. Unlike a
    normal sync this skips re-upserting the raw row itself, so ``fetched_at``
    stays put — this is a re-derive, not a fresh pull. Used by
    :func:`reparse_pending` (the nightly sweep — raw_payload_service.
    sweep_pending_job)."""
    raw = raw_row.payload if isinstance(raw_row.payload, dict) else {}
    await _upsert_workout(session, raw, raw_payload_id=raw_row.id)


async def reparse_owned_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> None:
    """Re-derive one owned workout from an exact subject/connection raw row.

    ``identity`` authorizes the subject boundary but does not replace historical
    attribution: a newly recovered workout inherits ``raw_row.actor_user_id``.
    Retired connections are allowed here only because this API requires the
    caller to supply the exact root and the operation re-derives an already-owned
    historical payload; fresh sync remains forbidden on a retired root.
    """

    _validate_owned_scope(
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    if (
        not isinstance(raw_row, RawPayload)
        or not isinstance(raw_row.id, int)
        or isinstance(raw_row.id, bool)
    ):
        raise HevyRawPayloadInvariantError(
            "owned Hevy reparse requires a persisted raw payload"
        )
    raw_state = sa_inspect(raw_row)
    if (
        not raw_state.persistent
        or raw_state.session is not session.sync_session
    ):
        raise HevyRawPayloadInvariantError(
            "owned Hevy reparse rejects detached or forged raw payload state"
        )
    with session.no_autoflush:
        preliminary_raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_row.id)
            .execution_options(populate_existing=True)
        )
    if preliminary_raw is None:
        raise HevyRawPayloadInvariantError(
            "owned Hevy raw payload does not exist"
        )
    if preliminary_raw.subject_id != identity.subject_id:
        raise HevyRawPayloadInvariantError(
            "raw payload belongs to another subject"
        )
    if preliminary_raw.integration_connection_id != integration_connection_id:
        raise HevyRawPayloadInvariantError(
            "raw payload belongs to another integration connection"
        )
    await _lock_owned_hevy_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=True,
    )
    with session.no_autoflush:
        persisted_raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_row.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    if persisted_raw is None:
        raise HevyRawPayloadInvariantError(
            "owned Hevy raw payload does not exist"
        )
    raw_row = persisted_raw
    if raw_row.subject_id != identity.subject_id:
        raise HevyRawPayloadInvariantError(
            "raw payload changed subject ownership during reparse"
        )
    if raw_row.integration_connection_id != integration_connection_id:
        raise HevyRawPayloadInvariantError(
            "raw payload changed integration connection during reparse"
        )
    if not isinstance(raw_row.external_id, str) or not raw_row.external_id:
        raise HevyRawPayloadInvariantError(
            "owned Hevy raw payload requires an external id"
        )
    try:
        raw_identity = WriteIdentity(
            subject_id=raw_row.subject_id,
            actor_user_id=raw_row.actor_user_id,
        )
    except TypeError as exc:
        raise HevyRawPayloadInvariantError(
            "raw payload has invalid subject/actor attribution"
        ) from exc

    raw = _validate_owned_raw_payload(
        raw_row,
        identity=raw_identity,
        integration_connection_id=integration_connection_id,
        external_id=raw_row.external_id,
    )
    external_id = str(raw["id"]).strip()
    workout, adopt_legacy = await _resolve_owned_workout(
        session,
        identity=raw_identity,
        integration_connection_id=integration_connection_id,
        external_id=external_id,
    )
    await _upsert_owned_workout(
        session,
        raw_row=raw_row,
        identity=raw_identity,
        integration_connection_id=integration_connection_id,
        workout=workout,
        adopt_legacy=adopt_legacy,
    )


async def reparse_owned_pending(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    limit: int = raw_payload_service.REPARSE_BATCH,
    since_days: int = raw_payload_service.REPARSE_WINDOW_DAYS,
) -> int:
    """Sweep pending Hevy rows in one exact S/C scope. Does not commit.

    Each row uses a SAVEPOINT so a malformed payload cannot leave a partially
    rebuilt workout that the next ``has_normalized`` check would skip forever.
    """

    await _require_owned_hevy_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=True,
    )
    has_normalized = (
        select(HevyWorkout.id)
        .where(
            HevyWorkout.raw_payload_id == RawPayload.id,
            HevyWorkout.subject_id == identity.subject_id,
            HevyWorkout.integration_connection_id == integration_connection_id,
        )
        .exists()
    )
    cutoff = now_local() - timedelta(days=since_days)
    stmt = (
        select(RawPayload)
        .where(
            RawPayload.subject_id == identity.subject_id,
            RawPayload.integration_connection_id == integration_connection_id,
            RawPayload.domain == DOMAIN,
            RawPayload.source == Source.HEVY_API.value,
            RawPayload.processed_at.is_(None),
            RawPayload.fetched_at >= cutoff,
            ~has_normalized,
        )
        .order_by(RawPayload.id)
        .limit(limit)
    )
    rows = list(await session.scalars(stmt))
    done = 0
    for raw_row in rows:
        try:
            async with session.begin_nested():
                await reparse_owned_from_raw(
                    session,
                    raw_row,
                    identity=identity,
                    integration_connection_id=integration_connection_id,
                )
                raw_row.processed_at = now_local()
                await session.flush()
        except Exception:
            logger.warning(
                "owned Hevy re-parse failed for raw payload %s",
                raw_row.id,
                exc_info=True,
            )
            continue
        done += 1
    return done


async def reparse_pending(
    session: AsyncSession,
    *,
    limit: int = raw_payload_service.REPARSE_BATCH,
    since_days: int = raw_payload_service.REPARSE_WINDOW_DAYS,
) -> int:
    """Sweep Hevy raw payloads still pending a normalized workout row. Does not
    commit."""
    has_normalized = (
        select(HevyWorkout.id).where(HevyWorkout.raw_payload_id == RawPayload.id).exists()
    )
    return await raw_payload_service.sweep_domain(
        session,
        domain=DOMAIN,
        reparse=reparse_from_raw,
        has_normalized=has_normalized,
        limit=limit,
        since_days=since_days,
    )


# ── Reads ─────────────────────────────────────────────────────────────────────
async def list_workouts(
    session: AsyncSession, *, limit: int = 50
) -> Sequence[HevyWorkout]:
    result = await session.execute(
        select(HevyWorkout)
        .options(selectinload(HevyWorkout.exercises).selectinload(HevyExercise.sets))
        .order_by(HevyWorkout.date.desc(), HevyWorkout.start_time.desc())
        .limit(limit)
    )
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
        for s in exercise.sets:
            if s.set_type not in _WORKING_SET_TYPES:
                continue
            working_sets += 1
            exercise_sets += 1
            if s.weight_kg and s.reps:
                set_volume = s.weight_kg * s.reps
                volume += set_volume
                exercise_volume += set_volume
            if s.weight_kg is not None:
                weights.append(s.weight_kg)
            if s.reps is not None:
                reps.append(s.reps)
            if s.rpe is not None:
                rpes.append(s.rpe)
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
        "exercises": [e.title for e in workout.exercises],
        "exercise_details": exercise_details,
    }


async def workout_count(
    session: AsyncSession, *, since: Optional[date_type] = None
) -> int:
    stmt = select(func.count()).select_from(HevyWorkout)
    if since is not None:
        stmt = stmt.where(HevyWorkout.date >= since)
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def latest_workout_date(session: AsyncSession) -> Optional[date_type]:
    result = await session.execute(select(func.max(HevyWorkout.date)))
    return result.scalar()


async def exercise_catalog(session: AsyncSession) -> list[dict]:
    """Distinct exercises seen across all workouts, with the most recent working
    weight + date — the picklist for the per-exercise history/progression view."""
    result = await session.execute(
        select(
            HevyExercise.exercise_template_id,
            HevyExercise.title,
            func.count(func.distinct(HevyExercise.workout_id)).label("sessions"),
            func.max(HevyWorkout.date).label("last_date"),
        )
        .join(HevyWorkout, HevyExercise.workout_id == HevyWorkout.id)
        .where(HevyExercise.exercise_template_id.is_not(None))
        .group_by(HevyExercise.exercise_template_id, HevyExercise.title)
        .order_by(func.max(HevyWorkout.date).desc())
    )
    return [
        {
            "exercise_template_id": tid,
            "title": title,
            "sessions": int(sessions),
            "last_date": last_date.isoformat() if last_date else None,
        }
        for (tid, title, sessions, last_date) in result.all()
    ]


async def _exercise_sessions(
    session: AsyncSession, exercise_template_id: str
) -> list[tuple[date_type, list[HevySet], Optional[str]]]:
    """Per-session (date, working sets, latest notes) for one exercise, oldest
    first. A session = one workout containing the exercise."""
    result = await session.execute(
        select(HevyWorkout.date, HevyExercise.id, HevyExercise.notes)
        .join(HevyExercise, HevyExercise.workout_id == HevyWorkout.id)
        .where(HevyExercise.exercise_template_id == exercise_template_id)
        .order_by(HevyWorkout.date)
    )
    rows = result.all()
    sessions: list[tuple[date_type, list[HevySet], Optional[str]]] = []
    for on_date, ex_id, notes in rows:
        set_result = await session.execute(
            select(HevySet).where(HevySet.exercise_id == ex_id).order_by(HevySet.set_index)
        )
        sets = [s for s in set_result.scalars().all() if s.set_type in _WORKING_SET_TYPES]
        if sets:
            sessions.append((on_date, sets, notes))
    return sessions


def _top_weight_session(on_date: date_type, sets: list[HevySet]) -> Optional[SessionResult]:
    """Reduce a session's working sets to the engine shape: the heaviest weight
    used and the reps of every set at that weight."""
    weighted = [s for s in sets if s.weight_kg is not None and s.reps is not None]
    if not weighted:
        return None
    top = max(s.weight_kg for s in weighted)
    reps = [s.reps for s in weighted if s.weight_kg == top]
    return SessionResult(on_date=on_date, weight_kg=top, reps=reps)


async def working_weight_series(
    session: AsyncSession, exercise_template_id: str
) -> list[dict]:
    """Top working weight per session over time — the working-weight history chart."""
    sessions = await _exercise_sessions(session, exercise_template_id)
    series: list[dict] = []
    for on_date, sets, _notes in sessions:
        sr = _top_weight_session(on_date, sets)
        if sr is not None:
            series.append(
                {
                    "date": on_date.isoformat(),
                    "weight_kg": sr.weight_kg,
                    "top_reps": max(sr.reps) if sr.reps else None,
                    "sets": len(sr.reps),
                }
            )
    return series


async def progression_for_exercise(
    session: AsyncSession,
    exercise_template_id: str,
    config: Optional[ProgressionConfig] = None,
) -> Optional[ProgressionVerdict]:
    """The progression verdict (🟢/🟡/🔴) for one exercise from its history."""
    sessions = await _exercise_sessions(session, exercise_template_id)
    results = [
        sr
        for (on_date, sets, _notes) in sessions
        if (sr := _top_weight_session(on_date, sets)) is not None
    ]
    return evaluate_progression(results, config or ProgressionConfig())


async def latest_notes(session: AsyncSession, exercise_template_id: str) -> Optional[str]:
    """Most recent technique note recorded for an exercise (from Hevy)."""
    sessions = await _exercise_sessions(session, exercise_template_id)
    for _date, _sets, notes in reversed(sessions):
        if notes:
            return notes
    return None


# ── Scheduler job ─────────────────────────────────────────────────────────────
async def sync_job(
    session_factory,
    redis=None,
    *,
    actor_username: str | None = None,
) -> Optional[dict]:
    """Every-6h Hevy sync (registered in vitals/scheduler/jobs.py). No-ops cleanly
    when Hevy isn't configured so the scheduler never logs spurious failures —
    returns None in that case, else the sync summary (the MCP ``sync_hevy`` tool
    reports it back to the model)."""
    from vitals.integrations.hevy_client import HevyClient

    client = HevyClient.from_config()
    if not client.is_configured:
        return None
    async with session_factory() as session:
        from vitals.services.legacy_ownership import (
            resolve_legacy_ownership_context,
        )

        ownership = await resolve_legacy_ownership_context(
            session,
            actor_username=actor_username,
            required_connections=(IntegrationProvider.HEVY,),
        )
        try:
            summary = await sync_owned(
                session,
                client,
                identity=ownership.write_identity,
                integration_connection_id=ownership.connection_id(
                    IntegrationProvider.HEVY
                ),
            )
        except HevyOwnershipInactiveConnectionError:
            logger.info("Hevy sync skipped: connection is not active")
            await session.rollback()
            return None
        await session.commit()
        if redis is not None:
            import time
            await redis.set("sync:last_success:hevy", str(int(time.time())))
        return summary
