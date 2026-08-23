"""Contract tests for the complete subject-ownership inventory."""

from __future__ import annotations

import pytest

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.models.base import Base
from vitals.ownership import (
    OWNERSHIP_REGISTRY,
    PENDING_OWNERSHIP_CONTRACT_COLUMNS,
    OwnershipClass,
    TargetColumn,
    ownership_for,
)


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader does when the ownership backfill has not reached a row yet, which is
# a state the application itself can no longer create. The schema says so, so
# this module asks for the one that stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


def test_every_registered_table_has_exactly_one_ownership_contract():
    assert set(OWNERSHIP_REGISTRY) == set(Base.metadata.tables)
    assert len(OWNERSHIP_REGISTRY) == 68


def test_unknown_table_fails_closed_instead_of_inheriting_a_default():
    with pytest.raises(KeyError, match="no ownership classification"):
        ownership_for("new_health_table_someone_forgot_to_register")


def test_registry_cannot_be_mutated_at_runtime():
    with pytest.raises(TypeError):
        OWNERSHIP_REGISTRY["users"] = OWNERSHIP_REGISTRY[  # type: ignore[index]
            "audit_events"
        ]


def test_subject_data_never_has_an_unscoped_target_contract():
    subject_classes = {
        OwnershipClass.SUBJECT_DATA,
        OwnershipClass.SUBJECT_CHILD,
        OwnershipClass.SUBJECT_OPTIONAL,
        OwnershipClass.MIXED_CATALOG,
        OwnershipClass.MIXED_CATALOG_CHILD,
        OwnershipClass.SUBJECT_CONTROL,
        OwnershipClass.SUBJECT_CONTROL_CHILD,
    }

    unscoped = {
        table_name
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        if spec.ownership in subject_classes and spec.subject is TargetColumn.NONE
    }
    assert unscoped == set()


def test_control_plane_and_live_links_are_not_user_portable():
    expected = {
        "ai_invocations",
        "ai_platform_quota_periods",
        "ai_subject_quota_periods",
        "audit_events",
        "care_relationships",
        "consent_grants",
        "consent_scopes",
        "file_assets",
        "health_subjects",
        "integration_connection_settings",
        "integration_connections",
        "legacy_openrouter_connection_bridges",
        "notification_delivery_intents",
        "notifications",
        "ownership_backfill_checkpoints",
        "platform_integration_connections",
        "platform_settings",
        "professional_invitations",
        "professional_profiles",
        "shared_reports",
        "subject_settings",
        "support_access_grants",
        "support_access_scopes",
        "user_federated_identities",
        "user_roles",
        "user_settings",
        "users",
        "weekly_digests",
    }
    assert {
        table_name
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        if not spec.user_portable
    } == expected


def test_platform_ai_contract_keeps_control_and_subject_use_separate():
    platform = OWNERSHIP_REGISTRY["platform_integration_connections"]
    platform_quota = OWNERSHIP_REGISTRY["ai_platform_quota_periods"]
    subject_quota = OWNERSHIP_REGISTRY["ai_subject_quota_periods"]
    invocation = OWNERSHIP_REGISTRY["ai_invocations"]
    bridge = OWNERSHIP_REGISTRY["legacy_openrouter_connection_bridges"]

    assert platform.ownership is OwnershipClass.PLATFORM_CONTROL
    assert platform.subject is TargetColumn.NONE
    assert platform.platform_connection is TargetColumn.NONE
    assert platform_quota.ownership is OwnershipClass.PLATFORM_CONTROL
    assert platform_quota.subject is TargetColumn.NONE
    assert subject_quota.ownership is OwnershipClass.SUBJECT_CONTROL
    assert subject_quota.subject is TargetColumn.REQUIRED
    assert invocation.ownership is OwnershipClass.SUBJECT_CONTROL
    assert invocation.subject is TargetColumn.REQUIRED
    assert invocation.actor is TargetColumn.OPTIONAL
    assert invocation.platform_connection is TargetColumn.REQUIRED
    assert bridge.ownership is OwnershipClass.PLATFORM_CONTROL_CHILD
    assert bridge.connection is TargetColumn.REQUIRED
    assert bridge.platform_connection is TargetColumn.REQUIRED


def test_notification_delivery_intent_is_nonportable_subject_control():
    intent = OWNERSHIP_REGISTRY["notification_delivery_intents"]

    assert intent.ownership is OwnershipClass.SUBJECT_CONTROL
    assert intent.subject is TargetColumn.REQUIRED
    assert intent.actor is TargetColumn.OPTIONAL
    assert intent.connection is TargetColumn.REQUIRED
    assert intent.user_portable is False


def test_portability_exclusions_follow_the_registry_contract():
    from vitals.services.data_portability_service import _EXCLUDED_TABLES

    expected = {
        table_name
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        if not spec.user_portable
    }
    assert _EXCLUDED_TABLES == expected


# ── The PR-04 contract: the registry and the columns say the same thing ───────

_OWNERSHIP_COLUMNS = (
    ("subject", "subject_id"),
    ("actor", "actor_user_id"),
    ("connection", "integration_connection_id"),
    ("platform_connection", "platform_connection_id"),
    ("file_asset", "file_asset_id"),
)


def _column_nullability() -> dict[tuple[str, str], bool]:
    return {
        (table_name, column.name): column.nullable
        for table_name, table in Base.metadata.tables.items()
        for column in table.columns
    }


def test_the_pending_contract_set_is_exactly_what_is_still_nullable():
    """The Stage-6 ratchet: ``REQUIRED`` columns the schema does not yet enforce.

    The registry has always described the *target* contract, and PR-03 added
    every one of these columns nullable so the expansion could ship without a
    write failing. Closing the gap is the contract migration's job, and it is
    blocked on the last legacy writers — the unscoped Garmin ingest and the
    unowned raw upsert still create rows without the reference.

    Recomputing the set here means it can only move one way. A table that gains
    its ``NOT NULL`` fails until the entry is removed; one that quietly loses it
    fails immediately. Reaching empty is the condition for writing the migration.
    """

    nullable = _column_nullability()
    observed = {
        (table_name, column_name)
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        for field, column_name in _OWNERSHIP_COLUMNS
        if getattr(spec, field) is TargetColumn.REQUIRED
        and nullable.get((table_name, column_name)) is True
    }
    assert observed == set(PENDING_OWNERSHIP_CONTRACT_COLUMNS), {
        "closed": sorted(set(PENDING_OWNERSHIP_CONTRACT_COLUMNS) - observed),
        "reopened": sorted(observed - set(PENDING_OWNERSHIP_CONTRACT_COLUMNS)),
    }


def test_a_required_reference_outside_the_pending_set_is_enforced():
    """Whatever has already left the pending set must be enforced, not merely gone.

    Removing an entry is how progress is recorded, so the removal has to be
    backed by an actual ``NOT NULL`` — otherwise the ratchet could be advanced
    by editing one line.
    """

    nullable = _column_nullability()
    for table_name, spec in sorted(OWNERSHIP_REGISTRY.items()):
        for field, column_name in _OWNERSHIP_COLUMNS:
            if getattr(spec, field) is not TargetColumn.REQUIRED:
                continue
            if (table_name, column_name) in PENDING_OWNERSHIP_CONTRACT_COLUMNS:
                continue
            if (table_name, column_name) not in nullable:
                continue
            assert nullable[(table_name, column_name)] is False, (
                f"{table_name}.{column_name} left the pending set without "
                "becoming NOT NULL"
            )


def test_a_reference_that_is_not_required_stays_nullable():
    """The converse, so the mixins cannot drift the other way.

    ``MIXED``, ``OPTIONAL`` and ``INHERITED`` all describe references that are
    legitimately absent on some rows — a curated catalog entry belongs to
    nobody, a platform alert to no patient, a hand-typed fact to no integration.
    A ``NOT NULL`` there would reject data the product is supposed to hold.
    """

    nullable = _column_nullability()
    offenders = [
        f"{table_name}.{column_name} ({getattr(spec, field).value})"
        for table_name, spec in sorted(OWNERSHIP_REGISTRY.items())
        for field, column_name in _OWNERSHIP_COLUMNS
        if getattr(spec, field)
        in (TargetColumn.MIXED, TargetColumn.OPTIONAL, TargetColumn.INHERITED)
        and nullable.get((table_name, column_name)) is False
    ]
    assert not offenders, (
        f"not required by the registry but NOT NULL in the model: {offenders}"
    )
