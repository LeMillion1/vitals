#!/usr/bin/env bash
# Safe production deploy/compatible-runtime rollback for the split web/worker stack.
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.vitals-deploy-state"
DEPLOY_PHASE="preflight"
ROLLBACK_SHA=""
STATE_TEMP=""

die() {
  echo "deploy: $*" >&2
  exit 1
}

on_error() {
  error_status=$?
  trap - ERR
  echo "deploy: failed during ${DEPLOY_PHASE}" >&2
  if [[ -n "$ROLLBACK_SHA" ]]; then
    echo "deploy: compatible runtime recovery: ./deploy.sh rollback $ROLLBACK_SHA" >&2
  else
    echo "deploy: no validated split-compatible rollback image is recorded" >&2
    echo "deploy: do not start a pre-split image against revision 0083; use the reviewed emergency runbook" >&2
  fi
  exit "$error_status"
}
trap on_error ERR

cleanup_state_temp() {
  if [[ -n "$STATE_TEMP" && -f "$STATE_TEMP" && ! -L "$STATE_TEMP" ]]; then
    rm -f -- "$STATE_TEMP"
  fi
  STATE_TEMP=""
}
trap cleanup_state_temp EXIT

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

validate_sha() {
  [[ "$1" =~ ^[0-9a-f]{40,64}$ ]]
}

validate_project() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9_-]*$ ]]
}

compose() {
  docker compose "$@"
}

runtime_image() {
  printf '%s_runtime:%s\n' "$COMPOSE_PROJECT_NAME" "$1"
}

ensure_clean_checkout() {
  git diff --quiet --ignore-submodules -- || die "tracked checkout changes must be reviewed before deploy"
  git diff --cached --quiet --ignore-submodules -- || die "staged checkout changes must be reviewed before deploy"
}

state_value() {
  key="$1"
  [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] || return 1
  awk -F= -v wanted="$key" '$1 == wanted { value=$2; count++ } END { if (count == 1) print value; else exit 1 }' "$STATE_FILE"
}

validate_state_project() {
  [[ -e "$STATE_FILE" ]] || return 0
  [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] || die "deploy state must be a regular non-symlink file"
  format_version="$(state_value format_version)" || die "deploy state is malformed"
  state_project="$(state_value project)" || die "deploy state is malformed"
  [[ "$format_version" == "1" && "$state_project" == "$COMPOSE_PROJECT_NAME" ]] || die "deploy state belongs to another project or format"
}

attest_existing_service() {
  service="$1"
  matches="$(docker ps -a \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --filter "label=com.docker.compose.service=$service" \
    --format '{{.ID}}')"
  match_count="$(printf '%s\n' "$matches" | awk 'NF { count++ } END { print count + 0 }')"
  [[ "$match_count" == "1" ]] || die \
    "existing project attestation expected one $service container, found $match_count"
}

attest_existing_project() {
  DEPLOY_PHASE="existing Compose project attestation"
  attest_existing_service vitals_db
  attest_existing_service vitals_app
}

write_state() {
  current_sha="$1"
  previous_sha="$2"
  [[ ! -L "$STATE_FILE" ]] || die "refusing to replace symlinked deploy state"
  STATE_TEMP="$(umask 077; mktemp "$SCRIPT_DIR/.vitals-deploy-state.tmp.XXXXXX")"
  printf 'format_version=1\nproject=%s\ncurrent_sha=%s\nprevious_sha=%s\n' \
    "$COMPOSE_PROJECT_NAME" "$current_sha" "$previous_sha" >"$STATE_TEMP"
  chmod 600 "$STATE_TEMP"
  mv -f -- "$STATE_TEMP" "$STATE_FILE"
  STATE_TEMP=""
}

validate_split_image() {
  sha="$1"
  image="$(runtime_image "$sha")"
  docker image inspect "$image" >/dev/null 2>&1 || return 1
  docker run --rm --entrypoint python "$image" -c \
    'from vitals.process_mode import ProcessMode; from vitals.worker_health import check_configured_worker_health; assert ProcessMode.WORKER.value == "worker"; assert callable(check_configured_worker_health)' \
    >/dev/null 2>&1
}

assert_shared_runtime_image() {
  expected="$(runtime_image "$VITALS_IMAGE_TAG")"
  compose config --format json | python3 -c '
import json
import os
import sys

payload = json.load(sys.stdin)
services = payload.get("services", {})
names = ("vitals_app", "vitals_worker", "vitals_migrate", "vitals_db_roles")
images = {services.get(name, {}).get("image") for name in names}
expected = "{}_runtime:{}".format(
    os.environ["COMPOSE_PROJECT_NAME"], os.environ["VITALS_IMAGE_TAG"]
)
raise SystemExit(0 if images == {expected} else 1)
' || die "runtime services do not resolve to one expected image: $expected"
}

assert_runtime_config_mounts() {
  export VITALS_DEPLOY_SCRIPT_DIR="$SCRIPT_DIR"
  compose config --format json | python3 -c '
import json
import os
from pathlib import Path
import stat
import sys

payload = json.load(sys.stdin)
services = payload.get("services", {})
app = services.get("vitals_app", {})
worker = services.get("vitals_worker", {})
target = "/run/vitals-runtime"

def one_mount(service):
    matches = [item for item in service.get("volumes", []) if item.get("target") == target]
    if len(matches) != 1 or matches[0].get("type") != "bind":
        raise SystemExit(1)
    return matches[0]

app_mount = one_mount(app)
worker_mount = one_mount(worker)
raw_source = Path(app_mount.get("source", ""))
source = raw_source.resolve(strict=False)
runtime_file = source / "vitals.env"
operator_file = (Path(os.environ["VITALS_DEPLOY_SCRIPT_DIR"]) / ".env").resolve(strict=False)
if (
    worker_mount.get("source") != app_mount.get("source")
    or bool(app_mount.get("read_only", False))
    or worker_mount.get("read_only") is not True
    or (app.get("environment") or {}).get("VITALS_ENV_FILE") != "/run/vitals-runtime/vitals.env"
    or (worker.get("environment") or {}).get("VITALS_ENV_FILE") != "/run/vitals-runtime/vitals.env"
    or any(item.get("target") == "/app/.env" for service in (app, worker) for item in service.get("volumes", []))
    or operator_file == source
    or operator_file.is_relative_to(source)
    or raw_source.is_symlink()
    or not source.is_dir()
    or source.stat().st_uid != os.geteuid()
    or runtime_file.is_symlink()
    or not runtime_file.is_file()
    or runtime_file.stat().st_uid != os.geteuid()
    or stat.S_IMODE(source.stat().st_mode) != 0o700
    or stat.S_IMODE(runtime_file.stat().st_mode) != 0o600
):
    raise SystemExit(1)
' || die "runtime config must be one private directory: web read/write, worker read-only, operator .env absent"
}

compose_preflight() {
  DEPLOY_PHASE="compose preflight"
  compose config --quiet
  assert_shared_runtime_image
  assert_runtime_config_mounts
}

start_data_services() {
  DEPLOY_PHASE="database and Redis readiness"
  compose up -d --wait --wait-timeout 90 --no-build vitals_db vitals_redis
}

run_schema_and_roles() {
  DEPLOY_PHASE="schema migration"
  compose run --rm --no-deps vitals_migrate
  DEPLOY_PHASE="runtime role provisioning"
  compose run --rm --no-deps vitals_db_roles
}

switch_runtime_service() {
  service="$1"
  DEPLOY_PHASE="$service switch"
  compose up -d --wait --wait-timeout 180 --no-deps --no-build \
    --force-recreate "$service"
}

local_smoke() {
  DEPLOY_PHASE="local HTTP smoke"
  endpoint="$(compose port vitals_app 8000)"
  [[ "$endpoint" =~ ^127\.0\.0\.1:[0-9]{1,5}$ ]] || die "vitals_app is not published on one IPv4 loopback port"
  curl --fail --silent --show-error --max-time 10 "http://$endpoint/health" >/dev/null
  curl --fail --silent --show-error --max-time 10 "http://$endpoint/login" >/dev/null
}

load_recorded_sha() {
  key="$1"
  value="$(state_value "$key")" || return 1
  [[ -z "$value" ]] && return 1
  validate_sha "$value" || die "deploy state contains an invalid $key"
  printf '%s\n' "$value"
}

deploy_release() {
  ensure_clean_checkout
  validate_state_project
  attest_existing_project
  branch="$(git rev-parse --abbrev-ref HEAD)"
  [[ "$branch" != "HEAD" ]] || die "detached HEAD; checkout the production branch first"
  git check-ref-format --branch "$branch" >/dev/null
  checkout_sha="$(git rev-parse HEAD)"
  validate_sha "$checkout_sha" || die "current checkout revision is invalid"

  recorded_current="$(load_recorded_sha current_sha || true)"
  recorded_previous="$(load_recorded_sha previous_sha || true)"

  DEPLOY_PHASE="remote fast-forward"
  git fetch --prune origin
  target_sha="$(git rev-parse "origin/$branch^{commit}")"
  validate_sha "$target_sha" || die "remote target revision is invalid"
  git merge-base --is-ancestor "$checkout_sha" "$target_sha" || die "remote update is not a fast-forward"
  git merge --ff-only "$target_sha"
  ensure_clean_checkout

  export VITALS_IMAGE_TAG="$target_sha"
  compose_preflight

  DEPLOY_PHASE="immutable runtime image build"
  target_image="$(runtime_image "$target_sha")"
  if ! docker image inspect "$target_image" >/dev/null 2>&1; then
    compose build vitals_app
  fi
  validate_split_image "$target_sha" || die "target image is not split-runtime compatible"

  if [[ "$recorded_current" == "$target_sha" ]]; then
    rollback_candidate="$recorded_previous"
  else
    rollback_candidate="$recorded_current"
  fi
  if [[ -n "$rollback_candidate" ]]; then
    validate_split_image "$rollback_candidate" || die "recorded rollback image is missing or not split-compatible"
    ROLLBACK_SHA="$rollback_candidate"
  else
    echo "deploy: first split cutover has no automatic rollback anchor" >&2
  fi

  start_data_services
  run_schema_and_roles
  switch_runtime_service vitals_worker
  switch_runtime_service vitals_app
  local_smoke

  write_state "$target_sha" "$ROLLBACK_SHA"
  DEPLOY_PHASE="complete"
  echo "✓ Vitals deployed — $branch @ ${target_sha:0:12}"
}

rollback_release() {
  ensure_clean_checkout
  validate_state_project
  attest_existing_project
  recorded_current="$(load_recorded_sha current_sha || true)"
  [[ -n "$recorded_current" ]] || die "no successful split deployment is recorded"
  target_sha="${1:-$(load_recorded_sha previous_sha || true)}"
  [[ -n "$target_sha" ]] || die "no previous split-compatible image is recorded"
  validate_sha "$target_sha" || die "rollback revision must be a full hexadecimal SHA"
  validate_split_image "$target_sha" || die "rollback image is missing or not split-compatible"

  validated_previous=""
  if validate_split_image "$recorded_current"; then
    ROLLBACK_SHA="$recorded_current"
    validated_previous="$recorded_current"
  fi
  export VITALS_IMAGE_TAG="$target_sha"
  compose_preflight
  start_data_services
  # Runtime rollback deliberately does not run Alembic or role provisioning.
  # It is valid only for a previous split image compatible with the current DB.
  switch_runtime_service vitals_worker
  switch_runtime_service vitals_app
  local_smoke

  write_state "$target_sha" "$validated_previous"
  DEPLOY_PHASE="complete"
  echo "✓ Vitals runtime rolled back — ${target_sha:0:12}"
}

usage() {
  echo "usage: COMPOSE_PROJECT_NAME=<exact-project> ./deploy.sh [deploy|rollback [full-sha]]" >&2
}

main() {
  cd "$SCRIPT_DIR"
  [[ -n "${COMPOSE_PROJECT_NAME:-}" ]] || die \
    "set COMPOSE_PROJECT_NAME to the existing production project (for example vitals_prod)"
  validate_project "$COMPOSE_PROJECT_NAME" || die "COMPOSE_PROJECT_NAME has an invalid Compose project shape"
  export COMPOSE_PROJECT_NAME
  require_command git
  require_command docker
  require_command python3
  require_command curl
  require_command mktemp

  action="${1:-deploy}"
  case "$action" in
    deploy)
      [[ $# -le 1 ]] || { usage; exit 2; }
      deploy_release
      ;;
    rollback)
      [[ $# -le 2 ]] || { usage; exit 2; }
      rollback_release "${2:-}"
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
