"""Transactional first migration from a verified Runtime V3 install to V4."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from app.release_signing import verify_manifest
from runtime_v4.business_bundle import (
    extract_verified_business_archive,
    verify_business_directory,
)
from runtime_v4.core_manifest import verify_core_directory
from runtime_v4.migration import assemble_core_from_v3, inspect_v3_install
from runtime_v4.migration_package import verify_migration_package


class MigrationTransactionError(RuntimeError):
    pass


VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")


@dataclass(frozen=True)
class LegacyCleanupResult:
    removed_versions: tuple[str, ...]
    preserved_versions: tuple[str, ...]


@dataclass(frozen=True)
class MigrationTransactionResult:
    source_version: str
    application_version: str
    core_version: str
    core_reused_files: int
    core_payload_files: int
    legacy_cleanup: LegacyCleanupResult
    backup_root: Path | None


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
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


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


@contextlib.contextmanager
def _migration_lock(install_root: Path) -> Iterator[None]:
    lock_path = install_root / ".v4-migration.lock"
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise MigrationTransactionError(
                    "another Runtime V4 migration is already running"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise MigrationTransactionError(
                    "another Runtime V4 migration is already running"
                ) from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _paths_under_install_root(install_root: Path) -> tuple[tuple[int, str], ...]:
    if os.name != "nt":
        return ()

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        raise MigrationTransactionError("cannot enumerate running processes")
    process_entry = PROCESSENTRY32W()
    process_entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    results: list[tuple[int, str]] = []
    root_text = str(install_root.resolve()).rstrip("\\/") + os.sep
    try:
        more = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(process_entry)))
        while more:
            pid = int(process_entry.th32ProcessID)
            if pid and pid != os.getpid():
                process = kernel32.OpenProcess(0x1000, False, pid)
                if process:
                    try:
                        size = ctypes.c_ulong(32768)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel32.QueryFullProcessImageNameW(
                            process,
                            0,
                            buffer,
                            ctypes.byref(size),
                        ):
                            path = buffer.value
                            if path.lower().startswith(root_text.lower()):
                                results.append((pid, path))
                    finally:
                        kernel32.CloseHandle(process)
            more = bool(kernel32.Process32NextW(snapshot, ctypes.byref(process_entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(results)


def _assert_install_not_running(install_root: Path) -> None:
    running = _paths_under_install_root(install_root)
    if running:
        summary = ", ".join(f"PID {pid}: {path}" for pid, path in running[:5])
        raise MigrationTransactionError(
            f"LiveClipper installation is still running; close it first ({summary})"
        )


def _run_v4_launcher(install_root: Path, timeout: float) -> bool:
    launcher = install_root / "LiveClipperWeb.exe"
    try:
        completed = subprocess.run(
            [str(launcher)],
            cwd=str(install_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            timeout=max(10.0, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _confirmed_v4_state(
    state_path: Path,
    *,
    application_version: str,
    core_version: str,
) -> bool:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    current = state.get("current") if isinstance(state, dict) else None
    return bool(
        isinstance(current, dict)
        and state.get("schema_version") == 1
        and state.get("runtime_layout_version") == 4
        and current.get("application_version") == application_version
        and current.get("core_version") == core_version
        and state.get("pending") is False
    )


def _fault(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


def _journal_version(value: object, *, label: str) -> str:
    version = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise MigrationTransactionError(f"interrupted migration has invalid {label}")
    return version


def _safe_remove_created_directory(path: Path, root: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise MigrationTransactionError(f"refusing to remove path outside root: {path}") from exc
    if resolved == root.resolve() or path.is_symlink():
        raise MigrationTransactionError(f"refusing to remove unsafe migration path: {path}")
    shutil.rmtree(path)


def cleanup_legacy_v3_versions(
    install_root: str | Path,
    public_key_path: str | Path,
    *,
    active_v4_version: str,
) -> LegacyCleanupResult:
    root = Path(install_root).resolve()
    versions_root = (root / "versions").resolve()
    removed: list[str] = []
    preserved: list[str] = []
    if not versions_root.is_dir():
        return LegacyCleanupResult((), ())
    for version_root in sorted(versions_root.iterdir(), key=lambda item: item.name):
        if not version_root.is_dir() or version_root.name == active_v4_version:
            continue
        manifest_path = version_root / "runtime_manifest.json"
        if not manifest_path.is_file() or version_root.is_symlink():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest is not an object")
            verify_manifest(manifest, public_key_path)
            if manifest.get("runtime_layout_version") != 3:
                raise ValueError("not Runtime V3")
            if str(manifest.get("version") or "") != version_root.name:
                raise ValueError("version mismatch")
            files = manifest.get("files")
            if not isinstance(files, dict) or not files:
                raise ValueError("file manifest is empty")
            expected = {str(path) for path in files} | {"runtime_manifest.json"}
            actual = {
                path.relative_to(version_root).as_posix()
                for path in version_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            unsafe = any(path.is_symlink() for path in version_root.rglob("*"))
            if unsafe or actual != expected:
                raise ValueError("directory contains unknown files")
        except Exception:
            preserved.append(version_root.name)
            continue
        shutil.rmtree(version_root)
        removed.append(version_root.name)
    return LegacyCleanupResult(tuple(removed), tuple(preserved))


def _recover_interrupted_migration(
    install_root: Path,
    *,
    application_version: str,
    core_version: str,
    process_guard: Callable[[Path], None],
) -> tuple[str, Path | None]:
    transactions_root = install_root / ".v4-migration"
    if not transactions_root.is_dir():
        return "none", None
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for transaction_root in transactions_root.iterdir():
        journal_path = transaction_root / "journal.json"
        if not transaction_root.is_dir() or not journal_path.is_file():
            continue
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(journal, dict) or journal.get("schema_version") != 1:
            continue
        if journal.get("stage") in {"confirmed", "rolled_back"}:
            continue
        candidates.append((journal_path.stat().st_mtime, transaction_root, journal))
    if not candidates:
        return "none", None
    if len(candidates) != 1:
        raise MigrationTransactionError(
            "multiple interrupted Runtime V4 migrations require manual inspection"
        )
    _, transaction_root, journal = candidates[0]
    transaction_id = str(journal.get("transaction_id") or "")
    if not re.fullmatch(r"[0-9a-f]{12}", transaction_id):
        raise MigrationTransactionError("interrupted migration has an invalid transaction id")
    if transaction_root.name != transaction_id:
        raise MigrationTransactionError("interrupted migration directory does not match its journal")
    staging = install_root / ".v4-staging" / f"migration-{transaction_id}"
    source_version = _journal_version(journal.get("source_version"), label="source version")
    target_application = _journal_version(
        journal.get("application_version"),
        label="application version",
    )
    target_core = _journal_version(journal.get("core_version"), label="core version")
    if target_application != application_version or target_core != core_version:
        raise MigrationTransactionError(
            "interrupted migration targets a different V4 package"
        )

    state_path = install_root / "current.json"
    if _confirmed_v4_state(
        state_path,
        application_version=application_version,
        core_version=core_version,
    ):
        journal["stage"] = "confirmed"
        journal["recovered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _atomic_write_json(transaction_root / "journal.json", journal)
        if staging.exists():
            _safe_remove_created_directory(staging, install_root / ".v4-staging")
        return "confirmed", transaction_root

    process_guard(install_root)
    backup_launcher = transaction_root / "LiveClipperWeb.v3.exe"
    backup_state = transaction_root / "current.v3.json"
    if not backup_launcher.is_file() or not backup_state.is_file():
        raise MigrationTransactionError(
            "interrupted migration is missing its V3 rollback files"
        )
    temporary_launcher = install_root / f".LiveClipperWeb.recover-{uuid.uuid4().hex[:8]}.exe"
    shutil.copy2(backup_launcher, temporary_launcher)
    os.replace(temporary_launcher, install_root / "LiveClipperWeb.exe")
    _atomic_write_bytes(state_path, backup_state.read_bytes())

    if bool(journal.get("business_created_intent")):
        version_root = install_root / "versions" / target_application
        if version_root.exists():
            _safe_remove_created_directory(version_root, install_root / "versions")
    if bool(journal.get("core_created_intent")):
        core_root = install_root / "core" / target_core
        if core_root.exists():
            _safe_remove_created_directory(core_root, install_root / "core")
    journal["stage"] = "rolled_back"
    journal["recovered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    journal["recovery_reason"] = "interrupted before V4 health confirmation"
    journal["source_version"] = source_version
    _atomic_write_json(transaction_root / "journal.json", journal)
    if staging.exists():
        _safe_remove_created_directory(staging, install_root / ".v4-staging")
    return "rolled_back", transaction_root


def migrate_v3_install(
    install_root: str | Path,
    package_root: str | Path,
    public_key_path: str | Path,
    *,
    cleanup_legacy: bool = False,
    health_timeout: float = 90.0,
    health_runner: Callable[[Path, float], bool] | None = None,
    process_guard: Callable[[Path], None] | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> MigrationTransactionResult:
    root = Path(install_root).resolve()
    if not root.is_dir():
        raise MigrationTransactionError(f"Runtime V3 install root is missing: {root}")
    public_key = Path(public_key_path).resolve()
    verified_package = verify_migration_package(package_root, public_key)
    runner = health_runner or _run_v4_launcher
    guard = process_guard or _assert_install_not_running

    with _migration_lock(root):
        recovery, recovery_backup = _recover_interrupted_migration(
            root,
            application_version=verified_package.application_version,
            core_version=verified_package.core_version,
            process_guard=guard,
        )
        if recovery == "confirmed":
            cleanup = LegacyCleanupResult((), ())
            retained_backup = recovery_backup
            if cleanup_legacy:
                cleanup = cleanup_legacy_v3_versions(
                    root,
                    public_key,
                    active_v4_version=verified_package.application_version,
                )
                if not cleanup.preserved_versions and recovery_backup is not None:
                    shutil.rmtree(recovery_backup, ignore_errors=True)
                    retained_backup = None
            return MigrationTransactionResult(
                source_version=verified_package.source_version,
                application_version=verified_package.application_version,
                core_version=verified_package.core_version,
                core_reused_files=0,
                core_payload_files=0,
                legacy_cleanup=cleanup,
                backup_root=retained_backup,
            )
        legacy = inspect_v3_install(
            root,
            public_key,
            expected_version=verified_package.source_version,
        )
        guard(root)
        transaction_id = uuid.uuid4().hex[:12]
        staging = root / ".v4-staging" / f"migration-{transaction_id}"
        backup = root / ".v4-migration" / transaction_id
        state_path = root / "current.json"
        launcher_path = root / "LiveClipperWeb.exe"
        if not launcher_path.is_file():
            raise MigrationTransactionError("Runtime V3 root launcher is missing")
        core_target = root / "core" / verified_package.core_version
        version_root = root / "versions" / verified_package.application_version
        business_target = version_root / "business"
        if version_root.exists():
            raise MigrationTransactionError(
                "V4 application version already exists in the installation"
            )
        if core_target.exists():
            existing_core = verify_core_directory(
                core_target,
                public_key,
                expected_version=verified_package.core_version,
                hash_mode="full",
            )
            if existing_core.manifest_sha256 != verified_package.core.manifest_sha256:
                raise MigrationTransactionError("existing V4 Core does not match this package")

        staging.mkdir(parents=True)
        backup.mkdir(parents=True)
        original_state = state_path.read_bytes()
        shutil.copy2(launcher_path, backup / "LiveClipperWeb.v3.exe")
        _atomic_write_bytes(backup / "current.v3.json", original_state)
        journal = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "source_version": legacy.version,
            "application_version": verified_package.application_version,
            "core_version": verified_package.core_version,
            "core_created_intent": not core_target.exists(),
            "business_created_intent": True,
            "stage": "prepared",
        }
        _atomic_write_json(backup / "journal.json", journal)

        created_core = False
        created_business = False
        root_switched = False
        assembly = None
        try:
            if core_target.exists():
                assembly_reused = 0
                assembly_payload = 0
            else:
                staged_core = staging / "core"
                assembly = assemble_core_from_v3(
                    legacy,
                    verified_package.core_bridge,
                    staged_core,
                    public_key,
                    expected_core_version=verified_package.core_version,
                )
                core_target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_core, core_target)
                created_core = True
                assembly_reused = assembly.hardlinked_files
                assembly_payload = assembly.payload_files
            journal["stage"] = "core_installed"
            _atomic_write_json(backup / "journal.json", journal)
            _fault(fault_injector, "after_core")

            extracted = extract_verified_business_archive(
                verified_package.business_archive,
                staging / "business",
                public_key,
                expected_version=verified_package.application_version,
                expected_core_version=verified_package.core_version,
            )
            if extracted.manifest_sha256 != verified_package.business.manifest_sha256:
                raise MigrationTransactionError("initial V4 business manifest changed")
            version_root.mkdir(parents=True)
            os.replace(extracted.root, business_target)
            created_business = True
            verify_business_directory(
                business_target,
                public_key,
                expected_version=verified_package.application_version,
                expected_core_version=verified_package.core_version,
            )
            journal["stage"] = "business_installed"
            _atomic_write_json(backup / "journal.json", journal)
            _fault(fault_injector, "after_business")

            staged_launcher = staging / "LiveClipperWeb.exe"
            shutil.copy2(verified_package.launcher, staged_launcher)
            with staged_launcher.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staged_launcher, launcher_path)
            root_switched = True
            journal["stage"] = "launcher_replaced"
            _atomic_write_json(backup / "journal.json", journal)
            _fault(fault_injector, "after_launcher")

            new_state = {
                "schema_version": 1,
                "runtime_layout_version": 4,
                "current": {
                    "application_version": verified_package.application_version,
                    "core_version": verified_package.core_version,
                },
                "previous": None,
                "pending": True,
                "migration_transaction_id": transaction_id,
                "migrated_from": legacy.version,
                "activated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            _atomic_write_json(state_path, new_state)
            journal["stage"] = "state_replaced"
            _atomic_write_json(backup / "journal.json", journal)
            _fault(fault_injector, "after_state")

            healthy = bool(runner(root, max(10.0, health_timeout)))
            if not healthy or not _confirmed_v4_state(
                state_path,
                application_version=verified_package.application_version,
                core_version=verified_package.core_version,
            ):
                raise MigrationTransactionError(
                    "Runtime V4 first-launch health confirmation failed"
                )
            journal["stage"] = "confirmed"
            _atomic_write_json(backup / "journal.json", journal)
        except Exception as exc:
            if root_switched:
                temporary_launcher = root / f".LiveClipperWeb.v3-{transaction_id}.exe"
                shutil.copy2(backup / "LiveClipperWeb.v3.exe", temporary_launcher)
                os.replace(temporary_launcher, launcher_path)
                _atomic_write_bytes(state_path, original_state)
            if created_business and version_root.exists():
                _safe_remove_created_directory(version_root, root / "versions")
            if created_core and core_target.exists():
                _safe_remove_created_directory(core_target, root / "core")
            shutil.rmtree(staging, ignore_errors=True)
            journal["stage"] = "rolled_back"
            journal["error"] = str(exc)
            _atomic_write_json(backup / "journal.json", journal)
            if isinstance(exc, MigrationTransactionError):
                raise
            raise MigrationTransactionError("Runtime V4 migration failed and V3 was restored") from exc

        shutil.rmtree(staging, ignore_errors=True)
        cleanup = LegacyCleanupResult((), ())
        retained_backup: Path | None = backup
        if cleanup_legacy:
            cleanup = cleanup_legacy_v3_versions(
                root,
                public_key,
                active_v4_version=verified_package.application_version,
            )
            if not cleanup.preserved_versions:
                shutil.rmtree(backup, ignore_errors=True)
                retained_backup = None
        return MigrationTransactionResult(
            source_version=legacy.version,
            application_version=verified_package.application_version,
            core_version=verified_package.core_version,
            core_reused_files=assembly_reused,
            core_payload_files=assembly_payload,
            legacy_cleanup=cleanup,
            backup_root=retained_backup,
        )


__all__ = [
    "LegacyCleanupResult",
    "MigrationTransactionError",
    "MigrationTransactionResult",
    "cleanup_legacy_v3_versions",
    "migrate_v3_install",
]
