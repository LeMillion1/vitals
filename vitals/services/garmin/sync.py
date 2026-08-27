"""Garmin provider I/O orchestration without transaction ownership."""

from __future__ import annotations

from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle

import logging
import uuid
from datetime import date as date_type, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Severity, Source
from vitals.i18n import t
from vitals.integrations.garmin_client import (
    GarminAuthError,
    GarminLoginThrottled,
    GarminMFARequired,
)
from vitals.models.garmin import DOMAIN, GarminDaily
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
import vitals.services.garmin.alerts as garmin_alerts
import vitals.services.garmin.ingestion as ingestion
from vitals.services.garmin.errors import (
    GarminOwnershipAmbiguityError,
    GarminOwnershipConflictError,
    GarminOwnershipValidationError,
    GarminRawPayloadInvariantError,
)
from vitals.services.garmin.ownership import (
    _load_owned_garmin_connection,
    _lock_owned_garmin_scope,
    _require_legacy_adoption_subject,
    _row_scope_is_compatible,
)
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.weight import governance as weight_governance
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)


async def _enrich_activity_details(
    client: Any,
    activities: Sequence[dict],
) -> None:
    """Best-effort activity detail fetch before raw-first persistence."""

    fetch = getattr(client, "fetch_activity_details", None)
    if not callable(fetch):
        return
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        activity_id = activity.get("activityId") or activity.get("activityid")
        if activity_id is None:
            continue
        try:
            activity["_details"] = await fetch(activity_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Garmin activity-detail fetch failed for %s: %s",
                activity_id,
                exc,
            )


async def sync_owned(
    session: AsyncSession,
    client: Any,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    days: int = 2,
    on_date: Optional[date_type] = None,
) -> dict:
    """Pull and persist one explicit subject/account Garmin scope."""

    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise GarminOwnershipValidationError("days must be a positive integer")
    await _load_owned_garmin_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    alert_context = garmin_alerts._owned_provider_alert_context(
        identity=identity,
        integration_connection_id=integration_connection_id,
    )

    today = on_date or now_local().date()
    start = today - timedelta(days=days - 1)
    summary = {"days": 0, "activities": 0, "error": None}
    daily_payloads: list[tuple[date_type, dict]] = []
    activities: Optional[Sequence[dict]] = None
    auth_error: Optional[GarminAuthError] = None

    # All vendor I/O completes before mutation/row locks. The initial ownership
    # read fails closed without holding a lock over network latency.
    try:
        for offset in range(days):
            day = start + timedelta(days=offset)
            daily_payloads.append((day, await client.fetch_daily(day)))
        activities = await client.fetch_activities(start, today)
        await _enrich_activity_details(client, activities)
    except GarminAuthError as exc:
        auth_error = exc

    # Shared lock order is governance -> subject -> connection.
    await acquire_identity_governance_lock(session)
    for day, raw in daily_payloads:
        await ingestion.ingest_owned_daily(
            session,
            day,
            raw,
            identity=identity,
            integration_connection_id=integration_connection_id,
        )
        summary["days"] += 1
    if activities is not None:
        summary["activities"] = await ingestion.ingest_owned_activities(
            session,
            activities,
            identity=identity,
            integration_connection_id=integration_connection_id,
        )

    if auth_error is None:
        await alerts_service_lifecycle.resolve_scoped_by_key(
            session,
            context=alert_context,
            alert_key=garmin_alerts.AUTH_ALERT_KEY,
            legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
        )
    else:
        if isinstance(auth_error, GarminMFARequired):
            summary["error"], message = "mfa", t("alert.garmin_mfa")
        elif isinstance(auth_error, GarminLoginThrottled):
            summary["error"], message = "throttled", t(
                "alert.garmin_login_throttled"
            )
        else:
            summary["error"] = "auth"
            message = t("alert.garmin_auth_fail", error=str(auth_error))
        await alerts_service_lifecycle.raise_scoped_alert(
            session,
            context=alert_context,
            domain=Domain.GARMIN,
            severity=Severity.WARN,
            message=message,
            alert_key=garmin_alerts.AUTH_ALERT_KEY,
            legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
        )

    await garmin_alerts._refresh_owned_token_cache_alert(
        session,
        client,
        context=alert_context,
    )
    return summary


async def _pulse_base_payload(
    session: AsyncSession,
    *,
    on_date: date_type,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> dict:
    """Read the latest compatible full bundle after pulse network I/O."""

    daily_rows = list(
        await session.scalars(
            select(GarminDaily)
            .where(
                GarminDaily.date == on_date,
                or_(
                    GarminDaily.integration_connection_id
                    == integration_connection_id,
                    GarminDaily.integration_connection_id.is_(None),
                ),
            )
            .limit(2)
        )
    )
    if len(daily_rows) > 1:
        raise GarminOwnershipAmbiguityError(
            f"multiple Garmin rows match scoped key daily:{on_date}"
        )
    if not daily_rows:
        return {}
    daily = daily_rows[0]
    if not _row_scope_is_compatible(
        daily,
        identity=identity,
        integration_connection_id=integration_connection_id,
    ):
        raise GarminOwnershipConflictError(
            "Garmin pulse day belongs to another ownership scope"
        )
    if daily.subject_id is None and daily.integration_connection_id is None:
        await _require_legacy_adoption_subject(
            session,
            subject_id=identity.subject_id,
        )
    if daily.raw_payload_id is None:
        raise GarminRawPayloadInvariantError(
            "existing Garmin pulse day has no raw payload"
        )
    with session.no_autoflush:
        raw_row = await session.scalar(
            select(RawPayload).where(RawPayload.id == daily.raw_payload_id)
        )
    if raw_row is None:
        raise GarminRawPayloadInvariantError(
            "existing Garmin pulse raw payload no longer exists"
        )
    if raw_row.subject_id not in {None, identity.subject_id}:
        raise GarminRawPayloadInvariantError(
            "Garmin pulse raw payload belongs to another subject"
        )
    if raw_row.integration_connection_id not in {
        None,
        integration_connection_id,
    }:
        raise GarminRawPayloadInvariantError(
            "Garmin pulse raw payload belongs to another connection"
        )
    if raw_row.subject_id is None and raw_row.integration_connection_id is None:
        await _require_legacy_adoption_subject(
            session,
            subject_id=identity.subject_id,
        )
    expected_external_id = (
        f"hae:{on_date.isoformat()}"
        if raw_row.source == Source.HEALTH_AUTO_EXPORT.value
        else f"daily:{on_date.isoformat()}"
    )
    if (
        raw_row.domain != DOMAIN
        or raw_row.source
        not in {Source.GARMIN_API.value, Source.HEALTH_AUTO_EXPORT.value}
        or raw_row.external_id != expected_external_id
        or raw_row.file_asset_id is not None
        or not isinstance(raw_row.payload, dict)
    ):
        raise GarminRawPayloadInvariantError(
            "existing Garmin pulse raw payload has incompatible provenance"
        )
    return dict(raw_row.payload)


async def pulse_owned(
    session: AsyncSession,
    client: Any,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    on_date: Optional[date_type] = None,
) -> dict:
    """Refresh today's summary inside one owned daily/raw scope."""

    await _load_owned_garmin_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    day = on_date or now_local().date()
    out: dict = {"steps": None, "error": None}
    try:
        fresh = await client.fetch_summary(day)
    except GarminAuthError as exc:
        logger.warning("Garmin pulse skipped: %s", exc)
        out["error"] = (
            "throttled" if isinstance(exc, GarminLoginThrottled) else "auth"
        )
        return out
    if not fresh:
        out["error"] = "empty"
        return out

    from vitals.services.garmin_weight.contracts import GarminWeightExportContext

    prepared_weight_write = await weight_governance.prepare_weight_write(
        session,
        context=ingestion._owned_weight_write_context(
            identity=identity,
            on_date=day,
        ),
        garmin_weight_export_context=(
            GarminWeightExportContext(
                identity=identity,
                integration_connection_id=integration_connection_id,
                legacy_bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
            )
        ),
    )
    await _lock_owned_garmin_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    raw = await _pulse_base_payload(
        session,
        on_date=day,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    raw["summary"] = fresh
    row = await ingestion.ingest_owned_daily(
        session,
        day,
        raw,
        identity=identity,
        integration_connection_id=integration_connection_id,
        prepared_weight_write=prepared_weight_write,
    )
    out["steps"] = row.steps
    return out
