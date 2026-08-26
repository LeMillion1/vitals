"""Require the worker-only database capability for platform-wide RLS.

Revision 0053 made ``vitals.platform_scope=on`` an explicit cross-subject path,
but a custom PostgreSQL setting is not an authorization primitive: any runtime
login can set it.  Web no longer needs that path.  Installation-wide scheduler
jobs do, so the setting is now accepted only when ``session_user`` is a member
of the pure marker role provisioned for this database and worker login.

The marker name includes the current database OID.  PostgreSQL roles are
cluster-global; the suffix isolates parallel databases and ensures a logical
restore into a new database remains closed until role provisioning succeeds.
The catalog lookup deliberately returns false when the role is absent, so the
migrate-before-provision startup order is fail-closed without breaking queries.

Downgrade restores the previous setting-only predicates.  It does not remove a
cluster-global role owned by the provisioner; after downgrade that inert marker
has no effect on policy evaluation.
"""

from importlib import import_module
from typing import Sequence, Union

from alembic import op

revision: str = "0083"
down_revision: Union[str, None] = "0082"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
PLATFORM_CAPABILITY_ROLE_PREFIX = "vitals_platform_scope_db_"
POLICY_NAME = "rls_subject_isolation"

_CURRENT = f"NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid"
_RAW_PLATFORM = f"current_setting('{PLATFORM_SETTING}', true) = 'on'"
_CAPABILITY = f"""
EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles AS capability
    WHERE capability.rolname = '{PLATFORM_CAPABILITY_ROLE_PREFIX}' || (
        SELECT database.oid::text
        FROM pg_catalog.pg_database AS database
        WHERE database.datname = pg_catalog.current_database()
    )
      AND capability.rolcanlogin = false
      AND capability.rolsuper = false
      AND capability.rolcreatedb = false
      AND capability.rolcreaterole = false
      AND capability.rolinherit = false
      AND capability.rolreplication = false
      AND capability.rolbypassrls = false
      AND pg_catalog.pg_has_role(session_user, capability.oid, 'MEMBER')
)
""".strip()
_GATED_PLATFORM = f"({_RAW_PLATFORM} AND {_CAPABILITY})"

_STRICT_OLD = f"(subject_id = {_CURRENT} OR {_RAW_PLATFORM})"
_SHARED_OLD = f"(subject_id IS NULL OR subject_id = {_CURRENT} OR {_RAW_PLATFORM})"
_STRICT_NEW = f"(subject_id = {_CURRENT} OR {_GATED_PLATFORM})"
_SHARED_NEW = f"(subject_id IS NULL OR subject_id = {_CURRENT} OR {_GATED_PLATFORM})"

_POLICY_REVISIONS = (
    "0050_force_subject_row_level_security",
    "0051_row_security_for_catalogs_and_children",
    "0055_professional_invitations",
    "0056_care_relationships_and_consent",
    "0057_professional_notes_and_care_plans",
    "0060_per_subject_provider_credentials",
    "0061_care_team_threads_and_messages",
    "0062_support_access_requests",
    "0063_external_api_tokens",
    "0065_subject_scoped_mcp_grants",
    "0067_care_message_attachments",
    "0069_subject_isolated_care_push_outbox",
)
_DROPPED_TABLES = frozenset({"signals", "day_context"})


def _tables() -> tuple[tuple[str, bool], ...]:
    """Return every live platform-aware table and whether NULL is shared."""

    states: dict[str, bool] = {}
    for stem in _POLICY_REVISIONS:
        module = import_module(f"migrations.versions.{stem}")
        shared = set(getattr(module, "SHARED_WITH_INSTALLATION", ()))
        tables = set(getattr(module, "SUBJECT_ISOLATED_TABLES", ()))
        tables.update(getattr(module, "INHERITED_CHILDREN", ()))
        tables.update(shared)
        for table_name in tables - _DROPPED_TABLES:
            shared_null = table_name in shared
            previous = states.setdefault(table_name, shared_null)
            if previous is not shared_null:
                raise RuntimeError(
                    f"conflicting platform policy kind for {table_name}"
                )
    return tuple(sorted(states.items()))


def _replace(*, gated: bool) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table_name, shared_null in _tables():
        if gated:
            predicate = _SHARED_NEW if shared_null else _STRICT_NEW
        else:
            predicate = _SHARED_OLD if shared_null else _STRICT_OLD
        op.execute(
            f'ALTER POLICY "{POLICY_NAME}" ON "{table_name}" '
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def upgrade() -> None:
    _replace(gated=True)


def downgrade() -> None:
    _replace(gated=False)
