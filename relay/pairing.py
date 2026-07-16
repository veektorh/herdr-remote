"""Short-lived pairing codes and persisted scoped device credentials."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
import threading
import time
from dataclasses import dataclass


PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
DEFAULT_SCOPES = frozenset({"read", "control", "push"})
ADMIN_SCOPES = frozenset({"read", "control", "push", "events", "pair"})


@dataclass(frozen=True)
class AuthContext:
    role: str
    scopes: frozenset[str]
    device_id: str = ""

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


class PairingError(ValueError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PairingManager:
    def __init__(
        self, devices_file: str, ttl_seconds: int = 120, clock=time.time,
        max_pending: int = 16, max_devices: int = 64,
    ):
        self.devices_file = devices_file
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.max_pending = max_pending
        self.max_devices = max_devices
        self._pending: dict[str, float] = {}
        self._devices: list[dict] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.devices_file, encoding="utf-8") as handle:
                data = json.load(handle)
            devices = data.get("devices", [])
            if isinstance(devices, list):
                self._devices = [d for d in devices if isinstance(d, dict) and d.get("tokenHash")]
        except (FileNotFoundError, OSError, ValueError):
            self._devices = []

    def _save(self) -> None:
        directory = os.path.dirname(self.devices_file)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        temporary = f"{self.devices_file}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"devices": self._devices}, handle, separators=(",", ":"))
        os.replace(temporary, self.devices_file)
        os.chmod(self.devices_file, 0o600)

    def authenticate(self, candidate: str | None, main_token: str) -> AuthContext | None:
        if not main_token:
            return AuthContext("local-admin", ADMIN_SCOPES)
        if candidate and secrets.compare_digest(candidate, main_token):
            return AuthContext("admin", ADMIN_SCOPES)
        if not candidate:
            return None
        candidate_hash = _digest(candidate)
        with self._lock:
            for device in self._devices:
                if secrets.compare_digest(candidate_hash, str(device.get("tokenHash", ""))):
                    return AuthContext(
                        "device",
                        frozenset(device.get("scopes", DEFAULT_SCOPES)),
                        str(device.get("id", "")),
                    )
        return None

    def start(self) -> dict:
        now = self.clock()
        with self._lock:
            self._pending = {key: expiry for key, expiry in self._pending.items() if expiry > now}
            if len(self._pending) >= self.max_pending:
                raise PairingError("too many active pairing codes; wait for one to expire")
            code = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
            self._pending[_digest(code)] = now + self.ttl_seconds
        return {"code": code, "expiresIn": self.ttl_seconds}

    def exchange(self, code: str, device_name: str) -> dict:
        normalized = "".join(ch for ch in code.upper() if ch in string.ascii_uppercase + string.digits)
        if len(normalized) != 8:
            raise PairingError("invalid pairing code")
        now = self.clock()
        code_hash = _digest(normalized)
        with self._lock:
            if len(self._devices) >= self.max_devices:
                raise PairingError("paired device limit reached; revoke an old device")
            expiry = self._pending.pop(code_hash, None)
            if expiry is None or expiry <= now:
                raise PairingError("pairing code is invalid, expired, or already used")
            token = secrets.token_urlsafe(32)
            device_id = secrets.token_hex(8)
            safe_name = "".join(ch for ch in device_name.strip()[:80] if ch.isprintable()) or "Browser"
            self._devices.append({
                "id": device_id,
                "name": safe_name,
                "tokenHash": _digest(token),
                "scopes": sorted(DEFAULT_SCOPES),
                "createdAt": int(now),
            })
            self._save()
        return {
            "token": token,
            "deviceId": device_id,
            "scopes": sorted(DEFAULT_SCOPES),
        }

    def list_devices(self) -> list[dict]:
        with self._lock:
            return [
                {key: device.get(key) for key in ("id", "name", "scopes", "createdAt")}
                for device in self._devices
            ]

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            retained = [device for device in self._devices if device.get("id") != device_id]
            if len(retained) == len(self._devices):
                return False
            self._devices = retained
            self._save()
            return True
