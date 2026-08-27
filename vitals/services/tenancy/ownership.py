"""Fail-closed ownership resolution for unchanged legacy write paths.

This is an explicit transition boundary, not the default tenancy API. It may be
removed only after every production caller accepts a mandatory ``subject_id``
or a prepared access capability and tests prove that no path infers the sole
subject or legacy provider connection by omission.

This adapter gives single-subject callers an authoritative subject, owner,
optional human actor, and only the explicitly requested integration roots.  It
is intentionally read-only: it does not bootstrap or repair identity/tenancy
state, acquire a mutation lock, inspect configuration, flush, or commit.

Disabled connections remain valid provenance roots.  ``status`` describes
whether new provider activity is enabled, while historical facts must continue
to reference the same non-retired connection.  A retired root is never chosen.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import IntegrationConnection
from vitals.persistence.rls import bind_session_subject
from vitals.services.identity.contracts import IdentityValidationError
from vitals.services.identity.normalization import normalize_username
from vitals.services.tenancy.bootstrap import LEGACY_CONNECTION_TYPES
from vitals.services.tenancy.contracts import (
    LegacyActorMismatchError,
    LegacyConnectionAmbiguousError,
    LegacyConnectionMissingError,
    LegacyConnectionRetiredError,
    LegacyConnectionStateError,
    LegacyOwnerResolutionError,
    LegacyOwnershipContext,
    LegacyOwnershipValidationError,
    LegacySubjectResolutionError,
    NoPersonalRecordError,
)


def _validated_required_connections(
    required_connections: Iterable[IntegrationProvider],
) -> tuple[IntegrationProvider, ...]:
    if required_connections is None or isinstance(
        required_connections, (str, bytes)
    ):
        raise LegacyOwnershipValidationError(
            "required_connections must be an iterable of IntegrationProvider members"
        )
    try:
        providers = tuple(required_connections)
    except TypeError as exc:
        raise LegacyOwnershipValidationError(
            "required_connections must be iterable"
        ) from exc

    seen: set[IntegrationProvider] = set()
    for provider in providers:
        if not isinstance(provider, IntegrationProvider):
            raise LegacyOwnershipValidationError(
                "required_connections must contain only IntegrationProvider members"
            )
        if provider not in LEGACY_CONNECTION_TYPES:
            raise LegacyOwnershipValidationError(
                f"provider {provider.value!r} has no legacy connection type"
            )
        if provider in seen:
            raise LegacyOwnershipValidationError(
                f"provider {provider.value!r} was requested more than once"
            )
        seen.add(provider)
    return providers


async def _resolve_ownership(
    session: AsyncSession,
    *,
    actor_username: str | None,
    subject_id: uuid.UUID | None,
    required_connections: Iterable[IntegrationProvider],
) -> LegacyOwnershipContext:
    """Resolve one active-owner context without changing database state.

    Three ways in, and none of them picks a subject on the caller's behalf:

    * ``actor_username`` — the account's own record. Normalized with the same
      NFKC/strip/casefold rule as identity bootstrap, and it must own the
      subject. Additive roles are deliberately irrelevant to ownership.
    * ``subject_id`` with no actor — a trusted system boundary saying whose
      record it is acting on. ``actor_user_id`` stays unset, exactly as before;
      what changes is that the job names the subject instead of the installation
      being required to hold only one.
    * neither — the sole subject, or a refusal. Startup bootstrap still arrives
      this way, and so does anything not yet ported.

    That middle case is the whole point. Every scheduled job used to arrive with
    neither, so the moment a second person existed the digest, the reminders and
    the sweeps all stopped: nothing named whose record was meant, and picking one
    would have been inventing the answer. Naming it is the answer.
    """

    providers = _validated_required_connections(required_connections)
    if subject_id is not None and not isinstance(subject_id, uuid.UUID):
        raise LegacyOwnershipValidationError("subject_id must be a UUID")
    if actor_username is not None and subject_id is not None:
        # Unreachable through either public entry point, and checked anyway: the
        # actor arm would win and the named subject would be silently ignored,
        # which is the kind of disagreement only ever noticed once it has
        # written somewhere.
        raise LegacyOwnershipValidationError(
            "pass an actor or a subject, not both: an actor already names their "
            "own record"
        )
    if actor_username is None:
        actor_lookup_key = None
    else:
        try:
            actor_lookup_key = normalize_username(actor_username).lookup_key
        except IdentityValidationError as exc:
            raise LegacyOwnershipValidationError(str(exc)) from exc

    # Prevent a nominally read-only lookup from autoflushing unrelated pending
    # caller state. Pending identity/tenancy rows are not authoritative roots.
    with session.no_autoflush:
        if actor_lookup_key is not None:
            # A named actor selects *their own* record. This used to ask whether
            # the database held exactly one subject, which is a different
            # question and the wrong one: the check further down already
            # requires the actor to own the subject, so the count was standing
            # in for a relationship it could have asked about directly.
            #
            # It mattered the moment a second person existed. Every page in the
            # app resolves through here, so a doctor taking on one patient lost
            # their own dashboard, their own weight log and their own settings —
            # the count refused, and it refused for everybody rather than for
            # the one request that was genuinely ambiguous.
            #
            # ``uq_health_subjects_owner_user_id`` makes this at most one row,
            # so "the subject they own" is never ambiguous either.
            subject_rows = list(
                await session.execute(
                    select(HealthSubject.id, HealthSubject.owner_user_id)
                    .join(User, User.id == HealthSubject.owner_user_id)
                    .where(User.normalized_username == actor_lookup_key)
                    .limit(2)
                )
            )
            if not subject_rows:
                raise NoPersonalRecordError(
                    "this account keeps no health record of its own"
                )
            if len(subject_rows) != 1:
                raise LegacySubjectResolutionError(
                    "legacy ownership requires the actor to own exactly one "
                    f"health subject; found {len(subject_rows)}"
                )
        elif subject_id is not None:
            # A system boundary that knows whose record it is acting on. The
            # subject is looked up rather than trusted: an id that names nothing
            # is a caller bug, and continuing from it would bind the session to
            # a subject that does not exist.
            subject_rows = list(
                await session.execute(
                    select(HealthSubject.id, HealthSubject.owner_user_id)
                    .where(HealthSubject.id == subject_id)
                    .limit(2)
                )
            )
            if not subject_rows:
                raise LegacySubjectResolutionError(
                    f"health subject {subject_id} does not exist"
                )
        else:
            # Neither an actor nor a named subject — startup bootstrap, and
            # anything still to be ported. Here the sole-subject requirement is
            # the only honest answer: nothing names whose record was meant, and
            # picking one would be inventing it.
            subject_rows = list(
                await session.execute(
                    select(HealthSubject.id, HealthSubject.owner_user_id)
                    .order_by(HealthSubject.id)
                    .limit(2)
                )
            )
            if len(subject_rows) != 1:
                count_label = "2 or more" if len(subject_rows) == 2 else str(
                    len(subject_rows)
                )
                raise LegacySubjectResolutionError(
                    "legacy ownership requires exactly one health subject; "
                    f"found {count_label}"
                )
        subject_id, owner_user_id = subject_rows[0]

    # Bind before the first read of a policy-protected table. ``health_subjects``
    # and ``users`` are the roots the boundary is defined *from*, so they carry
    # no policy; ``integration_connections`` below does, and the lookup would
    # come back empty against an unbound session.
    await bind_session_subject(session, subject_id)

    with session.no_autoflush:
        owner_row = (
            await session.execute(
                select(User.id, User.normalized_username, User.status).where(
                    User.id == owner_user_id
                )
            )
        ).one_or_none()
        if owner_row is None:
            raise LegacyOwnerResolutionError(
                "the sole health subject's owner identity does not exist"
            )
        resolved_owner_id, owner_lookup_key, owner_status = owner_row
        if owner_status != UserStatus.ACTIVE.value:
            raise LegacyOwnerResolutionError(
                "the sole health subject's owner identity is not active"
            )

        actor_user_id: uuid.UUID | None = None
        if actor_lookup_key is not None:
            if actor_lookup_key != owner_lookup_key:
                raise LegacyActorMismatchError(
                    "actor username does not match the sole health subject's owner"
                )
            actor_user_id = resolved_owner_id

        known_statuses = {status.value for status in IntegrationConnectionStatus}
        connection_ids: dict[IntegrationProvider, uuid.UUID] = {}
        for provider in providers:
            connection_type = LEGACY_CONNECTION_TYPES[provider]
            rows = list(
                await session.execute(
                    select(IntegrationConnection.id, IntegrationConnection.status)
                    .where(
                        IntegrationConnection.subject_id == subject_id,
                        IntegrationConnection.provider == provider.value,
                        IntegrationConnection.connection_type
                        == connection_type.value,
                    )
                    .order_by(IntegrationConnection.id)
                )
            )
            if not rows:
                raise LegacyConnectionMissingError(
                    provider,
                    f"of type {connection_type.value!r} does not exist",
                )

            unknown_statuses = sorted(
                {status for _connection_id, status in rows} - known_statuses
            )
            if unknown_statuses:
                raise LegacyConnectionStateError(
                    provider,
                    f"has unknown status {unknown_statuses[0]!r}",
                )

            non_retired = [
                row
                for row in rows
                if row.status != IntegrationConnectionStatus.RETIRED.value
            ]
            if not non_retired:
                raise LegacyConnectionRetiredError(
                    provider,
                    f"of type {connection_type.value!r} is retired",
                )
            if len(non_retired) != 1:
                raise LegacyConnectionAmbiguousError(
                    provider,
                    f"of type {connection_type.value!r} has "
                    f"{len(non_retired)} non-retired matches",
                )
            connection_ids[provider] = non_retired[0].id

    access = None
    if actor_user_id is not None:
        # A named actor is a principal, so the operation can be *decided* rather
        # than merely resolved. The sole owner is authorized by self-ownership,
        # which is why nothing changes for this installation; what the snapshot
        # adds is somewhere for a denial to come from once it might.
        from vitals.services.authorization.subject_access import (
            enter_subject_scope,
            resolve_access_context,
        )

        access = await resolve_access_context(
            session, user_id=actor_user_id, subject_id=subject_id
        )
        # This resolver only ever returns the subject its actor owns, so the
        # scope is entered here; a caller reaching for somebody else's record
        # goes through ``require_access`` and enters it only if allowed.
        await enter_subject_scope(session, access)

    return LegacyOwnershipContext(
        subject_id=subject_id,
        owner_user_id=resolved_owner_id,
        actor_user_id=actor_user_id,
        connection_ids=connection_ids,
        access=access,
    )


async def resolve_legacy_ownership_context(
    session: AsyncSession,
    *,
    actor_username: str | None,
    required_connections: Iterable[IntegrationProvider] = (),
) -> LegacyOwnershipContext:
    """The request path: an account's own record, or the sole subject.

    ``actor_username`` names an account and resolves that account's own record.
    ``None`` is the startup bootstrap, which has no account and gets the sole
    subject or a refusal.

    A scheduled job must not arrive here. It has no account either, and asking
    for "the sole subject" is how the entire background half of the product
    stopped on a two-person installation. :func:`resolve_subject_ownership_context`
    is the one to use, and its subject is mandatory so the choice cannot be made
    by omission.
    """

    return await _resolve_ownership(
        session,
        actor_username=actor_username,
        subject_id=None,
        required_connections=required_connections,
    )


async def resolve_subject_ownership_context(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    required_connections: Iterable[IntegrationProvider] = (),
) -> LegacyOwnershipContext:
    """The system path: a trusted boundary saying whose record it is acting on.

    ``actor_user_id`` stays unset exactly as it did before, so nothing here is
    attributed to a person. What changes is that the caller names the subject
    instead of the installation being required to hold only one.

    The subject is mandatory, and deliberately: an omittable scope is precisely
    the shape ``vitals/legacy_scope.py`` exists to keep out of this codebase, and
    the reason is this function's own history — a job that could run without
    saying whose record it meant read across everybody by accident, and then
    read nothing at all once row security arrived.
    """

    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise LegacyOwnershipValidationError("subject_id must be a non-zero UUID")
    return await _resolve_ownership(
        session,
        actor_username=None,
        subject_id=subject_id,
        required_connections=required_connections,
    )


__all__ = ["resolve_legacy_ownership_context", "resolve_subject_ownership_context"]
