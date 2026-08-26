#!/bin/sh
# Replicate only complete ZITADEL recovery points into an independently
# encrypted restic repository. This process never handles the Vitals .env,
# health artifacts, repository initialization, retention, or restore.

set -eu
if (set -o pipefail) 2>/dev/null; then
    set -o pipefail
else
    echo "[idp-offsite] ERROR: /bin/sh must support pipefail" >&2
    exit 2
fi
umask 077

BACKUP_DIR="${VITALS_IDP_BACKUP_DIR:-/backups/idp}"
STATE_DIR="${VITALS_IDP_OFFSITE_STATE_DIR:-/state}"
INTERVAL_SECONDS="${VITALS_IDP_OFFSITE_INTERVAL_SECONDS:-900}"
RUN_ONCE="${VITALS_IDP_OFFSITE_RUN_ONCE:-false}"
BACKUP_HOSTNAME="${VITALS_IDP_OFFSITE_HOSTNAME:-vitals-identity}"
MARKER="$STATE_DIR/last-successful-manifest"

case "$INTERVAL_SECONDS" in
    ''|*[!0-9]*)
        echo "[idp-offsite] ERROR: VITALS_IDP_OFFSITE_INTERVAL_SECONDS must be a positive integer" >&2
        exit 2
        ;;
esac
if [ "$INTERVAL_SECONDS" -lt 1 ]; then
    echo "[idp-offsite] ERROR: VITALS_IDP_OFFSITE_INTERVAL_SECONDS must be at least 1" >&2
    exit 2
fi
case "$RUN_ONCE" in
    true|false) ;;
    *)
        echo "[idp-offsite] ERROR: VITALS_IDP_OFFSITE_RUN_ONCE must be true or false" >&2
        exit 2
        ;;
esac
case "$BACKUP_HOSTNAME" in
    ''|*[!A-Za-z0-9._-]*)
        echo "[idp-offsite] ERROR: VITALS_IDP_OFFSITE_HOSTNAME is invalid" >&2
        exit 2
        ;;
esac

for required_file in "$RESTIC_REPOSITORY_FILE" "$RESTIC_PASSWORD_FILE"; do
    if [ ! -s "$required_file" ]; then
        echo "[idp-offsite] ERROR: a required restic secret file is missing or empty" >&2
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
    read_single_line_secret "$VITALS_IDP_RESTIC_S3_ACCESS_KEY_FILE"
)"; then
    echo "[idp-offsite] ERROR: the S3 access-key secret must be one non-empty line" >&2
    exit 2
fi
if ! AWS_SECRET_ACCESS_KEY="$(
    read_single_line_secret "$VITALS_IDP_RESTIC_S3_SECRET_KEY_FILE"
)"; then
    echo "[idp-offsite] ERROR: the S3 secret-key secret must be one non-empty line" >&2
    exit 2
fi
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

validate_manifest() {
    manifest="$1"
    manifest_name="${manifest##*/}"
    stamp="${manifest_name#zitadel_bundle_}"
    stamp="${stamp%.sha256}"
    case "$stamp" in
        ????????T??????Z) ;;
        *)
            echo "[idp-offsite] ERROR: malformed identity manifest name" >&2
            return 1
            ;;
    esac
    case "$stamp" in
        *[!0-9TZ]*)
            echo "[idp-offsite] ERROR: malformed identity manifest name" >&2
            return 1
            ;;
    esac

    database_name="zitadel_${stamp}.sql.gz"
    actual_names="$(
        awk '
            NF != 2 || length($1) != 64 || $1 ~ /[^0-9a-f]/ { exit 1 }
            $2 ~ /^\// || $2 ~ /(^|\/)\.\.($|\/)/ { exit 1 }
            { print $2 }
        ' "$manifest"
    )" || {
        echo "[idp-offsite] ERROR: malformed identity manifest content" >&2
        return 1
    }
    if [ "$actual_names" != "$database_name" ]; then
        echo "[idp-offsite] ERROR: manifest does not name the exact identity set" >&2
        return 1
    fi
    if ! (cd "$BACKUP_DIR" && sha256sum -c "$manifest_name" >/dev/null); then
        echo "[idp-offsite] ERROR: local identity checksum failed" >&2
        return 1
    fi
}

replicate_latest() {
    if ! manifest="$(
        find "$BACKUP_DIR" -maxdepth 1 -type f \
            -name 'zitadel_bundle_*.sha256' -print | sort | tail -n 1
    )"; then
        echo "[idp-offsite] ERROR: identity-set discovery failed" >&2
        return 1
    fi
    if [ -z "$manifest" ]; then
        echo "[idp-offsite] ERROR: no complete identity recovery set exists" >&2
        return 1
    fi
    manifest_name="${manifest##*/}"
    if ! validate_manifest "$manifest"; then
        return 1
    fi

    if ! restic snapshots --json >/dev/null; then
        echo "[idp-offsite] ERROR: initialized repository preflight failed" >&2
        return 1
    fi
    if ! restic backup \
        --skip-if-unchanged \
        --host "$BACKUP_HOSTNAME" \
        --tag vitals-idp \
        --tag "vitals-idp-bundle:$stamp" \
        "$manifest" \
        "$BACKUP_DIR/$database_name"; then
        echo "[idp-offsite] ERROR: encrypted identity replication failed" >&2
        return 1
    fi

    if ! printf '%s\n' "$manifest_name" > "$MARKER.tmp" \
        || ! chmod 600 "$MARKER.tmp" \
        || ! mv "$MARKER.tmp" "$MARKER"; then
        rm -f "$MARKER.tmp"
        echo "[idp-offsite] ERROR: the replication marker could not be published" >&2
        return 1
    fi
    echo "[idp-offsite] verified encrypted identity set $stamp"
}

while true; do
    replicate_latest
    if [ "$RUN_ONCE" = true ]; then
        exit 0
    fi
    sleep "$INTERVAL_SECONDS"
done
