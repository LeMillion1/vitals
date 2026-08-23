"""Who a professional claims to be, and who checked.

A role says what kind of thing somebody is; it has never said whose record they
may reach, and it must not start now. This table exists so the *claim* has a
place to live and a lifecycle — a licence number, a person who looked at it, a
date they looked. Access is still a separate question, decided by a relationship
the patient accepted and a consent they gave.

The separation is not bureaucratic. Verification is about the world outside this
installation: is this person a doctor at all. Consent is about one patient's
record. Conflating them produces the failure where being verified anywhere means
being admitted everywhere, which is the thing a health record must never do.
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vitals.enums import (
    ProfessionalInvitationStatus,
    ProfessionalKind,
    ProfessionalVerificationStatus,
)
from vitals.models.base import Base
from vitals.models.identity import User, _created_at, _updated_at, _uuid_pk, _values


class ProfessionalProfile(Base):
    """One account's professional identity, and how far its check has got.

    ``verification_status`` is the only field anything downstream is allowed to
    read as permission-shaped, and only ``verified`` counts. The rest name the
    states a profile can sit in so that "nobody has looked at this yet" and
    "somebody looked and said no" are different answers.

    A rejection or a suspension keeps its reason, because a professional who is
    told no is entitled to know what to fix, and an operator reviewing the queue
    a year later needs to see what the last one concluded.
    """

    __tablename__ = "professional_profiles"
    __table_args__ = (
        # One professional identity per account. Somebody who is both a doctor
        # and a trainer holds one profile and two relationships; the kind that
        # applies to a patient is named by the relationship, so a single account
        # cannot silently take the wider of the two sets of defaults.
        UniqueConstraint("user_id", name="uq_professional_profiles_user_id"),
        CheckConstraint(
            f"kind IN ({_values(ProfessionalKind)})",
            name="ck_professional_profiles_kind",
        ),
        CheckConstraint(
            f"verification_status IN ({_values(ProfessionalVerificationStatus)})",
            name="ck_professional_profiles_verification_status",
        ),
        CheckConstraint(
            "length(trim(display_name)) > 0 AND length(display_name) <= 200",
            name="ck_professional_profiles_display_name",
        ),
        CheckConstraint(
            "credential_reference IS NULL OR "
            "(length(trim(credential_reference)) > 0 "
            "AND length(credential_reference) <= 200)",
            name="ck_professional_profiles_credential_reference",
        ),
        # Verified means somebody looked, and the record says who and when. A
        # verified row with no reviewer is a claim that verified itself.
        CheckConstraint(
            "(verification_status = 'verified' AND verified_at IS NOT NULL "
            "AND verified_by_user_id IS NOT NULL) OR "
            "(verification_status <> 'verified' AND verified_at IS NULL "
            "AND verified_by_user_id IS NULL)",
            name="ck_professional_profiles_verified_state",
        ),
        # A refusal keeps its reason: the professional needs to know what to
        # fix, and the next operator needs to see what the last one concluded.
        CheckConstraint(
            "(verification_status IN ('rejected', 'suspended') "
            "AND review_note IS NOT NULL AND length(trim(review_note)) > 0) OR "
            "(verification_status NOT IN ('rejected', 'suspended'))",
            name="ck_professional_profiles_refusal_reason",
        ),
        CheckConstraint(
            "review_note IS NULL OR length(review_note) <= 2000",
            name="ck_professional_profiles_review_note_length",
        ),
        Index(
            "ix_professional_profiles_status_kind",
            "verification_status",
            "kind",
        ),
        Index("ix_professional_profiles_verified_by", "verified_by_user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=ProfessionalVerificationStatus.UNVERIFIED.value,
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The licence or registration number as the professional stated it. Never a
    #: lookup key and never shown to a patient — an operator reads it once,
    #: checks it against the register that issued it, and records the verdict.
    credential_reference: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    verified_by: Mapped[Optional[User]] = relationship(
        foreign_keys=[verified_by_user_id]
    )




class ProfessionalInvitation(Base):
    """One patient's offer to let one professional into their record.

    The offer travels as a token in a link. Only its hash is stored, so a copy
    of this table is not a set of working invitations — the same reasoning the
    published-report tokens use, and for a stronger reason here, because
    accepting one creates a care relationship rather than opening a document.

    It is bound to an address as well as to a token. A link that anybody holding
    it may accept is a link that whoever it was forwarded to may accept, and the
    patient chose a person rather than a mailbox. The address is checked against
    a *verified* claim at acceptance; an unverified address is somebody saying
    they own a mailbox, which is exactly what the binding is meant to stop.

    ``subject_id`` is here because the invitation is the patient's — it is their
    record being offered — which also puts it inside row security. Accepting is
    therefore done in the platform scope: the professional is not bound to this
    subject yet, and the token is what authorizes reading the row at all.
    """

    __tablename__ = "professional_invitations"
    __table_args__ = (
        UniqueConstraint(
            "token_hash", name="uq_professional_invitations_token_hash"
        ),
        CheckConstraint(
            f"kind IN ({_values(ProfessionalKind)})",
            name="ck_professional_invitations_kind",
        ),
        CheckConstraint(
            f"status IN ({_values(ProfessionalInvitationStatus)})",
            name="ck_professional_invitations_status",
        ),
        CheckConstraint(
            "length(token_hash) = 64 AND lower(token_hash) = token_hash",
            name="ck_professional_invitations_token_hash_shape",
        ),
        CheckConstraint(
            "length(trim(invited_email)) > 0 AND length(invited_email) <= 320",
            name="ck_professional_invitations_invited_email",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_professional_invitations_positive_ttl",
        ),
        # One-time, and the record says by whom. An accepted invitation with no
        # acceptor is a state nothing in the service can produce and nothing
        # downstream could interpret.
        CheckConstraint(
            "(status = 'accepted' AND accepted_at IS NOT NULL "
            "AND accepted_by_user_id IS NOT NULL) OR "
            "(status <> 'accepted' AND accepted_at IS NULL "
            "AND accepted_by_user_id IS NULL)",
            name="ck_professional_invitations_accepted_state",
        ),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_professional_invitations_revoked_state",
        ),
        Index(
            "ix_professional_invitations_subject_status",
            "subject_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_professional_invitations_email_status",
            "invited_email",
            "status",
        ),
        Index(
            "ix_professional_invitations_accepted_by", "accepted_by_user_id"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Normalized form of the address the link was sent to. Not a lookup key for
    #: an account — accounts are found by the token — but the thing the accepting
    #: identity's verified claim has to match.
    invited_email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: SHA-256 of the token. The token itself exists once, in the link.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=ProfessionalInvitationStatus.PENDING.value,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    invited_by: Mapped[User] = relationship(foreign_keys=[invited_by_user_id])
    accepted_by: Mapped[Optional[User]] = relationship(
        foreign_keys=[accepted_by_user_id]
    )


__all__ = ["ProfessionalInvitation", "ProfessionalProfile"]
