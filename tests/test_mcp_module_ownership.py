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

    # Refused a layer lower than it used to be, and still refused.
    #
    # ``resolve_legacy_ownership_context`` no longer rejects an installation for
    # holding a second subject — it selects the actor's own record, because
    # otherwise a doctor taking on one patient lost their own dashboard. The
    # scoped-setting bridge behind the module map is its own sole-subject gate,
    # and 36 more like it remain across the services. Each guards something
    # specific, so they are being retired one at a time rather than in a sweep.
    from vitals.services.scoped_settings_service import (
        LegacyScopedSettingBridgeClosedError,
    )

    closed = (
        LegacyScopedSettingBridgeClosedError,
        LegacySubjectResolutionError,
    )

    # Reading the module map degrades rather than refusing: ``get_enabled_modules``
    # has always caught a failed read and answered with the safe defaults, so
    # nothing here can hand back another subject's settings — the worst case is
    # a map nobody configured.
    modules = await mcp_router.get_modules()
    assert modules["enabled"] == modules_service.DEFAULT_STATE

    # Anything that would write still stops, though not all in the same place:
    # set_module hits the scoped-setting bridge, log_signal the conflict
    # engine's. Both are sole-subject gates of the same family.
    from vitals.services import conflict_engine

    closed = closed + (conflict_engine.ConflictLegacyBridgeError,)
    with pytest.raises(closed):
        await mcp_router.set_module("body_comp", True)

    # log_signal is gated by the signals module, which now reads as off from
    # the degraded default map — so it declines rather than raising one of the
    # bridge errors. A different shape of "no", and still a no.
    signal_result = await mcp_router.log_signal(key="headache", kind="symptom")
    assert signal_result.get("ok") is not True, signal_result

    # And nothing reached the other person's record, which is the property that
    # has to hold whichever gate does the stopping.
    from sqlalchemy import func, select

    from vitals.models.signals import Signal

    other_subject_id = await db_session.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == other.id)
    )
    assert await db_session.scalar(
        select(func.count())
        .select_from(Signal)
        .where(Signal.subject_id == other_subject_id)
    ) == 0
