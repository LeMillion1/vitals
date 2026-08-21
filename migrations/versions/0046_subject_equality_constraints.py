"""Add deferred parent/child subject-equality foreign keys.

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-21

Revision 0038 deliberately postponed these composite references until the bounded
Stage-3 backfill had copied and validated ownership from every parent row.  This
revision adds them, and on PostgreSQL it adds them ``NOT VALID`` so the DDL takes
only a brief lock and never scans the whole lake inside a migration.  Making them
valid is the separately reviewed Stage-4 validation operation's job; an
unvalidated constraint still enforces every new and updated row.

The constraints are pure schema: they hold no data, so downgrade simply drops
them.  They also never fire while either side is NULL, which keeps the
pre-cutover rollback boundary intact for a binary that cannot express S.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# child table -> (constraint, child columns, parent table, parent columns)
SUBJECT_EQUALITY_FOREIGN_KEYS: tuple[
    tuple[str, str, tuple[str, str], str, tuple[str, str]], ...
] = (
    (
        "body_scan_metrics",
        "fk_body_scan_metrics_scan_subject",
        ("scan_id", "subject_id"),
        "body_scans",
        ("id", "subject_id"),
    ),
    (
        "hevy_exercises",
        "fk_hevy_exercises_workout_subject",
        ("workout_id", "subject_id"),
        "hevy_workouts",
        ("id", "subject_id"),
    ),
    (
        "hevy_sets",
        "fk_hevy_sets_exercise_subject",
        ("exercise_id", "subject_id"),
        "hevy_exercises",
        ("id", "subject_id"),
    ),
    (
        "hrt_compound_components",
        "fk_hrt_compound_components_compound_subject",
        ("compound_id", "subject_id"),
        "hrt_compounds",
        ("id", "subject_id"),
    ),
    (
        "hrt_cycle_items",
        "fk_hrt_cycle_items_cycle_subject",
        ("cycle_id", "subject_id"),
        "hrt_cycles",
        ("id", "subject_id"),
    ),
    (
        "hrt_cycle_template_items",
        "fk_hrt_cycle_template_items_template_subject",
        ("template_id", "subject_id"),
        "hrt_cycle_templates",
        ("id", "subject_id"),
    ),
)


def _quote(columns: tuple[str, str]) -> str:
    return ", ".join(f'"{column}"' for column in columns)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for (
            table_name,
            constraint_name,
            child_columns,
            parent_table,
            parent_columns,
        ) in SUBJECT_EQUALITY_FOREIGN_KEYS:
            op.execute(
                f'ALTER TABLE "{table_name}" ADD CONSTRAINT "{constraint_name}" '
                f"FOREIGN KEY ({_quote(child_columns)}) "
                f'REFERENCES "{parent_table}" ({_quote(parent_columns)}) '
                "ON DELETE CASCADE NOT VALID"
            )
        return
    for (
        table_name,
        constraint_name,
        child_columns,
        parent_table,
        parent_columns,
    ) in SUBJECT_EQUALITY_FOREIGN_KEYS:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_foreign_key(
                constraint_name,
                parent_table,
                list(child_columns),
                list(parent_columns),
                ondelete="CASCADE",
            )


def downgrade() -> None:
    for (
        table_name,
        constraint_name,
        _child_columns,
        _parent_table,
        _parent_columns,
    ) in reversed(SUBJECT_EQUALITY_FOREIGN_KEYS):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="foreignkey")
