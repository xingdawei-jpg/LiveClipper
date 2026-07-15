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
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from typing import Any, Callable

from release_signing import SignatureError, sha256_file, verify_manifest


GITHUB_REPO = "xingdawei-jpg/LiveClipper"
RELEASE_CHANNEL_PATH = "release/stable.json"
DEFAULT_RELEASE_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
RUNTIME_LAYOUT_VERSION = 3
DELTA_FORMAT = "liveclipper-version-delta-v1"
UPDATE_AGENT_RELATIVE = Path("updater") / "LiveClipperUpdater.exe"
UPDATE_PUBLIC_KEY_RELATIVE = Path("updater") / "release_update_public_key.pem"
INSTALL_MANIFEST = "install_manifest.json"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_ATTEMPTS_PER_SOURCE = 2
DOWNLOAD_SOCKET_TIMEOUT = 30
DOWNLOAD_TOTAL_TIMEOUT = 300
INACTIVE_CHANNEL_STATES = {
    "hold",
    "paused",
    "disabled",
    "awaiting-external-distribution",
}
CHAIN_PLAN_FORMAT = "liveclipper-update-chain-plan-v1"
MAX_PATCH_CHAIN_LENGTH = 8
DISK_SPACE_BUFFER_BYTES = 128 * 1024 * 1024

DownloadProgress = Callable[[int, int, str], None]


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(
            getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal")
        ).resolve()
    configured = os.environ.get("LIVECLIPPER_BUNDLE_DIR")
    if configured:
        return Path(configured).resolve()
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


def _installed_updater_version() -> str:
    try:
        data = json.loads(
            (_install_root() / INSTALL_MANIFEST).read_text(encoding="utf-8-sig")
        )
        return str(data.get("updater_version") or "")
    except Exception:
        return ""


def _updater_meets_minimum(info: dict[str, Any]) -> bool:
    required = str(info.get("minimum_updater_version") or "1.0.0")
    installed = _installed_updater_version()
    return bool(installed and parse_version(installed) >= parse_version(required))


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
    result["channel_status"] = str(result.get("channel_status") or "ready").strip().lower()
    result.setdefault("update_strategy", "verified-version-delta")
    result.setdefault("requires_full_package", False)
    result.setdefault("minimum_updater_version", "1.0.0")
    result.setdefault("release_page_url", DEFAULT_RELEASE_PAGE)
    result.setdefault(
        "requires_full_package_note",
        "当前版本没有可验证的直达补丁，请使用完整包修复或升级。",
    )
    return result


def _channel_is_active(release: dict[str, Any]) -> bool:
    status = str(release.get("channel_status") or "ready").strip().lower()
    return status not in INACTIVE_CHANNEL_STATES


def _delta_candidates(info: dict[str, Any]) -> list[dict[str, Any]]:
    raw = info.get("patches")
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [item for item in (raw or []) if isinstance(item, dict)]


def _patch_sources(patch: dict[str, Any]) -> list[dict[str, str]]:
    values: list[Any] = []
    legacy_url = str(patch.get("url") or patch.get("download_url") or "").strip()
    if legacy_url:
        values.append({"name": patch.get("source_name") or "", "url": legacy_url})
    raw_sources = patch.get("sources")
    if not isinstance(raw_sources, list):
        raw_sources = patch.get("urls")
    if isinstance(raw_sources, list):
        values.extend(raw_sources)

    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(values, start=1):
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("download_url") or "").strip()
            name = str(item.get("name") or item.get("label") or "").strip()
        else:
            url = str(item or "").strip()
            name = ""
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            continue
        if url in seen:
            continue
        seen.add(url)
        sources.append(
            {
                "name": name or parsed.hostname or f"下载源{index}",
                "url": url,
            }
        )
    return sources


def _normalize_delta_patch(candidate: dict[str, Any]) -> dict[str, Any] | None:
    if str(candidate.get("format") or "") != DELTA_FORMAT:
        return None
    from_version = str(candidate.get("from_version") or "").strip()
    to_version = str(candidate.get("to_version") or "").strip()
    if not from_version or not to_version or from_version == to_version:
        return None
    sources = _patch_sources(candidate)
    sha256 = str(candidate.get("sha256") or "").strip().lower()
    try:
        size = int(candidate.get("size") or 0)
    except Exception:
        size = 0
    if not sources or not re.fullmatch(r"[0-9a-f]{64}", sha256) or size <= 0:
        return None
    result = dict(candidate)
    result.update(
        {
            "from_version": from_version,
            "to_version": to_version,
            "url": sources[0]["url"],
            "urls": [source["url"] for source in sources],
            "sources": sources,
            "sha256": sha256,
            "size": size,
        }
    )
    return result


def _normalized_delta_candidates(info: dict[str, Any]) -> list[dict[str, Any]]:
    patches: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in _delta_candidates(info):
        patch = _normalize_delta_patch(candidate)
        if patch is None:
            continue
        key = (patch["from_version"], patch["to_version"], patch["sha256"])
        if key in seen:
            continue
        seen.add(key)
        patches.append(patch)
    return patches


def _select_delta_patch(
    info: dict[str, Any],
    local_version: str | None = None,
) -> dict[str, Any] | None:
    source_version = str(local_version or _get_installed_version())
    target_version = _manifest_version(info)
    for candidate in _normalized_delta_candidates(info):
        if candidate["from_version"] == source_version and candidate["to_version"] == target_version:
            return candidate
    return None


def _max_patch_chain_length(info: dict[str, Any]) -> int:
    policy = info.get("patch_policy") if isinstance(info.get("patch_policy"), dict) else {}
    try:
        value = int(policy.get("max_chain_depth") or policy.get("max_patch_chain_length") or 0)
    except Exception:
        value = 0
    return max(1, min(value or MAX_PATCH_CHAIN_LENGTH, MAX_PATCH_CHAIN_LENGTH))


def _select_delta_chain(
    info: dict[str, Any],
    local_version: str | None = None,
) -> list[dict[str, Any]] | None:
    source_version = str(local_version or _get_installed_version())
    target_version = _manifest_version(info)
    if source_version == target_version:
        return []

    direct = _select_delta_patch(info, source_version)
    if direct is not None:
        return [direct]

    max_depth = _max_patch_chain_length(info)
    edges: dict[str, list[dict[str, Any]]] = {}
    for patch in _normalized_delta_candidates(info):
        edges.setdefault(patch["from_version"], []).append(patch)

    queue: list[tuple[str, list[dict[str, Any]]]] = [(source_version, [])]
    best_depth: dict[str, int] = {source_version: 0}
    while queue:
        version, path = queue.pop(0)
        if len(path) >= max_depth:
            continue
        for patch in edges.get(version, []):
            next_version = patch["to_version"]
            if any(item["from_version"] == next_version for item in path):
                continue
            next_path = [*path, patch]
            if next_version == target_version:
                return next_path
            previous_depth = best_depth.get(next_version)
            if previous_depth is None or len(next_path) < previous_depth:
                best_depth[next_version] = len(next_path)
                queue.append((next_version, next_path))
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
    if not _channel_is_active(release):
        release["selected_patch"] = None
        release["selected_patches"] = []
        release["patch_chain_length"] = 0
        release["supports_incremental_updates"] = False
        release["requires_full_package"] = True
        release["update_strategy"] = "hold"
        return release
    patches = _select_delta_chain(release, local_version)
    supported = bool(
        patches
        and delta_runtime_supported()
        and _updater_meets_minimum(release)
    )
    release["selected_patch"] = patches[0] if patches and len(patches) == 1 else None
    release["selected_patches"] = patches or []
    release["patch_chain_length"] = len(patches or [])
    release["supports_incremental_updates"] = supported
    release["requires_full_package"] = not supported
    release["update_strategy"] = "verified-version-delta-chain" if supported else "full-package"
    return release


def check_update() -> dict[str, Any] | None:
    local_manifest = _local_manifest()
    local_version = _manifest_version(local_manifest)
    remote = _fetch_signed_release(RELEASE_CHANNEL_PATH)
    release = _normalize_release(remote) if remote else None
    if release and _channel_is_active(release):
        if is_newer(_manifest_version(release), local_version):
            return _with_update_route(release, local_version)

    mismatches = _manifest_integrity_mismatches(local_manifest)
    if mismatches:
        repair = _normalize_release(release or local_manifest)
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


def _emit_download_progress(
    progress_callback: DownloadProgress | None,
    downloaded: int,
    total: int,
    message: str,
) -> None:
    if not progress_callback:
        return
    try:
        progress_callback(downloaded, total, message)
    except TypeError:
        progress_callback(downloaded, total)  # type: ignore[misc]


def _safe_download_error(exc: Exception) -> str:
    text = str(exc or type(exc).__name__).strip()
    text = re.sub(r"https?://[^\s；;]+", "download source", text, flags=re.I)
    return text[:240] or type(exc).__name__

def _download_from_source(
    url: str,
    destination: Path,
    *,
    expected_size: int,
    progress_callback: DownloadProgress | None,
    deadline: float,
    source_name: str,
) -> int:
    offset = destination.stat().st_size if destination.is_file() else 0
    if expected_size > 0 and offset > expected_size:
        destination.unlink(missing_ok=True)
        offset = 0
    if expected_size > 0 and offset == expected_size:
        return offset

    headers = {"User-Agent": "LiveClipper/3"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(
        request,
        timeout=DOWNLOAD_SOCKET_TIMEOUT,
        context=_ssl_context(),
    ) as response:
        get_final_url = getattr(response, "geturl", None)
        final_url = str(get_final_url() if callable(get_final_url) else url)
        if urllib.parse.urlparse(final_url).scheme.lower() != "https":
            raise RuntimeError("下载源重定向到不安全地址")
        status = int(getattr(response, "status", 0) or response.getcode() or 200)
        if offset and status == 206:
            content_range = str(response.headers.get("Content-Range") or "")
            match = re.match(r"bytes\s+(\d+)-\d+/(?:\d+|\*)", content_range, re.I)
            if not match or int(match.group(1)) != offset:
                destination.unlink(missing_ok=True)
                raise RuntimeError("服务器返回的断点位置无效")
            mode = "ab"
            downloaded = offset
        else:
            mode = "wb"
            downloaded = 0

        total = expected_size
        if total <= 0:
            response_size = int(response.headers.get("Content-Length") or 0)
            total = downloaded + response_size if response_size else 0
        _emit_download_progress(
            progress_callback,
            downloaded,
            total,
            f"正在从 {source_name} 下载",
        )

        with destination.open(mode) as handle:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("下载超过总时限")
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if expected_size > 0 and downloaded > expected_size:
                    handle.close()
                    destination.unlink(missing_ok=True)
                    raise RuntimeError("下载内容超过清单声明大小")
                _emit_download_progress(
                    progress_callback,
                    downloaded,
                    total,
                    f"正在从 {source_name} 下载",
                )
    return downloaded


def download_file(url: str, dest_path: str, progress_callback=None) -> None:
    destination = Path(dest_path)
    destination.unlink(missing_ok=True)
    _download_from_source(
        url,
        destination,
        expected_size=0,
        progress_callback=progress_callback,
        deadline=time.monotonic() + DOWNLOAD_TOTAL_TIMEOUT,
        source_name=urllib.parse.urlparse(url).hostname or "download source",
    )


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
    names = [
        str(patch.get("filename") or "").strip(),
        Path(urllib.parse.urlparse(str(patch.get("url") or "")).path).name,
    ]
    for name in names:
        if name.lower().endswith(".zip") and re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            return name
    return f"LiveClipperPatch_{patch['from_version']}_to_{patch['to_version']}.zip"


def _verified_download(
    patch: dict[str, Any],
    destination: Path,
    progress_callback: DownloadProgress | None = None,
    *,
    total_timeout: int = DOWNLOAD_TOTAL_TIMEOUT,
) -> None:
    expected_hash = str(patch["sha256"]).lower()
    expected_size = int(patch["size"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if destination.stat().st_size == expected_size and sha256_file(destination) == expected_hash:
            _emit_download_progress(
                progress_callback,
                expected_size,
                expected_size,
                "已使用本地验证缓存",
            )
            return
        destination.unlink(missing_ok=True)

    temporary = destination.with_suffix(destination.suffix + ".part")
    if temporary.is_file() and temporary.stat().st_size > expected_size:
        temporary.unlink(missing_ok=True)
    if temporary.is_file() and temporary.stat().st_size == expected_size:
        _emit_download_progress(
            progress_callback,
            expected_size,
            expected_size,
            "正在校验已下载文件",
        )
        if sha256_file(temporary) == expected_hash:
            os.replace(temporary, destination)
            _emit_download_progress(
                progress_callback,
                expected_size,
                expected_size,
                "增量包下载并校验完成",
            )
            return
        temporary.unlink(missing_ok=True)

    sources = _patch_sources(patch)
    if not sources:
        raise RuntimeError("更新清单没有可用的 HTTPS 下载源")

    deadline = time.monotonic() + max(1, int(total_timeout))
    errors: list[str] = []
    for source in sources:
        source_name = source["name"]
        for attempt in range(1, DOWNLOAD_ATTEMPTS_PER_SOURCE + 1):
            if time.monotonic() >= deadline:
                errors.append("下载超过总时限")
                break
            try:
                _download_from_source(
                    source["url"],
                    temporary,
                    expected_size=expected_size,
                    progress_callback=progress_callback,
                    deadline=deadline,
                    source_name=source_name,
                )
                current_size = temporary.stat().st_size if temporary.is_file() else 0
                if current_size != expected_size:
                    raise RuntimeError(
                        f"文件不完整 {current_size}/{expected_size} 字节"
                    )
                _emit_download_progress(
                    progress_callback,
                    expected_size,
                    expected_size,
                    "正在校验增量包",
                )
                if sha256_file(temporary) != expected_hash:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError("SHA256 校验失败")
                os.replace(temporary, destination)
                _emit_download_progress(
                    progress_callback,
                    expected_size,
                    expected_size,
                    "增量包下载并校验完成",
                )
                return
            except Exception as exc:
                errors.append(
                    f"{source_name} 第{attempt}次: "
                    f"{type(exc).__name__}: {_safe_download_error(exc)}"
                )
                if temporary.is_file() and temporary.stat().st_size > expected_size:
                    temporary.unlink(missing_ok=True)
                remaining = deadline - time.monotonic()
                if attempt < DOWNLOAD_ATTEMPTS_PER_SOURCE and remaining > 0:
                    time.sleep(min(float(attempt), remaining))
        if time.monotonic() >= deadline:
            break

    partial_size = temporary.stat().st_size if temporary.is_file() else 0
    detail = "；".join(errors[-4:]) or "未知网络错误"
    raise RuntimeError(
        f"增量包下载失败，已保留断点 {partial_size}/{expected_size} 字节；{detail}"
    )


def _load_patch_manifest_from_archive(patch_path: Path, public_key: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(patch_path) as archive:
            manifest = json.loads(archive.read("patch_manifest.json").decode("utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"invalid patch archive: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("patch manifest is not an object")
    verify_manifest(manifest, public_key)
    return manifest


def _verify_signed_patch_manifest(
    patch: dict[str, Any],
    patch_path: Path,
    public_key: Path,
    expected_from: str,
) -> dict[str, Any]:
    expected_hash = str(patch["sha256"]).lower()
    expected_size = int(patch["size"])
    if patch_path.stat().st_size != expected_size:
        raise RuntimeError("patch size does not match release channel")
    if sha256_file(patch_path) != expected_hash:
        raise RuntimeError("patch SHA256 does not match release channel")
    manifest = _load_patch_manifest_from_archive(patch_path, public_key)
    if manifest.get("format") != DELTA_FORMAT:
        raise RuntimeError("unsupported patch format")
    if int(manifest.get("schema_version") or 0) != 3:
        raise RuntimeError("unsupported patch schema")
    from_version = str(manifest.get("from_version") or "")
    to_version = str(manifest.get("to_version") or "")
    if from_version != expected_from:
        raise RuntimeError("patch chain source version mismatch")
    if from_version != patch["from_version"] or to_version != patch["to_version"]:
        raise RuntimeError("patch version does not match release channel")
    if int(manifest.get("target_layout_version") or 0) != RUNTIME_LAYOUT_VERSION:
        raise RuntimeError("patch target is not Runtime V3")
    for field_name, version in (
        ("source_runtime_manifest", from_version),
        ("target_runtime_manifest", to_version),
    ):
        runtime_manifest = manifest.get(field_name)
        if not isinstance(runtime_manifest, dict):
            raise RuntimeError(f"patch missing {field_name}")
        verify_manifest(runtime_manifest, public_key)
        if str(runtime_manifest.get("version") or "") != version:
            raise RuntimeError(f"{field_name} version mismatch")
    install_manifest = manifest.get("target_install_manifest")
    if not isinstance(install_manifest, dict):
        raise RuntimeError("patch missing target install manifest")
    verify_manifest(install_manifest, public_key)
    if str(install_manifest.get("initial_version") or "") != to_version:
        raise RuntimeError("install manifest version mismatch")
    if install_manifest.get("files") != manifest.get("stable_result_files"):
        raise RuntimeError("install manifest stable result mismatch")
    stable_payload = manifest.get("stable_payload") or {}
    if not isinstance(stable_payload, dict):
        raise RuntimeError("stable payload must be an object")
    if stable_payload:
        raise RuntimeError("Runtime V3 chain patch cannot replace stable components")
    return manifest


def _runtime_manifest_total_size(manifest: dict[str, Any]) -> int:
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    total = 0
    for meta in files.values():
        if isinstance(meta, dict):
            try:
                total += max(0, int(meta.get("size") or 0))
            except Exception:
                pass
    return total


def _require_free_space(path: Path, required_bytes: int, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(path).free
    except Exception:
        return
    if free < required_bytes:
        needed_mb = required_bytes // (1024 * 1024)
        free_mb = free // (1024 * 1024)
        raise RuntimeError(f"not enough disk space for {label}: need {needed_mb} MB, free {free_mb} MB")


def _write_chain_plan(
    download_root: Path,
    target_version: str,
    source_version: str,
    entries: list[dict[str, Any]],
) -> Path:
    plan = {
        "schema_version": 1,
        "format": CHAIN_PLAN_FORMAT,
        "source_version": source_version,
        "target_version": target_version,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "patches": entries,
    }
    plan_path = download_root / "chain_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan_path.with_name(plan_path.name + ".tmp")
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, plan_path)
    return plan_path


def apply_update_headless(
    version_info: dict[str, Any] | None,
    progress_callback: DownloadProgress | None = None,
) -> dict[str, Any]:
    info = _normalize_release(version_info or {})
    if not _channel_is_active(info):
        return {
            "ok": False,
            "full_package_required": False,
            "restart_required": False,
            "version": _manifest_version(info),
            "updated": [],
            "failed": [],
            "msg": "Update channel is temporarily on hold.",
        }
    patches = info.get("selected_patches") if isinstance(info.get("selected_patches"), list) else None
    if patches is None:
        selected_patch = info.get("selected_patch") if isinstance(info.get("selected_patch"), dict) else None
        patches = [selected_patch] if selected_patch is not None else (_select_delta_chain(info) or [])
    patches = [patch for patch in patches if isinstance(patch, dict)]
    if not patches:
        return _full_package_result(info)
    if not delta_runtime_supported():
        return _full_package_result(info, "Runtime V3 is required for signed delta updates.")
    if not _updater_meets_minimum(info):
        return _full_package_result(info, "The installed updater is too old; install the full package once.")

    target_version = _manifest_version(info)
    source_version = str(patches[0].get("from_version") or _get_installed_version())
    download_root = _download_root(target_version)
    total_size = sum(int(patch["size"]) for patch in patches)
    _require_free_space(download_root, total_size + DISK_SPACE_BUFFER_BYTES, "patch download")

    public_key = _release_public_key_path()
    completed_bytes = 0
    expected_from = source_version
    downloaded_entries: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    for index, patch in enumerate(patches, start=1):
        patch_path = download_root / _patch_filename(patch)

        def chain_progress(done: int, _total: int, message: str, *, base: int = completed_bytes, item: int = index) -> None:
            _emit_download_progress(
                progress_callback,
                min(total_size, base + done),
                total_size,
                f"patch {item}/{len(patches)}: {message}",
            )

        _verified_download(
            patch,
            patch_path,
            progress_callback=chain_progress,
        )
        manifest = _verify_signed_patch_manifest(
            patch,
            patch_path,
            public_key,
            expected_from,
        )
        manifests.append(manifest)
        downloaded_entries.append(
            {
                "from_version": patch["from_version"],
                "to_version": patch["to_version"],
                "path": str(patch_path),
                "filename": patch_path.name,
                "sha256": str(patch["sha256"]).lower(),
                "size": int(patch["size"]),
                "sources": patch.get("sources") or [],
            }
        )
        completed_bytes += int(patch["size"])
        expected_from = str(patch["to_version"])

    apply_space = max(
        [_runtime_manifest_total_size(manifest.get("target_runtime_manifest") or {}) for manifest in manifests]
        or [0]
    )
    _require_free_space(_install_root(), apply_space + DISK_SPACE_BUFFER_BYTES, "patch staging")
    _emit_download_progress(
        progress_callback,
        total_size,
        total_size,
        "all patches downloaded and verified; starting updater",
    )

    plan_path = _write_chain_plan(download_root, target_version, source_version, downloaded_entries)
    agent_copy = download_root / "LiveClipperUpdater.exe"
    key_copy = download_root / "release_update_public_key.pem"
    shutil.copy2(_update_agent_path(), agent_copy)
    shutil.copy2(public_key, key_copy)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess,
            "DETACHED_PROCESS",
            0,
        )
    command = [
        str(agent_copy),
        "--install-root",
        str(_install_root()),
        "--public-key",
        str(key_copy),
        "--wait-pid",
        str(os.getpid()),
        "--non-interactive",
        "--show-progress",
    ]
    if len(downloaded_entries) == 1:
        command.extend(
            [
                "--patch",
                downloaded_entries[0]["path"],
                "--expected-patch-sha256",
                downloaded_entries[0]["sha256"],
            ]
        )
    else:
        command.extend(["--plan", str(plan_path)])
    subprocess.Popen(
        command,
        cwd=str(download_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    _emit_download_progress(
        progress_callback,
        total_size,
        total_size,
        "updater started; client will exit",
    )
    return {
        "ok": True,
        "full_package_required": False,
        "agent_started": True,
        "exit_required": True,
        "restart_required": True,
        "version": target_version,
        "patch_size": total_size,
        "patch_count": len(downloaded_entries),
        "patch_chain": [f"{entry['from_version']}->{entry['to_version']}" for entry in downloaded_entries],
        "updated": [],
        "failed": [],
        "msg": "Patches verified; updater will apply them and restart LiveClipper.",
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
    "_select_delta_chain",
]
