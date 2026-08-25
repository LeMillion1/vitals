"""Persistence boundary for PHI-free portability v2 import receipts."""

from __future__ import annotations

import importlib
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, Table, Uuid, create_engine, inspect
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.sql.sqltypes import String

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.portability import (
    PORTABILITY_IMPORT_MODE_REPLACE,
    PortabilityImportReceipt,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


async def _roots(db_session, suffix: str) -> tuple[HealthSubject, User]:
    owner = User(
        username=f"portability-owner-{suffix}",
        normalized_username=f"portability-owner-{suffix}",
        password_hash="$synthetic-portability-owner",
        status=UserStatus.ACTIVE.value,
    )
    actor = User(
        username=f"portability-actor-{suffix}",
        normalized_username=f"portability-actor-{suffix}",
        password_hash="$synthetic-portability-actor",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add_all((owner, actor))
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name="Synthetic portability subject",
        timezone="Asia/Almaty",
    )
    db_session.add(subject)
    await db_session.flush()
    return subject, actor


def _values(subject: HealthSubject, actor: User) -> dict:
    return {
        "subject_id": subject.id,
        "actor_user_id": actor.id,
        "operation_id": uuid.uuid4(),
        "archive_id": uuid.uuid4(),
        "manifest_digest": _SHA_A,
        "record_ref": "record_A-19",
        "record_digest": _SHA_B,
        "mapping_digest": _SHA_C,
        "row_count": 12,
        "resource_count": 3,
    }


def test_receipt_schema_has_only_the_reviewed_control_fields():
    assert set(PortabilityImportReceipt.__table__.columns.keys()) == {
        "id",
        "subject_id",
        "actor_user_id",
        "operation_id",
        "archive_id",
        "manifest_digest",
        "record_ref",
        "record_digest",
        "mapping_digest",
        "mode",
        "row_count",
        "resource_count",
        "completed_at",
    }


async def test_receipt_uses_database_defaults_and_actor_need_not_own_subject(
    db_session,
):
    subject, actor = await _roots(db_session, "defaults")
    receipt = PortabilityImportReceipt(**_values(subject, actor))
    db_session.add(receipt)
    await db_session.flush()
    await db_session.refresh(receipt)

    assert actor.id != subject.owner_user_id
    assert receipt.mode == PORTABILITY_IMPORT_MODE_REPLACE
    assert receipt.completed_at is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"manifest_digest": "a" * 63},
        {"manifest_digest": "A" * 64},
        {"manifest_digest": "g" * 64},
        {"record_digest": "A" * 64},
        {"mapping_digest": "g" * 64},
        {"record_ref": ""},
        {"record_ref": "records/subject.json"},
        {"record_ref": "record ref"},
        {"record_ref": "r" * 129},
        {"mode": "merge"},
        {"row_count": -1},
        {"resource_count": -1},
    ],
)
async def test_receipt_rejects_nonopaque_or_malformed_control_values(
    db_session, changes
):
    subject, actor = await _roots(db_session, str(abs(hash(str(changes)))))
    db_session.add(PortabilityImportReceipt(**(_values(subject, actor) | changes)))

    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_subject_operation_is_the_idempotency_key(db_session):
    subject, actor = await _roots(db_session, "idempotency")
    values = _values(subject, actor)
    db_session.add(PortabilityImportReceipt(**values))
    await db_session.flush()

    db_session.add(
        PortabilityImportReceipt(
            **(
                _values(subject, actor)
                | {
                    "operation_id": values["operation_id"],
                    "archive_id": uuid.uuid4(),
                    "record_ref": "different_record",
                }
            )
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


def _named_constraints(model, kind: str) -> set[str]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if constraint.__class__.__name__ == kind and constraint.name is not None
    }


def _assert_migration_model_parity(connection) -> None:
    inspector = inspect(connection)
    table_name = PortabilityImportReceipt.__tablename__
    migrated_columns = {
        column["name"]: column for column in inspector.get_columns(table_name)
    }
    assert set(migrated_columns) == set(
        PortabilityImportReceipt.__table__.columns.keys()
    )

    for column in PortabilityImportReceipt.__table__.columns:
        migrated = migrated_columns[column.name]
        assert migrated["nullable"] is column.nullable
        if isinstance(column.type, String):
            assert migrated["type"].length == column.type.length
        assert (migrated["default"] is not None) is (
            column.server_default is not None
        )

    assert {
        item["name"] for item in inspector.get_check_constraints(table_name)
    } == _named_constraints(PortabilityImportReceipt, "CheckConstraint")
    assert {
        item["name"] for item in inspector.get_unique_constraints(table_name)
    } == _named_constraints(PortabilityImportReceipt, "UniqueConstraint")

    assert {
        (item["name"], item["unique"], tuple(item["column_names"]))
        for item in inspector.get_indexes(table_name)
    } == {
        (
            index.name,
            index.unique,
            tuple(expression.name for expression in index.expressions),
        )
        for index in PortabilityImportReceipt.__table__.indexes
    }
    assert {
        (
            tuple(item["constrained_columns"]),
            item["referred_table"],
            (item.get("options") or {}).get("ondelete"),
        )
        for item in inspector.get_foreign_keys(table_name)
    } == {
        (("subject_id",), "health_subjects", "RESTRICT"),
        (("actor_user_id",), "users", "RESTRICT"),
    }


def test_0078_to_0079_downgrade_and_repeat_upgrade_match_model(monkeypatch):
    """Exercise the new revision across its real predecessor boundary."""

    predecessor = importlib.import_module(
        "migrations.versions.0078_break_glass_sessions"
    )
    migration = importlib.import_module(
        "migrations.versions.0079_portability_import_receipts"
    )
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            roots = MetaData()
            Table(
                "users",
                roots,
                Column("id", Uuid(as_uuid=True), primary_key=True),
            )
            Table(
                "health_subjects",
                roots,
                Column("id", Uuid(as_uuid=True), primary_key=True),
            )
            roots.create_all(connection)
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(predecessor, "op", operations)
            monkeypatch.setattr(migration, "op", operations)

            predecessor.upgrade()
            assert migration.down_revision == predecessor.revision == "0078"
            assert "portability_import_receipts" not in inspect(
                connection
            ).get_table_names()

            migration.upgrade()
            _assert_migration_model_parity(connection)

            migration.downgrade()
            tables = set(inspect(connection).get_table_names())
            assert "portability_import_receipts" not in tables
            assert "break_glass_sessions" in tables

            migration.upgrade()
            _assert_migration_model_parity(connection)

            migration.downgrade()
            predecessor.downgrade()
            assert set(inspect(connection).get_table_names()) == {
                "health_subjects",
                "users",
            }
    finally:
        engine.dispose()


def test_0079_declares_strict_force_rls_contract():
    migration = importlib.import_module(
        "migrations.versions.0079_portability_import_receipts"
    )

    assert migration.SUBJECT_ISOLATED_TABLES == ("portability_import_receipts",)
    assert migration.SUBJECT_SETTING == "vitals.subject_id"
    assert migration.POLICY_NAME == "rls_subject_isolation"
    assert migration._PREDICATE == (
        "subject_id = NULLIF(current_setting('vitals.subject_id', true), '')::uuid"
    )
