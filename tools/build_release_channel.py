"""Build release/stable.json from a completed full-package archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "app" / "version.json"
CHANNEL_FILE = ROOT / "release" / "stable.json"
GITHUB_REPO = "xingdawei-jpg/LiveClipper"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the stable full-package release channel.")
    parser.add_argument("package", type=Path)
    parser.add_argument("--url", default="")
    parser.add_argument("--output", type=Path, default=CHANNEL_FILE)
    args = parser.parse_args()

    package = args.package.resolve()
    if not package.is_file():
        parser.error(f"package not found: {package}")

    runtime = json.loads(VERSION_FILE.read_text(encoding="utf-8-sig"))
    version = str(runtime.get("version") or runtime.get("latest_version") or "")
    if not version:
        parser.error("app/version.json has no version")
    package_url = args.url or (
        f"https://github.com/{GITHUB_REPO}/releases/download/v{version}/{package.name}"
    )
    release_page = f"https://github.com/{GITHUB_REPO}/releases/tag/v{version}"
    manifest = {
        "schema_version": 2,
        "channel": "stable",
        "version": version,
        "latest_version": version,
        "runtime_layout_version": int(runtime.get("runtime_layout_version") or 2),
        "minimum_runtime_layout_version": 2,
        "update_strategy": "full-package",
        "supports_incremental_updates": False,
        "requires_full_package": True,
        "requires_full_package_note": runtime.get("requires_full_package_note") or "请下载完整包更新。",
        "release_notes": runtime.get("release_notes") or "",
        "force_update": bool(runtime.get("force_update", False)),
        "release_page_url": release_page,
        "package": {
            "format": "zip",
            "url": package_url,
            "sha256": _sha256(package),
            "size": package.stat().st_size,
            "filename": package.name,
        },
        "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output}")
    print(f"sha256={manifest['package']['sha256']} size={manifest['package']['size']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
