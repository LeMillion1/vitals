"""Operational alert ownership for Garmin accounts."""

from __future__ import annotations

from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import legacy as alerts_service_legacy
from vitals.services.alerts import lifecycle as alerts_service_lifecycle

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, IntegrationProvider, Severity
from vitals.i18n import t
from vitals.models.garmin import DOMAIN
from vitals.ownership import WriteIdentity
from vitals.services.garmin.ownership import _validate_owned_context

AUTH_ALERT_KEY = "garmin.auth"
TOKEN_ALERT_KEY = "garmin.token_cache"


async def refresh_token_cache_alert(
    session: AsyncSession,
    client: Any,
    *,
    resolve_if_clear: bool = True,
) -> None:
    """Surface token-store failures collected by a legacy Garmin operation."""

    warnings = list(getattr(client, "token_warnings", None) or ())
    if warnings:
        await alerts_service_legacy.raise_alert(
            session,
            domain=DOMAIN,
            severity=Severity.WARN.value,
            message=t("alert.garmin_token_cache", error=warnings[0]),
            alert_key=TOKEN_ALERT_KEY,
        )
    elif resolve_if_clear:
        await alerts_service_legacy.resolve_by_key(session, alert_key=TOKEN_ALERT_KEY)


def _owned_provider_alert_context(
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> alerts_service_contracts.ProviderAlertContext:
    """Bind operational Garmin alerts to the account, never to a human actor."""

    _validate_owned_context(identity, integration_connection_id)
    return alerts_service_contracts.ProviderAlertContext(
        identity=WriteIdentity(subject_id=identity.subject_id, actor_user_id=None),
        provider=IntegrationProvider.GARMIN,
        integration_connection_id=integration_connection_id,
    )


async def _refresh_owned_token_cache_alert(
    session: AsyncSession,
    client: Any,
    *,
    context: alerts_service_contracts.ProviderAlertContext,
    resolve_if_clear: bool = True,
) -> None:
    """Surface token-store warnings within one Garmin account scope."""

    warnings = list(getattr(client, "token_warnings", None) or ())
    if warnings:
        await alerts_service_lifecycle.raise_scoped_alert(
            session,
            context=context,
            domain=Domain.GARMIN,
            severity=Severity.WARN,
            message=t("alert.garmin_token_cache", error=warnings[0]),
            alert_key=TOKEN_ALERT_KEY,
            legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
        )
    elif resolve_if_clear:
        await alerts_service_lifecycle.resolve_scoped_by_key(
            session,
            context=context,
            alert_key=TOKEN_ALERT_KEY,
            legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
        )
