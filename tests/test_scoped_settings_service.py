"""Contracts for the allowlisted legacy/scoped settings bridge."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import StatementError
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
    UserSetting,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.scoped_settings_service import (
    SCOPED_SETTING_REGISTRY,
    ForbiddenScopedSettingKeyError,
    LegacyScopedSettingBridgeClosedError,
    ScopedSettingDriftError,
    ScopedSettingKey,
    ScopedSettingOwnershipError,
    ScopedSettingScopeMismatchError,
    ScopedSettingTargetNotFoundError,
    ScopedSettingValidationError,
    SettingScope,
    UnknownScopedSettingKeyError,
    get_scoped_setting,
    mirror_legacy_setting,
    set_scoped_setting,
)


@dataclass(frozen=True)
class _Graph:
    user: User
    subject: HealthSubject
    garmin: IntegrationConnection
    hevy: IntegrationConnection


async def _graph(session: AsyncSession, slug: str = "scoped-owner") -> _Graph:
    user = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    garmin = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=f"{slug}-garmin",
        credential_ref="legacy_env:garmin",
        status=IntegrationConnectionStatus.LEGACY.value,
    )
    hevy = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.HEVY.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=f"{slug}-hevy",
        credential_ref="legacy_env:hevy",
        status=IntegrationConnectionStatus.LEGACY.value,
    )
    session.add_all([garmin, hevy])
    await session.flush()
    return _Graph(user=user, subject=subject, garmin=garmin, hevy=hevy)


def _request(
    graph: _Graph,
    key: ScopedSettingKey,
) -> dict[str, Any]:
    route = SCOPED_SETTING_REGISTRY[key]
    if route.scope is SettingScope.USER:
        return {"scope": route.scope, "key": key, "user_id": graph.user.id}
    if route.scope is SettingScope.SUBJECT:
        return {
            "scope": route.scope,
            "key": key,
            "subject_id": graph.subject.id,
        }
    return {
        "scope": route.scope,
        "key": key,
        "subject_id": graph.subject.id,
        "integration_connection_id": graph.garmin.id,
    }


def _scoped_pk(graph: _Graph, key: ScopedSettingKey) -> tuple[uuid.UUID, str]:
    route = SCOPED_SETTING_REGISTRY[key]
    ids = {
        SettingScope.USER: graph.user.id,
        SettingScope.SUBJECT: graph.subject.id,
        SettingScope.INTEGRATION_CONNECTION: graph.garmin.id,
    }
    return ids[route.scope], key.value


def test_registry_contains_only_the_five_reviewed_non_secret_mappings():
    expected = {
        ScopedSettingKey.UI_LANGUAGE: (
            SettingScope.USER,
            UserSetting,
            "user_id",
            None,
        ),
        ScopedSettingKey.ENABLED_MODULES: (
            SettingScope.SUBJECT,
            SubjectSetting,
            "subject_id",
            None,
        ),
        ScopedSettingKey.CUSTOM_CHARTS: (
            SettingScope.SUBJECT,
            SubjectSetting,
            "subject_id",
            None,
        ),
        ScopedSettingKey.WEEK_TEMPLATE: (
            SettingScope.SUBJECT,
            SubjectSetting,
            "subject_id",
            None,
        ),
        ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED: (
            SettingScope.INTEGRATION_CONNECTION,
            IntegrationConnectionSetting,
            "integration_connection_id",
            IntegrationProvider.GARMIN,
        ),
    }

    assert set(SCOPED_SETTING_REGISTRY) == set(expected)
    for key, expected_route in expected.items():
        route = SCOPED_SETTING_REGISTRY[key]
        assert (
            route.scope,
            route.model,
            route.scope_id_field,
            route.required_provider,
        ) == expected_route
        assert route.legacy_key == key.value

    with pytest.raises(TypeError):
        SCOPED_SETTING_REGISTRY[ScopedSettingKey.UI_LANGUAGE] = (  # type: ignore[index]
            SCOPED_SETTING_REGISTRY[ScopedSettingKey.UI_LANGUAGE]
        )


@pytest.mark.parametrize("key", list(ScopedSettingKey))
async def test_read_prefers_scoped_json_then_falls_back_to_legacy(db_session, key):
    graph = await _graph(db_session, f"read-{key.value}")
    request = _request(graph, key)
    legacy_value = {"origin": "legacy", "nested": [1, 2]}
    scoped_value = {"origin": "scoped", "nested": [3, 4]}
    db_session.add(AppSetting(key=key.value, value=legacy_value))
    await db_session.flush()

    fallback = await get_scoped_setting(db_session, **request)
    assert fallback == legacy_value

    route = SCOPED_SETTING_REGISTRY[key]
    db_session.add(
        route.model(
            **{
                route.scope_id_field: _scoped_pk(graph, key)[0],
                "key": key.value,
                "value": scoped_value,
            }
        )
    )
    await db_session.flush()

    preferred = await get_scoped_setting(db_session, **request)
    assert preferred == scoped_value
    preferred["nested"].append(99)
    assert await get_scoped_setting(db_session, **request) == scoped_value


async def test_read_returns_detached_default_when_both_rows_are_missing(db_session):
    graph = await _graph(db_session)
    default = {"enabled": ["weight"]}

    result = await get_scoped_setting(
        db_session,
        scope=SettingScope.SUBJECT,
        key=ScopedSettingKey.ENABLED_MODULES,
        subject_id=graph.subject.id,
        default=default,
    )

    assert result == default
    assert result is not default
    result["enabled"].append("labs")
    assert default == {"enabled": ["weight"]}


@pytest.mark.parametrize("key", list(ScopedSettingKey))
async def test_set_dual_writes_exact_scoped_model_and_legacy_row(db_session, key):
    graph = await _graph(db_session, f"write-{key.value}")
    request = _request(graph, key)
    first = {"revision": 1, "nested": [True, None, "value"]}

    returned = await set_scoped_setting(
        db_session,
        value=first,
        **request,
    )

    route = SCOPED_SETTING_REGISTRY[key]
    scoped = await db_session.get(route.model, _scoped_pk(graph, key))
    legacy = await db_session.get(AppSetting, key.value)
    assert returned == first
    assert returned is not first
    assert scoped is not None and scoped.value == first
    assert legacy is not None and legacy.value == first
    assert scoped.value is not legacy.value

    second = {"revision": 2, "nested": [False]}
    await set_scoped_setting(db_session, value=second, **request)
    assert scoped.value == second
    assert legacy.value == second

    # JSON changes are replacement assignments, never an untracked in-place edit.
    second["nested"].append("caller mutation")
    assert scoped.value == {"revision": 2, "nested": [False]}
    assert legacy.value == {"revision": 2, "nested": [False]}


async def test_set_flushes_but_leaves_commit_and_rollback_to_caller(db_session):
    graph = await _graph(db_session)
    subject_id = graph.subject.id
    await db_session.commit()

    await set_scoped_setting(
        db_session,
        scope=SettingScope.SUBJECT,
        key=ScopedSettingKey.WEEK_TEMPLATE,
        subject_id=subject_id,
        value={"monday": "training"},
    )

    assert db_session.in_transaction()
    assert await db_session.get(
        SubjectSetting,
        (subject_id, ScopedSettingKey.WEEK_TEMPLATE.value),
    ) is not None
    assert await db_session.get(
        AppSetting,
        ScopedSettingKey.WEEK_TEMPLATE.value,
    ) is not None

    await db_session.rollback()
    assert await db_session.get(
        SubjectSetting,
        (subject_id, ScopedSettingKey.WEEK_TEMPLATE.value),
    ) is None
    assert await db_session.get(
        AppSetting,
        ScopedSettingKey.WEEK_TEMPLATE.value,
    ) is None


async def test_dual_write_failure_rolls_back_both_existing_values(db_session):
    graph = await _graph(db_session)
    request = _request(graph, ScopedSettingKey.ENABLED_MODULES)
    await set_scoped_setting(db_session, value={"weight": True}, **request)
    subject_id = graph.subject.id
    await db_session.commit()

    with pytest.raises(StatementError):
        await set_scoped_setting(db_session, value=object(), **request)
    await db_session.rollback()

    scoped = await db_session.get(
        SubjectSetting,
        (subject_id, ScopedSettingKey.ENABLED_MODULES.value),
    )
    legacy = await db_session.get(
        AppSetting,
        ScopedSettingKey.ENABLED_MODULES.value,
    )
    assert scoped is not None and scoped.value == {"weight": True}
    assert legacy is not None and legacy.value == {"weight": True}


@pytest.mark.parametrize("key", list(ScopedSettingKey))
async def test_mirror_copies_each_known_legacy_value_once(db_session, key):
    graph = await _graph(db_session, f"mirror-{key.value}")
    request = _request(graph, key)
    value = {"key": key.value, "items": [1, 2]}
    db_session.add(AppSetting(key=key.value, value=value))
    await db_session.flush()

    assert await mirror_legacy_setting(db_session, **request) is True
    assert await mirror_legacy_setting(db_session, **request) is False

    route = SCOPED_SETTING_REGISTRY[key]
    scoped = await db_session.get(route.model, _scoped_pk(graph, key))
    legacy = await db_session.get(AppSetting, key.value)
    assert scoped is not None and scoped.value == value
    assert legacy is not None and legacy.value == value


async def test_mirror_missing_legacy_is_noop_and_drift_fails_closed(db_session):
    graph = await _graph(db_session)
    request = _request(graph, ScopedSettingKey.CUSTOM_CHARTS)

    assert await mirror_legacy_setting(db_session, **request) is False
    assert await db_session.get(
        SubjectSetting,
        (graph.subject.id, ScopedSettingKey.CUSTOM_CHARTS.value),
    ) is None

    db_session.add_all(
        [
            AppSetting(
                key=ScopedSettingKey.CUSTOM_CHARTS.value,
                value=[{"id": "legacy"}],
            ),
            SubjectSetting(
                subject_id=graph.subject.id,
                key=ScopedSettingKey.CUSTOM_CHARTS.value,
                value=[{"id": "scoped"}],
            ),
        ]
    )
    await db_session.flush()

    with pytest.raises(ScopedSettingDriftError):
        await mirror_legacy_setting(db_session, **request)


@pytest.mark.parametrize(
    "key",
    [
        "twofa_secret",
        "garmin_oauth_token",
        "password_reset",
        "vendor_api_key",
        "apikey",
        "credential_ref",
    ],
)
async def test_secret_like_keys_are_rejected_before_any_legacy_read(db_session, key):
    graph = await _graph(db_session, f"secret-{uuid.uuid4().hex}")
    db_session.add(AppSetting(key=key, value="must-not-be-readable"))
    await db_session.flush()

    with pytest.raises(ForbiddenScopedSettingKeyError):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.USER,
            key=key,
            user_id=graph.user.id,
        )


@pytest.mark.parametrize(
    "key",
    ["proactive", "unknown", "", " UI_LANGUAGE", "ui_language "],
)
async def test_unknown_and_excluded_keys_are_rejected_without_guessing_scope(
    db_session,
    key,
):
    graph = await _graph(db_session, f"unknown-{uuid.uuid4().hex}")
    db_session.add(AppSetting(key=key or "empty-placeholder", value="legacy"))
    await db_session.flush()

    with pytest.raises(UnknownScopedSettingKeyError):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.USER,
            key=key,
            user_id=graph.user.id,
        )


async def test_key_scope_mismatch_and_platform_scope_are_rejected(db_session):
    graph = await _graph(db_session)

    with pytest.raises(ScopedSettingScopeMismatchError):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.USER,
            key=ScopedSettingKey.ENABLED_MODULES,
            user_id=graph.user.id,
        )
    with pytest.raises(ScopedSettingValidationError, match="unknown.*scope"):
        await get_scoped_setting(
            db_session,
            scope="platform",
            key=ScopedSettingKey.UI_LANGUAGE,
            user_id=graph.user.id,
        )


@pytest.mark.parametrize("bad_id", [None, "not-a-uuid", uuid.UUID(int=0)])
async def test_missing_or_non_uuid_scope_is_rejected(db_session, bad_id):
    with pytest.raises(ScopedSettingValidationError, match="user_id"):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.USER,
            key=ScopedSettingKey.UI_LANGUAGE,
            user_id=bad_id,
        )


async def test_irrelevant_scope_identifiers_are_rejected(db_session):
    graph = await _graph(db_session)

    with pytest.raises(ScopedSettingScopeMismatchError):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.USER,
            key=ScopedSettingKey.UI_LANGUAGE,
            user_id=graph.user.id,
            subject_id=graph.subject.id,
        )
    with pytest.raises(ScopedSettingScopeMismatchError):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.SUBJECT,
            key=ScopedSettingKey.WEEK_TEMPLATE,
            user_id=graph.user.id,
            subject_id=graph.subject.id,
        )
    with pytest.raises(ScopedSettingScopeMismatchError):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.INTEGRATION_CONNECTION,
            key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED,
            user_id=graph.user.id,
            integration_connection_id=graph.garmin.id,
        )


@pytest.mark.parametrize("scope", [SettingScope.USER, SettingScope.SUBJECT])
async def test_nonexistent_scope_cannot_leak_a_legacy_singleton(db_session, scope):
    key = (
        ScopedSettingKey.UI_LANGUAGE
        if scope is SettingScope.USER
        else ScopedSettingKey.ENABLED_MODULES
    )
    db_session.add(AppSetting(key=key.value, value="private legacy value"))
    await db_session.flush()
    kwargs = {"user_id": uuid.uuid4()} if scope is SettingScope.USER else {
        "subject_id": uuid.uuid4()
    }

    with pytest.raises(ScopedSettingTargetNotFoundError):
        await get_scoped_setting(
            db_session,
            scope=scope,
            key=key,
            **kwargs,
        )


async def test_garmin_setting_rejects_wrong_provider_and_subject(db_session):
    first = await _graph(db_session, "connection-first")
    second = await _graph(db_session, "connection-second")

    with pytest.raises(ScopedSettingOwnershipError, match="garmin"):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.INTEGRATION_CONNECTION,
            key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED,
            subject_id=first.subject.id,
            integration_connection_id=first.hevy.id,
        )
    with pytest.raises(ScopedSettingOwnershipError, match="does not belong"):
        await set_scoped_setting(
            db_session,
            scope=SettingScope.INTEGRATION_CONNECTION,
            key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED,
            subject_id=second.subject.id,
            integration_connection_id=first.garmin.id,
            value=True,
        )

    assert await db_session.get(
        IntegrationConnectionSetting,
        (first.garmin.id, ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value),
    ) is None
    assert await db_session.get(
        AppSetting,
        ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value,
    ) is None


async def test_garmin_subject_check_is_optional_but_connection_must_exist(db_session):
    graph = await _graph(db_session)

    await set_scoped_setting(
        db_session,
        scope=SettingScope.INTEGRATION_CONNECTION,
        key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED,
        integration_connection_id=graph.garmin.id,
        value=True,
    )
    assert await get_scoped_setting(
        db_session,
        scope=SettingScope.INTEGRATION_CONNECTION,
        key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED,
        subject_id=graph.subject.id,
        integration_connection_id=graph.garmin.id,
    ) is True

    with pytest.raises(ScopedSettingTargetNotFoundError):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.INTEGRATION_CONNECTION,
            key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED,
            integration_connection_id=uuid.uuid4(),
        )


@pytest.mark.parametrize("key", list(ScopedSettingKey))
async def test_multiple_subjects_close_every_legacy_bridge_operation(db_session, key):
    first = await _graph(db_session, f"multi-first-{key.value}")
    await _graph(db_session, f"multi-second-{key.value}")
    request = _request(first, key)
    legacy_value = {"origin": "legacy", "unchanged": True}
    db_session.add(AppSetting(key=key.value, value=legacy_value))
    await db_session.flush()

    with pytest.raises(LegacyScopedSettingBridgeClosedError):
        await get_scoped_setting(db_session, **request)
    with pytest.raises(LegacyScopedSettingBridgeClosedError):
        await set_scoped_setting(
            db_session,
            value={"origin": "forbidden write"},
            **request,
        )
    with pytest.raises(LegacyScopedSettingBridgeClosedError):
        await mirror_legacy_setting(db_session, **request)

    route = SCOPED_SETTING_REGISTRY[key]
    assert await db_session.get(route.model, _scoped_pk(first, key)) is None
    legacy = await db_session.get(AppSetting, key.value)
    assert legacy is not None and legacy.value == legacy_value


async def test_user_bridge_requires_the_sole_subjects_active_owner(db_session):
    graph = await _graph(db_session)
    other_user = User(
        username="standalone-user",
        normalized_username="standalone-user",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add_all(
        [
            other_user,
            AppSetting(key=ScopedSettingKey.UI_LANGUAGE.value, value="ru"),
        ]
    )
    await db_session.flush()

    with pytest.raises(LegacyScopedSettingBridgeClosedError):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.USER,
            key=ScopedSettingKey.UI_LANGUAGE,
            user_id=other_user.id,
        )
    with pytest.raises(LegacyScopedSettingBridgeClosedError):
        await set_scoped_setting(
            db_session,
            scope=SettingScope.USER,
            key=ScopedSettingKey.UI_LANGUAGE,
            user_id=other_user.id,
            value="en",
        )

    assert await db_session.get(
        UserSetting,
        (other_user.id, ScopedSettingKey.UI_LANGUAGE.value),
    ) is None
    legacy = await db_session.get(AppSetting, ScopedSettingKey.UI_LANGUAGE.value)
    assert legacy is not None and legacy.value == "ru"
    assert graph.subject.owner_user_id != other_user.id


async def test_inactive_sole_owner_closes_legacy_fallback_and_writes(db_session):
    graph = await _graph(db_session)
    graph.user.status = UserStatus.SUSPENDED.value
    db_session.add(
        AppSetting(key=ScopedSettingKey.ENABLED_MODULES.value, value={"weight": True})
    )
    await db_session.flush()

    with pytest.raises(LegacyScopedSettingBridgeClosedError, match="active"):
        await get_scoped_setting(
            db_session,
            scope=SettingScope.SUBJECT,
            key=ScopedSettingKey.ENABLED_MODULES,
            subject_id=graph.subject.id,
        )
    with pytest.raises(LegacyScopedSettingBridgeClosedError, match="active"):
        await set_scoped_setting(
            db_session,
            scope=SettingScope.SUBJECT,
            key=ScopedSettingKey.ENABLED_MODULES,
            subject_id=graph.subject.id,
            value={"weight": False},
        )


async def test_retired_connection_can_read_scoped_but_cannot_touch_legacy(db_session):
    graph = await _graph(db_session)
    graph.garmin.status = IntegrationConnectionStatus.RETIRED.value
    graph.garmin.retired_at = datetime(2026, 8, 19, 12, tzinfo=UTC)
    scoped = IntegrationConnectionSetting(
        integration_connection_id=graph.garmin.id,
        key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value,
        value=False,
    )
    legacy = AppSetting(
        key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value,
        value=True,
    )
    db_session.add_all([scoped, legacy])
    await db_session.flush()
    request = _request(graph, ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED)

    assert await get_scoped_setting(db_session, **request) is False
    with pytest.raises(LegacyScopedSettingBridgeClosedError, match="retired"):
        await set_scoped_setting(db_session, value=True, **request)
    with pytest.raises(LegacyScopedSettingBridgeClosedError, match="retired"):
        await mirror_legacy_setting(db_session, **request)
    assert scoped.value is False
    assert legacy.value is True

    await db_session.delete(scoped)
    await db_session.flush()
    with pytest.raises(LegacyScopedSettingBridgeClosedError, match="retired"):
        await get_scoped_setting(db_session, **request)


@pytest.mark.integration
async def test_postgres_concurrent_first_writes_are_serialized_by_scope_root(
    db_session,
):
    """Concurrent absent-row inserts cannot race into a composite-PK failure.

    Whole-value replacement deliberately has last-writer-wins semantics, so
    there is no read/modify/write lost-update claim to make here.  This test pins
    the relevant concurrency guarantee: the second writer waits for the locked
    ownership root before it checks and inserts the setting rows.
    """

    graph = await _graph(db_session)
    subject_id = graph.subject.id
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    session_a = factory()
    await set_scoped_setting(
        session_a,
        scope=SettingScope.SUBJECT,
        key=ScopedSettingKey.WEEK_TEMPLATE,
        subject_id=subject_id,
        value={"writer": "a"},
    )

    async def write_b() -> None:
        async with factory() as session_b:
            await set_scoped_setting(
                session_b,
                scope=SettingScope.SUBJECT,
                key=ScopedSettingKey.WEEK_TEMPLATE,
                subject_id=subject_id,
                value={"writer": "b"},
            )
            await session_b.commit()

    task_b = asyncio.create_task(write_b())
    await asyncio.sleep(0.25)
    assert not task_b.done(), "writer B should wait on the subject row lock"

    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        scoped = await verify.get(
            SubjectSetting,
            (subject_id, ScopedSettingKey.WEEK_TEMPLATE.value),
        )
        legacy = await verify.get(
            AppSetting,
            ScopedSettingKey.WEEK_TEMPLATE.value,
        )
    assert scoped is not None and scoped.value == {"writer": "b"}
    assert legacy is not None and legacy.value == {"writer": "b"}


def test_service_has_no_cache_or_existing_service_dependencies():
    """Keep this bridge low-level until each product caller migrates explicitly."""

    import vitals.services.scoped_settings_service as service

    names = set(vars(service))
    assert not any("redis" in name.casefold() for name in names)
    assert not {
        "language_service",
        "modules_service",
        "custom_charts_service",
        "garmin_weight_service",
    } & names
