"""Platform-funded AI invocation provenance without prompts or health data.

The OpenRouter credential root is installation-owned. Each paid attempt remains
subject-scoped for authorization, idempotency, quota accounting, and attribution,
but this table deliberately stores no prompt, response, document, medical value,
or free-form provider error. Domain artifacts retain their own PHI separately.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from datetime import date as date_type
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
)
from vitals.models.base import Base


def _values(enum_type: type) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AIInvocation(Base):
    """One idempotent, potentially paid platform-AI operation for one subject."""

    __tablename__ = "ai_invocations"
    __table_args__ = (
        UniqueConstraint("id", "subject_id", name="uq_ai_invocations_id_subject"),
        UniqueConstraint(
            "subject_id",
            "purpose",
            "idempotency_key",
            name="uq_ai_invocations_subject_purpose_idempotency",
        ),
        ForeignKeyConstraint(
            ["raw_payload_id", "subject_id"],
            ["raw_payloads.id", "raw_payloads.subject_id"],
            ondelete="RESTRICT",
            name="fk_ai_invocations_raw_payload_subject",
        ),
        ForeignKeyConstraint(
            ["platform_integration_connection_id", "config_version"],
            [
                "platform_integration_connections.id",
                "platform_integration_connections.config_version",
            ],
            ondelete="RESTRICT",
            name="fk_ai_invocations_platform_connection_config",
        ),
        ForeignKeyConstraint(
            ["quota_period_start", "quota_period_end"],
            [
                "ai_platform_quota_periods.period_start",
                "ai_platform_quota_periods.period_end",
            ],
            ondelete="RESTRICT",
            name="fk_ai_invocations_platform_quota_period",
        ),
        ForeignKeyConstraint(
            ["subject_id", "quota_period_start", "quota_period_end"],
            [
                "ai_subject_quota_periods.subject_id",
                "ai_subject_quota_periods.period_start",
                "ai_subject_quota_periods.period_end",
            ],
            ondelete="RESTRICT",
            name="fk_ai_invocations_subject_quota_period",
        ),
        CheckConstraint(
            f"purpose IN ({_values(AIInvocationPurpose)})",
            name="ck_ai_invocations_purpose",
        ),
        CheckConstraint(
            "(purpose IN ('signal_parse', 'question_reply', "
            "'lab_document_parse', 'body_scan_parse') "
            "AND raw_payload_id IS NOT NULL) OR "
            "(purpose IN ('weekly_digest', 'daily_brief') "
            "AND raw_payload_id IS NULL)",
            name="ck_ai_invocations_purpose_raw_payload",
        ),
        CheckConstraint(
            f"source IN ({_values(AIInvocationSource)})",
            name="ck_ai_invocations_source",
        ),
        CheckConstraint(
            "(source = 'scheduler' AND actor_user_id IS NULL) OR "
            "(source <> 'scheduler' AND actor_user_id IS NOT NULL)",
            name="ck_ai_invocations_source_actor",
        ),
        CheckConstraint(
            f"status IN ({_values(AIInvocationStatus)})",
            name="ck_ai_invocations_status",
        ),
        CheckConstraint(
            "length(trim(model)) > 0", name="ck_ai_invocations_model_not_blank"
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_ai_invocations_idempotency_key_not_blank",
        ),
        CheckConstraint(
            "config_version >= 1",
            name="ck_ai_invocations_config_version_positive",
        ),
        CheckConstraint(
            "upstream_request_id IS NULL OR "
            "length(trim(upstream_request_id)) > 0",
            name="ck_ai_invocations_upstream_request_id_not_blank",
        ),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_ai_invocations_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_ai_invocations_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "cost_microunits IS NULL OR cost_microunits >= 0",
            name="ck_ai_invocations_cost_microunits_nonnegative",
        ),
        CheckConstraint(
            "quota_period_end > quota_period_start",
            name="ck_ai_invocations_quota_period_positive",
        ),
        CheckConstraint(
            "reserved_cost_microunits >= 0 AND reserved_units >= 0 AND "
            "(reserved_cost_microunits > 0 OR reserved_units > 0)",
            name="ck_ai_invocations_reservation_positive",
        ),
        CheckConstraint(
            "charged_cost_microunits >= 0 AND charged_units >= 0 AND "
            "charged_cost_microunits <= reserved_cost_microunits AND "
            "charged_units <= reserved_units",
            name="ck_ai_invocations_charge_within_reservation",
        ),
        CheckConstraint(
            "cost_microunits IS NULL OR "
            "cost_microunits <= reserved_cost_microunits",
            name="ck_ai_invocations_actual_cost_within_reservation",
        ),
        CheckConstraint(
            "coalesce(input_tokens, 0) + coalesce(output_tokens, 0) "
            "<= reserved_units",
            name="ck_ai_invocations_actual_units_within_reservation",
        ),
        CheckConstraint(
            "(status = 'prepared' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'dispatching' AND started_at IS NOT NULL "
            "AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'ambiguous') "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status = 'cancelled' AND started_at IS NULL "
            "AND finished_at IS NOT NULL)",
            name="ck_ai_invocations_lifecycle_timestamps",
        ),
        CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name="ck_ai_invocations_timestamp_order",
        ),
        CheckConstraint(
            "(status IN ('failed', 'ambiguous', 'cancelled') "
            f"AND error_code IN ({_values(AIInvocationErrorCode)})) OR "
            "(status NOT IN ('failed', 'ambiguous', 'cancelled') "
            "AND error_code IS NULL)",
            name="ck_ai_invocations_error_state",
        ),
        CheckConstraint(
            "(status IN ('prepared', 'cancelled') "
            "AND charged_cost_microunits = 0 AND charged_units = 0) OR "
            "(status IN ('dispatching', 'succeeded', 'failed', 'ambiguous') "
            "AND charged_cost_microunits = reserved_cost_microunits "
            "AND charged_units = reserved_units)",
            name="ck_ai_invocations_accounting_state",
        ),
        Index(
            "ix_ai_invocations_subject_created",
            "subject_id",
            "created_at",
        ),
        Index(
            "ix_ai_invocations_platform_status_created",
            "platform_integration_connection_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_ai_invocations_actor_created",
            "actor_user_id",
            "created_at",
        ),
        Index(
            "ix_ai_invocations_raw_purpose_created",
            "raw_payload_id",
            "purpose",
            "created_at",
        ),
        Index(
            "uq_ai_invocations_raw_purpose_succeeded",
            "raw_payload_id",
            "purpose",
            unique=True,
            postgresql_where=text(
                "raw_payload_id IS NOT NULL AND status = 'succeeded'"
            ),
            sqlite_where=text(
                "raw_payload_id IS NOT NULL AND status = 'succeeded'"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    raw_payload_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    platform_integration_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    quota_period_start: Mapped[date_type] = mapped_column(Date, nullable=False)
    quota_period_end: Mapped[date_type] = mapped_column(Date, nullable=False)
    reserved_cost_microunits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_cost_microunits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    charged_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default=AIInvocationStatus.PREPARED.value,
    )
    upstream_request_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    input_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    cost_microunits: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AIPlatformQuotaPeriod(Base):
    """One half-open installation billing period with hard shared counters."""

    __tablename__ = "ai_platform_quota_periods"
    __table_args__ = (
        CheckConstraint(
            "period_end > period_start",
            name="ck_ai_platform_quota_periods_positive_period",
        ),
        CheckConstraint(
            "cost_limit_microunits >= 0 AND unit_limit >= 0",
            name="ck_ai_platform_quota_periods_nonnegative_limits",
        ),
        CheckConstraint(
            "reserved_cost_microunits >= 0 AND charged_cost_microunits >= 0 "
            "AND reserved_units >= 0 AND charged_units >= 0",
            name="ck_ai_platform_quota_periods_nonnegative_counters",
        ),
        CheckConstraint(
            "reserved_cost_microunits + charged_cost_microunits "
            "<= cost_limit_microunits AND "
            "reserved_units + charged_units <= unit_limit",
            name="ck_ai_platform_quota_periods_within_limits",
        ),
        Index("ix_ai_platform_quota_periods_end", "period_end"),
    )

    period_start: Mapped[date_type] = mapped_column(Date, primary_key=True)
    period_end: Mapped[date_type] = mapped_column(Date, primary_key=True)
    cost_limit_microunits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_cost_microunits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    charged_cost_microunits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    reserved_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    charged_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    configured_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AISubjectQuotaPeriod(Base):
    """One half-open subject billing period; no nullable platform sentinel."""

    __tablename__ = "ai_subject_quota_periods"
    __table_args__ = (
        CheckConstraint(
            "period_end > period_start",
            name="ck_ai_subject_quota_periods_positive_period",
        ),
        CheckConstraint(
            "cost_limit_microunits >= 0 AND unit_limit >= 0",
            name="ck_ai_subject_quota_periods_nonnegative_limits",
        ),
        CheckConstraint(
            "reserved_cost_microunits >= 0 AND charged_cost_microunits >= 0 "
            "AND reserved_units >= 0 AND charged_units >= 0",
            name="ck_ai_subject_quota_periods_nonnegative_counters",
        ),
        CheckConstraint(
            "reserved_cost_microunits + charged_cost_microunits "
            "<= cost_limit_microunits AND "
            "reserved_units + charged_units <= unit_limit",
            name="ck_ai_subject_quota_periods_within_limits",
        ),
        Index(
            "ix_ai_subject_quota_periods_end",
            "subject_id",
            "period_end",
        ),
    )

    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    period_start: Mapped[date_type] = mapped_column(Date, primary_key=True)
    period_end: Mapped[date_type] = mapped_column(Date, primary_key=True)
    cost_limit_microunits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_cost_microunits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    charged_cost_microunits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    reserved_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    charged_units: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    configured_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class LegacyOpenRouterConnectionBridge(Base):
    """Exact non-secret mapping from one retired subject root to a platform root."""

    __tablename__ = "legacy_openrouter_connection_bridges"
    __table_args__ = (
        Index(
            "ix_legacy_openrouter_bridges_platform_connection",
            "platform_integration_connection_id",
        ),
    )

    legacy_integration_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    platform_integration_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("platform_integration_connections.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = _created_at()


__all__ = [
    "AIInvocation",
    "AIPlatformQuotaPeriod",
    "AISubjectQuotaPeriod",
    "LegacyOpenRouterConnectionBridge",
]
