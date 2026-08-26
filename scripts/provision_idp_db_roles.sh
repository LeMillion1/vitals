#!/bin/sh
# Provision ZITADEL's non-superuser owner and read-only backup credentials.

set -eu

read_secret() {
    name="$1"
    path="$2"
    value="$(awk 'NR > 1 { exit 1 } { value = $0 } END { if (NR != 1 || value == "") exit 1; printf "%s", value }' "$path")" || {
        echo "[idp-db] ERROR: $name must contain exactly one non-empty line" >&2
        exit 2
    }
    case "$value" in
        *[!A-Za-z0-9_.~-]*)
            echo "[idp-db] ERROR: $name must use URL-safe characters only" >&2
            exit 2
            ;;
    esac
    printf '%s' "$value"
}

admin_password="$(read_secret admin-password "$VITALS_IDP_DB_ADMIN_PASSWORD_FILE")"
service_password="$(read_secret service-password "$VITALS_IDP_DB_SERVICE_PASSWORD_FILE")"
backup_password="$(read_secret backup-password "$VITALS_IDP_DB_BACKUP_PASSWORD_FILE")"
export PGPASSWORD="$admin_password"
unset admin_password

psql_admin() {
    psql -X -v ON_ERROR_STOP=1 -h vitals_idp_db -U postgres -d postgres "$@"
}

case "${1:-}" in
    provision)
        psql_admin \
            -v service_password="$service_password" \
            -v backup_password="$backup_password" <<'SQL'
SELECT format(
    'CREATE ROLE zitadel LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'service_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zitadel') \gexec
SELECT format(
    'ALTER ROLE zitadel LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'service_password'
) \gexec
SELECT 'CREATE DATABASE zitadel OWNER zitadel'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'zitadel') \gexec
ALTER DATABASE zitadel OWNER TO zitadel;
SELECT format(
    'CREATE ROLE zitadel_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'backup_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zitadel_backup') \gexec
SELECT format(
    'ALTER ROLE zitadel_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'backup_password'
) \gexec
SQL
        ;;
    grant-backup)
        psql_admin -v backup_password="$backup_password" <<'SQL'
SELECT format(
    'ALTER ROLE zitadel_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'backup_password'
) \gexec
GRANT CONNECT ON DATABASE zitadel TO zitadel_backup;
GRANT pg_read_all_data TO zitadel_backup;
SQL
        ;;
    *)
        echo "usage: $0 provision|grant-backup" >&2
        exit 2
        ;;
esac

unset PGPASSWORD service_password backup_password
echo "[idp-db] ${1} complete"
