"""Idempotent runtime bootstrap of the legacy environment-backed owner.

This is a transitional bridge, not a second authentication implementation.
The existing bcrypt hash is copied verbatim into the identity foundation while
the current web login remains environment-backed.  Any identity or credential
mismatch fails closed rather than silently minting or modifying an admin.

The function mutates and flushes only.  Startup must commit or roll back in a
short, dedicated transaction before catalog seeding and scheduler startup.
"""
from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import AuditOutcome, UserRoleName, UserStatus
from vitals.models.identity import AuditEvent, HealthSubject, User, UserRole
from vitals.persistence.rls import bind_session_subject
from vitals.services.identity_service import (
    acquire_identity_governance_lock,
    bcrypt_cost,
    normalize_username,
)

_MAX_TIMEZONE_LENGTH = 64
_BOOTSTRAP_ROLES = (
    UserRoleName.MEMBER,
    UserRoleName.PLATFORM_SUPERADMIN,
)


class LegacyOwnerBootstrapError(RuntimeError):
    """Base class for a fail-closed legacy-owner bootstrap failure."""


class LegacyOwnerConfigurationError(ValueError):
    """Legacy-owner configuration is missing or cannot be represented."""


class LegacyOwnerIdentityMismatchError(LegacyOwnerBootstrapError):
    """Existing identity rows do not unambiguously match the configured owner."""


class LegacyOwnerCredentialMismatchError(LegacyOwnerBootstrapError):
    """The configured bcrypt hash differs from the persisted owner hash."""


class LegacyOwnerStateMismatchError(LegacyOwnerBootstrapError):
    """An existing owner is not active and must not be reactivated implicitly."""


@dataclass(frozen=True, slots=True)
class LegacyOwnerBootstrapResult:
    user_id: uuid.UUID
    subject_id: uuid.UUID
    user_created: bool
    subject_created: bool
    roles_added: frozenset[UserRoleName]
    timezone_updated: bool
    display_name_repaired: bool
    audit_event_id: uuid.UUID | None

    @property
    def changed(self) -> bool:
        return self.audit_event_id is not None


def validate_timezone(value: str) -> str:
    """Return a stripped, valid IANA timezone name."""

    if not isinstance(value, str):
        raise LegacyOwnerConfigurationError("timezone must be a string")
    timezone = value.strip()
    if not timezone:
        raise LegacyOwnerConfigurationError("timezone must not be blank")
    if len(timezone) > _MAX_TIMEZONE_LENGTH:
        raise LegacyOwnerConfigurationError("timezone is too long")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise LegacyOwnerConfigurationError(
            f"timezone {timezone!r} is not an installed IANA zone"
        ) from exc
    return timezone


async def bootstrap_legacy_owner(
    session: AsyncSession,
    *,
    username: str,
    password_hash: str,
    timezone: str,
) -> LegacyOwnerBootstrapResult:
    """Create or repair the one configured legacy owner, idempotently.

    Existing users are accepted only by their canonical lookup key, exact bcrypt
    hash, and active status.  A non-empty database with no canonical match is an
    operator-visible mismatch, not permission to create another superadmin.
    """

    try:
        normalized = normalize_username(username)
        bcrypt_cost(password_hash)
    except ValueError as exc:
        raise LegacyOwnerConfigurationError(str(exc)) from exc
    owner_timezone = validate_timezone(timezone)

    await acquire_identity_governance_lock(session)
    user = await session.scalar(
        select(User)
        .where(User.normalized_username == normalized.lookup_key)
        .with_for_update()
    )

    changed_fields: list[str] = []
    user_created = False
    if user is None:
        any_user_id = await session.scalar(select(User.id).limit(1))
        if any_user_id is not None:
            raise LegacyOwnerIdentityMismatchError(
                "configured legacy owner does not match the non-empty users table"
            )
        user = User(
            username=normalized.display,
            normalized_username=normalized.lookup_key,
            email=None,
            normalized_email=None,
            password_hash=password_hash,
            status=UserStatus.ACTIVE.value,
            session_version=1,
        )
        session.add(user)
        await session.flush()
        user_created = True
        changed_fields.append("user")
    else:
        if not hmac.compare_digest(user.password_hash, password_hash):
            raise LegacyOwnerCredentialMismatchError(
                "configured legacy owner hash differs from persisted identity"
            )
        if user.status != UserStatus.ACTIVE.value:
            raise LegacyOwnerStateMismatchError(
                "persisted legacy owner is not active; explicit recovery is required"
            )

    existing_assignments = list(
        await session.scalars(
            select(UserRole)
            .where(UserRole.user_id == user.id)
            .with_for_update()
        )
    )
    existing_roles = {assignment.role for assignment in existing_assignments}
    roles_added: set[UserRoleName] = set()
    for role in _BOOTSTRAP_ROLES:
        if role.value in existing_roles:
            continue
        session.add(
            UserRole(
                user_id=user.id,
                role=role.value,
                assigned_by_user_id=None,
            )
        )
        roles_added.add(role)
        changed_fields.append(f"roles.{role.value}")

    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.owner_user_id == user.id)
        .with_for_update()
    )
    subject_created = False
    timezone_updated = False
    display_name_repaired = False
    if subject is None:
        subject = HealthSubject(
            owner_user_id=user.id,
            display_name=user.username,
            timezone=owner_timezone,
        )
        session.add(subject)
        subject_created = True
        changed_fields.append("health_subject")
    else:
        # During PR-02 the environment remains the profile source of truth.  This
        # one-way mirror must be removed when subject profile writes move to DB.
        if subject.timezone != owner_timezone:
            subject.timezone = owner_timezone
            timezone_updated = True
            changed_fields.append("health_subject.timezone")
        if subject.display_name is None:
            subject.display_name = user.username
            display_name_repaired = True
            changed_fields.append("health_subject.display_name")

    await session.flush()

    # Identity roots are deliberately outside subject RLS, so the exact legacy
    # owner can be discovered or created while the session is unbound. From
    # this point onward every mutable row belongs to that one record: bind
    # before writing the subject-owned audit event and leave the caller bound
    # for resource-root/settings bootstrap in the same transaction.
    await bind_session_subject(session, subject.id)

    audit_event_id: uuid.UUID | None = None
    if changed_fields:
        audit_event = AuditEvent(
            actor_user_id=None,
            subject_id=subject.id,
            event_type="identity.legacy_owner.bootstrap",
            outcome=AuditOutcome.SUCCESS.value,
            resource_type="user",
            resource_id=str(user.id),
            metadata_json={
                "source_surface": "startup",
                "result_code": "created" if user_created else "repaired",
                "changed_fields": changed_fields,
            },
        )
        session.add(audit_event)
        await session.flush()
        audit_event_id = audit_event.id

    return LegacyOwnerBootstrapResult(
        user_id=user.id,
        subject_id=subject.id,
        user_created=user_created,
        subject_created=subject_created,
        roles_added=frozenset(roles_added),
        timezone_updated=timezone_updated,
        display_name_repaired=display_name_repaired,
        audit_event_id=audit_event_id,
    )


__all__ = [
    "LegacyOwnerBootstrapError",
    "LegacyOwnerBootstrapResult",
    "LegacyOwnerConfigurationError",
    "LegacyOwnerCredentialMismatchError",
    "LegacyOwnerIdentityMismatchError",
    "LegacyOwnerStateMismatchError",
    "bootstrap_legacy_owner",
    "validate_timezone",
]
