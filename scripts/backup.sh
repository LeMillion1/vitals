#!/bin/sh
# Periodic backup for Vitals: PostgreSQL, private medical files, and Garmin tokens.
#
# Runs as the vitals_backup sidecar (postgres:15-alpine, which ships pg_dump).
# Dumps the database, gzips it, writes atomically (.tmp -> final) into
# /backups, archives the private-file and Garmin-session volumes next to it,
# then prunes all three older than the retention window. One pass on start,
# then once every 24h.
#
# The Garmin archive is small but it is the one file in this project that may be
# unrecoverable: Garmin's login flow changes without notice, so a lost token can
# mean the integration simply cannot be logged back in. The database can always
# be re-synced from Garmin; the session cannot be re-derived from anything.
set -eu

BACKUP_DIR="${VITALS_BACKUP_DIR:-/backups}"
RETENTION_DAYS="${VITALS_BACKUP_RETENTION_DAYS:-7}"
INTERVAL_SECONDS="${VITALS_BACKUP_INTERVAL_SECONDS:-86400}"
GARMIN_SESSION_DIR="${VITALS_GARMIN_SESSION_DIR:-/garmin_session}"
PRIVATE_FILE_DIR="${VITALS_PRIVATE_FILE_DIR:-/private_files}"

mkdir -p "$BACKUP_DIR"
echo "[backup] starting — dir=$BACKUP_DIR retention=${RETENTION_DAYS}d interval=${INTERVAL_SECONDS}s"

while true; do
    ts="$(date +%Y%m%d_%H%M%S)"
    out="$BACKUP_DIR/vitals_${ts}.sql.gz"
    # PGHOST/PGUSER/PGPASSWORD/PGDATABASE come from the environment (compose).
    if pg_dump --no-owner --no-privileges | gzip -c > "$out.tmp"; then
        mv "$out.tmp" "$out"
        echo "[backup] wrote $out ($(wc -c < "$out") bytes)"
    else
        echo "[backup] ERROR: pg_dump failed at $ts" >&2
        rm -f "$out.tmp"
    fi

    # Garmin token store (read-only mount of the vitals_garmin_session volume).
    if [ -d "$GARMIN_SESSION_DIR" ]; then
        tokens="$BACKUP_DIR/garmin_session_${ts}.tar.gz"
        if tar -czf "$tokens.tmp" -C "$GARMIN_SESSION_DIR" .; then
            mv "$tokens.tmp" "$tokens"
            chmod 600 "$tokens"  # session tokens: owner-only, like a credential
            echo "[backup] wrote $tokens ($(wc -c < "$tokens") bytes)"
        else
            echo "[backup] ERROR: garmin session archive failed at $ts" >&2
            rm -f "$tokens.tmp"
        fi
    else
        echo "[backup] WARN: no Garmin session dir at $GARMIN_SESSION_DIR — not archived" >&2
    fi

    # Private medical bytes are not reconstructible from the database metadata.
    # Archive the read-only volume separately and keep the result owner-only.
    if [ -d "$PRIVATE_FILE_DIR" ]; then
        private_files="$BACKUP_DIR/private_files_${ts}.tar.gz"
        if tar -czf "$private_files.tmp" -C "$PRIVATE_FILE_DIR" .; then
            mv "$private_files.tmp" "$private_files"
            chmod 600 "$private_files"
            echo "[backup] wrote $private_files ($(wc -c < "$private_files") bytes)"
        else
            echo "[backup] ERROR: private file archive failed at $ts" >&2
            rm -f "$private_files.tmp"
        fi
    else
        echo "[backup] WARN: no private file dir at $PRIVATE_FILE_DIR — not archived" >&2
    fi

    # Rotation: drop backups older than the retention window.
    deleted="$(find "$BACKUP_DIR" \( -name 'vitals_*.sql.gz' -o -name 'garmin_session_*.tar.gz' -o -name 'private_files_*.tar.gz' \) -type f -mtime +"$RETENTION_DAYS" -print -delete | wc -l)"
    if [ "$deleted" -gt 0 ]; then
        echo "[backup] pruned $deleted file(s) older than ${RETENTION_DAYS}d"
    fi

    sleep "$INTERVAL_SECONDS"
done
