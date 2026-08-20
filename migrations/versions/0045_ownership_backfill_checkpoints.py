"""Add durable checkpoints for the bounded subject-ownership backfill.

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-20

Alembic owns only the checkpoint schema.  A separately reviewed application
operation owns every data scan and update recorded here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE_NAME = "ownership_backfill_checkpoints"
_STATUS_UPDATED_INDEX = "ix_ownership_backfill_checkpoints_status_updated"
_DOWNGRADE_REFUSAL = (
    "0045 downgrade refused: ownership backfill checkpoints contain durable state"
)
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


def _lock_downgrade_cutover() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Close the guard/drop race.  The table has no children, so this single
    # maintenance lock cannot invert a wider ownership lock order.
    bind.execute(
        sa.text(
            "LOCK TABLE ownership_backfill_checkpoints "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )


def _assert_downgrade_is_lossless() -> None:
    checkpoints = sa.table(
        _TABLE_NAME,
        sa.column("phase_key", sa.String()),
    )
    if (
        op.get_bind()
        .execute(sa.select(checkpoints.c.phase_key).limit(1))
        .first()
        is not None
    ):
        raise RuntimeError(_DOWNGRADE_REFUSAL)


def upgrade() -> None:
    op.create_table(
        _TABLE_NAME,
        sa.Column("phase_key", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="running",
        ),
        sa.Column("scan_high_watermark_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_rows", sa.BigInteger(), nullable=False),
        sa.Column(
            "last_scanned_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "scanned_rows",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_rows",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "unchanged_rows",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("data_checksum_before", sa.String(64), nullable=False),
        sa.Column("data_checksum_after", sa.String(64), nullable=False),
        sa.Column("ownership_checksum_after", sa.String(64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("phase_key"),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["health_subjects.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'restore_blocked')",
            name="ck_ownership_backfill_status",
        ),
        sa.CheckConstraint(
            "scan_high_watermark_id >= 0 AND last_scanned_id >= 0",
            name="ck_ownership_backfill_watermarks_nonnegative",
        ),
        sa.CheckConstraint(
            "snapshot_rows >= 0",
            name="ck_ownership_backfill_snapshot_rows_nonnegative",
        ),
        sa.CheckConstraint(
            "last_scanned_id <= scan_high_watermark_id",
            name="ck_ownership_backfill_watermark_order",
        ),
        sa.CheckConstraint(
            "scanned_rows >= 0 AND updated_rows >= 0 AND unchanged_rows >= 0",
            name="ck_ownership_backfill_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "scanned_rows <= snapshot_rows",
            name="ck_ownership_backfill_scanned_within_snapshot",
        ),
        sa.CheckConstraint(
            "scanned_rows = updated_rows + unchanged_rows",
            name="ck_ownership_backfill_count_balance",
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("data_checksum_before"),
            name="ck_ownership_backfill_data_before_sha256",
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("data_checksum_after"),
            name="ck_ownership_backfill_data_after_sha256",
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("ownership_checksum_after"),
            name="ck_ownership_backfill_ownership_after_sha256",
        ),
        sa.CheckConstraint(
            "(status IN ('running', 'restore_blocked') "
            "AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND last_scanned_id = scan_high_watermark_id "
            "AND scanned_rows = snapshot_rows)",
            name="ck_ownership_backfill_completed_state",
        ),
        sa.CheckConstraint(
            "status <> 'restore_blocked' OR "
            "(last_scanned_id = 0 AND scanned_rows = 0 "
            "AND updated_rows = 0 AND unchanged_rows = 0 "
            f"AND data_checksum_before = '{_EMPTY_SHA256}' "
            f"AND data_checksum_after = '{_EMPTY_SHA256}' "
            f"AND ownership_checksum_after = '{_EMPTY_SHA256}')",
            name="ck_ownership_backfill_restore_blocked_state",
        ),
        sa.CheckConstraint(
            "updated_at >= started_at AND "
            "(completed_at IS NULL OR completed_at >= started_at)",
            name="ck_ownership_backfill_timestamp_order",
        ),
    )
    op.create_index(
        _STATUS_UPDATED_INDEX,
        _TABLE_NAME,
        ["status", "updated_at"],
    )


def downgrade() -> None:
    _lock_downgrade_cutover()
    _assert_downgrade_is_lossless()
    op.drop_index(_STATUS_UPDATED_INDEX, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)


__all__ = ["downgrade", "upgrade"]
