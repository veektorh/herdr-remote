#!/bin/bash
set -euo pipefail
umask 077

CONFIG_FILE="${HERDR_CONFIG_FILE:-$HOME/.config/herdr-remote/config.env}"

usage() {
    echo "Usage: $0 [https://relay-host]"
    echo "Creates a single-use pairing code using the protected relay admin token."
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    exit
fi

if [ ! -r "$CONFIG_FILE" ]; then
    echo "Error: Relay config is not readable: $CONFIG_FILE" >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"
if [ -z "${HERDR_RELAY_TOKEN:-}" ]; then
    echo "Error: HERDR_RELAY_TOKEN is missing from $CONFIG_FILE" >&2
    exit 1
fi

BASE_URL="${1:-${HERDR_PUBLIC_URL:-}}"
if [ -z "$BASE_URL" ] && command -v tailscale >/dev/null 2>&1; then
    BASE_URL="$(tailscale serve status 2>/dev/null | awk '/^https:\/\// { print $1; exit }')"
fi
if [ -z "$BASE_URL" ]; then
    echo "Error: No public or tailnet URL found." >&2
    echo "Pass it explicitly, for example: $0 https://host.tailnet.ts.net" >&2
    exit 1
fi
BASE_URL="${BASE_URL%/}"

RESPONSE="$(curl -fsS --max-time 10 \
    -H "Authorization: Bearer $HERDR_RELAY_TOKEN" \
    "$BASE_URL/api/pair/start")"

python3 -c 'import json, sys
data = json.load(sys.stdin)
print("Code:", data["code"])
print("Pairing link:", data["pairUrl"])
print("Expires in:", data["expiresIn"], "seconds")' <<<"$RESPONSE"
