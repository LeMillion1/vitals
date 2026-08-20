"""Schema contract for WeeklyDigest -> platform AI invocation provenance."""
from __future__ import annotations

import importlib
import uuid
from datetime import date

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    DigestKind,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.ai import (
    AIInvocation,
    AIPlatformQuotaPeriod,
    AISubjectQuotaPeriod,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.milestones import WeeklyDigest
from vitals.models.tenancy import IntegrationConnection, PlatformIntegrationConnection

PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)
DAY = date(2026, 8, 20)
DOWNGRADE_REFUSAL = (
    "0041 downgrade refused: weekly_digests.ai_invocation_id contains "
    "AI provenance data"
)


def _invocation(
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    root_id: uuid.UUID,
) -> AIInvocation:
    return AIInvocation(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        platform_integration_connection_id=root_id,
        purpose=AIInvocationPurpose.WEEKLY_DIGEST.value,
        source=AIInvocationSource.WEB.value,
        model="synthetic/digest-model",
        config_version=1,
        idempotency_key=f"opaque-{uuid.uuid4().hex}",
        quota_period_start=PERIOD_START,
        quota_period_end=PERIOD_END,
        reserved_cost_microunits=100,
        reserved_units=500,
        charged_cost_microunits=0,
        charged_units=0,
        status=AIInvocationStatus.PREPARED.value,
    )


def _digest(
    *,
    subject_id: uuid.UUID | None,
    invocation_id: uuid.UUID | None,
    integration_connection_id: uuid.UUID | None = None,
) -> WeeklyDigest:
    return WeeklyDigest(
        subject_id=subject_id,
        actor_user_id=None,
        integration_connection_id=integration_connection_id,
        ai_invocation_id=invocation_id,
        date=DAY,
        domain=Domain.MILESTONES.value,
        source=Source.MANUAL.value,
        kind=DigestKind.WEEKLY.value,
        content="Synthetic narrative",
        context_json={"synthetic": True},
        model="synthetic/digest-model",
    )


async def _model_roots(db_session, legacy_owner_roots):
    if db_session.get_bind().dialect.name == "sqlite":
        await db_session.execute(text("PRAGMA foreign_keys=ON"))

    second_user = User(
        username="digest-schema-second",
        normalized_username="digest-schema-second",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second_user)
    await db_session.flush()
    second_subject = HealthSubject(
        owner_user_id=second_user.id,
        display_name="Synthetic second subject",
        timezone="UTC",
    )
    db_session.add(second_subject)
    await db_session.flush()

    root = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"opaque-{uuid.uuid4().hex}",
        credential_ref="legacy_env:openrouter",
        status=IntegrationConnectionStatus.ACTIVE.value,
        config_version=1,
        configured_by_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(root)
    await db_session.flush()
    db_session.add(
        AIPlatformQuotaPeriod(
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            cost_limit_microunits=10_000,
            unit_limit=10_000,
            configured_by_user_id=legacy_owner_roots.user_id,
        )
    )
    for subject_id in (legacy_owner_roots.subject_id, second_subject.id):
        db_session.add(
            AISubjectQuotaPeriod(
                subject_id=subject_id,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                cost_limit_microunits=10_000,
                unit_limit=10_000,
                configured_by_user_id=legacy_owner_roots.user_id,
            )
        )
    await db_session.flush()
    first = _invocation(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        root_id=root.id,
    )
    second = _invocation(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        root_id=root.id,
    )
    db_session.add_all((first, second))
    await db_session.commit()
    return first, second, second_subject


def test_model_declares_exact_ai_invocation_provenance_contract():
    table = WeeklyDigest.__table__
    invocation_column = table.c.ai_invocation_id
    assert isinstance(invocation_column.type, sa.Uuid)
    assert invocation_column.type.as_uuid is True
    assert invocation_column.nullable is True
    assert {
        foreign_key.target_fullname for foreign_key in invocation_column.foreign_keys
    } == {"ai_invocations.id"}
    assert all(
        foreign_key.ondelete == "RESTRICT"
        for foreign_key in invocation_column.foreign_keys
    )
    assert {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    } >= {"uq_weekly_digests_ai_invocation_id"}
    foreign_key = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_weekly_digests_ai_invocation_subject"
    )
    assert tuple(foreign_key.column_keys) == ("ai_invocation_id", "subject_id")
    assert tuple(element.target_fullname for element in foreign_key.elements) == (
        "ai_invocations.id",
        "ai_invocations.subject_id",
    )
    assert foreign_key.ondelete == "RESTRICT"
    check = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_weekly_digests_ai_invocation_ownership"
    )
    assert "integration_connection_id IS NULL" in str(check.sqltext)
    assert "ix_weekly_digests_ai_invocation_id" in {
        index.name for index in table.indexes
    }


async def test_model_accepts_exact_subject_and_rejects_cross_subject_fk(
    db_session,
    legacy_owner_roots,
):
    first, second, second_subject = await _model_roots(
        db_session, legacy_owner_roots
    )
    valid = _digest(
        subject_id=legacy_owner_roots.subject_id,
        invocation_id=first.id,
    )
    db_session.add(valid)
    await db_session.commit()
    assert valid.ai_invocation_id == first.id

    db_session.add(
        _digest(subject_id=second_subject.id, invocation_id=second.id)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_model_rejects_duplicate_invocation_artifact(
    db_session,
    legacy_owner_roots,
):
    first, _, _ = await _model_roots(db_session, legacy_owner_roots)
    db_session.add(
        _digest(subject_id=legacy_owner_roots.subject_id, invocation_id=first.id)
    )
    await db_session.commit()
    db_session.add(
        _digest(subject_id=legacy_owner_roots.subject_id, invocation_id=first.id)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_model_rejects_platform_and_legacy_provider_provenance_together(
    db_session,
    legacy_owner_roots,
):
    first, _, _ = await _model_roots(db_session, legacy_owner_roots)
    legacy_connection_id = await db_session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == legacy_owner_roots.subject_id
        )
    )
    assert legacy_connection_id is not None
    db_session.add(
        _digest(
            subject_id=legacy_owner_roots.subject_id,
            invocation_id=first.id,
            integration_connection_id=legacy_connection_id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


def _pre_0041_schema(connection) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "ai_invocations",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint(
            "id", "subject_id", name="uq_ai_invocations_id_subject"
        ),
    )
    sa.Table(
        "weekly_digests",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("integration_connection_id", sa.Uuid(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
    )
    metadata.create_all(connection)


def _migration(monkeypatch, connection):
    migration = importlib.import_module(
        "migrations.versions.0041_weekly_digest_ai_invocation"
    )
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _assert_migrated_contract(connection) -> None:
    inspector = inspect(connection)
    assert "ai_invocation_id" in {
        column["name"] for column in inspector.get_columns("weekly_digests")
    }
    assert "uq_weekly_digests_ai_invocation_id" in {
        item["name"] for item in inspector.get_unique_constraints("weekly_digests")
    }
    assert "fk_weekly_digests_ai_invocation_subject" in {
        item["name"] for item in inspector.get_foreign_keys("weekly_digests")
    }
    assert "ck_weekly_digests_ai_invocation_ownership" in {
        item["name"] for item in inspector.get_check_constraints("weekly_digests")
    }
    assert "ix_weekly_digests_ai_invocation_id" in {
        item["name"] for item in inspector.get_indexes("weekly_digests")
    }


def test_sqlite_0041_empty_roundtrip_preserves_legacy_rows(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _pre_0041_schema(connection)
        weekly = sa.Table(
            "weekly_digests", sa.MetaData(), autoload_with=connection
        )
        connection.execute(
            weekly.insert().values(id=1, subject_id=None, content="legacy")
        )
        migration = _migration(monkeypatch, connection)
        migration.upgrade()
        _assert_migrated_contract(connection)
        migration.downgrade()
        inspector = inspect(connection)
        assert "ai_invocation_id" not in {
            column["name"]
            for column in inspector.get_columns("weekly_digests")
        }
        restored = sa.Table(
            "weekly_digests", sa.MetaData(), autoload_with=connection
        )
        assert (
            connection.execute(sa.select(restored.c.content)).scalar_one()
            == "legacy"
        )


def test_sqlite_0041_downgrade_data_guard_is_nondestructive(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _pre_0041_schema(connection)
        migration = _migration(monkeypatch, connection)
        migration.upgrade()
        tables = {
            name: sa.Table(name, sa.MetaData(), autoload_with=connection)
            for name in ("ai_invocations", "weekly_digests")
        }
        # SQLite reflects the generic UUID columns as scalar NUMERIC affinity,
        # so use the same opaque hex representation as the older migration tests.
        subject_id = uuid.uuid4().hex
        invocation_id = uuid.uuid4().hex
        connection.execute(
            tables["ai_invocations"].insert().values(
                id=invocation_id,
                subject_id=subject_id,
            )
        )
        connection.execute(
            tables["weekly_digests"].insert().values(
                id=1,
                subject_id=subject_id,
                ai_invocation_id=invocation_id,
                content="linked",
            )
        )
        with pytest.raises(RuntimeError) as exc_info:
            migration.downgrade()
        assert str(exc_info.value) == DOWNGRADE_REFUSAL
        _assert_migrated_contract(connection)
        assert connection.execute(
            sa.select(tables["weekly_digests"].c.content)
        ).scalar_one() == "linked"


@pytest.mark.integration
async def test_postgres_0041_downgrade_guard_keeps_schema_and_data(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    first, _, _ = await _model_roots(db_session, legacy_owner_roots)
    digest = _digest(
        subject_id=legacy_owner_roots.subject_id,
        invocation_id=first.id,
    )
    db_session.add(digest)
    await db_session.commit()
    connection = await db_session.connection()

    def assert_guard(sync_connection):
        migration = _migration(monkeypatch, sync_connection)
        before = {
            column["name"]
            for column in inspect(sync_connection).get_columns("weekly_digests")
        }
        with pytest.raises(RuntimeError) as exc_info:
            migration.downgrade()
        assert str(exc_info.value) == DOWNGRADE_REFUSAL
        assert {
            column["name"]
            for column in inspect(sync_connection).get_columns("weekly_digests")
        } == before
        weekly = sa.Table(
            "weekly_digests", sa.MetaData(), autoload_with=sync_connection
        )
        assert sync_connection.execute(
            sa.select(weekly.c.ai_invocation_id).where(weekly.c.id == digest.id)
        ).scalar_one() == first.id

    await connection.run_sync(assert_guard)
