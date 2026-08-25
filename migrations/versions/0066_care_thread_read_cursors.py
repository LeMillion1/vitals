"""Track what each care-thread participant has actually read.

Revision ID: 0066
Revises: 0065
Create Date: 2026-08-25

The previous navigation marker counted open conversations, not unread ones.
One cursor per participant makes unread state a durable fact without changing
message history. Existing conversations are backfilled as read at migration
time so an upgrade does not manufacture an inbox full of old work.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: Union[str, None] = "0065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("care_thread_participants") as batch:
        batch.add_column(
            sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True)
        )

    # Deliberate cutover semantics: history that predates the feature is not a
    # newly delivered task. Only messages written after this point may become
    # unread.
    op.execute(
        sa.text(
            "UPDATE care_thread_participants "
            "SET last_read_at = CURRENT_TIMESTAMP "
            "WHERE last_read_at IS NULL"
        )
    )

    with op.batch_alter_table("care_thread_participants") as batch:
        batch.alter_column(
            "last_read_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        )
        batch.create_check_constraint(
            "ck_care_thread_participants_read_cursor",
            "last_read_at >= joined_at",
        )
        batch.create_index(
            "ix_care_thread_participants_user_unread",
            ["user_id", "subject_id", "last_read_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("care_thread_participants") as batch:
        batch.drop_index("ix_care_thread_participants_user_unread")
        batch.drop_constraint(
            "ck_care_thread_participants_read_cursor", type_="check"
        )
        batch.drop_column("last_read_at")
