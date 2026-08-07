"""shared_reports — password-protected documents for a doctor

One row per published document: the frozen snapshot, who may open it (bcrypt
password) and for how long. The token is unique and indexed because it is the
only lookup the public route ever performs.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "shared_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(60), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("preset", sa.String(32), nullable=True),
        sa.Column("domains", _JSON_TYPE, nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "labs_flagged_only", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("snapshot", _JSON_TYPE, nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("opened_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_opened_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique *and* the lookup index: the public route resolves a report by token
    # and nothing else, and two rows sharing one token would be two documents at
    # one address.
    op.create_index("ix_shared_reports_token", "shared_reports", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_shared_reports_token", table_name="shared_reports")
    op.drop_table("shared_reports")
