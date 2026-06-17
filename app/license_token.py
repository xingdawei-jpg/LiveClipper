# -*- coding: utf-8 -*-
"""Signed license token helpers.

Tokens are Ed25519 signed and look like:
    lc1.<base64url header>.<base64url payload>.<base64url signature>

Server keeps the private key. Client only needs the public key.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any


TOKEN_PREFIX = "lc1"
DEFAULT_PUBLIC_KEY = ""
PUBLIC_KEY_FILE = "license_public_key.txt"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    text = str(text or "").strip()
    text += "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text.encode("ascii"))


def _json_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except Exception as exc:
        raise RuntimeError("cryptography is required for signed license tokens") from exc
    return serialization, ed25519


def _decode_key_material(text: str) -> bytes:
    value = str(text or "").strip()
    if not value:
        raise ValueError("empty key")
    if value.startswith("-----BEGIN"):
        return value.encode("utf-8")
    compact = value.replace(" ", "").replace("\n", "")
    try:
        raw = bytes.fromhex(compact)
        if len(raw) in (32, 64):
            return raw
    except Exception:
        pass
    raw = _b64d(compact)
    if len(raw) in (32, 64):
        return raw
    raise ValueError("key must be PEM, 32-byte hex, or 32-byte base64url")


def generate_keypair() -> dict[str, str]:
    serialization, ed25519 = _load_crypto()
    private = ed25519.Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {"private_key": private_raw.hex(), "public_key": public_raw.hex()}


def public_key_from_private(private_key: str) -> str:
    serialization, ed25519 = _load_crypto()
    raw = _decode_key_material(private_key)
    if raw.startswith(b"-----BEGIN"):
        private = serialization.load_pem_private_key(raw, password=None)
    else:
        private = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return public_raw.hex()


def _private_key(private_key: str):
    serialization, ed25519 = _load_crypto()
    raw = _decode_key_material(private_key)
    if raw.startswith(b"-----BEGIN"):
        return serialization.load_pem_private_key(raw, password=None)
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])


def _public_key(public_key: str):
    serialization, ed25519 = _load_crypto()
    raw = _decode_key_material(public_key)
    if raw.startswith(b"-----BEGIN"):
        return serialization.load_pem_public_key(raw)
    return ed25519.Ed25519PublicKey.from_public_bytes(raw[:32])


def configured_public_key() -> str:
    env_key = os.environ.get("LIVECLIPPER_LICENSE_PUBLIC_KEY", "").strip()
    if env_key:
        return env_key

    explicit_file = os.environ.get("LIVECLIPPER_LICENSE_PUBLIC_KEY_FILE", "").strip()
    candidates: list[Path] = []
    if explicit_file:
        candidates.append(Path(explicit_file))

    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "_internal" / "app" / PUBLIC_KEY_FILE)
        candidates.append(Path(sys.executable).resolve().parent / PUBLIC_KEY_FILE)

    candidates.append(Path(__file__).resolve().with_name(PUBLIC_KEY_FILE))
    candidates.append(Path.cwd() / PUBLIC_KEY_FILE)

    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except Exception:
            pass

    return DEFAULT_PUBLIC_KEY


def sign_license_token(payload: dict[str, Any], private_key: str) -> str:
    now = int(time.time())
    body = dict(payload or {})
    body.setdefault("iat", now)
    body.setdefault("jti", secrets.token_hex(16))
    header = {"alg": "EdDSA", "typ": "liveclipper-license-token", "v": 1}
    header_b64 = _b64e(_json_bytes(header))
    payload_b64 = _b64e(_json_bytes(body))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = _private_key(private_key).sign(signing_input)
    return f"{TOKEN_PREFIX}.{header_b64}.{payload_b64}.{_b64e(signature)}"


def decode_license_token(token: str) -> dict[str, Any]:
    parts = str(token or "").strip().split(".")
    if len(parts) != 4 or parts[0] != TOKEN_PREFIX:
        raise ValueError("invalid token format")
    header = json.loads(_b64d(parts[1]).decode("utf-8"))
    payload = json.loads(_b64d(parts[2]).decode("utf-8"))
    return {"header": header, "payload": payload}


def verify_license_token(
    token: str,
    public_key: str | None = None,
    *,
    machine_id: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    public_key = (public_key if public_key is not None else configured_public_key()).strip()
    if not public_key:
        return {"ok": False, "reason": "license token public key is not configured"}

    try:
        parts = str(token or "").strip().split(".")
        if len(parts) != 4 or parts[0] != TOKEN_PREFIX:
            return {"ok": False, "reason": "invalid token format"}
        signing_input = f"{parts[1]}.{parts[2]}".encode("ascii")
        signature = _b64d(parts[3])
        _public_key(public_key).verify(signature, signing_input)

        header = json.loads(_b64d(parts[1]).decode("utf-8"))
        payload = json.loads(_b64d(parts[2]).decode("utf-8"))
        if header.get("alg") != "EdDSA":
            return {"ok": False, "reason": "unsupported token algorithm"}

        current = int(time.time()) if now is None else int(now)
        if payload.get("nbf") and current < int(payload["nbf"]):
            return {"ok": False, "reason": "license token is not active yet", "payload": payload}
        if payload.get("offline_until") and current > int(payload["offline_until"]):
            return {"ok": False, "reason": "license token offline window expired", "payload": payload}
        if payload.get("expires_at") and current > int(payload["expires_at"]):
            return {"ok": False, "reason": "license token expired", "payload": payload}
        if machine_id and payload.get("machine_id") and payload.get("machine_id") != machine_id:
            return {"ok": False, "reason": "license token machine mismatch", "payload": payload}

        return {"ok": True, "payload": payload, "header": header}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
