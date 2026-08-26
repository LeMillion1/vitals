#!/bin/sh
# Restore one exact ZITADEL SQL+Login-PAT bundle into fresh identity volumes.
# This primitive intentionally refuses any non-empty target database or an
# existing bootstrap PAT; the caller owns scratch-project lifecycle and cleanup.

set -eu
if (set -o pipefail) 2>/dev/null; then
    set -o pipefail
else
    echo "[idp-restore] ERROR: /bin/sh must support pipefail" >&2
    exit 2
fi
umask 077

BACKUP_DIR="${VITALS_IDP_BACKUP_DIR:-/backups/idp}"
BOOTSTRAP_DIR="${VITALS_IDP_BOOTSTRAP_DIR:-/zitadel/bootstrap}"
MANIFEST_NAME="${VITALS_IDP_RESTORE_MANIFEST:-}"
DB_PASSWORD_FILE="${VITALS_IDP_DB_PASSWORD_FILE:-}"
BOOTSTRAP_OWNER="${VITALS_IDP_BOOTSTRAP_OWNER:-1000:65533}"
case "$BOOTSTRAP_OWNER" in
    *[!0-9:]*|:*|*:|*:*:*)
        echo "[idp-restore] ERROR: bootstrap owner must be numeric uid:gid" >&2
        exit 2
        ;;
esac

case "$MANIFEST_NAME" in
    zitadel_bundle_????????T??????Z.sha256) ;;
    *)
        echo "[idp-restore] ERROR: select one exact identity manifest filename" >&2
        exit 2
        ;;
esac
case "$MANIFEST_NAME" in
    *[!A-Za-z0-9_.-]*)
        echo "[idp-restore] ERROR: identity manifest filename is invalid" >&2
        exit 2
        ;;
esac
manifest="$BACKUP_DIR/$MANIFEST_NAME"
if [ ! -f "$manifest" ] || [ -L "$manifest" ]; then
    echo "[idp-restore] ERROR: selected identity manifest is missing or unsafe" >&2
    exit 2
fi

stamp="${MANIFEST_NAME#zitadel_bundle_}"
stamp="${stamp%.sha256}"
case "$stamp" in
    *[!0-9TZ]*)
        echo "[idp-restore] ERROR: identity manifest timestamp is invalid" >&2
        exit 2
        ;;
esac
database_name="zitadel_${stamp}.sql.gz"
bootstrap_name="zitadel_login_client_${stamp}.pat"
actual_names="$(
    awk '
        NF != 2 || length($1) != 64 || $1 ~ /[^0-9a-f]/ { exit 1 }
        $2 ~ /^\// || $2 ~ /(^|\/)\.\.($|\/)/ { exit 1 }
        { print $2 }
    ' "$manifest"
)" || {
    echo "[idp-restore] ERROR: identity manifest content is malformed" >&2
    exit 2
}
expected_names="$(printf '%s\n%s' "$database_name" "$bootstrap_name")"
if [ "$actual_names" != "$expected_names" ]; then
    echo "[idp-restore] ERROR: manifest does not name the exact identity set" >&2
    exit 2
fi
bootstrap_digest="$(awk 'NR == 2 { print $1 }' "$manifest")"
for artifact in "$BACKUP_DIR/$database_name" "$BACKUP_DIR/$bootstrap_name"; do
    if [ ! -f "$artifact" ] || [ -L "$artifact" ] || [ ! -s "$artifact" ]; then
        echo "[idp-restore] ERROR: identity artifact is missing or unsafe" >&2
        exit 2
    fi
done
if ! (cd "$BACKUP_DIR" && sha256sum -c "$MANIFEST_NAME" >/dev/null); then
    echo "[idp-restore] ERROR: identity bundle checksum failed" >&2
    exit 2
fi

if [ -z "$DB_PASSWORD_FILE" ] || [ ! -f "$DB_PASSWORD_FILE" ] \
    || [ -L "$DB_PASSWORD_FILE" ] || [ ! -s "$DB_PASSWORD_FILE" ]; then
    echo "[idp-restore] ERROR: database password secret is missing or invalid" >&2
    exit 2
fi
if ! PGPASSWORD="$(awk 'NR > 1 { exit 1 } { value = $0 } END { if (NR != 1 || value == "") exit 1; printf "%s", value }' "$DB_PASSWORD_FILE")"; then
    echo "[idp-restore] ERROR: database password secret must be one non-empty line" >&2
    exit 2
fi
export PGPASSWORD

table_count="$(
    psql -X -A -t -v ON_ERROR_STOP=1 -c \
        "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
)" || {
    echo "[idp-restore] ERROR: target database readiness check failed" >&2
    exit 1
}
case "$table_count" in
    0) ;;
    *)
        echo "[idp-restore] ERROR: target identity database is not empty" >&2
        exit 2
        ;;
esac
if [ -n "$(find "$BOOTSTRAP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    echo "[idp-restore] ERROR: target identity bootstrap is not empty" >&2
    exit 2
fi

cp "$BACKUP_DIR/$bootstrap_name" "$BOOTSTRAP_DIR/login-client.pat.tmp"
chmod 600 "$BOOTSTRAP_DIR/login-client.pat.tmp"
if ! gzip -dc "$BACKUP_DIR/$database_name" \
    | psql -X -1 -v ON_ERROR_STOP=1; then
    rm -f "$BOOTSTRAP_DIR/login-client.pat.tmp"
    echo "[idp-restore] ERROR: atomic identity database restore failed" >&2
    exit 1
fi
if ! (cd "$BACKUP_DIR" && sha256sum -c "$MANIFEST_NAME" >/dev/null) \
    || [ "$(sha256sum "$BOOTSTRAP_DIR/login-client.pat.tmp" | awk '{ print $1 }')" != "$bootstrap_digest" ]; then
    rm -f "$BOOTSTRAP_DIR/login-client.pat.tmp"
    echo "[idp-restore] ERROR: identity bundle changed during restore" >&2
    exit 1
fi
mv "$BOOTSTRAP_DIR/login-client.pat.tmp" "$BOOTSTRAP_DIR/login-client.pat"
chown "$BOOTSTRAP_OWNER" "$BOOTSTRAP_DIR/login-client.pat" "$BOOTSTRAP_DIR"
chmod 640 "$BOOTSTRAP_DIR/login-client.pat"
chmod 750 "$BOOTSTRAP_DIR"
unset PGPASSWORD
echo "[idp-restore] restored identity bundle $stamp into fresh targets"
