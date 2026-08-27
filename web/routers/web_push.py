"""Account-scoped browser notification setup, with no health-data response."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.credentials import vault
from vitals.services.notifications import web_push_config
from vitals.services.notifications import web_push_subscriptions as subscriptions
from web.care_context import principal_user_id
from web.deps import get_session, require_auth
from web.ratelimit import rate_limit
from web.request_bodies import read_bounded_json_object

MAX_JSON_BODY_BYTES = 8192

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/account/notifications",
    tags=["account-notifications"],
    dependencies=[Depends(require_auth)],
)


async def _json_object(request: Request) -> dict[str, Any]:
    return await read_bounded_json_object(
        request,
        max_bytes=MAX_JSON_BODY_BYTES,
    )


def _browser_configuration() -> web_push_config.WebPushConfig | None:
    try:
        config = web_push_config.load_config()
    except web_push_config.WebPushConfigurationError:
        logger.error("Web Push is enabled but its VAPID configuration is invalid")
        return None
    if config is None or not vault.is_available():
        return None
    return config


def _response(content: dict[str, Any]) -> JSONResponse:
    return JSONResponse(content, headers={"Cache-Control": "no-store"})


def _require_shape(value: dict[str, Any], fields: set[str]) -> None:
    if set(value) != fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)


@router.get("/configuration")
async def configuration() -> JSONResponse:
    """Expose only the public VAPID key, and only to a signed-in account."""

    config = _browser_configuration()
    if config is None:
        return _response({"available": False})
    return _response(
        {"available": True, "applicationServerKey": config.public_key}
    )


@router.post("/status")
async def current_device_status(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _limit: None = Depends(rate_limit("web_push_status", limit=60, window=60)),
) -> JSONResponse:
    body = await _json_object(request)
    _require_shape(body, {"endpoint"})
    try:
        active = await subscriptions.endpoint_is_active(
            db,
            user_id=await principal_user_id(request, db),
            endpoint=body.get("endpoint"),
        )
    except subscriptions.InvalidWebPushSubscription:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from None
    return _response({"enabled": active})


@router.post("/subscription")
async def register_current_device(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _limit: None = Depends(rate_limit("web_push_register", limit=20, window=60)),
) -> JSONResponse:
    if _browser_configuration() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="notifications_unavailable",
        )
    body = await _json_object(request)
    _require_shape(body, {"endpoint", "keys"})
    keys = body.get("keys")
    if not isinstance(keys, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    _require_shape(keys, {"p256dh", "auth"})
    try:
        await subscriptions.register(
            db,
            user_id=await principal_user_id(request, db),
            endpoint=body.get("endpoint"),
            p256dh=keys.get("p256dh"),
            auth=keys.get("auth"),
        )
    except subscriptions.SubscriptionBelongsToAnotherAccount:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="device_linked_elsewhere",
        ) from None
    except subscriptions.TooManyWebPushSubscriptions:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="device_limit_reached",
        ) from None
    except (
        subscriptions.InvalidWebPushSubscription,
        vault.CredentialVaultValidationError,
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from None
    except vault.CredentialVaultUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="notifications_unavailable",
        ) from None
    except IntegrityError:
        # Two accounts can race before either new endpoint row is visible. The
        # database unique is the final owner boundary; a loser gets the same
        # generic conflict as an already-bound shared browser, never a 500 with
        # constraint details and never an endpoint takeover.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="device_linked_elsewhere",
        ) from None
    await db.commit()
    return _response({"enabled": True})


@router.post("/subscription/revoke")
async def revoke_current_device(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _limit: None = Depends(rate_limit("web_push_revoke", limit=20, window=60)),
) -> JSONResponse:
    body = await _json_object(request)
    _require_shape(body, {"endpoint"})
    try:
        await subscriptions.revoke_endpoint(
            db,
            user_id=await principal_user_id(request, db),
            endpoint=body.get("endpoint"),
        )
    except subscriptions.InvalidWebPushSubscription:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from None
    await db.commit()
    # Idempotent and deliberately does not disclose whether a server row existed.
    return _response({"enabled": False})


__all__ = ["MAX_JSON_BODY_BYTES", "router"]
