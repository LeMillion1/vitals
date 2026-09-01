"""Add PII-free, short-lived open-registration intents.

Revision ID: 0085
Revises: 0084
Create Date: 2026-09-02

An intent records only an opaque UUID and the non-privileged account kind a
browser selected before OIDC begins. Provider claims, email addresses, names,
and application user identifiers do not belong in this handoff state.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0085"
down_revision: Union[str, None] = "0084"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "registration_intents",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("account_kind", sa.String(16), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "account_kind IN ('member', 'doctor', 'trainer')",
            name="ck_registration_intents_account_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'consumed', 'expired')",
            name="ck_registration_intents_status",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_registration_intents_expiry",
        ),
        sa.CheckConstraint(
            "(status = 'pending' "
            "AND consumed_at IS NULL AND expired_at IS NULL) OR "
            "(status = 'consumed' "
            "AND consumed_at IS NOT NULL AND consumed_at >= created_at "
            "AND expired_at IS NULL) OR "
            "(status = 'expired' "
            "AND consumed_at IS NULL "
            "AND expired_at IS NOT NULL AND expired_at >= expires_at)",
            name="ck_registration_intents_state",
        ),
    )
    op.create_index(
        "ix_registration_intents_status_expiry",
        "registration_intents",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_registration_intents_status_expiry",
        table_name="registration_intents",
    )
    op.drop_table("registration_intents")
