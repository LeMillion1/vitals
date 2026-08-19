"""Focused tests for the strict subject-owned raw-payload chokepoint."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from vitals.enums import (
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import raw_payload_service
from vitals.services.raw_payload_service import (
    RawPayloadAmbiguityError,
    RawPayloadConflictError,
    RawPayloadReferenceLifecycleError,
    RawPayloadReferenceNotFoundError,
    RawPayloadReferenceOwnershipError,
    RawPayloadValidationError,
    upsert_owned_raw_payload,
    upsert_raw_payload,
)
from vitals.utils.timeutils import now_local


async def _identity(
    session,
    slug: str,
    *,
    system: bool = False,
) -> tuple[User, HealthSubject, WriteIdentity]:
    owner = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    return (
        owner,
        subject,
        WriteIdentity(
            subject_id=subject.id,
            actor_user_id=None if system else owner.id,
        ),
    )


async def _connection(
    session,
    *,
    subject_id: uuid.UUID,
    discriminator: str,
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.ACTIVE,
) -> IntegrationConnection:
    connection = IntegrationConnection(
        subject_id=subject_id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=discriminator,
        status=status.value,
        retired_at=now_local()
        if status is IntegrationConnectionStatus.RETIRED
        else None,
    )
    session.add(connection)
    await session.flush()
    return connection


async def _file_asset(
    session,
    *,
    subject_id: uuid.UUID,
    slug: str,
    status: FileAssetStatus = FileAssetStatus.LEGACY_PLACEHOLDER,
) -> FileAsset:
    lifecycle_at = now_local()
    asset = FileAsset(
        subject_id=subject_id,
        opaque_key=uuid.uuid4(),
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref=f"labs/{slug}.pdf",
        status=status.value,
        deleted_at=lifecycle_at
        if status in {FileAssetStatus.DELETED, FileAssetStatus.PURGED}
        else None,
        purged_at=lifecycle_at if status is FileAssetStatus.PURGED else None,
    )
    session.add(asset)
    await session.flush()
    return asset


async def _raw_count(session) -> int:
    return int(
        await session.scalar(select(func.count()).select_from(RawPayload)) or 0
    )


@pytest.mark.asyncio
async def test_new_human_and_system_rows_write_exact_ownership(db_session):
    owner, subject, human = await _identity(db_session, "owner")
    system = WriteIdentity(subject_id=subject.id, actor_user_id=None)
    connection = await _connection(
        db_session,
        subject_id=subject.id,
        discriminator="primary-account",
    )
    asset = await _file_asset(
        db_session,
        subject_id=subject.id,
        slug="human-upload",
    )

    human_row = await upsert_owned_raw_payload(
        db_session,
        identity=human,
        integration_connection_id=connection.id,
        file_asset_id=asset.id,
        domain="labs",
        source="upload",
        external_id="document-1",
        payload={"synthetic": "human"},
    )
    system_row = await upsert_owned_raw_payload(
        db_session,
        identity=system,
        domain="garmin",
        source="garmin_api",
        external_id="daily-1",
        payload={"synthetic": "system"},
    )

    assert human_row.id is not None
    assert human_row.subject_id == subject.id
    assert human_row.actor_user_id == owner.id
    assert human_row.integration_connection_id == connection.id
    assert human_row.file_asset_id == asset.id
    assert system_row.subject_id == subject.id
    assert system_row.actor_user_id is None
    assert system_row.integration_connection_id is None
    assert system_row.file_asset_id is None


@pytest.mark.asyncio
async def test_same_business_key_is_isolated_between_subjects(db_session):
    _owner_a, subject_a, identity_a = await _identity(db_session, "owner-a")
    _owner_b, subject_b, identity_b = await _identity(db_session, "owner-b")

    row_a = await upsert_owned_raw_payload(
        db_session,
        identity=identity_a,
        domain="signals",
        source="telegram",
        external_id="message-1",
        payload={"owner": "a"},
    )
    row_b = await upsert_owned_raw_payload(
        db_session,
        identity=identity_b,
        domain="signals",
        source="telegram",
        external_id="message-1",
        payload={"owner": "b"},
    )

    assert row_a.id != row_b.id
    assert {row_a.subject_id, row_b.subject_id} == {subject_a.id, subject_b.id}
    assert await _raw_count(db_session) == 2


@pytest.mark.asyncio
async def test_same_business_key_is_isolated_between_connections(db_session):
    _owner, subject, identity = await _identity(db_session, "owner")
    first_connection = await _connection(
        db_session,
        subject_id=subject.id,
        discriminator="first-account",
    )
    second_connection = await _connection(
        db_session,
        subject_id=subject.id,
        discriminator="second-account",
    )

    first = await upsert_owned_raw_payload(
        db_session,
        identity=identity,
        integration_connection_id=first_connection.id,
        domain="garmin",
        source="garmin_api",
        external_id="activity-1",
        payload={"connection": 1},
    )
    second = await upsert_owned_raw_payload(
        db_session,
        identity=identity,
        integration_connection_id=second_connection.id,
        domain="garmin",
        source="garmin_api",
        external_id="activity-1",
        payload={"connection": 2},
    )

    assert first.id != second.id
    assert first.integration_connection_id == first_connection.id
    assert second.integration_connection_id == second_connection.id
    assert await _raw_count(db_session) == 2


@pytest.mark.asyncio
async def test_refresh_is_idempotent_and_preserves_historical_actor(
    db_session, monkeypatch
):
    owner, subject, identity = await _identity(db_session, "owner")
    row = await upsert_owned_raw_payload(
        db_session,
        identity=identity,
        domain="hevy",
        source="hevy_api",
        external_id="workout-1",
        payload={"revision": 1},
    )
    original_id = row.id
    original_actor = row.actor_user_id
    row.processed_at = now_local() - timedelta(hours=1)
    await db_session.flush()
    refreshed_at = now_local() + timedelta(minutes=1)
    monkeypatch.setattr(raw_payload_service, "now_local", lambda: refreshed_at)

    refreshed = await upsert_owned_raw_payload(
        db_session,
        identity=WriteIdentity(subject_id=subject.id, actor_user_id=None),
        domain="hevy",
        source="hevy_api",
        external_id="workout-1",
        payload={"revision": 2},
    )

    assert refreshed.id == original_id
    assert refreshed.actor_user_id == original_actor == owner.id
    assert refreshed.payload == {"revision": 2}
    assert refreshed.fetched_at == refreshed_at
    assert refreshed.processed_at is None
    assert await _raw_count(db_session) == 1


@pytest.mark.asyncio
async def test_adopts_one_unscoped_legacy_row_without_inventing_actor(db_session):
    owner, subject, identity = await _identity(db_session, "owner", system=True)
    connection = await _connection(
        db_session,
        subject_id=subject.id,
        discriminator="legacy-account",
    )
    asset = await _file_asset(
        db_session,
        subject_id=subject.id,
        slug="legacy-document",
    )
    legacy = RawPayload(
        actor_user_id=owner.id,
        domain="labs",
        source="upload",
        external_id="legacy-1",
        payload={"revision": 1},
        fetched_at=now_local(),
    )
    db_session.add(legacy)
    await db_session.flush()
    legacy_id = legacy.id

    adopted = await upsert_owned_raw_payload(
        db_session,
        identity=identity,
        integration_connection_id=connection.id,
        file_asset_id=asset.id,
        domain="labs",
        source="upload",
        external_id="legacy-1",
        payload={"revision": 2},
    )

    assert adopted.id == legacy_id
    assert adopted.subject_id == subject.id
    assert adopted.actor_user_id == owner.id
    assert adopted.integration_connection_id == connection.id
    assert adopted.file_asset_id == asset.id
    assert adopted.payload == {"revision": 2}


@pytest.mark.asyncio
async def test_adopts_same_subject_connection_null_row_when_adding_connection(
    db_session,
):
    owner, subject, identity = await _identity(db_session, "owner")
    connection = await _connection(
        db_session,
        subject_id=subject.id,
        discriminator="new-account-root",
    )
    existing = RawPayload(
        subject_id=subject.id,
        actor_user_id=owner.id,
        domain="garmin",
        source="garmin_api",
        external_id="activity-1",
        payload={"revision": 1},
        fetched_at=now_local(),
    )
    db_session.add(existing)
    await db_session.flush()

    adopted = await upsert_owned_raw_payload(
        db_session,
        identity=identity,
        integration_connection_id=connection.id,
        domain="garmin",
        source="garmin_api",
        external_id="activity-1",
        payload={"revision": 2},
    )

    assert adopted.id == existing.id
    assert adopted.integration_connection_id == connection.id
    assert adopted.actor_user_id == owner.id
    assert await _raw_count(db_session) == 1


@pytest.mark.asyncio
async def test_ambiguous_legacy_candidates_fail_before_mutation(db_session):
    _owner, _subject, identity = await _identity(db_session, "owner")
    rows = [
        RawPayload(
            domain="garmin",
            source="garmin_api",
            external_id="duplicate-1",
            payload={"candidate": number},
            fetched_at=now_local(),
        )
        for number in (1, 2)
    ]
    db_session.add_all(rows)
    await db_session.flush()

    with pytest.raises(RawPayloadAmbiguityError):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity,
            domain="garmin",
            source="garmin_api",
            external_id="duplicate-1",
            payload={"replacement": True},
        )

    assert [row.subject_id for row in rows] == [None, None]
    assert [row.payload for row in rows] == [
        {"candidate": 1},
        {"candidate": 2},
    ]


@pytest.mark.asyncio
async def test_unscoped_legacy_adoption_requires_pre_registration_single_subject(
    db_session,
):
    _owner_a, _subject_a, identity_a = await _identity(db_session, "owner-a")
    await _identity(db_session, "owner-b")
    legacy = RawPayload(
        domain="garmin",
        source="garmin_api",
        external_id="unscoped-legacy",
        payload={"original": True},
        fetched_at=now_local(),
    )
    db_session.add(legacy)
    await db_session.flush()

    with pytest.raises(RawPayloadConflictError, match="multi-subject"):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity_a,
            domain="garmin",
            source="garmin_api",
            external_id="unscoped-legacy",
            payload={"replacement": True},
        )

    assert legacy.subject_id is None
    assert legacy.payload == {"original": True}


@pytest.mark.asyncio
async def test_duplicate_exact_scope_fails_before_mutation(db_session):
    _owner, subject, identity = await _identity(db_session, "owner")
    rows = [
        RawPayload(
            subject_id=subject.id,
            domain="garmin",
            source="garmin_api",
            external_id="duplicate-1",
            payload={"candidate": number},
            fetched_at=now_local(),
        )
        for number in (1, 2)
    ]
    db_session.add_all(rows)
    await db_session.flush()

    with pytest.raises(RawPayloadAmbiguityError):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity,
            domain="garmin",
            source="garmin_api",
            external_id="duplicate-1",
            payload={"replacement": True},
        )

    assert [row.payload for row in rows] == [
        {"candidate": 1},
        {"candidate": 2},
    ]


@pytest.mark.asyncio
async def test_legacy_connection_and_file_conflicts_do_not_rebind(db_session):
    _owner, subject, identity = await _identity(db_session, "owner")
    old_connection = await _connection(
        db_session,
        subject_id=subject.id,
        discriminator="old-account",
    )
    new_connection = await _connection(
        db_session,
        subject_id=subject.id,
        discriminator="new-account",
    )
    old_file = await _file_asset(
        db_session,
        subject_id=subject.id,
        slug="old-document",
    )
    new_file = await _file_asset(
        db_session,
        subject_id=subject.id,
        slug="new-document",
    )
    legacy = RawPayload(
        integration_connection_id=old_connection.id,
        file_asset_id=old_file.id,
        domain="labs",
        source="upload",
        external_id="legacy-conflict",
        payload={"original": True},
        fetched_at=now_local(),
    )
    db_session.add(legacy)
    await db_session.flush()

    with pytest.raises(RawPayloadConflictError, match="connection"):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity,
            integration_connection_id=new_connection.id,
            file_asset_id=old_file.id,
            domain="labs",
            source="upload",
            external_id="legacy-conflict",
            payload={"replacement": True},
        )

    assert legacy.subject_id is None
    assert legacy.integration_connection_id == old_connection.id
    assert legacy.file_asset_id == old_file.id
    assert legacy.payload == {"original": True}

    with pytest.raises(RawPayloadConflictError, match="file_asset_id"):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity,
            integration_connection_id=old_connection.id,
            file_asset_id=new_file.id,
            domain="labs",
            source="upload",
            external_id="legacy-conflict",
            payload={"replacement": True},
        )
    assert legacy.subject_id is None
    assert legacy.file_asset_id == old_file.id


@pytest.mark.asyncio
async def test_exact_refresh_rejects_non_null_file_rebind_before_mutation(db_session):
    owner, subject, identity = await _identity(db_session, "owner")
    original_file = await _file_asset(
        db_session,
        subject_id=subject.id,
        slug="original-document",
    )
    replacement_file = await _file_asset(
        db_session,
        subject_id=subject.id,
        slug="replacement-document",
    )
    row = await upsert_owned_raw_payload(
        db_session,
        identity=identity,
        file_asset_id=original_file.id,
        domain="labs",
        source="upload",
        external_id="document-1",
        payload={"original": True},
    )

    with pytest.raises(RawPayloadConflictError, match="file_asset_id"):
        await upsert_owned_raw_payload(
            db_session,
            identity=WriteIdentity(subject_id=subject.id, actor_user_id=None),
            file_asset_id=replacement_file.id,
            domain="labs",
            source="upload",
            external_id="document-1",
            payload={"replacement": True},
        )

    assert row.file_asset_id == original_file.id
    assert row.actor_user_id == owner.id
    assert row.payload == {"original": True}


@pytest.mark.asyncio
async def test_missing_and_cross_subject_connection_fail_typed(db_session):
    _owner_a, subject_a, identity_a = await _identity(db_session, "owner-a")
    _owner_b, subject_b, _identity_b = await _identity(db_session, "owner-b")
    foreign = await _connection(
        db_session,
        subject_id=subject_b.id,
        discriminator="foreign-account",
    )

    with pytest.raises(RawPayloadReferenceNotFoundError):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity_a,
            integration_connection_id=uuid.uuid4(),
            domain="garmin",
            source="garmin_api",
            external_id="missing-connection",
            payload={},
        )
    with pytest.raises(RawPayloadReferenceOwnershipError):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity_a,
            integration_connection_id=foreign.id,
            domain="garmin",
            source="garmin_api",
            external_id="foreign-connection",
            payload={},
        )

    assert subject_a.id != subject_b.id
    assert await _raw_count(db_session) == 0


@pytest.mark.asyncio
async def test_retired_connection_rejects_new_and_existing_refresh(db_session):
    _owner, subject, identity = await _identity(db_session, "owner")
    connection = await _connection(
        db_session,
        subject_id=subject.id,
        discriminator="retired-account",
        status=IntegrationConnectionStatus.RETIRED,
    )

    with pytest.raises(RawPayloadReferenceLifecycleError, match="retired"):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity,
            integration_connection_id=connection.id,
            domain="garmin",
            source="garmin_api",
            external_id="new-row",
            payload={"new": True},
        )

    existing = RawPayload(
        subject_id=subject.id,
        integration_connection_id=connection.id,
        domain="garmin",
        source="garmin_api",
        external_id="existing-row",
        payload={"original": True},
        fetched_at=now_local(),
    )
    db_session.add(existing)
    await db_session.flush()
    with pytest.raises(RawPayloadReferenceLifecycleError, match="retired"):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity,
            integration_connection_id=connection.id,
            domain="garmin",
            source="garmin_api",
            external_id="existing-row",
            payload={"replacement": True},
        )
    assert existing.payload == {"original": True}


@pytest.mark.asyncio
async def test_disabled_connection_remains_valid_provenance(db_session):
    _owner, subject, identity = await _identity(db_session, "owner")
    connection = await _connection(
        db_session,
        subject_id=subject.id,
        discriminator="disabled-account",
        status=IntegrationConnectionStatus.DISABLED,
    )

    row = await upsert_owned_raw_payload(
        db_session,
        identity=identity,
        integration_connection_id=connection.id,
        domain="garmin",
        source="garmin_api",
        external_id="historical-import",
        payload={"historical": True},
    )

    assert row.integration_connection_id == connection.id


@pytest.mark.asyncio
async def test_missing_and_cross_subject_file_fail_typed(db_session):
    _owner_a, _subject_a, identity_a = await _identity(db_session, "owner-a")
    _owner_b, subject_b, _identity_b = await _identity(db_session, "owner-b")
    foreign = await _file_asset(
        db_session,
        subject_id=subject_b.id,
        slug="foreign-document",
    )

    with pytest.raises(RawPayloadReferenceNotFoundError):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity_a,
            file_asset_id=uuid.uuid4(),
            domain="labs",
            source="upload",
            external_id="missing-file",
            payload={},
        )
    with pytest.raises(RawPayloadReferenceOwnershipError):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity_a,
            file_asset_id=foreign.id,
            domain="labs",
            source="upload",
            external_id="foreign-file",
            payload={},
        )
    assert await _raw_count(db_session) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [FileAssetStatus.DELETED, FileAssetStatus.PURGED])
async def test_closed_file_rejects_new_and_existing_refresh(db_session, status):
    _owner, subject, identity = await _identity(db_session, "owner")
    asset = await _file_asset(
        db_session,
        subject_id=subject.id,
        slug=f"closed-{status.value}",
        status=status,
    )

    with pytest.raises(RawPayloadReferenceLifecycleError, match=status.value):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity,
            file_asset_id=asset.id,
            domain="labs",
            source="upload",
            external_id="new-row",
            payload={"new": True},
        )

    existing = RawPayload(
        subject_id=subject.id,
        file_asset_id=asset.id,
        domain="labs",
        source="upload",
        external_id="existing-row",
        payload={"original": True},
        fetched_at=now_local(),
    )
    db_session.add(existing)
    await db_session.flush()
    with pytest.raises(RawPayloadReferenceLifecycleError, match=status.value):
        await upsert_owned_raw_payload(
            db_session,
            identity=identity,
            domain="labs",
            source="upload",
            external_id="existing-row",
            payload={"replacement": True},
        )
    assert existing.payload == {"original": True}


@pytest.mark.asyncio
async def test_invalid_typed_inputs_fail_before_database_work(db_session):
    with pytest.raises(RawPayloadValidationError, match="WriteIdentity"):
        await upsert_owned_raw_payload(
            db_session,
            identity=object(),  # type: ignore[arg-type]
            domain="garmin",
            source="garmin_api",
            external_id="invalid-identity",
            payload={},
        )

    _owner, _subject, identity = await _identity(db_session, "owner")
    for field_name, kwargs in (
        ("integration_connection_id", {"integration_connection_id": "bad"}),
        ("file_asset_id", {"file_asset_id": "bad"}),
    ):
        with pytest.raises(RawPayloadValidationError, match=field_name):
            await upsert_owned_raw_payload(
                db_session,
                identity=identity,
                domain="garmin",
                source="garmin_api",
                external_id=f"invalid-{field_name}",
                payload={},
                **kwargs,  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_owned_upsert_flushes_but_rollback_removes_the_row(db_session):
    _owner, subject, identity = await _identity(db_session, "owner")
    subject_id = subject.id
    await db_session.commit()

    row = await upsert_owned_raw_payload(
        db_session,
        identity=identity,
        domain="garmin",
        source="garmin_api",
        external_id="rollback-1",
        payload={"temporary": True},
    )
    assert row.id is not None
    await db_session.rollback()

    assert await db_session.get(HealthSubject, subject_id) is not None
    assert await _raw_count(db_session) == 0


@pytest.mark.asyncio
async def test_legacy_upsert_remains_global_and_ownership_neutral(db_session):
    legacy = RawPayload(
        domain="signals",
        source="telegram",
        external_id="legacy-helper-1",
        payload={"revision": 1},
        fetched_at=now_local(),
        processed_at=now_local(),
    )
    db_session.add(legacy)
    await db_session.flush()

    refreshed = await upsert_raw_payload(
        db_session,
        domain="signals",
        source="telegram",
        external_id="legacy-helper-1",
        payload={"revision": 2},
    )

    assert refreshed.id == legacy.id
    assert refreshed.subject_id is None
    assert refreshed.actor_user_id is None
    assert refreshed.integration_connection_id is None
    assert refreshed.file_asset_id is None
    assert refreshed.payload == {"revision": 2}
    assert refreshed.processed_at is None
