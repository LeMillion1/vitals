"""Durable identity-governance operations for the multi-user foundation.

The functions in this module deliberately do not commit.  They acquire the
shared identity-governance lock, mutate, and flush; the web, job, or startup
boundary owns the transaction outcome.

PostgreSQL uses one transaction-scoped advisory lock for every operation that
can change the active platform-superadmin set or an identity credential.  This
serializes empty-table bootstrap and the otherwise racy "last active admin"
check.  SQLite is the fast test path and has no equivalent cross-connection
guarantee, so the lock is intentionally a no-op there.
"""
from __future__ import annotations

import hmac
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import AuditOutcome, UserRoleName, UserStatus
from vitals.models.identity import AuditEvent, HealthSubject, User, UserRole

# Stable two-int PostgreSQL advisory-lock namespace.  Never use Python's hash(),
# whose result changes between processes.  0x5649544C spells "VITL".
IDENTITY_GOVERNANCE_LOCK_NAMESPACE = 0x5649544C
IDENTITY_GOVERNANCE_LOCK_KEY = 1
_PRE_IDENTITY_COMPATIBILITY_TRANSACTION_KEY = (
    "vitals.identity.pre_identity_compatibility_transaction"
)

_BCRYPT_RE = re.compile(
    r"^\$2[aby]\$(?P<cost>\d{2})\$[./A-Za-z0-9]{53}$"
)
_MIN_BCRYPT_COST = 4
_MAX_BCRYPT_COST = 31
_MAX_USERNAME_LENGTH = 128
_MAX_EMAIL_LENGTH = 320


class IdentityServiceError(RuntimeError):
    """Base class for identity state and governance failures."""


class IdentityValidationError(ValueError):
    """An identity input cannot be represented safely."""


class PreIdentityCompatibilityError(IdentityServiceError):
    """A zero-subject compatibility operation cannot prove a safe snapshot."""


class UnsupportedIdentityDatabaseError(IdentityServiceError):
    """Identity governance was called on an unsupported database dialect."""


class UserNotFoundError(IdentityServiceError):
    """The requested identity does not exist."""


class IdentityStateConflictError(IdentityServiceError):
    """Persisted identity state is inconsistent with the requested mutation."""


class LastActivePlatformSuperadminError(IdentityServiceError):
    """A mutation would leave the platform without an active superadmin."""


class PasswordHashMismatchError(IdentityServiceError):
    """A compare-and-swap password update used a stale current hash."""


class PasswordHashDowngradeError(IdentityServiceError):
    """A password update attempted to lower the bcrypt work factor."""


@dataclass(frozen=True, slots=True)
class NormalizedUsername:
    """Display spelling plus the unique, case-insensitive lookup key."""

    display: str
    lookup_key: str


@dataclass(frozen=True, slots=True)
class NormalizedEmail:
    """Display spelling plus the shallow mailbox comparison key.

    Email is never an identity key.  This representation exists only for
    uniqueness and for exact invitation-address matching after an identity
    provider has independently vouched for the claim.
    """

    display: str
    lookup_key: str


def normalize_username(raw: str) -> NormalizedUsername:
    """Return the one canonical username representation used by identity writes.

    NFKC prevents compatibility spellings from creating separate accounts, and
    ``casefold`` is deliberately stronger than ASCII-only ``lower``.  Internal
    whitespace remains valid for the legacy owner; registration can impose a
    narrower product policy later without changing the persisted lookup rule.
    """

    if not isinstance(raw, str):
        raise IdentityValidationError("username must be a string")
    display = unicodedata.normalize("NFKC", raw).strip()
    if not display:
        raise IdentityValidationError("username must not be blank")
    if any(unicodedata.category(char).startswith("C") for char in display):
        raise IdentityValidationError("username must not contain control characters")
    lookup_key = display.casefold()
    if len(display) > _MAX_USERNAME_LENGTH or len(lookup_key) > _MAX_USERNAME_LENGTH:
        raise IdentityValidationError("normalized username is too long")
    return NormalizedUsername(display=display, lookup_key=lookup_key)


def normalize_email(raw: str) -> NormalizedEmail:
    """Normalize an email claim without provider-specific rewriting.

    NFKC, surrounding whitespace removal and case folding are the complete
    policy.  Dropping dots or ``+`` suffixes would be correct for one provider
    and would merge distinct mailboxes at another.
    """

    if not isinstance(raw, str):
        raise IdentityValidationError("email must be a string")
    display = unicodedata.normalize("NFKC", raw).strip()
    if not display or "@" not in display:
        raise IdentityValidationError("email is not a usable address")
    if any(unicodedata.category(char).startswith("C") for char in display):
        raise IdentityValidationError("email must not contain control characters")
    lookup_key = display.casefold()
    if len(display) > _MAX_EMAIL_LENGTH or len(lookup_key) > _MAX_EMAIL_LENGTH:
        raise IdentityValidationError("normalized email is too long")
    return NormalizedEmail(display=display, lookup_key=lookup_key)


def bcrypt_cost(password_hash: str) -> int:
    """Validate a bcrypt hash envelope and return its work factor.

    This validates syntax only.  Bootstrap has no plaintext password and must
    copy the configured one-way hash verbatim rather than verify or re-hash it.
    """

    if not isinstance(password_hash, str):
        raise IdentityValidationError("password hash must be a string")
    match = _BCRYPT_RE.fullmatch(password_hash)
    if match is None:
        raise IdentityValidationError("password hash must be a complete bcrypt hash")
    cost = int(match.group("cost"))
    if not _MIN_BCRYPT_COST <= cost <= _MAX_BCRYPT_COST:
        raise IdentityValidationError("bcrypt cost is outside the supported range")
    return cost


async def acquire_identity_governance_lock(session: AsyncSession) -> None:
    """Serialize identity-governance mutations for the current transaction."""

    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return
    if dialect != "postgresql":
        raise UnsupportedIdentityDatabaseError(
            f"identity governance does not support database dialect {dialect!r}"
        )
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "CAST(:namespace AS INTEGER), CAST(:lock_key AS INTEGER))"
        ),
        {
            "namespace": IDENTITY_GOVERNANCE_LOCK_NAMESPACE,
            "lock_key": IDENTITY_GOVERNANCE_LOCK_KEY,
        },
    )


def _reject_pending_subject_state(sync_session) -> None:
    """Refuse to treat a session with pending subject rows as pre-identity."""

    subject_state = (
        tuple(sync_session.new)
        + tuple(sync_session.dirty)
        + tuple(sync_session.deleted)
    )
    if any(isinstance(row, HealthSubject) for row in subject_state):
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility rejects pending subject identity state"
        )


async def _guard_pre_identity_root(session: AsyncSession) -> object:
    """Hold the governance lock for this transaction and prove zero subjects.

    Both compatibility entry points share this body; they differ only in what
    they demand of the transaction *before* it runs.  The two facts proved here
    are the ones a legacy mutation actually needs: the shared identity-governance
    lock is held for the remainder of the transaction, so a concurrent bootstrap
    stays frozen until the caller commits, and the database still has zero health
    subjects.
    """

    sync_session = session.sync_session
    transaction = sync_session.get_transaction()
    if (
        transaction is None
        or not transaction.is_active
        or sync_session.info.get(_PRE_IDENTITY_COMPATIBILITY_TRANSACTION_KEY)
        is not transaction
    ):
        pending_transaction = transaction
        with session.no_autoflush:
            if session.get_bind().dialect.name == "sqlite":
                # SQLite has no advisory locks; ``BEGIN IMMEDIATE`` is the
                # equivalent, and it can only open a transaction that has not
                # started one yet.  An adopted transaction therefore keeps
                # SQLite's deferred write lock — which is no weaker than the
                # cross-connection guarantee this dialect offers anyway.
                if transaction is None or not bool(
                    getattr(transaction, "_connections", {})
                ):
                    await session.execute(text("BEGIN IMMEDIATE"))
            else:
                await acquire_identity_governance_lock(session)
        transaction = sync_session.get_transaction()
        if (
            transaction is None
            or not transaction.is_active
            or (
                pending_transaction is not None
                and transaction is not pending_transaction
            )
        ):
            raise PreIdentityCompatibilityError(
                "pre-identity compatibility could not establish a guarded transaction"
            )
        sync_session.info[
            _PRE_IDENTITY_COMPATIBILITY_TRANSACTION_KEY
        ] = transaction

    with session.no_autoflush:
        subject_id = await session.scalar(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(1)
        )
    if subject_id is not None:
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility is closed after identity bootstrap"
        )
    return transaction


async def authorize_pre_identity_compatibility_transaction(
    session: AsyncSession,
) -> object:
    """Authorize and return one guarded root transaction with zero subjects.

    This is the *boundary* API: a web request, a job, or a legacy preference
    write must present a fresh root, so earlier unguarded work can never be
    smuggled into a legacy mutation.  Re-entry is allowed only for the exact root
    transaction this function authorized; arbitrary pre-open or nested
    transactions fail closed.  Pending, dirty, or deleted subject state is never
    autoflushed or treated as an authoritative zero-subject database.

    Service hooks that run deep inside a caller's transaction cannot present a
    fresh root and must use :func:`require_pre_identity_compatibility` instead.
    """

    sync_session = session.sync_session
    if sync_session.get_nested_transaction() is not None:
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility requires a fresh outer transaction"
        )

    _reject_pending_subject_state(sync_session)

    transaction = sync_session.get_transaction()
    authorized = sync_session.info.get(
        _PRE_IDENTITY_COMPATIBILITY_TRANSACTION_KEY
    )
    # ``Session.add()`` creates a logical root before any database work.  That
    # exact connectionless root is still safe to guard below, which is how
    # unrelated pending ORM state can stay pending and unflushed; anything that
    # has already reached a connection is somebody else's transaction.
    if (
        transaction is not None
        and not (transaction is authorized and transaction.is_active)
        and (
            not transaction.is_active
            or bool(getattr(transaction, "_connections", {None: None}))
        )
    ):
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility requires a fresh guarded transaction"
        )

    return await _guard_pre_identity_root(session)


async def require_pre_identity_compatibility(session: AsyncSession) -> object:
    """Prove pre-identity compatibility inside the caller's open transaction.

    Legacy service hooks — the Garmin weight outbox projection, its
    reconciliation, the local delete hook — run in the middle of somebody else's
    write transaction.  They cannot present the fresh root that
    :func:`authorize_pre_identity_compatibility_transaction` demands, and
    demanding it there buys no safety: it only turns a legitimate zero-subject
    write into a silent no-op.  This sibling adopts the caller's transaction and
    proves exactly the same two facts, so the guarantee a legacy mutation needs —
    identity bootstrap frozen until the caller commits — is unchanged.

    The one contract this cannot verify, and every caller must therefore honour,
    is lock order.  Call it *before* the Garmin outbox advisory and before any
    row lock, in the position
    :func:`vitals.services.weight_service.prepare_weight_write` gives
    :func:`acquire_identity_governance_lock` on the scoped path.  Taking
    governance after a lock that the canonical order puts behind it is the
    inversion that deadlocks.
    """

    sync_session = session.sync_session
    if sync_session.get_nested_transaction() is not None:
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility requires a fresh outer transaction"
        )

    _reject_pending_subject_state(sync_session)

    return await _guard_pre_identity_root(session)


async def _user_for_update(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
    if user is None:
        raise UserNotFoundError(f"user {user_id} does not exist")
    return user


async def _role_for_update(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    role: UserRoleName,
) -> Optional[UserRole]:
    return await session.scalar(
        select(UserRole)
        .where(UserRole.user_id == user_id, UserRole.role == role.value)
        .with_for_update()
    )


async def has_active_platform_superadmin(
    session: AsyncSession,
    *,
    exclude_user_id: uuid.UUID | None = None,
) -> bool:
    """Return whether the current transaction can see an active superadmin."""

    query = (
        select(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            User.status == UserStatus.ACTIVE.value,
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
        )
        .limit(1)
    )
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    return await session.scalar(query) is not None


def _add_audit_event(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID | None,
    user_id: uuid.UUID,
    event_type: str,
    result_code: str,
    changed_fields: list[str],
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS.value,
            resource_type="user",
            resource_id=str(user_id),
            metadata_json={
                "source_surface": "identity_service",
                "result_code": result_code,
                "changed_fields": changed_fields,
            },
        )
    )


def _as_role(role: UserRoleName | str) -> UserRoleName:
    try:
        return UserRoleName(role)
    except (TypeError, ValueError) as exc:
        raise IdentityValidationError(f"unknown user role: {role!r}") from exc


def _as_status(status: UserStatus | str) -> UserStatus:
    try:
        return UserStatus(status)
    except (TypeError, ValueError) as exc:
        raise IdentityValidationError(f"unknown user status: {status!r}") from exc


async def assign_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    role: UserRoleName | str,
    assigned_by_user_id: uuid.UUID | None,
) -> UserRole:
    """Idempotently assign a capability role without granting subject access."""

    role_name = _as_role(role)
    await acquire_identity_governance_lock(session)
    await _user_for_update(session, user_id)
    existing = await _role_for_update(session, user_id=user_id, role=role_name)
    if existing is not None:
        return existing

    assignment = UserRole(
        user_id=user_id,
        role=role_name.value,
        assigned_by_user_id=assigned_by_user_id,
    )
    session.add(assignment)
    _add_audit_event(
        session,
        actor_user_id=assigned_by_user_id,
        user_id=user_id,
        event_type="identity.role.assigned",
        result_code=f"{role_name.value}_assigned",
        changed_fields=["roles"],
    )
    await session.flush()
    return assignment


async def revoke_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    role: UserRoleName | str,
    actor_user_id: uuid.UUID | None,
) -> bool:
    """Revoke a role, rejecting removal of the last active superadmin."""

    role_name = _as_role(role)
    await acquire_identity_governance_lock(session)
    user = await _user_for_update(session, user_id)
    assignment = await _role_for_update(session, user_id=user_id, role=role_name)
    if assignment is None:
        return False

    if (
        role_name is UserRoleName.PLATFORM_SUPERADMIN
        and user.status == UserStatus.ACTIVE.value
        and not await has_active_platform_superadmin(
            session, exclude_user_id=user.id
        )
    ):
        raise LastActivePlatformSuperadminError(
            "cannot revoke the last active platform_superadmin role"
        )

    await session.delete(assignment)
    _add_audit_event(
        session,
        actor_user_id=actor_user_id,
        user_id=user_id,
        event_type="identity.role.revoked",
        result_code=f"{role_name.value}_revoked",
        changed_fields=["roles"],
    )
    await session.flush()
    return True


async def change_user_status(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    new_status: UserStatus | str,
    actor_user_id: uuid.UUID | None,
) -> User:
    """Change lifecycle status while preserving one active platform superadmin."""

    status = _as_status(new_status)
    await acquire_identity_governance_lock(session)
    user = await _user_for_update(session, user_id)
    if user.status == status.value:
        return user

    superadmin_role = await _role_for_update(
        session,
        user_id=user.id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
    )
    if (
        user.status == UserStatus.ACTIVE.value
        and status is not UserStatus.ACTIVE
        and superadmin_role is not None
        and not await has_active_platform_superadmin(
            session, exclude_user_id=user.id
        )
    ):
        raise LastActivePlatformSuperadminError(
            "cannot deactivate the last active platform_superadmin"
        )

    previous_status = user.status
    user.status = status.value
    # Any lifecycle transition invalidates credentials minted under the previous
    # state.  PR-05 will make browser/MCP validation consume this version.
    user.session_version += 1
    if status is not UserStatus.ACTIVE:
        # A browser endpoint is an account credential. Suspending the account
        # invalidates it in the same transaction as its sessions and status;
        # reactivation requires an explicit browser permission gesture again.
        from vitals.services.notifications import web_push_subscriptions

        await web_push_subscriptions.revoke_all(session, user_id=user.id)
    _add_audit_event(
        session,
        actor_user_id=actor_user_id,
        user_id=user.id,
        event_type="identity.user.status_changed",
        result_code=f"{previous_status}_to_{status.value}",
        changed_fields=["status", "session_version"],
    )
    await session.flush()
    return user


async def rotate_password_hash(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    expected_current_hash: str,
    new_hash: str,
    actor_user_id: uuid.UUID | None,
) -> User:
    """Replace a bcrypt hash with compare-and-swap and revoke old sessions."""

    new_cost = bcrypt_cost(new_hash)
    await acquire_identity_governance_lock(session)
    user = await _user_for_update(session, user_id)
    if not isinstance(expected_current_hash, str) or not hmac.compare_digest(
        user.password_hash, expected_current_hash
    ):
        raise PasswordHashMismatchError("stored password hash changed concurrently")

    try:
        current_cost = bcrypt_cost(user.password_hash)
    except IdentityValidationError as exc:
        raise IdentityStateConflictError(
            "stored password hash is not a valid bcrypt hash"
        ) from exc
    if new_cost < current_cost:
        raise PasswordHashDowngradeError(
            "new bcrypt work factor is lower than the stored work factor"
        )
    if hmac.compare_digest(user.password_hash, new_hash):
        return user

    user.password_hash = new_hash
    user.session_version += 1
    _add_audit_event(
        session,
        actor_user_id=actor_user_id,
        user_id=user.id,
        event_type="identity.password.rotated",
        result_code="password_hash_rotated",
        changed_fields=["password_hash", "session_version"],
    )
    await session.flush()
    return user


__all__ = [
    "IDENTITY_GOVERNANCE_LOCK_KEY",
    "IDENTITY_GOVERNANCE_LOCK_NAMESPACE",
    "IdentityServiceError",
    "IdentityStateConflictError",
    "IdentityValidationError",
    "LastActivePlatformSuperadminError",
    "NormalizedEmail",
    "NormalizedUsername",
    "PasswordHashDowngradeError",
    "PasswordHashMismatchError",
    "PreIdentityCompatibilityError",
    "UnsupportedIdentityDatabaseError",
    "UserNotFoundError",
    "acquire_identity_governance_lock",
    "authorize_pre_identity_compatibility_transaction",
    "assign_role",
    "bcrypt_cost",
    "change_user_status",
    "has_active_platform_superadmin",
    "normalize_username",
    "normalize_email",
    "require_pre_identity_compatibility",
    "revoke_role",
    "rotate_password_hash",
]
