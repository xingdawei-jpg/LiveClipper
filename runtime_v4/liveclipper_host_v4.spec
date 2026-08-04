# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller prototype for the stable Runtime V4 desktop host."""

import importlib.util
import os


block_cipher = None

V4_DIR = SPECPATH
ROOT_DIR = os.path.dirname(V4_DIR)
APP_DIR = os.path.join(ROOT_DIR, "app")
WEB_DIR = os.path.join(ROOT_DIR, "web_client")
FFMPEG_DIR = os.path.abspath(
    os.environ.get("LIVECLIPPER_FFMPEG_DIR", "").strip()
    or os.path.join(APP_DIR, "ffmpeg")
)
WEBVIEW2_RUNTIME_DIR = os.path.abspath(
    os.environ.get("LIVECLIPPER_WEBVIEW2_RUNTIME_DIR", "").strip()
    or os.path.join(ROOT_DIR, "vendor", "webview2_runtime_x64")
)
NATIVE_FILE_DROP_BRIDGE = os.path.join(WEB_DIR, "native_file_drop_bridge.dll")
ICON_FILE = os.path.join(ROOT_DIR, "assets", "liveclipper.ico")
RELEASE_PUBLIC_KEY = os.path.abspath(
    os.environ.get("LIVECLIPPER_V4_RELEASE_PUBLIC_KEY", "").strip()
    or os.path.join(APP_DIR, "release_update_public_key.pem")
)
V4_UPDATE_SOURCES = os.path.join(ROOT_DIR, "release", "runtime_v4_update_sources.json")
HOST_CONSOLE = os.environ.get("LIVECLIPPER_V4_HOST_CONSOLE", "").strip() == "1"


def _required_file(path, label):
    if not os.path.isfile(path):
        raise RuntimeError(f"Runtime V4 host requires {label}: {path}")
    return path


def _required_dir(path, marker, label):
    if not os.path.isfile(os.path.join(path, marker)):
        raise RuntimeError(f"Runtime V4 host requires {label}: {path}")
    return path


def _module_file(module_name, *parts):
    spec = importlib.util.find_spec(module_name)
    if not spec or not spec.origin:
        return None
    return os.path.join(os.path.dirname(spec.origin), *parts)


def _existing(items):
    return [(source, destination) for source, destination in items if source and os.path.exists(source)]


SENSEVOICE_RUNTIME_MODULES = (
    "funasr.register",
    "funasr.auto.auto_model",
    "funasr.tokenizer.sentencepiece_tokenizer",
    "funasr.tokenizer.char_tokenizer",
    "funasr.frontends.wav_frontend",
    "funasr.models.ctc.ctc",
    "funasr.models.paraformer.search",
    "funasr.models.sense_voice.model",
    "funasr.models.fsmn_vad_streaming.encoder",
    "funasr.models.fsmn_vad_streaming.model",
    "funasr.models.sanm.encoder",
    "funasr.models.ct_transformer.model",
    "funasr.models.specaug.specaug",
)

cv2_data_dir = _module_file("cv2", "data")
certifi_pem = _module_file("certifi", "cacert.pem")
funasr_version = _module_file("funasr", "version.txt")

datas = [
    (_required_dir(WEBVIEW2_RUNTIME_DIR, "msedgewebview2.exe", "fixed WebView2 runtime"), "webview2_runtime"),
    (_required_file(NATIVE_FILE_DROP_BRIDGE, "native file-drop bridge"), "web_client"),
    (_required_file(os.path.join(APP_DIR, "license_public_key.txt"), "license public key"), "core_keys"),
    (_required_file(RELEASE_PUBLIC_KEY, "release update public key"), "core_keys"),
    (_required_file(V4_UPDATE_SOURCES, "Runtime V4 update source config"), "core_config"),
]
datas += _existing([
    (ICON_FILE, "assets"),
    (r"C:\Windows\Fonts\msyhbd.ttc", "fonts"),
    (r"C:\Windows\Fonts\msyh.ttc", "fonts"),
    (os.path.join(cv2_data_dir, "haarcascade_frontalface_default.xml") if cv2_data_dir else "", "."),
    (os.path.join(cv2_data_dir, "haarcascade_upperbody.xml") if cv2_data_dir else "", "."),
    (os.path.join(cv2_data_dir, "haarcascade_fullbody.xml") if cv2_data_dir else "", "."),
    (certifi_pem if certifi_pem else "", "certifi"),
    (funasr_version if funasr_version else "", "funasr"),
])

binaries = [
    (_required_file(os.path.join(FFMPEG_DIR, "ffmpeg.exe"), "ffmpeg"), "ffmpeg"),
    (_required_file(os.path.join(FFMPEG_DIR, "ffprobe.exe"), "ffprobe"), "ffmpeg"),
]

a = Analysis(
    [os.path.join(V4_DIR, "desktop_host.py")],
    pathex=[ROOT_DIR, APP_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "release_signing",
        "runtime_v4.core_manifest",
        "runtime_v4.launcher",
        "runtime_v4.update_agent",
        "runtime_v4.update_channel",
        "runtime_v4.update_service",
        "fastapi",
        "fastapi.responses",
        "fastapi.staticfiles",
        "uvicorn",
        "starlette",
        "starlette.responses",
        "starlette.staticfiles",
        "pydantic",
        "webview",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "anyio",
        "httpx",
        "httpcore",
        "h11",
        "sniffio",
        "requests",
        "urllib3",
        "charset_normalizer",
        "idna",
        "certifi",
        "cryptography",
        "cv2",
        "numpy",
        "PIL",
        "PIL.Image",
        # Business modules are loaded from the signed bundle at runtime, so
        # PyInstaller cannot discover their Tk dialog imports statically.
        "tkinter",
        "tkinter.colorchooser",
        "tkinter.commondialog",
        "tkinter.filedialog",
        "tkinter.font",
        "tkinter.messagebox",
        "tkinter.simpledialog",
        "tkinter.ttk",
        "openpyxl",
        "tos",
        "fsspec",
        "torch",
        "torchaudio",
        "tokenizers",
        *SENSEVOICE_RUNTIME_MODULES,
    ],
    excludes=[
        "test",
        "doctest",
        "ensurepip",
        "venv",
        "turtledemo",
        "idlelib",
        "tkinter.test",
        "matplotlib",
        "pandas",
        "tensorflow",
        "keras",
        "jupyter",
        "IPython",
        "boto3",
        "botocore",
        "sphinx",
        "docutils",
        "flask",
        "django",
        "graphviz",
        "notebook",
        "transformers.models.whisper",
        # These modules must come only from the verified business directory.
        "server",
        "updater",
        "ai_clipper",
        "cutter_logic",
        "stt",
        "volcengine_asr",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiveClipperHost",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=HOST_CONSOLE,
    disable_windowed_traceback=False,
    icon=ICON_FILE if os.path.exists(ICON_FILE) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="LiveClipperHost",
)
