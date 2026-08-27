"""Lifecycle boundary for system alerts."""

from __future__ import annotations

from datetime import date as date_type
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    Severity,
)
from vitals.models.system_alert import SystemAlert
from vitals.utils.timeutils import now_local, today_local

from vitals.services.alerts.contracts import (
    AlertValidationError,
    AlertScopeConflictError,
    AlertAmbiguousMatchError,
    AlertActorRequiredError,
    LegacyAlertBridge,
    HealthAlertContext,
    AlertContext,
)

from vitals.services.alerts.validation import (
    _require_context,
    _require_key,
    _require_entity_ref,
    _require_message,
    _require_domain,
    _require_severity,
    _require_alert_id,
    _require_optional_entity,
    _actor_user_id,
)

from vitals.services.alerts.context import (
    _allowed_domains_for_key,
    _validate_platform_domain,
    _prepare_context,
)

from vitals.services.alerts.queries import (
    _acquire_alert_key_lock,
    _candidate_scope_predicate,
    _adopt_legacy_row,
    _ownership_values,
    _choose_active_row,
    _validate_row_semantics,
    _stamp_resolution,
    _stamp_override,
    _require_no_broken_class_alert,
    _active_rows_for_key,
)


async def raise_scoped_alert(
    session: AsyncSession,
    *,
    context: AlertContext,
    domain: Domain,
    severity: Severity,
    message: str,
    alert_key: str,
    entity_ref: str = "",
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
    overridden: bool = False,
) -> SystemAlert:
    """Raise or refresh one alert in an explicit S/C/platform scope.

    Provider refreshes are fresh operational writes and therefore require a
    legacy or active connection. ``overridden`` is a human-only transition: its
    timestamp and actor are stamped together without replacing an earlier
    override attribution. The function flushes but never commits.
    """

    _require_domain(domain)
    _require_severity(severity)
    _require_message(message)
    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    _require_context(context)
    if not isinstance(overridden, bool):
        raise AlertValidationError("overridden must be a boolean")
    actor_user_id = _actor_user_id(context)
    if overridden and actor_user_id is None:
        raise AlertActorRequiredError("override requires an active human actor")
    legacy_bridge = await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=True,
        lock_roots=True,
    )
    allowed_domains = await _allowed_domains_for_key(session, context, alert_key)
    if domain.value not in allowed_domains:
        raise AlertScopeConflictError("alert domain conflicts with its registered key")
    await _acquire_alert_key_lock(session, alert_key)

    await _require_no_broken_class_alert(
        session, alert_key=alert_key, entity_ref=entity_ref, context=context
    )
    rows = await _active_rows_for_key(
        session,
        alert_key=alert_key,
        entity_ref=entity_ref,
        context=context,
    )
    row = _choose_active_row(
        rows,
        context=context,
        legacy_bridge=legacy_bridge,
    )
    if row is not None:
        await _validate_row_semantics(session, row, context)
        if row.domain != domain.value:
            raise AlertScopeConflictError("an active alert cannot change its persisted domain")
        _adopt_legacy_row(row, context, legacy_bridge)
        row.severity = severity.value
        row.message = message
        if overridden:
            assert actor_user_id is not None
            _stamp_override(row, actor_user_id)
        await session.flush()
        return row

    override_at = now_local() if overridden else None
    row = SystemAlert(
        domain=domain.value,
        severity=severity.value,
        message=message,
        alert_key=alert_key,
        entity_ref=entity_ref,
        override_at=override_at,
        overridden_by_user_id=(actor_user_id if overridden else None),
        **_ownership_values(context),
    )
    session.add(row)
    await session.flush()
    return row


async def _scoped_row_by_id(
    session: AsyncSession,
    *,
    alert_id: int,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge,
    for_update: bool,
) -> SystemAlert | None:
    stmt = select(SystemAlert).where(
        SystemAlert.id == alert_id,
        _candidate_scope_predicate(context, legacy_bridge),
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(stmt)


async def resolve_scoped_alert(
    session: AsyncSession,
    alert_id: int,
    *,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> SystemAlert | None:
    """Resolve one visible scoped alert; foreign IDs are non-enumerating misses."""

    _require_alert_id(alert_id)
    legacy_bridge = await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    with session.no_autoflush:
        candidate = await _scoped_row_by_id(
            session,
            alert_id=alert_id,
            context=context,
            legacy_bridge=legacy_bridge,
            for_update=False,
        )
    if candidate is None:
        return None
    await _validate_row_semantics(session, candidate, context)
    await _acquire_alert_key_lock(session, candidate.alert_key)
    row = await _scoped_row_by_id(
        session,
        alert_id=alert_id,
        context=context,
        legacy_bridge=legacy_bridge,
        for_update=True,
    )
    if row is None:
        return None
    changed = _adopt_legacy_row(row, context, legacy_bridge)
    changed = _stamp_resolution(row, _actor_user_id(context)) or changed
    if changed:
        await session.flush()
    return row


async def resolve_scoped_by_key(
    session: AsyncSession,
    *,
    context: AlertContext,
    alert_key: str,
    entity_ref: str = "",
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> SystemAlert | None:
    """Resolve the active alert in one exact scope and natural key."""

    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    legacy_bridge = await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    await _allowed_domains_for_key(session, context, alert_key)
    await _acquire_alert_key_lock(session, alert_key)
    await _require_no_broken_class_alert(
        session, alert_key=alert_key, entity_ref=entity_ref, context=context
    )
    row = _choose_active_row(
        await _active_rows_for_key(
            session,
            alert_key=alert_key,
            entity_ref=entity_ref,
            context=context,
        ),
        context=context,
        legacy_bridge=legacy_bridge,
    )
    if row is None:
        return None
    await _validate_row_semantics(session, row, context)
    _adopt_legacy_row(row, context, legacy_bridge)
    _stamp_resolution(row, _actor_user_id(context))
    await session.flush()
    return row


async def resolve_fully_unowned_by_key_preserving_roots(
    session: AsyncSession,
    *,
    context: HealthAlertContext,
    alert_key: str,
    entity_ref: str = "",
) -> SystemAlert | None:
    """Resolve one legacy health alert without fabricating ownership roots.

    This narrow migration seam is for automated cleanup of a historical row
    whose ``S`` and ``C`` were never recorded.  Exact-one governance proves
    which installation may retire it, but that proof does not reconstruct its
    original subject or provider provenance.  New alerts must use the regular
    scoped APIs.
    """

    if not isinstance(context, HealthAlertContext):
        raise AlertValidationError("legacy root preservation requires health context")
    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    # The effective bridge is deliberately not read here. Every other caller
    # needs it because its query is widened by ``_candidate_scope_predicate``;
    # this one names ``subject_id IS NULL`` itself, so a downgrade would change
    # nothing — with no unowned rows the select simply finds none.
    await _prepare_context(
        session,
        context=context,
        legacy_bridge=LegacyAlertBridge.FULLY_UNOWNED,
        fresh_provider_write=False,
        lock_roots=True,
    )
    await _allowed_domains_for_key(session, context, alert_key)
    await _acquire_alert_key_lock(session, alert_key)
    rows = list(
        await session.scalars(
            select(SystemAlert)
            .where(
                SystemAlert.subject_id.is_(None),
                SystemAlert.integration_connection_id.is_(None),
                SystemAlert.alert_key == alert_key,
                SystemAlert.entity_ref == entity_ref,
                SystemAlert.resolved_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(rows) > 1:
        raise AlertAmbiguousMatchError("multiple fully-unowned alerts share one active natural key")
    if not rows:
        return None
    row = rows[0]
    await _validate_row_semantics(session, row, context)
    _stamp_resolution(row, _actor_user_id(context))
    await session.flush()
    return row


async def override_scoped_alert(
    session: AsyncSession,
    alert_id: int,
    *,
    context: AlertContext,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> SystemAlert | None:
    """Stamp one human override without rewriting an earlier lifecycle actor."""

    _require_alert_id(alert_id)
    _require_context(context)
    actor_user_id = _actor_user_id(context)
    if actor_user_id is None:
        raise AlertActorRequiredError("override requires an active human actor")
    legacy_bridge = await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    with session.no_autoflush:
        candidate = await _scoped_row_by_id(
            session,
            alert_id=alert_id,
            context=context,
            legacy_bridge=legacy_bridge,
            for_update=False,
        )
    if candidate is None:
        return None
    await _validate_row_semantics(session, candidate, context)
    await _acquire_alert_key_lock(session, candidate.alert_key)
    row = await _scoped_row_by_id(
        session,
        alert_id=alert_id,
        context=context,
        legacy_bridge=legacy_bridge,
        for_update=True,
    )
    if row is None:
        return None
    changed = _adopt_legacy_row(row, context, legacy_bridge)
    changed = _stamp_override(row, actor_user_id) or changed
    if changed:
        await session.flush()
    return row


async def resolve_scoped_superseded(
    session: AsyncSession,
    *,
    context: AlertContext,
    alert_key: str,
    keep_entity: str | None,
    marker: str | None = None,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> int:
    """Resolve stale entities for one key without crossing its ownership scope."""

    _require_key(alert_key)
    _require_optional_entity(keep_entity, "keep_entity")
    _require_optional_entity(marker, "marker")
    legacy_bridge = await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    await _allowed_domains_for_key(session, context, alert_key)
    await _acquire_alert_key_lock(session, alert_key)
    rows = list(
        await session.scalars(
            select(SystemAlert)
            .where(
                SystemAlert.alert_key == alert_key,
                SystemAlert.resolved_at.is_(None),
                _candidate_scope_predicate(context, legacy_bridge),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    changed = 0
    actor_user_id = _actor_user_id(context)
    for row in rows:
        await _validate_row_semantics(session, row, context)
        if row.entity_ref == keep_entity:
            continue
        if marker is not None and not (
            row.entity_ref == marker or row.entity_ref.startswith(f"{marker}:")
        ):
            continue
        _adopt_legacy_row(row, context, legacy_bridge)
        if _stamp_resolution(row, actor_user_id):
            changed += 1
    if changed:
        await session.flush()
    return changed


async def was_scoped_dismissed_today(
    session: AsyncSession,
    *,
    context: AlertContext,
    alert_key: str,
    entity_ref: str,
    on_date: date_type | None = None,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> bool:
    """Return whether this scoped alert was resolved on one local calendar date."""

    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    if on_date is not None and not isinstance(on_date, date_type):
        raise AlertValidationError("on_date must be a date or None")
    legacy_bridge = await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=False,
    )
    allowed_domains = await _allowed_domains_for_key(session, context, alert_key)
    resolved_date = on_date or today_local()
    count = await session.scalar(
        select(func.count()).where(
            SystemAlert.alert_key == alert_key,
            SystemAlert.entity_ref == entity_ref,
            SystemAlert.resolved_at.is_not(None),
            func.date(SystemAlert.resolved_at) == resolved_date,
            SystemAlert.domain.in_(tuple(allowed_domains)),
            _candidate_scope_predicate(context, legacy_bridge),
        )
    )
    return (count or 0) > 0


async def was_scoped_ever_dismissed(
    session: AsyncSession,
    *,
    context: AlertContext,
    alert_key: str,
    entity_ref: str,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> bool:
    """Return whether this exact scoped alert was ever resolved."""

    _require_key(alert_key)
    _require_entity_ref(entity_ref)
    legacy_bridge = await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=False,
    )
    allowed_domains = await _allowed_domains_for_key(session, context, alert_key)
    count = await session.scalar(
        select(func.count()).where(
            SystemAlert.alert_key == alert_key,
            SystemAlert.entity_ref == entity_ref,
            SystemAlert.resolved_at.is_not(None),
            SystemAlert.domain.in_(tuple(allowed_domains)),
            _candidate_scope_predicate(context, legacy_bridge),
        )
    )
    return (count or 0) > 0


async def list_active_scoped(
    session: AsyncSession,
    *,
    context: AlertContext,
    domain: Domain | None = None,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> Sequence[SystemAlert]:
    """List active alerts visible in exactly one typed ownership context."""

    _require_domain(domain, optional=True)
    legacy_bridge = await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=False,
    )
    _validate_platform_domain(context, domain)
    stmt = select(SystemAlert).where(
        SystemAlert.resolved_at.is_(None),
        _candidate_scope_predicate(context, legacy_bridge),
    )
    if domain is not None:
        stmt = stmt.where(SystemAlert.domain == domain.value)
    stmt = stmt.order_by(SystemAlert.created_at.desc(), SystemAlert.id.desc())
    rows = list(await session.scalars(stmt))
    for row in rows:
        await _validate_row_semantics(session, row, context)
    return rows


async def resolve_all_scoped(
    session: AsyncSession,
    *,
    context: AlertContext,
    domain: Domain | None = None,
    legacy_bridge: LegacyAlertBridge = LegacyAlertBridge.REJECT,
) -> int:
    """Resolve all currently active alerts in one exact ownership scope."""

    _require_domain(domain, optional=True)
    legacy_bridge = await _prepare_context(
        session,
        context=context,
        legacy_bridge=legacy_bridge,
        fresh_provider_write=False,
        lock_roots=True,
    )
    _validate_platform_domain(context, domain)
    predicate = _candidate_scope_predicate(context, legacy_bridge)
    key_stmt = select(SystemAlert.alert_key).where(SystemAlert.resolved_at.is_(None), predicate)
    if domain is not None:
        key_stmt = key_stmt.where(SystemAlert.domain == domain.value)
    keys = sorted(set(await session.scalars(key_stmt)))
    for alert_key in keys:
        await _allowed_domains_for_key(session, context, alert_key)
        await _acquire_alert_key_lock(session, alert_key)

    stmt = select(SystemAlert).where(SystemAlert.resolved_at.is_(None), predicate)
    if domain is not None:
        stmt = stmt.where(SystemAlert.domain == domain.value)
    rows = list(
        await session.scalars(stmt.with_for_update().execution_options(populate_existing=True))
    )
    actor_user_id = _actor_user_id(context)
    for row in rows:
        await _validate_row_semantics(session, row, context)
        _adopt_legacy_row(row, context, legacy_bridge)
        _stamp_resolution(row, actor_user_id)
    if rows:
        await session.flush()
    return len(rows)
