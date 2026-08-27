"""Raw-first Garmin normalization writes without transaction ownership."""
from __future__ import annotations

import uuid
from datetime import date as date_type, datetime
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.garmin import (
    DOMAIN,
    GarminActivity,
    GarminDaily,
    GarminIntraday,
)
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services import raw_payload_service
from vitals.services.conflicts import engine
from vitals.services.garmin.errors import (
    GarminOwnershipAmbiguityError,
    GarminOwnershipConflictError,
    GarminOwnershipValidationError,
    GarminRawPayloadInvariantError,
)
from vitals.services.garmin.normalization import (
    _activity_external_id,
    _dig,
    _extract_weight_kg,
    _intish,
    _intraday_series,
    _normalize_daily,
    _normalize_hr_zones,
    _normalize_splits,
    _num,
    _parse_activity_start,
    _parse_hae_date,
)
from vitals.services.garmin.ownership import (
    _adopt_owned_row,
    _load_owned_garmin_connection,
    _lock_owned_garmin_scope,
    _owned_single_row_candidate,
    _require_legacy_adoption_subject,
)
from vitals.services.garmin.raw_payloads import (
    _validate_intraday_raw_reference,
    _validate_owned_raw_row,
)
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.weight.contracts import PreparedWeightWrite
from vitals.services.weight import governance as weight_governance
from vitals.services.weight import writes as weight_writes
from vitals.utils.timeutils import now_local


def _owned_weight_write_context(
    *,
    identity: WriteIdentity,
    on_date: date_type,
) -> engine.ConflictWriteContext:
    return engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
    )


# Pure normalization lives in vitals.services.garmin.normalization.

async def ingest_owned_intraday(
    session: AsyncSession,
    on_date: date_type,
    series_type: str,
    points: Sequence[tuple[datetime, float]],
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    raw_payload_id: Optional[int] = None,
    source: str = Source.GARMIN_API.value,
) -> int:
    """Replace one subject+connection series without touching another scope.

    Nullable rows in a compatible legacy scope are adopted immediately before
    replacement so the delete itself remains exact ``S+C``. An empty series is
    still a no-op and cannot erase prior samples.
    """

    if not points:
        return 0
    if source != Source.GARMIN_API.value:
        raise GarminOwnershipValidationError(
            "owned intraday ingestion accepts Garmin API provenance only"
        )
    await _lock_owned_garmin_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    if raw_payload_id is not None:
        await _validate_intraday_raw_reference(
            session,
            raw_payload_id=raw_payload_id,
            on_date=on_date,
            identity=identity,
            integration_connection_id=integration_connection_id,
            source=source,
        )
    return await _replace_owned_intraday(
        session,
        on_date,
        series_type,
        points,
        identity=identity,
        integration_connection_id=integration_connection_id,
        raw_payload_id=raw_payload_id,
        source=source,
    )


async def _replace_owned_intraday(
    session: AsyncSession,
    on_date: date_type,
    series_type: str,
    points: Sequence[tuple[datetime, float]],
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    raw_payload_id: Optional[int],
    source: str,
) -> int:
    """Validated implementation shared with historical reparse."""

    if not points:
        return 0

    # SQL ``IN (value, NULL)`` never matches NULL, so spell nullable compatibility
    # explicitly while keeping foreign rows outside the query entirely.
    compatible_scope = and_(
        or_(
            GarminIntraday.subject_id == identity.subject_id,
            GarminIntraday.subject_id.is_(None),
        ),
        or_(
            GarminIntraday.integration_connection_id
            == integration_connection_id,
            GarminIntraday.integration_connection_id.is_(None),
        ),
    )
    existing = list(
        await session.scalars(
            select(GarminIntraday)
            .where(
                GarminIntraday.date == on_date,
                GarminIntraday.series_type == series_type,
                compatible_scope,
            )
            .with_for_update()
        )
    )
    scope_shapes = {
        (row.subject_id, row.integration_connection_id) for row in existing
    }
    if len(scope_shapes) > 1:
        raise GarminOwnershipAmbiguityError(
            "owned and legacy Garmin intraday scopes coexist for one series"
        )
    if scope_shapes == {(None, None)}:
        await _require_legacy_adoption_subject(
            session, subject_id=identity.subject_id
        )
    for row in existing:
        if row.subject_id is None:
            row.subject_id = identity.subject_id
        if row.integration_connection_id is None:
            row.integration_connection_id = integration_connection_id
    if existing:
        await session.flush()

    await session.execute(
        GarminIntraday.__table__.delete().where(
            GarminIntraday.subject_id == identity.subject_id,
            GarminIntraday.integration_connection_id
            == integration_connection_id,
            GarminIntraday.date == on_date,
            GarminIntraday.series_type == series_type,
        )
    )
    session.add_all(
        [
            GarminIntraday(
                subject_id=identity.subject_id,
                integration_connection_id=integration_connection_id,
                date=on_date,
                domain=DOMAIN,
                source=source,
                raw_payload_id=raw_payload_id,
                series_type=series_type,
                ts=ts,
                value=value,
            )
            for ts, value in points
        ]
    )
    await session.flush()
    return len(points)


# Normalization lives in vitals.services.garmin.normalization.

# ── Daily upsert ──────────────────────────────────────────────────────────────
# Read queries live in vitals.services.garmin.queries.

async def _apply_owned_daily_raw(
    session: AsyncSession,
    on_date: date_type,
    *,
    raw_row: RawPayload,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    source: str,
    candidate: GarminDaily | None,
    prepared_weight_write: PreparedWeightWrite | None,
) -> GarminDaily:
    """Apply one complete Garmin API bundle to its owned daily projection."""

    if source != Source.GARMIN_API.value:
        raise GarminRawPayloadInvariantError(
            "complete Garmin daily projection requires Garmin API provenance"
        )
    fields = _normalize_daily(raw_row.payload)
    row = candidate
    if row is None:
        row = GarminDaily(
            subject_id=identity.subject_id,
            actor_user_id=raw_row.actor_user_id,
            integration_connection_id=integration_connection_id,
            date=on_date,
            domain=DOMAIN,
        )
        session.add(row)
    else:
        _adopt_owned_row(
            row,
            identity=identity,
            integration_connection_id=integration_connection_id,
        )
    row.source = source
    row.raw_payload_id = raw_row.id
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()

    for series_type, points in _intraday_series(raw_row.payload).items():
        await _replace_owned_intraday(
            session,
            on_date,
            series_type,
            points,
            identity=identity,
            integration_connection_id=integration_connection_id,
            raw_payload_id=raw_row.id,
            source=source,
        )

    weight_kg = _extract_weight_kg(raw_row.payload)
    if weight_kg is not None:
        if prepared_weight_write is None:
            raise GarminOwnershipValidationError(
                "owned Garmin weight requires a prepared Weight capability"
            )
        weight_row = await weight_writes.log_weight(
            session,
            on_date=on_date,
            weight_kg=weight_kg,
            source=Source.GARMIN_API.value,
            raw_payload_id=raw_row.id,
            identity=identity,
            integration_connection_id=integration_connection_id,
            prepared_weight_write=prepared_weight_write,
            origin_actor_user_id=raw_row.actor_user_id,
        )
        if weight_row.subject_id not in {None, identity.subject_id}:
            raise GarminOwnershipConflictError(
                "Garmin weight fact belongs to another subject"
            )
        if weight_row.integration_connection_id not in {
            None,
            integration_connection_id,
        }:
            raise GarminOwnershipConflictError(
                "Garmin weight fact belongs to another connection"
            )
        if weight_row.raw_payload_id not in {None, raw_row.id}:
            raise GarminRawPayloadInvariantError(
                "Garmin weight fact references a different raw payload"
            )
    raw_row.processed_at = now_local()
    return row


async def _apply_owned_hae_raw(
    session: AsyncSession,
    on_date: date_type,
    *,
    raw_row: RawPayload,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    candidate: GarminDaily | None,
) -> GarminDaily:
    """Apply only fields present in one owned Health Auto Export payload."""

    raw_fields = raw_row.payload.get("metrics")
    allowed_fields = {column for column, _convert in _HAE_METRIC_MAP.values()}
    if not isinstance(raw_fields, dict) or any(
        key not in allowed_fields for key in raw_fields
    ):
        raise GarminRawPayloadInvariantError(
            "Health Auto Export raw payload has invalid normalized metrics"
        )

    row = candidate
    if row is None:
        row = GarminDaily(
            subject_id=identity.subject_id,
            actor_user_id=raw_row.actor_user_id,
            integration_connection_id=integration_connection_id,
            date=on_date,
            domain=DOMAIN,
        )
        session.add(row)
    else:
        _adopt_owned_row(
            row,
            identity=identity,
            integration_connection_id=integration_connection_id,
        )
    row.source = Source.HEALTH_AUTO_EXPORT.value
    row.raw_payload_id = raw_row.id
    for key, value in raw_fields.items():
        setattr(row, key, value)
    await session.flush()
    raw_row.processed_at = now_local()
    return row


async def ingest_owned_daily(
    session: AsyncSession,
    on_date: date_type,
    raw: dict,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    source: str = Source.GARMIN_API.value,
    prepared_weight_write: PreparedWeightWrite | None = None,
) -> GarminDaily:
    """Owned raw-first daily ingest with scoped normalized replacement."""

    if not isinstance(on_date, date_type) or isinstance(on_date, datetime):
        raise GarminOwnershipValidationError("on_date must be a date")
    if not isinstance(raw, dict):
        raise GarminOwnershipValidationError("raw must be a JSON object")
    if source != Source.GARMIN_API.value:
        raise GarminOwnershipValidationError(
            "owned daily ingestion accepts Garmin API bundles only"
        )
    await _load_owned_garmin_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )

    # Establish governance -> active-weight advisory -> subject/user before the
    # provider's S/C/raw locks. A supplied capability comes from an outer caller
    # (notably pulse) that already established the same order.
    from vitals.services.garmin_weight import contracts as garmin_weight_contracts
    from vitals.services.garmin_weight import outbox as garmin_weight_outbox

    weight_kg = _extract_weight_kg(raw)
    if prepared_weight_write is None and weight_kg is not None:
        await acquire_identity_governance_lock(session)
        await _require_legacy_adoption_subject(
            session,
            subject_id=identity.subject_id,
        )
        prepared_weight_write = await weight_governance.prepare_weight_write(
            session,
            context=_owned_weight_write_context(
                identity=identity,
                on_date=on_date,
            ),
            garmin_weight_export_context=(
                garmin_weight_contracts.GarminWeightExportContext(
                    identity=identity,
                    integration_connection_id=integration_connection_id,
                    legacy_bridge=(
                        engine.LegacyConflictBridge.FULLY_UNOWNED
                    ),
                )
            ),
        )
    elif prepared_weight_write is not None:
        prepared_context = weight_governance.require_prepared_weight_identity(
            session,
            prepared=prepared_weight_write,
            identity=identity,
        )
        if prepared_context.evaluation_date != on_date:
            raise GarminOwnershipValidationError(
                "prepared Weight capability belongs to another date"
            )
        if (
            prepared_context.legacy_bridge
            is not engine.LegacyConflictBridge.FULLY_UNOWNED
        ):
            raise GarminOwnershipValidationError(
                "owned Garmin ingest requires a fully-unowned Weight bridge"
            )
        if (
            prepared_weight_write.garmin_weight_export is None
            or prepared_weight_write.garmin_weight_export.context.integration_connection_id
            != integration_connection_id
        ):
            raise GarminOwnershipValidationError(
                "owned Garmin ingest requires its prepared export destination"
            )
    else:
        await acquire_identity_governance_lock(session)
        await garmin_weight_outbox.lock_active_weight_change(session)

    await _lock_owned_garmin_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    candidate = await _owned_single_row_candidate(
        session,
        model=GarminDaily,
        natural_clause=GarminDaily.date == on_date,
        identity=identity,
        integration_connection_id=integration_connection_id,
        key_label=f"daily:{on_date.isoformat()}",
    )
    external_id = f"daily:{on_date.isoformat()}"
    raw_row = await raw_payload_service.upsert_owned_raw_payload(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        domain=DOMAIN,
        source=source,
        external_id=external_id,
        payload=raw,
    )
    _validate_owned_raw_row(
        raw_row,
        identity=identity,
        integration_connection_id=integration_connection_id,
        source=source,
        external_id=external_id,
    )
    if candidate is None:
        # ``upsert_owned_raw_payload`` locks C. Re-read normalized absence after
        # that serialization point so a concurrent writer cannot leave us with
        # a stale ``None`` candidate and a duplicate insert.
        candidate = await _owned_single_row_candidate(
            session,
            model=GarminDaily,
            natural_clause=GarminDaily.date == on_date,
            identity=identity,
            integration_connection_id=integration_connection_id,
            key_label=external_id,
        )
    return await _apply_owned_daily_raw(
        session,
        on_date,
        raw_row=raw_row,
        identity=identity,
        integration_connection_id=integration_connection_id,
        source=source,
        candidate=candidate,
        prepared_weight_write=prepared_weight_write,
    )


# ── Activities ────────────────────────────────────────────────────────────────
# Activity parsing lives in vitals.services.garmin.normalization.

async def _apply_owned_activity_raw(
    session: AsyncSession,
    *,
    raw_row: RawPayload,
    external_id: str,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    candidate: GarminActivity | None,
) -> GarminActivity:
    raw = raw_row.payload
    start = _parse_activity_start(raw)
    row = candidate
    if row is None:
        row = GarminActivity(
            subject_id=identity.subject_id,
            actor_user_id=raw_row.actor_user_id,
            integration_connection_id=integration_connection_id,
            external_id=external_id,
            domain=DOMAIN,
        )
        session.add(row)
    else:
        _adopt_owned_row(
            row,
            identity=identity,
            integration_connection_id=integration_connection_id,
        )
    row.source = Source.GARMIN_API.value
    row.raw_payload_id = raw_row.id
    row.date = (start or now_local()).date()
    row.activity_type = _dig(raw, "activityType", "typeKey") or raw.get(
        "activityType"
    )
    row.name = raw.get("activityName")
    row.start_time = start
    row.duration_seconds = _intish(raw.get("duration"))
    row.distance_m = _num(raw.get("distance"))
    row.calories = _intish(raw.get("calories"))
    row.avg_hr = _intish(raw.get("averageHR"))
    row.max_hr = _intish(raw.get("maxHR"))
    row.elevation_gain_m = _num(raw.get("elevationGain"))
    row.avg_power = _intish(raw.get("avgPower"))
    row.training_effect_aerobic = _num(raw.get("aerobicTrainingEffect"))
    row.training_effect_anaerobic = _num(raw.get("anaerobicTrainingEffect"))
    row.hr_zone_seconds = _normalize_hr_zones(raw)
    row.splits = _normalize_splits(raw)
    await session.flush()
    raw_row.processed_at = now_local()
    return row


async def ingest_owned_activities(
    session: AsyncSession,
    activities: Sequence[dict],
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> int:
    """Owned raw-first activity upsert, isolated by ``S+C+external_id``."""

    await _lock_owned_garmin_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )

    prepared: list[tuple[dict, str]] = []
    candidates: dict[str, GarminActivity | None] = {}
    for raw in activities:
        if not isinstance(raw, dict):
            raise GarminOwnershipValidationError(
                "each Garmin activity must be a JSON object"
            )
        external_id = _activity_external_id(raw)
        if not external_id:
            continue
        if external_id not in candidates:
            candidate = await _owned_single_row_candidate(
                session,
                model=GarminActivity,
                natural_clause=GarminActivity.external_id == external_id,
                identity=identity,
                integration_connection_id=integration_connection_id,
                key_label=f"activity:{external_id}",
            )
            candidates[external_id] = candidate  # type: ignore[assignment]
        prepared.append((raw, external_id))

    written = 0
    for raw, external_id in prepared:
        raw_external_id = f"activity:{external_id}"
        raw_row = await raw_payload_service.upsert_owned_raw_payload(
            session,
            identity=identity,
            integration_connection_id=integration_connection_id,
            domain=DOMAIN,
            source=Source.GARMIN_API.value,
            external_id=raw_external_id,
            payload=raw,
        )
        _validate_owned_raw_row(
            raw_row,
            identity=identity,
            integration_connection_id=integration_connection_id,
            source=Source.GARMIN_API.value,
            external_id=raw_external_id,
        )
        if candidates[external_id] is None:
            candidates[external_id] = await _owned_single_row_candidate(
                session,
                model=GarminActivity,
                natural_clause=GarminActivity.external_id == external_id,
                identity=identity,
                integration_connection_id=integration_connection_id,
                key_label=raw_external_id,
            )  # type: ignore[assignment]
        row = await _apply_owned_activity_raw(
            session,
            raw_row=raw_row,
            external_id=external_id,
            identity=identity,
            integration_connection_id=integration_connection_id,
            candidate=candidates[external_id],
        )
        candidates[external_id] = row
        written += 1
    return written


# Activity detail normalization lives in vitals.services.garmin.normalization.

# ── Health Auto Export (backup channel) ───────────────────────────────────────
# Map Health Auto Export metric names → the daily column they populate, with the
# unit conversion needed (HAE reports minutes for sleep, count for steps, etc.).
_HAE_METRIC_MAP = {
    "step_count": ("steps", lambda q: _intish(q)),
    "active_energy": ("active_calories", lambda q: _intish(q)),
    "basal_energy_burned": ("bmr_calories", lambda q: _intish(q)),
    "resting_heart_rate": ("resting_hr", lambda q: _intish(q)),
    "heart_rate_variability": ("hrv_avg", lambda q: _num(q)),
    "respiratory_rate": ("avg_respiration", lambda q: _num(q)),
    "blood_oxygen_saturation": ("spo2_avg", lambda q: _num(q)),
    "sleep_analysis": ("sleep_seconds", lambda q: _intish((q or 0) * 3600)),  # hours → s
}


async def ingest_owned_health_auto_export(
    session: AsyncSession,
    payload: dict,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> dict:
    """Owned HAE ingest with a distinct raw key and partial daily projection."""

    if not isinstance(payload, dict):
        raise GarminOwnershipValidationError("payload must be a JSON object")
    await _lock_owned_garmin_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )

    metrics = _dig(payload, "data", "metrics") or payload.get("metrics") or []
    by_date: dict[date_type, dict] = {}
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        mapping = _HAE_METRIC_MAP.get(metric.get("name"))
        if not mapping:
            continue
        column, convert = mapping
        for point in metric.get("data") or []:
            if not isinstance(point, dict):
                continue
            day = _parse_hae_date(point.get("date"))
            if day is None:
                continue
            value = convert(point.get("qty"))
            if value is not None:
                by_date.setdefault(day, {})[column] = value

    prepared: list[tuple[date_type, dict, GarminDaily | None]] = []
    for day, fields in sorted(by_date.items()):
        candidate = await _owned_single_row_candidate(
            session,
            model=GarminDaily,
            natural_clause=GarminDaily.date == day,
            identity=identity,
            integration_connection_id=integration_connection_id,
            key_label=f"daily:{day.isoformat()}",
        )
        prepared.append((day, fields, candidate))  # type: ignore[arg-type]

    for day, fields, candidate in prepared:
        external_id = f"hae:{day.isoformat()}"
        raw_row = await raw_payload_service.upsert_owned_raw_payload(
            session,
            identity=identity,
            integration_connection_id=integration_connection_id,
            domain=DOMAIN,
            source=Source.HEALTH_AUTO_EXPORT.value,
            external_id=external_id,
            payload={"metrics": fields, "source_payload": payload},
        )
        _validate_owned_raw_row(
            raw_row,
            identity=identity,
            integration_connection_id=integration_connection_id,
            source=Source.HEALTH_AUTO_EXPORT.value,
            external_id=external_id,
        )
        if candidate is None:
            candidate = await _owned_single_row_candidate(
                session,
                model=GarminDaily,
                natural_clause=GarminDaily.date == day,
                identity=identity,
                integration_connection_id=integration_connection_id,
                key_label=f"daily:{day.isoformat()}",
            )  # type: ignore[assignment]
        await _apply_owned_hae_raw(
            session,
            day,
            raw_row=raw_row,
            identity=identity,
            integration_connection_id=integration_connection_id,
            candidate=candidate,
        )
    return {"dates": len(prepared)}


# Health Auto Export date parsing lives in vitals.services.garmin.normalization.
