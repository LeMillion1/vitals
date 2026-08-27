"""Period AI digest service (module 10) — the product core.

For each report we assemble a versioned, module-aware **structured cross-domain
snapshot** with one authoritative date window and ask the LLM for an *analytical
narrative* — the interpretation of how the domains relate, not a restatement of
the numbers. The structured context is stored alongside the text so it can be
re-inspected or re-run later.

Production generation reserves one subject-owned platform AI invocation, closes
the database transaction, performs exactly one provider call, then atomically
finalizes accounting and the digest artifact.  The legacy injected-client seam
is quarantined to databases with no commercial identity roots.
"""
from __future__ import annotations

from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts
from vitals.services.ai_gateway import dispatch as ai_gateway_service_dispatch
from vitals.services.ai_gateway import invocations as ai_gateway_service_invocations

from vitals.services.milestones import queries as milestone_queries

import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import date as date_type
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    DigestKind,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject, User
from vitals.models.milestones import DOMAIN, WeeklyDigest
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.utils.timeutils import today_local

from vitals.services.digest.projection.assembly import assemble_context
from vitals.services.digest.prompt import DIGEST_SYSTEM, DIGEST_SYSTEM_EN, build_prompt

# Output budget for one narrative. Was 6000 and prod hit it: a reasoning model
# (claude-opus-5) spends part of the same budget on thinking tokens, so the
# visible digest got cut mid-sentence. Russian is ~2 chars/token, and a full
# cross-domain digest runs 8-10k chars, so leave headroom for both.
_DIGEST_MAX_TOKENS = 16000
_DIGEST_POLICY_VERSION = "wd:v1"
_DIGEST_RESERVATION_OVERHEAD_UNITS = 512
_DIGEST_RESERVED_COST_MICROUNITS = 10_000_000
_DIGEST_MAX_ATTEMPTS = 3
_DIGEST_OWNERSHIP_CHECKPOINT_PHASE = (
    "stage3.retained_artifact.weekly_digests.v1.weekly_digests"
)

_BODY_MEASUREMENT_LIMIT = 6
_BODY_SCAN_LIMIT = 3
_GARMIN_ACTIVITY_LIMIT = 500
_HEVY_SESSION_LIMIT = 300
_TREATMENT_EVENT_LIMIT = 500
_SKINCARE_EVENT_LIMIT = 500
_LAB_HISTORY_PER_MARKER = 3
_GENETICS_LIMIT = 200
_TIMELINE_LIMIT = 200

_REPORT_BODY_METRIC_KEYS = frozenset(
    {
        "weight",
        "skeletal_muscle_mass",
        "body_fat_mass",
        "body_fat_pct",
        "lean_body_mass",
        "fat_free_mass",
        "protein",
        "minerals",
        "total_body_water",
        "intracellular_water",
        "extracellular_water",
        "ecw_tbw_ratio",
        "visceral_fat_area",
        "visceral_fat_level",
        "phase_angle",
        "inbody_score",
        "bmr",
        "waist_hip_ratio",
        "segmental_lean",
        "segmental_fat",
    }
)

_DOMAIN_MODULE = {
    "weight": "weight",
    "body_comp": "body_comp",
    "glp1": "glp1",
    "supplements": "supplements",
    "genetics": "genetics",
    "skincare": "skincare",
    "workouts": "hevy",
    "garmin": "garmin",
    "labs": "labs",
    "nutrition": "nutrition",
    "hrt": "hrt",
    "timeline": "timeline",
    "milestones": "reports",
    "system": "reports",
}

CONTEXT_SCHEMA_VERSION = 2
REPORT_MODE_CLOSED = "closed_period"
REPORT_MODE_BRIEF = "daily_brief"
MIN_PERIOD_DAYS = 1
MAX_PERIOD_DAYS = 90

_HISTORICAL_GATEWAY_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }
)
_DIGEST_SOURCES = frozenset(
    {Source.MANUAL.value, Source.MCP.value, Source.SCHEDULER.value}
)
_DIGEST_KINDS = frozenset(kind.value for kind in DigestKind)
_ARTIFACT_SOURCE_BY_INVOCATION_SOURCE = {
    AIInvocationSource.WEB: Source.MANUAL.value,
    AIInvocationSource.MCP: Source.MCP.value,
    AIInvocationSource.SCHEDULER: Source.SCHEDULER.value,
}
_INVOCATION_SOURCE_BY_ARTIFACT_SOURCE = {
    artifact_source: invocation_source.value
    for invocation_source, artifact_source in (
        _ARTIFACT_SOURCE_BY_INVOCATION_SOURCE.items()
    )
}
_INVOCATION_PURPOSE_BY_DIGEST_KIND = {
    DigestKind.WEEKLY.value: AIInvocationPurpose.WEEKLY_DIGEST.value,
    DigestKind.DAILY_BRIEF.value: AIInvocationPurpose.DAILY_BRIEF.value,
}
class DigestOwnershipError(ValueError):
    """A digest operation has invalid subject, actor, or provider roots."""


class DigestPreparedOwnerError(DigestOwnershipError):
    """A digest read lacks a live service-issued exact-one owner proof."""


class DigestInvocationStateError(DigestOwnershipError):
    """A paid digest attempt is not eligible for another provider dispatch."""


@dataclass(frozen=True, slots=True)
class _DigestAttemptState:
    """Projected invocation state; never carries provider or health payloads."""

    attempt: int
    invocation_id: uuid.UUID
    status: AIInvocationStatus


class PreparedDigestOwner:
    """Opaque exact-one owner proof bound to one session transaction."""

    __slots__ = (
        "_actor_user_id",
        "_fingerprint",
        "_nested_transaction",
        "_owner_user_id",
        "_seal",
        "_session",
        "_subject_id",
        "_transaction",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise DigestPreparedOwnerError(
            "prepared digest owners are issued only by prepare_digest_owner"
        )

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        subject_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
    ) -> "PreparedDigestOwner":
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "_subject_id", subject_id)
        object.__setattr__(prepared, "_owner_user_id", owner_user_id)
        object.__setattr__(prepared, "_actor_user_id", actor_user_id)
        object.__setattr__(
            prepared,
            "_fingerprint",
            (subject_id, owner_user_id, actor_user_id),
        )
        object.__setattr__(prepared, "_session", session)
        object.__setattr__(
            prepared, "_transaction", session.sync_session.get_transaction()
        )
        object.__setattr__(
            prepared,
            "_nested_transaction",
            session.sync_session.get_nested_transaction(),
        )
        object.__setattr__(prepared, "_seal", _PREPARED_DIGEST_OWNER_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedDigestOwner is immutable")

    @property
    def identity(self) -> WriteIdentity:
        return WriteIdentity(
            subject_id=self._subject_id,
            actor_user_id=self._actor_user_id,
        )

    @property
    def owner_user_id(self) -> uuid.UUID:
        """Return the non-PHI owner authority frozen by this proof."""

        return self._owner_user_id


_PREPARED_DIGEST_OWNER_SEAL = object()


class PreparedDigest:
    """Opaque PHI-bearing snapshot bound to one exact AI reservation."""

    __slots__ = (
        "_actor_user_id",
        "_artifact_source",
        "_context_json_text",
        "_dispatchable",
        "_existing_artifact_id",
        "_fingerprint",
        "_invocation_id",
        "_invocation_source",
        "_lang",
        "_model",
        "_on_date",
        "_owner_user_id",
        "_period_days",
        "_prompt",
        "_attempt",
        "_reservation_status",
        "_seal",
        "_subject_id",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise DigestPreparedOwnerError(
            "prepared digests are issued only by prepare_digest"
        )

    @classmethod
    def _issue(
        cls,
        *,
        on_date: date_type,
        period_days: int,
        artifact_source: str,
        invocation_source: AIInvocationSource,
        lang: str,
        subject_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        model: str,
        attempt: int,
        invocation_id: uuid.UUID,
        reservation_status: AIInvocationStatus,
        dispatchable: bool,
        existing_artifact_id: int | None,
        context_json_text: str,
        prompt: str,
    ) -> "PreparedDigest":
        prepared = object.__new__(cls)
        values = {
            "_on_date": on_date,
            "_period_days": period_days,
            "_artifact_source": artifact_source,
            "_invocation_source": invocation_source,
            "_lang": lang,
            "_subject_id": subject_id,
            "_owner_user_id": owner_user_id,
            "_actor_user_id": actor_user_id,
            "_model": model,
            "_attempt": attempt,
            "_invocation_id": invocation_id,
            "_reservation_status": reservation_status,
            "_dispatchable": dispatchable,
            "_existing_artifact_id": existing_artifact_id,
            "_context_json_text": context_json_text,
            "_prompt": prompt,
        }
        for name, value in values.items():
            object.__setattr__(prepared, name, value)
        object.__setattr__(
            prepared,
            "_fingerprint",
            cls._fingerprint_for(**values),
        )
        object.__setattr__(prepared, "_seal", _PREPARED_DIGEST_SEAL)
        return prepared

    @staticmethod
    def _fingerprint_for(**values) -> tuple:
        return (
            values["_on_date"],
            values["_period_days"],
            values["_artifact_source"],
            values["_invocation_source"],
            values["_lang"],
            values["_subject_id"],
            values["_owner_user_id"],
            values["_actor_user_id"],
            values["_model"],
            values["_attempt"],
            values["_invocation_id"],
            values["_reservation_status"],
            values["_dispatchable"],
            values["_existing_artifact_id"],
            hashlib.sha256(values["_context_json_text"].encode("utf-8")).digest(),
            hashlib.sha256(values["_prompt"].encode("utf-8")).digest(),
        )

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedDigest is immutable")

    def __repr__(self) -> str:
        return (
            f"<PreparedDigest invocation_id={self._invocation_id} "
            f"status={self._reservation_status.value} redacted>"
        )

    def __reduce__(self):
        raise TypeError("PreparedDigest is not pickleable")

    @property
    def invocation_id(self) -> uuid.UUID:
        return self._invocation_id

    @property
    def reservation_status(self) -> AIInvocationStatus:
        return self._reservation_status

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def dispatchable(self) -> bool:
        return self._dispatchable

    @property
    def existing_artifact_id(self) -> int | None:
        return self._existing_artifact_id


_PREPARED_DIGEST_SEAL = object()


def _require_prepared_digest_owner(
    session: AsyncSession,
    prepared_owner: PreparedDigestOwner,
) -> PreparedDigestOwner:
    if not isinstance(prepared_owner, PreparedDigestOwner):
        raise DigestPreparedOwnerError("digest owner is not a valid capability")
    try:
        valid_fingerprint = prepared_owner._fingerprint == (
            prepared_owner._subject_id,
            prepared_owner._owner_user_id,
            prepared_owner._actor_user_id,
        )
        valid_seal = prepared_owner._seal is _PREPARED_DIGEST_OWNER_SEAL
        prepared_session = prepared_owner._session
        transaction = prepared_owner._transaction
        nested_transaction = prepared_owner._nested_transaction
    except (AttributeError, TypeError) as exc:
        raise DigestPreparedOwnerError(
            "digest owner is not a valid issued capability"
        ) from exc
    if not valid_seal or not valid_fingerprint:
        raise DigestPreparedOwnerError("digest owner capability was modified")
    if prepared_session is not session:
        raise DigestPreparedOwnerError("digest owner belongs to another session")
    if session.sync_session.get_transaction() is not transaction:
        raise DigestPreparedOwnerError("digest owner transaction is no longer active")
    if session.sync_session.get_nested_transaction() is not nested_transaction:
        raise DigestPreparedOwnerError("digest owner savepoint is no longer active")
    return prepared_owner


def _require_prepared_digest(prepared: PreparedDigest) -> PreparedDigest:
    if not isinstance(prepared, PreparedDigest):
        raise DigestPreparedOwnerError("digest snapshot is not a valid capability")
    try:
        values = {
            "_on_date": prepared._on_date,
            "_period_days": prepared._period_days,
            "_artifact_source": prepared._artifact_source,
            "_invocation_source": prepared._invocation_source,
            "_lang": prepared._lang,
            "_subject_id": prepared._subject_id,
            "_owner_user_id": prepared._owner_user_id,
            "_actor_user_id": prepared._actor_user_id,
            "_model": prepared._model,
            "_attempt": prepared._attempt,
            "_invocation_id": prepared._invocation_id,
            "_reservation_status": prepared._reservation_status,
            "_dispatchable": prepared._dispatchable,
            "_existing_artifact_id": prepared._existing_artifact_id,
            "_context_json_text": prepared._context_json_text,
            "_prompt": prepared._prompt,
        }
        valid = (
            prepared._seal is _PREPARED_DIGEST_SEAL
            and prepared._fingerprint == PreparedDigest._fingerprint_for(**values)
        )
    except (AttributeError, KeyError, TypeError, UnicodeError) as exc:
        raise DigestPreparedOwnerError(
            "digest snapshot is not a valid issued capability"
        ) from exc
    if not valid:
        raise DigestPreparedOwnerError("digest snapshot capability was modified")
    return prepared


def _as_invocation_source(value: AIInvocationSource | str) -> AIInvocationSource:
    try:
        source = AIInvocationSource(value)
    except (TypeError, ValueError) as exc:
        raise DigestOwnershipError("unsupported digest invocation source") from exc
    if source not in _ARTIFACT_SOURCE_BY_INVOCATION_SOURCE:
        raise DigestOwnershipError("unsupported digest invocation source")
    return source


def _validate_source_actor(
    *,
    source: str,
    actor_user_id: uuid.UUID | None,
    owner_user_id: uuid.UUID,
    historical: bool,
) -> None:
    if source not in _DIGEST_SOURCES:
        raise DigestOwnershipError(f"unsupported digest source {source!r}")
    if source in {Source.MANUAL.value, Source.MCP.value}:
        if actor_user_id != owner_user_id and not (
            historical and actor_user_id is None
        ):
            raise DigestOwnershipError(
                "human digest source requires the current owner actor"
            )
    elif actor_user_id is not None:
        raise DigestOwnershipError("scheduled digest must not have a human actor")


def _digest_idempotency_key(
    *,
    invocation_source: AIInvocationSource,
    on_date: date_type,
    period_days: int,
    lang: str,
    model: str,
    attempt: int,
) -> str:
    key_material = "|".join(
        (
            _DIGEST_POLICY_VERSION,
            invocation_source.value,
            on_date.isoformat(),
            str(period_days),
            lang,
            model,
            str(attempt),
        )
    )
    return (
        f"{_DIGEST_POLICY_VERSION}:"
        f"{hashlib.sha256(key_material.encode('utf-8')).hexdigest()}"
    )


async def _load_digest_attempts(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    invocation_source: AIInvocationSource,
    model: str,
    idempotency_keys: Sequence[str],
) -> dict[int, _DigestAttemptState]:
    """Read product-attempt state before comparing mutable gateway fingerprints.

    Gateway roots, quota periods, and conservative reservation size may change
    after a paid attempt.  Those operational values must not hide a succeeded or
    dispatching invocation for the same immutable digest product key.
    """

    attempt_by_key = {key: attempt for attempt, key in enumerate(idempotency_keys)}
    with session.no_autoflush:
        rows = list(
            await session.execute(
                select(
                    AIInvocation.id,
                    AIInvocation.actor_user_id,
                    AIInvocation.source,
                    AIInvocation.model,
                    AIInvocation.idempotency_key,
                    AIInvocation.status,
                ).where(
                    AIInvocation.subject_id == identity.subject_id,
                    AIInvocation.purpose
                    == AIInvocationPurpose.WEEKLY_DIGEST.value,
                    AIInvocation.idempotency_key.in_(tuple(idempotency_keys)),
                )
            )
        )
    attempts: dict[int, _DigestAttemptState] = {}
    for row in rows:
        attempt = attempt_by_key.get(row.idempotency_key)
        if (
            attempt is None
            or row.actor_user_id != identity.actor_user_id
            or row.source != invocation_source.value
            or row.model != model
            or attempt in attempts
        ):
            raise DigestInvocationStateError(
                "digest invocation retry provenance is inconsistent"
            )
        try:
            status = AIInvocationStatus(row.status)
        except (TypeError, ValueError) as exc:
            raise DigestInvocationStateError(
                "digest invocation has an invalid lifecycle state"
            ) from exc
        attempts[attempt] = _DigestAttemptState(
            attempt=attempt,
            invocation_id=row.id,
            status=status,
        )
    live = [
        state
        for state in attempts.values()
        if state.status
        in {
            AIInvocationStatus.PREPARED,
            AIInvocationStatus.DISPATCHING,
            AIInvocationStatus.SUCCEEDED,
        }
    ]
    if len(live) > 1:
        raise DigestInvocationStateError(
            "digest invocation retry history has multiple live attempts"
        )
    return attempts


async def _validate_digest_rows(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None,
    owner_user_id: uuid.UUID | None,
) -> None:
    """Validate every persisted digest root without materializing narrative PHI."""
    historical_high_watermark = int(
        await session.scalar(
            select(OwnershipBackfillCheckpoint.scan_high_watermark_id).where(
                OwnershipBackfillCheckpoint.phase_key
                == _DIGEST_OWNERSHIP_CHECKPOINT_PHASE,
                OwnershipBackfillCheckpoint.subject_id == subject_id,
                OwnershipBackfillCheckpoint.status == "completed",
            )
        )
        or 0
    )
    roots = list(
        await session.execute(
            select(
                WeeklyDigest.id,
                WeeklyDigest.subject_id,
                WeeklyDigest.actor_user_id,
                WeeklyDigest.integration_connection_id,
                WeeklyDigest.ai_invocation_id,
                WeeklyDigest.domain,
                WeeklyDigest.source,
                WeeklyDigest.kind,
                WeeklyDigest.model,
            ).order_by(WeeklyDigest.id)
        )
    )
    connection_ids = {
        root.integration_connection_id
        for root in roots
        if root.integration_connection_id is not None
    }
    connections = (
        {
            row.id: row
            for row in await session.scalars(
                select(IntegrationConnection)
                .where(IntegrationConnection.id.in_(tuple(connection_ids)))
                .execution_options(populate_existing=True)
            )
        }
        if connection_ids
        else {}
    )
    invocation_ids = {
        root.ai_invocation_id for root in roots if root.ai_invocation_id is not None
    }
    invocations = (
        {
            row.id: row
            for row in await session.scalars(
                select(AIInvocation)
                .where(AIInvocation.id.in_(tuple(invocation_ids)))
                .execution_options(populate_existing=True)
            )
        }
        if invocation_ids
        else {}
    )

    for root in roots:
        historical = root.id <= historical_high_watermark
        if root.domain != DOMAIN:
            raise DigestOwnershipError(
                f"digest {root.id} has unexpected domain {root.domain!r}"
            )
        if root.kind not in _DIGEST_KINDS:
            raise DigestOwnershipError(
                f"digest {root.id} has unknown kind {root.kind!r}"
            )
        if root.source not in _DIGEST_SOURCES:
            raise DigestOwnershipError(
                f"digest {root.id} has unknown source {root.source!r}"
            )
        if root.subject_id is None:
            if (
                root.actor_user_id is not None
                or root.integration_connection_id is not None
                or root.ai_invocation_id is not None
            ):
                raise DigestOwnershipError(
                    f"digest {root.id} has partial legacy ownership roots"
                )
            continue
        if subject_id is None or owner_user_id is None:
            raise DigestOwnershipError(
                f"digest {root.id} is owned but no subject scope was prepared"
            )
        if root.subject_id != subject_id:
            raise DigestOwnershipError(
                f"digest {root.id} belongs to another subject"
            )
        _validate_source_actor(
            source=root.source,
            actor_user_id=root.actor_user_id,
            owner_user_id=owner_user_id,
            historical=historical,
        )
        if root.ai_invocation_id is not None:
            if root.integration_connection_id is not None:
                raise DigestOwnershipError(
                    f"digest {root.id} mixes platform and subject provider roots"
                )
            invocation = invocations.get(root.ai_invocation_id)
            if invocation is None:
                raise DigestOwnershipError(
                    f"digest {root.id} AI invocation is missing"
                )
            expected_purpose = _INVOCATION_PURPOSE_BY_DIGEST_KIND.get(root.kind)
            expected_source = _INVOCATION_SOURCE_BY_ARTIFACT_SOURCE.get(root.source)
            if (
                invocation.subject_id != root.subject_id
                or invocation.actor_user_id != root.actor_user_id
                or expected_purpose is None
                or invocation.purpose != expected_purpose
                or expected_source is None
                or invocation.source != expected_source
            ):
                raise DigestOwnershipError(
                    f"digest {root.id} has invalid AI invocation provenance"
                )
            if root.kind == DigestKind.WEEKLY.value:
                valid_lifecycle = (
                    invocation.status == AIInvocationStatus.SUCCEEDED.value
                    and root.model == invocation.model
                )
            else:
                valid_lifecycle = (
                    invocation.status == AIInvocationStatus.SUCCEEDED.value
                    and root.model == invocation.model
                ) or (
                    invocation.status
                    in {
                        AIInvocationStatus.FAILED.value,
                        AIInvocationStatus.AMBIGUOUS.value,
                        AIInvocationStatus.CANCELLED.value,
                    }
                    and root.model is None
                )
            if not valid_lifecycle:
                raise DigestOwnershipError(
                    f"digest {root.id} has invalid AI invocation lifecycle"
                )
            continue
        if root.kind == DigestKind.WEEKLY.value:
            if root.integration_connection_id is None and not historical:
                raise DigestOwnershipError(
                    f"weekly digest {root.id} lacks OpenRouter provenance"
                )
        elif (
            root.integration_connection_id is None
            and root.model is not None
            and not historical
        ):
            raise DigestOwnershipError(
                f"digest {root.id} has a model without provider provenance"
            )
        if root.integration_connection_id is None:
            continue
        connection = connections.get(root.integration_connection_id)
        if connection is None:
            raise DigestOwnershipError(
                f"digest {root.id} integration connection is missing"
            )
        if connection.subject_id != subject_id:
            raise DigestOwnershipError(
                f"digest {root.id} integration belongs to another subject"
            )
        if (
            connection.provider != IntegrationProvider.OPENROUTER.value
            or connection.connection_type
            != IntegrationConnectionType.AI_GATEWAY.value
        ):
            raise DigestOwnershipError(
                f"digest {root.id} requires an OpenRouter AI gateway"
            )
        if connection.status not in _HISTORICAL_GATEWAY_STATUSES:
            raise DigestOwnershipError(
                f"digest {root.id} has invalid provider lifecycle state"
            )


async def legacy_unowned_digest_present(session: AsyncSession) -> bool:
    """Whether any weekly digest is still waiting for the ownership backfill.

    What the digest compatibility bridge is for, and a different question from
    how many people the installation holds. A digest row with no subject is
    tolerated by the root validation below — see the ``root.subject_id is None``
    arm — and that toleration is the whole widening. With no such row it widens
    nothing, and there is nobody's digest to decide the owner of.

    ``scripts/backfill_weekly_digest_subject_ownership.py`` empties this set,
    run while the installation is still one person, which is exactly when
    adopting an unowned digest into that person is right. Revision 0049 made
    ``weekly_digests.subject_id`` NOT NULL, so on a current schema the answer is
    already no and this costs one index probe.
    """

    with session.no_autoflush:
        found = await session.scalar(
            select(WeeklyDigest.id)
            .where(WeeklyDigest.subject_id.is_(None))
            .limit(1)
        )
    return found is not None


async def prepare_subject_digest_owner(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> PreparedDigestOwner:
    """The weekly digest's roots, for a system boundary that names its subject.

    The digest job used to ask for "the sole subject", so on a two-person
    installation nobody got a weekly digest at all — silently, because a report
    that never arrives looks like a quiet week. The subject is mandatory here for
    the reason given in ``resolve_subject_ownership_context``.
    """

    from vitals.services.tenancy.ownership import resolve_subject_ownership_context

    # Governance first, as on the other path: the lock has to precede the
    # owner-lifecycle proof, not follow it, or a rotation committing in between
    # would be proved against roots that are already gone. Taking it again inside
    # ``prepare_digest_owner`` is a no-op for the transaction that holds it.
    await acquire_identity_governance_lock(session)
    ownership = await resolve_subject_ownership_context(
        session,
        subject_id=subject_id,
    )
    return await prepare_digest_owner(
        session,
        actor_username=None,
        subject_ownership=ownership,
    )


async def prepare_digest_owner(
    session: AsyncSession,
    *,
    actor_username: str | None,
    subject_ownership: Any | None = None,
) -> PreparedDigestOwner:
    """Prepare one subject's read/generation roots in canonical lock order.

    ``subject_ownership`` is an already-resolved ``LegacyOwnershipContext`` from
    :func:`prepare_subject_digest_owner` — a system boundary that named its
    subject. Typed loosely because importing it here would close an import
    cycle: legacy_ownership is resolved lazily inside these functions for the
    same reason. It is threaded rather than re-resolved because the ordered locks
    below have to be taken once, in this order, by whichever path arrived.

    Note it is not an omittable scope: a caller that does not pass one still has
    to pass an ``actor_username``, so the record is named either way. That is the
    distinction ``vitals/legacy_scope.py`` is about — not the number of
    parameters, but whether any of them can be left out and still act.
    """
    from vitals.services.tenancy.ownership import resolve_legacy_ownership_context

    await acquire_identity_governance_lock(session)
    ownership = subject_ownership or await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
    )
    with session.no_autoflush:
        if await legacy_unowned_digest_present(session):
            subject_ids = list(
                await session.scalars(
                    select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
                )
            )
            if subject_ids != [ownership.subject_id]:
                raise DigestOwnershipError(
                    "digest compatibility requires exactly one health subject"
                )
        subject = await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == ownership.subject_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subject is None or subject.owner_user_id != ownership.owner_user_id:
            raise DigestOwnershipError("digest subject owner changed")
        owner = await session.scalar(
            select(User)
            .where(User.id == ownership.owner_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None or owner.status != UserStatus.ACTIVE.value:
            raise DigestOwnershipError("digest owner is missing or inactive")
        if (
            ownership.actor_user_id is not None
            and ownership.actor_user_id != owner.id
        ):
            raise DigestOwnershipError("digest actor is not the subject owner")
    await _validate_digest_rows(
        session,
        subject_id=subject.id,
        owner_user_id=owner.id,
    )
    return PreparedDigestOwner._issue(
        session=session,
        subject_id=subject.id,
        owner_user_id=owner.id,
        actor_user_id=ownership.actor_user_id,
    )


async def prepare_digest_owner_for_identity(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    owner_user_id: uuid.UUID,
) -> PreparedDigestOwner:
    """Prepare a full fail-closed digest read proof for a core-owned identity.

    Delivery/inbound services already hold an exact subject/recipient binding and
    must not reach into web configuration to turn that binding back into a
    username. This path performs the same governance, S, owner, and complete
    digest-root validation as :func:`prepare_digest_owner`.
    """

    if not isinstance(identity, WriteIdentity) or not isinstance(
        owner_user_id, uuid.UUID
    ):
        raise DigestOwnershipError("digest core owner identity is invalid")
    await acquire_identity_governance_lock(session)
    with session.no_autoflush:
        if await legacy_unowned_digest_present(session):
            subject_ids = list(
                await session.scalars(
                    select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
                )
            )
            if subject_ids != [identity.subject_id]:
                raise DigestOwnershipError(
                    "digest compatibility requires exactly one health subject"
                )
        subject = await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == identity.subject_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        owner = await session.scalar(
            select(User)
            .where(User.id == owner_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            subject is None
            or subject.owner_user_id != owner_user_id
            or owner is None
            or owner.status != UserStatus.ACTIVE.value
            or (
                identity.actor_user_id is not None
                and identity.actor_user_id != owner_user_id
            )
        ):
            raise DigestOwnershipError("digest core owner is missing or inactive")
    await _validate_digest_rows(
        session,
        subject_id=identity.subject_id,
        owner_user_id=owner_user_id,
    )
    return PreparedDigestOwner._issue(
        session=session,
        subject_id=identity.subject_id,
        owner_user_id=owner_user_id,
        actor_user_id=identity.actor_user_id,
    )


async def _owner_or_zero_subject_legacy(
    session: AsyncSession,
    prepared_owner: PreparedDigestOwner | None,
) -> PreparedDigestOwner | None:
    if prepared_owner is not None:
        return _require_prepared_digest_owner(session, prepared_owner)
    await acquire_identity_governance_lock(session)
    if await session.scalar(select(HealthSubject.id).limit(1)) is not None:
        raise DigestPreparedOwnerError(
            "digest reads require a prepared owner once identity exists"
        )
    await _validate_digest_rows(session, subject_id=None, owner_user_id=None)
    return None


async def prepare_digest(
    session: AsyncSession,
    *,
    actor_username: str | None,
    invocation_source: AIInvocationSource | str,
    prepared_owner: PreparedDigestOwner | None = None,
    on_date: Optional[date_type] = None,
    period_days: int = 7,
) -> PreparedDigest:
    """Freeze one subject's PHI and reserve one paid call without external I/O.

    ``prepared_owner`` is the proof a caller has already taken — the scheduled
    job prepares it to read the language before it gets here. Passing it through
    is not only an economy: preparing twice would take the governance lock and
    the ordered subject/owner row locks a second time, in the middle of a
    transaction that is already holding them.
    """
    invocation_source_value = _as_invocation_source(invocation_source)
    artifact_source = _ARTIFACT_SOURCE_BY_INVOCATION_SOURCE[
        invocation_source_value
    ]
    if (
        invocation_source_value is not AIInvocationSource.SCHEDULER
        and actor_username is None
    ):
        raise DigestOwnershipError("human digest source requires an actor")
    if (
        invocation_source_value is AIInvocationSource.SCHEDULER
        and actor_username is not None
    ):
        raise DigestOwnershipError("scheduled digest must not have a human actor")
    owner = prepared_owner or await prepare_digest_owner(
        session,
        actor_username=actor_username,
    )
    _validate_source_actor(
        source=artifact_source,
        actor_user_id=owner._actor_user_id,
        owner_user_id=owner._owner_user_id,
        historical=False,
    )


    await milestone_queries.list_milestones(
        session,
        subject_id=owner._subject_id,
    )
    frozen_date = on_date or today_local()
    from vitals.i18n import current_lang

    lang = current_lang.get()
    model = load_config().llm_model_digest.strip()
    if not model:
        raise DigestOwnershipError("digest model is not configured")
    context = await assemble_context(
        session,
        subject_id=owner._subject_id,
        on_date=frozen_date,
        period_days=period_days,
    )
    frozen_context = deepcopy(context)
    context_json_text = json.dumps(
        frozen_context,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prompt = build_prompt(frozen_context, lang=lang)
    system = DIGEST_SYSTEM_EN if lang == "en" else DIGEST_SYSTEM
    reserved_units = (
        len((system + "\n" + prompt).encode("utf-8"))
        + _DIGEST_MAX_TOKENS
        + _DIGEST_RESERVATION_OVERHEAD_UNITS
    )
    idempotency_keys = tuple(
        _digest_idempotency_key(
            invocation_source=invocation_source_value,
            on_date=frozen_date,
            period_days=period_days,
            lang=lang,
            model=model,
            attempt=attempt,
        )
        for attempt in range(_DIGEST_MAX_ATTEMPTS)
    )
    existing_attempts = await _load_digest_attempts(
        session,
        identity=owner.identity,
        invocation_source=invocation_source_value,
        model=model,
        idempotency_keys=idempotency_keys,
    )
    terminal_statuses = {
        AIInvocationStatus.FAILED,
        AIInvocationStatus.AMBIGUOUS,
        AIInvocationStatus.CANCELLED,
    }
    reservation = None
    attempt = 0
    for attempt, idempotency_key in enumerate(idempotency_keys):
        existing = existing_attempts.get(attempt)
        if existing is not None and existing.status in {
            AIInvocationStatus.SUCCEEDED,
            AIInvocationStatus.DISPATCHING,
        }:
            # Product identity is independent of the mutable gateway root,
            # billing period, and context-derived reservation ceiling.  Reuse a
            # paid/live attempt without asking the current gateway to compare a
            # now-obsolete operational fingerprint.
            reservation = ai_gateway_service_contracts.AIReservationResult(
                invocation_id=existing.invocation_id,
                status=existing.status,
                created=False,
                dispatchable=False,
            )
            break
        if existing is not None and existing.status in terminal_statuses:
            reservation = ai_gateway_service_contracts.AIReservationResult(
                invocation_id=existing.invocation_id,
                status=existing.status,
                created=False,
                dispatchable=False,
            )
            if attempt + 1 < _DIGEST_MAX_ATTEMPTS:
                continue
            break
        try:
            candidate = await ai_gateway_service_invocations.reserve_ai_invocation(
                session,
                identity=owner.identity,
                purpose=AIInvocationPurpose.WEEKLY_DIGEST,
                source=invocation_source_value,
                model=model,
                idempotency_key=idempotency_key,
                reserved_cost_microunits=_DIGEST_RESERVED_COST_MICROUNITS,
                reserved_units=reserved_units,
            )
        except ai_gateway_service_contracts.AIIdempotencyConflictError as exc:
            if (
                existing is None
                or existing.status is not AIInvocationStatus.PREPARED
            ):
                # prepare_digest_owner holds the subject root, so an unseen or
                # non-prepared conflict cannot be a legitimate concurrent
                # transition.  Never buy a second call around corrupt history.
                raise DigestInvocationStateError(
                    "digest invocation retry history changed unexpectedly"
                ) from exc
            cancelled = await ai_gateway_service_dispatch.cancel_reserved_ai_invocation(
                session,
                identity=owner.identity,
                invocation_id=existing.invocation_id,
            )
            if cancelled.status != AIInvocationStatus.CANCELLED.value:
                raise DigestInvocationStateError(
                    "stale digest reservation was not released"
                )
            reservation = ai_gateway_service_contracts.AIReservationResult(
                invocation_id=existing.invocation_id,
                status=AIInvocationStatus.CANCELLED,
                created=False,
                dispatchable=False,
            )
            if attempt + 1 >= _DIGEST_MAX_ATTEMPTS:
                break
            continue
        if existing is not None and candidate.invocation_id != existing.invocation_id:
            raise DigestInvocationStateError(
                "digest reservation changed identity during preparation"
            )
        reservation = candidate
        if (
            candidate.status in terminal_statuses
            and attempt + 1 < _DIGEST_MAX_ATTEMPTS
        ):
            continue
        break
    if reservation is None:  # pragma: no cover - loop either reserves or raises
        raise DigestInvocationStateError("digest reservation was not created")
    existing_artifact_id = None
    if reservation.status is AIInvocationStatus.SUCCEEDED:
        existing_artifact_id = await session.scalar(
            select(WeeklyDigest.id).where(
                WeeklyDigest.ai_invocation_id == reservation.invocation_id,
                WeeklyDigest.subject_id == owner._subject_id,
            )
        )
        if existing_artifact_id is None:
            raise DigestInvocationStateError(
                "a succeeded digest invocation is missing its artifact"
            )
    return PreparedDigest._issue(
        on_date=frozen_date,
        period_days=period_days,
        artifact_source=artifact_source,
        invocation_source=invocation_source_value,
        lang=lang,
        subject_id=owner._subject_id,
        owner_user_id=owner._owner_user_id,
        actor_user_id=owner._actor_user_id,
        model=model,
        attempt=attempt,
        invocation_id=reservation.invocation_id,
        reservation_status=reservation.status,
        dispatchable=reservation.dispatchable,
        existing_artifact_id=existing_artifact_id,
        context_json_text=context_json_text,
        prompt=prompt,
    )
