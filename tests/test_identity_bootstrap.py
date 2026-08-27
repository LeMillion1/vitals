"""Focused PR-02 tests for legacy bootstrap and identity governance."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import AuditEvent, HealthSubject, User, UserRole
from vitals.persistence.rls import bound_subject, in_platform_scope
from vitals.services.identity.bootstrap import (
    LegacyOwnerConfigurationError,
    LegacyOwnerCredentialMismatchError,
    LegacyOwnerIdentityMismatchError,
    LegacyOwnerStateMismatchError,
    bootstrap_legacy_owner,
)
from vitals.services.identity.contracts import (
    IdentityStateConflictError,
    IdentityValidationError,
    LastActivePlatformSuperadminError,
    PasswordHashDowngradeError,
    PasswordHashMismatchError,
)
from vitals.services.identity.credentials import (
    retire_password_hash,
    rotate_password_hash,
)
from vitals.services.identity.normalization import normalize_username
from vitals.services.identity.queries import has_active_platform_superadmin
from vitals.services.identity.roles import (
    assign_role,
    change_user_status,
    revoke_role,
)
from vitals.utils.passwords import hash_password


def _hash(password: str) -> str:
    return hash_password(password)


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _bootstrap(session: AsyncSession, password_hash: str):
    return await bootstrap_legacy_owner(
        session,
        username="Legacy Owner",
        password_hash=password_hash,
        timezone="Asia/Almaty",
    )


@pytest.mark.parametrize(
    ("raw", "display", "lookup_key"),
    [
        ("  Legacy Owner  ", "Legacy Owner", "legacy owner"),
        ("ＴＥＳＴＥＲ", "TESTER", "tester"),
        ("Straße", "Straße", "strasse"),
    ],
)
def test_normalize_username_uses_nfkc_trim_and_casefold(raw, display, lookup_key):
    normalized = normalize_username(raw)
    assert normalized.display == display
    assert normalized.lookup_key == lookup_key


@pytest.mark.parametrize("raw", ["", "   ", "bad\x00name", "x" * 129])
def test_normalize_username_rejects_unsafe_values(raw):
    with pytest.raises(IdentityValidationError):
        normalize_username(raw)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"username": "   "},
        {"password_hash": ""},
        {"password_hash": "$2b$03$" + "a" * 53},
        {"timezone": "Etc/Definitely-Not-A-Zone"},
        {"timezone": "x" * 65},
    ],
    ids=["blank-username", "blank-hash", "weak-cost", "unknown-zone", "long-zone"],
)
@pytest.mark.asyncio
async def test_bootstrap_rejects_invalid_configuration_before_writes(db_session, kwargs):
    values = {
        "username": "Legacy Owner",
        "password_hash": _hash("legacy-password"),
        "timezone": "Asia/Almaty",
        **kwargs,
    }
    with pytest.raises(LegacyOwnerConfigurationError):
        await bootstrap_legacy_owner(db_session, **values)

    assert await _count(db_session, User) == 0
    assert await _count(db_session, AuditEvent) == 0


@pytest.mark.asyncio
async def test_bootstrap_creates_owner_roles_subject_and_one_audit_event(db_session):
    password_hash = _hash("legacy-password")
    result = await _bootstrap(db_session, password_hash)

    user = await db_session.get(User, result.user_id)
    subject = await db_session.get(HealthSubject, result.subject_id)
    roles = set(
        await db_session.scalars(select(UserRole.role).where(UserRole.user_id == result.user_id))
    )
    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "identity.legacy_owner.bootstrap")
    )

    assert result.user_created is True
    assert result.subject_created is True
    assert result.roles_added == {
        UserRoleName.MEMBER,
        UserRoleName.PLATFORM_SUPERADMIN,
    }
    assert result.changed is True
    assert user is not None
    assert user.username == "Legacy Owner"
    assert user.normalized_username == "legacy owner"
    assert user.password_hash == password_hash
    assert user.email is None and user.normalized_email is None
    assert user.status == UserStatus.ACTIVE.value
    assert user.session_version == 1
    assert subject is not None
    assert subject.owner_user_id == user.id
    assert subject.timezone == "Asia/Almaty"
    assert roles == {"member", "platform_superadmin"}
    assert event is not None
    assert event.actor_user_id is None
    assert event.subject_id == subject.id
    assert event.metadata_json["source_surface"] == "startup"
    assert password_hash not in str(event.metadata_json)
    assert bound_subject(db_session) == subject.id
    assert not in_platform_scope(db_session)


@pytest.mark.asyncio
async def test_bootstrap_does_not_commit(db_session):
    await _bootstrap(db_session, _hash("legacy-password"))
    await db_session.rollback()

    assert await _count(db_session, User) == 0
    assert await _count(db_session, UserRole) == 0
    assert await _count(db_session, HealthSubject) == 0
    assert await _count(db_session, AuditEvent) == 0


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent_and_does_not_audit_a_noop(db_session):
    password_hash = _hash("legacy-password")
    first = await _bootstrap(db_session, password_hash)
    second = await _bootstrap(db_session, password_hash)

    assert first.changed is True
    assert second.changed is False
    assert second.user_id == first.user_id
    assert second.subject_id == first.subject_id
    assert second.roles_added == frozenset()
    assert await _count(db_session, User) == 1
    assert await _count(db_session, UserRole) == 2
    assert await _count(db_session, HealthSubject) == 1
    assert await _count(db_session, AuditEvent) == 1


@pytest.mark.asyncio
async def test_bootstrap_repairs_missing_state_without_overwriting_identity(db_session):
    password_hash = _hash("legacy-password")
    user = User(
        username="LEGACY OWNER",
        normalized_username="legacy owner",
        email="owner@example.test",
        normalized_email="owner@example.test",
        password_hash=password_hash,
        status=UserStatus.ACTIVE.value,
        session_version=7,
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserRole(user_id=user.id, role=UserRoleName.MEMBER.value))
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=None,
        timezone="UTC",
    )
    db_session.add(subject)
    await db_session.flush()

    result = await _bootstrap(db_session, password_hash)
    await db_session.refresh(user)
    await db_session.refresh(subject)

    assert result.user_created is False
    assert result.subject_created is False
    assert result.roles_added == {UserRoleName.PLATFORM_SUPERADMIN}
    assert result.timezone_updated is True
    assert result.display_name_repaired is True
    assert user.username == "LEGACY OWNER"
    assert user.email == "owner@example.test"
    assert user.password_hash == password_hash
    assert user.session_version == 7
    assert subject.timezone == "Asia/Almaty"
    assert subject.display_name == "LEGACY OWNER"


@pytest.mark.asyncio
async def test_bootstrap_rejects_nonempty_database_without_owner_match(db_session):
    other_hash = _hash("other-password")
    db_session.add(
        User(
            username="Other",
            normalized_username="other",
            password_hash=other_hash,
            status=UserStatus.ACTIVE.value,
        )
    )
    await db_session.flush()

    with pytest.raises(LegacyOwnerIdentityMismatchError):
        await _bootstrap(db_session, _hash("legacy-password"))

    assert await _count(db_session, User) == 1
    assert await _count(db_session, UserRole) == 0


@pytest.mark.asyncio
async def test_bootstrap_rejects_persisted_hash_mismatch_without_repair(db_session):
    stored_hash = _hash("stored-password")
    user = User(
        username="Legacy Owner",
        normalized_username="legacy owner",
        password_hash=stored_hash,
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()

    with pytest.raises(LegacyOwnerCredentialMismatchError):
        await _bootstrap(db_session, _hash("different-password"))

    assert user.password_hash == stored_hash
    assert await _count(db_session, UserRole) == 0
    assert await _count(db_session, HealthSubject) == 0


@pytest.mark.parametrize("status", [UserStatus.PENDING, UserStatus.SUSPENDED])
@pytest.mark.asyncio
async def test_bootstrap_never_reactivates_existing_user(db_session, status):
    password_hash = _hash("legacy-password")
    user = User(
        username="Legacy Owner",
        normalized_username="legacy owner",
        password_hash=password_hash,
        status=status.value,
    )
    db_session.add(user)
    await db_session.flush()

    with pytest.raises(LegacyOwnerStateMismatchError):
        await _bootstrap(db_session, password_hash)

    assert user.status == status.value
    assert await _count(db_session, UserRole) == 0


@pytest.mark.asyncio
async def test_last_active_superadmin_role_and_status_are_guarded(db_session):
    result = await _bootstrap(db_session, _hash("legacy-password"))

    with pytest.raises(LastActivePlatformSuperadminError):
        await revoke_role(
            db_session,
            user_id=result.user_id,
            role=UserRoleName.PLATFORM_SUPERADMIN,
            actor_user_id=result.user_id,
        )
    with pytest.raises(LastActivePlatformSuperadminError):
        await change_user_status(
            db_session,
            user_id=result.user_id,
            new_status=UserStatus.SUSPENDED,
            actor_user_id=result.user_id,
        )

    user = await db_session.get(User, result.user_id)
    assert user is not None
    assert user.status == UserStatus.ACTIVE.value
    assert user.session_version == 1
    assert await has_active_platform_superadmin(db_session) is True


@pytest.mark.asyncio
async def test_second_superadmin_allows_first_to_be_deactivated_or_revoked(db_session):
    first = await _bootstrap(db_session, _hash("legacy-password"))
    second = User(
        username="Second Admin",
        normalized_username="second admin",
        password_hash=_hash("second-password"),
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second)
    await db_session.flush()
    await assign_role(
        db_session,
        user_id=second.id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
        assigned_by_user_id=first.user_id,
    )

    changed = await revoke_role(
        db_session,
        user_id=first.user_id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
        actor_user_id=second.id,
    )

    assert changed is True
    assert await has_active_platform_superadmin(db_session) is True
    assert (
        await db_session.scalar(
            select(UserRole).where(
                UserRole.user_id == first.user_id,
                UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_non_superadmin_role_assignment_and_revocation_are_idempotent(db_session):
    owner = await _bootstrap(db_session, _hash("legacy-password"))

    first_assignment = await assign_role(
        db_session,
        user_id=owner.user_id,
        role=UserRoleName.DOCTOR,
        assigned_by_user_id=owner.user_id,
    )
    second_assignment = await assign_role(
        db_session,
        user_id=owner.user_id,
        role=UserRoleName.DOCTOR,
        assigned_by_user_id=owner.user_id,
    )

    assert second_assignment.id == first_assignment.id
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(UserRole)
            .where(
                UserRole.user_id == owner.user_id,
                UserRole.role == UserRoleName.DOCTOR.value,
            )
        )
        == 1
    )
    assert (
        await revoke_role(
            db_session,
            user_id=owner.user_id,
            role=UserRoleName.DOCTOR,
            actor_user_id=owner.user_id,
        )
        is True
    )
    assert (
        await revoke_role(
            db_session,
            user_id=owner.user_id,
            role=UserRoleName.DOCTOR,
            actor_user_id=owner.user_id,
        )
        is False
    )
    assert await has_active_platform_superadmin(db_session) is True


@pytest.mark.asyncio
async def test_password_retirement_recovery_requires_audit_evidence(db_session):
    password_hash = _hash("legacy-password")
    owner = await _bootstrap(db_session, password_hash)
    user = await db_session.get(User, owner.user_id)
    assert user is not None
    user.password_hash = None
    await db_session.flush()

    with pytest.raises(IdentityStateConflictError, match="audit evidence"):
        await retire_password_hash(
            db_session,
            user_id=owner.user_id,
            expected_current_hash=password_hash,
            actor_user_id=owner.user_id,
            allow_already_retired=True,
        )


@pytest.mark.asyncio
async def test_allowed_status_change_increments_session_version(db_session):
    owner = await _bootstrap(db_session, _hash("legacy-password"))
    second = User(
        username="Second Admin",
        normalized_username="second admin",
        password_hash=_hash("second-password"),
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second)
    await db_session.flush()
    await assign_role(
        db_session,
        user_id=second.id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
        assigned_by_user_id=owner.user_id,
    )

    changed = await change_user_status(
        db_session,
        user_id=owner.user_id,
        new_status=UserStatus.SUSPENDED,
        actor_user_id=second.id,
    )

    assert changed.status == UserStatus.SUSPENDED.value
    assert changed.session_version == 2
    assert await has_active_platform_superadmin(db_session) is True


@pytest.mark.asyncio
async def test_password_rotation_is_cas_and_increments_session_version(db_session):
    old_hash = _hash("legacy-password")
    new_hash = _hash("new-password")
    result = await _bootstrap(db_session, old_hash)

    user = await rotate_password_hash(
        db_session,
        user_id=result.user_id,
        expected_current_hash=old_hash,
        new_hash=new_hash,
        actor_user_id=result.user_id,
    )

    assert user.password_hash == new_hash
    assert user.session_version == 2
    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "identity.password.rotated")
    )
    assert event is not None
    assert "password" not in event.metadata_json
    assert old_hash not in str(event.metadata_json)
    assert new_hash not in str(event.metadata_json)

    with pytest.raises(PasswordHashMismatchError):
        await rotate_password_hash(
            db_session,
            user_id=result.user_id,
            expected_current_hash=old_hash,
            new_hash=_hash("another-password"),
            actor_user_id=result.user_id,
        )
    assert user.password_hash == new_hash
    assert user.session_version == 2


@pytest.mark.asyncio
async def test_password_rotation_with_same_hash_is_a_noop(db_session):
    password_hash = _hash("legacy-password")
    owner = await _bootstrap(db_session, password_hash)

    user = await rotate_password_hash(
        db_session,
        user_id=owner.user_id,
        expected_current_hash=password_hash,
        new_hash=password_hash,
        actor_user_id=owner.user_id,
    )

    assert user.password_hash == password_hash
    assert user.session_version == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "identity.password.rotated")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_password_retirement_is_audited_cas_and_idempotent(db_session):
    password_hash = _hash("legacy-password")
    owner = await _bootstrap(db_session, password_hash)

    user = await retire_password_hash(
        db_session,
        user_id=owner.user_id,
        expected_current_hash=password_hash,
        actor_user_id=owner.user_id,
    )

    assert user.password_hash is None
    assert user.session_version == 2
    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "identity.password.retired")
    )
    assert event is not None
    assert password_hash not in str(event.metadata_json)

    with pytest.raises(PasswordHashMismatchError, match="already absent"):
        await retire_password_hash(
            db_session,
            user_id=owner.user_id,
            expected_current_hash=password_hash,
            actor_user_id=owner.user_id,
        )

    repeated = await retire_password_hash(
        db_session,
        user_id=owner.user_id,
        expected_current_hash=password_hash,
        actor_user_id=owner.user_id,
        allow_already_retired=True,
    )
    assert repeated.session_version == 2
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "identity.password.retired")
        )
        == 1
    )


@pytest.mark.asyncio
async def test_password_retirement_refuses_a_stale_hash(db_session):
    password_hash = _hash("legacy-password")
    owner = await _bootstrap(db_session, password_hash)

    with pytest.raises(PasswordHashMismatchError):
        await retire_password_hash(
            db_session,
            user_id=owner.user_id,
            expected_current_hash=_hash("different-password"),
            actor_user_id=owner.user_id,
        )

    user = await db_session.get(User, owner.user_id)
    assert user is not None
    assert user.password_hash == password_hash
    assert user.session_version == 1


@pytest.mark.asyncio
async def test_password_rotation_rejects_bcrypt_cost_downgrade(db_session):
    import bcrypt

    strong_hash = bcrypt.hashpw(b"legacy-password", bcrypt.gensalt(rounds=5)).decode()
    weak_hash = bcrypt.hashpw(b"new-password", bcrypt.gensalt(rounds=4)).decode()
    result = await _bootstrap(db_session, strong_hash)

    with pytest.raises(PasswordHashDowngradeError):
        await rotate_password_hash(
            db_session,
            user_id=result.user_id,
            expected_current_hash=strong_hash,
            new_hash=weak_hash,
            actor_user_id=result.user_id,
        )

    user = await db_session.get(User, result.user_id)
    assert user is not None
    assert user.password_hash == strong_hash
    assert user.session_version == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_concurrent_bootstrap_serializes_empty_table(db_session):
    """The advisory lock closes the empty-table race across real connections."""

    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)
    password_hash = _hash("legacy-password")

    async def run_bootstrap():
        async with factory() as session:
            result = await _bootstrap(session, password_hash)
            await session.commit()
            return result

    first, second = await asyncio.gather(run_bootstrap(), run_bootstrap())
    assert first.user_id == second.user_id
    assert first.subject_id == second.subject_id

    async with factory() as session:
        assert await _count(session, User) == 1
        assert await _count(session, UserRole) == 2
        assert await _count(session, HealthSubject) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_concurrent_admin_revocations_leave_one_active(db_session):
    """Two valid checks cannot concurrently remove the final two admins."""

    first = await _bootstrap(db_session, _hash("legacy-password"))
    second = User(
        username="Second Admin",
        normalized_username="second admin",
        password_hash=_hash("second-password"),
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second)
    await db_session.flush()
    await assign_role(
        db_session,
        user_id=second.id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
        assigned_by_user_id=first.user_id,
    )
    second_id = second.id
    await db_session.commit()

    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)

    async def revoke(user_id):
        async with factory() as session:
            try:
                changed = await revoke_role(
                    session,
                    user_id=user_id,
                    role=UserRoleName.PLATFORM_SUPERADMIN,
                    actor_user_id=None,
                )
                await session.commit()
                return changed
            except LastActivePlatformSuperadminError as exc:
                await session.rollback()
                return exc

    outcomes = await asyncio.gather(revoke(first.user_id), revoke(second_id))
    assert sum(outcome is True for outcome in outcomes) == 1
    assert sum(isinstance(outcome, LastActivePlatformSuperadminError) for outcome in outcomes) == 1

    async with factory() as session:
        assert await has_active_platform_superadmin(session) is True
        active_admin_count = await session.scalar(
            select(func.count())
            .select_from(User)
            .join(UserRole, UserRole.user_id == User.id)
            .where(
                User.status == UserStatus.ACTIVE.value,
                UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
            )
        )
        assert active_admin_count == 1
