"""Allowlisted bridge between legacy and explicitly scoped settings.

This module is the low-level Stage-2 compatibility boundary.  It deliberately
has no cache integration: product services own UUID-namespaced caching and move
onto this bridge one reviewed call site at a time.

Only the reviewed non-secret mappings below are accepted.  Reads prefer the
scoped row and fall back to ``app_settings``.  Writes replace the JSON value in
both stores in one caller-owned transaction and flush without committing.
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import IntegrationConnectionStatus, IntegrationProvider, UserStatus
from vitals.models.app_settings import AppSetting
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import (
    IntegrationConnectionSetting,
    SubjectSetting,
    UserSetting,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity_service import acquire_identity_governance_lock


class SettingScope(StrEnum):
    """Supported ownership boundaries for reviewed legacy settings."""

    USER = "user"
    SUBJECT = "subject"
    INTEGRATION_CONNECTION = "integration_connection"


class ScopedSettingKey(StrEnum):
    """Legacy keys whose destination scope has been explicitly reviewed."""

    UI_LANGUAGE = "ui_language"
    ENABLED_MODULES = "enabled_modules"
    CUSTOM_CHARTS = "custom_charts"
    WEEK_TEMPLATE = "week_template"
    GARMIN_WEIGHT_EXPORT_ENABLED = "garmin_weight_export_enabled"


@dataclass(frozen=True, slots=True)
class ScopedSettingRoute:
    """One allowlisted legacy-to-scoped persistence mapping."""

    scope: SettingScope
    legacy_key: str
    model: type[UserSetting | SubjectSetting | IntegrationConnectionSetting]
    scope_id_field: str
    required_provider: IntegrationProvider | None = None


SCOPED_SETTING_REGISTRY: Mapping[ScopedSettingKey, ScopedSettingRoute] = (
    MappingProxyType(
        {
            ScopedSettingKey.UI_LANGUAGE: ScopedSettingRoute(
                scope=SettingScope.USER,
                legacy_key="ui_language",
                model=UserSetting,
                scope_id_field="user_id",
            ),
            ScopedSettingKey.ENABLED_MODULES: ScopedSettingRoute(
                scope=SettingScope.SUBJECT,
                legacy_key="enabled_modules",
                model=SubjectSetting,
                scope_id_field="subject_id",
            ),
            ScopedSettingKey.CUSTOM_CHARTS: ScopedSettingRoute(
                scope=SettingScope.SUBJECT,
                legacy_key="custom_charts",
                model=SubjectSetting,
                scope_id_field="subject_id",
            ),
            ScopedSettingKey.WEEK_TEMPLATE: ScopedSettingRoute(
                scope=SettingScope.SUBJECT,
                legacy_key="week_template",
                model=SubjectSetting,
                scope_id_field="subject_id",
            ),
            ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED: ScopedSettingRoute(
                scope=SettingScope.INTEGRATION_CONNECTION,
                legacy_key="garmin_weight_export_enabled",
                model=IntegrationConnectionSetting,
                scope_id_field="integration_connection_id",
                required_provider=IntegrationProvider.GARMIN,
            ),
        }
    )
)

_SECRET_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "credential",
)


class ScopedSettingError(RuntimeError):
    """Base class for persisted scoped-setting state failures."""


class ScopedSettingValidationError(ValueError):
    """The requested key, scope, or identifier is not safe to use."""


class UnknownScopedSettingKeyError(ScopedSettingValidationError):
    """A key is not present in the reviewed migration registry."""


class ForbiddenScopedSettingKeyError(ScopedSettingValidationError):
    """A secret-like key was offered to a generic setting store."""


class ScopedSettingScopeMismatchError(ScopedSettingValidationError):
    """A known key was requested through the wrong ownership boundary."""


class ScopedSettingTargetNotFoundError(ScopedSettingError):
    """The requested user, subject, or connection does not exist."""


class ScopedSettingOwnershipError(ScopedSettingError):
    """A connection is not an allowed resource of the requested subject."""


class ScopedSettingDriftError(ScopedSettingError):
    """Legacy and already-created scoped values disagree during mirroring."""


class LegacyScopedSettingBridgeClosedError(ScopedSettingError):
    """Singleton legacy compatibility is unsafe for the persisted identity graph."""


@dataclass(frozen=True, slots=True)
class _ValidatedRequest:
    key: ScopedSettingKey
    route: ScopedSettingRoute
    scope_id: uuid.UUID
    expected_subject_id: uuid.UUID | None


def _as_scope(scope: SettingScope | str) -> SettingScope:
    try:
        return SettingScope(scope)
    except (TypeError, ValueError) as exc:
        raise ScopedSettingValidationError(
            f"unknown scoped-setting scope: {scope!r}"
        ) from exc


def _as_key(key: ScopedSettingKey | str) -> ScopedSettingKey:
    if not isinstance(key, str):
        raise UnknownScopedSettingKeyError("scoped-setting key must be a string")
    normalized = key.casefold()
    if any(marker in normalized for marker in _SECRET_KEY_MARKERS):
        raise ForbiddenScopedSettingKeyError(
            "secret-like keys are forbidden in generic scoped settings"
        )
    try:
        return ScopedSettingKey(key)
    except ValueError as exc:
        raise UnknownScopedSettingKeyError(
            f"unknown scoped-setting key: {key!r}"
        ) from exc


def _required_uuid(value: object, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise ScopedSettingValidationError(f"{field} must be a non-zero UUID")
    return value


def _optional_uuid(value: object, *, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    return _required_uuid(value, field=field)


def _validate_request(
    *,
    scope: SettingScope | str,
    key: ScopedSettingKey | str,
    user_id: uuid.UUID | None,
    subject_id: uuid.UUID | None,
    integration_connection_id: uuid.UUID | None,
) -> _ValidatedRequest:
    parsed_scope = _as_scope(scope)
    parsed_key = _as_key(key)
    route = SCOPED_SETTING_REGISTRY[parsed_key]
    if route.scope is not parsed_scope:
        raise ScopedSettingScopeMismatchError(
            f"{parsed_key.value!r} belongs to {route.scope.value!r}, "
            f"not {parsed_scope.value!r}"
        )

    if parsed_scope is SettingScope.USER:
        if subject_id is not None or integration_connection_id is not None:
            raise ScopedSettingScopeMismatchError(
                "user settings accept only user_id"
            )
        scope_id = _required_uuid(user_id, field="user_id")
        expected_subject_id = None
    elif parsed_scope is SettingScope.SUBJECT:
        if user_id is not None or integration_connection_id is not None:
            raise ScopedSettingScopeMismatchError(
                "subject settings accept only subject_id"
            )
        scope_id = _required_uuid(subject_id, field="subject_id")
        expected_subject_id = None
    else:
        if user_id is not None:
            raise ScopedSettingScopeMismatchError(
                "integration-connection settings do not accept user_id"
            )
        scope_id = _required_uuid(
            integration_connection_id,
            field="integration_connection_id",
        )
        expected_subject_id = _optional_uuid(subject_id, field="subject_id")

    return _ValidatedRequest(
        key=parsed_key,
        route=route,
        scope_id=scope_id,
        expected_subject_id=expected_subject_id,
    )


async def _require_scope_target(
    session: AsyncSession,
    request: _ValidatedRequest,
    *,
    for_update: bool,
) -> IntegrationConnection | None:
    """Validate and optionally lock the ownership root for one request."""

    if request.route.scope is SettingScope.USER:
        query = select(User.id).where(User.id == request.scope_id)
        if for_update:
            query = query.with_for_update()
        if await session.scalar(query) is None:
            raise ScopedSettingTargetNotFoundError(
                f"user {request.scope_id} does not exist"
            )
        return None

    if request.route.scope is SettingScope.SUBJECT:
        query = select(HealthSubject.id).where(HealthSubject.id == request.scope_id)
        if for_update:
            query = query.with_for_update()
        if await session.scalar(query) is None:
            raise ScopedSettingTargetNotFoundError(
                f"health subject {request.scope_id} does not exist"
            )
        return None

    query = select(IntegrationConnection).where(
        IntegrationConnection.id == request.scope_id
    )
    if for_update:
        query = query.with_for_update()
    connection = await session.scalar(query)
    if connection is None:
        raise ScopedSettingTargetNotFoundError(
            f"integration connection {request.scope_id} does not exist"
        )
    if (
        request.route.required_provider is not None
        and connection.provider != request.route.required_provider.value
    ):
        raise ScopedSettingOwnershipError(
            f"{request.key.value!r} requires a "
            f"{request.route.required_provider.value} connection"
        )
    if (
        request.expected_subject_id is not None
        and connection.subject_id != request.expected_subject_id
    ):
        raise ScopedSettingOwnershipError(
            f"integration connection {request.scope_id} does not belong to "
            f"health subject {request.expected_subject_id}"
        )
    return connection


async def _require_legacy_bridge_open(
    session: AsyncSession,
    request: _ValidatedRequest,
    *,
    connection: IntegrationConnection | None,
) -> None:
    """Fail closed unless the legacy singleton has one unambiguous active owner.

    The caller acquires the identity-governance advisory lock before entering
    this function.  Locking the subject/owner rows then keeps the checked graph
    stable until its transaction ends.  Future registration and identity writes
    use the same governance lock.
    """

    rows = list(
        await session.execute(
            select(
                HealthSubject.id,
                HealthSubject.owner_user_id,
                User.status,
            )
            .join(User, User.id == HealthSubject.owner_user_id)
            .order_by(HealthSubject.id)
            .limit(2)
            .with_for_update()
        )
    )
    if len(rows) != 1:
        raise LegacyScopedSettingBridgeClosedError(
            "legacy scoped-setting compatibility requires exactly one health subject"
        )

    legacy_subject_id, legacy_owner_user_id, owner_status = rows[0]
    if owner_status != UserStatus.ACTIVE.value:
        raise LegacyScopedSettingBridgeClosedError(
            "legacy scoped-setting compatibility requires an active subject owner"
        )

    if request.route.scope is SettingScope.USER:
        request_matches = request.scope_id == legacy_owner_user_id
    elif request.route.scope is SettingScope.SUBJECT:
        request_matches = request.scope_id == legacy_subject_id
    else:
        assert connection is not None
        request_matches = connection.subject_id == legacy_subject_id
        if connection.status == IntegrationConnectionStatus.RETIRED.value:
            raise LegacyScopedSettingBridgeClosedError(
                "retired integration connections cannot use legacy compatibility"
            )

    if not request_matches:
        raise LegacyScopedSettingBridgeClosedError(
            "requested setting scope is not owned by the legacy health subject"
        )


async def _scoped_row(
    session: AsyncSession,
    request: _ValidatedRequest,
    *,
    for_update: bool,
) -> UserSetting | SubjectSetting | IntegrationConnectionSetting | None:
    model = request.route.model
    scope_column = getattr(model, request.route.scope_id_field)
    query = select(model).where(
        scope_column == request.scope_id,
        model.key == request.key.value,
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def _legacy_row(
    session: AsyncSession,
    request: _ValidatedRequest,
    *,
    for_update: bool,
) -> AppSetting | None:
    query = select(AppSetting).where(AppSetting.key == request.route.legacy_key)
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


def _new_scoped_row(
    request: _ValidatedRequest,
    *,
    value: Any,
) -> UserSetting | SubjectSetting | IntegrationConnectionSetting:
    return request.route.model(
        **{
            request.route.scope_id_field: request.scope_id,
            "key": request.key.value,
            "value": deepcopy(value),
        }
    )


def _replace_rows(
    session: AsyncSession,
    request: _ValidatedRequest,
    *,
    scoped: UserSetting | SubjectSetting | IntegrationConnectionSetting | None,
    legacy: AppSetting | None,
    value: Any,
) -> None:
    """Replace both compatibility representations without flushing."""

    if scoped is None:
        session.add(_new_scoped_row(request, value=value))
    else:
        scoped.value = deepcopy(value)

    if legacy is None:
        session.add(
            AppSetting(
                key=request.route.legacy_key,
                value=deepcopy(value),
            )
        )
    else:
        legacy.value = deepcopy(value)


async def get_scoped_setting(
    session: AsyncSession,
    *,
    scope: SettingScope | str,
    key: ScopedSettingKey | str,
    user_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    integration_connection_id: uuid.UUID | None = None,
    default: Any = None,
) -> Any:
    """Read scoped state first and fall back to its one allowlisted legacy key.

    The ownership root is checked before fallback.  A random UUID therefore
    cannot be used to read the singleton legacy value through this bridge.
    Returned JSON is detached so a caller cannot mutate ORM state in place.
    """

    request = _validate_request(
        scope=scope,
        key=key,
        user_id=user_id,
        subject_id=subject_id,
        integration_connection_id=integration_connection_id,
    )
    await _require_scope_target(session, request, for_update=False)
    scoped = await _scoped_row(session, request, for_update=False)
    if scoped is not None:
        return deepcopy(scoped.value)
    await acquire_identity_governance_lock(session)
    connection = await _require_scope_target(session, request, for_update=True)
    await _require_legacy_bridge_open(
        session,
        request,
        connection=connection,
    )
    legacy = await _legacy_row(session, request, for_update=False)
    if legacy is not None:
        return deepcopy(legacy.value)
    return deepcopy(default)


async def set_scoped_setting(
    session: AsyncSession,
    *,
    scope: SettingScope | str,
    key: ScopedSettingKey | str,
    value: Any,
    user_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    integration_connection_id: uuid.UUID | None = None,
) -> Any:
    """Atomically replace one scoped value and its legacy compatibility row.

    The shared identity lock, ownership root, existing scoped row, and existing
    legacy row are acquired in that order.  Locking the root also serializes
    concurrent first inserts for the same scope where no setting row exists yet.
    The caller owns commit or rollback; this function only flushes.
    """

    request = _validate_request(
        scope=scope,
        key=key,
        user_id=user_id,
        subject_id=subject_id,
        integration_connection_id=integration_connection_id,
    )
    await acquire_identity_governance_lock(session)
    connection = await _require_scope_target(session, request, for_update=True)
    await _require_legacy_bridge_open(
        session,
        request,
        connection=connection,
    )
    scoped = await _scoped_row(session, request, for_update=True)
    legacy = await _legacy_row(session, request, for_update=True)

    _replace_rows(
        session,
        request,
        scoped=scoped,
        legacy=legacy,
        value=value,
    )

    await session.flush()
    return deepcopy(value)


async def update_scoped_setting(
    session: AsyncSession,
    *,
    scope: SettingScope | str,
    key: ScopedSettingKey | str,
    update: Callable[[Any], Any],
    user_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    integration_connection_id: uuid.UUID | None = None,
    default: Any = None,
) -> Any:
    """Atomically read, transform, and dual-write one scoped setting.

    ``update`` runs synchronously after the scope root and both representations
    are locked.  It receives a detached copy of the new-first value, or of
    ``default`` when neither row exists.  This is the safe boundary for product
    settings stored as one JSON collection: callers cannot lose a concurrent
    toggle, append, or removal by reading before the row lock is acquired.
    """

    if not callable(update):
        raise ScopedSettingValidationError("update must be callable")
    request = _validate_request(
        scope=scope,
        key=key,
        user_id=user_id,
        subject_id=subject_id,
        integration_connection_id=integration_connection_id,
    )
    await acquire_identity_governance_lock(session)
    connection = await _require_scope_target(session, request, for_update=True)
    await _require_legacy_bridge_open(
        session,
        request,
        connection=connection,
    )
    scoped = await _scoped_row(session, request, for_update=True)
    legacy = await _legacy_row(session, request, for_update=True)
    if scoped is not None:
        current = deepcopy(scoped.value)
    elif legacy is not None:
        current = deepcopy(legacy.value)
    else:
        current = deepcopy(default)
    value = update(current)
    _replace_rows(
        session,
        request,
        scoped=scoped,
        legacy=legacy,
        value=value,
    )
    await session.flush()
    return deepcopy(value)


async def mirror_legacy_setting(
    session: AsyncSession,
    *,
    scope: SettingScope | str,
    key: ScopedSettingKey | str,
    user_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    integration_connection_id: uuid.UUID | None = None,
) -> bool:
    """Idempotently copy one allowlisted legacy value into its scoped row.

    ``True`` means a scoped row was created.  Missing legacy state and an exact
    existing mirror both return ``False``.  An existing but different scoped
    value is authoritative for new-first reads, so drift is reported instead of
    silently overwriting either side.
    """

    request = _validate_request(
        scope=scope,
        key=key,
        user_id=user_id,
        subject_id=subject_id,
        integration_connection_id=integration_connection_id,
    )
    await acquire_identity_governance_lock(session)
    connection = await _require_scope_target(session, request, for_update=True)
    await _require_legacy_bridge_open(
        session,
        request,
        connection=connection,
    )
    scoped = await _scoped_row(session, request, for_update=True)
    legacy = await _legacy_row(session, request, for_update=True)
    if legacy is None:
        return False
    if scoped is None:
        session.add(_new_scoped_row(request, value=legacy.value))
        await session.flush()
        return True
    if scoped.value != legacy.value:
        raise ScopedSettingDriftError(
            f"scoped and legacy values disagree for {request.key.value!r}"
        )
    return False


__all__ = [
    "SCOPED_SETTING_REGISTRY",
    "ForbiddenScopedSettingKeyError",
    "LegacyScopedSettingBridgeClosedError",
    "ScopedSettingDriftError",
    "ScopedSettingError",
    "ScopedSettingKey",
    "ScopedSettingOwnershipError",
    "ScopedSettingRoute",
    "ScopedSettingScopeMismatchError",
    "ScopedSettingTargetNotFoundError",
    "ScopedSettingValidationError",
    "SettingScope",
    "UnknownScopedSettingKeyError",
    "get_scoped_setting",
    "mirror_legacy_setting",
    "set_scoped_setting",
    "update_scoped_setting",
]
