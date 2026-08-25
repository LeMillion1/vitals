"""Persist operator invitation idempotency.

Revision ID: 0073
Revises: 0072
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: Union[str, None] = "0072"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DIGEST_CHECK = (
    "length(issuance_request_digest) = 64 "
    "AND lower(issuance_request_digest) = issuance_request_digest AND "
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "issuance_request_digest, '0', ''), '1', ''), '2', ''), '3', ''), "
    "'4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), "
    "'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''"
)


def upgrade() -> None:
    with op.batch_alter_table("registration_invitations") as batch:
        batch.add_column(
            sa.Column("issuance_request_digest", sa.String(64), nullable=True)
        )
        batch.create_check_constraint(
            "ck_registration_invitations_issuance_request_digest",
            f"issuance_request_digest IS NULL OR ({_DIGEST_CHECK})",
        )
        batch.create_unique_constraint(
            "uq_registration_invitations_issuance_request_digest",
            ["issuance_request_digest"],
        )


def downgrade() -> None:
    with op.batch_alter_table("registration_invitations") as batch:
        batch.drop_constraint(
            "uq_registration_invitations_issuance_request_digest",
            type_="unique",
        )
        batch.drop_constraint(
            "ck_registration_invitations_issuance_request_digest",
            type_="check",
        )
        batch.drop_column("issuance_request_digest")
