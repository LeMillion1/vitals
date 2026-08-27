"""Durable vendor-I/O saga for Garmin Weight export and deletion."""

from __future__ import annotations

import logging
import math
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.garmin import (
    WEIGHT_EXPORT_CHECKING,
    WEIGHT_EXPORT_CONFLICT,
    WEIGHT_EXPORT_DELETE_CHECKING,
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_MATCHED,
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_SENT,
    WEIGHT_EXPORT_SKIPPED,
    WEIGHT_EXPORT_UNVERIFIED,
    WEIGHT_EXPORT_DELETE_FAILED,
    GarminWeightExport,
)
from vitals.services.garmin_weight.contracts import (
    DELETE_STATUSES,
    DUE_STATUSES,
    MAX_ERROR_LENGTH,
    OPERATION_LOCK_TTL_SECONDS,
    SUPERSEDEABLE_STATUSES,
    DispatchIdentity,
    GarminWeightConflict,
    GarminWeightExportConnectionInactiveError,
    GarminWeightExportOwnershipError,
    OperationLease,
    PreparedGarminWeightExport,
    RemoteWeighIn,
    _dispatch_timestamp_ms,
    _require_prepared_export,
    _same_local_weight,
    _same_weight,
)
from vitals.services.garmin_weight.outbox import (
    _acquire_operation_lock,
    _activate_scoped_export,
    _active_export_context,
    _reprepare_active_export,
    _scoped_rows,
)
from vitals.services.garmin_weight.reconciliation import (
    _mark_deleted,
    _reset_retry,
    _resolve_alert_if_clear,
    _watermark_date,
    reconcile_latest,
)
from vitals.services.garmin_weight.settings import is_enabled
from vitals.services.proactive.preferences import contracts as preference_contracts
from vitals.services.proactive.preferences import legacy as preference_legacy
from vitals.services.proactive.preferences import queries as preference_queries
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)


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
        slot = (row.measured_at.second * 999 + existing_ms - 1 + 7_919) % (60 * 999)
    else:
        seed = row.id * 1_000_003 + row.attempts * 97_409
        slot = seed % (60 * 999)
    second, millisecond_index = divmod(slot, 999)
    row.measured_at = row.measured_at.replace(
        second=second,
        microsecond=(millisecond_index + 1) * 1000,
    )


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


def _dispatch_identity_matches(row: GarminWeightExport, dispatch: DispatchIdentity) -> bool:
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
        if not isinstance(value, bool) and isinstance(value, (str, int)) and str(value):
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
            source_type=(str(entry["sourceType"]) if entry.get("sourceType") is not None else None),
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
    row.remote_weight_kg = row.weight_kg if remote_weight_kg is None else remote_weight_kg
    row.remote_owned = True


def _mark_matched(row: GarminWeightExport, *, now: datetime, remote: RemoteWeighIn) -> None:
    row.status = WEIGHT_EXPORT_MATCHED
    row.exported_at = now
    row.attempts = 0
    row.next_attempt_at = None
    row.last_error = None
    row.remote_sample_pk = remote.sample_pk
    row.remote_weight_kg = remote.weight_kg
    row.remote_owned = False
    row.dispatch_timestamp_ms = None


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
    if _active_export_context() is None:
        await preference_legacy.get_pre_identity_legacy_prefs(session)
        await _acquire_operation_lock(session)
        await session.refresh(row)
    else:
        await _reprepare_active_export(session, historical=True)
        row = await _refresh_operation(session, row)
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
    if _active_export_context() is None:
        await preference_legacy.get_pre_identity_legacy_prefs(session)
        await _acquire_operation_lock(session)
        await session.refresh(row)
    else:
        await _reprepare_active_export(session, historical=True)
        row = await _refresh_operation(session, row)
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
    if not await _authorize_scoped_vendor_dispatch(
        session,
        row,
        dispatch,
        require_enabled=require_enabled,
    ):
        return _current_result(row)
    try:
        response = await client.add_weigh_in(dispatch.remote_weight_kg, dispatch.measured_at)
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
        owned = next((item for item in remote if item.sample_pk == row.remote_sample_pk), None)
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
            if not await _authorize_scoped_vendor_lease(
                session,
                row,
                lease,
                require_enabled=require_enabled,
            ):
                return _current_result(row)
            await client.delete_weigh_in(sample_pk, row.date)
            after_delete = await _fetch_remote(client, row.date)
            if not await _revalidate_network_attempt(
                session,
                row,
                lease,
                require_enabled=require_enabled,
                historical=True,
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
    matches = [item for item in remote if _same_weight(item.weight_kg, attempted_weight)]
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
    if not await _authorize_scoped_vendor_lease(
        session,
        row,
        lease,
        require_enabled=require_enabled,
    ):
        return _current_result(row)
    await client.delete_weigh_in(sample_pk, row.date)
    after_delete = await _fetch_remote(client, row.date)
    if not await _revalidate_network_attempt(
        session,
        row,
        lease,
        require_enabled=require_enabled,
        historical=True,
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
    if _active_export_context() is None:
        result = await session.execute(
            select(GarminWeightExport).where(
                GarminWeightExport.status.in_((*DELETE_STATUSES, WEIGHT_EXPORT_UNVERIFIED))
            )
        )
        candidates = list(result.scalars().all())
    else:
        candidates = await _scoped_rows(
            session,
            filters=(GarminWeightExport.status.in_((*DELETE_STATUSES, WEIGHT_EXPORT_UNVERIFIED)),),
            for_update=True,
        )
    rows = [row for row in candidates if _due(row, now, force=force)]
    priority = {
        WEIGHT_EXPORT_DELETE_FAILED: 0,
        WEIGHT_EXPORT_DELETE_PENDING: 1,
        WEIGHT_EXPORT_DELETE_CHECKING: 2,
        WEIGHT_EXPORT_UNVERIFIED: 3,
    }
    return min(rows, key=lambda row: (priority[row.status], row.date, row.id)) if rows else None


async def _refresh_operation(session: AsyncSession, row: GarminWeightExport) -> GarminWeightExport:
    if _active_export_context() is None:
        result = await session.execute(
            select(GarminWeightExport)
            .where(GarminWeightExport.id == row.id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one()
    rows = await _scoped_rows(
        session,
        filters=(GarminWeightExport.id == row.id,),
        for_update=True,
    )
    if len(rows) != 1:
        raise GarminWeightExportOwnershipError(
            "leased outbox row left its prepared ownership scope"
        )
    return rows[0]


def _current_result(row: GarminWeightExport) -> dict[str, Any]:
    return {"status": row.status, "sent": False, "date": row.date}


async def _revalidate_network_attempt(
    session: AsyncSession,
    row: GarminWeightExport,
    lease: OperationLease,
    *,
    require_enabled: bool,
    historical: bool = False,
) -> bool:
    """Re-read a committed lease after vendor I/O, under the shared DB lock."""
    if _active_export_context() is None:
        await preference_legacy.get_pre_identity_legacy_prefs(session)
        await _acquire_operation_lock(session)
        await session.refresh(row)
    else:
        await _reprepare_active_export(session, historical=historical)
        row = await _refresh_operation(session, row)
    if require_enabled and not await is_enabled(session):
        return False
    return _lease_matches(row, lease)


async def _release_legacy_roots_for_vendor_io(session: AsyncSession) -> bool:
    """Re-prove the legacy root, then leave nothing locked for the vendor call.

    The scoped branch of both authorizers re-prepares its roots and commits, so
    no identity or outbox lock survives into the request. The legacy branch has
    no scoped roots to re-prepare, but it does have one fact that can expire
    underneath it: a database is only allowed on this path while it has zero
    health subjects. Proving that needs the transaction-scoped identity-governance
    advisory lock, which means it can only be proved inside a transaction — and
    that transaction has to end here.

    Committing is therefore the point of this helper, not an afterthought. The
    caller has already committed its own work and holds nothing worth keeping;
    what must not happen is that the short probe transaction stays open across a
    Garmin round trip, because every local weight save and delete hook queues
    behind the very same governance lock (see ``prepare_weight_write``). Returning
    ``False`` means the database was bootstrapped mid-flight and the legacy path
    must stop, mirroring the scoped branch's inactive-connection exit.
    """

    try:
        await preference_legacy.get_pre_identity_legacy_prefs(session)
    except preference_contracts.ProactivePreferencesError:
        await session.rollback()
        return False
    await session.commit()
    return True


async def _authorize_scoped_vendor_lease(
    session: AsyncSession,
    row: GarminWeightExport,
    lease: OperationLease,
    *,
    require_enabled: bool,
) -> bool:
    """Freshly validate roots immediately before a scoped vendor request."""

    if _active_export_context() is None:
        return await _release_legacy_roots_for_vendor_io(session)
    try:
        await _reprepare_active_export(session, historical=False)
    except GarminWeightExportConnectionInactiveError:
        await session.rollback()
        return False
    row = await _refresh_operation(session, row)
    allowed = _lease_matches(row, lease) and (not require_enabled or await is_enabled(session))
    # Never retain identity/outbox row locks across vendor I/O.
    await session.commit()
    return allowed


async def _authorize_scoped_vendor_dispatch(
    session: AsyncSession,
    row: GarminWeightExport,
    dispatch: DispatchIdentity,
    *,
    require_enabled: bool,
) -> bool:
    if _active_export_context() is None:
        return await _release_legacy_roots_for_vendor_io(session)
    try:
        await _reprepare_active_export(session, historical=False)
    except GarminWeightExportConnectionInactiveError:
        await session.rollback()
        return False
    row = await _refresh_operation(session, row)
    allowed = _dispatch_identity_matches(row, dispatch) and (
        not require_enabled or await is_enabled(session)
    )
    await session.commit()
    return allowed


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
    context = _active_export_context()
    if context is None:
        settings = await preference_legacy.get_pre_identity_legacy_prefs(session)
    await _acquire_operation_lock(session)
    if require_enabled and not await is_enabled(session):
        return {"status": "disabled", "sent": False}
    clock = now or now_local()
    if context is None:
        base_minutes = settings["garmin_weight_export_minutes"]
        max_age_days = settings["garmin_weight_max_age_days"]
    else:
        policy = await preference_queries.get_garmin_policy(
            session,
            subject_id=context.identity.subject_id,
            integration_connection_id=context.integration_connection_id,
        )
        base_minutes = policy.weight_export_minutes
        max_age_days = policy.weight_max_age_days
    projected = await reconcile_latest(
        session,
        now=clock,
        max_age_days=max_age_days,
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

    if not await _authorize_scoped_vendor_lease(
        session,
        row,
        lease,
        require_enabled=require_enabled,
    ):
        return _current_result(row)

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
                if (lease.remote_weight_kg is not None and lease.dispatch_timestamp_ms is not None)
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
                if (require_enabled and not await is_enabled(session)) or not _lease_matches(
                    row, lease
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
            historical=True,
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
            historical=True,
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


async def export_latest_scoped(
    session: AsyncSession,
    client: Any,
    *,
    prepared: PreparedGarminWeightExport,
    now: Optional[datetime] = None,
    force: bool = False,
    require_enabled: bool = False,
) -> dict[str, Any]:
    _require_prepared_export(session, prepared, historical_ok=False)
    with _activate_scoped_export(prepared):
        return await export_latest(
            session,
            client,
            now=now,
            force=force,
            require_enabled=require_enabled,
        )
