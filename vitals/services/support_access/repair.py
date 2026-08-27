"""Schema-fixed, separately reviewed support repair workflow."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.access import AccessContext, AccessRequest, PolicyAction, PolicyResourceType, is_allowed
from vitals.enums import (
    AuditOutcome,
    Domain,
    SupportAccessMode,
    SupportAccessStatus,
    SupportRepairStatus,
)
from vitals.models.identity import AuditEvent, SupportAccessGrant
from vitals.models.support_repair import SupportRepairAction
from vitals.models.weight import BodyMeasurement
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.support_access.contracts import (
    AUDIT_SURFACE,
    EVENT_REPAIR_APPROVED,
    EVENT_REPAIR_DECLINED,
    EVENT_REPAIR_EXECUTED,
    EVENT_REPAIR_PROPOSED,
    EVENT_REPAIR_REVERTED,
    EVENT_REPAIR_STALE,
    REPAIR_OPERATION_KEY,
    NotASupportSession,
    NotTheSubjectOwner,
    RepairNotFound,
    RepairStateError,
    SupportAccessError,
    _as_utc,
    _exact_repair_scope_rows,
    _now,
    _require_platform_admin,
    _require_subject_owner,
)

def _repair_audit(
    session: AsyncSession,
    *,
    action: SupportRepairAction,
    actor_user_id: uuid.UUID,
    event_type: str,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    result_code: str,
) -> None:
    """Append a grant-correlated repair event without copying medical values."""

    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            subject_id=action.subject_id,
            support_access_grant_id=action.support_access_grant_id,
            event_type=event_type,
            outcome=outcome.value,
            resource_type="body_measurement",
            resource_id=str(action.target_body_measurement_id),
            metadata_json={
                "request_id": str(action.id),
                "source_surface": AUDIT_SURFACE,
                "result_code": result_code,
                "reason_code": "approved_support_repair",
                "resource_type": "body_measurement",
                "resource_id": str(action.target_body_measurement_id),
                "changed_fields": ["body_fat_pct", "lbm_kg"],
                "grant_mode": SupportAccessMode.REPAIR.value,
            },
        )
    )


def _grant_has_exact_repair_scope(grant: SupportAccessGrant) -> bool:
    return {
        (scope.resource_type, scope.resource_key, scope.action)
        for scope in grant.scopes
    } == _exact_repair_scope_rows() and len(grant.scopes) == 2


def _context_has_exact_repair_scope(context: AccessContext) -> bool:
    required = (
        AccessRequest(
            subject_id=context.subject_id,
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=Domain.WEIGHT.value,
            action=PolicyAction.READ,
        ),
        AccessRequest(
            subject_id=context.subject_id,
            resource_type=PolicyResourceType.OPERATION,
            resource_key=REPAIR_OPERATION_KEY,
            action=PolicyAction.REPAIR,
        ),
    )
    return all(is_allowed(context, request) for request in required)


async def _lock_live_repair_grant(
    session: AsyncSession, *, context: AccessContext
) -> tuple[SupportAccessGrant, datetime]:
    snapshot = context.support_grant
    if (
        snapshot is None
        or snapshot.subject_id != context.subject_id
        or snapshot.granted_to_user_id != context.principal.user_id
        or snapshot.mode is not SupportAccessMode.REPAIR
        or not _context_has_exact_repair_scope(context)
    ):
        raise NotASupportSession("repair is not based on the exact approved grant")

    await acquire_identity_governance_lock(session)
    await _require_platform_admin(session, user_id=context.principal.user_id)
    grant = await session.scalar(
        select(SupportAccessGrant)
        .options(selectinload(SupportAccessGrant.scopes))
        .where(
            SupportAccessGrant.id == snapshot.grant_id,
            SupportAccessGrant.subject_id == context.subject_id,
            SupportAccessGrant.granted_to_user_id == context.principal.user_id,
        )
        .with_for_update()
    )
    if grant is None:
        raise NotASupportSession("the repair grant no longer exists")
    now = await _now(session)
    if (
        grant.mode != SupportAccessMode.REPAIR.value
        or grant.status != SupportAccessStatus.ACTIVE.value
        or grant.revoked_at is not None
        or now >= _as_utc(grant.expires_at)
        or not _grant_has_exact_repair_scope(grant)
    ):
        raise NotASupportSession("the repair grant is no longer exact and active")
    return grant, now


@dataclass(frozen=True, slots=True)
class RepairMeasurement:
    measurement_id: int
    date: object
    body_fat_pct: float | None
    lbm_kg: float | None


@dataclass(frozen=True, slots=True)
class RepairActionView:
    action_id: uuid.UUID
    grant_id: uuid.UUID
    operator_username: str
    measurement_id: int | None
    measurement_date: object
    before_body_fat_pct: float | None
    before_lbm_kg: float | None
    status: str
    proposed_at: datetime
    execute_before: datetime


def _effective_repair_status(
    action: SupportRepairAction, *, now: datetime
) -> str:
    if action.status not in {
        SupportRepairStatus.PROPOSED.value,
        SupportRepairStatus.APPROVED.value,
    }:
        return action.status
    grant = action.grant
    if (
        grant.status != SupportAccessStatus.ACTIVE.value
        or grant.revoked_at is not None
        or now >= _as_utc(grant.expires_at)
        or now >= _as_utc(action.execute_before)
    ):
        return "expired"
    return action.status


def _repair_view(
    action: SupportRepairAction, *, now: datetime
) -> RepairActionView:
    return RepairActionView(
        action_id=action.id,
        grant_id=action.support_access_grant_id,
        operator_username=action.proposed_by.username,
        measurement_id=action.target_body_measurement_id,
        measurement_date=action.target_date,
        before_body_fat_pct=action.before_body_fat_pct,
        before_lbm_kg=action.before_lbm_kg,
        status=_effective_repair_status(action, now=now),
        proposed_at=_as_utc(action.proposed_at),
        execute_before=_as_utc(action.execute_before),
    )


async def repair_workspace(
    session: AsyncSession, *, context: AccessContext
) -> tuple[tuple[RepairMeasurement, ...], tuple[RepairActionView, ...]]:
    """Subject-bound proposal workspace for one exact repair grant."""

    grant, now = await _lock_live_repair_grant(session, context=context)
    measurements = tuple(
        RepairMeasurement(
            measurement_id=row.id,
            date=row.date,
            body_fat_pct=row.body_fat_pct,
            lbm_kg=row.lbm_kg,
        )
        for row in await session.scalars(
            select(BodyMeasurement)
            .where(
                BodyMeasurement.subject_id == context.subject_id,
                (BodyMeasurement.body_fat_pct.is_not(None))
                | (BodyMeasurement.lbm_kg.is_not(None)),
            )
            .order_by(BodyMeasurement.date.desc(), BodyMeasurement.id.desc())
            .limit(100)
        )
    )
    actions = list(
        await session.scalars(
            select(SupportRepairAction)
            .options(
                selectinload(SupportRepairAction.grant),
                selectinload(SupportRepairAction.proposed_by),
                selectinload(SupportRepairAction.target),
            )
            .where(SupportRepairAction.support_access_grant_id == grant.id)
            .order_by(SupportRepairAction.proposed_at.desc())
            .limit(50)
        )
    )
    return measurements, tuple(_repair_view(row, now=now) for row in actions)


async def propose_clear_derived_estimates(
    session: AsyncSession,
    *,
    context: AccessContext,
    measurement_id: int,
    idempotency_key: uuid.UUID,
) -> SupportRepairAction:
    """Propose the fixed NULL/NULL diff. It does not mutate the measurement."""

    if isinstance(measurement_id, bool) or not isinstance(measurement_id, int):
        raise RepairNotFound("measurement id must be an integer")
    if not isinstance(idempotency_key, uuid.UUID) or idempotency_key.int == 0:
        raise SupportAccessError("idempotency key must be a non-zero UUID")
    grant, now = await _lock_live_repair_grant(session, context=context)
    existing = await session.scalar(
        select(SupportRepairAction).where(
            SupportRepairAction.support_access_grant_id == grant.id,
            SupportRepairAction.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    target = await session.scalar(
        select(BodyMeasurement)
        .where(
            BodyMeasurement.id == measurement_id,
            BodyMeasurement.subject_id == context.subject_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise RepairNotFound("no such body measurement in this record")
    if target.body_fat_pct is None and target.lbm_kg is None:
        raise RepairStateError("the derived estimates are already absent")
    open_action = await session.scalar(
        select(SupportRepairAction.id).where(
            SupportRepairAction.subject_id == context.subject_id,
            SupportRepairAction.target_body_measurement_id == target.id,
            SupportRepairAction.operation_key == REPAIR_OPERATION_KEY,
            SupportRepairAction.status.in_(
                (
                    SupportRepairStatus.PROPOSED.value,
                    SupportRepairStatus.APPROVED.value,
                )
            ),
        )
    )
    if open_action is not None:
        raise RepairStateError("this grant already has an open action for the target")

    action = SupportRepairAction(
        subject_id=context.subject_id,
        support_access_grant_id=grant.id,
        proposed_by_user_id=context.principal.user_id,
        operation_key=REPAIR_OPERATION_KEY,
        target_body_measurement_id=target.id,
        target_date=target.date,
        status=SupportRepairStatus.PROPOSED.value,
        idempotency_key=idempotency_key,
        proposed_at=now,
        execute_before=_as_utc(grant.expires_at),
        before_body_fat_pct=target.body_fat_pct,
        before_lbm_kg=target.lbm_kg,
        target_updated_at_at_proposal=target.updated_at,
    )
    session.add(action)
    await session.flush()
    _repair_audit(
        session,
        action=action,
        actor_user_id=context.principal.user_id,
        event_type=EVENT_REPAIR_PROPOSED,
        result_code="proposed",
    )
    await session.flush()
    return action


async def review_repair(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    action_id: uuid.UUID,
    approve: bool,
) -> SupportRepairAction:
    """The record owner separately approves or declines one exact diff."""

    action = await session.scalar(
        select(SupportRepairAction)
        .options(selectinload(SupportRepairAction.grant).selectinload(SupportAccessGrant.scopes))
        .where(SupportRepairAction.id == action_id)
        .with_for_update()
    )
    if action is None:
        raise RepairNotFound("no such support repair")
    await _require_subject_owner(
        session, user_id=owner_user_id, subject_id=action.subject_id
    )
    if action.status != SupportRepairStatus.PROPOSED.value:
        raise RepairStateError("this repair proposal was already reviewed")
    now = await _now(session)
    if approve:
        grant = action.grant
        await _require_platform_admin(session, user_id=action.proposed_by_user_id)
        if (
            grant.mode != SupportAccessMode.REPAIR.value
            or grant.status != SupportAccessStatus.ACTIVE.value
            or grant.revoked_at is not None
            or now >= _as_utc(grant.expires_at)
            or now >= _as_utc(action.execute_before)
            or not _grant_has_exact_repair_scope(grant)
        ):
            raise RepairStateError("the repair grant is no longer active")
    action.status = (
        SupportRepairStatus.APPROVED.value
        if approve
        else SupportRepairStatus.DECLINED.value
    )
    action.reviewed_by_user_id = owner_user_id
    action.reviewed_at = now
    _repair_audit(
        session,
        action=action,
        actor_user_id=owner_user_id,
        event_type=EVENT_REPAIR_APPROVED if approve else EVENT_REPAIR_DECLINED,
        result_code="approved" if approve else "declined",
    )
    await session.flush()
    return action


async def execute_repair(
    session: AsyncSession,
    *,
    context: AccessContext,
    action_id: uuid.UUID,
    override: bool = False,
) -> SupportRepairAction:
    """Execute once, or durably close the approved proposal as stale.

    ``override`` applies only after the live, exact repair grant and the
    patient's approval of this fixed action have both been revalidated.  It is
    the shared conflict-engine override, not a way to broaden a support grant.
    """

    grant, now = await _lock_live_repair_grant(session, context=context)
    action = await session.scalar(
        select(SupportRepairAction)
        .where(
            SupportRepairAction.id == action_id,
            SupportRepairAction.subject_id == context.subject_id,
            SupportRepairAction.support_access_grant_id == grant.id,
            SupportRepairAction.proposed_by_user_id == context.principal.user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if action is None:
        raise RepairNotFound("no such repair under this grant")
    if action.status == SupportRepairStatus.EXECUTED.value:
        return action
    if action.status != SupportRepairStatus.APPROVED.value:
        raise RepairStateError("only an approved repair can execute")
    if now >= _as_utc(action.execute_before):
        raise RepairStateError("the repair approval has expired")

    target_date = await session.scalar(
        select(BodyMeasurement.date).where(
            BodyMeasurement.id == action.target_body_measurement_id,
            BodyMeasurement.subject_id == action.subject_id,
        )
    )
    if target_date is None:
        raise RepairNotFound("the repair target no longer exists")
    prepared = await engine.prepare_scoped_write(
        session,
        context=engine.ConflictWriteContext(
            identity=WriteIdentity(
                subject_id=action.subject_id,
                actor_user_id=context.principal.user_id,
            ),
            evaluation_date=target_date,
        ),
    )
    target = await session.scalar(
        select(BodyMeasurement)
        .where(
            BodyMeasurement.id == action.target_body_measurement_id,
            BodyMeasurement.subject_id == action.subject_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise RepairNotFound("the repair target no longer exists")
    if (
        target.updated_at != action.target_updated_at_at_proposal
        or target.body_fat_pct != action.before_body_fat_pct
        or target.lbm_kg != action.before_lbm_kg
    ):
        action.status = SupportRepairStatus.STALE.value
        _repair_audit(
            session,
            action=action,
            actor_user_id=context.principal.user_id,
            event_type=EVENT_REPAIR_STALE,
            outcome=AuditOutcome.FAILED,
            result_code="target_changed",
        )
        await session.flush()
        return action

    await engine.enforce_prepared(
        session,
        prepared=prepared,
        domain=Domain.WEIGHT,
        proposed_state={"measurement": True},
        override=override,
        entity_ref=f"body_measurement:{target.date.isoformat()}",
    )
    target.body_fat_pct = None
    target.lbm_kg = None
    await session.flush()
    await session.refresh(target, attribute_names=["updated_at"])
    action.status = SupportRepairStatus.EXECUTED.value
    action.executed_by_user_id = context.principal.user_id
    action.executed_at = now
    action.target_updated_at_after_execute = target.updated_at
    _repair_audit(
        session,
        action=action,
        actor_user_id=context.principal.user_id,
        event_type=EVENT_REPAIR_EXECUTED,
        result_code="executed",
    )
    await session.flush()
    return action


async def revert_repair(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    action_id: uuid.UUID,
    override: bool = False,
) -> SupportRepairAction:
    """Owner-safe inverse, allowed after the support grant itself has closed."""

    await acquire_identity_governance_lock(session)
    action = await session.scalar(
        select(SupportRepairAction)
        .where(SupportRepairAction.id == action_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if action is None:
        raise RepairNotFound("no such support repair")
    await _require_subject_owner(
        session, user_id=owner_user_id, subject_id=action.subject_id
    )
    if action.status != SupportRepairStatus.EXECUTED.value:
        raise RepairStateError("only an executed repair can be reverted")
    target_date = await session.scalar(
        select(BodyMeasurement.date).where(
            BodyMeasurement.id == action.target_body_measurement_id,
            BodyMeasurement.subject_id == action.subject_id,
        )
    )
    if target_date is None:
        raise RepairNotFound("the repair target no longer exists")
    prepared = await engine.prepare_scoped_write(
        session,
        context=engine.ConflictWriteContext(
            identity=WriteIdentity(
                subject_id=action.subject_id, actor_user_id=owner_user_id
            ),
            evaluation_date=target_date,
        ),
    )
    target = await session.scalar(
        select(BodyMeasurement)
        .where(
            BodyMeasurement.id == action.target_body_measurement_id,
            BodyMeasurement.subject_id == action.subject_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise RepairNotFound("the repair target no longer exists")
    if (
        target.body_fat_pct is not None
        or target.lbm_kg is not None
        or target.updated_at != action.target_updated_at_after_execute
    ):
        raise RepairStateError("the measurement changed after repair; revert refused")
    await engine.enforce_prepared(
        session,
        prepared=prepared,
        domain=Domain.WEIGHT,
        proposed_state={"measurement": True},
        override=override,
        entity_ref=f"body_measurement:{target.date.isoformat()}",
    )
    target.body_fat_pct = action.before_body_fat_pct
    target.lbm_kg = action.before_lbm_kg
    now = await _now(session)
    action.status = SupportRepairStatus.REVERTED.value
    action.reverted_by_user_id = owner_user_id
    action.reverted_at = now
    _repair_audit(
        session,
        action=action,
        actor_user_id=owner_user_id,
        event_type=EVENT_REPAIR_REVERTED,
        result_code="reverted",
    )
    await session.flush()
    return action


async def repair_actions_for_subject(
    session: AsyncSession, *, context: AccessContext, limit: int = 50
) -> tuple[RepairActionView, ...]:
    """Bounded protected history for the record owner."""

    if context.subject_owner_user_id != context.principal.user_id:
        raise NotTheSubjectOwner("only the record owner may read repair history")
    now = await _now(session)
    actions = list(
        await session.scalars(
            select(SupportRepairAction)
            .options(
                selectinload(SupportRepairAction.grant),
                selectinload(SupportRepairAction.proposed_by),
                selectinload(SupportRepairAction.target),
            )
            .where(SupportRepairAction.subject_id == context.subject_id)
            .order_by(SupportRepairAction.proposed_at.desc())
            .limit(limit)
        )
    )
    return tuple(_repair_view(action, now=now) for action in actions)
