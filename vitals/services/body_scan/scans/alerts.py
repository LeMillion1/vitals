"""Body-composition alert projection and lifecycle."""

from __future__ import annotations

import uuid
from datetime import date as date_type

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Severity
from vitals.i18n import t
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.ownership import WriteIdentity
from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import legacy as alerts_service_legacy
from vitals.services.alerts import lifecycle as alerts_service_lifecycle
from vitals.services.conflicts import engine
from vitals.services.weight.contracts import PreparedWeightWrite

from .contracts import (
    BodyScanOwnershipError,
    PHASE_ALERT_KEY,
    VISCERAL_ALERT_KEY,
    require_evaluation_date as _require_evaluation_date,
    require_scoped_prepared_write as _require_scoped_prepared_write,
)
from .queries import (
    _validate_persisted_scan,
    get_scan,
    latest_scan,
)

def _alert_bridge(
    context: engine.ConflictWriteContext,
) -> alerts_service_contracts.LegacyAlertBridge:
    if context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED:
        return alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED
    return alerts_service_contracts.LegacyAlertBridge.REJECT


def _system_alert_context(
    context: engine.ConflictWriteContext,
) -> alerts_service_contracts.HealthAlertContext:
    return alerts_service_contracts.HealthAlertContext(
        WriteIdentity(context.identity.subject_id, None)
    )

async def refresh_alerts(
    session: AsyncSession,
    *,
    on_date: date_type | None = None,
    subject_id: uuid.UUID,
    identity: WriteIdentity,
    prepared_weight_write: PreparedWeightWrite,
) -> None:
    """Raise/clear passive ``info`` alerts from the latest scan: visceral fat above
    its printed range, or phase angle below its printed range. Idempotent. Each
    alert is bound to the triggering scan's id, so a dismissal sticks forever
    for that scan — only a newer scan can raise it again."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    if context is not None:
        if on_date is not None:
            _require_evaluation_date(context, on_date)
        if subject_id is not None and subject_id != identity.subject_id:
            raise engine.ConflictPreparedWriteError(
                "subject_id does not match prepared body-scan identity"
            )
        subject_id = identity.subject_id

    scan = await latest_scan(
        session,
        subject_id=subject_id,
    )
    alert_context = _system_alert_context(context) if context is not None else None
    alert_bridge = _alert_bridge(context) if context is not None else None

    if context is not None and scan is not None:
        # Raw/F/parser-C validation and locks precede the scan and its children;
        # typed alerts acquire their natural-key locks only after this block.
        await _validate_persisted_scan(
            session,
            scan,
            subject_id=identity.subject_id,
            for_update=True,
        )
        await session.scalar(
            select(BodyScan)
            .where(BodyScan.id == scan.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        list(
            await session.scalars(
                select(BodyScanMetric)
                .where(BodyScanMetric.scan_id == scan.id)
                .order_by(BodyScanMetric.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        scan = await get_scan(
            session,
            scan.id,
            subject_id=identity.subject_id,
        )
        if scan is None:
            raise BodyScanOwnershipError(
                "body scan disappeared while acquiring alert locks"
            )

    if scan is None:
        if alert_context is None:
            await alerts_service_legacy.resolve_superseded(
                session, alert_key=VISCERAL_ALERT_KEY, keep_entity=None
            )
            await alerts_service_legacy.resolve_superseded(
                session, alert_key=PHASE_ALERT_KEY, keep_entity=None
            )
        else:
            assert alert_bridge is not None
            await alerts_service_lifecycle.resolve_scoped_superseded(
                session,
                context=alert_context,
                alert_key=VISCERAL_ALERT_KEY,
                keep_entity=None,
                legacy_bridge=alert_bridge,
            )
            await alerts_service_lifecycle.resolve_scoped_superseded(
                session,
                context=alert_context,
                alert_key=PHASE_ALERT_KEY,
                keep_entity=None,
                legacy_bridge=alert_bridge,
            )
        return

    entity = str(scan.id)
    if alert_context is None:
        await alerts_service_legacy.resolve_superseded(
            session, alert_key=VISCERAL_ALERT_KEY, keep_entity=entity
        )
        await alerts_service_legacy.resolve_superseded(
            session, alert_key=PHASE_ALERT_KEY, keep_entity=entity
        )
    else:
        assert alert_bridge is not None
        await alerts_service_lifecycle.resolve_scoped_superseded(
            session,
            context=alert_context,
            alert_key=VISCERAL_ALERT_KEY,
            keep_entity=entity,
            legacy_bridge=alert_bridge,
        )
        await alerts_service_lifecycle.resolve_scoped_superseded(
            session,
            context=alert_context,
            alert_key=PHASE_ALERT_KEY,
            keep_entity=entity,
            legacy_bridge=alert_bridge,
        )

    by_key = {m.metric_key: m for m in scan.metrics}

    vfa = by_key.get("visceral_fat_area") or by_key.get("visceral_fat_level")
    if vfa is not None and vfa.ref_high is not None and vfa.value > vfa.ref_high:
        dismissed = (
            await alerts_service_legacy._was_ever_dismissed(
                session, VISCERAL_ALERT_KEY, entity
            )
            if alert_context is None
            else await alerts_service_lifecycle.was_scoped_ever_dismissed(
                session,
                context=alert_context,
                alert_key=VISCERAL_ALERT_KEY,
                entity_ref=entity,
                legacy_bridge=alert_bridge,
            )
        )
        if not dismissed:
            message = t(
                "alert.body_visceral_high",
                value=vfa.value,
                unit=((" " + vfa.unit) if vfa.unit else ""),
            )
            if alert_context is None:
                await alerts_service_legacy.raise_alert(
                    session,
                    domain=Domain.BODY_COMPOSITION.value,
                    severity=Severity.INFO.value,
                    message=message,
                    alert_key=VISCERAL_ALERT_KEY,
                    entity_ref=entity,
                )
            else:
                await alerts_service_lifecycle.raise_scoped_alert(
                    session,
                    context=alert_context,
                    domain=Domain.BODY_COMPOSITION,
                    severity=Severity.INFO,
                    message=message,
                    alert_key=VISCERAL_ALERT_KEY,
                    entity_ref=entity,
                    legacy_bridge=alert_bridge,
                )
    else:
        if alert_context is None:
            await alerts_service_legacy.resolve_by_key(
                session, alert_key=VISCERAL_ALERT_KEY, entity_ref=entity
            )
        else:
            await alerts_service_lifecycle.resolve_scoped_by_key(
                session,
                context=alert_context,
                alert_key=VISCERAL_ALERT_KEY,
                entity_ref=entity,
                legacy_bridge=alert_bridge,
            )

    phase = by_key.get("phase_angle")
    if phase is not None and phase.ref_low is not None and phase.value < phase.ref_low:
        dismissed = (
            await alerts_service_legacy._was_ever_dismissed(session, PHASE_ALERT_KEY, entity)
            if alert_context is None
            else await alerts_service_lifecycle.was_scoped_ever_dismissed(
                session,
                context=alert_context,
                alert_key=PHASE_ALERT_KEY,
                entity_ref=entity,
                legacy_bridge=alert_bridge,
            )
        )
        if not dismissed:
            message = t("alert.body_phase_low", value=phase.value)
            if alert_context is None:
                await alerts_service_legacy.raise_alert(
                    session,
                    domain=Domain.BODY_COMPOSITION.value,
                    severity=Severity.INFO.value,
                    message=message,
                    alert_key=PHASE_ALERT_KEY,
                    entity_ref=entity,
                )
            else:
                await alerts_service_lifecycle.raise_scoped_alert(
                    session,
                    context=alert_context,
                    domain=Domain.BODY_COMPOSITION,
                    severity=Severity.INFO,
                    message=message,
                    alert_key=PHASE_ALERT_KEY,
                    entity_ref=entity,
                    legacy_bridge=alert_bridge,
                )
    else:
        if alert_context is None:
            await alerts_service_legacy.resolve_by_key(
                session, alert_key=PHASE_ALERT_KEY, entity_ref=entity
            )
        else:
            await alerts_service_lifecycle.resolve_scoped_by_key(
                session,
                context=alert_context,
                alert_key=PHASE_ALERT_KEY,
                entity_ref=entity,
                legacy_bridge=alert_bridge,
            )
