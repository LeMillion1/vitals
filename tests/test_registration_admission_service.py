"""Account admission is explicit, one-time, and race-safe.

The persistence tables intentionally arrived before this service.  These tests
pin the behavior that makes those rows useful without turning either an OIDC
claim or an address into ambient permission to create an account.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    RegistrationAccountKind,
    RegistrationInvitationStatus,
    RegistrationRequestStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import (
    AuditEvent,
    HealthSubject,
    User,
    UserFederatedIdentity,
    UserRole,
)
from vitals.models.registration import RegistrationInvitation, RegistrationRequest
from vitals.persistence.rls import in_platform_scope
from vitals.services.authentication import admission
from vitals.services.authentication import registration as registration_policy
from vitals.services.authentication.admission import retention as retention_service


ISSUER = "https://idp.example.test"


async def _consume_invitation(session: AsyncSession, **kwargs):
    """Present the verified-claim bit the OIDC boundary proved."""

    return await admission.consume_invitation(
        session,
        email_verified=True,
        **kwargs,
    )


async def _submit_request(session: AsyncSession, **kwargs):
    """Submit through the strict verified-email admission boundary."""

    return await admission.submit_request(
        session,
        email_verified=True,
        **kwargs,
    )


async def _user(
    session: AsyncSession,
    slug: str,
    *,
    roles: tuple[UserRoleName, ...] = (),
    status: UserStatus = UserStatus.ACTIVE,
    email: str | None = None,
) -> User:
    user = User(
        username=slug,
        normalized_username=slug.casefold(),
        email=email,
        normalized_email=email.casefold() if email else None,
        password_hash="$synthetic-admission-test-hash",
        status=status.value,
    )
    session.add(user)
    await session.flush()
    session.add_all(
        UserRole(user_id=user.id, role=role.value) for role in roles
    )
    await session.flush()
    return user


async def _admin(session: AsyncSession, slug: str = "admission-admin") -> User:
    return await _user(
        session,
        slug,
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )


async def _mode(
    session: AsyncSession,
    monkeypatch,
    mode: registration_policy.RegistrationMode,
) -> None:
    monkeypatch.setenv(registration_policy.REGISTRATION_UNLOCK_ENV, "1")
    await registration_policy.set_stored_mode(session, mode)


async def _issued(
    session: AsyncSession,
    monkeypatch,
    *,
    slug: str = "member",
    kind: RegistrationAccountKind = RegistrationAccountKind.MEMBER,
):
    await _mode(session, monkeypatch, registration_policy.RegistrationMode.INVITE_ONLY)
    actor = await _admin(session, f"issuer-{slug}")
    issued = await admission.issue_invitation(
        session,
        actor_user_id=actor.id,
        email=f"{slug}@example.test",
        account_kind=kind,
    )
    return actor, issued


async def _request(
    session: AsyncSession,
    monkeypatch,
    *,
    suffix: str = "member",
) -> RegistrationRequest:
    await _mode(
        session,
        monkeypatch,
        registration_policy.RegistrationMode.ADMIN_APPROVED,
    )
    return await _submit_request(
        session,
        issuer=ISSUER,
        subject=f"request-{suffix}",
        verified_email=f"{suffix}@example.test",
        preferred_username=f"request-{suffix}",
    )


def _age_invitation(row: RegistrationInvitation, *, days: int = 40) -> None:
    row.created_at = datetime.now(timezone.utc) - timedelta(days=days)
    row.expires_at = row.created_at + timedelta(days=14)


def _age_request(row: RegistrationRequest, *, days: int = 50) -> None:
    row.created_at = datetime.now(timezone.utc) - timedelta(days=days)
    row.last_seen_at = row.created_at
    row.expires_at = row.created_at + timedelta(days=30)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def test_issue_stores_only_the_token_digest_and_redacted_audit(
    db_session, monkeypatch
):
    actor, issued = await _issued(db_session, monkeypatch, slug="token-secret")

    assert issued.token
    assert issued.invitation.token_digest == hashlib.sha256(
        issued.token.encode()
    ).hexdigest()
    assert issued.token not in issued.invitation.token_digest
    assert issued.invitation.invited_by_user_id == actor.id

    event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "registration.invitation.issued"
        )
    )
    assert event is not None and event.actor_user_id == actor.id
    envelope = json.dumps(event.metadata_json, sort_keys=True)
    for secret in (issued.token, "token-secret@example.test"):
        assert secret not in envelope


async def test_claim_exchanges_a_live_bearer_for_only_its_opaque_id(
    db_session, monkeypatch
):
    _actor, issued = await _issued(db_session, monkeypatch, slug="browser-claim")

    first = await admission.claim_invitation(db_session, token=issued.token)
    second = await admission.claim_invitation(db_session, token=issued.token)

    assert first == second == issued.invitation.id
    assert issued.invitation.status == RegistrationInvitationStatus.PENDING.value
    assert issued.invitation.token_digest is not None
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity)
    ) == 0


async def test_claim_refuses_unknown_spent_and_wrong_mode_uniformly(
    db_session, legacy_owner_roots, monkeypatch
):
    _actor, issued = await _issued(db_session, monkeypatch, slug="claim-uniform")
    await _consume_invitation(
        db_session,
        token=issued.token,
        issuer=ISSUER,
        subject="claim-uniform",
        verified_email="claim-uniform@example.test",
    )

    messages = set()
    for token in (issued.token, "not-issued"):
        with pytest.raises(admission.AdmissionRefused) as caught:
            await admission.claim_invitation(db_session, token=token)
        messages.add(str(caught.value))
    await _mode(db_session, monkeypatch, registration_policy.RegistrationMode.DISABLED)
    with pytest.raises(admission.AdmissionRefused) as caught:
        await admission.claim_invitation(db_session, token="not-issued")
    messages.add(str(caught.value))
    assert len(messages) == 1


@pytest.mark.parametrize(
    "ttl",
    [
        timedelta(0),
        timedelta(days=-1),
        admission.MAX_INVITATION_TTL + timedelta(seconds=1),
    ],
)
async def test_invitation_ttl_is_positive_and_bounded(db_session, monkeypatch, ttl):
    await _mode(db_session, monkeypatch, registration_policy.RegistrationMode.INVITE_ONLY)
    actor = await _admin(db_session, f"ttl-admin-{abs(hash(ttl))}")
    with pytest.raises(admission.AdmissionValidationError):
        await admission.issue_invitation(
            db_session,
            actor_user_id=actor.id,
            email=f"ttl-{abs(hash(ttl))}@example.test",
            account_kind=RegistrationAccountKind.MEMBER,
            ttl=ttl,
        )


async def test_issue_refuses_an_address_already_owned_by_a_local_account(
    db_session, monkeypatch
):
    await _mode(db_session, monkeypatch, registration_policy.RegistrationMode.INVITE_ONLY)
    actor = await _admin(db_session, "existing-email-admin")
    await _user(db_session, "existing-email-user", email="owned@example.test")

    with pytest.raises(admission.AdmissionValidationError) as caught:
        await admission.issue_invitation(
            db_session,
            actor_user_id=actor.id,
            email="  OWNED@example.test ",
            account_kind=RegistrationAccountKind.MEMBER,
        )
    assert "owned@example.test" not in str(caught.value).casefold()


async def test_reissue_revokes_the_live_token_instead_of_faking_expiry(
    db_session, monkeypatch
):
    actor, first = await _issued(db_session, monkeypatch, slug="supersede")
    replacement = await admission.issue_invitation(
        db_session,
        actor_user_id=actor.id,
        email="supersede@example.test",
        account_kind=RegistrationAccountKind.DOCTOR,
    )

    assert first.invitation.status == RegistrationInvitationStatus.REVOKED.value
    assert first.invitation.revoked_by_user_id == actor.id
    assert first.invitation.revoked_at is not None
    assert first.invitation.expired_at is None
    assert replacement.invitation.status == RegistrationInvitationStatus.PENDING.value
    assert replacement.token != first.token
    event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "registration.invitation.revoked",
            AuditEvent.resource_id == str(first.invitation.id),
        )
    )
    assert event is not None
    assert event.metadata_json["result_code"] == "superseded"


@pytest.mark.parametrize(
    "roles,status",
    [
        ((), UserStatus.ACTIVE),
        ((UserRoleName.MEMBER,), UserStatus.ACTIVE),
        ((UserRoleName.PLATFORM_SUPERADMIN,), UserStatus.SUSPENDED),
    ],
)
async def test_only_an_active_platform_admin_issues_invitations(
    db_session, monkeypatch, roles, status
):
    await _mode(db_session, monkeypatch, registration_policy.RegistrationMode.INVITE_ONLY)
    actor = await _user(db_session, f"non-admin-{status.value}-{len(roles)}", roles=roles, status=status)

    with pytest.raises(admission.AdmissionForbidden):
        await admission.issue_invitation(
            db_session,
            actor_user_id=actor.id,
            email="invited@example.test",
            account_kind=RegistrationAccountKind.MEMBER,
        )


async def test_invitation_can_be_revoked_only_while_pending(db_session, monkeypatch):
    actor, issued = await _issued(db_session, monkeypatch, slug="revoke")

    revoked = await admission.revoke_invitation(
        db_session,
        invitation_id=issued.invitation.id,
        actor_user_id=actor.id,
    )
    assert revoked.status == RegistrationInvitationStatus.REVOKED.value
    assert revoked.revoked_by_user_id == actor.id and revoked.revoked_at is not None

    with pytest.raises(admission.AdmissionStateError):
        await admission.revoke_invitation(
            db_session,
            invitation_id=issued.invitation.id,
            actor_user_id=actor.id,
        )


async def test_invitation_revocation_rechecks_platform_admin_authorization(
    db_session, monkeypatch
):
    _actor, issued = await _issued(db_session, monkeypatch, slug="revoke-auth")
    outsider = await _user(db_session, "revoke-outsider")

    with pytest.raises(admission.AdmissionForbidden):
        await admission.revoke_invitation(
            db_session,
            invitation_id=issued.invitation.id,
            actor_user_id=outsider.id,
        )
    assert issued.invitation.status == RegistrationInvitationStatus.PENDING.value


@pytest.mark.parametrize(
    ("kind", "expected_role", "has_record"),
    [
        (RegistrationAccountKind.MEMBER, UserRoleName.MEMBER, True),
        (RegistrationAccountKind.DOCTOR, UserRoleName.DOCTOR, False),
        (RegistrationAccountKind.TRAINER, UserRoleName.TRAINER, False),
    ],
)
async def test_consumption_maps_kind_to_one_role_and_the_right_record_shape(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    kind,
    expected_role,
    has_record,
):
    actor, issued = await _issued(
        db_session, monkeypatch, slug=f"consume-{kind.value}", kind=kind
    )
    authenticated_at = datetime.now(timezone.utc) - timedelta(minutes=3)

    result = await _consume_invitation(
        db_session,
        token=issued.token,
        issuer=ISSUER,
        subject=f"consume-{kind.value}",
        verified_email=f"consume-{kind.value}@example.test",
        preferred_username=f"new-{kind.value}",
        authenticated_at=authenticated_at,
    )

    role = await db_session.scalar(
        select(UserRole).where(UserRole.user_id == result.user.id)
    )
    link = await db_session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.user_id == result.user.id
        )
    )
    subject_id = await db_session.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == result.user.id)
    )
    assert role is not None
    assert role.role == expected_role.value
    assert role.assigned_by_user_id == actor.id
    assert link is not None
    assert link.last_authenticated_at is not None
    assert _utc(link.last_authenticated_at) == authenticated_at
    assert (subject_id is not None) is has_record
    assert issued.invitation.status == RegistrationInvitationStatus.CONSUMED.value
    assert issued.invitation.consumed_by_user_id == result.user.id


async def test_invalid_or_taken_username_uses_an_opaque_stable_fallback(
    db_session, legacy_owner_roots, monkeypatch
):
    await _user(db_session, "claimed-name")
    _actor, first = await _issued(db_session, monkeypatch, slug="fallback-a")
    first_result = await _consume_invitation(
        db_session,
        token=first.token,
        issuer=ISSUER,
        subject="fallback-a",
        verified_email="fallback-a@example.test",
        preferred_username="claimed-name",
    )
    _actor, second = await _issued(db_session, monkeypatch, slug="fallback-b")
    second_result = await _consume_invitation(
        db_session,
        token=second.token,
        issuer=ISSUER,
        subject="fallback-b",
        verified_email="fallback-b@example.test",
        preferred_username="invalid\x00claim",
    )

    assert first_result.user.normalized_username != "claimed-name"
    assert second_result.user.normalized_username != "invalid\x00claim"
    assert first_result.user.normalized_username != second_result.user.normalized_username


async def test_unknown_expired_replayed_wrong_and_unverified_invites_refuse_uniformly(
    db_session, legacy_owner_roots, monkeypatch
):
    _actor, spent = await _issued(db_session, monkeypatch, slug="uniform-spent")
    await _consume_invitation(
        db_session,
        token=spent.token,
        issuer=ISSUER,
        subject="uniform-spent",
        verified_email="uniform-spent@example.test",
    )
    _actor, expired = await _issued(db_session, monkeypatch, slug="uniform-expired")
    _age_invitation(expired.invitation)
    _actor, wrong = await _issued(db_session, monkeypatch, slug="uniform-wrong")
    _actor, unverified = await _issued(
        db_session, monkeypatch, slug="uniform-unverified"
    )
    await db_session.flush()

    attempts = (
        ("not-issued", "nobody@example.test", "unknown"),
        (expired.token, "uniform-expired@example.test", "expired"),
        (spent.token, "uniform-spent@example.test", "replay"),
        (wrong.token, "somebody-else@example.test", "wrong"),
        (unverified.token, None, "unverified"),
    )
    messages: set[str] = set()
    for token, email, subject in attempts:
        with pytest.raises(admission.AdmissionRefused) as caught:
            await _consume_invitation(
                db_session,
                token=token,
                issuer=ISSUER,
                subject=subject,
                verified_email=email,
            )
        messages.add(str(caught.value))
    assert len(messages) == 1


@pytest.mark.parametrize("email_verified", [False, None, 1, "true"])
async def test_invitation_requires_the_literal_verified_claim_bit(
    db_session, monkeypatch, email_verified
):
    suffix = f"proof-{type(email_verified).__name__}-{str(email_verified).casefold()}"
    _actor, issued = await _issued(db_session, monkeypatch, slug=suffix)
    with pytest.raises(admission.AdmissionRefused):
        await admission.consume_invitation(
            db_session,
            token=issued.token,
            issuer=ISSUER,
            subject=f"proof-{email_verified!r}",
            verified_email=issued.invitation.normalized_email,
            email_verified=email_verified,
        )
    assert issued.invitation.status == RegistrationInvitationStatus.PENDING.value


async def test_oversized_token_is_a_uniform_refusal_before_hashing(
    db_session, monkeypatch
):
    _actor, _issued_token = await _issued(db_session, monkeypatch, slug="long-token")
    messages = set()
    for token in ("unknown", "x" * 513, " token-with-whitespace "):
        with pytest.raises(admission.AdmissionRefused) as caught:
            await _consume_invitation(
                db_session,
                token=token,
                issuer=ISSUER,
                subject="long-token",
                verified_email="long-token@example.test",
            )
        messages.add(str(caught.value))
    assert len(messages) == 1


async def test_refused_invitation_proof_never_enters_platform_scope(
    db_session, monkeypatch
):
    await _mode(
        db_session,
        monkeypatch,
        registration_policy.RegistrationMode.INVITE_ONLY,
    )

    with pytest.raises(admission.AdmissionRefused):
        await _consume_invitation(
            db_session,
            token="not-issued",
            issuer=ISSUER,
            subject="scope-refusal",
            verified_email="scope-refusal@example.test",
        )

    assert not in_platform_scope(db_session)


async def test_verified_email_collision_is_a_uniform_refusal_without_partial_graph(
    db_session, legacy_owner_roots, monkeypatch
):
    await _user(db_session, "email-owner", email="collision@example.test")
    _actor, issued = await _issued(
        db_session, monkeypatch, slug="email-invite-collision"
    )
    issued.invitation.normalized_email = "collision@example.test"
    await db_session.flush()

    with pytest.raises(admission.AdmissionRefused) as collision:
        await _consume_invitation(
            db_session,
            token=issued.token,
            issuer=ISSUER,
            subject="email-collision",
            verified_email="collision@example.test",
        )
    with pytest.raises(admission.AdmissionRefused) as unknown:
        await _consume_invitation(
            db_session,
            token="not-issued",
            issuer=ISSUER,
            subject="unknown",
            verified_email="collision@example.test",
        )
    assert str(collision.value) == str(unknown.value)
    assert not in_platform_scope(db_session)
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "email-collision"
        )
    ) == 0


async def test_consumption_audit_contains_no_oidc_claim_or_invitation_secret(
    db_session, legacy_owner_roots, monkeypatch
):
    _actor, issued = await _issued(db_session, monkeypatch, slug="audit-consume")
    result = await _consume_invitation(
        db_session,
        token=issued.token,
        issuer=ISSUER,
        subject="audit-subject-secret",
        verified_email="audit-consume@example.test",
        preferred_username="audit-name-secret",
    )
    event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "registration.invitation.consumed"
        )
    )
    assert event is not None and event.resource_id == str(issued.invitation.id)
    envelope = json.dumps(event.metadata_json, sort_keys=True)
    for secret in (
        issued.token,
        ISSUER,
        "audit-subject-secret",
        "audit-consume@example.test",
        "audit-name-secret",
    ):
        assert secret not in envelope
    assert str(result.user.id) not in envelope


async def test_invalid_authentication_time_is_rejected_before_any_account_mutation(
    db_session, legacy_owner_roots, monkeypatch
):
    _actor, issued = await _issued(db_session, monkeypatch, slug="invalid-auth-time")

    with pytest.raises(admission.AdmissionValidationError):
        await _consume_invitation(
            db_session,
            token=issued.token,
            issuer=ISSUER,
            subject="invalid-auth-time",
            verified_email="invalid-auth-time@example.test",
            authenticated_at="not-a-timestamp",
        )

    assert not in_platform_scope(db_session)
    assert issued.invitation.status == RegistrationInvitationStatus.PENDING.value
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "invalid-auth-time"
        )
    ) == 0


async def test_submit_is_repeatable_server_fixed_member_and_creates_no_account(
    db_session, monkeypatch
):
    request = await _request(db_session, monkeypatch, suffix="repeat")
    first_seen = request.last_seen_at
    again = await _submit_request(
        db_session,
        issuer=ISSUER,
        subject="request-repeat",
        verified_email="REPEAT@example.test",
        preferred_username="changed-claim",
    )

    assert again.id == request.id
    assert again.account_kind == RegistrationAccountKind.MEMBER.value
    assert again.last_seen_at >= first_seen
    assert await db_session.scalar(select(func.count()).select_from(User)) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(RegistrationRequest)
    ) == 1


async def test_request_submission_requires_admin_approved_mode(
    db_session, monkeypatch
):
    monkeypatch.setenv(registration_policy.REGISTRATION_UNLOCK_ENV, "1")
    for mode in (
        registration_policy.RegistrationMode.DISABLED,
        registration_policy.RegistrationMode.INVITE_ONLY,
        registration_policy.RegistrationMode.OPEN,
    ):
        await registration_policy.set_stored_mode(db_session, mode)
        with pytest.raises(admission.AdmissionRefused):
            await _submit_request(
                db_session,
                issuer=ISSUER,
                subject=f"wrong-mode-{mode.value}",
                verified_email="member@example.test",
            )


async def test_request_submission_without_verified_email_is_uniformly_refused(
    db_session, monkeypatch
):
    await _mode(
        db_session,
        monkeypatch,
        registration_policy.RegistrationMode.ADMIN_APPROVED,
    )
    messages = set()
    for email in (None, "", "not-an-address"):
        with pytest.raises(admission.AdmissionRefused) as caught:
            await _submit_request(
                db_session,
                issuer=ISSUER,
                subject=f"unverified-{email!r}",
                verified_email=email,
            )
        messages.add(str(caught.value))
    assert len(messages) == 1


@pytest.mark.parametrize("email_verified", [False, None, 1, "true"])
async def test_request_requires_the_literal_verified_claim_bit(
    db_session, monkeypatch, email_verified
):
    await _mode(
        db_session,
        monkeypatch,
        registration_policy.RegistrationMode.ADMIN_APPROVED,
    )
    with pytest.raises(admission.AdmissionRefused):
        await admission.submit_request(
            db_session,
            issuer=ISSUER,
            subject=f"request-proof-{email_verified!r}",
            verified_email="request-proof@example.test",
            email_verified=email_verified,
        )


@pytest.mark.parametrize(
    "ttl",
    [
        timedelta(0),
        timedelta(days=-1),
        admission.MAX_REQUEST_TTL + timedelta(seconds=1),
    ],
)
async def test_request_ttl_is_positive_and_bounded(db_session, monkeypatch, ttl):
    await _mode(
        db_session,
        monkeypatch,
        registration_policy.RegistrationMode.ADMIN_APPROVED,
    )
    with pytest.raises(admission.AdmissionValidationError):
        await _submit_request(
            db_session,
            issuer=ISSUER,
            subject=f"request-ttl-{abs(hash(ttl))}",
            verified_email=f"request-ttl-{abs(hash(ttl))}@example.test",
            ttl=ttl,
        )


async def test_request_lookup_is_exact_and_available_only_in_its_mode(
    db_session, monkeypatch
):
    request = await _request(db_session, monkeypatch, suffix="lookup")

    found = await admission.get_request(
        db_session,
        issuer=ISSUER,
        subject="request-lookup",
    )
    assert found is not None and found.id == request.id
    assert await admission.get_request(
        db_session,
        issuer=ISSUER,
        subject="somebody-else",
    ) is None

    await registration_policy.set_stored_mode(
        db_session, registration_policy.RegistrationMode.DISABLED
    )
    with pytest.raises(admission.AdmissionRefused):
        await admission.get_request(
            db_session,
            issuer=ISSUER,
            subject="request-lookup",
        )


async def test_approve_provisions_one_member_graph_with_human_role_provenance(
    db_session, legacy_owner_roots, monkeypatch
):
    request = await _request(db_session, monkeypatch, suffix="approve")
    reviewer = await _admin(db_session, "request-reviewer")

    result = await admission.approve_request(
        db_session,
        request_id=request.id,
        reviewer_user_id=reviewer.id,
    )

    role = await db_session.scalar(
        select(UserRole).where(UserRole.user_id == result.user.id)
    )
    link = await db_session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.user_id == result.user.id
        )
    )
    record = await db_session.scalar(
        select(HealthSubject).where(HealthSubject.owner_user_id == result.user.id)
    )
    assert request.status == RegistrationRequestStatus.APPROVED.value
    assert request.reviewer_user_id == reviewer.id
    assert request.provisioned_user_id == result.user.id
    assert role is not None and role.role == UserRoleName.MEMBER.value
    assert role.assigned_by_user_id == reviewer.id
    assert link is not None and link.issuer == ISSUER
    assert record is not None
    assert result.user.email is None
    assert result.user.normalized_email is None
    assert result.user.email_verified_at is None
    assert link.last_authenticated_at is None


async def test_approval_refuses_saved_email_collision_without_merging(
    db_session, legacy_owner_roots, monkeypatch
):
    await _user(db_session, "existing-mailbox", email="request-collision@example.test")
    request = await _request(db_session, monkeypatch, suffix="collision")
    request.verified_email = "request-collision@example.test"
    request.normalized_verified_email = "request-collision@example.test"
    reviewer = await _admin(db_session, "collision-reviewer")
    await db_session.flush()

    with pytest.raises(admission.AdmissionRefused):
        await admission.approve_request(
            db_session,
            request_id=request.id,
            reviewer_user_id=reviewer.id,
        )

    assert request.status == RegistrationRequestStatus.PENDING.value
    assert request.provisioned_user_id is None
    assert not in_platform_scope(db_session)
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "request-collision"
        )
    ) == 0


async def test_reject_records_bounded_reason_without_creating_identity(
    db_session, monkeypatch
):
    request = await _request(db_session, monkeypatch, suffix="reject")
    reviewer = await _admin(db_session, "reject-reviewer")

    rejected = await admission.reject_request(
        db_session,
        request_id=request.id,
        reviewer_user_id=reviewer.id,
        reason="  identity could not be verified  ",
    )
    assert rejected.status == RegistrationRequestStatus.REJECTED.value
    assert rejected.review_note == "identity could not be verified"
    assert rejected.reviewer_user_id == reviewer.id
    assert await db_session.scalar(
        select(func.count()).select_from(UserFederatedIdentity)
    ) == 0


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_request_decisions_require_an_active_platform_admin(
    db_session, legacy_owner_roots, monkeypatch, decision
):
    request = await _request(db_session, monkeypatch, suffix=f"forbidden-{decision}")
    outsider = await _user(db_session, f"outsider-{decision}")

    with pytest.raises(admission.AdmissionForbidden):
        if decision == "approve":
            await admission.approve_request(
                db_session,
                request_id=request.id,
                reviewer_user_id=outsider.id,
            )
        else:
            await admission.reject_request(
                db_session,
                request_id=request.id,
                reviewer_user_id=outsider.id,
                reason="not allowed",
            )
    assert not in_platform_scope(db_session)


@pytest.mark.parametrize("first", ["approve", "reject"])
async def test_request_terminal_decisions_are_strict(
    db_session, legacy_owner_roots, monkeypatch, first
):
    request = await _request(db_session, monkeypatch, suffix=f"strict-{first}")
    reviewer = await _admin(db_session, f"strict-reviewer-{first}")
    if first == "approve":
        await admission.approve_request(
            db_session, request_id=request.id, reviewer_user_id=reviewer.id
        )
    else:
        await admission.reject_request(
            db_session,
            request_id=request.id,
            reviewer_user_id=reviewer.id,
            reason="not verified",
        )

    for operation in ("approve", "reject"):
        with pytest.raises(admission.AdmissionStateError):
            if operation == "approve":
                await admission.approve_request(
                    db_session,
                    request_id=request.id,
                    reviewer_user_id=reviewer.id,
                )
            else:
                await admission.reject_request(
                    db_session,
                    request_id=request.id,
                    reviewer_user_id=reviewer.id,
                    reason="still no",
                )


@pytest.mark.parametrize("terminal", ["rejected", "expired"])
async def test_terminal_request_reapply_has_a_cooldown_then_creates_a_new_row(
    db_session, monkeypatch, terminal
):
    request = await _request(db_session, monkeypatch, suffix=f"cooldown-{terminal}")
    if terminal == "rejected":
        reviewer = await _admin(db_session, "cooldown-reviewer")
        await admission.reject_request(
            db_session,
            request_id=request.id,
            reviewer_user_id=reviewer.id,
            reason="synthetic rejection",
        )
        terminal_at_field = "reviewed_at"
    else:
        _age_request(request)
        await db_session.flush()
        await admission.expire_due(db_session)
        terminal_at_field = "expired_at"

    with pytest.raises(admission.AdmissionRefused):
        await _submit_request(
            db_session,
            issuer=ISSUER,
            subject=f"request-cooldown-{terminal}",
            verified_email=f"cooldown-{terminal}@example.test",
        )

    older = datetime.now(timezone.utc) - admission.REQUEST_REAPPLY_COOLDOWN - timedelta(
        minutes=1
    )
    request.created_at = older - timedelta(days=31)
    request.last_seen_at = request.created_at
    request.expires_at = request.created_at + timedelta(days=30)
    setattr(request, terminal_at_field, older)
    await db_session.flush()

    reapplied = await _submit_request(
        db_session,
        issuer=ISSUER,
        subject=f"request-cooldown-{terminal}",
        verified_email=f"cooldown-{terminal}@example.test",
    )
    assert reapplied.id != request.id
    assert reapplied.status == RegistrationRequestStatus.PENDING.value


@pytest.mark.parametrize("operation", ["revoke", "approve", "reject"])
async def test_stale_operator_actions_record_expiry_before_refusing(
    db_session, legacy_owner_roots, monkeypatch, operation
):
    reviewer = await _admin(db_session, f"expiry-reviewer-{operation}")
    if operation == "revoke":
        await _mode(
            db_session,
            monkeypatch,
            registration_policy.RegistrationMode.INVITE_ONLY,
        )
        issued = await admission.issue_invitation(
            db_session,
            actor_user_id=reviewer.id,
            email="stale-revoke@example.test",
            account_kind=RegistrationAccountKind.MEMBER,
        )
        _age_invitation(issued.invitation)
        await db_session.flush()
        resource_id = issued.invitation.id
        with pytest.raises(admission.AdmissionStateError):
            await admission.revoke_invitation(
                db_session,
                invitation_id=resource_id,
                actor_user_id=reviewer.id,
            )
        event_type = "registration.invitation.expired"
    else:
        request = await _request(
            db_session,
            monkeypatch,
            suffix=f"stale-{operation}",
        )
        _age_request(request)
        await db_session.flush()
        resource_id = request.id
        with pytest.raises(admission.AdmissionStateError):
            if operation == "approve":
                await admission.approve_request(
                    db_session,
                    request_id=resource_id,
                    reviewer_user_id=reviewer.id,
                )
            else:
                await admission.reject_request(
                    db_session,
                    request_id=resource_id,
                    reviewer_user_id=reviewer.id,
                    reason="stale request",
                )
        event_type = "registration.request.expired"

    event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == event_type,
            AuditEvent.resource_id == str(resource_id),
        )
    )
    assert event is not None
    assert event.actor_user_id == reviewer.id
    assert event.metadata_json["result_code"] == f"expired_on_{operation}"


async def test_review_audits_are_value_free(db_session, monkeypatch):
    request = await _request(db_session, monkeypatch, suffix="audit-review")
    reviewer = await _admin(db_session, "audit-reviewer")
    await admission.reject_request(
        db_session,
        request_id=request.id,
        reviewer_user_id=reviewer.id,
        reason="private operator rationale",
    )

    events = list(
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(
                    {
                        "registration.request.submitted",
                        "registration.request.rejected",
                    }
                )
            )
        )
    )
    assert len(events) == 2
    envelope = json.dumps([event.metadata_json for event in events], sort_keys=True)
    for private in (
        ISSUER,
        "request-audit-review",
        "audit-review@example.test",
        "private operator rationale",
    ):
        assert private not in envelope


async def test_purge_scrubs_applicant_identifiers_and_keeps_replay_tombstone(
    db_session, monkeypatch
):
    actor, issued = await _issued(db_session, monkeypatch, slug="retention-invite")
    issued.invitation.issuance_request_digest = "a" * 64
    _age_invitation(issued.invitation, days=100)
    request = await _request(db_session, monkeypatch, suffix="retention-request")
    _age_request(request, days=100)
    await db_session.flush()

    maintenance_time = datetime.now(timezone.utc) - timedelta(days=60)
    expired = await admission.expire_due(db_session, now=maintenance_time, limit=1)
    assert expired.invitations == expired.requests == 1
    assert issued.invitation.status == RegistrationInvitationStatus.EXPIRED.value
    assert request.status == RegistrationRequestStatus.EXPIRED.value

    retention_time = datetime.now(timezone.utc)
    cutoff = retention_time - admission.MINIMUM_RETENTION
    purged = await admission.purge_terminal(
        db_session,
        before=cutoff,
        now=retention_time,
        limit=1,
    )
    assert purged.invitations == purged.requests == 1
    assert issued.invitation.token_digest is None
    assert issued.invitation.issuance_request_digest == "a" * 64
    assert issued.invitation.normalized_email is None
    assert issued.invitation.invited_by_user_id is None
    assert request.issuer is request.subject is None
    assert request.verified_email is request.normalized_verified_email is None
    assert request.preferred_username is None
    assert actor.id is not None  # actor account itself is retained


async def test_purge_requires_a_past_cutoff_and_never_scrubs_pending_rows(
    db_session, monkeypatch
):
    _actor, issued = await _issued(db_session, monkeypatch, slug="pending-retention")
    now = datetime.now(timezone.utc)
    with pytest.raises(admission.AdmissionValidationError):
        await admission.purge_terminal(db_session, before=now, now=now)
    result = await admission.purge_terminal(
        db_session,
        before=now - timedelta(days=365),
        now=now,
    )
    assert result.invitations == result.requests == 0
    assert issued.invitation.token_digest is not None
    assert issued.invitation.normalized_email is not None


async def test_default_retention_scrubs_at_ninety_days_not_before(
    db_session, monkeypatch
):
    _older_actor, older = await _issued(
        db_session,
        monkeypatch,
        slug="retention-boundary-older",
    )
    _newer_actor, newer = await _issued(
        db_session,
        monkeypatch,
        slug="retention-boundary-newer",
    )
    _age_invitation(older.invitation, days=200)
    _age_invitation(newer.invitation, days=200)
    now = datetime.now(timezone.utc)
    older.invitation.status = RegistrationInvitationStatus.EXPIRED.value
    older.invitation.expired_at = now - admission.DEFAULT_RETENTION
    newer.invitation.status = RegistrationInvitationStatus.EXPIRED.value
    newer.invitation.expired_at = now - admission.DEFAULT_RETENTION + timedelta(
        seconds=1
    )
    await db_session.flush()

    result = await admission.purge_terminal(db_session, now=now)

    assert result.invitations == 1
    assert older.invitation.purged_at == now
    assert newer.invitation.purged_at is None


def _factory(db_session) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )


async def test_scheduled_maintenance_commits_expiry_and_retention(
    db_session, monkeypatch
):
    _old_actor, old_invitation = await _issued(
        db_session,
        monkeypatch,
        slug="scheduled-old-invitation",
    )
    old_invitation.invitation.issuance_request_digest = "b" * 64
    _age_invitation(old_invitation.invitation, days=200)
    old_request = await _request(
        db_session,
        monkeypatch,
        suffix="scheduled-old-request",
    )
    _age_request(old_request, days=200)
    old_invitation_id = old_invitation.invitation.id
    old_request_id = old_request.id

    historical_run = datetime.now(timezone.utc) - timedelta(days=100)
    historical = await admission.expire_due(
        db_session,
        now=historical_run,
        limit=1,
    )
    assert historical.invitations == historical.requests == 1

    _due_actor, due_invitation = await _issued(
        db_session,
        monkeypatch,
        slug="scheduled-due-invitation",
    )
    _age_invitation(due_invitation.invitation, days=40)
    due_request = await _request(
        db_session,
        monkeypatch,
        suffix="scheduled-due-request",
    )
    _age_request(due_request, days=50)
    due_invitation_id = due_invitation.invitation.id
    due_request_id = due_request.id
    await db_session.commit()

    await admission.maintenance_job(_factory(db_session))

    db_session.expire_all()
    purged_invitation = await db_session.get(
        RegistrationInvitation,
        old_invitation_id,
    )
    purged_request = await db_session.get(RegistrationRequest, old_request_id)
    expired_invitation = await db_session.get(
        RegistrationInvitation,
        due_invitation_id,
    )
    expired_request = await db_session.get(RegistrationRequest, due_request_id)
    assert purged_invitation is not None
    assert purged_invitation.purged_at is not None
    assert purged_invitation.token_digest is None
    assert purged_invitation.normalized_email is None
    assert purged_invitation.issuance_request_digest == "b" * 64
    assert purged_request is not None and purged_request.purged_at is not None
    assert expired_invitation is not None
    assert expired_invitation.status == RegistrationInvitationStatus.EXPIRED.value
    assert expired_request is not None
    assert expired_request.status == RegistrationRequestStatus.EXPIRED.value


async def test_scheduled_maintenance_keeps_expiry_when_purge_fails(
    db_session, monkeypatch
):
    _actor, issued = await _issued(
        db_session,
        monkeypatch,
        slug="scheduled-expiry-before-purge-error",
    )
    _age_invitation(issued.invitation, days=40)
    invitation_id = issued.invitation.id
    await db_session.commit()

    async def fail_purge(*_args, **_kwargs):
        raise RuntimeError("synthetic purge failure")

    monkeypatch.setattr(retention_service, "purge_terminal", fail_purge)
    with pytest.raises(RuntimeError, match="synthetic purge failure"):
        await admission.maintenance_job(_factory(db_session))

    db_session.expire_all()
    row = await db_session.get(RegistrationInvitation, invitation_id)
    assert row is not None
    assert row.status == RegistrationInvitationStatus.EXPIRED.value


@pytest.mark.integration
@pytest.mark.parametrize("proof_kind", ["bearer", "signed_claim"])
async def test_postgres_double_consume_creates_exactly_one_account_graph(
    db_session, legacy_owner_roots, monkeypatch, proof_kind
):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row/advisory-lock semantics")
    slug = f"race-consume-{proof_kind}"
    _actor, issued = await _issued(db_session, monkeypatch, slug=slug)
    invitation_id = issued.invitation.id
    await db_session.commit()
    factory = _factory(db_session)

    async def worker(index: int):
        async with factory() as session:
            try:
                if proof_kind == "bearer":
                    result = await _consume_invitation(
                        session,
                        token=issued.token,
                        issuer=ISSUER,
                        subject=f"{slug}-{index}",
                        verified_email=f"{slug}@example.test",
                    )
                else:
                    result = await admission.consume_invitation_claim(
                        session,
                        invitation_id=invitation_id,
                        issuer=ISSUER,
                        subject=f"{slug}-{index}",
                        verified_email=f"{slug}@example.test",
                        email_verified=True,
                    )
                await session.commit()
                return result
            except admission.AdmissionRefused as exc:
                await session.rollback()
                return exc

    outcomes = await asyncio.wait_for(
        asyncio.gather(worker(1), worker(2)), timeout=10
    )
    assert sum(isinstance(item, admission.AdmissionResult) for item in outcomes) == 1
    assert sum(isinstance(item, admission.AdmissionRefused) for item in outcomes) == 1
    async with factory() as verify:
        assert await verify.scalar(select(func.count()).select_from(UserFederatedIdentity)) == 1
        assert await verify.scalar(
            select(func.count()).select_from(User).where(
                User.normalized_email == f"{slug}@example.test"
            )
        ) == 1


@pytest.mark.integration
async def test_postgres_revoke_and_consume_have_one_terminal_winner(
    db_session, legacy_owner_roots, monkeypatch
):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row/advisory-lock semantics")
    actor, issued = await _issued(db_session, monkeypatch, slug="race-revoke")
    invitation_id = issued.invitation.id
    actor_id = actor.id
    await db_session.commit()
    factory = _factory(db_session)

    async def revoke():
        async with factory() as session:
            try:
                row = await admission.revoke_invitation(
                    session, invitation_id=invitation_id, actor_user_id=actor_id
                )
                await session.commit()
                return row.status
            except admission.AdmissionStateError:
                await session.rollback()
                return "lost"

    async def consume():
        async with factory() as session:
            try:
                await _consume_invitation(
                    session,
                    token=issued.token,
                    issuer=ISSUER,
                    subject="race-revoke",
                    verified_email="race-revoke@example.test",
                )
                await session.commit()
                return RegistrationInvitationStatus.CONSUMED.value
            except admission.AdmissionRefused:
                await session.rollback()
                return "lost"

    outcomes = await asyncio.wait_for(asyncio.gather(revoke(), consume()), timeout=10)
    assert outcomes.count("lost") == 1
    async with factory() as verify:
        row = await verify.get(RegistrationInvitation, invitation_id)
        assert row.status in {
            RegistrationInvitationStatus.REVOKED.value,
            RegistrationInvitationStatus.CONSUMED.value,
        }
        assert await verify.scalar(
            select(func.count()).select_from(UserFederatedIdentity)
        ) == (1 if row.status == RegistrationInvitationStatus.CONSUMED.value else 0)


@pytest.mark.integration
@pytest.mark.parametrize("race", ["double_approve", "approve_reject"])
async def test_postgres_request_decisions_have_one_terminal_winner(
    db_session, legacy_owner_roots, monkeypatch, race
):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row/advisory-lock semantics")
    request = await _request(db_session, monkeypatch, suffix=f"race-{race}")
    first = await _admin(db_session, f"race-{race}-first")
    second = await _admin(db_session, f"race-{race}-second")
    request_id, first_id, second_id = request.id, first.id, second.id
    await db_session.commit()
    factory = _factory(db_session)

    async def decide(operation: str, reviewer_id: uuid.UUID):
        async with factory() as session:
            try:
                if operation == "approve":
                    await admission.approve_request(
                        session,
                        request_id=request_id,
                        reviewer_user_id=reviewer_id,
                    )
                else:
                    await admission.reject_request(
                        session,
                        request_id=request_id,
                        reviewer_user_id=reviewer_id,
                        reason="synthetic race rejection",
                    )
                await session.commit()
                return operation
            except admission.AdmissionStateError:
                await session.rollback()
                return "lost"

    operations = (
        ("approve", first_id),
        ("approve" if race == "double_approve" else "reject", second_id),
    )
    outcomes = await asyncio.wait_for(
        asyncio.gather(*(decide(*operation) for operation in operations)), timeout=10
    )
    assert outcomes.count("lost") == 1
    async with factory() as verify:
        row = await verify.get(RegistrationRequest, request_id)
        assert row.status in {
            RegistrationRequestStatus.APPROVED.value,
            RegistrationRequestStatus.REJECTED.value,
        }
        links = await verify.scalar(
            select(func.count()).select_from(UserFederatedIdentity)
        )
        assert links == (1 if row.status == RegistrationRequestStatus.APPROVED.value else 0)


@pytest.mark.integration
async def test_postgres_duplicate_request_submission_converges_on_one_row(
    db_session, monkeypatch
):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row/advisory-lock semantics")
    await _mode(
        db_session,
        monkeypatch,
        registration_policy.RegistrationMode.ADMIN_APPROVED,
    )
    await db_session.commit()
    factory = _factory(db_session)

    async def submit(index: int):
        async with factory() as session:
            row = await _submit_request(
                session,
                issuer=ISSUER,
                subject="duplicate-request-race",
                verified_email="duplicate-request@example.test",
                preferred_username=f"claim-{index}",
            )
            await session.commit()
            return row.id

    ids = await asyncio.wait_for(asyncio.gather(submit(1), submit(2)), timeout=10)
    assert ids[0] == ids[1]
    async with factory() as verify:
        assert await verify.scalar(
            select(func.count()).select_from(RegistrationRequest).where(
                RegistrationRequest.issuer == ISSUER,
                RegistrationRequest.subject == "duplicate-request-race",
            )
        ) == 1


@pytest.mark.integration
@pytest.mark.parametrize("flow", ["invitation", "request"])
async def test_postgres_expiry_uses_time_after_transaction_wait(
    db_session, legacy_owner_roots, monkeypatch, flow
):
    """A transaction opened before expiry cannot act after the deadline."""

    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL transaction timestamp semantics")

    ttl = timedelta(milliseconds=500)
    reviewer_id = None
    if flow == "invitation":
        await _mode(
            db_session,
            monkeypatch,
            registration_policy.RegistrationMode.INVITE_ONLY,
        )
        reviewer = await _admin(db_session, "deadline-invitation-admin")
        issued = await admission.issue_invitation(
            db_session,
            actor_user_id=reviewer.id,
            email="deadline-invitation@example.test",
            account_kind=RegistrationAccountKind.MEMBER,
            ttl=ttl,
        )
    else:
        await _mode(
            db_session,
            monkeypatch,
            registration_policy.RegistrationMode.ADMIN_APPROVED,
        )
        request = await _submit_request(
            db_session,
            issuer=ISSUER,
            subject="deadline-request",
            verified_email="deadline-request@example.test",
            ttl=ttl,
        )
        reviewer = await _admin(db_session, "deadline-request-admin")
        reviewer_id = reviewer.id
    await db_session.commit()

    factory = _factory(db_session)
    async with factory() as session:
        # PostgreSQL's now() is pinned here, before expiry. The admission check
        # must use the later statement clock rather than inheriting this value.
        await session.scalar(select(func.now()))
        await asyncio.sleep(0.7)

        if flow == "invitation":
            with pytest.raises(admission.AdmissionRefused):
                await _consume_invitation(
                    session,
                    token=issued.token,
                    issuer=ISSUER,
                    subject="deadline-invitation",
                    verified_email="deadline-invitation@example.test",
                )
        else:
            with pytest.raises(admission.AdmissionStateError):
                await admission.approve_request(
                    session,
                    request_id=request.id,
                    reviewer_user_id=reviewer_id,
                )
        assert not in_platform_scope(session)


@pytest.mark.integration
async def test_postgres_mode_close_fences_invitation_consumption(
    db_session, legacy_owner_roots, monkeypatch
):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory-lock semantics")
    _actor, issued = await _issued(db_session, monkeypatch, slug="race-close")
    invitation_id = issued.invitation.id
    await db_session.commit()
    factory = _factory(db_session)
    started = asyncio.Event()

    async def consume():
        async with factory() as session:
            started.set()
            try:
                await _consume_invitation(
                    session,
                    token=issued.token,
                    issuer=ISSUER,
                    subject="race-close",
                    verified_email="race-close@example.test",
                )
            except admission.AdmissionRefused:
                await session.rollback()
                return False
            await session.commit()
            return True

    async with factory() as closer:
        await registration_policy.set_stored_mode(
            closer, registration_policy.RegistrationMode.DISABLED
        )
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.sleep(0.2)
            assert not task.done()
            await closer.commit()
            assert await asyncio.wait_for(task, timeout=8) is False
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with factory() as verify:
        row = await verify.get(RegistrationInvitation, invitation_id)
        assert row.status == RegistrationInvitationStatus.PENDING.value
        assert await verify.scalar(
            select(func.count()).select_from(UserFederatedIdentity)
        ) == 0
