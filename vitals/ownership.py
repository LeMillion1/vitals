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
        # ``MIXED`` until it was noticed that this table has no subject column
        # to be mixed about. It is the pre-commercial key/value store, keyed by
        # a string and installation-wide by construction; rows leave it for
        # ``subject_settings``, which does carry a subject. Claiming a boundary
        # the schema cannot express made three ordinary reads look like
        # unscoped ones in the bare-key inventory.
        subject=TargetColumn.NONE,
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
    # A conversation is the patient's, and every row in it names them: the
    # thread so row security can see it, the participants and the messages
    # because a child that inherited its subject implicitly could disagree with
    # its parent, and the composite foreign keys exist to stop exactly that.
    #
    # Not ``user_portable``, and the reason is the third party. An export of one
    # subject would carry what a doctor and a trainer wrote to them, which is
    # not the patient's alone to hand out; an import would let a crafted file
    # put words in a professional's mouth. What the patient may take with them
    # is a question for the portability work, not a default.
    "care_messages": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "care_message_attachments": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        file_asset=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "care_thread_participants": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "care_threads": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
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
    # Not ``user_portable`` and never will be: an export that carried this row
    # would carry somebody's Garmin password out of the installation, and an
    # import that accepted one would let a crafted file plant a credential
    # against another subject's connection.
    "integration_credentials": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL_CHILD,
        subject=TargetColumn.REQUIRED,
        connection=TargetColumn.REQUIRED,
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
    "skincare_logs": _SUBJECT,
    "skincare_observations": _SUBJECT,
    "skincare_products": _SUBJECT,
    "supplements": _SUBJECT,
    # A claim about the world outside this installation, attached to an account.
    # No subject: being a verified doctor is not access to anybody's record, and
    # giving this table a subject column would be the first step to reading it
    # as though it were.
    "professional_profiles": OwnershipSpec(
        OwnershipClass.ACCOUNT_CONTROL,
        subject=TargetColumn.NONE,
        actor=TargetColumn.NONE,
        user_portable=False,
    ),
    # The patient's offer, so it is the patient's row — which puts it inside
    # row security. Accepting reads it in the platform scope, because the
    # professional is not bound to this subject yet and the token is what
    # authorizes reading it at all.
    "professional_invitations": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.NONE,
        user_portable=False,
    ),
    # Half of what access needs, and the patient's row either way.
    "care_relationships": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.NONE,
        user_portable=False,
    ),
    # The other half. Versioned rather than edited, so a superseded row is
    # history and not clutter — which is why it is control-plane rather than
    # portable: a restore that recreated consents would re-grant them.
    "consent_grants": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.NONE,
        user_portable=False,
    ),
    "consent_scopes": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL_CHILD,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.NONE,
        user_portable=False,
    ),
    # The professional's own contribution, in the patient's record but not among
    # the patient's facts. Subject-scoped so row security covers it; authored, so
    # who may change it is a question about the author rather than the subject.
    "professional_notes": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "care_plans": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        actor=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "support_access_grants": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "external_api_tokens": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    # Connector grants belong to the authorizing account and name exactly one
    # record. A nullable subject is retained only for revoked pre-cutover rows.
    "mcp_access_tokens": OwnershipSpec(
        OwnershipClass.ACCOUNT_CONTROL,
        # Revoked pre-cutover rows whose account never owned a record are kept
        # for history and are the only rows allowed to leave this null.
        subject=TargetColumn.OPTIONAL,
        user_portable=False,
    ),
    "mcp_access_token_scopes": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL_CHILD,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "support_access_requests": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL,
        subject=TargetColumn.REQUIRED,
        user_portable=False,
    ),
    "support_access_request_scopes": OwnershipSpec(
        OwnershipClass.SUBJECT_CONTROL_CHILD,
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
    # An account-plane row: it says which provider identity may become this
    # user, and nothing about any person's health. No subject, for the same
    # reason ``users`` has none — a login is not a health record.
    "user_federated_identities": _ACCOUNT,
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


#: ``REQUIRED`` references whose column is still nullable — now none.
#:
#: This was a ratchet, and it reached zero: the provider writers that created
#: ownerless rows are gone, the models declare every registered-required
#: reference ``NOT NULL``, and revision 0049 installs the same contract in the
#: database. The paired test recomputes the set from the models and fails in
#: either direction, so a column cannot quietly lose its constraint and an entry
#: cannot be removed without the constraint actually being there.
PENDING_OWNERSHIP_CONTRACT_COLUMNS: frozenset[tuple[str, str]] = frozenset()


#: The revision that turns every ``REQUIRED`` reference above into ``NOT NULL``,
#: and the last one before it. The distinction is operational, not cosmetic: a
#: lake whose ownership backfill has not finished can be migrated as far as
#: :data:`PRE_OWNERSHIP_CONTRACT_REVISION` and no further, because the contract
#: revision refuses to run while any target column still holds unstamped rows.
#: The deploy order is therefore: migrate to the pre-contract revision, run the
#: backfill phases to completion, then migrate to head.
OWNERSHIP_CONTRACT_REVISION = "0049"
PRE_OWNERSHIP_CONTRACT_REVISION = "0048"


class OwnershipBackfillIncompleteError(RuntimeError):
    """A ``REQUIRED`` ownership column still holds rows nobody owns.

    Raised by the contract migration before it alters anything. It names every
    table that is behind and by how much, so an operator can finish the backfill
    rather than read a bare ``NOT NULL`` violation on whichever column happened
    to come first alphabetically.
    """


def required_ownership_columns() -> tuple[tuple[str, str], ...]:
    """Every ``(table, column)`` the registry says must always name an owner.

    The contract migration keeps its own frozen copy of this list, because a
    migration has to mean the same thing next year as it did on the day it ran.
    This function is the live view, and a contract test compares the two so the
    frozen copy cannot silently fall behind a reclassified table.
    """

    from vitals.models.base import Base

    fields = (
        ("subject", ("subject_id",)),
        ("actor", ("actor_user_id",)),
        # Two tables spell the connection with a qualifier: the AI ledger and
        # the OpenRouter bridge both hold a *platform* connection, and the
        # bridge holds a legacy one beside it. The registry names the boundary;
        # this resolves it against the column the table actually has.
        ("connection", ("integration_connection_id", "legacy_integration_connection_id")),
        ("platform_connection", ("platform_connection_id", "platform_integration_connection_id")),
        ("file_asset", ("file_asset_id",)),
    )
    resolved: list[tuple[str, str]] = []
    for table_name, spec in sorted(_OWNERSHIP_REGISTRY.items()):
        columns = Base.metadata.tables[table_name].columns
        for field, candidates in fields:
            if getattr(spec, field) is not TargetColumn.REQUIRED:
                continue
            match = next((name for name in candidates if name in columns), None)
            if match is None:
                raise LookupError(
                    f"{table_name} is registered {field}=REQUIRED but has none "
                    f"of {candidates}"
                )
            resolved.append((table_name, match))
    return tuple(resolved)


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
