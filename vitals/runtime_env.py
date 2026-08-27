"""Application-only environment-file boundary.

Production Compose keeps database-owner and infrastructure credentials in the
host ``.env`` file.  The long-lived processes receive a separate
``.vitals-runtime/vitals.env`` file containing only settings they are allowed to
consume.  Keeping the allowlist here makes a newly introduced setting an
explicit security review instead of an implicit copy of every operator secret.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
import threading
from collections.abc import Mapping
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values


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
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WRITE_LOCK = threading.Lock()


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


def _open_runtime_parent(path: Path, *, require_owner_only: bool) -> int:
    """Open and anchor the real directory containing *path*."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.parent, flags)
    parent_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(parent_stat.st_mode):
        os.close(descriptor)
        raise OSError(
            errno.ENOTDIR,
            f"Runtime env parent is not a real directory: {path.parent}",
            path.parent,
        )
    if require_owner_only:
        if parent_stat.st_uid != os.geteuid():
            os.close(descriptor)
            raise RuntimeEnvIsolationError(
                "application runtime environment directory must belong to the "
                "current user"
            )
        if stat.S_IMODE(parent_stat.st_mode) != 0o700:
            os.close(descriptor)
            raise RuntimeEnvIsolationError(
                "application runtime environment directory must have mode 0700"
            )
    return descriptor


def _read_runtime_lines(
    path: Path,
    *,
    parent_descriptor: int,
    require_existing: bool,
    require_owner_only: bool,
) -> list[str]:
    """Read an existing regular runtime file without following a symlink."""

    try:
        path_stat = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if require_existing:
            raise RuntimeEnvIsolationError(
                "application runtime environment file is missing"
            ) from None
        return []
    if not stat.S_ISREG(path_stat.st_mode):
        raise OSError(
            errno.EINVAL,
            f"Refusing to rewrite non-regular env file: {path}",
            path,
        )
    if require_owner_only:
        if path_stat.st_uid != os.geteuid():
            raise RuntimeEnvIsolationError(
                "application runtime environment file must belong to the current user"
            )
        if stat.S_IMODE(path_stat.st_mode) != 0o600:
            raise RuntimeEnvIsolationError(
                "application runtime environment file must have mode 0600"
            )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise OSError(f"Refusing to rewrite non-regular env file: {path}")
        if require_owner_only and (
            opened_stat.st_uid != path_stat.st_uid
            or stat.S_IMODE(opened_stat.st_mode) != 0o600
        ):
            raise RuntimeEnvIsolationError(
                "application runtime environment changed during validation"
            )
        with os.fdopen(
            descriptor,
            mode="r",
            encoding="utf-8",
            newline="",
        ) as env_file:
            descriptor = -1
            return env_file.readlines()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_owner_only_write(
    path: Path,
    content: str,
    *,
    parent_descriptor: int,
) -> None:
    """Atomically publish *content* from a unique mode-0600 sibling file."""

    descriptor = -1
    temporary_name: str | None = None
    try:
        temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(
            descriptor,
            mode="w",
            encoding="utf-8",
            newline="",
        ) as env_file:
            descriptor = -1
            env_file.write(content)
            env_file.flush()
            os.fsync(env_file.fileno())
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = None
        try:
            os.fsync(parent_descriptor)
        except OSError as exc:
            unsupported = {errno.EINVAL, errno.EBADF}
            if hasattr(errno, "ENOTSUP"):
                unsupported.add(errno.ENOTSUP)
            if exc.errno not in unsupported:
                raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass


def read_env_key(
    path: Path,
    key: str,
    *,
    require_existing: bool = False,
    require_owner_only: bool = False,
) -> str:
    """Read one assignment without following the file or parent symlinks."""

    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        raise ValueError(f"Invalid environment key: {key!r}")
    try:
        parent_descriptor = _open_runtime_parent(
            path,
            require_owner_only=require_owner_only,
        )
    except FileNotFoundError:
        return ""
    try:
        lines = _read_runtime_lines(
            path,
            parent_descriptor=parent_descriptor,
            require_existing=require_existing or require_owner_only,
            require_owner_only=require_owner_only,
        )
    finally:
        os.close(parent_descriptor)
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(stripped)
        if match is not None and match.group("key") == key:
            parsed = dotenv_values(stream=StringIO(stripped), interpolate=False)
            value = parsed.get(key)
            if value is None:
                raise RuntimeEnvIsolationError(
                    f"application runtime environment has an invalid value for {key}"
                )
            return value
    return ""


def write_env_keys(
    path: Path,
    updates: Mapping[str, str],
    *,
    require_existing: bool = False,
    require_owner_only: bool = False,
) -> None:
    """Atomically update assignments while preserving comments and ordering.

    ``require_owner_only`` is for host-operator workflows: it additionally
    requires a current-user-owned mode-0700 parent and an existing
    current-user-owned mode-0600 runtime file. The web compatibility wrapper
    intentionally leaves that option off because local development may use a
    repository-level ``.env``.
    """

    for key, value in updates.items():
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise ValueError(f"Invalid environment key: {key!r}")
        if not isinstance(value, str):
            raise TypeError(f"Value for {key!r} must be a string")
        if "\n" in value or "\r" in value:
            raise ValueError(f"Value for {key!r} contains a newline character")

    with _WRITE_LOCK:
        parent_descriptor = _open_runtime_parent(
            path,
            require_owner_only=require_owner_only,
        )
        try:
            lines = _read_runtime_lines(
                path,
                parent_descriptor=parent_descriptor,
                require_existing=require_existing or require_owner_only,
                require_owner_only=require_owner_only,
            )
            remaining = set(updates)
            new_lines: list[str] = []
            for line in lines:
                stripped = line.strip()
                if not stripped.startswith("#"):
                    match = _ASSIGNMENT.match(stripped)
                    candidate = match.group("key") if match is not None else None
                    if candidate in remaining:
                        newline = "\r\n" if line.endswith("\r\n") else "\n"
                        new_lines.append(
                            f"{candidate}={updates[candidate]}{newline}"
                        )
                        remaining.discard(candidate)
                        continue
                new_lines.append(line)
            for key in sorted(remaining):
                new_lines.append(f"{key}={updates[key]}\n")
            _atomic_owner_only_write(
                path,
                "".join(new_lines),
                parent_descriptor=parent_descriptor,
            )
        finally:
            os.close(parent_descriptor)


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
