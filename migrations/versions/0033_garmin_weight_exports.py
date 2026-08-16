"""Garmin weight export outbox.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "garmin_weight_exports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("weight_log_id", sa.Integer(), nullable=True),
        sa.Column("weight_kg", sa.Float(), nullable=False),
        sa.Column("measured_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("exported_at", sa.DateTime(), nullable=True),
        sa.Column("remote_sample_pk", sa.String(64), nullable=True),
        sa.Column("remote_weight_kg", sa.Float(), nullable=True),
        sa.Column(
            "remote_owned", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "weight_kg > 0", name="ck_garmin_weight_exports_weight_positive"
        ),
        sa.ForeignKeyConstraint(
            ["weight_log_id"], ["weight_logs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", name="uq_garmin_weight_exports_date"),
    )
    op.create_index(
        "ix_garmin_weight_exports_weight_log_id",
        "garmin_weight_exports",
        ["weight_log_id"],
        unique=False,
    )
    op.create_index(
        "ix_garmin_weight_exports_status_next",
        "garmin_weight_exports",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_garmin_weight_exports_status_next", table_name="garmin_weight_exports"
    )
    op.drop_index(
        "ix_garmin_weight_exports_weight_log_id", table_name="garmin_weight_exports"
    )
    op.drop_table("garmin_weight_exports")
