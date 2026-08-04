"""Construct a verified Runtime V4 core from a signed Runtime V3 install."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from app.release_signing import verify_manifest
from runtime_v4.core_manifest import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    VerifiedCore,
    VerifiedCoreManifest,
    verify_core_directory,
    verify_core_manifest,
)


V3_RUNTIME_LAYOUT_VERSION = 3
V3_MANIFEST_SCHEMA_VERSION = 3
V3_MANIFEST_FORMAT = "liveclipper-runtime-manifest-v1"
V3_ENTRYPOINT = "LiveClipperWeb.exe"
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyRuntime:
    install_root: Path
    runtime_root: Path
    version: str
    files: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class CoreReusePlan:
    source_version: str
    core_version: str
    target_files: int
    target_bytes: int
    reusable_files: int
    reusable_bytes: int
    payload_files: tuple[str, ...]
    payload_bytes: int


@dataclass(frozen=True)
class CoreAssemblyResult:
    core: VerifiedCore
    plan: CoreReusePlan
    hardlinked_files: int
    payload_files: int


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise MigrationError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"{label} must be a JSON object")
    return value


def _safe_version(value: object, *, label: str) -> str:
    version = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise MigrationError(f"invalid {label}: {version!r}")
    return version


def _safe_relative_path(value: object, *, label: str) -> str:
    text = str(value or "")
    if not text or "\\" in text or "\x00" in text or ":" in text:
        raise MigrationError(f"invalid {label}: {text!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MigrationError(f"unsafe {label}: {text!r}")
    if path.as_posix() != text:
        raise MigrationError(f"non-canonical {label}: {text!r}")
    return text


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & REPARSE_POINT_ATTRIBUTE)
    except (AttributeError, OSError):
        return False


def _owned_path(root: Path, relative: str, *, label: str) -> Path:
    target = root / Path(*PurePosixPath(relative).parts)
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MigrationError(f"{label} escapes its root: {relative}") from exc
    return target


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _validated_files(value: object, *, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise MigrationError(f"{label} file manifest is empty")
    result: dict[str, dict[str, Any]] = {}
    for raw_relative, raw_meta in value.items():
        relative = _safe_relative_path(raw_relative, label=f"{label} file path")
        if relative in result:
            raise MigrationError(f"duplicate {label} file: {relative}")
        if not isinstance(raw_meta, dict) or set(raw_meta) != {"sha256", "size"}:
            raise MigrationError(f"invalid {label} file metadata: {relative}")
        try:
            size = int(raw_meta["size"])
        except (TypeError, ValueError) as exc:
            raise MigrationError(f"invalid {label} file size: {relative}") from exc
        digest = str(raw_meta["sha256"] or "").strip().lower()
        if size < 0 or not SHA256_PATTERN.fullmatch(digest):
            raise MigrationError(f"invalid {label} file digest: {relative}")
        result[relative] = {"sha256": digest, "size": size}
    return result


def inspect_v3_install(
    install_root: str | Path,
    public_key_path: str | Path,
    *,
    expected_version: str | None = None,
) -> LegacyRuntime:
    root = Path(install_root).resolve()
    if not root.is_dir() or _is_link_like(root):
        raise MigrationError(f"Runtime V3 install root is invalid: {root}")
    state = _load_json(root / "current.json", label="Runtime V3 state")
    if state.get("schema_version") != 1:
        raise MigrationError("Runtime V3 state schema is unsupported")
    if state.get("runtime_layout_version") != V3_RUNTIME_LAYOUT_VERSION:
        raise MigrationError("source installation is not Runtime V3")
    version = _safe_version(state.get("current_version"), label="Runtime V3 version")
    if expected_version is not None and version != str(expected_version):
        raise MigrationError(
            f"Runtime V3 version mismatch: expected {expected_version}, got {version}"
        )
    versions_root = (root / "versions").resolve()
    runtime_root = (versions_root / version).resolve()
    try:
        runtime_root.relative_to(versions_root)
    except ValueError as exc:
        raise MigrationError("Runtime V3 directory escapes versions root") from exc
    if not runtime_root.is_dir() or _is_link_like(runtime_root):
        raise MigrationError(f"Runtime V3 directory is missing or unsafe: {runtime_root}")

    manifest = _load_json(
        runtime_root / "runtime_manifest.json",
        label="Runtime V3 manifest",
    )
    try:
        verify_manifest(manifest, Path(public_key_path).resolve())
    except Exception as exc:
        raise MigrationError("Runtime V3 manifest signature verification failed") from exc
    if manifest.get("schema_version") != V3_MANIFEST_SCHEMA_VERSION:
        raise MigrationError("Runtime V3 manifest schema is unsupported")
    if manifest.get("format") != V3_MANIFEST_FORMAT:
        raise MigrationError("Runtime V3 manifest format is unsupported")
    if manifest.get("runtime_layout_version") != V3_RUNTIME_LAYOUT_VERSION:
        raise MigrationError("Runtime V3 manifest layout mismatch")
    if str(manifest.get("version") or "") != version:
        raise MigrationError("Runtime V3 state and manifest versions differ")
    if manifest.get("entrypoint") != V3_ENTRYPOINT:
        raise MigrationError("Runtime V3 entrypoint is unsupported")
    files = _validated_files(manifest.get("files"), label="Runtime V3")
    if V3_ENTRYPOINT not in files:
        raise MigrationError("Runtime V3 entrypoint is absent from its manifest")
    return LegacyRuntime(root, runtime_root, version, files)


def plan_core_reuse(
    legacy: LegacyRuntime,
    target: VerifiedCoreManifest,
) -> CoreReusePlan:
    reusable_files = 0
    reusable_bytes = 0
    payload: list[str] = []
    payload_bytes = 0
    target_bytes = 0
    for relative, raw_meta in target.files.items():
        meta = {"sha256": str(raw_meta["sha256"]), "size": int(raw_meta["size"])}
        target_bytes += meta["size"]
        if legacy.files.get(relative) == meta:
            reusable_files += 1
            reusable_bytes += meta["size"]
        else:
            payload.append(relative)
            payload_bytes += meta["size"]
    return CoreReusePlan(
        source_version=legacy.version,
        core_version=target.core_version,
        target_files=len(target.files),
        target_bytes=target_bytes,
        reusable_files=reusable_files,
        reusable_bytes=reusable_bytes,
        payload_files=tuple(payload),
        payload_bytes=payload_bytes,
    )


def build_core_bridge(
    legacy: LegacyRuntime,
    target_core_root: str | Path,
    public_key_path: str | Path,
    output_root: str | Path,
) -> CoreReusePlan:
    target = verify_core_directory(target_core_root, public_key_path, hash_mode="full")
    blueprint = verify_core_manifest(
        target.root,
        public_key_path,
        expected_version=target.core_version,
    )
    plan = plan_core_reuse(legacy, blueprint)
    output = Path(output_root).resolve()
    if output.exists():
        raise MigrationError(f"V4 core bridge output already exists: {output}")
    output.mkdir(parents=True)
    try:
        shutil.copy2(target.root / MANIFEST_NAME, output / MANIFEST_NAME)
        shutil.copy2(target.root / SIGNATURE_NAME, output / SIGNATURE_NAME)
        payload_root = output / "payload"
        for relative in plan.payload_files:
            source = _owned_path(target.root, relative, label="target core file")
            destination = _owned_path(payload_root, relative, label="bridge payload file")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        (output / "bridge_plan.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "runtime_layout_version": 4,
                    "source_version": plan.source_version,
                    "core_version": plan.core_version,
                    "core_manifest_sha256": target.manifest_sha256,
                    "target_files": plan.target_files,
                    "target_bytes": plan.target_bytes,
                    "reusable_files": plan.reusable_files,
                    "reusable_bytes": plan.reusable_bytes,
                    "payload_files": len(plan.payload_files),
                    "payload_bytes": plan.payload_bytes,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return plan


def assemble_core_from_v3(
    legacy: LegacyRuntime,
    bridge_root: str | Path,
    destination: str | Path,
    public_key_path: str | Path,
    *,
    expected_core_version: str | None = None,
) -> CoreAssemblyResult:
    bridge = Path(bridge_root).resolve()
    if not bridge.is_dir() or _is_link_like(bridge):
        raise MigrationError(f"V4 core bridge is invalid: {bridge}")
    blueprint = verify_core_manifest(
        bridge,
        public_key_path,
        expected_version=expected_core_version,
    )
    plan = plan_core_reuse(legacy, blueprint)
    payload_root = (bridge / "payload").resolve()
    target = Path(destination).resolve()
    if target.exists():
        raise MigrationError(f"V4 core destination already exists: {target}")
    if target == legacy.runtime_root or legacy.runtime_root in target.parents:
        raise MigrationError("V4 core destination overlaps the Runtime V3 source")
    target.mkdir(parents=True)
    hardlinked = 0
    copied = 0
    try:
        for relative, raw_meta in blueprint.files.items():
            meta = {"sha256": str(raw_meta["sha256"]), "size": int(raw_meta["size"])}
            destination_file = _owned_path(target, relative, label="V4 core file")
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            if legacy.files.get(relative) == meta:
                source = _owned_path(
                    legacy.runtime_root,
                    relative,
                    label="Runtime V3 reuse file",
                )
                if not source.is_file() or _is_link_like(source):
                    raise MigrationError(f"Runtime V3 reuse file is missing or unsafe: {relative}")
                if source.stat().st_size != meta["size"]:
                    raise MigrationError(f"Runtime V3 reuse file size mismatch: {relative}")
                try:
                    os.link(source, destination_file)
                except OSError as exc:
                    raise MigrationError(
                        f"cannot hard-link Runtime V3 file into V4 core: {relative}"
                    ) from exc
                hardlinked += 1
                continue

            source = _owned_path(payload_root, relative, label="bridge payload file")
            if not source.is_file() or _is_link_like(source):
                raise MigrationError(f"V4 bridge payload is missing or unsafe: {relative}")
            if source.stat().st_size != meta["size"] or _sha256_file(source) != meta["sha256"]:
                raise MigrationError(f"V4 bridge payload digest mismatch: {relative}")
            shutil.copy2(source, destination_file)
            copied += 1

        shutil.copy2(bridge / MANIFEST_NAME, target / MANIFEST_NAME)
        shutil.copy2(bridge / SIGNATURE_NAME, target / SIGNATURE_NAME)
        verified = verify_core_directory(
            target,
            public_key_path,
            expected_version=blueprint.core_version,
            hash_mode="full",
        )
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError("assembled V4 core failed full verification") from exc
    return CoreAssemblyResult(verified, plan, hardlinked, copied)


__all__ = [
    "CoreAssemblyResult",
    "CoreReusePlan",
    "LegacyRuntime",
    "MigrationError",
    "assemble_core_from_v3",
    "build_core_bridge",
    "inspect_v3_install",
    "plan_core_reuse",
]
