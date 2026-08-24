"""A connector's access, and the ability to take it back.

Revision ID: 0064
Revises: 0063
Create Date: 2026-08-24

The MCP access token stays a signed value the server does not look up to
validate — that is what makes it cheap. This table is what makes it *revocable*:
the token carries a ``jti``, and one indexed read says whether that id is still
good.

Before it, revoking an issued connector token meant rotating
``VITALS_SESSION_SECRET``. That invalidates every MCP token *and* every web
session at once, so "disconnect the laptop I lost" and "sign the whole household
out and reconnect every client" were the same operation — which is a revocation
mechanism in the sense that a fire alarm is a door.

No subject column: this is an account's credential, not a record's. Which record
a connector then reaches is decided per request by the subject seam in
``web/routers/mcp.py``, and putting a subject here would be a second answer to
that question with no way to keep the two in step. So no row-security policy
either — the boundary this table needs is the account's, and every read of it is
by ``user_id`` or by primary key.

``adopted`` marks a token minted before this existed and recorded on first use.
Kept visible rather than smoothed over: such a token was issued without an
audience and without a ``jti`` of its own, and somebody reading their list of
connections is entitled to know which of them predate the guarantee.

``downgrade`` drops the table, and with it every revocation. That direction is
safe in the only way that matters here: afterwards nothing is revocable rather
than something being wrongly believed revoked.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: Union[str, None] = "0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_access_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("client_name", sa.String(255), nullable=True),
        sa.Column("audience", sa.String(255), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "adopted",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "length(trim(client_id)) > 0", name="ck_mcp_access_tokens_client_id"
        ),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_mcp_access_tokens_positive_ttl"
        ),
    )
    op.create_index(
        "ix_mcp_access_tokens_user_issued",
        "mcp_access_tokens",
        ["user_id", "issued_at"],
    )


def downgrade() -> None:
    """Drops every recorded connector, and with it every revocation.

    Safe in the only direction that matters: afterwards nothing is revocable,
    rather than something being wrongly believed revoked.
    """

    op.drop_index("ix_mcp_access_tokens_user_issued", table_name="mcp_access_tokens")
    op.drop_table("mcp_access_tokens")
