"""Actorless expired-snapshot minimization and its scheduled entry point."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.identity import HealthSubject, User
from vitals.models.share import SharedReport
from vitals.persistence.rls import enter_platform_scope
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.share.ownership import ShareOwnershipError
from vitals.services.share.snapshot import _historical_bridge_state, _validate_report_roots
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

async def purge_expired(session: AsyncSession, *, now: Optional[datetime] = None) -> int:
    """Empty the snapshot of every dead link; keep the metadata.

    An expired report is unreachable already — this is about not keeping a full
    copy of the medical record for every appointment ever attended. The row stays
    so /share can still say what was shared and when.
    """
    await acquire_identity_governance_lock(session)
    moment = now or now_local()
    root_rows = list(
        await session.execute(
            select(
                SharedReport.id,
                SharedReport.subject_id,
                SharedReport.created_by_user_id,
                SharedReport.revoked_by_user_id,
                SharedReport.revoked_at,
            )
            .where(SharedReport.expires_at <= moment)
            .where(SharedReport.snapshot.is_not(None))
            .order_by(SharedReport.id)
        )
    )
    if not root_rows:
        return 0

    legacy_owner: tuple[uuid.UUID, uuid.UUID] | None = None
    subject_ids = {
        row.subject_id for row in root_rows if row.subject_id is not None
    }
    if any(row.subject_id is None for row in root_rows):
        null_rows = [row for row in root_rows if row.subject_id is None]
        if any(
            row.created_by_user_id is not None or row.revoked_by_user_id is not None
            for row in null_rows
        ):
            raise ShareOwnershipError(
                "expired shared report has partial legacy ownership roots"
            )
        # A fully-null report has no stored S to lock.  Under governance, map it
        # only when there is exactly one subject, then validate that subject and
        # its owner through the same ordered locks below.  Owner suspension must
        # not retain expired PHI, and this actorless purge never adopts the roots.
        with session.no_autoflush:
            legacy_subjects = list(
                await session.execute(
                    select(HealthSubject.id, HealthSubject.owner_user_id)
                    .order_by(HealthSubject.id)
                    .limit(2)
                )
            )
        if len(legacy_subjects) != 1:
            raise ShareOwnershipError(
                "expired legacy reports require exactly one health subject"
            )
        legacy_subject_id, legacy_owner_user_id = legacy_subjects[0]
        legacy_owner = (legacy_subject_id, legacy_owner_user_id)
        subject_ids.add(legacy_subject_id)

    subjects = {
        subject.id: subject
        for subject in await session.scalars(
            select(HealthSubject)
            .where(HealthSubject.id.in_(tuple(subject_ids)))
            .order_by(HealthSubject.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    if set(subjects) != subject_ids:
        raise ShareOwnershipError("expired shared report subject is missing")
    if legacy_owner is not None:
        legacy_subject_id, legacy_owner_user_id = legacy_owner
        if subjects[legacy_subject_id].owner_user_id != legacy_owner_user_id:
            raise ShareOwnershipError(
                "expired legacy report owner changed during purge"
            )
    owner_ids = {subject.owner_user_id for subject in subjects.values()}
    owners = {
        owner.id: owner
        for owner in await session.scalars(
            select(User)
            .where(User.id.in_(tuple(owner_ids)))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    # Suspension closes public access, but it must not retain an already-expired
    # PHI snapshot.  Purge is actorless data minimization: the owner root must
    # still exist and match the subject/actor graph, but it need not be active.
    if any(owners.get(owner_id) is None for owner_id in owner_ids):
        raise ShareOwnershipError("expired shared report owner is missing")

    bridge_states = {
        subject_id: await _historical_bridge_state(
            session,
            subject_id=subject_id,
        )
        for subject_id in sorted(subject_ids, key=str)
    }
    expected_roots = {}
    for root in root_rows:
        if root.subject_id is None:
            assert legacy_owner is not None
            expected_subject_id, owner_user_id = legacy_owner
        else:
            expected_subject_id = root.subject_id
            owner_user_id = subjects[root.subject_id].owner_user_id
        _validate_report_roots(
            report_id=root.id,
            expected_subject_id=expected_subject_id,
            owner_user_id=owner_user_id,
            subject_id=root.subject_id,
            created_by_user_id=root.created_by_user_id,
            revoked_by_user_id=root.revoked_by_user_id,
            revoked_at=root.revoked_at,
            bridge_state=bridge_states[expected_subject_id],
        )
        expected_roots[root.id] = (
            root.subject_id,
            root.created_by_user_id,
            root.revoked_by_user_id,
            root.revoked_at,
        )

    rows = list(
        await session.scalars(
            select(SharedReport)
            .where(SharedReport.id.in_(tuple(expected_roots)))
            .order_by(SharedReport.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if {row.id for row in rows} != set(expected_roots):
        raise ShareOwnershipError("expired shared report changed during purge")
    purged = 0
    for row in rows:
        if (
            row.subject_id,
            row.created_by_user_id,
            row.revoked_by_user_id,
            row.revoked_at,
        ) != expected_roots[row.id]:
            raise ShareOwnershipError(
                "expired shared report ownership changed during purge"
            )
        if row.expires_at > moment or row.snapshot is None:
            continue
        row.snapshot = None
        purged += 1
    await session.flush()
    return purged


async def purge_job(session_factory, redis=None) -> None:
    """Daily sweep — see :func:`purge_expired`."""
    async with session_factory() as session:
        # Housekeeping across every subject's expired snapshots: there is no
        # person this job acts as.
        await enter_platform_scope(session)
        purged = await purge_expired(session)
        await session.commit()
    if purged:
        logger.info("shared reports: cleared %s expired snapshot(s)", purged)
