"""Keep every professional verification decision as immutable history.

Revision ID: 0071
Revises: 0070
Create Date: 2026-08-25

The profile row is current state: a corrected rejection clears its current
reason, and reinstatement replaces a suspension.  That is right for the
professional's home and insufficient for identity governance.  This table
keeps the exact transition, reviewer and bounded reason that existed when the
operator decided, without putting those personal details into the operational
audit envelope.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: Union[str, None] = "0070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "professional_review_decisions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("professional_profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(16), nullable=False),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_status IN ('unverified', 'pending', 'verified', "
            "'rejected', 'suspended')",
            name="ck_professional_review_decisions_from_status",
        ),
        sa.CheckConstraint(
            "to_status IN ('unverified', 'pending', 'verified', "
            "'rejected', 'suspended')",
            name="ck_professional_review_decisions_to_status",
        ),
        sa.CheckConstraint(
            "(from_status = 'pending' AND to_status IN ('verified', 'rejected')) "
            "OR (from_status = 'verified' AND to_status = 'suspended') "
            "OR (from_status = 'suspended' AND to_status = 'verified')",
            name="ck_professional_review_decisions_transition",
        ),
        sa.CheckConstraint(
            "(to_status IN ('rejected', 'suspended') "
            "AND note IS NOT NULL AND length(trim(note)) > 0 "
            "AND length(note) <= 2000) OR "
            "(to_status NOT IN ('rejected', 'suspended') AND note IS NULL)",
            name="ck_professional_review_decisions_note",
        ),
    )
    op.create_index(
        "ix_professional_review_decisions_profile_created",
        "professional_review_decisions",
        ["profile_id", "created_at"],
    )
    op.create_index(
        "ix_professional_review_decisions_reviewer_created",
        "professional_review_decisions",
        ["reviewer_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_professional_review_decisions_reviewer_created",
        table_name="professional_review_decisions",
    )
    op.drop_index(
        "ix_professional_review_decisions_profile_created",
        table_name="professional_review_decisions",
    )
    op.drop_table("professional_review_decisions")
