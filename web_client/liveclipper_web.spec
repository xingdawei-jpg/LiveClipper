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
FFMPEG_DIR = os.path.join(APP_DIR, "ffmpeg")
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

def _existing(items):
    return [(src, dest) for src, dest in items if src and os.path.exists(src)]


def _module_file(module_name, *parts):
    spec = importlib.util.find_spec(module_name)
    if not spec or not spec.origin:
        return None
    return os.path.join(os.path.dirname(spec.origin), *parts)


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


cv2_data_dir = _module_file("cv2", "data")
fw_assets_dir = _module_file("faster_whisper", "assets")
certifi_pem = _module_file("certifi", "cacert.pem")

datas = []
datas += [(FRONTEND_DIR, os.path.join("web_client", "frontend"))]
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
    (os.path.join(fw_assets_dir, "silero_vad_v6.onnx") if fw_assets_dir else "", os.path.join("faster_whisper", "assets")),
    (certifi_pem if certifi_pem else "", "certifi"),
])

binaries = _existing([
    (os.path.join(FFMPEG_DIR, "ffmpeg.exe"), "ffmpeg"),
    (os.path.join(FFMPEG_DIR, "ffprobe.exe"), "ffmpeg"),
])

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
        "av",
        "av.descriptor",
        "faster_whisper",
        "ctranslate2",
        "tokenizers",
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
        "unittest",
        "pdb",
        "doctest",
        "ensurepip",
        "venv",
        "turtledemo",
        "idlelib",
        "tkinter.test",
        "matplotlib",
        "scipy",
        "pandas",
        "tensorflow",
        "torch",
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
