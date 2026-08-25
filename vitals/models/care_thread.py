"""Where a patient and their care team talk, with the patient in the room.

The safe first communication feature, and the shape is the whole point. A thread
belongs to the subject, the subject is a participant in it from the moment it
exists and cannot be removed, and every message in it is one the patient can
read. A hidden doctor-to-trainer channel is a different product with a different
privacy and legal answer, and it is deliberately not this one — see the decision
log in ``docs/COMMERCIAL_MULTI_USER_ROADMAP.md``.

**Being in the room is a row, not an inference.** A professional participates
because somebody added them, and that row records the care relationship it was
added under. Deriving participation from "has an active relationship" would mean
a doctor taken on last week silently joining a conversation that started before
them, and a doctor whose care ended silently vanishing from a history the
patient can still see. Both are wrong: the first is a disclosure and the second
is a record that changes behind the reader.

**Authorization is asked at every read and every send**, and is not this
table's job. A participant row says somebody was let in; ``consent_scopes`` says
whether they may still speak or listen today, and the patient can withdraw that
without the conversation being deleted.

Nothing here is deletable. A message is edited in place with its authorship and
its edit time intact, for the same reason a professional's note is: a clinical
conversation somebody can make disappear is a worse record than one that stays.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vitals.enums import CareThreadStatus
from vitals.models.base import Base
from vitals.models.identity import User


def _values(enum_type: type) -> str:
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


class CareThread(Base):
    """One conversation about one patient."""

    __tablename__ = "care_threads"
    __table_args__ = (
        # The composite key children hang off. ``care_messages`` and
        # ``care_thread_participants`` both carry their own ``subject_id`` so row
        # security has a column to police, and this is what stops the two from
        # disagreeing: a message filed under a thread that belongs to somebody
        # else cannot exist.
        UniqueConstraint("id", "subject_id", name="uq_care_threads_id_subject"),
        CheckConstraint(
            f"status IN ({_values(CareThreadStatus)})", name="ck_care_threads_status"
        ),
        CheckConstraint(
            "length(trim(title)) > 0 AND length(title) <= 200",
            name="ck_care_threads_title",
        ),
        Index("ix_care_threads_subject_updated", "subject_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Who opened it. Either the patient or a professional in live care, and
    #: kept because "who started this" is part of reading a conversation back.
    opened_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=CareThreadStatus.OPEN.value
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    opened_by: Mapped[User] = relationship(foreign_keys=[opened_by_user_id])


class CareThreadParticipant(Base):
    """One person who was let into one conversation, and under what care."""

    __tablename__ = "care_thread_participants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["thread_id", "subject_id"],
            ["care_threads.id", "care_threads.subject_id"],
            name="fk_care_thread_participants_thread_subject",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "thread_id", "user_id", name="uq_care_thread_participants_thread_user"
        ),
        # The subject participates as themselves and under no relationship;
        # everybody else is in the room because of one. Neither state is
        # inferred at read time, so the constraint is what keeps them apart.
        CheckConstraint(
            "(relationship_id IS NULL) OR (removed_at IS NULL OR removed_at > joined_at)",
            name="ck_care_thread_participants_removal",
        ),
        CheckConstraint(
            "last_read_at >= joined_at",
            name="ck_care_thread_participants_read_cursor",
        ),
        Index(
            "ix_care_thread_participants_subject_user",
            "subject_id",
            "user_id",
        ),
        Index("ix_care_thread_participants_thread", "thread_id"),
        Index(
            "ix_care_thread_participants_user_unread",
            "user_id",
            "subject_id",
            "last_read_at",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: ``NULL`` for the patient, who is in the room as its subject. Set for a
    #: professional, naming the care they were added under — so a history read
    #: back a year later says which relationship this person was speaking from.
    relationship_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("care_relationships.id", ondelete="RESTRICT"),
        nullable=True,
    )
    joined_at: Mapped[datetime] = _created_at()
    #: Per-reader cursor. It advances only to a message this participant has
    #: actually opened (or authored), never to wall-clock "now", so a message
    #: racing with a page read cannot be swallowed as already seen.
    last_read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Set rather than deleted. Somebody who left is part of the record of who
    #: was in the room when a thing was said.
    removed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    participant: Mapped[User] = relationship(foreign_keys=[user_id])


class CareMessage(Base):
    """One thing somebody said, in one conversation, about one patient."""

    __tablename__ = "care_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["thread_id", "subject_id"],
            ["care_threads.id", "care_threads.subject_id"],
            name="fk_care_messages_thread_subject",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(trim(body)) > 0 AND length(body) <= 20000",
            name="ck_care_messages_body",
        ),
        CheckConstraint(
            "edited_at IS NULL OR edited_at >= created_at",
            name="ck_care_messages_edit_time",
        ),
        Index("ix_care_messages_thread_created", "thread_id", "created_at"),
        Index("ix_care_messages_subject_created", "subject_id", "created_at"),
        Index("ix_care_messages_actor_created", "actor_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: ``actor_user_id`` rather than ``author_user_id``: the ownership registry
    #: and every sibling table name the acting person this way, and a column
    #: that means the same thing under a different name is one the contract
    #: cannot see. The relationship below is still ``author``, which is what it
    #: is to a reader.
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``NULL`` until somebody corrects what they said. Never a way to make the
    #: original disappear — the row is the same row, and the reader is told it
    #: changed.
    edited_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    author: Mapped[User] = relationship(foreign_keys=[actor_user_id])


__all__ = ["CareMessage", "CareThread", "CareThreadParticipant"]
