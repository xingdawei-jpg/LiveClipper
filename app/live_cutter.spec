# -*- mode: python ; coding: utf-8 -*-
"""
直播带货切片工具 - PyInstaller 打包配置
入口：launcher.py（最小启动器）
"""
import os
import importlib.util

block_cipher = None


def _existing(items):
    return [(src, dest) for src, dest in items if os.path.exists(src)]


def _module_file(module_name, *parts):
    spec = importlib.util.find_spec(module_name)
    if not spec or not spec.origin:
        return None
    return os.path.join(os.path.dirname(spec.origin), *parts)


ffmpeg_dir = os.path.join(SPECPATH, 'ffmpeg')
cv2_data_dir = _module_file('cv2', 'data')
fw_assets_dir = _module_file('faster_whisper', 'assets')

a = Analysis(
    [os.path.join(SPECPATH, 'launcher.py')],
    pathex=[SPECPATH],
    binaries=_existing([
        (os.path.join(ffmpeg_dir, 'ffmpeg.exe'), os.path.join('ffmpeg')),
        (os.path.join(ffmpeg_dir, 'ffprobe.exe'), os.path.join('ffmpeg')),
    ]),
    datas=_existing([
        # app/ 目录下的业务代码（launcher.py 会定位到此目录）
        ('config.py', 'app'),
        ('cutter_logic.py', 'app'),
        ('srt_parser.py', 'app'),
        ('stt.py', 'app'),
        ('ai_clipper.py', 'app'),
        ('license_client.py', 'app'),
        ('updater.py', 'app'),
        ('version.json', 'app'),
        ('keywords.json', 'app'),
        ('asr_api.py', 'app'),
        ('cutter.py', 'app'),
        ('schedule_splitter.py', 'app'),
        ('schedule_page.py', 'app'),
        ('volcengine_asr.py', 'app'),
        ('aliyun_asr.py', 'app'),
        ('aliyun_asr_v2.py', 'app'),
        ('multi_version.py', 'app'),
        ('mix_page.py', 'app'),
        ('dedup_page.py', 'app'),
        ('platform_config.py', 'app'),
        ('run_pipeline.py', 'app'),
        ('smart_crop.py', 'app'),
        ('srt_splitter.py', 'app'),
        ('tighten.py', 'app'),
        ('trim_long.py', 'app'),
        # 主程序（launcher.py 动态导入）
        ('gui.py', 'app'),
        # 新增：单品扫描
        ('product_scanner.py', 'app'),
        ('product_scan_window.py', 'app'),
        ('live_recorder_page.py', 'app'),
        ('douyin_stream.py', 'app'),
        ('product_scan_page.py', 'app'),
        # 字体文件
        (r'C:\Windows\Fonts\msyhbd.ttc', 'fonts'),
        (r'C:\Windows\Fonts\msyh.ttc', 'fonts'),
        # Haar Cascade（Smart Crop）
        (os.path.join(cv2_data_dir, 'haarcascade_frontalface_default.xml') if cv2_data_dir else '', '.'),
        (os.path.join(cv2_data_dir, 'haarcascade_upperbody.xml') if cv2_data_dir else '', '.'),
        (os.path.join(cv2_data_dir, 'haarcascade_fullbody.xml') if cv2_data_dir else '', '.'),
        # Silero VAD
        (os.path.join(fw_assets_dir, 'silero_vad_v6.onnx') if fw_assets_dir else '', 'faster_whisper/assets'),
    ]),
    hiddenimports=[
        'gui', 'dedup_page', 'mix_page', 'product_scanner', 'product_scan_window',
        'product_scan_page', 'live_recorder_page', 'schedule_page', 'douyin_stream',
        'socketserver', 'mimetypes', 'calendar', 'fnmatch', 'nturl2path',
        'urllib', 'urllib.parse', 'urllib.error', 'urllib.request',
        'base64', 'hmac', 'hashlib', 'time', 'random', 'json',
        'email', 'smtplib', 'profile', 'pstats',
        'xml',
        'email.mime', 'email.mime.multipart', 'email.mime.text',
        'email.mime.application', 'cgi', 'html',
        'PIL._tkinter_finder',
        'cv2', 'numpy', 'ctranslate2', 'tokenizers', 'faster_whisper',
        'av', 'tos', 'fsspec', 'packaging', 'anyio', 'httpx', 'httpcore', 'schedule_splitter', 'openpyxl',
        'h11', 'sniffio', 'certifi', 'urllib3', 'charset_normalizer',
        'idna', 'cryptography', 'pytz', 'tqdm', 'rich', 'pygments',
        'click', 'requests', 'yaml',
    ],
    excludes=[
        'test', 'unittest', 'pdb', 'doctest',
        'ensurepip', 'venv',
        'turtledemo', 'idlelib',
        'tkinter.test',
        'PIL.ImageShow', 'PIL.ImageQt',
        'matplotlib', 'scipy', 'pandas',
        'tensorflow', 'torch', 'keras',
        'jupyter', 'IPython',
        'boto3', 'botocore',
        'sphinx', 'docutils',
        'flask', 'django',
        'graphviz', 'notebook',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='直播切片工具',
    debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, upx_exclude=[],
    runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name='直播切片工具',
)
