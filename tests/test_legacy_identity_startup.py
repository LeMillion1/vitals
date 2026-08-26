"""Web startup boundary tests for the transitional legacy identity."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.scoped_settings import SubjectSetting
from vitals.persistence.rls import bound_subject, in_platform_scope
from vitals.services import modules_service
from vitals.services.identity_bootstrap import LegacyOwnerIdentityMismatchError
from vitals.services.proactive import prefs
from vitals.services.scoped_settings_service import ScopedSettingKey
from vitals.utils.passwords import hash_password
from web.main import _bootstrap_legacy_identity, _load_oidc_identity_state


async def _count(session, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_startup_boundary_commits_the_configured_legacy_owner(
    db_session, session_factory
):
    preference_bundle = await _bootstrap_legacy_identity(
        session_factory,
        timezone="Asia/Almaty",
    )

    user = await db_session.scalar(select(User))
    assert user is not None
    assert user.username == "tester"
    assert user.status == UserStatus.ACTIVE.value
    assert await _count(db_session, UserRole) == 2
    assert await _count(db_session, HealthSubject) == 1
    subject_id = await db_session.scalar(select(HealthSubject.id))
    assert subject_id is not None
    module_policy = await db_session.scalar(
        select(SubjectSetting).where(
            SubjectSetting.subject_id == subject_id,
            SubjectSetting.key == ScopedSettingKey.ENABLED_MODULES.value,
        )
    )
    assert module_policy is not None
    assert module_policy.value == modules_service.DEFAULT_STATE
    assert preference_bundle.as_flat_dict() == prefs.sanitize(None)
    assert bound_subject(db_session) == subject_id
    assert not in_platform_scope(db_session)


async def test_startup_boundary_rolls_back_and_propagates_identity_mismatch(
    db_session, session_factory
):
    db_session.add(
        User(
            username="Different owner",
            normalized_username="different owner",
            password_hash=hash_password("different-password"),
            status=UserStatus.ACTIVE.value,
        )
    )
    await db_session.commit()
    rollback = AsyncMock(wraps=db_session.rollback)
    db_session.rollback = rollback

    with pytest.raises(LegacyOwnerIdentityMismatchError):
        await _bootstrap_legacy_identity(session_factory, timezone="Asia/Almaty")

    rollback.assert_awaited_once()
    assert await _count(db_session, User) == 1
    assert await _count(db_session, UserRole) == 0
    assert await _count(db_session, HealthSubject) == 0


async def test_oidc_startup_needs_no_legacy_environment_credential(
    db_session,
    session_factory,
    monkeypatch,
):
    expected = await _bootstrap_legacy_identity(
        session_factory,
        timezone="Asia/Almaty",
    )
    persisted_hash = await db_session.scalar(select(User.password_hash))

    for name, value in (
        ("VITALS_OIDC_ISSUER", "https://idp.example.test"),
        ("VITALS_OIDC_CLIENT_ID", "vitals"),
        ("VITALS_OIDC_CLIENT_SECRET", "synthetic-secret"),
        (
            "VITALS_OIDC_REDIRECT_URL",
            "https://vitals.example.test/auth/callback",
        ),
        ("VITALS_OIDC_BOOTSTRAP_SUBJECT", "provider-owner-subject"),
        ("VITALS_PUBLIC_URL", "https://vitals.example.test"),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("VITALS_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("VITALS_AUTH_PASSWORD_HASH", raising=False)

    actual = await _load_oidc_identity_state(session_factory)

    assert actual == expected
    assert await _count(db_session, User) == 1
    assert await db_session.scalar(select(User.password_hash)) == persisted_hash
