"""The policy engine, finally with a caller.

``vitals/access.py`` has been complete and unused since PR-02: a principal, the
grants, and :func:`~vitals.access.is_allowed` that decides between them. Every
scoped path went instead through ``resolve_legacy_ownership_context``, which
answers a narrower question — *is this the sole owner of the sole subject?* —
and refuses outright when the answer involves more than one of either.

These tests pin what changes when the wider question is asked instead. Nothing,
for the installation as it stands: self-ownership authorizes on its own. What
becomes possible is a denial — a second person's record stops being an error
about the database's cardinality and becomes ordinary refused access.
"""

from __future__ import annotations

import uuid

import pytest

from vitals.access import PolicyAction, PolicyResourceType
from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.services.access_resolution import (
    AccessDeniedError,
    NoAccessibleSubjectError,
    PrincipalNotFoundError,
    SubjectNotFoundError,
    require_access,
    resolve_access_context,
)


async def _person(db_session, label: str, *, roles=(), status=UserStatus.ACTIVE):
    """One user and the health subject they own."""

    user = User(
        username=label,
        normalized_username=label,
        password_hash="$synthetic-test-hash",
        status=status.value,
    )
    db_session.add(user)
    await db_session.flush()
    for role in roles:
        db_session.add(UserRole(user_id=user.id, role=role.value))
    subject = HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty")
    db_session.add(subject)
    await db_session.flush()
    return user, subject


def _read_own_record(context):
    require_access(
        context,
        resource_type=PolicyResourceType.DOMAIN,
        resource_key="weight",
        action=PolicyAction.READ,
    )


# ── The ordinary case ────────────────────────────────────────────────────────

async def test_owner_reaches_their_own_record_without_any_grant(db_session):
    """Self-ownership is an authorization basis in its own right.

    This is the whole installation today, and it must not require a grant to be
    manufactured for it — a consent row that nobody gave is not consent.
    """

    user, subject = await _person(db_session, "owner")
    context = await resolve_access_context(
        db_session, user_id=user.id, subject_id=None
    )

    assert context.subject_id == subject.id
    assert context.subject_owner_user_id == user.id
    assert context.principal.user_id == user.id
    _read_own_record(context)


async def test_naming_your_own_subject_explicitly_is_the_same_answer(db_session):
    user, subject = await _person(db_session, "owner")
    context = await resolve_access_context(
        db_session, user_id=user.id, subject_id=subject.id
    )
    assert context.subject_id == subject.id
    _read_own_record(context)


# ── What a second subject now means ──────────────────────────────────────────

async def test_a_second_persons_record_is_denied_rather_than_undecidable(db_session):
    """The point of the change.

    The legacy resolver refuses as soon as a second subject exists, for either
    person, because it cannot tell whose installation it is. Here the owner
    still reaches their own record, and the other person's is simply denied.
    """

    first_user, first_subject = await _person(db_session, "first")
    _second_user, second_subject = await _person(db_session, "second")

    own = await resolve_access_context(
        db_session, user_id=first_user.id, subject_id=None
    )
    assert own.subject_id == first_subject.id
    _read_own_record(own)

    theirs = await resolve_access_context(
        db_session, user_id=first_user.id, subject_id=second_subject.id
    )
    assert theirs.subject_owner_user_id != first_user.id
    with pytest.raises(AccessDeniedError):
        _read_own_record(theirs)


async def test_a_role_is_not_a_grant(db_session):
    """A doctor is a doctor everywhere and a stranger to every record.

    Roles describe product capability. Reaching another person's data needs a
    live, actor-bound, exactly-scoped grant — which none of these have.
    """

    doctor, _ = await _person(
        db_session, "doctor", roles=(UserRoleName.DOCTOR,)
    )
    superadmin, _ = await _person(
        db_session, "superadmin", roles=(UserRoleName.PLATFORM_SUPERADMIN,)
    )
    _patient, patient_subject = await _person(db_session, "patient")

    for user in (doctor, superadmin):
        context = await resolve_access_context(
            db_session, user_id=user.id, subject_id=patient_subject.id
        )
        with pytest.raises(AccessDeniedError):
            _read_own_record(context)


async def test_building_a_context_authorizes_nothing_by_itself(db_session):
    """Resolution and authorization are separate steps, and must stay separate.

    A caller that resolved a context for somebody else's subject and then acted
    on having got one would have no boundary at all.
    """

    stranger, _ = await _person(db_session, "stranger")
    _other, other_subject = await _person(db_session, "other")

    context = await resolve_access_context(
        db_session, user_id=stranger.id, subject_id=other_subject.id
    )
    assert context.subject_id == other_subject.id  # resolved happily
    with pytest.raises(AccessDeniedError):  # and authorized nothing
        _read_own_record(context)


# ── Failing closed ───────────────────────────────────────────────────────────

async def test_a_suspended_user_is_not_a_principal(db_session):
    user, subject = await _person(
        db_session, "suspended", status=UserStatus.SUSPENDED
    )
    with pytest.raises(PrincipalNotFoundError):
        await resolve_access_context(
            db_session, user_id=user.id, subject_id=subject.id
        )


async def test_an_unknown_user_or_subject_is_refused(db_session):
    user, _ = await _person(db_session, "known")
    with pytest.raises(PrincipalNotFoundError):
        await resolve_access_context(
            db_session, user_id=uuid.uuid4(), subject_id=None
        )
    with pytest.raises(SubjectNotFoundError):
        await resolve_access_context(
            db_session, user_id=user.id, subject_id=uuid.uuid4()
        )


async def test_a_user_who_owns_no_subject_has_no_implicit_one(db_session):
    """The failure mode this replaces: falling back to the only record around."""

    orphan = User(
        username="orphan",
        normalized_username="orphan",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(orphan)
    await db_session.flush()
    await _person(db_session, "somebody-else")

    with pytest.raises(NoAccessibleSubjectError):
        await resolve_access_context(
            db_session, user_id=orphan.id, subject_id=None
        )


async def test_one_person_owns_at_most_one_record(db_session):
    """"The subject they own" is unambiguous because the schema says so.

    ``uq_health_subjects_owner_user_id`` is what makes the omitted-subject
    reading safe; without it, resolving "my record" would be a guess.
    """

    import sqlalchemy.exc

    user, _ = await _person(db_session, "double")
    db_session.add(HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_the_scope_is_exact_not_a_family(db_session):
    """A grant for one action on one domain is not a grant for its neighbours.

    Nothing here has a grant at all, but the shape matters: the request carries
    the exact resource and action, so widening either has to be a new decision.
    """

    user, _ = await _person(db_session, "exact")
    _other, other_subject = await _person(db_session, "exact-other")
    context = await resolve_access_context(
        db_session, user_id=user.id, subject_id=other_subject.id
    )

    for action in (PolicyAction.READ, PolicyAction.EXPORT, PolicyAction.DELETE):
        with pytest.raises(AccessDeniedError):
            require_access(
                context,
                resource_type=PolicyResourceType.DOMAIN,
                resource_key="labs",
                action=action,
            )


# ── The engine, in a production path ─────────────────────────────────────────

async def test_the_personal_export_routes_decide_rather_than_assume(
    auth_client, db_session, legacy_owner_roots, monkeypatch
):
    """Downloading one's record is authorized by policy, not merely by login.

    The whole-installation legacy snapshot has its own operator decision. These
    two downloads answer the subject-scoped question "what is mine", so what
    this pins is that the policy question is asked at all: make the engine
    refuse, and the download stops with a 403 rather than a crash or a file.

    Two requests, no more: the route is rate-limited to two per minute, and a
    test that trips its own limiter proves nothing about authorization.
    """

    from vitals.services import access_resolution

    asked: list[tuple[str, str]] = []

    def _refuse(context, request):
        asked.append((request.resource_key, request.action.value))
        return False

    monkeypatch.setattr(access_resolution, "is_allowed", _refuse)

    # Both branches of the handler: a browser asks for HTML, everything else
    # gets JSON. The first version of this test only exercised the JSON branch,
    # which is how an undefined name in the HTML one went unnoticed.
    for path, accept in (
        ("/settings/export-subject", "text/html,application/xhtml+xml"),
        ("/settings/export-llm", "application/json"),
    ):
        refused = await auth_client.get(path, headers={"Accept": accept})
        assert refused.status_code == 403
        # The refusal says nothing about whose record was reached for or
        # whether it exists: a denial and a miss look the same from outside.
        assert "vitals_backup" not in refused.text
        assert "weight_logs" not in refused.text

    assert asked == [("data_portability.export", "export")] * 2
