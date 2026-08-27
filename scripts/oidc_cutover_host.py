#!/usr/bin/env python3
"""Coordinate a crash-recoverable production OIDC cutover from the host.

The in-container ``oidc_cutover.py`` helper owns runtime-file and database
validation.  This command owns the part that helper deliberately cannot prove:
the exact Compose project, the existing application container and image, the
stop/recreate boundary, and the HTTP behavior actually served afterwards.

No credential value is accepted on the command line.  The selected OIDC client
secret and legacy-password proof are copied into a disposable owner-only
directory, and only that directory is mounted read-only into the immutable
helper image.  The phase journal contains only operational identifiers and is
safe to inspect.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vitals.runtime_env import RUNTIME_ENV_KEYS, read_env_key  # noqa: E402


APP_SERVICE = "vitals_app"
APP_PORT = "8000"
RUNTIME_TARGET = "/run/vitals-runtime"
RUNTIME_FILE = f"{RUNTIME_TARGET}/vitals.env"
SECRET_TARGET = "/run/vitals-oidc-secret"
HELPER = "scripts/oidc_cutover.py"
STATE_FORMAT = 1
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
OIDC_RUNTIME_KEYS = (
    "VITALS_OIDC_ISSUER",
    "VITALS_OIDC_CLIENT_ID",
    "VITALS_OIDC_CLIENT_SECRET",
    "VITALS_OIDC_REDIRECT_URL",
)
OIDC_BOOTSTRAP_KEY = "VITALS_OIDC_BOOTSTRAP_SUBJECT"
SESSION_SECRET_KEY = "VITALS_SESSION_SECRET"
PROCESS_ONLY_RUNTIME_KEYS = frozenset({"VITALS_PROCESS_MODE"})
SHADOWING_RUNTIME_KEYS = RUNTIME_ENV_KEYS - PROCESS_ONLY_RUNTIME_KEYS
REQUIRED_WEB_ENV = {
    "VITALS_ENV_FILE": RUNTIME_FILE,
    "VITALS_PROCESS_MODE": "web",
    "VITALS_RUNTIME_ENV_ISOLATION_REQUIRED": "true",
}

CUTOVER_CONFIRMATION = "CUT OVER TO OIDC; AUTOMATIC ROLLBACK ON FAILED POSTFLIGHT"
FINALIZE_CONFIRMATION = "OWNER OIDC LOGIN VERIFIED; FINALIZE CUTOVER"
ROLLBACK_CONFIRMATION = "ROLL BACK TO PASSWORD MODE AND ROTATE SESSIONS"
RETIRE_CONFIRMATION = "IDP RESTORE VERIFIED; RETIRE LEGACY PASSWORD"

HELPER_ENABLE_CONFIRMATION = "WEB STOPPED; ENABLE OIDC AND ROTATE SESSIONS"
HELPER_FINALIZE_CONFIRMATION = "OWNER OIDC BINDING VERIFIED; REMOVE BOOTSTRAP"
HELPER_ROLLBACK_CONFIRMATION = "WEB STOPPED; RESTORE PASSWORD MODE AND ROTATE SESSIONS"
HELPER_RETIRE_CONFIRMATION = "OIDC RECOVERY VERIFIED; RETIRE PASSWORD BRIDGE AND ROTATE SESSIONS"
MUTATING_HELPER_OPERATIONS = frozenset({"enable", "finalize", "rollback", "retire-legacy"})


class CoordinatorError(RuntimeError):
    """A fail-closed orchestration or attestation refusal."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class ProbeResponse:
    status: int
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ProviderArguments:
    issuer: str
    client_id: str
    client_secret_file: Path
    legacy_password_file: Path
    redirect_url: str
    bootstrap_subject: str


@dataclass(frozen=True, slots=True)
class Attestation:
    container_id: str
    image: str
    image_id: str
    config_id: str
    network: str
    network_id: str
    running: bool


Runner = Callable[[Sequence[str]], ProcessResult]
HttpProbe = Callable[[str], ProbeResponse]

INCOMPLETE_PHASES = frozenset(
    {
        "cutover_web_stopped",
        "cutover_config_written",
        "cutover_postflight_failed",
        "cutover_compensation_written",
        "finalize_web_stopped",
        "finalize_config_written",
        "rollback_web_stopped",
        "rollback_config_written",
        "retire_web_stopped",
        "retire_config_written",
        "recovery_required",
    }
)
STABLE_PHASES = frozenset(
    {
        "preflight_passed",
        "password_ready",
        "rolled_back",
        "awaiting_owner_binding",
        "oidc_bound",
        "legacy_retired",
    }
)


def _default_runner(command: Sequence[str]) -> ProcessResult:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return ProcessResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _default_http_probe(url: str) -> ProbeResponse:
    opener = build_opener(_NoRedirect)
    request = Request(
        url,
        method="GET",
        headers={
            "Accept": "text/html",
            "User-Agent": "vitals-oidc-cutover/1",
        },
    )
    try:
        with opener.open(request, timeout=10) as response:
            return ProbeResponse(response.status, dict(response.headers.items()))
    except HTTPError as exc:
        return ProbeResponse(exc.code, dict(exc.headers.items()))
    except URLError as exc:
        raise CoordinatorError("application HTTP probe could not connect") from exc


def _json_object(raw: str, *, boundary: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CoordinatorError(f"{boundary} returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise CoordinatorError(f"{boundary} returned a non-object JSON value")
    return payload


def _one_json_record(raw: str, *, boundary: str) -> dict[str, object]:
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        raise CoordinatorError(f"{boundary} returned an ambiguous result")
    return _json_object(lines[0], boundary=boundary)


def _container_env(raw: object, *, boundary: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise CoordinatorError(f"{boundary} has malformed environment metadata")
    values: dict[str, str] = {}
    for item in raw:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise CoordinatorError(f"{boundary} has malformed environment metadata")
        if key in values:
            raise CoordinatorError(f"{boundary} has duplicate environment keys")
        values[key] = value
    return values


def _normalized_origin(value: str, *, field: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(value)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise CoordinatorError(f"{field} is not a valid URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or not 1 <= port <= 65535
    ):
        raise CoordinatorError(f"{field} is not a safe HTTP origin")
    return parsed.scheme, parsed.hostname.casefold(), port


class OidcCutoverHost:
    """Host-side coordinator with injectable process and HTTP boundaries."""

    def __init__(
        self,
        *,
        project: str,
        compose_files: Sequence[Path],
        env_files: Sequence[Path],
        runtime_env: Path,
        state_file: Path,
        runner: Runner = _default_runner,
        http_probe: HttpProbe = _default_http_probe,
    ) -> None:
        if PROJECT_PATTERN.fullmatch(project) is None:
            raise CoordinatorError("Compose project name has an invalid shape")
        if not compose_files:
            raise CoordinatorError("at least one explicit Compose file is required")
        self.project = project
        self.compose_files = tuple(self._validate_config_file(path) for path in compose_files)
        self.env_files = tuple(self._validate_config_file(path, private=True) for path in env_files)
        self.runtime_env = self._validate_runtime_env(runtime_env)
        # `resolve()` would erase the fact that the operator supplied a symlink
        # before the later lstat safety check had a chance to reject it.
        self.state_file = Path(os.path.abspath(state_file.expanduser()))
        self.runner = runner
        self.http_probe = http_probe

    @staticmethod
    def _validate_config_file(path: Path, *, private: bool = False) -> Path:
        candidate = path.expanduser()
        try:
            metadata = candidate.lstat()
        except FileNotFoundError as exc:
            raise CoordinatorError("an explicit Compose input file is missing") from exc
        if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise CoordinatorError("Compose input files must be regular non-symlinks")
        if private and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise CoordinatorError("Compose env files must be owner-only")
        return candidate.resolve(strict=True)

    @staticmethod
    def _validate_runtime_env(path: Path) -> Path:
        candidate = path.expanduser()
        parent = candidate.parent
        try:
            parent_stat = parent.lstat()
            file_stat = candidate.lstat()
        except FileNotFoundError as exc:
            raise CoordinatorError("application runtime environment is missing") from exc
        if (
            parent.is_symlink()
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            raise CoordinatorError("runtime environment directory must be owner-only mode 0700")
        if (
            candidate.is_symlink()
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise CoordinatorError("runtime environment file must be owner-only mode 0600")
        return candidate.resolve(strict=True)

    @property
    def _compose(self) -> list[str]:
        command = ["docker", "compose", "--project-name", self.project]
        for path in self.compose_files:
            command.extend(("--file", str(path)))
        for path in self.env_files:
            command.extend(("--env-file", str(path)))
        return command

    def _execute(
        self,
        command: Sequence[str],
        *,
        boundary: str,
    ) -> ProcessResult:
        try:
            result = self.runner(tuple(command))
        except (OSError, subprocess.SubprocessError) as exc:
            raise CoordinatorError(f"{boundary} could not be executed") from exc
        if result.returncode != 0:
            # Command output may contain a DSN or a provider error.  The operator
            # gets the phase and boundary, never raw subprocess output.
            raise CoordinatorError(f"{boundary} failed")
        return result

    def _rendered_service(self) -> dict[str, object]:
        result = self._execute(
            [*self._compose, "config", "--format", "json"],
            boundary="Compose config rendering",
        )
        payload = _json_object(result.stdout, boundary="Compose config")
        services = payload.get("services")
        if not isinstance(services, dict):
            raise CoordinatorError("Compose config has no services object")
        service = services.get(APP_SERVICE)
        if not isinstance(service, dict):
            raise CoordinatorError("Compose config has no vitals_app service")
        return service

    @staticmethod
    def _service_config_id(service: Mapping[str, object]) -> str:
        canonical = json.dumps(
            service,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def attest(self, *, require_running: bool | None = None) -> Attestation:
        service = self._rendered_service()
        config_id = self._service_config_id(service)
        expected_image = service.get("image")
        environment = service.get("environment") or {}
        service_env_files = service.get("env_file") or []
        volumes = service.get("volumes") or []
        if not isinstance(expected_image, str) or not expected_image.strip():
            raise CoordinatorError("vitals_app has no explicit rendered image")
        if not isinstance(environment, dict) or any(
            environment.get(key) != value for key, value in REQUIRED_WEB_ENV.items()
        ):
            raise CoordinatorError("vitals_app uses unexpected runtime control values")
        if service_env_files:
            raise CoordinatorError("vitals_app must not use Compose env_file injection")
        if SHADOWING_RUNTIME_KEYS.intersection(environment):
            raise CoordinatorError("vitals_app directly injects runtime-file authority")
        runtime_mounts = [
            item
            for item in volumes
            if isinstance(item, dict) and item.get("target") == RUNTIME_TARGET
        ]
        if len(runtime_mounts) != 1:
            raise CoordinatorError("vitals_app must have exactly one runtime mount")
        rendered_mount = runtime_mounts[0]
        expected_source = str(self.runtime_env.parent)
        if (
            rendered_mount.get("type") != "bind"
            or bool(rendered_mount.get("read_only", False))
            or str(Path(str(rendered_mount.get("source", ""))).resolve(strict=False))
            != expected_source
        ):
            raise CoordinatorError("rendered runtime mount does not match the host path")

        listed = self._execute(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--filter",
                f"label=com.docker.compose.service={APP_SERVICE}",
                "--format",
                "{{.ID}}",
            ],
            boundary="existing container lookup",
        )
        container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if len(container_ids) != 1:
            raise CoordinatorError("expected exactly one existing vitals_app container")
        container_id = container_ids[0]
        inspected = self._execute(
            ["docker", "inspect", container_id],
            boundary="existing container inspection",
        )
        try:
            records = json.loads(inspected.stdout)
        except json.JSONDecodeError as exc:
            raise CoordinatorError("container inspection returned malformed JSON") from exc
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise CoordinatorError("container inspection returned an ambiguous result")
        record = records[0]
        labels = (record.get("Config") or {}).get("Labels") or {}
        configured_image = (record.get("Config") or {}).get("Image")
        container_env = _container_env(
            (record.get("Config") or {}).get("Env"),
            boundary="vitals_app container",
        )
        running = bool((record.get("State") or {}).get("Running"))
        if (
            not isinstance(labels, dict)
            or labels.get("com.docker.compose.project") != self.project
            or labels.get("com.docker.compose.service") != APP_SERVICE
        ):
            raise CoordinatorError("container labels do not match the requested project")
        if configured_image != expected_image:
            raise CoordinatorError("container image name differs from rendered Compose")
        if SHADOWING_RUNTIME_KEYS.intersection(container_env):
            raise CoordinatorError("vitals_app container shadows runtime-file authority")
        if any(container_env.get(key) != value for key, value in REQUIRED_WEB_ENV.items()):
            raise CoordinatorError("vitals_app container has unexpected runtime control values")

        actual_mounts = [
            mount
            for mount in (record.get("Mounts") or [])
            if isinstance(mount, dict) and mount.get("Destination") == RUNTIME_TARGET
        ]
        if len(actual_mounts) != 1:
            raise CoordinatorError("container has an ambiguous runtime mount")
        actual_mount = actual_mounts[0]
        if (
            actual_mount.get("Type") != "bind"
            or not bool(actual_mount.get("RW"))
            or str(Path(str(actual_mount.get("Source", ""))).resolve(strict=False))
            != expected_source
        ):
            raise CoordinatorError("container runtime mount differs from rendered Compose")

        image_inspection = self._execute(
            ["docker", "image", "inspect", expected_image],
            boundary="expected image inspection",
        )
        try:
            image_records = json.loads(image_inspection.stdout)
        except json.JSONDecodeError as exc:
            raise CoordinatorError("image inspection returned malformed JSON") from exc
        if (
            not isinstance(image_records, list)
            or len(image_records) != 1
            or not isinstance(image_records[0], dict)
            or not isinstance(image_records[0].get("Id"), str)
        ):
            raise CoordinatorError("image inspection returned an ambiguous result")
        expected_image_id = image_records[0]["Id"]
        image_env = _container_env(
            (image_records[0].get("Config") or {}).get("Env"),
            boundary="vitals_app image",
        )
        if SHADOWING_RUNTIME_KEYS.intersection(image_env):
            raise CoordinatorError("vitals_app image shadows runtime-file authority")
        if set(REQUIRED_WEB_ENV).intersection(image_env):
            raise CoordinatorError("vitals_app image bakes runtime control values")
        if record.get("Image") != expected_image_id:
            raise CoordinatorError("container does not run the rendered image ID")

        network_attachments = (record.get("NetworkSettings") or {}).get("Networks") or {}
        if not isinstance(network_attachments, dict) or len(network_attachments) != 1:
            raise CoordinatorError("vitals_app must use exactly one Compose network")
        network, attachment = next(iter(network_attachments.items()))
        if not isinstance(network, str) or not isinstance(attachment, dict):
            raise CoordinatorError("container network attachment is malformed")
        attached_network_id = attachment.get("NetworkID")
        if not isinstance(attached_network_id, str) or not attached_network_id:
            raise CoordinatorError("container network has no immutable ID")
        network_inspection = self._execute(
            ["docker", "network", "inspect", network],
            boundary="Compose network inspection",
        )
        try:
            network_records = json.loads(network_inspection.stdout)
        except json.JSONDecodeError as exc:
            raise CoordinatorError("network inspection returned malformed JSON") from exc
        if (
            not isinstance(network_records, list)
            or len(network_records) != 1
            or not isinstance(network_records[0], dict)
        ):
            raise CoordinatorError("network inspection returned an ambiguous result")
        network_record = network_records[0]
        network_labels = network_record.get("Labels") or {}
        network_id = network_record.get("Id")
        if (
            not isinstance(network_labels, dict)
            or network_labels.get("com.docker.compose.project") != self.project
            or not network_labels.get("com.docker.compose.network")
            or network_id != attached_network_id
        ):
            raise CoordinatorError("container network is not the exact Compose network")
        if require_running is not None and running is not require_running:
            expected = "running" if require_running else "stopped"
            raise CoordinatorError(f"vitals_app is not {expected}")
        return Attestation(
            container_id=container_id,
            image=expected_image,
            image_id=expected_image_id,
            config_id=config_id,
            network=network,
            network_id=network_id,
            running=running,
        )

    def _validate_secret_file(self, path: Path) -> Path:
        candidate = path.expanduser()
        parent = candidate.parent
        try:
            parent_stat = parent.lstat()
            file_stat = candidate.lstat()
        except FileNotFoundError as exc:
            raise CoordinatorError("operator proof file is missing") from exc
        if (
            parent.is_symlink()
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            raise CoordinatorError("operator proof parent must be owner-only mode 0700")
        if (
            candidate.is_symlink()
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or not 0 < file_stat.st_size <= 8192
        ):
            raise CoordinatorError("operator proof file must be owner-only mode 0600")
        return candidate.resolve(strict=True)

    @contextmanager
    def _staged_secret_files(self, selected: Mapping[str, Path]):
        """Copy only selected proof files into one disposable private directory."""

        if not selected:
            yield {}
            return
        if set(selected) - {"client-secret", "legacy-password"}:
            raise CoordinatorError("unsupported operator proof selection")
        sources = {name: self._validate_secret_file(path) for name, path in selected.items()}
        if len(set(sources.values())) != len(sources):
            raise CoordinatorError("operator proof files must be distinct")
        stage_parent = self.state_file.parent
        staged_directory = Path(tempfile.mkdtemp(prefix=".vitals-oidc-secret.", dir=stage_parent))
        staged_directory.chmod(0o700)
        staged: dict[str, Path] = {}
        try:
            for name, source in sources.items():
                staged_file = staged_directory / name
                source_fd = -1
                destination_fd = -1
                try:
                    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    source_flags |= getattr(os, "O_NOFOLLOW", 0)
                    source_fd = os.open(source, source_flags)
                    source_stat = os.fstat(source_fd)
                    if (
                        not stat.S_ISREG(source_stat.st_mode)
                        or source_stat.st_uid != os.geteuid()
                        or stat.S_IMODE(source_stat.st_mode) != 0o600
                        or not 0 < source_stat.st_size <= 8192
                    ):
                        raise CoordinatorError("operator proof file changed during staging")
                    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    destination_flags |= getattr(os, "O_CLOEXEC", 0)
                    destination_flags |= getattr(os, "O_NOFOLLOW", 0)
                    destination_fd = os.open(staged_file, destination_flags, 0o600)
                    while True:
                        chunk = os.read(source_fd, 8192)
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_fd, view)
                            view = view[written:]
                    os.fchmod(destination_fd, 0o600)
                    os.fsync(destination_fd)
                finally:
                    if source_fd >= 0:
                        os.close(source_fd)
                    if destination_fd >= 0:
                        os.close(destination_fd)
                staged[name] = staged_file
            yield staged
        finally:
            for staged_file in staged_directory.iterdir():
                try:
                    staged_file.unlink()
                except FileNotFoundError:
                    pass
            try:
                staged_directory.rmdir()
            except FileNotFoundError:
                pass

    def _run_helper(
        self,
        arguments: Sequence[str],
        *,
        authority: Attestation,
        secret_files: Mapping[str, Path] | None = None,
    ) -> dict[str, object]:
        rewritten_arguments = list(arguments)
        operation = rewritten_arguments[0] if rewritten_arguments else ""
        if operation in MUTATING_HELPER_OPERATIONS:
            if authority.running:
                raise CoordinatorError("mutating OIDC helper requires a stopped vitals_app")
            current = self.attest(require_running=False)
            self._assert_same_authority(authority, current)
        runtime_source = str(self.runtime_env.parent)
        if "," in runtime_source:
            raise CoordinatorError("runtime mount path cannot contain a comma")
        selected = dict(secret_files or {})
        with self._staged_secret_files(selected) as staged:
            mount_arguments = [
                "--mount",
                f"type=bind,src={runtime_source},dst={RUNTIME_TARGET}",
            ]
            if staged:
                staged_parent = str(next(iter(staged.values())).parent)
                if "," in staged_parent:
                    raise CoordinatorError("staged secret path cannot contain a comma")
                mount_arguments.extend(
                    (
                        "--mount",
                        f"type=bind,src={staged_parent},dst={SECRET_TARGET},readonly",
                    )
                )
                for name, source in selected.items():
                    source_argument = str(source)
                    indices = [
                        index
                        for index, value in enumerate(rewritten_arguments)
                        if value == source_argument
                    ]
                    if len(indices) != 1:  # pragma: no cover - internal caller contract
                        raise CoordinatorError("operator proof argument was not staged")
                    rewritten_arguments[indices[0]] = f"{SECRET_TARGET}/{name}"
            result = self._execute(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull",
                    "never",
                    "--network",
                    authority.network_id,
                    "--user",
                    f"{os.geteuid()}:{os.getegid()}",
                    "--entrypoint",
                    "python",
                    "--workdir",
                    "/app",
                    *mount_arguments,
                    authority.image_id,
                    HELPER,
                    "--runtime-env",
                    RUNTIME_FILE,
                    *rewritten_arguments,
                ],
                boundary="OIDC runtime helper",
            )
        payload = _one_json_record(result.stdout, boundary="OIDC runtime helper")
        if payload.get("result") != "ok":
            raise CoordinatorError("OIDC runtime helper refused the operation")
        return payload

    @staticmethod
    def _provider_helper_arguments(operation: str, provider: ProviderArguments) -> list[str]:
        return [
            operation,
            "--issuer",
            provider.issuer,
            "--client-id",
            provider.client_id,
            "--client-secret-file",
            str(provider.client_secret_file),
            "--legacy-password-file",
            str(provider.legacy_password_file),
            "--redirect-url",
            provider.redirect_url,
            "--bootstrap-subject",
            provider.bootstrap_subject,
        ]

    def _helper_status(self, *, authority: Attestation) -> str:
        payload = self._run_helper(["status"], authority=authority)
        state = payload.get("readback")
        if state not in {"password", "oidc_bootstrap_pending", "oidc_bound"}:
            raise CoordinatorError("OIDC helper reported an unknown auth state")
        return str(state)

    def _stop_app(self) -> Attestation:
        self._execute(
            [*self._compose, "stop", "--timeout", "30", APP_SERVICE],
            boundary="vitals_app stop",
        )
        return self.attest(require_running=False)

    @staticmethod
    def _assert_same_authority(
        expected: Attestation | Mapping[str, object],
        actual: Attestation,
    ) -> None:
        expected_values = {
            "image": expected.image if isinstance(expected, Attestation) else expected.get("image"),
            "image_id": (
                expected.image_id if isinstance(expected, Attestation) else expected.get("image_id")
            ),
            "config_id": (
                expected.config_id
                if isinstance(expected, Attestation)
                else expected.get("config_id")
            ),
            "network": (
                expected.network if isinstance(expected, Attestation) else expected.get("network")
            ),
            "network_id": (
                expected.network_id
                if isinstance(expected, Attestation)
                else expected.get("network_id")
            ),
        }
        actual_values = {
            "image": actual.image,
            "image_id": actual.image_id,
            "config_id": actual.config_id,
            "network": actual.network,
            "network_id": actual.network_id,
        }
        if expected_values != actual_values:
            raise CoordinatorError("runtime image or Compose authority changed mid-operation")

    def _recreate_app(self, *, authority: Attestation) -> Attestation:
        current = self.attest(require_running=False)
        self._assert_same_authority(authority, current)
        self._execute(
            [
                *self._compose,
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "180",
                APP_SERVICE,
            ],
            boundary="vitals_app recreate",
        )
        recreated = self.attest(require_running=True)
        self._assert_same_authority(authority, recreated)
        return recreated

    def _application_origin(self) -> str:
        result = self._execute(
            [*self._compose, "port", APP_SERVICE, APP_PORT],
            boundary="vitals_app port lookup",
        )
        endpoint = result.stdout.strip()
        match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", endpoint)
        if match is None or not 1 <= int(match.group(1)) <= 65535:
            raise CoordinatorError("vitals_app is not published on one IPv4 loopback port")
        return f"http://{endpoint}"

    def _health_postflight(self) -> str:
        origin = self._application_origin()
        response = self.http_probe(f"{origin}/health")
        if response.status != 200:
            raise CoordinatorError("vitals_app health postflight failed")
        return origin

    def _anonymous_protected_postflight(self, origin: str) -> None:
        response = self.http_probe(f"{origin}/today")
        location = response.headers.get("Location") or response.headers.get("location")
        if response.status not in {302, 303, 307, 308} or not location:
            raise CoordinatorError("an anonymous protected route did not redirect")
        login_url = urljoin(f"{origin}/today", location)
        parsed_login = urlsplit(login_url)
        if (
            _normalized_origin(login_url, field="protected-route redirect")
            != _normalized_origin(origin, field="Vitals origin")
            or parsed_login.path != "/login"
        ):
            raise CoordinatorError("an anonymous protected route bypassed local login")

    def _password_postflight(self) -> None:
        origin = self._health_postflight()
        self._anonymous_protected_postflight(origin)
        if self.http_probe(f"{origin}/login").status != 200:
            raise CoordinatorError("password login postflight failed")
        if self.http_probe(f"{origin}/auth/start").status != 404:
            raise CoordinatorError("OIDC route remained active in password mode")

    def _oidc_postflight(self, issuer: str) -> None:
        issuer_parts = urlsplit(issuer)
        if issuer_parts.scheme != "https" or issuer_parts.path not in {"", "/"}:
            raise CoordinatorError("production OIDC issuer must be an HTTPS origin")
        expected_origin = _normalized_origin(issuer, field="OIDC issuer")
        origin = self._health_postflight()
        self._anonymous_protected_postflight(origin)
        login = self.http_probe(f"{origin}/login")
        login_location = login.headers.get("Location") or login.headers.get("location")
        if login.status not in {302, 303, 307, 308} or not login_location:
            raise CoordinatorError("OIDC login postflight did not redirect")
        start_url = urljoin(f"{origin}/login", login_location)
        parsed_start = urlsplit(start_url)
        if (
            _normalized_origin(start_url, field="OIDC start redirect")
            != _normalized_origin(origin, field="Vitals origin")
            or parsed_start.path != "/auth/start"
        ):
            raise CoordinatorError("login did not redirect to the local OIDC start route")
        start = self.http_probe(start_url)
        provider_location = start.headers.get("Location") or start.headers.get("location")
        if start.status not in {302, 303, 307, 308} or not provider_location:
            raise CoordinatorError("OIDC start postflight did not reach the provider")
        if _normalized_origin(provider_location, field="provider redirect") != expected_origin:
            raise CoordinatorError("OIDC start redirected to an unexpected provider")

    def _journal_identity(self) -> dict[str, object]:
        return {
            "compose_files": [str(path) for path in self.compose_files],
            "env_files": [str(path) for path in self.env_files],
            "project": self.project,
            "runtime_env": str(self.runtime_env),
            "service": APP_SERVICE,
        }

    def _runtime_oidc_authority_id(self, *, required: bool = False) -> str | None:
        """Return a secret-safe, session-keyed ID for the exact OIDC runtime group."""

        try:
            values = {
                key: read_env_key(
                    self.runtime_env,
                    key,
                    require_existing=True,
                    require_owner_only=True,
                ).strip()
                for key in (*OIDC_RUNTIME_KEYS, OIDC_BOOTSTRAP_KEY, SESSION_SECRET_KEY)
            }
        except Exception as exc:
            raise CoordinatorError("runtime OIDC authority could not be read safely") from exc
        present = [bool(values[key]) for key in OIDC_RUNTIME_KEYS]
        if any(present) and not all(present):
            raise CoordinatorError("runtime contains a partial OIDC authority")
        if not any(present):
            if values[OIDC_BOOTSTRAP_KEY]:
                raise CoordinatorError("runtime has an OIDC bootstrap subject without OIDC")
            if required:
                raise CoordinatorError("journaled OIDC authority is no longer configured")
            return None
        if not values[SESSION_SECRET_KEY]:
            raise CoordinatorError("runtime session authority is absent")
        canonical = json.dumps(
            [[key, values[key]] for key in (*OIDC_RUNTIME_KEYS, OIDC_BOOTSTRAP_KEY)],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(
            values[SESSION_SECRET_KEY].encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()

    def _assert_runtime_oidc_authority(
        self,
        journal: Mapping[str, object],
        *,
        required: bool,
    ) -> None:
        expected = journal.get("oidc_authority_id")
        if not isinstance(expected, str) or not expected:
            if required:
                raise CoordinatorError("cutover journal has no OIDC authority binding")
            return
        actual = self._runtime_oidc_authority_id(required=True)
        if actual is None or not hmac.compare_digest(expected, actual):
            raise CoordinatorError("runtime OIDC authority changed after the journal boundary")

    @contextmanager
    def operation_lock(self):
        """Exclude a second host coordinator from interleaving phases."""

        lock_path = self.state_file.with_name(f"{self.state_file.name}.lock")
        parent = lock_path.parent
        try:
            parent_stat = parent.lstat()
        except FileNotFoundError as exc:
            raise CoordinatorError("cutover lock directory is missing") from exc
        if (
            parent.is_symlink()
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
        ):
            raise CoordinatorError("cutover lock directory is not safely owned")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise CoordinatorError("cutover lock is not a safe regular file") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise CoordinatorError("cutover lock must be owner-only mode 0600")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CoordinatorError("another OIDC coordinator is already running") from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _validate_existing_journal_file(self) -> None:
        if not self.state_file.exists() and not self.state_file.is_symlink():
            return
        metadata = self.state_file.lstat()
        if (
            self.state_file.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise CoordinatorError("cutover journal must be owner-only mode 0600")

    def _read_journal(self, *, required: bool = False) -> dict[str, object] | None:
        self._validate_existing_journal_file()
        if not self.state_file.exists():
            if required:
                raise CoordinatorError("cutover journal is missing")
            return None
        payload = _json_object(
            self.state_file.read_text(encoding="utf-8"),
            boundary="cutover journal",
        )
        if payload.get("format_version") != STATE_FORMAT:
            raise CoordinatorError("cutover journal has an unsupported format")
        for key, value in self._journal_identity().items():
            if payload.get(key) != value:
                raise CoordinatorError("cutover journal belongs to another deployment")
        if not isinstance(payload.get("phase"), str):
            raise CoordinatorError("cutover journal has no phase")
        return payload

    def _guard_new_operation(
        self,
        *,
        attestation: Attestation,
        required_phase: str | None = None,
    ) -> dict[str, object] | None:
        journal = self._read_journal()
        if journal is None:
            if required_phase is not None:
                raise CoordinatorError("cutover journal is missing")
            return None
        phase = str(journal["phase"])
        if phase in INCOMPLETE_PHASES:
            self._assert_same_authority(journal, attestation)
            raise CoordinatorError("an incomplete OIDC operation must be recovered first")
        if phase not in STABLE_PHASES:
            raise CoordinatorError("cutover journal has an unknown phase")
        if required_phase is not None and phase != required_phase:
            raise CoordinatorError(f"operation requires journal phase {required_phase!r}")
        if required_phase is not None:
            self._assert_same_authority(journal, attestation)
        if required_phase in {"awaiting_owner_binding", "oidc_bound"}:
            self._assert_runtime_oidc_authority(journal, required=True)
        return journal

    def _guard_recovery_deployment_authority(
        self,
        *,
        journal: Mapping[str, object],
        attestation: Attestation,
    ) -> None:
        phase = str(journal.get("phase") or "")
        if phase not in INCOMPLETE_PHASES and phase not in STABLE_PHASES:
            raise CoordinatorError("cutover journal has an unknown phase")
        self._assert_same_authority(journal, attestation)

    def _guard_recovery_runtime_authority(
        self,
        *,
        journal: Mapping[str, object],
        auth_state: str,
    ) -> None:
        phase = str(journal.get("phase") or "")
        oidc_bound_phases = {
            "cutover_config_written",
            "cutover_postflight_failed",
            "awaiting_owner_binding",
            "finalize_config_written",
            "oidc_bound",
            "retire_config_written",
            "legacy_retired",
        }
        compensated_password_phases = {
            "cutover_config_written",
            "cutover_postflight_failed",
            "recovery_required",
        }
        if phase in oidc_bound_phases and not (
            auth_state == "password" and phase in compensated_password_phases
        ):
            self._assert_runtime_oidc_authority(journal, required=True)

    def _write_journal(
        self,
        *,
        phase: str,
        operation: str,
        attestation: Attestation,
        proof_not_before: str | None = None,
    ) -> None:
        self._validate_existing_journal_file()
        parent = self.state_file.parent
        try:
            parent_stat = parent.lstat()
        except FileNotFoundError as exc:
            raise CoordinatorError("cutover journal directory is missing") from exc
        if (
            parent.is_symlink()
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
        ):
            raise CoordinatorError("cutover journal directory is not safely owned")
        payload = {
            "format_version": STATE_FORMAT,
            **self._journal_identity(),
            "operation": operation,
            "phase": phase,
            "image": attestation.image,
            "image_id": attestation.image_id,
            "config_id": attestation.config_id,
            "network": attestation.network,
            "network_id": attestation.network_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        oidc_authority_id = self._runtime_oidc_authority_id()
        if oidc_authority_id is not None:
            payload["oidc_authority_id"] = oidc_authority_id
        if proof_not_before is not None:
            payload["proof_not_before"] = proof_not_before
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{self.state_file.name}.",
                suffix=".tmp",
                dir=parent,
            )
            temporary = Path(raw_path)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_file)
            temporary = None
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def status(self) -> dict[str, object]:
        attestation = self.attest()
        auth_state = self._helper_status(authority=attestation)
        journal = self._read_journal()
        return {
            "auth_state": auth_state,
            "container_running": attestation.running,
            "journal_phase": journal.get("phase") if journal else None,
            "operation": "oidc_cutover_host_status",
            "result": "ok",
        }

    def preflight(self, provider: ProviderArguments) -> dict[str, object]:
        attestation = self.attest(require_running=True)
        self._guard_new_operation(attestation=attestation)
        self._password_postflight()
        self._run_helper(
            self._provider_helper_arguments("preflight", provider),
            authority=attestation,
            secret_files={
                "client-secret": provider.client_secret_file,
                "legacy-password": provider.legacy_password_file,
            },
        )
        self._write_journal(
            phase="preflight_passed",
            operation="cutover",
            attestation=attestation,
        )
        return {"operation": "oidc_cutover_host_preflight", "result": "ok"}

    def _compensate_failed_cutover(
        self,
        *,
        attestation: Attestation,
        legacy_password_file: Path,
    ) -> None:
        # The journal is evidence, not permission to skip the safety action.  A
        # full disk or transient fsync failure here must not prevent rollback.
        try:
            self._write_journal(
                phase="cutover_postflight_failed",
                operation="cutover",
                attestation=attestation,
            )
        except Exception:
            pass
        try:
            stopped = self._stop_app() if attestation.running else attestation
            self._assert_same_authority(attestation, stopped)
            self._run_helper(
                [
                    "rollback",
                    "--legacy-password-file",
                    str(legacy_password_file),
                    "--confirm",
                    HELPER_ROLLBACK_CONFIRMATION,
                ],
                authority=stopped,
                secret_files={"legacy-password": legacy_password_file},
            )
            self._write_journal(
                phase="cutover_compensation_written",
                operation="cutover",
                attestation=stopped,
            )
            running = self._recreate_app(authority=stopped)
            self._password_postflight()
            self._write_journal(
                phase="rolled_back",
                operation="cutover",
                attestation=running,
            )
        except Exception as exc:
            try:
                current = self.attest()
                self._write_journal(
                    phase="recovery_required",
                    operation="cutover",
                    attestation=current,
                )
            except Exception:
                pass
            raise CoordinatorError(
                "OIDC postflight failed and automatic password recovery failed; run recover"
            ) from exc

    def cutover(
        self,
        provider: ProviderArguments,
        *,
        confirmation: str | None,
    ) -> dict[str, object]:
        if confirmation != CUTOVER_CONFIRMATION:
            raise CoordinatorError("cutover confirmation did not match")
        self.preflight(provider)
        authority = self.attest(require_running=True)
        stopped = self._stop_app()
        self._assert_same_authority(authority, stopped)
        try:
            self._write_journal(
                phase="cutover_web_stopped",
                operation="cutover",
                attestation=stopped,
            )
            self._run_helper(
                [
                    *self._provider_helper_arguments("enable", provider),
                    "--confirm",
                    HELPER_ENABLE_CONFIRMATION,
                ],
                authority=stopped,
                secret_files={
                    "client-secret": provider.client_secret_file,
                    "legacy-password": provider.legacy_password_file,
                },
            )
        except Exception as exc:
            try:
                running = self._recreate_app(authority=stopped)
                self._password_postflight()
                self._write_journal(
                    phase="rolled_back",
                    operation="cutover",
                    attestation=running,
                )
            except Exception as recovery_exc:
                raise CoordinatorError(
                    "OIDC enable failed and password application recovery failed; run recover"
                ) from recovery_exc
            raise CoordinatorError("OIDC enable failed; password mode was restored") from exc
        try:
            self._write_journal(
                phase="cutover_config_written",
                operation="cutover",
                attestation=stopped,
            )
            running = self._recreate_app(authority=stopped)
            self._oidc_postflight(provider.issuer)
        except Exception as exc:
            # A failed `compose up` can leave either the previous stopped
            # container or no inspectable replacement.  The already attested
            # stopped image/mount remains sufficient authority to run the
            # no-deps helper and recreate the service from rendered Compose.
            current = stopped
            try:
                current = self.attest()
                self._assert_same_authority(stopped, current)
            except CoordinatorError:
                current = stopped
            self._compensate_failed_cutover(
                attestation=current,
                legacy_password_file=provider.legacy_password_file,
            )
            raise CoordinatorError("OIDC postflight failed; password mode was restored") from exc
        self._write_journal(
            phase="awaiting_owner_binding",
            operation="cutover",
            attestation=running,
        )
        return {
            "next_action": "complete the owner OIDC login, then run finalize",
            "operation": "oidc_cutover_host_cutover",
            "result": "ok",
            "session_secret_rotated": True,
        }

    def finalize(
        self,
        *,
        issuer: str,
        confirmation: str | None,
    ) -> dict[str, object]:
        if confirmation != FINALIZE_CONFIRMATION:
            raise CoordinatorError("finalize confirmation did not match")
        attestation = self.attest(require_running=True)
        journal = self._guard_new_operation(
            attestation=attestation,
            required_phase="awaiting_owner_binding",
        )
        assert journal is not None  # required_phase makes absence a refusal
        not_before = journal.get("updated_at")
        if not isinstance(not_before, str) or not not_before:
            raise CoordinatorError("cutover journal has no owner-login proof boundary")
        self._oidc_postflight(issuer)
        stopped = self._stop_app()
        self._assert_same_authority(attestation, stopped)
        self._write_journal(
            phase="finalize_web_stopped",
            operation="finalize",
            attestation=stopped,
        )
        try:
            self._run_helper(
                [
                    "finalize",
                    "--not-before",
                    not_before,
                    "--confirm",
                    HELPER_FINALIZE_CONFIRMATION,
                ],
                authority=stopped,
            )
            self._write_journal(
                phase="finalize_config_written",
                operation="finalize",
                attestation=stopped,
            )
            running = self._recreate_app(authority=stopped)
            self._oidc_postflight(issuer)
        except Exception:
            # The config helper is idempotently recoverable from its reported
            # auth state.  Put the service back before returning an error.
            try:
                running = self._recreate_app(authority=stopped)
                self._oidc_postflight(issuer)
                state = self._helper_status(authority=running)
                phase = "oidc_bound" if state == "oidc_bound" else "awaiting_owner_binding"
                self._write_journal(
                    phase=phase,
                    operation="finalize",
                    attestation=running,
                )
            except Exception as recovery_exc:
                raise CoordinatorError("finalize failed and requires recover") from recovery_exc
            raise CoordinatorError("finalize failed; the OIDC application was restored")
        self._write_journal(
            phase="oidc_bound",
            operation="finalize",
            attestation=running,
        )
        return {"operation": "oidc_cutover_host_finalize", "result": "ok"}

    def rollback(
        self,
        *,
        issuer: str,
        legacy_password_file: Path,
        confirmation: str | None,
    ) -> dict[str, object]:
        if confirmation != ROLLBACK_CONFIRMATION:
            raise CoordinatorError("rollback confirmation did not match")
        attestation = self.attest(require_running=True)
        journal = self._guard_new_operation(attestation=attestation)
        if journal and journal.get("phase") == "legacy_retired":
            raise CoordinatorError("normal password rollback is retired")
        self._oidc_postflight(issuer)
        stopped = self._stop_app()
        self._assert_same_authority(attestation, stopped)
        self._write_journal(
            phase="rollback_web_stopped",
            operation="rollback",
            attestation=stopped,
        )
        try:
            self._run_helper(
                [
                    "rollback",
                    "--legacy-password-file",
                    str(legacy_password_file),
                    "--confirm",
                    HELPER_ROLLBACK_CONFIRMATION,
                ],
                authority=stopped,
                secret_files={"legacy-password": legacy_password_file},
            )
            self._write_journal(
                phase="rollback_config_written",
                operation="rollback",
                attestation=stopped,
            )
            running = self._recreate_app(authority=stopped)
            self._password_postflight()
        except Exception:
            try:
                running = self._recreate_app(authority=stopped)
                current = self._helper_status(authority=running)
                if current == "password":
                    self._password_postflight()
                    phase = "rolled_back"
                else:
                    self._oidc_postflight(issuer)
                    phase = "oidc_bound" if current == "oidc_bound" else "awaiting_owner_binding"
                self._write_journal(
                    phase=phase,
                    operation="rollback",
                    attestation=running,
                )
            except Exception as recovery_exc:
                raise CoordinatorError("rollback failed and requires recover") from recovery_exc
            raise CoordinatorError("rollback failed; the prior auth mode was restored")
        self._write_journal(
            phase="rolled_back",
            operation="rollback",
            attestation=running,
        )
        return {
            "operation": "oidc_cutover_host_rollback",
            "result": "ok",
            "session_secret_rotated": True,
        }

    def retire_legacy(
        self,
        *,
        issuer: str,
        confirmation: str | None,
    ) -> dict[str, object]:
        if confirmation != RETIRE_CONFIRMATION:
            raise CoordinatorError("legacy retirement confirmation did not match")
        attestation = self.attest(require_running=True)
        journal = self._guard_new_operation(
            attestation=attestation,
            required_phase="oidc_bound",
        )
        assert journal is not None
        not_before = journal.get("updated_at")
        if not isinstance(not_before, str) or not not_before:
            raise CoordinatorError("cutover journal has no retirement proof boundary")
        if self._helper_status(authority=attestation) != "oidc_bound":
            raise CoordinatorError("legacy credentials retire only after OIDC finalization")
        self._oidc_postflight(issuer)
        self._run_helper(
            ["retire-preflight", "--not-before", not_before],
            authority=attestation,
        )
        stopped = self._stop_app()
        self._assert_same_authority(attestation, stopped)
        self._write_journal(
            phase="retire_web_stopped",
            operation="retire_legacy",
            attestation=stopped,
            proof_not_before=not_before,
        )
        try:
            self._run_helper(
                [
                    "retire-legacy",
                    "--not-before",
                    not_before,
                    "--confirm",
                    HELPER_RETIRE_CONFIRMATION,
                ],
                authority=stopped,
            )
            self._write_journal(
                phase="retire_config_written",
                operation="retire_legacy",
                attestation=stopped,
                proof_not_before=not_before,
            )
            running = self._recreate_app(authority=stopped)
            self._oidc_postflight(issuer)
        except Exception as exc:
            raise CoordinatorError("legacy retirement requires recover") from exc
        self._write_journal(
            phase="legacy_retired",
            operation="retire_legacy",
            attestation=running,
            proof_not_before=not_before,
        )
        return {"operation": "oidc_cutover_host_retire_legacy", "result": "ok"}

    def recover(
        self,
        *,
        issuer: str | None,
        legacy_password_file: Path | None = None,
    ) -> dict[str, object]:
        journal = self._read_journal(required=True)
        attestation = self.attest()
        self._guard_recovery_deployment_authority(
            journal=journal,
            attestation=attestation,
        )
        auth_state = self._helper_status(authority=attestation)
        self._guard_recovery_runtime_authority(
            journal=journal,
            auth_state=auth_state,
        )
        phase = str(journal["phase"])
        operation = str(journal.get("operation") or "recover")

        failed_cutover = operation == "cutover" and phase in {
            "cutover_postflight_failed",
            "cutover_compensation_written",
            "recovery_required",
        }
        compensated_password = operation == "cutover" and phase in {
            "cutover_config_written",
            "cutover_postflight_failed",
            "recovery_required",
        }
        if compensated_password and auth_state == "password":
            if legacy_password_file is None:
                raise CoordinatorError(
                    "--legacy-password-file is required to prove compensated password recovery"
                )
            self._run_helper(
                [
                    "password-preflight",
                    "--legacy-password-file",
                    str(legacy_password_file),
                ],
                authority=attestation,
                secret_files={"legacy-password": legacy_password_file},
            )
        if failed_cutover and auth_state != "password":
            if legacy_password_file is None:
                raise CoordinatorError("--legacy-password-file is required for password recovery")
            if attestation.running:
                stopped = self._stop_app()
                self._assert_same_authority(attestation, stopped)
                attestation = stopped
            self._run_helper(
                [
                    "rollback",
                    "--legacy-password-file",
                    str(legacy_password_file),
                    "--confirm",
                    HELPER_ROLLBACK_CONFIRMATION,
                ],
                authority=attestation,
                secret_files={"legacy-password": legacy_password_file},
            )
            auth_state = "password"

        if operation == "retire_legacy" and phase == "retire_web_stopped":
            proof_not_before = journal.get("proof_not_before")
            if not isinstance(proof_not_before, str) or not proof_not_before:
                raise CoordinatorError("retirement journal lost its proof boundary")
            if attestation.running:
                stopped = self._stop_app()
                self._assert_same_authority(attestation, stopped)
                attestation = stopped
            self._run_helper(
                [
                    "retire-legacy",
                    "--not-before",
                    proof_not_before,
                    "--allow-already-retired",
                    "--confirm",
                    HELPER_RETIRE_CONFIRMATION,
                ],
                authority=attestation,
            )
            auth_state = self._helper_status(authority=attestation)

        if not attestation.running:
            attestation = self._recreate_app(authority=attestation)

        if auth_state == "password":
            self._password_postflight()
            resolved_phase = (
                "rolled_back" if operation in {"cutover", "rollback"} else "password_ready"
            )
        else:
            if not issuer:
                raise CoordinatorError("--issuer is required to recover an OIDC runtime")
            try:
                self._oidc_postflight(issuer)
            except Exception as exc:
                incomplete_cutover = operation == "cutover" and phase in {
                    "preflight_passed",
                    "cutover_web_stopped",
                    "cutover_config_written",
                }
                if incomplete_cutover:
                    if legacy_password_file is None:
                        raise CoordinatorError(
                            "--legacy-password-file is required for password recovery"
                        ) from exc
                    self._compensate_failed_cutover(
                        attestation=attestation,
                        legacy_password_file=legacy_password_file,
                    )
                    raise CoordinatorError(
                        "recovered cutover failed postflight; password mode was restored"
                    ) from exc
                raise
            if operation == "retire_legacy":
                resolved_phase = "legacy_retired"
            else:
                resolved_phase = (
                    "oidc_bound" if auth_state == "oidc_bound" else "awaiting_owner_binding"
                )
        self._write_journal(
            phase=resolved_phase,
            operation="recover",
            attestation=attestation,
            proof_not_before=(
                str(journal["proof_not_before"])
                if operation == "retire_legacy" and journal.get("proof_not_before")
                else None
            ),
        )
        return {
            "auth_state": auth_state,
            "journal_phase": resolved_phase,
            "operation": "oidc_cutover_host_recover",
            "result": "ok",
        }


def _provider_from_args(args: argparse.Namespace) -> ProviderArguments:
    return ProviderArguments(
        issuer=args.issuer.strip(),
        client_id=args.client_id.strip(),
        client_secret_file=args.client_secret_file,
        legacy_password_file=args.legacy_password_file,
        redirect_url=args.redirect_url.strip(),
        bootstrap_subject=args.bootstrap_subject.strip(),
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--compose-file",
        action="append",
        required=True,
        type=Path,
        help="repeat in the exact production Compose order",
    )
    parser.add_argument("--env-file", action="append", default=[], type=Path)
    parser.add_argument(
        "--runtime-env",
        required=True,
        type=Path,
        help="host path corresponding to /run/vitals-runtime/vitals.env",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(".vitals-oidc-cutover-state"),
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("status")

    def add_provider(command: argparse.ArgumentParser) -> None:
        command.add_argument("--issuer", required=True)
        command.add_argument("--client-id", required=True)
        command.add_argument("--client-secret-file", required=True, type=Path)
        command.add_argument("--legacy-password-file", required=True, type=Path)
        command.add_argument("--redirect-url", required=True)
        command.add_argument("--bootstrap-subject", required=True)

    preflight = commands.add_parser("preflight")
    add_provider(preflight)
    cutover = commands.add_parser("cutover")
    add_provider(cutover)
    cutover.add_argument("--confirm")

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--issuer", required=True)
    finalize.add_argument("--confirm")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--issuer", required=True)
    rollback.add_argument("--legacy-password-file", required=True, type=Path)
    rollback.add_argument("--confirm")
    recover = commands.add_parser("recover")
    recover.add_argument("--issuer")
    recover.add_argument("--legacy-password-file", type=Path)
    retire = commands.add_parser("retire-legacy")
    retire.add_argument("--issuer", required=True)
    retire.add_argument("--confirm")
    return parser.parse_args(argv)


def _emit(payload: Mapping[str, object], *, error: bool = False) -> None:
    print(json.dumps(payload, sort_keys=True), file=sys.stderr if error else sys.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        coordinator = OidcCutoverHost(
            project=args.project,
            compose_files=args.compose_file,
            env_files=args.env_file,
            runtime_env=args.runtime_env,
            state_file=args.state_file,
        )
        with coordinator.operation_lock():
            if args.operation == "status":
                payload = coordinator.status()
            elif args.operation == "preflight":
                payload = coordinator.preflight(_provider_from_args(args))
            elif args.operation == "cutover":
                payload = coordinator.cutover(_provider_from_args(args), confirmation=args.confirm)
            elif args.operation == "finalize":
                payload = coordinator.finalize(
                    issuer=args.issuer.strip(), confirmation=args.confirm
                )
            elif args.operation == "rollback":
                payload = coordinator.rollback(
                    issuer=args.issuer.strip(),
                    legacy_password_file=args.legacy_password_file,
                    confirmation=args.confirm,
                )
            elif args.operation == "recover":
                payload = coordinator.recover(
                    issuer=args.issuer.strip() if args.issuer else None,
                    legacy_password_file=args.legacy_password_file,
                )
            elif args.operation == "retire-legacy":
                payload = coordinator.retire_legacy(
                    issuer=args.issuer.strip(), confirmation=args.confirm
                )
            else:  # pragma: no cover - argparse owns the boundary
                raise CoordinatorError("unsupported coordinator operation")
    except CoordinatorError as exc:
        _emit(
            {
                "operation": f"oidc_cutover_host_{args.operation.replace('-', '_')}",
                "reason": str(exc),
                "result": "error",
            },
            error=True,
        )
        return 2
    except Exception:
        # A production coordinator must never turn an unexpected library error
        # into a traceback containing subprocess output or a credential path.
        _emit(
            {
                "operation": f"oidc_cutover_host_{args.operation.replace('-', '_')}",
                "reason": "unexpected coordinator failure",
                "result": "error",
            },
            error=True,
        )
        return 2
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
