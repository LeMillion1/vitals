"""Installation-level AI gateway and MCP authority settings."""

from __future__ import annotations

from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts

import logging
import os
import re
import secrets
import uuid
from datetime import date
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.platform import ai_control as platform_ai_control
from vitals.services.platform import authorization as platform_authorization
from web.deps import get_session, require_auth, require_recent_auth
from web.services.env_writer import read_key, write_keys
from web.settings.forms import is_secret_sentinel
from web.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()

_AI_ENV_DEFAULTS = {
    "VITALS_OPENROUTER_API_KEY": "",
    "VITALS_OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "VITALS_LLM_MODEL_DIGEST": "anthropic/claude-sonnet-4.6",
    "VITALS_LLM_MODEL_PARSER": "google/gemini-2.5-flash",
    "VITALS_LLM_MODEL_BRIEF": "",
}
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_MODEL_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,189}"
)


def _effective_ai_value(key: str) -> str:
    """Read one AI setting without ever returning a secret to a response."""

    persisted = read_key(key).strip()
    if persisted:
        return persisted
    runtime = os.getenv(key, "").strip()
    return runtime or _AI_ENV_DEFAULTS[key]


def _validate_openrouter_credential(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if len(value) > 2048 or any(
        char.isspace() or not char.isprintable() for char in value
    ):
        raise ValueError("OpenRouter credential is invalid")
    return value


def _canonical_openrouter_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "openrouter.ai"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path != "/api/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("OpenRouter base URL is not approved")
    return _OPENROUTER_BASE_URL


def _validate_openrouter_model(value: str, *, allow_empty: bool = False) -> str:
    value = value.strip()
    if not value and allow_empty:
        return ""
    if not _OPENROUTER_MODEL_RE.fullmatch(value):
        raise ValueError("OpenRouter model slug is invalid")
    return value


def _set_runtime_values(updates: dict[str, str]) -> None:
    for key, value in updates.items():
        os.environ[key] = value


async def _commit_ai_control_change(
    db: AsyncSession,
    *,
    updates: dict[str, str],
) -> None:
    """Commit a DB/environment change with fail-closed ambiguity handling."""

    previous_persisted = {key: read_key(key) for key in updates}
    previous_runtime = {
        key: (key in os.environ, os.environ.get(key, "")) for key in updates
    }
    environment_written = False
    try:
        if updates:
            write_keys(updates)
            environment_written = True
            _set_runtime_values(updates)
        await db.commit()
    except BaseException as commit_error:
        compensation_error: Exception | None = None
        if environment_written:
            safe_values = dict(previous_persisted)
            safe_values["VITALS_OPENROUTER_API_KEY"] = ""
            os.environ["VITALS_OPENROUTER_API_KEY"] = ""
            try:
                write_keys(safe_values)
                for key, (was_present, value) in previous_runtime.items():
                    if key == "VITALS_OPENROUTER_API_KEY":
                        continue
                    if was_present:
                        os.environ[key] = value
                    else:
                        os.environ.pop(key, None)
            except Exception as exc:
                compensation_error = exc
        await db.rollback()
        if compensation_error is not None:
            logger.critical(
                "platform AI configuration failed and its environment values "
                "could not be restored; explicit reconciliation is required"
            )
            raise RuntimeError(
                "platform AI configuration could not restore its environment"
            ) from compensation_error
        if environment_written and isinstance(commit_error, Exception):
            logger.critical(
                "platform AI database commit outcome is ambiguous; the gateway "
                "credential was cleared and explicit reconciliation is required"
            )
            raise RuntimeError(
                "platform AI commit outcome is ambiguous; credential cleared"
            ) from None
        raise


def _platform_ai_redirect(
    *,
    saved: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    query = f"?saved={saved}" if saved else f"?error={error}" if error else ""
    return RedirectResponse(
        url=f"/settings/platform/ai{query}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _prepare_platform_admin_or_403(
    db: AsyncSession,
    *,
    username: str,
) -> platform_authorization.PreparedPlatformAdmin:
    try:
        return await platform_authorization.prepare_platform_admin(
            db,
            actor_username=username,
        )
    except platform_authorization.PlatformAdminAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        ) from exc


@router.get("/platform", response_class=HTMLResponse)
async def platform_settings_page(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    saved: Optional[str] = None,
):
    """Render installation controls outside every personal record."""

    await _prepare_platform_admin_or_403(db, username=username)
    return templates.TemplateResponse(
        request,
        "settings/platform.html",
        {
            "username": username,
            "saved": saved,
            "mcp_client_id": read_key("VITALS_MCP_CLIENT_ID")
            or "vitals-claude-connector",
            "mcp_client_secret_set": bool(read_key("VITALS_MCP_CLIENT_SECRET")),
        },
    )


@router.get("/platform/ai", response_class=HTMLResponse)
async def platform_ai_page(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    saved: Optional[str] = None,
    error: Optional[str] = None,
):
    prepared_admin = await _prepare_platform_admin_or_403(db, username=username)
    snapshot = await platform_ai_control.get_platform_ai_control_snapshot(
        db,
        prepared=prepared_admin,
    )
    return templates.TemplateResponse(
        request,
        "settings/platform_ai.html",
        {
            "username": username,
            "saved": saved,
            "error": error,
            "gateway": snapshot.gateway,
            "eligible_subject_ids": snapshot.eligible_subject_ids,
            "platform_periods": snapshot.platform_periods,
            "subject_periods": snapshot.subject_periods,
            "openrouter_api_key_set": bool(
                _effective_ai_value("VITALS_OPENROUTER_API_KEY")
            ),
            "openrouter_base_url": _effective_ai_value(
                "VITALS_OPENROUTER_BASE_URL"
            ),
            "llm_model_digest": _effective_ai_value("VITALS_LLM_MODEL_DIGEST"),
            "llm_model_parser": _effective_ai_value("VITALS_LLM_MODEL_PARSER"),
            "llm_model_brief": _effective_ai_value("VITALS_LLM_MODEL_BRIEF"),
        },
    )


@router.post("/ai")
@router.post("/platform/ai/configuration")
async def save_ai(
    request: Request,
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    openrouter_api_key: str = Form(""),
    openrouter_base_url: str = Form(""),
    llm_model_digest: str = Form(""),
    llm_model_parser: str = Form(""),
    llm_model_brief: str = Form(""),
):
    del request
    prepared_admin = await _prepare_platform_admin_or_403(db, username=username)
    current_values = {key: _effective_ai_value(key) for key in _AI_ENV_DEFAULTS}
    submitted_key = openrouter_api_key.strip()
    try:
        effective_key = _validate_openrouter_credential(
            submitted_key
            if submitted_key and not is_secret_sentinel(submitted_key)
            else current_values["VITALS_OPENROUTER_API_KEY"]
        )
        submitted_values = {
            "VITALS_OPENROUTER_BASE_URL": _canonical_openrouter_base_url(
                openrouter_base_url.strip()
                or current_values["VITALS_OPENROUTER_BASE_URL"]
            ),
            "VITALS_LLM_MODEL_DIGEST": _validate_openrouter_model(
                llm_model_digest.strip()
                or current_values["VITALS_LLM_MODEL_DIGEST"]
            ),
            "VITALS_LLM_MODEL_PARSER": _validate_openrouter_model(
                llm_model_parser.strip()
                or current_values["VITALS_LLM_MODEL_PARSER"]
            ),
            "VITALS_LLM_MODEL_BRIEF": _validate_openrouter_model(
                llm_model_brief,
                allow_empty=True,
            ),
        }
    except ValueError:
        await db.rollback()
        return _platform_ai_redirect(error="configuration_invalid")

    updates: dict[str, str] = {}
    changed_fields: set[str] = set()
    if (
        submitted_key
        and not is_secret_sentinel(submitted_key)
        and not secrets.compare_digest(
            effective_key,
            current_values["VITALS_OPENROUTER_API_KEY"],
        )
    ):
        updates["VITALS_OPENROUTER_API_KEY"] = effective_key
        changed_fields.add("credential_ref")
    field_names = {
        "VITALS_OPENROUTER_BASE_URL": "base_url",
        "VITALS_LLM_MODEL_DIGEST": "digest_model",
        "VITALS_LLM_MODEL_PARSER": "parser_model",
        "VITALS_LLM_MODEL_BRIEF": "brief_model",
    }
    for key, value in submitted_values.items():
        if value != current_values[key]:
            updates[key] = value
            changed_fields.add(field_names[key])

    try:
        transition = await platform_ai_control.apply_gateway_configuration(
            db,
            prepared=prepared_admin,
            configuration_changed=bool(changed_fields),
            credential_available=bool(effective_key),
            desired_enabled=None,
            changed_fields=frozenset(changed_fields),
        )
        if (
            updates
            or transition.action
            is not platform_ai_control.GatewayTransitionAction.NO_CHANGE
        ):
            await _commit_ai_control_change(db, updates=updates)
    except (
        ValueError,
        platform_ai_control.PlatformAIControlError,
        platform_authorization.PlatformAdminValidationError,
    ):
        await db.rollback()
        return _platform_ai_redirect(error="configuration_invalid")
    return _platform_ai_redirect(saved="ai")


@router.post("/platform/ai/enable")
async def enable_platform_ai(
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    prepared_admin = await _prepare_platform_admin_or_403(db, username=username)
    try:
        credential = _validate_openrouter_credential(
            _effective_ai_value("VITALS_OPENROUTER_API_KEY")
        )
        _canonical_openrouter_base_url(
            _effective_ai_value("VITALS_OPENROUTER_BASE_URL")
        )
        _validate_openrouter_model(_effective_ai_value("VITALS_LLM_MODEL_DIGEST"))
        _validate_openrouter_model(_effective_ai_value("VITALS_LLM_MODEL_PARSER"))
        _validate_openrouter_model(
            _effective_ai_value("VITALS_LLM_MODEL_BRIEF"),
            allow_empty=True,
        )
        transition = await platform_ai_control.apply_gateway_configuration(
            db,
            prepared=prepared_admin,
            configuration_changed=False,
            credential_available=bool(credential),
            desired_enabled=True,
        )
        if (
            transition.action
            is not platform_ai_control.GatewayTransitionAction.NO_CHANGE
        ):
            await db.commit()
    except (ValueError, platform_ai_control.PlatformAIControlError):
        await db.rollback()
        error = (
            "credential_missing"
            if not _effective_ai_value("VITALS_OPENROUTER_API_KEY")
            else "configuration_invalid"
        )
        return _platform_ai_redirect(error=error)
    return _platform_ai_redirect(saved="enabled")


@router.post("/platform/ai/disable")
async def disable_platform_ai(
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    prepared_admin = await _prepare_platform_admin_or_403(db, username=username)
    try:
        transition = await platform_ai_control.apply_gateway_configuration(
            db,
            prepared=prepared_admin,
            configuration_changed=False,
            credential_available=bool(
                _effective_ai_value("VITALS_OPENROUTER_API_KEY")
            ),
            desired_enabled=False,
        )
        if (
            transition.action
            is not platform_ai_control.GatewayTransitionAction.NO_CHANGE
        ):
            await db.commit()
    except platform_ai_control.PlatformAIControlError:
        await db.rollback()
        return _platform_ai_redirect(error="gateway_invalid")
    return _platform_ai_redirect(saved="disabled")


@router.post("/platform/ai/quota")
async def configure_platform_ai_quota(
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    subject_id: str = Form(...),
    period_start: date = Form(...),
    period_end: date = Form(...),
    platform_cost_limit_microunits: int = Form(...),
    platform_unit_limit: int = Form(...),
    subject_cost_limit_microunits: int = Form(...),
    subject_unit_limit: int = Form(...),
):
    prepared_admin = await _prepare_platform_admin_or_403(db, username=username)
    try:
        parsed_subject_id = uuid.UUID(subject_id)
        result = await platform_ai_control.configure_aligned_quota_period(
            db,
            prepared=prepared_admin,
            subject_id=parsed_subject_id,
            period_start=period_start,
            period_end=period_end,
            platform_cost_limit_microunits=platform_cost_limit_microunits,
            platform_unit_limit=platform_unit_limit,
            subject_cost_limit_microunits=subject_cost_limit_microunits,
            subject_unit_limit=subject_unit_limit,
        )
        if result.changed:
            await db.commit()
    except (
        TypeError,
        ValueError,
        platform_ai_control.PlatformAIControlError,
        ai_gateway_service_contracts.AIGatewayError,
    ):
        await db.rollback()
        return _platform_ai_redirect(error="quota_invalid")
    return _platform_ai_redirect(saved="quota")


@router.post("/mcp")
@router.post("/platform/mcp")
async def save_mcp(
    request: Request,
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    mcp_client_id: str = Form("vitals-claude-connector"),
    mcp_client_secret: str = Form(""),
):
    """Update installation OAuth authority outside every personal record."""

    del request
    from vitals.services.authentication import mcp_tokens

    prepared_admin = await _prepare_platform_admin_or_403(db, username=username)
    current_client_id = (
        read_key("VITALS_MCP_CLIENT_ID").strip() or "vitals-claude-connector"
    )
    updates: dict[str, str] = {}
    changed_fields: set[str] = set()
    requested_client_id = mcp_client_id.strip()
    if requested_client_id and requested_client_id != current_client_id:
        if len(requested_client_id) > 255 or any(
            char.isspace() or not char.isprintable() for char in requested_client_id
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid MCP client identifier",
            )
        updates["VITALS_MCP_CLIENT_ID"] = requested_client_id
        changed_fields.add("client_id")
    if mcp_client_secret.strip() and not is_secret_sentinel(mcp_client_secret):
        updates["VITALS_MCP_CLIENT_SECRET"] = mcp_client_secret.strip()
        changed_fields.add("client_secret")

    if not updates:
        return RedirectResponse(
            url="/settings/platform?saved=mcp",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    revoked_connectors = 0
    if "client_id" in changed_fields:
        revoked_connectors = await mcp_tokens.revoke_all_live(db)
    await platform_authorization.record_mcp_configuration_change(
        db,
        prepared=prepared_admin,
        changed_fields=changed_fields,
        revoked_connectors=revoked_connectors,
    )

    previous_persisted = {key: read_key(key) for key in updates}
    previous_runtime = {
        key: (key in os.environ, os.environ.get(key, "")) for key in updates
    }
    environment_written = False
    try:
        write_keys(updates)
        environment_written = True
        _set_runtime_values(updates)
        await db.commit()
    except BaseException:
        compensation_error: Exception | None = None
        if environment_written:
            try:
                write_keys(previous_persisted)
                for key, (was_present, value) in previous_runtime.items():
                    if was_present:
                        os.environ[key] = value
                    else:
                        os.environ.pop(key, None)
            except Exception as exc:
                compensation_error = exc
                for key in updates:
                    os.environ[key] = ""
        await db.rollback()
        if compensation_error is not None:
            logger.critical(
                "platform MCP configuration failed and could not restore its "
                "environment; connector authorization is disabled"
            )
            raise RuntimeError(
                "platform MCP configuration could not restore its environment"
            ) from compensation_error
        raise
    return RedirectResponse(
        url="/settings/platform?saved=mcp",
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = ["router"]
