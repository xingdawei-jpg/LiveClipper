"""Signed release checking and Runtime V3 update handoff for LiveClipper."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from release_signing import SignatureError, sha256_file, verify_manifest


GITHUB_REPO = "xingdawei-jpg/LiveClipper"
RELEASE_CHANNEL_PATH = "release/stable.json"
DEFAULT_RELEASE_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
RUNTIME_LAYOUT_VERSION = 3
DELTA_FORMAT = "liveclipper-version-delta-v1"
UPDATE_AGENT_RELATIVE = Path("updater") / "LiveClipperUpdater.exe"
UPDATE_PUBLIC_KEY_RELATIVE = Path("updater") / "release_update_public_key.pem"


def _runtime_root() -> Path:
    configured = os.environ.get("LIVECLIPPER_BUNDLE_DIR")
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        return Path(
            getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal")
        ).resolve()
    return Path(__file__).resolve().parent.parent


def _install_root() -> Path:
    configured = os.environ.get("LIVECLIPPER_INSTALL_ROOT")
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        if executable_dir.parent.name.lower() == "versions":
            return executable_dir.parent.parent.resolve()
        return executable_dir
    return _runtime_root()


def _update_agent_path() -> Path:
    return _install_root() / UPDATE_AGENT_RELATIVE


def _release_public_key_path() -> Path:
    candidates = [
        _runtime_root() / "app" / "release_update_public_key.pem",
        _install_root() / UPDATE_PUBLIC_KEY_RELATIVE,
        Path(__file__).resolve().with_name("release_update_public_key.pem"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("release update public key is missing")


def delta_runtime_supported() -> bool:
    root = _install_root()
    return bool(
        getattr(sys, "frozen", False)
        and (root / "current.json").is_file()
        and (root / "LiveClipperWeb.exe").is_file()
        and _update_agent_path().is_file()
        and _release_public_key_path().is_file()
    )


def _local_manifest_path() -> Path:
    bundled = _runtime_root() / "app" / "version.json"
    if bundled.is_file():
        return bundled
    return Path(__file__).resolve().with_name("version.json")


def _local_manifest() -> dict[str, Any]:
    try:
        data = json.loads(_local_manifest_path().read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _manifest_version(data: dict[str, Any]) -> str:
    return str(data.get("version") or data.get("latest_version") or "0")


CURRENT_VERSION = _manifest_version(_local_manifest())


def parse_version(version_str: Any) -> tuple[int, ...]:
    text = str(version_str or "").strip().lstrip("vV")
    match = re.match(r"(\d+(?:\.\d+)*)", text)
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(remote_version: Any, local_version: Any) -> bool:
    return parse_version(remote_version) > parse_version(local_version)


def init_installed_version() -> str:
    return _get_installed_version()


def _get_installed_version() -> str:
    return _manifest_version(_local_manifest())


def _get_installed_version_file() -> str:
    return str(_install_root() / "current.json")


def _set_installed_version(_version: Any) -> bool:
    return False


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _request_bytes(url: str, timeout: int = 15) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "LiveClipper/3"})
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
        return response.read()


def _fetch_json(url: str, timeout: int = 15) -> dict[str, Any]:
    data = json.loads(_request_bytes(url, timeout=timeout).decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("release manifest is not an object")
    return data


def _fetch_github_file(path: str, timeout: int = 15) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref=main"
    payload = _fetch_json(url, timeout=timeout)
    content = base64.b64decode(payload["content"])
    data = json.loads(content.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("release manifest is not an object")
    return data


def get_version_url() -> str:
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{RELEASE_CHANNEL_PATH}"


def _release_sources(path: str) -> list[tuple[str, str]]:
    raw = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{path}"
    return [
        ("GitHub API", path),
        ("GitHub Raw", raw),
        ("jsDelivr", f"https://cdn.jsdelivr.net/gh/{GITHUB_REPO}@main/{path}"),
    ]


def _fetch_signed_release(path: str) -> dict[str, Any] | None:
    fetched = False
    last_network_error: Exception | None = None
    for source, value in _release_sources(path):
        try:
            data = _fetch_github_file(value) if source == "GitHub API" else _fetch_json(value, timeout=12)
            fetched = True
            verify_manifest(data, _release_public_key_path())
            return data
        except SignatureError:
            raise RuntimeError("远程更新清单签名无效，已拒绝本次更新")
        except Exception as exc:
            last_network_error = exc
    if fetched:
        raise RuntimeError("远程更新清单无法通过验证")
    _ = last_network_error
    return None


def _normalize_release(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    version = _manifest_version(result)
    result["version"] = version
    result["latest_version"] = version
    result["schema_version"] = int(result.get("schema_version") or 1)
    result.setdefault("update_strategy", "verified-version-delta")
    result.setdefault("requires_full_package", False)
    result.setdefault("release_page_url", DEFAULT_RELEASE_PAGE)
    result.setdefault(
        "requires_full_package_note",
        "当前版本没有可验证的直达补丁，请使用完整包修复或升级。",
    )
    return result


def _delta_candidates(info: dict[str, Any]) -> list[dict[str, Any]]:
    raw = info.get("patches")
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [item for item in (raw or []) if isinstance(item, dict)]


def _select_delta_patch(
    info: dict[str, Any],
    local_version: str | None = None,
) -> dict[str, Any] | None:
    source_version = str(local_version or _get_installed_version())
    target_version = _manifest_version(info)
    for candidate in _delta_candidates(info):
        if str(candidate.get("format") or "") != DELTA_FORMAT:
            continue
        if str(candidate.get("from_version") or "") != source_version:
            continue
        if str(candidate.get("to_version") or "") != target_version:
            continue
        url = str(candidate.get("url") or candidate.get("download_url") or "").strip()
        sha256 = str(candidate.get("sha256") or "").strip().lower()
        try:
            size = int(candidate.get("size") or 0)
        except Exception:
            size = 0
        if not url or not re.fullmatch(r"[0-9a-f]{64}", sha256) or size <= 0:
            continue
        result = dict(candidate)
        result.update({"url": url, "sha256": sha256, "size": size})
        return result
    return None


def _manifest_file_path(relative_path: str) -> Path | None:
    normalized = str(relative_path or "").replace("\\", "/").lstrip("/")
    root = _runtime_root()
    if normalized.startswith(("app/", "web_client/", "tools/")):
        if normalized.startswith("web_client/tools/"):
            packaged_tool = root / "tools" / Path(normalized).name
            return packaged_tool if packaged_tool.exists() else root / normalized
        return root / normalized
    return None


def _normalized_sha256(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() in {".py", ".json", ".txt", ".html", ".css", ".js", ".pem"}:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest().lower()


def _manifest_integrity_mismatches(manifest: dict[str, Any]) -> list[str]:
    files = manifest.get("runtime_files") if isinstance(manifest.get("runtime_files"), dict) else {}
    mismatches: list[str] = []
    for name in list(manifest.get("integrity_files") or []):
        path = _manifest_file_path(name)
        expected = str(files.get(name) or "").lower()
        try:
            matched = bool(path and expected and path.is_file() and _normalized_sha256(path) == expected)
        except Exception:
            matched = False
        if not matched:
            mismatches.append(str(name))
    return mismatches


def _with_update_route(release: dict[str, Any], local_version: str) -> dict[str, Any]:
    patch = _select_delta_patch(release, local_version)
    supported = bool(patch and delta_runtime_supported())
    release["selected_patch"] = patch
    release["supports_incremental_updates"] = supported
    release["requires_full_package"] = not supported
    release["update_strategy"] = "verified-version-delta" if supported else "full-package"
    return release


def check_update() -> dict[str, Any] | None:
    local_manifest = _local_manifest()
    local_version = _manifest_version(local_manifest)
    remote = _fetch_signed_release(RELEASE_CHANNEL_PATH)
    if remote:
        release = _normalize_release(remote)
        if is_newer(_manifest_version(release), local_version):
            return _with_update_route(release, local_version)

    mismatches = _manifest_integrity_mismatches(local_manifest)
    if mismatches:
        repair = _normalize_release(remote or local_manifest)
        repair.update(
            {
                "repair_required": True,
                "integrity_mismatches": mismatches,
                "supports_incremental_updates": False,
                "requires_full_package": True,
                "update_strategy": "full-package",
            }
        )
        return repair
    return None


def compute_sha256(filepath: str | os.PathLike[str]) -> str:
    return sha256_file(filepath)


def download_file(url: str, dest_path: str, progress_callback=None) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "LiveClipper/3"})
    with urllib.request.urlopen(request, timeout=180, context=_ssl_context()) as response:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(dest_path, "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total)


def _package_url(info: dict[str, Any]) -> str:
    package = info.get("package") if isinstance(info.get("package"), dict) else {}
    return str(
        package.get("url")
        or info.get("package_url")
        or info.get("release_page_url")
        or DEFAULT_RELEASE_PAGE
    )


def _full_package_result(info: dict[str, Any], message: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "full_package_required": True,
        "restart_required": False,
        "version": _manifest_version(info),
        "package_url": _package_url(info),
        "updated": [],
        "failed": [],
        "msg": message or info.get("requires_full_package_note"),
    }


def _download_root(target_version: str) -> Path:
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or tempfile.gettempdir()
    )
    path = base / "LiveClipper" / "update_downloads" / target_version
    path.mkdir(parents=True, exist_ok=True)
    return path


def _patch_filename(patch: dict[str, Any]) -> str:
    url_name = Path(urllib.parse.urlparse(str(patch.get("url") or "")).path).name
    if url_name.lower().endswith(".zip") and re.fullmatch(r"[A-Za-z0-9_.-]+", url_name):
        return url_name
    return f"LiveClipperPatch_{patch['from_version']}_to_{patch['to_version']}.zip"


def _verified_download(patch: dict[str, Any], destination: Path) -> None:
    expected_hash = str(patch["sha256"]).lower()
    expected_size = int(patch["size"])
    if destination.is_file():
        if destination.stat().st_size == expected_size and sha256_file(destination) == expected_hash:
            return
        destination.unlink(missing_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    try:
        download_file(str(patch["url"]), str(temporary))
        if temporary.stat().st_size != expected_size:
            raise RuntimeError("增量包大小校验失败")
        if sha256_file(temporary) != expected_hash:
            raise RuntimeError("增量包 SHA256 校验失败")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def apply_update_headless(version_info: dict[str, Any] | None) -> dict[str, Any]:
    info = _normalize_release(version_info or {})
    patch = info.get("selected_patch") if isinstance(info.get("selected_patch"), dict) else None
    if patch is None:
        patch = _select_delta_patch(info)
    if patch is None:
        return _full_package_result(info)
    if not delta_runtime_supported():
        return _full_package_result(info, "当前客户端不是 Runtime V3，请使用架构桥接包完成本次升级。")

    target_version = _manifest_version(info)
    download_root = _download_root(target_version)
    patch_path = download_root / _patch_filename(patch)
    _verified_download(patch, patch_path)

    agent_copy = download_root / "LiveClipperUpdater.exe"
    key_copy = download_root / "release_update_public_key.pem"
    shutil.copy2(_update_agent_path(), agent_copy)
    shutil.copy2(_release_public_key_path(), key_copy)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    subprocess.Popen(
        [
            str(agent_copy),
            "--patch",
            str(patch_path),
            "--install-root",
            str(_install_root()),
            "--public-key",
            str(key_copy),
            "--wait-pid",
            str(os.getpid()),
            "--expected-patch-sha256",
            str(patch["sha256"]),
            "--non-interactive",
        ],
        cwd=str(download_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    return {
        "ok": True,
        "full_package_required": False,
        "agent_started": True,
        "exit_required": True,
        "restart_required": True,
        "version": target_version,
        "patch_size": int(patch["size"]),
        "updated": [],
        "failed": [],
        "msg": "增量包已验证，客户端即将退出并切换到新版本。",
    }


def check_and_prompt_update(parent_window) -> None:
    def worker() -> None:
        try:
            info = check_update()
        except Exception:
            return
        if not info:
            return

        def prompt() -> None:
            from tkinter import messagebox

            version = _manifest_version(info)
            if info.get("requires_full_package"):
                message = f"发现新版本 {version}。\n\n本次没有可验证的直达补丁，是否打开下载页？"
            else:
                message = f"发现新版本 {version}。\n\n请在软件设置页安装增量更新。"
            if messagebox.askyesno("LiveClipper 更新", message, parent=parent_window):
                if info.get("requires_full_package"):
                    webbrowser.open(_package_url(info), new=2)

        try:
            parent_window.after(0, prompt)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


__all__ = [
    "CURRENT_VERSION",
    "DELTA_FORMAT",
    "RUNTIME_LAYOUT_VERSION",
    "apply_update_headless",
    "check_and_prompt_update",
    "check_update",
    "compute_sha256",
    "delta_runtime_supported",
    "download_file",
    "get_version_url",
    "init_installed_version",
    "is_newer",
    "parse_version",
    "_get_installed_version",
    "_select_delta_patch",
]
