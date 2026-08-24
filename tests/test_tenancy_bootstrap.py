"""Focused stage-0 tests for harmless legacy tenancy roots."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.identity import AuditEvent, HealthSubject, User
from vitals.models.tenancy import IntegrationConnection
from vitals.services.tenancy_bootstrap import (
    LEGACY_ACCOUNT_DISCRIMINATOR,
    LegacySubjectNotFoundError,
    bootstrap_legacy_resource_roots,
)

_EXPECTED_TYPES = {
    IntegrationProvider.GARMIN: IntegrationConnectionType.ACCOUNT,
    IntegrationProvider.HEVY: IntegrationConnectionType.ACCOUNT,
    IntegrationProvider.OPENROUTER: IntegrationConnectionType.AI_GATEWAY,
    IntegrationProvider.TELEGRAM: IntegrationConnectionType.RECIPIENT,
}


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _subject(session: AsyncSession, slug: str = "legacy-owner") -> HealthSubject:
    owner = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name="Synthetic owner",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return subject


@pytest.mark.asyncio
async def test_bootstrap_creates_exact_legacy_roots_and_one_safe_audit(db_session):
    subject = await _subject(db_session)

    result = await bootstrap_legacy_resource_roots(
        db_session,
        subject_id=subject.id,
        adopt_environment_credentials=True,
    )

    rows = list(
        await db_session.scalars(
            select(IntegrationConnection).order_by(IntegrationConnection.provider)
        )
    )
    event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "tenancy.legacy_resource_roots.bootstrap"
        )
    )

    assert result.changed is True
    assert result.subject_id == subject.id
    assert len(result.created_connection_ids) == 4
    assert result.created_providers == frozenset(_EXPECTED_TYPES)
    assert result.skipped_providers == frozenset()
    assert len(rows) == 4
    for row in rows:
        provider = IntegrationProvider(row.provider)
        assert row.subject_id == subject.id
        assert row.connection_type == _EXPECTED_TYPES[provider].value
        assert row.external_account_discriminator == LEGACY_ACCOUNT_DISCRIMINATOR
        # ``adopt_environment_credentials=True`` below, so all four claim the
        # environment. Without it Garmin and Hevy start with no ref at all —
        # they describe a person, and ``.env`` describes the installation.
        assert row.credential_ref == f"legacy_env:{provider.value}"
        assert row.status == IntegrationConnectionStatus.LEGACY.value
        assert row.retired_at is None

    assert event is not None
    assert result.audit_event_id == event.id
    assert event.actor_user_id is None
    assert event.subject_id == subject.id
    assert event.resource_type == "health_subject"
    assert event.resource_id == str(subject.id)
    assert event.metadata_json == {
        "source_surface": "startup",
        "result_code": "legacy_connection_roots_created",
        "changed_fields": [
            "integration_connections.garmin",
            "integration_connections.hevy",
            "integration_connections.openrouter",
            "integration_connections.telegram",
        ],
        "record_count": 4,
    }
    assert "credential" not in str(event.metadata_json).casefold()
    assert "legacy_env" not in str(event.metadata_json)


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent_and_does_not_audit_a_noop(db_session):
    subject = await _subject(db_session)

    first = await bootstrap_legacy_resource_roots(db_session, subject_id=subject.id)
    second = await bootstrap_legacy_resource_roots(db_session, subject_id=subject.id)

    assert first.changed is True
    assert second.changed is False
    assert second.created_connection_ids == ()
    assert second.created_providers == frozenset()
    assert second.skipped_providers == frozenset(_EXPECTED_TYPES)
    assert await _count(db_session, IntegrationConnection) == 4
    assert await _count(db_session, AuditEvent) == 1


@pytest.mark.asyncio
async def test_bootstrap_preserves_active_connection_and_skips_its_pair(db_session):
    subject = await _subject(db_session)
    existing = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator="opaque-real-account-v2",
        credential_ref="secret-store:connection/immutable-test-ref",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(existing)
    await db_session.flush()
    before = (
        existing.id,
        existing.external_account_discriminator,
        existing.credential_ref,
        existing.status,
        existing.retired_at,
    )

    result = await bootstrap_legacy_resource_roots(db_session, subject_id=subject.id)
    await db_session.refresh(existing)

    assert IntegrationProvider.GARMIN in result.skipped_providers
    assert IntegrationProvider.GARMIN not in result.created_providers
    assert len(result.created_connection_ids) == 3
    assert (
        existing.id,
        existing.external_account_discriminator,
        existing.credential_ref,
        existing.status,
        existing.retired_at,
    ) == before
    garmin_rows = list(
        await db_session.scalars(
            select(IntegrationConnection).where(
                IntegrationConnection.subject_id == subject.id,
                IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
                IntegrationConnection.connection_type
                == IntegrationConnectionType.ACCOUNT.value,
            )
        )
    )
    assert garmin_rows == [existing]


@pytest.mark.asyncio
async def test_bootstrap_missing_subject_fails_before_any_write(db_session):
    missing_id = uuid.uuid4()

    with pytest.raises(LegacySubjectNotFoundError, match=str(missing_id)):
        await bootstrap_legacy_resource_roots(db_session, subject_id=missing_id)

    assert await _count(db_session, IntegrationConnection) == 0
    assert await _count(db_session, AuditEvent) == 0


@pytest.mark.asyncio
async def test_bootstrap_is_flush_only(db_session):
    subject = await _subject(db_session)
    subject_id = subject.id
    await db_session.commit()

    await bootstrap_legacy_resource_roots(db_session, subject_id=subject_id)
    await db_session.rollback()

    assert await _count(db_session, IntegrationConnection) == 0
    assert await _count(db_session, AuditEvent) == 0
    assert await db_session.get(HealthSubject, subject_id) is not None


@pytest.mark.asyncio
async def test_bootstrap_persists_only_fixed_non_secret_sentinels(
    db_session, monkeypatch
):
    subject = await _subject(db_session)
    synthetic_secret = "must-never-reach-a-tenancy-row"
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", synthetic_secret)
    monkeypatch.setenv("VITALS_HEVY_API_KEY", synthetic_secret)
    monkeypatch.setenv("VITALS_OPENROUTER_API_KEY", synthetic_secret)
    monkeypatch.setenv("VITALS_TELEGRAM_BOT_TOKEN", synthetic_secret)

    await bootstrap_legacy_resource_roots(
        db_session, subject_id=subject.id, adopt_environment_credentials=True
    )

    rows = list(await db_session.scalars(select(IntegrationConnection)))
    audit = await db_session.scalar(select(AuditEvent))
    persisted = repr(
        [
            {
                "provider": row.provider,
                "connection_type": row.connection_type,
                "discriminator": row.external_account_discriminator,
                "credential_ref": row.credential_ref,
            }
            for row in rows
        ]
        + ([audit.metadata_json] if audit is not None else [])
    )

    assert synthetic_secret not in persisted
    assert {row.credential_ref for row in rows} == {
        f"legacy_env:{provider.value}" for provider in _EXPECTED_TYPES
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_concurrent_bootstrap_creates_one_root_per_mapping(db_session):
    subject = await _subject(db_session)
    subject_id = subject.id
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def run_bootstrap():
        async with factory() as session:
            result = await bootstrap_legacy_resource_roots(
                session,
                subject_id=subject_id,
            )
            await session.commit()
            return result

    first, second = await asyncio.gather(run_bootstrap(), run_bootstrap())

    assert sum(result.changed for result in (first, second)) == 1
    async with factory() as session:
        assert await _count(session, IntegrationConnection) == 4
        assert await _count(session, AuditEvent) == 1
