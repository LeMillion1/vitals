"""Add the subject-isolated, PHI-free care message push outbox.

Revision ID: 0069
Revises: 0068
Create Date: 2026-08-25

Each row binds one patient-visible message to one recipient account's exact
browser subscription. Composite foreign keys keep both copied ownership edges
honest, and FORCE RLS makes an unbound application connection see no rows. No
rendered text, patient/sender name, attachment name, endpoint, provider payload,
or free-form error is stored here.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: Union[str, None] = "0068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = ("care_push_deliveries",)
SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"(subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid "
    f"OR current_setting('{PLATFORM_SETTING}', true) = 'on')"
)


def upgrade() -> None:
    op.create_table(
        "care_push_deliveries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("message_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("subscription_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recipient_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("lease_token", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "dispatch_started_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(32), nullable=True),
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
            "id", "subject_id", name="uq_care_push_deliveries_id_subject"
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "subject_id"],
            ["care_messages.id", "care_messages.subject_id"],
            name="fk_care_push_deliveries_message_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id", "recipient_user_id"],
            ["web_push_subscriptions.id", "web_push_subscriptions.user_id"],
            name="fk_care_push_deliveries_subscription_recipient",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "message_id",
            "recipient_user_id",
            "subscription_id",
            name="uq_care_push_deliveries_message_recipient_subscription",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatching', 'sent', 'ambiguous', 'cancelled')",
            name="ck_care_push_deliveries_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND lease_token IS NULL "
            "AND dispatch_started_at IS NULL AND completed_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'dispatching' AND lease_token IS NOT NULL "
            "AND dispatch_started_at IS NOT NULL AND completed_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'sent' AND lease_token IS NOT NULL "
            "AND dispatch_started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(status = 'ambiguous' AND lease_token IS NOT NULL "
            "AND dispatch_started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL) OR "
            "(status = 'cancelled' AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL AND "
            "((lease_token IS NULL AND dispatch_started_at IS NULL) OR "
            "(lease_token IS NOT NULL AND dispatch_started_at IS NOT NULL)))",
            name="ck_care_push_deliveries_lifecycle",
        ),
        sa.CheckConstraint(
            "dispatch_started_at IS NULL OR completed_at IS NULL "
            "OR completed_at >= dispatch_started_at",
            name="ck_care_push_deliveries_timestamp_order",
        ),
        sa.CheckConstraint(
            "(status = 'ambiguous' AND error_code IN "
            "('transport_error', 'invalid_response', 'stale_dispatch', "
            "'internal_error')) OR "
            "(status = 'cancelled' AND error_code IN "
            "('access_revoked', 'account_inactive', 'subscription_revoked', "
            "'stale_pending', 'provider_gone')) OR "
            "(status NOT IN ('ambiguous', 'cancelled') AND error_code IS NULL)",
            name="ck_care_push_deliveries_error_state",
        ),
        sa.CheckConstraint(
            "error_code <> 'provider_gone' OR "
            "(lease_token IS NOT NULL AND dispatch_started_at IS NOT NULL)",
            name="ck_care_push_deliveries_provider_gone_after_dispatch",
        ),
        sa.CheckConstraint(
            "error_code <> 'stale_pending' OR "
            "(lease_token IS NULL AND dispatch_started_at IS NULL)",
            name="ck_care_push_deliveries_stale_pending_before_dispatch",
        ),
        sa.CheckConstraint(
            "error_code NOT IN "
            "('access_revoked', 'account_inactive', 'subscription_revoked') OR "
            "(lease_token IS NULL AND dispatch_started_at IS NULL)",
            name="ck_care_push_deliveries_pre_dispatch_cancellation",
        ),
    )
    for name, columns in (
        (
            "ix_care_push_deliveries_status_created",
            ["status", "created_at", "id"],
        ),
        (
            "ix_care_push_deliveries_subject_status_created",
            ["subject_id", "status", "created_at"],
        ),
        (
            "ix_care_push_deliveries_subscription_status_created",
            ["subscription_id", "status", "created_at"],
        ),
        (
            "ix_care_push_deliveries_recipient_status_created",
            ["recipient_user_id", "status", "created_at"],
        ),
    ):
        op.create_index(name, "care_push_deliveries", columns)

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute('ALTER TABLE "care_push_deliveries" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "care_push_deliveries" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{POLICY_NAME}" ON "care_push_deliveries" '
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            'DROP POLICY IF EXISTS "rls_subject_isolation" '
            'ON "care_push_deliveries"'
        )
    for name in (
        "ix_care_push_deliveries_recipient_status_created",
        "ix_care_push_deliveries_subscription_status_created",
        "ix_care_push_deliveries_subject_status_created",
        "ix_care_push_deliveries_status_created",
    ):
        op.drop_index(name, table_name="care_push_deliveries")
    op.drop_table("care_push_deliveries")
