#!/usr/bin/env python3
"""Check recovery-stream freshness without reading recovery payloads or secrets."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Sequence


FORMAT_VERSION = 2
COMMAND_TIMEOUT_SECONDS = 5
ALLOWED_STREAMS = frozenset({"health-local", "health-offsite", "idp-local", "idp-offsite"})
STREAM_SERVICE = {
    "health-local": "vitals_backup",
    "health-offsite": "vitals_offsite_backup",
    "idp-local": "vitals_idp_backup",
    "idp-offsite": "vitals_idp_offsite_backup",
}
SERVICE_NAMES = frozenset(STREAM_SERVICE.values())
MANIFEST_PATTERNS = {
    "health": re.compile(r"vitals_bundle_(\d{8}T\d{6}Z)\.sha256"),
    "idp": re.compile(r"zitadel_bundle_(\d{8}T\d{6}Z)\.sha256"),
}
MANIFEST_PREFIXES = {
    "health": "vitals_bundle_",
    "idp": "zitadel_bundle_",
}
PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{12,64}")


@dataclass(frozen=True)
class Result:
    level: str
    key: str
    message: str


@dataclass(frozen=True)
class Manifest:
    path: Path
    name: str
    timestamp: datetime


@dataclass(frozen=True)
class Container:
    service: str
    container_id: str
    status: str
    restarting: bool
    restart_count: int
    health: str | None


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class MonitorError(RuntimeError):
    """The checker itself could not establish an answer."""


class RecoveryProblem(RuntimeError):
    """A required recovery artifact is observably invalid."""


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MonitorError(f"command unavailable or timed out: {command[0]}") from exc


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise MonitorError("timestamp is not a valid UTC calendar time") from exc
    return parsed.replace(tzinfo=UTC)


def _parse_expiry(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise MonitorError("Login V2 PAT expiration is not a valid UTC timestamp") from exc
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    if canonical != value:
        raise MonitorError("Login V2 PAT expiration is not canonical UTC")
    return parsed.replace(tzinfo=UTC)


def _require_absolute(path: Path, name: str) -> None:
    if not path.is_absolute():
        raise MonitorError(f"{name} must be an absolute path")


def _manifest_directory(backup_root: Path, kind: str) -> Path:
    return backup_root if kind == "health" else backup_root / "idp"


def discover_latest_manifest(backup_root: Path, kind: str) -> Manifest:
    directory = _manifest_directory(backup_root, kind)
    pattern = MANIFEST_PATTERNS[kind]
    prefix = MANIFEST_PREFIXES[kind]
    manifests: list[Manifest] = []
    malformed: list[str] = []
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise MonitorError(f"{kind} backup directory is unavailable") from exc

    for entry in entries:
        if not (entry.name.startswith(prefix) and entry.name.endswith(".sha256")):
            continue
        match = pattern.fullmatch(entry.name)
        if match is None or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            malformed.append(entry.name)
            continue
        try:
            timestamp = _parse_utc_timestamp(match.group(1))
            size = entry.stat(follow_symlinks=False).st_size
        except (MonitorError, OSError):
            malformed.append(entry.name)
            continue
        if size < 1:
            malformed.append(entry.name)
            continue
        manifests.append(Manifest(Path(entry.path), entry.name, timestamp))

    if malformed:
        raise RecoveryProblem(f"{kind} backup directory contains a malformed manifest")
    if not manifests:
        raise FileNotFoundError(f"no complete {kind} recovery manifest exists")
    return max(manifests, key=lambda item: (item.timestamp, item.name))


def _freshness_result(
    *,
    key: str,
    label: str,
    timestamp: datetime,
    now: datetime,
    maximum_age: int,
    clock_skew: int,
) -> Result:
    age = int((now - timestamp).total_seconds())
    if age < -clock_skew:
        return Result("critical", key, f"{label} timestamp is in the future")
    effective_age = max(age, 0)
    if effective_age > maximum_age:
        return Result(
            "critical",
            key,
            f"{label} is stale (age={effective_age}s, limit={maximum_age}s)",
        )
    return Result("ok", key, f"{label} is fresh (age={effective_age}s)")


def inspect_container(project: str, service: str, runner: Runner) -> Container:
    selection = runner(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--format",
            "{{.ID}}",
        ]
    )
    if selection.returncode != 0:
        raise MonitorError(f"Docker could not select {service}")
    matches = [line.strip() for line in selection.stdout.splitlines() if line.strip()]
    if len(matches) != 1 or not CONTAINER_ID_PATTERN.fullmatch(matches[0]):
        raise MonitorError(
            f"project attestation expected one {service} container, found {len(matches)}"
        )
    container_id = matches[0]
    template = (
        "[{{json .State.Status}},{{json .State.Restarting}},"
        "{{json .RestartCount}},{{if .State.Health}}"
        "{{json .State.Health.Status}}{{else}}null{{end}}]"
    )
    inspected = runner(["docker", "inspect", "--format", template, container_id])
    if inspected.returncode != 0:
        raise MonitorError(f"Docker could not inspect {service}")
    try:
        payload = json.loads(inspected.stdout)
        status, restarting, restart_count, health = payload
    except (ValueError, TypeError) as exc:
        raise MonitorError(f"Docker returned malformed state for {service}") from exc
    if (
        not isinstance(status, str)
        or not isinstance(restarting, bool)
        or type(restart_count) is not int
        or restart_count < 0
        or (health is not None and not isinstance(health, str))
    ):
        raise MonitorError(f"Docker returned malformed state for {service}")
    return Container(service, container_id, status, restarting, restart_count, health)


def _pending_restart_result(service: str, pending: object) -> Result | None:
    if not isinstance(pending, dict):
        return None
    return Result(
        "warning",
        f"restart.{service}",
        "unacknowledged restart activity: "
        f"{pending['reason']} "
        "(acknowledge with: acknowledge --state-file <configured-state-file> "
        f"--service {service} --container-id {pending['container_id']} "
        f"--restart-count {pending['restart_count']})",
    )


def _container_results(
    container: Container, previous: dict[str, object] | None
) -> tuple[list[Result], dict[str, object]]:
    results: list[Result] = []
    key = f"container.{container.service}"
    if container.status != "running" or container.restarting:
        results.append(
            Result(
                "critical",
                key,
                f"container is {container.status} (restarting={str(container.restarting).lower()})",
            )
        )
    elif container.health is not None and container.health != "healthy":
        results.append(Result("critical", key, f"container health is {container.health}"))
    else:
        results.append(Result("ok", key, "container is running"))

    previous_id = previous.get("container_id") if isinstance(previous, dict) else None
    previous_count = previous.get("restart_count") if isinstance(previous, dict) else None
    pending = previous.get("pending_restart") if isinstance(previous, dict) else None
    restart_key = f"restart.{container.service}"
    reason: str | None = None
    if previous_id is not None and previous_id != container.container_id:
        reason = "container ID changed after the established baseline"
    elif previous_id == container.container_id and isinstance(previous_count, int):
        if container.restart_count < previous_count:
            results.append(Result("unknown", restart_key, "restart count moved backwards"))
        elif container.restart_count > previous_count:
            reason = f"restart count increased from {previous_count} to {container.restart_count}"
    elif container.restart_count > 0:
        reason = f"newly observed container already has {container.restart_count} restarts"

    if reason is not None:
        pending = {
            "container_id": container.container_id,
            "restart_count": container.restart_count,
            "reason": reason,
        }
    pending_result = _pending_restart_result(container.service, pending)
    if pending_result is not None:
        results.append(pending_result)
    elif not any(result.key == restart_key for result in results):
        results.append(
            Result(
                "ok",
                restart_key,
                f"restart baseline is stable at {container.restart_count}",
            )
        )
    record = {
        "container_id": container.container_id,
        "restart_count": container.restart_count,
        "pending_restart": pending,
    }
    return results, record


def read_marker(container_id: str, runner: Runner) -> str:
    marker_script = (
        "marker=/state/last-successful-manifest; "
        'test -f "$marker" && test ! -L "$marker" && '
        "awk 'NR > 1 { exit 1 } { value=$0 } "
        'END { if (NR != 1 || value == "") exit 1; print value }\' "$marker"'
    )
    completed = runner(["docker", "exec", container_id, "/bin/sh", "-c", marker_script])
    if completed.returncode != 0:
        raise MonitorError("offsite marker is missing or malformed")
    return completed.stdout.rstrip("\n")


def validate_marker(
    *,
    marker_name: str,
    backup_root: Path,
    kind: str,
    local: Manifest,
    now: datetime,
    rpo_seconds: int,
    replication_delay_seconds: int,
    clock_skew_seconds: int,
) -> list[Result]:
    pattern = MANIFEST_PATTERNS[kind]
    match = pattern.fullmatch(marker_name)
    key = f"freshness.{kind}-offsite"
    if match is None or "/" in marker_name:
        return [Result("critical", key, "offsite marker name is malformed")]
    try:
        marker_timestamp = _parse_utc_timestamp(match.group(1))
    except MonitorError:
        return [Result("critical", key, "offsite marker timestamp is invalid")]
    marker_path = _manifest_directory(backup_root, kind) / marker_name
    try:
        marker_stat = marker_path.lstat()
    except OSError:
        return [Result("critical", key, "offsite marker names no local manifest")]
    if not stat.S_ISREG(marker_stat.st_mode) or marker_path.is_symlink() or marker_stat.st_size < 1:
        return [Result("critical", key, "offsite marker names no regular local manifest")]

    results = [
        _freshness_result(
            key=key,
            label=f"{kind} offsite recovery point",
            timestamp=marker_timestamp,
            now=now,
            maximum_age=rpo_seconds + replication_delay_seconds,
            clock_skew=clock_skew_seconds,
        )
    ]
    if local.timestamp > marker_timestamp:
        local_age = int((now - local.timestamp).total_seconds())
        if local_age > replication_delay_seconds:
            results.append(
                Result(
                    "critical",
                    f"replication.{kind}",
                    f"newest local recovery point is unreplicated after {local_age}s",
                )
            )
        else:
            results.append(
                Result(
                    "ok",
                    f"replication.{kind}",
                    f"newest local recovery point is within the {replication_delay_seconds}s grace",
                )
            )
    else:
        results.append(
            Result("ok", f"replication.{kind}", "newest local recovery point is replicated")
        )
    return results


def read_pat_expiration(path: Path) -> datetime:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MonitorError("IDP environment file is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise MonitorError("IDP environment file must be owner-owned and owner-only")
    matches: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VITALS_IDP_LOGIN_PAT_EXPIRATION="):
                    matches.append(line.rstrip("\n").split("=", 1)[1])
    except (OSError, UnicodeError) as exc:
        raise MonitorError("IDP environment file could not be parsed") from exc
    if len(matches) != 1 or not matches[0]:
        raise MonitorError("IDP environment must declare one Login V2 PAT expiration")
    return _parse_expiry(matches[0])


def _pat_result(
    expiry: datetime,
    now: datetime,
    warning_seconds: int,
    critical_seconds: int,
) -> Result:
    remaining = int((expiry - now).total_seconds())
    if remaining <= 0:
        return Result("critical", "pat.expiry", "Login V2 PAT has expired")
    if remaining <= critical_seconds:
        return Result("critical", "pat.expiry", f"Login V2 PAT expires in {remaining}s")
    if remaining <= warning_seconds:
        return Result("warning", "pat.expiry", f"Login V2 PAT expires in {remaining}s")
    return Result("ok", "pat.expiry", f"Login V2 PAT expires in {remaining}s")


def load_state(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        if path.is_symlink():
            raise MonitorError("monitor state must not be a symlink")
        return {}
    try:
        metadata = path.lstat()
        if path.is_symlink():
            raise MonitorError("monitor state must not be a symlink")
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise MonitorError("monitor state must be a private regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except MonitorError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise MonitorError("monitor state is malformed") from exc
    if not isinstance(payload, dict) or payload.get("format_version") not in {1, FORMAT_VERSION}:
        raise MonitorError("monitor state is malformed")
    containers = payload.get("containers")
    if not isinstance(containers, dict):
        raise MonitorError("monitor state is malformed")
    normalized: dict[str, dict[str, object]] = {}
    for service, value in containers.items():
        if (
            service not in SERVICE_NAMES
            or not isinstance(value, dict)
            or not isinstance(value.get("container_id"), str)
            or CONTAINER_ID_PATTERN.fullmatch(value["container_id"]) is None
            or type(value.get("restart_count")) is not int
            or value["restart_count"] < 0
        ):
            raise MonitorError("monitor state is malformed")
        pending = value.get("pending_restart")
        if pending is not None and (
            not isinstance(pending, dict)
            or CONTAINER_ID_PATTERN.fullmatch(str(pending.get("container_id", ""))) is None
            or type(pending.get("restart_count")) is not int
            or pending["restart_count"] < 0
            or not isinstance(pending.get("reason"), str)
            or not pending["reason"]
        ):
            raise MonitorError("monitor state is malformed")
        normalized[service] = {
            "container_id": value["container_id"],
            "restart_count": value["restart_count"],
            "pending_restart": pending,
        }
    return normalized


@contextmanager
def state_lock(path: Path):
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise MonitorError("monitor state directory is unavailable") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or parent.is_symlink():
        raise MonitorError("monitor state directory must be a regular directory")
    lock_path = path.with_name(f"{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise MonitorError("monitor state lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise MonitorError("monitor state lock must be a private regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise MonitorError("monitor state lock failed") from exc
    finally:
        os.close(descriptor)


def write_state(path: Path, containers: dict[str, dict[str, object]]) -> None:
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise MonitorError("monitor state directory is unavailable") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or parent.is_symlink():
        raise MonitorError("monitor state directory must be a regular directory")
    if path.is_symlink():
        raise MonitorError("monitor state must not be a symlink")
    descriptor = -1
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(
                {"format_version": FORMAT_VERSION, "containers": containers},
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
    except OSError as exc:
        raise MonitorError("monitor state could not be published") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="run all configured checks")
    check_parser.add_argument("--project", required=True)
    check_parser.add_argument("--backup-root", required=True, type=Path)
    check_parser.add_argument("--streams", required=True)
    check_parser.add_argument(
        "--idp-env", type=Path, default=os.environ.get("VITALS_MONITOR_IDP_ENV")
    )
    check_parser.add_argument("--state-file", required=True, type=Path)
    check_parser.add_argument("--rpo-seconds", type=int, default=86400)
    check_parser.add_argument("--replication-delay-seconds", type=int, default=900)
    check_parser.add_argument("--pat-warning-seconds", type=int, default=2592000)
    check_parser.add_argument("--pat-critical-seconds", type=int, default=604800)
    check_parser.add_argument("--clock-skew-seconds", type=int, default=300)
    check_parser.add_argument("--output", choices=("text", "json"), default="text")
    acknowledge_parser = commands.add_parser(
        "acknowledge", help="acknowledge one exact observed restart or recreation"
    )
    acknowledge_parser.add_argument("--state-file", required=True, type=Path)
    acknowledge_parser.add_argument("--service", required=True, choices=sorted(SERVICE_NAMES))
    acknowledge_parser.add_argument("--container-id", required=True)
    acknowledge_parser.add_argument("--restart-count", required=True, type=int)
    return parser


def _validate_args(args: argparse.Namespace) -> list[str]:
    if PROJECT_PATTERN.fullmatch(args.project) is None:
        raise MonitorError("project name is invalid")
    streams = [item.strip() for item in args.streams.split(",") if item.strip()]
    if not streams or len(streams) != len(set(streams)) or not set(streams) <= ALLOWED_STREAMS:
        raise MonitorError("streams must be a unique comma-separated supported set")
    _require_absolute(args.backup_root, "backup root")
    _require_absolute(args.state_file, "state file")
    if any(stream.startswith("idp-") for stream in streams):
        if args.idp_env is None:
            raise MonitorError("IDP streams require --idp-env")
        _require_absolute(args.idp_env, "IDP environment file")
    values = (
        args.rpo_seconds,
        args.replication_delay_seconds,
        args.pat_warning_seconds,
        args.pat_critical_seconds,
        args.clock_skew_seconds,
    )
    if any(value < 1 for value in values):
        raise MonitorError("all time thresholds must be positive integers")
    if args.pat_critical_seconds >= args.pat_warning_seconds:
        raise MonitorError("PAT critical threshold must be smaller than warning threshold")
    return streams


def _check_unlocked(
    args: argparse.Namespace, *, runner: Runner = _run, now: datetime | None = None
) -> list[Result]:
    streams = _validate_args(args)
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise MonitorError("monitor clock must be timezone-aware")
    current_time = current_time.astimezone(UTC)
    results: list[Result] = []
    previous = load_state(args.state_file)
    next_state = dict(previous)
    containers: dict[str, Container] = {}

    for stream in streams:
        service = STREAM_SERVICE[stream]
        try:
            container = inspect_container(args.project, service, runner)
        except MonitorError as exc:
            results.append(Result("unknown", f"container.{service}", str(exc)))
            previous_record = previous.get(service)
            if isinstance(previous_record, dict):
                pending_result = _pending_restart_result(
                    service, previous_record.get("pending_restart")
                )
                if pending_result is not None:
                    results.append(pending_result)
            continue
        containers[stream] = container
        container_results, next_record = _container_results(container, previous.get(service))
        results.extend(container_results)
        next_state[service] = next_record

    maximum_age = args.rpo_seconds + args.replication_delay_seconds
    manifest_cache: dict[str, Manifest] = {}
    for kind in ("health", "idp"):
        relevant = [stream for stream in streams if stream.startswith(f"{kind}-")]
        if not relevant:
            continue
        try:
            manifest = discover_latest_manifest(args.backup_root, kind)
        except FileNotFoundError as exc:
            results.append(Result("critical", f"freshness.{kind}-local", str(exc)))
            continue
        except RecoveryProblem as exc:
            results.append(Result("critical", f"freshness.{kind}-local", str(exc)))
            continue
        except MonitorError as exc:
            results.append(Result("unknown", f"freshness.{kind}-local", str(exc)))
            continue
        manifest_cache[kind] = manifest
        if f"{kind}-local" in streams:
            results.append(
                _freshness_result(
                    key=f"freshness.{kind}-local",
                    label=f"{kind} local recovery point",
                    timestamp=manifest.timestamp,
                    now=current_time,
                    maximum_age=maximum_age,
                    clock_skew=args.clock_skew_seconds,
                )
            )

    for kind in ("health", "idp"):
        stream = f"{kind}-offsite"
        if stream not in streams or kind not in manifest_cache:
            continue
        container = containers.get(stream)
        if container is None or container.status != "running" or container.restarting:
            results.append(
                Result(
                    "critical",
                    f"freshness.{kind}-offsite",
                    "offsite marker is unavailable because its container is not running",
                )
            )
            continue
        try:
            marker_name = read_marker(container.container_id, runner)
        except MonitorError as exc:
            results.append(Result("critical", f"freshness.{kind}-offsite", str(exc)))
            continue
        results.extend(
            validate_marker(
                marker_name=marker_name,
                backup_root=args.backup_root,
                kind=kind,
                local=manifest_cache[kind],
                now=current_time,
                rpo_seconds=args.rpo_seconds,
                replication_delay_seconds=args.replication_delay_seconds,
                clock_skew_seconds=args.clock_skew_seconds,
            )
        )

    if any(stream.startswith("idp-") for stream in streams):
        assert args.idp_env is not None
        try:
            expiry = read_pat_expiration(args.idp_env)
            results.append(
                _pat_result(
                    expiry,
                    current_time,
                    args.pat_warning_seconds,
                    args.pat_critical_seconds,
                )
            )
        except MonitorError as exc:
            results.append(Result("unknown", "pat.expiry", str(exc)))

    write_state(args.state_file, next_state)
    return sorted(results, key=lambda result: result.key)


def check(
    args: argparse.Namespace, *, runner: Runner = _run, now: datetime | None = None
) -> list[Result]:
    _validate_args(args)
    with state_lock(args.state_file):
        return _check_unlocked(args, runner=runner, now=now)


def acknowledge(args: argparse.Namespace) -> str:
    _require_absolute(args.state_file, "state file")
    if args.service not in SERVICE_NAMES:
        raise MonitorError("acknowledgement service is invalid")
    if CONTAINER_ID_PATTERN.fullmatch(args.container_id) is None:
        raise MonitorError("acknowledgement container ID is invalid")
    if args.restart_count < 0:
        raise MonitorError("acknowledgement restart count is invalid")
    with state_lock(args.state_file):
        containers = load_state(args.state_file)
        record = containers.get(args.service)
        if not isinstance(record, dict):
            raise MonitorError("no restart baseline exists for that service")
        pending = record.get("pending_restart")
        if not isinstance(pending, dict):
            raise MonitorError("no restart activity is awaiting acknowledgement")
        expected = (args.container_id, args.restart_count)
        current = (record.get("container_id"), record.get("restart_count"))
        observed = (pending.get("container_id"), pending.get("restart_count"))
        if current != expected or observed != expected:
            raise MonitorError("acknowledgement does not match the latest observed container state")
        record["pending_restart"] = None
        write_state(args.state_file, containers)
    return (
        f"acknowledged restart activity for {args.service} "
        f"at container={args.container_id}, restart_count={args.restart_count}"
    )


def _render(results: list[Result], output: str) -> None:
    if output == "json":
        print(
            json.dumps(
                [
                    {"level": result.level, "check": result.key, "message": result.message}
                    for result in results
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    for result in results:
        print(f"{result.level.upper()} {result.key}: {result.message}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "acknowledge":
        try:
            message = acknowledge(args)
        except MonitorError as exc:
            print(f"UNKNOWN acknowledgement: {exc}", file=sys.stderr)
            return 2
        print(f"OK acknowledgement: {message}")
        return 0
    try:
        results = check(args)
    except MonitorError as exc:
        print(f"UNKNOWN monitor: {exc}", file=sys.stderr)
        return 2
    _render(results, args.output)
    if any(result.level == "unknown" for result in results):
        return 2
    if any(result.level in {"warning", "critical"} for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
