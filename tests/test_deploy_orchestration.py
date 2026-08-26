"""Static safety contracts for the production deploy orchestrator."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy.sh"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}", start)
    return source[start:end]


def test_deploy_script_has_valid_bash_syntax_and_is_executable():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert os.access(SCRIPT, os.X_OK)


def test_deploy_requires_an_explicit_existing_compose_project():
    source = _script()
    env = os.environ.copy()
    env.pop("COMPOSE_PROJECT_NAME", None)
    result = subprocess.run(
        ["bash", str(SCRIPT), "deploy"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "set COMPOSE_PROJECT_NAME" in result.stderr
    assert "${COMPOSE_PROJECT_NAME:-vitals}" not in source
    assert "docker compose -p" not in source
    main = _function(source, "main")
    assert main.index('[[ -n "${COMPOSE_PROJECT_NAME:-}" ]]') < main.index(
        "require_command docker"
    )


def test_deploy_fast_forwards_and_builds_one_immutable_shared_image():
    source = _script()
    deploy = _function(source, "deploy_release")

    assert "git merge --ff-only \"$target_sha\"" in deploy
    assert "git fetch --prune origin\n" in deploy
    assert "git reset" not in source
    assert "git checkout" not in source
    assert source.count("compose build vitals_app") == 1
    assert "docker image tag" not in source
    assert "assert_shared_runtime_image" in source
    assert "assert_runtime_config_mounts" in _function(source, "compose_preflight")
    assert "assert_runtime_data_mounts" in _function(source, "compose_preflight")
    assert "vitals.worker_health import check_configured_worker_health" in source


def test_deploy_attests_existing_project_before_fetch_or_start():
    source = _script()
    attestation = _function(source, "attest_existing_service")
    deploy = _function(source, "deploy_release")
    rollback = _function(source, "rollback_release")

    assert "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" in attestation
    assert "label=com.docker.compose.service=$service" in attestation
    assert "[[ \"$match_count\" == \"1\" ]]" in attestation
    assert deploy.index("attest_existing_project") < deploy.index("git fetch")
    assert deploy.index("attest_existing_project") < deploy.index(
        "start_data_services"
    )
    assert rollback.index("attest_existing_project") < rollback.index(
        "start_data_services"
    )


def test_deploy_orders_data_migration_roles_worker_web_and_smoke():
    deploy = _function(_script(), "deploy_release")
    ordered = (
        "start_data_services",
        "run_schema_and_roles",
        "switch_runtime_service vitals_worker",
        "switch_runtime_service vitals_app",
        "local_smoke",
        "write_state",
    )

    positions = [deploy.index(step) for step in ordered]
    assert positions == sorted(positions)


def test_runtime_rollback_is_split_compatible_and_never_changes_schema():
    source = _script()
    rollback = _function(source, "rollback_release")

    assert rollback.index("validate_split_image \"$target_sha\"") < rollback.index(
        "switch_runtime_service vitals_worker"
    )
    assert rollback.index("switch_runtime_service vitals_worker") < rollback.index(
        "switch_runtime_service vitals_app"
    )
    assert rollback.index("switch_runtime_service vitals_app") < rollback.index(
        "local_smoke"
    )
    assert "run_schema_and_roles" not in rollback
    assert "compose run" not in rollback
    assert "first split cutover has no automatic rollback anchor" in source
    assert "do not start a pre-split image against revision 0083" in source


def test_rollback_never_records_an_unvalidated_current_image():
    shell = r'''
source "$1"
current=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
target=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
ensure_clean_checkout() { :; }
validate_state_project() { :; }
attest_existing_project() { :; }
load_recorded_sha() { printf '%s\n' "$current"; }
validate_split_image() { [[ "$1" == "$target" ]]; }
compose_preflight() { :; }
start_data_services() { :; }
switch_runtime_service() { :; }
local_smoke() { :; }
write_state() { printf 'state:%s:%s\n' "$1" "$2"; }
export COMPOSE_PROJECT_NAME=vitals_prod
rollback_release "$target"
'''
    result = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "state:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:" in result.stdout
    assert "state:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:aaaa" not in (
        result.stdout
    )


def test_deploy_state_is_published_only_after_both_health_gates():
    source = _script()
    deploy = _function(source, "deploy_release")

    assert deploy.index("local_smoke") < deploy.index("write_state")
    write_state = _function(source, "write_state")
    assert 'mktemp "$SCRIPT_DIR/.vitals-deploy-state.tmp.XXXXXX"' in write_state
    assert 'mv -f -- "$STATE_TEMP" "$STATE_FILE"' in write_state
    assert 'STATE_TEMP=""' in write_state
    assert 'rm -f -- "$STATE_TEMP"' in _function(
        source, "cleanup_state_temp"
    )
    assert "trap cleanup_state_temp EXIT" in source
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "\n.vitals-deploy-state\n" in ignored
    assert "\n.vitals-deploy-state.tmp.*\n" in ignored


def test_failed_state_publish_removes_its_exact_temporary_file(tmp_path):
    state = tmp_path / ".vitals-deploy-state"
    shell = r'''
source "$1"
SCRIPT_DIR="$2"
STATE_FILE="$3"
export COMPOSE_PROJECT_NAME=vitals_prod
mv() { return 1; }
write_state 0123456789abcdef0123456789abcdef01234567 ""
'''
    result = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT), str(tmp_path), str(state)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not state.exists()
    assert list(tmp_path.glob(".vitals-deploy-state.tmp.*")) == []


def test_successful_state_publish_is_private_and_atomic(tmp_path):
    state = tmp_path / ".vitals-deploy-state"
    shell = r'''
source "$1"
SCRIPT_DIR="$2"
STATE_FILE="$3"
export COMPOSE_PROJECT_NAME=vitals_prod
write_state 0123456789abcdef0123456789abcdef01234567 ""
'''
    result = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT), str(tmp_path), str(state)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert state.stat().st_mode & 0o777 == 0o600
    assert "project=vitals_prod" in state.read_text(encoding="utf-8")
    assert list(tmp_path.glob(".vitals-deploy-state.tmp.*")) == []


def test_shared_image_helper_executes_with_synthetic_compose_json():
    shell = r'''
source "$1"
export COMPOSE_PROJECT_NAME=vitals_prod
export VITALS_IMAGE_TAG=0123456789abcdef0123456789abcdef01234567
compose() {
  printf '%s\n' '{"services":{"vitals_app":{"image":"vitals_prod_runtime:0123456789abcdef0123456789abcdef01234567"},"vitals_worker":{"image":"vitals_prod_runtime:0123456789abcdef0123456789abcdef01234567"},"vitals_migrate":{"image":"vitals_prod_runtime:0123456789abcdef0123456789abcdef01234567"},"vitals_db_roles":{"image":"vitals_prod_runtime:0123456789abcdef0123456789abcdef01234567"}}}'
}
assert_shared_runtime_image
'''
    result = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_runtime_mount_helper_proves_directory_permissions_and_asymmetric_access(
    tmp_path,
):
    runtime_dir = tmp_path / ".vitals-runtime"
    runtime_dir.mkdir(mode=0o700)
    (runtime_dir / "vitals.env").write_text(
        "VITALS_DATABASE_URL=synthetic\n", encoding="utf-8"
    )
    (runtime_dir / "vitals.env").chmod(0o600)
    payload = {
        "services": {
            name: {
                "environment": {
                    "VITALS_ENV_FILE": "/run/vitals-runtime/vitals.env"
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(runtime_dir),
                        "target": "/run/vitals-runtime",
                        "read_only": name == "vitals_worker",
                    }
                ],
            }
            for name in ("vitals_app", "vitals_worker")
        }
    }
    compose_json = tmp_path / "compose.json"
    compose_json.write_text(json.dumps(payload), encoding="utf-8")
    shell = r'''
source "$1"
SCRIPT_DIR="$2"
compose() { cat "$COMPOSE_JSON"; }
assert_runtime_config_mounts
'''
    environment = os.environ.copy()
    environment["COMPOSE_JSON"] = str(compose_json)

    result = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT), str(tmp_path)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    payload["services"]["vitals_app"]["volumes"][0]["read_only"] = True
    compose_json.write_text(json.dumps(payload), encoding="utf-8")
    rejected = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT), str(tmp_path)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode != 0
    assert "web read/write, worker read-only" in rejected.stderr

    # A private directory is still forbidden if it is broad enough to contain
    # the host/operator .env. This pins the actual owner-secret boundary rather
    # than merely comparing a directory path with a file path.
    payload["services"]["vitals_app"]["volumes"][0]["read_only"] = False
    for service in payload["services"].values():
        service["volumes"][0]["source"] = str(tmp_path)
    tmp_path.chmod(0o700)
    (tmp_path / "vitals.env").write_text(
        "VITALS_DATABASE_URL=synthetic\n", encoding="utf-8"
    )
    (tmp_path / "vitals.env").chmod(0o600)
    (tmp_path / ".env").write_text(
        "VITALS_DB_PASSWORD=operator-secret\n", encoding="utf-8"
    )
    compose_json.write_text(json.dumps(payload), encoding="utf-8")
    contains_operator_env = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT), str(tmp_path)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert contains_operator_env.returncode != 0
    assert "operator .env absent" in contains_operator_env.stderr


def test_shared_image_helper_rejects_a_mixed_runtime_image():
    shell = r'''
source "$1"
export COMPOSE_PROJECT_NAME=vitals_prod
export VITALS_IMAGE_TAG=0123456789abcdef0123456789abcdef01234567
compose() {
  printf '%s\n' '{"services":{"vitals_app":{"image":"vitals_prod_runtime:0123456789abcdef0123456789abcdef01234567"},"vitals_worker":{"image":"vitals_prod_runtime:different"},"vitals_migrate":{"image":"vitals_prod_runtime:0123456789abcdef0123456789abcdef01234567"},"vitals_db_roles":{"image":"vitals_prod_runtime:0123456789abcdef0123456789abcdef01234567"}}}'
}
assert_shared_runtime_image
'''
    result = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "do not resolve to one expected image" in result.stderr


def test_runtime_data_mount_helper_rejects_web_worker_source_drift(tmp_path):
    targets = (
        "/data/garmin_session",
        "/app/web/static/uploads",
        "/data/private_files",
    )
    payload = {
        "services": {
            name: {
                "volumes": [
                    {
                        "type": "volume",
                        "source": f"shared-{index}",
                        "target": target,
                        "read_only": target == "/app/web/static/uploads",
                    }
                    for index, target in enumerate(targets)
                ]
            }
            for name in ("vitals_app", "vitals_worker")
        }
    }
    compose_json = tmp_path / "compose.json"
    compose_json.write_text(json.dumps(payload), encoding="utf-8")
    shell = r'''
source "$1"
compose() { cat "$COMPOSE_JSON"; }
assert_runtime_data_mounts
'''
    environment = os.environ.copy()
    environment["COMPOSE_JSON"] = str(compose_json)

    accepted = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert accepted.returncode == 0, accepted.stderr

    worker_upload = payload["services"]["vitals_worker"]["volumes"][1]
    worker_upload["source"] = "/wrong/checkout/uploads"
    compose_json.write_text(json.dumps(payload), encoding="utf-8")
    rejected_source = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert rejected_source.returncode != 0
    assert "identical sources and access modes" in rejected_source.stderr

    worker_upload["source"] = "shared-1"
    worker_upload["read_only"] = False
    compose_json.write_text(json.dumps(payload), encoding="utf-8")
    rejected_access = subprocess.run(
        ["bash", "-c", shell, "test", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert rejected_access.returncode != 0
    assert "identical sources and access modes" in rejected_access.stderr


def test_operator_docs_preserve_project_and_first_cutover_boundary():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    ownership = (ROOT / "docs" / "OWNERSHIP_CUTOVER_RUNBOOK.md").read_text(
        encoding="utf-8"
    )
    recovery = (ROOT / "docs" / "BACKUP_RESTORE_RUNBOOK.md").read_text(
        encoding="utf-8"
    )

    assert readme.count("export COMPOSE_PROJECT_NAME=vitals_prod") >= 2
    assert readme.count("./deploy.sh rollback <full-git-sha>") == 2
    assert readme.count("VITALS_WORKER_DATABASE_URL=\"") == 2
    assert "currently deployed pre-split revision `dba1053`" in ownership
    assert "do not invoke the old deploy.sh" in ownership
    assert (
        "git worktree add --detach /root/vitals-pre-split dba1053" in ownership
    )
    assert ownership.index(
        "git worktree add --detach /root/vitals-pre-split dba1053"
    ) < ownership.index("\n./deploy.sh\n")
    assert "vitals_prod_pre_split:dba1053" in ownership
    assert "source: /root/vitals-commercial-production/.env.runtime" in ownership
    assert ownership.count(
        "--env-file /root/vitals-commercial-production/.env"
    ) >= 3
    assert 'mounts[0]["source"] == expected_source' in ownership
    assert (
        "not copy `.env`, `.env.runtime`, or `.vitals-runtime/` into the worktree"
        in ownership
    )
    assert (
        "python3 scripts/create_runtime_env.py --migrate-from .env.runtime"
        in ownership
    )
    assert ownership.index(
        "python3 scripts/create_runtime_env.py --migrate-from .env.runtime"
    ) < ownership.index("\n./deploy.sh\n")
    assert ownership.count("export COMPOSE_PROJECT_NAME=vitals_prod") >= 3
    assert '"${EDITOR:?set EDITOR}" docker-compose.emergency-pre-split.yml' in (
        ownership
    )
    assert "stat -c '%a' docker-compose.emergency-pre-split.yml" in ownership
    assert "alembic downgrade 0082" in ownership
    assert "Keep the split `vitals_worker` stopped" in ownership
    assert "`deploy.sh` refuses a new or" in recovery
    assert "waits for worker readiness" in recovery
