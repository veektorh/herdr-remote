#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "zeroconf>=0.80.0", "pywebpush>=2.0.0", "py-vapid>=1.9.0", "qrcode>=8.0"]
# ///
"""herdr-remote relay — polls herdr, exposes HTTP/WebSocket APIs, and broadcasts to clients."""
import asyncio, base64, io, json, logging, os, re, shutil, signal, socket, subprocess, time, urllib.parse

try:
    from websockets.asyncio.server import serve
except ImportError:
    from websockets.server import serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from relay_security import (
    SlidingWindowLimiter, ValidationError, origin_is_allowed, request_token, require_secure_bind,
    validate_message,
)
from pairing import PairingError, PairingManager
from herdr_compat import parse_pane_list
from vapid_keys import ensure_vapid_keys

from logging.handlers import RotatingFileHandler
import sys

def _get_log_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/herdr-remote")
    if os.path.isdir("/var/log") and os.access("/var/log", os.W_OK):
        return "/var/log/herdr-remote"
    return os.path.expanduser("~/.local/state/herdr-remote/log")

LOG_DIR = os.environ.get("HERDR_LOG_DIR", _get_log_dir())
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "relay.log")
AUDIT_FILE = os.path.join(LOG_DIR, "audit.log")

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.setFormatter(_formatter)
os.chmod(LOG_FILE, 0o600)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

log = logging.getLogger("herdr-relay")
log.setLevel(logging.INFO)
log.addHandler(_file_handler)
log.addHandler(_console_handler)
logging.getLogger("websockets").setLevel(logging.WARNING)

HERDR = os.environ.get("HERDR_BIN", "herdr")
WS_PORT = int(os.environ.get("HERDR_RELAY_PORT", "8375"))
WS_BIND = os.environ.get("HERDR_RELAY_BIND", "127.0.0.1")
POLL_INTERVAL = 2
AUTH_TOKEN = os.environ.get("HERDR_RELAY_TOKEN", "")
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/").lower()
    for origin in os.environ.get("HERDR_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
MDNS_ENABLED = os.environ.get("HERDR_MDNS", "false").lower() == "true"
CONFIG_DIR = os.environ.get("HERDR_CONFIG_DIR", os.path.expanduser("~/.config/herdr-remote"))
PUBLIC_URL = os.environ.get("HERDR_PUBLIC_URL", "").rstrip("/")
MAX_CLIENTS = max(1, int(os.environ.get("HERDR_MAX_CLIENTS", "16")))
MAX_PUSH_SUBSCRIPTIONS = max(1, int(os.environ.get("HERDR_MAX_PUSH_SUBSCRIPTIONS", "64")))
pairing = PairingManager(os.path.join(CONFIG_DIR, "devices.json"))

# VAPID Web Push
VAPID_PUBLIC_KEY = os.environ.get("HERDR_VAPID_PUBLIC", "")
VAPID_PRIVATE_KEY = os.environ.get("HERDR_VAPID_PRIVATE", "")
VAPID_SUBJECT = os.environ.get("HERDR_VAPID_SUBJECT", "mailto:herdr@localhost")
try:
    VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY = ensure_vapid_keys(
        CONFIG_DIR, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY
    )
except Exception as exc:
    log.warning("Could not initialize Web Push keys: %s", exc)
push_subscriptions = []  # list of PushSubscription dicts
PUSH_SUBS_FILE = os.path.join(LOG_DIR, "push_subs.json")

# Remote hosts: comma-separated SSH targets
REMOTES = [r.strip() for r in os.environ.get("HERDR_REMOTES", "").split(",") if r.strip()]

TOOL_OPTIONS = ["yes, single permission", "trust, always allow", "no (tab to edit)"]
SUBAGENT_OPTIONS = ["approve all pending", "configure individually", "exit (cancel subagents)"]
CHROME_RE = re.compile(
    r"^[\s─━═_—│|◔◑◕●\s]+$"
    r"|Kiro\s[·•]"
    r"|esc to cancel"
    r"|type to queue"
    r"|^\s*[◔◑◕●]\s+(Shell|Bash)"
)

clients = set()
last_statuses = {}
event_queue = asyncio.Queue()
pane_remote_map = {}
known_panes = set()
last_poll_at = 0.0
last_poll_ok = None
last_agent_count = 0
pairing_limiter = SlidingWindowLimiter(12, 120)
auth_failure_limiter = SlidingWindowLimiter(30, 60)
command_limiter = SlidingWindowLimiter(120, 10)

# --- Audit logging ---
_audit_handler = RotatingFileHandler(AUDIT_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
os.chmod(AUDIT_FILE, 0o600)
audit_log = logging.getLogger("herdr-audit")
audit_log.setLevel(logging.INFO)
audit_log.addHandler(_audit_handler)
audit_log.propagate = False


def audit(action: str, ip: str, device: str, pane_id: str, detail: str = ""):
    """Append a write action to the audit log as structured JSONL."""
    import datetime
    entry = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "action": action,
        "paneId": pane_id,
        "ip": ip,
        "device": device,
    }
    if detail:
        entry["detail"] = detail[:120]  # truncate like collie
    audit_log.info(json.dumps(entry, separators=(",", ":")))


def _security_header_items():
    return [
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Content-Security-Policy", (
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self' ws: wss: https:; manifest-src 'self'"
        )),
    ]


def _headers(items=()):
    from websockets.datastructures import Headers
    return Headers([*_security_header_items(), *items])


def _json_response(status, reason, payload, extra_headers=()):
    from websockets.http11 import Response
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = _headers([("Content-Type", "application/json"), ("Cache-Control", "no-store"), *extra_headers])
    return Response(status, reason, headers, body)


def _request_client_key(connection, request) -> str:
    if request.headers.get("CF-Ray"):
        cloudflare_ip = request.headers.get("CF-Connecting-IP", "").strip()
        if cloudflare_ip:
            return f"cf:{cloudflare_ip}"
    remote = getattr(connection, "remote_address", None)
    if isinstance(remote, tuple) and remote:
        return f"peer:{remote[0]}"
    return "peer:local-proxy"


def _public_url(request):
    if PUBLIC_URL:
        return PUBLIC_URL
    host = request.headers.get("Host", "127.0.0.1:%d" % WS_PORT)
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    if forwarded_proto in {"http", "https"}:
        scheme = forwarded_proto
    else:
        hostname = host.split(":", 1)[0].strip("[]").lower()
        scheme = "http" if hostname in {"localhost", "127.0.0.1", "::1"} else "https"
    return f"{scheme}://{host}"


def _pairing_qr_data_url(pair_url):
    import qrcode
    import qrcode.image.svg
    image = qrcode.make(pair_url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=3)
    output = io.BytesIO()
    image.save(output)
    return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")


# --- Web Push helpers ---
def _load_push_subs():
    global push_subscriptions
    if os.path.isfile(PUSH_SUBS_FILE):
        try:
            with open(PUSH_SUBS_FILE) as f:
                loaded = json.load(f)
            push_subscriptions = loaded if isinstance(loaded, list) else []
        except Exception:
            push_subscriptions = []


def _save_push_subs():
    fd = os.open(PUSH_SUBS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(push_subscriptions, f)
    os.chmod(PUSH_SUBS_FILE, 0o600)


def _push_subscription(record):
    """Return the browser subscription from current or legacy storage."""
    if isinstance(record, dict) and isinstance(record.get("subscription"), dict):
        return record["subscription"]
    return record


def _push_subscription_muted(record) -> bool:
    return bool(
        isinstance(record, dict)
        and isinstance(record.get("subscription"), dict)
        and record.get("muted")
    )


def _set_push_subscription_muted(subscription, muted: bool, device_id: str = "") -> bool:
    for index, record in enumerate(push_subscriptions):
        if _push_subscription(record) != subscription:
            continue
        record_device_id = record.get("deviceId", "") if isinstance(record, dict) else ""
        if device_id and record_device_id and record_device_id != device_id:
            continue
        push_subscriptions[index] = {
            "deviceId": record_device_id or device_id,
            "subscription": subscription,
            "muted": muted,
        }
        _save_push_subs()
        return True
    return False


def _remove_device_push_subscriptions(device_id: str) -> int:
    retained = [
        record for record in push_subscriptions
        if not isinstance(record, dict) or record.get("deviceId") != device_id
    ]
    removed = len(push_subscriptions) - len(retained)
    if removed:
        push_subscriptions[:] = retained
        _save_push_subs()
    return removed


async def send_web_push(
    title: str, body: str, url: str = "/", clear: bool = False, tag: str = "herdr-status"
):
    """Send push notification to all registered subscriptions.
    
    Uses collapse topic + TTL so offline devices get only the latest.
    If clear=True, sends a clear instruction instead of showing a notification.
    """
    muted_count = sum(_push_subscription_muted(record) for record in push_subscriptions)
    result = {
        "attempted": len(push_subscriptions) - muted_count,
        "muted": muted_count,
        "sent": 0,
        "failed": 0,
        "removed": 0,
    }
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return result
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("pywebpush not installed, skipping push")
        result["failed"] = result["attempted"]
        return result
    if clear:
        payload = json.dumps({"type": "clear", "tag": "herdr-blocked"})
    else:
        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    headers = {"Topic": "herdr-herd", "TTL": "21600"}  # 6h TTL, collapse key
    dead = []
    for i, record in enumerate(push_subscriptions):
        if _push_subscription_muted(record):
            continue
        sub = _push_subscription(record)
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                headers=headers,
            )
            result["sent"] += 1
        except Exception as exc:
            result["failed"] += 1
            log.warning("Push delivery failed for subscription %d (%s)", i, type(exc).__name__)
            if "410" in str(exc) or "404" in str(exc):
                dead.append(i)
    if dead:
        for i in reversed(dead):
            push_subscriptions.pop(i)
        _save_push_subs()
        result["removed"] = len(dead)
    return result

_load_push_subs()


def run_herdr_result(*args, remote=None):
    """Run Herdr without logging command arguments, which may contain user input."""
    try:
        if remote:
            cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, HERDR, *args]
        else:
            cmd = [HERDR, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode == 0, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


def run_herdr(*args, remote=None):
    ok, output = run_herdr_result(*args, remote=remote)
    return output if ok else ""


def submit_text(pane_id: str, value: str, remote=None) -> bool:
    """Insert text and press Enter only when the text insertion succeeded."""
    inserted, _ = run_herdr_result("pane", "send-text", pane_id, value, remote=remote)
    if not inserted:
        return False
    submitted, _ = run_herdr_result("pane", "send-keys", pane_id, "Enter", remote=remote)
    return submitted


def command_result(action: str, pane_id: str, ok: bool, request_id=None) -> dict:
    result = {"type": "command_result", "action": action, "ok": ok, "pane_id": pane_id}
    if request_id:
        result["request_id"] = request_id
    return result


def proxy_status(request_host: str, request_headers) -> str:
    """Report the active proxy rather than stale installer configuration."""
    if request_host.endswith(".ts.net"):
        return "tailscale-serve"
    if request_headers.get("CF-Ray"):
        return "cloudflare-tunnel"

    tailscale = shutil.which("tailscale")
    if tailscale:
        try:
            status = subprocess.run(
                [tailscale, "serve", "status"], capture_output=True, text=True, timeout=2
            )
            if status.returncode == 0 and f"127.0.0.1:{WS_PORT}" in status.stdout:
                return "tailscale-serve"
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        cloudflare = subprocess.run(
            ["systemctl", "--user", "is-active", "herdr-tunnel.service"],
            capture_output=True, text=True, timeout=2,
        )
        if cloudflare.returncode == 0 and cloudflare.stdout.strip() == "active":
            return "cloudflare-tunnel"
    except (OSError, subprocess.SubprocessError):
        pass
    return "none"


def get_agents_from_host(remote=None):
    _, agents = query_agents_from_host(remote=remote)
    return agents


def query_agents_from_host(remote=None):
    ok, raw = run_herdr_result("pane", "list", remote=remote)
    return ok, parse_pane_list(raw, remote=remote) if ok else []


def get_all_agents():
    global last_poll_at, last_poll_ok, last_agent_count
    local_ok, agents = query_agents_from_host(remote=None)
    poll_results = [local_ok]
    for remote in REMOTES:
        remote_ok, remote_agents = query_agents_from_host(remote=remote)
        poll_results.append(remote_ok)
        agents.extend(remote_agents)
    last_poll_at = time.time()
    last_poll_ok = all(poll_results)
    if last_poll_ok:
        last_agent_count = len(agents)
    return agents


def read_pane(pane_id, remote=None):
    raw = run_herdr("pane", "read", pane_id, "--lines", "50", "--source", "recent", remote=remote)
    lines = [l for l in raw.splitlines() if l.strip() and not CHROME_RE.search(l)]
    return "\n".join(lines[-20:])


def detect_options(text):
    lower = text.lower()
    if "yes, single permission" in lower:
        return TOOL_OPTIONS
    if "approve all pending" in lower:
        return SUBAGENT_OPTIONS
    return None


async def broadcast(msg):
    data = json.dumps(msg)
    dead = set()
    for ws in clients:
        try:
            await ws.send(data)
        except (ConnectionClosedError, ConnectionClosedOK):
            dead.add(ws)
        except Exception:
            dead.add(ws)
    if dead:
        log.debug("Removed %d dead client(s)", len(dead))
    clients.difference_update(dead)


def is_completion_transition(previous_status, current_status):
    return previous_status in {"working", "blocked"} and current_status in {"idle", "done"}


async def poll_loop():
    poll_failure_reported = False
    while True:
        agents = get_all_agents()
        if not last_poll_ok:
            if not poll_failure_reported:
                log.warning("Herdr pane poll failed; retaining the last valid agent list")
                poll_failure_reported = True
            await asyncio.sleep(POLL_INTERVAL)
            continue
        if poll_failure_reported:
            log.info("Herdr pane poll recovered")
            poll_failure_reported = False

        # A successful empty list is valid and must still clear disconnected panes.
        for a in agents:
            pane_remote_map[a["pane_id"]] = a.get("remote")
            known_panes.add(a["pane_id"])
        await broadcast({"type": "agents", "agents": agents})
        for a in agents:
            pid, status = a["pane_id"], a["status"]
            previous_status = last_statuses.get(pid)
            if status == "blocked" and previous_status != "blocked":
                content = read_pane(pid, remote=a.get("remote"))
                options = detect_options(content)
                await broadcast({
                    "type": "blocked", "pane_id": pid,
                    "agent": a["agent"], "project": a["project"],
                    "host": a.get("host", "local"),
                    "prompt": content[:500],
                    "options": options or TOOL_OPTIONS
                })
                # Web Push notification
                await send_web_push(
                    title=f"🐑 {a['project']} blocked",
                    body=content[:120],
                    url=f"/?pane={urllib.parse.quote(pid, safe='')}",
                    tag="herdr-blocked",
                )
            if is_completion_transition(previous_status, status):
                await send_web_push(
                    title=f"✅ {a['project']} finished",
                    body=f"{a['agent']} is ready for your review.",
                    url=f"/?pane={urllib.parse.quote(pid, safe='')}",
                    tag="herdr-complete",
                )
            elif status != "blocked" and previous_status == "blocked":
                await send_web_push("", "", clear=True)
            last_statuses[pid] = status
        # Clean up panes that are no longer reported
        current_pane_ids = {a["pane_id"] for a in agents}
        stale = known_panes - current_pane_ids
        if stale:
            known_panes.difference_update(stale)
            for pid in stale:
                pane_remote_map.pop(pid, None)
                last_statuses.pop(pid, None)
        await asyncio.sleep(POLL_INTERVAL)


def build_agent_update(event):
    pane_id = event.get("pane_id", "")
    if not pane_id or event.get("type") != "agent_event":
        return None
    agent = {"pane_id": pane_id}
    for field in ("agent", "status", "cwd", "project", "host"):
        if event.get(field):
            agent[field] = event[field]
    return {
        "type": "agent_update",
        "agent": agent,
    }


async def event_push():
    while True:
        event = await event_queue.get()
        pane_id = event.get("pane_id", "")
        status = event.get("status", "")
        host = event.get("host", "local")

        if status == "blocked" and pane_id:
            remote = pane_remote_map.get(pane_id)
            if remote or host == "local":
                content = read_pane(pane_id, remote=remote)
            else:
                content = event.get("prompt", "Agent is blocked")
            options = detect_options(content)
            await broadcast({
                "type": "blocked", "pane_id": pane_id,
                "agent": event.get("agent", ""),
                "project": event.get("project", ""),
                "host": host,
                "prompt": content[:500],
                "options": options or TOOL_OPTIONS
            })

        update = build_agent_update(event)
        if update:
            await broadcast(update)


async def process_request(connection, request):
    """Handle HTTP endpoints on the same port as WebSocket."""
    from websockets.http11 import Response

    path = (request.path or "/").split("?")[0]
    origin = request.headers.get("Origin", "")
    host = request.headers.get("Host", "")
    upgrade = request.headers.get("Upgrade", "").lower()
    client_key = _request_client_key(connection, request)

    # One-time exchange is intentionally unauthenticated; the code is random,
    # short-lived, single-use, and carried only in the URL fragment until exchange.
    if path == "/api/pair/exchange":
        if not origin_is_allowed(origin, host, ALLOWED_ORIGINS):
            return Response(403, "Forbidden", _headers([("Content-Type", "text/plain")]), b"Forbidden\n")
        if not pairing_limiter.allow(client_key):
            return _json_response(
                429, "Too Many Requests", {"error": "too many pairing attempts"},
                [("Retry-After", "120")],
            )
        import urllib.parse
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.path).query)
        code = params.get("code", [""])[0]
        name = params.get("name", ["Browser"])[0]
        try:
            credential = pairing.exchange(code, name)
        except PairingError as exc:
            return _json_response(400, "Bad Request", {"error": str(exc)})
        log.info("Paired device: id=%s", credential["deviceId"])
        audit("pair_device", "local", "browser", credential["deviceId"])
        return _json_response(200, "OK", credential)

    auth = pairing.authenticate(request_token(request.headers, request.path), AUTH_TOKEN)
    protected = path.startswith("/api/") or path == "/events" or upgrade == "websocket"
    if protected and not auth and not auth_failure_limiter.allow(client_key):
        return _json_response(
            429, "Too Many Requests", {"error": "too many authentication failures"},
            [("Retry-After", "60")],
        )

    if path == "/api/pair/start":
        if not auth or not auth.allows("pair"):
            return _json_response(401, "Unauthorized", {"error": "admin token required"})
        pair_data = pairing.start()
        pair_url = f"{_public_url(request)}/#pair={pair_data['code']}"
        pair_data.update({"pairUrl": pair_url, "qr": _pairing_qr_data_url(pair_url)})
        return _json_response(200, "OK", pair_data)

    if path == "/api/devices":
        if not auth or not auth.allows("pair"):
            return _json_response(401, "Unauthorized", {"error": "admin token required"})
        return _json_response(200, "OK", {"devices": pairing.list_devices()})

    if path == "/api/devices/revoke":
        if not auth or not auth.allows("pair"):
            return _json_response(401, "Unauthorized", {"error": "admin token required"})
        import urllib.parse
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.path).query)
        device_id = params.get("id", [""])[0]
        revoked = pairing.revoke(device_id)
        disconnected = 0
        removed_push_subscriptions = 0
        if revoked:
            removed_push_subscriptions = _remove_device_push_subscriptions(device_id)
            matching_clients = [
                client for client in clients
                if getattr(getattr(client, "herdr_auth", None), "device_id", "") == device_id
            ]
            if matching_clients:
                await asyncio.gather(*(
                    client.close(code=4003, reason="device credential revoked")
                    for client in matching_clients
                ), return_exceptions=True)
                disconnected = len(matching_clients)
            audit("revoke_device", "local", auth.role, device_id)
        return _json_response(200, "OK", {
            "revoked": revoked,
            "disconnected": disconnected,
            "removedPushSubscriptions": removed_push_subscriptions,
        })

    if path == "/api/push/test":
        if not auth or not auth.allows("push"):
            return _json_response(401, "Unauthorized", {"error": "push permission required"})
        result = await send_web_push(
            "Herdr test notification",
            "Push notifications are working.",
            "/",
        )
        audit("test_push", "local", auth.role, auth.device_id)
        return _json_response(200, "OK", result)

    if path == "/api/health":
        if not auth:
            return _json_response(401, "Unauthorized", {"error": "authentication required"})
        herdr_available = (
            os.path.isfile(HERDR) and os.access(HERDR, os.X_OK)
            if os.path.isabs(HERDR) else shutil.which(HERDR) is not None
        )
        request_host = host.split(":", 1)[0].strip("[]").lower()
        push_configured = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)
        push_count = len(push_subscriptions)
        quiet_push_count = sum(_push_subscription_muted(record) for record in push_subscriptions)
        poll_age = round(time.time() - last_poll_at, 1) if last_poll_at else None
        return _json_response(200, "OK", {
            "relay": "ok",
            "herdr": "available" if herdr_available else "not-found",
            "authentication": "required" if AUTH_TOKEN else "local-only",
            "bind": f"{WS_BIND}:{WS_PORT}",
            "proxy": proxy_status(request_host, request.headers),
            "push": (
                "subscribed" if push_count else
                "configured-no-subscriptions" if push_configured else
                "not-configured"
            ),
            "pushSubscriptions": push_count,
            "quietPushSubscriptions": quiet_push_count,
            "pairedDevices": len(pairing.list_devices()),
            "herdrPoll": (
                "not-yet-polled" if last_poll_ok is None else
                "degraded" if not last_poll_ok else
                "stale" if poll_age is not None and poll_age > POLL_INTERVAL * 3 else
                "fresh"
            ),
            "lastPollSeconds": poll_age,
            "agents": last_agent_count,
            "clients": len(clients),
            "maxClients": MAX_CLIENTS,
            "credentialRole": auth.role,
            "canPair": auth.allows("pair"),
        })

    # Check if this is a WebSocket upgrade.
    if upgrade == "websocket":
        if not origin_is_allowed(origin, host, ALLOWED_ORIGINS):
            return Response(403, "Forbidden", _headers([("Content-Type", "text/plain")]), b"Origin not allowed\n")
        if not auth:
            return Response(401, "Unauthorized", _headers([("Content-Type", "text/plain")]), b"Invalid token\n")
        if len(clients) >= MAX_CLIENTS:
            return _json_response(503, "Service Unavailable", {"error": "relay client limit reached"})
        if connection is not None:
            connection.herdr_auth = auth
        return None  # proceed with WebSocket handshake

    # Protect every API and event endpoint. Static app assets remain public.
    if path.startswith("/api/") or path == "/events":
        if not auth:
            return Response(401, "Unauthorized", _headers([("Content-Type", "text/plain")]), b"Invalid token\n")

    # Serve web app for GET / or GET /index.html
    if path in ("/", "/index.html"):
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        index_path = os.path.join(web_dir, "index.html")
        if os.path.isfile(index_path):
            with open(index_path, "rb") as f:
                body = f.read()
            headers = _headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-cache"),
            ])
            return Response(200, "OK", headers, body)

    # Serve PWA assets with explicit content types.
    static_files = {
        "/sw.js": ("sw.js", "application/javascript"),
        "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
        "/logo.svg": ("logo.svg", "image/svg+xml"),
        "/icon-192.png": ("icon-192.png", "image/png"),
        "/icon-512.png": ("icon-512.png", "image/png"),
        "/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"),
    }
    if path in static_files:
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        filename, content_type = static_files[path]
        asset_path = os.path.join(web_dir, filename)
        if os.path.isfile(asset_path):
            with open(asset_path, "rb") as f:
                body = f.read()
            header_items = [("Content-Type", content_type), ("Cache-Control", "no-cache")]
            if path == "/sw.js":
                header_items.append(("Service-Worker-Allowed", "/"))
            return Response(200, "OK", _headers(header_items), body)

    # Serve VAPID public key
    if path == "/api/vapid-public-key":
        return _json_response(200, "OK", {"publicKey": VAPID_PUBLIC_KEY})

    # Legacy HTTP event ingestion. Authentication is enforced above.
    import urllib.parse
    if path == "/events" and "?" in (request.path or ""):
        _, qs = request.path.split("?", 1)
        params = urllib.parse.parse_qs(qs)
        if "d" in params:
            try:
                event = json.loads(urllib.parse.unquote(params["d"][0]))
                event_queue.put_nowait(validate_message(event))
            except Exception:
                return Response(400, "Bad Request", _headers([("Content-Type", "text/plain")]), b"Invalid event\n")

    return Response(404, "Not Found", _headers([("Content-Type", "text/plain")]), b"Not found\n")


async def handle_client(ws):
    remote_addr = ws.remote_address
    ip = remote_addr[0] if remote_addr else "unknown"
    ua = ws.request.headers.get("User-Agent", "unknown") if ws.request else "unknown"
    origin = ws.request.headers.get("Origin", "") if ws.request else ""

    device = "unknown"
    ua_lower = ua.lower()
    if "iphone" in ua_lower or "ipad" in ua_lower:
        device = "iOS"
    elif "android" in ua_lower:
        device = "Android"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        device = "macOS"
    elif "windows" in ua_lower:
        device = "Windows"
    elif "linux" in ua_lower:
        device = "Linux"
    elif "telegram" in ua_lower or "bot" in ua_lower:
        device = "bot"
    elif "python" in ua_lower:
        device = "script"

    log.info("Client connected: ip=%s device=%s origin=%s", ip, device, origin or "-")
    clients.add(ws)
    auth = getattr(ws, "herdr_auth", None)
    connected_at = time.monotonic()
    try:
        async for raw in ws:
            if not isinstance(raw, str) or len(raw) > 65536:
                await ws.send(json.dumps({"type": "error", "message": "message too large"}))
                continue
            try:
                msg = validate_message(json.loads(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                await ws.send(json.dumps({"type": "error", "message": str(exc)}))
                continue
            msg_type = msg.get("type")
            required_scope = {
                "respond": "control", "send_keys": "control", "send_text": "control",
                "submit_text": "control",
                "read_pane": "read", "agent_event": "events",
                "push_subscribe": "push", "push_unsubscribe": "push", "push_quiet": "push",
            }[msg_type]
            if not auth or not auth.allows(required_scope):
                if msg_type in {"respond", "send_keys", "send_text", "submit_text"}:
                    await ws.send(json.dumps(command_result(
                        msg_type, msg["pane_id"], False, msg.get("request_id")
                    )))
                    continue
                await ws.send(json.dumps({"type": "error", "message": "not authorized for this action"}))
                continue
            if msg_type in {"respond", "send_keys", "send_text", "submit_text"}:
                limiter_key = getattr(auth, "device_id", "") or f"{getattr(auth, 'role', 'client')}:{ip}"
                if not command_limiter.allow(limiter_key):
                    audit("rate_limited", ip, device, msg["pane_id"], f"action={msg_type}")
                    await ws.send(json.dumps(command_result(
                        msg_type, msg["pane_id"], False, msg.get("request_id")
                    )))
                    continue
            if msg_type == "respond":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps(command_result(
                        msg_type, pane_id, False, msg.get("request_id")
                    )))
                    continue
                text = msg["text"]
                remote = pane_remote_map.get(pane_id)
                log.info("Allowed response from %s (%s): pane=%s", ip, device, pane_id)
                audit("respond", ip, device, pane_id)
                ok, _ = run_herdr_result("pane", "send-text", pane_id, text + "\n", remote=remote)
                await ws.send(json.dumps(command_result(
                    msg_type, pane_id, ok, msg.get("request_id")
                )))
            elif msg_type == "agent_event":
                event_queue.put_nowait(msg)
            elif msg_type == "read_pane":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                lines = msg["lines"]
                remote = pane_remote_map.get(pane_id)
                content = run_herdr("pane", "read", pane_id, "--lines", str(lines), "--source", "recent", remote=remote)
                await ws.send(json.dumps({"type": "pane_content", "pane_id": pane_id, "content": content}))
            elif msg_type == "send_keys":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps(command_result(
                        msg_type, pane_id, False, msg.get("request_id")
                    )))
                    continue
                keys = msg["keys"]
                remote = pane_remote_map.get(pane_id)
                log.info("Keys from %s (%s): pane=%s keys=%s", ip, device, pane_id, keys)
                audit("send_keys", ip, device, pane_id, f"keys={keys}")
                ok, _ = run_herdr_result("pane", "send-keys", pane_id, *keys, remote=remote)
                await ws.send(json.dumps(command_result(
                    msg_type, pane_id, ok, msg.get("request_id")
                )))
            elif msg_type == "send_text":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps(command_result(
                        msg_type, pane_id, False, msg.get("request_id")
                    )))
                    continue
                text = msg["text"]
                remote = pane_remote_map.get(pane_id)
                log.info("Text input from %s (%s): pane=%s length=%d", ip, device, pane_id, len(text))
                audit("send_text", ip, device, pane_id, f"length={len(text)}")
                ok, _ = run_herdr_result("pane", "send-text", pane_id, text, remote=remote)
                await ws.send(json.dumps(command_result(
                    msg_type, pane_id, ok, msg.get("request_id")
                )))
            elif msg_type == "submit_text":
                pane_id = msg["pane_id"]
                request_id = msg.get("request_id")
                if pane_id not in known_panes:
                    await ws.send(json.dumps(command_result(
                        msg_type, pane_id, False, request_id
                    )))
                    continue
                text = msg["text"]
                remote = pane_remote_map.get(pane_id)
                log.info("Text submission from %s (%s): pane=%s length=%d", ip, device, pane_id, len(text))
                audit("submit_text", ip, device, pane_id, f"length={len(text)}")
                ok = submit_text(pane_id, text, remote=remote)
                await ws.send(json.dumps(command_result(
                    msg_type, pane_id, ok, request_id
                )))
            elif msg_type == "push_subscribe":
                sub = msg.get("subscription")
                existing = False
                migrated = False
                muted = False
                for index, record in enumerate(push_subscriptions):
                    if _push_subscription(record) != sub:
                        continue
                    existing = True
                    muted = _push_subscription_muted(record)
                    if auth.device_id and not record.get("deviceId"):
                        push_subscriptions[index] = {
                            "deviceId": auth.device_id,
                            "subscription": sub,
                            "muted": muted,
                        }
                        migrated = True
                    break
                if sub and not existing:
                    if len(push_subscriptions) >= MAX_PUSH_SUBSCRIPTIONS:
                        await ws.send(json.dumps({
                            "type": "push_subscribed", "ok": False,
                            "error": "push subscription limit reached",
                        }))
                        continue
                    push_subscriptions.append({
                        "deviceId": auth.device_id,
                        "subscription": sub,
                        "muted": False,
                    })
                    migrated = True
                if migrated:
                    _save_push_subs()
                    log.info("Push subscription added from %s (%s)", ip, device)
                await ws.send(json.dumps({
                    "type": "push_subscribed", "ok": True, "quiet": muted,
                }))
            elif msg_type == "push_unsubscribe":
                sub = msg.get("subscription")
                retained = [record for record in push_subscriptions if _push_subscription(record) != sub]
                if len(retained) != len(push_subscriptions):
                    push_subscriptions[:] = retained
                    _save_push_subs()
                await ws.send(json.dumps({"type": "push_unsubscribed", "ok": True}))
            elif msg_type == "push_quiet":
                quiet = msg["quiet"]
                updated = _set_push_subscription_muted(
                    msg["subscription"], quiet, auth.device_id
                )
                if updated:
                    log.info("Push quiet mode set to %s from %s (%s)", quiet, ip, device)
                    audit("push_quiet", ip, device, "", f"quiet={quiet}")
                await ws.send(json.dumps({
                    "type": "push_quiet", "ok": updated, "quiet": quiet,
                    "error": "subscription not found" if not updated else "",
                }))
    except (ConnectionClosedError, ConnectionClosedOK):
        pass
    finally:
        duration = int(time.monotonic() - connected_at)
        log.info("Client disconnected: ip=%s device=%s duration=%ds", ip, device, duration)
        clients.discard(ws)


class UDPPlugin(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        try:
            event_queue.put_nowait(json.loads(data.decode()))
        except Exception:
            pass


def start_mdns():
    try:
        from zeroconf import Zeroconf, ServiceInfo
        import socket as sock_mod
        import threading
        ip = sock_mod.gethostbyname(sock_mod.gethostname())
        info = ServiceInfo(
            "_herdr-remote._tcp.local.", "herdr-remote._herdr-remote._tcp.local.",
            addresses=[sock_mod.inet_aton(ip)], port=WS_PORT,
        )
        zc = Zeroconf()
        threading.Thread(target=zc.register_service, args=(info,), daemon=True).start()
        log.info("mDNS registering at %s", ip)
        return zc, info
    except Exception as e:
        log.warning("mDNS skipped: %s", e)
        return None, None


async def main():
    require_secure_bind(WS_BIND, AUTH_TOKEN)
    zc, info = start_mdns() if MDNS_ENABLED else (None, None)
    loop = asyncio.get_running_loop()
    try:
        await loop.create_datagram_endpoint(UDPPlugin, local_addr=("127.0.0.1", 8376))
    except OSError:
        log.warning("UDP 8376 in use, plugin push disabled")
    asyncio.create_task(poll_loop())
    asyncio.create_task(event_push())
    server = await serve(
        handle_client, WS_BIND, WS_PORT,
        process_request=process_request,
        subprotocols=["herdr-v1"],
    )
    hosts = ["local"] + REMOTES
    log.info("herdr-remote relay on %s:%d (WebSocket + HTTP)", WS_BIND, WS_PORT)
    log.info("Authentication: %s", "required" if AUTH_TOKEN else "local-only listener")
    log.info("Polling: %s", ", ".join(hosts))
    stop = loop.create_future()

    def request_stop():
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)
    await stop
    server.close()
    if zc and info:
        zc.unregister_service(info)
        zc.close()


if __name__ == "__main__":
    asyncio.run(main())
