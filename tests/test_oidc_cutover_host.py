"""Host OIDC cutover orchestration without a real Docker daemon or provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from scripts.oidc_cutover_host import (
    APP_SERVICE,
    CUTOVER_CONFIRMATION,
    FINALIZE_CONFIRMATION,
    OIDC_BOOTSTRAP_KEY,
    OIDC_RUNTIME_KEYS,
    REQUIRED_WEB_ENV,
    RETIRE_CONFIRMATION,
    ROLLBACK_CONFIRMATION,
    RUNTIME_FILE,
    RUNTIME_TARGET,
    Attestation,
    CoordinatorError,
    OidcCutoverHost,
    ProbeResponse,
    ProcessResult,
    ProviderArguments,
)


ISSUER = "https://auth.example.test"
IMAGE = "vitals_prod_runtime:test"
IMAGE_ID = "sha256:synthetic-image"
NETWORK = "vitals_prod_default"
NETWORK_ID = "synthetic-network-id"


class FakeProduction:
    def __init__(self, runtime_parent: Path) -> None:
        self.runtime_parent = runtime_parent.resolve()
        self.runtime_env = self.runtime_parent / "vitals.env"
        self.running = True
        self.auth_state = "password"
        self.commands: list[tuple[str, ...]] = []
        self.fail_oidc_postflight = False
        self.container_image_id = IMAGE_ID
        self.expected_image_id = IMAGE_ID
        self.image_after_up: str | None = None
        self.config_marker: str | None = None
        self.direct_auth_key: str | None = None
        self.service_env_file = False
        self.container_env_key: str | None = None
        self.image_env_key: str | None = None
        self.container_control_overrides: dict[str, str | None] = {}
        self.extra_network = False
        self.staged_secret_sets: list[frozenset[str]] = []
        self.staged_secret_modes: list[tuple[int, frozenset[int]]] = []

    def update_runtime(self, updates: dict[str, str]) -> None:
        lines = self.runtime_env.read_text(encoding="utf-8").splitlines()
        remaining = dict(updates)
        rendered: list[str] = []
        for line in lines:
            key, separator, _value = line.partition("=")
            if separator and key in remaining:
                rendered.append(f"{key}={remaining.pop(key)}")
            else:
                rendered.append(line)
        assert not remaining
        self.runtime_env.write_text("\n".join(rendered) + "\n", encoding="utf-8")
        self.runtime_env.chmod(0o600)

    @property
    def config(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "services": {
                APP_SERVICE: {
                    "image": IMAGE,
                    "environment": dict(REQUIRED_WEB_ENV),
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(self.runtime_parent),
                            "target": RUNTIME_TARGET,
                            "read_only": False,
                        },
                        {
                            "type": "bind",
                            "source": "/synthetic/health-uploads",
                            "target": "/app/web/static/uploads",
                            "read_only": False,
                        },
                    ],
                }
            }
        }
        if self.config_marker is not None:
            payload["services"][APP_SERVICE]["environment"]["SYNTHETIC_MARKER"] = self.config_marker
        if self.direct_auth_key is not None:
            payload["services"][APP_SERVICE]["environment"][self.direct_auth_key] = "shadowed"
        if self.service_env_file:
            payload["services"][APP_SERVICE]["env_file"] = ["/tmp/unsafe.env"]
        return payload

    @property
    def container_environment(self) -> list[str]:
        values = dict(REQUIRED_WEB_ENV)
        for key, value in self.container_control_overrides.items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        if self.container_env_key is not None:
            values[self.container_env_key] = "shadowed"
        return [f"{key}={value}" for key, value in values.items()]

    @property
    def inspection(self) -> list[dict[str, object]]:
        networks = {NETWORK: {"NetworkID": NETWORK_ID}}
        if self.extra_network:
            networks["unexpected"] = {"NetworkID": "other-network"}
        return [
            {
                "Config": {
                    "Image": IMAGE,
                    "Env": self.container_environment,
                    "Labels": {
                        "com.docker.compose.project": "vitals_prod",
                        "com.docker.compose.service": APP_SERVICE,
                    },
                },
                "Image": self.container_image_id,
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": str(self.runtime_parent),
                        "Destination": RUNTIME_TARGET,
                        "RW": True,
                    },
                    {
                        "Type": "bind",
                        "Source": "/synthetic/health-uploads",
                        "Destination": "/app/web/static/uploads",
                        "RW": True,
                    },
                ],
                "NetworkSettings": {"Networks": networks},
                "State": {"Running": self.running},
            }
        ]

    def run(self, command) -> ProcessResult:
        command = tuple(command)
        self.commands.append(command)
        if command[:3] == ("docker", "ps", "-a"):
            return ProcessResult(0, "container-id\n")
        if command[:2] == ("docker", "inspect"):
            return ProcessResult(0, json.dumps(self.inspection))
        if command[:3] == ("docker", "image", "inspect"):
            return ProcessResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": self.expected_image_id,
                            "Config": {
                                "Env": (
                                    [f"{self.image_env_key}=shadowed"]
                                    if self.image_env_key is not None
                                    else []
                                )
                            },
                        }
                    ]
                ),
            )
        if command[:3] == ("docker", "network", "inspect"):
            return ProcessResult(
                0,
                json.dumps(
                    [
                        {
                            "Id": NETWORK_ID,
                            "Labels": {
                                "com.docker.compose.project": "vitals_prod",
                                "com.docker.compose.network": "default",
                            },
                        }
                    ]
                ),
            )
        if "config" in command and "--format" in command:
            return ProcessResult(0, json.dumps(self.config))
        if "stop" in command:
            self.running = False
            return ProcessResult(0)
        if "up" in command:
            if self.image_after_up is not None:
                self.container_image_id = self.image_after_up
                self.expected_image_id = self.image_after_up
            self.running = True
            return ProcessResult(0)
        if "port" in command:
            return ProcessResult(0, "127.0.0.1:8000\n")
        if "run" in command and "scripts/oidc_cutover.py" in command:
            mounts = [
                command[index + 1] for index, value in enumerate(command) if value == "--mount"
            ]
            secret_mounts = [mount for mount in mounts if "dst=/run/vitals-oidc-secret" in mount]
            if secret_mounts:
                staged_parent = Path(secret_mounts[0].split("src=", 1)[1].split(",dst=", 1)[0])
                self.staged_secret_sets.append(
                    frozenset(path.name for path in staged_parent.iterdir())
                )
                self.staged_secret_modes.append(
                    (
                        stat_mode(staged_parent),
                        frozenset(stat_mode(path) for path in staged_parent.iterdir()),
                    )
                )
            helper_index = command.index("scripts/oidc_cutover.py")
            operation = command[helper_index + 3]
            if operation == "status":
                return ProcessResult(
                    0,
                    json.dumps(
                        {
                            "result": "ok",
                            "readback": self.auth_state,
                        }
                    )
                    + "\n",
                )
            if operation == "preflight":
                return ProcessResult(0, '{"result":"ok"}\n')
            if operation == "password-preflight":
                return ProcessResult(0, '{"result":"ok"}\n')
            if operation == "retire-preflight":
                return ProcessResult(0, '{"result":"ok"}\n')
            if operation == "enable":
                self.auth_state = "oidc_bootstrap_pending"
                self.update_runtime(
                    {
                        "VITALS_OIDC_ISSUER": ISSUER,
                        "VITALS_OIDC_CLIENT_ID": "vitals-web",
                        "VITALS_OIDC_CLIENT_SECRET": "synthetic-client-secret",
                        "VITALS_OIDC_REDIRECT_URL": ("https://vitals.example.test/auth/callback"),
                        "VITALS_OIDC_BOOTSTRAP_SUBJECT": "owner-subject",
                        "VITALS_SESSION_SECRET": "cutover-session-secret",
                    }
                )
                return ProcessResult(0, '{"result":"ok"}\n')
            if operation == "finalize":
                self.auth_state = "oidc_bound"
                self.update_runtime({"VITALS_OIDC_BOOTSTRAP_SUBJECT": ""})
                return ProcessResult(0, '{"result":"ok"}\n')
            if operation == "rollback":
                self.auth_state = "password"
                self.update_runtime(
                    {
                        **{key: "" for key in (*OIDC_RUNTIME_KEYS, OIDC_BOOTSTRAP_KEY)},
                        "VITALS_SESSION_SECRET": "rollback-session-secret",
                    }
                )
                return ProcessResult(0, '{"result":"ok"}\n')
            if operation == "retire-legacy":
                self.update_runtime(
                    {
                        "VITALS_AUTH_USERNAME": "",
                        "VITALS_AUTH_PASSWORD_HASH": "",
                        "VITALS_SESSION_SECRET": "retired-session-secret",
                    }
                )
                return ProcessResult(0, '{"result":"ok"}\n')
        raise AssertionError(f"unexpected command boundary: {command}")

    def probe(self, url: str) -> ProbeResponse:
        path = urlsplit(url).path
        if path == "/health":
            return ProbeResponse(200, {})
        if path == "/today":
            return ProbeResponse(303, {"Location": "/login?next=%2Ftoday"})
        if self.auth_state == "password":
            if path == "/login":
                return ProbeResponse(200, {})
            if path == "/auth/start":
                return ProbeResponse(404, {})
        if path == "/login":
            return ProbeResponse(303, {"Location": "/auth/start"})
        if path == "/auth/start":
            if self.fail_oidc_postflight:
                return ProbeResponse(502, {})
            return ProbeResponse(
                303,
                {"Location": f"{ISSUER}/oauth/v2/authorize?client_id=vitals"},
            )
        raise AssertionError(f"unexpected HTTP probe: {url}")


@pytest.fixture
def installation(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    runtime_parent = tmp_path / ".vitals-runtime"
    runtime_parent.mkdir(mode=0o700)
    runtime_parent.chmod(0o700)
    runtime = runtime_parent / "vitals.env"
    runtime.write_text(
        "\n".join(
            [
                "VITALS_AUTH_USERNAME=owner",
                "VITALS_AUTH_PASSWORD_HASH=synthetic-hash",
                "VITALS_OIDC_ISSUER=",
                "VITALS_OIDC_CLIENT_ID=",
                "VITALS_OIDC_CLIENT_SECRET=",
                "VITALS_OIDC_REDIRECT_URL=",
                "VITALS_OIDC_BOOTSTRAP_SUBJECT=",
                "VITALS_SESSION_SECRET=synthetic-session-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runtime.chmod(0o600)
    secret_parent = tmp_path / ".oidc-secret"
    secret_parent.mkdir(mode=0o700)
    secret_parent.chmod(0o700)
    secret = secret_parent / "client-secret"
    secret.write_text("never-print-this-secret\n", encoding="utf-8")
    secret.chmod(0o600)
    legacy_password = secret_parent / "legacy-password"
    legacy_password.write_text("synthetic-legacy-password\n", encoding="utf-8")
    legacy_password.chmod(0o600)
    unselected = secret_parent / "unselected-secret"
    unselected.write_text("must-not-be-staged\n", encoding="utf-8")
    unselected.chmod(0o600)
    fake = FakeProduction(runtime_parent)
    coordinator = OidcCutoverHost(
        project="vitals_prod",
        compose_files=[compose],
        env_files=[],
        runtime_env=runtime,
        state_file=tmp_path / ".cutover-state",
        runner=fake.run,
        http_probe=fake.probe,
    )
    provider = ProviderArguments(
        issuer=ISSUER,
        client_id="vitals-web",
        client_secret_file=secret,
        legacy_password_file=legacy_password,
        redirect_url="https://vitals.example.test/auth/callback",
        bootstrap_subject="owner-subject",
    )
    return coordinator, fake, provider


def _command_with(commands, token: str) -> tuple[str, ...]:
    return next(command for command in commands if token in command)


def test_attestation_requires_exact_running_image_and_runtime_mount(installation):
    coordinator, fake, _provider = installation
    attestation = coordinator.attest(require_running=True)

    assert attestation == Attestation(
        container_id="container-id",
        image=IMAGE,
        image_id=IMAGE_ID,
        config_id=coordinator._service_config_id(fake.config["services"][APP_SERVICE]),
        network=NETWORK,
        network_id=NETWORK_ID,
        running=True,
    )

    fake.container_image_id = "sha256:stale"
    with pytest.raises(CoordinatorError, match="rendered image ID"):
        coordinator.attest()


def test_attestation_refuses_more_than_one_container_network(installation):
    coordinator, fake, _provider = installation
    fake.extra_network = True

    with pytest.raises(CoordinatorError, match="exactly one Compose network"):
        coordinator.attest()


def test_preflight_stages_only_read_only_secret_parent_and_nonsecret_journal(
    installation,
):
    coordinator, fake, provider = installation

    assert coordinator.preflight(provider)["result"] == "ok"

    helper = _command_with(fake.commands, "scripts/oidc_cutover.py")
    assert RUNTIME_FILE in helper
    assert helper[:2] == ("docker", "run")
    assert helper[helper.index("--pull") + 1] == "never"
    assert helper[helper.index("--network") + 1] == NETWORK_ID
    assert helper[helper.index("--user") + 1] == f"{os.geteuid()}:{os.getegid()}"
    assert helper[helper.index("--entrypoint") + 1] == "python"
    assert helper[helper.index("--workdir") + 1] == "/app"
    assert IMAGE_ID in helper
    mounts = [helper[index + 1] for index, value in enumerate(helper) if value == "--mount"]
    assert len(mounts) == 2
    secret_mount = next(mount for mount in mounts if "dst=/run/vitals-oidc-secret" in mount)
    assert str(provider.client_secret_file.parent.resolve()) not in secret_mount
    assert secret_mount.endswith(",readonly")
    staged_parent = Path(secret_mount.split("src=", 1)[1].split(",dst=", 1)[0])
    assert not staged_parent.exists()
    runtime_mount = next(mount for mount in mounts if f"dst={RUNTIME_TARGET}" in mount)
    assert "readonly" not in runtime_mount
    assert "/synthetic/health-uploads" not in " ".join(helper)
    assert "never-print-this-secret" not in " ".join(helper)
    assert "synthetic-legacy-password" not in " ".join(helper)
    assert fake.staged_secret_sets == [frozenset({"client-secret", "legacy-password"})]
    assert fake.staged_secret_modes == [(0o700, frozenset({0o600}))]
    assert "unselected-secret" not in fake.staged_secret_sets[0]
    journal = coordinator.state_file.read_text(encoding="utf-8")
    assert stat_mode(coordinator.state_file) == 0o600
    assert "never-print-this-secret" not in journal
    assert "synthetic-legacy-password" not in journal
    assert str(provider.client_secret_file) not in journal
    assert str(provider.legacy_password_file) not in journal
    assert provider.client_id not in journal
    assert provider.bootstrap_subject not in journal
    assert json.loads(journal)["phase"] == "preflight_passed"


@pytest.mark.parametrize(
    "injection",
    [
        ("environment", "VITALS_DATABASE_URL"),
        ("environment", "VITALS_REGISTRATION_UNLOCKED"),
        ("container", "VITALS_SESSION_SECRET"),
        ("image", "VITALS_OIDC_CLIENT_SECRET"),
        ("env_file", None),
    ],
)
def test_attestation_refuses_compose_auth_authority_injection(installation, injection):
    coordinator, fake, _provider = installation
    location, key = injection
    if location == "environment":
        fake.direct_auth_key = key
    elif location == "container":
        fake.container_env_key = key
    elif location == "image":
        fake.image_env_key = key
    else:
        fake.service_env_file = True

    with pytest.raises(CoordinatorError, match="inject|env_file|shadows"):
        coordinator.attest()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("VITALS_ENV_FILE", "/app/alternate.env"),
        ("VITALS_RUNTIME_ENV_ISOLATION_REQUIRED", "false"),
        ("VITALS_RUNTIME_ENV_ISOLATION_REQUIRED", None),
        ("VITALS_PROCESS_MODE", "worker"),
    ],
)
def test_attestation_refuses_actual_container_control_drift(installation, key, value):
    coordinator, fake, _provider = installation
    fake.container_control_overrides[key] = value

    with pytest.raises(CoordinatorError, match="runtime control"):
        coordinator.attest()


def test_attestation_refuses_image_baked_runtime_control(installation):
    coordinator, fake, _provider = installation
    fake.image_env_key = "VITALS_ENV_FILE"

    with pytest.raises(CoordinatorError, match="bakes runtime control"):
        coordinator.attest()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_successful_cutover_stops_mutates_and_recreates_only_app(installation):
    coordinator, fake, provider = installation

    payload = coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)

    assert payload["result"] == "ok"
    assert fake.running is True
    assert fake.auth_state == "oidc_bootstrap_pending"
    assert json.loads(coordinator.state_file.read_text())["phase"] == ("awaiting_owner_binding")
    stop = _command_with(fake.commands, "stop")
    up = _command_with(fake.commands, "up")
    assert stop[-1] == APP_SERVICE
    assert up[-1] == APP_SERVICE
    assert "--no-deps" in up
    assert "--no-build" in up
    assert "--force-recreate" in up
    assert up[up.index("--pull") + 1] == "never"
    assert all("vitals_worker" not in command for command in fake.commands)


def test_failed_oidc_postflight_automatically_restores_password_mode(installation):
    coordinator, fake, provider = installation
    fake.fail_oidc_postflight = True

    with pytest.raises(CoordinatorError, match="password mode was restored"):
        coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)

    assert fake.running is True
    assert fake.auth_state == "password"
    journal = json.loads(coordinator.state_file.read_text())
    assert journal["phase"] == "rolled_back"
    helper_commands = [command for command in fake.commands if "scripts/oidc_cutover.py" in command]
    assert any("enable" in command for command in helper_commands)
    assert any("rollback" in command for command in helper_commands)


def test_cutover_compensates_if_anonymous_today_is_exposed(installation):
    coordinator, fake, provider = installation

    def fail_open_probe(url: str) -> ProbeResponse:
        if urlsplit(url).path == "/today" and fake.auth_state != "password":
            return ProbeResponse(200, {})
        return fake.probe(url)

    coordinator.http_probe = fail_open_probe

    with pytest.raises(CoordinatorError, match="password mode was restored"):
        coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)

    assert fake.auth_state == "password"
    assert fake.running is True


def test_compensation_rolls_back_even_if_failure_journal_cannot_be_written(
    installation,
):
    coordinator, fake, provider = installation
    fake.fail_oidc_postflight = True
    original_write = coordinator._write_journal

    def fail_first_failure_journal(**kwargs):
        if kwargs["phase"] == "cutover_postflight_failed":
            raise OSError("synthetic full disk")
        return original_write(**kwargs)

    coordinator._write_journal = fail_first_failure_journal

    with pytest.raises(CoordinatorError, match="password mode was restored"):
        coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)

    assert fake.auth_state == "password"
    assert fake.running is True
    assert any(
        "rollback" in command for command in fake.commands if "scripts/oidc_cutover.py" in command
    )


def test_postmutation_config_journal_failure_also_triggers_compensation(
    installation,
):
    coordinator, fake, provider = installation
    original_write = coordinator._write_journal

    def fail_config_written(**kwargs):
        if kwargs["phase"] == "cutover_config_written":
            raise OSError("synthetic fsync failure")
        return original_write(**kwargs)

    coordinator._write_journal = fail_config_written

    with pytest.raises(CoordinatorError, match="password mode was restored"):
        coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)

    assert fake.auth_state == "password"
    assert fake.running is True


def test_failed_enable_restarts_the_unchanged_password_application(installation):
    coordinator, fake, provider = installation
    original_run = fake.run

    def fail_enable(command):
        if "scripts/oidc_cutover.py" in command and "enable" in command:
            return ProcessResult(2, stderr="synthetic secret-bearing error")
        return original_run(command)

    coordinator.runner = fail_enable

    with pytest.raises(CoordinatorError, match="password mode was restored"):
        coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)

    assert fake.running is True
    assert fake.auth_state == "password"
    assert json.loads(coordinator.state_file.read_text())["phase"] == "rolled_back"


def test_mutating_helper_reattests_that_the_app_remains_stopped(installation):
    coordinator, fake, provider = installation
    stopped = coordinator._stop_app()
    fake.running = True

    with pytest.raises(CoordinatorError, match="not stopped"):
        coordinator._run_helper(
            [
                "rollback",
                "--legacy-password-file",
                str(provider.legacy_password_file),
                "--confirm",
                "synthetic-confirmation",
            ],
            authority=stopped,
            secret_files={"legacy-password": provider.legacy_password_file},
        )

    assert not any(
        command[:2] == ("docker", "run") and "rollback" in command for command in fake.commands
    )


def test_finalize_proves_oidc_then_recreates_bound_runtime(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    # The browser callback has durably linked the exact owner.
    fake.auth_state = "oidc_bootstrap_pending"

    not_before = json.loads(coordinator.state_file.read_text())["updated_at"]
    payload = coordinator.finalize(
        issuer=ISSUER,
        confirmation=FINALIZE_CONFIRMATION,
    )

    assert payload["result"] == "ok"
    assert fake.auth_state == "oidc_bound"
    assert fake.running is True
    assert json.loads(coordinator.state_file.read_text())["phase"] == "oidc_bound"
    finalize_helper = next(
        command
        for command in fake.commands
        if "scripts/oidc_cutover.py" in command and "finalize" in command
    )
    assert finalize_helper[finalize_helper.index("--not-before") + 1] == not_before


def test_finalize_refuses_runtime_oidc_authority_drift(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    fake.update_runtime({"VITALS_OIDC_BOOTSTRAP_SUBJECT": "replacement-subject"})

    with pytest.raises(CoordinatorError, match="OIDC authority changed"):
        coordinator.finalize(issuer=ISSUER, confirmation=FINALIZE_CONFIRMATION)

    assert fake.running is True


def test_normal_rollback_restores_password_and_rotates_sessions(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)

    payload = coordinator.rollback(
        issuer=ISSUER,
        legacy_password_file=provider.legacy_password_file,
        confirmation=ROLLBACK_CONFIRMATION,
    )

    assert payload["session_secret_rotated"] is True
    assert fake.auth_state == "password"
    assert fake.running is True
    assert json.loads(coordinator.state_file.read_text())["phase"] == "rolled_back"
    assert fake.staged_secret_sets[-1] == frozenset({"legacy-password"})
    rollback_helper = next(
        command
        for command in reversed(fake.commands)
        if "scripts/oidc_cutover.py" in command and "rollback" in command
    )
    assert str(provider.legacy_password_file) not in rollback_helper
    assert f"{RUNTIME_TARGET.rsplit('/', 1)[0]}/vitals-oidc-secret/legacy-password" in (
        rollback_helper
    )


def test_recover_recreates_a_stopped_oidc_runtime(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    fake.running = False

    payload = coordinator.recover(issuer=ISSUER)

    assert payload["auth_state"] == "oidc_bootstrap_pending"
    assert payload["journal_phase"] == "awaiting_owner_binding"
    assert fake.running is True


def test_recover_compensates_an_incomplete_cutover_that_fails_postflight(
    installation,
):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    coordinator._write_journal(
        phase="cutover_config_written",
        operation="cutover",
        attestation=coordinator.attest(require_running=True),
    )
    fake.fail_oidc_postflight = True

    with pytest.raises(CoordinatorError, match="password mode was restored"):
        coordinator.recover(
            issuer=ISSUER,
            legacy_password_file=provider.legacy_password_file,
        )

    assert fake.auth_state == "password"
    assert fake.running is True
    assert json.loads(coordinator.state_file.read_text())["phase"] == "rolled_back"


def test_recover_refuses_password_compensation_without_legacy_proof(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    coordinator._write_journal(
        phase="cutover_config_written",
        operation="cutover",
        attestation=coordinator.attest(require_running=True),
    )
    fake.fail_oidc_postflight = True

    with pytest.raises(CoordinatorError, match="--legacy-password-file"):
        coordinator.recover(issuer=ISSUER)

    assert fake.auth_state == "oidc_bootstrap_pending"


def test_recover_uses_staged_legacy_proof_for_pending_compensation(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    coordinator._write_journal(
        phase="cutover_postflight_failed",
        operation="cutover",
        attestation=coordinator.attest(require_running=True),
    )

    payload = coordinator.recover(
        issuer=ISSUER,
        legacy_password_file=provider.legacy_password_file,
    )

    assert payload["auth_state"] == "password"
    assert payload["journal_phase"] == "rolled_back"
    assert fake.staged_secret_sets[-1] == frozenset({"legacy-password"})


def test_recover_accepts_only_proven_password_after_compensation_crash(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    coordinator._write_journal(
        phase="cutover_postflight_failed",
        operation="cutover",
        attestation=coordinator.attest(require_running=True),
    )
    fake.auth_state = "password"
    fake.update_runtime(
        {
            **{key: "" for key in (*OIDC_RUNTIME_KEYS, OIDC_BOOTSTRAP_KEY)},
            "VITALS_SESSION_SECRET": "compensated-session-secret",
        }
    )

    with pytest.raises(CoordinatorError, match="legacy-password-file"):
        coordinator.recover(issuer=ISSUER)

    payload = coordinator.recover(
        issuer=ISSUER,
        legacy_password_file=provider.legacy_password_file,
    )

    assert payload["auth_state"] == "password"
    assert payload["journal_phase"] == "rolled_back"
    assert any(
        "password-preflight" in command
        for command in fake.commands
        if "scripts/oidc_cutover.py" in command
    )


def test_incomplete_recovery_refuses_a_retagged_image(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    coordinator._write_journal(
        phase="cutover_config_written",
        operation="cutover",
        attestation=coordinator.attest(require_running=True),
    )
    fake.container_image_id = "sha256:new-valid-image"
    fake.expected_image_id = "sha256:new-valid-image"
    command_boundary = len(fake.commands)

    with pytest.raises(CoordinatorError, match="authority changed"):
        coordinator.recover(issuer=ISSUER)

    assert not any(
        "scripts/oidc_cutover.py" in command for command in fake.commands[command_boundary:]
    )


def test_incomplete_recovery_refuses_changed_rendered_app_config(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    coordinator._write_journal(
        phase="finalize_web_stopped",
        operation="finalize",
        attestation=coordinator.attest(require_running=True),
    )
    fake.config_marker = "changed"

    with pytest.raises(CoordinatorError, match="authority changed"):
        coordinator.recover(issuer=ISSUER)


def test_recreate_refuses_an_image_id_change_after_compose_up(installation):
    coordinator, fake, _provider = installation
    authority = coordinator.attest(require_running=True)
    stopped = coordinator._stop_app()
    coordinator._assert_same_authority(authority, stopped)
    fake.image_after_up = "sha256:unexpected-after-up"

    with pytest.raises(CoordinatorError, match="authority changed"):
        coordinator._recreate_app(authority=stopped)


def test_retire_legacy_is_explicit_and_disables_normal_rollback(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    coordinator.finalize(issuer=ISSUER, confirmation=FINALIZE_CONFIRMATION)
    not_before = json.loads(coordinator.state_file.read_text())["updated_at"]

    payload = coordinator.retire_legacy(
        issuer=ISSUER,
        confirmation=RETIRE_CONFIRMATION,
    )

    assert payload["result"] == "ok"
    assert json.loads(coordinator.state_file.read_text())["phase"] == "legacy_retired"
    assert any(
        "scripts/oidc_cutover.py" in command and "retire-preflight" in command
        for command in fake.commands
    )
    assert any(
        "scripts/oidc_cutover.py" in command and "retire-legacy" in command
        for command in fake.commands
    )
    retire_helper = next(
        command
        for command in fake.commands
        if "scripts/oidc_cutover.py" in command and "retire-legacy" in command
    )
    assert retire_helper[retire_helper.index("--not-before") + 1] == not_before
    assert "--allow-already-retired" not in retire_helper
    with pytest.raises(CoordinatorError, match="rollback is retired"):
        coordinator.rollback(
            issuer=ISSUER,
            legacy_password_file=provider.legacy_password_file,
            confirmation=ROLLBACK_CONFIRMATION,
        )


def test_retire_recovery_preserves_original_login_proof_boundary(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    coordinator.finalize(issuer=ISSUER, confirmation=FINALIZE_CONFIRMATION)
    not_before = json.loads(coordinator.state_file.read_text())["updated_at"]
    stopped = coordinator._stop_app()
    coordinator._write_journal(
        phase="retire_web_stopped",
        operation="retire_legacy",
        attestation=stopped,
        proof_not_before=not_before,
    )

    payload = coordinator.recover(issuer=ISSUER)

    assert payload["journal_phase"] == "legacy_retired"
    retire_helper = next(
        command
        for command in fake.commands
        if "scripts/oidc_cutover.py" in command and "retire-legacy" in command
    )
    assert retire_helper[retire_helper.index("--not-before") + 1] == not_before
    assert "--allow-already-retired" in retire_helper
    recovered = json.loads(coordinator.state_file.read_text())
    assert recovered["proof_not_before"] == not_before


def test_finalize_requires_the_current_cutover_waiting_phase(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    coordinator._write_journal(
        phase="oidc_bound",
        operation="finalize",
        attestation=coordinator.attest(require_running=True),
    )

    with pytest.raises(CoordinatorError, match="awaiting_owner_binding"):
        coordinator.finalize(issuer=ISSUER, confirmation=FINALIZE_CONFIRMATION)

    assert fake.running is True


def test_command_failure_never_repeats_subprocess_secret(installation):
    coordinator, _fake, _provider = installation

    def failed(_command):
        return ProcessResult(1, stderr="postgresql://user:secret@db/vitals")

    coordinator.runner = failed
    with pytest.raises(CoordinatorError) as refusal:
        coordinator.attest()

    assert "secret" not in str(refusal.value)
    assert "postgresql" not in str(refusal.value)


def test_owner_only_journal_refuses_a_symlink(installation, tmp_path):
    coordinator, _fake, _provider = installation
    target = tmp_path / "elsewhere"
    target.write_text("{}", encoding="utf-8")
    coordinator.state_file.symlink_to(target)

    with pytest.raises(CoordinatorError, match="owner-only"):
        coordinator.status()


def test_preexisting_journal_symlink_is_not_resolved_away(installation, tmp_path):
    coordinator, fake, _provider = installation
    target = tmp_path / "journal-target"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "journal-link"
    link.symlink_to(target)
    guarded = OidcCutoverHost(
        project="vitals_prod",
        compose_files=coordinator.compose_files,
        env_files=[],
        runtime_env=coordinator.runtime_env,
        state_file=link,
        runner=fake.run,
        http_probe=fake.probe,
    )

    with pytest.raises(CoordinatorError, match="owner-only"):
        guarded.status()


def test_recovery_requires_issuer_for_an_oidc_runtime(installation):
    coordinator, fake, provider = installation
    coordinator.cutover(provider, confirmation=CUTOVER_CONFIRMATION)
    fake.running = False

    with pytest.raises(CoordinatorError, match="--issuer"):
        coordinator.recover(issuer=None)


def test_exact_confirmation_is_required_before_any_stop(installation):
    coordinator, fake, provider = installation

    with pytest.raises(CoordinatorError, match="confirmation"):
        coordinator.cutover(provider, confirmation="yes")

    assert not any("stop" in command for command in fake.commands)


def test_secret_parent_permissions_are_checked_without_reading_secret(
    installation,
):
    coordinator, _fake, provider = installation
    provider.client_secret_file.parent.chmod(0o755)

    with pytest.raises(CoordinatorError, match="0700"):
        coordinator.preflight(provider)


def test_operation_lock_refuses_a_concurrent_coordinator(installation):
    coordinator, _fake, _provider = installation

    with coordinator.operation_lock():
        with pytest.raises(CoordinatorError, match="already running"):
            with coordinator.operation_lock():
                pass

    lock = coordinator.state_file.with_name(f"{coordinator.state_file.name}.lock")
    assert stat_mode(lock) == 0o600
