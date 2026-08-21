"""Schema contract for the bounded PR-03 child ownership expansion."""
from __future__ import annotations

import pytest
from sqlalchemy import UniqueConstraint, Uuid

from vitals.models.body_scan import BodyScanMetric
from vitals.models.hevy import HevyExercise, HevySet
from vitals.models.hrt import (
    HrtCompoundComponent,
    HrtCycleItem,
    HrtCycleTemplateItem,
)

_OWNERSHIP_TARGETS = {
    "subject_id": "health_subjects.id",
    "actor_user_id": "users.id",
    "integration_connection_id": "integration_connections.id",
    "file_asset_id": "file_assets.id",
}

_MODEL_OWNERSHIP_COLUMNS = {
    BodyScanMetric: {"subject_id"},
    HevyExercise: {"subject_id", "integration_connection_id"},
    HevySet: {"subject_id", "integration_connection_id"},
    HrtCompoundComponent: {"subject_id"},
    HrtCycleItem: {"subject_id"},
    HrtCycleTemplateItem: {"subject_id"},
}

_PARENT_FOREIGN_KEYS = {
    BodyScanMetric: ("scan_id", "body_scans.id", "CASCADE"),
    HevyExercise: ("workout_id", "hevy_workouts.id", "CASCADE"),
    HevySet: ("exercise_id", "hevy_exercises.id", "CASCADE"),
    HrtCompoundComponent: ("compound_id", "hrt_compounds.id", "CASCADE"),
    HrtCycleItem: ("cycle_id", "hrt_cycles.id", "CASCADE"),
    HrtCycleTemplateItem: (
        "template_id",
        "hrt_cycle_templates.id",
        "CASCADE",
    ),
}

_LEGACY_INDEXES = {
    BodyScanMetric: {
        "ix_body_scan_metrics_scan": ("scan_id",),
        "ix_body_scan_metrics_key": ("metric_key",),
    },
    HevyExercise: {
        "ix_hevy_exercises_template": ("exercise_template_id",),
        "ix_hevy_exercises_workout": ("workout_id",),
    },
    HevySet: {"ix_hevy_sets_exercise": ("exercise_id",)},
    HrtCompoundComponent: {
        "ix_hrt_compound_components_compound": ("compound_id",),
    },
    HrtCycleItem: {"ix_hrt_cycle_items_cycle": ("cycle_id",)},
    HrtCycleTemplateItem: {
        "ix_hrt_cycle_template_items_template": ("template_id",),
    },
}


# The exact parent each child's ``subject_id`` must equal after Stage 4.
_SUBJECT_EQUALITY_TARGETS = {
    BodyScanMetric: "body_scans.subject_id",
    HevyExercise: "hevy_workouts.subject_id",
    HevySet: "hevy_exercises.subject_id",
    HrtCompoundComponent: "hrt_compounds.subject_id",
    HrtCycleItem: "hrt_cycles.subject_id",
    HrtCycleTemplateItem: "hrt_cycle_templates.subject_id",
}


@pytest.mark.parametrize(
    ("model", "expected_columns"),
    _MODEL_OWNERSHIP_COLUMNS.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_child_models_have_exact_nullable_ownership_foreign_keys(
    model, expected_columns
):
    table = model.__table__
    actual_columns = set(table.columns.keys()).intersection(_OWNERSHIP_TARGETS)
    assert actual_columns == expected_columns

    indexes = {index.name: index for index in table.indexes}
    for column_name in expected_columns:
        column = table.columns[column_name]
        assert isinstance(column.type, Uuid)
        assert column.type.as_uuid is True
        assert column.nullable is True
        assert column.default is None
        assert column.server_default is None
        # ``subject_id`` additionally carries the Stage-4 subject-equality
        # reference to the owning parent, which cascades with that parent
        # exactly like the plain parent link it doubles.
        ownership_keys = {
            foreign_key.target_fullname: foreign_key.ondelete
            for foreign_key in column.foreign_keys
        }
        expected = {_OWNERSHIP_TARGETS[column_name]: "RESTRICT"}
        equality_target = _SUBJECT_EQUALITY_TARGETS.get(model)
        if column_name == "subject_id" and equality_target is not None:
            expected[equality_target] = "CASCADE"
        assert ownership_keys == expected

        index_name = f"ix_{table.name}_{column_name}"
        assert index_name in indexes
        assert tuple(item.name for item in indexes[index_name].columns) == (
            column_name,
        )
        assert indexes[index_name].unique is False


@pytest.mark.parametrize(
    ("model", "parent_contract"),
    _PARENT_FOREIGN_KEYS.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_child_models_keep_one_unambiguous_simple_parent_fk(model, parent_contract):
    parent_column, parent_target, ondelete = parent_contract
    table = model.__table__
    column = table.columns[parent_column]

    # The plain link plus the Stage-4 subject-equality pair both point at the
    # same parent row and use the same delete rule.
    assert {
        (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in column.foreign_keys
    } == {(parent_target, ondelete)}

    # Exactly two references to the parent: the plain link the ORM navigates and
    # the Stage-4 subject-equality pair that makes a cross-subject child
    # impossible.
    parent_constraints = sorted(
        (
            tuple(item.name for item in constraint.columns)
            for constraint in table.foreign_key_constraints
            if any(
                element.target_fullname.split(".")[0]
                == parent_target.split(".")[0]
                for element in constraint.elements
            )
        ),
        key=len,
    )
    assert parent_constraints == [
        (parent_column,),
        (parent_column, "subject_id"),
    ]


@pytest.mark.parametrize(
    ("model", "legacy_indexes"),
    _LEGACY_INDEXES.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_child_models_keep_exact_legacy_and_ownership_indexes(
    model, legacy_indexes
):
    expected_indexes = dict(legacy_indexes)
    for column_name in _MODEL_OWNERSHIP_COLUMNS[model]:
        expected_indexes[f"ix_{model.__tablename__}_{column_name}"] = (column_name,)

    actual_indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in model.__table__.indexes
    }
    assert actual_indexes == expected_indexes
    assert all(index.unique is False for index in model.__table__.indexes)


def test_hevy_exercise_parent_subject_unique_is_exact():
    uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in HevyExercise.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert uniques == {"uq_hevy_exercises_id_subject": ("id", "subject_id")}


@pytest.mark.parametrize(
    "model",
    (
        BodyScanMetric,
        HevySet,
        HrtCompoundComponent,
        HrtCycleItem,
        HrtCycleTemplateItem,
    ),
    ids=lambda model: model.__name__,
)
def test_other_child_models_do_not_gain_parent_subject_unique_early(model):
    assert not any(
        isinstance(constraint, UniqueConstraint)
        for constraint in model.__table__.constraints
    )
