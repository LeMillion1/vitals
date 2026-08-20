"""0043/0044 durable, non-PHI outbound notification delivery contracts."""
from __future__ import annotations

import importlib
import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint, create_engine, inspect
from sqlalchemy.exc import IntegrityError

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.enums import (
    IntegrationConnectionType,
    IntegrationProvider,
    NotificationDeliveryErrorCode,
    NotificationDeliveryStatus,
    UserStatus,
)
from vitals.models.identity import User
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection

NOW = datetime(2026, 8, 20, 12)
POLICY_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)
POLICY_DATE = date(2026, 8, 20)
DST_POLICY_AT = datetime(
    2026,
    10,
    25,
    3,
    30,
    tzinfo=ZoneInfo("Europe/Chisinau"),
    fold=1,
)
UPGRADE_REFUSAL = (
    "0043 upgrade refused: keyed notifications contain partial ownership roots"
)
UPGRADE_DELIVERY_ROOT_REFUSAL = (
    "0043 upgrade refused: keyed owned notifications contain mismatched "
    "delivery roots"
)
DOWNGRADE_REFUSALS = {
    "intent": (
        "0043 downgrade refused: notification delivery intents contain durable state"
    ),
    "link": (
        "0043 downgrade refused: notifications contain delivery intent provenance"
    ),
    "dedupe": (
        "0043 downgrade refused: scoped notification dedupe keys cannot restore "
        "the global unique index"
    ),
}
RAW_CATEGORY_UNIQUE_INDEX = "uq_notification_delivery_intents_raw_category"
RAW_CATEGORY_UPGRADE_REFUSAL = (
    "0044 upgrade refused: notification delivery intents contain duplicate "
    "raw/category claims"
)


def _columns(constraint) -> tuple[str, ...]:
    return tuple(column.name for column in constraint.columns)


def _named_constraints(table, constraint_type):
    return {
        constraint.name: constraint
        for constraint in table.constraints
        if isinstance(constraint, constraint_type) and constraint.name is not None
    }


def test_intent_model_is_non_phi_and_has_exact_durable_shape():
    table = NotificationDeliveryIntent.__table__
    expected_nullability = {
        "id": False,
        "subject_id": False,
        "recipient_user_id": False,
        "actor_user_id": True,
        "integration_connection_id": False,
        "raw_payload_id": True,
        "ai_invocation_id": True,
        "category": False,
        "channel": False,
        "idempotency_key": False,
        "policy_key": True,
        "policy_at": False,
        "policy_date": False,
        "status": False,
        "lease_token": True,
        "dispatch_started_at": True,
        "completed_at": True,
        "error_code": True,
        "created_at": False,
        "updated_at": False,
    }
    assert {column.name: column.nullable for column in table.columns} == (
        expected_nullability
    )
    assert isinstance(table.c.policy_date.type, sa.Date)
    assert isinstance(table.c.policy_at.type, sa.DateTime)
    assert table.c.policy_at.type.timezone is True
    assert {
        "payload",
        "text",
        "buttons",
        "reply_to",
        "external_id",
        "request_body",
        "response_body",
        "error_detail",
    }.isdisjoint(table.columns)

    uniques = _named_constraints(table, UniqueConstraint)
    assert _columns(uniques["uq_notification_delivery_intents_id_subject"]) == (
        "id",
        "subject_id",
    )
    assert _columns(uniques["uq_notification_delivery_intents_delivery_graph"]) == (
        "id",
        "subject_id",
        "recipient_user_id",
        "integration_connection_id",
        "category",
        "channel",
        "idempotency_key",
    )
    assert _columns(
        uniques[
            "uq_notification_delivery_intents_subject_recipient_idempotency"
        ]
    ) == ("subject_id", "recipient_user_id", "idempotency_key")
    assert _columns(
        uniques["uq_notification_delivery_intents_ai_invocation_id"]
    ) == ("ai_invocation_id",)

    foreign_keys = _named_constraints(table, ForeignKeyConstraint)
    expected_composites = {
        "fk_notification_delivery_intents_connection_subject": (
            ("integration_connection_id", "subject_id"),
            ("integration_connections.id", "integration_connections.subject_id"),
        ),
        "fk_notification_delivery_intents_raw_subject": (
            ("raw_payload_id", "subject_id"),
            ("raw_payloads.id", "raw_payloads.subject_id"),
        ),
        "fk_notification_delivery_intents_ai_invocation_subject": (
            ("ai_invocation_id", "subject_id"),
            ("ai_invocations.id", "ai_invocations.subject_id"),
        ),
    }
    for name, (local, remote) in expected_composites.items():
        constraint = foreign_keys[name]
        assert _columns(constraint) == local
        assert tuple(element.target_fullname for element in constraint.elements) == remote
        assert constraint.ondelete == "RESTRICT"

    direct_targets = {
        "subject_id": "health_subjects.id",
        "recipient_user_id": "users.id",
        "actor_user_id": "users.id",
    }
    for column_name, target in direct_targets.items():
        targets = {
            foreign_key.target_fullname
            for foreign_key in table.c[column_name].foreign_keys
        }
        assert target in targets
        assert all(
            foreign_key.ondelete == "RESTRICT"
            for foreign_key in table.c[column_name].foreign_keys
        )

    indexes_by_name = {index.name: index for index in table.indexes}
    indexes = {name: _columns(index) for name, index in indexes_by_name.items()}
    assert indexes == {
        "ix_notification_delivery_intents_status_updated": (
            "status",
            "updated_at",
            "id",
        ),
        "ix_notification_delivery_intents_subject_status_created": (
            "subject_id",
            "status",
            "created_at",
        ),
        "ix_notification_delivery_intents_connection_status_created": (
            "integration_connection_id",
            "status",
            "created_at",
        ),
        "ix_notification_delivery_intents_raw_category_created": (
            "raw_payload_id",
            "category",
            "created_at",
        ),
        RAW_CATEGORY_UNIQUE_INDEX: (
            "raw_payload_id",
            "category",
        ),
        "ix_notification_delivery_intents_recipient_created": (
            "recipient_user_id",
            "created_at",
        ),
        "ix_notification_delivery_intents_budget": (
            "subject_id",
            "recipient_user_id",
            "policy_date",
            "status",
            "category",
        ),
        "ix_notification_delivery_intents_policy": (
            "subject_id",
            "recipient_user_id",
            "policy_key",
            "status",
            "policy_at",
        ),
    }
    assert indexes_by_name[RAW_CATEGORY_UNIQUE_INDEX].unique is True


def test_intent_checks_are_allowlisted_without_owner_as_recipient_constraint():
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in NotificationDeliveryIntent.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert set(checks) == {
        "ck_notification_delivery_intents_channel",
        "ck_notification_delivery_intents_category",
        "ck_notification_delivery_intents_raw_category",
        "ck_notification_delivery_intents_ai_provenance",
        "ck_notification_delivery_intents_idempotency_key_opaque",
        "ck_notification_delivery_intents_policy_key_category",
        "ck_notification_delivery_intents_policy_key_opaque",
        "ck_notification_delivery_intents_status",
        "ck_notification_delivery_intents_lifecycle",
        "ck_notification_delivery_intents_timestamp_order",
        "ck_notification_delivery_intents_error_state",
    }
    assert "channel = 'telegram'" in checks[
        "ck_notification_delivery_intents_channel"
    ]
    assert "length(idempotency_key) = 64" in checks[
        "ck_notification_delivery_intents_idempotency_key_opaque"
    ]
    assert "category = 'nudge' AND policy_key IS NOT NULL" in checks[
        "ck_notification_delivery_intents_policy_key_category"
    ]
    assert "category <> 'nudge' AND policy_key IS NULL" in checks[
        "ck_notification_delivery_intents_policy_key_category"
    ]
    assert "length(policy_key) = 64" in checks[
        "ck_notification_delivery_intents_policy_key_opaque"
    ]
    assert all(
        "actor_user_id = recipient_user_id" not in sql for sql in checks.values()
    )
    assert {member.value for member in NotificationDeliveryStatus} == {
        "pending",
        "dispatching",
        "sent",
        "ambiguous",
        "cancelled",
    }
    assert {member.value for member in NotificationDeliveryErrorCode} == {
        "transport_error",
        "invalid_response",
        "stale_dispatch",
        "cancelled_by_policy",
        "stale_pending",
        "scope_invalid",
        "internal_error",
    }


def test_notification_link_and_dedupe_roots_are_exact():
    table = Notification.__table__
    foreign_keys = _named_constraints(table, ForeignKeyConstraint)
    link = foreign_keys["fk_notifications_delivery_intent_subject"]
    assert _columns(link) == (
        "delivery_intent_id",
        "subject_id",
        "recipient_user_id",
        "integration_connection_id",
        "category",
        "channel",
        "dedupe_key",
    )
    assert tuple(element.target_fullname for element in link.elements) == (
        "notification_delivery_intents.id",
        "notification_delivery_intents.subject_id",
        "notification_delivery_intents.recipient_user_id",
        "notification_delivery_intents.integration_connection_id",
        "notification_delivery_intents.category",
        "notification_delivery_intents.channel",
        "notification_delivery_intents.idempotency_key",
    )
    assert link.ondelete == "RESTRICT"

    uniques = _named_constraints(table, UniqueConstraint)
    assert _columns(uniques["uq_notifications_delivery_intent_id"]) == (
        "delivery_intent_id",
    )
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert "ck_notifications_delivery_intent_scope" in checks
    assert "ck_notifications_dedupe_root_shape" in checks
    intent_scope = checks["ck_notifications_delivery_intent_scope"]
    assert "external_id IS NOT NULL" in intent_scope
    assert "length(trim(external_id)) > 0" in intent_scope
    root_shape = checks["ck_notifications_dedupe_root_shape"]
    assert "dedupe_key IS NULL" in root_shape
    assert "subject_id IS NOT NULL" in root_shape
    assert "recipient_user_id IS NOT NULL" in root_shape
    assert "integration_connection_id IS NOT NULL" in root_shape
    assert "subject_id IS NULL" in root_shape
    assert "actor_user_id IS NULL" in root_shape

    indexes = {index.name: index for index in table.indexes}
    assert "uq_notification_dedupe_key" not in indexes
    assert "ix_notifications_delivery_intent_id" not in indexes
    for name, columns in {
        "uq_notifications_owned_dedupe_key": (
            "subject_id",
            "recipient_user_id",
            "dedupe_key",
        ),
        "uq_notifications_legacy_dedupe_key": ("dedupe_key",),
    }.items():
        index = indexes[name]
        assert index.unique is True
        assert _columns(index) == columns
        assert index.dialect_options["postgresql"]["where"] is not None
        assert index.dialect_options["sqlite"]["where"] is not None


async def _telegram_connection_id(session, subject_id: uuid.UUID) -> uuid.UUID:
    connection_id = await session.scalar(
        sa.select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.RECIPIENT.value,
        )
    )
    assert connection_id is not None
    return connection_id


def _intent(roots, connection_id: uuid.UUID, **overrides):
    values = {
        "subject_id": roots.subject_id,
        "recipient_user_id": roots.user_id,
        "actor_user_id": roots.user_id,
        "integration_connection_id": connection_id,
        "category": "brief",
        "channel": "telegram",
        "idempotency_key": uuid.uuid4().hex * 2,
        "policy_at": POLICY_AT,
        "policy_date": POLICY_DATE,
        "status": NotificationDeliveryStatus.PENDING.value,
    }
    values.update(overrides)
    return NotificationDeliveryIntent(**values)


@pytest.mark.asyncio
async def test_actor_may_differ_from_recipient_and_lifecycle_is_enforced(
    db_session,
    legacy_owner_roots,
):
    if db_session.get_bind().dialect.name == "sqlite":
        await db_session.execute(sa.text("PRAGMA foreign_keys=ON"))
    actor = User(
        username="authorized professional",
        normalized_username="authorized-professional",
        password_hash="synthetic-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(actor)
    await db_session.flush()
    connection_id = await _telegram_connection_id(
        db_session, legacy_owner_roots.subject_id
    )
    row = _intent(
        legacy_owner_roots,
        connection_id,
        actor_user_id=actor.id,
    )
    db_session.add(row)
    await db_session.commit()
    assert row.actor_user_id == actor.id
    assert row.actor_user_id != row.recipient_user_id

    malformed = _intent(
        legacy_owner_roots,
        connection_id,
        status=NotificationDeliveryStatus.DISPATCHING.value,
    )
    db_session.add(malformed)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    missing_nudge_policy = _intent(
        legacy_owner_roots,
        connection_id,
        category="nudge",
    )
    db_session.add(missing_nudge_policy)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_notification_link_rejects_mismatched_transport_graph(
    db_session,
    legacy_owner_roots,
):
    if db_session.get_bind().dialect.name == "sqlite":
        await db_session.execute(sa.text("PRAGMA foreign_keys=ON"))
    connection_id = await _telegram_connection_id(
        db_session, legacy_owner_roots.subject_id
    )
    intent = _intent(legacy_owner_roots, connection_id)
    db_session.add(intent)
    await db_session.commit()

    row = Notification(
        delivery_intent_id=intent.id,
        subject_id=intent.subject_id,
        actor_user_id=intent.actor_user_id,
        recipient_user_id=intent.recipient_user_id,
        integration_connection_id=intent.integration_connection_id,
        sent_at=NOW,
        category=intent.category,
        channel=intent.channel,
        dedupe_key="f" * 64,
        external_id="101",
        payload={"synthetic": True},
    )
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_notification_link_accepts_exact_sent_transport_graph(
    db_session,
    legacy_owner_roots,
):
    if db_session.get_bind().dialect.name == "sqlite":
        await db_session.execute(sa.text("PRAGMA foreign_keys=ON"))
    connection_id = await _telegram_connection_id(
        db_session, legacy_owner_roots.subject_id
    )
    key = uuid.uuid4().hex * 2
    intent = _intent(
        legacy_owner_roots,
        connection_id,
        idempotency_key=key,
        status=NotificationDeliveryStatus.SENT.value,
        lease_token=uuid.uuid4(),
        dispatch_started_at=POLICY_AT,
        completed_at=POLICY_AT,
    )
    db_session.add(intent)
    await db_session.commit()

    row = Notification(
        delivery_intent_id=intent.id,
        subject_id=intent.subject_id,
        actor_user_id=intent.actor_user_id,
        recipient_user_id=intent.recipient_user_id,
        integration_connection_id=intent.integration_connection_id,
        sent_at=NOW,
        category=intent.category,
        channel=intent.channel,
        dedupe_key=intent.idempotency_key,
        external_id="101",
        payload={"synthetic": True},
    )
    db_session.add(row)
    await db_session.commit()

    assert row.delivery_intent_id == intent.id


@pytest.mark.asyncio
@pytest.mark.parametrize("external_id", (None, "", "   "))
async def test_notification_link_requires_nonblank_provider_message_id(
    db_session,
    legacy_owner_roots,
    external_id,
):
    if db_session.get_bind().dialect.name == "sqlite":
        await db_session.execute(sa.text("PRAGMA foreign_keys=ON"))
    connection_id = await _telegram_connection_id(
        db_session, legacy_owner_roots.subject_id
    )
    intent = _intent(legacy_owner_roots, connection_id)
    db_session.add(intent)
    await db_session.commit()

    db_session.add(
        Notification(
            delivery_intent_id=intent.id,
            subject_id=intent.subject_id,
            actor_user_id=intent.actor_user_id,
            recipient_user_id=intent.recipient_user_id,
            integration_connection_id=intent.integration_connection_id,
            sent_at=NOW,
            category=intent.category,
            channel=intent.channel,
            dedupe_key=intent.idempotency_key,
            external_id=external_id,
            payload={"synthetic": True},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_notification_dedupe_root_shape_rejects_partial_keyed_rows(
    db_session,
    legacy_owner_roots,
):
    row = Notification(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        recipient_user_id=None,
        integration_connection_id=None,
        sent_at=NOW,
        category="brief",
        channel="telegram",
        dedupe_key="partial-root",
        payload={"synthetic": True},
    )
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_raw_category_claim_is_unique_but_null_raws_and_categories_are_distinct(
    db_session,
    legacy_owner_roots,
):
    if db_session.get_bind().dialect.name == "sqlite":
        await db_session.execute(sa.text("PRAGMA foreign_keys=ON"))
    connection_id = await _telegram_connection_id(
        db_session, legacy_owner_roots.subject_id
    )
    raw = RawPayload(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=connection_id,
        domain="signals",
        source="telegram",
        external_id="synthetic-raw-category-claim",
        payload={"synthetic": True},
    )
    db_session.add(raw)
    await db_session.flush()
    raw_id = raw.id

    db_session.add(
        _intent(
            legacy_owner_roots,
            connection_id,
            raw_payload_id=raw_id,
            category="reply",
        )
    )
    await db_session.commit()

    db_session.add(
        _intent(
            legacy_owner_roots,
            connection_id,
            raw_payload_id=raw_id,
            category="reply",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    db_session.add_all(
        [
            _intent(
                legacy_owner_roots,
                connection_id,
                raw_payload_id=raw_id,
                category="echo",
            ),
            _intent(
                legacy_owner_roots,
                connection_id,
                raw_payload_id=None,
                category="reply",
            ),
            _intent(
                legacy_owner_roots,
                connection_id,
                raw_payload_id=None,
                category="reply",
            ),
        ]
    )
    await db_session.commit()

    assert await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(NotificationDeliveryIntent)
        .where(NotificationDeliveryIntent.category.in_(("reply", "echo")))
    ) == 4


def _pre_0043_schema(connection) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    sa.Table(
        "health_subjects",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id",
            name="uq_health_subjects_owner_user_id",
        ),
    )
    sa.Table(
        "integration_connections",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("connection_type", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "id",
            "subject_id",
            name="uq_integration_connections_id_subject",
        ),
    )
    sa.Table(
        "raw_payloads",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("id", "subject_id", name="uq_raw_payloads_id_subject"),
    )
    sa.Table(
        "ai_invocations",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint("id", "subject_id", name="uq_ai_invocations_id_subject"),
    )
    notifications = sa.Table(
        "notifications",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_user_id", sa.Uuid(), nullable=True),
        sa.Column("integration_connection_id", sa.Uuid(), nullable=True),
        sa.Column("ai_invocation_id", sa.Uuid(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("dedupe_key", sa.String(128), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
    )
    sa.Index(
        "uq_notification_dedupe_key",
        notifications.c.dedupe_key,
        unique=True,
        sqlite_where=sa.text("dedupe_key IS NOT NULL"),
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )
    metadata.create_all(connection)


def _migration(monkeypatch, connection):
    migration = importlib.import_module(
        "migrations.versions.0043_notification_delivery_intents"
    )
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _migration_0044(monkeypatch, connection):
    migration = importlib.import_module(
        "migrations.versions.0044_notification_delivery_raw_category_unique"
    )
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _assert_raw_category_unique_index(connection, *, present: bool = True) -> None:
    indexes = {
        item["name"]: item
        for item in inspect(connection).get_indexes(
            "notification_delivery_intents"
        )
    }
    if not present:
        assert RAW_CATEGORY_UNIQUE_INDEX not in indexes
        return
    index = indexes[RAW_CATEGORY_UNIQUE_INDEX]
    assert bool(index["unique"]) is True
    assert tuple(index["column_names"]) == ("raw_payload_id", "category")


def _assert_migrated_contract(connection) -> None:
    inspector = inspect(connection)
    assert "notification_delivery_intents" in inspector.get_table_names()
    intent_columns = {
        column["name"]: column
        for column in inspector.get_columns("notification_delivery_intents")
    }
    assert set(intent_columns) == {
        "id",
        "subject_id",
        "recipient_user_id",
        "actor_user_id",
        "integration_connection_id",
        "raw_payload_id",
        "ai_invocation_id",
        "category",
        "channel",
        "idempotency_key",
        "policy_key",
        "policy_at",
        "policy_date",
        "status",
        "lease_token",
        "dispatch_started_at",
        "completed_at",
        "error_code",
        "created_at",
        "updated_at",
    }
    assert intent_columns["policy_date"]["nullable"] is False
    assert intent_columns["policy_at"]["nullable"] is False
    assert isinstance(intent_columns["policy_at"]["type"], sa.DateTime)
    if connection.dialect.name == "postgresql":
        assert intent_columns["policy_at"]["type"].timezone is True
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "notification_delivery_intents"
        )
    } >= {
        "uq_notification_delivery_intents_id_subject",
        "uq_notification_delivery_intents_delivery_graph",
        "uq_notification_delivery_intents_subject_recipient_idempotency",
        "uq_notification_delivery_intents_ai_invocation_id",
    }
    assert {
        item["name"]
        for item in inspector.get_indexes("notification_delivery_intents")
    } >= {
        "ix_notification_delivery_intents_status_updated",
        "ix_notification_delivery_intents_subject_status_created",
        "ix_notification_delivery_intents_connection_status_created",
        "ix_notification_delivery_intents_raw_category_created",
        "ix_notification_delivery_intents_recipient_created",
        "ix_notification_delivery_intents_budget",
        "ix_notification_delivery_intents_policy",
    }
    assert {
        item["name"]
        for item in inspector.get_check_constraints(
            "notification_delivery_intents"
        )
    } >= {
        "ck_notification_delivery_intents_channel",
        "ck_notification_delivery_intents_category",
        "ck_notification_delivery_intents_raw_category",
        "ck_notification_delivery_intents_ai_provenance",
        "ck_notification_delivery_intents_idempotency_key_opaque",
        "ck_notification_delivery_intents_policy_key_category",
        "ck_notification_delivery_intents_policy_key_opaque",
        "ck_notification_delivery_intents_status",
        "ck_notification_delivery_intents_lifecycle",
        "ck_notification_delivery_intents_timestamp_order",
        "ck_notification_delivery_intents_error_state",
    }
    intent_foreign_keys = {
        item["name"]: item
        for item in inspector.get_foreign_keys("notification_delivery_intents")
        if item["name"] is not None
    }
    assert set(intent_foreign_keys) >= {
        "fk_notification_delivery_intents_connection_subject",
        "fk_notification_delivery_intents_raw_subject",
        "fk_notification_delivery_intents_ai_invocation_subject",
    }

    notification_columns = {
        item["name"] for item in inspector.get_columns("notifications")
    }
    assert "delivery_intent_id" in notification_columns
    notification_indexes = {
        item["name"] for item in inspector.get_indexes("notifications")
    }
    assert "uq_notification_dedupe_key" not in notification_indexes
    assert "ix_notifications_delivery_intent_id" not in notification_indexes
    assert notification_indexes >= {
        "uq_notifications_owned_dedupe_key",
        "uq_notifications_legacy_dedupe_key",
    }
    notification_checks = {
        item["name"]: item["sqltext"]
        for item in inspector.get_check_constraints("notifications")
    }
    assert set(notification_checks) >= {
        "ck_notifications_delivery_intent_scope",
        "ck_notifications_dedupe_root_shape",
    }
    intent_scope = notification_checks["ck_notifications_delivery_intent_scope"]
    normalized_intent_scope = intent_scope.lower()
    assert "external_id is not null" in normalized_intent_scope
    assert "length(trim(" in normalized_intent_scope
    assert "> 0" in normalized_intent_scope
    notification_foreign_keys = {
        item["name"]: item
        for item in inspector.get_foreign_keys("notifications")
        if item["name"] is not None
    }
    link = notification_foreign_keys["fk_notifications_delivery_intent_subject"]
    assert tuple(link["constrained_columns"]) == (
        "delivery_intent_id",
        "subject_id",
        "recipient_user_id",
        "integration_connection_id",
        "category",
        "channel",
        "dedupe_key",
    )
    assert tuple(link["referred_columns"]) == (
        "id",
        "subject_id",
        "recipient_user_id",
        "integration_connection_id",
        "category",
        "channel",
        "idempotency_key",
    )


def _seed_migration_intent_roots(connection):
    tables = {
        name: sa.Table(name, sa.MetaData(), autoload_with=connection)
        for name in (
            "users",
            "health_subjects",
            "integration_connections",
            "raw_payloads",
            "notification_delivery_intents",
            "notifications",
        )
    }
    subject_id = uuid.uuid4().hex
    recipient_id = uuid.uuid4().hex
    actor_id = uuid.uuid4().hex
    connection_id = uuid.uuid4().hex
    connection.execute(
        tables["users"].insert(),
        [{"id": recipient_id}, {"id": actor_id}],
    )
    connection.execute(
        tables["health_subjects"].insert().values(
            id=subject_id,
            owner_user_id=recipient_id,
        )
    )
    connection.execute(
        tables["integration_connections"].insert().values(
            id=connection_id,
            subject_id=subject_id,
            provider="telegram",
            connection_type="recipient",
        )
    )
    return tables, subject_id, recipient_id, actor_id, connection_id


def _insert_migration_intent(
    connection,
    tables,
    *,
    subject_id,
    recipient_id,
    actor_id,
    connection_id,
    key=None,
    **overrides,
):
    values = {
        "id": uuid.uuid4().hex,
        "subject_id": subject_id,
        "recipient_user_id": recipient_id,
        "actor_user_id": actor_id,
        "integration_connection_id": connection_id,
        "category": "brief",
        "channel": "telegram",
        "idempotency_key": key or (uuid.uuid4().hex * 2),
        "policy_at": POLICY_AT,
        "policy_date": POLICY_DATE,
        "status": "pending",
    }
    values.update(overrides)
    connection.execute(
        tables["notification_delivery_intents"].insert().values(**values)
    )
    return values


def _seed_linked_raw_reply(connection):
    tables, subject_id, recipient_id, actor_id, connection_id = (
        _seed_migration_intent_roots(connection)
    )
    raw_payload_id = 1
    connection.execute(
        tables["raw_payloads"].insert().values(
            id=raw_payload_id,
            subject_id=subject_id,
        )
    )
    intent = _insert_migration_intent(
        connection,
        tables,
        subject_id=subject_id,
        recipient_id=recipient_id,
        actor_id=actor_id,
        connection_id=connection_id,
        raw_payload_id=raw_payload_id,
        category="reply",
        status="sent",
        lease_token=uuid.uuid4().hex,
        dispatch_started_at=POLICY_AT,
        completed_at=POLICY_AT,
    )
    connection.execute(
        tables["notifications"].insert().values(
            id=1,
            delivery_intent_id=intent["id"],
            subject_id=subject_id,
            actor_user_id=actor_id,
            recipient_user_id=recipient_id,
            integration_connection_id=connection_id,
            category=intent["category"],
            channel=intent["channel"],
            dedupe_key=intent["idempotency_key"],
            external_id="101",
        )
    )
    return (
        tables,
        intent,
        subject_id,
        recipient_id,
        actor_id,
        connection_id,
        raw_payload_id,
    )


def test_sqlite_0043_empty_roundtrip_preserves_legacy_rows(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            _pre_0043_schema(connection)
            notifications = sa.Table(
                "notifications", sa.MetaData(), autoload_with=connection
            )
            connection.execute(
                notifications.insert().values(
                    id=1,
                    category="brief",
                    channel="telegram",
                    dedupe_key="legacy-opaque-key",
                )
            )
            migration = _migration(monkeypatch, connection)
            migration.upgrade()
            _assert_migrated_contract(connection)
            migration.downgrade()

            inspector = inspect(connection)
            assert "notification_delivery_intents" not in inspector.get_table_names()
            assert "delivery_intent_id" not in {
                column["name"] for column in inspector.get_columns("notifications")
            }
            assert "uq_notification_dedupe_key" in {
                item["name"] for item in inspector.get_indexes("notifications")
            }
            restored = sa.Table(
                "notifications", sa.MetaData(), autoload_with=connection
            )
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(restored)
            ) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "partial_root",
    (
        {"actor_user_id": uuid.uuid4().hex},
        {"recipient_user_id": uuid.uuid4().hex},
        {"integration_connection_id": uuid.uuid4().hex},
        {
            "subject_id": uuid.uuid4().hex,
            "integration_connection_id": uuid.uuid4().hex,
        },
        {
            "subject_id": uuid.uuid4().hex,
            "recipient_user_id": uuid.uuid4().hex,
        },
    ),
)
def test_sqlite_0043_upgrade_refuses_partial_dedupe_roots_before_ddl(
    monkeypatch,
    partial_root,
):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _pre_0043_schema(connection)
            notifications = sa.Table(
                "notifications", sa.MetaData(), autoload_with=connection
            )
            connection.execute(
                notifications.insert().values(
                    id=1,
                    category="brief",
                    channel="telegram",
                    dedupe_key="partial-root",
                    **partial_root,
                )
            )
            before = {
                "tables": tuple(inspect(connection).get_table_names()),
                "columns": tuple(
                    column["name"]
                    for column in inspect(connection).get_columns("notifications")
                ),
                "indexes": tuple(
                    item["name"]
                    for item in inspect(connection).get_indexes("notifications")
                ),
            }
            migration = _migration(monkeypatch, connection)
            with pytest.raises(RuntimeError) as exc_info:
                migration.upgrade()
            assert str(exc_info.value) == UPGRADE_REFUSAL
            assert {
                "tables": tuple(inspect(connection).get_table_names()),
                "columns": tuple(
                    column["name"]
                    for column in inspect(connection).get_columns("notifications")
                ),
                "indexes": tuple(
                    item["name"]
                    for item in inspect(connection).get_indexes("notifications")
                ),
            } == before
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(notifications)
            ) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mismatch",
    ("subject", "recipient", "provider", "connection_type", "channel"),
)
def test_sqlite_0043_upgrade_refuses_mismatched_delivery_root_before_ddl(
    monkeypatch,
    mismatch,
):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _pre_0043_schema(connection)
            metadata = sa.MetaData()
            users = sa.Table("users", metadata, autoload_with=connection)
            subjects = sa.Table(
                "health_subjects", metadata, autoload_with=connection
            )
            connections = sa.Table(
                "integration_connections", metadata, autoload_with=connection
            )
            notifications = sa.Table(
                "notifications", metadata, autoload_with=connection
            )
            subject_id = uuid.uuid4().hex
            other_subject_id = uuid.uuid4().hex
            recipient_id = uuid.uuid4().hex
            other_recipient_id = uuid.uuid4().hex
            connection_id = uuid.uuid4().hex
            connection.execute(
                users.insert(),
                [{"id": recipient_id}, {"id": other_recipient_id}],
            )
            connection.execute(
                subjects.insert(),
                [
                    {"id": subject_id, "owner_user_id": recipient_id},
                    {
                        "id": other_subject_id,
                        "owner_user_id": other_recipient_id,
                    },
                ],
            )
            connection_values = {
                "id": connection_id,
                "subject_id": subject_id,
                "provider": "telegram",
                "connection_type": "recipient",
            }
            notification_values = {
                "id": 1,
                "subject_id": subject_id,
                "recipient_user_id": recipient_id,
                "integration_connection_id": connection_id,
                "category": "brief",
                "channel": "telegram",
                "dedupe_key": "owned-key",
            }
            if mismatch == "subject":
                connection_values["subject_id"] = other_subject_id
            elif mismatch == "recipient":
                notification_values["recipient_user_id"] = other_recipient_id
            elif mismatch == "provider":
                connection_values["provider"] = "openrouter"
            elif mismatch == "connection_type":
                connection_values["connection_type"] = "account"
            else:
                notification_values["channel"] = "openrouter"
            connection.execute(connections.insert().values(**connection_values))
            connection.execute(notifications.insert().values(**notification_values))

            before = {
                "tables": tuple(inspect(connection).get_table_names()),
                "columns": tuple(
                    column["name"]
                    for column in inspect(connection).get_columns("notifications")
                ),
                "indexes": tuple(
                    item["name"]
                    for item in inspect(connection).get_indexes("notifications")
                ),
            }
            migration = _migration(monkeypatch, connection)
            with pytest.raises(RuntimeError) as exc_info:
                migration.upgrade()
            assert str(exc_info.value) == UPGRADE_DELIVERY_ROOT_REFUSAL
            assert {
                "tables": tuple(inspect(connection).get_table_names()),
                "columns": tuple(
                    column["name"]
                    for column in inspect(connection).get_columns("notifications")
                ),
                "indexes": tuple(
                    item["name"]
                    for item in inspect(connection).get_indexes("notifications")
                ),
            } == before
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(notifications)
            ) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("incompatibility", tuple(DOWNGRADE_REFUSALS))
def test_sqlite_0043_downgrade_guards_are_nondestructive(
    monkeypatch,
    incompatibility,
):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            _pre_0043_schema(connection)
            migration = _migration(monkeypatch, connection)
            migration.upgrade()
            if incompatibility in {"intent", "link"}:
                roots = _seed_migration_intent_roots(connection)
                tables, subject_id, recipient_id, actor_id, connection_id = roots
                intent = _insert_migration_intent(
                    connection,
                    tables,
                    subject_id=subject_id,
                    recipient_id=recipient_id,
                    actor_id=actor_id,
                    connection_id=connection_id,
                )
                if incompatibility == "link":
                    connection.execute(
                        tables["notifications"].insert().values(
                            id=1,
                            delivery_intent_id=intent["id"],
                            subject_id=subject_id,
                            actor_user_id=actor_id,
                            recipient_user_id=recipient_id,
                            integration_connection_id=connection_id,
                            category=intent["category"],
                            channel=intent["channel"],
                            dedupe_key=intent["idempotency_key"],
                            external_id="101",
                        )
                    )
                tracked = tables[
                    "notifications"
                    if incompatibility == "link"
                    else "notification_delivery_intents"
                ]
            else:
                notifications = sa.Table(
                    "notifications", sa.MetaData(), autoload_with=connection
                )
                shared_key = "shared-scoped-key"
                for row_id in (1, 2):
                    connection.execute(
                        notifications.insert().values(
                            id=row_id,
                            subject_id=uuid.uuid4().hex,
                            recipient_user_id=uuid.uuid4().hex,
                            integration_connection_id=uuid.uuid4().hex,
                            category="brief",
                            channel="telegram",
                            dedupe_key=shared_key,
                        )
                    )
                tracked = notifications

            before_indexes = {
                table: tuple(
                    item["name"] for item in inspect(connection).get_indexes(table)
                )
                for table in (
                    "notification_delivery_intents",
                    "notifications",
                )
            }
            with pytest.raises(RuntimeError) as exc_info:
                migration.downgrade()
            assert str(exc_info.value) == DOWNGRADE_REFUSALS[incompatibility]
            _assert_migrated_contract(connection)
            assert {
                table: tuple(
                    item["name"] for item in inspect(connection).get_indexes(table)
                )
                for table in (
                    "notification_delivery_intents",
                    "notifications",
                )
            } == before_indexes
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(tracked)
            ) >= 1
    finally:
        engine.dispose()


def test_sqlite_0043_migrated_constraints_enforce_graph_and_state(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            _pre_0043_schema(connection)
            migration = _migration(monkeypatch, connection)
            migration.upgrade()
            tables, subject_id, recipient_id, actor_id, connection_id = (
                _seed_migration_intent_roots(connection)
            )
            intent = _insert_migration_intent(
                connection,
                tables,
                subject_id=subject_id,
                recipient_id=recipient_id,
                actor_id=actor_id,
                connection_id=connection_id,
            )
            assert actor_id != recipient_id

            with connection.begin_nested(), pytest.raises(IntegrityError):
                _insert_migration_intent(
                    connection,
                    tables,
                    subject_id=subject_id,
                    recipient_id=recipient_id,
                    actor_id=actor_id,
                    connection_id=connection_id,
                    status="dispatching",
                )
            with connection.begin_nested(), pytest.raises(IntegrityError):
                _insert_migration_intent(
                    connection,
                    tables,
                    subject_id=subject_id,
                    recipient_id=recipient_id,
                    actor_id=actor_id,
                    connection_id=connection_id,
                    policy_date=None,
                )
            with connection.begin_nested(), pytest.raises(IntegrityError):
                _insert_migration_intent(
                    connection,
                    tables,
                    subject_id=subject_id,
                    recipient_id=recipient_id,
                    actor_id=actor_id,
                    connection_id=connection_id,
                    category="nudge",
                    policy_key=None,
                )
            with connection.begin_nested(), pytest.raises(IntegrityError):
                connection.execute(
                    tables["notifications"].insert().values(
                        id=1,
                        delivery_intent_id=intent["id"],
                        subject_id=subject_id,
                        actor_user_id=actor_id,
                        recipient_user_id=recipient_id,
                        integration_connection_id=connection_id,
                        category=intent["category"],
                        channel=intent["channel"],
                        dedupe_key="f" * 64,
                        external_id="101",
                    )
                )
            with connection.begin_nested(), pytest.raises(IntegrityError):
                connection.execute(
                    tables["notifications"].insert().values(
                        id=2,
                        delivery_intent_id=intent["id"],
                        subject_id=subject_id,
                        actor_user_id=actor_id,
                        recipient_user_id=recipient_id,
                        integration_connection_id=connection_id,
                        category=intent["category"],
                        channel=intent["channel"],
                        dedupe_key=intent["idempotency_key"],
                        external_id="   ",
                    )
                )
            with connection.begin_nested(), pytest.raises(IntegrityError):
                connection.execute(
                    tables["notifications"].insert().values(
                        id=3,
                        subject_id=subject_id,
                        actor_user_id=actor_id,
                        recipient_user_id=None,
                        integration_connection_id=connection_id,
                        category="brief",
                        channel="telegram",
                        dedupe_key="partial-root",
                    )
                )
    finally:
        engine.dispose()


def test_sqlite_0044_roundtrip_preserves_linked_notification_and_null_claims(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            _pre_0043_schema(connection)
            _migration(monkeypatch, connection).upgrade()
            (
                tables,
                intent,
                subject_id,
                recipient_id,
                actor_id,
                connection_id,
                raw_payload_id,
            ) = _seed_linked_raw_reply(connection)
            migration = _migration_0044(monkeypatch, connection)
            assert migration.revision == "0044"
            assert migration.down_revision == "0043"

            migration.upgrade()
            _assert_raw_category_unique_index(connection)
            assert connection.scalar(
                sa.select(sa.func.count())
                .select_from(tables["notifications"])
                .where(tables["notifications"].c.delivery_intent_id.is_not(None))
            ) == 1

            with pytest.raises(IntegrityError), connection.begin_nested():
                _insert_migration_intent(
                    connection,
                    tables,
                    subject_id=subject_id,
                    recipient_id=recipient_id,
                    actor_id=actor_id,
                    connection_id=connection_id,
                    raw_payload_id=raw_payload_id,
                    category="reply",
                )

            _insert_migration_intent(
                connection,
                tables,
                subject_id=subject_id,
                recipient_id=recipient_id,
                actor_id=actor_id,
                connection_id=connection_id,
                raw_payload_id=raw_payload_id,
                category="echo",
            )
            for _ in range(2):
                _insert_migration_intent(
                    connection,
                    tables,
                    subject_id=subject_id,
                    recipient_id=recipient_id,
                    actor_id=actor_id,
                    connection_id=connection_id,
                    raw_payload_id=None,
                    category="reply",
                )
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(
                    tables["notification_delivery_intents"]
                )
            ) == 4

            migration.downgrade()
            _assert_raw_category_unique_index(connection, present=False)
            assert connection.scalar(
                sa.select(sa.func.count())
                .select_from(tables["notifications"])
                .where(
                    tables["notifications"].c.delivery_intent_id
                    == intent["id"]
                )
            ) == 1
            _insert_migration_intent(
                connection,
                tables,
                subject_id=subject_id,
                recipient_id=recipient_id,
                actor_id=actor_id,
                connection_id=connection_id,
                raw_payload_id=raw_payload_id,
                category="reply",
            )
    finally:
        engine.dispose()


def test_sqlite_0044_duplicate_preflight_is_before_ddl_and_nondestructive(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            _pre_0043_schema(connection)
            _migration(monkeypatch, connection).upgrade()
            (
                tables,
                _intent_row,
                subject_id,
                recipient_id,
                actor_id,
                connection_id,
                raw_payload_id,
            ) = _seed_linked_raw_reply(connection)
            _insert_migration_intent(
                connection,
                tables,
                subject_id=subject_id,
                recipient_id=recipient_id,
                actor_id=actor_id,
                connection_id=connection_id,
                raw_payload_id=raw_payload_id,
                category="reply",
            )
            before_intents = tuple(
                connection.execute(
                    sa.select(
                        tables["notification_delivery_intents"].c.id,
                        tables["notification_delivery_intents"].c.raw_payload_id,
                        tables["notification_delivery_intents"].c.category,
                    ).order_by(tables["notification_delivery_intents"].c.id)
                )
            )
            before_links = tuple(
                connection.execute(
                    sa.select(
                        tables["notifications"].c.id,
                        tables["notifications"].c.delivery_intent_id,
                    ).order_by(tables["notifications"].c.id)
                )
            )
            statements = []

            def capture_statement(
                _connection,
                _cursor,
                statement,
                _parameters,
                _context,
                _executemany,
            ):
                statements.append(" ".join(statement.lower().split()))

            migration = _migration_0044(monkeypatch, connection)
            sa.event.listen(connection, "before_cursor_execute", capture_statement)
            try:
                with pytest.raises(RuntimeError) as exc_info:
                    migration.upgrade()
            finally:
                sa.event.remove(
                    connection,
                    "before_cursor_execute",
                    capture_statement,
                )

            assert str(exc_info.value) == RAW_CATEGORY_UPGRADE_REFUSAL
            assert statements
            assert all(
                not statement.startswith(("alter ", "create ", "drop "))
                for statement in statements
            )
            _assert_raw_category_unique_index(connection, present=False)
            assert tuple(
                connection.execute(
                    sa.select(
                        tables["notification_delivery_intents"].c.id,
                        tables["notification_delivery_intents"].c.raw_payload_id,
                        tables["notification_delivery_intents"].c.category,
                    ).order_by(tables["notification_delivery_intents"].c.id)
                )
            ) == before_intents
            assert tuple(
                connection.execute(
                    sa.select(
                        tables["notifications"].c.id,
                        tables["notifications"].c.delivery_intent_id,
                    ).order_by(tables["notifications"].c.id)
                )
            ) == before_links
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_postgres_policy_coordinates_preserve_dst_aware_instant(
    db_session,
    legacy_owner_roots,
):
    connection_id = await _telegram_connection_id(
        db_session, legacy_owner_roots.subject_id
    )
    row = _intent(
        legacy_owner_roots,
        connection_id,
        policy_at=DST_POLICY_AT,
        policy_date=DST_POLICY_AT.date(),
    )
    db_session.add(row)
    await db_session.commit()
    row_id = row.id
    db_session.expunge_all()

    persisted = await db_session.get(NotificationDeliveryIntent, row_id)
    assert persisted is not None
    assert persisted.policy_at == DST_POLICY_AT.astimezone(UTC)
    assert persisted.policy_at.tzinfo is not None
    assert persisted.policy_date == DST_POLICY_AT.date()


@pytest.mark.integration
async def test_postgres_0043_empty_downgrade_upgrade_roundtrip(
    db_session,
    monkeypatch,
):
    connection = await db_session.connection()

    def roundtrip(sync_connection):
        statements = []

        def capture_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            statements.append(" ".join(statement.lower().split()))

        migration = _migration(monkeypatch, sync_connection)
        sa.event.listen(
            sync_connection,
            "before_cursor_execute",
            capture_statement,
        )
        try:
            migration.downgrade()
            downgrade_statements = tuple(statements)
            statements.clear()

            inspector = inspect(sync_connection)
            assert "notification_delivery_intents" not in inspector.get_table_names()
            assert "delivery_intent_id" not in {
                item["name"] for item in inspector.get_columns("notifications")
            }
            assert "uq_notification_dedupe_key" in {
                item["name"] for item in inspector.get_indexes("notifications")
            }

            statements.clear()
            migration.upgrade()
            upgrade_statements = tuple(statements)
            _assert_migrated_contract(sync_connection)
        finally:
            sa.event.remove(
                sync_connection,
                "before_cursor_execute",
                capture_statement,
            )

        assert downgrade_statements[0] == (
            "lock table health_subjects, users, integration_connections, "
            "raw_payloads, ai_invocations, notification_delivery_intents, "
            "notifications in access exclusive mode"
        )
        assert upgrade_statements[0] == (
            "lock table health_subjects, users, integration_connections, "
            "raw_payloads, ai_invocations, notifications "
            "in access exclusive mode"
        )

    await connection.run_sync(roundtrip)


@pytest.mark.integration
async def test_postgres_0044_locked_preflight_and_constraint_roundtrip(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    connection_id = await _telegram_connection_id(
        db_session, legacy_owner_roots.subject_id
    )
    raw = RawPayload(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=connection_id,
        domain="signals",
        source="telegram",
        external_id="synthetic-pg-0044-raw",
        payload={"synthetic": True},
    )
    db_session.add(raw)
    await db_session.flush()
    intent = _intent(
        legacy_owner_roots,
        connection_id,
        raw_payload_id=raw.id,
        category="reply",
        status=NotificationDeliveryStatus.SENT.value,
        lease_token=uuid.uuid4(),
        dispatch_started_at=POLICY_AT,
        completed_at=POLICY_AT,
    )
    db_session.add(intent)
    await db_session.flush()
    db_session.add(
        Notification(
            delivery_intent_id=intent.id,
            subject_id=intent.subject_id,
            actor_user_id=intent.actor_user_id,
            recipient_user_id=intent.recipient_user_id,
            integration_connection_id=intent.integration_connection_id,
            sent_at=NOW,
            category=intent.category,
            channel=intent.channel,
            dedupe_key=intent.idempotency_key,
            external_id="101",
            payload={"synthetic": True},
        )
    )
    await db_session.commit()
    intent_id = intent.id
    raw_payload_id = raw.id
    subject_id = legacy_owner_roots.subject_id
    recipient_id = legacy_owner_roots.user_id
    actor_id = legacy_owner_roots.user_id
    connection = await db_session.connection()

    def roundtrip(sync_connection):
        migration = _migration_0044(monkeypatch, sync_connection)
        _assert_raw_category_unique_index(sync_connection)
        migration.downgrade()
        _assert_raw_category_unique_index(sync_connection, present=False)

        tables = {
            name: sa.Table(name, sa.MetaData(), autoload_with=sync_connection)
            for name in (
                "notification_delivery_intents",
                "notifications",
            )
        }
        assert sync_connection.scalar(
            sa.select(sa.func.count())
            .select_from(tables["notifications"])
            .where(tables["notifications"].c.delivery_intent_id == intent_id)
        ) == 1
        duplicate = _insert_migration_intent(
            sync_connection,
            tables,
            subject_id=subject_id,
            recipient_id=recipient_id,
            actor_id=actor_id,
            connection_id=connection_id,
            raw_payload_id=raw_payload_id,
            category="reply",
            id=uuid.uuid4(),
        )

        statements = []

        def capture_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            statements.append(" ".join(statement.lower().split()))

        sa.event.listen(
            sync_connection,
            "before_cursor_execute",
            capture_statement,
        )
        try:
            with pytest.raises(RuntimeError) as exc_info:
                migration.upgrade()
            failed_upgrade_statements = tuple(statements)
            assert str(exc_info.value) == RAW_CATEGORY_UPGRADE_REFUSAL
            assert failed_upgrade_statements[0] == (
                "lock table notification_delivery_intents "
                "in access exclusive mode"
            )
            assert not any(
                statement.startswith("create unique index")
                for statement in failed_upgrade_statements
            )
            _assert_raw_category_unique_index(sync_connection, present=False)
            assert sync_connection.scalar(
                sa.select(sa.func.count())
                .select_from(tables["notification_delivery_intents"])
                .where(
                    tables["notification_delivery_intents"].c.raw_payload_id
                    == raw_payload_id,
                    tables["notification_delivery_intents"].c.category == "reply",
                )
            ) == 2
            assert sync_connection.scalar(
                sa.select(sa.func.count())
                .select_from(tables["notifications"])
                .where(tables["notifications"].c.delivery_intent_id == intent_id)
            ) == 1

            sync_connection.execute(
                tables["notification_delivery_intents"]
                .delete()
                .where(
                    tables["notification_delivery_intents"].c.id
                    == duplicate["id"]
                )
            )
            statements.clear()
            migration.upgrade()
            successful_upgrade_statements = tuple(statements)
        finally:
            sa.event.remove(
                sync_connection,
                "before_cursor_execute",
                capture_statement,
            )

        assert successful_upgrade_statements[0] == (
            "lock table notification_delivery_intents "
            "in access exclusive mode"
        )
        preflight_position = next(
            index
            for index, statement in enumerate(successful_upgrade_statements)
            if statement.startswith(
                "select notification_delivery_intents.raw_payload_id"
            )
        )
        ddl_position = next(
            index
            for index, statement in enumerate(successful_upgrade_statements)
            if statement.startswith(
                "create unique index uq_notification_delivery_intents_raw_category"
            )
        )
        assert preflight_position < ddl_position
        _assert_raw_category_unique_index(sync_connection)

        with pytest.raises(IntegrityError), sync_connection.begin_nested():
            _insert_migration_intent(
                sync_connection,
                tables,
                subject_id=subject_id,
                recipient_id=recipient_id,
                actor_id=actor_id,
                connection_id=connection_id,
                raw_payload_id=raw_payload_id,
                category="reply",
                id=uuid.uuid4(),
            )
        assert sync_connection.scalar(
            sa.select(sa.func.count())
            .select_from(tables["notifications"])
            .where(tables["notifications"].c.delivery_intent_id == intent_id)
        ) == 1

    await connection.run_sync(roundtrip)
