"""Schema contract for the bounded PR-03 protocols ownership expansion."""
from __future__ import annotations

import pytest
from sqlalchemy import Uuid

from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.conflict_rule import ConflictRule
from vitals.models.hevy import HevyExercise, HevySet
from vitals.models.hrt import (
    HrtCompound,
    HrtCompoundComponent,
    HrtCycle,
    HrtCycleItem,
    HrtCycleTemplate,
    HrtCycleTemplateItem,
    HrtDose,
    HrtSideEffect,
)
from vitals.models.labs import LabMarker, LabResult
from vitals.models.milestones import Milestone, WeeklyDigest

_COLUMN_TARGETS = {
    "subject_id": "health_subjects.id",
    "actor_user_id": "users.id",
    "integration_connection_id": "integration_connections.id",
    "file_asset_id": "file_assets.id",
}

# Each ownership column maps to the exact foreign-key targets it must carry and
# the delete rule each one uses.  A Stage-4 subject-equality reference to the
# owning parent cascades with that parent, exactly like the plain parent link it
# doubles; every other ownership reference restricts.
_MULTI_FOREIGN_KEY_TARGETS = {
    (WeeklyDigest, "subject_id"): {
        "health_subjects.id": "RESTRICT",
        "ai_invocations.subject_id": "RESTRICT",
    },
    (BodyScanMetric, "subject_id"): {
        "health_subjects.id": "RESTRICT",
        "body_scans.subject_id": "CASCADE",
    },
    (HevyExercise, "subject_id"): {
        "health_subjects.id": "RESTRICT",
        "hevy_workouts.subject_id": "CASCADE",
    },
    (HevySet, "subject_id"): {
        "health_subjects.id": "RESTRICT",
        "hevy_exercises.subject_id": "CASCADE",
    },
    (HrtCompoundComponent, "subject_id"): {
        "health_subjects.id": "RESTRICT",
        "hrt_compounds.subject_id": "CASCADE",
    },
    (HrtCycleItem, "subject_id"): {
        "health_subjects.id": "RESTRICT",
        "hrt_cycles.subject_id": "CASCADE",
    },
    (HrtCycleTemplateItem, "subject_id"): {
        "health_subjects.id": "RESTRICT",
        "hrt_cycle_templates.subject_id": "CASCADE",
    },
}

_MODEL_COLUMNS = {
    BodyScan: {"subject_id", "actor_user_id", "file_asset_id"},
    BodyScanMetric: {"subject_id"},
    Milestone: {"subject_id", "actor_user_id"},
    WeeklyDigest: {
        "subject_id",
        "actor_user_id",
        "integration_connection_id",
    },
    LabResult: {"subject_id", "actor_user_id"},
    LabMarker: {"subject_id", "actor_user_id"},
    HrtCompound: {"subject_id", "actor_user_id"},
    HrtCompoundComponent: {"subject_id"},
    HrtDose: {"subject_id", "actor_user_id"},
    HrtSideEffect: {"subject_id", "actor_user_id"},
    HrtCycle: {"subject_id", "actor_user_id"},
    HrtCycleItem: {"subject_id"},
    HrtCycleTemplate: {"subject_id", "actor_user_id"},
    HrtCycleTemplateItem: {"subject_id"},
    ConflictRule: {"subject_id"},
}

_INSIGHTS_MODELS = (
    BodyScan,
    WeeklyDigest,
    LabResult,
    HrtDose,
    HrtSideEffect,
)

_RETAINED_SCHEMA_OBJECTS = {
    BodyScan: {"ix_body_scans_domain_date"},
    BodyScanMetric: {
        "ix_body_scan_metrics_scan",
        "ix_body_scan_metrics_key",
    },
    Milestone: {"ix_milestones_status"},
    WeeklyDigest: {"ix_weekly_digests_domain_date"},
    LabResult: {
        "ix_lab_results_domain_date",
        "ix_lab_results_marker_date",
    },
    LabMarker: {"uq_lab_markers_subject_name"},
    HrtCompound: {
        "uq_hrt_compounds_platform_key",
        "ix_hrt_compounds_active",
        "ix_hrt_compounds_class",
    },
    HrtCompoundComponent: {"ix_hrt_compound_components_compound"},
    HrtDose: {
        "ix_hrt_doses_domain_date",
        "ix_hrt_doses_compound_key",
        "ck_hrt_doses_dose_positive",
    },
    HrtSideEffect: {
        "ix_hrt_side_effects_domain_date",
        "ck_hrt_side_effects_severity_range",
    },
    HrtCycle: {"ix_hrt_cycles_range"},
    HrtCycleItem: {"ix_hrt_cycle_items_cycle"},
    HrtCycleTemplate: {"ix_hrt_cycle_templates_name"},
    HrtCycleTemplateItem: {"ix_hrt_cycle_template_items_template"},
    ConflictRule: {"uq_conflict_rules_platform_code"},
}


@pytest.mark.parametrize(
    ("model", "expected_columns"),
    _MODEL_COLUMNS.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_protocol_models_have_exact_nullable_ownership_foreign_keys(
    model, expected_columns
):
    table = model.__table__
    actual_columns = set(table.columns.keys()).intersection(_COLUMN_TARGETS)
    assert actual_columns == expected_columns

    indexes = {index.name: index for index in table.indexes}
    for column_name in expected_columns:
        column = table.columns[column_name]
        assert isinstance(column.type, Uuid)
        assert column.type.as_uuid is True
        assert column.nullable is True
        expected_targets = _MULTI_FOREIGN_KEY_TARGETS.get(
            (model, column_name),
            {_COLUMN_TARGETS[column_name]: "RESTRICT"},
        )
        foreign_keys = list(column.foreign_keys)
        assert {
            foreign_key.target_fullname: foreign_key.ondelete
            for foreign_key in foreign_keys
        } == expected_targets

        index_name = f"ix_{table.name}_{column_name}"
        assert index_name in indexes
        assert [item.name for item in indexes[index_name].columns] == [column_name]
        assert indexes[index_name].unique is False


@pytest.mark.parametrize("model", _INSIGHTS_MODELS, ids=lambda model: model.__name__)
def test_protocol_insights_indexes_survive_ownership_mixins(model):
    index_name = f"ix_{model.__tablename__}_domain_date"
    indexes = {index.name: index for index in model.__table__.indexes}

    assert index_name in indexes
    assert [column.name for column in indexes[index_name].columns] == [
        "domain",
        "date",
    ]


@pytest.mark.parametrize(
    ("model", "expected_names"),
    _RETAINED_SCHEMA_OBJECTS.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_protocol_existing_table_args_survive_ownership_mixins(
    model, expected_names
):
    table = model.__table__
    actual_names = {
        item.name for item in (*table.indexes, *table.constraints) if item.name
    }

    assert expected_names <= actual_names


def test_curated_conflict_rule_keeps_global_null_subject_default():
    column = ConflictRule.__table__.columns.subject_id

    assert column.nullable is True
    assert column.default is None
    assert column.server_default is None
