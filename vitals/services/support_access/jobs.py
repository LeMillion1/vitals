"""Expiry maintenance for stale support requests and grants."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import SupportAccessRequestStatus, SupportAccessStatus
from vitals.models.identity import SupportAccessGrant, SupportAccessRequest
from vitals.services.support_access.contracts import (
    EVENT_EXPIRED,
    _LIVE_REQUEST,
    _audit,
    _now,
)

async def expire_stale(session: AsyncSession) -> tuple[int, int]:
    """Mark lapsed asks and lapsed grants for what they are. Never commits.

    Returns ``(requests, grants)`` closed. Expiry is already enforced at every
    read — :func:`load_support_grant` compares the clock and the policy compares
    it again — so this changes no authorization. What it changes is what the
    screens say: a grant that ran out three days ago still reading "active" in a
    patient's access history is the list telling them something untrue about who
    can see their record.
    """

    now = await _now(session)

    stale_requests = (
        await session.execute(
            select(SupportAccessRequest)
            .where(
                SupportAccessRequest.status == _LIVE_REQUEST,
                SupportAccessRequest.expires_at <= now,
            )
            .with_for_update()
        )
    ).scalars().all()
    for request in stale_requests:
        request.status = SupportAccessRequestStatus.EXPIRED.value
        request.decided_at = now
        # Nobody decided it; the clock did. The column is not nullable for a
        # decided row, so the requester stands as the named party — the history
        # reads "the ask this admin made lapsed", which is what happened.
        request.decided_by_user_id = request.requested_by_user_id
        _audit(
            session,
            event_type=EVENT_EXPIRED,
            actor_user_id=request.requested_by_user_id,
            subject_id=request.subject_id,
            grant_id=None,
            resource_id=request.id,
            reason_code="support_request_lapsed",
        )

    stale_grants = (
        await session.execute(
            select(SupportAccessGrant)
            .where(
                SupportAccessGrant.status == SupportAccessStatus.ACTIVE.value,
                SupportAccessGrant.expires_at <= now,
            )
            .with_for_update()
        )
    ).scalars().all()
    for grant in stale_grants:
        # Not ``revoked``: nobody took it away, it ran out. The revocation
        # columns stay null, which the schema's revocation-state constraint
        # requires for any status that is not ``revoked``.
        grant.status = SupportAccessStatus.EXPIRED.value
        _audit(
            session,
            event_type=EVENT_EXPIRED,
            actor_user_id=grant.granted_to_user_id,
            subject_id=grant.subject_id,
            grant_id=grant.id,
            resource_id=grant.id,
            reason_code="support_grant_lapsed",
        )

    await session.flush()
    return len(stale_requests), len(stale_grants)
