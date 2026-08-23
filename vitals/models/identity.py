"""Identity, health-subject ownership, scoped support access, and audit facts.

This module is deliberately disconnected from the current single-user web auth.
It establishes durable identities and authorization primitives without opening
registration or changing who can sign in.

Two boundaries are intentional:

* additive roles express product capabilities, never PHI access;
* support access exists only when a grant is active, not expired or revoked, and
  has a matching explicit scope. A grant with no scope rows authorizes nothing.

Audit metadata is an allowlisted *operational* envelope. It may contain only
opaque identifiers, result/reason codes, field names, scope keys, and counts.
Passwords, tokens, prompts, raw payloads, notes, health values, diagnoses, and
other secrets/PHI must never be placed in ``AuditEvent.metadata_json``.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from vitals.enums import (
    AuditOutcome,
    SupportAccessMode,
    SupportAccessStatus,
    SupportScopeResourceType,
    UserRoleName,
    UserStatus,
)
from vitals.models.base import Base

_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


def _values(enum_type: type) -> str:
    """Render a stable SQL ``IN`` value list for string-backed enums."""

    return ", ".join(f"'{member.value}'" for member in enum_type)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class User(Base):
    """Application identity prepared for the later auth migration.

    The current environment-backed owner is not wired to this table in PR-01.
    ``normalized_*`` values are the unique lookup keys; the future auth service
    is responsible for producing their canonical representation.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "normalized_username", name="uq_users_normalized_username"
        ),
        UniqueConstraint("normalized_email", name="uq_users_normalized_email"),
        CheckConstraint(
            "length(trim(username)) > 0", name="ck_users_username_not_blank"
        ),
        CheckConstraint(
            "length(trim(normalized_username)) > 0",
            name="ck_users_normalized_username_not_blank",
        ),
        CheckConstraint(
            "(email IS NULL AND normalized_email IS NULL) OR "
            "(email IS NOT NULL AND normalized_email IS NOT NULL)",
            name="ck_users_email_normalized_pair",
        ),
        CheckConstraint(
            "email IS NULL OR length(trim(email)) > 0",
            name="ck_users_email_not_blank",
        ),
        CheckConstraint(
            "normalized_email IS NULL OR length(trim(normalized_email)) > 0",
            name="ck_users_normalized_email_not_blank",
        ),
        CheckConstraint(
            "password_hash IS NULL OR length(trim(password_hash)) > 0",
            name="ck_users_password_hash_not_blank",
        ),
        CheckConstraint(
            f"status IN ({_values(UserStatus)})", name="ck_users_status"
        ),
        CheckConstraint(
            "session_version >= 1", name="ck_users_session_version_positive"
        ),
        Index("ix_users_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_username: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    normalized_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    # NULL for everyone the identity provider authenticates, which after the
    # OIDC cutover is everyone. Password material is the provider's to hold —
    # hashing, reset, breach response and rotation are all things it already
    # does properly, and a copy here would be a second thing to get right. The
    # column survives only for the pre-cutover owner's migrated bcrypt hash.
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=UserStatus.PENDING.value
    )
    session_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    roles: Mapped[list["UserRole"]] = relationship(
        back_populates="user",
        foreign_keys="UserRole.user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    owned_subject: Mapped[Optional["HealthSubject"]] = relationship(
        back_populates="owner", uselist=False, foreign_keys="HealthSubject.owner_user_id"
    )


class UserFederatedIdentity(Base):
    """One provider-authenticated identity, bound to a local user.

    The pair the provider guarantees is ``(issuer, subject)``: an opaque subject
    inside the issuer's namespace, immutable for the life of the account. That
    pair is the identity key and nothing else is. Email and display name arrive
    in the same token and are deliberately not used for lookup — a provider may
    let a person change either, and matching on them would hand one account to
    whoever claimed the address next.

    Its own table rather than columns on ``users`` because the relationship is
    one-to-many in principle: the same person may later be reachable through a
    second issuer, and adding one is then a row rather than a migration.
    """

    __tablename__ = "user_federated_identities"
    __table_args__ = (
        UniqueConstraint(
            "issuer", "subject", name="uq_user_federated_identities_issuer_subject"
        ),
        CheckConstraint(
            "length(trim(issuer)) > 0",
            name="ck_user_federated_identities_issuer_not_blank",
        ),
        CheckConstraint(
            "length(trim(subject)) > 0",
            name="ck_user_federated_identities_subject_not_blank",
        ),
        Index("ix_user_federated_identities_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The provider's ``iss`` claim, exactly as issued. Compared verbatim.
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The provider's ``sub`` claim — opaque, and never a username or address.
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    #: When this local user was first bound to that pair.
    linked_at: Mapped[datetime] = _created_at()
    #: The most recent ``auth_time`` the provider reported, which is what a
    #: step-up check measures freshness against.
    last_authenticated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    user: Mapped["User"] = relationship(foreign_keys=[user_id])


class UserRole(Base):
    """One additive role assignment; rows do not confer subject-data access."""

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role", name="uq_user_roles_user_role"),
        CheckConstraint(
            f"role IN ({_values(UserRoleName)})", name="ck_user_roles_role"
        ),
        Index("ix_user_roles_role", "role"),
        Index("ix_user_roles_assigned_by_user_id", "assigned_by_user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # Null means a bootstrap/system assignment. Human assignments retain their
    # assigner and therefore restrict deleting that identity.
    assigned_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    assigned_at: Mapped[datetime] = _created_at()

    user: Mapped[User] = relationship(
        back_populates="roles", foreign_keys=[user_id]
    )
    assigned_by: Mapped[Optional[User]] = relationship(
        foreign_keys=[assigned_by_user_id]
    )


class HealthSubject(Base):
    """Stable owner boundary to which health facts will be attached later."""

    __tablename__ = "health_subjects"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_health_subjects_owner_user_id"),
        CheckConstraint(
            "length(trim(timezone)) > 0", name="ck_health_subjects_timezone_not_blank"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    display_name: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    # IANA identifier (for example ``Asia/Almaty``); service validation is added
    # with the future profile write path.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    owner: Mapped[User] = relationship(
        back_populates="owned_subject", foreign_keys=[owner_user_id]
    )
    support_grants: Mapped[list["SupportAccessGrant"]] = relationship(
        back_populates="subject", foreign_keys="SupportAccessGrant.subject_id"
    )


class SupportAccessGrant(Base):
    """Time-limited, owner-approved platform-support access to one subject.

    Effective authorization must check all of the following: ``status`` is
    active, ``revoked_at`` is null, current UTC time is before ``expires_at``,
    and a matching :class:`SupportAccessScope` exists. The grantee's role alone
    is never sufficient.
    """

    __tablename__ = "support_access_grants"
    __table_args__ = (
        CheckConstraint(
            f"mode IN ({_values(SupportAccessMode)})",
            name="ck_support_access_grants_mode",
        ),
        CheckConstraint(
            f"status IN ({_values(SupportAccessStatus)})",
            name="ck_support_access_grants_status",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_support_access_grants_reason_not_blank",
        ),
        CheckConstraint(
            "length(reason) <= 2000", name="ck_support_access_grants_reason_length"
        ),
        CheckConstraint(
            "revocation_reason IS NULL OR length(revocation_reason) <= 2000",
            name="ck_support_access_grants_revocation_reason_length",
        ),
        CheckConstraint(
            "expires_at > approved_at",
            name="ck_support_access_grants_positive_ttl",
        ),
        CheckConstraint(
            "granted_to_user_id <> approved_by_user_id",
            name="ck_support_access_grants_no_self_approval",
        ),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL "
            "AND revocation_reason IS NOT NULL "
            "AND length(trim(revocation_reason)) > 0) OR "
            "(status <> 'revoked' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL AND revocation_reason IS NULL)",
            name="ck_support_access_grants_revocation_state",
        ),
        Index(
            "ix_support_access_grants_subject_status_expires",
            "subject_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_support_access_grants_grantee_status_expires",
            "granted_to_user_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_support_access_grants_approved_by_user_id", "approved_by_user_id"
        ),
        Index(
            "ix_support_access_grants_revoked_by_user_id", "revoked_by_user_id"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    granted_to_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=SupportAccessStatus.ACTIVE.value
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    revocation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    subject: Mapped[HealthSubject] = relationship(
        back_populates="support_grants", foreign_keys=[subject_id]
    )
    granted_to: Mapped[User] = relationship(foreign_keys=[granted_to_user_id])
    approved_by: Mapped[User] = relationship(foreign_keys=[approved_by_user_id])
    revoked_by: Mapped[Optional[User]] = relationship(
        foreign_keys=[revoked_by_user_id]
    )
    scopes: Mapped[list["SupportAccessScope"]] = relationship(
        back_populates="grant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SupportAccessScope(Base):
    """One explicit resource/action pair within a support grant.

    Wildcards are forbidden: broad access is represented by enumerating the
    concrete catalog keys. ``action`` is still checked against the parent grant's
    maximum ``mode`` by the future authorization service.
    """

    __tablename__ = "support_access_scopes"
    __table_args__ = (
        UniqueConstraint(
            "grant_id",
            "resource_type",
            "resource_key",
            "action",
            name="uq_support_access_scopes_grant_resource_action",
        ),
        CheckConstraint(
            f"resource_type IN ({_values(SupportScopeResourceType)})",
            name="ck_support_access_scopes_resource_type",
        ),
        CheckConstraint(
            f"action IN ({_values(SupportAccessMode)})",
            name="ck_support_access_scopes_action",
        ),
        CheckConstraint(
            "length(trim(resource_key)) > 0",
            name="ck_support_access_scopes_resource_key_not_blank",
        ),
        CheckConstraint(
            "resource_key NOT LIKE '%*%'",
            name="ck_support_access_scopes_no_wildcard",
        ),
        Index(
            "ix_support_access_scopes_resource",
            "resource_type",
            "resource_key",
            "action",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    grant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("support_access_grants.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    grant: Mapped[SupportAccessGrant] = relationship(
        back_populates="scopes", foreign_keys=[grant_id]
    )


AUDIT_METADATA_ALLOWED_KEYS = frozenset(
    {
        "request_id",
        "correlation_id",
        "source_surface",
        "result_code",
        "reason_code",
        "resource_type",
        "resource_id",
        "changed_fields",
        "scope_keys",
        "record_count",
        "grant_mode",
    }
)
_AUDIT_METADATA_LIST_KEYS = frozenset({"changed_fields", "scope_keys"})
_AUDIT_METADATA_INT_KEYS = frozenset({"record_count"})


class AuditEvent(Base):
    """Append-only operational security event.

    Mapper guards reject ORM updates/deletes. Direct SQL hardening belongs in a
    later database-privilege/trigger PR; PR-01 does not claim protection against
    a database owner issuing arbitrary SQL.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "length(trim(event_type)) > 0", name="ck_audit_events_event_type_not_blank"
        ),
        CheckConstraint(
            f"outcome IN ({_values(AuditOutcome)})",
            name="ck_audit_events_outcome",
        ),
        CheckConstraint(
            "resource_type IS NULL OR length(trim(resource_type)) > 0",
            name="ck_audit_events_resource_type_not_blank",
        ),
        CheckConstraint(
            "resource_id IS NULL OR length(trim(resource_id)) > 0",
            name="ck_audit_events_resource_id_not_blank",
        ),
        Index("ix_audit_events_occurred_at", "occurred_at"),
        Index("ix_audit_events_actor_occurred", "actor_user_id", "occurred_at"),
        Index("ix_audit_events_subject_occurred", "subject_id", "occurred_at"),
        Index(
            "ix_audit_events_grant_occurred",
            "support_access_grant_id",
            "occurred_at",
        ),
        Index("ix_audit_events_type_occurred", "event_type", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    occurred_at: Mapped[datetime] = _created_at()
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    subject_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=True,
    )
    support_access_grant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("support_access_grants.id", ondelete="RESTRICT"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Opaque identifier only; never a display value or medical datum.
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        _JSON_TYPE,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )

    actor: Mapped[Optional[User]] = relationship(foreign_keys=[actor_user_id])
    subject: Mapped[Optional[HealthSubject]] = relationship(foreign_keys=[subject_id])
    support_access_grant: Mapped[Optional[SupportAccessGrant]] = relationship(
        foreign_keys=[support_access_grant_id]
    )

    @validates("metadata_json")
    def _validate_metadata_json(self, _key: str, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("audit metadata_json must be an object")
        unknown = set(value) - AUDIT_METADATA_ALLOWED_KEYS
        if unknown:
            raise ValueError(
                "audit metadata_json contains non-operational keys: "
                + ", ".join(sorted(str(key) for key in unknown))
            )
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if key in _AUDIT_METADATA_LIST_KEYS:
                if not isinstance(item, list) or not all(
                    isinstance(entry, str) and 0 < len(entry) <= 128
                    for entry in item
                ):
                    raise ValueError(f"audit metadata_json[{key!r}] must be short strings")
                clean[key] = list(item)
            elif key in _AUDIT_METADATA_INT_KEYS:
                if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                    raise ValueError(
                        f"audit metadata_json[{key!r}] must be a non-negative integer"
                    )
                clean[key] = item
            else:
                if not isinstance(item, str) or not 0 < len(item) <= 256:
                    raise ValueError(f"audit metadata_json[{key!r}] must be a short code")
                clean[key] = item
        return clean


def _reject_audit_mutation(_mapper: Any, _connection: Any, _target: AuditEvent) -> None:
    raise ValueError("audit_events are append-only")


event.listen(AuditEvent, "before_update", _reject_audit_mutation)
event.listen(AuditEvent, "before_delete", _reject_audit_mutation)
