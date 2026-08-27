"""Queries boundary for system alerts."""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationProvider,
)
from vitals.models.system_alert import SystemAlert
from vitals.utils.timeutils import now_local

from vitals.services.alerts.contracts import (
    AlertUnsupportedDatabaseError,
    AlertScopeConflictError,
    AlertAmbiguousMatchError,
    AlertLegacyBridgeError,
    LegacyAlertBridge,
    HealthAlertContext,
    ProviderAlertContext,
    PlatformAlertContext,
    AlertContext,
    HEALTH_ALERT_KEYS,
    PROVIDER_ALERT_KEYS,
    PLATFORM_ALERT_KEYS,
    ALERT_KEY_LOCK_NAMESPACE,
)

from vitals.services.alerts.validation import (
    _provider_key_matches,
    _is_health_key,
)

from vitals.services.alerts.context import (
    _allowed_domains_for_key,
)


def _alert_lock_key(alert_key: str) -> int:
    digest = hashlib.sha256(alert_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], byteorder="big", signed=True)


async def _acquire_alert_key_lock(
    session: AsyncSession,
    alert_key: str,
) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return
    if dialect != "postgresql":
        raise AlertUnsupportedDatabaseError(
            f"scoped alerts do not support database dialect {dialect!r}"
        )
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock(CAST(:namespace AS INTEGER), CAST(:lock_key AS INTEGER))"
        ),
        {
            "namespace": ALERT_KEY_LOCK_NAMESPACE,
            "lock_key": _alert_lock_key(alert_key),
        },
    )


def _provider_key_predicate(provider: IntegrationProvider):
    keys = PROVIDER_ALERT_KEYS[provider]
    if not keys:
        return SystemAlert.alert_key.in_(("",))
    return SystemAlert.alert_key.in_(tuple(keys))


def _health_key_predicate():
    # SQL performs a portable coarse filter for the one dynamic family. Python's
    # exact positive-integer validator rejects malformed ``conflict:`` keys.
    return or_(
        SystemAlert.alert_key.in_(tuple(HEALTH_ALERT_KEYS)),
        SystemAlert.alert_key.like("conflict:%"),
    )


def _exact_scope_predicate(context: AlertContext):
    if isinstance(context, PlatformAlertContext):
        return and_(
            SystemAlert.subject_id.is_(None),
            SystemAlert.integration_connection_id.is_(None),
            SystemAlert.alert_key.in_(tuple(PLATFORM_ALERT_KEYS[context.namespace])),
        )
    if isinstance(context, ProviderAlertContext):
        return and_(
            SystemAlert.subject_id == context.identity.subject_id,
            SystemAlert.integration_connection_id == context.integration_connection_id,
        )
    return and_(
        SystemAlert.subject_id == context.identity.subject_id,
        SystemAlert.integration_connection_id.is_(None),
    )


def _legacy_scope_predicate(context: AlertContext):
    unowned = and_(
        SystemAlert.subject_id.is_(None),
        SystemAlert.integration_connection_id.is_(None),
    )
    if isinstance(context, ProviderAlertContext):
        return and_(unowned, _provider_key_predicate(context.provider))
    if isinstance(context, HealthAlertContext):
        return and_(unowned, _health_key_predicate())
    raise AlertLegacyBridgeError("platform contexts have no legacy bridge")


def _candidate_scope_predicate(
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
):
    exact = _exact_scope_predicate(context)
    if legacy_bridge is LegacyAlertBridge.REJECT:
        return exact
    return or_(exact, _legacy_scope_predicate(context))


def _row_is_exact(row: SystemAlert, context: AlertContext) -> bool:
    if isinstance(context, PlatformAlertContext):
        return (
            row.subject_id is None
            and row.integration_connection_id is None
            and row.alert_key in PLATFORM_ALERT_KEYS[context.namespace]
        )
    if isinstance(context, ProviderAlertContext):
        return (
            row.subject_id == context.identity.subject_id
            and row.integration_connection_id == context.integration_connection_id
        )
    return row.subject_id == context.identity.subject_id and row.integration_connection_id is None


def _row_is_eligible_legacy(
    row: SystemAlert,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
) -> bool:
    if legacy_bridge is not LegacyAlertBridge.FULLY_UNOWNED:
        return False
    if isinstance(context, PlatformAlertContext):
        return False
    if row.subject_id is not None or row.integration_connection_id is not None:
        return False
    if isinstance(context, ProviderAlertContext):
        return _provider_key_matches(context.provider, row.alert_key)
    return _is_health_key(row.alert_key)


def _adopt_legacy_row(
    row: SystemAlert,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
) -> bool:
    if _row_is_exact(row, context):
        return False
    if not _row_is_eligible_legacy(row, context, legacy_bridge):
        raise AlertScopeConflictError("alert is not eligible for fully-unowned legacy adoption")
    assert not isinstance(context, PlatformAlertContext)
    row.subject_id = context.identity.subject_id
    if isinstance(context, ProviderAlertContext):
        row.integration_connection_id = context.integration_connection_id
    return True


def _ownership_values(context: AlertContext) -> dict[str, uuid.UUID | None]:
    if isinstance(context, PlatformAlertContext):
        return {"subject_id": None, "integration_connection_id": None}
    return {
        "subject_id": context.identity.subject_id,
        "integration_connection_id": (
            context.integration_connection_id if isinstance(context, ProviderAlertContext) else None
        ),
    }


def _choose_active_row(
    rows: Sequence[SystemAlert],
    *,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
) -> SystemAlert | None:
    exact = [row for row in rows if _row_is_exact(row, context)]
    legacy = [row for row in rows if _row_is_eligible_legacy(row, context, legacy_bridge)]
    selected_ids = {id(row) for row in (*exact, *legacy)}
    foreign = [row for row in rows if id(row) not in selected_ids]

    if len(exact) > 1 or len(legacy) > 1 or (exact and legacy):
        raise AlertAmbiguousMatchError("multiple alerts match the exact or fully-unowned scope")
    if foreign and (exact or legacy):
        raise AlertAmbiguousMatchError("matching and unusable active alerts share one scoped key")
    if exact:
        return exact[0]
    if legacy:
        return legacy[0]
    if foreign:
        # A partially-owned row inside this class: not adoptable and not ours,
        # so it can neither be refreshed nor stepped over.
        raise AlertScopeConflictError(
            "an active alert in this scope has unusable ownership provenance"
        )
    return None


async def _validate_row_semantics(
    session: AsyncSession,
    row: SystemAlert,
    context: AlertContext,
) -> None:
    allowed_domains = await _allowed_domains_for_key(session, context, row.alert_key)
    if row.domain not in allowed_domains:
        raise AlertScopeConflictError("persisted alert domain conflicts with its registered key")


def _stamp_resolution(row: SystemAlert, actor_user_id: uuid.UUID | None) -> bool:
    if row.resolved_at is not None:
        return False
    row.resolved_at = now_local()
    row.resolved_by_user_id = actor_user_id
    return True


def _stamp_override(row: SystemAlert, actor_user_id: uuid.UUID) -> bool:
    if row.override_at is not None:
        return False
    row.override_at = now_local()
    row.overridden_by_user_id = actor_user_id
    return True


def _alert_class_scope(context: AlertContext):
    """Restrict the active-alert lookup to the root the alert belongs to.

    One unresolved alert per key lives inside the connection for a provider
    alert, inside the subject for a health alert, and inside the installation
    for a platform alert.  A row that has not been adopted yet carries neither
    root and is still a candidate for the subject or connection claiming it —
    which is also exactly the platform class's own shape.
    """

    unadopted = and_(
        SystemAlert.subject_id.is_(None),
        SystemAlert.integration_connection_id.is_(None),
    )
    if isinstance(context, PlatformAlertContext):
        return unadopted
    if isinstance(context, ProviderAlertContext):
        return or_(
            SystemAlert.integration_connection_id == context.integration_connection_id,
            unadopted,
        )
    return or_(
        and_(
            SystemAlert.subject_id == context.identity.subject_id,
            SystemAlert.integration_connection_id.is_(None),
        ),
        unadopted,
    )


async def _require_no_broken_class_alert(
    session: AsyncSession,
    *,
    alert_key: str,
    entity_ref: str,
    context: AlertContext,
) -> None:
    """Refuse to write past an active alert whose ownership shape is wrong.

    Each alert class has exactly one legitimate shape: a provider alert names
    both a subject and a connection, a health alert names a subject and no
    connection, and a platform alert names neither.  A row under this key that
    has some other shape belongs to no root the scoped keys recognise, so
    writing beside it would leave the key with two active rows and no way to say
    whose it is.  A row of the *right* shape in another subject or another
    account is not broken and is deliberately left alone.
    """

    unadopted = and_(
        SystemAlert.subject_id.is_(None),
        SystemAlert.integration_connection_id.is_(None),
    )
    if isinstance(context, PlatformAlertContext):
        well_formed = unadopted
    elif isinstance(context, ProviderAlertContext):
        well_formed = or_(
            and_(
                SystemAlert.subject_id.is_not(None),
                SystemAlert.integration_connection_id.is_not(None),
            ),
            unadopted,
        )
    else:
        well_formed = or_(
            and_(
                SystemAlert.subject_id.is_not(None),
                SystemAlert.integration_connection_id.is_(None),
            ),
            unadopted,
        )
    broken = await session.scalar(
        select(SystemAlert.id)
        .where(
            SystemAlert.alert_key == alert_key,
            SystemAlert.entity_ref == entity_ref,
            SystemAlert.resolved_at.is_(None),
            ~well_formed,
        )
        .limit(1)
    )
    if broken is not None:
        raise AlertScopeConflictError(
            "an active alert for this key has unusable ownership provenance"
        )


async def _active_rows_for_key(
    session: AsyncSession,
    *,
    alert_key: str,
    entity_ref: str,
    context: AlertContext,
) -> list[SystemAlert]:
    return list(
        await session.scalars(
            select(SystemAlert)
            .where(
                SystemAlert.alert_key == alert_key,
                SystemAlert.entity_ref == entity_ref,
                SystemAlert.resolved_at.is_(None),
                _alert_class_scope(context),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
