"""Bound account-admission retention scans.

Revision ID: 0074
Revises: 0073
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: Union[str, None] = "0073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNPURGED_TERMINAL = sa.text("purged_at IS NULL AND status <> 'pending'")


def upgrade() -> None:
    op.create_index(
        "ix_registration_invitations_retention_scan",
        "registration_invitations",
        ["created_at", "id"],
        postgresql_where=_UNPURGED_TERMINAL,
        sqlite_where=_UNPURGED_TERMINAL,
    )
    op.create_index(
        "ix_registration_requests_retention_scan",
        "registration_requests",
        ["created_at", "id"],
        postgresql_where=_UNPURGED_TERMINAL,
        sqlite_where=_UNPURGED_TERMINAL,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_registration_requests_retention_scan",
        table_name="registration_requests",
    )
    op.drop_index(
        "ix_registration_invitations_retention_scan",
        table_name="registration_invitations",
    )
