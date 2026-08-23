"""Machine-readable catalog of the Stage-5 scoped-key cutover.

Every natural key in the single-user schema is global: one weight per date, one
marker per name, one Garmin activity per external id.  A second subject cannot
exist while those hold, because two people legitimately share a date, a marker
name, and — through two accounts — an external id.

This registry is the single reviewed inventory of that change.  It names, for
each legacy global key, the exact scoped key that replaced it and the column the
scope comes from, so the audit that proved the cutover safe, the migrations that
installed the scoped keys and dropped the global ones, and the tests that guard
them all read the same source rather than four hand-kept lists.  The legacy
entries stay after the drop: they are what revision 0048's downgrade recreates.

Scoping alone is not enough: a scoped key over a nullable scope column silently
degenerates to a global key for the rows whose scope is missing.  Every entry
therefore also declares whether its scope column must be present, and the audit
proves that before any index is created.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class ScopeKind(StrEnum):
    """What the replacement key is scoped by."""

    # One row per subject and natural key.
    SUBJECT = "subject"
    # One row per connection and natural key: two accounts of the same provider
    # legitimately carry the same external id or day.
    CONNECTION = "connection"
    # A curated platform row stays globally unique; a subject's custom row is
    # unique only within that subject.
    MIXED_CATALOG = "mixed_catalog"
    # One unresolved alert per key, per the root the alert actually belongs to.
    ALERT_CLASS = "alert_class"


class LegacyKeyKind(StrEnum):
    """How the legacy global key is expressed in the schema today."""

    UNIQUE_CONSTRAINT = "unique_constraint"
    UNIQUE_INDEX = "unique_index"


@dataclass(frozen=True, slots=True)
class ScopedIndex:
    """One replacement unique index."""

    name: str
    columns: tuple[str, ...]
    postgresql_predicate: str | None = None
    sqlite_predicate: str | None = None
    # The column whose absence would silently widen this key back to global.
    # ``None`` means the index deliberately covers rows with no scope at all.
    required_scope_column: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.columns:
            raise ValueError("a scoped index needs a name and at least one column")
        if (self.postgresql_predicate is None) != (self.sqlite_predicate is None):
            raise ValueError("a partial index must define both dialect predicates")
        if (
            self.required_scope_column is not None
            and self.required_scope_column not in self.columns
        ):
            raise ValueError("the required scope column must be part of the key")


@dataclass(frozen=True, slots=True)
class ScopedKeySpec:
    """One legacy global key and the scoped keys that replace it."""

    table: str
    scope: ScopeKind
    legacy_name: str
    legacy_kind: LegacyKeyKind
    legacy_columns: tuple[str, ...]
    replacements: tuple[ScopedIndex, ...]
    legacy_postgresql_predicate: str | None = None
    legacy_sqlite_predicate: str | None = None

    def __post_init__(self) -> None:
        if not self.replacements:
            raise ValueError("a scoped key must name at least one replacement")
        if (self.legacy_postgresql_predicate is None) != (
            self.legacy_sqlite_predicate is None
        ):
            raise ValueError("a partial legacy key must define both predicates")


# One weight per date, one context per date, one marker per name: personal facts
# whose natural key is only unique inside one person's record.
_SUBJECT_SCOPED: tuple[ScopedKeySpec, ...] = (
    ScopedKeySpec(
        table="body_measurements",
        scope=ScopeKind.SUBJECT,
        legacy_name="uq_body_measurement_per_date",
        legacy_kind=LegacyKeyKind.UNIQUE_CONSTRAINT,
        legacy_columns=("date",),
        replacements=(
            ScopedIndex(
                name="uq_body_measurements_subject_date",
                columns=("subject_id", "date"),
                required_scope_column="subject_id",
            ),
        ),
    ),
    ScopedKeySpec(
        table="weight_logs",
        scope=ScopeKind.SUBJECT,
        legacy_name="uq_active_weight_per_date",
        legacy_kind=LegacyKeyKind.UNIQUE_INDEX,
        legacy_columns=("date",),
        legacy_postgresql_predicate="superseded = false",
        legacy_sqlite_predicate="superseded = 0",
        replacements=(
            ScopedIndex(
                name="uq_active_weight_per_subject_date",
                columns=("subject_id", "date"),
                postgresql_predicate="superseded = false",
                sqlite_predicate="superseded = 0",
                required_scope_column="subject_id",
            ),
        ),
    ),
    ScopedKeySpec(
        table="genetic_variants",
        scope=ScopeKind.SUBJECT,
        legacy_name="uq_genetic_variant_rsid",
        legacy_kind=LegacyKeyKind.UNIQUE_INDEX,
        legacy_columns=("rsid",),
        legacy_postgresql_predicate="rsid IS NOT NULL",
        legacy_sqlite_predicate="rsid IS NOT NULL",
        replacements=(
            ScopedIndex(
                name="uq_genetic_variant_subject_rsid",
                columns=("subject_id", "rsid"),
                postgresql_predicate="rsid IS NOT NULL",
                sqlite_predicate="rsid IS NOT NULL",
                required_scope_column="subject_id",
            ),
        ),
    ),
    ScopedKeySpec(
        table="lab_markers",
        scope=ScopeKind.SUBJECT,
        legacy_name="ix_lab_markers_name",
        legacy_kind=LegacyKeyKind.UNIQUE_INDEX,
        legacy_columns=("name",),
        replacements=(
            ScopedIndex(
                name="uq_lab_markers_subject_name",
                columns=("subject_id", "name"),
                required_scope_column="subject_id",
            ),
        ),
    ),
)

# A provider row is unique inside the account it was fetched from.  Two Garmin
# accounts legitimately report the same day and the same activity id.
_CONNECTION_SCOPED: tuple[ScopedKeySpec, ...] = (
    ScopedKeySpec(
        table="garmin_daily",
        scope=ScopeKind.CONNECTION,
        legacy_name="uq_garmin_daily_date",
        legacy_kind=LegacyKeyKind.UNIQUE_CONSTRAINT,
        legacy_columns=("date",),
        replacements=(
            ScopedIndex(
                name="uq_garmin_daily_connection_date",
                columns=("integration_connection_id", "date"),
                required_scope_column="integration_connection_id",
            ),
        ),
    ),
    ScopedKeySpec(
        table="garmin_activities",
        scope=ScopeKind.CONNECTION,
        legacy_name="uq_garmin_activities_external_id",
        legacy_kind=LegacyKeyKind.UNIQUE_CONSTRAINT,
        legacy_columns=("external_id",),
        replacements=(
            ScopedIndex(
                name="uq_garmin_activities_connection_external_id",
                columns=("integration_connection_id", "external_id"),
                required_scope_column="integration_connection_id",
            ),
        ),
    ),
    ScopedKeySpec(
        table="hevy_workouts",
        scope=ScopeKind.CONNECTION,
        legacy_name="uq_hevy_workouts_external_id",
        legacy_kind=LegacyKeyKind.UNIQUE_CONSTRAINT,
        legacy_columns=("external_id",),
        replacements=(
            ScopedIndex(
                name="uq_hevy_workouts_connection_external_id",
                columns=("integration_connection_id", "external_id"),
                required_scope_column="integration_connection_id",
            ),
        ),
    ),
    ScopedKeySpec(
        table="garmin_weight_exports",
        scope=ScopeKind.CONNECTION,
        legacy_name="uq_garmin_weight_exports_date",
        legacy_kind=LegacyKeyKind.UNIQUE_CONSTRAINT,
        legacy_columns=("date",),
        replacements=(
            ScopedIndex(
                name="uq_garmin_weight_exports_connection_date",
                columns=("integration_connection_id", "date"),
                required_scope_column="integration_connection_id",
            ),
        ),
    ),
)

# A curated catalog row belongs to the platform and keeps its global key; a
# subject's own row may reuse that key without colliding with anyone.
_MIXED_CATALOG_SCOPED: tuple[ScopedKeySpec, ...] = (
    ScopedKeySpec(
        table="hrt_compounds",
        scope=ScopeKind.MIXED_CATALOG,
        legacy_name="ix_hrt_compounds_key",
        legacy_kind=LegacyKeyKind.UNIQUE_INDEX,
        legacy_columns=("key",),
        replacements=(
            ScopedIndex(
                name="uq_hrt_compounds_platform_key",
                columns=("key",),
                postgresql_predicate="subject_id IS NULL",
                sqlite_predicate="subject_id IS NULL",
            ),
            ScopedIndex(
                name="uq_hrt_compounds_subject_key",
                columns=("subject_id", "key"),
                postgresql_predicate="subject_id IS NOT NULL",
                sqlite_predicate="subject_id IS NOT NULL",
                required_scope_column="subject_id",
            ),
        ),
    ),
    ScopedKeySpec(
        table="conflict_rules",
        scope=ScopeKind.MIXED_CATALOG,
        legacy_name="ix_conflict_rules_code",
        legacy_kind=LegacyKeyKind.UNIQUE_INDEX,
        legacy_columns=("code",),
        replacements=(
            ScopedIndex(
                name="uq_conflict_rules_platform_code",
                columns=("code",),
                postgresql_predicate="subject_id IS NULL",
                sqlite_predicate="subject_id IS NULL",
            ),
            ScopedIndex(
                name="uq_conflict_rules_subject_code",
                columns=("subject_id", "code"),
                postgresql_predicate="subject_id IS NOT NULL",
                sqlite_predicate="subject_id IS NOT NULL",
                required_scope_column="subject_id",
            ),
        ),
    ),
)

# An unresolved alert is unique per key inside the root it describes: the
# connection for a provider alert, the subject for a health alert, and the
# installation itself for a platform alert.
_ALERT_SCOPED: tuple[ScopedKeySpec, ...] = (
    ScopedKeySpec(
        table="system_alerts",
        scope=ScopeKind.ALERT_CLASS,
        legacy_name="uq_active_alert_per_key_entity",
        legacy_kind=LegacyKeyKind.UNIQUE_INDEX,
        legacy_columns=("alert_key", "entity_ref"),
        legacy_postgresql_predicate="resolved_at IS NULL",
        legacy_sqlite_predicate="resolved_at IS NULL",
        replacements=(
            ScopedIndex(
                name="uq_active_alert_per_connection_key_entity",
                columns=("integration_connection_id", "alert_key", "entity_ref"),
                postgresql_predicate=(
                    "resolved_at IS NULL AND integration_connection_id IS NOT NULL"
                ),
                sqlite_predicate=(
                    "resolved_at IS NULL AND integration_connection_id IS NOT NULL"
                ),
                required_scope_column="integration_connection_id",
            ),
            ScopedIndex(
                name="uq_active_alert_per_subject_key_entity",
                columns=("subject_id", "alert_key", "entity_ref"),
                postgresql_predicate=(
                    "resolved_at IS NULL AND subject_id IS NOT NULL "
                    "AND integration_connection_id IS NULL"
                ),
                sqlite_predicate=(
                    "resolved_at IS NULL AND subject_id IS NOT NULL "
                    "AND integration_connection_id IS NULL"
                ),
                required_scope_column="subject_id",
            ),
            ScopedIndex(
                name="uq_active_alert_per_platform_key_entity",
                columns=("alert_key", "entity_ref"),
                postgresql_predicate=(
                    "resolved_at IS NULL AND subject_id IS NULL "
                    "AND integration_connection_id IS NULL"
                ),
                sqlite_predicate=(
                    "resolved_at IS NULL AND subject_id IS NULL "
                    "AND integration_connection_id IS NULL"
                ),
            ),
        ),
    ),
)


SCOPED_KEYS: tuple[ScopedKeySpec, ...] = (
    *_SUBJECT_SCOPED,
    *_CONNECTION_SCOPED,
    *_MIXED_CATALOG_SCOPED,
    *_ALERT_SCOPED,
)

SCOPED_KEY_REGISTRY: Mapping[str, ScopedKeySpec] = MappingProxyType(
    {spec.legacy_name: spec for spec in SCOPED_KEYS}
)


def scoped_keys_for(table: str) -> Sequence[ScopedKeySpec]:
    """Return every reviewed cutover that touches one table."""

    return tuple(spec for spec in SCOPED_KEYS if spec.table == table)


if len(SCOPED_KEY_REGISTRY) != len(SCOPED_KEYS):  # pragma: no cover - import guard
    raise RuntimeError("two scoped keys claim the same legacy name")

_REPLACEMENT_NAMES = [
    index.name for spec in SCOPED_KEYS for index in spec.replacements
]
if len(set(_REPLACEMENT_NAMES)) != len(_REPLACEMENT_NAMES):  # pragma: no cover
    raise RuntimeError("two scoped keys claim the same replacement index name")


__all__ = [
    "SCOPED_KEYS",
    "SCOPED_KEY_REGISTRY",
    "LegacyKeyKind",
    "ScopeKind",
    "ScopedIndex",
    "ScopedKeySpec",
    "scoped_keys_for",
]
