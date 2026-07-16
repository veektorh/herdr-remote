#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/herdr-remote"
CONFIG_FILE="$CONFIG_DIR/config.env"
PID_FILE="$CONFIG_DIR/relay.pid"

load_config() {
    if [ ! -r "$CONFIG_FILE" ]; then
        echo "Error: Config not found at $CONFIG_FILE" >&2
        echo "Run: $(cd "$(dirname "$0")" && pwd)/install-service.sh" >&2
        exit 1
    fi

    set -a
    # This file is generated with shell escaping and mode 0600 by the installer.
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
    set +a

    if [ ! -x "${HERDR_UV_PATH:-}" ] || ! "$HERDR_UV_PATH" --version >/dev/null 2>&1; then
        echo "Error: HERDR_UV_PATH is not an executable WSL/Linux uv binary." >&2
        echo "Re-run install-service.sh after installing uv inside WSL." >&2
        exit 1
    fi
}

is_running() {
    [ -r "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

run_relay() {
    load_config
    exec "$HERDR_UV_PATH" run "$HERDR_RELAY_DIR/herdr_relay.py"
}

start_relay() {
    load_config
    if is_running; then
        echo "Relay already running (pid $(cat "$PID_FILE"))."
        return
    fi

    mkdir -p "$HERDR_LOG_DIR"
    nohup "$0" run >>"$HERDR_LOG_DIR/relay-service.log" 2>&1 &
    local pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "Error: Relay failed to start. Check $HERDR_LOG_DIR/relay-service.log" >&2
        exit 1
    fi
    echo "Relay started (pid $pid)."
}

stop_relay() {
    if ! is_running; then
        rm -f "$PID_FILE"
        echo "Relay is not running."
        return
    fi
    local pid
    pid="$(cat "$PID_FILE")"
    kill "$pid"
    for _ in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "Error: Relay did not stop (pid $pid)." >&2
        exit 1
    fi
    rm -f "$PID_FILE"
    echo "Relay stopped."
}

status_relay() {
    if is_running; then
        echo "Relay is running (pid $(cat "$PID_FILE"))."
        return
    fi
    echo "Relay is stopped."
    return 1
}

case "${1:-}" in
    run) run_relay ;;
    start) start_relay ;;
    stop) stop_relay ;;
    restart) stop_relay; start_relay ;;
    status) status_relay ;;
    *) echo "Usage: $0 {run|start|stop|restart|status}" >&2; exit 2 ;;
esac
