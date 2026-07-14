"""Apply signed LiveClipper patches into immutable runtime version directories."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

APP_IMPORT_DIR = Path(__file__).resolve().parents[1] / "app"
if APP_IMPORT_DIR.is_dir() and str(APP_IMPORT_DIR) not in sys.path:
    sys.path.insert(0, str(APP_IMPORT_DIR))


from release_signing import (
    canonical_manifest_bytes,
    sha256_file,
    verify_manifest,
)


PATCH_FORMAT = "liveclipper-version-delta-v1"
RUNTIME_MANIFEST_FORMAT = "liveclipper-runtime-manifest-v1"
INSTALL_MANIFEST_FORMAT = "liveclipper-install-manifest-v1"
PATCH_MANIFEST = "patch_manifest.json"
RUNTIME_MANIFEST = "runtime_manifest.json"
INSTALL_MANIFEST = "install_manifest.json"
STATE_FILE = "current.json"
DEFAULT_ENTRYPOINT = "LiveClipperWeb.exe"
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,79}$")
RUNTIME_OWNED_ENV = (
    "LIVECLIPPER_BUNDLE_DIR",
    "LIVECLIPPER_FROZEN",
    "LIVECLIPPER_CODE_SOURCE",
    "LIVECLIPPER_LEGACY_OVERLAYS_PRESENT",
    "LIVECLIPPER_INSTALL_ROOT",
    "LIVECLIPPER_ACTIVE_VERSION",
    "LIVECLIPPER_RUNTIME_LAYOUT",
    "LIVECLIPPER_LAUNCHER_VERSION",
    "LIVECLIPPER_HEALTH_FILE",
    "LIVECLIPPER_HEALTH_TOKEN",
    "LIVECLIPPER_ROLLBACK_REASON",
)
ProgressCallback = Callable[[int, str], None]


class UpdateError(RuntimeError):
    pass


def _safe_version(value: Any) -> str:
    version = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(version) or version.startswith("."):
        raise UpdateError(f"invalid version: {value!r}")
    return version


def _safe_relative_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text or text.startswith(("/", "./", "../")):
        raise UpdateError(f"unsafe patch path: {value!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UpdateError(f"unsafe patch path: {value!r}")
    if ":" in path.parts[0] or path.parts[0].lower().startswith(".update-"):
        raise UpdateError(f"unsafe patch path: {value!r}")
    return path.as_posix()


def _target_path(root: Path, relative: str) -> Path:
    safe = _safe_relative_path(relative)
    root = Path(os.path.abspath(root))
    target = root / Path(*PurePosixPath(safe).parts)
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise UpdateError(f"path escapes root: {relative}")
    return target


def _manifest_path(root: Path, validated_relative: str) -> Path:
    return root / Path(*PurePosixPath(validated_relative).parts)


def _launcher_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in RUNTIME_OWNED_ENV:
        env.pop(name, None)
    return env


def _data_root() -> Path:
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or tempfile.gettempdir()
    )
    return base / "LiveClipper"


def _write_log(message: str) -> None:
    path = _data_root() / "update_logs" / "updater.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def _emit_progress(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback is not None:
        callback(max(0, min(100, int(percent))), message)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise UpdateError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise UpdateError(f"{path.name} is not an object")
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:8]}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _wait_for_process(pid: int, timeout: float = 240.0) -> None:
    if pid <= 0:
        return
    if os.name != "nt":
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.25)
        raise UpdateError(f"process {pid} did not exit")

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return
    try:
        result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
        if result == wait_timeout:
            raise UpdateError(f"process {pid} did not exit")
    finally:
        kernel32.CloseHandle(handle)


def _replace_with_retry(source: Path, destination: Path, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.4)
    raise UpdateError(f"cannot replace {destination}: {last_error}")


def _public_key_path(argument: str, install_root: Path) -> Path:
    candidates: list[Path] = []
    if argument:
        candidates.append(Path(argument))
    candidates.extend(
        [
            install_root / "updater" / "release_update_public_key.pem",
            Path(getattr(sys, "_MEIPASS", "")) / "app" / "release_update_public_key.pem",
            Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
            / "release_update_public_key.pem",
            Path(__file__).resolve().parent.parent / "app" / "release_update_public_key.pem",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise UpdateError("release update public key is missing")


def _validate_runtime_manifest(
    manifest: dict[str, Any],
    public_key: Path,
    expected_version: str,
) -> dict[str, dict[str, Any]]:
    verify_manifest(manifest, public_key)
    if manifest.get("format") != RUNTIME_MANIFEST_FORMAT:
        raise UpdateError("unsupported runtime manifest format")
    if _safe_version(manifest.get("version")) != expected_version:
        raise UpdateError("runtime manifest version mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise UpdateError("runtime manifest has no files")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_path, raw_meta in files.items():
        path = _safe_relative_path(raw_path)
        if not isinstance(raw_meta, dict):
            raise UpdateError(f"invalid runtime file metadata: {path}")
        sha256 = str(raw_meta.get("sha256") or "").lower()
        raw_size = raw_meta.get("size")
        if raw_size is None:
            raise UpdateError(f"invalid runtime file metadata: {path}")
        size = int(raw_size)
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or size < 0:
            raise UpdateError(f"invalid runtime file metadata: {path}")
        normalized[path] = {"sha256": sha256, "size": size}
    entrypoint = _safe_relative_path(manifest.get("entrypoint") or DEFAULT_ENTRYPOINT)
    if entrypoint not in normalized:
        raise UpdateError("runtime manifest does not cover its entrypoint")
    return normalized


def _load_patch(patch_path: Path, public_key: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(patch_path) as archive:
            manifest = json.loads(archive.read(PATCH_MANIFEST).decode("utf-8-sig"))
    except Exception as exc:
        raise UpdateError(f"invalid patch archive: {exc}") from exc
    if not isinstance(manifest, dict):
        raise UpdateError("patch manifest is not an object")
    verify_manifest(manifest, public_key)
    if manifest.get("format") != PATCH_FORMAT:
        raise UpdateError("unsupported patch format")
    if int(manifest.get("schema_version") or 0) != 3:
        raise UpdateError("unsupported patch schema")
    _safe_version(manifest.get("from_version"))
    _safe_version(manifest.get("to_version"))
    if int(manifest.get("target_layout_version") or 0) != 3:
        raise UpdateError("patch target is not Runtime V3")
    for group_name in ("runtime_payload", "stable_payload", "stable_result_files"):
        group = manifest.get(group_name) or {}
        if not isinstance(group, dict):
            raise UpdateError(f"{group_name} must be an object")
        for raw_path, raw_meta in group.items():
            _safe_relative_path(raw_path)
            if not isinstance(raw_meta, dict):
                raise UpdateError(f"invalid {group_name} metadata")
            if raw_meta.get("archive"):
                _safe_relative_path(raw_meta.get("archive"))
    return manifest


def _read_v2_version(root: Path) -> str:
    data = _load_json(root / "_internal" / "app" / "version.json")
    return _safe_version(data.get("version") or data.get("latest_version"))


def _detect_source(
    install_root: Path,
    expected_version: str,
    public_key: Path,
) -> tuple[int, Path, dict[str, Any] | None, dict[str, Any] | None]:
    state_path = install_root / STATE_FILE
    if state_path.is_file():
        state = _load_json(state_path)
        if int(state.get("runtime_layout_version") or 0) != 3:
            raise UpdateError("existing current.json is not Runtime V3")
        version = _safe_version(state.get("current_version"))
        if version != expected_version:
            raise UpdateError(f"patch requires {expected_version}, installed version is {version}")
        runtime_root = install_root / "versions" / version
        manifest = _load_json(runtime_root / RUNTIME_MANIFEST)
        _validate_runtime_manifest(manifest, public_key, version)
        return 3, runtime_root.resolve(), manifest, state

    version = _read_v2_version(install_root)
    if version != expected_version:
        raise UpdateError(f"patch requires {expected_version}, installed version is {version}")
    if not (install_root / DEFAULT_ENTRYPOINT).is_file():
        raise UpdateError("legacy install root has no LiveClipperWeb.exe")
    return 2, install_root.resolve(), None, None


def _verify_file_size(path: Path, meta: dict[str, Any], label: str) -> None:
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise UpdateError(f"{label} is missing: {path.name}") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise UpdateError(f"{label} is not a regular file: {path.name}")
    expected_size = meta.get("size")
    if expected_size is None or file_stat.st_size != int(expected_size):
        raise UpdateError(f"{label} size mismatch: {path.name}")


def _verify_file(path: Path, meta: dict[str, Any], label: str) -> None:
    _verify_file_size(path, meta, label)
    if sha256_file(path) != str(meta.get("sha256") or "").lower():
        raise UpdateError(f"{label} hash mismatch: {path.name}")


def _copy_or_link(source: Path, destination: Path) -> bool:
    try:
        os.link(source, destination)
        return True
    except OSError:
        shutil.copy2(source, destination)
        return False


def _extract_member(
    archive: zipfile.ZipFile,
    archive_name: str,
    destination: Path,
    meta: dict[str, Any],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".part-{uuid.uuid4().hex[:8]}")
    try:
        with archive.open(_safe_relative_path(archive_name)) as source, temporary.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        _verify_file(temporary, meta, "patch payload")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _construct_runtime(
    archive: zipfile.ZipFile,
    source_root: Path,
    destination: Path,
    runtime_manifest: dict[str, Any],
    public_key: Path,
    payload: dict[str, Any],
    *,
    progress: ProgressCallback | None = None,
    progress_start: int = 10,
    progress_end: int = 80,
    verify_reused: bool = False,
) -> None:
    version = _safe_version(runtime_manifest.get("version"))
    files = _validate_runtime_manifest(runtime_manifest, public_key, version)
    destination.mkdir(parents=True, exist_ok=False)
    source_root = source_root.resolve()
    destination = destination.resolve()
    created_parents: set[Path] = set()
    total = max(1, len(files))
    last_percent = -1
    _emit_progress(progress, progress_start, f"正在建立版本 {version}（0/{len(files)}）")
    for index, (relative, meta) in enumerate(files.items(), start=1):
        target = _manifest_path(destination, relative)
        parent = target.parent
        if parent not in created_parents:
            parent.mkdir(parents=True, exist_ok=True)
            created_parents.add(parent)
        payload_meta = payload.get(relative)
        if payload_meta:
            _extract_member(archive, payload_meta.get("archive"), target, payload_meta)
        else:
            source = _manifest_path(source_root, relative)
            hard_linked = _copy_or_link(source, target)
            if verify_reused or not hard_linked:
                _verify_file(target, meta, "constructed runtime file")
            else:
                _verify_file_size(target, meta, "reused runtime file")
        percent = progress_start + round((progress_end - progress_start) * index / total)
        if percent != last_percent:
            _emit_progress(progress, percent, f"正在建立版本 {version}（{index}/{len(files)}）")
            last_percent = percent
    _atomic_write_json(destination / RUNTIME_MANIFEST, runtime_manifest)


def _verify_runtime_directory(
    runtime_root: Path,
    runtime_manifest: dict[str, Any],
    public_key: Path,
) -> None:
    version = _safe_version(runtime_manifest.get("version"))
    files = _validate_runtime_manifest(runtime_manifest, public_key, version)
    runtime_root = runtime_root.resolve()
    for relative, meta in files.items():
        _verify_file(_manifest_path(runtime_root, relative), meta, "target runtime file")


def _stage_stable_files(
    archive: zipfile.ZipFile,
    staging: Path,
    stable_payload: dict[str, Any],
) -> None:
    for relative, meta in stable_payload.items():
        _extract_member(
            archive,
            meta.get("archive"),
            _target_path(staging, relative),
            meta,
        )


def _apply_stable_files(
    install_root: Path,
    staging: Path,
    stable_payload: dict[str, Any],
    backup: Path,
    operations: list[tuple[str, bool]] | None = None,
) -> list[tuple[str, bool]]:
    if operations is None:
        operations = []
    for relative, meta in stable_payload.items():
        destination = _target_path(install_root, relative)
        staged = _target_path(staging, relative)
        _verify_file(staged, meta, "staged stable file")
        existed = destination.is_file()
        if existed:
            backup_file = _target_path(backup, relative)
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup_file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + f".new-{uuid.uuid4().hex[:8]}")
        shutil.copy2(staged, temporary)
        _replace_with_retry(temporary, destination)
        operations.append((relative, existed))
    return operations


def _restore_stable_files(
    install_root: Path,
    backup: Path,
    operations: list[tuple[str, bool]],
) -> None:
    for relative, existed in reversed(operations):
        destination = _target_path(install_root, relative)
        backup_file = _target_path(backup, relative)
        try:
            if existed and backup_file.is_file():
                temporary = destination.with_name(destination.name + f".rollback-{uuid.uuid4().hex[:8]}")
                shutil.copy2(backup_file, temporary)
                _replace_with_retry(temporary, destination)
            elif not existed:
                destination.unlink(missing_ok=True)
        except Exception as exc:
            _write_log(f"stable rollback failed for {relative}: {exc}")


def _verify_stable_result(install_root: Path, files: dict[str, Any]) -> None:
    for relative, meta in files.items():
        _verify_file(_target_path(install_root, relative), meta, "stable component")


def _install_version_directory(
    versions_root: Path,
    staging: Path,
    version: str,
    runtime_manifest: dict[str, Any],
    public_key: Path,
) -> Path:
    final = versions_root / _safe_version(version)
    if final.exists():
        try:
            _verify_runtime_directory(final, runtime_manifest, public_key)
            shutil.rmtree(staging, ignore_errors=True)
            return final
        except Exception:
            quarantine = versions_root / f".failed-{version}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            os.replace(final, quarantine)
    os.replace(staging, final)
    return final


def _validate_runtime_plan(
    source_files: dict[str, dict[str, Any]],
    target_files: dict[str, dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    for relative, payload_meta in payload.items():
        target_meta = target_files.get(relative)
        if target_meta is None:
            raise UpdateError(f"patch payload is not in target manifest: {relative}")
        normalized_payload = {
            "sha256": str(payload_meta.get("sha256") or "").lower(),
            "size": int(payload_meta.get("size") or 0),
        }
        if normalized_payload != target_meta:
            raise UpdateError(f"patch payload does not match target manifest: {relative}")
    for relative, target_meta in target_files.items():
        if relative in payload:
            continue
        if source_files.get(relative) != target_meta:
            raise UpdateError(f"patch omits changed runtime file: {relative}")


def apply_patch(
    patch_path: Path,
    install_root: Path,
    public_key: Path,
    *,
    expected_patch_sha256: str = "",
    launch_after: bool = True,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    patch_path = patch_path.resolve()
    install_root = install_root.resolve()
    _emit_progress(progress, 1, "正在校验更新包")
    patch_sha256 = sha256_file(patch_path)
    if expected_patch_sha256:
        if patch_sha256 != expected_patch_sha256.strip().lower():
            raise UpdateError("patch archive SHA256 mismatch")
    manifest = _load_patch(patch_path, public_key)
    _emit_progress(progress, 5, "更新包签名校验通过")
    from_version = _safe_version(manifest.get("from_version"))
    to_version = _safe_version(manifest.get("to_version"))
    source_layout, source_root, local_source_manifest, old_state = _detect_source(
        install_root,
        from_version,
        public_key,
    )
    if source_layout != int(manifest.get("source_layout_version") or 0):
        raise UpdateError("patch source layout mismatch")
    _emit_progress(progress, 8, "当前版本校验通过")

    source_manifest = manifest.get("source_runtime_manifest")
    target_manifest = manifest.get("target_runtime_manifest")
    if not isinstance(source_manifest, dict) or not isinstance(target_manifest, dict):
        raise UpdateError("patch runtime manifests are missing")
    source_files = _validate_runtime_manifest(source_manifest, public_key, from_version)
    target_files = _validate_runtime_manifest(target_manifest, public_key, to_version)
    runtime_payload = manifest.get("runtime_payload") or {}
    _validate_runtime_plan(
        source_files,
        target_files,
        runtime_payload,
    )
    target_install_manifest = manifest.get("target_install_manifest")
    if not isinstance(target_install_manifest, dict):
        raise UpdateError("patch install manifest is missing")
    verify_manifest(target_install_manifest, public_key)
    if target_install_manifest.get("format") != INSTALL_MANIFEST_FORMAT:
        raise UpdateError("unsupported install manifest format")
    if _safe_version(target_install_manifest.get("initial_version")) != to_version:
        raise UpdateError("install manifest version mismatch")
    stable_result = target_install_manifest.get("files")
    if not isinstance(stable_result, dict) or stable_result != manifest.get("stable_result_files"):
        raise UpdateError("install manifest stable files do not match patch")
    if local_source_manifest is not None:
        if canonical_manifest_bytes(local_source_manifest) != canonical_manifest_bytes(source_manifest):
            raise UpdateError("installed source runtime manifest does not match this patch")

    transaction_id = uuid.uuid4().hex[:8]
    backup_id = f"{from_version}_to_{to_version}_{int(time.time())}_{transaction_id}"
    work_root = install_root / ".lc-update" / transaction_id
    versions_root = install_root / "versions"
    backup = _data_root() / "update_backups" / backup_id
    runtime_staging = work_root / "runtime"
    previous_staging = work_root / "previous"
    stable_staging = work_root / "stable"
    versions_root.mkdir(parents=True, exist_ok=True)
    backup.mkdir(parents=True, exist_ok=False)
    work_root.mkdir(parents=True, exist_ok=False)

    state_path = install_root / STATE_FILE
    old_state_bytes = state_path.read_bytes() if state_path.is_file() else None
    stable_operations: list[tuple[str, bool]] = []
    state_switched = False
    try:
        with zipfile.ZipFile(patch_path) as archive:
            if source_layout == 2:
                _construct_runtime(
                    archive,
                    source_root,
                    previous_staging,
                    source_manifest,
                    public_key,
                    {},
                    progress=progress,
                    progress_start=10,
                    progress_end=35,
                    verify_reused=True,
                )
                _install_version_directory(
                    versions_root,
                    previous_staging,
                    from_version,
                    source_manifest,
                    public_key,
                )
            _construct_runtime(
                archive,
                source_root,
                runtime_staging,
                target_manifest,
                public_key,
                runtime_payload,
                progress=progress,
                progress_start=35 if source_layout == 2 else 10,
                progress_end=80,
            )
            target_root = _install_version_directory(
                versions_root,
                runtime_staging,
                to_version,
                target_manifest,
                public_key,
            )
            _emit_progress(progress, 82, "新版本目录已建立")
            _stage_stable_files(
                archive,
                stable_staging,
                manifest.get("stable_payload") or {},
            )
            install_manifest_path = stable_staging / INSTALL_MANIFEST
            _atomic_write_json(install_manifest_path, target_install_manifest)
            install_manifest_meta = {
                "sha256": sha256_file(install_manifest_path),
                "size": install_manifest_path.stat().st_size,
            }
            effective_stable_payload = dict(manifest.get("stable_payload") or {})
            effective_stable_payload[INSTALL_MANIFEST] = install_manifest_meta

        _emit_progress(progress, 86, "正在更新稳定启动组件")
        _apply_stable_files(
            install_root,
            stable_staging,
            effective_stable_payload,
            backup,
            stable_operations,
        )
        _verify_stable_result(
            install_root,
            manifest.get("stable_result_files") or {},
        )
        installed_manifest = _load_json(install_root / INSTALL_MANIFEST)
        verify_manifest(installed_manifest, public_key)
        if canonical_manifest_bytes(installed_manifest) != canonical_manifest_bytes(target_install_manifest):
            raise UpdateError("installed manifest does not match patch")
        _emit_progress(progress, 94, "安装完整性校验通过")
        state = {
            "schema_version": 1,
            "runtime_layout_version": 3,
            "current_version": to_version,
            "previous_version": from_version,
            "pending": True,
            "generation": int((old_state or {}).get("generation") or 0) + 1,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_layout_version": source_layout,
        }
        _atomic_write_json(state_path, state)
        state_switched = True
        _emit_progress(progress, 98, "版本切换完成，正在启动")

        launcher = install_root / DEFAULT_ENTRYPOINT
        if launch_after:
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                    subprocess, "DETACHED_PROCESS", 0
                )
            subprocess.Popen(
                [str(launcher)],
                cwd=str(install_root),
                env=_launcher_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
            )
        result = {
            "from_version": from_version,
            "to_version": to_version,
            "source_layout_version": source_layout,
            "target_layout_version": 3,
            "target_runtime": str(target_root),
            "pending_health_confirmation": True,
            "patch_sha256": patch_sha256,
            "runtime_payload_files": len(manifest.get("runtime_payload") or {}),
            "stable_payload_files": len(manifest.get("stable_payload") or {}),
        }
        _atomic_write_json(backup / "transaction.json", result)
        _write_log(
            f"staged {from_version} -> {to_version}; "
            f"runtime_files={result['runtime_payload_files']} stable_files={result['stable_payload_files']}"
        )
        _emit_progress(progress, 100, "更新完成，正在启动新版本")
        return result
    except Exception:
        if state_switched:
            try:
                if old_state_bytes is None:
                    state_path.unlink(missing_ok=True)
                else:
                    restore = state_path.with_name(state_path.name + ".rollback")
                    restore.write_bytes(old_state_bytes)
                    os.replace(restore, state_path)
            except Exception as exc:
                _write_log(f"state rollback failed: {exc}")
        _restore_stable_files(install_root, backup, stable_operations)
        raise
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
        try:
            work_root.parent.rmdir()
        except OSError:
            pass


def _candidate_patch_paths() -> list[Path]:
    bundle_value = getattr(sys, "_MEIPASS", "")
    if bundle_value:
        bundle_dir = Path(bundle_value).resolve()
        embedded = list(bundle_dir.glob("LiveClipperPatch_*.zip"))
        embedded.extend(bundle_dir.glob("embedded_patch.zip"))
        if embedded:
            return list(dict.fromkeys(path.resolve() for path in embedded if path.is_file()))

    executable_dir = Path(
        sys.executable if getattr(sys, "frozen", False) else __file__
    ).resolve().parent
    candidates = list(executable_dir.glob("LiveClipperPatch_*.zip"))
    candidates.extend(executable_dir.glob("*.lcpatch.zip"))
    return list(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))


def _interactive_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.patch:
        patch = Path(args.patch).resolve()
    else:
        candidates = _candidate_patch_paths()
        if len(candidates) != 1:
            raise UpdateError("place exactly one LiveClipper patch beside the bridge updater")
        patch = candidates[0]

    if args.install_root:
        return patch, Path(args.install_root).resolve()
    candidates = [
        Path.cwd(),
        Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent,
    ]
    for root in candidates:
        if (root / STATE_FILE).is_file() or (
            (root / DEFAULT_ENTRYPOINT).is_file() and (root / "_internal").is_dir()
        ):
            return patch, root.resolve()

    import tkinter as tk
    from tkinter import filedialog

    window = tk.Tk()
    window.withdraw()
    selected = filedialog.askdirectory(title="选择 LiveClipperWeb 程序文件夹")
    window.destroy()
    if not selected:
        raise UpdateError("未选择 LiveClipperWeb 程序文件夹")
    return patch, Path(selected).resolve()


def _show_message(title: str, message: str, error: bool = False) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        window = tk.Tk()
        window.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(title, message)
        window.destroy()
    except Exception:
        print(message, file=sys.stderr if error else sys.stdout)


def _write_update_status(status: str, percent: int, message: str) -> None:
    try:
        _atomic_write_json(
            _data_root() / "update_status.json",
            {
                "schema_version": 1,
                "status": status,
                "percent": max(0, min(100, int(percent))),
                "message": str(message),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
    except Exception:
        pass


def _make_progress_callback(
    events: queue.Queue[tuple[str, Any]] | None = None,
) -> ProgressCallback:
    state = {"saved": -100, "logged": -100}

    def callback(percent: int, message: str) -> None:
        percent = max(0, min(100, int(percent)))
        if events is not None:
            events.put(("progress", (percent, message)))
        if percent in {0, 100} or percent - state["saved"] >= 5:
            _write_update_status("running", percent, message)
            state["saved"] = percent
        if percent in {0, 100} or percent - state["logged"] >= 10:
            _write_log(f"progress {percent}%: {message}")
            state["logged"] = percent

    return callback


def _execute_update(
    args: argparse.Namespace,
    patch: Path,
    install_root: Path,
    public_key: Path,
    events: queue.Queue[tuple[str, Any]] | None = None,
) -> dict[str, Any]:
    progress = _make_progress_callback(events)
    try:
        progress(0, "正在等待 LiveClipper 安全退出")
        _wait_for_process(args.wait_pid)
        result = apply_patch(
            patch,
            install_root,
            public_key,
            expected_patch_sha256=args.expected_patch_sha256,
            launch_after=not args.no_launch,
            progress=progress,
        )
        _write_update_status("complete", 100, "更新完成，正在启动新版本")
        if events is not None:
            events.put(("done", result))
        return result
    except Exception as exc:
        message = f"更新失败：{exc}"
        _write_update_status("failed", 0, message)
        if events is not None:
            events.put(("error", message))
        raise


def _run_progress_window(
    args: argparse.Namespace,
    patch: Path,
    install_root: Path,
    public_key: Path,
) -> int:
    import tkinter as tk
    from tkinter import ttk

    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    outcome = {"finished": False, "code": 1}
    window = tk.Tk()
    window.title("LiveClipper 正在更新")
    window.geometry("520x210")
    window.resizable(False, False)
    try:
        window.attributes("-topmost", True)
        window.after(1500, lambda: window.attributes("-topmost", False))
    except Exception:
        pass

    frame = ttk.Frame(window, padding=20)
    frame.pack(fill="both", expand=True)
    title_var = tk.StringVar(value="正在准备更新")
    detail_var = tk.StringVar(value="请保持电脑开机，更新完成后软件会自动启动")
    percent_var = tk.StringVar(value="0%")
    ttk.Label(frame, textvariable=title_var, font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
    ttk.Label(frame, textvariable=detail_var, wraplength=470).pack(anchor="w", pady=(10, 14))
    progress_bar = ttk.Progressbar(frame, maximum=100, mode="determinate")
    progress_bar.pack(fill="x")
    ttk.Label(frame, textvariable=percent_var).pack(anchor="e", pady=(4, 8))
    close_button = ttk.Button(frame, text="关闭", command=window.destroy, state="disabled")
    close_button.pack(anchor="e")

    def on_close() -> None:
        if outcome["finished"]:
            window.destroy()
        else:
            window.bell()
            detail_var.set("更新仍在进行，请等待完成后自动启动软件")

    window.protocol("WM_DELETE_WINDOW", on_close)

    def worker() -> None:
        try:
            _execute_update(args, patch, install_root, public_key, events)
        except Exception:
            pass

    def poll_events() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "progress":
                    percent, message = payload
                    progress_bar["value"] = percent
                    percent_var.set(f"{percent}%")
                    detail_var.set(str(message))
                elif kind == "done":
                    outcome.update(finished=True, code=0)
                    progress_bar["value"] = 100
                    percent_var.set("100%")
                    title_var.set("更新完成")
                    detail_var.set("正在启动新版本")
                    window.after(1800, window.destroy)
                elif kind == "error":
                    outcome.update(finished=True, code=1)
                    title_var.set("更新未完成")
                    detail_var.set(str(payload))
                    close_button.configure(state="normal")
        except queue.Empty:
            pass
        if window.winfo_exists() and not outcome["finished"]:
            window.after(100, poll_events)

    threading.Thread(target=worker, name="LiveClipperUpdateWorker", daemon=True).start()
    window.after(100, poll_events)
    window.mainloop()
    return int(outcome["code"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a signed LiveClipper version update.")
    parser.add_argument("--patch", default="")
    parser.add_argument("--install-root", default="")
    parser.add_argument("--public-key", default="")
    parser.add_argument("--wait-pid", type=int, default=0)
    parser.add_argument("--expected-patch-sha256", default="")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--show-progress", action="store_true")
    args = parser.parse_args(argv)
    try:
        patch, install_root = _interactive_paths(args)
        public_key = _public_key_path(args.public_key, install_root)
        if args.show_progress:
            try:
                return _run_progress_window(args, patch, install_root, public_key)
            except Exception as exc:
                _write_log(f"progress window unavailable, continuing headless: {exc}")
        if not args.non_interactive:
            _show_message(
                "LiveClipper 架构升级",
                "即将验证补丁并建立新的版本目录。设置、授权、素材和缓存不会被修改。",
            )
        result = _execute_update(args, patch, install_root, public_key)
        if not args.non_interactive:
            _show_message(
                "LiveClipper 升级已启动",
                f"{result['from_version']} 已迁移到 {result['to_version']}。"
                "新版本启动检查失败时会自动回到上一版本。",
            )
        return 0
    except Exception as exc:
        _write_log(f"update failed: {exc}")
        if args.non_interactive:
            print(str(exc), file=sys.stderr)
        else:
            _show_message("LiveClipper 升级失败", str(exc), error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
