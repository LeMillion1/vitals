"""Separately governed, short-lived emergency record access.

These rows are intentionally unrelated to ordinary support grants.  A break-
glass session names one holder, one subject and exact read-only domain scopes;
two other active platform superadmins must approve before it can become live.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vitals.enums import BreakGlassStatus
from vitals.models.base import Base, TimestampMixin


class BreakGlassSession(Base, TimestampMixin):
    """One emergency request and, after two reviews, its access receipt."""

    __tablename__ = "break_glass_sessions"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "subject_id",
            name="uq_break_glass_sessions_id_subject",
        ),
        UniqueConstraint(
            "id",
            "subject_id",
            "initiated_by_user_id",
            name="uq_break_glass_sessions_id_subject_holder",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'expired')",
            name="ck_break_glass_sessions_status",
        ),
        CheckConstraint(
            "requested_ttl_minutes IN (15, 30, 60)",
            name="ck_break_glass_sessions_ttl",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0 AND length(reason) <= 2000",
            name="ck_break_glass_sessions_reason",
        ),
        CheckConstraint(
            "incident_reference IS NULL OR (length(trim(incident_reference)) > 0 "
            "AND length(incident_reference) <= 120)",
            name="ck_break_glass_sessions_incident_reference",
        ),
        CheckConstraint(
            "approval_deadline > initiated_at",
            name="ck_break_glass_sessions_positive_approval_window",
        ),
        CheckConstraint(
            "expires_at IS NULL OR activated_at IS NOT NULL",
            name="ck_break_glass_sessions_expiry_after_activation",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > activated_at",
            name="ck_break_glass_sessions_positive_access_window",
        ),
        CheckConstraint(
            "(status = 'pending' AND activated_at IS NULL AND expires_at IS NULL "
            "AND revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(status = 'active' AND activated_at IS NOT NULL AND expires_at IS NOT NULL "
            "AND revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL) OR "
            "(status = 'expired' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL)",
            name="ck_break_glass_sessions_lifecycle",
        ),
        Index(
            "ix_break_glass_sessions_subject_status_deadline",
            "subject_id",
            "status",
            "approval_deadline",
        ),
        Index(
            "ix_break_glass_sessions_holder_status",
            "initiated_by_user_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BreakGlassStatus.PENDING.value
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    incident_reference: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
    requested_ttl_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    initiated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    approval_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    subject = relationship("HealthSubject", foreign_keys=[subject_id])
    initiated_by = relationship("User", foreign_keys=[initiated_by_user_id])
    revoked_by = relationship("User", foreign_keys=[revoked_by_user_id])
    scopes: Mapped[list["BreakGlassScope"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    approvals: Mapped[list["BreakGlassApproval"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class BreakGlassScope(Base):
    """One exact read-only domain admitted to an emergency session."""

    __tablename__ = "break_glass_scopes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "subject_id"],
            ["break_glass_sessions.id", "break_glass_sessions.subject_id"],
            name="fk_break_glass_scopes_exact_session",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "session_id", "resource_key", name="uq_break_glass_scopes_session_domain"
        ),
        CheckConstraint(
            "resource_type = 'domain'", name="ck_break_glass_scopes_resource_type"
        ),
        CheckConstraint("action = 'read'", name="ck_break_glass_scopes_action"),
        CheckConstraint(
            "length(trim(resource_key)) > 0 AND length(resource_key) <= 128",
            name="ck_break_glass_scopes_resource_key",
        ),
        CheckConstraint(
            "resource_key NOT LIKE '%*%'", name="ck_break_glass_scopes_no_wildcard"
        ),
        Index(
            "ix_break_glass_scopes_subject_resource",
            "subject_id",
            "resource_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="domain"
    )
    resource_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="read")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    session: Mapped[BreakGlassSession] = relationship(back_populates="scopes")


class BreakGlassApproval(Base):
    """One of the two distinct, non-holder emergency approvals."""

    __tablename__ = "break_glass_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "subject_id", "holder_user_id"],
            [
                "break_glass_sessions.id",
                "break_glass_sessions.subject_id",
                "break_glass_sessions.initiated_by_user_id",
            ],
            name="fk_break_glass_approvals_exact_session",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "session_id",
            "approved_by_user_id",
            name="uq_break_glass_approvals_session_reviewer",
        ),
        CheckConstraint(
            "approved_by_user_id <> holder_user_id",
            name="ck_break_glass_approvals_not_holder",
        ),
        Index(
            "ix_break_glass_approvals_subject_session",
            "subject_id",
            "session_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    holder_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    session: Mapped[BreakGlassSession] = relationship(back_populates="approvals")
    approved_by = relationship("User", foreign_keys=[approved_by_user_id])


__all__ = ["BreakGlassApproval", "BreakGlassScope", "BreakGlassSession"]
