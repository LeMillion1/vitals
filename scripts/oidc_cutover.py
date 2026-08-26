#!/usr/bin/env python3
"""Atomically switch the production runtime between password and OIDC auth.

Run this command through a one-off ``vitals_app`` container while the long-lived
web service is stopped.  The worker mounts the runtime directory read-only and
does not participate in authentication.  Every mutation replaces the complete
OIDC group and the session-signing secret in one owner-only filesystem update;
no secret value is written to stdout or stderr.

The initial enable path proves three things before publishing configuration:
the provider metadata matches the intended issuer, the existing database has
the exact safe one-owner bootstrap graph, and the callback is the public Vitals
origin plus ``/auth/callback``.  ``finalize`` removes the one-time provider
subject only after that exact identity is durably linked to the active local
owner.  ``rollback`` clears the complete OIDC group and rotates every signed
Vitals browser/MCP credential before password mode can start again.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vitals.runtime_env import (  # noqa: E402
    RuntimeEnvIsolationError,
    read_env_key,
    validate_runtime_environment,
    write_env_keys,
)
from vitals.services.authentication.oidc import (  # noqa: E402
    OidcDiscoveryError,
    OidcError,
    OidcProvider,
    OidcSettings,
)
from scripts.registration_gate import read_gate_state  # noqa: E402


CONTAINER_RUNTIME_ENV = Path("/run/vitals-runtime/vitals.env")
DEFAULT_RUNTIME_ENV = CONTAINER_RUNTIME_ENV
ENABLE_CONFIRMATION = "WEB STOPPED; ENABLE OIDC AND ROTATE SESSIONS"
FINALIZE_CONFIRMATION = "OWNER OIDC BINDING VERIFIED; REMOVE BOOTSTRAP"
ROLLBACK_CONFIRMATION = "WEB STOPPED; RESTORE PASSWORD MODE AND ROTATE SESSIONS"
RETIRE_CONFIRMATION = "OIDC RECOVERY VERIFIED; RETIRE PASSWORD BRIDGE AND ROTATE SESSIONS"

OIDC_REQUIRED_KEYS = (
    "VITALS_OIDC_ISSUER",
    "VITALS_OIDC_CLIENT_ID",
    "VITALS_OIDC_CLIENT_SECRET",
    "VITALS_OIDC_REDIRECT_URL",
)
OIDC_BOOTSTRAP_KEY = "VITALS_OIDC_BOOTSTRAP_SUBJECT"
LEGACY_AUTH_KEYS = ("VITALS_AUTH_USERNAME", "VITALS_AUTH_PASSWORD_HASH")
SESSION_SECRET_KEY = "VITALS_SESSION_SECRET"


class OidcCutoverError(RuntimeError):
    """A cutover, recovery, or readback gate failed."""


def _parse_not_before(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise OidcCutoverError("login-proof timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OidcCutoverError("login-proof timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _read_values(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    return {
        key: read_env_key(
            path,
            key,
            require_existing=True,
            require_owner_only=True,
        ).strip()
        for key in keys
    }


def _runtime_state(path: Path) -> tuple[str, dict[str, str]]:
    validate_runtime_environment(path, environ={})
    keys = OIDC_REQUIRED_KEYS + (OIDC_BOOTSTRAP_KEY,)
    values = _read_values(path, keys)
    present = [bool(values[key]) for key in OIDC_REQUIRED_KEYS]
    if any(present) and not all(present):
        raise OidcCutoverError(
            "runtime has a partial OIDC group; restore all required values or clear all"
        )
    if not any(present):
        if values[OIDC_BOOTSTRAP_KEY]:
            raise OidcCutoverError("runtime has an OIDC bootstrap subject without a provider group")
        return "password", values
    return (
        "oidc_bootstrap_pending" if values[OIDC_BOOTSTRAP_KEY] else "oidc_bound",
        values,
    )


def _validate_private_secret_file(path: Path, *, label: str = "secret") -> str:
    """Read one owner-only line without following either path component."""

    parent_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    parent_flags |= getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_descriptor = os.open(path.parent, parent_flags)
    except OSError as exc:
        raise OidcCutoverError(f"{label} parent is not a safe directory") from exc
    descriptor = -1
    try:
        parent_stat = os.fstat(parent_descriptor)
        if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) != 0o700:
            raise OidcCutoverError(
                f"{label} directory must belong to the current user with mode 0700"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OidcCutoverError(f"{label} must be a regular file")
        if file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise OidcCutoverError(f"{label} file must belong to the current user with mode 0600")
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
            descriptor = -1
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise OidcCutoverError(f"{label} is unexpectedly large")
        lines = raw.splitlines()
        if (
            len(lines) != 1
            or not lines[0]
            or lines[0] != lines[0].strip()
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in lines[0])
        ):
            raise OidcCutoverError(f"{label} file must contain one non-empty line")
        return lines[0]
    except OidcCutoverError:
        raise
    except OSError as exc:
        raise OidcCutoverError(f"could not read the {label} file safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _validate_public_callback(*, runtime_env: Path, issuer: str, redirect_url: str) -> None:
    public_url = read_env_key(
        runtime_env,
        "VITALS_PUBLIC_URL",
        require_existing=True,
        require_owner_only=True,
    ).strip()
    if not public_url:
        raise OidcCutoverError("runtime has no VITALS_PUBLIC_URL")
    if issuer.endswith("/"):
        raise OidcCutoverError("OIDC issuer must not have a trailing slash")
    parsed_issuer = urlsplit(issuer)
    if parsed_issuer.path not in ("", "/"):
        raise OidcCutoverError("production OIDC issuer must be an origin without a path")
    if parsed_issuer.scheme != "https":
        raise OidcCutoverError("production OIDC issuer must use https")
    expected_redirect = public_url.rstrip("/") + "/auth/callback"
    if not hmac.compare_digest(redirect_url, expected_redirect):
        raise OidcCutoverError(
            "OIDC redirect must exactly equal VITALS_PUBLIC_URL plus /auth/callback"
        )


async def _validate_provider(settings: OidcSettings) -> None:
    try:
        metadata = await OidcProvider(settings).metadata()
    except OidcDiscoveryError as exc:
        raise OidcCutoverError(f"provider discovery failed: {exc}") from exc
    methods = metadata.get("token_endpoint_auth_methods_supported")
    if not isinstance(methods, list) or "client_secret_post" not in methods:
        raise OidcCutoverError(
            "provider metadata must explicitly permit client_secret_post token authentication"
        )
    challenges = metadata.get("code_challenge_methods_supported")
    if not isinstance(challenges, list) or "S256" not in challenges:
        raise OidcCutoverError("provider metadata must explicitly permit S256 PKCE")
    logout_endpoint = metadata.get("end_session_endpoint")
    if not isinstance(logout_endpoint, str) or not logout_endpoint.strip():
        raise OidcCutoverError("provider metadata has no end_session_endpoint for upstream logout")


async def _validate_database_bootstrap(
    *, runtime_env: Path, issuer: str, bootstrap_subject: str
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from vitals.services.authentication import registration as registration_service
    from vitals.services.authentication.startup import validate_oidc_startup_state

    if read_gate_state(runtime_env) != "locked":
        raise OidcCutoverError("account registration must be deployment-locked during OIDC cutover")

    database_url = read_env_key(
        runtime_env,
        "VITALS_DATABASE_URL",
        require_existing=True,
        require_owner_only=True,
    ).strip()
    engine = None
    try:
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            stored_mode = await registration_service.get_stored_mode(session)
            if stored_mode is not registration_service.RegistrationMode.DISABLED:
                raise OidcCutoverError(
                    "stored account registration mode must be disabled during OIDC cutover"
                )
            await validate_oidc_startup_state(
                session,
                issuer=issuer,
                bootstrap_subject=bootstrap_subject,
            )
    except OidcCutoverError:
        raise
    except Exception as exc:
        raise OidcCutoverError("database refused the proposed OIDC bootstrap") from exc
    finally:
        if engine is not None:
            await engine.dispose()


async def _validate_password_rollback(
    *,
    runtime_env: Path,
    legacy_password: str,
) -> None:
    """Refuse the single-user password bridge after a second account exists."""

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from vitals.services.authentication import registration as registration_service
    from vitals.services.authentication.startup import (
        require_oidc_owner_bootstrap_graph,
    )
    from vitals.services.identity_service import normalize_username
    from vitals.utils.passwords import verify_password

    if read_gate_state(runtime_env) != "locked":
        raise OidcCutoverError(
            "account registration must be deployment-locked before password rollback"
        )
    database_url = read_env_key(
        runtime_env,
        "VITALS_DATABASE_URL",
        require_existing=True,
        require_owner_only=True,
    ).strip()
    legacy = _read_values(runtime_env, LEGACY_AUTH_KEYS)
    if not verify_password(legacy_password, legacy["VITALS_AUTH_PASSWORD_HASH"]):
        raise OidcCutoverError("legacy password proof does not match the rollback hash")
    try:
        normalized_username = normalize_username(legacy["VITALS_AUTH_USERNAME"])
    except ValueError as exc:
        raise OidcCutoverError("legacy rollback username is invalid") from exc
    engine = None
    try:
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            stored_mode = await registration_service.get_stored_mode(session)
            if stored_mode is not registration_service.RegistrationMode.DISABLED:
                raise OidcCutoverError(
                    "stored account registration mode must be disabled before password rollback"
                )
            owner = await require_oidc_owner_bootstrap_graph(session)
            if owner.normalized_username != normalized_username.lookup_key or (
                not isinstance(owner.password_hash, str)
                or not hmac.compare_digest(
                    owner.password_hash,
                    legacy["VITALS_AUTH_PASSWORD_HASH"],
                )
            ):
                raise OidcCutoverError(
                    "legacy rollback credentials do not match the sole durable owner"
                )
    except OidcCutoverError:
        raise
    except Exception as exc:
        raise OidcCutoverError(
            "database refused rollback to the single-user password bridge"
        ) from exc
    finally:
        if engine is not None:
            await engine.dispose()


async def _revoke_subject_mcp_tokens(
    session,
    *,
    subject_id,
    revoked_at: datetime,
) -> int:
    """Revoke one subject's live connectors inside the caller's transaction."""

    import sqlalchemy as sa

    from vitals.models.identity import McpAccessToken
    from vitals.persistence.rls import SUBJECT_SETTING

    if session.get_bind().dialect.name == "postgresql":
        # The web database role has no installation-wide RLS capability.  Move
        # the transaction-local subject setting deliberately instead of
        # escalating the cutover helper to the migration or worker role.
        await session.execute(
            sa.text("SELECT set_config(:name, :value, true)"),
            {"name": SUBJECT_SETTING, "value": str(subject_id)},
        )
    result = await session.execute(
        sa.update(McpAccessToken)
        .where(
            McpAccessToken.subject_id == subject_id,
            McpAccessToken.revoked_at.is_(None),
            McpAccessToken.expires_at > revoked_at,
        )
        .values(revoked_at=revoked_at)
        .execution_options(synchronize_session=False)
    )
    remaining = await session.scalar(
        sa.select(sa.func.count())
        .select_from(McpAccessToken)
        .where(
            McpAccessToken.subject_id == subject_id,
            McpAccessToken.revoked_at.is_(None),
            McpAccessToken.expires_at > revoked_at,
        )
    )
    if remaining:
        raise OidcCutoverError("MCP connector revocation did not reach a stable state")
    return int(result.rowcount or 0)


async def _revoke_live_mcp_tokens(*, runtime_env: Path) -> int:
    """Durably disconnect every live MCP connector before session rotation.

    The runtime database role is intentionally subject-scoped.  All subjects
    are therefore updated one at a time under the production RLS setting, but
    within one database transaction.  PostgreSQL also locks the token table so
    no concurrent issuance can slip between the subject inventory and commit.

    This commit precedes the owner-only runtime-file update.  The two stores
    cannot share a physical transaction; if the later file write fails, leaving
    connectors revoked while restoring the previous auth configuration is the
    safe, retryable direction.  The reverse ordering could publish a new signing
    key while durable Settings rows still claimed those connectors were live.
    """

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from vitals.models.identity import HealthSubject

    database_url = read_env_key(
        runtime_env,
        "VITALS_DATABASE_URL",
        require_existing=True,
        require_owner_only=True,
    ).strip()
    engine = None
    try:
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                # SHARE ROW EXCLUSIVE conflicts with token INSERT/UPDATE/DELETE,
                # while remaining available to the restricted web role through
                # its ordinary table privileges.  The host coordinator has
                # already stopped web, but this also makes direct helper use
                # deterministic rather than merely timing-dependent.
                await session.execute(
                    sa.text(
                        "LOCK TABLE mcp_access_tokens IN SHARE ROW EXCLUSIVE MODE"
                    )
                )
                revoked_at = await session.scalar(sa.select(sa.func.current_timestamp()))
            else:
                revoked_at = datetime.now(timezone.utc)
            if revoked_at is None:  # pragma: no cover - database contract
                raise OidcCutoverError("database did not provide an MCP revocation time")
            if revoked_at.tzinfo is None:
                revoked_at = revoked_at.replace(tzinfo=timezone.utc)
            subject_ids = list(
                (
                    await session.scalars(
                        sa.select(HealthSubject.id).order_by(HealthSubject.id)
                    )
                ).all()
            )
            revoked = 0
            for subject_id in subject_ids:
                revoked += await _revoke_subject_mcp_tokens(
                    session,
                    subject_id=subject_id,
                    revoked_at=revoked_at,
                )
        return revoked
    except OidcCutoverError:
        raise
    except Exception as exc:
        raise OidcCutoverError("could not revoke live MCP connectors") from exc
    finally:
        if engine is not None:
            await engine.dispose()


async def _require_bound_owner(
    *,
    runtime_env: Path,
    issuer: str,
    bootstrap_subject: str | None,
    not_before: datetime,
    legacy_credentials: tuple[str, str] | None = None,
    require_password_retired: bool = False,
    retire_password: bool = False,
    allow_already_retired: bool = False,
) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import vitals.models  # noqa: F401 - populate relationship metadata
    from vitals.enums import UserRoleName, UserStatus
    from vitals.models.identity import (
        HealthSubject,
        User,
        UserFederatedIdentity,
        UserRole,
    )
    from vitals.services.authentication import registration as registration_service
    from vitals.services.identity_service import (
        acquire_identity_governance_lock,
        normalize_username,
        retire_password_hash,
    )

    if read_gate_state(runtime_env) != "locked":
        raise OidcCutoverError(
            "account registration must remain deployment-locked during finalization"
        )

    database_url = read_env_key(
        runtime_env,
        "VITALS_DATABASE_URL",
        require_existing=True,
        require_owner_only=True,
    ).strip()
    engine = None
    try:
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await acquire_identity_governance_lock(session)
            stored_mode = await registration_service.get_stored_mode(session)
            if stored_mode is not registration_service.RegistrationMode.DISABLED:
                raise OidcCutoverError(
                    "stored account registration mode must remain disabled during finalization"
                )
            conditions = [
                UserFederatedIdentity.issuer == issuer,
                User.status == UserStatus.ACTIVE.value,
            ]
            if bootstrap_subject is not None:
                conditions.append(UserFederatedIdentity.subject == bootstrap_subject)
            user_ids = set(
                await session.scalars(
                    sa.select(UserFederatedIdentity.user_id)
                    .join(User, User.id == UserFederatedIdentity.user_id)
                    .join(HealthSubject, HealthSubject.owner_user_id == User.id)
                    .where(*conditions)
                )
            )
            if len(user_ids) != 1:
                raise OidcCutoverError(
                    "the exact bootstrap identity is not linked to one active owner"
                )
            user_id = next(iter(user_ids))
            user = await session.get(User, user_id)
            if user is None or user.last_login_at is None:
                raise OidcCutoverError("the bound owner has not completed a federated login")
            last_login_at = user.last_login_at
            if last_login_at.tzinfo is None:
                last_login_at = last_login_at.replace(tzinfo=timezone.utc)
            if last_login_at.astimezone(timezone.utc) < not_before:
                raise OidcCutoverError("the bound owner has not logged in after this cutover")
            roles = set(
                await session.scalars(sa.select(UserRole.role).where(UserRole.user_id == user_id))
            )
            required_roles = {
                UserRoleName.MEMBER.value,
                UserRoleName.PLATFORM_SUPERADMIN.value,
            }
            if not required_roles.issubset(roles):
                raise OidcCutoverError("the bound owner lacks member or platform_superadmin")
            if legacy_credentials is not None:
                legacy_username, legacy_hash = legacy_credentials
                normalized = normalize_username(legacy_username)
                if user.normalized_username != normalized.lookup_key:
                    raise OidcCutoverError(
                        "legacy password identity does not match the bound owner"
                    )
                if user.password_hash is None and not allow_already_retired:
                    raise OidcCutoverError("durable legacy password verifier is already absent")
                if user.password_hash is not None:
                    if not hmac.compare_digest(user.password_hash, legacy_hash):
                        raise OidcCutoverError(
                            "legacy password hash changed after cutover preflight"
                        )
                if retire_password:
                    await retire_password_hash(
                        session,
                        user_id=user.id,
                        expected_current_hash=legacy_hash,
                        actor_user_id=None,
                        allow_already_retired=allow_already_retired,
                    )
            elif require_password_retired:
                await retire_password_hash(
                    session,
                    user_id=user.id,
                    expected_current_hash="",
                    actor_user_id=None,
                    allow_already_retired=allow_already_retired,
                )
    except OidcCutoverError:
        raise
    except Exception as exc:
        raise OidcCutoverError("could not verify the bound owner") from exc
    finally:
        if engine is not None:
            await engine.dispose()


def _readback_matches(path: Path, expected: dict[str, str]) -> bool:
    actual = _read_values(path, tuple(expected))
    return all(hmac.compare_digest(actual[key], value) for key, value in expected.items())


def _publish_runtime_state(
    path: Path,
    updates: dict[str, str],
    *,
    expected_state: str,
) -> None:
    """Publish one auth transition or restore its exact previous values."""

    previous = _read_values(path, tuple(updates))
    try:
        write_env_keys(
            path,
            updates,
            require_existing=True,
            require_owner_only=True,
        )
        if not _readback_matches(path, updates):
            raise OidcCutoverError("runtime readback did not match the atomic update")
        state, _values = _runtime_state(path)
        if state != expected_state:
            raise OidcCutoverError(f"runtime did not enter expected auth state {expected_state}")
    except Exception as transition_error:
        try:
            write_env_keys(
                path,
                previous,
                require_existing=True,
                require_owner_only=True,
            )
            if not _readback_matches(path, previous):
                raise OidcCutoverError("previous runtime readback did not match")
        except Exception as recovery_error:
            raise OidcCutoverError(
                "auth transition failed and runtime recovery is ambiguous; keep web "
                "stopped and restore the owner-only runtime file from escrow"
            ) from recovery_error
        raise OidcCutoverError(
            "auth transition failed; the previous runtime values were restored"
        ) from transition_error


def _emit(payload: dict[str, object], *, error: bool = False) -> None:
    print(json.dumps(payload, sort_keys=True), file=sys.stderr if error else sys.stdout)


async def _enable_preflight(args: argparse.Namespace) -> dict[str, str]:
    state, _values = _runtime_state(args.runtime_env)
    if state != "password":
        raise OidcCutoverError("OIDC can be enabled only from password mode")
    legacy = _read_values(args.runtime_env, LEGACY_AUTH_KEYS)
    if not all(legacy.values()):
        raise OidcCutoverError("password rollback credentials must remain present through cutover")
    client_secret = _validate_private_secret_file(
        args.client_secret_file, label="OIDC client secret"
    )
    legacy_password = _validate_private_secret_file(
        args.legacy_password_file, label="legacy password proof"
    )
    from vitals.utils.passwords import verify_password

    if not verify_password(legacy_password, legacy["VITALS_AUTH_PASSWORD_HASH"]):
        raise OidcCutoverError("legacy password proof does not match the rollback hash")
    proposed = {
        "VITALS_OIDC_ISSUER": args.issuer.strip(),
        "VITALS_OIDC_CLIENT_ID": args.client_id.strip(),
        "VITALS_OIDC_CLIENT_SECRET": client_secret,
        "VITALS_OIDC_REDIRECT_URL": args.redirect_url.strip(),
        OIDC_BOOTSTRAP_KEY: args.bootstrap_subject.strip(),
    }
    if not all(proposed.values()):
        raise OidcCutoverError("the complete OIDC group and bootstrap subject are required")
    settings = OidcSettings(
        issuer=proposed["VITALS_OIDC_ISSUER"],
        client_id=proposed["VITALS_OIDC_CLIENT_ID"],
        client_secret=proposed["VITALS_OIDC_CLIENT_SECRET"],
        redirect_url=proposed["VITALS_OIDC_REDIRECT_URL"],
    )
    _validate_public_callback(
        runtime_env=args.runtime_env,
        issuer=settings.issuer,
        redirect_url=settings.redirect_url,
    )
    await _validate_provider(settings)
    await _validate_database_bootstrap(
        runtime_env=args.runtime_env,
        issuer=settings.issuer,
        bootstrap_subject=proposed[OIDC_BOOTSTRAP_KEY],
    )
    return proposed


async def _preflight(args: argparse.Namespace) -> dict[str, object]:
    await _enable_preflight(args)
    return {
        "operation": "oidc_cutover_preflight",
        "readback": "password",
        "registration": "locked_disabled",
        "result": "ok",
    }


async def _enable(args: argparse.Namespace) -> dict[str, object]:
    if args.confirm != ENABLE_CONFIRMATION:
        raise OidcCutoverError(f"refusing enable without exact --confirm {ENABLE_CONFIRMATION!r}")
    proposed = await _enable_preflight(args)
    updates = dict(proposed)
    updates[SESSION_SECRET_KEY] = secrets.token_urlsafe(64)
    revoked_connectors = await _revoke_live_mcp_tokens(runtime_env=args.runtime_env)
    _publish_runtime_state(
        args.runtime_env,
        updates,
        expected_state="oidc_bootstrap_pending",
    )
    state, _values = _runtime_state(args.runtime_env)
    return {
        "next_action": (
            "recreate and health-check only vitals_app, reconnect MCP clients, "
            "then complete the owner login"
        ),
        "operation": "oidc_cutover_enable",
        "readback": state,
        "result": "ok",
        "mcp_connectors_revoked": revoked_connectors,
        "session_secret_rotated": True,
    }


async def _finalize(args: argparse.Namespace) -> dict[str, object]:
    state, values = _runtime_state(args.runtime_env)
    if state != "oidc_bootstrap_pending":
        raise OidcCutoverError("OIDC bootstrap can be finalized only while pending")
    if args.confirm != FINALIZE_CONFIRMATION:
        raise OidcCutoverError(
            f"refusing finalize without exact --confirm {FINALIZE_CONFIRMATION!r}"
        )
    await _require_bound_owner(
        runtime_env=args.runtime_env,
        issuer=values["VITALS_OIDC_ISSUER"],
        bootstrap_subject=values[OIDC_BOOTSTRAP_KEY],
        not_before=_parse_not_before(args.not_before),
    )
    _publish_runtime_state(
        args.runtime_env,
        {OIDC_BOOTSTRAP_KEY: ""},
        expected_state="oidc_bound",
    )
    state, _values = _runtime_state(args.runtime_env)
    return {
        "next_action": "recreate and health-check only vitals_app",
        "operation": "oidc_cutover_finalize",
        "readback": state,
        "result": "ok",
    }


async def _rollback(args: argparse.Namespace) -> dict[str, object]:
    state, _values = _runtime_state(args.runtime_env)
    if state == "password":
        raise OidcCutoverError("runtime is already in password mode")
    if args.confirm != ROLLBACK_CONFIRMATION:
        raise OidcCutoverError(
            f"refusing rollback without exact --confirm {ROLLBACK_CONFIRMATION!r}"
        )
    legacy = _read_values(args.runtime_env, LEGACY_AUTH_KEYS)
    if not all(legacy.values()):
        raise OidcCutoverError("cannot restore password mode without both legacy credentials")
    legacy_password = _validate_private_secret_file(
        args.legacy_password_file, label="legacy password proof"
    )
    await _validate_password_rollback(
        runtime_env=args.runtime_env,
        legacy_password=legacy_password,
    )
    updates = {key: "" for key in OIDC_REQUIRED_KEYS + (OIDC_BOOTSTRAP_KEY,)}
    updates[SESSION_SECRET_KEY] = secrets.token_urlsafe(64)
    revoked_connectors = await _revoke_live_mcp_tokens(runtime_env=args.runtime_env)
    _publish_runtime_state(
        args.runtime_env,
        updates,
        expected_state="password",
    )
    state, _values = _runtime_state(args.runtime_env)
    return {
        "next_action": "recreate and health-check only vitals_app; reconnect every MCP client",
        "operation": "oidc_cutover_rollback",
        "readback": state,
        "result": "ok",
        "mcp_connectors_revoked": revoked_connectors,
        "session_secret_rotated": True,
    }


async def _password_preflight(args: argparse.Namespace) -> dict[str, object]:
    state, _values = _runtime_state(args.runtime_env)
    if state != "password":
        raise OidcCutoverError("password recovery proof requires password mode")
    legacy_password = _validate_private_secret_file(
        args.legacy_password_file, label="legacy password proof"
    )
    await _validate_password_rollback(
        runtime_env=args.runtime_env,
        legacy_password=legacy_password,
    )
    return {
        "operation": "oidc_cutover_password_preflight",
        "readback": "password",
        "result": "ok",
    }


async def _retire_legacy(args: argparse.Namespace) -> dict[str, object]:
    state, values = _runtime_state(args.runtime_env)
    if state != "oidc_bound":
        raise OidcCutoverError(
            "legacy password material can be retired only from finalized OIDC mode"
        )
    if args.confirm != RETIRE_CONFIRMATION:
        raise OidcCutoverError(
            f"refusing retirement without exact --confirm {RETIRE_CONFIRMATION!r}"
        )
    legacy = _read_values(args.runtime_env, LEGACY_AUTH_KEYS)
    legacy_present = [bool(legacy[key]) for key in LEGACY_AUTH_KEYS]
    if any(legacy_present) and not all(legacy_present):
        raise OidcCutoverError("legacy password material is only partially present")
    if not any(legacy_present) and not args.allow_already_retired:
        raise OidcCutoverError(
            "already-absent legacy password material requires journaled recovery"
        )
    await _require_bound_owner(
        runtime_env=args.runtime_env,
        issuer=values["VITALS_OIDC_ISSUER"],
        bootstrap_subject=None,
        not_before=_parse_not_before(args.not_before),
        legacy_credentials=(
            legacy["VITALS_AUTH_USERNAME"],
            legacy["VITALS_AUTH_PASSWORD_HASH"],
        )
        if all(legacy_present)
        else None,
        require_password_retired=True,
        retire_password=True,
        allow_already_retired=args.allow_already_retired,
    )
    if not any(legacy_present):
        return {
            "already_retired": True,
            "next_action": "recreate and health-check only vitals_app",
            "operation": "oidc_cutover_retire_legacy",
            "readback": "oidc_bound",
            "result": "ok",
            "session_secret_rotated": False,
        }
    updates = {key: "" for key in LEGACY_AUTH_KEYS}
    updates[SESSION_SECRET_KEY] = secrets.token_urlsafe(64)
    revoked_connectors = await _revoke_live_mcp_tokens(runtime_env=args.runtime_env)
    _publish_runtime_state(
        args.runtime_env,
        updates,
        expected_state="oidc_bound",
    )
    return {
        "next_action": (
            "recreate and health-check only vitals_app; reconnect every browser and MCP client"
        ),
        "operation": "oidc_cutover_retire_legacy",
        "readback": "oidc_bound",
        "result": "ok",
        "already_retired": False,
        "mcp_connectors_revoked": revoked_connectors,
        "session_secret_rotated": True,
    }


async def _retire_preflight(args: argparse.Namespace) -> dict[str, object]:
    state, values = _runtime_state(args.runtime_env)
    if state != "oidc_bound":
        raise OidcCutoverError("legacy password retirement requires finalized OIDC mode")
    legacy = _read_values(args.runtime_env, LEGACY_AUTH_KEYS)
    legacy_present = [bool(legacy[key]) for key in LEGACY_AUTH_KEYS]
    if any(legacy_present) and not all(legacy_present):
        raise OidcCutoverError("legacy password material is only partially present")
    await _require_bound_owner(
        runtime_env=args.runtime_env,
        issuer=values["VITALS_OIDC_ISSUER"],
        bootstrap_subject=None,
        not_before=_parse_not_before(args.not_before),
        legacy_credentials=(
            legacy["VITALS_AUTH_USERNAME"],
            legacy["VITALS_AUTH_PASSWORD_HASH"],
        )
        if all(legacy_present)
        else None,
        require_password_retired=not any(legacy_present),
        retire_password=False,
    )
    return {
        "operation": "oidc_cutover_retire_preflight",
        "readback": "oidc_bound",
        "result": "ok",
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-env",
        type=Path,
        default=DEFAULT_RUNTIME_ENV,
        help="owner-only application runtime file (default: %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("status", help="read the current auth mode without mutation")

    def add_provider_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--issuer", required=True)
        command.add_argument("--client-id", required=True)
        command.add_argument("--client-secret-file", required=True, type=Path)
        command.add_argument("--legacy-password-file", required=True, type=Path)
        command.add_argument("--redirect-url", required=True)
        command.add_argument("--bootstrap-subject", required=True)

    preflight = subparsers.add_parser(
        "preflight", help="validate provider and local bootstrap without mutation"
    )
    add_provider_arguments(preflight)

    enable = subparsers.add_parser("enable", help="atomically enable OIDC")
    add_provider_arguments(enable)
    enable.add_argument("--confirm")

    finalize = subparsers.add_parser(
        "finalize", help="remove the bootstrap subject after the first binding"
    )
    finalize.add_argument("--not-before", required=True)
    finalize.add_argument("--confirm")

    rollback = subparsers.add_parser("rollback", help="atomically restore password mode")
    rollback.add_argument("--legacy-password-file", required=True, type=Path)
    rollback.add_argument("--confirm")
    password_preflight = subparsers.add_parser("password-preflight", help=argparse.SUPPRESS)
    password_preflight.add_argument("--legacy-password-file", required=True, type=Path)
    retire_preflight = subparsers.add_parser(
        "retire-preflight",
        help="verify a fresh owner login before stopping web for retirement",
    )
    retire_preflight.add_argument("--not-before", required=True)
    retire = subparsers.add_parser(
        "retire-legacy",
        help="remove legacy password material after verified OIDC recovery",
    )
    retire.add_argument("--not-before", required=True)
    retire.add_argument("--allow-already-retired", action="store_true", help=argparse.SUPPRESS)
    retire.add_argument("--confirm")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    try:
        if args.operation == "status":
            state, _values = _runtime_state(args.runtime_env)
            payload = {
                "operation": "oidc_cutover_status",
                "readback": state,
                "result": "ok",
            }
        elif args.operation == "preflight":
            payload = await _preflight(args)
        elif args.operation == "enable":
            payload = await _enable(args)
        elif args.operation == "finalize":
            payload = await _finalize(args)
        elif args.operation == "rollback":
            payload = await _rollback(args)
        elif args.operation == "password-preflight":
            payload = await _password_preflight(args)
        elif args.operation == "retire-preflight":
            payload = await _retire_preflight(args)
        elif args.operation == "retire-legacy":
            payload = await _retire_legacy(args)
        else:  # pragma: no cover - argparse owns this boundary
            raise OidcCutoverError("unsupported operation")
    except (
        OSError,
        OidcCutoverError,
        OidcError,
        RuntimeEnvIsolationError,
        TypeError,
        ValueError,
    ) as exc:
        _emit(
            {
                "operation": f"oidc_cutover_{args.operation}",
                "reason": str(exc),
                "result": "error",
            },
            error=True,
        )
        return 2
    except Exception:
        _emit(
            {
                "operation": f"oidc_cutover_{args.operation}",
                "reason": "unexpected cutover failure",
                "result": "error",
            },
            error=True,
        )
        return 2
    _emit(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
