"""Module 7 — Lab results & parser.

Two tables, ``domain='labs'``:

  * ``lab_results`` — one row per (date, marker) measurement. Carries
    ``InsightsMixin`` (the panel's collection ``date``, ``source`` =
    ``manual`` | ``lab_parser``), the value + unit + the reference range as a
    **snapshot** (labs differ, so we store the range that came with the result),
    the computed out-of-range ``flag``, and a link to the uploaded document's raw
    payload. This is what the per-marker history charts read.
  * ``lab_markers`` — a lean per-marker catalog holding the things that are a
    property of the *marker*, not of any single result: importance ``tier``
    (1 critical / 2 deferrable), an optional ``retest_interval_days``, and a
    ``defer_until`` date set by "Defer Retest" to pause an overdue alert. Rows are
    auto-created the first time a marker is seen.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date as date_type
from typing import Any, Optional

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from vitals.enums import Domain
from vitals.models.base import Base, TimestampMixin
from vitals.models.mixins import InsightsMixin, insights_index
from vitals.models.ownership_mixins import (
    OriginActorMixin,
    RequiredSubjectOwnershipMixin,
)

DOMAIN = Domain.LABS.value
_WHITESPACE = re.compile(r"\s+")


def _fallback_marker_key(context: Any) -> str:
    """Compatibility default for ORM-built fixtures and internal seed rows.

    Live writers pass the alias-aware key explicitly.  This conservative default
    keeps direct ORM construction valid without teaching the model the service's
    reviewed synonym catalog.
    """

    value = str(context.get_current_parameters().get("marker") or "")
    cleaned = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())
    return cleaned.casefold().replace("ё", "е")


def _fallback_original_marker(context: Any) -> str:
    return str(context.get_current_parameters().get("marker") or "")


def _fallback_normalized_name(context: Any) -> str:
    value = str(context.get_current_parameters().get("name") or "")
    cleaned = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())
    return cleaned.casefold().replace("ё", "е")


class LabResult(
    Base,
    RequiredSubjectOwnershipMixin,
    OriginActorMixin,
    InsightsMixin,
    TimestampMixin,
):
    """A single measured marker value on a date."""

    __tablename__ = "lab_results"
    __table_args__ = (
        insights_index(__tablename__),
        Index("ix_lab_results_subject_date", "subject_id", "date"),
        Index(
            "ix_lab_results_subject_domain_date", "subject_id", "domain", "date"
        ),
        # Per-marker history scans (charts) hit this constantly.
        Index("ix_lab_results_marker_date", "marker", "date"),
        Index(
            "ix_lab_results_subject_marker_date", "subject_id", "marker", "date"
        ),
        Index(
            "ix_lab_results_subject_marker_key_date",
            "subject_id",
            "marker_key",
            "date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marker: Mapped[str] = mapped_column(String(128), nullable=False)
    # Stable identity is deliberately separate from presentation.  ``marker``
    # is the canonical display spelling; ``marker_original`` preserves exactly
    # which spelling entered this normalized fact so the cutover is reversible.
    marker_key: Mapped[str] = mapped_column(
        String(256), nullable=False, default=_fallback_marker_key
    )
    marker_original: Mapped[str] = mapped_column(
        String(128), nullable=False, default=_fallback_original_marker
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Reference range snapshot as reported on this result (a lab's range can
    # differ from the catalog default).
    ref_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ref_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Computed classification (vitals.enums.LabFlag); null until range is known.
    flag: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    lab_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    raw_payload_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("raw_payloads.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class LabMarker(Base, RequiredSubjectOwnershipMixin, OriginActorMixin, TimestampMixin):
    """Per-marker reference/config (catalog). No per-day date → no InsightsMixin,
    just ``domain``/``source`` for uniform export (like the supplements catalog)."""

    __tablename__ = "lab_markers"
    __table_args__ = (
        Index("ix_lab_markers_subject_name", "subject_id", "name"),
        # A marker's reference range and deferral are personal, so the name is
        # unique inside one record, not across the installation.
        Index("uq_lab_markers_subject_name", "subject_id", "name", unique=True),
        Index("ix_lab_markers_subject_normalized_name", "subject_id", "normalized_name"),
        Index(
            "uq_lab_markers_subject_normalized_canonical",
            "subject_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("is_canonical = true"),
            sqlite_where=text("is_canonical = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), nullable=False, server_default=DOMAIN)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(256), nullable=False, default=_fallback_normalized_name
    )
    # Collision losers stay as complete, exportable aliases.  Keeping them in
    # the protected source table retains every personal catalog setting and its
    # actor/timestamps while exactly one row drives live behavior.
    is_canonical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ref_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ref_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Importance tier: 1 = critical (alert promptly), 2 = deferrable.
    tier: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    # How often this marker should be retested; null = no schedule.
    retest_interval_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # "Defer Retest" — suppress the overdue alert until this date.
    defer_until: Mapped[Optional[date_type]] = mapped_column(Date, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
