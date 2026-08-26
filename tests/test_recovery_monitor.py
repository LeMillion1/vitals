"""Executable contracts for the host-only recovery freshness monitor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from scripts import check_recovery_monitor as monitor


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
CONTAINER_IDS = {
    "vitals_backup": "a" * 64,
    "vitals_offsite_backup": "b" * 64,
    "vitals_idp_backup": "c" * 64,
    "vitals_idp_offsite_backup": "d" * 64,
}


def _manifest(root: Path, kind: str, when: datetime) -> Path:
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    if kind == "health":
        path = root / f"vitals_bundle_{stamp}.sha256"
    else:
        path = root / "idp" / f"zitadel_bundle_{stamp}.sha256"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic checksum manifest\n", encoding="utf-8")
    return path


def _idp_env(tmp_path: Path, expiry: datetime, *, extra: str = "") -> Path:
    path = tmp_path / ".env.idp"
    path.write_text(
        f"VITALS_IDP_ADMIN_PASSWORD_FILE={extra}\n"
        f"VITALS_IDP_LOGIN_PAT_EXPIRATION={expiry.strftime('%Y-%m-%dT%H:%M:%SZ')}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _args(
    tmp_path: Path,
    *,
    streams: str = "health-local,health-offsite,idp-local,idp-offsite",
    expiry: datetime | None = None,
):
    backup_root = tmp_path / "backups"
    backup_root.mkdir(exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    parser = monitor._build_parser()
    argv = [
        "check",
        "--project",
        "vitals_prod",
        "--backup-root",
        str(backup_root),
        "--streams",
        streams,
        "--state-file",
        str(state_dir / "state.json"),
    ]
    if "idp-" in streams:
        argv.extend(
            [
                "--idp-env",
                str(_idp_env(tmp_path, expiry or NOW + timedelta(days=90))),
            ]
        )
    return parser.parse_args(argv)


class FakeDocker:
    def __init__(self, markers: dict[str, str] | None = None):
        self.markers = markers or {}
        self.container_ids = dict(CONTAINER_IDS)
        self.states = {service: ["running", False, 0, None] for service in CONTAINER_IDS}
        self.calls: list[list[str]] = []

    def __call__(self, command):
        command = list(command)
        self.calls.append(command)
        if command[:3] == ["docker", "ps", "-a"]:
            service_filter = next(
                item for item in command if item.startswith("label=com.docker.compose.service=")
            )
            service = service_filter.rsplit("=", 1)[1]
            container_id = self.container_ids.get(service, "")
            return subprocess.CompletedProcess(command, 0, container_id + "\n", "")
        if command[:2] == ["docker", "inspect"]:
            container_id = command[-1]
            service = next(
                name for name, value in self.container_ids.items() if value == container_id
            )
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.states[service]) + "\n", ""
            )
        if command[:2] == ["docker", "exec"]:
            marker = self.markers.get(command[2])
            if marker is None:
                return subprocess.CompletedProcess(command, 1, "", "missing")
            return subprocess.CompletedProcess(command, 0, marker + "\n", "")
        raise AssertionError(command)


def test_fresh_required_streams_pass_and_publish_private_state(tmp_path):
    args = _args(tmp_path)
    health = _manifest(args.backup_root, "health", NOW - timedelta(hours=1))
    identity = _manifest(args.backup_root, "idp", NOW - timedelta(hours=1))
    docker = FakeDocker(
        {
            CONTAINER_IDS["vitals_offsite_backup"]: health.name,
            CONTAINER_IDS["vitals_idp_offsite_backup"]: identity.name,
        }
    )

    results = monitor.check(args, runner=docker, now=NOW)

    assert {result.level for result in results} == {"ok"}
    assert stat.S_IMODE(args.state_file.stat().st_mode) == 0o600
    assert json.loads(args.state_file.read_text())["format_version"] == 2
    assert all("compose" not in call for call in docker.calls)
    inspect_calls = [call for call in docker.calls if call[:2] == ["docker", "inspect"]]
    assert inspect_calls
    assert all(".Config" not in " ".join(call) for call in inspect_calls)


def test_manifest_timestamp_not_mtime_controls_freshness(tmp_path):
    args = _args(tmp_path, streams="health-local")
    old = _manifest(args.backup_root, "health", NOW - timedelta(days=2))
    os.utime(old, (NOW.timestamp(), NOW.timestamp()))

    results = monitor.check(args, runner=FakeDocker(), now=NOW)

    freshness = next(result for result in results if result.key == "freshness.health-local")
    assert freshness.level == "critical"
    assert "stale" in freshness.message


@pytest.mark.parametrize("damage", ["malformed", "empty", "symlink", "future"])
def test_malformed_or_impossible_local_manifest_does_not_look_fresh(tmp_path, damage):
    args = _args(tmp_path, streams="health-local")
    if damage == "malformed":
        (args.backup_root / "vitals_bundle_not-a-time.sha256").write_text("x")
    elif damage == "empty":
        _manifest(args.backup_root, "health", NOW).write_text("")
    elif damage == "symlink":
        target = tmp_path / "target"
        target.write_text("x")
        (args.backup_root / "vitals_bundle_20260827T120000Z.sha256").symlink_to(target)
    else:
        _manifest(args.backup_root, "health", NOW + timedelta(hours=1))

    results = monitor.check(args, runner=FakeDocker(), now=NOW)

    freshness = next(result for result in results if result.key == "freshness.health-local")
    assert freshness.level == "critical"


def test_old_marker_mtime_cannot_hide_staleness(tmp_path):
    args = _args(tmp_path, streams="health-local,health-offsite")
    old = _manifest(args.backup_root, "health", NOW - timedelta(days=2))
    os.utime(old, (NOW.timestamp(), NOW.timestamp()))
    docker = FakeDocker({CONTAINER_IDS["vitals_offsite_backup"]: old.name})

    results = monitor.check(args, runner=docker, now=NOW)

    assert next(r for r in results if r.key == "freshness.health-offsite").level == "critical"


def test_new_local_bundle_has_replication_grace_then_alerts(tmp_path):
    args = _args(tmp_path, streams="health-offsite")
    old = _manifest(args.backup_root, "health", NOW - timedelta(days=1))
    _manifest(args.backup_root, "health", NOW - timedelta(seconds=899))
    docker = FakeDocker({CONTAINER_IDS["vitals_offsite_backup"]: old.name})

    within = monitor.check(args, runner=docker, now=NOW)
    assert next(r for r in within if r.key == "replication.health").level == "ok"
    new = monitor.discover_latest_manifest(args.backup_root, "health")

    late = monitor.validate_marker(
        marker_name=old.name,
        backup_root=args.backup_root,
        kind="health",
        local=new,
        now=NOW + timedelta(seconds=2),
        rpo_seconds=args.rpo_seconds,
        replication_delay_seconds=args.replication_delay_seconds,
        clock_skew_seconds=args.clock_skew_seconds,
    )
    assert next(r for r in late if r.key == "replication.health").level == "critical"


@pytest.mark.parametrize("marker", ["../escape", "bad", "vitals_bundle_20261301T000000Z.sha256"])
def test_bad_offsite_marker_fails_without_reading_payloads(tmp_path, marker):
    args = _args(tmp_path, streams="health-offsite")
    _manifest(args.backup_root, "health", NOW)
    docker = FakeDocker({CONTAINER_IDS["vitals_offsite_backup"]: marker})

    results = monitor.check(args, runner=docker, now=NOW)

    assert next(r for r in results if r.key == "freshness.health-offsite").level == "critical"


def test_container_restart_alert_is_sticky_until_exact_acknowledgement(tmp_path):
    args = _args(tmp_path, streams="health-local")
    _manifest(args.backup_root, "health", NOW)
    docker = FakeDocker()
    monitor.check(args, runner=docker, now=NOW)

    docker.states["vitals_backup"] = ["running", False, 1, None]
    restarted = monitor.check(args, runner=docker, now=NOW)
    assert next(r for r in restarted if r.key == "restart.vitals_backup").level == "warning"

    stable = monitor.check(args, runner=docker, now=NOW)
    sticky = next(r for r in stable if r.key == "restart.vitals_backup")
    assert sticky.level == "warning"
    assert "unacknowledged" in sticky.message
    assert "acknowledge --state-file" in sticky.message

    ack_args = monitor._build_parser().parse_args(
        [
            "acknowledge",
            "--state-file",
            str(args.state_file),
            "--service",
            "vitals_backup",
            "--container-id",
            docker.container_ids["vitals_backup"],
            "--restart-count",
            "1",
        ]
    )
    assert "acknowledged" in monitor.acknowledge(ack_args)
    acknowledged = monitor.check(args, runner=docker, now=NOW)
    assert next(r for r in acknowledged if r.key == "restart.vitals_backup").level == "ok"

    docker.states["vitals_backup"] = ["exited", False, 1, None]
    stopped = monitor.check(args, runner=docker, now=NOW)
    assert next(r for r in stopped if r.key == "container.vitals_backup").level == "critical"


def test_container_recreation_after_baseline_warns_and_remains_sticky(tmp_path):
    args = _args(tmp_path, streams="health-local")
    _manifest(args.backup_root, "health", NOW)
    docker = FakeDocker()
    first = monitor.check(args, runner=docker, now=NOW)
    assert next(r for r in first if r.key == "restart.vitals_backup").level == "ok"

    docker.container_ids["vitals_backup"] = "e" * 64
    replaced = monitor.check(args, runner=docker, now=NOW)
    replacement = next(r for r in replaced if r.key == "restart.vitals_backup")
    assert replacement.level == "warning"
    assert "container ID changed" in replacement.message

    still_pending = monitor.check(args, runner=docker, now=NOW)
    assert next(r for r in still_pending if r.key == "restart.vitals_backup").level == "warning"


def test_first_observation_preserves_baseline_behavior(tmp_path):
    args = _args(tmp_path, streams="health-local")
    _manifest(args.backup_root, "health", NOW)
    docker = FakeDocker()
    docker.states["vitals_backup"] = ["running", False, 3, None]

    first = monitor.check(args, runner=docker, now=NOW)
    warning = next(r for r in first if r.key == "restart.vitals_backup")
    assert warning.level == "warning"
    assert "newly observed" in warning.message

    repeated = monitor.check(args, runner=docker, now=NOW)
    assert next(r for r in repeated if r.key == "restart.vitals_backup").level == "warning"


def test_acknowledgement_is_compare_and_swap_and_cli_clears_exact_event(tmp_path, capsys):
    args = _args(tmp_path, streams="health-local")
    _manifest(args.backup_root, "health", NOW)
    docker = FakeDocker()
    monitor.check(args, runner=docker, now=NOW)
    docker.states["vitals_backup"] = ["running", False, 2, None]
    monitor.check(args, runner=docker, now=NOW)

    common = [
        "acknowledge",
        "--state-file",
        str(args.state_file),
        "--service",
        "vitals_backup",
        "--container-id",
        docker.container_ids["vitals_backup"],
        "--restart-count",
    ]
    assert monitor.main([*common, "1"]) == 2
    state = json.loads(args.state_file.read_text())
    assert state["containers"]["vitals_backup"]["pending_restart"] is not None

    assert monitor.main([*common, "2"]) == 0
    output = capsys.readouterr()
    assert "acknowledged restart activity for vitals_backup" in output.out
    assert "secret" not in (output.out + output.err).lower()
    state = json.loads(args.state_file.read_text())
    assert state["containers"]["vitals_backup"]["pending_restart"] is None


def test_missing_or_ambiguous_container_is_unknown(tmp_path):
    args = _args(tmp_path, streams="health-local")
    _manifest(args.backup_root, "health", NOW)

    def no_container(command):
        if command[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    results = monitor.check(args, runner=no_container, now=NOW)
    assert next(r for r in results if r.key == "container.vitals_backup").level == "unknown"


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (timedelta(days=31), "ok"),
        (timedelta(days=30), "warning"),
        (timedelta(days=7), "critical"),
        (timedelta(seconds=-1), "critical"),
    ],
)
def test_pat_expiry_thresholds_and_secret_free_output(tmp_path, remaining, expected):
    args = _args(tmp_path, streams="idp-local", expiry=NOW + remaining)
    _manifest(args.backup_root, "idp", NOW)
    secret = "SYNTHETIC-SECRET-MUST-NOT-LEAK"
    args.idp_env.write_text(
        f"VITALS_IDP_ADMIN_PASSWORD_FILE={secret}\n"
        f"VITALS_IDP_LOGIN_PAT_EXPIRATION={(NOW + remaining).strftime('%Y-%m-%dT%H:%M:%SZ')}\n",
        encoding="utf-8",
    )

    results = monitor.check(args, runner=FakeDocker(), now=NOW)

    pat = next(result for result in results if result.key == "pat.expiry")
    assert pat.level == expected
    assert secret not in " ".join(result.message for result in results)


@pytest.mark.parametrize("damage", ["duplicate", "quoted", "permissive", "symlink"])
def test_invalid_idp_expiry_source_is_unknown(tmp_path, damage):
    args = _args(tmp_path, streams="idp-local")
    _manifest(args.backup_root, "idp", NOW)
    if damage == "duplicate":
        args.idp_env.write_text("VITALS_IDP_LOGIN_PAT_EXPIRATION=2027-01-01T00:00:00Z\n" * 2)
    elif damage == "quoted":
        args.idp_env.write_text('VITALS_IDP_LOGIN_PAT_EXPIRATION="2027-01-01T00:00:00Z"\n')
    elif damage == "permissive":
        args.idp_env.chmod(0o644)
    else:
        target = tmp_path / "replacement"
        target.write_text("VITALS_IDP_LOGIN_PAT_EXPIRATION=2027-01-01T00:00:00Z\n")
        target.chmod(0o600)
        args.idp_env.unlink()
        args.idp_env.symlink_to(target)

    results = monitor.check(args, runner=FakeDocker(), now=NOW)
    assert next(r for r in results if r.key == "pat.expiry").level == "unknown"


def test_optional_streams_are_not_queried(tmp_path):
    args = _args(tmp_path, streams="health-local")
    _manifest(args.backup_root, "health", NOW)
    docker = FakeDocker()

    monitor.check(args, runner=docker, now=NOW)

    selected = " ".join(" ".join(call) for call in docker.calls)
    assert "vitals_backup" in selected
    assert "vitals_offsite_backup" not in selected
    assert "vitals_idp" not in selected


def test_systemd_environment_can_supply_idp_path(tmp_path, monkeypatch):
    idp_env = _idp_env(tmp_path, NOW + timedelta(days=90))
    monkeypatch.setenv("VITALS_MONITOR_IDP_ENV", str(idp_env))
    parser = monitor._build_parser()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    args = parser.parse_args(
        [
            "check",
            "--project",
            "vitals_prod",
            "--backup-root",
            str(tmp_path / "backups"),
            "--streams",
            "idp-local",
            "--state-file",
            str(state_dir / "state.json"),
        ]
    )

    assert args.idp_env == idp_env


def test_symlinked_state_is_refused_without_replacement(tmp_path):
    args = _args(tmp_path, streams="health-local")
    _manifest(args.backup_root, "health", NOW)
    target = tmp_path / "operator-file"
    target.write_text("preserve me")
    args.state_file.symlink_to(target)

    with pytest.raises(monitor.MonitorError, match="symlink"):
        monitor.check(args, runner=FakeDocker(), now=NOW)

    assert target.read_text() == "preserve me"


def test_cli_exit_codes_distinguish_alert_from_monitor_failure(tmp_path, monkeypatch):
    args = _args(tmp_path, streams="health-local")
    old = _manifest(args.backup_root, "health", NOW - timedelta(days=2))
    del old
    monkeypatch.setattr(monitor, "_run", FakeDocker())
    monkeypatch.setattr(monitor, "check", lambda parsed: [monitor.Result("critical", "x", "stale")])
    assert (
        monitor.main(
            [
                "check",
                "--project",
                "vitals_prod",
                "--backup-root",
                str(args.backup_root),
                "--streams",
                "health-local",
                "--state-file",
                str(args.state_file),
            ]
        )
        == 1
    )

    monkeypatch.setattr(
        monitor,
        "check",
        lambda parsed: (_ for _ in ()).throw(monitor.MonitorError("docker unavailable")),
    )
    assert (
        monitor.main(
            [
                "check",
                "--project",
                "vitals_prod",
                "--backup-root",
                str(args.backup_root),
                "--streams",
                "health-local",
                "--state-file",
                str(args.state_file),
            ]
        )
        == 2
    )


def test_systemd_units_are_persistent_hardened_and_secret_free():
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/vitals-recovery-monitor.service").read_text()
    timer = (root / "deploy/systemd/vitals-recovery-monitor.timer").read_text()
    example = (root / "deploy/systemd/recovery-monitor.conf.example").read_text()

    assert "Type=oneshot" in service
    assert "EnvironmentFile=/etc/vitals/recovery-monitor.conf" in service
    assert "StateDirectory=vitals-recovery-monitor" in service
    assert "TimeoutStartSec=90" in service
    assert monitor.COMMAND_TIMEOUT_SECONDS == 5
    assert "check --project" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "RestrictAddressFamilies=AF_UNIX" in service
    assert "docker compose" not in service
    assert ".env.idp" not in service
    assert "Telegram" not in service and "webhook" not in service.lower()
    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert "VITALS_MONITOR_CHECKOUT=" in example
    assert "VITALS_MONITOR_PROJECT=vitals_prod" in example
    assert "VITALS_MONITOR_BACKUP_ROOT=" in example
    assert "VITALS_MONITOR_STREAMS=health-local,idp-local" in example
    assert "VITALS_MONITOR_IDP_ENV=" in example
    assert "PASSWORD=" not in example and "TOKEN=" not in example


def test_script_is_executable_and_compiles():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/check_recovery_monitor.py"
    assert os.access(script, os.X_OK)
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
