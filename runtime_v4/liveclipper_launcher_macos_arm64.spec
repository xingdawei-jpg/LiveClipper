# -*- mode: python ; coding: utf-8 -*-
"""Apple-Silicon V4 stable launcher app, separate from the Windows launcher."""

import os
import platform


block_cipher = None

if platform.system() != "Darwin" or platform.machine() != "arm64":
    raise RuntimeError("LiveClipper macOS V4 launcher must be built natively on Apple Silicon")

V4_DIR = SPECPATH
ROOT_DIR = os.path.dirname(V4_DIR)
APP_DIR = os.path.join(ROOT_DIR, "app")
PUBLIC_KEY = os.path.join(APP_DIR, "release_update_public_key.pem")

if not os.path.isfile(PUBLIC_KEY):
    raise RuntimeError(f"macOS V4 launcher requires release verification key: {PUBLIC_KEY}")

a = Analysis(
    [os.path.join(V4_DIR, "launcher.py")],
    pathex=[ROOT_DIR],
    datas=[(PUBLIC_KEY, "core_keys")],
    hiddenimports=[
        "runtime_v4.business_bundle",
        "runtime_v4.core_manifest",
        "cryptography",
        "cryptography.hazmat.primitives.asymmetric.ed25519",
    ],
    excludes=["unittest", "doctest"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiveClipper",
    console=False,
    target_arch="arm64",
    codesign_identity=os.environ.get("LIVECLIPPER_MACOS_CODESIGN_IDENTITY") or None,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="LiveClipper")
app = BUNDLE(
    coll,
    name="LiveClipper.app",
    bundle_identifier="com.liveclipper.v4.macos-arm64",
)
