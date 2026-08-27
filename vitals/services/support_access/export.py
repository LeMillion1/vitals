"""Grant-rechecked record opening and one-shot subject export."""

from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.access import AccessContext, AccessRequest, PolicyAction, PolicyResourceType, is_allowed
from vitals.enums import (
    AuditOutcome,
    SupportAccessMode,
    SupportAccessStatus,
    SupportScopeResourceType,
)
from vitals.models.identity import AuditEvent, SupportAccessGrant, SupportAccessScope
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.portability import v1_export
from vitals.services.support_access.contracts import (
    EVENT_RECORD_EXPORTED,
    EVENT_RECORD_OPENED,
    EXPORT_OPERATION_KEY,
    NotASupportSession,
    _as_utc,
    _now,
    _require_platform_admin,
)

async def record_record_opened(
    session: AsyncSession,
    *,
    context: AccessContext,
    domain_keys: Iterable[str],
    artifact_keys: Iterable[str] = (),
) -> AuditEvent:
    """Durably describe one support-granted record response, without PHI.

    The exact grant row is locked and rechecked after the record was assembled.
    This turns a revoke/read race into an order: either revocation wins and the
    response is refused, or this event commits before the response is returned
    and revocation follows it. A caller must commit this event before handing
    the rendered medical response to the browser.
    """

    snapshot = context.support_grant
    if snapshot is None:
        raise NotASupportSession("record access is not based on a support grant")
    if snapshot.subject_id != context.subject_id:
        raise NotASupportSession("support grant and selected record do not match")
    if snapshot.granted_to_user_id != context.principal.user_id:
        raise NotASupportSession("support grant and signed-in account do not match")

    # Role assignment/removal takes this same transaction lock. Holding it
    # through the caller's disclosure commit makes the live-role check an
    # ordered fact rather than a snapshot that can race role revocation.
    await acquire_identity_governance_lock(session)
    await _require_platform_admin(session, user_id=context.principal.user_id)
    grant = (
        await session.execute(
            select(
                SupportAccessGrant.id,
                SupportAccessGrant.status,
                SupportAccessGrant.revoked_at,
                SupportAccessGrant.expires_at,
                SupportAccessGrant.mode,
            )
            .where(
                SupportAccessGrant.id == snapshot.grant_id,
                SupportAccessGrant.subject_id == context.subject_id,
                SupportAccessGrant.granted_to_user_id == context.principal.user_id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if grant is None:
        raise NotASupportSession("the support grant no longer exists")

    now = await _now(session)
    if (
        grant.status != SupportAccessStatus.ACTIVE.value
        or grant.revoked_at is not None
        or now >= _as_utc(grant.expires_at)
    ):
        raise NotASupportSession("the support grant is no longer active")

    live_scopes = set(
        (
            await session.execute(
                select(
                    SupportAccessScope.resource_type,
                    SupportAccessScope.resource_key,
                    SupportAccessScope.action,
                ).where(SupportAccessScope.grant_id == grant.id)
            )
        ).all()
    )

    domains = tuple(
        sorted({str(key).strip() for key in domain_keys if str(key).strip()})
    )
    artifacts = tuple(
        sorted({str(key).strip() for key in artifact_keys if str(key).strip()})
    )
    requested = tuple((PolicyResourceType.DOMAIN, key) for key in domains) + tuple(
        (PolicyResourceType.ARTIFACT, key) for key in artifacts
    )
    for resource_type, resource_key in requested:
        request = AccessRequest(
            subject_id=context.subject_id,
            resource_type=resource_type,
            resource_key=resource_key,
            action=PolicyAction.READ,
        )
        if not is_allowed(context, request):
            raise NotASupportSession(
                "the rendered record exceeds the approved support scope"
            )
        if (
            resource_type.value,
            resource_key,
            SupportAccessMode.READ.value,
        ) not in live_scopes:
            raise NotASupportSession(
                "the live support grant does not contain the rendered scope"
            )

    event = AuditEvent(
        actor_user_id=context.principal.user_id,
        subject_id=context.subject_id,
        support_access_grant_id=grant.id,
        event_type=EVENT_RECORD_OPENED,
        outcome=AuditOutcome.SUCCESS.value,
        resource_type="health_record",
        resource_id=str(context.subject_id),
        metadata_json={
            "correlation_id": str(uuid.uuid4()),
            "source_surface": "web.care.record",
            "reason_code": "approved_support_read",
            "resource_type": "health_record",
            "resource_id": str(context.subject_id),
            "grant_mode": grant.mode,
        },
    )
    session.add(event)
    await session.flush()
    return event


async def consume_subject_export(
    session: AsyncSession,
    *,
    context: AccessContext,
) -> dict[str, object]:
    """Build and consume one exact exceptional export grant. Never commits.

    The grant and identity-governance locks remain held while the portability
    snapshot is assembled. The caller must serialize the returned value and
    commit this transaction before returning any bytes. A generation or
    serialization failure can then roll back without spending the approval;
    once the commit lands, the grant is terminal even if the connection drops.
    """

    snapshot = context.support_grant
    if snapshot is None:
        raise NotASupportSession("export is not based on a support grant")
    if (
        snapshot.subject_id != context.subject_id
        or snapshot.granted_to_user_id != context.principal.user_id
        or snapshot.mode is not SupportAccessMode.EXPORT
    ):
        raise NotASupportSession("support export grant does not match this request")

    exact_request = AccessRequest(
        subject_id=context.subject_id,
        resource_type=PolicyResourceType.OPERATION,
        resource_key=EXPORT_OPERATION_KEY,
        action=PolicyAction.EXPORT,
    )
    if not is_allowed(context, exact_request):
        raise NotASupportSession("support export is outside the approved scope")

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
        raise NotASupportSession("the support export grant no longer exists")

    now = await _now(session)
    if (
        grant.mode != SupportAccessMode.EXPORT.value
        or grant.status != SupportAccessStatus.ACTIVE.value
        or grant.revoked_at is not None
        or grant.consumed_at is not None
        or now >= _as_utc(grant.expires_at)
    ):
        raise NotASupportSession("the support export grant is no longer usable")

    live_scopes = {
        (scope.resource_type, scope.resource_key, scope.action)
        for scope in grant.scopes
    }
    required_scope = {
        (
            SupportScopeResourceType.OPERATION.value,
            EXPORT_OPERATION_KEY,
            SupportAccessMode.EXPORT.value,
        )
    }
    if live_scopes != required_scope:
        raise NotASupportSession("the support export grant is not exact")

    payload = await v1_export.export_subject(
        session, subject_id=context.subject_id
    )
    grant.status = SupportAccessStatus.CONSUMED.value
    grant.consumed_at = now
    event = AuditEvent(
        actor_user_id=context.principal.user_id,
        subject_id=context.subject_id,
        support_access_grant_id=grant.id,
        event_type=EVENT_RECORD_EXPORTED,
        outcome=AuditOutcome.SUCCESS.value,
        resource_type="subject_export",
        resource_id=str(context.subject_id),
        metadata_json={
            "correlation_id": str(uuid.uuid4()),
            "source_surface": "web.settings.support_export",
            "reason_code": "approved_support_export",
            "resource_type": "subject_export",
            "resource_id": str(context.subject_id),
            "grant_mode": SupportAccessMode.EXPORT.value,
        },
    )
    session.add(event)
    await session.flush()
    return payload
