"""
平台适配层 - 统一管理 Windows/Mac 差异
所有平台相关的配置都从这里读取，其他文件不要硬编码平台差异。
"""
import os
import sys
import platform

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# ============================================================
# 字体配置
# ============================================================
if IS_MAC:
    FONT_NAME = "PingFang SC"
    FONT_BOLD_NAME = "PingFang SC"
    # Mac 字体路径（打包后在 _internal/fonts/，开发时用系统路径）
    if getattr(sys, "frozen", False):
        FONT_DIR = os.path.join(os.path.dirname(sys.executable), "_internal", "fonts")
    else:
        FONT_DIR = "/System/Library/Fonts"
    FONT_PATH = os.path.join(FONT_DIR, "PingFang.ttc")
    FONT_BOLD_PATH = FONT_PATH  # PingFang SC 不区分粗体文件
    # FFmpeg drawtext 用的路径（Mac 路径在滤镜里不需要转义冒号）
    DRAWTEXT_FONT_PATH = FONT_PATH
else:
    FONT_NAME = "Microsoft YaHei"
    FONT_BOLD_NAME = "Microsoft YaHei Bold"
    if getattr(sys, "frozen", False):
        FONT_DIR = os.path.join(os.path.dirname(sys.executable), "_internal", "fonts")
    else:
        FONT_DIR = r"C:\Windows\Fonts"
    FONT_PATH = os.path.join(FONT_DIR, "msyh.ttc")
    FONT_BOLD_PATH = os.path.join(FONT_DIR, "msyhbd.ttc")
    # Windows 下 drawtext 需要转义冒号
    DRAWTEXT_FONT_PATH = FONT_BOLD_PATH.replace("\\", "/").replace(":", "\\:")

# ============================================================
# FFmpeg 配置
# ============================================================
if getattr(sys, "frozen", False):
    if IS_MAC:
        FFMPEG_DIR = os.path.join(os.path.dirname(sys.executable), "_internal", "ffmpeg")
    else:
        FFMPEG_DIR = os.path.join(os.path.dirname(sys.executable), "_internal", "ffmpeg")
else:
    if IS_MAC:
        FFMPEG_DIR = "/usr/local/bin"
    else:
        FFMPEG_DIR = r"C:\ffmpeg\bin"

FFMPEG_CMD = os.path.join(FFMPEG_DIR, "ffmpeg" + (".exe" if IS_WIN else ""))
FFPROBE_CMD = os.path.join(FFMPEG_DIR, "ffprobe" + (".exe" if IS_WIN else ""))

# ============================================================
# 应用数据目录（缓存、许可证等）
# ============================================================
if IS_MAC:
    APP_DATA_DIR = os.path.expanduser("~/Library/Application Support/LiveClipper")
else:
    APP_DATA_DIR = os.environ.get("APPDATA", os.path.expanduser("~"))

LICENSE_CACHE_DIR = os.path.join(APP_DATA_DIR, "LiveClipper")
LICENSE_CACHE_FILE = os.path.join(LICENSE_CACHE_DIR, "license_cache.json")

# ============================================================
# Whisper 加速配置
# ============================================================
if IS_MAC and platform.machine() == "arm64":
    # Apple Silicon: 使用 Metal GPU 加速
    WHISPER_DEVICE = "mps"
    WHISPER_COMPUTE = "float32"  # Metal 支持 float32
else:
    # Windows/Intel Mac: CPU int8
    WHISPER_DEVICE = "cpu"
    WHISPER_COMPUTE = "int8"
