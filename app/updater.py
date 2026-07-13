"""LiveClipper release checker.

Program files are immutable at runtime. Updates are delivered as a verified
full package; this module never writes Python, frontend, or tool files into the
user-data directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


GITHUB_REPO = "xingdawei-jpg/LiveClipper"
RELEASE_CHANNEL_PATH = "release/stable.json"
LEGACY_VERSION_PATH = "app/version.json"
DEFAULT_RELEASE_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
RUNTIME_LAYOUT_VERSION = 2


def _runtime_root() -> Path:
    configured = os.environ.get("LIVECLIPPER_BUNDLE_DIR")
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal")).resolve()
    return Path(__file__).resolve().parent.parent


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
    """Compatibility API: the running package manifest is the only version."""
    return _get_installed_version()


def _get_installed_version() -> str:
    return _manifest_version(_local_manifest())


def _get_installed_version_file() -> str:
    """Return the retired marker path for diagnostics only."""
    return str(_runtime_root() / ".installed_version")


def _set_installed_version(_version: Any) -> bool:
    """Retired compatibility API. A marker can no longer change runtime truth."""
    return False


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _request_bytes(url: str, timeout: int = 15) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "LiveClipper/2"})
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


def _fetch_release_path(path: str) -> dict[str, Any] | None:
    for source, value in _release_sources(path):
        try:
            if source == "GitHub API":
                return _fetch_github_file(value)
            return _fetch_json(value, timeout=12)
        except Exception:
            continue
    return None


def _normalize_release(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    version = _manifest_version(result)
    result["version"] = version
    result["latest_version"] = version
    result["schema_version"] = int(result.get("schema_version") or 1)
    result["update_strategy"] = "full-package"
    result["requires_full_package"] = True
    result.setdefault("release_page_url", DEFAULT_RELEASE_PAGE)
    result.setdefault(
        "requires_full_package_note",
        "本版本采用整包升级。程序代码不会再写入用户数据目录，请下载完整包后替换旧程序。",
    )
    return result


def _manifest_file_path(relative_path: str) -> Path | None:
    normalized = str(relative_path or "").replace("\\", "/").lstrip("/")
    root = _runtime_root()
    if normalized.startswith("app/"):
        return root / normalized
    if normalized.startswith("web_client/tools/"):
        packaged_tool = root / "tools" / Path(normalized).name
        return packaged_tool if packaged_tool.exists() else root / normalized
    if normalized.startswith(("web_client/", "tools/")):
        return root / normalized
    return None


def _normalized_sha256(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() in {".py", ".json", ".txt", ".html", ".css", ".js"}:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest().lower()


def _manifest_integrity_mismatches(manifest: dict[str, Any]) -> list[str]:
    files = manifest.get("runtime_files") if isinstance(manifest.get("runtime_files"), dict) else {}
    if not files:
        files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    targets = list(manifest.get("integrity_files") or [])
    mismatches: list[str] = []
    for name in targets:
        path = _manifest_file_path(name)
        expected = str(files.get(name) or "").lower()
        try:
            matched = bool(path and expected and path.is_file() and _normalized_sha256(path) == expected)
        except Exception:
            matched = False
        if not matched:
            mismatches.append(str(name))
    return mismatches


def check_update() -> dict[str, Any] | None:
    local_manifest = _local_manifest()
    local_version = _manifest_version(local_manifest)

    remote = _fetch_release_path(RELEASE_CHANNEL_PATH)
    if not remote:
        remote = _fetch_release_path(LEGACY_VERSION_PATH)
    if remote:
        release = _normalize_release(remote)
        if is_newer(_manifest_version(release), local_version):
            return release

    mismatches = _manifest_integrity_mismatches(local_manifest)
    if mismatches:
        repair = _normalize_release(remote or local_manifest)
        repair["repair_required"] = True
        repair["integrity_mismatches"] = mismatches
        repair["release_page_url"] = repair.get("release_page_url") or DEFAULT_RELEASE_PAGE
        return repair
    return None


def apply_update_headless(version_info: dict[str, Any] | None) -> dict[str, Any]:
    info = _normalize_release(version_info or {})
    package = info.get("package") if isinstance(info.get("package"), dict) else {}
    package_url = (
        package.get("url")
        or info.get("package_url")
        or info.get("release_page_url")
        or info.get("update_url")
        or info.get("download_url")
        or DEFAULT_RELEASE_PAGE
    )
    return {
        "ok": False,
        "full_package_required": True,
        "restart_required": False,
        "version": _manifest_version(info),
        "package_url": package_url,
        "updated": [],
        "failed": [],
        "msg": info.get("requires_full_package_note"),
    }


def compute_sha256(filepath: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(filepath, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, dest_path: str, progress_callback=None) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "LiveClipper/2"})
    with urllib.request.urlopen(request, timeout=60, context=_ssl_context()) as response:
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


def check_and_prompt_update(parent_window) -> None:
    def worker() -> None:
        info = check_update()
        if not info:
            return

        def prompt() -> None:
            from tkinter import messagebox

            version = _manifest_version(info)
            message = (
                f"发现新版本 {version}。\n\n"
                "本次需要下载完整包，用户设置和素材不会受影响。\n"
                "是否打开下载页面？"
            )
            if messagebox.askyesno("LiveClipper 更新", message, parent=parent_window):
                webbrowser.open(_package_url(info), new=2)

        try:
            parent_window.after(0, prompt)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


__all__ = [
    "CURRENT_VERSION",
    "RUNTIME_LAYOUT_VERSION",
    "apply_update_headless",
    "check_and_prompt_update",
    "check_update",
    "compute_sha256",
    "download_file",
    "get_version_url",
    "init_installed_version",
    "is_newer",
    "parse_version",
    "_get_installed_version",
]
