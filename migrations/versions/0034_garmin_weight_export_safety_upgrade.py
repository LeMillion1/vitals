"""Quarantine Garmin weight intents created before durable dispatch tracking.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-17

The first exporter could infer ownership from a sole equal read-back and could
roll an accepted-but-timed-out POST back to ``pending``/``failed``.  Those rows
cannot be distinguished retrospectively from safe ones.  Fail closed: retain
the facts and remote identifiers for display, but remove deletion authority and
never automatically repeat an old actionable POST.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UPGRADE_ERROR = (
    "Pre-upgrade Garmin POST state cannot be verified; automatic repeat is blocked"
)


def upgrade() -> None:
    op.add_column(
        "garmin_weight_exports",
        sa.Column("dispatch_timestamp_ms", sa.BigInteger(), nullable=True),
    )
    exports = sa.table(
        "garmin_weight_exports",
        sa.column("id", sa.Integer),
        sa.column("status", sa.String),
        sa.column("weight_kg", sa.Float),
        sa.column("measured_at", sa.DateTime),
        sa.column("remote_sample_pk", sa.String),
        sa.column("remote_weight_kg", sa.Float),
        sa.column("remote_owned", sa.Boolean),
        sa.column("next_attempt_at", sa.DateTime),
        sa.column("last_error", sa.Text),
        sa.column("updated_at", sa.DateTime),
    )

    # A legacy success with no identifier is an ambiguous 204/read-back result,
    # not an external match and not a safely repeatable send.
    op.execute(
        exports.update()
        .where(
            exports.c.status == "sent",
            exports.c.remote_sample_pk.is_(None),
        )
        .values(
            status="unverified",
            remote_weight_kg=sa.func.coalesce(
                exports.c.remote_weight_kg, exports.c.weight_kg
            ),
            remote_owned=False,
            next_attempt_at=None,
            last_error=_UPGRADE_ERROR,
            updated_at=sa.func.now(),
        )
    )

    # A stored identifier may have come from the old unsafe sole-match
    # inference. Keep it as an external match, without delete authority.
    op.execute(
        exports.update()
        .where(
            exports.c.status == "sent",
            exports.c.remote_sample_pk.is_not(None),
        )
        .values(
            status="matched",
            remote_owned=False,
            next_attempt_at=None,
            last_error=None,
            updated_at=sa.func.now(),
        )
    )

    # A process crash could have rolled an already accepted POST back to either
    # of these states (including pending with attempts=0), so retrying any of
    # them would violate the new at-most-once invariant.
    op.execute(
        exports.update()
        .where(exports.c.status.in_(("pending", "failed")))
        .values(
            status="unverified",
            remote_sample_pk=None,
            remote_weight_kg=sa.func.coalesce(
                exports.c.remote_weight_kg, exports.c.weight_kg
            ),
            remote_owned=False,
            next_attempt_at=None,
            last_error=_UPGRADE_ERROR,
            updated_at=sa.func.now(),
        )
    )

    # New code reserves a non-zero, millisecond-aligned measured_at fraction as
    # its exact POST correlation marker. Legacy timestamps were not markers, so
    # zero every quarantined fraction and make ownership recovery fail closed.
    bind = op.get_bind()
    quarantined = bind.execute(
        sa.select(exports.c.id, exports.c.measured_at).where(
            exports.c.status == "unverified"
        )
    ).all()
    for row_id, measured_at in quarantined:
        if measured_at is not None and measured_at.microsecond:
            bind.execute(
                exports.update()
                .where(exports.c.id == row_id)
                .values(measured_at=measured_at.replace(microsecond=0))
            )

    # Defense in depth for any other legacy state carrying inferred ownership.
    op.execute(
        exports.update()
        .where(exports.c.remote_owned.is_(True))
        .values(remote_owned=False, updated_at=sa.func.now())
    )


def downgrade() -> None:
    # Trust cannot be reconstructed after it has deliberately been removed.
    # The schema can still be rolled back; quarantined data stays conservative.
    op.drop_column("garmin_weight_exports", "dispatch_timestamp_ms")
