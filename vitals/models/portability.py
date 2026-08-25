"""Durable, PHI-free receipts for completed portability imports.

The receipt proves that one archive record was applied to one health subject.
It deliberately stores no payload, filename, filesystem path, display label, or
free-form text.  Import authorization and replay semantics belong to the
portability service; this module owns only the persistence contract.
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
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from vitals.models.base import Base

PORTABILITY_IMPORT_MODE_REPLACE = "replace"
PORTABILITY_RECORD_REF_MAX_LENGTH = 128


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


class PortabilityImportReceipt(Base):
    """Idempotency evidence for one completed subject-record replacement."""

    __tablename__ = "portability_import_receipts"
    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "operation_id",
            name="uq_portability_import_receipts_subject_operation",
        ),
        CheckConstraint(
            _lowercase_sha256_check("manifest_digest"),
            name="ck_portability_import_receipts_manifest_digest",
        ),
        CheckConstraint(
            _opaque_record_ref_check("record_ref"),
            name="ck_portability_import_receipts_record_ref",
        ),
        CheckConstraint(
            _lowercase_sha256_check("record_digest"),
            name="ck_portability_import_receipts_record_digest",
        ),
        CheckConstraint(
            _lowercase_sha256_check("mapping_digest"),
            name="ck_portability_import_receipts_mapping_digest",
        ),
        CheckConstraint(
            f"mode = '{PORTABILITY_IMPORT_MODE_REPLACE}'",
            name="ck_portability_import_receipts_mode",
        ),
        CheckConstraint(
            "row_count >= 0 AND resource_count >= 0",
            name="ck_portability_import_receipts_counts_nonnegative",
        ),
        Index(
            "ix_portability_import_receipts_subject_completed",
            "subject_id",
            "completed_at",
        ),
        Index(
            "ix_portability_import_receipts_actor_completed",
            "actor_user_id",
            "completed_at",
        ),
        Index(
            "ix_portability_import_receipts_archive_subject",
            "archive_id",
            "subject_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "health_subjects.id",
            name="fk_portability_import_receipts_subject",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "users.id",
            name="fk_portability_import_receipts_actor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    archive_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    record_ref: Mapped[str] = mapped_column(
        String(PORTABILITY_RECORD_REF_MAX_LENGTH), nullable=False
    )
    record_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PORTABILITY_IMPORT_MODE_REPLACE,
        server_default=PORTABILITY_IMPORT_MODE_REPLACE,
    )
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resource_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "PORTABILITY_IMPORT_MODE_REPLACE",
    "PORTABILITY_RECORD_REF_MAX_LENGTH",
    "PortabilityImportReceipt",
]
