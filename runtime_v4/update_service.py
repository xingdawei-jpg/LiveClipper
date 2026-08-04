"""Stable Host-owned update service exposed to the Runtime V4 business API."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

from runtime_v4.update_agent import (
    UpdateError,
    _load_state,
    cleanup_download_cache,
    initialize_download_cache,
    prune_business_versions,
)
from runtime_v4.update_channel import (
    BusinessUpdateDecision,
    UpdateChannelError,
    VerifiedUpdateChannel,
    apply_signed_business_update,
    fetch_signed_update_channel,
    plan_business_update,
)


SOURCE_CONFIG_SCHEMA_VERSION = 1
RUNTIME_LAYOUT_VERSION = 4
SERVICE_LOCK_FILE = ".v4-host-update.lock"


class UpdateServiceError(RuntimeError):
    pass


def _acquire_service_update_lock(install_root: Path):
    lock_path = install_root / SERVICE_LOCK_FILE
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError as exc:
        handle.close()
        raise UpdateServiceError("另一个 V4 更新进程正在运行") from exc


def _release_service_update_lock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def default_download_root() -> Path:
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or tempfile.gettempdir()
    )
    return (base / "LiveClipper" / "update_downloads" / "v4").absolute()


def load_update_source_config(path: str | Path) -> tuple[str, ...]:
    source = Path(path).resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise UpdateServiceError(f"cannot read Runtime V4 update source config: {source}") from exc
    if not isinstance(document, dict):
        raise UpdateServiceError("Runtime V4 update source config must be an object")
    expected = {"schema_version", "runtime_layout_version", "channel", "urls"}
    if set(document) != expected:
        raise UpdateServiceError("Runtime V4 update source config fields mismatch")
    if document.get("schema_version") != SOURCE_CONFIG_SCHEMA_VERSION:
        raise UpdateServiceError("unsupported Runtime V4 update source config schema")
    if document.get("runtime_layout_version") != RUNTIME_LAYOUT_VERSION:
        raise UpdateServiceError("Runtime V4 update source config layout mismatch")
    if str(document.get("channel") or "").strip() != "stable":
        raise UpdateServiceError("Runtime V4 update source config channel mismatch")
    raw_urls = document.get("urls")
    if not isinstance(raw_urls, list):
        raise UpdateServiceError("Runtime V4 update source config URLs must be a list")
    urls: list[str] = []
    for raw_url in raw_urls:
        url = str(raw_url or "").strip()
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise UpdateServiceError("Runtime V4 update source config contains an unsafe URL")
        if url not in urls:
            urls.append(url)
    return tuple(urls)


def _decision_message(decision: BusinessUpdateDecision) -> str:
    messages = {
        "update_available": "发现可安装的 V4 业务更新",
        "up_to_date": "当前已经是最新版本",
        "channel_hold": "更新通道暂时暂停",
        "channel_paused": "更新通道暂时暂停",
        "channel_disabled": "在线更新暂未开放",
        "source_version_not_allowed": "当前版本无法直接增量升级，需要使用 V4 完整包",
        "core_incompatible": "当前核心无法运行该业务版本，需要使用 V4 完整包",
    }
    return messages.get(decision.reason, "当前没有可安装更新")


def _public_update(
    channel: VerifiedUpdateChannel,
    decision: BusinessUpdateDecision,
) -> dict[str, Any]:
    requires_full = decision.reason in {"source_version_not_allowed", "core_incompatible"}
    return {
        "version": channel.application_version,
        "release_notes": channel.release_notes,
        "force_update": False,
        "file_count": 0,
        "has_package": False,
        "package_url": "",
        "package_sha256": "",
        "package_size": 0,
        "patch_sha256": channel.sha256,
        "patch_size": channel.size,
        "requires_full_package": requires_full,
        "requires_full_package_note": _decision_message(decision) if requires_full else "",
        "supports_web_incremental_updates": not requires_full,
        "update_strategy": "v4-signed-business-bundle",
        "repair_required": False,
        "integrity_mismatches": [],
        "channel_status": channel.channel_status,
        "channel_reason": decision.reason,
        "compatible_core_versions": list(channel.compatible_core_versions),
        "bundle_manifest_sha256": channel.bundle_manifest_sha256,
        "channel_document_sha256": channel.document_sha256,
    }


class RuntimeV4UpdateService:
    """Narrow stable-core service; callers cannot supply paths, keys, or URLs."""

    def __init__(
        self,
        install_root: str | Path,
        public_key_path: str | Path,
        channel_urls: Sequence[str],
        *,
        download_root: str | Path | None = None,
        restart_callback: Callable[[], bool] | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._install_root = Path(install_root).resolve()
        self._public_key_path = Path(public_key_path).resolve()
        self._channel_urls = tuple(str(value).strip() for value in channel_urls if str(value).strip())
        configured_download_root = (
            Path(download_root).absolute()
            if download_root is not None
            else default_download_root()
        )
        self._restart_callback = restart_callback
        self._opener = opener
        self._apply_lock = threading.Lock()
        if not self._install_root.is_dir():
            raise UpdateServiceError(
                f"Runtime V4 installation root does not exist: {self._install_root}"
            )
        if not self._public_key_path.is_file():
            raise UpdateServiceError("Runtime V4 Host release public key is missing")
        try:
            self._download_root = initialize_download_cache(configured_download_root)
        except UpdateError as exc:
            raise UpdateServiceError(str(exc)) from exc

    @property
    def available(self) -> bool:
        return bool(self._channel_urls)

    def _current(self):
        try:
            _, _, current = _load_state(self._install_root)
            return current
        except UpdateError as exc:
            raise UpdateServiceError(str(exc)) from exc

    def _channel(self) -> VerifiedUpdateChannel:
        if not self._channel_urls:
            raise UpdateServiceError("V4 在线更新通道尚未配置")
        try:
            return fetch_signed_update_channel(
                self._channel_urls,
                self._public_key_path,
                opener=self._opener,
            )
        except UpdateChannelError as exc:
            raise UpdateServiceError(str(exc)) from exc

    def check_update(self) -> dict[str, Any]:
        current = self._current()
        if not self._channel_urls:
            return {
                "ok": True,
                "update_available": False,
                "current_version": current.application_version,
                "reason": "channel_not_configured",
                "msg": "V4 在线更新通道尚未发布",
            }
        channel = self._channel()
        decision = plan_business_update(
            channel,
            current_version=current.application_version,
            current_core_version=current.core_version,
        )
        newer_but_requires_full = decision.reason in {
            "source_version_not_allowed",
            "core_incompatible",
        }
        if not decision.available and not newer_but_requires_full:
            return {
                "ok": True,
                "update_available": False,
                "current_version": current.application_version,
                "reason": decision.reason,
                "msg": _decision_message(decision),
            }
        return {
            "ok": True,
            "update_available": True,
            "current_version": current.application_version,
            "update": _public_update(channel, decision),
        }

    def apply_update(
        self,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        if not self._apply_lock.acquire(blocking=False):
            raise UpdateServiceError("另一个 V4 更新任务正在运行")
        process_lock = None
        try:
            process_lock = _acquire_service_update_lock(self._install_root)
            channel = self._channel()
            current = self._current()
            decision = plan_business_update(
                channel,
                current_version=current.application_version,
                current_core_version=current.core_version,
            )
            if not decision.available:
                requires_full = decision.reason in {
                    "source_version_not_allowed",
                    "core_incompatible",
                }
                return {
                    "ok": not requires_full,
                    "updated": [],
                    "restart_required": False,
                    "full_package_required": requires_full,
                    "msg": _decision_message(decision),
                }

            def localized_progress(downloaded: int, total: int, message: str) -> None:
                localized = message
                if message.startswith("downloading from "):
                    localized = f"正在从 {message.removeprefix('downloading from ')} 下载"
                elif message == "using verified download cache":
                    localized = "正在使用已验证的下载缓存"
                elif message == "download verified":
                    localized = "更新包下载并校验完成"
                if progress_callback is not None:
                    progress_callback(downloaded, total, localized)

            applied = apply_signed_business_update(
                self._install_root,
                channel,
                self._download_root,
                public_key_path=self._public_key_path,
                progress_callback=localized_progress,
                opener=self._opener,
            )
            installed = applied.install_result
            if installed is None:
                return {
                    "ok": True,
                    "updated": [],
                    "restart_required": False,
                    "msg": _decision_message(applied.decision),
                }

            cleanup_warnings: list[str] = []
            if applied.archive_path is not None:
                try:
                    applied.archive_path.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_warnings.append(f"更新包清理失败：{type(exc).__name__}")
            try:
                versions = prune_business_versions(
                    self._install_root,
                    keep_recent_unreferenced=1,
                )
            except Exception as exc:
                versions = None
                cleanup_warnings.append(f"旧业务版本清理失败：{type(exc).__name__}")
            try:
                downloads = cleanup_download_cache(
                    self._download_root,
                    keep_recent_directories=2,
                    stale_partial_days=14,
                )
            except Exception as exc:
                downloads = None
                cleanup_warnings.append(f"下载缓存清理失败：{type(exc).__name__}")
            return {
                "ok": True,
                "updated": [installed.application_version] if installed.activated else [],
                "restart_required": bool(installed.activated),
                "application_version": installed.application_version,
                "core_version": installed.core_version,
                "manifest_sha256": installed.manifest_sha256,
                "already_installed": installed.already_installed,
                "cleanup": {
                    "removed_versions": list(versions.removed_versions) if versions else [],
                    "removed_downloads": list(downloads.removed) if downloads else [],
                    "warnings": cleanup_warnings,
                },
                "msg": (
                    "V4 业务更新已安装，正在准备安全重启"
                    if installed.activated
                    else "当前业务版本已经安装"
                ),
            }
        except (UpdateChannelError, UpdateError) as exc:
            raise UpdateServiceError(str(exc)) from exc
        finally:
            if process_lock is not None:
                _release_service_update_lock(process_lock)
            self._apply_lock.release()

    def schedule_restart(self) -> bool:
        if self._restart_callback is None:
            return False
        try:
            return bool(self._restart_callback())
        except Exception as exc:
            raise UpdateServiceError("V4 Launcher 重启调度失败") from exc


__all__ = [
    "RuntimeV4UpdateService",
    "UpdateServiceError",
    "default_download_root",
    "load_update_source_config",
]
