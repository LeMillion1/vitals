"""Explicitly scoped runtime settings for the commercial data boundary.

The legacy ``app_settings`` table remains the compatibility source while only
the bootstrapped owner can write.  These tables prevent user, health-subject,
integration, and platform state from sharing a global key namespace.  Secrets,
MFA material, and provider credentials do not belong in any generic setting.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vitals.models.base import Base

_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


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


class PlatformSetting(Base):
    """Installation-wide non-secret product configuration."""

    __tablename__ = "platform_settings"
    __table_args__ = (
        PrimaryKeyConstraint("key", name="pk_platform_settings"),
        CheckConstraint(
            "length(trim(key)) > 0", name="ck_platform_settings_key_not_blank"
        ),
    )

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class UserSetting(Base):
    """Non-secret UI/account preference belonging to one user."""

    __tablename__ = "user_settings"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "key", name="pk_user_settings"),
        CheckConstraint(
            "length(trim(key)) > 0", name="ck_user_settings_key_not_blank"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class SubjectSetting(Base):
    """Preference whose meaning belongs to one health subject."""

    __tablename__ = "subject_settings"
    __table_args__ = (
        PrimaryKeyConstraint("subject_id", "key", name="pk_subject_settings"),
        CheckConstraint(
            "length(trim(key)) > 0", name="ck_subject_settings_key_not_blank"
        ),
    )

    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class IntegrationConnectionSetting(Base):
    """Non-secret option controlling one integration connection."""

    __tablename__ = "integration_connection_settings"
    __table_args__ = (
        PrimaryKeyConstraint(
            "integration_connection_id",
            "key",
            name="pk_integration_connection_settings",
        ),
        CheckConstraint(
            "length(trim(key)) > 0",
            name="ck_integration_connection_settings_key_not_blank",
        ),
    )

    integration_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


__all__ = [
    "IntegrationConnectionSetting",
    "PlatformSetting",
    "SubjectSetting",
    "UserSetting",
]
