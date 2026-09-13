# -*- mode: python ; coding: utf-8 -*-
"""Apple-Silicon Runtime V4 Core Host.  This is never a Windows fallback."""

import os
import platform


block_cipher = None

if platform.system() != "Darwin" or platform.machine() != "arm64":
    raise RuntimeError("LiveClipper macOS V4 Core must be built natively on Apple Silicon")

V4_DIR = SPECPATH
ROOT_DIR = os.path.dirname(V4_DIR)
APP_DIR = os.path.join(ROOT_DIR, "app")
WEB_DIR = os.path.join(ROOT_DIR, "web_client")
FFMPEG_DIR = os.path.abspath(
    os.environ.get("LIVECLIPPER_MACOS_FFMPEG_DIR", "").strip()
    or "/opt/homebrew/opt/ffmpeg-full/bin"
)
PUBLIC_KEY = os.path.join(APP_DIR, "release_update_public_key.pem")
LICENSE_KEY = os.path.join(APP_DIR, "license_public_key.txt")
UPDATE_SOURCES = os.path.join(ROOT_DIR, "release", "runtime_v4_macos_arm64_update_sources.json")


def _required(path, label):
    if not os.path.isfile(path):
        raise RuntimeError(f"macOS V4 Core requires {label}: {path}")
    return path


a = Analysis(
    [os.path.join(V4_DIR, "desktop_host.py")],
    pathex=[ROOT_DIR, APP_DIR, WEB_DIR],
    binaries=[
        (_required(os.path.join(FFMPEG_DIR, "ffmpeg"), "arm64 ffmpeg"), "ffmpeg"),
        (_required(os.path.join(FFMPEG_DIR, "ffprobe"), "arm64 ffprobe"), "ffmpeg"),
    ],
    datas=[
        (_required(PUBLIC_KEY, "release verification key"), "core_keys"),
        (_required(LICENSE_KEY, "license verification key"), "core_keys"),
        (_required(UPDATE_SOURCES, "macOS update source configuration"), "core_config/runtime_v4_update_sources.json"),
        (_required(os.path.join(WEB_DIR, "__init__.py"), "web_client package initializer"), "web_client"),
        (_required(os.path.join(WEB_DIR, "desktop.py"), "web_client desktop shell"), "web_client"),
    ],
    hiddenimports=[
        "release_signing",
        "runtime_v4.core_manifest",
        "runtime_v4.launcher",
        "runtime_v4.update_agent",
        "runtime_v4.update_channel",
        "runtime_v4.update_service",
        "web_client.desktop",
        "webview",
        "webview.platforms.cocoa",
        "fastapi",
        "fastapi.responses",
        "fastapi.staticfiles",
        "uvicorn",
        "cryptography",
        "torch",
        "funasr",
        "modelscope",
        "kaldi_native_fbank",
    ],
    excludes=[
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "torchaudio",
        "test",
        "unittest",
        "doctest",
        "ensurepip",
        "venv",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiveClipperHost",
    console=False,
    target_arch="arm64",
    codesign_identity=os.environ.get("LIVECLIPPER_MACOS_CODESIGN_IDENTITY") or None,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="LiveClipperHost")
app = BUNDLE(
    coll,
    name="LiveClipperHost.app",
    bundle_identifier="com.liveclipper.host.v4.macos-arm64",
)
