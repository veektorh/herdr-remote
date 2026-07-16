#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${HERDR_CONFIG_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/herdr-remote/config.env}"
restart=true

if [ "${1:-}" = "--no-restart" ]; then
    restart=false
elif [ -n "${1:-}" ]; then
    echo "Usage: $0 [--no-restart]" >&2
    exit 2
fi

if [ ! -r "$CONFIG_FILE" ]; then
    echo "Error: Config not found at $CONFIG_FILE" >&2
    echo "Run install-service.sh first." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
set +a

if command -v openssl >/dev/null 2>&1; then
    HERDR_RELAY_TOKEN="$(openssl rand -hex 32)"
else
    HERDR_RELAY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi
export HERDR_RELAY_TOKEN
HERDR_CONFIG_FILE="$CONFIG_FILE" "$SCRIPT_DIR/write-config.sh"

if [ "$restart" = true ]; then
    if command -v systemctl >/dev/null 2>&1 && systemctl --user is-enabled herdr-relay.service >/dev/null 2>&1; then
        systemctl --user restart herdr-relay.service
        echo "Admin token rotated and relay service restarted."
        exit 0
    fi
    if "$SCRIPT_DIR/service.sh" status >/dev/null 2>&1; then
        "$SCRIPT_DIR/service.sh" restart
        echo "Admin token rotated and fallback relay restarted."
        exit 0
    fi
fi

echo "Admin token rotated in $CONFIG_FILE. Restart the relay before remote use."
