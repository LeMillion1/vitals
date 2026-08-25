"""Add private, subject-scoped care-message attachments.

Revision ID: 0067
Revises: 0066
Create Date: 2026-08-25

An attachment is a child of both one message and one ``FileAsset``. Composite
foreign keys make all three rows agree on ``subject_id``; PostgreSQL row-level
security then protects the attachment metadata with the same boundary as the
conversation. Bytes live outside the static tree under the ``private_local``
backend and are never addressed by their storage reference.

The downgrade removes attachment metadata and its file-asset rows. It cannot
remove private-local bytes because Alembic deliberately has no access to the
configured storage root; operators downgrading across this revision must prune
the now-unreferenced ``care/`` objects separately.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: Union[str, None] = "0066"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = ("care_message_attachments",)
SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"(subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid "
    f"OR current_setting('{PLATFORM_SETTING}', true) = 'on')"
)

_PURPOSES_WITH_ATTACHMENT = (
    "progress_photo",
    "lab_document",
    "body_scan_document",
    "care_message_attachment",
)
_PURPOSES_BEFORE_ATTACHMENT = _PURPOSES_WITH_ATTACHMENT[:-1]


def _purpose_check(values: tuple[str, ...]) -> str:
    return "purpose IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table("file_assets") as batch:
        batch.drop_constraint("ck_file_assets_purpose", type_="check")
        batch.create_check_constraint(
            "ck_file_assets_purpose", _purpose_check(_PURPOSES_WITH_ATTACHMENT)
        )

    with op.batch_alter_table("care_messages") as batch:
        batch.create_unique_constraint(
            "uq_care_messages_id_subject", ["id", "subject_id"]
        )

    op.create_table(
        "care_message_attachments",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("message_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("file_asset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["message_id", "subject_id"],
            ["care_messages.id", "care_messages.subject_id"],
            name="fk_care_message_attachments_message_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_asset_id", "subject_id"],
            ["file_assets.id", "file_assets.subject_id"],
            name="fk_care_message_attachments_asset_subject",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("message_id", name="uq_care_message_attachments_message"),
        sa.UniqueConstraint("file_asset_id", name="uq_care_message_attachments_asset"),
        sa.CheckConstraint(
            "length(trim(original_filename)) > 0 AND length(original_filename) <= 255",
            name="ck_care_message_attachments_filename",
        ),
    )
    op.create_index(
        "ix_care_message_attachments_subject_created",
        "care_message_attachments",
        ["subject_id", "created_at"],
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

    op.drop_index(
        "ix_care_message_attachments_subject_created",
        table_name="care_message_attachments",
    )
    op.drop_table("care_message_attachments")
    op.execute(
        sa.text(
            "DELETE FROM file_assets WHERE purpose = 'care_message_attachment'"
        )
    )

    with op.batch_alter_table("care_messages") as batch:
        batch.drop_constraint("uq_care_messages_id_subject", type_="unique")

    with op.batch_alter_table("file_assets") as batch:
        batch.drop_constraint("ck_file_assets_purpose", type_="check")
        batch.create_check_constraint(
            "ck_file_assets_purpose", _purpose_check(_PURPOSES_BEFORE_ATTACHMENT)
        )
