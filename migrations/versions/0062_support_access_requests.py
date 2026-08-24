"""An ask for support access, which is not access.

Revision ID: 0062
Revises: 0061
Create Date: 2026-08-24

``support_access_grants`` cannot hold a pending request, and that is a feature
of it rather than a gap. Its constraints say a row there was approved by
somebody who is not its grantee (``ck_support_access_grants_no_self_approval``)
and expires strictly after that approval (``..._positive_ttl``). Neither
sentence is true of an ask nobody has answered, so a "pending" status on that
table would have to be bought by dropping both — and those two are most of what
makes a row there mean *authorized*. The ask gets its own table instead, and
approving one is what writes a grant.

**The decision belongs to the subject's owner.** An admin asks; nothing on the
platform side answers for the patient. ``ck_support_access_requests_decision_state``
keeps a decided row from existing without a decider and a time, because a
history that can say "declined" with nobody having declined it is worse than no
history. ``ck_support_access_requests_grant_link`` makes the other direction as
tight: exactly the approved rows name a grant, and every approved row does.

Scopes are enumerated here as well as on the grant, with the same wildcard ban.
The screen the patient reads is built from *these* rows — they are being asked
to agree to something specific, and "everything" is not something a person can
weigh.

Both tables are subject-isolated under the same policy as the rest of the
schema, and the child carries its own ``subject_id`` with a composite foreign
key back to the parent so the two cannot disagree — the mechanism revisions
0060 and 0061 use for the same reason.

``downgrade`` drops both, and with them the record of who asked for access to
whose record and what the answer was. That is stated rather than left to be
found: an access history is exactly the thing a patient cannot reconstruct from
anywhere else.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: Union[str, None] = "0061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = (
    "support_access_requests",
    "support_access_request_scopes",
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
        "support_access_requests",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        _subject_column(),
        sa.Column(
            "requested_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ticket_reference", sa.String(120), nullable=True),
        sa.Column("requested_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "decided_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "granted_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("support_access_grants.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        *_timestamps(),
        sa.UniqueConstraint(
            "id", "subject_id", name="uq_support_access_requests_id_subject"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'declined', 'withdrawn', 'expired')",
            name="ck_support_access_requests_status",
        ),
        sa.CheckConstraint(
            "mode IN ('read', 'repair', 'export')",
            name="ck_support_access_requests_mode",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0 AND length(reason) <= 2000",
            name="ck_support_access_requests_reason",
        ),
        sa.CheckConstraint(
            "ticket_reference IS NULL OR "
            "(length(trim(ticket_reference)) > 0 "
            "AND length(ticket_reference) <= 120)",
            name="ck_support_access_requests_ticket_reference",
        ),
        sa.CheckConstraint(
            "requested_ttl_seconds > 0 AND requested_ttl_seconds <= 86400",
            name="ck_support_access_requests_ttl_bounds",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND decided_at IS NULL "
            "AND decided_by_user_id IS NULL) OR "
            "(status <> 'pending' AND decided_at IS NOT NULL "
            "AND decided_by_user_id IS NOT NULL)",
            name="ck_support_access_requests_decision_state",
        ),
        sa.CheckConstraint(
            "(status = 'approved') = (granted_id IS NOT NULL)",
            name="ck_support_access_requests_grant_link",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_support_access_requests_positive_window",
        ),
    )
    op.create_index(
        "ix_support_access_requests_subject_status",
        "support_access_requests",
        ["subject_id", "status", "created_at"],
    )
    op.create_index(
        "ix_support_access_requests_requester_status",
        "support_access_requests",
        ["requested_by_user_id", "status", "created_at"],
    )

    op.create_table(
        "support_access_request_scopes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        _subject_column(),
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
            ["request_id", "subject_id"],
            ["support_access_requests.id", "support_access_requests.subject_id"],
            name="fk_support_access_request_scopes_request_subject",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "request_id",
            "resource_type",
            "resource_key",
            "action",
            name="uq_support_access_request_scopes_request_resource_action",
        ),
        sa.CheckConstraint(
            "resource_type IN ('domain', 'artifact', 'operation')",
            name="ck_support_access_request_scopes_resource_type",
        ),
        sa.CheckConstraint(
            "action IN ('read', 'repair', 'export')",
            name="ck_support_access_request_scopes_action",
        ),
        sa.CheckConstraint(
            "length(trim(resource_key)) > 0",
            name="ck_support_access_request_scopes_resource_key_not_blank",
        ),
        sa.CheckConstraint(
            "resource_key NOT LIKE '%*%'",
            name="ck_support_access_request_scopes_no_wildcard",
        ),
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
    """Drops both, and with them every record of who asked for what.

    Stated rather than left to be discovered. An access history is not derivable
    from anything else in the schema: the grants table remembers the approvals
    and nothing at all about the asks that were declined or withdrawn, which are
    the ones a patient is most likely to be looking for.
    """

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table_name in SUBJECT_ISOLATED_TABLES:
            op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')

    op.drop_table("support_access_request_scopes")

    for name in (
        "ix_support_access_requests_requester_status",
        "ix_support_access_requests_subject_status",
    ):
        op.drop_index(name, table_name="support_access_requests")
    op.drop_table("support_access_requests")
