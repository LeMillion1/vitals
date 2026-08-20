"""0040 quota schema parity and lossless migration guards."""
from __future__ import annotations

import importlib
import uuid
from datetime import date

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy import create_engine, inspect

from vitals.models.ai import (
    AIInvocation,
    AIPlatformQuotaPeriod,
    AISubjectQuotaPeriod,
)

PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)
QUOTA_TABLES = {"ai_platform_quota_periods", "ai_subject_quota_periods"}
NEW_INVOCATION_COLUMNS = {
    "quota_period_start",
    "quota_period_end",
    "reserved_cost_microunits",
    "reserved_units",
    "charged_cost_microunits",
    "charged_units",
}


def _upgrade_to_0039(connection, monkeypatch):
    operations = Operations(MigrationContext.configure(connection))
    for module_name in (
        "migrations.versions.0035_identity_foundation",
        "migrations.versions.0036_tenancy_roots_and_scoped_settings",
        "migrations.versions.0039_platform_ai_gateway_foundation",
    ):
        migration = importlib.import_module(module_name)
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()
    migration_0040 = importlib.import_module(
        "migrations.versions.0040_ai_gateway_quota_accounting"
    )
    monkeypatch.setattr(migration_0040, "op", operations)
    return migration_0040


def _reflected(connection, *table_names):
    metadata = sa.MetaData()
    return {
        name: sa.Table(name, metadata, autoload_with=connection)
        for name in table_names
    }


def _insert_user_subject(connection):
    tables = _reflected(connection, "users", "health_subjects")
    user_id = uuid.uuid4().hex
    subject_id = uuid.uuid4().hex
    connection.execute(
        tables["users"].insert().values(
            id=user_id,
            username="migration-fixture",
            normalized_username="migration-fixture",
            password_hash="$synthetic-test-hash",
            status="active",
        )
    )
    connection.execute(
        tables["health_subjects"].insert().values(
            id=subject_id,
            owner_user_id=user_id,
            display_name="Synthetic migration subject",
            timezone="UTC",
        )
    )
    return user_id, subject_id


def _insert_root(connection, *, configured_by_user_id=None):
    root = _reflected(connection, "platform_integration_connections")[
        "platform_integration_connections"
    ]
    root_id = uuid.uuid4().hex
    values = {
        "id": root_id,
        "provider": "openrouter",
        "connection_type": "ai_gateway",
        "external_account_discriminator": f"opaque-{root_id}",
        "credential_ref": "synthetic_ref:migration",
        "status": "active",
        "config_version": 1,
    }
    if configured_by_user_id is not None:
        values["configured_by_user_id"] = configured_by_user_id
    connection.execute(root.insert().values(**values))
    return root_id


def _insert_quota_rows(connection, *, user_id, subject_id):
    tables = _reflected(
        connection,
        "ai_platform_quota_periods",
        "ai_subject_quota_periods",
    )
    common = {
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "cost_limit_microunits": 1_000,
        "unit_limit": 1_000,
        "configured_by_user_id": user_id,
    }
    connection.execute(
        tables["ai_platform_quota_periods"].insert().values(**common)
    )
    connection.execute(
        tables["ai_subject_quota_periods"].insert().values(
            subject_id=subject_id,
            **common,
        )
    )
    return tables


def _assert_model_parity(
    connection,
    model,
    *,
    excluded_columns=frozenset(),
    excluded_constraints=frozenset(),
    excluded_indexes=frozenset(),
):
    inspector = inspect(connection)
    assert {
        column["name"] for column in inspector.get_columns(model.__tablename__)
    } == set(model.__table__.columns.keys()) - set(excluded_columns)
    assert {
        item["name"]
        for item in inspector.get_check_constraints(model.__tablename__)
    } == {
        constraint.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name not in excluded_constraints
    }
    assert {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_unique_constraints(model.__tablename__)
    } == {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name not in excluded_constraints
    }
    assert {
        item["name"]: (tuple(item["column_names"]), bool(item["unique"]))
        for item in inspector.get_indexes(model.__tablename__)
    } == {
        index.name: (
            tuple(column.name for column in index.columns),
            bool(index.unique),
        )
        for index in model.__table__.indexes
        if index.name not in excluded_indexes
    }
    assert {
        (
            tuple(item["constrained_columns"]),
            tuple(
                f"{item['referred_table']}.{column}"
                for column in item["referred_columns"]
            ),
            (item.get("options") or {}).get("ondelete"),
        )
        for item in inspector.get_foreign_keys(model.__tablename__)
    } == {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in model.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name not in excluded_constraints
    }


def test_0040_empty_round_trip_and_model_parity(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            migration = _upgrade_to_0039(connection, monkeypatch)
            legacy_columns = {
                item["name"]
                for item in inspect(connection).get_columns("ai_invocations")
            }
            migration.upgrade()
            assert QUOTA_TABLES <= set(inspect(connection).get_table_names())
            for model in (AIPlatformQuotaPeriod, AISubjectQuotaPeriod):
                _assert_model_parity(connection, model)
            _assert_model_parity(
                connection,
                AIInvocation,
                excluded_columns={"raw_payload_id"},
                excluded_constraints={
                    "ck_ai_invocations_purpose_raw_payload",
                    "fk_ai_invocations_raw_payload_subject",
                },
                excluded_indexes={
                    "ix_ai_invocations_raw_purpose_created",
                    "uq_ai_invocations_raw_purpose_succeeded",
                },
            )

            migration.downgrade()
            assert QUOTA_TABLES.isdisjoint(inspect(connection).get_table_names())
            assert {
                item["name"]
                for item in inspect(connection).get_columns("ai_invocations")
            } == legacy_columns
            migration.upgrade()
            assert NEW_INVOCATION_COLUMNS <= {
                item["name"]
                for item in inspect(connection).get_columns("ai_invocations")
            }
    finally:
        engine.dispose()


def test_0040_upgrade_refuses_unaccounted_invocation_without_schema_change(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            migration = _upgrade_to_0039(connection, monkeypatch)
            user_id, subject_id = _insert_user_subject(connection)
            root_id = _insert_root(connection, configured_by_user_id=user_id)
            invocation = _reflected(connection, "ai_invocations")["ai_invocations"]
            connection.execute(
                invocation.insert().values(
                    id=uuid.uuid4().hex,
                    subject_id=subject_id,
                    actor_user_id=user_id,
                    platform_integration_connection_id=root_id,
                    purpose="weekly_digest",
                    source="web",
                    model="synthetic/model-v1",
                    config_version=1,
                    idempotency_key="opaque-pre-0040",
                    status="prepared",
                )
            )
            tables_before = set(inspect(connection).get_table_names())
            columns_before = {
                item["name"]
                for item in inspect(connection).get_columns("ai_invocations")
            }

            with pytest.raises(RuntimeError) as exc_info:
                migration.upgrade()

            assert str(exc_info.value) == (
                "0040 upgrade refused: AI accounting data exists in ai_invocations"
            )
            assert set(inspect(connection).get_table_names()) == tables_before
            assert {
                item["name"]
                for item in inspect(connection).get_columns("ai_invocations")
            } == columns_before
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(invocation)
            ) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "populated_table",
    [
        "ai_platform_quota_periods",
        "ai_subject_quota_periods",
        "ai_invocations",
    ],
)
def test_0040_downgrade_refuses_each_populated_table_without_data_loss(
    monkeypatch,
    populated_table,
):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            migration = _upgrade_to_0039(connection, monkeypatch)
            migration.upgrade()
            user_id, subject_id = _insert_user_subject(connection)
            tracked_tables = _reflected(connection, populated_table)
            if populated_table == "ai_platform_quota_periods":
                connection.execute(
                    tracked_tables[populated_table].insert().values(
                        period_start=PERIOD_START,
                        period_end=PERIOD_END,
                        cost_limit_microunits=1_000,
                        unit_limit=1_000,
                        configured_by_user_id=user_id,
                    )
                )
            elif populated_table == "ai_subject_quota_periods":
                connection.execute(
                    tracked_tables[populated_table].insert().values(
                        subject_id=subject_id,
                        period_start=PERIOD_START,
                        period_end=PERIOD_END,
                        cost_limit_microunits=1_000,
                        unit_limit=1_000,
                        configured_by_user_id=user_id,
                    )
                )
            else:
                root_id = _insert_root(connection, configured_by_user_id=user_id)
                quota_tables = _insert_quota_rows(
                    connection,
                    user_id=user_id,
                    subject_id=subject_id,
                )
                tracked_tables.update(quota_tables)
                connection.execute(
                    tracked_tables[populated_table].insert().values(
                        id=uuid.uuid4().hex,
                        subject_id=subject_id,
                        actor_user_id=user_id,
                        platform_integration_connection_id=root_id,
                        purpose="weekly_digest",
                        source="web",
                        model="synthetic/model-v1",
                        config_version=1,
                        idempotency_key="opaque-0040-downgrade",
                        quota_period_start=PERIOD_START,
                        quota_period_end=PERIOD_END,
                        reserved_cost_microunits=100,
                        reserved_units=100,
                        charged_cost_microunits=0,
                        charged_units=0,
                        status="prepared",
                    )
                )
            counts_before = {
                name: connection.scalar(
                    sa.select(sa.func.count()).select_from(table)
                )
                for name, table in tracked_tables.items()
            }

            with pytest.raises(RuntimeError) as exc_info:
                migration.downgrade()

            assert str(exc_info.value) == (
                "0040 downgrade refused: AI accounting data exists in "
                f"{populated_table}"
            )
            assert QUOTA_TABLES <= set(inspect(connection).get_table_names())
            assert NEW_INVOCATION_COLUMNS <= {
                item["name"]
                for item in inspect(connection).get_columns("ai_invocations")
            }
            assert {
                name: connection.scalar(
                    sa.select(sa.func.count()).select_from(table)
                )
                for name, table in tracked_tables.items()
            } == counts_before
    finally:
        engine.dispose()
