"""Contract tests for the complete subject-ownership inventory."""

from __future__ import annotations

import pytest

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.models.base import Base
from vitals.ownership import (
    OWNERSHIP_REGISTRY,
    OwnershipClass,
    TargetColumn,
    ownership_for,
)


def test_every_registered_table_has_exactly_one_ownership_contract():
    assert set(OWNERSHIP_REGISTRY) == set(Base.metadata.tables)
    assert len(OWNERSHIP_REGISTRY) == 49


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
        "audit_events",
        "health_subjects",
        "shared_reports",
        "support_access_grants",
        "support_access_scopes",
        "user_roles",
        "users",
    }
    assert {
        table_name
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        if not spec.user_portable
    } == expected


def test_portability_exclusions_follow_the_registry_contract():
    from vitals.services.data_portability_service import _EXCLUDED_TABLES

    expected = {
        table_name
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        if not spec.user_portable
    }
    assert _EXCLUDED_TABLES == expected
