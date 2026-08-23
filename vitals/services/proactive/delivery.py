"""The gate every outgoing message passes through: may this be sent, and was it.

Five rules, in the order they're checked:

1. **No channel** → nothing happens (the app before the bot exists).
2. **Module off** → nothing happens either: switching ``signals`` off in Settings
   is the emergency switch, and it has to silence the bot without a deploy.
3. **Dedupe.** A ``dedupe_key`` that's already in the journal means this exact
   message went out; a re-run of the job is a no-op, not a second ping.
4. **Quiet hours** hold back *nudges* — the bot's own idea of a good moment. The
   brief and the evening block go out at a time the owner typed by hand into the
   same settings card, so silencing them by quiet hours is one field quietly
   cancelling another with no way to see which won.
5. **The daily budget** (also from the settings card) covers all three
   self-initiated categories — the brief, the evening block, nudges.

   Answers to the owner (``reply``, ``echo``) are deliberately exempt. Counting
   them would mean that after the fourth thing you logged, the bot stops replying
   to you — which reads as a broken bot, not as a budget. This is the single
   easiest rule in the whole feature to get wrong, so it lives in one ``frozenset``
   right here rather than at each call site.

A send that fails at the transport is logged and swallowed: the caller's DB work
(the signals it just parsed, the digest it just stored) must not be rolled back
because Telegram had a bad minute, and an un-sent message writes no journal row,
so it costs nothing from the budget either.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import (
    date as date_type,
    datetime,
    time as time_type,
    timedelta,
    timezone,
)
from time import monotonic
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    NotificationDeliveryErrorCode,
    NotificationDeliveryStatus,
    Source,
    UserStatus,
)
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject, User
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.scoped_settings import (
    IntegrationConnectionSetting,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity_service import (
    PreIdentityCompatibilityError,
    acquire_identity_governance_lock,
    authorize_pre_identity_compatibility_transaction,
)
from vitals.services.proactive import prefs
from vitals.services.proactive.channels import (
    LEGACY_TELEGRAM_CREDENTIAL_REF,
    BoundNotifier,
    BoundNotifierResolver,
    Buttons,
    DeliveryEndpointBinding,
    Notifier,
    canonicalize_buttons,
    canonicalize_text,
    resolve_legacy_bound_notifier,
)
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.services.rls_session import enter_platform_scope
from vitals.services.transaction_outcome import (
    TransactionOutcomeError,
    register_root_transaction_outcome,
)
from vitals.utils.timeutils import now_local, now_utc

logger = logging.getLogger(__name__)

#: The domain the Telegram bot stamped on every message it stored. A literal
#: rather than an enum member: the bot is gone and so is the domain, but a reply
#: intent still points at the raw that provoked it, and the check that the two
#: belong together has to keep working for rows already in the lake.
_INBOUND_RAW_DOMAIN = "signals"

# Categories. Only the first three are the bot talking first.
CATEGORY_BRIEF = "brief"
CATEGORY_EVENING = "evening"
CATEGORY_NUDGE = "nudge"
CATEGORY_REPLY = "reply"
CATEGORY_ECHO = "echo"
# A send the owner asked for from the web ("Отправить тестовое"): it exists to
# catch broken formatting, so it must go out even when today's brief already did,
# and it is not the bot talking first — hence off-budget and outside quiet hours.
CATEGORY_TEST = "test"

INITIATIVE_CATEGORIES = frozenset({CATEGORY_BRIEF, CATEGORY_EVENING, CATEGORY_NUDGE})
_DELIVERY_CATEGORIES = frozenset(
    {
        CATEGORY_BRIEF,
        CATEGORY_EVENING,
        CATEGORY_NUDGE,
        CATEGORY_REPLY,
        CATEGORY_ECHO,
        CATEGORY_TEST,
    }
)
HISTORICAL_RECIPIENT_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }
)

# Fallbacks only — the live values come from ``prefs`` (the settings card), which
# is why they are read per send rather than captured at import.
DAILY_BUDGET = prefs.DEFAULTS["daily_budget"]
QUIET_START = prefs.as_time(prefs.DEFAULTS["quiet_start"])
QUIET_END = prefs.as_time(prefs.DEFAULTS["quiet_end"])

PENDING_STALE_AFTER = timedelta(minutes=15)
DISPATCHING_STALE_AFTER = timedelta(hours=1)
RECONCILIATION_BATCH_SIZE = 100

_OPAQUE_KEY_RE = re.compile(r"[0-9a-f]{64}")
_INITIATIVE_CLAIM_STATUSES = frozenset(
    {
        NotificationDeliveryStatus.PENDING.value,
        NotificationDeliveryStatus.DISPATCHING.value,
        NotificationDeliveryStatus.SENT.value,
        NotificationDeliveryStatus.AMBIGUOUS.value,
    }
)
def _register_transaction_outcome(
    session: AsyncSession,
    *,
    on_commit,
    on_rollback,
) -> None:
    try:
        register_root_transaction_outcome(
            session,
            on_commit=on_commit,
            on_rollback=on_rollback,
        )
    except TransactionOutcomeError:
        raise DeliveryCapabilityError(
            "delivery capability requires one active outer transaction"
        ) from None


class DeliveryError(RuntimeError):
    """Base class for durable outbound-delivery failures."""


class DeliveryCapabilityError(DeliveryError):
    """An opaque delivery capability is forged, stale, or already consumed."""


class DeliveryScopeError(DeliveryError):
    """The requested subject/recipient/connection graph is not authorized."""


class DeliveryStateError(DeliveryError):
    """A durable delivery intent is not in the required lifecycle state."""


class DeliveryIdempotencyConflictError(DeliveryError):
    """An idempotency key is already bound to different immutable metadata."""


class DurableDeliveryRequiredError(DeliveryError):
    """An owned send attempted to bypass the durable three-phase service."""


class DeliveryPolicyUnavailableError(DeliveryError):
    """Subject-local delivery policy cannot be resolved safely."""


def _opaque_key(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _OPAQUE_KEY_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _hash_parts(namespace: str, stable_parts: tuple[object, ...]) -> str:
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("namespace must be a non-blank string")
    canonical: list[str] = ["vitals-delivery-v1", namespace.strip()]
    for part in stable_parts:
        if isinstance(part, (str, int, uuid.UUID, date_type, datetime)):
            value = part.isoformat() if isinstance(part, (date_type, datetime)) else str(part)
        else:
            raise TypeError("delivery key parts must be stable scalar identifiers")
        if not value:
            raise ValueError("delivery key parts must not be blank")
        canonical.append(value)
    payload = "\x1f".join(
        f"{len(item.encode('utf-8'))}:{item}" for item in canonical
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def make_delivery_idempotency_key(namespace: str, *stable_parts: object) -> str:
    """Return one opaque occurrence key; rendered content is never an input."""

    return _hash_parts(namespace, stable_parts)


def make_delivery_policy_key(namespace: str, *stable_parts: object) -> str:
    """Return an opaque grouping key, for example one static nudge-rule id."""

    return _hash_parts(f"policy:{namespace}", stable_parts)


def _binding_for(ownership: ProactiveOwnershipContext) -> DeliveryEndpointBinding:
    return DeliveryEndpointBinding(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.recipient_user_id,
        integration_connection_id=ownership.connection_id,
        channel=IntegrationProvider.TELEGRAM.value,
    )


def _same_binding(
    left: DeliveryEndpointBinding,
    right: DeliveryEndpointBinding,
) -> bool:
    return (
        isinstance(left, DeliveryEndpointBinding)
        and isinstance(right, DeliveryEndpointBinding)
        and left == right
    )


_PREPARED_SEAL = object()


class PreparedDeliveryIntent:
    """Memory-only payload for exactly one freshly inserted PENDING intent."""

    __slots__ = (
        "__weakref__",
        "_actor_user_id",
        "_ai_invocation_id",
        "_armed",
        "_binding",
        "_buttons",
        "_category",
        "_channel",
        "_consumed",
        "_finalizing",
        "_fingerprint",
        "_idempotency_key",
        "_intent_id",
        "_journal_raw_payload_id",
        "_policy_at",
        "_policy_date",
        "_policy_key",
        "_raw_payload_id",
        "_redact_journal_content",
        "_reply_to",
        "_seal",
        "_sent_at",
        "_session",
        "_text",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise DeliveryCapabilityError("prepared delivery intents are service-issued only")

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        intent: NotificationDeliveryIntent,
        binding: DeliveryEndpointBinding,
        text: str,
        buttons: tuple[tuple[str, str], ...] | None,
        reply_to: str | None,
        sent_at: datetime,
        redact_journal_content: bool,
        journal_raw_payload_id: int | None,
    ) -> "PreparedDeliveryIntent":
        prepared = object.__new__(cls)
        fingerprint = _intent_fingerprint(intent)
        for name, value in (
            ("_intent_id", intent.id),
            ("_actor_user_id", intent.actor_user_id),
            ("_ai_invocation_id", intent.ai_invocation_id),
            ("_binding", binding),
            ("_category", intent.category),
            ("_channel", intent.channel),
            ("_idempotency_key", intent.idempotency_key),
            ("_policy_at", _aware_utc(intent.policy_at)),
            ("_policy_date", intent.policy_date),
            ("_policy_key", intent.policy_key),
            ("_raw_payload_id", intent.raw_payload_id),
            ("_text", text),
            ("_buttons", buttons),
            ("_reply_to", reply_to),
            ("_sent_at", sent_at),
            ("_redact_journal_content", redact_journal_content),
            ("_journal_raw_payload_id", journal_raw_payload_id),
            ("_fingerprint", fingerprint),
            ("_session", session),
            ("_armed", False),
            ("_consumed", False),
            ("_finalizing", False),
            ("_seal", _PREPARED_SEAL),
        ):
            object.__setattr__(prepared, name, value)

        prepared_ref = weakref.ref(prepared)

        def after_commit() -> None:
            target = prepared_ref()
            if target is None:
                return
            object.__setattr__(target, "_armed", True)

        def after_rollback() -> None:
            target = prepared_ref()
            if target is None:
                return
            object.__setattr__(target, "_consumed", True)
            target._clear_payload()
            object.__setattr__(target, "_session", None)

        _register_transaction_outcome(
            session,
            on_commit=after_commit,
            on_rollback=after_rollback,
        )
        return prepared

    def _clear_payload(self) -> None:
        object.__setattr__(self, "_text", None)
        object.__setattr__(self, "_buttons", None)
        object.__setattr__(self, "_reply_to", None)

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedDeliveryIntent is immutable")

    def __repr__(self) -> str:
        return f"<PreparedDeliveryIntent intent_id={self._intent_id} redacted>"

    def __reduce__(self):
        raise TypeError("PreparedDeliveryIntent is not pickleable")

    def __copy__(self):
        raise TypeError("PreparedDeliveryIntent is not copyable")

    __deepcopy__ = __copy__

    @property
    def intent_id(self) -> uuid.UUID:
        return self._intent_id


_LEASE_SEAL = object()


class DeliveryDispatchLease:
    """One-shot proof that DISPATCHING committed with a fresh bound transport."""

    __slots__ = (
        "__weakref__",
        "_armed",
        "_binding",
        "_buttons",
        "_consumed",
        "_fingerprint",
        "_issued_monotonic",
        "_journal_raw_payload_id",
        "_lease_token",
        "_notifier",
        "_redact_journal_content",
        "_reply_to",
        "_seal",
        "_sent_at",
        "_session",
        "_text",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise DeliveryCapabilityError("delivery dispatch leases are service-issued only")

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        prepared: PreparedDeliveryIntent,
        lease_token: uuid.UUID,
        notifier: BoundNotifier,
        sent_at: datetime,
    ) -> "DeliveryDispatchLease":
        lease = object.__new__(cls)
        for name, value in (
            ("_binding", prepared._binding),
            ("_text", prepared._text),
            ("_buttons", prepared._buttons),
            ("_reply_to", prepared._reply_to),
            ("_sent_at", sent_at),
            ("_redact_journal_content", prepared._redact_journal_content),
            ("_journal_raw_payload_id", prepared._journal_raw_payload_id),
            ("_fingerprint", prepared._fingerprint),
            ("_issued_monotonic", monotonic()),
            ("_lease_token", lease_token),
            ("_notifier", notifier),
            ("_session", session),
            ("_armed", False),
            ("_consumed", False),
            ("_seal", _LEASE_SEAL),
        ):
            object.__setattr__(lease, name, value)
        return lease

    def _invalidate(self) -> None:
        object.__setattr__(self, "_consumed", True)
        object.__setattr__(self, "_notifier", None)
        object.__setattr__(self, "_text", None)
        object.__setattr__(self, "_buttons", None)
        object.__setattr__(self, "_reply_to", None)
        object.__setattr__(self, "_session", None)

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("DeliveryDispatchLease is immutable")

    def __repr__(self) -> str:
        return f"<DeliveryDispatchLease intent_id={self._fingerprint[0]} redacted>"

    def __reduce__(self):
        raise TypeError("DeliveryDispatchLease is not pickleable")

    def __copy__(self):
        raise TypeError("DeliveryDispatchLease is not copyable")

    __deepcopy__ = __copy__

    @property
    def intent_id(self) -> uuid.UUID:
        return self._fingerprint[0]


_COMPLETION_SEAL = object()


class DeliveryCompletion:
    """Sanitized provider outcome with a memory-only journal payload."""

    __slots__ = (
        "__weakref__",
        "_buttons",
        "_consumed",
        "_error_code",
        "_external_id",
        "_finalizing",
        "_fingerprint",
        "_journal_raw_payload_id",
        "_lease_token",
        "_redact_journal_content",
        "_seal",
        "_sent_at",
        "_session",
        "_status",
        "_text",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise DeliveryCapabilityError("delivery completions are service-issued only")

    @classmethod
    def _issue(
        cls,
        *,
        lease: DeliveryDispatchLease,
        status: NotificationDeliveryStatus,
        error_code: NotificationDeliveryErrorCode | None,
        external_id: str | None,
        text: str | None,
        buttons: tuple[tuple[str, str], ...] | None,
    ) -> "DeliveryCompletion":
        completion = object.__new__(cls)
        for name, value in (
            ("_status", status),
            ("_error_code", error_code),
            ("_external_id", external_id),
            ("_text", text),
            ("_buttons", buttons),
            ("_sent_at", lease._sent_at),
            ("_redact_journal_content", lease._redact_journal_content),
            ("_journal_raw_payload_id", lease._journal_raw_payload_id),
            ("_fingerprint", lease._fingerprint),
            ("_lease_token", lease._lease_token),
            ("_consumed", False),
            ("_finalizing", False),
            ("_session", None),
            ("_seal", _COMPLETION_SEAL),
        ):
            object.__setattr__(completion, name, value)
        return completion

    def _bind_finalization(self, session: AsyncSession) -> None:
        completion_ref = weakref.ref(self)
        object.__setattr__(self, "_finalizing", True)
        object.__setattr__(self, "_session", session)

        def after_commit() -> None:
            target = completion_ref()
            if target is None:
                return
            object.__setattr__(target, "_consumed", True)
            object.__setattr__(target, "_finalizing", False)
            object.__setattr__(target, "_text", None)
            object.__setattr__(target, "_buttons", None)
            object.__setattr__(target, "_session", None)

        def after_rollback() -> None:
            target = completion_ref()
            if target is None:
                return
            object.__setattr__(target, "_finalizing", False)
            object.__setattr__(target, "_session", None)

        _register_transaction_outcome(
            session,
            on_commit=after_commit,
            on_rollback=after_rollback,
        )

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("DeliveryCompletion is immutable")

    def __repr__(self) -> str:
        return (
            f"<DeliveryCompletion intent_id={self._fingerprint[0]} "
            f"status={self._status.value} redacted>"
        )

    def __reduce__(self):
        raise TypeError("DeliveryCompletion is not pickleable")

    def __copy__(self):
        raise TypeError("DeliveryCompletion is not copyable")

    __deepcopy__ = __copy__

    @property
    def intent_id(self) -> uuid.UUID:
        return self._fingerprint[0]

    @property
    def status(self) -> NotificationDeliveryStatus:
        return self._status

    @property
    def error_code(self) -> NotificationDeliveryErrorCode | None:
        return self._error_code


@dataclass(frozen=True, slots=True)
class _DeliveryPolicy:
    """Typed subject policy consumed by both T1 and the fresh T2 recheck."""

    daily_budget: int
    quiet_start: time_type
    quiet_end: time_type

    def __post_init__(self) -> None:
        if (
            isinstance(self.daily_budget, bool)
            or not isinstance(self.daily_budget, int)
            or self.daily_budget < 1
        ):
            raise DeliveryPolicyUnavailableError(
                "delivery daily budget must be a positive integer"
            )
        if not isinstance(self.quiet_start, time_type) or not isinstance(
            self.quiet_end, time_type
        ):
            raise DeliveryPolicyUnavailableError(
                "delivery quiet hours must be time values"
            )


@dataclass(frozen=True, slots=True)
class _LockedDeliveryAuthority:
    binding: DeliveryEndpointBinding
    timezone: ZoneInfo
    credential_ref: str
    telegram_connection_ids: frozenset[uuid.UUID]


_PREPARATION_SCOPE_SEAL = object()


class DeliveryPreparationScope:
    """One-transaction proof that every live delivery root is already locked.

    Domain services may use the interval between this prelock and
    :func:`prepare_delivery_intent` to persist deterministic raw-backed state.
    The scope contains no rendered payload and cannot survive its exact outer
    transaction.
    """

    __slots__ = (
        "__weakref__",
        "_actor_user_id",
        "_authority",
        "_category",
        "_consumed",
        "_fingerprint",
        "_include_legacy_unowned",
        "_local_at",
        "_module_enabled",
        "_ownership_connection_id",
        "_ownership_recipient_user_id",
        "_ownership_subject_id",
        "_policy",
        "_policy_at",
        "_seal",
        "_session",
        "_transaction",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise DeliveryCapabilityError(
            "delivery preparation scopes are service-issued only"
        )

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        authority: _LockedDeliveryAuthority,
        module_enabled: bool,
        policy: _DeliveryPolicy,
        policy_at: datetime,
        local_at: datetime,
        category: str,
        ownership: ProactiveOwnershipContext,
        actor_user_id: uuid.UUID | None,
    ) -> "DeliveryPreparationScope":
        transaction = session.sync_session.get_transaction()
        if transaction is None or session.in_nested_transaction():
            raise DeliveryCapabilityError(
                "delivery preparation scope requires an outer transaction"
            )
        scope = object.__new__(cls)
        fingerprint = (
            ownership.subject_id,
            ownership.recipient_user_id,
            ownership.connection_id,
            ownership.include_legacy_unowned,
            actor_user_id,
            category,
            authority.binding,
            authority.timezone.key,
            authority.credential_ref,
            authority.telegram_connection_ids,
            module_enabled,
            policy,
            policy_at,
            local_at,
        )
        for name, value in (
            ("_ownership_subject_id", ownership.subject_id),
            ("_ownership_recipient_user_id", ownership.recipient_user_id),
            ("_ownership_connection_id", ownership.connection_id),
            ("_include_legacy_unowned", ownership.include_legacy_unowned),
            ("_actor_user_id", actor_user_id),
            ("_category", category),
            ("_authority", authority),
            ("_module_enabled", module_enabled),
            ("_policy", policy),
            ("_policy_at", policy_at),
            ("_local_at", local_at),
            ("_fingerprint", fingerprint),
            ("_session", session),
            ("_transaction", transaction),
            ("_consumed", False),
            ("_seal", _PREPARATION_SCOPE_SEAL),
        ):
            object.__setattr__(scope, name, value)

        scope_ref = weakref.ref(scope)

        def invalidate() -> None:
            target = scope_ref()
            if target is not None:
                target._invalidate()

        _register_transaction_outcome(
            session,
            on_commit=invalidate,
            on_rollback=invalidate,
        )
        return scope

    def _invalidate(self) -> None:
        object.__setattr__(self, "_consumed", True)
        object.__setattr__(self, "_session", None)
        object.__setattr__(self, "_transaction", None)

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("DeliveryPreparationScope is immutable")

    def __repr__(self) -> str:
        return "<DeliveryPreparationScope redacted>"

    def __reduce__(self):
        raise TypeError("DeliveryPreparationScope is not pickleable")

    def __copy__(self):
        raise TypeError("DeliveryPreparationScope is not copyable")

    __deepcopy__ = __copy__


@dataclass(frozen=True, slots=True)
class _IntentSnapshot:
    id: uuid.UUID
    subject_id: uuid.UUID
    recipient_user_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    integration_connection_id: uuid.UUID
    raw_payload_id: int | None
    ai_invocation_id: uuid.UUID | None
    category: str
    channel: str
    idempotency_key: str
    policy_key: str | None
    policy_at: datetime
    policy_date: date_type


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise DeliveryStateError("delivery timestamp is invalid")
    if value.tzinfo is None:
        # SQLite drops timezone information from DateTime(timezone=True).
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _policy_clock(
    *,
    timezone_value: ZoneInfo,
    now: datetime | None,
) -> tuple[datetime, datetime]:
    """Return one UTC instant and its aware subject-local representation."""

    if now is None:
        instant = now_utc().astimezone(timezone.utc)
    elif not isinstance(now, datetime):
        raise TypeError("now must be a datetime or None")
    elif now.tzinfo is None:
        local_candidate = now.replace(tzinfo=timezone_value)
        instant = local_candidate.astimezone(timezone.utc)
        roundtrip = instant.astimezone(timezone_value).replace(tzinfo=None)
        if roundtrip != now:
            raise DeliveryPolicyUnavailableError(
                "naive delivery time does not exist in the subject timezone"
            )
    else:
        instant = now.astimezone(timezone.utc)
    return instant, instant.astimezone(timezone_value)


def _intent_fingerprint(intent: NotificationDeliveryIntent) -> tuple:
    return (
        intent.id,
        intent.subject_id,
        intent.recipient_user_id,
        intent.actor_user_id,
        intent.integration_connection_id,
        intent.raw_payload_id,
        intent.ai_invocation_id,
        intent.category,
        intent.channel,
        intent.idempotency_key,
        intent.policy_key,
        _aware_utc(intent.policy_at),
        intent.policy_date,
    )


def _snapshot_from_fingerprint(fingerprint: tuple) -> _IntentSnapshot:
    if not isinstance(fingerprint, tuple) or len(fingerprint) != 13:
        raise DeliveryCapabilityError("delivery capability fingerprint is invalid")
    return _IntentSnapshot(*fingerprint)


def _snapshot_fingerprint(snapshot: _IntentSnapshot) -> tuple:
    return (
        snapshot.id,
        snapshot.subject_id,
        snapshot.recipient_user_id,
        snapshot.actor_user_id,
        snapshot.integration_connection_id,
        snapshot.raw_payload_id,
        snapshot.ai_invocation_id,
        snapshot.category,
        snapshot.channel,
        snapshot.idempotency_key,
        snapshot.policy_key,
        snapshot.policy_at,
        snapshot.policy_date,
    )


def _prepared_is_valid(prepared: object) -> bool:
    return bool(
        isinstance(prepared, PreparedDeliveryIntent)
        and prepared._seal is _PREPARED_SEAL
        and prepared._armed
        and not prepared._consumed
        and not prepared._finalizing
        and prepared._session is not None
        and prepared._text is not None
    )


def _prepared_fingerprint_matches(prepared: PreparedDeliveryIntent) -> bool:
    return prepared._fingerprint == (
        prepared._intent_id,
        prepared._binding.subject_id,
        prepared._binding.recipient_user_id,
        prepared._actor_user_id,
        prepared._binding.integration_connection_id,
        prepared._raw_payload_id,
        prepared._ai_invocation_id,
        prepared._category,
        prepared._channel,
        prepared._idempotency_key,
        prepared._policy_key,
        prepared._policy_at,
        prepared._policy_date,
    )


async def _lock_live_delivery_authority(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None,
) -> _LockedDeliveryAuthority:
    """Lock governance -> sole S -> sorted users -> exact current Telegram C."""

    _validate_ownership(ownership, actor_user_id=actor_user_id)
    await acquire_identity_governance_lock(session)
    subject_rows = list(
        await session.scalars(
            select(HealthSubject)
            .order_by(HealthSubject.id)
            .limit(2)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(subject_rows) != 1:
        raise DeliveryPolicyUnavailableError(
            "legacy Telegram delivery requires exactly one health subject"
        )
    subject = subject_rows[0]
    if (
        subject.id != ownership.subject_id
        or subject.owner_user_id != ownership.recipient_user_id
    ):
        raise DeliveryScopeError("delivery subject and recipient are not authorized")
    try:
        subject_timezone = ZoneInfo(subject.timezone)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise DeliveryPolicyUnavailableError(
            "health subject timezone is invalid"
        ) from exc

    user_ids = sorted(
        {
            ownership.recipient_user_id,
            *({actor_user_id} if actor_user_id is not None else set()),
        },
        key=str,
    )
    users = list(
        await session.scalars(
            select(User)
            .where(User.id.in_(user_ids))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(users) != len(user_ids) or any(
        row.status != UserStatus.ACTIVE.value for row in users
    ):
        raise DeliveryScopeError("delivery recipient or actor is not active")

    connections = list(
        await session.scalars(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.subject_id == ownership.subject_id,
                IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
                IntegrationConnection.connection_type
                == IntegrationConnectionType.RECIPIENT.value,
            )
            .order_by(IntegrationConnection.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    known_statuses = {item.value for item in IntegrationConnectionStatus}
    if any(row.status not in known_statuses for row in connections):
        raise DeliveryScopeError("Telegram recipient has unknown lifecycle state")
    current = [
        row
        for row in connections
        if row.status != IntegrationConnectionStatus.RETIRED.value
    ]
    if len(current) != 1 or current[0].id != ownership.connection_id:
        raise DeliveryScopeError("Telegram recipient connection is not exact/current")
    connection = current[0]
    if connection.status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }:
        raise DeliveryScopeError("Telegram recipient connection is inactive")
    if connection.credential_ref != LEGACY_TELEGRAM_CREDENTIAL_REF:
        raise DeliveryScopeError("Telegram credential resolver is unavailable")
    return _LockedDeliveryAuthority(
        binding=_binding_for(ownership),
        timezone=subject_timezone,
        credential_ref=connection.credential_ref,
        telegram_connection_ids=frozenset(row.id for row in connections),
    )


async def _lock_historical_delivery_roots(
    session: AsyncSession,
    *,
    intent: NotificationDeliveryIntent | _IntentSnapshot,
) -> None:
    """Lock frozen provenance without applying post-send live authorization."""

    # Freeze identity/tenancy graph writers before projecting the raw row's
    # historical connection.  The projection stays intentionally unlocked so
    # the canonical row-lock order remains governance -> S -> users -> Cs -> raw.
    await acquire_identity_governance_lock(session)
    raw_connection_id: uuid.UUID | None = None
    if intent.raw_payload_id is not None:
        with session.no_autoflush:
            raw_connection_id = await session.scalar(
                select(RawPayload.integration_connection_id).where(
                    RawPayload.id == intent.raw_payload_id
                )
            )
        if raw_connection_id is None:
            raise DeliveryScopeError("delivery raw provenance no longer exists")
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == intent.subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subject is None:
        raise DeliveryScopeError("delivery subject no longer exists")
    user_ids = sorted(
        {
            intent.recipient_user_id,
            *(
                {intent.actor_user_id}
                if intent.actor_user_id is not None
                else set()
            ),
        },
        key=str,
    )
    users = list(
        await session.scalars(
            select(User)
            .where(User.id.in_(user_ids))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(users) != len(user_ids):
        raise DeliveryScopeError("delivery recipient or actor no longer exists")
    connection_ids = sorted(
        {
            intent.integration_connection_id,
            *({raw_connection_id} if raw_connection_id is not None else set()),
        },
        key=str,
    )
    connections = list(
        await session.scalars(
            select(IntegrationConnection)
            .where(IntegrationConnection.id.in_(connection_ids))
            .order_by(IntegrationConnection.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(connections) != len(connection_ids) or any(
        row.subject_id != intent.subject_id
        or row.provider != intent.channel
        or row.connection_type != IntegrationConnectionType.RECIPIENT.value
        or row.status not in HISTORICAL_RECIPIENT_STATUSES
        for row in connections
    ):
        raise DeliveryScopeError("historical delivery connection is invalid")
    await _lock_raw_and_ai_provenance(
        session,
        intent=intent,
        telegram_connection_ids=frozenset(connection_ids),
    )


async def _lock_raw_and_ai_provenance(
    session: AsyncSession,
    *,
    intent: NotificationDeliveryIntent | _IntentSnapshot,
    telegram_connection_ids: frozenset[uuid.UUID],
) -> None:
    if intent.raw_payload_id is not None:
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == intent.raw_payload_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            raw is None
            or raw.subject_id != intent.subject_id
            or raw.actor_user_id != intent.recipient_user_id
            or raw.integration_connection_id not in telegram_connection_ids
            or raw.file_asset_id is not None
            or raw.domain != _INBOUND_RAW_DOMAIN
            or raw.source != Source.TELEGRAM.value
        ):
            raise DeliveryScopeError("delivery raw provenance is invalid")
    if intent.ai_invocation_id is None:
        return
    invocation = await session.scalar(
        select(AIInvocation)
        .where(AIInvocation.id == intent.ai_invocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    expected_purpose = (
        AIInvocationPurpose.SIGNAL_PARSE.value
        if intent.category == CATEGORY_ECHO
        else AIInvocationPurpose.QUESTION_REPLY.value
    )
    allowed_statuses = {
        AIInvocationStatus.SUCCEEDED.value,
        AIInvocationStatus.FAILED.value,
        AIInvocationStatus.AMBIGUOUS.value,
    }
    if intent.category == CATEGORY_REPLY:
        allowed_statuses.add(AIInvocationStatus.CANCELLED.value)
    if (
        invocation is None
        or invocation.subject_id != intent.subject_id
        or invocation.actor_user_id != intent.recipient_user_id
        or invocation.raw_payload_id != intent.raw_payload_id
        or invocation.purpose != expected_purpose
        or invocation.source != AIInvocationSource.TELEGRAM.value
        or invocation.status not in allowed_statuses
    ):
        raise DeliveryScopeError("delivery AI provenance is invalid")


async def _locked_signals_module_enabled(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> bool:
    """Read only the already-scoped module row after the subject lock."""

    del session, subject_id
    # The proactive layer had a master switch: the ``signals`` module, which was
    # also the free-text capture domain. Both are gone, and nothing replaced the
    # switch — the layer's own preferences say what it sends and how often, and a
    # second switch above them was only ever an emergency stop for a bot that no
    # longer exists.
    return True


async def _load_locked_delivery_policy(
    session: AsyncSession,
    *,
    authority: _LockedDeliveryAuthority,
) -> _DeliveryPolicy:
    """Load the reviewed scoped policy projection without lock-order inversion."""

    # The connection root is already locked. Lock the exact scoped policy row as
    # well so a composed domain mutation and final intent reservation observe one
    # frozen policy even when the caller keeps this outer transaction open.
    await session.scalar(
        select(IntegrationConnectionSetting.integration_connection_id)
        .where(
            IntegrationConnectionSetting.integration_connection_id
            == authority.binding.integration_connection_id,
            IntegrationConnectionSetting.key
            == prefs.TELEGRAM_DELIVERY_POLICY_KEY,
        )
        .with_for_update()
    )
    return await _read_delivery_policy(session, authority=authority)


async def _read_delivery_policy(
    session: AsyncSession,
    *,
    authority: _LockedDeliveryAuthority,
) -> _DeliveryPolicy:
    """Reproject the exact policy whose row/root is already locked."""

    getter = getattr(prefs, "get_locked_delivery_policy", None)
    if getter is None:
        raise DeliveryPolicyUnavailableError(
            "subject-scoped delivery policy service is unavailable"
        )
    raw = await getter(
        session,
        subject_id=authority.binding.subject_id,
        recipient_user_id=authority.binding.recipient_user_id,
        integration_connection_id=authority.binding.integration_connection_id,
    )
    try:
        quiet_start = raw.quiet_start
        quiet_end = raw.quiet_end
        daily_budget = raw.daily_budget
    except AttributeError:
        if not isinstance(raw, dict):
            raise DeliveryPolicyUnavailableError(
                "delivery policy projection is invalid"
            )
        try:
            quiet_start = raw["quiet_start"]
            quiet_end = raw["quiet_end"]
            daily_budget = raw["daily_budget"]
        except KeyError as exc:
            raise DeliveryPolicyUnavailableError(
                "delivery policy projection is invalid"
            ) from exc
    try:
        return _DeliveryPolicy(
            daily_budget=daily_budget,
            quiet_start=(
                quiet_start
                if isinstance(quiet_start, time_type)
                else prefs.as_time(quiet_start)
            ),
            quiet_end=(
                quiet_end
                if isinstance(quiet_end, time_type)
                else prefs.as_time(quiet_end)
            ),
        )
    except (TypeError, ValueError) as exc:
        raise DeliveryPolicyUnavailableError(
            "delivery policy projection is invalid"
        ) from exc


def _validate_ownership(
    ownership: ProactiveOwnershipContext | None,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    if ownership is not None and not isinstance(
        ownership, ProactiveOwnershipContext
    ):
        raise TypeError("ownership must be a ProactiveOwnershipContext or None")
    if actor_user_id is not None and not isinstance(actor_user_id, uuid.UUID):
        raise TypeError("actor_user_id must be a UUID or None")
    if ownership is None and actor_user_id is not None:
        raise ValueError("actor_user_id requires explicit proactive ownership")
    if (
        ownership is not None
        and actor_user_id is not None
        and actor_user_id != ownership.recipient_user_id
    ):
        raise ValueError("proactive delivery actor must be the recipient user")


def notification_ownership_scope(
    ownership: ProactiveOwnershipContext,
    *,
    connection_scoped: bool = True,
):
    if not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext")
    connection_filters = [
        IntegrationConnection.id == Notification.integration_connection_id,
        IntegrationConnection.subject_id == ownership.subject_id,
        IntegrationConnection.connection_type
        == IntegrationConnectionType.RECIPIENT.value,
        IntegrationConnection.status.in_(HISTORICAL_RECIPIENT_STATUSES),
        IntegrationConnection.provider == Notification.channel,
    ]
    if connection_scoped:
        connection_filters.append(
            IntegrationConnection.id == ownership.connection_id
        )
    valid_connection = (
        select(IntegrationConnection.id)
        .where(*connection_filters)
        .correlate(Notification)
        .exists()
    )
    owned = and_(
        Notification.subject_id == ownership.subject_id,
        Notification.recipient_user_id == ownership.recipient_user_id,
        or_(
            Notification.actor_user_id.is_(None),
            Notification.actor_user_id == ownership.recipient_user_id,
        ),
        valid_connection,
    )
    if ownership.include_legacy_unowned:
        owned = or_(
            owned,
            and_(
                Notification.subject_id.is_(None),
                Notification.actor_user_id.is_(None),
                Notification.recipient_user_id.is_(None),
                Notification.integration_connection_id.is_(None),
            ),
        )
    return owned


class NotificationOwnershipConflictError(RuntimeError):
    """A global legacy dedupe key is already owned by another scope."""


class ProactiveOwnershipScopeError(ValueError):
    """A delivery context does not resolve to the legacy owner/channel graph."""


@dataclass(frozen=True, slots=True)
class _PreparedDelivery:
    """Policy-approved message that may cross the network without a DB session."""

    text: str = field(repr=False)
    category: str
    dedupe_key: str | None
    buttons: tuple[tuple[str, str], ...] | None
    reply_to: str | None
    sent_at: datetime
    channel: str
    ownership: ProactiveOwnershipContext | None
    actor_user_id: uuid.UUID | None
    ai_invocation_id: uuid.UUID | None
    redact_journal_content: bool
    journal_raw_payload_id: int | None
    _session: AsyncSession = field(repr=False, compare=False)
    _transaction: object = field(repr=False, compare=False)

    def __reduce__(self):
        raise TypeError("_PreparedDelivery is not pickleable")


@dataclass(frozen=True, slots=True)
class _DeliveredMessage:
    """Successful transport result, including channels without a message id."""

    external_id: str | None


async def _require_ownership_scope(
    session: AsyncSession,
    ownership: ProactiveOwnershipContext,
    *,
    channel: str,
) -> None:
    """Revalidate the legacy recipient and channel before network delivery.

    Owner-as-recipient is a Stage-2 compatibility invariant.  A future care-team
    delivery model must replace it with an explicit recipient/access binding.
    Historical reads may retain inactive provenance, but a live delivery is
    allowed only through a legacy-compatible or active recipient connection.
    """

    subject = (
        await session.execute(
            select(HealthSubject.owner_user_id, User.status)
            .join(User, User.id == HealthSubject.owner_user_id)
            .where(HealthSubject.id == ownership.subject_id)
        )
    ).one_or_none()
    if subject is None:
        raise ProactiveOwnershipScopeError(
            "proactive delivery subject or owner does not exist"
        )
    owner_user_id, owner_status = subject
    if owner_user_id != ownership.recipient_user_id:
        raise ProactiveOwnershipScopeError(
            "proactive recipient is not the legacy subject owner"
        )
    if owner_status != UserStatus.ACTIVE.value:
        raise ProactiveOwnershipScopeError(
            "proactive recipient identity is not active"
        )

    if not isinstance(channel, str) or not channel.strip():
        raise ProactiveOwnershipScopeError("proactive delivery channel is invalid")

    connection = (
        await session.execute(
            select(
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
            ).where(
                IntegrationConnection.id == ownership.connection_id,
                IntegrationConnection.subject_id == ownership.subject_id,
            )
        )
    ).one_or_none()
    if connection is None:
        raise ProactiveOwnershipScopeError(
            "proactive delivery connection does not match the subject"
        )
    provider, connection_type, status = connection
    if provider != channel:
        raise ProactiveOwnershipScopeError(
            "proactive notifier channel does not match its connection provider"
        )
    if connection_type != IntegrationConnectionType.RECIPIENT.value:
        raise ProactiveOwnershipScopeError(
            "proactive delivery connection is not a recipient binding"
        )
    known_statuses = {item.value for item in IntegrationConnectionStatus}
    if status not in known_statuses:
        raise ProactiveOwnershipScopeError(
            "proactive delivery connection has unknown lifecycle state"
        )
    if status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }:
        raise ProactiveOwnershipScopeError(
            "inactive proactive delivery connection cannot send"
        )


async def _require_ai_invocation_scope(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext | None,
    category: str,
    ai_invocation_id: uuid.UUID | None,
) -> int | None:
    if ai_invocation_id is None:
        return None
    if not isinstance(ai_invocation_id, uuid.UUID):
        raise TypeError("ai_invocation_id must be a UUID or None")
    if ownership is None or category not in {CATEGORY_ECHO, CATEGORY_REPLY}:
        raise ProactiveOwnershipScopeError(
            "AI delivery provenance requires an owned reply or echo"
        )
    expected_purpose = (
        AIInvocationPurpose.SIGNAL_PARSE
        if category == CATEGORY_ECHO
        else AIInvocationPurpose.QUESTION_REPLY
    )
    allowed_statuses = {
        AIInvocationStatus.SUCCEEDED.value,
        AIInvocationStatus.FAILED.value,
        AIInvocationStatus.AMBIGUOUS.value,
    }
    if expected_purpose is AIInvocationPurpose.QUESTION_REPLY:
        # A reply reservation cancelled before provider I/O still owns the one
        # deterministic fallback journal for its raw question.
        allowed_statuses.add(AIInvocationStatus.CANCELLED.value)
    row = (
        await session.execute(
            select(
                AIInvocation.subject_id,
                AIInvocation.actor_user_id,
                AIInvocation.purpose,
                AIInvocation.source,
                AIInvocation.status,
                AIInvocation.raw_payload_id,
            ).where(AIInvocation.id == ai_invocation_id)
        )
    ).one_or_none()
    if row is None:
        raise ProactiveOwnershipScopeError("AI delivery invocation does not exist")
    subject_id, actor_user_id, purpose, source, status, raw_payload_id = row
    if (
        subject_id != ownership.subject_id
        or actor_user_id != ownership.recipient_user_id
        or purpose != expected_purpose.value
        or source != AIInvocationSource.TELEGRAM.value
        or status not in allowed_statuses
        or raw_payload_id is None
    ):
        raise ProactiveOwnershipScopeError("AI delivery invocation provenance is invalid")
    return raw_payload_id


async def _require_redacted_reply_raw_scope(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    invocation_raw_payload_id: int | None,
) -> None:
    """Prove the JSON journal marker points at the exact owned Telegram raw."""

    row = (
        await session.execute(
            select(
                RawPayload.subject_id,
                RawPayload.actor_user_id,
                RawPayload.file_asset_id,
                RawPayload.domain,
                RawPayload.source,
                RawPayload.processed_at,
                IntegrationConnection.subject_id,
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
            )
            .join(
                IntegrationConnection,
                IntegrationConnection.id == RawPayload.integration_connection_id,
            )
            .where(RawPayload.id == raw_payload_id)
        )
    ).one_or_none()
    if row is None:
        raise ProactiveOwnershipScopeError(
            "redacted reply raw provenance does not exist"
        )
    (
        raw_subject_id,
        raw_actor_user_id,
        raw_file_asset_id,
        raw_domain,
        raw_source,
        raw_processed_at,
        connection_subject_id,
        connection_provider,
        connection_type,
        connection_status,
    ) = row
    if (
        raw_subject_id != ownership.subject_id
        or raw_actor_user_id != ownership.recipient_user_id
        or raw_file_asset_id is not None
        or raw_domain != _INBOUND_RAW_DOMAIN
        or raw_source != Source.TELEGRAM.value
        or raw_processed_at is None
        or connection_subject_id != ownership.subject_id
        or connection_provider != IntegrationProvider.TELEGRAM.value
        or connection_type != IntegrationConnectionType.RECIPIENT.value
        or connection_status not in HISTORICAL_RECIPIENT_STATUSES
        or (
            invocation_raw_payload_id is not None
            and invocation_raw_payload_id != raw_payload_id
        )
    ):
        raise ProactiveOwnershipScopeError(
            "redacted reply raw provenance is invalid"
        )


def in_quiet_hours(
    at: time_type, *, start: time_type = QUIET_START, end: time_type = QUIET_END
) -> bool:
    """Is ``at`` inside the quiet window? Handles a window that wraps midnight,
    because the settings card lets the owner set exactly that."""
    if start == end:
        return False
    if start < end:
        return start <= at < end
    return at >= start or at < end


def _owned_notification_candidate_scope(
    ownership: ProactiveOwnershipContext,
):
    owned = and_(
        Notification.subject_id == ownership.subject_id,
        Notification.recipient_user_id == ownership.recipient_user_id,
        Notification.integration_connection_id.is_not(None),
        or_(
            Notification.actor_user_id.is_(None),
            Notification.actor_user_id == ownership.recipient_user_id,
        ),
    )
    if not ownership.include_legacy_unowned:
        return owned
    return or_(
        owned,
        and_(
            Notification.subject_id.is_(None),
            Notification.actor_user_id.is_(None),
            Notification.recipient_user_id.is_(None),
            Notification.integration_connection_id.is_(None),
        ),
    )


async def _assert_no_malformed_notification_candidates(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    predicates: tuple = (),
) -> None:
    malformed = or_(
        and_(
            Notification.subject_id == ownership.subject_id,
            or_(
                Notification.recipient_user_id.is_(None),
                Notification.integration_connection_id.is_(None),
                and_(
                    Notification.actor_user_id.is_not(None),
                    Notification.actor_user_id != ownership.recipient_user_id,
                    Notification.recipient_user_id
                    == ownership.recipient_user_id,
                ),
            ),
        ),
        and_(
            Notification.subject_id.is_(None),
            or_(
                Notification.actor_user_id.is_not(None),
                Notification.recipient_user_id.is_not(None),
                Notification.integration_connection_id.is_not(None),
            ),
        ),
    )
    query = select(Notification.id).where(*predicates, malformed).limit(1)
    if await session.scalar(query) is not None:
        raise DeliveryStateError("notification candidate has malformed ownership")


async def _validate_notification_read_graph(
    session: AsyncSession,
    *,
    row: Notification,
    ownership: ProactiveOwnershipContext,
) -> bool:
    """Validate one candidate journal; False means a different owned recipient."""

    fully_legacy = (
        row.subject_id is None
        and row.actor_user_id is None
        and row.recipient_user_id is None
        and row.integration_connection_id is None
    )
    if fully_legacy:
        if row.delivery_intent_id is not None or row.ai_invocation_id is not None:
            raise DeliveryStateError("legacy notification has owned provenance links")
        return ownership.include_legacy_unowned
    if row.subject_id is None:
        raise DeliveryStateError("notification has partial legacy ownership")
    if row.subject_id != ownership.subject_id:
        return False
    if row.recipient_user_id is None or row.integration_connection_id is None:
        raise DeliveryStateError("notification has partial subject ownership")
    if row.recipient_user_id != ownership.recipient_user_id:
        return False
    if row.actor_user_id not in {None, ownership.recipient_user_id}:
        raise DeliveryStateError("notification actor is outside the current scope")
    connection = await session.scalar(
        select(IntegrationConnection)
        .where(IntegrationConnection.id == row.integration_connection_id)
        .execution_options(populate_existing=True)
    )
    if (
        connection is None
        or connection.subject_id != row.subject_id
        or connection.provider != row.channel
        or connection.connection_type
        != IntegrationConnectionType.RECIPIENT.value
        or connection.status not in HISTORICAL_RECIPIENT_STATUSES
    ):
        raise DeliveryStateError("notification connection graph is inconsistent")
    if row.delivery_intent_id is not None:
        snapshots = list(
            await session.execute(
                _intent_snapshot_select().where(
                    NotificationDeliveryIntent.id == row.delivery_intent_id
                )
            )
        )
        if len(snapshots) != 1:
            raise DeliveryStateError("linked delivery intent does not exist")
        intent = await _lock_and_validate_intent_snapshot(
            session,
            snapshot=_snapshot_from_row(snapshots[0]),
            ownership=ownership,
        )
        if (
            row.subject_id != intent.subject_id
            or row.recipient_user_id != intent.recipient_user_id
            or row.actor_user_id != intent.actor_user_id
            or row.integration_connection_id != intent.integration_connection_id
            or row.ai_invocation_id != intent.ai_invocation_id
            or row.category != intent.category
            or row.channel != intent.channel
            or row.dedupe_key != intent.idempotency_key
        ):
            raise DeliveryStateError("notification/intent graph is inconsistent")
    elif row.ai_invocation_id is not None:
        # Legacy, unlinked journals still need the complete AI provenance
        # contract: exact S/Q, category-specific purpose, Telegram source,
        # terminal status, and a persisted raw input.  A UUID-shaped link alone
        # must never make a row trusted reply context.
        try:
            raw_payload_id = await _require_ai_invocation_scope(
                session,
                ownership=ownership,
                category=row.category,
                ai_invocation_id=row.ai_invocation_id,
            )
        except (ProactiveOwnershipScopeError, TypeError, ValueError):
            raise DeliveryStateError(
                "notification AI graph is inconsistent"
            ) from None
        if raw_payload_id is None:
            raise DeliveryStateError("notification AI graph is inconsistent")
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_payload_id)
            .execution_options(populate_existing=True)
        )
        if (
            raw is None
            or raw.subject_id != row.subject_id
            or raw.actor_user_id != row.recipient_user_id
            or raw.integration_connection_id is None
            or raw.file_asset_id is not None
            or raw.domain != _INBOUND_RAW_DOMAIN
            or raw.source != Source.TELEGRAM.value
        ):
            raise DeliveryStateError("notification AI raw graph is inconsistent")
        raw_connection = await session.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.id == raw.integration_connection_id)
            .execution_options(populate_existing=True)
        )
        if (
            raw_connection is None
            or raw_connection.subject_id != row.subject_id
            or raw_connection.provider != IntegrationProvider.TELEGRAM.value
            or raw_connection.connection_type
            != IntegrationConnectionType.RECIPIENT.value
            or raw_connection.status not in HISTORICAL_RECIPIENT_STATUSES
        ):
            raise DeliveryStateError(
                "notification AI raw connection is inconsistent"
            )
        if row.category == CATEGORY_REPLY and row.payload != {
            "content_redacted": True,
            "raw_payload_id": raw_payload_id,
        }:
            raise DeliveryStateError(
                "notification AI reply payload is not an exact redacted marker"
            )
    return True


async def sent_today(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    ownership: ProactiveOwnershipContext | None = None,
) -> int:
    """How much of today's budget is spent (self-initiated messages only)."""
    on_date = on_date or now_local().date()
    _validate_ownership(ownership)
    if ownership is None:
        await _require_zero_subject_legacy_delivery(session)
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.category.in_(INITIATIVE_CATEGORIES),
                    func.date(Notification.sent_at) == on_date,
                )
            )
            or 0
        )
    journal_predicates = (
        Notification.category.in_(INITIATIVE_CATEGORIES),
        func.date(Notification.sent_at) == on_date,
    )
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
        predicates=journal_predicates,
    )
    journal_rows = list(
        await session.scalars(
            select(Notification)
            .where(*journal_predicates, _owned_notification_candidate_scope(ownership))
            .order_by(Notification.id)
        )
    )
    count = 0
    for row in journal_rows:
        if row.delivery_intent_id is not None:
            # The durable intent is counted below, so its journal cannot consume
            # the budget twice.
            await _validate_notification_read_graph(
                session,
                row=row,
                ownership=ownership,
            )
            continue
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            count += 1
    intent_rows = list(
        await session.execute(
            _intent_snapshot_select().where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id
                == ownership.recipient_user_id,
                NotificationDeliveryIntent.category.in_(INITIATIVE_CATEGORIES),
                NotificationDeliveryIntent.policy_date == on_date,
                NotificationDeliveryIntent.status.in_(
                    _INITIATIVE_CLAIM_STATUSES
                ),
            )
        )
    )
    for raw_snapshot in intent_rows:
        await _lock_and_validate_intent_snapshot(
            session,
            snapshot=_snapshot_from_row(raw_snapshot),
            ownership=ownership,
        )
        count += 1
    return count


async def find_sent(
    session: AsyncSession,
    external_id: str,
    *,
    ownership: ProactiveOwnershipContext | None = None,
) -> Optional[Notification]:
    """The journal row for a message id — how an incoming reply finds the context
    it is replying to."""
    _validate_ownership(ownership)
    if ownership is None:
        await _require_zero_subject_legacy_delivery(session)
        return await session.scalar(
            select(Notification)
            .where(Notification.external_id == str(external_id))
            .order_by(Notification.id.desc())
            .limit(1)
        )
    external_predicate = Notification.external_id == str(external_id)
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
        predicates=(external_predicate,),
    )
    rows = list(
        await session.scalars(
            select(Notification)
            .where(
                external_predicate,
                _owned_notification_candidate_scope(ownership),
            )
            .order_by(Notification.id.desc())
        )
    )
    for row in rows:
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            return row
    return None


async def recent_sent(
    session: AsyncSession,
    *,
    limit: int = 3,
    ownership: ProactiveOwnershipContext | None = None,
) -> list[Notification]:
    """The last few messages we sent, oldest first — the context a question typed
    without Telegram's Reply has to be read against.

    Almost nothing is typed as a reply on mobile, so «что за ключ странный на
    второе» arrived with no message attached and was answered against the morning
    brief's JSON: the bot could not see the echo it had sent a minute earlier and
    guessed the owner meant the 2nd of the month.
    """
    _validate_ownership(ownership)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if ownership is None:
        await _require_zero_subject_legacy_delivery(session)
        result = await session.scalars(
            select(Notification).order_by(Notification.id.desc()).limit(limit)
        )
        return list(reversed(result.all()))
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
    )
    rows = list(
        await session.scalars(
            select(Notification)
            .where(_owned_notification_candidate_scope(ownership))
            .order_by(Notification.id.desc())
            .limit(limit)
        )
    )
    validated: list[Notification] = []
    for row in rows:
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            validated.append(row)
            if len(validated) == limit:
                break
    return list(reversed(validated))


async def already_sent(
    session: AsyncSession,
    dedupe_key: str,
    *,
    ownership: ProactiveOwnershipContext | None = None,
    legacy_dedupe_key: str | None = None,
) -> bool:
    _validate_ownership(ownership)
    if ownership is None:
        await _require_zero_subject_legacy_delivery(session)
        keys = {dedupe_key}
        if legacy_dedupe_key is not None:
            keys.add(_validate_legacy_dedupe_key(legacy_dedupe_key))
        return (
            await session.scalar(
                select(Notification.id).where(Notification.dedupe_key.in_(keys))
            )
            is not None
        )
    if ownership is not None and _OPAQUE_KEY_RE.fullmatch(dedupe_key):
        if await delivery_claim_exists(
            session,
            idempotency_key=dedupe_key,
            ownership=ownership,
        ):
            return True
    keys = {dedupe_key}
    if legacy_dedupe_key is not None:
        keys.add(_validate_legacy_dedupe_key(legacy_dedupe_key))
    key_predicate = Notification.dedupe_key.in_(keys)
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
        predicates=(key_predicate,),
    )
    rows = list(
        await session.scalars(
            select(Notification)
            .where(
                key_predicate,
                _owned_notification_candidate_scope(ownership),
            )
            .order_by(Notification.id)
        )
    )
    for row in rows:
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            return True
    return False


async def _initiative_claim_count(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    policy_date: date_type,
) -> int:
    intent_count = await session.scalar(
        select(func.count())
        .select_from(NotificationDeliveryIntent)
        .where(
            NotificationDeliveryIntent.subject_id == ownership.subject_id,
            NotificationDeliveryIntent.recipient_user_id
            == ownership.recipient_user_id,
            NotificationDeliveryIntent.policy_date == policy_date,
            NotificationDeliveryIntent.category.in_(INITIATIVE_CATEGORIES),
            NotificationDeliveryIntent.status.in_(_INITIATIVE_CLAIM_STATUSES),
        )
    )
    journal_scope = or_(
        and_(
            Notification.subject_id == ownership.subject_id,
            Notification.recipient_user_id == ownership.recipient_user_id,
            or_(
                Notification.actor_user_id.is_(None),
                Notification.actor_user_id == ownership.recipient_user_id,
            ),
        ),
        and_(
            ownership.include_legacy_unowned,
            Notification.subject_id.is_(None),
            Notification.actor_user_id.is_(None),
            Notification.recipient_user_id.is_(None),
            Notification.integration_connection_id.is_(None),
        ),
    )
    journal_count = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.delivery_intent_id.is_(None),
            Notification.category.in_(INITIATIVE_CATEGORIES),
            func.date(Notification.sent_at) == policy_date,
            journal_scope,
        )
    )
    return int(intent_count or 0) + int(journal_count or 0)


async def _initiative_claim_is_within_budget(
    session: AsyncSession,
    *,
    intent: NotificationDeliveryIntent,
    ownership: ProactiveOwnershipContext,
    daily_budget: int,
) -> bool:
    journal_scope = or_(
        and_(
            Notification.subject_id == ownership.subject_id,
            Notification.recipient_user_id == ownership.recipient_user_id,
            or_(
                Notification.actor_user_id.is_(None),
                Notification.actor_user_id == ownership.recipient_user_id,
            ),
        ),
        and_(
            ownership.include_legacy_unowned,
            Notification.subject_id.is_(None),
            Notification.actor_user_id.is_(None),
            Notification.recipient_user_id.is_(None),
            Notification.integration_connection_id.is_(None),
        ),
    )
    legacy_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.delivery_intent_id.is_(None),
                Notification.category.in_(INITIATIVE_CATEGORIES),
                func.date(Notification.sent_at) == intent.policy_date,
                journal_scope,
            )
        )
        or 0
    )
    claim_ids = list(
        await session.scalars(
            select(NotificationDeliveryIntent.id)
            .where(
                NotificationDeliveryIntent.subject_id == intent.subject_id,
                NotificationDeliveryIntent.recipient_user_id
                == intent.recipient_user_id,
                NotificationDeliveryIntent.policy_date == intent.policy_date,
                NotificationDeliveryIntent.category.in_(INITIATIVE_CATEGORIES),
                NotificationDeliveryIntent.status.in_(
                    _INITIATIVE_CLAIM_STATUSES
                ),
            )
            .order_by(
                NotificationDeliveryIntent.policy_at,
                NotificationDeliveryIntent.id,
            )
        )
    )
    try:
        rank = claim_ids.index(intent.id) + 1
    except ValueError:
        raise DeliveryStateError("initiative claim is missing from its budget") from None
    return legacy_count + rank <= daily_budget


def _existing_intent_metadata(intent: NotificationDeliveryIntent) -> tuple:
    # Connection rotation alone must not reopen an occurrence.  The unique key
    # is S/Q-scoped, while every other caller-controlled provenance field must
    # remain identical.
    metadata = (
        intent.subject_id,
        intent.recipient_user_id,
        intent.actor_user_id,
        intent.raw_payload_id,
        intent.ai_invocation_id,
        intent.category,
        intent.channel,
        intent.idempotency_key,
        intent.policy_key,
    )
    if intent.category in INITIATIVE_CATEGORIES:
        return (*metadata, intent.policy_date)
    return metadata


async def _matching_journal_claim(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None,
    category: str,
    channel: str,
    idempotency_key: str,
    legacy_dedupe_key: str | None,
    raw_payload_id: int | None,
    ai_invocation_id: uuid.UUID | None,
) -> Notification | None:
    keys = {idempotency_key}
    if legacy_dedupe_key is not None:
        keys.add(legacy_dedupe_key)
    predicates = [Notification.dedupe_key.in_(keys)]
    if ai_invocation_id is not None:
        predicates.append(Notification.ai_invocation_id == ai_invocation_id)
    if raw_payload_id is not None:
        predicates.append(
            and_(
                Notification.category == category,
                Notification.payload["raw_payload_id"].as_integer()
                == raw_payload_id,
            )
        )
    candidate_scope = or_(
        Notification.subject_id == ownership.subject_id,
        and_(
            Notification.subject_id.is_(None),
            or_(
                Notification.actor_user_id.is_not(None),
                Notification.recipient_user_id.is_not(None),
                Notification.integration_connection_id.is_not(None),
                and_(
                    Notification.actor_user_id.is_(None),
                    Notification.recipient_user_id.is_(None),
                    Notification.integration_connection_id.is_(None),
                ),
            ),
        ),
    )
    rows = list(
        await session.scalars(
            select(Notification)
            .where(or_(*predicates), candidate_scope)
            .order_by(Notification.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for row in rows:
        fully_legacy = (
            row.subject_id is None
            and row.actor_user_id is None
            and row.recipient_user_id is None
            and row.integration_connection_id is None
        )
        if fully_legacy:
            if not ownership.include_legacy_unowned:
                continue
            if row.category != category or row.channel != channel:
                raise DeliveryIdempotencyConflictError(
                    "legacy notification claim metadata conflicts"
                )
            return row
        if row.subject_id is None:
            raise DeliveryIdempotencyConflictError(
                "notification claim has partial legacy ownership"
            )
        if row.recipient_user_id is None or row.integration_connection_id is None:
            raise DeliveryIdempotencyConflictError(
                "notification claim has partial subject ownership"
            )
        if (
            row.subject_id != ownership.subject_id
            or row.recipient_user_id != ownership.recipient_user_id
        ):
            continue
        valid_owned = await session.scalar(
            select(Notification.id).where(
                Notification.id == row.id,
                notification_ownership_scope(
                    ownership,
                    connection_scoped=False,
                ),
            )
        )
        if valid_owned is None:
            raise DeliveryIdempotencyConflictError(
                "notification claim has invalid owned provenance"
            )
        if (
            row.actor_user_id != actor_user_id
            or row.category != category
            or row.channel != channel
            or (
                ai_invocation_id is not None
                and row.ai_invocation_id != ai_invocation_id
            )
        ):
            raise DeliveryIdempotencyConflictError(
                "notification claim metadata conflicts"
            )
        return row
    return None


def _validate_legacy_dedupe_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("legacy_dedupe_key must be a non-blank bounded string")
    return value


def _validate_reply_target(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    if not normalized.isdigit() or int(normalized) <= 0 or len(normalized) > 64:
        raise ValueError("reply_to must be a positive Telegram message id")
    return normalized


def _validate_delivery_category(category: str) -> str:
    if category not in _DELIVERY_CATEGORIES:
        raise ValueError("delivery category is invalid")
    return category


def _preparation_scope_fingerprint(scope: DeliveryPreparationScope) -> tuple:
    authority = scope._authority
    return (
        scope._ownership_subject_id,
        scope._ownership_recipient_user_id,
        scope._ownership_connection_id,
        scope._include_legacy_unowned,
        scope._actor_user_id,
        scope._category,
        authority.binding,
        authority.timezone.key,
        authority.credential_ref,
        authority.telegram_connection_ids,
        scope._module_enabled,
        scope._policy,
        scope._policy_at,
        scope._local_at,
    )


async def lock_delivery_preparation_scope(
    session: AsyncSession,
    notifier: BoundNotifier | None,
    *,
    category: str,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> DeliveryPreparationScope | None:
    """Prelock every live root needed by a composed delivery T1 transaction.

    Callers that must atomically persist deterministic domain state and a PENDING
    delivery intent acquire this scope before taking any raw/domain locks, then
    pass it to :func:`prepare_delivery_intent` in the same exact outer
    transaction. No rendered payload is retained here.
    """

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    if session.in_nested_transaction():
        raise DeliveryCapabilityError(
            "delivery preparation scope requires an outer transaction"
        )
    if notifier is None or not isinstance(notifier, BoundNotifier):
        return None
    category = _validate_delivery_category(category)
    _validate_ownership(ownership, actor_user_id=actor_user_id)
    if ownership is None:
        raise TypeError("delivery preparation scope requires ownership")
    expected_binding = _binding_for(ownership)
    if (
        notifier.channel != expected_binding.channel
        or not _same_binding(notifier.binding, expected_binding)
    ):
        raise DeliveryScopeError("notifier binding does not match delivery ownership")

    authority = await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=actor_user_id,
    )
    if not _same_binding(authority.binding, notifier.binding):
        raise DeliveryScopeError("notifier binding is not the current endpoint")
    module_enabled = await _locked_signals_module_enabled(
        session,
        subject_id=ownership.subject_id,
    )
    policy = await _load_locked_delivery_policy(session, authority=authority)
    policy_at, local_at = _policy_clock(
        timezone_value=authority.timezone,
        now=now,
    )
    return DeliveryPreparationScope._issue(
        session=session,
        authority=authority,
        module_enabled=module_enabled,
        policy=policy,
        policy_at=policy_at,
        local_at=local_at,
        category=category,
        ownership=ownership,
        actor_user_id=actor_user_id,
    )


async def _revalidate_delivery_preparation_scope(
    session: AsyncSession,
    scope: DeliveryPreparationScope,
) -> None:
    """Reproject already-locked authority and policy without taking earlier locks."""

    # The strict policy projection intentionally uses ``no_autoflush``. Flush the
    # caller's composed domain state and any same-session root/policy mutation now
    # so revalidation can never authorize the intent from a stale ORM snapshot.
    await session.flush()
    subject_rows = list(
        await session.scalars(
            select(HealthSubject)
            .order_by(HealthSubject.id)
            .limit(2)
            .execution_options(populate_existing=True)
        )
    )
    if len(subject_rows) != 1:
        raise DeliveryScopeError("delivery preparation subject scope changed")
    subject = subject_rows[0]
    if (
        subject.id != scope._ownership_subject_id
        or subject.owner_user_id != scope._ownership_recipient_user_id
    ):
        raise DeliveryScopeError("delivery preparation subject authority changed")
    try:
        subject_timezone = ZoneInfo(subject.timezone)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise DeliveryScopeError("delivery preparation timezone changed") from exc
    if subject_timezone.key != scope._authority.timezone.key:
        raise DeliveryScopeError("delivery preparation timezone changed")

    user_ids = sorted(
        {
            scope._ownership_recipient_user_id,
            *(
                {scope._actor_user_id}
                if scope._actor_user_id is not None
                else set()
            ),
        },
        key=str,
    )
    users = list(
        await session.scalars(
            select(User)
            .where(User.id.in_(user_ids))
            .order_by(User.id)
            .execution_options(populate_existing=True)
        )
    )
    if len(users) != len(user_ids) or any(
        row.status != UserStatus.ACTIVE.value for row in users
    ):
        raise DeliveryScopeError("delivery preparation user authority changed")

    connections = list(
        await session.scalars(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.subject_id == scope._ownership_subject_id,
                IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
                IntegrationConnection.connection_type
                == IntegrationConnectionType.RECIPIENT.value,
            )
            .order_by(IntegrationConnection.id)
            .execution_options(populate_existing=True)
        )
    )
    known_statuses = {item.value for item in IntegrationConnectionStatus}
    current = [
        row
        for row in connections
        if row.status != IntegrationConnectionStatus.RETIRED.value
    ]
    if (
        frozenset(row.id for row in connections)
        != scope._authority.telegram_connection_ids
        or any(row.status not in known_statuses for row in connections)
        or len(current) != 1
        or current[0].id != scope._ownership_connection_id
        or current[0].status
        not in {
            IntegrationConnectionStatus.LEGACY.value,
            IntegrationConnectionStatus.ACTIVE.value,
        }
        or current[0].credential_ref != scope._authority.credential_ref
    ):
        raise DeliveryScopeError("delivery preparation connection authority changed")

    # The module re-check stood here — the layer's master switch could be turned
    # off between preparing a send and committing it, and a message already
    # composed had to stop. There is no switch any more (see
    # ``_locked_signals_module_enabled``), so there is nothing to have changed.
    policy = await _read_delivery_policy(session, authority=scope._authority)
    if policy != scope._policy:
        raise DeliveryPolicyUnavailableError(
            "Telegram delivery policy changed during delivery preparation"
        )


async def _consume_delivery_preparation_scope(
    session: AsyncSession,
    scope: DeliveryPreparationScope,
    *,
    notifier: BoundNotifier,
    category: str,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None,
    now: datetime | None,
) -> tuple[_LockedDeliveryAuthority, bool, _DeliveryPolicy, datetime, datetime]:
    if (
        not isinstance(scope, DeliveryPreparationScope)
        or scope._seal is not _PREPARATION_SCOPE_SEAL
        or scope._consumed
        or scope._session is not session
        or scope._transaction is None
        or session.sync_session.get_transaction() is not scope._transaction
        or session.in_nested_transaction()
        or scope._fingerprint != _preparation_scope_fingerprint(scope)
    ):
        raise DeliveryCapabilityError(
            "delivery preparation scope is forged, expired, or already consumed"
        )
    expected_binding = _binding_for(ownership)
    if (
        scope._ownership_subject_id != ownership.subject_id
        or scope._ownership_recipient_user_id != ownership.recipient_user_id
        or scope._ownership_connection_id != ownership.connection_id
        or scope._include_legacy_unowned != ownership.include_legacy_unowned
        or scope._actor_user_id != actor_user_id
        or scope._category != category
        or notifier.channel != expected_binding.channel
        or not _same_binding(notifier.binding, expected_binding)
        or not _same_binding(scope._authority.binding, expected_binding)
    ):
        raise DeliveryCapabilityError(
            "delivery preparation scope does not match the continuation"
        )
    if now is not None:
        policy_at, local_at = _policy_clock(
            timezone_value=scope._authority.timezone,
            now=now,
        )
        if policy_at != scope._policy_at or local_at != scope._local_at:
            raise DeliveryCapabilityError(
                "delivery preparation clock changed after prelock"
            )
    values = (
        scope._authority,
        scope._module_enabled,
        scope._policy,
        scope._policy_at,
        scope._local_at,
    )
    scope._invalidate()
    await _revalidate_delivery_preparation_scope(session, scope)
    return values


async def prepare_delivery_intent(
    session: AsyncSession,
    notifier: BoundNotifier | None,
    *,
    text: str,
    category: str,
    idempotency_key: str,
    policy_key: str | None = None,
    legacy_dedupe_key: str | None = None,
    buttons: Optional[Buttons] = None,
    reply_to: str | None = None,
    now: datetime | None = None,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None = None,
    raw_payload_id: int | None = None,
    ai_invocation_id: uuid.UUID | None = None,
    redact_journal_content: bool = False,
    preparation_scope: DeliveryPreparationScope | None = None,
) -> PreparedDeliveryIntent | None:
    """T1: reserve one fresh, non-PHI, subject-scoped outbound claim.

    The returned capability is armed only by the caller's successful outer
    commit.  An existing intent in any state suppresses dispatch; its missing
    payload can never be reconstructed from caller-supplied text.
    """

    if session.in_nested_transaction():
        raise DeliveryCapabilityError("delivery preparation requires an outer transaction")
    if notifier is None or not isinstance(notifier, BoundNotifier):
        if preparation_scope is not None:
            raise DeliveryCapabilityError(
                "scoped delivery continuation requires an exact bound notifier"
            )
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    text = canonicalize_text(text)
    frozen_buttons = canonicalize_buttons(buttons)
    category = _validate_delivery_category(category)
    idempotency_key = _opaque_key(
        idempotency_key,
        field_name="idempotency_key",
    )
    if category == CATEGORY_NUDGE:
        if policy_key is None:
            raise ValueError("nudge delivery requires policy_key")
        policy_key = _opaque_key(policy_key, field_name="policy_key")
    elif policy_key is not None:
        raise ValueError("policy_key is allowed only for nudge delivery")
    legacy_dedupe_key = _validate_legacy_dedupe_key(legacy_dedupe_key)
    reply_to = _validate_reply_target(reply_to)
    if raw_payload_id is not None and (
        isinstance(raw_payload_id, bool)
        or not isinstance(raw_payload_id, int)
        or raw_payload_id < 1
    ):
        raise ValueError("raw_payload_id must be a positive integer or None")
    if raw_payload_id is not None and category not in {CATEGORY_REPLY, CATEGORY_ECHO}:
        raise DeliveryScopeError("raw provenance is allowed only for reply or echo")
    if ai_invocation_id is not None and raw_payload_id is None:
        raise DeliveryScopeError("AI delivery provenance requires a raw payload")
    if not isinstance(redact_journal_content, bool):
        raise TypeError("redact_journal_content must be a bool")
    if redact_journal_content and (
        category != CATEGORY_REPLY or raw_payload_id is None
    ):
        raise DeliveryScopeError(
            "redacted delivery journals require a raw-backed reply"
        )
    if (
        ai_invocation_id is not None
        and category == CATEGORY_REPLY
        and not redact_journal_content
    ):
        raise DeliveryScopeError("AI question replies require a redacted journal")
    _validate_ownership(ownership, actor_user_id=actor_user_id)
    expected_binding = _binding_for(ownership)
    if notifier is not None and (
        notifier.channel != expected_binding.channel
        or not _same_binding(notifier.binding, expected_binding)
    ):
        raise DeliveryScopeError("notifier binding does not match delivery ownership")

    if preparation_scope is None:
        preparation_scope = await lock_delivery_preparation_scope(
            session,
            notifier,
            category=category,
            ownership=ownership,
            actor_user_id=actor_user_id,
            now=now,
        )
        if preparation_scope is None:
            return None
    authority, module_enabled, policy, policy_at, local_at = (
        await _consume_delivery_preparation_scope(
            session,
            preparation_scope,
            notifier=notifier,
            category=category,
            ownership=ownership,
            actor_user_id=actor_user_id,
            now=now,
        )
    )
    provisional = NotificationDeliveryIntent(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.recipient_user_id,
        actor_user_id=actor_user_id,
        integration_connection_id=ownership.connection_id,
        raw_payload_id=raw_payload_id,
        ai_invocation_id=ai_invocation_id,
        category=category,
        channel=authority.binding.channel,
        idempotency_key=idempotency_key,
        policy_key=policy_key,
        policy_at=policy_at,
        policy_date=local_at.date(),
        status=NotificationDeliveryStatus.PENDING.value,
    )
    await _lock_raw_and_ai_provenance(
        session,
        intent=provisional,
        telegram_connection_ids=authority.telegram_connection_ids,
    )

    existing = await session.scalar(
        select(NotificationDeliveryIntent)
        .where(
            NotificationDeliveryIntent.subject_id == ownership.subject_id,
            NotificationDeliveryIntent.recipient_user_id
            == ownership.recipient_user_id,
            NotificationDeliveryIntent.idempotency_key == idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    expected_metadata = _existing_intent_metadata(provisional)
    if existing is not None:
        if _existing_intent_metadata(existing) != expected_metadata:
            raise DeliveryIdempotencyConflictError(
                "delivery idempotency key metadata conflicts"
            )
        if existing.integration_connection_id not in authority.telegram_connection_ids:
            raise DeliveryStateError(
                "existing delivery intent has invalid historical connection"
            )
        await _validate_linked_journal_for_intent(session, intent=existing)
        return None
    if raw_payload_id is not None:
        raw_claims = list(
            await session.scalars(
                select(NotificationDeliveryIntent)
                .where(
                    NotificationDeliveryIntent.raw_payload_id == raw_payload_id,
                    NotificationDeliveryIntent.category == category,
                )
                .order_by(NotificationDeliveryIntent.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(raw_claims) > 1:
            raise DeliveryStateError(
                "raw payload already has multiple delivery claims"
            )
        if raw_claims:
            raw_claim = raw_claims[0]
            if (
                raw_claim.subject_id != ownership.subject_id
                or raw_claim.recipient_user_id != ownership.recipient_user_id
                or raw_claim.actor_user_id != actor_user_id
                or raw_claim.ai_invocation_id != ai_invocation_id
                or raw_claim.channel != authority.binding.channel
                or raw_claim.integration_connection_id
                not in authority.telegram_connection_ids
            ):
                raise DeliveryIdempotencyConflictError(
                    "raw delivery occurrence metadata conflicts"
                )
            await _validate_linked_journal_for_intent(
                session,
                intent=raw_claim,
            )
            return None
    if ai_invocation_id is not None:
        existing_ai = await session.scalar(
            select(NotificationDeliveryIntent)
            .where(NotificationDeliveryIntent.ai_invocation_id == ai_invocation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing_ai is not None:
            if _existing_intent_metadata(existing_ai) != expected_metadata:
                raise DeliveryIdempotencyConflictError(
                    "AI invocation is bound to a different delivery intent"
                )
            if (
                existing_ai.integration_connection_id
                not in authority.telegram_connection_ids
            ):
                raise DeliveryStateError(
                    "existing AI delivery intent has invalid historical connection"
                )
            await _validate_linked_journal_for_intent(
                session,
                intent=existing_ai,
            )
            return None
    if await _matching_journal_claim(
        session,
        ownership=ownership,
        actor_user_id=actor_user_id,
        category=category,
        channel=authority.binding.channel,
        idempotency_key=idempotency_key,
        legacy_dedupe_key=legacy_dedupe_key,
        raw_payload_id=raw_payload_id,
        ai_invocation_id=ai_invocation_id,
    ) is not None:
        return None

    if not module_enabled:
        if (
            raw_payload_id is not None
            and category in {CATEGORY_REPLY, CATEGORY_ECHO}
        ):
            _cancel_pending_intent(
                provisional,
                completed_at=policy_at,
                error_code=NotificationDeliveryErrorCode.CANCELLED_BY_POLICY,
            )
            session.add(provisional)
            await session.flush()
        return None
    if category == CATEGORY_NUDGE and in_quiet_hours(
        local_at.time().replace(tzinfo=None),
        start=policy.quiet_start,
        end=policy.quiet_end,
    ):
        return None
    if category in INITIATIVE_CATEGORIES and await _initiative_claim_count(
        session,
        ownership=ownership,
        policy_date=local_at.date(),
    ) >= policy.daily_budget:
        return None

    session.add(provisional)
    await session.flush()
    return PreparedDeliveryIntent._issue(
        session=session,
        intent=provisional,
        binding=authority.binding,
        text=text,
        buttons=frozen_buttons,
        reply_to=reply_to,
        sent_at=local_at.replace(tzinfo=None),
        redact_journal_content=redact_journal_content,
        journal_raw_payload_id=(raw_payload_id if redact_journal_content else None),
    )


async def rearm_stale_raw_delivery_intent(
    session: AsyncSession,
    notifier: BoundNotifier | None,
    *,
    text: str,
    category: str,
    idempotency_key: str,
    stale_before: datetime,
    legacy_dedupe_key: str | None = None,
    buttons: Optional[Buttons] = None,
    reply_to: str | None = None,
    now: datetime | None = None,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None = None,
    raw_payload_id: int,
    ai_invocation_id: uuid.UUID | None = None,
    redact_journal_content: bool = False,
) -> PreparedDeliveryIntent | None:
    """Reissue T1 memory state for one abandoned raw-backed PENDING claim.

    The intent deliberately contains no rendered payload, so ordinary duplicate
    preparation can never resume it. This narrower recovery seam is available
    only to deterministic inbound reply/echo handlers after the row has become
    stale. It revalidates the complete live and historical graph under the
    canonical locks, and the returned capability is armed only by this root
    transaction's commit just like a freshly inserted T1 capability.
    """

    if session.in_nested_transaction():
        raise DeliveryCapabilityError("delivery re-arm requires an outer transaction")
    if notifier is not None and not isinstance(notifier, BoundNotifier):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    text = canonicalize_text(text)
    frozen_buttons = canonicalize_buttons(buttons)
    if category not in {CATEGORY_REPLY, CATEGORY_ECHO}:
        raise DeliveryScopeError("only raw-backed reply or echo may be re-armed")
    idempotency_key = _opaque_key(
        idempotency_key,
        field_name="idempotency_key",
    )
    legacy_dedupe_key = _validate_legacy_dedupe_key(legacy_dedupe_key)
    reply_to = _validate_reply_target(reply_to)
    if (
        isinstance(raw_payload_id, bool)
        or not isinstance(raw_payload_id, int)
        or raw_payload_id < 1
    ):
        raise ValueError("raw_payload_id must be a positive integer")
    if ai_invocation_id is not None and not isinstance(ai_invocation_id, uuid.UUID):
        raise TypeError("ai_invocation_id must be a UUID or None")
    if not isinstance(redact_journal_content, bool):
        raise TypeError("redact_journal_content must be a bool")
    if redact_journal_content and category != CATEGORY_REPLY:
        raise DeliveryScopeError(
            "redacted delivery journals require a raw-backed reply"
        )
    if (
        ai_invocation_id is not None
        and category == CATEGORY_REPLY
        and not redact_journal_content
    ):
        raise DeliveryScopeError("AI question replies require a redacted journal")
    _validate_ownership(ownership, actor_user_id=actor_user_id)
    expected_binding = _binding_for(ownership)
    if notifier is not None and (
        notifier.channel != expected_binding.channel
        or not _same_binding(notifier.binding, expected_binding)
    ):
        raise DeliveryScopeError("notifier binding does not match delivery ownership")

    cutoff = _reconciliation_now(stale_before)
    authority = await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=actor_user_id,
    )
    if notifier is not None and not _same_binding(
        authority.binding, notifier.binding
    ):
        raise DeliveryScopeError("notifier binding is not the current endpoint")
    module_enabled = await _locked_signals_module_enabled(
        session,
        subject_id=ownership.subject_id,
    )
    rearmed_at, local_at = _policy_clock(
        timezone_value=authority.timezone,
        now=now,
    )
    if cutoff > rearmed_at:
        raise ValueError("stale_before cannot be later than the re-arm instant")
    if module_enabled:
        if notifier is None:
            # Missing transport is recoverable. It cannot authorize a payload
            # capability, but it must not erase the stale claim either.
            return None
        # The strict scoped policy is part of live delivery authority even
        # though reply/echo are exempt from quiet hours and initiative budget.
        await _load_locked_delivery_policy(session, authority=authority)

    provisional = NotificationDeliveryIntent(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.recipient_user_id,
        actor_user_id=actor_user_id,
        integration_connection_id=ownership.connection_id,
        raw_payload_id=raw_payload_id,
        ai_invocation_id=ai_invocation_id,
        category=category,
        channel=authority.binding.channel,
        idempotency_key=idempotency_key,
        policy_key=None,
        policy_at=rearmed_at,
        policy_date=local_at.date(),
        status=NotificationDeliveryStatus.PENDING.value,
    )
    # Raw/AI rows are locked before the intent row. The raw may legitimately
    # point at a retired Telegram recipient connection after C rotation.
    await _lock_raw_and_ai_provenance(
        session,
        intent=provisional,
        telegram_connection_ids=authority.telegram_connection_ids,
    )

    candidate_predicates = [
        and_(
            NotificationDeliveryIntent.subject_id == ownership.subject_id,
            NotificationDeliveryIntent.recipient_user_id
            == ownership.recipient_user_id,
            NotificationDeliveryIntent.idempotency_key == idempotency_key,
        ),
        and_(
            NotificationDeliveryIntent.raw_payload_id == raw_payload_id,
            NotificationDeliveryIntent.category == category,
        ),
    ]
    if ai_invocation_id is not None:
        candidate_predicates.append(
            NotificationDeliveryIntent.ai_invocation_id == ai_invocation_id
        )
    candidates = list(
        await session.scalars(
            select(NotificationDeliveryIntent)
            .where(or_(*candidate_predicates))
            .order_by(NotificationDeliveryIntent.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if not candidates:
        return None
    if len(candidates) != 1:
        raise DeliveryIdempotencyConflictError(
            "raw delivery recovery resolves to multiple claims"
        )
    intent = candidates[0]
    if (
        intent.subject_id != ownership.subject_id
        or intent.recipient_user_id != ownership.recipient_user_id
        or intent.actor_user_id != actor_user_id
        or intent.raw_payload_id != raw_payload_id
        or intent.ai_invocation_id != ai_invocation_id
        or intent.category != category
        or intent.channel != authority.binding.channel
        or intent.idempotency_key != idempotency_key
        or intent.policy_key is not None
    ):
        raise DeliveryIdempotencyConflictError(
            "raw delivery recovery metadata conflicts"
        )
    if intent.integration_connection_id not in authority.telegram_connection_ids:
        raise DeliveryScopeError(
            "raw delivery recovery has an invalid historical connection"
        )
    await _validate_linked_journal_for_intent(session, intent=intent)
    if intent.status == NotificationDeliveryStatus.PENDING.value:
        if any(
            value is not None
            for value in (
                intent.lease_token,
                intent.dispatch_started_at,
                intent.completed_at,
                intent.error_code,
            )
        ):
            raise DeliveryStateError("pending delivery intent lifecycle is malformed")
        if _aware_utc(intent.updated_at) >= cutoff:
            return None
    elif intent.status == NotificationDeliveryStatus.CANCELLED.value:
        if (
            intent.error_code
            not in {
                NotificationDeliveryErrorCode.STALE_PENDING.value,
                NotificationDeliveryErrorCode.SCOPE_INVALID.value,
            }
            or intent.lease_token is not None
            or intent.dispatch_started_at is not None
            or intent.completed_at is None
        ):
            raise DeliveryStateError(
                "cancelled delivery intent is not safe to re-arm"
            )
        if max(
            _aware_utc(intent.updated_at),
            _aware_utc(intent.completed_at),
        ) >= cutoff:
            return None
    else:
        raise DeliveryStateError(
            "dispatching or terminal delivery intent cannot be re-armed"
        )
    if await _matching_journal_claim(
        session,
        ownership=ownership,
        actor_user_id=actor_user_id,
        category=category,
        channel=authority.binding.channel,
        idempotency_key=idempotency_key,
        legacy_dedupe_key=legacy_dedupe_key,
        raw_payload_id=raw_payload_id,
        ai_invocation_id=ai_invocation_id,
    ) is not None:
        return None
    if not module_enabled:
        _cancel_pending_intent(
            intent,
            completed_at=rearmed_at,
            error_code=NotificationDeliveryErrorCode.CANCELLED_BY_POLICY,
        )
        await session.flush()
        return None

    # No provider attempt has started, so a historical C may be rebound to the
    # exact current recipient. Refreshing updated_at also gives a committed
    # recovery capability its own abandonment window if the process crashes
    # again before T2.
    intent.integration_connection_id = authority.binding.integration_connection_id
    intent.policy_at = rearmed_at
    intent.policy_date = local_at.date()
    intent.status = NotificationDeliveryStatus.PENDING.value
    intent.lease_token = None
    intent.dispatch_started_at = None
    intent.completed_at = None
    intent.error_code = None
    intent.updated_at = rearmed_at
    await session.flush()
    return PreparedDeliveryIntent._issue(
        session=session,
        intent=intent,
        binding=authority.binding,
        text=text,
        buttons=frozen_buttons,
        reply_to=reply_to,
        sent_at=local_at.replace(tzinfo=None),
        redact_journal_content=redact_journal_content,
        journal_raw_payload_id=(raw_payload_id if redact_journal_content else None),
    )


def _bind_prepared_start(
    prepared: PreparedDeliveryIntent,
    *,
    session: AsyncSession,
    lease: DeliveryDispatchLease | None,
) -> None:
    prepared_ref = weakref.ref(prepared)
    lease_ref = weakref.ref(lease) if lease is not None else None
    object.__setattr__(prepared, "_finalizing", True)

    def after_commit() -> None:
        target = prepared_ref()
        if target is not None:
            object.__setattr__(target, "_consumed", True)
            object.__setattr__(target, "_finalizing", False)
            target._clear_payload()
            object.__setattr__(target, "_session", None)
        lease_target = lease_ref() if lease_ref is not None else None
        if lease_target is not None:
            object.__setattr__(lease_target, "_armed", True)

    def after_rollback() -> None:
        target = prepared_ref()
        if target is not None:
            object.__setattr__(target, "_finalizing", False)
        lease_target = lease_ref() if lease_ref is not None else None
        if lease_target is not None:
            lease_target._invalidate()

    _register_transaction_outcome(
        session,
        on_commit=after_commit,
        on_rollback=after_rollback,
    )


def _cancel_pending_intent(
    intent: NotificationDeliveryIntent,
    *,
    completed_at: datetime,
    error_code: NotificationDeliveryErrorCode,
) -> None:
    intent.status = NotificationDeliveryStatus.CANCELLED.value
    intent.lease_token = None
    intent.dispatch_started_at = None
    intent.completed_at = completed_at
    intent.error_code = error_code.value


async def start_delivery_dispatch(
    session: AsyncSession,
    prepared: PreparedDeliveryIntent,
    *,
    now: datetime | None = None,
    notifier_resolver: BoundNotifierResolver = resolve_legacy_bound_notifier,
) -> DeliveryDispatchLease | None:
    """T2: freshly reauthorize policy/C and commit-arm one dispatch lease."""

    if session.in_nested_transaction():
        raise DeliveryCapabilityError("start dispatch requires an outer transaction")
    if not _prepared_is_valid(prepared) or not _prepared_fingerprint_matches(prepared):
        raise DeliveryCapabilityError(
            "prepared delivery is stale, uncommitted, forged, or consumed"
        )
    if not callable(notifier_resolver):
        raise TypeError("notifier_resolver must be synchronous and callable")
    snapshot = _snapshot_from_fingerprint(prepared._fingerprint)
    ownership = ProactiveOwnershipContext(
        subject_id=snapshot.subject_id,
        recipient_user_id=snapshot.recipient_user_id,
        connection_id=snapshot.integration_connection_id,
        include_legacy_unowned=True,
    )
    authority = await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=snapshot.actor_user_id,
    )
    module_enabled = await _locked_signals_module_enabled(
        session,
        subject_id=snapshot.subject_id,
    )
    policy = await _load_locked_delivery_policy(session, authority=authority)
    current_at, current_local = _policy_clock(
        timezone_value=authority.timezone,
        now=now,
    )
    await _lock_raw_and_ai_provenance(
        session,
        intent=snapshot,
        telegram_connection_ids=authority.telegram_connection_ids,
    )
    intent = await session.scalar(
        select(NotificationDeliveryIntent)
        .where(NotificationDeliveryIntent.id == snapshot.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is None or _intent_fingerprint(intent) != prepared._fingerprint:
        raise DeliveryCapabilityError("prepared delivery provenance no longer matches")
    if intent.status != NotificationDeliveryStatus.PENDING.value:
        raise DeliveryStateError("delivery intent cannot obtain another lease")

    cancelled = (
        not module_enabled
        or (
            intent.category in INITIATIVE_CATEGORIES
            and current_local.date() != intent.policy_date
        )
        or (
            intent.category == CATEGORY_NUDGE
            and in_quiet_hours(
                current_local.time().replace(tzinfo=None),
                start=policy.quiet_start,
                end=policy.quiet_end,
            )
        )
    )
    if not cancelled and intent.category in INITIATIVE_CATEGORIES:
        cancelled = not await _initiative_claim_is_within_budget(
            session,
            intent=intent,
            ownership=ownership,
            daily_budget=policy.daily_budget,
        )
    if cancelled:
        _cancel_pending_intent(
            intent,
            completed_at=current_at,
            error_code=NotificationDeliveryErrorCode.CANCELLED_BY_POLICY,
        )
        await session.flush()
        _bind_prepared_start(prepared, session=session, lease=None)
        return None

    notifier = notifier_resolver(authority.binding, authority.credential_ref)
    if notifier is None:
        _cancel_pending_intent(
            intent,
            completed_at=current_at,
            error_code=NotificationDeliveryErrorCode.SCOPE_INVALID,
        )
        await session.flush()
        _bind_prepared_start(prepared, session=session, lease=None)
        return None
    if (
        not isinstance(notifier, BoundNotifier)
        or notifier.channel != authority.binding.channel
        or not _same_binding(notifier.binding, authority.binding)
    ):
        raise DeliveryScopeError("resolved notifier is not bound to exact S/Q/C")

    lease_token = uuid.uuid4()
    intent.status = NotificationDeliveryStatus.DISPATCHING.value
    intent.lease_token = lease_token
    intent.dispatch_started_at = current_at
    intent.completed_at = None
    intent.error_code = None
    await session.flush()
    lease = DeliveryDispatchLease._issue(
        session=session,
        prepared=prepared,
        lease_token=lease_token,
        notifier=notifier,
        sent_at=current_local.replace(tzinfo=None),
    )
    _bind_prepared_start(prepared, session=session, lease=lease)
    return lease


def _valid_telegram_external_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if not value.isdigit() or len(value) > 64 or int(value) <= 0:
        return None
    return value


async def dispatch_delivery(
    lease: DeliveryDispatchLease,
) -> DeliveryCompletion:
    """Consume one committed lease and perform exactly one Telegram call."""

    if (
        not isinstance(lease, DeliveryDispatchLease)
        or lease._seal is not _LEASE_SEAL
        or not lease._armed
        or lease._consumed
        or lease._session is None
        or lease._notifier is None
        or lease._text is None
    ):
        raise DeliveryCapabilityError(
            "delivery lease is stale, uncommitted, forged, or consumed"
        )
    snapshot = _snapshot_from_fingerprint(lease._fingerprint)
    if not _same_binding(lease._binding, DeliveryEndpointBinding(
        subject_id=snapshot.subject_id,
        recipient_user_id=snapshot.recipient_user_id,
        integration_connection_id=snapshot.integration_connection_id,
        channel=snapshot.channel,
    )):
        raise DeliveryCapabilityError("delivery lease fingerprint is invalid")
    issuing_session = lease._session
    if issuing_session.in_transaction():
        raise DeliveryCapabilityError("Telegram call cannot span a DB transaction")

    notifier = lease._notifier
    if monotonic() - lease._issued_monotonic >= DISPATCHING_STALE_AFTER.total_seconds():
        lease._invalidate()
        logger.warning("delivery ended ambiguously (code=stale_dispatch)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.STALE_DISPATCH,
            external_id=None,
            text=None,
            buttons=None,
        )
    if (
        not isinstance(notifier, BoundNotifier)
        or notifier.channel != lease._binding.channel
        or not _same_binding(notifier.binding, lease._binding)
    ):
        lease._invalidate()
        logger.warning("delivery ended ambiguously (code=internal_error)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.INTERNAL_ERROR,
            external_id=None,
            text=None,
            buttons=None,
        )
    text = lease._text
    buttons = lease._buttons
    reply_to = lease._reply_to
    object.__setattr__(lease, "_consumed", True)
    object.__setattr__(lease, "_notifier", None)
    object.__setattr__(lease, "_text", None)
    object.__setattr__(lease, "_buttons", None)
    object.__setattr__(lease, "_reply_to", None)
    object.__setattr__(lease, "_session", None)
    try:
        external_id = await notifier.send(
            text,
            buttons=buttons,
            reply_to=reply_to,
        )
    except asyncio.CancelledError:
        logger.warning("delivery ended ambiguously (code=transport_error)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.TRANSPORT_ERROR,
            external_id=None,
            text=None,
            buttons=None,
        )
    except Exception:
        logger.warning("delivery ended ambiguously (code=transport_error)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.TRANSPORT_ERROR,
            external_id=None,
            text=None,
            buttons=None,
        )
    valid_external_id = _valid_telegram_external_id(external_id)
    if valid_external_id is None:
        logger.warning("delivery ended ambiguously (code=invalid_response)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.INVALID_RESPONSE,
            external_id=None,
            text=None,
            buttons=None,
        )
    return DeliveryCompletion._issue(
        lease=lease,
        status=NotificationDeliveryStatus.SENT,
        error_code=None,
        external_id=valid_external_id,
        text=text,
        buttons=buttons,
    )


def _completion_is_valid(completion: object) -> bool:
    if (
        not isinstance(completion, DeliveryCompletion)
        or completion._seal is not _COMPLETION_SEAL
        or completion._consumed
        or completion._finalizing
        or not isinstance(completion._lease_token, uuid.UUID)
    ):
        return False
    if completion._status is NotificationDeliveryStatus.SENT:
        return bool(
            completion._error_code is None
            and _valid_telegram_external_id(completion._external_id) is not None
            and completion._text is not None
        )
    return bool(
        completion._status is NotificationDeliveryStatus.AMBIGUOUS
        and completion._error_code
        in {
            NotificationDeliveryErrorCode.TRANSPORT_ERROR,
            NotificationDeliveryErrorCode.INVALID_RESPONSE,
            NotificationDeliveryErrorCode.STALE_DISPATCH,
            NotificationDeliveryErrorCode.INTERNAL_ERROR,
        }
        and completion._external_id is None
        and completion._text is None
        and completion._buttons is None
    )


def _journal_payload(completion: DeliveryCompletion) -> dict:
    if completion._redact_journal_content:
        return {
            "content_redacted": True,
            "raw_payload_id": completion._journal_raw_payload_id,
        }
    return {
        "text": completion._text,
        "buttons": (
            [list(button) for button in completion._buttons]
            if completion._buttons
            else None
        ),
    }


def _journal_matches_completion(
    row: Notification,
    *,
    intent: NotificationDeliveryIntent,
    completion: DeliveryCompletion,
) -> bool:
    return (
        row.delivery_intent_id == intent.id
        and row.subject_id == intent.subject_id
        and row.recipient_user_id == intent.recipient_user_id
        and row.actor_user_id == intent.actor_user_id
        and row.integration_connection_id == intent.integration_connection_id
        and row.ai_invocation_id == intent.ai_invocation_id
        and row.category == intent.category
        and row.channel == intent.channel
        and row.dedupe_key == intent.idempotency_key
        and row.external_id == completion._external_id
        and row.sent_at == completion._sent_at
        and row.payload == _journal_payload(completion)
    )


async def finalize_delivery(
    session: AsyncSession,
    completion: DeliveryCompletion,
) -> Notification | None:
    """T3: atomically close the frozen intent and journal a confirmed send."""

    if session.in_nested_transaction():
        raise DeliveryCapabilityError("delivery finalization requires an outer transaction")
    if not _completion_is_valid(completion):
        raise DeliveryCapabilityError("delivery completion is forged or consumed")
    snapshot = _snapshot_from_fingerprint(completion._fingerprint)
    await _lock_historical_delivery_roots(session, intent=snapshot)
    intent = await session.scalar(
        select(NotificationDeliveryIntent)
        .where(NotificationDeliveryIntent.id == snapshot.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is None or _intent_fingerprint(intent) != completion._fingerprint:
        raise DeliveryCapabilityError("delivery completion provenance does not match")
    if intent.lease_token != completion._lease_token:
        raise DeliveryCapabilityError("delivery completion lease token does not match")

    if intent.status != NotificationDeliveryStatus.DISPATCHING.value:
        if intent.status == completion._status.value:
            existing = await session.scalar(
                select(Notification)
                .where(Notification.delivery_intent_id == intent.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if completion._status is NotificationDeliveryStatus.SENT:
                if (
                    intent.error_code is not None
                    or existing is None
                    or not _journal_matches_completion(
                        existing,
                        intent=intent,
                        completion=completion,
                    )
                ):
                    raise DeliveryStateError(
                        "sent delivery has no exact linked journal"
                    )
            elif (
                existing is not None
                or completion._error_code is None
                or intent.error_code != completion._error_code.value
            ):
                object.__setattr__(completion, "_consumed", True)
                object.__setattr__(completion, "_text", None)
                object.__setattr__(completion, "_buttons", None)
                raise DeliveryStateError(
                    "ambiguous delivery terminal state does not match"
                )
            completion._bind_finalization(session)
            return existing
        # A reconciler may have conservatively closed DISPATCHING while this
        # provider result was in flight.  Never turn that into a second send or
        # retain its plaintext completion indefinitely.
        object.__setattr__(completion, "_consumed", True)
        object.__setattr__(completion, "_text", None)
        object.__setattr__(completion, "_buttons", None)
        raise DeliveryStateError("delivery intent is already terminal")

    if intent.dispatch_started_at is None:
        raise DeliveryStateError("dispatching delivery has no start timestamp")
    completed_at = max(
        now_utc().astimezone(timezone.utc),
        _aware_utc(intent.dispatch_started_at),
    )
    intent.status = completion._status.value
    intent.completed_at = completed_at
    intent.error_code = (
        completion._error_code.value
        if completion._error_code is not None
        else None
    )
    journal: Notification | None = None
    if completion._status is NotificationDeliveryStatus.SENT:
        external_id = _valid_telegram_external_id(completion._external_id)
        if external_id is None:
            raise DeliveryCapabilityError("successful delivery id is invalid")
        journal = Notification(
            delivery_intent_id=intent.id,
            subject_id=intent.subject_id,
            actor_user_id=intent.actor_user_id,
            recipient_user_id=intent.recipient_user_id,
            integration_connection_id=intent.integration_connection_id,
            sent_at=completion._sent_at,
            category=intent.category,
            dedupe_key=intent.idempotency_key,
            channel=intent.channel,
            external_id=external_id,
            ai_invocation_id=intent.ai_invocation_id,
            payload=_journal_payload(completion),
        )
        session.add(journal)
    await session.flush()
    completion._bind_finalization(session)
    return journal


async def delivery_claim_exists(
    session: AsyncSession,
    *,
    idempotency_key: str,
    ownership: ProactiveOwnershipContext,
) -> bool:
    """Return whether any durable terminal or in-flight occurrence exists."""

    _validate_ownership(ownership)
    key = _opaque_key(idempotency_key, field_name="idempotency_key")
    rows = list(
        await session.execute(
            _intent_snapshot_select().where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id
                == ownership.recipient_user_id,
                NotificationDeliveryIntent.idempotency_key == key,
            )
        )
    )
    if not rows:
        return False
    if len(rows) != 1:
        raise DeliveryStateError("idempotency key has multiple delivery claims")
    snapshot = _snapshot_from_row(rows[0])
    await _lock_and_validate_intent_snapshot(
        session,
        snapshot=snapshot,
        ownership=ownership,
    )
    return True


async def confirmed_delivery_journal(
    session: AsyncSession,
    *,
    idempotency_key: str,
    category: str,
    ownership: ProactiveOwnershipContext,
    legacy_dedupe_key: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Notification | None:
    """Return exact evidence that an occurrence is durably SENT.

    Unlike :func:`delivery_claim_exists`, PENDING, DISPATCHING, AMBIGUOUS, and
    CANCELLED claims return ``None``. This is the sequencing gate for products
    whose second message must never leapfrog an uncertain first message.
    """

    _validate_ownership(ownership, actor_user_id=actor_user_id)
    key = _opaque_key(idempotency_key, field_name="idempotency_key")
    if category not in {
        CATEGORY_BRIEF,
        CATEGORY_EVENING,
        CATEGORY_NUDGE,
        CATEGORY_REPLY,
        CATEGORY_ECHO,
        CATEGORY_TEST,
    }:
        raise ValueError("delivery category is invalid")
    legacy_key = _validate_legacy_dedupe_key(legacy_dedupe_key)
    rows = list(
        await session.execute(
            _intent_snapshot_select().where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id
                == ownership.recipient_user_id,
                NotificationDeliveryIntent.idempotency_key == key,
            )
        )
    )
    if len(rows) > 1:
        raise DeliveryStateError("idempotency key has multiple delivery claims")
    if rows:
        intent = await _lock_and_validate_intent_snapshot(
            session,
            snapshot=_snapshot_from_row(rows[0]),
            ownership=ownership,
        )
        if (
            intent.actor_user_id != actor_user_id
            or intent.category != category
            or intent.channel != IntegrationProvider.TELEGRAM.value
        ):
            raise DeliveryIdempotencyConflictError(
                "delivery idempotency key metadata conflicts"
            )
        if intent.status != NotificationDeliveryStatus.SENT.value:
            return None
        return await _validate_linked_journal_for_intent(session, intent=intent)

    legacy_journal = await _matching_journal_claim(
        session,
        ownership=ownership,
        actor_user_id=actor_user_id,
        category=category,
        channel=IntegrationProvider.TELEGRAM.value,
        idempotency_key=key,
        legacy_dedupe_key=legacy_key,
        raw_payload_id=None,
        ai_invocation_id=None,
    )
    if (
        legacy_journal is None
        or _valid_telegram_external_id(legacy_journal.external_id) is None
    ):
        return None
    return legacy_journal


async def delivery_claim_for_raw(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    category: str,
    ownership: ProactiveOwnershipContext,
) -> NotificationDeliveryIntent | None:
    """Return the newest exact S/Q raw-backed claim for recovery decisions."""

    _validate_ownership(ownership)
    if (
        isinstance(raw_payload_id, bool)
        or not isinstance(raw_payload_id, int)
        or raw_payload_id < 1
    ):
        raise ValueError("raw_payload_id must be a positive integer")
    if category not in {CATEGORY_REPLY, CATEGORY_ECHO}:
        raise ValueError("raw delivery claims are reply or echo only")
    rows = list(
        await session.execute(
            _intent_snapshot_select()
            .where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id
                == ownership.recipient_user_id,
                NotificationDeliveryIntent.raw_payload_id == raw_payload_id,
                NotificationDeliveryIntent.category == category,
            )
            .order_by(NotificationDeliveryIntent.id)
        )
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise DeliveryStateError("raw payload has multiple delivery claims")
    snapshot = _snapshot_from_row(rows[0])
    return await _lock_and_validate_intent_snapshot(
        session,
        snapshot=snapshot,
        ownership=ownership,
    )


async def delivery_policy_claimed_since(
    session: AsyncSession,
    *,
    policy_key: str,
    not_before: datetime,
    ownership: ProactiveOwnershipContext,
    legacy_dedupe_prefix: str | None = None,
) -> bool:
    """Whether a nudge policy has a certain or uncertain recent initiative claim."""

    _validate_ownership(ownership)
    key = _opaque_key(policy_key, field_name="policy_key")
    if (
        not isinstance(not_before, datetime)
        or not_before.tzinfo is None
        or not_before.utcoffset() is None
    ):
        raise ValueError("not_before must be timezone-aware")
    cutoff = not_before.astimezone(timezone.utc)
    # This check is a reservation gate, not an eventually-consistent report.
    # Hold the canonical subject/current-recipient locks through the query and
    # the caller's immediately-following T1 in the same outer transaction so
    # two distinct hourly occurrence keys cannot both clear one cooldown.
    await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=None,
    )
    rows = list(
        await session.execute(
            _intent_snapshot_select()
            .where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id
                == ownership.recipient_user_id,
                NotificationDeliveryIntent.policy_key == key,
                NotificationDeliveryIntent.status.in_(
                    _INITIATIVE_CLAIM_STATUSES
                ),
                NotificationDeliveryIntent.policy_at >= cutoff,
            )
            .order_by(NotificationDeliveryIntent.policy_at.desc())
        )
    )
    for raw_snapshot in rows:
        await _lock_and_validate_intent_snapshot(
            session,
            snapshot=_snapshot_from_row(raw_snapshot),
            ownership=ownership,
        )
    if rows:
        return True
    if legacy_dedupe_prefix is None:
        return False
    if (
        not isinstance(legacy_dedupe_prefix, str)
        or not legacy_dedupe_prefix
        or len(legacy_dedupe_prefix) > 128
    ):
        raise ValueError("legacy_dedupe_prefix is invalid")
    escaped_legacy_prefix = (
        legacy_dedupe_prefix.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == ownership.subject_id)
        .execution_options(populate_existing=True)
    )
    if subject is None:
        raise DeliveryScopeError("delivery subject does not exist")
    try:
        zone = ZoneInfo(subject.timezone)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise DeliveryPolicyUnavailableError(
            "health subject timezone is invalid"
        ) from exc
    local_cutoff = cutoff.astimezone(zone).replace(tzinfo=None)
    legacy_predicates = (
        Notification.dedupe_key.like(
            f"{escaped_legacy_prefix}%",
            escape="\\",
        ),
        Notification.sent_at >= local_cutoff,
    )
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
        predicates=legacy_predicates,
    )
    journals = list(
        await session.scalars(
            select(Notification)
            .where(
                *legacy_predicates,
                _owned_notification_candidate_scope(ownership),
            )
            .order_by(Notification.sent_at.desc(), Notification.id.desc())
        )
    )
    for row in journals:
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            return True
    return False


async def delivery_policy_clock(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Freeze one instant and its subject-local representation under S/C locks.

    Rule engines use this before calculating cooldown cutoffs so a subject
    timezone change cannot race the subsequent claim check and T1 reservation.
    The first value is aware UTC; the second is aware subject-local time.
    """

    _validate_ownership(ownership)
    authority = await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=None,
    )
    return _policy_clock(timezone_value=authority.timezone, now=now)


def _reconciliation_now(value: datetime | None) -> datetime:
    current = value or now_utc()
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("reconciliation now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _reconciliation_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ValueError("reconciliation limit must be between 1 and 1000")
    return value


def _snapshot_from_row(row) -> _IntentSnapshot:
    values = tuple(row)
    if len(values) != 13:
        raise DeliveryStateError("delivery reconciliation snapshot is invalid")
    mutable = list(values)
    mutable[11] = _aware_utc(mutable[11])
    return _IntentSnapshot(*mutable)


def _intent_snapshot_select():
    return select(
        NotificationDeliveryIntent.id,
        NotificationDeliveryIntent.subject_id,
        NotificationDeliveryIntent.recipient_user_id,
        NotificationDeliveryIntent.actor_user_id,
        NotificationDeliveryIntent.integration_connection_id,
        NotificationDeliveryIntent.raw_payload_id,
        NotificationDeliveryIntent.ai_invocation_id,
        NotificationDeliveryIntent.category,
        NotificationDeliveryIntent.channel,
        NotificationDeliveryIntent.idempotency_key,
        NotificationDeliveryIntent.policy_key,
        NotificationDeliveryIntent.policy_at,
        NotificationDeliveryIntent.policy_date,
    )


async def _validate_linked_journal_for_intent(
    session: AsyncSession,
    *,
    intent: NotificationDeliveryIntent,
) -> Notification | None:
    rows = list(
        await session.scalars(
            select(Notification)
            .where(Notification.delivery_intent_id == intent.id)
            .order_by(Notification.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(rows) > 1:
        raise DeliveryStateError("delivery intent has multiple linked journals")
    row = rows[0] if rows else None
    if intent.status == NotificationDeliveryStatus.SENT.value:
        if row is None:
            raise DeliveryStateError("sent delivery intent has no linked journal")
        if (
            row.subject_id != intent.subject_id
            or row.recipient_user_id != intent.recipient_user_id
            or row.actor_user_id != intent.actor_user_id
            or row.integration_connection_id != intent.integration_connection_id
            or row.ai_invocation_id != intent.ai_invocation_id
            or row.category != intent.category
            or row.channel != intent.channel
            or row.dedupe_key != intent.idempotency_key
            or _valid_telegram_external_id(row.external_id) is None
        ):
            raise DeliveryStateError("linked delivery journal graph is inconsistent")
        return row
    if row is not None:
        raise DeliveryStateError("non-sent delivery intent has a linked journal")
    return None


async def _lock_and_validate_intent_snapshot(
    session: AsyncSession,
    *,
    snapshot: _IntentSnapshot,
    ownership: ProactiveOwnershipContext | None = None,
) -> NotificationDeliveryIntent:
    if ownership is not None and (
        snapshot.subject_id != ownership.subject_id
        or snapshot.recipient_user_id != ownership.recipient_user_id
        or snapshot.actor_user_id
        not in {None, ownership.recipient_user_id}
    ):
        raise DeliveryStateError("delivery claim is outside the authorized scope")
    await _lock_historical_delivery_roots(session, intent=snapshot)
    intent = await session.scalar(
        select(NotificationDeliveryIntent)
        .where(NotificationDeliveryIntent.id == snapshot.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is None or _intent_fingerprint(intent) != _snapshot_fingerprint(snapshot):
        raise DeliveryStateError("delivery claim changed during graph validation")
    try:
        NotificationDeliveryStatus(intent.status)
    except ValueError as exc:
        raise DeliveryStateError("delivery claim has an unknown status") from exc
    await _validate_linked_journal_for_intent(session, intent=intent)
    return intent


async def reconcile_stale_pending_deliveries(
    session: AsyncSession,
    *,
    stale_before: datetime,
    limit: int = RECONCILIATION_BATCH_SIZE,
) -> int:
    """Cancel a bounded page of PENDING rows; never reconstruct payload/send."""

    cutoff = _reconciliation_now(stale_before)
    batch_limit = _reconciliation_limit(limit)
    candidates = list(
        await session.execute(
            _intent_snapshot_select()
            .where(
                NotificationDeliveryIntent.status
                == NotificationDeliveryStatus.PENDING.value,
                NotificationDeliveryIntent.updated_at < cutoff,
                or_(
                    NotificationDeliveryIntent.raw_payload_id.is_(None),
                    NotificationDeliveryIntent.category.notin_(
                        {CATEGORY_REPLY, CATEGORY_ECHO}
                    ),
                ),
            )
            .order_by(
                NotificationDeliveryIntent.subject_id,
                NotificationDeliveryIntent.id,
            )
            .limit(batch_limit)
        )
    )
    changed = 0
    completed_at = now_utc().astimezone(timezone.utc)
    for candidate in candidates:
        snapshot = _snapshot_from_row(candidate)
        await _lock_historical_delivery_roots(session, intent=snapshot)
        intent = await session.scalar(
            select(NotificationDeliveryIntent)
            .where(NotificationDeliveryIntent.id == snapshot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            intent is None
            or _intent_fingerprint(intent) != _snapshot_fingerprint(snapshot)
            or intent.status != NotificationDeliveryStatus.PENDING.value
            or _aware_utc(intent.updated_at) >= cutoff
            or (
                intent.raw_payload_id is not None
                and intent.category in {CATEGORY_REPLY, CATEGORY_ECHO}
            )
        ):
            continue
        _cancel_pending_intent(
            intent,
            completed_at=completed_at,
            error_code=NotificationDeliveryErrorCode.STALE_PENDING,
        )
        changed += 1
    await session.flush()
    return changed


async def reconcile_stale_delivery_dispatches(
    session: AsyncSession,
    *,
    stale_before: datetime,
    limit: int = RECONCILIATION_BATCH_SIZE,
) -> int:
    """Close a bounded page of uncertain provider attempts as AMBIGUOUS."""

    cutoff = _reconciliation_now(stale_before)
    batch_limit = _reconciliation_limit(limit)
    candidates = list(
        await session.execute(
            _intent_snapshot_select()
            .where(
                NotificationDeliveryIntent.status
                == NotificationDeliveryStatus.DISPATCHING.value,
                NotificationDeliveryIntent.dispatch_started_at < cutoff,
            )
            .order_by(
                NotificationDeliveryIntent.subject_id,
                NotificationDeliveryIntent.id,
            )
            .limit(batch_limit)
        )
    )
    changed = 0
    completed_at = now_utc().astimezone(timezone.utc)
    for candidate in candidates:
        snapshot = _snapshot_from_row(candidate)
        await _lock_historical_delivery_roots(session, intent=snapshot)
        intent = await session.scalar(
            select(NotificationDeliveryIntent)
            .where(NotificationDeliveryIntent.id == snapshot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            intent is None
            or _intent_fingerprint(intent) != _snapshot_fingerprint(snapshot)
            or intent.status != NotificationDeliveryStatus.DISPATCHING.value
            or intent.dispatch_started_at is None
            or _aware_utc(intent.dispatch_started_at) >= cutoff
        ):
            continue
        intent.status = NotificationDeliveryStatus.AMBIGUOUS.value
        intent.completed_at = max(
            completed_at,
            _aware_utc(intent.dispatch_started_at),
        )
        intent.error_code = NotificationDeliveryErrorCode.STALE_DISPATCH.value
        changed += 1
    await session.flush()
    return changed


async def delivery_reconciliation_job(session_factory, redis=None) -> None:
    """Shared-scheduler entry point; bounded and deliberately provider-free."""

    del redis
    current = now_utc().astimezone(timezone.utc)
    async with session_factory() as session:
        # Sweeps every subject's stalled deliveries, so it belongs to nobody in
        # particular and row security would otherwise match no row at all.
        await enter_platform_scope(session)
        await reconcile_stale_pending_deliveries(
            session,
            stale_before=current - PENDING_STALE_AFTER,
        )
        await reconcile_stale_delivery_dispatches(
            session,
            stale_before=current - DISPATCHING_STALE_AFTER,
        )
        await session.commit()


async def _require_zero_subject_legacy_delivery(session: AsyncSession) -> object:
    """Authorize one root transaction for the zero-subject compatibility path.

    PostgreSQL's advisory governance lock serializes identity bootstrap. SQLite
    has no advisory locks and ignores ``FOR UPDATE``, so its equivalent is a
    fresh ``BEGIN IMMEDIATE`` held through provider I/O and journaling.  An
    arbitrary pre-open transaction is rejected: otherwise a caller could check
    zero subjects without holding the lock that freezes a concurrent bootstrap.
    """

    try:
        transaction = await authorize_pre_identity_compatibility_transaction(
            session
        )
    except PreIdentityCompatibilityError:
        raise DurableDeliveryRequiredError(
            "zero-subject delivery requires a fresh guarded transaction"
        ) from None

    sync_session = session.sync_session
    if transaction is not sync_session.get_transaction():
        raise DeliveryCapabilityError(
            "legacy delivery lost its guarded root transaction"
        )
    identity_types = (User, HealthSubject, IntegrationConnection)
    pending_identity = (
        tuple(sync_session.new)
        + tuple(sync_session.dirty)
        + tuple(sync_session.deleted)
    )
    if any(isinstance(row, identity_types) for row in pending_identity):
        raise DurableDeliveryRequiredError(
            "zero-subject delivery rejects pending identity changes"
        )
    return transaction


async def _prepare_delivery(
    session: AsyncSession,
    notifier: Optional[Notifier],
    *,
    text: str,
    category: str,
    dedupe_key: Optional[str] = None,
    buttons: Optional[Buttons] = None,
    reply_to: Optional[str] = None,
    now: Optional[datetime] = None,
    ownership: ProactiveOwnershipContext | None = None,
    actor_user_id: uuid.UUID | None = None,
    ai_invocation_id: uuid.UUID | None = None,
    redact_journal_content: bool = False,
    journal_raw_payload_id: int | None = None,
) -> _PreparedDelivery | None:
    """Apply delivery policy without calling the transport or mutating state.

    A scheduler can commit the caller-owned read transaction after this returns,
    call :func:`_transmit_prepared_delivery`, then journal the successful send in
    a new transaction. That is the safe seam for jobs which must never keep a
    database transaction open across a network await.
    """
    if ownership is not None:
        raise DurableDeliveryRequiredError(
            "owned delivery must reserve a durable intent before network I/O"
        )
    if notifier is None or not text.strip():
        return None
    legacy_transaction = await _require_zero_subject_legacy_delivery(session)
    if not isinstance(redact_journal_content, bool):
        raise TypeError("redact_journal_content must be a bool")
    if journal_raw_payload_id is not None and (
        isinstance(journal_raw_payload_id, bool)
        or not isinstance(journal_raw_payload_id, int)
        or journal_raw_payload_id < 1
    ):
        raise ValueError("journal_raw_payload_id must be a positive integer or None")
    if redact_journal_content:
        if (
            ownership is None
            or category != CATEGORY_REPLY
            or journal_raw_payload_id is None
        ):
            raise ProactiveOwnershipScopeError(
                "redacted delivery journals require an owned raw-backed reply"
            )
    elif journal_raw_payload_id is not None:
        raise ProactiveOwnershipScopeError(
            "journal_raw_payload_id requires redacted journal content"
        )
    _validate_ownership(ownership, actor_user_id=actor_user_id)
    if ownership is not None:
        await _require_ownership_scope(
            session,
            ownership,
            channel=notifier.channel,
        )
    invocation_raw_payload_id = await _require_ai_invocation_scope(
        session,
        ownership=ownership,
        category=category,
        ai_invocation_id=ai_invocation_id,
    )
    if redact_journal_content:
        assert ownership is not None and journal_raw_payload_id is not None
        await _require_redacted_reply_raw_scope(
            session,
            ownership=ownership,
            raw_payload_id=journal_raw_payload_id,
            invocation_raw_payload_id=invocation_raw_payload_id,
        )
    if dedupe_key:
        existing = await session.scalar(
            select(Notification).where(Notification.dedupe_key == dedupe_key)
        )
        if existing is not None:
            if ownership is None:
                logger.info(
                    "skipping %s: already sent (code=duplicate)", category
                )
                return None
            valid_existing = await session.scalar(
                select(Notification.id).where(
                    Notification.id == existing.id,
                    notification_ownership_scope(
                        ownership,
                        connection_scoped=False,
                    ),
                )
            )
            if valid_existing is not None:
                logger.info(
                    "skipping %s: already sent (code=duplicate)", category
                )
                return None
            raise NotificationOwnershipConflictError(
                "notification dedupe key belongs to another ownership scope"
            )

    at = now or now_local()
    if category in INITIATIVE_CATEGORIES:
        settings = await prefs.get_prefs(session)
        if category == CATEGORY_NUDGE and in_quiet_hours(
            at.time(),
            start=prefs.as_time(settings["quiet_start"]),
            end=prefs.as_time(settings["quiet_end"]),
        ):
            logger.info("skipping %s: quiet hours (%s)", category, at.time())
            return None
        if await sent_today(
            session,
            on_date=at.date(),
            ownership=ownership,
        ) >= settings["daily_budget"]:
            logger.info(
                "skipping %s: daily budget of %s used",
                category,
                settings["daily_budget"],
            )
            return None

    return _PreparedDelivery(
        text=text,
        category=category,
        dedupe_key=dedupe_key,
        buttons=tuple(buttons) if buttons else None,
        reply_to=reply_to,
        sent_at=at,
        channel=notifier.channel,
        ownership=ownership,
        actor_user_id=actor_user_id,
        ai_invocation_id=ai_invocation_id,
        redact_journal_content=redact_journal_content,
        journal_raw_payload_id=journal_raw_payload_id,
        _session=session,
        _transaction=legacy_transaction,
    )


async def _transmit_prepared_delivery(
    notifier: Notifier,
    prepared: _PreparedDelivery,
) -> _DeliveredMessage | None:
    """Send a prepared message without accepting or touching a DB session."""
    if not isinstance(prepared, _PreparedDelivery):
        raise TypeError("prepared must be a _PreparedDelivery")
    if (
        not prepared._session.in_transaction()
        or prepared._session.sync_session.get_transaction()
        is not prepared._transaction
    ):
        raise DurableDeliveryRequiredError(
            "zero-subject compatibility proof expired before network I/O"
        )
    if notifier.channel != prepared.channel:
        raise ProactiveOwnershipScopeError(
            "prepared delivery channel does not match the notifier"
        )
    try:
        return _DeliveredMessage(
            external_id=await notifier.send(
                prepared.text,
                buttons=prepared.buttons,
                reply_to=prepared.reply_to,
            )
        )
    except (asyncio.CancelledError, Exception):
        # Exception strings/tracebacks can contain Telegram's token-bearing URL
        # or outbound PHI.  Even the temporary zero-subject bridge logs only an
        # allowlisted bounded code.
        logger.warning(
            "delivery failed for %s; message dropped (code=transport_error)",
            prepared.category,
        )
        return None


async def _journal_prepared_delivery(
    session: AsyncSession,
    prepared: _PreparedDelivery,
    *,
    external_id: str | None,
) -> Notification:
    """Persist a successful prepared send; flush only, caller commits."""
    if not isinstance(prepared, _PreparedDelivery):
        raise TypeError("prepared must be a _PreparedDelivery")
    if prepared.ownership is not None:
        raise DurableDeliveryRequiredError(
            "owned delivery journals must link a durable intent"
        )
    if (
        session is not prepared._session
        or session.sync_session.get_transaction() is not prepared._transaction
    ):
        raise DurableDeliveryRequiredError(
            "zero-subject journal must share the network transaction"
        )
    _validate_ownership(
        prepared.ownership,
        actor_user_id=prepared.actor_user_id,
    )
    if prepared.ownership is not None:
        await _require_ownership_scope(
            session,
            prepared.ownership,
            channel=prepared.channel,
        )
    invocation_raw_payload_id = await _require_ai_invocation_scope(
        session,
        ownership=prepared.ownership,
        category=prepared.category,
        ai_invocation_id=prepared.ai_invocation_id,
    )
    if prepared.redact_journal_content:
        assert prepared.ownership is not None
        assert prepared.journal_raw_payload_id is not None
        await _require_redacted_reply_raw_scope(
            session,
            ownership=prepared.ownership,
            raw_payload_id=prepared.journal_raw_payload_id,
            invocation_raw_payload_id=invocation_raw_payload_id,
        )
    payload = (
        {
            "content_redacted": True,
            "raw_payload_id": prepared.journal_raw_payload_id,
        }
        if prepared.redact_journal_content
        else {
            "text": prepared.text,
            "buttons": (
                [list(button) for button in prepared.buttons]
                if prepared.buttons
                else None
            ),
        }
    )
    row = Notification(
        subject_id=(
            prepared.ownership.subject_id
            if prepared.ownership is not None
            else None
        ),
        actor_user_id=prepared.actor_user_id,
        recipient_user_id=(
            prepared.ownership.recipient_user_id
            if prepared.ownership is not None
            else None
        ),
        integration_connection_id=(
            prepared.ownership.connection_id
            if prepared.ownership is not None
            else None
        ),
        sent_at=prepared.sent_at,
        category=prepared.category,
        dedupe_key=prepared.dedupe_key,
        channel=prepared.channel,
        external_id=external_id or None,
        ai_invocation_id=prepared.ai_invocation_id,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    return row


async def send(
    session: AsyncSession,
    notifier: Optional[Notifier],
    *,
    text: str,
    category: str,
    dedupe_key: Optional[str] = None,
    buttons: Optional[Buttons] = None,
    reply_to: Optional[str] = None,
    now: Optional[datetime] = None,
    ownership: ProactiveOwnershipContext | None = None,
    actor_user_id: uuid.UUID | None = None,
    ai_invocation_id: uuid.UUID | None = None,
) -> Optional[Notification]:
    """Send if allowed, and journal what was sent. ``None`` = nothing went out."""
    if ownership is not None:
        raise DurableDeliveryRequiredError(
            "owned delivery must use the durable three-phase API"
        )
    prepared = await _prepare_delivery(
        session,
        notifier,
        text=text,
        category=category,
        dedupe_key=dedupe_key,
        buttons=buttons,
        reply_to=reply_to,
        now=now,
        ownership=ownership,
        actor_user_id=actor_user_id,
        ai_invocation_id=ai_invocation_id,
    )
    if prepared is None:
        return None
    assert notifier is not None
    delivered = await _transmit_prepared_delivery(notifier, prepared)
    if delivered is None:
        return None
    return await _journal_prepared_delivery(
        session,
        prepared,
        external_id=delivered.external_id,
    )
