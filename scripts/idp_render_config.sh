#!/bin/sh
# Render ZITADEL's secret-bearing config into a private named volume. The
# distroless provider image reads it directly and never needs a shell wrapper.

set -eu
umask 077

read_secret() {
    name="$1"
    path="$2"
    value="$(awk 'NR > 1 { exit 1 } { value = $0 } END { if (NR != 1 || value == "") exit 1; printf "%s", value }' "$path")" || {
        echo "[idp-config-render] ERROR: $name must contain exactly one non-empty line" >&2
        exit 2
    }
    case "$value" in
        *[!A-Za-z0-9_.~-]*)
            echo "[idp-config-render] ERROR: $name must use URL-safe characters only" >&2
            exit 2
            ;;
    esac
    printf '%s' "$value"
}

service_password="$(read_secret service-password "$VITALS_IDP_DB_SERVICE_PASSWORD_FILE")"
admin_password="$(read_secret first-admin-password "$VITALS_IDP_ADMIN_PASSWORD_FILE")"
config_dir="${VITALS_IDP_CONFIG_DIR:-/zitadel/config}"
config_owner="${VITALS_IDP_CONFIG_OWNER:-1000:1000}"
case "$config_owner" in
    *[!0-9:]*|:*|*:|*:*:*)
        echo "[idp-config-render] ERROR: config owner must be numeric uid:gid" >&2
        exit 2
        ;;
esac
case "$VITALS_IDP_ADMIN_EMAIL" in
    *[!A-Za-z0-9@._+-]*)
        echo "[idp-config-render] ERROR: first-admin email is not YAML-safe" >&2
        exit 2
        ;;
esac

mkdir -p "$config_dir"
{
    printf '%s\n' 'Database:'
    printf '%s\n' '  postgres:'
    printf '    DSN: postgresql://zitadel:%s@vitals_idp_db:5432/zitadel?sslmode=disable\n' "$service_password"
} > "$config_dir/config.yaml.tmp"
{
    printf '%s\n' 'FirstInstance:'
    printf '%s\n' '  LoginClientPatPath: /zitadel/bootstrap/login-client.pat'
    printf '%s\n' '  Org:'
    printf '%s\n' '    Human:'
    printf '      UserName: %s\n' "$VITALS_IDP_ADMIN_USERNAME"
    printf '%s\n' '      FirstName: Vitals'
    printf '%s\n' '      LastName: Operator'
    printf '%s\n' '      Email:'
    printf '        Address: %s\n' "$VITALS_IDP_ADMIN_EMAIL"
    printf '%s\n' '        Verified: true'
    printf '      Password: %s\n' "$admin_password"
    printf '%s\n' '      PasswordChangeRequired: true'
    printf '%s\n' '    LoginClient:'
    printf '%s\n' '      Machine:'
    printf '%s\n' '        Username: login-client'
    printf '%s\n' '        Name: Vitals Login Client'
    printf '%s\n' '      Pat:'
    printf '        ExpirationDate: %s\n' "$VITALS_IDP_LOGIN_PAT_EXPIRATION"
} > "$config_dir/steps.yaml.tmp"
chown "$config_owner" "$config_dir/config.yaml.tmp" "$config_dir/steps.yaml.tmp"
chmod 400 "$config_dir/config.yaml.tmp" "$config_dir/steps.yaml.tmp"
mv "$config_dir/config.yaml.tmp" "$config_dir/config.yaml"
mv "$config_dir/steps.yaml.tmp" "$config_dir/steps.yaml"
unset service_password admin_password
echo "[idp-config-render] private ZITADEL config rendered"
