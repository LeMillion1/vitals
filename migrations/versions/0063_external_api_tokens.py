"""A bearer credential that names one record, and only that record.

Revision ID: 0063
Revises: 0062
Create Date: 2026-08-24

``VITALS_EXTERNAL_API_TOKEN`` is one string for the whole installation, and the
endpoint it opens resolves its subject from whoever the ``.env`` file names as
the owner. On a single-user machine those are the same thing. The moment a
second person exists they are not: the token has no boundary, its holder reads a
record nobody granted them, and nothing about the credential says whose data
came back. That is the last of the ``.env``-owner reads on a data path, and this
table is what replaces it.

Only the SHA-256 of the secret is stored, so an operator reading this table
cannot use what they find — the rule ``professional_invitations`` already
follows. ``ck_external_api_tokens_revocation_state`` keeps a revoked row from
existing without a revocation time, because a credential history that can say
"revoked" with nothing having revoked it is worse than none.

Revoked rows are kept rather than deleted. "This dashboard could read my weight
until March" is part of who-saw-what, and it is not derivable from anywhere
else.

Subject-isolated under the same policy as the rest of the schema. ``downgrade``
drops the table, and with it every issued credential — which is the safe
direction: after a downgrade nothing authenticates rather than something
authenticating for the wrong person.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: Union[str, None] = "0062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = ("external_api_tokens",)

SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"(subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid "
    f"OR current_setting('{PLATFORM_SETTING}', true) = 'on')"
)


def upgrade() -> None:
    op.create_table(
        "external_api_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "issued_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("token_hash", name="uq_external_api_tokens_token_hash"),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_external_api_tokens_status",
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64 AND lower(token_hash) = token_hash",
            name="ck_external_api_tokens_token_hash_shape",
        ),
        sa.CheckConstraint(
            "length(trim(label)) > 0 AND length(label) <= 120",
            name="ck_external_api_tokens_label",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_external_api_tokens_positive_ttl",
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_external_api_tokens_revocation_state",
        ),
    )
    op.create_index(
        "ix_external_api_tokens_subject_status",
        "external_api_tokens",
        ["subject_id", "status", "expires_at"],
    )

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
    """Drops every issued credential, which is the safe direction.

    After this nothing authenticates against the external API. The alternative —
    keeping rows a downgraded application no longer scopes by — is something
    authenticating for the wrong person.
    """

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table_name in SUBJECT_ISOLATED_TABLES:
            op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')

    op.drop_index(
        "ix_external_api_tokens_subject_status", table_name="external_api_tokens"
    )
    op.drop_table("external_api_tokens")
