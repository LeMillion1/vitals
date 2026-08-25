"""Add one exact, separately reviewed and reversible support repair.

Revision ID: 0076
Revises: 0075
Create Date: 2026-08-25

The only operation represented by this table clears the two derived body-
composition estimates on one retained measurement. Before values remain in the
subject-isolated receipt; no medical value is copied to AuditEvent.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: Union[str, None] = "0075"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = ("support_repair_actions",)
SUBJECT_SETTING = "vitals.subject_id"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid"
)


def upgrade() -> None:
    with op.batch_alter_table("support_access_grants") as batch:
        batch.create_unique_constraint(
            "uq_support_access_grants_id_subject_grantee",
            ["id", "subject_id", "granted_to_user_id"],
        )
    with op.batch_alter_table("body_measurements") as batch:
        batch.create_unique_constraint(
            "uq_body_measurements_id_subject", ["id", "subject_id"]
        )

    op.create_table(
        "support_repair_actions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("support_access_grant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "proposed_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("operation_key", sa.String(96), nullable=False),
        sa.Column("target_body_measurement_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execute_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("before_body_fat_pct", sa.Float(), nullable=True),
        sa.Column("before_lbm_kg", sa.Float(), nullable=True),
        sa.Column("target_updated_at_at_proposal", sa.DateTime(), nullable=False),
        sa.Column(
            "reviewed_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "executed_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_updated_at_after_execute", sa.DateTime(), nullable=True),
        sa.Column(
            "reverted_by_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["support_access_grant_id", "subject_id", "proposed_by_user_id"],
            [
                "support_access_grants.id",
                "support_access_grants.subject_id",
                "support_access_grants.granted_to_user_id",
            ],
            name="fk_support_repair_actions_exact_grant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_body_measurement_id", "subject_id"],
            ["body_measurements.id", "body_measurements.subject_id"],
            name="fk_support_repair_actions_exact_measurement",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "support_access_grant_id",
            "idempotency_key",
            name="uq_support_repair_actions_grant_idempotency",
        ),
        sa.CheckConstraint(
            "operation_key = 'body_measurement.clear_derived_estimates'",
            name="ck_support_repair_actions_operation",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'executed', "
            "'stale', 'reverted')",
            name="ck_support_repair_actions_status",
        ),
        sa.CheckConstraint(
            "execute_before > proposed_at",
            name="ck_support_repair_actions_positive_window",
        ),
        sa.CheckConstraint(
            "before_body_fat_pct IS NOT NULL OR before_lbm_kg IS NOT NULL",
            name="ck_support_repair_actions_has_change",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND reviewed_by_user_id IS NULL "
            "AND reviewed_at IS NULL AND executed_by_user_id IS NULL "
            "AND executed_at IS NULL AND target_updated_at_after_execute IS NULL "
            "AND reverted_by_user_id IS NULL AND reverted_at IS NULL) OR "
            "(status IN ('approved', 'declined', 'stale') "
            "AND reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND executed_by_user_id IS NULL AND executed_at IS NULL "
            "AND target_updated_at_after_execute IS NULL "
            "AND reverted_by_user_id IS NULL AND reverted_at IS NULL) OR "
            "(status = 'executed' AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND executed_by_user_id IS NOT NULL "
            "AND executed_at IS NOT NULL "
            "AND target_updated_at_after_execute IS NOT NULL "
            "AND reverted_by_user_id IS NULL AND reverted_at IS NULL) OR "
            "(status = 'reverted' AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND executed_by_user_id IS NOT NULL "
            "AND executed_at IS NOT NULL "
            "AND target_updated_at_after_execute IS NOT NULL "
            "AND reverted_by_user_id IS NOT NULL AND reverted_at IS NOT NULL)",
            name="ck_support_repair_actions_lifecycle",
        ),
    )
    op.create_index(
        "ix_support_repair_actions_subject_status_proposed",
        "support_repair_actions",
        ["subject_id", "status", "proposed_at"],
    )
    op.create_index(
        "ix_support_repair_actions_grant_status",
        "support_repair_actions",
        ["support_access_grant_id", "status"],
    )
    op.create_index(
        "uq_support_repair_actions_open_target",
        "support_repair_actions",
        ["subject_id", "target_body_measurement_id", "operation_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('proposed', 'approved')"),
        sqlite_where=sa.text("status IN ('proposed', 'approved')"),
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE "support_repair_actions" ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE "support_repair_actions" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "support_repair_actions" '
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "support_repair_actions"'
        )
        op.execute(
            'ALTER TABLE "support_repair_actions" NO FORCE ROW LEVEL SECURITY'
        )
        op.execute('ALTER TABLE "support_repair_actions" DISABLE ROW LEVEL SECURITY')
    op.drop_index(
        "uq_support_repair_actions_open_target", table_name="support_repair_actions"
    )
    op.drop_index(
        "ix_support_repair_actions_grant_status", table_name="support_repair_actions"
    )
    op.drop_index(
        "ix_support_repair_actions_subject_status_proposed",
        table_name="support_repair_actions",
    )
    op.drop_table("support_repair_actions")
    with op.batch_alter_table("body_measurements") as batch:
        batch.drop_constraint("uq_body_measurements_id_subject", type_="unique")
    with op.batch_alter_table("support_access_grants") as batch:
        batch.drop_constraint(
            "uq_support_access_grants_id_subject_grantee", type_="unique"
        )
