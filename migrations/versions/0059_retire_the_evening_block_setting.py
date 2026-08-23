"""Strip ``evening_time`` from the stored proactive policies.

Revision ID: 0059
Revises: 0058
Create Date: 2026-08-24

The evening block was the 23:45 message that asked what the day had been made
of — office or remote, gym or not, how heavy — and wrote the answers to
``day_context``. Revision 0058 dropped that table along with the chat that asked
the questions. Nothing schedules an evening block any more, so the time field
that said when to send it set a schedule with no reader, and the settings card
kept offering it.

This is a data migration, not a cosmetic one, because the policy decoder is
strict on purpose: ``prefs._strict_object`` compares the stored key set against
``_SUBJECT_FIELDS`` with ``!=``, so a row carrying a field the code no longer
knows raises ``ProactivePreferencesUnavailableError`` rather than ignoring it.
That strictness is the right default — a preference row that has drifted from
the code is worth failing on — and it means retiring a field has to rewrite the
rows in the same revision that removes it.

Two shapes hold the same policy:

* ``subject_settings`` under ``proactive_subject_policy`` — the live per-person
  row, alongside ``brief_time`` and ``nudges``. One row per subject, so this
  rewrites each by its own primary key rather than by the shared key alone.
* ``app_settings`` under ``proactive`` — the flat pre-identity row, which also
  carries the delivery and Garmin fields. Read by installations that have not
  been through identity bootstrap yet.

``downgrade`` restores the default rather than the value that was there: the old
value is not recoverable from here, and a row *missing* the key fails the same
strict decoder on an older binary, so putting something back is the only shape
that starts. 23:45 is what ``DEFAULTS`` held, so a downgraded installation reads
what a fresh one did.
"""
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059"
down_revision: Union[str, None] = "0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FIELD = "evening_time"
_DEFAULT = "23:45"


def _json_type():
    return (
        postgresql.JSONB
        if op.get_bind().dialect.name == "postgresql"
        else sa.JSON
    )


def _tables() -> tuple[sa.Table, sa.Table]:
    """Minimal reflection-free shapes — only the columns this touches."""

    json_type = _json_type()
    metadata = sa.MetaData()
    subject_settings = sa.Table(
        "subject_settings",
        metadata,
        sa.Column("subject_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", json_type),
    )
    app_settings = sa.Table(
        "app_settings",
        metadata,
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", json_type),
    )
    return subject_settings, app_settings


def _edit(value: Any, *, drop: bool) -> dict[str, Any] | None:
    """Return the rewritten policy, or ``None`` when there is nothing to do.

    A value that is not an object is malformed under any reading and is left
    exactly as found: a migration is the wrong place to guess what a broken
    preference meant.
    """

    if not isinstance(value, dict):
        return None
    if drop:
        if _FIELD not in value:
            return None
        updated = dict(value)
        updated.pop(_FIELD)
        return updated
    if _FIELD in value:
        return None
    return {**value, _FIELD: _DEFAULT}


def _rewrite(*, drop: bool) -> None:
    bind = op.get_bind()
    subject_settings, app_settings = _tables()

    rows = bind.execute(
        sa.select(subject_settings.c.subject_id, subject_settings.c.value).where(
            subject_settings.c.key == "proactive_subject_policy"
        )
    ).fetchall()
    for subject_id, value in rows:
        updated = _edit(value, drop=drop)
        if updated is None:
            continue
        bind.execute(
            subject_settings.update()
            .where(
                subject_settings.c.subject_id == subject_id,
                subject_settings.c.key == "proactive_subject_policy",
            )
            .values(value=updated)
        )

    rows = bind.execute(
        sa.select(app_settings.c.value).where(app_settings.c.key == "proactive")
    ).fetchall()
    for (value,) in rows:
        updated = _edit(value, drop=drop)
        if updated is None:
            continue
        bind.execute(
            app_settings.update()
            .where(app_settings.c.key == "proactive")
            .values(value=updated)
        )


def upgrade() -> None:
    _rewrite(drop=True)


def downgrade() -> None:
    _rewrite(drop=False)
