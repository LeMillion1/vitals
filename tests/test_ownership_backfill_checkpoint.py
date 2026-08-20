"""Stage-3A checkpoint model, registry, and migration contracts."""
from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine, inspect
from sqlalchemy.exc import IntegrityError

from vitals.models import OwnershipBackfillCheckpoint as ExportedCheckpoint
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.ownership import OWNERSHIP_REGISTRY, OwnershipClass, TargetColumn


TABLE_NAME = "ownership_backfill_checkpoints"
INDEX_NAME = "ix_ownership_backfill_checkpoints_status_updated"
DOWNGRADE_REFUSAL = (
    "0045 downgrade refused: ownership backfill checkpoints contain durable state"
)
EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)
NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def _normalized_default(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    if "::" in rendered:
        rendered = rendered.split("::", 1)[0]
    rendered = rendered.strip("'\"").lower()
    if rendered in {"now()", "current_timestamp", "(current_timestamp)"}:
        return "now"
    return rendered


def _type_signature(column_type: Any, dialect: Any) -> tuple[Any, ...]:
    if dialect.name == "sqlite":
        return (str(column_type.compile(dialect=dialect)),)
    if isinstance(column_type, sa.String):
        return ("string", column_type.length)
    if isinstance(column_type, sa.BigInteger):
        return ("bigint",)
    if isinstance(column_type, sa.Uuid):
        return ("uuid",)
    if isinstance(column_type, sa.DateTime):
        return ("datetime", column_type.timezone)
    return (str(column_type.compile(dialect=dialect)),)


def _migration(monkeypatch, connection):
    migration = importlib.import_module(
        "migrations.versions.0045_ownership_backfill_checkpoints"
    )
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_pre_0045_schema(connection) -> sa.Table:
    metadata = sa.MetaData()
    subjects = sa.Table(
        "health_subjects",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    metadata.create_all(connection)
    return subjects


def _migrated_table(connection) -> sa.Table:
    return sa.Table(TABLE_NAME, sa.MetaData(), autoload_with=connection)


def _valid_values(subject_id: uuid.UUID | str, **changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "phase_key": "raw_payloads",
        "subject_id": subject_id,
        "status": "running",
        "scan_high_watermark_id": 10,
        "snapshot_rows": 5,
        "last_scanned_id": 5,
        "scanned_rows": 5,
        "updated_rows": 3,
        "unchanged_rows": 2,
        "data_checksum_before": EMPTY_SHA256,
        "data_checksum_after": EMPTY_SHA256,
        "ownership_checksum_after": EMPTY_SHA256,
    }
    values.update(changes)
    return values


def _assert_migration_matches_model(connection) -> None:
    inspector = inspect(connection)
    model = OwnershipBackfillCheckpoint.__table__
    primary_key_columns = set(
        inspector.get_pk_constraint(TABLE_NAME)["constrained_columns"]
    )
    migrated_columns = {
        item["name"]: (
            _type_signature(item["type"], connection.dialect),
            item["nullable"],
            item["name"] in primary_key_columns,
            _normalized_default(item.get("default")),
        )
        for item in inspector.get_columns(TABLE_NAME)
    }
    expected_columns = {
        column.name: (
            _type_signature(column.type, connection.dialect),
            column.nullable,
            column.primary_key,
            _normalized_default(
                column.server_default.arg
                if column.server_default is not None
                else None
            ),
        )
        for column in model.columns
    }
    assert migrated_columns == expected_columns
    assert {
        item["name"] for item in inspector.get_check_constraints(TABLE_NAME)
    } == {
        constraint.name
        for constraint in model.constraints
        if isinstance(constraint, CheckConstraint)
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
        for item in inspector.get_foreign_keys(TABLE_NAME)
    } == {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in model.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert {
        item["name"]: (tuple(item["column_names"]), bool(item["unique"]))
        for item in inspector.get_indexes(TABLE_NAME)
    } == {
        index.name: (
            tuple(column.name for column in index.columns),
            bool(index.unique),
        )
        for index in model.indexes
    }


def test_model_export_metadata_and_registry_contract_are_exact():
    assert ExportedCheckpoint is OwnershipBackfillCheckpoint
    table = OwnershipBackfillCheckpoint.__table__
    assert tuple(table.columns) == tuple(
        table.c[name]
        for name in (
            "phase_key",
            "subject_id",
            "status",
            "scan_high_watermark_id",
            "snapshot_rows",
            "last_scanned_id",
            "scanned_rows",
            "updated_rows",
            "unchanged_rows",
            "data_checksum_before",
            "data_checksum_after",
            "ownership_checksum_after",
            "started_at",
            "updated_at",
            "completed_at",
        )
    )
    assert tuple(column.name for column in table.primary_key.columns) == (
        "phase_key",
    )
    assert table.c.phase_key.type.length == 64
    assert table.c.phase_key.nullable is False
    assert table.c.subject_id.nullable is False
    assert {
        (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in table.c.subject_id.foreign_keys
    } == {("health_subjects.id", "RESTRICT")}
    for name in (
        "scan_high_watermark_id",
        "snapshot_rows",
        "last_scanned_id",
        "scanned_rows",
        "updated_rows",
        "unchanged_rows",
    ):
        assert isinstance(table.c[name].type, sa.BigInteger)
        assert table.c[name].nullable is False
    for name in (
        "data_checksum_before",
        "data_checksum_after",
        "ownership_checksum_after",
    ):
        assert table.c[name].type.length == 64
        assert table.c[name].nullable is False
    for name in ("started_at", "updated_at", "completed_at"):
        assert isinstance(table.c[name].type, sa.DateTime)
        assert table.c[name].type.timezone is True
    assert table.c.started_at.server_default is not None
    assert table.c.updated_at.server_default is not None
    assert table.c.updated_at.onupdate is not None
    assert table.c.completed_at.nullable is True
    assert {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
    } == {INDEX_NAME: ("status", "updated_at")}

    spec = OWNERSHIP_REGISTRY[TABLE_NAME]
    assert spec.ownership is OwnershipClass.SUBJECT_CONTROL
    assert spec.subject is TargetColumn.REQUIRED
    assert spec.actor is TargetColumn.NONE
    assert spec.connection is TargetColumn.NONE
    assert spec.platform_connection is TargetColumn.NONE
    assert spec.file_asset is TargetColumn.NONE
    assert spec.user_portable is False


def test_0045_revision_and_sqlite_empty_round_trip_match_the_model(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _create_pre_0045_schema(connection)
            migration = _migration(monkeypatch, connection)
            assert migration.revision == "0045"
            assert migration.down_revision == "0044"

            migration.upgrade()
            _assert_migration_matches_model(connection)
            migration.downgrade()
            assert TABLE_NAME not in inspect(connection).get_table_names()
            assert "health_subjects" in inspect(connection).get_table_names()

            migration.upgrade()
            _assert_migration_matches_model(connection)
    finally:
        engine.dispose()


def test_0045_populated_downgrade_refuses_before_any_ddl(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            subjects = _create_pre_0045_schema(connection)
            migration = _migration(monkeypatch, connection)
            migration.upgrade()
            subject_id = uuid.uuid4()
            connection.execute(subjects.insert().values(id=subject_id))
            checkpoints = _migrated_table(connection)
            connection.execute(
                checkpoints.insert().values(**_valid_values(subject_id.hex))
            )
            statements: list[str] = []

            def capture_statement(
                _connection,
                _cursor,
                statement,
                _parameters,
                _context,
                _executemany,
            ):
                statements.append(" ".join(statement.lower().split()))

            sa.event.listen(connection, "before_cursor_execute", capture_statement)
            try:
                with pytest.raises(RuntimeError, match=DOWNGRADE_REFUSAL):
                    migration.downgrade()
            finally:
                sa.event.remove(
                    connection,
                    "before_cursor_execute",
                    capture_statement,
                )

            assert statements
            assert all(
                not statement.startswith(("alter ", "create ", "drop "))
                for statement in statements
            )
            assert TABLE_NAME in inspect(connection).get_table_names()
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(checkpoints)
            ) == 1
    finally:
        engine.dispose()


async def _identity_graph(db_session, slug: str) -> HealthSubject:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status="active",
    )
    db_session.add(user)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"Synthetic {slug}",
        timezone="UTC",
    )
    db_session.add(subject)
    await db_session.flush()
    return subject


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unknown"},
        {"scan_high_watermark_id": -1, "last_scanned_id": 0},
        {"snapshot_rows": -1},
        {"last_scanned_id": -1},
        {"last_scanned_id": 11},
        {"scanned_rows": -1, "updated_rows": -1, "unchanged_rows": 0},
        {"scanned_rows": 5, "updated_rows": 6, "unchanged_rows": -1},
        {"scanned_rows": 5, "updated_rows": 2, "unchanged_rows": 2},
        {"snapshot_rows": 4},
        {"data_checksum_before": "A" * 64},
        {"data_checksum_after": "g" * 64},
        {"ownership_checksum_after": "a" * 63},
        {"completed_at": NOW},
        {"status": "restore_blocked", "completed_at": NOW},
        {"status": "restore_blocked"},
        {
            "status": "restore_blocked",
            "last_scanned_id": 0,
            "scanned_rows": 0,
            "updated_rows": 0,
            "unchanged_rows": 0,
            "data_checksum_before": "a" * 64,
            "data_checksum_after": "a" * 64,
        },
        {
            "status": "completed",
            "last_scanned_id": 10,
            "completed_at": None,
        },
        {"status": "completed", "completed_at": NOW},
        {
            "status": "completed",
            "snapshot_rows": 6,
            "last_scanned_id": 10,
            "completed_at": NOW,
        },
        {"started_at": NOW, "updated_at": NOW - timedelta(seconds=1)},
        {
            "status": "completed",
            "last_scanned_id": 10,
            "started_at": NOW,
            "updated_at": NOW,
            "completed_at": NOW - timedelta(seconds=1),
        },
    ],
    ids=[
        "status",
        "negative-high-watermark",
        "negative-snapshot-rows",
        "negative-last-scanned",
        "cursor-past-watermark",
        "negative-scanned-updated",
        "negative-unchanged",
        "unbalanced-counts",
        "scanned-past-snapshot",
        "uppercase-before-checksum",
        "nonhex-after-checksum",
        "short-ownership-checksum",
        "running-with-completed-time",
        "restore-blocked-with-completed-time",
        "restore-blocked-with-progress",
        "restore-blocked-with-nonempty-digest",
        "completed-without-completed-time",
        "completed-before-high-watermark",
        "completed-before-full-snapshot",
        "updated-before-started",
        "completed-before-started",
    ],
)
async def test_checkpoint_checks_reject_invalid_rows(db_session, changes):
    subject = await _identity_graph(db_session, f"checkpoint-{uuid.uuid4().hex}")
    db_session.add(
        OwnershipBackfillCheckpoint(**_valid_values(subject.id, **changes))
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_checkpoint_defaults_and_completed_state_round_trip(db_session):
    subject = await _identity_graph(db_session, "checkpoint-valid")
    running = OwnershipBackfillCheckpoint(
        phase_key="raw_payloads",
        subject_id=subject.id,
        scan_high_watermark_id=0,
        snapshot_rows=0,
        data_checksum_before=EMPTY_SHA256,
        data_checksum_after=EMPTY_SHA256,
        ownership_checksum_after=EMPTY_SHA256,
    )
    completed = OwnershipBackfillCheckpoint(
        **_valid_values(
            subject.id,
            phase_key="normalized_top_level",
            status="completed",
            last_scanned_id=10,
            started_at=NOW - timedelta(seconds=1),
            updated_at=NOW,
            completed_at=NOW,
        )
    )
    restore_blocked = OwnershipBackfillCheckpoint(
        phase_key="raw_payloads_restore_blocked",
        subject_id=subject.id,
        status="restore_blocked",
        scan_high_watermark_id=10,
        snapshot_rows=5,
        last_scanned_id=0,
        scanned_rows=0,
        updated_rows=0,
        unchanged_rows=0,
        data_checksum_before=EMPTY_SHA256,
        data_checksum_after=EMPTY_SHA256,
        ownership_checksum_after=EMPTY_SHA256,
    )
    db_session.add_all([running, completed, restore_blocked])
    await db_session.flush()

    assert running.status == "running"
    assert running.snapshot_rows == 0
    assert running.last_scanned_id == 0
    assert running.scanned_rows == 0
    assert running.updated_rows == 0
    assert running.unchanged_rows == 0
    assert running.completed_at is None
    assert completed.status == "completed"
    assert completed.scanned_rows == completed.snapshot_rows == 5
    assert completed.completed_at is not None
    assert restore_blocked.status == "restore_blocked"
    assert restore_blocked.completed_at is None


@pytest.mark.integration
async def test_postgres_checkpoint_fk_and_timestamps_are_native(db_session):
    subject = await _identity_graph(db_session, "checkpoint-postgres")
    checkpoint = OwnershipBackfillCheckpoint(
        **_valid_values(subject.id, phase_key="raw_payloads_postgres")
    )
    db_session.add(checkpoint)
    await db_session.commit()
    db_session.expunge_all()

    persisted = await db_session.get(
        OwnershipBackfillCheckpoint,
        "raw_payloads_postgres",
    )
    assert persisted is not None
    assert persisted.started_at.tzinfo is not None
    assert persisted.updated_at.tzinfo is not None

    db_session.add(
        OwnershipBackfillCheckpoint(
            **_valid_values(
                uuid.uuid4(),
                phase_key="foreign-subject",
            )
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
async def test_postgres_0045_locks_guard_then_empty_round_trips(
    db_session,
    monkeypatch,
):
    subject = await _identity_graph(db_session, "checkpoint-pg-migration")
    db_session.add(
        OwnershipBackfillCheckpoint(
            **_valid_values(subject.id, phase_key="postgres_migration")
        )
    )
    await db_session.commit()
    connection = await db_session.connection()

    def guard_and_round_trip(sync_connection):
        migration = _migration(monkeypatch, sync_connection)
        statements: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            statements.append(" ".join(statement.lower().split()))

        sa.event.listen(sync_connection, "before_cursor_execute", capture_statement)
        try:
            with pytest.raises(RuntimeError, match=DOWNGRADE_REFUSAL):
                migration.downgrade()
            refusal_statements = tuple(statements)
            assert refusal_statements[0] == (
                "lock table ownership_backfill_checkpoints "
                "in access exclusive mode"
            )
            assert all(
                not statement.startswith(("alter ", "create ", "drop "))
                for statement in refusal_statements
            )

            checkpoints = _migrated_table(sync_connection)
            sync_connection.execute(checkpoints.delete())
            statements.clear()
            migration.downgrade()
            assert TABLE_NAME not in inspect(sync_connection).get_table_names()
            migration.upgrade()
            _assert_migration_matches_model(sync_connection)
        finally:
            sa.event.remove(
                sync_connection,
                "before_cursor_execute",
                capture_statement,
            )

    await connection.run_sync(guard_and_round_trip)
