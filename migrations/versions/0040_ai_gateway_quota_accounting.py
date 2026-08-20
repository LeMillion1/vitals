"""Add hard platform/subject AI quota accounting and dispatch reservations.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-20

0039 deliberately had no production writers.  This migration refuses to invent
quota provenance for any unexpected pre-0040 invocation, then replaces that
empty table with the fully constrained accounting shape.  Quota periods are
half-open and explicit: absence never means unlimited.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def _assert_table_empty(table_name: str, *, direction: str) -> None:
    table = sa.table(table_name)
    if op.get_bind().execute(sa.select(sa.func.count()).select_from(table)).scalar_one():
        raise RuntimeError(
            f"0040 {direction} refused: AI accounting data exists in {table_name}"
        )


def _create_platform_quota_periods() -> None:
    op.create_table(
        "ai_platform_quota_periods",
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("cost_limit_microunits", sa.BigInteger(), nullable=False),
        sa.Column("unit_limit", sa.BigInteger(), nullable=False),
        sa.Column(
            "reserved_cost_microunits",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "charged_cost_microunits",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "reserved_units", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "charged_units", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("configured_by_user_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "period_end > period_start",
            name="ck_ai_platform_quota_periods_positive_period",
        ),
        sa.CheckConstraint(
            "cost_limit_microunits >= 0 AND unit_limit >= 0",
            name="ck_ai_platform_quota_periods_nonnegative_limits",
        ),
        sa.CheckConstraint(
            "reserved_cost_microunits >= 0 AND charged_cost_microunits >= 0 "
            "AND reserved_units >= 0 AND charged_units >= 0",
            name="ck_ai_platform_quota_periods_nonnegative_counters",
        ),
        sa.CheckConstraint(
            "reserved_cost_microunits + charged_cost_microunits "
            "<= cost_limit_microunits AND "
            "reserved_units + charged_units <= unit_limit",
            name="ck_ai_platform_quota_periods_within_limits",
        ),
        sa.ForeignKeyConstraint(
            ["configured_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("period_start", "period_end"),
    )
    op.create_index(
        "ix_ai_platform_quota_periods_end",
        "ai_platform_quota_periods",
        ["period_end"],
        unique=False,
    )


def _create_subject_quota_periods() -> None:
    op.create_table(
        "ai_subject_quota_periods",
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("cost_limit_microunits", sa.BigInteger(), nullable=False),
        sa.Column("unit_limit", sa.BigInteger(), nullable=False),
        sa.Column(
            "reserved_cost_microunits",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "charged_cost_microunits",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "reserved_units", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "charged_units", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("configured_by_user_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "period_end > period_start",
            name="ck_ai_subject_quota_periods_positive_period",
        ),
        sa.CheckConstraint(
            "cost_limit_microunits >= 0 AND unit_limit >= 0",
            name="ck_ai_subject_quota_periods_nonnegative_limits",
        ),
        sa.CheckConstraint(
            "reserved_cost_microunits >= 0 AND charged_cost_microunits >= 0 "
            "AND reserved_units >= 0 AND charged_units >= 0",
            name="ck_ai_subject_quota_periods_nonnegative_counters",
        ),
        sa.CheckConstraint(
            "reserved_cost_microunits + charged_cost_microunits "
            "<= cost_limit_microunits AND "
            "reserved_units + charged_units <= unit_limit",
            name="ck_ai_subject_quota_periods_within_limits",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["health_subjects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["configured_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("subject_id", "period_start", "period_end"),
    )
    op.create_index(
        "ix_ai_subject_quota_periods_end",
        "ai_subject_quota_periods",
        ["subject_id", "period_end"],
        unique=False,
    )


def _drop_ai_invocations() -> None:
    op.drop_index("ix_ai_invocations_actor_created", table_name="ai_invocations")
    op.drop_index(
        "ix_ai_invocations_platform_status_created", table_name="ai_invocations"
    )
    op.drop_index("ix_ai_invocations_subject_created", table_name="ai_invocations")
    op.drop_table("ai_invocations")


def _create_ai_invocations(*, with_quota: bool) -> None:
    quota_columns: list[sa.Column] = []
    quota_constraints: list[sa.Constraint] = []
    if with_quota:
        quota_columns = [
            sa.Column("quota_period_start", sa.Date(), nullable=False),
            sa.Column("quota_period_end", sa.Date(), nullable=False),
            sa.Column("reserved_cost_microunits", sa.BigInteger(), nullable=False),
            sa.Column("reserved_units", sa.BigInteger(), nullable=False),
            sa.Column(
                "charged_cost_microunits",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "charged_units", sa.BigInteger(), nullable=False, server_default="0"
            ),
        ]
        quota_constraints = [
            sa.CheckConstraint(
                "(source = 'scheduler' AND actor_user_id IS NULL) OR "
                "(source <> 'scheduler' AND actor_user_id IS NOT NULL)",
                name="ck_ai_invocations_source_actor",
            ),
            sa.CheckConstraint(
                "quota_period_end > quota_period_start",
                name="ck_ai_invocations_quota_period_positive",
            ),
            sa.CheckConstraint(
                "reserved_cost_microunits >= 0 AND reserved_units >= 0 AND "
                "(reserved_cost_microunits > 0 OR reserved_units > 0)",
                name="ck_ai_invocations_reservation_positive",
            ),
            sa.CheckConstraint(
                "charged_cost_microunits >= 0 AND charged_units >= 0 AND "
                "charged_cost_microunits <= reserved_cost_microunits AND "
                "charged_units <= reserved_units",
                name="ck_ai_invocations_charge_within_reservation",
            ),
            sa.CheckConstraint(
                "cost_microunits IS NULL OR "
                "cost_microunits <= reserved_cost_microunits",
                name="ck_ai_invocations_actual_cost_within_reservation",
            ),
            sa.CheckConstraint(
                "coalesce(input_tokens, 0) + coalesce(output_tokens, 0) "
                "<= reserved_units",
                name="ck_ai_invocations_actual_units_within_reservation",
            ),
            sa.CheckConstraint(
                "(status IN ('prepared', 'cancelled') "
                "AND charged_cost_microunits = 0 AND charged_units = 0) OR "
                "(status IN ('dispatching', 'succeeded', 'failed', 'ambiguous') "
                "AND charged_cost_microunits = reserved_cost_microunits "
                "AND charged_units = reserved_units)",
                name="ck_ai_invocations_accounting_state",
            ),
            sa.ForeignKeyConstraint(
                ["quota_period_start", "quota_period_end"],
                [
                    "ai_platform_quota_periods.period_start",
                    "ai_platform_quota_periods.period_end",
                ],
                ondelete="RESTRICT",
                name="fk_ai_invocations_platform_quota_period",
            ),
            sa.ForeignKeyConstraint(
                ["subject_id", "quota_period_start", "quota_period_end"],
                [
                    "ai_subject_quota_periods.subject_id",
                    "ai_subject_quota_periods.period_start",
                    "ai_subject_quota_periods.period_end",
                ],
                ondelete="RESTRICT",
                name="fk_ai_invocations_subject_quota_period",
            ),
        ]

    cancelled_state = (
        "(status = 'cancelled' AND started_at IS NULL AND finished_at IS NOT NULL)"
        if with_quota
        else "(status = 'cancelled' AND finished_at IS NOT NULL)"
    )
    op.create_table(
        "ai_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("platform_integration_connection_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        *quota_columns,
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="prepared"
        ),
        sa.Column("upstream_request_id", sa.String(255), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_microunits", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "purpose IN ('weekly_digest', 'daily_brief', 'lab_document_parse', "
            "'body_scan_parse', 'signal_parse', 'question_reply')",
            name="ck_ai_invocations_purpose",
        ),
        sa.CheckConstraint(
            "source IN ('web', 'mcp', 'scheduler', 'telegram')",
            name="ck_ai_invocations_source",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'dispatching', 'succeeded', 'failed', "
            "'ambiguous', 'cancelled')",
            name="ck_ai_invocations_status",
        ),
        sa.CheckConstraint(
            "length(trim(model)) > 0", name="ck_ai_invocations_model_not_blank"
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_ai_invocations_idempotency_key_not_blank",
        ),
        sa.CheckConstraint(
            "config_version >= 1",
            name="ck_ai_invocations_config_version_positive",
        ),
        sa.CheckConstraint(
            "upstream_request_id IS NULL OR "
            "length(trim(upstream_request_id)) > 0",
            name="ck_ai_invocations_upstream_request_id_not_blank",
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_ai_invocations_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_ai_invocations_output_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "cost_microunits IS NULL OR cost_microunits >= 0",
            name="ck_ai_invocations_cost_microunits_nonnegative",
        ),
        sa.CheckConstraint(
            "(status = 'prepared' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'dispatching' AND started_at IS NOT NULL "
            "AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'ambiguous') "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            f"{cancelled_state}",
            name="ck_ai_invocations_lifecycle_timestamps",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name="ck_ai_invocations_timestamp_order",
        ),
        sa.CheckConstraint(
            "(status IN ('failed', 'ambiguous', 'cancelled') AND error_code IN "
            "('provider_unconfigured', 'provider_unavailable', 'timeout', "
            "'invalid_response', 'cancelled_by_policy', 'quota_exceeded', "
            "'internal_error')) OR "
            "(status NOT IN ('failed', 'ambiguous', 'cancelled') "
            "AND error_code IS NULL)",
            name="ck_ai_invocations_error_state",
        ),
        *quota_constraints,
        sa.ForeignKeyConstraint(
            ["subject_id"], ["health_subjects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["platform_integration_connection_id", "config_version"],
            [
                "platform_integration_connections.id",
                "platform_integration_connections.config_version",
            ],
            ondelete="RESTRICT",
            name="fk_ai_invocations_platform_connection_config",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "subject_id", name="uq_ai_invocations_id_subject"),
        sa.UniqueConstraint(
            "subject_id",
            "purpose",
            "idempotency_key",
            name="uq_ai_invocations_subject_purpose_idempotency",
        ),
    )
    op.create_index(
        "ix_ai_invocations_subject_created",
        "ai_invocations",
        ["subject_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_invocations_platform_status_created",
        "ai_invocations",
        ["platform_integration_connection_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_invocations_actor_created",
        "ai_invocations",
        ["actor_user_id", "created_at"],
        unique=False,
    )


def upgrade() -> None:
    _assert_table_empty("ai_invocations", direction="upgrade")
    _create_platform_quota_periods()
    _create_subject_quota_periods()
    _drop_ai_invocations()
    _create_ai_invocations(with_quota=True)


def downgrade() -> None:
    for table_name in (
        "ai_invocations",
        "ai_subject_quota_periods",
        "ai_platform_quota_periods",
    ):
        _assert_table_empty(table_name, direction="downgrade")

    _drop_ai_invocations()
    op.drop_index(
        "ix_ai_subject_quota_periods_end",
        table_name="ai_subject_quota_periods",
    )
    op.drop_table("ai_subject_quota_periods")
    op.drop_index(
        "ix_ai_platform_quota_periods_end",
        table_name="ai_platform_quota_periods",
    )
    op.drop_table("ai_platform_quota_periods")
    _create_ai_invocations(with_quota=False)
