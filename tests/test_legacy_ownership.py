"""Focused tests for the read-only legacy ownership resolver."""
from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.identity_service import normalize_username
from vitals.services.legacy_ownership import (
    LegacyConnectionAmbiguousError,
    LegacyConnectionMissingError,
    LegacyConnectionNotResolvedError,
    LegacyConnectionRetiredError,
    LegacyOwnerResolutionError,
    LegacyOwnershipContext,
    LegacyOwnershipValidationError,
    LegacySubjectResolutionError,
    resolve_legacy_ownership_context,
)
from vitals.services.tenancy_bootstrap import LEGACY_CONNECTION_TYPES

_EXPECTED_CONNECTION_TYPES = {
    IntegrationProvider.GARMIN: IntegrationConnectionType.ACCOUNT,
    IntegrationProvider.HEVY: IntegrationConnectionType.ACCOUNT,
    IntegrationProvider.OPENROUTER: IntegrationConnectionType.AI_GATEWAY,
    IntegrationProvider.TELEGRAM: IntegrationConnectionType.RECIPIENT,
}


async def _subject(
    session,
    *,
    username: str = "owner",
    status: UserStatus = UserStatus.ACTIVE,
) -> tuple[User, HealthSubject]:
    normalized = normalize_username(username)
    owner = User(
        username=normalized.display,
        normalized_username=normalized.lookup_key,
        password_hash="$synthetic-test-hash",
        status=status.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    return owner, subject


async def _connection(
    session,
    *,
    subject_id: uuid.UUID,
    provider: IntegrationProvider,
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.ACTIVE,
    discriminator: str = "opaque-account-1",
    connection_type: IntegrationConnectionType | None = None,
) -> IntegrationConnection:
    row = IntegrationConnection(
        subject_id=subject_id,
        provider=provider.value,
        connection_type=(
            connection_type or LEGACY_CONNECTION_TYPES[provider]
        ).value,
        external_account_discriminator=discriminator,
        status=status.value,
        retired_at=datetime.now(UTC)
        if status is IntegrationConnectionStatus.RETIRED
        else None,
    )
    session.add(row)
    await session.flush()
    return row


def test_legacy_connection_type_contract_is_exact_and_immutable():
    assert dict(LEGACY_CONNECTION_TYPES) == _EXPECTED_CONNECTION_TYPES
    with pytest.raises(TypeError):
        LEGACY_CONNECTION_TYPES[IntegrationProvider.GARMIN] = (  # type: ignore[index]
            IntegrationConnectionType.IMPORT
        )


@pytest.mark.asyncio
async def test_system_context_resolves_active_owner_without_roles(db_session):
    owner, subject = await _subject(db_session)

    context = await resolve_legacy_ownership_context(
        db_session,
        actor_username=None,
    )

    assert context.subject_id == subject.id
    assert context.owner_user_id == owner.id
    assert context.actor_user_id is None
    assert dict(context.connection_ids) == {}
    assert context.write_identity == WriteIdentity(
        subject_id=subject.id,
        actor_user_id=None,
    )
    assert context.system_action() == context.write_identity
    assert context.owner_action() == WriteIdentity(
        subject_id=subject.id,
        actor_user_id=owner.id,
    )


@pytest.mark.asyncio
async def test_human_actor_uses_shared_unicode_normalization(db_session):
    owner, subject = await _subject(db_session, username="Ålice")

    context = await resolve_legacy_ownership_context(
        db_session,
        actor_username="  ÅLICE  ",
    )

    assert context.subject_id == subject.id
    assert context.owner_user_id == owner.id
    assert context.actor_user_id == owner.id
    assert context.write_identity == WriteIdentity(
        subject_id=subject.id,
        actor_user_id=owner.id,
    )
    assert context.owner_action() == context.write_identity
    assert context.system_action() == WriteIdentity(
        subject_id=subject.id,
        actor_user_id=None,
    )


@pytest.mark.asyncio
async def test_an_actor_who_owns_nothing_resolves_nothing(db_session):
    """The mismatch is structural now, and that is stronger than it was.

    This used to load the sole subject, then compare its owner to the actor and
    refuse — ``LegacyActorMismatchError``. The owner is named in the query
    instead, so a stranger's request matches no row at all: there is nothing to
    load and nothing to compare, and the refusal cannot be reached by any path
    that forgets the comparison.
    """

    await _subject(db_session, username="owner")

    with pytest.raises(LegacySubjectResolutionError, match="found 0"):
        await resolve_legacy_ownership_context(
            db_session,
            actor_username="someone-else",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_username", ["   ", "owner\x00", 123])
async def test_invalid_actor_username_fails_as_typed_validation(
    db_session, actor_username
):
    await _subject(db_session)

    with pytest.raises(LegacyOwnershipValidationError):
        await resolve_legacy_ownership_context(
            db_session,
            actor_username=actor_username,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_missing_subject_fails_closed(db_session):
    with pytest.raises(LegacySubjectResolutionError, match="found 0"):
        await resolve_legacy_ownership_context(db_session, actor_username=None)


@pytest.mark.asyncio
async def test_multiple_subjects_fail_closed(db_session):
    await _subject(db_session, username="owner-one")
    await _subject(db_session, username="owner-two")

    with pytest.raises(LegacySubjectResolutionError, match="found 2 or more"):
        await resolve_legacy_ownership_context(db_session, actor_username=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [UserStatus.PENDING, UserStatus.SUSPENDED])
async def test_non_active_owner_fails_closed(db_session, status):
    await _subject(db_session, status=status)

    with pytest.raises(LegacyOwnerResolutionError, match="not active"):
        await resolve_legacy_ownership_context(db_session, actor_username=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", list(IntegrationProvider))
async def test_each_requested_provider_resolves_its_frozen_type(db_session, provider):
    _owner, subject = await _subject(db_session)
    connection = await _connection(
        db_session,
        subject_id=subject.id,
        provider=provider,
    )

    context = await resolve_legacy_ownership_context(
        db_session,
        actor_username=None,
        required_connections=(provider,),
    )

    assert context.connection_ids == {provider: connection.id}
    assert context.connection_id(provider) == connection.id


@pytest.mark.asyncio
async def test_missing_requested_connection_fails_closed(db_session):
    await _subject(db_session)

    with pytest.raises(LegacyConnectionMissingError) as error:
        await resolve_legacy_ownership_context(
            db_session,
            actor_username=None,
            required_connections=(IntegrationProvider.GARMIN,),
        )

    assert error.value.provider is IntegrationProvider.GARMIN


@pytest.mark.asyncio
async def test_wrong_connection_type_does_not_satisfy_frozen_pair(db_session):
    _owner, subject = await _subject(db_session)
    await _connection(
        db_session,
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN,
        connection_type=IntegrationConnectionType.IMPORT,
    )

    with pytest.raises(LegacyConnectionMissingError):
        await resolve_legacy_ownership_context(
            db_session,
            actor_username=None,
            required_connections=(IntegrationProvider.GARMIN,),
        )


@pytest.mark.asyncio
async def test_duplicate_non_retired_pair_is_ambiguous(db_session):
    _owner, subject = await _subject(db_session)
    await _connection(
        db_session,
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN,
        discriminator="opaque-account-1",
    )
    await _connection(
        db_session,
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN,
        status=IntegrationConnectionStatus.DISABLED,
        discriminator="opaque-account-2",
    )

    with pytest.raises(LegacyConnectionAmbiguousError) as error:
        await resolve_legacy_ownership_context(
            db_session,
            actor_username=None,
            required_connections=(IntegrationProvider.GARMIN,),
        )

    assert error.value.provider is IntegrationProvider.GARMIN


@pytest.mark.asyncio
async def test_retired_only_pair_fails_but_retired_history_is_ignored(db_session):
    _owner, subject = await _subject(db_session)
    await _connection(
        db_session,
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN,
        status=IntegrationConnectionStatus.RETIRED,
        discriminator="retired-account",
    )

    with pytest.raises(LegacyConnectionRetiredError):
        await resolve_legacy_ownership_context(
            db_session,
            actor_username=None,
            required_connections=(IntegrationProvider.GARMIN,),
        )

    active = await _connection(
        db_session,
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN,
        discriminator="current-account",
    )
    context = await resolve_legacy_ownership_context(
        db_session,
        actor_username=None,
        required_connections=(IntegrationProvider.GARMIN,),
    )
    assert context.connection_id(IntegrationProvider.GARMIN) == active.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        IntegrationConnectionStatus.LEGACY,
        IntegrationConnectionStatus.PENDING,
        IntegrationConnectionStatus.ACTIVE,
        IntegrationConnectionStatus.DISABLED,
    ],
)
async def test_every_non_retired_status_is_valid_provenance(db_session, status):
    _owner, subject = await _subject(db_session)
    connection = await _connection(
        db_session,
        subject_id=subject.id,
        provider=IntegrationProvider.HEVY,
        status=status,
    )

    context = await resolve_legacy_ownership_context(
        db_session,
        actor_username=None,
        required_connections=(IntegrationProvider.HEVY,),
    )

    assert context.connection_id(IntegrationProvider.HEVY) == connection.id


@pytest.mark.asyncio
async def test_unrequested_provider_is_not_validated(db_session):
    _owner, subject = await _subject(db_session)
    garmin = await _connection(
        db_session,
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN,
    )
    await _connection(
        db_session,
        subject_id=subject.id,
        provider=IntegrationProvider.HEVY,
        discriminator="duplicate-one",
    )
    await _connection(
        db_session,
        subject_id=subject.id,
        provider=IntegrationProvider.HEVY,
        discriminator="duplicate-two",
    )

    context = await resolve_legacy_ownership_context(
        db_session,
        actor_username=None,
        required_connections=(IntegrationProvider.GARMIN,),
    )

    assert dict(context.connection_ids) == {IntegrationProvider.GARMIN: garmin.id}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "required_connections",
    [
        None,
        IntegrationProvider.GARMIN,
        ("garmin",),
        (IntegrationProvider.GARMIN, IntegrationProvider.GARMIN),
        42,
    ],
)
async def test_invalid_required_connections_fail_before_lookup(
    db_session, required_connections
):
    with pytest.raises(LegacyOwnershipValidationError):
        await resolve_legacy_ownership_context(
            db_session,
            actor_username=None,
            required_connections=required_connections,  # type: ignore[arg-type]
        )


def test_context_copies_mapping_and_is_deeply_immutable():
    subject_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    supplied = {IntegrationProvider.GARMIN: connection_id}
    context = LegacyOwnershipContext(
        subject_id=subject_id,
        owner_user_id=owner_id,
        actor_user_id=None,
        connection_ids=supplied,
    )

    supplied.clear()
    assert context.connection_id(IntegrationProvider.GARMIN) == connection_id
    with pytest.raises(TypeError):
        context.connection_ids[IntegrationProvider.HEVY] = (  # type: ignore[index]
            uuid.uuid4()
        )
    with pytest.raises(FrozenInstanceError):
        context.subject_id = uuid.uuid4()  # type: ignore[misc]
    with pytest.raises(LegacyConnectionNotResolvedError):
        context.connection_id(IntegrationProvider.HEVY)
    with pytest.raises(LegacyOwnershipValidationError):
        context.connection_id("garmin")  # type: ignore[arg-type]


def test_write_identity_validates_uuids_and_is_immutable():
    identity = WriteIdentity(subject_id=uuid.uuid4(), actor_user_id=None)

    with pytest.raises(FrozenInstanceError):
        identity.actor_user_id = uuid.uuid4()  # type: ignore[misc]
    with pytest.raises(TypeError, match="subject_id"):
        WriteIdentity(  # type: ignore[arg-type]
            subject_id="not-a-uuid",
            actor_user_id=None,
        )
    with pytest.raises(TypeError, match="actor_user_id"):
        WriteIdentity(  # type: ignore[arg-type]
            subject_id=uuid.uuid4(),
            actor_user_id="not-a-uuid",
        )


@pytest.mark.asyncio
async def test_resolution_does_not_autoflush_pending_caller_state(db_session):
    await _subject(db_session)
    pending = User(
        username="pending-unrelated",
        normalized_username="pending-unrelated",
        password_hash="$synthetic-test-hash",
        status=UserStatus.PENDING.value,
    )
    db_session.add(pending)

    await resolve_legacy_ownership_context(db_session, actor_username=None)

    assert pending in db_session.new
    assert pending.id is None
