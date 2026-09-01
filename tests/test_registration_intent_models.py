"""Database invariants for short-lived, PII-free registration intents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError, IntegrityError

from vitals.enums import RegistrationAccountKind, RegistrationIntentStatus
from vitals.models.registration import RegistrationIntent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _values() -> dict[str, object]:
    return {
        "account_kind": RegistrationAccountKind.MEMBER.value,
        "expires_at": _now() + timedelta(minutes=10),
    }


async def test_intent_schema_is_pii_free_and_has_lifecycle_constraints(db_session):
    connection = await db_session.connection()

    def _schema(sync_connection):
        inspector = inspect(sync_connection)
        return (
            {item["name"] for item in inspector.get_columns("registration_intents")},
            {
                item["name"]
                for item in inspector.get_check_constraints("registration_intents")
            },
            {
                (item["name"], item["unique"], tuple(item["column_names"]))
                for item in inspector.get_indexes("registration_intents")
            },
        )

    columns, checks, indexes = await connection.run_sync(_schema)
    assert columns == {
        "id",
        "account_kind",
        "status",
        "expires_at",
        "consumed_at",
        "expired_at",
        "created_at",
        "updated_at",
    }
    assert checks == {
        "ck_registration_intents_account_kind",
        "ck_registration_intents_expiry",
        "ck_registration_intents_state",
        "ck_registration_intents_status",
    }
    assert indexes == {
        (
            "ix_registration_intents_status_expiry",
            False,
            ("status", "expires_at"),
        )
    }


async def test_pending_intent_receives_database_defaults(db_session):
    intent = RegistrationIntent(**_values())
    db_session.add(intent)
    await db_session.flush()

    assert intent.status == RegistrationIntentStatus.PENDING.value
    assert intent.created_at is not None and intent.updated_at is not None
    assert intent.consumed_at is intent.expired_at is None


@pytest.mark.parametrize(
    "changes",
    [
        {"account_kind": "platform_superadmin"},
        {"status": "unknown"},
        {"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)},
        {"status": RegistrationIntentStatus.CONSUMED.value},
        {"status": RegistrationIntentStatus.EXPIRED.value},
        {
            "status": RegistrationIntentStatus.PENDING.value,
            "consumed_at": _now(),
        },
    ],
)
async def test_intent_rejects_privileged_kind_and_impossible_states(
    db_session, changes
):
    db_session.add(RegistrationIntent(**(_values() | changes)))
    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


@pytest.mark.parametrize(
    ("status", "transition_field"),
    [
        (RegistrationIntentStatus.CONSUMED, "consumed_at"),
        (RegistrationIntentStatus.EXPIRED, "expired_at"),
    ],
)
async def test_each_terminal_intent_shape_is_allowed(
    db_session, status, transition_field
):
    values = _values()
    transition_at = values["expires_at"] + timedelta(seconds=1)
    if status is RegistrationIntentStatus.CONSUMED:
        transition_at = _now() + timedelta(seconds=1)
    values |= {
        "status": status.value,
        transition_field: transition_at,
    }
    intent = RegistrationIntent(**values)
    db_session.add(intent)
    await db_session.flush()
    assert intent.status == status.value
