"""Settings panel: read and persist VITALS_* configuration via the web UI.

GET  /settings          — render the settings page (prefilled from .env)
POST /settings/profile  — save profile block (height, sex, age, timezone, program, goals)
POST /settings/ai       — save AI / OpenRouter block (api key, model slugs)
POST /settings/hevy     — save Hevy API key
POST /settings/garmin   — save Garmin credentials (email + password)
POST /settings/password — change the login password (requires old password)

All writes go to the .env file via ``web.services.env_writer``.  The app
shows a banner asking the user to restart the container so the new values
are picked up by ``load_config()`` / ``get_web_config()``.

Sensitive inputs (API keys, passwords) are always shown masked in the form.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import uuid
from datetime import date
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import t
from vitals.integrations.garmin_client import login_breaker_state
from vitals.services import (
    ai_gateway_service,
    data_portability_service,
    garmin_weight_service,
    language_service,
    modules_service,
    platform_ai_control_service,
    platform_admin_service,
    twofa_service,
)
from vitals.services.modules_service import ModuleToggleError
from vitals.access import PolicyAction, PolicyResourceType
from web.config import get_web_config
from vitals.services.access_resolution import AccessDeniedError, require_access
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from vitals.services.installation_operator import (
    NotAnOperator,
    require_installation_operator,
)
from vitals.services.proactive import prefs
from vitals.utils.timeutils import today_local
from web.deps import get_redis, get_session, require_auth
from web.ratelimit import rate_limit
from web.services.env_writer import read_key, write_keys
from web.templating import templates
from web.uploads import JSON_EXTS, VCF_MAX_BYTES, read_capped, validate_extension

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

# Keys that are shown partially-masked in the UI (never echoed as plaintext).
_SECRET_KEYS = {
    "VITALS_OPENROUTER_API_KEY",
    "VITALS_HEVY_API_KEY",
    "VITALS_GARMIN_PASSWORD",
    "VITALS_AUTH_PASSWORD_HASH",
    "VITALS_MCP_CLIENT_SECRET",
}

_SENTINEL = "••••••••"  # what we show in place of a real secret

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


def _masked(key: str) -> str:
    """Return a masked placeholder when the key has a value, else empty."""
    return _SENTINEL if read_key(key) else ""


def _is_sentinel(value: str) -> bool:
    return value.strip() == _SENTINEL


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
    if len(value) > 2048 or any(char.isspace() or not char.isprintable() for char in value):
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
    """Commit one DB/environment change with a fail-closed ambiguity policy.

    The transaction already holds the shared identity-governance lock, so no new
    gateway dispatch can start between the local environment write and commit.
    Once commit begins, an exception cannot prove whether the database accepted
    it. The credential is therefore cleared persistently instead of guessing
    which secret version matches the current root. Explicit reconciliation is
    required before dispatch can resume. A hard process stop remains an
    unavoidable window for this legacy filesystem-backed secret store.
    """

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
            # Fail closed in this process even if the persistence repair fails.
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
) -> platform_admin_service.PreparedPlatformAdmin:
    try:
        return await platform_admin_service.prepare_platform_admin(
            db,
            actor_username=username,
        )
    except platform_admin_service.PlatformAdminAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        ) from exc


def _garmin_credentials() -> tuple[str, str]:
    """Return the persisted Garmin credentials without ever logging them."""
    return (
        read_key("VITALS_GARMIN_EMAIL").strip(),
        read_key("VITALS_GARMIN_PASSWORD").strip(),
    )


def _activate_garmin_credentials(email: str, password: str) -> None:
    """Make persisted credentials visible to clients created in this process."""
    os.environ["VITALS_GARMIN_EMAIL"] = email
    os.environ["VITALS_GARMIN_PASSWORD"] = password


async def _garmin_weight_control(
    request: Request,
    *,
    db: AsyncSession,
    username: str,
    action: Optional[str] = None,
) -> HTMLResponse:
    """Render the self-contained HTMX control after a live action."""
    email, password = _garmin_credentials()
    export_context = await garmin_weight_service.resolve_legacy_export_context(
        db,
        actor_username=username,
    )
    prepared_export = await garmin_weight_service.prepare_scoped_export(
        db,
        context=export_context,
        historical=True,
    )
    return templates.TemplateResponse(
        request,
        "partials/garmin_weight_export.html",
        {
            "garmin_credentials_configured": bool(email and password),
            "garmin_weight_export": await garmin_weight_service.get_status_scoped(
                db,
                prepared=prepared_export,
            ),
            "garmin_weight_action": action,
        },
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _redirect(suffix: str = "") -> RedirectResponse:
    url = f"/settings{suffix}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


async def _page(
    request: Request,
    username: str,
    *,
    db: AsyncSession,
    redis: Optional[Redis] = None,
    saved: Optional[str] = None,
    error: Optional[str] = None,
    adjusted: Optional[str] = None,
) -> HTMLResponse:
    """Build the template context and render settings.html."""
    # Redis is external I/O. Read it before any database preparation can acquire
    # transaction-lifetime identity/outbox locks.
    breaker = await login_breaker_state(redis)
    preference_scope = await prefs.resolve_legacy_preferences_scope(
        db,
        actor_username=username,
    )
    can_manage_openrouter = await platform_admin_service.is_active_platform_admin(
        db,
        actor_username=username,
    )
    proactive = (
        await prefs.get_preferences_bundle(
            db,
            scope=preference_scope,
            actor_username=username,
        )
    ).as_flat_dict()
    export_context = await garmin_weight_service.resolve_legacy_export_context(
        db,
        actor_username=username,
    )
    prepared_export = await garmin_weight_service.prepare_scoped_export(
        db,
        context=export_context,
        historical=True,
    )
    # Under the OIDC cutover the provider owns sign-in entirely, and every
    # route behind this card already answers 404. Reading the state anyway would
    # put a live enrolment secret on screen — a half-finished enrolment from
    # before the cutover still reads as ``pending`` — beside buttons that cannot
    # act on it.
    federated_signin = get_web_config().oidc_enabled
    twofa = (
        twofa_service.TwoFAState()
        if federated_signin
        else await twofa_service.get_state(db)
    )
    _twofa_uri = (
        twofa_service.provisioning_uri(twofa.secret, account=username) if twofa.pending else ""
    )
    ctx = {
        "username": username,
        # Two-factor auth. The card renders one of three states off this: off,
        # mid-enrolment (secret minted, not yet proven), on.
        "federated_signin": federated_signin,
        "twofa": twofa,
        # Only ever shown while enrolment is unconfirmed — once 2FA is on, the
        # secret has no reason to appear on screen again.
        "twofa_secret_display": twofa_service.format_secret(twofa.secret) if twofa.pending else "",
        "twofa_uri": _twofa_uri,
        "twofa_qr": twofa_service.qr_svg(_twofa_uri) if _twofa_uri else "",
        "saved": saved,
        "error": error,
        "adjusted": adjusted,
        # Profile
        "height_cm": read_key("VITALS_HEIGHT_CM") or "190",
        "sex": read_key("VITALS_SEX") or "male",
        "user_age": read_key("VITALS_USER_AGE") or "18",
        "timezone": read_key("VITALS_TIMEZONE") or "Europe/Chisinau",
        "user_program": read_key("VITALS_USER_PROGRAM"),
        "user_goals": read_key("VITALS_USER_GOALS"),
        # AI
        "can_manage_openrouter": can_manage_openrouter,
        "openrouter_api_key_set": (
            bool(read_key("VITALS_OPENROUTER_API_KEY"))
            if can_manage_openrouter
            else False
        ),
        "openrouter_base_url": (
            read_key("VITALS_OPENROUTER_BASE_URL")
            or "https://openrouter.ai/api/v1"
            if can_manage_openrouter
            else ""
        ),
        "llm_model_digest": (
            read_key("VITALS_LLM_MODEL_DIGEST")
            or "anthropic/claude-sonnet-4.6"
            if can_manage_openrouter
            else ""
        ),
        "llm_model_parser": (
            read_key("VITALS_LLM_MODEL_PARSER")
            or "google/gemini-2.5-flash"
            if can_manage_openrouter
            else ""
        ),
        # Empty = "use the digest model", which is what keeps the brief working
        # before this is ever set. Placeholder, not a pre-filled default.
        "llm_model_brief": (
            read_key("VITALS_LLM_MODEL_BRIEF") if can_manage_openrouter else ""
        ),
        # Hevy
        "hevy_api_key_set": bool(read_key("VITALS_HEVY_API_KEY")),
        # Garmin
        "garmin_email": read_key("VITALS_GARMIN_EMAIL"),
        "garmin_password_set": bool(read_key("VITALS_GARMIN_PASSWORD")),
        "garmin_credentials_configured": bool(all(_garmin_credentials())),
        "garmin_weight_export": await garmin_weight_service.get_status_scoped(
            db,
            prepared=prepared_export,
        ),
        # MCP
        "mcp_client_id": read_key("VITALS_MCP_CLIENT_ID") or "vitals-claude-connector",
        "mcp_client_secret_set": bool(read_key("VITALS_MCP_CLIENT_SECRET")),
        # Dashboard modules — registry + current state (set on request.state by
        # the global load_enabled_modules dependency).
        # Nutrition goals
        "nutrition_protein_target_g": read_key("VITALS_NUTRITION_PROTEIN_TARGET_G") or "150",
        "nutrition_calories_min": read_key("VITALS_NUTRITION_CALORIES_MIN") or "1300",
        "nutrition_calories_max": read_key("VITALS_NUTRITION_CALORIES_MAX") or "1700",
        # Dashboard modules — ``module_registry`` is a Jinja global (templating.py).
        "enabled_modules": getattr(request.state, "enabled_modules", {}) or {},
        # Proactive layer — DB-backed, unlike everything above.
        "proactive": proactive,
        "nudge_categories": prefs.NUDGE_CATEGORIES,
        "budget_range": prefs.BUDGET_RANGE,
        "sync_hours_range": prefs.SYNC_HOURS_RANGE,
        "pulse_range": prefs.PULSE_SECONDS_RANGE,
        "weight_export_minutes_range": prefs.WEIGHT_EXPORT_MINUTES_RANGE,
        "weight_max_age_days_range": prefs.WEIGHT_MAX_AGE_DAYS_RANGE,
        # The login breaker: how many credential logins today's poll
        # frequency has actually cost, right next to the field that sets it.
        "breaker": breaker,
    }
    return templates.TemplateResponse(request, "settings/settings.html", ctx)


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    saved: Optional[str] = None,
    error: Optional[str] = None,
    adjusted: Optional[str] = None,
):
    return await _page(request, username, db=db, redis=redis, saved=saved, error=error, adjusted=adjusted)


@router.post("/profile")
async def save_profile(
    request: Request,
    username: str = Depends(require_auth),
    height_cm: str = Form("190"),
    sex: str = Form("male"),
    user_age: str = Form("18"),
    timezone: str = Form("Europe/Chisinau"),
    user_program: str = Form(""),
    user_goals: str = Form(""),
    nutrition_protein_target_g: str = Form(""),
    nutrition_calories_min: str = Form(""),
    nutrition_calories_max: str = Form(""),
):
    updates: dict[str, str] = {}
    if height_cm.strip():
        updates["VITALS_HEIGHT_CM"] = height_cm.strip()
    if sex in ("male", "female"):
        updates["VITALS_SEX"] = sex
    if user_age.strip().isdigit():
        updates["VITALS_USER_AGE"] = user_age.strip()
    if timezone.strip():
        updates["VITALS_TIMEZONE"] = timezone.strip()
    if user_program.strip():
        # Collapse newlines: this textarea is free text, but env_writer rejects
        # \n/\r in values (an unescaped newline would break out of its KEY=value
        # line in the .env file).
        updates["VITALS_USER_PROGRAM"] = " ".join(user_program.split())
    if user_goals.strip():
        updates["VITALS_USER_GOALS"] = user_goals.strip()
    if nutrition_protein_target_g.strip():
        updates["VITALS_NUTRITION_PROTEIN_TARGET_G"] = nutrition_protein_target_g.strip()
    if nutrition_calories_min.strip():
        updates["VITALS_NUTRITION_CALORIES_MIN"] = nutrition_calories_min.strip()
    if nutrition_calories_max.strip():
        updates["VITALS_NUTRITION_CALORIES_MAX"] = nutrition_calories_max.strip()

    if updates:
        write_keys(updates)
    return _redirect("?saved=profile")


@router.get("/platform/ai", response_class=HTMLResponse)
async def platform_ai_page(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    saved: Optional[str] = None,
    error: Optional[str] = None,
):
    prepared_admin = await _prepare_platform_admin_or_403(db, username=username)
    snapshot = await platform_ai_control_service.get_platform_ai_control_snapshot(
        db,
        prepared=prepared_admin,
    )
    return templates.TemplateResponse(
        request,
        "settings/platform_ai.html",
        {
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
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    openrouter_api_key: str = Form(""),
    openrouter_base_url: str = Form(""),
    llm_model_digest: str = Form(""),
    llm_model_parser: str = Form(""),
    llm_model_brief: str = Form(""),
):
    del request
    prepared_admin = await _prepare_platform_admin_or_403(db, username=username)

    current_values = {
        key: _effective_ai_value(key) for key in _AI_ENV_DEFAULTS
    }
    submitted_key = openrouter_api_key.strip()
    try:
        effective_key = _validate_openrouter_credential(
            submitted_key
            if submitted_key and not _is_sentinel(submitted_key)
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
            # Empty is the intentional "use digest model" value.
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
    if submitted_key and not _is_sentinel(submitted_key) and not secrets.compare_digest(
        effective_key,
        current_values["VITALS_OPENROUTER_API_KEY"],
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
        transition = await platform_ai_control_service.apply_gateway_configuration(
            db,
            prepared=prepared_admin,
            configuration_changed=bool(changed_fields),
            credential_available=bool(effective_key),
            desired_enabled=None,
            changed_fields=frozenset(changed_fields),
        )
        if updates or transition.action is not platform_ai_control_service.GatewayTransitionAction.NO_CHANGE:
            await _commit_ai_control_change(db, updates=updates)
    except (
        ValueError,
        platform_ai_control_service.PlatformAIControlError,
        platform_admin_service.PlatformAdminValidationError,
    ):
        await db.rollback()
        return _platform_ai_redirect(error="configuration_invalid")
    return _platform_ai_redirect(saved="ai")


@router.post("/platform/ai/enable")
async def enable_platform_ai(
    username: str = Depends(require_auth),
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
        transition = await platform_ai_control_service.apply_gateway_configuration(
            db,
            prepared=prepared_admin,
            configuration_changed=False,
            credential_available=bool(credential),
            desired_enabled=True,
        )
        if transition.action is not platform_ai_control_service.GatewayTransitionAction.NO_CHANGE:
            await db.commit()
    except (ValueError, platform_ai_control_service.PlatformAIControlError):
        await db.rollback()
        error = "credential_missing" if not _effective_ai_value(
            "VITALS_OPENROUTER_API_KEY"
        ) else "configuration_invalid"
        return _platform_ai_redirect(error=error)
    return _platform_ai_redirect(saved="enabled")


@router.post("/platform/ai/disable")
async def disable_platform_ai(
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    prepared_admin = await _prepare_platform_admin_or_403(db, username=username)
    try:
        transition = await platform_ai_control_service.apply_gateway_configuration(
            db,
            prepared=prepared_admin,
            configuration_changed=False,
            credential_available=bool(
                _effective_ai_value("VITALS_OPENROUTER_API_KEY")
            ),
            desired_enabled=False,
        )
        if transition.action is not platform_ai_control_service.GatewayTransitionAction.NO_CHANGE:
            await db.commit()
    except platform_ai_control_service.PlatformAIControlError:
        await db.rollback()
        return _platform_ai_redirect(error="gateway_invalid")
    return _platform_ai_redirect(saved="disabled")


@router.post("/platform/ai/quota")
async def configure_platform_ai_quota(
    username: str = Depends(require_auth),
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
        result = await platform_ai_control_service.configure_aligned_quota_period(
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
        platform_ai_control_service.PlatformAIControlError,
        ai_gateway_service.AIGatewayError,
    ):
        await db.rollback()
        return _platform_ai_redirect(error="quota_invalid")
    return _platform_ai_redirect(saved="quota")


@router.post("/hevy")
async def save_hevy(
    request: Request,
    username: str = Depends(require_auth),
    hevy_api_key: str = Form(""),
):
    updates: dict[str, str] = {}
    if hevy_api_key.strip() and not _is_sentinel(hevy_api_key):
        updates["VITALS_HEVY_API_KEY"] = hevy_api_key.strip()

    if updates:
        write_keys(updates)
    return _redirect("?saved=hevy")


@router.post("/garmin")
async def save_garmin(
    request: Request,
    username: str = Depends(require_auth),
    garmin_email: str = Form(""),
    garmin_password: str = Form(""),
):
    stored_email, stored_password = _garmin_credentials()
    submitted_email = garmin_email.strip()
    submitted_password = garmin_password.strip()
    effective_email = submitted_email or stored_email
    effective_password = (
        submitted_password
        if submitted_password and not _is_sentinel(submitted_password)
        else stored_password
    )

    updates: dict[str, str] = {}
    if submitted_email:
        updates["VITALS_GARMIN_EMAIL"] = submitted_email
    if submitted_password and not _is_sentinel(submitted_password):
        updates["VITALS_GARMIN_PASSWORD"] = submitted_password

    if updates:
        write_keys(updates)
    # GarminClient reads load_config() for every new client. Updating the process
    # environment here makes newly saved credentials effective immediately while
    # preserving blank/sentinel fields as "keep the current value".
    _activate_garmin_credentials(effective_email, effective_password)
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
    email, password = _garmin_credentials()
    if enabled and not (email and password):
        # Never persist a fail-open opt-in. The scheduled job also guards this,
        # but the settings boundary should make the rejected state explicit.
        return await _garmin_weight_control(
            request,
            db=db,
            username=username,
            action="credentials_required",
        )

    try:
        if enabled:
            _activate_garmin_credentials(email, password)
        export_context = await garmin_weight_service.resolve_legacy_export_context(
            db,
            actor_username=username,
        )
        prepared_export = await garmin_weight_service.prepare_scoped_export(
            db,
            context=export_context,
            historical=not enabled,
        )
        await garmin_weight_service.set_enabled_scoped(
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
        export_context = await garmin_weight_service.resolve_legacy_export_context(
            db,
            actor_username=username,
        )
        prepared_export = await garmin_weight_service.prepare_scoped_export(
            db,
            context=export_context,
        )
        result = await garmin_weight_service.send_now_scoped(
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


@router.post("/mcp")
async def save_mcp(
    request: Request,
    username: str = Depends(require_auth),
    mcp_client_id: str = Form("vitals-claude-connector"),
    mcp_client_secret: str = Form(""),
):
    updates: dict[str, str] = {}
    if mcp_client_id.strip():
        updates["VITALS_MCP_CLIENT_ID"] = mcp_client_id.strip()
    if mcp_client_secret.strip() and not _is_sentinel(mcp_client_secret):
        updates["VITALS_MCP_CLIENT_SECRET"] = mcp_client_secret.strip()

    if updates:
        write_keys(updates)
        # Apply to current process environment to support immediate refresh
        import os
        for k, v in updates.items():
            os.environ[k] = v
    return _redirect("?saved=mcp")



@router.post("/modules")
async def toggle_module(
    request: Request,
    module: str = Form(...),
    enabled: bool = Form(...),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    _rl: None = Depends(rate_limit("settings_modules", limit=30, window=60)),
):
    """Enable/disable an Optional dashboard module, on the fly.

    Persists to ``app_settings`` (source of truth), write-through to Redis, then
    returns an OOB fragment that re-renders the header nav so it updates live —
    no page reload.
    """
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    try:
        state = await modules_service.set_module_enabled(
            db,
            key=module,
            enabled=enabled,
            subject_id=ownership.subject_id,
        )
    except ModuleToggleError as e:
        # Core/unknown module — reject loudly (Zero Silent Errors).
        return JSONResponse({"error": str(e)}, status_code=status.HTTP_400_BAD_REQUEST)

    await db.commit()
    await modules_service.prime_cache(
        redis,
        state,
        subject_id=ownership.subject_id,
    )
    # Reflect the new state for the OOB nav render in *this* response.
    request.state.enabled_modules = state
    return templates.TemplateResponse(
        request,
        "partials/modules_oob.html",
        {"username": username, "enabled_modules": state},
    )


# ── Two-factor auth ───────────────────────────────────────────────────────────


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


@router.post("/proactive")
async def save_proactive(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    brief_time: str = Form(prefs.DEFAULTS["brief_time"]),
    evening_time: str = Form(prefs.DEFAULTS["evening_time"]),
    quiet_start: str = Form(prefs.DEFAULTS["quiet_start"]),
    quiet_end: str = Form(prefs.DEFAULTS["quiet_end"]),
    daily_budget: int = Form(prefs.DEFAULTS["daily_budget"]),
    garmin_sync_hours: int = Form(prefs.DEFAULTS["garmin_sync_hours"]),
    garmin_weight_export_minutes: int = Form(
        prefs.DEFAULTS["garmin_weight_export_minutes"]
    ),
    garmin_weight_max_age_days: int = Form(
        prefs.DEFAULTS["garmin_weight_max_age_days"]
    ),
    pulse_seconds: int = Form(prefs.DEFAULTS["pulse_seconds"]),
    pulse_start_hour: int = Form(prefs.DEFAULTS["pulse_start_hour"]),
    pulse_end_hour: int = Form(prefs.DEFAULTS["pulse_end_hour"]),
    nudges: list[str] = Form([]),
):
    """Save the proactive settings **and rebuild the schedule on the spot**.

    Everything else on this page writes ``.env`` and needs a restart; these are in
    the DB precisely so they don't. ``prefs.sanitize`` clamps whatever arrives —
    the HTML min/max are a courtesy, not the guard.
    """
    raw_prefs = {
        "brief_time": brief_time,
        "evening_time": evening_time,
        "quiet_start": quiet_start,
        "quiet_end": quiet_end,
        "daily_budget": daily_budget,
        "garmin_sync_hours": garmin_sync_hours,
        "garmin_weight_export_minutes": garmin_weight_export_minutes,
        "garmin_weight_max_age_days": garmin_weight_max_age_days,
        "pulse_seconds": pulse_seconds,
        "pulse_start_hour": pulse_start_hour,
        "pulse_end_hour": pulse_end_hour,
        # Unchecked boxes don't post, so the checked list *is* the answer.
        "nudges": {c: c in nudges for c in prefs.NUDGE_CATEGORIES},
    }
    preference_scope = await prefs.resolve_legacy_preferences_scope(
        db,
        actor_username=username,
    )
    settings = (
        await prefs.set_preferences_bundle(
            db,
            raw_prefs,
            scope=preference_scope,
            actor_username=username,
        )
    ).as_flat_dict()
    await db.commit()

    apply_schedule(request.app, settings)
    # prefs.sanitize() (called inside set_preferences_bundle) silently clamps
    # out-of-range
    # input — compare what was submitted to what actually got stored so the
    # user can be told, instead of seeing a plain "saved" while their number
    # was quietly changed underneath them.
    adjusted = raw_prefs != settings
    return _redirect("?saved=proactive&adjusted=1" if adjusted else "?saved=proactive")


def apply_schedule(app, settings: dict) -> None:
    """Re-register the jobs and push them onto the running scheduler.

    Best-effort on purpose: the settings *are* saved by the time this runs, so a
    scheduler that isn't up (tests, a worker that never started one) must not turn
    a successful save into a 500 — the new schedule is picked up at next boot
    either way.
    """
    from vitals.scheduler.jobs import register_all_jobs
    from vitals.scheduler.scheduler import apply_registry
    from web.deps import get_redis_client, get_session_factory

    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        return
    try:
        register_all_jobs(settings)
        apply_registry(scheduler, get_session_factory(), get_redis_client())
    except Exception:
        logger.exception("could not apply the new schedule; it takes effect on restart")


@router.post("/language")
async def save_language(
    request: Request,
    language: str = Form(...),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    lang = await language_service.set_language(
        db,
        language,
        redis=None,
        user_id=ownership.owner_user_id,
    )
    await db.commit()
    await language_service.prime_cache(
        redis,
        lang,
        user_id=ownership.owner_user_id,
    )
    return RedirectResponse(
        url="/settings?saved=language",
        status_code=status.HTTP_303_SEE_OTHER,
    )



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
    from vitals.services.identity_bootstrap import bootstrap_legacy_owner
    from vitals.services.identity_service import bcrypt_cost, rotate_password_hash
    from vitals.utils.passwords import hash_password
    from web.auth import authenticate, create_session, set_session_cookie
    from web.config import get_web_config

    cfg = get_web_config()

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


async def _authorize_export(db: AsyncSession, username: str):
    """Decide the export, rather than infer it from being logged in.

    Downloading the record is the one routine operation that takes the data out
    of the boundary everything else keeps it inside, so it is the first to be
    *decided* by the policy engine rather than merely resolved. Today the answer
    is always yes — self-ownership authorizes it — and the value is that there is
    now one place for the answer to become no.
    """

    ownership = await resolve_legacy_ownership_context(db, actor_username=username)
    if ownership.access is None:  # pragma: no cover - require_auth names an actor
        raise AccessDeniedError("an export needs a principal behind it")
    require_access(
        ownership.access,
        resource_type=PolicyResourceType.OPERATION,
        resource_key="data_portability.export",
        action=PolicyAction.EXPORT,
    )
    return ownership



async def _authorize_installation_operation(
    db: AsyncSession, username: str, *, operation: str
) -> None:
    """Decide an operation that is about the installation, not about a record.

    Restoring a backup replaces portable data for everybody in the database, and
    restarting takes the whole process down. Neither is a question about one
    subject, so neither goes through the subject-scoped policy — see
    ``vitals.services.installation_operator`` for why passing the caller's own
    subject in would read as a check while always saying yes.
    """

    ownership = await resolve_legacy_ownership_context(db, actor_username=username)
    try:
        await require_installation_operator(
            db,
            access=ownership.access,
            operation=operation,
        )
    except NotAnOperator as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc


@router.get("/export")
async def export_backup(
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_export", limit=2, window=60)),
):
    """Download portable health data without identity/control-plane state.

    This is the whole-installation file, and format v1 describes an installation
    holding one person. In a shared one it therefore has nothing honest to
    write, which is a thing to say — with the export that *does* work named in
    the same breath — rather than a stack trace to serve as a 500. The personal
    export below is not a lesser version of this one: it is the right file for
    anybody who is not the whole installation.
    """
    await _authorize_export(db, username)
    try:
        snapshot = await data_portability_service.export_full(db)
    except data_portability_service.MultiSubjectBackupError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=t("portability.error.v1_multi_subject_alternative"),
        ) from exc
    except data_portability_service.PortabilityError as exc:
        # Anything else this raises is about the data being unrepresentable in
        # the format, which is the caller's answer to have — not a 500.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    filename = f"vitals_backup_{today_local().strftime('%Y%m%d')}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export-subject")
async def export_subject_backup(
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_export", limit=2, window=60)),
):
    """Download exactly this subject's record — no installation configuration.

    The other export answers "what is in this installation" and is an operator's
    file. This one answers "what is mine": one subject's rows, no app settings,
    and none of the installation's curated catalog, which the receiving
    installation seeds for itself.
    """

    ownership = await _authorize_export(db, username)
    snapshot = await data_portability_service.export_subject(
        db, subject_id=ownership.subject_id
    )
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    filename = f"vitals_record_{today_local().strftime('%Y%m%d')}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export-llm")
async def export_llm(
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_export", limit=2, window=60)),
):
    """Download a curated, flat, secret-free digest for pasting into an LLM chat."""
    await _authorize_export(db, username)
    snapshot = await data_portability_service.export_llm(db)
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    filename = f"vitals_llm_{today_local().strftime('%Y%m%d')}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import")
async def import_backup(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    backup_file: UploadFile = File(...),
    _rl: None = Depends(rate_limit("data_import", limit=2, window=60)),
):
    """Restore (replace) the whole DB from an uploaded full-backup JSON file.

    Atomic: the import runs in this request's transaction, so a malformed file
    rolls everything back. Validation failures return a clean 400 (no silent
    errors); success returns an OOB fragment with the per-domain stats.
    """
    await _authorize_installation_operation(
        db, username, operation="a restore"
    )
    validate_extension(backup_file.filename, JSON_EXTS)
    # Backups can be large (the raw_payloads data-lake), so allow the bigger cap.
    raw = await read_capped(backup_file, max_bytes=VCF_MAX_BYTES)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("import.error.bad_json", msg=exc.msg, line=exc.lineno),
        )

    try:
        stats = await data_portability_service.import_full(db, payload)
    except data_portability_service.PortabilityError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    await db.commit()
    return templates.TemplateResponse(
        request,
        "settings/import_result.html",
        {"summary": stats.summary()},
    )


@router.post("/import-subject")
async def import_subject_record(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    backup_file: UploadFile = File(...),
    _rl: None = Depends(rate_limit("data_import", limit=2, window=60)),
):
    """Restore this subject's own record, and nobody else's.

    Not the operator's restore: that one empties every portable table and is
    correct only for a whole-database backup. This deletes and reloads exactly
    the caller's subject, so it needs the same authorization as an export rather
    than an operator's, and it refuses a full backup outright.
    """

    ownership = await _authorize_export(db, username)
    validate_extension(backup_file.filename, JSON_EXTS)
    raw = await read_capped(backup_file, max_bytes=VCF_MAX_BYTES)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("import.error.bad_json", msg=exc.msg, line=exc.lineno),
        )

    try:
        stats = await data_portability_service.import_subject(
            db, payload, subject_id=ownership.subject_id
        )
    except data_portability_service.PortabilityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    await db.commit()
    return templates.TemplateResponse(
        request,
        "settings/import_result.html",
        {"summary": stats.summary()},
    )


@router.post("/restart")
async def restart_container(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    import os
    import signal
    import asyncio
    from fastapi.responses import JSONResponse

    await _authorize_installation_operation(
        db, username, operation="a restart"
    )

    logger.info("User %s requested container restart. Terminating process in 500ms...", username)

    async def shutdown():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(shutdown())
    return JSONResponse(content={"status": "restarting"})
