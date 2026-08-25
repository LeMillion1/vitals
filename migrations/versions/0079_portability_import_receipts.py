"""Add subject-isolated portability v2 import receipts.

Revision ID: 0079
Revises: 0078
Create Date: 2026-08-25

Each row is PHI-free idempotency evidence for one completed replacement of one
subject record.  It contains no archive bytes, filenames, paths, labels, or
free-form metadata.  Import authorization and replay comparison are application
contracts layered on top of this schema.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: Union[str, None] = "0078"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "portability_import_receipts"
SUBJECT_ISOLATED_TABLES: tuple[str, ...] = (TABLE_NAME,)
SUBJECT_SETTING = "vitals.subject_id"
POLICY_NAME = "rls_subject_isolation"
PORTABILITY_IMPORT_MODE_REPLACE = "replace"
PORTABILITY_RECORD_REF_MAX_LENGTH = 128
_PREDICATE = (
    f"subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid"
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


def _opaque_record_ref_check(column_name: str) -> str:
    """Allow only bounded base64url-style identifiers, never paths or names."""

    remainder = column_name
    for character in (
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
    ):
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) BETWEEN 1 AND {PORTABILITY_RECORD_REF_MAX_LENGTH} "
        f"AND length({remainder}) = 0"
    )


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                "health_subjects.id",
                name="fk_portability_import_receipts_subject",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                name="fk_portability_import_receipts_actor",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("operation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("archive_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column(
            "record_ref",
            sa.String(PORTABILITY_RECORD_REF_MAX_LENGTH),
            nullable=False,
        ),
        sa.Column("record_digest", sa.String(64), nullable=False),
        sa.Column("mapping_digest", sa.String(64), nullable=False),
        sa.Column(
            "mode",
            sa.String(16),
            nullable=False,
            server_default=PORTABILITY_IMPORT_MODE_REPLACE,
        ),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("resource_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "subject_id",
            "operation_id",
            name="uq_portability_import_receipts_subject_operation",
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("manifest_digest"),
            name="ck_portability_import_receipts_manifest_digest",
        ),
        sa.CheckConstraint(
            _opaque_record_ref_check("record_ref"),
            name="ck_portability_import_receipts_record_ref",
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("record_digest"),
            name="ck_portability_import_receipts_record_digest",
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("mapping_digest"),
            name="ck_portability_import_receipts_mapping_digest",
        ),
        sa.CheckConstraint(
            f"mode = '{PORTABILITY_IMPORT_MODE_REPLACE}'",
            name="ck_portability_import_receipts_mode",
        ),
        sa.CheckConstraint(
            "row_count >= 0 AND resource_count >= 0",
            name="ck_portability_import_receipts_counts_nonnegative",
        ),
    )
    op.create_index(
        "ix_portability_import_receipts_subject_completed",
        TABLE_NAME,
        ["subject_id", "completed_at"],
    )
    op.create_index(
        "ix_portability_import_receipts_actor_completed",
        TABLE_NAME,
        ["actor_user_id", "completed_at"],
    )
    op.create_index(
        "ix_portability_import_receipts_archive_subject",
        TABLE_NAME,
        ["archive_id", "subject_id"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(f'ALTER TABLE "{TABLE_NAME}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{TABLE_NAME}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "{TABLE_NAME}" '
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{TABLE_NAME}"')
        op.execute(f'ALTER TABLE "{TABLE_NAME}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{TABLE_NAME}" DISABLE ROW LEVEL SECURITY')
    op.drop_index(
        "ix_portability_import_receipts_archive_subject", table_name=TABLE_NAME
    )
    op.drop_index(
        "ix_portability_import_receipts_actor_completed", table_name=TABLE_NAME
    )
    op.drop_index(
        "ix_portability_import_receipts_subject_completed", table_name=TABLE_NAME
    )
    op.drop_table(TABLE_NAME)


__all__ = ["downgrade", "upgrade"]
