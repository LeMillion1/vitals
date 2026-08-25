"""One exact, separately reviewed support repair and its reversible receipt."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vitals.enums import SupportRepairStatus
from vitals.models.base import Base, TimestampMixin


CLEAR_DERIVED_ESTIMATES_OPERATION = "body_measurement.clear_derived_estimates"


class SupportRepairAction(Base, TimestampMixin):
    """Clear two derived estimates on one measurement, never arbitrary fields.

    The before values are PHI and therefore stay in this subject-isolated table.
    Audit events reference the action and list field names, but never copy values.
    The after state is schema-fixed: both estimates are NULL.
    """

    __tablename__ = "support_repair_actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["support_access_grant_id", "subject_id", "proposed_by_user_id"],
            [
                "support_access_grants.id",
                "support_access_grants.subject_id",
                "support_access_grants.granted_to_user_id",
            ],
            name="fk_support_repair_actions_exact_grant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_body_measurement_id", "subject_id"],
            ["body_measurements.id", "body_measurements.subject_id"],
            name="fk_support_repair_actions_exact_measurement",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "support_access_grant_id",
            "idempotency_key",
            name="uq_support_repair_actions_grant_idempotency",
        ),
        CheckConstraint(
            f"operation_key = '{CLEAR_DERIVED_ESTIMATES_OPERATION}'",
            name="ck_support_repair_actions_operation",
        ),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'executed', "
            "'stale', 'reverted')",
            name="ck_support_repair_actions_status",
        ),
        CheckConstraint(
            "execute_before > proposed_at",
            name="ck_support_repair_actions_positive_window",
        ),
        CheckConstraint(
            "before_body_fat_pct IS NOT NULL OR before_lbm_kg IS NOT NULL",
            name="ck_support_repair_actions_has_change",
        ),
        CheckConstraint(
            "status NOT IN ('proposed', 'approved') "
            "OR target_body_measurement_id IS NOT NULL",
            name="ck_support_repair_actions_open_target",
        ),
        CheckConstraint(
            "(status = 'proposed' AND reviewed_by_user_id IS NULL "
            "AND reviewed_at IS NULL AND executed_by_user_id IS NULL "
            "AND executed_at IS NULL AND target_updated_at_after_execute IS NULL "
            "AND reverted_by_user_id IS NULL AND reverted_at IS NULL) OR "
            "(status IN ('approved', 'declined', 'stale') "
            "AND reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND executed_by_user_id IS NULL AND executed_at IS NULL "
            "AND target_updated_at_after_execute IS NULL "
            "AND reverted_by_user_id IS NULL AND reverted_at IS NULL) OR "
            "(status = 'executed' AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND executed_by_user_id IS NOT NULL "
            "AND executed_at IS NOT NULL "
            "AND target_updated_at_after_execute IS NOT NULL "
            "AND reverted_by_user_id IS NULL AND reverted_at IS NULL) OR "
            "(status = 'reverted' AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND executed_by_user_id IS NOT NULL "
            "AND executed_at IS NOT NULL "
            "AND target_updated_at_after_execute IS NOT NULL "
            "AND reverted_by_user_id IS NOT NULL AND reverted_at IS NOT NULL)",
            name="ck_support_repair_actions_lifecycle",
        ),
        Index(
            "ix_support_repair_actions_subject_status_proposed",
            "subject_id",
            "status",
            "proposed_at",
        ),
        Index(
            "ix_support_repair_actions_grant_status",
            "support_access_grant_id",
            "status",
        ),
        Index(
            "uq_support_repair_actions_open_target",
            "subject_id",
            "target_body_measurement_id",
            "operation_key",
            unique=True,
            postgresql_where=text("status IN ('proposed', 'approved')"),
            sqlite_where=text("status IN ('proposed', 'approved')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    support_access_grant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    proposed_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_key: Mapped[str] = mapped_column(
        String(96), nullable=False, default=CLEAR_DERIVED_ESTIMATES_OPERATION
    )
    target_body_measurement_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SupportRepairStatus.PROPOSED.value
    )
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    execute_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    before_body_fat_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    before_lbm_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_updated_at_at_proposal: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False
    )
    reviewed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    target_updated_at_after_execute: Mapped[Optional[datetime]] = mapped_column(
        DateTime(), nullable=True
    )
    reverted_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reverted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    grant = relationship("SupportAccessGrant", foreign_keys=[support_access_grant_id])
    target = relationship("BodyMeasurement", foreign_keys=[target_body_measurement_id])
    proposed_by = relationship("User", foreign_keys=[proposed_by_user_id])
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_user_id])
    executed_by = relationship("User", foreign_keys=[executed_by_user_id])
    reverted_by = relationship("User", foreign_keys=[reverted_by_user_id])


__all__ = ["CLEAR_DERIVED_ESTIMATES_OPERATION", "SupportRepairAction"]
