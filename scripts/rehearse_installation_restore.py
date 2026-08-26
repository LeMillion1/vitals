#!/usr/bin/env python3
"""Rehearse one exact Vitals recovery bundle in an isolated Compose project.

The command never reads an installation environment, accepts no database URL or
Compose project from the operator, and never restores into an existing target.
Its stdout is one aggregate-only JSON record. A successful ordinary run cleans
itself; ``--serve`` retains a verified loopback-only app until ``destroy``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
from typing import Any, NoReturn
from urllib import error as urlerror
from urllib import request as urlrequest


FORMAT_VERSION = 1
OPERATION = "installation_restore_drill"
MANIFEST_RE = re.compile(r"vitals_bundle_(\d{8}T\d{6}Z)\.sha256")
RUN_ID_RE = re.compile(r"[0-9a-f]{12}")
PROJECT_RE = re.compile(r"vitals_drill_[0-9a-f]{12}")
RUN_DIR_RE = re.compile(r"run-[0-9a-f]{12}-[a-z0-9_-]+")
REVISION_RE = re.compile(r"[0-9a-f]{4,64}")
EXPECTED_PREFIXES = (
    "vitals",
    "garmin_session",
    "private_files",
    "legacy_uploads",
)
OPERATOR_ENV_KEYS = (
    "VITALS_APP_PORT",
    "VITALS_DATABASE_URL",
    "VITALS_DB_NAME",
    "VITALS_DB_PASSWORD",
    "VITALS_DB_USER",
    "VITALS_DRILL_GARMIN_DIR",
    "VITALS_DRILL_LEGACY_UPLOAD_DIR",
    "VITALS_DRILL_PRIVATE_FILES_DIR",
    "VITALS_DRILL_RUNTIME_ENV_FILE",
    "VITALS_MIGRATION_DATABASE_URL",
    "VITALS_RUNTIME_ENV_FILE",
)
MARKER_NAME = ".vitals-restore-drill"
STATE_NAME = "state.json"
SYNTHETIC_HASH = (
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
)


class DrillError(RuntimeError):
    """A bounded failure whose message is safe to include in JSON."""


class DrillCleanupError(DrillError):
    """Cleanup failed and a sensitive scratch run may still exist."""

    def __init__(self, context: Context) -> None:
        self.project = context.project
        self.run_dir = str(context.run_dir)
        self.run_id = context.run_id
        super().__init__("drill_cleanup_failed")


@dataclass(frozen=True, slots=True)
class Bundle:
    manifest: Path
    timestamp: str
    entries: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class Context:
    run_id: str
    project: str
    scratch_parent: Path
    run_dir: Path
    source_dir: Path
    bundle_dir: Path
    operator_env: Path
    runtime_env: Path
    state_file: Path
    marker_file: Path
    port: int
    source_revision: str
    bundle_timestamp: str
    manifest_sha256: str
    database: str
    owner_role: str
    runtime_role: str
    compose_env: dict[str, str]
    docker_mutated: bool = False


def _json_lines(*streams: bytes) -> list[dict[str, Any]]:
    """Return only standalone JSON objects; never surface surrounding output."""

    payloads: list[dict[str, Any]] = []
    ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    for stream in streams:
        try:
            lines = stream.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line in lines:
            candidate = ansi.sub("", line).strip()
            opening = candidate.find("{")
            if opening < 0:
                continue
            try:
                payload = json.loads(candidate[opening:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


def _fail(code: str) -> NoReturn:
    if re.fullmatch(r"[a-z0-9_]+", code) is None:
        code = "internal_error"
    raise DrillError(code)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _regular_file(path: Path, *, code: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError:
        _fail(code)
    if not stat.S_ISREG(value.st_mode) or path.is_symlink():
        _fail(code)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_manifest(path: Path) -> Bundle:
    if not path.is_absolute():
        _fail("manifest_path_not_absolute")
    _regular_file(path, code="manifest_not_regular")
    path = path.resolve(strict=True)
    _regular_file(path, code="manifest_not_regular")
    match = MANIFEST_RE.fullmatch(path.name)
    if match is None:
        _fail("manifest_name_invalid")
    timestamp = match.group(1)
    try:
        datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        _fail("manifest_timestamp_invalid")
    expected = tuple(
        f"{prefix}_{timestamp}.{'sql.gz' if prefix == 'vitals' else 'tar.gz'}"
        for prefix in EXPECTED_PREFIXES
    )
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        _fail("manifest_content_invalid")
    if len(lines) != 4:
        _fail("manifest_line_count_invalid")
    entries: list[tuple[str, str]] = []
    for line, expected_name in zip(lines, expected, strict=True):
        item = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if item is None or item.group(2) != expected_name:
            _fail("manifest_entry_invalid")
        artifact = path.parent / expected_name
        _regular_file(artifact, code="bundle_artifact_not_regular")
        if _sha256(artifact) != item.group(1):
            _fail("bundle_checksum_mismatch")
        entries.append((item.group(1), expected_name))
    return Bundle(path, timestamp, tuple(entries))


def _stable_copy(source: Path, destination: Path) -> None:
    before = _regular_file(source, code="bundle_source_changed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as src, destination.open("xb") as dst:
            opened = os.fstat(src.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                _fail("bundle_source_changed")
            shutil.copyfileobj(src, dst, 1024 * 1024)
    except FileExistsError:
        _fail("scratch_copy_exists")
    except OSError:
        _fail("bundle_copy_failed")
    os.chmod(destination, 0o600)
    after = _regular_file(source, code="bundle_source_changed")
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or destination.stat().st_size != before.st_size:
        _fail("bundle_source_changed")


def stage_bundle(bundle: Bundle, destination: Path) -> Bundle:
    destination.mkdir(mode=0o700)
    _stable_copy(bundle.manifest, destination / bundle.manifest.name)
    for digest, name in bundle.entries:
        target = destination / name
        _stable_copy(bundle.manifest.parent / name, target)
        if _sha256(target) != digest:
            _fail("scratch_bundle_checksum_mismatch")
    staged = validate_manifest(destination / bundle.manifest.name)
    return staged


def _archive_target(name: str) -> tuple[str, ...]:
    if "\\" in name or name.startswith("/"):
        _fail("archive_path_invalid")
    parts = tuple(part for part in PurePosixPath(name).parts if part not in ("", "."))
    if ".." in parts:
        _fail("archive_path_invalid")
    return parts


def inspect_tar(path: Path) -> tuple[list[tarfile.TarInfo], int]:
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
    except (OSError, tarfile.TarError):
        _fail("archive_invalid")
    seen: set[tuple[str, ...]] = set()
    total = 0
    for member in members:
        parts = _archive_target(member.name)
        if not parts:
            if not member.isdir():
                _fail("archive_root_entry_invalid")
            continue
        if parts in seen:
            _fail("archive_duplicate_path")
        seen.add(parts)
        if not member.isdir() and not member.isreg():
            _fail("archive_entry_type_invalid")
        if member.size < 0:
            _fail("archive_size_invalid")
        total += member.size
    return members, total


def extract_tar(path: Path, destination: Path) -> int:
    members, declared_size = inspect_tar(path)
    free = shutil.disk_usage(destination.parent).free
    if declared_size > free // 2:
        _fail("insufficient_scratch_space")
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in members:
                parts = _archive_target(member.name)
                if not parts:
                    continue
                target = destination.joinpath(*parts)
                if not _is_relative_to(target.resolve(strict=False), destination):
                    _fail("archive_path_invalid")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(target, 0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    _fail("archive_read_failed")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                os.chmod(target, 0o600)
    except DrillError:
        raise
    except (OSError, tarfile.TarError):
        _fail("archive_extract_failed")
    return declared_size


def verify_gzip(path: Path) -> int:
    total = 0
    try:
        with gzip.open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                total += len(chunk)
    except (OSError, EOFError, gzip.BadGzipFile):
        _fail("database_gzip_invalid")
    if total == 0:
        _fail("database_dump_empty")
    return total


def _safe_env() -> dict[str, str]:
    allowed = (
        "PATH",
        "HOME",
        "TMPDIR",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "XDG_RUNTIME_DIR",
        "LANG",
        "LC_ALL",
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    code: str = "command_failed",
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        _fail(code)
    if result.returncode != 0:
        detail = ""
        for payload in reversed(_json_lines(result.stdout, result.stderr)):
            candidate = payload.get("error_code", "")
            if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]+", candidate):
                detail = candidate
                break
        _fail(f"{code}_{detail}" if detail else code)
    return result


def _write_owner_only(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)


def _rewrite_owner_only(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _state_payload(context: Context, **updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "operation": OPERATION,
        "run_id": context.run_id,
        "project": context.project,
        "scratch_parent": str(context.scratch_parent),
        "run_dir": str(context.run_dir),
        "source_revision": context.source_revision,
        "bundle_timestamp": context.bundle_timestamp,
        "bundle_manifest_sha256": context.manifest_sha256,
        "port": context.port,
        "updated_at": _utc_now(),
    }
    payload.update(updates)
    return payload


def _write_state(context: Context, **updates: Any) -> dict[str, Any]:
    payload = _state_payload(context, **updates)
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    if context.state_file.exists():
        _rewrite_owner_only(context.state_file, serialized)
    else:
        _write_owner_only(context.state_file, serialized)
    return payload


def _validate_scratch_parent(parent: Path, *, repository: Path, bundle: Bundle) -> Path:
    if not parent.is_absolute():
        _fail("scratch_parent_not_absolute")
    existed = parent.exists()
    if parent.exists() and parent.is_symlink():
        _fail("scratch_parent_symlink")
    try:
        parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    except OSError:
        _fail("scratch_parent_unavailable")
    resolved = parent.resolve(strict=True)
    forbidden = {Path("/"), repository, repository.parent, bundle.manifest.parent}
    home = Path.home().resolve()
    forbidden.add(home)
    if resolved in forbidden:
        _fail("scratch_parent_forbidden")
    if _is_relative_to(resolved, repository) or _is_relative_to(
        resolved, bundle.manifest.parent
    ):
        _fail("scratch_parent_forbidden")
    if not stat.S_ISDIR(resolved.stat().st_mode):
        _fail("scratch_parent_unavailable")
    if not existed:
        os.chmod(resolved, 0o700)
    return resolved


def _port_available(port: int) -> bool:
    if port <= 1024 or port == 8000 or port > 65535:
        return False
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _stage_source(repository: Path, revision: str, destination: Path) -> None:
    archive_path = destination.parent / "source.tar"
    _run(
        ["git", "archive", "--format=tar", f"--output={archive_path}", revision],
        cwd=repository,
        env=_safe_env(),
        code="git_archive_failed",
    )
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(archive_path, "r:") as archive:
            members = archive.getmembers()
            for member in members:
                parts = _archive_target(member.name)
                if not parts:
                    continue
                if not member.isdir() and not member.isreg():
                    _fail("source_archive_entry_invalid")
                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    _fail("source_archive_read_failed")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(target, 0o700 if member.mode & 0o111 else 0o600)
    except DrillError:
        raise
    except (OSError, tarfile.TarError):
        _fail("source_archive_extract_failed")
    archive_path.unlink(missing_ok=True)


def _runtime_content(context: Context, *, password_hash: str = SYNTHETIC_HASH) -> str:
    runtime_url = (
        f"postgresql+asyncpg://{context.runtime_role}:"
        f"{context.compose_env['VITALS_DRILL_RUNTIME_PASSWORD']}@"
        f"vitals_db:5432/{context.database}"
    )
    values = {
        "VITALS_AUTH_PASSWORD_HASH": password_hash,
        "VITALS_AUTH_USERNAME": f"drill-{context.run_id}",
        "VITALS_COOKIE_SECURE": "false",
        "VITALS_CREDENTIAL_KEY": "",
        "VITALS_DATABASE_URL": runtime_url,
        "VITALS_EXTERNAL_API_TOKEN": "",
        "VITALS_GARMIN_EMAIL": "",
        "VITALS_GARMIN_PASSWORD": "",
        "VITALS_GARMIN_TOKEN_DIR": "/data/garmin_session",
        "VITALS_HEVY_API_KEY": "",
        "VITALS_MCP_CLIENT_ID": "vitals-restore-drill",
        "VITALS_MCP_CLIENT_SECRET": context.compose_env["VITALS_DRILL_MCP_SECRET"],
        "VITALS_OIDC_BOOTSTRAP_SUBJECT": "",
        "VITALS_OIDC_CLIENT_ID": "",
        "VITALS_OIDC_CLIENT_SECRET": "",
        "VITALS_OIDC_ISSUER": "",
        "VITALS_OIDC_REDIRECT_URL": "",
        "VITALS_OPENROUTER_API_KEY": "",
        "VITALS_REDIS_URL": "redis://vitals_redis:6379/0",
        "VITALS_SESSION_SECRET": context.compose_env["VITALS_DRILL_SESSION_SECRET"],
        "VITALS_TIMEZONE": "Asia/Almaty",
        "VITALS_WEB_PUSH_ENABLED": "false",
    }
    return "".join(f"{key}={value}\n" for key, value in sorted(values.items()))


def _operator_content(context: Context) -> str:
    return "".join(
        f"{key}={context.compose_env[key]}\n" for key in OPERATOR_ENV_KEYS
    )


def _compose(context: Context) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        context.project,
        "--env-file",
        str(context.operator_env),
        "-f",
        str(context.source_dir / "docker-compose.yml"),
        "-f",
        str(context.source_dir / "docker-compose.restore-drill.yml"),
    ]


def _render_and_assert(context: Context) -> dict[str, Any]:
    result = _run(
        _compose(context) + ["config", "--format", "json"],
        cwd=context.source_dir,
        env=_safe_env(),
        code="compose_render_failed",
    )
    try:
        rendered = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("compose_render_invalid")
    networks = rendered.get("networks", {})
    if not networks or any(
        item.get("internal") is not True or item.get("external") is True
        for item in networks.values()
    ):
        _fail("compose_network_not_internal")
    services = rendered.get("services", {})
    expected_services = {
        "vitals_app",
        "vitals_db",
        "vitals_db_roles",
        "vitals_migrate",
        "vitals_redis",
    }
    if set(services) != expected_services:
        _fail("compose_service_set_invalid")
    app = services.get("vitals_app", {})
    ports = app.get("ports") or []
    if len(ports) != 1:
        _fail("compose_port_count_invalid")
    port = ports[0]
    if (
        str(port.get("host_ip")) != "127.0.0.1"
        or int(port.get("target", 0)) != 8000
        or int(port.get("published", 0)) != context.port
    ):
        _fail("compose_port_mapping_invalid")
    expected_targets = {
        "/app/.env",
        "/data/garmin_session",
        "/app/web/static/uploads",
        "/data/private_files",
    }
    seen: set[str] = set()
    for mount in app.get("volumes") or []:
        target = mount.get("target")
        if target not in expected_targets:
            continue
        seen.add(target)
        if mount.get("type") != "bind" or mount.get("read_only") is not True:
            _fail("compose_sensitive_mount_not_read_only")
        source = Path(mount.get("source", "")).resolve(strict=True)
        if not _is_relative_to(source, context.run_dir):
            _fail("compose_bind_outside_scratch")
    if seen != expected_targets:
        _fail("compose_sensitive_mount_missing")
    if app.get("read_only") is not True:
        _fail("compose_app_root_not_read_only")
    app_networks = app.get("networks") or {}
    if not app_networks or any(name not in networks for name in app_networks):
        _fail("compose_app_network_invalid")
    for volume in (rendered.get("volumes") or {}).values():
        name = str(volume.get("name", ""))
        if not name.startswith(f"{context.project}_") or volume.get("external") is True:
            _fail("compose_volume_scope_invalid")
    profiled_result = _run(
        _compose(context)
        + ["--profile", "restore-drill-disabled", "config", "--format", "json"],
        cwd=context.source_dir,
        env=_safe_env(),
        code="compose_render_failed",
    )
    try:
        profiled = json.loads(profiled_result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("compose_render_invalid")
    backup = (profiled.get("services") or {}).get("vitals_backup", {})
    if backup.get("profiles") != ["restore-drill-disabled"]:
        _fail("compose_backup_not_disabled")
    return rendered


def _project_absent(project: str) -> None:
    result = _run(
        ["docker", "compose", "ls", "--all", "--format", "json"],
        env=_safe_env(),
        code="docker_compose_unavailable",
    )
    try:
        projects = json.loads(result.stdout or b"[]")
    except json.JSONDecodeError:
        _fail("docker_compose_list_invalid")
    if any(item.get("Name") == project for item in projects):
        _fail("drill_project_exists")
    if any(_resource_ids(project).values()):
        _fail("drill_project_exists")


def _json_from_output(result: subprocess.CompletedProcess[bytes], *, code: str) -> dict:
    payloads = _json_lines(result.stdout, result.stderr)
    if not payloads:
        _fail(code)
    payload = payloads[-1]
    if payload.get("result") not in ("ok", None) and payload.get("status") != "completed":
        _fail(code)
    return payload


def _psql(context: Context, sql: str, *, code: str) -> str:
    result = _run(
        _compose(context)
        + [
            "exec",
            "-T",
            "vitals_db",
            "psql",
            "-XAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            context.owner_role,
            "-d",
            context.database,
            "-c",
            sql,
        ],
        cwd=context.source_dir,
        env=_safe_env(),
        code=code,
    )
    try:
        return result.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        _fail(code)


def _restore_database(context: Context, dump: Path) -> None:
    command = _compose(context) + [
        "exec",
        "-T",
        "vitals_db",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "-U",
        context.owner_role,
        "-d",
        context.database,
    ]
    output = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            command,
            cwd=context.source_dir,
            env=_safe_env(),
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=output,
        )
        assert process.stdin is not None
        try:
            with gzip.open(dump, "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    process.stdin.write(chunk)
            process.stdin.close()
        except (BrokenPipeError, OSError, EOFError, gzip.BadGzipFile):
            process.kill()
            process.wait()
            _fail("database_restore_failed")
        if process.wait() != 0:
            _fail("database_restore_failed")
    except OSError:
        _fail("database_restore_failed")
    finally:
        output.close()


def _service_run(
    context: Context,
    service: str,
    command: list[str],
    *,
    code: str,
    extra_env_names: tuple[str, ...] = (),
    bind_files: tuple[tuple[Path, str], ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    options: list[str] = ["run", "--rm", "--no-deps"]
    for name in extra_env_names:
        options.extend(("-e", name))
    for source, target in bind_files:
        _regular_file(source, code=code)
        options.extend(("-v", f"{source}:{target}:ro"))
    return _run(
        _compose(context) + options + [service, *command],
        cwd=context.source_dir,
        env=context.compose_env | _safe_env(),
        code=code,
    )


def _wait_http(port: int, path: str, *, timeout: float = 60.0) -> int:
    deadline = time.monotonic() + timeout
    last_status = 0
    while time.monotonic() < deadline:
        try:
            with urlrequest.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=2
            ) as response:
                last_status = response.status
                if response.status == 200:
                    return 200
        except urlerror.HTTPError as exc:
            last_status = exc.code
        except (OSError, urlerror.URLError):
            pass
        time.sleep(1)
    return last_status


def _classify_app_failure(context: Context) -> str:
    """Map captured app diagnostics to a fixed code without returning log text."""

    logs = _run(
        _compose(context) + ["logs", "--no-color", "--tail", "200", "vitals_app"],
        cwd=context.source_dir,
        env=_safe_env(),
        code="drill_app_diagnostics_failed",
    )
    combined = (logs.stdout + b"\n" + logs.stderr).lower()
    patterns = (
        (b"read-only file system", "drill_app_read_only_failure"),
        (b"permission denied", "drill_app_permission_failure"),
        (b"password authentication failed", "drill_app_database_auth_failure"),
        (b"connection refused", "drill_app_database_connect_failure"),
        (b"could not translate host name", "drill_app_database_connect_failure"),
        (b"credential key", "drill_app_credential_config_failure"),
        (b"runtime environment", "drill_app_runtime_env_failure"),
        (b"application startup failed", "drill_app_startup_failure"),
    )
    for marker, code in patterns:
        if marker in combined:
            return code

    status = _run(
        _compose(context) + ["ps", "--all", "--format", "json", "vitals_app"],
        cwd=context.source_dir,
        env=_safe_env(),
        code="drill_app_diagnostics_failed",
    )
    payloads = _json_lines(status.stdout, status.stderr)
    if payloads:
        state = str(payloads[-1].get("State", "")).lower()
        exit_code = payloads[-1].get("ExitCode")
        if state == "exited" and isinstance(exit_code, int) and 0 <= exit_code <= 255:
            return f"drill_app_exited_{exit_code}"
        if state == "running":
            return "drill_app_endpoint_unavailable"
    return "drill_app_health_failed"


def _resource_ids(project: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for kind, command in (
        ("containers", ["docker", "ps", "-aq"]),
        ("networks", ["docker", "network", "ls", "-q"]),
        ("volumes", ["docker", "volume", "ls", "-q"]),
    ):
        output = _run(
            command + ["--filter", f"label=com.docker.compose.project={project}"],
            env=_safe_env(),
            code="docker_resource_inspection_failed",
        ).stdout.decode("ascii")
        result[kind] = [line for line in output.splitlines() if line]
    return result


def _cleanup(context: Context, *, remove_directory: bool = True) -> None:
    if context.docker_mutated:
        _run(
            _compose(context)
            + ["down", "--volumes", "--remove-orphans", "--timeout", "10"],
            cwd=context.source_dir,
            env=_safe_env(),
            code="drill_cleanup_failed",
        )
        if any(_resource_ids(context.project).values()):
            _fail("drill_cleanup_incomplete")
    if not remove_directory:
        return
    run_dir = context.run_dir
    if (
        run_dir.is_symlink()
        or not RUN_DIR_RE.fullmatch(run_dir.name)
        or run_dir.parent.resolve(strict=True) != context.scratch_parent
    ):
        _fail("scratch_cleanup_guard_failed")
    _regular_file(context.marker_file, code="scratch_cleanup_guard_failed")
    try:
        marker = json.loads(context.marker_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _fail("scratch_cleanup_guard_failed")
    if marker != {"project": context.project, "run_id": context.run_id}:
        _fail("scratch_cleanup_guard_failed")
    shutil.rmtree(run_dir)


def _context_from_state(run_dir: Path) -> tuple[Context, dict[str, Any]]:
    if not run_dir.is_absolute() or run_dir.is_symlink():
        _fail("run_dir_invalid")
    run_dir = run_dir.resolve(strict=True)
    if not RUN_DIR_RE.fullmatch(run_dir.name):
        _fail("run_dir_invalid")
    state_file = run_dir / STATE_NAME
    marker_file = run_dir / MARKER_NAME
    _regular_file(state_file, code="run_state_invalid")
    _regular_file(marker_file, code="run_marker_invalid")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _fail("run_state_invalid")
    run_id = state.get("run_id", "")
    project = state.get("project", "")
    port = state.get("port")
    source_revision = state.get("source_revision", "")
    bundle_timestamp = state.get("bundle_timestamp", "")
    manifest_sha256 = state.get("bundle_manifest_sha256", "")
    try:
        declared_run_dir = Path(state["run_dir"]).resolve(strict=True)
        scratch_parent = Path(state["scratch_parent"]).resolve(strict=True)
    except (KeyError, OSError, TypeError):
        _fail("run_state_invalid")
    if (
        state.get("format_version") != FORMAT_VERSION
        or state.get("operation") != OPERATION
        or RUN_ID_RE.fullmatch(run_id) is None
        or project != f"vitals_drill_{run_id}"
        or isinstance(port, bool)
        or not isinstance(port, int)
        or port <= 1024
        or port == 8000
        or port > 65535
        or re.fullmatch(r"[0-9a-f]{40,64}", source_revision) is None
        or re.fullmatch(r"\d{8}T\d{6}Z", bundle_timestamp) is None
        or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
        or marker != {"project": project, "run_id": run_id}
        or declared_run_dir != run_dir
        or scratch_parent != run_dir.parent
    ):
        _fail("run_state_invalid")
    operator_env = run_dir / "operator.env"
    _regular_file(operator_env, code="run_operator_env_invalid")
    values: dict[str, str] = {}
    for line in operator_env.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            _fail("run_operator_env_invalid")
        values[key] = value
    if set(values) != set(OPERATOR_ENV_KEYS):
        _fail("run_operator_env_invalid")
    database = values.get("VITALS_DB_NAME", "")
    owner = values.get("VITALS_DB_USER", "")
    runtime_url = values.get("VITALS_DATABASE_URL", "")
    migration_url = values.get("VITALS_MIGRATION_DATABASE_URL", "")
    runtime_match = re.fullmatch(
        r"postgresql\+asyncpg://([^:]+):[^@]+@vitals_db:5432/([^/?#]+)",
        runtime_url,
    )
    migration_match = re.fullmatch(
        r"postgresql\+asyncpg://([^:]+):[^@]+@vitals_db:5432/([^/?#]+)",
        migration_url,
    )
    if runtime_match is None or migration_match is None:
        _fail("run_operator_env_invalid")
    if (
        database != f"vitals_drill_{run_id}"
        or owner != f"vitals_drill_owner_{run_id}"
        or runtime_match.group(1) != f"vitals_drill_runtime_{run_id}"
        or runtime_match.group(2) != database
        or migration_match.groups() != (owner, database)
        or values.get("VITALS_APP_PORT") != str(port)
        or values.get("VITALS_DRILL_GARMIN_DIR") != str(run_dir / "garmin")
        or values.get("VITALS_DRILL_LEGACY_UPLOAD_DIR")
        != str(run_dir / "legacy_uploads")
        or values.get("VITALS_DRILL_PRIVATE_FILES_DIR")
        != str(run_dir / "private_files")
        or values.get("VITALS_DRILL_RUNTIME_ENV_FILE")
        != str(run_dir / "runtime.env")
        or values.get("VITALS_RUNTIME_ENV_FILE") != str(run_dir / "runtime.env")
    ):
        _fail("run_operator_env_invalid")
    context = Context(
        run_id=run_id,
        project=project,
        scratch_parent=scratch_parent,
        run_dir=run_dir,
        source_dir=run_dir / "source",
        bundle_dir=run_dir / "bundle",
        operator_env=operator_env,
        runtime_env=run_dir / "runtime.env",
        state_file=state_file,
        marker_file=marker_file,
        port=port,
        source_revision=source_revision,
        bundle_timestamp=bundle_timestamp,
        manifest_sha256=manifest_sha256,
        database=database,
        owner_role=owner,
        runtime_role=runtime_match.group(1),
        compose_env=values,
        docker_mutated=True,
    )
    return context, state


def _build_context(bundle: Bundle, scratch_parent: Path, port: int) -> Context:
    repository = Path(
        _run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).resolve().parent.parent,
            env=_safe_env(),
            code="repository_not_found",
        ).stdout.decode("utf-8").strip()
    ).resolve(strict=True)
    revision = _run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        env=_safe_env(),
        code="source_revision_invalid",
    ).stdout.decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40,64}", revision) is None:
        _fail("source_revision_invalid")
    parent = _validate_scratch_parent(
        scratch_parent, repository=repository, bundle=bundle
    )
    if not _port_available(port):
        _fail("drill_port_unavailable")
    run_id = secrets.token_hex(6)
    run_dir = Path(tempfile.mkdtemp(prefix=f"run-{run_id}-", dir=parent))
    try:
        os.chmod(run_dir, 0o700)
        project = f"vitals_drill_{run_id}"
        database = f"vitals_drill_{run_id}"
        owner = f"vitals_drill_owner_{run_id}"
        runtime = f"vitals_drill_runtime_{run_id}"
        owner_password = secrets.token_urlsafe(24)
        runtime_password = secrets.token_urlsafe(24)
        compose_env = {
            "VITALS_APP_PORT": str(port),
            "VITALS_DATABASE_URL": (
                f"postgresql+asyncpg://{runtime}:{runtime_password}@"
                f"vitals_db:5432/{database}"
            ),
            "VITALS_DB_NAME": database,
            "VITALS_DB_PASSWORD": owner_password,
            "VITALS_DB_USER": owner,
            "VITALS_DRILL_GARMIN_DIR": str(run_dir / "garmin"),
            "VITALS_DRILL_LEGACY_UPLOAD_DIR": str(run_dir / "legacy_uploads"),
            "VITALS_DRILL_MCP_SECRET": secrets.token_urlsafe(32),
            "VITALS_DRILL_PRIVATE_FILES_DIR": str(run_dir / "private_files"),
            "VITALS_DRILL_RUNTIME_ENV_FILE": str(run_dir / "runtime.env"),
            "VITALS_DRILL_RUNTIME_PASSWORD": runtime_password,
            "VITALS_DRILL_SESSION_SECRET": secrets.token_urlsafe(48),
            "VITALS_MIGRATION_DATABASE_URL": (
                f"postgresql+asyncpg://{owner}:{owner_password}@"
                f"vitals_db:5432/{database}"
            ),
            "VITALS_RUNTIME_ENV_FILE": str(run_dir / "runtime.env"),
        }
        context = Context(
            run_id=run_id,
            project=project,
            scratch_parent=parent,
            run_dir=run_dir,
            source_dir=run_dir / "source",
            bundle_dir=run_dir / "bundle",
            operator_env=run_dir / "operator.env",
            runtime_env=run_dir / "runtime.env",
            state_file=run_dir / STATE_NAME,
            marker_file=run_dir / MARKER_NAME,
            port=port,
            source_revision=revision,
            bundle_timestamp=bundle.timestamp,
            manifest_sha256=_sha256(bundle.manifest),
            database=database,
            owner_role=owner,
            runtime_role=runtime,
            compose_env=compose_env,
        )
        _write_owner_only(
            context.marker_file,
            json.dumps({"project": project, "run_id": run_id}, sort_keys=True),
        )
        _stage_source(repository, revision, context.source_dir)
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return context


def run_drill(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    source_bundle = validate_manifest(args.manifest)
    context = _build_context(source_bundle, args.scratch_parent, args.port)
    try:
        _write_state(context, phase="staging")
        staged = stage_bundle(source_bundle, context.bundle_dir)
        dump = context.bundle_dir / f"vitals_{staged.timestamp}.sql.gz"
        verify_gzip(dump)
        extracted_size = 0
        for prefix, destination in (
            ("garmin_session", context.run_dir / "garmin"),
            ("private_files", context.run_dir / "private_files"),
            ("legacy_uploads", context.run_dir / "legacy_uploads"),
        ):
            extracted_size += extract_tar(
                context.bundle_dir / f"{prefix}_{staged.timestamp}.tar.gz",
                destination,
            )
        _write_owner_only(context.runtime_env, _runtime_content(context))
        _write_owner_only(context.operator_env, _operator_content(context))
        _project_absent(context.project)
        _render_and_assert(context)
        _write_state(context, phase="building", extracted_bytes=extracted_size)
        _run(
            _compose(context)
            + ["build", "vitals_migrate", "vitals_db_roles", "vitals_app"],
            cwd=context.source_dir,
            env=_safe_env(),
            code="compose_build_failed",
        )
        context.docker_mutated = True
        _write_state(context, phase="starting_database")
        _run(
            _compose(context)
            + [
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "60",
                "--no-build",
                "vitals_db",
                "vitals_redis",
            ],
            cwd=context.source_dir,
            env=_safe_env(),
            code="drill_database_start_failed",
        )
        if _psql(
            context,
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='public' "
            "AND c.relkind IN ('r','p');",
            code="empty_database_check_failed",
        ) != "0":
            _fail("restore_target_not_empty")
        _write_state(context, phase="restoring")
        _restore_database(context, dump)
        restored_revision = _psql(
            context,
            "SELECT version_num FROM alembic_version;",
            code="restored_revision_invalid",
        )
        if REVISION_RE.fullmatch(restored_revision) is None:
            _fail("restored_revision_invalid")
        _write_state(context, phase="migrating", restored_revision=restored_revision)
        _service_run(
            context,
            "vitals_migrate",
            ["alembic", "upgrade", "head"],
            code="restore_migration_failed",
        )
        head_output = _service_run(
            context,
            "vitals_migrate",
            ["alembic", "heads"],
            code="head_revision_invalid",
        ).stdout.decode("utf-8")
        heads = re.findall(r"(?m)^([0-9a-f]+) \(head\)$", head_output)
        if len(heads) != 1:
            _fail("head_revision_invalid")
        head_revision = heads[0]
        if _psql(
            context,
            "SELECT version_num FROM alembic_version;",
            code="head_revision_invalid",
        ) != head_revision:
            _fail("head_revision_invalid")
        _write_state(context, phase="validating", head_revision=head_revision)
        validations: dict[str, str] = {}
        for script, key in (
            ("scripts/validate_subject_ownership.py", "ownership"),
            ("scripts/audit_scoped_keys.py", "scoped_keys"),
        ):
            for apply in (False, True):
                command = ["python", script] + (["--apply"] if apply else [])
                payload = _json_from_output(
                    _service_run(
                        context,
                        "vitals_migrate",
                        command,
                        code=f"{key}_validation_failed",
                    ),
                    code=f"{key}_validation_failed",
                )
                if payload.get("result") != "ok":
                    _fail(f"{key}_validation_failed")
                if apply and payload.get("completed") is not True:
                    _fail(f"{key}_validation_failed")
                validations[key] = str(payload.get("status"))
        role_payload = _json_from_output(
            _service_run(
                context,
                "vitals_db_roles",
                ["python", "scripts/provision_runtime_db_role.py"],
                code="runtime_role_provision_failed",
            ),
            code="runtime_role_provision_failed",
        )
        required_zero = (
            "owned_objects",
            "role_memberships",
            "role_settings",
            "extra_privileges",
        )
        if role_payload.get("status") != "completed" or any(
            role_payload.get(key) != 0 for key in required_zero
        ):
            _fail("runtime_role_provision_failed")
        rls_payload = _json_from_output(
            _service_run(
                context,
                "vitals_db_roles",
                ["python", "scripts/run_restore_validator.py", "runtime-rls"],
                code="runtime_rls_validation_failed",
            ),
            code="runtime_rls_validation_failed",
        )
        if rls_payload.get("result") != "ok":
            _fail("runtime_rls_validation_failed")
        _write_state(context, phase="restarting_database")
        _run(
            _compose(context) + ["restart", "vitals_db"],
            cwd=context.source_dir,
            env=_safe_env(),
            code="database_restart_failed",
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if _psql(context, "SELECT 1;", code="database_not_ready") == "1":
                    break
            except DrillError:
                time.sleep(1)
        else:
            _fail("database_restart_failed")
        if _psql(
            context,
            "SELECT version_num FROM alembic_version;",
            code="restart_revision_invalid",
        ) != head_revision:
            _fail("restart_revision_invalid")
        _json_from_output(
            _service_run(
                context,
                "vitals_db_roles",
                ["python", "scripts/run_restore_validator.py", "runtime-rls"],
                code="restart_rls_validation_failed",
            ),
            code="restart_rls_validation_failed",
        )
        duration = round(time.monotonic() - started, 3)
        base = {
            "result": "ok",
            "restored_revision": restored_revision,
            "head_revision": head_revision,
            "ownership_validation": validations["ownership"],
            "scoped_key_audit": validations["scoped_keys"],
            "runtime_role_audit": "completed",
            "runtime_rls_audit": "completed",
            "rto_seconds": duration,
        }
        if not args.serve:
            payload = _state_payload(context, phase="completed", **base)
            _cleanup(context)
            payload.pop("run_dir", None)
            payload.pop("project", None)
            payload.pop("port", None)
            return payload

        password = secrets.token_urlsafe(18)
        context.compose_env["VITALS_DRILL_PLAINTEXT_PASSWORD"] = password
        hash_result = _service_run(
            context,
            "vitals_migrate",
            [
                "python",
                "-c",
                "import os; from vitals.utils.passwords import hash_password; "
                "print(hash_password(os.environ['VITALS_DRILL_PLAINTEXT_PASSWORD']))",
            ],
            code="drill_password_hash_failed",
            extra_env_names=("VITALS_DRILL_PLAINTEXT_PASSWORD",),
        )
        context.compose_env.pop("VITALS_DRILL_PLAINTEXT_PASSWORD", None)
        try:
            password_hash = hash_result.stdout.decode("ascii").splitlines()[-1].strip()
        except (UnicodeDecodeError, IndexError):
            _fail("drill_password_hash_failed")
        if re.fullmatch(r"\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}", password_hash) is None:
            _fail("drill_password_hash_failed")
        context.compose_env.update(
            {
                "VITALS_DRILL_PASSWORD_HASH": password_hash,
                "VITALS_DRILL_USERNAME": f"drill-{context.run_id}",
                "VITALS_RESTORE_DRILL": "true",
                "VITALS_RESTORE_DRILL_MARKER_FILE": (
                    "/run/vitals-restore-drill/marker.json"
                ),
            }
        )
        _json_from_output(
            _service_run(
                context,
                "vitals_migrate",
                ["python", "scripts/prepare_restore_drill_login.py"],
                code="drill_login_prepare_failed",
                extra_env_names=(
                    "VITALS_DRILL_PASSWORD_HASH",
                    "VITALS_DRILL_USERNAME",
                    "VITALS_RESTORE_DRILL",
                    "VITALS_RESTORE_DRILL_MARKER_FILE",
                ),
                bind_files=(
                    (
                        context.marker_file,
                        "/run/vitals-restore-drill/marker.json",
                    ),
                ),
            ),
            code="drill_login_prepare_failed",
        )
        _rewrite_owner_only(
            context.runtime_env,
            _runtime_content(context, password_hash=password_hash),
        )
        credentials = context.run_dir / "browser-credentials.txt"
        _write_owner_only(
            credentials,
            f"username=drill-{context.run_id}\npassword={password}\n",
        )
        _run(
            _compose(context) + ["up", "-d", "--no-deps", "--no-build", "vitals_app"],
            cwd=context.source_dir,
            env=_safe_env(),
            code="drill_app_start_failed",
        )
        health_status = _wait_http(context.port, "/health")
        if health_status != 200:
            if 100 <= health_status <= 599:
                _fail(f"drill_app_health_status_{health_status}")
            _fail(_classify_app_failure(context))
        if _wait_http(context.port, "/login") != 200:
            _fail("drill_login_page_failed")
        return _write_state(
            context,
            phase="served",
            credentials_file=str(credentials),
            **base,
        )
    except BaseException:
        try:
            _cleanup(context)
        except Exception as cleanup_error:
            try:
                _write_state(context, phase="cleanup_failed")
            except Exception:
                pass
            raise DrillCleanupError(context) from cleanup_error
        raise


def status_run(run_dir: Path) -> dict[str, Any]:
    context, state = _context_from_state(run_dir)
    resources = _resource_ids(context.project)
    status = _wait_http(context.port, "/health", timeout=2)
    return {
        "format_version": FORMAT_VERSION,
        "operation": OPERATION,
        "result": "ok" if status == 200 else "error",
        "phase": state.get("phase"),
        "run_id": context.run_id,
        "port": context.port,
        "health_status": status,
        "containers": len(resources["containers"]),
        "networks": len(resources["networks"]),
        "volumes": len(resources["volumes"]),
    }


def restart_run(run_dir: Path) -> dict[str, Any]:
    context, state = _context_from_state(run_dir)
    if state.get("phase") != "served":
        _fail("run_not_served")
    _render_and_assert(context)
    _run(
        _compose(context) + ["restart", "vitals_app"],
        cwd=context.source_dir,
        env=_safe_env(),
        code="drill_app_restart_failed",
    )
    if _wait_http(context.port, "/health") != 200:
        _fail("drill_app_restart_failed")
    rls_payload = _json_from_output(
        _service_run(
            context,
            "vitals_db_roles",
            ["python", "scripts/run_restore_validator.py", "runtime-rls"],
            code="restart_rls_validation_failed",
        ),
        code="restart_rls_validation_failed",
    )
    if rls_payload.get("result") != "ok":
        _fail("restart_rls_validation_failed")
    preserved = {
        key: state[key]
        for key in (
            "credentials_file",
            "head_revision",
            "ownership_validation",
            "restored_revision",
            "rto_seconds",
            "runtime_role_audit",
            "runtime_rls_audit",
            "scoped_key_audit",
        )
        if key in state
    }
    payload = _write_state(context, phase="served", **preserved)
    payload.update({"result": "ok", "restart": "completed"})
    return payload


def destroy_run(run_dir: Path) -> dict[str, Any]:
    context, _state = _context_from_state(run_dir)
    run_id = context.run_id
    _cleanup(context)
    return {
        "format_version": FORMAT_VERSION,
        "operation": OPERATION,
        "result": "ok",
        "phase": "destroyed",
        "run_id": run_id,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", allow_abbrev=False)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--scratch-parent", type=Path, required=True)
    run.add_argument("--serve", action="store_true")
    run.add_argument("--port", type=int, default=18080)
    for name in ("status", "restart", "destroy"):
        command = subparsers.add_parser(name, allow_abbrev=False)
        command.add_argument("--run-dir", type=Path, required=True)
    return parser


def main() -> int:
    def terminate(_signum: int, _frame: Any) -> NoReturn:
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, terminate)
    try:
        args = build_parser().parse_args()
        if args.command == "run":
            payload = run_drill(args)
        elif args.command == "status":
            payload = status_run(args.run_dir)
        elif args.command == "restart":
            payload = restart_run(args.run_dir)
        else:
            payload = destroy_run(args.run_dir)
        exit_code = 0 if payload.get("result") == "ok" else 1
    except KeyboardInterrupt:
        payload = {
            "format_version": FORMAT_VERSION,
            "operation": OPERATION,
            "result": "error",
            "error_code": "cancelled",
        }
        exit_code = 130
    except DrillCleanupError as exc:
        payload = {
            "format_version": FORMAT_VERSION,
            "operation": OPERATION,
            "result": "error",
            "error_code": str(exc),
            "project": exc.project,
            "run_dir": exc.run_dir,
            "run_id": exc.run_id,
        }
        exit_code = 1
    except DrillError as exc:
        payload = {
            "format_version": FORMAT_VERSION,
            "operation": OPERATION,
            "result": "error",
            "error_code": str(exc),
        }
        exit_code = 1
    except Exception:
        payload = {
            "format_version": FORMAT_VERSION,
            "operation": OPERATION,
            "result": "error",
            "error_code": "internal_error",
        }
        exit_code = 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
