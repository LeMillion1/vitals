"""Transaction-scoped identity locks and explicit pre-identity compatibility.

PostgreSQL uses one stable transaction advisory lock for every mutation that can
change identity, role, credential, or bootstrap state. SQLite is the fast test
path and deliberately uses its bounded compatibility behavior.

The pre-identity functions are an explicit legacy bridge. They may be removed
only after every environment-backed zero-subject write path is retired and the
architecture tests prove that no production caller needs a pre-bootstrap write.
They fail closed as soon as a health subject exists.
"""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.identity import HealthSubject
from vitals.services.identity.contracts import (
    PreIdentityCompatibilityError,
    UnsupportedIdentityDatabaseError,
)

IDENTITY_GOVERNANCE_LOCK_NAMESPACE = 0x5649544C
IDENTITY_GOVERNANCE_LOCK_KEY = 1
_PRE_IDENTITY_COMPATIBILITY_TRANSACTION_KEY = (
    "vitals.identity.pre_identity_compatibility_transaction"
)


async def acquire_identity_governance_lock(session: AsyncSession) -> None:
    """Serialize identity-governance mutations for the current transaction."""

    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return
    if dialect != "postgresql":
        raise UnsupportedIdentityDatabaseError(
            f"identity governance does not support database dialect {dialect!r}"
        )
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock(CAST(:namespace AS INTEGER), "
            "CAST(:lock_key AS INTEGER))"
        ),
        {
            "namespace": IDENTITY_GOVERNANCE_LOCK_NAMESPACE,
            "lock_key": IDENTITY_GOVERNANCE_LOCK_KEY,
        },
    )


def _reject_pending_subject_state(sync_session) -> None:
    subject_state = (
        tuple(sync_session.new) + tuple(sync_session.dirty) + tuple(sync_session.deleted)
    )
    if any(isinstance(row, HealthSubject) for row in subject_state):
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility rejects pending subject identity state"
        )


async def _guard_pre_identity_root(session: AsyncSession) -> object:
    sync_session = session.sync_session
    transaction = sync_session.get_transaction()
    if (
        transaction is None
        or not transaction.is_active
        or sync_session.info.get(_PRE_IDENTITY_COMPATIBILITY_TRANSACTION_KEY)
        is not transaction
    ):
        pending_transaction = transaction
        with session.no_autoflush:
            if session.get_bind().dialect.name == "sqlite":
                if transaction is None or not bool(
                    getattr(transaction, "_connections", {})
                ):
                    await session.execute(text("BEGIN IMMEDIATE"))
            else:
                await acquire_identity_governance_lock(session)
        transaction = sync_session.get_transaction()
        if (
            transaction is None
            or not transaction.is_active
            or (
                pending_transaction is not None
                and transaction is not pending_transaction
            )
        ):
            raise PreIdentityCompatibilityError(
                "pre-identity compatibility could not establish a guarded transaction"
            )
        sync_session.info[_PRE_IDENTITY_COMPATIBILITY_TRANSACTION_KEY] = transaction

    with session.no_autoflush:
        subject_id = await session.scalar(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(1)
        )
    if subject_id is not None:
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility is closed after identity bootstrap"
        )
    return transaction


async def authorize_pre_identity_compatibility_transaction(
    session: AsyncSession,
) -> object:
    """Authorize one fresh guarded root transaction while zero subjects exist."""

    sync_session = session.sync_session
    if sync_session.get_nested_transaction() is not None:
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility requires a fresh outer transaction"
        )
    _reject_pending_subject_state(sync_session)
    transaction = sync_session.get_transaction()
    authorized = sync_session.info.get(_PRE_IDENTITY_COMPATIBILITY_TRANSACTION_KEY)
    if (
        transaction is not None
        and not (transaction is authorized and transaction.is_active)
        and (
            not transaction.is_active
            or bool(getattr(transaction, "_connections", {None: None}))
        )
    ):
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility requires a fresh guarded transaction"
        )
    return await _guard_pre_identity_root(session)


async def require_pre_identity_compatibility(session: AsyncSession) -> object:
    """Adopt an open root and prove the same zero-subject legacy invariant."""

    sync_session = session.sync_session
    if sync_session.get_nested_transaction() is not None:
        raise PreIdentityCompatibilityError(
            "pre-identity compatibility requires a fresh outer transaction"
        )
    _reject_pending_subject_state(sync_session)
    return await _guard_pre_identity_root(session)


__all__ = [
    "IDENTITY_GOVERNANCE_LOCK_KEY",
    "IDENTITY_GOVERNANCE_LOCK_NAMESPACE",
    "acquire_identity_governance_lock",
    "authorize_pre_identity_compatibility_transaction",
    "require_pre_identity_compatibility",
]
