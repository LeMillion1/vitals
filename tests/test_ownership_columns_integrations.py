"""Schema contract for the bounded PR-03 provider ownership expansion."""
from __future__ import annotations

import pytest

from vitals.scoped_keys import scoped_keys_for
from sqlalchemy import CheckConstraint, UniqueConstraint, Uuid

from vitals.models.garmin import (
    GarminActivity,
    GarminDaily,
    GarminIntraday,
    GarminWeightExport,
)
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout

_COLUMN_TARGETS = {
    "subject_id": "health_subjects.id",
    "actor_user_id": "users.id",
    "integration_connection_id": "integration_connections.id",
    "requested_by_user_id": "users.id",
}

_MODEL_COLUMNS = {
    GarminDaily: {"subject_id", "actor_user_id", "integration_connection_id"},
    GarminActivity: {
        "subject_id",
        "actor_user_id",
        "integration_connection_id",
    },
    GarminIntraday: {"subject_id", "integration_connection_id"},
    GarminWeightExport: {
        "subject_id",
        "integration_connection_id",
        "requested_by_user_id",
    },
    HevyWorkout: {"subject_id", "actor_user_id", "integration_connection_id"},
    HevyExercise: {"subject_id", "integration_connection_id"},
    HevySet: {"subject_id", "integration_connection_id"},
}

_LEGACY_INDEX_COLUMNS = {
    GarminDaily: {
        "ix_garmin_daily_date": ("date",),
        "ix_garmin_daily_domain": ("domain",),
        "ix_garmin_daily_domain_date": ("domain", "date"),
        "ix_garmin_daily_subject_date": ("subject_id", "date"),
        "ix_garmin_daily_subject_domain_date": (
            "subject_id",
            "domain",
            "date",
        ),
        "ix_garmin_daily_connection_date": (
            "integration_connection_id",
            "date",
        ),
    },
    GarminActivity: {
        "ix_garmin_activities_date": ("date",),
        "ix_garmin_activities_domain": ("domain",),
        "ix_garmin_activities_domain_date": ("domain", "date"),
        "ix_garmin_activities_subject_date": ("subject_id", "date"),
        "ix_garmin_activities_subject_domain_date": (
            "subject_id",
            "domain",
            "date",
        ),
        "ix_garmin_activities_connection_external": (
            "integration_connection_id",
            "external_id",
        ),
    },
    GarminIntraday: {
        "ix_garmin_intraday_date": ("date",),
        "ix_garmin_intraday_domain": ("domain",),
        "ix_garmin_intraday_domain_date": ("domain", "date"),
        "ix_garmin_intraday_series_date": ("series_type", "date"),
        "ix_garmin_intraday_date_ts": ("date", "ts"),
        "ix_garmin_intraday_subject_date": ("subject_id", "date"),
        "ix_garmin_intraday_subject_domain_date": (
            "subject_id",
            "domain",
            "date",
        ),
        "ix_garmin_intraday_connection_series_date": (
            "integration_connection_id",
            "series_type",
            "date",
        ),
        "ix_garmin_intraday_connection_date_ts": (
            "integration_connection_id",
            "date",
            "ts",
        ),
    },
    GarminWeightExport: {
        "ix_garmin_weight_exports_status_next": ("status", "next_attempt_at"),
        "ix_garmin_weight_exports_weight_log_id": ("weight_log_id",),
        "ix_garmin_weight_exports_subject_date": ("subject_id", "date"),
        "ix_garmin_weight_exports_connection_date": (
            "integration_connection_id",
            "date",
        ),
        "ix_garmin_weight_exports_connection_status_next": (
            "integration_connection_id",
            "status",
            "next_attempt_at",
        ),
    },
    HevyWorkout: {
        "ix_hevy_workouts_date": ("date",),
        "ix_hevy_workouts_domain": ("domain",),
        "ix_hevy_workouts_domain_date": ("domain", "date"),
        "ix_hevy_workouts_subject_date": ("subject_id", "date"),
        "ix_hevy_workouts_subject_domain_date": (
            "subject_id",
            "domain",
            "date",
        ),
        "ix_hevy_workouts_connection_external": (
            "integration_connection_id",
            "external_id",
        ),
    },
    HevyExercise: {
        "ix_hevy_exercises_template": ("exercise_template_id",),
        "ix_hevy_exercises_workout": ("workout_id",),
    },
    HevySet: {"ix_hevy_sets_exercise": ("exercise_id",)},
}

_LEGACY_UNIQUE_COLUMNS = {
    GarminDaily: {"uq_garmin_daily_date": ("date",)},
    GarminActivity: {
        "uq_garmin_activities_external_id": ("external_id",),
    },
    GarminWeightExport: {"uq_garmin_weight_exports_date": ("date",)},
    HevyWorkout: {
        "uq_hevy_workouts_external_id": ("external_id",),
        "uq_hevy_workouts_id_subject": ("id", "subject_id"),
    },
}


_SUBJECT_EQUALITY_TARGETS = {
    HevyExercise: "hevy_workouts.subject_id",
    HevySet: "hevy_exercises.subject_id",
}


@pytest.mark.parametrize(
    ("model", "expected_columns"),
    _MODEL_COLUMNS.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_provider_roots_have_exact_nullable_ownership_foreign_keys(
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
        assert column.default is None
        assert column.server_default is None
        # An inherited child's ``subject_id`` also carries the Stage-4
        # subject-equality reference to its owning parent.
        expected = {_COLUMN_TARGETS[column_name]: "RESTRICT"}
        equality_target = _SUBJECT_EQUALITY_TARGETS.get(model)
        if column_name == "subject_id" and equality_target is not None:
            expected[equality_target] = "CASCADE"
        assert {
            foreign_key.target_fullname: foreign_key.ondelete
            for foreign_key in column.foreign_keys
        } == expected

        index_name = f"ix_{table.name}_{column_name}"
        assert index_name in indexes
        assert tuple(item.name for item in indexes[index_name].columns) == (
            column_name,
        )
        assert indexes[index_name].unique is False


def test_provider_models_without_human_actor_keep_that_boundary_explicit():
    assert "actor_user_id" not in GarminIntraday.__table__.columns
    assert "actor_user_id" not in GarminWeightExport.__table__.columns
    assert "requested_by_user_id" not in GarminIntraday.__table__.columns


@pytest.mark.parametrize(
    ("model", "legacy_indexes"),
    _LEGACY_INDEX_COLUMNS.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_provider_existing_indexes_survive_with_exact_columns(
    model, legacy_indexes
):
    expected = dict(legacy_indexes)
    for column_name in _MODEL_COLUMNS.get(model, ()):
        expected[f"ix_{model.__tablename__}_{column_name}"] = (column_name,)
    # The Stage-5 cutover adds the connection-scoped replacement for the
    # provider's legacy global key beside it; nothing else changes.
    for spec in scoped_keys_for(model.__tablename__):
        for index in spec.replacements:
            expected[index.name] = index.columns

    actual = {
        index.name: tuple(column.name for column in index.columns)
        for index in model.__table__.indexes
    }
    assert actual == expected
    # Expansion added no uniqueness of its own; the Stage-5 cutover replaces the
    # provider's global key with the scoped one, and nothing else.
    scoped = {
        index.name
        for spec in scoped_keys_for(model.__tablename__)
        for index in spec.replacements
    }
    assert all(
        index.unique is (index.name in scoped)
        for index in model.__table__.indexes
    )


@pytest.mark.parametrize(
    ("model", "expected_uniques"),
    _LEGACY_UNIQUE_COLUMNS.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_provider_existing_unique_constraints_survive(model, expected_uniques):
    actual = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert actual == expected_uniques


def test_garmin_weight_export_positive_check_survives():
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in GarminWeightExport.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert checks == {
        "ck_garmin_weight_exports_weight_positive": "weight_kg > 0"
    }
