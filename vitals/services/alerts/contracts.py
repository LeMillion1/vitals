"""Contracts boundary for system alerts."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias


from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.ownership import WriteIdentity


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
        if self.actor_user_id is not None and not isinstance(self.actor_user_id, uuid.UUID):
            raise AlertContextError("actor_user_id must be a UUID or None")


AlertContext: TypeAlias = HealthAlertContext | ProviderAlertContext | PlatformAlertContext


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
        # Nothing raises this any more — the parser it belonged to is gone with
        # the chat it read. The key stays registered so alerts already in the
        # lake can still be listed and resolved, and because the
        # ``ck_system_alerts_ai_invocation_scope`` constraint still names it.
        "signal_parser_failed",
        "scheduler.job_failed:glp1_plateau",
        "scheduler.job_failed:hrt_reminders",
        "scheduler.job_failed:nutrition_day_end",
        "scheduler.job_failed:daily_brief",
        "scheduler.job_failed:nudges",
        "scheduler.job_failed:weekly_digest",
    }
)
PROVIDER_ALERT_KEYS: Mapping[IntegrationProvider, frozenset[str]] = MappingProxyType(
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
PLATFORM_ALERT_KEYS: Mapping[PlatformAlertNamespace, frozenset[str]] = MappingProxyType(
    {
        PlatformAlertNamespace.SCHEDULER_JOB_FAILURE: frozenset(
            {
                "scheduler.job_failed:raw_payload_sweep",
                "scheduler.job_failed:share_purge",
                "scheduler.job_failed:ai_invocation_reconcile",
                "scheduler.job_failed:notification_delivery_reconcile",
                "scheduler.job_failed:care_push_dispatch",
                "scheduler.job_failed:registration_admission_retention",
            }
        )
    }
)
PROVIDER_ALERT_CONNECTION_TYPES: Mapping[IntegrationProvider, IntegrationConnectionType] = (
    MappingProxyType(
        {
            IntegrationProvider.GARMIN: IntegrationConnectionType.ACCOUNT,
            IntegrationProvider.HEVY: IntegrationConnectionType.ACCOUNT,
            IntegrationProvider.OPENROUTER: IntegrationConnectionType.AI_GATEWAY,
            IntegrationProvider.TELEGRAM: IntegrationConnectionType.RECIPIENT,
        }
    )
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
        # Historical only; see the note beside it in HEALTH_ALERT_KEYS. Domain
        # SYSTEM because the domain it used to carry went with the parser.
        "signal_parser_failed": Domain.SYSTEM,
        **{
            key: Domain.SYSTEM
            for key in set().union(
                *(
                    keys
                    for keys in (
                        HEALTH_ALERT_KEYS,
                        *PROVIDER_ALERT_KEYS.values(),
                        *PLATFORM_ALERT_KEYS.values(),
                    )
                )
            )
            if key.startswith("scheduler.job_failed:")
        },
    }
)

ALERT_KEY_LOCK_NAMESPACE = 0x414C5254  # ASCII "ALRT", signed int32-safe.
_KNOWN_CONNECTION_STATUSES = frozenset(status.value for status in IntegrationConnectionStatus)
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
