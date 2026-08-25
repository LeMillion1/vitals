"""Detach terminal support repair history from replaced personal facts.

Revision ID: 0080
Revises: 0079
Create Date: 2026-08-25

Portability replace must be able to remove an old body measurement without
discarding the governed support-repair history that referred to it.  Preserve
the measurement date on the receipt, let the exact target FK become nullable,
and continue to require a live target for open actions.  The replacement
preflight explicitly detaches terminal rows before it deletes old facts; the
composite FK remains ``RESTRICT`` so a missed preflight cannot erase history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0080"
down_revision: Union[str, None] = "0079"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "support_repair_actions"
FK_NAME = "fk_support_repair_actions_exact_measurement"
CHECK_NAME = "ck_support_repair_actions_open_target"


def _set_force_rls(enabled: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    mode = "FORCE" if enabled else "NO FORCE"
    op.execute(f'ALTER TABLE "{TABLE_NAME}" {mode} ROW LEVEL SECURITY')


def _null_count(column: str) -> int:
    value = op.get_bind().scalar(
        sa.text(f'SELECT count(*) FROM "{TABLE_NAME}" WHERE "{column}" IS NULL')
    )
    return int(value or 0)


def upgrade() -> None:
    _set_force_rls(False)
    op.add_column(TABLE_NAME, sa.Column("target_date", sa.Date(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE support_repair_actions "
            "SET target_date = ("
            "SELECT body_measurements.date FROM body_measurements "
            "WHERE body_measurements.id = "
            "support_repair_actions.target_body_measurement_id "
            "AND body_measurements.subject_id = support_repair_actions.subject_id"
            ")"
        )
    )
    if _null_count("target_date"):
        raise RuntimeError("support repair target date backfill is incomplete")
    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.drop_constraint(FK_NAME, type_="foreignkey")
        batch.alter_column(
            "target_body_measurement_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch.alter_column(
            "target_date",
            existing_type=sa.Date(),
            nullable=False,
        )
        batch.create_foreign_key(
            FK_NAME,
            "body_measurements",
            ["target_body_measurement_id", "subject_id"],
            ["id", "subject_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            CHECK_NAME,
            "status NOT IN ('proposed', 'approved') OR target_body_measurement_id IS NOT NULL",
        )
    _set_force_rls(True)


def downgrade() -> None:
    _set_force_rls(False)
    if _null_count("target_body_measurement_id"):
        raise RuntimeError(
            "cannot downgrade detached support repair history without its old target"
        )
    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.drop_constraint(CHECK_NAME, type_="check")
        batch.drop_constraint(FK_NAME, type_="foreignkey")
        batch.alter_column(
            "target_body_measurement_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.create_foreign_key(
            FK_NAME,
            "body_measurements",
            ["target_body_measurement_id", "subject_id"],
            ["id", "subject_id"],
            ondelete="RESTRICT",
        )
        batch.drop_column("target_date")
    _set_force_rls(True)


__all__ = ["downgrade", "upgrade"]
