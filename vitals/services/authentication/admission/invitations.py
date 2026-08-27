"""Email-bound, one-time account invitations."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import RegistrationAccountKind, RegistrationInvitationStatus
from vitals.models.identity import User
from vitals.models.registration import RegistrationInvitation
from vitals.services.authentication.admission._shared import (
    INVITATION_TTL,
    MAX_INVITATION_TTL,
    TOKEN_BYTES,
    AdmissionRefused,
    AdmissionResult,
    AdmissionReplayError,
    AdmissionStateError,
    AdmissionValidationError,
    IssuedInvitation,
    account_kind as resolve_account_kind,
    as_utc,
    audit,
    bounded_ttl,
    clean_identity_pair,
    database_now,
    expire_invitation,
    presented_token,
    provision_and_link,
    require_mode,
    require_operator,
    token_digest,
    verified_email as validate_verified_email,
)
from vitals.services.authentication.registration import RegistrationMode
from vitals.services.identity.contracts import IdentityValidationError
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.identity.normalization import NormalizedEmail, normalize_email


async def issue_invitation(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    email: str,
    account_kind: RegistrationAccountKind | str,
    ttl: timedelta = INVITATION_TTL,
    issuance_request_digest: str | None = None,
) -> IssuedInvitation:
    """Issue one email-bound token, revoking any prior live token for the email."""

    ttl = bounded_ttl(ttl, name="ttl", maximum=MAX_INVITATION_TTL)
    kind = resolve_account_kind(account_kind)
    try:
        mailbox = normalize_email(email)
    except IdentityValidationError as exc:
        raise AdmissionValidationError(str(exc)) from exc
    if issuance_request_digest is not None and (
        len(issuance_request_digest) != 64
        or issuance_request_digest != issuance_request_digest.casefold()
        or any(char not in "0123456789abcdef" for char in issuance_request_digest)
    ):
        raise AdmissionValidationError("issuance request digest must be SHA-256")

    await acquire_identity_governance_lock(session)
    await require_operator(session, actor_user_id=actor_user_id)
    if issuance_request_digest is not None:
        repeated = await session.scalar(
            select(RegistrationInvitation.id)
            .where(
                RegistrationInvitation.issuance_request_digest
                == issuance_request_digest
            )
            .with_for_update()
        )
        if repeated is not None:
            raise AdmissionReplayError("this invitation request already completed")
    await require_mode(session, RegistrationMode.INVITE_ONLY)

    email_owner = await session.scalar(
        select(User.id).where(User.normalized_email == mailbox.lookup_key)
    )
    if email_owner is not None:
        raise AdmissionValidationError(
            "a local account already uses that email address"
        )

    old_rows = list(
        await session.scalars(
            select(RegistrationInvitation)
            .where(
                RegistrationInvitation.normalized_email == mailbox.lookup_key,
                RegistrationInvitation.status
                == RegistrationInvitationStatus.PENDING.value,
            )
            .with_for_update()
        )
    )
    now = await database_now(session)
    for old in old_rows:
        if now >= as_utc(old.expires_at):
            expire_invitation(old, now=now)
            event_type = "registration.invitation.expired"
            result_code = "expired_on_reissue"
        else:
            old.status = RegistrationInvitationStatus.REVOKED.value
            old.revoked_by_user_id = actor_user_id
            old.revoked_at = now
            event_type = "registration.invitation.revoked"
            result_code = "superseded"
        audit(
            session,
            actor_user_id=actor_user_id,
            event_type=event_type,
            resource_type="registration_invitation",
            resource_id=old.id,
            result_code=result_code,
            changed_fields=("status",),
        )
    if old_rows:
        await session.flush()

    token = secrets.token_urlsafe(TOKEN_BYTES)
    invitation = RegistrationInvitation(
        token_digest=token_digest(token),
        issuance_request_digest=issuance_request_digest,
        normalized_email=mailbox.lookup_key,
        account_kind=kind.value,
        invited_by_user_id=actor_user_id,
        status=RegistrationInvitationStatus.PENDING.value,
        expires_at=now + ttl,
    )
    session.add(invitation)
    await session.flush()
    audit(
        session,
        actor_user_id=actor_user_id,
        event_type="registration.invitation.issued",
        resource_type="registration_invitation",
        resource_id=invitation.id,
        result_code="issued",
        changed_fields=("status", "account_kind", "expires_at"),
    )
    await session.flush()
    return IssuedInvitation(invitation=invitation, token=token)


async def revoke_invitation(
    session: AsyncSession,
    *,
    invitation_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> RegistrationInvitation:
    """Revoke one pending invitation. Closure never prevents revocation."""

    await acquire_identity_governance_lock(session)
    await require_operator(session, actor_user_id=actor_user_id)
    row = await session.scalar(
        select(RegistrationInvitation)
        .where(RegistrationInvitation.id == invitation_id)
        .with_for_update()
    )
    if row is None:
        raise AdmissionStateError("registration invitation does not exist")
    if row.status != RegistrationInvitationStatus.PENDING.value:
        raise AdmissionStateError("registration invitation is no longer pending")
    now = await database_now(session)
    if now >= as_utc(row.expires_at):
        expire_invitation(row, now=now)
        audit(
            session,
            actor_user_id=actor_user_id,
            event_type="registration.invitation.expired",
            resource_type="registration_invitation",
            resource_id=row.id,
            result_code="expired_on_revoke",
            changed_fields=("status",),
        )
        await session.flush()
        raise AdmissionStateError("registration invitation has expired")
    row.status = RegistrationInvitationStatus.REVOKED.value
    row.revoked_by_user_id = actor_user_id
    row.revoked_at = now
    audit(
        session,
        actor_user_id=actor_user_id,
        event_type="registration.invitation.revoked",
        resource_type="registration_invitation",
        resource_id=row.id,
        result_code="revoked",
        changed_fields=("status",),
    )
    await session.flush()
    return row


async def claim_invitation(session: AsyncSession, *, token: str) -> uuid.UUID:
    """Exchange a bearer once for its opaque invitation id.

    The caller may put that id in a short-lived, signed browser handoff. The id
    is not authority by itself: final consumption still requires the signed
    handoff and rechecks mode, pending state, expiry, and the provider's verified
    address under the same lock. The emailed link remains retryable until final
    consumption so link scanners or a lost Set-Cookie response cannot strand a
    valid invitation.
    """

    token = presented_token(token)
    await acquire_identity_governance_lock(session)
    await require_mode(session, RegistrationMode.INVITE_ONLY)
    row = await session.scalar(
        select(RegistrationInvitation)
        .where(RegistrationInvitation.token_digest == token_digest(token))
        .with_for_update()
    )
    if row is None or row.status != RegistrationInvitationStatus.PENDING.value:
        raise AdmissionRefused("this admission proof does not open an account")
    now = await database_now(session)
    if now >= as_utc(row.expires_at):
        expire_invitation(row, now=now)
        audit(
            session,
            event_type="registration.invitation.expired",
            resource_type="registration_invitation",
            resource_id=row.id,
            result_code="expired_on_claim",
            changed_fields=("status",),
        )
        await session.flush()
        raise AdmissionRefused("this admission proof does not open an account")
    return row.id


async def _consume_row(
    session: AsyncSession,
    *,
    row: RegistrationInvitation | None,
    issuer: str,
    subject: str,
    mailbox: NormalizedEmail,
    preferred_username: str | None,
    authenticated_at: datetime | None,
) -> AdmissionResult:
    if row is None or row.status != RegistrationInvitationStatus.PENDING.value:
        raise AdmissionRefused("this admission proof does not open an account")
    now = await database_now(session)
    if now >= as_utc(row.expires_at):
        expire_invitation(row, now=now)
        audit(
            session,
            event_type="registration.invitation.expired",
            resource_type="registration_invitation",
            resource_id=row.id,
            result_code="expired_on_consume",
            changed_fields=("status",),
        )
        await session.flush()
        raise AdmissionRefused("this admission proof does not open an account")
    if row.normalized_email is None or not secrets.compare_digest(
        mailbox.lookup_key, row.normalized_email
    ):
        raise AdmissionRefused("this admission proof does not open an account")

    kind = resolve_account_kind(row.account_kind)
    result = await provision_and_link(
        session,
        admission_id=row.id,
        account_kind_value=kind,
        issuer=issuer,
        subject=subject,
        mailbox=mailbox,
        persist_email_proof=True,
        verified_at=now,
        preferred_username_value=preferred_username,
        authenticated_at=authenticated_at,
        assigned_by_user_id=row.invited_by_user_id,
    )
    result.user.last_login_at = now
    row.status = RegistrationInvitationStatus.CONSUMED.value
    row.consumed_by_user_id = result.user.id
    row.consumed_at = now
    audit(
        session,
        event_type="registration.invitation.consumed",
        resource_type="registration_invitation",
        resource_id=row.id,
        result_code=f"{kind.value}_account_provisioned",
        changed_fields=("status", "federated_identity", "roles"),
    )
    await session.flush()
    return result


async def consume_invitation(
    session: AsyncSession,
    *,
    token: str,
    issuer: str,
    subject: str,
    verified_email: str,
    email_verified: bool,
    preferred_username: str | None = None,
    authenticated_at: datetime | None = None,
) -> AdmissionResult:
    """Consume one current, address-bound invitation and atomically link OIDC."""

    token = presented_token(token)
    try:
        issuer, subject = clean_identity_pair(issuer, subject)
    except AdmissionValidationError as exc:
        raise AdmissionRefused("this admission proof does not open an account") from exc
    mailbox = validate_verified_email(
        verified_email, email_verified=email_verified
    )

    await acquire_identity_governance_lock(session)
    await require_mode(session, RegistrationMode.INVITE_ONLY)
    row = await session.scalar(
        select(RegistrationInvitation)
        .where(RegistrationInvitation.token_digest == token_digest(token))
        .with_for_update()
    )
    return await _consume_row(
        session,
        row=row,
        issuer=issuer,
        subject=subject,
        mailbox=mailbox,
        preferred_username=preferred_username,
        authenticated_at=authenticated_at,
    )


async def consume_invitation_claim(
    session: AsyncSession,
    *,
    invitation_id: uuid.UUID,
    issuer: str,
    subject: str,
    verified_email: str,
    email_verified: bool,
    preferred_username: str | None = None,
    authenticated_at: datetime | None = None,
) -> AdmissionResult:
    """Consume the invitation named by a signed browser handoff."""

    if not isinstance(invitation_id, uuid.UUID) or invitation_id.int == 0:
        raise AdmissionRefused("this admission proof does not open an account")
    try:
        issuer, subject = clean_identity_pair(issuer, subject)
    except AdmissionValidationError as exc:
        raise AdmissionRefused("this admission proof does not open an account") from exc
    mailbox = validate_verified_email(
        verified_email, email_verified=email_verified
    )

    await acquire_identity_governance_lock(session)
    await require_mode(session, RegistrationMode.INVITE_ONLY)
    row = await session.scalar(
        select(RegistrationInvitation)
        .where(RegistrationInvitation.id == invitation_id)
        .with_for_update()
    )
    return await _consume_row(
        session,
        row=row,
        issuer=issuer,
        subject=subject,
        mailbox=mailbox,
        preferred_username=preferred_username,
        authenticated_at=authenticated_at,
    )


__all__ = [
    "claim_invitation",
    "consume_invitation",
    "consume_invitation_claim",
    "issue_invitation",
    "revoke_invitation",
]
