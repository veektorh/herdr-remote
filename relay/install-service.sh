#!/bin/bash
set -e
umask 077

LABEL_RELAY="com.herdr-remote.relay"
LABEL_TUNNEL="com.herdr-remote.tunnel"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/herdr-remote"
CONFIG_FILE="$CONFIG_DIR/config.env"
WS_PORT="${HERDR_RELAY_PORT:-8375}"

# --- Detect OS ---

detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)  echo "linux" ;;
        *)      echo "unsupported" ;;
    esac
}

cloudflared_download_url() {
    local os="$1"
    local arch="$2"
    local asset_arch=""

    if [ "$os" != "linux" ]; then
        echo "Error: Automatic cloudflared binary installation requires Linux or Homebrew on macOS." >&2
        return 1
    fi

    case "$arch" in
        x86_64|amd64) asset_arch="amd64" ;;
        aarch64|arm64) asset_arch="arm64" ;;
        armv7l|armv6l) asset_arch="arm" ;;
        i386|i686)     asset_arch="386" ;;
        *)
            echo "Error: Unsupported cloudflared architecture: $arch" >&2
            return 1
            ;;
    esac

    echo "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$asset_arch"
}

route_tunnel_dns() {
    local tunnel_name="$1"
    local hostname="$2"

    echo "  Routing DNS: $hostname -> $tunnel_name"
    if "$CLOUDFLARED_PATH" tunnel route dns "$tunnel_name" "$hostname"; then
        return
    fi

    echo "  A DNS record for $hostname already exists."
    read -p "  Replace it with this tunnel route? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "  Error: DNS was not routed to tunnel '$tunnel_name'."
        return 1
    fi

    "$CLOUDFLARED_PATH" tunnel route dns --overwrite-dns "$tunnel_name" "$hostname"
}

OS="$(detect_os)"
if [ "$OS" = "unsupported" ]; then
    echo "Error: Unsupported OS ($(uname -s)). Only macOS and Linux are supported."
    exit 1
fi

if [ "${1:-}" = "--print-cloudflared-download-url" ]; then
    cloudflared_download_url "$OS" "${2:-$(uname -m)}"
    exit
fi

# --- Log directory (matches relay's _get_log_dir) ---

if [ "$OS" = "macos" ]; then
    LOG_DIR="$HOME/Library/Logs/herdr-remote"
elif [ -d "/var/log" ] && [ -w "/var/log" ]; then
    LOG_DIR="/var/log/herdr-remote"
else
    LOG_DIR="$HOME/.local/state/herdr-remote/log"
fi
mkdir -p "$LOG_DIR"

# --- Detect binaries ---

find_binary() {
    local name="$1"
    local found=""

    # 1. Already in PATH
    found="$(command -v "$name" 2>/dev/null || true)"
    if [ -n "$found" ] && [ -x "$found" ] && "$found" --version >/dev/null 2>&1; then
        echo "$found"
        return
    fi

    # 2. Homebrew (macOS Apple Silicon + Intel)
    for prefix in /opt/homebrew/bin /usr/local/bin; do
        if [ -x "$prefix/$name" ] && "$prefix/$name" --version >/dev/null 2>&1; then
            echo "$prefix/$name"
            return
        fi
    done

    # 3. Cargo
    if [ -x "$HOME/.cargo/bin/$name" ] && "$HOME/.cargo/bin/$name" --version >/dev/null 2>&1; then
        echo "$HOME/.cargo/bin/$name"
        return
    fi

    # 4. Common locations
    for dir in "$HOME/.local/bin" "$HOME/bin" /usr/bin; do
        if [ -x "$dir/$name" ] && "$dir/$name" --version >/dev/null 2>&1; then
            echo "$dir/$name"
            return
        fi
    done

    echo ""
}

UV_PATH="$(find_binary uv)"
HERDR_PATH="$(find_binary herdr)"
HERDR_PUSH_PATH="$(find_binary herdr-push)"
CLOUDFLARED_PATH="$(find_binary cloudflared)"
TAILSCALE_PATH="$(find_binary tailscale)"

echo "herdr-remote relay installer"
echo "============================"
echo ""
echo "  OS:          $OS"
echo "  uv:          ${UV_PATH:-NOT FOUND}"
echo "  herdr:       ${HERDR_PATH:-NOT FOUND}"
echo "  herdr-push:  ${HERDR_PUSH_PATH:-NOT FOUND}"
echo "  cloudflared: ${CLOUDFLARED_PATH:-NOT FOUND}"
echo "  tailscale:   ${TAILSCALE_PATH:-NOT FOUND}"
echo "  relay:       $SCRIPT_DIR/herdr_relay.py"
echo "  config:      $CONFIG_FILE"
echo "  logs:        $LOG_DIR/"
echo "  port:        $WS_PORT"
echo ""

if [ -z "$UV_PATH" ]; then
    echo "Error: uv not found."
    echo "Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

if [ -z "$HERDR_PATH" ]; then
    echo "Warning: herdr binary not found. The relay needs it to poll agents."
    echo "Install options:"
    echo "  brew install herdr"
    echo "  cargo install herdr"
    echo "  curl -fsSL https://herdr.dev/install.sh | sh"
    echo ""
    read -p "Continue anyway? [y/N] " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
fi

# --- Handle --uninstall ---

if [ "$1" = "--uninstall" ]; then
    echo "Uninstalling herdr-remote services..."
    if [ "$OS" = "macos" ]; then
        launchctl bootout "gui/$(id -u)/$LABEL_RELAY" 2>/dev/null || true
        launchctl bootout "gui/$(id -u)/$LABEL_TUNNEL" 2>/dev/null || true
        rm -f "$HOME/Library/LaunchAgents/$LABEL_RELAY.plist"
        rm -f "$HOME/Library/LaunchAgents/$LABEL_TUNNEL.plist"
    else
        "$SCRIPT_DIR/service.sh" stop 2>/dev/null || true
        if [ -r "$CONFIG_DIR/tunnel.pid" ]; then
            kill "$(cat "$CONFIG_DIR/tunnel.pid")" 2>/dev/null || true
            rm -f "$CONFIG_DIR/tunnel.pid"
        fi
        systemctl --user stop herdr-relay.service 2>/dev/null || true
        systemctl --user stop herdr-tunnel.service 2>/dev/null || true
        systemctl --user disable herdr-relay.service 2>/dev/null || true
        systemctl --user disable herdr-tunnel.service 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/herdr-relay.service"
        rm -f "$HOME/.config/systemd/user/herdr-tunnel.service"
        systemctl --user daemon-reload 2>/dev/null || true
    fi
    echo "Done. Config preserved at $CONFIG_FILE"
    exit 0
fi

# --- Secure remote access ---

TUNNEL_MODE="none"
REMOTE_ACCESS="cloudflare"

if [ -n "$TAILSCALE_PATH" ]; then
    echo "Remote access"
    echo "-------------"
    echo "  1) tailscale  — private tailnet HTTPS (recommended)"
    echo "  2) cloudflare — public hostname; requires Cloudflare Access"
    echo "  3) none       — local access only"
    echo ""
    read -p "  Remote access [1/2/3, default 1]: " -n 1 -r REMOTE_CHOICE
    echo
    case "$REMOTE_CHOICE" in
        2) REMOTE_ACCESS="cloudflare" ;;
        3) REMOTE_ACCESS="none" ;;
        *) REMOTE_ACCESS="tailscale" ;;
    esac
fi

if [ "$REMOTE_ACCESS" = "tailscale" ]; then
    echo ""
    echo "Tailscale Serve"
    echo "---------------"
    if ! "$TAILSCALE_PATH" status >/dev/null 2>&1; then
        echo "  Error: Tailscale is installed but not connected. Run: sudo tailscale up" >&2
        exit 1
    fi
    TAILSCALE_STATUS="$("$TAILSCALE_PATH" serve status 2>/dev/null || true)"
    if echo "$TAILSCALE_STATUS" | grep -q "127.0.0.1:$WS_PORT"; then
        echo "  Reusing existing Tailscale Serve route."
    elif ! "$TAILSCALE_PATH" serve --bg --yes "http://127.0.0.1:$WS_PORT"; then
        if ! command -v sudo >/dev/null 2>&1; then
            echo "  Error: Tailscale Serve requires elevated permission and sudo is unavailable." >&2
            exit 1
        fi
        echo "  Retrying Tailscale Serve with sudo..."
        sudo "$TAILSCALE_PATH" serve --bg --yes "http://127.0.0.1:$WS_PORT"
    fi
    TAILSCALE_URL=$("$TAILSCALE_PATH" serve status 2>/dev/null | awk '/^https:\/\// { print $1; exit }')
    if [ -z "$TAILSCALE_URL" ]; then
        echo "  Error: Tailscale Serve did not report an HTTPS URL." >&2
        exit 1
    fi
    TUNNEL_MODE="tailscale"
    HERDR_PUBLIC_URL="$TAILSCALE_URL"
    echo "  Tailnet-only URL: $TAILSCALE_URL"
elif [ "$REMOTE_ACCESS" = "none" ]; then
    echo "  Remote access disabled; relay remains on loopback."
fi

if [ "$REMOTE_ACCESS" = "cloudflare" ]; then

if [ -z "$CLOUDFLARED_PATH" ]; then
    echo "Cloudflare tunnel"
    echo "-----------------"
    echo "  cloudflared not found."
    echo ""
    read -p "  Install cloudflared? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if [ "$OS" = "macos" ] && command -v brew >/dev/null 2>&1; then
            echo "  Running: brew install cloudflared"
            brew install cloudflared
        else
            CLOUDFLARED_URL="$(cloudflared_download_url "$OS" "$(uname -m)")"
            CLOUDFLARED_TEMP="$(mktemp)"
            echo "  Running: curl -fsSL $CLOUDFLARED_URL"
            curl -fsSL "$CLOUDFLARED_URL" -o "$CLOUDFLARED_TEMP"
            chmod 755 "$CLOUDFLARED_TEMP"
            if [ -w /usr/local/bin ]; then
                mv "$CLOUDFLARED_TEMP" /usr/local/bin/cloudflared
            else
                mkdir -p "$HOME/.local/bin"
                mv "$CLOUDFLARED_TEMP" "$HOME/.local/bin/cloudflared"
            fi
        fi
        CLOUDFLARED_PATH="$(find_binary cloudflared)"
        if [ -n "$CLOUDFLARED_PATH" ]; then
            echo "  Installed: $CLOUDFLARED_PATH"
        else
            echo "  Warning: Install succeeded but cloudflared not found in PATH."
        fi
    else
        echo "  Skipping tunnel setup (local access only)."
        echo ""
    fi
fi

# --- Tunnel configuration ---

if [ -n "$CLOUDFLARED_PATH" ]; then
    echo ""
    echo "Cloudflare tunnel setup"
    echo "-----------------------"

    # Check if cloudflared is authenticated
    CF_CERT="$HOME/.cloudflared/cert.pem"
    CF_AUTHENTICATED=false

    if [ -f "$CF_CERT" ]; then
        CF_AUTHENTICATED=true
        echo "  Auth: logged in (cert found)"
    elif "$CLOUDFLARED_PATH" tunnel list >/dev/null 2>&1; then
        CF_AUTHENTICATED=true
        echo "  Auth: logged in"
    else
        echo "  Auth: NOT logged in"
        echo ""
        echo "  Named tunnels require authentication."
        echo "  Temp tunnels work without auth."
        echo ""
        read -p "  Login to Cloudflare now? [y/N] " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "  Opening browser for Cloudflare login..."
            "$CLOUDFLARED_PATH" tunnel login
            if [ -f "$CF_CERT" ]; then
                CF_AUTHENTICATED=true
                echo "  Login successful."
            else
                echo "  Login failed or was cancelled."
            fi
        fi
    fi

    echo ""
    echo "  1) named   — persistent URL via your domain (requires auth)"
    echo "  2) temp    — random trycloudflare.com URL (changes on restart)"
    echo "  3) none    — no tunnel, local access only"
    echo ""

    # Load existing config if available
    if [ -f "$CONFIG_FILE" ]; then
        source "$CONFIG_FILE"
        if [ -n "$HERDR_TUNNEL_MODE" ]; then
            echo "  Current config: mode=$HERDR_TUNNEL_MODE"
            [ -n "$HERDR_TUNNEL_NAME" ] && echo "                  tunnel=$HERDR_TUNNEL_NAME"
            [ -n "$HERDR_TUNNEL_HOSTNAME" ] && echo "                  hostname=$HERDR_TUNNEL_HOSTNAME"
            echo ""
        fi
    fi

    read -p "  Tunnel mode [1/2/3]: " -n 1 -r TUNNEL_CHOICE
    echo ""

    case "$TUNNEL_CHOICE" in
        1)
            if [ "$CF_AUTHENTICATED" = false ]; then
                echo ""
                echo "  Error: Named tunnels require authentication."
                echo "  Run: cloudflared tunnel login"
                echo ""
                read -p "  Fall back to temp tunnel? [Y/n] " -n 1 -r
                echo
                if [[ $REPLY =~ ^[Nn]$ ]]; then
                    TUNNEL_MODE="none"
                else
                    TUNNEL_MODE="temp"
                fi
            else
                TUNNEL_MODE="named"
                echo ""

                # Detect existing tunnels
                echo "  Checking existing tunnels..."
                TUNNEL_LIST=$("$CLOUDFLARED_PATH" tunnel list --output json 2>/dev/null || echo "[]")
                TUNNEL_COUNT=$(echo "$TUNNEL_LIST" | python3 -c '
import sys, json
print(len(json.loads(sys.stdin.read())))
' 2>/dev/null || echo "0")

                if [ "$TUNNEL_COUNT" -gt 0 ]; then
                    echo ""
                    echo "  Found $TUNNEL_COUNT existing tunnel(s):"
                    echo "$TUNNEL_LIST" | python3 -c '
import sys, json
tunnels = json.loads(sys.stdin.read())
for i, t in enumerate(tunnels, 1):
    name = t.get("name", "unnamed")
    tid = t.get("id", "?")[:8]
    conns = len(t.get("connections", []))
    status = "active" if conns > 0 else "inactive"
    print(f"    {i}) {name} (id: {tid}...) [{status}, {conns} conn(s)]")
' 2>/dev/null || "$CLOUDFLARED_PATH" tunnel list 2>/dev/null | head -10
                    echo ""

                    # Check if any tunnel is already installed as a system service
                    EXISTING_SERVICE=false
                    EXISTING_SERVICE_AUTO=false
                    CF_PLIST=""
                    if [ "$OS" = "macos" ]; then
                        # Check both user agents AND system daemons
                        CF_PLIST=$(find "$HOME/Library/LaunchAgents" /Library/LaunchDaemons /Library/LaunchAgents 2>/dev/null -name "*cloudflare*" -o -name "*cloudflared*" | head -1)
                        if [ -n "$CF_PLIST" ]; then
                            EXISTING_SERVICE=true
                            echo "  Found service: $CF_PLIST"
                        fi
                        # Also check if it's actually loaded (running)
                        if launchctl list 2>/dev/null | grep -qi "cloudflare"; then
                            EXISTING_SERVICE=true
                        fi
                        if sudo launchctl list 2>/dev/null | grep -qi "cloudflare"; then
                            EXISTING_SERVICE=true
                            # System daemon is always auto-start
                            EXISTING_SERVICE_AUTO=true
                        fi
                        # Check plist for RunAtLoad/KeepAlive
                        if [ -n "$CF_PLIST" ] && [ "$EXISTING_SERVICE_AUTO" = false ]; then
                            if grep -q "KeepAlive" "$CF_PLIST" 2>/dev/null || \
                               (grep -q "RunAtLoad" "$CF_PLIST" 2>/dev/null && grep -A1 "RunAtLoad" "$CF_PLIST" | grep -q "true"); then
                                EXISTING_SERVICE_AUTO=true
                            fi
                        fi
                    else
                        # Check systemd (user + system level)
                        if systemctl --user is-enabled cloudflared.service >/dev/null 2>&1 || \
                           systemctl is-enabled cloudflared.service >/dev/null 2>&1; then
                            EXISTING_SERVICE=true
                            EXISTING_SERVICE_AUTO=true
                        elif systemctl --user list-units 2>/dev/null | grep -qi cloudflared || \
                             systemctl list-units 2>/dev/null | grep -qi cloudflared; then
                            EXISTING_SERVICE=true
                        fi
                    fi

                    if [ "$EXISTING_SERVICE" = true ]; then
                        if [ "$EXISTING_SERVICE_AUTO" = true ]; then
                            echo "  A cloudflared service is already installed and set to start automatically."
                            echo ""
                            read -p "  Use existing service (skip tunnel install)? [Y/n] " -n 1 -r
                            echo
                            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                                echo "  Using existing cloudflared service."
                                # Still need tunnel name/hostname for config
                                TUNNEL_NAME=$(echo "$TUNNEL_LIST" | python3 -c '
import sys, json
t = json.loads(sys.stdin.read())
print(t[0]["name"] if t else "")
' 2>/dev/null)
                                TUNNEL_HOSTNAME="${HERDR_TUNNEL_HOSTNAME:-}"
                                if [ -z "$TUNNEL_HOSTNAME" ]; then
                                    read -p "  What hostname does it serve? (e.g. relay.yourdomain.com): " TUNNEL_HOSTNAME
                                fi
                                TUNNEL_MODE="named-external"
                                # skip our own tunnel service install later
                            fi
                        else
                            echo "  A cloudflared service exists but is NOT set to start automatically."
                            echo ""
                            read -p "  Make it automatic (start on boot)? [Y/n] " -n 1 -r
                            echo
                            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                                if [ "$OS" = "macos" ]; then
                                    if [ -n "$CF_PLIST" ]; then
                                        # Inject RunAtLoad if missing, or set to true
                                        if grep -q "RunAtLoad" "$CF_PLIST"; then
                                            sed -i '' 's|<false/>|<true/>|' "$CF_PLIST" 2>/dev/null
                                        else
                                            sed -i '' '/<dict>/a\
    <key>RunAtLoad</key>\
    <true/>' "$CF_PLIST" 2>/dev/null
                                        fi
                                        echo "  Updated plist to start on boot."
                                    fi
                                else
                                    systemctl --user enable cloudflared.service 2>/dev/null || \
                                        sudo systemctl enable cloudflared.service 2>/dev/null
                                    echo "  Enabled cloudflared service."
                                fi
                                TUNNEL_NAME=$(echo "$TUNNEL_LIST" | python3 -c '
import sys, json
t = json.loads(sys.stdin.read())
print(t[0]["name"] if t else "")
' 2>/dev/null)
                                TUNNEL_HOSTNAME="${HERDR_TUNNEL_HOSTNAME:-}"
                                if [ -z "$TUNNEL_HOSTNAME" ]; then
                                    read -p "  What hostname does it serve? (e.g. relay.yourdomain.com): " TUNNEL_HOSTNAME
                                fi
                                TUNNEL_MODE="named-external"
                            fi
                        fi
                    fi

                    # If not using external service, pick or create a tunnel
                    if [ "$TUNNEL_MODE" = "named" ]; then
                        echo ""
                        EXISTING_NAME="${HERDR_TUNNEL_NAME:-}"

                        if [ -n "$EXISTING_NAME" ]; then
                            read -p "  Tunnel name [$EXISTING_NAME]: " TUNNEL_NAME
                            TUNNEL_NAME="${TUNNEL_NAME:-$EXISTING_NAME}"
                        else
                            read -p "  Pick tunnel (number, name, or 'new' to create): " TUNNEL_PICK
                            # If it's a number, resolve to name
                            if [[ "$TUNNEL_PICK" =~ ^[0-9]+$ ]]; then
                                TUNNEL_NAME=$(echo "$TUNNEL_LIST" | PICK="$TUNNEL_PICK" python3 -c '
import sys, json, os
tunnels = json.loads(sys.stdin.read())
idx = int(os.environ["PICK"]) - 1
print(tunnels[idx]["name"] if 0 <= idx < len(tunnels) else "")
' 2>/dev/null)
                                if [ -z "$TUNNEL_NAME" ]; then
                                    echo "  Invalid selection."
                                    TUNNEL_NAME="$TUNNEL_PICK"
                                else
                                    echo "  Selected: $TUNNEL_NAME"
                                fi
                            else
                                TUNNEL_NAME="$TUNNEL_PICK"
                            fi
                        fi

                        if [ "$TUNNEL_NAME" != "new" ]; then
                            echo ""
                            echo "  Warning: Reusing a tunnel can mix Herdr with existing routes and connectors."
                            echo "  A dedicated tunnel is strongly recommended."
                            read -p "  Type 'reuse $TUNNEL_NAME' to reuse it; press Enter for a new tunnel: " REUSE_CONFIRM
                            if [ "$REUSE_CONFIRM" != "reuse $TUNNEL_NAME" ]; then
                                TUNNEL_NAME="new"
                            fi
                        fi

                        # Create tunnel if requested
                        if [ "$TUNNEL_NAME" = "new" ]; then
                            read -p "  New tunnel name [herdr-relay]: " NEW_NAME
                            TUNNEL_NAME="${NEW_NAME:-herdr-relay}"
                            echo "  Creating tunnel '$TUNNEL_NAME'..."
                            "$CLOUDFLARED_PATH" tunnel create "$TUNNEL_NAME" || {
                                echo "  Error creating tunnel. It may already exist."
                                read -p "  Use existing '$TUNNEL_NAME'? [Y/n] " -n 1 -r
                                echo
                                [[ $REPLY =~ ^[Nn]$ ]] && exit 1
                            }
                        fi

                        EXISTING_HOST="${HERDR_TUNNEL_HOSTNAME:-}"
                        if [ -n "$EXISTING_HOST" ]; then
                            read -p "  Hostname [$EXISTING_HOST]: " TUNNEL_HOSTNAME
                            TUNNEL_HOSTNAME="${TUNNEL_HOSTNAME:-$EXISTING_HOST}"
                        else
                            read -p "  Hostname (e.g. relay.yourdomain.com): " TUNNEL_HOSTNAME
                        fi

                        if [ -z "$TUNNEL_NAME" ] || [ -z "$TUNNEL_HOSTNAME" ]; then
                            echo "  Error: Both tunnel name and hostname are required."
                            echo ""
                            read -p "  Fall back to temp tunnel? [Y/n] " -n 1 -r
                            echo
                            if [[ $REPLY =~ ^[Nn]$ ]]; then
                                TUNNEL_MODE="none"
                            else
                                TUNNEL_MODE="temp"
                            fi
                        else
                            route_tunnel_dns "$TUNNEL_NAME" "$TUNNEL_HOSTNAME"
                        fi
                    fi
                else
                    # No existing tunnels — create one
                    echo ""
                    echo "  No existing tunnels found. Creating one..."
                    read -p "  Tunnel name [herdr-relay]: " TUNNEL_NAME
                    TUNNEL_NAME="${TUNNEL_NAME:-herdr-relay}"
                    echo "  Creating tunnel '$TUNNEL_NAME'..."
                    "$CLOUDFLARED_PATH" tunnel create "$TUNNEL_NAME" || {
                        echo "  Error creating tunnel."
                        read -p "  Fall back to temp tunnel? [Y/n] " -n 1 -r
                        echo
                        if [[ $REPLY =~ ^[Nn]$ ]]; then
                            TUNNEL_MODE="none"
                        else
                            TUNNEL_MODE="temp"
                        fi
                        TUNNEL_NAME=""
                    }

                    if [ "$TUNNEL_MODE" = "named" ] && [ -n "$TUNNEL_NAME" ]; then
                        read -p "  Hostname (e.g. relay.yourdomain.com): " TUNNEL_HOSTNAME
                        if [ -z "$TUNNEL_HOSTNAME" ]; then
                            echo "  Error: Hostname required."
                            TUNNEL_MODE="temp"
                        else
                            route_tunnel_dns "$TUNNEL_NAME" "$TUNNEL_HOSTNAME"
                        fi
                    fi
                fi
            fi
            ;;
        2)
            TUNNEL_MODE="temp"
            ;;
        *)
            TUNNEL_MODE="none"
            ;;
    esac
fi

fi

# --- Save config ---

# Normalize mode for config (named-external is still "named" at runtime)
CONFIG_TUNNEL_MODE="$TUNNEL_MODE"
[ "$CONFIG_TUNNEL_MODE" = "named-external" ] && CONFIG_TUNNEL_MODE="named"

mkdir -p "$CONFIG_DIR"
HERDR_CONFIG_FILE="$CONFIG_FILE" \
HERDR_RELAY_BIND="${HERDR_RELAY_BIND:-127.0.0.1}" \
HERDR_RELAY_PORT="$WS_PORT" \
HERDR_RELAY_TOKEN="${HERDR_RELAY_TOKEN:-}" \
HERDR_ALLOWED_ORIGINS="${HERDR_ALLOWED_ORIGINS:-}" \
HERDR_PUBLIC_URL="${HERDR_PUBLIC_URL:-}" \
HERDR_CONFIG_DIR="$CONFIG_DIR" \
HERDR_BIN="${HERDR_PATH:-herdr}" \
HERDR_LOG_DIR="$LOG_DIR" \
HERDR_TUNNEL_MODE="$CONFIG_TUNNEL_MODE" \
HERDR_TUNNEL_NAME="${TUNNEL_NAME:-}" \
HERDR_TUNNEL_HOSTNAME="${TUNNEL_HOSTNAME:-}" \
HERDR_RELAY_DIR="$SCRIPT_DIR" \
HERDR_UV_PATH="$UV_PATH" \
HERDR_CLOUDFLARED_PATH="${CLOUDFLARED_PATH:-}" \
    "$SCRIPT_DIR/write-config.sh"
RELAY_TOKEN="$(sed -n 's/^HERDR_RELAY_TOKEN=//p' "$CONFIG_FILE")"

echo ""
echo "Config saved to $CONFIG_FILE"
echo ""

# --- Build PATH for the service ---

SERVICE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
[ -d "$HOME/.cargo/bin" ] && SERVICE_PATH="$HOME/.cargo/bin:$SERVICE_PATH"
[ -d "$HOME/.local/bin" ] && SERVICE_PATH="$HOME/.local/bin:$SERVICE_PATH"

# --- Install relay service ---

# Stop a relay previously installed by this script before checking for conflicts.
# This avoids killing its child process while the service manager restarts it.
if [ "$OS" = "macos" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL_RELAY" 2>/dev/null || true
else
    if systemctl --user is-active --quiet herdr-relay.service 2>/dev/null; then
        echo "Stopping existing managed relay service..."
        systemctl --user stop herdr-relay.service
    fi
    "$SCRIPT_DIR/service.sh" stop >/dev/null 2>&1 || true
fi

# Any remaining listener is unrelated or wasn't managed by this installer.
EXISTING_PID=$(lsof -iTCP:"$WS_PORT" -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$EXISTING_PID" ]; then
    EXISTING_CMD=$(ps -p "$EXISTING_PID" -o command= 2>/dev/null || echo "unknown")
    echo "Port $WS_PORT is already in use:"
    echo "  PID $EXISTING_PID: $EXISTING_CMD"
    echo ""
    read -p "  Kill it and proceed? [Y/n] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        echo "  Aborting. Stop the existing process first."
        exit 1
    fi
    # Try graceful shutdown first (SIGTERM)
    kill "$EXISTING_PID" 2>/dev/null
    for i in 1 2 3 4 5; do
        if ! kill -0 "$EXISTING_PID" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    # Force kill if still alive
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "  Process didn't exit gracefully, sending SIGKILL..."
        kill -9 "$EXISTING_PID" 2>/dev/null || true
        sleep 1
    fi
    # Final check on port (socket may linger briefly)
    for i in 1 2 3; do
        if ! lsof -iTCP:"$WS_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if lsof -iTCP:"$WS_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "  Error: Port $WS_PORT still in use after killing PID $EXISTING_PID."
        echo "  Try manually: kill -9 $EXISTING_PID"
        exit 1
    fi
    echo "  Stopped."
fi

echo "Preparing relay dependencies..."
"$UV_PATH" sync --script "$SCRIPT_DIR/herdr_relay.py"
echo "  Relay dependencies ready."
echo ""

echo "Installing relay service..."

if [ "$OS" = "macos" ]; then
    PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL_RELAY.plist"

    launchctl bootout "gui/$(id -u)/$LABEL_RELAY" 2>/dev/null || true
    sleep 1

    cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL_RELAY</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SCRIPT_DIR/service.sh</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/relay-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/relay-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$SERVICE_PATH</string>
    </dict>
</dict>
</plist>
EOF

    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

else
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"

    if systemctl --user show-environment >/dev/null 2>&1; then
        systemctl --user stop herdr-relay.service 2>/dev/null || true

        cat > "$UNIT_DIR/herdr-relay.service" <<EOF
[Unit]
Description=herdr-remote relay
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=/bin/bash "$SCRIPT_DIR/service.sh" run
WorkingDirectory=$SCRIPT_DIR
Restart=always
RestartSec=5
TimeoutStopSec=20
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=default.target
EOF

        systemctl --user daemon-reload
        systemctl --user enable herdr-relay.service
        systemctl --user start herdr-relay.service
        SERVICE_MANAGER="systemd-user"
    else
        echo "  systemd user services unavailable; using the managed process fallback."
        "$SCRIPT_DIR/service.sh" restart
        SERVICE_MANAGER="fallback"
    fi
fi

echo "  Relay service installed."

# --- Install secure proxy service (if configured) ---

if [ "$TUNNEL_MODE" = "tailscale" ]; then
    systemctl --user disable --now herdr-tunnel.service 2>/dev/null || true
    echo "  Proxy: Tailscale Serve is persistent through tailscaled."
    echo "  URL: $TAILSCALE_URL"
elif [ "$TUNNEL_MODE" = "named-external" ]; then
    echo "  Tunnel: using existing cloudflared service (not managed by herdr-remote)."
    echo "  Hostname: ${TUNNEL_HOSTNAME:-unknown}"
elif [ "$TUNNEL_MODE" != "none" ] && [ -n "$CLOUDFLARED_PATH" ]; then
    echo "Installing tunnel service (mode: $TUNNEL_MODE)..."

    if [ "$TUNNEL_MODE" = "named" ]; then
        CF_CONFIG_DIR="$HOME/.cloudflared"
        mkdir -p "$CF_CONFIG_DIR"
        TUNNEL_ID=$("$CLOUDFLARED_PATH" tunnel list --output json 2>/dev/null | \
            TUNNEL_NAME="$TUNNEL_NAME" python3 -c '
import json, os, sys
name = os.environ["TUNNEL_NAME"]
for tunnel in json.load(sys.stdin):
    if tunnel.get("name") == name or tunnel.get("id") == name:
        print(tunnel["id"])
        break
' 2>/dev/null) || true
        if [ -z "$TUNNEL_ID" ]; then
            echo "  Error: Could not resolve tunnel '$TUNNEL_NAME' to a Cloudflare tunnel ID."
            exit 1
        fi

        CF_CREDENTIALS="$CF_CONFIG_DIR/$TUNNEL_ID.json"
        if [ ! -s "$CF_CREDENTIALS" ]; then
            echo "  Fetching credentials for tunnel '$TUNNEL_NAME'..."
            "$CLOUDFLARED_PATH" tunnel token --cred-file "$CF_CREDENTIALS" "$TUNNEL_ID" >/dev/null
        fi
        chmod 600 "$CF_CREDENTIALS"

        # Write ingress config for the selected named tunnel.
        CF_CONFIG="$CF_CONFIG_DIR/config-herdr.yml"
        cat > "$CF_CONFIG" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CF_CREDENTIALS

ingress:
  - hostname: $TUNNEL_HOSTNAME
    service: http://127.0.0.1:$WS_PORT
  - service: http_status:404
EOF
        chmod 600 "$CF_CONFIG"
        TUNNEL_ARGS="tunnel --config $CF_CONFIG run $TUNNEL_ID"
        echo "  Tunnel config: $CF_CONFIG"
    else
        TUNNEL_ARGS="tunnel --url http://localhost:$WS_PORT"
    fi

    if [ "$OS" = "macos" ]; then
        PLIST_TUNNEL="$HOME/Library/LaunchAgents/$LABEL_TUNNEL.plist"

        launchctl bootout "gui/$(id -u)/$LABEL_TUNNEL" 2>/dev/null || true
        sleep 1

        # Build ProgramArguments array
        ARGS_XML=""
        for arg in $TUNNEL_ARGS; do
            ARGS_XML="$ARGS_XML        <string>$arg</string>
"
        done

        cat > "$PLIST_TUNNEL" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL_TUNNEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$CLOUDFLARED_PATH</string>
$ARGS_XML    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/tunnel-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/tunnel-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$SERVICE_PATH</string>
    </dict>
</dict>
</plist>
EOF

        launchctl bootstrap "gui/$(id -u)" "$PLIST_TUNNEL"

    else
        if [ "${SERVICE_MANAGER:-}" = "systemd-user" ]; then
            systemctl --user stop herdr-tunnel.service 2>/dev/null || true

            cat > "$UNIT_DIR/herdr-tunnel.service" <<EOF
[Unit]
Description=herdr-remote Cloudflare tunnel
After=herdr-relay.service
Requires=herdr-relay.service

[Service]
ExecStart=$CLOUDFLARED_PATH $TUNNEL_ARGS
Restart=always
RestartSec=10
Environment=PATH=$SERVICE_PATH

[Install]
WantedBy=default.target
EOF

            systemctl --user daemon-reload
            systemctl --user enable herdr-tunnel.service
            systemctl --user start herdr-tunnel.service
        else
            nohup "$CLOUDFLARED_PATH" $TUNNEL_ARGS >>"$LOG_DIR/tunnel-stdout.log" 2>>"$LOG_DIR/tunnel-stderr.log" &
            TUNNEL_PID=$!
            printf '%s\n' "$TUNNEL_PID" > "$CONFIG_DIR/tunnel.pid"
            echo "  Tunnel started with fallback manager (pid $TUNNEL_PID)."
        fi
    fi

    echo "  Tunnel service installed."

    if [ "$TUNNEL_MODE" = "temp" ]; then
        echo ""
        echo "  Temp tunnel URL will appear in: $LOG_DIR/tunnel-stderr.log"
        echo "  Run: grep trycloudflare $LOG_DIR/tunnel-stderr.log"
    else
        echo "  Named tunnel: wss://$TUNNEL_HOSTNAME"
    fi
fi

echo ""
echo "Services installed and started."
echo ""

# --- Smoke test ---

echo "Running smoke test..."

port_is_listening() {
    lsof -iTCP:"$WS_PORT" -sTCP:LISTEN >/dev/null 2>&1 || \
        ss -tln 2>/dev/null | grep -q ":$WS_PORT "
}

# The first uv run may need to download and build relay dependencies.
SMOKE_TIMEOUT="${HERDR_SMOKE_TIMEOUT:-60}"
SMOKE_WAITED=0
while ! port_is_listening && [ "$SMOKE_WAITED" -lt "$SMOKE_TIMEOUT" ]; do
    sleep 1
    SMOKE_WAITED=$((SMOKE_WAITED + 1))
done

# 1. Check port is listening
if ! port_is_listening; then
    echo ""
    echo "  FAIL: Port $WS_PORT is not listening after $SMOKE_TIMEOUT seconds."
    echo "  Check logs: tail -20 $LOG_DIR/relay.log"
    exit 1
fi
echo "  [ok] Port $WS_PORT is listening (waited ${SMOKE_WAITED}s)"

# 2. WebSocket connect and receive agents broadcast
SMOKE_RESULT=$(WS_PORT="$WS_PORT" RELAY_TOKEN="$RELAY_TOKEN" "$UV_PATH" run --with websockets python -c '
import asyncio, json, sys, os
async def test():
    port = os.environ["WS_PORT"]
    token = os.environ["RELAY_TOKEN"]
    try:
        import websockets
    except ImportError:
        print("ws_ok:skip")
        return
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}",
            additional_headers={"Authorization": f"Bearer {token}"},
            subprotocols=["herdr-v1"],
            open_timeout=5,
        ) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            if data.get("type") == "agents":
                agents = data.get("agents", [])
                print(f"ws_ok:agents:{len(agents)}")
            else:
                print(f"ws_ok:msg:{data.get('type', 'unknown')}")
    except Exception as e:
        print(f"ws_fail:{e}")
asyncio.run(test())
' 2>/dev/null || echo "ws_fail:python_error")

case "$SMOKE_RESULT" in
    ws_ok:agents:*)
        COUNT="${SMOKE_RESULT##*:}"
        echo "  [ok] WebSocket connected, received agents broadcast ($COUNT agent(s))"
        ;;
    ws_ok:msg:*)
        TYPE="${SMOKE_RESULT##*:}"
        echo "  [ok] WebSocket connected, received message (type: $TYPE)"
        ;;
    ws_ok:skip)
        echo "  [ok] WebSocket connect skipped (websockets not importable outside relay env)"
        ;;
    ws_fail:*)
        ERR="${SMOKE_RESULT#ws_fail:}"
        echo "  FAIL: WebSocket authentication test failed: $ERR"
        exit 1
        ;;
esac

# 3. Check herdr can poll
if [ -n "$HERDR_PATH" ]; then
    if "$HERDR_PATH" pane list >/dev/null 2>&1; then
        echo "  [ok] herdr pane list works"
    else
        echo "  [warn] herdr pane list failed (tmux may not be running)"
    fi
fi

# 4. Check secure proxy
if [ "$TUNNEL_MODE" = "tailscale" ] && [ -n "${TAILSCALE_URL:-}" ]; then
    if curl -fsS --max-time 10 -o /dev/null "$TAILSCALE_URL"; then
        echo "  [ok] Tailscale Serve reachable at $TAILSCALE_URL"
    else
        echo "  [warn] Tailscale Serve is configured but not reachable yet"
    fi
elif [ "$TUNNEL_MODE" = "named" ] && [ -n "$TUNNEL_HOSTNAME" ]; then
    sleep 2
    if curl -s -o /dev/null -w "%{http_code}" "https://$TUNNEL_HOSTNAME" 2>/dev/null | grep -q "^[23]"; then
        echo "  [ok] Tunnel reachable at https://$TUNNEL_HOSTNAME"
    else
        echo "  [warn] Tunnel not reachable yet at https://$TUNNEL_HOSTNAME (may take a moment)"
    fi
elif [ "$TUNNEL_MODE" = "temp" ]; then
    sleep 3
    TUNNEL_URL=$(grep -o 'https://[^ ]*\.trycloudflare\.com' "$LOG_DIR/tunnel-stderr.log" 2>/dev/null | tail -1)
    if [ -n "$TUNNEL_URL" ]; then
        echo "  [ok] Temp tunnel active: $TUNNEL_URL"
        echo "       WebSocket: wss://$(echo "$TUNNEL_URL" | sed 's|https://||')"
    else
        echo "  [warn] Temp tunnel URL not found yet. Check: grep trycloudflare $LOG_DIR/tunnel-stderr.log"
    fi
fi

echo ""
echo "Smoke test complete."
echo ""
echo "=== Summary ==="
echo "  Relay:   running on 127.0.0.1:$WS_PORT"
echo "  Auth:    required (token stored only in $CONFIG_FILE)"
[ "$TUNNEL_MODE" != "none" ] && echo "  Tunnel:  $TUNNEL_MODE"
[ "$TUNNEL_MODE" = "named" ] && echo "  URL:     wss://$TUNNEL_HOSTNAME"
[ "$TUNNEL_MODE" = "tailscale" ] && echo "  URL:     $TAILSCALE_URL"
echo "  Logs:    $LOG_DIR/"
echo "  Config:  $CONFIG_FILE"
echo ""
echo "Commands:"
echo "  View logs:  tail -f $LOG_DIR/relay.log"
echo "  Doctor:     $SCRIPT_DIR/doctor.sh"
echo "  Rotate key: $SCRIPT_DIR/rotate-token.sh"
if [ "$OS" = "macos" ]; then
    echo "  Stop:       launchctl bootout gui/$(id -u)/$LABEL_RELAY"
    echo "  Start:      launchctl bootstrap gui/$(id -u) $HOME/Library/LaunchAgents/$LABEL_RELAY.plist"
else
    if [ "${SERVICE_MANAGER:-}" = "systemd-user" ]; then
        echo "  Stop:       systemctl --user stop herdr-relay"
        echo "  Start:      systemctl --user start herdr-relay"
        echo "  Status:     systemctl --user status herdr-relay"
    else
        echo "  Stop:       $SCRIPT_DIR/service.sh stop"
        echo "  Start:      $SCRIPT_DIR/service.sh start"
        echo "  Status:     $SCRIPT_DIR/service.sh status"
    fi
fi
echo "  Uninstall:  $0 --uninstall"
