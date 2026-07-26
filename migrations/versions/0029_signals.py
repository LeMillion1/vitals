"""Signals capture domain: signals, day_context

``signals`` holds the free-text facts the bot parses out of a message (one phrase
→ 1..N rows sharing a ``batch_id``); ``day_context`` holds one answered/guessed
row per day. Both carry the InsightsMixin triple (date, domain, source).

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False, server_default=sa.text("'signals'")),
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(16), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("at_time", sa.Time(), nullable=True),
        sa.Column("raw_id", sa.Integer(), nullable=True),
        sa.Column("batch_id", sa.String(32), nullable=False),
        sa.Column("misparse", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["raw_id"], ["raw_payloads.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_signals_date", "signals", ["date"])
    op.create_index("ix_signals_domain", "signals", ["domain"])
    op.create_index("ix_signals_domain_date", "signals", ["domain", "date"])
    op.create_index("ix_signals_raw_id", "signals", ["raw_id"])
    op.create_index("ix_signals_batch", "signals", ["batch_id"])
    op.create_index("ix_signals_key_date", "signals", ["key", "date"])

    op.create_table(
        "day_context",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False, server_default=sa.text("'signals'")),
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("answers", _JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("planned", _JSON_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", name="uq_day_context_per_date"),
    )
    op.create_index("ix_day_context_date", "day_context", ["date"])
    op.create_index("ix_day_context_domain", "day_context", ["domain"])
    op.create_index("ix_day_context_domain_date", "day_context", ["domain", "date"])


def downgrade() -> None:
    op.drop_index("ix_day_context_domain_date", table_name="day_context")
    op.drop_index("ix_day_context_domain", table_name="day_context")
    op.drop_index("ix_day_context_date", table_name="day_context")
    op.drop_table("day_context")

    op.drop_index("ix_signals_key_date", table_name="signals")
    op.drop_index("ix_signals_batch", table_name="signals")
    op.drop_index("ix_signals_raw_id", table_name="signals")
    op.drop_index("ix_signals_domain_date", table_name="signals")
    op.drop_index("ix_signals_domain", table_name="signals")
    op.drop_index("ix_signals_date", table_name="signals")
    op.drop_table("signals")
