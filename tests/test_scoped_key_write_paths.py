"""Stage-5C: every key-based write path resolves inside its own scope.

Each of these paths used to look a natural key up across the whole
installation and then check afterwards whether the row it found happened to
belong to the caller. They now look the key up *inside* the caller's scope, and
a row outside it is reported through a typed error without being read into the
write path or mutated. Until the legacy global keys are dropped, that report
also stands in for the integrity error the surviving global key would raise.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Severity,
    Source,
    UserStatus,
)
from vitals.models.conflict_rule import ConflictRule
from vitals.models.garmin import GarminDaily
from vitals.models.hrt import HrtCompound
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import DayContext
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import (
    alerts_service,
    conflict_catalog,
    garmin_service,
    hrt_catalog,
    signals_service,
)


DAY = date(2026, 8, 19)


async def _graph(
    session,
    slug: str,
    *,
    provider: IntegrationProvider = IntegrationProvider.GARMIN,
):
    owner = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider=provider.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=f"opaque-{slug}",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
    return owner, subject, connection


async def test_day_context_upsert_never_reads_another_subjects_day(db_session):
    _owner_a, subject_a, _connection_a = await _graph(db_session, "context-owner-a")
    owner_b, subject_b, _connection_b = await _graph(db_session, "context-owner-b")
    foreign = DayContext(
        subject_id=subject_a.id,
        date=DAY,
        domain=Domain.SIGNALS.value,
        source=Source.MANUAL.value,
        answers={"gym": True},
    )
    db_session.add(foreign)
    await db_session.flush()

    with pytest.raises(signals_service.SignalOwnershipError):
        await signals_service.set_day_context(
            db_session,
            DAY,
            answers={"gym": False},
            identity=WriteIdentity(subject_b.id, owner_b.id),
        )

    assert foreign.subject_id == subject_a.id
    assert foreign.answers == {"gym": True}
    assert await db_session.scalar(select(func.count()).select_from(DayContext)) == 1


async def test_garmin_daily_upsert_never_reads_another_connections_day(db_session):
    owner_a, subject_a, connection_a = await _graph(db_session, "daily-owner-a")
    _owner_b, subject_b, connection_b = await _graph(db_session, "daily-owner-b")
    foreign = GarminDaily(
        subject_id=subject_b.id,
        integration_connection_id=connection_b.id,
        date=DAY,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        steps=11,
    )
    db_session.add(foreign)
    await db_session.flush()

    with pytest.raises(garmin_service.GarminOwnershipConflictError):
        await garmin_service.ingest_owned_daily(
            db_session,
            DAY,
            {"summary": {"totalSteps": 4321}},
            identity=WriteIdentity(subject_a.id, owner_a.id),
            integration_connection_id=connection_a.id,
        )

    # The conflict is reported before raw ingestion writes anything.
    assert foreign.steps == 11
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0


async def test_provider_alert_never_reads_another_connections_alert(db_session):
    _owner_a, subject_a, connection_a = await _graph(db_session, "alert-owner-a")
    owner_b, subject_b, connection_b = await _graph(db_session, "alert-owner-b")
    foreign = SystemAlert(
        subject_id=subject_a.id,
        integration_connection_id=connection_a.id,
        domain=Domain.GARMIN.value,
        severity=Severity.WARN.value,
        message="subject A's account needs attention",
        alert_key="garmin.auth",
        entity_ref="",
    )
    db_session.add(foreign)
    await db_session.flush()

    context = alerts_service.ProviderAlertContext(
        identity=WriteIdentity(subject_b.id, owner_b.id),
        integration_connection_id=connection_b.id,
        provider=IntegrationProvider.GARMIN,
    )
    with pytest.raises(alerts_service.AlertScopedUniqueCutoverRequiredError):
        await alerts_service.raise_scoped_alert(
            db_session,
            context=context,
            domain=Domain.GARMIN,
            severity=Severity.WARN,
            message="subject B's account needs attention",
            alert_key="garmin.auth",
        )

    assert foreign.subject_id == subject_a.id
    assert foreign.integration_connection_id == connection_a.id
    assert foreign.message == "subject A's account needs attention"


async def test_compound_catalog_never_reads_a_subjects_own_compound(db_session):
    _owner, subject, _connection = await _graph(db_session, "compound-owner")
    protected = HrtCompound(
        subject_id=subject.id,
        domain="hrt",
        source=Source.MANUAL.value,
        key="testosterone_enanthate",
        name="A subject's own definition",
        compound_class="testosterone",
        route="intramuscular",
        dose_unit="mg",
        half_life_hours=1.0,
        active_fraction=1.0,
    )
    db_session.add(protected)
    await db_session.flush()

    with pytest.raises(hrt_catalog.HrtCatalogCollisionError):
        await hrt_catalog.sync_catalog(db_session)

    assert protected.name == "A subject's own definition"
    assert protected.source == Source.MANUAL.value


async def test_rule_catalog_never_reads_a_subjects_own_rule(db_session):
    _owner, subject, _connection = await _graph(db_session, "rule-owner")
    entry = conflict_catalog.load_rule_catalog()[0]
    protected = ConflictRule(
        subject_id=subject.id,
        code=entry["code"],
        rule_type=entry["rule_type"],
        domain_a=entry["domain_a"],
        condition_a=entry["condition_a"],
        domain_b=entry["domain_b"],
        condition_b=entry["condition_b"],
        severity=entry["severity"],
        message="a subject's own rule",
    )
    db_session.add(protected)
    await db_session.flush()

    with pytest.raises(conflict_catalog.ConflictCatalogCollisionError):
        await conflict_catalog.sync_catalog(db_session)

    assert protected.message == "a subject's own rule"
