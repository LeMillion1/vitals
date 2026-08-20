"""Platform-funded AI invocation provenance without prompts or health data.

The OpenRouter credential root is installation-owned. Each paid attempt remains
subject-scoped for authorization, idempotency, quota accounting, and attribution,
but this table deliberately stores no prompt, response, document, medical value,
or free-form provider error. Domain artifacts retain their own PHI separately.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
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
            ["platform_integration_connection_id", "config_version"],
            [
                "platform_integration_connections.id",
                "platform_integration_connections.config_version",
            ],
            ondelete="RESTRICT",
            name="fk_ai_invocations_platform_connection_config",
        ),
        CheckConstraint(
            f"purpose IN ({_values(AIInvocationPurpose)})",
            name="ck_ai_invocations_purpose",
        ),
        CheckConstraint(
            f"source IN ({_values(AIInvocationSource)})",
            name="ck_ai_invocations_source",
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
            "(status = 'prepared' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'dispatching' AND started_at IS NOT NULL "
            "AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'ambiguous') "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status = 'cancelled' AND finished_at IS NOT NULL)",
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
    platform_integration_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
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


__all__ = ["AIInvocation", "LegacyOpenRouterConnectionBridge"]
