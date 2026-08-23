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
    Integer,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vitals.enums import (
    CareRelationshipStatus,
    ConsentStatus,
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


class CareRelationship(Base):
    """One professional currently in care for one patient, or formerly.

    Half of what access needs. The other half is a consent, and neither is
    sufficient: a relationship with no live consent is somebody the patient
    agreed to work with and has not yet — or no longer — agreed to show
    anything to.

    ``kind`` is stored here rather than read from the professional's profile.
    Somebody who is both a doctor and a trainer holds one profile, and the
    defaults each kind gets differ by domain; taking the kind from the profile
    would let a trainer relationship silently inherit a doctor's reach.

    ``paused`` and ``ended`` are different states on purpose. A pause is the
    patient stepping back and resuming must not need a new invitation; an end is
    an end, and coming back is a fresh offer they have to make again.
    """

    __tablename__ = "care_relationships"
    __table_args__ = (
        # At most one live relationship per pair. A second would mean two sets
        # of consent for one person and no rule about which applies.
        Index(
            "uq_care_relationships_live_pair",
            "subject_id",
            "professional_user_id",
            unique=True,
            sqlite_where=text("status <> 'ended'"),
            postgresql_where=text("status <> 'ended'"),
        ),
        CheckConstraint(
            f"kind IN ({_values(ProfessionalKind)})",
            name="ck_care_relationships_kind",
        ),
        CheckConstraint(
            f"status IN ({_values(CareRelationshipStatus)})",
            name="ck_care_relationships_status",
        ),
        CheckConstraint(
            "subject_owner_user_id <> professional_user_id",
            name="ck_care_relationships_two_parties",
        ),
        CheckConstraint(
            "(status = 'ended' AND ended_at IS NOT NULL) OR "
            "(status <> 'ended' AND ended_at IS NULL)",
            name="ck_care_relationships_ended_state",
        ),
        Index(
            "ix_care_relationships_subject_status",
            "subject_id",
            "status",
        ),
        Index(
            "ix_care_relationships_professional_status",
            "professional_user_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: Denormalized so the two-parties check can be a database constraint rather
    #: than a rule the application remembers to apply. A relationship naming one
    #: person twice has no second party to consent to anything.
    subject_owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    professional_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=CareRelationshipStatus.ACTIVE.value
    )
    #: How it started, when it started that way. Nullable because a relationship
    #: may also be established by an operator repairing one.
    invitation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("professional_invitations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    established_at: Mapped[datetime] = _created_at()
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    professional: Mapped[User] = relationship(foreign_keys=[professional_user_id])
    consents: Mapped[list["ConsentGrant"]] = relationship(
        back_populates="care_relationship",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ConsentGrant(Base):
    """One version of what a patient agreed this professional may see.

    Versioned rather than edited. Narrowing what somebody may read is a new
    version superseding the old, so "what was this professional allowed to see
    on the day they read it" stays answerable — which is the question any later
    dispute is actually about, and one an updated row cannot answer.

    Exactly one version per relationship may be live at a time. That is a
    database constraint rather than a convention, because two live versions
    would mean the wider of them silently wins.
    """

    __tablename__ = "consent_grants"
    __table_args__ = (
        UniqueConstraint(
            "relationship_id", "version", name="uq_consent_grants_relationship_version"
        ),
        Index(
            "uq_consent_grants_live_version",
            "relationship_id",
            unique=True,
            sqlite_where=text("status IN ('active', 'paused')"),
            postgresql_where=text("status IN ('active', 'paused')"),
        ),
        CheckConstraint(
            f"status IN ({_values(ConsentStatus)})",
            name="ck_consent_grants_status",
        ),
        CheckConstraint("version >= 1", name="ck_consent_grants_version_positive"),
        CheckConstraint(
            "expires_at > granted_at", name="ck_consent_grants_positive_ttl"
        ),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_consent_grants_revoked_state",
        ),
        CheckConstraint(
            "(status = 'paused' AND paused_at IS NOT NULL) OR "
            "(status <> 'paused' AND paused_at IS NULL)",
            name="ck_consent_grants_paused_state",
        ),
        Index(
            "ix_consent_grants_subject_status_expires",
            "subject_id",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    relationship_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("care_relationships.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Repeated from the relationship so row security can see it. A consent is
    #: the patient's row, and reaching it through a join would put it outside
    #: the policy that protects everything else of theirs.
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=ConsentStatus.ACTIVE.value
    )
    granted_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    paused_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    # Not named ``relationship``: that would shadow SQLAlchemy's own
    # ``relationship()`` for the rest of the class body, and the next attribute
    # to use it fails with an error that names neither.
    care_relationship: Mapped[CareRelationship] = relationship(
        back_populates="consents"
    )
    scopes: Mapped[list["ConsentScope"]] = relationship(
        back_populates="grant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ConsentScope(Base):
    """One exact resource/action pair a consent version allows.

    Wildcards are forbidden, exactly as they are for support access: broad
    permission is a longer list of concrete keys, so that reading the row tells
    you what it permits without having to know what the catalog contained on the
    day it was written.
    """

    __tablename__ = "consent_scopes"
    __table_args__ = (
        UniqueConstraint(
            "consent_grant_id",
            "resource_type",
            "resource_key",
            "action",
            name="uq_consent_scopes_grant_resource_action",
        ),
        CheckConstraint(
            "resource_type IN ('domain', 'artifact', 'operation')",
            name="ck_consent_scopes_resource_type",
        ),
        CheckConstraint(
            "action IN ('read', 'list', 'search', 'create', 'update', 'delete', "
            "'attach', 'share', 'export', 'sync', 'message', 'repair')",
            name="ck_consent_scopes_action",
        ),
        CheckConstraint(
            "length(trim(resource_key)) > 0 AND length(resource_key) <= 128",
            name="ck_consent_scopes_resource_key",
        ),
        CheckConstraint(
            "resource_key NOT LIKE '%*%'", name="ck_consent_scopes_no_wildcard"
        ),
        Index(
            "ix_consent_scopes_resource",
            "resource_type",
            "resource_key",
            "action",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    consent_grant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("consent_grants.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Inherited from the grant so row security covers this table too, on the
    #: same terms as every other child of a subject-owned row.
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    grant: Mapped[ConsentGrant] = relationship(back_populates="scopes")


__all__ = [
    "CareRelationship",
    "ConsentGrant",
    "ConsentScope",
    "ProfessionalInvitation",
    "ProfessionalProfile",
]
