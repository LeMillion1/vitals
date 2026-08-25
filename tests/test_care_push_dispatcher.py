"""At-most-once Web Push dispatch with consent and device revalidation."""

from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    CareRelationshipStatus,
    CarePushDeliveryErrorCode,
    CarePushDeliveryStatus,
    ConsentStatus,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.integrations.web_push import (
    WebPushProtocolError,
    WebPushProviderOutcome,
    WebPushProviderResult,
    WebPushTransportError,
)
from vitals.models.care_thread import CareMessage, CareThread, CareThreadParticipant
from vitals.models.identity import User, UserRole
from vitals.models.professional import (
    CareRelationship,
    ConsentGrant,
    ConsentScope,
    ProfessionalProfile,
)
from vitals.models.web_push import CarePushDelivery
from vitals.persistence.rls import enter_platform_scope
from vitals.services import credential_vault_service
from vitals.services.care import professionals
from vitals.services.notifications import care_push_dispatcher as dispatcher
from vitals.services.notifications import web_push_subscriptions
from vitals.utils.timeutils import now_utc


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _subscription(token: str = "dispatch-device") -> dict[str, str]:
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": f"https://fcm.googleapis.com/fcm/send/{token}",
        "p256dh": _b64(public),
        "auth": _b64(b"d" * 16),
    }


async def _queued_for_owner(db_session, roots, *, token="dispatch-device"):
    author = User(
        username=f"author-{token}",
        normalized_username=f"author-{token}",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(author)
    await db_session.flush()
    thread = CareThread(
        subject_id=roots.subject_id,
        title="Synthetic dispatch room",
        opened_by_user_id=roots.user_id,
    )
    db_session.add(thread)
    await db_session.flush()
    db_session.add(
        CareThreadParticipant(
            thread_id=thread.id,
            subject_id=roots.subject_id,
            user_id=roots.user_id,
        )
    )
    message = CareMessage(
        thread_id=thread.id,
        subject_id=roots.subject_id,
        actor_user_id=author.id,
        body="Sensitive synthetic message body",
    )
    db_session.add(message)
    subscription_values = _subscription(token)
    subscription = await web_push_subscriptions.register(
        db_session,
        user_id=roots.user_id,
        **subscription_values,
    )
    await db_session.flush()
    delivery = CarePushDelivery(
        subject_id=roots.subject_id,
        message_id=message.id,
        subscription_id=subscription.id,
        recipient_user_id=roots.user_id,
    )
    db_session.add(delivery)
    await db_session.commit()
    return delivery, subscription, subscription_values


async def _queued_for_professional(db_session, roots, *, token: str):
    doctor = User(
        username=f"doctor-{token}",
        normalized_username=f"doctor-{token}",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(doctor)
    await db_session.flush()
    role = UserRole(user_id=doctor.id, role=UserRoleName.DOCTOR.value)
    operator = User(
        username=f"operator-{token}",
        normalized_username=f"operator-{token}",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(operator)
    await db_session.flush()
    db_session.add_all(
        [
            role,
            UserRole(
                user_id=operator.id,
                role=UserRoleName.PLATFORM_SUPERADMIN.value,
            ),
        ]
    )
    await db_session.flush()
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name=f"Dr {token}",
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        status="verified",
    )
    relationship = CareRelationship(
        subject_id=roots.subject_id,
        subject_owner_user_id=roots.user_id,
        professional_user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR.value,
        status=CareRelationshipStatus.ACTIVE.value,
    )
    db_session.add(relationship)
    await db_session.flush()
    current = now_utc()
    grant = ConsentGrant(
        relationship_id=relationship.id,
        subject_id=roots.subject_id,
        version=1,
        status=ConsentStatus.ACTIVE.value,
        expires_at=current + timedelta(days=30),
    )
    db_session.add(grant)
    await db_session.flush()
    db_session.add(
        ConsentScope(
            consent_grant_id=grant.id,
            subject_id=roots.subject_id,
            resource_type="operation",
            resource_key="care_team.message",
            action="read",
        )
    )
    thread = CareThread(
        subject_id=roots.subject_id,
        title="Professional dispatch room",
        opened_by_user_id=roots.user_id,
    )
    db_session.add(thread)
    await db_session.flush()
    db_session.add(
        CareThreadParticipant(
            thread_id=thread.id,
            subject_id=roots.subject_id,
            user_id=doctor.id,
            relationship_id=relationship.id,
        )
    )
    message = CareMessage(
        thread_id=thread.id,
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        body="Synthetic patient reply",
    )
    db_session.add(message)
    values = _subscription(token)
    subscription = await web_push_subscriptions.register(
        db_session, user_id=doctor.id, **values
    )
    await db_session.flush()
    delivery = CarePushDelivery(
        subject_id=roots.subject_id,
        message_id=message.id,
        subscription_id=subscription.id,
        recipient_user_id=doctor.id,
    )
    db_session.add(delivery)
    await db_session.commit()
    return delivery, doctor, role, relationship, grant


def _factory(db_session) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )


class _FakeClient:
    def __init__(self, result):
        self.result = result
        self.targets = []

    async def send_care_message_wakeup(self, target):
        self.targets.append(target)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _enable(monkeypatch, fake):
    monkeypatch.setattr(
        dispatcher.web_push_config,
        "load_config",
        lambda: SimpleNamespace(private_key="redacted", subject="mailto:x@test"),
    )
    monkeypatch.setattr(dispatcher, "WebPushClient", lambda _config: fake)


@pytest.mark.parametrize(
    ("provider", "expected_status", "expected_error", "revoked"),
    [
        (
            WebPushProviderOutcome.ACCEPTED,
            CarePushDeliveryStatus.SENT,
            None,
            False,
        ),
        (
            WebPushProviderOutcome.GONE,
            CarePushDeliveryStatus.CANCELLED,
            CarePushDeliveryErrorCode.PROVIDER_GONE,
            True,
        ),
        (
            WebPushProviderOutcome.REJECTED,
            CarePushDeliveryStatus.CANCELLED,
            CarePushDeliveryErrorCode.PROVIDER_REJECTED,
            False,
        ),
        (
            WebPushProviderOutcome.AMBIGUOUS,
            CarePushDeliveryStatus.AMBIGUOUS,
            CarePushDeliveryErrorCode.TRANSPORT_ERROR,
            False,
        ),
    ],
)
async def test_provider_outcomes_are_terminal_and_never_retried(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    provider,
    expected_status,
    expected_error,
    revoked,
):
    delivery, subscription, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token=provider.value
    )
    status_code = {
        WebPushProviderOutcome.ACCEPTED: 201,
        WebPushProviderOutcome.GONE: 410,
        WebPushProviderOutcome.REJECTED: 400,
        WebPushProviderOutcome.AMBIGUOUS: 503,
    }[provider]
    fake = _FakeClient(WebPushProviderResult(provider, status_code))
    _enable(monkeypatch, fake)

    await dispatcher.dispatch_job(_factory(db_session))
    await dispatcher.dispatch_job(_factory(db_session))

    await db_session.rollback()
    await db_session.refresh(delivery)
    await db_session.refresh(subscription)
    stored = delivery
    device = subscription
    assert stored.status == expected_status.value
    assert stored.error_code == (expected_error.value if expected_error else None)
    assert stored.completed_at is not None
    assert len(fake.targets) == 1
    assert (device.revoked_at is not None) is revoked
    assert (device.ciphertext is None) is revoked
    if provider is WebPushProviderOutcome.ACCEPTED:
        assert device.last_success_at is not None


@pytest.mark.parametrize(
    ("error", "expected", "raises_job"),
    [
        (
            WebPushProtocolError("must not persist"),
            CarePushDeliveryErrorCode.INVALID_RESPONSE,
            False,
        ),
        (
            WebPushTransportError("must not persist"),
            CarePushDeliveryErrorCode.TRANSPORT_ERROR,
            False,
        ),
        (
            RuntimeError("endpoint and body must not persist"),
            CarePushDeliveryErrorCode.INTERNAL_ERROR,
            True,
        ),
    ],
)
async def test_sanitized_failures_become_ambiguous_once(
    db_session, legacy_owner_roots, monkeypatch, error, expected, raises_job
):
    delivery, _subscription_row, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token=expected.value
    )
    fake = _FakeClient(error)
    _enable(monkeypatch, fake)

    if raises_job:
        with pytest.raises(
            dispatcher.CarePushDispatchError,
            match="internal delivery error",
        ):
            await dispatcher.dispatch_job(_factory(db_session))
    else:
        await dispatcher.dispatch_job(_factory(db_session))
    await dispatcher.dispatch_job(_factory(db_session))

    await db_session.rollback()
    await db_session.refresh(delivery)
    stored = delivery
    assert stored.status == CarePushDeliveryStatus.AMBIGUOUS.value
    assert stored.error_code == expected.value
    assert len(fake.targets) == 1
    assert "must not persist" not in str(stored.__dict__)


async def test_disabled_transport_reconciles_but_does_not_claim_fresh_work(
    db_session, legacy_owner_roots, monkeypatch
):
    delivery, _subscription_row, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="disabled"
    )
    monkeypatch.setattr(dispatcher.web_push_config, "load_config", lambda: None)

    await dispatcher.dispatch_job(_factory(db_session))

    await db_session.rollback()
    await db_session.refresh(delivery)
    stored = delivery
    assert stored.status == CarePushDeliveryStatus.PENDING.value


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("inactive", CarePushDeliveryErrorCode.ACCOUNT_INACTIVE),
        ("removed", CarePushDeliveryErrorCode.ACCESS_REVOKED),
        ("revoked_device", CarePushDeliveryErrorCode.SUBSCRIPTION_REVOKED),
    ],
)
async def test_claim_rechecks_account_participation_and_subscription(
    db_session, legacy_owner_roots, mutation, expected
):
    delivery, subscription, values = await _queued_for_owner(
        db_session, legacy_owner_roots, token=mutation
    )
    if mutation == "inactive":
        owner = await db_session.get(User, legacy_owner_roots.user_id)
        owner.status = UserStatus.SUSPENDED.value
    elif mutation == "removed":
        participant = await db_session.scalar(
            select(CareThreadParticipant).where(
                CareThreadParticipant.user_id == legacy_owner_roots.user_id
            )
        )
        participant.removed_at = now_utc() + timedelta(seconds=1)
    else:
        await web_push_subscriptions.revoke_endpoint(
            db_session, user_id=legacy_owner_roots.user_id, endpoint=values["endpoint"]
        )
    await db_session.commit()

    await enter_platform_scope(db_session)
    claims = await dispatcher.claim_batch(db_session)
    await db_session.commit()

    await db_session.refresh(delivery)
    assert claims == ()
    assert delivery.status == CarePushDeliveryStatus.CANCELLED.value
    assert delivery.error_code == expected.value
    if mutation == "revoked_device":
        await db_session.refresh(subscription)
        assert subscription.ciphertext is None


async def test_a_new_relationship_cannot_revive_an_old_room_notification(
    db_session, legacy_owner_roots
):
    delivery, doctor, _role, old_relationship, old_grant = (
        await _queued_for_professional(
            db_session, legacy_owner_roots, token="new-relationship"
        )
    )
    current = now_utc()
    old_relationship.status = CareRelationshipStatus.ENDED.value
    old_relationship.ended_at = current
    old_relationship.ended_by_user_id = legacy_owner_roots.user_id
    old_grant.status = ConsentStatus.REVOKED.value
    old_grant.revoked_at = current
    replacement = CareRelationship(
        subject_id=legacy_owner_roots.subject_id,
        subject_owner_user_id=legacy_owner_roots.user_id,
        professional_user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR.value,
        status=CareRelationshipStatus.ACTIVE.value,
    )
    db_session.add(replacement)
    await db_session.flush()
    replacement_grant = ConsentGrant(
        relationship_id=replacement.id,
        subject_id=legacy_owner_roots.subject_id,
        version=1,
        status=ConsentStatus.ACTIVE.value,
        expires_at=current + timedelta(days=30),
    )
    db_session.add(replacement_grant)
    await db_session.flush()
    db_session.add(
        ConsentScope(
            consent_grant_id=replacement_grant.id,
            subject_id=legacy_owner_roots.subject_id,
            resource_type="operation",
            resource_key="care_team.message",
            action="read",
        )
    )
    await db_session.commit()

    await enter_platform_scope(db_session)
    assert await dispatcher.claim_batch(db_session) == ()
    await db_session.commit()

    await db_session.refresh(delivery)
    assert delivery.status == CarePushDeliveryStatus.CANCELLED.value
    assert delivery.error_code == CarePushDeliveryErrorCode.ACCESS_REVOKED.value


async def test_revoked_professional_role_stops_background_push(
    db_session, legacy_owner_roots
):
    delivery, _doctor, role, _relationship, _grant = (
        await _queued_for_professional(
            db_session, legacy_owner_roots, token="revoked-role"
        )
    )
    await db_session.delete(role)
    await db_session.commit()

    await enter_platform_scope(db_session)
    assert await dispatcher.claim_batch(db_session) == ()
    await db_session.commit()

    await db_session.refresh(delivery)
    assert delivery.status == CarePushDeliveryStatus.CANCELLED.value
    assert delivery.error_code == CarePushDeliveryErrorCode.ACCESS_REVOKED.value


async def test_suspended_professional_profile_stops_background_push(
    db_session, legacy_owner_roots
):
    delivery, doctor, _role, _relationship, _grant = (
        await _queued_for_professional(
            db_session, legacy_owner_roots, token="suspended-profile"
        )
    )
    profile = await db_session.scalar(
        select(ProfessionalProfile).where(ProfessionalProfile.user_id == doctor.id)
    )
    reviewer_id = await db_session.scalar(
        select(UserRole.user_id).where(
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value
        )
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=reviewer_id,
        status=ProfessionalVerificationStatus.SUSPENDED,
        note="synthetic licence withdrawal",
    )
    await db_session.commit()

    await enter_platform_scope(db_session)
    assert await dispatcher.claim_batch(db_session) == ()
    await db_session.commit()

    await db_session.refresh(delivery)
    assert delivery.status == CarePushDeliveryStatus.CANCELLED.value
    assert delivery.error_code == CarePushDeliveryErrorCode.ACCESS_REVOKED.value


async def test_unavailable_vault_rolls_back_and_keeps_pending_credential(
    db_session, legacy_owner_roots, monkeypatch
):
    delivery, subscription, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="vault-unavailable"
    )
    original_ciphertext = bytes(subscription.ciphertext)
    monkeypatch.delenv(credential_vault_service.CREDENTIAL_KEY_ENV, raising=False)

    await enter_platform_scope(db_session)
    with pytest.raises(credential_vault_service.CredentialVaultUnavailable):
        await dispatcher.claim_batch(db_session)
    await db_session.rollback()

    await db_session.refresh(delivery)
    await db_session.refresh(subscription)
    assert delivery.status == CarePushDeliveryStatus.PENDING.value
    assert bytes(subscription.ciphertext) == original_ciphertext
    assert subscription.revoked_at is None


async def test_corrupt_credential_is_erased_and_cancelled_before_network(
    db_session, legacy_owner_roots
):
    delivery, subscription, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="corrupt"
    )
    tampered = bytearray(bytes(subscription.ciphertext))
    tampered[-1] ^= 1
    subscription.ciphertext = bytes(tampered)
    await db_session.commit()

    await enter_platform_scope(db_session)
    assert await dispatcher.claim_batch(db_session) == ()
    await db_session.commit()

    await db_session.refresh(delivery)
    await db_session.refresh(subscription)
    assert delivery.error_code == CarePushDeliveryErrorCode.SUBSCRIPTION_REVOKED.value
    assert subscription.revoked_at is not None
    assert subscription.ciphertext is None


async def test_stale_rows_are_terminalized_without_network(
    db_session, legacy_owner_roots
):
    pending, _subscription_row, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="stale"
    )
    current = now_utc()
    pending.created_at = current - dispatcher.PENDING_STALE_AFTER - timedelta(seconds=1)
    dispatching = CarePushDelivery(
        subject_id=pending.subject_id,
        message_id=pending.message_id,
        subscription_id=pending.subscription_id,
        recipient_user_id=pending.recipient_user_id,
        status=CarePushDeliveryStatus.DISPATCHING.value,
        lease_token=uuid.uuid4(),
        dispatch_started_at=current
        - dispatcher.DISPATCHING_STALE_AFTER
        - timedelta(seconds=1),
    )
    # Reuse is impossible because of the unique message/device claim; create a
    # sibling message with the same exact subject/thread roots instead.
    source_message = await db_session.get(CareMessage, pending.message_id)
    sibling = CareMessage(
        thread_id=source_message.thread_id,
        subject_id=source_message.subject_id,
        actor_user_id=source_message.actor_user_id,
        body="Second synthetic message",
    )
    db_session.add(sibling)
    await db_session.flush()
    dispatching.message_id = sibling.id
    db_session.add(dispatching)
    await db_session.commit()

    await enter_platform_scope(db_session)
    changed = await dispatcher.reconcile_stale(db_session, at=current)
    await db_session.commit()

    await db_session.refresh(pending)
    await db_session.refresh(dispatching)
    assert changed == 2
    assert pending.status == CarePushDeliveryStatus.CANCELLED.value
    assert pending.error_code == CarePushDeliveryErrorCode.STALE_PENDING.value
    assert dispatching.status == CarePushDeliveryStatus.AMBIGUOUS.value
    assert dispatching.error_code == CarePushDeliveryErrorCode.STALE_DISPATCH.value


async def test_stale_lease_cannot_finalize(db_session, legacy_owner_roots):
    delivery, _subscription_row, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="lease"
    )
    await enter_platform_scope(db_session)
    (claim,) = await dispatcher.claim_batch(db_session)
    await db_session.commit()

    completion = await dispatcher.dispatch_claim(
        _FakeClient(
            WebPushProviderResult(WebPushProviderOutcome.ACCEPTED, 201)
        ),
        claim,
    )
    wrong = replace(completion, lease_token=uuid.uuid4())
    with pytest.raises(dispatcher.CarePushLeaseError, match="forged"):
        await dispatcher.finalize(db_session, completion=wrong)

    delivery.lease_token = uuid.uuid4()
    await db_session.commit()
    assert not await dispatcher.finalize(db_session, completion=completion)
    await db_session.rollback()

    await db_session.refresh(delivery)
    assert delivery.status == CarePushDeliveryStatus.DISPATCHING.value


async def test_late_gone_does_not_revoke_refreshed_device_generation(
    db_session, legacy_owner_roots
):
    delivery, subscription, values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="refresh-race"
    )
    await enter_platform_scope(db_session)
    (claim,) = await dispatcher.claim_batch(db_session)
    await db_session.commit()

    completion = await dispatcher.dispatch_claim(
        _FakeClient(WebPushProviderResult(WebPushProviderOutcome.GONE, 410)),
        claim,
    )
    refreshed = await web_push_subscriptions.register(
        db_session, user_id=legacy_owner_roots.user_id, **values
    )
    await db_session.commit()
    assert refreshed.id == subscription.id

    await enter_platform_scope(db_session)
    assert await dispatcher.finalize(
        db_session,
        completion=completion,
    )
    await db_session.commit()

    await db_session.refresh(delivery)
    await db_session.refresh(subscription)
    assert delivery.error_code == CarePushDeliveryErrorCode.PROVIDER_GONE.value
    assert subscription.revoked_at is None
    assert subscription.ciphertext is not None


async def test_claim_refuses_network_before_commit_and_after_rollback(
    db_session, legacy_owner_roots
):
    delivery, _subscription_row, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="transaction-bound"
    )
    await enter_platform_scope(db_session)
    (claim,) = await dispatcher.claim_batch(db_session)
    fake = _FakeClient(
        WebPushProviderResult(WebPushProviderOutcome.ACCEPTED, 201)
    )

    with pytest.raises(dispatcher.CarePushLeaseError, match="not committed"):
        await dispatcher.dispatch_claim(fake, claim)
    await db_session.rollback()
    with pytest.raises(dispatcher.CarePushLeaseError, match="rolled back"):
        await dispatcher.dispatch_claim(fake, claim)

    await db_session.refresh(delivery)
    assert delivery.status == CarePushDeliveryStatus.PENDING.value
    assert fake.targets == []


async def test_committed_claim_can_contact_provider_only_once(
    db_session, legacy_owner_roots
):
    delivery, _subscription_row, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="one-shot"
    )
    await enter_platform_scope(db_session)
    (claim,) = await dispatcher.claim_batch(db_session)
    await db_session.commit()
    fake = _FakeClient(
        WebPushProviderResult(WebPushProviderOutcome.ACCEPTED, 201)
    )

    completion = await dispatcher.dispatch_claim(fake, claim)
    with pytest.raises(dispatcher.CarePushLeaseError, match="already consumed"):
        await dispatcher.dispatch_claim(fake, claim)
    assert await dispatcher.finalize(db_session, completion=completion)
    await db_session.commit()

    await db_session.refresh(delivery)
    assert delivery.status == CarePushDeliveryStatus.SENT.value
    assert len(fake.targets) == 1


async def test_savepoint_commit_does_not_activate_outer_claim(
    db_session, legacy_owner_roots
):
    delivery, _subscription_row, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="savepoint"
    )
    await enter_platform_scope(db_session)
    (claim,) = await dispatcher.claim_batch(db_session)
    async with db_session.begin_nested():
        pass
    fake = _FakeClient(
        WebPushProviderResult(WebPushProviderOutcome.ACCEPTED, 201)
    )

    with pytest.raises(dispatcher.CarePushLeaseError, match="not committed"):
        await dispatcher.dispatch_claim(fake, claim)
    await db_session.commit()
    completion = await dispatcher.dispatch_claim(fake, claim)
    assert await dispatcher.finalize(db_session, completion=completion)
    await db_session.commit()

    await db_session.refresh(delivery)
    assert delivery.status == CarePushDeliveryStatus.SENT.value
    assert len(fake.targets) == 1


async def test_claim_batch_rejects_nested_transaction(
    db_session, legacy_owner_roots
):
    delivery, _subscription_row, _values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="nested-claim"
    )
    await enter_platform_scope(db_session)
    async with db_session.begin_nested():
        with pytest.raises(
            dispatcher.CarePushLeaseError, match="outer transaction"
        ):
            await dispatcher.claim_batch(db_session)
    await db_session.rollback()

    await db_session.refresh(delivery)
    assert delivery.status == CarePushDeliveryStatus.PENDING.value


async def test_completion_cannot_be_substituted_between_claims(
    db_session, legacy_owner_roots
):
    first, first_subscription, _first_values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="sealed-first"
    )
    second, second_subscription, _second_values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="sealed-second"
    )
    await enter_platform_scope(db_session)
    claims = await dispatcher.claim_batch(db_session, limit=2)
    await db_session.commit()
    claims_by_delivery = {claim.delivery_id: claim for claim in claims}
    first_completion = await dispatcher.dispatch_claim(
        _FakeClient(
            WebPushProviderResult(WebPushProviderOutcome.ACCEPTED, 201)
        ),
        claims_by_delivery[first.id],
    )
    second_completion = await dispatcher.dispatch_claim(
        _FakeClient(WebPushProviderResult(WebPushProviderOutcome.GONE, 410)),
        claims_by_delivery[second.id],
    )
    forged = replace(
        first_completion,
        subscription_id=second_completion.subscription_id,
        recipient_user_id=second_completion.recipient_user_id,
        generation=second_completion.generation,
    )

    with pytest.raises(dispatcher.CarePushLeaseError, match="forged"):
        await dispatcher.finalize(db_session, completion=forged)
    assert await dispatcher.finalize(db_session, completion=first_completion)
    await db_session.commit()

    await db_session.refresh(first_subscription)
    await db_session.refresh(second_subscription)
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.status == CarePushDeliveryStatus.SENT.value
    assert first_subscription.last_success_at is not None
    assert second.status == CarePushDeliveryStatus.DISPATCHING.value
    assert second_subscription.last_success_at is None
    assert second_subscription.revoked_at is None


def test_claim_repr_redacts_every_identifier_and_device_secret():
    assert all(
        field.repr is False
        for field in dispatcher.CarePushClaim.__dataclass_fields__.values()
    )
    assert all(
        field.repr is False
        for field in dispatcher.CarePushCompletion.__dataclass_fields__.values()
    )


@pytest.mark.integration
async def test_postgres_claimers_serialize_without_duplicate_leases(
    db_session, legacy_owner_roots
):
    first, _first_subscription, _first_values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="concurrent-first"
    )
    second, _second_subscription, _second_values = await _queued_for_owner(
        db_session, legacy_owner_roots, token="concurrent-second"
    )
    factory = _factory(db_session)

    async with factory() as session_one, factory() as session_two:
        await enter_platform_scope(session_one)
        await enter_platform_scope(session_two)
        (claim_one,) = await dispatcher.claim_batch(session_one, limit=1)
        waiting = asyncio.create_task(dispatcher.claim_batch(session_two, limit=1))
        await asyncio.sleep(0.05)
        assert not waiting.done()

        await session_one.commit()
        (claim_two,) = await asyncio.wait_for(waiting, timeout=2)
        await session_two.commit()

    assert {claim_one.delivery_id, claim_two.delivery_id} == {first.id, second.id}
    assert claim_one.lease_token != claim_two.lease_token


@pytest.mark.integration
async def test_postgres_claim_fences_a_concurrent_profile_suspension(
    db_session, legacy_owner_roots, monkeypatch
):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory-lock semantics")
    _delivery, doctor, _role, _relationship, _grant = (
        await _queued_for_professional(
            db_session, legacy_owner_roots, token="profile-fence"
        )
    )
    profile_id = await db_session.scalar(
        select(ProfessionalProfile.id).where(
            ProfessionalProfile.user_id == doctor.id
        )
    )
    reviewer_id = await db_session.scalar(
        select(UserRole.user_id).where(
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value
        )
    )
    factory = _factory(db_session)

    async with factory() as claim_session, factory() as review_session:
        await enter_platform_scope(claim_session)
        (claim,) = await dispatcher.claim_batch(claim_session)
        attempted = asyncio.Event()
        original_lock = professionals.acquire_identity_governance_lock

        async def observed_lock(session):
            attempted.set()
            return await original_lock(session)

        monkeypatch.setattr(
            professionals, "acquire_identity_governance_lock", observed_lock
        )
        suspension = asyncio.create_task(
            professionals.decide(
                review_session,
                profile_id=profile_id,
                reviewer_user_id=reviewer_id,
                status=ProfessionalVerificationStatus.SUSPENDED,
                note="synthetic concurrent suspension",
            )
        )
        await asyncio.wait_for(attempted.wait(), timeout=2)
        assert not suspension.done()

        # The committed authorization claim wins this ordering.  Its payload is
        # generic and sealed for one attempt; the suspension fences every later
        # claim rather than rewriting a decision already committed.
        await claim_session.commit()
        await asyncio.wait_for(suspension, timeout=2)
        await review_session.commit()

    completion = await dispatcher.dispatch_claim(
        _FakeClient(WebPushProviderResult(WebPushProviderOutcome.ACCEPTED, 201)),
        claim,
    )
    assert completion.outcome.status is CarePushDeliveryStatus.SENT
