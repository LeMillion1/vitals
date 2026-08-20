"""Durable, non-PHI outbound notification delivery intents.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-20

The sent-message journal remains backward compatible. New provider attempts gain
an exact-owned intent that is committed before network I/O and linked atomically
when a successful send is journalled. No rendered content, provider request, or
free-form transport error belongs in the intent table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UPGRADE_ROOT_REFUSAL = (
    "0043 upgrade refused: keyed notifications contain partial ownership roots"
)
_UPGRADE_DELIVERY_ROOT_REFUSAL = (
    "0043 upgrade refused: keyed owned notifications contain mismatched "
    "delivery roots"
)
_DOWNGRADE_INTENT_REFUSAL = (
    "0043 downgrade refused: notification delivery intents contain durable state"
)
_DOWNGRADE_LINK_REFUSAL = (
    "0043 downgrade refused: notifications contain delivery intent provenance"
)
_DOWNGRADE_DEDUPE_REFUSAL = (
    "0043 downgrade refused: scoped notification dedupe keys cannot restore the "
    "global unique index"
)


def _exists(statement: sa.Select) -> bool:
    return op.get_bind().execute(statement.limit(1)).first() is not None


def _lock_upgrade_cutover() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # This is a maintenance cutover: application writers must be stopped.  Lock
    # every existing parent before the journal so later FK/DDL acquisition cannot
    # invert the live S -> A/Q -> C -> raw -> AI -> intent -> Notification order.
    bind.execute(
        sa.text(
            "LOCK TABLE health_subjects, users, integration_connections, "
            "raw_payloads, ai_invocations, notifications "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )


def _lock_downgrade_cutover() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # The same maintenance-only lock order closes both guard/write race windows.
    bind.execute(
        sa.text(
            "LOCK TABLE health_subjects, users, integration_connections, "
            "raw_payloads, ai_invocations, notification_delivery_intents, "
            "notifications IN ACCESS EXCLUSIVE MODE"
        )
    )


def _assert_upgrade_is_cutover_safe() -> None:
    notifications = sa.table(
        "notifications",
        sa.column("dedupe_key", sa.String()),
        sa.column("subject_id", sa.Uuid()),
        sa.column("actor_user_id", sa.Uuid()),
        sa.column("recipient_user_id", sa.Uuid()),
        sa.column("integration_connection_id", sa.Uuid()),
        sa.column("channel", sa.String()),
    )
    connections = sa.table(
        "integration_connections",
        sa.column("id", sa.Uuid()),
        sa.column("subject_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("connection_type", sa.String()),
    )
    subjects = sa.table(
        "health_subjects",
        sa.column("id", sa.Uuid()),
        sa.column("owner_user_id", sa.Uuid()),
    )
    partial_owned = sa.or_(
        sa.and_(
            notifications.c.subject_id.is_(None),
            sa.or_(
                notifications.c.actor_user_id.is_not(None),
                notifications.c.recipient_user_id.is_not(None),
                notifications.c.integration_connection_id.is_not(None),
            ),
        ),
        sa.and_(
            notifications.c.subject_id.is_not(None),
            sa.or_(
                notifications.c.recipient_user_id.is_(None),
                notifications.c.integration_connection_id.is_(None),
            ),
        ),
    )
    if _exists(
        sa.select(notifications.c.dedupe_key).where(
            notifications.c.dedupe_key.is_not(None),
            partial_owned,
        )
    ):
        raise RuntimeError(_UPGRADE_ROOT_REFUSAL)

    fully_owned = sa.and_(
        notifications.c.subject_id.is_not(None),
        notifications.c.recipient_user_id.is_not(None),
        notifications.c.integration_connection_id.is_not(None),
    )
    exact_delivery_root = (
        sa.select(connections.c.id)
        .select_from(
            connections.join(
                subjects,
                subjects.c.id == connections.c.subject_id,
            )
        )
        .where(
            connections.c.id == notifications.c.integration_connection_id,
            connections.c.subject_id == notifications.c.subject_id,
            connections.c.provider == notifications.c.channel,
            connections.c.provider == "telegram",
            connections.c.connection_type == "recipient",
            subjects.c.id == notifications.c.subject_id,
            subjects.c.owner_user_id == notifications.c.recipient_user_id,
        )
        .correlate(notifications)
        .exists()
    )
    if _exists(
        sa.select(notifications.c.dedupe_key).where(
            notifications.c.dedupe_key.is_not(None),
            fully_owned,
            ~exact_delivery_root,
        )
    ):
        raise RuntimeError(_UPGRADE_DELIVERY_ROOT_REFUSAL)


def _assert_downgrade_is_lossless() -> None:
    notifications = sa.table(
        "notifications",
        sa.column("delivery_intent_id", sa.Uuid()),
        sa.column("dedupe_key", sa.String()),
    )
    if _exists(
        sa.select(notifications.c.delivery_intent_id).where(
            notifications.c.delivery_intent_id.is_not(None)
        )
    ):
        raise RuntimeError(_DOWNGRADE_LINK_REFUSAL)

    intents = sa.table(
        "notification_delivery_intents",
        sa.column("id", sa.Uuid()),
    )
    if _exists(sa.select(intents.c.id)):
        raise RuntimeError(_DOWNGRADE_INTENT_REFUSAL)

    duplicate_keys = (
        sa.select(notifications.c.dedupe_key)
        .where(notifications.c.dedupe_key.is_not(None))
        .group_by(notifications.c.dedupe_key)
        .having(sa.func.count() > 1)
    )
    if _exists(duplicate_keys):
        raise RuntimeError(_DOWNGRADE_DEDUPE_REFUSAL)


def upgrade() -> None:
    _lock_upgrade_cutover()
    _assert_upgrade_is_cutover_safe()

    op.create_table(
        "notification_delivery_intents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("integration_connection_id", sa.Uuid(), nullable=False),
        sa.Column("raw_payload_id", sa.Integer(), nullable=True),
        sa.Column("ai_invocation_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("policy_key", sa.String(64), nullable=True),
        sa.Column("policy_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "subject_id",
            name="uq_notification_delivery_intents_id_subject",
        ),
        sa.UniqueConstraint(
            "id",
            "subject_id",
            "recipient_user_id",
            "integration_connection_id",
            "category",
            "channel",
            "idempotency_key",
            name="uq_notification_delivery_intents_delivery_graph",
        ),
        sa.UniqueConstraint(
            "subject_id",
            "recipient_user_id",
            "idempotency_key",
            name="uq_notification_delivery_intents_subject_recipient_idempotency",
        ),
        sa.UniqueConstraint(
            "ai_invocation_id",
            name="uq_notification_delivery_intents_ai_invocation_id",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["health_subjects.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["integration_connection_id", "subject_id"],
            ["integration_connections.id", "integration_connections.subject_id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_connection_subject",
        ),
        sa.ForeignKeyConstraint(
            ["raw_payload_id", "subject_id"],
            ["raw_payloads.id", "raw_payloads.subject_id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_raw_subject",
        ),
        sa.ForeignKeyConstraint(
            ["ai_invocation_id", "subject_id"],
            ["ai_invocations.id", "ai_invocations.subject_id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_ai_invocation_subject",
        ),
        sa.CheckConstraint(
            "channel = 'telegram'",
            name="ck_notification_delivery_intents_channel",
        ),
        sa.CheckConstraint(
            "category IN ('brief', 'evening', 'nudge', 'reply', 'echo', 'test')",
            name="ck_notification_delivery_intents_category",
        ),
        sa.CheckConstraint(
            "raw_payload_id IS NULL OR category IN ('reply', 'echo')",
            name="ck_notification_delivery_intents_raw_category",
        ),
        sa.CheckConstraint(
            "ai_invocation_id IS NULL OR "
            "(raw_payload_id IS NOT NULL AND category IN ('reply', 'echo'))",
            name="ck_notification_delivery_intents_ai_provenance",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) = 64",
            name="ck_notification_delivery_intents_idempotency_key_opaque",
        ),
        sa.CheckConstraint(
            "(category = 'nudge' AND policy_key IS NOT NULL) OR "
            "(category <> 'nudge' AND policy_key IS NULL)",
            name="ck_notification_delivery_intents_policy_key_category",
        ),
        sa.CheckConstraint(
            "policy_key IS NULL OR length(policy_key) = 64",
            name="ck_notification_delivery_intents_policy_key_opaque",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatching', 'sent', 'ambiguous', 'cancelled')",
            name="ck_notification_delivery_intents_status",
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
            "(status = 'cancelled' AND lease_token IS NULL "
            "AND dispatch_started_at IS NULL AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name="ck_notification_delivery_intents_lifecycle",
        ),
        sa.CheckConstraint(
            "dispatch_started_at IS NULL OR completed_at IS NULL "
            "OR completed_at >= dispatch_started_at",
            name="ck_notification_delivery_intents_timestamp_order",
        ),
        sa.CheckConstraint(
            "(status = 'ambiguous' AND error_code IN "
            "('transport_error', 'invalid_response', 'stale_dispatch', "
            "'internal_error')) OR "
            "(status = 'cancelled' AND error_code IN "
            "('cancelled_by_policy', 'stale_pending', 'scope_invalid', "
            "'internal_error')) OR "
            "(status NOT IN ('ambiguous', 'cancelled') AND error_code IS NULL)",
            name="ck_notification_delivery_intents_error_state",
        ),
    )
    op.create_index(
        "ix_notification_delivery_intents_status_updated",
        "notification_delivery_intents",
        ["status", "updated_at", "id"],
    )
    op.create_index(
        "ix_notification_delivery_intents_subject_status_created",
        "notification_delivery_intents",
        ["subject_id", "status", "created_at"],
    )
    op.create_index(
        "ix_notification_delivery_intents_connection_status_created",
        "notification_delivery_intents",
        ["integration_connection_id", "status", "created_at"],
    )
    op.create_index(
        "ix_notification_delivery_intents_raw_category_created",
        "notification_delivery_intents",
        ["raw_payload_id", "category", "created_at"],
    )
    op.create_index(
        "ix_notification_delivery_intents_recipient_created",
        "notification_delivery_intents",
        ["recipient_user_id", "created_at"],
    )
    op.create_index(
        "ix_notification_delivery_intents_budget",
        "notification_delivery_intents",
        [
            "subject_id",
            "recipient_user_id",
            "policy_date",
            "status",
            "category",
        ],
    )
    op.create_index(
        "ix_notification_delivery_intents_policy",
        "notification_delivery_intents",
        [
            "subject_id",
            "recipient_user_id",
            "policy_key",
            "status",
            "policy_at",
        ],
    )

    op.add_column(
        "notifications",
        sa.Column("delivery_intent_id", sa.Uuid(), nullable=True),
    )
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.create_unique_constraint(
            "uq_notifications_delivery_intent_id",
            ["delivery_intent_id"],
        )
        batch_op.create_foreign_key(
            "fk_notifications_delivery_intent_subject",
            "notification_delivery_intents",
            [
                "delivery_intent_id",
                "subject_id",
                "recipient_user_id",
                "integration_connection_id",
                "category",
                "channel",
                "dedupe_key",
            ],
            [
                "id",
                "subject_id",
                "recipient_user_id",
                "integration_connection_id",
                "category",
                "channel",
                "idempotency_key",
            ],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_notifications_delivery_intent_scope",
            "delivery_intent_id IS NULL OR "
            "(subject_id IS NOT NULL AND recipient_user_id IS NOT NULL "
            "AND integration_connection_id IS NOT NULL "
            "AND dedupe_key IS NOT NULL AND external_id IS NOT NULL "
            "AND length(trim(external_id)) > 0)",
        )
        batch_op.create_check_constraint(
            "ck_notifications_dedupe_root_shape",
            "dedupe_key IS NULL OR "
            "(subject_id IS NOT NULL AND recipient_user_id IS NOT NULL "
            "AND integration_connection_id IS NOT NULL) OR "
            "(subject_id IS NULL AND actor_user_id IS NULL "
            "AND recipient_user_id IS NULL "
            "AND integration_connection_id IS NULL)",
        )
    op.create_index(
        "uq_notifications_owned_dedupe_key",
        "notifications",
        ["subject_id", "recipient_user_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text(
            "dedupe_key IS NOT NULL AND subject_id IS NOT NULL "
            "AND recipient_user_id IS NOT NULL "
            "AND integration_connection_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "dedupe_key IS NOT NULL AND subject_id IS NOT NULL "
            "AND recipient_user_id IS NOT NULL "
            "AND integration_connection_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_notifications_legacy_dedupe_key",
        "notifications",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text(
            "dedupe_key IS NOT NULL AND subject_id IS NULL "
            "AND recipient_user_id IS NULL AND actor_user_id IS NULL "
            "AND integration_connection_id IS NULL"
        ),
        sqlite_where=sa.text(
            "dedupe_key IS NOT NULL AND subject_id IS NULL "
            "AND recipient_user_id IS NULL AND actor_user_id IS NULL "
            "AND integration_connection_id IS NULL"
        ),
    )
    op.drop_index("uq_notification_dedupe_key", table_name="notifications")


def downgrade() -> None:
    _lock_downgrade_cutover()
    _assert_downgrade_is_lossless()

    op.create_index(
        "uq_notification_dedupe_key",
        "notifications",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
        sqlite_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.drop_index(
        "uq_notifications_legacy_dedupe_key",
        table_name="notifications",
    )
    op.drop_index(
        "uq_notifications_owned_dedupe_key",
        table_name="notifications",
    )
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.drop_constraint(
            "ck_notifications_dedupe_root_shape",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_notifications_delivery_intent_scope",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_notifications_delivery_intent_subject",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_notifications_delivery_intent_id",
            type_="unique",
        )
        batch_op.drop_column("delivery_intent_id")

    op.drop_index(
        "ix_notification_delivery_intents_policy",
        table_name="notification_delivery_intents",
    )
    op.drop_index(
        "ix_notification_delivery_intents_budget",
        table_name="notification_delivery_intents",
    )
    op.drop_index(
        "ix_notification_delivery_intents_recipient_created",
        table_name="notification_delivery_intents",
    )
    op.drop_index(
        "ix_notification_delivery_intents_raw_category_created",
        table_name="notification_delivery_intents",
    )
    op.drop_index(
        "ix_notification_delivery_intents_connection_status_created",
        table_name="notification_delivery_intents",
    )
    op.drop_index(
        "ix_notification_delivery_intents_subject_status_created",
        table_name="notification_delivery_intents",
    )
    op.drop_index(
        "ix_notification_delivery_intents_status_updated",
        table_name="notification_delivery_intents",
    )
    op.drop_table("notification_delivery_intents")


__all__ = ["downgrade", "upgrade"]
