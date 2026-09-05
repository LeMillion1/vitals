"""Personal profile and language settings delivery routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.services.profile import health as health_profile_service
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
from web.deps import get_session, require_auth
from web.templating import templates

from .common import (
    SETTINGS_PROFILE_PATH,
    blank_if_none as _blank_if_none,
    number as _number,
    redirect as _redirect,
)

router = APIRouter()


async def render_profile_settings(
    request: Request,
    username: str,
    *,
    db: AsyncSession,
    saved: Optional[str] = None,
    error: Optional[str] = None,
) -> HTMLResponse:
    """Render only settings backed by this account's personal record."""

    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    projection = await health_profile_service.get_profile_projection(
        db,
        subject_id=ownership.subject_id,
    )
    profile = projection.profile
    return templates.TemplateResponse(
        request,
        "settings/profile.html",
        {
            "username": username,
            "saved": saved,
            "error": error,
            # An unset field is blank rather than somebody else's process
            # default. Nutrition targets keep their documented defaults.
            "height_cm": _blank_if_none(profile.height_cm),
            "sex": profile.sex or "",
            "user_age": _blank_if_none(profile.age),
            "timezone": projection.timezone or load_config().timezone,
            "user_program": profile.program or "",
            "user_goals": ", ".join(profile.goals),
            "nutrition_protein_target_g": _number(profile.protein_target_g),
            "nutrition_calories_min": str(profile.calories_min),
            "nutrition_calories_max": str(profile.calories_max),
        },
    )


@router.get("/profile", response_class=HTMLResponse)
async def profile_settings_page(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    saved: Optional[str] = None,
    error: Optional[str] = None,
) -> HTMLResponse:
    return await render_profile_settings(
        request,
        username,
        db=db,
        saved=saved,
        error=error,
    )


@router.post("/profile")
async def save_profile(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    height_cm: str = Form(""),
    sex: str = Form(""),
    user_age: str = Form(""),
    timezone: str = Form(""),
    user_program: str = Form(""),
    user_goals: str = Form(""),
    nutrition_protein_target_g: str = Form(""),
    nutrition_calories_min: str = Form(""),
    nutrition_calories_max: str = Form(""),
):
    """Save the profile to this person's record rather than to ``.env``.

    Every field here used to be written into the installation's environment,
    which describes nobody. The old keys remain untouched as the bounded
    startup-adoption source for installations that have not upgraded yet.
    """

    del request
    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    await health_profile_service.set_profile(
        db,
        subject_id=identity.subject_id,
        raw={
            "height_cm": height_cm,
            "sex": sex,
            "age": user_age,
            "program": user_program,
            "goals": user_goals,
            "protein_target_g": nutrition_protein_target_g,
            "calories_min": nutrition_calories_min,
            "calories_max": nutrition_calories_max,
        },
    )
    await health_profile_service.set_subject_timezone_if_valid(
        db,
        subject_id=identity.subject_id,
        timezone=timezone.strip(),
    )
    await db.commit()
    return _redirect(
        "?saved=profile",
        destination=SETTINGS_PROFILE_PATH,
    )


async def _page(
    request: Request,
    username: str,
    *,
    db: AsyncSession,
    redis: Optional[Redis] = None,
    saved: Optional[str] = None,
    error: Optional[str] = None,
    adjusted: Optional[str] = None,
    deferred: Optional[str] = None,
    issued_external_token: Optional[str] = None,
) -> HTMLResponse:
    """Compatibility dispatcher for the former all-settings projection.

    Tests and extensions historically imported ``web.routers.settings._page``.
    Keep that seam while ensuring a direct render now loads only the focused
    page that owns the supplied outcome. A marker-free call means the new
    Settings index and therefore never resolves a personal record.
    """

    security_saved = {
        "external_api",
        "external_api_revoked",
        "mcp_tokens",
        "password",
        "twofa",
        "twofa_off",
    }
    security_errors = {
        "external_api",
        "mcp_tokens",
        "password_mismatch",
        "password_too_short",
        "twofa_bad_code",
        "wrong_password",
    }
    if issued_external_token or saved in security_saved or error in security_errors:
        from .security import render_security_settings

        return await render_security_settings(
            request,
            username,
            db=db,
            saved=saved,
            error=error,
            issued_external_token=issued_external_token,
        )
    if saved in {"hevy", "garmin"} or error in {
        "garmin",
        "hevy",
        "no_credential_key",
    }:
        from .providers import render_integrations_settings

        return await render_integrations_settings(
            request,
            username,
            db=db,
            redis=redis,
            saved=saved,
            error=error,
        )
    if saved == "proactive" or adjusted is not None or deferred is not None:
        from .preferences import render_brief_settings

        return await render_brief_settings(
            request,
            username,
            db=db,
            saved=saved,
            error=error,
            adjusted=adjusted,
            deferred=deferred,
        )
    if saved in {"language", "profile"}:
        return await render_profile_settings(
            request,
            username,
            db=db,
            saved=saved,
            error=error,
        )

    from .index import render_settings_index

    del redis
    return await render_settings_index(request, username=username)


# Compatibility aliases for tests/extensions that imported these helpers from
# the former aggregate projection module.
from .security import _connector_rows, _external_token_rows  # noqa: E402


__all__ = [
    "_connector_rows",
    "_external_token_rows",
    "_page",
    "profile_settings_page",
    "render_profile_settings",
    "router",
    "save_profile",
]
