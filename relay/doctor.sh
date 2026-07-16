#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${HERDR_CONFIG_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/herdr-remote/config.env}"
failures=0
warnings=0

pass() { printf '  [ok]   %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; warnings=$((warnings + 1)); }
fail() { printf '  [fail] %s\n' "$1"; failures=$((failures + 1)); }

if [ "${1:-}" = "--help" ]; then
    echo "Usage: $0"
    echo "Check Herdr, relay security, service, tunnel, Push, and WSL startup health."
    exit 0
elif [ -n "${1:-}" ]; then
    echo "Usage: $0" >&2
    exit 2
fi

echo "herdr-remote doctor"
echo "==================="

if grep -qi microsoft /proc/version 2>/dev/null; then
    pass "WSL detected"
else
    warn "WSL not detected; Linux checks still apply"
fi

if [ ! -r "$CONFIG_FILE" ]; then
    fail "Config is missing or unreadable: $CONFIG_FILE"
    echo
    echo "Result: $failures failure(s), $warnings warning(s)"
    exit 1
fi

config_mode="$(stat -c '%a' "$CONFIG_FILE" 2>/dev/null || stat -f '%Lp' "$CONFIG_FILE" 2>/dev/null || true)"
if [ "$config_mode" = "600" ]; then
    pass "Config permissions are 0600"
else
    fail "Config permissions are ${config_mode:-unknown}; expected 600"
fi

set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
set +a

relay_token="${HERDR_RELAY_TOKEN:-}"

case "${HERDR_RELAY_BIND:-}" in
    127.0.0.1|localhost|::1) pass "Relay bind is loopback-only (${HERDR_RELAY_BIND})" ;;
    *) fail "Relay bind is not loopback-only (${HERDR_RELAY_BIND:-unset})" ;;
esac

if [ "${#relay_token}" -ge 43 ]; then
    pass "Admin authentication token is configured"
else
    fail "Admin authentication token is missing or too short"
fi

if [ -x "${HERDR_UV_PATH:-}" ] && "$HERDR_UV_PATH" --version >/dev/null 2>&1; then
    pass "uv is executable (${HERDR_UV_PATH})"
else
    fail "Configured uv is unavailable (${HERDR_UV_PATH:-unset})"
fi

if [ -x "${HERDR_BIN:-}" ] && herdr_version="$($HERDR_BIN --version 2>/dev/null)"; then
    case "$herdr_version" in
        "herdr 0."[0-6].*) fail "Herdr 0.7+ is required ($herdr_version)" ;;
        *) pass "Herdr CLI is available ($herdr_version)" ;;
    esac
else
    fail "Configured Herdr binary is unavailable (${HERDR_BIN:-unset})"
fi

service_running=false
if command -v systemctl >/dev/null 2>&1 && systemctl --user is-active herdr-relay.service >/dev/null 2>&1; then
    service_running=true
    pass "systemd user relay service is active"
    if systemctl --user is-enabled herdr-relay.service >/dev/null 2>&1; then
        pass "systemd user relay service is enabled"
    else
        warn "systemd user relay service is not enabled"
    fi
elif HOME="$HOME" "$SCRIPT_DIR/service.sh" status >/dev/null 2>&1; then
    service_running=true
    pass "fallback relay service is active"
else
    fail "relay service is not running"
fi

health=""
if command -v curl >/dev/null 2>&1; then
    health="$(curl -fsS --max-time 5 \
        -H "Authorization: Bearer $relay_token" \
        "http://127.0.0.1:${HERDR_RELAY_PORT:-8375}/api/health" 2>/dev/null || true)"
fi
if [ -n "$health" ] && HEALTH_JSON="$health" python3 -c 'import json,os; data=json.loads(os.environ["HEALTH_JSON"]); assert data["relay"] == "ok"' >/dev/null 2>&1; then
    pass "authenticated relay health endpoint is responding"
    health_summary="$(HEALTH_JSON="$health" python3 -c 'import json,os; d=json.loads(os.environ["HEALTH_JSON"]); print(f"Herdr={d.get('"'"'herdr'"'"')}/{d.get('"'"'herdrPoll'"'"')} agents={d.get('"'"'agents'"'"')} clients={d.get('"'"'clients'"'"')}/{d.get('"'"'maxClients'"'"')} proxy={d.get('"'"'proxy'"'"')} push={d.get('"'"'push'"'"')} devices={d.get('"'"'pairedDevices'"'"')}")')"
    printf '         %s\n' "$health_summary"
else
    if [ "$service_running" = true ]; then
        fail "relay service is active but authenticated health is unavailable"
    else
        warn "health check skipped because relay is stopped"
    fi
fi

case "${HERDR_TUNNEL_MODE:-none}" in
    tailscale)
        if command -v tailscale >/dev/null 2>&1 && tailscale serve status 2>/dev/null | grep -q "127.0.0.1:${HERDR_RELAY_PORT:-8375}"; then
            pass "Tailscale Serve routes to the loopback relay"
        else
            fail "Tailscale mode is configured but Serve is not routing to the relay"
        fi
        ;;
    named|temp)
        if systemctl --user is-active herdr-tunnel.service >/dev/null 2>&1; then
            pass "Cloudflare tunnel service is active"
        else
            fail "Cloudflare tunnel mode is configured but its service is inactive"
        fi
        ;;
    none) warn "No remote-access tunnel is configured" ;;
    *) warn "Unknown tunnel mode: ${HERDR_TUNNEL_MODE}" ;;
esac

if grep -qi microsoft /proc/version 2>/dev/null && command -v cmd.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
    appdata_win="$(cmd.exe /C echo %APPDATA% 2>/dev/null | tr -d '\r' | tail -1)"
    appdata_wsl="$(wslpath -u "$appdata_win" 2>/dev/null || true)"
    if [ -f "$appdata_wsl/Microsoft/Windows/Start Menu/Programs/Startup/herdr-remote-wsl.cmd" ]; then
        pass "Windows login startup command is installed"
    else
        warn "Windows login startup command was not found"
    fi
fi

echo
echo "Windows must remain awake, and WSL plus the relay must remain running for remote access."
echo "Result: $failures failure(s), $warnings warning(s)"
[ "$failures" -eq 0 ]
