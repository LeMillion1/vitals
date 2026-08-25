"""Add separately governed emergency record sessions.

Revision ID: 0078
Revises: 0077
Create Date: 2026-08-25

Break-glass sessions are not support grants.  One holder names one subject and
exact read-only record domains; two other superadmins approve within fifteen
minutes.  Every row is subject-isolated with FORCE RLS.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: Union[str, None] = "0077"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = (
    "break_glass_sessions",
    "break_glass_scopes",
    "break_glass_approvals",
)
SUBJECT_SETTING = "vitals.subject_id"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "break_glass_sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "initiated_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("incident_reference", sa.String(120), nullable=True),
        sa.Column("requested_ttl_minutes", sa.Integer(), nullable=False),
        sa.Column("initiated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "id",
            "subject_id",
            name="uq_break_glass_sessions_id_subject",
        ),
        sa.UniqueConstraint(
            "id",
            "subject_id",
            "initiated_by_user_id",
            name="uq_break_glass_sessions_id_subject_holder",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'expired')",
            name="ck_break_glass_sessions_status",
        ),
        sa.CheckConstraint(
            "requested_ttl_minutes IN (15, 30, 60)",
            name="ck_break_glass_sessions_ttl",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0 AND length(reason) <= 2000",
            name="ck_break_glass_sessions_reason",
        ),
        sa.CheckConstraint(
            "incident_reference IS NULL OR (length(trim(incident_reference)) > 0 "
            "AND length(incident_reference) <= 120)",
            name="ck_break_glass_sessions_incident_reference",
        ),
        sa.CheckConstraint(
            "approval_deadline > initiated_at",
            name="ck_break_glass_sessions_positive_approval_window",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR activated_at IS NOT NULL",
            name="ck_break_glass_sessions_expiry_after_activation",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > activated_at",
            name="ck_break_glass_sessions_positive_access_window",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND activated_at IS NULL AND expires_at IS NULL "
            "AND revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(status = 'active' AND activated_at IS NOT NULL AND expires_at IS NOT NULL "
            "AND revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL) OR "
            "(status = 'expired' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL)",
            name="ck_break_glass_sessions_lifecycle",
        ),
    )
    op.create_index(
        "ix_break_glass_sessions_subject_status_deadline",
        "break_glass_sessions",
        ["subject_id", "status", "approval_deadline"],
    )
    op.create_index(
        "ix_break_glass_sessions_holder_status",
        "break_glass_sessions",
        ["initiated_by_user_id", "status"],
    )

    op.create_table(
        "break_glass_scopes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("session_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(16), nullable=False),
        sa.Column("resource_key", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "subject_id"],
            ["break_glass_sessions.id", "break_glass_sessions.subject_id"],
            name="fk_break_glass_scopes_exact_session",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "session_id", "resource_key", name="uq_break_glass_scopes_session_domain"
        ),
        sa.CheckConstraint(
            "resource_type = 'domain'", name="ck_break_glass_scopes_resource_type"
        ),
        sa.CheckConstraint("action = 'read'", name="ck_break_glass_scopes_action"),
        sa.CheckConstraint(
            "length(trim(resource_key)) > 0 AND length(resource_key) <= 128",
            name="ck_break_glass_scopes_resource_key",
        ),
        sa.CheckConstraint(
            "resource_key NOT LIKE '%*%'", name="ck_break_glass_scopes_no_wildcard"
        ),
    )
    op.create_index(
        "ix_break_glass_scopes_subject_resource",
        "break_glass_scopes",
        ["subject_id", "resource_key"],
    )

    op.create_table(
        "break_glass_approvals",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("session_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "holder_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "approved_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "subject_id", "holder_user_id"],
            [
                "break_glass_sessions.id",
                "break_glass_sessions.subject_id",
                "break_glass_sessions.initiated_by_user_id",
            ],
            name="fk_break_glass_approvals_exact_session",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "session_id",
            "approved_by_user_id",
            name="uq_break_glass_approvals_session_reviewer",
        ),
        sa.CheckConstraint(
            "approved_by_user_id <> holder_user_id",
            name="ck_break_glass_approvals_not_holder",
        ),
    )
    op.create_index(
        "ix_break_glass_approvals_subject_session",
        "break_glass_approvals",
        ["subject_id", "session_id"],
    )

    if op.get_bind().dialect.name == "postgresql":
        for table in SUBJECT_ISOLATED_TABLES:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(
                f'CREATE POLICY "{POLICY_NAME}" ON "{table}" '
                f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in reversed(SUBJECT_ISOLATED_TABLES):
            op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table}"')
            op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.drop_index(
        "ix_break_glass_approvals_subject_session", table_name="break_glass_approvals"
    )
    op.drop_table("break_glass_approvals")
    op.drop_index(
        "ix_break_glass_scopes_subject_resource", table_name="break_glass_scopes"
    )
    op.drop_table("break_glass_scopes")
    op.drop_index(
        "ix_break_glass_sessions_holder_status", table_name="break_glass_sessions"
    )
    op.drop_index(
        "ix_break_glass_sessions_subject_status_deadline",
        table_name="break_glass_sessions",
    )
    op.drop_table("break_glass_sessions")
