"""Personal access tokens, two-factor authentication, and password routes."""

from __future__ import annotations

import logging
import os
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.authentication import legacy_two_factor as twofa_service
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
from web.config import get_web_config
from web.deps import get_redis, get_session, require_auth
from web.ratelimit import rate_limit
from web.services.env_writer import read_key, write_keys

from .common import redirect as _redirect
from .profile import _page

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/external-api")
async def issue_external_api_token(
    request: Request,
    label: str = Form(""),
    days: int = Form(90),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    """Mint a read-only credential for this record.

    The secret comes back through the redirect and is rendered once. It is not
    stored, so there is no second chance to show it and no query that could —
    which is the point rather than an inconvenience.
    """

    from datetime import timedelta

    from vitals.services.external_api import tokens as external_tokens

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
        return _redirect("?error=external_api")
    await db.commit()
    # Rendered straight from the POST rather than redirected with the secret in
    # the query string, for the reason ``consents.issue_invitation`` records: a
    # URL ends up in browser history, in the access log and in the next page's
    # referrer, and a bearer token is a capability. This body is the only copy
    # that leaves here, and nothing can show it again because only its hash was
    # stored.
    return await _page(
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
    from vitals.services.external_api import tokens as external_tokens

    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    try:
        await external_tokens.revoke(
            db,
            owner_user_id=identity.access.principal.user_id,
            token_id=token_id,
        )
    except external_tokens.ExternalApiTokenError:
        await db.rollback()
        return _redirect("?error=external_api")
    await db.commit()
    return _redirect("?saved=external_api_revoked")



@router.post("/2fa/start")
async def start_twofa(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    if get_web_config().oidc_enabled:
        # The provider owns second factors now — enrolling one here would be a
        # second thing to keep, rotate and recover, competing with the one that
        # already does it properly.
        raise HTTPException(status_code=404)
    """Mint a secret and show it. 2FA is NOT on yet — see ``confirm_twofa``."""
    if (await twofa_service.get_state(db)).enabled:
        return _redirect()
    await twofa_service.start_enrolment(db)
    await db.commit()
    return _redirect()


@router.post("/2fa/enable")
async def confirm_twofa(
    request: Request,
    code: str = Form(""),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    _rl: None = Depends(rate_limit("twofa_setup", limit=10, window=300)),
):
    if get_web_config().oidc_enabled:
        # The provider owns second factors now — enrolling one here would be a
        # second thing to keep, rotate and recover, competing with the one that
        # already does it properly.
        raise HTTPException(status_code=404)
    """Finish enrolment: a correct code proves the secret reached the phone."""
    if not await twofa_service.confirm_enrolment(db, code):
        return await _page(request, username, db=db, redis=redis, error="twofa_bad_code")
    await db.commit()
    return _redirect("?saved=twofa")


@router.post("/2fa/disable")
async def disable_twofa(
    request: Request,
    code: str = Form(""),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    _rl: None = Depends(rate_limit("twofa_setup", limit=10, window=300)),
):
    if get_web_config().oidc_enabled:
        # The provider owns second factors now — enrolling one here would be a
        # second thing to keep, rotate and recover, competing with the one that
        # already does it properly.
        raise HTTPException(status_code=404)
    """Switch 2FA off (needs a current code), or drop a half-finished enrolment
    (needs nothing — it never granted anything)."""
    if not await twofa_service.disable(db, code):
        return await _page(request, username, db=db, redis=redis, error="twofa_bad_code")
    await db.commit()
    return _redirect("?saved=twofa_off")



@router.post("/password")
async def change_password(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
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
    from web.config import get_web_config

    cfg = get_web_config()
    if cfg.oidc_enabled:
        # Password authentication no longer exists on this installation. Keep
        # the hidden form endpoint aligned with the login and 2FA endpoints.
        raise HTTPException(status_code=404)

    if not authenticate(cfg.auth_username, old_password):
        return await _page(request, username, db=db, redis=redis, error="wrong_password")

    if not new_password or len(new_password) < 8:
        return await _page(request, username, db=db, redis=redis, error="password_too_short")

    if new_password != new_password_confirm:
        return await _page(request, username, db=db, redis=redis, error="password_mismatch")

    hashed = hash_password(
        new_password,
        minimum_rounds=bcrypt_cost(cfg.auth_password_hash),
    )

    # The compatibility login still reads the environment, while the durable
    # identity is now the fail-closed startup anchor.  Update both as one logical
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
    response = _redirect("?saved=password")
    set_session_cookie(response, token)
    return response


# ── Data portability (backup / restore / LLM export) ──────────────────────────
