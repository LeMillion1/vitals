"""Durable checkpoints for the bounded subject-ownership backfill.

The checkpoint contains only control-plane progress, counts, and deterministic
digests.  It must never contain row payloads, health values, file paths, or
other PHI copied from the tables being scanned.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from vitals.models.base import Base

_EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)


def _lowercase_sha256_check(column_name: str) -> str:
    """Return a SQLite/PostgreSQL-compatible lowercase SHA-256 check."""

    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 "
        f"AND lower({column_name}) = {column_name} "
        f"AND length({remainder}) = 0"
    )


class OwnershipBackfillCheckpoint(Base):
    """One resumable, subject-bound checkpoint per reviewed backfill phase."""

    __tablename__ = "ownership_backfill_checkpoints"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'restore_blocked')",
            name="ck_ownership_backfill_status",
        ),
        CheckConstraint(
            "scan_high_watermark_id >= 0 AND last_scanned_id >= 0",
            name="ck_ownership_backfill_watermarks_nonnegative",
        ),
        CheckConstraint(
            "snapshot_rows >= 0",
            name="ck_ownership_backfill_snapshot_rows_nonnegative",
        ),
        CheckConstraint(
            "last_scanned_id <= scan_high_watermark_id",
            name="ck_ownership_backfill_watermark_order",
        ),
        CheckConstraint(
            "scanned_rows >= 0 AND updated_rows >= 0 AND unchanged_rows >= 0",
            name="ck_ownership_backfill_counts_nonnegative",
        ),
        CheckConstraint(
            "scanned_rows <= snapshot_rows",
            name="ck_ownership_backfill_scanned_within_snapshot",
        ),
        CheckConstraint(
            "scanned_rows = updated_rows + unchanged_rows",
            name="ck_ownership_backfill_count_balance",
        ),
        CheckConstraint(
            _lowercase_sha256_check("data_checksum_before"),
            name="ck_ownership_backfill_data_before_sha256",
        ),
        CheckConstraint(
            _lowercase_sha256_check("data_checksum_after"),
            name="ck_ownership_backfill_data_after_sha256",
        ),
        CheckConstraint(
            _lowercase_sha256_check("ownership_checksum_after"),
            name="ck_ownership_backfill_ownership_after_sha256",
        ),
        CheckConstraint(
            "(status IN ('running', 'restore_blocked') "
            "AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND last_scanned_id = scan_high_watermark_id "
            "AND scanned_rows = snapshot_rows)",
            name="ck_ownership_backfill_completed_state",
        ),
        CheckConstraint(
            "status <> 'restore_blocked' OR "
            "(last_scanned_id = 0 AND scanned_rows = 0 "
            "AND updated_rows = 0 AND unchanged_rows = 0 "
            f"AND data_checksum_before = '{_EMPTY_SHA256}' "
            f"AND data_checksum_after = '{_EMPTY_SHA256}' "
            f"AND ownership_checksum_after = '{_EMPTY_SHA256}')",
            name="ck_ownership_backfill_restore_blocked_state",
        ),
        CheckConstraint(
            "updated_at >= started_at AND "
            "(completed_at IS NULL OR completed_at >= started_at)",
            name="ck_ownership_backfill_timestamp_order",
        ),
        Index(
            "ix_ownership_backfill_checkpoints_status_updated",
            "status",
            "updated_at",
        ),
    )

    phase_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="running"
    )
    scan_high_watermark_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    snapshot_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_scanned_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    scanned_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    updated_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    unchanged_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    data_checksum_before: Mapped[str] = mapped_column(String(64), nullable=False)
    data_checksum_after: Mapped[str] = mapped_column(String(64), nullable=False)
    ownership_checksum_after: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["OwnershipBackfillCheckpoint"]
