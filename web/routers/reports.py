"""Endpoints for module 10: goal cards (milestones) and the weekly AI digest."""
from __future__ import annotations

import logging
import hashlib
import secrets
from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    AIInvocationSource,
    AIInvocationStatus,
    DigestKind,
    Domain,
)
from vitals.services import (
    ai_gateway_service,
    conflict_engine,
    digest_service,
    milestones_service,
)
from vitals.services.legacy_ownership import (
    LegacyOwnershipError,
)
from vitals.services.proactive import brief, channels, delivery
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth
from web.ratelimit import rate_limit
from web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])

# Domains a goal can relate to (for the create form select).
GOAL_DOMAINS = [
    Domain.WEIGHT.value, Domain.BODY_COMPOSITION.value, Domain.GLP1.value, Domain.WORKOUTS.value,
    Domain.GARMIN.value, Domain.LABS.value, Domain.SKINCARE.value,
]


@router.get("", response_class=HTMLResponse)
async def reports_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Goal cards, the latest weekly digest and its history, and today's brief."""
    milestone_scope = await conflict_engine.resolve_legacy_conflict_scope(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    cards = await milestones_service.dashboard_cards(
        db,
        subject_id=milestone_scope.subject_id,
        include_legacy_unowned=milestone_scope.include_legacy_unowned,
    )
    digest_owner = await digest_service.prepare_digest_owner(
        db,
        actor_username=username,
    )
    latest = await digest_service.latest_digest(db, prepared_owner=digest_owner)
    history = await digest_service.list_digests(
        db,
        limit=12,
        prepared_owner=digest_owner,
    )
    latest_brief = await digest_service.latest_digest(
        db,
        kind=DigestKind.DAILY_BRIEF.value,
        prepared_owner=digest_owner,
    )
    config = load_config()
    ai_availability = await brief.project_ai_availability(
        db,
        actor_username=username,
    )

    return templates.TemplateResponse(
        request,
        "reports/index.html",
        {
            "username": username,
            "cards": cards,
            "latest_digest": latest,
            "history": history,
            "latest_brief": latest_brief,
            "goal_domains": GOAL_DOMAINS,
            "llm_configured": ai_availability.available,
            "brief_ai_available": ai_availability.available,
            "channel_configured": bool(config.telegram_bot_token and config.telegram_chat_id),
            "brief_build_token": secrets.token_urlsafe(24),
            "brief_test_token": secrets.token_urlsafe(24),
            "today": today_local().isoformat(),
            "digest": request.query_params.get("digest"),
            "brief": request.query_params.get("brief"),
        },
    )


@router.post("/milestone")
async def create_milestone(
    request: Request,
    name: str = Form(...),
    domain: str = Form(Domain.WEIGHT.value),
    target_value: Optional[float] = Form(None),
    target_unit: Optional[str] = Form(None),
    deadline: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context = await conflict_engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    prepared = await conflict_engine.prepare_scoped_write(
        db,
        context=conflict_context,
    )
    await milestones_service.create_milestone(
        db,
        name=name.strip(),
        domain=domain,
        target_value=target_value,
        target_unit=target_unit,
        deadline=date_type.fromisoformat(deadline) if deadline else None,
        note=note,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/milestone/{milestone_id}/status")
async def set_milestone_status(
    request: Request,
    milestone_id: int,
    status_value: str = Form(..., alias="status"),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context = await conflict_engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    prepared = await conflict_engine.prepare_scoped_write(
        db,
        context=conflict_context,
    )
    await milestones_service.set_status(
        db,
        milestone_id,
        status_value,
        identity=conflict_context.identity,
        include_legacy_unowned=True,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/milestone/{milestone_id}/delete")
async def delete_milestone(
    request: Request,
    milestone_id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context = await conflict_engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    prepared = await conflict_engine.prepare_scoped_write(
        db,
        context=conflict_context,
    )
    await milestones_service.delete_milestone(
        db,
        milestone_id,
        identity=conflict_context.identity,
        include_legacy_unowned=True,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/digest")
async def generate_digest_now(
    request: Request,
    period_days: int = Form(7),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _rl: None = Depends(rate_limit("digest_generate", limit=5, window=60)),
):
    """Generate this week's digest on demand."""
    prepared = None
    try:
        prepared = await digest_service.prepare_digest(
            db,
            actor_username=username,
            invocation_source=AIInvocationSource.WEB,
            period_days=period_days,
        )
        await db.commit()
        if prepared.existing_artifact_id is not None:
            return _redirect(request, "?digest=ok")
        if not prepared.dispatchable:
            if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
                return _redirect(request, "?digest=pending")
            return _redirect(request, "?digest=error")
        lease = await digest_service.start_digest_dispatch(db, prepared)
        await db.commit()
        completion = await digest_service.render_digest(prepared, lease)
        row = await digest_service.persist_digest(db, prepared, completion)
        await db.commit()
        if row is None:
            return _redirect(request, "?digest=provider_error")
    except ai_gateway_service.AIQuotaExceededError:
        await db.rollback()
        return _redirect(request, "?digest=quota")
    except ai_gateway_service.AIGatewayConfigurationError:
        await _release_digest_reservation(db, prepared)
        return _redirect(request, "?digest=not_configured")
    except (
        ai_gateway_service.AIGatewayAuthorizationError,
        LegacyOwnershipError,
        digest_service.DigestOwnershipError,
        milestones_service.MilestoneOwnershipError,
    ):
        await _release_digest_reservation(db, prepared)
        raise
    except ai_gateway_service.AIInvocationStateError:
        await db.rollback()
        return _redirect(request, "?digest=pending")
    except Exception:  # noqa: BLE001 — surface generation failures softly
        await _release_digest_reservation(db, prepared)
        logger.warning("Digest generation failed (code=internal_error)")
        return _redirect(request, "?digest=error")
    return _redirect(request, "?digest=ok")


@router.post("/brief")
async def build_brief_now(
    request: Request,
    request_token: str = Form(
        ...,
        min_length=22,
        max_length=96,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _rl: None = Depends(rate_limit("brief_build", limit=10, window=60)),
):
    """Assemble one intentionally requested brief and show it without sending."""
    request_date = today_local()
    try:
        row, outcome = await _run_brief_generation(
            db,
            actor_username=username,
            surface=brief.BriefSurface.BUILD,
            request_token=request_token,
            on_date=request_date,
        )
    except (
        ai_gateway_service.AIGatewayAuthorizationError,
        LegacyOwnershipError,
        digest_service.DigestOwnershipError,
        brief.BriefOwnershipError,
    ):
        await db.rollback()
        raise
    except Exception:  # noqa: BLE001 — sanitized soft failure
        await db.rollback()
        logger.warning("Daily Brief build failed (code=internal_error)")
        return _redirect(request, "?brief=error")
    if outcome == "pending":
        return _redirect(request, "?brief=pending")
    if row is None:
        return _redirect(request, "?brief=empty")
    return _redirect(
        request,
        "?brief=header" if row.model is None else "?brief=ok",
    )


@router.post("/brief/test")
async def send_test_brief(
    request: Request,
    request_token: str = Form(
        ...,
        min_length=22,
        max_length=96,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _rl: None = Depends(rate_limit("brief_test", limit=5, window=300)),
):
    """One live send, to catch what only a real Telegram message shows — broken
    formatting, a message too long, a channel that isn't actually wired up."""
    request_date = today_local()
    try:
        request_token = brief.validate_request_token(request_token)
        ownership = await channels.resolve_legacy_channel_ownership(
            db,
            actor_username=username,
        )
        legacy_test_dedupe_key = (
            f"brief_test:{request_date.isoformat()}:"
            f"{hashlib.sha256(request_token.encode()).hexdigest()}"
        )
        test_delivery_key = delivery.make_delivery_idempotency_key(
            "brief-test",
            request_date,
            request_token,
        )
        if await delivery.confirmed_delivery_journal(
            db,
            idempotency_key=test_delivery_key,
            category=delivery.CATEGORY_TEST,
            ownership=ownership,
            legacy_dedupe_key=legacy_test_dedupe_key,
            actor_user_id=ownership.recipient_user_id,
        ) is not None:
            await db.commit()
            return _redirect(request, "?brief=sent")
        if await delivery.delivery_claim_exists(
            db,
            idempotency_key=test_delivery_key,
            ownership=ownership,
        ):
            await db.commit()
            return _redirect(request, "?brief=pending")
        endpoint_available = await channels.build_legacy_bound_notifier(
            db,
            ownership,
        )
        if endpoint_available is None:
            await db.commit()
            return _redirect(request, "?brief=no_channel")
        # Availability is only a preflight. T1 below resolves a fresh bound
        # client, and T2 resolves again after its current-policy/C recheck.
        del endpoint_available
        await db.commit()
        row, outcome = await _run_brief_generation(
            db,
            actor_username=username,
            surface=brief.BriefSurface.TEST,
            request_token=request_token,
            on_date=request_date,
        )
        if outcome == "pending":
            return _redirect(request, "?brief=pending")
        if row is None:
            return _redirect(request, "?brief=empty")
        ownership = await channels.resolve_legacy_channel_ownership(
            db,
            actor_username=username,
        )
        bound_notifier = await channels.build_legacy_bound_notifier(
            db,
            ownership,
        )
        if bound_notifier is None:
            await db.commit()
            return _redirect(request, "?brief=no_channel")
        prepared_delivery = await delivery.prepare_delivery_intent(
            db,
            bound_notifier,
            text=row.content,
            category=delivery.CATEGORY_TEST,
            idempotency_key=test_delivery_key,
            legacy_dedupe_key=legacy_test_dedupe_key,
            ownership=ownership,
            actor_user_id=ownership.recipient_user_id,
        )
        await db.commit()
        if prepared_delivery is None:
            ownership = await channels.resolve_legacy_channel_ownership(
                db,
                actor_username=username,
            )
            if await delivery.confirmed_delivery_journal(
                db,
                idempotency_key=test_delivery_key,
                category=delivery.CATEGORY_TEST,
                ownership=ownership,
                legacy_dedupe_key=legacy_test_dedupe_key,
                actor_user_id=ownership.recipient_user_id,
            ) is not None:
                await db.commit()
                return _redirect(request, "?brief=sent")
            claimed = await delivery.delivery_claim_exists(
                db,
                idempotency_key=test_delivery_key,
                ownership=ownership,
            )
            await db.commit()
            return _redirect(
                request,
                "?brief=pending" if claimed else "?brief=error",
            )
        dispatch_lease = await delivery.start_delivery_dispatch(
            db,
            prepared_delivery,
            notifier_resolver=channels.resolve_legacy_bound_notifier,
        )
        await db.commit()
        if dispatch_lease is None:
            return _redirect(request, "?brief=error")
        completion = await delivery.dispatch_delivery(dispatch_lease)
        journal = None
        for finalize_try in range(2):
            try:
                journal = await delivery.finalize_delivery(db, completion)
                await db.commit()
                break
            except Exception:
                await db.rollback()
                if finalize_try:
                    raise
        if journal is None:
            return _redirect(request, "?brief=error")
    except (
        ai_gateway_service.AIGatewayAuthorizationError,
        LegacyOwnershipError,
        digest_service.DigestOwnershipError,
        brief.BriefOwnershipError,
    ):
        await db.rollback()
        raise
    except Exception:  # noqa: BLE001 — sanitized soft failure
        await db.rollback()
        logger.warning("Daily Brief test failed (code=internal_error)")
        return _redirect(request, "?brief=error")
    return _redirect(request, "?brief=sent")


async def _run_brief_generation(
    session: AsyncSession,
    *,
    actor_username: str,
    surface: brief.BriefSurface,
    request_token: str,
    on_date: date_type,
) -> tuple[object | None, str]:
    """Own T1/T2/T3 commits while provider I/O stays transaction-free."""

    prepared = None
    for prepare_try in range(2):
        prepared = await brief.prepare_brief(
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
        row = await brief.existing_brief_for_prepared(session, prepared)
        await session.commit()
        return row, "existing"
    if not prepared.dispatchable:
        if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
            return None, "pending"
        row = await brief.persist_brief(session, prepared, None)
        await session.commit()
        return row, "header"

    lease = None
    for start_try in range(2):
        try:
            lease = await brief.start_brief_dispatch(session, prepared)
        except ai_gateway_service.AIGatewayConfigurationError:
            await session.rollback()
            row = await brief.cancel_and_persist_header_brief(session, prepared)
            await session.commit()
            return row, "header"
        except ai_gateway_service.AIInvocationStateError:
            await session.rollback()
            recovered = await brief.prepare_brief(
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
                row = await brief.existing_brief_for_prepared(session, recovered)
                await session.commit()
                return row, "existing"
            if recovered.reservation_status is AIInvocationStatus.DISPATCHING:
                return None, "pending"
            row = await brief.persist_brief(session, recovered, None)
            await session.commit()
            return row, "header"
        try:
            await session.commit()
            break
        except Exception:
            # A lease whose COMMIT outcome is ambiguous is never dispatched.
            lease = None
            await session.rollback()
            prepared = await brief.prepare_brief(
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
                row = await brief.existing_brief_for_prepared(session, prepared)
                await session.commit()
                return row, "existing"
            if not prepared.dispatchable:
                if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
                    return None, "pending"
                row = await brief.persist_brief(session, prepared, None)
                await session.commit()
                return row, "header"
            if start_try:
                return None, "pending"
    if lease is None:  # pragma: no cover - every branch returns or assigns
        return None, "pending"
    completion = await brief.render_brief(prepared, lease)
    for persist_try in range(2):
        try:
            row = await brief.persist_brief(session, prepared, completion)
            await session.commit()
            return row, "ok" if row.model is not None else "header"
        except Exception:
            await session.rollback()
            if persist_try:
                raise
    raise RuntimeError("Daily Brief persistence did not resolve")


def _redirect(request: Request, suffix: str = "") -> RedirectResponse:
    url = f"/reports{suffix}"
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = url
    return response


async def _release_digest_reservation(
    session: AsyncSession,
    prepared: digest_service.PreparedDigest | None,
) -> None:
    """Release a committed PREPARED call after a zero-network boundary error."""

    await session.rollback()
    if prepared is None or not prepared.dispatchable:
        return
    if await digest_service.release_prepared_digest(session, prepared):
        await session.commit()
    else:
        await session.rollback()
