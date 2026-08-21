"""Drop the legacy global keys the scoped keys replaced.

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-21

Revision 0047 installed each scoped key beside the installation-wide key it
narrows, and every key-based write path now resolves inside its own scope.  This
revision removes the global keys themselves.  It is the point at which two
people may share a weigh-in date, a lab-marker name, an rsID, and a day, and two
accounts of one provider may share an external id — which is exactly what a
second subject needs and what the global keys made impossible.

It is gated on ``stage5.scoped_key_audit.v1``: the audit proves that every row
carries the scope its key depends on, because a scoped unique index over a null
scope column keeps no uniqueness at all for that row, and dropping the global
key would then leave it with none.

The expansion indexes the scoped keys duplicate are deliberately kept: revision
0037's contract still describes them, and a duplicated index is a smaller cost
than a schema contract that no longer matches the models.  Downgrade recreates every dropped object, but it can only succeed while the data
still satisfies the global keys — once a second subject has written a duplicate
of a legacy global key, this revision is a one-way boundary and recovery is a
verified backup plus a forward fix.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# table, name, kind, columns, PostgreSQL predicate, SQLite predicate
LEGACY_GLOBAL_KEYS: tuple[
    tuple[str, str, str, tuple[str, ...], Union[str, None], Union[str, None]], ...
] = (
    (
        'body_measurements',
        'uq_body_measurement_per_date',
        'unique_constraint',
        ('date',),
        None,
        None,
    ),
    (
        'day_context',
        'uq_day_context_per_date',
        'unique_constraint',
        ('date',),
        None,
        None,
    ),
    (
        'weight_logs',
        'uq_active_weight_per_date',
        'unique_index',
        ('date',),
        'superseded = false',
        'superseded = 0',
    ),
    (
        'genetic_variants',
        'uq_genetic_variant_rsid',
        'unique_index',
        ('rsid',),
        'rsid IS NOT NULL',
        'rsid IS NOT NULL',
    ),
    (
        'lab_markers',
        'ix_lab_markers_name',
        'unique_index',
        ('name',),
        None,
        None,
    ),
    (
        'garmin_daily',
        'uq_garmin_daily_date',
        'unique_constraint',
        ('date',),
        None,
        None,
    ),
    (
        'garmin_activities',
        'uq_garmin_activities_external_id',
        'unique_constraint',
        ('external_id',),
        None,
        None,
    ),
    (
        'hevy_workouts',
        'uq_hevy_workouts_external_id',
        'unique_constraint',
        ('external_id',),
        None,
        None,
    ),
    (
        'garmin_weight_exports',
        'uq_garmin_weight_exports_date',
        'unique_constraint',
        ('date',),
        None,
        None,
    ),
    (
        'hrt_compounds',
        'ix_hrt_compounds_key',
        'unique_index',
        ('key',),
        None,
        None,
    ),
    (
        'conflict_rules',
        'ix_conflict_rules_code',
        'unique_index',
        ('code',),
        None,
        None,
    ),
    (
        'system_alerts',
        'uq_active_alert_per_key_entity',
        'unique_index',
        ('alert_key', 'entity_ref'),
        'resolved_at IS NULL',
        'resolved_at IS NULL',
    ),
)

# Dismissal history is read by (key, entity) across every alert state, which the
# unresolved-only scoped keys cannot serve once the global key is gone.
ALERT_HISTORY_INDEX = (
    "system_alerts",
    "ix_system_alerts_key_entity",
    ("alert_key", "entity_ref"),
)


def upgrade() -> None:
    bind = op.get_bind()
    table, name, columns = ALERT_HISTORY_INDEX
    op.create_index(name, table, list(columns))
    for table, name, kind, _columns, _pg, _sqlite in reversed(LEGACY_GLOBAL_KEYS):
        if kind == "unique_index":
            op.drop_index(name, table_name=table)
        elif bind.dialect.name == "sqlite":
            # SQLite cannot drop a table constraint in place.
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(name, type_="unique")
        else:
            op.drop_constraint(name, table_name=table, type_="unique")


def downgrade() -> None:
    bind = op.get_bind()
    for table, name, kind, columns, pg_where, sqlite_where in LEGACY_GLOBAL_KEYS:
        if kind == "unique_index":
            where = pg_where if bind.dialect.name == "postgresql" else sqlite_where
            op.create_index(
                name,
                table,
                list(columns),
                unique=True,
                postgresql_where=sa.text(pg_where) if pg_where is not None else None,
                sqlite_where=(
                    sa.text(sqlite_where) if sqlite_where is not None else None
                ),
            )
        elif bind.dialect.name == "sqlite":
            with op.batch_alter_table(table) as batch:
                batch.create_unique_constraint(name, list(columns))
        else:
            op.create_unique_constraint(name, table, list(columns))
    table, name, _columns = ALERT_HISTORY_INDEX
    op.drop_index(name, table_name=table)
