"""Read-only compatibility bridges for bounded ownership transitions.

Request-time services may read transition evidence, but must not depend on the
operational programs that create it.  This module is the lower-level seam used
by both sides; it never advances a checkpoint or commits a transaction.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint


_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROGRESS_PHOTO_CHECKPOINT_PHASE = (
    "stage3.file_backed.progress_photos.v1.progress_photos"
)
_SHARED_REPORT_CHECKPOINT_PHASE = (
    "stage3.retained_artifact.shared_reports.v1.shared_reports"
)


class ProgressPhotoOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed progress-photo transition failures."""


class ProgressPhotoOwnershipBackfillValidationError(
    ProgressPhotoOwnershipBackfillError, ValueError
):
    """A progress-photo bridge argument or persisted scalar is invalid."""


class ProgressPhotoOwnershipBackfillStateError(
    ProgressPhotoOwnershipBackfillError
):
    """Progress-photo checkpoint evidence is inconsistent."""


class SharedReportOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed shared-report transition failures."""


class SharedReportOwnershipBackfillValidationError(
    SharedReportOwnershipBackfillError, ValueError
):
    """A shared-report bridge argument or persisted scalar is invalid."""


class SharedReportOwnershipBackfillStateError(SharedReportOwnershipBackfillError):
    """Shared-report checkpoint evidence is inconsistent."""


@dataclass(frozen=True, slots=True)
class SharedReportHistoricalBridgeState:
    processed_high_watermark_id: int
    snapshot_high_watermark_id: int
    completed: bool


@dataclass(frozen=True, slots=True)
class _CheckpointProjection:
    phase_key: str
    subject_id: uuid.UUID
    status: str
    scan_high_watermark_id: int
    snapshot_rows: int
    last_scanned_id: int
    scanned_rows: int
    updated_rows: int
    unchanged_rows: int
    data_checksum_before: str
    data_checksum_after: str
    ownership_checksum_after: str
    started_at: Any
    updated_at: Any
    completed_at: Any


def _valid_counter(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _POSTGRES_INTEGER_MAX
    )


async def _load_checkpoint(
    session: AsyncSession, *, phase: str
) -> _CheckpointProjection | None:
    row = (
        await session.execute(
            select(
                OwnershipBackfillCheckpoint.phase_key,
                OwnershipBackfillCheckpoint.subject_id,
                OwnershipBackfillCheckpoint.status,
                OwnershipBackfillCheckpoint.scan_high_watermark_id,
                OwnershipBackfillCheckpoint.snapshot_rows,
                OwnershipBackfillCheckpoint.last_scanned_id,
                OwnershipBackfillCheckpoint.scanned_rows,
                OwnershipBackfillCheckpoint.updated_rows,
                OwnershipBackfillCheckpoint.unchanged_rows,
                OwnershipBackfillCheckpoint.data_checksum_before,
                OwnershipBackfillCheckpoint.data_checksum_after,
                OwnershipBackfillCheckpoint.ownership_checksum_after,
                OwnershipBackfillCheckpoint.started_at,
                OwnershipBackfillCheckpoint.updated_at,
                OwnershipBackfillCheckpoint.completed_at,
            ).where(OwnershipBackfillCheckpoint.phase_key == phase)
        )
    ).one_or_none()
    return _CheckpointProjection(*row) if row is not None else None


def _validate_checkpoint(
    checkpoint: _CheckpointProjection,
    *,
    phase: str,
    subject_id: uuid.UUID,
    error: type[RuntimeError],
) -> str:
    if checkpoint.phase_key != phase or checkpoint.subject_id != subject_id:
        raise error("an ownership checkpoint has the wrong phase or subject")
    if checkpoint.status not in {"running", "completed", "restore_blocked"}:
        raise error("an ownership checkpoint has an unknown status")
    counters = (
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    )
    if not all(_valid_counter(value) for value in counters):
        raise error("an ownership checkpoint has invalid counters")
    if (
        checkpoint.snapshot_rows > checkpoint.scan_high_watermark_id
        or checkpoint.last_scanned_id > checkpoint.scan_high_watermark_id
        or checkpoint.scanned_rows > checkpoint.snapshot_rows
        or checkpoint.scanned_rows
        != checkpoint.updated_rows + checkpoint.unchanged_rows
    ):
        raise error("an ownership checkpoint has inconsistent counters")
    digests = (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    )
    if any(
        not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
        for value in digests
    ):
        raise error("an ownership checkpoint has an invalid checksum")
    if checkpoint.data_checksum_before != checkpoint.data_checksum_after:
        raise error("an ownership checkpoint has divergent data evidence")
    if (
        checkpoint.started_at is None
        or checkpoint.updated_at is None
        or checkpoint.updated_at < checkpoint.started_at
    ):
        raise error("an ownership checkpoint has invalid timestamps")
    if checkpoint.status == "completed":
        if (
            checkpoint.completed_at is None
            or checkpoint.completed_at < checkpoint.started_at
            or checkpoint.last_scanned_id != checkpoint.scan_high_watermark_id
            or checkpoint.scanned_rows != checkpoint.snapshot_rows
        ):
            raise error("a completed ownership checkpoint is malformed")
    elif checkpoint.completed_at is not None:
        raise error("a non-completed ownership checkpoint has completed_at")
    if checkpoint.status == "running" and (
        checkpoint.scan_high_watermark_id == 0 or checkpoint.snapshot_rows == 0
    ):
        raise error("a running ownership checkpoint must be nonempty")
    if checkpoint.status == "restore_blocked" and (
        checkpoint.scan_high_watermark_id == 0
        or checkpoint.snapshot_rows == 0
        or checkpoint.last_scanned_id != 0
        or checkpoint.scanned_rows != 0
        or checkpoint.updated_rows != 0
        or checkpoint.unchanged_rows != 0
        or any(value != _EMPTY_SHA256 for value in digests)
    ):
        raise error("a restore-blocked ownership checkpoint is malformed")
    return checkpoint.status


async def progress_photo_historical_processed_bound(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> int | None:
    """Return the migrated historical prefix consumed by compatibility reads."""

    if not isinstance(subject_id, uuid.UUID):
        raise ProgressPhotoOwnershipBackfillValidationError(
            "subject_id must be a UUID"
        )
    checkpoint = await _load_checkpoint(
        session, phase=_PROGRESS_PHOTO_CHECKPOINT_PHASE
    )
    if checkpoint is None:
        return None
    status = _validate_checkpoint(
        checkpoint,
        phase=_PROGRESS_PHOTO_CHECKPOINT_PHASE,
        subject_id=subject_id,
        error=ProgressPhotoOwnershipBackfillStateError,
    )
    if status == "running":
        return checkpoint.last_scanned_id
    if status == "completed":
        return checkpoint.scan_high_watermark_id
    return None


async def shared_report_historical_bridge_state(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> SharedReportHistoricalBridgeState:
    """Return the bounded legacy bridge authorized by the exact K checkpoint."""

    if not isinstance(subject_id, uuid.UUID):
        raise SharedReportOwnershipBackfillValidationError(
            "subject_id must be a UUID"
        )
    checkpoint = await _load_checkpoint(
        session, phase=_SHARED_REPORT_CHECKPOINT_PHASE
    )
    if checkpoint is None:
        return SharedReportHistoricalBridgeState(0, 0, False)
    status = _validate_checkpoint(
        checkpoint,
        phase=_SHARED_REPORT_CHECKPOINT_PHASE,
        subject_id=subject_id,
        error=SharedReportOwnershipBackfillStateError,
    )
    if status == "restore_blocked":
        raise SharedReportOwnershipBackfillStateError(
            "Stage-3K checkpoints cannot be restore-blocked"
        )
    snapshot_high = checkpoint.scan_high_watermark_id
    if status == "completed":
        return SharedReportHistoricalBridgeState(snapshot_high, snapshot_high, True)
    return SharedReportHistoricalBridgeState(
        checkpoint.last_scanned_id,
        snapshot_high,
        False,
    )


__all__ = [
    "ProgressPhotoOwnershipBackfillError",
    "ProgressPhotoOwnershipBackfillStateError",
    "ProgressPhotoOwnershipBackfillValidationError",
    "SharedReportHistoricalBridgeState",
    "SharedReportOwnershipBackfillError",
    "SharedReportOwnershipBackfillStateError",
    "SharedReportOwnershipBackfillValidationError",
    "progress_photo_historical_processed_bound",
    "shared_report_historical_bridge_state",
]
