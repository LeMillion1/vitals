"""Add fail-closed, purge-ready account-admission state.

Revision ID: 0072
Revises: 0071
Create Date: 2026-08-25

This revision does not open registration.  It adds the persistence contracts a
later invitation and operator-approval workflow must obey.  Applicant PII and
provider identity can be purged only after a terminal outcome.  User references
remain RESTRICT while present, but the same terminal purge explicitly unlinks
them so account erasure does not destroy the retained opaque outcome.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: Union[str, None] = "0071"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LOWERCASE_SHA256_CHECK = (
    "length(token_digest) = 64 AND lower(token_digest) = token_digest AND "
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "token_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
    "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
    "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''"
)


def upgrade() -> None:
    op.create_table(
        "registration_invitations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("token_digest", sa.String(64), nullable=True),
        sa.Column("normalized_email", sa.String(320), nullable=True),
        sa.Column("account_kind", sa.String(16), nullable=False),
        sa.Column(
            "invited_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consumed_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "token_digest", name="uq_registration_invitations_token_digest"
        ),
        sa.CheckConstraint(
            f"token_digest IS NULL OR ({_LOWERCASE_SHA256_CHECK})",
            name="ck_registration_invitations_token_digest",
        ),
        sa.CheckConstraint(
            "normalized_email IS NULL OR "
            "(length(trim(normalized_email)) > 0 "
            "AND length(normalized_email) <= 320)",
            name="ck_registration_invitations_normalized_email",
        ),
        sa.CheckConstraint(
            "account_kind IN ('member', 'doctor', 'trainer')",
            name="ck_registration_invitations_account_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'consumed', 'revoked', 'expired')",
            name="ck_registration_invitations_status",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_registration_invitations_expiry",
        ),
        sa.CheckConstraint(
            "(status = 'pending' "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'consumed' "
            "AND consumed_at IS NOT NULL AND consumed_at >= created_at "
            "AND ((purged_at IS NULL AND consumed_by_user_id IS NOT NULL) "
            "OR (purged_at IS NOT NULL AND consumed_by_user_id IS NULL)) "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'revoked' "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_at IS NOT NULL AND revoked_at >= created_at "
            "AND ((purged_at IS NULL AND revoked_by_user_id IS NOT NULL) "
            "OR (purged_at IS NOT NULL AND revoked_by_user_id IS NULL)) "
            "AND expired_at IS NULL) OR "
            "(status = 'expired' "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL "
            "AND expired_at IS NOT NULL AND expired_at >= expires_at)",
            name="ck_registration_invitations_state",
        ),
        sa.CheckConstraint(
            "(purged_at IS NULL "
            "AND token_digest IS NOT NULL AND normalized_email IS NOT NULL "
            "AND invited_by_user_id IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND status <> 'pending' "
            "AND purged_at >= created_at "
            "AND token_digest IS NULL AND normalized_email IS NULL "
            "AND invited_by_user_id IS NULL "
            "AND consumed_by_user_id IS NULL "
            "AND revoked_by_user_id IS NULL)",
            name="ck_registration_invitations_purge",
        ),
        sa.CheckConstraint(
            "purged_at IS NULL OR "
            "(status = 'consumed' AND purged_at >= consumed_at) OR "
            "(status = 'revoked' AND purged_at >= revoked_at) OR "
            "(status = 'expired' AND purged_at >= expired_at)",
            name="ck_registration_invitations_purge_time",
        ),
    )
    op.create_index(
        "uq_registration_invitations_live_email",
        "registration_invitations",
        ["normalized_email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_registration_invitations_inviter_status",
        "registration_invitations",
        ["invited_by_user_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_registration_invitations_status_expiry",
        "registration_invitations",
        ["status", "expires_at"],
    )

    op.create_table(
        "registration_requests",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("issuer", sa.String(512), nullable=True),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("verified_email", sa.String(320), nullable=True),
        sa.Column("normalized_verified_email", sa.String(320), nullable=True),
        sa.Column("preferred_username", sa.String(128), nullable=True),
        sa.Column("account_kind", sa.String(16), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "provisioned_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
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
            "issuer IS NULL OR "
            "(length(trim(issuer)) > 0 AND length(issuer) <= 512)",
            name="ck_registration_requests_issuer",
        ),
        sa.CheckConstraint(
            "subject IS NULL OR "
            "(length(trim(subject)) > 0 AND length(subject) <= 255)",
            name="ck_registration_requests_subject",
        ),
        sa.CheckConstraint(
            "(verified_email IS NULL AND normalized_verified_email IS NULL) OR "
            "(verified_email IS NOT NULL "
            "AND normalized_verified_email IS NOT NULL "
            "AND length(trim(verified_email)) > 0 "
            "AND length(verified_email) <= 320 "
            "AND length(trim(normalized_verified_email)) > 0 "
            "AND length(normalized_verified_email) <= 320)",
            name="ck_registration_requests_verified_email_pair",
        ),
        sa.CheckConstraint(
            "preferred_username IS NULL OR "
            "(length(trim(preferred_username)) > 0 "
            "AND length(preferred_username) <= 128)",
            name="ck_registration_requests_preferred_username",
        ),
        sa.CheckConstraint(
            "account_kind IN ('member', 'doctor', 'trainer')",
            name="ck_registration_requests_account_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_registration_requests_status",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_registration_requests_expiry",
        ),
        sa.CheckConstraint(
            "last_seen_at >= created_at",
            name="ck_registration_requests_last_seen",
        ),
        sa.CheckConstraint(
            "(status = 'pending' "
            "AND reviewer_user_id IS NULL AND reviewed_at IS NULL "
            "AND provisioned_user_id IS NULL AND review_note IS NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'approved' "
            "AND reviewed_at IS NOT NULL AND reviewed_at >= created_at "
            "AND ((purged_at IS NULL AND reviewer_user_id IS NOT NULL "
            "AND provisioned_user_id IS NOT NULL) "
            "OR (purged_at IS NOT NULL AND reviewer_user_id IS NULL "
            "AND provisioned_user_id IS NULL)) "
            "AND review_note IS NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'rejected' "
            "AND reviewed_at IS NOT NULL AND reviewed_at >= created_at "
            "AND ((purged_at IS NULL AND reviewer_user_id IS NOT NULL) "
            "OR (purged_at IS NOT NULL AND reviewer_user_id IS NULL)) "
            "AND provisioned_user_id IS NULL "
            "AND ((purged_at IS NULL AND review_note IS NOT NULL "
            "AND length(trim(review_note)) > 0 "
            "AND length(review_note) <= 2000) "
            "OR (purged_at IS NOT NULL AND review_note IS NULL)) "
            "AND expired_at IS NULL) OR "
            "(status = 'expired' "
            "AND reviewer_user_id IS NULL AND reviewed_at IS NULL "
            "AND provisioned_user_id IS NULL AND review_note IS NULL "
            "AND expired_at IS NOT NULL AND expired_at >= expires_at)",
            name="ck_registration_requests_state",
        ),
        sa.CheckConstraint(
            "(purged_at IS NULL AND issuer IS NOT NULL AND subject IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND status <> 'pending' "
            "AND purged_at >= created_at "
            "AND issuer IS NULL AND subject IS NULL "
            "AND verified_email IS NULL AND normalized_verified_email IS NULL "
            "AND preferred_username IS NULL AND review_note IS NULL "
            "AND reviewer_user_id IS NULL AND provisioned_user_id IS NULL)",
            name="ck_registration_requests_purge",
        ),
        sa.CheckConstraint(
            "purged_at IS NULL OR "
            "(status IN ('approved', 'rejected') AND purged_at >= reviewed_at) OR "
            "(status = 'expired' AND purged_at >= expired_at)",
            name="ck_registration_requests_purge_time",
        ),
    )
    op.create_index(
        "uq_registration_requests_issuer_subject",
        "registration_requests",
        ["issuer", "subject"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_registration_requests_status_created",
        "registration_requests",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_registration_requests_status_expiry",
        "registration_requests",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_registration_requests_reviewer_reviewed",
        "registration_requests",
        ["reviewer_user_id", "reviewed_at"],
    )
    op.create_index(
        "ix_registration_requests_provisioned_user",
        "registration_requests",
        ["provisioned_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_registration_requests_provisioned_user",
        table_name="registration_requests",
    )
    op.drop_index(
        "ix_registration_requests_reviewer_reviewed",
        table_name="registration_requests",
    )
    op.drop_index(
        "ix_registration_requests_status_expiry",
        table_name="registration_requests",
    )
    op.drop_index(
        "ix_registration_requests_status_created",
        table_name="registration_requests",
    )
    op.drop_index(
        "uq_registration_requests_issuer_subject",
        table_name="registration_requests",
    )
    op.drop_table("registration_requests")

    op.drop_index(
        "ix_registration_invitations_status_expiry",
        table_name="registration_invitations",
    )
    op.drop_index(
        "ix_registration_invitations_inviter_status",
        table_name="registration_invitations",
    )
    op.drop_index(
        "uq_registration_invitations_live_email",
        table_name="registration_invitations",
    )
    op.drop_table("registration_invitations")
