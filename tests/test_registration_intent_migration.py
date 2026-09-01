"""Reversible migration and model parity for registration intents."""

from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.sql.sqltypes import String

from vitals.models.registration import RegistrationIntent


def _named_model_constraints(constraint_type: str) -> set[str]:
    return {
        constraint.name
        for constraint in RegistrationIntent.__table__.constraints
        if constraint.__class__.__name__ == constraint_type
        and constraint.name is not None
    }


def test_0085_upgrade_downgrade_and_model_parity(monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.0085_registration_intents"
    )
    assert migration.revision == "0085"
    assert migration.down_revision == "0084"

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(migration, "op", operations)

            migration.upgrade()
            inspector = inspect(connection)
            assert inspector.get_table_names() == ["registration_intents"]

            migrated_columns = {
                column["name"]: column
                for column in inspector.get_columns("registration_intents")
            }
            assert set(migrated_columns) == set(
                RegistrationIntent.__table__.columns.keys()
            )
            for column in RegistrationIntent.__table__.columns:
                migrated = migrated_columns[column.name]
                assert migrated["nullable"] is column.nullable
                if isinstance(column.type, String):
                    assert migrated["type"].length == column.type.length
                assert (migrated["default"] is not None) is (
                    column.server_default is not None
                )

            assert {
                item["name"]
                for item in inspector.get_check_constraints("registration_intents")
            } == _named_model_constraints("CheckConstraint")
            assert {
                (item["name"], item["unique"], tuple(item["column_names"]))
                for item in inspector.get_indexes("registration_intents")
            } == {
                (
                    index.name,
                    index.unique,
                    tuple(expression.name for expression in index.expressions),
                )
                for index in RegistrationIntent.__table__.indexes
            }

            migration.downgrade()
            assert inspect(connection).get_table_names() == []
    finally:
        engine.dispose()
