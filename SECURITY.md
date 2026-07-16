# Security model

Herdr Remote can send text and keys to active terminal panes. Treat the relay as
a command-capable service, even when it is reachable only through a private
network.

## Recommended deployment

1. Keep `HERDR_RELAY_BIND=127.0.0.1`.
2. Use Tailscale Serve for tailnet-only HTTPS access.
3. Restrict tailnet policy to the user and devices that need Herdr.
4. Keep relay authentication enabled as a second independent control.
5. Pair phones and browsers with two-minute single-use codes so they receive
   scoped device credentials instead of the administrator token.

Do not use Tailscale Funnel. A Cloudflare named tunnel is appropriate only when
the hostname is protected by a deny-by-default Access policy. If Access is not
available for the account, do not publish the command relay through Cloudflare.
Quick `trycloudflare.com` tunnels are for short tests only.

## Relay controls

- Non-loopback listeners cannot start without an administrator token.
- Browser WebSocket origins must be same-origin or explicitly configured.
- Device credentials are hashed at rest and scoped to read, control, and Push.
- Pairing codes are random, single-use, expire after two minutes, and are rate
  limited. Pending codes and paired devices are capped.
- Authentication failures, command traffic, clients, and Push subscriptions are
  bounded in memory.
- Pane IDs must exist in the latest Herdr inventory before control commands run.
- Approval responses and terminal keys use explicit allowlists. Text is bounded,
  control characters are rejected, and text-plus-Enter is acknowledged.
- Audit logs record action, pane, client, and safe metadata but not credentials
  or user text. Logs rotate automatically.
- HTTP responses prevent MIME sniffing, framing, broad browser permissions, and
  referrer leakage. The current single-file PWA still requires inline script and
  style allowances; all remote values inserted into HTML are escaped.

## Credential operations

Configuration and device files are written with mode `0600`. VAPID and relay
credentials are generated with cryptographically secure randomness.

```bash
./relay/create-pairing.sh   # create a short-lived device pairing link
./relay/rotate-token.sh     # replace the administrator token and restart
./relay/doctor.sh           # verify the effective security and runtime state
```

Rotating the administrator token does not revoke scoped devices. Revoke a device
from Settings to disconnect it immediately and remove its owned Push
subscriptions.

Never place `config.env`, `devices.json`, VAPID private keys, Push subscription
state, pairing links, or credentials in Git, command URLs, screenshots, tickets,
or logs.

## Availability boundary

The Windows computer must remain awake. WSL, the relay, the selected HTTPS proxy,
and Herdr must remain running. The Windows startup helper starts WSL and uses the
systemd user service when available, otherwise the managed-process fallback.
This improves recovery after sign-in but does not prevent Windows sleep.
