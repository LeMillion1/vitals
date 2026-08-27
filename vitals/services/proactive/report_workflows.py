"""Application workflows behind the interactive Reports actions.

This module owns transaction phases, retries, idempotency recovery, paid AI
dispatch, and notification delivery. HTTP adapters receive only typed outcomes.
"""

from __future__ import annotations

from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts

from vitals.services.milestones import governance as milestone_governance

from vitals.services.proactive.delivery import contracts as delivery_contracts
from vitals.services.proactive.delivery import queries as delivery_queries
from vitals.services.proactive.delivery import preparation as delivery_preparation
from vitals.services.proactive.delivery import dispatch as delivery_dispatch

import hashlib
import logging
from datetime import date as date_type
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import AIInvocationSource, AIInvocationStatus
from vitals.services.digest import generation as digest_generation
from vitals.services.digest import ownership as digest_ownership
from vitals.services.legacy_ownership import LegacyOwnershipError
from vitals.services.proactive import channels
from vitals.services.proactive.brief import contracts as brief_contracts
from vitals.services.proactive.brief import persistence as brief_persistence
from vitals.services.proactive.brief import preparation as brief_preparation
from vitals.services.proactive.brief import rendering as brief_rendering

logger = logging.getLogger(__name__)


class DigestWorkflowOutcome(StrEnum):
    OK = "ok"
    PENDING = "pending"
    ERROR = "error"
    PROVIDER_ERROR = "provider_error"
    QUOTA = "quota"
    NOT_CONFIGURED = "not_configured"


class BriefWorkflowOutcome(StrEnum):
    OK = "ok"
    HEADER = "header"
    EMPTY = "empty"
    PENDING = "pending"
    SENT = "sent"
    NO_CHANNEL = "no_channel"
    ERROR = "error"


async def generate_digest(
    session: AsyncSession,
    *,
    actor_username: str,
    period_days: int,
) -> DigestWorkflowOutcome:
    """Generate one web-requested weekly digest through all paid phases."""

    prepared = None
    try:
        prepared = await digest_ownership.prepare_digest(
            session,
            actor_username=actor_username,
            invocation_source=AIInvocationSource.WEB,
            period_days=period_days,
        )
        await session.commit()
        if prepared.existing_artifact_id is not None:
            return DigestWorkflowOutcome.OK
        if not prepared.dispatchable:
            if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
                return DigestWorkflowOutcome.PENDING
            return DigestWorkflowOutcome.ERROR
        lease = await digest_generation.start_digest_dispatch(session, prepared)
        await session.commit()
        completion = await digest_generation.render_digest(prepared, lease)
        row = await digest_generation.persist_digest(session, prepared, completion)
        await session.commit()
        if row is None:
            return DigestWorkflowOutcome.PROVIDER_ERROR
    except ai_gateway_service_contracts.AIQuotaExceededError:
        await session.rollback()
        return DigestWorkflowOutcome.QUOTA
    except ai_gateway_service_contracts.AIGatewayConfigurationError:
        await _release_digest_reservation(session, prepared)
        return DigestWorkflowOutcome.NOT_CONFIGURED
    except (
        ai_gateway_service_contracts.AIGatewayAuthorizationError,
        LegacyOwnershipError,
        digest_ownership.DigestOwnershipError,
        milestone_governance.MilestoneOwnershipError,
    ):
        await _release_digest_reservation(session, prepared)
        raise
    except ai_gateway_service_contracts.AIInvocationStateError:
        await session.rollback()
        return DigestWorkflowOutcome.PENDING
    except Exception:  # noqa: BLE001 - return only a sanitized product outcome
        await _release_digest_reservation(session, prepared)
        logger.warning("Digest generation failed (code=internal_error)")
        return DigestWorkflowOutcome.ERROR
    return DigestWorkflowOutcome.OK


async def build_brief(
    session: AsyncSession,
    *,
    actor_username: str,
    request_token: str,
    on_date: date_type,
) -> BriefWorkflowOutcome:
    """Build an intentionally requested brief without delivering it."""

    try:
        row, outcome = await _run_brief_generation(
            session,
            actor_username=actor_username,
            surface=brief_contracts.BriefSurface.BUILD,
            request_token=request_token,
            on_date=on_date,
        )
    except (
        ai_gateway_service_contracts.AIGatewayAuthorizationError,
        LegacyOwnershipError,
        digest_ownership.DigestOwnershipError,
        brief_contracts.BriefOwnershipError,
    ):
        await session.rollback()
        raise
    except Exception:  # noqa: BLE001 - return only a sanitized product outcome
        await session.rollback()
        logger.warning("Daily Brief build failed (code=internal_error)")
        return BriefWorkflowOutcome.ERROR
    if outcome == "pending":
        return BriefWorkflowOutcome.PENDING
    if row is None:
        return BriefWorkflowOutcome.EMPTY
    return (
        BriefWorkflowOutcome.HEADER
        if row.model is None
        else BriefWorkflowOutcome.OK
    )


async def send_test_brief(
    session: AsyncSession,
    *,
    actor_username: str,
    request_token: str,
    on_date: date_type,
) -> BriefWorkflowOutcome:
    """Build and durably send one idempotent test brief."""

    try:
        request_token = brief_preparation.validate_request_token(request_token)
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=actor_username,
        )
        legacy_test_dedupe_key = (
            f"brief_test:{on_date.isoformat()}:"
            f"{hashlib.sha256(request_token.encode()).hexdigest()}"
        )
        test_delivery_key = delivery_contracts.make_delivery_idempotency_key(
            "brief-test",
            on_date,
            request_token,
        )
        if await delivery_queries.confirmed_delivery_journal(
            session,
            idempotency_key=test_delivery_key,
            category=delivery_contracts.CATEGORY_TEST,
            ownership=ownership,
            legacy_dedupe_key=legacy_test_dedupe_key,
            actor_user_id=ownership.recipient_user_id,
        ) is not None:
            await session.commit()
            return BriefWorkflowOutcome.SENT
        if await delivery_queries.delivery_claim_exists(
            session,
            idempotency_key=test_delivery_key,
            ownership=ownership,
        ):
            await session.commit()
            return BriefWorkflowOutcome.PENDING
        endpoint_available = await channels.build_legacy_bound_notifier(
            session,
            ownership,
        )
        if endpoint_available is None:
            await session.commit()
            return BriefWorkflowOutcome.NO_CHANNEL
        # Availability is only a preflight. T1 below resolves a fresh bound
        # client, and T2 resolves again after its current-policy/C recheck.
        del endpoint_available
        await session.commit()

        row, outcome = await _run_brief_generation(
            session,
            actor_username=actor_username,
            surface=brief_contracts.BriefSurface.TEST,
            request_token=request_token,
            on_date=on_date,
        )
        if outcome == "pending":
            return BriefWorkflowOutcome.PENDING
        if row is None:
            return BriefWorkflowOutcome.EMPTY

        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=actor_username,
        )
        bound_notifier = await channels.build_legacy_bound_notifier(
            session,
            ownership,
        )
        if bound_notifier is None:
            await session.commit()
            return BriefWorkflowOutcome.NO_CHANNEL
        prepared_delivery = await delivery_preparation.prepare_delivery_intent(
            session,
            bound_notifier,
            text=row.content,
            category=delivery_contracts.CATEGORY_TEST,
            idempotency_key=test_delivery_key,
            legacy_dedupe_key=legacy_test_dedupe_key,
            ownership=ownership,
            actor_user_id=ownership.recipient_user_id,
        )
        await session.commit()
        if prepared_delivery is None:
            ownership = await channels.resolve_legacy_channel_ownership(
                session,
                actor_username=actor_username,
            )
            if await delivery_queries.confirmed_delivery_journal(
                session,
                idempotency_key=test_delivery_key,
                category=delivery_contracts.CATEGORY_TEST,
                ownership=ownership,
                legacy_dedupe_key=legacy_test_dedupe_key,
                actor_user_id=ownership.recipient_user_id,
            ) is not None:
                await session.commit()
                return BriefWorkflowOutcome.SENT
            claimed = await delivery_queries.delivery_claim_exists(
                session,
                idempotency_key=test_delivery_key,
                ownership=ownership,
            )
            await session.commit()
            return (
                BriefWorkflowOutcome.PENDING
                if claimed
                else BriefWorkflowOutcome.ERROR
            )
        dispatch_lease = await delivery_dispatch.start_delivery_dispatch(
            session,
            prepared_delivery,
            notifier_resolver=channels.resolve_legacy_bound_notifier,
        )
        await session.commit()
        if dispatch_lease is None:
            return BriefWorkflowOutcome.ERROR
        completion = await delivery_dispatch.dispatch_delivery(dispatch_lease)
        journal = None
        for finalize_try in range(2):
            try:
                journal = await delivery_dispatch.finalize_delivery(session, completion)
                await session.commit()
                break
            except Exception:
                await session.rollback()
                if finalize_try:
                    raise
        if journal is None:
            return BriefWorkflowOutcome.ERROR
    except (
        ai_gateway_service_contracts.AIGatewayAuthorizationError,
        LegacyOwnershipError,
        digest_ownership.DigestOwnershipError,
        brief_contracts.BriefOwnershipError,
    ):
        await session.rollback()
        raise
    except Exception:  # noqa: BLE001 - return only a sanitized product outcome
        await session.rollback()
        logger.warning("Daily Brief test failed (code=internal_error)")
        return BriefWorkflowOutcome.ERROR
    return BriefWorkflowOutcome.SENT


async def _run_brief_generation(
    session: AsyncSession,
    *,
    actor_username: str,
    surface: brief_contracts.BriefSurface,
    request_token: str,
    on_date: date_type,
):
    """Own T1/T2/T3 commits while provider I/O stays transaction-free."""

    prepared = None
    for prepare_try in range(2):
        prepared = await brief_preparation.prepare_brief(
            session,
            actor_username=actor_username,
            invocation_source=AIInvocationSource.WEB,
            surface=surface,
            request_token=request_token,
            on_date=on_date,
        )
        try:
            await session.commit()
            break
        except Exception:
            await session.rollback()
            if prepare_try:
                raise
    if prepared is None:
        return None, "empty"
    if prepared.existing_artifact_id is not None:
        row = await brief_persistence.existing_brief_for_prepared(session, prepared)
        await session.commit()
        return row, "existing"
    if not prepared.dispatchable:
        if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
            return None, "pending"
        row = await brief_persistence.persist_brief(session, prepared, None)
        await session.commit()
        return row, "header"

    lease = None
    for start_try in range(2):
        try:
            lease = await brief_rendering.start_brief_dispatch(session, prepared)
        except ai_gateway_service_contracts.AIGatewayConfigurationError:
            await session.rollback()
            row = await brief_persistence.cancel_and_persist_header_brief(
                session, prepared
            )
            await session.commit()
            return row, "header"
        except ai_gateway_service_contracts.AIInvocationStateError:
            await session.rollback()
            recovered = await brief_preparation.prepare_brief(
                session,
                actor_username=actor_username,
                invocation_source=AIInvocationSource.WEB,
                surface=surface,
                request_token=request_token,
                on_date=on_date,
            )
            await session.commit()
            if recovered is None:
                return None, "empty"
            if recovered.existing_artifact_id is not None:
                row = await brief_persistence.existing_brief_for_prepared(
                    session, recovered
                )
                await session.commit()
                return row, "existing"
            if recovered.reservation_status is AIInvocationStatus.DISPATCHING:
                return None, "pending"
            row = await brief_persistence.persist_brief(session, recovered, None)
            await session.commit()
            return row, "header"
        try:
            await session.commit()
            break
        except Exception:
            # A lease whose COMMIT outcome is ambiguous is never dispatched.
            lease = None
            await session.rollback()
            prepared = await brief_preparation.prepare_brief(
                session,
                actor_username=actor_username,
                invocation_source=AIInvocationSource.WEB,
                surface=surface,
                request_token=request_token,
                on_date=on_date,
            )
            await session.commit()
            if prepared is None:
                return None, "empty"
            if prepared.existing_artifact_id is not None:
                row = await brief_persistence.existing_brief_for_prepared(
                    session, prepared
                )
                await session.commit()
                return row, "existing"
            if not prepared.dispatchable:
                if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
                    return None, "pending"
                row = await brief_persistence.persist_brief(session, prepared, None)
                await session.commit()
                return row, "header"
            if start_try:
                return None, "pending"
    if lease is None:  # pragma: no cover - every branch returns or assigns
        return None, "pending"
    completion = await brief_rendering.render_brief(prepared, lease)
    for persist_try in range(2):
        try:
            row = await brief_persistence.persist_brief(
                session, prepared, completion
            )
            await session.commit()
            return row, "ok" if row.model is not None else "header"
        except Exception:
            await session.rollback()
            if persist_try:
                raise
    raise RuntimeError("Daily Brief persistence did not resolve")


async def _release_digest_reservation(
    session: AsyncSession,
    prepared: digest_ownership.PreparedDigest | None,
) -> None:
    """Release a committed PREPARED call after a zero-network boundary error."""

    await session.rollback()
    if prepared is None or not prepared.dispatchable:
        return
    if await digest_generation.release_prepared_digest(session, prepared):
        await session.commit()
    else:
        await session.rollback()


__all__ = [
    "BriefWorkflowOutcome",
    "DigestWorkflowOutcome",
    "build_brief",
    "generate_digest",
    "send_test_brief",
]
