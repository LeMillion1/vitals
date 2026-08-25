"""Migration parity for the registration-admission persistence boundary."""

from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, Table, Uuid, create_engine, inspect
from sqlalchemy.sql.sqltypes import String

from vitals.models.registration import RegistrationInvitation, RegistrationRequest


def _named_model_constraints(model, constraint_type: str) -> set[str]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if constraint.__class__.__name__ == constraint_type
        and constraint.name is not None
    }


def test_0072_to_0074_upgrade_downgrade_and_model_parity(monkeypatch):
    """Exercise the real reversible DDL and pin its complete public shape."""

    foundation = importlib.import_module(
        "migrations.versions.0072_registration_admission_schema"
    )
    idempotency = importlib.import_module(
        "migrations.versions.0073_registration_invitation_idempotency"
    )
    retention_indexes = importlib.import_module(
        "migrations.versions.0074_registration_retention_indexes"
    )
    engine = create_engine("sqlite://")
    models = (RegistrationInvitation, RegistrationRequest)

    try:
        with engine.begin() as connection:
            # Revision 0072 references the pre-existing identity root.  A
            # synthetic root keeps this isolated fast test independent of the
            # PostgreSQL-only historical migration chain.
            Table(
                "users",
                MetaData(),
                Column("id", Uuid(as_uuid=True), primary_key=True),
            ).create(connection)
            context = MigrationContext.configure(connection)
            operations = Operations(context)
            monkeypatch.setattr(foundation, "op", operations)
            monkeypatch.setattr(idempotency, "op", operations)
            monkeypatch.setattr(retention_indexes, "op", operations)

            foundation.upgrade()
            idempotency.upgrade()
            retention_indexes.upgrade()
            inspector = inspect(connection)

            for model in models:
                table_name = model.__tablename__
                migrated_columns = {
                    column["name"]: column
                    for column in inspector.get_columns(table_name)
                }
                assert set(migrated_columns) == set(model.__table__.columns.keys())

                for column in model.__table__.columns:
                    migrated = migrated_columns[column.name]
                    assert migrated["nullable"] is column.nullable
                    if isinstance(column.type, String):
                        assert migrated["type"].length == column.type.length
                    assert (migrated["default"] is not None) is (
                        column.server_default is not None
                    )

                assert {
                    item["name"]
                    for item in inspector.get_check_constraints(table_name)
                } == _named_model_constraints(model, "CheckConstraint")
                assert {
                    item["name"]
                    for item in inspector.get_unique_constraints(table_name)
                } == _named_model_constraints(model, "UniqueConstraint")

                migrated_indexes = {
                    (
                        item["name"],
                        item["unique"],
                        tuple(item["column_names"]),
                    )
                    for item in inspector.get_indexes(table_name)
                }
                model_indexes = {
                    (
                        index.name,
                        index.unique,
                        tuple(expression.name for expression in index.expressions),
                    )
                    for index in model.__table__.indexes
                }
                assert migrated_indexes == model_indexes

                migrated_fks = {
                    (
                        tuple(item["constrained_columns"]),
                        item["referred_table"],
                        (item.get("options") or {}).get("ondelete"),
                    )
                    for item in inspector.get_foreign_keys(table_name)
                }
                model_fks = {
                    (
                        (column.name,),
                        foreign_key.column.table.name,
                        foreign_key.ondelete,
                    )
                    for column in model.__table__.columns
                    for foreign_key in column.foreign_keys
                }
                assert migrated_fks == model_fks

            retention_indexes.downgrade()
            idempotency.downgrade()
            foundation.downgrade()
            assert set(inspect(connection).get_table_names()) == {"users"}
    finally:
        engine.dispose()
