"""Independent three-account emergency access stays short, exact and visible."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.schema import ForeignKeyConstraint, UniqueConstraint

from vitals.enums import Domain, UserRoleName, UserStatus
from vitals.models.break_glass import BreakGlassApproval
from vitals.models.identity import AuditEvent, HealthSubject, User, UserRole
from vitals.services.emergency import access as emergency
from vitals.services.emergency import projection as record_projection


async def _user(session, name: str, *, admin: bool = False) -> User:
    row = User(
        username=name,
        normalized_username=name,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(row)
    await session.flush()
    if admin:
        session.add(
            UserRole(user_id=row.id, role=UserRoleName.PLATFORM_SUPERADMIN.value)
        )
        await session.flush()
    return row


async def _patient(session, name: str):
    owner = await _user(session, name)
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=f"Synthetic {name}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return owner, subject


async def _three_admins(session, prefix: str):
    return tuple(
        [await _user(session, f"{prefix}-{index}", admin=True) for index in range(3)]
    )


async def _request(session, *, holder, subject, domains=(Domain.LABS,), ttl=15):
    return await emergency.initiate(
        session,
        holder_user_id=holder.id,
        subject_id=subject.id,
        reason="A synthetic outage makes the patient-facing import unavailable.",
        incident_reference="INC-SYNTHETIC",
        domains=domains,
        ttl_minutes=ttl,
    )


async def _activate(session, *, holder, first, second, subject, domains=(Domain.LABS,)):
    row = await _request(
        session, holder=holder, subject=subject, domains=domains
    )
    await emergency.approve(
        session,
        approver_user_id=first.id,
        subject_id=subject.id,
        session_id=row.id,
    )
    await emergency.approve(
        session,
        approver_user_id=second.id,
        subject_id=subject.id,
        session_id=row.id,
    )
    return row


async def test_initiation_is_not_access_and_two_other_admins_activate(db_session):
    _owner, subject = await _patient(db_session, "emergency-patient")
    holder, first, second = await _three_admins(db_session, "emergency-admin")
    row = await _request(db_session, holder=holder, subject=subject)

    with pytest.raises(emergency.EmergencySessionClosed):
        await emergency.authorize_read(
            db_session,
            holder_user_id=holder.id,
            subject_id=subject.id,
            session_id=row.id,
        )

    await emergency.approve(
        db_session,
        approver_user_id=first.id,
        subject_id=subject.id,
        session_id=row.id,
    )
    with pytest.raises(emergency.EmergencySessionClosed):
        await emergency.authorize_read(
            db_session,
            holder_user_id=holder.id,
            subject_id=subject.id,
            session_id=row.id,
        )

    await emergency.approve(
        db_session,
        approver_user_id=second.id,
        subject_id=subject.id,
        session_id=row.id,
    )
    access = await emergency.authorize_read(
        db_session,
        holder_user_id=holder.id,
        subject_id=subject.id,
        session_id=row.id,
    )
    assert access.domain_keys == (Domain.LABS.value,)
    activated = row.activated_at.replace(tzinfo=access.expires_at.tzinfo)
    assert access.expires_at - activated == timedelta(minutes=15)
    approvals = (
        await db_session.scalars(
            select(BreakGlassApproval).where(BreakGlassApproval.session_id == row.id)
        )
    ).all()
    assert {item.approved_by_user_id for item in approvals} == {first.id, second.id}


async def test_holder_duplicate_and_non_admin_approvals_are_refused(db_session):
    stranger, subject = await _patient(db_session, "emergency-review-patient")
    holder, first, _second = await _three_admins(db_session, "emergency-review")
    row = await _request(db_session, holder=holder, subject=subject)

    with pytest.raises(emergency.ApprovalNotAllowed):
        await emergency.approve(
            db_session,
            approver_user_id=holder.id,
            subject_id=subject.id,
            session_id=row.id,
        )
    with pytest.raises(emergency.NotAPlatformAdmin):
        await emergency.approve(
            db_session,
            approver_user_id=stranger.id,
            subject_id=subject.id,
            session_id=row.id,
        )
    await emergency.approve(
        db_session,
        approver_user_id=first.id,
        subject_id=subject.id,
        session_id=row.id,
    )
    with pytest.raises(emergency.ApprovalNotAllowed):
        await emergency.approve(
            db_session,
            approver_user_id=first.id,
            subject_id=subject.id,
            session_id=row.id,
        )


async def test_only_reviewed_read_domains_and_ttls_can_be_requested(db_session):
    _owner, subject = await _patient(db_session, "emergency-shape-patient")
    holder = await _user(db_session, "emergency-shape-holder", admin=True)

    for domains, ttl in (
        ((), 15),
        ((Domain.LABS, Domain.LABS), 15),
        ((Domain.SYSTEM,), 15),
        ((Domain.TIMELINE,), 15),
        ((Domain.LABS,), 61),
    ):
        with pytest.raises(emergency.InvalidEmergencyRequest):
            await _request(
                db_session,
                holder=holder,
                subject=subject,
                domains=domains,
                ttl=ttl,
            )


async def test_exact_subject_session_and_holder_must_all_match(db_session):
    _owner, subject = await _patient(db_session, "emergency-exact-patient")
    _other_owner, other_subject = await _patient(
        db_session, "emergency-exact-other-patient"
    )
    holder, first, second = await _three_admins(db_session, "emergency-exact")
    row = await _activate(
        db_session,
        holder=holder,
        first=first,
        second=second,
        subject=subject,
    )

    with pytest.raises(emergency.EmergencySessionNotFound):
        await emergency.authorize_read(
            db_session,
            holder_user_id=first.id,
            subject_id=subject.id,
            session_id=row.id,
        )
    with pytest.raises(emergency.EmergencySessionNotFound):
        await emergency.authorize_read(
            db_session,
            holder_user_id=holder.id,
            subject_id=other_subject.id,
            session_id=row.id,
        )


async def test_role_loss_and_expiry_close_access_immediately(db_session):
    _owner, subject = await _patient(db_session, "emergency-live-patient")
    holder, first, second = await _three_admins(db_session, "emergency-live")
    row = await _activate(
        db_session,
        holder=holder,
        first=first,
        second=second,
        subject=subject,
    )
    await db_session.execute(
        UserRole.__table__.delete().where(UserRole.user_id == first.id)
    )
    with pytest.raises(emergency.EmergencySessionClosed):
        await emergency.authorize_read(
            db_session,
            holder_user_id=holder.id,
            subject_id=subject.id,
            session_id=row.id,
        )

    # Restore the reviewer, then move the stored expiry behind the DB clock.
    db_session.add(
        UserRole(user_id=first.id, role=UserRoleName.PLATFORM_SUPERADMIN.value)
    )
    row.activated_at = row.activated_at - timedelta(hours=1)
    row.expires_at = row.activated_at + timedelta(minutes=15)
    await db_session.flush()
    with pytest.raises(emergency.EmergencySessionClosed):
        await emergency.authorize_read(
            db_session,
            holder_user_id=holder.id,
            subject_id=subject.id,
            session_id=row.id,
        )


async def test_patient_can_revoke_pending_or_live_access_and_history_keeps_it(db_session):
    owner, subject = await _patient(db_session, "emergency-revoke-patient")
    holder, _first, _second = await _three_admins(db_session, "emergency-revoke")
    row = await _request(db_session, holder=holder, subject=subject)

    await emergency.revoke_by_owner(
        db_session,
        owner_user_id=owner.id,
        subject_id=subject.id,
        session_id=row.id,
    )
    history = await emergency.list_for_subject(
        db_session, owner_user_id=owner.id, subject_id=subject.id
    )
    assert len(history) == 1
    assert history[0].status == "revoked"
    state = await emergency.open_counts_for_subject(
        db_session, subject_id=subject.id
    )
    assert state.total_count == 0


async def test_patient_revoke_closes_active_access_and_keeps_history(db_session):
    owner, subject = await _patient(db_session, "emergency-active-revoke-patient")
    holder, first, second = await _three_admins(db_session, "emergency-active-revoke")
    row = await _activate(
        db_session,
        holder=holder,
        first=first,
        second=second,
        subject=subject,
    )
    before = await emergency.open_counts_for_subject(
        db_session, subject_id=subject.id
    )
    assert (before.pending_count, before.active_count) == (0, 1)

    await emergency.revoke_by_owner(
        db_session,
        owner_user_id=owner.id,
        subject_id=subject.id,
        session_id=row.id,
    )
    with pytest.raises(emergency.EmergencySessionClosed):
        await emergency.authorize_read(
            db_session,
            holder_user_id=holder.id,
            subject_id=subject.id,
            session_id=row.id,
        )
    history = await emergency.list_for_subject(
        db_session, owner_user_id=owner.id, subject_id=subject.id
    )
    assert history[0].status == "revoked"
    after = await emergency.open_counts_for_subject(
        db_session, subject_id=subject.id
    )
    assert after.total_count == 0


async def test_banner_includes_live_pending_but_filters_lapsed_pending(db_session):
    _owner, subject = await _patient(db_session, "emergency-banner-patient")
    holder = await _user(db_session, "emergency-banner-holder", admin=True)
    row = await _request(db_session, holder=holder, subject=subject)

    state = await emergency.open_counts_for_subject(
        db_session, subject_id=subject.id
    )
    assert (state.pending_count, state.active_count, state.total_count) == (1, 0, 1)

    row.initiated_at = row.initiated_at - timedelta(hours=1)
    row.approval_deadline = row.initiated_at + timedelta(minutes=15)
    await db_session.flush()
    state = await emergency.open_counts_for_subject(
        db_session, subject_id=subject.id
    )
    assert (state.pending_count, state.active_count, state.total_count) == (0, 0, 0)

    active_holder, first, second = await _three_admins(
        db_session, "emergency-banner-active"
    )
    active = await _activate(
        db_session,
        holder=active_holder,
        first=first,
        second=second,
        subject=subject,
    )
    state = await emergency.open_counts_for_subject(
        db_session, subject_id=subject.id
    )
    assert (state.pending_count, state.active_count, state.total_count) == (0, 1, 1)
    active.activated_at = active.activated_at - timedelta(hours=1)
    active.expires_at = active.activated_at + timedelta(minutes=15)
    await db_session.flush()
    state = await emergency.open_counts_for_subject(
        db_session, subject_id=subject.id
    )
    assert (state.pending_count, state.active_count, state.total_count) == (0, 0, 0)


async def test_open_audit_is_phi_free_and_names_only_loaded_scopes(db_session):
    _owner, subject = await _patient(db_session, "emergency-audit-patient")
    holder, first, second = await _three_admins(db_session, "emergency-audit")
    row = await _activate(
        db_session,
        holder=holder,
        first=first,
        second=second,
        subject=subject,
        domains=(Domain.LABS, Domain.WEIGHT),
    )
    access = await emergency.authorize_read(
        db_session,
        holder_user_id=holder.id,
        subject_id=subject.id,
        session_id=row.id,
    )
    await emergency.record_opened(
        db_session, authorization=access, loaded_domain_keys=(Domain.LABS.value,)
    )
    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == emergency.EVENT_OPENED)
    )
    assert event is not None
    assert event.support_access_grant_id is None
    assert event.metadata_json["scope_keys"] == ["domain:labs"]
    serialized = str(event.metadata_json)
    assert "synthetic outage" not in serialized.lower()
    assert "INC-SYNTHETIC" not in serialized


async def test_explicit_projection_never_calls_an_unapproved_loader(
    db_session, monkeypatch
):
    calls: list[str] = []

    async def _loaded(_session, _subject_id, _window):
        calls.append("labs")
        return record_projection._LoadedSection(value={}, row_count=0)

    async def _forbidden(_session, _subject_id, _window):
        raise AssertionError("an unapproved loader was queried")

    monkeypatch.setitem(record_projection._LOADERS, "labs", _loaded)
    monkeypatch.setitem(record_projection._LOADERS, "weight", _forbidden)
    result = await record_projection.assemble_record_projection(
        db_session,
        subject_id=(await _patient(db_session, "emergency-projection"))[1].id,
        allowed_domain_keys=(Domain.LABS.value,),
        enabled_modules={"labs": True, "weight": True},
        subject_timezone_name="UTC",
    )
    assert calls == ["labs"]
    assert result.loaded_domains == (Domain.LABS.value,)
    assert Domain.WEIGHT.value not in result.loaded_domains


async def test_projection_does_not_query_forbidden_emergency_surfaces(db_session):
    _owner, subject = await _patient(db_session, "emergency-sql-boundary")
    statements: list[str] = []
    sync_engine = db_session.get_bind()

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        await record_projection.assemble_record_projection(
            db_session,
            subject_id=subject.id,
            allowed_domain_keys=tuple(
                sorted(domain.value for domain in emergency.ALLOWED_DOMAINS)
            ),
            enabled_modules={section.module: True for section in record_projection.SECTIONS},
            subject_timezone_name="UTC",
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)

    sql = "\n".join(statements)
    assert "lab_results" in sql
    for forbidden in (
        "raw_payloads",
        "file_assets",
        "professional_notes",
        "care_messages",
        "care_plans",
        "support_access_grants",
    ):
        offending = [statement for statement in statements if forbidden in statement]
        assert not offending, (forbidden, offending)
    for forbidden_column in (
        "raw_payload_id",
        "file_asset_id",
        "file_key",
        "external_id",
        "interpretation",
        "action_notes",
        "usage_instructions",
        "contraindications",
        ".note",
        ".notes",
    ):
        offending = [
            statement for statement in statements if forbidden_column in statement
        ]
        assert not offending, (forbidden_column, offending)
    assert "select *" not in sql


def test_emergency_allowlist_is_explicit_and_exact():
    assert emergency.APPROVAL_WINDOW == timedelta(minutes=15)
    assert emergency.ALLOWED_TTL_MINUTES == {15, 30, 60}
    assert emergency.ALLOWED_DOMAINS == {
        Domain.WEIGHT,
        Domain.LABS,
        Domain.BODY_COMPOSITION,
        Domain.NUTRITION,
        Domain.HRT,
        Domain.GLP1,
        Domain.SUPPLEMENTS,
        Domain.SKINCARE,
        Domain.GENETICS,
        Domain.GARMIN,
        Domain.WORKOUTS,
    }
    assert emergency.ALLOWED_DOMAINS == {
        section.domain for section in record_projection.SECTIONS
    }
    assert set(record_projection._LOADERS) == {
        section.key for section in record_projection.SECTIONS
    }


def test_emergency_service_has_no_support_or_mcp_authority_dependency():
    source = __import__("inspect").getsource(emergency)
    assert "support_access_service" not in source
    assert "resolve_access_context" not in source
    assert "vitals.services.mcp" not in source.lower()


def test_model_exact_composite_constraints_match_migration_contract():
    from importlib import import_module

    from vitals.models.break_glass import (
        BreakGlassApproval,
        BreakGlassScope,
        BreakGlassSession,
    )

    migration = import_module("migrations.versions.0078_break_glass_sessions")
    assert migration.down_revision == "0077"
    session_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in BreakGlassSession.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert session_uniques["uq_break_glass_sessions_id_subject"] == (
        "id",
        "subject_id",
    )
    assert session_uniques["uq_break_glass_sessions_id_subject_holder"] == (
        "id",
        "subject_id",
        "initiated_by_user_id",
    )
    scope_fk = next(
        constraint
        for constraint in BreakGlassScope.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_break_glass_scopes_exact_session"
    )
    assert tuple(column.name for column in scope_fk.columns) == (
        "session_id",
        "subject_id",
    )
    approval_fk = next(
        constraint
        for constraint in BreakGlassApproval.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_break_glass_approvals_exact_session"
    )
    assert tuple(column.name for column in approval_fk.columns) == (
        "session_id",
        "subject_id",
        "holder_user_id",
    )


def _login_identity(identity: User) -> tuple[str, object, int]:
    return identity.username, identity.id, identity.session_version


def _sign_in(
    client, identity: str | tuple[str, object, int], *, stale: bool = False
) -> None:
    from web.auth import create_federated_session, create_session
    from web.config import SESSION_COOKIE

    if isinstance(identity, tuple):
        username, user_id, session_version = identity
        age_seconds = 3600 if stale else 0
        token = create_federated_session(
            username=username,
            user_id=user_id,
            session_version=session_version,
            authenticated_at=(
                int(datetime.now(timezone.utc).timestamp()) - age_seconds
            ),
            subject_id=None,
        )
    else:
        if stale:
            raise ValueError("stale synthetic login requires a database identity")
        token = create_session(identity)
    client.cookies.set(
        SESSION_COOKIE,
        token,
    )


async def test_patient_page_always_shows_emergency_history_empty_state(
    client, legacy_owner_roots
):
    _sign_in(client, "tester")
    page = await client.get("/settings/access", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "data-break-glass-history" in page.text
    assert "data-break-glass-empty" in page.text
    del legacy_owner_roots


async def test_http_flow_needs_two_reviewers_and_pending_is_in_patient_banner(
    client, db_session, legacy_owner_roots
):
    holder, first, second = await _three_admins(db_session, "web-emergency")
    holder_login = _login_identity(holder)
    first_login = _login_identity(first)
    second_login = _login_identity(second)
    await db_session.commit()
    subject_id = legacy_owner_roots.subject_id

    _sign_in(client, holder_login)
    opened = await client.post(
        "/settings/platform/break-glass/initiate",
        data={
            "subject_selector": str(subject_id),
            "reason": "Synthetic emergency for the browser flow.",
            "incident_reference": "INC-WEB-SYNTHETIC",
            "domains": Domain.LABS.value,
            "ttl_minutes": "15",
        },
        headers={"Referer": "http://test/settings/platform/break-glass"},
    )
    assert opened.status_code == 303
    location = opened.headers["location"]
    assert location.startswith(f"/settings/platform/break-glass/{subject_id}/session/")

    _sign_in(client, "tester")
    banner = await client.get("/weight", headers={"Accept": "text/html"})
    assert banner.status_code == 200
    assert "data-break-glass-banner" in banner.text
    history = await client.get("/settings/access", headers={"Accept": "text/html"})
    assert 'data-break-glass-session="pending"' in history.text

    _sign_in(client, first_login)
    approval_one = await client.post(
        f"{location}/approve",
        headers={"Referer": f"http://test{location}"},
    )
    assert approval_one.status_code == 303

    _sign_in(client, holder_login)
    before_second = await client.get(
        f"{location}/record", headers={"Accept": "text/html"}
    )
    assert before_second.status_code == 404

    _sign_in(client, second_login)
    approval_two = await client.post(
        f"{location}/approve",
        headers={"Referer": f"http://test{location}"},
    )
    assert approval_two.status_code == 303

    _sign_in(client, holder_login)
    record = await client.get(
        f"{location}/record", headers={"Accept": "text/html"}
    )
    assert record.status_code == 200
    assert "data-break-glass-record" in record.text
    assert "no-store" in record.headers["cache-control"]
    assert "/files/" not in record.text
    assert "/messages" not in record.text


def test_emergency_record_and_selector_views_require_recent_auth():
    import inspect

    from web.deps import require_recent_auth
    from web.routers import break_glass

    for endpoint in (
        break_glass.console,
        break_glass.inspect_redirect,
        break_glass.session_detail,
        break_glass.record,
    ):
        dependencies = {
            parameter.default.dependency
            for parameter in inspect.signature(endpoint).parameters.values()
            if hasattr(parameter.default, "dependency")
        }
        assert require_recent_auth in dependencies, endpoint.__name__


def test_no_emergency_mcp_or_api_route_exists():
    from web.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert not any(
        "break-glass" in path and not path.startswith("/settings/")
        for path in paths
    )
