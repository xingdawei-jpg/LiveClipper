# -*- mode: python ; coding: utf-8 -*-
"""
LiveClipper Web desktop client - PyInstaller onedir build config.

Entry: web_client/desktop.py
Output: release_dist/LiveClipperWeb
"""
import glob
import importlib.util
import os

block_cipher = None

WEB_DIR = SPECPATH
ROOT_DIR = os.path.dirname(WEB_DIR)
APP_DIR = os.path.join(ROOT_DIR, "app")
TOOLS_DIR = os.path.join(ROOT_DIR, "tools")
WEB_TOOLS_DIR = os.path.join(WEB_DIR, "tools")
FRONTEND_DIR = os.path.join(WEB_DIR, "frontend")
NATIVE_FILE_DROP_BRIDGE = os.path.join(WEB_DIR, "native_file_drop_bridge.dll")
DEFAULT_FFMPEG_DIR = os.path.join(APP_DIR, "ffmpeg")
FFMPEG_DIR = os.path.abspath(
    os.environ.get("LIVECLIPPER_FFMPEG_DIR", "").strip()
    or DEFAULT_FFMPEG_DIR
)
FFMPEG_REQUIRED_FILES = (
    "ffmpeg.exe",
    "ffprobe.exe",
)
ICON_FILE = os.path.join(ROOT_DIR, "assets", "liveclipper.ico")
WEBVIEW2_RUNTIME_NAME = (
    "Microsoft.WebView2.FixedVersionRuntime.149.0.4022.98.x64"
)
DEFAULT_WEBVIEW2_RUNTIME_DIR = os.path.join(
    ROOT_DIR,
    "vendor",
    "webview2_runtime_x64",
    WEBVIEW2_RUNTIME_NAME,
)
WEBVIEW2_RUNTIME_DIR = os.path.abspath(
    os.environ.get("LIVECLIPPER_WEBVIEW2_RUNTIME_DIR", "").strip()
    or DEFAULT_WEBVIEW2_RUNTIME_DIR
)
WEBVIEW2_REQUIRED_FILES = (
    "msedgewebview2.exe",
    "icudtl.dat",
    "msedge_elf.dll",
)


def _require_fixed_webview2_runtime():
    if os.path.basename(WEBVIEW2_RUNTIME_DIR) != WEBVIEW2_RUNTIME_NAME:
        raise RuntimeError(
            "LIVECLIPPER_WEBVIEW2_RUNTIME_DIR must point to the pinned "
            f"{WEBVIEW2_RUNTIME_NAME} directory: {WEBVIEW2_RUNTIME_DIR}"
        )
    missing = [
        name
        for name in WEBVIEW2_REQUIRED_FILES
        if not os.path.isfile(os.path.join(WEBVIEW2_RUNTIME_DIR, name))
    ]
    if missing:
        raise RuntimeError(
            "Fixed WebView2 Runtime is required for a desktop package; "
            f"missing {', '.join(missing)} in {WEBVIEW2_RUNTIME_DIR}"
        )
    return WEBVIEW2_RUNTIME_DIR


def _require_ffmpeg_binaries():
    missing = [
        name
        for name in FFMPEG_REQUIRED_FILES
        if not os.path.isfile(os.path.join(FFMPEG_DIR, name))
    ]
    if missing:
        raise RuntimeError(
            "Bundled FFmpeg tools are required for a desktop package; "
            f"missing {', '.join(missing)} in {FFMPEG_DIR}"
        )
    return [
        (os.path.join(FFMPEG_DIR, name), "ffmpeg")
        for name in FFMPEG_REQUIRED_FILES
    ]


def _existing(items):
    return [(src, dest) for src, dest in items if src and os.path.exists(src)]


def _module_file(module_name, *parts):
    spec = importlib.util.find_spec(module_name)
    if not spec or not spec.origin:
        return None
    return os.path.join(os.path.dirname(spec.origin), *parts)


def _require_modules(*module_names):
    missing = [name for name in module_names if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError(
            "SenseVoice desktop build requires " + ", ".join(missing)
            + "; install requirements.txt before running PyInstaller"
        )


_require_modules("funasr", "modelscope", "torch", "torchaudio")


# FunASR relies on runtime package scanning to populate these registries. That
# scan cannot discover modules that PyInstaller leaves outside the archive, so
# include the exact SenseVoice, VAD, and punctuation components explicitly.
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


def _app_datas():
    datas = []
    skip_suffixes = (
        ".bak",
        ".bak_corrupted",
        ".bak_task7_done",
        ".bak_pre_task7_redo",
        ".bak_cat_merge",
    )
    skip_names = {
        "ai_settings.json",
        ".installed_version",
        "_package_final.py",
        "_toggle_monitor.py",
        "clip_tuple_check.py",
        "generated_codes.json",
        "gui_clean.py",
        "gui_fresh.py",
        "gui_tmp.py",
        "licenses.json",
        "live_recorder_page_BACKUP.py",
        "license_generator.py",
        "license_server.py",
        "license_feishu_backend.py",
        "license_stats_store.py",
        "feishu_scheduler.py",
        "verify.py",
    }

    for path in glob.glob(os.path.join(APP_DIR, "*")):
        name = os.path.basename(path)
        if os.path.isdir(path):
            continue
        if name.startswith("_"):
            continue
        if name in skip_names:
            continue
        if name.endswith(skip_suffixes):
            continue
        if name.endswith((".json", ".pem")) or name == "license_public_key.txt":
            datas.append((path, "app"))
    return datas


def _tool_datas():
    names = [
        "local_asr_worker.py",
        "douyin_active_product_probe_poc.py",
        "douyin_chrome_live_poc.py",
    ]
    datas = []
    for name in names:
        for base_dir in (WEB_TOOLS_DIR, TOOLS_DIR):
            src = os.path.join(base_dir, name)
            if os.path.exists(src):
                datas.append((src, "tools"))
                break
    return datas


def _require_native_file_drop_bridge():
    if not os.path.isfile(NATIVE_FILE_DROP_BRIDGE):
        raise RuntimeError(
            "native_file_drop_bridge.dll is required for zero-copy Explorer drag-and-drop; "
            f"build web_client/native_file_drop_bridge.cs before packaging: {NATIVE_FILE_DROP_BRIDGE}"
        )
    return [(NATIVE_FILE_DROP_BRIDGE, "web_client")]


cv2_data_dir = _module_file("cv2", "data")
certifi_pem = _module_file("certifi", "cacert.pem")
funasr_version = _module_file("funasr", "version.txt")

datas = []
datas += [(FRONTEND_DIR, os.path.join("web_client", "frontend"))]
datas += _require_native_file_drop_bridge()
datas += _existing([(ICON_FILE, "assets")])
datas += [(_require_fixed_webview2_runtime(), "webview2_runtime")]
datas += _app_datas()
datas += _tool_datas()
datas += _existing([
    (r"C:\Windows\Fonts\msyhbd.ttc", "fonts"),
    (r"C:\Windows\Fonts\msyh.ttc", "fonts"),
    (os.path.join(cv2_data_dir, "haarcascade_frontalface_default.xml") if cv2_data_dir else "", "."),
    (os.path.join(cv2_data_dir, "haarcascade_upperbody.xml") if cv2_data_dir else "", "."),
    (os.path.join(cv2_data_dir, "haarcascade_fullbody.xml") if cv2_data_dir else "", "."),
    (certifi_pem if certifi_pem else "", "certifi"),
    (funasr_version if funasr_version else "", "funasr"),
])

binaries = _require_ffmpeg_binaries()

a = Analysis(
    [os.path.join(WEB_DIR, "desktop.py")],
    pathex=[WEB_DIR, APP_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "server",
        "updater",
        "release_signing",
        "fastapi",
        "uvicorn",
        "starlette",
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
        "cv2",
        "numpy",
        "PIL",
        "PIL.Image",
        "openpyxl",
        "tos",
        "fsspec",
        "local_asr", "local_asr_quality",
        "torch", "torchaudio",
        "tokenizers",
        *SENSEVOICE_RUNTIME_MODULES,
        "ai_clipper",
        "category_profiles",
        "selection_contracts",
        "aliyun_asr",
        "aliyun_asr_v2",
        "asr_api",
        "config",
        "cutter_logic",
        "dedup_page",
        "douyin_stream",
        "license_client",
        "license_events",
        "license_guard",
        "license_token",
        "live_recorder_page",
        "mix_page",
        "multi_version",
        "platform_config",
        "product_scan_page",
        "product_scanner",
        "schedule_splitter",
        "smart_crop",
        "srt_parser",
        "stt",
        "volcengine_asr",
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
        # Local ASR is SenseVoice-only. Keep Transformers' unused Whisper model
        # family out of the frozen runtime instead of shipping dormant code.
        "transformers.models.whisper",
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
    name="LiveClipperWeb",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_FILE if os.path.exists(ICON_FILE) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LiveClipperWeb",
)
