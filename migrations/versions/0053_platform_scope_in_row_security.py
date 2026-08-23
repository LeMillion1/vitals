"""Let a transaction act for the installation, and say so.

Revisions 0050 and 0051 gave every table that names a subject a policy comparing
it to the session's subject.  That is right for every request made *by* somebody
and wrong for the two kinds of work that are not.

A published report is opened by a visitor holding a token.  The token is the
authorization — that is the whole design of the feature — and there is no
session to bind because there is no account.  Under the policies as written the
row simply did not exist, so every published link answered "not found", which is
also what a revoked link answers.  Nothing failed loudly; the feature just
stopped.

Housekeeping jobs are the second kind.  Sweeping unprocessed raw payloads and
reconciling provider invocations are about the installation's own state, across
everybody, and have no person to act as.

So the policies gain a second, explicit way to qualify: a transaction may
declare itself the installation's by setting ``vitals.platform_scope``.  Both
settings are transaction-local, so neither outlives its request or rides a
pooled connection into the next one.

This is a widening and worth being uncomfortable about.  What keeps it narrow is
that it must be asked for by name, in code, and a contract test enumerates every
caller — three today — so a fourth is something a reviewer sees rather than
something that accumulates.  It is not a fallback for a path that forgot to bind
a subject: an unbound session seeing nothing is the design, and reaching for
this to make an empty page non-empty would turn the boundary off for that
request.

Downgrade restores the subject-only predicates, which is the schema 0051 left —
and reinstates the broken published link along with it.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"

_CURRENT = f"NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid"
_PLATFORM = f"current_setting('{PLATFORM_SETTING}', true) = 'on'"

_STRICT_OLD = f"subject_id = {_CURRENT}"
_SHARED_OLD = f"(subject_id IS NULL OR subject_id = {_CURRENT})"

_STRICT_NEW = f"(subject_id = {_CURRENT} OR {_PLATFORM})"
_SHARED_NEW = f"(subject_id IS NULL OR subject_id = {_CURRENT} OR {_PLATFORM})"


def _tables() -> tuple[tuple[str, str, str], ...]:
    """Every policy installed so far, with its old and new predicate."""

    from importlib import import_module

    isolated = import_module(
        "migrations.versions.0050_force_subject_row_level_security"
    )
    catalogs = import_module(
        "migrations.versions.0051_row_security_for_catalogs_and_children"
    )
    rows: list[tuple[str, str, str]] = []
    for table_name in isolated.SUBJECT_ISOLATED_TABLES:
        rows.append((table_name, _STRICT_OLD, _STRICT_NEW))
    for table_name in catalogs.INHERITED_CHILDREN:
        rows.append((table_name, _STRICT_OLD, _STRICT_NEW))
    for table_name in catalogs.SHARED_WITH_INSTALLATION:
        rows.append((table_name, _SHARED_OLD, _SHARED_NEW))
    return tuple(rows)


def _replace(predicate_index: int) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for row in _tables():
        table_name, predicate = row[0], row[predicate_index]
        op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')
        op.execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "{table_name}" '
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def upgrade() -> None:
    _replace(2)


def downgrade() -> None:
    _replace(1)
