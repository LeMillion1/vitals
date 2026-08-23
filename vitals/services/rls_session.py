"""Tell the database whose request this transaction is serving.

Revision 0050 gives every subject-owned table a policy that compares
``subject_id`` to the ``vitals.subject_id`` session setting. This module is the
other half: the application sets that value once the subject is known, and the
database refuses everything else.

The binding is transaction-scoped on purpose. ``set_config(..., is_local =>
true)`` is undone at commit or rollback, so a subject cannot outlive the request
that resolved it or ride a pooled connection into the next one — the leak that
makes connection-level session variables a liability. Because it is undone at
commit, a session that commits and keeps working would lose it, so the subject
is remembered on the session and re-applied whenever a new transaction begins.

An unbound session is not an unrestricted one. The setting reads as NULL, the
policy's comparison is NULL, and no row qualifies: code that forgot to say whose
data it wants sees nothing rather than everything.
"""

from __future__ import annotations

import uuid

from sqlalchemy import event, text
from sqlalchemy.orm import Session

#: Name of the PostgreSQL session setting the policies read. Must match the
#: constant in revision 0050; the paired contract test pins that it does.
SUBJECT_SETTING = "vitals.subject_id"

#: The other thing a transaction may be acting for. Some work legitimately
#: belongs to the installation rather than to a person: the published report
#: that a visitor opens with a token, where the token *is* the authorization
#: and there is no session to bind; and the housekeeping jobs that sweep
#: unprocessed payloads or reconcile provider invocations across everybody.
#:
#: Declaring it is deliberate and narrow. It is transaction-local like the
#: subject, it must be asked for by name, and a contract test enumerates every
#: caller — so a fourth one is something a reviewer sees rather than something
#: that accumulates.
PLATFORM_SETTING = "vitals.platform_scope"

_SUBJECT_KEY = "vitals_rls_subject_id"
_PLATFORM_KEY = "vitals_rls_platform_scope"


class RlsSessionError(RuntimeError):
    """A session was asked to serve a subject it cannot be bound to."""


def _apply(connection, subject_id: uuid.UUID) -> None:
    if connection.dialect.name != "postgresql":
        # SQLite has no row security; the fast suite proves the application
        # scoping and the policies are proven against PostgreSQL.
        return
    connection.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": SUBJECT_SETTING, "value": str(subject_id)},
    )


def _apply_platform(connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text("SELECT set_config(:name, 'on', true)"), {"name": PLATFORM_SETTING}
    )


@event.listens_for(Session, "after_begin")
def _rebind_on_new_transaction(session, transaction, connection) -> None:
    """Re-apply the binding to each transaction the session opens.

    ``set_config`` with ``is_local`` is discarded at commit, so a service that
    commits and continues would otherwise carry on against a policy that now
    matches nothing. Remembering the subject on the session and re-applying here
    keeps the two in step without ever making the setting outlive a transaction.
    """

    subject_id = session.info.get(_SUBJECT_KEY)
    if subject_id is not None:
        _apply(connection, subject_id)
    if session.info.get(_PLATFORM_KEY):
        _apply_platform(connection)


async def bind_session_subject(session, subject_id: uuid.UUID) -> None:
    """Bind this session's transactions to one health subject.

    Idempotent for the same subject. Rebinding a session to a *different*
    subject is refused: one transaction serves one person, and silently
    switching would make every row already loaded in the identity map belong to
    the wrong policy.
    """

    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise RlsSessionError("subject_id must be a non-zero UUID")
    current = session.info.get(_SUBJECT_KEY)
    if current is not None and current != subject_id:
        raise RlsSessionError(
            "this session is already serving a different health subject"
        )
    session.info[_SUBJECT_KEY] = subject_id
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": SUBJECT_SETTING, "value": str(subject_id)},
    )


async def enter_platform_scope(session) -> None:
    """Declare that this transaction acts for the installation, not for a person.

    Two kinds of work need it, and both are legitimate rather than a way around
    the boundary. A published report is opened by a visitor holding a token: the
    token is the authorization, and there is no session to bind because there is
    no account. Housekeeping jobs — sweeping unprocessed payloads, reconciling
    provider invocations — are about the installation's own state and have no
    person to act as.

    Everything else must bind a subject instead. This is not a fallback for a
    path that forgot to: an unbound session seeing nothing is the design, and
    reaching for this to make an empty page non-empty would turn the boundary
    off for that request.
    """

    session.info[_PLATFORM_KEY] = True
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT set_config(:name, 'on', true)"), {"name": PLATFORM_SETTING}
    )


def in_platform_scope(session) -> bool:
    """Whether this session declared itself the installation's."""

    return bool(session.info.get(_PLATFORM_KEY))


def bound_subject(session) -> uuid.UUID | None:
    """The subject this session is serving, if any."""

    return session.info.get(_SUBJECT_KEY)
