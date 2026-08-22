"""Signals — the capture domain for everything that happens "in the moment" and
has no shape: «спать пиздец хочу», «голова раскалывается», «кофе в 22».

This is the layer that *explains* the Garmin numbers. Without it correlations are
one-sided: you can see **what** happened to the body, not **why**.

Two tables, deliberately not one:

  * ``signals`` — one row per parsed fact. Three ``kind`` shapes (state / symptom /
    exposure, see ``vitals.enums.SignalKind``); one incoming phrase can produce
    several rows sharing a ``batch_id`` (the unit of undo).
  * ``day_context`` — one row per day, the answers to "remote? gym? heavy day?".
    Different shape, different life: read by the morning brief and the week
    template, never plotted. Merging it into ``signals`` is the mistake that makes
    the two impossible to separate later.

Keys are **free text** during the shake-out period (owner's call: collect a month,
then consolidate). What keeps that from becoming a trap: the raw text always lands
in ``raw_payloads`` first, and key normalization happens on *read* through
``signals_service.KEY_ALIASES`` — so consolidation is a dict edit, not a migration.
"""
from __future__ import annotations

from datetime import time as time_type
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from vitals.enums import Domain
from vitals.models.base import Base, TimestampMixin
from vitals.models.mixins import InsightsMixin, insights_index
from vitals.models.ownership_mixins import (
    IntegrationConnectionOwnershipMixin,
    OriginActorMixin,
    RequiredSubjectOwnershipMixin,
)

DOMAIN = Domain.SIGNALS.value

# JSONB on Postgres, generic JSON on the SQLite fast-test path (mirrors raw_payloads).
_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


class Signal(
    Base,
    RequiredSubjectOwnershipMixin,
    OriginActorMixin,
    IntegrationConnectionOwnershipMixin,
    InsightsMixin,
    TimestampMixin,
):
    __tablename__ = "signals"
    __table_args__ = (
        insights_index(__tablename__),
        # The undo unit: one incoming message → N rows → one "не то" tap.
        Index("ix_signals_batch", "batch_id"),
        # The key-frequency screen and per-key chart series.
        Index("ix_signals_key_date", "key", "date"),
        Index("ix_signals_subject_date", "subject_id", "date"),
        Index(
            "ix_signals_subject_domain_date",
            "subject_id",
            "domain",
            "date",
        ),
        Index("ix_signals_subject_batch", "subject_id", "batch_id"),
        Index("ix_signals_subject_key_date", "subject_id", "key", "date"),
        Index(
            "ix_signals_connection_batch",
            "integration_connection_id",
            "batch_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Free-form slug until the registry is closed ('sleepiness', 'headache',
    # 'caffeine_late'). Stored as written; aliases fold in on read.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Intensity 1-5 for state/symptom, or an amount for exposure (200 mg of
    # caffeine) — Float, because amounts are not always whole.
    value_num: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # The owner's own words for this fragment — what the frequency screen shows
    # as an example, and what makes a misparse readable after the fact.
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Time of day — critical for exposure ("кофе в 22" only means something with
    # the hour attached). Null when the phrase carried no time.
    at_time: Mapped[Optional[time_type]] = mapped_column(Time, nullable=True)
    raw_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("raw_payloads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Shared by every row parsed out of the same message.
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # Flagged "не то" from the echo message. Dropped from charts, **kept** in the
    # table: these are the actual mistakes the key registry gets built from.
    misparse: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class DayContext(
    Base,
    RequiredSubjectOwnershipMixin,
    OriginActorMixin,
    IntegrationConnectionOwnershipMixin,
    InsightsMixin,
    TimestampMixin,
):
    """What kind of day this is — remote/office, gym or not, workload.

    Overwritten, not versioned: the latest answer wins. ``planned`` keeps what the
    week template had guessed, which is what the template later learns from;
    ``updated_at`` shows the answer was changed.
    """

    __tablename__ = "day_context"
    __table_args__ = (
        insights_index(__tablename__),
        Index("ix_day_context_subject_date", "subject_id", "date"),
        # One answered day per person.
        Index("uq_day_context_subject_date", "subject_id", "date", unique=True),
        Index(
            "ix_day_context_subject_domain_date",
            "subject_id",
            "domain",
            "date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # e.g. {"remote": true, "gym": false, "load": "heavy"} — free-form on purpose,
    # the question set is owner-editable in settings.
    answers: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False, server_default=text("'{}'"))
    planned: Mapped[Optional[Any]] = mapped_column(_JSON_TYPE, nullable=True)
