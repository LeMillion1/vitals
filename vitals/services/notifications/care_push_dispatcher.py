"""Consent-rechecked, at-most-once dispatch for generic care wakeups.

The database claim commits before provider I/O and no uncertain attempt ever
returns to ``pending``.  This deliberately chooses a missed wakeup over a
duplicate lock-screen alert: unread conversation state is durable in Vitals,
while Web Push is only a generic hint to open that inbox.

Every network attempt is preceded by fresh checks of the recipient account,
thread participation, the exact relationship that admitted a professional,
versioned consent, and the exact encrypted device generation.  No database
session remains open while the provider is contacted.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import PolicyAction, PolicyResourceType
from vitals.enums import (
    CarePushDeliveryErrorCode,
    CarePushDeliveryStatus,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.integrations.web_push import (
    InvalidWebPushTarget,
    WebPushClient,
    WebPushProtocolError,
    WebPushProviderOutcome,
    WebPushProviderResult,
    WebPushTarget,
    WebPushTransportError,
)
from vitals.models.care_thread import CareMessage, CareThreadParticipant
from vitals.models.identity import User
from vitals.models.professional import CareRelationship, ProfessionalProfile
from vitals.models.web_push import CarePushDelivery
from vitals.persistence.rls import enter_platform_scope
from vitals.services.authorization.subject_access import (
    AccessResolutionError,
    require_access,
    resolve_access_context,
)
from vitals.services.care.threads import MESSAGE_OPERATION
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.notifications import web_push_config, web_push_subscriptions
from vitals.services.notifications.web_push_subscriptions import (
    SubscriptionGeneration,
)
from vitals.utils.timeutils import now_utc

BATCH_SIZE = 10
RECONCILIATION_BATCH_SIZE = 100
PENDING_STALE_AFTER = timedelta(hours=24)
DISPATCHING_STALE_AFTER = timedelta(minutes=5)


class CarePushDispatchError(RuntimeError):
    """A sanitized platform failure after every leased row was made terminal."""


class CarePushLeaseError(RuntimeError):
    """A claim or completion was used outside its exact transaction lifecycle."""


_SEAL_KEY = secrets.token_bytes(32)


@dataclass(slots=True)
class _ClaimLifecycle:
    target: WebPushTarget | None = field(repr=False)
    committed: bool = False
    invalid: bool = False
    consumed: bool = False


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CarePushClaim:
    """A redacted one-shot lease activated only by the claiming commit."""

    delivery_id: uuid.UUID = field(repr=False)
    subject_id: uuid.UUID = field(repr=False)
    subscription_id: uuid.UUID = field(repr=False)
    recipient_user_id: uuid.UUID = field(repr=False)
    lease_token: uuid.UUID = field(repr=False)
    generation: SubscriptionGeneration = field(repr=False)
    _seal: bytes = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CarePushCompletion:
    """A sealed post-I/O result that cannot retain the decrypted target."""

    delivery_id: uuid.UUID = field(repr=False)
    subject_id: uuid.UUID = field(repr=False)
    subscription_id: uuid.UUID = field(repr=False)
    recipient_user_id: uuid.UUID = field(repr=False)
    lease_token: uuid.UUID = field(repr=False)
    generation: SubscriptionGeneration = field(repr=False)
    outcome: _TerminalOutcome = field(repr=False)
    _seal: bytes = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _TerminalOutcome:
    status: CarePushDeliveryStatus
    error_code: CarePushDeliveryErrorCode | None


_SENT = _TerminalOutcome(CarePushDeliveryStatus.SENT, None)
_GONE = _TerminalOutcome(
    CarePushDeliveryStatus.CANCELLED,
    CarePushDeliveryErrorCode.PROVIDER_GONE,
)
_REJECTED = _TerminalOutcome(
    CarePushDeliveryStatus.CANCELLED,
    CarePushDeliveryErrorCode.PROVIDER_REJECTED,
)
_TRANSPORT_ERROR = _TerminalOutcome(
    CarePushDeliveryStatus.AMBIGUOUS,
    CarePushDeliveryErrorCode.TRANSPORT_ERROR,
)
_INVALID_RESPONSE = _TerminalOutcome(
    CarePushDeliveryStatus.AMBIGUOUS,
    CarePushDeliveryErrorCode.INVALID_RESPONSE,
)
_INTERNAL_ERROR = _TerminalOutcome(
    CarePushDeliveryStatus.AMBIGUOUS,
    CarePushDeliveryErrorCode.INTERNAL_ERROR,
)

_CLAIM_LIFECYCLES: weakref.WeakKeyDictionary[CarePushClaim, _ClaimLifecycle] = (
    weakref.WeakKeyDictionary()
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stamp(value: datetime | None = None) -> datetime:
    return _utc(value or now_utc())


def _cancel_before_dispatch(
    delivery: CarePushDelivery,
    *,
    code: CarePushDeliveryErrorCode,
    at: datetime,
) -> None:
    delivery.status = CarePushDeliveryStatus.CANCELLED.value
    delivery.completed_at = at
    delivery.error_code = code.value


def _capability_digest(
    *,
    kind: bytes,
    delivery_id: uuid.UUID,
    subject_id: uuid.UUID,
    subscription_id: uuid.UUID,
    recipient_user_id: uuid.UUID,
    lease_token: uuid.UUID,
    generation: SubscriptionGeneration,
    outcome: _TerminalOutcome | None = None,
) -> bytes:
    parts = [
        kind,
        delivery_id.bytes,
        subject_id.bytes,
        subscription_id.bytes,
        recipient_user_id.bytes,
        lease_token.bytes,
        generation.key_version.to_bytes(8, "big", signed=False),
        generation.ciphertext_fingerprint,
    ]
    if outcome is not None:
        parts.extend(
            [
                outcome.status.value.encode("ascii"),
                (outcome.error_code.value if outcome.error_code else "").encode(
                    "ascii"
                ),
            ]
        )
    return hmac.digest(_SEAL_KEY, b"\0".join(parts), "sha256")


def _claim_digest(claim: CarePushClaim) -> bytes:
    return _capability_digest(
        kind=b"claim",
        delivery_id=claim.delivery_id,
        subject_id=claim.subject_id,
        subscription_id=claim.subscription_id,
        recipient_user_id=claim.recipient_user_id,
        lease_token=claim.lease_token,
        generation=claim.generation,
    )


def _arm_claims_after_transaction(
    session: AsyncSession, claims: tuple[CarePushClaim, ...]
) -> None:
    """Bind claim activation to this session's exact outer transaction."""

    if not claims:
        return
    outer_transaction = session.sync_session.get_transaction()
    if outer_transaction is None:
        raise CarePushLeaseError("care push claim has no outer transaction")
    lifecycles = tuple(_CLAIM_LIFECYCLES[claim] for claim in claims)
    outer_commit_started = False
    completed = False

    def _before_commit(sync_session) -> None:
        nonlocal outer_commit_started
        current = (
            sync_session.get_nested_transaction()
            or sync_session.get_transaction()
        )
        if current is outer_transaction:
            outer_commit_started = True

    def _after_commit(_sync_session) -> None:
        nonlocal completed
        if not outer_commit_started or completed:
            return
        for lifecycle in lifecycles:
            if not lifecycle.invalid:
                lifecycle.committed = True
        completed = True

    def _after_transaction_end(_sync_session, transaction) -> None:
        nonlocal completed
        if transaction is not outer_transaction or completed:
            return
        for lifecycle in lifecycles:
            if not lifecycle.committed:
                lifecycle.invalid = True
        completed = True

    event.listen(session.sync_session, "before_commit", _before_commit)
    event.listen(session.sync_session, "after_commit", _after_commit)
    event.listen(
        session.sync_session,
        "after_transaction_end",
        _after_transaction_end,
    )


def _take_committed_target(claim: CarePushClaim) -> WebPushTarget:
    if not hmac.compare_digest(claim._seal, _claim_digest(claim)):
        raise CarePushLeaseError("care push claim was forged")
    lifecycle = _CLAIM_LIFECYCLES.get(claim)
    if lifecycle is None:
        raise CarePushLeaseError("care push claim was not issued by this process")
    if lifecycle.invalid:
        raise CarePushLeaseError("care push claim was rolled back")
    if not lifecycle.committed:
        raise CarePushLeaseError("care push claim is not committed")
    if lifecycle.consumed or lifecycle.target is None:
        raise CarePushLeaseError("care push claim was already consumed")
    lifecycle.consumed = True
    target = lifecycle.target
    lifecycle.target = None
    return target


def _validate_completion(completion: CarePushCompletion) -> _TerminalOutcome:
    expected = _capability_digest(
        kind=b"completion",
        delivery_id=completion.delivery_id,
        subject_id=completion.subject_id,
        subscription_id=completion.subscription_id,
        recipient_user_id=completion.recipient_user_id,
        lease_token=completion.lease_token,
        generation=completion.generation,
        outcome=completion.outcome,
    )
    if not hmac.compare_digest(completion._seal, expected):
        raise CarePushLeaseError("care push completion was forged")
    return completion.outcome


async def _participation_matches_context(
    session: AsyncSession, context, participation
) -> bool:
    """Reject support-only grants and a new relationship reviving an old room."""

    if context.subject_owner_user_id == context.principal.user_id:
        return participation.relationship_id is None
    grant = context.relationship_grant
    if grant is None or participation.relationship_id != grant.relationship_id:
        return False
    kind = await session.scalar(
        select(CareRelationship.kind)
        .join(
            ProfessionalProfile,
            (ProfessionalProfile.user_id == context.principal.user_id)
            & (ProfessionalProfile.kind == CareRelationship.kind)
            & (
                ProfessionalProfile.verification_status
                == ProfessionalVerificationStatus.VERIFIED.value
            ),
        )
        .where(
            CareRelationship.id == grant.relationship_id,
            CareRelationship.subject_id == context.subject_id,
            CareRelationship.professional_user_id == context.principal.user_id,
        )
    )
    required_role = {
        ProfessionalKind.DOCTOR.value: UserRoleName.DOCTOR,
        ProfessionalKind.TRAINER.value: UserRoleName.TRAINER,
    }.get(kind)
    return required_role is not None and required_role in context.principal.roles


async def claim_batch(
    session: AsyncSession,
    *,
    at: datetime | None = None,
    limit: int = BATCH_SIZE,
) -> tuple[CarePushClaim, ...]:
    """Claim a bounded page after fresh authorization checks. Never commits."""

    if session.in_nested_transaction():
        raise CarePushLeaseError(
            "care push claims require an outer transaction boundary"
        )
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= BATCH_SIZE
    ):
        raise ValueError(f"limit must be between 1 and {BATCH_SIZE}")
    current = _stamp(at)
    # Relationship, consent, account-status and role mutations take this same
    # transaction lock.  The claim therefore decides from one stable identity
    # snapshot before it commits the post-I/O-only state.
    await acquire_identity_governance_lock(session)
    candidates = (
        await session.scalars(
            select(CarePushDelivery)
            .where(CarePushDelivery.status == CarePushDeliveryStatus.PENDING.value)
            .order_by(CarePushDelivery.created_at, CarePushDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claims: list[CarePushClaim] = []
    for delivery in candidates:
        if _utc(delivery.created_at) < current - PENDING_STALE_AFTER:
            _cancel_before_dispatch(
                delivery,
                code=CarePushDeliveryErrorCode.STALE_PENDING,
                at=current,
            )
            continue

        user = await session.scalar(
            select(User)
            .where(User.id == delivery.recipient_user_id)
            .with_for_update()
        )
        if user is None or user.status != UserStatus.ACTIVE.value:
            _cancel_before_dispatch(
                delivery,
                code=CarePushDeliveryErrorCode.ACCOUNT_INACTIVE,
                at=current,
            )
            continue

        message = await session.scalar(
            select(CareMessage).where(
                CareMessage.id == delivery.message_id,
                CareMessage.subject_id == delivery.subject_id,
            )
        )
        if message is None or message.actor_user_id == delivery.recipient_user_id:
            _cancel_before_dispatch(
                delivery,
                code=CarePushDeliveryErrorCode.ACCESS_REVOKED,
                at=current,
            )
            continue
        participation = await session.scalar(
            select(CareThreadParticipant).where(
                CareThreadParticipant.thread_id == message.thread_id,
                CareThreadParticipant.subject_id == delivery.subject_id,
                CareThreadParticipant.user_id == delivery.recipient_user_id,
                CareThreadParticipant.removed_at.is_(None),
            ).with_for_update()
        )
        if participation is None:
            _cancel_before_dispatch(
                delivery,
                code=CarePushDeliveryErrorCode.ACCESS_REVOKED,
                at=current,
            )
            continue

        try:
            context = await resolve_access_context(
                session,
                user_id=delivery.recipient_user_id,
                subject_id=delivery.subject_id,
                evaluated_at=current,
            )
            require_access(
                context,
                resource_type=PolicyResourceType.OPERATION,
                resource_key=MESSAGE_OPERATION,
                action=PolicyAction.READ,
            )
        except AccessResolutionError:
            _cancel_before_dispatch(
                delivery,
                code=CarePushDeliveryErrorCode.ACCESS_REVOKED,
                at=current,
            )
            continue
        if not await _participation_matches_context(
            session, context, participation
        ):
            _cancel_before_dispatch(
                delivery,
                code=CarePushDeliveryErrorCode.ACCESS_REVOKED,
                at=current,
            )
            continue

        try:
            subscription = await web_push_subscriptions.load_for_dispatch(
                session,
                subscription_id=delivery.subscription_id,
                user_id=delivery.recipient_user_id,
            )
        except web_push_subscriptions.CorruptWebPushSubscription:
            await web_push_subscriptions.revoke_by_id(
                session,
                subscription_id=delivery.subscription_id,
                user_id=delivery.recipient_user_id,
                revoked_at=current,
            )
            _cancel_before_dispatch(
                delivery,
                code=CarePushDeliveryErrorCode.SUBSCRIPTION_REVOKED,
                at=current,
            )
            continue
        if subscription is None:
            _cancel_before_dispatch(
                delivery,
                code=CarePushDeliveryErrorCode.SUBSCRIPTION_REVOKED,
                at=current,
            )
            continue

        lease_token = uuid.uuid4()
        delivery.status = CarePushDeliveryStatus.DISPATCHING.value
        delivery.lease_token = lease_token
        delivery.dispatch_started_at = current
        seal = _capability_digest(
            kind=b"claim",
            delivery_id=delivery.id,
            subject_id=delivery.subject_id,
            subscription_id=delivery.subscription_id,
            recipient_user_id=delivery.recipient_user_id,
            lease_token=lease_token,
            generation=subscription.generation,
        )
        claim = CarePushClaim(
            delivery_id=delivery.id,
            subject_id=delivery.subject_id,
            subscription_id=delivery.subscription_id,
            recipient_user_id=delivery.recipient_user_id,
            lease_token=lease_token,
            generation=subscription.generation,
            _seal=seal,
        )
        _CLAIM_LIFECYCLES[claim] = _ClaimLifecycle(target=subscription.target)
        claims.append(claim)
    await session.flush()
    result = tuple(claims)
    _arm_claims_after_transaction(session, result)
    return result


async def reconcile_stale(
    session: AsyncSession,
    *,
    at: datetime | None = None,
    limit: int = RECONCILIATION_BATCH_SIZE,
) -> int:
    """Terminalize old pre-I/O and uncertain post-I/O state. Never retries."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    current = _stamp(at)
    rows = (
        await session.scalars(
            select(CarePushDelivery)
            .where(
                or_(
                    (
                        CarePushDelivery.status
                        == CarePushDeliveryStatus.PENDING.value
                    )
                    & (
                        CarePushDelivery.created_at
                        < current - PENDING_STALE_AFTER
                    ),
                    (
                        CarePushDelivery.status
                        == CarePushDeliveryStatus.DISPATCHING.value
                    )
                    & (
                        CarePushDelivery.dispatch_started_at
                        < current - DISPATCHING_STALE_AFTER
                    ),
                )
            )
            .order_by(CarePushDelivery.created_at, CarePushDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for delivery in rows:
        if delivery.status == CarePushDeliveryStatus.PENDING.value:
            _cancel_before_dispatch(
                delivery,
                code=CarePushDeliveryErrorCode.STALE_PENDING,
                at=current,
            )
        else:
            delivery.status = CarePushDeliveryStatus.AMBIGUOUS.value
            delivery.completed_at = max(
                current,
                _utc(delivery.dispatch_started_at),
            )
            delivery.error_code = CarePushDeliveryErrorCode.STALE_DISPATCH.value
    await session.flush()
    return len(rows)


def _provider_outcome(result: WebPushProviderResult) -> _TerminalOutcome:
    return {
        WebPushProviderOutcome.ACCEPTED: _SENT,
        WebPushProviderOutcome.GONE: _GONE,
        WebPushProviderOutcome.REJECTED: _REJECTED,
        WebPushProviderOutcome.AMBIGUOUS: _TRANSPORT_ERROR,
    }[result.outcome]


async def finalize(
    session: AsyncSession,
    *,
    completion: CarePushCompletion,
    at: datetime | None = None,
) -> bool:
    """Apply a result only to the exact live lease. Never commits."""

    outcome = _validate_completion(completion)
    current = _stamp(at)
    delivery = await session.scalar(
        select(CarePushDelivery)
        .where(
            CarePushDelivery.id == completion.delivery_id,
            CarePushDelivery.subject_id == completion.subject_id,
            CarePushDelivery.subscription_id == completion.subscription_id,
            CarePushDelivery.recipient_user_id == completion.recipient_user_id,
            CarePushDelivery.lease_token == completion.lease_token,
            CarePushDelivery.status == CarePushDeliveryStatus.DISPATCHING.value,
        )
        .with_for_update()
    )
    if delivery is None:
        return False
    delivery.status = outcome.status.value
    delivery.completed_at = max(current, _utc(delivery.dispatch_started_at))
    delivery.error_code = (
        outcome.error_code.value if outcome.error_code is not None else None
    )
    if outcome is _SENT:
        await web_push_subscriptions.record_success_if_dispatch_matches(
            session,
            subscription_id=completion.subscription_id,
            user_id=completion.recipient_user_id,
            generation=completion.generation,
            succeeded_at=current,
        )
    elif outcome is _GONE:
        await web_push_subscriptions.revoke_if_dispatch_matches(
            session,
            subscription_id=completion.subscription_id,
            user_id=completion.recipient_user_id,
            generation=completion.generation,
            revoked_at=current,
        )
    await session.flush()
    return True


async def _send_once(client: WebPushClient, target: WebPushTarget) -> _TerminalOutcome:
    try:
        result = await client.send_care_message_wakeup(target)
    except (InvalidWebPushTarget, WebPushProtocolError):
        return _INVALID_RESPONSE
    except WebPushTransportError:
        return _TRANSPORT_ERROR
    except Exception:
        return _INTERNAL_ERROR
    return _provider_outcome(result)


async def dispatch_claim(
    client: WebPushClient, claim: CarePushClaim
) -> CarePushCompletion:
    """Consume one committed claim and seal its provider result."""

    target = _take_committed_target(claim)
    try:
        outcome = await _send_once(client, target)
    finally:
        del target
    seal = _capability_digest(
        kind=b"completion",
        delivery_id=claim.delivery_id,
        subject_id=claim.subject_id,
        subscription_id=claim.subscription_id,
        recipient_user_id=claim.recipient_user_id,
        lease_token=claim.lease_token,
        generation=claim.generation,
        outcome=outcome,
    )
    return CarePushCompletion(
        delivery_id=claim.delivery_id,
        subject_id=claim.subject_id,
        subscription_id=claim.subscription_id,
        recipient_user_id=claim.recipient_user_id,
        lease_token=claim.lease_token,
        generation=claim.generation,
        outcome=outcome,
        _seal=seal,
    )


async def dispatch_job(session_factory, redis=None) -> None:
    """Shared-scheduler entry point; the scheduler owns its Redis lock."""

    del redis
    current = _stamp()
    async with session_factory() as session:
        # This bounded outbox spans every subject and has no user acting for it.
        # Exact predicates below keep every claim rooted even under platform
        # scope; the scheduler's reviewed allowlist is the authorization here.
        await enter_platform_scope(session)
        await reconcile_stale(session, at=current)
        await session.commit()

    config = web_push_config.load_config()
    if config is None:
        return
    client = WebPushClient(config)

    async with session_factory() as session:
        await enter_platform_scope(session)
        claims = await claim_batch(session, at=_stamp())
        await session.commit()

    pending_claims = list(claims)
    internal_failure = False
    while pending_claims:
        claim = pending_claims.pop(0)
        completion = await dispatch_claim(client, claim)
        del claim
        if completion.outcome is _INTERNAL_ERROR:
            internal_failure = True
        async with session_factory() as session:
            await enter_platform_scope(session)
            await finalize(
                session,
                completion=completion,
                at=_stamp(),
            )
            await session.commit()
    if internal_failure:
        raise CarePushDispatchError(
            "care push dispatch encountered an internal delivery error"
        )


__all__ = [
    "BATCH_SIZE",
    "CarePushClaim",
    "CarePushCompletion",
    "CarePushDispatchError",
    "CarePushLeaseError",
    "DISPATCHING_STALE_AFTER",
    "PENDING_STALE_AFTER",
    "claim_batch",
    "dispatch_claim",
    "dispatch_job",
    "finalize",
    "reconcile_stale",
]
