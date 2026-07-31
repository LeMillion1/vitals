"""weekly_digests.kind — one store for weekly / daily brief / evening

The daily brief is the same artifact as the weekly digest (narrative + the
context it was built from), just a different cadence, so it reuses this table and
the /reports page rather than growing a second store. Existing rows are all
weekly, which is exactly what the server default backfills them to.

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "weekly_digests",
        sa.Column(
            "kind", sa.String(16), nullable=False, server_default="weekly"
        ),
    )
    # Readers always filter on kind, and the table grows a row a day now.
    op.create_index("ix_weekly_digests_kind_date", "weekly_digests", ["kind", "date"])


def downgrade() -> None:
    op.drop_index("ix_weekly_digests_kind_date", table_name="weekly_digests")
    op.drop_column("weekly_digests", "kind")
