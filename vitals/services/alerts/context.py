"""Context boundary for system alerts."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.conflict_rule import ConflictRule
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity_service import (
    UnsupportedIdentityDatabaseError,
    acquire_identity_governance_lock,
)

from vitals.services.alerts.contracts import (
    AlertUnsupportedDatabaseError,
    AlertSubjectNotFoundError,
    AlertActorNotFoundError,
    AlertActorInactiveError,
    AlertConnectionNotFoundError,
    AlertConnectionOwnershipError,
    AlertConnectionProviderError,
    AlertConnectionTypeError,
    AlertConnectionStateError,
    AlertScopeConflictError,
    AlertLegacyBridgeError,
    AlertPlatformNamespaceError,
    LegacyAlertBridge,
    ProviderAlertContext,
    PlatformAlertContext,
    AlertContext,
    PROVIDER_ALERT_CONNECTION_TYPES,
    ALERT_KEY_DOMAINS,
    _KNOWN_CONNECTION_STATUSES,
    _FRESH_PROVIDER_STATUSES,
    _HISTORICAL_PROVIDER_STATUSES,
)

from vitals.services.alerts.validation import (
    _require_context,
    _require_bridge,
    _is_classified_key,
    _validate_context_key,
)


async def _allowed_domains_for_key(
    session: AsyncSession,
    context: AlertContext,
    alert_key: str,
) -> frozenset[str]:
    _validate_context_key(context, alert_key)
    subject_id = None if isinstance(context, PlatformAlertContext) else context.identity.subject_id
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
            raise AlertScopeConflictError("registered alert_key has no domain contract") from exc

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
        raise AlertScopeConflictError("conflict alert references another subject's rule")
    try:
        domains = frozenset({Domain(rule.domain_a).value, Domain(rule.domain_b).value})
    except ValueError as exc:
        raise AlertScopeConflictError("conflict rule contains an unknown domain") from exc
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
        raise AlertPlatformNamespaceError("platform alert namespaces require Domain.SYSTEM")


async def _validate_active_actor(
    session: AsyncSession,
    actor_user_id: uuid.UUID | None,
) -> None:
    if actor_user_id is None:
        return
    actor_status = await session.scalar(select(User.status).where(User.id == actor_user_id))
    if actor_status is None:
        raise AlertActorNotFoundError("actor user does not exist")
    if actor_status != UserStatus.ACTIVE.value:
        raise AlertActorInactiveError("actor user is not active")


async def _installation_has_unowned_alerts(session: AsyncSession) -> bool:
    """Whether any alert is still waiting for the ownership backfill.

    This is the question the fully-unowned bridge exists to answer, and it is
    not the same question as "how many people are in this installation". The
    bridge widens exactly one predicate — ``subject_id IS NULL AND
    integration_connection_id IS NULL`` — so with no such row it widens nothing,
    and there is nobody's alert to decide the owner of.

    ``scripts/backfill_system_alert_subject_ownership.py`` is what empties this
    set, and it is meant to run while the installation is still one person,
    which is exactly when adopting an unowned row into that person is right.
    Afterwards this returns False and the bridge is inert.
    """

    with session.no_autoflush:
        found = await session.scalar(
            select(SystemAlert.id)
            .where(
                SystemAlert.subject_id.is_(None),
                SystemAlert.integration_connection_id.is_(None),
            )
            .limit(1)
        )
    return found is not None


async def _require_single_subject_bridge(
    session: AsyncSession,
    subject: HealthSubject,
) -> None:
    with session.no_autoflush:
        subject_ids = list(
            await session.scalars(select(HealthSubject.id).order_by(HealthSubject.id).limit(2))
        )
    if subject_ids != [subject.id]:
        raise AlertLegacyBridgeError(
            "fully-unowned alerts require exactly one matching health subject"
        )
    owner_status = await session.scalar(select(User.status).where(User.id == subject.owner_user_id))
    if owner_status != UserStatus.ACTIVE.value:
        raise AlertLegacyBridgeError("fully-unowned alerts require an active sole-subject owner")


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
) -> LegacyAlertBridge:
    """Validate the roots, and report the bridge that actually applies.

    Returns the requested bridge, except that a requested ``FULLY_UNOWNED``
    comes back as ``REJECT`` when the installation holds no unowned alert. That
    is a downgrade rather than a permission: with nothing to adopt, widening and
    not widening return the same rows, and the caller must use the value
    returned rather than the one it asked for — otherwise the sole-subject proof
    would be skipped while the query still reached for nobody's rows, which is
    the one combination that could show one person another's alert.
    """

    _require_context(context)
    _require_bridge(legacy_bridge)
    if isinstance(context, PlatformAlertContext):
        if legacy_bridge is not LegacyAlertBridge.REJECT:
            raise AlertLegacyBridgeError(
                "platform alert contexts cannot use the legacy ownership bridge"
            )
        await _validate_active_actor(session, context.actor_user_id)
        return legacy_bridge

    if legacy_bridge is LegacyAlertBridge.FULLY_UNOWNED:
        try:
            await acquire_identity_governance_lock(session)
        except UnsupportedIdentityDatabaseError as exc:
            raise AlertUnsupportedDatabaseError(str(exc)) from exc
        # Under the governance lock, so the answer cannot change underneath the
        # proof that follows it.
        if not await _installation_has_unowned_alerts(session):
            legacy_bridge = LegacyAlertBridge.REJECT

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
        return legacy_bridge
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
        raise AlertConnectionOwnershipError("integration connection belongs to another subject")
    if connection.provider != context.provider.value:
        raise AlertConnectionProviderError("integration connection belongs to another provider")
    expected_type = PROVIDER_ALERT_CONNECTION_TYPES[context.provider]
    if connection.connection_type != expected_type.value:
        raise AlertConnectionTypeError("integration connection has the wrong provider-alert type")
    if connection.status not in _KNOWN_CONNECTION_STATUSES:
        raise AlertConnectionStateError("integration connection has an unknown lifecycle state")
    allowed_statuses = (
        _FRESH_PROVIDER_STATUSES if fresh_provider_write else _HISTORICAL_PROVIDER_STATUSES
    )
    if connection.status not in allowed_statuses:
        raise AlertConnectionStateError(
            "integration connection cannot authorize this alert operation"
        )
    return legacy_bridge
