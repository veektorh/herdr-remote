"""Create and persist VAPID Web Push keys with restrictive permissions."""

from __future__ import annotations

import base64
import os


def ensure_vapid_keys(config_dir: str, public_key: str, private_key: str) -> tuple[str, str]:
    if public_key and private_key:
        return public_key, private_key

    private_path = os.path.join(config_dir, "vapid_private.pem")
    public_path = os.path.join(config_dir, "vapid_public.txt")
    if os.path.isfile(private_path) and os.path.isfile(public_path):
        with open(public_path, encoding="ascii") as handle:
            return handle.read().strip(), private_path

    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid

    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    vapid = Vapid()
    vapid.generate_keys()
    vapid.save_key(private_path)
    raw_public = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    generated_public = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode("ascii")
    public_fd = os.open(public_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(public_fd, "w", encoding="ascii") as handle:
        handle.write(generated_public)
        handle.write("\n")
    os.chmod(private_path, 0o600)
    os.chmod(public_path, 0o600)
    return generated_public, private_path
