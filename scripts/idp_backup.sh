#!/bin/sh
# Periodic, independently restorable recovery points for the ZITADEL database.
#
# Identity data has a different recovery boundary from health data. A ZITADEL
# dump is complete only after its one-artifact checksum manifest is published;
# offsite replication and restore tooling ignore every loose file.

set -eu
if (set -o pipefail) 2>/dev/null; then
    set -o pipefail
else
    echo "[idp-backup] ERROR: /bin/sh must support pipefail" >&2
    exit 2
fi
umask 077

BACKUP_DIR="${VITALS_IDP_BACKUP_DIR:-/backups/idp}"
RETENTION_DAYS="${VITALS_IDP_BACKUP_RETENTION_DAYS:-7}"
INTERVAL_SECONDS="${VITALS_IDP_BACKUP_INTERVAL_SECONDS:-86400}"
RUN_ONCE="${VITALS_IDP_BACKUP_RUN_ONCE:-false}"

require_positive_integer() {
    name="$1"
    value="$2"
    case "$value" in
        ''|*[!0-9]*)
            echo "[idp-backup] ERROR: $name must be a positive integer" >&2
            exit 2
            ;;
    esac
    if [ "$value" -lt 1 ]; then
        echo "[idp-backup] ERROR: $name must be at least 1" >&2
        exit 2
    fi
}

require_positive_integer "VITALS_IDP_BACKUP_RETENTION_DAYS" "$RETENTION_DAYS"
require_positive_integer "VITALS_IDP_BACKUP_INTERVAL_SECONDS" "$INTERVAL_SECONDS"
case "$RUN_ONCE" in
    true|false) ;;
    *)
        echo "[idp-backup] ERROR: VITALS_IDP_BACKUP_RUN_ONCE must be true or false" >&2
        exit 2
        ;;
esac

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
echo "[idp-backup] starting — retention=${RETENTION_DAYS}d interval=${INTERVAL_SECONDS}s"

remove_cycle_files() {
    rm -f \
        "$database_file" "$database_file.tmp" \
        "$manifest_file" "$manifest_file.tmp"
}

rotate_complete_bundles() {
    find "$BACKUP_DIR" -name 'zitadel_bundle_*.sha256' -type f \
        -mtime +"$RETENTION_DAYS" -print | while IFS= read -r old_manifest; do
        old_name="${old_manifest##*/}"
        old_stamp="${old_name#zitadel_bundle_}"
        old_stamp="${old_stamp%.sha256}"
        case "$old_stamp" in
            ????????T??????Z) ;;
            *)
                echo "[idp-backup] WARN: refusing malformed manifest name $old_name" >&2
                continue
                ;;
        esac
        case "$old_stamp" in
            *[!0-9TZ]*)
                echo "[idp-backup] WARN: refusing malformed manifest name $old_name" >&2
                continue
                ;;
        esac
        rm -f \
            "$BACKUP_DIR/zitadel_${old_stamp}.sql.gz" \
            "$old_manifest"
        echo "[idp-backup] pruned complete bundle $old_stamp"
    done

    # Pre-manifest or interrupted versions remain loose and are never recovery
    # points. Retire them only after a new complete identity bundle exists.
    find "$BACKUP_DIR" -name 'zitadel_*.sql.gz' -type f \
        -mtime +"$RETENTION_DAYS" -print -exec rm -f {} +
}

run_backup_cycle() {
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    database_file="$BACKUP_DIR/zitadel_${timestamp}.sql.gz"
    manifest_file="$BACKUP_DIR/zitadel_bundle_${timestamp}.sha256"

    remove_cycle_files
    # PGHOST/PGUSER/PGPASSWORD/PGDATABASE come from the isolated sidecar.
    if ! table_count="$(
        psql -X -A -t -v ON_ERROR_STOP=1 -c \
            "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
    )"; then
        echo "[idp-backup] ERROR: database readiness check failed at $timestamp" >&2
        remove_cycle_files
        return 1
    fi
    case "$table_count" in
        ''|*[!0-9]*|0)
            echo "[idp-backup] ERROR: refusing an empty identity database at $timestamp" >&2
            remove_cycle_files
            return 1
            ;;
    esac
    if ! pg_dump --no-owner --no-privileges | gzip -c > "$database_file.tmp"; then
        echo "[idp-backup] ERROR: database dump failed at $timestamp" >&2
        remove_cycle_files
        return 1
    fi
    chmod 600 "$database_file.tmp"
    mv "$database_file.tmp" "$database_file"

    if ! (
        cd "$BACKUP_DIR"
        sha256sum "${database_file##*/}" > "${manifest_file##*/}.tmp"
    ); then
        echo "[idp-backup] ERROR: checksum manifest failed at $timestamp" >&2
        remove_cycle_files
        return 1
    fi
    chmod 600 "$manifest_file.tmp"
    mv "$manifest_file.tmp" "$manifest_file"

    echo "[idp-backup] wrote complete bundle $timestamp"
    rotate_complete_bundles
}

while true; do
    run_backup_cycle
    if [ "$RUN_ONCE" = true ]; then
        exit 0
    fi
    sleep "$INTERVAL_SECONDS"
done
