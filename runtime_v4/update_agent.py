"""Atomic business-only installer for Runtime V4."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from runtime_v4.business_bundle import (
    MAX_ARCHIVE_UNCOMPRESSED_SIZE,
    BundleVerificationError,
    extract_verified_business_archive,
    verify_business_directory,
)
from runtime_v4.launcher import (
    RUNTIME_LAYOUT_VERSION,
    STATE_FILE,
    STATE_SCHEMA_VERSION,
    RuntimeSelection,
    _atomic_write_json,
    _load_json,
    _public_key_path,
    _safe_version,
    _selection,
)


STAGING_DIR = ".v4-staging"
LOCK_FILE = ".v4-update.lock"
MIN_FREE_SPACE_MARGIN = 64 * 1024 * 1024
DOWNLOAD_CACHE_MARKER = ".liveclipper-v4-download-cache"


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class BusinessInstallResult:
    application_version: str
    core_version: str
    manifest_sha256: str
    activated: bool
    already_installed: bool
    state_path: Path
    business_root: Path


@dataclass(frozen=True)
class BusinessRetentionResult:
    removed_versions: tuple[str, ...]
    retained_versions: tuple[str, ...]
    skipped: tuple[str, ...]


@dataclass(frozen=True)
class DownloadRetentionResult:
    removed: tuple[str, ...]
    retained: tuple[str, ...]
    skipped: tuple[str, ...]


def _load_state(install_root: Path) -> tuple[Path, dict[str, Any], RuntimeSelection]:
    state_path = install_root / STATE_FILE
    state = _load_json(state_path)
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise UpdateError("current.json has an unsupported Runtime V4 schema")
    if state.get("runtime_layout_version") != RUNTIME_LAYOUT_VERSION:
        raise UpdateError("current.json is not a Runtime V4 state file")
    try:
        current = _selection(state.get("current"), label="current")
    except Exception as exc:
        raise UpdateError(str(exc)) from exc
    return state_path, state, current


def _archive_uncompressed_size(archive_path: Path) -> int:
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            total = sum(info.file_size for info in archive.infolist() if not info.is_dir())
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("cannot inspect the Runtime V4 business archive") from exc
    if total < 0 or total > MAX_ARCHIVE_UNCOMPRESSED_SIZE:
        raise UpdateError("Runtime V4 business archive expands beyond its limit")
    return total


def _check_free_space(install_root: Path, archive_path: Path) -> None:
    required = _archive_uncompressed_size(archive_path) + MIN_FREE_SPACE_MARGIN
    free = shutil.disk_usage(install_root).free
    if free < required:
        raise UpdateError(
            "insufficient disk space for Runtime V4 business update: "
            f"required={required}, free={free}"
        )


@contextlib.contextmanager
def _update_lock(install_root: Path) -> Iterator[None]:
    lock_path = install_root / LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateError("another Runtime V4 update is already running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _staging_root(install_root: Path) -> Path:
    root = (install_root / STAGING_DIR).resolve()
    expected_parent = install_root.resolve()
    try:
        root.relative_to(expected_parent)
    except ValueError as exc:
        raise UpdateError("Runtime V4 staging directory escapes the installation") from exc
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cleanup_staging(staging_root: Path, *, keep: Path | None = None) -> None:
    for path in staging_root.iterdir():
        if keep is not None and path == keep:
            continue
        if path.is_dir() and path.name.startswith("business-"):
            shutil.rmtree(path, ignore_errors=True)


def _fault(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _state_versions(state: dict[str, Any]) -> set[str]:
    protected: set[str] = set()
    for field in ("current", "previous", "failed"):
        value = state.get(field)
        if not isinstance(value, dict):
            continue
        version = str(value.get("application_version") or "").strip()
        try:
            protected.add(_safe_version(version, label=f"{field} application version"))
        except Exception:
            continue
    return protected


def prune_business_versions(
    install_root: str | Path,
    *,
    keep_recent_unreferenced: int = 1,
) -> BusinessRetentionResult:
    """Remove only unreferenced, ordinary V4 business version directories."""

    install_root = Path(install_root).resolve()
    keep_recent = max(0, int(keep_recent_unreferenced))
    with _update_lock(install_root):
        _, state, _ = _load_state(install_root)
        raw_versions_root = install_root / "versions"
        if _is_link_like(raw_versions_root):
            raise UpdateError("Runtime V4 versions root is unsafe")
        versions_root = raw_versions_root.resolve()
        try:
            versions_root.relative_to(install_root)
        except ValueError as exc:
            raise UpdateError("Runtime V4 versions root escapes the installation") from exc
        versions_root.mkdir(parents=True, exist_ok=True)
        protected = _state_versions(state)
        ordinary: list[tuple[float, str, Path]] = []
        skipped: list[str] = []
        for path in versions_root.iterdir():
            if not path.is_dir() or _is_link_like(path):
                skipped.append(path.name)
                continue
            try:
                version = _safe_version(path.name, label="installed application version")
                resolved = path.resolve()
                resolved.relative_to(versions_root)
            except Exception:
                skipped.append(path.name)
                continue
            children = {child.name for child in path.iterdir()}
            business = path / "business"
            if children != {"business"} or not business.is_dir() or _is_link_like(business):
                skipped.append(version)
                continue
            ordinary.append((path.stat().st_mtime, version, path))

        recent_versions = [
            version
            for _, version, _ in sorted(ordinary, reverse=True)
            if version not in protected
        ]
        recent = set(recent_versions[:keep_recent])
        retained = protected | recent
        removed: list[str] = []
        for _, version, path in ordinary:
            if version in retained:
                continue
            shutil.rmtree(path)
            removed.append(version)
        existing = sorted(version for _, version, path in ordinary if path.exists())
        return BusinessRetentionResult(
            removed_versions=tuple(sorted(removed)),
            retained_versions=tuple(existing),
            skipped=tuple(sorted(set(skipped))),
        )


def cleanup_download_cache(
    download_root: str | Path,
    *,
    keep_recent_directories: int = 2,
    stale_partial_days: int = 14,
) -> DownloadRetentionResult:
    """Bound V4 download cache growth without following links or foreign paths."""

    raw_root = Path(download_root).absolute()
    if _is_link_like(raw_root):
        raise UpdateError("Runtime V4 download cache root is unsafe")
    root = raw_root.resolve()
    if not root.exists():
        return DownloadRetentionResult((), (), ())
    if not root.is_dir():
        raise UpdateError("Runtime V4 download cache root is unsafe")
    marker = root / DOWNLOAD_CACHE_MARKER
    try:
        marker_value = marker.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise UpdateError("Runtime V4 download cache marker is missing") from exc
    if marker_value != "runtime_layout_version=4":
        raise UpdateError("Runtime V4 download cache marker is invalid")
    keep_recent = max(0, int(keep_recent_directories))
    cutoff = time.time() - max(1, int(stale_partial_days)) * 24 * 60 * 60
    directories: list[tuple[float, str, Path]] = []
    removed: list[str] = []
    skipped: list[str] = []
    for path in root.iterdir():
        if _is_link_like(path):
            skipped.append(path.name)
            continue
        if path.is_file():
            if path.name.endswith(".part") and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path.name)
            else:
                skipped.append(path.name)
            continue
        if not path.is_dir():
            skipped.append(path.name)
            continue
        try:
            _safe_version(path.name, label="download cache version")
            path.resolve().relative_to(root)
        except Exception:
            skipped.append(path.name)
            continue
        directories.append((path.stat().st_mtime, path.name, path))

    retained_names = {
        name for _, name, _ in sorted(directories, reverse=True)[:keep_recent]
    }
    for _, name, path in directories:
        if name in retained_names:
            for partial in path.glob("*.part"):
                if not _is_link_like(partial) and partial.stat().st_mtime < cutoff:
                    partial.unlink()
                    removed.append(f"{name}/{partial.name}")
            continue
        shutil.rmtree(path)
        removed.append(name)
    retained = sorted(name for _, name, path in directories if path.exists())
    return DownloadRetentionResult(
        removed=tuple(sorted(removed)),
        retained=tuple(retained),
        skipped=tuple(sorted(set(skipped))),
    )


def initialize_download_cache(download_root: str | Path) -> Path:
    raw_root = Path(download_root).absolute()
    if _is_link_like(raw_root):
        raise UpdateError("Runtime V4 download cache root is unsafe")
    root = raw_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / DOWNLOAD_CACHE_MARKER
    expected = "runtime_layout_version=4\n"
    if marker.exists():
        try:
            if marker.read_text(encoding="ascii") != expected:
                raise UpdateError("Runtime V4 download cache marker is invalid")
        except OSError as exc:
            raise UpdateError("cannot read Runtime V4 download cache marker") from exc
    else:
        marker.write_text(expected, encoding="ascii")
    return root


def install_business_archive(
    install_root: str | Path,
    archive_path: str | Path,
    *,
    application_version: str,
    public_key_path: str | Path | None = None,
    activate: bool = True,
    expected_current_version: str | None = None,
    expected_core_version: str | None = None,
    expected_manifest_sha256: str | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> BusinessInstallResult:
    """Install one immutable business bundle and atomically activate it."""

    install_root = Path(install_root).resolve()
    archive_path = Path(archive_path).resolve()
    if not install_root.is_dir():
        raise UpdateError(f"Runtime V4 installation root does not exist: {install_root}")
    if not archive_path.is_file():
        raise UpdateError(f"Runtime V4 business archive does not exist: {archive_path}")
    try:
        target_version = _safe_version(
            application_version,
            label="target application version",
        )
    except Exception as exc:
        raise UpdateError(str(exc)) from exc
    public_key = (
        Path(public_key_path).resolve()
        if public_key_path is not None
        else _public_key_path(install_root)
    )
    expected_current = None
    if expected_current_version is not None:
        try:
            expected_current = _safe_version(
                expected_current_version,
                label="expected current application version",
            )
        except Exception as exc:
            raise UpdateError(str(exc)) from exc
    expected_core = None
    if expected_core_version is not None:
        try:
            expected_core = _safe_version(
                expected_core_version,
                label="expected current core version",
            )
        except Exception as exc:
            raise UpdateError(str(exc)) from exc
    expected_manifest = str(expected_manifest_sha256 or "").strip().lower() or None
    if expected_manifest is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_manifest):
        raise UpdateError("expected Runtime V4 business manifest SHA256 is invalid")

    with _update_lock(install_root):
        state_path, state, current = _load_state(install_root)
        if expected_current is not None and current.application_version != expected_current:
            raise UpdateError(
                "Runtime V4 current application changed before installation: "
                f"expected={expected_current}, actual={current.application_version}"
            )
        if expected_core is not None and current.core_version != expected_core:
            raise UpdateError(
                "Runtime V4 current core changed before installation: "
                f"expected={expected_core}, actual={current.core_version}"
            )
        staging_root = _staging_root(install_root)
        _cleanup_staging(staging_root)
        _check_free_space(install_root, archive_path)

        version_root = (install_root / "versions" / target_version).resolve()
        versions_root = (install_root / "versions").resolve()
        try:
            version_root.relative_to(versions_root)
        except ValueError as exc:
            raise UpdateError("Runtime V4 target version escapes the versions root") from exc
        target_business = version_root / "business"
        already_installed = False
        verified = None
        staging = staging_root / f"business-{target_version}-{uuid.uuid4().hex[:12]}"

        try:
            try:
                extracted = extract_verified_business_archive(
                    archive_path,
                    staging,
                    public_key,
                    expected_version=target_version,
                    expected_core_version=current.core_version,
                )
            except BundleVerificationError as exc:
                raise UpdateError(str(exc)) from exc
            if (
                expected_manifest is not None
                and extracted.manifest_sha256 != expected_manifest
            ):
                raise UpdateError(
                    "Runtime V4 business manifest does not match the signed update channel"
                )
            _fault(fault_injector, "after_verify")
            if target_business.exists():
                try:
                    verified = verify_business_directory(
                        target_business,
                        public_key,
                        expected_version=target_version,
                        expected_core_version=current.core_version,
                    )
                except Exception as exc:
                    raise UpdateError(
                        f"existing Runtime V4 business version is invalid: {target_version}"
                    ) from exc
                if verified.manifest_sha256 != extracted.manifest_sha256:
                    raise UpdateError(
                        "existing Runtime V4 business version does not match the requested archive: "
                        f"{target_version}"
                    )
                already_installed = True
            else:
                version_root.mkdir(parents=True, exist_ok=True)
                if any(version_root.iterdir()):
                    raise UpdateError(
                        f"Runtime V4 target version directory is not empty: {target_version}"
                    )
                os.replace(extracted.root, target_business)
                _fault(fault_injector, "after_move")
                verified = verify_business_directory(
                    target_business,
                    public_key,
                    expected_version=target_version,
                    expected_core_version=current.core_version,
                )

            if verified is None:
                raise UpdateError("Runtime V4 business verification produced no result")
            activated = bool(
                activate and current.application_version != target_version
            )
            if activated:
                state.update(
                    {
                        "current": RuntimeSelection(
                            target_version,
                            current.core_version,
                        ).as_dict(),
                        "previous": current.as_dict(),
                        "pending": True,
                        "update_transaction_id": uuid.uuid4().hex,
                        "activated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "rollback_reason": "",
                    }
                )
                _atomic_write_json(state_path, state)
                _fault(fault_injector, "after_state")
            return BusinessInstallResult(
                application_version=target_version,
                core_version=current.core_version,
                manifest_sha256=verified.manifest_sha256,
                activated=activated,
                already_installed=already_installed,
                state_path=state_path,
                business_root=target_business,
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            _cleanup_staging(staging_root)


__all__ = [
    "BusinessInstallResult",
    "BusinessRetentionResult",
    "DownloadRetentionResult",
    "UpdateError",
    "cleanup_download_cache",
    "initialize_download_cache",
    "install_business_archive",
    "prune_business_versions",
]
