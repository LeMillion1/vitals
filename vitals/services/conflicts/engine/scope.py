"""Subject, actor, transaction, and legacy-bridge scope validation."""

from __future__ import annotations

import uuid
from datetime import date as date_type

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.ownership import WriteIdentity
from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.conflicts.engine.contracts import (
    ConflictActorInactive,
    ConflictActorNotFound,
    ConflictActorOwnershipError,
    ConflictLegacyBridgeError,
    ConflictPreparedWriteError,
    ConflictScope,
    ConflictSubjectNotFound,
    ConflictUnsupportedDatabaseError,
    ConflictWriteContext,
    LegacyConflictBridge,
    PreparedConflictWrite,
    _PREPARED_WRITE_SEAL,
    _write_context_fingerprint,
)
from vitals.services.conflicts.engine.registry import _resolvers
from vitals.utils.timeutils import today_local


def _domain_value(domain: Domain | str) -> str:
    try:
        return Domain(domain).value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown conflict domain: {domain!r}") from exc


async def _acquire_legacy_governance_lock(session: AsyncSession) -> None:
    from vitals.services.identity.contracts import UnsupportedIdentityDatabaseError
    from vitals.services.identity.governance import acquire_identity_governance_lock

    try:
        await acquire_identity_governance_lock(session)
    except UnsupportedIdentityDatabaseError as exc:
        raise ConflictUnsupportedDatabaseError(str(exc)) from exc


async def _bridge_can_adopt_anything(session: AsyncSession) -> bool:
    """Whether the fully-unowned bridge would widen anything in this database.

    Asked before demanding a sole health subject, because those are two
    different questions and only one of them is about people. "Is there a row
    nobody owns" is what the bridge exists for; "is this installation one
    person" is what has to hold before adopting such a row, and it only has to
    hold if the first answer is yes.

    Caller holds the identity governance lock, so nothing can become a second
    subject between this and the proof. The rows themselves cannot appear
    underneath it either: the columns every fact-side widening tests were made
    ``NOT NULL`` by revision 0049, and the two catalogs where a null subject is
    still legal are written only by the checked-in seeders, which always stamp
    the ``code`` or ``key`` that classifies the row as global. What is left is
    exactly what a pre-0049 installation carried in, which is what
    ``scripts/backfill_*_subject_ownership.py`` is for.
    """

    from vitals.services.conflicts.activation import (
        unclassified_global_rule_exists,
    )

    if await unclassified_global_rule_exists(session):
        return True
    for registration in _resolvers.values():
        probe = registration.legacy_probe
        if probe is not None and await probe(session):
            return True
    return False


async def _validate_scope(session: AsyncSession, scope: ConflictScope) -> None:
    if not isinstance(scope, ConflictScope):
        raise TypeError("scope must be a ConflictScope")
    if scope.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED:
        # The proof and every resolver read must share this transaction-scoped
        # lock. Otherwise a second subject can commit between the count and a
        # later resolver query, causing fully-unowned facts to be adopted after
        # the installation has already become multi-subject.
        await _acquire_legacy_governance_lock(session)
        if not await _bridge_can_adopt_anything(session):
            # Nothing to adopt, so the widening selects nothing and there is
            # nobody's row to decide the owner of. The bridge stays recorded on
            # the scope — callers such as the Garmin ingest read it as "this is
            # the legacy path" — but it has no effect to prove safe.
            return
        subject_ids = list(
            await session.scalars(select(HealthSubject.id).order_by(HealthSubject.id).limit(2))
        )
        if subject_ids != [scope.subject_id]:
            raise ConflictLegacyBridgeError(
                "fully-unowned conflict reads require exactly one matching health subject"
            )
        return
    exists = await session.scalar(
        select(HealthSubject.id).where(HealthSubject.id == scope.subject_id)
    )
    if exists is None:
        raise ConflictSubjectNotFound("health subject does not exist")


def _require_write_context(context: ConflictWriteContext) -> None:
    if not isinstance(context, ConflictWriteContext):
        raise TypeError("context must be a ConflictWriteContext")


def _require_typed_domain(domain: Domain) -> None:
    if not isinstance(domain, Domain):
        raise TypeError("domain must be a Domain")


def _alert_bridge(context: ConflictWriteContext) -> alerts_service_contracts.LegacyAlertBridge:
    if context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED:
        return alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED
    return alerts_service_contracts.LegacyAlertBridge.REJECT


def _health_alert_context(
    context: ConflictWriteContext,
) -> alerts_service_contracts.HealthAlertContext:
    return alerts_service_contracts.HealthAlertContext(context.identity)


async def prepare_scoped_write(
    session: AsyncSession,
    *,
    context: ConflictWriteContext,
) -> PreparedConflictWrite:
    """Lock and validate the identity roots for one scoped conflict write.

    Identity governance is always taken before subject/user row locks. Besides
    freezing the compatibility bridge's exact-one proof, this prevents a strict
    write from deadlocking against an identity mutation that takes governance
    and user locks before reaching the subject row.
    """

    _require_write_context(context)
    await _acquire_legacy_governance_lock(session)
    # Only the subject *count* turns on whether the bridge has anything to
    # adopt. The owner-lifecycle and actor-ownership checks below stay on the
    # requested bridge: they ask who may use the legacy path at all, which is a
    # permission and does not stop being one because there is nothing to widen.
    count_must_be_proved = (
        context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED
        and await _bridge_can_adopt_anything(session)
    )
    with session.no_autoflush:
        if count_must_be_proved:
            subject_ids = list(
                await session.scalars(select(HealthSubject.id).order_by(HealthSubject.id).limit(2))
            )
            if subject_ids != [context.identity.subject_id]:
                raise ConflictLegacyBridgeError(
                    "fully-unowned conflict writes require exactly one matching health subject"
                )

        subject = await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == context.identity.subject_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subject is None:
            raise ConflictSubjectNotFound("health subject does not exist")

        required_user_ids: set[uuid.UUID] = set()
        if context.identity.actor_user_id is not None:
            required_user_ids.add(context.identity.actor_user_id)
        if context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED:
            required_user_ids.add(subject.owner_user_id)

        users = (
            {
                user.id: user
                for user in await session.scalars(
                    select(User)
                    .where(User.id.in_(tuple(required_user_ids)))
                    .order_by(User.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            }
            if required_user_ids
            else {}
        )

        if context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED:
            owner = users.get(subject.owner_user_id)
            if owner is None or owner.status != UserStatus.ACTIVE.value:
                raise ConflictLegacyBridgeError(
                    "fully-unowned conflict writes require an active sole-subject owner"
                )

        actor_user_id = context.identity.actor_user_id
        if actor_user_id is not None:
            actor = users.get(actor_user_id)
            if actor is None:
                raise ConflictActorNotFound("actor user does not exist")
            if actor.status != UserStatus.ACTIVE.value:
                raise ConflictActorInactive("actor user is not active")
            if (
                context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED
                and actor_user_id != subject.owner_user_id
            ):
                raise ConflictActorOwnershipError(
                    "fully-unowned conflict writes require the owner or system actor"
                )

    transaction = session.sync_session.get_transaction()
    if transaction is None:  # pragma: no cover - every SQLAlchemy query autobegins
        raise ConflictPreparedWriteError("conflict write has no active transaction")
    return PreparedConflictWrite._issue(
        context=context,
        session=session,
        transaction=transaction,
        nested_transaction=session.sync_session.get_nested_transaction(),
    )


def _require_live_prepared_write(
    session: AsyncSession,
    prepared: PreparedConflictWrite,
) -> ConflictWriteContext:
    if not isinstance(prepared, PreparedConflictWrite):
        raise ConflictPreparedWriteError("prepared must be a PreparedConflictWrite")
    try:
        valid_seal = prepared._seal is _PREPARED_WRITE_SEAL
        context = prepared._context
        valid_fingerprint = prepared._context_fingerprint == _write_context_fingerprint(context)
        prepared_session = prepared._session
        transaction = prepared._transaction
        nested_transaction = prepared._nested_transaction
    except (AttributeError, TypeError) as exc:
        raise ConflictPreparedWriteError(
            "prepared conflict write is not a valid issued capability"
        ) from exc
    if not valid_seal or not valid_fingerprint:
        raise ConflictPreparedWriteError(
            "prepared conflict write context was not issued by the validator"
        )
    if prepared_session is not session:
        raise ConflictPreparedWriteError("prepared conflict write belongs to another session")
    if session.sync_session.get_transaction() is not transaction:
        raise ConflictPreparedWriteError("prepared conflict write transaction is no longer active")
    if session.sync_session.get_nested_transaction() is not nested_transaction:
        raise ConflictPreparedWriteError("prepared conflict write savepoint is no longer active")
    return context


def require_prepared_identity(
    session: AsyncSession,
    *,
    prepared: PreparedConflictWrite,
    identity: WriteIdentity,
) -> ConflictWriteContext:
    """Validate a capability before a domain service reads its target row.

    Stateful updates often need the locked row to build ``proposed_state``.
    This public guard lets them prove the exact session/transaction/identity
    first, so an invalid token cannot be used to materialize or lock a row from
    another scope before :func:`enforce_prepared` runs.
    """

    if not isinstance(identity, WriteIdentity):
        raise ConflictPreparedWriteError(
            "a prepared conflict write requires an explicit WriteIdentity"
        )
    context = _require_live_prepared_write(session, prepared)
    if context.identity != identity:
        raise ConflictPreparedWriteError("write identity does not match prepared conflict write")
    return context


async def legacy_unowned_raw_present(session: AsyncSession) -> bool:
    """Whether any raw payload belongs to nobody.

    Mirror of the ``fully_unowned`` half of :func:`raw_payload_scope_conditions`.
    Several domains widen to it on a write without their resolver widening on a
    read, so this probe is registered for them rather than being folded into one
    of the fact probes.
    """

    from vitals.models.raw_payload import RawPayload

    found = await session.scalar(
        select(RawPayload.id)
        .where(
            RawPayload.subject_id.is_(None),
            RawPayload.actor_user_id.is_(None),
        )
        .limit(1)
    )
    return found is not None


def raw_payload_scope_conditions(scope: ConflictScope):
    """Return exact-subject and fully-unowned SQL predicates for RawPayload.

    The exact predicate validates every portable ownership root without loading
    raw payload contents. Historical connections may be disabled or retired,
    but an unresolved/pending connection is not established provenance.
    """

    from vitals.enums import IntegrationConnectionStatus
    from vitals.models.identity import HealthSubject
    from vitals.models.raw_payload import RawPayload
    from vitals.models.tenancy import FileAsset, IntegrationConnection

    historical_statuses = tuple(
        status.value
        for status in IntegrationConnectionStatus
        if status is not IntegrationConnectionStatus.PENDING
    )
    owner_user_id = (
        select(HealthSubject.owner_user_id)
        .where(HealthSubject.id == scope.subject_id)
        .scalar_subquery()
    )
    exact = and_(
        RawPayload.id.is_not(None),
        RawPayload.subject_id == scope.subject_id,
        or_(
            RawPayload.actor_user_id.is_(None),
            RawPayload.actor_user_id == owner_user_id,
        ),
        or_(
            RawPayload.integration_connection_id.is_(None),
            exists(
                select(1).where(
                    IntegrationConnection.id == RawPayload.integration_connection_id,
                    IntegrationConnection.subject_id == scope.subject_id,
                    IntegrationConnection.status.in_(historical_statuses),
                )
            ),
        ),
        or_(
            RawPayload.file_asset_id.is_(None),
            exists(
                select(1).where(
                    FileAsset.id == RawPayload.file_asset_id,
                    FileAsset.subject_id == scope.subject_id,
                )
            ),
        ),
    )
    fully_unowned = and_(
        RawPayload.id.is_not(None),
        RawPayload.subject_id.is_(None),
        RawPayload.actor_user_id.is_(None),
        RawPayload.integration_connection_id.is_(None),
        RawPayload.file_asset_id.is_(None),
    )
    return exact, fully_unowned


_CURATED_RULE_FIELDS = (
    "rule_type",
    "domain_a",
    "condition_a",
    "domain_b",
    "condition_b",
    "severity",
    "message",
    "params",
    "category",
    "source",
    "evidence",
)


async def resolve_legacy_conflict_write_context(
    session: AsyncSession,
    *,
    actor_username: str | None,
    evaluation_date: date_type | None = None,
) -> ConflictWriteContext:
    """Resolve the registration-disabled owner into an explicit write context.

    The governance lock precedes every exact-one, owner-lifecycle, and username
    proof and remains held for the surrounding transaction. A username denotes
    an authenticated owner action; ``None`` denotes a trusted system/job action.
    """

    from vitals.services.tenancy.ownership import resolve_legacy_ownership_context

    await _acquire_legacy_governance_lock(session)
    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
    )
    identity = ownership.system_action() if actor_username is None else ownership.owner_action()
    return ConflictWriteContext(
        identity=identity,
        evaluation_date=evaluation_date or today_local(),
        legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
    )


async def resolve_subject_conflict_write_context(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    evaluation_date: date_type | None = None,
) -> ConflictWriteContext:
    """The same write context, for a system boundary that names its subject.

    A scheduled job has no account, so it cannot go through the actor path; and
    it must not go through the actorless one, which asks for "the sole subject"
    and therefore refused outright the moment a second person existed. It says
    which record it is running for instead, once per subject.

    The subject is mandatory. An omittable one is the shape
    ``vitals/legacy_scope.py`` exists to keep out, and here it would also put the
    old refusal back within reach of a caller who simply forgot.
    """

    from vitals.services.tenancy.ownership import resolve_subject_ownership_context

    await _acquire_legacy_governance_lock(session)
    ownership = await resolve_subject_ownership_context(
        session,
        subject_id=subject_id,
    )
    return ConflictWriteContext(
        identity=ownership.system_action(),
        evaluation_date=evaluation_date or today_local(),
        legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
    )


async def resolve_legacy_conflict_scope(
    session: AsyncSession,
    *,
    actor_username: str | None,
    evaluation_date: date_type | None = None,
) -> ConflictScope:
    """Resolve and authenticate the exact-one owner under one governance lock."""

    from vitals.services.tenancy.ownership import resolve_legacy_ownership_context

    # Lock before sampling subject cardinality, owner lifecycle, or actor
    # identity. The transaction retains it through the caller's subsequent rule
    # and resolver reads; identity mutations use the same governance lock.
    await _acquire_legacy_governance_lock(session)
    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
    )
    return ConflictScope(
        subject_id=ownership.subject_id,
        evaluation_date=evaluation_date or today_local(),
        legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
    )
