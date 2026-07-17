"""Security and validation helpers for the herdr relay.

This module intentionally uses only the Python standard library so the security
rules can be tested without starting the relay or installing its runtime deps.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
import time
from collections import defaultdict, deque
from urllib.parse import parse_qs, urlsplit


SAFE_RESPONSES = {
    "y", "n", "a", "yes", "no", "trust",
    "yes, single permission", "trust, always allow", "no (tab to edit)",
    "approve all pending", "configure individually", "exit (cancel subagents)",
}
SAFE_KEYS = {
    "y", "n", "a", "Enter", "Tab", "Escape", "C-c",
    "Up", "Down", "Left", "Right", "BSpace",
}
KEY_ALIASES = {"Ctrl+c": "C-c", "Ctrl+C": "C-c"}
MESSAGE_TYPES = {
    "respond", "agent_event", "read_pane", "send_keys", "send_text", "submit_text",
    "push_subscribe", "push_unsubscribe", "push_quiet",
}
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ValidationError(ValueError):
    """A client message failed validation."""


class SlidingWindowLimiter:
    """Small in-memory rate limiter for a single relay process."""

    def __init__(
        self, limit: int, window_seconds: float, clock=time.monotonic,
        max_keys: int = 1024,
    ):
        if limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("rate limit, window, and key cap must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self.max_keys = max_keys
        self._events = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self.clock()
        cutoff = now - self.window_seconds
        if key not in self._events and len(self._events) >= self.max_keys:
            expired = [
                existing_key for existing_key, existing_events in self._events.items()
                if not existing_events or existing_events[-1] <= cutoff
            ]
            for existing_key in expired:
                self._events.pop(existing_key, None)
            if len(self._events) >= self.max_keys:
                self._events.pop(next(iter(self._events)))
        events = self._events[key]
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True


def is_loopback_host(host: str) -> bool:
    """Return whether a bind host is restricted to the local machine."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def require_secure_bind(host: str, token: str) -> None:
    """Reject a command-capable non-loopback listener without authentication."""
    if not is_loopback_host(host) and not token:
        raise RuntimeError(
            "HERDR_RELAY_TOKEN is required when HERDR_RELAY_BIND is not loopback"
        )


def tokens_match(candidate: str | None, expected: str) -> bool:
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


def request_token(headers, path: str) -> str | None:
    """Read bearer/subprotocol auth, retaining query compatibility for old clients."""
    authorization = headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[7:]
    protocols = headers.get("Sec-WebSocket-Protocol", "")
    for protocol in (part.strip() for part in protocols.split(",")):
        if protocol.startswith("herdr-auth."):
            return protocol.removeprefix("herdr-auth.")
    query = parse_qs(urlsplit(path or "").query)
    return query.get("token", [None])[0]


def origin_is_allowed(origin: str, host: str, configured: set[str]) -> bool:
    """Allow non-browser clients, same-origin browsers, or explicit origins."""
    if not origin:
        return True
    normalized = origin.rstrip("/").lower()
    if normalized in configured:
        return True
    parsed = urlsplit(normalized)
    return parsed.scheme in {"http", "https"} and parsed.netloc == host.lower()


def _required_string(message: dict, name: str, maximum: int = 256) -> str:
    value = message.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError(f"{name} must be a non-empty string up to {maximum} characters")
    if _CONTROL_CHAR_RE.search(value):
        raise ValidationError(f"{name} contains control characters")
    return value


def validate_message(message) -> dict:
    """Validate and normalize one client protocol message."""
    if not isinstance(message, dict):
        raise ValidationError("message must be a JSON object")
    message_type = message.get("type")
    if message_type not in MESSAGE_TYPES:
        raise ValidationError("unknown message type")

    clean = {"type": message_type}
    if message_type in {"respond", "read_pane", "send_keys", "send_text", "submit_text"}:
        clean["pane_id"] = _required_string(message, "pane_id")

    request_id = message.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
            raise ValidationError("request_id contains invalid characters")
        clean["request_id"] = request_id

    if message_type == "respond":
        text = _required_string(message, "text", 80)
        if text.strip().lower() not in SAFE_RESPONSES:
            raise ValidationError("response not in allowlist")
        clean["text"] = text
    elif message_type == "read_pane":
        try:
            lines = int(message.get("lines", 30))
        except (TypeError, ValueError) as exc:
            raise ValidationError("lines must be an integer") from exc
        if not 1 <= lines <= 5000:
            raise ValidationError("lines must be between 1 and 5000")
        clean["lines"] = lines
    elif message_type == "send_keys":
        keys = message.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
            raise ValidationError("keys must contain between 1 and 16 entries")
        normalized = [KEY_ALIASES.get(key, key) for key in keys]
        if not all(isinstance(key, str) and key in SAFE_KEYS for key in normalized):
            raise ValidationError("keys contain disallowed values")
        clean["keys"] = normalized
    elif message_type in {"send_text", "submit_text"}:
        clean["text"] = _required_string(message, "text", 1000)
    elif message_type == "agent_event":
        clean.update({
            "pane_id": _required_string(message, "pane_id"),
            "status": _required_string(message, "status", 32),
            "agent": str(message.get("agent", ""))[:80],
            "project": str(message.get("project", ""))[:256],
            "cwd": str(message.get("cwd", ""))[:1000],
            "host": str(message.get("host", "remote"))[:256],
        })
    else:
        subscription = message.get("subscription")
        if not isinstance(subscription, dict):
            raise ValidationError("subscription must be an object")
        endpoint = subscription.get("endpoint")
        keys = subscription.get("keys")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 2048:
            raise ValidationError("subscription endpoint must be an HTTPS URL")
        if not isinstance(keys, dict) or not all(isinstance(keys.get(k), str) for k in ("p256dh", "auth")):
            raise ValidationError("subscription keys are invalid")
        clean["subscription"] = subscription
        if message_type == "push_quiet":
            quiet = message.get("quiet")
            if not isinstance(quiet, bool):
                raise ValidationError("quiet must be a boolean")
            clean["quiet"] = quiet
    return clean
