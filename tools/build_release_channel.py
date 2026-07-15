"""Build and sign release/stable.json for Runtime V3."""

from __future__ import annotations

import argparse
import sys
import json
import time
import urllib.parse
import zipfile
from pathlib import Path

APP_IMPORT_DIR = Path(__file__).resolve().parents[1] / "app"
if APP_IMPORT_DIR.is_dir() and str(APP_IMPORT_DIR) not in sys.path:
    sys.path.insert(0, str(APP_IMPORT_DIR))


from release_signing import sha256_file, sign_manifest, verify_manifest


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "app" / "version.json"
CHANNEL_FILE = ROOT / "release" / "stable.json"
PUBLIC_KEY_FILE = ROOT / "app" / "release_update_public_key.pem"
GITHUB_REPO = "xingdawei-jpg/LiveClipper"


def _download_sources(urls: list[str]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_url in urls:
        url = str(raw_url or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError(f"patch source must be a direct HTTPS URL: {url}")
        if url in seen:
            continue
        seen.add(url)
        hostname = parsed.hostname or "download source"
        if hostname.endswith("github.com"):
            name = "GitHub"
        elif hostname.endswith("aliyuncs.com"):
            name = "Aliyun OSS"
        else:
            name = hostname
        sources.append({"name": name, "url": url})
    if not sources:
        raise ValueError("patch has no download source")
    return sources


def _patch_record(path: Path, urls: list[str]) -> dict:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("patch_manifest.json").decode("utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError(f"invalid patch manifest: {path}")
    verify_manifest(manifest, PUBLIC_KEY_FILE)
    sources = _download_sources(urls)
    return {
        "format": manifest.get("format"),
        "from_version": str(manifest.get("from_version") or ""),
        "to_version": str(manifest.get("to_version") or ""),
        "source_layout_version": int(manifest.get("source_layout_version") or 0),
        "target_layout_version": int(manifest.get("target_layout_version") or 0),
        "url": sources[0]["url"],
        "urls": [source["url"] for source in sources],
        "sources": sources,
        "sha256": sha256_file(path).upper(),
        "size": path.stat().st_size,
        "filename": path.name,
        "runtime_payload_files": len(manifest.get("runtime_payload") or {}),
        "stable_payload_files": len(manifest.get("stable_payload") or {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the signed Runtime V3 stable channel.")
    parser.add_argument("package", type=Path)
    parser.add_argument("--url", default="")
    parser.add_argument("--patch", type=Path, action="append", default=[])
    parser.add_argument("--patch-url", action="append", default=[])
    parser.add_argument(
        "--patch-mirror",
        action="append",
        default=[],
        metavar="PATCH_FILENAME=HTTPS_URL",
    )
    parser.add_argument(
        "--channel-status",
        choices=("auto", "ready", "hold", "awaiting-external-distribution"),
        default="auto",
    )
    parser.add_argument("--bridge-url", default="")
    parser.add_argument("--max-chain-depth", type=int, default=8)
    parser.add_argument("--rollup-after-versions", type=int, default=2)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=CHANNEL_FILE)
    args = parser.parse_args()

    package = args.package.resolve()
    if not package.is_file():
        parser.error(f"package not found: {package}")
    if len(args.patch) != len(args.patch_url):
        parser.error("each --patch must have a matching --patch-url")

    runtime = json.loads(VERSION_FILE.read_text(encoding="utf-8-sig"))
    version = str(runtime.get("version") or runtime.get("latest_version") or "")
    if not version:
        parser.error("app/version.json has no version")
    package_url = args.url
    release_page = (
        f"https://github.com/{GITHUB_REPO}/releases/tag/v{version}"
        if package_url.startswith("https://github.com/")
        else ""
    )
    mirrors: dict[str, list[str]] = {}
    for value in args.patch_mirror:
        filename, separator, url = str(value).partition("=")
        filename = filename.strip()
        url = url.strip()
        if not separator or not filename or not url:
            parser.error("--patch-mirror must be PATCH_FILENAME=HTTPS_URL")
        mirrors.setdefault(filename, []).append(url)

    patches = []
    try:
        for path, url in zip(args.patch, args.patch_url, strict=True):
            resolved = path.resolve()
            patches.append(
                _patch_record(
                    resolved,
                    [url, *mirrors.get(resolved.name, [])],
                )
            )
    except ValueError as exc:
        parser.error(str(exc))
    for patch in patches:
        if patch["to_version"] != version:
            parser.error(
                f"patch target {patch['to_version']} does not match channel version {version}"
            )

    if args.channel_status == "auto":
        channel_status = (
            "ready"
            if package_url or patches
            else "awaiting-external-distribution"
        )
    else:
        channel_status = args.channel_status
    published_patches = patches if channel_status == "ready" else []
    incremental_ready = channel_status == "ready" and bool(published_patches)

    manifest = {
        "schema_version": 3,
        "channel": "stable",
        "channel_status": channel_status,
        "version": version,
        "latest_version": version,
        "runtime_layout_version": 3,
        "minimum_runtime_layout_version": 2,
        "minimum_launcher_version": str(runtime.get("minimum_launcher_version") or "1.0.0"),
        "minimum_updater_version": str(runtime.get("minimum_updater_version") or "1.0.0"),
        "update_strategy": (
            "verified-version-delta-with-full-fallback"
            if incremental_ready
            else ("hold" if channel_status == "hold" else "full-package")
        ),
        "supports_incremental_updates": incremental_ready,
        "requires_full_package": not incremental_ready,
        "requires_full_package_note": "没有匹配当前版本的签名补丁时，请使用完整包。",
        "release_notes": runtime.get("release_notes") or "",
        "force_update": bool(runtime.get("force_update", False)),
        "release_page_url": release_page,
        "bridge_url": args.bridge_url,
        "patch_policy": {
            "graph_version": 1,
            "max_chain_depth": max(1, int(args.max_chain_depth)),
            "rollup_after_versions": max(1, int(args.rollup_after_versions)),
        },
        "package": {
            "format": "zip",
            "url": package_url,
            "sha256": sha256_file(package).upper(),
            "size": package.stat().st_size,
            "filename": package.name,
        },
        "patches": published_patches,
        "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    signed = sign_manifest(manifest, args.private_key.resolve())
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(signed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output}")
    print(
        f"package_sha256={signed['package']['sha256']} "
        f"size={signed['package']['size']} patches={len(published_patches)} "
        f"status={channel_status}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
