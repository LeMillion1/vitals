"""Bind raw-backed AI invocations and Telegram parser provenance.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-20

Raw payloads remain the durable PHI source of truth.  AI invocations store only
the opaque raw row identifier, authorization/configuration provenance, and
sanitized accounting metadata.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RAW_PURPOSES = (
    "signal_parse",
    "question_reply",
    "lab_document_parse",
    "body_scan_parse",
)
_UPGRADE_RAW_BACKFILL_REFUSAL = (
    "0042 upgrade refused: raw-backed AI invocations require explicit "
    "raw_payload_id backfill"
)
_DOWNGRADE_RAW_REFUSAL = (
    "0042 downgrade refused: ai_invocations.raw_payload_id contains raw "
    "provenance data"
)
_DOWNGRADE_NOTIFICATION_REFUSAL = (
    "0042 downgrade refused: notifications.ai_invocation_id contains AI "
    "provenance data"
)
_DOWNGRADE_ALERT_REFUSAL = (
    "0042 downgrade refused: system_alerts.ai_invocation_id contains AI "
    "provenance data"
)
_DOWNGRADE_PLATFORM_ALERT_REFUSAL = (
    "0042 downgrade refused: invocation-null platform signal parser alerts "
    "require explicit legacy conversion"
)


def _exists(statement: sa.Select) -> bool:
    return op.get_bind().execute(statement.limit(1)).first() is not None


def _assert_upgrade_is_backfillable() -> None:
    invocations = sa.table(
        "ai_invocations",
        sa.column("purpose", sa.String()),
    )
    if _exists(
        sa.select(invocations.c.purpose).where(
            invocations.c.purpose.in_(_RAW_PURPOSES)
        )
    ):
        raise RuntimeError(_UPGRADE_RAW_BACKFILL_REFUSAL)


def _assert_downgrade_is_lossless() -> None:
    invocations = sa.table(
        "ai_invocations",
        sa.column("raw_payload_id", sa.Integer()),
    )
    if _exists(
        sa.select(invocations.c.raw_payload_id).where(
            invocations.c.raw_payload_id.is_not(None)
        )
    ):
        raise RuntimeError(_DOWNGRADE_RAW_REFUSAL)

    notifications = sa.table(
        "notifications",
        sa.column("ai_invocation_id", sa.Uuid()),
    )
    if _exists(
        sa.select(notifications.c.ai_invocation_id).where(
            notifications.c.ai_invocation_id.is_not(None)
        )
    ):
        raise RuntimeError(_DOWNGRADE_NOTIFICATION_REFUSAL)

    alerts = sa.table(
        "system_alerts",
        sa.column("ai_invocation_id", sa.Uuid()),
        sa.column("subject_id", sa.Uuid()),
        sa.column("integration_connection_id", sa.Uuid()),
        sa.column("alert_key", sa.String()),
        sa.column("entity_ref", sa.String()),
    )
    if _exists(
        sa.select(alerts.c.ai_invocation_id).where(
            alerts.c.ai_invocation_id.is_not(None)
        )
    ):
        raise RuntimeError(_DOWNGRADE_ALERT_REFUSAL)
    if _exists(
        sa.select(alerts.c.alert_key).where(
            alerts.c.ai_invocation_id.is_(None),
            alerts.c.subject_id.is_not(None),
            alerts.c.integration_connection_id.is_(None),
            alerts.c.alert_key == "signal_parser_failed",
            sa.func.length(sa.func.trim(alerts.c.entity_ref)) > 0,
        )
    ):
        raise RuntimeError(_DOWNGRADE_PLATFORM_ALERT_REFUSAL)


def upgrade() -> None:
    _assert_upgrade_is_backfillable()

    op.add_column(
        "ai_invocations",
        sa.Column("raw_payload_id", sa.Integer(), nullable=True),
    )
    with op.batch_alter_table("ai_invocations") as batch_op:
        batch_op.create_foreign_key(
            "fk_ai_invocations_raw_payload_subject",
            "raw_payloads",
            ["raw_payload_id", "subject_id"],
            ["id", "subject_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_ai_invocations_purpose_raw_payload",
            "(purpose IN ('signal_parse', 'question_reply', "
            "'lab_document_parse', 'body_scan_parse') "
            "AND raw_payload_id IS NOT NULL) OR "
            "(purpose IN ('weekly_digest', 'daily_brief') "
            "AND raw_payload_id IS NULL)",
        )
    op.create_index(
        "ix_ai_invocations_raw_purpose_created",
        "ai_invocations",
        ["raw_payload_id", "purpose", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_ai_invocations_raw_purpose_succeeded",
        "ai_invocations",
        ["raw_payload_id", "purpose"],
        unique=True,
        postgresql_where=sa.text(
            "raw_payload_id IS NOT NULL AND status = 'succeeded'"
        ),
        sqlite_where=sa.text(
            "raw_payload_id IS NOT NULL AND status = 'succeeded'"
        ),
    )

    op.add_column(
        "notifications",
        sa.Column("ai_invocation_id", sa.Uuid(), nullable=True),
    )
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.create_unique_constraint(
            "uq_notifications_ai_invocation_id",
            ["ai_invocation_id"],
        )
        batch_op.create_foreign_key(
            "fk_notifications_ai_invocation_subject",
            "ai_invocations",
            ["ai_invocation_id", "subject_id"],
            ["id", "subject_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_notifications_ai_invocation_delivery",
            "ai_invocation_id IS NULL OR "
            "(subject_id IS NOT NULL AND recipient_user_id IS NOT NULL "
            "AND integration_connection_id IS NOT NULL "
            "AND channel = 'telegram' AND category IN ('reply', 'echo'))",
        )
    op.create_index(
        "ix_notifications_ai_invocation_id",
        "notifications",
        ["ai_invocation_id"],
        unique=False,
    )

    op.add_column(
        "system_alerts",
        sa.Column("ai_invocation_id", sa.Uuid(), nullable=True),
    )
    with op.batch_alter_table("system_alerts") as batch_op:
        batch_op.create_foreign_key(
            "fk_system_alerts_ai_invocation_subject",
            "ai_invocations",
            ["ai_invocation_id", "subject_id"],
            ["id", "subject_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_system_alerts_ai_invocation_scope",
            "ai_invocation_id IS NULL OR "
            "(subject_id IS NOT NULL AND integration_connection_id IS NULL "
            "AND alert_key = 'signal_parser_failed' "
            "AND length(trim(entity_ref)) > 0)",
        )
    op.create_index(
        "ix_system_alerts_ai_invocation_id",
        "system_alerts",
        ["ai_invocation_id"],
        unique=False,
    )


def downgrade() -> None:
    _assert_downgrade_is_lossless()

    op.drop_index(
        "ix_system_alerts_ai_invocation_id",
        table_name="system_alerts",
    )
    with op.batch_alter_table("system_alerts") as batch_op:
        batch_op.drop_constraint(
            "ck_system_alerts_ai_invocation_scope",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_system_alerts_ai_invocation_subject",
            type_="foreignkey",
        )
        batch_op.drop_column("ai_invocation_id")

    op.drop_index(
        "ix_notifications_ai_invocation_id",
        table_name="notifications",
    )
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.drop_constraint(
            "ck_notifications_ai_invocation_delivery",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_notifications_ai_invocation_subject",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_notifications_ai_invocation_id",
            type_="unique",
        )
        batch_op.drop_column("ai_invocation_id")

    op.drop_index(
        "uq_ai_invocations_raw_purpose_succeeded",
        table_name="ai_invocations",
    )
    op.drop_index(
        "ix_ai_invocations_raw_purpose_created",
        table_name="ai_invocations",
    )
    with op.batch_alter_table("ai_invocations") as batch_op:
        batch_op.drop_constraint(
            "ck_ai_invocations_purpose_raw_payload",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_ai_invocations_raw_payload_subject",
            type_="foreignkey",
        )
        batch_op.drop_column("raw_payload_id")


__all__ = ["downgrade", "upgrade"]
