"""One patient's offer to let one professional into their record.

Revision ID: 0055
Revises: 0054
Create Date: 2026-08-23

The offer travels as a token in a link, and only the token's hash is stored, so
a copy of this table is not a set of working invitations.  The published-report
tokens work the same way; the reason is stronger here, because accepting one of
these creates a care relationship rather than opening a document.

It binds to an address as well as to a token.  A link anybody holding it may
accept is a link whoever it was forwarded to may accept, and the patient chose a
person rather than a mailbox.  The address is matched against a *verified* claim
at acceptance — an unverified address is somebody asserting they own a mailbox,
which is precisely what the binding exists to stop.

``subject_id`` is present because the invitation is the patient's: it is their
record being offered.  That puts it inside row security, and the policy carries
the same two clauses every other one has since revision 0053 — the bound subject,
or the platform scope.  The platform clause is not decoration here: accepting is
done by somebody who is not bound to this subject yet, and the token is what
authorizes reading the row at all.

Downgrade drops the policy with the table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Covered by row security by this revision. Named so the contract test can read
#: it rather than repeat it.
SUBJECT_ISOLATED_TABLES: tuple[str, ...] = ("professional_invitations",)

SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"(subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid "
    f"OR current_setting('{PLATFORM_SETTING}', true) = 'on')"
)


def upgrade() -> None:
    op.create_table(
        "professional_invitations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invited_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("invited_email", sa.String(320), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending"
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accepted_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
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
            "token_hash", name="uq_professional_invitations_token_hash"
        ),
        sa.CheckConstraint(
            "kind IN ('doctor', 'trainer')",
            name="ck_professional_invitations_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'revoked', 'expired')",
            name="ck_professional_invitations_status",
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64 AND lower(token_hash) = token_hash",
            name="ck_professional_invitations_token_hash_shape",
        ),
        sa.CheckConstraint(
            "length(trim(invited_email)) > 0 AND length(invited_email) <= 320",
            name="ck_professional_invitations_invited_email",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_professional_invitations_positive_ttl",
        ),
        sa.CheckConstraint(
            "(status = 'accepted' AND accepted_at IS NOT NULL "
            "AND accepted_by_user_id IS NOT NULL) OR "
            "(status <> 'accepted' AND accepted_at IS NULL "
            "AND accepted_by_user_id IS NULL)",
            name="ck_professional_invitations_accepted_state",
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_professional_invitations_revoked_state",
        ),
    )
    op.create_index(
        "ix_professional_invitations_subject_status",
        "professional_invitations",
        ["subject_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_professional_invitations_email_status",
        "professional_invitations",
        ["invited_email", "status"],
    )
    op.create_index(
        "ix_professional_invitations_accepted_by",
        "professional_invitations",
        ["accepted_by_user_id"],
    )

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
    op.drop_index(
        "ix_professional_invitations_accepted_by",
        table_name="professional_invitations",
    )
    op.drop_index(
        "ix_professional_invitations_email_status",
        table_name="professional_invitations",
    )
    op.drop_index(
        "ix_professional_invitations_subject_status",
        table_name="professional_invitations",
    )
    op.drop_table("professional_invitations")
