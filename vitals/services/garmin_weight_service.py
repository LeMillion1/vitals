"""Opt-in, retryable export of the latest local weight to Garmin Connect.

The local weight log remains the source of truth.  This module discovers the
latest fresh direct measurement, projects it into an outbox row, then reconciles
that row with Garmin before writing.  It never exports a Garmin-imported value
back to Garmin and never backfills the full history.

Garmin Connect's health write endpoint is unofficial, so the safety rules are
intentionally conservative:

* POST only into a freshly observed empty Garmin day;
* an ambiguous POST is read-reconciled and never retried automatically;
* delete only a ``samplePk`` established as Vitals-owned;
* a local save never calls Garmin — only explicit reconciliation owns network
  activity (the scheduler or the user-triggered ``Send now`` action).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Severity, Source
from vitals.config import load_config
from vitals.i18n import t
from vitals.models.app_settings import AppSetting
from vitals.models.garmin import (
    DOMAIN,
    WEIGHT_EXPORT_CHECKING,
    WEIGHT_EXPORT_CONFLICT,
    WEIGHT_EXPORT_DELETED,
    WEIGHT_EXPORT_DELETE_CHECKING,
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
from vitals.services import alerts_service
from vitals.services.proactive import prefs as proactive_prefs
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

SETTING_KEY = "garmin_weight_export_enabled"
ALERT_KEY = "garmin.weight_export"
ALERT_ENTITY = "weight"
WEIGHT_TOLERANCE_KG = 0.05
LOCAL_WEIGHT_TOLERANCE_KG = 1e-6
MAX_ERROR_LENGTH = 500
OPERATION_LOCK_TTL_SECONDS = 900
# Transaction-scoped PostgreSQL advisory lock shared by scheduled/manual export,
# opt-in changes, reconciliation, and delete hooks. The append-only date watermark
# cannot regress, but without a cross-date lock an older row could still reach POST
# while another transaction committed a newer intent.
_DB_OPERATION_LOCK_ID = 0x564954414C535747  # ASCII-ish "VITALSWG", signed bigint-safe.

ELIGIBLE_SOURCES = (
    Source.MANUAL.value,
    Source.MCP.value,
    Source.BODY_SCAN.value,
)
EXPORT_INTENT_STATUSES = (
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_CONFLICT,
)
SUPERSEDEABLE_STATUSES = (*EXPORT_INTENT_STATUSES, WEIGHT_EXPORT_CHECKING)
DUE_STATUSES = (
    *EXPORT_INTENT_STATUSES,
    WEIGHT_EXPORT_CHECKING,
    WEIGHT_EXPORT_UNVERIFIED,
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_DELETE_FAILED,
)
DELETE_STATUSES = (
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_DELETE_FAILED,
    WEIGHT_EXPORT_DELETE_CHECKING,
)
ISSUE_STATUSES = (
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_CONFLICT,
    WEIGHT_EXPORT_UNVERIFIED,
    WEIGHT_EXPORT_DELETE_FAILED,
)


class GarminWeightConflict(RuntimeError):
    """The remote day is non-empty but unsafe to mutate automatically."""


async def _acquire_operation_lock(session: AsyncSession) -> None:
    """Serialize every production outbox transition for the transaction lifetime."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _DB_OPERATION_LOCK_ID},
        )


async def lock_active_weight_change(session: AsyncSession) -> None:
    """Take the outbox lock before a local delete can touch its FK.

    PostgreSQL applies ``ON DELETE SET NULL`` while the weight row is flushed,
    which locks the related outbox row.  Taking the advisory lock first keeps the
    same lock order as the exporter and avoids an outbox-row/advisory deadlock.
    This helper is deliberately local-only and never talks to Garmin.
    """
    await _acquire_operation_lock(session)


async def handle_active_weight_changed(
    session: AsyncSession, *, now: Optional[datetime] = None
) -> None:
    """Invalidate/project the outbox in the same transaction as a local save.

    This is local-only. It closes the window where an exporter could finish a
    stale preflight after a newer active weight committed without touching the
    outbox lease.
    """
    await _acquire_operation_lock(session)
    if await is_enabled(session):
        await reconcile_latest(session, now=now)


@dataclass(frozen=True)
class RemoteWeighIn:
    sample_pk: Optional[str]
    weight_kg: Optional[float]
    timestamp_ms: Optional[int] = None
    source_type: Optional[str] = None
    sample_pk_exact: bool = False


@dataclass(frozen=True)
class OperationLease:
    """Identity of one committed network attempt."""

    row_id: int
    status: str
    attempts: int
    last_attempt_at: Optional[datetime]
    next_attempt_at: Optional[datetime]
    weight_log_id: Optional[int]
    weight_kg: float
    measured_at: datetime
    dispatch_timestamp_ms: Optional[int]
    remote_sample_pk: Optional[str]
    remote_weight_kg: Optional[float]
    remote_owned: bool


@dataclass(frozen=True)
class DispatchIdentity:
    """Durable identity of one non-idempotent POST attempt."""

    row_id: int
    measured_at: datetime
    dispatch_timestamp_ms: int
    remote_weight_kg: float


def _lease_for(row: GarminWeightExport) -> OperationLease:
    return OperationLease(
        row_id=row.id,
        status=row.status,
        attempts=row.attempts,
        last_attempt_at=row.last_attempt_at,
        next_attempt_at=row.next_attempt_at,
        weight_log_id=row.weight_log_id,
        weight_kg=row.weight_kg,
        measured_at=row.measured_at,
        dispatch_timestamp_ms=row.dispatch_timestamp_ms,
        remote_sample_pk=row.remote_sample_pk,
        remote_weight_kg=row.remote_weight_kg,
        remote_owned=row.remote_owned,
    )


def _lease_matches(row: GarminWeightExport, lease: OperationLease) -> bool:
    return _lease_for(row) == lease


def _same_weight(left: Optional[float], right: Optional[float]) -> bool:
    return (
        left is not None
        and right is not None
        and math.isclose(left, right, rel_tol=0.0, abs_tol=WEIGHT_TOLERANCE_KG)
    )


def _same_local_weight(left: float, right: float) -> bool:
    """DB values are the desired truth; do not hide a small local correction."""
    return math.isclose(
        left, right, rel_tol=0.0, abs_tol=LOCAL_WEIGHT_TOLERANCE_KG
    )


def _stamp_dispatch_timestamp(row: GarminWeightExport) -> None:
    """Embed a per-attempt correlation token in the date-only fact's timestamp.

    Garmin's normal POST is HTTP 204, but day-view returns the exact millisecond
    timestamp. A non-zero millisecond plus a deterministic per-attempt second
    lets Vitals establish the resulting ``samplePk`` without guessing from weight
    alone. The minute/date remain unchanged for the user-facing record.
    """
    # A row can legitimately POST again after its exact previous object was
    # deleted for a correction. Retry counters reset after success, so derive the
    # next token from the persisted timestamp as well as the row identity. The
    # coprime step walks every non-zero millisecond slot in the existing minute
    # before repeating and therefore never reuses the immediately prior marker.
    existing_ms = row.measured_at.microsecond // 1000
    if row.measured_at.microsecond % 1000 == 0 and 1 <= existing_ms <= 999:
        slot = (row.measured_at.second * 999 + existing_ms - 1 + 7_919) % (
            60 * 999
        )
    else:
        seed = row.id * 1_000_003 + row.attempts * 97_409
        slot = seed % (60 * 999)
    second, millisecond_index = divmod(slot, 999)
    row.measured_at = row.measured_at.replace(
        second=second,
        microsecond=(millisecond_index + 1) * 1000,
    )


def _dispatch_timestamp_ms(measured_at: datetime) -> int:
    zone = ZoneInfo(load_config().timezone)
    aware = (
        measured_at.replace(tzinfo=zone)
        if measured_at.tzinfo is None
        else measured_at.astimezone(zone)
    )
    # measured_at is deliberately millisecond-aligned; round instead of truncate
    # so binary floating-point cannot move a .XYZ marker back by one millisecond.
    return round(aware.timestamp() * 1000)


def _exact_dispatch_match(
    row: GarminWeightExport, remote: list[RemoteWeighIn]
) -> Optional[RemoteWeighIn]:
    """Return one entry carrying Vitals' exact timestamp correlation token."""
    if len(remote) != 1:
        return None
    marker_ms = row.measured_at.microsecond // 1000
    if row.measured_at.microsecond % 1000 != 0 or not 1 <= marker_ms <= 999:
        return None
    entry = remote[0]
    attempted_weight = row.remote_weight_kg
    if (
        entry.sample_pk is None
        or not entry.sample_pk_exact
        or entry.source_type != "MANUAL"
        or row.dispatch_timestamp_ms is None
        or entry.timestamp_ms != row.dispatch_timestamp_ms
        or entry.weight_kg is None
        or attempted_weight is None
        or not _same_local_weight(entry.weight_kg, attempted_weight)
    ):
        return None
    return entry


def _dispatch_identity(row: GarminWeightExport) -> Optional[DispatchIdentity]:
    if row.remote_weight_kg is None or row.dispatch_timestamp_ms is None:
        return None
    return DispatchIdentity(
        row_id=row.id,
        measured_at=row.measured_at,
        dispatch_timestamp_ms=row.dispatch_timestamp_ms,
        remote_weight_kg=row.remote_weight_kg,
    )


def _dispatch_identity_matches(
    row: GarminWeightExport, dispatch: DispatchIdentity
) -> bool:
    """Allow local correction/delete metadata to change, but never the POST marker."""
    return (
        row.id == dispatch.row_id
        and row.status == WEIGHT_EXPORT_UNVERIFIED
        and row.measured_at == dispatch.measured_at
        and row.dispatch_timestamp_ms == dispatch.dispatch_timestamp_ms
        and row.remote_weight_kg is not None
        and _same_local_weight(row.remote_weight_kg, dispatch.remote_weight_kg)
        and not row.remote_owned
        and row.remote_sample_pk is None
    )


def _entry_weight_kg(entry: dict[str, Any]) -> Optional[float]:
    raw: Any = None
    for key in ("weight", "weightValue", "value"):
        if entry.get(key) is not None:
            raw = entry[key]
            break
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    # Garmin's user-weight payload represents mass in grams.  Accept kg as well
    # because some library versions normalise the response before returning it.
    return value / 1000.0 if value > 400 else value


def _entry_sample_pk(entry: dict[str, Any]) -> Optional[str]:
    for key in ("samplePk", "samplePK", "sampleId", "id"):
        value = entry.get(key)
        if (
            not isinstance(value, bool)
            and isinstance(value, (str, int))
            and str(value)
        ):
            return str(value)
    return None


def _exact_sample_pk(entry: dict[str, Any]) -> Optional[str]:
    """Return only the literal day-view/delete-contract ``samplePk`` field."""
    value = entry.get("samplePk")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value) if str(value) else None


def _entry_timestamp_ms(entry: dict[str, Any]) -> Optional[int]:
    """Read the exact documented day-view GMT epoch-millisecond field.

    Correlation deliberately rejects strings, floats, seconds, and alternate
    date fields. Permissive timestamp parsing is useful for display, but unsafe
    as an ownership proof.
    """
    raw = entry.get("timestampGMT")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 100_000_000_000 <= raw < 100_000_000_000_000 else None


def parse_daily_weigh_ins(payload: Any) -> list[RemoteWeighIn]:
    """Normalise the individual-weigh-in response without using daily averages."""
    entries: Any
    if payload is None:
        raise ValueError("Garmin daily weigh-ins response is missing")
    if payload == {}:
        entries = []
    elif isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = None
        for key in ("dateWeightList", "weightList", "weighIns"):
            if key in payload:
                entries = payload[key]
                break
        if entries is None and any(k in payload for k in ("samplePk", "weight")):
            entries = [payload]
        if entries is None:
            raise ValueError("unexpected Garmin daily weigh-ins response")
    else:
        raise ValueError("unexpected Garmin daily weigh-ins response")

    if not isinstance(entries, list):
        raise ValueError("Garmin daily weigh-ins list is not an array")
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Garmin daily weigh-ins contain a non-object entry")
    return [
        RemoteWeighIn(
            sample_pk=_entry_sample_pk(entry),
            weight_kg=_entry_weight_kg(entry),
            timestamp_ms=_entry_timestamp_ms(entry),
            source_type=(
                str(entry["sourceType"])
                if entry.get("sourceType") is not None
                else None
            ),
            sample_pk_exact=_exact_sample_pk(entry) is not None,
        )
        for entry in entries
    ]


def _validated_remote(payload: Any) -> list[RemoteWeighIn]:
    try:
        rows = parse_daily_weigh_ins(payload)
    except ValueError as exc:
        raise GarminWeightConflict(str(exc)) from exc
    if any(row.sample_pk is None or row.weight_kg is None for row in rows):
        raise GarminWeightConflict("Garmin returned an incomplete daily weigh-in")
    return rows


def _response_sample_pk(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    direct = _exact_sample_pk(payload)
    if direct is not None:
        return direct
    for key in ("userWeight", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _exact_sample_pk(nested)
            if found is not None:
                return found
    return None


async def is_enabled(session: AsyncSession) -> bool:
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


async def set_enabled(
    session: AsyncSession,
    enabled: bool,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Persist the opt-in switch. Flushes; the caller owns the commit."""
    await _acquire_operation_lock(session)
    clean = bool(enabled)
    row = await session.get(AppSetting, SETTING_KEY)
    if row is None:
        session.add(AppSetting(key=SETTING_KEY, value=clean))
    else:
        row.value = clean
    if clean:
        # Populate the status card immediately; enabling never performs network I/O.
        await reconcile_latest(session, now=now)
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
        await alerts_service.resolve_by_key(
            session, alert_key=ALERT_KEY, entity_ref=ALERT_ENTITY
        )
    await session.flush()
    return clean


def _measurement_time(on_date: date_type, now: datetime) -> datetime:
    # Date-only local records have no honest historical time. Noon avoids moving
    # the calendar date across practically every timezone boundary.
    return now if on_date == now.date() else datetime.combine(on_date, time(hour=12))


async def _skip_actionable_except(
    session: AsyncSession, *, keep_date: Optional[date_type]
) -> None:
    result = await session.execute(
        select(GarminWeightExport).where(
            GarminWeightExport.status.in_(SUPERSEDEABLE_STATUSES)
        )
    )
    for row in result.scalars().all():
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
) -> GarminWeightExport:
    """Insert one date intent without racing scheduler and manual reconciliation."""
    values = {
        "date": on_date,
        "weight_log_id": weight_log_id,
        "weight_kg": weight_kg,
        "measured_at": measured_at,
        "status": status,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        stmt = postgresql_insert(GarminWeightExport).values(**values)
    elif dialect == "sqlite":
        stmt = sqlite_insert(GarminWeightExport).values(**values)
    else:  # The project supports PostgreSQL and the SQLite fast-test path.
        raise RuntimeError(f"unsupported database dialect for Garmin outbox: {dialect}")
    await session.execute(stmt.on_conflict_do_nothing(index_elements=["date"]))
    result = await session.execute(
        select(GarminWeightExport)
        .where(GarminWeightExport.date == on_date)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


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
        WEIGHT_EXPORT_MATCHED
        if row.remote_sample_pk is not None
        else WEIGHT_EXPORT_UNVERIFIED
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
    await _acquire_operation_lock(session)
    clock = now or now_local()
    if max_age_days is None:
        settings = await proactive_prefs.get_prefs(session)
        max_age_days = settings["garmin_weight_max_age_days"]
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
    watermark = await _watermark_date(session, through_date=clock.date())

    # Once a newer local fact has been observed, deleting it must never expose an
    # older measurement as a new export intent. Every observed date is retained
    # as a terminal outbox row, making MAX(date) an append-only, race-safe cursor.
    if (
        local is not None
        and watermark is not None
        and local.date < watermark
    ):
        await _skip_actionable_except(session, keep_date=None)
        await _resolve_alert_if_clear(session)
        await session.flush()
        return None

    cutoff = clock.date() - timedelta(days=max_age_days)
    # Decide eligibility *after* finding the latest active fact. If today's
    # latest value came from Garmin, exporting an older manual row would be a
    # disguised history backfill and could distort Garmin's daily average.
    eligible = not (
        local is None
        or local.date < cutoff
        or local.source not in ELIGIBLE_SOURCES
    )
    if not eligible:
        if local is not None and (watermark is None or local.date >= watermark):
            await _ensure_outbox_row(
                session,
                on_date=local.date,
                weight_log_id=local.id,
                weight_kg=local.weight_kg,
                measured_at=_measurement_time(local.date, clock),
                status=WEIGHT_EXPORT_SKIPPED,
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


async def handle_active_weight_deleted(
    session: AsyncSession,
    *,
    deleted_id: int,
    on_date: date_type,
    deleted_weight_kg: float,
    replacement: Optional[WeightLog],
    now: Optional[datetime] = None,
) -> None:
    """Update the local outbox after an active weight is deleted; never networks."""
    await _acquire_operation_lock(session)
    clock = now or now_local()
    result = await session.execute(
        select(GarminWeightExport)
        .where(GarminWeightExport.date == on_date)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    outbox = result.scalar_one_or_none()
    if outbox is None:
        # A value created and deleted between scheduler ticks still advances the
        # append-only watermark while export is enabled. If another newer outbox
        # date already exists, that row already provides the required protection.
        if on_date > clock.date():
            await session.flush()
            return
        watermark = await _watermark_date(session, through_date=clock.date())
        if (
            not await is_enabled(session)
            or (watermark is not None and on_date < watermark)
        ):
            await session.flush()
            return
        replacement_is_eligible = (
            replacement is not None and replacement.source in ELIGIBLE_SOURCES
        )
        outbox = await _ensure_outbox_row(
            session,
            on_date=on_date,
            weight_log_id=replacement.id if replacement_is_eligible else None,
            weight_kg=(
                replacement.weight_kg
                if replacement_is_eligible
                else deleted_weight_kg
            ),
            measured_at=_measurement_time(on_date, clock),
            status=(
                WEIGHT_EXPORT_PENDING
                if replacement_is_eligible
                else WEIGHT_EXPORT_DELETED
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


def _due(row: GarminWeightExport, now: datetime, *, force: bool = False) -> bool:
    # Checking states are durable preflight leases. Even Send now must not steal
    # one from a live worker; after expiry a new attempt gets a distinct token.
    if row.status in (WEIGHT_EXPORT_CHECKING, WEIGHT_EXPORT_DELETE_CHECKING):
        return row.next_attempt_at is None or row.next_attempt_at <= now
    return row.status in DUE_STATUSES and (
        force or row.next_attempt_at is None or row.next_attempt_at <= now
    )


def _begin_attempt(row: GarminWeightExport, now: datetime) -> None:
    row.attempts += 1
    row.last_attempt_at = now


def _retry_at(row: GarminWeightExport, now: datetime, base_minutes: int) -> datetime:
    exponent = min(max(row.attempts - 1, 0), 5)
    delay_minutes = min(base_minutes * (2**exponent), 360)
    return now + timedelta(minutes=delay_minutes)


def _error(exc: BaseException | str) -> str:
    if isinstance(exc, BaseException):
        return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
    return str(exc)[:MAX_ERROR_LENGTH]


def _mark_issue(
    row: GarminWeightExport,
    *,
    status: str,
    error: BaseException | str,
    now: datetime,
    base_minutes: int,
) -> str:
    message = _error(error)
    row.status = status
    row.next_attempt_at = _retry_at(row, now, base_minutes)
    row.last_error = message
    return message


def _mark_sent(
    row: GarminWeightExport,
    *,
    now: datetime,
    sample_pk: str,
    remote_weight_kg: Optional[float] = None,
) -> None:
    row.status = WEIGHT_EXPORT_SENT
    row.exported_at = now
    row.attempts = 0
    row.next_attempt_at = None
    row.last_error = None
    row.remote_sample_pk = sample_pk
    row.remote_weight_kg = (
        row.weight_kg if remote_weight_kg is None else remote_weight_kg
    )
    row.remote_owned = True


def _mark_matched(
    row: GarminWeightExport, *, now: datetime, remote: RemoteWeighIn
) -> None:
    row.status = WEIGHT_EXPORT_MATCHED
    row.exported_at = now
    row.attempts = 0
    row.next_attempt_at = None
    row.last_error = None
    row.remote_sample_pk = remote.sample_pk
    row.remote_weight_kg = remote.weight_kg
    row.remote_owned = False
    row.dispatch_timestamp_ms = None


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
    await alerts_service.raise_alert(
        session,
        domain=DOMAIN,
        severity=Severity.WARN.value,
        message=t(
            message_key,
            date=row.date.isoformat(),
            error=error,
        ),
        alert_key=ALERT_KEY,
        entity_ref=ALERT_ENTITY,
    )


async def _resolve_alert_if_clear(session: AsyncSession) -> None:
    result = await session.execute(
        select(GarminWeightExport)
        .where(GarminWeightExport.status.in_(ISSUE_STATUSES))
        .order_by(GarminWeightExport.date.desc(), GarminWeightExport.id.desc())
    )
    issues = result.scalars().all()
    if not issues:
        await alerts_service.resolve_by_key(
            session, alert_key=ALERT_KEY, entity_ref=ALERT_ENTITY
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


async def _fetch_remote(client: Any, on_date: date_type) -> list[RemoteWeighIn]:
    return _validated_remote(await client.fetch_daily_weigh_ins(on_date))


async def _issue_result(
    session: AsyncSession,
    *,
    row: GarminWeightExport,
    status: str,
    error: BaseException | str,
    now: datetime,
    base_minutes: int,
) -> dict[str, Any]:
    message = _mark_issue(
        row,
        status=status,
        error=error,
        now=now,
        base_minutes=base_minutes,
    )
    await session.flush()
    # The alert is an aggregate singleton. Always let the resolver choose the
    # highest-priority outstanding issue instead of allowing a newer, less severe
    # failure to overwrite an older cleanup warning.
    await _resolve_alert_if_clear(session)
    await session.flush()
    logger.warning("Garmin weight operation is %s for %s: %s", status, row.date, message)
    return {
        "status": row.status,
        "sent": False,
        "date": row.date,
        "error": message,
    }


async def _persist_post_intent(
    session: AsyncSession,
    row: GarminWeightExport,
    *,
    lease: OperationLease,
    require_enabled: bool,
    now: datetime,
    base_minutes: int,
) -> Optional[dict[str, Any]]:
    """Durably block duplicate POSTs before dispatching a non-idempotent request.

    This deliberately commits. A crash before the HTTP call can therefore leave a
    conservative ``unverified`` row, but a crash after Garmin accepts the call can
    never restore ``pending`` and blindly POST again.
    """
    await session.flush()
    if not await _revalidate_network_attempt(
        session,
        row,
        lease,
        require_enabled=require_enabled,
    ):
        return _current_result(row)

    if row.weight_log_id is None:
        if row.remote_owned and row.remote_sample_pk is not None:
            row.status = WEIGHT_EXPORT_DELETE_PENDING
            _reset_retry(row)
        elif row.status != WEIGHT_EXPORT_UNVERIFIED:
            _mark_deleted(row, now=now)
        await _resolve_alert_if_clear(session)
        await session.flush()
        return {"status": row.status, "sent": False, "date": row.date}

    watermark = await _watermark_date(session, through_date=now.date())
    if watermark is not None and row.date < watermark:
        row.status = WEIGHT_EXPORT_SKIPPED
        _reset_retry(row)
        await _resolve_alert_if_clear(session)
        await session.flush()
        return {"status": row.status, "sent": False, "date": row.date}
    if row.status != WEIGHT_EXPORT_CHECKING:
        return {"status": row.status, "sent": False, "date": row.date}

    _stamp_dispatch_timestamp(row)
    row.dispatch_timestamp_ms = _dispatch_timestamp_ms(row.measured_at)
    row.status = WEIGHT_EXPORT_UNVERIFIED
    row.remote_sample_pk = None
    row.remote_weight_kg = row.weight_kg
    row.remote_owned = False
    row.last_error = (
        "Garmin POST dispatch was recorded before the request; its outcome must "
        "be verified without a duplicate retry"
    )
    row.next_attempt_at = _retry_at(row, now, base_minutes)
    await session.flush()
    await _resolve_alert_if_clear(session)
    await session.flush()
    await session.commit()
    return None


async def _finalize_owned_dispatch_locked(
    session: AsyncSession,
    row: GarminWeightExport,
    *,
    sample_pk: str,
    dispatched_weight: float,
    now: datetime,
) -> dict[str, Any]:
    """Persist exact ownership and derive the operation from fresh local intent.

    The caller has already reacquired the shared advisory lock and refreshed the
    row. This commit is deliberately internal: the only exact cleanup token must
    survive a caller rollback or process loss immediately after the HTTP result.
    """
    row.remote_sample_pk = sample_pk
    row.remote_weight_kg = dispatched_weight
    row.remote_owned = True
    if row.weight_log_id is None:
        row.status = WEIGHT_EXPORT_DELETE_PENDING
        _reset_retry(row)
    elif not _same_local_weight(row.weight_kg, dispatched_weight):
        row.status = WEIGHT_EXPORT_PENDING
        _reset_retry(row)
        row.exported_at = None
    else:
        _mark_sent(
            row,
            now=now,
            sample_pk=sample_pk,
            remote_weight_kg=dispatched_weight,
        )
    await _resolve_alert_if_clear(session)
    await session.flush()
    await session.commit()
    return {
        "status": row.status,
        "sent": row.status == WEIGHT_EXPORT_SENT,
        "date": row.date,
    }


async def _finalize_response_identity(
    session: AsyncSession,
    row: GarminWeightExport,
    *,
    dispatch: DispatchIdentity,
    sample_pk: str,
    now: datetime,
) -> dict[str, Any]:
    """Record a POST response token unless that dispatch was already superseded."""
    await _acquire_operation_lock(session)
    await session.refresh(row)
    if row.remote_owned and row.remote_sample_pk == sample_pk:
        return _current_result(row)
    if not _dispatch_identity_matches(row, dispatch):
        return _current_result(row)
    return await _finalize_owned_dispatch_locked(
        session,
        row,
        sample_pk=sample_pk,
        dispatched_weight=dispatch.remote_weight_kg,
        now=now,
    )


async def _revalidate_dispatch_identity(
    session: AsyncSession,
    row: GarminWeightExport,
    dispatch: DispatchIdentity,
) -> bool:
    """Refresh a dispatched POST marker while allowing local intent to evolve."""
    await _acquire_operation_lock(session)
    await session.refresh(row)
    return _dispatch_identity_matches(row, dispatch)


async def _post_weight(
    session: AsyncSession,
    client: Any,
    row: GarminWeightExport,
    *,
    lease: OperationLease,
    require_enabled: bool,
    now: datetime,
    base_minutes: int,
) -> dict[str, Any]:
    """POST into a freshly observed empty day after a durable dispatch marker."""
    aborted = await _persist_post_intent(
        session,
        row,
        lease=lease,
        require_enabled=require_enabled,
        now=now,
        base_minutes=base_minutes,
    )
    if aborted is not None:
        return aborted

    dispatch = _dispatch_identity(row)
    if dispatch is None:  # Defensive: the durable marker always records a weight.
        raise RuntimeError("Garmin POST dispatch marker has no attempted weight")
    try:
        response = await client.add_weigh_in(
            dispatch.remote_weight_kg, dispatch.measured_at
        )
    except Exception as exc:  # noqa: BLE001 — the request outcome can be ambiguous
        # The request already left Vitals. Preserve its diagnostic even if a
        # correction, deletion, or opt-out happened while Garmin was responding.
        if not await _revalidate_dispatch_identity(session, row, dispatch):
            return _current_result(row)
        return await _issue_result(
            session,
            row=row,
            status=WEIGHT_EXPORT_UNVERIFIED,
            error=exc,
            now=now,
            base_minutes=base_minutes,
        )

    response_pk = _response_sample_pk(response)
    if response_pk is not None:
        return await _finalize_response_identity(
            session,
            row,
            dispatch=dispatch,
            sample_pk=response_pk,
            now=now,
        )

    # A response without identity authorises only a diagnostic GET. Local intent
    # may have changed after dispatch, but the immutable timestamp/weight marker
    # still identifies the one request whose result we are observing.
    if not await _revalidate_dispatch_identity(session, row, dispatch):
        return _current_result(row)
    await session.commit()
    try:
        post_remote = await _fetch_remote(client, row.date)
    except Exception as exc:  # noqa: BLE001 — a successful POST must not be repeated
        logger.warning("Garmin weight POST succeeded but read-back failed", exc_info=True)
        if not await _revalidate_dispatch_identity(session, row, dispatch):
            return _current_result(row)
        return await _issue_result(
            session,
            row=row,
            status=WEIGHT_EXPORT_UNVERIFIED,
            error=exc,
            now=now,
            base_minutes=base_minutes,
        )

    if not await _revalidate_dispatch_identity(session, row, dispatch):
        return _current_result(row)
    exact = _exact_dispatch_match(row, post_remote)
    if exact is not None:
        return await _finalize_owned_dispatch_locked(
            session,
            row,
            sample_pk=exact.sample_pk,
            dispatched_weight=dispatch.remote_weight_kg,
            now=now,
        )
    return await _issue_result(
        session,
        row=row,
        status=WEIGHT_EXPORT_UNVERIFIED,
        error=(
            "Garmin accepted the weight but did not return its identity; read-back "
            f"found {len(post_remote)} record(s), which cannot prove ownership"
        ),
        now=now,
        base_minutes=base_minutes,
    )


async def _process_remote_export(
    session: AsyncSession,
    client: Any,
    row: GarminWeightExport,
    remote: list[RemoteWeighIn],
    *,
    lease: OperationLease,
    require_enabled: bool,
    now: datetime,
    base_minutes: int,
) -> dict[str, Any]:
    """Reconcile one validated remote day; mutate only an owned, isolated row."""
    if row.remote_owned and row.remote_sample_pk is not None:
        owned = next(
            (item for item in remote if item.sample_pk == row.remote_sample_pk), None
        )
        foreign = [item for item in remote if item.sample_pk != row.remote_sample_pk]
        if owned is not None and foreign:
            return await _issue_result(
                session,
                row=row,
                status=WEIGHT_EXPORT_CONFLICT,
                error="Garmin has another weigh-in next to the Vitals-owned record",
                now=now,
                base_minutes=base_minutes,
            )
        if owned is not None and _same_local_weight(owned.weight_kg, row.weight_kg):
            _mark_sent(
                row,
                now=now,
                sample_pk=row.remote_sample_pk,
                remote_weight_kg=owned.weight_kg,
            )
            await _resolve_alert_if_clear(session)
            await session.flush()
            return {"status": row.status, "sent": False, "date": row.date}
        if owned is not None:
            # Delete only the exact object we own. A subsequent GET must prove the
            # day empty before a corrected value may be posted. Release the short
            # DB lock for vendor I/O, then validate the same lease again before
            # applying the result or dispatching another mutation.
            sample_pk = row.remote_sample_pk
            await session.commit()
            await client.delete_weigh_in(sample_pk, row.date)
            after_delete = await _fetch_remote(client, row.date)
            if not await _revalidate_network_attempt(
                session,
                row,
                lease,
                require_enabled=require_enabled,
            ):
                return _current_result(row)
            if any(item.sample_pk == sample_pk for item in after_delete):
                raise RuntimeError("Garmin did not confirm deletion of the owned weigh-in")
            row.remote_sample_pk = None
            row.remote_weight_kg = None
            row.remote_owned = False
            if after_delete:
                return await _issue_result(
                    session,
                    row=row,
                    status=WEIGHT_EXPORT_CONFLICT,
                    error="Garmin has an external weigh-in on the correction date",
                    now=now,
                    base_minutes=base_minutes,
                )
            return await _post_weight(
                session,
                client,
                row,
                lease=_lease_for(row),
                require_enabled=require_enabled,
                now=now,
                base_minutes=base_minutes,
            )

        # The owned object was removed outside Vitals. Its absence is safe; any
        # remaining record is external and must pass the conservative rules below.
        row.remote_sample_pk = None
        row.remote_weight_kg = None
        row.remote_owned = False

    if not remote:
        return await _post_weight(
            session,
            client,
            row,
            lease=_lease_for(row),
            require_enabled=require_enabled,
            now=now,
            base_minutes=base_minutes,
        )
    if len(remote) == 1 and _same_weight(remote[0].weight_kg, row.weight_kg):
        _mark_matched(row, now=now, remote=remote[0])
        await _resolve_alert_if_clear(session)
        await session.flush()
        return {"status": row.status, "sent": False, "date": row.date}

    return await _issue_result(
        session,
        row=row,
        status=WEIGHT_EXPORT_CONFLICT,
        error="Garmin already has a different or multiple weigh-in for this date",
        now=now,
        base_minutes=base_minutes,
    )


async def _process_unverified(
    session: AsyncSession,
    client: Any,
    row: GarminWeightExport,
    remote: list[RemoteWeighIn],
    *,
    lease: OperationLease,
    require_enabled: bool,
    now: datetime,
    base_minutes: int,
) -> dict[str, Any]:
    if row.remote_owned and row.remote_sample_pk is not None:
        return await _process_remote_export(
            session,
            client,
            row,
            remote,
            lease=lease,
            require_enabled=require_enabled,
            now=now,
            base_minutes=base_minutes,
        )

    attempted_weight = row.remote_weight_kg
    matches = [
        item for item in remote if _same_weight(item.weight_kg, attempted_weight)
    ]
    if not matches:
        return await _issue_result(
            session,
            row=row,
            status=WEIGHT_EXPORT_UNVERIFIED,
            error=(
                "The deleted local weight may still appear in Garmin; cleanup remains unverified"
                if row.weight_log_id is None
                else "The previous POST is still unverified; duplicate retry is blocked"
            ),
            now=now,
            base_minutes=base_minutes,
        )
    return await _issue_result(
        session,
        row=row,
        status=WEIGHT_EXPORT_UNVERIFIED,
        error=(
            "A matching Garmin record is visible, but the POST response did not "
            "identify it; Vitals cannot claim or delete that sample safely"
            if len(remote) == 1 and len(matches) == 1
            else "The previous POST cannot be identified among Garmin records"
        ),
        now=now,
        base_minutes=base_minutes,
    )


async def _process_delete(
    session: AsyncSession,
    client: Any,
    row: GarminWeightExport,
    remote: list[RemoteWeighIn],
    *,
    lease: OperationLease,
    require_enabled: bool,
    now: datetime,
    base_minutes: int,
) -> dict[str, Any]:
    sample_pk = row.remote_sample_pk if row.remote_owned else None
    if sample_pk is None:
        return await _issue_result(
            session,
            row=row,
            status=WEIGHT_EXPORT_DELETE_FAILED,
            error="Vitals has no verified ownership token for Garmin deletion",
            now=now,
            base_minutes=base_minutes,
        )
    if not any(item.sample_pk == sample_pk for item in remote):
        _mark_deleted(row, now=now)
        await _resolve_alert_if_clear(session)
        await session.flush()
        return {"status": row.status, "sent": False, "date": row.date}

    # Exact-owned deletion is idempotent, but its lease still protects the local
    # transition from a concurrent correction/delete. Never hold the advisory
    # lock while Garmin is on the wire.
    await session.commit()
    await client.delete_weigh_in(sample_pk, row.date)
    after_delete = await _fetch_remote(client, row.date)
    if not await _revalidate_network_attempt(
        session,
        row,
        lease,
        require_enabled=require_enabled,
    ):
        return _current_result(row)
    if any(item.sample_pk == sample_pk for item in after_delete):
        raise RuntimeError("Garmin did not confirm deletion of the owned weigh-in")
    _mark_deleted(row, now=now)
    await _resolve_alert_if_clear(session)
    await session.flush()
    return {"status": row.status, "sent": False, "date": row.date}


async def _next_protected_operation(
    session: AsyncSession, *, now: datetime, force: bool
) -> Optional[GarminWeightExport]:
    result = await session.execute(
        select(GarminWeightExport).where(
            GarminWeightExport.status.in_(
                (*DELETE_STATUSES, WEIGHT_EXPORT_UNVERIFIED)
            )
        )
    )
    rows = [row for row in result.scalars().all() if _due(row, now, force=force)]
    priority = {
        WEIGHT_EXPORT_DELETE_FAILED: 0,
        WEIGHT_EXPORT_DELETE_PENDING: 1,
        WEIGHT_EXPORT_DELETE_CHECKING: 2,
        WEIGHT_EXPORT_UNVERIFIED: 3,
    }
    return min(rows, key=lambda row: (priority[row.status], row.date, row.id)) if rows else None


async def _refresh_operation(
    session: AsyncSession, row: GarminWeightExport
) -> GarminWeightExport:
    result = await session.execute(
        select(GarminWeightExport)
        .where(GarminWeightExport.id == row.id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


def _current_result(row: GarminWeightExport) -> dict[str, Any]:
    return {"status": row.status, "sent": False, "date": row.date}


async def _revalidate_network_attempt(
    session: AsyncSession,
    row: GarminWeightExport,
    lease: OperationLease,
    *,
    require_enabled: bool,
) -> bool:
    """Re-read a committed lease after vendor I/O, under the shared DB lock."""
    await _acquire_operation_lock(session)
    await session.refresh(row)
    if require_enabled and not await is_enabled(session):
        return False
    return _lease_matches(row, lease)


async def export_latest(
    session: AsyncSession,
    client: Any,
    *,
    now: Optional[datetime] = None,
    force: bool = False,
    require_enabled: bool = False,
) -> dict[str, Any]:
    """Reconcile and send at most one weight.

    The caller owns the final commit, but a duplicate-blocking ``unverified``
    marker is committed immediately before every non-idempotent POST.
    """
    await _acquire_operation_lock(session)
    if require_enabled and not await is_enabled(session):
        return {"status": "disabled", "sent": False}
    clock = now or now_local()
    settings = await proactive_prefs.get_prefs(session)
    base_minutes = settings["garmin_weight_export_minutes"]
    projected = await reconcile_latest(
        session,
        now=clock,
        max_age_days=settings["garmin_weight_max_age_days"],
    )
    row = await _next_protected_operation(session, now=clock, force=force)
    if row is None:
        row = projected
    if row is None:
        await _resolve_alert_if_clear(session)
        return {"status": "empty", "sent": False}
    row = await _refresh_operation(session, row)
    if not _due(row, clock, force=force):
        if row.status in (WEIGHT_EXPORT_SENT, WEIGHT_EXPORT_MATCHED):
            await _resolve_alert_if_clear(session)
        return {"status": row.status, "sent": False, "date": row.date}

    operation_status = row.status
    _begin_attempt(row, clock)

    if operation_status in DELETE_STATUSES:
        row.status = WEIGHT_EXPORT_DELETE_CHECKING
        row.next_attempt_at = clock + timedelta(seconds=OPERATION_LOCK_TTL_SECONDS)
    elif operation_status in SUPERSEDEABLE_STATUSES:
        row.status = WEIGHT_EXPORT_CHECKING
        row.next_attempt_at = clock + timedelta(seconds=OPERATION_LOCK_TTL_SECONDS)
    await session.flush()
    lease = _lease_for(row)
    # Release the short DB/advisory lock before touching Garmin. Local saves and
    # delete hooks remain independent of a slow upstream preflight request.
    await session.commit()

    try:
        remote = await _fetch_remote(client, row.date)
        if lease.status == WEIGHT_EXPORT_UNVERIFIED:
            dispatch = (
                DispatchIdentity(
                    row_id=lease.row_id,
                    measured_at=lease.measured_at,
                    dispatch_timestamp_ms=lease.dispatch_timestamp_ms,
                    remote_weight_kg=lease.remote_weight_kg,
                )
                if (
                    lease.remote_weight_kg is not None
                    and lease.dispatch_timestamp_ms is not None
                )
                else None
            )
            if dispatch is not None:
                # Exact attribution is safe and useful even if a local correction,
                # deletion, or opt-out invalidated the retry lease during GET.
                if not await _revalidate_dispatch_identity(session, row, dispatch):
                    return _current_result(row)
                exact = _exact_dispatch_match(row, remote)
                if exact is not None:
                    return await _finalize_owned_dispatch_locked(
                        session,
                        row,
                        sample_pk=exact.sample_pk,
                        dispatched_weight=dispatch.remote_weight_kg,
                        now=clock,
                    )
                if (
                    (require_enabled and not await is_enabled(session))
                    or not _lease_matches(row, lease)
                ):
                    return _current_result(row)
            elif not await _revalidate_network_attempt(
                session,
                row,
                lease,
                require_enabled=require_enabled,
            ):
                return _current_result(row)
        elif not await _revalidate_network_attempt(
            session,
            row,
            lease,
            require_enabled=require_enabled,
        ):
            return _current_result(row)
        if lease.status == WEIGHT_EXPORT_DELETE_CHECKING:
            return await _process_delete(
                session,
                client,
                row,
                remote,
                lease=lease,
                require_enabled=require_enabled,
                now=clock,
                base_minutes=base_minutes,
            )
        if lease.status == WEIGHT_EXPORT_UNVERIFIED:
            return await _process_unverified(
                session,
                client,
                row,
                remote,
                lease=lease,
                require_enabled=require_enabled,
                now=clock,
                base_minutes=base_minutes,
            )
        return await _process_remote_export(
            session,
            client,
            row,
            remote,
            lease=lease,
            require_enabled=require_enabled,
            now=clock,
            base_minutes=base_minutes,
        )
    except GarminWeightConflict as exc:
        if not await _revalidate_network_attempt(
            session,
            row,
            lease,
            require_enabled=require_enabled,
        ):
            return _current_result(row)
        status = (
            WEIGHT_EXPORT_DELETE_FAILED
            if lease.status == WEIGHT_EXPORT_DELETE_CHECKING
            else WEIGHT_EXPORT_UNVERIFIED
            if lease.status == WEIGHT_EXPORT_UNVERIFIED
            else WEIGHT_EXPORT_CONFLICT
        )
        return await _issue_result(
            session,
            row=row,
            status=status,
            error=exc,
            now=clock,
            base_minutes=base_minutes,
        )
    except Exception as exc:  # noqa: BLE001 — upstream failures stay retryable
        if not await _revalidate_network_attempt(
            session,
            row,
            lease,
            require_enabled=require_enabled,
        ):
            return _current_result(row)
        status = (
            WEIGHT_EXPORT_DELETE_FAILED
            if lease.status == WEIGHT_EXPORT_DELETE_CHECKING
            else WEIGHT_EXPORT_UNVERIFIED
            if lease.status == WEIGHT_EXPORT_UNVERIFIED
            else WEIGHT_EXPORT_FAILED
        )
        return await _issue_result(
            session,
            row=row,
            status=status,
            error=exc,
            now=clock,
            base_minutes=base_minutes,
        )


async def get_status(session: AsyncSession) -> dict[str, Any]:
    """Small settings-card projection, prioritising unresolved safety states."""
    enabled = await is_enabled(session)
    result = await session.execute(
        select(GarminWeightExport)
        .order_by(GarminWeightExport.date.desc(), GarminWeightExport.id.desc())
    )
    rows = result.scalars().all()
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


async def send_now(session: AsyncSession, *, redis=None) -> dict[str, Any]:
    """Explicit user-triggered reconciliation. The caller owns the commit."""
    from vitals.integrations.garmin_client import GarminClient

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
        result = await export_latest(
            session, client, force=True, require_enabled=True
        )
    from vitals.services.garmin_service import refresh_token_cache_alert

    await refresh_token_cache_alert(session, client, resolve_if_clear=False)
    return result


async def export_job(session_factory, redis=None) -> None:
    """Scheduled entry point; no network while opt-in is off or unconfigured."""
    from vitals.i18n import current_lang
    from vitals.integrations.garmin_client import GarminClient
    from vitals.services.garmin_service import refresh_token_cache_alert
    from vitals.services.language_service import get_language

    async with session_factory() as session:
        if not await is_enabled(session):
            return
        current_lang.set(await get_language(session, redis))
        client = GarminClient.from_config(redis=redis)
        if not client.is_configured:
            return
        await export_latest(session, client, require_enabled=True)
        await refresh_token_cache_alert(session, client, resolve_if_clear=False)
        await session.commit()
