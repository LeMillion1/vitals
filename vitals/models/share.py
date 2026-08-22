"""Shared doctor reports — the one place data leaves Vitals.

A row is a **frozen document**: the structured snapshot is built once, at
creation, and never refreshed. Data moved on? Make a new report. That is what
makes a link handed to a doctor mean the same thing tomorrow as it did in the
appointment.

No ``InsightsMixin`` here on purpose. That mixin is for log/metric rows in the
data lake (``date`` / ``domain`` / ``source``); this is a service table, like
``app_settings`` and ``conflict_rules`` — it records an artifact about the data,
not an observation of the body.
"""
from __future__ import annotations

import uuid
from datetime import date as date_type, datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vitals.models.base import Base, TimestampMixin
from vitals.models.ownership_mixins import RequiredSubjectOwnershipMixin

_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


class SharedReport(Base, RequiredSubjectOwnershipMixin, TimestampMixin):
    """One password-protected document published at ``/r/<token>``."""

    __tablename__ = "shared_reports"
    __table_args__ = (
        Index("ix_shared_reports_subject_created", "subject_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    revoked_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # secrets.token_urlsafe(32) — the whole address space of the public route.
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # bcrypt. The plaintext is returned by create_report exactly once and is
    # never stored anywhere.
    password_hash: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    preset: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    domains: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False)
    period_start: Mapped[date_type] = mapped_column(Date, nullable=False)
    period_end: Mapped[date_type] = mapped_column(Date, nullable=False)
    labs_flagged_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The frozen document. Emptied by ``purge_expired`` once the link dies, so an
    # expired report stops holding a full copy of the medical record while its
    # metadata stays readable on /share.
    snapshot: Mapped[Optional[Any]] = mapped_column(_JSON_TYPE, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    opened_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
