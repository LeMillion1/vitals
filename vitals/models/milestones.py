"""Module 10 — Milestones & weekly reporting.

  * ``milestones`` — goal cards. ``domain`` here names the *related* health area
    (weight / glp1 / labs / …) so goals can be filtered per area; it's a config
    object (no per-day date), so it carries ``domain`` like the supplements
    catalog rather than the full ``InsightsMixin``.
  * ``weekly_digests`` — the generated AI narrative. This *is* a dated artifact in
    the data lake, so it uses ``InsightsMixin`` (``domain='milestones'``,
    ``source='scheduler'`` | ``'manual'``). ``context_json`` keeps the structured
    cross-domain snapshot the narrative was built from (re-inspect / re-run).
"""
from __future__ import annotations

from datetime import date as date_type
from typing import Any, Optional

from sqlalchemy import JSON, Date, Float, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vitals.enums import DigestKind, Domain, MilestoneStatus
from vitals.models.base import Base, TimestampMixin
from vitals.models.mixins import InsightsMixin, insights_index
from vitals.models.ownership_mixins import (
    IntegrationConnectionOwnershipMixin,
    OriginActorMixin,
    SubjectOwnershipMixin,
)

DOMAIN = Domain.MILESTONES.value

_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


class Milestone(Base, SubjectOwnershipMixin, OriginActorMixin, TimestampMixin):
    """A goal card: name, related domain, optional numeric target + deadline."""

    __tablename__ = "milestones"
    __table_args__ = (
        Index("ix_milestones_status", "status"),
        Index("ix_milestones_subject_status", "subject_id", "status"),
        Index("ix_milestones_subject_deadline", "subject_id", "deadline"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # The health area this goal relates to (weight / glp1 / labs / ...).
    domain: Mapped[str] = mapped_column(String(32), nullable=False, server_default=Domain.WEIGHT.value)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    target_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    deadline: Mapped[Optional[date_type]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=MilestoneStatus.ACTIVE.value
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class WeeklyDigest(
    Base,
    SubjectOwnershipMixin,
    OriginActorMixin,
    IntegrationConnectionOwnershipMixin,
    InsightsMixin,
    TimestampMixin,
):
    """A generated cross-domain narrative + the context it was built from.

    ``kind`` (weekly | daily_brief | evening) is what lets the daily brief reuse
    this table and the /reports page instead of a second store: same artifact,
    different cadence. Readers filter by it, so a brief never surfaces where a
    weekly digest is expected.
    """

    __tablename__ = "weekly_digests"
    __table_args__ = (
        insights_index(__tablename__),
        Index("ix_weekly_digests_subject_date", "subject_id", "date"),
        Index(
            "ix_weekly_digests_subject_domain_date",
            "subject_id",
            "domain",
            "date",
        ),
        Index(
            "ix_weekly_digests_subject_kind_date", "subject_id", "kind", "date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=DigestKind.WEEKLY.value
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
