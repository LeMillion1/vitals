"""Link generated weekly-digest artifacts to exact platform AI invocations.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-20

The nullable shape preserves historical subject-owned OpenRouter provenance.
New platform-funded writers use ``ai_invocation_id`` and leave the legacy
``integration_connection_id`` empty.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DOWNGRADE_REFUSAL = (
    "0041 downgrade refused: weekly_digests.ai_invocation_id contains "
    "AI provenance data"
)


def _assert_no_ai_provenance() -> None:
    weekly_digests = sa.table(
        "weekly_digests",
        sa.column("ai_invocation_id", sa.Uuid()),
    )
    exists = op.get_bind().execute(
        sa.select(weekly_digests.c.ai_invocation_id)
        .where(weekly_digests.c.ai_invocation_id.is_not(None))
        .limit(1)
    ).first()
    if exists is not None:
        raise RuntimeError(_DOWNGRADE_REFUSAL)


def upgrade() -> None:
    op.add_column(
        "weekly_digests",
        sa.Column("ai_invocation_id", sa.Uuid(), nullable=True),
    )
    with op.batch_alter_table("weekly_digests") as batch_op:
        batch_op.create_unique_constraint(
            "uq_weekly_digests_ai_invocation_id",
            ["ai_invocation_id"],
        )
        batch_op.create_foreign_key(
            "fk_weekly_digests_ai_invocation_subject",
            "ai_invocations",
            ["ai_invocation_id", "subject_id"],
            ["id", "subject_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_weekly_digests_ai_invocation_ownership",
            "ai_invocation_id IS NULL OR "
            "(subject_id IS NOT NULL AND integration_connection_id IS NULL)",
        )
    op.create_index(
        "ix_weekly_digests_ai_invocation_id",
        "weekly_digests",
        ["ai_invocation_id"],
        unique=False,
    )


def downgrade() -> None:
    _assert_no_ai_provenance()
    op.drop_index(
        "ix_weekly_digests_ai_invocation_id",
        table_name="weekly_digests",
    )
    with op.batch_alter_table("weekly_digests") as batch_op:
        batch_op.drop_constraint(
            "ck_weekly_digests_ai_invocation_ownership",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_weekly_digests_ai_invocation_subject",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_weekly_digests_ai_invocation_id",
            type_="unique",
        )
        batch_op.drop_column("ai_invocation_id")


__all__ = ["downgrade", "upgrade"]
