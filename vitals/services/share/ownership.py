"""Exact owner capability and ownership errors for shared reports."""

from __future__ import annotations

import logging
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.share import SharedReport
from vitals.ownership import WriteIdentity
from vitals.services.identity.governance import acquire_identity_governance_lock

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
POSTGRES_PUBLIC_AUTHORIZATION_ROUTINE = (
    "public.attest_shared_report_token(text)"
)


class ShareOwnershipError(ValueError):
    """A report is outside, or corrupt within, its validated subject scope."""


class SharePreparedOwnerError(ShareOwnershipError):
    """A scoped report operation lacks a live service-issued owner proof."""


class _PublicReportOwnershipError(ShareOwnershipError):
    """Internal public-token validation failure, always mapped to not-found."""


class PreparedShareOwner:
    """Opaque exact-one owner proof bound to one session transaction.

    The capability keeps the identity-governance, subject, and active-owner locks
    alive while legacy whole-lake snapshot readers run.  It cannot be reused
    after a commit, rollback, or savepoint boundary.
    """

    __slots__ = (
        "_identity",
        "_identity_fingerprint",
        "_nested_transaction",
        "_owner_user_id",
        "_seal",
        "_session",
        "_transaction",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise SharePreparedOwnerError(
            "prepared share owners are issued only by prepare_legacy_owner"
        )

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        identity: WriteIdentity,
        owner_user_id: uuid.UUID,
    ) -> "PreparedShareOwner":
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "_identity", identity)
        object.__setattr__(prepared, "_owner_user_id", owner_user_id)
        object.__setattr__(
            prepared,
            "_identity_fingerprint",
            (identity.subject_id, identity.actor_user_id, owner_user_id),
        )
        object.__setattr__(prepared, "_session", session)
        object.__setattr__(
            prepared,
            "_transaction",
            session.sync_session.get_transaction(),
        )
        object.__setattr__(
            prepared,
            "_nested_transaction",
            session.sync_session.get_nested_transaction(),
        )
        object.__setattr__(prepared, "_seal", _PREPARED_OWNER_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedShareOwner is immutable")

    @property
    def identity(self) -> WriteIdentity:
        return self._identity


_PREPARED_OWNER_SEAL = object()


async def legacy_unowned_report_present(session: AsyncSession) -> bool:
    """Whether any shared report is still waiting for the ownership backfill.

    Mirror of the second arm of :func:`_owner_scope`, which is the entire
    widening this bridge performs. It is a different question from how many
    people the installation holds: only if a report belongs to nobody does it
    matter that there is more than one person it could belong to.

    ``scripts/backfill_shared_report_subject_ownership.py`` empties this set,
    run while the installation is still one person. Revision 0049 made
    ``shared_reports.subject_id`` NOT NULL, so on a current schema this is one
    index probe that answers no.
    """

    with session.no_autoflush:
        found = await session.scalar(
            select(SharedReport.id)
            .where(
                SharedReport.subject_id.is_(None),
                SharedReport.created_by_user_id.is_(None),
                SharedReport.revoked_by_user_id.is_(None),
            )
            .limit(1)
        )
    return found is not None


async def prepare_legacy_owner(
    session: AsyncSession,
    *,
    actor_username: str,
) -> PreparedShareOwner:
    """Lock and authenticate the exact-one legacy owner for one transaction."""
    from vitals.services.tenancy.ownership import resolve_legacy_ownership_context

    await acquire_identity_governance_lock(session)
    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
    )
    identity = ownership.owner_action()
    with session.no_autoflush:
        if await legacy_unowned_report_present(session):
            subject_ids = list(
                await session.scalars(
                    select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
                )
            )
            if subject_ids != [identity.subject_id]:
                raise SharePreparedOwnerError(
                    "share compatibility requires exactly one matching "
                    "health subject"
                )
        subject = await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == identity.subject_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subject is None or subject.owner_user_id != ownership.owner_user_id:
            raise SharePreparedOwnerError("share subject owner changed during validation")
        owner = await session.scalar(
            select(User)
            .where(User.id == ownership.owner_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None or owner.status != UserStatus.ACTIVE.value:
            raise SharePreparedOwnerError("share owner is missing or inactive")
        if identity.actor_user_id != owner.id:
            raise SharePreparedOwnerError("share actions require the active subject owner")
    if session.sync_session.get_transaction() is None:  # pragma: no cover
        raise SharePreparedOwnerError("share owner proof has no active transaction")
    return PreparedShareOwner._issue(
        session=session,
        identity=identity,
        owner_user_id=owner.id,
    )


def _require_prepared_owner(
    session: AsyncSession,
    prepared_owner: PreparedShareOwner,
) -> PreparedShareOwner:
    if not isinstance(prepared_owner, PreparedShareOwner):
        raise SharePreparedOwnerError(
            "prepared share owner belongs to another session"
        )
    try:
        identity = prepared_owner._identity
        owner_user_id = prepared_owner._owner_user_id
        valid_fingerprint = prepared_owner._identity_fingerprint == (
            identity.subject_id,
            identity.actor_user_id,
            owner_user_id,
        )
        valid_seal = prepared_owner._seal is _PREPARED_OWNER_SEAL
        prepared_session = prepared_owner._session
        transaction = prepared_owner._transaction
        nested_transaction = prepared_owner._nested_transaction
    except (AttributeError, TypeError) as exc:
        raise SharePreparedOwnerError(
            "prepared share owner is not a valid issued capability"
        ) from exc
    if not valid_seal or not valid_fingerprint:
        raise SharePreparedOwnerError(
            "prepared share owner identity was not issued by the validator"
        )
    if prepared_session is not session:
        raise SharePreparedOwnerError(
            "prepared share owner belongs to another session"
        )
    if session.sync_session.get_transaction() is not transaction:
        raise SharePreparedOwnerError(
            "prepared share owner transaction is no longer active"
        )
    if (
        session.sync_session.get_nested_transaction()
        is not nested_transaction
    ):
        raise SharePreparedOwnerError(
            "prepared share owner savepoint is no longer active"
        )
    return prepared_owner


async def _owner_or_zero_subject_legacy(
    session: AsyncSession,
    prepared_owner: PreparedShareOwner | None,
) -> PreparedShareOwner | None:
    """Validate a production capability or quarantine the old zero-subject API.

    Commercial startup always materializes one subject before serving traffic.
    The zero-subject arm exists only for direct legacy/service consumers and the
    pure snapshot test suite; it cannot authorize a production owner route.
    """
    if prepared_owner is not None:
        return _require_prepared_owner(session, prepared_owner)
    await acquire_identity_governance_lock(session)
    if await session.scalar(select(HealthSubject.id).limit(1)) is not None:
        raise SharePreparedOwnerError(
            "share operations require a prepared owner once identity exists"
        )
    return None
