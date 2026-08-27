"""Legacy singleton bridge policy for reviewed scoped settings.

This module may be removed only after every registered setting has stopped
dual-reading and dual-writing the installation-wide ``app_settings`` key and
the historical singleton rows have been retired by an explicit migration.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import IntegrationConnectionStatus, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import IntegrationConnection
from vitals.services.settings.contracts import (
    LegacyScopedSettingBridgeClosedError,
    SettingScope,
    _ValidatedRequest,
)


async def _installation_is_still_one_person(session: AsyncSession) -> bool:
    """Whether the legacy ``app_settings`` singleton still means anything.

    Every route here has two representations: the scoped row, which belongs to
    one user, subject or connection, and one global ``app_settings`` key, which
    belongs to the installation. The second only *means* something while the
    installation is one person — with two subjects, "the module map" is not a
    thing that exists, and the row is nobody's in particular.

    So this is not a permission check. It is the question of whether the
    compatibility half of the bridge still has a subject to be about.
    """

    rows = list(
        await session.execute(select(HealthSubject.id).order_by(HealthSubject.id).limit(2))
    )
    return len(rows) == 1


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


__all__ = []
