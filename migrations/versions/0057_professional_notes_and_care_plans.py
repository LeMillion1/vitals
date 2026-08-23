"""Where a professional's contribution goes, which is not into the patient's facts.

Revision ID: 0057
Revises: 0056
Create Date: 2026-08-23

A doctor's reading of a lab panel is not the lab panel.  Storing it there would
make the two indistinguishable a year later, and it would give a professional a
reason to be able to write into somebody else's measurements — which is the one
thing the default scopes are built to prevent.  These two tables are where the
contribution goes instead.

Each row carries three references and each answers a different question.
``subject_id`` is whose record it sits in, which is what row security reads.
``actor_user_id`` is who wrote it, which is what stops a second professional
editing it.  ``relationship_id`` is the care it was written under, which is what
makes it reviewable later; a note with no relationship behind it is one nobody
can say was authorized.

The relationship is deliberately not the only reference.  Care ends and the note
does not — a record that became unreadable when care ended would be exactly the
record a patient needs after care ends, which is why the foreign key is
``RESTRICT`` and the reads are by subject.

Neither table has a delete path.  A clinical note somebody can make disappear is
a worse record than one that stays and is superseded, and a plan that can vanish
is one the patient cannot hold anybody to.  A plan is archived instead.

Downgrade drops both, and their policies with them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = ("professional_notes", "care_plans")

SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"(subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid "
    f"OR current_setting('{PLATFORM_SETTING}', true) = 'on')"
)


def _authored_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "relationship_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("care_relationships.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
    ]


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "professional_notes",
        *_authored_columns(),
        sa.Column("body", sa.Text(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "length(trim(body)) > 0 AND length(body) <= 20000",
            name="ck_professional_notes_body",
        ),
    )
    op.create_index(
        "ix_professional_notes_subject_created",
        "professional_notes",
        ["subject_id", "created_at"],
    )
    op.create_index(
        "ix_professional_notes_author_created",
        "professional_notes",
        ["actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_professional_notes_relationship",
        "professional_notes",
        ["relationship_id"],
    )

    op.create_table(
        "care_plans",
        *_authored_columns(),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')", name="ck_care_plans_status"
        ),
        sa.CheckConstraint(
            "length(trim(title)) > 0 AND length(title) <= 200",
            name="ck_care_plans_title",
        ),
        sa.CheckConstraint(
            "length(trim(body)) > 0 AND length(body) <= 20000",
            name="ck_care_plans_body",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_care_plans_effective_range",
        ),
    )
    op.create_index(
        "ix_care_plans_subject_status", "care_plans", ["subject_id", "status"]
    )
    op.create_index(
        "ix_care_plans_author_created", "care_plans", ["actor_user_id", "created_at"]
    )
    op.create_index("ix_care_plans_relationship", "care_plans", ["relationship_id"])

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table_name in SUBJECT_ISOLATED_TABLES:
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "{table_name}" '
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table_name in SUBJECT_ISOLATED_TABLES:
            op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')
    op.drop_index("ix_care_plans_relationship", table_name="care_plans")
    op.drop_index("ix_care_plans_author_created", table_name="care_plans")
    op.drop_index("ix_care_plans_subject_status", table_name="care_plans")
    op.drop_table("care_plans")
    op.drop_index(
        "ix_professional_notes_relationship", table_name="professional_notes"
    )
    op.drop_index(
        "ix_professional_notes_author_created", table_name="professional_notes"
    )
    op.drop_index(
        "ix_professional_notes_subject_created", table_name="professional_notes"
    )
    op.drop_table("professional_notes")
