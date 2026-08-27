"""Subject-scoped digest artifact queries."""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import DigestKind
from vitals.models.milestones import WeeklyDigest
from vitals.services.digest.ownership import (
    DigestOwnershipError,
    PreparedDigestOwner,
    _DIGEST_KINDS,
    _owner_or_zero_subject_legacy,
)

async def latest_digest(
    session: AsyncSession,
    *,
    kind: str = DigestKind.WEEKLY.value,
    prepared_owner: PreparedDigestOwner | None = None,
) -> Optional[WeeklyDigest]:
    """The most recent narrative of one kind. Defaults to the weekly digest, so a
    daily brief can never show up where a weekly one is expected."""
    if kind not in _DIGEST_KINDS:
        raise DigestOwnershipError(f"unknown digest kind {kind!r}")
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    scope = (
        and_(
            WeeklyDigest.subject_id.is_(None),
            WeeklyDigest.actor_user_id.is_(None),
            WeeklyDigest.integration_connection_id.is_(None),
            WeeklyDigest.ai_invocation_id.is_(None),
        )
        if owner is None
        else or_(
            WeeklyDigest.subject_id == owner._subject_id,
            and_(
                WeeklyDigest.subject_id.is_(None),
                WeeklyDigest.actor_user_id.is_(None),
                WeeklyDigest.integration_connection_id.is_(None),
                WeeklyDigest.ai_invocation_id.is_(None),
            ),
        )
    )
    result = await session.execute(
        select(WeeklyDigest)
        .where(scope, WeeklyDigest.kind == kind)
        .order_by(WeeklyDigest.date.desc(), WeeklyDigest.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def list_digests(
    session: AsyncSession,
    *,
    limit: int = 20,
    kind: str = DigestKind.WEEKLY.value,
    prepared_owner: PreparedDigestOwner | None = None,
) -> Sequence[WeeklyDigest]:
    if kind not in _DIGEST_KINDS:
        raise DigestOwnershipError(f"unknown digest kind {kind!r}")
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    scope = (
        and_(
            WeeklyDigest.subject_id.is_(None),
            WeeklyDigest.actor_user_id.is_(None),
            WeeklyDigest.integration_connection_id.is_(None),
            WeeklyDigest.ai_invocation_id.is_(None),
        )
        if owner is None
        else or_(
            WeeklyDigest.subject_id == owner._subject_id,
            and_(
                WeeklyDigest.subject_id.is_(None),
                WeeklyDigest.actor_user_id.is_(None),
                WeeklyDigest.integration_connection_id.is_(None),
                WeeklyDigest.ai_invocation_id.is_(None),
            ),
        )
    )
    result = await session.execute(
        select(WeeklyDigest)
        .where(scope, WeeklyDigest.kind == kind)
        .order_by(WeeklyDigest.date.desc(), WeeklyDigest.id.desc())
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    return result.scalars().all()


# ── Scheduler job ─────────────────────────────────────────────────────────────
