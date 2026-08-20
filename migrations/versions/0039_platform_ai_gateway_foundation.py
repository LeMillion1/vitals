"""Add the platform-owned AI gateway and subject invocation ledger.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-20

This is an expand-only foundation. It does not inspect provider credentials,
rewrite existing subject-owned OpenRouter roots, or infer historical invocations.
The explicit bridge table is populated later by a fail-closed runtime backfill.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
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


def upgrade() -> None:
    op.create_table(
        "platform_integration_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("connection_type", sa.String(32), nullable=False),
        sa.Column(
            "external_account_discriminator", sa.String(128), nullable=False
        ),
        sa.Column("credential_ref", sa.String(255), nullable=False),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="pending"
        ),
        sa.Column(
            "config_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("configured_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "provider = 'openrouter' AND connection_type = 'ai_gateway'",
            name="ck_platform_integration_connections_provider_type_pair",
        ),
        sa.CheckConstraint(
            "status IN ('legacy', 'pending', 'active', 'disabled', 'retired')",
            name="ck_platform_integration_connections_status",
        ),
        sa.CheckConstraint(
            "length(trim(external_account_discriminator)) > 0",
            name="ck_platform_integration_connections_discriminator_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(credential_ref)) > 0",
            name="ck_platform_integration_connections_credential_ref_not_blank",
        ),
        sa.CheckConstraint(
            "config_version >= 1",
            name="ck_platform_integration_connections_config_version_positive",
        ),
        sa.CheckConstraint(
            "(status = 'retired' AND retired_at IS NOT NULL) OR "
            "(status <> 'retired' AND retired_at IS NULL)",
            name="ck_platform_integration_connections_retirement_state",
        ),
        sa.ForeignKeyConstraint(
            ["configured_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "connection_type",
            "external_account_discriminator",
            name="uq_platform_integration_connections_provider_type_discriminator",
        ),
        sa.UniqueConstraint(
            "id",
            "config_version",
            name="uq_platform_integration_connections_id_config_version",
        ),
    )
    op.create_index(
        "uq_platform_integration_connections_current_provider_type",
        "platform_integration_connections",
        ["provider", "connection_type"],
        unique=True,
        postgresql_where=sa.text("status <> 'retired'"),
        sqlite_where=sa.text("status <> 'retired'"),
    )
    op.create_index(
        "ix_platform_integration_connections_provider_status",
        "platform_integration_connections",
        ["provider", "status"],
        unique=False,
    )
    op.create_index(
        "ix_platform_integration_connections_configured_by_user_id",
        "platform_integration_connections",
        ["configured_by_user_id"],
        unique=False,
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
            "(status = 'cancelled' AND finished_at IS NOT NULL)",
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
        sa.UniqueConstraint(
            "id", "subject_id", name="uq_ai_invocations_id_subject"
        ),
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

    op.create_table(
        "legacy_openrouter_connection_bridges",
        sa.Column("legacy_integration_connection_id", sa.Uuid(), nullable=False),
        sa.Column(
            "platform_integration_connection_id", sa.Uuid(), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["legacy_integration_connection_id"],
            ["integration_connections.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["platform_integration_connection_id"],
            ["platform_integration_connections.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("legacy_integration_connection_id"),
    )
    op.create_index(
        "ix_legacy_openrouter_bridges_platform_connection",
        "legacy_openrouter_connection_bridges",
        ["platform_integration_connection_id"],
        unique=False,
    )


def _assert_downgrade_is_safe() -> None:
    bind = op.get_bind()
    for table_name in (
        "legacy_openrouter_connection_bridges",
        "ai_invocations",
        "platform_integration_connections",
    ):
        table = sa.table(table_name)
        if bind.execute(sa.select(sa.func.count()).select_from(table)).scalar_one():
            raise RuntimeError(
                f"0039 downgrade refused: platform AI data exists in {table_name}"
            )


def downgrade() -> None:
    _assert_downgrade_is_safe()

    op.drop_index(
        "ix_legacy_openrouter_bridges_platform_connection",
        table_name="legacy_openrouter_connection_bridges",
    )
    op.drop_table("legacy_openrouter_connection_bridges")

    op.drop_index("ix_ai_invocations_actor_created", table_name="ai_invocations")
    op.drop_index(
        "ix_ai_invocations_platform_status_created", table_name="ai_invocations"
    )
    op.drop_index("ix_ai_invocations_subject_created", table_name="ai_invocations")
    op.drop_table("ai_invocations")

    op.drop_index(
        "ix_platform_integration_connections_configured_by_user_id",
        table_name="platform_integration_connections",
    )
    op.drop_index(
        "ix_platform_integration_connections_provider_status",
        table_name="platform_integration_connections",
    )
    op.drop_index(
        "uq_platform_integration_connections_current_provider_type",
        table_name="platform_integration_connections",
    )
    op.drop_table("platform_integration_connections")
