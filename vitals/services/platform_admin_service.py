"""Platform-control authorization without granting health-data access.

The platform-superadmin role may configure installation-wide providers and
quotas.  It is deliberately resolved independently from ``HealthSubject`` and
``AccessContext``: a successful check authorizes only the named control-plane
operation and never a PHI read or model invocation.

Configuration currently has one unavoidable compatibility seam: the secret is
written to the environment-backed file while the database transaction records
the audit event.  The shared governance lock is held across that short local
write so a concurrent role suspension/revocation cannot race authorization.
The caller owns commit/rollback and must not perform network I/O while holding
the returned capability.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import AuditOutcome, UserRoleName, UserStatus
from vitals.models.identity import AuditEvent, User, UserRole
from vitals.services.identity_service import (
    acquire_identity_governance_lock,
    normalize_username,
)

_OPENROUTER_CHANGED_FIELDS = frozenset(
    {
        "credential_ref",
        "base_url",
        "digest_model",
        "parser_model",
        "brief_model",
    }
)


class PlatformAdminError(RuntimeError):
    """Base class for platform-control authorization failures."""


class PlatformAdminAuthorizationError(PlatformAdminError):
    """The authenticated identity is not an active platform superadmin."""


class PlatformAdminCapabilityError(PlatformAdminError):
    """A platform-control mutation lacks a live service-issued capability."""


class PlatformAdminValidationError(ValueError):
    """Platform-control audit input is not in the reviewed vocabulary."""


class PreparedPlatformAdmin:
    """Opaque role proof bound to one session/root transaction/savepoint."""

    __slots__ = (
        "_fingerprint",
        "_nested_transaction",
        "_seal",
        "_session",
        "_transaction",
        "_user_id",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise PlatformAdminCapabilityError(
            "platform-admin capabilities are issued only by prepare_platform_admin"
        )

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> "PreparedPlatformAdmin":
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "_user_id", user_id)
        object.__setattr__(prepared, "_fingerprint", (user_id,))
        object.__setattr__(prepared, "_session", session)
        object.__setattr__(
            prepared,
            "_transaction",
            session.sync_session.get_transaction(),
        )
        object.__setattr__(
            prepared,
            "_nested_transaction",
            session.sync_session.get_nested_transaction(),
        )
        object.__setattr__(prepared, "_seal", _PREPARED_PLATFORM_ADMIN_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedPlatformAdmin is immutable")

    @property
    def user_id(self) -> uuid.UUID:
        return self._user_id


_PREPARED_PLATFORM_ADMIN_SEAL = object()


def _require_prepared(
    session: AsyncSession,
    prepared: PreparedPlatformAdmin,
) -> PreparedPlatformAdmin:
    if not isinstance(prepared, PreparedPlatformAdmin):
        raise PlatformAdminCapabilityError(
            "platform-admin action requires a prepared capability"
        )
    try:
        valid = (
            prepared._seal is _PREPARED_PLATFORM_ADMIN_SEAL
            and prepared._fingerprint == (prepared._user_id,)
            and prepared._session is session
            and prepared._transaction
            is session.sync_session.get_transaction()
            and prepared._nested_transaction
            is session.sync_session.get_nested_transaction()
        )
    except (AttributeError, TypeError) as exc:
        raise PlatformAdminCapabilityError(
            "platform-admin capability is invalid"
        ) from exc
    if not valid:
        raise PlatformAdminCapabilityError(
            "platform-admin capability is stale, foreign, or modified"
        )
    return prepared


async def is_active_platform_admin(
    session: AsyncSession,
    *,
    actor_username: str,
) -> bool:
    """Return UI visibility only; this is never a write authorization."""

    lookup_key = normalize_username(actor_username).lookup_key
    with session.no_autoflush:
        return (
            await session.scalar(
                select(User.id)
                .join(UserRole, UserRole.user_id == User.id)
                .where(
                    User.normalized_username == lookup_key,
                    User.status == UserStatus.ACTIVE.value,
                    UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
                )
                .limit(1)
            )
            is not None
        )


async def prepare_platform_admin(
    session: AsyncSession,
    *,
    actor_username: str,
) -> PreparedPlatformAdmin:
    """Lock and prove one active platform-superadmin identity."""

    lookup_key = normalize_username(actor_username).lookup_key
    await acquire_identity_governance_lock(session)
    with session.no_autoflush:
        user = await session.scalar(
            select(User)
            .where(User.normalized_username == lookup_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise PlatformAdminAuthorizationError(
                "active platform-superadmin authorization is required"
            )
        role = await session.scalar(
            select(UserRole)
            .where(
                UserRole.user_id == user.id,
                UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if role is None:
            raise PlatformAdminAuthorizationError(
                "active platform-superadmin authorization is required"
            )
    return PreparedPlatformAdmin._issue(session=session, user_id=user.id)


async def record_openrouter_configuration_change(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    changed_fields: Iterable[str],
) -> AuditEvent | None:
    """Append a value-free audit record for one authorized configuration save."""

    capability = _require_prepared(session, prepared)
    try:
        fields = tuple(sorted(set(changed_fields)))
    except TypeError as exc:
        raise PlatformAdminValidationError(
            "changed_fields must be an iterable of reviewed field names"
        ) from exc
    if any(field not in _OPENROUTER_CHANGED_FIELDS for field in fields):
        raise PlatformAdminValidationError(
            "changed_fields contains an unreviewed OpenRouter field"
        )
    if not fields:
        return None

    event = AuditEvent(
        actor_user_id=capability.user_id,
        subject_id=None,
        event_type="platform.openrouter.configuration.updated",
        outcome=AuditOutcome.SUCCESS.value,
        resource_type="platform_integration",
        resource_id="openrouter",
        metadata_json={
            "source_surface": "web.settings",
            "result_code": "configuration_updated",
            "changed_fields": list(fields),
        },
    )
    session.add(event)
    await session.flush()
    return event


__all__ = [
    "PlatformAdminAuthorizationError",
    "PlatformAdminCapabilityError",
    "PlatformAdminError",
    "PlatformAdminValidationError",
    "PreparedPlatformAdmin",
    "is_active_platform_admin",
    "prepare_platform_admin",
    "record_openrouter_configuration_change",
]
