"""Schema contract for the bounded PR-03 special/lifecycle ownership slice."""
from __future__ import annotations

import pytest
from sqlalchemy import UniqueConstraint, Uuid

from vitals.models.proactive import Notification
from vitals.models.raw_payload import RawPayload
from vitals.models.share import SharedReport
from vitals.models.signals import DayContext, Signal
from vitals.models.system_alert import SystemAlert

_COLUMN_TARGETS = {
    "subject_id": "health_subjects.id",
    "actor_user_id": "users.id",
    "integration_connection_id": "integration_connections.id",
    "file_asset_id": "file_assets.id",
    "recipient_user_id": "users.id",
    "created_by_user_id": "users.id",
    "revoked_by_user_id": "users.id",
    "overridden_by_user_id": "users.id",
    "resolved_by_user_id": "users.id",
}

_MULTI_FOREIGN_KEY_TARGETS = {
    (Notification, "subject_id"): {
        "health_subjects.id",
        "ai_invocations.subject_id",
    },
    (SystemAlert, "subject_id"): {
        "health_subjects.id",
        "ai_invocations.subject_id",
    },
}

_MODEL_COLUMNS = {
    RawPayload: {
        "subject_id",
        "actor_user_id",
        "integration_connection_id",
        "file_asset_id",
    },
    Signal: {"subject_id", "actor_user_id", "integration_connection_id"},
    DayContext: {"subject_id", "actor_user_id", "integration_connection_id"},
    Notification: {
        "subject_id",
        "actor_user_id",
        "integration_connection_id",
        "recipient_user_id",
    },
    SharedReport: {
        "subject_id",
        "created_by_user_id",
        "revoked_by_user_id",
    },
    SystemAlert: {
        "subject_id",
        "integration_connection_id",
        "overridden_by_user_id",
        "resolved_by_user_id",
    },
}

_RETAINED_SCHEMA_OBJECTS = {
    RawPayload: {
        "ix_raw_payloads_payload_gin",
        "ix_raw_payloads_domain_source_external",
    },
    Signal: {
        "ix_signals_domain_date",
        "ix_signals_batch",
        "ix_signals_key_date",
    },
    DayContext: {"ix_day_context_domain_date", "uq_day_context_per_date"},
    Notification: {
        "uq_notification_dedupe_key",
        "ix_notifications_category_sent",
        "ix_notifications_external_id",
    },
    SharedReport: {"ix_shared_reports_token"},
    SystemAlert: {
        "uq_active_alert_per_key_entity",
        "ix_system_alerts_domain_resolved",
    },
}

_STAGE1_INDEXES = {
    RawPayload: {
        "ix_raw_payloads_subject_domain_processed": (
            "subject_id",
            "domain",
            "processed_at",
        ),
        "ix_raw_payloads_connection_domain_processed": (
            "integration_connection_id",
            "domain",
            "processed_at",
        ),
        "ix_raw_payloads_subject_domain_source_external": (
            "subject_id",
            "domain",
            "source",
            "external_id",
        ),
        "ix_raw_payloads_connection_domain_source_external": (
            "integration_connection_id",
            "domain",
            "source",
            "external_id",
        ),
    },
    Signal: {
        "ix_signals_subject_date": ("subject_id", "date"),
        "ix_signals_subject_domain_date": ("subject_id", "domain", "date"),
        "ix_signals_subject_batch": ("subject_id", "batch_id"),
        "ix_signals_subject_key_date": ("subject_id", "key", "date"),
        "ix_signals_connection_batch": ("integration_connection_id", "batch_id"),
    },
    DayContext: {
        "ix_day_context_subject_date": ("subject_id", "date"),
        "ix_day_context_subject_domain_date": (
            "subject_id",
            "domain",
            "date",
        ),
    },
    Notification: {
        "ix_notifications_subject_sent": ("subject_id", "sent_at"),
        "ix_notifications_recipient_subject_category_sent": (
            "recipient_user_id",
            "subject_id",
            "category",
            "sent_at",
        ),
        "ix_notifications_connection_recipient_dedupe": (
            "integration_connection_id",
            "recipient_user_id",
            "dedupe_key",
        ),
        "ix_notifications_connection_recipient_external": (
            "integration_connection_id",
            "recipient_user_id",
            "external_id",
        ),
    },
    SharedReport: {
        "ix_shared_reports_subject_created": ("subject_id", "created_at"),
    },
    SystemAlert: {
        "ix_system_alerts_subject_domain_resolved": (
            "subject_id",
            "domain",
            "resolved_at",
        ),
        "ix_system_alerts_connection_domain_resolved": (
            "integration_connection_id",
            "domain",
            "resolved_at",
        ),
    },
}


@pytest.mark.parametrize(
    ("model", "expected_columns"),
    _MODEL_COLUMNS.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_special_models_have_exact_nullable_ownership_and_lifecycle_fks(
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
            {_COLUMN_TARGETS[column_name]},
        )
        foreign_keys = list(column.foreign_keys)
        assert {
            foreign_key.target_fullname for foreign_key in foreign_keys
        } == expected_targets
        assert all(
            foreign_key.ondelete == "RESTRICT" for foreign_key in foreign_keys
        )

        index_name = f"ix_{table.name}_{column_name}"
        assert index_name in indexes
        assert [item.name for item in indexes[index_name].columns] == [column_name]
        assert indexes[index_name].unique is False


@pytest.mark.parametrize(
    "model",
    (Signal, DayContext),
    ids=lambda model: model.__name__,
)
def test_special_insights_indexes_survive_ownership_mixins(model):
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
def test_special_existing_table_args_survive_new_columns(model, expected_names):
    table = model.__table__
    actual_names = {
        item.name for item in (*table.indexes, *table.constraints) if item.name
    }

    assert expected_names <= actual_names


@pytest.mark.parametrize(
    ("model", "expected_indexes"),
    _STAGE1_INDEXES.items(),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_special_stage1_supporting_indexes_are_exact(model, expected_indexes):
    indexes = {index.name: index for index in model.__table__.indexes}

    for index_name, expected_columns in expected_indexes.items():
        assert index_name in indexes
        index = indexes[index_name]
        assert tuple(column.name for column in index.columns) == expected_columns
        assert index.unique is False


def test_raw_payload_parent_subject_unique_is_exact():
    constraint = next(
        item
        for item in RawPayload.__table__.constraints
        if item.name == "uq_raw_payloads_id_subject"
    )

    assert isinstance(constraint, UniqueConstraint)
    assert tuple(column.name for column in constraint.columns) == (
        "id",
        "subject_id",
    )


def test_special_lifecycle_models_do_not_gain_generic_actor_column():
    assert "actor_user_id" not in SharedReport.__table__.columns
    assert "actor_user_id" not in SystemAlert.__table__.columns


def test_special_partial_indexes_keep_both_dialect_predicates():
    for model, index_name in (
        (Notification, "uq_notification_dedupe_key"),
        (SystemAlert, "uq_active_alert_per_key_entity"),
    ):
        index = next(
            item for item in model.__table__.indexes if item.name == index_name
        )
        assert index.unique is True
        assert index.dialect_options["postgresql"]["where"] is not None
        assert index.dialect_options["sqlite"]["where"] is not None


def test_raw_payload_gin_index_keeps_postgresql_method():
    index = next(
        item
        for item in RawPayload.__table__.indexes
        if item.name == "ix_raw_payloads_payload_gin"
    )

    assert index.dialect_options["postgresql"]["using"] == "gin"
