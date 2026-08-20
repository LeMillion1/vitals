"""Durable outbound intents and the journal of messages actually sent.

Not a health metric, so no ``InsightsMixin`` (same reasoning as ``system_alerts``):
this is infrastructure. It exists because three separate promises are fiction
without a written record:

  * **Budget.** "At most 4 self-initiated messages a day" can only be enforced by
    counting what actually went out today.
  * **Dedupe.** The same brief must not arrive twice because a job re-ran — the
    partial-unique index on ``dedupe_key`` (the ``system_alerts`` pattern) makes
    a second attempt impossible rather than merely unlikely.
  * **Idempotency / reply context.** ``external_id`` is the channel's own message
    id, which is how an incoming reply is matched back to the message it answers.

``channel`` keeps the table honest about the delivery seam: a second delivery
channel adds rows here, not a second table.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vitals.enums import (
    NotificationDeliveryErrorCode,
    NotificationDeliveryStatus,
)
from vitals.models.base import Base
from vitals.models.ownership_mixins import (
    IntegrationConnectionOwnershipMixin,
    OriginActorMixin,
    SubjectOwnershipMixin,
)

_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


def _values(enum_type: type) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


class NotificationDeliveryIntent(Base):
    """One non-PHI, at-most-once outbound delivery claim.

    Rendered text, buttons, provider recipient identifiers, provider errors, and
    request bodies never belong here. A committed ``dispatching`` row means
    Telegram may have accepted the call; recovery therefore closes it as
    ``ambiguous`` instead of attempting the send again.
    """

    __tablename__ = "notification_delivery_intents"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "subject_id",
            name="uq_notification_delivery_intents_id_subject",
        ),
        UniqueConstraint(
            "id",
            "subject_id",
            "recipient_user_id",
            "integration_connection_id",
            "category",
            "channel",
            "idempotency_key",
            name="uq_notification_delivery_intents_delivery_graph",
        ),
        UniqueConstraint(
            "subject_id",
            "recipient_user_id",
            "idempotency_key",
            name="uq_notification_delivery_intents_subject_recipient_idempotency",
        ),
        UniqueConstraint(
            "ai_invocation_id",
            name="uq_notification_delivery_intents_ai_invocation_id",
        ),
        ForeignKeyConstraint(
            ["integration_connection_id", "subject_id"],
            ["integration_connections.id", "integration_connections.subject_id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_connection_subject",
        ),
        ForeignKeyConstraint(
            ["raw_payload_id", "subject_id"],
            ["raw_payloads.id", "raw_payloads.subject_id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_raw_subject",
        ),
        ForeignKeyConstraint(
            ["ai_invocation_id", "subject_id"],
            ["ai_invocations.id", "ai_invocations.subject_id"],
            ondelete="RESTRICT",
            name="fk_notification_delivery_intents_ai_invocation_subject",
        ),
        CheckConstraint(
            "channel = 'telegram'",
            name="ck_notification_delivery_intents_channel",
        ),
        CheckConstraint(
            "category IN ('brief', 'evening', 'nudge', 'reply', 'echo', 'test')",
            name="ck_notification_delivery_intents_category",
        ),
        CheckConstraint(
            "raw_payload_id IS NULL OR category IN ('reply', 'echo')",
            name="ck_notification_delivery_intents_raw_category",
        ),
        CheckConstraint(
            "ai_invocation_id IS NULL OR "
            "(raw_payload_id IS NOT NULL AND category IN ('reply', 'echo'))",
            name="ck_notification_delivery_intents_ai_provenance",
        ),
        CheckConstraint(
            "length(idempotency_key) = 64",
            name="ck_notification_delivery_intents_idempotency_key_opaque",
        ),
        CheckConstraint(
            "(category = 'nudge' AND policy_key IS NOT NULL) OR "
            "(category <> 'nudge' AND policy_key IS NULL)",
            name="ck_notification_delivery_intents_policy_key_category",
        ),
        CheckConstraint(
            "policy_key IS NULL OR length(policy_key) = 64",
            name="ck_notification_delivery_intents_policy_key_opaque",
        ),
        CheckConstraint(
            f"status IN ({_values(NotificationDeliveryStatus)})",
            name="ck_notification_delivery_intents_status",
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
            "(status = 'cancelled' AND lease_token IS NULL "
            "AND dispatch_started_at IS NULL AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name="ck_notification_delivery_intents_lifecycle",
        ),
        CheckConstraint(
            "dispatch_started_at IS NULL OR completed_at IS NULL "
            "OR completed_at >= dispatch_started_at",
            name="ck_notification_delivery_intents_timestamp_order",
        ),
        CheckConstraint(
            "(status = 'ambiguous' AND error_code IN "
            "('transport_error', 'invalid_response', 'stale_dispatch', "
            "'internal_error')) OR "
            "(status = 'cancelled' AND error_code IN "
            "('cancelled_by_policy', 'stale_pending', 'scope_invalid', "
            "'internal_error')) OR "
            "(status NOT IN ('ambiguous', 'cancelled') AND error_code IS NULL)",
            name="ck_notification_delivery_intents_error_state",
        ),
        Index(
            "ix_notification_delivery_intents_status_updated",
            "status",
            "updated_at",
            "id",
        ),
        Index(
            "ix_notification_delivery_intents_subject_status_created",
            "subject_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_notification_delivery_intents_connection_status_created",
            "integration_connection_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_notification_delivery_intents_raw_category_created",
            "raw_payload_id",
            "category",
            "created_at",
        ),
        Index(
            "ix_notification_delivery_intents_recipient_created",
            "recipient_user_id",
            "created_at",
        ),
        Index(
            "ix_notification_delivery_intents_budget",
            "subject_id",
            "recipient_user_id",
            "policy_date",
            "status",
            "category",
        ),
        Index(
            "ix_notification_delivery_intents_policy",
            "subject_id",
            "recipient_user_id",
            "policy_key",
            "status",
            "policy_at",
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
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    integration_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    raw_payload_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ai_invocation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    policy_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    policy_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default=NotificationDeliveryStatus.PENDING.value,
    )
    lease_token: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    dispatch_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Notification(
    Base,
    SubjectOwnershipMixin,
    OriginActorMixin,
    IntegrationConnectionOwnershipMixin,
):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint(
            "ai_invocation_id",
            name="uq_notifications_ai_invocation_id",
        ),
        ForeignKeyConstraint(
            ["ai_invocation_id", "subject_id"],
            ["ai_invocations.id", "ai_invocations.subject_id"],
            ondelete="RESTRICT",
            name="fk_notifications_ai_invocation_subject",
        ),
        UniqueConstraint(
            "delivery_intent_id",
            name="uq_notifications_delivery_intent_id",
        ),
        ForeignKeyConstraint(
            [
                "delivery_intent_id",
                "subject_id",
                "recipient_user_id",
                "integration_connection_id",
                "category",
                "channel",
                "dedupe_key",
            ],
            [
                "notification_delivery_intents.id",
                "notification_delivery_intents.subject_id",
                "notification_delivery_intents.recipient_user_id",
                "notification_delivery_intents.integration_connection_id",
                "notification_delivery_intents.category",
                "notification_delivery_intents.channel",
                "notification_delivery_intents.idempotency_key",
            ],
            ondelete="RESTRICT",
            name="fk_notifications_delivery_intent_subject",
        ),
        CheckConstraint(
            "ai_invocation_id IS NULL OR "
            "(subject_id IS NOT NULL AND recipient_user_id IS NOT NULL "
            "AND integration_connection_id IS NOT NULL "
            "AND channel = 'telegram' AND category IN ('reply', 'echo'))",
            name="ck_notifications_ai_invocation_delivery",
        ),
        CheckConstraint(
            "delivery_intent_id IS NULL OR "
            "(subject_id IS NOT NULL AND recipient_user_id IS NOT NULL "
            "AND integration_connection_id IS NOT NULL "
            "AND dedupe_key IS NOT NULL AND external_id IS NOT NULL "
            "AND length(trim(external_id)) > 0)",
            name="ck_notifications_delivery_intent_scope",
        ),
        CheckConstraint(
            "dedupe_key IS NULL OR "
            "(subject_id IS NOT NULL AND recipient_user_id IS NOT NULL "
            "AND integration_connection_id IS NOT NULL) OR "
            "(subject_id IS NULL AND actor_user_id IS NULL "
            "AND recipient_user_id IS NULL "
            "AND integration_connection_id IS NULL)",
            name="ck_notifications_dedupe_root_shape",
        ),
        # New owned rows dedupe per subject and recipient, independent of channel
        # connection rotation. Fully-unowned history retains its own exact bridge.
        Index(
            "uq_notifications_owned_dedupe_key",
            "subject_id",
            "recipient_user_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text(
                "dedupe_key IS NOT NULL AND subject_id IS NOT NULL "
                "AND recipient_user_id IS NOT NULL "
                "AND integration_connection_id IS NOT NULL"
            ),
            sqlite_where=text(
                "dedupe_key IS NOT NULL AND subject_id IS NOT NULL "
                "AND recipient_user_id IS NOT NULL "
                "AND integration_connection_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_notifications_legacy_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text(
                "dedupe_key IS NOT NULL AND subject_id IS NULL "
                "AND recipient_user_id IS NULL AND actor_user_id IS NULL "
                "AND integration_connection_id IS NULL"
            ),
            sqlite_where=text(
                "dedupe_key IS NOT NULL AND subject_id IS NULL "
                "AND recipient_user_id IS NULL AND actor_user_id IS NULL "
                "AND integration_connection_id IS NULL"
            ),
        ),
        # The daily-budget count: category IN (...) AND date(sent_at) = today.
        Index("ix_notifications_category_sent", "category", "sent_at"),
        # Matching an incoming reply back to the message it answers.
        Index("ix_notifications_external_id", "external_id"),
        Index("ix_notifications_subject_sent", "subject_id", "sent_at"),
        Index(
            "ix_notifications_recipient_subject_category_sent",
            "recipient_user_id",
            "subject_id",
            "category",
            "sent_at",
        ),
        Index(
            "ix_notifications_connection_recipient_dedupe",
            "integration_connection_id",
            "recipient_user_id",
            "dedupe_key",
        ),
        Index(
            "ix_notifications_connection_recipient_external",
            "integration_connection_id",
            "recipient_user_id",
            "external_id",
        ),
        Index("ix_notifications_ai_invocation_id", "ai_invocation_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    recipient_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    ai_invocation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    delivery_intent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    # Local wall-clock time (set by the caller via now_local(), not the DB clock)
    # — the budget window is a *local* calendar day.
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    # 'brief' | 'evening' | 'nudge' (count toward the budget) or 'reply' | 'echo'
    # (answers to the owner — never counted, or the bot would go mute mid-chat).
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    # Caller-composed idempotency key, usually carrying the date it belongs to
    # ("brief:2026-07-26"). NULL = this message is not deduped.
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # The channel's own message id (Telegram message_id) — the join key for replies.
    external_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Ordinary messages keep what was sent so a reply can use the exact visible
    # context. Platform-AI question answers deliberately persist only a bounded
    # {"content_redacted": true, "raw_payload_id": ...} provenance marker.
    payload: Mapped[Optional[Any]] = mapped_column(_JSON_TYPE, nullable=True)
