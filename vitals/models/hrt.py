"""HRT / TRT domain models — hormone & anabolic-steroid cycle tracking.

Four tables, all ``domain = 'hrt'``:

  * ``hrt_compounds`` — the curated **molecule catalog** (reference, like
    ``supplements``/``conflict_rules``: no per-day ``InsightsMixin.date``). Seeded
    from ``vitals/data/hrt_compounds.yaml`` via ``hrt_catalog.sync_catalog``,
    keyed on a stable ``key`` slug. The user may add custom rows too.
  * ``hrt_compound_components`` — per-ester breakdown of a multi-ester blend
    (Sustanon/Omnadren) so the active-release curve can sum each ester's decay.
  * ``hrt_doses`` — the **actual administration log** (point metric,
    ``InsightsMixin``). Carries the grey-market provenance the user asked to
    track (brand / underground-lab / batch / measured concentration) as plain
    fields on the row — a molecule is sold under many brands, so brand never
    lives on the catalog.
  * ``hrt_side_effects`` — symptom log graded 1-5 (mirrors GLP-1).

Nothing here blocks: the domain is a harm-reduction tracker. Cross-domain
soft-warn rules (oral 17aa + high liver enzymes, high hematocrit + active
testosterone) live in the conflict-engine catalog, not in the schema.
"""
from __future__ import annotations

from datetime import date as date_type
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vitals.enums import Domain
from vitals.models.base import Base, TimestampMixin
from vitals.models.mixins import InsightsMixin, insights_index
from vitals.models.ownership_mixins import OriginActorMixin, SubjectOwnershipMixin

DOMAIN = Domain.HRT.value

_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class HrtCompound(Base, SubjectOwnershipMixin, OriginActorMixin, TimestampMixin):
    """A molecule in the reference catalog (testosterone ester, oral AAS, AI,
    SERM, GH/IGF/peptide, ...). Brand-agnostic — describes the substance, not a
    product. ``key`` is the stable slug the catalog upserts on and that a dose
    row snapshots."""

    __tablename__ = "hrt_compounds"
    __table_args__ = (
        # The curated catalog belongs to the platform and keeps a global key; a
        # subject's own compound may reuse that key without colliding with
        # anyone.
        Index(
            "uq_hrt_compounds_platform_key",
            "key",
            unique=True,
            postgresql_where=text("subject_id IS NULL"),
            sqlite_where=text("subject_id IS NULL"),
        ),
        Index(
            "uq_hrt_compounds_subject_key",
            "subject_id",
            "key",
            unique=True,
            postgresql_where=text("subject_id IS NOT NULL"),
            sqlite_where=text("subject_id IS NOT NULL"),
        ),
        Index("ix_hrt_compounds_active", "active"),
        Index("ix_hrt_compounds_class", "compound_class"),
        Index("ix_hrt_compounds_subject_key", "subject_id", "key"),
        Index("ix_hrt_compounds_subject_active", "subject_id", "active"),
        UniqueConstraint("id", "subject_id", name="uq_hrt_compounds_id_subject"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), nullable=False, server_default=DOMAIN)
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="manual")

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    name_ru: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Free string validated against the YAML catalog (not a DB enum) so the
    # catalog can grow new classes without a migration.
    compound_class: Mapped[str] = mapped_column(String(32), nullable=False)
    ester: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # vitals.enums.Route.
    route: Mapped[str] = mapped_column(String(32), nullable=False)
    # vitals.enums.DoseUnit — the unit a dose of this compound is logged in.
    dose_unit: Mapped[str] = mapped_column(String(8), nullable=False, server_default="mg")

    conc_mg_ml: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tablet_mg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    half_life_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Parent-hormone mass fraction of the esterified compound (e.g. 0.70 for
    # test enanthate) — converts administered mg into active-hormone mg for the
    # release graph. 1.0 for base hormones/orals.
    active_fraction: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    # Tri-state: 'true' | 'false' | 'partial' (informational, for E2 management).
    aromatizes: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Common street/short names — for search & log matching (NOT brands).
    aliases: Mapped[Optional[Any]] = mapped_column(_JSON_TYPE, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    components: Mapped[list["HrtCompoundComponent"]] = relationship(
        foreign_keys="HrtCompoundComponent.compound_id",
        back_populates="compound",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class HrtCompoundComponent(Base, SubjectOwnershipMixin, TimestampMixin):
    """One ester of a multi-ester blend, with its mg per ml. Empty for
    single-ester/oral compounds; populated for Sustanon-style blends so the
    active-release curve sums each ester's own half-life."""

    __tablename__ = "hrt_compound_components"
    __table_args__ = (
        # Stage-4 subject equality: a component of a custom compound can never
        # reach a compound owned by a different subject, and a curated global
        # component stays with its curated global parent.
        ForeignKeyConstraint(
            ["compound_id", "subject_id"],
            ["hrt_compounds.id", "hrt_compounds.subject_id"],
            ondelete="CASCADE",
            name="fk_hrt_compound_components_compound_subject",
        ),
        Index("ix_hrt_compound_components_compound", "compound_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    compound_id: Mapped[int] = mapped_column(
        ForeignKey("hrt_compounds.id", ondelete="CASCADE"), nullable=False
    )
    ester: Mapped[str] = mapped_column(String(32), nullable=False)
    mg: Mapped[float] = mapped_column(Float, nullable=False)  # mg per ml

    compound: Mapped["HrtCompound"] = relationship(
        back_populates="components",
        foreign_keys="HrtCompoundComponent.compound_id",
    )


class HrtDose(
    Base,
    SubjectOwnershipMixin,
    OriginActorMixin,
    InsightsMixin,
    TimestampMixin,
):
    """A single administration (injection / tablet / application). ``dose`` is in
    ``unit`` (mg for AAS/esters, IU for GH, mcg for peptides). Injectables are
    entered as ``volume_ml`` × concentration; the service computes mg."""

    __tablename__ = "hrt_doses"
    __table_args__ = (
        insights_index(__tablename__),
        Index("ix_hrt_doses_subject_date", "subject_id", "date"),
        Index(
            "ix_hrt_doses_subject_domain_date", "subject_id", "domain", "date"
        ),
        Index("ix_hrt_doses_compound_key", "compound_key"),
        Index(
            "ix_hrt_doses_subject_compound_date",
            "subject_id",
            "compound_key",
            "date",
        ),
        CheckConstraint("dose > 0", name="ck_hrt_doses_dose_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # FK for joins; SET NULL so deleting a catalog entry never wipes history.
    compound_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("hrt_compounds.id", ondelete="SET NULL"), nullable=True
    )
    # Stable slug snapshot — the durable reference, survives catalog edits.
    compound_key: Mapped[str] = mapped_column(String(64), nullable=False)

    dose: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False, server_default="mg")
    # Injectable draw + the concentration actually used (grey-market vials vary
    # from the catalog's typical value); null for orals.
    volume_ml: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    concentration_mg_ml: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Grey-market provenance the user asked to track — free text on the row.
    brand: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lab: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    batch: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Body-map rotation site (vitals.enums.HrtInjectionSite); null for orals.
    site: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class HrtSideEffect(
    Base,
    SubjectOwnershipMixin,
    OriginActorMixin,
    InsightsMixin,
    TimestampMixin,
):
    """A reported side effect on a date, graded 1-5 (mirrors GLP-1)."""

    __tablename__ = "hrt_side_effects"
    __table_args__ = (
        insights_index(__tablename__),
        Index("ix_hrt_side_effects_subject_date", "subject_id", "date"),
        Index(
            "ix_hrt_side_effects_subject_domain_date",
            "subject_id",
            "domain",
            "date",
        ),
        CheckConstraint(
            "severity >= 1 AND severity <= 5",
            name="ck_hrt_side_effects_severity_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    effect_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class HrtCycle(Base, SubjectOwnershipMixin, OriginActorMixin, TimestampMixin):
    """A protocol spanning a date range (``end_date`` null = ongoing), like the
    GLP-1 ``DosePhase`` — carries ``domain``/``source`` for uniform export but no
    single ``InsightsMixin.date``. Owns one plan item per compound; those items'
    schedules drive the planned-dose overlay and the injection reminder."""

    __tablename__ = "hrt_cycles"
    __table_args__ = (
        Index("ix_hrt_cycles_range", "domain", "start_date", "end_date"),
        Index(
            "ix_hrt_cycles_subject_domain_range",
            "subject_id",
            "domain",
            "start_date",
            "end_date",
        ),
        UniqueConstraint("id", "subject_id", name="uq_hrt_cycles_id_subject"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), nullable=False, server_default=DOMAIN)
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="manual")

    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # vitals.enums.CycleKind.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    start_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    end_date: Mapped[Optional[date_type]] = mapped_column(Date, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    items: Mapped[list["HrtCycleItem"]] = relationship(
        foreign_keys="HrtCycleItem.cycle_id",
        back_populates="cycle",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class HrtCycleItem(Base, SubjectOwnershipMixin, TimestampMixin):
    """One compound's plan within a cycle. ``schedule`` is an ordered JSON list of
    segments — each ``{"dose", "interval_days", "duration_days"}`` (flat) or a
    linear ramp ``{"dose_start", "dose_end", "step", "step_every_days",
    "interval_days", "duration_days"}``. The schedule engine
    (``hrt_cycle_service.expand_item_schedule``) turns it into planned
    administrations off a fixed grid anchored at the cycle start."""

    __tablename__ = "hrt_cycle_items"
    __table_args__ = (
        # Stage-4 subject equality with the cycle this item plans.
        ForeignKeyConstraint(
            ["cycle_id", "subject_id"],
            ["hrt_cycles.id", "hrt_cycles.subject_id"],
            ondelete="CASCADE",
            name="fk_hrt_cycle_items_cycle_subject",
        ),
        Index("ix_hrt_cycle_items_cycle", "cycle_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
        ForeignKey("hrt_cycles.id", ondelete="CASCADE"), nullable=False
    )
    compound_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("hrt_compounds.id", ondelete="SET NULL"), nullable=True
    )
    compound_key: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False, server_default="mg")
    # Days after the cycle start before this compound's own grid begins — real
    # protocols stagger compounds (e.g. winstrol from week 5 → 28). 0 = starts
    # with the cycle.
    start_offset_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    schedule: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    cycle: Mapped["HrtCycle"] = relationship(
        back_populates="items",
        foreign_keys="HrtCycleItem.cycle_id",
    )


class HrtCycleTemplate(
    Base,
    SubjectOwnershipMixin,
    OriginActorMixin,
    TimestampMixin,
):
    """A reusable, **date-free** snapshot of a cycle plan — everything relative
    (per-item offsets + schedules), no ``start_date``. Materialized into a real
    ``HrtCycle`` by ``create_cycle_from_template``, and portable across
    instances via export/import because items reference the shared catalog by
    ``compound_key`` slug."""

    __tablename__ = "hrt_cycle_templates"
    __table_args__ = (
        Index("ix_hrt_cycle_templates_name", "name"),
        Index(
            "ix_hrt_cycle_templates_subject_name", "subject_id", "name"
        ),
        UniqueConstraint(
            "id", "subject_id", name="uq_hrt_cycle_templates_id_subject"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(32), nullable=False, server_default=DOMAIN)
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="manual")

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # vitals.enums.CycleKind — the kind a cycle created from this template gets.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    items: Mapped[list["HrtCycleTemplateItem"]] = relationship(
        foreign_keys="HrtCycleTemplateItem.template_id",
        back_populates="template",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class HrtCycleTemplateItem(Base, SubjectOwnershipMixin, TimestampMixin):
    """One compound's plan inside a template — the same shape as ``HrtCycleItem``
    minus the cycle FK. ``compound_key`` only (no ``compound_id``): the slug is
    the portable reference, resolved against the local catalog on apply."""

    __tablename__ = "hrt_cycle_template_items"
    __table_args__ = (
        # Stage-4 subject equality with the template this item belongs to.
        ForeignKeyConstraint(
            ["template_id", "subject_id"],
            ["hrt_cycle_templates.id", "hrt_cycle_templates.subject_id"],
            ondelete="CASCADE",
            name="fk_hrt_cycle_template_items_template_subject",
        ),
        Index("ix_hrt_cycle_template_items_template", "template_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("hrt_cycle_templates.id", ondelete="CASCADE"), nullable=False
    )
    compound_key: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False, server_default="mg")
    start_offset_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    schedule: Mapped[Any] = mapped_column(_JSON_TYPE, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    template: Mapped["HrtCycleTemplate"] = relationship(
        back_populates="items",
        foreign_keys="HrtCycleTemplateItem.template_id",
    )
