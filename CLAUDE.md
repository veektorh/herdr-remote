# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

herdr-remote is a multi-client system for monitoring and approving [herdr](https://herdr.dev) AI agents remotely. It provides a WebSocket relay that bridges the herdr CLI with phone, desktop, Telegram, and terminal clients.

## Architecture

```
Clients (web/mac/ios/telegram/tui)
        │ WebSocket
        ▼
   relay (:8375)  ←── Tailscale Serve (preferred) or protected Cloudflare Tunnel
        │
        ▼
   herdr CLI (local or SSH to HERDR_REMOTES)
```

The relay (`relay/herdr_relay.py`) is the central hub: it polls herdr for agent state, accepts push events via HTTP POST and UDP, and broadcasts to connected WebSocket clients. Clients send `respond`, `read_pane`, `send_keys`, and `send_text` messages back through the relay to control agents.

## Components

| Path | What | Language |
|------|------|----------|
| `relay/herdr_relay.py` | WebSocket+HTTP relay server | Python (websockets, zeroconf) |
| `relay/herdr_telegram.py` | Telegram bot client | Python (python-telegram-bot) |
| `relay/herdr_tui.py` | Terminal TUI client | Python (textual) |
| `web/index.html` | Mobile/desktop web app (single file) | HTML/CSS/JS |
| `demo-worker/` | Cloudflare Worker mock relay for demos | JS |
| `herdi-mac/` | macOS menu bar app | Swift (SPM) |
| `herdi-ios/` | iOS app with widgets + Live Activities | Swift (XcodeGen) |

## Running Components

All Python scripts use [PEP 723 inline metadata](https://peps.python.org/pep-0723/) — `uv run` handles dependency installation automatically.

```bash
# Relay (main server)
uv run relay/herdr_relay.py

# Full setup with Cloudflare tunnel
relay/start.sh

# Telegram bot
HERDI_TG_TOKEN="..." HERDI_TG_CHAT_ID="..." uv run relay/herdr_telegram.py

# Terminal TUI
uv run relay/herdr_tui.py

# Demo worker (Cloudflare)
cd demo-worker && npx wrangler dev

# macOS app
cd herdi-mac && ./build.sh

# iOS app (generate Xcode project)
cd herdi-ios && xcodegen generate
```

## Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `HERDR_RELAY_PORT` | Relay WebSocket port (default: 8375) |
| `HERDR_RELAY_BIND` | Listener address (default: `127.0.0.1`) |
| `HERDR_RELAY_TOKEN` | Admin secret; mandatory for non-loopback binds |
| `HERDR_REMOTES` | Comma-separated SSH targets to poll |
| `HERDR_BIN` | Path to herdr binary (installer discovers it; runtime default: `herdr`) |
| `HERDR_RELAY` | Relay URL used by clients (default: `ws://127.0.0.1:8375`) |
| `HERDR_ALLOWED_ORIGINS` | Comma-separated external browser origins |
| `HERDR_PUBLIC_URL` | Canonical HTTPS URL placed in pairing QR codes |
| `HERDR_MAX_CLIENTS` | Maximum simultaneous WebSocket clients (default: 16) |
| `HERDR_MAX_PUSH_SUBSCRIPTIONS` | Maximum stored Push subscriptions (default: 64) |

## Web App

The web app is a single self-contained HTML file (`web/index.html`) with inline CSS and JS — no build step. It includes an installable manifest, offline application shell, Android/Windows layout, Web Push with delivery testing, short-lived QR pairing, paired-device revocation, and a mobile terminal keyboard. Browser credentials use a WebSocket subprotocol rather than URL query parameters.

Pairing codes are eight-character, single-use, and valid for two minutes. Paired
device tokens are scoped and only their hashes are persisted in
`~/.config/herdr-remote/devices.json` with mode `0600`. VAPID keys are generated
on first relay start and persisted in the same protected config directory.

## WebSocket Protocol

Messages are JSON with a `type` field:

**Server → Client:** `agents` (state list), `blocked` (approval prompt), `pane_content` (terminal read), `command_result` (correlated write acknowledgement)

**Client → Server:** `respond` (allowlisted approval), `read_pane` (request terminal content), `send_keys` (allowlisted key sequences), `send_text` (raw text without newline), `submit_text` (text plus Enter with correlated acknowledgement)

Write messages may include a bounded `request_id`; new clients use it to match
`command_result` responses. `send_text` remains supported for Herdr 0.7-era
clients. Never log message text, credentials, WebSocket subprotocol values, or
Push endpoint secrets.

## Deployment

- Web app: Cloudflare Pages (push to main deploys `web/`)
- Demo worker: `npx wrangler deploy` from `demo-worker/`
- macOS app: `herdi-mac/build.sh` produces `dist/Herdi.app`
