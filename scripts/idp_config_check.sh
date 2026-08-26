#!/bin/sh
# Fail closed before any self-hosted identity-provider state is created.

set -eu

read_secret() {
    name="$1"
    path="$2"
    if [ ! -f "$path" ] || [ -L "$path" ] || [ ! -s "$path" ]; then
        echo "[idp-config] ERROR: $name must be a non-empty regular secret file" >&2
        exit 2
    fi
    value="$(awk 'NR > 1 { exit 1 } { value = $0 } END { if (NR != 1 || value == "") exit 1; printf "%s", value }' "$path")" || {
        echo "[idp-config] ERROR: $name must contain exactly one non-empty line" >&2
        exit 2
    }
    printf '%s' "$value"
}

masterkey="$(read_secret VITALS_IDP_MASTERKEY_FILE "$VITALS_IDP_MASTERKEY_FILE")"
db_admin="$(read_secret VITALS_IDP_DB_ADMIN_PASSWORD_FILE "$VITALS_IDP_DB_ADMIN_PASSWORD_FILE")"
db_service="$(read_secret VITALS_IDP_DB_SERVICE_PASSWORD_FILE "$VITALS_IDP_DB_SERVICE_PASSWORD_FILE")"
db_backup="$(read_secret VITALS_IDP_DB_BACKUP_PASSWORD_FILE "$VITALS_IDP_DB_BACKUP_PASSWORD_FILE")"
admin_password="$(read_secret VITALS_IDP_ADMIN_PASSWORD_FILE "$VITALS_IDP_ADMIN_PASSWORD_FILE")"

masterkey_bytes="$(wc -c < "$VITALS_IDP_MASTERKEY_FILE" | tr -d ' ')"
if [ "${#masterkey}" -ne 32 ] || [ "$masterkey_bytes" -ne 32 ]; then
    echo "[idp-config] ERROR: the ZITADEL master-key file must contain exactly 32 bytes with no newline" >&2
    exit 2
fi
for value in "$db_admin" "$db_service" "$db_backup" "$admin_password"; do
    case "$value" in
        *[!A-Za-z0-9_.~-]*)
            echo "[idp-config] ERROR: generated passwords must use URL-safe characters only" >&2
            exit 2
            ;;
    esac
done
for value in "$db_admin" "$db_service" "$db_backup"; do
    if [ "${#value}" -lt 24 ]; then
        echo "[idp-config] ERROR: database passwords must contain at least 24 characters" >&2
        exit 2
    fi
done
if [ "${#admin_password}" -lt 12 ]; then
    echo "[idp-config] ERROR: the first administrator password is too short" >&2
    exit 2
fi
case "$admin_password" in *[A-Z]*) ;; *) echo "[idp-config] ERROR: the first administrator password needs uppercase" >&2; exit 2 ;; esac
case "$admin_password" in *[a-z]*) ;; *) echo "[idp-config] ERROR: the first administrator password needs lowercase" >&2; exit 2 ;; esac
case "$admin_password" in *[0-9]*) ;; *) echo "[idp-config] ERROR: the first administrator password needs a number" >&2; exit 2 ;; esac
case "$admin_password" in *[._~-]*) ;; *) echo "[idp-config] ERROR: the first administrator password needs a symbol" >&2; exit 2 ;; esac
if [ "$db_admin" = "$db_service" ] || [ "$db_admin" = "$db_backup" ] \
    || [ "$db_service" = "$db_backup" ]; then
    echo "[idp-config] ERROR: database admin, service, and backup passwords must differ" >&2
    exit 2
fi

case "${VITALS_IDP_DOMAIN:-}" in
    ''|*[!A-Za-z0-9.-]*|.*|*..*|*.)
        echo "[idp-config] ERROR: VITALS_IDP_DOMAIN must be a plain DNS hostname" >&2
        exit 2
        ;;
esac
case "${VITALS_IDP_PUBLIC_SCHEME:-}" in
    http|https) ;;
    *)
        echo "[idp-config] ERROR: VITALS_IDP_PUBLIC_SCHEME must be http or https" >&2
        exit 2
        ;;
esac
case "${VITALS_IDP_SECURE:-}" in
    true|false) ;;
    *)
        echo "[idp-config] ERROR: VITALS_IDP_SECURE must be true or false" >&2
        exit 2
        ;;
esac
if { [ "$VITALS_IDP_PUBLIC_SCHEME" = https ] && [ "$VITALS_IDP_SECURE" != true ]; } \
    || { [ "$VITALS_IDP_PUBLIC_SCHEME" = http ] && [ "$VITALS_IDP_SECURE" != false ]; }; then
    echo "[idp-config] ERROR: public scheme and external-secure flag disagree" >&2
    exit 2
fi
for pair in \
    "VITALS_IDP_EXTERNAL_PORT:${VITALS_IDP_EXTERNAL_PORT:-}" \
    "VITALS_IDP_ORIGIN_PORT:${VITALS_IDP_ORIGIN_PORT:-}"; do
    name="${pair%%:*}"
    value="${pair#*:}"
    case "$value" in
        ''|*[!0-9]*)
            echo "[idp-config] ERROR: $name must be a valid TCP port" >&2
            exit 2
            ;;
    esac
    if [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
        echo "[idp-config] ERROR: $name must be a valid TCP port" >&2
        exit 2
    fi
done
if [ "$VITALS_IDP_DOMAIN" != localhost ]; then
    if [ "$VITALS_IDP_PUBLIC_SCHEME" != https ] || [ "$VITALS_IDP_EXTERNAL_PORT" -ne 443 ]; then
        echo "[idp-config] ERROR: a non-local identity provider must use public HTTPS on port 443" >&2
        exit 2
    fi
fi
expected_authority="$VITALS_IDP_DOMAIN:$VITALS_IDP_EXTERNAL_PORT"
if { [ "$VITALS_IDP_PUBLIC_SCHEME" = https ] && [ "$VITALS_IDP_EXTERNAL_PORT" -eq 443 ]; } \
    || { [ "$VITALS_IDP_PUBLIC_SCHEME" = http ] && [ "$VITALS_IDP_EXTERNAL_PORT" -eq 80 ]; }; then
    expected_authority="$VITALS_IDP_DOMAIN"
fi
if [ "${VITALS_IDP_PUBLIC_AUTHORITY:-}" != "$expected_authority" ]; then
    echo "[idp-config] ERROR: VITALS_IDP_PUBLIC_AUTHORITY must be the canonical public authority $expected_authority" >&2
    exit 2
fi
case "${VITALS_IDP_ADMIN_USERNAME:-}" in
    ''|*[!A-Za-z0-9._-]*)
        echo "[idp-config] ERROR: VITALS_IDP_ADMIN_USERNAME is invalid" >&2
        exit 2
        ;;
esac
case "${VITALS_IDP_ADMIN_EMAIL:-}" in
    ?*@?*.?*) ;;
    *)
        echo "[idp-config] ERROR: VITALS_IDP_ADMIN_EMAIL is invalid" >&2
        exit 2
        ;;
esac
case "${VITALS_IDP_LOGIN_PAT_EXPIRATION:-}" in
    20??-??-??T??:??:??Z) ;;
    *)
        echo "[idp-config] ERROR: VITALS_IDP_LOGIN_PAT_EXPIRATION must be an explicit UTC timestamp" >&2
        exit 2
        ;;
esac
if ! awk -v expiry="$VITALS_IDP_LOGIN_PAT_EXPIRATION" \
    -v now="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    'BEGIN { exit !(expiry > now) }'; then
    echo "[idp-config] ERROR: Login V2 PAT expiration must be in the future" >&2
    exit 2
fi

echo "[idp-config] production-like ZITADEL configuration accepted"
