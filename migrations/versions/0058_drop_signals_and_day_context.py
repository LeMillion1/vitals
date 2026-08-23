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
    """

    json_type = _json_type()

    op.create_table(
        "day_context",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("answers", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("planned", json_type, nullable=True),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "integration_connection_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("integration_connections.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_day_context_subject_date", "day_context", ["subject_id", "date"])
    op.create_index(
        "uq_day_context_subject_date",
        "day_context",
        ["subject_id", "date"],
        unique=True,
    )
    op.create_index(
        "ix_day_context_subject_domain_date",
        "day_context",
        ["subject_id", "domain", "date"],
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("at_time", sa.Time(), nullable=True),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column(
            "misparse", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "integration_connection_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("integration_connections.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "raw_payload_id",
            sa.Integer(),
            sa.ForeignKey("raw_payloads.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_signals_subject_date", "signals", ["subject_id", "date"])
    op.create_index("ix_signals_subject_key_date", "signals", ["subject_id", "key", "date"])

    if _is_postgres():
        for table in ("signals", "day_context"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY rls_subject_isolation ON {table} "
                "USING (subject_id = current_setting('vitals.subject_id', true)::uuid) "
                "WITH CHECK (subject_id = current_setting('vitals.subject_id', true)::uuid)"
            )
