"""Durable platform/subject integration roots and private file artifacts.

These models establish durable ownership only. They intentionally do not hold
provider secrets, access tokens, scheduler cursors, file bytes, or network state.
Subject ``IntegrationConnection`` rows remain subject-bound; the OpenRouter
platform root is structurally separate. Legacy rows are registered by an
application bootstrap after the identity owner exists; Alembic creates schema only.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vitals.enums import (
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.models.base import Base


def _values(enum_type: type) -> str:
    """Render a stable SQL ``IN`` value list for string-backed enums."""

    return ", ".join(f"'{member.value}'" for member in enum_type)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PlatformIntegrationConnection(Base):
    """Installation-owned provider root with no health-subject ownership.

    The row contains only an opaque credential resolver handle. Secret material,
    prompts, responses, and other PHI belong neither here nor in generic settings.
    ``external_account_discriminator`` and ``config_version`` are immutable root
    identity after insert. The future platform-gateway service is the sole write
    boundary for that contract: rotation must atomically retire the current row
    and insert a replacement with a new discriminator in the same database
    transaction. Historical AI invocations therefore retain the exact platform
    root and frozen configuration version that paid for them. The composite
    invocation foreign key also prevents config-version drift after first use;
    discriminator immutability remains an explicit service invariant.
    """

    __tablename__ = "platform_integration_connections"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "connection_type",
            "external_account_discriminator",
            name="uq_platform_integration_connections_provider_type_discriminator",
        ),
        UniqueConstraint(
            "id",
            "config_version",
            name="uq_platform_integration_connections_id_config_version",
        ),
        CheckConstraint(
            "provider = 'openrouter' AND connection_type = 'ai_gateway'",
            name="ck_platform_integration_connections_provider_type_pair",
        ),
        CheckConstraint(
            f"status IN ({_values(IntegrationConnectionStatus)})",
            name="ck_platform_integration_connections_status",
        ),
        CheckConstraint(
            "length(trim(external_account_discriminator)) > 0",
            name="ck_platform_integration_connections_discriminator_not_blank",
        ),
        CheckConstraint(
            "length(trim(credential_ref)) > 0",
            name="ck_platform_integration_connections_credential_ref_not_blank",
        ),
        CheckConstraint(
            "config_version >= 1",
            name="ck_platform_integration_connections_config_version_positive",
        ),
        CheckConstraint(
            "(status = 'retired' AND retired_at IS NOT NULL) OR "
            "(status <> 'retired' AND retired_at IS NULL)",
            name="ck_platform_integration_connections_retirement_state",
        ),
        Index(
            "uq_platform_integration_connections_current_provider_type",
            "provider",
            "connection_type",
            unique=True,
            postgresql_where=text("status <> 'retired'"),
            sqlite_where=text("status <> 'retired'"),
        ),
        Index(
            "ix_platform_integration_connections_provider_status",
            "provider",
            "status",
        ),
        Index(
            "ix_platform_integration_connections_configured_by_user_id",
            "configured_by_user_id",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_discriminator: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    credential_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default=IntegrationConnectionStatus.PENDING.value,
    )
    config_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    configured_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class IntegrationConnection(Base):
    """One logical provider/channel boundary for exactly one health subject.

    ``external_account_discriminator`` is an opaque logical key.  It must never
    be an email, chat ID, username, API-key suffix, or unsalted PII hash.
    ``credential_ref`` is only a resolver handle such as
    ``legacy_env:garmin``; secret material never belongs in this table.
    """

    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "provider",
            "connection_type",
            "external_account_discriminator",
            name="uq_integration_connections_subject_provider_type_discriminator",
        ),
        UniqueConstraint(
            "id", "subject_id", name="uq_integration_connections_id_subject"
        ),
        CheckConstraint(
            f"provider IN ({_values(IntegrationProvider)})",
            name="ck_integration_connections_provider",
        ),
        CheckConstraint(
            f"connection_type IN ({_values(IntegrationConnectionType)})",
            name="ck_integration_connections_type",
        ),
        CheckConstraint(
            "(provider = 'garmin' AND connection_type IN ('account', 'import')) OR "
            "(provider = 'hevy' AND connection_type = 'account') OR "
            "(provider = 'openrouter' AND connection_type = 'ai_gateway') OR "
            "(provider = 'telegram' AND connection_type = 'recipient')",
            name="ck_integration_connections_provider_type_pair",
        ),
        CheckConstraint(
            f"status IN ({_values(IntegrationConnectionStatus)})",
            name="ck_integration_connections_status",
        ),
        CheckConstraint(
            "length(trim(external_account_discriminator)) > 0",
            name="ck_integration_connections_discriminator_not_blank",
        ),
        CheckConstraint(
            "credential_ref IS NULL OR length(trim(credential_ref)) > 0",
            name="ck_integration_connections_credential_ref_not_blank",
        ),
        CheckConstraint(
            "(status = 'retired' AND retired_at IS NOT NULL) OR "
            "(status <> 'retired' AND retired_at IS NULL)",
            name="ck_integration_connections_retirement_state",
        ),
        Index(
            "ix_integration_connections_subject_status", "subject_id", "status"
        ),
        Index("ix_integration_connections_provider_status", "provider", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_discriminator: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    credential_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default=IntegrationConnectionStatus.PENDING.value,
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class FileAsset(Base):
    """Ownership and private storage metadata for one persisted medical file.

    ``opaque_key`` is a rotatable lookup identifier.  ``storage_ref`` is a
    private locator relative to its backend and must never be serialized as a
    download URL.  A legacy placeholder records only the existing database
    reference; backfill does not inspect or move file bytes.
    """

    __tablename__ = "file_assets"
    __table_args__ = (
        UniqueConstraint("opaque_key", name="uq_file_assets_opaque_key"),
        UniqueConstraint("id", "subject_id", name="uq_file_assets_id_subject"),
        UniqueConstraint(
            "storage_backend",
            "storage_ref",
            name="uq_file_assets_backend_storage_ref",
        ),
        CheckConstraint(
            f"purpose IN ({_values(FileAssetPurpose)})",
            name="ck_file_assets_purpose",
        ),
        CheckConstraint(
            f"storage_backend IN ({_values(FileStorageBackend)})",
            name="ck_file_assets_storage_backend",
        ),
        CheckConstraint(
            f"status IN ({_values(FileAssetStatus)})",
            name="ck_file_assets_status",
        ),
        CheckConstraint(
            "length(trim(storage_ref)) > 0 "
            "AND storage_ref NOT LIKE '/%' "
            "AND storage_ref NOT LIKE '%..%'",
            name="ck_file_assets_storage_ref_safe",
        ),
        CheckConstraint(
            "media_type IS NULL OR length(trim(media_type)) > 0",
            name="ck_file_assets_media_type_not_blank",
        ),
        CheckConstraint(
            "byte_size IS NULL OR byte_size >= 0",
            name="ck_file_assets_byte_size_nonnegative",
        ),
        CheckConstraint(
            "sha256_hex IS NULL OR "
            "(length(sha256_hex) = 64 AND lower(sha256_hex) = sha256_hex)",
            name="ck_file_assets_sha256_shape",
        ),
        CheckConstraint(
            "status <> 'active' OR "
            "(media_type IS NOT NULL AND byte_size IS NOT NULL "
            "AND sha256_hex IS NOT NULL AND storage_backend <> 'legacy_local')",
            name="ck_file_assets_active_metadata",
        ),
        CheckConstraint(
            "(status IN ('legacy_placeholder', 'pending', 'active') "
            "AND deleted_at IS NULL AND purged_at IS NULL) OR "
            "(status = 'deleted' AND deleted_at IS NOT NULL "
            "AND purged_at IS NULL) OR "
            "(status = 'purged' AND deleted_at IS NOT NULL "
            "AND purged_at IS NOT NULL AND purged_at >= deleted_at)",
            name="ck_file_assets_lifecycle_state",
        ),
        CheckConstraint(
            "status <> 'legacy_placeholder' OR storage_backend = 'legacy_local'",
            name="ck_file_assets_legacy_storage",
        ),
        Index(
            "ix_file_assets_subject_purpose_status_created",
            "subject_id",
            "purpose",
            "status",
            "created_at",
        ),
        Index(
            "ix_file_assets_uploaded_by_created",
            "uploaded_by_user_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    uploaded_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    opaque_key: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(24), nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    byte_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sha256_hex: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=FileAssetStatus.PENDING.value
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


__all__ = [
    "FileAsset",
    "IntegrationConnection",
    "PlatformIntegrationConnection",
]
