"""Machine-readable target ownership contract for every persisted table.

The commercial migration cannot rely on convention or a hand-maintained list in
one router.  This registry is the single inventory that makes a newly added table
fail tests until its actor/subject/connection/file boundary is classified.

It describes the *contract target*.  PR-03 initially adds nullable expansion
columns and backfills the legacy owner; ``REQUIRED`` therefore means required at
the eventual cutover, not necessarily non-null in the first migration revision.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class OwnershipClass(StrEnum):
    """Where a table belongs in the authorization/data hierarchy."""

    PLATFORM_JOURNAL = "platform_journal"
    PLATFORM_CONTROL = "platform_control"
    PLATFORM_CONTROL_CHILD = "platform_control_child"
    ACCOUNT_CONTROL = "account_control"
    SUBJECT_ROOT = "subject_root"
    SUBJECT_CONTROL = "subject_control"
    SUBJECT_CONTROL_CHILD = "subject_control_child"
    SUBJECT_DATA = "subject_data"
    SUBJECT_CHILD = "subject_child"
    SUBJECT_OPTIONAL = "subject_optional"
    MIXED_CATALOG = "mixed_catalog"
    MIXED_CATALOG_CHILD = "mixed_catalog_child"
    LEGACY_COMPAT = "legacy_compat"


class TargetColumn(StrEnum):
    """Target requirement for a normalized ownership/provenance reference."""

    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"
    INHERITED = "inherited"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class WriteIdentity:
    """Stable subject/actor attribution passed to domain write services.

    ``actor_user_id=None`` is reserved for an authenticated system/job boundary;
    it is not an instruction to infer the owner inside a domain service.
    """

    subject_id: uuid.UUID
    actor_user_id: uuid.UUID | None

    def __post_init__(self) -> None:
        if not isinstance(self.subject_id, uuid.UUID):
            raise TypeError("subject_id must be a UUID")
        if self.actor_user_id is not None and not isinstance(
            self.actor_user_id, uuid.UUID
        ):
            raise TypeError("actor_user_id must be a UUID or None")


@dataclass(frozen=True, slots=True)
class OwnershipSpec:
    """Target ownership references and ordinary-user portability policy."""

    ownership: OwnershipClass
    subject: TargetColumn = TargetColumn.NONE
    actor: TargetColumn = TargetColumn.NONE
    connection: TargetColumn = TargetColumn.NONE
    platform_connection: TargetColumn = TargetColumn.NONE
    file_asset: TargetColumn = TargetColumn.NONE
    user_portable: bool = True

    def __post_init__(self) -> None:
        values = (
            self.subject,
            self.actor,
            self.connection,
            self.platform_connection,
            self.file_asset,
        )
        if any(not isinstance(value, TargetColumn) for value in values):
            raise TypeError("ownership columns must use TargetColumn values")
        if not isinstance(self.ownership, OwnershipClass):
            raise TypeError("ownership must use an OwnershipClass value")
        if not isinstance(self.user_portable, bool):
            raise TypeError("user_portable must be a boolean")


_ACCOUNT = OwnershipSpec(
    OwnershipClass.ACCOUNT_CONTROL,
    user_portable=False,
)
_SUBJECT = OwnershipSpec(
    OwnershipClass.SUBJECT_DATA,
    subject=TargetColumn.REQUIRED,
    actor=TargetColumn.OPTIONAL,
)
_SUBJECT_CHILD = OwnershipSpec(
    OwnershipClass.SUBJECT_CHILD,
    subject=TargetColumn.INHERITED,
)


# Keep this literal and reviewable. Dynamic inference is unsafe: it would happily
# classify a newly introduced PHI table by the columns its author forgot to add.
_OWNERSHIP_REGISTRY: dict[str, OwnershipSpec] = {
    "ai_invocations": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        platform_connection=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "ai_platform_quota_periods": OwnershipSpec(
        OwnershipClass.PLATFORM_CONTROL,
        user_portable=False,
    ),
    "ai_subject_quota_periods": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "annotations": _SUBJECT,
    "app_settings": OwnershipSpec(
        OwnershipClass.LEGACY_COMPAT,
        subject=TargetColumn.MIXED,
    ),
    "audit_events": OwnershipSpec(
        OwnershipClass.PLATFORM_JOURNAL,
        subject=TargetColumn.OPTIONAL,
        actor=TargetColumn.OPTIONAL,
        user_portable=False,
    ),
    "body_measurements": _SUBJECT,
    "body_scan_metrics": _SUBJECT_CHILD,
    "body_scans": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        file_asset=TargetColumn.OPTIONAL,
    ),
    "conflict_rules": OwnershipSpec(
        OwnershipClass.MIXED_CATALOG,
        subject=TargetColumn.MIXED,
    ),
    "day_context": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.OPTIONAL,
    ),
    "file_assets": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        user_portable=False,
    ),
    "garmin_activities": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.REQUIRED,
    ),
    "garmin_daily": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.REQUIRED,
    ),
    "garmin_intraday": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        connection=TargetColumn.REQUIRED,
    ),
    "garmin_weight_exports": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.REQUIRED,
    ),
    "genetic_variants": _SUBJECT,
    "glp1_dose_phases": _SUBJECT,
    "glp1_injections": _SUBJECT,
    "glp1_side_effects": _SUBJECT,
    "health_subjects": OwnershipSpec(
        OwnershipClass.SUBJECT_ROOT,
        user_portable=False,
    ),
    "hevy_exercises": OwnershipSpec(
        OwnershipClass.SUBJECT_CHILD,
        subject=TargetColumn.INHERITED,
        connection=TargetColumn.INHERITED,
    ),
    "hevy_sets": OwnershipSpec(
        OwnershipClass.SUBJECT_CHILD,
        subject=TargetColumn.INHERITED,
        connection=TargetColumn.INHERITED,
    ),
    "hevy_workouts": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.REQUIRED,
    ),
    "hrt_compound_components": OwnershipSpec(
        OwnershipClass.MIXED_CATALOG_CHILD,
        subject=TargetColumn.INHERITED,
    ),
    "hrt_compounds": OwnershipSpec(
        OwnershipClass.MIXED_CATALOG,
        subject=TargetColumn.MIXED,
        actor=TargetColumn.OPTIONAL,
    ),
    "hrt_cycle_items": _SUBJECT_CHILD,
    "hrt_cycle_template_items": _SUBJECT_CHILD,
    "hrt_cycle_templates": _SUBJECT,
    "hrt_cycles": _SUBJECT,
    "hrt_doses": _SUBJECT,
    "hrt_side_effects": _SUBJECT,
    "integration_connection_settings": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL_CHILD,
        subject=TargetColumn.INHERITED,
        connection=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "integration_connections": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "lab_markers": _SUBJECT,
    "lab_results": _SUBJECT,
    "legacy_openrouter_connection_bridges": OwnershipSpec(
        OwnershipClass.PLATFORM_CONTROL_CHILD,
        connection=TargetColumn.REQUIRED,
        platform_connection=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "meal_logs": _SUBJECT,
    "milestones": _SUBJECT,
    "noise_markers": _SUBJECT,
    "notifications": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.OPTIONAL,
        # A delivered message only means something together with the person it
        # went to and the channel that carried it, and backup v1 transports
        # neither.  A restored address-less row would also resurrect dedupe keys
        # that no longer scope to anything, so the local delivery log is
        # retained in place rather than exported and replaced.
        user_portable=False,
    ),
    "notification_delivery_intents": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "ownership_backfill_checkpoints": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "platform_settings": OwnershipSpec(
        OwnershipClass.ACCOUNT_CONTROL,
        user_portable=False,
    ),
    "platform_integration_connections": OwnershipSpec(
        OwnershipClass.PLATFORM_CONTROL,
        user_portable=False,
    ),
    "progress_photos": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        file_asset=TargetColumn.REQUIRED,
    ),
    "raw_payloads": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.OPTIONAL,
        file_asset=TargetColumn.OPTIONAL,
    ),
    "shared_reports": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        user_portable=False,
    ),
    "signals": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.OPTIONAL,
    ),
    "skincare_logs": _SUBJECT,
    "skincare_observations": _SUBJECT,
    "skincare_products": _SUBJECT,
    "supplements": _SUBJECT,
    "support_access_grants": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "support_access_scopes": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL_CHILD,
        subject=TargetColumn.INHERITED,
        user_portable=False,
    ),
    "subject_settings": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "system_alerts": OwnershipSpec(
        OwnershipClass.SUBJECT_OPTIONAL,
        subject=TargetColumn.OPTIONAL,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.OPTIONAL,
    ),
    "user_roles": _ACCOUNT,
    "user_settings": _ACCOUNT,
    "users": _ACCOUNT,
    "weekly_digests": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.OPTIONAL,
        # Backup v1 cannot carry either subject-provider C or platform
        # AIInvocation provenance. Preserve live artifacts in place and keep the
        # narrative available through the separate curated LLM export instead of
        # restoring a forged S-only generated row.
        user_portable=False,
    ),
    "weight_logs": OwnershipSpec(
        OwnershipClass.SUBJECT_DATA,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.OPTIONAL,
        connection=TargetColumn.OPTIONAL,
    ),
}
OWNERSHIP_REGISTRY: Mapping[str, OwnershipSpec] = MappingProxyType(
    _OWNERSHIP_REGISTRY
)


#: ``REQUIRED`` references whose column is still nullable, and why that is not
#: yet fixable. Every one of these is waiting on the same thing: a legacy write
#: path that still creates the row without the reference. ``garmin_service``'s
#: unscoped ``ingest_daily`` / ``ingest_intraday`` / ``ingest_activities`` and
#: ``raw_payload_service.upsert_raw_payload`` are the remaining writers, and a
#: ``NOT NULL`` installed before they are retired would break the Garmin and
#: Hevy syncs on their next run rather than protect anything.
#:
#: This is a ratchet, like ``vitals/legacy_scope.py``: the paired test recomputes
#: the set from the models and fails in either direction, so it can only shrink.
#: When it is empty the contract migration is safe to write.
PENDING_OWNERSHIP_CONTRACT_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("annotations", "subject_id"),
        ("body_measurements", "subject_id"),
        ("body_scans", "subject_id"),
        ("day_context", "subject_id"),
        ("garmin_activities", "integration_connection_id"),
        ("garmin_activities", "subject_id"),
        ("garmin_daily", "integration_connection_id"),
        ("garmin_daily", "subject_id"),
        ("garmin_intraday", "integration_connection_id"),
        ("garmin_intraday", "subject_id"),
        ("garmin_weight_exports", "integration_connection_id"),
        ("garmin_weight_exports", "subject_id"),
        ("genetic_variants", "subject_id"),
        ("glp1_dose_phases", "subject_id"),
        ("glp1_injections", "subject_id"),
        ("glp1_side_effects", "subject_id"),
        ("hevy_workouts", "integration_connection_id"),
        ("hevy_workouts", "subject_id"),
        ("hrt_cycle_templates", "subject_id"),
        ("hrt_cycles", "subject_id"),
        ("hrt_doses", "subject_id"),
        ("hrt_side_effects", "subject_id"),
        ("lab_markers", "subject_id"),
        ("lab_results", "subject_id"),
        ("meal_logs", "subject_id"),
        ("milestones", "subject_id"),
        ("noise_markers", "subject_id"),
        ("notifications", "subject_id"),
        ("progress_photos", "file_asset_id"),
        ("progress_photos", "subject_id"),
        ("raw_payloads", "subject_id"),
        ("shared_reports", "subject_id"),
        ("signals", "subject_id"),
        ("skincare_logs", "subject_id"),
        ("skincare_observations", "subject_id"),
        ("skincare_products", "subject_id"),
        ("supplements", "subject_id"),
        ("weekly_digests", "subject_id"),
        ("weight_logs", "subject_id"),
    }
)


def required_ownership_columns() -> tuple[tuple[str, str], ...]:
    """Every ``(table, column)`` the registry says must always name an owner.

    The contract migration keeps its own frozen copy of this list, because a
    migration has to mean the same thing next year as it did on the day it ran.
    This function is the live view, and a contract test compares the two so the
    frozen copy cannot silently fall behind a reclassified table.
    """

    return tuple(
        (table_name, column_name)
        for table_name, spec in sorted(_OWNERSHIP_REGISTRY.items())
        for field, column_name in (
            ("subject", "subject_id"),
            ("actor", "actor_user_id"),
            ("connection", "integration_connection_id"),
            ("platform_connection", "platform_connection_id"),
            ("file_asset", "file_asset_id"),
        )
        if getattr(spec, field) is TargetColumn.REQUIRED
    )


def ownership_for(table_name: str) -> OwnershipSpec:
    """Return the reviewed target contract; unknown tables fail closed."""

    try:
        return OWNERSHIP_REGISTRY[table_name]
    except KeyError as exc:
        raise KeyError(f"table {table_name!r} has no ownership classification") from exc


__all__ = [
    "OWNERSHIP_CONTRACT_REVISION",
    "OwnershipBackfillIncompleteError",
    "OWNERSHIP_REGISTRY",
    "PRE_OWNERSHIP_CONTRACT_REVISION",
    "OwnershipClass",
    "OwnershipSpec",
    "PENDING_OWNERSHIP_CONTRACT_COLUMNS",
    "TargetColumn",
    "WriteIdentity",
    "ownership_for",
    "required_ownership_columns",
]
