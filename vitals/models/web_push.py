"""The two ownership halves of browser push, kept distinct in one transport model.

A browser subscription belongs to the signed-in account and device, not to a
health subject.  That distinction matters for professionals: one browser can
receive work from many patient records, and copying the same endpoint into each
record would turn a device identity into patient data and make revocation
ambiguous.

A care delivery belongs to the patient whose message caused it. It therefore
has a mandatory ``subject_id`` and FORCE RLS, but points to the account-owned
subscription through a composite account-equality foreign key. The row is a
PHI-free delivery graph only; no rendered notification or provider payload is
stored beside those identifiers.

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
    ForeignKeyConstraint,
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
from vitals.enums import CarePushDeliveryStatus


def _values(enum_type: type) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


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


class CarePushDelivery(Base):
    """One subject-isolated, PHI-free notification claim for one device.

    The row deliberately carries no message text, title, sender name, patient
    name, attachment name, rendered payload, or provider response.  Its foreign
    keys say only which already-authorized message should cause a generic wakeup
    on which account-owned subscription.  A dispatcher must still revalidate
    account, participation, relationship, and consent immediately before any
    network call.
    """

    __tablename__ = "care_push_deliveries"
    __table_args__ = (
        UniqueConstraint("id", "subject_id", name="uq_care_push_deliveries_id_subject"),
        ForeignKeyConstraint(
            ["message_id", "subject_id"],
            ["care_messages.id", "care_messages.subject_id"],
            name="fk_care_push_deliveries_message_subject",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["subscription_id", "recipient_user_id"],
            ["web_push_subscriptions.id", "web_push_subscriptions.user_id"],
            name="fk_care_push_deliveries_subscription_recipient",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "message_id",
            "recipient_user_id",
            "subscription_id",
            name="uq_care_push_deliveries_message_recipient_subscription",
        ),
        CheckConstraint(
            f"status IN ({_values(CarePushDeliveryStatus)})",
            name="ck_care_push_deliveries_status",
        ),
        CheckConstraint(
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
            "(status = 'cancelled' AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL AND "
            "((lease_token IS NULL AND dispatch_started_at IS NULL) OR "
            "(lease_token IS NOT NULL AND dispatch_started_at IS NOT NULL)))",
            name="ck_care_push_deliveries_lifecycle",
        ),
        CheckConstraint(
            "dispatch_started_at IS NULL OR completed_at IS NULL "
            "OR completed_at >= dispatch_started_at",
            name="ck_care_push_deliveries_timestamp_order",
        ),
        CheckConstraint(
            "(status = 'ambiguous' AND error_code IN "
            "('transport_error', 'invalid_response', 'stale_dispatch', "
            "'internal_error')) OR "
            "(status = 'cancelled' AND error_code IN "
            "('access_revoked', 'account_inactive', 'subscription_revoked', "
            "'stale_pending', 'provider_gone', 'provider_rejected')) OR "
            "(status NOT IN ('ambiguous', 'cancelled') AND error_code IS NULL)",
            name="ck_care_push_deliveries_error_state",
        ),
        CheckConstraint(
            "error_code NOT IN ('provider_gone', 'provider_rejected') OR "
            "(lease_token IS NOT NULL AND dispatch_started_at IS NOT NULL)",
            name="ck_care_push_deliveries_provider_outcome_after_dispatch",
        ),
        CheckConstraint(
            "error_code <> 'stale_pending' OR "
            "(lease_token IS NULL AND dispatch_started_at IS NULL)",
            name="ck_care_push_deliveries_stale_pending_before_dispatch",
        ),
        CheckConstraint(
            "error_code NOT IN "
            "('access_revoked', 'account_inactive', 'subscription_revoked') OR "
            "(lease_token IS NULL AND dispatch_started_at IS NULL)",
            name="ck_care_push_deliveries_pre_dispatch_cancellation",
        ),
        Index(
            "ix_care_push_deliveries_status_created",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_care_push_deliveries_subject_status_created",
            "subject_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_care_push_deliveries_subscription_status_created",
            "subscription_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_care_push_deliveries_recipient_status_created",
            "recipient_user_id",
            "status",
            "created_at",
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
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=CarePushDeliveryStatus.PENDING.value,
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    dispatch_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["CarePushDelivery", "WebPushSubscription"]
