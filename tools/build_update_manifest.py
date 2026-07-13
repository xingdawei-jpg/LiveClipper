"""Build the immutable runtime manifest stored in app/version.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
WEB_DIR = ROOT / "web_client"
VERSION_FILE = APP_DIR / "version.json"
RELEASE_PAGE_URL = "https://github.com/xingdawei-jpg/LiveClipper/releases/latest"
RUNTIME_LAYOUT_VERSION = 2

APP_SKIP = {
    ".installed_version",
    "_package_final.py",
    "_toggle_monitor.py",
    "clip_tuple_check.py",
    "gui_clean.py",
    "gui_fresh.py",
    "gui_tmp.py",
    "license_generator.py",
    "license_server.py",
    "license_feishu_backend.py",
    "license_stats_store.py",
    "feishu_scheduler.py",
    "live_recorder_page_BACKUP.py",
    "verify.py",
    "version.json",
}

RUNTIME_FILES = [
    APP_DIR / "keywords.json",
    APP_DIR / "license_public_key.txt",
    WEB_DIR / "frontend" / "index.html",
    WEB_DIR / "frontend" / "assets" / "app.js",
    WEB_DIR / "frontend" / "assets" / "styles.css",
    WEB_DIR / "frontend" / "assets" / "liveclipper.ico",
    WEB_DIR / "tools" / "douyin_active_product_probe_poc.py",
    WEB_DIR / "tools" / "douyin_chrome_live_poc.py",
]

SOURCE_FILES = [
    WEB_DIR / "desktop.py",
    WEB_DIR / "server.py",
    WEB_DIR / "liveclipper_web.spec",
    ROOT / "tools" / "build_update_manifest.py",
    ROOT / "tools" / "build_release_channel.py",
]

TEXT_SUFFIXES = {".py", ".json", ".txt", ".html", ".css", ".js", ".spec"}


def _manifest_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return data


def _sha256(path: Path) -> str:
    return hashlib.sha256(_manifest_bytes(path)).hexdigest()


def _source_files() -> list[Path]:
    result = list(SOURCE_FILES)
    for path in sorted(APP_DIR.iterdir()):
        if not path.is_file() or path.name in APP_SKIP or path.name.startswith("_"):
            continue
        if ".bak" in path.name:
            continue
        if path.suffix.lower() in {".py", ".json", ".txt"}:
            result.append(path)
    return list(dict.fromkeys(path for path in result if path.exists()))


def _hash_map(paths: list[Path]) -> dict[str, str]:
    return {path.relative_to(ROOT).as_posix(): _sha256(path) for path in paths if path.exists()}


def build_manifest(version: str, release_notes: str, force_update: bool = False) -> dict:
    runtime_files = _hash_map(RUNTIME_FILES)
    source_files = _hash_map(_source_files())
    return {
        "schema_version": 2,
        "runtime_layout_version": RUNTIME_LAYOUT_VERSION,
        "build_id": version,
        "version": version,
        "latest_version": version,
        "update_strategy": "full-package",
        "supports_incremental_updates": False,
        "requires_full_package": True,
        "requires_full_package_note": "本版本采用整包升级。程序代码不会再写入用户数据目录，请下载完整包后替换旧程序。",
        "release_page_url": RELEASE_PAGE_URL,
        "package_url": "",
        "package_sha256": "",
        "package_size": 0,
        # Kept empty deliberately: legacy clients treat this field as files to
        # copy into AppData. Runtime v2 never publishes program-file deltas.
        "files": {},
        "runtime_files": runtime_files,
        "integrity_files": list(runtime_files),
        "source_files": source_files,
        "release_notes": release_notes,
        "update_message": release_notes,
        "update_url": "",
        "force_update": bool(force_update),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the LiveClipper runtime manifest.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--requires-full-package", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--recovery", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    manifest = build_manifest(args.version, args.notes or f"v{args.version} update", args.force)
    if args.check and VERSION_FILE.exists():
        current = json.loads(VERSION_FILE.read_text(encoding="utf-8-sig"))
        manifest["updated_at"] = current.get("updated_at") or manifest["updated_at"]
        if current != manifest:
            print("app/version.json is not up to date.")
            return 1
        print("app/version.json is up to date.")
        return 0

    VERSION_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {VERSION_FILE}")
    print(f"runtime_files={len(manifest['runtime_files'])} source_files={len(manifest['source_files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
