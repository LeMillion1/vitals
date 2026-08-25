"""Represent definitive Web Push provider rejections honestly.

Revision ID: 0070
Revises: 0069
Create Date: 2026-08-25

An HTTP 3xx/4xx response other than 404/410 is neither an uncertain transport
failure nor proof that a browser subscription is gone.  It is a definitive
rejection of this attempt, so the at-most-once outbox needs its own bounded,
post-dispatch terminal reason.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0070"
down_revision: Union[str, None] = "0069"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_ERROR_STATE = (
    "(status = 'ambiguous' AND error_code IN "
    "('transport_error', 'invalid_response', 'stale_dispatch', "
    "'internal_error')) OR "
    "(status = 'cancelled' AND error_code IN "
    "('access_revoked', 'account_inactive', 'subscription_revoked', "
    "'stale_pending', 'provider_gone')) OR "
    "(status NOT IN ('ambiguous', 'cancelled') AND error_code IS NULL)"
)
_NEW_ERROR_STATE = _OLD_ERROR_STATE.replace(
    "'stale_pending', 'provider_gone')",
    "'stale_pending', 'provider_gone', 'provider_rejected')",
)


def upgrade() -> None:
    with op.batch_alter_table("care_push_deliveries") as batch:
        batch.drop_constraint(
            "ck_care_push_deliveries_error_state", type_="check"
        )
        batch.drop_constraint(
            "ck_care_push_deliveries_provider_gone_after_dispatch",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_care_push_deliveries_error_state", _NEW_ERROR_STATE
        )
        batch.create_check_constraint(
            "ck_care_push_deliveries_provider_outcome_after_dispatch",
            "error_code NOT IN ('provider_gone', 'provider_rejected') OR "
            "(lease_token IS NOT NULL AND dispatch_started_at IS NOT NULL)",
        )


def downgrade() -> None:
    # The old vocabulary has no honest definitive-rejection value. Preserve
    # at-most-once terminality and the post-dispatch lease while conservatively
    # mapping it to the closest old non-retry state.
    op.execute(
        "UPDATE care_push_deliveries "
        "SET status = 'ambiguous', error_code = 'invalid_response' "
        "WHERE status = 'cancelled' AND error_code = 'provider_rejected'"
    )
    with op.batch_alter_table("care_push_deliveries") as batch:
        batch.drop_constraint(
            "ck_care_push_deliveries_provider_outcome_after_dispatch",
            type_="check",
        )
        batch.drop_constraint(
            "ck_care_push_deliveries_error_state", type_="check"
        )
        batch.create_check_constraint(
            "ck_care_push_deliveries_error_state", _OLD_ERROR_STATE
        )
        batch.create_check_constraint(
            "ck_care_push_deliveries_provider_gone_after_dispatch",
            "error_code <> 'provider_gone' OR "
            "(lease_token IS NOT NULL AND dispatch_started_at IS NOT NULL)",
        )
