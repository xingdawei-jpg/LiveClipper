"""Build and verify immutable Runtime V4 core directories."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


CORE_SCHEMA_VERSION = 1
RUNTIME_LAYOUT_VERSION = 4
MANIFEST_NAME = "core_manifest.json"
SIGNATURE_NAME = "core_manifest.sig"
SIGNATURE_ALGORITHM = "ed25519"
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")


class CoreBuildError(ValueError):
    pass


class CoreVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedCore:
    root: Path
    core_version: str
    entrypoint_path: str
    entrypoint: Path
    manifest_sha256: str
    metadata_sha256: str
    signature_key_id: str
    files: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class VerifiedCoreManifest:
    root: Path
    core_version: str
    entrypoint_path: str
    manifest_sha256: str
    signature_key_id: str
    files: Mapping[str, Mapping[str, Any]]


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _safe_version(value: object, *, error_type: type[ValueError]) -> str:
    version = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise error_type(f"invalid V4 core version: {version!r}")
    return version


def _safe_relative_path(value: object, *, label: str) -> str:
    text = str(value or "")
    if not text or "\\" in text or "\x00" in text or ":" in text:
        raise CoreVerificationError(f"invalid {label}: {text!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CoreVerificationError(f"unsafe {label}: {text!r}")
    if path.as_posix() != text:
        raise CoreVerificationError(f"non-canonical {label}: {text!r}")
    return text


def _resolved_path(root: Path, relative: str) -> Path:
    target = (root / Path(*PurePosixPath(relative).parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CoreVerificationError(f"core file escapes root: {relative}") from exc
    return target


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise CoreBuildError("core manifest signing key is not Ed25519")
    return key


def _load_public_key(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise CoreVerificationError("core manifest public key is not Ed25519")
    return key


def _public_key_id(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _signature_document(
    manifest_bytes: bytes,
    private_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": _public_key_id(private_key.public_key()),
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "value": base64.b64encode(private_key.sign(manifest_bytes)).decode("ascii"),
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _payload_files(root: Path, *, error_type: type[ValueError]) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            relative = path.relative_to(root).as_posix()
            raise error_type(f"V4 core cannot contain symlinks: {relative}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {MANIFEST_NAME, SIGNATURE_NAME}:
            continue
        selected[relative] = path
    return selected


def build_core_manifest(
    core_root: str | Path,
    *,
    core_version: str,
    private_key_path: str | Path,
    entrypoint: str = "LiveClipperHost.exe",
) -> dict[str, Any]:
    """Write a detached signed manifest beside an already built core."""

    root = Path(core_root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise CoreBuildError(f"V4 core root is invalid: {root}")
    version = _safe_version(core_version, error_type=CoreBuildError)
    try:
        entrypoint_path = _safe_relative_path(entrypoint, label="core entrypoint")
    except CoreVerificationError as exc:
        raise CoreBuildError(str(exc)) from exc
    selected = _payload_files(root, error_type=CoreBuildError)
    if entrypoint_path not in selected:
        raise CoreBuildError(f"V4 core entrypoint is missing: {entrypoint_path}")
    if not selected:
        raise CoreBuildError("V4 core contains no payload files")

    files: dict[str, dict[str, Any]] = {}
    for relative, path in selected.items():
        files[relative] = {
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        }
    manifest = {
        "schema_version": CORE_SCHEMA_VERSION,
        "runtime_layout_version": RUNTIME_LAYOUT_VERSION,
        "core_version": version,
        "entrypoint": entrypoint_path,
        "files": files,
    }
    manifest_bytes = _canonical_json_bytes(manifest)
    signature = _signature_document(
        manifest_bytes,
        _load_private_key(Path(private_key_path).resolve()),
    )
    _atomic_write(root / MANIFEST_NAME, manifest_bytes)
    _atomic_write(root / SIGNATURE_NAME, _canonical_json_bytes(signature))
    return {
        "core_root": str(root),
        "core_version": version,
        "entrypoint": entrypoint_path,
        "file_count": len(files),
        "manifest_sha256": signature["manifest_sha256"],
        "signature_key_id": signature["key_id"],
    }


def _read_canonical_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CoreVerificationError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise CoreVerificationError(f"{label} must be a JSON object")
    if raw != _canonical_json_bytes(value):
        raise CoreVerificationError(f"{label} is not canonical JSON")
    return value, raw


def _verify_signature(
    manifest_bytes: bytes,
    signature: Mapping[str, Any],
    public_key_path: Path,
) -> str:
    if signature.get("schema_version") != 1:
        raise CoreVerificationError("unsupported core signature schema")
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        raise CoreVerificationError("unsupported core signature algorithm")
    if str(signature.get("manifest_sha256") or "").lower() != _sha256_bytes(manifest_bytes):
        raise CoreVerificationError("core manifest digest mismatch")
    public_key = _load_public_key(public_key_path)
    expected_key_id = _public_key_id(public_key)
    if str(signature.get("key_id") or "") != expected_key_id:
        raise CoreVerificationError("core signature key id mismatch")
    try:
        value = base64.b64decode(str(signature.get("value") or ""), validate=True)
        public_key.verify(value, manifest_bytes)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise CoreVerificationError("core signature verification failed") from exc
    return expected_key_id


def verify_core_manifest(
    core_root: str | Path,
    public_key_path: str | Path,
    *,
    expected_version: str | None = None,
 ) -> VerifiedCoreManifest:
    """Verify signed core metadata without requiring its payload files yet."""

    root = Path(core_root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise CoreVerificationError(f"V4 core root is invalid: {root}")

    manifest, manifest_bytes = _read_canonical_json(
        root / MANIFEST_NAME,
        label="core manifest",
    )
    signature, _ = _read_canonical_json(
        root / SIGNATURE_NAME,
        label="core signature",
    )
    key_id = _verify_signature(manifest_bytes, signature, Path(public_key_path).resolve())
    if manifest.get("schema_version") != CORE_SCHEMA_VERSION:
        raise CoreVerificationError("unsupported V4 core manifest schema")
    if manifest.get("runtime_layout_version") != RUNTIME_LAYOUT_VERSION:
        raise CoreVerificationError("V4 core runtime layout mismatch")
    version = _safe_version(manifest.get("core_version"), error_type=CoreVerificationError)
    if expected_version is not None and version != str(expected_version):
        raise CoreVerificationError(
            f"V4 core version mismatch: expected {expected_version}, got {version}"
        )
    entrypoint_path = _safe_relative_path(manifest.get("entrypoint"), label="core entrypoint")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise CoreVerificationError("V4 core file manifest is empty")

    verified_files: dict[str, dict[str, Any]] = {}
    for raw_relative, raw_meta in files.items():
        relative = _safe_relative_path(raw_relative, label="core manifest file path")
        if relative in verified_files:
            raise CoreVerificationError(f"duplicate V4 core file: {relative}")
        if not isinstance(raw_meta, dict) or set(raw_meta) != {"sha256", "size"}:
            raise CoreVerificationError(f"invalid V4 core metadata: {relative}")
        try:
            size = int(raw_meta["size"])
        except (TypeError, ValueError) as exc:
            raise CoreVerificationError(f"invalid V4 core file size: {relative}") from exc
        digest = str(raw_meta["sha256"] or "").lower()
        if size < 0 or len(digest) != 64:
            raise CoreVerificationError(f"V4 core file digest mismatch: {relative}")
        verified_files[relative] = {"sha256": digest, "size": size}

    if entrypoint_path not in verified_files:
        raise CoreVerificationError("V4 core entrypoint is not in the file manifest")
    return VerifiedCoreManifest(
        root=root,
        core_version=version,
        entrypoint_path=entrypoint_path,
        manifest_sha256=_sha256_bytes(manifest_bytes),
        signature_key_id=key_id,
        files=verified_files,
    )


def verify_core_directory(
    core_root: str | Path,
    public_key_path: str | Path,
    *,
    expected_version: str | None = None,
    hash_mode: str = "full",
) -> VerifiedCore:
    """Verify the complete stable core before its entrypoint is executed."""

    blueprint = verify_core_manifest(
        core_root,
        public_key_path,
        expected_version=expected_version,
    )
    root = blueprint.root
    if hash_mode not in {"full", "metadata", "entrypoint"}:
        raise CoreVerificationError(f"unsupported V4 core hash mode: {hash_mode}")
    actual_payloads = (
        _payload_files(root, error_type=CoreVerificationError)
        if hash_mode != "entrypoint"
        else None
    )
    entrypoint_path = blueprint.entrypoint_path
    verified_files = dict(blueprint.files)
    metadata: dict[str, dict[str, int]] = {}
    for relative, raw_meta in verified_files.items():
        size = int(raw_meta["size"])
        digest = str(raw_meta["sha256"])
        if hash_mode != "entrypoint" or relative == entrypoint_path:
            path = _resolved_path(root, relative)
            if (
                actual_payloads is not None
                and actual_payloads.get(relative) != path
            ) or not path.is_file() or path.is_symlink():
                raise CoreVerificationError(f"V4 core file is missing or unsafe: {relative}")
            stat_result = path.stat()
            if stat_result.st_size != size:
                raise CoreVerificationError(f"V4 core file size mismatch: {relative}")
            if (hash_mode == "full" or relative == entrypoint_path) and _sha256_file(path) != digest:
                raise CoreVerificationError(f"V4 core file digest mismatch: {relative}")
            metadata[relative] = {
                "size": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
            }

    if actual_payloads is not None:
        actual_files = set(actual_payloads)
        expected_files = set(verified_files)
        if actual_files != expected_files:
            extra = sorted(actual_files - expected_files)
            missing = sorted(expected_files - actual_files)
            raise CoreVerificationError(
                f"V4 core file set mismatch: extra={extra}, missing={missing}"
            )

    return VerifiedCore(
        root=root,
        core_version=blueprint.core_version,
        entrypoint_path=entrypoint_path,
        entrypoint=_resolved_path(root, entrypoint_path),
        manifest_sha256=blueprint.manifest_sha256,
        metadata_sha256=_sha256_bytes(_canonical_json_bytes(metadata)),
        signature_key_id=blueprint.signature_key_id,
        files=verified_files,
    )


__all__ = [
    "CoreBuildError",
    "CoreVerificationError",
    "VerifiedCore",
    "VerifiedCoreManifest",
    "build_core_manifest",
    "verify_core_directory",
    "verify_core_manifest",
]
