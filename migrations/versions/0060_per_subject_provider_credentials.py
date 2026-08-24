"""Somewhere for a Garmin account that belongs to one patient.

Revision ID: 0060
Revises: 0059
Create Date: 2026-08-24

``VITALS_GARMIN_EMAIL``, ``VITALS_GARMIN_PASSWORD`` and ``VITALS_HEVY_API_KEY``
are one watch and one workout account for the whole process. That is a
single-user shape, and it is the reason four scheduled jobs still cannot be run
once per subject: doing so with those credentials would write the operator's own
watch data into everybody else's record, which turns an outage into a
disclosure.

This creates the table a per-person credential goes in.

**It holds ciphertext, and that is the whole reason it is a separate table.**
``integration_connections`` says in its own docstring that secret material never
belongs there, and that stays true — ``credential_ref`` on that row is a handle
naming where the secret is, and ``vault:v1`` is one of the things it can name.
The plaintext is a small JSON object; the key is ``VITALS_CREDENTIAL_KEY`` and
belongs to the installation, which is the kind of thing ``.env`` is for.

**The foreign key is composite on purpose.** ``integration_connections`` already
carries ``uq_integration_connections_id_subject`` so that a child row can name
both the connection and its subject and have the database check they agree. A
credential whose two owners disagree is the exact row this migration exists to
make impossible, so it is a constraint rather than a rule readers must remember.

``key_version`` has no second value yet. It is here so that rotating the
installation key is later a migration that can read old rows while writing new
ones, rather than an outage that invalidates every stored credential at once.

**It also takes one ref away from everybody it was never about.** The tenancy
bootstrap wrote ``credential_ref = 'legacy_env:garmin'`` on *every* subject's
roots, without knowing whose they were — harmless while nothing resolved it, and
a disclosure the moment something did, because it says "my Garmin password is in
``.env``" and ``.env`` holds the operator's. This clears that ref from every
Garmin and Hevy connection except the one belonging to ``VITALS_AUTH_USERNAME``,
whose account those values really are. OpenRouter and Telegram keep theirs:
those are installation-wide gateways and the ref means what it says for
everybody.

If ``VITALS_AUTH_USERNAME`` is unset when this runs, every such ref is cleared.
That fails closed — the owner re-enters their credentials on the settings card,
which is a form — rather than guessing which record the environment describes.

``downgrade`` drops the table, and says plainly that it drops the credentials
with it. They are not recoverable from anywhere else — ``.env`` holds at most
the installation owner's — and re-entering them is a form somebody fills in. It
does *not* put the cleared refs back: they were wrong, and restoring them would
restore the disclosure.
"""
import os
import unicodedata
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: Union[str, None] = "0059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SUBJECT_ISOLATED_TABLES: tuple[str, ...] = ("integration_credentials",)

SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
POLICY_NAME = "rls_subject_isolation"
_PREDICATE = (
    f"(subject_id = NULLIF(current_setting('{SUBJECT_SETTING}', true), '')::uuid "
    f"OR current_setting('{PLATFORM_SETTING}', true) = 'on')"
)


def upgrade() -> None:
    op.create_table(
        "integration_credentials",
        sa.Column(
            "integration_connection_id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("health_subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["integration_connection_id", "subject_id"],
            [
                "integration_connections.id",
                "integration_connections.subject_id",
            ],
            name="fk_integration_credentials_connection_subject",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "key_version >= 1", name="ck_integration_credentials_key_version"
        ),
        sa.CheckConstraint(
            "length(ciphertext) > 0", name="ck_integration_credentials_ciphertext"
        ),
    )
    op.create_index(
        "ix_integration_credentials_subject",
        "integration_credentials",
        ["subject_id"],
    )

    _clear_borrowed_environment_refs()

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table_name in SUBJECT_ISOLATED_TABLES:
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "{table_name}" '
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def _owner_lookup_key() -> str:
    """``VITALS_AUTH_USERNAME`` in the form ``users.normalized_username`` holds.

    Mirrors ``identity_service.normalize_username`` rather than importing it: a
    migration that follows the application's code follows it forward too, and
    this has to keep meaning the same thing in five years.
    """

    raw = (os.getenv("VITALS_AUTH_USERNAME") or "").strip()
    if not raw:
        return ""
    return unicodedata.normalize("NFKC", raw).casefold()


def _clear_borrowed_environment_refs() -> None:
    connections = sa.table(
        "integration_connections",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("subject_id", sa.Uuid(as_uuid=True)),
        sa.column("provider", sa.String),
        sa.column("credential_ref", sa.String),
    )
    subjects = sa.table(
        "health_subjects",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("owner_user_id", sa.Uuid(as_uuid=True)),
    )
    users = sa.table(
        "users",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("normalized_username", sa.String),
    )

    bind = op.get_bind()
    lookup_key = _owner_lookup_key()
    keep: set = set()
    if lookup_key:
        keep = {
            row[0]
            for row in bind.execute(
                sa.select(subjects.c.id)
                .select_from(
                    subjects.join(users, users.c.id == subjects.c.owner_user_id)
                )
                .where(users.c.normalized_username == lookup_key)
            )
        }

    condition = sa.and_(
        connections.c.provider.in_(("garmin", "hevy")),
        connections.c.credential_ref.like("legacy_env:%"),
    )
    if keep:
        condition = sa.and_(condition, connections.c.subject_id.notin_(keep))
    bind.execute(
        sa.update(connections).where(condition).values(credential_ref=None)
    )


def downgrade() -> None:
    """Drops the table, and with it every stored provider credential.

    Stated rather than left to be discovered. There is no other copy: ``.env``
    holds at most the installation owner's, and nothing else in the schema
    carries a Garmin password. After a downgrade every subject who had connected
    an account has to enter it again.
    """

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table_name in SUBJECT_ISOLATED_TABLES:
            op.execute(f'DROP POLICY IF EXISTS "{POLICY_NAME}" ON "{table_name}"')
    op.drop_index(
        "ix_integration_credentials_subject", table_name="integration_credentials"
    )
    op.drop_table("integration_credentials")
