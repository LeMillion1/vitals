"""Framework-independent subject access context and policy vocabulary.

This module is deliberately pure: callers resolve identities, ownership,
relationships, consent, and support grants before constructing an
:class:`AccessContext`. Policy evaluation performs no I/O and never imports the
web delivery layer.

Roles describe capabilities but are not patient-data grants. In particular, a
doctor, trainer, or platform superadmin role alone never authorizes access to a
different health subject. Cross-subject access needs one live, actor-bound grant
and one exact resource/action scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from vitals.enums import SupportAccessMode, SupportAccessStatus, UserRoleName


class PolicyAction(StrEnum):
    """Stable operations understood by subject-aware policy boundaries."""

    READ = "read"
    LIST = "list"
    SEARCH = "search"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    ATTACH = "attach"
    SHARE = "share"
    EXPORT = "export"
    SYNC = "sync"
    MESSAGE = "message"
    REPAIR = "repair"


class PolicyResourceType(StrEnum):
    """Shape of the concrete, subject-scoped resource key in a policy request."""

    DOMAIN = "domain"
    ARTIFACT = "artifact"
    OPERATION = "operation"


def _require_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")


def _require_aware_utc(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    offset = value.utcoffset()
    if offset is None or offset != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


def _require_resource_key(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("resource_key must be a string")
    if not value.strip():
        raise ValueError("resource_key must not be blank")
    if len(value) > 128:
        raise ValueError("resource_key must be at most 128 characters")
    if "*" in value:
        raise ValueError("resource_key must be concrete; wildcards are forbidden")


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated application identity, independent of an HTTP credential."""

    user_id: UUID
    roles: frozenset[UserRoleName] = field(default_factory=frozenset)
    session_version: int = 1

    def __post_init__(self) -> None:
        _require_uuid(self.user_id, "user_id")
        roles = frozenset(self.roles)
        if any(not isinstance(role, UserRoleName) for role in roles):
            raise TypeError("roles must contain only UserRoleName values")
        if isinstance(self.session_version, bool) or not isinstance(
            self.session_version, int
        ):
            raise TypeError("session_version must be an integer")
        if self.session_version < 1:
            raise ValueError("session_version must be at least 1")
        object.__setattr__(self, "roles", roles)


@dataclass(frozen=True, slots=True)
class AccessScope:
    """One exact resource/action capability; absence of rows authorizes nothing."""

    resource_type: PolicyResourceType
    resource_key: str
    action: PolicyAction

    def __post_init__(self) -> None:
        if not isinstance(self.resource_type, PolicyResourceType):
            raise TypeError("resource_type must be a PolicyResourceType")
        _require_resource_key(self.resource_key)
        if not isinstance(self.action, PolicyAction):
            raise TypeError("action must be a PolicyAction")


@dataclass(frozen=True, slots=True)
class AccessRequest:
    """A single policy question against one selected health subject."""

    subject_id: UUID
    resource_type: PolicyResourceType
    resource_key: str
    action: PolicyAction

    def __post_init__(self) -> None:
        _require_uuid(self.subject_id, "subject_id")
        if not isinstance(self.resource_type, PolicyResourceType):
            raise TypeError("resource_type must be a PolicyResourceType")
        _require_resource_key(self.resource_key)
        if not isinstance(self.action, PolicyAction):
            raise TypeError("action must be a PolicyAction")

    @property
    def scope(self) -> AccessScope:
        """Return the exact normalized scope needed to satisfy this request."""

        return AccessScope(
            resource_type=self.resource_type,
            resource_key=self.resource_key,
            action=self.action,
        )


def _freeze_scopes(scopes: object) -> frozenset[AccessScope]:
    try:
        frozen = frozenset(scopes)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError("scopes must be an iterable of AccessScope values") from exc
    if any(not isinstance(scope, AccessScope) for scope in frozen):
        raise TypeError("scopes must contain only AccessScope values")
    return frozen


@dataclass(frozen=True, slots=True)
class RelationshipGrant:
    """Resolved active-care relationship and its versioned consent snapshot.

    A loader must invalidate/rebuild this value when relationship or consent state
    changes. The pure policy still verifies actor, subject, lifecycle, expiry, and
    the exact requested scope on every decision.
    """

    relationship_id: UUID
    consent_grant_id: UUID
    professional_user_id: UUID
    subject_id: UUID
    consent_version: int
    expires_at: datetime
    scopes: frozenset[AccessScope] = field(default_factory=frozenset)
    active: bool = True
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.relationship_id, "relationship_id")
        _require_uuid(self.consent_grant_id, "consent_grant_id")
        _require_uuid(self.professional_user_id, "professional_user_id")
        _require_uuid(self.subject_id, "subject_id")
        if isinstance(self.consent_version, bool) or not isinstance(
            self.consent_version, int
        ):
            raise TypeError("consent_version must be an integer")
        if self.consent_version < 1:
            raise ValueError("consent_version must be at least 1")
        _require_aware_utc(self.expires_at, "expires_at")
        if not isinstance(self.active, bool):
            raise TypeError("active must be a boolean")
        if self.revoked_at is not None:
            _require_aware_utc(self.revoked_at, "revoked_at")
        object.__setattr__(self, "scopes", _freeze_scopes(self.scopes))


_SUPPORT_SCOPE_ACTIONS = frozenset(
    PolicyAction(mode.value) for mode in SupportAccessMode
)
_SUPPORT_MODE_ACTIONS = {
    SupportAccessMode.READ: frozenset({PolicyAction.READ}),
    SupportAccessMode.REPAIR: frozenset({PolicyAction.READ, PolicyAction.REPAIR}),
    SupportAccessMode.EXPORT: frozenset({PolicyAction.READ, PolicyAction.EXPORT}),
}


@dataclass(frozen=True, slots=True)
class SupportGrant:
    """Resolved support-access snapshot for one admin and one health subject."""

    grant_id: UUID
    granted_to_user_id: UUID
    subject_id: UUID
    mode: SupportAccessMode
    status: SupportAccessStatus
    expires_at: datetime
    scopes: frozenset[AccessScope] = field(default_factory=frozenset)
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.grant_id, "grant_id")
        _require_uuid(self.granted_to_user_id, "granted_to_user_id")
        _require_uuid(self.subject_id, "subject_id")
        if not isinstance(self.mode, SupportAccessMode):
            raise TypeError("mode must be a SupportAccessMode")
        if not isinstance(self.status, SupportAccessStatus):
            raise TypeError("status must be a SupportAccessStatus")
        _require_aware_utc(self.expires_at, "expires_at")
        if self.revoked_at is not None:
            _require_aware_utc(self.revoked_at, "revoked_at")
        scopes = _freeze_scopes(self.scopes)
        if any(scope.action not in _SUPPORT_SCOPE_ACTIONS for scope in scopes):
            raise ValueError("support scopes may use only read, repair, or export")
        object.__setattr__(self, "scopes", scopes)


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Complete, immutable input to one subject-scoped policy evaluation."""

    principal: Principal
    subject_id: UUID
    subject_owner_user_id: UUID
    evaluated_at: datetime
    relationship_grant: RelationshipGrant | None = None
    support_grant: SupportGrant | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be a Principal")
        _require_uuid(self.subject_id, "subject_id")
        _require_uuid(self.subject_owner_user_id, "subject_owner_user_id")
        _require_aware_utc(self.evaluated_at, "evaluated_at")
        if self.relationship_grant is not None:
            if not isinstance(self.relationship_grant, RelationshipGrant):
                raise TypeError("relationship_grant must be a RelationshipGrant")
            if self.relationship_grant.subject_id != self.subject_id:
                raise ValueError("relationship_grant must match the selected subject")
        if self.support_grant is not None:
            if not isinstance(self.support_grant, SupportGrant):
                raise TypeError("support_grant must be a SupportGrant")
            if self.support_grant.subject_id != self.subject_id:
                raise ValueError("support_grant must match the selected subject")


_PROFESSIONAL_ROLES = frozenset(
    {UserRoleName.DOCTOR, UserRoleName.TRAINER}
)


def _relationship_allows(context: AccessContext, request: AccessRequest) -> bool:
    grant = context.relationship_grant
    if grant is None:
        return False
    if not context.principal.roles.intersection(_PROFESSIONAL_ROLES):
        return False
    if grant.professional_user_id != context.principal.user_id:
        return False
    if not grant.active or grant.revoked_at is not None:
        return False
    if context.evaluated_at >= grant.expires_at:
        return False
    return request.scope in grant.scopes


def _support_allows(context: AccessContext, request: AccessRequest) -> bool:
    grant = context.support_grant
    if grant is None:
        return False
    if UserRoleName.PLATFORM_SUPERADMIN not in context.principal.roles:
        return False
    if grant.granted_to_user_id != context.principal.user_id:
        return False
    if grant.status is not SupportAccessStatus.ACTIVE or grant.revoked_at is not None:
        return False
    if context.evaluated_at >= grant.expires_at:
        return False
    # Mode is a ceiling/purpose, never a grant by itself. Repair and exceptional
    # export may include explicitly enumerated reads, but neither implies them:
    # the exact resource/action scope below remains mandatory.
    if request.action not in _SUPPORT_MODE_ACTIONS[grant.mode]:
        return False
    return request.scope in grant.scopes


def is_allowed(context: AccessContext, request: AccessRequest) -> bool:
    """Return whether ``request`` is authorized by this immutable snapshot.

    The selected subject is checked before any ownership or grant logic. Self
    ownership is an authorization basis in its own right. Every cross-subject
    path is deny-by-default and requires an exact, actor-bound live grant.
    """

    if not isinstance(context, AccessContext):
        raise TypeError("context must be an AccessContext")
    if not isinstance(request, AccessRequest):
        raise TypeError("request must be an AccessRequest")
    if request.subject_id != context.subject_id:
        return False
    if context.principal.user_id == context.subject_owner_user_id:
        return True
    return _relationship_allows(context, request) or _support_allows(context, request)


__all__ = [
    "AccessContext",
    "AccessRequest",
    "AccessScope",
    "PolicyAction",
    "PolicyResourceType",
    "Principal",
    "RelationshipGrant",
    "SupportGrant",
    "is_allowed",
]
