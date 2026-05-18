#!/bin/bash
# set-env-secret.sh — Idempotent .env key=value upsert from stdin
#
# Kullanim:
#   echo "secret-value" | bash set-env-secret.sh KEY_NAME
#
# Yoksa ekler, varsa update eder. Tek satira sigar (\n strip).
# Stdout: "updated: KEY" veya "appended: KEY"
# Stderr: hata

set -euo pipefail

KEY="${1:-}"
if [ -z "$KEY" ]; then
    echo "ERROR: usage: $0 <KEY>" >&2
    exit 2
fi

# Key isim valid (uppercase + underscore + digit only)
if ! [[ "$KEY" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
    echo "ERROR: invalid key '$KEY' (must match ^[A-Z_][A-Z0-9_]*$)" >&2
    exit 3
fi

VAL=$(cat)
# Strip trailing newlines + carriage returns
VAL=$(printf '%s' "$VAL" | tr -d '\r' | sed -e 's/[[:space:]]*$//')

if [ -z "$VAL" ]; then
    echo "ERROR: empty value on stdin" >&2
    exit 4
fi

ENV_FILE="${ENV_FILE:-/opt/linux-ai-server/.env}"

# sed escape (& | \ in value)
VAL_SED=$(printf '%s' "$VAL" | sed -e 's/[&|\\]/\\&/g')

if grep -q "^${KEY}=" "$ENV_FILE"; then
    sed -i "s|^${KEY}=.*|${KEY}=${VAL_SED}|" "$ENV_FILE"
    echo "updated: $KEY (len=${#VAL})"
else
    printf '%s=%s\n' "$KEY" "$VAL" >> "$ENV_FILE"
    echo "appended: $KEY (len=${#VAL})"
fi
