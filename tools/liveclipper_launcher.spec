# -*- mode: python ; coding: utf-8 -*-
import os


TOOLS_DIR = SPECPATH
ROOT = os.path.dirname(TOOLS_DIR)
APP_DIR = os.path.join(ROOT, "app")
SCRIPT = os.path.join(TOOLS_DIR, "liveclipper_launcher.py")
PUBLIC_KEY = os.path.join(APP_DIR, "release_update_public_key.pem")
ICON = os.path.join(ROOT, "assets", "liveclipper.ico")

if not os.path.isfile(PUBLIC_KEY):
    raise SystemExit(f"release update public key is missing: {PUBLIC_KEY}")

a = Analysis(
    [SCRIPT],
    pathex=[TOOLS_DIR, APP_DIR],
    binaries=[],
    datas=[(PUBLIC_KEY, "app")],
    hiddenimports=["release_signing", "tkinter", "tkinter.messagebox"],
    excludes=[
        "numpy",
        "cv2",
        "PIL",
        "fastapi",
        "uvicorn",
        "webview",
        "openpyxl",
        "faster_whisper",
        "ctranslate2",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LiveClipperLauncher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON if os.path.exists(ICON) else None,
)
