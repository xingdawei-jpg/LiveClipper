"""Build app/version.json for LiveClipper incremental updates."""

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
    "licenses.json",
    "generated_codes.json",
    "ai_settings.json",
    "version.json",
    "live_cutter.spec",
    "live_recorder_page_BACKUP.py",
    "verify.py",
}
APP_SUFFIXES = {".py", ".json", ".txt"}
WEB_FILES = [
    WEB_DIR / "server.py",
    WEB_DIR / "frontend" / "index.html",
    WEB_DIR / "frontend" / "assets" / "app.js",
    WEB_DIR / "frontend" / "assets" / "styles.css",
    WEB_DIR / "frontend" / "assets" / "liveclipper.ico",
    WEB_DIR / "tools" / "douyin_active_product_probe_poc.py",
    WEB_DIR / "tools" / "douyin_chrome_live_poc.py",
]


TEXT_SUFFIXES = {".py", ".json", ".txt", ".html", ".css", ".js"}


def _manifest_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        # GitHub raw serves repository-normalized LF text for these files.
        data = data.replace(b"\r\n", b"\n")
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(_manifest_bytes(path))
    return digest.hexdigest()


def _app_files() -> list[Path]:
    result: list[Path] = []
    for path in sorted(APP_DIR.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith("_"):
            continue
        if path.name in APP_SKIP:
            continue
        if ".bak" in path.name:
            continue
        if path.suffix.lower() in APP_SUFFIXES:
            result.append(path)
    return result


def build_manifest(version: str, release_notes: str, force_update: bool = False) -> dict:
    files: dict[str, str] = {}
    for path in _app_files():
        files[f"app/{path.name}"] = _sha256(path)
    for path in WEB_FILES:
        if path.exists():
            files[path.relative_to(ROOT).as_posix()] = _sha256(path)
    return {
        "version": version,
        "latest_version": version,
        "files": files,
        "release_notes": release_notes,
        "update_url": "",
        "update_message": release_notes,
        "requires_full_package_note": "如果当前客户端提示旧完整包或用户喜好库刷新 Not Found，请关闭旧包，下载新版完整包后再使用；旧启动器不能安全应用后端增量更新。",
        "force_update": bool(force_update),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LiveClipper incremental update manifest.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check", action="store_true", help="Do not write; fail if manifest differs.")
    args = parser.parse_args()

    if args.check:
        current = VERSION_FILE.read_text(encoding="utf-8")
        try:
            current_manifest = json.loads(current)
            current_updated_at = current_manifest.get("updated_at")
        except Exception:
            current_updated_at = None
        manifest = build_manifest(args.version, args.notes or f"v{args.version} update", args.force)
        if current_updated_at:
            manifest["updated_at"] = current_updated_at
        text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        if current != text:
            print("app/version.json is not up to date.")
            return 1
        print("app/version.json is up to date.")
        return 0
    manifest = build_manifest(args.version, args.notes or f"v{args.version} update", args.force)
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    VERSION_FILE.write_text(text, encoding="utf-8")
    print(f"wrote {VERSION_FILE}")
    print(f"files={len(manifest['files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
