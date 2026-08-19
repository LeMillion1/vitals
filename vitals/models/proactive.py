"""``notifications`` — the journal of everything the app has sent out.

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
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vitals.models.base import Base
from vitals.models.ownership_mixins import (
    IntegrationConnectionOwnershipMixin,
    OriginActorMixin,
    SubjectOwnershipMixin,
)

_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


class Notification(
    Base,
    SubjectOwnershipMixin,
    OriginActorMixin,
    IntegrationConnectionOwnershipMixin,
):
    __tablename__ = "notifications"
    __table_args__ = (
        # One row per dedupe_key, ever. Rows without a key (replies, echoes) don't
        # participate — hence partial, not a plain UNIQUE that NULLs would skip
        # silently on Postgres anyway.
        Index(
            "uq_notification_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
            sqlite_where=text("dedupe_key IS NOT NULL"),
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
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    recipient_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
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
    # What was actually sent: {"text": ..., "buttons": [...]}. Kept so a reply can
    # be answered against the exact context the owner is looking at.
    payload: Mapped[Optional[Any]] = mapped_column(_JSON_TYPE, nullable=True)
