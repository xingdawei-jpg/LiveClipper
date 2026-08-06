"""Build a complete Runtime V4 full-baseline package ZIP.

Usage:
    python tools/build_v4_full_package.py --version 2026.8.5.2 [--skip-core] [--skip-launcher]
"""

from __future__ import annotations

import argparse
import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.release_signing import sha256_file


PYTHON = os.environ.get("LIVECLIPPER_PYTHON", sys.executable)
DIST_ROOT = Path(os.environ.get("LIVECLIPPER_V4_BUILD_ROOT", "C:\\lc_v4_build"))  # short path
CORE_SPEC = ROOT / "runtime_v4" / "liveclipper_host_v4.spec"
LAUNCHER_SPEC = ROOT / "runtime_v4" / "liveclipper_launcher_v4.spec"
BUSINESS_TOOL = ROOT / "tools" / "build_business_bundle.py"
CORE_TOOL = ROOT / "tools" / "build_v4_core_manifest.py"
PRIVATE_KEY = Path(os.environ.get(
    "LIVECLIPPER_V4_PRIVATE_KEY",
    str(Path.home() / ".liveclipper-keys" / "release_update_private_key.pem"),
))
PUBLIC_KEY = ROOT / "app" / "release_update_public_key.pem"
BUSINESS_POLICY = ROOT / "release" / "runtime_v4_business_policy.json"
WEBVIEW2_DIR = os.environ.get(
    "LIVECLIPPER_WEBVIEW2_RUNTIME_DIR",
    str(ROOT / "vendor" / "webview2_runtime_x64" / "Microsoft.WebView2.FixedVersionRuntime.149.0.4022.98.x64"),
)
LAUNCHER_NAME = os.environ.get("LIVECLIPPER_V4_LAUNCHER_NAME", "LiveClipperWeb")
HEALTH_TIMEOUT = 180


def _run(cmd: list[str], label: str) -> int:
    print(f"[{label}] {cmd[0]} ...")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT), env={**os.environ})
    elapsed = time.time() - t0
    if proc.returncode != 0:
        print(f"[ERROR] {label} failed (exit {proc.returncode}) after {elapsed:.0f}s")
    else:
        print(f"[OK] {label} ({elapsed:.0f}s)")
    return proc.returncode


def build_core() -> Path:
    """Build the frozen Core Host via PyInstaller."""
    core_dist = DIST_ROOT / "core_dist"
    core_work = DIST_ROOT / "core_work"
    if core_dist.exists():
        shutil.rmtree(core_dist)
    if core_work.exists():
        shutil.rmtree(core_work)
    env = {
        **os.environ,
        "LIVECLIPPER_WEBVIEW2_RUNTIME_DIR": WEBVIEW2_DIR,
    }
    rc = subprocess.run(
        [PYTHON, "-m", "PyInstaller",
         str(CORE_SPEC), "--distpath", str(core_dist), "--workpath", str(core_work)],
        cwd=str(ROOT), env=env,
    ).returncode
    if rc != 0:
        raise RuntimeError(f"Core build failed (exit {rc})")
    host_dir = core_dist / "LiveClipperHost"
    if not (host_dir / "LiveClipperHost.exe").exists():
        raise RuntimeError(f"Core exe not found at {host_dir}")
    return host_dir


def build_launcher() -> Path:
    """Build the frozen Launcher via PyInstaller."""
    launcher_dist = DIST_ROOT / "launcher_dist"
    launcher_work = DIST_ROOT / "launcher_work"
    if launcher_dist.exists():
        shutil.rmtree(launcher_dist)
    if launcher_work.exists():
        shutil.rmtree(launcher_work)
    env = {
        **os.environ,
        "LIVECLIPPER_V4_LAUNCHER_NAME": LAUNCHER_NAME,
    }
    rc = subprocess.run(
        [PYTHON, "-m", "PyInstaller",
         str(LAUNCHER_SPEC), "--distpath", str(launcher_dist), "--workpath", str(launcher_work)],
        cwd=str(ROOT), env=env,
    ).returncode
    if rc != 0:
        raise RuntimeError(f"Launcher build failed (exit {rc})")
    exe = launcher_dist / f"{LAUNCHER_NAME}.exe"
    if not exe.exists():
        raise RuntimeError(f"Launcher exe not found at {exe}")
    return exe


def build_business(version: str) -> Path:
    """Build and sign the business bundle ZIP."""
    biz_dir = DIST_ROOT / "business"
    biz_dir.mkdir(parents=True, exist_ok=True)
    archive = biz_dir / f"LiveClipperBusiness_{version}.zip"
    # use the internal API directly (avoids shell escaping with private key path)
    sys.path.insert(0, str(ROOT))
    from runtime_v4.business_bundle import build_business_archive
    result = build_business_archive(
        str(ROOT),
        str(archive),
        application_version=version,
        private_key_path=str(PRIVATE_KEY),
        policy_path=str(BUSINESS_POLICY),
    )
    print(f"[business] {result['file_count']} files, {result['compressed_size']} bytes")
    return archive


def sign_core(core_dir: Path) -> dict:
    """Sign the Core manifest."""
    sys.path.insert(0, str(ROOT))
    from runtime_v4.core_manifest import build_core_manifest
    return build_core_manifest(
        str(core_dir),
        core_version="4.0.0",
        private_key_path=str(PRIVATE_KEY),
        entrypoint="LiveClipperHost.exe",
    )


def assemble_package(
    core_dir: Path,
    launcher_exe: Path,
    business_archive: Path,
    version: str,
    *,
    backup_business_version: str | None = None,
    backup_business_archive: Path | None = None,
) -> Path:
    """Assemble the final LiveClipperWeb directory, ready for zipping."""
    root = DIST_ROOT / "pkg" / "LiveClipperWeb"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    # 1. Core
    core_target = root / "core" / "4.0.0"
    core_target.mkdir(parents=True)
    shutil.copytree(str(core_dir), str(core_target), dirs_exist_ok=True)

    # 2. Launcher
    shutil.copy2(str(launcher_exe), str(root / launcher_exe.name))

    # 3. Primary business (current)
    biz_target = root / "versions" / version
    biz_target.mkdir(parents=True)
    with zipfile.ZipFile(str(business_archive)) as z:
        z.extractall(str(biz_target))

    # 4. Backup business (rollback target)
    if backup_business_version and backup_business_archive:
        backup_target = root / "versions" / backup_business_version
        backup_target.mkdir(parents=True)
        with zipfile.ZipFile(str(backup_business_archive)) as z:
            z.extractall(str(backup_target))

    # 5. current.json
    cm = json.loads((core_target / "core_manifest.json").read_text(encoding="utf-8"))
    cm_bytes = json.dumps(cm, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    mf_hash = hashlib.sha256(cm_bytes).hexdigest()

    state = {
        "schema_version": 1,
        "runtime_layout_version": 4,
        "current": {"application_version": version, "core_version": "4.0.0"},
        "previous": (
            {"application_version": backup_business_version, "core_version": "4.0.0"}
            if backup_business_version else None
        ),
        "pending": False,
        "verified_cores": {
            "4.0.0": {
                "verification_mode": "full",
                "manifest_sha256": mf_hash,
                "metadata_sha256": "",
                "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        },
        "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rollback_reason": "",
    }
    (root / "current.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return root


def verify_package(package_dir: Path) -> bool:
    """Quick sanity check: core_manifest matches, key files present."""
    core = package_dir / "core" / "4.0.0"
    cm = json.loads((core / "core_manifest.json").read_text(encoding="utf-8"))
    exe_path = core / "LiveClipperHost.exe"
    if not exe_path.exists():
        print("[ERROR] core exe missing")
        return False
    expected_size = cm["files"]["LiveClipperHost.exe"]["size"]
    actual_size = exe_path.stat().st_size
    if expected_size != actual_size:
        print(f"[ERROR] exe size mismatch: manifest={expected_size} actual={actual_size}")
        return False
    for p in [
        package_dir / f"{LAUNCHER_NAME}.exe",
        package_dir / "current.json",
        core / "core_manifest.sig",
        core / "_internal" / "web_client" / "desktop.py",
    ]:
        if not p.exists():
            print(f"[ERROR] missing: {p.relative_to(package_dir)}")
            return False
    print("[OK] package verified")
    return True


def zip_package(package_dir: Path, version: str) -> tuple[Path, str]:
    """Create the final distributable ZIP."""
    zip_path = Path.home() / "Desktop" / f"LiveClipperWeb_v4.0.0_{version}_全量包.zip"
    if zip_path.exists():
        zip_path.unlink()
    count = 0
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for rt, dirs, files in os.walk(str(package_dir)):
            for f in files:
                full = os.path.join(rt, f)
                rel = os.path.relpath(full, str(package_dir.parent))
                z.write(full, rel)
                count += 1
    sha = sha256_file(str(zip_path))
    (zip_path.parent / f"{zip_path.name}.sha256.txt").write_text(
        f"{sha}  {zip_path.name}\n", encoding="utf-8"
    )
    size_mb = zip_path.stat().st_size / (1024 ** 2)
    print(f"[zip] {count} files, {size_mb:.0f}MB, sha256={sha[:16]}")
    return zip_path, sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Business version, e.g. 2026.8.5.2")
    parser.add_argument("--skip-core", action="store_true", help="Skip Core rebuild (use existing)")
    parser.add_argument("--skip-launcher", action="store_true", help="Skip Launcher rebuild")
    parser.add_argument("--backup-version", help="Backup business version for rollback, e.g. 2026.8.5.1")
    parser.add_argument("--backup-archive", type=Path, help="Path to backup business ZIP")
    args = parser.parse_args()

    if not PRIVATE_KEY.exists():
        print(f"[ERROR] private key not found: {PRIVATE_KEY}")
        return 1

    DIST_ROOT.mkdir(parents=True, exist_ok=True)

    # Step 1: Core
    if not args.skip_core:
        print("--- Step 1: Build Core ---")
        core_dir = build_core()
        print("--- Step 1a: Sign Core ---")
        sign_result = sign_core(core_dir)
        print(f"      manifest={sign_result['manifest_sha256'][:16]} files={sign_result['file_count']}")
    else:
        core_dir = DIST_ROOT / "core_dist" / "LiveClipperHost"
        if not core_dir.exists():
            print("[ERROR] --skip-core but no existing core at", core_dir)
            return 1
        print(f"--- Step 1: Skip Core (using {core_dir}) ---")

    # Step 2: Launcher
    if not args.skip_launcher:
        print("--- Step 2: Build Launcher ---")
        launcher_exe = build_launcher()
    else:
        launcher_exe = DIST_ROOT / "launcher_dist" / f"{LAUNCHER_NAME}.exe"
        if not launcher_exe.exists():
            print("[ERROR] --skip-launcher but no existing launcher at", launcher_exe)
            return 1
        print(f"--- Step 2: Skip Launcher (using {launcher_exe}) ---")

    # Step 3: Business
    print("--- Step 3: Build Business Bundle ---")
    business_archive = build_business(args.version)

    # Step 4: Assemble
    print("--- Step 4: Assemble Package ---")
    package_dir = assemble_package(
        core_dir, launcher_exe, business_archive, args.version,
        backup_business_version=args.backup_version,
        backup_business_archive=args.backup_archive,
    )

    # Step 5: Verify
    print("--- Step 5: Verify ---")
    if not verify_package(package_dir):
        return 1

    # Step 6: Zip
    print("--- Step 6: Zip ---")
    zip_path, sha = zip_package(package_dir, args.version)

    print("\nDone. Package:", zip_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
