"""Account security, connected assistants, and personal API access."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import ExternalApiTokenStatus
from vitals.services.authentication import legacy_two_factor as twofa_service
from vitals.services.authentication import mcp_tokens
from vitals.services.external_api import tokens as external_tokens
from vitals.services.identity.queries import find_user_id_by_username
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
from web.config import get_web_config
from web.deps import get_session, require_auth
from web.ratelimit import rate_limit
from web.services.env_writer import read_key, write_keys
from web.templating import templates

from .common import SETTINGS_SECURITY_PATH, redirect as _redirect

logger = logging.getLogger(__name__)
router = APIRouter()

async def _external_token_rows(db: AsyncSession, *, subject_id) -> list[dict]:
    """Flatten personal bearer credentials without exposing their secrets."""
    now = datetime.now(timezone.utc)
    rows = await external_tokens.list_for_subject(db, subject_id=subject_id)
    listed = []
    for row in rows:
        if row.status == ExternalApiTokenStatus.REVOKED.value:
            state = "revoked"
        elif external_tokens.is_live(row, at=now):
            state = "active"
        else:
            state = "expired"
        listed.append({
            "id": row.id, "label": row.label, "state": state,
            "expires_at": row.expires_at,
        })
    return listed


async def _connector_rows(
    db: AsyncSession,
    *,
    actor_username: str,
) -> list[dict]:
    """Flatten assistant credentials attached to the signed-in account."""
    user_id = await find_user_id_by_username(db, username=actor_username)
    if user_id is None:
        return []

    now = datetime.now(timezone.utc)
    rows = await mcp_tokens.list_for_user(db, user_id=user_id)
    listed = []
    for row in rows:
        if row.revoked_at is not None:
            state = "revoked"
        elif mcp_tokens.is_live(row, at=now):
            state = "active"
        else:
            state = "expired"
        listed.append({
            "id": row.id, "name": row.client_name or row.client_id,
            "state": state, "issued_at": row.issued_at, "adopted": row.adopted,
        })
    return listed


async def render_security_settings(
    request: Request,
    username: str,
    *,
    db: AsyncSession,
    saved: Optional[str] = None,
    error: Optional[str] = None,
    issued_external_token: Optional[str] = None,
) -> HTMLResponse:
    """Render account controls without requiring a personal health record."""

    has_own_record = bool(getattr(request.state, "has_own_record", False))
    external_tokens: list[dict] = []
    if has_own_record:
        ownership = await resolve_legacy_ownership_context(
            db,
            actor_username=username,
        )
        external_tokens = await _external_token_rows(
            db,
            subject_id=ownership.subject_id,
        )

    cfg = get_web_config()
    federated_signin = cfg.oidc_enabled
    # The legacy password and second factor are installation credentials, not
    # generic per-account controls. In practice only the configured legacy
    # owner can hold such a session; keeping the explicit predicate makes a
    # synthetic recordless or stale account fail closed as well.
    local_signin_available = (
        not federated_signin and username == cfg.auth_username
    )
    twofa = (
        await twofa_service.get_state(db)
        if local_signin_available
        else twofa_service.TwoFAState()
    )
    twofa_uri = (
        twofa_service.provisioning_uri(twofa.secret, account=username)
        if local_signin_available and twofa.pending
        else ""
    )
    return templates.TemplateResponse(
        request,
        "settings/security.html",
        {
            "username": username,
            "saved": saved,
            "error": error,
            "has_own_record": has_own_record,
            "federated_signin": federated_signin,
            "local_signin_available": local_signin_available,
            "twofa": twofa,
            "twofa_secret_display": (
                twofa_service.format_secret(twofa.secret)
                if local_signin_available and twofa.pending
                else ""
            ),
            "twofa_uri": twofa_uri,
            "twofa_qr": twofa_service.qr_svg(twofa_uri) if twofa_uri else "",
            "mcp_connectors": await _connector_rows(
                db,
                actor_username=username,
            ),
            "external_tokens": external_tokens,
            # Passed only from the minting POST. The database stores only its
            # hash, and a redirect URL must never carry this capability.
            "issued_external_token": issued_external_token,
        },
    )


@router.get("/security", response_class=HTMLResponse)
async def security_settings_page(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    saved: Optional[str] = None,
    error: Optional[str] = None,
) -> HTMLResponse:
    return await render_security_settings(
        request,
        username,
        db=db,
        saved=saved,
        error=error,
    )


@router.post("/external-api")
async def issue_external_api_token(
    request: Request,
    label: str = Form(""),
    days: int = Form(90),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    """Mint a read-only credential and render its only plaintext copy."""

    from datetime import timedelta

    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    try:
        issued = await external_tokens.issue(
            db,
            owner_user_id=identity.access.principal.user_id,
            subject_id=identity.subject_id,
            label=label,
            lifetime=timedelta(days=days),
        )
    except external_tokens.ExternalApiTokenError:
        await db.rollback()
        return _redirect(
            "?error=external_api",
            destination=SETTINGS_SECURITY_PATH,
        )
    await db.commit()
    return await render_security_settings(
        request,
        username,
        db=db,
        saved="external_api",
        issued_external_token=issued.secret,
    )


@router.post("/external-api/{token_id}/revoke")
async def revoke_external_api_token(
    request: Request,
    token_id: uuid.UUID,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    del request
    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    try:
        await external_tokens.revoke(
            db,
            owner_user_id=identity.access.principal.user_id,
            token_id=token_id,
        )
    except external_tokens.ExternalApiTokenError:
        await db.rollback()
        return _redirect(
            "?error=external_api",
            destination=SETTINGS_SECURITY_PATH,
        )
    await db.commit()
    return _redirect(
        "?saved=external_api_revoked",
        destination=SETTINGS_SECURITY_PATH,
    )


@router.post("/2fa/start")
async def start_twofa(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    del request, username
    if get_web_config().oidc_enabled:
        raise HTTPException(status_code=404)
    if (await twofa_service.get_state(db)).enabled:
        return _redirect(destination=SETTINGS_SECURITY_PATH)
    await twofa_service.start_enrolment(db)
    await db.commit()
    return _redirect(destination=SETTINGS_SECURITY_PATH)


@router.post("/2fa/enable")
async def confirm_twofa(
    request: Request,
    code: str = Form(""),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("twofa_setup", limit=10, window=300)),
):
    if get_web_config().oidc_enabled:
        raise HTTPException(status_code=404)
    if not await twofa_service.confirm_enrolment(db, code):
        return await render_security_settings(
            request,
            username,
            db=db,
            error="twofa_bad_code",
        )
    await db.commit()
    return _redirect(
        "?saved=twofa",
        destination=SETTINGS_SECURITY_PATH,
    )


@router.post("/2fa/disable")
async def disable_twofa(
    request: Request,
    code: str = Form(""),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("twofa_setup", limit=10, window=300)),
):
    if get_web_config().oidc_enabled:
        raise HTTPException(status_code=404)
    if not await twofa_service.disable(db, code):
        return await render_security_settings(
            request,
            username,
            db=db,
            error="twofa_bad_code",
        )
    await db.commit()
    return _redirect(
        "?saved=twofa_off",
        destination=SETTINGS_SECURITY_PATH,
    )


@router.post("/password")
async def change_password(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    old_password: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
):
    from vitals.config import load_config
    from vitals.services.identity.bootstrap import bootstrap_legacy_owner
    from vitals.services.identity.credentials import bcrypt_cost, rotate_password_hash
    from vitals.utils.passwords import hash_password
    from web.authentication.legacy import authenticate
    from web.authentication.tokens import create_session, set_session_cookie

    cfg = get_web_config()
    if cfg.oidc_enabled:
        raise HTTPException(status_code=404)

    validation_error = None
    if not authenticate(cfg.auth_username, old_password):
        validation_error = "wrong_password"
    elif not new_password or len(new_password) < 8:
        validation_error = "password_too_short"
    elif new_password != new_password_confirm:
        validation_error = "password_mismatch"
    if validation_error is not None:
        return await render_security_settings(
            request,
            username,
            db=db,
            error=validation_error,
        )

    hashed = hash_password(
        new_password,
        minimum_rounds=bcrypt_cost(cfg.auth_password_hash),
    )

    # The compatibility login still reads the environment, while the durable
    # identity is now the fail-closed startup anchor. Update both as one logical
    # operation: bootstrap/rotation only flush, this HTTP boundary owns commit,
    # and a failed DB commit restores the old environment credential best-effort.
    bootstrap = await bootstrap_legacy_owner(
        db,
        username=cfg.auth_username,
        password_hash=cfg.auth_password_hash,
        timezone=load_config().timezone,
    )
    await rotate_password_hash(
        db,
        user_id=bootstrap.user_id,
        expected_current_hash=cfg.auth_password_hash,
        new_hash=hashed,
        actor_user_id=bootstrap.user_id,
    )

    key = "VITALS_AUTH_PASSWORD_HASH"
    previous_persisted_hash = read_key(key) or cfg.auth_password_hash
    token = create_session(cfg.auth_username)
    environment_written = False
    try:
        write_keys({key: hashed})
        environment_written = True
        os.environ[key] = hashed
        await db.commit()
    # Cancellation and process-shutdown exceptions also need compensation after
    # the file write. Re-raise every BaseException once the old credential has
    # been restored; this is not an error-swallowing boundary.
    except BaseException:
        try:
            await db.rollback()
        finally:
            os.environ[key] = cfg.auth_password_hash
            if environment_written:
                try:
                    write_keys({key: previous_persisted_hash})
                except Exception as compensation_error:
                    logger.critical(
                        "password rotation failed and the environment file could "
                        "not be restored; explicit credential reconciliation is "
                        "required"
                    )
                    raise RuntimeError(
                        "password rotation could not restore its persisted credential"
                    ) from compensation_error
        raise

    # Existing browser cookies remain compatibility credentials until PR-05;
    # this response merely gives the current browser the new versioned envelope.
    response = _redirect(
        "?saved=password",
        destination=SETTINGS_SECURITY_PATH,
    )
    set_session_cookie(response, token)
    return response


__all__ = [
    "_connector_rows", "_external_token_rows", "change_password", "confirm_twofa",
    "disable_twofa", "issue_external_api_token", "render_security_settings",
    "revoke_external_api_token", "router", "security_settings_page", "start_twofa",
]
