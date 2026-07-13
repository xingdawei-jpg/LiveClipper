"""Signing helpers shared by LiveClipper release tooling and frozen updaters."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


SIGNATURE_ALGORITHM = "ed25519"
SIGNATURE_FIELD = "signature"


class SignatureError(ValueError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def unsigned_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(manifest)
    payload.pop(SIGNATURE_FIELD, None)
    return payload


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(
        unsigned_manifest(manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError("release private key is not Ed25519")
    return key


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError("release public key is not Ed25519")
    return key


def public_key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def sign_manifest(
    manifest: dict[str, Any],
    private_key_path: str | Path,
) -> dict[str, Any]:
    private_key = load_private_key(private_key_path)
    public_key = private_key.public_key()
    result = unsigned_manifest(manifest)
    signature = private_key.sign(canonical_manifest_bytes(result))
    result[SIGNATURE_FIELD] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": public_key_id(public_key),
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return result


def verify_manifest(
    manifest: dict[str, Any],
    public_key_path: str | Path,
) -> None:
    signature_info = manifest.get(SIGNATURE_FIELD)
    if not isinstance(signature_info, dict):
        raise SignatureError("release manifest has no signature")
    if signature_info.get("algorithm") != SIGNATURE_ALGORITHM:
        raise SignatureError("unsupported release signature algorithm")
    public_key = load_public_key(public_key_path)
    expected_key_id = public_key_id(public_key)
    if str(signature_info.get("key_id") or "") != expected_key_id:
        raise SignatureError("release signature key id mismatch")
    try:
        signature = base64.b64decode(
            str(signature_info.get("value") or ""),
            validate=True,
        )
        public_key.verify(signature, canonical_manifest_bytes(manifest))
    except Exception as exc:
        raise SignatureError("release manifest signature verification failed") from exc


def generate_keypair(private_path: str | Path, public_path: str | Path) -> str:
    private_path = Path(private_path)
    public_path = Path(public_path)
    if private_path.exists() or public_path.exists():
        raise FileExistsError("refusing to overwrite an existing release signing key")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key = private_key.public_key()
    public_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return public_key_id(public_key)


__all__ = [
    "SignatureError",
    "canonical_manifest_bytes",
    "generate_keypair",
    "public_key_id",
    "sha256_file",
    "sign_manifest",
    "unsigned_manifest",
    "verify_manifest",
]
