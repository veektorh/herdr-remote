# herdr-remote

Agent dashboard for [herdr](https://herdr.dev) -- browser, phone, Telegram, and menu bar clients.

**[Try the live demo](https://herdr-demo.pages.dev)**

## Install (10 seconds)

Download [Herdi.app](https://github.com/dcolinmorgan/herdr-remote/releases/latest) and drag to Applications.

Monitors all your local herdr agents automatically -- no relay, no config, no account.

```bash
curl -sL https://github.com/dcolinmorgan/herdr-remote/releases/latest/download/Herdi-0.6.0.dmg -o /tmp/Herdi.dmg && open /tmp/Herdi.dmg
```

## What you get

- **Live agent timeline** -- who worked when, who blocked, who finished
- **One-tap approvals** from phone, menu bar, or Telegram
- **Daily activity digest** -- `/digest` in Telegram shows working time + block count
- **Terminal interaction** -- read output, send commands, interrupt agents remotely
- **Notifications** -- know instantly when agents need you or finish
- **11 themes** -- dark, herdr, light, sand, clay, dune, nord, rose, dracula, kanagawa, midnight

## Screenshots

| Menu Bar App | Settings |
|:--:|:--:|
| ![Menu bar](public/mac_main.png) | ![Settings](public/mac_settings.png) |

| Agent List | Terminal View |
|:--:|:--:|
| ![Agent list](public/herdr-remote_main.png) | ![Terminal](public/herdr-remote_nokeys.png) |

## WSL2, Android, and Windows

The relay now defaults to `127.0.0.1`, rejects unauthenticated non-loopback
listeners, discovers the real Herdr binary, and supports WSL installations
without systemd user services. Start with the [secure WSL2 quick start](QUICKSTART.md).

The web dashboard is an installable Android/Windows PWA with a cached offline
shell, maskable icons, Web Push, and a two-minute single-use QR pairing flow.
Paired browsers receive scoped per-device credentials; only credential hashes
are persisted by the relay. Admin settings include paired-device revocation;
push settings report real subscriptions and can send a test notification.

For remote access, prefer Tailscale Serve with restrictive tailnet ACLs. For a
public hostname, use a named Cloudflare Tunnel protected by Cloudflare Access.
Relay token authentication remains required as defense in depth.

When Tailscale is installed, `./relay/install-service.sh` offers tailnet-only
Serve as its default remote-access option. To start WSL and the relay after
Windows sign-in, run `windows\install-wsl-startup.ps1` from PowerShell.

After installation, verify the entire chain without displaying credentials:

```bash
./relay/doctor.sh
```

## Remote monitoring (phone/Telegram)

For monitoring agents across machines or from your phone:

```bash
herdr plugin install dcolinmorgan/herdr-push
cd herdr-remote && ./relay/install-service.sh
```

Generate a single-use two-minute pairing code from Bash, Fish, or Zsh without
putting the administrator token on the phone or printing it:

```bash
./relay/create-pairing.sh
```

Open the resulting link on the new Android or Windows device. The device gets
its own restricted credential; it does not receive the admin token.

Rotate the long-lived administrator token without printing the replacement:

```bash
./relay/rotate-token.sh
```

The active relay is restarted, existing administrator connections must reconnect
with the new token, and scoped paired-device credentials remain valid. Revoke a
specific device from the PWA Settings screen. See [SECURITY.md](SECURITY.md) for
the deployment threat model and operational controls.

## Telegram Bot

Full agent interaction:

```bash
export HERDR_TG_TOKEN="your-token"
export HERDR_TG_CHAT_ID="your-chat-id"
uv run relay/herdr_telegram.py
```

| Command | Action |
|---------|--------|
| `/agents` | List all with status |
| `/read` | Read agent output |
| `/reply` | Read + respond in one flow |
| `/send` | Send text to an agent |
| `/trust` | Trust all tools for blocked agent |
| `/interrupt` | Send Ctrl+C |
| `/digest` | Today's activity summary |

## Architecture

```
                    ┌──────────────────────────────┐
                    │  macOS Menu Bar (Herdi.app)   │ <- zero config
                    └──────────────────────────────┘

┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Web App     │  │  Telegram    │  │  TUI         │
│  (phone)     │  │  Bot         │  │  (terminal)  │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                  │                  │
       └───── WebSocket ──┴──────────────────┘
                   │
        ┌──────────┴──────────┐
        │   relay (:8375)     │  <- Tailscale Serve / protected tunnel
        └──────────┬──────────┘
                   │
     ┌─────────────┼─────────────┐
     │ local poll  │ herdr-push  │
     │ (herdr CLI) │ (HTTP POST) │
     └──────┬──────┘──────┬──────┘
         ┌──┴──┐     ┌────┴────┐
         │herdr│     │herdr    │
         │local│     │remote   │
         └─────┘     └─────────┘
```

## Terminal TUI

```bash
uv run relay/herdr_tui.py
```

## Token Auth

```bash
export HERDR_RELAY_TOKEN="$(openssl rand -hex 32)"
uv run relay/herdr_relay.py
```

Authentication is mandatory when `HERDR_RELAY_BIND` is not loopback. The
installer generates a 256-bit token, persists it with mode `0600`, and never
prints it in relay logs. Query-token authentication remains temporarily for
existing clients. New Python clients use `Authorization: Bearer ...`; browsers
use the WebSocket subprotocol header so durable credentials do not appear in
connection URLs.

The relay validates browser origins, scopes every credential, allowlists keys
and approval responses, caps clients and Push subscriptions, throttles pairing,
authentication failures, and remote commands, and records secret-free JSONL
write audits. Browser responses include restrictive content, framing, referrer,
and permissions headers.

## Requirements

- macOS 14+ (menu bar app)
- Python 3.10+ with [uv](https://docs.astral.sh/uv/) (relay/TUI/bot)
- Tailscale Serve (recommended private access) or `cloudflared` (public access
  only when protected by Cloudflare Access)
- herdr 0.7+
- Zero-dep plugin: [`herdr-push`](https://github.com/dcolinmorgan/herdr-push)

## Changelog

### v0.6.0

- **Workspace drill-down** — agents grouped by workspace/space; blocked "Needs you" agents hoisted to top of dashboard before workspace cards
- **Prettier cards** — shadcn-style: 12px radius, subtle borders, hover lift/shadow, `active:scale(0.99)`, cwd display, chevron navigation
- **Web Push (VAPID)** — subscribe in Settings; get notified when agents block even with tab closed; auto-clears when agent unblocks
- **Structured audit log** — write actions are logged without message text or credentials (`~/.local/state/herdr-remote/log/audit.log` on Linux)
- **Push collapse + TTL** — offline devices get only the latest notification (Topic: `herdr-herd`, TTL: 6h), not a burst of stale alerts
- **Count pills** — workspace cards show pane/tab counts at a glance

### v0.5.0

Telegram bot (`/agents /read /send /reply /trust /interrupt`), demo bot, linux setup script.
