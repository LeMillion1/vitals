"""Drop ``signals`` and ``day_context``.

Revision ID: 0058
Revises: 0057
Create Date: 2026-08-24

Both tables were filled from one place: a Telegram chat. ``signals`` held free
text the bot parsed into facts — "кофе в 22", "голова раскалывается" —
and ``day_context`` held the answers to the evening block's questions about the
day just spent. The chat is gone: one bot token and one chat id in the
environment is a single-user shape, and a shared installation cannot have it.
Web push replaces the delivery, and nothing replaces the *inbound* half, so
neither table has a writer left.

They are dropped rather than left empty. An orphaned table with row-level
security, an ownership contract, a scoped unique key and six backfill services
still naming it is not neutral — every one of those inventories has to keep
listing it, and the next person to read them has to work out why a table nobody
writes to is still under contract.

**This deletes data, and there is no downgrade that brings it back.** The
downgrade recreates the shape only: the columns, the indexes, the row-security
policies, so an older binary can start against the schema it expects. Whatever
was in the rows is gone at ``upgrade`` time. That is the honest cost of removing
a feature rather than hiding it, and it is why this revision does nothing else —
a mixed migration is a worse thing to be halfway through.

What is deliberately *not* dropped:

* ``Domain.SIGNALS`` as a value. ``raw_payloads`` rows written by the bot carry
  it, and they stay: a raw payload is provenance, and the fact that a sentence
  arrived on a given day is true whether or not anything still parses it.
* ``Source.TELEGRAM``, for the same reason.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _json_type():
    return postgresql.JSONB if _is_postgres() else sa.JSON


def upgrade() -> None:
    # Order matters only for readability here — neither table references the
    # other, and nothing references either of them.
    op.drop_table("signals")
    op.drop_table("day_context")


def downgrade() -> None:
    """Recreate the shape, not the rows.

    Enough for an older binary to start and for its own migrations to reason
    about. The data is not recoverable from here and the docstring above says so
    rather than leaving it to be discovered.

    **The shape has to be the one revision 0057 left**, not a readable
    approximation of it, and the first version of this was the approximation.
    Going down from head, revisions 0051, 0050, 0049, 0038 and 0037 each drop
    indexes, constraints and columns from these two tables by name; anything
    this does not put back stops the downgrade with "index ... does not exist",
    halfway through, on a database somebody is using. The first attempt named
    the raw link ``raw_payload_id`` (it is ``raw_id``), left out four indexes
    and gave the foreign keys their default names, so the chain could not be
    walked back past revision 0037 at all.

    Nothing caught it because nothing had ever run it: the downgrade rehearsal
    starts from a synthetic revision-0034 lake, and the one path that would have
    reached here — a full ``upgrade head`` on an empty database, then back down
    — failed earlier for an unrelated reason. Both are fixed together.
    """

    json_type = _json_type()

    op.create_table(
        "day_context",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "domain",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'signals'"),
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("answers", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("planned", json_type, nullable=True),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "integration_connection_id", sa.Uuid(as_uuid=True), nullable=True
        ),
        # Named explicitly: revision 0037's downgrade drops them by these names.
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["health_subjects.id"],
            name="fk_day_context_subject_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_day_context_actor_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["integration_connection_id"],
            ["integration_connections.id"],
            name="fk_day_context_integration_connection_id",
            ondelete="RESTRICT",
        ),
    )
    for name, columns, unique in (
        ("ix_day_context_actor_user_id", ["actor_user_id"], False),
        ("ix_day_context_date", ["date"], False),
        ("ix_day_context_domain", ["domain"], False),
        ("ix_day_context_domain_date", ["domain", "date"], False),
        (
            "ix_day_context_integration_connection_id",
            ["integration_connection_id"],
            False,
        ),
        ("ix_day_context_subject_date", ["subject_id", "date"], False),
        (
            "ix_day_context_subject_domain_date",
            ["subject_id", "domain", "date"],
            False,
        ),
        ("ix_day_context_subject_id", ["subject_id"], False),
        ("uq_day_context_subject_date", ["subject_id", "date"], True),
    ):
        op.create_index(name, "day_context", columns, unique=unique)

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "domain",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'signals'"),
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("at_time", sa.Time(), nullable=True),
        # ``raw_id``, not ``raw_payload_id``: revision 0029 named it, nothing
        # renamed it, and revision 0037's downgrade drops an index on it.
        sa.Column("raw_id", sa.Integer(), nullable=True),
        sa.Column("batch_id", sa.String(length=32), nullable=False),
        sa.Column(
            "misparse", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "integration_connection_id", sa.Uuid(as_uuid=True), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["raw_id"],
            ["raw_payloads.id"],
            name="signals_raw_id_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["health_subjects.id"],
            name="fk_signals_subject_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_signals_actor_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["integration_connection_id"],
            ["integration_connections.id"],
            name="fk_signals_integration_connection_id",
            ondelete="RESTRICT",
        ),
    )
    for name, columns in (
        ("ix_signals_actor_user_id", ["actor_user_id"]),
        ("ix_signals_batch", ["batch_id"]),
        ("ix_signals_connection_batch", ["integration_connection_id", "batch_id"]),
        ("ix_signals_date", ["date"]),
        ("ix_signals_domain", ["domain"]),
        ("ix_signals_domain_date", ["domain", "date"]),
        ("ix_signals_integration_connection_id", ["integration_connection_id"]),
        ("ix_signals_key_date", ["key", "date"]),
        ("ix_signals_raw_id", ["raw_id"]),
        ("ix_signals_subject_batch", ["subject_id", "batch_id"]),
        ("ix_signals_subject_date", ["subject_id", "date"]),
        ("ix_signals_subject_domain_date", ["subject_id", "domain", "date"]),
        ("ix_signals_subject_id", ["subject_id"]),
        ("ix_signals_subject_key_date", ["subject_id", "key", "date"]),
    ):
        op.create_index(name, "signals", columns)

    if _is_postgres():
        # The predicate revisions 0050/0051 left, not revision 0037's: a
        # downgrade that recreates the older one would leave these two tables
        # invisible to the platform scope while every other table honoured it.
        predicate = (
            "(subject_id = NULLIF(current_setting('vitals.subject_id', true), '')::uuid"
            " OR current_setting('vitals.platform_scope', true) = 'on')"
        )
        for table in ("signals", "day_context"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY rls_subject_isolation ON {table} "
                f"USING ({predicate}) WITH CHECK ({predicate})"
            )
