"""Extend row security to the tables revision 0050 deliberately left out.

Revision 0050 covered the forty-one tables whose ``subject_id`` is mandatory,
where a policy comparing it to the session's subject is total: every row is
either this subject's or refused.  The ten tables here have a nullable subject,
and it means something different in each group, so a blanket predicate would
have been wrong rather than merely incomplete.

**Shared catalogs** — ``conflict_rules``, ``hrt_compounds``,
``hrt_compound_components``, ``system_alerts``, ``audit_events``.  A NULL subject
here is a real state: the checked-in conflict catalog and compound catalog belong
to the installation, not to a patient, and so do the platform's own alerts and
its audit journal.  Those rows must stay readable — a safety rule nobody can see
stops firing — so the predicate is *mine or the installation's*.

Note what that does and does not claim.  It keeps one patient's custom rule away
from another patient, which is what row security is for.  It does not keep
operational detail out of patient-facing surfaces: a platform alert is visible to
a bound session, and excluding it from a report remains the application's job
through ``alerts_service.is_platform_alert_key``.  That is a question about
content, not about ownership, and answering it here would have made the
installation's own alerts invisible to the code that manages them.

**Inherited children** — ``body_scan_metrics``, ``hevy_exercises``,
``hevy_sets``, ``hrt_cycle_items``, ``hrt_cycle_template_items``.  These take
their subject from a parent, and a NULL means the backfill has not reached them
yet.  The predicate is strict: an unstamped child is invisible, exactly as an
unstamped row is everywhere else.  Mid-backfill a reader shows precisely the
rows already stamped, which is the invariant the whole conversion holds to.

Downgrade drops the policies and disables row security, as in 0050.  It is pure
schema and reverses cleanly — but it reverses a boundary.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUBJECT_SETTING = "vitals.subject_id"
POLICY_NAME = "rls_subject_isolation"

#: A NULL subject is the installation's own row, and stays readable.
SHARED_WITH_INSTALLATION: tuple[str, ...] = (
    "audit_events",
    "conflict_rules",
    "hrt_compound_components",
    "hrt_compounds",
    "system_alerts",
)

#: A NULL subject is a row the backfill has not reached, and stays invisible.
INHERITED_CHILDREN: tuple[str, ...] = (
    "body_scan_metrics",
    "hevy_exercises",
    "hevy_sets",
    "hrt_cycle_items",
    "hrt_cycle_template_items",
)

_CURRENT = f"NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid"
_STRICT = f"subject_id = {_CURRENT}"
_SHARED = f"(subject_id IS NULL OR subject_id = {_CURRENT})"


def _install(table_name: str, predicate: str) -> None:
    op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{POLICY_NAME}" ON "{table_name}" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table_name in SHARED_WITH_INSTALLATION:
        _install(table_name, _SHARED)
    for table_name in INHERITED_CHILDREN:
        _install(table_name, _STRICT)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table_name in reversed(INHERITED_CHILDREN + SHARED_WITH_INSTALLATION):
        op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')
        op.execute(f'ALTER TABLE "{table_name}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY')
