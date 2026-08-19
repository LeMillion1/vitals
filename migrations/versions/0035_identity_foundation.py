"""Identity, health-subject ownership, support access, and audit foundation.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-19

This revision creates authorization primitives only. It does not migrate the
legacy environment-backed login, open registration, or attach existing health
facts to a subject yet.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_TYPE = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("normalized_username", sa.String(128), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("normalized_email", sa.String(320), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending"
        ),
        sa.Column(
            "session_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(trim(username)) > 0", name="ck_users_username_not_blank"
        ),
        sa.CheckConstraint(
            "length(trim(normalized_username)) > 0",
            name="ck_users_normalized_username_not_blank",
        ),
        sa.CheckConstraint(
            "(email IS NULL AND normalized_email IS NULL) OR "
            "(email IS NOT NULL AND normalized_email IS NOT NULL)",
            name="ck_users_email_normalized_pair",
        ),
        sa.CheckConstraint(
            "email IS NULL OR length(trim(email)) > 0",
            name="ck_users_email_not_blank",
        ),
        sa.CheckConstraint(
            "normalized_email IS NULL OR length(trim(normalized_email)) > 0",
            name="ck_users_normalized_email_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(password_hash)) > 0",
            name="ck_users_password_hash_not_blank",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'suspended')",
            name="ck_users_status",
        ),
        sa.CheckConstraint(
            "session_version >= 1", name="ck_users_session_version_positive"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_username", name="uq_users_normalized_username"
        ),
        sa.UniqueConstraint("normalized_email", name="uq_users_normalized_email"),
    )
    op.create_index("ix_users_status", "users", ["status"], unique=False)

    op.create_table(
        "user_roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("assigned_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "role IN ('member', 'doctor', 'trainer', 'platform_superadmin')",
            name="ck_user_roles_role",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "role", name="uq_user_roles_user_role"),
    )
    op.create_index(
        "ix_user_roles_assigned_by_user_id",
        "user_roles",
        ["assigned_by_user_id"],
        unique=False,
    )
    op.create_index("ix_user_roles_role", "user_roles", ["role"], unique=False)

    op.create_table(
        "health_subjects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(trim(timezone)) > 0",
            name="ck_health_subjects_timezone_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", name="uq_health_subjects_owner_user_id"
        ),
    )

    op.create_table(
        "support_access_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("granted_to_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="active"
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "mode IN ('read', 'repair', 'export')",
            name="ck_support_access_grants_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'expired')",
            name="ck_support_access_grants_status",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_support_access_grants_reason_not_blank",
        ),
        sa.CheckConstraint(
            "length(reason) <= 2000",
            name="ck_support_access_grants_reason_length",
        ),
        sa.CheckConstraint(
            "revocation_reason IS NULL OR length(revocation_reason) <= 2000",
            name="ck_support_access_grants_revocation_reason_length",
        ),
        sa.CheckConstraint(
            "expires_at > approved_at",
            name="ck_support_access_grants_positive_ttl",
        ),
        sa.CheckConstraint(
            "granted_to_user_id <> approved_by_user_id",
            name="ck_support_access_grants_no_self_approval",
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL "
            "AND revocation_reason IS NOT NULL "
            "AND length(trim(revocation_reason)) > 0) OR "
            "(status <> 'revoked' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL AND revocation_reason IS NULL)",
            name="ck_support_access_grants_revocation_state",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["granted_to_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["health_subjects.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_support_access_grants_approved_by_user_id",
        "support_access_grants",
        ["approved_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_support_access_grants_grantee_status_expires",
        "support_access_grants",
        ["granted_to_user_id", "status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_support_access_grants_revoked_by_user_id",
        "support_access_grants",
        ["revoked_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_support_access_grants_subject_status_expires",
        "support_access_grants",
        ["subject_id", "status", "expires_at"],
        unique=False,
    )

    op.create_table(
        "support_access_scopes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(16), nullable=False),
        sa.Column("resource_key", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "resource_type IN ('domain', 'artifact', 'operation')",
            name="ck_support_access_scopes_resource_type",
        ),
        sa.CheckConstraint(
            "action IN ('read', 'repair', 'export')",
            name="ck_support_access_scopes_action",
        ),
        sa.CheckConstraint(
            "length(trim(resource_key)) > 0",
            name="ck_support_access_scopes_resource_key_not_blank",
        ),
        sa.CheckConstraint(
            "resource_key NOT LIKE '%*%'",
            name="ck_support_access_scopes_no_wildcard",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["support_access_grants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "grant_id",
            "resource_type",
            "resource_key",
            "action",
            name="uq_support_access_scopes_grant_resource_action",
        ),
    )
    op.create_index(
        "ix_support_access_scopes_resource",
        "support_access_scopes",
        ["resource_type", "resource_key", "action"],
        unique=False,
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("support_access_grant_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(96), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column(
            "metadata_json",
            _JSON_TYPE,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.CheckConstraint(
            "length(trim(event_type)) > 0",
            name="ck_audit_events_event_type_not_blank",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'failed')",
            name="ck_audit_events_outcome",
        ),
        sa.CheckConstraint(
            "resource_type IS NULL OR length(trim(resource_type)) > 0",
            name="ck_audit_events_resource_type_not_blank",
        ),
        sa.CheckConstraint(
            "resource_id IS NULL OR length(trim(resource_id)) > 0",
            name="ck_audit_events_resource_id_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["health_subjects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["support_access_grant_id"],
            ["support_access_grants.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_actor_occurred",
        "audit_events",
        ["actor_user_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_grant_occurred",
        "audit_events",
        ["support_access_grant_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_occurred_at",
        "audit_events",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_subject_occurred",
        "audit_events",
        ["subject_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_type_occurred",
        "audit_events",
        ["event_type", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_type_occurred", table_name="audit_events")
    op.drop_index("ix_audit_events_subject_occurred", table_name="audit_events")
    op.drop_index("ix_audit_events_occurred_at", table_name="audit_events")
    op.drop_index("ix_audit_events_grant_occurred", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_occurred", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index(
        "ix_support_access_scopes_resource", table_name="support_access_scopes"
    )
    op.drop_table("support_access_scopes")

    op.drop_index(
        "ix_support_access_grants_subject_status_expires",
        table_name="support_access_grants",
    )
    op.drop_index(
        "ix_support_access_grants_revoked_by_user_id",
        table_name="support_access_grants",
    )
    op.drop_index(
        "ix_support_access_grants_grantee_status_expires",
        table_name="support_access_grants",
    )
    op.drop_index(
        "ix_support_access_grants_approved_by_user_id",
        table_name="support_access_grants",
    )
    op.drop_table("support_access_grants")

    op.drop_table("health_subjects")

    op.drop_index("ix_user_roles_role", table_name="user_roles")
    op.drop_index(
        "ix_user_roles_assigned_by_user_id", table_name="user_roles"
    )
    op.drop_table("user_roles")

    op.drop_index("ix_users_status", table_name="users")
    op.drop_table("users")
