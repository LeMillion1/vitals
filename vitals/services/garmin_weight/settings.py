"""Opt-in settings for one Garmin account's Weight export."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import IntegrationConnectionStatus
from vitals.models.app_settings import AppSetting
from vitals.models.garmin import (
    WEIGHT_EXPORT_DELETE_CHECKING,
    WEIGHT_EXPORT_DELETE_PENDING,
    GarminWeightExport,
)
from vitals.models.scoped_settings import IntegrationConnectionSetting
from vitals.models.tenancy import IntegrationConnection
from vitals.services import scoped_settings_service
from vitals.services.alerts import legacy as alerts_service_legacy
from vitals.services.conflicts import engine
from vitals.services.garmin_weight.contracts import (
    ALERT_ENTITY,
    ALERT_KEY,
    SETTING_KEY,
    PreparedGarminWeightExport,
    _require_prepared_export,
)
from vitals.services.garmin_weight.outbox import (
    _SCOPED_EXPORT,
    _acquire_operation_lock,
    _activate_scoped_export,
)
from vitals.services.proactive.preferences import legacy as preference_legacy


async def is_enabled(session: AsyncSession) -> bool:
    active = _SCOPED_EXPORT.get()
    if active is not None:
        context = _require_prepared_export(session, active)
        scoped = await session.scalar(
            select(IntegrationConnectionSetting).where(
                IntegrationConnectionSetting.integration_connection_id
                == context.integration_connection_id,
                IntegrationConnectionSetting.key == SETTING_KEY,
            )
        )
        if scoped is not None:
            return scoped.value is True
        if context.legacy_bridge is not engine.LegacyConflictBridge.FULLY_UNOWNED:
            return False
        connection_status = await session.scalar(
            select(IntegrationConnection.status).where(
                IntegrationConnection.id == context.integration_connection_id
            )
        )
        if connection_status == IntegrationConnectionStatus.RETIRED.value:
            return False
        value = await scoped_settings_service.get_scoped_setting(
            session,
            scope=scoped_settings_service.SettingScope.INTEGRATION_CONNECTION,
            key=scoped_settings_service.ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED,
            expected_subject_id=context.identity.subject_id,
            scope_id=context.integration_connection_id,
            default=False,
        )
        return value is True
    # ``expire_on_commit=False`` keeps objects in the identity map. Always force
    # a SELECT here so a long-running exporter observes an opt-out committed by
    # another session before it performs a vendor mutation.
    row = (
        await session.execute(
            select(AppSetting)
            .where(AppSetting.key == SETTING_KEY)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return row is not None and row.value is True


async def is_enabled_scoped(
    session: AsyncSession,
    *,
    prepared: PreparedGarminWeightExport,
) -> bool:
    _require_prepared_export(session, prepared)
    with _activate_scoped_export(prepared):
        return await is_enabled(session)


async def set_enabled(
    session: AsyncSession,
    enabled: bool,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Persist the opt-in switch. Flushes; the caller owns the commit."""
    from vitals.services.garmin_weight.reconciliation import (
        _reset_retry,
        _skip_actionable_except,
        reconcile_latest,
    )

    settings = await preference_legacy.get_pre_identity_legacy_prefs(session)
    await _acquire_operation_lock(session)
    clean = bool(enabled)
    row = await session.get(AppSetting, SETTING_KEY)
    if row is None:
        session.add(AppSetting(key=SETTING_KEY, value=clean))
    else:
        row.value = clean
    if clean:
        # Populate the status card immediately; enabling never performs network I/O.
        await reconcile_latest(
            session,
            now=now,
            max_age_days=settings["garmin_weight_max_age_days"],
        )
    else:
        # Cancel any fresh-value preflight atomically with opt-out. Cleanup and
        # ambiguous-POST records remain recorded, but no job runs while disabled.
        await _skip_actionable_except(session, keep_date=None)
        result = await session.execute(
            select(GarminWeightExport).where(
                GarminWeightExport.status == WEIGHT_EXPORT_DELETE_CHECKING
            )
        )
        for outbox in result.scalars().all():
            outbox.status = WEIGHT_EXPORT_DELETE_PENDING
            _reset_retry(outbox)
        await alerts_service_legacy.resolve_by_key(
            session, alert_key=ALERT_KEY, entity_ref=ALERT_ENTITY
        )
    await session.flush()
    return clean


async def set_enabled_scoped(
    session: AsyncSession,
    enabled: bool,
    *,
    prepared: PreparedGarminWeightExport,
    now: Optional[datetime] = None,
) -> bool:
    from vitals.services.garmin_weight.outbox import _scoped_rows
    from vitals.services.garmin_weight.reconciliation import (
        _reset_retry,
        _resolve_alert_if_clear,
        _skip_actionable_except,
        reconcile_latest,
    )

    clean = bool(enabled)
    context = _require_prepared_export(
        session,
        prepared,
        historical_ok=not clean,
    )
    with _activate_scoped_export(prepared):
        connection_status = await session.scalar(
            select(IntegrationConnection.status).where(
                IntegrationConnection.id == context.integration_connection_id
            )
        )
        bridge_is_open = (
            context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED
            and connection_status != IntegrationConnectionStatus.RETIRED.value
        )
        if bridge_is_open:
            await scoped_settings_service.set_scoped_setting(
                session,
                scope=scoped_settings_service.SettingScope.INTEGRATION_CONNECTION,
                key=(scoped_settings_service.ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED),
                value=clean,
                expected_subject_id=context.identity.subject_id,
                scope_id=context.integration_connection_id,
            )
        else:
            scoped = await session.scalar(
                select(IntegrationConnectionSetting)
                .where(
                    IntegrationConnectionSetting.integration_connection_id
                    == context.integration_connection_id,
                    IntegrationConnectionSetting.key == SETTING_KEY,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if scoped is None:
                session.add(
                    IntegrationConnectionSetting(
                        integration_connection_id=(context.integration_connection_id),
                        key=SETTING_KEY,
                        value=clean,
                    )
                )
            else:
                scoped.value = clean
        if clean:
            await reconcile_latest(session, now=now)
        else:
            await _skip_actionable_except(session, keep_date=None)
            for outbox in await _scoped_rows(
                session,
                filters=(GarminWeightExport.status == WEIGHT_EXPORT_DELETE_CHECKING,),
                for_update=True,
            ):
                outbox.status = WEIGHT_EXPORT_DELETE_PENDING
                _reset_retry(outbox)
            await _resolve_alert_if_clear(session)
        await session.flush()
        return clean
