"""Root-transaction outcome hooks for opaque in-memory capabilities.

SQLAlchemy's session-level commit and rollback events also fire for nested
SAVEPOINTs. Provider-facing leases must only become usable after the exact root
transaction that issued them commits, while rollback or ``Session.close()`` must
invalidate them. This registry installs one stable hook set per Session and
routes each callback pair by root transaction object identity.
"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

_HOOK_INSTALLED_KEY = "vitals.transaction_outcome.hook_installed"
_CALLBACKS_KEY = "vitals.transaction_outcome.callbacks"

OutcomeCallback = Callable[[], None]


class TransactionOutcomeError(RuntimeError):
    """A capability was not issued inside one active root transaction."""


def _run_callbacks(sync_session, *, committed: bool) -> None:
    # after_commit/after_rollback fire before a nested transaction is detached.
    # Ignore that event; the exact root remains the capability boundary.
    if sync_session.get_nested_transaction() is not None:
        return
    root = sync_session.get_transaction()
    callbacks = sync_session.info.get(_CALLBACKS_KEY, [])
    matching = [item for item in callbacks if item[0] is root]
    if not matching:
        return
    sync_session.info[_CALLBACKS_KEY] = [
        item for item in callbacks if item[0] is not root
    ]
    for _transaction, on_commit, on_rollback in matching:
        (on_commit if committed else on_rollback)()


def _after_commit(sync_session) -> None:
    _run_callbacks(sync_session, committed=True)


def _after_rollback(sync_session) -> None:
    _run_callbacks(sync_session, committed=False)


def _after_transaction_end(sync_session, transaction) -> None:
    # Session.close() rolls an active root back without emitting after_rollback.
    # A normal commit/rollback already removed its exact-root registrations.
    if transaction.parent is not None or transaction.nested:
        return
    callbacks = sync_session.info.get(_CALLBACKS_KEY, [])
    matching = [item for item in callbacks if item[0] is transaction]
    if not matching:
        return
    sync_session.info[_CALLBACKS_KEY] = [
        item for item in callbacks if item[0] is not transaction
    ]
    for _root, _on_commit, on_rollback in matching:
        on_rollback()


def register_root_transaction_outcome(
    session: AsyncSession,
    *,
    on_commit: OutcomeCallback,
    on_rollback: OutcomeCallback,
) -> None:
    """Register callbacks for the exact active outer transaction.

    Hooks remain installed once per SQLAlchemy Session; completed callback pairs
    are removed immediately, so reusable sessions do not accumulate listeners or
    retain capability payloads.
    """

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    if not callable(on_commit) or not callable(on_rollback):
        raise TypeError("transaction outcome callbacks must be callable")
    sync_session = session.sync_session
    transaction = sync_session.get_transaction()
    if transaction is None or session.in_nested_transaction():
        raise TransactionOutcomeError(
            "capability requires one active outer transaction"
        )
    if not sync_session.info.get(_HOOK_INSTALLED_KEY):
        event.listen(sync_session, "after_commit", _after_commit)
        event.listen(sync_session, "after_rollback", _after_rollback)
        event.listen(sync_session, "after_transaction_end", _after_transaction_end)
        sync_session.info[_HOOK_INSTALLED_KEY] = True
    sync_session.info.setdefault(_CALLBACKS_KEY, []).append(
        (transaction, on_commit, on_rollback)
    )


def pending_root_transaction_outcomes(session: AsyncSession) -> int:
    """Return the registry size for diagnostics and regression tests."""

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    return len(session.sync_session.info.get(_CALLBACKS_KEY, ()))


__all__ = [
    "TransactionOutcomeError",
    "pending_root_transaction_outcomes",
    "register_root_transaction_outcome",
]
