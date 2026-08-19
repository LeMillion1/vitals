"""HRT reminders — hormone-panel bloodwork seeding + the two scheduled nags.

Two protocol-aware reminders, complementary to Labs' own generic per-marker
overdue-retest alert:

  * **Bloodwork due** (``hrt.labs_due``) — while a cycle is active, if no
    hormone-panel result exists within a kind-dependent window (PCT needs
    tighter monitoring than a course), raise a passive ``warn``.
  * **Injection due** (``hrt.injection_due``) — for each active cycle item, if the
    most recent shot the fixed-grid schedule expected by today hasn't been logged,
    raise a per-compound ``info`` nag. Fixed grid: being late doesn't shift it.

Both are idempotent, respect same-day dismissal, and resolve themselves once the
condition clears — safe on every dashboard load and scheduler tick.

:func:`seed_hormone_panel` registers the panel markers in the Labs catalog (with
a retest interval + ``hrt_panel`` category) so they also power Labs' own overdue
alert and show up as a coherent group. Called once at startup, idempotent.
"""
from __future__ import annotations

import logging
from datetime import date as date_type
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Severity
from vitals.i18n import current_lang, t
from vitals.models.hrt import DOMAIN, HrtCycle, HrtCycleItem
from vitals.models.identity import HealthSubject
from vitals.models.labs import LabMarker, LabResult
from vitals.ownership import WriteIdentity
from vitals.services import (
    alerts_service,
    conflict_engine,
    hrt_cycle_service,
    hrt_service,
    labs_service,
    modules_service,
)
from vitals.utils.timeutils import today_local

logger = logging.getLogger(__name__)

LABS_DUE_KEY = "hrt.labs_due"
INJECTION_DUE_KEY = "hrt.injection_due"

# Hormone / safety panel: canonical marker name -> retest interval (days). Names
# are in the normalized form Labs stores (labs_service.normalize_marker), so a
# user-logged result lands on the same row.
HORMONE_PANEL: dict[str, int] = {
    "Тестостерон общий": 90,
    "Тестостерон свободный": 90,
    "Эстрадиол": 90,
    "ЛГ": 90,
    "ФСГ": 90,
    "Пролактин": 90,
    "ГСПГ": 90,
    "Гематокрит": 90,
    "Гемоглобин": 90,
    "АЛТ": 90,
    "АСТ": 90,
    "ПСА": 180,
}
_PANEL_CATEGORY = "hrt_panel"

# How stale the panel may get before nagging, by cycle kind. PCT needs tight
# monitoring (is natural production actually restarting?); a course follows the
# standard quarterly panel.
PANEL_WINDOW_BY_KIND: dict[str, int] = {
    "course": 90,
    "pct": 30,
}
_DEFAULT_PANEL_WINDOW = 90


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None,
    prepared: conflict_engine.PreparedConflictWrite | None,
) -> conflict_engine.ConflictWriteContext | None:
    if identity is None and prepared is None:
        return None
    if identity is None or prepared is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped HRT reminders require identity and a prepared conflict write"
        )
    return conflict_engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


def _reminder_date(
    context: conflict_engine.ConflictWriteContext | None,
    on_date: date_type | None,
) -> date_type:
    if context is None:
        return on_date or today_local()
    if on_date is not None and on_date != context.evaluation_date:
        raise conflict_engine.ConflictPreparedWriteError(
            "HRT reminder date does not match prepared conflict evaluation date"
        )
    return context.evaluation_date


def _alert_bridge(
    context: conflict_engine.ConflictWriteContext,
) -> alerts_service.LegacyAlertBridge:
    if context.legacy_bridge is conflict_engine.LegacyConflictBridge.FULLY_UNOWNED:
        return alerts_service.LegacyAlertBridge.FULLY_UNOWNED
    return alerts_service.LegacyAlertBridge.REJECT


def _system_alert_context(
    context: conflict_engine.ConflictWriteContext,
) -> alerts_service.HealthAlertContext:
    return alerts_service.HealthAlertContext(
        WriteIdentity(context.identity.subject_id, None)
    )


def _cycle_scope(
    context: conflict_engine.ConflictWriteContext,
):
    scope = HrtCycle.subject_id == context.identity.subject_id
    if context.scope.include_legacy_unowned:
        scope = or_(
            scope,
            and_(
                HrtCycle.subject_id.is_(None),
                HrtCycle.actor_user_id.is_(None),
            ),
        )
    return scope


async def _active_cycle(
    session: AsyncSession,
    *,
    on_date: date_type,
    context: conflict_engine.ConflictWriteContext | None,
) -> HrtCycle | None:
    if context is None:
        return await hrt_cycle_service.active_cycle(session, on_date=on_date)
    cycle = await session.scalar(
        select(HrtCycle)
        .where(
            HrtCycle.domain == DOMAIN,
            HrtCycle.start_date <= on_date,
            or_(HrtCycle.end_date.is_(None), HrtCycle.end_date >= on_date),
            _cycle_scope(context),
        )
        .order_by(HrtCycle.start_date.desc(), HrtCycle.id.desc())
        .limit(1)
    )
    if cycle is None:
        return None

    if context.scope.include_legacy_unowned:
        invalid_item_scope = and_(
            HrtCycleItem.subject_id.is_not(None),
            HrtCycleItem.subject_id != context.identity.subject_id,
        )
    else:
        invalid_item_scope = or_(
            HrtCycleItem.subject_id.is_(None),
            HrtCycleItem.subject_id != context.identity.subject_id,
        )
    invalid_item = await session.scalar(
        select(1)
        .select_from(HrtCycleItem)
        .where(
            HrtCycleItem.cycle_id == cycle.id,
            invalid_item_scope,
        )
        .limit(1)
    )
    if invalid_item is not None:
        raise conflict_engine.ConflictScopeError(
            "HRT reminder cycle contains an item outside the subject scope"
        )
    return cycle


async def _resolve_alert(
    session: AsyncSession,
    *,
    alert_key: str,
    entity_ref: str,
    context: conflict_engine.ConflictWriteContext | None,
) -> object | None:
    if context is None:
        return await alerts_service.resolve_by_key(
            session,
            alert_key=alert_key,
            entity_ref=entity_ref,
        )
    return await alerts_service.resolve_scoped_by_key(
        session,
        context=_system_alert_context(context),
        alert_key=alert_key,
        entity_ref=entity_ref,
        legacy_bridge=_alert_bridge(context),
    )


async def _was_dismissed_today(
    session: AsyncSession,
    *,
    alert_key: str,
    entity_ref: str,
    on_date: date_type,
    context: conflict_engine.ConflictWriteContext | None,
) -> bool:
    if context is None:
        return await alerts_service._was_dismissed_today(
            session,
            alert_key,
            entity_ref,
            on_date=on_date,
        )
    return await alerts_service.was_scoped_dismissed_today(
        session,
        context=_system_alert_context(context),
        alert_key=alert_key,
        entity_ref=entity_ref,
        on_date=on_date,
        legacy_bridge=_alert_bridge(context),
    )


async def _raise_alert(
    session: AsyncSession,
    *,
    severity: Severity,
    message: str,
    alert_key: str,
    entity_ref: str,
    context: conflict_engine.ConflictWriteContext | None,
) -> object:
    if context is None:
        return await alerts_service.raise_alert(
            session,
            domain=Domain.HRT.value,
            severity=severity.value,
            message=message,
            alert_key=alert_key,
            entity_ref=entity_ref,
        )
    return await alerts_service.raise_scoped_alert(
        session,
        context=_system_alert_context(context),
        domain=Domain.HRT,
        severity=severity,
        message=message,
        alert_key=alert_key,
        entity_ref=entity_ref,
        legacy_bridge=_alert_bridge(context),
    )


async def seed_hormone_panel(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> dict[str, int]:
    """Register the panel markers in the Labs catalog. Idempotent — creates a
    missing marker, and backfills ``category``/``retest_interval_days`` on an
    existing one only when unset (never clobbers a user's edit)."""
    created = 0
    updated = 0
    if (identity is None) != (prepared_conflict_write is None):
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped hormone-panel seed requires identity and a prepared write"
        )
    for name, interval in HORMONE_PANEL.items():
        if identity is not None:
            assert prepared_conflict_write is not None
            _row, was_created, was_updated = (
                await labs_service.ensure_marker_catalog_entry(
                    session,
                    name=name,
                    category=_PANEL_CATEGORY,
                    retest_interval_days=interval,
                    identity=identity,
                    include_legacy_unowned=include_legacy_unowned,
                    prepared_conflict_write=prepared_conflict_write,
                )
            )
            created += int(was_created)
            updated += int(was_updated)
            continue
        row = await labs_service.get_marker(session, name)
        if row is None:
            session.add(
                LabMarker(
                    domain=labs_service.DOMAIN,
                    name=labs_service.normalize_marker(name),
                    category=_PANEL_CATEGORY,
                    retest_interval_days=interval,
                )
            )
            created += 1
        else:
            touched = False
            if row.category is None:
                row.category = _PANEL_CATEGORY
                touched = True
            if row.retest_interval_days is None:
                row.retest_interval_days = interval
                touched = True
            if touched:
                updated += 1
    await session.flush()
    logger.info("hrt_reminders.seed_hormone_panel: %d created, %d updated", created, updated)
    return {"created": created, "updated": updated}


async def _latest_panel_result_date(
    session: AsyncSession,
    *,
    on_date: date_type,
    context: conflict_engine.ConflictWriteContext | None,
) -> Optional[date_type]:
    names = [labs_service.normalize_marker(n) for n in HORMONE_PANEL]
    if context is None:
        result = await session.execute(
            select(LabResult.date)
            .where(LabResult.marker.in_(names), LabResult.date <= on_date)
            .order_by(LabResult.date.desc())
            .limit(1)
        )
        return result.scalars().first()
    rows = await labs_service.list_results(
        session,
        end=on_date,
        limit=1_000_000,
        subject_id=context.identity.subject_id,
        include_legacy_unowned=context.scope.include_legacy_unowned,
    )
    return max((row.date for row in rows if row.marker in names), default=None)


async def refresh_labs_due(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    identity: WriteIdentity | None = None,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> None:
    """Raise/clear the bloodwork-due warn for the active cycle. No cycle → clear."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    today = _reminder_date(context, on_date)
    cycle = await _active_cycle(session, on_date=today, context=context)
    if cycle is None:
        await _resolve_alert(
            session,
            alert_key=LABS_DUE_KEY,
            entity_ref="",
            context=context,
        )
        return

    window = PANEL_WINDOW_BY_KIND.get(cycle.kind, _DEFAULT_PANEL_WINDOW)
    latest = await _latest_panel_result_date(
        session,
        on_date=today,
        context=context,
    )
    overdue = latest is None or (today - latest).days > window
    if overdue:
        if await _was_dismissed_today(
            session,
            alert_key=LABS_DUE_KEY,
            entity_ref="",
            on_date=today,
            context=context,
        ):
            return
        await _raise_alert(
            session,
            severity=Severity.WARN,
            message=t("alert.hrt_labs_due", days=window),
            alert_key=LABS_DUE_KEY,
            entity_ref="",
            context=context,
        )
    else:
        await _resolve_alert(
            session,
            alert_key=LABS_DUE_KEY,
            entity_ref="",
            context=context,
        )


async def _compound_display_name(
    session: AsyncSession,
    key: str,
    *,
    context: conflict_engine.ConflictWriteContext | None,
) -> str:
    """Localized catalog name for a compound key (falls back to the key for a
    free-text/custom compound not in the catalog)."""
    compound = await hrt_service.get_compound(
        session,
        key,
        subject_id=(context.identity.subject_id if context is not None else None),
    )
    if compound is None:
        return key
    if current_lang.get() == "ru":
        return compound.name_ru or compound.name or key
    return compound.name or compound.name_ru or key


async def _last_actual_dose_date(
    session: AsyncSession,
    compound_key: str,
    *,
    on_date: date_type,
    context: conflict_engine.ConflictWriteContext | None,
) -> Optional[date_type]:
    rows = await hrt_service.list_doses(
        session,
        subject_id=(context.identity.subject_id if context is not None else None),
        include_legacy_unowned=(
            context.scope.include_legacy_unowned if context is not None else False
        ),
        end=on_date,
    )
    return next((row.date for row in rows if row.compound_key == compound_key), None)


async def refresh_injection_due(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    identity: WriteIdentity | None = None,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> None:
    """Per active-cycle-item: nag if the last shot the fixed grid expected by
    today hasn't been logged. Resolves per compound once caught up, and clears any
    stale alert whose compound is no longer planned (cycle ended/deleted or the
    item removed) so a nag never outlives the plan that raised it."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    today = _reminder_date(context, on_date)
    cycle = await _active_cycle(session, on_date=today, context=context)
    planned_keys: set[str] = set()

    if cycle is not None:
        for item in cycle.items:
            entity = item.compound_key
            planned_keys.add(entity)
            planned = hrt_cycle_service.expand_item_schedule(
                item, cycle.start_date, cycle.start_date, today
            )
            if not planned:
                await _resolve_alert(
                    session,
                    alert_key=INJECTION_DUE_KEY,
                    entity_ref=entity,
                    context=context,
                )
                continue
            last_planned = planned[-1][0]
            last_actual = await _last_actual_dose_date(
                session,
                entity,
                on_date=today,
                context=context,
            )
            overdue = last_actual is None or last_actual < last_planned
            if overdue:
                if await _was_dismissed_today(
                    session,
                    alert_key=INJECTION_DUE_KEY,
                    entity_ref=entity,
                    on_date=today,
                    context=context,
                ):
                    continue
                await _raise_alert(
                    session,
                    severity=Severity.INFO,
                    message=t(
                        "alert.hrt_injection_due",
                        compound=await _compound_display_name(
                            session,
                            entity,
                            context=context,
                        ),
                        date=last_planned.isoformat(),
                    ),
                    alert_key=INJECTION_DUE_KEY,
                    entity_ref=entity,
                    context=context,
                )
            else:
                await _resolve_alert(
                    session,
                    alert_key=INJECTION_DUE_KEY,
                    entity_ref=entity,
                    context=context,
                )

    # Clear stale nags for compounds no longer in the active plan.
    if context is None:
        active_alerts = await alerts_service.list_active(
            session,
            domain=Domain.HRT.value,
        )
    else:
        active_alerts = await alerts_service.list_active_scoped(
            session,
            context=_system_alert_context(context),
            domain=Domain.HRT,
            legacy_bridge=_alert_bridge(context),
        )
    for alert in active_alerts:
        if alert.alert_key == INJECTION_DUE_KEY and alert.entity_ref not in planned_keys:
            await _resolve_alert(
                session,
                alert_key=INJECTION_DUE_KEY,
                entity_ref=alert.entity_ref,
                context=context,
            )


async def refresh_all(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    identity: WriteIdentity | None = None,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> None:
    """Run both reminders — called from the dashboard load and the scheduled job."""
    await refresh_labs_due(
        session,
        on_date=on_date,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    await refresh_injection_due(
        session,
        on_date=on_date,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )


async def reminders_job(session_factory, redis=None) -> None:
    """Daily HRT reminders (registered in vitals/scheduler/jobs.py)."""
    async with session_factory() as session:
        today = today_local()
        context = await conflict_engine.resolve_legacy_conflict_write_context(
            session,
            actor_username=None,
            evaluation_date=today,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=context,
        )
        enabled = await modules_service.get_enabled_modules(
            session,
            redis,
            subject_id=context.identity.subject_id,
        )
        if not enabled.get("hrt", False):
            await session.commit()
            return

        from vitals.i18n import current_lang
        from vitals.services.language_service import get_language

        owner_user_id = await session.scalar(
            select(HealthSubject.owner_user_id).where(
                HealthSubject.id == context.identity.subject_id
            )
        )
        lang = await get_language(session, redis, user_id=owner_user_id)
        current_lang.set(lang)

        await refresh_all(
            session,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
        await session.commit()
