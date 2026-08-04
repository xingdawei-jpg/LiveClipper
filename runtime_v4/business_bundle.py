"""Build and verify signed Runtime V4 business bundles.

This module is intended to live in the stable frozen host. It verifies every
external business file before adding the bundle directory to ``sys.path`` or
executing the bundle entrypoint.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


BUNDLE_SCHEMA_VERSION = 1
RUNTIME_LAYOUT_VERSION = 4
BUNDLE_ROOT_NAME = "business"
MANIFEST_NAME = "bundle_manifest.json"
SIGNATURE_NAME = "bundle_manifest.sig"
SIGNATURE_ALGORITHM = "ed25519"
FIXED_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
CORE_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
LEGACY_RUN_LOG_NAME_PATTERN = re.compile(
    r"^\d{8}_\d{6}_.+_(?:\u6210\u529f|\u5931\u8d25)\.json$"
)
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ARCHIVE_FILE_SIZE = 64 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_SIZE = 256 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 500


class BundleBuildError(ValueError):
    pass


class BundleVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedBusinessBundle:
    root: Path
    application_version: str
    compatible_core_versions: tuple[str, ...]
    entrypoint_path: str
    entrypoint_callable: str
    import_roots: tuple[str, ...]
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


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise BundleBuildError("business bundle signing key is not Ed25519")
    return key


def _load_public_key(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise BundleVerificationError("business bundle public key is not Ed25519")
    return key


def _public_key_id(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _safe_relative_path(value: object, *, label: str) -> str:
    text = str(value or "")
    if not text or "\\" in text or "\x00" in text or ":" in text:
        raise BundleVerificationError(f"invalid {label}: {text!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BundleVerificationError(f"unsafe {label}: {text!r}")
    normalized = path.as_posix()
    if normalized != text:
        raise BundleVerificationError(f"non-canonical {label}: {text!r}")
    return normalized


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _resolved_manifest_path(root: Path, relative: str) -> Path:
    target = (root / Path(*PurePosixPath(relative).parts)).resolve()
    if not _is_within(target, root):
        raise BundleVerificationError(f"bundle file escapes root: {relative}")
    return target


def _default_legacy_artifact_quarantine(root: Path) -> Path:
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    version = root.parent.name or "unknown-version"
    return (appdata / "LiveClipper" / "recovered_logs" / "v4" / version).resolve()


def _quarantine_unlisted_legacy_run_logs(
    root: Path,
    relative_paths: set[str],
    quarantine_root: Path,
) -> set[str]:
    """Move only known legacy cutter reports outside the signed business root."""
    moved: set[str] = set()
    quarantine_root = quarantine_root.resolve()
    if _is_within(quarantine_root, root):
        raise BundleVerificationError("legacy artifact quarantine cannot be inside the business bundle")
    for relative in sorted(relative_paths):
        parts = PurePosixPath(relative).parts
        if (
            len(parts) != 3
            or parts[:2] != ("app", "logs")
            or not LEGACY_RUN_LOG_NAME_PATTERN.fullmatch(parts[2])
        ):
            continue
        target = _resolved_manifest_path(root, relative)
        if target.is_symlink() or not target.is_file():
            continue
        destination = (quarantine_root / Path(*PurePosixPath(relative).parts)).resolve()
        if not _is_within(destination, quarantine_root):
            raise BundleVerificationError("legacy artifact quarantine path escapes its root")
        if destination.exists():
            digest = _sha256_file(target)[:12]
            recovered = destination.with_name(
                f"{destination.stem}_recovered_{digest}{destination.suffix}"
            )
            destination = recovered
            counter = 1
            while destination.exists():
                destination = recovered.with_name(
                    f"{recovered.stem}_{counter}{recovered.suffix}"
                )
                counter += 1
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(target, destination)
            except OSError:
                shutil.copy2(target, destination)
                target.unlink()
        except OSError:
            continue
        moved.add(relative)
    return moved


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _load_policy(policy_path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise BundleBuildError(f"cannot read V4 business policy: {policy_path}") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise BundleBuildError("unsupported V4 business policy schema")
    include = policy.get("include")
    exclude = policy.get("exclude")
    if not isinstance(include, list) or not include:
        raise BundleBuildError("V4 business policy include list is empty")
    if not isinstance(exclude, list):
        raise BundleBuildError("V4 business policy exclude list is invalid")
    compatible = policy.get("compatible_core_versions")
    if not isinstance(compatible, list) or not compatible:
        raise BundleBuildError("V4 business policy has no compatible core versions")
    normalized = [str(item or "").strip() for item in compatible]
    if len(set(normalized)) != len(normalized) or any(
        not CORE_VERSION_PATTERN.fullmatch(item) for item in normalized
    ):
        raise BundleBuildError("V4 business policy core compatibility is invalid")
    return policy


def _collect_source_files(source_root: Path, policy: Mapping[str, Any]) -> dict[str, Path]:
    include = [str(item) for item in policy["include"]]
    exclude = [str(item) for item in policy["exclude"]]
    max_file_size = int(policy.get("max_file_size") or 20 * 1024 * 1024)
    candidates: set[Path] = set()
    for raw_pattern in include:
        pattern = raw_pattern.strip().replace("\\", "/")
        parts = PurePosixPath(pattern).parts
        if not pattern or pattern.startswith("/") or ".." in parts:
            raise BundleBuildError(f"unsafe V4 business include pattern: {raw_pattern!r}")
        try:
            candidates.update(source_root.glob(pattern))
        except (OSError, ValueError) as exc:
            raise BundleBuildError(
                f"cannot expand V4 business include pattern: {raw_pattern!r}"
            ) from exc

    selected: dict[str, Path] = {}
    for path in sorted(candidates, key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root).as_posix()
        if _matches(relative, exclude):
            continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != source_root.parent):
            raise BundleBuildError(f"business bundle cannot contain symlinks: {relative}")
        size = path.stat().st_size
        if size > max_file_size:
            raise BundleBuildError(f"business bundle file exceeds size limit: {relative}")
        selected[relative] = path

    entrypoint = policy.get("entrypoint")
    if not isinstance(entrypoint, dict):
        raise BundleBuildError("V4 business policy entrypoint is invalid")
    entrypoint_path = str(entrypoint.get("path") or "")
    if entrypoint_path not in selected:
        raise BundleBuildError(f"business entrypoint is not selected: {entrypoint_path}")
    if not selected:
        raise BundleBuildError("V4 business policy selected no files")
    return selected


def _signature_document(manifest_bytes: bytes, private_key: Ed25519PrivateKey) -> dict[str, Any]:
    public_key = private_key.public_key()
    signature = private_key.sign(manifest_bytes)
    return {
        "schema_version": 1,
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": _public_key_id(public_key),
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "value": base64.b64encode(signature).decode("ascii"),
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def build_business_archive(
    source_root: str | Path,
    output_path: str | Path,
    *,
    application_version: str,
    private_key_path: str | Path,
    policy_path: str | Path,
) -> dict[str, Any]:
    """Build a deterministic, signed V4 business ZIP archive."""

    source_root = Path(source_root).resolve()
    output_path = Path(output_path).resolve()
    private_key_path = Path(private_key_path).resolve()
    policy_path = Path(policy_path).resolve()
    if not source_root.is_dir():
        raise BundleBuildError(f"business source root does not exist: {source_root}")
    version = str(application_version or "").strip()
    if not version or any(char not in "0123456789." for char in version):
        raise BundleBuildError(f"invalid application version: {version!r}")

    policy = _load_policy(policy_path)
    selected = _collect_source_files(source_root, policy)
    files: dict[str, dict[str, Any]] = {}
    payloads: dict[str, bytes] = {}
    for relative, source in sorted(selected.items()):
        payload = source.read_bytes()
        payloads[relative] = payload
        files[relative] = {
            "sha256": _sha256_bytes(payload),
            "size": len(payload),
        }

    entrypoint = policy["entrypoint"]
    configured_import_roots = [str(item) for item in policy.get("import_roots") or []]
    active_import_roots = [
        root
        for root in configured_import_roots
        if any(relative == root or relative.startswith(f"{root}/") for relative in files)
    ]
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "runtime_layout_version": RUNTIME_LAYOUT_VERSION,
        "application_version": version,
        "compatible_core_versions": [
            str(item) for item in policy["compatible_core_versions"]
        ],
        "policy_id": str(policy.get("policy_id") or "liveclipper-business-v1"),
        "entrypoint": {
            "path": str(entrypoint["path"]),
            "callable": str(entrypoint["callable"]),
        },
        "import_roots": active_import_roots,
        "files": files,
    }
    manifest_bytes = _canonical_json_bytes(manifest)
    private_key = _load_private_key(private_key_path)
    signature = _signature_document(manifest_bytes, private_key)
    signature_bytes = _canonical_json_bytes(signature)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for relative, payload in sorted(payloads.items()):
                archive.writestr(_zip_info(f"{BUNDLE_ROOT_NAME}/{relative}"), payload)
            archive.writestr(
                _zip_info(f"{BUNDLE_ROOT_NAME}/{MANIFEST_NAME}"),
                manifest_bytes,
            )
            archive.writestr(
                _zip_info(f"{BUNDLE_ROOT_NAME}/{SIGNATURE_NAME}"),
                signature_bytes,
            )
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()

    return {
        "archive": str(output_path),
        "archive_sha256": _sha256_file(output_path),
        "manifest_sha256": signature["manifest_sha256"],
        "signature_key_id": signature["key_id"],
        "application_version": version,
        "file_count": len(files),
        "compressed_size": output_path.stat().st_size,
    }


def _read_canonical_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise BundleVerificationError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise BundleVerificationError(f"{label} must be a JSON object")
    canonical = _canonical_json_bytes(value)
    if raw != canonical:
        raise BundleVerificationError(f"{label} is not canonical JSON")
    return value, raw


def _verify_signature(
    manifest_bytes: bytes,
    signature: Mapping[str, Any],
    public_key_path: Path,
) -> str:
    if signature.get("schema_version") != 1:
        raise BundleVerificationError("unsupported bundle signature schema")
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        raise BundleVerificationError("unsupported bundle signature algorithm")
    if str(signature.get("manifest_sha256") or "").lower() != _sha256_bytes(manifest_bytes):
        raise BundleVerificationError("bundle manifest digest mismatch")
    public_key = _load_public_key(public_key_path)
    expected_key_id = _public_key_id(public_key)
    if str(signature.get("key_id") or "") != expected_key_id:
        raise BundleVerificationError("bundle signature key id mismatch")
    try:
        value = base64.b64decode(str(signature.get("value") or ""), validate=True)
        public_key.verify(value, manifest_bytes)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise BundleVerificationError("bundle signature verification failed") from exc
    return expected_key_id


def verify_business_directory(
    bundle_root: str | Path,
    public_key_path: str | Path,
    *,
    expected_version: str | None = None,
    expected_core_version: str | None = None,
    repair_legacy_runtime_artifacts: bool = False,
    legacy_artifact_quarantine: str | Path | None = None,
) -> VerifiedBusinessBundle:
    """Verify a fully extracted business directory before any import."""

    root = Path(bundle_root).resolve()
    public_key_path = Path(public_key_path).resolve()
    if not root.is_dir() or root.is_symlink():
        raise BundleVerificationError(f"business bundle root is invalid: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            relative = path.relative_to(root).as_posix()
            raise BundleVerificationError(f"business bundle cannot contain symlinks: {relative}")
    manifest_path = root / MANIFEST_NAME
    signature_path = root / SIGNATURE_NAME
    if manifest_path.is_symlink() or signature_path.is_symlink():
        raise BundleVerificationError("bundle metadata cannot be symlinks")
    manifest, manifest_bytes = _read_canonical_json(manifest_path, label="bundle manifest")
    signature, _ = _read_canonical_json(signature_path, label="bundle signature")
    key_id = _verify_signature(manifest_bytes, signature, public_key_path)

    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise BundleVerificationError("unsupported business bundle schema")
    if manifest.get("runtime_layout_version") != RUNTIME_LAYOUT_VERSION:
        raise BundleVerificationError("business bundle runtime layout mismatch")
    version = str(manifest.get("application_version") or "")
    if expected_version is not None and version != str(expected_version):
        raise BundleVerificationError(
            f"business bundle version mismatch: expected {expected_version}, got {version}"
        )
    raw_compatible = manifest.get("compatible_core_versions")
    if not isinstance(raw_compatible, list) or not raw_compatible:
        raise BundleVerificationError("business bundle core compatibility is missing")
    compatible_core_versions: list[str] = []
    for raw_core_version in raw_compatible:
        core_version = str(raw_core_version or "").strip()
        if not CORE_VERSION_PATTERN.fullmatch(core_version):
            raise BundleVerificationError(
                f"invalid business bundle core compatibility: {core_version!r}"
            )
        if core_version in compatible_core_versions:
            raise BundleVerificationError(
                f"duplicate business bundle core compatibility: {core_version}"
            )
        compatible_core_versions.append(core_version)
    if (
        expected_core_version is not None
        and str(expected_core_version) not in compatible_core_versions
    ):
        raise BundleVerificationError(
            "business bundle is incompatible with core "
            f"{expected_core_version}; compatible={compatible_core_versions}"
        )

    entrypoint = manifest.get("entrypoint")
    if not isinstance(entrypoint, dict):
        raise BundleVerificationError("business bundle entrypoint is invalid")
    entrypoint_path = _safe_relative_path(entrypoint.get("path"), label="entrypoint path")
    entrypoint_callable = str(entrypoint.get("callable") or "")
    if not entrypoint_callable.isidentifier():
        raise BundleVerificationError("business bundle entrypoint callable is invalid")

    raw_import_roots = manifest.get("import_roots")
    if not isinstance(raw_import_roots, list):
        raise BundleVerificationError("business bundle import roots are invalid")
    import_roots: list[str] = []
    for raw_root in raw_import_roots:
        relative_root = _safe_relative_path(raw_root, label="import root")
        if relative_root in import_roots:
            raise BundleVerificationError(f"duplicate business import root: {relative_root}")
        import_root = _resolved_manifest_path(root, relative_root)
        if not import_root.is_dir() or import_root.is_symlink():
            raise BundleVerificationError(f"business import root is missing or unsafe: {relative_root}")
        import_roots.append(relative_root)

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise BundleVerificationError("business bundle file manifest is empty")
    verified_files: dict[str, Mapping[str, Any]] = {}
    for raw_relative, raw_meta in sorted(files.items()):
        relative = _safe_relative_path(raw_relative, label="manifest file path")
        if relative in {MANIFEST_NAME, SIGNATURE_NAME}:
            raise BundleVerificationError(f"reserved bundle file in manifest: {relative}")
        if not isinstance(raw_meta, dict):
            raise BundleVerificationError(f"invalid metadata for bundle file: {relative}")
        path = _resolved_manifest_path(root, relative)
        if not path.is_file() or path.is_symlink():
            raise BundleVerificationError(f"bundle file is missing or unsafe: {relative}")
        size = int(raw_meta.get("size", -1))
        digest = str(raw_meta.get("sha256") or "").lower()
        if size < 0 or path.stat().st_size != size:
            raise BundleVerificationError(f"bundle file size mismatch: {relative}")
        if len(digest) != 64 or _sha256_file(path) != digest:
            raise BundleVerificationError(f"bundle file digest mismatch: {relative}")
        verified_files[relative] = {"sha256": digest, "size": size}

    if entrypoint_path not in verified_files:
        raise BundleVerificationError("business bundle entrypoint is not in the file manifest")
    for import_root in import_roots:
        prefix = f"{import_root}/"
        if not any(relative.startswith(prefix) for relative in verified_files):
            raise BundleVerificationError(f"business import root has no verified files: {import_root}")

    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    expected_files = set(verified_files) | {MANIFEST_NAME, SIGNATURE_NAME}
    if repair_legacy_runtime_artifacts:
        quarantine_root = (
            Path(legacy_artifact_quarantine).resolve()
            if legacy_artifact_quarantine is not None
            else _default_legacy_artifact_quarantine(root)
        )
        moved = _quarantine_unlisted_legacy_run_logs(
            root,
            actual_files - expected_files,
            quarantine_root,
        )
        actual_files.difference_update(moved)
    if actual_files != expected_files:
        extra = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        raise BundleVerificationError(
            f"business bundle file set mismatch: extra={extra}, missing={missing}"
        )

    return VerifiedBusinessBundle(
        root=root,
        application_version=version,
        compatible_core_versions=tuple(compatible_core_versions),
        entrypoint_path=entrypoint_path,
        entrypoint_callable=entrypoint_callable,
        import_roots=tuple(import_roots),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        signature_key_id=key_id,
        files=verified_files,
    )


def _validate_archive_member(info: zipfile.ZipInfo) -> str:
    name = info.filename
    candidate = name[:-1] if info.is_dir() and name.endswith("/") else name
    relative = _safe_relative_path(candidate, label="archive member")
    parts = PurePosixPath(relative).parts
    if not parts or parts[0] != BUNDLE_ROOT_NAME or len(parts) < 2:
        raise BundleVerificationError(f"archive member is outside business root: {name}")
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        raise BundleVerificationError(f"archive member is a symlink: {name}")
    return relative


def _extract_archive(archive_path: Path, destination: Path) -> None:
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise BundleVerificationError("business bundle ZIP has too many entries")
            total_size = 0
            for info in infos:
                relative = _validate_archive_member(info)
                if relative in seen:
                    raise BundleVerificationError(f"duplicate archive member: {relative}")
                seen.add(relative)
                if info.flag_bits & 0x1:
                    raise BundleVerificationError(f"encrypted archive member: {relative}")
                if info.is_dir():
                    continue
                if info.file_size < 0 or info.file_size > MAX_ARCHIVE_FILE_SIZE:
                    raise BundleVerificationError(f"archive member is too large: {relative}")
                total_size += info.file_size
                if total_size > MAX_ARCHIVE_UNCOMPRESSED_SIZE:
                    raise BundleVerificationError("business bundle ZIP expands beyond its limit")
                if (
                    info.file_size > 1024 * 1024
                    and info.file_size > max(1, info.compress_size) * MAX_ARCHIVE_COMPRESSION_RATIO
                ):
                    raise BundleVerificationError(
                        f"archive member compression ratio is unsafe: {relative}"
                    )
                target = (destination / Path(*PurePosixPath(relative).parts)).resolve()
                if not _is_within(target, destination):
                    raise BundleVerificationError(f"archive member escapes staging: {relative}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("xb") as output:
                    copied = 0
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > info.file_size:
                            raise BundleVerificationError(
                                f"archive member exceeds declared size: {relative}"
                            )
                        output.write(chunk)
                    if copied != info.file_size:
                        raise BundleVerificationError(
                            f"archive member size changed during extraction: {relative}"
                        )
    except zipfile.BadZipFile as exc:
        raise BundleVerificationError("invalid business bundle ZIP") from exc


def extract_verified_business_archive(
    archive_path: str | Path,
    destination: str | Path,
    public_key_path: str | Path,
    *,
    expected_version: str | None = None,
    expected_core_version: str | None = None,
) -> VerifiedBusinessBundle:
    """Safely extract and verify a bundle into a new caller-owned directory."""

    archive_path = Path(archive_path).resolve()
    destination = Path(destination).resolve()
    if not archive_path.is_file():
        raise BundleVerificationError(f"business archive does not exist: {archive_path}")
    if destination.exists():
        raise BundleVerificationError(f"business extraction destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        _extract_archive(archive_path, destination)
        return verify_business_directory(
            destination / BUNDLE_ROOT_NAME,
            public_key_path,
            expected_version=expected_version,
            expected_core_version=expected_core_version,
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def verify_business_archive(
    archive_path: str | Path,
    public_key_path: str | Path,
    *,
    expected_version: str | None = None,
    expected_core_version: str | None = None,
) -> VerifiedBusinessBundle:
    """Safely extract a transport ZIP to a temporary directory and verify it."""

    archive_path = Path(archive_path).resolve()
    if not archive_path.is_file():
        raise BundleVerificationError(f"business archive does not exist: {archive_path}")
    with tempfile.TemporaryDirectory(prefix="liveclipper-v4-verify-") as temporary:
        temp_root = Path(temporary).resolve()
        verified = extract_verified_business_archive(
            archive_path,
            temp_root / "extracted",
            public_key_path,
            expected_version=expected_version,
            expected_core_version=expected_core_version,
        )
        return VerifiedBusinessBundle(
            root=archive_path,
            application_version=verified.application_version,
            compatible_core_versions=verified.compatible_core_versions,
            entrypoint_path=verified.entrypoint_path,
            entrypoint_callable=verified.entrypoint_callable,
            import_roots=verified.import_roots,
            manifest_sha256=verified.manifest_sha256,
            signature_key_id=verified.signature_key_id,
            files=verified.files,
        )


def _import_path_texts(verified: VerifiedBusinessBundle) -> list[str]:
    roots = [
        str(_resolved_manifest_path(verified.root, relative))
        for relative in verified.import_roots
    ]
    roots.append(str(verified.root))
    return roots


def activate_verified_import_roots(verified: VerifiedBusinessBundle) -> tuple[str, ...]:
    """Keep only manifest-declared, verified bundle paths active for this process."""

    paths = _import_path_texts(verified)
    for path_text in reversed(paths):
        sys.path.insert(0, path_text)
    return tuple(paths)


def _remove_import_roots(paths: tuple[str, ...]) -> None:
    for path_text in paths:
        try:
            sys.path.remove(path_text)
        except ValueError:
            pass


def _load_entrypoint_module(
    verified: VerifiedBusinessBundle,
    *,
    retain_import_roots: bool,
) -> ModuleType:
    entrypoint = _resolved_manifest_path(verified.root, verified.entrypoint_path)
    module_name = f"_liveclipper_v4_bundle_{verified.manifest_sha256[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, entrypoint)
    if spec is None or spec.loader is None:
        raise BundleVerificationError("cannot create business bundle module spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    import_paths = activate_verified_import_roots(verified)
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if not retain_import_roots:
            _remove_import_roots(import_paths)
    return module


def load_verified_application(
    bundle_root: str | Path,
    public_key_path: str | Path,
    context: Any,
    *,
    expected_version: str | None = None,
    expected_core_version: str | None = None,
    retain_import_roots: bool = False,
) -> tuple[Any, VerifiedBusinessBundle]:
    """Verify, import, and call ``create_application(context)`` in that order."""

    verified = verify_business_directory(
        bundle_root,
        public_key_path,
        expected_version=expected_version,
        expected_core_version=expected_core_version,
    )
    module = _load_entrypoint_module(
        verified,
        retain_import_roots=retain_import_roots,
    )
    factory = getattr(module, verified.entrypoint_callable, None)
    if not callable(factory):
        raise BundleVerificationError("business bundle entrypoint callable is missing")
    return factory(context), verified


__all__ = [
    "BUNDLE_ROOT_NAME",
    "MANIFEST_NAME",
    "SIGNATURE_NAME",
    "BundleBuildError",
    "BundleVerificationError",
    "VerifiedBusinessBundle",
    "activate_verified_import_roots",
    "build_business_archive",
    "extract_verified_business_archive",
    "load_verified_application",
    "verify_business_archive",
    "verify_business_directory",
]
