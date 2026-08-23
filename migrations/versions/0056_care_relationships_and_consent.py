"""The two halves access needs, and neither of them is sufficient alone.

Revision ID: 0056
Revises: 0055
Create Date: 2026-08-23

A ``care_relationship`` says a professional is in care for a patient.  A
``consent_grant`` says what that patient has agreed they may see.  Access
requires both, live, at the moment of the request — a relationship with no live
consent is somebody the patient agreed to work with and has not yet agreed to
show anything to, which is an ordinary and correct state to be in.

Consent is versioned rather than edited.  Narrowing what somebody may read is a
new version superseding the old, so "what was this professional allowed to see
on the day they read it" stays answerable; an updated row cannot answer that,
and it is the question any later dispute is actually about.  Exactly one version
per relationship may be live, enforced by a partial unique index rather than by
convention, because two live versions would mean the wider of them silently
wins.

Two more constraints are worth naming.  A relationship carries the subject's
owner as a column purely so "the two parties are two people" can be a database
check rather than a rule the application remembers.  And scopes forbid
wildcards, exactly as support scopes do: broad permission is a longer list of
concrete keys, so reading the row tells you what it permits without knowing what
the catalog held the day it was written.

All three tables carry ``subject_id`` and the standard two-clause policy.  The
consent tables repeat the column rather than reaching it through a join, because
a row reachable only by joining is a row outside the policy protecting
everything else of that patient's.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = (
    "care_relationships",
    "consent_grants",
    "consent_scopes",
)

SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"(subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid "
    f"OR current_setting('{PLATFORM_SETTING}', true) = 'on')"
)

_ACTIONS = (
    "'read', 'list', 'search', 'create', 'update', 'delete', 'attach', "
    "'share', 'export', 'sync', 'message', 'repair'"
)


def upgrade() -> None:
    op.create_table(
        "care_relationships",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subject_owner_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "professional_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="active"
        ),
        sa.Column(
            "invitation_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("professional_invitations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "established_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ended_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
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
            "kind IN ('doctor', 'trainer')", name="ck_care_relationships_kind"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'ended')",
            name="ck_care_relationships_status",
        ),
        sa.CheckConstraint(
            "subject_owner_user_id <> professional_user_id",
            name="ck_care_relationships_two_parties",
        ),
        sa.CheckConstraint(
            "(status = 'ended' AND ended_at IS NOT NULL) OR "
            "(status <> 'ended' AND ended_at IS NULL)",
            name="ck_care_relationships_ended_state",
        ),
    )
    op.create_index(
        "uq_care_relationships_live_pair",
        "care_relationships",
        ["subject_id", "professional_user_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'ended'"),
        sqlite_where=sa.text("status <> 'ended'"),
    )
    op.create_index(
        "ix_care_relationships_subject_status",
        "care_relationships",
        ["subject_id", "status"],
    )
    op.create_index(
        "ix_care_relationships_professional_status",
        "care_relationships",
        ["professional_user_id", "status"],
    )

    op.create_table(
        "consent_grants",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "relationship_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("care_relationships.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="active"
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "relationship_id",
            "version",
            name="uq_consent_grants_relationship_version",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'superseded', 'revoked', 'expired')",
            name="ck_consent_grants_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_consent_grants_version_positive"),
        sa.CheckConstraint(
            "expires_at > granted_at", name="ck_consent_grants_positive_ttl"
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_consent_grants_revoked_state",
        ),
        sa.CheckConstraint(
            "(status = 'paused' AND paused_at IS NOT NULL) OR "
            "(status <> 'paused' AND paused_at IS NULL)",
            name="ck_consent_grants_paused_state",
        ),
    )
    op.create_index(
        "uq_consent_grants_live_version",
        "consent_grants",
        ["relationship_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'paused')"),
        sqlite_where=sa.text("status IN ('active', 'paused')"),
    )
    op.create_index(
        "ix_consent_grants_subject_status_expires",
        "consent_grants",
        ["subject_id", "status", "expires_at"],
    )

    op.create_table(
        "consent_scopes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "consent_grant_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("consent_grants.id", ondelete="CASCADE"),
            nullable=False,
        ),
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
        sa.UniqueConstraint(
            "consent_grant_id",
            "resource_type",
            "resource_key",
            "action",
            name="uq_consent_scopes_grant_resource_action",
        ),
        sa.CheckConstraint(
            "resource_type IN ('domain', 'artifact', 'operation')",
            name="ck_consent_scopes_resource_type",
        ),
        sa.CheckConstraint(
            f"action IN ({_ACTIONS})", name="ck_consent_scopes_action"
        ),
        sa.CheckConstraint(
            "length(trim(resource_key)) > 0 AND length(resource_key) <= 128",
            name="ck_consent_scopes_resource_key",
        ),
        sa.CheckConstraint(
            "resource_key NOT LIKE '%*%'", name="ck_consent_scopes_no_wildcard"
        ),
    )
    op.create_index(
        "ix_consent_scopes_resource",
        "consent_scopes",
        ["resource_type", "resource_key", "action"],
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
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table_name in SUBJECT_ISOLATED_TABLES:
            op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')
    op.drop_index("ix_consent_scopes_resource", table_name="consent_scopes")
    op.drop_table("consent_scopes")
    op.drop_index(
        "ix_consent_grants_subject_status_expires", table_name="consent_grants"
    )
    op.drop_index("uq_consent_grants_live_version", table_name="consent_grants")
    op.drop_table("consent_grants")
    op.drop_index(
        "ix_care_relationships_professional_status", table_name="care_relationships"
    )
    op.drop_index(
        "ix_care_relationships_subject_status", table_name="care_relationships"
    )
    op.drop_index("uq_care_relationships_live_pair", table_name="care_relationships")
    op.drop_table("care_relationships")
