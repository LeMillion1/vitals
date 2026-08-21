"""Focused Stage-2 ownership contract for ``system_alerts``."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    RuleType,
    Severity,
    UserStatus,
)
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import alerts_service as alerts
from vitals.utils.timeutils import now_local

_PASSWORD_HASH = "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
HEALTH_KEY = "weight.noisy_period_active"
PROVIDER_KEY = "garmin.auth"
PLATFORM_KEY = "scheduler.job_failed:raw_payload_sweep"


async def _user(
    session: AsyncSession,
    slug: str,
    *,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    row = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash=_PASSWORD_HASH,
        status=status.value,
    )
    session.add(row)
    await session.flush()
    return row


async def _subject(
    session: AsyncSession,
    slug: str,
    *,
    owner_status: UserStatus = UserStatus.ACTIVE,
) -> tuple[User, HealthSubject]:
    owner = await _user(session, slug, status=owner_status)
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
    *,
    provider: IntegrationProvider = IntegrationProvider.GARMIN,
    connection_type: IntegrationConnectionType = IntegrationConnectionType.ACCOUNT,
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.ACTIVE,
) -> IntegrationConnection:
    row = IntegrationConnection(
        subject_id=subject.id,
        provider=provider.value,
        connection_type=connection_type.value,
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


def _health(
    owner: User,
    subject: HealthSubject,
    *,
    system: bool = False,
) -> alerts.HealthAlertContext:
    return alerts.HealthAlertContext(
        WriteIdentity(
            subject_id=subject.id,
            actor_user_id=None if system else owner.id,
        )
    )


def _provider(
    owner: User,
    subject: HealthSubject,
    connection: IntegrationConnection,
    *,
    provider: IntegrationProvider = IntegrationProvider.GARMIN,
    system: bool = False,
) -> alerts.ProviderAlertContext:
    return alerts.ProviderAlertContext(
        identity=WriteIdentity(
            subject_id=subject.id,
            actor_user_id=None if system else owner.id,
        ),
        provider=provider,
        integration_connection_id=connection.id,
    )


async def _direct_alert(
    session: AsyncSession,
    *,
    key: str = HEALTH_KEY,
    domain: Domain = Domain.WEIGHT,
    entity: str = "",
    subject_id: uuid.UUID | None = None,
    connection_id: uuid.UUID | None = None,
    resolved: bool = False,
) -> SystemAlert:
    row = SystemAlert(
        domain=domain.value,
        severity=Severity.WARN.value,
        message="test alert",
        alert_key=key,
        entity_ref=entity,
        subject_id=subject_id,
        integration_connection_id=connection_id,
        resolved_at=now_local() if resolved else None,
    )
    session.add(row)
    await session.flush()
    return row


async def test_legacy_api_remains_unscoped_and_string_compatible(db_session):
    row = await alerts.raise_alert(
        db_session,
        domain=Domain.WEIGHT.value,
        severity=Severity.INFO.value,
        message="legacy",
        alert_key="legacy.test.key",
    )

    assert row.subject_id is None
    assert row.integration_connection_id is None
    assert row.overridden_by_user_id is None
    assert row.resolved_by_user_id is None


def test_contexts_and_registries_are_strict_and_immutable():
    with pytest.raises(alerts.AlertContextError):
        alerts.HealthAlertContext(object())  # type: ignore[arg-type]
    with pytest.raises(alerts.AlertContextError):
        alerts.PlatformAlertContext("scheduler")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        alerts.PROVIDER_ALERT_KEYS[IntegrationProvider.GARMIN] = frozenset()  # type: ignore[index]
    assert PLATFORM_KEY in alerts.PLATFORM_ALERT_KEYS[
        alerts.PlatformAlertNamespace.SCHEDULER_JOB_FAILURE
    ]
    classified_exact = set(alerts.HEALTH_ALERT_KEYS).union(
        *alerts.PROVIDER_ALERT_KEYS.values(),
        *alerts.PLATFORM_ALERT_KEYS.values(),
    )
    assert set(alerts.ALERT_KEY_DOMAINS) == classified_exact
    assert set(alerts.PROVIDER_ALERT_CONNECTION_TYPES) == set(IntegrationProvider)


async def test_health_raise_refreshes_exact_scope_and_preserves_domain(db_session):
    owner, subject = await _subject(db_session, "owner")
    context = _health(owner, subject)
    first = await alerts.raise_scoped_alert(
        db_session,
        context=context,
        domain=Domain.WEIGHT,
        severity=Severity.INFO,
        message="old",
        alert_key=HEALTH_KEY,
    )
    second = await alerts.raise_scoped_alert(
        db_session,
        context=context,
        domain=Domain.WEIGHT,
        severity=Severity.WARN,
        message="new",
        alert_key=HEALTH_KEY,
    )

    assert second.id == first.id
    assert second.subject_id == subject.id
    assert second.integration_connection_id is None
    assert second.severity == Severity.WARN.value
    assert second.message == "new"
    assert second.overridden_by_user_id is None
    assert second.resolved_by_user_id is None

    with pytest.raises(alerts.AlertScopeConflictError):
        await alerts.raise_scoped_alert(
            db_session,
            context=context,
            domain=Domain.LABS,
            severity=Severity.WARN,
            message="wrong domain",
            alert_key=HEALTH_KEY,
        )
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 1


async def test_lifecycle_actor_semantics_are_idempotent(db_session):
    owner, subject = await _subject(db_session, "owner")
    system_context = _health(owner, subject, system=True)
    human_context = _health(owner, subject)

    automatic = await alerts.raise_scoped_alert(
        db_session,
        context=system_context,
        domain=Domain.WEIGHT,
        severity=Severity.INFO,
        message="automatic",
        alert_key=HEALTH_KEY,
    )
    await alerts.resolve_scoped_by_key(
        db_session,
        context=system_context,
        alert_key=HEALTH_KEY,
    )
    assert automatic.resolved_by_user_id is None

    human = await alerts.raise_scoped_alert(
        db_session,
        context=human_context,
        domain=Domain.WEIGHT,
        severity=Severity.BLOCK,
        message="human",
        alert_key=HEALTH_KEY,
    )
    await alerts.override_scoped_alert(db_session, human.id, context=human_context)
    await alerts.resolve_scoped_alert(db_session, human.id, context=human_context)
    override_at = human.override_at
    resolved_at = human.resolved_at
    assert human.overridden_by_user_id == owner.id
    assert human.resolved_by_user_id == owner.id

    other = await _user(db_session, "professional")
    other_context = alerts.HealthAlertContext(
        WriteIdentity(subject_id=subject.id, actor_user_id=other.id)
    )
    await alerts.override_scoped_alert(db_session, human.id, context=other_context)
    await alerts.resolve_scoped_alert(db_session, human.id, context=other_context)
    assert human.override_at == override_at
    assert human.resolved_at == resolved_at
    assert human.overridden_by_user_id == owner.id
    assert human.resolved_by_user_id == owner.id

    with pytest.raises(alerts.AlertActorRequiredError):
        await alerts.override_scoped_alert(
            db_session,
            human.id,
            context=system_context,
        )


async def test_missing_and_inactive_actors_fail_before_mutation(db_session):
    _owner, subject = await _subject(db_session, "owner")
    missing = alerts.HealthAlertContext(
        WriteIdentity(subject_id=subject.id, actor_user_id=uuid.uuid4())
    )
    with pytest.raises(alerts.AlertActorNotFoundError):
        await alerts.raise_scoped_alert(
            db_session,
            context=missing,
            domain=Domain.WEIGHT,
            severity=Severity.INFO,
            message="x",
            alert_key=HEALTH_KEY,
        )

    inactive = await _user(db_session, "inactive", status=UserStatus.SUSPENDED)
    suspended = alerts.HealthAlertContext(
        WriteIdentity(subject_id=subject.id, actor_user_id=inactive.id)
    )
    with pytest.raises(alerts.AlertActorInactiveError):
        await alerts.raise_scoped_alert(
            db_session,
            context=suspended,
            domain=Domain.WEIGHT,
            severity=Severity.INFO,
            message="x",
            alert_key=HEALTH_KEY,
        )
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0


async def test_foreign_scope_is_hidden_and_both_subjects_keep_their_own_alert(
    db_session,
):
    owner_a, subject_a = await _subject(db_session, "owner-a")
    owner_b, subject_b = await _subject(db_session, "owner-b")
    row = await alerts.raise_scoped_alert(
        db_session,
        context=_health(owner_a, subject_a),
        domain=Domain.WEIGHT,
        severity=Severity.INFO,
        message="a",
        alert_key=HEALTH_KEY,
    )

    context_b = _health(owner_b, subject_b)
    assert await alerts.list_active_scoped(db_session, context=context_b) == []
    assert await alerts.resolve_scoped_alert(
        db_session, row.id, context=context_b
    ) is None
    # One health key, two people: the scoped key is per subject, so raising it
    # for B neither reads nor refreshes A's row.
    mine = await alerts.raise_scoped_alert(
        db_session,
        context=context_b,
        domain=Domain.WEIGHT,
        severity=Severity.WARN,
        message="b",
        alert_key=HEALTH_KEY,
    )
    assert mine.subject_id == subject_b.id
    assert mine.message == "b"
    assert row.subject_id == subject_a.id
    assert row.message == "a"
    assert row.resolved_at is None
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 2


async def test_provider_scope_validates_subject_provider_type_and_status(db_session):
    owner, subject = await _subject(db_session, "owner")
    connection = await _connection(db_session, subject)
    context = _provider(owner, subject, connection)
    row = await alerts.raise_scoped_alert(
        db_session,
        context=context,
        domain=Domain.GARMIN,
        severity=Severity.WARN,
        message="auth",
        alert_key=PROVIDER_KEY,
    )
    assert row.subject_id == subject.id
    assert row.integration_connection_id == connection.id

    connection.status = IntegrationConnectionStatus.DISABLED.value
    await db_session.flush()
    with pytest.raises(alerts.AlertConnectionStateError):
        await alerts.raise_scoped_alert(
            db_session,
            context=context,
            domain=Domain.GARMIN,
            severity=Severity.WARN,
            message="disabled",
            alert_key=PROVIDER_KEY,
        )
    assert [item.id for item in await alerts.list_active_scoped(
        db_session, context=context
    )] == [row.id]
    assert await alerts.resolve_scoped_by_key(
        db_session, context=context, alert_key=PROVIDER_KEY
    ) is row

    pending = await _connection(
        db_session,
        subject,
        status=IntegrationConnectionStatus.PENDING,
    )
    pending_context = _provider(owner, subject, pending)
    with pytest.raises(alerts.AlertConnectionStateError):
        await alerts.list_active_scoped(db_session, context=pending_context)

    wrong_type = await _connection(
        db_session,
        subject,
        connection_type=IntegrationConnectionType.IMPORT,
    )
    with pytest.raises(alerts.AlertConnectionTypeError):
        await alerts.raise_scoped_alert(
            db_session,
            context=_provider(owner, subject, wrong_type),
            domain=Domain.GARMIN,
            severity=Severity.WARN,
            message="wrong type",
            alert_key="garmin.weight_export",
        )

    with pytest.raises(alerts.AlertConnectionProviderError):
        await alerts.raise_scoped_alert(
            db_session,
            context=_provider(
                owner,
                subject,
                connection,
                provider=IntegrationProvider.HEVY,
            ),
            domain=Domain.WORKOUTS,
            severity=Severity.WARN,
            message="wrong provider",
            alert_key="hevy.sync_failed",
        )

    other_owner, other_subject = await _subject(db_session, "other-owner")
    with pytest.raises(alerts.AlertConnectionOwnershipError):
        await alerts.raise_scoped_alert(
            db_session,
            context=_provider(other_owner, other_subject, connection),
            domain=Domain.GARMIN,
            severity=Severity.WARN,
            message="wrong subject",
            alert_key=PROVIDER_KEY,
        )


async def test_retired_connection_is_historical_only(db_session):
    owner, subject = await _subject(db_session, "owner")
    connection = await _connection(
        db_session,
        subject,
        status=IntegrationConnectionStatus.RETIRED,
    )
    context = _provider(owner, subject, connection)
    row = await _direct_alert(
        db_session,
        key="garmin.token_cache",
        domain=Domain.GARMIN,
        subject_id=subject.id,
        connection_id=connection.id,
    )

    assert [item.id for item in await alerts.list_active_scoped(
        db_session, context=context
    )] == [row.id]
    await alerts.resolve_scoped_alert(db_session, row.id, context=context)
    assert row.resolved_by_user_id == owner.id
    with pytest.raises(alerts.AlertConnectionStateError):
        await alerts.raise_scoped_alert(
            db_session,
            context=context,
            domain=Domain.GARMIN,
            severity=Severity.WARN,
            message="new",
            alert_key=PROVIDER_KEY,
        )


async def test_platform_namespace_is_exact_and_not_domain_inferred(db_session):
    owner, subject = await _subject(db_session, "owner")
    platform = alerts.PlatformAlertContext(
        alerts.PlatformAlertNamespace.SCHEDULER_JOB_FAILURE
    )
    platform_row = await alerts.raise_scoped_alert(
        db_session,
        context=platform,
        domain=Domain.SYSTEM,
        severity=Severity.WARN,
        message="sweep",
        alert_key=PLATFORM_KEY,
    )
    health_row = await alerts.raise_scoped_alert(
        db_session,
        context=_health(owner, subject, system=True),
        domain=Domain.SYSTEM,
        severity=Severity.INFO,
        message="brief",
        alert_key="brief_empty_day",
    )
    assert platform_row.subject_id is None
    assert health_row.subject_id == subject.id
    assert [row.id for row in await alerts.list_active_scoped(
        db_session, context=platform
    )] == [platform_row.id]

    with pytest.raises(alerts.AlertPlatformNamespaceError):
        await alerts.raise_scoped_alert(
            db_session,
            context=platform,
            domain=Domain.SYSTEM,
            severity=Severity.WARN,
            message="wrong job",
            alert_key="scheduler.job_failed:garmin_sync",
        )
    with pytest.raises(alerts.AlertPlatformNamespaceError):
        await alerts.raise_scoped_alert(
            db_session,
            context=_health(owner, subject),
            domain=Domain.SYSTEM,
            severity=Severity.WARN,
            message="wrong scope",
            alert_key=PLATFORM_KEY,
        )


async def test_full_null_health_and_provider_rows_adopt_only_with_bridge(db_session):
    owner, subject = await _subject(db_session, "owner")
    connection = await _connection(db_session, subject)
    health_legacy = await _direct_alert(db_session)
    provider_legacy = await _direct_alert(
        db_session,
        key="garmin.token_cache",
        domain=Domain.GARMIN,
    )

    with pytest.raises(alerts.AlertScopeConflictError):
        await alerts.raise_scoped_alert(
            db_session,
            context=_health(owner, subject),
            domain=Domain.WEIGHT,
            severity=Severity.INFO,
            message="reject",
            alert_key=HEALTH_KEY,
        )

    adopted_health = await alerts.raise_scoped_alert(
        db_session,
        context=_health(owner, subject),
        domain=Domain.WEIGHT,
        severity=Severity.INFO,
        message="adopted",
        alert_key=HEALTH_KEY,
        legacy_bridge=alerts.LegacyAlertBridge.FULLY_UNOWNED,
    )
    adopted_provider = await alerts.raise_scoped_alert(
        db_session,
        context=_provider(owner, subject, connection),
        domain=Domain.GARMIN,
        severity=Severity.WARN,
        message="adopted provider",
        alert_key="garmin.token_cache",
        legacy_bridge=alerts.LegacyAlertBridge.FULLY_UNOWNED,
    )
    assert adopted_health.id == health_legacy.id
    assert adopted_health.subject_id == subject.id
    assert adopted_health.integration_connection_id is None
    assert adopted_provider.id == provider_legacy.id
    assert adopted_provider.subject_id == subject.id
    assert adopted_provider.integration_connection_id == connection.id


async def test_bridge_rejects_second_subject_partial_and_unknown_rows(db_session):
    owner, subject = await _subject(db_session, "owner")
    await _direct_alert(db_session)
    await _subject(db_session, "other")
    with pytest.raises(alerts.AlertLegacyBridgeError):
        await alerts.list_active_scoped(
            db_session,
            context=_health(owner, subject),
            legacy_bridge=alerts.LegacyAlertBridge.FULLY_UNOWNED,
        )

    await db_session.rollback()
    owner, subject = await _subject(db_session, "sole")
    connection = await _connection(db_session, subject)
    partial = await _direct_alert(db_session, connection_id=connection.id)
    with pytest.raises(alerts.AlertScopeConflictError):
        await alerts.raise_scoped_alert(
            db_session,
            context=_health(owner, subject),
            domain=Domain.WEIGHT,
            severity=Severity.INFO,
            message="partial",
            alert_key=HEALTH_KEY,
            legacy_bridge=alerts.LegacyAlertBridge.FULLY_UNOWNED,
        )
    assert partial.subject_id is None

    partial.resolved_at = now_local()
    await _direct_alert(db_session, key="unknown.alert", domain=Domain.SYSTEM)
    with pytest.raises(alerts.AlertLegacyBridgeError):
        await alerts.list_active_scoped(
            db_session,
            context=_health(owner, subject),
            legacy_bridge=alerts.LegacyAlertBridge.FULLY_UNOWNED,
        )


async def test_bridge_requires_active_sole_subject_owner(db_session):
    owner, subject = await _subject(db_session, "owner")
    await _direct_alert(db_session)
    owner.status = UserStatus.SUSPENDED.value
    await db_session.flush()
    with pytest.raises(alerts.AlertLegacyBridgeError):
        await alerts.list_active_scoped(
            db_session,
            context=_health(owner, subject, system=True),
            legacy_bridge=alerts.LegacyAlertBridge.FULLY_UNOWNED,
        )


async def test_registered_domain_and_conflict_rule_integrity(db_session):
    owner, subject = await _subject(db_session, "owner")
    context = _health(owner, subject)
    with pytest.raises(alerts.AlertScopeConflictError):
        await alerts.raise_scoped_alert(
            db_session,
            context=context,
            domain=Domain.LABS,
            severity=Severity.WARN,
            message="wrong",
            alert_key=HEALTH_KEY,
        )

    legacy_wrong_domain = await _direct_alert(
        db_session,
        key="labs.retest_due",
        domain=Domain.WEIGHT,
    )
    with pytest.raises(alerts.AlertLegacyBridgeError):
        await alerts.raise_scoped_alert(
            db_session,
            context=context,
            domain=Domain.LABS,
            severity=Severity.INFO,
            message="wrong legacy domain",
            alert_key="labs.retest_due",
            legacy_bridge=alerts.LegacyAlertBridge.FULLY_UNOWNED,
        )
    assert legacy_wrong_domain.subject_id is None
    legacy_wrong_domain.resolved_at = now_local()
    legacy_wrong_domain.subject_id = subject.id

    rule = ConflictRule(
        rule_type=RuleType.SOFT_WARN.value,
        domain_a=Domain.WEIGHT.value,
        condition_a={},
        domain_b=Domain.SUPPLEMENTS.value,
        condition_b={},
        severity=Severity.WARN.value,
        message="conflict",
        active=True,
    )
    db_session.add(rule)
    await db_session.flush()
    conflict_key = f"conflict:{rule.id}"
    row = await alerts.raise_scoped_alert(
        db_session,
        context=context,
        domain=Domain.WEIGHT,
        severity=Severity.WARN,
        message="conflict",
        alert_key=conflict_key,
    )
    assert row.subject_id == subject.id
    with pytest.raises(alerts.AlertScopeConflictError):
        await alerts.raise_scoped_alert(
            db_session,
            context=context,
            domain=Domain.LABS,
            severity=Severity.WARN,
            message="wrong side",
            alert_key=conflict_key,
        )
    with pytest.raises(alerts.AlertScopeConflictError):
        await alerts.raise_scoped_alert(
            db_session,
            context=context,
            domain=Domain.WEIGHT,
            severity=Severity.WARN,
            message="missing rule",
            alert_key="conflict:999999",
        )


async def test_scoped_history_supersede_and_bulk_resolution_are_isolated(db_session):
    owner_a, subject_a = await _subject(db_session, "owner-a")
    owner_b, subject_b = await _subject(db_session, "owner-b")
    context_a = _health(owner_a, subject_a)
    old = await _direct_alert(
        db_session, entity="old", subject_id=subject_a.id
    )
    current = await _direct_alert(
        db_session, entity="current", subject_id=subject_a.id
    )
    foreign = await _direct_alert(
        db_session, entity="foreign", subject_id=subject_b.id
    )
    labs = await _direct_alert(
        db_session,
        key="labs.out_of_range",
        domain=Domain.LABS,
        entity="lab:1",
        subject_id=subject_a.id,
    )

    changed = await alerts.resolve_scoped_superseded(
        db_session,
        context=context_a,
        alert_key=HEALTH_KEY,
        keep_entity="current",
    )
    assert changed == 1
    assert old.resolved_by_user_id == owner_a.id
    assert current.resolved_at is None
    assert foreign.resolved_at is None

    assert await alerts.was_scoped_ever_dismissed(
        db_session,
        context=context_a,
        alert_key=HEALTH_KEY,
        entity_ref="old",
    ) is True
    assert await alerts.was_scoped_dismissed_today(
        db_session,
        context=context_a,
        alert_key=HEALTH_KEY,
        entity_ref="old",
    ) is True
    assert await alerts.resolve_all_scoped(
        db_session, context=context_a, domain=Domain.LABS
    ) == 1
    assert labs.resolved_by_user_id == owner_a.id
    assert foreign.resolved_at is None


async def test_scoped_service_flushes_without_commit_and_rollback_restores_adoption(
    db_session,
):
    owner, subject = await _subject(db_session, "owner")
    legacy = await _direct_alert(db_session)
    await db_session.commit()

    adopted = await alerts.raise_scoped_alert(
        db_session,
        context=_health(owner, subject),
        domain=Domain.WEIGHT,
        severity=Severity.INFO,
        message="adopt",
        alert_key=HEALTH_KEY,
        legacy_bridge=alerts.LegacyAlertBridge.FULLY_UNOWNED,
    )
    assert adopted.id == legacy.id
    assert adopted.subject_id == subject.id
    legacy_id = legacy.id
    await db_session.rollback()

    restored = await db_session.get(SystemAlert, legacy_id)
    assert restored is not None
    assert restored.subject_id is None
    assert restored.message == "test alert"


@pytest.mark.integration
async def test_postgres_concurrent_same_scope_raise_returns_one_row(db_session):
    owner, subject = await _subject(db_session, "owner")
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    context = _health(owner, subject, system=True)

    async def run_once() -> int:
        async with factory() as session:
            row = await alerts.raise_scoped_alert(
                session,
                context=context,
                domain=Domain.WEIGHT,
                severity=Severity.INFO,
                message="concurrent",
                alert_key=HEALTH_KEY,
            )
            await session.commit()
            return row.id

    first_id, second_id = await asyncio.gather(run_once(), run_once())
    assert first_id == second_id
    async with factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(SystemAlert)
        ) == 1


@pytest.mark.integration
async def test_postgres_competing_scopes_each_get_their_own_alert(db_session):
    owner_a, subject_a = await _subject(db_session, "owner-a")
    owner_b, subject_b = await _subject(db_session, "owner-b")
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def run_once(context: alerts.HealthAlertContext) -> str:
        async with factory() as session:
            try:
                await alerts.raise_scoped_alert(
                    session,
                    context=context,
                    domain=Domain.WEIGHT,
                    severity=Severity.INFO,
                    message="concurrent",
                    alert_key=HEALTH_KEY,
                )
                await session.commit()
                return "created"
            except alerts.AlertScopeConflictError:
                await session.rollback()
                return "conflict"

    outcomes = await asyncio.gather(
        run_once(_health(owner_a, subject_a, system=True)),
        run_once(_health(owner_b, subject_b, system=True)),
    )
    # The scoped key is per subject, so two concurrent transactions raising the
    # same health key both succeed instead of racing for one global row.
    assert outcomes == ["created", "created"]
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 2
