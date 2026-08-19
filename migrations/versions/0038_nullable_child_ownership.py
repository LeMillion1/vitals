"""Expand inherited child rows with nullable ownership references.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-19

This is a DDL-only compatibility expansion.  It preserves every legacy parent
foreign key and does not yet add subject-equality composite foreign keys.  The
new references remain nullable until the separately gated backfill has copied
and validated ownership from each parent row.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_S = ("subject_id", "health_subjects")
_C = ("integration_connection_id", "integration_connections")

_OWNERSHIP_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "body_scan_metrics": (_S,),
    "hevy_exercises": (_S, _C),
    "hevy_sets": (_S, _C),
    "hrt_compound_components": (_S,),
    "hrt_cycle_items": (_S,),
    "hrt_cycle_template_items": (_S,),
}

_PARENT_UNIQUES = {
    "hevy_exercises": "uq_hevy_exercises_id_subject",
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
    for table_name, columns in _OWNERSHIP_COLUMNS.items():
        _expand_table(table_name, columns)


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
                "0038 downgrade refused: child ownership data exists in "
                + table_name
            )


def downgrade() -> None:
    _assert_downgrade_is_safe()

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
