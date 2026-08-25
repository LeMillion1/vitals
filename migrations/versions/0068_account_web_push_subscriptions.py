"""Add encrypted account-scoped browser push subscriptions.

Revision ID: 0068
Revises: 0067
Create Date: 2026-08-25

The table deliberately has no ``subject_id``: a browser endpoint belongs to an
account/device and a professional uses the same endpoint across many patient
records.  Endpoint URL and key material are one authenticated ciphertext; only
an opaque SHA-256 lookup digest is stored in the clear.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0068"
down_revision: Union[str, None] = "0067"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "web_push_subscriptions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint_hash", sa.String(64), nullable=False),
        sa.Column("key_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "id", "user_id", name="uq_web_push_subscriptions_id_user"
        ),
        sa.UniqueConstraint(
            "endpoint_hash", name="uq_web_push_subscriptions_endpoint_hash"
        ),
        sa.CheckConstraint(
            "length(endpoint_hash) = 64 AND endpoint_hash = lower(endpoint_hash)",
            name="ck_web_push_subscriptions_endpoint_hash",
        ),
        sa.CheckConstraint(
            "key_version >= 1", name="ck_web_push_subscriptions_key_version"
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND ciphertext IS NOT NULL "
            "AND length(ciphertext) > 0) OR "
            "(revoked_at IS NOT NULL AND ciphertext IS NULL)",
            name="ck_web_push_subscriptions_ciphertext_lifecycle",
        ),
        sa.CheckConstraint(
            "last_success_at IS NULL OR revoked_at IS NULL",
            name="ck_web_push_subscriptions_success_active",
        ),
    )
    op.create_index(
        "ix_web_push_subscriptions_user_active",
        "web_push_subscriptions",
        ["user_id", "revoked_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_web_push_subscriptions_user_active",
        table_name="web_push_subscriptions",
    )
    op.drop_table("web_push_subscriptions")
