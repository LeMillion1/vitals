#!/bin/bash
# Vitals deploy script. Runs on the server, from the checkout it lives in.
set -e

# The whole body is a function so bash parses the file up front. Without this,
# the `git reset --hard` below can rewrite this very script mid-execution and
# bash resumes reading at a stale byte offset — garbage, on the one deploy that
# actually changes deploy.sh.
main() {
  cd "$(dirname "$0")"

  # GitHub is the source of truth — hard-sync so the deploy target always
  # matches origin, discarding any local drift.
  git fetch origin
  git reset --hard origin/master

  docker compose up -d --build
  echo "✓ Vitals deployed"
}

main "$@"
