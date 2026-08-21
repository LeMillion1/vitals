"""Install the Stage-5 scoped unique keys beside the legacy global ones.

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-21

Every natural key in the single-user schema is global: one weight per date, one
lab marker per name, one Garmin activity per external id.  None of those is true
of a platform — two people share a date and a marker name, and two accounts of
the same provider share an external id — so a second subject cannot exist while
they hold.

This revision is deliberately purely additive.  Each scoped key is installed
*beside* the legacy global key it will eventually replace, never instead of it:
a scoped key is strictly weaker than the global key it narrows, so installing it
can never reject data the lake already holds, and every legacy reader and writer
keeps working unchanged.  Dropping the global keys is the separately reviewed
Stage-5 cutover's job, after every key-based path has been switched.

Installing is still gated: ``stage5.scoped_key_audit.v1`` must have proved that
no row would collide under a proposed key and that no row is missing the scope
its key depends on, because a scoped unique index over a null scope column keeps
no uniqueness at all for that row.

On PostgreSQL the indexes are built ``CONCURRENTLY`` so the migration never
holds a write lock on a health table, and ``IF NOT EXISTS`` so a re-run after an
interrupted build is safe.  Downgrade drops them transactionally, so a refused
downgrade further down the chain rolls the whole attempt back rather than
leaving the lake half-cut-over.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# table, index, key columns, PostgreSQL predicate, SQLite predicate
SCOPED_UNIQUE_INDEXES: tuple[
    tuple[str, str, tuple[str, ...], Union[str, None], Union[str, None]], ...
] = (
    (
        'body_measurements',
        'uq_body_measurements_subject_date',
        ('subject_id', 'date'),
        None,
        None,
    ),
    (
        'day_context',
        'uq_day_context_subject_date',
        ('subject_id', 'date'),
        None,
        None,
    ),
    (
        'weight_logs',
        'uq_active_weight_per_subject_date',
        ('subject_id', 'date'),
        'superseded = false',
        'superseded = 0',
    ),
    (
        'genetic_variants',
        'uq_genetic_variant_subject_rsid',
        ('subject_id', 'rsid'),
        'rsid IS NOT NULL',
        'rsid IS NOT NULL',
    ),
    (
        'lab_markers',
        'uq_lab_markers_subject_name',
        ('subject_id', 'name'),
        None,
        None,
    ),
    (
        'garmin_daily',
        'uq_garmin_daily_connection_date',
        ('integration_connection_id', 'date'),
        None,
        None,
    ),
    (
        'garmin_activities',
        'uq_garmin_activities_connection_external_id',
        ('integration_connection_id', 'external_id'),
        None,
        None,
    ),
    (
        'hevy_workouts',
        'uq_hevy_workouts_connection_external_id',
        ('integration_connection_id', 'external_id'),
        None,
        None,
    ),
    (
        'garmin_weight_exports',
        'uq_garmin_weight_exports_connection_date',
        ('integration_connection_id', 'date'),
        None,
        None,
    ),
    (
        'hrt_compounds',
        'uq_hrt_compounds_platform_key',
        ('key',),
        'subject_id IS NULL',
        'subject_id IS NULL',
    ),
    (
        'hrt_compounds',
        'uq_hrt_compounds_subject_key',
        ('subject_id', 'key'),
        'subject_id IS NOT NULL',
        'subject_id IS NOT NULL',
    ),
    (
        'conflict_rules',
        'uq_conflict_rules_platform_code',
        ('code',),
        'subject_id IS NULL',
        'subject_id IS NULL',
    ),
    (
        'conflict_rules',
        'uq_conflict_rules_subject_code',
        ('subject_id', 'code'),
        'subject_id IS NOT NULL',
        'subject_id IS NOT NULL',
    ),
    (
        'system_alerts',
        'uq_active_alert_per_connection_key_entity',
        ('integration_connection_id', 'alert_key', 'entity_ref'),
        'resolved_at IS NULL AND integration_connection_id IS NOT NULL',
        'resolved_at IS NULL AND integration_connection_id IS NOT NULL',
    ),
    (
        'system_alerts',
        'uq_active_alert_per_subject_key_entity',
        ('subject_id', 'alert_key', 'entity_ref'),
        'resolved_at IS NULL AND subject_id IS NOT NULL AND integration_connection_id IS NULL',
        'resolved_at IS NULL AND subject_id IS NOT NULL AND integration_connection_id IS NULL',
    ),
    (
        'system_alerts',
        'uq_active_alert_per_platform_key_entity',
        ('alert_key', 'entity_ref'),
        'resolved_at IS NULL AND subject_id IS NULL AND integration_connection_id IS NULL',
        'resolved_at IS NULL AND subject_id IS NULL AND integration_connection_id IS NULL',
    ),
)


def _quoted(columns: tuple[str, ...]) -> str:
    return ", ".join(f'"{column}"' for column in columns)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Outside the migration transaction: CONCURRENTLY cannot run inside one.
        with op.get_context().autocommit_block():
            for table, name, columns, predicate, _ in SCOPED_UNIQUE_INDEXES:
                statement = (
                    f'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "{name}" '
                    f'ON "{table}" ({_quoted(columns)})'
                )
                if predicate is not None:
                    statement += f" WHERE {predicate}"
                op.execute(sa.text(statement))
        return
    for table, name, columns, _, predicate in SCOPED_UNIQUE_INDEXES:
        op.create_index(
            name,
            table,
            list(columns),
            unique=True,
            sqlite_where=sa.text(predicate) if predicate is not None else None,
        )


def downgrade() -> None:
    for table, name, _columns, _pg, _sqlite in reversed(SCOPED_UNIQUE_INDEXES):
        op.drop_index(name, table_name=table)
