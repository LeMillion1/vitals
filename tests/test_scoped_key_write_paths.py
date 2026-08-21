"""Stage-5D: two people, and two accounts, may finally share a natural key.

Every one of these shapes was impossible while the installation-wide unique
keys stood: one weight per date, one lab-marker name, one Garmin activity id,
one unresolved alert per key. Each write path now resolves its key inside its
own scope, and the scoped unique keys are what the database enforces, so the
rows below coexist — which is exactly what a second subject needs.

Each case also asserts that the other scope's row was neither read into the
write path nor mutated.
"""

from __future__ import annotations

from datetime import date

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


async def test_two_subjects_answer_the_same_day(db_session):
    _owner_a, subject_a, _connection_a = await _graph(db_session, "context-owner-a")
    owner_b, subject_b, _connection_b = await _graph(db_session, "context-owner-b")
    theirs = DayContext(
        subject_id=subject_a.id,
        date=DAY,
        domain=Domain.SIGNALS.value,
        source=Source.MANUAL.value,
        answers={"gym": True},
    )
    db_session.add(theirs)
    await db_session.flush()

    mine = await signals_service.set_day_context(
        db_session,
        DAY,
        answers={"gym": False},
        identity=WriteIdentity(subject_b.id, owner_b.id),
    )

    assert mine.subject_id == subject_b.id
    assert mine.answers == {"gym": False}
    # The other subject's day was never read into this write.
    assert theirs.subject_id == subject_a.id
    assert theirs.answers == {"gym": True}
    assert await db_session.scalar(select(func.count()).select_from(DayContext)) == 2


async def test_two_garmin_accounts_report_the_same_day(db_session):
    owner_a, subject_a, connection_a = await _graph(db_session, "daily-owner-a")
    _owner_b, subject_b, connection_b = await _graph(db_session, "daily-owner-b")
    theirs = GarminDaily(
        subject_id=subject_b.id,
        integration_connection_id=connection_b.id,
        date=DAY,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        steps=11,
    )
    db_session.add(theirs)
    await db_session.flush()

    mine = await garmin_service.ingest_owned_daily(
        db_session,
        DAY,
        {"summary": {"totalSteps": 4321}},
        identity=WriteIdentity(subject_a.id, owner_a.id),
        integration_connection_id=connection_a.id,
    )

    assert mine.integration_connection_id == connection_a.id
    assert mine.steps == 4321
    assert theirs.integration_connection_id == connection_b.id
    assert theirs.steps == 11
    assert await db_session.scalar(select(func.count()).select_from(GarminDaily)) == 2


async def test_two_accounts_raise_the_same_provider_alert(db_session):
    _owner_a, subject_a, connection_a = await _graph(db_session, "alert-owner-a")
    owner_b, subject_b, connection_b = await _graph(db_session, "alert-owner-b")
    theirs = SystemAlert(
        subject_id=subject_a.id,
        integration_connection_id=connection_a.id,
        domain=Domain.GARMIN.value,
        severity=Severity.WARN.value,
        message="subject A's account needs attention",
        alert_key="garmin.auth",
        entity_ref="",
    )
    db_session.add(theirs)
    await db_session.flush()

    mine = await alerts_service.raise_scoped_alert(
        db_session,
        context=alerts_service.ProviderAlertContext(
            identity=WriteIdentity(subject_b.id, owner_b.id),
            integration_connection_id=connection_b.id,
            provider=IntegrationProvider.GARMIN,
        ),
        domain=Domain.GARMIN,
        severity=Severity.WARN,
        message="subject B's account needs attention",
        alert_key="garmin.auth",
    )

    assert mine.integration_connection_id == connection_b.id
    assert mine.message == "subject B's account needs attention"
    assert theirs.integration_connection_id == connection_a.id
    assert theirs.message == "subject A's account needs attention"
    assert theirs.resolved_at is None


async def test_a_subjects_compound_may_reuse_a_curated_key(db_session):
    _owner, subject, _connection = await _graph(db_session, "compound-owner")
    theirs = HrtCompound(
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
    db_session.add(theirs)
    await db_session.flush()

    # The catalog cannot see a subject's own compound at all, so it seeds the
    # curated definition beside it instead of refusing or overwriting it.
    result = await hrt_catalog.sync_catalog(db_session)
    assert result["inserted"] > 0

    assert theirs.name == "A subject's own definition"
    assert theirs.source == Source.MANUAL.value
    curated = await db_session.scalar(
        select(HrtCompound).where(
            HrtCompound.key == "testosterone_enanthate",
            HrtCompound.subject_id.is_(None),
        )
    )
    assert curated is not None and curated.id != theirs.id


async def test_a_subjects_rule_may_reuse_a_curated_code(db_session):
    _owner, subject, _connection = await _graph(db_session, "rule-owner")
    entry = conflict_catalog.load_rule_catalog()[0]
    theirs = ConflictRule(
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
    db_session.add(theirs)
    await db_session.flush()

    result = await conflict_catalog.sync_catalog(db_session)
    assert result["inserted"] > 0

    assert theirs.message == "a subject's own rule"
    curated = await db_session.scalar(
        select(ConflictRule).where(
            ConflictRule.code == entry["code"],
            ConflictRule.subject_id.is_(None),
        )
    )
    assert curated is not None and curated.id != theirs.id


async def test_a_subjects_compound_still_cannot_squat_an_unowned_curated_key(
    db_session,
):
    """The catalog's own half of the key is still exactly one row."""

    unowned = HrtCompound(
        subject_id=None,
        domain="hrt",
        source=Source.MANUAL.value,
        key="testosterone_enanthate",
        name="An unowned manual definition",
        compound_class="testosterone",
        route="intramuscular",
        dose_unit="mg",
        half_life_hours=1.0,
        active_fraction=1.0,
    )
    db_session.add(unowned)
    await db_session.flush()

    with pytest.raises(hrt_catalog.HrtCatalogCollisionError):
        await hrt_catalog.sync_catalog(db_session)

    assert unowned.name == "An unowned manual definition"
