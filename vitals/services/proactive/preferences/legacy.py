"""Zero-subject compatibility bridge for pre-identity preferences."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.app_settings import AppSetting
from vitals.services.identity_service import (
    PreIdentityCompatibilityError,
    authorize_pre_identity_compatibility_transaction,
    require_pre_identity_compatibility,
)
from vitals.services.proactive.preferences.codec import sanitize
from vitals.services.proactive.preferences.contracts import (
    LEGACY_SETTINGS_KEY,
    ProactivePreferencesScopeError,
)

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
