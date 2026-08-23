"""Contracts for the split, strict proactive preference control plane."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.app_settings import AppSetting
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import (
    IntegrationConnectionSetting,
    SubjectSetting,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity_service import (
    authorize_pre_identity_compatibility_transaction,
)
from vitals.services.proactive import prefs


async def _legacy_scope(db_session: AsyncSession) -> prefs.ProactivePreferencesScope:
    return await prefs.resolve_legacy_preferences_scope(
        db_session,
        actor_username="tester",
    )


async def _new_scope(
    db_session: AsyncSession,
    slug: str,
) -> prefs.ProactivePreferencesScope:
    user = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=slug,
        timezone="Asia/Almaty",
    )
    db_session.add(subject)
    await db_session.flush()
    telegram = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator=f"{slug}-telegram",
        credential_ref=f"vault:{slug}:telegram",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    garmin = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=f"{slug}-garmin",
        credential_ref=f"vault:{slug}:garmin",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add_all([telegram, garmin])
    await db_session.flush()
    return prefs.ProactivePreferencesScope(
        subject_id=subject.id,
        recipient_user_id=user.id,
        telegram_connection_id=telegram.id,
        garmin_connection_id=garmin.id,
        include_legacy=False,
    )


async def _clear_preferences(
    db_session: AsyncSession,
    scope: prefs.ProactivePreferencesScope,
) -> None:
    await db_session.execute(
        delete(SubjectSetting).where(
            SubjectSetting.subject_id == scope.subject_id,
            SubjectSetting.key == prefs.SUBJECT_POLICY_KEY,
        )
    )
    await db_session.execute(
        delete(IntegrationConnectionSetting).where(
            IntegrationConnectionSetting.integration_connection_id.in_(
                (scope.telegram_connection_id, scope.garmin_connection_id)
            )
        )
    )
    await db_session.execute(
        delete(AppSetting).where(
            AppSetting.key == prefs.LEGACY_SETTINGS_KEY
        )
    )
    await db_session.flush()


def _custom(**overrides):
    value = prefs.sanitize(
        {
            "brief_time": "08:15",
            "evening_time": "22:30",
            "quiet_start": "00:30",
            "quiet_end": "07:45",
            "daily_budget": 7,
            "garmin_sync_hours": 3,
            "garmin_weight_export_minutes": 25,
            "garmin_weight_max_age_days": 9,
            "pulse_seconds": 300,
            "pulse_start_hour": 7,
            "pulse_end_hour": 21,
            "nudges": {"activity": True, "nutrition": False, "data": True},
        }
    )
    value.update(overrides)
    return value


async def test_startup_splits_legacy_value_and_strict_reads_are_typed(
    db_session,
    legacy_owner_roots,
):
    scope = await _legacy_scope(db_session)
    await _clear_preferences(db_session, scope)
    legacy = _custom()
    db_session.add(AppSetting(key=prefs.LEGACY_SETTINGS_KEY, value=legacy))
    await db_session.flush()

    first = await prefs.initialize_legacy_preferences(db_session, scope=scope)
    second = await prefs.initialize_legacy_preferences(db_session, scope=scope)

    assert first == second
    assert first.as_flat_dict() == legacy
    assert first.subject.brief_time.strftime("%H:%M") == "08:15"
    assert first.subject.enabled_nudge_categories == frozenset(
        {"activity", "data"}
    )
    assert first.delivery.daily_budget == 7
    assert first.garmin.weight_export_minutes == 25

    subject_row = await db_session.get(
        SubjectSetting,
        (scope.subject_id, prefs.SUBJECT_POLICY_KEY),
    )
    delivery_row = await db_session.get(
        IntegrationConnectionSetting,
        (scope.telegram_connection_id, prefs.TELEGRAM_DELIVERY_POLICY_KEY),
    )
    garmin_row = await db_session.get(
        IntegrationConnectionSetting,
        (scope.garmin_connection_id, prefs.GARMIN_POLICY_KEY),
    )
    assert set(subject_row.value) == {"brief_time", "evening_time", "nudges"}
    assert set(delivery_row.value) == {"quiet_start", "quiet_end", "daily_budget"}
    assert set(garmin_row.value) == {
        "garmin_sync_hours",
        "garmin_weight_export_minutes",
        "garmin_weight_max_age_days",
        "pulse_seconds",
        "pulse_start_hour",
        "pulse_end_hour",
    }

    strict = await prefs.get_preferences_bundle(
        db_session,
        scope=scope,
        actor_username="tester",
    )
    exact_one = await prefs.get_exact_one_preferences_bundle(
        db_session,
        scope=scope,
    )
    locked = await prefs.get_locked_delivery_policy(
        db_session,
        subject_id=scope.subject_id,
        recipient_user_id=scope.recipient_user_id,
        integration_connection_id=scope.telegram_connection_id,
    )
    assert strict == first
    assert exact_one == first
    assert locked == first.delivery


async def test_startup_seeds_explicit_defaults_before_strict_runtime_reads(
    db_session,
    legacy_owner_roots,
):
    scope = await _legacy_scope(db_session)
    await _clear_preferences(db_session, scope)
    bundle = await prefs.initialize_legacy_preferences(db_session, scope=scope)

    assert bundle.as_flat_dict() == prefs.sanitize(None)
    legacy = await db_session.get(AppSetting, prefs.LEGACY_SETTINGS_KEY)
    assert legacy is not None and legacy.value == prefs.sanitize(None)
    assert (
        await prefs.get_preferences_bundle(
            db_session,
            scope=scope,
            actor_username="tester",
        )
        == bundle
    )


async def test_partial_malformed_and_drifted_state_fail_closed(
    db_session,
    legacy_owner_roots,
):
    """Missing is not malformed, and only one of the two is a failure.

    A row with the wrong field set is tampered-with or from a schema this build
    does not understand, and every path below still stops on it. A row that does
    not exist is a person who has never opened the notification settings, and
    the honest answer to that is the defaults — the same ones the form shows.

    The two used to be one case, which made the settings page crash for anybody
    but the legacy owner: only the owner's rows are seeded at startup, so every
    other subject read as corrupt. The write side still requires all three
    partitions, because there a missing one means a half-written split.
    """

    scope = await _legacy_scope(db_session)
    await _clear_preferences(db_session, scope)

    unconfigured = await prefs.get_preferences_bundle(
        db_session,
        scope=scope,
        actor_username="tester",
    )
    assert unconfigured.as_flat_dict() == prefs.sanitize(None)

    with pytest.raises(prefs.ProactivePreferencesUnavailableError):
        await prefs.get_locked_delivery_policy(
            db_session,
            subject_id=scope.subject_id,
            recipient_user_id=scope.recipient_user_id,
            integration_connection_id=scope.telegram_connection_id,
        )

    await prefs.initialize_legacy_preferences(db_session, scope=scope)
    delivery = await db_session.get(
        IntegrationConnectionSetting,
        (scope.telegram_connection_id, prefs.TELEGRAM_DELIVERY_POLICY_KEY),
    )
    delivery.value = {"daily_budget": 4}
    await db_session.flush()
    with pytest.raises(prefs.ProactivePreferencesUnavailableError):
        await prefs.get_locked_delivery_policy(
            db_session,
            subject_id=scope.subject_id,
            recipient_user_id=scope.recipient_user_id,
            integration_connection_id=scope.telegram_connection_id,
        )
    await db_session.rollback()

    scope = await _legacy_scope(db_session)
    await prefs.initialize_legacy_preferences(db_session, scope=scope)
    legacy = await db_session.get(AppSetting, prefs.LEGACY_SETTINGS_KEY)
    legacy.value = _custom(daily_budget=11)
    await db_session.flush()
    with pytest.raises(prefs.ProactivePreferencesDriftError):
        await prefs.initialize_legacy_preferences(db_session, scope=scope)


async def test_two_subjects_keep_all_three_policy_partitions_isolated(
    db_session,
    legacy_owner_roots,
):
    first_legacy_scope = await _legacy_scope(db_session)
    await prefs.initialize_legacy_preferences(db_session, scope=first_legacy_scope)
    legacy_before = (
        await db_session.get(AppSetting, prefs.LEGACY_SETTINGS_KEY)
    ).value.copy()

    second_scope = await _new_scope(db_session, "second-policy-owner")
    first_scope = prefs.ProactivePreferencesScope(
        subject_id=first_legacy_scope.subject_id,
        recipient_user_id=first_legacy_scope.recipient_user_id,
        telegram_connection_id=first_legacy_scope.telegram_connection_id,
        garmin_connection_id=first_legacy_scope.garmin_connection_id,
        include_legacy=False,
    )
    first_value = _custom(brief_time="06:10", daily_budget=3)
    second_value = _custom(brief_time="12:20", daily_budget=10)
    await prefs.set_preferences_bundle(
        db_session,
        first_value,
        scope=first_scope,
        actor_username="tester",
    )
    await prefs.set_preferences_bundle(
        db_session,
        second_value,
        scope=second_scope,
        actor_username="second-policy-owner",
    )

    assert (
        await prefs.get_preferences_bundle(
            db_session,
            scope=first_scope,
            actor_username="tester",
        )
    ).as_flat_dict() == first_value
    assert (
        await prefs.get_preferences_bundle(
            db_session,
            scope=second_scope,
            actor_username="second-policy-owner",
        )
    ).as_flat_dict() == second_value
    assert (
        await db_session.get(AppSetting, prefs.LEGACY_SETTINGS_KEY)
    ).value == legacy_before

    foreign_scope = prefs.ProactivePreferencesScope(
        subject_id=first_scope.subject_id,
        recipient_user_id=first_scope.recipient_user_id,
        telegram_connection_id=second_scope.telegram_connection_id,
        garmin_connection_id=first_scope.garmin_connection_id,
    )
    with pytest.raises(prefs.ProactivePreferencesScopeError):
        await prefs.get_preferences_bundle(
            db_session,
            scope=foreign_scope,
            actor_username="tester",
        )
    with pytest.raises(prefs.ProactivePreferencesUnavailableError):
        await prefs.get_locked_delivery_policy(
            db_session,
            subject_id=first_scope.subject_id,
            recipient_user_id=first_scope.recipient_user_id,
            integration_connection_id=second_scope.telegram_connection_id,
        )

    with pytest.raises(prefs.LegacyProactivePreferencesBridgeClosedError):
        await prefs.set_preferences_bundle(
            db_session,
            first_value,
            scope=first_legacy_scope,
            actor_username="tester",
        )

    with pytest.raises(prefs.ProactivePreferencesScopeError):
        await prefs.get_preferences_bundle(
            db_session,
            scope=second_scope,
            actor_username="tester",
        )
    with pytest.raises(prefs.ProactivePreferencesScopeError):
        await prefs.set_preferences_bundle(
            db_session,
            _custom(daily_budget=12),
            scope=second_scope,
            actor_username="tester",
        )
    with pytest.raises(prefs.LegacyProactivePreferencesBridgeClosedError):
        await prefs.get_exact_one_preferences_bundle(
            db_session,
            scope=first_legacy_scope,
        )


async def test_unscoped_compatibility_is_zero_subject_only(
    db_session,
    legacy_owner_roots,
):
    with pytest.raises(prefs.ProactivePreferencesScopeError):
        await prefs.get_pre_identity_legacy_prefs(db_session)
    with pytest.raises(prefs.ProactivePreferencesScopeError):
        await prefs.set_pre_identity_legacy_prefs(db_session, _custom())


@pytest.mark.parametrize("operation", ["get", "set"])
async def test_pre_identity_compatibility_rejects_pending_identity_without_flush(
    db_session,
    operation,
):
    await authorize_pre_identity_compatibility_transaction(db_session)
    user = User(
        id=uuid.uuid4(),
        username="pending-preference-owner",
        normalized_username="pending-preference-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    subject = HealthSubject(
        id=uuid.uuid4(),
        owner_user_id=user.id,
        display_name="Pending preference owner",
        timezone="Asia/Almaty",
    )
    db_session.add_all([user, subject])

    with pytest.raises(prefs.ProactivePreferencesScopeError):
        if operation == "get":
            await prefs.get_pre_identity_legacy_prefs(db_session)
        else:
            await prefs.set_pre_identity_legacy_prefs(
                db_session,
                _custom(daily_budget=8),
            )
    assert user in db_session.new
    assert subject in db_session.new
    with db_session.no_autoflush:
        assert (
            await db_session.scalar(
                select(func.count()).select_from(HealthSubject)
            )
            == 0
        )
        assert (
            await db_session.scalar(
                select(func.count()).select_from(AppSetting)
            )
            == 0
        )


@pytest.mark.parametrize("operation", ["get", "set"])
async def test_pre_identity_compatibility_does_not_flush_unrelated_pending_state(
    db_session,
    operation,
):
    user = User(
        id=uuid.uuid4(),
        username="pending-unrelated-user",
        normalized_username="pending-unrelated-user",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)

    if operation == "get":
        assert await prefs.get_pre_identity_legacy_prefs(
            db_session
        ) == prefs.sanitize(None)
        expected_setting_count = 0
    else:
        assert (
            await prefs.set_pre_identity_legacy_prefs(
                db_session,
                _custom(daily_budget=8),
            )
        )["daily_budget"] == 8
        expected_setting_count = 1

    assert user in db_session.new
    with db_session.no_autoflush:
        assert await db_session.scalar(select(func.count()).select_from(User)) == 0
        assert (
            await db_session.scalar(
                select(func.count()).select_from(AppSetting)
            )
            == expected_setting_count
        )


async def test_pre_identity_compatibility_rejects_unrecognized_transaction(
    db_session,
):
    await db_session.scalar(select(func.count()).select_from(AppSetting))

    with pytest.raises(prefs.ProactivePreferencesScopeError):
        await prefs.get_pre_identity_legacy_prefs(db_session)
    with pytest.raises(prefs.ProactivePreferencesScopeError):
        await prefs.set_pre_identity_legacy_prefs(db_session, _custom())


async def test_pre_identity_compatibility_rejects_nested_transaction(
    db_session,
):
    await authorize_pre_identity_compatibility_transaction(db_session)

    async with db_session.begin_nested():
        with pytest.raises(prefs.ProactivePreferencesScopeError):
            await prefs.get_pre_identity_legacy_prefs(db_session)


@pytest.mark.integration
async def test_postgres_absent_scoped_rows_serialize_on_subject_root(
    db_session,
    legacy_owner_roots,
):
    legacy_scope = await _legacy_scope(db_session)
    scope = prefs.ProactivePreferencesScope(
        subject_id=legacy_scope.subject_id,
        recipient_user_id=legacy_scope.recipient_user_id,
        telegram_connection_id=legacy_scope.telegram_connection_id,
        garmin_connection_id=legacy_scope.garmin_connection_id,
        include_legacy=False,
    )
    await _clear_preferences(db_session, legacy_scope)
    await db_session.commit()
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    session_a = factory()
    await prefs.set_preferences_bundle(
        session_a,
        _custom(daily_budget=2),
        scope=scope,
        actor_username="tester",
    )

    async def write_b() -> None:
        async with factory() as session_b:
            await prefs.set_preferences_bundle(
                session_b,
                _custom(daily_budget=9),
                scope=scope,
                actor_username="tester",
            )
            await session_b.commit()

    task_b = asyncio.create_task(write_b())
    await asyncio.sleep(0.25)
    assert not task_b.done()
    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        assert (
            await prefs.get_preferences_bundle(
                verify,
                scope=scope,
                actor_username="tester",
            )
        ).delivery.daily_budget == 9


@pytest.mark.integration
async def test_postgres_pre_identity_read_serializes_identity_bootstrap(
    db_session,
):
    from vitals.services.identity_bootstrap import bootstrap_legacy_owner

    assert db_session.bind is not None
    await db_session.commit()
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    holder = factory()
    assert await prefs.get_pre_identity_legacy_prefs(holder) == prefs.sanitize(
        None
    )

    attempted = asyncio.Event()

    async def bootstrap() -> None:
        async with factory() as contender:
            attempted.set()
            await bootstrap_legacy_owner(
                contender,
                username="race-preference-owner",
                password_hash=(
                    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/"
                    "siMDlha"
                ),
                timezone="Asia/Almaty",
            )
            await contender.commit()

    task = asyncio.create_task(bootstrap())
    try:
        await attempted.wait()
        await asyncio.sleep(0.2)
        assert not task.done(), "identity bootstrap must wait for governance"
        await holder.commit()
        await holder.close()
        await asyncio.wait_for(task, timeout=5)
    finally:
        if holder.in_transaction():
            await holder.rollback()
        await holder.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async with factory() as verify:
        assert (
            await verify.scalar(
                select(func.count()).select_from(HealthSubject)
            )
            == 1
        )
