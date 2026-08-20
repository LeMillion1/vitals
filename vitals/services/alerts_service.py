"""system_alerts lifecycle: raise / resolve / override / list_active.

Raising is **idempotent** while an alert stays active: the partial-unique index
``uq_active_alert_per_key_entity`` guarantees one unresolved row per
``(alert_key, entity_ref)``, and :func:`raise_alert` first looks for that active
row and updates it instead of inserting a duplicate.

These functions ``flush`` (so a freshly inserted row gets its id) but do **not**
``commit`` — the caller owns the transaction boundary. In the web layer the
``get_session`` dependency commits on success; tests/scheduler commit explicitly.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as date_type
from enum import StrEnum
from types import MappingProxyType
from typing import Optional, Sequence, TypeAlias

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Severity,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.conflict_rule import ConflictRule
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.identity_service import (
    UnsupportedIdentityDatabaseError,
    acquire_identity_governance_lock,
)
from vitals.utils.timeutils import now_local, today_local


async def _find_active(
    session: AsyncSession, alert_key: str, entity_ref: str
) -> Optional[SystemAlert]:
    result = await session.execute(
        select(SystemAlert).where(
            SystemAlert.alert_key == alert_key,
            SystemAlert.entity_ref == entity_ref,
            SystemAlert.resolved_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _was_dismissed_today(
    session: AsyncSession, alert_key: str, entity_ref: str, on_date: Optional[date_type] = None
) -> bool:
    """Return True if this alert was already dismissed (resolved) today.

    For status alerts recomputed from fast-moving data (a new weigh-in lands
    most days), binding entity_ref to "the latest triggering row" would barely
    change anything, and binding it to something coarser (e.g. the active
    noise period) could suppress a still-relevant status for weeks. So these
    keep the daily-nag contract: dismissing hides the alert for the rest of
    today; it becomes raiseable again the next calendar day. Used by the
    weight noise-period alert and the GLP-1 plateau alert — contrast with
    :func:`_was_ever_dismissed`, used where the alert is bound to a specific,
    infrequently-arriving row (lab results, body scans).
    """
    today = on_date or today_local()
    result = await session.execute(
        select(func.count()).where(
            SystemAlert.alert_key == alert_key,
            SystemAlert.entity_ref == entity_ref,
            SystemAlert.resolved_at.is_not(None),
            func.date(SystemAlert.resolved_at) == today,
        )
    )
    return (result.scalar() or 0) > 0


async def _was_ever_dismissed(
    session: AsyncSession, alert_key: str, entity_ref: str
) -> bool:
    """Return True if this exact (alert_key, entity_ref) was ever dismissed.

    Callers bind ``entity_ref`` to the specific row that triggered the alert
    (e.g. ``f"{marker}:{lab_result_id}"``), so once dismissed it never comes
    back for that row — only a new triggering row (new entity_ref) can raise
    it again. See :func:`resolve_superseded` for cleaning up alerts tied to a
    row that's no longer the current one.
    """
    result = await session.execute(
        select(func.count()).where(
            SystemAlert.alert_key == alert_key,
            SystemAlert.entity_ref == entity_ref,
            SystemAlert.resolved_at.is_not(None),
        )
    )
    return (result.scalar() or 0) > 0


async def resolve_superseded(
    session: AsyncSession,
    *,
    alert_key: str,
    keep_entity: Optional[str],
    marker: Optional[str] = None,
) -> None:
    """Resolve active ``alert_key`` rows that no longer correspond to the
    current triggering row, so they don't linger as orphaned duplicates once
    ``entity_ref`` starts varying per row instead of staying fixed per marker.

    If ``marker`` is given, only rows for that marker are touched — either the
    bare legacy ``entity_ref == marker`` form or the ``f"{marker}:"``-prefixed
    form — since multiple markers share one ``alert_key``. If ``marker`` is
    ``None``, every active row for ``alert_key`` other than ``keep_entity`` is
    resolved (the singleton case, e.g. body-scan alerts, where only one entity
    is ever current). ``keep_entity=None`` resolves everything for the key.
    """
    result = await session.execute(
        select(SystemAlert).where(
            SystemAlert.alert_key == alert_key,
            SystemAlert.resolved_at.is_(None),
        )
    )
    now = now_local()
    changed = False
    for row in result.scalars().all():
        if row.entity_ref == keep_entity:
            continue
        if marker is not None and not (
            row.entity_ref == marker or row.entity_ref.startswith(f"{marker}:")
        ):
            continue
        row.resolved_at = now
        changed = True
    if changed:
        await session.flush()


async def raise_alert(
    session: AsyncSession,
    *,
    domain: str,
    severity: str,
    message: str,
    alert_key: str,
    entity_ref: str = "",
    overridden: bool = False,
) -> SystemAlert:
    """Raise (or refresh) an active alert.

    If an unresolved alert with the same ``(alert_key, entity_ref)`` already
    exists, its ``severity``/``message`` are refreshed and it is returned — so
    re-raising the same condition never piles up duplicate rows. ``overridden``
    stamps ``override_at`` immediately (used by the conflict-engine override flow
    when a ``block`` is saved anyway).
    """
    existing = await _find_active(session, alert_key, entity_ref)
    if existing is not None:
        existing.severity = severity
        existing.message = message
        if overridden and existing.override_at is None:
            existing.override_at = now_local()
        await session.flush()
        return existing

    alert = SystemAlert(
        domain=domain,
        severity=severity,
        message=message,
        alert_key=alert_key,
        entity_ref=entity_ref,
        override_at=now_local() if overridden else None,
    )
    session.add(alert)
    await session.flush()
    return alert


async def resolve_alert(session: AsyncSession, alert_id: int) -> Optional[SystemAlert]:
    """Mark exactly the one alert identified by ``alert_id`` resolved. Returns the
    target row, or None if it doesn't exist.

    Alert identity is ``(alert_key, entity_ref)`` — two rows that merely share
    message text (e.g. the same templated message for two different lab markers,
    or two conflict rules with identical wording) are distinct alerts and must
    NOT be collapsed. Stale per-row duplicates from a re-imported source are
    cleaned up structurally by :func:`resolve_superseded`, never by fuzzy text
    matching (which previously could resolve an unrelated alert — even in another
    domain — that happened to read the same)."""
    alert = await session.get(SystemAlert, alert_id)
    if alert is None:
        return None
    if alert.resolved_at is None:
        alert.resolved_at = now_local()
        await session.flush()
    return alert


async def resolve_by_key(
    session: AsyncSession, *, alert_key: str, entity_ref: str = ""
) -> Optional[SystemAlert]:
    """Resolve the active alert for a ``(key, entity)`` — used when the condition
    that raised it clears (e.g. a noisy-weight period ends). No-op if none active."""
    existing = await _find_active(session, alert_key, entity_ref)
    if existing is None:
        return None
    existing.resolved_at = now_local()
    await session.flush()
    return existing


async def override_alert(session: AsyncSession, alert_id: int) -> Optional[SystemAlert]:
    """Stamp ``override_at`` on an existing alert (the user chose 'Save anyway')."""
    alert = await session.get(SystemAlert, alert_id)
    if alert is None:
        return None
    if alert.override_at is None:
        alert.override_at = now_local()
        await session.flush()
    return alert


async def resolve_all(session: AsyncSession, *, domain: Optional[str] = None) -> None:
    """Resolve all active alerts, optionally filtered by domain."""
    stmt = select(SystemAlert).where(SystemAlert.resolved_at.is_(None))
    if domain is not None:
        stmt = stmt.where(SystemAlert.domain == domain)
    result = await session.execute(stmt)
    active = result.scalars().all()
    now = now_local()
    for alert in active:
        alert.resolved_at = now
    await session.flush()


async def list_active(
    session: AsyncSession, *, domain: Optional[str] = None
) -> Sequence[SystemAlert]:
    """Active (unresolved) alerts, newest first, optionally filtered by domain.

    The ``uq_active_alert_per_key_entity`` partial-unique index already guarantees
    one active row per ``(alert_key, entity_ref)``, so there are no true duplicates
    to filter — every active row is a distinct alert and is returned as-is. (The
    old normalized-message dedup hid legitimately different alerts that shared
    templated wording and made the result nondeterministic.)"""
    stmt = select(SystemAlert).where(SystemAlert.resolved_at.is_(None))
    if domain is not None:
        stmt = stmt.where(SystemAlert.domain == domain)
    stmt = stmt.order_by(SystemAlert.created_at.desc(), SystemAlert.id.desc())
    result = await session.execute(stmt)
    return result.scalars().all()



def is_blocking(severity: str) -> bool:
    """True when a severity should stop a save unless overridden."""
    return severity == Severity.BLOCK.value


# ── Stage-2 subject-aware API ─────────────────────────────────────────────────
#
# The legacy functions above intentionally remain unchanged while callers move
# one bounded domain at a time.  New code must use the explicit contexts below;
# a bare alert key or primary key is not an ownership authority.


class AlertServiceError(Exception):
    """Base class for fail-closed subject-aware alert failures."""


class AlertValidationError(AlertServiceError):
    """A scoped alert input does not satisfy the strict typed contract."""


class AlertContextError(AlertServiceError):
    """The supplied subject, actor, connection, or platform context is invalid."""


class AlertUnsupportedDatabaseError(AlertContextError):
    """Scoped alert locking was requested on an unsupported database dialect."""


class AlertSubjectNotFoundError(AlertContextError):
    """The selected health subject does not exist."""


class AlertActorNotFoundError(AlertContextError):
    """The attributed human identity does not exist."""


class AlertActorInactiveError(AlertContextError):
    """The attributed human identity is not active."""


class AlertConnectionNotFoundError(AlertContextError):
    """The selected integration connection does not exist."""


class AlertConnectionOwnershipError(AlertContextError):
    """The selected integration connection belongs to another subject."""


class AlertConnectionProviderError(AlertContextError):
    """The selected integration connection belongs to another provider."""


class AlertConnectionTypeError(AlertContextError):
    """The selected connection has the wrong purpose for provider alerts."""


class AlertConnectionStateError(AlertContextError):
    """The selected integration connection cannot be used for this operation."""


class AlertScopeConflictError(AlertServiceError):
    """Persisted alert ownership conflicts with the requested exact scope."""


class AlertScopedUniqueCutoverRequiredError(AlertScopeConflictError):
    """The retained global active-alert key is occupied by another scope."""


class AlertAmbiguousMatchError(AlertScopeConflictError):
    """More than one persisted row could satisfy a scoped alert operation."""


class AlertLegacyBridgeError(AlertScopeConflictError):
    """The registration-disabled fully-unowned bridge cannot be proved safe."""


class AlertPlatformNamespaceError(AlertContextError):
    """A platform or provider key was used through the wrong typed context."""


class AlertLifecycleError(AlertServiceError):
    """A requested alert lifecycle transition is not permitted."""


class AlertActorRequiredError(AlertLifecycleError):
    """A human-only alert lifecycle action has no attributed actor."""


class LegacyAlertBridge(StrEnum):
    """Whether one pre-ownership ``S=NULL, C=NULL`` row may be bridged."""

    REJECT = "reject"
    FULLY_UNOWNED = "fully_unowned"


class PlatformAlertNamespace(StrEnum):
    """Allowlisted key namespaces whose durable owner is the platform."""

    SCHEDULER_JOB_FAILURE = "scheduler.job_failed:"


@dataclass(frozen=True, slots=True)
class HealthAlertContext:
    """A health-subject alert whose exact connection scope is NULL."""

    identity: WriteIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.identity, WriteIdentity):
            raise AlertContextError("identity must be a WriteIdentity")


@dataclass(frozen=True, slots=True)
class ProviderAlertContext:
    """A provider alert owned by one exact subject and connection root."""

    identity: WriteIdentity
    provider: IntegrationProvider
    integration_connection_id: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.identity, WriteIdentity):
            raise AlertContextError("identity must be a WriteIdentity")
        if not isinstance(self.provider, IntegrationProvider):
            raise AlertContextError("provider must be an IntegrationProvider")
        if not isinstance(self.integration_connection_id, uuid.UUID):
            raise AlertContextError("integration_connection_id must be a UUID")


@dataclass(frozen=True, slots=True)
class PlatformAlertContext:
    """A non-PHI platform alert in one allowlisted key namespace."""

    namespace: PlatformAlertNamespace
    actor_user_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, PlatformAlertNamespace):
            raise AlertContextError("namespace must be a PlatformAlertNamespace")
        if self.actor_user_id is not None and not isinstance(
            self.actor_user_id, uuid.UUID
        ):
            raise AlertContextError("actor_user_id must be a UUID or None")


AlertContext: TypeAlias = (
    HealthAlertContext | ProviderAlertContext | PlatformAlertContext
)


# Alert-key ownership is an exhaustive allowlist.  A broad ``garmin.*`` prefix is
# not proof that a historical NULL/NULL row belongs to a Garmin account, and an
# unknown key must block the bridge until it is explicitly classified.
HEALTH_ALERT_KEYS = frozenset(
    {
        "weight.noisy_period_active",
        "glp1.plateau",
        "labs.out_of_range",
        "labs.retest_due",
        "body_comp.visceral_high",
        "body_comp.phase_low",
        "hrt.labs_due",
        "hrt.injection_due",
        "brief_empty_day",
        # New platform-funded parser alerts are health-subject scoped (C=NULL).
        # The same key intentionally remains in the historical OpenRouter
        # provider registry below so old subject-C alerts can be resolved.
        "signal_parser_failed",
        "scheduler.job_failed:glp1_plateau",
        "scheduler.job_failed:hrt_reminders",
        "scheduler.job_failed:nutrition_day_end",
        "scheduler.job_failed:daily_brief",
        "scheduler.job_failed:evening_block",
        "scheduler.job_failed:nudges",
        "scheduler.job_failed:weekly_digest",
    }
)
PROVIDER_ALERT_KEYS: Mapping[
    IntegrationProvider, frozenset[str]
] = MappingProxyType(
    {
        IntegrationProvider.GARMIN: frozenset(
            {
                "garmin.auth",
                "garmin.token_cache",
                "garmin.weight_export",
                "scheduler.job_failed:garmin_sync",
                "scheduler.job_failed:garmin_weight_export",
                "scheduler.job_failed:garmin_pulse",
            }
        ),
        IntegrationProvider.HEVY: frozenset(
            {
                "hevy.sync_failed",
                "scheduler.job_failed:hevy_sync",
            }
        ),
        IntegrationProvider.OPENROUTER: frozenset({"signal_parser_failed"}),
        IntegrationProvider.TELEGRAM: frozenset(),
    }
)
PLATFORM_ALERT_KEYS: Mapping[
    PlatformAlertNamespace, frozenset[str]
] = MappingProxyType(
    {
        PlatformAlertNamespace.SCHEDULER_JOB_FAILURE: frozenset(
            {
                "scheduler.job_failed:raw_payload_sweep",
                "scheduler.job_failed:share_purge",
                "scheduler.job_failed:ai_invocation_reconcile",
            }
        )
    }
)
PROVIDER_ALERT_CONNECTION_TYPES: Mapping[
    IntegrationProvider, IntegrationConnectionType
] = MappingProxyType(
    {
        IntegrationProvider.GARMIN: IntegrationConnectionType.ACCOUNT,
        IntegrationProvider.HEVY: IntegrationConnectionType.ACCOUNT,
        IntegrationProvider.OPENROUTER: IntegrationConnectionType.AI_GATEWAY,
        IntegrationProvider.TELEGRAM: IntegrationConnectionType.RECIPIENT,
    }
)
ALERT_KEY_DOMAINS: Mapping[str, Domain] = MappingProxyType(
    {
        "weight.noisy_period_active": Domain.WEIGHT,
        "glp1.plateau": Domain.GLP1,
        "labs.out_of_range": Domain.LABS,
        "labs.retest_due": Domain.LABS,
        "body_comp.visceral_high": Domain.BODY_COMPOSITION,
        "body_comp.phase_low": Domain.BODY_COMPOSITION,
        "hrt.labs_due": Domain.HRT,
        "hrt.injection_due": Domain.HRT,
        "brief_empty_day": Domain.SYSTEM,
        "garmin.auth": Domain.GARMIN,
        "garmin.token_cache": Domain.GARMIN,
        "garmin.weight_export": Domain.GARMIN,
        "hevy.sync_failed": Domain.WORKOUTS,
        "signal_parser_failed": Domain.SIGNALS,
        **{
            key: Domain.SYSTEM
            for key in set().union(
                *(keys for keys in (HEALTH_ALERT_KEYS, *PROVIDER_ALERT_KEYS.values(), *PLATFORM_ALERT_KEYS.values()))
            )
            if key.startswith("scheduler.job_failed:")
        },
    }
)

ALERT_KEY_LOCK_NAMESPACE = 0x414C5254  # ASCII "ALRT", signed int32-safe.
_KNOWN_CONNECTION_STATUSES = frozenset(
    status.value for status in IntegrationConnectionStatus
)
_FRESH_PROVIDER_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }
)
_HISTORICAL_PROVIDER_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }
)
_MAX_ALERT_KEY_LENGTH = 128
_MAX_ENTITY_REF_LENGTH = 128


def _require_context(context: AlertContext) -> None:
    if not isinstance(
        context,
        (HealthAlertContext, ProviderAlertContext, PlatformAlertContext),
    ):
        raise AlertContextError("context must be a typed alert context")


def _require_bridge(value: LegacyAlertBridge) -> None:
    if not isinstance(value, LegacyAlertBridge):
        raise AlertValidationError("legacy_bridge must be a LegacyAlertBridge")


def _has_forbidden_control(value: str) -> bool:
    return any(
        unicodedata.category(char).startswith("C")
        for char in value
        if char not in {"\n", "\r", "\t"}
    )


def _require_key(value: str) -> None:
    if not isinstance(value, str):
        raise AlertValidationError("alert_key must be a string")
    if not value or value != value.strip():
        raise AlertValidationError("alert_key must be non-blank without outer whitespace")
    if len(value) > _MAX_ALERT_KEY_LENGTH:
        raise AlertValidationError("alert_key is too long")
    if _has_forbidden_control(value):
        raise AlertValidationError("alert_key must not contain control characters")


def _require_entity_ref(value: str) -> None:
    if not isinstance(value, str):
        raise AlertValidationError("entity_ref must be a string")
    if value and value != value.strip():
        raise AlertValidationError(
            "entity_ref must not contain outer whitespace when non-empty"
        )
    if len(value) > _MAX_ENTITY_REF_LENGTH:
        raise AlertValidationError("entity_ref is too long")
    if _has_forbidden_control(value):
        raise AlertValidationError("entity_ref must not contain control characters")


def _require_message(value: str) -> None:
    if not isinstance(value, str):
        raise AlertValidationError("message must be a string")
    if not value.strip():
        raise AlertValidationError("message must not be blank")
    if _has_forbidden_control(value):
        raise AlertValidationError("message must not contain control characters")


def _require_domain(value: Domain | None, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, Domain):
        expected = "a Domain or None" if optional else "a Domain"
        raise AlertValidationError(f"domain must be {expected}")


def _require_severity(value: Severity) -> None:
    if not isinstance(value, Severity):
        raise AlertValidationError("severity must be a Severity")


def _require_alert_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AlertValidationError("alert_id must be a positive integer")


def _require_optional_entity(value: str | None, field_name: str) -> None:
    if value is None:
        return
    try:
        _require_entity_ref(value)
    except AlertValidationError as exc:
        raise AlertValidationError(f"invalid {field_name}: {exc}") from exc


def _actor_user_id(context: AlertContext) -> uuid.UUID | None:
    if isinstance(context, PlatformAlertContext):
        return context.actor_user_id
    return context.identity.actor_user_id


def _provider_key_matches(provider: IntegrationProvider, alert_key: str) -> bool:
    return alert_key in PROVIDER_ALERT_KEYS[provider]


def _is_platform_key(alert_key: str) -> bool:
    return any(alert_key in keys for keys in PLATFORM_ALERT_KEYS.values())


def is_platform_alert_key(alert_key: str) -> bool:
    """Return whether ``alert_key`` belongs to a platform-only namespace.

    This small public classifier is the transitional composition guard while
    Today/digest are still on the singleton reader.  Platform diagnostics may
    contain operational exception details and must never enter a health report
    or an external-model prompt.  Full subject/provider aggregation replaces
    this guard at the composition cutover.
    """

    if not isinstance(alert_key, str):
        return False
    return _is_platform_key(alert_key)


def _is_provider_key(alert_key: str) -> bool:
    return any(
        _provider_key_matches(provider, alert_key)
        for provider in IntegrationProvider
    )


def _is_health_key(alert_key: str) -> bool:
    if alert_key in HEALTH_ALERT_KEYS:
        return True
    return re.fullmatch(r"conflict:[1-9][0-9]*", alert_key) is not None


def _is_classified_key(alert_key: str) -> bool:
    return (
        _is_health_key(alert_key)
        or _is_provider_key(alert_key)
        or _is_platform_key(alert_key)
    )


def _validate_context_key(context: AlertContext, alert_key: str) -> None:
    if isinstance(context, PlatformAlertContext):
        if alert_key not in PLATFORM_ALERT_KEYS[context.namespace]:
            raise AlertPlatformNamespaceError(
                "alert_key does not belong to the selected platform namespace"
            )
        return
    if isinstance(context, ProviderAlertContext):
        if not _provider_key_matches(context.provider, alert_key):
            raise AlertPlatformNamespaceError(
                "alert_key does not belong to the selected provider namespace"
            )
        return
    if not _is_health_key(alert_key):
        raise AlertPlatformNamespaceError(
            "alert_key is not registered as a health-subject alert"
        )


async def _allowed_domains_for_key(
    session: AsyncSession,
    context: AlertContext,
    alert_key: str,
) -> frozenset[str]:
    _validate_context_key(context, alert_key)
    subject_id = (
        None
        if isinstance(context, PlatformAlertContext)
        else context.identity.subject_id
    )
    return await _registered_domains_for_key(session, alert_key, subject_id)


async def _registered_domains_for_key(
    session: AsyncSession,
    alert_key: str,
    subject_id: uuid.UUID | None,
) -> frozenset[str]:
    if not alert_key.startswith("conflict:"):
        try:
            return frozenset({ALERT_KEY_DOMAINS[alert_key].value})
        except KeyError as exc:
            raise AlertScopeConflictError(
                "registered alert_key has no domain contract"
            ) from exc

    rule_id = int(alert_key.removeprefix("conflict:"))
    rule = (
        await session.execute(
            select(
                ConflictRule.subject_id,
                ConflictRule.domain_a,
                ConflictRule.domain_b,
            ).where(ConflictRule.id == rule_id)
        )
    ).one_or_none()
    if rule is None:
        raise AlertScopeConflictError("conflict alert references a missing rule")
    if subject_id is None or rule.subject_id not in {None, subject_id}:
        raise AlertScopeConflictError(
            "conflict alert references another subject's rule"
        )
    try:
        domains = frozenset(
            {Domain(rule.domain_a).value, Domain(rule.domain_b).value}
        )
    except ValueError as exc:
        raise AlertScopeConflictError(
            "conflict rule contains an unknown domain"
        ) from exc
    return domains


def _validate_platform_domain(
    context: AlertContext,
    domain: Domain | None,
) -> None:
    if (
        isinstance(context, PlatformAlertContext)
        and domain is not None
        and domain is not Domain.SYSTEM
    ):
        raise AlertPlatformNamespaceError(
            "platform alert namespaces require Domain.SYSTEM"
        )


async def _validate_active_actor(
    session: AsyncSession,
    actor_user_id: uuid.UUID | None,
) -> None:
    if actor_user_id is None:
        return
    actor_status = await session.scalar(
        select(User.status).where(User.id == actor_user_id)
    )
    if actor_status is None:
        raise AlertActorNotFoundError("actor user does not exist")
    if actor_status != UserStatus.ACTIVE.value:
        raise AlertActorInactiveError("actor user is not active")


async def _require_single_subject_bridge(
    session: AsyncSession,
    subject: HealthSubject,
) -> None:
    with session.no_autoflush:
        subject_ids = list(
            await session.scalars(
                select(HealthSubject.id)
                .order_by(HealthSubject.id)
                .limit(2)
            )
        )
    if subject_ids != [subject.id]:
        raise AlertLegacyBridgeError(
            "fully-unowned alerts require exactly one matching health subject"
        )
    owner_status = await session.scalar(
        select(User.status).where(User.id == subject.owner_user_id)
    )
    if owner_status != UserStatus.ACTIVE.value:
        raise AlertLegacyBridgeError(
            "fully-unowned alerts require an active sole-subject owner"
        )


async def _reject_unknown_fully_unowned_keys(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> None:
    rows = list(
        await session.execute(
            select(SystemAlert.alert_key, SystemAlert.domain)
            .where(
                SystemAlert.subject_id.is_(None),
                SystemAlert.integration_connection_id.is_(None),
            )
            .distinct()
        )
    )
    for alert_key, domain in rows:
        if not _is_classified_key(alert_key):
            raise AlertLegacyBridgeError(
                "an unclassified fully-unowned alert blocks legacy bridging"
            )
        try:
            allowed_domains = await _registered_domains_for_key(
                session,
                alert_key,
                subject_id,
            )
        except AlertScopeConflictError as exc:
            raise AlertLegacyBridgeError(str(exc)) from exc
        if domain not in allowed_domains:
            raise AlertLegacyBridgeError(
                "a fully-unowned alert has a domain inconsistent with its key"
            )


async def _prepare_context(
    session: AsyncSession,
    *,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
    fresh_provider_write: bool,
    lock_roots: bool,
) -> None:
    _require_context(context)
    _require_bridge(legacy_bridge)
    if isinstance(context, PlatformAlertContext):
        if legacy_bridge is not LegacyAlertBridge.REJECT:
            raise AlertLegacyBridgeError(
                "platform alert contexts cannot use the legacy ownership bridge"
            )
        await _validate_active_actor(session, context.actor_user_id)
        return

    if legacy_bridge is LegacyAlertBridge.FULLY_UNOWNED:
        try:
            await acquire_identity_governance_lock(session)
        except UnsupportedIdentityDatabaseError as exc:
            raise AlertUnsupportedDatabaseError(str(exc)) from exc

    subject_stmt = (
        select(HealthSubject)
        .where(HealthSubject.id == context.identity.subject_id)
        .execution_options(populate_existing=True)
    )
    if lock_roots:
        subject_stmt = subject_stmt.with_for_update()
    subject = await session.scalar(subject_stmt)
    if subject is None:
        raise AlertSubjectNotFoundError("health subject does not exist")

    if legacy_bridge is LegacyAlertBridge.FULLY_UNOWNED:
        await _require_single_subject_bridge(session, subject)
        await _reject_unknown_fully_unowned_keys(
            session,
            context.identity.subject_id,
        )

    await _validate_active_actor(session, context.identity.actor_user_id)

    if not isinstance(context, ProviderAlertContext):
        return
    connection_stmt = (
        select(IntegrationConnection)
        .where(IntegrationConnection.id == context.integration_connection_id)
        .execution_options(populate_existing=True)
    )
    if lock_roots:
        connection_stmt = connection_stmt.with_for_update()
    connection = await session.scalar(connection_stmt)
    if connection is None:
        raise AlertConnectionNotFoundError("integration connection does not exist")
    if connection.subject_id != context.identity.subject_id:
        raise AlertConnectionOwnershipError(
            "integration connection belongs to another subject"
        )
    if connection.provider != context.provider.value:
        raise AlertConnectionProviderError(
            "integration connection belongs to another provider"
        )
    expected_type = PROVIDER_ALERT_CONNECTION_TYPES[context.provider]
    if connection.connection_type != expected_type.value:
        raise AlertConnectionTypeError(
            "integration connection has the wrong provider-alert type"
        )
    if connection.status not in _KNOWN_CONNECTION_STATUSES:
        raise AlertConnectionStateError(
            "integration connection has an unknown lifecycle state"
        )
    allowed_statuses = (
        _FRESH_PROVIDER_STATUSES
        if fresh_provider_write
        else _HISTORICAL_PROVIDER_STATUSES
    )
    if connection.status not in allowed_statuses:
        raise AlertConnectionStateError(
            "integration connection cannot authorize this alert operation"
        )


def _alert_lock_key(alert_key: str) -> int:
    digest = hashlib.sha256(alert_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], byteorder="big", signed=True)


async def _acquire_alert_key_lock(
    session: AsyncSession,
    alert_key: str,
) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return
    if dialect != "postgresql":
        raise AlertUnsupportedDatabaseError(
            f"scoped alerts do not support database dialect {dialect!r}"
        )
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "CAST(:namespace AS INTEGER), CAST(:lock_key AS INTEGER))"
        ),
        {
            "namespace": ALERT_KEY_LOCK_NAMESPACE,
            "lock_key": _alert_lock_key(alert_key),
        },
    )


def _provider_key_predicate(provider: IntegrationProvider):
    keys = PROVIDER_ALERT_KEYS[provider]
    if not keys:
        return SystemAlert.alert_key.in_(("",))
    return SystemAlert.alert_key.in_(tuple(keys))


def _health_key_predicate():
    # SQL performs a portable coarse filter for the one dynamic family. Python's
    # exact positive-integer validator rejects malformed ``conflict:`` keys.
    return or_(
        SystemAlert.alert_key.in_(tuple(HEALTH_ALERT_KEYS)),
        SystemAlert.alert_key.like("conflict:%"),
    )


def _exact_scope_predicate(context: AlertContext):
    if isinstance(context, PlatformAlertContext):
        return and_(
            SystemAlert.subject_id.is_(None),
            SystemAlert.integration_connection_id.is_(None),
            SystemAlert.alert_key.in_(tuple(PLATFORM_ALERT_KEYS[context.namespace])),
        )
    if isinstance(context, ProviderAlertContext):
        return and_(
            SystemAlert.subject_id == context.identity.subject_id,
            SystemAlert.integration_connection_id
            == context.integration_connection_id,
        )
    return and_(
        SystemAlert.subject_id == context.identity.subject_id,
        SystemAlert.integration_connection_id.is_(None),
    )


def _legacy_scope_predicate(context: AlertContext):
    unowned = and_(
        SystemAlert.subject_id.is_(None),
        SystemAlert.integration_connection_id.is_(None),
    )
    if isinstance(context, ProviderAlertContext):
        return and_(unowned, _provider_key_predicate(context.provider))
    if isinstance(context, HealthAlertContext):
        return and_(unowned, _health_key_predicate())
    raise AlertLegacyBridgeError("platform contexts have no legacy bridge")


def _candidate_scope_predicate(
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
):
    exact = _exact_scope_predicate(context)
    if legacy_bridge is LegacyAlertBridge.REJECT:
        return exact
    return or_(exact, _legacy_scope_predicate(context))


def _row_is_exact(row: SystemAlert, context: AlertContext) -> bool:
    if isinstance(context, PlatformAlertContext):
        return (
            row.subject_id is None
            and row.integration_connection_id is None
            and row.alert_key in PLATFORM_ALERT_KEYS[context.namespace]
        )
    if isinstance(context, ProviderAlertContext):
        return (
            row.subject_id == context.identity.subject_id
            and row.integration_connection_id
            == context.integration_connection_id
        )
    return (
        row.subject_id == context.identity.subject_id
        and row.integration_connection_id is None
    )


def _row_is_eligible_legacy(
    row: SystemAlert,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
) -> bool:
    if legacy_bridge is not LegacyAlertBridge.FULLY_UNOWNED:
        return False
    if isinstance(context, PlatformAlertContext):
        return False
    if row.subject_id is not None or row.integration_connection_id is not None:
        return False
    if isinstance(context, ProviderAlertContext):
        return _provider_key_matches(context.provider, row.alert_key)
    return _is_health_key(row.alert_key)


def _adopt_legacy_row(
    row: SystemAlert,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
) -> bool:
    if _row_is_exact(row, context):
        return False
    if not _row_is_eligible_legacy(row, context, legacy_bridge):
        raise AlertScopeConflictError(
            "alert is not eligible for fully-unowned legacy adoption"
        )
    assert not isinstance(context, PlatformAlertContext)
    row.subject_id = context.identity.subject_id
    if isinstance(context, ProviderAlertContext):
        row.integration_connection_id = context.integration_connection_id
    return True


def _ownership_values(context: AlertContext) -> dict[str, uuid.UUID | None]:
    if isinstance(context, PlatformAlertContext):
        return {"subject_id": None, "integration_connection_id": None}
    return {
        "subject_id": context.identity.subject_id,
        "integration_connection_id": (
            context.integration_connection_id
            if isinstance(context, ProviderAlertContext)
            else None
        ),
    }


def _choose_active_row(
    rows: Sequence[SystemAlert],
    *,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
) -> SystemAlert | None:
    exact = [row for row in rows if _row_is_exact(row, context)]
    legacy = [
        row
        for row in rows
        if _row_is_eligible_legacy(row, context, legacy_bridge)
    ]
    selected_ids = {id(row) for row in (*exact, *legacy)}
    foreign = [row for row in rows if id(row) not in selected_ids]

    if len(exact) > 1 or len(legacy) > 1 or (exact and legacy):
        raise AlertAmbiguousMatchError(
            "multiple alerts match the exact or fully-unowned scope"
        )
    if foreign and (exact or legacy):
        raise AlertAmbiguousMatchError(
            "matching and foreign active alerts share one global key"
        )
    if exact:
        return exact[0]
    if legacy:
        return legacy[0]
    if foreign:
        raise AlertScopedUniqueCutoverRequiredError(
            "the global active-alert key is occupied by another ownership scope"
        )
    return None


async def _validate_row_semantics(
    session: AsyncSession,
    row: SystemAlert,
    context: AlertContext,
) -> None:
    allowed_domains = await _allowed_domains_for_key(session, context, row.alert_key)
    if row.domain not in allowed_domains:
        raise AlertScopeConflictError(
            "persisted alert domain conflicts with its registered key"
        )


def _stamp_resolution(row: SystemAlert, actor_user_id: uuid.UUID | None) -> bool:
    if row.resolved_at is not None:
        return False
    row.resolved_at = now_local()
    row.resolved_by_user_id = actor_user_id
    return True


def _stamp_override(row: SystemAlert, actor_user_id: uuid.UUID) -> bool:
    if row.override_at is not None:
        return False
    row.override_at = now_local()
    row.overridden_by_user_id = actor_user_id
    return True


async def _active_rows_for_key(
    session: AsyncSession,
    *,
    alert_key: str,
    entity_ref: str,
) -> list[SystemAlert]:
    return list(
        await session.scalars(
            select(SystemAlert)
            .where(
                SystemAlert.alert_key == alert_key,
                SystemAlert.entity_ref == entity_ref,
                SystemAlert.resolved_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


async def raise_scoped_alert(
    session: AsyncSession,
    *,
    context: AlertContext,
    domain: Domain,
    severity: Severity,
    message: str,
    alert_key: str,
    entity_ref: str = "",
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
    overridden: bool = False,
) -> SystemAlert:
    """Raise or refresh one alert in an explicit S/C/platform scope.

    Provider refreshes are fresh operational writes and therefore require a
    legacy or active connection. ``overridden`` is a human-only transition: its
    timestamp and actor are stamped together without replacing an earlier
    override attribution. The function flushes but never commits.
    """

    _require_domain(domain)
    _require_severity(severity)
    _require_message(message)
    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    _require_context(context)
    if not isinstance(overridden, bool):
        raise AlertValidationError("overridden must be a boolean")
    actor_user_id = _actor_user_id(context)
    if overridden and actor_user_id is None:
        raise AlertActorRequiredError("override requires an active human actor")
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=True,
        lock_roots=True,
    )
    allowed_domains = await _allowed_domains_for_key(session, context, alert_key)
    if domain.value not in allowed_domains:
        raise AlertScopeConflictError(
            "alert domain conflicts with its registered key"
        )
    await _acquire_alert_key_lock(session, alert_key)

    rows = await _active_rows_for_key(
        session,
        alert_key=alert_key,
        entity_ref=entity_ref,
    )
    row = _choose_active_row(
        rows,
        context=context,
        legacy_bridge=legacy_bridge,
    )
    if row is not None:
        await _validate_row_semantics(session, row, context)
        if row.domain != domain.value:
            raise AlertScopeConflictError(
                "an active alert cannot change its persisted domain"
            )
        _adopt_legacy_row(row, context, legacy_bridge)
        row.severity = severity.value
        row.message = message
        if overridden:
            assert actor_user_id is not None
            _stamp_override(row, actor_user_id)
        await session.flush()
        return row

    override_at = now_local() if overridden else None
    row = SystemAlert(
        domain=domain.value,
        severity=severity.value,
        message=message,
        alert_key=alert_key,
        entity_ref=entity_ref,
        override_at=override_at,
        overridden_by_user_id=(actor_user_id if overridden else None),
        **_ownership_values(context),
    )
    session.add(row)
    await session.flush()
    return row


async def _scoped_row_by_id(
    session: AsyncSession,
    *,
    alert_id: int,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
    for_update: bool,
) -> SystemAlert | None:
    stmt = select(SystemAlert).where(
        SystemAlert.id == alert_id,
        _candidate_scope_predicate(context, legacy_bridge),
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(stmt)


async def resolve_scoped_alert(
    session: AsyncSession,
    alert_id: int,
    *,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> SystemAlert | None:
    """Resolve one visible scoped alert; foreign IDs are non-enumerating misses."""

    _require_alert_id(alert_id)
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    with session.no_autoflush:
        candidate = await _scoped_row_by_id(
            session,
            alert_id=alert_id,
            context=context,
            legacy_bridge=legacy_bridge,
            for_update=False,
        )
    if candidate is None:
        return None
    await _validate_row_semantics(session, candidate, context)
    await _acquire_alert_key_lock(session, candidate.alert_key)
    row = await _scoped_row_by_id(
        session,
        alert_id=alert_id,
        context=context,
        legacy_bridge=legacy_bridge,
        for_update=True,
    )
    if row is None:
        return None
    changed = _adopt_legacy_row(row, context, legacy_bridge)
    changed = _stamp_resolution(row, _actor_user_id(context)) or changed
    if changed:
        await session.flush()
    return row


async def resolve_scoped_by_key(
    session: AsyncSession,
    *,
    context: AlertContext,
    alert_key: str,
    entity_ref: str = "",
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> SystemAlert | None:
    """Resolve the active alert in one exact scope and natural key."""

    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    await _allowed_domains_for_key(session, context, alert_key)
    await _acquire_alert_key_lock(session, alert_key)
    row = _choose_active_row(
        await _active_rows_for_key(
            session,
            alert_key=alert_key,
            entity_ref=entity_ref,
        ),
        context=context,
        legacy_bridge=legacy_bridge,
    )
    if row is None:
        return None
    await _validate_row_semantics(session, row, context)
    _adopt_legacy_row(row, context, legacy_bridge)
    _stamp_resolution(row, _actor_user_id(context))
    await session.flush()
    return row


async def resolve_fully_unowned_by_key_preserving_roots(
    session: AsyncSession,
    *,
    context: HealthAlertContext,
    alert_key: str,
    entity_ref: str = "",
) -> SystemAlert | None:
    """Resolve one legacy health alert without fabricating ownership roots.

    This narrow migration seam is for automated cleanup of a historical row
    whose ``S`` and ``C`` were never recorded.  Exact-one governance proves
    which installation may retire it, but that proof does not reconstruct its
    original subject or provider provenance.  New alerts must use the regular
    scoped APIs.
    """

    if not isinstance(context, HealthAlertContext):
        raise AlertValidationError("legacy root preservation requires health context")
    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=LegacyAlertBridge.FULLY_UNOWNED,
        fresh_provider_write=False,
        lock_roots=True,
    )
    await _allowed_domains_for_key(session, context, alert_key)
    await _acquire_alert_key_lock(session, alert_key)
    rows = list(
        await session.scalars(
            select(SystemAlert)
            .where(
                SystemAlert.subject_id.is_(None),
                SystemAlert.integration_connection_id.is_(None),
                SystemAlert.alert_key == alert_key,
                SystemAlert.entity_ref == entity_ref,
                SystemAlert.resolved_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(rows) > 1:
        raise AlertAmbiguousMatchError(
            "multiple fully-unowned alerts share one active natural key"
        )
    if not rows:
        return None
    row = rows[0]
    await _validate_row_semantics(session, row, context)
    _stamp_resolution(row, _actor_user_id(context))
    await session.flush()
    return row


async def override_scoped_alert(
    session: AsyncSession,
    alert_id: int,
    *,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> SystemAlert | None:
    """Stamp one human override without rewriting an earlier lifecycle actor."""

    _require_alert_id(alert_id)
    _require_context(context)
    actor_user_id = _actor_user_id(context)
    if actor_user_id is None:
        raise AlertActorRequiredError("override requires an active human actor")
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    with session.no_autoflush:
        candidate = await _scoped_row_by_id(
            session,
            alert_id=alert_id,
            context=context,
            legacy_bridge=legacy_bridge,
            for_update=False,
        )
    if candidate is None:
        return None
    await _validate_row_semantics(session, candidate, context)
    await _acquire_alert_key_lock(session, candidate.alert_key)
    row = await _scoped_row_by_id(
        session,
        alert_id=alert_id,
        context=context,
        legacy_bridge=legacy_bridge,
        for_update=True,
    )
    if row is None:
        return None
    changed = _adopt_legacy_row(row, context, legacy_bridge)
    changed = _stamp_override(row, actor_user_id) or changed
    if changed:
        await session.flush()
    return row


async def resolve_scoped_superseded(
    session: AsyncSession,
    *,
    context: AlertContext,
    alert_key: str,
    keep_entity: str | None,
    marker: str | None = None,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> int:
    """Resolve stale entities for one key without crossing its ownership scope."""

    _require_key(alert_key)
    _require_optional_entity(keep_entity, "keep_entity")
    _require_optional_entity(marker, "marker")
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    await _allowed_domains_for_key(session, context, alert_key)
    await _acquire_alert_key_lock(session, alert_key)
    rows = list(
        await session.scalars(
            select(SystemAlert)
            .where(
                SystemAlert.alert_key == alert_key,
                SystemAlert.resolved_at.is_(None),
                _candidate_scope_predicate(context, legacy_bridge),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    changed = 0
    actor_user_id = _actor_user_id(context)
    for row in rows:
        await _validate_row_semantics(session, row, context)
        if row.entity_ref == keep_entity:
            continue
        if marker is not None and not (
            row.entity_ref == marker or row.entity_ref.startswith(f"{marker}:")
        ):
            continue
        _adopt_legacy_row(row, context, legacy_bridge)
        if _stamp_resolution(row, actor_user_id):
            changed += 1
    if changed:
        await session.flush()
    return changed


async def was_scoped_dismissed_today(
    session: AsyncSession,
    *,
    context: AlertContext,
    alert_key: str,
    entity_ref: str,
    on_date: date_type | None = None,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> bool:
    """Return whether this scoped alert was resolved on one local calendar date."""

    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    if on_date is not None and not isinstance(on_date, date_type):
        raise AlertValidationError("on_date must be a date or None")
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=False,
    )
    allowed_domains = await _allowed_domains_for_key(session, context, alert_key)
    resolved_date = on_date or today_local()
    count = await session.scalar(
        select(func.count()).where(
            SystemAlert.alert_key == alert_key,
            SystemAlert.entity_ref == entity_ref,
            SystemAlert.resolved_at.is_not(None),
            func.date(SystemAlert.resolved_at) == resolved_date,
            SystemAlert.domain.in_(tuple(allowed_domains)),
            _candidate_scope_predicate(context, legacy_bridge),
        )
    )
    return (count or 0) > 0


async def was_scoped_ever_dismissed(
    session: AsyncSession,
    *,
    context: AlertContext,
    alert_key: str,
    entity_ref: str,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> bool:
    """Return whether this exact scoped alert was ever resolved."""

    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=False,
    )
    allowed_domains = await _allowed_domains_for_key(session, context, alert_key)
    count = await session.scalar(
        select(func.count()).where(
            SystemAlert.alert_key == alert_key,
            SystemAlert.entity_ref == entity_ref,
            SystemAlert.resolved_at.is_not(None),
            SystemAlert.domain.in_(tuple(allowed_domains)),
            _candidate_scope_predicate(context, legacy_bridge),
        )
    )
    return (count or 0) > 0


async def list_active_scoped(
    session: AsyncSession,
    *,
    context: AlertContext,
    domain: Domain | None = None,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> Sequence[SystemAlert]:
    """List active alerts visible in exactly one typed ownership context."""

    _require_domain(domain, optional=True)
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=False,
    )
    _validate_platform_domain(context, domain)
    stmt = select(SystemAlert).where(
        SystemAlert.resolved_at.is_(None),
        _candidate_scope_predicate(context, legacy_bridge),
    )
    if domain is not None:
        stmt = stmt.where(SystemAlert.domain == domain.value)
    stmt = stmt.order_by(SystemAlert.created_at.desc(), SystemAlert.id.desc())
    rows = list(await session.scalars(stmt))
    for row in rows:
        await _validate_row_semantics(session, row, context)
    return rows


async def resolve_all_scoped(
    session: AsyncSession,
    *,
    context: AlertContext,
    domain: Domain | None = None,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> int:
    """Resolve all currently active alerts in one exact ownership scope."""

    _require_domain(domain, optional=True)
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    _validate_platform_domain(context, domain)
    predicate = _candidate_scope_predicate(context, legacy_bridge)
    key_stmt = select(SystemAlert.alert_key).where(
        SystemAlert.resolved_at.is_(None), predicate
    )
    if domain is not None:
        key_stmt = key_stmt.where(SystemAlert.domain == domain.value)
    keys = sorted(set(await session.scalars(key_stmt)))
    for alert_key in keys:
        await _allowed_domains_for_key(session, context, alert_key)
        await _acquire_alert_key_lock(session, alert_key)

    stmt = select(SystemAlert).where(
        SystemAlert.resolved_at.is_(None), predicate
    )
    if domain is not None:
        stmt = stmt.where(SystemAlert.domain == domain.value)
    rows = list(
        await session.scalars(
            stmt.with_for_update().execution_options(populate_existing=True)
        )
    )
    actor_user_id = _actor_user_id(context)
    for row in rows:
        await _validate_row_semantics(session, row, context)
        _adopt_legacy_row(row, context, legacy_bridge)
        _stamp_resolution(row, actor_user_id)
    if rows:
        await session.flush()
    return len(rows)
