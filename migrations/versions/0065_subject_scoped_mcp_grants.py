"""Bind every MCP credential to one subject and exact capabilities.

Revision ID: 0065
Revises: 0064
Create Date: 2026-08-25

An account identity is not a health-data grant. This revision gives the MCP
registry the missing boundary: one subject, an optional relationship plus
concrete consent version for cross-subject access, and normalized exact scopes.

Existing credentials are bound only to a record owned by their authorizing
account and receive a frozen snapshot of the owner connector catalog. Accounts
without a personal record cannot be assigned a subject honestly, so those rows
are retained as revoked history. An empty scope set authorizes nothing.
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: Union[str, None] = "0064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = (
    "mcp_access_tokens",
    "mcp_access_token_scopes",
)
SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"(subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid "
    f"OR current_setting('{PLATFORM_SETTING}', true) = 'on')"
)

_OWNER_DOMAINS = (
    "weight",
    "body_comp",
    "glp1",
    "supplements",
    "genetics",
    "skincare",
    "workouts",
    "garmin",
    "labs",
    "nutrition",
    "hrt",
    "milestones",
    "timeline",
)
_OWNER_DOMAIN_ACTIONS = ("read", "list", "search", "create", "update", "delete")
_OWNER_SURFACES = (
    ("artifact", "health_profile", "read"),
    ("artifact", "weekly_digest", "read"),
    ("artifact", "weekly_digest", "list"),
    ("artifact", "weekly_digest", "create"),
    ("artifact", "safety_alert", "read"),
    ("artifact", "safety_alert", "update"),
    ("operation", "conflict.check", "read"),
    ("operation", "modules", "read"),
    ("operation", "modules", "update"),
    ("operation", "proactive", "read"),
    ("operation", "record.export", "export"),
    ("operation", "garmin.sync", "sync"),
    ("operation", "hevy.sync", "sync"),
)


def upgrade() -> None:
    with op.batch_alter_table("mcp_access_tokens") as batch:
        batch.add_column(sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=True))
        batch.add_column(
            sa.Column("relationship_id", sa.Uuid(as_uuid=True), nullable=True)
        )
        batch.add_column(
            sa.Column("consent_grant_id", sa.Uuid(as_uuid=True), nullable=True)
        )
        batch.add_column(sa.Column("consent_version", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_mcp_access_tokens_subject_id",
            "health_subjects",
            ["subject_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_mcp_access_tokens_relationship_id",
            "care_relationships",
            ["relationship_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_mcp_access_tokens_consent_grant_id",
            "consent_grants",
            ["consent_grant_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    # A pre-cutover credential can safely inherit only the record its account
    # owns. Professional accounts commonly own none; revocation is the only
    # truthful migration for those rows.
    op.execute(
        sa.text(
            "UPDATE mcp_access_tokens SET subject_id = ("
            "SELECT health_subjects.id FROM health_subjects "
            "WHERE health_subjects.owner_user_id = mcp_access_tokens.user_id"
            ")"
        )
    )
    op.execute(
        sa.text(
            "UPDATE mcp_access_tokens SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) "
            "WHERE subject_id IS NULL"
        )
    )

    with op.batch_alter_table("mcp_access_tokens") as batch:
        batch.create_check_constraint(
            "ck_mcp_access_tokens_subject_or_revoked",
            "subject_id IS NOT NULL OR revoked_at IS NOT NULL",
        )
        batch.create_check_constraint(
            "ck_mcp_access_tokens_professional_binding",
            "(relationship_id IS NULL AND consent_grant_id IS NULL AND "
            "consent_version IS NULL) OR (relationship_id IS NOT NULL AND "
            "consent_grant_id IS NOT NULL AND consent_version IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_mcp_access_tokens_consent_version",
            "consent_version IS NULL OR consent_version >= 1",
        )
        batch.create_unique_constraint(
            "uq_mcp_access_tokens_id_subject", ["id", "subject_id"]
        )
        batch.create_index(
            "ix_mcp_access_tokens_subject_live",
            ["subject_id", "revoked_at", "expires_at"],
        )

    op.create_table(
        "mcp_access_token_scopes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("token_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(16), nullable=False),
        sa.Column("resource_key", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["token_id", "subject_id"],
            ["mcp_access_tokens.id", "mcp_access_tokens.subject_id"],
            name="fk_mcp_access_token_scopes_token_subject",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "token_id",
            "resource_type",
            "resource_key",
            "action",
            name="uq_mcp_access_token_scopes_capability",
        ),
        sa.CheckConstraint(
            "resource_type IN ('domain', 'artifact', 'operation')",
            name="ck_mcp_access_token_scopes_resource_type",
        ),
        sa.CheckConstraint(
            "action IN ('read', 'list', 'search', 'create', 'update', 'delete', "
            "'attach', 'share', 'export', 'sync', 'message', 'repair')",
            name="ck_mcp_access_token_scopes_action",
        ),
        sa.CheckConstraint(
            "length(trim(resource_key)) > 0 AND length(resource_key) <= 128",
            name="ck_mcp_access_token_scopes_resource_key",
        ),
        sa.CheckConstraint(
            "resource_key NOT LIKE '%*%'",
            name="ck_mcp_access_token_scopes_no_wildcard",
        ),
    )
    op.create_index(
        "ix_mcp_access_token_scopes_subject_capability",
        "mcp_access_token_scopes",
        ["subject_id", "resource_type", "resource_key", "action"],
    )

    # Registry-backed owner tokens issued by 0064 remain usable, but receive a
    # frozen snapshot rather than a wildcard. A future domain therefore does
    # not enter their reach. Rows revoked above have no subject and get none.
    connection = op.get_bind()
    existing = list(
        connection.execute(
            sa.text(
                "SELECT id, subject_id FROM mcp_access_tokens "
                "WHERE subject_id IS NOT NULL"
            )
        )
    )
    scope_table = sa.table(
        "mcp_access_token_scopes",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("token_id", sa.Uuid(as_uuid=True)),
        sa.column("subject_id", sa.Uuid(as_uuid=True)),
        sa.column("resource_type", sa.String()),
        sa.column("resource_key", sa.String()),
        sa.column("action", sa.String()),
    )
    catalog = tuple(
        ("domain", domain, action)
        for domain in _OWNER_DOMAINS
        for action in _OWNER_DOMAIN_ACTIONS
    ) + _OWNER_SURFACES
    if existing:
        op.bulk_insert(
            scope_table,
            [
                {
                    "id": uuid.uuid4(),
                    "token_id": token_id,
                    "subject_id": subject_id,
                    "resource_type": resource_type,
                    "resource_key": resource_key,
                    "action": action,
                }
                for token_id, subject_id in existing
                for resource_type, resource_key, action in catalog
            ],
        )

    if connection.dialect.name == "postgresql":
        for table_name in SUBJECT_ISOLATED_TABLES:
            op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
            op.execute(
                f'CREATE POLICY "{POLICY_NAME}" ON "{table_name}" '
                f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
            )


def downgrade() -> None:
    """Remove the new authorization boundary and its delegated capabilities."""

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table_name in reversed(SUBJECT_ISOLATED_TABLES):
            op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')
            op.execute(f'ALTER TABLE "{table_name}" NO FORCE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY')

    op.drop_index(
        "ix_mcp_access_token_scopes_subject_capability",
        table_name="mcp_access_token_scopes",
    )
    op.drop_table("mcp_access_token_scopes")

    with op.batch_alter_table("mcp_access_tokens") as batch:
        batch.drop_index("ix_mcp_access_tokens_subject_live")
        batch.drop_constraint("uq_mcp_access_tokens_id_subject", type_="unique")
        batch.drop_constraint(
            "ck_mcp_access_tokens_consent_version", type_="check"
        )
        batch.drop_constraint(
            "ck_mcp_access_tokens_professional_binding", type_="check"
        )
        batch.drop_constraint("ck_mcp_access_tokens_subject_or_revoked", type_="check")
        batch.drop_constraint(
            "fk_mcp_access_tokens_consent_grant_id", type_="foreignkey"
        )
        batch.drop_constraint(
            "fk_mcp_access_tokens_relationship_id", type_="foreignkey"
        )
        batch.drop_constraint("fk_mcp_access_tokens_subject_id", type_="foreignkey")
        batch.drop_column("consent_version")
        batch.drop_column("consent_grant_id")
        batch.drop_column("relationship_id")
        batch.drop_column("subject_id")
