#!/usr/bin/env bash
# Run the test suite against a throwaway PostgreSQL.
set -euo pipefail

CONTAINER="vitals_test_pg_$$"
PORT="${VITALS_TEST_PG_PORT:-55432}"
PASSWORD="testpass"
DB="vitals_test"

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "→ starting throwaway Postgres ($CONTAINER) on host port ${PORT}…"
docker run -d --rm --name "$CONTAINER" \
  -e POSTGRES_PASSWORD="$PASSWORD" \
  -e POSTGRES_DB="$DB" \
  -p "127.0.0.1:${PORT}:5432" \
  postgres:15-alpine >/dev/null

echo "→ waiting for Postgres to accept connections…"
ready=0
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER" pg_isready -U postgres -d "$DB" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "✗ Postgres did not become ready in 30s" >&2
  exit 1
fi

export VITALS_TEST_DATABASE_URL="postgresql+asyncpg://postgres:${PASSWORD}@127.0.0.1:${PORT}/${DB}"
echo "→ running pytest against $VITALS_TEST_DATABASE_URL"

TEST_PYTHON="${VITALS_TEST_PYTHON:-.venv/bin/python}"
if [ ! -x "$TEST_PYTHON" ]; then
  TEST_PYTHON="python3"
fi

# Exercise the real migration chain and this branch's reversible identity slice
# before create_all()-based tests begin. The database is disposable, so a
# downgrade to the previous head is safe and catches drift that model tests miss.
if [ "${VITALS_TEST_ALEMBIC:-1}" = "1" ]; then
  export VITALS_DATABASE_URL="$VITALS_TEST_DATABASE_URL"
  echo "→ checking Alembic upgrade head → downgrade 0034 → upgrade head"
  "$TEST_PYTHON" -m alembic upgrade head
  "$TEST_PYTHON" -m alembic downgrade 0034
  "$TEST_PYTHON" -m alembic upgrade head
fi

# Pass through any extra args (specific files, -k, -q, …); default to full suite.
if [ "$#" -gt 0 ]; then
  "$TEST_PYTHON" -m pytest "$@"
else
  "$TEST_PYTHON" -m pytest
fi
