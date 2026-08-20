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


def ownership_for(table_name: str) -> OwnershipSpec:
    """Return the reviewed target contract; unknown tables fail closed."""

    try:
        return OWNERSHIP_REGISTRY[table_name]
    except KeyError as exc:
        raise KeyError(f"table {table_name!r} has no ownership classification") from exc


__all__ = [
    "OWNERSHIP_REGISTRY",
    "OwnershipClass",
    "OwnershipSpec",
    "TargetColumn",
    "WriteIdentity",
    "ownership_for",
]
