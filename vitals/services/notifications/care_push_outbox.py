"""Create PHI-free care-message notification claims in the message transaction.

Unread state remains the durable user-facing truth.  These rows are only a
best-effort wakeup for devices that were already enrolled when the message was
written: adding a subscription later never replays conversation history.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserStatus
from vitals.models.care_thread import CareMessage, CareThreadParticipant
from vitals.models.identity import User
from vitals.models.web_push import CarePushDelivery, WebPushSubscription


async def enqueue_for_message(
    session: AsyncSession, *, message: CareMessage
) -> tuple[CarePushDelivery, ...]:
    """Enqueue one row per current recipient device. Never commits.

    Only active participants and currently active subscriptions are selected,
    and the author is excluded.  Authorization is deliberately rechecked again
    by the future dispatcher: consent or care can change after this transaction
    commits and before a provider is contacted.
    """

    if message.id is None or message.subject_id is None or message.thread_id is None:
        raise ValueError("message must be persistent and subject-scoped")

    recipients = (
        await session.execute(
            select(CareThreadParticipant.user_id, WebPushSubscription.id)
            .join(User, User.id == CareThreadParticipant.user_id)
            .join(
                WebPushSubscription,
                WebPushSubscription.user_id == CareThreadParticipant.user_id,
            )
            .where(
                CareThreadParticipant.thread_id == message.thread_id,
                CareThreadParticipant.subject_id == message.subject_id,
                CareThreadParticipant.removed_at.is_(None),
                CareThreadParticipant.user_id != message.actor_user_id,
                User.status == UserStatus.ACTIVE.value,
                WebPushSubscription.revoked_at.is_(None),
            )
            .order_by(CareThreadParticipant.user_id, WebPushSubscription.id)
        )
    ).all()
    deliveries = tuple(
        CarePushDelivery(
            subject_id=message.subject_id,
            message_id=message.id,
            recipient_user_id=recipient_user_id,
            subscription_id=subscription_id,
        )
        for recipient_user_id, subscription_id in recipients
    )
    if deliveries:
        session.add_all(deliveries)
        await session.flush()
    return deliveries


__all__ = ["enqueue_for_message"]
