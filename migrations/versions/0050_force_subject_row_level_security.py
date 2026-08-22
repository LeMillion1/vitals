"""Enforce subject isolation in the database, not only in the application.

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-22

Every read in this application is now scoped, and revision 0049 made the column
those scopes rest on mandatory.  This revision adds the second boundary: the
database itself refuses to return another person's row, whatever query reaches
it.  The two are not redundant.  Application scoping is a property of the code
that happens to be running; a policy is a property of the data, and it survives
a new query written in a hurry, a report assembled by hand, and a console
session opened during an incident.

The policy compares ``subject_id`` to ``vitals.subject_id``, a session setting
the application assigns per transaction.  An unset setting reads as NULL, the
comparison is NULL, and no row qualifies — so a connection that forgot to say
whose request it is serving sees nothing rather than everything.  ``NULLIF``
is there because an empty string is not a UUID and would raise instead of
filtering; empty and unset must mean the same thing.

``FORCE`` is the operative word.  PostgreSQL exempts a table's owner from its
own policies, and the application connects as the owner, so without ``FORCE``
every policy here would be inert for the one role that matters.  Roles that
must see across subjects — the migration runner, the ownership backfill, the
platform control plane — are exempt by role attribute (``BYPASSRLS``) or by
being superuser, which is deliberate: a backfill that could not see an
unstamped row could not stamp it.

Scope is the forty-one tables whose ``subject_id`` is mandatory, where the
comparison is total.  Tables whose subject is ``OPTIONAL`` or ``MIXED`` —
``system_alerts`` and ``conflict_rules`` — need "mine or the installation's"
rather than "mine", and the inherited children carry a nullable subject whose
NULL means different things per table.  Both groups need their own reviewed
predicate and are deliberately not covered by a blanket one here.

Downgrade drops every policy and disables row security.  Unlike the ownership
columns this is pure schema and holds no data, so it reverses cleanly — but it
reverses a boundary, and after a second subject exists the application scoping
underneath is the only thing left.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The session setting the policies read. ``SET LOCAL`` scopes it to one
#: transaction, so it cannot outlive the request that set it or leak into the
#: next checkout of a pooled connection.
SUBJECT_SETTING = "vitals.subject_id"

POLICY_NAME = "rls_subject_isolation"

#: Tables whose ``subject_id`` is mandatory, so the policy's comparison covers
#: every row. Generated from ``vitals.ownership.OWNERSHIP_REGISTRY``; the paired
#: contract test recomputes it and fails when a table joins or leaves the set.
SUBJECT_ISOLATED_TABLES: tuple[str, ...] = (
    "ai_invocations",
    "ai_subject_quota_periods",
    "annotations",
    "body_measurements",
    "body_scans",
    "day_context",
    "file_assets",
    "garmin_activities",
    "garmin_daily",
    "garmin_intraday",
    "garmin_weight_exports",
    "genetic_variants",
    "glp1_dose_phases",
    "glp1_injections",
    "glp1_side_effects",
    "hevy_workouts",
    "hrt_cycle_templates",
    "hrt_cycles",
    "hrt_doses",
    "hrt_side_effects",
    "integration_connections",
    "lab_markers",
    "lab_results",
    "meal_logs",
    "milestones",
    "noise_markers",
    "notification_delivery_intents",
    "notifications",
    "ownership_backfill_checkpoints",
    "progress_photos",
    "raw_payloads",
    "shared_reports",
    "signals",
    "skincare_logs",
    "skincare_observations",
    "skincare_products",
    "subject_settings",
    "supplements",
    "support_access_grants",
    "weekly_digests",
    "weight_logs",
)

_PREDICATE = (
    f"subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite has no row security. The fast suite proves the application
        # scoping; the policies themselves are proven on PostgreSQL.
        return
    for table_name in SUBJECT_ISOLATED_TABLES:
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "{table_name}" '
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table_name in reversed(SUBJECT_ISOLATED_TABLES):
        op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')
        op.execute(f'ALTER TABLE "{table_name}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY')
