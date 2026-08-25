"""Control-state preflight for a portability-v2 subject replacement.

The replacement writer owns the outer transaction.  This module only acquires
the locks that must survive through that transaction, rejects unsafe control
state, resolves active subject alerts, and flushes those alert updates.  It does
not delete portable rows, commit, or roll back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import SupportRepairStatus
from vitals.models.ai import AIInvocation
from vitals.models.garmin import (
    WEIGHT_EXPORT_DELETED,
    WEIGHT_EXPORT_MATCHED,
    WEIGHT_EXPORT_SKIPPED,
    GarminWeightExport,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.proactive import NotificationDeliveryIntent
from vitals.models.support_repair import SupportRepairAction
from vitals.models.system_alert import SystemAlert
from vitals.services.garmin_weight_service import lock_active_weight_change
from vitals.services.identity_service import acquire_identity_governance_lock


_TERMINAL_GARMIN_STATUSES: Final = frozenset(
    {
        WEIGHT_EXPORT_MATCHED,
        WEIGHT_EXPORT_SKIPPED,
        WEIGHT_EXPORT_DELETED,
    }
)
_OPEN_REPAIR_STATUSES: Final = frozenset(
    {
        SupportRepairStatus.PROPOSED.value,
        SupportRepairStatus.APPROVED.value,
    }
)
_TERMINAL_REPAIR_STATUSES: Final = frozenset(
    {
        SupportRepairStatus.DECLINED.value,
        SupportRepairStatus.EXECUTED.value,
        SupportRepairStatus.STALE.value,
        SupportRepairStatus.REVERTED.value,
    }
)


class ReplacementPreflightError(RuntimeError):
    """A stable, PHI-free reason why replacement preparation was refused."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReplacementPreflightPlan:
    """Immutable control metadata the replacement delete phase must honour.

    ``retained_raw_payload_ids`` is an exclusion set: those raw rows belong to
    the portable subject graph but are also referenced by non-portable AI or
    delivery audit history, whose ``RESTRICT`` provenance must survive replace.
    Terminal Garmin and repair identifiers are evidence of preserved audit
    history, not exclusions for their old portable targets.
    """

    subject_id: uuid.UUID
    actor_user_id: uuid.UUID
    database_now: datetime
    retained_raw_payload_ids: tuple[int, ...]
    preserved_terminal_garmin_export_ids: tuple[int, ...]
    preserved_terminal_repair_action_ids: tuple[uuid.UUID, ...]
    detached_terminal_repair_count: int
    resolved_system_alert_ids: tuple[int, ...]


def _error(code: str, detail: str) -> ReplacementPreflightError:
    return ReplacementPreflightError(code, detail)


def _require_uuid(value: object, *, code: str, detail: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise _error(code, detail)
    return value


async def _lock_subject_and_actor(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    subject = await session.scalar(
        select(HealthSubject.id).where(HealthSubject.id == subject_id).with_for_update()
    )
    if subject is None:
        raise _error(
            "replacement_subject_not_found",
            "replacement subject does not exist",
        )
    actor = await session.scalar(select(User.id).where(User.id == actor_user_id).with_for_update())
    if actor is None:
        raise _error(
            "replacement_actor_not_found",
            "replacement actor does not exist",
        )


async def _inspect_garmin_exports(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> tuple[int, ...]:
    rows = tuple(
        await session.execute(
            select(GarminWeightExport.id, GarminWeightExport.status)
            .where(GarminWeightExport.subject_id == subject_id)
            .order_by(GarminWeightExport.id)
            .with_for_update()
        )
    )
    if any(status not in _TERMINAL_GARMIN_STATUSES for _, status in rows):
        raise _error(
            "replacement_garmin_export_nonterminal",
            "replacement is blocked by nonterminal Garmin export state",
        )
    return tuple(row_id for row_id, _ in rows)


async def _inspect_support_repairs(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> tuple[tuple[uuid.UUID, ...], int]:
    rows = tuple(
        await session.execute(
            select(SupportRepairAction.id, SupportRepairAction.status)
            .where(SupportRepairAction.subject_id == subject_id)
            .order_by(SupportRepairAction.id)
            .with_for_update()
        )
    )
    if any(status in _OPEN_REPAIR_STATUSES for _, status in rows):
        raise _error(
            "replacement_support_repair_open",
            "replacement is blocked by an open support repair",
        )
    if any(status not in _TERMINAL_REPAIR_STATUSES for _, status in rows):
        raise _error(
            "replacement_support_repair_state_invalid",
            "replacement is blocked by invalid support repair state",
        )
    terminal_actions = tuple(
        await session.scalars(
            select(SupportRepairAction)
            .where(SupportRepairAction.subject_id == subject_id)
            .order_by(SupportRepairAction.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    detached_count = 0
    for action in terminal_actions:
        if action.target_body_measurement_id is not None:
            action.target_body_measurement_id = None
            detached_count += 1
    return tuple(action.id for action in terminal_actions), detached_count


async def _retained_raw_payload_ids(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> tuple[int, ...]:
    ai_ids = tuple(
        await session.scalars(
            select(AIInvocation.raw_payload_id)
            .where(
                AIInvocation.subject_id == subject_id,
                AIInvocation.raw_payload_id.is_not(None),
            )
            .order_by(AIInvocation.raw_payload_id)
            .with_for_update()
        )
    )
    delivery_ids = tuple(
        await session.scalars(
            select(NotificationDeliveryIntent.raw_payload_id)
            .where(
                NotificationDeliveryIntent.subject_id == subject_id,
                NotificationDeliveryIntent.raw_payload_id.is_not(None),
            )
            .order_by(NotificationDeliveryIntent.raw_payload_id)
            .with_for_update()
        )
    )
    return tuple(sorted({*ai_ids, *delivery_ids}))


async def _resolve_active_subject_alerts(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    database_now: datetime,
) -> tuple[int, ...]:
    alerts = tuple(
        await session.scalars(
            select(SystemAlert)
            .where(
                SystemAlert.subject_id == subject_id,
                SystemAlert.resolved_at.is_(None),
            )
            .order_by(SystemAlert.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for alert in alerts:
        alert.resolved_at = database_now
        alert.resolved_by_user_id = actor_user_id
    return tuple(alert.id for alert in alerts)


async def prepare_replacement_preflight(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> ReplacementPreflightPlan:
    """Lock and prepare non-portable control state for one subject replace.

    The locks are intentionally transaction-scoped.  The caller must perform
    the portable-row replacement and receipt write in this same transaction;
    returning this plan and committing before deletion would reopen the races
    that the preflight closes.
    """

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    subject_id = _require_uuid(
        subject_id,
        code="replacement_subject_invalid",
        detail="replacement subject identifier is invalid",
    )
    actor_user_id = _require_uuid(
        actor_user_id,
        code="replacement_actor_invalid",
        detail="replacement actor identifier is invalid",
    )

    # This is the canonical Garmin writer lock order.  Holding both advisory
    # locks through the outer replace transaction prevents an exporter from
    # transitioning a terminal row after this service has classified it.
    await acquire_identity_governance_lock(session)
    await lock_active_weight_change(session)

    with session.no_autoflush:
        await _lock_subject_and_actor(
            session,
            subject_id=subject_id,
            actor_user_id=actor_user_id,
        )
        garmin_ids = await _inspect_garmin_exports(
            session,
            subject_id=subject_id,
        )
        repair_ids, detached_repair_count = await _inspect_support_repairs(
            session,
            subject_id=subject_id,
        )
        retained_raw_ids = await _retained_raw_payload_ids(
            session,
            subject_id=subject_id,
        )
        # PostgreSQL ``now()`` is timezone-aware, while this legacy alert
        # column is intentionally ``timestamp without time zone``.  Cast in the
        # database so the lifecycle stamp is still the DB clock and asyncpg
        # receives the exact Python shape the mapped column accepts.
        clock_expression = (
            cast(func.now(), DateTime(timezone=False))
            if session.get_bind().dialect.name == "postgresql"
            else func.now()
        )
        database_now = await session.scalar(select(clock_expression))
        if not isinstance(database_now, datetime):
            raise _error(
                "replacement_database_clock_unavailable",
                "database did not return a replacement timestamp",
            )
        alert_ids = await _resolve_active_subject_alerts(
            session,
            subject_id=subject_id,
            actor_user_id=actor_user_id,
            database_now=database_now,
        )

    await session.flush()
    return ReplacementPreflightPlan(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        database_now=database_now,
        retained_raw_payload_ids=retained_raw_ids,
        preserved_terminal_garmin_export_ids=garmin_ids,
        preserved_terminal_repair_action_ids=repair_ids,
        detached_terminal_repair_count=detached_repair_count,
        resolved_system_alert_ids=alert_ids,
    )


__all__ = [
    "ReplacementPreflightError",
    "ReplacementPreflightPlan",
    "prepare_replacement_preflight",
]
