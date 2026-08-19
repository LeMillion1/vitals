"""Contract tests for the PR-01 identity and access-control foundation.

These tests intentionally stop at the persistence boundary. Registration,
authentication, and authorization services are introduced by later PRs.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from vitals.enums import (
    AuditOutcome,
    SupportAccessMode,
    SupportAccessStatus,
    SupportScopeResourceType,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import (
    AUDIT_METADATA_ALLOWED_KEYS,
    AuditEvent,
    HealthSubject,
    SupportAccessGrant,
    SupportAccessScope,
    User,
    UserRole,
)


def _user(slug: str, *, normalized_email: str | None = None) -> User:
    email = f"{slug}@example.test" if normalized_email is not None else None
    return User(
        username=slug,
        normalized_username=slug.casefold(),
        email=email,
        normalized_email=normalized_email,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )


async def _persist_user(db_session: Any, slug: str) -> User:
    user = _user(slug)
    db_session.add(user)
    await db_session.flush()
    return user


async def _support_graph(
    db_session: Any,
) -> tuple[User, User, HealthSubject, SupportAccessGrant, datetime]:
    owner = await _persist_user(db_session, "owner")
    support_user = await _persist_user(db_session, "support")
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name="Synthetic subject",
        timezone="Asia/Almaty",
    )
    db_session.add(subject)
    await db_session.flush()

    approved_at = datetime(2026, 8, 19, 10, tzinfo=UTC)
    grant = SupportAccessGrant(
        subject_id=subject.id,
        granted_to_user_id=support_user.id,
        approved_by_user_id=owner.id,
        mode=SupportAccessMode.REPAIR.value,
        status=SupportAccessStatus.ACTIVE.value,
        reason="Investigate synthetic import failure",
        approved_at=approved_at,
        expires_at=approved_at + timedelta(hours=1),
    )
    db_session.add(grant)
    await db_session.flush()
    return owner, support_user, subject, grant, approved_at


def test_0035_migration_upgrade_and_downgrade_matches_models(monkeypatch):
    """Exercise the real revision in isolation, including its reversible DDL.

    The repository's historical migration chain starts with PostgreSQL-only
    JSONB, so SQLite cannot meaningfully replay revision 0001. Revision 0035
    references only tables it creates itself, which lets the fast suite still
    pin this PR's actual upgrade/downgrade and model parity.
    """

    migration = importlib.import_module(
        "migrations.versions.0035_identity_foundation"
    )
    engine = create_engine("sqlite://")
    identity_models = (
        User,
        UserRole,
        HealthSubject,
        SupportAccessGrant,
        SupportAccessScope,
        AuditEvent,
    )

    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            monkeypatch.setattr(migration, "op", Operations(context))

            migration.upgrade()
            inspector = inspect(connection)
            assert set(model.__tablename__ for model in identity_models) <= set(
                inspector.get_table_names()
            )
            for model in identity_models:
                migrated_columns = {
                    column["name"]
                    for column in inspector.get_columns(model.__tablename__)
                }
                assert migrated_columns == set(model.__table__.columns.keys())

            migration.downgrade()
            remaining = set(inspect(connection).get_table_names())
            assert not remaining.intersection(
                model.__tablename__ for model in identity_models
            )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_create_all_registers_identity_tables_and_foreign_keys(db_session):
    connection = await db_session.connection()

    def _schema(sync_connection):
        inspector = inspect(sync_connection)
        tables = set(inspector.get_table_names())
        foreign_keys = {
            (table, tuple(fk["constrained_columns"])): (
                fk["referred_table"],
                (fk.get("options") or {}).get("ondelete"),
            )
            for table in (
                "user_roles",
                "health_subjects",
                "support_access_grants",
                "support_access_scopes",
                "audit_events",
            )
            for fk in inspector.get_foreign_keys(table)
        }
        return tables, foreign_keys

    tables, foreign_keys = await connection.run_sync(_schema)

    assert {
        "users",
        "user_roles",
        "health_subjects",
        "support_access_grants",
        "support_access_scopes",
        "audit_events",
    } <= tables
    assert foreign_keys[("user_roles", ("user_id",))] == ("users", "CASCADE")
    assert foreign_keys[("health_subjects", ("owner_user_id",))] == (
        "users",
        "RESTRICT",
    )
    assert foreign_keys[("support_access_grants", ("subject_id",))] == (
        "health_subjects",
        "RESTRICT",
    )
    assert foreign_keys[("support_access_scopes", ("grant_id",))] == (
        "support_access_grants",
        "CASCADE",
    )
    assert foreign_keys[("audit_events", ("actor_user_id",))] == (
        "users",
        "RESTRICT",
    )
    assert foreign_keys[("audit_events", ("subject_id",))] == (
        "health_subjects",
        "RESTRICT",
    )
    assert foreign_keys[("audit_events", ("support_access_grant_id",))] == (
        "support_access_grants",
        "RESTRICT",
    )


@pytest.mark.asyncio
async def test_roles_are_additive_and_include_doctor_and_trainer(db_session):
    assert UserRoleName.DOCTOR.value == "doctor"
    assert UserRoleName.TRAINER.value == "trainer"

    user = await _persist_user(db_session, "clinician-coach")
    db_session.add_all(
        [
            UserRole(user_id=user.id, role=UserRoleName.DOCTOR.value),
            UserRole(user_id=user.id, role=UserRoleName.TRAINER.value),
        ]
    )
    await db_session.flush()

    roles = set(
        (
            await db_session.scalars(
                select(UserRole.role).where(UserRole.user_id == user.id)
            )
        ).all()
    )
    assert roles == {"doctor", "trainer"}


@pytest.mark.asyncio
async def test_duplicate_normalized_username_is_rejected(db_session):
    first = _user("Alice")
    second = _user("alice")
    second.username = "ALICE"
    db_session.add_all([first, second])

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_duplicate_normalized_email_is_rejected(db_session):
    first = _user("alice", normalized_email="alice@example.test")
    second = _user("alice-2", normalized_email="alice@example.test")
    second.email = "ALICE@example.test"
    db_session.add_all([first, second])

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_duplicate_role_assignment_is_rejected(db_session):
    user = await _persist_user(db_session, "member")
    db_session.add_all(
        [
            UserRole(user_id=user.id, role=UserRoleName.DOCTOR.value),
            UserRole(user_id=user.id, role=UserRoleName.DOCTOR.value),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_user_can_own_only_one_health_subject(db_session):
    owner = await _persist_user(db_session, "single-owner")
    db_session.add_all(
        [
            HealthSubject(owner_user_id=owner.id, timezone="UTC"),
            HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty"),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_support_grant_persists_explicit_scope_expiry_and_revocation(db_session):
    owner, support_user, subject, active_grant, approved_at = await _support_graph(
        db_session
    )
    scope = SupportAccessScope(
        grant_id=active_grant.id,
        resource_type=SupportScopeResourceType.DOMAIN.value,
        resource_key="weight",
        action=SupportAccessMode.READ.value,
    )
    revoked_grant = SupportAccessGrant(
        subject_id=subject.id,
        granted_to_user_id=support_user.id,
        approved_by_user_id=owner.id,
        mode=SupportAccessMode.EXPORT.value,
        status=SupportAccessStatus.REVOKED.value,
        reason="Export synthetic incident evidence",
        approved_at=approved_at,
        expires_at=approved_at + timedelta(hours=2),
        revoked_at=approved_at + timedelta(minutes=5),
        revoked_by_user_id=owner.id,
        revocation_reason="Synthetic investigation completed",
    )
    db_session.add_all([scope, revoked_grant])
    await db_session.flush()

    assert {mode.value for mode in SupportAccessMode} == {"read", "repair", "export"}
    assert active_grant.reason == "Investigate synthetic import failure"
    assert active_grant.expires_at == approved_at + timedelta(hours=1)
    assert scope.resource_type == "domain"
    assert scope.resource_key == "weight"
    assert scope.action == "read"
    assert revoked_grant.status == "revoked"
    assert revoked_grant.revoked_at == approved_at + timedelta(minutes=5)
    assert revoked_grant.revoked_by_user_id == owner.id
    assert revoked_grant.revocation_reason == "Synthetic investigation completed"


@pytest.mark.asyncio
async def test_duplicate_support_scope_is_rejected(db_session):
    _, _, _, grant, _ = await _support_graph(db_session)
    values = {
        "grant_id": grant.id,
        "resource_type": SupportScopeResourceType.DOMAIN.value,
        "resource_key": "labs",
        "action": SupportAccessMode.READ.value,
    }
    db_session.add_all([SupportAccessScope(**values), SupportAccessScope(**values)])

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unknown"},
        {"session_version": 0},
        {"username": "   "},
        {"normalized_username": "   "},
        {"password_hash": "   "},
        {"email": "unpaired@example.test"},
    ],
    ids=[
        "status",
        "session-version",
        "blank-username",
        "blank-normalized-username",
        "blank-password-hash",
        "email-normalized-pair",
    ],
)
@pytest.mark.asyncio
async def test_user_check_constraints_reject_invalid_values(db_session, changes):
    user = _user("invalid-user")
    for field, value in changes.items():
        setattr(user, field, value)
    db_session.add(user)

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_role_check_constraint_rejects_unknown_role(db_session):
    user = await _persist_user(db_session, "unknown-role")
    db_session.add(UserRole(user_id=user.id, role="owner"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "admin"},
        {"status": "unknown"},
        {"reason": "   "},
        {"reason": "x" * 2001},
        {"expires_at": "same-as-approved"},
        {"granted_to_user_id": "same-as-approver"},
        {"status": SupportAccessStatus.REVOKED.value},
    ],
    ids=[
        "mode",
        "status",
        "blank-reason",
        "reason-too-long",
        "non-positive-ttl",
        "self-approval",
        "incomplete-revocation",
    ],
)
@pytest.mark.asyncio
async def test_support_grant_check_constraints_reject_invalid_values(
    db_session, changes
):
    owner, _, _, grant, approved_at = await _support_graph(db_session)
    if changes.get("expires_at") == "same-as-approved":
        changes = {**changes, "expires_at": approved_at}
    if changes.get("granted_to_user_id") == "same-as-approver":
        changes = {**changes, "granted_to_user_id": owner.id}
    for field, value in changes.items():
        setattr(grant, field, value)

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize("invalid_revocation", ["missing-revoker", "reason-too-long"])
@pytest.mark.asyncio
async def test_revocation_requires_an_actor_and_bounded_reason(
    db_session, invalid_revocation
):
    owner, _, _, grant, approved_at = await _support_graph(db_session)
    grant.status = SupportAccessStatus.REVOKED.value
    grant.revoked_at = approved_at + timedelta(minutes=1)
    grant.revoked_by_user_id = owner.id
    grant.revocation_reason = "Synthetic support access revoked"
    if invalid_revocation == "missing-revoker":
        grant.revoked_by_user_id = None
    else:
        grant.revocation_reason = "x" * 2001

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "changes",
    [
        {"resource_type": "table"},
        {"action": "admin"},
        {"resource_key": "   "},
        {"resource_key": "labs.*"},
    ],
    ids=["resource-type", "action", "blank-key", "wildcard-key"],
)
@pytest.mark.asyncio
async def test_support_scope_check_constraints_reject_invalid_values(
    db_session, changes
):
    _, _, _, grant, _ = await _support_graph(db_session)
    values = {
        "grant_id": grant.id,
        "resource_type": SupportScopeResourceType.ARTIFACT.value,
        "resource_key": "synthetic-report",
        "action": SupportAccessMode.READ.value,
        **changes,
    }
    db_session.add(SupportAccessScope(**values))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_audit_event_links_access_context_and_round_trips_safe_metadata(db_session):
    _, support_user, subject, grant, _ = await _support_graph(db_session)
    metadata = {
        "request_id": "req-synthetic-001",
        "source_surface": "test-suite",
        "result_code": "scope_match",
        "resource_type": "domain",
        "resource_id": "opaque-resource-001",
        "changed_fields": ["status"],
        "scope_keys": ["weight"],
        "record_count": 1,
        "grant_mode": "repair",
    }
    event = AuditEvent(
        actor_user_id=support_user.id,
        subject_id=subject.id,
        support_access_grant_id=grant.id,
        event_type="support.scope.checked",
        outcome=AuditOutcome.SUCCESS.value,
        resource_type="domain",
        resource_id="opaque-resource-001",
        metadata_json=metadata,
    )
    db_session.add(event)
    await db_session.flush()

    stored = await db_session.scalar(
        select(AuditEvent)
        .where(AuditEvent.id == event.id)
        .options(
            selectinload(AuditEvent.actor),
            selectinload(AuditEvent.subject),
            selectinload(AuditEvent.support_access_grant),
        )
    )

    assert stored is not None
    assert stored.actor.id == support_user.id
    assert stored.subject.id == subject.id
    assert stored.support_access_grant.id == grant.id
    assert stored.metadata_json == metadata
    assert set(stored.metadata_json) <= AUDIT_METADATA_ALLOWED_KEYS


@pytest.mark.asyncio
async def test_audit_metadata_rejects_non_operational_or_phi_shaped_keys(db_session):
    with pytest.raises(ValueError, match="non-operational keys"):
        AuditEvent(
            event_type="support.scope.checked",
            outcome=AuditOutcome.DENIED.value,
            metadata_json={"diagnosis": "synthetic-sensitive-value"},
        )


@pytest.mark.parametrize("field", ["outcome", "event_type"])
@pytest.mark.asyncio
async def test_audit_event_check_constraints_reject_invalid_values(db_session, field):
    values = {
        "event_type": "identity.login",
        "outcome": AuditOutcome.SUCCESS.value,
        field: "unknown" if field == "outcome" else "   ",
    }
    db_session.add(AuditEvent(**values))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize("mutation", ["update", "delete"])
@pytest.mark.asyncio
async def test_audit_events_are_append_only_through_the_orm(db_session, mutation):
    event = AuditEvent(
        event_type="identity.synthetic",
        outcome=AuditOutcome.SUCCESS.value,
        metadata_json={"result_code": "created"},
    )
    db_session.add(event)
    await db_session.flush()

    if mutation == "update":
        event.outcome = AuditOutcome.FAILED.value
    else:
        await db_session.delete(event)

    with pytest.raises(ValueError, match="append-only"):
        await db_session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_enforces_identity_foreign_keys(db_session):
    """SQLite's suite engine keeps FK enforcement off; production must reject it."""

    db_session.add(
        UserRole(user_id=uuid.uuid4(), role=UserRoleName.MEMBER.value)
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
