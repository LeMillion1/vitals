"""Settings panel: read and persist VITALS_* configuration via the web UI.

GET  /settings          — render the settings page
POST /settings/profile  — save profile block (height, sex, age, timezone, program, goals)
POST /settings/ai       — save AI / OpenRouter block (api key, model slugs)
POST /settings/hevy     — save Hevy API key
POST /settings/garmin   — save Garmin credentials (email + password)
POST /settings/password — change the login password (requires old password)

Most writes here go to the .env file via ``web.services.env_writer``, and the
app shows a banner asking the user to restart the container so the new values
are picked up by ``load_config()`` / ``get_web_config()``.

**The profile block is the exception, and the direction of travel.** Age, sex,
height, the programme, the goals, the nutrition targets and the timezone belong
to a person rather than to the installation, so they are stored on the health
subject and take effect immediately. ``.env`` should hold only what the
installation owns — the database, Redis, the session secret, the identity
provider, the AI gateway — and the remaining blocks above are what is left to
move.

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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import PolicyAction, PolicyResourceType
from vitals.config import load_config
from vitals.i18n import t
from vitals.integrations.garmin_client import login_breaker_state
from vitals.models.identity import HealthSubject
from vitals.operations.ownership import portability_v1
from vitals.process_mode import ProcessMode, load_process_mode
from vitals.services import (
    ai_gateway_service,
    credential_vault_service,
    data_portability_service,
    garmin_weight_service,
    health_profile_service,
    language_service,
    modules_service,
    platform_admin_service,
    platform_ai_control_service,
    provider_credentials_service,
)
from vitals.services.access_resolution import AccessDeniedError, require_access
from vitals.services.authentication import legacy_two_factor as twofa_service
from vitals.services.installation_operator import (
    NotAnOperator,
    require_installation_operator_user,
)
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from vitals.services.modules_service import ModuleToggleError
from vitals.services.proactive import prefs
from vitals.utils.timeutils import today_local
from web.care_context import principal_user_id
from web.config import get_web_config
from web.deps import get_redis, get_session, require_auth, require_recent_auth
from web.downloads import private_json_download
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


def _blank_if_none(value) -> str:
    """An unset profile field renders as an empty box, not as a default.

    The form used to be pre-filled with 190 cm, male, 18 — the installation's
    values, which every reader saw as though the app already knew them. An empty
    field is the honest rendering of nobody having said.
    """

    if value is None:
        return ""
    return _number(value)


def _number(value) -> str:
    """``80.0`` reads as ``80`` in an input box; ``80.5`` stays ``80.5``."""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _is_known_timezone(zone: str) -> bool:
    """Whether the IANA database has this zone.

    Checked before it is stored rather than when a page reads it: an unknown
    zone written here would raise on every later request that asks what day it
    is for this person, which is most of them.
    """

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


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


async def _subject_garmin_account(db: AsyncSession, username: str):
    """This account's own Garmin connection, whatever it is signed in as.

    Replaces reading ``VITALS_GARMIN_EMAIL`` off the environment, which is the
    installation's one watch: on a shared installation every patient's settings
    card showed the operator's address in the email box and "connected" beside
    it, and the outbound-weight opt-in they were offered would have pushed their
    weight to somebody else's Garmin.
    """

    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    return await provider_credentials_service.resolve_garmin_account(
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
            "garmin_credentials_configured": bool(account and account.configured),
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
    deferred: Optional[str] = None,
    issued_external_token: Optional[str] = None,
) -> HTMLResponse:
    """Build the template context and render settings.html.

    ``issued_external_token`` is handed in by the POST that minted it and
    rendered once. It never travels through a URL — see
    :func:`issue_external_api_token`.
    """
    preference_scope = await prefs.resolve_legacy_preferences_scope(
        db,
        actor_username=username,
    )
    profile = await health_profile_service.get_profile(
        db, subject_id=preference_scope.subject_id
    )
    subject_timezone_value = await db.scalar(
        select(HealthSubject.timezone).where(
            HealthSubject.id == preference_scope.subject_id
        )
    ) or load_config().timezone
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
    # Whose provider accounts these cards are about. Read from this subject's
    # own connections rather than from the environment, which describes the
    # installation's single watch and single workout account. Two plain selects,
    # deliberately ahead of the Redis read below: the breaker's key is per
    # account, so it cannot be read until the account is known.
    garmin_account = await provider_credentials_service.resolve_garmin_account(
        db, subject_id=preference_scope.subject_id
    )
    hevy_account = await provider_credentials_service.resolve_hevy_account(
        db, subject_id=preference_scope.subject_id
    )
    # Read-only credentials for another app's glance cards. Listed with the
    # revoked and lapsed ones, because "what can read my data" and "what could"
    # are the same list to somebody auditing it.
    external_tokens = await _external_token_rows(
        db, subject_id=preference_scope.subject_id
    )
    # Assistants connected to this account. An account rather than a record:
    # the token authorizes a person, and which record it then reaches is
    # decided per request.
    mcp_connectors = await _connector_rows(db, actor_username=username)
    # Redis is external I/O. Read it before any database *preparation* can
    # acquire transaction-lifetime identity/outbox locks — which is what
    # ``prepare_scoped_export`` below does, and what this ordering is about.
    breaker = await login_breaker_state(
        redis, garmin_account.namespace if garmin_account else ""
    )
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
        "deferred": deferred,
        # Profile — this person's row, not the installation's environment. An
        # unfilled field renders empty rather than as somebody's default: a
        # settings form pre-filled with 190 cm is a claim about the reader.
        "height_cm": _blank_if_none(profile.height_cm),
        "sex": profile.sex or "",
        "user_age": _blank_if_none(profile.age),
        "timezone": subject_timezone_value,
        "user_program": profile.program or "",
        "user_goals": ", ".join(profile.goals),
        "external_tokens": external_tokens,
        "mcp_connectors": mcp_connectors,
        # Handed straight through from the redirect. Shown once and stored
        # nowhere: only the hash reaches the database.
        "issued_external_token": issued_external_token,
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
        "hevy_api_key_set": bool(hevy_account and hevy_account.configured),
        # Garmin
        "garmin_email": (
            garmin_account.config.garmin_email if garmin_account else ""
        ),
        "garmin_password_set": bool(
            garmin_account and garmin_account.config.garmin_password
        ),
        # A deployment with no ``VITALS_CREDENTIAL_KEY`` cannot store one, and
        # the card says so rather than accepting a password and failing on save.
        "credential_vault_available": credential_vault_service.is_available(),
        "garmin_credentials_configured": bool(
            garmin_account and garmin_account.configured
        ),
        "garmin_weight_export": await garmin_weight_service.get_status_scoped(
            db,
            prepared=prepared_export,
        ),
        # MCP
        "mcp_client_id": read_key("VITALS_MCP_CLIENT_ID") or "vitals-claude-connector",
        "mcp_client_secret_set": bool(read_key("VITALS_MCP_CLIENT_SECRET")),
        # Dashboard modules — registry + current state (set on request.state by
        # the global load_enabled_modules dependency).
        # Nutrition goals — a target keeps a default where a body measurement
        # does not, so these are always a number.
        "nutrition_protein_target_g": _number(profile.protein_target_g),
        "nutrition_calories_min": str(profile.calories_min),
        "nutrition_calories_max": str(profile.calories_max),
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
    deferred: Optional[str] = None,
):
    return await _page(
        request,
        username,
        db=db,
        redis=redis,
        saved=saved,
        error=error,
        adjusted=adjusted,
        deferred=deferred,
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
    which describes nobody: one age, one sex, one height and one programme for
    however many patients the installation holds. The two visible consequences
    were a report that printed the owner's body on every patient's document,
    and a Navy body-fat estimate computed from the owner's height for everybody.

    ``VITALS_TIMEZONE`` went the same way and for a sharper reason: the day a
    page shows has been read from ``health_subjects.timezone`` since the
    per-subject clock landed, so this form was still writing the one place
    nothing reads. Changing your timezone in Settings did nothing at all.

    The old keys are left in ``.env`` untouched. They are what the startup
    adoption reads on an installation that has not upgraded yet, and rewriting
    them here would make the environment a second, disagreeing answer.
    """

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
    zone = timezone.strip()
    if zone and _is_known_timezone(zone):
        subject = await db.get(HealthSubject, identity.subject_id)
        if subject is not None:
            subject.timezone = zone
    await db.commit()
    return _redirect("?saved=profile")


async def _external_token_rows(db: AsyncSession, *, subject_id) -> list[dict]:
    """The credential list as a template can read it.

    Flattened to dictionaries on purpose: an ORM row on a settings page is one a
    template can lazy-load from, and this list exists on a screen that renders
    a dozen other things.
    """

    from datetime import datetime
    from datetime import timezone as _timezone

    from vitals.enums import ExternalApiTokenStatus
    from vitals.services import external_api_token_service as external_tokens

    now = datetime.now(_timezone.utc)
    rows = await external_tokens.list_for_subject(db, subject_id=subject_id)
    listed = []
    for row in rows:
        if row.status == ExternalApiTokenStatus.REVOKED.value:
            state = "revoked"
        elif external_tokens.is_live(row, at=now):
            state = "active"
        else:
            # Lapsed rather than stopped. The row still says ``active`` because
            # nobody revoked it; the clock did, and the screen should say which.
            state = "expired"
        listed.append(
            {
                "id": row.id,
                "label": row.label,
                "state": state,
                "expires_at": row.expires_at,
            }
        )
    return listed


async def _connector_rows(db: AsyncSession, *, actor_username: str) -> list[dict]:
    """The connector list as a template can read it.

    Flattened for the reason the credential list beside it is: an ORM row on a
    settings page is one a template can lazy-load from.
    """

    from datetime import datetime
    from datetime import timezone as _timezone

    from vitals.models.identity import User
    from vitals.services.authentication import mcp_tokens
    from vitals.services.identity_service import normalize_username

    lookup = normalize_username(actor_username).lookup_key
    user_id = await db.scalar(
        select(User.id).where(User.normalized_username == lookup)
    )
    if user_id is None:
        return []

    now = datetime.now(_timezone.utc)
    rows = await mcp_tokens.list_for_user(db, user_id=user_id)
    listed = []
    for row in rows:
        if row.revoked_at is not None:
            state = "revoked"
        elif mcp_tokens.is_live(row, at=now):
            state = "active"
        else:
            # Lapsed rather than disconnected: nobody stopped it, the clock did,
            # and the screen should say which.
            state = "expired"
        listed.append(
            {
                "id": row.id,
                "name": row.client_name or row.client_id,
                "state": state,
                "issued_at": row.issued_at,
                "adopted": row.adopted,
            }
        )
    return listed


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

    from vitals.models.identity import User
    from vitals.services.authentication import mcp_tokens
    from vitals.services.identity_service import normalize_username

    lookup = normalize_username(username).lookup_key
    user_id = await db.scalar(
        select(User.id).where(User.normalized_username == lookup)
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

    from vitals.services import external_api_token_service as external_tokens

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
    from vitals.services import external_api_token_service as external_tokens

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
    db: AsyncSession = Depends(get_session),
    hevy_api_key: str = Form(""),
):
    """Store this person's Hevy key against their own connection.

    It went into ``VITALS_HEVY_API_KEY`` — one workout account for the whole
    installation. The blank/sentinel field still means "keep what is there",
    which is what makes it safe to submit the card without retyping a secret.
    """

    submitted = hevy_api_key.strip()
    if not submitted or _is_sentinel(submitted):
        return _redirect("?saved=hevy")
    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    try:
        await provider_credentials_service.set_hevy_credentials(
            db, subject_id=identity.subject_id, api_key=submitted
        )
    except credential_vault_service.CredentialVaultUnavailable:
        await db.rollback()
        logger.warning("Hevy credential not stored: no installation vault key")
        return _redirect("?error=no_credential_key")
    except provider_credentials_service.ProviderCredentialsError:
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
        if submitted_password and not _is_sentinel(submitted_password)
        else stored_password
    )
    if not (effective_email and effective_password):
        return _redirect("?error=garmin")

    identity = await resolve_legacy_ownership_context(db, actor_username=username)
    try:
        await provider_credentials_service.set_garmin_credentials(
            db,
            subject_id=identity.subject_id,
            email=effective_email,
            password=effective_password,
        )
    except credential_vault_service.CredentialVaultUnavailable:
        await db.rollback()
        logger.warning("Garmin credential not stored: no installation vault key")
        return _redirect("?error=no_credential_key")
    except provider_credentials_service.ProviderCredentialsError:
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
):
    """Save proactive settings and rebuild a process-local schedule when present.

    Everything else on this page writes ``.env`` and needs a restart; these are in
    the DB precisely so they don't. ``prefs.sanitize`` clamps whatever arrives —
    the HTML min/max are a courtesy, not the guard.

    The card no longer offers quiet hours, the daily budget or the nudge
    switches: every one of them gates a *send*, and with the Telegram transport
    gone there is nothing to send with. The stored policy keeps them, because the
    delivery engine still reads it and a first web push has to be governed by
    something — so this handler reads the current values and writes them back
    unchanged rather than letting the ``Form`` defaults quietly reset whatever the
    owner last chose.
    """
    preference_scope = await prefs.resolve_legacy_preferences_scope(
        db,
        actor_username=username,
    )
    current = (
        await prefs.get_preferences_bundle(
            db,
            scope=preference_scope,
            actor_username=username,
        )
    ).as_flat_dict()
    raw_prefs = {
        **current,
        "brief_time": brief_time,
        "garmin_sync_hours": garmin_sync_hours,
        "garmin_weight_export_minutes": garmin_weight_export_minutes,
        "garmin_weight_max_age_days": garmin_weight_max_age_days,
        "pulse_seconds": pulse_seconds,
        "pulse_start_hour": pulse_start_hour,
        "pulse_end_hour": pulse_end_hour,
    }
    settings = (
        await prefs.set_preferences_bundle(
            db,
            raw_prefs,
            scope=preference_scope,
            actor_username=username,
        )
    ).as_flat_dict()
    # Asked before the commit closes the transaction, and answered about this
    # person: the scheduler registry is one per process, so rebuilding it from
    # a save re-times everybody's jobs. Whose Save that is allowed to be is a
    # question the row itself cannot answer.
    governs_schedule = await prefs.governs_the_process_schedule(
        db, subject_id=preference_scope.subject_id
    )
    await db.commit()

    schedule_applied = False
    if governs_schedule:
        schedule_applied = apply_schedule(request.app, settings)
    # prefs.sanitize() (called inside set_preferences_bundle) silently clamps
    # out-of-range
    # input — compare what was submitted to what actually got stored so the
    # user can be told, instead of seeing a plain "saved" while their number
    # was quietly changed underneath them.
    adjusted = raw_prefs != settings
    query = "?saved=proactive"
    if adjusted:
        query += "&adjusted=1"
    deferred = not governs_schedule
    if (
        governs_schedule
        and load_process_mode() is ProcessMode.WEB
        and not schedule_applied
    ):
        # The split worker cannot observe process memory. Until the Redis
        # generation/reload protocol lands, be honest that both processes need
        # a restart instead of claiming the live schedule changed.
        deferred = True
    if deferred:
        # Saved, and deliberately not applied to the running scheduler. A plain
        # "saved" here would be true about the row and false about the effect,
        # which is the worse of the two silences.
        query += "&deferred=1"
    return _redirect(query)


def apply_schedule(app, settings: dict) -> bool:
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
        return False
    try:
        register_all_jobs(settings)
        apply_registry(scheduler, get_session_factory(), get_redis_client())
    except Exception:
        logger.exception("could not apply the new schedule; it takes effect on restart")
        return False
    return True


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
    request: Request, db: AsyncSession, *, operation: str
) -> None:
    """Decide an operation that is about the installation, not about a record.

    Restoring a backup replaces portable data for everybody in the database, and
    restarting takes the whole process down. Neither is a question about one
    subject, so neither goes through the subject-scoped policy — see
    ``vitals.services.installation_operator`` for why passing the caller's own
    subject in would read as a check while always saying yes.
    """

    try:
        await require_installation_operator_user(
            db,
            user_id=await principal_user_id(request, db),
            operation=operation,
        )
    except NotAnOperator as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc


@router.get("/export")
async def export_backup(
    request: Request,
    _username: str = Depends(require_recent_auth),
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
    await _authorize_installation_operation(
        request, db, operation="a full portability export"
    )
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
    return private_json_download(body=body, filename=filename)


@router.get("/export-subject")
async def export_subject_backup(
    username: str = Depends(require_recent_auth),
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
    try:
        snapshot = await data_portability_service.export_subject(
            db, subject_id=ownership.subject_id
        )
    except data_portability_service.PortabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    filename = f"vitals_record_{today_local().strftime('%Y%m%d')}.json"
    return private_json_download(body=body, filename=filename)


@router.get("/export-llm")
async def export_llm(
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_export", limit=2, window=60)),
):
    """Download a curated, flat, secret-free digest for pasting into an LLM chat."""
    # The subject the authorization just resolved, handed on rather than
    # dropped. It used to be dropped, and ``export_llm`` read every table
    # unfiltered — so this download returned everybody's record on an
    # installation with more than one person in it.
    ownership = await _authorize_export(db, username)
    snapshot = await data_portability_service.export_llm(
        db, subject_id=ownership.subject_id
    )
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    filename = f"vitals_llm_{today_local().strftime('%Y%m%d')}.json"
    return private_json_download(body=body, filename=filename)


@router.post("/import")
async def import_backup(
    request: Request,
    _username: str = Depends(require_recent_auth),
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
        request, db, operation="a restore"
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
        stats = await portability_v1.import_full(db, payload)
    except data_portability_service.MultiSubjectBackupError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=t("portability.error.v1_multi_subject_alternative"),
        ) from exc
    except data_portability_service.PortabilityError as exc:
        await db.rollback()
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
    username: str = Depends(require_recent_auth),
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
    import asyncio
    import os
    import signal

    from fastapi.responses import JSONResponse

    await _authorize_installation_operation(
        request, db, operation="a restart"
    )

    logger.info("User %s requested container restart. Terminating process in 500ms...", username)

    async def shutdown():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(shutdown())
    return JSONResponse(content={"status": "restarting"})
