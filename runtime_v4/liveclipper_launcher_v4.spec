# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller prototype for the stable Runtime V4 launcher."""

import os


block_cipher = None

V4_DIR = SPECPATH
ROOT_DIR = os.path.dirname(V4_DIR)
APP_DIR = os.path.join(ROOT_DIR, "app")
ICON_FILE = os.path.join(ROOT_DIR, "assets", "liveclipper.ico")
RELEASE_PUBLIC_KEY = os.path.abspath(
    os.environ.get("LIVECLIPPER_V4_RELEASE_PUBLIC_KEY", "").strip()
    or os.path.join(APP_DIR, "release_update_public_key.pem")
)
LAUNCHER_CONSOLE = os.environ.get("LIVECLIPPER_V4_LAUNCHER_CONSOLE", "").strip() == "1"
LAUNCHER_NAME = os.environ.get("LIVECLIPPER_V4_LAUNCHER_NAME", "").strip() or "LiveClipperWeb"
if LAUNCHER_NAME not in {"LiveClipperWeb", "LiveClipperLauncherV4"}:
    raise RuntimeError(f"unsupported Runtime V4 launcher name: {LAUNCHER_NAME}")


def _required_file(path, label):
    if not os.path.isfile(path):
        raise RuntimeError(f"Runtime V4 launcher requires {label}: {path}")
    return path


a = Analysis(
    [os.path.join(V4_DIR, "launcher.py")],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=[
        (_required_file(RELEASE_PUBLIC_KEY, "release update public key"), "core_keys"),
    ],
    hiddenimports=[
        "runtime_v4.business_bundle",
        "runtime_v4.core_manifest",
        "cryptography",
        "cryptography.hazmat.primitives.asymmetric.ed25519",
    ],
    excludes=[
        "tkinter.test",
        "unittest",
        "doctest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name=LAUNCHER_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=LAUNCHER_CONSOLE,
    disable_windowed_traceback=False,
    icon=ICON_FILE if os.path.exists(ICON_FILE) else None,
)
