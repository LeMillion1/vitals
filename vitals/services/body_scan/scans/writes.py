"""Mutations of existing body-scan records."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.weight.contracts import PreparedWeightWrite

from .contracts import require_scoped_prepared_write as _require_scoped_prepared_write
from . import queries

async def _lock_scan_for_update(
    session: AsyncSession,
    scan_id: int,
    *,
    context: engine.ConflictWriteContext,
) -> BodyScan | None:
    candidate = await queries.get_scan(
        session,
        scan_id,
        subject_id=context.identity.subject_id,
    )
    if candidate is None:
        return None
    await queries._validate_persisted_scan(
        session,
        candidate,
        subject_id=context.identity.subject_id,
        for_update=True,
    )
    # Provenance roots were validated before the fact lock. Lock the parent and
    # children in their stable order, then reject a concurrent provenance swap.
    row = await session.scalar(
        select(BodyScan)
        .where(
            BodyScan.id == scan_id,
            queries._subject_scope(
                BodyScan,
                context.identity.subject_id,
            ),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    list(
        await session.scalars(
            select(BodyScanMetric)
            .where(BodyScanMetric.scan_id == row.id)
            .order_by(BodyScanMetric.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    refreshed = await queries.get_scan(
        session,
        row.id,
        subject_id=context.identity.subject_id,
    )
    if refreshed is None:
        return None
    if (
        refreshed.raw_payload_id != candidate.raw_payload_id
        or refreshed.source != candidate.source
        or refreshed.file_asset_id != candidate.file_asset_id
    ):
        raise engine.ConflictRawOwnershipError(
            "body-scan provenance changed while acquiring write locks"
        )
    return refreshed


async def update_scan_note(
    session: AsyncSession,
    scan_id: int,
    *,
    note: str | None,
    identity: WriteIdentity,
    prepared_weight_write: PreparedWeightWrite,
) -> BodyScan | None:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    assert context is not None
    row = await _lock_scan_for_update(
        session,
        scan_id,
        context=context,
    )
    if row is None:
        return None
    row.note = note
    await session.flush()
    return row


async def delete_scan(
    session: AsyncSession,
    scan_id: int,
    *,
    subject_id: uuid.UUID,
    identity: WriteIdentity,
    prepared_weight_write: PreparedWeightWrite,
) -> bool:
    """Delete a scan (cascades to its metrics). Returns False if not found.

    The bridged weight row is left as-is (it's an independent weight log); the
    owner can remove it from the weight tab if desired."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    if context is not None:
        if subject_id is not None and subject_id != identity.subject_id:
            raise engine.ConflictPreparedWriteError(
                "subject_id does not match prepared body-scan identity"
            )
        scan = await _lock_scan_for_update(
            session,
            scan_id,
            context=context,
        )
    else:
        scan = await queries.get_scan(
            session,
            scan_id,
            subject_id=subject_id,
        )
    if scan is None:
        return False
    await session.delete(scan)
    await session.flush()
    return True
