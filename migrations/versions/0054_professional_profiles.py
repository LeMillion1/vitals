"""Give a professional's claim about themselves a place to live and a lifecycle.

Revision ID: 0054
Revises: 0053
Create Date: 2026-08-23

A role says what kind of thing somebody is.  It has never said whose record they
may reach, and this table does not change that: ``professional_profiles`` holds
a claim — a name, a licence number, a kind — plus the record of an operator
having checked it.  Nothing downstream may read a verified profile as access.

The separation matters more than it looks.  Verification answers a question
about the world outside this installation: is this person a doctor at all.
Consent answers a question about one patient's record.  Conflating them produces
the failure where being verified anywhere means being admitted everywhere, which
is exactly what a health record must not do — so the table deliberately has no
``subject_id`` and no row security.  There is nothing here to isolate, because
there is nothing here that belongs to a patient.

Two states are constrained rather than merely recorded.  ``verified`` requires
both a timestamp and a reviewer, so a claim cannot verify itself.  ``rejected``
and ``suspended`` require a note, because a professional told no is entitled to
know what to fix and the next operator needs to see what the last one concluded.

Downgrade drops the table.  Nothing references it yet.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "professional_profiles",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "verification_status",
            sa.String(16),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("credential_reference", sa.String(200), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "verified_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("user_id", name="uq_professional_profiles_user_id"),
        sa.CheckConstraint(
            "kind IN ('doctor', 'trainer')",
            name="ck_professional_profiles_kind",
        ),
        sa.CheckConstraint(
            "verification_status IN ('unverified', 'pending', 'verified', "
            "'rejected', 'suspended')",
            name="ck_professional_profiles_verification_status",
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0 AND length(display_name) <= 200",
            name="ck_professional_profiles_display_name",
        ),
        sa.CheckConstraint(
            "credential_reference IS NULL OR "
            "(length(trim(credential_reference)) > 0 "
            "AND length(credential_reference) <= 200)",
            name="ck_professional_profiles_credential_reference",
        ),
        sa.CheckConstraint(
            "(verification_status = 'verified' AND verified_at IS NOT NULL "
            "AND verified_by_user_id IS NOT NULL) OR "
            "(verification_status <> 'verified' AND verified_at IS NULL "
            "AND verified_by_user_id IS NULL)",
            name="ck_professional_profiles_verified_state",
        ),
        sa.CheckConstraint(
            "(verification_status IN ('rejected', 'suspended') "
            "AND review_note IS NOT NULL AND length(trim(review_note)) > 0) OR "
            "(verification_status NOT IN ('rejected', 'suspended'))",
            name="ck_professional_profiles_refusal_reason",
        ),
    )
    op.create_index(
        "ix_professional_profiles_status_kind",
        "professional_profiles",
        ["verification_status", "kind"],
    )
    op.create_index(
        "ix_professional_profiles_verified_by",
        "professional_profiles",
        ["verified_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_professional_profiles_verified_by", table_name="professional_profiles"
    )
    op.drop_index(
        "ix_professional_profiles_status_kind", table_name="professional_profiles"
    )
    op.drop_table("professional_profiles")
