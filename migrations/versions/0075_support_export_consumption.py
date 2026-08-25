"""Make exceptional support exports single-use.

Revision ID: 0075
Revises: 0074
Create Date: 2026-08-25

The export itself is never persisted. ``consumed_at`` is the durable receipt
that the exact patient-approved grant released one transient subject export.
It is terminal, export-only, and mutually exclusive with every other grant
state.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: Union[str, None] = "0074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("support_access_grants") as batch:
        batch.add_column(
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.drop_constraint("ck_support_access_grants_status", type_="check")
        batch.create_check_constraint(
            "ck_support_access_grants_status",
            "status IN ('active', 'revoked', 'expired', 'consumed')",
        )
        batch.create_check_constraint(
            "ck_support_access_grants_consumed_state",
            "(status = 'consumed' AND mode = 'export' AND consumed_at IS NOT NULL) "
            "OR (status <> 'consumed' AND consumed_at IS NULL)",
        )


def downgrade() -> None:
    # The exported bytes were never retained, so a consumed grant cannot become
    # usable again. Mapping it to expired preserves the terminal authorization
    # fact while the append-only audit event keeps the historical disclosure.
    op.execute(
        sa.text(
            "UPDATE support_access_grants SET status = 'expired', consumed_at = NULL "
            "WHERE status = 'consumed'"
        )
    )
    with op.batch_alter_table("support_access_grants") as batch:
        batch.drop_constraint(
            "ck_support_access_grants_consumed_state", type_="check"
        )
        batch.drop_constraint("ck_support_access_grants_status", type_="check")
        batch.create_check_constraint(
            "ck_support_access_grants_status",
            "status IN ('active', 'revoked', 'expired')",
        )
        batch.drop_column("consumed_at")
