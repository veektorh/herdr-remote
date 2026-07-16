#!/bin/sh
set -eu

DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
HOME_DIR="$TMP_DIR/home"
CONFIG_FILE="$HOME_DIR/.config/herdr-remote/config.env"
FAKE_UV="$TMP_DIR/uv"
trap 'HOME="$HOME_DIR" "$DIR/relay/service.sh" stop >/dev/null 2>&1 || true; rm -rf "$TMP_DIR"' EXIT

cat > "$FAKE_UV" <<'EOF'
#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    echo "uv 0.test"
    exit 0
fi
if [ "${1:-}" = "run" ]; then
    exec sleep 30
fi
exit 2
EOF
chmod 700 "$FAKE_UV"

[ "$("$DIR/relay/install-service.sh" --print-cloudflared-download-url x86_64)" = \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" ]
[ "$("$DIR/relay/install-service.sh" --print-cloudflared-download-url aarch64)" = \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64" ]
if "$DIR/relay/install-service.sh" --print-cloudflared-download-url mips64 >/dev/null 2>&1; then
    echo "Expected unsupported cloudflared architecture to fail" >&2
    exit 1
fi
grep -q 'tunnel token --cred-file' "$DIR/relay/install-service.sh"
grep -q 'credentials-file: \$CF_CREDENTIALS' "$DIR/relay/install-service.sh"
grep -q 'service: http://127.0.0.1:\$WS_PORT' "$DIR/relay/install-service.sh"
grep -q "Type 'reuse \$TUNNEL_NAME'" "$DIR/relay/install-service.sh"
grep -q 'route dns --overwrite-dns' "$DIR/relay/install-service.sh"
grep -q 'serve --bg --yes' "$DIR/relay/install-service.sh"
grep -q 'Reusing existing Tailscale Serve route' "$DIR/relay/install-service.sh"
grep -q 'subprotocols=\["herdr-v1"\]' "$DIR/relay/install-service.sh"
grep -q 'TUNNEL_MODE="tailscale"' "$DIR/relay/install-service.sh"
bash -n "$DIR/relay/create-pairing.sh"
"$DIR/relay/create-pairing.sh" --help | grep -q '^Usage:'
[ -f "$DIR/windows/install-wsl-startup.ps1" ]
grep -q 'New-ScheduledTaskTrigger -AtLogOn' "$DIR/windows/install-wsl-startup.ps1"
grep -q 'GetFolderPath("Startup")' "$DIR/windows/install-wsl-startup.ps1"
grep -q 'HERDR_RELAY_DIR/service.sh' "$DIR/windows/install-wsl-startup.ps1"

HERDR_CONFIG_FILE="$CONFIG_FILE" \
HERDR_RELAY_DIR="/tmp/herdr relay" \
HERDR_UV_PATH="$FAKE_UV" \
HERDR_BIN="/home/test/.local/bin/herdr" \
HERDR_LOG_DIR="$TMP_DIR/log" \
    "$DIR/relay/write-config.sh"

MODE="$(stat -c '%a' "$CONFIG_FILE")"
[ "$MODE" = "600" ]
grep -q '^HERDR_RELAY_BIND=127.0.0.1$' "$CONFIG_FILE"
TOKEN_ONE="$(sed -n 's/^HERDR_RELAY_TOKEN=//p' "$CONFIG_FILE")"
[ "${#TOKEN_ONE}" -ge 43 ]

HERDR_CONFIG_FILE="$CONFIG_FILE" \
HERDR_RELAY_DIR="/tmp/herdr relay" \
HERDR_UV_PATH="$FAKE_UV" \
HERDR_BIN="/home/test/.local/bin/herdr" \
HERDR_LOG_DIR="$TMP_DIR/log" \
    "$DIR/relay/write-config.sh"

TOKEN_TWO="$(sed -n 's/^HERDR_RELAY_TOKEN=//p' "$CONFIG_FILE")"
[ "$TOKEN_ONE" = "$TOKEN_TWO" ]
grep -q 'HERDR_RELAY_DIR=/tmp/herdr\\ relay' "$CONFIG_FILE"
grep -q '^HERDR_MAX_CLIENTS=16$' "$CONFIG_FILE"

ROTATE_OUTPUT="$(HERDR_CONFIG_FILE="$CONFIG_FILE" "$DIR/relay/rotate-token.sh" --no-restart)"
TOKEN_THREE="$(sed -n 's/^HERDR_RELAY_TOKEN=//p' "$CONFIG_FILE")"
[ "$TOKEN_TWO" != "$TOKEN_THREE" ]
[ "$(stat -c '%a' "$CONFIG_FILE")" = "600" ]
case "$ROTATE_OUTPUT" in *"$TOKEN_THREE"*) echo "Rotated token was printed" >&2; exit 1 ;; esac

HOME="$HOME_DIR" "$DIR/relay/service.sh" start >/dev/null
HOME="$HOME_DIR" "$DIR/relay/service.sh" status >/dev/null
HOME="$HOME_DIR" "$DIR/relay/service.sh" stop >/dev/null
