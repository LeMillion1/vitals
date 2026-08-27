"""Local-only outbox projection and active-weight reconciliation."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationProvider,
    Severity,
)
from vitals.i18n import t
from vitals.models.garmin import (
    DOMAIN,
    WEIGHT_EXPORT_CONFLICT,
    WEIGHT_EXPORT_DELETED,
    WEIGHT_EXPORT_DELETE_FAILED,
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_MATCHED,
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_SENT,
    WEIGHT_EXPORT_SKIPPED,
    WEIGHT_EXPORT_UNVERIFIED,
    GarminWeightExport,
)
from vitals.models.weight import WeightLog
from vitals.models.tenancy import IntegrationConnection
from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import legacy as alerts_service_legacy
from vitals.services.alerts import lifecycle as alerts_service_lifecycle
from vitals.services.conflicts import engine
from vitals.services.garmin_weight.contracts import (
    ALERT_ENTITY,
    ALERT_KEY,
    ELIGIBLE_SOURCES,
    DELETE_STATUSES,
    ISSUE_STATUSES,
    SUPERSEDEABLE_STATUSES,
    GarminWeightExportOwnershipError,
    PreparedGarminWeightExport,
    _require_prepared_export,
    _same_local_weight,
)
from vitals.services.garmin_weight.outbox import (
    _SCOPED_EXPORT,
    _acquire_operation_lock,
    _activate_scoped_export,
    _active_export_context,
    _assert_outbox_scope_integrity,
    _outbox_visible_scope,
    _scoped_outbox_query,
    _scoped_rows,
    _validate_scoped_outbox_row,
)
from vitals.services.proactive.preferences import legacy as preference_legacy
from vitals.services.proactive.preferences import queries as preference_queries
from vitals.utils.timeutils import now_local


async def handle_active_weight_changed(
    session: AsyncSession, *, now: Optional[datetime] = None
) -> None:
    """Invalidate/project the outbox in the same transaction as a local save.

    This is local-only. It closes the window where an exporter could finish a
    stale preflight after a newer active weight committed without touching the
    outbox lease.
    """
    from vitals.services.garmin_weight.settings import is_enabled

    await _acquire_operation_lock(session)
    if await is_enabled(session):
        await reconcile_latest(session, now=now)


async def handle_active_weight_changed_scoped(
    session: AsyncSession,
    *,
    prepared: PreparedGarminWeightExport,
    now: Optional[datetime] = None,
) -> None:
    _require_prepared_export(session, prepared, historical_ok=False)
    with _activate_scoped_export(prepared):
        await handle_active_weight_changed(session, now=now)


def _measurement_time(on_date: date_type, now: datetime) -> datetime:
    # Date-only local records have no honest historical time. Noon avoids moving
    # the calendar date across practically every timezone boundary.
    return now if on_date == now.date() else datetime.combine(on_date, time(hour=12))


async def _skip_actionable_except(session: AsyncSession, *, keep_date: Optional[date_type]) -> None:
    if _active_export_context() is not None:
        rows = await _scoped_rows(
            session,
            filters=(GarminWeightExport.status.in_(SUPERSEDEABLE_STATUSES),),
            for_update=True,
        )
    else:
        result = await session.execute(
            select(GarminWeightExport).where(GarminWeightExport.status.in_(SUPERSEDEABLE_STATUSES))
        )
        rows = list(result.scalars().all())
    for row in rows:
        if keep_date is not None and row.date == keep_date:
            continue
        row.status = WEIGHT_EXPORT_SKIPPED
        row.attempts = 0
        row.last_attempt_at = None
        row.next_attempt_at = None
        row.last_error = None


async def _watermark_date(
    session: AsyncSession, *, through_date: Optional[date_type] = None
) -> Optional[date_type]:
    """Newest date ever observed by the append-only outbox."""
    statement = select(func.max(GarminWeightExport.date))
    context = _active_export_context()
    if context is not None:
        filters = (GarminWeightExport.date <= through_date,) if through_date is not None else ()
        await _assert_outbox_scope_integrity(
            session,
            context,
            filters=filters,
        )
        statement = statement.where(_outbox_visible_scope(context))
    if through_date is not None:
        statement = statement.where(GarminWeightExport.date <= through_date)
    return (await session.execute(statement)).scalar_one_or_none()


async def _ensure_outbox_row(
    session: AsyncSession,
    *,
    on_date: date_type,
    weight_log_id: Optional[int],
    weight_kg: float,
    measured_at: datetime,
    status: str,
    requested_by_user_id: uuid.UUID | None = None,
) -> GarminWeightExport:
    """Insert one date intent without racing scheduler and manual reconciliation."""
    context = _active_export_context()
    if context is not None:
        prepared = _SCOPED_EXPORT.get()
        assert prepared is not None
        _require_prepared_export(session, prepared, historical_ok=False)
        existing = await session.scalar(
            _scoped_outbox_query(on_date, context=context)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            exact_or_legacy = (
                existing.subject_id == context.identity.subject_id
                and existing.integration_connection_id == context.integration_connection_id
            ) or (
                existing.subject_id is None
                and existing.integration_connection_id is None
                and existing.requested_by_user_id is None
                and context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED
            )
            if not exact_or_legacy:
                raise GarminWeightExportOwnershipError(
                    "the outbox row for this date cannot be adopted in this scope"
                )
            await _validate_scoped_outbox_row(
                session,
                existing,
                context,
                adopt_legacy=True,
            )
            return existing
        row = GarminWeightExport(
            subject_id=context.identity.subject_id,
            integration_connection_id=context.integration_connection_id,
            requested_by_user_id=requested_by_user_id,
            date=on_date,
            weight_log_id=weight_log_id,
            weight_kg=weight_kg,
            measured_at=measured_at,
            status=status,
        )
        session.add(row)
        await session.flush()
        await _validate_scoped_outbox_row(
            session,
            row,
            context,
            adopt_legacy=False,
        )
        return row
    # No export context means no usable Garmin account, and an outbox row is an
    # intent to send a weight *to* one. The bridge that used to run here wrote a
    # row addressed to nowhere, owned by nobody, keyed on a bare date that the
    # scoped key no longer treats as unique.
    raise GarminWeightExportOwnershipError(
        "a Garmin weight export needs the destination account it is bound for"
    )


def _reset_retry(row: GarminWeightExport) -> None:
    row.attempts = 0
    row.last_attempt_at = None
    row.next_attempt_at = None
    row.last_error = None


def _normalize_legacy_status(row: GarminWeightExport) -> None:
    """Upgrade the first implementation's overloaded ``sent`` state in place."""
    if row.status != WEIGHT_EXPORT_SENT or row.remote_owned:
        return
    row.status = (
        WEIGHT_EXPORT_MATCHED if row.remote_sample_pk is not None else WEIGHT_EXPORT_UNVERIFIED
    )
    row.next_attempt_at = None
    if row.status == WEIGHT_EXPORT_UNVERIFIED and not row.last_error:
        row.last_error = "Garmin ownership could not be established for the previous export"


async def reconcile_latest(
    session: AsyncSession,
    *,
    now: Optional[datetime] = None,
    max_age_days: Optional[int] = None,
) -> Optional[GarminWeightExport]:
    """Project only the latest fresh direct measurement into the outbox."""
    clock = now or now_local()
    context = _active_export_context()
    if context is None:
        # Reconciliation is a hook as often as it is an entry point: ``log_weight``
        # and the delete hook call it from the middle of a local write. Prove the
        # legacy root inside whatever transaction that caller owns, and prove it
        # before the outbox advisory below so the canonical order holds.
        settings = await preference_legacy.get_pre_identity_legacy_prefs_in_transaction(session)
        if max_age_days is None:
            max_age_days = settings["garmin_weight_max_age_days"]
    await _acquire_operation_lock(session)
    if max_age_days is None:
        if context is not None:
            policy = await preference_queries.get_garmin_policy(
                session,
                subject_id=context.identity.subject_id,
                integration_connection_id=context.integration_connection_id,
            )
            max_age_days = policy.weight_max_age_days
    if context is None:
        result = await session.execute(
            select(WeightLog)
            .where(
                WeightLog.superseded.is_(False),
                WeightLog.date <= clock.date(),
            )
            .order_by(WeightLog.date.desc(), WeightLog.id.desc())
            .execution_options(populate_existing=True)
            .limit(1)
        )
        local = result.scalar_one_or_none()
    else:
        from vitals.services.weight import logs as weight_logs

        rows = await weight_logs.list_active_weights(
            session,
            end=clock.date(),
            subject_id=context.identity.subject_id,
        )
        local = max(rows, key=lambda row: (row.date, row.id), default=None)
    requested_by_user_id = (
        local.actor_user_id
        if context is None and local is not None
        else context.identity.actor_user_id
        if context is not None
        else None
    )
    watermark = await _watermark_date(session, through_date=clock.date())

    # Once a newer local fact has been observed, deleting it must never expose an
    # older measurement as a new export intent. Every observed date is retained
    # as a terminal outbox row, making MAX(date) an append-only, race-safe cursor.
    if local is not None and watermark is not None and local.date < watermark:
        await _skip_actionable_except(session, keep_date=None)
        await _resolve_alert_if_clear(session)
        await session.flush()
        return None

    cutoff = clock.date() - timedelta(days=max_age_days)
    # Decide eligibility *after* finding the latest active fact. If today's
    # latest value came from Garmin, exporting an older manual row would be a
    # disguised history backfill and could distort Garmin's daily average.
    eligible = not (local is None or local.date < cutoff or local.source not in ELIGIBLE_SOURCES)
    if not eligible:
        if local is not None and (watermark is None or local.date >= watermark):
            await _ensure_outbox_row(
                session,
                on_date=local.date,
                weight_log_id=local.id,
                weight_kg=local.weight_kg,
                measured_at=_measurement_time(local.date, clock),
                status=WEIGHT_EXPORT_SKIPPED,
                requested_by_user_id=requested_by_user_id,
            )
        await _skip_actionable_except(session, keep_date=None)
        await _resolve_alert_if_clear(session)
        await session.flush()
        return None

    outbox = await _ensure_outbox_row(
        session,
        on_date=local.date,
        weight_log_id=local.id,
        weight_kg=local.weight_kg,
        measured_at=_measurement_time(local.date, clock),
        status=WEIGHT_EXPORT_PENDING,
        requested_by_user_id=requested_by_user_id,
    )
    if outbox is not None:
        # ``sent`` used to include both an equal external record and a POST whose
        # identity could not be read back. Neither is an owned successful send.
        _normalize_legacy_status(outbox)
        weight_changed = not _same_local_weight(outbox.weight_kg, local.weight_kg)
        outbox.weight_log_id = local.id
        if weight_changed:
            outbox.weight_kg = local.weight_kg
            # A POST whose identity is still unknown must be reconciled before a
            # correction can delete or add anything. Keep that state and its
            # measured_at/remote_weight_kg dispatch identity. Existing rows retain
            # measured_at in every other state too, so a later correction POST
            # advances rather than accidentally reuses its prior marker.
            if outbox.status == WEIGHT_EXPORT_UNVERIFIED:
                outbox.attempts = 0
                outbox.last_attempt_at = None
                outbox.next_attempt_at = None
                outbox.last_error = (
                    "The previous Garmin POST is still unverified; the local "
                    "correction is waiting for safe reconciliation"
                )
            else:
                outbox.status = WEIGHT_EXPORT_PENDING
                _reset_retry(outbox)
            outbox.exported_at = None
            # Keep remote_*: an owned previous value is what makes a correction
            # replaceable without touching unrelated Garmin data.
        elif outbox.status in (
            WEIGHT_EXPORT_SKIPPED,
            WEIGHT_EXPORT_DELETED,
            WEIGHT_EXPORT_DELETE_PENDING,
            WEIGHT_EXPORT_DELETE_FAILED,
        ):
            outbox.status = WEIGHT_EXPORT_PENDING
            _reset_retry(outbox)

    await _skip_actionable_except(session, keep_date=local.date)
    await _resolve_alert_if_clear(session)
    await session.flush()
    return outbox


async def reconcile_latest_scoped(
    session: AsyncSession,
    *,
    prepared: PreparedGarminWeightExport,
    now: Optional[datetime] = None,
    max_age_days: Optional[int] = None,
) -> Optional[GarminWeightExport]:
    _require_prepared_export(session, prepared, historical_ok=False)
    with _activate_scoped_export(prepared):
        return await reconcile_latest(
            session,
            now=now,
            max_age_days=max_age_days,
        )


async def handle_active_weight_deleted(
    session: AsyncSession,
    *,
    deleted_id: int,
    on_date: date_type,
    deleted_weight_kg: float,
    replacement: Optional[WeightLog],
    now: Optional[datetime] = None,
) -> None:
    from vitals.services.garmin_weight.settings import is_enabled

    """Update the local outbox after an active weight is deleted; never networks."""
    await _acquire_operation_lock(session)
    clock = now or now_local()
    context = _active_export_context()
    result = await session.execute(
        _scoped_outbox_query(on_date, context=context)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    outbox = result.scalar_one_or_none()
    if outbox is not None and outbox.weight_log_id == deleted_id:
        # The fact this row cites is gone. ``ON DELETE SET NULL`` says so on
        # PostgreSQL, but the caller already knows which id it deleted, so say
        # it here too rather than depending on the dialect's enforcement.
        outbox.weight_log_id = None
        await session.flush()
    if outbox is not None and context is not None:
        exact_or_legacy = (
            outbox.subject_id == context.identity.subject_id
            and outbox.integration_connection_id == context.integration_connection_id
        ) or (
            outbox.subject_id is None
            and outbox.integration_connection_id is None
            and outbox.requested_by_user_id is None
            and context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED
        )
        if not exact_or_legacy:
            raise GarminWeightExportOwnershipError(
                "the outbox row for this date cannot be adopted in this scope"
            )
        await _validate_scoped_outbox_row(
            session,
            outbox,
            context,
            adopt_legacy=True,
        )
    if outbox is None:
        # A value created and deleted between scheduler ticks still advances the
        # append-only watermark while export is enabled. If another newer outbox
        # date already exists, that row already provides the required protection.
        if on_date > clock.date():
            await session.flush()
            return
        watermark = await _watermark_date(session, through_date=clock.date())
        if not await is_enabled(session) or (watermark is not None and on_date < watermark):
            await session.flush()
            return
        replacement_is_eligible = replacement is not None and replacement.source in ELIGIBLE_SOURCES
        outbox = await _ensure_outbox_row(
            session,
            on_date=on_date,
            weight_log_id=replacement.id if replacement_is_eligible else None,
            weight_kg=(replacement.weight_kg if replacement_is_eligible else deleted_weight_kg),
            measured_at=_measurement_time(on_date, clock),
            status=(WEIGHT_EXPORT_PENDING if replacement_is_eligible else WEIGHT_EXPORT_DELETED),
            requested_by_user_id=(
                context.identity.actor_user_id
                if context is not None
                else replacement.actor_user_id
                if replacement is not None
                else None
            ),
        )
    elif outbox.weight_log_id not in (None, deleted_id):
        await session.flush()
        return
    _normalize_legacy_status(outbox)

    if replacement is not None and replacement.source in ELIGIBLE_SOURCES:
        changed = not _same_local_weight(outbox.weight_kg, replacement.weight_kg)
        outbox.weight_log_id = replacement.id
        if changed:
            outbox.weight_kg = replacement.weight_kg
            if outbox.status == WEIGHT_EXPORT_UNVERIFIED:
                outbox.attempts = 0
                outbox.last_attempt_at = None
                outbox.next_attempt_at = None
                outbox.last_error = (
                    "The previous Garmin POST is still unverified; the replacement "
                    "weight is waiting for safe reconciliation"
                )
            else:
                outbox.status = WEIGHT_EXPORT_PENDING
                _reset_retry(outbox)
            outbox.exported_at = None
        elif outbox.status in (
            WEIGHT_EXPORT_SKIPPED,
            WEIGHT_EXPORT_DELETED,
            *DELETE_STATUSES,
        ):
            outbox.status = WEIGHT_EXPORT_PENDING
            _reset_retry(outbox)
        await _resolve_alert_if_clear(session)
        await session.flush()
        return

    outbox.weight_log_id = None
    if outbox.status == WEIGHT_EXPORT_UNVERIFIED:
        outbox.next_attempt_at = None
        outbox.last_error = (
            "The local weight was deleted while Garmin ownership is still unverified"
        )
    elif outbox.remote_owned and outbox.remote_sample_pk is not None:
        outbox.status = WEIGHT_EXPORT_DELETE_PENDING
        _reset_retry(outbox)
    else:
        _mark_deleted(outbox, now=clock)
    await _resolve_alert_if_clear(session)
    await session.flush()


async def handle_active_weight_deleted_scoped(
    session: AsyncSession,
    *,
    prepared: PreparedGarminWeightExport,
    deleted_id: int,
    on_date: date_type,
    deleted_weight_kg: float,
    replacement: Optional[WeightLog],
    now: Optional[datetime] = None,
) -> None:
    _require_prepared_export(session, prepared, historical_ok=False)
    with _activate_scoped_export(prepared):
        await handle_active_weight_deleted(
            session,
            deleted_id=deleted_id,
            on_date=on_date,
            deleted_weight_kg=deleted_weight_kg,
            replacement=replacement,
            now=now,
        )


def _mark_deleted(row: GarminWeightExport, *, now: datetime) -> None:
    row.status = WEIGHT_EXPORT_DELETED
    row.exported_at = now
    row.attempts = 0
    row.next_attempt_at = None
    row.last_error = None
    row.remote_sample_pk = None
    row.remote_weight_kg = None
    row.remote_owned = False
    row.dispatch_timestamp_ms = None


async def _raise_failure_alert(
    session: AsyncSession, *, row: GarminWeightExport, error: str
) -> None:
    message_key = {
        WEIGHT_EXPORT_CONFLICT: "alert.garmin_weight_conflict",
        WEIGHT_EXPORT_UNVERIFIED: "alert.garmin_weight_unverified",
        WEIGHT_EXPORT_DELETE_FAILED: "alert.garmin_weight_delete",
    }.get(row.status, "alert.garmin_weight_export")
    context = _active_export_context()
    message = t(
        message_key,
        date=row.date.isoformat(),
        error=error,
    )
    if context is None:
        await alerts_service_legacy.raise_alert(
            session,
            domain=DOMAIN,
            severity=Severity.WARN.value,
            message=message,
            alert_key=ALERT_KEY,
            entity_ref=ALERT_ENTITY,
        )
        return
    active_prepared = _SCOPED_EXPORT.get()
    if active_prepared is not None and active_prepared.historical:
        connection_status = await session.scalar(
            select(IntegrationConnection.status).where(
                IntegrationConnection.id == context.integration_connection_id
            )
        )
        if connection_status not in {
            IntegrationConnectionStatus.LEGACY.value,
            IntegrationConnectionStatus.ACTIVE.value,
        }:
            # The outcome is still durable on the outbox row, but a disabled or
            # retired provider cannot authorize a fresh provider alert write.
            return
    await alerts_service_lifecycle.raise_scoped_alert(
        session,
        context=alerts_service_contracts.ProviderAlertContext(
            identity=context.identity,
            provider=IntegrationProvider.GARMIN,
            integration_connection_id=context.integration_connection_id,
        ),
        domain=Domain.GARMIN,
        severity=Severity.WARN,
        message=message,
        alert_key=ALERT_KEY,
        entity_ref=ALERT_ENTITY,
        legacy_bridge=(
            alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED
            if context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED
            else alerts_service_contracts.LegacyAlertBridge.REJECT
        ),
    )


async def _resolve_alert_if_clear(session: AsyncSession) -> None:
    context = _active_export_context()
    if context is None:
        result = await session.execute(
            select(GarminWeightExport)
            .where(GarminWeightExport.status.in_(ISSUE_STATUSES))
            .order_by(GarminWeightExport.date.desc(), GarminWeightExport.id.desc())
        )
        issues = list(result.scalars().all())
    else:
        issues = await _scoped_rows(
            session,
            filters=(GarminWeightExport.status.in_(ISSUE_STATUSES),),
            for_update=True,
        )
    if not issues:
        if context is None:
            await alerts_service_legacy.resolve_by_key(
                session, alert_key=ALERT_KEY, entity_ref=ALERT_ENTITY
            )
        else:
            await alerts_service_lifecycle.resolve_scoped_by_key(
                session,
                context=alerts_service_contracts.ProviderAlertContext(
                    identity=context.identity,
                    provider=IntegrationProvider.GARMIN,
                    integration_connection_id=context.integration_connection_id,
                ),
                alert_key=ALERT_KEY,
                entity_ref=ALERT_ENTITY,
                legacy_bridge=(
                    alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED
                    if context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED
                    else alerts_service_contracts.LegacyAlertBridge.REJECT
                ),
            )
        return
    priority = {
        WEIGHT_EXPORT_DELETE_FAILED: 0,
        WEIGHT_EXPORT_UNVERIFIED: 1,
        WEIGHT_EXPORT_CONFLICT: 2,
        WEIGHT_EXPORT_FAILED: 3,
    }
    issue = min(
        issues,
        key=lambda row: (priority[row.status], -row.date.toordinal(), -row.id),
    )
    if not issue.last_error:
        issue.last_error = {
            WEIGHT_EXPORT_DELETE_FAILED: "The Vitals-owned Garmin weight could not be removed safely",
            WEIGHT_EXPORT_UNVERIFIED: "Garmin ownership could not be verified",
            WEIGHT_EXPORT_CONFLICT: "The Garmin day is unsafe to mutate automatically",
            WEIGHT_EXPORT_FAILED: "The Garmin weight operation failed without diagnostic details",
        }[issue.status]
    await _raise_failure_alert(session, row=issue, error=issue.last_error)
