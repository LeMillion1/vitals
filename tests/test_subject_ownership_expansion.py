"""Exact schema contract for revision 0037's nullable ownership expansion.

These tests deliberately keep the expected inventory literal.  Importing the
migration's private registries as the expectation would let the ORM and Alembic
drift together while the test remained green.
"""
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
from sqlalchemy import Index, Integer, UniqueConstraint, Uuid, inspect
from sqlalchemy.exc import IntegrityError

import vitals.models  # noqa: F401 -- register the complete model graph
from vitals.models.base import Base
from vitals.models.weight import WeightLog


SUBJECT = "subject_id"
ACTOR = "actor_user_id"
CONNECTION = "integration_connection_id"
FILE_ASSET = "file_asset_id"
RECIPIENT = "recipient_user_id"
REQUESTED_BY = "requested_by_user_id"
CREATED_BY = "created_by_user_id"
REVOKED_BY = "revoked_by_user_id"
OVERRIDDEN_BY = "overridden_by_user_id"
RESOLVED_BY = "resolved_by_user_id"

EXPECTED_OWNERSHIP_COLUMNS: dict[str, tuple[str, ...]] = {
    "raw_payloads": (SUBJECT, ACTOR, CONNECTION, FILE_ASSET),
    "weight_logs": (SUBJECT, ACTOR, CONNECTION),
    "body_scans": (SUBJECT, ACTOR, FILE_ASSET),
    "hevy_workouts": (SUBJECT, ACTOR, CONNECTION),
    "hrt_compounds": (SUBJECT, ACTOR),
    "hrt_cycles": (SUBJECT, ACTOR),
    "hrt_cycle_templates": (SUBJECT, ACTOR),
    "annotations": (SUBJECT, ACTOR),
    "body_measurements": (SUBJECT, ACTOR),
    "conflict_rules": (SUBJECT,),
    "day_context": (SUBJECT, ACTOR, CONNECTION),
    "garmin_activities": (SUBJECT, ACTOR, CONNECTION),
    "garmin_daily": (SUBJECT, ACTOR, CONNECTION),
    "garmin_intraday": (SUBJECT, CONNECTION),
    "garmin_weight_exports": (SUBJECT, CONNECTION, REQUESTED_BY),
    "genetic_variants": (SUBJECT, ACTOR),
    "glp1_dose_phases": (SUBJECT, ACTOR),
    "glp1_injections": (SUBJECT, ACTOR),
    "glp1_side_effects": (SUBJECT, ACTOR),
    "hrt_doses": (SUBJECT, ACTOR),
    "hrt_side_effects": (SUBJECT, ACTOR),
    "lab_markers": (SUBJECT, ACTOR),
    "lab_results": (SUBJECT, ACTOR),
    "meal_logs": (SUBJECT, ACTOR),
    "milestones": (SUBJECT, ACTOR),
    "noise_markers": (SUBJECT, ACTOR),
    "notifications": (SUBJECT, RECIPIENT, ACTOR, CONNECTION),
    "progress_photos": (SUBJECT, ACTOR, FILE_ASSET),
    "shared_reports": (SUBJECT, CREATED_BY, REVOKED_BY),
    "signals": (SUBJECT, ACTOR, CONNECTION),
    "skincare_logs": (SUBJECT, ACTOR),
    "skincare_observations": (SUBJECT, ACTOR),
    "skincare_products": (SUBJECT, ACTOR),
    "supplements": (SUBJECT, ACTOR),
    "system_alerts": (SUBJECT, CONNECTION, OVERRIDDEN_BY, RESOLVED_BY),
    "weekly_digests": (SUBJECT, ACTOR, CONNECTION),
}

COLUMN_TARGETS = {
    SUBJECT: "health_subjects.id",
    ACTOR: "users.id",
    CONNECTION: "integration_connections.id",
    FILE_ASSET: "file_assets.id",
    RECIPIENT: "users.id",
    REQUESTED_BY: "users.id",
    CREATED_BY: "users.id",
    REVOKED_BY: "users.id",
    OVERRIDDEN_BY: "users.id",
    RESOLVED_BY: "users.id",
}

MULTI_FOREIGN_KEY_TARGETS = {
    ("notifications", SUBJECT): {
        "health_subjects.id",
        "ai_invocations.subject_id",
        "notification_delivery_intents.subject_id",
    },
    ("notifications", RECIPIENT): {
        "users.id",
        "notification_delivery_intents.recipient_user_id",
    },
    ("notifications", CONNECTION): {
        "integration_connections.id",
        "notification_delivery_intents.integration_connection_id",
    },
    ("system_alerts", SUBJECT): {
        "health_subjects.id",
        "ai_invocations.subject_id",
    },
    ("weekly_digests", SUBJECT): {
        "health_subjects.id",
        "ai_invocations.subject_id",
    },
}

EXPECTED_INSIGHTS_TABLES = (
    "annotations",
    "body_measurements",
    "body_scans",
    "day_context",
    "garmin_activities",
    "garmin_daily",
    "garmin_intraday",
    "glp1_injections",
    "glp1_side_effects",
    "hevy_workouts",
    "hrt_doses",
    "hrt_side_effects",
    "lab_results",
    "meal_logs",
    "progress_photos",
    "signals",
    "skincare_logs",
    "skincare_observations",
    "weekly_digests",
    "weight_logs",
)

EXPECTED_QUERY_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ix_annotations_subject_date_range": (
        "annotations",
        (SUBJECT, "date", "end_date"),
    ),
    "ix_noise_markers_subject_domain_range": (
        "noise_markers",
        (SUBJECT, "domain", "start_date", "end_date"),
    ),
    "ix_glp1_dose_phases_subject_domain_range": (
        "glp1_dose_phases",
        (SUBJECT, "domain", "start_date", "end_date"),
    ),
    "ix_hrt_cycles_subject_domain_range": (
        "hrt_cycles",
        (SUBJECT, "domain", "start_date", "end_date"),
    ),
    "ix_garmin_activities_connection_external": (
        "garmin_activities",
        (CONNECTION, "external_id"),
    ),
    "ix_garmin_daily_connection_date": (
        "garmin_daily",
        (CONNECTION, "date"),
    ),
    "ix_garmin_intraday_connection_series_date": (
        "garmin_intraday",
        (CONNECTION, "series_type", "date"),
    ),
    "ix_garmin_intraday_connection_date_ts": (
        "garmin_intraday",
        (CONNECTION, "date", "ts"),
    ),
    "ix_garmin_weight_exports_subject_date": (
        "garmin_weight_exports",
        (SUBJECT, "date"),
    ),
    "ix_garmin_weight_exports_connection_date": (
        "garmin_weight_exports",
        (CONNECTION, "date"),
    ),
    "ix_garmin_weight_exports_connection_status_next": (
        "garmin_weight_exports",
        (CONNECTION, "status", "next_attempt_at"),
    ),
    "ix_hevy_workouts_connection_external": (
        "hevy_workouts",
        (CONNECTION, "external_id"),
    ),
    "ix_genetic_variants_subject_rsid": (
        "genetic_variants",
        (SUBJECT, "rsid"),
    ),
    "ix_genetic_variants_subject_marker": (
        "genetic_variants",
        (SUBJECT, "marker"),
    ),
    "ix_hrt_compounds_subject_key": (
        "hrt_compounds",
        (SUBJECT, "key"),
    ),
    "ix_hrt_compounds_subject_active": (
        "hrt_compounds",
        (SUBJECT, "active"),
    ),
    "ix_hrt_cycle_templates_subject_name": (
        "hrt_cycle_templates",
        (SUBJECT, "name"),
    ),
    "ix_hrt_doses_subject_compound_date": (
        "hrt_doses",
        (SUBJECT, "compound_key", "date"),
    ),
    "ix_lab_markers_subject_name": (
        "lab_markers",
        (SUBJECT, "name"),
    ),
    "ix_lab_results_subject_marker_date": (
        "lab_results",
        (SUBJECT, "marker", "date"),
    ),
    "ix_milestones_subject_status": (
        "milestones",
        (SUBJECT, "status"),
    ),
    "ix_milestones_subject_deadline": (
        "milestones",
        (SUBJECT, "deadline"),
    ),
    "ix_notifications_subject_sent": (
        "notifications",
        (SUBJECT, "sent_at"),
    ),
    "ix_notifications_recipient_subject_category_sent": (
        "notifications",
        (RECIPIENT, SUBJECT, "category", "sent_at"),
    ),
    "ix_notifications_connection_recipient_dedupe": (
        "notifications",
        (CONNECTION, RECIPIENT, "dedupe_key"),
    ),
    "ix_notifications_connection_recipient_external": (
        "notifications",
        (CONNECTION, RECIPIENT, "external_id"),
    ),
    "ix_raw_payloads_subject_domain_processed": (
        "raw_payloads",
        (SUBJECT, "domain", "processed_at"),
    ),
    "ix_raw_payloads_connection_domain_processed": (
        "raw_payloads",
        (CONNECTION, "domain", "processed_at"),
    ),
    "ix_raw_payloads_subject_domain_source_external": (
        "raw_payloads",
        (SUBJECT, "domain", "source", "external_id"),
    ),
    "ix_raw_payloads_connection_domain_source_external": (
        "raw_payloads",
        (CONNECTION, "domain", "source", "external_id"),
    ),
    "ix_shared_reports_subject_created": (
        "shared_reports",
        (SUBJECT, "created_at"),
    ),
    "ix_signals_subject_batch": ("signals", (SUBJECT, "batch_id")),
    "ix_signals_subject_key_date": (
        "signals",
        (SUBJECT, "key", "date"),
    ),
    "ix_signals_connection_batch": (
        "signals",
        (CONNECTION, "batch_id"),
    ),
    "ix_skincare_products_subject_name": (
        "skincare_products",
        (SUBJECT, "name"),
    ),
    "ix_skincare_products_subject_type": (
        "skincare_products",
        (SUBJECT, "type"),
    ),
    "ix_skincare_products_subject_active": (
        "skincare_products",
        (SUBJECT, "active"),
    ),
    "ix_supplements_subject_key": (
        "supplements",
        (SUBJECT, "key"),
    ),
    "ix_supplements_subject_active": (
        "supplements",
        (SUBJECT, "active"),
    ),
    "ix_system_alerts_subject_domain_resolved": (
        "system_alerts",
        (SUBJECT, "domain", "resolved_at"),
    ),
    "ix_system_alerts_connection_domain_resolved": (
        "system_alerts",
        (CONNECTION, "domain", "resolved_at"),
    ),
    "ix_weekly_digests_subject_kind_date": (
        "weekly_digests",
        (SUBJECT, "kind", "date"),
    ),
    "ix_weight_logs_connection_date": (
        "weight_logs",
        (CONNECTION, "date"),
    ),
}

EXPECTED_PARENT_UNIQUES = {
    "raw_payloads": "uq_raw_payloads_id_subject",
    "weight_logs": "uq_weight_logs_id_subject",
    "body_scans": "uq_body_scans_id_subject",
    "hevy_workouts": "uq_hevy_workouts_id_subject",
    "hrt_compounds": "uq_hrt_compounds_id_subject",
    "hrt_cycles": "uq_hrt_cycles_id_subject",
    "hrt_cycle_templates": "uq_hrt_cycle_templates_id_subject",
    "integration_connections": "uq_integration_connections_id_subject",
    "file_assets": "uq_file_assets_id_subject",
}

# name -> (table, columns, PostgreSQL predicate, SQLite predicate)
LEGACY_GLOBAL_UNIQUES: dict[
    str, tuple[str, tuple[str, ...], str | None, str | None]
] = {
    "uq_body_measurement_per_date": (
        "body_measurements",
        ("date",),
        None,
        None,
    ),
    "uq_day_context_per_date": ("day_context", ("date",), None, None),
    "uq_garmin_daily_date": ("garmin_daily", ("date",), None, None),
    "uq_garmin_activities_external_id": (
        "garmin_activities",
        ("external_id",),
        None,
        None,
    ),
    "uq_garmin_weight_exports_date": (
        "garmin_weight_exports",
        ("date",),
        None,
        None,
    ),
    "uq_hevy_workouts_external_id": (
        "hevy_workouts",
        ("external_id",),
        None,
        None,
    ),
    "uq_genetic_variant_rsid": (
        "genetic_variants",
        ("rsid",),
        "rsid IS NOT NULL",
        "rsid IS NOT NULL",
    ),
    "ix_conflict_rules_code": ("conflict_rules", ("code",), None, None),
    "ix_hrt_compounds_key": ("hrt_compounds", ("key",), None, None),
    "ix_lab_markers_name": ("lab_markers", ("name",), None, None),
    "uq_notification_dedupe_key": (
        "notifications",
        ("dedupe_key",),
        "dedupe_key IS NOT NULL",
        "dedupe_key IS NOT NULL",
    ),
    "uq_active_alert_per_key_entity": (
        "system_alerts",
        ("alert_key", "entity_ref"),
        "resolved_at IS NULL",
        "resolved_at IS NULL",
    ),
    "ix_shared_reports_token": ("shared_reports", ("token",), None, None),
    "uq_active_weight_per_date": (
        "weight_logs",
        ("date",),
        "superseded = false",
        "superseded = 0",
    ),
}


def _migration():
    return importlib.import_module(
        "migrations.versions.0037_nullable_subject_ownership"
    )


def _index_map(table: sa.Table) -> dict[str, Index]:
    return {index.name: index for index in table.indexes if index.name is not None}


def _column_names(item: Any) -> tuple[str, ...]:
    return tuple(column.name for column in item.columns)


def test_revision_registries_match_the_literal_36_table_contract():
    migration = _migration()
    target_tables = {
        column: target.rsplit(".", 1)[0]
        for column, target in COLUMN_TARGETS.items()
    }
    expected_migration_columns = {
        table_name: tuple(
            (column_name, target_tables[column_name])
            for column_name in column_names
        )
        for table_name, column_names in EXPECTED_OWNERSHIP_COLUMNS.items()
    }

    assert migration.revision == "0037"
    assert migration.down_revision == "0036"
    assert migration._OWNERSHIP_COLUMNS == expected_migration_columns
    assert len(migration._OWNERSHIP_COLUMNS) == 36
    assert migration._INSIGHTS_TABLES == EXPECTED_INSIGHTS_TABLES
    assert migration._QUERY_INDEXES == EXPECTED_QUERY_INDEXES
    assert migration._PARENT_UNIQUES == EXPECTED_PARENT_UNIQUES


def test_all_36_models_have_exact_nullable_uuid_fk_and_simple_index_contract():
    ownership_vocabulary = set(COLUMN_TARGETS)

    for table_name, expected_columns in EXPECTED_OWNERSHIP_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        actual_columns = ownership_vocabulary.intersection(table.columns.keys())
        assert actual_columns == set(expected_columns), table_name

        indexes = _index_map(table)
        for column_name in expected_columns:
            column = table.c[column_name]
            assert isinstance(column.type, Uuid), f"{table_name}.{column_name}"
            assert column.type.as_uuid is True
            assert column.nullable is True

            expected_targets = MULTI_FOREIGN_KEY_TARGETS.get(
                (table_name, column_name),
                {COLUMN_TARGETS[column_name]},
            )
            foreign_keys = list(column.foreign_keys)
            assert {
                foreign_key.target_fullname for foreign_key in foreign_keys
            } == expected_targets
            assert all(
                foreign_key.ondelete == "RESTRICT"
                for foreign_key in foreign_keys
            )

            index_name = f"ix_{table_name}_{column_name}"
            assert index_name in indexes
            assert _column_names(indexes[index_name]) == (column_name,)
            assert indexes[index_name].unique is False


def test_exact_20_insights_composites_and_43_query_indexes_match_models():
    assert len(EXPECTED_INSIGHTS_TABLES) == 20
    assert len(EXPECTED_QUERY_INDEXES) == 43

    for table_name in EXPECTED_INSIGHTS_TABLES:
        indexes = _index_map(Base.metadata.tables[table_name])
        expected = {
            f"ix_{table_name}_subject_date": (SUBJECT, "date"),
            f"ix_{table_name}_subject_domain_date": (
                SUBJECT,
                "domain",
                "date",
            ),
        }
        for index_name, columns in expected.items():
            assert index_name in indexes
            assert _column_names(indexes[index_name]) == columns
            assert indexes[index_name].unique is False

    for index_name, (table_name, columns) in EXPECTED_QUERY_INDEXES.items():
        indexes = _index_map(Base.metadata.tables[table_name])
        assert index_name in indexes
        assert _column_names(indexes[index_name]) == columns
        assert indexes[index_name].unique is False


def test_exact_parent_composite_uniques_and_genetics_raw_link_match_models():
    for table_name, constraint_name in EXPECTED_PARENT_UNIQUES.items():
        constraints = {
            constraint.name: constraint
            for constraint in Base.metadata.tables[table_name].constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert constraint_name in constraints
        assert _column_names(constraints[constraint_name]) == ("id", SUBJECT)

    variants = Base.metadata.tables["genetic_variants"]
    raw_column = variants.c.raw_payload_id
    assert isinstance(raw_column.type, Integer)
    assert raw_column.nullable is True
    raw_foreign_keys = list(raw_column.foreign_keys)
    assert len(raw_foreign_keys) == 1
    assert raw_foreign_keys[0].target_fullname == "raw_payloads.id"
    assert raw_foreign_keys[0].ondelete == "SET NULL"
    raw_index = _index_map(variants)["ix_genetic_variants_raw_payload_id"]
    assert _column_names(raw_index) == ("raw_payload_id",)
    assert raw_index.unique is False


def _normalized_predicate(value: Any) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split()).casefold()


def test_legacy_global_uniques_are_still_present_and_unscoped_in_models():
    for name, (table_name, columns, pg_where, sqlite_where) in (
        LEGACY_GLOBAL_UNIQUES.items()
    ):
        # Revision 0043 deliberately replaces this global key with separate
        # exact-owned and fully-null legacy bridges. The literal remains below
        # because the rest of this file verifies revision 0037 itself.
        if name == "uq_notification_dedupe_key":
            continue
        table = Base.metadata.tables[table_name]
        objects = {
            item.name: item
            for item in (*table.constraints, *table.indexes)
            if item.name is not None
        }
        assert name in objects
        item = objects[name]
        assert _column_names(item) == columns
        assert SUBJECT not in columns

        if isinstance(item, Index):
            assert item.unique is True
            assert _normalized_predicate(
                item.dialect_options["postgresql"]["where"]
            ) == _normalized_predicate(pg_where)
            assert _normalized_predicate(
                item.dialect_options["sqlite"]["where"]
            ) == _normalized_predicate(sqlite_where)
        else:
            assert isinstance(item, UniqueConstraint)
            assert pg_where is None and sqlite_where is None


def _legacy_unique_constraints_for(table_name: str) -> list[UniqueConstraint]:
    constraints: list[UniqueConstraint] = []
    for name, (owner, columns, pg_where, sqlite_where) in (
        LEGACY_GLOBAL_UNIQUES.items()
    ):
        if owner != table_name or pg_where is not None or sqlite_where is not None:
            continue
        # Index-backed uniques are added separately below; these six historical
        # names are actual table constraints.
        if name.startswith("uq_"):
            constraints.append(UniqueConstraint(*columns, name=name))
    return constraints


def _minimal_column(column_name: str) -> sa.Column[Any]:
    if column_name == "id":
        return sa.Column(column_name, sa.Integer(), primary_key=True)
    if column_name in {"date", "start_date", "end_date", "deadline"}:
        return sa.Column(column_name, sa.Date(), nullable=True)
    if column_name in {
        "created_at",
        "next_attempt_at",
        "processed_at",
        "resolved_at",
        "sent_at",
        "ts",
    }:
        return sa.Column(column_name, sa.DateTime(), nullable=True)
    if column_name in {"active", "superseded"}:
        return sa.Column(column_name, sa.Boolean(), nullable=True)
    return sa.Column(column_name, sa.String(160), nullable=True)


def _create_minimal_legacy_tables(connection: sa.Connection) -> None:
    """Create only the pre-0037 columns that the real revision indexes.

    Replaying revisions 0001-0034 on SQLite is not a supported contract because
    historical revisions contain PostgreSQL-only JSONB/ALTER operations.  The
    actual 0035 and 0036 revisions create the identity/tenancy roots; this helper
    supplies an empty 0036-shaped legacy data plane for an isolated batch test.
    """

    required: dict[str, set[str]] = {
        table_name: {"id"} for table_name in EXPECTED_OWNERSHIP_COLUMNS
    }
    for table_name in EXPECTED_INSIGHTS_TABLES:
        required[table_name].update({"date", "domain"})
    for _index_name, (table_name, columns) in EXPECTED_QUERY_INDEXES.items():
        required[table_name].update(
            column_name
            for column_name in columns
            if column_name not in COLUMN_TARGETS
        )
    for _name, (table_name, columns, _pg_where, _sqlite_where) in (
        LEGACY_GLOBAL_UNIQUES.items()
    ):
        required[table_name].update(columns)
    required["weight_logs"].add("superseded")

    metadata = sa.MetaData()
    tables: dict[str, sa.Table] = {}
    for table_name, column_names in required.items():
        ordered_names = ["id", *sorted(column_names - {"id"})]
        tables[table_name] = sa.Table(
            table_name,
            metadata,
            *(_minimal_column(column_name) for column_name in ordered_names),
            *_legacy_unique_constraints_for(table_name),
        )

    # The remaining historical uniques are indexes, including four partial ones.
    for name, (table_name, columns, _pg_where, sqlite_where) in (
        LEGACY_GLOBAL_UNIQUES.items()
    ):
        if any(
            constraint.name == name
            for constraint in tables[table_name].constraints
        ):
            continue
        kwargs: dict[str, Any] = {"unique": True}
        if sqlite_where is not None:
            kwargs["sqlite_where"] = sa.text(sqlite_where)
        Index(
            name,
            *(tables[table_name].c[column_name] for column_name in columns),
            **kwargs,
        )

    metadata.create_all(connection)


def _prepare_minimal_0036(
    connection: sa.Connection, monkeypatch: pytest.MonkeyPatch
) -> Any:
    identity = importlib.import_module(
        "migrations.versions.0035_identity_foundation"
    )
    tenancy = importlib.import_module(
        "migrations.versions.0036_tenancy_roots_and_scoped_settings"
    )
    migration = _migration()
    operations = Operations(MigrationContext.configure(connection))
    monkeypatch.setattr(identity, "op", operations)
    monkeypatch.setattr(tenancy, "op", operations)
    monkeypatch.setattr(migration, "op", operations)

    identity.upgrade()
    tenancy.upgrade()
    _create_minimal_legacy_tables(connection)
    return migration


def _inspector_index_map(
    inspector: sa.Inspector, table_name: str
) -> dict[str, dict[str, Any]]:
    return {
        index["name"]: index
        for index in inspector.get_indexes(table_name)
        if index["name"] is not None
    }


def _inspector_unique_map(
    inspector: sa.Inspector, table_name: str
) -> dict[str, tuple[str, ...]]:
    return {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint["name"] is not None
    }


def _assert_migrated_0037_schema(connection: sa.Connection) -> None:
    inspector = inspect(connection)
    for table_name, columns in EXPECTED_OWNERSHIP_COLUMNS.items():
        migrated_columns = {
            column["name"]: column
            for column in inspector.get_columns(table_name)
        }
        foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        indexes = _inspector_index_map(inspector, table_name)
        for column_name in columns:
            assert migrated_columns[column_name]["nullable"] is True
            fk_name = f"fk_{table_name}_{column_name}"
            assert foreign_keys[fk_name]["constrained_columns"] == [column_name]
            assert foreign_keys[fk_name]["referred_table"] == COLUMN_TARGETS[
                column_name
            ].split(".", 1)[0]
            assert foreign_keys[fk_name]["options"]["ondelete"] == "RESTRICT"
            index_name = f"ix_{table_name}_{column_name}"
            assert indexes[index_name]["column_names"] == [column_name]
            assert indexes[index_name]["unique"] == 0

    for table_name in EXPECTED_INSIGHTS_TABLES:
        indexes = _inspector_index_map(inspector, table_name)
        assert indexes[f"ix_{table_name}_subject_date"]["column_names"] == [
            SUBJECT,
            "date",
        ]
        assert indexes[f"ix_{table_name}_subject_domain_date"][
            "column_names"
        ] == [SUBJECT, "domain", "date"]

    for index_name, (table_name, columns) in EXPECTED_QUERY_INDEXES.items():
        index = _inspector_index_map(inspector, table_name)[index_name]
        assert index["column_names"] == list(columns)
        assert index["unique"] == 0

    for table_name, constraint_name in EXPECTED_PARENT_UNIQUES.items():
        assert _inspector_unique_map(inspector, table_name)[constraint_name] == (
            "id",
            SUBJECT,
        )

    variants_columns = {
        column["name"]: column
        for column in inspector.get_columns("genetic_variants")
    }
    assert variants_columns["raw_payload_id"]["nullable"] is True
    variants_fks = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("genetic_variants")
    }
    raw_fk = variants_fks["fk_genetic_variants_raw_payload_id"]
    assert raw_fk["constrained_columns"] == ["raw_payload_id"]
    assert raw_fk["referred_table"] == "raw_payloads"
    assert raw_fk["options"]["ondelete"] == "SET NULL"
    assert _inspector_index_map(inspector, "genetic_variants")[
        "ix_genetic_variants_raw_payload_id"
    ]["column_names"] == ["raw_payload_id"]


def _assert_legacy_uniques_in_inspector(connection: sa.Connection) -> None:
    inspector = inspect(connection)
    for name, (table_name, columns, _pg_where, _sqlite_where) in (
        LEGACY_GLOBAL_UNIQUES.items()
    ):
        unique_constraints = _inspector_unique_map(inspector, table_name)
        indexes = _inspector_index_map(inspector, table_name)
        if name in unique_constraints:
            assert unique_constraints[name] == columns
        else:
            assert indexes[name]["column_names"] == list(columns)
            assert indexes[name]["unique"] == 1


def test_real_0037_sqlite_upgrade_downgrade_and_reupgrade(
    monkeypatch: pytest.MonkeyPatch,
):
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            migration = _prepare_minimal_0036(connection, monkeypatch)
            assert not connection.scalar(
                sa.text("SELECT count(*) FROM health_subjects")
            )
            tables_at_0036 = set(inspect(connection).get_table_names())
            _assert_legacy_uniques_in_inspector(connection)

            migration.upgrade()
            _assert_migrated_0037_schema(connection)
            _assert_legacy_uniques_in_inspector(connection)

            migration.downgrade()
            assert set(inspect(connection).get_table_names()) == tables_at_0036
            inspector = inspect(connection)
            for table_name, columns in EXPECTED_OWNERSHIP_COLUMNS.items():
                remaining = {
                    column["name"]
                    for column in inspector.get_columns(table_name)
                }
                assert set(columns).isdisjoint(remaining)
            assert "raw_payload_id" not in {
                column["name"]
                for column in inspector.get_columns("genetic_variants")
            }
            for table_name, constraint_name in EXPECTED_PARENT_UNIQUES.items():
                assert constraint_name not in _inspector_unique_map(
                    inspector, table_name
                )
            _assert_legacy_uniques_in_inspector(connection)

            migration.upgrade()
            _assert_migrated_0037_schema(connection)
            _assert_legacy_uniques_in_inspector(connection)
    finally:
        engine.dispose()


@pytest.fixture
def expanded_sqlite_0037(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[sa.Connection, Any]]:
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            migration = _prepare_minimal_0036(connection, monkeypatch)
            migration.upgrade()
            yield connection, migration
    finally:
        engine.dispose()


def _insert_probe(
    connection: sa.Connection,
    *,
    table_name: str,
    row_id: int,
    column_name: str,
    value: Any,
    column_type: sa.types.TypeEngine[Any],
) -> sa.Table:
    probe = sa.table(
        table_name,
        sa.column("id", sa.Integer()),
        sa.column(column_name, column_type),
    )
    connection.execute(
        probe.insert().values(id=row_id, **{column_name: value})
    )
    return probe


def test_downgrade_guard_detects_every_ownership_field_and_genetics_raw_link(
    expanded_sqlite_0037: tuple[sa.Connection, Any],
):
    connection, migration = expanded_sqlite_0037
    row_id = 1
    for table_name, column_names in EXPECTED_OWNERSHIP_COLUMNS.items():
        for column_name in column_names:
            probe = _insert_probe(
                connection,
                table_name=table_name,
                row_id=row_id,
                column_name=column_name,
                value=uuid.uuid4(),
                column_type=sa.Uuid(),
            )
            with pytest.raises(RuntimeError, match=table_name):
                migration._assert_downgrade_is_safe()
            connection.execute(probe.delete().where(probe.c.id == row_id))
            row_id += 1

    variants = _insert_probe(
        connection,
        table_name="genetic_variants",
        row_id=row_id,
        column_name="raw_payload_id",
        value=123,
        column_type=sa.Integer(),
    )
    with pytest.raises(RuntimeError, match="genetic raw provenance"):
        migration._assert_downgrade_is_safe()
    connection.execute(variants.delete().where(variants.c.id == row_id))


def test_public_downgrade_fails_before_mutating_schema_when_data_exists(
    expanded_sqlite_0037: tuple[sa.Connection, Any],
):
    connection, migration = expanded_sqlite_0037
    probe = _insert_probe(
        connection,
        table_name="weight_logs",
        row_id=1,
        column_name=SUBJECT,
        value=uuid.uuid4(),
        column_type=sa.Uuid(),
    )

    with pytest.raises(RuntimeError, match="ownership data exists in weight_logs"):
        migration.downgrade()

    assert SUBJECT in {
        column["name"] for column in inspect(connection).get_columns("weight_logs")
    }
    connection.execute(probe.delete().where(probe.c.id == 1))


@pytest.mark.integration
async def test_postgres_enforces_expanded_subject_foreign_key(db_session):
    row = WeightLog(
        date=date(2099, 1, 1),
        domain="weight",
        source="manual",
        weight_kg=80.0,
        superseded=False,
        subject_id=uuid.uuid4(),
    )
    db_session.add(row)

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
