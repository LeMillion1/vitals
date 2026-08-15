"""Opt-in, retryable export of the latest local weight to Garmin Connect.

The local weight log remains the source of truth.  This module discovers the
latest fresh direct measurement, projects it into an outbox row, then reconciles
that row with Garmin before writing.  It never exports a Garmin-imported value
back to Garmin and never backfills the full history.

Garmin Connect's health write endpoint is unofficial, so the safety rules are
intentionally conservative:

* read before write, so an equal remote value makes the operation idempotent;
* after an ambiguous POST failure, the next attempt reads again before retrying;
* delete only a ``samplePk`` observed after Vitals' own successful POST;
* a local save never calls Garmin — the scheduler owns all network activity.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Severity, Source
from vitals.i18n import t
from vitals.models.app_settings import AppSetting
from vitals.models.garmin import (
    DOMAIN,
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_SENT,
    WEIGHT_EXPORT_SKIPPED,
    GarminWeightExport,
)
from vitals.models.weight import WeightLog
from vitals.services import alerts_service
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

SETTING_KEY = "garmin_weight_export_enabled"
ALERT_KEY = "garmin.weight_export"
ALERT_ENTITY = "weight"
MAX_AGE_DAYS = 30
EXPORT_INTERVAL_MINUTES = 15
WEIGHT_TOLERANCE_KG = 0.05
LOCAL_WEIGHT_TOLERANCE_KG = 1e-6
MAX_ERROR_LENGTH = 500

ELIGIBLE_SOURCES = (
    Source.MANUAL.value,
    Source.MCP.value,
    Source.BODY_SCAN.value,
)
ACTIONABLE_STATUSES = (WEIGHT_EXPORT_PENDING, WEIGHT_EXPORT_FAILED)


@dataclass(frozen=True)
class RemoteWeighIn:
    sample_pk: Optional[str]
    weight_kg: Optional[float]


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
        if value is not None and str(value):
            return str(value)
    return None


def parse_daily_weigh_ins(payload: Any) -> list[RemoteWeighIn]:
    """Normalise the individual-weigh-in response without using daily averages."""
    entries: Any
    if payload in (None, {}):
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
    return [
        RemoteWeighIn(_entry_sample_pk(entry), _entry_weight_kg(entry))
        for entry in entries
        if isinstance(entry, dict)
    ]


def _response_sample_pk(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    direct = _entry_sample_pk(payload)
    if direct is not None:
        return direct
    for key in ("userWeight", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _entry_sample_pk(nested)
            if found is not None:
                return found
    return None


async def is_enabled(session: AsyncSession) -> bool:
    row = await session.get(AppSetting, SETTING_KEY)
    return row is not None and row.value is True


async def set_enabled(session: AsyncSession, enabled: bool) -> bool:
    """Persist the opt-in switch. Flushes; the caller owns the commit."""
    clean = bool(enabled)
    row = await session.get(AppSetting, SETTING_KEY)
    if row is None:
        session.add(AppSetting(key=SETTING_KEY, value=clean))
    else:
        row.value = clean
    if clean:
        # Populate the status card immediately; the network write still belongs
        # exclusively to the background job.
        await reconcile_latest(session)
    else:
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
            GarminWeightExport.status.in_(ACTIONABLE_STATUSES)
        )
    )
    for row in result.scalars().all():
        if keep_date is not None and row.date == keep_date:
            continue
        row.status = WEIGHT_EXPORT_SKIPPED
        row.next_attempt_at = None
        row.last_error = None


async def reconcile_latest(
    session: AsyncSession, *, now: Optional[datetime] = None
) -> Optional[GarminWeightExport]:
    """Project only the latest fresh direct measurement into the outbox."""
    clock = now or now_local()
    cutoff = clock.date() - timedelta(days=MAX_AGE_DAYS)
    result = await session.execute(
        select(WeightLog)
        .where(
            WeightLog.superseded.is_(False),
            WeightLog.date >= cutoff,
            WeightLog.date <= clock.date(),
        )
        .order_by(WeightLog.date.desc(), WeightLog.id.desc())
        .limit(1)
    )
    local = result.scalar_one_or_none()
    # Decide eligibility *after* finding the latest active fact. If today's
    # latest value came from Garmin, exporting an older manual row would be a
    # disguised history backfill and could distort Garmin's daily average.
    if local is None or local.source not in ELIGIBLE_SOURCES:
        await _skip_actionable_except(session, keep_date=None)
        await session.flush()
        return None

    result = await session.execute(
        select(GarminWeightExport).where(GarminWeightExport.date == local.date)
    )
    outbox = result.scalar_one_or_none()
    if outbox is None:
        outbox = GarminWeightExport(
            date=local.date,
            weight_log_id=local.id,
            weight_kg=local.weight_kg,
            measured_at=_measurement_time(local.date, clock),
            status=WEIGHT_EXPORT_PENDING,
        )
        session.add(outbox)
    else:
        weight_changed = not _same_local_weight(outbox.weight_kg, local.weight_kg)
        outbox.weight_log_id = local.id
        if weight_changed:
            outbox.weight_kg = local.weight_kg
            outbox.measured_at = _measurement_time(local.date, clock)
            outbox.status = WEIGHT_EXPORT_PENDING
            outbox.attempts = 0
            outbox.last_attempt_at = None
            outbox.next_attempt_at = None
            outbox.exported_at = None
            outbox.last_error = None
            # Keep remote_*: an owned previous value is what makes a correction
            # replaceable without touching unrelated Garmin data.
        elif outbox.status == WEIGHT_EXPORT_SKIPPED:
            outbox.status = WEIGHT_EXPORT_PENDING
            outbox.next_attempt_at = None
            outbox.last_error = None

    await _skip_actionable_except(session, keep_date=local.date)
    await session.flush()
    return outbox


def _due(row: GarminWeightExport, now: datetime) -> bool:
    return row.status in ACTIONABLE_STATUSES and (
        row.next_attempt_at is None or row.next_attempt_at <= now
    )


def _mark_sent(
    row: GarminWeightExport,
    *,
    now: datetime,
    sample_pk: Optional[str],
    owned: bool,
) -> None:
    row.status = WEIGHT_EXPORT_SENT
    row.exported_at = now
    row.next_attempt_at = None
    row.last_error = None
    row.remote_sample_pk = sample_pk
    row.remote_weight_kg = row.weight_kg
    row.remote_owned = owned


async def _raise_failure_alert(
    session: AsyncSession, *, row: GarminWeightExport, error: str
) -> None:
    await alerts_service.raise_alert(
        session,
        domain=DOMAIN,
        severity=Severity.WARN.value,
        message=t(
            "alert.garmin_weight_export",
            date=row.date.isoformat(),
            error=error,
        ),
        alert_key=ALERT_KEY,
        entity_ref=ALERT_ENTITY,
    )


async def export_latest(
    session: AsyncSession,
    client: Any,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Reconcile and, when due, send at most one local weight. Never commits."""
    clock = now or now_local()
    row = await reconcile_latest(session, now=clock)
    if row is None:
        await alerts_service.resolve_by_key(
            session, alert_key=ALERT_KEY, entity_ref=ALERT_ENTITY
        )
        return {"status": "empty", "sent": False}
    if not _due(row, clock):
        if row.status == WEIGHT_EXPORT_SENT:
            await alerts_service.resolve_by_key(
                session, alert_key=ALERT_KEY, entity_ref=ALERT_ENTITY
            )
        return {"status": row.status, "sent": False, "date": row.date}

    row.attempts += 1
    row.last_attempt_at = clock

    try:
        remote = parse_daily_weigh_ins(await client.fetch_daily_weigh_ins(row.date))

        # A correction may replace only the exact remote object Vitals created.
        # If it vanished elsewhere, clear our ownership and continue normally.
        if row.remote_owned and row.remote_sample_pk is not None:
            owned_entry = next(
                (item for item in remote if item.sample_pk == row.remote_sample_pk), None
            )
            if owned_entry is None:
                row.remote_sample_pk = None
                row.remote_weight_kg = None
                row.remote_owned = False
            elif not _same_weight(owned_entry.weight_kg, row.weight_kg):
                await client.delete_weigh_in(row.remote_sample_pk, row.date)
                remote = [item for item in remote if item.sample_pk != row.remote_sample_pk]
                row.remote_sample_pk = None
                row.remote_weight_kg = None
                row.remote_owned = False

        match = next(
            (item for item in remote if _same_weight(item.weight_kg, row.weight_kg)),
            None,
        )
        if match is not None:
            still_owned = bool(
                row.remote_owned
                and row.remote_sample_pk is not None
                and match.sample_pk == row.remote_sample_pk
            )
            _mark_sent(
                row,
                now=clock,
                sample_pk=match.sample_pk,
                owned=still_owned,
            )
            await alerts_service.resolve_by_key(
                session, alert_key=ALERT_KEY, entity_ref=ALERT_ENTITY
            )
            await session.flush()
            return {"status": row.status, "sent": False, "date": row.date}

        before_ids = {item.sample_pk for item in remote if item.sample_pk is not None}
        response = await client.add_weigh_in(row.weight_kg, row.measured_at)
        response_pk = _response_sample_pk(response)

        # A successful POST is final even if read-back is temporarily unavailable:
        # retrying it blindly would create the duplicate this outbox exists to
        # prevent.  Read-back only determines whether future corrections may
        # safely delete the record.
        post_remote: Sequence[RemoteWeighIn] = ()
        try:
            post_remote = parse_daily_weigh_ins(
                await client.fetch_daily_weigh_ins(row.date)
            )
        except Exception:  # noqa: BLE001
            logger.warning("Garmin weight POST succeeded but read-back failed", exc_info=True)

        new_matches = [
            item
            for item in post_remote
            if _same_weight(item.weight_kg, row.weight_kg)
            and item.sample_pk not in before_ids
        ]
        observed = next(
            (item for item in new_matches if item.sample_pk == response_pk),
            new_matches[0] if len(new_matches) == 1 else None,
        )
        sample_pk = response_pk or (observed.sample_pk if observed is not None else None)
        owned = sample_pk is not None and (
            response_pk is not None or observed is not None
        )
        _mark_sent(row, now=clock, sample_pk=sample_pk, owned=owned)
        await alerts_service.resolve_by_key(
            session, alert_key=ALERT_KEY, entity_ref=ALERT_ENTITY
        )
        await session.flush()
        return {"status": row.status, "sent": True, "date": row.date}
    except Exception as exc:  # noqa: BLE001 — every upstream failure is retryable
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
        exponent = min(max(row.attempts - 1, 0), 5)
        delay_minutes = min(EXPORT_INTERVAL_MINUTES * (2**exponent), 360)
        row.status = WEIGHT_EXPORT_FAILED
        row.next_attempt_at = clock + timedelta(minutes=delay_minutes)
        row.last_error = error
        await _raise_failure_alert(session, row=row, error=error)
        await session.flush()
        logger.warning("Garmin weight export failed for %s: %s", row.date, error)
        return {
            "status": row.status,
            "sent": False,
            "date": row.date,
            "error": error,
        }


async def get_status(session: AsyncSession) -> dict[str, Any]:
    """Small settings-card projection of the newest outbox state."""
    enabled = await is_enabled(session)
    result = await session.execute(
        select(GarminWeightExport)
        .order_by(GarminWeightExport.date.desc(), GarminWeightExport.id.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return {
        "enabled": enabled,
        "status": row.status if row is not None else None,
        "date": row.date if row is not None else None,
        "weight_kg": row.weight_kg if row is not None else None,
        "exported_at": row.exported_at if row is not None else None,
        "last_error": row.last_error if row is not None else None,
    }


async def export_job(session_factory, redis=None) -> None:
    """Quarter-hour scheduler entry point; no network while opt-in is off."""
    from vitals.i18n import current_lang
    from vitals.integrations.garmin_client import GarminClient
    from vitals.services.language_service import get_language

    async with session_factory() as session:
        if not await is_enabled(session):
            return
        current_lang.set(await get_language(session, redis))
        client = GarminClient.from_config(redis=redis)
        await export_latest(session, client)
        await session.commit()
