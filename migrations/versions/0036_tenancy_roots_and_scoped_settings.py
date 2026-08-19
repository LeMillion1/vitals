"""Add subject-bound integration/file roots and scoped settings.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-19

This revision is deliberately DDL-only.  On a fresh deployment Alembic runs
before the application bootstraps the legacy owner/subject, so connection and
file placeholders belong to a later idempotent application backfill.  No
credential, token, provider client, or file is read or moved here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_TYPE = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


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
        "integration_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("connection_type", sa.String(32), nullable=False),
        sa.Column(
            "external_account_discriminator", sa.String(128), nullable=False
        ),
        sa.Column("credential_ref", sa.String(255), nullable=True),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="pending"
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "provider IN ('garmin', 'hevy', 'openrouter', 'telegram')",
            name="ck_integration_connections_provider",
        ),
        sa.CheckConstraint(
            "connection_type IN ('account', 'import', 'ai_gateway', 'recipient')",
            name="ck_integration_connections_type",
        ),
        sa.CheckConstraint(
            "(provider = 'garmin' AND connection_type IN ('account', 'import')) OR "
            "(provider = 'hevy' AND connection_type = 'account') OR "
            "(provider = 'openrouter' AND connection_type = 'ai_gateway') OR "
            "(provider = 'telegram' AND connection_type = 'recipient')",
            name="ck_integration_connections_provider_type_pair",
        ),
        sa.CheckConstraint(
            "status IN ('legacy', 'pending', 'active', 'disabled', 'retired')",
            name="ck_integration_connections_status",
        ),
        sa.CheckConstraint(
            "length(trim(external_account_discriminator)) > 0",
            name="ck_integration_connections_discriminator_not_blank",
        ),
        sa.CheckConstraint(
            "credential_ref IS NULL OR length(trim(credential_ref)) > 0",
            name="ck_integration_connections_credential_ref_not_blank",
        ),
        sa.CheckConstraint(
            "(status = 'retired' AND retired_at IS NOT NULL) OR "
            "(status <> 'retired' AND retired_at IS NULL)",
            name="ck_integration_connections_retirement_state",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["health_subjects.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_id",
            "provider",
            "connection_type",
            "external_account_discriminator",
            name="uq_integration_connections_subject_provider_type_discriminator",
        ),
    )
    op.create_index(
        "ix_integration_connections_subject_status",
        "integration_connections",
        ["subject_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_integration_connections_provider_status",
        "integration_connections",
        ["provider", "status"],
        unique=False,
    )

    op.create_table(
        "file_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("opaque_key", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("storage_backend", sa.String(24), nullable=False),
        sa.Column("storage_ref", sa.String(512), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("sha256_hex", sa.String(64), nullable=True),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="pending"
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "purpose IN ('progress_photo', 'lab_document', 'body_scan_document')",
            name="ck_file_assets_purpose",
        ),
        sa.CheckConstraint(
            "storage_backend IN ('legacy_local', 'private_local', 'object_store')",
            name="ck_file_assets_storage_backend",
        ),
        sa.CheckConstraint(
            "status IN ('legacy_placeholder', 'pending', 'active', 'deleted', "
            "'purged')",
            name="ck_file_assets_status",
        ),
        sa.CheckConstraint(
            "length(trim(storage_ref)) > 0 "
            "AND storage_ref NOT LIKE '/%' "
            "AND storage_ref NOT LIKE '%..%'",
            name="ck_file_assets_storage_ref_safe",
        ),
        sa.CheckConstraint(
            "media_type IS NULL OR length(trim(media_type)) > 0",
            name="ck_file_assets_media_type_not_blank",
        ),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size >= 0",
            name="ck_file_assets_byte_size_nonnegative",
        ),
        sa.CheckConstraint(
            "sha256_hex IS NULL OR "
            "(length(sha256_hex) = 64 AND lower(sha256_hex) = sha256_hex)",
            name="ck_file_assets_sha256_shape",
        ),
        sa.CheckConstraint(
            "status <> 'active' OR "
            "(media_type IS NOT NULL AND byte_size IS NOT NULL "
            "AND sha256_hex IS NOT NULL AND storage_backend <> 'legacy_local')",
            name="ck_file_assets_active_metadata",
        ),
        sa.CheckConstraint(
            "(status IN ('legacy_placeholder', 'pending', 'active') "
            "AND deleted_at IS NULL AND purged_at IS NULL) OR "
            "(status = 'deleted' AND deleted_at IS NOT NULL "
            "AND purged_at IS NULL) OR "
            "(status = 'purged' AND deleted_at IS NOT NULL "
            "AND purged_at IS NOT NULL AND purged_at >= deleted_at)",
            name="ck_file_assets_lifecycle_state",
        ),
        sa.CheckConstraint(
            "status <> 'legacy_placeholder' OR storage_backend = 'legacy_local'",
            name="ck_file_assets_legacy_storage",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["health_subjects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("opaque_key", name="uq_file_assets_opaque_key"),
        sa.UniqueConstraint(
            "storage_backend",
            "storage_ref",
            name="uq_file_assets_backend_storage_ref",
        ),
    )
    op.create_index(
        "ix_file_assets_subject_purpose_status_created",
        "file_assets",
        ["subject_id", "purpose", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_file_assets_uploaded_by_created",
        "file_assets",
        ["uploaded_by_user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", _JSON_TYPE, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "length(trim(key)) > 0", name="ck_platform_settings_key_not_blank"
        ),
        sa.PrimaryKeyConstraint("key", name="pk_platform_settings"),
    )
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", _JSON_TYPE, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "length(trim(key)) > 0", name="ck_user_settings_key_not_blank"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "key", name="pk_user_settings"),
    )
    op.create_table(
        "subject_settings",
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", _JSON_TYPE, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "length(trim(key)) > 0", name="ck_subject_settings_key_not_blank"
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["health_subjects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("subject_id", "key", name="pk_subject_settings"),
    )
    op.create_table(
        "integration_connection_settings",
        sa.Column("integration_connection_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", _JSON_TYPE, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "length(trim(key)) > 0",
            name="ck_integration_connection_settings_key_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["integration_connection_id"],
            ["integration_connections.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "integration_connection_id",
            "key",
            name="pk_integration_connection_settings",
        ),
    )


def downgrade() -> None:
    op.drop_table("integration_connection_settings")
    op.drop_table("subject_settings")
    op.drop_table("user_settings")
    op.drop_table("platform_settings")

    op.drop_index("ix_file_assets_uploaded_by_created", table_name="file_assets")
    op.drop_index(
        "ix_file_assets_subject_purpose_status_created", table_name="file_assets"
    )
    op.drop_table("file_assets")

    op.drop_index(
        "ix_integration_connections_provider_status",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_subject_status",
        table_name="integration_connections",
    )
    op.drop_table("integration_connections")
