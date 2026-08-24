"""A place for a patient and their care team to talk, with the patient in it.

Revision ID: 0061
Revises: 0060
Create Date: 2026-08-24

Three tables, and the shape carries the decision. A thread belongs to the
subject; the subject is in it from the moment it exists; every message in it is
one they can read. A hidden doctor-to-trainer channel is a different product
with a different privacy and legal answer, and this schema cannot express one —
there is no thread without a subject and no participant list the subject is
absent from.

**Every child carries its own ``subject_id``, and a composite foreign key makes
the two agree.** ``care_threads`` gains ``uq_care_threads_id_subject`` for that,
the same mechanism ``integration_connections`` provides for its credential in
revision 0060. Row security needs a column on the row it is policing, and a
denormalized one that could drift from its parent would be worse than none: a
message filed under somebody else's thread would be invisible to its own
patient and visible to another. The constraint is why it cannot happen.

``care_thread_participants.relationship_id`` is ``NULL`` for the patient, who is
in the room as its subject, and set for a professional — naming the care they
were added under, so a conversation read back a year later says which
relationship each person was speaking from. ``ON DELETE RESTRICT`` throughout:
care ends, the record of it does not.

Nothing here has a delete path, in the schema or above it. ``removed_at`` on a
participant and ``edited_at`` on a message are how leaving and correcting are
recorded — a clinical conversation somebody can make disappear is a worse record
than one that stays and is superseded, and the patient cannot review a history
they cannot see.

``downgrade`` drops all three, and with them every message. That is stated
rather than left to be found: there is no other copy, and no ordinary export
carries them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: Union[str, None] = "0060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = (
    "care_threads",
    "care_thread_participants",
    "care_messages",
)

SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"(subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid "
    f"OR current_setting('{PLATFORM_SETTING}', true) = 'on')"
)


def _subject_column() -> sa.Column:
    return sa.Column(
        "subject_id",
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "care_threads",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        _subject_column(),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column(
            "opened_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        *_timestamps(),
        sa.UniqueConstraint("id", "subject_id", name="uq_care_threads_id_subject"),
        sa.CheckConstraint(
            "status IN ('open', 'closed')", name="ck_care_threads_status"
        ),
        sa.CheckConstraint(
            "length(trim(title)) > 0 AND length(title) <= 200",
            name="ck_care_threads_title",
        ),
    )
    op.create_index(
        "ix_care_threads_subject_updated", "care_threads", ["subject_id", "updated_at"]
    )

    op.create_table(
        "care_thread_participants",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("thread_id", sa.Uuid(as_uuid=True), nullable=False),
        _subject_column(),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "relationship_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("care_relationships.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["thread_id", "subject_id"],
            ["care_threads.id", "care_threads.subject_id"],
            name="fk_care_thread_participants_thread_subject",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "thread_id", "user_id", name="uq_care_thread_participants_thread_user"
        ),
        sa.CheckConstraint(
            "(relationship_id IS NULL) OR "
            "(removed_at IS NULL OR removed_at > joined_at)",
            name="ck_care_thread_participants_removal",
        ),
    )
    op.create_index(
        "ix_care_thread_participants_subject_user",
        "care_thread_participants",
        ["subject_id", "user_id"],
    )
    op.create_index(
        "ix_care_thread_participants_thread",
        "care_thread_participants",
        ["thread_id"],
    )

    op.create_table(
        "care_messages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("thread_id", sa.Uuid(as_uuid=True), nullable=False),
        _subject_column(),
        sa.Column(
            "actor_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["thread_id", "subject_id"],
            ["care_threads.id", "care_threads.subject_id"],
            name="fk_care_messages_thread_subject",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(trim(body)) > 0 AND length(body) <= 20000",
            name="ck_care_messages_body",
        ),
        sa.CheckConstraint(
            "edited_at IS NULL OR edited_at >= created_at",
            name="ck_care_messages_edit_time",
        ),
    )
    for name, columns in (
        ("ix_care_messages_thread_created", ["thread_id", "created_at"]),
        ("ix_care_messages_subject_created", ["subject_id", "created_at"]),
        ("ix_care_messages_actor_created", ["actor_user_id", "created_at"]),
    ):
        op.create_index(name, "care_messages", columns)

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table_name in SUBJECT_ISOLATED_TABLES:
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "{table_name}" '
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    """Drops all three, and every message with them.

    Stated rather than left to be discovered. There is no other copy: no
    ordinary export carries a care-team conversation, deliberately — it holds
    what a doctor and a trainer wrote, which is not the patient's alone to hand
    out.
    """

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table_name in SUBJECT_ISOLATED_TABLES:
            op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')

    for name in (
        "ix_care_messages_actor_created",
        "ix_care_messages_subject_created",
        "ix_care_messages_thread_created",
    ):
        op.drop_index(name, table_name="care_messages")
    op.drop_table("care_messages")

    for name in (
        "ix_care_thread_participants_thread",
        "ix_care_thread_participants_subject_user",
    ):
        op.drop_index(name, table_name="care_thread_participants")
    op.drop_table("care_thread_participants")

    op.drop_index("ix_care_threads_subject_updated", table_name="care_threads")
    op.drop_table("care_threads")
