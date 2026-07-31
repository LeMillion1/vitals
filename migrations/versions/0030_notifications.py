"""Proactive delivery journal: notifications

Every outgoing message is written here — that record is what makes the daily
budget, the dedupe key and reply-matching real rather than aspirational. The
partial-unique index on ``dedupe_key`` mirrors ``system_alerts``: a second send
of the same keyed message is impossible at the DB level, not just unlikely.

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("dedupe_key", sa.String(128), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("payload", _JSON_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_notification_dedupe_key",
        "notifications",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
        sqlite_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.create_index(
        "ix_notifications_category_sent", "notifications", ["category", "sent_at"]
    )
    op.create_index("ix_notifications_external_id", "notifications", ["external_id"])


def downgrade() -> None:
    op.drop_index("ix_notifications_external_id", table_name="notifications")
    op.drop_index("ix_notifications_category_sent", table_name="notifications")
    op.drop_index("uq_notification_dedupe_key", table_name="notifications")
    op.drop_table("notifications")
