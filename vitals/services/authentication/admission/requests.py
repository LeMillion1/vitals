"""Federated admission requests and strict operator decisions."""

from __future__ import annotations

import unicodedata
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import RegistrationAccountKind, RegistrationRequestStatus
from vitals.models.identity import User, UserFederatedIdentity
from vitals.models.registration import RegistrationRequest
from vitals.services.authentication.admission._shared import (
    MAX_REQUEST_TTL,
    REQUEST_REAPPLY_COOLDOWN,
    REQUEST_TTL,
    AdmissionRefused,
    AdmissionResult,
    AdmissionStateError,
    AdmissionValidationError,
    as_utc,
    audit,
    bounded_ttl,
    clean_identity_pair,
    database_now,
    expire_request,
    preferred_username as clean_preferred_username,
    provision_and_link,
    require_mode,
    require_operator,
    verified_email as validate_verified_email,
)
from vitals.services.authentication.registration import RegistrationMode
from vitals.services.identity_service import acquire_identity_governance_lock


async def submit_request(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    verified_email: str,
    email_verified: bool,
    preferred_username: str | None = None,
    ttl: timedelta = REQUEST_TTL,
) -> RegistrationRequest:
    """Create or refresh one pending member request without creating a User."""

    ttl = bounded_ttl(ttl, name="ttl", maximum=MAX_REQUEST_TTL)
    try:
        issuer, subject = clean_identity_pair(issuer, subject)
    except AdmissionValidationError as exc:
        raise AdmissionRefused("this admission request cannot be submitted") from exc
    mailbox = validate_verified_email(
        verified_email, email_verified=email_verified
    )
    preferred = clean_preferred_username(preferred_username)

    await acquire_identity_governance_lock(session)
    await require_mode(session, RegistrationMode.ADMIN_APPROVED)
    existing_link = await session.scalar(
        select(UserFederatedIdentity.id).where(
            UserFederatedIdentity.issuer == issuer,
            UserFederatedIdentity.subject == subject,
        )
    )
    if existing_link is not None:
        raise AdmissionRefused("this admission request cannot be submitted")
    email_owner = await session.scalar(
        select(User.id).where(User.normalized_email == mailbox.lookup_key)
    )
    if email_owner is not None:
        raise AdmissionRefused("this admission request cannot be submitted")

    row = await session.scalar(
        select(RegistrationRequest)
        .where(
            RegistrationRequest.issuer == issuer,
            RegistrationRequest.subject == subject,
            RegistrationRequest.status == RegistrationRequestStatus.PENDING.value,
        )
        .with_for_update()
    )
    now = await database_now(session)
    if row is not None and now >= as_utc(row.expires_at):
        expire_request(row, now=now)
        audit(
            session,
            event_type="registration.request.expired",
            resource_type="registration_request",
            resource_id=row.id,
            result_code="expired_on_resubmit",
            changed_fields=("status",),
        )
        await session.flush()
        row = None
    if row is not None:
        row.verified_email = mailbox.display
        row.normalized_verified_email = mailbox.lookup_key
        row.preferred_username = preferred
        row.last_seen_at = now
        audit(
            session,
            event_type="registration.request.refreshed",
            resource_type="registration_request",
            resource_id=row.id,
            result_code="pending_claims_refreshed",
            changed_fields=("verified_email", "preferred_username", "last_seen_at"),
        )
        await session.flush()
        return row

    latest_terminal = await session.scalar(
        select(RegistrationRequest)
        .where(
            RegistrationRequest.issuer == issuer,
            RegistrationRequest.subject == subject,
            RegistrationRequest.status != RegistrationRequestStatus.PENDING.value,
        )
        .order_by(RegistrationRequest.created_at.desc(), RegistrationRequest.id)
        .limit(1)
        .with_for_update()
    )
    if latest_terminal is not None:
        if latest_terminal.status == RegistrationRequestStatus.APPROVED.value:
            raise AdmissionStateError(
                "approved registration request has no federated identity"
            )
        terminal_at = None
        if latest_terminal.status == RegistrationRequestStatus.REJECTED.value:
            terminal_at = latest_terminal.reviewed_at
        elif latest_terminal.status == RegistrationRequestStatus.EXPIRED.value:
            terminal_at = latest_terminal.expired_at
        if (
            terminal_at is not None
            and now < as_utc(terminal_at) + REQUEST_REAPPLY_COOLDOWN
        ):
            raise AdmissionRefused("this admission request cannot be submitted")

    row = RegistrationRequest(
        issuer=issuer,
        subject=subject,
        verified_email=mailbox.display,
        normalized_verified_email=mailbox.lookup_key,
        preferred_username=preferred,
        account_kind=RegistrationAccountKind.MEMBER.value,
        status=RegistrationRequestStatus.PENDING.value,
        expires_at=now + ttl,
        last_seen_at=now,
    )
    session.add(row)
    await session.flush()
    audit(
        session,
        event_type="registration.request.submitted",
        resource_type="registration_request",
        resource_id=row.id,
        result_code="member_request_submitted",
        changed_fields=("status", "account_kind", "expires_at"),
    )
    await session.flush()
    return row


async def get_request(
    session: AsyncSession, *, issuer: str, subject: str
) -> RegistrationRequest | None:
    """Return only the caller's exact current request in admin-approved mode.

    This is an OIDC service seam, not an operator directory API. A delivery
    layer must never accept a different issuer/subject pair from browser input.
    """

    try:
        issuer, subject = clean_identity_pair(issuer, subject)
    except AdmissionValidationError as exc:
        raise AdmissionRefused("this admission request is unavailable") from exc
    await acquire_identity_governance_lock(session)
    await require_mode(session, RegistrationMode.ADMIN_APPROVED)
    row = await session.scalar(
        select(RegistrationRequest)
        .where(
            RegistrationRequest.issuer == issuer,
            RegistrationRequest.subject == subject,
        )
        .order_by(RegistrationRequest.created_at.desc(), RegistrationRequest.id)
        .limit(1)
        .with_for_update()
    )
    now = await database_now(session)
    if (
        row is not None
        and row.status == RegistrationRequestStatus.PENDING.value
        and now >= as_utc(row.expires_at)
    ):
        expire_request(row, now=now)
        audit(
            session,
            event_type="registration.request.expired",
            resource_type="registration_request",
            resource_id=row.id,
            result_code="expired_on_read",
            changed_fields=("status",),
        )
        await session.flush()
    return row


async def approve_request(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    expected_issuer: str,
    username: str | None = None,
) -> AdmissionResult:
    """Approve exactly one pending request and atomically provision its member."""

    await acquire_identity_governance_lock(session)
    await require_operator(session, actor_user_id=reviewer_user_id)
    await require_mode(session, RegistrationMode.ADMIN_APPROVED)
    row = await session.scalar(
        select(RegistrationRequest)
        .where(RegistrationRequest.id == request_id)
        .with_for_update()
    )
    if row is None or row.status != RegistrationRequestStatus.PENDING.value:
        raise AdmissionStateError("registration request is no longer pending")
    now = await database_now(session)
    if now >= as_utc(row.expires_at):
        expire_request(row, now=now)
        audit(
            session,
            actor_user_id=reviewer_user_id,
            event_type="registration.request.expired",
            resource_type="registration_request",
            resource_id=row.id,
            result_code="expired_on_approve",
            changed_fields=("status",),
        )
        await session.flush()
        raise AdmissionStateError("registration request has expired")
    if (
        row.issuer is None
        or row.subject is None
        or row.verified_email is None
        or row.normalized_verified_email is None
        or row.account_kind != RegistrationAccountKind.MEMBER.value
    ):
        raise AdmissionStateError("registration request is incomplete")
    try:
        trusted_issuer, _subject = clean_identity_pair(expected_issuer, row.subject)
    except AdmissionValidationError as exc:
        raise AdmissionStateError("configured identity provider is invalid") from exc
    if row.issuer != trusted_issuer:
        raise AdmissionStateError(
            "registration request belongs to a different identity provider"
        )
    mailbox = validate_verified_email(row.verified_email, email_verified=True)
    if mailbox.lookup_key != row.normalized_verified_email:
        raise AdmissionStateError("registration request email snapshot is inconsistent")

    result = await provision_and_link(
        session,
        admission_id=row.id,
        account_kind_value=RegistrationAccountKind.MEMBER,
        issuer=row.issuer,
        subject=row.subject,
        mailbox=mailbox,
        persist_email_proof=False,
        verified_at=None,
        preferred_username_value=username or row.preferred_username,
        authenticated_at=None,
        assigned_by_user_id=reviewer_user_id,
    )
    row.status = RegistrationRequestStatus.APPROVED.value
    row.reviewer_user_id = reviewer_user_id
    row.reviewed_at = now
    row.provisioned_user_id = result.user.id
    audit(
        session,
        actor_user_id=reviewer_user_id,
        event_type="registration.request.approved",
        resource_type="registration_request",
        resource_id=row.id,
        result_code="member_account_provisioned",
        changed_fields=("status", "federated_identity", "roles", "subject"),
    )
    await session.flush()
    return result


def _reason(raw: object) -> str:
    if not isinstance(raw, str):
        raise AdmissionValidationError("rejection reason must be a string")
    value = unicodedata.normalize("NFKC", raw).strip()
    if not value or len(value) > 2000:
        raise AdmissionValidationError(
            "rejection reason must contain between 1 and 2000 characters"
        )
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise AdmissionValidationError(
            "rejection reason must not contain control characters"
        )
    return value


async def reject_request(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    reason: str,
) -> RegistrationRequest:
    """Reject exactly one pending request, retaining a bounded operator reason."""

    note = _reason(reason)
    await acquire_identity_governance_lock(session)
    await require_operator(session, actor_user_id=reviewer_user_id)
    row = await session.scalar(
        select(RegistrationRequest)
        .where(RegistrationRequest.id == request_id)
        .with_for_update()
    )
    if row is None or row.status != RegistrationRequestStatus.PENDING.value:
        raise AdmissionStateError("registration request is no longer pending")
    now = await database_now(session)
    if now >= as_utc(row.expires_at):
        expire_request(row, now=now)
        audit(
            session,
            actor_user_id=reviewer_user_id,
            event_type="registration.request.expired",
            resource_type="registration_request",
            resource_id=row.id,
            result_code="expired_on_reject",
            changed_fields=("status",),
        )
        await session.flush()
        raise AdmissionStateError("registration request has expired")
    row.status = RegistrationRequestStatus.REJECTED.value
    row.reviewer_user_id = reviewer_user_id
    row.reviewed_at = now
    row.review_note = note
    audit(
        session,
        actor_user_id=reviewer_user_id,
        event_type="registration.request.rejected",
        resource_type="registration_request",
        resource_id=row.id,
        result_code="rejected",
        changed_fields=("status", "review_note"),
    )
    await session.flush()
    return row


__all__ = ["approve_request", "get_request", "reject_request", "submit_request"]
