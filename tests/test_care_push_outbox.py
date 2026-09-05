"""Transactional, subject-isolated care-message notification claims."""

from __future__ import annotations

import base64
import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from vitals.access import PolicyAction, PolicyResourceType
from vitals.enums import (
    CarePushDeliveryErrorCode,
    CarePushDeliveryStatus,
    CareRelationshipStatus,
    ConsentStatus,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.care_thread import CareMessage, CareThread, CareThreadParticipant
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import (
    CareRelationship,
    ConsentGrant,
    ConsentScope,
    ProfessionalProfile,
)
from vitals.models.web_push import CarePushDelivery
from vitals.services.authorization.subject_access import resolve_access_context
from vitals.services.care import threads
from vitals.services.notifications import web_push_subscriptions
from vitals.utils.timeutils import now_utc


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _subscription(token: str) -> dict[str, str]:
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": f"https://fcm.googleapis.com/fcm/send/{token}",
        "p256dh": _b64(public),
        "auth": _b64(b"a" * 16),
    }


async def _user(session, slug: str, *, status: UserStatus = UserStatus.ACTIVE) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic",
        status=status.value,
    )
    session.add(user)
    await session.flush()
    return user


async def _thread(session, roots, *recipients: User) -> tuple[CareThread, object]:
    relationships = []
    verified_at = now_utc()
    for recipient in recipients:
        session.add_all(
            [
                UserRole(user_id=recipient.id, role=UserRoleName.DOCTOR.value),
                ProfessionalProfile(
                    user_id=recipient.id,
                    kind=ProfessionalKind.DOCTOR.value,
                    verification_status=ProfessionalVerificationStatus.VERIFIED.value,
                    display_name=recipient.username,
                    verified_at=verified_at,
                    verified_by_user_id=roots.user_id,
                ),
            ]
        )
        relationship = CareRelationship(
            subject_id=roots.subject_id,
            subject_owner_user_id=roots.user_id,
            professional_user_id=recipient.id,
            kind=ProfessionalKind.DOCTOR.value,
            status=CareRelationshipStatus.ACTIVE.value,
        )
        session.add(relationship)
        relationships.append(relationship)
    await session.flush()
    for relationship in relationships:
        grant = ConsentGrant(
            relationship_id=relationship.id,
            subject_id=roots.subject_id,
            version=1,
            status=ConsentStatus.ACTIVE.value,
            granted_at=verified_at,
            expires_at=verified_at + timedelta(days=365),
        )
        session.add(grant)
        await session.flush()
        session.add_all(
            [
                ConsentScope(
                    consent_grant_id=grant.id,
                    subject_id=roots.subject_id,
                    resource_type=PolicyResourceType.OPERATION.value,
                    resource_key=threads.MESSAGE_OPERATION,
                    action=action.value,
                )
                for action in (PolicyAction.READ, PolicyAction.MESSAGE)
            ]
        )
    thread = CareThread(
        subject_id=roots.subject_id,
        title="Synthetic notification room",
        opened_by_user_id=roots.user_id,
    )
    session.add(thread)
    await session.flush()
    session.add_all(
        [
            CareThreadParticipant(
                thread_id=thread.id,
                subject_id=roots.subject_id,
                user_id=roots.user_id,
            )
        ]
        + [
            CareThreadParticipant(
                thread_id=thread.id,
                subject_id=roots.subject_id,
                user_id=recipient.id,
                relationship_id=relationship.id,
            )
            for recipient, relationship in zip(recipients, relationships, strict=True)
        ]
    )
    await session.flush()
    context = await resolve_access_context(
        session, user_id=roots.user_id, subject_id=roots.subject_id
    )
    return thread, context


async def test_send_enqueues_each_current_recipient_device_but_never_the_author(
    db_session, legacy_owner_roots
):
    doctor = await _user(db_session, "push-outbox-doctor")
    trainer = await _user(db_session, "push-outbox-trainer")
    thread, context = await _thread(
        db_session, legacy_owner_roots, doctor, trainer
    )
    doctor_devices = [
        await web_push_subscriptions.register(
            db_session, user_id=doctor.id, **_subscription(f"doctor-{index}")
        )
        for index in range(2)
    ]
    trainer_device = await web_push_subscriptions.register(
        db_session, user_id=trainer.id, **_subscription("trainer")
    )
    await web_push_subscriptions.register(
        db_session,
        user_id=legacy_owner_roots.user_id,
        **_subscription("author"),
    )

    message = await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Private body"
    )
    deliveries = (
        await db_session.scalars(
            select(CarePushDelivery).order_by(CarePushDelivery.subscription_id)
        )
    ).all()

    assert len(deliveries) == 3
    assert {row.recipient_user_id for row in deliveries} == {doctor.id, trainer.id}
    assert {row.subscription_id for row in deliveries} == {
        *(device.id for device in doctor_devices),
        trainer_device.id,
    }
    assert {row.message_id for row in deliveries} == {message.id}
    assert {row.subject_id for row in deliveries} == {
        legacy_owner_roots.subject_id
    }
    assert {row.status for row in deliveries} == {
        CarePushDeliveryStatus.PENDING.value
    }


async def test_removed_inactive_and_revoked_recipients_are_not_enqueued(
    db_session, legacy_owner_roots
):
    removed = await _user(db_session, "push-outbox-removed")
    inactive = await _user(db_session, "push-outbox-inactive")
    revoked = await _user(db_session, "push-outbox-revoked")
    live = await _user(db_session, "push-outbox-live")
    thread, context = await _thread(
        db_session, legacy_owner_roots, removed, inactive, revoked, live
    )
    for user in (removed, inactive, revoked, live):
        await web_push_subscriptions.register(
            db_session, user_id=user.id, **_subscription(user.username)
        )
    removed_participation = await db_session.scalar(
        select(CareThreadParticipant).where(
            CareThreadParticipant.thread_id == thread.id,
            CareThreadParticipant.user_id == removed.id,
        )
    )
    removed_participation.removed_at = now_utc() + timedelta(seconds=1)
    inactive.status = UserStatus.SUSPENDED.value
    await web_push_subscriptions.revoke_all(db_session, user_id=revoked.id)

    await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="One recipient"
    )
    deliveries = (await db_session.scalars(select(CarePushDelivery))).all()
    assert [row.recipient_user_id for row in deliveries] == [live.id]


async def test_message_and_outbox_rollback_together(db_session, legacy_owner_roots):
    recipient = await _user(db_session, "push-outbox-rollback")
    thread, context = await _thread(db_session, legacy_owner_roots, recipient)
    await web_push_subscriptions.register(
        db_session, user_id=recipient.id, **_subscription("rollback")
    )
    await db_session.commit()
    thread_id = thread.id

    await threads.send_message(
        db_session, context=context, thread_id=thread_id, body="Roll me back"
    )
    assert await db_session.scalar(select(func.count(CarePushDelivery.id))) == 1
    await db_session.rollback()

    assert (
        await db_session.scalar(
            select(func.count(CareMessage.id)).where(CareMessage.thread_id == thread_id)
        )
        == 0
    )
    assert await db_session.scalar(select(func.count(CarePushDelivery.id))) == 0


async def test_later_device_enrollment_does_not_replay_message_history(
    db_session, legacy_owner_roots
):
    recipient = await _user(db_session, "push-outbox-no-replay")
    thread, context = await _thread(db_session, legacy_owner_roots, recipient)
    await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Before enrollment"
    )
    await db_session.commit()
    assert await db_session.scalar(select(func.count(CarePushDelivery.id))) == 0

    await web_push_subscriptions.register(
        db_session, user_id=recipient.id, **_subscription("later")
    )
    await db_session.commit()
    assert await db_session.scalar(select(func.count(CarePushDelivery.id))) == 0


@pytest.mark.integration
async def test_composite_foreign_keys_reject_cross_subject_delivery(
    db_session, legacy_owner_roots
):
    recipient = await _user(db_session, "push-outbox-cross-subject")
    thread, context = await _thread(db_session, legacy_owner_roots, recipient)
    message = await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Subject A"
    )
    subscription = await web_push_subscriptions.register(
        db_session, user_id=recipient.id, **_subscription("cross-subject")
    )
    other_owner = await _user(db_session, "push-outbox-other-owner")
    other_subject = HealthSubject(
        owner_user_id=other_owner.id,
        display_name="Other subject",
        timezone="Asia/Almaty",
    )
    db_session.add(other_subject)
    await db_session.flush()

    db_session.add(
        CarePushDelivery(
            subject_id=other_subject.id,
            message_id=message.id,
            subscription_id=subscription.id,
            recipient_user_id=recipient.id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
async def test_composite_foreign_keys_reject_subscription_recipient_mismatch(
    db_session, legacy_owner_roots
):
    recipient = await _user(db_session, "push-outbox-device-recipient")
    device_owner = await _user(db_session, "push-outbox-device-owner")
    thread, context = await _thread(db_session, legacy_owner_roots, recipient)
    message = await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Exact account"
    )
    subscription = await web_push_subscriptions.register(
        db_session, user_id=device_owner.id, **_subscription("wrong-account")
    )

    db_session.add(
        CarePushDelivery(
            subject_id=message.subject_id,
            message_id=message.id,
            subscription_id=subscription.id,
            recipient_user_id=recipient.id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_unique_claim_rejects_duplicate_message_recipient_device(
    db_session, legacy_owner_roots
):
    recipient = await _user(db_session, "push-outbox-duplicate")
    thread, context = await _thread(db_session, legacy_owner_roots, recipient)
    subscription = await web_push_subscriptions.register(
        db_session, user_id=recipient.id, **_subscription("duplicate")
    )
    message = await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Only once"
    )

    db_session.add(
        CarePushDelivery(
            subject_id=message.subject_id,
            message_id=message.id,
            subscription_id=subscription.id,
            recipient_user_id=recipient.id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    ("status", "lease_token", "started", "completed", "error_code"),
    [
        (CarePushDeliveryStatus.SENT, None, None, now_utc(), None),
        (
            CarePushDeliveryStatus.CANCELLED,
            uuid.uuid4(),
            now_utc(),
            now_utc(),
            CarePushDeliveryErrorCode.ACCESS_REVOKED,
        ),
        (
            CarePushDeliveryStatus.CANCELLED,
            None,
            None,
            now_utc(),
            CarePushDeliveryErrorCode.PROVIDER_GONE,
        ),
        (
            CarePushDeliveryStatus.AMBIGUOUS,
            uuid.uuid4(),
            now_utc(),
            now_utc(),
            CarePushDeliveryErrorCode.ACCESS_REVOKED,
        ),
    ],
)
async def test_lifecycle_constraints_reject_impossible_delivery_states(
    db_session,
    legacy_owner_roots,
    status,
    lease_token,
    started,
    completed,
    error_code,
):
    case_id = uuid.uuid4().hex[:8]
    recipient = await _user(db_session, f"push-state-{case_id}")
    thread, context = await _thread(db_session, legacy_owner_roots, recipient)
    await web_push_subscriptions.register(
        db_session,
        user_id=recipient.id,
        **_subscription(f"state-{case_id}"),
    )
    message = await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="State guard"
    )
    queued = await db_session.scalar(
        select(CarePushDelivery).where(CarePushDelivery.message_id == message.id)
    )
    queued.status = status.value
    queued.lease_token = lease_token
    queued.dispatch_started_at = started
    queued.completed_at = completed
    queued.error_code = error_code.value if error_code is not None else None

    with pytest.raises(IntegrityError):
        await db_session.flush()


def test_outbox_schema_has_no_phi_or_provider_payload_columns():
    assert set(CarePushDelivery.__table__.columns.keys()) == {
        "id",
        "subject_id",
        "message_id",
        "subscription_id",
        "recipient_user_id",
        "status",
        "lease_token",
        "dispatch_started_at",
        "completed_at",
        "error_code",
        "created_at",
        "updated_at",
    }
