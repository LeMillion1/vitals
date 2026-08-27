"""Atomic scoped preference initialization and replacement writes."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.app_settings import AppSetting
from vitals.models.scoped_settings import IntegrationConnectionSetting, SubjectSetting
from vitals.services.proactive.preferences.codec import (
    _bundle_from_clean,
    _decode_bundle,
    _delivery_value,
    _garmin_value,
    _subject_value,
    sanitize,
)
from vitals.services.proactive.preferences.contracts import (
    GARMIN_POLICY_KEY,
    LEGACY_SETTINGS_KEY,
    SUBJECT_POLICY_KEY,
    TELEGRAM_DELIVERY_POLICY_KEY,
    LegacyProactivePreferencesBridgeClosedError,
    ProactivePreferencesBundle,
    ProactivePreferencesDriftError,
    ProactivePreferencesScope,
    ProactivePreferencesUnavailableError,
    ProactivePreferencesValidationError,
)
from vitals.services.proactive.preferences.queries import (
    _lock_write_roots,
    _required_actor_lookup_key,
    _require_complete_rows,
    _setting_rows,
)

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
    if not bridge_open:
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
