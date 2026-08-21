"""Exact migration contract for revision 0038's child ownership expansion."""
from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import UniqueConstraint, Uuid, inspect
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError

import vitals.models  # noqa: F401 -- register the complete model graph
from vitals.models.base import Base
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.models.hrt import (
    HrtCompound,
    HrtCompoundComponent,
    HrtCycle,
    HrtCycleItem,
    HrtCycleTemplate,
    HrtCycleTemplateItem,
)


SUBJECT = "subject_id"
CONNECTION = "integration_connection_id"

# Literal product contract: never derive the expectation from the migration.
EXPECTED_CHILD_COLUMNS: dict[str, tuple[str, ...]] = {
    "body_scan_metrics": (SUBJECT,),
    "hevy_exercises": (SUBJECT, CONNECTION),
    "hevy_sets": (SUBJECT, CONNECTION),
    "hrt_compound_components": (SUBJECT,),
    "hrt_cycle_items": (SUBJECT,),
    "hrt_cycle_template_items": (SUBJECT,),
}

# The exact parent each child's ``subject_id`` must equal from revision 0046.
SUBJECT_EQUALITY_TARGETS = {
    "body_scan_metrics": "body_scans.subject_id",
    "hevy_exercises": "hevy_workouts.subject_id",
    "hevy_sets": "hevy_exercises.subject_id",
    "hrt_compound_components": "hrt_compounds.subject_id",
    "hrt_cycle_items": "hrt_cycles.subject_id",
    "hrt_cycle_template_items": "hrt_cycle_templates.subject_id",
}

COLUMN_TARGETS = {
    SUBJECT: "health_subjects.id",
    CONNECTION: "integration_connections.id",
}

EXPECTED_PARENT_UNIQUES = {
    "hevy_exercises": "uq_hevy_exercises_id_subject",
}

LEGACY_PARENT_FKS: dict[
    str, tuple[tuple[str, str, str], ...]
] = {
    "body_scan_metrics": (("scan_id", "body_scans.id", "CASCADE"),),
    "hevy_exercises": (("workout_id", "hevy_workouts.id", "CASCADE"),),
    "hevy_sets": (("exercise_id", "hevy_exercises.id", "CASCADE"),),
    "hrt_compound_components": (
        ("compound_id", "hrt_compounds.id", "CASCADE"),
    ),
    "hrt_cycle_items": (
        ("cycle_id", "hrt_cycles.id", "CASCADE"),
        ("compound_id", "hrt_compounds.id", "SET NULL"),
    ),
    "hrt_cycle_template_items": (
        ("template_id", "hrt_cycle_templates.id", "CASCADE"),
    ),
}

LEGACY_INDEXES: dict[str, dict[str, tuple[str, ...]]] = {
    "body_scan_metrics": {
        "ix_body_scan_metrics_scan": ("scan_id",),
        "ix_body_scan_metrics_key": ("metric_key",),
    },
    "hevy_exercises": {
        "ix_hevy_exercises_template": ("exercise_template_id",),
        "ix_hevy_exercises_workout": ("workout_id",),
    },
    "hevy_sets": {"ix_hevy_sets_exercise": ("exercise_id",)},
    "hrt_compound_components": {
        "ix_hrt_compound_components_compound": ("compound_id",),
    },
    "hrt_cycle_items": {"ix_hrt_cycle_items_cycle": ("cycle_id",)},
    "hrt_cycle_template_items": {
        "ix_hrt_cycle_template_items_template": ("template_id",),
    },
}

OWNERSHIP_VOCABULARY = {
    SUBJECT,
    "actor_user_id",
    CONNECTION,
    "file_asset_id",
    "recipient_user_id",
    "requested_by_user_id",
    "created_by_user_id",
    "revoked_by_user_id",
    "overridden_by_user_id",
    "resolved_by_user_id",
}


def _migration() -> Any:
    return importlib.import_module(
        "migrations.versions.0038_nullable_child_ownership"
    )


def _column_names(item: Any) -> tuple[str, ...]:
    return tuple(column.name for column in item.columns)


def _model_index_map(table: sa.Table) -> dict[str, sa.Index]:
    return {index.name: index for index in table.indexes if index.name is not None}


def test_0038_revision_registry_matches_literal_six_table_matrix():
    migration = _migration()
    expected_migration_columns = {
        table_name: tuple(
            (column_name, COLUMN_TARGETS[column_name].split(".", 1)[0])
            for column_name in column_names
        )
        for table_name, column_names in EXPECTED_CHILD_COLUMNS.items()
    }

    assert migration.revision == "0038"
    assert migration.down_revision == "0037"
    assert migration._OWNERSHIP_COLUMNS == expected_migration_columns
    assert migration._PARENT_UNIQUES == EXPECTED_PARENT_UNIQUES
    assert len(migration._OWNERSHIP_COLUMNS) == 6
    assert sum(map(len, migration._OWNERSHIP_COLUMNS.values())) == 8


def test_0038_orm_matches_literal_nullable_fk_index_and_unique_contract():
    for table_name, expected_columns in EXPECTED_CHILD_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        actual_columns = OWNERSHIP_VOCABULARY.intersection(table.columns.keys())
        assert actual_columns == set(expected_columns), table_name

        indexes = _model_index_map(table)
        for column_name in expected_columns:
            column = table.c[column_name]
            assert isinstance(column.type, Uuid), f"{table_name}.{column_name}"
            assert column.type.as_uuid is True
            assert column.nullable is True
            assert column.default is None
            assert column.server_default is None

            # Revision 0038 owns the nullable ownership reference; revision 0046
            # adds the Stage-4 subject-equality pair on top of it, which cascades
            # with the parent it doubles.
            ownership_keys = {
                foreign_key.target_fullname: foreign_key.ondelete
                for foreign_key in column.foreign_keys
            }
            expected = {COLUMN_TARGETS[column_name]: "RESTRICT"}
            equality_target = SUBJECT_EQUALITY_TARGETS.get(table_name)
            if column_name == "subject_id" and equality_target is not None:
                expected[equality_target] = "CASCADE"
            assert ownership_keys == expected

            index_name = f"ix_{table_name}_{column_name}"
            assert index_name in indexes
            assert _column_names(indexes[index_name]) == (column_name,)
            assert indexes[index_name].unique is False

        uniques = {
            constraint.name: _column_names(constraint)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        if table_name == "hevy_exercises":
            assert uniques == {
                "uq_hevy_exercises_id_subject": ("id", SUBJECT)
            }
        else:
            assert uniques == {}

        actual_legacy_fks = {
            (
                tuple(constraint.column_keys),
                tuple(element.target_fullname for element in constraint.elements),
                constraint.ondelete,
            )
            for constraint in table.foreign_key_constraints
            if not set(constraint.column_keys).intersection(expected_columns)
        }
        expected_legacy_fks = {
            ((column_name,), (target,), ondelete)
            for column_name, target, ondelete in LEGACY_PARENT_FKS[table_name]
        }
        assert actual_legacy_fks == expected_legacy_fks


def _subject_parent(
    metadata: sa.MetaData,
    name: str,
) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            SUBJECT,
            sa.Uuid(),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.UniqueConstraint("id", SUBJECT, name=f"uq_{name}_id_subject"),
    )


def _create_minimal_0037_schema(connection: sa.Connection) -> None:
    """Create the real roots, parent keys and legacy child FK/index shape."""

    metadata = sa.MetaData()
    sa.Table(
        "health_subjects",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    sa.Table(
        "integration_connections",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            SUBJECT,
            sa.Uuid(),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "id", SUBJECT, name="uq_integration_connections_id_subject"
        ),
    )

    _subject_parent(metadata, "body_scans")
    _subject_parent(metadata, "hevy_workouts")
    _subject_parent(metadata, "hrt_compounds")
    _subject_parent(metadata, "hrt_cycles")
    _subject_parent(metadata, "hrt_cycle_templates")

    body_metrics = sa.Table(
        "body_scan_metrics",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scan_id",
            sa.Integer(),
            sa.ForeignKey("body_scans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric_key", sa.String(64), nullable=False),
    )
    hevy_exercises = sa.Table(
        "hevy_exercises",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workout_id",
            sa.Integer(),
            sa.ForeignKey("hevy_workouts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("exercise_template_id", sa.String(64), nullable=True),
    )
    hevy_sets = sa.Table(
        "hevy_sets",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "exercise_id",
            sa.Integer(),
            sa.ForeignKey("hevy_exercises.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    compound_components = sa.Table(
        "hrt_compound_components",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "compound_id",
            sa.Integer(),
            sa.ForeignKey("hrt_compounds.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    cycle_items = sa.Table(
        "hrt_cycle_items",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.Integer(),
            sa.ForeignKey("hrt_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "compound_id",
            sa.Integer(),
            sa.ForeignKey("hrt_compounds.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    template_items = sa.Table(
        "hrt_cycle_template_items",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("hrt_cycle_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    tables = {
        table.name: table
        for table in (
            body_metrics,
            hevy_exercises,
            hevy_sets,
            compound_components,
            cycle_items,
            template_items,
        )
    }
    for table_name, indexes in LEGACY_INDEXES.items():
        table = tables[table_name]
        for index_name, columns in indexes.items():
            sa.Index(index_name, *(table.c[column_name] for column_name in columns))

    metadata.create_all(connection)


def _inspector_index_map(
    inspector: sa.Inspector,
    table_name: str,
) -> dict[str, dict[str, Any]]:
    return {
        index["name"]: index
        for index in inspector.get_indexes(table_name)
        if index["name"] is not None
    }


def _inspector_unique_map(
    inspector: sa.Inspector,
    table_name: str,
) -> dict[str, tuple[str, ...]]:
    return {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint["name"] is not None
    }


def _legacy_child_signature(connection: sa.Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    return {
        table_name: {
            "columns": {
                column["name"]: (
                    str(column["type"]),
                    bool(column["nullable"]),
                    bool(column["primary_key"]),
                )
                for column in inspector.get_columns(table_name)
                if column["name"] not in EXPECTED_CHILD_COLUMNS[table_name]
            },
            "foreign_keys": {
                (
                    tuple(foreign_key["constrained_columns"]),
                    foreign_key["referred_table"],
                    tuple(foreign_key["referred_columns"]),
                    (foreign_key.get("options") or {}).get("ondelete"),
                )
                for foreign_key in inspector.get_foreign_keys(table_name)
                if not set(foreign_key["constrained_columns"]).intersection(
                    EXPECTED_CHILD_COLUMNS[table_name]
                )
            },
            "indexes": {
                name: (tuple(index["column_names"]), bool(index["unique"]))
                for name, index in _inspector_index_map(
                    inspector, table_name
                ).items()
                if name in LEGACY_INDEXES[table_name]
            },
        }
        for table_name in EXPECTED_CHILD_COLUMNS
    }


def _assert_expanded_0038_schema(connection: sa.Connection) -> None:
    inspector = inspect(connection)
    sqlite_dialect = sqlite.dialect()

    for table_name, expected_columns in EXPECTED_CHILD_COLUMNS.items():
        model_table = Base.metadata.tables[table_name]
        columns = {
            column["name"]: column
            for column in inspector.get_columns(table_name)
        }
        foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        indexes = _inspector_index_map(inspector, table_name)

        for column_name in expected_columns:
            migrated = columns[column_name]
            model_column = model_table.c[column_name]
            assert str(migrated["type"]) == str(
                model_column.type.compile(dialect=sqlite_dialect)
            )
            assert migrated["nullable"] is model_column.nullable is True

            foreign_key = foreign_keys[f"fk_{table_name}_{column_name}"]
            assert foreign_key["constrained_columns"] == [column_name]
            assert foreign_key["referred_table"] == COLUMN_TARGETS[
                column_name
            ].split(".", 1)[0]
            assert foreign_key["referred_columns"] == ["id"]
            assert foreign_key["options"]["ondelete"] == "RESTRICT"

            index = indexes[f"ix_{table_name}_{column_name}"]
            assert index["column_names"] == [column_name]
            assert index["unique"] == 0

        uniques = _inspector_unique_map(inspector, table_name)
        if table_name == "hevy_exercises":
            assert uniques["uq_hevy_exercises_id_subject"] == (
                "id",
                SUBJECT,
            )
        else:
            assert not set(uniques).intersection(EXPECTED_PARENT_UNIQUES.values())


def test_real_0038_sqlite_upgrade_downgrade_and_reupgrade(
    monkeypatch: pytest.MonkeyPatch,
):
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _create_minimal_0037_schema(connection)
            schema_at_0037 = _legacy_child_signature(connection)
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(migration, "op", operations)

            migration.upgrade()
            _assert_expanded_0038_schema(connection)
            assert _legacy_child_signature(connection) == schema_at_0037

            migration.downgrade()
            assert _legacy_child_signature(connection) == schema_at_0037
            inspector = inspect(connection)
            for table_name, columns in EXPECTED_CHILD_COLUMNS.items():
                remaining = {
                    column["name"]
                    for column in inspector.get_columns(table_name)
                }
                assert set(columns).isdisjoint(remaining)
            assert "uq_hevy_exercises_id_subject" not in _inspector_unique_map(
                inspector, "hevy_exercises"
            )

            migration.upgrade()
            _assert_expanded_0038_schema(connection)
            assert _legacy_child_signature(connection) == schema_at_0037
    finally:
        engine.dispose()


def _seed_minimal_child_rows(connection: sa.Connection) -> dict[str, str]:
    subject_id = uuid.uuid4().hex
    connection_id = uuid.uuid4().hex
    connection.execute(
        sa.text("INSERT INTO health_subjects (id) VALUES (:id)"),
        {"id": subject_id},
    )
    connection.execute(
        sa.text(
            "INSERT INTO integration_connections (id, subject_id) "
            "VALUES (:id, :subject_id)"
        ),
        {"id": connection_id, "subject_id": subject_id},
    )

    for table_name in (
        "body_scans",
        "hevy_workouts",
        "hrt_compounds",
        "hrt_cycles",
        "hrt_cycle_templates",
    ):
        connection.execute(
            sa.text(
                f"INSERT INTO {table_name} (id, subject_id) "
                "VALUES (1, :subject_id)"
            ),
            {"subject_id": subject_id},
        )

    inserts = (
        "INSERT INTO body_scan_metrics (id, scan_id, metric_key) "
        "VALUES (1, 1, 'weight')",
        "INSERT INTO hevy_exercises "
        "(id, workout_id, exercise_template_id) VALUES (1, 1, 'bench')",
        "INSERT INTO hevy_sets (id, exercise_id) VALUES (1, 1)",
        "INSERT INTO hrt_compound_components (id, compound_id) VALUES (1, 1)",
        "INSERT INTO hrt_cycle_items (id, cycle_id, compound_id) VALUES (1, 1, 1)",
        "INSERT INTO hrt_cycle_template_items (id, template_id) VALUES (1, 1)",
    )
    for statement in inserts:
        connection.execute(sa.text(statement))

    return {SUBJECT: subject_id, CONNECTION: connection_id}


@pytest.fixture
def expanded_sqlite_0038(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[sa.Connection, Any, dict[str, str]]]:
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _create_minimal_0037_schema(connection)
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(migration, "op", operations)
            migration.upgrade()
            root_ids = _seed_minimal_child_rows(connection)
            yield connection, migration, root_ids
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        (table_name, column_name)
        for table_name, columns in EXPECTED_CHILD_COLUMNS.items()
        for column_name in columns
    ],
)
def test_0038_downgrade_refuses_each_populated_ownership_field(
    expanded_sqlite_0038: tuple[sa.Connection, Any, dict[str, str]],
    table_name: str,
    column_name: str,
):
    connection, migration, root_ids = expanded_sqlite_0038
    connection.execute(
        sa.text(
            f"UPDATE {table_name} SET {column_name} = :value WHERE id = 1"
        ),
        {"value": root_ids[column_name]},
    )

    with pytest.raises(RuntimeError, match=table_name):
        migration.downgrade()

    # The safety probe runs before any destructive DDL.
    inspector = inspect(connection)
    for expected_table, expected_columns in EXPECTED_CHILD_COLUMNS.items():
        remaining = {
            column["name"]
            for column in inspector.get_columns(expected_table)
        }
        assert set(expected_columns) <= remaining


async def _seed_postgres_child_graph(db_session: Any) -> dict[str, int]:
    scan = BodyScan(
        date=date(2026, 8, 19),
        domain="body_comp",
        source="manual",
    )
    workout = HevyWorkout(
        external_id="0038-pg-workout",
        date=date(2026, 8, 19),
        domain="workouts",
        source="hevy_api",
    )
    compound = HrtCompound(
        domain="hrt",
        source="manual",
        key="0038_testosterone",
        name="Synthetic testosterone",
        compound_class="testosterone",
        route="intramuscular",
    )
    cycle = HrtCycle(
        domain="hrt",
        source="manual",
        kind="course",
        start_date=date(2026, 8, 19),
    )
    template = HrtCycleTemplate(
        domain="hrt",
        source="manual",
        name="0038 template",
        kind="course",
    )
    db_session.add_all([scan, workout, compound, cycle, template])
    await db_session.flush()

    metric = BodyScanMetric(
        scan_id=scan.id,
        metric_key="weight",
        label="Weight",
        value=73.0,
    )
    exercise = HevyExercise(
        workout_id=workout.id,
        exercise_index=0,
        title="Bench Press",
        exercise_template_id="bench",
    )
    component = HrtCompoundComponent(
        compound_id=compound.id,
        ester="enanthate",
        mg=250.0,
    )
    cycle_item = HrtCycleItem(
        cycle_id=cycle.id,
        compound_id=compound.id,
        compound_key=compound.key,
        schedule=[{"dose": 250, "interval_days": 7}],
    )
    template_item = HrtCycleTemplateItem(
        template_id=template.id,
        compound_key=compound.key,
        schedule=[{"dose": 250, "interval_days": 7}],
    )
    db_session.add_all(
        [metric, exercise, component, cycle_item, template_item]
    )
    await db_session.flush()
    hevy_set = HevySet(exercise_id=exercise.id, set_index=0, reps=8)
    db_session.add(hevy_set)
    await db_session.flush()

    return {
        "body_scan_metrics": metric.id,
        "hevy_exercises": exercise.id,
        "hevy_sets": hevy_set.id,
        "hrt_compound_components": component.id,
        "hrt_cycle_items": cycle_item.id,
        "hrt_cycle_template_items": template_item.id,
    }


@pytest.mark.integration
async def test_postgres_enforces_every_0038_ownership_foreign_key(db_session):
    row_ids = await _seed_postgres_child_graph(db_session)

    for table_name, columns in EXPECTED_CHILD_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        for column_name in columns:
            with pytest.raises(IntegrityError):
                async with db_session.begin_nested():
                    await db_session.execute(
                        table.update()
                        .where(table.c.id == row_ids[table_name])
                        .values({column_name: uuid.uuid4()})
                    )
