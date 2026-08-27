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

import hashlib
import logging
import re
import uuid
import weakref
from dataclasses import dataclass
from datetime import (
    date as date_type,
    datetime,
    time as time_type,
    timedelta,
    timezone,
)
from time import monotonic
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationProvider,
    NotificationDeliveryErrorCode,
    NotificationDeliveryStatus,
)
from vitals.models.proactive import NotificationDeliveryIntent
from vitals.services.proactive.preferences import codec as preference_codec
from vitals.services.proactive.preferences import contracts as preference_contracts
from vitals.services.proactive.channels import (
    BoundNotifier,
    DeliveryEndpointBinding,
)
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.persistence.transactions import (
    TransactionOutcomeError,
    register_root_transaction_outcome,
)
from vitals.utils.timeutils import now_utc

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
DAILY_BUDGET = preference_contracts.DEFAULTS["daily_budget"]
QUIET_START = preference_codec.as_time(preference_contracts.DEFAULTS["quiet_start"])
QUIET_END = preference_codec.as_time(preference_contracts.DEFAULTS["quiet_end"])

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
    payload = "\x1f".join(f"{len(item.encode('utf-8'))}:{item}" for item in canonical).encode(
        "utf-8"
    )
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
            raise DeliveryPolicyUnavailableError("delivery daily budget must be a positive integer")
        if not isinstance(self.quiet_start, time_type) or not isinstance(self.quiet_end, time_type):
            raise DeliveryPolicyUnavailableError("delivery quiet hours must be time values")


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
        raise DeliveryCapabilityError("delivery preparation scopes are service-issued only")

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


def _valid_external_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if not value.isdigit() or len(value) > 64 or int(value) <= 0:
        return None
    return value
