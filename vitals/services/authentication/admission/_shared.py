"""Shared admission contracts, validation, authorization, and provisioning."""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AuditOutcome,
    RegistrationAccountKind,
    RegistrationInvitationStatus,
    RegistrationRequestStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import AuditEvent, User, UserFederatedIdentity, UserRole
from vitals.models.registration import RegistrationInvitation, RegistrationRequest
from vitals.persistence.rls import enter_platform_scope
from vitals.services.authentication.provisioning import (
    AccountAlreadyExists,
    AccountProvisioningError,
    ProvisionedAccount,
    provision_account,
)
from vitals.services.authentication.registration import (
    RegistrationMode,
    effective_mode,
)
from vitals.services.identity_service import (
    IdentityValidationError,
    NormalizedEmail,
    normalize_email,
    normalize_username,
)
from vitals.utils.timeutils import now_utc

INVITATION_TTL = timedelta(days=14)
REQUEST_TTL = timedelta(days=30)
MAX_INVITATION_TTL = timedelta(days=30)
MAX_REQUEST_TTL = timedelta(days=90)
REQUEST_REAPPLY_COOLDOWN = timedelta(hours=24)
DEFAULT_RETENTION = timedelta(days=90)
MINIMUM_RETENTION = timedelta(days=30)
MAX_MAINTENANCE_BATCH = 1000
TOKEN_BYTES = 32
AUDIT_SURFACE = "authentication.admission"


class AdmissionError(RuntimeError):
    """Base class for account-admission failures."""


class AdmissionValidationError(ValueError):
    """A supplied value is not safe to persist or act on."""


class AdmissionRefused(AdmissionError):
    """An anonymous admission proof did not authorize an account.

    Unknown, spent, expired, wrong-mode, wrong-address, and already-linked
    proofs intentionally share one outward exception.
    """


class AdmissionForbidden(AdmissionError):
    """The actor is not an active platform superadmin."""


class AdmissionStateError(AdmissionError):
    """An operator attempted a stale or impossible state transition."""


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    invitation: RegistrationInvitation
    #: Returned exactly once. Only its SHA-256 digest is persisted.
    token: str


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    user: User
    account: ProvisionedAccount


@dataclass(frozen=True, slots=True)
class RetentionResult:
    invitations: int = 0
    requests: int = 0


ACCOUNT_SHAPES = {
    RegistrationAccountKind.MEMBER: (UserRoleName.MEMBER, True),
    RegistrationAccountKind.DOCTOR: (UserRoleName.DOCTOR, False),
    RegistrationAccountKind.TRAINER: (UserRoleName.TRAINER, False),
}


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def coerce_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise AdmissionValidationError("timestamp must be a datetime")
    return as_utc(value)


async def database_now(
    session: AsyncSession, *, supplied: datetime | None = None
) -> datetime:
    """Use the database statement clock for lifecycle decisions.

    Account-admission rows use database defaults for ``created_at``. Reading
    persisted transition timestamps from the app process can violate
    ``transition_at >= created_at`` when the app and PostgreSQL hosts differ by
    even a few milliseconds. PostgreSQL's transaction-scoped ``now()`` is also
    unsafe for an expiry boundary: a transaction may begin before a governance
    lock wait and act after the deadline. ``statement_timestamp()`` observes the
    statement issued after that wait. SQLite keeps its native current-time
    expression for fast compatibility tests. An explicit supplied value remains
    available for deterministic maintenance tests.
    """

    if supplied is not None:
        return coerce_timestamp(supplied)
    expression = (
        func.statement_timestamp()
        if session.get_bind().dialect.name == "postgresql"
        else func.now()
    )
    stamp = await session.scalar(select(expression))
    return as_utc(stamp) if stamp is not None else now_utc()


def bounded_ttl(
    value: timedelta, *, name: str, maximum: timedelta
) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise AdmissionValidationError(f"{name} must be a positive interval")
    if value > maximum:
        raise AdmissionValidationError(
            f"{name} must not exceed {maximum.days} days"
        )
    return value


def bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdmissionValidationError("limit must be an integer")
    if value < 1 or value > MAX_MAINTENANCE_BATCH:
        raise AdmissionValidationError(
            f"limit must be between 1 and {MAX_MAINTENANCE_BATCH}"
        )
    return value


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def presented_token(raw: object) -> str:
    # The issued token is 43 ASCII characters today. Keep a generous protocol
    # ceiling for future encodings, but never hash attacker-controlled
    # unbounded input or silently canonicalize a bearer secret.
    if (
        not isinstance(raw, str)
        or not raw
        or raw != raw.strip()
        or len(raw) > 512
    ):
        raise AdmissionRefused("this admission proof does not open an account")
    return raw


def clean_identity_pair(issuer: object, subject: object) -> tuple[str, str]:
    if not isinstance(issuer, str) or not isinstance(subject, str):
        raise AdmissionValidationError("issuer and subject must be strings")
    if (
        not issuer
        or not subject
        or issuer != issuer.strip()
        or subject != subject.strip()
        or len(issuer) > 512
        or len(subject) > 255
    ):
        raise AdmissionValidationError(
            "issuer and subject must be bounded exact provider values"
        )
    return issuer, subject


def verified_email(raw: object, *, email_verified: bool) -> NormalizedEmail:
    if email_verified is not True:
        raise AdmissionRefused("this admission proof does not open an account")
    try:
        return normalize_email(raw)
    except (IdentityValidationError, TypeError) as exc:
        raise AdmissionRefused("this admission proof does not open an account") from exc


def preferred_username(raw: object) -> str | None:
    if raw is None or not isinstance(raw, str):
        return None
    value = unicodedata.normalize("NFKC", raw).strip()
    if not value or len(value) > 128:
        return None
    if any(unicodedata.category(char).startswith("C") for char in value):
        return None
    try:
        return normalize_username(value).display
    except IdentityValidationError:
        return None


def account_kind(
    value: RegistrationAccountKind | str,
) -> RegistrationAccountKind:
    try:
        return RegistrationAccountKind(value)
    except (TypeError, ValueError) as exc:
        raise AdmissionValidationError("unknown registration account kind") from exc


async def require_mode(session: AsyncSession, expected: RegistrationMode) -> None:
    if await effective_mode(session) is not expected:
        raise AdmissionRefused("this admission proof does not open an account")


async def require_operator(
    session: AsyncSession, *, actor_user_id: uuid.UUID
) -> User:
    if not isinstance(actor_user_id, uuid.UUID) or actor_user_id.int == 0:
        raise AdmissionForbidden("active platform-superadmin authorization is required")
    user = await session.scalar(
        select(User)
        .where(User.id == actor_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise AdmissionForbidden("active platform-superadmin authorization is required")
    role = await session.scalar(
        select(UserRole)
        .where(
            UserRole.user_id == actor_user_id,
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if role is None:
        raise AdmissionForbidden("active platform-superadmin authorization is required")
    return user


def audit(
    session: AsyncSession,
    *,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID | str,
    result_code: str,
    actor_user_id: uuid.UUID | None = None,
    changed_fields: tuple[str, ...] = (),
    record_count: int | None = None,
) -> None:
    metadata: dict[str, object] = {
        "source_surface": AUDIT_SURFACE,
        "result_code": result_code,
        "resource_type": resource_type,
        "resource_id": str(resource_id),
    }
    if changed_fields:
        metadata["changed_fields"] = list(changed_fields)
    if record_count is not None:
        metadata["record_count"] = record_count
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            subject_id=None,
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS.value,
            resource_type=resource_type,
            resource_id=str(resource_id),
            metadata_json=metadata,
        )
    )


def expire_invitation(row: RegistrationInvitation, *, now: datetime) -> None:
    # Superseding a live invitation is revocation, not a rewrite of its expiry.
    row.expired_at = max(now, as_utc(row.expires_at))
    row.status = RegistrationInvitationStatus.EXPIRED.value


def expire_request(row: RegistrationRequest, *, now: datetime) -> None:
    row.status = RegistrationRequestStatus.EXPIRED.value
    row.expired_at = max(now, as_utc(row.expires_at))


async def available_username(
    session: AsyncSession,
    *,
    preferred: str | None,
    seed: uuid.UUID,
    prefix: str,
) -> str:
    candidates = []
    cleaned = preferred_username(preferred)
    if cleaned is not None:
        candidates.append(cleaned)
    candidates.append(f"{prefix}-{seed.hex[:24]}")
    for candidate in candidates:
        normalized = normalize_username(candidate)
        taken = await session.scalar(
            select(User.id).where(User.normalized_username == normalized.lookup_key)
        )
        if taken is None:
            return normalized.display
    for suffix in range(1, 100):
        candidate = f"{prefix}-{seed.hex[:20]}-{suffix}"
        normalized = normalize_username(candidate)
        taken = await session.scalar(
            select(User.id).where(User.normalized_username == normalized.lookup_key)
        )
        if taken is None:
            return normalized.display
    raise AdmissionStateError("could not allocate an opaque local username")


async def provision_and_link(
    session: AsyncSession,
    *,
    admission_id: uuid.UUID,
    account_kind_value: RegistrationAccountKind,
    issuer: str,
    subject: str,
    mailbox: NormalizedEmail,
    persist_email_proof: bool,
    verified_at: datetime | None,
    preferred_username_value: str | None,
    authenticated_at: datetime | None,
    assigned_by_user_id: uuid.UUID,
) -> AdmissionResult:
    # Validate every caller-supplied timestamp before the first mutation or
    # flush. A delivery boundary may translate validation failures without
    # rolling back immediately; it must never be handed a partial account graph.
    last_authenticated_at = (
        coerce_timestamp(authenticated_at)
        if authenticated_at is not None
        else None
    )
    verified_timestamp = (
        coerce_timestamp(verified_at) if verified_at is not None else None
    )
    if persist_email_proof and verified_timestamp is None:
        raise AdmissionStateError("verified email proof needs its timestamp")

    existing_link = await session.scalar(
        select(UserFederatedIdentity)
        .where(
            UserFederatedIdentity.issuer == issuer,
            UserFederatedIdentity.subject == subject,
        )
        .with_for_update()
    )
    if existing_link is not None:
        raise AdmissionRefused("this admission proof does not open an account")
    email_owner = await session.scalar(
        select(User.id).where(User.normalized_email == mailbox.lookup_key)
    )
    if email_owner is not None:
        raise AdmissionRefused("this admission proof does not open an account")

    role, with_record = ACCOUNT_SHAPES[account_kind_value]
    username = await available_username(
        session,
        preferred=preferred_username_value,
        seed=admission_id,
        prefix=account_kind_value.value,
    )
    # A member account materializes strict subject-owned roots before a subject
    # session can exist. Enter only after every proof, collision check, input
    # validation, and local-name allocation has succeeded. Professional-only
    # accounts create no subject graph and never need the broad scope.
    if with_record:
        await enter_platform_scope(session)
    try:
        account = await provision_account(
            session,
            username=username,
            email=mailbox.display if persist_email_proof else None,
            display_name=username,
            roles=(role.value,),
            with_health_record=with_record,
        )
    except AccountAlreadyExists as exc:
        raise AdmissionRefused("this admission proof does not open an account") from exc
    except AccountProvisioningError as exc:
        raise AdmissionStateError("could not provision the admitted account") from exc

    user = await session.get(User, account.user_id)
    if user is None:  # pragma: no cover - provision_account just flushed it
        raise AdmissionStateError("provisioning did not produce an account")
    if persist_email_proof:
        user.email_verified_at = verified_timestamp
    assignment = await session.scalar(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role == role.value,
        )
    )
    if assignment is None:  # pragma: no cover - provisioning invariant
        raise AdmissionStateError("provisioning did not produce the required role")
    assignment.assigned_by_user_id = assigned_by_user_id
    session.add(
        UserFederatedIdentity(
            user_id=user.id,
            issuer=issuer,
            subject=subject,
            last_authenticated_at=last_authenticated_at,
        )
    )
    await session.flush()
    return AdmissionResult(user=user, account=account)
