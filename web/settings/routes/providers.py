"""Personal provider credentials, connections, and Garmin weight controls."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.garmin_weight import jobs as garmin_weight_jobs
from vitals.services.garmin_weight import outbox as garmin_weight_outbox
from vitals.services.garmin_weight import settings as garmin_weight_settings
from vitals.services.credentials import providers, vault
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from web.deps import get_redis, get_session, require_auth
from web.ratelimit import rate_limit
from web.settings.forms import is_secret_sentinel
from web.templating import templates

from .common import redirect as _redirect

logger = logging.getLogger(__name__)
router = APIRouter()

async def _subject_garmin_account(db: AsyncSession, username: str):
    """This account's own Garmin connection, whatever it is signed in as.

    Replaces reading ``VITALS_GARMIN_EMAIL`` off the environment, which is the
    installation's one watch: on a shared installation every patient's settings
    card showed the operator's address in the email box and "connected" beside
    it, and the outbound-weight opt-in they were offered would have pushed their
    weight to somebody else's Garmin.
    """

    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    return await providers.resolve_garmin_account(
        db, subject_id=identity.subject_id
    )


async def _garmin_weight_control(
    request: Request,
    *,
    db: AsyncSession,
    username: str,
    action: Optional[str] = None,
) -> HTMLResponse:
    """Render the self-contained HTMX control after a live action."""
    account = await _subject_garmin_account(db, username)
    export_context = await garmin_weight_outbox.resolve_legacy_export_context(
        db,
        actor_username=username,
    )
    prepared_export = await garmin_weight_outbox.prepare_scoped_export(
        db,
        context=export_context,
        historical=True,
    )
    return templates.TemplateResponse(
        request,
        "partials/garmin_weight_export.html",
        {
            "garmin_credentials_configured": bool(account and account.configured),
            "garmin_weight_export": await garmin_weight_jobs.get_status_scoped(
                db,
                prepared=prepared_export,
            ),
            "garmin_weight_action": action,
        },
    )


# ── Helpers ───────────────────────────────────────────────────────────────────



@router.post("/connectors/{connector_id}/revoke")
async def revoke_connector(
    request: Request,
    connector_id: uuid.UUID,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    """Disconnect one assistant, and nothing else.

    The point of the whole ``jti`` mechanism: before it, withdrawing an issued
    connector token meant rotating the signing secret, which also invalidates
    every web session in the installation.
    """

    from vitals.services.authentication import mcp_tokens
    from vitals.services.identity_service import find_user_id_by_username

    user_id = await find_user_id_by_username(
        db,
        username=username,
    )
    if user_id is None:
        return _redirect("?error=mcp_tokens")
    try:
        await mcp_tokens.revoke(db, user_id=user_id, jti=connector_id)
    except mcp_tokens.McpTokenError:
        await db.rollback()
        return _redirect("?error=mcp_tokens")
    await db.commit()
    return _redirect("?saved=mcp_tokens")



@router.post("/hevy")
async def save_hevy(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    hevy_api_key: str = Form(""),
):
    """Store this person's Hevy key against their own connection.

    It went into ``VITALS_HEVY_API_KEY`` — one workout account for the whole
    installation. The blank/sentinel field still means "keep what is there",
    which is what makes it safe to submit the card without retyping a secret.
    """

    submitted = hevy_api_key.strip()
    if not submitted or is_secret_sentinel(submitted):
        return _redirect("?saved=hevy")
    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    try:
        await providers.set_hevy_credentials(
            db, subject_id=identity.subject_id, api_key=submitted
        )
    except vault.CredentialVaultUnavailable:
        await db.rollback()
        logger.warning("Hevy credential not stored: no installation vault key")
        return _redirect("?error=no_credential_key")
    except providers.ProviderCredentialsError:
        await db.rollback()
        logger.warning("Hevy credential not stored", exc_info=True)
        return _redirect("?error=hevy")
    await db.commit()
    return _redirect("?saved=hevy")


@router.post("/garmin")
async def save_garmin(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    garmin_email: str = Form(""),
    garmin_password: str = Form(""),
):
    """Store this person's Garmin sign-in against their own connection.

    It went into ``VITALS_GARMIN_EMAIL``/``_PASSWORD`` and then straight into
    ``os.environ`` so a new client would see it — one watch for the whole
    process, which is the reason four scheduled jobs still could not be run per
    subject.

    Blank and sentinel fields keep whatever is stored, so the card can be
    submitted without retyping a password. That merge now happens against the
    resolved account rather than against the environment file, which for a
    second patient held somebody else's address.
    """

    account = await _subject_garmin_account(db, username)
    stored_email = account.config.garmin_email if account else ""
    stored_password = account.config.garmin_password if account else ""
    submitted_email = garmin_email.strip()
    submitted_password = garmin_password.strip()
    effective_email = submitted_email or stored_email
    effective_password = (
        submitted_password
        if submitted_password and not is_secret_sentinel(submitted_password)
        else stored_password
    )
    if not (effective_email and effective_password):
        return _redirect("?error=garmin")

    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    try:
        await providers.set_garmin_credentials(
            db,
            subject_id=identity.subject_id,
            email=effective_email,
            password=effective_password,
        )
    except vault.CredentialVaultUnavailable:
        await db.rollback()
        logger.warning("Garmin credential not stored: no installation vault key")
        return _redirect("?error=no_credential_key")
    except providers.ProviderCredentialsError:
        await db.rollback()
        logger.warning("Garmin credential not stored", exc_info=True)
        return _redirect("?error=garmin")
    await db.commit()
    return _redirect("?saved=garmin")


@router.post("/garmin/weight-toggle", response_class=HTMLResponse)
async def toggle_garmin_weight_export(
    request: Request,
    enabled: bool = Form(False),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("garmin_weight_toggle", limit=20, window=60)),
):
    """Apply the outbound-weight opt-in immediately, without a page reload."""
    account = await _subject_garmin_account(db, username)
    if enabled and not (account and account.configured):
        # Never persist a fail-open opt-in. The scheduled job also guards this,
        # but the settings boundary should make the rejected state explicit.
        #
        # Asked about *this* subject's account now. Reading the environment
        # meant a patient with no Garmin of their own passed the check on the
        # strength of the operator's, and their weight would have been pushed to
        # somebody else's watch.
        return await _garmin_weight_control(
            request,
            db=db,
            username=username,
            action="credentials_required",
        )

    try:
        export_context = await garmin_weight_outbox.resolve_legacy_export_context(
            db,
            actor_username=username,
        )
        prepared_export = await garmin_weight_outbox.prepare_scoped_export(
            db,
            context=export_context,
            historical=not enabled,
        )
        await garmin_weight_settings.set_enabled_scoped(
            db,
            enabled,
            prepared=prepared_export,
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — return a safe localized fragment.
        await db.rollback()
        logger.exception("Could not update Garmin weight export opt-in")
        return await _garmin_weight_control(
            request,
            db=db,
            username=username,
            action="error",
        )

    return await _garmin_weight_control(
        request,
        db=db,
        username=username,
        action="toggle_enabled" if enabled else "toggle_disabled",
    )


@router.post("/garmin/weight/send-now", response_class=HTMLResponse)
async def send_garmin_weight_now(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    _rl: None = Depends(rate_limit("garmin_weight_send_now", limit=6, window=3600)),
):
    """Run one explicit safe reconciliation and return the refreshed control."""
    try:
        export_context = await garmin_weight_outbox.resolve_legacy_export_context(
            db,
            actor_username=username,
        )
        prepared_export = await garmin_weight_outbox.prepare_scoped_export(
            db,
            context=export_context,
        )
        result = await garmin_weight_jobs.send_now_scoped(
            db,
            prepared=prepared_export,
            redis=redis,
        )
        await db.commit()
        action = str(result.get("status") or "done")
    except Exception:  # noqa: BLE001 — upstream details belong in logs/outbox.
        await db.rollback()
        logger.exception("Could not run Garmin weight reconciliation")
        action = "error"
    return await _garmin_weight_control(
        request,
        db=db,
        username=username,
        action=action,
    )
