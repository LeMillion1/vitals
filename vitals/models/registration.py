"""Fail-closed persistence contracts for future account admission workflows.

These tables do not authorize registration.  They preserve the minimum state a
later invitation or operator-approval service needs while registration remains
closed at the service boundary.  Applicant identity and contact fields have an
explicit purge lifecycle: only terminal rows may be scrubbed, and a scrub
removes every field used to recognize or contact the applicant while retaining
the opaque outcome and its timestamps.  The later maintenance workflow owns the
retention window; this schema does not pretend that expiry alone performs it.

Every user foreign key is ``RESTRICT`` deliberately while it is present.
Account erasure must first run the future admission scrub/unlink workflow, which
nulls those references only together with the terminal row's applicant data,
instead of silently destroying governance history.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vitals.enums import (
    RegistrationAccountKind,
    RegistrationInvitationStatus,
    RegistrationRequestStatus,
)
from vitals.models.base import Base
from vitals.models.identity import User, _created_at, _updated_at, _uuid_pk, _values

_LOWERCASE_SHA256_CHECK = (
    "length(token_digest) = 64 AND lower(token_digest) = token_digest AND "
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "token_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
    "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
    "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''"
)


class RegistrationInvitation(Base):
    """One expiring invitation to create a non-privileged account."""

    __tablename__ = "registration_invitations"
    __table_args__ = (
        UniqueConstraint(
            "token_digest", name="uq_registration_invitations_token_digest"
        ),
        UniqueConstraint(
            "issuance_request_digest",
            name="uq_registration_invitations_issuance_request_digest",
        ),
        CheckConstraint(
            f"token_digest IS NULL OR ({_LOWERCASE_SHA256_CHECK})",
            name="ck_registration_invitations_token_digest",
        ),
        CheckConstraint(
            "issuance_request_digest IS NULL OR ("
            f"{_LOWERCASE_SHA256_CHECK.replace('token_digest', 'issuance_request_digest')}"
            ")",
            name="ck_registration_invitations_issuance_request_digest",
        ),
        CheckConstraint(
            "normalized_email IS NULL OR "
            "(length(trim(normalized_email)) > 0 "
            "AND length(normalized_email) <= 320)",
            name="ck_registration_invitations_normalized_email",
        ),
        CheckConstraint(
            f"account_kind IN ({_values(RegistrationAccountKind)})",
            name="ck_registration_invitations_account_kind",
        ),
        CheckConstraint(
            f"status IN ({_values(RegistrationInvitationStatus)})",
            name="ck_registration_invitations_status",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_registration_invitations_expiry",
        ),
        CheckConstraint(
            "(status = 'pending' "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'consumed' "
            "AND consumed_at IS NOT NULL AND consumed_at >= created_at "
            "AND ((purged_at IS NULL AND consumed_by_user_id IS NOT NULL) "
            "OR (purged_at IS NOT NULL AND consumed_by_user_id IS NULL)) "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'revoked' "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_at IS NOT NULL AND revoked_at >= created_at "
            "AND ((purged_at IS NULL AND revoked_by_user_id IS NOT NULL) "
            "OR (purged_at IS NOT NULL AND revoked_by_user_id IS NULL)) "
            "AND expired_at IS NULL) OR "
            "(status = 'expired' "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL "
            "AND expired_at IS NOT NULL AND expired_at >= expires_at)",
            name="ck_registration_invitations_state",
        ),
        CheckConstraint(
            "(purged_at IS NULL "
            "AND token_digest IS NOT NULL AND normalized_email IS NOT NULL "
            "AND invited_by_user_id IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND status <> 'pending' "
            "AND purged_at >= created_at "
            "AND token_digest IS NULL AND normalized_email IS NULL "
            "AND invited_by_user_id IS NULL "
            "AND consumed_by_user_id IS NULL "
            "AND revoked_by_user_id IS NULL)",
            name="ck_registration_invitations_purge",
        ),
        CheckConstraint(
            "purged_at IS NULL OR "
            "(status = 'consumed' AND purged_at >= consumed_at) OR "
            "(status = 'revoked' AND purged_at >= revoked_at) OR "
            "(status = 'expired' AND purged_at >= expired_at)",
            name="ck_registration_invitations_purge_time",
        ),
        Index(
            "uq_registration_invitations_live_email",
            "normalized_email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index(
            "ix_registration_invitations_inviter_status",
            "invited_by_user_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_registration_invitations_status_expiry",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    token_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    issuance_request_digest: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    normalized_email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True
    )
    account_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    invited_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=RegistrationInvitationStatus.PENDING.value,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    invited_by: Mapped[Optional[User]] = relationship(
        foreign_keys=[invited_by_user_id]
    )
    consumed_by: Mapped[Optional[User]] = relationship(
        foreign_keys=[consumed_by_user_id]
    )
    revoked_by: Mapped[Optional[User]] = relationship(
        foreign_keys=[revoked_by_user_id]
    )


class RegistrationRequest(Base):
    """One provider admission request with an accountable lifecycle."""

    __tablename__ = "registration_requests"
    __table_args__ = (
        CheckConstraint(
            "issuer IS NULL OR "
            "(length(trim(issuer)) > 0 AND length(issuer) <= 512)",
            name="ck_registration_requests_issuer",
        ),
        CheckConstraint(
            "subject IS NULL OR "
            "(length(trim(subject)) > 0 AND length(subject) <= 255)",
            name="ck_registration_requests_subject",
        ),
        CheckConstraint(
            "(verified_email IS NULL AND normalized_verified_email IS NULL) OR "
            "(verified_email IS NOT NULL "
            "AND normalized_verified_email IS NOT NULL "
            "AND length(trim(verified_email)) > 0 "
            "AND length(verified_email) <= 320 "
            "AND length(trim(normalized_verified_email)) > 0 "
            "AND length(normalized_verified_email) <= 320)",
            name="ck_registration_requests_verified_email_pair",
        ),
        CheckConstraint(
            "preferred_username IS NULL OR "
            "(length(trim(preferred_username)) > 0 "
            "AND length(preferred_username) <= 128)",
            name="ck_registration_requests_preferred_username",
        ),
        CheckConstraint(
            f"account_kind IN ({_values(RegistrationAccountKind)})",
            name="ck_registration_requests_account_kind",
        ),
        CheckConstraint(
            f"status IN ({_values(RegistrationRequestStatus)})",
            name="ck_registration_requests_status",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_registration_requests_expiry",
        ),
        CheckConstraint(
            "last_seen_at >= created_at",
            name="ck_registration_requests_last_seen",
        ),
        CheckConstraint(
            "(status = 'pending' "
            "AND reviewer_user_id IS NULL AND reviewed_at IS NULL "
            "AND provisioned_user_id IS NULL AND review_note IS NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'approved' "
            "AND reviewed_at IS NOT NULL AND reviewed_at >= created_at "
            "AND ((purged_at IS NULL AND reviewer_user_id IS NOT NULL "
            "AND provisioned_user_id IS NOT NULL) "
            "OR (purged_at IS NOT NULL AND reviewer_user_id IS NULL "
            "AND provisioned_user_id IS NULL)) "
            "AND review_note IS NULL "
            "AND expired_at IS NULL) OR "
            "(status = 'rejected' "
            "AND reviewed_at IS NOT NULL AND reviewed_at >= created_at "
            "AND ((purged_at IS NULL AND reviewer_user_id IS NOT NULL) "
            "OR (purged_at IS NOT NULL AND reviewer_user_id IS NULL)) "
            "AND provisioned_user_id IS NULL "
            "AND ((purged_at IS NULL AND review_note IS NOT NULL "
            "AND length(trim(review_note)) > 0 "
            "AND length(review_note) <= 2000) "
            "OR (purged_at IS NOT NULL AND review_note IS NULL)) "
            "AND expired_at IS NULL) OR "
            "(status = 'expired' "
            "AND reviewer_user_id IS NULL AND reviewed_at IS NULL "
            "AND provisioned_user_id IS NULL AND review_note IS NULL "
            "AND expired_at IS NOT NULL AND expired_at >= expires_at)",
            name="ck_registration_requests_state",
        ),
        CheckConstraint(
            "(purged_at IS NULL AND issuer IS NOT NULL AND subject IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND status <> 'pending' "
            "AND purged_at >= created_at "
            "AND issuer IS NULL AND subject IS NULL "
            "AND verified_email IS NULL AND normalized_verified_email IS NULL "
            "AND preferred_username IS NULL AND review_note IS NULL "
            "AND reviewer_user_id IS NULL AND provisioned_user_id IS NULL)",
            name="ck_registration_requests_purge",
        ),
        CheckConstraint(
            "purged_at IS NULL OR "
            "(status IN ('approved', 'rejected') AND purged_at >= reviewed_at) OR "
            "(status = 'expired' AND purged_at >= expired_at)",
            name="ck_registration_requests_purge_time",
        ),
        Index(
            "uq_registration_requests_issuer_subject",
            "issuer",
            "subject",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index(
            "ix_registration_requests_status_created",
            "status",
            "created_at",
        ),
        Index(
            "ix_registration_requests_status_expiry",
            "status",
            "expires_at",
        ),
        Index(
            "ix_registration_requests_reviewer_reviewed",
            "reviewer_user_id",
            "reviewed_at",
        ),
        Index(
            "ix_registration_requests_provisioned_user",
            "provisioned_user_id",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    issuer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verified_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    normalized_verified_email: Mapped[Optional[str]] = mapped_column(
        String(320), nullable=True
    )
    preferred_username: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    account_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=RegistrationRequestStatus.PENDING.value,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewer_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provisioned_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    reviewer: Mapped[Optional[User]] = relationship(
        foreign_keys=[reviewer_user_id]
    )
    provisioned_user: Mapped[Optional[User]] = relationship(
        foreign_keys=[provisioned_user_id]
    )
