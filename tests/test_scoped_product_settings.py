"""Product-level contracts for Stage-2 scoped setting compatibility."""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import UserStatus
from vitals.models.app_settings import AppSetting
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import SubjectSetting, UserSetting
from vitals.services.charts import configuration as custom_charts_service
from vitals.services.modules import preferences as modules_service
from vitals.services.preferences import language as language_service
from vitals.services.settings.contracts import ScopedSettingKey


WEIGHT_SERIES = [{"domain": "weight", "metric_key": "weight.weight_kg"}]


async def _owner_graph(session: AsyncSession, slug: str) -> tuple[User, HealthSubject]:
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
    return user, subject


async def test_language_dual_writes_and_uses_only_uuid_cache(db_session, redis):
    user, _subject = await _owner_graph(db_session, "language-owner")

    assert await language_service.set_language(
        db_session,
        "ru",
        redis,
        user_id=user.id,
    ) == "ru"

    scoped = await db_session.get(
        UserSetting,
        (user.id, ScopedSettingKey.UI_LANGUAGE.value),
    )
    legacy = await db_session.get(AppSetting, ScopedSettingKey.UI_LANGUAGE.value)
    assert scoped is not None and scoped.value == "ru"
    assert legacy is not None and legacy.value == "ru"
    assert await redis.get(language_service.cache_key(user.id)) == "ru"
    assert await redis.get(language_service.REDIS_KEY) is None


async def test_modules_and_charts_dual_write_scoped_collections(db_session, redis):
    _user, subject = await _owner_graph(db_session, "subject-settings-owner")

    state = await modules_service.set_module_enabled(
        db_session,
        key="skincare",
        enabled=True,
        subject_id=subject.id,
    )
    await modules_service.prime_cache(
        redis,
        state,
        subject_id=subject.id,
    )
    chart = await custom_charts_service.create_chart(
        db_session,
        name="Owned chart",
        series=WEIGHT_SERIES,
        redis=redis,
        subject_id=subject.id,
    )

    modules_row = await db_session.get(
        SubjectSetting,
        (subject.id, ScopedSettingKey.ENABLED_MODULES.value),
    )
    charts_row = await db_session.get(
        SubjectSetting,
        (subject.id, ScopedSettingKey.CUSTOM_CHARTS.value),
    )
    assert modules_row is not None and modules_row.value["skincare"] is True
    assert charts_row is not None and charts_row.value[0]["id"] == chart["id"]
    assert (await db_session.get(AppSetting, "enabled_modules")).value == (
        modules_row.value
    )
    assert (await db_session.get(AppSetting, "custom_charts")).value == (
        charts_row.value
    )
    assert json.loads(
        await redis.get(modules_service.cache_key(subject.id))
    )["skincare"] is True
    assert json.loads(
        await redis.get(custom_charts_service.cache_key(subject.id))
    )[0]["id"] == chart["id"]
    assert await redis.get(modules_service.REDIS_KEY) is None
    assert await redis.get(custom_charts_service.REDIS_KEY) is None

    assert await custom_charts_service.delete_chart(
        db_session,
        chart["id"],
        redis,
        subject_id=subject.id,
    ) is True
    assert charts_row.value == []


async def test_scoped_reads_do_not_share_global_or_other_subject_cache(
    db_session,
    redis,
):
    first_user, first_subject = await _owner_graph(db_session, "first-owner")
    second_user, second_subject = await _owner_graph(db_session, "second-owner")
    db_session.add_all(
        [
            UserSetting(
                user_id=first_user.id,
                key=ScopedSettingKey.UI_LANGUAGE.value,
                value="ru",
            ),
            UserSetting(
                user_id=second_user.id,
                key=ScopedSettingKey.UI_LANGUAGE.value,
                value="en",
            ),
            SubjectSetting(
                subject_id=first_subject.id,
                key=ScopedSettingKey.ENABLED_MODULES.value,
                value={"skincare": True},
            ),
            SubjectSetting(
                subject_id=second_subject.id,
                key=ScopedSettingKey.ENABLED_MODULES.value,
                value={"skincare": False},
            ),
            SubjectSetting(
                subject_id=first_subject.id,
                key=ScopedSettingKey.CUSTOM_CHARTS.value,
                value=[
                    {
                        "id": "first-chart",
                        "name": "First",
                        "series": WEIGHT_SERIES,
                    }
                ],
            ),
            SubjectSetting(
                subject_id=second_subject.id,
                key=ScopedSettingKey.CUSTOM_CHARTS.value,
                value=[
                    {
                        "id": "second-chart",
                        "name": "Second",
                        "series": WEIGHT_SERIES,
                    }
                ],
            ),
        ]
    )
    await db_session.flush()
    await redis.set(language_service.REDIS_KEY, "global-leak")
    await redis.set(modules_service.REDIS_KEY, json.dumps({"skincare": True}))
    await redis.set(
        custom_charts_service.REDIS_KEY,
        json.dumps(
            [{"id": "global-chart", "name": "Global", "series": WEIGHT_SERIES}]
        ),
    )

    assert await language_service.get_language(
        db_session,
        redis,
        user_id=first_user.id,
    ) == "ru"
    assert await language_service.get_language(
        db_session,
        redis,
        user_id=second_user.id,
    ) == "en"
    assert (
        await modules_service.get_enabled_modules(
            db_session,
            redis,
            subject_id=first_subject.id,
        )
    )["skincare"] is True
    assert (
        await modules_service.get_enabled_modules(
            db_session,
            redis,
            subject_id=second_subject.id,
        )
    )["skincare"] is False
    assert [
        chart["id"]
        for chart in await custom_charts_service.list_charts(
            db_session,
            redis,
            subject_id=first_subject.id,
        )
    ] == ["first-chart"]
    assert [
        chart["id"]
        for chart in await custom_charts_service.list_charts(
            db_session,
            redis,
            subject_id=second_subject.id,
        )
    ] == ["second-chart"]


async def test_a_second_subject_stops_the_mirroring_and_not_the_setting(db_session):
    """The shared ``app_settings`` key is what a second subject retires.

    These settings each have two representations: a scoped row that belongs to
    one person, and one global key that belongs to the installation. Only the
    second stops meaning anything when there are two people — and writing it on
    one person's behalf would hand their choice to everybody still reading the
    fallback.

    This used to refuse all three, which closed the settings for everybody and,
    through the module gate, half the app with them. The scoped write proceeds
    now; the mirror is what stops.
    """

    from sqlalchemy import select

    from vitals.models.app_settings import AppSetting

    first_user, first_subject = await _owner_graph(db_session, "bridge-first")
    await _owner_graph(db_session, "bridge-second")

    await language_service.set_language(db_session, "ru", user_id=first_user.id)
    await modules_service.set_module_enabled(
        db_session,
        key="skincare",
        enabled=True,
        subject_id=first_subject.id,
    )
    await custom_charts_service.create_chart(
        db_session,
        name="Mine",
        series=WEIGHT_SERIES,
        subject_id=first_subject.id,
    )

    # Each landed in its own scope...
    assert await language_service.get_language(
        db_session, None, user_id=first_user.id
    ) == "ru"
    enabled = await modules_service.get_enabled_modules(
        db_session, subject_id=first_subject.id
    )
    assert enabled["skincare"] is True

    # ...and the installation-wide keys were left alone.
    shared = list(await db_session.scalars(select(AppSetting.key)))
    assert shared == [], shared


@pytest.mark.integration
async def test_scoped_module_updates_are_serialized(db_session):
    _user, subject = await _owner_graph(db_session, "concurrent-settings-owner")
    await modules_service.set_module_enabled(
        db_session,
        key="timeline",
        enabled=True,
        subject_id=subject.id,
    )
    subject_id = subject.id
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    session_a = factory()
    await modules_service.set_module_enabled(
        session_a,
        key="glp1",
        enabled=True,
        subject_id=subject_id,
    )

    async def toggle_b() -> None:
        async with factory() as session_b:
            await modules_service.set_module_enabled(
                session_b,
                key="hevy",
                enabled=True,
                subject_id=subject_id,
            )
            await session_b.commit()

    task_b = asyncio.create_task(toggle_b())
    await asyncio.sleep(0.25)
    assert not task_b.done()
    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        state = await modules_service.get_enabled_modules(
            verify,
            subject_id=subject_id,
        )
    assert state["timeline"] is True
    assert state["glp1"] is True
    assert state["hevy"] is True


async def test_web_setting_writes_land_in_scoped_rows(auth_client, db_session, redis):
    subject = await db_session.scalar(select(HealthSubject))
    user = await db_session.scalar(select(User))
    assert subject is not None and user is not None

    language_response = await auth_client.post(
        "/settings/language",
        data={"language": "en"},
    )
    module_response = await auth_client.post(
        "/settings/modules",
        data={"module": "skincare", "enabled": "true"},
    )
    chart_response = await auth_client.post(
        "/charts",
        data={
            "name": "Web owned",
            "domain": "weight",
            "metric_key": "weight.weight_kg",
            "param": "",
        },
    )

    assert language_response.status_code == 303
    assert module_response.status_code == 200
    assert chart_response.status_code == 303
    assert (
        await db_session.get(
            UserSetting,
            (user.id, ScopedSettingKey.UI_LANGUAGE.value),
        )
    ).value == "en"
    assert (
        await db_session.get(
            SubjectSetting,
            (subject.id, ScopedSettingKey.ENABLED_MODULES.value),
        )
    ).value["skincare"] is True
    charts = await db_session.get(
        SubjectSetting,
        (subject.id, ScopedSettingKey.CUSTOM_CHARTS.value),
    )
    assert charts is not None and charts.value[0]["name"] == "Web owned"
    assert await redis.get(language_service.cache_key(user.id)) == "en"
    assert json.loads(
        await redis.get(modules_service.cache_key(subject.id))
    )["skincare"] is True
