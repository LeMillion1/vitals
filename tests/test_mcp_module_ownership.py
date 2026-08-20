"""MCP v1 module settings stay on the verified singleton subject bridge."""

from __future__ import annotations

import pytest

from vitals.enums import UserStatus
from vitals.models.app_settings import AppSetting
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import SubjectSetting
from vitals.services import modules_service
from vitals.services.legacy_ownership import LegacySubjectResolutionError

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


async def test_module_tools_and_gate_fail_closed_with_a_second_subject(
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

    with pytest.raises(LegacySubjectResolutionError):
        await mcp_router.get_modules()
    with pytest.raises(LegacySubjectResolutionError):
        await mcp_router.set_module("body_comp", True)
    with pytest.raises(LegacySubjectResolutionError):
        await mcp_router.log_signal(key="headache", kind="symptom")
