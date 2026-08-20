"""0042 raw-backed AI provenance and capability binding contracts."""
from __future__ import annotations

import asyncio
import importlib
import uuid
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.ai import (
    AIInvocation,
    AIPlatformQuotaPeriod,
    AISubjectQuotaPeriod,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.proactive import Notification
from vitals.models.raw_payload import RawPayload
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection, PlatformIntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import ai_gateway_service as gateway
from vitals.services import platform_admin_service
from web.config import get_web_config

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)
RAW_PURPOSES = (
    AIInvocationPurpose.SIGNAL_PARSE,
    AIInvocationPurpose.QUESTION_REPLY,
    AIInvocationPurpose.LAB_DOCUMENT_PARSE,
    AIInvocationPurpose.BODY_SCAN_PARSE,
)
UPGRADE_REFUSAL = (
    "0042 upgrade refused: raw-backed AI invocations require explicit "
    "raw_payload_id backfill"
)
DOWNGRADE_REFUSALS = {
    "raw": (
        "0042 downgrade refused: ai_invocations.raw_payload_id contains raw "
        "provenance data"
    ),
    "notification": (
        "0042 downgrade refused: notifications.ai_invocation_id contains AI "
        "provenance data"
    ),
    "alert": (
        "0042 downgrade refused: system_alerts.ai_invocation_id contains AI "
        "provenance data"
    ),
    "platform_alert": (
        "0042 downgrade refused: invocation-null platform signal parser alerts "
        "require explicit legacy conversion"
    ),
}


def _identity(roots) -> WriteIdentity:
    return WriteIdentity(roots.subject_id, roots.user_id)


async def _configure_gateway(session, roots) -> PlatformIntegrationConnection:
    if session.get_bind().dialect.name == "sqlite":
        await session.execute(text("PRAGMA foreign_keys=ON"))
    prepared = await platform_admin_service.prepare_platform_admin(
        session,
        actor_username=get_web_config().auth_username,
    )
    root = await gateway.create_gateway(
        session,
        prepared=prepared,
        external_account_discriminator="opaque-0042-root",
        credential_ref="env:VITALS_OPENROUTER_API_KEY",
    )
    await gateway.configure_platform_quota_period(
        session,
        prepared=prepared,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        cost_limit_microunits=100_000,
        unit_limit=100_000,
    )
    await gateway.configure_subject_quota_period(
        session,
        prepared=prepared,
        subject_id=roots.subject_id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        cost_limit_microunits=100_000,
        unit_limit=100_000,
    )
    await session.commit()
    return root


async def _telegram_connection_id(session, subject_id: uuid.UUID) -> uuid.UUID:
    connection_id = await session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.RECIPIENT.value,
        )
    )
    assert connection_id is not None
    return connection_id


async def _raw(session, roots, *, suffix: str = "one") -> RawPayload:
    row = RawPayload(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        integration_connection_id=await _telegram_connection_id(
            session, roots.subject_id
        ),
        domain="signals",
        source="telegram",
        external_id=f"opaque-raw-{suffix}",
        payload={"synthetic": True},
    )
    session.add(row)
    await session.flush()
    return row


def _invocation(
    *,
    roots,
    root: PlatformIntegrationConnection,
    raw_payload_id: int | None,
    purpose: AIInvocationPurpose,
    key: str,
    status: AIInvocationStatus = AIInvocationStatus.PREPARED,
) -> AIInvocation:
    paid = status in {
        AIInvocationStatus.DISPATCHING,
        AIInvocationStatus.SUCCEEDED,
        AIInvocationStatus.FAILED,
        AIInvocationStatus.AMBIGUOUS,
    }
    terminal = status in {
        AIInvocationStatus.SUCCEEDED,
        AIInvocationStatus.FAILED,
        AIInvocationStatus.AMBIGUOUS,
    }
    return AIInvocation(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        raw_payload_id=raw_payload_id,
        platform_integration_connection_id=root.id,
        purpose=purpose.value,
        source=AIInvocationSource.TELEGRAM.value,
        model="synthetic/raw-model",
        config_version=root.config_version,
        idempotency_key=key,
        quota_period_start=PERIOD_START,
        quota_period_end=PERIOD_END,
        reserved_cost_microunits=100,
        reserved_units=500,
        charged_cost_microunits=100 if paid else 0,
        charged_units=500 if paid else 0,
        status=status.value,
        started_at=NOW if paid else None,
        finished_at=NOW if terminal else None,
        error_code=(
            AIInvocationErrorCode.INTERNAL_ERROR.value
            if status in {AIInvocationStatus.FAILED, AIInvocationStatus.AMBIGUOUS}
            else None
        ),
    )


def test_models_declare_exact_0042_provenance_contract():
    invocation = AIInvocation.__table__
    raw_column = invocation.c.raw_payload_id
    assert isinstance(raw_column.type, sa.Integer)
    assert raw_column.nullable is True
    assert {
        foreign_key.target_fullname for foreign_key in raw_column.foreign_keys
    } == {"raw_payloads.id"}

    invocation_fk = next(
        constraint
        for constraint in invocation.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_ai_invocations_raw_payload_subject"
    )
    assert tuple(invocation_fk.column_keys) == ("raw_payload_id", "subject_id")
    assert tuple(item.target_fullname for item in invocation_fk.elements) == (
        "raw_payloads.id",
        "raw_payloads.subject_id",
    )
    assert invocation_fk.ondelete == "RESTRICT"
    assert "ck_ai_invocations_purpose_raw_payload" in {
        constraint.name
        for constraint in invocation.constraints
        if isinstance(constraint, CheckConstraint)
    }
    invocation_indexes = {item.name: item for item in invocation.indexes}
    assert tuple(
        column.name
        for column in invocation_indexes[
            "ix_ai_invocations_raw_purpose_created"
        ].columns
    ) == ("raw_payload_id", "purpose", "created_at")
    successful = invocation_indexes["uq_ai_invocations_raw_purpose_succeeded"]
    assert successful.unique is True
    assert successful.dialect_options["postgresql"]["where"] is not None
    assert successful.dialect_options["sqlite"]["where"] is not None

    for model, unique, check, index in (
        (
            Notification,
            "uq_notifications_ai_invocation_id",
            "ck_notifications_ai_invocation_delivery",
            "ix_notifications_ai_invocation_id",
        ),
        (
            SystemAlert,
            None,
            "ck_system_alerts_ai_invocation_scope",
            "ix_system_alerts_ai_invocation_id",
        ),
    ):
        table = model.__table__
        column = table.c.ai_invocation_id
        assert isinstance(column.type, sa.Uuid)
        assert column.type.as_uuid is True
        assert column.nullable is True
        foreign_key = next(
            constraint
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            and constraint.name
            == f"fk_{table.name}_ai_invocation_subject"
        )
        assert tuple(foreign_key.column_keys) == (
            "ai_invocation_id",
            "subject_id",
        )
        assert tuple(item.target_fullname for item in foreign_key.elements) == (
            "ai_invocations.id",
            "ai_invocations.subject_id",
        )
        assert foreign_key.ondelete == "RESTRICT"
        assert check in {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert index in {item.name for item in table.indexes}
        uniques = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        if unique is None:
            assert "uq_system_alerts_ai_invocation_id" not in uniques
        else:
            assert unique in uniques


@pytest.mark.parametrize("purpose", RAW_PURPOSES)
async def test_gateway_binds_each_raw_backed_purpose_through_dispatch_capability(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    purpose,
):
    monkeypatch.setattr(gateway, "now_utc", lambda: NOW)
    await _configure_gateway(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, suffix=purpose.value)
    await db_session.commit()

    reservation = await gateway.reserve_ai_invocation(
        db_session,
        identity=_identity(legacy_owner_roots),
        purpose=purpose,
        source=AIInvocationSource.TELEGRAM,
        model="synthetic/raw-model",
        idempotency_key=f"opaque-{purpose.value}",
        reserved_cost_microunits=100,
        reserved_units=500,
        raw_payload_id=raw.id,
    )
    await db_session.commit()
    lease = await gateway.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _reference: "synthetic-secret",
    )
    assert "raw_payload_id" not in repr(lease)
    await db_session.commit()

    seen = []

    async def provider_call(request):
        seen.append((request.raw_payload_id, db_session.in_transaction()))
        assert request._fingerprint[3] == raw.id
        assert "raw_payload_id" not in repr(request)
        return "synthetic result"

    completion = await gateway.dispatch_ai(
        lease,
        provider_call=provider_call,
        usage_extractor=lambda _result: gateway.SanitizedAIUsage(
            input_tokens=10,
            output_tokens=10,
            cost_microunits=10,
        ),
    )
    assert seen == [(raw.id, False)]
    await gateway.finalize_ai_invocation(db_session, completion=completion)
    await db_session.commit()
    stored = await db_session.get(AIInvocation, reservation.invocation_id)
    assert stored is not None
    assert stored.raw_payload_id == raw.id
    assert stored.status == AIInvocationStatus.SUCCEEDED.value


@pytest.mark.parametrize(
    ("purpose", "raw_payload_id"),
    [
        (AIInvocationPurpose.SIGNAL_PARSE, None),
        (AIInvocationPurpose.QUESTION_REPLY, None),
        (AIInvocationPurpose.LAB_DOCUMENT_PARSE, None),
        (AIInvocationPurpose.BODY_SCAN_PARSE, None),
        (AIInvocationPurpose.WEEKLY_DIGEST, 1),
        (AIInvocationPurpose.DAILY_BRIEF, 1),
        (AIInvocationPurpose.SIGNAL_PARSE, True),
        (AIInvocationPurpose.SIGNAL_PARSE, 0),
        (AIInvocationPurpose.SIGNAL_PARSE, -1),
        (AIInvocationPurpose.SIGNAL_PARSE, 1 << 31),
    ],
)
async def test_gateway_rejects_invalid_purpose_raw_pair_before_db_work(
    db_session,
    legacy_owner_roots,
    purpose,
    raw_payload_id,
):
    with pytest.raises(ValueError):
        await gateway.reserve_ai_invocation(
            db_session,
            identity=_identity(legacy_owner_roots),
            purpose=purpose,
            source=AIInvocationSource.TELEGRAM,
            model="synthetic/raw-model",
            idempotency_key="opaque-invalid-binding",
            reserved_cost_microunits=100,
            reserved_units=500,
            raw_payload_id=raw_payload_id,
        )
    assert db_session.in_transaction() is False


async def test_gateway_rejects_foreign_raw_and_binds_raw_in_idempotency(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    monkeypatch.setattr(gateway, "now_utc", lambda: NOW)
    await _configure_gateway(db_session, legacy_owner_roots)
    own_one = await _raw(db_session, legacy_owner_roots, suffix="own-one")
    own_two = await _raw(db_session, legacy_owner_roots, suffix="own-two")

    foreign_user = User(
        username="raw-foreign-owner",
        normalized_username="raw-foreign-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(foreign_user)
    await db_session.flush()
    foreign_subject = HealthSubject(
        owner_user_id=foreign_user.id,
        display_name="Synthetic foreign raw subject",
        timezone="UTC",
    )
    db_session.add(foreign_subject)
    await db_session.flush()
    foreign_raw = RawPayload(
        subject_id=foreign_subject.id,
        actor_user_id=foreign_user.id,
        domain="signals",
        source="telegram",
        payload={"synthetic": True},
    )
    db_session.add(foreign_raw)
    await db_session.commit()
    own_one_id = own_one.id
    own_two_id = own_two.id
    foreign_raw_id = foreign_raw.id

    with pytest.raises(gateway.AIGatewayAuthorizationError):
        await gateway.reserve_ai_invocation(
            db_session,
            identity=_identity(legacy_owner_roots),
            purpose=AIInvocationPurpose.SIGNAL_PARSE,
            source=AIInvocationSource.TELEGRAM,
            model="synthetic/raw-model",
            idempotency_key="opaque-foreign-raw",
            reserved_cost_microunits=100,
            reserved_units=500,
            raw_payload_id=foreign_raw_id,
        )
    await db_session.rollback()

    first = await gateway.reserve_ai_invocation(
        db_session,
        identity=_identity(legacy_owner_roots),
        purpose=AIInvocationPurpose.SIGNAL_PARSE,
        source=AIInvocationSource.TELEGRAM,
        model="synthetic/raw-model",
        idempotency_key="opaque-raw-fingerprint",
        reserved_cost_microunits=100,
        reserved_units=500,
        raw_payload_id=own_one_id,
    )
    await db_session.commit()
    with pytest.raises(gateway.AIIdempotencyConflictError):
        await gateway.reserve_ai_invocation(
            db_session,
            identity=_identity(legacy_owner_roots),
            purpose=AIInvocationPurpose.SIGNAL_PARSE,
            source=AIInvocationSource.TELEGRAM,
            model="synthetic/raw-model",
            idempotency_key="opaque-raw-fingerprint",
            reserved_cost_microunits=100,
            reserved_units=500,
            raw_payload_id=own_two_id,
        )
    await db_session.rollback()
    assert first.invocation_id == await db_session.scalar(
        select(AIInvocation.id).where(
            AIInvocation.idempotency_key == "opaque-raw-fingerprint"
        )
    )


async def test_completion_fails_closed_if_raw_provenance_changes(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    monkeypatch.setattr(gateway, "now_utc", lambda: NOW)
    await _configure_gateway(db_session, legacy_owner_roots)
    first_raw = await _raw(db_session, legacy_owner_roots, suffix="sealed-one")
    second_raw = await _raw(db_session, legacy_owner_roots, suffix="sealed-two")
    await db_session.commit()
    reservation = await gateway.reserve_ai_invocation(
        db_session,
        identity=_identity(legacy_owner_roots),
        purpose=AIInvocationPurpose.QUESTION_REPLY,
        source=AIInvocationSource.TELEGRAM,
        model="synthetic/raw-model",
        idempotency_key="opaque-sealed-raw",
        reserved_cost_microunits=100,
        reserved_units=500,
        raw_payload_id=first_raw.id,
    )
    await db_session.commit()
    lease = await gateway.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _reference: "synthetic-secret",
    )
    await db_session.commit()
    completion = await gateway.dispatch_ai(
        lease,
        provider_call=lambda _request: asyncio.sleep(0, result="result"),
        usage_extractor=lambda _result: gateway.SanitizedAIUsage(),
    )

    invocation = await db_session.get(AIInvocation, reservation.invocation_id)
    assert invocation is not None
    invocation.raw_payload_id = second_raw.id
    await db_session.commit()
    with pytest.raises(gateway.AICapabilityError, match="provenance"):
        await gateway.finalize_ai_invocation(db_session, completion=completion)
    await db_session.rollback()


async def test_succeeded_raw_partial_unique_and_raw_delete_restrict(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    monkeypatch.setattr(gateway, "now_utc", lambda: NOW)
    root = await _configure_gateway(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, suffix="unique")
    first = _invocation(
        roots=legacy_owner_roots,
        root=root,
        raw_payload_id=raw.id,
        purpose=AIInvocationPurpose.SIGNAL_PARSE,
        key="opaque-success-one",
        status=AIInvocationStatus.SUCCEEDED,
    )
    db_session.add(first)
    await db_session.commit()
    raw_id = raw.id
    root_id = root.id

    db_session.add(
        _invocation(
            roots=legacy_owner_roots,
            root=root,
            raw_payload_id=raw_id,
            purpose=AIInvocationPurpose.SIGNAL_PARSE,
            key="opaque-success-two",
            status=AIInvocationStatus.SUCCEEDED,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    root = await db_session.get(PlatformIntegrationConnection, root_id)
    assert root is not None

    db_session.add(
        _invocation(
            roots=legacy_owner_roots,
            root=root,
            raw_payload_id=raw_id,
            purpose=AIInvocationPurpose.SIGNAL_PARSE,
            key="opaque-failed-retry",
            status=AIInvocationStatus.FAILED,
        )
    )
    await db_session.commit()
    raw = await db_session.get(RawPayload, raw_id)
    assert raw is not None
    await db_session.delete(raw)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_notification_and_alert_links_enforce_exact_subject_and_shapes(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    monkeypatch.setattr(gateway, "now_utc", lambda: NOW)
    root = await _configure_gateway(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, suffix="journal")
    invocation = _invocation(
        roots=legacy_owner_roots,
        root=root,
        raw_payload_id=raw.id,
        purpose=AIInvocationPurpose.QUESTION_REPLY,
        key="opaque-journal",
        status=AIInvocationStatus.SUCCEEDED,
    )
    db_session.add(invocation)
    await db_session.commit()
    invocation_id = invocation.id
    telegram_id = await _telegram_connection_id(
        db_session, legacy_owner_roots.subject_id
    )

    notification = Notification(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=None,
        recipient_user_id=legacy_owner_roots.user_id,
        integration_connection_id=telegram_id,
        ai_invocation_id=invocation_id,
        category="reply",
        channel="telegram",
        external_id="opaque-message-1",
        payload={"synthetic": True},
    )
    db_session.add(notification)
    db_session.add_all(
        (
            SystemAlert(
                subject_id=legacy_owner_roots.subject_id,
                integration_connection_id=None,
                ai_invocation_id=invocation_id,
                domain="signals",
                severity="warn",
                message="Sanitized parser failure",
                alert_key="signal_parser_failed",
                entity_ref=f"raw_payload:{raw.id}",
            ),
            SystemAlert(
                subject_id=legacy_owner_roots.subject_id,
                integration_connection_id=None,
                ai_invocation_id=invocation_id,
                domain="signals",
                severity="warn",
                message="Sanitized parser failure",
                alert_key="signal_parser_failed",
                entity_ref=f"raw_payload:{raw.id}:retry",
            ),
        )
    )
    await db_session.commit()

    duplicate = Notification(
        subject_id=legacy_owner_roots.subject_id,
        recipient_user_id=legacy_owner_roots.user_id,
        integration_connection_id=telegram_id,
        ai_invocation_id=invocation_id,
        category="echo",
        channel="telegram",
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    malformed = Notification(
        subject_id=legacy_owner_roots.subject_id,
        recipient_user_id=legacy_owner_roots.user_id,
        integration_connection_id=telegram_id,
        ai_invocation_id=uuid.uuid4(),
        category="brief",
        channel="telegram",
    )
    db_session.add(malformed)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    malformed_alert = SystemAlert(
        subject_id=legacy_owner_roots.subject_id,
        integration_connection_id=telegram_id,
        ai_invocation_id=invocation_id,
        domain="signals",
        severity="warn",
        message="Sanitized parser failure",
        alert_key="signal_parser_failed",
        entity_ref="raw_payload:invalid",
    )
    db_session.add(malformed_alert)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_notification_and_alert_links_reject_cross_subject_invocation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    monkeypatch.setattr(gateway, "now_utc", lambda: NOW)
    root = await _configure_gateway(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, suffix="cross-subject")
    invocation = _invocation(
        roots=legacy_owner_roots,
        root=root,
        raw_payload_id=raw.id,
        purpose=AIInvocationPurpose.QUESTION_REPLY,
        key="opaque-cross-subject-journal",
        status=AIInvocationStatus.SUCCEEDED,
    )
    foreign_user = User(
        username="journal-foreign-owner",
        normalized_username="journal-foreign-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add_all((invocation, foreign_user))
    await db_session.flush()
    foreign_subject = HealthSubject(
        owner_user_id=foreign_user.id,
        display_name="Synthetic foreign journal subject",
        timezone="UTC",
    )
    db_session.add(foreign_subject)
    await db_session.commit()
    invocation_id = invocation.id
    foreign_user_id = foreign_user.id
    foreign_subject_id = foreign_subject.id
    telegram_id = await _telegram_connection_id(
        db_session, legacy_owner_roots.subject_id
    )

    db_session.add(
        Notification(
            subject_id=foreign_subject_id,
            recipient_user_id=foreign_user_id,
            integration_connection_id=telegram_id,
            ai_invocation_id=invocation_id,
            category="reply",
            channel="telegram",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    db_session.add(
        SystemAlert(
            subject_id=foreign_subject_id,
            integration_connection_id=None,
            ai_invocation_id=invocation_id,
            domain="signals",
            severity="warn",
            message="Sanitized parser failure",
            alert_key="signal_parser_failed",
            entity_ref="raw_payload:foreign",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


def _pre_0042_schema(connection) -> None:
    metadata = sa.MetaData()
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
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("id", "subject_id", name="uq_ai_invocations_id_subject"),
    )
    sa.Table(
        "notifications",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_user_id", sa.Uuid(), nullable=True),
        sa.Column("integration_connection_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
    )
    sa.Table(
        "system_alerts",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("integration_connection_id", sa.Uuid(), nullable=True),
        sa.Column("alert_key", sa.String(128), nullable=False),
        sa.Column("entity_ref", sa.String(128), nullable=False),
    )
    metadata.create_all(connection)


def _migration(monkeypatch, connection):
    migration = importlib.import_module(
        "migrations.versions.0042_signal_ai_provenance"
    )
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _assert_migrated_contract(connection) -> None:
    inspector = inspect(connection)
    assert "raw_payload_id" in {
        column["name"] for column in inspector.get_columns("ai_invocations")
    }
    assert "ai_invocation_id" in {
        column["name"] for column in inspector.get_columns("notifications")
    }
    assert "ai_invocation_id" in {
        column["name"] for column in inspector.get_columns("system_alerts")
    }
    assert {
        "ix_ai_invocations_raw_purpose_created",
        "uq_ai_invocations_raw_purpose_succeeded",
    } <= {
        item["name"] for item in inspector.get_indexes("ai_invocations")
    }
    assert "fk_ai_invocations_raw_payload_subject" in {
        item["name"] for item in inspector.get_foreign_keys("ai_invocations")
    }
    assert "fk_notifications_ai_invocation_subject" in {
        item["name"] for item in inspector.get_foreign_keys("notifications")
    }
    assert "fk_system_alerts_ai_invocation_subject" in {
        item["name"] for item in inspector.get_foreign_keys("system_alerts")
    }


def test_sqlite_0042_empty_roundtrip_preserves_legacy_rows(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _pre_0042_schema(connection)
            invocations = sa.Table(
                "ai_invocations", sa.MetaData(), autoload_with=connection
            )
            connection.execute(
                invocations.insert().values(
                    id=uuid.uuid4().hex,
                    subject_id=uuid.uuid4().hex,
                    purpose="weekly_digest",
                    status="prepared",
                )
            )
            migration = _migration(monkeypatch, connection)
            migration.upgrade()
            _assert_migrated_contract(connection)
            migration.downgrade()
            inspector = inspect(connection)
            assert "raw_payload_id" not in {
                item["name"] for item in inspector.get_columns("ai_invocations")
            }
            assert "ai_invocation_id" not in {
                item["name"] for item in inspector.get_columns("notifications")
            }
            assert "ai_invocation_id" not in {
                item["name"] for item in inspector.get_columns("system_alerts")
            }
            restored = sa.Table(
                "ai_invocations", sa.MetaData(), autoload_with=connection
            )
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(restored)
            ) == 1
    finally:
        engine.dispose()


def test_sqlite_0042_upgrade_guard_precedes_all_ddl(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _pre_0042_schema(connection)
            invocations = sa.Table(
                "ai_invocations", sa.MetaData(), autoload_with=connection
            )
            connection.execute(
                invocations.insert().values(
                    id=uuid.uuid4().hex,
                    subject_id=uuid.uuid4().hex,
                    purpose="signal_parse",
                    status="prepared",
                )
            )
            before = {
                table: tuple(
                    column["name"] for column in inspect(connection).get_columns(table)
                )
                for table in ("ai_invocations", "notifications", "system_alerts")
            }
            migration = _migration(monkeypatch, connection)
            with pytest.raises(RuntimeError) as exc_info:
                migration.upgrade()
            assert str(exc_info.value) == UPGRADE_REFUSAL
            assert {
                table: tuple(
                    column["name"] for column in inspect(connection).get_columns(table)
                )
                for table in ("ai_invocations", "notifications", "system_alerts")
            } == before
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(invocations)
            ) == 1
    finally:
        engine.dispose()


def _insert_migration_invocation(
    connection,
    tables,
    *,
    subject_id,
    purpose="weekly_digest",
    status="prepared",
    raw_payload_id=None,
):
    invocation_id = uuid.uuid4().hex
    connection.execute(
        tables["ai_invocations"].insert().values(
            id=invocation_id,
            subject_id=subject_id,
            purpose=purpose,
            status=status,
            raw_payload_id=raw_payload_id,
        )
    )
    return invocation_id


@pytest.mark.parametrize(
    "incompatibility",
    ("raw", "notification", "alert", "platform_alert"),
)
def test_sqlite_0042_each_downgrade_guard_is_nondestructive(
    monkeypatch,
    incompatibility,
):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            _pre_0042_schema(connection)
            migration = _migration(monkeypatch, connection)
            migration.upgrade()
            tables = {
                name: sa.Table(name, sa.MetaData(), autoload_with=connection)
                for name in (
                    "raw_payloads",
                    "ai_invocations",
                    "notifications",
                    "system_alerts",
                )
            }
            subject_id = uuid.uuid4().hex
            if incompatibility == "raw":
                connection.execute(
                    tables["raw_payloads"].insert().values(
                        id=1,
                        subject_id=subject_id,
                    )
                )
                _insert_migration_invocation(
                    connection,
                    tables,
                    subject_id=subject_id,
                    purpose="signal_parse",
                    raw_payload_id=1,
                )
                tracked = tables["ai_invocations"]
            elif incompatibility == "notification":
                invocation_id = _insert_migration_invocation(
                    connection,
                    tables,
                    subject_id=subject_id,
                )
                connection.execute(
                    tables["notifications"].insert().values(
                        id=1,
                        subject_id=subject_id,
                        recipient_user_id=uuid.uuid4().hex,
                        integration_connection_id=uuid.uuid4().hex,
                        channel="telegram",
                        category="reply",
                        ai_invocation_id=invocation_id,
                    )
                )
                tracked = tables["notifications"]
            elif incompatibility == "alert":
                invocation_id = _insert_migration_invocation(
                    connection,
                    tables,
                    subject_id=subject_id,
                )
                connection.execute(
                    tables["system_alerts"].insert().values(
                        id=1,
                        subject_id=subject_id,
                        integration_connection_id=None,
                        alert_key="signal_parser_failed",
                        entity_ref="raw_payload:1",
                        ai_invocation_id=invocation_id,
                    )
                )
                tracked = tables["system_alerts"]
            else:
                connection.execute(
                    tables["system_alerts"].insert().values(
                        id=1,
                        subject_id=subject_id,
                        integration_connection_id=None,
                        alert_key="signal_parser_failed",
                        entity_ref="raw_payload:1",
                        ai_invocation_id=None,
                    )
                )
                tracked = tables["system_alerts"]

            with pytest.raises(RuntimeError) as exc_info:
                migration.downgrade()
            assert str(exc_info.value) == DOWNGRADE_REFUSALS[incompatibility]
            _assert_migrated_contract(connection)
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(tracked)
            ) == 1
    finally:
        engine.dispose()


def test_sqlite_0042_migration_enforces_raw_fk_check_and_partial_unique(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            _pre_0042_schema(connection)
            migration = _migration(monkeypatch, connection)
            migration.upgrade()
            tables = {
                name: sa.Table(name, sa.MetaData(), autoload_with=connection)
                for name in ("raw_payloads", "ai_invocations")
            }
            first_subject = uuid.uuid4().hex
            second_subject = uuid.uuid4().hex
            connection.execute(
                tables["raw_payloads"].insert().values(
                    id=1,
                    subject_id=first_subject,
                )
            )
            _insert_migration_invocation(
                connection,
                tables,
                subject_id=first_subject,
                purpose="signal_parse",
                status="succeeded",
                raw_payload_id=1,
            )

            with connection.begin_nested(), pytest.raises(IntegrityError):
                _insert_migration_invocation(
                    connection,
                    tables,
                    subject_id=first_subject,
                    purpose="signal_parse",
                    status="succeeded",
                    raw_payload_id=1,
                )
            with connection.begin_nested(), pytest.raises(IntegrityError):
                _insert_migration_invocation(
                    connection,
                    tables,
                    subject_id=second_subject,
                    purpose="question_reply",
                    raw_payload_id=1,
                )
            with connection.begin_nested(), pytest.raises(IntegrityError):
                _insert_migration_invocation(
                    connection,
                    tables,
                    subject_id=first_subject,
                    purpose="weekly_digest",
                    raw_payload_id=1,
                )
            _insert_migration_invocation(
                connection,
                tables,
                subject_id=first_subject,
                purpose="signal_parse",
                status="failed",
                raw_payload_id=1,
            )
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_postgres_0042_empty_downgrade_upgrade_roundtrip(
    db_session,
    monkeypatch,
):
    connection = await db_session.connection()

    def roundtrip(sync_connection):
        migration = _migration(monkeypatch, sync_connection)
        migration.downgrade()
        inspector = inspect(sync_connection)
        assert "raw_payload_id" not in {
            item["name"] for item in inspector.get_columns("ai_invocations")
        }
        assert "ai_invocation_id" not in {
            item["name"] for item in inspector.get_columns("notifications")
        }
        assert "ai_invocation_id" not in {
            item["name"] for item in inspector.get_columns("system_alerts")
        }
        migration.upgrade()
        _assert_migrated_contract(sync_connection)

    await connection.run_sync(roundtrip)


@pytest.mark.integration
@pytest.mark.parametrize("incompatibility", tuple(DOWNGRADE_REFUSALS))
async def test_postgres_0042_downgrade_guards_preserve_schema_and_data(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    incompatibility,
):
    monkeypatch.setattr(gateway, "now_utc", lambda: NOW)
    if incompatibility == "platform_alert":
        row = SystemAlert(
            subject_id=legacy_owner_roots.subject_id,
            integration_connection_id=None,
            ai_invocation_id=None,
            domain="signals",
            severity="warn",
            message="Sanitized parser failure",
            alert_key="signal_parser_failed",
            entity_ref="raw_payload:1",
        )
    else:
        root = await _configure_gateway(db_session, legacy_owner_roots)
        if incompatibility == "raw":
            raw = await _raw(db_session, legacy_owner_roots, suffix="pg-raw")
            row = _invocation(
                roots=legacy_owner_roots,
                root=root,
                raw_payload_id=raw.id,
                purpose=AIInvocationPurpose.SIGNAL_PARSE,
                key="opaque-pg-raw",
            )
        else:
            invocation = _invocation(
                roots=legacy_owner_roots,
                root=root,
                raw_payload_id=None,
                purpose=AIInvocationPurpose.DAILY_BRIEF,
                key=f"opaque-pg-{incompatibility}",
                status=AIInvocationStatus.SUCCEEDED,
            )
            db_session.add(invocation)
            await db_session.flush()
            if incompatibility == "notification":
                row = Notification(
                    subject_id=legacy_owner_roots.subject_id,
                    recipient_user_id=legacy_owner_roots.user_id,
                    integration_connection_id=await _telegram_connection_id(
                        db_session, legacy_owner_roots.subject_id
                    ),
                    ai_invocation_id=invocation.id,
                    category="reply",
                    channel="telegram",
                )
            else:
                row = SystemAlert(
                    subject_id=legacy_owner_roots.subject_id,
                    integration_connection_id=None,
                    ai_invocation_id=invocation.id,
                    domain="signals",
                    severity="warn",
                    message="Sanitized parser failure",
                    alert_key="signal_parser_failed",
                    entity_ref="raw_payload:1",
                )
    db_session.add(row)
    await db_session.commit()
    row_id = row.id
    table_name = row.__tablename__
    connection = await db_session.connection()

    def assert_guard(sync_connection):
        migration = _migration(monkeypatch, sync_connection)
        with pytest.raises(RuntimeError) as exc_info:
            migration.downgrade()
        assert str(exc_info.value) == DOWNGRADE_REFUSALS[incompatibility]
        _assert_migrated_contract(sync_connection)
        table = sa.Table(table_name, sa.MetaData(), autoload_with=sync_connection)
        assert sync_connection.scalar(
            sa.select(sa.func.count()).select_from(table).where(table.c.id == row_id)
        ) == 1

    await connection.run_sync(assert_guard)
