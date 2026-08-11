"""Build a complete Runtime V4 full-baseline package ZIP.

Usage:
    python tools/build_v4_full_package.py --version 2026.8.5.2 --core-version 4.0.0
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
from runtime_v4.business_bundle import (
    extract_verified_business_archive,
    verify_business_directory,
)
from runtime_v4.core_manifest import verify_core_directory
from runtime_v4.desktop_host import CORE_VERSION as HOST_CORE_VERSION
from runtime_v4.launcher import LAUNCHER_VERSION


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
VERSION_FILE = ROOT / "app" / "version.json"
DEFAULT_CORE_VERSION = HOST_CORE_VERSION
WEBVIEW2_DIR = os.environ.get(
    "LIVECLIPPER_WEBVIEW2_RUNTIME_DIR",
    str(ROOT / "vendor" / "webview2_runtime_x64" / "Microsoft.WebView2.FixedVersionRuntime.149.0.4022.98.x64"),
)
LAUNCHER_NAME = os.environ.get("LIVECLIPPER_V4_LAUNCHER_NAME", "LiveClipperWeb")
HEALTH_TIMEOUT = 180


def validate_embedded_core_version(core_version: str) -> None:
    """Reject a full build whose frozen identities do not match its signed Core."""

    target = str(core_version or "").strip()
    if target != HOST_CORE_VERSION:
        raise RuntimeError(
            "target Core version differs from the embedded Host identity: "
            f"{target!r} != {HOST_CORE_VERSION!r}"
        )
    if target != LAUNCHER_VERSION:
        raise RuntimeError(
            "target Core version differs from the embedded Launcher identity: "
            f"{target!r} != {LAUNCHER_VERSION!r}"
        )


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
    try:
        ui_version = str(json.loads(VERSION_FILE.read_text(encoding="utf-8"))["version"])
    except Exception as exc:
        raise RuntimeError(f"Cannot read application version: {VERSION_FILE}") from exc
    if ui_version != version:
        raise RuntimeError(
            "Business package version differs from app/version.json: "
            f"--version={version}, app/version.json={ui_version}. "
            "Run tools/build_update_manifest.py before packaging."
        )
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


def sign_core(core_dir: Path, core_version: str = DEFAULT_CORE_VERSION) -> dict:
    """Sign the Core manifest."""
    sys.path.insert(0, str(ROOT))
    from runtime_v4.core_manifest import build_core_manifest
    return build_core_manifest(
        str(core_dir),
        core_version=core_version,
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
    core_version: str = DEFAULT_CORE_VERSION,
) -> Path:
    """Assemble the final LiveClipperWeb directory, ready for zipping."""
    root = DIST_ROOT / "pkg" / "LiveClipperWeb"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    if bool(backup_business_version) != bool(backup_business_archive):
        raise RuntimeError("Backup version and backup archive must be supplied together")
    if backup_business_version == version:
        raise RuntimeError("Backup business version must differ from the current version")

    # 1. Core
    core_target = root / "core" / core_version
    core_target.mkdir(parents=True)
    shutil.copytree(str(core_dir), str(core_target), dirs_exist_ok=True)

    # 2. Launcher
    shutil.copy2(str(launcher_exe), str(root / launcher_exe.name))

    # 3. Primary business (current)
    biz_target = root / "versions" / version
    extract_verified_business_archive(
        business_archive,
        biz_target,
        PUBLIC_KEY,
        expected_version=version,
        expected_core_version=core_version,
    )

    # 4. Backup business (rollback target)
    if backup_business_version and backup_business_archive:
        backup_target = root / "versions" / backup_business_version
        extract_verified_business_archive(
            backup_business_archive,
            backup_target,
            PUBLIC_KEY,
            expected_version=backup_business_version,
            expected_core_version=core_version,
        )

    # 5. current.json
    cm = json.loads((core_target / "core_manifest.json").read_text(encoding="utf-8"))
    cm_bytes = json.dumps(cm, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    mf_hash = hashlib.sha256(cm_bytes).hexdigest()

    state = {
        "schema_version": 1,
        "runtime_layout_version": 4,
        "current": {"application_version": version, "core_version": core_version},
        "previous": (
            {"application_version": backup_business_version, "core_version": core_version}
            if backup_business_version else None
        ),
        "pending": False,
        "verified_cores": {
            core_version: {
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


def verify_package(
    package_dir: Path,
    *,
    version: str,
    backup_business_version: str,
    core_version: str = DEFAULT_CORE_VERSION,
) -> bool:
    """Verify every signed runtime layer before a full baseline is distributed."""
    try:
        core = package_dir / "core" / core_version
        for path in (
            package_dir / f"{LAUNCHER_NAME}.exe",
            package_dir / "current.json",
            core / "_internal" / "web_client" / "desktop.py",
        ):
            if not path.exists():
                raise RuntimeError(f"missing package file: {path.relative_to(package_dir)}")

        verified_core = verify_core_directory(
            core,
            PUBLIC_KEY,
            expected_version=core_version,
            hash_mode="full",
        )
        current_business = verify_business_directory(
            package_dir / "versions" / version / "business",
            PUBLIC_KEY,
            expected_version=version,
            expected_core_version=core_version,
        )
        verify_business_directory(
            package_dir / "versions" / backup_business_version / "business",
            PUBLIC_KEY,
            expected_version=backup_business_version,
            expected_core_version=core_version,
        )

        app_version = json.loads(
            (current_business.root / "app" / "version.json").read_text(encoding="utf-8")
        ).get("version")
        if app_version != version:
            raise RuntimeError(
                "current business bundle app/version.json differs from --version: "
                f"{app_version!r} != {version!r}"
            )

        state_path = package_dir / "current.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        expected_current = {"application_version": version, "core_version": core_version}
        expected_previous = {
            "application_version": backup_business_version,
            "core_version": core_version,
        }
        if state.get("current") != expected_current or state.get("previous") != expected_previous:
            raise RuntimeError("current.json selection pair does not match verified bundles")

        # Keep the release receipt coherent with the full verification just run.
        state["verified_cores"] = {
            core_version: {
                "verification_mode": "full",
                "manifest_sha256": verified_core.manifest_sha256,
                "metadata_sha256": verified_core.metadata_sha256,
                "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        }
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[ERROR] package verification failed: {exc}")
        return False
    print("[OK] package signatures, versions, rollback target, and Core files verified")
    return True


def zip_package(
    package_dir: Path,
    version: str,
    core_version: str = DEFAULT_CORE_VERSION,
) -> tuple[Path, str]:
    """Create the final distributable ZIP."""
    zip_path = Path.home() / "Desktop" / f"LiveClipperWeb_v{core_version}_{version}_全量包.zip"
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
    with zipfile.ZipFile(str(zip_path), "r") as archive:
        corrupt = archive.testzip()
    if corrupt:
        raise RuntimeError(f"Final package ZIP is corrupt: {corrupt}")
    (zip_path.parent / f"{zip_path.name}.sha256.txt").write_text(
        f"{sha}  {zip_path.name}\n", encoding="utf-8"
    )
    size_mb = zip_path.stat().st_size / (1024 ** 2)
    print(f"[zip] {count} files, {size_mb:.0f}MB, sha256={sha[:16]}")
    return zip_path, sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Business version, e.g. 2026.8.5.2")
    parser.add_argument(
        "--core-version",
        default=DEFAULT_CORE_VERSION,
        help=f"Core semantic version (default: {DEFAULT_CORE_VERSION})",
    )
    parser.add_argument("--backup-version", help="Backup business version for rollback, e.g. 2026.8.5.1")
    parser.add_argument("--backup-archive", type=Path, help="Path to backup business ZIP")
    args = parser.parse_args()

    if not args.backup_version or args.backup_archive is None:
        parser.error("a full V4 baseline requires --backup-version and --backup-archive")
    if args.backup_version == args.version:
        parser.error("--backup-version must differ from --version")
    if not args.backup_archive.is_file():
        parser.error(f"--backup-archive does not exist: {args.backup_archive}")
    try:
        validate_embedded_core_version(args.core_version)
    except RuntimeError as exc:
        parser.error(str(exc))

    if not PRIVATE_KEY.exists():
        print(f"[ERROR] private key not found: {PRIVATE_KEY}")
        return 1

    DIST_ROOT.mkdir(parents=True, exist_ok=True)

    # A full-baseline build always rebuilds both frozen layers. Business-only
    # releases use build_business_bundle.py and never enter this script.
    print("--- Step 1: Build Core ---")
    core_dir = build_core()
    print("--- Step 1a: Sign Core ---")
    sign_result = sign_core(core_dir, args.core_version)
    print(f"      manifest={sign_result['manifest_sha256'][:16]} files={sign_result['file_count']}")

    print("--- Step 2: Build Launcher ---")
    launcher_exe = build_launcher()

    # Step 3: Business
    print("--- Step 3: Build Business Bundle ---")
    business_archive = build_business(args.version)

    # Step 4: Assemble
    print("--- Step 4: Assemble Package ---")
    package_dir = assemble_package(
        core_dir, launcher_exe, business_archive, args.version,
        backup_business_version=args.backup_version,
        backup_business_archive=args.backup_archive,
        core_version=args.core_version,
    )

    # Step 5: Verify
    print("--- Step 5: Verify ---")
    if not verify_package(
        package_dir,
        version=args.version,
        backup_business_version=args.backup_version,
        core_version=args.core_version,
    ):
        return 1

    # Step 6: Zip
    print("--- Step 6: Zip ---")
    zip_path, sha = zip_package(package_dir, args.version, args.core_version)

    print("\nDone. Package:", zip_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
