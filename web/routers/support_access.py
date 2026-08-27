"""Two sides of the same grant: an administrator asks, a patient answers.

Deliberately one module and two routers, because the pair only makes sense
together. Every rule about who may do what lives in
:mod:`vitals.services.support_access`; these routes carry a form to it
and turn its refusals into pages.

The administrator's console accepts one exact opaque record code and binds that
subject before reading anything; it neither searches nor lists patients. The
patient's screens resolve their subject from *who they are* rather than from the
path — the same rule ``web/routers/consents.py`` follows, for the same reason:
the subject has to come from whichever source cannot be stale.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import PolicyAction, PolicyResourceType
from vitals.enums import RECORD_SECTIONS, Domain, SupportAccessMode
from vitals.persistence.rls import bind_session_subject
from vitals.services.support_access import contracts as support_contracts
from vitals.services.support_access import export as support_export
from vitals.services.support_access import lifecycle as support_lifecycle
from vitals.services.support_access import projections as support_projections
from vitals.services.support_access import repair as support_repair
from vitals.services.portability import v1_contract
from vitals.services.authorization.subject_access import (
    AccessDeniedError,
    AccessResolutionError,
    enter_subject_scope,
    require_access,
    resolve_access_context,
)
from vitals.services.emergency import access as emergency_access
from vitals.services.tenancy.contracts import NoPersonalRecordError
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.utils.timeutils import today_local
from web.care_context import principal_user_id
from web.deps import get_session, require_auth, require_recent_auth
from web.downloads import private_json_download
from web.ratelimit import rate_limit
from web.templating import templates

admin_router = APIRouter(prefix="/settings/platform/support", tags=["support"])
patient_router = APIRouter(prefix="/settings/access", tags=["support"])


def _back(url: str, marker: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"{url}?{marker}", status_code=status.HTTP_303_SEE_OTHER
    )


def _console_back(subject_id: uuid.UUID, marker: str) -> RedirectResponse:
    key, _, value = marker.partition("=")
    query = urlencode({"record_id": str(subject_id), key: value})
    return RedirectResponse(
        url=f"/settings/platform/support?{query}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _record_selector(raw: str | None) -> uuid.UUID | None:
    if raw is None or not raw.strip():
        return None
    try:
        selected = uuid.UUID(raw.strip())
    except (ValueError, TypeError, AttributeError):
        return None
    return selected if selected.int else None


async def _admin_id(request: Request, db: AsyncSession) -> uuid.UUID:
    """The signed-in account, proven to be an active superadmin by the service.

    The check is not repeated here on purpose. Every entry point in the service
    makes it, and a second copy in the router is a second thing to keep in
    step — the one that gets forgotten is always the copy.
    """

    return await principal_user_id(request, db)


async def _own_subject(request: Request, db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """This account and the record it owns.

    Raises ``NoPersonalRecordError`` rather than a bare 409, which is the
    difference between the branded refusal page — with a way out of it — and one
    unstyled sentence. This page is about *my* record, and a doctor or a trainer
    keeps none; they are the accounts most likely to arrive here from a link
    somewhere, and the registered handler sends whoever holds patients to their
    roster instead of stranding them.
    """

    user_id = await principal_user_id(request, db)
    try:
        access = await resolve_access_context(db, user_id=user_id, subject_id=None)
    except AccessResolutionError as exc:
        raise NoPersonalRecordError(
            "this account keeps no health record of its own"
        ) from exc
    await enter_subject_scope(db, access)
    return user_id, access.subject_id


# ── The administrator's console ──────────────────────────────────────────────


@admin_router.get("", response_class=HTMLResponse)
async def console(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    asked: str | None = None,
    error: str | None = None,
    record_id: str | None = None,
):
    admin_user_id = await _admin_id(request, db)
    selected_subject_id = _record_selector(record_id)
    try:
        if selected_subject_id is not None:
            await bind_session_subject(db, selected_subject_id)
        state = await support_projections.console_for_admin(
            db,
            admin_user_id=admin_user_id,
            subject_id=selected_subject_id,
        )
    except support_contracts.NotAPlatformAdmin as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        ) from exc
    return templates.TemplateResponse(
        request,
        "settings/support_console.html",
        {
            "username": username,
            "grants": state.grants,
            "requests": state.requests,
            "selected_subject_id": selected_subject_id,
            "domains": [domain.value for domain in RECORD_SECTIONS],
            "default_hours": int(
                support_contracts.DEFAULT_GRANT_TTL.total_seconds() // 3600
            ),
            "max_hours": int(support_contracts.MAX_GRANT_TTL.total_seconds() // 3600),
            "asked": asked,
            "error": error,
        },
    )


@admin_router.post("/request")
async def ask_for_access(
    request: Request,
    subject_id: uuid.UUID = Form(...),
    reason: str = Form(...),
    hours: int = Form(...),
    domains: list[str] = Form(default=[]),
    ticket_reference: str = Form(default=""),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    admin_user_id = await _admin_id(request, db)
    try:
        chosen = [Domain(value) for value in domains]
    except ValueError:
        return _console_back(subject_id, "error=domain")
    if any(domain not in RECORD_SECTIONS for domain in chosen):
        # Not a section of anybody's record, so not a thing to be asked for —
        # checked here as well as omitted from the form, because the form is a
        # suggestion and this is the rule.
        return _console_back(subject_id, "error=domain")
    # The form names exactly one record. Binding it is the PostgreSQL isolation
    # boundary; the service's live platform-role check remains authorization.
    await bind_session_subject(db, subject_id)
    try:
        await support_lifecycle.open_request(
            db,
            admin_user_id=admin_user_id,
            subject_id=subject_id,
            reason=reason,
            scopes=support_contracts.read_scopes_for(chosen),
            ttl=timedelta(hours=hours),
            ticket_reference=ticket_reference,
        )
    except support_contracts.NotAPlatformAdmin as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        ) from exc
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _console_back(subject_id, "error=refused")
    await db.commit()
    return _console_back(subject_id, "asked=1")


@admin_router.post("/export/request")
async def ask_for_export(
    request: Request,
    subject_id: uuid.UUID = Form(...),
    reason: str = Form(...),
    ticket_reference: str = Form(default=""),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    """Ask for one separately approved, one-shot subject export."""

    admin_user_id = await _admin_id(request, db)
    await bind_session_subject(db, subject_id)
    try:
        await support_lifecycle.open_request(
            db,
            admin_user_id=admin_user_id,
            subject_id=subject_id,
            reason=reason,
            scopes=support_contracts.export_scope(),
            ttl=support_contracts.DEFAULT_GRANT_TTL,
            ticket_reference=ticket_reference,
            mode=SupportAccessMode.EXPORT,
        )
    except support_contracts.NotAPlatformAdmin as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        ) from exc
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _console_back(subject_id, "error=refused")
    await db.commit()
    return _console_back(subject_id, "asked=export")


@admin_router.post("/repair/request")
async def ask_for_repair(
    request: Request,
    subject_id: uuid.UUID = Form(...),
    reason: str = Form(...),
    ticket_reference: str = Form(default=""),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    """Ask for the fixed repair door; each later diff is reviewed again."""

    admin_user_id = await _admin_id(request, db)
    await bind_session_subject(db, subject_id)
    try:
        await support_lifecycle.open_request(
            db,
            admin_user_id=admin_user_id,
            subject_id=subject_id,
            reason=reason,
            scopes=support_contracts.repair_scope(),
            ttl=support_contracts.DEFAULT_GRANT_TTL,
            ticket_reference=ticket_reference,
            mode=SupportAccessMode.REPAIR,
        )
    except support_contracts.NotAPlatformAdmin as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _console_back(subject_id, "error=refused")
    await db.commit()
    return _console_back(subject_id, "asked=repair")


def _repair_selectors(subject_selector: str, grant_selector: str):
    try:
        subject_id = uuid.UUID(subject_selector)
        grant_id = uuid.UUID(grant_selector)
        if subject_id.int == 0 or grant_id.int == 0:
            raise ValueError
        return subject_id, grant_id
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


async def _repair_context(
    request: Request,
    db: AsyncSession,
    *,
    subject_selector: str,
    grant_selector: str,
):
    subject_id, grant_id = _repair_selectors(subject_selector, grant_selector)
    admin_user_id = await _admin_id(request, db)
    await bind_session_subject(db, subject_id)
    context = await resolve_access_context(
        db,
        user_id=admin_user_id,
        subject_id=subject_id,
        support_grant_id=grant_id,
    )
    require_access(
        context,
        resource_type=PolicyResourceType.DOMAIN,
        resource_key=Domain.WEIGHT.value,
        action=PolicyAction.READ,
    )
    require_access(
        context,
        resource_type=PolicyResourceType.OPERATION,
        resource_key=support_contracts.REPAIR_OPERATION_KEY,
        action=PolicyAction.REPAIR,
    )
    return context


@admin_router.get(
    "/{subject_selector}/grant/{grant_selector}/repair",
    response_class=HTMLResponse,
)
async def repair_workspace(
    request: Request,
    subject_selector: str,
    grant_selector: str,
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    saved: str | None = None,
    error: str | None = None,
):
    try:
        context = await _repair_context(
            request,
            db,
            subject_selector=subject_selector,
            grant_selector=grant_selector,
        )
        measurements, actions = await support_repair.repair_workspace(db, context=context)
        response = templates.TemplateResponse(
            request,
            "settings/support_repair.html",
            {
                "username": username,
                "context": context,
                "measurements": measurements,
                "actions": actions,
                "idempotency_key": uuid.uuid4(),
                "saved": saved,
                "error": error,
            },
        )
        await support_export.record_record_opened(
            db, context=context, domain_keys=(Domain.WEIGHT.value,)
        )
        await db.commit()
        return response
    except (AccessResolutionError, AccessDeniedError, support_contracts.SupportAccessError):
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


@admin_router.post(
    "/{subject_selector}/grant/{grant_selector}/repair/propose"
)
async def propose_repair(
    request: Request,
    subject_selector: str,
    grant_selector: str,
    measurement_id: int = Form(...),
    idempotency_key: uuid.UUID = Form(...),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    back = (
        f"/settings/platform/support/{subject_selector}/grant/"
        f"{grant_selector}/repair"
    )
    try:
        context = await _repair_context(
            request,
            db,
            subject_selector=subject_selector,
            grant_selector=grant_selector,
        )
        await support_repair.propose_clear_derived_estimates(
            db,
            context=context,
            measurement_id=measurement_id,
            idempotency_key=idempotency_key,
        )
        await db.commit()
    except (AccessResolutionError, AccessDeniedError, support_contracts.SupportAccessError):
        await db.rollback()
        return _back(back, "error=refused")
    return _back(back, "saved=proposed")


@admin_router.post(
    "/{subject_selector}/grant/{grant_selector}/repair/{action_id}/execute"
)
async def execute_repair(
    request: Request,
    subject_selector: str,
    grant_selector: str,
    action_id: uuid.UUID,
    override: bool = Form(False),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    back = (
        f"/settings/platform/support/{subject_selector}/grant/"
        f"{grant_selector}/repair"
    )
    try:
        context = await _repair_context(
            request,
            db,
            subject_selector=subject_selector,
            grant_selector=grant_selector,
        )
        action = await support_repair.execute_repair(
            db,
            context=context,
            action_id=action_id,
            override=override,
        )
        await db.commit()
    except ConflictBlocked as exc:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [row.to_dict() for row in exc.violations]},
        )
    except (AccessResolutionError, AccessDeniedError, support_contracts.SupportAccessError):
        await db.rollback()
        return _back(back, "error=refused")
    marker = "stale" if action.status == "stale" else "executed"
    return _back(back, f"saved={marker}")


@admin_router.post("/{subject_selector}/grant/{grant_selector}/export")
async def download_export(
    request: Request,
    subject_selector: str,
    grant_selector: str,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_export", limit=2, window=60)),
):
    """Release one transient export, consuming the exact grant atomically."""

    try:
        subject_id = uuid.UUID(subject_selector)
        grant_id = uuid.UUID(grant_selector)
        if subject_id.int == 0 or grant_id.int == 0:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    admin_user_id = await _admin_id(request, db)
    try:
        await bind_session_subject(db, subject_id)
        context = await resolve_access_context(
            db,
            user_id=admin_user_id,
            subject_id=subject_id,
            support_grant_id=grant_id,
        )
        require_access(
            context,
            resource_type=PolicyResourceType.OPERATION,
            resource_key=support_contracts.EXPORT_OPERATION_KEY,
            action=PolicyAction.EXPORT,
        )
        payload = await support_export.consume_subject_export(db, context=context)
        # Serialization is part of the release transaction. If it fails, the
        # grant and success audit roll back and the patient approval is retryable.
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        await db.commit()
    except (
        AccessResolutionError,
        AccessDeniedError,
        support_contracts.SupportAccessError,
    ):
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except v1_contract.PortabilityError as exc:
        # A valid grant can outlive the v1 format's ability to represent the
        # record (for example after a provider connection was added). Keep the
        # one-shot approval retryable and return a controlled, non-PHI refusal.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except BaseException:
        await db.rollback()
        raise

    filename = f"vitals_support_record_{today_local().strftime('%Y%m%d')}.json"
    return private_json_download(body=body, filename=filename)


@admin_router.post("/{request_id}/withdraw")
async def withdraw(
    request: Request,
    request_id: uuid.UUID,
    subject_id: uuid.UUID = Form(...),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    admin_user_id = await _admin_id(request, db)
    await bind_session_subject(db, subject_id)
    try:
        await support_lifecycle.withdraw_request(
            db, admin_user_id=admin_user_id, request_id=request_id
        )
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _console_back(subject_id, "error=refused")
    await db.commit()
    return _console_back(subject_id, "asked=withdrawn")


@admin_router.post("/grant/{grant_id}/revoke")
async def hand_it_back(
    request: Request,
    grant_id: uuid.UUID,
    subject_id: uuid.UUID = Form(...),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    """The admin putting the access down rather than waiting for it to lapse."""

    admin_user_id = await _admin_id(request, db)
    await bind_session_subject(db, subject_id)
    try:
        await support_lifecycle.revoke_grant(
            db,
            actor_user_id=admin_user_id,
            grant_id=grant_id,
            reason="Handed back by the administrator who held it.",
        )
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _console_back(subject_id, "error=refused")
    await db.commit()
    return _console_back(subject_id, "asked=handed-back")


# ── The patient's side ───────────────────────────────────────────────────────


@patient_router.get("", response_class=HTMLResponse)
async def access_history(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    decided: str | None = None,
    error: str | None = None,
):
    """Every ask ever made about this record, and whatever is live right now.

    Reachable whether or not anything has ever been asked. A page that only
    exists once support has been in is a page nobody knows to look for, and
    "has anybody been reading my record" is a question a patient is entitled to
    ask on a quiet day and get *no* for.
    """

    _user_id, subject_id = await _own_subject(request, db)
    context = await resolve_access_context(db, user_id=_user_id, subject_id=None)
    history = await support_lifecycle.list_for_subject(db, context=context)
    repairs = await support_repair.repair_actions_for_subject(db, context=context)
    opened = await support_projections.record_opened_history(db, subject_id=subject_id)
    live_grants = await support_lifecycle.live_grants_for(db, context=context)
    emergency_history = await emergency_access.list_for_subject(
        db, owner_user_id=_user_id, subject_id=subject_id
    )
    return templates.TemplateResponse(
        request,
        "settings/access_history.html",
        {
            "username": username,
            "subject_id": subject_id,
            "pending_requests": history.pending,
            "past_requests": history.past,
            "request_history_has_more": history.has_more,
            "opened": opened.events,
            "opened_has_more": opened.has_more,
            "live_grants": live_grants,
            "repair_actions": repairs,
            "emergency_history": emergency_history,
            "decided": decided,
            "error": error,
        },
    )


@patient_router.post("/{request_id}/approve")
async def approve(
    request: Request,
    request_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    user_id, _subject_id = await _own_subject(request, db)
    try:
        await support_lifecycle.approve_request(
            db, owner_user_id=user_id, request_id=request_id
        )
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _back("/settings/access", "error=refused")
    await db.commit()
    return _back("/settings/access", "decided=approved")


@patient_router.post("/{request_id}/decline")
async def decline(
    request: Request,
    request_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    user_id, _subject_id = await _own_subject(request, db)
    try:
        await support_lifecycle.decline_request(
            db, owner_user_id=user_id, request_id=request_id
        )
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _back("/settings/access", "error=refused")
    await db.commit()
    return _back("/settings/access", "decided=declined")


@patient_router.post("/repairs/{action_id}/approve")
async def approve_repair(
    request: Request,
    action_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    user_id, _subject_id = await _own_subject(request, db)
    try:
        await support_repair.review_repair(
            db, owner_user_id=user_id, action_id=action_id, approve=True
        )
        await db.commit()
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _back("/settings/access", "error=refused")
    return _back("/settings/access", "decided=repair-approved")


@patient_router.post("/repairs/{action_id}/decline")
async def decline_repair(
    request: Request,
    action_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    user_id, _subject_id = await _own_subject(request, db)
    try:
        await support_repair.review_repair(
            db, owner_user_id=user_id, action_id=action_id, approve=False
        )
        await db.commit()
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _back("/settings/access", "error=refused")
    return _back("/settings/access", "decided=repair-declined")


@patient_router.post("/repairs/{action_id}/revert")
async def revert_repair(
    request: Request,
    action_id: uuid.UUID,
    override: bool = Form(False),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    user_id, _subject_id = await _own_subject(request, db)
    try:
        await support_repair.revert_repair(
            db,
            owner_user_id=user_id,
            action_id=action_id,
            override=override,
        )
        await db.commit()
    except ConflictBlocked as exc:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [row.to_dict() for row in exc.violations]},
        )
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _back("/settings/access", "error=refused")
    return _back("/settings/access", "decided=repair-reverted")


@patient_router.post("/grant/{grant_id}/revoke")
async def take_it_back(
    request: Request,
    grant_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    """Changing your mind, without having to find anybody to ask."""

    user_id, _subject_id = await _own_subject(request, db)
    try:
        await support_lifecycle.revoke_grant(
            db,
            actor_user_id=user_id,
            grant_id=grant_id,
            reason="Withdrawn by the person whose record it is.",
        )
    except support_contracts.SupportAccessError:
        await db.rollback()
        return _back("/settings/access", "error=refused")
    await db.commit()
    return _back("/settings/access", "decided=revoked")
