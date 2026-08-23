"""MCP v1 module settings stay on the verified singleton subject bridge."""

from __future__ import annotations

import pytest

from vitals.enums import UserStatus
from vitals.models.app_settings import AppSetting
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import SubjectSetting
from vitals.services import modules_service

mcp_router = pytest.importorskip("web.routers.mcp")


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)


async def test_module_tools_read_and_atomically_dual_write_subject_state(
    db_session,
    legacy_owner_roots,
    redis,
    monkeypatch,
):
    initial = {key: True for key in modules_service.MODULE_REGISTRY}
    await db_session.merge(
        AppSetting(key=modules_service.SETTINGS_KEY, value=initial)
    )
    await db_session.merge(
        SubjectSetting(
            subject_id=legacy_owner_roots.subject_id,
            key=modules_service.SETTINGS_KEY,
            value=initial,
        )
    )
    await db_session.commit()
    await modules_service.prime_cache(
        redis,
        initial,
        subject_id=legacy_owner_roots.subject_id,
    )
    monkeypatch.setattr(mcp_router, "get_redis_client", lambda: redis)

    state = await mcp_router.get_modules()
    assert state["enabled"]["body_comp"] is True

    changed = await mcp_router.set_module("body_comp", False)
    assert changed["enabled"]["body_comp"] is False

    scoped = await db_session.get(
        SubjectSetting,
        (legacy_owner_roots.subject_id, modules_service.SETTINGS_KEY),
    )
    legacy = await db_session.get(AppSetting, modules_service.SETTINGS_KEY)
    assert scoped is not None and scoped.value["body_comp"] is False
    assert legacy is not None and legacy.value["body_comp"] is False
    cached = await modules_service.get_enabled_modules(
        db_session,
        redis,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert cached["body_comp"] is False


async def test_module_tools_stay_closed_with_a_second_subject(
    db_session,
    legacy_owner_roots,
):
    other = User(
        username="other",
        normalized_username="other",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        HealthSubject(
            owner_user_id=other.id,
            display_name="Other subject",
            timezone="UTC",
        )
    )
    await db_session.commit()

    # The module map is the owner's, and a second subject does not take it away.
    # It used to: the scoped-setting bridge refused any installation holding two
    # subjects, the map read as off, every optional page vanished, and the write
    # was refused. What the bridge actually needed a sole subject for is the
    # *shared* ``app_settings`` key — see test_scoped_settings_service — so that
    # is what stopped instead of the setting itself.
    from sqlalchemy import select

    from vitals.models.scoped_settings import SubjectSetting

    owner_subject_id = legacy_owner_roots.subject_id
    other_subject_id = await db_session.scalar(
        select(HealthSubject.id).where(
            HealthSubject.owner_user_id != legacy_owner_roots.user_id
        )
    )
    assert other_subject_id is not None

    modules = await mcp_router.get_modules()
    assert set(modules["enabled"]) == set(modules_service.DEFAULT_STATE)

    await mcp_router.set_module("body_comp", True)
    db_session.expire_all()

    owner_row = await db_session.get(
        SubjectSetting, (owner_subject_id, modules_service.SETTINGS_KEY)
    )
    assert owner_row is not None and owner_row.value["body_comp"] is True

    # Nothing was written for the other person, and the shared legacy key was
    # not overwritten with one subject's choice.
    assert await db_session.get(
        SubjectSetting, (other_subject_id, modules_service.SETTINGS_KEY)
    ) is None

