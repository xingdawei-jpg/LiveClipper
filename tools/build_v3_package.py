"""Assemble a signed Runtime V3 LiveClipper package from a PyInstaller runtime."""

from __future__ import annotations

import argparse
import sys
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath

APP_IMPORT_DIR = Path(__file__).resolve().parents[1] / "app"
if APP_IMPORT_DIR.is_dir() and str(APP_IMPORT_DIR) not in sys.path:
    sys.path.insert(0, str(APP_IMPORT_DIR))


from release_signing import sha256_file, sign_manifest
from runtime_v3_versions import LAUNCHER_VERSION, UPDATER_VERSION


RUNTIME_MANIFEST_FORMAT = "liveclipper-runtime-manifest-v1"
INSTALL_MANIFEST_FORMAT = "liveclipper-install-manifest-v1"
DEFAULT_ENTRYPOINT = "LiveClipperWeb.exe"
FIXED_WEBVIEW2_RUNTIME = (
    Path("_internal")
    / "webview2_runtime"
    / "msedgewebview2.exe"
)
BUNDLED_MEDIA_TOOLS = (
    Path("_internal") / "ffmpeg" / "ffmpeg.exe",
    Path("_internal") / "ffmpeg" / "ffprobe.exe",
)
RUNTIME_MANIFEST = "runtime_manifest.json"
INSTALL_MANIFEST = "install_manifest.json"


def _version_from_runtime(runtime_dir: Path) -> str:
    path = runtime_dir / "_internal" / "app" / "version.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    version = str(data.get("version") or data.get("latest_version") or "").strip()
    if not version:
        raise ValueError(f"runtime has no version: {path}")
    return version


def _source_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _file_map(root: Path, *, exclude: set[str] | None = None) -> dict[str, dict[str, object]]:
    excluded = exclude or set()
    result: dict[str, dict[str, object]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        result[relative] = {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
    return result


def _copy_or_link(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _clone_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            _copy_or_link(path, target)


def build_runtime_manifest(
    runtime_dir: Path,
    version: str,
    private_key: Path,
    source_commit: str = "",
) -> dict:
    manifest = {
        "schema_version": 3,
        "format": RUNTIME_MANIFEST_FORMAT,
        "runtime_layout_version": 3,
        "version": version,
        "entrypoint": DEFAULT_ENTRYPOINT,
        "source_commit": source_commit,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": _file_map(runtime_dir, exclude={RUNTIME_MANIFEST}),
    }
    return sign_manifest(manifest, private_key)


def build_install_manifest(
    package_root: Path,
    version: str,
    private_key: Path,
) -> dict:
    stable_paths = [
        "LiveClipperWeb.exe",
        "updater/LiveClipperUpdater.exe",
        "updater/release_update_public_key.pem",
    ]
    files: dict[str, dict[str, object]] = {}
    for relative in stable_paths:
        path = package_root / Path(*PurePosixPath(relative).parts)
        if not path.is_file():
            raise FileNotFoundError(path)
        files[relative] = {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
    manifest = {
        "schema_version": 3,
        "format": INSTALL_MANIFEST_FORMAT,
        "runtime_layout_version": 3,
        "initial_version": version,
        "launcher_version": LAUNCHER_VERSION,
        "updater_version": UPDATER_VERSION,
        "files": files,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return sign_manifest(manifest, private_key)


def assemble(
    runtime_dir: Path,
    output_root: Path,
    launcher: Path,
    updater: Path,
    public_key: Path,
    private_key: Path,
    repo_root: Path,
) -> str:
    runtime_dir = runtime_dir.resolve()
    required_runtime_files = (
        runtime_dir / DEFAULT_ENTRYPOINT,
        runtime_dir / FIXED_WEBVIEW2_RUNTIME,
        *(runtime_dir / path for path in BUNDLED_MEDIA_TOOLS),
    )
    missing_runtime_files = [
        str(path.relative_to(runtime_dir))
        for path in required_runtime_files
        if not path.is_file()
    ]
    if missing_runtime_files:
        raise FileNotFoundError(
            "runtime is not a self-contained desktop build; missing "
            + ", ".join(missing_runtime_files)
        )
    output_root = output_root.resolve()
    if output_root.exists():
        raise FileExistsError(f"output already exists: {output_root}")
    version = _version_from_runtime(runtime_dir)
    target_runtime = output_root / "versions" / version
    _clone_tree(runtime_dir, target_runtime)

    output_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(launcher, output_root / DEFAULT_ENTRYPOINT)
    updater_dir = output_root / "updater"
    updater_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(updater, updater_dir / "LiveClipperUpdater.exe")
    shutil.copy2(public_key, updater_dir / "release_update_public_key.pem")

    runtime_manifest = build_runtime_manifest(
        target_runtime,
        version,
        private_key,
        _source_commit(repo_root),
    )
    (target_runtime / RUNTIME_MANIFEST).write_text(
        json.dumps(runtime_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    state = {
        "schema_version": 1,
        "runtime_layout_version": 3,
        "current_version": version,
        "previous_version": "",
        "pending": False,
        "generation": 1,
        "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (output_root / "current.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    install_manifest = build_install_manifest(output_root, version, private_key)
    (output_root / INSTALL_MANIFEST).write_text(
        json.dumps(install_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return version


def build_zip(
    package_root: Path,
    output_zip: Path,
    compresslevel: int = 6,
) -> tuple[int, str]:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        raise FileExistsError(output_zip)
    count = 0
    with zipfile.ZipFile(
        output_zip,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=compresslevel,
        allowZip64=True,
    ) as archive:
        for path in sorted(item for item in package_root.rglob("*") if item.is_file()):
            relative = path.relative_to(package_root).as_posix()
            archive.write(path, (PurePosixPath("LiveClipperWeb") / relative).as_posix())
            count += 1
    digest = sha256_file(output_zip).upper()
    output_zip.with_suffix(output_zip.suffix + ".sha256.txt").write_text(
        f"{digest}  {output_zip.name}\n",
        encoding="ascii",
    )
    return count, digest


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble a signed LiveClipper Runtime V3 package.")
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--updater", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--zip", dest="output_zip", type=Path)
    parser.add_argument("--compress-level", type=int, choices=range(0, 10), default=6)
    args = parser.parse_args()
    version = assemble(
        args.runtime_dir,
        args.output_root,
        args.launcher,
        args.updater,
        args.public_key,
        args.private_key,
        args.repo_root.resolve(),
    )
    print(f"assembled Runtime V3 {version}: {args.output_root.resolve()}")
    if args.output_zip:
        count, digest = build_zip(
            args.output_root.resolve(),
            args.output_zip.resolve(),
            compresslevel=args.compress_level,
        )
        print(f"zip={args.output_zip.resolve()} entries={count} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
