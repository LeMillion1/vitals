"""Transactional scoped-setting store with a bounded legacy bridge.

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
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.app_settings import AppSetting
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import (
    IntegrationConnectionSetting,
    SubjectSetting,
    UserSetting,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.settings.contracts import (
    ScopedSettingDriftError,
    ScopedSettingKey,
    ScopedSettingOwnershipError,
    ScopedSettingTargetNotFoundError,
    ScopedSettingValidationError,
    SettingScope,
    _ValidatedRequest,
    _validate_request,
)
from vitals.services.settings.legacy import (
    _installation_is_still_one_person,
    _require_legacy_bridge_open,
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
    mirror: bool = True,
) -> None:
    """Replace the scoped representation, and the legacy one while it exists.

    ``mirror`` is false once the installation holds more than one subject: the
    global key stops being anybody's, and writing it would hand one person's
    value to everyone still reading the fallback.
    """

    if scoped is None:
        session.add(_new_scoped_row(request, value=value))
    else:
        scoped.value = deepcopy(value)

    if not mirror:
        return

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
    scope_id: uuid.UUID,
    expected_subject_id: uuid.UUID | None = None,
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
        scope_id=scope_id,
        expected_subject_id=expected_subject_id,
    )
    await _require_scope_target(session, request, for_update=False)
    scoped = await _scoped_row(session, request, for_update=False)
    if scoped is not None:
        return deepcopy(scoped.value)
    if not await _installation_is_still_one_person(session):
        # No scoped row, and no installation-wide value that could be this
        # subject's. That is not a refusal — it is the honest answer that this
        # scope has no setting, and the caller's default is what it means.
        return deepcopy(default)
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
    scope_id: uuid.UUID,
    expected_subject_id: uuid.UUID | None = None,
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
        scope_id=scope_id,
        expected_subject_id=expected_subject_id,
    )
    await acquire_identity_governance_lock(session)
    connection = await _require_scope_target(session, request, for_update=True)
    # Mirroring into the shared ``app_settings`` key is the half that needs a
    # sole subject, and needs it for a reason worth keeping: with two people in
    # the installation, writing one person's value into the global row would
    # overwrite everybody's fallback with one of them. So the mirror stops
    # rather than the write — the scoped row is whose the value actually is.
    mirror = await _installation_is_still_one_person(session)
    if mirror:
        await _require_legacy_bridge_open(
            session,
            request,
            connection=connection,
        )
    scoped = await _scoped_row(session, request, for_update=True)
    legacy = await _legacy_row(session, request, for_update=True) if mirror else None

    _replace_rows(
        session,
        request,
        scoped=scoped,
        legacy=legacy,
        value=value,
        mirror=mirror,
    )

    await session.flush()
    return deepcopy(value)


async def update_scoped_setting(
    session: AsyncSession,
    *,
    scope: SettingScope | str,
    key: ScopedSettingKey | str,
    update: Callable[[Any], Any],
    scope_id: uuid.UUID,
    expected_subject_id: uuid.UUID | None = None,
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
        scope_id=scope_id,
        expected_subject_id=expected_subject_id,
    )
    await acquire_identity_governance_lock(session)
    connection = await _require_scope_target(session, request, for_update=True)
    # Same rule as ``set_scoped_setting``: the shared key is what needs a sole
    # subject, so the mirror stops rather than the update.
    mirror = await _installation_is_still_one_person(session)
    if mirror:
        await _require_legacy_bridge_open(
            session,
            request,
            connection=connection,
        )
    scoped = await _scoped_row(session, request, for_update=True)
    legacy = await _legacy_row(session, request, for_update=True) if mirror else None
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
        mirror=mirror,
    )
    await session.flush()
    return deepcopy(value)


async def mirror_legacy_setting(
    session: AsyncSession,
    *,
    scope: SettingScope | str,
    key: ScopedSettingKey | str,
    scope_id: uuid.UUID,
    expected_subject_id: uuid.UUID | None = None,
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
        scope_id=scope_id,
        expected_subject_id=expected_subject_id,
    )
    await acquire_identity_governance_lock(session)
    connection = await _require_scope_target(session, request, for_update=True)
    if not await _installation_is_still_one_person(session):
        # This copies the installation-wide value *into* one scope. Once the
        # installation is more than one person the shared value is nobody's, so
        # there is nothing to copy and adopting it for whoever asked first would
        # be inventing the answer.
        return False
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
    "get_scoped_setting",
    "mirror_legacy_setting",
    "set_scoped_setting",
    "update_scoped_setting",
]
