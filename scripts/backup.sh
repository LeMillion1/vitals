#!/bin/sh
# Periodic, verifiable local recovery points for Vitals.
#
# A bundle is usable only when its SHA-256 manifest exists. The manifest is
# published atomically after the database, Garmin session, private-file volume,
# and legacy upload archives all succeed. Offsite replication and restore
# tooling must ignore loose artifacts without a manifest.

set -eu
if (set -o pipefail) 2>/dev/null; then
    set -o pipefail
else
    echo "[backup] ERROR: /bin/sh must support pipefail" >&2
    exit 2
fi
umask 077

BACKUP_DIR="${VITALS_BACKUP_DIR:-/backups}"
RETENTION_DAYS="${VITALS_BACKUP_RETENTION_DAYS:-7}"
INTERVAL_SECONDS="${VITALS_BACKUP_INTERVAL_SECONDS:-86400}"
RUN_ONCE="${VITALS_BACKUP_RUN_ONCE:-false}"
GARMIN_SESSION_DIR="${VITALS_GARMIN_SESSION_DIR:-/garmin_session}"
PRIVATE_FILE_DIR="${VITALS_PRIVATE_FILE_DIR:-/private_files}"
LEGACY_UPLOAD_DIR="${VITALS_LEGACY_UPLOAD_DIR:-/legacy_uploads}"

require_positive_integer() {
    name="$1"
    value="$2"
    case "$value" in
        ''|*[!0-9]*)
            echo "[backup] ERROR: $name must be a positive integer" >&2
            exit 2
            ;;
    esac
    if [ "$value" -lt 1 ]; then
        echo "[backup] ERROR: $name must be at least 1" >&2
        exit 2
    fi
}

require_positive_integer "VITALS_BACKUP_RETENTION_DAYS" "$RETENTION_DAYS"
require_positive_integer "VITALS_BACKUP_INTERVAL_SECONDS" "$INTERVAL_SECONDS"
case "$RUN_ONCE" in
    true|false) ;;
    *)
        echo "[backup] ERROR: VITALS_BACKUP_RUN_ONCE must be true or false" >&2
        exit 2
        ;;
esac

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
echo "[backup] starting — dir=$BACKUP_DIR retention=${RETENTION_DAYS}d interval=${INTERVAL_SECONDS}s"

remove_cycle_files() {
    rm -f \
        "$database_file" "$database_file.tmp" \
        "$garmin_file" "$garmin_file.tmp" \
        "$private_file" "$private_file.tmp" \
        "$legacy_upload_file" "$legacy_upload_file.tmp" \
        "$manifest_file" "$manifest_file.tmp"
}

rotate_complete_bundles() {
    # Rotation is allowed only after a new complete bundle exists. Remove the
    # exact artifacts named by an expired manifest, then the manifest itself.
    find "$BACKUP_DIR" -name 'vitals_bundle_*.sha256' -type f \
        -mtime +"$RETENTION_DAYS" -print | while IFS= read -r old_manifest; do
        old_name="${old_manifest##*/}"
        old_stamp="${old_name#vitals_bundle_}"
        old_stamp="${old_stamp%.sha256}"
        case "$old_stamp" in
            ????????T??????Z) ;;
            *)
                echo "[backup] WARN: refusing malformed manifest name $old_name" >&2
                continue
                ;;
        esac
        case "$old_stamp" in
            *[!0-9TZ]*)
                echo "[backup] WARN: refusing malformed manifest name $old_name" >&2
                continue
                ;;
        esac
        rm -f \
            "$BACKUP_DIR/vitals_${old_stamp}.sql.gz" \
            "$BACKUP_DIR/garmin_session_${old_stamp}.tar.gz" \
            "$BACKUP_DIR/private_files_${old_stamp}.tar.gz" \
            "$BACKUP_DIR/legacy_uploads_${old_stamp}.tar.gz" \
            "$old_manifest"
        echo "[backup] pruned complete bundle $old_stamp"
    done

    # Pre-manifest versions produced loose artifacts. Once a verified bundle
    # exists, their original retention contract can safely continue.
    find "$BACKUP_DIR" \
        \( -name 'vitals_*.sql.gz' -o -name 'garmin_session_*.tar.gz' -o -name 'private_files_*.tar.gz' -o -name 'legacy_uploads_*.tar.gz' \) \
        -type f -mtime +"$RETENTION_DAYS" -print -exec rm -f {} +
}

run_backup_cycle() {
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    database_file="$BACKUP_DIR/vitals_${timestamp}.sql.gz"
    garmin_file="$BACKUP_DIR/garmin_session_${timestamp}.tar.gz"
    private_file="$BACKUP_DIR/private_files_${timestamp}.tar.gz"
    legacy_upload_file="$BACKUP_DIR/legacy_uploads_${timestamp}.tar.gz"
    manifest_file="$BACKUP_DIR/vitals_bundle_${timestamp}.sha256"

    remove_cycle_files

    # PGHOST/PGUSER/PGPASSWORD/PGDATABASE come from the sidecar environment.
    if ! pg_dump --no-owner --no-privileges | gzip -c > "$database_file.tmp"; then
        echo "[backup] ERROR: database dump failed at $timestamp" >&2
        remove_cycle_files
        return 1
    fi
    chmod 600 "$database_file.tmp"
    mv "$database_file.tmp" "$database_file"

    if [ ! -d "$GARMIN_SESSION_DIR" ]; then
        echo "[backup] ERROR: no Garmin session dir at $GARMIN_SESSION_DIR" >&2
        remove_cycle_files
        return 1
    fi
    if ! tar -czf "$garmin_file.tmp" -C "$GARMIN_SESSION_DIR" .; then
        echo "[backup] ERROR: Garmin session archive failed at $timestamp" >&2
        remove_cycle_files
        return 1
    fi
    chmod 600 "$garmin_file.tmp"
    mv "$garmin_file.tmp" "$garmin_file"

    if [ ! -d "$PRIVATE_FILE_DIR" ]; then
        echo "[backup] ERROR: no private file dir at $PRIVATE_FILE_DIR" >&2
        remove_cycle_files
        return 1
    fi
    if ! tar -czf "$private_file.tmp" -C "$PRIVATE_FILE_DIR" .; then
        echo "[backup] ERROR: private-file archive failed at $timestamp" >&2
        remove_cycle_files
        return 1
    fi
    chmod 600 "$private_file.tmp"
    mv "$private_file.tmp" "$private_file"

    # This bind mount remains part of the application until every installation
    # has relocated pre-private-volume uploads. It must therefore remain in the
    # disaster-recovery set even when it is empty on a new installation.
    if [ ! -d "$LEGACY_UPLOAD_DIR" ]; then
        echo "[backup] ERROR: no legacy upload dir at $LEGACY_UPLOAD_DIR" >&2
        remove_cycle_files
        return 1
    fi
    if ! tar -czf "$legacy_upload_file.tmp" -C "$LEGACY_UPLOAD_DIR" .; then
        echo "[backup] ERROR: legacy-upload archive failed at $timestamp" >&2
        remove_cycle_files
        return 1
    fi
    chmod 600 "$legacy_upload_file.tmp"
    mv "$legacy_upload_file.tmp" "$legacy_upload_file"

    if ! (
        cd "$BACKUP_DIR"
        sha256sum \
            "${database_file##*/}" \
            "${garmin_file##*/}" \
            "${private_file##*/}" \
            "${legacy_upload_file##*/}" > "${manifest_file##*/}.tmp"
    ); then
        echo "[backup] ERROR: checksum manifest failed at $timestamp" >&2
        remove_cycle_files
        return 1
    fi
    chmod 600 "$manifest_file.tmp"
    mv "$manifest_file.tmp" "$manifest_file"

    echo "[backup] wrote complete bundle $timestamp"
    rotate_complete_bundles
}

while true; do
    # Keep this as a simple command so `set -e` remains active inside the shell
    # function. Compose then exposes and restarts a failed backup sidecar.
    run_backup_cycle
    if [ "$RUN_ONCE" = true ]; then
        exit 0
    fi
    sleep "$INTERVAL_SECONDS"
done
