"""Platform support reaches a record only when the patient said it could.

The policy engine has understood support grants since PR-02 and nothing ever
created one, so every rule below was written and unreachable. These tests are
mostly about the ways it could quietly stop being true now that the path exists.

Four rules, one section each. An admin asks and the patient answers. A grant is
bounded in time and in scope. Either side can end it. Nothing is deleted.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select

from vitals.access import (
    AccessRequest,
    PolicyAction,
    PolicyResourceType,
    is_allowed,
)
from vitals.enums import (
    Domain,
    RuleType,
    Severity,
    Source,
    SupportAccessMode,
    SupportAccessRequestStatus,
    SupportAccessStatus,
    SupportRepairStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import (
    AuditEvent,
    HealthSubject,
    SupportAccessRequestScope,
    User,
    UserRole,
)
from vitals.models.conflict_rule import ConflictRule
from vitals.models.system_alert import SystemAlert
from vitals.models.weight import BodyMeasurement
from vitals.services import conflict_engine, support_access_service as support
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


async def test_repair_request_accepts_only_the_fixed_exact_scope(db_session):
    owner, subject = await _patient(db_session, "sup-repair-scope")
    admin = await _admin(db_session, "sup-repair-scope-admin")

    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=subject.id,
        reason="Patient reported stale derived estimates in ticket 41.",
        scopes=support.repair_scope(),
        mode=SupportAccessMode.REPAIR,
    )
    assert request.mode == SupportAccessMode.REPAIR.value

    with pytest.raises(support.UnsupportedMode):
        await support.open_request(
            db_session,
            admin_user_id=admin.id,
            subject_id=subject.id,
            reason="Attempting a broader repair scope.",
            scopes=support.read_scopes_for((Domain.WEIGHT,)),
            mode=SupportAccessMode.REPAIR,
        )
    del owner


async def _approved_repair_grant(db_session, *, slug: str):
    owner, subject = await _patient(db_session, slug)
    admin = await _admin(db_session, f"{slug}-admin")
    measurement = BodyMeasurement(
        subject_id=subject.id,
        actor_user_id=owner.id,
        date=date(2026, 8, 1),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        neck_cm=38.0,
        waist_cm=90.0,
        body_fat_pct=17.5,
        lbm_kg=66.0,
        note="synthetic retained note",
    )
    db_session.add(measurement)
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=subject.id,
        reason="Patient reported stale derived estimates in ticket 41.",
        scopes=support.repair_scope(),
        mode=SupportAccessMode.REPAIR,
    )
    grant = await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()
    context = await resolve_access_context(
        db_session,
        user_id=admin.id,
        subject_id=subject.id,
        support_grant_id=grant.id,
    )
    return owner, subject, admin, measurement, grant, context


async def test_fixed_repair_requires_review_executes_and_owner_can_revert(db_session):
    owner, _subject, admin, measurement, grant, context = (
        await _approved_repair_grant(db_session, slug="sup-repair-flow")
    )
    original = {
        "neck_cm": measurement.neck_cm,
        "waist_cm": measurement.waist_cm,
        "note": measurement.note,
        "actor_user_id": measurement.actor_user_id,
        "source": measurement.source,
    }

    action = await support.propose_clear_derived_estimates(
        db_session,
        context=context,
        measurement_id=measurement.id,
        idempotency_key=uuid.uuid4(),
    )
    duplicate = await support.propose_clear_derived_estimates(
        db_session,
        context=context,
        measurement_id=measurement.id,
        idempotency_key=action.idempotency_key,
    )
    assert duplicate.id == action.id
    assert action.status == SupportRepairStatus.PROPOSED.value
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (17.5, 66.0)
    await db_session.commit()

    await support.review_repair(
        db_session, owner_user_id=owner.id, action_id=action.id, approve=True
    )
    await db_session.commit()
    executed = await support.execute_repair(
        db_session, context=context, action_id=action.id
    )
    retried = await support.execute_repair(
        db_session, context=context, action_id=action.id
    )
    await db_session.commit()
    await db_session.refresh(measurement)
    assert executed.status == SupportRepairStatus.EXECUTED.value
    assert retried.id == executed.id
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (None, None)
    assert {
        "neck_cm": measurement.neck_cm,
        "waist_cm": measurement.waist_cm,
        "note": measurement.note,
        "actor_user_id": measurement.actor_user_id,
        "source": measurement.source,
    } == original

    events = list(
        await db_session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.event_type.in_(
                    (
                        support.EVENT_REPAIR_PROPOSED,
                        support.EVENT_REPAIR_APPROVED,
                        support.EVENT_REPAIR_EXECUTED,
                    )
                )
            )
            .order_by(AuditEvent.occurred_at)
        )
    )
    assert {event.event_type for event in events} == {
        support.EVENT_REPAIR_PROPOSED,
        support.EVENT_REPAIR_APPROVED,
        support.EVENT_REPAIR_EXECUTED,
    }
    assert len(events) == 3
    for event in events:
        assert event.support_access_grant_id == grant.id
        assert event.metadata_json["changed_fields"] == ["body_fat_pct", "lbm_kg"]
        assert "17.5" not in repr(event.metadata_json)
        assert "66.0" not in repr(event.metadata_json)

    reverted = await support.revert_repair(
        db_session, owner_user_id=owner.id, action_id=action.id
    )
    await db_session.commit()
    await db_session.refresh(measurement)
    assert reverted.status == SupportRepairStatus.REVERTED.value
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (17.5, 66.0)
    with pytest.raises(support.RepairStateError):
        await support.revert_repair(
            db_session, owner_user_id=owner.id, action_id=action.id
        )
    del admin


async def test_fixed_repair_closes_stale_instead_of_overwriting_a_new_edit(db_session):
    owner, _subject, _admin, measurement, _grant, context = (
        await _approved_repair_grant(db_session, slug="sup-repair-stale")
    )
    action = await support.propose_clear_derived_estimates(
        db_session,
        context=context,
        measurement_id=measurement.id,
        idempotency_key=uuid.uuid4(),
    )
    await support.review_repair(
        db_session, owner_user_id=owner.id, action_id=action.id, approve=True
    )
    await db_session.commit()

    measurement.body_fat_pct = 18.25
    await db_session.commit()
    stale = await support.execute_repair(
        db_session, context=context, action_id=action.id
    )
    await db_session.commit()
    await db_session.refresh(measurement)
    assert stale.status == SupportRepairStatus.STALE.value
    assert stale.executed_at is None
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (18.25, 66.0)


async def test_terminal_repair_history_survives_personal_fact_replacement(db_session):
    owner, subject, _admin, measurement, _grant, context = (
        await _approved_repair_grant(
            db_session, slug="sup-repair-detached-history"
        )
    )
    action = await support.propose_clear_derived_estimates(
        db_session,
        context=context,
        measurement_id=measurement.id,
        idempotency_key=uuid.uuid4(),
    )
    await support.review_repair(
        db_session,
        owner_user_id=owner.id,
        action_id=action.id,
        approve=False,
    )
    await db_session.commit()

    # Portability replacement performs this explicit detach only after it has
    # rejected every open proposal. The exact composite FK remains RESTRICT.
    action.target_body_measurement_id = None
    await db_session.flush()
    await db_session.delete(measurement)
    await db_session.commit()
    await db_session.refresh(action)

    assert action.status == SupportRepairStatus.DECLINED.value
    assert action.target_body_measurement_id is None
    assert action.target_date == date(2026, 8, 1)
    owner_context = await resolve_access_context(
        db_session,
        user_id=owner.id,
        subject_id=subject.id,
    )
    history = await support.repair_actions_for_subject(
        db_session,
        context=owner_context,
    )
    assert history[0].measurement_id is None
    assert history[0].measurement_date == date(2026, 8, 1)


async def test_repair_review_and_execution_fail_closed_after_grant_expiry(db_session):
    owner, _subject, _admin, measurement, grant, context = (
        await _approved_repair_grant(db_session, slug="sup-repair-expired-review")
    )
    proposal = await support.propose_clear_derived_estimates(
        db_session,
        context=context,
        measurement_id=measurement.id,
        idempotency_key=uuid.uuid4(),
    )
    grant.approved_at -= timedelta(days=2)
    grant.expires_at = grant.approved_at + timedelta(hours=1)
    await db_session.commit()
    with pytest.raises(support.RepairStateError):
        await support.review_repair(
            db_session, owner_user_id=owner.id, action_id=proposal.id, approve=True
        )
    await db_session.rollback()

    owner, _subject, _admin, measurement, grant, context = (
        await _approved_repair_grant(db_session, slug="sup-repair-expired-execute")
    )
    proposal = await support.propose_clear_derived_estimates(
        db_session,
        context=context,
        measurement_id=measurement.id,
        idempotency_key=uuid.uuid4(),
    )
    await support.review_repair(
        db_session, owner_user_id=owner.id, action_id=proposal.id, approve=True
    )
    grant.approved_at -= timedelta(days=2)
    grant.expires_at = grant.approved_at + timedelta(hours=1)
    await db_session.commit()
    with pytest.raises(support.NotASupportSession):
        await support.execute_repair(
            db_session, context=context, action_id=proposal.id
        )
    await db_session.rollback()
    await db_session.refresh(measurement)
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (17.5, 66.0)


async def test_repair_override_cannot_bypass_review_or_platform_role(db_session):
    owner, _subject, admin, measurement, _grant, context = (
        await _approved_repair_grant(db_session, slug="sup-repair-override-scope")
    )
    proposal = await support.propose_clear_derived_estimates(
        db_session,
        context=context,
        measurement_id=measurement.id,
        idempotency_key=uuid.uuid4(),
    )
    proposal_id = proposal.id
    owner_id = owner.id
    admin_id = admin.id
    await db_session.commit()

    # The conflict override is not approval of the repair itself.
    with pytest.raises(support.RepairStateError):
        await support.execute_repair(
            db_session,
            context=context,
            action_id=proposal_id,
            override=True,
        )
    await db_session.rollback()

    await support.review_repair(
        db_session,
        owner_user_id=owner_id,
        action_id=proposal_id,
        approve=True,
    )
    await db_session.commit()
    await db_session.execute(
        delete(UserRole).where(
            UserRole.user_id == admin_id,
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
        )
    )
    await db_session.commit()

    # Nor can an override preserve support authority after the operator loses
    # the platform role that underpins the exact repair grant.
    with pytest.raises(support.NotAPlatformAdmin):
        await support.execute_repair(
            db_session,
            context=context,
            action_id=proposal_id,
            override=True,
        )
    await db_session.rollback()
    await db_session.refresh(measurement)
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (17.5, 66.0)
    assert await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == support.EVENT_REPAIR_EXECUTED
        )
    ) is None


async def test_export_is_a_separate_exact_one_shot_grant(db_session, monkeypatch):
    from sqlalchemy import select

    owner, subject = await _patient(db_session, "sup-export")
    admin = await _admin(db_session, "sup-export-admin")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=subject.id,
        reason="Patient asked support to recover a portable copy.",
        scopes=support.export_scope(),
        mode=SupportAccessMode.EXPORT,
    )
    grant = await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()
    context = await resolve_access_context(
        db_session,
        user_id=admin.id,
        subject_id=subject.id,
        support_grant_id=grant.id,
    )
    assert context.support_grant is not None
    assert is_allowed(
        context,
        AccessRequest(
            subject_id=subject.id,
            resource_type=PolicyResourceType.OPERATION,
            resource_key=support.EXPORT_OPERATION_KEY,
            action=PolicyAction.EXPORT,
        ),
    )
    assert not is_allowed(context, _labs_read(subject.id))

    async def synthetic_export(_session, *, subject_id):
        assert subject_id == subject.id
        return {"metadata": {"kind": "subject_export"}, "weight_logs": []}

    monkeypatch.setattr(
        support.data_portability_service, "export_subject", synthetic_export
    )
    payload = await support.consume_subject_export(db_session, context=context)
    await db_session.commit()
    await db_session.refresh(grant)
    assert payload["metadata"] == {"kind": "subject_export"}
    assert grant.status == SupportAccessStatus.CONSUMED.value
    assert grant.consumed_at is not None
    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == support.EVENT_RECORD_EXPORTED)
    )
    assert event is not None
    assert event.support_access_grant_id == grant.id
    assert set(event.metadata_json) == {
        "correlation_id",
        "source_surface",
        "reason_code",
        "resource_type",
        "resource_id",
        "grant_mode",
    }
    assert "Patient asked" not in str(event.metadata_json)

    with pytest.raises(support.NotASupportSession):
        await support.consume_subject_export(db_session, context=context)

    grant.approved_at -= timedelta(days=2)
    grant.expires_at = grant.approved_at + timedelta(hours=2)
    await db_session.commit()
    owner_context = await resolve_access_context(
        db_session, user_id=owner.id, subject_id=None
    )
    history = await support.list_for_subject(db_session, context=owner_context)
    exported_request = next(row for row in history.past if row.request_id == request.id)
    assert exported_request.grant_lifecycle == "consumed"
    assert exported_request.grant_ends_at.replace(
        tzinfo=None
    ) == grant.consumed_at.replace(tzinfo=None)


async def test_export_request_rejects_scope_or_ttl_broadening(db_session):
    _owner, subject = await _patient(db_session, "sup-export-shape")
    admin = await _admin(db_session, "sup-export-shape-admin")
    with pytest.raises(support.UnsupportedMode):
        await support.open_request(
            db_session,
            admin_user_id=admin.id,
            subject_id=subject.id,
            reason="A broadened synthetic request.",
            scopes=(*support.export_scope(), *support.read_scopes_for((Domain.LABS,))),
            mode=SupportAccessMode.EXPORT,
        )
    with pytest.raises(support.SupportAccessError):
        await support.open_request(
            db_session,
            admin_user_id=admin.id,
            subject_id=subject.id,
            reason="An extended synthetic request.",
            scopes=support.export_scope(),
            ttl=timedelta(hours=3),
            mode=SupportAccessMode.EXPORT,
        )


async def test_approval_rechecks_a_stored_export_scope_under_lock(db_session):
    from sqlalchemy import select

    owner, subject = await _patient(db_session, "sup-export-forged")
    admin = await _admin(db_session, "sup-export-forged-admin")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=subject.id,
        reason="A request that is altered before approval.",
        scopes=support.export_scope(),
        mode=SupportAccessMode.EXPORT,
    )
    scope = await db_session.scalar(
        select(SupportAccessRequestScope).where(
            SupportAccessRequestScope.request_id == request.id
        )
    )
    assert scope is not None
    scope.resource_key = "data_portability.unreviewed_export"
    await db_session.commit()

    with pytest.raises(support.UnsupportedMode):
        await support.approve_request(
            db_session, owner_user_id=owner.id, request_id=request.id
        )


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


async def test_the_owner_sees_every_live_support_grant_as_frozen_details(db_session):
    owner, subject = await _patient(db_session, "sup-all-live")
    first_admin = await _admin(db_session, "sup-all-live-first")
    second_admin = await _admin(db_session, "sup-all-live-second")

    first_request = await _ask(
        db_session,
        admin=first_admin,
        subject=subject,
        domains=(Domain.LABS,),
        ttl=timedelta(hours=1),
    )
    second_request = await _ask(
        db_session,
        admin=second_admin,
        subject=subject,
        domains=(Domain.NUTRITION,),
        ttl=timedelta(hours=2),
    )
    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=first_request.id
    )
    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=second_request.id
    )
    await db_session.commit()

    owner_context = await resolve_access_context(
        db_session, user_id=owner.id, subject_id=None
    )
    live = await support.live_grants_for(db_session, context=owner_context)

    assert [grant.grantee_username for grant in live] == [
        first_admin.username,
        second_admin.username,
    ]
    assert [grant.scope_keys for grant in live] == [
        (f"domain:{Domain.LABS.value}",),
        (f"domain:{Domain.NUTRITION.value}",),
    ]
    assert live[0].expires_at < live[1].expires_at

    holder_context = await resolve_access_context(
        db_session, user_id=first_admin.id, subject_id=subject.id
    )
    with pytest.raises(support.NotTheSubjectOwner):
        await support.live_grants_for(db_session, context=holder_context)


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

    context = await resolve_access_context(
        db_session, user_id=owner.id, subject_id=subject.id
    )
    history = await support.list_for_subject(db_session, context=context)
    assert {row.effective_status for row in history.past} == {
        SupportAccessRequestStatus.DECLINED.value,
        SupportAccessRequestStatus.WITHDRAWN.value,
        SupportAccessRequestStatus.APPROVED.value,
    }
    assert not history.pending
    assert all(row.reason for row in history.past)


async def test_patient_history_uses_db_time_without_writing_lapsed_state(db_session):
    owner, subject = await _patient(db_session, "sup-effective-history")
    admin = await _admin(db_session, "sup-effective-history-admin")
    lapsed = await _ask(db_session, admin=admin, subject=subject)
    lapsed.created_at -= timedelta(days=30)
    lapsed.expires_at = lapsed.created_at + support.REQUEST_WINDOW

    actionable = await _ask(db_session, admin=admin, subject=subject)
    for number in range(3):
        declined = await _ask(db_session, admin=admin, subject=subject)
        declined.reason = f"Synthetic past request {number}."
        await support.decline_request(
            db_session, owner_user_id=owner.id, request_id=declined.id
        )
    await db_session.commit()

    context = await resolve_access_context(
        db_session, user_id=owner.id, subject_id=subject.id
    )
    history = await support.list_for_subject(db_session, context=context, limit=2)

    assert [row.request_id for row in history.pending] == [actionable.id]
    assert len(history.past) == 1
    assert history.has_more

    full = await support.list_for_subject(db_session, context=context)
    effective_lapsed = next(row for row in full.past if row.request_id == lapsed.id)
    assert effective_lapsed.effective_status == SupportAccessRequestStatus.EXPIRED.value
    assert effective_lapsed.expires_at == lapsed.expires_at
    await db_session.refresh(lapsed)
    assert lapsed.status == SupportAccessRequestStatus.PENDING.value
    assert lapsed.decided_at is None


async def test_patient_history_derives_every_approved_grant_lifecycle(db_session):
    owner, subject = await _patient(db_session, "sup-grant-history")
    admin = await _admin(db_session, "sup-grant-history-admin")

    requests = {}
    grants = {}
    for name in ("live", "expired", "owner", "holder"):
        request = await _ask(db_session, admin=admin, subject=subject)
        request.reason = f"Synthetic {name} lifecycle request."
        grant = await support.approve_request(
            db_session, owner_user_id=owner.id, request_id=request.id
        )
        requests[name] = request
        grants[name] = grant

    past_approval = grants["expired"].approved_at - timedelta(hours=3)
    grants["expired"].approved_at = past_approval
    grants["expired"].expires_at = past_approval + timedelta(hours=1)
    await support.revoke_grant(
        db_session,
        actor_user_id=owner.id,
        grant_id=grants["owner"].id,
        reason="Owner ended synthetic access.",
    )
    await support.revoke_grant(
        db_session,
        actor_user_id=admin.id,
        grant_id=grants["holder"].id,
        reason="Holder handed synthetic access back.",
    )
    await db_session.commit()

    context = await resolve_access_context(
        db_session, user_id=owner.id, subject_id=subject.id
    )
    history = await support.list_for_subject(db_session, context=context)
    by_id = {row.request_id: row for row in history.past}

    assert by_id[requests["live"].id].grant_lifecycle == "live"
    assert by_id[requests["live"].id].grant_ends_at == grants["live"].expires_at
    assert by_id[requests["expired"].id].grant_lifecycle == "expired"
    assert by_id[requests["expired"].id].grant_ends_at == grants["expired"].expires_at
    assert by_id[requests["owner"].id].grant_lifecycle == "revoked_by_owner"
    assert by_id[requests["owner"].id].grant_end_actor_username == owner.username
    assert by_id[requests["holder"].id].grant_lifecycle == "handed_back_by_holder"
    assert by_id[requests["holder"].id].grant_end_actor_username == admin.username

    holder_context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )
    with pytest.raises(support.NotTheSubjectOwner):
        await support.list_for_subject(db_session, context=holder_context)


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
        assert Domain.LABS.value not in serialized
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


# ── The screens, opened rather than asserted about ───────────────────────────
#
# Every service test above passes against a page that answers 500. That is not
# hypothetical here: the care-team conversation shipped with seventeen green
# service tests and a template that raised ``MissingGreenlet`` on the one page
# the feature existed for. These render.


def _sign_in(client, username: str):
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session(username))


async def test_the_patients_access_page_renders_before_anybody_has_asked(
    client, db_session, legacy_owner_roots
):
    """The empty state is the answer, not a placeholder.

    A page that only exists once support has been in is one nobody knows to
    look for, and "has anybody been reading my record" is worth being able to
    ask on a quiet day and get *no* for.
    """

    _sign_in(client, "tester")
    response = await client.get("/settings/access", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "<html" in response.text.lower()
    del db_session, legacy_owner_roots


async def test_the_patients_access_page_renders_a_pending_ask(
    client, db_session, legacy_owner_roots
):
    """Rendered, not just written.

    The template reaches ``row.requested_by.username`` and walks ``row.scopes``;
    a relationship lazy-loading outside the async driver's greenlet raises
    rather than loads, which is exactly how the conversation page answered 500.
    """

    admin = await _admin(db_session, "web-support-admin")
    await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Checking a failed import from ticket 12.",
        scopes=support.read_scopes_for((Domain.LABS, Domain.NUTRITION)),
    )
    await db_session.commit()

    _sign_in(client, "tester")
    response = await client.get("/settings/access", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "ticket 12" in response.text
    assert "web-support-admin" in response.text


async def test_the_patient_can_answer_from_the_page_and_the_banner_appears(
    client, db_session, legacy_owner_roots
):
    """Approval through the form, and then the banner on an unrelated page.

    The banner is the part that is easy to ship broken: it is a global
    dependency reading a different service on every document request, and if it
    raises, every page in the app goes with it.
    """

    admin = await _admin(db_session, "web-support-banner-admin")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Investigating a sync failure.",
        scopes=support.read_scopes_for((Domain.LABS,)),
    )
    await db_session.commit()
    request_id = request.id

    _sign_in(client, "tester")
    approved = await client.post(
        f"/settings/access/{request_id}/approve", follow_redirects=False
    )
    assert approved.status_code == 303

    # An ordinary page, nothing to do with support.
    page = await client.get("/weight", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "/settings/access" in page.text


async def test_every_live_grant_is_visible_and_one_revoke_leaves_the_other(
    client, db_session, legacy_owner_roots
):
    first_admin = await _admin(db_session, "web-live-first-admin")
    second_admin = await _admin(db_session, "web-live-second-admin")
    first_request = await support.open_request(
        db_session,
        admin_user_id=first_admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Investigating the laboratory import.",
        scopes=support.read_scopes_for((Domain.LABS,)),
        ttl=timedelta(hours=1),
    )
    second_request = await support.open_request(
        db_session,
        admin_user_id=second_admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Investigating the nutrition import.",
        scopes=support.read_scopes_for((Domain.NUTRITION,)),
        ttl=timedelta(hours=2),
    )
    first_grant = await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=first_request.id,
    )
    second_grant = await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=second_request.id,
    )
    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    await db_session.commit()

    expected_expiries = {
        grant.expires_at.astimezone(ZoneInfo(subject.timezone)).strftime(
            "%d-%m-%Y %H:%M"
        )
        for grant in (first_grant, second_grant)
    }

    _sign_in(client, "tester")
    management = await client.get(
        "/settings/access", headers={"Accept": "text/html"}
    )
    assert management.status_code == 200
    assert management.text.count("data-support-live-grant") == 2
    assert "data-support-banner" not in management.text
    assert first_admin.username in management.text
    assert second_admin.username in management.text
    assert management.text.count("<time datetime=") >= 2
    assert all(value in management.text for value in expected_expiries)
    assert f'datetime="{first_grant.expires_at.isoformat()}"' in management.text
    assert f'datetime="{second_grant.expires_at.isoformat()}"' in management.text

    ordinary = await client.get("/weight", headers={"Accept": "text/html"})
    assert ordinary.status_code == 200
    assert ordinary.text.count("data-support-banner") == 1
    assert "Активных доступов поддержки к этой записи: 2" in ordinary.text
    assert "Проверить доступ" in ordinary.text
    assert "Прекратить этот доступ" not in ordinary.text

    stopped = await client.post(
        f"/settings/access/grant/{first_grant.id}/revoke",
        follow_redirects=True,
    )
    assert stopped.status_code == 200
    assert stopped.text.count("data-support-live-grant") == 1
    assert second_admin.username in stopped.text
    assert first_admin.username in stopped.text  # Kept in the request history.
    assert "Этот доступ прекращён." in stopped.text
    assert "Доступ закрыт" not in stopped.text


async def test_patient_page_orders_pending_before_openings_and_shows_true_endings(
    client, db_session, legacy_owner_roots
):
    admin = await _admin(db_session, "web-history-truth-admin")
    handed_back_request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Investigating a completed support read.",
        scopes=support.read_scopes_for((Domain.LABS,)),
    )
    handed_back_grant = await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=handed_back_request.id,
    )
    await db_session.commit()

    admin_context = await resolve_access_context(
        db_session,
        user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
    )
    await support.record_record_opened(
        db_session, context=admin_context, domain_keys=(Domain.LABS.value,)
    )
    await db_session.commit()
    await support.revoke_grant(
        db_session,
        actor_user_id=admin.id,
        grant_id=handed_back_grant.id,
        reason="The synthetic support read is complete.",
    )

    pending = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="This question still needs an answer.",
        scopes=support.read_scopes_for((Domain.NUTRITION,)),
    )
    lapsed = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="This old question is no longer answerable.",
        scopes=support.read_scopes_for((Domain.WEIGHT,)),
    )
    lapsed.created_at -= timedelta(days=30)
    lapsed.expires_at = lapsed.created_at + support.REQUEST_WINDOW
    await db_session.commit()

    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    expected_pending_deadline = pending.expires_at.astimezone(
        ZoneInfo(subject.timezone)
    ).strftime("%d-%m-%Y %H:%M")

    _sign_in(client, "tester")
    page = await client.get("/settings/access", headers={"Accept": "text/html"})
    assert page.status_code == 200

    pending_position = page.text.index("data-support-pending-requests")
    openings_position = page.text.index("Фактические открытия поддержкой")
    past_position = page.text.index("Прошлые просьбы")
    assert pending_position < openings_position < past_position
    assert expected_pending_deadline in page.text
    assert f'datetime="{pending.expires_at.isoformat()}"' in page.text
    assert f'data-support-request-id="{lapsed.id}"' in page.text
    assert f"/settings/access/{lapsed.id}/approve" not in page.text
    assert f"/settings/access/{lapsed.id}/decline" not in page.text
    assert 'data-support-grant-lifecycle="handed_back_by_holder"' in page.text
    assert admin.username in page.text
    assert f'datetime="{handed_back_grant.revoked_at.isoformat()}"' in page.text

    await db_session.refresh(lapsed)
    assert lapsed.status == SupportAccessRequestStatus.PENDING.value
    assert lapsed.decided_at is None


async def test_a_stale_login_must_reauthenticate_before_support_changes(
    client, db_session, legacy_owner_roots
):
    from datetime import datetime, timezone

    from web.auth import create_federated_session
    from web.config import SESSION_COOKIE

    admin = await _admin(db_session, "web-support-stale-admin")
    await db_session.commit()
    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username=admin.username,
            user_id=admin.id,
            session_version=admin.session_version,
            authenticated_at=int(datetime.now(timezone.utc).timestamp()) - 3600,
            subject_id=None,
        ),
    )

    response = await client.post(
        "/settings/platform/support/request",
        data={
            "subject_id": str(legacy_owner_roots.subject_id),
            "reason": "This must not be created from a stale session.",
            "hours": "2",
            "domains": "labs",
        },
        headers={
            "Accept": "text/html",
            "Referer": "http://test/settings/platform/support",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?")
    assert response.cookies.get(SESSION_COOKIE) in (None, "")


async def test_a_revoked_grant_closes_the_former_holder_route_immediately(
    client, db_session, legacy_owner_roots
):
    """The next request is a bounded refusal, not a stale page or a hang."""

    admin = await _admin(db_session, "web-support-revoked-admin")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Investigating a failed lab import.",
        scopes=support.read_scopes_for((Domain.LABS,)),
    )
    grant = await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()

    _sign_in(client, admin.username)
    opened = await asyncio.wait_for(
        client.get(
            f"/care/{legacy_owner_roots.subject_id}",
            headers={"Accept": "text/html"},
        ),
        timeout=3,
    )
    assert opened.status_code == 200
    from sqlalchemy import func, select

    reads_before_revoke = int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
        )
        or 0
    )
    assert reads_before_revoke == 1

    await support.revoke_grant(
        db_session,
        actor_user_id=legacy_owner_roots.user_id,
        grant_id=grant.id,
        reason="Withdrawn by the record owner.",
    )
    await db_session.commit()

    refused = await asyncio.wait_for(
        client.get(
            f"/care/{legacy_owner_roots.subject_id}",
            headers={"Accept": "text/html"},
        ),
        timeout=3,
    )
    assert refused.status_code == 404
    assert "<html" in refused.text.lower()
    reads_after_refusal = int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
        )
        or 0
    )
    assert reads_after_refusal == reads_before_revoke


async def test_the_banner_is_absent_when_nothing_is_open(
    client, db_session, legacy_owner_roots
):
    """A warning stripe that is always there is one nobody reads."""

    _sign_in(client, "tester")
    page = await client.get("/weight", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "/settings/access/grant/" not in page.text
    del db_session, legacy_owner_roots


async def test_the_console_refuses_an_account_that_is_not_an_administrator(
    client, db_session, legacy_owner_roots
):
    """403, and by asking the database rather than trusting the session.

    Signed in as an ordinary member, not as the installation owner: bootstrap
    gives that account ``platform_superadmin`` along with ``member``, so it is
    the wrong account to prove a refusal with — it would pass.
    """

    await _user(db_session, "web-console-nobody")
    await db_session.commit()

    _sign_in(client, "web-console-nobody")
    response = await client.get(
        "/settings/platform/support", headers={"Accept": "text/html"}
    )
    assert response.status_code == 403
    del legacy_owner_roots


async def test_the_console_renders_for_an_administrator(
    client, db_session, legacy_owner_roots
):
    """Including the list of records an ask may name.

    Which is a list rather than a search box on purpose: choosing whose record
    to investigate has to be a choice somebody can audit later, not a patient
    found by typing part of their name.
    """

    admin = await _admin(db_session, "web-console-admin")
    await db_session.commit()

    _sign_in(client, "web-console-admin")
    response = await client.get(
        "/settings/platform/support", headers={"Accept": "text/html"}
    )
    assert response.status_code == 200
    assert "<html" in response.text.lower()
    del admin, legacy_owner_roots


async def test_the_console_offers_only_sections_a_record_actually_has(
    client, db_session, legacy_owner_roots
):
    """``Domain`` has a member for infrastructure alerts, and a record has no
    such section. Offering it beside Labs and Nutrition asks a patient to
    approve reading something that does not exist — and the patient reads this
    list, so every line of it has to mean something to them."""

    admin = await _admin(db_session, "sections-console-admin")
    await db_session.commit()

    _sign_in(client, "sections-console-admin")
    page = (
        await client.get(
            "/settings/platform/support", headers={"Accept": "text/html"}
        )
    ).text
    assert 'value="labs"' in page
    assert 'value="system"' not in page, "the console offered a section nobody has"
    del admin, legacy_owner_roots


async def test_an_ask_naming_a_section_nobody_has_is_refused(
    client, db_session, legacy_owner_roots
):
    """The form omits it; this is the rule behind the form."""

    admin = await _admin(db_session, "sections-post-admin")
    _owner, subject = await _patient(db_session, "sections-patient")
    await db_session.commit()

    _sign_in(client, "sections-post-admin")
    response = await client.post(
        "/settings/platform/support/request",
        data={
            "subject_id": str(subject.id),
            "reason": "Looking into a ticket about missing labs.",
            "hours": "2",
            "domains": ["system"],
            "ticket_reference": "SUP-1",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert "error=domain" in response.headers["location"]

    state = await support.console_for_admin(db_session, admin_user_id=admin.id)
    assert not state.requests, "an ask for a section nobody has was recorded"
    del legacy_owner_roots


async def test_a_granted_record_opens_and_shows_only_what_was_granted(
    client, db_session, legacy_owner_roots
):
    """The console's "open the record" link, followed.

    Before this the button led to a 404 — ``/care`` is the professional's
    surface and a support grant was not a basis for it — so a read grant
    authorized reads nobody could perform. The same screens serve both readers
    deliberately: what may be shown is decided by the policy from the grant's
    exact scopes, and building a second, narrower record view would mean two
    places for that to drift apart.
    """

    admin = await _admin(db_session, "care-support-admin")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Checking a sync failure.",
        scopes=support.read_scopes_for((Domain.WEIGHT,)),
    )
    await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()

    _sign_in(client, "care-support-admin")
    page = await client.get(
        f"/care/{legacy_owner_roots.subject_id}", headers={"Accept": "text/html"}
    )
    assert page.status_code == 200
    # Named as support, not as a professional: a banner reading "(Doctor)" over
    # a support session tells the patient something untrue about who is here.
    assert "care.kind" not in page.text
    # Support learns only that this opening is restricted. Naming an enabled
    # but ungranted section would itself disclose something about the patient.
    assert (
        "Sections approved for this opening" in page.text
        or "Разделы, разрешённые для этого открытия" in page.text
    )
    assert '<div class="mh-eyebrow">Labs</div>' not in page.text
    assert '<div class="mh-eyebrow">Анализы</div>' not in page.text
    assert '<div class="mh-eyebrow">Nutrition</div>' not in page.text
    assert '<div class="mh-eyebrow">Питание</div>' not in page.text


async def test_same_admin_grants_stay_exact_from_console_link_through_audit(
    client, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from vitals.services.access_resolution import SupportGrantSelectionError

    admin = await _admin(db_session, "care-support-multi-admin")
    grants = {}
    for domain in (Domain.LABS, Domain.NUTRITION):
        request = await support.open_request(
            db_session,
            admin_user_id=admin.id,
            subject_id=legacy_owner_roots.subject_id,
            reason=f"Checking the synthetic {domain.value} import.",
            scopes=support.read_scopes_for((domain,)),
        )
        grants[domain] = await support.approve_request(
            db_session,
            owner_user_id=legacy_owner_roots.user_id,
            request_id=request.id,
        )
    await db_session.commit()
    grant_ids = {domain: grant.id for domain, grant in grants.items()}

    with pytest.raises(SupportGrantSelectionError):
        await resolve_access_context(
            db_session,
            user_id=admin.id,
            subject_id=legacy_owner_roots.subject_id,
        )
    for domain, grant in grants.items():
        selected = await resolve_access_context(
            db_session,
            user_id=admin.id,
            subject_id=legacy_owner_roots.subject_id,
            support_grant_id=grant_ids[domain],
        )
        assert selected.support_grant is not None
        assert selected.support_grant.grant_id == grant_ids[domain]
        assert {scope.resource_key for scope in selected.support_grant.scopes} == {
            domain.value
        }

    _sign_in(client, admin.username)
    console = await client.get(
        "/settings/platform/support", headers={"Accept": "text/html"}
    )
    assert console.status_code == 200
    for grant_id in grant_ids.values():
        assert (
            f'/care/{legacy_owner_roots.subject_id}?support_grant_id={grant_id}'
            in console.text
        )

    async def exact_synthetic_projection(_db, care):
        from datetime import date

        from vitals.services.care.record_projection import RecordProjection

        allowed = {
            scope.resource_key
            for scope in care.access.support_grant.scopes
            if scope.resource_type is PolicyResourceType.DOMAIN
        }
        visible_key = next(iter(allowed))
        return RecordProjection(
            record={
                Domain.LABS.value: {"out_of_range": []},
                Domain.NUTRITION.value: {
                    "avg_calories_per_day": None,
                    "avg_protein_per_day_g": None,
                    "days_with_logs": 0,
                },
            },
            coverage={
                Domain.LABS.value: {
                    "status": (
                        "available" if Domain.LABS.value in allowed else "disabled"
                    ),
                    "module": Domain.LABS.value,
                },
                Domain.NUTRITION.value: {
                    "status": (
                        "available"
                        if Domain.NUTRITION.value in allowed
                        else "disabled"
                    ),
                    "module": Domain.NUTRITION.value,
                },
            },
            period={"period_start": date(2026, 1, 1), "period_end": date(2026, 1, 7)},
            withheld_domains=(),
            loaded_domains=(visible_key,),
            restricted=True,
        )

    monkeypatch.setattr("web.routers.care._visible_record", exact_synthetic_projection)
    rendered = {}
    for domain, grant_id in grant_ids.items():
        page = await client.get(
            f"/care/{legacy_owner_roots.subject_id}?support_grant_id={grant_id}",
            headers={"Accept": "text/html"},
        )
        assert page.status_code == 200
        rendered[domain] = page.text

    labs_cards = (
        '<div class="mh-eyebrow">Labs</div>',
        '<div class="mh-eyebrow">Анализы</div>',
    )
    nutrition_cards = (
        '<div class="mh-eyebrow">Nutrition</div>',
        '<div class="mh-eyebrow">Питание</div>',
    )
    assert any(card in rendered[Domain.LABS] for card in labs_cards)
    assert not any(card in rendered[Domain.LABS] for card in nutrition_cards)
    assert any(card in rendered[Domain.NUTRITION] for card in nutrition_cards)
    assert not any(card in rendered[Domain.NUTRITION] for card in labs_cards)

    events = list(
        (
            await db_session.execute(
                select(AuditEvent)
                .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        ).scalars()
    )
    assert {event.support_access_grant_id for event in events} == {
        grant_ids[Domain.LABS],
        grant_ids[Domain.NUTRITION],
    }
    assert len(events) == 2
    audit_count = len(events)

    other_admin = await _admin(db_session, "care-support-foreign-admin")
    foreign_request = await support.open_request(
        db_session,
        admin_user_id=other_admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="A synthetic grant belonging to another administrator.",
        scopes=support.read_scopes_for((Domain.WEIGHT,)),
    )
    foreign_grant = await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=foreign_request.id,
    )
    await db_session.commit()
    foreign_grant_id = foreign_grant.id

    async def phi_read_would_be_a_bug(*_args, **_kwargs):
        raise AssertionError("invalid support selector reached the PHI projection")

    monkeypatch.setattr("web.routers.care._visible_record", phi_read_would_be_a_bug)
    refused_urls = (
        f"/care/{legacy_owner_roots.subject_id}",
        f"/care/{legacy_owner_roots.subject_id}?support_grant_id={uuid.uuid4()}",
        f"/care/{legacy_owner_roots.subject_id}?support_grant_id=not-a-uuid",
        f"/care/{legacy_owner_roots.subject_id}?support_grant_id={grant_ids[Domain.LABS]}"
        f"&support_grant_id={grant_ids[Domain.NUTRITION]}",
        f"/care/{legacy_owner_roots.subject_id}?support_grant_id={foreign_grant_id}",
    )
    for url in refused_urls:
        refused = await client.get(url, headers={"Accept": "text/html"})
        assert refused.status_code == 404

    await support.revoke_grant(
        db_session,
        actor_user_id=legacy_owner_roots.user_id,
        grant_id=grant_ids[Domain.LABS],
        reason="Synthetic owner revocation.",
    )
    await db_session.commit()
    revoked = await client.get(
        f"/care/{legacy_owner_roots.subject_id}"
        f"?support_grant_id={grant_ids[Domain.LABS]}",
        headers={"Accept": "text/html"},
    )
    assert revoked.status_code == 404
    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
            )
            or 0
        )
        == audit_count
    )


async def test_each_support_record_response_commits_one_phi_free_read_event(
    client, db_session, legacy_owner_roots
):
    """The audit is a prerequisite for the response, not best-effort logging."""

    from sqlalchemy import select

    admin = await _admin(db_session, "care-support-audited-read")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Checking a sync failure with private clinical details.",
        scopes=support.read_scopes_for((Domain.WEIGHT,)),
    )
    grant = await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()

    _sign_in(client, admin.username)
    for expected_count in (1, 2):
        page = await client.get(
            f"/care/{legacy_owner_roots.subject_id}",
            headers={"Accept": "text/html"},
        )
        assert page.status_code == 200

        events = list(
            (
                await db_session.execute(
                    select(AuditEvent).where(
                        AuditEvent.subject_id == legacy_owner_roots.subject_id,
                        AuditEvent.event_type == support.EVENT_RECORD_OPENED,
                    )
                )
            ).scalars()
        )
        assert len(events) == expected_count

    event = events[-1]
    from vitals.persistence.rls import bound_subject

    # The test client reuses one AsyncSession so the test can inspect writes,
    # but its request dependency clears the transaction-local subject exactly
    # as production does when a request-scoped session closes.
    assert bound_subject(db_session) is None
    assert event.actor_user_id == admin.id
    assert event.support_access_grant_id == grant.id
    assert event.resource_type == "health_record"
    assert event.metadata_json == {
        "correlation_id": event.metadata_json["correlation_id"],
        "source_surface": "web.care.record",
        "reason_code": "approved_support_read",
        "resource_type": "health_record",
        "resource_id": str(legacy_owner_roots.subject_id),
        "grant_mode": SupportAccessMode.READ.value,
    }
    serialized = repr(event.metadata_json)
    assert "private clinical details" not in serialized
    assert Domain.WEIGHT.value not in serialized


async def test_support_read_audit_refuses_scopes_outside_the_exact_grant(db_session):
    from sqlalchemy import func, select

    owner, subject = await _patient(db_session, "support-audit-scope")
    admin = await _admin(db_session, "support-audit-scope-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()
    context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )

    with pytest.raises(support.NotASupportSession):
        await support.record_record_opened(
            db_session,
            context=context,
            domain_keys=(Domain.WEIGHT.value,),
        )
    await db_session.rollback()

    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
            )
            or 0
        )
        == 0
    )


async def test_support_read_audit_rechecks_a_role_lost_after_context_resolution(
    db_session,
):
    from sqlalchemy import delete, func, select

    owner, subject = await _patient(db_session, "support-audit-demoted")
    admin = await _admin(db_session, "support-audit-demoted-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    await db_session.commit()
    context = await resolve_access_context(
        db_session, user_id=admin.id, subject_id=subject.id
    )
    assert context.support_grant is not None

    await db_session.execute(
        delete(UserRole).where(
            UserRole.user_id == admin.id,
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
        )
    )
    await db_session.commit()

    with pytest.raises(support.NotAPlatformAdmin):
        await support.record_record_opened(
            db_session,
            context=context,
            domain_keys=(Domain.LABS.value,),
        )
    await db_session.rollback()
    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
            )
            or 0
        )
        == 0
    )


@pytest.mark.integration
async def test_locked_audit_refreshes_a_grant_revoked_in_another_transaction(
    db_session,
):
    """A stale identity-map grant cannot survive a revocation that won first."""

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    owner, subject = await _patient(db_session, "support-audit-race")
    admin = await _admin(db_session, "support-audit-race-admin")
    request = await _ask(db_session, admin=admin, subject=subject)
    grant = await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    owner_id = owner.id
    subject_id = subject.id
    admin_id = admin.id
    grant_id = grant.id
    await db_session.commit()

    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as reader, factory() as revoker:
        stale_context = await resolve_access_context(
            reader, user_id=admin_id, subject_id=subject_id
        )
        assert stale_context.support_grant is not None

        await support.revoke_grant(
            revoker,
            actor_user_id=owner_id,
            grant_id=grant_id,
            reason="Patient ended access before the next disclosure.",
        )
        await revoker.commit()

        with pytest.raises(support.NotASupportSession):
            await support.record_record_opened(
                reader,
                context=stale_context,
                domain_keys=(Domain.LABS.value,),
            )
        await reader.rollback()

    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
            )
            or 0
        )
        == 0
    )


@pytest.mark.integration
async def test_role_revocation_that_commits_first_refuses_the_disclosure(
    db_session,
):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vitals.services import identity_service

    owner, subject = await _patient(db_session, "support-role-race-revoke-first")
    operator = await _admin(db_session, "support-role-race-operator")
    reviewer = await _admin(db_session, "support-role-race-reviewer")
    request = await _ask(db_session, admin=operator, subject=subject)
    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    subject_id = subject.id
    operator_id = operator.id
    reviewer_id = reviewer.id
    await db_session.commit()

    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as reader, factory() as revoker:
        stale_context = await resolve_access_context(
            reader, user_id=operator_id, subject_id=subject_id
        )
        assert stale_context.support_grant is not None

        assert await identity_service.revoke_role(
            revoker,
            user_id=operator_id,
            role=UserRoleName.PLATFORM_SUPERADMIN,
            actor_user_id=reviewer_id,
        )
        await revoker.commit()

        with pytest.raises(support.NotAPlatformAdmin):
            await support.record_record_opened(
                reader,
                context=stale_context,
                domain_keys=(Domain.LABS.value,),
            )
        await reader.rollback()

    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
            )
            or 0
        )
        == 0
    )


@pytest.mark.integration
async def test_disclosure_that_holds_the_governance_lock_precedes_role_revocation(
    db_session,
):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vitals.services import identity_service

    owner, subject = await _patient(db_session, "support-role-race-read-first")
    operator = await _admin(db_session, "support-role-race-read-operator")
    reviewer = await _admin(db_session, "support-role-race-read-reviewer")
    request = await _ask(db_session, admin=operator, subject=subject)
    await support.approve_request(
        db_session, owner_user_id=owner.id, request_id=request.id
    )
    subject_id = subject.id
    operator_id = operator.id
    reviewer_id = reviewer.id
    await db_session.commit()

    factory = async_sessionmaker(
        db_session.bind, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as reader, factory() as revoker:
        context = await resolve_access_context(
            reader, user_id=operator_id, subject_id=subject_id
        )
        await support.record_record_opened(
            reader,
            context=context,
            domain_keys=(Domain.LABS.value,),
        )

        started = asyncio.Event()

        async def _revoke_role() -> bool:
            started.set()
            changed = await identity_service.revoke_role(
                revoker,
                user_id=operator_id,
                role=UserRoleName.PLATFORM_SUPERADMIN,
                actor_user_id=reviewer_id,
            )
            await revoker.commit()
            return changed

        revocation = asyncio.create_task(_revoke_role())
        await started.wait()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(revocation), timeout=0.1)

        await reader.commit()
        assert await asyncio.wait_for(revocation, timeout=3)

    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
            )
            or 0
        )
        == 1
    )


async def test_template_failure_happens_before_a_support_read_is_audited(
    client, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    from web.routers import care as care_router

    admin = await _admin(db_session, "care-support-template-failure")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Checking a sync failure.",
        scopes=support.read_scopes_for((Domain.WEIGHT,)),
    )
    await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()
    _sign_in(client, admin.username)

    def _fail_render(*_args, **_kwargs):
        raise RuntimeError("synthetic template render failure")

    monkeypatch.setattr(care_router.templates, "TemplateResponse", _fail_render)
    with pytest.raises(RuntimeError, match="template render failure"):
        await client.get(
            f"/care/{legacy_owner_roots.subject_id}",
            headers={"Accept": "text/html"},
        )
    await db_session.rollback()

    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
            )
            or 0
        )
        == 0
    )


async def test_audit_commit_failure_does_not_return_the_medical_page(
    client, db_session, legacy_owner_roots, monkeypatch
):
    from sqlalchemy import func, select

    admin = await _admin(db_session, "care-support-audit-commit")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Checking a sync failure.",
        scopes=support.read_scopes_for((Domain.WEIGHT,)),
    )
    await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()
    _sign_in(client, admin.username)

    original_commit = db_session.commit

    async def _fail_commit():
        raise RuntimeError("synthetic audit commit failure")

    monkeypatch.setattr(db_session, "commit", _fail_commit)
    with pytest.raises(RuntimeError, match="audit commit failure"):
        await client.get(
            f"/care/{legacy_owner_roots.subject_id}",
            headers={"Accept": "text/html"},
        )
    monkeypatch.setattr(db_session, "commit", original_commit)
    await db_session.rollback()

    assert (
        int(
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
            )
            or 0
        )
        == 0
    )


async def test_owner_reads_do_not_create_support_use_events(
    client, db_session, legacy_owner_roots
):
    """The event means support used a grant, not merely that a page rendered."""

    from sqlalchemy import func, select

    _sign_in(client, "tester")
    page = await client.get(
        f"/care/{legacy_owner_roots.subject_id}",
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    count = int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == support.EVENT_RECORD_OPENED)
        )
        or 0
    )
    assert count == 0


async def test_patient_access_centre_shows_actual_support_openings(
    client, db_session, legacy_owner_roots, redis
):
    from vitals.services import modules_service

    await modules_service.set_module_enabled(
        db_session,
        key="hrt",
        enabled=False,
        subject_id=legacy_owner_roots.subject_id,
    )
    await db_session.commit()
    await redis.delete(modules_service.cache_key(legacy_owner_roots.subject_id))

    admin = await _admin(db_session, "care-support-visible-read")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Checking a sync failure.",
        scopes=support.read_scopes_for((Domain.HRT,)),
    )
    await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()

    _sign_in(client, admin.username)
    opened = await client.get(
        f"/care/{legacy_owner_roots.subject_id}",
        headers={"Accept": "text/html"},
    )
    assert opened.status_code == 200

    _sign_in(client, "tester")
    history = await client.get(
        "/settings/access", headers={"Accept": "text/html"}
    )
    assert history.status_code == 200
    assert admin.username in history.text
    assert "support.opened_title" not in history.text
    assert "support.opened_scopes" not in history.text
    assert "domain:hrt" not in history.text
    assert "Разделы, разрешённые для этого открытия" in history.text


async def test_support_read_history_is_subject_isolated(db_session):
    first_owner, first_subject = await _patient(db_session, "read-history-first")
    second_owner, second_subject = await _patient(db_session, "read-history-second")
    admin = await _admin(db_session, "read-history-admin")

    for owner, subject, domain in (
        (first_owner, first_subject, Domain.LABS),
        (second_owner, second_subject, Domain.WEIGHT),
    ):
        request = await _ask(
            db_session, admin=admin, subject=subject, domains=(domain,)
        )
        await support.approve_request(
            db_session, owner_user_id=owner.id, request_id=request.id
        )
        context = await resolve_access_context(
            db_session, user_id=admin.id, subject_id=subject.id
        )
        await support.record_record_opened(
            db_session, context=context, domain_keys=(domain.value,)
        )
        await db_session.commit()

    first = await support.record_opened_history(
        db_session, subject_id=first_subject.id
    )
    assert len(first.events) == 1
    assert first.events[0].scope_keys == (f"domain:{Domain.LABS.value}",)


async def test_a_read_grant_cannot_write_a_note_through_the_record_screen(
    client, db_session, legacy_owner_roots
):
    """The affordance is hidden, and the route refuses anyway.

    Hiding a button is a courtesy; the service asking the policy is the
    guarantee. Both are checked, because a screen that only hides is one a
    crafted POST walks straight past.
    """

    admin = await _admin(db_session, "care-support-write")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Checking a sync failure.",
        scopes=support.read_scopes_for((Domain.WEIGHT,)),
    )
    await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()

    _sign_in(client, "care-support-write")
    page = await client.get(
        f"/care/{legacy_owner_roots.subject_id}", headers={"Accept": "text/html"}
    )
    assert page.status_code == 200
    assert f'action="/care/{legacy_owner_roots.subject_id}/note"' not in page.text
    assert f'action="/care/{legacy_owner_roots.subject_id}/plan"' not in page.text
    assert f'/care/{legacy_owner_roots.subject_id}/messages' not in page.text

    posted = await client.post(
        f"/care/{legacy_owner_roots.subject_id}/note",
        data={"body": "A note support had no business writing."},
        follow_redirects=False,
    )
    assert posted.status_code >= 400


async def test_the_clinical_conversation_is_not_opened_by_a_support_grant(
    client, db_session, legacy_owner_roots
):
    """Being in the room is a row, and a grant does not add one.

    A care-team thread is joined by somebody adding you, and support was not
    added. The list is therefore empty for them rather than filtered — which is
    the same answer, reached without the grant ever being asked about it.
    """

    admin = await _admin(db_session, "care-support-threads")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Checking a sync failure.",
        scopes=support.read_scopes_for((Domain.WEIGHT,)),
    )
    await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()

    _sign_in(client, "care-support-threads")
    page = await client.get(
        f"/care/{legacy_owner_roots.subject_id}/messages",
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    assert "care-support-threads" not in page.text


async def test_the_console_has_a_link_somebody_can_actually_click(
    client, db_session, legacy_owner_roots
):
    """And it is in the rail, because /settings refuses the account it is for.

    A platform superadmin usually keeps no record of their own, so ``/settings``
    and ``/more`` both answer 409 — and the console's only link lived on
    ``/settings``. The one account the console exists for could reach it by
    typing the URL and no other way, which is the same defect as shipping no
    console at all.
    """

    await _admin(db_session, "rail-admin")
    await db_session.commit()

    _sign_in(client, "rail-admin")
    # ``/care`` rather than ``/weight``: an account with no record of its own is
    # refused by every personal page, and this one is what they can open.
    page = await client.get("/care", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "/settings/platform/support" in page.text
    assert "/settings/platform/registration" in page.text
    del legacy_owner_roots


async def test_the_console_link_is_not_offered_to_anybody_else(
    client, db_session, legacy_owner_roots
):
    """A link that answers 403 is worse than no link."""

    await _user(db_session, "rail-member")
    await db_session.commit()

    _sign_in(client, "rail-member")
    page = await client.get("/weight", headers={"Accept": "text/html"})
    assert "/settings/platform/support" not in page.text
    assert "/settings/platform/registration" not in page.text
    del legacy_owner_roots


async def test_a_professional_reaching_the_access_page_is_not_stranded(
    client, db_session, legacy_owner_roots
):
    """``/settings/access`` is about *my* record, and a professional keeps none.

    ``NoPersonalRecordError`` is what the registered handler understands. A
    professional role now keeps onboarding reachable even before the first
    relationship, so this personal-only page returns them to their care home.
    """

    doctor = await _user(db_session, "access-page-doctor", roles=(UserRoleName.DOCTOR,))
    await db_session.commit()

    _sign_in(client, "access-page-doctor")
    response = await client.get(
        "/settings/access", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/care"
    del doctor, legacy_owner_roots


async def test_support_export_http_is_post_only_no_store_and_one_shot(
    client, db_session, legacy_owner_roots, monkeypatch
):
    admin = await _admin(db_session, "web-support-export-admin")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Recover the patient's synthetic portability file.",
        scopes=support.export_scope(),
        mode=SupportAccessMode.EXPORT,
    )
    grant = await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()
    grant_id = grant.id
    url = (
        f"/settings/platform/support/{legacy_owner_roots.subject_id}"
        f"/grant/{grant_id}/export"
    )

    calls = []

    async def synthetic_export(_session, *, subject_id):
        calls.append(subject_id)
        return {"metadata": {"kind": "subject_export"}, "raw_payloads": []}

    monkeypatch.setattr(
        support.data_portability_service, "export_subject", synthetic_export
    )
    _sign_in(client, admin.username)

    get_response = await client.get(url)
    assert get_response.status_code == 405
    assert not calls

    response = await client.post(url)
    assert response.status_code == 200
    assert response.json()["metadata"]["kind"] == "subject_export"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert calls == [legacy_owner_roots.subject_id]

    second = await client.post(url)
    assert second.status_code == 404
    assert calls == [legacy_owner_roots.subject_id]
    await db_session.refresh(grant)
    assert grant.status == SupportAccessStatus.CONSUMED.value


async def test_exact_repair_http_surfaces_complete_the_reviewed_flow(
    client, db_session, legacy_owner_roots
):
    admin = await _admin(db_session, "web-support-repair-admin")
    measurement = BodyMeasurement(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        date=date(2035, 8, 1),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        waist_cm=90.0,
        body_fat_pct=17.5,
        lbm_kg=66.0,
    )
    db_session.add(measurement)
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Patient reported stale derived estimates in ticket 91.",
        scopes=support.repair_scope(),
        mode=SupportAccessMode.REPAIR,
    )
    grant = await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()
    context = await resolve_access_context(
        db_session,
        user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        support_grant_id=grant.id,
    )
    action = await support.propose_clear_derived_estimates(
        db_session,
        context=context,
        measurement_id=measurement.id,
        idempotency_key=uuid.uuid4(),
    )
    execute_rule = ConflictRule(
        subject_id=legacy_owner_roots.subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.WEIGHT.value,
        condition_a={"measurement": True},
        domain_b=Domain.LABS.value,
        condition_b={"present": True},
        severity=Severity.BLOCK.value,
        message="Synthetic support execute conflict",
        active=True,
    )
    db_session.add(execute_rule)
    await db_session.commit()
    action_id = action.id
    admin_id = admin.id
    admin_username = admin.username
    grant_id = grant.id
    execute_rule_id = execute_rule.id

    async def current_weight(_session, *, scope):
        assert scope.subject_id == legacy_owner_roots.subject_id
        return []

    async def current_labs(_session, *, scope):
        assert scope.subject_id == legacy_owner_roots.subject_id
        return [{"present": True}]

    conflict_engine.register_domain_resolver(Domain.WEIGHT.value, current_weight)
    conflict_engine.register_domain_resolver(Domain.LABS.value, current_labs)

    workspace_url = (
        f"/settings/platform/support/{legacy_owner_roots.subject_id}"
        f"/grant/{grant_id}/repair"
    )
    _sign_in(client, admin_username)
    workspace = await client.get(workspace_url, headers={"Accept": "text/html"})
    assert workspace.status_code == 200
    assert f"/repair/{action_id}/execute" not in workspace.text
    assert 'x-data="protocolForm()"' in workspace.text
    assert '{% include "partials/conflict_modal.html" %}' not in workspace.text
    assert 'x-show="showConfirm"' in workspace.text

    _sign_in(client, "tester")
    patient_page = await client.get(
        "/settings/access", headers={"Accept": "text/html"}
    )
    assert patient_page.status_code == 200
    assert f"/repairs/{action_id}/approve" in patient_page.text
    assert 'x-data="protocolForm()"' in patient_page.text
    assert 'x-show="showConfirm"' in patient_page.text
    approved = await client.post(
        f"/settings/access/repairs/{action_id}/approve", follow_redirects=False
    )
    assert approved.status_code == 303

    _sign_in(client, admin_username)
    workspace = await client.get(workspace_url, headers={"Accept": "text/html"})
    assert workspace.status_code == 200
    execute_fragment = f'/repair/{action_id}/execute"'
    assert execute_fragment in workspace.text
    execute_index = workspace.text.index(execute_fragment)
    execute_form = workspace.text[workspace.text.rfind("<form", 0, execute_index) :]
    execute_form = execute_form[: execute_form.index("</form>")]
    assert '@submit.prevent="submitForm($event)"' in execute_form
    assert 'hx-boost="false"' in execute_form

    blocked = await client.post(
        f"{workspace_url}/{action_id}/execute", follow_redirects=False
    )
    assert blocked.status_code == 409
    assert [row["message"] for row in blocked.json()["violations"]] == [
        "Synthetic support execute conflict"
    ]
    await db_session.refresh(measurement)
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (17.5, 66.0)

    executed = await client.post(
        f"{workspace_url}/{action_id}/execute",
        data={"override": "true"},
        follow_redirects=False,
    )
    assert executed.status_code == 303
    await db_session.refresh(measurement)
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (None, None)
    execute_alert = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == f"conflict:{execute_rule_id}"
        )
    )
    assert execute_alert is not None
    assert execute_alert.override_at is not None
    assert execute_alert.overridden_by_user_id == admin_id

    persisted_execute_rule = await db_session.get(ConflictRule, execute_rule_id)
    assert persisted_execute_rule is not None
    persisted_execute_rule.active = False
    revert_rule = ConflictRule(
        subject_id=legacy_owner_roots.subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.WEIGHT.value,
        condition_a={"measurement": True},
        domain_b=Domain.LABS.value,
        condition_b={"present": True},
        severity=Severity.BLOCK.value,
        message="Synthetic owner revert conflict",
        active=True,
    )
    db_session.add(revert_rule)
    await db_session.commit()
    revert_rule_id = revert_rule.id

    _sign_in(client, "tester")
    patient_page = await client.get(
        "/settings/access", headers={"Accept": "text/html"}
    )
    revert_fragment = f'/repairs/{action_id}/revert"'
    assert revert_fragment in patient_page.text
    revert_index = patient_page.text.index(revert_fragment)
    revert_form = patient_page.text[patient_page.text.rfind("<form", 0, revert_index) :]
    revert_form = revert_form[: revert_form.index("</form>")]
    assert '@submit.prevent="submitForm($event)"' in revert_form
    assert 'hx-boost="false"' in revert_form

    blocked = await client.post(
        f"/settings/access/repairs/{action_id}/revert", follow_redirects=False
    )
    assert blocked.status_code == 409
    assert [row["message"] for row in blocked.json()["violations"]] == [
        "Synthetic owner revert conflict"
    ]
    await db_session.refresh(measurement)
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (None, None)

    reverted = await client.post(
        f"/settings/access/repairs/{action_id}/revert",
        data={"override": "true"},
        follow_redirects=False,
    )
    assert reverted.status_code == 303
    await db_session.refresh(measurement)
    assert (measurement.body_fat_pct, measurement.lbm_kg) == (17.5, 66.0)
    revert_alert = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == f"conflict:{revert_rule_id}"
        )
    )
    assert revert_alert is not None
    assert revert_alert.override_at is not None
    assert revert_alert.overridden_by_user_id == legacy_owner_roots.user_id

    repair_events = list(
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(
                    (support.EVENT_REPAIR_EXECUTED, support.EVENT_REPAIR_REVERTED)
                )
            )
        )
    )
    assert {event.event_type for event in repair_events} == {
        support.EVENT_REPAIR_EXECUTED,
        support.EVENT_REPAIR_REVERTED,
    }
    assert all(event.support_access_grant_id == grant_id for event in repair_events)


async def test_support_repair_invalid_selectors_never_reach_the_workspace(
    client, db_session, monkeypatch
):
    admin = await _admin(db_session, "web-support-repair-invalid-admin")
    await db_session.commit()

    async def phi_read_would_be_a_bug(*_args, **_kwargs):
        raise AssertionError("invalid repair selector reached the PHI workspace")

    monkeypatch.setattr(support, "repair_workspace", phi_read_would_be_a_bug)
    _sign_in(client, admin.username)
    response = await client.get(
        "/settings/platform/support/not-a-subject/grant/not-a-grant/repair"
    )
    assert response.status_code == 404


async def test_support_export_invalid_selector_never_reaches_portability(
    client, db_session, monkeypatch
):
    admin = await _admin(db_session, "web-support-export-invalid-admin")
    await db_session.commit()

    async def phi_read_would_be_a_bug(*_args, **_kwargs):
        raise AssertionError("invalid selector reached the portability exporter")

    monkeypatch.setattr(
        support.data_portability_service, "export_subject", phi_read_would_be_a_bug
    )
    _sign_in(client, admin.username)
    response = await client.post(
        "/settings/platform/support/not-a-subject/grant/not-a-grant/export"
    )
    assert response.status_code == 404


async def test_unrepresentable_support_export_does_not_spend_the_grant(
    client, db_session, legacy_owner_roots, monkeypatch
):
    admin = await _admin(db_session, "web-support-export-format-admin")
    request = await support.open_request(
        db_session,
        admin_user_id=admin.id,
        subject_id=legacy_owner_roots.subject_id,
        reason="Recover a synthetic portability file.",
        scopes=support.export_scope(),
        mode=SupportAccessMode.EXPORT,
    )
    grant = await support.approve_request(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        request_id=request.id,
    )
    await db_session.commit()

    async def unrepresentable(*_args, **_kwargs):
        raise support.data_portability_service.PortabilityError(
            "This record cannot be represented by portability v1."
        )

    monkeypatch.setattr(
        support.data_portability_service, "export_subject", unrepresentable
    )
    _sign_in(client, admin.username)
    response = await client.post(
        f"/settings/platform/support/{legacy_owner_roots.subject_id}"
        f"/grant/{grant.id}/export"
    )

    assert response.status_code == 409
    await db_session.refresh(grant)
    assert grant.status == SupportAccessStatus.ACTIVE.value
    assert grant.consumed_at is None
