#!/bin/sh
# Replicate only complete local Vitals recovery sets into an initialized restic
# repository. This process never initializes, forgets, prunes, or restores a
# repository; destructive retention belongs on a separate trusted admin host.

set -eu
if (set -o pipefail) 2>/dev/null; then
    set -o pipefail
else
    echo "[offsite] ERROR: /bin/sh must support pipefail" >&2
    exit 2
fi
umask 077

BACKUP_DIR="${VITALS_BACKUP_DIR:-/backups}"
STATE_DIR="${VITALS_OFFSITE_STATE_DIR:-/state}"
SOURCE_ENV="${VITALS_OFFSITE_ENV_FILE:-/source/vitals.env}"
SOURCE_RUNTIME_ENV="${VITALS_OFFSITE_RUNTIME_ENV_FILE:-/source/vitals.runtime.env}"
INTERVAL_SECONDS="${VITALS_OFFSITE_INTERVAL_SECONDS:-900}"
RUN_ONCE="${VITALS_OFFSITE_RUN_ONCE:-false}"
BACKUP_HOSTNAME="${VITALS_OFFSITE_HOSTNAME:-vitals-installation}"
MARKER="$STATE_DIR/last-successful-manifest"

case "$INTERVAL_SECONDS" in
    ''|*[!0-9]*)
        echo "[offsite] ERROR: VITALS_OFFSITE_INTERVAL_SECONDS must be a positive integer" >&2
        exit 2
        ;;
esac
if [ "$INTERVAL_SECONDS" -lt 1 ]; then
    echo "[offsite] ERROR: VITALS_OFFSITE_INTERVAL_SECONDS must be at least 1" >&2
    exit 2
fi
case "$RUN_ONCE" in
    true|false) ;;
    *)
        echo "[offsite] ERROR: VITALS_OFFSITE_RUN_ONCE must be true or false" >&2
        exit 2
        ;;
esac
case "$BACKUP_HOSTNAME" in
    ''|*[!A-Za-z0-9._-]*)
        echo "[offsite] ERROR: VITALS_OFFSITE_HOSTNAME is invalid" >&2
        exit 2
        ;;
esac

for required_file in "$RESTIC_REPOSITORY_FILE" "$RESTIC_PASSWORD_FILE"; do
    if [ ! -s "$required_file" ]; then
        echo "[offsite] ERROR: a required restic secret file is missing or empty" >&2
        exit 2
    fi
done

read_single_line_secret() {
    awk '
        NR > 1 { exit 1 }
        { value = $0 }
        END {
            if (NR != 1 || value == "") exit 1
            printf "%s", value
        }
    ' "$1"
}
if ! AWS_ACCESS_KEY_ID="$(
    read_single_line_secret "$VITALS_RESTIC_S3_ACCESS_KEY_FILE"
)"; then
    echo "[offsite] ERROR: the S3 access-key secret must be one non-empty line" >&2
    exit 2
fi
if ! AWS_SECRET_ACCESS_KEY="$(
    read_single_line_secret "$VITALS_RESTIC_S3_SECRET_KEY_FILE"
)"; then
    echo "[offsite] ERROR: the S3 secret-key secret must be one non-empty line" >&2
    exit 2
fi
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
for source_file in "$SOURCE_ENV" "$SOURCE_RUNTIME_ENV"; do
    if [ ! -f "$source_file" ]; then
        echo "[offsite] ERROR: a protected installation environment file is missing" >&2
        exit 2
    fi
done
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

validate_manifest() {
    manifest="$1"
    manifest_name="${manifest##*/}"
    stamp="${manifest_name#vitals_bundle_}"
    stamp="${stamp%.sha256}"
    case "$stamp" in
        ????????T??????Z) ;;
        *)
            echo "[offsite] ERROR: malformed complete-set manifest name" >&2
            return 1
            ;;
    esac
    case "$stamp" in
        *[!0-9TZ]*)
            echo "[offsite] ERROR: malformed complete-set manifest name" >&2
            return 1
            ;;
    esac

expected_names="vitals_${stamp}.sql.gz
garmin_session_${stamp}.tar.gz
private_files_${stamp}.tar.gz
legacy_uploads_${stamp}.tar.gz"
    actual_names="$(
        awk '
            NF != 2 || length($1) != 64 || $1 ~ /[^0-9a-f]/ { exit 1 }
            { print $2 }
        ' "$manifest"
    )" || {
        echo "[offsite] ERROR: malformed complete-set manifest content" >&2
        return 1
    }
    if [ "$actual_names" != "$expected_names" ]; then
        echo "[offsite] ERROR: manifest does not name the exact recovery set" >&2
        return 1
    fi
    if ! (cd "$BACKUP_DIR" && sha256sum -c "$manifest_name" >/dev/null); then
        echo "[offsite] ERROR: local recovery-set checksum failed" >&2
        return 1
    fi

    database_name="vitals_${stamp}.sql.gz"
    garmin_name="garmin_session_${stamp}.tar.gz"
    private_name="private_files_${stamp}.tar.gz"
    legacy_upload_name="legacy_uploads_${stamp}.tar.gz"
}

publish_marker() {
    if ! printf '%s\n' "$manifest_name" > "$MARKER.tmp" \
        || ! chmod 600 "$MARKER.tmp" \
        || ! mv "$MARKER.tmp" "$MARKER"; then
        rm -f "$MARKER.tmp"
        echo "[offsite] ERROR: the replication marker could not be published" >&2
        return 1
    fi
}

replicate_latest() {
    if ! manifest="$(
        find "$BACKUP_DIR" -maxdepth 1 -type f -name 'vitals_bundle_*.sha256' -print \
            | sort | tail -n 1
    )"; then
        echo "[offsite] ERROR: local recovery-set discovery failed" >&2
        return 1
    fi
    if [ -z "$manifest" ]; then
        echo "[offsite] waiting for a complete local recovery set"
        return 0
    fi
    manifest_name="${manifest##*/}"
    if ! validate_manifest "$manifest"; then
        return 1
    fi

    # Opening the repository is the preflight. Production never calls init.
    # The bundle tag is also the durable idempotency key: container-created
    # parent-directory metadata can differ even when every selected file is
    # byte-for-byte unchanged, so --skip-if-unchanged alone is insufficient.
    if ! existing_snapshots="$(
        restic snapshots --json --tag "vitals-bundle:$stamp"
    )"; then
        echo "[offsite] ERROR: initialized repository preflight failed" >&2
        return 1
    fi
    case "$existing_snapshots" in
        \[*\]) ;;
        *)
            echo "[offsite] ERROR: repository preflight returned malformed JSON" >&2
            return 1
            ;;
    esac
    if [ "$existing_snapshots" != "[]" ]; then
        publish_marker
        echo "[offsite] complete set $stamp is already replicated"
        return 0
    fi
    if ! restic backup \
        --skip-if-unchanged \
        --host "$BACKUP_HOSTNAME" \
        --tag vitals \
        --tag "vitals-bundle:$stamp" \
        "$manifest" \
        "$BACKUP_DIR/$database_name" \
        "$BACKUP_DIR/$garmin_name" \
        "$BACKUP_DIR/$private_name" \
        "$BACKUP_DIR/$legacy_upload_name" \
        "$SOURCE_ENV" \
        "$SOURCE_RUNTIME_ENV"; then
        echo "[offsite] ERROR: encrypted replication failed" >&2
        return 1
    fi

    publish_marker
    echo "[offsite] verified encrypted replication for complete set $stamp"
}

while true; do
    # Keep this as a simple command: placing a shell function in an `if` or `||`
    # condition disables `set -e` inside its body in POSIX shells.
    replicate_latest
    if [ "$RUN_ONCE" = true ]; then
        exit 0
    fi
    sleep "$INTERVAL_SECONDS"
done
