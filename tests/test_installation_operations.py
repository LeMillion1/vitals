"""Operations that are about the installation, not about anybody's record.

Restoring a backup replaces portable data for every subject in the database, and
restarting takes the whole process down. Neither is a question about one health
record, and until now neither was a question at all: ``/settings/import`` had no
authorization beyond holding a session — its paired ``/settings/export`` did —
and ``/settings/restart`` had none either.

That was survivable while an installation was one person. It stops being
survivable at the second account, which is what this migration is producing.

Two things are worth saying plainly about where this sits today.

The shape of the check matters as much as its presence. Asking the subject-scoped
policy engine about a restore, with the caller's own subject as the resource,
would read like a check while being unconditionally true: self-ownership
authorizes everything on one's own subject, so every account would pass it. That
trap is pinned below as a test rather than left as a comment.

The commercial bootstrap gives the historical owner the explicit
``platform_superadmin`` role.  That role, not the number of health subjects or
ownership of one of them, is the stable installation-operation capability.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from vitals.access import AccessContext, Principal
from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.services.installation_operator import (
    NotAnOperator,
    require_installation_operator,
    require_installation_operator_user,
)


async def _subject_with_owner(session, slug: str) -> HealthSubject:
    user = User(
        username=slug,
        normalized_username=slug,
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
    return subject


def _context(subject: HealthSubject, *, roles=frozenset()) -> AccessContext:
    """The context the resolver would produce for this subject's owner."""

    return AccessContext(
        principal=Principal(
            user_id=subject.owner_user_id,
            roles=roles,
            session_version=1,
        ),
        subject_id=subject.id,
        subject_owner_user_id=subject.owner_user_id,
        evaluated_at=datetime.now(UTC),
    )


# ── The service ──────────────────────────────────────────────────────────────


async def test_subject_ownership_never_implies_platform_authority(db_session):
    """One medical record is not an implicit process-control capability."""

    subject = await _subject_with_owner(db_session, "operator-only")
    with pytest.raises(NotAnOperator, match="reserved for an operator"):
        await require_installation_operator(
            db_session, access=_context(subject), operation="a restore"
        )
    with pytest.raises(NotAnOperator, match="reserved for an operator"):
        await require_installation_operator_user(
            db_session,
            user_id=subject.owner_user_id,
            operation="a restart",
        )


async def test_a_second_subject_closes_the_installation_operations(db_session):
    """Not degraded, not scoped down — closed, until somebody holds the role.

    These operations cannot be made safe per-subject: a full restore wipes
    portable tables for everybody. Refusing is the honest answer; guessing which
    half of the database the caller meant is not.
    """

    subject = await _subject_with_owner(db_session, "operator-first")
    await _subject_with_owner(db_session, "operator-second")

    with pytest.raises(NotAnOperator, match="reserved for an operator"):
        await require_installation_operator(
            db_session, access=_context(subject), operation="a restore"
        )


async def test_a_platform_superadmin_stays_an_operator(db_session):
    """The role is what carries the capability once ownership stops implying it."""

    subject = await _subject_with_owner(db_session, "operator-admin")
    await _subject_with_owner(db_session, "operator-admin-second")

    await require_installation_operator(
        db_session,
        access=_context(
            subject, roles=frozenset({UserRoleName.PLATFORM_SUPERADMIN})
        ),
        operation="a restore",
    )


async def test_a_platform_superadmin_needs_no_personal_record(db_session):
    """Control-plane authority must not depend on owning patient data."""

    await _subject_with_owner(db_session, "operator-record")
    admin = User(
        username="operator-without-record",
        normalized_username="operator-without-record",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(admin)
    await db_session.flush()
    db_session.add(
        UserRole(user_id=admin.id, role=UserRoleName.PLATFORM_SUPERADMIN.value)
    )
    await db_session.flush()

    await require_installation_operator_user(
        db_session, user_id=admin.id, operation="a full portability export"
    )


async def test_another_persons_account_is_not_an_operator(db_session):
    """Selecting somebody's subject cannot create platform authority."""

    subject = await _subject_with_owner(db_session, "operator-owner")
    stranger = _context(subject)
    stranger = dataclasses.replace(
        stranger,
        principal=dataclasses.replace(stranger.principal, user_id=uuid.uuid4()),
    )

    with pytest.raises(NotAnOperator, match="reserved for an operator"):
        await require_installation_operator(
            db_session, access=stranger, operation="a restore"
        )


async def test_no_principal_is_not_an_operator(db_session):
    with pytest.raises(NotAnOperator, match="needs a principal"):
        await require_installation_operator(
            db_session, access=None, operation="a restore"
        )


async def test_owning_a_subject_is_not_platform_authority(db_session):
    """The trap this check exists to avoid, stated as a test.

    The caller owns the subject their access context selected, so a
    subject-scoped policy question says yes — correctly, about a question nobody
    asked. Owning one record in a shared installation does not make somebody the
    operator of the installation.
    """

    from vitals.access import (
        AccessRequest,
        PolicyAction,
        PolicyResourceType,
        is_allowed,
    )

    subject = await _subject_with_owner(db_session, "operator-trap")
    await _subject_with_owner(db_session, "operator-trap-second")
    access = _context(subject)

    assert is_allowed(
        access,
        AccessRequest(
            subject_id=access.subject_id,
            resource_type=PolicyResourceType.OPERATION,
            resource_key="data_portability.restore",
            action=PolicyAction.CREATE,
        ),
    )
    with pytest.raises(NotAnOperator):
        await require_installation_operator(
            db_session, access=access, operation="a restore"
        )


# ── The routes ───────────────────────────────────────────────────────────────


async def test_the_restore_route_asks_before_it_wipes(
    auth_client, db_session, legacy_owner_roots, monkeypatch
):
    """The dangerous half of the pair, which had no decision at all before.

    Driven by making the gate refuse rather than by seeding a second subject:
    the compatibility resolver would fail first, and the assertion would then be
    about that failure rather than about this one.
    """

    from web.routers import settings as settings_router

    async def _refuse(session, *, user_id, operation):
        del session, user_id
        raise NotAnOperator(f"{operation} is reserved for an operator")

    monkeypatch.setattr(
        settings_router, "require_installation_operator_user", _refuse
    )

    import io
    import json

    payload = json.dumps({"metadata": {"version": "1.0"}})
    response = await auth_client.post(
        "/settings/import",
        files={
            "backup_file": (
                "backup.json",
                io.BytesIO(payload.encode()),
                "application/json",
            )
        },
    )
    assert response.status_code == 403
    assert "operator" in response.text


async def test_the_full_export_route_is_operator_only(
    auth_client, legacy_owner_roots, monkeypatch
):
    from web.routers import settings as settings_router

    async def _refuse(session, *, user_id, operation):
        del session, user_id
        raise NotAnOperator(f"{operation} is reserved for an operator")

    monkeypatch.setattr(
        settings_router, "require_installation_operator_user", _refuse
    )

    response = await auth_client.get("/settings/export")
    assert response.status_code == 403
    assert "operator" in response.text


async def test_the_restart_route_asks_before_it_stops_the_process(
    auth_client, legacy_owner_roots, monkeypatch
):
    from web.routers import settings as settings_router

    async def _refuse(session, *, user_id, operation):
        del session, user_id
        raise NotAnOperator(f"{operation} is reserved for an operator")

    monkeypatch.setattr(
        settings_router, "require_installation_operator_user", _refuse
    )

    response = await auth_client.post("/settings/restart")
    assert response.status_code == 403
    assert "operator" in response.text


async def test_a_member_cannot_see_or_trigger_restart_as_the_sole_owner(
    auth_client,
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    role = await db_session.scalar(
        select(UserRole).where(
            UserRole.user_id == legacy_owner_roots.user_id,
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
        )
    )
    assert role is not None
    await db_session.delete(role)
    await db_session.commit()

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))

    page = await auth_client.get("/settings", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "triggerRestart" not in page.text
    assert 'href="/settings/platform"' not in page.text

    response = await auth_client.post("/settings/restart")
    assert response.status_code == 403
    assert killed == []


async def test_restart_requires_a_recent_login(auth_client, monkeypatch):
    from web.authentication import tokens

    monkeypatch.setattr(
        tokens,
        "session_issued_at",
        lambda _token: datetime.now(UTC) - timedelta(hours=1),
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))

    response = await auth_client.post("/settings/restart")

    assert response.status_code == 401
    assert response.json() == {"detail": "Recent authentication required"}
    assert killed == []


async def test_the_bootstrap_platform_admin_can_restore(
    auth_client, legacy_owner_roots
):
    """The historical owner keeps the operation through its explicit role."""

    import io
    import json

    from vitals.services.portability.v1_contract import (
        BACKUP_VERSION,
        KIND_FULL,
    )

    payload = json.dumps(
        {
            "metadata": {"version": BACKUP_VERSION, "kind": KIND_FULL},
            "app_settings": [],
        }
    )
    response = await auth_client.post(
        "/settings/import",
        files={
            "backup_file": (
                "backup.json",
                io.BytesIO(payload.encode()),
                "application/json",
            )
        },
    )
    assert response.status_code == 200, response.text


async def test_shared_installation_restore_is_409_before_any_mutation(
    auth_client, db_session, legacy_owner_roots
):
    """Legacy v1 is refused by type and leaves existing portable rows intact."""

    import io
    import json

    from vitals.models.app_settings import AppSetting
    from vitals.services.portability.v1_contract import BACKUP_VERSION, KIND_FULL

    await _subject_with_owner(db_session, "restore-second-record")
    marker = AppSetting(key="multi_subject_restore_guard", value="kept")
    db_session.add(marker)
    await db_session.commit()

    payload = json.dumps(
        {
            "metadata": {"version": BACKUP_VERSION, "kind": KIND_FULL},
            "app_settings": [],
        }
    )
    response = await auth_client.post(
        "/settings/import",
        files={
            "backup_file": (
                "backup.json",
                io.BytesIO(payload.encode()),
                "application/json",
            )
        },
    )

    assert response.status_code == 409
    db_session.expire_all()
    assert await db_session.get(AppSetting, "multi_subject_restore_guard") is not None
