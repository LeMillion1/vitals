"""The shallow, role-aware Settings index."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from vitals.services.modules.registry import MODULE_REGISTRY
from web.config import get_web_config
from web.deps import require_auth
from web.templating import templates

from .common import (
    SETTINGS_BRIEF_PATH,
    SETTINGS_INTEGRATIONS_PATH,
    SETTINGS_PROFILE_PATH,
    SETTINGS_SECURITY_PATH,
    redirect as _redirect,
)

router = APIRouter()

_SAVED_DESTINATIONS = {
    "external_api": SETTINGS_SECURITY_PATH,
    "external_api_revoked": SETTINGS_SECURITY_PATH,
    "garmin": SETTINGS_INTEGRATIONS_PATH,
    "hevy": SETTINGS_INTEGRATIONS_PATH,
    "language": SETTINGS_PROFILE_PATH,
    "mcp_tokens": SETTINGS_SECURITY_PATH,
    "password": SETTINGS_SECURITY_PATH,
    "profile": SETTINGS_PROFILE_PATH,
    "proactive": SETTINGS_BRIEF_PATH,
    "twofa": SETTINGS_SECURITY_PATH,
    "twofa_off": SETTINGS_SECURITY_PATH,
}
_ERROR_DESTINATIONS = {
    "external_api": SETTINGS_SECURITY_PATH,
    "garmin": SETTINGS_INTEGRATIONS_PATH,
    "hevy": SETTINGS_INTEGRATIONS_PATH,
    "mcp_tokens": SETTINGS_SECURITY_PATH,
    "no_credential_key": SETTINGS_INTEGRATIONS_PATH,
    "password_mismatch": SETTINGS_SECURITY_PATH,
    "password_too_short": SETTINGS_SECURITY_PATH,
    "twofa_bad_code": SETTINGS_SECURITY_PATH,
    "wrong_password": SETTINGS_SECURITY_PATH,
}


def _legacy_deep_link(
    *,
    saved: Optional[str],
    error: Optional[str],
    adjusted: Optional[str],
    deferred: Optional[str],
):
    """Move known aggregate-page outcomes to their focused owner.

    Only server-known presentation markers survive. In particular, arbitrary
    query parameters and bearer material can never be reflected into a target.
    """

    destination = _ERROR_DESTINATIONS.get(error or "")
    if destination is None:
        destination = _SAVED_DESTINATIONS.get(saved or "")
    if destination is None and (adjusted == "1" or deferred in {"1", "reload"}):
        destination = SETTINGS_BRIEF_PATH
    if destination is None:
        return None

    query: list[tuple[str, str]] = []
    if _SAVED_DESTINATIONS.get(saved or "") == destination:
        query.append(("saved", saved or ""))
    if _ERROR_DESTINATIONS.get(error or "") == destination:
        query.append(("error", error or ""))
    if destination == SETTINGS_BRIEF_PATH:
        if adjusted == "1":
            query.append(("adjusted", adjusted))
        if deferred in {"1", "reload"}:
            query.append(("deferred", deferred))
    return _redirect(
        f"?{urlencode(query)}" if query else "",
        destination=destination,
    )


def _index_context(request: Request, *, username: str) -> dict[str, object]:
    """Build summaries from request chrome without opening a personal record.

    A professional or platform operator commonly owns no health record.  The
    index therefore uses only state resolved by the global chrome dependencies;
    focused personal pages perform their own explicit ownership resolution.
    """

    enabled = getattr(request.state, "enabled_modules", {}) or {}
    return {
        "username": username,
        "has_own_record": bool(
            getattr(request.state, "has_own_record", False)
        ),
        "is_professional": bool(
            getattr(request.state, "is_professional", False)
        ),
        "is_platform_admin": bool(
            getattr(request.state, "is_platform_admin", False)
        ),
        "enabled_modules": enabled,
        "enabled_module_count": sum(
            bool(enabled.get(key, False)) for key in MODULE_REGISTRY
        ),
        "enabled_module_total": len(MODULE_REGISTRY),
        "federated_signin": get_web_config().oidc_enabled,
    }


async def render_settings_index(
    request: Request,
    *,
    username: str,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "settings/index.html",
        _index_context(request, username=username),
    )


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    username: str = Depends(require_auth),
    saved: Optional[str] = None,
    error: Optional[str] = None,
    adjusted: Optional[str] = None,
    deferred: Optional[str] = None,
) -> HTMLResponse:
    if legacy := _legacy_deep_link(
        saved=saved,
        error=error,
        adjusted=adjusted,
        deferred=deferred,
    ):
        return legacy
    return await render_settings_index(request, username=username)


__all__ = ["render_settings_index", "router", "settings_page"]
