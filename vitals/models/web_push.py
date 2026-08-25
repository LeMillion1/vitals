"""Account-owned browser push endpoints, with no readable endpoint in the lake.

A browser subscription belongs to the signed-in account and device, not to a
health subject.  That distinction matters for professionals: one browser can
receive work from many patient records, and copying the same endpoint into each
record would turn a device identity into patient data and make revocation
ambiguous.

The endpoint and its two encryption keys are credentials.  Only a SHA-256
lookup key is stored in the clear; the complete subscription is authenticated-
encrypted with the installation credential key.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from vitals.models.base import Base


class WebPushSubscription(Base):
    """One browser-profile subscription owned by exactly one user."""

    __tablename__ = "web_push_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "id", "user_id", name="uq_web_push_subscriptions_id_user"
        ),
        UniqueConstraint(
            "endpoint_hash", name="uq_web_push_subscriptions_endpoint_hash"
        ),
        CheckConstraint(
            "length(endpoint_hash) = 64 AND endpoint_hash = lower(endpoint_hash)",
            name="ck_web_push_subscriptions_endpoint_hash",
        ),
        CheckConstraint(
            "key_version >= 1", name="ck_web_push_subscriptions_key_version"
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND ciphertext IS NOT NULL "
            "AND length(ciphertext) > 0) OR "
            "(revoked_at IS NOT NULL AND ciphertext IS NULL)",
            name="ck_web_push_subscriptions_ciphertext_lifecycle",
        ),
        CheckConstraint(
            "last_success_at IS NULL OR revoked_at IS NULL",
            name="ck_web_push_subscriptions_success_active",
        ),
        Index(
            "ix_web_push_subscriptions_user_active",
            "user_id",
            "revoked_at",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["WebPushSubscription"]
