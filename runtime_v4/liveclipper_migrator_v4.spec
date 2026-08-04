# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for the external Runtime V4 migrator."""

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
MIGRATOR_CONSOLE = os.environ.get("LIVECLIPPER_V4_MIGRATOR_CONSOLE", "").strip() == "1"


def _required_file(path, label):
    if not os.path.isfile(path):
        raise RuntimeError(f"Runtime V4 migrator requires {label}: {path}")
    return path


a = Analysis(
    [os.path.join(V4_DIR, "migrator.py")],
    pathex=[ROOT_DIR, APP_DIR],
    binaries=[],
    datas=[
        (_required_file(RELEASE_PUBLIC_KEY, "release update public key"), "core_keys"),
    ],
    hiddenimports=[
        "release_signing",
        "runtime_v4.business_bundle",
        "runtime_v4.core_manifest",
        "runtime_v4.migration",
        "runtime_v4.migration_package",
        "runtime_v4.migration_transaction",
        "cryptography",
        "cryptography.hazmat.primitives.asymmetric.ed25519",
    ],
    excludes=[
        "torch",
        "torchaudio",
        "funasr",
        "cv2",
        "numpy",
        "PIL",
        "fastapi",
        "uvicorn",
        "webview",
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
    name="LiveClipperMigratorV4",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=MIGRATOR_CONSOLE,
    disable_windowed_traceback=False,
    icon=ICON_FILE if os.path.exists(ICON_FILE) else None,
)
