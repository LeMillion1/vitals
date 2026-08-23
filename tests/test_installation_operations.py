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

And the second-subject clause is not what closes these routes *right now* —
``resolve_legacy_ownership_context`` refuses a second subject before the gate is
reached, so today the routes fail with a resolution error. That is an accident of
the compatibility resolver, not a decision, and it disappears the moment
``resolve_access_context`` replaces it. The point of the gate is that the answer
is then still no, for a stated reason, instead of becoming yes.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

import pytest

from vitals.access import AccessContext, Principal
from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.services.installation_operator import (
    NotAnOperator,
    require_installation_operator,
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


async def test_the_sole_owner_is_the_operator_of_their_own_installation(db_session):
    """Self-hosted is the case this has to keep working unchanged."""

    subject = await _subject_with_owner(db_session, "operator-only")
    await require_installation_operator(
        db_session, access=_context(subject), operation="a restore"
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


async def test_another_persons_account_is_not_an_operator(db_session):
    """Sole-subject ownership is the clause, not merely being the only account."""

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


async def test_owning_a_subject_is_not_enough_when_it_is_not_the_only_one(db_session):
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

    async def _refuse(session, *, access, operation):
        raise NotAnOperator(f"{operation} is reserved for an operator")

    monkeypatch.setattr(
        settings_router, "require_installation_operator", _refuse
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


async def test_the_restart_route_asks_before_it_stops_the_process(
    auth_client, legacy_owner_roots, monkeypatch
):
    from web.routers import settings as settings_router

    async def _refuse(session, *, access, operation):
        raise NotAnOperator(f"{operation} is reserved for an operator")

    monkeypatch.setattr(
        settings_router, "require_installation_operator", _refuse
    )

    response = await auth_client.post("/settings/restart")
    assert response.status_code == 403
    assert "operator" in response.text


async def test_the_sole_owner_can_still_restore_and_restart(
    auth_client, legacy_owner_roots
):
    """The gate is new; the self-hosted behaviour it guards is not."""

    import io
    import json

    from vitals.services.data_portability_service import (
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
