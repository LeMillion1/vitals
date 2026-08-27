"""Role-aware destinations for personal pages opened by recordless accounts."""
from __future__ import annotations

import re

import pytest

from vitals.enums import UserRoleName, UserStatus
from vitals.models.app_settings import AppSetting
from vitals.models.identity import User, UserRole
from vitals.services.preferences import language as language_service
from web.auth import create_session
from web.config import SESSION_COOKIE

pytestmark = pytest.mark.asyncio


async def _recordless_user(db_session, slug: str, *, roles=()) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    for role in roles:
        db_session.add(UserRole(user_id=user.id, role=role.value))
    await db_session.flush()
    return user


def _sign_in(client, user: User) -> None:
    client.cookies.set(SESSION_COOKIE, create_session(user.username))


def _primary_action(body: str) -> tuple[str, str]:
    match = re.search(
        r'<a href="([^"]+)" class="v-btn justify-center">([^<]+)</a>', body
    )
    assert match is not None, "recordless refusal has no primary action"
    return match.group(1), match.group(2).strip()


@pytest.mark.parametrize(
    ("lang", "headline", "body_copy", "primary_label"),
    (
        (
            "en",
            "This operator account has no health record",
            "Use Platform to manage this installation",
            "Open Platform",
        ),
        (
            "ru",
            "У операторского аккаунта нет медицинской записи",
            "Управляйте инсталляцией в разделе «Платформа»",
            "Открыть платформу",
        ),
    ),
)
async def test_recordless_platform_operator_gets_its_platform_way_out(
    client,
    db_session,
    legacy_owner_roots,
    lang,
    headline,
    body_copy,
    primary_label,
):
    operator = await _recordless_user(
        db_session,
        f"recordless-platform-{lang}",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    await db_session.merge(
        AppSetting(key=language_service.SETTINGS_KEY, value=lang)
    )
    await db_session.commit()
    _sign_in(client, operator)

    response = await client.get(
        "/settings/access",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert headline in response.text
    assert body_copy in response.text
    assert _primary_action(response.text) == ("/settings/platform", primary_label)
    del legacy_owner_roots


async def test_professional_workspace_keeps_precedence_over_platform_role(
    client, db_session, legacy_owner_roots
):
    operator_doctor = await _recordless_user(
        db_session,
        "recordless-platform-doctor",
        roles=(UserRoleName.DOCTOR, UserRoleName.PLATFORM_SUPERADMIN),
    )
    await db_session.commit()
    _sign_in(client, operator_doctor)

    response = await client.get(
        "/settings/access",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/care"
    del legacy_owner_roots


async def test_other_recordless_account_keeps_the_existing_generic_refusal(
    client, db_session, legacy_owner_roots
):
    account = await _recordless_user(db_session, "recordless-generic")
    await db_session.merge(
        AppSetting(key=language_service.SETTINGS_KEY, value="en")
    )
    await db_session.commit()
    _sign_in(client, account)

    response = await client.get(
        "/settings/access",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "This account keeps no record of its own" in response.text
    assert _primary_action(response.text) == ("/care", "Go to care")
    assert "This operator account has no health record" not in response.text
    del legacy_owner_roots
