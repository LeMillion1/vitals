"""Application-only environment-file boundary.

Production Compose keeps database-owner and infrastructure credentials in the
host ``.env`` file.  The web process receives a separate ``.env.runtime`` file
containing only settings it is allowed to consume.  Keeping the allowlist here
makes a newly introduced setting an explicit security review instead of an
implicit copy of every operator secret.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path


RUNTIME_ENV_KEYS = frozenset(
    {
        "VITALS_AUTH_PASSWORD_HASH",
        "VITALS_AUTH_USERNAME",
        "VITALS_BODY_FAT_SOURCE",
        "VITALS_COOKIE_SAMESITE",
        "VITALS_COOKIE_SECURE",
        "VITALS_CREDENTIAL_KEY",
        "VITALS_DATABASE_URL",
        "VITALS_DB_MAX_OVERFLOW",
        "VITALS_DB_POOL_RECYCLE",
        "VITALS_DB_POOL_SIZE",
        "VITALS_DB_POOL_TIMEOUT",
        "VITALS_DB_STATEMENT_TIMEOUT_MS",
        "VITALS_EXTERNAL_API_TOKEN",
        "VITALS_GARMIN_EMAIL",
        "VITALS_GARMIN_PASSWORD",
        "VITALS_GARMIN_TOKEN_DIR",
        "VITALS_HEIGHT_CM",
        "VITALS_HEVY_API_KEY",
        "VITALS_HEVY_BASE_URL",
        "VITALS_LLM_MODEL_BRIEF",
        "VITALS_LLM_MODEL_DIGEST",
        "VITALS_LLM_MODEL_PARSER",
        "VITALS_MCP_CLIENT_ID",
        "VITALS_MCP_CLIENT_SECRET",
        "VITALS_MCP_REDIRECT_HOSTS",
        "VITALS_NUTRITION_CALORIES_MAX",
        "VITALS_NUTRITION_CALORIES_MIN",
        "VITALS_NUTRITION_PROTEIN_TARGET_G",
        "VITALS_OIDC_BOOTSTRAP_SUBJECT",
        "VITALS_OIDC_CLIENT_ID",
        "VITALS_OIDC_CLIENT_SECRET",
        "VITALS_OIDC_ISSUER",
        "VITALS_OIDC_REDIRECT_URL",
        "VITALS_OPENROUTER_API_KEY",
        "VITALS_OPENROUTER_BASE_URL",
        "VITALS_OPENROUTER_HTTP_REFERER",
        "VITALS_OPENROUTER_X_TITLE",
        "VITALS_PRIVATE_FILE_ROOT",
        "VITALS_PROCESS_MODE",
        "VITALS_PUBLIC_URL",
        "VITALS_REDIS_URL",
        "VITALS_REGISTRATION_UNLOCKED",
        "VITALS_SESSION_SECRET",
        "VITALS_SESSION_TTL",
        "VITALS_SEX",
        "VITALS_TIMEZONE",
        "VITALS_USER_AGE",
        "VITALS_USER_GOALS",
        "VITALS_USER_PROGRAM",
        "VITALS_WEB_PUSH_ENABLED",
        "VITALS_WEB_PUSH_VAPID_PRIVATE_KEY",
        "VITALS_WEB_PUSH_VAPID_PUBLIC_KEY",
        "VITALS_WEB_PUSH_VAPID_SUBJECT",
    }
)

PRIVILEGED_ENV_KEYS = frozenset(
    {
        "DATABASE_URL",
        "PGPASSWORD",
        "POSTGRES_PASSWORD",
        "VITALS_DB_PASSWORD",
        "VITALS_IDP_ADMIN_PASSWORD",
        "VITALS_IDP_DB_PASSWORD",
        "VITALS_IDP_MASTERKEY",
        "VITALS_MIGRATION_DATABASE_URL",
        "VITALS_WORKER_DATABASE_URL",
    }
)

_ASSIGNMENT = re.compile(
    r"^(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*="
)


class RuntimeEnvIsolationError(RuntimeError):
    """The application environment would cross the operator privilege boundary."""


def runtime_environment_path(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """The dotenv file mounted into a long-lived application process."""

    values = os.environ if environ is None else environ
    configured = (values.get("VITALS_ENV_FILE") or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / ".env"


def require_runtime_environment_isolation(
    environ: Mapping[str, str] | None = None,
) -> None:
    """Run the Compose privilege preflight when the process requires it."""

    values = os.environ if environ is None else environ
    required = (values.get("VITALS_RUNTIME_ENV_ISOLATION_REQUIRED") or "").strip()
    if required.lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        validate_runtime_environment(
            runtime_environment_path(values),
            environ=values,
        )
    except RuntimeEnvIsolationError as exc:
        raise RuntimeError(f"runtime environment isolation failed: {exc}") from exc


def parse_assignment_lines(path: Path) -> list[tuple[str, str]]:
    """Return assignment keys and their original lines without exposing values."""

    if not path.is_file():
        raise RuntimeEnvIsolationError("application runtime environment file is missing")
    assignments: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(keepends=True), start=1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(stripped)
        if match is None:
            raise RuntimeEnvIsolationError(
                f"unsupported runtime environment syntax on line {line_number}"
            )
        key = match.group("key")
        if key in seen:
            raise RuntimeEnvIsolationError(
                f"duplicate runtime environment key: {key}"
            )
        seen.add(key)
        assignments.append((key, line if line.endswith(("\n", "\r")) else line + "\n"))
    return assignments


def validate_runtime_environment(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Fail closed unless the file and process contain application authority only."""

    assignments = parse_assignment_lines(path)
    keys = frozenset(key for key, _line in assignments)
    unsupported = sorted(keys - RUNTIME_ENV_KEYS)
    if unsupported:
        raise RuntimeEnvIsolationError(
            "application runtime environment contains non-runtime keys: "
            + ", ".join(unsupported)
        )
    process_environment = os.environ if environ is None else environ
    privileged_process_keys = sorted(PRIVILEGED_ENV_KEYS.intersection(process_environment))
    if privileged_process_keys:
        raise RuntimeEnvIsolationError(
            "application process contains privileged environment keys: "
            + ", ".join(privileged_process_keys)
        )
    if "VITALS_DATABASE_URL" not in keys:
        raise RuntimeEnvIsolationError(
            "application runtime environment is missing VITALS_DATABASE_URL"
        )
    return keys
