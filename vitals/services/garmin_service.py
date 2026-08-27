"""Garmin activity & recovery service (module 6).

Owns the garmin domain:

  * **Daily sync** — pull the day's sub-metrics, keep the full payload in
    ``raw_payloads``, and normalise the wide ``garmin_daily`` row (sleep, HRV,
    RHR, stress, Body Battery, steps, calories, HR, intensity minutes, training
    readiness, …). Upsert by date, so re-syncing a day refreshes it.
  * **Intraday series** — the stress / Body Battery curves inside the same
    payload (~480 samples each per day) land in ``garmin_intraday``, one row per
    sample. Re-import rebuilds a day+series wholesale.
  * **Weight bridge** — a Garmin weigh-in for a date is pushed into the weight
    domain as a ``garmin_api`` row, where the weight service's manual-over-Garmin
    priority already lets a manual entry supersede it.
  * **Activities** — recorded sport sessions, upserted by Garmin activity id.
  * **Recovery advice** — a passive read (Sleep Score < 60 or Body Battery < 40)
    surfaced in the training block; never a popup.
  * **Auth/MFA alert** — a login/MFA failure raises a critical ``warn`` system
    alert (the user re-seeds the token store out-of-band).
  * **Health Auto Export** — a REST backup channel: parse the uploaded JSON into
    ``garmin_daily`` rows (``source='health_auto_export'``).

Normalisation (``_normalize_daily``) is pure and unit-tested; the service is
handed a client (tests pass a fake), never touching the network itself.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Severity,
    Source,
)
from vitals.i18n import t
from vitals.integrations.garmin_client import (
    GarminAuthError,
    GarminLoginThrottled,
    GarminMFARequired,
)
from vitals.models.garmin import (
    DOMAIN,
    SERIES_BODY_BATTERY,
    SERIES_HEART_RATE,
    SERIES_SLEEP_BB,
    SERIES_SLEEP_HR,
    SERIES_SLEEP_HRV,
    SERIES_SLEEP_MOVEMENT,
    SERIES_SLEEP_RESPIRATION,
    SERIES_SLEEP_SPO2,
    SERIES_SLEEP_STRESS,
    SERIES_STRESS,
    GarminActivity,
    GarminDaily,
    GarminIntraday,
)
from vitals.models.raw_payload import RawPayload
from vitals.models.identity import HealthSubject
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import alerts_service, raw_payload_service, weight_service
from vitals.services.conflicts import engine
from vitals.services.credentials import providers
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.utils.timeutils import now_local, to_local_naive

logger = logging.getLogger(__name__)

AUTH_ALERT_KEY = "garmin.auth"
TOKEN_ALERT_KEY = "garmin.token_cache"

SLEEP_SCORE_FLOOR = 60
BODY_BATTERY_FLOOR = 40
SPO2_FLOOR = 90


class GarminOwnershipError(Exception):
    """Base class for fail-closed owned Garmin ingestion failures."""


class GarminOwnershipValidationError(GarminOwnershipError):
    """The caller did not provide the strict ownership contract."""


class GarminConnectionInactiveError(GarminOwnershipValidationError):
    """A non-active provenance root cannot authorize fresh provider work."""


class GarminOwnershipConflictError(GarminOwnershipError):
    """A legacy-global normalized key belongs to another ownership scope."""


class GarminOwnershipAmbiguityError(GarminOwnershipConflictError):
    """More than one normalized row can represent the requested owned fact."""


class GarminRawPayloadInvariantError(GarminOwnershipError):
    """A reparse root does not carry internally consistent Garmin provenance."""


def _validate_owned_context(
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> None:
    if not isinstance(identity, WriteIdentity):
        raise GarminOwnershipValidationError("identity must be a WriteIdentity")
    if not isinstance(integration_connection_id, uuid.UUID):
        raise GarminOwnershipValidationError(
            "integration_connection_id must be a UUID"
        )


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


async def _load_owned_garmin_connection(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    allow_retired: bool = False,
    for_update: bool = False,
) -> IntegrationConnection:
    """Validate the provider root independently of a raw-payload write."""

    _validate_owned_context(identity, integration_connection_id)
    statement = select(IntegrationConnection).where(
        IntegrationConnection.id == integration_connection_id
    )
    if for_update:
        statement = statement.with_for_update()
    with session.no_autoflush:
        connection = await session.scalar(statement)
    if connection is None:
        raise GarminOwnershipValidationError(
            "integration_connection_id does not exist"
        )
    if connection.subject_id != identity.subject_id:
        raise GarminOwnershipConflictError(
            "Garmin connection belongs to another subject"
        )
    if (
        connection.provider != IntegrationProvider.GARMIN.value
        or connection.connection_type != IntegrationConnectionType.ACCOUNT.value
    ):
        raise GarminOwnershipValidationError(
            "integration_connection_id is not a Garmin account connection"
        )
    known_statuses = {status.value for status in IntegrationConnectionStatus}
    if connection.status not in known_statuses:
        raise GarminOwnershipValidationError(
            "Garmin connection has an unknown lifecycle state"
        )
    allowed_statuses = {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }
    if allow_retired:
        allowed_statuses.update(
            {
                IntegrationConnectionStatus.DISABLED.value,
                IntegrationConnectionStatus.RETIRED.value,
            }
        )
    if connection.status not in allowed_statuses:
        raise GarminConnectionInactiveError(
            f"Garmin connection status {connection.status!r} cannot authorize this operation"
        )
    return connection


async def _lock_owned_garmin_scope(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    allow_retired: bool = False,
) -> IntegrationConnection:
    """Serialize owned writes in the shared Subject -> Connection lock order."""

    _validate_owned_context(identity, integration_connection_id)
    with session.no_autoflush:
        subject_id = await session.scalar(
            select(HealthSubject.id)
            .where(HealthSubject.id == identity.subject_id)
            .with_for_update()
        )
    if subject_id is None:
        raise GarminOwnershipValidationError("identity subject does not exist")
    return await _load_owned_garmin_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=allow_retired,
        for_update=True,
    )


async def _require_legacy_adoption_subject(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> None:
    """Keep fully unscoped adoption behind the pre-registration invariant."""

    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    if subject_ids != [subject_id]:
        raise GarminOwnershipConflictError(
            "unscoped legacy Garmin row cannot be adopted after multi-subject "
            "activation"
        )


def _row_scope_is_compatible(
    row: GarminDaily | GarminActivity,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> bool:
    return (
        row.subject_id in {None, identity.subject_id}
        and row.integration_connection_id
        in {None, integration_connection_id}
    )


async def _owned_single_row_candidate(
    session: AsyncSession,
    *,
    model: type[GarminDaily] | type[GarminActivity],
    natural_clause: Any,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    key_label: str,
) -> GarminDaily | GarminActivity | None:
    """Lock this connection's row for the natural key before raw ingestion.

    A Garmin day or activity id is unique inside the account it was fetched
    from, not across the installation, so the lookup is scoped by connection.  A
    row that has not been adopted yet carries no connection at all and is still
    a candidate for the connection now claiming it.
    """

    rows = list(
        await session.scalars(
            select(model)
            .where(
                natural_clause,
                or_(
                    model.integration_connection_id == integration_connection_id,
                    model.integration_connection_id.is_(None),
                ),
            )
            .with_for_update()
        )
    )
    if len(rows) > 1:
        raise GarminOwnershipAmbiguityError(
            f"multiple Garmin rows match scoped key {key_label}"
        )
    if not rows:
        return None
    row = rows[0]
    if not _row_scope_is_compatible(
        row,
        identity=identity,
        integration_connection_id=integration_connection_id,
    ):
        raise GarminOwnershipConflictError(
            f"Garmin row for {key_label} belongs to another ownership scope"
        )
    if row.subject_id is None and row.integration_connection_id is None:
        await _require_legacy_adoption_subject(
            session, subject_id=identity.subject_id
        )
    return row


def _adopt_owned_row(
    row: GarminDaily | GarminActivity,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> None:
    """Fill nullable legacy roots without rewriting historical actor identity."""

    if row.subject_id is None:
        row.subject_id = identity.subject_id
    if row.integration_connection_id is None:
        row.integration_connection_id = integration_connection_id


def _require_matching_normalized_raw_link(
    row: GarminDaily | GarminActivity,
    *,
    raw_payload_id: int,
) -> None:
    """Never make a historical reparse silently replace another raw root."""

    if row.raw_payload_id not in {None, raw_payload_id}:
        raise GarminRawPayloadInvariantError(
            "normalized Garmin row already references a different raw payload"
        )


def _validate_owned_raw_row(
    raw_row: RawPayload,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    source: str,
    external_id: str,
) -> None:
    expected = {
        "subject_id": identity.subject_id,
        "integration_connection_id": integration_connection_id,
        "domain": DOMAIN,
        "source": source,
        "external_id": external_id,
    }
    for field_name, expected_value in expected.items():
        if getattr(raw_row, field_name) != expected_value:
            raise GarminRawPayloadInvariantError(
                f"raw payload {field_name} does not match owned Garmin context"
            )
    if raw_row.file_asset_id is not None:
        raise GarminRawPayloadInvariantError(
            "Garmin account payload cannot reference a file asset"
        )
    if not isinstance(raw_row.payload, dict):
        raise GarminRawPayloadInvariantError("Garmin raw payload must be a JSON object")


async def _validate_intraday_raw_reference(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    on_date: date_type,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    source: str,
) -> None:
    if not isinstance(raw_payload_id, int) or isinstance(raw_payload_id, bool):
        raise GarminOwnershipValidationError("raw_payload_id must be an integer or None")
    with session.no_autoflush:
        raw_row = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_payload_id)
            .with_for_update()
        )
    if raw_row is None:
        raise GarminRawPayloadInvariantError(
            "intraday raw payload does not exist"
        )
    _validate_owned_raw_row(
        raw_row,
        identity=identity,
        integration_connection_id=integration_connection_id,
        source=source,
        external_id=f"daily:{on_date.isoformat()}",
    )


async def _locked_owned_raw_context(
    session: AsyncSession,
    raw_row: RawPayload,
) -> tuple[RawPayload, WriteIdentity, uuid.UUID]:
    """Reload and derive a reparse context from durable raw provenance."""

    if (
        not isinstance(raw_row, RawPayload)
        or not isinstance(raw_row.id, int)
        or isinstance(raw_row.id, bool)
    ):
        raise GarminOwnershipValidationError(
            "raw_row must be a persisted RawPayload"
        )
    with session.no_autoflush:
        preliminary = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_row.id)
            .execution_options(populate_existing=True)
        )
    if preliminary is None:
        raise GarminRawPayloadInvariantError("raw payload no longer exists")
    if not isinstance(preliminary.subject_id, uuid.UUID):
        raise GarminRawPayloadInvariantError(
            "raw payload has no subject ownership"
        )
    if not isinstance(preliminary.integration_connection_id, uuid.UUID):
        raise GarminRawPayloadInvariantError(
            "raw payload has no Garmin connection ownership"
        )
    scope_identity = WriteIdentity(
        subject_id=preliminary.subject_id,
        actor_user_id=None,
    )
    preliminary_connection_id = preliminary.integration_connection_id
    await _lock_owned_garmin_scope(
        session,
        identity=scope_identity,
        integration_connection_id=preliminary_connection_id,
        allow_retired=True,
    )
    with session.no_autoflush:
        persisted = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if persisted is None:
        raise GarminRawPayloadInvariantError("raw payload no longer exists")
    if (
        persisted.subject_id != scope_identity.subject_id
        or persisted.integration_connection_id
        != preliminary_connection_id
    ):
        raise GarminRawPayloadInvariantError(
            "raw payload ownership changed during reparse"
        )
    try:
        identity = WriteIdentity(
            subject_id=persisted.subject_id,
            actor_user_id=persisted.actor_user_id,
        )
    except TypeError as exc:
        raise GarminRawPayloadInvariantError(
            "raw payload actor ownership is invalid"
        ) from exc
    return persisted, identity, persisted.integration_connection_id


# ── Pure extraction helpers ───────────────────────────────────────────────────
def _dig(payload: Any, *path: str) -> Any:
    """Walk nested dict keys, tolerating missing keys / non-dicts → None."""
    cur = payload
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _intish(value: Any) -> Optional[int]:
    n = _num(value)
    return int(round(n)) if n is not None else None


def _first(*values: Any) -> Any:
    """First non-None value (key-fallback chains across Garmin shape variants)."""
    for v in values:
        if v is not None:
            return v
    return None


def _strip_level_suffix(phrase: Optional[str]) -> Optional[str]:
    """Garmin's training-status feedback phrase carries a numeric intensity
    level suffix (e.g. ``"PRODUCTIVE_1"``) the wide column doesn't need -- the
    raw phrase is still kept in ``raw_payloads``."""
    if not isinstance(phrase, str):
        return phrase
    base, _, suffix = phrase.rpartition("_")
    return base if base and suffix.isdigit() else phrase


def _parse_sleep_boundary(sleep_dto: dict, prefix: str) -> Optional[datetime]:
    """Sleep bed/wake timestamp (``prefix`` is e.g. ``"sleepStart"``) -> local
    naive datetime.

    Garmin ships both a ``*TimestampGMT`` (a true UTC epoch, converted the same
    way as ``_parse_activity_start``) and a ``*TimestampLocal`` variant whose ms
    count already bakes the local offset in -- decoding THAT as UTC and just
    stripping tzinfo gives the right wall-clock time directly; running it through
    ``to_local_naive`` too would shift it a second time. GMT is preferred when
    both are present since it's unambiguous."""
    gmt_ms = _num(sleep_dto.get(f"{prefix}TimestampGMT"))
    if gmt_ms is not None:
        return to_local_naive(datetime.fromtimestamp(gmt_ms / 1000, tz=timezone.utc))
    local_ms = _num(sleep_dto.get(f"{prefix}TimestampLocal"))
    if local_ms is not None:
        return datetime.fromtimestamp(local_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
    return None


# ── Intraday series (stress / Body Battery curves) ────────────────────────────
def _epoch_ms_to_local(value: Any) -> Optional[datetime]:
    """Garmin's intraday timestamps are true UTC epoch **milliseconds** (unlike
    the sleep DTO's ``*Local`` variant, which pre-bakes the offset)."""
    ms = _num(value)
    if ms is None:
        return None
    return to_local_naive(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def _descriptor_index(descriptors: Any, wanted_key: str) -> Optional[int]:
    """Column position of ``wanted_key`` in a positional intraday array, read from
    the descriptor list Garmin ships next to it.

    Worth the indirection because the shapes genuinely differ per endpoint:
    ``get_stress_data`` returns Body Battery as ``[ts, status, level, version]``
    while ``get_body_battery`` returns ``[ts, level]`` — hard-coding a position
    would silently store the *status* column for one of them. Both the key and
    index field names also vary (``key``/``index`` for stress,
    ``bodyBatteryValueDescriptor*`` for Body Battery), hence the fallbacks."""
    if not isinstance(descriptors, list):
        return None
    for item in descriptors:
        if not isinstance(item, dict):
            continue
        key = _first(item.get("key"), item.get("bodyBatteryValueDescriptorKey"))
        if key == wanted_key:
            return _intish(_first(
                item.get("index"), item.get("bodyBatteryValueDescriptorIndex")
            ))
    return None


def _parse_intraday_points(
    rows: Any, *, value_index: Optional[int] = None
) -> list[tuple[datetime, float]]:
    """``[[epoch_ms, value, …], …]`` → ``[(local_ts, value), …]``, sorted by time.

    ``value_index`` comes from the descriptor list; without one we take the first
    numeric column after the timestamp, which lands on the value in every shape
    Garmin has been seen to return. Negative readings are Garmin's sentinels
    (stress ``-1`` = no reading, ``-2`` = watch off the wrist) and are dropped —
    they are absence of data, not a measurement, and would drag any average down.
    The raw array is kept whole in ``raw_payloads`` regardless."""
    out: list[tuple[datetime, float]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        ts = _epoch_ms_to_local(row[0])
        if ts is None:
            continue
        if value_index is not None and 0 <= value_index < len(row):
            value = _num(row[value_index])
        else:
            value = next((v for v in (_num(c) for c in row[1:]) if v is not None), None)
        if value is None or value < 0:
            continue
        out.append((ts, value))
    out.sort(key=lambda p: p[0])
    return out


def _intraday_series(raw: dict) -> dict[str, list[tuple[datetime, float]]]:
    """Every intraday curve in the day's bundle, keyed by ``series_type``. Pure.

    The whole-day stress and Body Battery curves ride in the one
    ``get_stress_data`` payload at full ~3-minute resolution; the separate
    ``get_body_battery`` payload only carries inflection points, so it's a
    fallback for when the stress payload came back without the array. The
    whole-day heart rate comes from ``get_heart_rates`` and the night's seven
    series from ``get_sleep_data``; all of them join here, so ``ingest_daily``
    stores every curve through one loop."""
    stress_payload = raw.get("stress") or {}

    stress_rows = stress_payload.get("stressValuesArray")
    stress_index = _descriptor_index(
        stress_payload.get("stressValueDescriptorsDTOList"), "stressLevel"
    )

    bb_rows = stress_payload.get("bodyBatteryValuesArray")
    bb_descriptors = stress_payload.get("bodyBatteryValueDescriptorsDTOList")
    if not bb_rows:
        bb_payload = raw.get("body_battery")
        bb0 = bb_payload[0] if isinstance(bb_payload, list) and bb_payload else bb_payload
        if isinstance(bb0, dict):
            bb_rows = bb0.get("bodyBatteryValuesArray")
            bb_descriptors = _first(
                bb0.get("bodyBatteryValueDescriptorDTOList"),
                bb0.get("bodyBatteryValueDescriptorsDTOList"),
            )
    bb_index = _descriptor_index(bb_descriptors, "bodyBatteryLevel")

    hr_payload = raw.get("heart_rate") or {}
    hr_rows = hr_payload.get("heartRateValues")
    # Garmin ships ``[ts, null]`` for the minutes the watch wasn't measuring;
    # _parse_intraday_points drops those alongside the negative sentinels.
    hr_index = _descriptor_index(hr_payload.get("heartRateValueDescriptors"), "heartrate")

    return {
        SERIES_STRESS: _parse_intraday_points(stress_rows, value_index=stress_index),
        SERIES_BODY_BATTERY: _parse_intraday_points(bb_rows, value_index=bb_index),
        SERIES_HEART_RATE: _parse_intraday_points(hr_rows, value_index=hr_index),
        **_sleep_intraday_series(raw),
    }


# ── The night's series (sleep detail, level B) ────────────────────────────────
# Each nightly array in the one ``get_sleep_data`` payload: the series it feeds,
# its key, and the field names holding the sample's moment and value. The shapes
# genuinely differ per array — verified against the watch's own responses — so
# this table is the parser: most ship an epoch-ms ``startGMT`` + ``value``, but
# respiration renames both fields, SpO2 renames both *and* ships an ISO string,
# and movement ships an ISO string with its own value field.
_SLEEP_SERIES = (
    (SERIES_SLEEP_HR, "sleepHeartRate", "startGMT", "value"),
    (SERIES_SLEEP_STRESS, "sleepStress", "startGMT", "value"),
    (SERIES_SLEEP_BB, "sleepBodyBattery", "startGMT", "value"),
    (SERIES_SLEEP_HRV, "hrvData", "startGMT", "value"),
    (SERIES_SLEEP_RESPIRATION, "wellnessEpochRespirationDataDTOList",
     "startTimeGMT", "respirationValue"),
    (SERIES_SLEEP_SPO2, "wellnessEpochSPO2DataDTOList", "epochTimestamp", "spo2Reading"),
    (SERIES_SLEEP_MOVEMENT, "sleepMovement", "startGMT", "activityLevel"),
)

# ``sleepLevels.activityLevel`` is a stage code, not a measurement.
_SLEEP_STAGE_NAMES = {0: "deep", 1: "light", 2: "rem", 3: "awake"}


def _gmt_moment(value: Any) -> Optional[datetime]:
    """A nightly timestamp in either shape Garmin uses → local naive.

    Epoch ms for most arrays, but an ISO-8601 string for sleepLevels /
    sleepMovement / the SpO2 epochs. The string carries no offset marker yet is
    GMT, so it needs the same UTC→local conversion as the epochs — reading it as
    already-local would smear the whole night by the offset and put stages and
    heart rate on two different timelines."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_ms_to_local(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # to_local_naive() reads a naive value as UTC, which is exactly what these are.
    return to_local_naive(parsed)


def _parse_sleep_points(
    rows: Any, ts_key: str, value_key: str
) -> list[tuple[datetime, float]]:
    """``[{ts_key: …, value_key: …}, …]`` → ``[(local_ts, value), …]``, by time.

    Negatives are dropped for the same reason as the whole-day curves: Garmin's
    ``-1``/``-2`` are "no reading" / "off the wrist" sentinels, not measurements.
    Zero is kept — a movement level of 0.0 means lying perfectly still, which is
    a reading. The raw array stays whole in ``raw_payloads`` either way."""
    out: list[tuple[datetime, float]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _gmt_moment(row.get(ts_key))
        value = _num(row.get(value_key))
        if ts is None or value is None or value < 0:
            continue
        out.append((ts, value))
    out.sort(key=lambda p: p[0])
    return out


def _sleep_intraday_series(raw: dict) -> dict[str, list[tuple[datetime, float]]]:
    """Every nightly point series in the day's bundle, keyed by ``series_type``."""
    sleep = raw.get("sleep")
    if not isinstance(sleep, dict):
        sleep = {}
    return {
        series_type: _parse_sleep_points(sleep.get(key), ts_key, value_key)
        for series_type, key, ts_key, value_key in _SLEEP_SERIES
    }


def _parse_sleep_intervals(rows: Any, value_key: str, out_key: str) -> Optional[list]:
    """A nightly *interval* array → ``[{"start", "end", <out_key>}, …]``.

    Spans, not samples, so they can't be point series — they go in a JSONB column
    on the night's row. Timestamps are stored as ISO strings because JSON has no
    datetime, and the chart plots them as-is."""
    if not isinstance(rows, list) or not rows:
        return None
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        start = _gmt_moment(item.get("startGMT"))
        end = _gmt_moment(item.get("endGMT"))
        if start is None or end is None:
            continue
        out.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            out_key: _intish(item.get(value_key)),
        })
    # ISO strings sort chronologically as text; Garmin ships these in order, but
    # the hypnogram is drawn straight from this list so don't rely on it.
    out.sort(key=lambda s: s["start"])
    return out or None


def _normalize_sleep_stages(raw: dict) -> Optional[list]:
    """The hypnogram from ``sleepLevels`` — the stage code resolved to a name."""
    stages = _parse_sleep_intervals(_dig(raw, "sleep", "sleepLevels"), "activityLevel", "stage")
    if stages is None:
        return None
    for stage in stages:
        stage["stage"] = _SLEEP_STAGE_NAMES.get(stage["stage"], "unknown")
    return stages


def _normalize_breathing_events(raw: dict) -> Optional[list]:
    """``breathingDisruptionData`` — severity spans across the night. The
    undisturbed (``value`` 0) spans are kept too: dropping them would erase the
    difference between "measured and fine" and "never measured"."""
    return _parse_sleep_intervals(
        _dig(raw, "sleep", "breathingDisruptionData"), "value", "value"
    )


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


def _normalize_daily(raw: dict) -> dict:
    """Reduce the raw per-day sub-payload bundle to ``garmin_daily`` column values.
    Pure (no DB); every field defaults to None so a sparse day is fine."""
    summary = raw.get("summary") or {}
    sleep_dto = _dig(raw, "sleep", "dailySleepDTO") or {}
    hrv = _dig(raw, "hrv", "hrvSummary") or {}
    tr = raw.get("training_readiness")
    tr0 = tr[0] if isinstance(tr, list) and tr else (tr if isinstance(tr, dict) else {})
    mm = raw.get("max_metrics")
    mm0 = mm[0] if isinstance(mm, list) and mm else (mm if isinstance(mm, dict) else {})
    ts_map = _dig(raw, "training_status", "mostRecentTrainingStatus", "latestTrainingStatusData")
    ts0 = next(iter(ts_map.values()), {}) if isinstance(ts_map, dict) else {}
    if not isinstance(ts0, dict):
        ts0 = {}
    acute_load_dto = ts0.get("acuteTrainingLoadDTO") or {}

    return {
        # Sleep
        "sleep_seconds": _intish(sleep_dto.get("sleepTimeSeconds")),
        "sleep_score": _intish(_dig(sleep_dto, "sleepScores", "overall", "value")),
        "deep_sleep_seconds": _intish(sleep_dto.get("deepSleepSeconds")),
        "light_sleep_seconds": _intish(sleep_dto.get("lightSleepSeconds")),
        "rem_sleep_seconds": _intish(sleep_dto.get("remSleepSeconds")),
        "awake_seconds": _intish(sleep_dto.get("awakeSleepSeconds")),
        "sleep_start": _parse_sleep_boundary(sleep_dto, "sleepStart"),
        "sleep_end": _parse_sleep_boundary(sleep_dto, "sleepEnd"),
        "awake_count": _intish(sleep_dto.get("awakeCount")),
        "restless_moments": _intish(_first(
            sleep_dto.get("restlessMomentsCount"),
            _dig(raw, "sleep", "restlessMomentsCount"),
        )),
        "avg_sleep_stress": _intish(sleep_dto.get("avgSleepStress")),
        "avg_sleep_hr": _intish(sleep_dto.get("avgHeartRate")),
        "spo2_lowest": _intish(sleep_dto.get("lowestSpO2Value")),
        "respiration_lowest": _num(sleep_dto.get("lowestRespirationValue")),
        "respiration_highest": _num(sleep_dto.get("highestRespirationValue")),
        "body_battery_change": _intish(_dig(raw, "sleep", "bodyBatteryChange")),
        "breathing_disruption": sleep_dto.get("breathingDisruptionSeverity"),
        "sleep_need_actual": _intish(_first(
            _dig(sleep_dto, "nextSleepNeed", "actual"),
            _dig(raw, "sleep", "nextSleepNeed", "actual"),
        )),
        "sleep_stages": _normalize_sleep_stages(raw),
        "breathing_events": _normalize_breathing_events(raw),
        # Heart / HRV / respiration
        "resting_hr": _intish(_first(summary.get("restingHeartRate"), _dig(raw, "rhr", "restingHeartRate"))),
        "avg_hr": _intish(summary.get("averageHeartRate")),
        "max_hr": _intish(summary.get("maxHeartRate")),
        "min_hr": _intish(summary.get("minHeartRate")),
        "hrv_avg": _num(_first(hrv.get("lastNightAvg"), hrv.get("weeklyAvg"))),
        "hrv_status": hrv.get("status"),
        "avg_respiration": _num(summary.get("avgWakingRespirationValue")),
        "spo2_avg": _num(_first(summary.get("averageSpo2"), summary.get("averageSpo2Value"))),
        # Stress / Body Battery
        "avg_stress": _intish(summary.get("averageStressLevel")),
        "max_stress": _intish(summary.get("maxStressLevel")),
        "body_battery_high": _intish(summary.get("bodyBatteryHighestValue")),
        "body_battery_low": _intish(summary.get("bodyBatteryLowestValue")),
        # Activity / energy
        "steps": _intish(summary.get("totalSteps")),
        "floors_climbed": _intish(summary.get("floorsAscended")),
        "active_calories": _intish(summary.get("activeKilocalories")),
        "bmr_calories": _intish(summary.get("bmrKilocalories")),
        "total_calories": _intish(summary.get("totalKilocalories")),
        "intensity_minutes_moderate": _intish(summary.get("moderateIntensityMinutes")),
        "intensity_minutes_vigorous": _intish(summary.get("vigorousIntensityMinutes")),
        # Training
        "training_readiness": _intish(tr0.get("score") if isinstance(tr0, dict) else None),
        "vo2max": _num(_dig(mm0, "generic", "vo2MaxValue") if isinstance(mm0, dict) else None),
        "training_status": _strip_level_suffix(ts0.get("trainingStatusFeedbackPhrase")),
        "acute_load": _num(acute_load_dto.get("acuteTrainingLoad")),
        "load_ratio": _num(acute_load_dto.get("acwrPercent")),
    }


def _extract_weight_kg(raw: dict) -> Optional[float]:
    """Garmin weigh-in (grams) → kg, if the day had one."""
    grams = _first(
        _dig(raw, "body_composition", "totalAverage", "weight"),
        _dig(raw, "summary", "weight"),
    )
    kg = _num(grams)
    if kg is None:
        return None
    # Garmin reports weight in grams; guard the odd payload already in kg.
    return round(kg / 1000.0, 2) if kg > 1000 else round(kg, 2)


# ── Daily upsert ──────────────────────────────────────────────────────────────
async def get_daily(session: AsyncSession, on_date: date_type) -> Optional[GarminDaily]:
    result = await session.execute(select(GarminDaily).where(GarminDaily.date == on_date))
    return result.scalars().first()


async def _apply_owned_daily_raw(
    session: AsyncSession,
    on_date: date_type,
    *,
    raw_row: RawPayload,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    source: str,
    candidate: GarminDaily | None,
    prepared_weight_write: weight_service.PreparedWeightWrite | None,
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
        weight_row = await weight_service.log_weight(
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
    prepared_weight_write: weight_service.PreparedWeightWrite | None = None,
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
    from vitals.services import garmin_weight_service

    weight_kg = _extract_weight_kg(raw)
    if prepared_weight_write is None and weight_kg is not None:
        await acquire_identity_governance_lock(session)
        await _require_legacy_adoption_subject(
            session,
            subject_id=identity.subject_id,
        )
        prepared_weight_write = await weight_service.prepare_weight_write(
            session,
            context=_owned_weight_write_context(
                identity=identity,
                on_date=on_date,
            ),
            garmin_weight_export_context=(
                garmin_weight_service.GarminWeightExportContext(
                    identity=identity,
                    integration_connection_id=integration_connection_id,
                    legacy_bridge=(
                        engine.LegacyConflictBridge.FULLY_UNOWNED
                    ),
                )
            ),
        )
    elif prepared_weight_write is not None:
        prepared_context = weight_service.require_prepared_weight_identity(
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
        await garmin_weight_service.lock_active_weight_change(session)

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


def _owned_daily_date(raw_row: RawPayload) -> date_type:
    external_id = raw_row.external_id or ""
    if not external_id.startswith("daily:"):
        raise GarminRawPayloadInvariantError(
            "daily reparse requires a daily: external_id"
        )
    value = external_id.removeprefix("daily:")
    try:
        on_date = date_type.fromisoformat(value)
    except ValueError:
        raise GarminRawPayloadInvariantError(
            "daily raw payload has an invalid external date"
        ) from None
    if external_id != f"daily:{on_date.isoformat()}":
        raise GarminRawPayloadInvariantError(
            "daily raw payload external_id is not canonical"
        )
    return on_date


async def reparse_owned_daily_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
) -> GarminDaily:
    """Reparse a daily row from its own durable ``S+C`` raw root.

    Unlike fresh ingestion this accepts a retired connection: retirement closes
    new provider activity, not the ability to recover historical normalized data.
    The raw row is never re-upserted, so fetch time and historical actor remain
    unchanged.
    """

    on_date_hint = _owned_daily_date(raw_row)
    if raw_row.subject_id is None:
        raise GarminRawPayloadInvariantError(
            "owned daily reparse requires a subject root"
        )
    preliminary_identity = WriteIdentity(
        raw_row.subject_id,
        raw_row.actor_user_id,
    )
    from vitals.services import garmin_weight_service

    resolved_export = await garmin_weight_service.resolve_optional_legacy_export_context(
        session,
        actor_username=None,
    )
    prepared_weight_write = await weight_service.prepare_weight_write(
        session,
        context=_owned_weight_write_context(
            identity=preliminary_identity,
            on_date=on_date_hint,
        ),
        garmin_weight_export_context=(
            garmin_weight_service.GarminWeightExportContext(
                identity=preliminary_identity,
                integration_connection_id=resolved_export.integration_connection_id,
                legacy_bridge=resolved_export.legacy_bridge,
            )
            if resolved_export is not None
            else None
        ),
    )
    persisted, identity, connection_id = await _locked_owned_raw_context(
        session, raw_row
    )
    on_date = _owned_daily_date(persisted)
    if identity != preliminary_identity or on_date != on_date_hint:
        raise GarminRawPayloadInvariantError(
            "daily raw ownership changed during prepared reparse"
        )
    if persisted.source != Source.GARMIN_API.value:
        raise GarminRawPayloadInvariantError(
            "daily raw payload must use Garmin API provenance"
        )
    _validate_owned_raw_row(
        persisted,
        identity=identity,
        integration_connection_id=connection_id,
        source=Source.GARMIN_API.value,
        external_id=f"daily:{on_date.isoformat()}",
    )
    candidate = await _owned_single_row_candidate(
        session,
        model=GarminDaily,
        natural_clause=GarminDaily.date == on_date,
        identity=identity,
        integration_connection_id=connection_id,
        key_label=persisted.external_id,
    )
    if candidate is not None:
        _require_matching_normalized_raw_link(
            candidate,
            raw_payload_id=persisted.id,
        )
    return await _apply_owned_daily_raw(
        session,
        on_date,
        raw_row=persisted,
        identity=identity,
        integration_connection_id=connection_id,
        source=Source.GARMIN_API.value,
        candidate=candidate,
        prepared_weight_write=prepared_weight_write,
    )


def _owned_hae_date(raw_row: RawPayload) -> date_type:
    external_id = raw_row.external_id or ""
    if not external_id.startswith("hae:"):
        raise GarminRawPayloadInvariantError(
            "Health Auto Export reparse requires a hae: external_id"
        )
    value = external_id.removeprefix("hae:")
    try:
        on_date = date_type.fromisoformat(value)
    except ValueError:
        raise GarminRawPayloadInvariantError(
            "Health Auto Export raw payload has an invalid external date"
        ) from None
    if external_id != f"hae:{on_date.isoformat()}":
        raise GarminRawPayloadInvariantError(
            "Health Auto Export raw external_id is not canonical"
        )
    return on_date


async def reparse_owned_health_auto_export_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
) -> GarminDaily:
    """Reapply one owned HAE partial payload without clobbering other columns."""

    persisted, identity, connection_id = await _locked_owned_raw_context(
        session, raw_row
    )
    on_date = _owned_hae_date(persisted)
    _validate_owned_raw_row(
        persisted,
        identity=identity,
        integration_connection_id=connection_id,
        source=Source.HEALTH_AUTO_EXPORT.value,
        external_id=f"hae:{on_date.isoformat()}",
    )
    candidate = await _owned_single_row_candidate(
        session,
        model=GarminDaily,
        natural_clause=GarminDaily.date == on_date,
        identity=identity,
        integration_connection_id=connection_id,
        key_label=f"daily:{on_date.isoformat()}",
    )
    if candidate is not None:
        _require_matching_normalized_raw_link(
            candidate,
            raw_payload_id=persisted.id,
        )
    return await _apply_owned_hae_raw(
        session,
        on_date,
        raw_row=persisted,
        identity=identity,
        integration_connection_id=connection_id,
        candidate=candidate,
    )


# ── Activities ────────────────────────────────────────────────────────────────
def _activity_external_id(raw: dict) -> str:
    return str(raw.get("activityId") or raw.get("activityid") or "").strip()


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


def _owned_activity_id(raw_row: RawPayload) -> str:
    external_id = raw_row.external_id or ""
    if not external_id.startswith("activity:"):
        raise GarminRawPayloadInvariantError(
            "activity reparse requires an activity: external_id"
        )
    value = external_id.removeprefix("activity:")
    if not value or external_id != f"activity:{value}":
        raise GarminRawPayloadInvariantError(
            "activity raw payload external_id is not canonical"
        )
    payload_id = _activity_external_id(raw_row.payload)
    if payload_id != value:
        raise GarminRawPayloadInvariantError(
            "activity payload id does not match raw external_id"
        )
    return value


async def reparse_owned_activity_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
) -> GarminActivity:
    """Reparse an activity without re-upserting or rebinding its raw root."""

    persisted, identity, connection_id = await _locked_owned_raw_context(
        session, raw_row
    )
    _validate_owned_raw_row(
        persisted,
        identity=identity,
        integration_connection_id=connection_id,
        source=Source.GARMIN_API.value,
        external_id=persisted.external_id or "",
    )
    external_id = _owned_activity_id(persisted)
    candidate = await _owned_single_row_candidate(
        session,
        model=GarminActivity,
        natural_clause=GarminActivity.external_id == external_id,
        identity=identity,
        integration_connection_id=connection_id,
        key_label=persisted.external_id,
    )
    if candidate is not None:
        _require_matching_normalized_raw_link(
            candidate,
            raw_payload_id=persisted.id,
        )
    return await _apply_owned_activity_raw(
        session,
        raw_row=persisted,
        external_id=external_id,
        identity=identity,
        integration_connection_id=connection_id,
        candidate=candidate,
    )


async def reparse_owned_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
) -> None:
    """Dispatch a durable owned raw root without accepting caller-owned context."""

    external_id = raw_row.external_id or ""
    if external_id.startswith("daily:"):
        await reparse_owned_daily_from_raw(session, raw_row)
    elif external_id.startswith("hae:"):
        await reparse_owned_health_auto_export_from_raw(session, raw_row)
    elif external_id.startswith("activity:"):
        await reparse_owned_activity_from_raw(session, raw_row)
    else:
        raise GarminRawPayloadInvariantError(
            f"unrecognized owned Garmin raw external_id: {external_id!r}"
        )


async def reparse_owned_pending(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    limit: int = raw_payload_service.REPARSE_BATCH,
    since_days: int = raw_payload_service.REPARSE_WINDOW_DAYS,
) -> int:
    """Sweep one explicit Garmin scope, isolating every reparse in a savepoint."""

    await _load_owned_garmin_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=True,
    )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise GarminOwnershipValidationError("limit must be a positive integer")
    if (
        not isinstance(since_days, int)
        or isinstance(since_days, bool)
        or since_days < 0
    ):
        raise GarminOwnershipValidationError(
            "since_days must be a non-negative integer"
        )

    has_normalized = or_(
        select(GarminDaily.id)
        .where(
            GarminDaily.raw_payload_id == RawPayload.id,
            GarminDaily.subject_id == RawPayload.subject_id,
            GarminDaily.integration_connection_id
            == RawPayload.integration_connection_id,
        )
        .exists(),
        select(GarminActivity.id)
        .where(
            GarminActivity.raw_payload_id == RawPayload.id,
            GarminActivity.subject_id == RawPayload.subject_id,
            GarminActivity.integration_connection_id
            == RawPayload.integration_connection_id,
        )
        .exists(),
    )
    cutoff = now_local() - timedelta(days=since_days)
    stmt = (
        select(RawPayload.id)
        .where(
            RawPayload.subject_id == identity.subject_id,
            RawPayload.integration_connection_id == integration_connection_id,
            RawPayload.domain == DOMAIN,
            RawPayload.source.in_(
                [
                    Source.GARMIN_API.value,
                    Source.HEALTH_AUTO_EXPORT.value,
                ]
            ),
            RawPayload.processed_at.is_(None),
            RawPayload.fetched_at >= cutoff,
            ~has_normalized,
        )
        .order_by(RawPayload.id)
        .limit(limit)
    )
    raw_ids = list(await session.scalars(stmt))
    done = 0
    for raw_row_id in raw_ids:
        try:
            async with session.begin_nested():
                # Candidate discovery must not retain a raw-row lock before the
                # callback establishes its domain lock order. In particular, a
                # daily payload may contain Weight and therefore has to acquire
                # governance -> active-weight advisory -> subject/connection
                # before it authoritatively reloads and locks this raw row.
                raw_row = await session.scalar(
                    select(RawPayload)
                    .where(RawPayload.id == raw_row_id)
                    .execution_options(populate_existing=True)
                )
                if raw_row is None:
                    raise GarminRawPayloadInvariantError(
                        "raw payload disappeared during reparse sweep"
                    )
                # The query scopes candidates, and the reparse callback reloads the
                # durable row and derives its historical actor. Check the supplied
                # boundary too so a stale/mutated ORM object cannot switch targets.
                if (
                    raw_row.subject_id != identity.subject_id
                    or raw_row.integration_connection_id
                    != integration_connection_id
                ):
                    raise GarminRawPayloadInvariantError(
                        "raw payload changed ownership during reparse sweep"
                    )
                await reparse_owned_from_raw(session, raw_row)
                raw_row.processed_at = now_local()
                await session.flush()
        except Exception:
            logger.warning(
                "owned Garmin re-parse failed for raw payload %s",
                raw_row_id,
                exc_info=True,
            )
            continue
        done += 1
    return done


def _normalize_hr_zones(raw: dict) -> Optional[list]:
    """Seconds-in-HR-zone as a compact array. Prefers the per-activity
    ``get_activity_hr_zones`` detail (carries each zone's low HR boundary); falls
    back to the ``hrTimeInZone_N`` fields already on the activity summary."""
    detail = _dig(raw, "_details", "hr_zones")
    if isinstance(detail, list) and detail:
        out = [
            {
                "zone": _intish(z.get("zoneNumber")),
                "secs": _num(z.get("secsInZone")),
                "low_hr": _intish(z.get("zoneLowBoundary")),
            }
            for z in detail
            if isinstance(z, dict)
        ]
        if out:
            return out
    fallback = [
        {"zone": n, "secs": _num(raw.get(f"hrTimeInZone_{n}")), "low_hr": None}
        for n in range(1, 6)
        if raw.get(f"hrTimeInZone_{n}") is not None
    ]
    return fallback or None


def _normalize_splits(raw: dict) -> Optional[list]:
    """Per-lap splits from the ``get_activity_splits`` detail (``lapDTOs``). Only
    outdoor/interval activities carry more than one lap; strength has none."""
    laps = _dig(raw, "_details", "splits", "lapDTOs")
    if not isinstance(laps, list) or not laps:
        return None
    out = [
        {
            "index": _intish(lap.get("lapIndex")),
            "distance_m": _num(lap.get("distance")),
            "duration_s": _num(lap.get("duration")),
            "avg_hr": _intish(lap.get("averageHR")),
            "max_hr": _intish(lap.get("maxHR")),
            "avg_speed_mps": _num(lap.get("averageSpeed")),
        }
        for lap in laps
        if isinstance(lap, dict)
    ]
    return out or None


async def _enrich_activity_details(client: Any, activities: Sequence[dict]) -> None:
    """Fetch each in-window activity's detail bundle (HR zones + splits) and merge
    it under a synthetic ``_details`` key so the whole thing lands in
    ``raw_payloads`` and the normalizers can read it. Best-effort and bounded —
    only the handful of activities in the sync window. A client without the
    method (or a failing call) leaves the activity detail-less, not broken."""
    fetch = getattr(client, "fetch_activity_details", None)
    if not callable(fetch):
        return
    for act in activities:
        if not isinstance(act, dict):
            continue
        activity_id = act.get("activityId") or act.get("activityid")
        if activity_id is None:
            continue
        try:
            act["_details"] = await fetch(activity_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Garmin activity-detail fetch failed for %s: %s", activity_id, e)


def _parse_activity_start(raw: dict) -> Optional[datetime]:
    for key in ("startTimeGMT", "startTimeLocal"):
        value = raw.get(key)
        if value:
            try:
                text = str(value).replace("Z", "+00:00")
                return to_local_naive(datetime.fromisoformat(text))
            except (ValueError, TypeError):
                continue
    return None


# ── Sync orchestration ────────────────────────────────────────────────────────
async def refresh_token_cache_alert(
    session: AsyncSession,
    client: Any,
    *,
    resolve_if_clear: bool = True,
) -> None:
    """Surface token-store failures collected by any Garmin client operation."""
    warnings = list(getattr(client, "token_warnings", None) or ())
    if warnings:
        await alerts_service.raise_alert(
            session,
            domain=DOMAIN,
            severity=Severity.WARN.value,
            message=t("alert.garmin_token_cache", error=warnings[0]),
            alert_key=TOKEN_ALERT_KEY,
        )
    elif resolve_if_clear:
        await alerts_service.resolve_by_key(session, alert_key=TOKEN_ALERT_KEY)


def _owned_provider_alert_context(
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> alerts_service.ProviderAlertContext:
    """Bind operational Garmin alerts to the account, never to a human actor."""

    _validate_owned_context(identity, integration_connection_id)
    return alerts_service.ProviderAlertContext(
        identity=WriteIdentity(subject_id=identity.subject_id, actor_user_id=None),
        provider=IntegrationProvider.GARMIN,
        integration_connection_id=integration_connection_id,
    )


async def _refresh_owned_token_cache_alert(
    session: AsyncSession,
    client: Any,
    *,
    context: alerts_service.ProviderAlertContext,
    resolve_if_clear: bool = True,
) -> None:
    """Owned counterpart of the legacy helper retained by weight-export code."""

    warnings = list(getattr(client, "token_warnings", None) or ())
    if warnings:
        await alerts_service.raise_scoped_alert(
            session,
            context=context,
            domain=Domain.GARMIN,
            severity=Severity.WARN,
            message=t("alert.garmin_token_cache", error=warnings[0]),
            alert_key=TOKEN_ALERT_KEY,
            legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
        )
    elif resolve_if_clear:
        await alerts_service.resolve_scoped_by_key(
            session,
            context=context,
            alert_key=TOKEN_ALERT_KEY,
            legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
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
    """Run a Garmin pull into one explicit subject/account scope.

    The fail-closed connection preflight is a database read before vendor I/O,
    so the caller's transaction remains open during the fetch. A later network /
    persistence split should remove that pool-pressure tradeoff before broad
    multi-user activation.
    """

    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise GarminOwnershipValidationError("days must be a positive integer")
    await _load_owned_garmin_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    alert_context = _owned_provider_alert_context(
        identity=identity,
        integration_connection_id=integration_connection_id,
    )

    today = on_date or now_local().date()
    start = today - timedelta(days=days - 1)
    summary = {"days": 0, "activities": 0, "error": None}
    daily_payloads: list[tuple[date_type, dict]] = []
    activities: Optional[Sequence[dict]] = None
    auth_error: Optional[GarminAuthError] = None

    # Resolve/validate ownership before this complete vendor-I/O phase. As in the
    # legacy path, no database mutation or row/advisory lock spans network latency.
    try:
        for offset in range(days):
            day = start + timedelta(days=offset)
            raw = await client.fetch_daily(day)
            daily_payloads.append((day, raw))
        activities = await client.fetch_activities(start, today)
        await _enrich_activity_details(client, activities)
    except GarminAuthError as exc:
        auth_error = exc

    # Alert legacy adoption and owned ingestion both lock identity/tenancy roots.
    # Acquire the shared governance lock after vendor I/O but before either root
    # lock so every path follows governance -> subject -> connection.
    await acquire_identity_governance_lock(session)

    for day, raw in daily_payloads:
        await ingest_owned_daily(
            session,
            day,
            raw,
            identity=identity,
            integration_connection_id=integration_connection_id,
        )
        summary["days"] += 1
    if activities is not None:
        summary["activities"] = await ingest_owned_activities(
            session,
            activities,
            identity=identity,
            integration_connection_id=integration_connection_id,
        )

    if auth_error is None:
        await alerts_service.resolve_scoped_by_key(
            session,
            context=alert_context,
            alert_key=AUTH_ALERT_KEY,
            legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
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
        await alerts_service.raise_scoped_alert(
            session,
            context=alert_context,
            domain=Domain.GARMIN,
            severity=Severity.WARN,
            message=message,
            alert_key=AUTH_ALERT_KEY,
            legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
        )

    await _refresh_owned_token_cache_alert(
        session,
        client,
        context=alert_context,
    )
    return summary


# ── Light pulse (N3) ──────────────────────────────────────────────────────────
# Outside the active hours on the settings card the pulse doesn't run: nothing it
# reads (steps, active calories, intensity minutes) moves while he's asleep, and
# every skipped poll is one fewer chance to spend a login on a night nobody reads.


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
            session, subject_id=identity.subject_id
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
            session, subject_id=identity.subject_id
        )
    if (
        raw_row.domain != DOMAIN
        or raw_row.source
        not in {
            Source.GARMIN_API.value,
            Source.HEALTH_AUTO_EXPORT.value,
        }
        or raw_row.external_id
        != (
            f"hae:{on_date.isoformat()}"
            if raw_row.source == Source.HEALTH_AUTO_EXPORT.value
            else f"daily:{on_date.isoformat()}"
        )
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

    # Fetch first, then serialize and read the latest stored bundle. Reading the
    # base before the await lets a concurrent full sync commit newer sleep/HRV
    # data which this lightweight pulse would subsequently overwrite as stale.
    from vitals.services import garmin_weight_service

    prepared_weight_write = await weight_service.prepare_weight_write(
        session,
        context=_owned_weight_write_context(
            identity=identity,
            on_date=day,
        ),
        garmin_weight_export_context=(
            garmin_weight_service.GarminWeightExportContext(
                identity=identity,
                integration_connection_id=integration_connection_id,
                legacy_bridge=(
                    engine.LegacyConflictBridge.FULLY_UNOWNED
                ),
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
    row = await ingest_owned_daily(
        session,
        day,
        raw,
        identity=identity,
        integration_connection_id=integration_connection_id,
        prepared_weight_write=prepared_weight_write,
    )
    out["steps"] = row.steps
    return out


async def pulse_job(
    session_factory,
    redis=None,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
) -> None:
    """The light pulse on its own interval (both from the settings card).

    Cheap by construction, but it still opens a Garmin session — which is safe
    only because the credential-login breaker rations logins. No-ops when Garmin
    isn't configured, when the pulse is switched off, or outside active hours.

    The active-hours check is here rather than in the trigger because APScheduler
    intervals have no concept of a window, and because a saved setting must apply
    to the *next* tick without touching the job."""
    from vitals.integrations.garmin_client import GarminClient
    from vitals.services.legacy_ownership import (
        LegacyOwnershipError,
        LegacySubjectResolutionError,
        resolve_subject_ownership_context,
    )
    from vitals.services.proactive import prefs

    del integration_connection_id  # named by the fan-out; resolved below

    async with session_factory() as session:
        try:
            ownership = await resolve_subject_ownership_context(
                session,
                subject_id=subject_id,
                required_connections=(IntegrationProvider.GARMIN,),
            )
        except LegacySubjectResolutionError:
            # A pulse writes a day of somebody's watch data. Without a subject
            # there is nobody to write it for, and the pre-identity arm that
            # used to run here wrote rows belonging to no one.
            logger.warning(
                "Garmin pulse skipped: no single health subject to sync for",
                exc_info=True,
            )
            return
        except LegacyOwnershipError:
            logger.warning(
                "Garmin pulse skipped: legacy ownership is unavailable",
                exc_info=True,
            )
            return
        try:
            policy = await prefs.get_garmin_policy(
                session,
                subject_id=ownership.subject_id,
                integration_connection_id=ownership.connection_id(
                    IntegrationProvider.GARMIN
                ),
            )
        except prefs.ProactivePreferencesError:
            logger.warning(
                "Garmin pulse skipped: scoped preferences are unavailable",
                exc_info=True,
            )
            return
        if not policy.pulse_seconds:
            return
        if not policy.pulse_start_hour <= now_local().hour < policy.pulse_end_hour:
            return

        # This subject's watch. Built from the resolved account rather than
        # from ``load_config()``, which is the installation's single Garmin —
        # polling it under somebody else's ownership would file the operator's
        # step count as that patient's.
        account = await providers.resolve_garmin_account(
            session, subject_id=ownership.subject_id
        )
        if account is None or not account.configured:
            return
        client = GarminClient.from_config(account.config, redis)
        try:
            await pulse_owned(
                session,
                client,
                identity=ownership.write_identity,
                integration_connection_id=ownership.connection_id(
                    IntegrationProvider.GARMIN
                ),
            )
        except GarminConnectionInactiveError:
            logger.info("Garmin pulse skipped: connection is not active")
            await session.rollback()
            return
        await session.commit()


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


def _parse_hae_date(value: Any) -> Optional[date_type]:
    if not value:
        return None
    text = str(value)
    # HAE dates look like "2026-06-10 00:00:00 +0000" — take the date prefix.
    try:
        return date_type.fromisoformat(text[:10])
    except ValueError:
        return None


# ── Reads / advice ────────────────────────────────────────────────────────────
def recovery_advice(daily: Optional[GarminDaily]) -> Optional[str]:
    """Passive recovery hint for the training block, or None when recovery is fine."""
    if daily is None:
        return None
    notes: list[str] = []
    if daily.sleep_score is not None and daily.sleep_score < SLEEP_SCORE_FLOOR:
        notes.append(t("alert.recovery_sleep", score=daily.sleep_score))
    if daily.body_battery_high is not None and daily.body_battery_high < BODY_BATTERY_FLOOR:
        notes.append(t("alert.recovery_battery", value=daily.body_battery_high))
    if daily.spo2_lowest is not None and daily.spo2_lowest < SPO2_FLOOR:
        notes.append(t("alert.recovery_spo2", value=daily.spo2_lowest))
    if daily.breathing_disruption and daily.breathing_disruption != "NONE":
        notes.append(t("alert.recovery_breathing"))
    if not notes:
        return None
    return t("alert.recovery_prefix") + ", ".join(notes) + t("alert.recovery_suffix")


async def list_daily(
    session: AsyncSession, *, limit: int = 30
) -> Sequence[GarminDaily]:
    result = await session.execute(
        select(GarminDaily).order_by(GarminDaily.date.desc()).limit(limit)
    )
    return result.scalars().all()


async def list_daily_between(
    session: AsyncSession,
    start: date_type,
    end: date_type,
    *,
    subject_id: uuid.UUID,
) -> Sequence[GarminDaily]:
    """Every day in a date range, chronological. ``list_daily`` counts backwards
    from the newest row, which answers "the last N days I have" — not "the days
    between these two dates", the question every period report actually asks.

    ``subject_id`` is required: a watch belongs to a person, so "the days" is
    always somebody's days. Rows the backfill has not stamped yet are simply not
    theirs to show, and the range comes back short rather than borrowed."""
    stmt = (
        select(GarminDaily)
        .where(
            GarminDaily.subject_id == subject_id,
            GarminDaily.date >= start,
            GarminDaily.date <= end,
        )
        .order_by(GarminDaily.date)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_nights(
    session: AsyncSession, *, limit: int = 60
) -> Sequence[GarminDaily]:
    """Days with a recorded sleep session, newest first — the Sleep tab's feed.
    Unlike ``list_daily``, a day synced with no sleep data (steps/HR only) is
    excluded rather than showing up as a noise row in the night list."""
    result = await session.execute(
        select(GarminDaily)
        .where(GarminDaily.sleep_seconds.is_not(None))
        .order_by(GarminDaily.date.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def list_activities(
    session: AsyncSession, *, limit: int = 20
) -> Sequence[GarminActivity]:
    result = await session.execute(
        select(GarminActivity).order_by(GarminActivity.date.desc(), GarminActivity.start_time.desc()).limit(limit)
    )
    return result.scalars().all()


async def list_intraday(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    series_types: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> Sequence[GarminIntraday]:
    """Intraday samples over a date window, oldest first (a curve reads in time
    order). A day holds ~480 samples per series, so callers cap the window."""
    stmt = select(GarminIntraday)
    if start is not None:
        stmt = stmt.where(GarminIntraday.date >= start)
    if end is not None:
        stmt = stmt.where(GarminIntraday.date <= end)
    if series_types:
        stmt = stmt.where(GarminIntraday.series_type.in_(list(series_types)))
    stmt = stmt.order_by(GarminIntraday.ts, GarminIntraday.series_type)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def intraday_series_map(
    session: AsyncSession,
    on_date: date_type,
    *,
    series_types: Optional[Sequence[str]] = None,
) -> dict[str, list[dict]]:
    """One day's curves as ``{series_type: [{"ts", "value"}, …]}`` — the shape the
    dashboard chart and the MCP tool both consume. Series with no samples are
    absent rather than empty, so a caller can just check for the key."""
    rows = await list_intraday(session, start=on_date, end=on_date, series_types=series_types)
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row.series_type, []).append(
            {"ts": row.ts.isoformat(), "value": row.value}
        )
    return out


# What has to be on a row before it counts as a day the watch reported. The
# sync writes a row the moment the date turns, and at half past midnight every
# one of these is still null — a placeholder, not a day. Returned as "the latest
# day" it turned /garmin and /today into a screen of dashes while yesterday's
# complete row sat one place behind it.
_REPORTED_DAILY_COLS = (
    GarminDaily.sleep_score,
    GarminDaily.sleep_seconds,
    GarminDaily.resting_hr,
    GarminDaily.hrv_avg,
    GarminDaily.body_battery_high,
    GarminDaily.avg_stress,
    GarminDaily.steps,
    GarminDaily.active_calories,
)


async def latest_daily(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    before_or_on: Optional[date_type] = None,
) -> Optional[GarminDaily]:
    """The newest day that actually carries numbers, not merely the newest row.

    Every caller is a screen or a message that reads the row's metrics, so an
    empty placeholder is never the answer any of them wants — including the
    silence nudge, for which a row with nothing on it is exactly the silence it
    is looking for. The placeholder is still stored and still shows up in the
    history list; it just stops being "the latest day".

    ``subject_id`` is required for the same reason it is on ``list_daily_between``:
    every screen this feeds is one person's recovery, and "the newest row in the
    database" is not an answer to that question.
    """
    stmt = select(GarminDaily).where(
        GarminDaily.subject_id == subject_id,
        or_(*(col.is_not(None) for col in _REPORTED_DAILY_COLS)),
    )
    if before_or_on is not None:
        stmt = stmt.where(GarminDaily.date <= before_or_on)
    stmt = stmt.order_by(GarminDaily.date.desc()).limit(1)
    result = await session.execute(stmt)
    return result.scalars().first()


async def adjacent_night_dates(
    session: AsyncSession, on_date: date_type
) -> tuple[Optional[date_type], Optional[date_type]]:
    """The nearest earlier/later dates that also have a recorded sleep session —
    feeds the night page's ‹ previous / next › links. Either side is ``None``
    when there's no such neighbour."""
    prev_result = await session.execute(
        select(GarminDaily.date)
        .where(GarminDaily.sleep_seconds.is_not(None), GarminDaily.date < on_date)
        .order_by(GarminDaily.date.desc())
        .limit(1)
    )
    next_result = await session.execute(
        select(GarminDaily.date)
        .where(GarminDaily.sleep_seconds.is_not(None), GarminDaily.date > on_date)
        .order_by(GarminDaily.date.asc())
        .limit(1)
    )
    return prev_result.scalar(), next_result.scalar()


async def daily_count(session: AsyncSession) -> int:
    """Count days with at least one real metric (excludes ghost rows from initial sync)."""
    from sqlalchemy import or_
    result = await session.execute(
        select(func.count()).select_from(GarminDaily).where(
            or_(
                GarminDaily.sleep_score.is_not(None),
                GarminDaily.resting_hr.is_not(None),
                GarminDaily.hrv_avg.is_not(None),
            )
        )
    )
    return int(result.scalar() or 0)


# ── Scheduler job ─────────────────────────────────────────────────────────────
async def sync_now_for_actor(
    session_factory,
    redis=None,
    *,
    actor_username: str,
    days: int = 2,
) -> Optional[dict]:
    """"Sync my Garmin now", for a person who asked through MCP.

    A separate entry point rather than an optional argument on :func:`sync_job`.
    An omittable scope is the shape ``vitals/legacy_scope.py`` exists to keep
    out, and the two callers mean genuinely different things: this one resolves
    the record the *actor* owns, and the scheduler names the record directly.
    """

    from vitals.services.legacy_ownership import resolve_legacy_ownership_context

    async with session_factory() as session:
        ownership = await resolve_legacy_ownership_context(
            session,
            actor_username=actor_username,
            required_connections=(IntegrationProvider.GARMIN,),
        )
        subject_id = ownership.subject_id
        actor_user_id = ownership.actor_user_id
    return await sync_job(
        session_factory,
        redis,
        days=days,
        subject_id=subject_id,
        actor_user_id=actor_user_id,
    )


async def sync_job(
    session_factory,
    redis=None,
    *,
    days: int = 2,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Optional[dict]:
    """Garmin poll (registered in vitals/scheduler/jobs.py). No-ops cleanly when
    this record has no Garmin account — returns None in that case, else the sync
    summary.

    ``subject_id`` is mandatory, and deliberately: it used to be absent, so the
    resolver was asked for "the sole subject, or refuse" and the whole job
    stopped on a two-person installation. The fan-out passes it once per
    configured account.

    ``actor_user_id`` is attribution rather than scope — whose *request* this
    was, not whose record. A scheduled run leaves it unset and the rows belong
    to the account; :func:`sync_now_for_actor` fills it in when a person asked.
    """
    from vitals.integrations.garmin_client import GarminClient
    from vitals.services.legacy_ownership import resolve_subject_ownership_context

    del integration_connection_id  # named by the fan-out; resolved below

    async with session_factory() as session:
        from vitals.services.language_service import get_language
        from vitals.i18n import current_lang

        # Ownership before the client, which is a reordering and not a tidy-up.
        # The client used to be built from ``load_config()`` before anything had
        # said whose record this run was for, so "is Garmin configured" was a
        # question about the installation. It is a question about the account.
        ownership = await resolve_subject_ownership_context(
            session,
            subject_id=subject_id,
            required_connections=(IntegrationProvider.GARMIN,),
        )
        account = await providers.resolve_garmin_account(
            session, subject_id=ownership.subject_id
        )
        if account is None or not account.configured:
            await session.rollback()
            return None
        client = GarminClient.from_config(account.config, redis)
        lang = await get_language(
            session,
            redis,
            user_id=ownership.owner_user_id,
        )
        current_lang.set(lang)

        try:
            summary = await sync_owned(
                session,
                client,
                # Attributed to the person who asked, when one did; a
                # scheduled run leaves it the account's.
                identity=(
                    WriteIdentity(ownership.subject_id, actor_user_id)
                    if actor_user_id is not None
                    else ownership.write_identity
                ),
                integration_connection_id=ownership.connection_id(
                    IntegrationProvider.GARMIN
                ),
                days=days,
            )
        except GarminConnectionInactiveError:
            logger.info("Garmin sync skipped: connection is not active")
            await session.rollback()
            return None
        await session.commit()
        if redis is not None and summary.get("error") is None:
            import time
            await redis.set(
                providers.sync_marker_key(
                    IntegrationProvider.GARMIN, account.namespace
                ),
                str(int(time.time())),
            )
        return summary
