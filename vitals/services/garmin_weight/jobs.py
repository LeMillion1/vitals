"""Status, explicit dispatch, and scheduled Garmin Weight entry points."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import IntegrationProvider
from vitals.models.garmin import (
    WEIGHT_EXPORT_CHECKING,
    WEIGHT_EXPORT_CONFLICT,
    WEIGHT_EXPORT_DELETE_CHECKING,
    WEIGHT_EXPORT_DELETE_FAILED,
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_UNVERIFIED,
    GarminWeightExport,
)
from vitals.models.identity import HealthSubject
from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.garmin_weight.contracts import (
    OPERATION_LOCK_TTL_SECONDS,
    GarminWeightExportConnectionInactiveError,
    GarminWeightExportOwnershipError,
    PreparedGarminWeightExport,
    _require_prepared_export,
)
from vitals.services.garmin_weight.dispatch import export_latest, export_latest_scoped
from vitals.services.garmin_weight.outbox import (
    _activate_scoped_export,
    _active_export_context,
    _scoped_rows,
    prepare_scoped_export,
    resolve_scoped_export_context,
)
from vitals.services.garmin_weight.settings import is_enabled
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.proactive.preferences import legacy as preference_legacy

logger = logging.getLogger(__name__)


async def get_status(session: AsyncSession) -> dict[str, Any]:
    """Small settings-card projection, prioritising unresolved safety states."""
    enabled = await is_enabled(session)
    if _active_export_context() is None:
        result = await session.execute(
            select(GarminWeightExport).order_by(
                GarminWeightExport.date.desc(), GarminWeightExport.id.desc()
            )
        )
        rows = list(result.scalars().all())
    else:
        rows = await _scoped_rows(session, for_update=False)
    priority = {
        WEIGHT_EXPORT_DELETE_FAILED: 0,
        WEIGHT_EXPORT_UNVERIFIED: 1,
        WEIGHT_EXPORT_CONFLICT: 2,
        WEIGHT_EXPORT_FAILED: 3,
        WEIGHT_EXPORT_DELETE_PENDING: 4,
        WEIGHT_EXPORT_DELETE_CHECKING: 5,
        WEIGHT_EXPORT_CHECKING: 6,
        WEIGHT_EXPORT_PENDING: 7,
    }
    row = min(
        rows,
        key=lambda item: (priority.get(item.status, 10), -item.date.toordinal(), -item.id),
        default=None,
    )
    return {
        "enabled": enabled,
        "status": row.status if row is not None else None,
        "date": row.date if row is not None else None,
        "weight_kg": row.weight_kg if row is not None else None,
        "exported_at": row.exported_at if row is not None else None,
        "next_attempt_at": row.next_attempt_at if row is not None else None,
        "last_error": row.last_error if row is not None else None,
    }


async def get_status_scoped(
    session: AsyncSession,
    *,
    prepared: PreparedGarminWeightExport,
) -> dict[str, Any]:
    _require_prepared_export(session, prepared)
    with _activate_scoped_export(prepared):
        return await get_status(session)


async def send_now(session: AsyncSession, *, redis=None) -> dict[str, Any]:
    """Explicit user-triggered reconciliation. The caller owns the commit."""
    from vitals.integrations.garmin_client import GarminClient

    if _active_export_context() is None:
        await preference_legacy.get_pre_identity_legacy_prefs(session)
    if not await is_enabled(session):
        return {"status": "disabled", "sent": False}
    client = GarminClient.from_config(redis=redis)
    if not client.is_configured:
        return {"status": "unconfigured", "sent": False}
    if redis is not None:
        from vitals.scheduler.scheduler_lock import with_scheduler_lock

        result = await with_scheduler_lock(
            redis,
            "garmin_weight_export",
            OPERATION_LOCK_TTL_SECONDS,
            export_latest,
            session,
            client,
            force=True,
            require_enabled=True,
        )
        result = result or {"status": "busy", "sent": False}
    else:
        result = await export_latest(session, client, force=True, require_enabled=True)
    from vitals.services.garmin.alerts import refresh_token_cache_alert

    await refresh_token_cache_alert(session, client, resolve_if_clear=False)
    return result


async def send_now_scoped(
    session: AsyncSession,
    *,
    prepared: PreparedGarminWeightExport,
    redis=None,
) -> dict[str, Any]:
    """Scoped explicit reconciliation; suitable for the Settings boundary."""

    context = _require_prepared_export(
        session,
        prepared,
        historical_ok=False,
    )
    with _activate_scoped_export(prepared):
        enabled = await is_enabled(session)
    if not enabled:
        return {"status": "disabled", "sent": False}
    from vitals.integrations.garmin_client import GarminClient
    from vitals.services.credentials import providers

    account = await providers.resolve_garmin_account(
        session, subject_id=context.identity.subject_id
    )
    if account is None or not account.configured:
        return {"status": "unconfigured", "sent": False}
    client = GarminClient.from_config(account.config, redis)

    # The Settings request prepared governance, advisory, identity, and account
    # locks before reaching this boundary. Release all of them before waiting on
    # Redis. Only the immutable context may cross the commit; the callback gets
    # a capability issued in its own current transaction.
    await session.commit()

    async def run_scoped_export() -> dict[str, Any]:
        try:
            fresh = await prepare_scoped_export(
                session,
                context=context,
                historical=False,
            )
            result = await export_latest_scoped(
                session,
                client,
                prepared=fresh,
                force=True,
                require_enabled=True,
            )
            # Every return path must release transaction-scoped DB locks before
            # with_scheduler_lock performs its external Redis unlock.
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise

    if redis is not None:
        from vitals.scheduler.scheduler_lock import with_scheduler_lock

        # Per connection, not per job. The flat ``garmin_weight_export`` name
        # was one lock for the installation, so with two people one patient
        # pressing Send now made the other's answer "busy" — for an operation
        # that touches a different Garmin account entirely. Mutual exclusion is
        # about the account the outbox is draining into.
        result = await with_scheduler_lock(
            redis,
            f"garmin_weight_export:{context.integration_connection_id}",
            OPERATION_LOCK_TTL_SECONDS,
            run_scoped_export,
        )
        result = result or {"status": "busy", "sent": False}
    else:
        result = await run_scoped_export()
    # export_latest may commit more than once; only immutable context crosses
    # that boundary and a fresh capability authorizes the provider alert.
    try:
        alert_prepared = await prepare_scoped_export(
            session,
            context=context,
            historical=False,
        )
    except GarminWeightExportConnectionInactiveError:
        return result
    from vitals.services.garmin import alerts as garmin_alerts

    with _activate_scoped_export(alert_prepared):
        await garmin_alerts._refresh_owned_token_cache_alert(
            session,
            client,
            context=alerts_service_contracts.ProviderAlertContext(
                identity=context.identity,
                provider=IntegrationProvider.GARMIN,
                integration_connection_id=context.integration_connection_id,
            ),
            resolve_if_clear=False,
        )
    return result


async def _export_job_pre_identity_legacy(session, *, redis=None) -> None:
    """Compatibility job used only while the database has zero subjects."""

    from vitals.i18n import current_lang
    from vitals.integrations.garmin_client import GarminClient
    from vitals.services.garmin.alerts import refresh_token_cache_alert
    from vitals.services.preferences.language import get_language

    await preference_legacy.get_pre_identity_legacy_prefs(session)
    if not await is_enabled(session):
        return
    # Governance is already held by export_job. Release it before Redis or the
    # provider client boundary; export_latest acquires its own advisory lease.
    await session.commit()
    current_lang.set(await get_language(session, redis))
    await session.commit()
    client = GarminClient.from_config(redis=redis)
    if not client.is_configured:
        return
    await export_latest(session, client, require_enabled=True)
    await refresh_token_cache_alert(session, client, resolve_if_clear=False)
    await session.commit()


async def export_job(
    session_factory,
    redis=None,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
) -> None:
    """Scheduled scoped entry point for the sole registration-off owner graph."""
    from vitals.i18n import current_lang
    from vitals.integrations.garmin_client import GarminClient
    from vitals.services.preferences.language import get_language
    from vitals.services.tenancy.contracts import LegacyOwnershipError

    async with session_factory() as session:
        await acquire_identity_governance_lock(session)
        any_subject = await session.scalar(select(HealthSubject.id).limit(1))
        if any_subject is None:
            # Re-enter through the shared guarded zero-subject boundary. The
            # probe above opened an unrecognized read transaction.
            await session.rollback()
            await _export_job_pre_identity_legacy(session, redis=redis)
            return
        try:
            context = await resolve_scoped_export_context(
                session,
                subject_id=subject_id,
            )
            prepared = await prepare_scoped_export(
                session,
                context=context,
                historical=False,
            )
        except (LegacyOwnershipError, GarminWeightExportOwnershipError) as exc:
            logger.warning("Garmin Weight export ownership is unavailable: %s", exc)
            return
        with _activate_scoped_export(prepared):
            enabled = await is_enabled(session)
        if not enabled:
            return
        owner_user_id = await session.scalar(
            select(HealthSubject.owner_user_id).where(
                HealthSubject.id == context.identity.subject_id
            )
        )
        # Do not carry governance/account/outbox locks into client construction
        # or any external Redis/vendor request. Re-prepare below in the
        # transaction that performs the scoped reconciliation.
        await session.commit()
        current_lang.set(await get_language(session, redis, user_id=owner_user_id))
        from vitals.services.credentials import providers

        account = await providers.resolve_garmin_account(
            session, subject_id=context.identity.subject_id
        )
        if account is None or not account.configured:
            return
        client = GarminClient.from_config(account.config, redis)
        try:
            prepared = await prepare_scoped_export(
                session,
                context=context,
                historical=False,
            )
        except GarminWeightExportConnectionInactiveError:
            await session.rollback()
            return
        await export_latest_scoped(
            session,
            client,
            prepared=prepared,
            require_enabled=True,
        )

        # export_latest may have committed one or more durable leases. Prepare
        # again before writing the provider-scoped token-cache alert.
        try:
            alert_prepared = await prepare_scoped_export(
                session,
                context=context,
                historical=False,
            )
        except GarminWeightExportConnectionInactiveError:
            await session.rollback()
            return
        from vitals.services.garmin import alerts as garmin_alerts

        with _activate_scoped_export(alert_prepared):
            await garmin_alerts._refresh_owned_token_cache_alert(
                session,
                client,
                context=alerts_service_contracts.ProviderAlertContext(
                    identity=context.identity,
                    provider=IntegrationProvider.GARMIN,
                    integration_connection_id=(context.integration_connection_id),
                ),
                resolve_if_clear=False,
            )
        await session.commit()
