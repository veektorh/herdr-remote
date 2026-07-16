# Secure WSL2 Quick Start

This milestone runs the Herdr relay in WSL2 and serves the web dashboard to an
Android or Windows browser. The relay stays on `127.0.0.1` and requires its own
token even when a tunnel or tailnet also provides identity checks.

## 1. Prerequisites inside WSL

```bash
herdr --version        # requires Herdr 0.7+
uv --version           # must be a Linux uv binary, not a Windows shim
```

If `uv` resolves into `/mnt/c/...` or cannot execute, install it inside WSL:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
exec "$SHELL" -l
```

## 2. Install the relay

```bash
git clone https://github.com/dcolinmorgan/herdr-remote
cd herdr-remote
./relay/install-service.sh
```

The installer:

- finds the actual `herdr` and Linux `uv` executables;
- generates a 256-bit relay token and saves it in
  `~/.config/herdr-remote/config.env` with mode `0600`;
- listens on `127.0.0.1:8375` by default;
- installs a systemd user service when one is available;
- otherwise starts the relay with a managed process fallback and protected PID file;
- offers Tailscale Serve as the recommended private remote-access option when
  Tailscale is installed.

Useful commands on a system without a systemd user manager:

```bash
./relay/service.sh status
./relay/service.sh restart
./relay/service.sh stop
```

Use `systemctl --user status herdr-relay` when the installer selected systemd.
For either service mode, run the complete diagnostic:

```bash
./relay/doctor.sh
```

## 3. Choose remote access

### Recommended: Tailscale Serve

Install Tailscale in WSL and sign in on both WSL and the Android/Windows device,
then proxy the loopback relay over tailnet-only HTTPS:

```bash
tailscale serve --bg --yes http://127.0.0.1:8375
tailscale serve status
```

Open the reported `https://...ts.net` address. Keep tailnet ACLs restricted to
your user/devices. Do not use Tailscale Funnel: Funnel is public Internet
exposure, while Serve remains inside the tailnet.

### Cloudflare Tunnel

A named tunnel behind a Cloudflare Access self-hosted application is the safer
Cloudflare setup. Configure its origin as:

```text
http://127.0.0.1:8375
```

Create the Access application and deny-by-default allow policy before publishing
the hostname. Access and identity-provider availability depends on your
Cloudflare account and plan. If you cannot enforce Access, use Tailscale Serve
instead of publishing a command-capable hostname. Keep relay token
authentication enabled as a second layer.

A quick tunnel is suitable only for short tests because its URL is public:

```bash
cloudflared tunnel --url http://127.0.0.1:8375
```

Never run the relay on `0.0.0.0` without a token. The relay now refuses to start
in that configuration.

## 4. Bootstrap and pair Android or Windows

Use the long-lived admin token only once in a trusted desktop browser:

1. Open the HTTPS URL reported by Tailscale Serve or Cloudflare Tunnel on the
   Windows computer.
2. Open Settings in the Herdr dashboard.
3. Read the admin token locally in WSL:

   ```bash
   sed -n 's/^HERDR_RELAY_TOKEN=//p' ~/.config/herdr-remote/config.env
   ```

4. Paste it into the token field and connect.
5. In Settings, select **Create 2-minute pairing code**.
6. Scan the QR code with the Android camera. The code is random, single-use, and
   expires after two minutes.

From any shell, including Fish, the same short-lived link can be generated with:

```bash
./relay/create-pairing.sh
```

The phone exchanges the code for its own scoped device credential. Only a hash
of that credential is stored by the relay. Browser credentials travel in the
WebSocket subprotocol header, not in the WebSocket URL. Paired devices may read
sessions, send validated input, and manage their push subscription, but cannot
create new pairing codes or inject Herdr events.

## 5. Install the PWA

The dashboard must be opened over HTTPS for installation and Web Push.

**Android Chrome:** open the dashboard, accept the in-app **Install** prompt, or
choose **Install app** from Chrome's menu.

**Windows Edge/Chrome:** open the dashboard and select the install icon in the
address bar, or use the in-app **Install** prompt.

After installation, open Settings and select **Enable Push**, then **Test
notification**. Admin browsers can also list and immediately revoke paired
devices from Settings. The application shell remains available offline, but
live sessions, approvals, and terminal data still require the WSL relay and
secure proxy to be online.

Push Settings reports three independent states: browser permission, local Push
subscription, and relay synchronization. Tapping a notification opens its Herdr
pane even when the installed PWA is already running. If an update is not visible,
fully close and reopen the PWA once so the network-first shell can activate the
new service worker.

## 6. Keep WSL running

The Windows computer must be awake, and the WSL distribution, relay, and chosen
proxy must remain running. WSL does not necessarily start merely because Windows
has signed in.

For automatic startup, open PowerShell in the repository and install the
included per-user Task Scheduler task:

```powershell
.\windows\install-wsl-startup.ps1 -Distro Ubuntu
```

It starts the WSL distribution and enabled relay service at Windows sign-in;
Tailscale Serve resumes from its persistent configuration. If Task Scheduler
registration is denied, the script installs an equivalent command in the
current user's Startup folder. Remove either form with:

```powershell
.\windows\install-wsl-startup.ps1 -Uninstall
```

The equivalent manual Task Scheduler action uses `wsl.exe` as the program and
these arguments:

```text
-d Ubuntu --exec bash -lc "/home/you/herdr-remote/relay/service.sh start"
```

If systemd user services are available, use this action instead so starting the
distribution activates the enabled unit:

```text
-d Ubuntu --exec bash -lc "systemctl --user start herdr-relay"
```

The included task retries failures three times. Windows sleep still makes the
relay unreachable; use an appropriate power plan when unattended access is
required.

## 7. Verify

```bash
./relay/doctor.sh
herdr pane list
./relay/service.sh status                 # fallback manager
ss -ltn | grep 8375                       # should show 127.0.0.1:8375
tail -n 50 ~/.local/state/herdr-remote/log/relay.log
```

The installer also performs a local authenticated WebSocket smoke test inside
the relay environment.

## 8. Security maintenance

Rotate the administrator token after suspected disclosure:

```bash
./relay/rotate-token.sh
```

This updates `~/.config/herdr-remote/config.env` with mode `0600`, restarts the
active service, and does not print the new token. Paired device credentials are
separate and remain valid; revoke them individually from Settings.

Generated credentials and state live outside the repository:

```text
~/.config/herdr-remote/config.env
~/.config/herdr-remote/devices.json
~/.config/herdr-remote/vapid_private.pem
~/.local/state/herdr-remote/log/push_subs.json
```

Do not copy these files into Git, tickets, chat, URLs, or logs.

## Troubleshooting

- **Fish rejects `source config.env`:** use `./relay/create-pairing.sh`; the file
  is Bash-compatible service configuration, not Fish syntax.
- **Relay active but initially unreachable:** `uv` may need several seconds to
  prepare its environment. Run `./relay/doctor.sh` and inspect the relay log.
- **Phone says disconnected:** confirm Windows is awake, Tailscale is connected
  on both devices, and WSL plus `herdr-relay` are running.
- **Message remains in the input:** Herdr did not acknowledge it. The PWA keeps
  unconfirmed input intentionally; retry only after connection health recovers.
- **Push is denied:** enable notifications for the installed site in Android or
  Windows browser settings, reopen the PWA, and enable Push again.
