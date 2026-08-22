"""Owned Hevy ingestion and reparse boundaries."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from vitals.enums import (
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import hevy_service
from vitals.services.legacy_ownership import LegacyActorMismatchError

from tests.conftest import legacy_unenforced_write


# The adoption tests here seed a legacy row with no owner and prove the owned
# ingest takes it over — the one operation whose whole purpose is to turn an
# unstamped row into an owned one. Nothing else can create that state now, so
# this module asks for the schema that stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


_PASSWORD_HASH = "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"


class FakeHevyClient:
    is_configured = True

    def __init__(self, workouts):
        self.workouts = list(workouts)
        self.calls = 0

    async def fetch_workouts(self, *, max_pages: int = 50):
        self.calls += 1
        return list(self.workouts)


def _payload(
    external_id: str,
    *,
    updated: str = "2026-08-19T11:00:00Z",
    title: str = "Owned push",
    reps: int = 8,
) -> dict:
    return {
        "id": external_id,
        "title": title,
        "start_time": "2026-08-19T10:00:00Z",
        "end_time": "2026-08-19T11:00:00Z",
        "updated_at": updated,
        "exercises": [
            {
                "index": 0,
                "title": "Bench Press",
                "exercise_template_id": "BENCH",
                "sets": [
                    {
                        "index": 0,
                        "type": "normal",
                        "weight_kg": 80.0,
                        "reps": reps,
                    }
                ],
            }
        ],
    }


async def _user(session, username: str) -> User:
    row = User(
        username=username,
        normalized_username=username.casefold(),
        password_hash=_PASSWORD_HASH,
        status=UserStatus.ACTIVE.value,
        session_version=1,
    )
    session.add(row)
    await session.flush()
    return row


async def _roots(
    session,
    username: str,
    *,
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.ACTIVE,
):
    owner = await _user(session, username)
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=username,
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.HEVY.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=f"synthetic-{username}",
        credential_ref="test:hevy",
        status=status.value,
        retired_at=(datetime.now(UTC) if status is IntegrationConnectionStatus.RETIRED else None),
    )
    session.add(connection)
    await session.flush()
    return owner, subject, connection


async def _owned_raw(
    session,
    *,
    identity: WriteIdentity,
    connection_id,
    payload: dict,
) -> RawPayload:
    row = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        integration_connection_id=connection_id,
        domain=hevy_service.DOMAIN,
        source=Source.HEVY_API.value,
        external_id=str(payload["id"]),
        payload=payload,
    )
    session.add(row)
    await session.flush()
    return row


async def test_sync_owned_stamps_raw_root_and_entire_child_graph(db_session):
    owner, subject, connection = await _roots(db_session, "owner")
    identity = WriteIdentity(subject.id, owner.id)

    summary = await hevy_service.sync_owned(
        db_session,
        FakeHevyClient([_payload("owned-1")]),
        identity=identity,
        integration_connection_id=connection.id,
    )

    assert summary == {"fetched": 1, "created": 1, "updated": 0, "skipped": 0}
    raw = await db_session.scalar(select(RawPayload))
    workout = await db_session.scalar(select(HevyWorkout))
    exercise = await db_session.scalar(select(HevyExercise))
    hevy_set = await db_session.scalar(select(HevySet))
    assert raw is not None and workout is not None
    assert (raw.subject_id, raw.actor_user_id, raw.integration_connection_id) == (
        subject.id,
        owner.id,
        connection.id,
    )
    assert raw.processed_at is not None
    assert (
        workout.subject_id,
        workout.actor_user_id,
        workout.integration_connection_id,
    ) == (subject.id, owner.id, connection.id)
    assert workout.raw_payload_id == raw.id
    assert exercise is not None and (
        exercise.subject_id,
        exercise.integration_connection_id,
    ) == (subject.id, connection.id)
    assert hevy_set is not None and (
        hevy_set.subject_id,
        hevy_set.integration_connection_id,
    ) == (subject.id, connection.id)


async def test_owned_refresh_preserves_historical_actor(db_session):
    owner, subject, connection = await _roots(db_session, "owner")
    original_identity = WriteIdentity(subject.id, owner.id)
    await hevy_service.sync_owned(
        db_session,
        FakeHevyClient([_payload("owned-actor")]),
        identity=original_identity,
        integration_connection_id=connection.id,
    )

    refreshed = _payload(
        "owned-actor",
        updated="2026-08-19T12:00:00Z",
        title="Refreshed by scheduler",
    )
    await hevy_service.sync_owned(
        db_session,
        FakeHevyClient([refreshed]),
        identity=WriteIdentity(subject.id, None),
        integration_connection_id=connection.id,
    )

    raw = await db_session.scalar(select(RawPayload))
    workout = await db_session.scalar(select(HevyWorkout))
    assert raw is not None and raw.actor_user_id == owner.id
    assert workout is not None and workout.actor_user_id == owner.id
    assert workout.title == "Refreshed by scheduler"


async def test_unchanged_exact_root_adopts_nullable_children_without_rebuild(
    db_session,
):
    owner, subject, connection = await _roots(db_session, "owner")
    identity = WriteIdentity(subject.id, owner.id)
    payload = _payload("exact-null-children")
    await hevy_service.sync_owned(
        db_session,
        FakeHevyClient([payload]),
        identity=identity,
        integration_connection_id=connection.id,
    )
    exercise = await db_session.scalar(select(HevyExercise))
    hevy_set = await db_session.scalar(select(HevySet))
    assert exercise is not None and hevy_set is not None
    # Partial parent/child ownership predates the Stage-4 subject-equality
    # constraints, so it has to be written the way history wrote it.
    async with legacy_unenforced_write(db_session):
        exercise.subject_id = None
        hevy_set.integration_connection_id = None
        await db_session.flush()
    exercise_id = exercise.id
    set_id = hevy_set.id

    summary = await hevy_service.sync_owned(
        db_session,
        FakeHevyClient([payload]),
        identity=WriteIdentity(subject.id, None),
        integration_connection_id=connection.id,
    )

    assert summary == {"fetched": 1, "created": 0, "updated": 0, "skipped": 1}
    assert exercise.id == exercise_id and hevy_set.id == set_id
    assert (
        exercise.subject_id,
        exercise.integration_connection_id,
    ) == (subject.id, connection.id)
    assert (
        hevy_set.subject_id,
        hevy_set.integration_connection_id,
    ) == (subject.id, connection.id)


async def test_sync_owned_adopts_only_fully_unowned_legacy_graph_and_keeps_actor(
    db_session,
):
    owner, subject, connection = await _roots(db_session, "owner")
    historical_actor = await _user(db_session, "historical")
    payload = _payload("legacy-owned", title="Legacy")
    raw = RawPayload(
        actor_user_id=historical_actor.id,
        domain=hevy_service.DOMAIN,
        source=Source.HEVY_API.value,
        external_id="legacy-owned",
        payload=payload,
    )
    workout = HevyWorkout(
        actor_user_id=historical_actor.id,
        external_id="legacy-owned",
        raw_payload_id=None,
        date=date(2026, 8, 19),
        domain=hevy_service.DOMAIN,
        source=Source.HEVY_API.value,
        title="Old legacy title",
    )
    db_session.add_all([raw, workout])
    await db_session.flush()
    workout.raw_payload_id = raw.id
    exercise = HevyExercise(
        workout_id=workout.id,
        exercise_index=0,
        title="Old exercise",
    )
    db_session.add(exercise)
    await db_session.flush()
    db_session.add(HevySet(exercise_id=exercise.id, set_index=0, reps=1))
    await db_session.flush()
    workout_id = workout.id
    raw_id = raw.id
    db_session.expunge(exercise)

    summary = await hevy_service.sync_owned(
        db_session,
        FakeHevyClient([payload]),
        identity=WriteIdentity(subject.id, owner.id),
        integration_connection_id=connection.id,
    )

    assert summary["updated"] == 1
    adopted = await db_session.get(HevyWorkout, workout_id)
    adopted_raw = await db_session.get(RawPayload, raw_id)
    assert adopted is not None and adopted_raw is not None
    assert (
        adopted.subject_id,
        adopted.integration_connection_id,
        adopted.actor_user_id,
    ) == (subject.id, connection.id, historical_actor.id)
    assert (
        adopted_raw.subject_id,
        adopted_raw.integration_connection_id,
        adopted_raw.actor_user_id,
    ) == (subject.id, connection.id, historical_actor.id)
    rebuilt_exercise = await db_session.scalar(select(HevyExercise))
    rebuilt_set = await db_session.scalar(select(HevySet))
    assert rebuilt_exercise is not None and (
        rebuilt_exercise.subject_id,
        rebuilt_exercise.integration_connection_id,
    ) == (subject.id, connection.id)
    assert rebuilt_set is not None and (
        rebuilt_set.subject_id,
        rebuilt_set.integration_connection_id,
    ) == (subject.id, connection.id)


async def test_partial_legacy_root_and_children_are_adopted_without_actor_rewrite(
    db_session,
):
    owner, subject, connection = await _roots(db_session, "owner")
    historical_actor = await _user(db_session, "historical")
    workout = HevyWorkout(
        subject_id=subject.id,
        actor_user_id=historical_actor.id,
        integration_connection_id=None,
        external_id="partial",
        date=date(2026, 8, 19),
        domain=hevy_service.DOMAIN,
        source=Source.HEVY_API.value,
        title="Untouched",
    )
    db_session.add(workout)
    await db_session.flush()
    exercise = HevyExercise(
        workout_id=workout.id,
        subject_id=None,
        integration_connection_id=connection.id,
        exercise_index=0,
        title="Legacy child",
    )
    # A partially owned legacy chain predates the Stage-4 subject-equality
    # constraints, so it is written the way history wrote it.
    async with legacy_unenforced_write(db_session):
        db_session.add(exercise)
        await db_session.flush()
        db_session.add(
            HevySet(
                exercise_id=exercise.id,
                subject_id=subject.id,
                integration_connection_id=None,
                set_index=0,
                reps=1,
            )
        )
    db_session.expunge(exercise)

    summary = await hevy_service.sync_owned(
        db_session,
        FakeHevyClient([_payload("partial", title="Adopted")]),
        identity=WriteIdentity(subject.id, owner.id),
        integration_connection_id=connection.id,
    )

    assert summary["updated"] == 1
    assert workout.title == "Adopted"
    assert (
        workout.subject_id,
        workout.actor_user_id,
        workout.integration_connection_id,
    ) == (subject.id, historical_actor.id, connection.id)
    rebuilt_exercise = await db_session.scalar(select(HevyExercise))
    rebuilt_set = await db_session.scalar(select(HevySet))
    assert rebuilt_exercise is not None and (
        rebuilt_exercise.subject_id,
        rebuilt_exercise.integration_connection_id,
    ) == (subject.id, connection.id)
    assert rebuilt_set is not None and (
        rebuilt_set.subject_id,
        rebuilt_set.integration_connection_id,
    ) == (subject.id, connection.id)


async def test_another_accounts_workout_id_is_never_read_or_overwritten(db_session):
    owner_a, subject_a, connection_a = await _roots(db_session, "owner-a")
    owner_b, subject_b, connection_b = await _roots(db_session, "owner-b")
    theirs = HevyWorkout(
        subject_id=subject_a.id,
        actor_user_id=owner_a.id,
        integration_connection_id=connection_a.id,
        external_id="shared-upstream-id",
        date=date(2026, 8, 19),
        domain=hevy_service.DOMAIN,
        source=Source.HEVY_API.value,
        title="Subject A",
    )
    db_session.add(theirs)
    await db_session.flush()

    # A Hevy workout id is unique inside the account it came from, so both
    # accounts keep their own workout under the same upstream id.
    await hevy_service.sync_owned(
        db_session,
        FakeHevyClient([_payload("shared-upstream-id", title="Subject B")]),
        identity=WriteIdentity(subject_b.id, owner_b.id),
        integration_connection_id=connection_b.id,
    )

    assert theirs.subject_id == subject_a.id
    assert theirs.integration_connection_id == connection_a.id
    assert theirs.title == "Subject A"
    rows = list(
        await db_session.scalars(
            select(HevyWorkout).where(
                HevyWorkout.external_id == "shared-upstream-id"
            )
        )
    )
    assert len(rows) == 2
    assert {row.integration_connection_id for row in rows} == {
        connection_a.id,
        connection_b.id,
    }


async def test_rebuild_rejects_foreign_child_scope_before_deleting_it(db_session):
    owner_a, subject_a, connection_a = await _roots(db_session, "owner-a")
    _owner_b, subject_b, connection_b = await _roots(db_session, "owner-b")
    identity = WriteIdentity(subject_a.id, owner_a.id)
    await hevy_service.sync_owned(
        db_session,
        FakeHevyClient([_payload("bad-child")]),
        identity=identity,
        integration_connection_id=connection_a.id,
    )
    exercise = await db_session.scalar(select(HevyExercise))
    assert exercise is not None
    # A cross-subject child is impossible to write after Stage 4, so the
    # reviewed regression reproduces it as pre-constraint history.
    async with legacy_unenforced_write(db_session):
        exercise.subject_id = subject_b.id
        exercise.integration_connection_id = connection_b.id
    exercise_id = exercise.id

    with pytest.raises(hevy_service.HevyOwnershipConflictError, match="exercise"):
        async with db_session.begin_nested():
            await hevy_service.sync_owned(
                db_session,
                FakeHevyClient(
                    [
                        _payload(
                            "bad-child",
                            updated="2026-08-19T12:00:00Z",
                        )
                    ]
                ),
                identity=identity,
                integration_connection_id=connection_a.id,
            )

    assert await db_session.get(HevyExercise, exercise_id) is not None


async def test_owned_reparse_derives_historical_actor_from_raw(db_session):
    owner, subject, connection = await _roots(db_session, "owner")
    raw = await _owned_raw(
        db_session,
        identity=WriteIdentity(subject.id, owner.id),
        connection_id=connection.id,
        payload=_payload("reparse-actor"),
    )

    await hevy_service.reparse_owned_from_raw(
        db_session,
        raw,
        identity=WriteIdentity(subject.id, None),
        integration_connection_id=connection.id,
    )

    workout = await db_session.scalar(select(HevyWorkout))
    assert workout is not None
    assert (
        workout.subject_id,
        workout.actor_user_id,
        workout.integration_connection_id,
    ) == (subject.id, owner.id, connection.id)


async def test_owned_reparse_preserves_stage3a_actorless_account_history(db_session):
    _, subject, connection = await _roots(db_session, "stage3a-actorless-hevy")
    raw = await _owned_raw(
        db_session,
        identity=WriteIdentity(subject.id, None),
        connection_id=connection.id,
        payload=_payload("stage3a-actorless-hevy"),
    )

    await hevy_service.reparse_owned_from_raw(
        db_session,
        raw,
        identity=WriteIdentity(subject.id, None),
        integration_connection_id=connection.id,
    )

    workout = await db_session.scalar(select(HevyWorkout))
    assert workout is not None
    assert (
        workout.subject_id,
        workout.actor_user_id,
        workout.integration_connection_id,
        workout.raw_payload_id,
    ) == (subject.id, None, connection.id, raw.id)


async def test_owned_reparse_rejects_raw_subject_or_connection_mismatch(db_session):
    owner_a, subject_a, connection_a = await _roots(db_session, "owner-a")
    owner_b, subject_b, connection_b = await _roots(db_session, "owner-b")
    raw = await _owned_raw(
        db_session,
        identity=WriteIdentity(subject_a.id, owner_a.id),
        connection_id=connection_a.id,
        payload=_payload("foreign-raw"),
    )

    with pytest.raises(hevy_service.HevyRawPayloadInvariantError):
        await hevy_service.reparse_owned_from_raw(
            db_session,
            raw,
            identity=WriteIdentity(subject_b.id, owner_b.id),
            integration_connection_id=connection_b.id,
        )

    assert await db_session.scalar(select(func.count()).select_from(HevyWorkout)) == 0


async def test_owned_reparse_rejects_payload_id_mismatch(db_session):
    owner, subject, connection = await _roots(db_session, "owner")
    raw = await _owned_raw(
        db_session,
        identity=WriteIdentity(subject.id, owner.id),
        connection_id=connection.id,
        payload=_payload("payload-id"),
    )
    raw.external_id = "raw-id"
    await db_session.flush()

    with pytest.raises(hevy_service.HevyRawPayloadInvariantError, match="payload id"):
        await hevy_service.reparse_owned_from_raw(
            db_session,
            raw,
            identity=WriteIdentity(subject.id, None),
            integration_connection_id=connection.id,
        )

    assert await db_session.scalar(select(func.count()).select_from(HevyWorkout)) == 0


async def test_owned_reparse_rejects_account_raw_with_file_provenance(db_session):
    owner, subject, connection = await _roots(db_session, "owner")
    file_asset = FileAsset(
        subject_id=subject.id,
        opaque_key=uuid.uuid4(),
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref="labs/synthetic-hevy.json",
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    db_session.add(file_asset)
    await db_session.flush()
    raw = await _owned_raw(
        db_session,
        identity=WriteIdentity(subject.id, owner.id),
        connection_id=connection.id,
        payload=_payload("file-backed"),
    )
    raw.file_asset_id = file_asset.id
    await db_session.flush()

    with pytest.raises(hevy_service.HevyRawPayloadInvariantError, match="file asset"):
        await hevy_service.reparse_owned_from_raw(
            db_session,
            raw,
            identity=WriteIdentity(subject.id, None),
            integration_connection_id=connection.id,
        )

    assert await db_session.scalar(select(func.count()).select_from(HevyWorkout)) == 0


async def test_owned_reparse_rejects_detached_raw_state(db_session):
    owner, subject, connection = await _roots(db_session, "owner")
    raw = await _owned_raw(
        db_session,
        identity=WriteIdentity(subject.id, owner.id),
        connection_id=connection.id,
        payload=_payload("detached"),
    )
    db_session.expunge(raw)

    with pytest.raises(hevy_service.HevyRawPayloadInvariantError, match="detached"):
        await hevy_service.reparse_owned_from_raw(
            db_session,
            raw,
            identity=WriteIdentity(subject.id, None),
            integration_connection_id=connection.id,
        )


async def test_owned_reparse_never_rewires_a_different_raw_root(db_session):
    owner, subject, connection = await _roots(db_session, "owner")
    identity = WriteIdentity(subject.id, owner.id)
    raw_one = await _owned_raw(
        db_session,
        identity=identity,
        connection_id=connection.id,
        payload=_payload("same-key", title="First raw"),
    )
    raw_two = await _owned_raw(
        db_session,
        identity=identity,
        connection_id=connection.id,
        payload=_payload("same-key", title="Second raw"),
    )
    workout = HevyWorkout(
        subject_id=subject.id,
        actor_user_id=owner.id,
        integration_connection_id=connection.id,
        external_id="same-key",
        raw_payload_id=raw_one.id,
        date=date(2026, 8, 19),
        domain=hevy_service.DOMAIN,
        source=Source.HEVY_API.value,
        title="Original",
    )
    db_session.add(workout)
    await db_session.flush()

    with pytest.raises(hevy_service.HevyRawPayloadInvariantError, match="different raw"):
        await hevy_service.reparse_owned_from_raw(
            db_session,
            raw_two,
            identity=identity,
            integration_connection_id=connection.id,
        )

    assert workout.raw_payload_id == raw_one.id
    assert workout.title == "Original"


async def test_reparse_owned_pending_is_exactly_subject_connection_scoped(db_session):
    owner_a, subject_a, connection_a = await _roots(db_session, "owner-a")
    owner_b, subject_b, connection_b = await _roots(db_session, "owner-b")
    raw_a = await _owned_raw(
        db_session,
        identity=WriteIdentity(subject_a.id, owner_a.id),
        connection_id=connection_a.id,
        payload=_payload("pending-a"),
    )
    raw_b = await _owned_raw(
        db_session,
        identity=WriteIdentity(subject_b.id, owner_b.id),
        connection_id=connection_b.id,
        payload=_payload("pending-b"),
    )

    done = await hevy_service.reparse_owned_pending(
        db_session,
        identity=WriteIdentity(subject_a.id, None),
        integration_connection_id=connection_a.id,
    )

    assert done == 1
    assert raw_a.processed_at is not None
    assert raw_b.processed_at is None
    workouts = list(await db_session.scalars(select(HevyWorkout)))
    assert [(row.subject_id, row.external_id) for row in workouts] == [
        (subject_a.id, "pending-a")
    ]


@pytest.mark.parametrize(
    "status",
    [IntegrationConnectionStatus.DISABLED, IntegrationConnectionStatus.RETIRED],
)
async def test_inactive_connection_rejects_sync_but_allows_historical_reparse(
    db_session,
    status,
):
    owner, subject, connection = await _roots(
        db_session,
        "owner",
        status=status,
    )
    identity = WriteIdentity(subject.id, owner.id)
    client = FakeHevyClient([_payload("retired")])

    with pytest.raises(
        hevy_service.HevyOwnershipReferenceError,
        match=status.value,
    ):
        await hevy_service.sync_owned(
            db_session,
            client,
            identity=identity,
            integration_connection_id=connection.id,
        )
    assert client.calls == 0

    raw = await _owned_raw(
        db_session,
        identity=identity,
        connection_id=connection.id,
        payload=_payload("retired"),
    )
    await hevy_service.reparse_owned_from_raw(
        db_session,
        raw,
        identity=WriteIdentity(subject.id, None),
        integration_connection_id=connection.id,
    )
    assert await db_session.scalar(select(func.count()).select_from(HevyWorkout)) == 1


@pytest.mark.parametrize(
    "status",
    [IntegrationConnectionStatus.PENDING, IntegrationConnectionStatus.DISABLED],
)
async def test_fresh_sync_rejects_inactive_connection_before_fetch(
    db_session,
    status,
):
    owner, subject, connection = await _roots(db_session, "owner", status=status)
    client = FakeHevyClient([_payload("must-not-fetch")])

    with pytest.raises(
        hevy_service.HevyOwnershipInactiveConnectionError,
        match=status.value,
    ):
        await hevy_service.sync_owned(
            db_session,
            client,
            identity=WriteIdentity(subject.id, owner.id),
            integration_connection_id=connection.id,
        )

    assert client.calls == 0


async def test_sync_job_resolves_system_and_named_owner_actor(
    db_session,
    session_factory,
    monkeypatch,
):
    owner, subject, connection = await _roots(db_session, "owner")
    client = FakeHevyClient([_payload("job-system")])
    monkeypatch.setattr(
        "vitals.integrations.hevy_client.HevyClient.from_config",
        lambda: client,
    )

    await hevy_service.sync_job(session_factory)
    system_workout = await db_session.scalar(
        select(HevyWorkout).where(HevyWorkout.external_id == "job-system")
    )
    assert system_workout is not None
    assert (
        system_workout.subject_id,
        system_workout.actor_user_id,
        system_workout.integration_connection_id,
    ) == (subject.id, None, connection.id)

    client.workouts = [_payload("job-owner")]
    await hevy_service.sync_job(session_factory, actor_username="  OWNER  ")
    owner_workout = await db_session.scalar(
        select(HevyWorkout).where(HevyWorkout.external_id == "job-owner")
    )
    assert owner_workout is not None and owner_workout.actor_user_id == owner.id


async def test_sync_job_actor_mismatch_fails_before_fetch(
    db_session,
    session_factory,
    monkeypatch,
):
    await _roots(db_session, "owner")
    client = FakeHevyClient([_payload("must-not-fetch")])
    monkeypatch.setattr(
        "vitals.integrations.hevy_client.HevyClient.from_config",
        lambda: client,
    )

    with pytest.raises(LegacyActorMismatchError):
        await hevy_service.sync_job(
            session_factory,
            actor_username="different-user",
        )

    assert client.calls == 0


async def test_sync_job_noops_for_disabled_connection(
    db_session,
    session_factory,
    monkeypatch,
):
    await _roots(
        db_session,
        "owner",
        status=IntegrationConnectionStatus.DISABLED,
    )
    client = FakeHevyClient([_payload("must-not-fetch")])
    monkeypatch.setattr(
        "vitals.integrations.hevy_client.HevyClient.from_config",
        lambda: client,
    )

    result = await hevy_service.sync_job(session_factory)

    assert result is None
    assert client.calls == 0
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0
