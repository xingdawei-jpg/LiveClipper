"""Build and verify signed Runtime V3-to-V4 migration package directories."""

from __future__ import annotations

import json
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.release_signing import sha256_file, sign_manifest, verify_manifest
from runtime_v4.business_bundle import VerifiedBusinessBundle, verify_business_archive
from runtime_v4.core_manifest import VerifiedCoreManifest, verify_core_manifest


MIGRATION_SCHEMA_VERSION = 1
MIGRATION_FORMAT = "liveclipper-runtime-v4-migration-v1"
RUNTIME_LAYOUT_VERSION = 4
MANIFEST_NAME = "migration_manifest.json"
LAUNCHER_PATH = "launcher/LiveClipperWeb.exe"
CORE_BRIDGE_PATH = "core_bridge"
BUSINESS_ARCHIVE_PATH = "business.zip"
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class MigrationPackageError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedMigrationPackage:
    root: Path
    source_version: str
    application_version: str
    core_version: str
    launcher: Path
    core_bridge: Path
    business_archive: Path
    business: VerifiedBusinessBundle
    core: VerifiedCoreManifest


def _safe_version(value: object, *, label: str) -> str:
    version = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise MigrationPackageError(f"invalid {label}: {version!r}")
    return version


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & REPARSE_POINT_ATTRIBUTE)
    except (AttributeError, OSError):
        return False


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise MigrationPackageError(f"cannot read migration manifest: {path}") from exc
    if not isinstance(value, dict):
        raise MigrationPackageError("migration manifest must be a JSON object")
    return value


def _file_meta(path: Path) -> dict[str, Any]:
    return {"size": path.stat().st_size, "sha256": sha256_file(path)}


def _validated_meta(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"path", "size", "sha256"}:
        raise MigrationPackageError(f"invalid {label} metadata")
    try:
        size = int(value["size"])
    except (TypeError, ValueError) as exc:
        raise MigrationPackageError(f"invalid {label} size") from exc
    digest = str(value["sha256"] or "").strip().lower()
    if size < 0 or not SHA256_PATTERN.fullmatch(digest):
        raise MigrationPackageError(f"invalid {label} digest")
    return {"path": str(value["path"]), "size": size, "sha256": digest}


def _verify_file(path: Path, metadata: dict[str, Any], *, label: str) -> None:
    if not path.is_file() or _is_link_like(path):
        raise MigrationPackageError(f"{label} is missing or unsafe")
    if path.stat().st_size != metadata["size"] or sha256_file(path) != metadata["sha256"]:
        raise MigrationPackageError(f"{label} size or SHA256 mismatch")


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or _is_link_like(source):
        raise MigrationPackageError(f"migration source directory is unsafe: {source}")
    for path in source.rglob("*"):
        if _is_link_like(path):
            raise MigrationPackageError(f"migration source contains a link: {path}")
    shutil.copytree(source, destination)


def build_migration_package(
    *,
    source_version: str,
    application_version: str,
    core_version: str,
    launcher_path: str | Path,
    core_bridge_path: str | Path,
    business_archive_path: str | Path,
    private_key_path: str | Path,
    public_key_path: str | Path,
    output_root: str | Path,
) -> VerifiedMigrationPackage:
    source = _safe_version(source_version, label="source version")
    application = _safe_version(application_version, label="application version")
    core_version = _safe_version(core_version, label="core version")
    launcher = Path(launcher_path).resolve()
    bridge = Path(core_bridge_path).resolve()
    business_archive = Path(business_archive_path).resolve()
    output = Path(output_root).resolve()
    if output.exists():
        raise MigrationPackageError(f"migration package output already exists: {output}")
    if not launcher.is_file() or _is_link_like(launcher):
        raise MigrationPackageError("V4 root launcher is missing or unsafe")
    core = verify_core_manifest(
        bridge,
        public_key_path,
        expected_version=core_version,
    )
    business = verify_business_archive(
        business_archive,
        public_key_path,
        expected_version=application,
        expected_core_version=core_version,
    )

    output.mkdir(parents=True)
    try:
        packaged_launcher = output / LAUNCHER_PATH
        packaged_launcher.parent.mkdir(parents=True)
        shutil.copy2(launcher, packaged_launcher)
        packaged_bridge = output / CORE_BRIDGE_PATH
        _copy_tree(bridge, packaged_bridge)
        packaged_business = output / BUSINESS_ARCHIVE_PATH
        shutil.copy2(business_archive, packaged_business)
        manifest = sign_manifest(
            {
                "schema_version": MIGRATION_SCHEMA_VERSION,
                "format": MIGRATION_FORMAT,
                "runtime_layout_version": RUNTIME_LAYOUT_VERSION,
                "source": {
                    "runtime_layout_version": 3,
                    "version": source,
                },
                "target": {
                    "application_version": application,
                    "core_version": core_version,
                },
                "launcher": {"path": LAUNCHER_PATH, **_file_meta(packaged_launcher)},
                "core_bridge": {
                    "path": CORE_BRIDGE_PATH,
                    "manifest_sha256": core.manifest_sha256,
                },
                "business": {
                    "path": BUSINESS_ARCHIVE_PATH,
                    **_file_meta(packaged_business),
                    "manifest_sha256": business.manifest_sha256,
                },
            },
            private_key_path,
        )
        (output / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return verify_migration_package(output, public_key_path)


def verify_migration_package(
    package_root: str | Path,
    public_key_path: str | Path,
) -> VerifiedMigrationPackage:
    root = Path(package_root).resolve()
    if not root.is_dir() or _is_link_like(root):
        raise MigrationPackageError(f"migration package root is invalid: {root}")
    top_level = {path.name for path in root.iterdir()}
    expected_top_level = {
        MANIFEST_NAME,
        "launcher",
        CORE_BRIDGE_PATH,
        BUSINESS_ARCHIVE_PATH,
    }
    if top_level != expected_top_level:
        raise MigrationPackageError("migration package top-level file set mismatch")
    manifest = _load_json(root / MANIFEST_NAME)
    try:
        verify_manifest(manifest, public_key_path)
    except Exception as exc:
        raise MigrationPackageError("migration manifest signature verification failed") from exc
    if set(manifest) != {
        "schema_version",
        "format",
        "runtime_layout_version",
        "source",
        "target",
        "launcher",
        "core_bridge",
        "business",
        "signature",
    }:
        raise MigrationPackageError("migration manifest fields are unsupported")
    if manifest.get("schema_version") != MIGRATION_SCHEMA_VERSION:
        raise MigrationPackageError("migration manifest schema is unsupported")
    if manifest.get("format") != MIGRATION_FORMAT:
        raise MigrationPackageError("migration manifest format is unsupported")
    if manifest.get("runtime_layout_version") != RUNTIME_LAYOUT_VERSION:
        raise MigrationPackageError("migration target layout is unsupported")

    source_raw = manifest.get("source")
    if not isinstance(source_raw, dict) or set(source_raw) != {
        "runtime_layout_version",
        "version",
    }:
        raise MigrationPackageError("migration source metadata is invalid")
    if source_raw.get("runtime_layout_version") != 3:
        raise MigrationPackageError("migration source layout is unsupported")
    source_version = _safe_version(source_raw.get("version"), label="source version")
    target_raw = manifest.get("target")
    if not isinstance(target_raw, dict) or set(target_raw) != {
        "application_version",
        "core_version",
    }:
        raise MigrationPackageError("migration target metadata is invalid")
    application_version = _safe_version(
        target_raw.get("application_version"),
        label="application version",
    )
    core_version = _safe_version(target_raw.get("core_version"), label="core version")
    if application_version == source_version:
        raise MigrationPackageError("V4 application version collides with the V3 source")

    launcher_meta = _validated_meta(manifest.get("launcher"), label="launcher")
    if launcher_meta["path"] != LAUNCHER_PATH:
        raise MigrationPackageError("migration launcher path is unsupported")
    launcher = root / LAUNCHER_PATH
    _verify_file(launcher, launcher_meta, label="V4 root launcher")

    bridge_raw = manifest.get("core_bridge")
    if not isinstance(bridge_raw, dict) or set(bridge_raw) != {
        "path",
        "manifest_sha256",
    }:
        raise MigrationPackageError("migration Core bridge metadata is invalid")
    if bridge_raw.get("path") != CORE_BRIDGE_PATH:
        raise MigrationPackageError("migration Core bridge path is unsupported")
    expected_core_manifest = str(bridge_raw.get("manifest_sha256") or "").lower()
    if not SHA256_PATTERN.fullmatch(expected_core_manifest):
        raise MigrationPackageError("migration Core manifest digest is invalid")
    bridge = root / CORE_BRIDGE_PATH
    core = verify_core_manifest(
        bridge,
        public_key_path,
        expected_version=core_version,
    )
    if core.manifest_sha256 != expected_core_manifest:
        raise MigrationPackageError("migration Core manifest digest mismatch")

    business_raw = manifest.get("business")
    if not isinstance(business_raw, dict) or set(business_raw) != {
        "path",
        "size",
        "sha256",
        "manifest_sha256",
    }:
        raise MigrationPackageError("migration business metadata is invalid")
    business_meta = _validated_meta(
        {
            "path": business_raw.get("path"),
            "size": business_raw.get("size"),
            "sha256": business_raw.get("sha256"),
        },
        label="business archive",
    )
    if business_meta["path"] != BUSINESS_ARCHIVE_PATH:
        raise MigrationPackageError("migration business archive path is unsupported")
    expected_business_manifest = str(
        business_raw.get("manifest_sha256") or ""
    ).lower()
    if not SHA256_PATTERN.fullmatch(expected_business_manifest):
        raise MigrationPackageError("migration business manifest digest is invalid")
    business_archive = root / BUSINESS_ARCHIVE_PATH
    _verify_file(business_archive, business_meta, label="V4 business archive")
    business = verify_business_archive(
        business_archive,
        public_key_path,
        expected_version=application_version,
        expected_core_version=core_version,
    )
    if business.manifest_sha256 != expected_business_manifest:
        raise MigrationPackageError("migration business manifest digest mismatch")
    return VerifiedMigrationPackage(
        root=root,
        source_version=source_version,
        application_version=application_version,
        core_version=core_version,
        launcher=launcher,
        core_bridge=bridge,
        business_archive=business_archive,
        business=business,
        core=core,
    )


__all__ = [
    "MigrationPackageError",
    "VerifiedMigrationPackage",
    "build_migration_package",
    "verify_migration_package",
]
