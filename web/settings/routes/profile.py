"""Settings page projection and personal profile delivery routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.integrations.garmin_client import login_breaker_state
from vitals.services.profile import health as health_profile_service
from vitals.services.garmin_weight import jobs as garmin_weight_jobs
from vitals.services.garmin_weight import outbox as garmin_weight_outbox
from vitals.services.authentication import legacy_two_factor as twofa_service
from vitals.services.credentials import providers, vault
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
from vitals.services.proactive.preferences import contracts as preference_contracts
from vitals.services.proactive.preferences import queries as preference_queries
from web.config import get_web_config
from web.deps import get_redis, get_session, require_auth
from web.templating import templates

from .common import (
    blank_if_none as _blank_if_none,
    compatibility_override,
    number as _number,
    redirect as _redirect,
)

router = APIRouter()

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
    preference_scope = await preference_queries.resolve_legacy_preferences_scope(
        db,
        actor_username=username,
    )
    profile_projection = await health_profile_service.get_profile_projection(
        db,
        subject_id=preference_scope.subject_id,
    )
    profile = profile_projection.profile
    subject_timezone_value = profile_projection.timezone or load_config().timezone
    proactive = (
        await preference_queries.get_preferences_bundle(
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
    garmin_account = await providers.resolve_garmin_account(
        db, subject_id=preference_scope.subject_id
    )
    hevy_account = await providers.resolve_hevy_account(
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
    breaker = await compatibility_override(
        "login_breaker_state", login_breaker_state
    )(
        redis, garmin_account.namespace if garmin_account else ""
    )
    export_context = await garmin_weight_outbox.resolve_legacy_export_context(
        db,
        actor_username=username,
    )
    prepared_export = await garmin_weight_outbox.prepare_scoped_export(
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
        "credential_vault_available": vault.is_available(),
        "garmin_credentials_configured": bool(
            garmin_account and garmin_account.configured
        ),
        "garmin_weight_export": await garmin_weight_jobs.get_status_scoped(
            db,
            prepared=prepared_export,
        ),
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
        "nudge_categories": preference_contracts.NUDGE_CATEGORIES,
        "budget_range": preference_contracts.BUDGET_RANGE,
        "sync_hours_range": preference_contracts.SYNC_HOURS_RANGE,
        "pulse_range": preference_contracts.PULSE_SECONDS_RANGE,
        "weight_export_minutes_range": preference_contracts.WEIGHT_EXPORT_MINUTES_RANGE,
        "weight_max_age_days_range": preference_contracts.WEIGHT_MAX_AGE_DAYS_RANGE,
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
    await health_profile_service.set_subject_timezone_if_valid(
        db,
        subject_id=identity.subject_id,
        timezone=zone,
    )
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
    from vitals.services.external_api import tokens as external_tokens

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

    from vitals.services.authentication import mcp_tokens
    from vitals.services.identity.queries import find_user_id_by_username

    user_id = await find_user_id_by_username(
        db,
        username=actor_username,
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
