# -*- coding: utf-8 -*-
"""Signed license token helpers.

Tokens are Ed25519 signed and look like:
    lc1.<base64url header>.<base64url payload>.<base64url signature>

Server keeps the private key. Client only needs the public key.
"""

from __future__ import annotations

import base64
import hashlib
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

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q)
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = (4 * pow(5, _Q - 2, _Q)) % _Q
_B = (_xrecover(_BY), _BY)


def _edwards_add(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = q
    denom = _D * x1 * x2 * y1 * y2
    x3 = (x1 * y2 + x2 * y1) * pow(1 + denom, _Q - 2, _Q)
    y3 = (y1 * y2 + x1 * x2) * pow(1 - denom, _Q - 2, _Q)
    return x3 % _Q, y3 % _Q


def _scalarmult(p: tuple[int, int], e: int) -> tuple[int, int]:
    result = (0, 1)
    addend = p
    while e:
        if e & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        e >>= 1
    return result


def _encodepoint(p: tuple[int, int]) -> bytes:
    x, y = p
    bits = bytearray(int(y).to_bytes(32, "little"))
    bits[31] |= (x & 1) << 7
    return bytes(bits)


def _decodepoint(data: bytes) -> tuple[int, int]:
    if len(data) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    y = int.from_bytes(data, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (data[31] >> 7):
        x = _Q - x
    if (-x * x + y * y - 1 - _D * x * x * y * y) % _Q != 0:
        raise ValueError("invalid Ed25519 point")
    return x, y


def _clamp_scalar(seed: bytes) -> tuple[int, bytes]:
    digest = hashlib.sha512(seed).digest()
    head = bytearray(digest[:32])
    head[0] &= 248
    head[31] &= 63
    head[31] |= 64
    return int.from_bytes(head, "little"), digest[32:]


def _hint(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _pure_public_key_from_seed(seed: bytes) -> bytes:
    scalar, _ = _clamp_scalar(seed)
    return _encodepoint(_scalarmult(_B, scalar))


def _pure_sign(seed: bytes, message: bytes) -> bytes:
    scalar, prefix = _clamp_scalar(seed)
    public_key = _pure_public_key_from_seed(seed)
    r = _hint(prefix + message) % _L
    encoded_r = _encodepoint(_scalarmult(_B, r))
    s = (r + _hint(encoded_r + public_key + message) * scalar) % _L
    return encoded_r + int(s).to_bytes(32, "little")


def _pure_verify(public_key: bytes, signature: bytes, message: bytes) -> None:
    if len(public_key) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    if len(signature) != 64:
        raise ValueError("Ed25519 signature must be 64 bytes")
    encoded_r = signature[:32]
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        raise ValueError("invalid Ed25519 signature")
    a = _decodepoint(public_key)
    r = _decodepoint(encoded_r)
    h = _hint(encoded_r + public_key + message) % _L
    if _scalarmult(_B, s) != _edwards_add(r, _scalarmult(a, h)):
        raise ValueError("invalid Ed25519 signature")


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
    private_raw = secrets.token_bytes(32)
    public_raw = _pure_public_key_from_seed(private_raw)
    return {"private_key": private_raw.hex(), "public_key": public_raw.hex()}


def public_key_from_private(private_key: str) -> str:
    raw = _decode_key_material(private_key)
    if raw.startswith(b"-----BEGIN"):
        serialization, _ = _load_crypto()
        private = serialization.load_pem_private_key(raw, password=None)
        public_raw = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return public_raw.hex()
    return _pure_public_key_from_seed(raw[:32]).hex()


def _private_key(private_key: str) -> bytes:
    raw = _decode_key_material(private_key)
    if raw.startswith(b"-----BEGIN"):
        serialization, ed25519 = _load_crypto()
        private = serialization.load_pem_private_key(raw, password=None)
        return private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    return raw[:32]


def _public_key(public_key: str) -> bytes:
    raw = _decode_key_material(public_key)
    if raw.startswith(b"-----BEGIN"):
        serialization, _ = _load_crypto()
        public = serialization.load_pem_public_key(raw)
        return public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    return raw[:32]


def _sign_raw(private_key: str, message: bytes) -> bytes:
    raw = _decode_key_material(private_key)
    if raw.startswith(b"-----BEGIN"):
        serialization, _ = _load_crypto()
        private = serialization.load_pem_private_key(raw, password=None)
        return private.sign(message)
    return _pure_sign(raw[:32], message)


def _verify_raw(public_key: str, signature: bytes, message: bytes) -> None:
    raw = _decode_key_material(public_key)
    if raw.startswith(b"-----BEGIN"):
        serialization, _ = _load_crypto()
        public = serialization.load_pem_public_key(raw)
        public.verify(signature, message)
        return
    _pure_verify(raw[:32], signature, message)


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
    signature = _sign_raw(private_key, signing_input)
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
        _verify_raw(public_key, signature, signing_input)

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
