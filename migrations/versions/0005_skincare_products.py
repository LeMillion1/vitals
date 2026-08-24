"""Add the skincare_products table. The seed that came with it is gone.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-22

**This revision was edited after it had been applied, which the repository
otherwise forbids, so the reason is here rather than in a commit message.**

It used to insert five products — one person's regimen, in Russian — into every
new installation. Revision ``0049`` later made ``skincare_products.subject_id``
NOT NULL and refuses to proceed while any row has no owner. On an installation
that already existed those five rows were the owner's and the Stage-3B backfill
adopted them. On an **empty** database there is no owner to adopt them: identity
bootstrap is an application step that runs after migrations, so the five rows
could never get one, ``0049`` refused, and ``alembic upgrade head`` — which is
the container's own start command — failed. A new installation could not be
created at all.

The seed could not be removed by a later revision, because ``0049`` comes first
and is what fails. It could not be made conditional either: at this point in the
chain the ownership columns do not exist yet, so nothing here can tell a fresh
database from a historical replay.

Deleting the insert is a no-op for every installation that has already run this
revision — Alembic will not run it again, the rows stay, and the backfill has
already given them an owner. The only behaviour that changes is the one that was
broken: a fresh installation now starts with an empty skincare catalog, which is
also the more correct default. ``docs/COMMERCIAL_OWNERSHIP_INVENTORY.md`` has
said for a while that this table is personal despite its "reference" label —
schedule and active state belong to a person — and shipping one person's
regimen to everybody was always the wrong shape for it.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create skincare_products table
    op.create_table(
        "skincare_products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("active_ingredient", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("usage_instructions", sa.Text(), nullable=True),
        sa.Column("default_time", sa.String(length=32), nullable=False, server_default=sa.text("'evening'")),
        sa.Column("schedule_days", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("skincare_products")
