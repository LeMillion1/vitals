"""Focused contract for the registration-disabled subject alert aggregate."""

from __future__ import annotations

from vitals.services.alerts import contracts as alerts_contracts

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Severity,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.services.alerts import legacy_subject as subject_alerts
from vitals.services.alerts import legacy_subject as subject_alerts_legacy
from vitals.services.tenancy.contracts import LegacyOwnershipContext
from vitals.utils.timeutils import now_local


_PASSWORD_HASH = "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
_CONNECTION_TYPES = {
    IntegrationProvider.GARMIN: IntegrationConnectionType.ACCOUNT,
    IntegrationProvider.HEVY: IntegrationConnectionType.ACCOUNT,
    IntegrationProvider.OPENROUTER: IntegrationConnectionType.AI_GATEWAY,
    IntegrationProvider.TELEGRAM: IntegrationConnectionType.RECIPIENT,
}


async def _user(session: AsyncSession, slug: str) -> User:
    row = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash=_PASSWORD_HASH,
        status=UserStatus.ACTIVE.value,
    )
    session.add(row)
    await session.flush()
    return row


async def _subject(
    session: AsyncSession,
    slug: str,
) -> tuple[User, HealthSubject]:
    owner = await _user(session, slug)
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=slug,
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return owner, subject


async def _connection(
    session: AsyncSession,
    subject: HealthSubject,
    provider: IntegrationProvider,
    *,
    connection_type: IntegrationConnectionType | None = None,
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.ACTIVE,
) -> IntegrationConnection:
    row = IntegrationConnection(
        subject_id=subject.id,
        provider=provider.value,
        connection_type=(connection_type or _CONNECTION_TYPES[provider]).value,
        external_account_discriminator=uuid.uuid4().hex,
        credential_ref=f"legacy_env:{provider.value}",
        status=status.value,
        retired_at=(
            now_local()
            if status is IntegrationConnectionStatus.RETIRED
            else None
        ),
    )
    session.add(row)
    await session.flush()
    return row


async def _roots(
    session: AsyncSession,
    subject: HealthSubject,
) -> dict[IntegrationProvider, IntegrationConnection]:
    return {
        provider: await _connection(session, subject, provider)
        for provider in IntegrationProvider
    }


def _ownership(
    owner: User,
    subject: HealthSubject,
    roots: dict[IntegrationProvider, IntegrationConnection],
    *,
    system: bool = False,
) -> LegacyOwnershipContext:
    return LegacyOwnershipContext(
        subject_id=subject.id,
        owner_user_id=owner.id,
        actor_user_id=None if system else owner.id,
        connection_ids={provider: row.id for provider, row in roots.items()},
    )


async def _alert(
    session: AsyncSession,
    *,
    key: str,
    domain: Domain,
    entity: str,
    subject_id: uuid.UUID | None = None,
    connection_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> SystemAlert:
    row = SystemAlert(
        created_at=created_at or datetime(2026, 1, 1, 12, 0),
        domain=domain.value,
        severity=Severity.WARN.value,
        message="synthetic alert",
        alert_key=key,
        entity_ref=entity,
        subject_id=subject_id,
        integration_connection_id=connection_id,
    )
    session.add(row)
    await session.flush()
    return row


async def test_list_aggregates_health_current_and_rotated_roots_in_newest_order(
    db_session: AsyncSession,
):
    owner, subject = await _subject(db_session, "owner")
    roots = await _roots(db_session, subject)
    retired_garmin = await _connection(
        db_session,
        subject,
        IntegrationProvider.GARMIN,
        status=IntegrationConnectionStatus.RETIRED,
    )
    pending_garmin = await _connection(
        db_session,
        subject,
        IntegrationProvider.GARMIN,
        status=IntegrationConnectionStatus.PENDING,
    )
    wrong_type_garmin = await _connection(
        db_session,
        subject,
        IntegrationProvider.GARMIN,
        connection_type=IntegrationConnectionType.IMPORT,
        status=IntegrationConnectionStatus.RETIRED,
    )
    base = datetime(2026, 1, 1, 12, 0)
    health = await _alert(
        db_session,
        key="weight.noisy_period_active",
        domain=Domain.WEIGHT,
        entity="health",
        subject_id=subject.id,
        created_at=base,
    )
    current = await _alert(
        db_session,
        key="garmin.auth",
        domain=Domain.GARMIN,
        entity="current",
        subject_id=subject.id,
        connection_id=roots[IntegrationProvider.GARMIN].id,
        created_at=base + timedelta(seconds=1),
    )
    rotated = await _alert(
        db_session,
        key="garmin.token_cache",
        domain=Domain.GARMIN,
        entity="retired",
        subject_id=subject.id,
        connection_id=retired_garmin.id,
        created_at=base + timedelta(seconds=2),
    )
    pending = await _alert(
        db_session,
        key="garmin.weight_export",
        domain=Domain.GARMIN,
        entity="pending",
        subject_id=subject.id,
        connection_id=pending_garmin.id,
        created_at=base + timedelta(seconds=3),
    )
    wrong_type = await _alert(
        db_session,
        key="scheduler.job_failed:garmin_sync",
        domain=Domain.SYSTEM,
        entity="wrong-type",
        subject_id=subject.id,
        connection_id=wrong_type_garmin.id,
        created_at=base + timedelta(seconds=4),
    )
    platform = await _alert(
        db_session,
        key="scheduler.job_failed:raw_payload_sweep",
        domain=Domain.SYSTEM,
        entity="platform",
        created_at=base + timedelta(seconds=5),
    )

    rows = await subject_alerts_legacy.list_active(
        db_session,
        ownership=_ownership(owner, subject, roots),
    )

    assert [row.id for row in rows] == [rotated.id, current.id, health.id]
    assert pending.id not in {row.id for row in rows}
    assert wrong_type.id not in {row.id for row in rows}
    assert platform.id not in {row.id for row in rows}
    ownership = _ownership(owner, subject, roots)
    assert await subject_alerts.resolve(
        db_session,
        pending.id,
        ownership=ownership,
    ) is None
    assert await subject_alerts.override(
        db_session,
        wrong_type.id,
        ownership=ownership,
    ) is None
    assert pending.resolved_at is None
    assert wrong_type.override_at is None


async def test_legacy_rows_are_read_without_adoption_then_bind_to_exact_roots(
    db_session: AsyncSession,
):
    owner, subject = await _subject(db_session, "owner")
    roots = await _roots(db_session, subject)
    health = await _alert(
        db_session,
        key="labs.out_of_range",
        domain=Domain.LABS,
        entity="legacy-health",
    )
    provider = await _alert(
        db_session,
        key="garmin.auth",
        domain=Domain.GARMIN,
        entity="legacy-provider",
    )
    ownership = _ownership(owner, subject, roots)

    listed = await subject_alerts_legacy.list_active(db_session, ownership=ownership)

    assert {row.id for row in listed} == {health.id, provider.id}
    assert health.subject_id is None
    assert health.integration_connection_id is None
    assert provider.subject_id is None
    assert provider.integration_connection_id is None

    resolved = await subject_alerts.resolve(
        db_session,
        health.id,
        ownership=ownership,
    )
    overridden = await subject_alerts.override(
        db_session,
        provider.id,
        ownership=ownership,
    )

    assert resolved is health
    assert health.subject_id == subject.id
    assert health.integration_connection_id is None
    assert health.resolved_by_user_id == owner.id
    assert overridden is provider
    assert provider.subject_id == subject.id
    assert provider.integration_connection_id == roots[IntegrationProvider.GARMIN].id
    assert provider.overridden_by_user_id == owner.id
    assert provider.resolved_at is None


async def test_owner_and_system_actor_semantics_and_non_enumerating_misses(
    db_session: AsyncSession,
):
    owner, subject = await _subject(db_session, "owner")
    roots = await _roots(db_session, subject)
    automatic = await _alert(
        db_session,
        key="weight.noisy_period_active",
        domain=Domain.WEIGHT,
        entity="automatic",
        subject_id=subject.id,
    )
    human_only = await _alert(
        db_session,
        key="labs.retest_due",
        domain=Domain.LABS,
        entity="human-only",
        subject_id=subject.id,
    )
    platform = await _alert(
        db_session,
        key="scheduler.job_failed:share_purge",
        domain=Domain.SYSTEM,
        entity="platform",
    )
    system = _ownership(owner, subject, roots, system=True)
    human = _ownership(owner, subject, roots)

    resolved = await subject_alerts.resolve(
        db_session,
        automatic.id,
        ownership=system,
    )

    assert resolved is automatic
    assert automatic.resolved_by_user_id is None
    with pytest.raises(alerts_contracts.AlertActorRequiredError):
        await subject_alerts.override(
            db_session,
            human_only.id,
            ownership=system,
        )
    assert human_only.override_at is None
    assert await subject_alerts.resolve(
        db_session,
        platform.id,
        ownership=human,
    ) is None
    assert await subject_alerts.override(
        db_session,
        platform.id,
        ownership=human,
    ) is None
    assert await subject_alerts_legacy.resolve_all(
        db_session,
        ownership=human,
        domain=Domain.SYSTEM,
    ) == 0
    assert platform.resolved_at is None
    assert await subject_alerts.resolve(
        db_session,
        999_999,
        ownership=human,
    ) is None


async def test_resolve_all_is_domain_scoped_and_excludes_non_subject_namespaces(
    db_session: AsyncSession,
):
    owner, subject = await _subject(db_session, "owner")
    roots = await _roots(db_session, subject)
    retired_hevy = await _connection(
        db_session,
        subject,
        IntegrationProvider.HEVY,
        status=IntegrationConnectionStatus.RETIRED,
    )
    weight = await _alert(
        db_session,
        key="weight.noisy_period_active",
        domain=Domain.WEIGHT,
        entity="weight",
        subject_id=subject.id,
    )
    labs = await _alert(
        db_session,
        key="labs.out_of_range",
        domain=Domain.LABS,
        entity="labs",
        subject_id=subject.id,
    )
    garmin = await _alert(
        db_session,
        key="garmin.auth",
        domain=Domain.GARMIN,
        entity="garmin",
        subject_id=subject.id,
        connection_id=roots[IntegrationProvider.GARMIN].id,
    )
    rotated_hevy = await _alert(
        db_session,
        key="hevy.sync_failed",
        domain=Domain.WORKOUTS,
        entity="retired-hevy",
        subject_id=subject.id,
        connection_id=retired_hevy.id,
    )
    platform = await _alert(
        db_session,
        key="scheduler.job_failed:share_purge",
        domain=Domain.SYSTEM,
        entity="platform",
    )
    ownership = _ownership(owner, subject, roots)

    assert [row.id for row in await subject_alerts_legacy.list_active(
        db_session,
        ownership=ownership,
        domain=Domain.WORKOUTS,
    )] == [rotated_hevy.id]
    assert [row.id for row in await subject_alerts_legacy.list_active(
        db_session,
        ownership=ownership,
        domain=Domain.GARMIN,
    )] == [garmin.id]

    assert await subject_alerts_legacy.resolve_all(
        db_session,
        ownership=ownership,
        domain=Domain.WEIGHT,
    ) == 1
    assert weight.resolved_by_user_id == owner.id
    assert labs.resolved_at is None
    assert await subject_alerts_legacy.resolve_all(
        db_session,
        ownership=ownership,
    ) == 3

    for row in (labs, garmin, rotated_hevy):
        assert row.resolved_at is not None
        assert row.resolved_by_user_id == owner.id
    assert platform.resolved_at is None


async def test_context_and_current_root_snapshot_are_validated_fail_closed(
    db_session: AsyncSession,
):
    owner, subject = await _subject(db_session, "owner")
    other = await _user(db_session, "other")
    roots = await _roots(db_session, subject)
    base_ids = {provider: row.id for provider, row in roots.items()}

    incomplete = LegacyOwnershipContext(
        subject_id=subject.id,
        owner_user_id=owner.id,
        actor_user_id=owner.id,
        connection_ids={
            provider: row.id
            for provider, row in roots.items()
            if provider is not IntegrationProvider.TELEGRAM
        },
    )
    with pytest.raises(subject_alerts.LegacySubjectAlertsContextError):
        await subject_alerts_legacy.list_active(db_session, ownership=incomplete)

    wrong_actor = LegacyOwnershipContext(
        subject_id=subject.id,
        owner_user_id=owner.id,
        actor_user_id=other.id,
        connection_ids=base_ids,
    )
    with pytest.raises(subject_alerts.LegacySubjectAlertsContextError):
        await subject_alerts_legacy.list_active(db_session, ownership=wrong_actor)

    forged_owner = LegacyOwnershipContext(
        subject_id=subject.id,
        owner_user_id=other.id,
        actor_user_id=other.id,
        connection_ids=base_ids,
    )
    with pytest.raises(subject_alerts.LegacySubjectAlertsContextError):
        await subject_alerts_legacy.list_active(db_session, ownership=forged_owner)

    duplicate_ids = dict(base_ids)
    duplicate_ids[IntegrationProvider.HEVY] = base_ids[IntegrationProvider.GARMIN]
    duplicate_roots = LegacyOwnershipContext(
        subject_id=subject.id,
        owner_user_id=owner.id,
        actor_user_id=owner.id,
        connection_ids=duplicate_ids,
    )
    with pytest.raises(subject_alerts.LegacySubjectAlertsContextError):
        await subject_alerts_legacy.list_active(db_session, ownership=duplicate_roots)

    roots[IntegrationProvider.GARMIN].status = IntegrationConnectionStatus.PENDING.value
    await db_session.flush()
    with pytest.raises(subject_alerts.LegacySubjectAlertsConnectionError):
        await subject_alerts_legacy.list_active(
            db_session,
            ownership=_ownership(owner, subject, roots),
        )


async def test_ambiguous_nonretired_provider_roots_fail_before_alert_mutation(
    db_session: AsyncSession,
):
    owner, subject = await _subject(db_session, "owner")
    roots = await _roots(db_session, subject)
    await _connection(
        db_session,
        subject,
        IntegrationProvider.GARMIN,
        status=IntegrationConnectionStatus.DISABLED,
    )
    row = await _alert(
        db_session,
        key="garmin.auth",
        domain=Domain.GARMIN,
        entity="unchanged",
        subject_id=subject.id,
        connection_id=roots[IntegrationProvider.GARMIN].id,
    )

    with pytest.raises(subject_alerts.LegacySubjectAlertsConnectionError):
        await subject_alerts_legacy.resolve_all(
            db_session,
            ownership=_ownership(owner, subject, roots),
        )

    assert row.resolved_at is None
    assert row.resolved_by_user_id is None


async def test_a_second_subject_does_not_leak_and_does_not_stop_the_first(
    db_session: AsyncSession,
):
    """Two people, every alert owned: each sees exactly their own.

    This used to assert that all three operations raised, because the bridge
    demanded a sole subject whenever it was requested — even here, where it has
    nothing to do: both rows carry an owner, so widening the query to unowned
    rows would add nothing. Refusing was safe but wrong, and it was not free:
    four screens answered 409 in a shared installation over a bridge that would
    have returned the same rows either way.

    What actually has to hold is below, and it held before and after: A's
    listing is A's, and A cannot resolve B's alert. The exception type was only
    ever a proxy for that.
    """

    owner_a, subject_a = await _subject(db_session, "owner-a")
    roots_a = await _roots(db_session, subject_a)
    _, subject_b = await _subject(db_session, "owner-b")
    row_a = await _alert(
        db_session,
        key="weight.noisy_period_active",
        domain=Domain.WEIGHT,
        entity="a",
        subject_id=subject_a.id,
    )
    row_b = await _alert(
        db_session,
        key="labs.out_of_range",
        domain=Domain.LABS,
        entity="b",
        subject_id=subject_b.id,
    )
    ownership_a = _ownership(owner_a, subject_a, roots_a)

    visible = await subject_alerts_legacy.list_active(db_session, ownership=ownership_a)
    assert [row.id for row in visible] == [row_a.id]

    assert (
        await subject_alerts.resolve(db_session, row_b.id, ownership=ownership_a)
    ) is None
    await subject_alerts_legacy.resolve_all(db_session, ownership=ownership_a)

    # A resolved A's own, and B's is untouched — the point of the whole test.
    assert row_b.resolved_at is None


async def test_an_unowned_alert_still_closes_the_bridge_for_two_subjects(
    db_session: AsyncSession,
):
    """The refusal is kept for the case it was written for.

    One alert with no owner and two people to give it to: nothing here can say
    whose it is, so every operation that would reach for it stops. This is what
    ``scripts/backfill_system_alert_subject_ownership.py`` is for, and it is
    meant to run while the installation is still one person.
    """

    owner_a, subject_a = await _subject(db_session, "owner-a")
    roots_a = await _roots(db_session, subject_a)
    await _subject(db_session, "owner-b")
    orphan = await _alert(
        db_session,
        key="weight.noisy_period_active",
        domain=Domain.WEIGHT,
        entity="orphan",
    )
    assert orphan.subject_id is None
    ownership_a = _ownership(owner_a, subject_a, roots_a)

    with pytest.raises(alerts_contracts.AlertLegacyBridgeError):
        await subject_alerts_legacy.list_active(db_session, ownership=ownership_a)
    with pytest.raises(alerts_contracts.AlertLegacyBridgeError):
        await subject_alerts_legacy.resolve_all(db_session, ownership=ownership_a)

    assert orphan.resolved_at is None


@pytest.mark.integration
async def test_postgres_rotation_cannot_adopt_legacy_alert_to_retired_snapshot(
    db_session: AsyncSession,
    monkeypatch,
):
    from vitals.services.identity.governance import acquire_identity_governance_lock

    owner, subject = await _subject(db_session, "owner")
    roots = await _roots(db_session, subject)
    legacy = await _alert(
        db_session,
        key="garmin.auth",
        domain=Domain.GARMIN,
        entity="legacy",
    )
    ownership = _ownership(owner, subject, roots)
    old_garmin_id = roots[IntegrationProvider.GARMIN].id
    legacy_id = legacy.id
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    rotation_staged = asyncio.Event()
    allow_rotation_commit = asyncio.Event()
    stale_root_preloaded = asyncio.Event()
    aggregate_waiting = asyncio.Event()
    real_aggregate_lock = subject_alerts.acquire_identity_governance_lock

    async def _signaled_aggregate_lock(session):
        aggregate_waiting.set()
        await real_aggregate_lock(session)

    monkeypatch.setattr(
        subject_alerts,
        "acquire_identity_governance_lock",
        _signaled_aggregate_lock,
    )

    async def _rotate_connection():
        async with factory() as session:
            await acquire_identity_governance_lock(session)
            old = await session.get(IntegrationConnection, old_garmin_id)
            assert old is not None
            old.status = IntegrationConnectionStatus.RETIRED.value
            old.retired_at = now_local()
            session.add(
                IntegrationConnection(
                    subject_id=subject.id,
                    provider=IntegrationProvider.GARMIN.value,
                    connection_type=IntegrationConnectionType.ACCOUNT.value,
                    external_account_discriminator=uuid.uuid4().hex,
                    credential_ref="legacy_env:garmin",
                    status=IntegrationConnectionStatus.ACTIVE.value,
                )
            )
            await session.flush()
            rotation_staged.set()
            await allow_rotation_commit.wait()
            await session.commit()

    async def _resolve_from_stale_snapshot():
        async with factory() as session:
            stale = await session.get(IntegrationConnection, old_garmin_id)
            assert stale is not None
            assert stale.status == IntegrationConnectionStatus.ACTIVE.value
            stale_root_preloaded.set()
            await rotation_staged.wait()
            with pytest.raises(
                subject_alerts.LegacySubjectAlertsConnectionError
            ):
                await subject_alerts.resolve(
                    session,
                    legacy_id,
                    ownership=ownership,
                )

    resolve = asyncio.create_task(_resolve_from_stale_snapshot())
    await asyncio.wait_for(stale_root_preloaded.wait(), timeout=5)
    rotation = asyncio.create_task(_rotate_connection())
    await asyncio.wait_for(rotation_staged.wait(), timeout=5)
    await asyncio.wait_for(aggregate_waiting.wait(), timeout=5)
    assert not resolve.done()
    allow_rotation_commit.set()
    await asyncio.wait_for(rotation, timeout=5)
    await asyncio.wait_for(resolve, timeout=5)

    async with factory() as session:
        row = await session.get(SystemAlert, legacy_id)
        assert row is not None
        assert (row.subject_id, row.integration_connection_id) == (None, None)
        assert row.resolved_at is None
