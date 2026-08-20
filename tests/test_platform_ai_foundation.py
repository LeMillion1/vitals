"""Persistence contract for the platform-owned OpenRouter foundation."""
from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.ai import AIInvocation, LegacyOpenRouterConnectionBridge
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import (
    IntegrationConnection,
    PlatformIntegrationConnection,
)


NOW = datetime(2026, 8, 20, 10, tzinfo=UTC)
PLATFORM_TABLES = {
    "platform_integration_connections",
    "ai_invocations",
    "legacy_openrouter_connection_bridges",
}


async def _subject(db_session: Any, slug: str) -> tuple[User, HealthSubject]:
    user = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    db_session.add(subject)
    await db_session.flush()
    return user, subject


def _platform_connection(**changes: Any) -> PlatformIntegrationConnection:
    values: dict[str, Any] = {
        "provider": IntegrationProvider.OPENROUTER.value,
        "connection_type": IntegrationConnectionType.AI_GATEWAY.value,
        "external_account_discriminator": f"opaque-{uuid.uuid4().hex}",
        "credential_ref": "legacy_env:openrouter",
        "status": IntegrationConnectionStatus.ACTIVE.value,
        "config_version": 1,
    }
    values.update(changes)
    return PlatformIntegrationConnection(**values)


def _invocation(
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    connection_id: uuid.UUID,
    **changes: Any,
) -> AIInvocation:
    values: dict[str, Any] = {
        "subject_id": subject_id,
        "actor_user_id": actor_user_id,
        "platform_integration_connection_id": connection_id,
        "purpose": AIInvocationPurpose.WEEKLY_DIGEST.value,
        "source": AIInvocationSource.WEB.value,
        "model": "synthetic/model-v1",
        "config_version": 1,
        "idempotency_key": f"opaque-{uuid.uuid4().hex}",
        "status": AIInvocationStatus.PREPARED.value,
    }
    values.update(changes)
    return AIInvocation(**values)


def test_metadata_has_platform_root_without_subject_or_phi_columns():
    root_columns = set(PlatformIntegrationConnection.__table__.columns.keys())
    invocation_columns = set(AIInvocation.__table__.columns.keys())

    assert "subject_id" not in root_columns
    assert {
        "credential_ref",
        "config_version",
        "configured_by_user_id",
        "retired_at",
    } <= root_columns
    assert {
        "subject_id",
        "actor_user_id",
        "platform_integration_connection_id",
        "purpose",
        "source",
        "model",
        "config_version",
        "idempotency_key",
        "status",
        "upstream_request_id",
        "input_tokens",
        "output_tokens",
        "cost_microunits",
        "started_at",
        "finished_at",
        "error_code",
    } <= invocation_columns
    forbidden = {
        "prompt",
        "response",
        "content",
        "payload",
        "document",
        "health_data",
        "error_detail",
    }
    assert not invocation_columns.intersection(forbidden)


def test_model_constraints_pin_exact_gateway_and_subject_provenance():
    root = PlatformIntegrationConnection.__table__
    invocation = AIInvocation.__table__
    root_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in root.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    invocation_fks = {
        constraint.name: (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in invocation.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }

    assert root_uniques["uq_platform_integration_connections_id_config_version"] == (
        "id",
        "config_version",
    )
    assert invocation_fks["fk_ai_invocations_platform_connection_config"] == (
        ("platform_integration_connection_id", "config_version"),
        (
            "platform_integration_connections.id",
            "platform_integration_connections.config_version",
        ),
        "RESTRICT",
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_ai_invocations_id_subject"
        and tuple(column.name for column in constraint.columns) == ("id", "subject_id")
        for constraint in invocation.constraints
    )
    assert {
        constraint.name
        for constraint in invocation.constraints
        if isinstance(constraint, CheckConstraint)
    } >= {
        "ck_ai_invocations_lifecycle_timestamps",
        "ck_ai_invocations_error_state",
        "ck_ai_invocations_input_tokens_nonnegative",
        "ck_ai_invocations_output_tokens_nonnegative",
        "ck_ai_invocations_cost_microunits_nonnegative",
    }


async def test_platform_root_is_singleton_per_current_provider_type(db_session):
    db_session.add(_platform_connection())
    await db_session.flush()
    db_session.add(_platform_connection())

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_retired_platform_roots_can_coexist_with_one_current_root(db_session):
    db_session.add(
        _platform_connection(
            status=IntegrationConnectionStatus.RETIRED.value,
            retired_at=NOW,
        )
    )
    db_session.add(_platform_connection())
    await db_session.flush()


async def test_platform_root_rotation_is_atomic_retire_then_insert(db_session):
    current = _platform_connection(
        external_account_discriminator="opaque-rotation-v1",
        config_version=7,
    )
    db_session.add(current)
    await db_session.flush()

    current.status = IntegrationConnectionStatus.RETIRED.value
    current.retired_at = NOW
    replacement = _platform_connection(
        external_account_discriminator="opaque-rotation-v2",
        config_version=8,
    )
    db_session.add(replacement)
    await db_session.flush()

    assert current.id != replacement.id
    assert current.status == IntegrationConnectionStatus.RETIRED.value
    assert replacement.status == IntegrationConnectionStatus.ACTIVE.value


@pytest.mark.parametrize(
    ("changes", "constraint_name"),
    [
        ({"provider": IntegrationProvider.GARMIN.value}, "provider_type_pair"),
        ({"connection_type": IntegrationConnectionType.ACCOUNT.value}, "provider_type_pair"),
        ({"credential_ref": "   "}, "credential_ref_not_blank"),
        ({"external_account_discriminator": ""}, "discriminator_not_blank"),
        ({"config_version": 0}, "config_version_positive"),
        (
            {
                "status": IntegrationConnectionStatus.RETIRED.value,
                "retired_at": None,
            },
            "retirement_state",
        ),
    ],
)
async def test_invalid_platform_roots_are_rejected(
    db_session, changes, constraint_name
):
    db_session.add(_platform_connection(**changes))
    with pytest.raises(IntegrityError, match=constraint_name):
        await db_session.flush()


async def test_invocations_are_subject_isolated_and_idempotent(db_session):
    first_user, first_subject = await _subject(db_session, "ai-first")
    second_user, second_subject = await _subject(db_session, "ai-second")
    gateway = _platform_connection()
    db_session.add(gateway)
    await db_session.flush()

    key = "opaque-shared-request"
    db_session.add_all(
        [
            _invocation(
                subject_id=first_subject.id,
                actor_user_id=first_user.id,
                connection_id=gateway.id,
                idempotency_key=key,
            ),
            _invocation(
                subject_id=second_subject.id,
                actor_user_id=second_user.id,
                connection_id=gateway.id,
                idempotency_key=key,
            ),
        ]
    )
    await db_session.flush()


async def test_duplicate_subject_purpose_idempotency_is_rejected(db_session):
    user, subject = await _subject(db_session, "ai-duplicate")
    gateway = _platform_connection()
    db_session.add(gateway)
    await db_session.flush()
    key = "opaque-one-paid-attempt"
    db_session.add_all(
        [
            _invocation(
                subject_id=subject.id,
                actor_user_id=user.id,
                connection_id=gateway.id,
                idempotency_key=key,
            ),
            _invocation(
                subject_id=subject.id,
                actor_user_id=user.id,
                connection_id=gateway.id,
                idempotency_key=key,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "changes",
    [
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"cost_microunits": -1},
        {"status": AIInvocationStatus.DISPATCHING.value, "started_at": None},
        {
            "status": AIInvocationStatus.SUCCEEDED.value,
            "started_at": NOW,
            "finished_at": NOW + timedelta(seconds=1),
            "error_code": AIInvocationErrorCode.TIMEOUT.value,
        },
        {
            "status": AIInvocationStatus.FAILED.value,
            "started_at": NOW,
            "finished_at": NOW + timedelta(seconds=1),
            "error_code": "provider_error_text",
        },
        {
            "status": AIInvocationStatus.FAILED.value,
            "started_at": NOW,
            "finished_at": NOW - timedelta(seconds=1),
            "error_code": AIInvocationErrorCode.TIMEOUT.value,
        },
    ],
)
async def test_invalid_invocation_state_is_rejected(db_session, changes):
    user, subject = await _subject(db_session, f"invalid-{uuid.uuid4().hex}")
    gateway = _platform_connection()
    db_session.add(gateway)
    await db_session.flush()
    db_session.add(
        _invocation(
            subject_id=subject.id,
            actor_user_id=user.id,
            connection_id=gateway.id,
            **changes,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_postgres_rejects_invocation_with_a_different_gateway_config_version(
    db_session,
):
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("composite foreign keys are a PostgreSQL integration gate")
    user, subject = await _subject(db_session, "invalid-config-version")
    gateway = _platform_connection(config_version=3)
    db_session.add(gateway)
    await db_session.flush()
    db_session.add(
        _invocation(
            subject_id=subject.id,
            actor_user_id=user.id,
            connection_id=gateway.id,
            config_version=2,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_bridge_preserves_exact_legacy_and_platform_roots(db_session):
    _user, subject = await _subject(db_session, "ai-bridge")
    legacy = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator="legacy-singleton-v1",
        credential_ref="legacy_env:openrouter",
        status=IntegrationConnectionStatus.LEGACY.value,
    )
    platform = _platform_connection()
    db_session.add_all([legacy, platform])
    await db_session.flush()
    db_session.add(
        LegacyOpenRouterConnectionBridge(
            legacy_integration_connection_id=legacy.id,
            platform_integration_connection_id=platform.id,
        )
    )
    await db_session.flush()


def _upgrade_0039_on_sqlite(connection, monkeypatch):
    identity_migration = importlib.import_module(
        "migrations.versions.0035_identity_foundation"
    )
    tenancy_migration = importlib.import_module(
        "migrations.versions.0036_tenancy_roots_and_scoped_settings"
    )
    migration = importlib.import_module(
        "migrations.versions.0039_platform_ai_gateway_foundation"
    )
    operations = Operations(MigrationContext.configure(connection))
    monkeypatch.setattr(identity_migration, "op", operations)
    monkeypatch.setattr(tenancy_migration, "op", operations)
    monkeypatch.setattr(migration, "op", operations)
    identity_migration.upgrade()
    tenancy_migration.upgrade()
    migration.upgrade()
    return migration


def _insert_0039_downgrade_fixture(connection, populated_table):
    metadata = sa.MetaData()
    tables = {
        table_name: sa.Table(table_name, metadata, autoload_with=connection)
        for table_name in PLATFORM_TABLES
    }
    # Reflected SQLite UUID columns lose SQLAlchemy's UUID bind processor.
    root_id = uuid.uuid4().hex
    connection.execute(
        tables["platform_integration_connections"].insert().values(
            id=root_id,
            provider=IntegrationProvider.OPENROUTER.value,
            connection_type=IntegrationConnectionType.AI_GATEWAY.value,
            external_account_discriminator="opaque-downgrade-root",
            credential_ref="synthetic_ref:downgrade",
            status=IntegrationConnectionStatus.ACTIVE.value,
            config_version=1,
        )
    )
    if populated_table == "platform_integration_connections":
        return tables

    user_id = uuid.uuid4().hex
    subject_id = uuid.uuid4().hex
    users = sa.Table("users", metadata, autoload_with=connection)
    subjects = sa.Table("health_subjects", metadata, autoload_with=connection)
    connection.execute(
        users.insert().values(
            id=user_id,
            username="downgrade-fixture",
            normalized_username="downgrade-fixture",
            password_hash="$synthetic-test-hash",
            status=UserStatus.ACTIVE.value,
        )
    )
    connection.execute(
        subjects.insert().values(
            id=subject_id,
            owner_user_id=user_id,
            display_name="Synthetic downgrade subject",
            timezone="Asia/Almaty",
        )
    )

    if populated_table == "ai_invocations":
        connection.execute(
            tables["ai_invocations"].insert().values(
                id=uuid.uuid4().hex,
                subject_id=subject_id,
                actor_user_id=user_id,
                platform_integration_connection_id=root_id,
                purpose=AIInvocationPurpose.WEEKLY_DIGEST.value,
                source=AIInvocationSource.WEB.value,
                model="synthetic/model-v1",
                config_version=1,
                idempotency_key="opaque-downgrade-invocation",
                status=AIInvocationStatus.PREPARED.value,
            )
        )
        return tables

    legacy_connections = sa.Table(
        "integration_connections", metadata, autoload_with=connection
    )
    legacy_id = uuid.uuid4().hex
    connection.execute(
        legacy_connections.insert().values(
            id=legacy_id,
            subject_id=subject_id,
            provider=IntegrationProvider.OPENROUTER.value,
            connection_type=IntegrationConnectionType.AI_GATEWAY.value,
            external_account_discriminator="legacy-downgrade-root",
            credential_ref="synthetic_ref:legacy-downgrade",
            status=IntegrationConnectionStatus.LEGACY.value,
        )
    )
    connection.execute(
        tables["legacy_openrouter_connection_bridges"].insert().values(
            legacy_integration_connection_id=legacy_id,
            platform_integration_connection_id=root_id,
        )
    )
    return tables


@pytest.mark.parametrize(
    "populated_table",
    [
        "platform_integration_connections",
        "ai_invocations",
        "legacy_openrouter_connection_bridges",
    ],
)
def test_0039_downgrade_refuses_each_populated_table_without_data_loss(
    monkeypatch, populated_table
):
    engine = create_engine("sqlite://")

    try:
        with engine.begin() as connection:
            migration = _upgrade_0039_on_sqlite(connection, monkeypatch)
            tables = _insert_0039_downgrade_fixture(connection, populated_table)
            counts_before = {
                table_name: connection.execute(
                    sa.select(sa.func.count()).select_from(table)
                ).scalar_one()
                for table_name, table in tables.items()
            }

            with pytest.raises(RuntimeError) as exc_info:
                migration.downgrade()

            assert str(exc_info.value) == (
                "0039 downgrade refused: platform AI data exists in "
                f"{populated_table}"
            )
            assert PLATFORM_TABLES <= set(inspect(connection).get_table_names())
            assert {
                table_name: connection.execute(
                    sa.select(sa.func.count()).select_from(table)
                ).scalar_one()
                for table_name, table in tables.items()
            } == counts_before
    finally:
        engine.dispose()


def test_0039_upgrade_downgrade_round_trip_and_model_shape(monkeypatch):
    engine = create_engine("sqlite://")

    try:
        with engine.begin() as connection:
            identity_migration = importlib.import_module(
                "migrations.versions.0035_identity_foundation"
            )
            tenancy_migration = importlib.import_module(
                "migrations.versions.0036_tenancy_roots_and_scoped_settings"
            )
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(identity_migration, "op", operations)
            monkeypatch.setattr(tenancy_migration, "op", operations)
            identity_migration.upgrade()
            tenancy_migration.upgrade()
            tables_before = set(inspect(connection).get_table_names())

            migration = importlib.import_module(
                "migrations.versions.0039_platform_ai_gateway_foundation"
            )
            monkeypatch.setattr(migration, "op", operations)
            migration.upgrade()
            inspector = inspect(connection)
            assert PLATFORM_TABLES <= set(inspector.get_table_names())
            for model in (
                PlatformIntegrationConnection,
                AIInvocation,
                LegacyOpenRouterConnectionBridge,
            ):
                assert {
                    column["name"]
                    for column in inspector.get_columns(model.__tablename__)
                } == set(model.__table__.columns.keys())
                assert {
                    constraint["name"]
                    for constraint in inspector.get_check_constraints(
                        model.__tablename__
                    )
                } == {
                    constraint.name
                    for constraint in model.__table__.constraints
                    if isinstance(constraint, CheckConstraint)
                }
                assert {
                    constraint["name"]: tuple(constraint["column_names"])
                    for constraint in inspector.get_unique_constraints(
                        model.__tablename__
                    )
                } == {
                    constraint.name: tuple(
                        column.name for column in constraint.columns
                    )
                    for constraint in model.__table__.constraints
                    if isinstance(constraint, UniqueConstraint)
                }
                assert {
                    index["name"]: (
                        tuple(index["column_names"]),
                        bool(index["unique"]),
                    )
                    for index in inspector.get_indexes(model.__tablename__)
                } == {
                    index.name: (
                        tuple(column.name for column in index.columns),
                        bool(index.unique),
                    )
                    for index in model.__table__.indexes
                }
                assert {
                    (
                        tuple(foreign_key["constrained_columns"]),
                        tuple(
                            f"{foreign_key['referred_table']}.{column}"
                            for column in foreign_key["referred_columns"]
                        ),
                        (foreign_key.get("options") or {}).get("ondelete"),
                    )
                    for foreign_key in inspector.get_foreign_keys(
                        model.__tablename__
                    )
                } == {
                    (
                        tuple(constraint.column_keys),
                        tuple(
                            element.target_fullname
                            for element in constraint.elements
                        ),
                        constraint.ondelete,
                    )
                    for constraint in model.__table__.constraints
                    if isinstance(constraint, ForeignKeyConstraint)
                }

            migration.downgrade()
            assert set(inspect(connection).get_table_names()) == tables_before
            migration.upgrade()
            assert PLATFORM_TABLES <= set(inspect(connection).get_table_names())
            migration.downgrade()
    finally:
        engine.dispose()
