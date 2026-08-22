"""Make the registered-required ownership references NOT NULL.

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-22

This is the contract migration every earlier ownership revision deferred to.
0037 and 0038 added these columns nullable so the expansion could ship without a
single write failing; Stage 3 backfilled them; 0046 tied children to parents;
0047 and 0048 moved the unique keys inside the scope these columns define.  All
of that assumed a value that the schema still let a writer omit.

A scoped unique index over a nullable column keeps no uniqueness for a row whose
scope is null, and a scoped read cannot return that row to anybody.  Such a row
is not "not yet migrated" — it is unreachable and unconstrained.  This revision
is what makes it impossible, and it is the precondition for FORCE RLS: a policy
comparing ``subject_id`` to the session's subject silently excludes every null
instead of protecting it.

It is gated on the application side by PR-04: ``vitals/legacy_scope.py`` is empty,
so no service can write one of these rows without stating its scope.  It is gated
on the data side here, in code rather than in prose: ``upgrade`` counts the
remaining nulls in every target column first and refuses with the table, the
column, and the count if any are left.  A half-run backfill therefore stops the
migration instead of having its unstamped rows rejected column by column.

On PostgreSQL each column is proven with a ``NOT VALID`` check constraint that is
then validated, before ``SET NOT NULL`` and dropping the check.  ``VALIDATE
CONSTRAINT`` takes SHARE UPDATE EXCLUSIVE rather than ACCESS EXCLUSIVE, and
PostgreSQL 12+ recognises the validated check as proof, so ``SET NOT NULL`` skips
the table scan it would otherwise perform under a full lock.  On a lake where
``garmin_intraday`` holds a couple of thousand samples for every day ever synced,
that is the difference between a brief pause and a visible outage.

Downgrade returns every column to nullable.  That is honest — it restores the
schema — but it does not restore the safety the columns were nullable *for*: by
the time this revision has run, scoped keys and RLS both depend on the value
being present, and a binary old enough to write a null here is old enough to be
outside the rollback boundary described in the ownership inventory.
"""
from typing import Sequence, Union

from alembic import op

from vitals.ownership import OwnershipBackfillIncompleteError

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Every (table, column) the ownership registry marks REQUIRED and revision 0037 /
# 0038 created nullable. Generated from ``vitals.ownership.OWNERSHIP_REGISTRY``;
# the paired contract test recomputes it, so a table that changes its
# classification fails there rather than drifting away from this list.
REQUIRED_OWNERSHIP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("annotations", "subject_id"),
    ("body_measurements", "subject_id"),
    ("body_scans", "subject_id"),
    ("day_context", "subject_id"),
    ("garmin_activities", "subject_id"),
    ("garmin_activities", "integration_connection_id"),
    ("garmin_daily", "subject_id"),
    ("garmin_daily", "integration_connection_id"),
    ("garmin_intraday", "subject_id"),
    ("garmin_intraday", "integration_connection_id"),
    ("garmin_weight_exports", "subject_id"),
    ("garmin_weight_exports", "integration_connection_id"),
    ("genetic_variants", "subject_id"),
    ("glp1_dose_phases", "subject_id"),
    ("glp1_injections", "subject_id"),
    ("glp1_side_effects", "subject_id"),
    ("hevy_workouts", "subject_id"),
    ("hevy_workouts", "integration_connection_id"),
    ("hrt_cycle_templates", "subject_id"),
    ("hrt_cycles", "subject_id"),
    ("hrt_doses", "subject_id"),
    ("hrt_side_effects", "subject_id"),
    ("lab_markers", "subject_id"),
    ("lab_results", "subject_id"),
    ("meal_logs", "subject_id"),
    ("milestones", "subject_id"),
    ("noise_markers", "subject_id"),
    ("notifications", "subject_id"),
    ("progress_photos", "subject_id"),
    ("progress_photos", "file_asset_id"),
    ("raw_payloads", "subject_id"),
    ("shared_reports", "subject_id"),
    ("signals", "subject_id"),
    ("skincare_logs", "subject_id"),
    ("skincare_observations", "subject_id"),
    ("skincare_products", "subject_id"),
    ("supplements", "subject_id"),
    ("weekly_digests", "subject_id"),
    ("weight_logs", "subject_id"),
)


def _refuse_remaining_nulls(bind) -> None:
    """Fail before altering anything if the backfill has rows left to stamp."""

    from sqlalchemy import text

    outstanding = []
    for table_name, column_name in REQUIRED_OWNERSHIP_COLUMNS:
        remaining = bind.scalar(
            text(
                f'SELECT count(*) FROM "{table_name}" '
                f'WHERE "{column_name}" IS NULL'
            )
        )
        if remaining:
            outstanding.append(f"{table_name}.{column_name}: {remaining}")
    if outstanding:
        raise OwnershipBackfillIncompleteError(
            "the ownership backfill has not finished; these columns still hold "
            "rows with no owner and would be rejected by NOT NULL: "
            + ", ".join(outstanding)
            + ". Run the remaining backfill phases to completion, then retry."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _refuse_remaining_nulls(bind)

    if bind.dialect.name == "postgresql":
        for table_name, column_name in REQUIRED_OWNERSHIP_COLUMNS:
            check = f"ck_{table_name}_{column_name}_present"
            op.execute(
                f'ALTER TABLE "{table_name}" ADD CONSTRAINT "{check}" '
                f'CHECK ("{column_name}" IS NOT NULL) NOT VALID'
            )
            op.execute(
                f'ALTER TABLE "{table_name}" VALIDATE CONSTRAINT "{check}"'
            )
            op.execute(
                f'ALTER TABLE "{table_name}" '
                f'ALTER COLUMN "{column_name}" SET NOT NULL'
            )
            op.execute(f'ALTER TABLE "{table_name}" DROP CONSTRAINT "{check}"')
        return

    for table_name, column_name in REQUIRED_OWNERSHIP_COLUMNS:
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column(column_name, nullable=False)


def downgrade() -> None:
    for table_name, column_name in reversed(REQUIRED_OWNERSHIP_COLUMNS):
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column(column_name, nullable=True)
