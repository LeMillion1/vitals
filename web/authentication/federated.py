"""OIDC browser protocol routes and response rendering."""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import t
from vitals.services.authentication import admission
from vitals.services.authentication.federation import (
    FederatedLoginError,
    FederatedRegistrationDecision,
    decide_federated_login,
)
from vitals.services.authentication.oidc import OidcError
from web.authentication.tokens import (
    OIDC_AUTH_SOURCE,
    clear_oidc_handoff_cookie,
    clear_pending_2fa_cookie,
    clear_session_cookie,
    create_federated_session,
    create_oidc_handoff,
    decode_session,
    read_oidc_handoff,
    read_session,
    safe_next,
    set_oidc_handoff_cookie,
    set_session_cookie,
)
from web.config import (
    OIDC_HANDOFF_COOKIE,
    REGISTRATION_ADMISSION_COOKIE,
    REGISTRATION_INTENT_COOKIE,
    REGISTRATION_REQUEST_COOKIE,
    SESSION_COOKIE,
    get_web_config,
)
from web.deps import get_session
from web.ratelimit import login_rate_limit
from web.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()

_provider_cache: tuple[tuple[str, str, str], object] | None = None
_REGISTRATION_REQUEST_CSP = (
    "default-src 'none'; "
    "script-src 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


def _registration_request_response(
    request: Request,
    *,
    state: str,
    reference: uuid.UUID,
) -> Response:
    """Show one non-enumerable applicant state without granting a session."""

    csp_nonce = secrets.token_urlsafe(24)
    response = templates.TemplateResponse(
        request,
        "registration_request_status.html",
        {
            "request_state": state,
            "request_reference": str(reference),
            "csp_nonce": csp_nonce,
        },
        status_code=(
            status.HTTP_202_ACCEPTED if state == "pending" else status.HTTP_200_OK
        ),
        headers={
            "Content-Security-Policy": _REGISTRATION_REQUEST_CSP.format(
                nonce=csp_nonce
            ),
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            "Cache-Control": "no-store",
        },
    )
    clear_session_cookie(response)
    clear_pending_2fa_cookie(response)
    clear_oidc_handoff_cookie(response)
    from web.admission_handoff import clear_invitation_claim_cookie
    from web.admission_handoff import clear_request_status_cookie

    clear_invitation_claim_cookie(response)
    clear_request_status_cookie(response)
    return response


def _registration_request_redirect(*, state: str, reference: uuid.UUID) -> Response:
    """Spend the OAuth query at a clean URL using an opaque signed handoff."""

    from web.admission_handoff import (
        clear_invitation_claim_cookie,
        clear_request_status_cookie,
        create_request_status_claim,
        set_request_status_cookie,
    )

    response = RedirectResponse(
        url="/auth/registration-request",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )
    clear_session_cookie(response)
    clear_pending_2fa_cookie(response)
    clear_oidc_handoff_cookie(response)
    clear_invitation_claim_cookie(response)
    clear_request_status_cookie(response)
    set_request_status_cookie(
        response,
        create_request_status_claim(reference, state=state),
    )
    return response


def _provider():
    """Return the configured, discovery-caching OIDC provider."""

    global _provider_cache
    from vitals.services.authentication.oidc import OidcProvider, OidcSettings

    cfg = get_web_config()
    key = (cfg.oidc_issuer, cfg.oidc_client_id, cfg.oidc_redirect_url)
    if _provider_cache is not None and _provider_cache[0] == key:
        return _provider_cache[1]
    provider = OidcProvider(
        OidcSettings(
            issuer=cfg.oidc_issuer,
            client_id=cfg.oidc_client_id,
            client_secret=cfg.oidc_client_secret,
            redirect_url=cfg.oidc_redirect_url,
        )
    )
    _provider_cache = (key, provider)
    return provider


def _login_failed(
    request: Request,
    reason: str,
    *,
    next_url: str | None = None,
    retry_url: str | None = None,
):
    """Render one response for every federated-login refusal."""

    logger.warning("federated login refused: %s", reason)
    destination = safe_next(next_url)
    response = templates.TemplateResponse(
        request,
        "oidc_error.html",
        {
            "error": t("login.error.federated"),
            "retry_url": retry_url
            or f"/auth/start?next={quote(destination, safe='')}",
        },
        status_code=status.HTTP_401_UNAUTHORIZED,
    )
    clear_oidc_handoff_cookie(response)
    from web.admission_handoff import clear_request_status_cookie

    clear_request_status_cookie(response)
    return response


@router.get("/auth/registration-request", response_class=HTMLResponse)
async def registration_request_status(
    request: Request,
    _rl: None = Depends(
        login_rate_limit(
            limit=30,
            window=300,
            bucket="registration_request_status",
        )
    ),
):
    """Render one signed applicant state after removing OAuth query secrets."""

    from web.admission_handoff import (
        clear_request_status_cookie,
        read_request_status_claim,
    )

    claim = read_request_status_claim(
        request.cookies.get(REGISTRATION_REQUEST_COOKIE)
    )
    if claim is None:
        response = _login_failed(request, "registration request status expired")
        clear_request_status_cookie(response)
        return response
    request_id, request_state = claim
    return _registration_request_response(
        request,
        state=request_state,
        reference=request_id,
    )


@router.get("/auth/start")
async def federated_login_start(
    request: Request,
    next: Optional[str] = None,
    step_up: bool = False,
):
    """Begin a login at the provider."""

    cfg = get_web_config()
    if not cfg.oidc_enabled:
        raise HTTPException(status_code=404)

    from web.admission_handoff import (
        read_invitation_claim,
        read_registration_intent_claim,
    )

    invitation_id = (
        None
        if step_up
        else read_invitation_claim(
            request.cookies.get(REGISTRATION_ADMISSION_COOKIE)
        )
    )
    registration_intent_id = (
        None
        if step_up or invitation_id is not None
        else read_registration_intent_claim(
            request.cookies.get(REGISTRATION_INTENT_COOKIE)
        )
    )
    if (
        not step_up
        and invitation_id is None
        and registration_intent_id is None
        and read_session(request.cookies.get(SESSION_COOKIE)) is not None
    ):
        response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
        from web.admission_handoff import clear_request_status_cookie

        clear_request_status_cookie(response)
        return response

    try:
        login = await _provider().begin_login(
            prompt="login" if step_up or invitation_id is not None else None,
            max_age_seconds=900 if step_up or invitation_id is not None else None,
        )
    except OidcError as exc:
        return _login_failed(
            request,
            f"could not begin a login: {exc}",
            next_url=next,
        )

    response = RedirectResponse(
        url=login.authorization_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    from web.admission_handoff import clear_request_status_cookie

    clear_request_status_cookie(response)
    set_oidc_handoff_cookie(
        response,
        create_oidc_handoff(
            state=login.state,
            nonce=login.nonce,
            code_verifier=login.code_verifier,
            next_url=safe_next(next),
            max_age_seconds=900 if step_up or invitation_id is not None else None,
            invitation_id=invitation_id,
            registration_intent_id=registration_intent_id,
        ),
    )
    return response


@router.get("/auth/callback")
async def federated_login_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    iss: Optional[str] = None,
    error: Optional[str] = None,
    _rl: None = Depends(login_rate_limit(limit=10, window=300)),
    db: AsyncSession = Depends(get_session),
):
    """Validate the OIDC callback and translate its domain outcome to HTTP."""

    cfg = get_web_config()
    if not cfg.oidc_enabled:
        raise HTTPException(status_code=404)

    handoff = read_oidc_handoff(request.cookies.get(OIDC_HANDOFF_COOKIE))
    if handoff is None:
        return _login_failed(request, "callback arrived with no usable handoff")
    if error:
        return _login_failed(
            request,
            f"provider returned an error: {error}",
            next_url=handoff["next"],
        )
    if not code or not state:
        return _login_failed(
            request,
            "callback arrived without a code and state",
            next_url=handoff["next"],
        )
    if not secrets.compare_digest(state, handoff["state"]):
        return _login_failed(
            request,
            "callback state does not match this browser's",
            next_url=handoff["next"],
        )

    invitation_id = handoff["invitation_id"]
    registration_intent_id = handoff["registration_intent_id"]
    if invitation_id is not None:
        from web.admission_handoff import (
            clear_invitation_claim_cookie,
            read_invitation_claim,
        )

        browser_claim = read_invitation_claim(
            request.cookies.get(REGISTRATION_ADMISSION_COOKIE)
        )
        if browser_claim != invitation_id:
            response = _login_failed(
                request,
                "callback invitation claim does not match this browser's",
                next_url=handoff["next"],
            )
            clear_invitation_claim_cookie(response)
            return response

    if registration_intent_id is not None:
        from web.admission_handoff import (
            clear_registration_intent_cookie,
            read_registration_intent_claim,
        )

        browser_claim = read_registration_intent_claim(
            request.cookies.get(REGISTRATION_INTENT_COOKIE)
        )
        if browser_claim != registration_intent_id:
            response = _login_failed(
                request,
                "callback registration intent does not match this browser's",
                next_url=handoff["next"],
                retry_url="/register",
            )
            clear_registration_intent_cookie(response)
            return response

    provider = _provider()
    try:
        provider.check_response_issuer(iss)
        identity = await provider.complete_login(
            code=code,
            code_verifier=handoff["code_verifier"],
            expected_nonce=handoff["nonce"],
            max_age_seconds=handoff["max_age_seconds"],
        )
    except OidcError as exc:
        return _login_failed(
            request,
            f"token rejected: {exc}",
            next_url=handoff["next"],
        )

    try:
        decision = await decide_federated_login(
            db,
            identity=identity,
            bootstrap_subject=cfg.oidc_bootstrap_subject,
            invitation_id=invitation_id,
            registration_intent_id=registration_intent_id,
            step_up=handoff["max_age_seconds"] is not None,
        )
    except (
        FederatedLoginError,
        admission.AdmissionError,
        admission.AdmissionValidationError,
    ) as exc:
        await db.rollback()
        response = _login_failed(
            request,
            f"no session for this identity: {exc}",
            next_url=handoff["next"],
            retry_url=("/register" if registration_intent_id is not None else None),
        )
        if registration_intent_id is not None:
            from web.admission_handoff import clear_registration_intent_cookie

            clear_registration_intent_cookie(response)
        return response

    if isinstance(decision, FederatedRegistrationDecision):
        # Persist the proof before telling the browser that it exists.
        await db.commit()
        return _registration_request_redirect(
            state=decision.state,
            reference=decision.reference,
        )

    token = create_federated_session(
        username=decision.username,
        user_id=decision.user_id,
        session_version=decision.session_version,
        authenticated_at=(
            int(decision.authenticated_at.timestamp())
            if decision.authenticated_at is not None
            else None
        ),
        subject_id=decision.subject_id,
    )
    response = RedirectResponse(
        url=safe_next(handoff["next"]),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    set_session_cookie(response, token)
    clear_oidc_handoff_cookie(response)
    from web.admission_handoff import clear_request_status_cookie

    clear_request_status_cookie(response)
    if invitation_id is not None:
        from web.admission_handoff import clear_invitation_claim_cookie

        clear_invitation_claim_cookie(response)
    if registration_intent_id is not None:
        from web.admission_handoff import clear_registration_intent_cookie

        clear_registration_intent_cookie(response)
    return response


@router.post("/logout")
async def logout(request: Request):
    """Clear the local session and use provider logout when available."""

    target = "/login"
    cfg = get_web_config()
    claims = decode_session(request.cookies.get(SESSION_COOKIE))
    if (
        cfg.oidc_enabled
        and claims is not None
        and claims.auth_source == OIDC_AUTH_SOURCE
    ):
        callback = urlsplit(cfg.oidc_redirect_url)
        post_logout_redirect_uri = urlunsplit(
            (callback.scheme, callback.netloc, "/", "", "")
        )
        try:
            provider_target = await _provider().end_session_url(
                post_logout_redirect_uri=post_logout_redirect_uri
            )
            if provider_target is not None:
                target = provider_target
        except OidcError as exc:
            logger.warning("provider logout unavailable: %s", exc)

    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    clear_pending_2fa_cookie(response)
    from web.admission_handoff import clear_registration_intent_cookie

    clear_registration_intent_cookie(response)
    return response


__all__ = ["router"]
