"""Build the one-time V2 to V3 bridge executable with an embedded signed patch."""

from __future__ import annotations

import os
import argparse
import subprocess
import sys
from pathlib import Path

APP_IMPORT_DIR = Path(__file__).resolve().parents[1] / "app"
if APP_IMPORT_DIR.is_dir() and str(APP_IMPORT_DIR) not in sys.path:
    sys.path.insert(0, str(APP_IMPORT_DIR))


from release_signing import sha256_file


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
AGENT_SCRIPT = ROOT / "tools" / "liveclipper_update_agent.py"
PUBLIC_KEY = APP_DIR / "release_update_public_key.pem"
ICON = ROOT / "assets" / "liveclipper.ico"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an embedded LiveClipper bridge updater.")
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--from-version", required=True)
    parser.add_argument("--to-version", required=True)
    parser.add_argument("--distpath", type=Path, required=True)
    parser.add_argument("--workpath", type=Path, required=True)
    args = parser.parse_args()

    patch = args.patch.resolve()
    if not patch.is_file():
        parser.error(f"patch not found: {patch}")
    name = f"LiveClipperBridge_{args.from_version}_to_{args.to_version}"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        name,
        "--paths",
        str(APP_DIR),
        "--add-data",
        f"{PUBLIC_KEY}{os.pathsep}app",
        "--add-data",
        f"{patch}{os.pathsep}.",
        "--distpath",
        str(args.distpath.resolve()),
        "--workpath",
        str(args.workpath.resolve()),
        "--specpath",
        str(args.workpath.resolve()),
    ]
    if ICON.is_file():
        command.extend(["--icon", str(ICON)])
    command.append(str(AGENT_SCRIPT))
    subprocess.run(command, cwd=ROOT, check=True)

    executable = args.distpath.resolve() / f"{name}.exe"
    digest = sha256_file(executable).upper()
    executable.with_suffix(executable.suffix + ".sha256.txt").write_text(
        f"{digest}  {executable.name}\n",
        encoding="ascii",
    )
    print(f"bridge={executable} size={executable.stat().st_size} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
