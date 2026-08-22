"""Typed, ownership-scoped preferences for proactive delivery and sync work.

The legacy application stored twelve unrelated knobs in one global
``app_settings['proactive']`` JSON object. That shape is unsafe once two health
subjects or two provider connections exist. The commercial cutover splits it
into three non-secret control-plane rows:

* subject schedule/content policy (brief, evening, nudge categories);
* Telegram recipient policy (quiet hours and daily initiative budget);
* Garmin connection policy (sync, pulse, and weight-export cadence).

Normal runtime reads are strict and new-only. They never consult the global row
and never turn a database/scope failure into permissive defaults. The sole
legacy owner is split at startup under the identity-governance lock, before any
scheduler or sender can run. While that exact-one bridge is active, a settings
save mirrors the normalized aggregate back to ``AppSetting`` atomically. A
multi-subject save writes scoped rows only.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import time as time_type
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.app_settings import AppSetting
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import (
    IntegrationConnectionSetting,
    SubjectSetting,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity_service import (
    IdentityValidationError,
    PreIdentityCompatibilityError,
    acquire_identity_governance_lock,
    authorize_pre_identity_compatibility_transaction,
    normalize_username,
    require_pre_identity_compatibility,
)

LEGACY_SETTINGS_KEY = "proactive"
SUBJECT_POLICY_KEY = "proactive_subject_policy"
TELEGRAM_DELIVERY_POLICY_KEY = "proactive_delivery_policy"
GARMIN_POLICY_KEY = "garmin_proactive_policy"

# Compatibility label only. Scoped runtime APIs never use this global key.
SETTINGS_KEY = LEGACY_SETTINGS_KEY

# The subject module that acts as the proactive emergency switch.
MODULE_KEY = "signals"

CATEGORY_ACTIVITY = "activity"
CATEGORY_NUTRITION = "nutrition"
CATEGORY_DATA = "data"
NUDGE_CATEGORIES: tuple[str, ...] = (
    CATEGORY_ACTIVITY,
    CATEGORY_NUTRITION,
    CATEGORY_DATA,
)

BUDGET_RANGE = (1, 12)
SYNC_HOURS_RANGE = (1, 24)
WEIGHT_EXPORT_MINUTES_RANGE = (5, 1440)
WEIGHT_MAX_AGE_DAYS_RANGE = (1, 30)
PULSE_SECONDS_RANGE = (60, 3600)

DEFAULTS: dict[str, Any] = {
    "brief_time": "11:00",
    "evening_time": "23:45",
    "quiet_start": "02:00",
    "quiet_end": "10:00",
    "daily_budget": 4,
    "garmin_sync_hours": 6,
    "garmin_weight_export_minutes": 15,
    "garmin_weight_max_age_days": 30,
    "pulse_seconds": 900,
    "pulse_start_hour": 8,
    "pulse_end_hour": 24,
    "nudges": {category: True for category in NUDGE_CATEGORIES},
}

_SUBJECT_FIELDS = frozenset({"brief_time", "evening_time", "nudges"})
_DELIVERY_FIELDS = frozenset({"quiet_start", "quiet_end", "daily_budget"})
_GARMIN_FIELDS = frozenset(
    {
        "garmin_sync_hours",
        "garmin_weight_export_minutes",
        "garmin_weight_max_age_days",
        "pulse_seconds",
        "pulse_start_hour",
        "pulse_end_hour",
    }
)
_LIVE_CONNECTION_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }
)
_NON_RETIRED_CONNECTION_STATUSES = frozenset(
    status.value
    for status in IntegrationConnectionStatus
    if status is not IntegrationConnectionStatus.RETIRED
)


class ProactivePreferencesError(RuntimeError):
    """Base class for scoped proactive preference failures."""


class ProactivePreferencesValidationError(ValueError):
    """Caller input or a persisted preference value is invalid."""


class ProactivePreferencesUnavailableError(ProactivePreferencesError):
    """A strict runtime policy cannot be proved from complete scoped rows."""


class ProactivePreferencesScopeError(ProactivePreferencesError):
    """The supplied subject, recipient, or connection graph is not exact."""


class ProactivePreferencesDriftError(ProactivePreferencesError):
    """Legacy and already-split values disagree during exact-one bootstrap."""


class LegacyProactivePreferencesBridgeClosedError(ProactivePreferencesError):
    """A requested legacy mirror is no longer safe for the identity graph."""


@dataclass(frozen=True, slots=True)
class ProactivePreferencesScope:
    """Exact roots needed to read or replace the three policy partitions."""

    subject_id: uuid.UUID
    recipient_user_id: uuid.UUID
    telegram_connection_id: uuid.UUID
    garmin_connection_id: uuid.UUID
    include_legacy: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "subject_id",
            "recipient_user_id",
            "telegram_connection_id",
            "garmin_connection_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, uuid.UUID) or value.int == 0:
                raise ProactivePreferencesValidationError(
                    f"{field_name} must be a non-zero UUID"
                )
        if not isinstance(self.include_legacy, bool):
            raise ProactivePreferencesValidationError(
                "include_legacy must be a bool"
            )
        if self.telegram_connection_id == self.garmin_connection_id:
            raise ProactivePreferencesValidationError(
                "Telegram and Garmin connections must be distinct"
            )


@dataclass(frozen=True, slots=True)
class SubjectProactivePolicy:
    brief_time: time_type
    evening_time: time_type
    enabled_nudge_categories: frozenset[str]


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    quiet_start: time_type
    quiet_end: time_type
    daily_budget: int


@dataclass(frozen=True, slots=True)
class GarminProactivePolicy:
    sync_hours: int
    weight_export_minutes: int
    weight_max_age_days: int
    pulse_seconds: int
    pulse_start_hour: int
    pulse_end_hour: int


@dataclass(frozen=True, slots=True)
class ProactivePreferencesBundle:
    subject: SubjectProactivePolicy
    delivery: DeliveryPolicy
    garmin: GarminProactivePolicy

    def as_flat_dict(self) -> dict[str, Any]:
        """Return the stable legacy/UI projection without shared mutable state."""

        enabled = self.subject.enabled_nudge_categories
        return {
            "brief_time": self.subject.brief_time.strftime("%H:%M"),
            "evening_time": self.subject.evening_time.strftime("%H:%M"),
            "quiet_start": self.delivery.quiet_start.strftime("%H:%M"),
            "quiet_end": self.delivery.quiet_end.strftime("%H:%M"),
            "daily_budget": self.delivery.daily_budget,
            "garmin_sync_hours": self.garmin.sync_hours,
            "garmin_weight_export_minutes": self.garmin.weight_export_minutes,
            "garmin_weight_max_age_days": self.garmin.weight_max_age_days,
            "pulse_seconds": self.garmin.pulse_seconds,
            "pulse_start_hour": self.garmin.pulse_start_hour,
            "pulse_end_hour": self.garmin.pulse_end_hour,
            "nudges": {
                category: category in enabled for category in NUDGE_CATEGORIES
            },
        }


def _time(raw: Any, default: str) -> str:
    try:
        return time_type.fromisoformat(str(raw)).strftime("%H:%M")
    except (TypeError, ValueError):
        return default


def _int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw)))
    except (TypeError, ValueError):
        return default


def sanitize(raw: Any) -> dict[str, Any]:
    """Normalize untrusted legacy/form input into one complete flat value."""

    src = raw if isinstance(raw, dict) else {}
    stored_nudges = src.get("nudges")
    if not isinstance(stored_nudges, dict):
        stored_nudges = {}

    pulse_raw = src.get("pulse_seconds", DEFAULTS["pulse_seconds"])
    try:
        pulse = int(pulse_raw)
    except (TypeError, ValueError):
        pulse = DEFAULTS["pulse_seconds"]
    pulse = (
        0
        if pulse <= 0
        else max(PULSE_SECONDS_RANGE[0], min(PULSE_SECONDS_RANGE[1], pulse))
    )

    start_hour = _int(
        src.get("pulse_start_hour"), DEFAULTS["pulse_start_hour"], 0, 23
    )
    end_hour = _int(
        src.get("pulse_end_hour"), DEFAULTS["pulse_end_hour"], 1, 24
    )
    end_hour = max(end_hour, start_hour + 1)

    return {
        "brief_time": _time(src.get("brief_time"), DEFAULTS["brief_time"]),
        "evening_time": _time(
            src.get("evening_time"), DEFAULTS["evening_time"]
        ),
        "quiet_start": _time(src.get("quiet_start"), DEFAULTS["quiet_start"]),
        "quiet_end": _time(src.get("quiet_end"), DEFAULTS["quiet_end"]),
        "daily_budget": _int(
            src.get("daily_budget"), DEFAULTS["daily_budget"], *BUDGET_RANGE
        ),
        "garmin_sync_hours": _int(
            src.get("garmin_sync_hours"),
            DEFAULTS["garmin_sync_hours"],
            *SYNC_HOURS_RANGE,
        ),
        "garmin_weight_export_minutes": _int(
            src.get("garmin_weight_export_minutes"),
            DEFAULTS["garmin_weight_export_minutes"],
            *WEIGHT_EXPORT_MINUTES_RANGE,
        ),
        "garmin_weight_max_age_days": _int(
            src.get("garmin_weight_max_age_days"),
            DEFAULTS["garmin_weight_max_age_days"],
            *WEIGHT_MAX_AGE_DAYS_RANGE,
        ),
        "pulse_seconds": pulse,
        "pulse_start_hour": start_hour,
        "pulse_end_hour": end_hour,
        "nudges": {
            category: bool(stored_nudges.get(category, True))
            for category in NUDGE_CATEGORIES
        },
    }


def hhmm(value: str) -> tuple[int, int]:
    parsed = time_type.fromisoformat(value)
    return parsed.hour, parsed.minute


def as_time(value: str) -> time_type:
    return time_type.fromisoformat(value)


def _subject_value(clean: dict[str, Any]) -> dict[str, Any]:
    return {key: clean[key] for key in _SUBJECT_FIELDS}


def _delivery_value(clean: dict[str, Any]) -> dict[str, Any]:
    return {key: clean[key] for key in _DELIVERY_FIELDS}


def _garmin_value(clean: dict[str, Any]) -> dict[str, Any]:
    return {key: clean[key] for key in _GARMIN_FIELDS}


def _strict_object(
    raw: Any, fields: frozenset[str], *, label: str
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ProactivePreferencesUnavailableError(
            f"{label} preference row has an invalid field set"
        )
    return raw


def _strict_time(raw: Any, *, field: str) -> time_type:
    if not isinstance(raw, str):
        raise ProactivePreferencesUnavailableError(
            f"{field} must be a canonical HH:MM string"
        )
    try:
        parsed = time_type.fromisoformat(raw)
    except ValueError as exc:
        raise ProactivePreferencesUnavailableError(
            f"{field} must be a canonical HH:MM string"
        ) from exc
    if parsed.second or parsed.microsecond or parsed.strftime("%H:%M") != raw:
        raise ProactivePreferencesUnavailableError(
            f"{field} must be a canonical HH:MM string"
        )
    return parsed


def _strict_int(raw: Any, *, field: str, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise ProactivePreferencesUnavailableError(
            f"{field} is outside its supported integer range"
        )
    return raw


def _decode_subject(raw: Any) -> SubjectProactivePolicy:
    value = _strict_object(raw, _SUBJECT_FIELDS, label="subject proactive")
    nudges = value["nudges"]
    if (
        not isinstance(nudges, dict)
        or set(nudges) != set(NUDGE_CATEGORIES)
        or any(not isinstance(nudges[key], bool) for key in NUDGE_CATEGORIES)
    ):
        raise ProactivePreferencesUnavailableError(
            "subject proactive nudge policy is malformed"
        )
    return SubjectProactivePolicy(
        brief_time=_strict_time(value["brief_time"], field="brief_time"),
        evening_time=_strict_time(
            value["evening_time"], field="evening_time"
        ),
        enabled_nudge_categories=frozenset(
            key for key in NUDGE_CATEGORIES if nudges[key]
        ),
    )


def _decode_delivery(raw: Any) -> DeliveryPolicy:
    value = _strict_object(raw, _DELIVERY_FIELDS, label="Telegram delivery")
    return DeliveryPolicy(
        quiet_start=_strict_time(value["quiet_start"], field="quiet_start"),
        quiet_end=_strict_time(value["quiet_end"], field="quiet_end"),
        daily_budget=_strict_int(
            value["daily_budget"],
            field="daily_budget",
            low=BUDGET_RANGE[0],
            high=BUDGET_RANGE[1],
        ),
    )


def _decode_garmin(raw: Any) -> GarminProactivePolicy:
    value = _strict_object(raw, _GARMIN_FIELDS, label="Garmin proactive")
    pulse_seconds = value["pulse_seconds"]
    if pulse_seconds != 0:
        pulse_seconds = _strict_int(
            pulse_seconds,
            field="pulse_seconds",
            low=PULSE_SECONDS_RANGE[0],
            high=PULSE_SECONDS_RANGE[1],
        )
    elif isinstance(pulse_seconds, bool) or not isinstance(pulse_seconds, int):
        raise ProactivePreferencesUnavailableError(
            "pulse_seconds is outside its supported integer range"
        )
    start_hour = _strict_int(
        value["pulse_start_hour"], field="pulse_start_hour", low=0, high=23
    )
    end_hour = _strict_int(
        value["pulse_end_hour"], field="pulse_end_hour", low=1, high=24
    )
    if end_hour <= start_hour:
        raise ProactivePreferencesUnavailableError(
            "Garmin pulse window must end after it starts"
        )
    return GarminProactivePolicy(
        sync_hours=_strict_int(
            value["garmin_sync_hours"],
            field="garmin_sync_hours",
            low=SYNC_HOURS_RANGE[0],
            high=SYNC_HOURS_RANGE[1],
        ),
        weight_export_minutes=_strict_int(
            value["garmin_weight_export_minutes"],
            field="garmin_weight_export_minutes",
            low=WEIGHT_EXPORT_MINUTES_RANGE[0],
            high=WEIGHT_EXPORT_MINUTES_RANGE[1],
        ),
        weight_max_age_days=_strict_int(
            value["garmin_weight_max_age_days"],
            field="garmin_weight_max_age_days",
            low=WEIGHT_MAX_AGE_DAYS_RANGE[0],
            high=WEIGHT_MAX_AGE_DAYS_RANGE[1],
        ),
        pulse_seconds=pulse_seconds,
        pulse_start_hour=start_hour,
        pulse_end_hour=end_hour,
    )


def _decode_bundle(
    subject_value: Any,
    delivery_value: Any,
    garmin_value: Any,
) -> ProactivePreferencesBundle:
    return ProactivePreferencesBundle(
        subject=_decode_subject(subject_value),
        delivery=_decode_delivery(delivery_value),
        garmin=_decode_garmin(garmin_value),
    )


def _bundle_from_clean(clean: dict[str, Any]) -> ProactivePreferencesBundle:
    return _decode_bundle(
        _subject_value(clean),
        _delivery_value(clean),
        _garmin_value(clean),
    )


def _required_actor_lookup_key(actor_username: str) -> str:
    try:
        return normalize_username(actor_username).lookup_key
    except IdentityValidationError as exc:
        raise ProactivePreferencesValidationError(str(exc)) from exc


async def resolve_legacy_preferences_scope(
    session: AsyncSession,
    *,
    actor_username: str | None,
) -> ProactivePreferencesScope:
    """Resolve the exact-one subject and both connection partitions."""

    from vitals.services.legacy_ownership import resolve_legacy_ownership_context

    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
        required_connections=(
            IntegrationProvider.TELEGRAM,
            IntegrationProvider.GARMIN,
        ),
    )
    return ProactivePreferencesScope(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.owner_user_id,
        telegram_connection_id=ownership.connection_id(
            IntegrationProvider.TELEGRAM
        ),
        garmin_connection_id=ownership.connection_id(IntegrationProvider.GARMIN),
        include_legacy=True,
    )


async def _validate_scope_roots(
    session: AsyncSession,
    scope: ProactivePreferencesScope,
    *,
    for_update: bool,
    actor_lookup_key: str | None = None,
    require_live_telegram: bool = False,
) -> None:
    if not isinstance(scope, ProactivePreferencesScope):
        raise ProactivePreferencesValidationError(
            "scope must be a ProactivePreferencesScope"
        )

    subject_query = select(HealthSubject.owner_user_id).where(
        HealthSubject.id == scope.subject_id
    )
    if for_update:
        subject_query = subject_query.with_for_update()
    owner_user_id = await session.scalar(subject_query)
    if owner_user_id != scope.recipient_user_id:
        raise ProactivePreferencesScopeError(
            "proactive preference recipient is not the subject owner"
        )

    owner_query = select(User.status, User.normalized_username).where(
        User.id == scope.recipient_user_id
    )
    if for_update:
        owner_query = owner_query.with_for_update()
    owner_row = (await session.execute(owner_query)).one_or_none()
    if owner_row is None or owner_row.status != UserStatus.ACTIVE.value:
        raise ProactivePreferencesScopeError(
            "proactive preference recipient is not active"
        )
    if (
        actor_lookup_key is not None
        and owner_row.normalized_username != actor_lookup_key
    ):
        raise ProactivePreferencesScopeError(
            "proactive preference actor is not the subject owner"
        )

    connection_query = (
        select(IntegrationConnection)
        .where(
            IntegrationConnection.id.in_(
                (scope.telegram_connection_id, scope.garmin_connection_id)
            )
        )
        .order_by(IntegrationConnection.id)
    )
    if for_update:
        connection_query = connection_query.with_for_update().execution_options(
            populate_existing=True
        )
    connections = {
        row.id: row for row in await session.scalars(connection_query)
    }
    if set(connections) != {
        scope.telegram_connection_id,
        scope.garmin_connection_id,
    }:
        raise ProactivePreferencesScopeError(
            "proactive preference connection roots are missing"
        )

    telegram = connections[scope.telegram_connection_id]
    garmin = connections[scope.garmin_connection_id]
    if (
        telegram.subject_id != scope.subject_id
        or telegram.provider != IntegrationProvider.TELEGRAM.value
        or telegram.connection_type
        != IntegrationConnectionType.RECIPIENT.value
        or telegram.status
        not in (
            _LIVE_CONNECTION_STATUSES
            if require_live_telegram
            else _NON_RETIRED_CONNECTION_STATUSES
        )
    ):
        raise ProactivePreferencesScopeError(
            "Telegram preference connection does not match the subject"
        )
    if (
        garmin.subject_id != scope.subject_id
        or garmin.provider != IntegrationProvider.GARMIN.value
        or garmin.connection_type != IntegrationConnectionType.ACCOUNT.value
        or garmin.status not in _NON_RETIRED_CONNECTION_STATUSES
    ):
        raise ProactivePreferencesScopeError(
            "Garmin preference connection does not match the subject"
        )


async def _setting_rows(
    session: AsyncSession,
    scope: ProactivePreferencesScope,
    *,
    for_update: bool,
) -> tuple[
    SubjectSetting | None,
    IntegrationConnectionSetting | None,
    IntegrationConnectionSetting | None,
]:
    subject_query = select(SubjectSetting).where(
        SubjectSetting.subject_id == scope.subject_id,
        SubjectSetting.key == SUBJECT_POLICY_KEY,
    )
    delivery_query = select(IntegrationConnectionSetting).where(
        IntegrationConnectionSetting.integration_connection_id
        == scope.telegram_connection_id,
        IntegrationConnectionSetting.key == TELEGRAM_DELIVERY_POLICY_KEY,
    )
    garmin_query = select(IntegrationConnectionSetting).where(
        IntegrationConnectionSetting.integration_connection_id
        == scope.garmin_connection_id,
        IntegrationConnectionSetting.key == GARMIN_POLICY_KEY,
    )
    if for_update:
        subject_query = subject_query.with_for_update().execution_options(
            populate_existing=True
        )
        delivery_query = delivery_query.with_for_update().execution_options(
            populate_existing=True
        )
        garmin_query = garmin_query.with_for_update().execution_options(
            populate_existing=True
        )
    return (
        await session.scalar(subject_query),
        await session.scalar(delivery_query),
        await session.scalar(garmin_query),
    )


def _require_complete_rows(
    rows: tuple[
        SubjectSetting | None,
        IntegrationConnectionSetting | None,
        IntegrationConnectionSetting | None,
    ],
) -> tuple[
    SubjectSetting,
    IntegrationConnectionSetting,
    IntegrationConnectionSetting,
]:
    if any(row is None for row in rows):
        raise ProactivePreferencesUnavailableError(
            "scoped proactive preferences are missing or partial"
        )
    subject, delivery, garmin = rows
    assert subject is not None and delivery is not None and garmin is not None
    return subject, delivery, garmin


async def get_preferences_bundle(
    session: AsyncSession,
    *,
    scope: ProactivePreferencesScope,
    actor_username: str,
) -> ProactivePreferencesBundle:
    """Load one actor-authorized, statement-consistent scoped snapshot.

    One joined statement is intentional: under PostgreSQL ``READ COMMITTED`` it
    observes the roots and all three policy partitions from one MVCC snapshot.
    Splitting this read into independent selects could combine values from two
    concurrent settings saves. Legacy/default state is never consulted.
    """

    if not isinstance(scope, ProactivePreferencesScope):
        raise ProactivePreferencesValidationError(
            "scope must be a ProactivePreferencesScope"
        )
    actor_lookup_key = _required_actor_lookup_key(actor_username)
    telegram_connection = aliased(IntegrationConnection)
    garmin_connection = aliased(IntegrationConnection)
    delivery_setting = aliased(IntegrationConnectionSetting)
    garmin_setting = aliased(IntegrationConnectionSetting)
    statement = (
        select(
            SubjectSetting.value,
            delivery_setting.value,
            garmin_setting.value,
        )
        .select_from(HealthSubject)
        .join(User, User.id == HealthSubject.owner_user_id)
        .outerjoin(
            SubjectSetting,
            and_(
                SubjectSetting.subject_id == HealthSubject.id,
                SubjectSetting.key == SUBJECT_POLICY_KEY,
            ),
        )
        .join(
            telegram_connection,
            telegram_connection.id == scope.telegram_connection_id,
        )
        .outerjoin(
            delivery_setting,
            and_(
                delivery_setting.integration_connection_id
                == telegram_connection.id,
                delivery_setting.key == TELEGRAM_DELIVERY_POLICY_KEY,
            ),
        )
        .join(
            garmin_connection,
            garmin_connection.id == scope.garmin_connection_id,
        )
        .outerjoin(
            garmin_setting,
            and_(
                garmin_setting.integration_connection_id
                == garmin_connection.id,
                garmin_setting.key == GARMIN_POLICY_KEY,
            ),
        )
        .where(
            HealthSubject.id == scope.subject_id,
            HealthSubject.owner_user_id == scope.recipient_user_id,
            User.id == scope.recipient_user_id,
            User.normalized_username == actor_lookup_key,
            User.status == UserStatus.ACTIVE.value,
            telegram_connection.subject_id == scope.subject_id,
            telegram_connection.provider == IntegrationProvider.TELEGRAM.value,
            telegram_connection.connection_type
            == IntegrationConnectionType.RECIPIENT.value,
            telegram_connection.status.in_(_NON_RETIRED_CONNECTION_STATUSES),
            garmin_connection.subject_id == scope.subject_id,
            garmin_connection.provider == IntegrationProvider.GARMIN.value,
            garmin_connection.connection_type
            == IntegrationConnectionType.ACCOUNT.value,
            garmin_connection.status.in_(_NON_RETIRED_CONNECTION_STATUSES),
        )
    )
    with session.no_autoflush:
        row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise ProactivePreferencesScopeError(
            "proactive preference actor or resource graph is out of scope"
        )
    return _decode_bundle(row[0], row[1], row[2])


async def get_exact_one_preferences_bundle(
    session: AsyncSession,
    *,
    scope: ProactivePreferencesScope,
) -> ProactivePreferencesBundle:
    """Strict actorless startup/job read while the exact-one bridge is open.

    This compatibility API is deliberately separate from the human read API.
    It serializes subject cardinality under identity governance, locks the
    canonical S/Q/C roots, and then locks all three scoped setting rows. It
    fails closed as soon as the database contains another health subject.
    """

    if not isinstance(scope, ProactivePreferencesScope) or not scope.include_legacy:
        raise ProactivePreferencesValidationError(
            "actorless preference reads require an exact-one legacy scope"
        )
    bridge_open = await _lock_write_roots(
        session,
        scope,
        actor_lookup_key=None,
    )
    if not bridge_open:  # pragma: no cover - enforced by _lock_write_roots
        raise LegacyProactivePreferencesBridgeClosedError(
            "legacy proactive preference bridge is closed"
        )
    subject, delivery, garmin = _require_complete_rows(
        await _setting_rows(session, scope, for_update=True)
    )
    return _decode_bundle(subject.value, delivery.value, garmin.value)


async def get_subject_policy(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> SubjectProactivePolicy:
    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise ProactivePreferencesValidationError(
            "subject_id must be a non-zero UUID"
        )
    with session.no_autoflush:
        row = await session.scalar(
            select(SubjectSetting)
            .join(HealthSubject, HealthSubject.id == SubjectSetting.subject_id)
            .where(
                SubjectSetting.subject_id == subject_id,
                SubjectSetting.key == SUBJECT_POLICY_KEY,
            )
        )
    if row is None:
        raise ProactivePreferencesUnavailableError(
            "subject proactive preference row is missing"
        )
    return _decode_subject(row.value)


async def get_garmin_policy(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID,
) -> GarminProactivePolicy:
    for field, value in (
        ("subject_id", subject_id),
        ("integration_connection_id", integration_connection_id),
    ):
        if not isinstance(value, uuid.UUID) or value.int == 0:
            raise ProactivePreferencesValidationError(
                f"{field} must be a non-zero UUID"
            )
    with session.no_autoflush:
        row = await session.scalar(
            select(IntegrationConnectionSetting)
            .join(
                IntegrationConnection,
                IntegrationConnection.id
                == IntegrationConnectionSetting.integration_connection_id,
            )
            .where(
                IntegrationConnectionSetting.integration_connection_id
                == integration_connection_id,
                IntegrationConnectionSetting.key == GARMIN_POLICY_KEY,
                IntegrationConnection.subject_id == subject_id,
                IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
                IntegrationConnection.connection_type
                == IntegrationConnectionType.ACCOUNT.value,
                IntegrationConnection.status.in_(_NON_RETIRED_CONNECTION_STATUSES),
            )
        )
    if row is None:
        raise ProactivePreferencesUnavailableError(
            "Garmin proactive preference row is missing or out of scope"
        )
    return _decode_garmin(row.value)


async def get_locked_delivery_policy(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    recipient_user_id: uuid.UUID,
    integration_connection_id: uuid.UUID,
) -> DeliveryPolicy:
    """Read strict Telegram policy after canonical roots are already locked.

    The caller must hold governance -> S -> Q -> Telegram-C locks. This function
    performs no ``FOR UPDATE``, advisory lock, legacy lookup, or default
    projection. It rechecks the exact graph in the current transaction and reads
    only the connection-scoped policy row.
    """

    for field, value in (
        ("subject_id", subject_id),
        ("recipient_user_id", recipient_user_id),
        ("integration_connection_id", integration_connection_id),
    ):
        if not isinstance(value, uuid.UUID) or value.int == 0:
            raise ProactivePreferencesValidationError(
                f"{field} must be a non-zero UUID"
            )
    with session.no_autoflush:
        raw = await session.scalar(
            select(IntegrationConnectionSetting.value)
            .join(
                IntegrationConnection,
                IntegrationConnection.id
                == IntegrationConnectionSetting.integration_connection_id,
            )
            .join(
                HealthSubject,
                HealthSubject.id == IntegrationConnection.subject_id,
            )
            .join(User, User.id == HealthSubject.owner_user_id)
            .where(
                IntegrationConnectionSetting.integration_connection_id
                == integration_connection_id,
                IntegrationConnectionSetting.key == TELEGRAM_DELIVERY_POLICY_KEY,
                IntegrationConnection.subject_id == subject_id,
                IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
                IntegrationConnection.connection_type
                == IntegrationConnectionType.RECIPIENT.value,
                IntegrationConnection.status.in_(_LIVE_CONNECTION_STATUSES),
                HealthSubject.owner_user_id == recipient_user_id,
                User.status == UserStatus.ACTIVE.value,
            )
        )
    if raw is None:
        raise ProactivePreferencesUnavailableError(
            "Telegram delivery policy is missing or out of scope"
        )
    return _decode_delivery(raw)


async def _lock_write_roots(
    session: AsyncSession,
    scope: ProactivePreferencesScope,
    *,
    actor_lookup_key: str | None,
) -> bool:
    """Lock canonical roots and return whether exact-one mirroring is allowed."""

    await acquire_identity_governance_lock(session)
    await _validate_scope_roots(
        session,
        scope,
        for_update=True,
        actor_lookup_key=actor_lookup_key,
    )
    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    exact_one = subject_ids == [scope.subject_id]
    if scope.include_legacy and not exact_one:
        raise LegacyProactivePreferencesBridgeClosedError(
            "legacy proactive preference mirroring requires exactly one subject"
        )
    return exact_one and scope.include_legacy


def _add_scoped_rows(
    session: AsyncSession,
    scope: ProactivePreferencesScope,
    clean: dict[str, Any],
) -> None:
    session.add_all(
        [
            SubjectSetting(
                subject_id=scope.subject_id,
                key=SUBJECT_POLICY_KEY,
                value=_subject_value(clean),
            ),
            IntegrationConnectionSetting(
                integration_connection_id=scope.telegram_connection_id,
                key=TELEGRAM_DELIVERY_POLICY_KEY,
                value=_delivery_value(clean),
            ),
            IntegrationConnectionSetting(
                integration_connection_id=scope.garmin_connection_id,
                key=GARMIN_POLICY_KEY,
                value=_garmin_value(clean),
            ),
        ]
    )


def _replace_scoped_rows(
    rows: tuple[
        SubjectSetting,
        IntegrationConnectionSetting,
        IntegrationConnectionSetting,
    ],
    clean: dict[str, Any],
) -> None:
    subject, delivery, garmin = rows
    subject.value = _subject_value(clean)
    delivery.value = _delivery_value(clean)
    garmin.value = _garmin_value(clean)


async def initialize_legacy_preferences(
    session: AsyncSession,
    *,
    scope: ProactivePreferencesScope,
) -> ProactivePreferencesBundle:
    """Idempotently split legacy/default values before jobs or sends can run."""

    if not isinstance(scope, ProactivePreferencesScope) or not scope.include_legacy:
        raise ProactivePreferencesValidationError(
            "legacy preference initialization requires an exact-one scope"
        )
    bridge_open = await _lock_write_roots(
        session,
        scope,
        actor_lookup_key=None,
    )
    if not bridge_open:  # pragma: no cover - guarded by _lock_write_roots
        raise LegacyProactivePreferencesBridgeClosedError(
            "legacy proactive preference bridge is closed"
        )
    rows = await _setting_rows(session, scope, for_update=True)
    existing_count = sum(row is not None for row in rows)
    legacy = await session.scalar(
        select(AppSetting)
        .where(AppSetting.key == LEGACY_SETTINGS_KEY)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if existing_count == 0:
        clean = sanitize(legacy.value if legacy is not None else None)
        _add_scoped_rows(session, scope, clean)
        if legacy is None:
            session.add(AppSetting(key=LEGACY_SETTINGS_KEY, value=clean))
        else:
            legacy.value = clean
        bundle = _bundle_from_clean(clean)
    elif existing_count != 3:
        raise ProactivePreferencesUnavailableError(
            "legacy proactive preference split is partial"
        )
    else:
        complete = _require_complete_rows(rows)
        bundle = _decode_bundle(*(row.value for row in complete))
        clean = bundle.as_flat_dict()
        if legacy is None:
            session.add(AppSetting(key=LEGACY_SETTINGS_KEY, value=clean))
        elif sanitize(legacy.value) != clean:
            raise ProactivePreferencesDriftError(
                "legacy and scoped proactive preferences disagree"
            )
        elif legacy.value != clean:
            legacy.value = clean

    await session.flush()
    return bundle


async def set_preferences_bundle(
    session: AsyncSession,
    raw: Any,
    *,
    scope: ProactivePreferencesScope,
    actor_username: str,
) -> ProactivePreferencesBundle:
    """Replace an active owner's policy partitions atomically; never commit."""

    clean = sanitize(raw)
    bridge_open = await _lock_write_roots(
        session,
        scope,
        actor_lookup_key=_required_actor_lookup_key(actor_username),
    )
    rows = await _setting_rows(session, scope, for_update=True)
    existing_count = sum(row is not None for row in rows)
    if existing_count == 0:
        _add_scoped_rows(session, scope, clean)
    elif existing_count != 3:
        raise ProactivePreferencesUnavailableError(
            "scoped proactive preference split is partial"
        )
    else:
        complete = _require_complete_rows(rows)
        _decode_bundle(*(row.value for row in complete))
        _replace_scoped_rows(complete, clean)

    if bridge_open:
        legacy = await session.scalar(
            select(AppSetting)
            .where(AppSetting.key == LEGACY_SETTINGS_KEY)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if legacy is None:
            session.add(AppSetting(key=LEGACY_SETTINGS_KEY, value=clean))
        else:
            legacy.value = clean
    await session.flush()
    return _bundle_from_clean(clean)


async def _read_legacy_setting_value(session: AsyncSession) -> Any:
    return await session.scalar(
        select(AppSetting.value).where(AppSetting.key == LEGACY_SETTINGS_KEY)
    )


async def get_pre_identity_legacy_prefs(
    session: AsyncSession,
) -> dict[str, Any]:
    """Explicit compatibility read for a database with zero health subjects.

    This is the transaction-boundary form: it insists on a fresh guarded root.
    Use :func:`get_pre_identity_legacy_prefs_in_transaction` from a service hook
    that is already inside a caller-owned transaction.
    """

    with session.no_autoflush:
        try:
            await authorize_pre_identity_compatibility_transaction(session)
        except PreIdentityCompatibilityError as exc:
            raise ProactivePreferencesScopeError(str(exc)) from exc
        value = await _read_legacy_setting_value(session)
    return sanitize(value)


async def get_pre_identity_legacy_prefs_in_transaction(
    session: AsyncSession,
) -> dict[str, Any]:
    """Compatibility read for a hook running inside a caller's transaction.

    Same governance lock and same zero-subject probe as
    :func:`get_pre_identity_legacy_prefs`; it adopts the open transaction rather
    than demanding a fresh root, because a hook cannot own the boundary. The
    caller owes the lock-order contract described on
    :func:`vitals.services.identity_service.require_pre_identity_compatibility`.
    """

    with session.no_autoflush:
        try:
            await require_pre_identity_compatibility(session)
        except PreIdentityCompatibilityError as exc:
            raise ProactivePreferencesScopeError(str(exc)) from exc
        value = await _read_legacy_setting_value(session)
    return sanitize(value)


async def set_pre_identity_legacy_prefs(
    session: AsyncSession,
    raw: Any,
) -> dict[str, Any]:
    """Explicit zero-subject compatibility write; flush, never commit."""

    clean = sanitize(raw)
    with session.no_autoflush:
        try:
            await authorize_pre_identity_compatibility_transaction(session)
        except PreIdentityCompatibilityError as exc:
            raise ProactivePreferencesScopeError(str(exc)) from exc
        row = next(
            (
                pending
                for pending in session.new
                if isinstance(pending, AppSetting)
                and pending.key == LEGACY_SETTINGS_KEY
            ),
            None,
        )
        if row is None:
            row = await session.scalar(
                select(AppSetting)
                .where(AppSetting.key == LEGACY_SETTINGS_KEY)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if row is None:
            row = AppSetting(key=LEGACY_SETTINGS_KEY, value=clean)
            session.add(row)
        else:
            row.value = clean
    # A targeted flush is part of this compatibility contract: unrelated
    # pending identity objects must not become durable merely because a legacy
    # preference was saved.
    await session.flush([row])
    return clean


# Transitional aliases retained only for tests and pre-bootstrap databases.
async def get_prefs(session: AsyncSession) -> dict[str, Any]:
    return await get_pre_identity_legacy_prefs(session)


async def set_prefs(session: AsyncSession, raw: Any) -> dict[str, Any]:
    return await set_pre_identity_legacy_prefs(session, raw)


async def bot_enabled(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    strict: bool = False,
) -> bool:
    """Return the subject module gate; strict mode rejects an absent subject."""

    from vitals.services import modules_service

    if strict:
        if subject_id is None:
            raise ValueError("strict module gating requires a subject_id")
        from vitals.services.scoped_settings_service import (
            ScopedSettingKey,
            SettingScope,
            get_scoped_setting,
        )

        raw = await get_scoped_setting(
            session,
            scope=SettingScope.SUBJECT,
            key=ScopedSettingKey.ENABLED_MODULES,
            subject_id=subject_id,
            default=dict(modules_service.DEFAULT_STATE),
        )
        return bool(raw.get(MODULE_KEY)) if isinstance(raw, dict) else False

    if subject_id is None:
        # A database with no subjects has no per-person module state to read,
        # only the installation-wide row the bootstrap will later split. This
        # arm is the zero-subject compatibility gate and goes with it.
        from sqlalchemy import select

        from vitals.models.app_settings import AppSetting

        raw = await session.scalar(
            select(AppSetting.value).where(
                AppSetting.key == modules_service.SETTINGS_KEY
            )
        )
        state = raw if isinstance(raw, dict) else dict(modules_service.DEFAULT_STATE)
        return bool(state.get(MODULE_KEY))
    state = await modules_service.get_enabled_modules(
        session,
        subject_id=subject_id,
    )
    return bool(state.get(MODULE_KEY))


__all__ = [
    "BUDGET_RANGE",
    "CATEGORY_ACTIVITY",
    "CATEGORY_DATA",
    "CATEGORY_NUTRITION",
    "DEFAULTS",
    "DeliveryPolicy",
    "GARMIN_POLICY_KEY",
    "GarminProactivePolicy",
    "LEGACY_SETTINGS_KEY",
    "LegacyProactivePreferencesBridgeClosedError",
    "MODULE_KEY",
    "NUDGE_CATEGORIES",
    "PULSE_SECONDS_RANGE",
    "ProactivePreferencesBundle",
    "ProactivePreferencesDriftError",
    "ProactivePreferencesError",
    "ProactivePreferencesScope",
    "ProactivePreferencesScopeError",
    "ProactivePreferencesUnavailableError",
    "ProactivePreferencesValidationError",
    "SETTINGS_KEY",
    "SUBJECT_POLICY_KEY",
    "SYNC_HOURS_RANGE",
    "SubjectProactivePolicy",
    "TELEGRAM_DELIVERY_POLICY_KEY",
    "WEIGHT_EXPORT_MINUTES_RANGE",
    "WEIGHT_MAX_AGE_DAYS_RANGE",
    "as_time",
    "bot_enabled",
    "get_exact_one_preferences_bundle",
    "get_garmin_policy",
    "get_locked_delivery_policy",
    "get_preferences_bundle",
    "get_pre_identity_legacy_prefs",
    "get_pre_identity_legacy_prefs_in_transaction",
    "get_prefs",
    "get_subject_policy",
    "hhmm",
    "initialize_legacy_preferences",
    "resolve_legacy_preferences_scope",
    "sanitize",
    "set_preferences_bundle",
    "set_pre_identity_legacy_prefs",
    "set_prefs",
]
