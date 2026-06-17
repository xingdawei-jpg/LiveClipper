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
    FONT_DIR = r"C:\Windows\Fonts"
    FONT_PATH = os.path.join(FONT_DIR, "msyh.ttc")
    FONT_BOLD_PATH = os.path.join(FONT_DIR, "msyhbd.ttc")
    # Windows 下 drawtext 需要转义冒号
    DRAWTEXT_FONT_PATH = FONT_BOLD_PATH.replace("\\", "/").replace(":", "\\:")

# ============================================================
# FFmpeg 配置（自动检测）
# ============================================================
def _find_ffmpeg():
    """从多个位置自动定位 FFmpeg"""
    # 1. PyInstaller 打包模式
    if getattr(sys, "frozen", False):
        d = os.path.join(os.path.dirname(sys.executable), "_internal", "ffmpeg")
        cmd = os.path.join(d, "ffmpeg" + (".exe" if IS_WIN else ""))
        if os.path.exists(cmd):
            return d, cmd

    candidates = []
    
    # Windows
    if IS_WIN:
        # 2. 源码目录下 app/ffmpeg（便携版）
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg"))
        # 3. 工作目录下 _internal/ffmpeg
        candidates.append(os.path.join(os.getcwd(), "_internal", "ffmpeg"))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_internal", "ffmpeg"))
        # 4. 常见的 FFmpeg 安装路径
        candidates.append(r"C:\ffmpeg\bin")
        candidates.append(r"C:\ProgramData\chocolatey\bin")
        # 4. PATH 中找
        for p in os.environ.get("PATH", "").split(";"):
            if "ffmpeg" in p.lower():
                candidates.append(p)
    else:
        candidates.extend(["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin"])
    
    for d in candidates:
        d = os.path.normpath(d)
        cmd = os.path.join(d, "ffmpeg" + (".exe" if IS_WIN else ""))
        if os.path.exists(cmd):
            return d, cmd
    
    return None, None

FFMPEG_DIR, FFMPEG_CMD = _find_ffmpeg()
FFPROBE_CMD = os.path.join(FFMPEG_DIR, "ffprobe" + (".exe" if IS_WIN else "")) if FFMPEG_DIR else "ffprobe"

# ============================================================
# 硬件编码检测
# ============================================================
HARDWARE_ENCODER = None
HARDWARE_ENCODER_DIAGNOSTICS = []


def _hw_diag(message):
    try:
        HARDWARE_ENCODER_DIAGNOSTICS.append(str(message))
    except Exception:
        pass


def _hw_error_summary(stderr):
    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    if not lines:
        return ""
    picked = []
    for line in lines[:2] + lines[-2:]:
        if line not in picked:
            picked.append(line)
    return " | ".join(picked)[:260]

def _hardware_encoder_enabled_from_settings():
    try:
        import json
        settings_path = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")),
            "LiveClipper",
            "ai_settings.json",
        )
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8-sig") as f:
                return bool(json.load(f).get("hardware_encoder_enabled", False))
    except Exception:
        pass
    return False


ENABLE_HARDWARE_ENCODER = (
    os.environ.get("LIVECLIPPER_ENABLE_HWENC", "").strip().lower() in ("1", "true", "yes", "on")
    or _hardware_encoder_enabled_from_settings()
)
_hw_diag("硬件加速开关: 开" if ENABLE_HARDWARE_ENCODER else "硬件加速开关: 关")
if FFMPEG_CMD and ENABLE_HARDWARE_ENCODER:
    import subprocess
    _hw_diag(f"FFmpeg: {FFMPEG_CMD}")

    def _hw_creationflags():
        return subprocess.CREATE_NO_WINDOW if IS_WIN else 0

    def _hw_encoder_args(encoder):
        if encoder == "h264_qsv":
            return ["-vf", "format=nv12", "-c:v", "h264_qsv", "-preset", "fast", "-global_quality", "22"]
        if encoder == "h264_amf":
            return ["-c:v", "h264_amf", "-quality", "speed", "-qp", "22"]
        if encoder == "h264_nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "fast", "-cq", "22", "-b:v", "0"]
        return []

    def _can_run_hw_encoder(encoder):
        """FFmpeg lists many encoders that are compiled in but unusable on this PC."""
        try:
            cmd = [
                FFMPEG_CMD, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=544x960:rate=30",
                "-frames:v", "1",
            ]
            cmd += _hw_encoder_args(encoder)
            cmd += ["-f", "null", "-"]
            ret = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                timeout=8, creationflags=_hw_creationflags()
            )
            if ret.returncode != 0:
                _hw_diag(f"{encoder}: 自检失败 rc={ret.returncode} {_hw_error_summary(ret.stderr)}")
            else:
                _hw_diag(f"{encoder}: 自检通过")
            return ret.returncode == 0
        except Exception as exc:
            _hw_diag(f"{encoder}: 自检异常 {type(exc).__name__}: {exc}")
            return False

    try:
        ret = subprocess.run([FFMPEG_CMD, "-encoders"], capture_output=True, text=True, timeout=5,
                             creationflags=_hw_creationflags())
        encoders = ret.stdout + ret.stderr
        visible = [name for name in ("h264_nvenc", "h264_amf", "h264_qsv") if name in encoders]
        _hw_diag("可见硬件编码器: " + (", ".join(visible) if visible else "无"))
        for _encoder in ("h264_nvenc", "h264_amf", "h264_qsv"):
            if _encoder in encoders and _can_run_hw_encoder(_encoder):
                HARDWARE_ENCODER = _encoder
                _hw_diag(f"最终硬件编码器: {HARDWARE_ENCODER}")
                break
        if HARDWARE_ENCODER is None:
            _hw_diag("最终硬件编码器: 无，使用软件编码")
    except Exception as exc:
        _hw_diag(f"硬件编码检测异常 {type(exc).__name__}: {exc}")
        pass
elif ENABLE_HARDWARE_ENCODER:
    _hw_diag("FFmpeg 未找到，无法启用硬件编码")

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
