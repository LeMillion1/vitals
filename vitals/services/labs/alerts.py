"""Derived alerts and retest deferrals for the Labs bounded context."""
from __future__ import annotations

from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle

import uuid
from datetime import date as date_type, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Severity
from vitals.i18n import t
from vitals.models.labs import LabMarker, LabResult
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from .flags import is_critical as _is_critical
from .flags import is_out_of_range
from .markers import _marker_for_update, list_markers
from .results import (
    _require_evaluation_date,
    _require_scoped_prepared_write,
    _subject_scope,
    latest_per_marker,
)
from vitals.utils.timeutils import today_local

OUT_OF_RANGE_KEY = "labs.out_of_range"
RETEST_DUE_KEY = "labs.retest_due"

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



async def defer_retest(
    session: AsyncSession,
    marker: str,
    *,
    until: date_type,
    note: Optional[str] = None,
    subject_id: uuid.UUID,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[LabMarker]:
    """Pause the overdue-retest alert for a marker until ``until``."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if identity is None or identity.actor_user_id is None:
        raise engine.ConflictPreparedWriteError(
            "lab retest deferral requires an active human actor"
        )
    if subject_id is not None and subject_id != identity.subject_id:
        raise engine.ConflictPreparedWriteError(
            "subject_id does not match prepared lab write identity"
        )
    subject_id = identity.subject_id
    row = await _marker_for_update(
        session,
        marker,
        subject_id=subject_id,
    )
    if row is None:
        return None
    row.defer_until = until
    if note is not None:
        row.note = note
    await session.flush()
    await alerts_service_lifecycle.resolve_scoped_superseded(
        session,
        context=alerts_service_contracts.HealthAlertContext(identity),
        alert_key=RETEST_DUE_KEY,
        marker=row.name,
        keep_entity=None,
        legacy_bridge=_alert_bridge(context),
    )
    return row


# ── Results ───────────────────────────────────────────────────────────────────

async def refresh_alerts(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    subject_id: uuid.UUID,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> None:
    """Raise/clear out-of-range + overdue-retest alerts from the latest values.
    Idempotent — safe on every dashboard load / scheduler tick. Each alert is
    bound to the specific LabResult row that triggered it (``entity_ref =
    f"{marker}:{result_id}"``), so a dismissal sticks forever for that row —
    only a new result for the marker can raise it again."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if on_date is not None:
        _require_evaluation_date(context, on_date)
    if subject_id is not None and subject_id != identity.subject_id:
        raise engine.ConflictPreparedWriteError(
            "subject_id does not match prepared lab write identity"
        )
    subject_id = identity.subject_id
    on_date = context.evaluation_date
    # Derived health alerts are system reconciliations even when a human
    # write caused the refresh. Lock facts/catalog before alert-key locks.
    result_scope = _subject_scope(
        LabResult,
        subject_id,
    )
    marker_scope = _subject_scope(
        LabMarker,
        subject_id,
    )
    list(
        await session.scalars(
            select(LabResult)
            .where(result_scope)
            .order_by(LabResult.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    locked_markers = list(
        await session.scalars(
            select(LabMarker)
            .where(marker_scope)
            .order_by(LabMarker.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    today = on_date or today_local()
    latest = await latest_per_marker(
        session,
        end=today,
        subject_id=subject_id,
    )
    markers = {
        m.normalized_name: m
        for m in await list_markers(
            session,
            subject_id=subject_id,
        )
    }
    alias_names_by_key: dict[str, set[str]] = {}
    for marker_row in locked_markers:
        alias_names_by_key.setdefault(marker_row.normalized_name, set()).add(
            marker_row.name
        )

    latest_by_name = {row.marker_key: row for row in latest}
    names = sorted(set(markers) | set(latest_by_name))
    alert_context = _system_alert_context(context)
    bridge = _alert_bridge(context)

    async def resolve_superseded(key: str, marker: str, keep: str | None) -> None:
        await alerts_service_lifecycle.resolve_scoped_superseded(
            session,
            context=alert_context,
            alert_key=key,
            marker=marker,
            keep_entity=keep,
            legacy_bridge=bridge,
        )

    async def was_dismissed(key: str, entity: str) -> bool:
        return await alerts_service_lifecycle.was_scoped_ever_dismissed(
            session,
            context=alert_context,
            alert_key=key,
            entity_ref=entity,
            legacy_bridge=bridge,
        )

    async def resolve_current(key: str, entity: str) -> None:
        await alerts_service_lifecycle.resolve_scoped_by_key(
            session,
            context=alert_context,
            alert_key=key,
            entity_ref=entity,
            legacy_bridge=bridge,
        )

    async def raise_derived(
        *, key: str, entity: str, severity: Severity, message: str
    ) -> None:
        await alerts_service_lifecycle.raise_scoped_alert(
            session,
            context=alert_context,
            domain=Domain.LABS,
            severity=severity,
            message=message,
            alert_key=key,
            entity_ref=entity,
            legacy_bridge=bridge,
        )

    for marker_key in names:
        r = latest_by_name.get(marker_key)
        marker_row = markers.get(marker_key)
        marker_name = marker_row.name if marker_row is not None else r.marker
        entity = f"{marker_name}:{r.id}" if r is not None else None
        for alias_name in alias_names_by_key.get(marker_key, {marker_name}):
            await resolve_superseded(OUT_OF_RANGE_KEY, alias_name, entity)
        if r is not None and is_out_of_range(r.flag):
            if not await was_dismissed(OUT_OF_RANGE_KEY, entity):
                tier = marker_row.tier if marker_row is not None else 2
                critical = _is_critical(r.flag) or tier == 1
                severity = Severity.WARN if critical else Severity.INFO
                await raise_derived(
                    key=OUT_OF_RANGE_KEY,
                    entity=entity,
                    severity=severity,
                    message=t(
                        "alert.lab_out_of_range",
                        marker=r.marker,
                        value=r.value,
                        unit=(" " + r.unit) if r.unit else "",
                        flag=t(f"enum.flag.{r.flag}"),
                    ),
                )
        elif entity is not None:
            await resolve_current(OUT_OF_RANGE_KEY, entity)

        has_schedule = (
            r is not None
            and marker_row is not None
            and marker_row.retest_interval_days is not None
        )
        for alias_name in alias_names_by_key.get(marker_key, {marker_name}):
            await resolve_superseded(
                RETEST_DUE_KEY,
                alias_name,
                entity if has_schedule else None,
            )
        if not has_schedule:
            continue
        assert r is not None and marker_row is not None and entity is not None
        due = r.date + timedelta(days=marker_row.retest_interval_days)
        deferred = marker_row.defer_until is not None and marker_row.defer_until >= today
        if today > due and not deferred:
            if not await was_dismissed(RETEST_DUE_KEY, entity):
                await raise_derived(
                    key=RETEST_DUE_KEY,
                    entity=entity,
                    severity=Severity.INFO,
                    message=t("alert.lab_retest", marker=r.marker, date=r.date),
                )
        else:
            await resolve_current(RETEST_DUE_KEY, entity)
