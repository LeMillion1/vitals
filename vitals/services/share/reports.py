"""Owner-authorized shared-report creation, queries, revocation, and deletion."""

from __future__ import annotations

import secrets
from datetime import date as date_type, timedelta
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.share import SharedReport
from vitals.services.share.ownership import (
    PreparedShareOwner,
    _owner_or_zero_subject_legacy,
    _require_prepared_owner,
)
from vitals.services.share.snapshot import (
    DEFAULT_EXPIRY_DAYS,
    _owner_scope,
    _reject_selected_scope_corruption,
    _validate_owner_roots,
    build_snapshot,
    generate_password,
)
from vitals.utils.passwords import hash_password
from vitals.utils.timeutils import now_local

async def create_report(
    session: AsyncSession,
    *,
    title: str,
    domains: Sequence[str],
    period_start: date_type,
    period_end: date_type,
    expires_days: int = DEFAULT_EXPIRY_DAYS,
    note: Optional[str] = None,
    labs_flagged_only: bool = False,
    preset: Optional[str] = None,
    enabled: Optional[dict[str, bool]] = None,
    prepared_owner: PreparedShareOwner | None = None,
) -> tuple[SharedReport, str]:
    """Freeze a document and publish it. Flushes; the caller commits.

    Returns the row **and the plaintext password**, which exists only in this
    return value — after this call there is nothing but the bcrypt hash.
    """
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    snapshot = await build_snapshot(
        session,
        domains=domains,
        period_start=period_start,
        period_end=period_end,
        labs_flagged_only=labs_flagged_only,
        enabled=enabled,
        prepared_owner=owner,
    )
    password = generate_password()
    row = SharedReport(
        subject_id=(owner._identity.subject_id if owner is not None else None),
        created_by_user_id=(
            owner._identity.actor_user_id if owner is not None else None
        ),
        token=secrets.token_urlsafe(32),
        password_hash=hash_password(password),
        title=title.strip()[:120],
        preset=preset,
        domains=snapshot["domains"],
        period_start=date_type.fromisoformat(snapshot["period"]["start"]),
        period_end=date_type.fromisoformat(snapshot["period"]["end"]),
        labs_flagged_only=bool(labs_flagged_only),
        note=(note or "").strip() or None,
        snapshot=snapshot,
        expires_at=now_local() + timedelta(days=max(int(expires_days), 1)),
    )
    session.add(row)
    await session.flush()
    return row, password


async def list_reports(
    session: AsyncSession,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> Sequence[SharedReport]:
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    if owner is None:
        stmt = select(SharedReport).where(
            SharedReport.subject_id.is_(None),
            SharedReport.created_by_user_id.is_(None),
            SharedReport.revoked_by_user_id.is_(None),
        )
    else:
        bridge_state = await _reject_selected_scope_corruption(session, owner)
        stmt = select(SharedReport).where(_owner_scope(owner))
    result = await session.execute(
        stmt.order_by(SharedReport.created_at.desc(), SharedReport.id.desc())
        .execution_options(populate_existing=True)
    )
    rows = result.scalars().all()
    if owner is not None:
        for row in rows:
            _validate_owner_roots(
                owner,
                report_id=row.id,
                subject_id=row.subject_id,
                created_by_user_id=row.created_by_user_id,
                revoked_by_user_id=row.revoked_by_user_id,
                revoked_at=row.revoked_at,
                bridge_state=bridge_state,
            )
    return rows


async def get_report(
    session: AsyncSession,
    report_id: int,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> Optional[SharedReport]:
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    if owner is None:
        stmt = select(SharedReport).where(
            SharedReport.id == report_id,
            SharedReport.subject_id.is_(None),
            SharedReport.created_by_user_id.is_(None),
            SharedReport.revoked_by_user_id.is_(None),
        )
    else:
        bridge_state = await _reject_selected_scope_corruption(session, owner)
        stmt = select(SharedReport).where(
            SharedReport.id == report_id,
            _owner_scope(owner),
        )
    row = await session.scalar(stmt.execution_options(populate_existing=True))
    if row is None:
        return None
    if owner is None:
        return row
    _validate_owner_roots(
        owner,
        report_id=row.id,
        subject_id=row.subject_id,
        created_by_user_id=row.created_by_user_id,
        revoked_by_user_id=row.revoked_by_user_id,
        revoked_at=row.revoked_at,
        bridge_state=bridge_state,
    )
    return row




async def _lock_owner_report(
    session: AsyncSession,
    report_id: int,
    *,
    prepared_owner: PreparedShareOwner,
) -> SharedReport | None:
    owner = _require_prepared_owner(session, prepared_owner)
    bridge_state = await _reject_selected_scope_corruption(session, owner)
    roots = (
        await session.execute(
            select(
                SharedReport.subject_id,
                SharedReport.created_by_user_id,
                SharedReport.revoked_by_user_id,
                SharedReport.revoked_at,
            ).where(
                SharedReport.id == report_id,
                _owner_scope(owner),
            )
        )
    ).one_or_none()
    if roots is None:
        return None
    subject_id, created_by_user_id, revoked_by_user_id, revoked_at = roots
    _validate_owner_roots(
        owner,
        report_id=report_id,
        subject_id=subject_id,
        created_by_user_id=created_by_user_id,
        revoked_by_user_id=revoked_by_user_id,
        revoked_at=revoked_at,
        bridge_state=bridge_state,
    )
    row = await session.scalar(
        select(SharedReport)
        .where(
            SharedReport.id == report_id,
            _owner_scope(owner),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    _validate_owner_roots(
        owner,
        report_id=row.id,
        subject_id=row.subject_id,
        created_by_user_id=row.created_by_user_id,
        revoked_by_user_id=row.revoked_by_user_id,
        revoked_at=row.revoked_at,
        bridge_state=bridge_state,
    )
    return row


async def revoke(
    session: AsyncSession,
    report_id: int,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> bool:
    """Kill the link now. The snapshot goes with it — a revoked report is one the
    owner decided should stop existing, not one to keep a copy of."""
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    if owner is None:
        row = await session.scalar(
            select(SharedReport)
            .where(
                SharedReport.id == report_id,
                SharedReport.subject_id.is_(None),
                SharedReport.created_by_user_id.is_(None),
                SharedReport.revoked_by_user_id.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    else:
        row = await _lock_owner_report(
            session,
            report_id,
            prepared_owner=owner,
        )
    if row is None or row.revoked_at is not None:
        return False
    if owner is not None:
        if row.subject_id is None:
            row.subject_id = owner._identity.subject_id
        # Preserve a known creator and preserve NULL when legacy history did not
        # record one; only this authenticated lifecycle action gets a new actor.
        row.revoked_by_user_id = owner._identity.actor_user_id
    row.revoked_at = now_local()
    row.snapshot = None
    await session.flush()
    return True


async def delete_report(
    session: AsyncSession,
    report_id: int,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> bool:
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    if owner is None:
        row = await session.scalar(
            select(SharedReport)
            .where(
                SharedReport.id == report_id,
                SharedReport.subject_id.is_(None),
                SharedReport.created_by_user_id.is_(None),
                SharedReport.revoked_by_user_id.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    else:
        row = await _lock_owner_report(
            session,
            report_id,
            prepared_owner=owner,
        )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
