"""Web startup boundary tests for the transitional legacy identity."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.services.identity_bootstrap import LegacyOwnerIdentityMismatchError
from vitals.utils.passwords import hash_password
from web.main import _bootstrap_legacy_identity


async def _count(session, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_startup_boundary_commits_the_configured_legacy_owner(
    db_session, session_factory
):
    await _bootstrap_legacy_identity(session_factory, timezone="Asia/Almaty")

    user = await db_session.scalar(select(User))
    assert user is not None
    assert user.username == "tester"
    assert user.status == UserStatus.ACTIVE.value
    assert await _count(db_session, UserRole) == 2
    assert await _count(db_session, HealthSubject) == 1


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
