"""Pure policy tests for the framework-independent access context."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from vitals.access import (
    AccessContext,
    AccessRequest,
    AccessScope,
    PolicyAction,
    PolicyResourceType,
    Principal,
    RelationshipGrant,
    SupportGrant,
    is_allowed,
)
from vitals.enums import SupportAccessMode, SupportAccessStatus, UserRoleName


NOW = datetime(2026, 8, 19, 10, tzinfo=UTC)
OWNER_ID = UUID("00000000-0000-0000-0000-000000000001")
SUBJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
OTHER_SUBJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
PROFESSIONAL_ID = UUID("00000000-0000-0000-0000-000000000004")
ADMIN_ID = UUID("00000000-0000-0000-0000-000000000005")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000006")
RELATIONSHIP_ID = UUID("00000000-0000-0000-0000-000000000007")
CONSENT_ID = UUID("00000000-0000-0000-0000-000000000008")
SUPPORT_GRANT_ID = UUID("00000000-0000-0000-0000-000000000009")


def _request(
    *,
    subject_id: UUID = SUBJECT_ID,
    resource_type: PolicyResourceType = PolicyResourceType.DOMAIN,
    resource_key: str = "weight",
    action: PolicyAction = PolicyAction.READ,
) -> AccessRequest:
    return AccessRequest(
        subject_id=subject_id,
        resource_type=resource_type,
        resource_key=resource_key,
        action=action,
    )


def _scope(
    *,
    resource_type: PolicyResourceType = PolicyResourceType.DOMAIN,
    resource_key: str = "weight",
    action: PolicyAction = PolicyAction.READ,
) -> AccessScope:
    return AccessScope(
        resource_type=resource_type,
        resource_key=resource_key,
        action=action,
    )


def _principal(
    user_id: UUID,
    *roles: UserRoleName,
    session_version: int = 1,
) -> Principal:
    return Principal(
        user_id=user_id,
        roles=frozenset(roles),
        session_version=session_version,
    )


def _context(
    principal: Principal,
    *,
    subject_id: UUID = SUBJECT_ID,
    relationship_grant: RelationshipGrant | None = None,
    support_grant: SupportGrant | None = None,
    evaluated_at: datetime = NOW,
) -> AccessContext:
    return AccessContext(
        principal=principal,
        subject_id=subject_id,
        subject_owner_user_id=OWNER_ID,
        evaluated_at=evaluated_at,
        relationship_grant=relationship_grant,
        support_grant=support_grant,
    )


def _relationship_grant(
    *,
    professional_user_id: UUID = PROFESSIONAL_ID,
    subject_id: UUID = SUBJECT_ID,
    scopes: frozenset[AccessScope] | None = None,
    active: bool = True,
    expires_at: datetime = NOW + timedelta(hours=1),
    revoked_at: datetime | None = None,
) -> RelationshipGrant:
    return RelationshipGrant(
        relationship_id=RELATIONSHIP_ID,
        consent_grant_id=CONSENT_ID,
        professional_user_id=professional_user_id,
        subject_id=subject_id,
        consent_version=3,
        expires_at=expires_at,
        scopes=frozenset({_scope()}) if scopes is None else scopes,
        active=active,
        revoked_at=revoked_at,
    )


def _support_grant(
    *,
    granted_to_user_id: UUID = ADMIN_ID,
    subject_id: UUID = SUBJECT_ID,
    mode: SupportAccessMode = SupportAccessMode.READ,
    status: SupportAccessStatus = SupportAccessStatus.ACTIVE,
    scopes: frozenset[AccessScope] | None = None,
    expires_at: datetime = NOW + timedelta(minutes=30),
    revoked_at: datetime | None = None,
) -> SupportGrant:
    return SupportGrant(
        grant_id=SUPPORT_GRANT_ID,
        granted_to_user_id=granted_to_user_id,
        subject_id=subject_id,
        mode=mode,
        status=status,
        expires_at=expires_at,
        scopes=frozenset({_scope()}) if scopes is None else scopes,
        revoked_at=revoked_at,
    )


def test_access_values_are_immutable_and_collections_are_frozen():
    source_roles = {UserRoleName.MEMBER}
    principal = Principal(user_id=OWNER_ID, roles=source_roles)  # type: ignore[arg-type]
    context = _context(principal)

    source_roles.add(UserRoleName.PLATFORM_SUPERADMIN)

    assert principal.roles == frozenset({UserRoleName.MEMBER})
    with pytest.raises(FrozenInstanceError):
        principal.session_version = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        context.subject_id = OTHER_SUBJECT_ID  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "exception"),
    [
        ({"user_id": "not-a-uuid"}, TypeError),
        ({"user_id": OWNER_ID, "roles": frozenset({"doctor"})}, TypeError),
        ({"user_id": OWNER_ID, "session_version": 0}, ValueError),
        ({"user_id": OWNER_ID, "session_version": True}, TypeError),
    ],
)
def test_principal_rejects_untyped_or_invalid_identity_values(kwargs, exception):
    with pytest.raises(exception):
        Principal(**kwargs)


@pytest.mark.parametrize(
    "evaluated_at",
    [
        datetime(2026, 8, 19, 10),
        datetime(2026, 8, 19, 10, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_context_requires_explicit_aware_utc_evaluation_time(evaluated_at):
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        _context(_principal(OWNER_ID), evaluated_at=evaluated_at)


@pytest.mark.parametrize("resource_key", ["", "   ", "*", "labs.*"])
def test_requests_and_scopes_forbid_blank_or_wildcard_resource_keys(resource_key):
    with pytest.raises(ValueError):
        _request(resource_key=resource_key)
    with pytest.raises(ValueError):
        _scope(resource_key=resource_key)


def test_self_owner_is_allowed_for_the_complete_policy_vocabulary():
    context = _context(_principal(OWNER_ID))

    for resource_type in PolicyResourceType:
        for action in PolicyAction:
            assert is_allowed(
                context,
                _request(
                    resource_type=resource_type,
                    resource_key=f"synthetic-{resource_type.value}",
                    action=action,
                ),
            )


def test_request_for_a_subject_other_than_the_selected_subject_is_denied():
    context = _context(_principal(OWNER_ID))

    assert not is_allowed(context, _request(subject_id=OTHER_SUBJECT_ID))


@pytest.mark.parametrize(
    "roles",
    [
        (UserRoleName.DOCTOR,),
        (UserRoleName.TRAINER,),
        (UserRoleName.PLATFORM_SUPERADMIN,),
        (
            UserRoleName.DOCTOR,
            UserRoleName.TRAINER,
            UserRoleName.PLATFORM_SUPERADMIN,
        ),
    ],
)
def test_roles_alone_never_authorize_another_subjects_phi(roles):
    context = _context(_principal(OTHER_ACTOR_ID, *roles))

    assert not is_allowed(context, _request())


@pytest.mark.parametrize("role", [UserRoleName.DOCTOR, UserRoleName.TRAINER])
def test_exact_live_relationship_consent_scope_authorizes_professional(role):
    principal = _principal(PROFESSIONAL_ID, role)
    context = _context(
        principal,
        relationship_grant=_relationship_grant(),
    )

    assert is_allowed(context, _request())


@pytest.mark.parametrize(
    "access_request",
    [
        _request(resource_key="labs"),
        _request(resource_type=PolicyResourceType.ARTIFACT),
        _request(action=PolicyAction.EXPORT),
    ],
)
def test_relationship_scope_matching_is_exact(access_request):
    context = _context(
        _principal(PROFESSIONAL_ID, UserRoleName.DOCTOR),
        relationship_grant=_relationship_grant(),
    )

    assert not is_allowed(context, access_request)


@pytest.mark.parametrize(
    "grant",
    [
        _relationship_grant(active=False),
        _relationship_grant(expires_at=NOW),
        _relationship_grant(revoked_at=NOW - timedelta(minutes=1)),
        _relationship_grant(scopes=frozenset()),
        _relationship_grant(professional_user_id=OTHER_ACTOR_ID),
    ],
)
def test_inactive_expired_revoked_empty_or_wrong_actor_relationship_denies(grant):
    context = _context(
        _principal(PROFESSIONAL_ID, UserRoleName.DOCTOR),
        relationship_grant=grant,
    )

    assert not is_allowed(context, _request())


def test_relationship_grant_does_not_authorize_a_non_professional_role():
    context = _context(
        _principal(PROFESSIONAL_ID, UserRoleName.MEMBER),
        relationship_grant=_relationship_grant(),
    )

    assert not is_allowed(context, _request())


def test_exact_live_support_scope_authorizes_its_platform_admin():
    context = _context(
        _principal(ADMIN_ID, UserRoleName.PLATFORM_SUPERADMIN),
        support_grant=_support_grant(),
    )

    assert is_allowed(context, _request())


@pytest.mark.parametrize(
    "grant",
    [
        _support_grant(status=SupportAccessStatus.EXPIRED),
        _support_grant(status=SupportAccessStatus.REVOKED, revoked_at=NOW),
        _support_grant(expires_at=NOW),
        _support_grant(revoked_at=NOW - timedelta(minutes=1)),
        _support_grant(scopes=frozenset()),
        _support_grant(granted_to_user_id=OTHER_ACTOR_ID),
    ],
)
def test_inactive_expired_revoked_empty_or_wrong_actor_support_grant_denies(grant):
    context = _context(
        _principal(ADMIN_ID, UserRoleName.PLATFORM_SUPERADMIN),
        support_grant=grant,
    )

    assert not is_allowed(context, _request())


def test_support_grant_does_not_authorize_without_platform_admin_role():
    context = _context(
        _principal(ADMIN_ID, UserRoleName.MEMBER),
        support_grant=_support_grant(),
    )

    assert not is_allowed(context, _request())


def test_support_mode_does_not_expand_into_implicit_actions():
    repair_scope = _scope(action=PolicyAction.REPAIR)
    context = _context(
        _principal(ADMIN_ID, UserRoleName.PLATFORM_SUPERADMIN),
        support_grant=_support_grant(
            mode=SupportAccessMode.REPAIR,
            scopes=frozenset({repair_scope}),
        ),
    )

    assert is_allowed(context, _request(action=PolicyAction.REPAIR))
    assert not is_allowed(context, _request(action=PolicyAction.READ))
    assert not is_allowed(context, _request(action=PolicyAction.UPDATE))


def test_repair_mode_can_include_an_explicit_read_scope():
    context = _context(
        _principal(ADMIN_ID, UserRoleName.PLATFORM_SUPERADMIN),
        support_grant=_support_grant(
            mode=SupportAccessMode.REPAIR,
            scopes=frozenset({_scope(action=PolicyAction.READ)}),
        ),
    )

    assert is_allowed(context, _request(action=PolicyAction.READ))


def test_support_modes_do_not_cross_repair_and_export_purposes():
    export_scope = _scope(action=PolicyAction.EXPORT)
    repair_context = _context(
        _principal(ADMIN_ID, UserRoleName.PLATFORM_SUPERADMIN),
        support_grant=_support_grant(
            mode=SupportAccessMode.REPAIR,
            scopes=frozenset({export_scope}),
        ),
    )
    repair_scope = _scope(action=PolicyAction.REPAIR)
    export_context = _context(
        _principal(ADMIN_ID, UserRoleName.PLATFORM_SUPERADMIN),
        support_grant=_support_grant(
            mode=SupportAccessMode.EXPORT,
            scopes=frozenset({repair_scope}),
        ),
    )

    assert not is_allowed(repair_context, _request(action=PolicyAction.EXPORT))
    assert not is_allowed(export_context, _request(action=PolicyAction.REPAIR))


def test_support_scope_vocabulary_rejects_non_support_actions():
    with pytest.raises(ValueError, match="read, repair, or export"):
        _support_grant(scopes=frozenset({_scope(action=PolicyAction.UPDATE)}))


@pytest.mark.parametrize("grant_type", ["relationship", "support"])
def test_context_rejects_a_grant_for_a_different_subject(grant_type):
    kwargs = (
        {"relationship_grant": _relationship_grant(subject_id=OTHER_SUBJECT_ID)}
        if grant_type == "relationship"
        else {"support_grant": _support_grant(subject_id=OTHER_SUBJECT_ID)}
    )

    with pytest.raises(ValueError, match="selected subject"):
        _context(_principal(OTHER_ACTOR_ID), **kwargs)
