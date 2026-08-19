"""Expand top-level health data with nullable ownership references.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-19

This is an expand-only schema revision.  It neither guesses the historical
actor nor backfills the legacy subject/connection/file roots.  Existing global
unique constraints and compatibility readers remain until the separately gated
scoped-key cutover.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_S = ("subject_id", "health_subjects")
_A = ("actor_user_id", "users")
_C = ("integration_connection_id", "integration_connections")
_F = ("file_asset_id", "file_assets")
_R = ("recipient_user_id", "users")
_Q = ("requested_by_user_id", "users")
_CB = ("created_by_user_id", "users")
_RB = ("revoked_by_user_id", "users")
_OB = ("overridden_by_user_id", "users")
_RS = ("resolved_by_user_id", "users")

# Dependency-friendly order: raw/root parents precede normalized rows whose
# subject-safe composite references are validated in a later stage.
_OWNERSHIP_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "raw_payloads": (_S, _A, _C, _F),
    "weight_logs": (_S, _A, _C),
    "body_scans": (_S, _A, _F),
    "hevy_workouts": (_S, _A, _C),
    "hrt_compounds": (_S, _A),
    "hrt_cycles": (_S, _A),
    "hrt_cycle_templates": (_S, _A),
    "annotations": (_S, _A),
    "body_measurements": (_S, _A),
    "conflict_rules": (_S,),
    "day_context": (_S, _A, _C),
    "garmin_activities": (_S, _A, _C),
    "garmin_daily": (_S, _A, _C),
    "garmin_intraday": (_S, _C),
    "garmin_weight_exports": (_S, _C, _Q),
    "genetic_variants": (_S, _A),
    "glp1_dose_phases": (_S, _A),
    "glp1_injections": (_S, _A),
    "glp1_side_effects": (_S, _A),
    "hrt_doses": (_S, _A),
    "hrt_side_effects": (_S, _A),
    "lab_markers": (_S, _A),
    "lab_results": (_S, _A),
    "meal_logs": (_S, _A),
    "milestones": (_S, _A),
    "noise_markers": (_S, _A),
    "notifications": (_S, _R, _A, _C),
    "progress_photos": (_S, _A, _F),
    "shared_reports": (_S, _CB, _RB),
    "signals": (_S, _A, _C),
    "skincare_logs": (_S, _A),
    "skincare_observations": (_S, _A),
    "skincare_products": (_S, _A),
    "supplements": (_S, _A),
    "system_alerts": (_S, _C, _OB, _RS),
    "weekly_digests": (_S, _A, _C),
}

_PARENT_UNIQUES = {
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

_INSIGHTS_TABLES = (
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

_QUERY_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ix_annotations_subject_date_range": (
        "annotations",
        ("subject_id", "date", "end_date"),
    ),
    "ix_noise_markers_subject_domain_range": (
        "noise_markers",
        ("subject_id", "domain", "start_date", "end_date"),
    ),
    "ix_glp1_dose_phases_subject_domain_range": (
        "glp1_dose_phases",
        ("subject_id", "domain", "start_date", "end_date"),
    ),
    "ix_hrt_cycles_subject_domain_range": (
        "hrt_cycles",
        ("subject_id", "domain", "start_date", "end_date"),
    ),
    "ix_garmin_activities_connection_external": (
        "garmin_activities",
        ("integration_connection_id", "external_id"),
    ),
    "ix_garmin_daily_connection_date": (
        "garmin_daily",
        ("integration_connection_id", "date"),
    ),
    "ix_garmin_intraday_connection_series_date": (
        "garmin_intraday",
        ("integration_connection_id", "series_type", "date"),
    ),
    "ix_garmin_intraday_connection_date_ts": (
        "garmin_intraday",
        ("integration_connection_id", "date", "ts"),
    ),
    "ix_garmin_weight_exports_subject_date": (
        "garmin_weight_exports",
        ("subject_id", "date"),
    ),
    "ix_garmin_weight_exports_connection_date": (
        "garmin_weight_exports",
        ("integration_connection_id", "date"),
    ),
    "ix_garmin_weight_exports_connection_status_next": (
        "garmin_weight_exports",
        ("integration_connection_id", "status", "next_attempt_at"),
    ),
    "ix_hevy_workouts_connection_external": (
        "hevy_workouts",
        ("integration_connection_id", "external_id"),
    ),
    "ix_genetic_variants_subject_rsid": (
        "genetic_variants",
        ("subject_id", "rsid"),
    ),
    "ix_genetic_variants_subject_marker": (
        "genetic_variants",
        ("subject_id", "marker"),
    ),
    "ix_hrt_compounds_subject_key": (
        "hrt_compounds",
        ("subject_id", "key"),
    ),
    "ix_hrt_compounds_subject_active": (
        "hrt_compounds",
        ("subject_id", "active"),
    ),
    "ix_hrt_cycle_templates_subject_name": (
        "hrt_cycle_templates",
        ("subject_id", "name"),
    ),
    "ix_hrt_doses_subject_compound_date": (
        "hrt_doses",
        ("subject_id", "compound_key", "date"),
    ),
    "ix_lab_markers_subject_name": (
        "lab_markers",
        ("subject_id", "name"),
    ),
    "ix_lab_results_subject_marker_date": (
        "lab_results",
        ("subject_id", "marker", "date"),
    ),
    "ix_milestones_subject_status": (
        "milestones",
        ("subject_id", "status"),
    ),
    "ix_milestones_subject_deadline": (
        "milestones",
        ("subject_id", "deadline"),
    ),
    "ix_notifications_subject_sent": (
        "notifications",
        ("subject_id", "sent_at"),
    ),
    "ix_notifications_recipient_subject_category_sent": (
        "notifications",
        ("recipient_user_id", "subject_id", "category", "sent_at"),
    ),
    "ix_notifications_connection_recipient_dedupe": (
        "notifications",
        ("integration_connection_id", "recipient_user_id", "dedupe_key"),
    ),
    "ix_notifications_connection_recipient_external": (
        "notifications",
        ("integration_connection_id", "recipient_user_id", "external_id"),
    ),
    "ix_raw_payloads_subject_domain_processed": (
        "raw_payloads",
        ("subject_id", "domain", "processed_at"),
    ),
    "ix_raw_payloads_connection_domain_processed": (
        "raw_payloads",
        ("integration_connection_id", "domain", "processed_at"),
    ),
    "ix_raw_payloads_subject_domain_source_external": (
        "raw_payloads",
        ("subject_id", "domain", "source", "external_id"),
    ),
    "ix_raw_payloads_connection_domain_source_external": (
        "raw_payloads",
        ("integration_connection_id", "domain", "source", "external_id"),
    ),
    "ix_shared_reports_subject_created": (
        "shared_reports",
        ("subject_id", "created_at"),
    ),
    "ix_signals_subject_batch": ("signals", ("subject_id", "batch_id")),
    "ix_signals_subject_key_date": (
        "signals",
        ("subject_id", "key", "date"),
    ),
    "ix_signals_connection_batch": (
        "signals",
        ("integration_connection_id", "batch_id"),
    ),
    "ix_skincare_products_subject_name": (
        "skincare_products",
        ("subject_id", "name"),
    ),
    "ix_skincare_products_subject_type": (
        "skincare_products",
        ("subject_id", "type"),
    ),
    "ix_skincare_products_subject_active": (
        "skincare_products",
        ("subject_id", "active"),
    ),
    "ix_supplements_subject_key": (
        "supplements",
        ("subject_id", "key"),
    ),
    "ix_supplements_subject_active": (
        "supplements",
        ("subject_id", "active"),
    ),
    "ix_system_alerts_subject_domain_resolved": (
        "system_alerts",
        ("subject_id", "domain", "resolved_at"),
    ),
    "ix_system_alerts_connection_domain_resolved": (
        "system_alerts",
        ("integration_connection_id", "domain", "resolved_at"),
    ),
    "ix_weekly_digests_subject_kind_date": (
        "weekly_digests",
        ("subject_id", "kind", "date"),
    ),
    "ix_weight_logs_connection_date": (
        "weight_logs",
        ("integration_connection_id", "date"),
    ),
}


def _expand_table(
    table_name: str,
    columns: tuple[tuple[str, str], ...],
) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        for column_name, _target_table in columns:
            batch_op.add_column(sa.Column(column_name, sa.Uuid(), nullable=True))
        for column_name, target_table in columns:
            batch_op.create_foreign_key(
                f"fk_{table_name}_{column_name}",
                target_table,
                [column_name],
                ["id"],
                ondelete="RESTRICT",
            )
        unique_name = _PARENT_UNIQUES.get(table_name)
        if unique_name is not None:
            batch_op.create_unique_constraint(unique_name, ["id", "subject_id"])

    for column_name, _target_table in columns:
        op.create_index(
            f"ix_{table_name}_{column_name}",
            table_name,
            [column_name],
            unique=False,
        )


def upgrade() -> None:
    # The two Stage-0 roots already have subject_id; add only the composite keys
    # needed by future subject-equality foreign keys.
    for table_name in ("integration_connections", "file_assets"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_unique_constraint(
                _PARENT_UNIQUES[table_name], ["id", "subject_id"]
            )

    for table_name, columns in _OWNERSHIP_COLUMNS.items():
        _expand_table(table_name, columns)

    # VCF imports were raw-first but historically left no durable link from the
    # interpreted variant.  The nullable link is expanded now and populated only
    # by the later dual-write/backfill operation.
    with op.batch_alter_table("genetic_variants") as batch_op:
        batch_op.add_column(sa.Column("raw_payload_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_genetic_variants_raw_payload_id",
            "raw_payloads",
            ["raw_payload_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_genetic_variants_raw_payload_id",
        "genetic_variants",
        ["raw_payload_id"],
        unique=False,
    )

    for table_name in _INSIGHTS_TABLES:
        op.create_index(
            f"ix_{table_name}_subject_date",
            table_name,
            ["subject_id", "date"],
            unique=False,
        )
        op.create_index(
            f"ix_{table_name}_subject_domain_date",
            table_name,
            ["subject_id", "domain", "date"],
            unique=False,
        )
    for index_name, (table_name, columns) in _QUERY_INDEXES.items():
        op.create_index(index_name, table_name, list(columns), unique=False)


def _assert_downgrade_is_safe() -> None:
    bind = op.get_bind()
    for table_name, columns in _OWNERSHIP_COLUMNS.items():
        probe = sa.table(
            table_name,
            *(sa.column(column_name) for column_name, _target in columns),
        )
        predicate = sa.or_(
            *(probe.c[column_name].is_not(None) for column_name, _target in columns)
        )
        if bind.execute(
            sa.select(sa.func.count()).select_from(probe).where(predicate)
        ).scalar_one():
            raise RuntimeError(
                "0037 downgrade refused: ownership data exists in " + table_name
            )

    variants = sa.table("genetic_variants", sa.column("raw_payload_id"))
    if bind.execute(
        sa.select(sa.func.count())
        .select_from(variants)
        .where(variants.c.raw_payload_id.is_not(None))
    ).scalar_one():
        raise RuntimeError(
            "0037 downgrade refused: genetic raw provenance links exist"
        )


def downgrade() -> None:
    _assert_downgrade_is_safe()

    for index_name, (table_name, _columns) in reversed(_QUERY_INDEXES.items()):
        op.drop_index(index_name, table_name=table_name)
    for table_name in reversed(_INSIGHTS_TABLES):
        op.drop_index(
            f"ix_{table_name}_subject_domain_date", table_name=table_name
        )
        op.drop_index(f"ix_{table_name}_subject_date", table_name=table_name)

    op.drop_index(
        "ix_genetic_variants_raw_payload_id", table_name="genetic_variants"
    )
    with op.batch_alter_table("genetic_variants") as batch_op:
        batch_op.drop_constraint(
            "fk_genetic_variants_raw_payload_id", type_="foreignkey"
        )
        batch_op.drop_column("raw_payload_id")

    for table_name, columns in reversed(_OWNERSHIP_COLUMNS.items()):
        for column_name, _target_table in reversed(columns):
            op.drop_index(
                f"ix_{table_name}_{column_name}", table_name=table_name
            )
        with op.batch_alter_table(table_name) as batch_op:
            unique_name = _PARENT_UNIQUES.get(table_name)
            if unique_name is not None:
                batch_op.drop_constraint(unique_name, type_="unique")
            for column_name, _target_table in reversed(columns):
                batch_op.drop_constraint(
                    f"fk_{table_name}_{column_name}", type_="foreignkey"
                )
                batch_op.drop_column(column_name)

    for table_name in ("file_assets", "integration_connections"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(
                _PARENT_UNIQUES[table_name], type_="unique"
            )
