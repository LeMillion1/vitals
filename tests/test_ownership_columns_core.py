"""Schema contract for the bounded PR-03 core ownership expansion."""
from __future__ import annotations

import pytest
from sqlalchemy import Uuid

from vitals.models.genetics import GeneticVariant
from vitals.models.glp1 import DosePhase, Injection, SideEffect
from vitals.models.nutrition import MealLog
from vitals.models.skincare import (
    SkincareLog,
    SkincareObservation,
    SkincareProduct,
)
from vitals.models.supplements import Supplement
from vitals.models.timeline import Annotation
from vitals.models.weight import (
    BodyMeasurement,
    NoiseMarker,
    ProgressPhoto,
    WeightLog,
)

_COLUMN_TARGETS = {
    "subject_id": "health_subjects.id",
    "actor_user_id": "users.id",
    "integration_connection_id": "integration_connections.id",
    "file_asset_id": "file_assets.id",
}

_MODEL_COLUMNS = {
    WeightLog: {"subject_id", "actor_user_id", "integration_connection_id"},
    BodyMeasurement: {"subject_id", "actor_user_id"},
    ProgressPhoto: {"subject_id", "actor_user_id", "file_asset_id"},
    NoiseMarker: {"subject_id", "actor_user_id"},
    Injection: {"subject_id", "actor_user_id"},
    DosePhase: {"subject_id", "actor_user_id"},
    SideEffect: {"subject_id", "actor_user_id"},
    Supplement: {"subject_id", "actor_user_id"},
    GeneticVariant: {"subject_id", "actor_user_id"},
    SkincareLog: {"subject_id", "actor_user_id"},
    SkincareObservation: {"subject_id", "actor_user_id"},
    SkincareProduct: {"subject_id", "actor_user_id"},
    MealLog: {"subject_id", "actor_user_id"},
    Annotation: {"subject_id", "actor_user_id"},
}

_INSIGHTS_MODELS = (
    WeightLog,
    BodyMeasurement,
    ProgressPhoto,
    Injection,
    SideEffect,
    SkincareLog,
    SkincareObservation,
    MealLog,
    Annotation,
)

_RETAINED_SCHEMA_OBJECTS = {
    WeightLog: {
        "ix_weight_logs_domain_date",
        "uq_active_weight_per_subject_date",
        "ck_weight_logs_weight_positive",
    },
    BodyMeasurement: {
        "ix_body_measurements_domain_date",
        "uq_body_measurements_subject_date",
    },
    ProgressPhoto: {"ix_progress_photos_domain_date"},
    NoiseMarker: {"ix_noise_markers_domain_range"},
    Injection: {
        "ix_glp1_injections_domain_date",
        "ck_glp1_injections_dose_positive",
    },
    DosePhase: {
        "ix_glp1_dose_phases_range",
        "ck_glp1_dose_phases_dose_positive",
    },
    SideEffect: {
        "ix_glp1_side_effects_domain_date",
        "ck_glp1_side_effects_severity_range",
    },
    Supplement: {"ix_supplements_key", "ix_supplements_active"},
    GeneticVariant: {
        "ix_genetic_variants_marker",
        "uq_genetic_variant_subject_rsid",
    },
    SkincareLog: {"ix_skincare_logs_domain_date"},
    SkincareObservation: {"ix_skincare_observations_domain_date"},
    SkincareProduct: set(),
    MealLog: {
        "ix_meal_logs_domain_date",
        "ck_meal_logs_calories_nonneg",
    },
    Annotation: {
        "ix_annotations_domain_date",
        "ix_annotations_date_range",
    },
}


@pytest.mark.parametrize(
    ("model", "expected_columns"),
    _MODEL_COLUMNS.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_core_models_have_exact_nullable_ownership_foreign_keys(
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
        assert len(column.foreign_keys) == 1
        foreign_key = next(iter(column.foreign_keys))
        assert foreign_key.target_fullname == _COLUMN_TARGETS[column_name]
        assert foreign_key.ondelete == "RESTRICT"

        index_name = f"ix_{table.name}_{column_name}"
        assert index_name in indexes
        assert [item.name for item in indexes[index_name].columns] == [column_name]
        assert indexes[index_name].unique is False


@pytest.mark.parametrize("model", _INSIGHTS_MODELS, ids=lambda model: model.__name__)
def test_core_insights_indexes_survive_ownership_mixins(model):
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
def test_core_existing_table_args_survive_ownership_mixins(model, expected_names):
    table = model.__table__
    actual_names = {
        item.name for item in (*table.indexes, *table.constraints) if item.name
    }

    assert expected_names <= actual_names


def test_core_partial_unique_indexes_keep_both_dialect_predicates():
    for model, index_name in (
        (WeightLog, "uq_active_weight_per_subject_date"),
        (GeneticVariant, "uq_genetic_variant_subject_rsid"),
    ):
        index = next(
            item for item in model.__table__.indexes if item.name == index_name
        )
        assert index.unique is True
        assert index.dialect_options["postgresql"]["where"] is not None
        assert index.dialect_options["sqlite"]["where"] is not None
