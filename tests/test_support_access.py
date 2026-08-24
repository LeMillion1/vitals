"""Platform support reaches a record only when the patient said it could.

The policy engine has understood support grants since PR-02 and nothing ever
created one, so every rule below was written and unreachable. These tests are
mostly about the ways it could quietly stop being true now that the path exists.

Four rules, one section each. An admin asks and the patient answers. A grant is
bounded in time and in scope. Either side can end it. Nothing is deleted.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from vitals.access import (
    AccessRequest,
    PolicyAction,
    PolicyResourceType,
    is_allowed,
)
from vitals.enums import (
    Domain,
    SupportAccessMode,
    SupportAccessRequestStatus,
    SupportAccessStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import AuditEvent, HealthSubject, User, UserRole
from vitals.services import support_access_service as support
from vitals.services.access_resolution import resolve_access_context


async def _user(session, slug: str, *, roles=()) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role=role.value))
    await session.flush()
    return user


async def _patient(session, slug: str):
    owner = await _user(session, slug)
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return owner, subject


async def _admin(session, slug: str) -> User:
    return await _user(session, slug, roles=(UserRoleName.PLATFORM_SUPERADMIN,))


async def _ask(session, *, admin, subject, domains=(Domain.LABS,), **kwargs):
    return await support.open_request(
        session,
        admin_user_id=admin.id,
        subject_id=subject.id,
        reason="Investigating a failed lab import reported in ticket 41.",
        scopes=support.read_scopes_for(domains),
        **kwargs,
    )


def _labs_read(subject_id):
    return AccessRequest(
        subject_id=subject_id,
        resource_type=PolicyResourceType.DOMAIN,
        resource_key=Domain.LABS.value,
        action=PolicyAction.READ,
    )


# ── The admin asks and the patient answers ───────────────────────────────────


async def test_an_approved_grant_is_what_finally_authorizes_the_admin(db_session):
    """The whole path, end to end, because none of it was reachable before.

    ``_support_allows`` has been in ``vitals/access.py`` since PR-02 and no code
    ever built a grant for it to read, so a superadmin's role authorized exactly
    nothing and the branch was dead. This is the test that the branch is alive.
    """

    owner, subject = await _patient(db_session, "sup-happy")
    admin = await _admin(db_session, "sup-happy-admin")

    context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )
    assert context.support_grant is None
    assert not is_allowed(context, _labs_read(subject.id))

    request = await _ask(db_session, admin=admin, subject=subject)
    await db_session.commit()

    # Asking is not access. The request exists and the answer is still no.
    context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )
    assert context.support_grant is None
    assert not is_allowed(context, _labs_read(subject.id))

    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()

    context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )
    assert context.support_grant is not None
    assert is_allowed(context, _labs_read(subject.id))


async def test_only_the_patient_may_approve_the_ask_about_their_record(db_session):
    """Not another admin, not the asker, not a stranger."""

    owner, subject = await _patient(db_session, "sup-approver")
    admin = await _admin(db_session, "sup-approver-admin")
    other_admin = await _admin(db_session, "sup-approver-admin2")
    stranger, _other_subject = await _patient(db_session, "sup-approver-stranger")

    request = await _ask(db_session, admin=admin, subject=subject)
    await db_session.commit()

    # Ids read before the loop. A rollback expires every ORM object, and
    # refreshing one needs IO that an attribute access cannot await.
    request_id = request.id
    actors = [admin.id, other_admin.id, stranger.id]

    for actor_id in actors:
        with pytest.raises(support.NotTheSubjectOwner):
            await support.approve_request(
                db_session, owner_user_id=actor_id, request_id=request_id
            )
        await db_session.rollback()


async def test_an_admin_cannot_ask_themselves_about_their_own_record(db_session):
    """Refused in words here, and by the schema one layer down.

    ``ck_support_access_grants_no_self_approval`` would catch it too, as an
    IntegrityError. "You cannot approve your own request" is a sentence; a check
    constraint violation is a stack trace.
    """

    admin = await _admin(db_session, "sup-self")
    subject = HealthSubject(
        owner_user_id=admin.id, display_name="Own record", timezone="UTC"
    )
    db_session.add(subject)
    await db_session.flush()

    with pytest.raises(support.NotTheSubjectOwner):
        await _ask(db_session, admin=admin, subject=subject)


async def test_somebody_who_is_not_an_admin_cannot_ask_at_all(db_session):
    owner, subject = await _patient(db_session, "sup-notadmin")
    nobody = await _user(db_session, "sup-notadmin-actor")

    with pytest.raises(support.NotAPlatformAdmin):
        await _ask(db_session, admin=nobody, subject=subject)
    del owner


async def test_a_role_removed_while_the_ask_waited_is_not_rewarded_by_the_answer(
    db_session,
):
    """The check is at approval too, not only at the ask.

    A request can sit for a week. If the person who made it stopped being an
    administrator in the meantime — suspended, demoted, gone — an approval
    arriving afterwards must not hand them a live grant. Checking only at the
    ask would make the patient's yes authorize somebody the platform had
    already removed.
    """

    owner, subject = await _patient(db_session, "sup-demoted")
    admin = await _admin(db_session, "sup-demoted-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    await db_session.commit()

    role = await db_session.scalar(
        UserRole.__table__.select().where(UserRole.user_id == admin.id)
    )
    await db_session.execute(
        UserRole.__table__.delete().where(UserRole.user_id == admin.id)
    )
    await db_session.commit()
    assert role is not None

    with pytest.raises(support.NotAPlatformAdmin):
        await support.approve_request(
            db_session, owner_user_id=owner.id, request_id=request.id
        )


# ── Bounded in time and in scope ─────────────────────────────────────────────


async def test_a_grant_authorizes_only_the_domains_that_were_asked_for(db_session):
    """The approval screen lists domains, and the grant is that list.

    A support grant that reads as "labs" and opens nutrition would make the
    patient's decision meaningless — they agreed to a specific thing.
    """

    owner, subject = await _patient(db_session, "sup-scope")
    admin = await _admin(db_session, "sup-scope-admin")
    request = await _ask(db_session, admin=admin, subject=subject, domains=(Domain.LABS,))
    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()

    context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )
    assert is_allowed(context, _labs_read(subject.id))
    assert not is_allowed(
        context,
        AccessRequest(
            subject_id=subject.id,
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=Domain.NUTRITION.value,
            action=PolicyAction.READ,
        ),
    )


async def test_a_read_grant_does_not_authorize_a_write(db_session):
    """Mode is a ceiling, and ``read`` has nothing above it."""

    owner, subject = await _patient(db_session, "sup-mode")
    admin = await _admin(db_session, "sup-mode-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()

    context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )
    for action in (PolicyAction.UPDATE, PolicyAction.DELETE, PolicyAction.REPAIR,
                   PolicyAction.EXPORT, PolicyAction.SHARE):
        assert not is_allowed(
            context,
            AccessRequest(
                subject_id=subject.id,
                resource_type=PolicyResourceType.DOMAIN,
                resource_key=Domain.LABS.value,
                action=action,
            ),
        )


async def test_repair_and_export_are_refused_by_name_rather_than_half_built(
    db_session,
):
    """A mode accepted and unimplemented is worse than one that says so.

    It would read as approved to the patient and then either do nothing or do
    something nobody designed. Both need their own review — a bounded diff for
    repair, a separate approval for export — and the roadmap sequences them
    after this.
    """

    owner, subject = await _patient(db_session, "sup-unimpl")
    admin = await _admin(db_session, "sup-unimpl-admin")

    for mode in (SupportAccessMode.REPAIR, SupportAccessMode.EXPORT):
        with pytest.raises(support.UnsupportedMode):
            await _ask(db_session, admin=admin, subject=subject, mode=mode)
    del owner


async def test_an_ask_with_no_scopes_is_refused(db_session):
    """A grant with no scope rows authorizes nothing, which is not a grant."""

    owner, subject = await _patient(db_session, "sup-noscope")
    admin = await _admin(db_session, "sup-noscope-admin")

    with pytest.raises(support.ScopesRequired):
        await support.open_request(
            db_session,
            admin_user_id=admin.id,
            subject_id=subject.id,
            reason="Nothing in particular.",
            scopes=(),
        )
    del owner


async def test_an_ask_without_a_reason_is_refused(db_session):
    """The reason is shown to the patient verbatim; an approval asked for
    without one is not an informed approval of anything."""

    owner, subject = await _patient(db_session, "sup-noreason")
    admin = await _admin(db_session, "sup-noreason-admin")

    with pytest.raises(support.SupportAccessError):
        await support.open_request(
            db_session,
            admin_user_id=admin.id,
            subject_id=subject.id,
            reason="   ",
            scopes=support.read_scopes_for((Domain.LABS,)),
        )
    del owner


async def test_a_grant_longer_than_a_day_is_refused(db_session):
    owner, subject = await _patient(db_session, "sup-ttl")
    admin = await _admin(db_session, "sup-ttl-admin")

    with pytest.raises(support.SupportAccessError):
        await _ask(db_session, admin=admin, subject=subject, ttl=timedelta(days=2))
    del owner


async def test_an_expired_grant_stops_authorizing_without_anybody_doing_anything(
    db_session,
):
    """The clock is the enforcement, not a job that has to have run.

    ``expire_stale`` exists to make the screens honest, and if authorization
    depended on it a missed run would leave a grant open. So this ages the grant
    without calling it.
    """

    owner, subject = await _patient(db_session, "sup-expiry")
    admin = await _admin(db_session, "sup-expiry-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    grant = await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()

    # Aged by moving both ends back together: the schema requires the expiry to
    # stay strictly after the approval.
    lapsed = grant.approved_at - timedelta(hours=6)
    grant.approved_at = lapsed
    grant.expires_at = lapsed + timedelta(hours=1)
    await db_session.commit()

    context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )
    assert context.support_grant is None
    assert not is_allowed(context, _labs_read(subject.id))


# ── Either side can end it ───────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["patient", "admin"])
async def test_a_live_grant_can_be_revoked_by_either_side(db_session, who):
    """The patient must not have to find somebody to change their mind, and the
    admin should be able to put the access down rather than wait it out."""

    owner, subject = await _patient(db_session, f"sup-revoke-{who}")
    admin = await _admin(db_session, f"sup-revoke-{who}-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    grant = await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()

    actor = owner if who == "patient" else admin
    await support.revoke_grant(
        db_session,
        actor_user_id=actor.id,
        grant_id=grant.id,
        reason="Finished with it.",
    )
    await db_session.commit()

    context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )
    assert context.support_grant is None
    assert not is_allowed(context, _labs_read(subject.id))


async def test_a_stranger_cannot_revoke_somebody_elses_grant(db_session):
    """And is told it does not exist rather than that they may not touch it."""

    owner, subject = await _patient(db_session, "sup-revoke-stranger")
    admin = await _admin(db_session, "sup-revoke-stranger-admin")
    outsider = await _admin(db_session, "sup-revoke-stranger-outsider")
    request = await _ask(db_session, admin=admin, subject=subject)
    grant = await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()

    with pytest.raises(support.GrantNotFound):
        await support.revoke_grant(
            db_session,
            actor_user_id=outsider.id,
            grant_id=grant.id,
            reason="Curious.",
        )


async def test_only_the_asker_may_withdraw_the_ask(db_session):
    owner, subject = await _patient(db_session, "sup-withdraw")
    admin = await _admin(db_session, "sup-withdraw-admin")
    other = await _admin(db_session, "sup-withdraw-other")
    request = await _ask(db_session, admin=admin, subject=subject)
    await db_session.commit()
    request_id, admin_id, other_id = request.id, admin.id, other.id

    with pytest.raises(support.RequestNotFound):
        await support.withdraw_request(
            db_session, admin_user_id=other_id, request_id=request_id
        )
    await db_session.rollback()

    withdrawn = await support.withdraw_request(
        db_session, admin_user_id=admin_id, request_id=request_id
    )
    assert withdrawn.status == SupportAccessRequestStatus.WITHDRAWN.value
    del owner


async def test_an_answered_request_cannot_be_answered_again(db_session):
    owner, subject = await _patient(db_session, "sup-twice")
    admin = await _admin(db_session, "sup-twice-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    await support.decline_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()

    with pytest.raises(support.RequestNotPending):
        await support.approve_request(
            db_session, owner_user_id=owner.id, request_id=request.id
        )


# ── Nothing is deleted ───────────────────────────────────────────────────────


async def test_the_access_history_keeps_the_noes_as_well_as_the_yeses(db_session):
    """The question a patient asks this list is "has anybody been looking at me".

    A history of only the times they agreed cannot answer it. Declined and
    withdrawn asks stay, and stay attributed.
    """

    owner, subject = await _patient(db_session, "sup-history")
    admin = await _admin(db_session, "sup-history-admin")

    declined = await _ask(db_session, admin=admin, subject=subject)
    await support.decline_request(
        db_session, owner_user_id=owner.id, request_id=declined.id
    )
    withdrawn = await _ask(db_session, admin=admin, subject=subject)
    await support.withdraw_request(
        db_session, admin_user_id=admin.id, request_id=withdrawn.id
    )
    approved = await _ask(db_session, admin=admin, subject=subject)
    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=approved.id
    )
    await db_session.commit()

    history = await support.list_for_subject(db_session, subject_id=subject.id)
    assert {row.status for row in history} == {
        SupportAccessRequestStatus.DECLINED.value,
        SupportAccessRequestStatus.WITHDRAWN.value,
        SupportAccessRequestStatus.APPROVED.value,
    }
    assert all(row.decided_by_user_id is not None for row in history)
    assert all(row.reason for row in history)


async def test_every_state_change_leaves_one_audit_event_without_the_reason_text(
    db_session,
):
    """The envelope is operational and gets shipped to log sinks.

    The admin's sentence about why they want to look at somebody's record is
    shown to that patient, who agreed to read it, and stored on the request. It
    has no business in a stream that operations staff read.
    """

    owner, subject = await _patient(db_session, "sup-audit")
    admin = await _admin(db_session, "sup-audit-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    grant = await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await support.revoke_grant(
        db_session,
        actor_user_id=owner.id,
        grant_id=grant.id,
        reason="Changed my mind.",
    )
    await db_session.commit()

    events = (
        await db_session.execute(
            AuditEvent.__table__.select().where(
                AuditEvent.__table__.c.subject_id == subject.id
            )
        )
    ).mappings().all()
    types = {row["event_type"] for row in events}
    assert types == {
        support.EVENT_REQUESTED,
        support.EVENT_APPROVED,
        support.EVENT_REVOKED,
    }
    for row in events:
        serialized = repr(row["metadata_json"])
        assert "failed lab import" not in serialized
        assert "Changed my mind" not in serialized
        assert row["metadata_json"]["source_surface"] == support.AUDIT_SURFACE


async def test_expiring_stale_rows_changes_the_screens_and_not_the_answer(db_session):
    """``expire_stale`` is bookkeeping, and must not be load-bearing.

    A grant that ran out three days ago still reading "active" in a patient's
    access history is the list telling them something untrue. But authorization
    already refuses it, so the job closing rows changes what is displayed and
    nothing about who may read what.
    """

    owner, subject = await _patient(db_session, "sup-stale")
    admin = await _admin(db_session, "sup-stale-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    grant = await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    lapsed = grant.approved_at - timedelta(hours=6)
    grant.approved_at = lapsed
    grant.expires_at = lapsed + timedelta(hours=1)
    await db_session.commit()

    requests_closed, grants_closed = await support.expire_stale(db_session)
    await db_session.commit()
    assert (requests_closed, grants_closed) == (0, 1)
    await db_session.refresh(grant)
    assert grant.status == SupportAccessStatus.EXPIRED.value
    # Expired is not revoked: nobody took it away, it ran out, and the schema's
    # revocation-state constraint requires those columns to stay empty.
    assert grant.revoked_at is None
    assert grant.revoked_by_user_id is None


async def test_a_lapsed_ask_is_closed_and_attributed(db_session):
    owner, subject = await _patient(db_session, "sup-lapse")
    admin = await _admin(db_session, "sup-lapse-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    request.created_at = request.created_at - timedelta(days=30)
    request.expires_at = request.created_at + support.REQUEST_WINDOW
    await db_session.commit()

    requests_closed, _grants = await support.expire_stale(db_session)
    await db_session.commit()
    assert requests_closed == 1
    await db_session.refresh(request)
    assert request.status == SupportAccessRequestStatus.EXPIRED.value
    assert request.decided_by_user_id == admin.id
    del owner


async def test_a_lapsed_ask_can_no_longer_be_approved(db_session):
    """An unanswered request is not a pending obligation forever."""

    owner, subject = await _patient(db_session, "sup-lapse-approve")
    admin = await _admin(db_session, "sup-lapse-approve-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    request.created_at = request.created_at - timedelta(days=30)
    request.expires_at = request.created_at + support.REQUEST_WINDOW
    await db_session.commit()

    with pytest.raises(support.RequestNotPending):
        await support.approve_request(
            db_session, owner_user_id=owner.id, request_id=request.id
        )


async def test_a_grant_for_one_patient_says_nothing_about_another(db_session):
    """Two patients, one admin, one approval. The other record stays shut."""

    owner_a, subject_a = await _patient(db_session, "sup-cross-a")
    owner_b, subject_b = await _patient(db_session, "sup-cross-b")
    admin = await _admin(db_session, "sup-cross-admin")

    request = await _ask(db_session, admin=admin, subject=subject_a)
    await support.approve_request(
        db_session, owner_user_id=owner_a.id, request_id=request.id
    )
    await db_session.commit()

    context_b = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject_b.id
    )
    assert context_b.support_grant is None
    assert not is_allowed(context_b, _labs_read(subject_b.id))
    del owner_b
