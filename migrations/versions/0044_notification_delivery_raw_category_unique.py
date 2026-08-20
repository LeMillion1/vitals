"""Make one outbound delivery intent authoritative per raw/category pair.

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-20

The nullable raw reference keeps scheduled and otherwise non-raw intents
unrestricted.  For a raw-backed reply or echo, the unique index closes races
across retries, terminal states, and integration-connection rotation.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "uq_notification_delivery_intents_raw_category"
_UPGRADE_REFUSAL = (
    "0044 upgrade refused: notification delivery intents contain duplicate "
    "raw/category claims"
)


def _lock_cutover() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # The lock closes the preflight/create race.  This migration only reads and
    # changes the intent table, so taking the one required lock cannot invert the
    # application's parent-before-intent lock order.
    bind.execute(
        sa.text(
            "LOCK TABLE notification_delivery_intents "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )


def _assert_raw_category_claims_are_unique() -> None:
    intents = sa.table(
        "notification_delivery_intents",
        sa.column("raw_payload_id", sa.Integer()),
        sa.column("category", sa.String()),
    )
    duplicates = (
        sa.select(intents.c.raw_payload_id, intents.c.category)
        .where(intents.c.raw_payload_id.is_not(None))
        .group_by(intents.c.raw_payload_id, intents.c.category)
        .having(sa.func.count() > 1)
    )
    if op.get_bind().execute(duplicates.limit(1)).first() is not None:
        raise RuntimeError(_UPGRADE_REFUSAL)


def upgrade() -> None:
    _lock_cutover()
    _assert_raw_category_claims_are_unique()
    op.create_index(
        _INDEX_NAME,
        "notification_delivery_intents",
        ["raw_payload_id", "category"],
        unique=True,
    )


def downgrade() -> None:
    _lock_cutover()
    op.drop_index(
        _INDEX_NAME,
        table_name="notification_delivery_intents",
    )


__all__ = ["downgrade", "upgrade"]
