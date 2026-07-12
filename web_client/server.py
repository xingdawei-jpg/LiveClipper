from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


def _version_key(value: str) -> tuple[int, int, int, int]:
    parts: list[int] = []
    for chunk in str(value or "").replace("-", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple((parts + [0, 0, 0, 0])[:4])


def _read_app_version(path: Path) -> str:
    try:
        data = json.loads((path / "version.json").read_text(encoding="utf-8-sig"))
        return data.get("version") or data.get("latest_version") or "0"
    except Exception:
        return "0"


def _valid_app_dir(path: Path) -> bool:
    required = ("license_client.py", "cutter_logic.py", "ai_model_config.py")
    return path.is_dir() and all((path / name).exists() for name in required)


def _app_public_key(path: Path) -> str:
    try:
        return (path / "license_public_key.txt").read_text(encoding="utf-8-sig").strip()
    except Exception:
        return ""


def _select_app_dir(*candidates: Path) -> Path:
    expected_public_key = _app_public_key(candidates[0]) if candidates else ""
    valid = [path for path in candidates if _valid_app_dir(path)]
    if expected_public_key:
        matched = [path for path in valid if _app_public_key(path) == expected_public_key]
        if matched:
            valid = matched
    if not valid:
        return candidates[0]
    valid.sort(key=lambda path: _version_key(_read_app_version(path)), reverse=True)
    return valid[0]


USER_UPDATE_ROOT = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper"
MODULE_WEB_DIR = Path(__file__).resolve().parent
ENV_BUNDLE_DIR = Path(os.environ["LIVECLIPPER_BUNDLE_DIR"]).resolve() if os.environ.get("LIVECLIPPER_BUNDLE_DIR") else None

if ENV_BUNDLE_DIR and ENV_BUNDLE_DIR.exists():
    BUNDLE_DIR = ENV_BUNDLE_DIR
    REPO_ROOT = BUNDLE_DIR
elif getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    REPO_ROOT = BUNDLE_DIR
else:
    BUNDLE_DIR = MODULE_WEB_DIR.parent
    REPO_ROOT = MODULE_WEB_DIR.parent


def _valid_web_dir(path: Path) -> bool:
    return path.is_dir() and (path / "frontend" / "index.html").exists() and (path / "frontend" / "assets").is_dir()


def _read_web_version(path: Path) -> str:
    return _read_app_version(path.parent / "app")


def _select_web_dir(*candidates: Path) -> Path:
    valid = [path for path in candidates if _valid_web_dir(path)]
    if valid:
        valid.sort(key=lambda path: _version_key(_read_web_version(path)), reverse=True)
        return valid[0]
    return candidates[0]


if ENV_BUNDLE_DIR or getattr(sys, "frozen", False):
    WEB_DIR = _select_web_dir(BUNDLE_DIR / "web_client", MODULE_WEB_DIR, USER_UPDATE_ROOT / "web_client")
else:
    WEB_DIR = _select_web_dir(MODULE_WEB_DIR, USER_UPDATE_ROOT / "web_client")
if ENV_BUNDLE_DIR or getattr(sys, "frozen", False):
    APP_DIR = _select_app_dir(
        REPO_ROOT / "app",
        WEB_DIR.parent / "app",
        USER_UPDATE_ROOT / "app",
    )
else:
    APP_DIR = _select_app_dir(
        REPO_ROOT / "app",
        WEB_DIR.parent / "app",
        USER_UPDATE_ROOT / "app",
    )
FRONTEND_DIR = WEB_DIR / "frontend"
ASSETS_DIR = FRONTEND_DIR / "assets"

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from ai_model_config import (
    DEEPSEEK_DEFAULT_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    ai_models_url,
    normalize_ai_base_url,
    normalize_ai_model_defaults as _shared_normalize_ai_model_defaults,
)


app = FastAPI(title="LiveClipper Web Client", version="0.1.0")
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

_LOGS: deque[dict[str, Any]] = deque(maxlen=1500)
_LOG_LOCK = threading.Lock()
_LOG_SEQ = 0
_LOG_LAST_BY_SCOPE: dict[str, str] = {}
_TASKS: dict[str, dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()
_OUTPUT_HISTORY_LOCK = threading.Lock()
_CANCELLED_TASKS: set[str] = set()
_TASK_CANCEL_EVENTS: dict[str, threading.Event] = {}
_SCAN_RESULTS: dict[str, Any] = {"products": [], "merged": []}
_SCAN_LOCK = threading.Lock()
_LIVE_PROCS: dict[str, dict[str, Any]] = {}
_LIVE_LOCK = threading.Lock()
LIVE_PROBE_STOP_NOTE = "__liveclipper_stop__"
_CLIP_PREVIEWS: dict[str, dict[str, Any]] = {}
_CLIP_PREVIEW_LOCK = threading.Lock()
_AI_PREVIEW_CACHE_SCHEMA = "sales_roles_v9"
VOLC_REGION_ALIASES = {
    "": "cn-beijing",
    "beijing": "cn-beijing",
    "bj": "cn-beijing",
    "cn-beijing": "cn-beijing",
    "\u5317\u4eac": "cn-beijing",
    "\u4e2d\u56fd\u5317\u4eac": "cn-beijing",
    "shanghai": "cn-shanghai",
    "sh": "cn-shanghai",
    "cn-shanghai": "cn-shanghai",
    "\u4e0a\u6d77": "cn-shanghai",
    "\u4e2d\u56fd\u4e0a\u6d77": "cn-shanghai",
    "guangzhou": "cn-guangzhou",
    "gz": "cn-guangzhou",
    "cn-guangzhou": "cn-guangzhou",
    "\u5e7f\u5dde": "cn-guangzhou",
    "\u4e2d\u56fd\u5e7f\u5dde": "cn-guangzhou",
    "singapore": "ap-southeast-1",
    "ap-southeast-1": "ap-southeast-1",
    "\u65b0\u52a0\u5761": "ap-southeast-1",
}
_PREVIEW_CLEARED_AT = 0.0
_VIDEO_INFO_CACHE: dict[str, dict[str, Any]] = {}
_VIDEO_FP_CACHE: dict[str, dict[str, Any]] = {}
_UPDATE_STATE: dict[str, Any] = {
    "running": False,
    "last_result": None,
}
_UPDATE_LOCK = threading.Lock()


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _clean_low_level_terms(text: str) -> str:
    replacements = {
        "volcengine_asr": "语音识别",
        "volcengine": "语音识别",
        "Volcengine": "云端语音识别",
        "火山引擎": "云端",
        "DeepSeek": "AI模型",
        "Whisper": "本地语音识别",
        "SmartCrop": "智能裁切",
        "FFmpeg": "视频处理程序",
        "ffmpeg": "视频处理程序",
        "libx264": "稳定编码",
        "h264_qsv": "硬件加速编码",
        "TOS": "云端上传",
        "ASR": "语音识别",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\[[TV]\]\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _short_error(raw: str) -> str:
    text = str(raw or "").strip()
    text = re.sub(r"Traceback \(most recent call last\):.*", "", text, flags=re.S)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        text = lines[-1]
    text = _clean_low_level_terms(text)
    text = text.replace("Exception:", "").replace("RuntimeError:", "").strip()
    if len(text) > 220:
        text = text[:220].rstrip() + "..."
    return text or "未知错误"


def _solution_for_error(raw: str) -> str:
    lower = str(raw or "").lower()
    if "no such file" in lower or "file not found" in lower or "文件不存在" in lower or "找不到" in lower:
        return "请检查路径是否正确，文件是否被移动或删除。"
    if "permission" in lower or "access is denied" in lower or "拒绝访问" in lower or "占用" in lower:
        return "请关闭正在占用该文件的软件，或换一个有写入权限的输出目录。"
    if "timeout" in lower or "timed out" in lower or "超时" in lower:
        return "请检查网络、代理和防火墙，稍后重试；云端服务建议先运行完整诊断。"
    if "ssl" in lower or "certificate" in lower or "eof occurred" in lower:
        return "请检查系统时间、证书、代理和防火墙；公司网络可尝试切换热点。"
    if "401" in lower or "403" in lower or "unauthorized" in lower or "forbidden" in lower or "鉴权" in lower or "api key" in lower or "ak" in lower or "sk" in lower:
        return "请到设置页重新填写密钥，并确认账号权限、地域和桶名一致。"
    if "402" in lower or "quota" in lower or "余额" in lower or "insufficient" in lower or "限额" in lower:
        return "请到模型平台检查余额和调用额度，充值或更换可用 API Key 后再试。"
    if "conversion failed" in lower or "codec" in lower or "decode" in lower or "encode" in lower or "invalid argument" in lower:
        return "请先用播放器确认源视频能正常播放；仍失败时可换一个输出目录，或把视频转成标准 MP4 后重试。"
    if "no valid speech" in lower or "silence" in lower or "无有效语音" in lower:
        return "测试音频或片段里没有有效人声；请换一段有讲话的视频再试。"
    if "excel" in lower or "openpyxl" in lower or "worksheet" in lower or "时间表" in lower:
        return "请确认表格是飞书导出的 xlsx，包含商品名称和起止时间，并且视频覆盖这些时间段。"
    if "activation" in lower or "license" in lower or "激活" in lower or "试用" in lower:
        return "请到设置页检查激活状态；如已购买，重新激活或解绑后再绑定。"
    if "json" in lower or "ai" in lower or "模型" in lower:
        return "请检查 AI API Key、模型名和网络；保存设置后再测试连接。"
    if "disk" in lower or "space" in lower or "磁盘" in lower or "空间" in lower:
        return "请清理磁盘空间，或把输出目录改到空间更大的磁盘。"
    return "请保存当前日志和素材路径，重新尝试；如果仍失败，把错误信息发给开发。"


def _friendly_error_message(raw: str) -> str:
    lower = str(raw or "").lower()
    if "402" in lower or "余额不足" in raw:
        return "AI 接口余额不足：请到模型平台充值，或在设置中更换可用 API Key 后再试。"
    return f"处理失败：{_short_error(raw)}。解决办法：{_solution_for_error(raw)}"


def _friendly_warning_message(raw: str) -> str | None:
    text = str(raw or "")
    lower = text.lower()
    cloud_asr = (
        "火山引擎" in text
        or "阿里云" in text
        or "云端ASR" in text
        or "云端 ASR" in text
        or "云端语音识别" in text
        or "volcengine" in lower
    )
    if cloud_asr and "未配" in text:
        return "云端语音识别未配置完整，正在使用本地语音识别。"
    if cloud_asr and ("失败" in text or "异常" in text) and (
        "降级" in text or "切换到本地" in text or "本地识别" in text or "whisper" in lower
    ):
        return "云端语音识别失败，已切换到本地语音识别。"
    if cloud_asr and ("上传失败" in text or "提交失败" in text or "准备音频失败" in text):
        return "云端语音识别准备失败，正在尝试本地语音识别；建议检查网络、代理、防火墙或云端诊断。"
    if cloud_asr and ("ASR 失败" in text or "ASR失败" in text or "语音识别失败" in text or "异常" in text):
        return "云端语音识别失败，正在尝试本地语音识别。"
    if "失败" in text and ("降级到本地" in text or "切换到本地" in text):
        return "云端语音识别不可用，正在使用本地语音识别。"
    if "硬件" in text and ("回" in text or "失败" in text):
        return "硬件加速不可用，已自动改用稳定编码，成片效果不受影响。"
    if "cut failed" in lower or "裁剪失败" in text:
        return "有片段裁剪失败，已自动跳过。建议检查源视频时长是否覆盖该片段。"
    if "cta" in lower or "行动号召" in text:
        return "提示：结尾促单片段偏弱，可换一段素材或放宽选片规则。"
    if "时间跳变" in text:
        return "提示：片段时间跨度较大，成片可能有跳跃感。"
    if "缺少" in text and "close" in lower:
        return "提示：结尾收口片段不足，成片可能偏产品介绍。"
    return None


def _progress_message(raw: str, scope: str) -> str | None:
    text = str(raw or "").strip()
    lower = text.lower()
    compact = re.sub(r"\s+", " ", text)
    cloud_asr = (
        "火山引擎" in compact
        or "阿里云" in compact
        or "云端ASR" in compact
        or "云端 ASR" in compact
        or "云端语音识别" in compact
        or "volcengine" in lower
    )
    local_asr = "Whisper" in compact or "whisper" in lower or "本地语音识别" in compact or "本地识别" in compact

    if not compact or "__batch" in lower or "[progress]" in lower:
        return ""
    if re.match(r"^(frame=|video:|audio:|subtitle:|qavg:)", lower):
        return ""
    if re.match(r"^(ffmpeg:|\[t\]|vf:|rc=|kb:)", lower):
        return ""
    if re.match(r"^\s*(hook|product|close)\s+\|", lower):
        return ""
    if re.match(r"^片段\d+\s*\|", compact):
        return _clean_low_level_terms(compact)
    if any(token in lower for token in ("temperature=", "字段", "原始", "编号srt", "预扫", "总分=", "qavg", "elapsed=")):
        return ""

    important_tokens = (
        "目标时长:",
        "使用本地SRT",
        "SRT已缓存",
        "素材分组:",
        "结构修复:",
        "最终片单:",
        "JSON不完整",
        "降级方案",
        "成品时长:",
        "警告:",
        "多版本输出完成",
    )
    if any(token in compact for token in important_tokens):
        return _clean_low_level_terms(compact)

    if re.match(r"^(切割|Cut)\s*\[\d+/\d+\]", compact):
        return _clean_low_level_terms(compact)
    if re.match(r"^OK\s*\[", compact):
        return _clean_low_level_terms(compact)
    if any(token in compact for token in (
        "Concatenating ",
        "Concat done:",
        "Concat copy:",
        "拼接 ",
        "拼接完成:",
        "Cut ",
    )):
        return _clean_low_level_terms(compact)
    if compact.startswith("SmartCrop:") and any(token in compact for token in (
        "应用封面构图",
        "应用兜底构图",
        "为保清晰度跳过",
        "zoom=",
    )):
        return _clean_low_level_terms(compact)
    if compact.startswith("KenBurns:") and any(token in compact for token in (
        "OpenCV",
        "FFmpeg",
        "推进",
        "拉远",
        "失败",
        "回退",
    )):
        return _clean_low_level_terms(compact)
    if compact.startswith("Ken Burns:"):
        return _clean_low_level_terms(compact)

    noisy_detail_tokens = (
        "SRT拆分",
        "前置清洗",
        "SRT短条目合并",
        "SRT预去重",
        "阶段耗时:",
        "切割报告",
        "综合评分:",
        "总时长:",
        "输出路径:",
        "路径:",
        "大小:",
        "片段:",
        "Hook:",
        "品类:",
        "━━",
        "▰",
        "▱",
        "品类过滤:",
        "品类合法性校验",
        "卖点聚焦",
        "废话裁剪",
        "价格排除",
        "尺码后置",
        "CTA警告",
        "Hook提取",
        "Hook降级",
        "短Hook检测",
        "时间重叠",
        "差异化历史",
        "历史避让",
        "历史保留",
        "检测视频时长",
        "视频时长:",
        "视频处理程序:",
        "FFmpeg:",
        "编码器:",
        "SmartCrop",
        "智能裁切:",
        "KenBurns",
        "开始切割",
        "切割 [",
        "OK [",
        "拼接 ",
        "拼接完成:",
        "整体去重",
        "去重步骤",
        "去重效果:",
        "音频提取完成",
        "drawtext",
        "滤镜数量",
        "视频:",
        "volcengine_asr:",
        "上传音频",
        "上传完成",
        "pre_signed_url",
        "提交 ASR",
        "任务已提交",
        "轮询中",
        "解析得到",
        "已清理",
        "字幕修复模型",
        "DeepSeek修复",
        "长句拆分",
        "字幕标点",
    )
    if any(token in compact for token in noisy_detail_tokens):
        return ""

    if "已启动" in compact or "任务已启动" in compact:
        return _clean_low_level_terms(compact)
    if "试用" in compact and "剩余" in compact:
        return compact

    if cloud_asr and any(token in compact for token in ("语音识别成功", "ASR 成功", "ASR成功", "识别完成", "解析得到")):
        return "云端语音识别成功。"
    if local_asr and any(token in compact for token in ("识别完成", "识别成功", "ASR 成功", "ASR成功")):
        return "本地语音识别完成。"
    if any(token in compact for token in ("云端ASR未启", "云端 ASR 未启", "云端语音识别未启")):
        return "未启用云端语音识别，正在使用本地语音识别。"
    if "未配置云端语音识别" in compact:
        return "云端语音识别未配置，正在使用本地语音识别。"
    if cloud_asr and any(token in compact for token in ("启动", "正在使用", "正在尝试", "已启", "上传音频", "提交", "轮询", "获取准确文本")):
        return "正在使用云端语音识别。"
    if local_asr and any(token in compact for token in ("启动", "开始本地", "正在", "使用本地", "降级到本地", "识别最终视频音频", "语音识别")):
        return "正在使用本地语音识别。"
    if local_asr and "失败" in compact:
        return "本地语音识别失败。"

    if any(token in compact for token in ("识别完成", "识别成功", "解析得到")):
        return "语音识别完成。"
    if any(token in compact for token in ("上传音频", "提交", "轮询", "语音识别", "识别中")):
        return "正在识别语音。"

    if any(token in compact for token in ("AI 智能选片", "智能选片", "调用", "响应成功", "补选")):
        return "正在智能选片。"
    if any(token in compact for token in ("校验通过", "解析到")) and "片段" in compact:
        return "片段选择完成。"

    if any(token in compact for token in ("SRT拆分", "前置清洗", "短条目", "品类过滤", "卖点聚焦", "Hook", "废话裁剪", "价格排除", "关联品类")):
        return "正在整理文案和片段。"

    if any(token in compact for token in ("检测视频时长", "视频时长", "检测到源视频", "检测到人物", "裁切")):
        return "正在分析画面。"
    if any(token in compact for token in ("开始切割", "切割 [", "Cut [")):
        return "正在裁剪片段。"
    if any(token in compact for token in ("鎷兼帴", "Concatenating", "Concat done")):
        return "正在合成成片。"
    if any(token in compact for token in ("去重", "Dedup")):
        if "完成" in compact:
            return "画面处理完成。"
        return "正在处理画面效果。"
    if any(token in compact for token in ("KenBurns", "动态", "缩放")):
        return "正在添加动态画面。"
    if "字幕" in compact or "drawtext" in lower:
        if "完成" in compact or "成功" in compact:
            return "字幕处理完成。"
        return "正在处理字幕。"
    if any(token in compact for token in ("生成成功", "Mix done", "处理完成", "成片完成")):
        return "成品生成完成。"

    if "读取时间" in compact or "时间表读取完成" in compact:
        return _clean_low_level_terms(compact)
    if "扫描" in compact and ("开始" in compact or "完成" in compact or "发现" in compact):
        return _clean_low_level_terms(compact)
    if "导出" in compact and ("完成" in compact or "已导出" in compact):
        return _clean_low_level_terms(compact)
    if "直播流检测" in compact:
        return "正在检测直播地址。"
    if "开始录制" in compact:
        return "正在录制直播。"
    if "录制完成" in compact:
        return "直播录制完成。"

    if "诊断" in compact:
        return _clean_low_level_terms(compact)
    if scope == "settings":
        return _clean_low_level_terms(compact)

    if any(token in lower for token in ("smartcrop", "volcengine", "deepseek", "whisper", "ffmpeg", "tos", "popen", "filter_complex")):
        return ""
    return _clean_low_level_terms(compact)


def _diagnostic_raw_message(raw: str, message: str) -> str:
    repaired = _repair_mojibake_text(raw).strip()
    if not repaired or repaired == str(message or "").strip():
        return ""
    if len(repaired) > 1200:
        repaired = repaired[:1200].rstrip() + "..."
    return repaired


def _is_noisy_runtime_log(raw: str) -> bool:
    lower = str(raw or "").strip().lower()
    if not lower:
        return True
    noisy_prefixes = (
        "frame=",
        "video:",
        "audio:",
        "subtitle:",
        "qavg:",
        "size=",
        "bitrate=",
        "speed=",
    )
    if lower.startswith(noisy_prefixes):
        return True
    if lower.startswith("[progress]"):
        return True
    return False


def _trim_log_message(text: str, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def _simplify_log(level: str, message: str, scope: str) -> tuple[str, str] | None:
    normalized_level = "warning" if level == "warn" else level
    raw = str(message or "")
    repaired_raw = _repair_mojibake_text(raw).strip()
    lower = raw.lower()

    if _is_noisy_runtime_log(repaired_raw):
        return None

    if normalized_level == "error":
        if repaired_raw and _mojibake_score(repaired_raw) == 0:
            return "error", _trim_log_message(repaired_raw, 420)
        return "error", _friendly_error_message(raw)
    if normalized_level == "success":
        if repaired_raw and _mojibake_score(repaired_raw) == 0:
            return "success", _trim_log_message(repaired_raw, 320)
        return "success", {"smart-cut": "智能成片任务完成。", "mix": "混剪任务完成。", "settings": "设置已保存。", "dedup": "创作辅助处理完成。"}.get(scope, "任务完成。")
    if normalized_level == "warning":
        friendly_warning = _friendly_warning_message(repaired_raw)
        if friendly_warning:
            return "warning", friendly_warning
        if repaired_raw and _mojibake_score(repaired_raw) == 0:
            return "warning", _trim_log_message(repaired_raw, 420)
        if any(token in lower for token in ("duplicate", "overlap", "dedup")):
            return "warning", "提示：已自动处理部分重复片段，仍建议预览确认。"
        return "warning", "提示：任务遇到可恢复问题，已尽量继续处理。"

    progress = _progress_message(repaired_raw, scope)
    if progress:
        return normalized_level, _trim_log_message(progress, 320)
    if progress == "":
        if "诊断" in repaired_raw or "diagnostic" in repaired_raw.lower():
            return normalized_level, _trim_log_message(_clean_low_level_terms(repaired_raw), 320)
        return None

    if repaired_raw and _mojibake_score(repaired_raw) == 0:
        return normalized_level, _trim_log_message(_clean_low_level_terms(repaired_raw), 320)

    if scope == "ai-scan":
        return "info", "正在扫描素材。"
    if scope == "settings":
        return "info", "正在处理设置。"
    if scope == "dedup":
        return "info", "正在处理创作辅助任务。"
    return "info", "任务正在运行。"


def emit_log(level: str, message: str, scope: str = "system") -> None:
    global _LOG_SEQ
    raw_message = str(message or "")
    simplified = _simplify_log(level, message, scope)
    if not simplified:
        return
    level, message = simplified
    raw_for_diagnostics = _diagnostic_raw_message(raw_message, message)
    with _LOG_LOCK:
        last_key = f"{scope}:{message}:{raw_for_diagnostics[:160]}"
        if _LOG_LAST_BY_SCOPE.get(scope) == last_key:
            return
        _LOG_LAST_BY_SCOPE[scope] = last_key
        _LOG_SEQ += 1
        _LOGS.append(
            {
                "id": _LOG_SEQ,
                "time": _now(),
                "level": level,
                "scope": scope,
                "message": message,
                "raw": raw_for_diagnostics,
            }
        )


def _snapshot_logs(after_id: int = 0) -> list[dict[str, Any]]:
    with _LOG_LOCK:
        return [item for item in list(_LOGS) if item["id"] > after_id]


def _load_version() -> str:
    version_file = APP_DIR / "version.json"
    try:
        data = json.loads(version_file.read_text(encoding="utf-8-sig"))
        return data.get("version") or data.get("latest_version") or "dev"
    except Exception:
        return "dev"


DEFAULT_PREFERENCE_WEIGHTS = {
    "版型显瘦": 2.0,
    "面料质感": 2.0,
    "颜色氛围": 1.5,
    "场景搭配": 1.5,
    "情绪感染": 1.0,
    "性价比": 1.0,
    "流行趋势": 0.5,
    "紧迫稀缺": 0.5,
}

PREFERENCE_WEIGHT_ALIASES = {
    "鐗堝瀷鏄剧槮": "版型显瘦",
    "闈㈡枡璐ㄦ劅": "面料质感",
    "棰滆壊姘涘洿": "颜色氛围",
    "穿着场景": "场景搭配",
    "鎯呯华鎰熸煋": "情绪感染",
}

DEFAULT_AI_RULES = {
    "narrative": "",
    "category_filter": True,
    "time_coherence": True,
    "hook_cap": "5秒",
    "custom_text": "",
}


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return {}


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _keyword_config_path() -> Path:
    user_file = _safe_user_child("keywords.json")
    return user_file if user_file.exists() else APP_DIR / "keywords.json"


def _load_keyword_config() -> dict[str, Any]:
    default_data = _read_json_file(APP_DIR / "keywords.json")
    user_file = _safe_user_child("keywords.json")
    user_data = _read_json_file(user_file)
    if not user_data:
        return default_data
    return user_data


def _load_effective_keyword_config() -> dict[str, Any]:
    try:
        from ai_clipper import load_keywords

        effective = load_keywords()
        return {
            "clip_keywords": effective.get("clip_keywords", {}),
            "forbidden_phrases": effective.get("forbidden_phrases", []),
            "filler_words": effective.get("filler_words", []),
            "negative_signals": effective.get("negative_signals", []),
            "preference_keywords": effective.get("preference_keywords", {}),
            "detail_keywords": effective.get("detail_keywords", []),
            "_web_vocab_full_override": True,
        }
    except Exception:
        return dict(_load_keyword_config())


def _keyword_count(data: dict[str, Any]) -> int:
    count = 0
    for value in data.values():
        if isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, list):
                    count += len(nested)
        elif isinstance(value, list):
            count += len(value)
    return count


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            items.append(text)
            seen.add(text)
    return items


def _clean_keyword_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, items in value.items():
        name = str(key or "").strip()
        cleaned = _clean_string_list(items)
        if name and cleaned:
            result[name] = cleaned
    return result


def _normalize_keyword_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(_load_effective_keyword_config())
    map_keys = {"clip_keywords", "preference_keywords"}
    list_keys = {"forbidden_phrases", "filler_words", "negative_signals", "detail_keywords"}
    handled = map_keys | list_keys
    derived_keys = {"hook_keywords", "_source"}

    for key in map_keys:
        if key in payload:
            data[key] = _clean_keyword_map(payload.get(key))
    for key in list_keys:
        if key in payload:
            data[key] = _clean_string_list(payload.get(key))
    for key, value in payload.items():
        if key not in handled and key not in derived_keys and not str(key).startswith("_"):
            data[key] = value
    data["_web_vocab_full_override"] = True
    return data


def _clear_ai_keyword_cache() -> None:
    try:
        import ai_clipper

        cache = getattr(ai_clipper, "_keywords_cache", None)
        if isinstance(cache, dict):
            cache["_data"] = None
            cache["_mtime"] = 0
    except Exception:
        pass


def _update_keyword_config(updates: dict[str, Any]) -> Path:
    user_file = _safe_user_child("keywords.json")
    data = _read_json_file(user_file) or _read_json_file(APP_DIR / "keywords.json")
    data.update(updates)
    user_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return user_file


def _load_settings() -> dict[str, Any]:
    from ai_clipper import load_settings

    keyword_config = _load_keyword_config()
    defaults = {
        "api_key": "",
        "base_url": DEEPSEEK_DEFAULT_BASE_URL,
        "model": DEEPSEEK_DEFAULT_MODEL,
        "enabled": False,
        "asr_enabled": False,
        "asr_provider": "火山引擎",
        "asr_api_key": "",
        "asr_base_url": "https://dashscope.aliyuncs.com",
        "asr_model": "paraformer-v2",
        "asr_preset": "火山引擎",
        "volc_api_key": "",
        "volc_app_id": "",
        "volc_access_token": "",
        "volc_tos_ak": "",
        "volc_tos_sk": "",
        "volc_bucket": "livec",
        "volc_region": "cn-beijing",
        "whisper_model": "small",
        "aliyun_api_key": "",
        "aliyun_oss_ak": "",
        "aliyun_oss_sk": "",
        "aliyun_bucket": "",
        "aliyun_region": "oss-cn-shanghai",
        "preference_weights": dict(DEFAULT_PREFERENCE_WEIGHTS),
        "style_profile_enabled": True,
        "style_profile_strength": "auto",
        "ai_rules": dict(DEFAULT_AI_RULES),
        "ui_theme": "system",
        "hardware_encoder_enabled": False,
        "subtitle_font_size": 52,
        "ui_font_size": 14,
    }
    loaded = load_settings()
    defaults.update(loaded or {})
    pref_weights = dict(DEFAULT_PREFERENCE_WEIGHTS)
    if isinstance((loaded or {}).get("preference_weights"), dict):
        for key, value in (loaded or {}).get("preference_weights", {}).items():
            pref_weights[PREFERENCE_WEIGHT_ALIASES.get(key, key)] = value
    defaults["preference_weights"] = pref_weights

    ai_rules = dict(DEFAULT_AI_RULES)
    if isinstance((loaded or {}).get("ai_rules"), dict):
        ai_rules.update((loaded or {}).get("ai_rules", {}))
    defaults["ai_rules"] = ai_rules
    defaults["volc_region"] = _normalize_volc_region(defaults.get("volc_region"))
    return _normalize_ai_model_defaults(defaults)


def _save_settings(settings: dict[str, Any]) -> bool:
    from ai_clipper import save_settings

    data = _normalize_ai_model_defaults(settings)
    data["volc_region"] = _normalize_volc_region(data.get("volc_region"))
    try:
        data["subtitle_font_size"] = max(32, min(96, int(float(data.get("subtitle_font_size", 52)))))
    except Exception:
        data["subtitle_font_size"] = 52
    try:
        data["ui_font_size"] = max(12, min(18, int(float(data.get("ui_font_size", 14)))))
    except Exception:
        data["ui_font_size"] = 14
    return bool(save_settings(data))


def _normalize_ai_model_defaults(settings: dict[str, Any]) -> dict[str, Any]:
    return _shared_normalize_ai_model_defaults(settings)


def _normalize_volc_region(value: Any) -> str:
    text = str(value or "").strip()
    compact = text.replace(" ", "").replace("_", "-").lower()
    return VOLC_REGION_ALIASES.get(text) or VOLC_REGION_ALIASES.get(compact) or compact or "cn-beijing"


def _get_user_data_dir() -> Path:
    from config import USER_DATA_DIR

    path = Path(USER_DATA_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_user_child(*parts: str) -> Path:
    base = _get_user_data_dir().resolve()
    target = base.joinpath(*parts).resolve()
    if not str(target).lower().startswith(str(base).lower()):
        raise HTTPException(status_code=400, detail="Unsafe path")
    return target


def _default_user_data_dir() -> Path:
    try:
        import config
        return Path(config._default_user_data_dir()).resolve()
    except Exception:
        return (Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper").resolve()


def _user_data_location_file() -> Path:
    try:
        import config
        return Path(config._location_file()).resolve()
    except Exception:
        return _default_user_data_dir() / "user_data_location.json"


def _path_same(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except Exception:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _path_is_child(path: Path, parent: Path) -> bool:
    try:
        path_resolved = path.resolve()
        parent_resolved = parent.resolve()
        return os.path.normcase(str(path_resolved)).startswith(os.path.normcase(str(parent_resolved)) + os.sep)
    except Exception:
        return False


def _validate_user_data_target(raw: str) -> Path:
    text = str(raw or "").strip().strip('"')
    if not text:
        raise HTTPException(status_code=400, detail="请选择用户数据目录。")
    target = Path(os.path.expandvars(text)).expanduser()
    if not target.is_absolute():
        target = target.resolve()
    if str(target) in {target.anchor, str(Path(target.anchor))}:
        raise HTTPException(status_code=400, detail="不能直接选择磁盘根目录，请新建一个 LiveClipperData 文件夹。")
    current = _get_user_data_dir()
    if not _path_same(target, current) and _path_is_child(target, current):
        raise HTTPException(status_code=400, detail="新目录不能放在当前用户数据目录里面，请选择另一个磁盘或独立文件夹。")
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / f".liveclipper_write_test_{os.getpid()}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"目标目录不可写：{exc}") from exc
    return target.resolve()


_USER_DATA_MIGRATE_SKIP = {
    "cache",
    "temp",
    "clip_previews",
    "web_uploads",
    "app",
    "web_client",
    "__pycache__",
    "user_data_location.json",
}


def _copy_user_data_persistent(current: Path, target: Path) -> dict[str, Any]:
    if _path_same(current, target) or not current.exists():
        return {"files": 0, "bytes": 0, "items": []}
    copied_files = 0
    copied_bytes = 0
    copied_items: list[str] = []
    for child in current.iterdir():
        if child.name in _USER_DATA_MIGRATE_SKIP:
            continue
        if _path_same(child, target) or _path_is_child(target, child):
            continue
        dst = target / child.name
        try:
            if child.is_dir():
                shutil.copytree(child, dst, dirs_exist_ok=True)
                copied_items.append(child.name)
                for item in child.rglob("*"):
                    if item.is_file():
                        copied_files += 1
                        copied_bytes += item.stat().st_size
            elif child.is_file():
                shutil.copy2(child, dst)
                copied_items.append(child.name)
                copied_files += 1
                copied_bytes += child.stat().st_size
        except Exception as exc:
            emit_log("warning", f"用户数据迁移跳过 {child.name}: {exc}", "settings")
    return {"files": copied_files, "bytes": copied_bytes, "items": copied_items}


def _cache_clear_active_tasks() -> list[str]:
    with _TASK_LOCK:
        return [
            str(task.get("title") or task.get("scope") or task_id)
            for task_id, task in _TASKS.items()
            if task.get("status") in {"queued", "running"}
        ]


def _cache_target_stats(path: Path) -> dict[str, Any]:
    files = 0
    size = 0
    try:
        if path.is_file():
            return {"files": 1, "bytes": path.stat().st_size}
        if path.is_dir():
            for item in path.rglob("*"):
                try:
                    if item.is_file():
                        files += 1
                        size += item.stat().st_size
                except Exception:
                    continue
    except Exception:
        pass
    return {"files": files, "bytes": size}


def _remove_cache_target(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        stats = _cache_target_stats(path)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        removed = not path.exists()
        return {
            "path": str(path),
            "files": int(stats.get("files") or 0),
            "bytes": int(stats.get("bytes") or 0),
            "mb": round(float(stats.get("bytes") or 0) / 1024 / 1024, 2),
            "removed": removed,
        }
    except Exception as exc:
        return {"path": str(path), "files": 0, "bytes": 0, "mb": 0, "removed": False, "error": str(exc)}


def _cache_clear_targets() -> list[Path]:
    targets: list[Path] = []
    for name in ("cache", "temp", "clip_previews", "web_uploads"):
        targets.append(_safe_user_child(name))

    temp_root = Path(tempfile.gettempdir())
    for name in ("live_cutter_web_asr", "live_cutter_stt", "livec_schedule", "liveclipper_ts_normalized"):
        targets.append(temp_root / name)
    for prefix in ("liveclipper_asr_", "liveclipper_volc_diag_"):
        try:
            targets.extend(path for path in temp_root.iterdir() if path.name.startswith(prefix))
        except Exception:
            pass

    root = Path("C:/")
    try:
        if (root / "lc_temp").exists():
            targets.append(root / "lc_temp")
        targets.extend(
            path for path in root.iterdir()
            if path.is_dir() and (path.name.startswith("lc_temp_") or path.name.startswith("lc_temp_mix_"))
        )
    except Exception:
        pass

    unique: list[Path] = []
    seen: set[str] = set()
    for path in targets:
        key = str(path).replace("/", "\\").lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _output_history_file() -> Path:
    return _safe_user_child("output_history.json")


def _normalize_history_path(value: Any) -> str:
    return str(value or "").strip().strip('"')


def _output_history_name(path: str) -> str:
    try:
        return Path(path).name or path
    except Exception:
        return path


def _output_history_records_from_task(task: dict[str, Any]) -> list[dict[str, Any]]:
    if str(task.get("status") or "") != "completed":
        return []
    paths: list[str] = []
    raw_outputs = task.get("outputs")
    if isinstance(raw_outputs, list):
        paths.extend(_normalize_history_path(item) for item in raw_outputs)
    output = _normalize_history_path(task.get("output"))
    if output:
        paths.append(output)
    if not paths:
        output_dir = _normalize_history_path(task.get("output_dir"))
        if output_dir:
            paths.append(output_dir)

    cleaned: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = path.replace("/", "\\").lower()
        if not path or key in seen:
            continue
        seen.add(key)
        cleaned.append(path)

    total = len(cleaned) or int(task.get("result_count") or 0) or 1
    batch_total = max(total, int(task.get("batch_total") or 0))
    batch_done = max(0, min(batch_total, int(task.get("batch_done") or total)))
    batch_failed = max(0, int(task.get("batch_failed") or 0))
    created_at = float(task.get("finished_at") or task.get("started_at") or time.time())
    records: list[dict[str, Any]] = []
    for index, path in enumerate(cleaned, start=1):
        records.append(
            {
                "path": path,
                "name": _output_history_name(path),
                "scope": task.get("scope") or "",
                "title": task.get("title") or "成片",
                "task_id": task.get("id") or "",
                "created_at": created_at,
                "created_at_text": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S"),
                "index": index,
                "total": total,
                "batch_total": batch_total,
                "batch_done": batch_done,
                "batch_failed": batch_failed,
                "exists": Path(path).exists(),
            }
        )
    return records


def _read_output_history_items() -> list[dict[str, Any]]:
    path = _output_history_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and _normalize_history_path(item.get("path"))]


def _write_output_history_items(items: list[dict[str, Any]]) -> None:
    path = _output_history_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "items": items[:120], "updated_at": time.time()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_output_history_items(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group or []:
            path = _normalize_history_path(item.get("path"))
            if not path:
                continue
            key = path.replace("/", "\\").lower()
            current = merged.get(key, {})
            if current and item.get("source") == "scan":
                next_item = {**item, **current}
            else:
                next_item = {**current, **item}
            next_item.update({"path": path, "name": next_item.get("name") or _output_history_name(path)})
            try:
                next_item["exists"] = Path(path).exists()
            except Exception:
                next_item["exists"] = False
            merged[key] = next_item
    return sorted(merged.values(), key=lambda item: float(item.get("created_at") or 0), reverse=True)


def _record_output_history(task: dict[str, Any]) -> None:
    try:
        records = _output_history_records_from_task(task)
        if not records:
            return
        with _OUTPUT_HISTORY_LOCK:
            existing = _read_output_history_items()
            _write_output_history_items(_merge_output_history_items(records, existing))
    except Exception:
        return


def _output_history_dirs_from_preferences() -> list[tuple[str, Path]]:
    scope_map = {
        "smart_cut": "smart-cut",
        "mix": "mix",
        "dedup": "dedup",
        "product_scan": "product-scan",
        "video_split": "video-split",
        "live_rec": "live-rec",
    }
    dirs: list[tuple[str, Path]] = []
    seen: set[str] = set()
    prefs = _load_preferences()
    for group_key, group in prefs.items():
        if not isinstance(group, dict):
            continue
        scope = scope_map.get(str(group_key), "")
        values = group.get("values") if isinstance(group.get("values"), dict) else {}
        for key, value in values.items():
            if key not in {"output-dir", "mix-output-dir", "dedup-output-dir", "ps-output-dir", "vs-output-dir", "live-save-dir"}:
                continue
            raw = _normalize_history_path(value)
            if not raw:
                continue
            path = Path(raw)
            key_norm = str(path).replace("/", "\\").lower()
            if key_norm in seen:
                continue
            seen.add(key_norm)
            dirs.append((scope, path))
    return dirs


def _scan_output_history_from_preferences(limit: int = 80) -> list[dict[str, Any]]:
    extensions = {".mp4", ".mov", ".m4v"}
    records: list[dict[str, Any]] = []
    for scope, root in _output_history_dirs_from_preferences()[:12]:
        if not root.exists() or not root.is_dir():
            continue
        scanned = 0
        try:
            iterator = root.rglob("*")
            for path in iterator:
                scanned += 1
                if scanned > 2500 or len(records) >= limit:
                    break
                if not path.is_file() or path.suffix.lower() not in extensions:
                    continue
                try:
                    stat = path.stat()
                    created_at = stat.st_mtime
                except Exception:
                    created_at = time.time()
                records.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "scope": scope,
                        "title": "历史成片",
                        "task_id": "",
                        "created_at": created_at,
                        "created_at_text": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S"),
                        "index": 1,
                        "total": 1,
                        "exists": True,
                        "source": "scan",
                    }
                )
        except Exception:
            continue
    return sorted(records, key=lambda item: float(item.get("created_at") or 0), reverse=True)[:limit]


def _preferences_file() -> Path:
    return _safe_user_child("web_preferences.json")


def _load_preferences() -> dict[str, Any]:
    data = _read_json_file(_preferences_file())
    return data if isinstance(data, dict) else {}


def _save_preferences(data: dict[str, Any]) -> None:
    target = _preferences_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_file(name: str) -> Path:
    target = _safe_user_child("cache", name)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _file_signature(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        return {
            "path": str(resolved),
            "mtime": round(stat.st_mtime, 3),
            "size": stat.st_size,
        }
    except Exception:
        return {"path": str(path)}


def _preview_cache_key(mode: str, paths: list[Path], payload: Any, srt_path: str | None = None) -> str:
    target_duration = getattr(payload, "target_duration", getattr(payload, "duration", 60))
    data = {
        "schema": _AI_PREVIEW_CACHE_SCHEMA,
        "mode": mode,
        "videos": [_file_signature(path) for path in paths],
        "sidecar_srt": [_file_signature(path.with_suffix(".srt")) for path in paths if path.with_suffix(".srt").exists()],
        "srt": _file_signature(Path(srt_path)) if srt_path else {},
        "category": getattr(payload, "category", ""),
        "focus_hint": getattr(payload, "focus_hint", ""),
        "target": target_duration,
        "ai_controls": getattr(payload, "ai_controls", {}) or {},
    }
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stamp_name() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _load_preview_cache(key: str) -> dict[str, Any] | None:
    data = _read_json_file(_cache_file("ai_preview_cache.json"))
    item = data.get(key)
    if not isinstance(item, dict):
        return None
    if time.time() - float(item.get("created_at", 0) or 0) > 7 * 24 * 3600:
        return None
    if not item.get("raw_clips") or not item.get("clips"):
        return None
    return item


def _save_preview_cache(key: str, item: dict[str, Any]) -> None:
    cache_path = _cache_file("ai_preview_cache.json")
    data = _read_json_file(cache_path)
    data[key] = {
        "created_at": time.time(),
        "raw_clips": item.get("raw_clips", []),
        "clips": item.get("clips", []),
        "srt_path": item.get("srt_path", ""),
        "video": item.get("video", ""),
        "video_name": item.get("video_name", ""),
    }
    if len(data) > 80:
        ordered = sorted(data.items(), key=lambda pair: float(pair[1].get("created_at", 0) or 0), reverse=True)
        data = dict(ordered[:80])
    _write_json_file(cache_path, data)


def _file_dialog_types(kind: str) -> list[tuple[str, str]]:
    if kind == "video":
        return [
            ("视频文件", "*.mp4 *.mov *.mkv *.avi *.flv *.ts *.m4v *.webm"),
            ("所有文件", "*.*"),
        ]
    if kind == "excel":
        return [
            ("Excel 文件", "*.xlsx *.xls"),
            ("所有文件", "*.*"),
        ]
    if kind == "image":
        return [
            ("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp"),
            ("所有文件", "*.*"),
        ]
    if kind == "srt":
        return [
            ("字幕文件", "*.srt"),
            ("所有文件", "*.*"),
        ]
    return [("所有文件", "*.*")]


def _dialog_subprocess(mode: str, title: str, kind: str = "file") -> list[str]:
    filetypes = _file_dialog_types(kind)
    if getattr(sys, "frozen", False):
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if mode == "directory":
                result = filedialog.askdirectory(title=title)
            elif mode == "files":
                result = filedialog.askopenfilenames(title=title, filetypes=filetypes)
            else:
                result = filedialog.askopenfilename(title=title, filetypes=filetypes)
            if isinstance(result, (tuple, list)):
                return [str(Path(p)) for p in result if p]
            return [str(Path(result))] if result else []
        finally:
            root.destroy()

    script = r"""
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

mode = sys.argv[1]
title = sys.argv[2]
filetypes = json.loads(sys.argv[3])

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
try:
    if mode == "directory":
        result = filedialog.askdirectory(title=title)
    elif mode == "files":
        result = filedialog.askopenfilenames(title=title, filetypes=filetypes)
    else:
        result = filedialog.askopenfilename(title=title, filetypes=filetypes)
    if isinstance(result, (tuple, list)):
        paths = [str(Path(p)) for p in result if p]
    else:
        paths = [str(Path(result))] if result else []
    sys.stdout.buffer.write(json.dumps(paths, ensure_ascii=False).encode("utf-8"))
finally:
    root.destroy()
"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, mode, title, json.dumps(filetypes, ensure_ascii=False)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            env=env,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(err or "文件选择窗口打开失败")
        return json.loads(proc.stdout.decode("utf-8", errors="replace") or "[]")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("文件选择超时，请重新点击选择。") from exc


def _choose_files_dialog(title: str = "选择视频文件") -> list[str]:
    return _dialog_subprocess("files", title, "video")


def _choose_file_dialog(title: str = "选择文件", kind: str = "file") -> str:
    paths = _dialog_subprocess("file", title, kind)
    return paths[0] if paths else ""


def _choose_directory_dialog(title: str = "选择文件夹") -> str:
    paths = _dialog_subprocess("directory", title, "file")
    return paths[0] if paths else ""


def _upload_dir() -> Path:
    target = _safe_user_child("web_uploads", time.strftime("%Y%m%d"))
    target.mkdir(parents=True, exist_ok=True)
    return target


class SettingsPayload(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    enabled: bool = False
    asr_enabled: bool = False
    asr_provider: str = "火山引擎"
    asr_api_key: str = ""
    asr_base_url: str = ""
    asr_model: str = ""
    asr_preset: str = ""
    volc_api_key: str = ""
    volc_app_id: str = ""
    volc_access_token: str = ""
    volc_tos_ak: str = ""
    volc_tos_sk: str = ""
    volc_bucket: str = ""
    volc_region: str = "cn-beijing"
    whisper_model: str = "small"
    aliyun_api_key: str = ""
    aliyun_oss_ak: str = ""
    aliyun_oss_sk: str = ""
    aliyun_bucket: str = ""
    aliyun_region: str = ""
    preference_weights: dict[str, float] = Field(default_factory=dict)
    style_profile_enabled: bool = True
    style_profile_strength: str = "auto"
    ai_rules: dict[str, Any] = Field(default_factory=dict)
    ui_theme: str = "system"
    hardware_encoder_enabled: bool = False
    subtitle_font_size: int = Field(default=52, ge=32, le=96)
    ui_font_size: int = Field(default=14, ge=12, le=18)


class LicensePayload(BaseModel):
    code: str = ""


class FileDialogPayload(BaseModel):
    kind: str = "file"
    title: str = ""


class PathPayload(BaseModel):
    path: str = ""


class UserDataDirPayload(BaseModel):
    path: str = ""
    migrate: bool = True


class PathsPayload(BaseModel):
    paths: list[str] = Field(default_factory=list)


class StopScopePayload(BaseModel):
    scope: str = ""


class StopTaskPayload(BaseModel):
    task_id: str = ""


class PreflightPayload(BaseModel):
    feature: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class SmartCutPayload(BaseModel):
    video_paths: list[str] = Field(default_factory=list)
    srt_path: str = ""
    output_dir: str = ""
    category: str = "自动检测"
    focus_hint: str = "自动"
    ai_controls: dict[str, Any] = Field(default_factory=dict)
    target_duration: int = Field(default=60, ge=10, le=600)
    versions: int = Field(default=1, ge=1, le=20)
    dedup_preset: str = "medium"
    video: dict[str, Any] = Field(default_factory=dict)
    audio: dict[str, Any] = Field(default_factory=dict)
    transition: dict[str, Any] = Field(default_factory=dict)
    mirror_enabled: bool = True
    subtitle_overlay: bool = True
    smart_crop_enabled: bool = True
    crop_level: str = "medium"
    ken_burns_enabled: bool = True
    ken_burns_intensity: str = "中"
    pip_enabled: bool = False
    pip_path: str = ""
    pip_folder: str = ""
    pip_size: float = Field(default=0.15, ge=0.05, le=1.0)
    pip_opacity: float = Field(default=0.03, ge=0.01, le=1.0)
    pip_pos: str = "鍙充笅"


class MixPayload(BaseModel):
    video_paths: list[str] = Field(default_factory=list)
    output_dir: str = ""
    category: str = "自动检测"
    versions: int = Field(default=1, ge=1, le=20)
    duration: int = Field(default=60, ge=10, le=600)
    focus_hint: str = "自动"
    ai_controls: dict[str, Any] = Field(default_factory=dict)
    dedup_preset: str = "medium"
    video: dict[str, Any] = Field(default_factory=dict)
    audio: dict[str, Any] = Field(default_factory=dict)
    transition: dict[str, Any] = Field(default_factory=dict)
    mirror_enabled: bool = True
    subtitle_overlay: bool = True
    smart_crop_enabled: bool = True
    crop_level: str = "medium"
    ken_burns_enabled: bool = True
    ken_burns_intensity: str = "中"
    pip_enabled: bool = False
    pip_path: str = ""
    pip_folder: str = ""
    pip_size: float = Field(default=0.15, ge=0.05, le=1.0)
    pip_opacity: float = Field(default=0.03, ge=0.01, le=1.0)
    pip_pos: str = "鍙充笅"


class MixBatchGroup(BaseModel):
    name: str = ""
    video_paths: list[str] = Field(default_factory=list)


class MixBatchPayload(MixPayload):
    groups: list[MixBatchGroup] = Field(default_factory=list)


class AiScanPayload(BaseModel):
    video_paths: list[str] = Field(default_factory=list)
    output_dir: str = ""
    auto_export: bool = False


class SmartPreviewCutPayload(SmartCutPayload):
    preview_id: str = ""
    selected_indices: list[int] = Field(default_factory=list)
    order: list[int] = Field(default_factory=list)
    selected_segments: dict[str, list[int]] = Field(default_factory=dict)


class MixPreviewCutPayload(MixPayload):
    preview_id: str = ""
    selected_indices: list[int] = Field(default_factory=list)
    order: list[int] = Field(default_factory=list)
    selected_segments: dict[str, list[int]] = Field(default_factory=dict)


class PreviewSelectionPayload(BaseModel):
    preview_id: str = ""
    scope: str = "smart"
    order: list[int] = Field(default_factory=list)
    selected_indices: list[int] = Field(default_factory=list)
    selected_segments: dict[str, list[int]] = Field(default_factory=dict)
    updated_at: float = 0


class SmartPreviewClipPayload(BaseModel):
    preview_id: str = ""
    clip_index: int = Field(default=0, ge=0)
    scope: str = "smart"
    order: list[int] = Field(default_factory=list)
    selected_indices: list[int] = Field(default_factory=list)
    selected_segments: dict[str, list[int]] = Field(default_factory=dict)
    updated_at: float = 0


class AiFeedbackImportPayload(BaseModel):
    path: str = ""


class AiFeedbackDeletePayload(BaseModel):
    role: str = ""
    text: str = ""


class ProductScanPayload(BaseModel):
    excel_path: str = ""
    video_paths: list[str] = Field(default_factory=list)
    output_dir: str = ""
    advance_seconds: int = Field(default=0, ge=0, le=600)
    video_start_offset: str = ""
    live_start_time: str = ""


class VideoSplitPayload(BaseModel):
    video_paths: list[str] = Field(default_factory=list)
    output_dir: str = ""
    mode: str = "count"
    segment_count: int = Field(default=2, ge=1, le=500)
    segment_seconds: float = Field(default=60.0, ge=0.1, le=86400)
    overrides: dict[str, int] = Field(default_factory=dict)


class DedupPayload(BaseModel):
    video_path: str = ""
    video_paths: list[str] = Field(default_factory=list)
    output_dir: str = ""
    dedup_preset: str = "medium"
    pip_enabled: bool = False
    pip_path: str = ""
    pip_folder: str = ""
    pip_size: float = Field(default=0.15, ge=0.03, le=1.0)
    pip_opacity: float = Field(default=0.03, ge=0.01, le=1.0)
    pip_pos: str = "鍙充笅"
    video: dict[str, Any] = Field(default_factory=dict)
    pip: dict[str, Any] = Field(default_factory=dict)
    audio: dict[str, Any] = Field(default_factory=dict)


class LiveRecPayload(BaseModel):
    save_dir: str = ""
    segment: str = "不限"
    check_interval: int = Field(default=30, ge=5, le=300)
    min_stream_quality: str = ""
    room_name: str = ""
    room_url: str = ""
    platform: str = "自定义RTMP"
    product_split_enabled: bool = False
    product_auto_cut: bool = False
    product_default_minutes: float = Field(default=10.0, ge=1.0, le=60.0)
    product_min_minutes: float = Field(default=3.0, ge=0.5, le=60.0)
    product_max_minutes: float = Field(default=15.0, ge=1.0, le=120.0)
    product_naming_mode: str = "product_id"
    product_switch_confirm_seconds: int = Field(default=8, ge=0, le=120)
    product_head_seconds: int = Field(default=10, ge=0, le=300)
    product_tail_seconds: int = Field(default=20, ge=0, le=300)


TOOLS_DIR = REPO_ROOT / "tools"


def _new_task(scope: str, title: str) -> str:
    task_id = f"{scope}-{int(time.time())}-{os.urandom(3).hex()}"
    with _TASK_LOCK:
        _TASK_CANCEL_EVENTS[task_id] = threading.Event()
        _TASKS[task_id] = {
            "id": task_id,
            "scope": scope,
            "title": title,
            "status": "queued",
            "progress": 0,
            "message": "排队中",
            "started_at": None,
            "finished_at": None,
            "error": "",
            "batch_total": 0,
            "batch_done": 0,
            "batch_current": 0,
            "batch_failed": 0,
            "batch_label": "",
        }
    return task_id


def _task_cancel_event(task_id: str) -> threading.Event:
    with _TASK_LOCK:
        event = _TASK_CANCEL_EVENTS.get(task_id)
        if event is None:
            event = threading.Event()
            _TASK_CANCEL_EVENTS[task_id] = event
        return event


def _ensure_scope_idle(scope: str, current_title: str = "任务") -> None:
    with _TASK_LOCK:
        for task in _TASKS.values():
            if task.get("scope") == scope and task.get("status") in {"queued", "running"}:
                title = task.get("title") or scope
                raise HTTPException(
                    status_code=409,
                    detail=f"{title}正在运行，请等待完成，或先点击停止后再启动{current_title}。",
                )


def _set_task(task_id: str, **updates: Any) -> None:
    completed_snapshot: dict[str, Any] | None = None
    with _TASK_LOCK:
        if task_id in _TASKS:
            current = _TASKS[task_id]
            next_status = updates.get("status")
            if current.get("status") == "cancelled" and next_status in {"running", "completed", "failed"}:
                return
            if next_status == "running" and "progress" not in updates:
                updates["progress"] = max(float(current.get("progress") or 0), 5)
            if next_status == "completed":
                updates.setdefault("progress", 100)
                updates.setdefault("message", "已完成")
            if next_status == "failed":
                updates.setdefault("progress", 100)
                updates.setdefault("message", "失败")
            if next_status == "cancelled":
                updates.setdefault("progress", 100)
                updates.setdefault("message", "已停止")
            if updates.get("message"):
                parsed = _task_batch_updates_from_message(str(updates.get("message") or ""))
                if parsed:
                    parsed.update(updates)
                    updates = parsed
            _TASKS[task_id].update(updates)
            if _TASKS[task_id].get("status") == "completed":
                completed_snapshot = dict(_TASKS[task_id])
    if completed_snapshot:
        _record_output_history(completed_snapshot)


_BATCH_PROGRESS_RE = re.compile(
    r"(跳过失败|完成扫描|完成导出|快速分割|处理|完成|扫描|导出)\s*(\d+)\s*/\s*(\d+)(?:\s*[:：]\s*(.+))?"
)
_BATCH_SUCCESS_RE = re.compile(r"成功\s*(\d+)\s*/\s*(\d+)")


def _task_batch_updates_from_message(message: str) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    if not text:
        return {}

    success = _BATCH_SUCCESS_RE.search(text)
    if success:
        done = int(success.group(1))
        total = int(success.group(2))
        if total > 1:
            return {
                "batch_total": total,
                "batch_done": max(0, min(total, done)),
                "batch_current": 0,
            }

    match = _BATCH_PROGRESS_RE.search(text)
    if not match:
        return {}

    action = match.group(1)
    current = int(match.group(2))
    total = int(match.group(3))
    if total <= 1:
        return {}

    safe_current = max(1, min(total, current))
    is_current_step = action in {"处理", "扫描", "导出", "快速分割"}
    label = (match.group(4) or "").strip()
    updates: dict[str, Any] = {
        "batch_total": total,
        "batch_done": safe_current - 1 if is_current_step else safe_current,
        "batch_current": safe_current if is_current_step else 0,
    }
    if label:
        updates["batch_label"] = label[:120]
    return updates


_LOG_PROGRESS_SIGNAL_RE = re.compile(r"\[PROGRESS\]\s*(\d+(?:\.\d+)?)", re.I)
_CUT_PROGRESS_RE = re.compile(r"(?:切割|Cut)\s*\[(\d+)\s*/\s*(\d+)\]", re.I)


_TASK_PROGRESS_RULES: tuple[tuple[float, str, tuple[str, ...]], ...] = (
    (12, "准备素材", ("任务已启动", "开始处理", "目标时长", "读取", "上传")),
    (22, "标准化素材", ("TS", "标准化", "normalized", "remux", "转码", "CFR", "genpts")),
    (36, "识别字幕", ("语音识别中", "启动本地语音识别", "云端语音识别", "ASR 识别", "ASR成功", "ASR 成功")),
    (56, "AI 选片", ("AI 智能选片", "AI 选片", "候选", "评分", "片单", "最终片单", "预览列表")),
    (64, "分析画面", ("检测到源视频", "输出分辨率", "SmartCrop: 检测", "SmartCrop: 应用", "SmartCrop: cover")),
    (72, "裁剪片段", ("开始切割", "切割片段中", "裁剪", "剪辑", "Cut [")),
    (84, "动态画面", ("KenBurns", "Ken Burns", "动态", "缩放")),
    (86, "拼接合并", ("拼接", "Concatenating", "Concat done", "Concat copy")),
    (90, "去重处理", ("整体去重", "去重步骤", "去重效果", "去重完成", "dedup")),
    (94, "字幕处理", ("字幕处理中", "字幕时间轴", "字幕烧录", "drawtext", "DeepSeek修复", "画中画")),
    (97, "收尾校验", ("成品真实时长", "切割报告", "生成成功", "输出路径", "大小:", "片段:")),
)


def _label_for_internal_progress(percent: float) -> str:
    if percent >= 98:
        return "收尾校验"
    if percent >= 90:
        return "字幕烧录"
    if percent >= 78:
        return "字幕处理"
    if percent >= 65:
        return "提取音频"
    if percent >= 60:
        return "去重处理"
    if percent >= 50:
        return "拼接合并"
    if percent >= 30:
        return "裁剪片段"
    return "剪辑处理中"


def _set_task_progress(task_id: str, progress: float, message: str | None = None) -> None:
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if not task or task.get("status") == "cancelled":
            return
        current = float(task.get("progress") or 0)
        incoming = max(0, min(99, float(progress)))
        task["progress"] = max(current, incoming)
        if message and incoming >= current - 0.5:
            task["message"] = message
            task.update(_task_batch_updates_from_message(message))


def _task_progress_from_log(raw: str) -> tuple[float, str] | None:
    text = str(raw or "")
    if not text:
        return None
    signal = _LOG_PROGRESS_SIGNAL_RE.search(text)
    if signal:
        value = float(signal.group(1))
        percent = value * 100 if value <= 1 else value
        percent = max(0, min(99, percent))
        return percent, _label_for_internal_progress(percent)

    cut_match = _CUT_PROGRESS_RE.search(text)
    if cut_match:
        current = max(1, int(cut_match.group(1)))
        total = max(1, int(cut_match.group(2)))
        percent = 72 + (min(current, total) - 1) / total * 10
        return min(82, percent), f"裁剪片段 {min(current, total)}/{total}"

    for progress, label, tokens in _TASK_PROGRESS_RULES:
        if any(token in text for token in tokens):
            return progress, label
    return None


def _is_fatal_ai_log(message: str) -> bool:
    text = str(message or "")
    lower = text.lower()
    if "ai" not in lower and "api" not in lower and "deepseek" not in lower:
        return False
    fatal_tokens = (
        "http 400",
        "http400",
        "http 401",
        "http401",
        "http 402",
        "http402",
        "http 403",
        "http403",
        "http 404",
        "http404",
        "http 413",
        "http413",
        "余额不足",
        "api key 无效",
        "api key无效",
        "接口地址错误",
        "base url",
    )
    return any(token in lower or token in text for token in fatal_tokens)


def _task_log_fn(task_id: str, scope: str, base: float = 10, span: float = 80):
    def _log(message: str) -> None:
        if _is_fatal_ai_log(message):
            emit_log("error", message, scope)
            return
        emit_log("info", message, scope)
        stage = _task_progress_from_log(message)
        if stage:
            progress, label = stage
            scaled = base + (progress / 100.0) * span
            _set_task_progress(task_id, scaled, label)

    return _log


def _is_task_cancelled(task_id: str) -> bool:
    with _TASK_LOCK:
        return task_id in _CANCELLED_TASKS or _TASKS.get(task_id, {}).get("status") == "cancelled"


def _cancel_task(task_id: str) -> int:
    task_id = str(task_id or "").strip()
    if not task_id:
        return 0
    scope = ""
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if not task or task.get("status") not in {"queued", "running"}:
            return 0
        scope = str(task.get("scope") or "")
        _CANCELLED_TASKS.add(task_id)
        event = _TASK_CANCEL_EVENTS.get(task_id)
        if event:
            event.set()
        task.update(
            status="cancelled",
            progress=100,
            message="已停止",
            finished_at=time.time(),
            error="用户已停止",
        )
    if scope == "live-rec":
        _stop_live_task_process(task_id)
    emit_log("warning", "已停止当前任务。", scope or "system")
    return 1


def _cancel_scope(scope: str) -> int:
    if not scope:
        return 0
    stopped = 0
    cancelled_ids: list[str] = []
    cancel_events: list[threading.Event] = []
    with _TASK_LOCK:
        for task_id, task in _TASKS.items():
            if task.get("scope") != scope or task.get("status") not in {"queued", "running"}:
                continue
            _CANCELLED_TASKS.add(task_id)
            event = _TASK_CANCEL_EVENTS.get(task_id)
            if event:
                event.set()
                cancel_events.append(event)
            cancelled_ids.append(task_id)
            task.update(
                status="cancelled",
                progress=100,
                message="已停止",
                finished_at=time.time(),
                error="用户已停止",
            )
            stopped += 1
    killed = 0
    if cancelled_ids:
        try:
            import cutter_logic as cutter_mod

            cancel_processes = getattr(cutter_mod, "cancel_active_processes", None)
            if callable(cancel_processes):
                for event in cancel_events:
                    killed += int(cancel_processes(event) or 0)
        except Exception:
            pass
    if scope == "live-rec":
        stopped += _stop_live_all()
    emit_log("warning", f"已停止任务，并尝试终止当前处理进程。{f'已终止 {killed} 个子进程。' if killed else ''}", scope)
    return stopped


def _ensure_feature_access(feature_name: str) -> None:
    try:
        from license_guard import get_feature_access

        access = get_feature_access(refresh=False)
        if access.get("ok"):
            return
        raise RuntimeError(access.get("reason") or "试用次数已用完，请激活后继续使用。")
    except Exception as exc:
        raise RuntimeError(f"{feature_name} 权限检查失败：{exc}") from exc


def _consume_trial(feature_name: str, units: int = 1, scope: str = "system") -> None:
    try:
        from license_guard import consume_trial_after_success

        remaining = consume_trial_after_success(feature_name, units=units, root=None)
        if remaining is not None:
            emit_log("warning", f"{feature_name} 完成，试用剩余 {max(remaining, 0)} 次。", scope)
    except Exception as exc:
        emit_log("warning", f"试用次数扣减异常：{exc}", scope)


def _clean_path(value: str) -> Path:
    return Path((value or "").strip().strip('"'))


def _existing_paths(values: list[str], label: str) -> list[Path]:
    paths = [_clean_path(v) for v in values if (v or "").strip()]
    if not paths:
        raise ValueError(f"请至少填写一个{label}路径。")
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError("文件不存在：" + ", ".join(missing[:3]))
    return paths


def _ffmpeg_cmd() -> str:
    try:
        from platform_config import FFMPEG_CMD

        if FFMPEG_CMD and os.path.exists(FFMPEG_CMD):
            return FFMPEG_CMD
    except Exception:
        pass
    return "ffmpeg"


def _ffprobe_cmd() -> str:
    ffmpeg = _ffmpeg_cmd()
    try:
        ffmpeg_path = Path(ffmpeg)
        if ffmpeg_path.name.lower() in {"ffmpeg.exe", "ffmpeg"}:
            probe_name = "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
            candidate = ffmpeg_path.with_name(probe_name)
            if candidate.exists():
                return str(candidate)
    except Exception:
        pass
    if ffmpeg.lower().endswith("ffmpeg.exe"):
        return ffmpeg[:-10] + "ffprobe.exe"
    if ffmpeg.lower().endswith("ffmpeg"):
        return ffmpeg[:-6] + "ffprobe"
    return "ffprobe"


def _probe_video_info(path_value: str) -> dict[str, Any]:
    raw_path = (path_value or "").strip().strip('"')
    info: dict[str, Any] = {
        "path": raw_path,
        "name": Path(raw_path).name if raw_path else "",
        "exists": False,
        "valid": False,
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "resolution": "",
        "fps": 0.0,
        "has_audio": False,
        "message": "",
    }
    if not raw_path:
        info["message"] = "路径为空"
        return info
    path = Path(raw_path)
    if not path.exists():
        info["message"] = "文件不存在"
        return info
    info["exists"] = True
    try:
        stat = path.stat()
        key = f"{str(path.resolve()).lower()}|{round(stat.st_mtime, 3)}|{stat.st_size}"
    except Exception:
        key = raw_path.lower()
    cached = _VIDEO_INFO_CACHE.get(key)
    if cached:
        return dict(cached)
    try:
        proc = subprocess.run(
            [_ffprobe_cmd(), "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            info["message"] = "无法读取视频信息"
        else:
            data = json.loads((proc.stdout or b"{}").decode("utf-8", errors="replace") or "{}")
            fmt = data.get("format") or {}
            streams = data.get("streams") or []
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
            if video_stream:
                info["width"] = int(video_stream.get("width") or 0)
                info["height"] = int(video_stream.get("height") or 0)
                if info["width"] and info["height"]:
                    info["resolution"] = f"{info['width']}x{info['height']}"
                rate = str(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "")
                try:
                    if "/" in rate:
                        num, den = rate.split("/", 1)
                        info["fps"] = round(float(num) / max(1.0, float(den)), 3)
                    elif rate:
                        info["fps"] = round(float(rate), 3)
                except Exception:
                    info["fps"] = 0.0
            info["has_audio"] = bool(audio_stream)
            try:
                info["duration"] = round(float(fmt.get("duration") or 0), 3)
            except Exception:
                info["duration"] = 0.0
            info["valid"] = bool(video_stream and info["duration"] > 0)
            info["message"] = "OK" if info["valid"] else "未检测到有效视频流"
    except Exception as exc:
        info["message"] = f"检测失败：{exc}"
    _VIDEO_INFO_CACHE[key] = dict(info)
    return info


def _probe_av_start_gap(path: Path) -> tuple[float, float, float, bool]:
    try:
        proc = subprocess.run(
            [
                _ffprobe_cmd(),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,start_time,duration",
                "-of",
                "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            return 0.0, 0.0, 0.0, False
        data = json.loads((proc.stdout or b"{}").decode("utf-8", errors="replace") or "{}")
        streams = data.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if not video:
            return 0.0, 0.0, 0.0, bool(audio)
        video_start = float(video.get("start_time") or 0.0)
        audio_start = float(audio.get("start_time") or 0.0) if audio else 0.0
        return video_start, audio_start, abs(video_start - audio_start), bool(audio)
    except Exception:
        return 0.0, 0.0, 0.0, False


def _transcode_live_recording_sync(source: Path, target: Path, scope: str, name: str, reason: str) -> bool:
    tmp_target = target.with_name(f"{target.stem}.sync_tmp{target.suffix}")
    cmd = [
        _ffmpeg_cmd(),
        "-hide_banner",
        "-y",
        "-fflags",
        "+genpts+igndts",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        "setpts=PTS-STARTPTS,fps=30,format=yuv420p",
        "-r",
        "30",
        "-vsync",
        "cfr",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-af",
        "aresample=async=1:first_pts=0",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-shortest",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(tmp_target),
    ]
    log_path = target.with_suffix(".sync.log")
    try:
        emit_log("info", f"{name}: 检测到直播录制时间戳偏移（{reason}），正在生成同步安全版 MP4。", scope)
        with log_path.open("wb") as log_handle:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=log_handle,
                timeout=3600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if proc.returncode == 0 and tmp_target.exists() and tmp_target.stat().st_size > 1000:
            os.replace(tmp_target, target)
            _VIDEO_INFO_CACHE.clear()
            return True
    except Exception as exc:
        emit_log("warning", f"{name}: 同步安全版 MP4 生成异常：{exc}", scope)
    try:
        if tmp_target.exists():
            tmp_target.unlink()
    except Exception:
        pass
    return False


def _clean_forbidden_title_text(name: str, fallback: str = "output") -> str:
    try:
        from ai_clipper import load_keywords
        from config import sanitize_forbidden_title

        words = load_keywords().get("forbidden_phrases", [])
        return sanitize_forbidden_title(name, extra_phrases=words, fallback=fallback)
    except Exception:
        return str(name or fallback)


def _safe_stem(name: str) -> str:
    cleaned = _clean_forbidden_title_text(name, fallback="output")
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in cleaned).strip()[:48] or "output"


def _mix_output_path(out_dir: Path, first_video: Path) -> Path:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", first_video.stem or "素材").strip(" ._")
    stem = stem[:80] or "素材"
    return out_dir / f"{stem}_mix_{_stamp_name()}.mp4"


def _default_output_dir(video: Path, explicit: str, folder: str = "output") -> Path:
    if explicit.strip():
        out = _clean_path(explicit)
    else:
        out = video.parent / folder
    out.mkdir(parents=True, exist_ok=True)
    return out


def _collect_smart_cut_outputs(out_dir: Path, video: Path, started_at: float, explicit_output: Path | None, result: Any) -> list[str]:
    outputs: list[Path] = []
    if explicit_output and explicit_output.exists():
        outputs.append(explicit_output)

    try:
        expected_versions = int(result.get("版本数") or result.get("versions") or 0) if isinstance(result, dict) else 0
    except Exception:
        expected_versions = 0
    if expected_versions > 1:
        prefix = f"{video.stem}_切片_"
        for candidate in out_dir.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() != ".mp4":
                continue
            if not candidate.name.startswith(prefix) or "_v" not in candidate.stem:
                continue
            try:
                if candidate.stat().st_mtime < started_at - 5:
                    continue
            except Exception:
                continue
            outputs.append(candidate)

    unique: list[Path] = []
    seen: set[str] = set()
    for item in outputs:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0)
    return [str(path) for path in unique]


def _video_split_mode(payload: VideoSplitPayload) -> str:
    return "duration" if str(payload.mode or "").strip().lower() in {"duration", "seconds", "time"} else "count"


def _clamp_segment_count(value: Any) -> int:
    try:
        count = int(round(float(value)))
    except Exception:
        count = 2
    return max(1, min(500, count))


def _video_split_paths(payload: VideoSplitPayload) -> list[Path]:
    return _existing_paths(payload.video_paths, "视频")


def _video_split_override_count(video: Path, payload: VideoSplitPayload) -> int:
    count = _clamp_segment_count(payload.segment_count)
    keys = {str(video), video.name, video.stem}
    try:
        keys.add(str(video.resolve()))
    except Exception:
        pass
    normalized = {key.strip().strip('"').lower().replace("/", "\\") for key in keys if key}
    for key, value in (payload.overrides or {}).items():
        norm = str(key or "").strip().strip('"').lower().replace("/", "\\")
        if norm in normalized:
            return _clamp_segment_count(value)
    return count


def _ffmpeg_timecode(seconds: float) -> str:
    value = max(0.0, float(seconds or 0.0))
    whole = int(value)
    ms = int(round((value - whole) * 1000))
    if ms >= 1000:
        whole += 1
        ms -= 1000
    h = whole // 3600
    m = (whole % 3600) // 60
    s = whole % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _video_split_plan(video: Path, payload: VideoSplitPayload, index: int = 1) -> dict[str, Any]:
    info = _probe_video_info(str(video))
    if not info.get("valid"):
        detail = info.get("message") or "无法读取视频信息"
        raise ValueError(f"{video.name} 不可分割：{detail}")

    duration = float(info.get("duration") or 0.0)
    if duration <= 0:
        raise ValueError(f"{video.name} 没有可用时长。")

    mode = _video_split_mode(payload)
    if mode == "duration":
        segment_seconds = max(0.1, float(payload.segment_seconds or 60.0))
        segment_count = max(1, math.ceil(duration / segment_seconds))
        if segment_count > 500:
            raise ValueError(f"{video.name} 将生成 {segment_count} 段，请增大每段秒数。")
        step = segment_seconds
    else:
        segment_count = _video_split_override_count(video, payload)
        step = duration / segment_count

    stem = _safe_stem(video.stem)
    segments: list[dict[str, Any]] = []
    for part_index in range(segment_count):
        start = min(duration, part_index * step)
        if mode == "duration":
            end = duration if part_index == segment_count - 1 else min(duration, start + step)
        else:
            end = duration if part_index == segment_count - 1 else min(duration, (part_index + 1) * step)
        segment_duration = max(0.0, end - start)
        if segment_duration <= 0:
            continue
        segments.append(
            {
                "index": part_index + 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(segment_duration, 3),
                "output_name": f"{stem}_part_{part_index + 1:03d}.mp4",
            }
        )

    return {
        "index": index,
        "path": str(video),
        "name": video.name,
        "duration": round(duration, 3),
        "resolution": info.get("resolution") or "",
        "mode": mode,
        "segment_count": len(segments),
        "segment_seconds": round(float(segment_seconds if mode == "duration" else step), 3),
        "segments": segments,
    }


def _video_split_preview(payload: VideoSplitPayload) -> dict[str, Any]:
    videos = [
        _video_split_plan(video, payload, index)
        for index, video in enumerate(_video_split_paths(payload), start=1)
    ]
    return {
        "ok": True,
        "mode": _video_split_mode(payload),
        "videos": videos,
        "total_segments": sum(len(item.get("segments") or []) for item in videos),
        "total_duration": round(sum(float(item.get("duration") or 0.0) for item in videos), 3),
    }


def _payload_to_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    if hasattr(payload, "dict"):
        return payload.dict()
    return {}


def _preflight_file_list(values: Any, label: str, errors: list[str], min_count: int = 1) -> list[Path]:
    raw_values = values if isinstance(values, list) else []
    paths = [_clean_path(str(v)) for v in raw_values if str(v or "").strip()]
    if len(paths) < min_count:
        errors.append(f"请至少填写 {min_count} 个{label}路径。")
        return []
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        errors.append("文件不存在：" + ", ".join(missing[:3]))
    directories = [str(path) for path in paths if path.exists() and path.is_dir()]
    if directories:
        errors.append(f"{label}路径不能是文件夹：" + ", ".join(directories[:3]))
    return paths


def _dedup_path_values(data: dict[str, Any]) -> list[str]:
    values = data.get("video_paths")
    if isinstance(values, list):
        paths = [str(v or "").strip() for v in values if str(v or "").strip()]
    else:
        paths = []
    single = str(data.get("video_path") or "").strip()
    if single and single not in paths:
        paths.insert(0, single)
    return paths


def _preflight_single_file(value: Any, label: str, errors: list[str], required: bool = True) -> Path | None:
    text = str(value or "").strip()
    if not text:
        if required:
            errors.append(f"请填写{label}路径。")
        return None
    path = _clean_path(text)
    if not path.exists():
        errors.append(f"{label}不存在：{path}")
    elif path.is_dir():
        errors.append(f"{label}不能是文件夹：{path}")
    return path


def _preflight_output_dir(value: Any, warnings: list[str], errors: list[str]) -> None:
    text = str(value or "").strip()
    if not text:
        return
    out_dir = _clean_path(text)
    if out_dir.exists() and not out_dir.is_dir():
        errors.append(f"输出目录不是文件夹：{out_dir}")
        return
    probe_dir = out_dir if out_dir.exists() else out_dir.parent
    if not probe_dir.exists():
        errors.append(f"输出目录的上级不存在：{probe_dir}")
        return
    try:
        probe = probe_dir / f".liveclipper_write_test_{os.getpid()}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception:
        errors.append(f"输出目录不可写：{probe_dir}")


def _preflight_ffmpeg(errors: list[str]) -> None:
    cmd = _ffmpeg_cmd()
    if os.path.exists(cmd) or shutil.which(cmd):
        return
    errors.append("没有找到视频处理程序 FFmpeg，请确认程序完整或重新解压安装包。")


def _preflight_pip(data: dict[str, Any], warnings: list[str], errors: list[str]) -> None:
    if not data.get("pip_enabled"):
        return
    folder = str(data.get("pip_folder") or "").strip()
    path = str(data.get("pip_path") or "").strip()
    if folder:
        try:
            queue = _pip_folder_queue(folder)
            if not queue:
                errors.append("画中画素材文件夹里没有可用视频。")
        except Exception as exc:
            errors.append(str(exc))
        return
    if path and path != "auto":
        _preflight_single_file(path, "画中画素材", errors, required=True)
        return
    if not path:
        warnings.append("画中画已启用但未选择素材，将使用自身缩略图。")


def _preflight_ai_settings(feature: str, warnings: list[str]) -> None:
    if feature not in {"smart-cut", "smart-preview", "smart-from-preview", "mix", "mix-preview", "mix-from-preview", "ai-scan", "ai-scan-export", "ai-scan-export-merge"}:
        return
    settings = _load_settings()
    if not (settings.get("api_key") or "").strip():
        warnings.append("AI API Key 未填写，AI 选片或字幕修复可能不可用。")
    if settings.get("asr_enabled", False):
        missing = [
            name
            for name, key in (("API Key", "volc_api_key"), ("TOS AK", "volc_tos_ak"), ("TOS SK", "volc_tos_sk"), ("Bucket", "volc_bucket"), ("地域", "volc_region"))
            if not str(settings.get(key) or "").strip()
        ]
        if missing:
            warnings.append("云端语音识别配置不完整：" + ", ".join(missing) + "，可能会切换到本地语音识别。")
    else:
        warnings.append("未启用云端语音识别，将使用本地语音识别。")


def _hms(seconds: float) -> str:
    value = max(0, int(round(float(seconds or 0))))
    h = value // 3600
    m = (value % 3600) // 60
    s = value % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _parse_offset_seconds(value: Any) -> float | None:
    text = str(value or "").strip().replace("：", ":")
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("请填写 26:49 或 1:28:13 这样的时间格式")
    try:
        numbers = [int(part.strip()) for part in parts]
    except Exception as exc:
        raise ValueError("请填写 26:49 或 1:28:13 这样的时间格式") from exc
    if len(numbers) == 2:
        minutes, seconds = numbers
        hours = 0
    else:
        hours, minutes, seconds = numbers
    if minutes < 0 or seconds < 0 or seconds >= 60 or (len(numbers) == 3 and minutes >= 60):
        raise ValueError("时间格式不正确，分钟和秒数不能超过 59")
    return float(hours * 3600 + minutes * 60 + seconds)


def _datetime_from_video_name(path: str) -> datetime | None:
    match = re.search(r"(20\d{10}(?:\d{2})?)", Path(str(path)).name)
    if not match:
        return None
    value = match.group(1)
    fmt = "%Y%m%d%H%M%S" if len(value) == 14 else "%Y%m%d%H%M"
    try:
        return datetime.strptime(value, fmt)
    except Exception:
        return None


def _parse_live_start_datetime(value: Any, video_values: list[str] | None = None) -> datetime | None:
    text = str(value or "").strip().replace("：", ":").replace("T", " ")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", text)
    if not match:
        raise ValueError("请填写 17:08 或 2026-03-13 17:08 这样的时间格式")
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("直播开始时间格式不正确")
    base_date = None
    for path in video_values or []:
        dt = _datetime_from_video_name(path)
        if dt:
            base_date = dt.date()
            break
    if base_date is None:
        base_date = datetime.now().date()
    return datetime.combine(base_date, datetime.min.time()).replace(hour=hour, minute=minute, second=second)


def _shift_schedule_offsets(schedule: list[dict[str, Any]], offset_seconds: float) -> None:
    for item in schedule:
        item["start_offset"] = float(item.get("start_offset", 0) or 0) - offset_seconds
        item["end_offset"] = float(item.get("end_offset", 0) or 0) - offset_seconds


def _schedule_range(schedule: list[dict[str, Any]]) -> tuple[float, float]:
    starts = [float(item.get("start_offset", 0) or 0) for item in schedule]
    ends = [float(item.get("end_offset", 0) or 0) for item in schedule]
    return (min(starts), max(ends)) if starts and ends else (0.0, 0.0)


def _schedule_overlap_count(schedule: list[dict[str, Any]], total_seconds: float) -> int:
    total = float(total_seconds or 0)
    count = 0
    for item in schedule:
        start = float(item.get("start_offset", 0) or 0)
        end = float(item.get("end_offset", 0) or 0)
        if min(end, total) - max(start, 0.0) >= 0.5:
            count += 1
    return count


def _preflight_product_schedule(data: dict[str, Any], warnings: list[str], errors: list[str]) -> None:
    excel = str(data.get("excel_path") or "").strip()
    video_values = [str(v or "").strip() for v in (data.get("video_paths") or []) if str(v or "").strip()]
    if not excel or not video_values:
        return
    try:
        requested_video_start = _parse_offset_seconds(data.get("video_start_offset"))
    except ValueError as exc:
        errors.append(f"所选视频起点格式不正确：{exc}")
        return
    try:
        requested_live_start = _parse_live_start_datetime(data.get("live_start_time"), video_values)
    except ValueError as exc:
        errors.append(f"直播开始时间格式不正确：{exc}")
        return
    try:
        from copy import deepcopy
        from schedule_splitter import _parse_video_time, _probe_durations, align_schedule_to_video, read_excel

        schedule, live_start = read_excel(excel)
        if not schedule:
            errors.append("排品表未读取到有效商品时间段，请确认 Excel 格式是否正确。")
            return
        durations = _probe_durations(video_values, ffmpeg_cmd=_ffmpeg_cmd())
        total = sum(float(item or 0) for item in durations)
        if total <= 0:
            warnings.append("未能读取视频总时长，无法提前校验排品表时间范围。")
            return

        if requested_video_start is not None:
            _shift_schedule_offsets(schedule, requested_video_start)
            min_start, max_end = _schedule_range(schedule)
            overlap_count = _schedule_overlap_count(schedule, total)
            if overlap_count:
                skipped = max(0, len(schedule) - overlap_count)
                note = f"，会跳过 {skipped} 条不在所选视频范围内的时段" if skipped else ""
                warnings.append(
                    f"已按所选视频起点 {_hms(requested_video_start)} 定位：可切 {overlap_count} 条时段{note}。"
                )
                return
            errors.append(
                "按所选视频起点定位后，排品表时间仍不在视频范围内："
                f"对齐后 {_hms(min_start)}~{_hms(max_end)}，所选视频总时长 {_hms(total)}。"
            )
            return

        min_start, max_end = _schedule_range(schedule)
        has_video_timestamps = any(_parse_video_time(path) is not None for path in video_values)
        live_start_for_align = requested_live_start or live_start
        if requested_live_start is not None and not has_video_timestamps:
            errors.append("已填写直播开始时间，但所选视频文件名没有可识别时间戳，无法自动定位。")
            return
        if min_start >= -5 and max_end <= total + 5 and not (live_start_for_align and has_video_timestamps):
            return

        aligned = deepcopy(schedule)
        align_schedule_to_video(aligned, video_values, live_start_for_align, ffmpeg_cmd=_ffmpeg_cmd())
        aligned_min, aligned_max = _schedule_range(aligned)
        overlap_count = _schedule_overlap_count(aligned, total)
        if overlap_count:
            skipped = max(0, len(aligned) - overlap_count)
            label = "直播开始时间" if requested_live_start is not None else "视频文件名"
            if skipped:
                warnings.append(
                    f"排品表已按{label}对齐：可切 {overlap_count} 条时段，会跳过 {skipped} 条不在所选视频范围内的时段；"
                    f"对齐后范围 {_hms(aligned_min)}~{_hms(aligned_max)}，视频总时长 {_hms(total)}。"
                )
            elif aligned_min < -5 or aligned_max > total + 5:
                warnings.append(
                    f"排品表已按{label}对齐，边界时段将按视频范围截取："
                    f"对齐后 {_hms(aligned_min)}~{_hms(aligned_max)}，视频总时长 {_hms(total)}。"
                )
            elif aligned_min != min_start or aligned_max != max_end:
                warnings.append(
                    f"排品表时间已按{label}自动对齐：表格范围 {_hms(min_start)}~{_hms(max_end)}，"
                    f"对齐后 {_hms(aligned_min)}~{_hms(aligned_max)}。"
                )
            return

        errors.append(
            "排品表时间不在视频范围内："
            f"表格范围 {_hms(min_start)}~{_hms(max_end)}，"
            f"对齐后 {_hms(aligned_min)}~{_hms(aligned_max)}，"
            f"所选视频总时长 {_hms(total)}。请确认视频是否选全、顺序是否正确，或排品表直播开始时间是否匹配。"
        )
    except Exception as exc:
        warnings.append(f"排品表时间范围预棢失败：{exc}")


def _preflight_checks(feature: str, data: dict[str, Any]) -> dict[str, Any]:
    feature = (feature or "").strip()
    errors: list[str] = []
    warnings: list[str] = []

    needs_ffmpeg = feature in {
        "smart-cut",
        "smart-preview",
        "smart-from-preview",
        "mix",
        "mix-batch",
        "mix-preview",
        "mix-from-preview",
        "ai-scan",
        "ai-scan-export",
        "ai-scan-export-merge",
        "product-scan",
        "video-split",
        "dedup",
        "dedup-check",
        "live-rec-detect",
        "live-rec-monitor",
    }
    if needs_ffmpeg:
        _preflight_ffmpeg(errors)

    if feature in {"smart-cut", "smart-preview"}:
        paths = _preflight_file_list(data.get("video_paths"), "视频", errors, min_count=1)
        if feature == "smart-preview" and len(paths) > 1:
            warnings.append("AI 选片预览当前只预览第 1 个视频；直接开始成片会按列表处理全部视频。")
        _preflight_single_file(data.get("srt_path"), "字幕文件", errors, required=False)
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        _preflight_pip(data, warnings, errors)
        _preflight_ai_settings(feature, warnings)
    elif feature == "smart-from-preview":
        if not str(data.get("preview_id") or "").strip():
            errors.append("请先生成 AI 选片预览。")
        if not data.get("selected_indices"):
            errors.append("请至少保留一个片段。")
        try:
            if int(data.get("versions") or 1) > 1:
                warnings.append("用预览成片会按当前预览结果固定输出 1 个版本；多版本请使用“开始成片”。")
        except Exception:
            pass
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        _preflight_pip(data, warnings, errors)
        _preflight_ai_settings(feature, warnings)
    elif feature in {"mix", "mix-preview"}:
        paths = _preflight_file_list(data.get("video_paths"), "视频", errors, min_count=1)
        if len(paths) == 1:
            warnings.append("混剪只添加了 1 个视频，建议添加至少 2 个素材。")
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        _preflight_pip(data, warnings, errors)
        _preflight_ai_settings(feature, warnings)
    elif feature == "mix-from-preview":
        if not str(data.get("preview_id") or "").strip():
            errors.append("请先生成混剪 AI 选片预览。")
        if not data.get("selected_indices"):
            errors.append("请至少保留一个片段。")
        try:
            if int(data.get("versions") or 1) > 1:
                warnings.append("用预览混剪会按当前预览结果固定输出 1 个版本；多版本请使用“开始混剪”。")
        except Exception:
            pass
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        _preflight_pip(data, warnings, errors)
        _preflight_ai_settings(feature, warnings)
    elif feature == "mix-batch":
        groups = data.get("groups") if isinstance(data.get("groups"), list) else []
        if not groups:
            errors.append("请至少保存 1 个混剪素材组。")
        for index, group in enumerate(groups, start=1):
            group_data = group if isinstance(group, dict) else {}
            paths = _preflight_file_list(group_data.get("video_paths"), f"第 {index} 组视频", errors, min_count=1)
            if len(paths) == 1:
                warnings.append(f"第 {index} 组只添加了 1 个视频，建议至少 2 个素材。")
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        _preflight_pip(data, warnings, errors)
        _preflight_ai_settings(feature, warnings)
    elif feature.startswith("ai-scan"):
        _preflight_file_list(data.get("video_paths"), "视频", errors, min_count=1)
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        _preflight_ai_settings(feature, warnings)
    elif feature == "product-scan-read":
        _preflight_single_file(data.get("excel_path"), "Excel 时间表", errors, required=True)
    elif feature == "product-scan":
        _preflight_single_file(data.get("excel_path"), "Excel 时间表", errors, required=True)
        _preflight_file_list(data.get("video_paths"), "直播视频", errors, min_count=1)
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        if not errors:
            _preflight_product_schedule(data, warnings, errors)
    elif feature == "video-split":
        _preflight_file_list(data.get("video_paths"), "视频", errors, min_count=1)
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        mode = str(data.get("mode") or "count").strip().lower()
        if mode in {"duration", "seconds", "time"}:
            try:
                seconds = float(data.get("segment_seconds") or 0)
            except Exception:
                seconds = 0
            if seconds <= 0:
                errors.append("每段秒数必须大于 0。")
        else:
            try:
                count = int(round(float(data.get("segment_count") or 0)))
            except Exception:
                count = 0
            if count < 1 or count > 500:
                errors.append("分段数量必须在 1 到 500 之间。")
    elif feature in {"dedup", "dedup-check"}:
        _preflight_file_list(_dedup_path_values(data), "视频", errors, min_count=1)
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        if data.get("pip_enabled") or (isinstance(data.get("pip"), dict) and data.get("pip", {}).get("enabled")):
            if data.get("pip_enabled"):
                _preflight_pip(data, warnings, errors)
            else:
                pip = data.get("pip") or {}
                _preflight_single_file(pip.get("path"), "画中画素材", errors, required=True)
    elif feature.startswith("live-rec"):
        if feature == "live-rec-monitor":
            if not str(data.get("room_name") or "").strip():
                errors.append("请填写直播间名称。")
            if not str(data.get("room_url") or "").strip():
                errors.append("请填写直播间地址。")
            _preflight_output_dir(data.get("save_dir"), warnings, errors)
            if data.get("product_split_enabled"):
                try:
                    min_minutes = min(15.0, float(data.get("product_min_minutes") or 3))
                    max_minutes = min(15.0, float(data.get("product_max_minutes") or 15))
                    default_minutes = float(data.get("product_default_minutes") or 10)
                    if min_minutes > max_minutes:
                        errors.append("商品分段的最短分钟不能大于最长分钟。")
                    if default_minutes <= 0:
                        errors.append("单品默认分钟必须大于 0。")
                except Exception:
                    errors.append("商品分段时长参数不正确。")
        elif feature == "live-rec-detect" and not str(data.get("room_url") or "").strip():
            errors.append("请填写直播间地址。")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _raise_preflight_errors(feature: str, payload: Any) -> None:
    result = _preflight_checks(feature, _payload_to_dict(payload))
    if result["errors"]:
        raise HTTPException(status_code=400, detail="启动检查未通过：" + ", ".join(result["errors"]))


def _format_srt_time(seconds: float) -> str:
    value = max(0.0, float(seconds or 0.0))
    whole = int(value)
    ms = int(round((value - whole) * 1000))
    if ms >= 1000:
        whole += 1
        ms -= 1000
    h = whole // 3600
    m = (whole % 3600) // 60
    s = whole % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _segments_to_srt(segments: list[Any]) -> str:
    blocks: list[str] = []
    for index, seg in enumerate(segments, start=1):
        if isinstance(seg, dict):
            start = seg.get("start", 0)
            end = seg.get("end", 0)
            text = str(seg.get("text", "")).strip()
        else:
            start = seg[0] if len(seg) > 0 else 0
            end = seg[1] if len(seg) > 1 else 0
            text = str(seg[2] if len(seg) > 2 else "").strip()
        if not text:
            continue
        start_f = float(start or 0)
        end_f = float(end or 0)
        if end_f <= start_f:
            end_f = start_f + 1.0
        blocks.append(f"{len(blocks) + 1}\n{_format_srt_time(start_f)} --> {_format_srt_time(end_f)}\n{text}\n")
    return "\n".join(blocks)


_PIP_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".ts", ".m4v", ".webm"}


def _pip_folder_queue(folder_value: str) -> list[Path]:
    folder = _clean_path(folder_value)
    if not folder.exists():
        raise FileNotFoundError(f"画中画素材文件夹不存在：{folder}")
    if not folder.is_dir():
        raise ValueError("画中画素材文件夹路径不是文件夹。")
    def _natural_key(path: Path) -> list[Any]:
        return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]

    return sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _PIP_VIDEO_EXTS],
        key=_natural_key,
    )


def _pick_pip_asset(payload: Any, scope: str) -> tuple[str | None, Path | None]:
    if not getattr(payload, "pip_enabled", False):
        return None, None
    pip_folder = (getattr(payload, "pip_folder", "") or "").strip()
    if pip_folder:
        queue = _pip_folder_queue(pip_folder)
        if not queue:
            raise FileNotFoundError("画中画素材文件夹里没有可用视频。")
        picked = queue[0]
        emit_log("info", f"已选择画中画素材：{picked.name}", scope)
        return str(picked), picked
    pip_path = (getattr(payload, "pip_path", "") or "").strip()
    if pip_path:
        return pip_path, None
    emit_log("info", "使用自身缩略图作为画中画素材。", scope)
    return "auto", None


def _archive_used_pip(pip_file: Path | None, scope: str) -> None:
    if not pip_file:
        return
    try:
        if pip_file.exists() and pip_file.is_file():
            used_dir = pip_file.parent / "已使用"
            used_dir.mkdir(parents=True, exist_ok=True)
            target = used_dir / pip_file.name
            if target.exists():
                stamp = time.strftime("%Y%m%d_%H%M%S")
                target = used_dir / f"{pip_file.stem}_{stamp}{pip_file.suffix}"
                counter = 2
                while target.exists():
                    target = used_dir / f"{pip_file.stem}_{stamp}_{counter}{pip_file.suffix}"
                    counter += 1
            shutil.move(str(pip_file), str(target))
            emit_log("success", f"已将用过的画中画素材移入：{target.parent.name}\\{target.name}", scope)
    except Exception as exc:
        emit_log("warning", f"画中画素材已使用，但移动到已使用文件夹失败：{exc}", scope)


def _inspect_pip_pool(folder_value: str) -> dict[str, Any]:
    folder = _clean_path(folder_value)
    info: dict[str, Any] = {
        "folder": str(folder),
        "exists": folder.exists() and folder.is_dir(),
        "remaining": 0,
        "used": 0,
        "empty": True,
        "message": "",
    }
    if not folder_value or not str(folder_value).strip():
        info["message"] = "未选择素材文件夹"
        return info
    if not info["exists"]:
        info["message"] = "素材文件夹不存在"
        return info
    queue = _pip_folder_queue(str(folder))
    used_dir = folder / "已使用"
    used_files: list[Path] = []
    if used_dir.exists() and used_dir.is_dir():
        used_files = [p for p in used_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}]
    info["remaining"] = len(queue)
    info["used"] = len(used_files)
    info["empty"] = len(queue) == 0
    info["message"] = "素材池为空，请补充画中画素材。" if info["empty"] else "OK"
    return info


def _clip_text_focus(clip: Any) -> tuple[str, str, str]:
    try:
        if isinstance(clip, dict):
            clip_type = str(clip.get("clip_type") or clip.get("type") or "product")
            text = str(clip.get("text") or "")
            focus = str(clip.get("focus") or clip.get("reason") or "")
        else:
            clip_type = str(clip[0] if len(clip) > 0 else "product")
            text = str(clip[1] if len(clip) > 1 else "")
            focus = str(clip[6] if len(clip) > 6 else "")
    except Exception:
        return "product", str(clip), ""
    return _repair_mojibake_text(clip_type), _repair_mojibake_text(text), _repair_mojibake_text(focus)


def _preview_effective_clip_text(clip: Any) -> str:
    if isinstance(clip, dict):
        segments = clip.get("segments")
        if isinstance(segments, list) and segments:
            selected = [
                _repair_mojibake_text(seg.get("text") or "").strip()
                for seg in segments
                if isinstance(seg, dict) and seg.get("selected") is not False
            ]
            selected = [text for text in selected if text]
            if selected:
                return " ".join(selected).strip()
        return _repair_mojibake_text(clip.get("text") or "").strip()
    try:
        return _repair_mojibake_text(clip[1] if len(clip) > 1 else "").strip()
    except Exception:
        return _repair_mojibake_text(clip).strip()


def _repair_mojibake_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    markers = ("Ã", "Â", "Ä", "Å", "Æ", "Ç", "È", "É", "Ê", "Ë", "Ì", "Í", "Î", "Ï", "å", "æ", "ç", "è", "é", "鏅", "璇", "鐗", "鍚", "棰", "绱", "浜", "銆", "锛")
    if not any(marker in text for marker in markers):
        return text
    for encoding in ("cp936", "gbk", "cp1252", "latin1"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except Exception:
            continue
        if repaired and _mojibake_score(repaired) < _mojibake_score(text):
            return repaired
    return text


def _mojibake_score(text: str) -> int:
    return sum(1 for ch in str(text or "") if ch in "ÃÂÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞßàáâãäåæçèéêëìíîï鏅璇鐗鍚棰绱浜銆锛")


def _preview_focus_block(clip: Any) -> str:
    _, raw_text, focus = _clip_text_focus(clip)
    text = _preview_effective_clip_text(clip) or raw_text
    has_selected_segments = isinstance(clip, dict) and bool(clip.get("segments"))
    hay = text if has_selected_segments else f"{focus} {text}"
    if any(k in hay for k in ("\u663e\u7626", "\u906e\u8089", "\u85cf\u8089", "\u6536\u8170", "\u663e\u9ad8", "\u6bd4\u4f8b", "\u4fee\u9970", "\u7248\u578b", "\u5ed3\u5f62", "\u526a\u88c1", "\u5bbd\u677e", "\u4fee\u8eab", "\u906e\u80ef", "\u906e\u526f\u4e73", "\u80a9\u578b", "\u62dc\u62dc\u8089")):
        return "\u7248\u578b\u663e\u7626"
    if any(k in hay for k in ("\u9762\u6599", "\u6750\u8d28", "\u624b\u611f", "\u89e6\u611f", "\u5782\u611f", "\u900f\u6c14", "\u4eb2\u80a4", "\u67d4\u8f6f", "\u9488\u7ec7", "\u51b0\u4e1d", "\u771f\u4e1d", "\u68c9\u9ebb", "\u4e0d\u95f7", "\u4e0d\u900f", "\u7af9\u8282\u9ebb")):
        return "\u9762\u6599\u8d28\u611f"
    if any(k in hay for k in ("\u505a\u5de5", "\u5de5\u827a", "\u7ec6\u8282", "\u54c1\u8d28", "\u8d28\u611f", "\u9ad8\u7ea7\u611f", "\u7cbe\u81f4", "\u8d70\u7ebf", "\u523a\u7ee3", "\u857e\u4e1d", "\u91cd\u5de5", "\u52fe\u82b1")):
        return "\u54c1\u8d28\u7ec6\u8282"
    if any(k in hay for k in ("\u989c\u8272", "\u8272\u7cfb", "\u663e\u767d", "\u63d0\u6c14\u8272", "\u590d\u53e4", "\u9ed1\u8272", "\u767d\u8272", "\u5496\u8272", "\u82b1\u8272", "\u649e\u8272", "\u7126\u7cd6")):
        return "\u989c\u8272\u6c1b\u56f4"
    if any(k in hay for k in ("\u573a\u666f", "\u901a\u52e4", "\u7ea6\u4f1a", "\u65e5\u5e38", "\u804c\u573a", "\u51fa\u95e8", "\u5ea6\u5047", "\u653e\u5047", "\u62cd\u7167", "\u51fa\u7247", "\u901b\u8857", "\u65c5\u6e38", "\u642d\u914d", "\u8349\u5e3d", "\u68d2\u7403\u5e3d", "\u53e0\u7a7f", "\u5185\u642d", "\u5916\u7a7f", "\u6210\u5957", "\u5957\u7a7f", "\u6d77\u5c9b", "\u4e91\u5357")):
        return "\u573a\u666f\u642d\u914d"
    return "\u5176\u4ed6"


_PREVIEW_SALES_ROLE_LABELS = {
    "hook": "Hook开头",
    "hook_followup": "承接Hook",
    "direct_effect": "直接效果",
    "proof_detail": "证明细节",
    "scene_crowd": "场景人群",
    "objection_resolver": "顾虑解除",
    "natural_close": "自然收尾",
    "weak_fragment": "弱断句",
    "other": "补充卖点",
}


def _preview_sales_role(clip: Any) -> str:
    clip_type, raw_text, focus = _clip_text_focus(clip)
    text = _preview_effective_clip_text(clip) or raw_text
    ctype = clip_type.lower()
    if "hook" in ctype:
        return "hook"
    if "close" in ctype or ctype in ("cta", "call_to_action", "urgency"):
        return "natural_close"

    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text).lower()
    block = _preview_focus_block(clip)
    weak_start = compact.startswith(("嗯", "啊", "好", "好的", "是的", "对", "然后", "而且", "但是", "不过", "其实", "就是"))
    stripped = str(text or "").strip().rstrip("，,。.!！?？、；;：: ")
    weak_end = stripped.endswith(("然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实", "对不对", "能理解吗", "有没有", "你会觉得", "就感觉", "呢", "吧", "啊", "呀"))
    if (weak_start or weak_end) and len(compact) < 18:
        return "weak_fragment"
    if block in ("版型显瘦", "穿着体验", "口感食欲") or re.search(r"显瘦|遮肉|显高|显腿长|上身|穿上|效果|显白|好吃|爆汁|口感|试吃", compact):
        return "direct_effect"
    if block in ("面料质感", "品质细节", "工艺细节", "新鲜品质", "产地溯源", "规格分量", "发货保鲜") or re.search(r"面料|材质|质感|手感|做工|工艺|细节|品质|新鲜|产地|源头|规格|分量|冷链|包赔", compact):
        return "proof_detail"
    if block in ("场景搭配", "场景吃法", "流行趋势") or re.search(r"通勤|上班|约会|日常|出门|旅游|搭配|出片|小个子|微胖|梨形|苹果型|全家|早餐|办公室|送礼", compact):
        return "scene_crowd"
    if block in ("尺寸长度", "对比优势") or re.search(r"不挑|不用担心|不会|不显|不胖|不勒|不卡|不闷|不透|不起球|遮肚子|胯宽|腿粗|尺码|码数|身高|体重|放心|安心|不踩雷", compact):
        return "objection_resolver"
    if re.search(r"推荐|建议|适合|放心|安心|闭眼|值得|自留|复购|老客", compact):
        return "natural_close"
    return "other"


def _preview_sales_role_label(clip: Any) -> str:
    return _PREVIEW_SALES_ROLE_LABELS.get(_preview_sales_role(clip), "补充卖点")


def _reorder_preview_focus_blocks(clips: list[Any]) -> list[Any]:
    if not clips or len(clips) < 5:
        return clips

    hooks: list[Any] = []
    products: list[Any] = []
    closes: list[Any] = []
    for clip in clips:
        clip_type, _, _ = _clip_text_focus(clip)
        ctype = clip_type.lower()
        if "hook" in ctype:
            hooks.append(clip)
        elif "close" in ctype or ctype == "call_to_action":
            closes.append(clip)
        else:
            products.append(clip)

    if len(products) < 4:
        return clips

    block_order = ["\u7248\u578b\u663e\u7626", "\u9762\u6599\u8d28\u611f", "\u54c1\u8d28\u7ec6\u8282", "\u989c\u8272\u6c1b\u56f4", "\u573a\u666f\u642d\u914d", "\u5176\u4ed6"]
    grouped: dict[str, list[Any]] = {block: [] for block in block_order}
    for clip in products:
        grouped.setdefault(_preview_focus_block(clip), []).append(clip)

    ordered_products: list[Any] = []
    for block in block_order:
        ordered_products.extend(grouped.get(block, []))

    before = [_preview_focus_block(clip) for clip in products]
    after = [_preview_focus_block(clip) for clip in ordered_products]
    if before == after:
        return clips
    return (hooks[:1] if hooks else []) + ordered_products + closes


def _clip_to_tuple(clip: Any, default_source: str = "") -> tuple[Any, ...]:
    if isinstance(clip, dict):
        start = float(clip.get("start") or 0)
        end = float(clip.get("end") or start)
        duration = float(clip.get("duration") or max(0.0, end - start))
        source = str(clip.get("source") or default_source or "")
        return (
            _repair_mojibake_text(clip.get("type") or clip.get("clip_type") or "product"),
            _repair_mojibake_text(clip.get("text") or ""),
            start,
            end,
            float(clip.get("score") or 0),
            duration,
            _repair_mojibake_text(clip.get("focus") or clip.get("reason") or ""),
            source,
        )
    if isinstance(clip, (list, tuple)):
        values = list(clip)
        while len(values) < 7:
            values.append("" if len(values) in (1, 6) else 0)
        values[0] = _repair_mojibake_text(values[0])
        values[1] = _repair_mojibake_text(values[1])
        values[6] = _repair_mojibake_text(values[6])
        if len(values) < 8 and default_source:
            values.append(default_source)
        return tuple(values)
    return ("product", str(clip), 0.0, 0.0, 0.0, 0.0, "", default_source)


def _normalize_preview_final_clips(
    clips: list[Any],
    srt_text: str = "",
    *,
    merge_mode: bool = False,
    default_source: str = "",
    preferred_category: str = "",
    preferred_focus: str = "",
    word_timings: list[dict[str, Any]] | None = None,
    target_duration: int | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Apply the same final-retention cleanup before showing preview rows."""
    original_count = len(clips or [])
    normalized = [_clip_to_tuple(clip, default_source=default_source) for clip in (clips or [])]
    if not normalized:
        return [], {"original_count": 0, "final_count": 0, "auto_removed_count": 0}

    removed_steps: list[dict[str, Any]] = []

    def _step(name: str, fn: Any) -> None:
        nonlocal normalized
        before = len(normalized)
        try:
            normalized = list(fn(normalized) or [])
        except Exception as exc:
            emit_log("warning", f"preview cleanup skipped: {name}: {exc}", "system")
            return
        removed = before - len(normalized)
        if removed > 0:
            removed_steps.append({"name": name, "removed": removed})

    try:
        import ai_clipper as ai_mod

        # Preview must preserve the AI-authored narrative. Only hard safety and
        # word-timed boundary cleanup are allowed here; no semantic deletion,
        # topic replacement, Hook promotion, or role-based reordering.
        if hasattr(ai_mod, "_filter_price_and_cta"):
            _step("forbidden_price_filter", lambda items: ai_mod._filter_price_and_cta(items, None))
        if srt_text and word_timings and hasattr(ai_mod, "_trim_filler_start"):
            _step(
                "word_boundary_trim",
                lambda items: ai_mod._trim_filler_start(
                    items,
                    srt_text,
                    None,
                    word_timings=word_timings,
                ),
            )
        if hasattr(ai_mod, "_filter_price_and_cta"):
            _step("final_forbidden_price_filter", lambda items: ai_mod._filter_price_and_cta(items, None))
    except Exception as exc:
        emit_log("warning", f"preview cleanup failed: {exc}", "system")

    auto_removed = max(0, original_count - len(normalized))
    return normalized, {
        "original_count": original_count,
        "final_count": len(normalized),
        "auto_removed_count": auto_removed,
        "auto_removed_steps": removed_steps,
    }


def _preview_repeat_key(text: str) -> str:
    text = _repair_mojibake_text(text).lower()
    text = re.sub(r"\[[vV]\d+\]\s*", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    noise = ("这个", "然后", "就是", "的话", "真的", "一个", "我们", "你们", "可以", "也是", "它是")
    for word in noise:
        text = text.replace(word, "")
    return text


def _preview_similarity(a: str, b: str) -> float:
    import difflib

    a_key = _preview_repeat_key(a)
    b_key = _preview_repeat_key(b)
    if not a_key or not b_key:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a_key, b_key).ratio()
    shorter, longer = sorted((a_key, b_key), key=len)
    if len(shorter) >= 8 and shorter in longer:
        ratio = max(ratio, 0.86)
    return ratio


def _annotate_preview_manual_repeats(public_clips: list[dict[str, Any]]) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    for i, left in enumerate(public_clips):
        left_text = str(left.get("text") or "")
        for j in range(i + 1, len(public_clips)):
            right = public_clips[j]
            score = _preview_similarity(left_text, str(right.get("text") or ""))
            if score < 0.78:
                continue
            level = "high" if score >= 0.9 else "near"
            reason = "高度相似，建议只保留一个" if level == "high" else "疑似重复，建议人工确认"
            groups.append({
                "level": level,
                "score": round(score, 3),
                "indices": [left.get("index"), right.get("index")],
                "reason": reason,
            })
            for clip in (left, right):
                marker = {
                    "level": level,
                    "score": round(score, 3),
                    "with": right.get("index") if clip is left else left.get("index"),
                    "reason": reason,
                }
                clip.setdefault("manual_repeat_checks", []).append(marker)
    return {
        "manual_check_count": len(groups),
        "manual_check_groups": groups[:12],
    }


def _clip_public(index: int, clip: Any) -> dict[str, Any]:
    try:
        if isinstance(clip, dict):
            clip_type = _repair_mojibake_text(clip.get("clip_type") or clip.get("type") or "product")
            text = _repair_mojibake_text(clip.get("text") or "").strip()
            start = float(clip.get("start") or 0)
            end = float(clip.get("end") or start)
            score = float(clip.get("score") or 0)
            duration = float(clip.get("duration") or max(0, end - start))
            focus = _repair_mojibake_text(clip.get("focus") or clip.get("reason") or "").strip()
            source = str(clip.get("source") or "")
        else:
            clip_type = _repair_mojibake_text(clip[0] if len(clip) > 0 else "product")
            text = _repair_mojibake_text(clip[1] if len(clip) > 1 else "").strip()
            start = float(clip[2] if len(clip) > 2 else 0)
            end = float(clip[3] if len(clip) > 3 else start)
            score = float(clip[4] if len(clip) > 4 else 0)
            duration = float(clip[5] if len(clip) > 5 else max(0, end - start))
            focus = _repair_mojibake_text(clip[6] if len(clip) > 6 else "").strip()
            raw_source = clip[7] if len(clip) > 7 else ""
            source = "" if isinstance(raw_source, dict) else str(raw_source).strip()
    except Exception:
        clip_type, text, start, end, score, duration, focus, source = "product", str(clip), 0.0, 0.0, 0.0, 0.0, "", ""
    if end < start:
        end = start
    if duration <= 0:
        duration = max(0.0, end - start)
    sales_role = _preview_sales_role(clip)
    return {
        "index": index,
        "clip_type": clip_type,
        "text": text,
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(duration, 3),
        "score": round(score, 2),
        "focus": focus,
        "focus_block": _preview_focus_block(clip),
        "sales_role": sales_role,
        "sales_role_label": _PREVIEW_SALES_ROLE_LABELS.get(sales_role, "补充卖点"),
        "source": source,
        "source_name": Path(source).name if source else "",
    }


def _strip_preview_source_marker(text: Any) -> str:
    return re.sub(r"\[[vV]\d+\]\s*", "", _repair_mojibake_text(text)).strip()


def _preview_text_for_marker(text: Any, marker: str) -> str:
    value = _repair_mojibake_text(text).strip()
    marker = str(marker or "").strip().upper()
    if not value or not marker:
        return _strip_preview_source_marker(value)
    matches = list(re.finditer(r"\[([vV]\d+)\]\s*", value))
    if not matches:
        return _strip_preview_source_marker(value)
    pieces: list[str] = []
    for index, match in enumerate(matches):
        current = str(match.group(1) or "").upper()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        if current == marker:
            piece = value[start:end].strip()
            if piece:
                pieces.append(piece)
    return " ".join(pieces).strip() or _strip_preview_source_marker(value)


def _preview_text_marker_and_body(text: Any) -> tuple[str, str]:
    value = _repair_mojibake_text(text).strip()
    match = re.match(r"^(\[[vV]\d+\]\s*)(.*)$", value)
    if not match:
        return "", value
    return match.group(1), match.group(2).strip()


def _preview_compact_text(text: Any) -> str:
    value = unicodedata.normalize("NFKC", _strip_preview_source_marker(text))
    for old, new in (("鏈接", "链接"), ("連結", "链接"), ("连结", "链接"), ("連接", "链接"), ("價", "价")):
        value = value.replace(old, new)
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", value)


def _preview_filler_words() -> set[str]:
    fallback = {
        "是的", "对", "对的", "好的", "好", "嗯", "嗯嗯", "啊", "哦", "噢",
        "呃", "额", "好吧", "对吧", "是吧", "没错", "可以", "行",
        "呀对不对", "对不对", "是不是这种感觉", "来准备好啊", "准备好啊",
    }
    try:
        data = _load_effective_keyword_config()
        words = data.get("filler_words", [])
    except Exception:
        words = []
    normalized = {
        re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(word or "").strip())
        for word in words
    }
    return {word for word in normalized if word} | fallback


def _preview_is_pure_filler(text: Any) -> bool:
    compact = _preview_compact_text(text)
    if not compact:
        return True
    if compact in _preview_filler_words():
        return True
    if compact in {"呀对不对", "对不对", "是不是这种感觉", "能理解吗", "有没有发现"}:
        return True
    if len(compact) <= 4 and compact in {"白开水", "纯白色"}:
        return True
    filler_chars = set("对嗯啊哦噢呃额哈呀呢嘛啦哇好是的没错可以行")
    return len(compact) <= 4 and set(compact) <= filler_chars


def _preview_forbidden_words() -> list[str]:
    try:
        data = _load_effective_keyword_config()
        words = data.get("forbidden_phrases", [])
    except Exception:
        words = []
    result = []
    for word in words:
        value = str(word or "").strip()
        if value:
            result.append(value)
    return result


def _preview_segment_block_reason(text: Any) -> str:
    clean = _strip_preview_source_marker(text)
    compact = _preview_compact_text(clean)
    if not compact:
        return ""
    try:
        for word in _preview_forbidden_words():
            word_compact = _preview_compact_text(word)
            if word and (word in clean or (word_compact and word_compact in compact)):
                return f"违禁词：{word}"
    except Exception:
        pass
    price_patterns = [
        r"\d{2,4}\s*[元块]",
        r"[到拿]手价?\s*\d",
        r"原价|现价|价格|秒杀价|福利价|破价|到手价|特价|优惠|折扣|领券|优惠券|消费券|凑单",
        r"321|三二一|正码正拍|正码|正拍|卡码|往大拍|小黄车|购物车|链接|连结|連結|上车|下单|去拍|赶紧拍",
    ]
    if any(re.search(pattern, clean) or re.search(pattern, compact) for pattern in price_patterns):
        return "价格或行动引导"
    try:
        import ai_clipper as ai_mod

        content_risks = ai_mod._content_safety_pattern_matches(clean)
        if content_risks:
            return "内容安全：" + "、".join(content_risks[:2])
        if ai_mod._is_backstage_instruction(clean):
            return "直播现场调度"
    except Exception:
        pass
    return ""


def _preview_has_forbidden_or_price(text: Any) -> bool:
    return bool(_preview_segment_block_reason(text))


def _preview_lock_unsafe_segments(public_clips: list[dict[str, Any]]) -> int:
    """Keep hard-blocked subtitle units out of preview drafts and final export."""
    locked = 0
    for clip in public_clips or []:
        for segment in list(clip.get("segments") or []):
            if not isinstance(segment, dict):
                continue
            reason = _preview_segment_block_reason(segment.get("text") or "")
            if not reason:
                continue
            if segment.get("selected") is not False or not segment.get("selection_locked"):
                locked += 1
            segment["selected"] = False
            segment["selection_locked"] = True
            segment["blocked_reason"] = reason
    return locked


def _preview_context_fragment_reason(text: Any) -> str:
    compact = _preview_compact_text(text)
    if compact.startswith(("就像你们说", "你们说的", "照你们说", "大家都说")):
        return "依赖前文的转述句"
    if compact.startswith(("还是这种", "这种类型", "这个类型", "也可以", "别说了")):
        return "缺少独立卖点的承接句"
    action_words = ("是", "有", "做", "穿", "显", "遮", "不", "能", "会", "可", "给", "让", "买", "搭", "选", "用", "看", "拉", "摸")
    if len(compact) <= 6 and not any(word in compact for word in action_words):
        return "短碎句，缺少独立卖点"
    return ""


def _preview_trim_context_prefix(text: Any) -> str:
    marker, body = _preview_text_marker_and_body(text)
    for prefix in ("就像你们说的", "你们说的", "照你们说", "大家都说"):
        if not body.startswith(prefix):
            continue
        trimmed = body[len(prefix):].lstrip(" ，,。.!！?？、")
        compact = _preview_compact_text(trimmed)
        if len(compact) >= 8 and any(word in compact for word in ("穿", "上身", "搭", "出门", "度假", "通勤", "显", "不")):
            return f"{marker}{trimmed}" if marker else trimmed
    return str(text or "").strip()


def _preview_unselect_context_fragments(public_clips: list[dict[str, Any]]) -> int:
    removed = 0
    for clip in public_clips or []:
        for segment in list(clip.get("segments") or []):
            if not isinstance(segment, dict) or segment.get("selected") is False:
                continue
            trimmed = _preview_trim_context_prefix(segment.get("text") or "")
            if trimmed != str(segment.get("text") or "").strip():
                segment["text"] = trimmed
                segment["trimmed_prefix"] = "移除依赖上文的开头"
            reason = _preview_context_fragment_reason(segment.get("text") or "")
            if not reason:
                continue
            segment["selected"] = False
            segment["auto_unselected_reason"] = reason
            removed += 1
    return removed


def _preview_selected_text(clip: dict[str, Any]) -> str:
    return " ".join(
        str(segment.get("text") or "").strip()
        for segment in list(clip.get("segments") or [])
        if isinstance(segment, dict) and segment.get("selected") is not False
    ).strip()


def _preview_subtopic_signature(clip: dict[str, Any]) -> str:
    text = _preview_compact_text(_preview_selected_text(clip))
    clip_type = str(clip.get("clip_type") or "").lower()
    if "close" in clip_type and any(word in text for word in ("尺码", "码", "体重", "身高", "斤")):
        return "收尾:尺码"
    block = _preview_focus_block(clip)
    if block == "场景搭配":
        if any(word in text for word in ("度假", "放假", "旅游", "海边", "云南", "泰兰德", "旅行")):
            return "场景搭配:度假旅行"
        if any(word in text for word in ("草帽", "棒球帽", "编织帽", "搭包")):
            return "场景搭配:配饰搭配"
        if any(word in text for word in ("通勤", "上班", "职场", "办公室")):
            return "场景搭配:通勤"
    if block == "版型显瘦":
        if any(word in text for word in ("肩", "肩线", "肩膀", "往里挖")):
            return "版型显瘦:肩线"
        if any(word in text for word in ("拜拜肉", "胳膊", "手臂")):
            return "版型显瘦:手臂"
        if any(word in text for word in ("显瘦", "遮肉", "藏肉", "正面", "侧面")):
            return "版型显瘦:泛化效果"
    if block == "面料质感":
        if any(word in text for word in ("不透", "透到", "透肤")):
            return "面料质感:防透"
        if any(word in text for word in ("柔软", "不扎", "舒服", "细")):
            return "面料质感:亲肤"
    if block in ("品质细节", "工艺细节") and any(word in text for word in ("拼接", "拼色", "色牢度", "掉色", "串色")):
        return "工艺细节:拼接色牢度"
    return ""


def _preview_clip_strength(clip: dict[str, Any]) -> float:
    text = _preview_compact_text(_preview_selected_text(clip))
    clip_type = str(clip.get("clip_type") or "").lower()
    score = min(24.0, len(text) / 2.5)
    if "hook" in clip_type:
        score += 30.0
    if any(word in text for word in ("面料", "材质", "工艺", "拼接", "色牢度", "不透", "肩线", "剪裁")):
        score += 12.0
    if any(word in text for word in ("s码", "m码", "l码", "xl", "斤", "身高", "体重")):
        score += 16.0
    return score


def _preview_unselect_intra_clip_repeated_subtopics(public_clips: list[dict[str, Any]], max_duration: float = 10.0) -> int:
    """Do not let adjacent subtitle units repeat one selling subtopic for too long."""
    removed = 0
    for clip in public_clips or []:
        seen_duration: dict[str, float] = {}
        for segment in list(clip.get("segments") or []):
            if not isinstance(segment, dict) or segment.get("selected") is False:
                continue
            probe = dict(clip)
            probe["segments"] = [segment]
            signature = _preview_subtopic_signature(probe)
            if not signature:
                continue
            duration = max(0.0, float(segment.get("duration") or 0.0))
            previous_duration = seen_duration.get(signature, 0.0)
            if previous_duration > 0 and previous_duration + duration > max_duration:
                segment["selected"] = False
                segment["auto_unselected_reason"] = f"同一子主题已保留{previous_duration:.1f}s，移除重复延展"
                removed += 1
                continue
            seen_duration[signature] = previous_duration + duration
    return removed


def _preview_unselect_repeated_subtopics(public_clips: list[dict[str, Any]]) -> int:
    """Prefer one self-contained evidence unit over repeated slogan variants."""
    kept: dict[str, dict[str, Any]] = {}
    removed = 0
    for clip in public_clips or []:
        if clip.get("selected") is False or not _preview_selected_text(clip):
            continue
        signature = _preview_subtopic_signature(clip)
        if not signature:
            continue
        clip_type = str(clip.get("clip_type") or "").lower()
        if signature == "版型显瘦:泛化效果" and any(key.startswith("版型显瘦:") for key in kept):
            for segment in list(clip.get("segments") or []):
                if isinstance(segment, dict) and segment.get("selected") is not False:
                    segment["selected"] = False
                    segment["auto_unselected_reason"] = "与已有版型证据重复的泛化效果"
                    removed += 1
            clip["selected"] = False
            continue
        previous = kept.get(signature)
        if previous is None:
            kept[signature] = clip
            continue
        previous_type = str(previous.get("clip_type") or "").lower()
        replace_previous = (
            signature == "收尾:尺码"
            and "hook" not in previous_type
            and _preview_clip_strength(clip) > _preview_clip_strength(previous)
        )
        target = previous if replace_previous else clip
        reason = f"重复子主题：{signature}"
        for segment in list(target.get("segments") or []):
            if isinstance(segment, dict) and segment.get("selected") is not False:
                segment["selected"] = False
                segment["auto_unselected_reason"] = reason
                removed += 1
        target["selected"] = False
        if replace_previous:
            kept[signature] = clip
    return removed


def _preview_quality_summary(public_clips: list[dict[str, Any]]) -> dict[str, Any]:
    blocked: dict[str, int] = {}
    auto_removed: dict[str, int] = {}
    for clip in public_clips or []:
        for segment in list(clip.get("segments") or []):
            if not isinstance(segment, dict):
                continue
            if segment.get("selection_locked"):
                reason = str(segment.get("blocked_reason") or "内容安全")
                blocked[reason] = blocked.get(reason, 0) + 1
            elif segment.get("selected") is False and segment.get("auto_unselected_reason"):
                reason = str(segment.get("auto_unselected_reason"))
                auto_removed[reason] = auto_removed.get(reason, 0) + 1
    return {
        "preview_locked_segments": sum(blocked.values()),
        "preview_auto_unselected_segments": sum(auto_removed.values()),
        "preview_locked_reason_counts": blocked,
        "preview_auto_unselected_reason_counts": auto_removed,
    }


def _preview_effective_clip_tuples(raw_clips: list[Any], public_clips: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Use the exact selected subtitle units for preview-side topic reporting."""
    effective: list[tuple[Any, ...]] = []
    raw_values = list(raw_clips or [])
    for index, public_clip in enumerate(public_clips or []):
        raw_clip = raw_values[index] if index < len(raw_values) else public_clip
        selected = [
            segment for segment in list(public_clip.get("segments") or [])
            if isinstance(segment, dict) and segment.get("selected") is not False
        ]
        if not selected:
            continue
        text = " ".join(str(segment.get("text") or "").strip() for segment in selected).strip()
        if not text:
            continue
        values = list(_clip_to_tuple(raw_clip, default_source=str(public_clip.get("source") or "")))
        while len(values) < 8:
            values.append("")
        marker = str(public_clip.get("source_marker") or "").strip().upper()
        values[1] = f"[{marker}] {text}" if marker else text
        start = min(_preview_segment_start(segment) for segment in selected)
        end = max(_preview_segment_end(segment) for segment in selected)
        values[2] = start
        values[3] = end
        values[5] = max(0.0, end - start)
        if not values[7]:
            values[7] = str(public_clip.get("source") or "")
        effective.append(tuple(values))
    return effective


def _preview_effective_topic_coverage(
    raw_clips: list[Any],
    public_clips: list[dict[str, Any]],
    preferred_focus: str,
    target_duration: int | None,
    requested: str = "自动",
) -> dict[str, Any]:
    try:
        import ai_clipper as ai_mod

        return dict(
            ai_mod._topic_coverage_summary(
                _preview_effective_clip_tuples(raw_clips, public_clips),
                preferred_focus,
                target_duration or 60,
                requested,
            ) or {}
        )
    except Exception:
        return {}


def _supplement_preview_preference(
    raw_clips: list[Any],
    public_clips: list[dict[str, Any]],
    srt_text: str,
    preferred_focus: str,
    target_duration: int | None,
    *,
    sources: list[Any] | None = None,
    merge_mode: bool = False,
    word_timings: list[dict[str, Any]] | None = None,
) -> int:
    """Backfill a missing preference only with a safe, standalone SRT unit."""
    if not srt_text or not preferred_focus:
        return 0
    try:
        import ai_clipper as ai_mod
        from cutter_logic import _parse_srt_to_segments
    except Exception:
        return 0

    preferred_topic = ai_mod._focus_label_to_block(preferred_focus) or str(preferred_focus or "").strip()
    if not preferred_topic:
        return 0
    coverage = _preview_effective_topic_coverage(raw_clips, public_clips, preferred_focus, target_duration)
    missing = max(0, int(coverage.get("preference_min", 0)) - int(coverage.get("preference_count", 0)))
    if not missing:
        return 0

    entries = list(_parse_srt_to_segments(srt_text) or [])
    source_list = [str(value) for value in list(sources or []) if str(value or "").strip()]
    existing_signatures = {
        _preview_subtopic_signature(clip)
        for clip in public_clips
        if clip.get("selected") is not False and _preview_selected_text(clip)
    }
    existing_ranges = [
        (
            str(clip.get("source") or ""),
            _preview_segment_start(segment),
            _preview_segment_end(segment),
        )
        for clip in public_clips
        for segment in list(clip.get("segments") or [])
        if isinstance(segment, dict) and segment.get("selected") is not False
    ]
    candidates: list[tuple[float, tuple[Any, ...]]] = []
    for entry in entries:
        try:
            start = float(entry.get("start") or 0)
            end = float(entry.get("end") or start)
        except Exception:
            continue
        duration = max(0.0, end - start)
        text = _repair_mojibake_text(entry.get("text") or "").strip()
        compact = _preview_compact_text(text)
        if duration < 2.2 or duration > 10.0 or len(compact) < 12:
            continue
        if ai_mod._is_safety_blocked_text(text) or ai_mod._is_backstage_instruction(text):
            continue
        topic = ai_mod._clip_primary_topic(("product", text, start, end, 42.0, duration, preferred_topic))
        if topic != preferred_topic:
            continue
        marker = _preview_source_marker(text)
        source = ""
        if marker and marker[1:].isdigit() and int(marker[1:]) <= len(source_list):
            source = source_list[int(marker[1:]) - 1]
        if merge_mode and not source:
            continue
        if any(
            (not source or not old_source or source == old_source) and start < old_end and end > old_start
            for old_source, old_start, old_end in existing_ranges
        ):
            continue
        evidence = ai_mod._topic_evidence_scores(("product", text, start, end, 42.0, duration, preferred_topic)).get(topic, 0.0)
        quality = evidence * 8.0 + min(16.0, len(compact) / 3.0) + min(10.0, duration)
        candidates.append((quality, ("product", text, start, end, 42.0, duration, preferred_topic, source)))

    added = 0
    for _quality, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        candidate_public = _preview_public_clips(
            [candidate],
            srt_text,
            sources=source_list,
            merge_mode=merge_mode,
            word_timings=word_timings,
        )
        if not candidate_public:
            continue
        public = candidate_public[0]
        if public.get("selected") is False or not _preview_selected_text(public):
            continue
        signature = _preview_subtopic_signature(public)
        if signature and signature in existing_signatures:
            continue
        raw_clips.append(candidate)
        public["index"] = len(public_clips)
        public_clips.append(public)
        if signature:
            existing_signatures.add(signature)
        for segment in list(public.get("segments") or []):
            if isinstance(segment, dict) and segment.get("selected") is not False:
                existing_ranges.append((str(public.get("source") or ""), _preview_segment_start(segment), _preview_segment_end(segment)))
        added += 1
        if added >= missing:
            break
    return added


def _clean_preview_filler_prefix(text: Any) -> str:
    marker, body = _preview_text_marker_and_body(text)
    prefixes = [
        "然后说一下", "好的是的", "是的是的", "说一下", "然后", "而且", "但是", "不过", "其实",
        "好的", "是的", "对的", "嗯嗯", "对吧", "是吧", "好吧", "嗯", "啊", "呃", "额", "哦", "噢", "对",
    ]
    changed = True
    while changed:
        changed = False
        stripped = body.lstrip(" ，,。.!！?？、")
        leading_space = body[: len(body) - len(stripped)]
        for prefix in sorted(prefixes, key=len, reverse=True):
            if not stripped.startswith(prefix):
                continue
            rest = stripped[len(prefix):].lstrip(" ，,。.!！?？、")
            if len(rest) < 4:
                continue
            if prefix == "对" and stripped.startswith(("对比", "对应", "对称")):
                continue
            body = leading_space + rest
            changed = True
            break
    return f"{marker}{body.strip()}" if marker else body.strip()


def _preview_source_marker(text: Any) -> str:
    match = re.search(r"\[([vV]\d+)\]", str(text or ""))
    return match.group(1).upper() if match else ""


def _preview_marker_source(sources: list[Any], text: Any) -> str:
    marker = _preview_source_marker(text)
    if not marker:
        return ""
    try:
        index = int(marker[1:]) - 1
    except Exception:
        return ""
    if index < 0 or index >= len(sources or []):
        return ""
    return str((sources or [])[index] or "").strip()


def _preview_source_path_key(value: Any) -> str:
    raw = str(value or "").strip().strip('"')
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve()).lower()
    except Exception:
        return str(Path(raw)).lower()


def _preview_source_marker_from_source(sources: list[Any], source: Any) -> str:
    source_key = _preview_source_path_key(source)
    if not source_key:
        return ""
    for index, candidate in enumerate(sources or []):
        if _preview_source_path_key(candidate) == source_key:
            return f"V{index + 1}"
    return ""


def _preview_expected_source_marker(public_clip: dict[str, Any], sources: list[Any] | None = None) -> str:
    marker = _preview_source_marker_from_source(list(sources or []), public_clip.get("source") or "")
    if marker:
        return marker
    marker = str(public_clip.get("source_marker") or "").strip().upper()
    if marker:
        return marker
    return _preview_source_marker(public_clip.get("text") or "")


def _preview_clip_with_source(clip: Any, source: str) -> Any:
    source = str(source or "").strip()
    if not source:
        return clip
    if isinstance(clip, dict):
        if str(clip.get("source") or "").strip():
            return clip
        next_clip = dict(clip)
        next_clip["source"] = source
        return next_clip
    if isinstance(clip, (list, tuple)):
        values = list(clip)
        if len(values) <= 7:
            values.append(source)
        elif isinstance(values[7], dict):
            values.insert(7, source)
        elif not str(values[7] or "").strip():
            values[7] = source
        else:
            return clip
        return tuple(values)
    return clip


def _attach_mix_sources_to_preview_clips(clips: list[Any], sources: list[Any]) -> list[Any]:
    result: list[Any] = []
    for clip in clips or []:
        info = _clip_public(0, clip)
        if info.get("source"):
            result.append(clip)
            continue
        marker_source = _preview_marker_source(sources, info.get("text") or "")
        result.append(_preview_clip_with_source(clip, marker_source) if marker_source else clip)
    return result


def _preview_segment_start(segment: dict[str, Any]) -> float:
    try:
        return float(segment.get("start") or 0)
    except Exception:
        return 0.0


def _preview_segment_end(segment: dict[str, Any]) -> float:
    start = _preview_segment_start(segment)
    try:
        end = float(segment.get("end") or start)
    except Exception:
        end = start
    return max(start, end)


def _preview_segment_index(segment: dict[str, Any]) -> int:
    try:
        return int(segment.get("index"))
    except Exception:
        return -1


def _sort_and_reindex_preview_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        segments,
        key=lambda seg: (
            _preview_segment_start(seg),
            _preview_segment_end(seg),
            int(seg.get("index") or 0),
        ),
    )
    for index, segment in enumerate(ordered):
        segment["index"] = index
    return ordered


def _preview_segments_for_clip(
    public_clip: dict[str, Any],
    srt_segments: list[dict[str, Any]],
    expected_marker: str = "",
    word_timings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    try:
        start = float(public_clip.get("start") or 0)
        end = float(public_clip.get("end") or start)
    except Exception:
        start, end = 0.0, 0.0
    marker = str(expected_marker or _preview_expected_source_marker(public_clip)).strip().upper()
    pieces: list[dict[str, Any]] = []
    if end > start and word_timings:
        for word_segment in word_timings:
            if not isinstance(word_segment, dict):
                continue
            segment_marker = str(word_segment.get("source_marker") or "").strip().upper()
            if marker and segment_marker != marker:
                continue
            if not marker and segment_marker:
                continue
            selected_words = []
            for word in word_segment.get("words") or []:
                if not isinstance(word, dict):
                    continue
                try:
                    word_start = float(word.get("start") or 0)
                    word_end = float(word.get("end") or word_start)
                except (TypeError, ValueError):
                    continue
                midpoint = (word_start + word_end) / 2.0
                if start - 0.02 <= midpoint <= end + 0.02 and word_end > word_start:
                    selected_words.append((word_start, word_end, str(word.get("text") or "")))
            if not selected_words:
                continue
            text = _clean_preview_filler_prefix("".join(item[2] for item in selected_words).strip())
            if not text or _preview_is_pure_filler(text):
                continue
            piece_start = max(start, selected_words[0][0])
            piece_end = min(end, selected_words[-1][1])
            pieces.append({
                "index": len(pieces),
                "start": round(piece_start, 3),
                "end": round(piece_end, 3),
                "duration": round(max(0.0, piece_end - piece_start), 3),
                "text": text,
                "selected": not _preview_has_forbidden_or_price(text),
                "word_timed": True,
            })
        if pieces:
            weak_tail_segments = {
                "反正就是不显白怎么说呢", "是的为什么", "为什么", "对不对", "能理解吗",
                "知道吧", "对吧", "是吧",
                "然后", "而且",
            }
            for piece in reversed(pieces):
                if piece.get("selected") is False:
                    continue
                compact = _preview_compact_text(piece.get("text") or "")
                if compact in weak_tail_segments:
                    piece["selected"] = False
                    piece["weak_edge"] = True
                    continue
                break
            return _sort_and_reindex_preview_segments(pieces)
    if end > start:
        boundary_slack = 0.65
        for seg in srt_segments or []:
            try:
                seg_start = float(seg.get("start") or 0)
                seg_end = float(seg.get("end") or seg_start)
            except Exception:
                continue
            overlap_start = max(start, seg_start)
            overlap_end = min(end, seg_end)
            overlap = overlap_end - overlap_start
            touches_start = -0.02 <= start - seg_end <= boundary_slack
            touches_end = -0.02 <= seg_start - end <= boundary_slack
            if overlap < 0.04 and not touches_start and not touches_end:
                continue
            seg_text = _repair_mojibake_text(seg.get("text") or "")
            seg_marker = _preview_source_marker(seg_text)
            if marker and seg_marker != marker:
                continue
            text = _strip_preview_source_marker(seg_text)
            if not text:
                continue
            if _preview_is_pure_filler(text):
                continue
            text = _clean_preview_filler_prefix(text)
            if not text:
                continue
            pieces.append({
                "index": len(pieces),
                "start": round(seg_start, 3),
                "end": round(seg_end, 3),
                "duration": round(max(0.0, seg_end - seg_start), 3),
                "text": text,
                "selected": overlap >= 0.04 and not _preview_has_forbidden_or_price(text),
            })
    if pieces:
        return _sort_and_reindex_preview_segments(pieces)
    text = _preview_text_for_marker(public_clip.get("text") or "", marker)
    if not text:
        text = str(public_clip.get("text") or "")
    text = _clean_preview_filler_prefix(text)
    return [{
        "index": 0,
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(max(0.0, end - start), 3),
        "text": text,
        "selected": not _preview_is_pure_filler(text) and not _preview_has_forbidden_or_price(text),
    }]


def _preview_unselect_duplicate_segments(public_clips: list[dict[str, Any]]) -> dict[str, Any]:
    seen: dict[tuple[str, float, float, str], int] = {}
    seen_fuzzy: list[dict[str, Any]] = []
    removed_segments = 0
    removed_clips = 0
    for clip in public_clips:
        segments = list(clip.get("segments") or [])
        if not segments:
            continue
        source = str(clip.get("source") or clip.get("source_name") or "")
        selected_before = [seg for seg in segments if seg.get("selected") is not False]
        if not selected_before:
            clip["selected"] = False
            continue
        removed_duration = 0.0
        total_duration = sum(float(seg.get("duration") or 0) for seg in selected_before)
        for seg in selected_before:
            start = round(float(seg.get("start") or 0), 3)
            end = round(float(seg.get("end") or start), 3)
            full_text_key = _preview_compact_text(seg.get("text") or "")
            text_key = full_text_key[:48]
            key = (source, start, end, text_key)
            duplicate_of = seen.get(key)
            if duplicate_of is None and len(full_text_key) >= 8:
                for old in seen_fuzzy:
                    if source and old.get("source") and source != old.get("source"):
                        continue
                    old_text = str(old.get("text") or "")
                    if not old_text:
                        continue
                    overlap = max(0.0, min(end, float(old.get("end") or 0)) - max(start, float(old.get("start") or 0)))
                    shorter, longer = sorted((full_text_key, old_text), key=len)
                    score = _preview_similarity(full_text_key, old_text)
                    same_sentence = len(shorter) >= 10 and shorter in longer
                    same_time_repeat = overlap >= 0.2 and score >= 0.72
                    strong_text_repeat = min(len(full_text_key), len(old_text)) >= 12 and score >= 0.92
                    if same_sentence or same_time_repeat or strong_text_repeat:
                        duplicate_of = int(old.get("clip_index", -1))
                        break
            if duplicate_of is not None:
                seg["selected"] = False
                seg["duplicate_of"] = duplicate_of
                removed_segments += 1
                removed_duration += float(seg.get("duration") or max(0.0, end - start))
            else:
                seen[key] = int(clip.get("index", -1))
                seen_fuzzy.append({
                    "source": source,
                    "start": start,
                    "end": end,
                    "text": full_text_key,
                    "clip_index": int(clip.get("index", -1)),
                })
        selected_after = [seg for seg in segments if seg.get("selected") is not False]
        if selected_after and total_duration > 0 and removed_duration / total_duration >= 0.65:
            for seg in segments:
                if seg.get("selected") is not False:
                    seg["selected"] = False
                    seg["duplicate_tail"] = True
                    removed_segments += 1
            selected_after = []
        if not selected_after:
            clip["selected"] = False
            removed_clips += 1
    return {"preview_duplicate_segments_removed": removed_segments, "preview_duplicate_clips_unselected": removed_clips}


def _refresh_preview_clip_sales_metadata(public_clips: list[dict[str, Any]]) -> None:
    for clip in public_clips or []:
        if not isinstance(clip, dict):
            continue
        segments = list(clip.get("segments") or [])
        if segments and not any(seg.get("selected") is not False for seg in segments if isinstance(seg, dict)):
            clip["selected"] = False
        clip["focus_block"] = _preview_focus_block(clip)
        sales_role = _preview_sales_role(clip)
        clip["sales_role"] = sales_role
        clip["sales_role_label"] = _PREVIEW_SALES_ROLE_LABELS.get(sales_role, "补充卖点")


def _preview_public_clips(
    clips: list[Any],
    srt_text: str = "",
    *,
    sources: list[Any] | None = None,
    merge_mode: bool = False,
    word_timings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    srt_segments: list[dict[str, Any]] = []
    if srt_text:
        try:
            from cutter_logic import _parse_srt_to_segments

            srt_segments = list(_parse_srt_to_segments(srt_text) or [])
        except Exception as exc:
            emit_log("warning", f"preview segment split skipped: {exc}", "system")
    source_list = list(sources or [])
    public_clips = [_clip_public(index, clip) for index, clip in enumerate(clips)]
    for clip in public_clips:
        expected_marker = _preview_expected_source_marker(clip, source_list) if merge_mode else ""
        if expected_marker:
            clip["source_marker"] = expected_marker
        clip["segments"] = _preview_segments_for_clip(
            clip,
            srt_segments,
            expected_marker,
            word_timings=word_timings,
        )
    _preview_lock_unsafe_segments(public_clips)
    _refresh_preview_clip_sales_metadata(public_clips)
    return public_clips


def _drop_unusable_preview_clips(
    raw_clips: list[Any],
    public_clips: list[dict[str, Any]],
) -> tuple[list[Any], list[dict[str, Any]], int]:
    kept_raw: list[Any] = []
    kept_public: list[dict[str, Any]] = []
    removed = 0
    for raw_clip, public_clip in zip(raw_clips or [], public_clips or []):
        segments = list(public_clip.get("segments") or [])
        has_selected = any(
            segment.get("selected") is not False and _preview_compact_text(segment.get("text") or "")
            for segment in segments
            if isinstance(segment, dict)
        )
        if public_clip.get("selected") is False or not has_selected:
            removed += 1
            continue
        public_clip["index"] = len(kept_public)
        kept_raw.append(raw_clip)
        kept_public.append(public_clip)

    for index, public_clip in enumerate(kept_public):
        public_clip["index"] = index
    _refresh_preview_clip_sales_metadata(kept_public)
    return kept_raw, kept_public, removed


def _merge_selected_segments(
    public_clip: dict[str, Any],
    raw_clip: Any,
    segment_indices: list[int] | None,
) -> list[tuple[Any, ...]]:
    segments = list(public_clip.get("segments") or [])
    if segment_indices is None:
        selected = [seg for seg in segments if seg.get("selected") is not False]
    else:
        wanted = {int(value) for value in segment_indices if isinstance(value, int) or str(value).lstrip("-").isdigit()}
        selected = [
            seg for seg in segments
            if int(seg.get("index", -1)) in wanted and not seg.get("selection_locked")
        ]
    if not selected:
        return []
    selected.sort(key=lambda seg: (_preview_segment_index(seg), _preview_segment_start(seg), _preview_segment_end(seg)))

    groups: list[list[dict[str, Any]]] = []
    group_end = 0.0
    group_last_index = -1
    for seg in selected:
        seg_index = _preview_segment_index(seg)
        seg_start = _preview_segment_start(seg)
        seg_end = _preview_segment_end(seg)
        if not groups:
            groups.append([seg])
            group_end = seg_end
            group_last_index = seg_index
            continue
        gap = seg_start - group_end
        consecutive_sentence = (
            group_last_index >= 0
            and seg_index >= 0
            and seg_index == group_last_index + 1
        )
        missing_index_fallback = (group_last_index < 0 or seg_index < 0) and gap <= 0.35
        if consecutive_sentence or missing_index_fallback:
            groups[-1].append(seg)
            group_end = max(group_end, seg_end)
            group_last_index = seg_index
        else:
            groups.append([seg])
            group_end = seg_end
            group_last_index = seg_index

    base = list(_clip_to_tuple(raw_clip, default_source=str(public_clip.get("source") or "")))
    while len(base) < 8:
        base.append("")
    result: list[tuple[Any, ...]] = []
    source_marker = _preview_expected_source_marker(public_clip)
    for group_index, group in enumerate(groups):
        start = min(float(seg.get("start") or 0) for seg in group)
        end = max(float(seg.get("end") or start) for seg in group)
        text = "".join(_strip_preview_source_marker(seg.get("text") or "") for seg in group).strip()
        if source_marker:
            text = f"[{source_marker}] {text}"
        values = list(base)
        values[1] = text or str(public_clip.get("text") or "")
        values[2] = start
        values[3] = end
        values[5] = max(0.0, end - start)
        if len(values) >= 8 and not values[7]:
            values[7] = str(public_clip.get("source") or "")
        values.append({
            "preview_exact": True,
            "preview_parent_index": int(public_clip.get("index", -1)),
            "preview_group_index": group_index,
            "preview_group_count": len(groups),
            "selected_segment_indices": [int(seg.get("index", -1)) for seg in group],
        })
        result.append(tuple(values))
    return result


def _clips_from_preview_selection(
    preview: dict[str, Any],
    selected_indices: list[int],
    selected_segments: dict[str, list[int]] | None = None,
) -> list[tuple[Any, ...]]:
    raw_clips = list(preview.get("raw_clips") or [])
    public_clips = {
        int(clip.get("index")): clip
        for clip in list(preview.get("clips") or [])
        if str(clip.get("index", "")).lstrip("-").isdigit()
    }
    segment_map = selected_segments or {}
    result: list[tuple[Any, ...]] = []
    for raw_index in selected_indices:
        try:
            idx = int(raw_index)
        except Exception:
            continue
        if idx < 0 or idx >= len(raw_clips):
            continue
        public_clip = public_clips.get(idx) or _clip_public(idx, raw_clips[idx])
        key = str(idx)
        requested_segments = segment_map.get(key)
        if requested_segments is None:
            requested_segments = segment_map.get(idx) if isinstance(segment_map, dict) else None
        result.extend(_merge_selected_segments(public_clip, raw_clips[idx], requested_segments))
    return result


def _preview_selection_segment_count(selected_segments: dict[str, list[int]] | None) -> int:
    if not isinstance(selected_segments, dict):
        return 0
    total = 0
    for values in selected_segments.values():
        if isinstance(values, list):
            total += len(values)
    return total


def _path_identity(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except Exception:
        return str(path).lower()


def _append_unique_source_path(paths: list[Path], seen: set[str], value: Any) -> None:
    raw = str(value or "").strip().strip('"')
    if not raw:
        return
    path = Path(raw)
    key = _path_identity(path)
    if key in seen:
        return
    paths.append(path)
    seen.add(key)


def _preview_mix_source_paths(
    preview: dict[str, Any],
    payload_paths: list[str],
    selected_clips: list[tuple[Any, ...]],
) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()

    for value in list(preview.get("sources") or []):
        _append_unique_source_path(paths, seen, value)

    if not paths:
        marker_sources: dict[int, str] = {}
        for clip in list(preview.get("raw_clips") or []) + list(preview.get("clips") or []):
            clip_info = _clip_public(0, clip)
            marker = _preview_source_marker(clip_info.get("text") or "")
            source = str(clip_info.get("source") or "").strip()
            if not marker or not source:
                continue
            try:
                marker_index = int(marker[1:])
            except Exception:
                continue
            marker_sources.setdefault(marker_index, source)
        for marker_index in sorted(marker_sources):
            _append_unique_source_path(paths, seen, marker_sources[marker_index])

    if not paths:
        for value in payload_paths or []:
            _append_unique_source_path(paths, seen, value)

    for clip in selected_clips or []:
        clip_info = _clip_public(0, clip)
        _append_unique_source_path(paths, seen, clip_info.get("source"))

    return paths


def _clip_has_preview_exact_marker(clip: Any) -> bool:
    if not isinstance(clip, (list, tuple)):
        return False
    return any(isinstance(item, dict) and item.get("preview_exact") for item in clip)


def _clip_preview_exact_meta(clip: Any) -> dict[str, Any]:
    if not isinstance(clip, (list, tuple)):
        return {}
    meta = next((item for item in clip if isinstance(item, dict) and item.get("preview_exact")), None)
    return dict(meta) if isinstance(meta, dict) else {}


def _log_preview_selection(
    scope: str,
    label: str,
    selected_indices: list[int],
    selected_segments: dict[str, list[int]] | None,
    clips: list[tuple[Any, ...]],
) -> None:
    exact_count = sum(1 for clip in clips if _clip_has_preview_exact_marker(clip))
    segment_count = _preview_selection_segment_count(selected_segments)
    emit_log(
        "info",
        f"{label}：收到 {len(selected_indices)} 个片段选择、{segment_count} 条子句选择，生成 {len(clips)} 个剪辑区间（精确子句 {exact_count} 个）。",
        scope,
    )
    for idx, clip in enumerate(clips[:8], start=1):
        try:
            text = str(clip[1] if len(clip) > 1 else "").strip()
            start = float(clip[2] if len(clip) > 2 else 0)
            end = float(clip[3] if len(clip) > 3 else start)
            exact = "精确" if _clip_has_preview_exact_marker(clip) else "整段"
            meta = _clip_preview_exact_meta(clip)
            seg_note = ""
            if meta:
                parent = meta.get("preview_parent_index")
                group_index = int(meta.get("preview_group_index", 0) or 0) + 1
                group_count = int(meta.get("preview_group_count", 1) or 1)
                segs = meta.get("selected_segment_indices") or []
                display_segs = [int(item) + 1 for item in segs if str(item).lstrip("-").isdigit()]
                seg_note = f" 父片段{parent} 子句{display_segs} 组{group_index}/{group_count}"
            emit_log("info", f"{label}片段[{idx}] {exact}{seg_note} {start:.3f}-{end:.3f}s | {text[:80]}", scope)
        except Exception:
            continue


def _preview_feedback_log_path() -> Path:
    return _safe_user_child("ai_feedback", "preview_selection_feedback.jsonl")


def _feedback_category_bucket(category: Any) -> str:
    text = str(category or "").strip()
    if "食品" in text or "生鲜" in text:
        return "food_fresh"
    if text and text not in {"自动", "自动检测", "auto"}:
        return "clothing"
    return "general"


def _feedback_scope_key(scope: str, category: Any = "") -> str:
    base = str(scope or "smart").strip() or "smart"
    return f"{base}:{_feedback_category_bucket(category)}"


def _preview_feedback_text(value: Any, limit: int = 140) -> str:
    text = _strip_preview_source_marker(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _preview_feedback_clip_entry(index: int, public_clip: dict[str, Any]) -> dict[str, Any]:
    start = float(public_clip.get("start") or 0)
    end = float(public_clip.get("end") or start)
    return {
        "clip_index": int(index),
        "text": _preview_feedback_text(public_clip.get("text") or ""),
        "start": round(start, 3),
        "end": round(max(start, end), 3),
        "duration": round(max(0.0, float(public_clip.get("duration") or (end - start))), 3),
        "clip_type": str(public_clip.get("clip_type") or "product"),
        "focus": str(public_clip.get("focus") or ""),
        "focus_block": str(public_clip.get("focus_block") or ""),
        "source_name": str(public_clip.get("source_name") or ""),
    }


def _preview_feedback_segment_entry(
    clip_index: int,
    public_clip: dict[str, Any],
    segment: dict[str, Any],
) -> dict[str, Any]:
    start = _preview_segment_start(segment)
    end = _preview_segment_end(segment)
    return {
        "clip_index": int(clip_index),
        "segment_index": _preview_segment_index(segment),
        "text": _preview_feedback_text(segment.get("text") or ""),
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(max(0.0, end - start), 3),
        "clip_type": str(public_clip.get("clip_type") or "product"),
        "focus": str(public_clip.get("focus") or ""),
        "focus_block": str(public_clip.get("focus_block") or ""),
        "source_name": str(public_clip.get("source_name") or ""),
    }


def _build_preview_selection_feedback(
    preview: dict[str, Any],
    scope: str,
    draft: dict[str, Any],
    event: str,
) -> dict[str, Any]:
    raw_clips = list(preview.get("raw_clips") or [])
    public_clips = {
        int(clip.get("index")): clip
        for clip in list(preview.get("clips") or [])
        if str(clip.get("index", "")).lstrip("-").isdigit()
    }
    raw_count = max(len(raw_clips), max(public_clips.keys(), default=-1) + 1)
    selected = _clean_preview_int_list(draft.get("selected_indices") or [], raw_count)
    selected_set = set(selected)
    selected_segments = draft.get("selected_segments") if isinstance(draft.get("selected_segments"), dict) else {}

    kept_texts: list[dict[str, Any]] = []
    rejected_segment_texts: list[dict[str, Any]] = []
    rejected_clip_texts: list[dict[str, Any]] = []
    clip_entries: dict[int, dict[str, Any]] = {}
    kept_by_clip: dict[int, list[dict[str, Any]]] = {}
    rejected_segments_by_clip: dict[int, list[dict[str, Any]]] = {}

    for index in range(raw_count):
        raw_clip = raw_clips[index] if index < len(raw_clips) else {}
        public_clip = public_clips.get(index) or _clip_public(index, raw_clip)
        clip_entry = _preview_feedback_clip_entry(index, public_clip)
        if clip_entry["text"]:
            clip_entries[index] = clip_entry
        segments = list(public_clip.get("segments") or [])
        if index in selected_set:
            if segments:
                raw_values = selected_segments.get(str(index))
                if raw_values is None:
                    raw_values = selected_segments.get(index) if isinstance(selected_segments, dict) else None
                if isinstance(raw_values, list):
                    wanted = set(_clean_preview_int_list(raw_values, len(segments)))
                else:
                    wanted = {
                        _preview_segment_index(seg)
                        for seg in segments
                        if seg.get("selected") is not False
                    }
                for segment in segments:
                    entry = _preview_feedback_segment_entry(index, public_clip, segment)
                    if not entry["text"]:
                        continue
                    if _preview_segment_index(segment) in wanted:
                        kept_texts.append(entry)
                        kept_by_clip.setdefault(index, []).append(entry)
                    else:
                        rejected_segment_texts.append(entry)
                        rejected_segments_by_clip.setdefault(index, []).append(entry)
            else:
                if clip_entry["text"]:
                    kept_texts.append(clip_entry)
                    kept_by_clip.setdefault(index, []).append(clip_entry)
        else:
            if clip_entry["text"]:
                rejected_clip_texts.append(clip_entry)

    draft_order = _clean_preview_int_list(draft.get("order") or [], raw_count)
    selected_order = [index for index in draft_order if index in selected_set]
    selected_order.extend(index for index in selected if index not in set(selected_order))
    first_selected = selected_order[0] if selected_order else None
    last_selected = selected_order[-1] if selected_order else None
    hook_positive = list(kept_by_clip.get(first_selected, [])) if first_selected is not None else []
    close_positive = list(kept_by_clip.get(last_selected, [])) if last_selected is not None else []
    hook_negative = list(rejected_segments_by_clip.get(first_selected, [])) if first_selected is not None else []
    close_negative = list(rejected_segments_by_clip.get(last_selected, [])) if last_selected is not None else []
    if first_selected is not None and first_selected != 0 and 0 in clip_entries:
        hook_negative.append(clip_entries[0])
    if last_selected is not None and raw_count > 0 and last_selected != raw_count - 1 and raw_count - 1 in clip_entries:
        close_negative.append(clip_entries[raw_count - 1])
    moved_to_front = list(kept_by_clip.get(first_selected, [])) if first_selected is not None and selected and first_selected != min(selected) else []
    moved_to_end = list(kept_by_clip.get(last_selected, [])) if last_selected is not None and selected and last_selected != max(selected) else []
    role_samples = {
        "hook_positive": hook_positive[:20],
        "hook_negative": hook_negative[:20],
        "close_positive": close_positive[:20],
        "close_negative": close_negative[:20],
        "sentence_positive": kept_texts[:80],
        "sentence_negative": rejected_segment_texts[:80],
        "move_to_front": moved_to_front[:20],
        "move_to_end": moved_to_end[:20],
    }
    category = str(preview.get("category") or draft.get("category") or "").strip()
    feedback_scope = str(preview.get("feedback_scope") or _feedback_scope_key(scope, category)).strip()

    return {
        "created_at": time.time(),
        "created_at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "scope": scope,
        "feedback_scope": feedback_scope,
        "category": category,
        "category_bucket": _feedback_category_bucket(category),
        "preview_id": str(preview.get("id") or draft.get("preview_id") or ""),
        "target_duration": preview.get("target_duration"),
        "selected_clip_count": len(selected),
        "kept_segment_count": len(kept_texts),
        "rejected_segment_count": len(rejected_segment_texts),
        "rejected_clip_count": len(rejected_clip_texts),
        "kept_texts": kept_texts[:80],
        "rejected_segment_texts": rejected_segment_texts[:80],
        "rejected_clip_texts": rejected_clip_texts[:40],
        "role_samples": role_samples,
    }


def _append_preview_selection_feedback(record: dict[str, Any]) -> None:
    path = _preview_feedback_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
            path.write_text("\n".join(lines[-600:]) + "\n", encoding="utf-8")
        except Exception:
            pass
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _preview_feedback_records_from_text(text: str) -> list[dict[str, Any]]:
    text = str(text or "").strip()
    if not text:
        return []
    records: list[dict[str, Any]] = []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except Exception:
            data = []
        if isinstance(data, list):
            records.extend(item for item in data if isinstance(item, dict))
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def _preview_feedback_record_key(record: dict[str, Any]) -> str:
    try:
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True)
    except Exception:
        raw = str(record)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _preview_feedback_load_records() -> list[dict[str, Any]]:
    path = _preview_feedback_log_path()
    if not path.exists():
        return []
    try:
        return _preview_feedback_records_from_text(path.read_text(encoding="utf-8-sig", errors="ignore"))
    except Exception:
        return []


def _preview_feedback_stats() -> dict[str, Any]:
    path = _preview_feedback_log_path()
    records = _preview_feedback_load_records()
    role_counts: dict[str, int] = {}
    for record in records:
        roles = record.get("role_samples") if isinstance(record.get("role_samples"), dict) else {}
        for key, values in roles.items():
            if isinstance(values, list):
                role_counts[key] = role_counts.get(key, 0) + len(values)
    return {
        "exists": path.exists(),
        "path": str(path),
        "record_count": len(records),
        "size": path.stat().st_size if path.exists() else 0,
        "role_counts": role_counts,
    }


_PREVIEW_FEEDBACK_ROLE_LABELS = {
    "hook_positive": "常用开头",
    "hook_negative": "不要的开头",
    "close_positive": "常用结尾",
    "close_negative": "不要的结尾",
    "move_to_front": "常拖到前面",
    "move_to_end": "常拖到最后",
    "sentence_positive": "保留句子",
    "sentence_negative": "删除句子",
}


def _preview_feedback_sample_text(item: Any) -> str:
    if isinstance(item, dict):
        value = item.get("text") or ""
    else:
        value = item
    text = _strip_preview_source_marker(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _preview_feedback_role_items(record: dict[str, Any], role: str) -> list[Any]:
    roles = record.get("role_samples") if isinstance(record.get("role_samples"), dict) else {}
    values = roles.get(role)
    if isinstance(values, list):
        return list(values)
    if role == "sentence_positive":
        values = record.get("kept_texts")
        return list(values) if isinstance(values, list) else []
    if role == "sentence_negative":
        values = record.get("rejected_segment_texts")
        return list(values) if isinstance(values, list) else []
    return []


def _preview_feedback_sample_id(role: str, text: str) -> str:
    raw = f"{role}\0{_preview_compact_text(text)}\0{text}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _preview_feedback_samples(limit_per_role: int = 80) -> list[dict[str, Any]]:
    records = _preview_feedback_load_records()
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        created_at = float(record.get("created_at") or 0)
        scope = str(record.get("scope") or "")
        for role in _PREVIEW_FEEDBACK_ROLE_LABELS:
            for item in _preview_feedback_role_items(record, role):
                text = _preview_feedback_sample_text(item)
                if not text:
                    continue
                key = (role, text)
                entry = grouped.setdefault(key, {
                    "id": _preview_feedback_sample_id(role, text),
                    "role": role,
                    "label": _PREVIEW_FEEDBACK_ROLE_LABELS.get(role, role),
                    "text": text,
                    "count": 0,
                    "latest_at": 0.0,
                    "scopes": set(),
                })
                entry["count"] += 1
                entry["latest_at"] = max(float(entry.get("latest_at") or 0), created_at)
                if scope:
                    entry["scopes"].add(scope)
    result: list[dict[str, Any]] = []
    for role in _PREVIEW_FEEDBACK_ROLE_LABELS:
        items = [entry for entry in grouped.values() if entry.get("role") == role]
        items.sort(key=lambda entry: (int(entry.get("count") or 0), float(entry.get("latest_at") or 0)), reverse=True)
        for entry in items[:limit_per_role]:
            scopes = sorted(entry.pop("scopes", set()))
            entry["scopes"] = scopes
            result.append(entry)
    return result


_PREVIEW_FEEDBACK_POSITIVE_ROLES = {"hook_positive", "close_positive", "move_to_front", "move_to_end", "sentence_positive"}
_PREVIEW_FEEDBACK_NEGATIVE_ROLES = {"hook_negative", "close_negative", "sentence_negative"}
_PREVIEW_FEEDBACK_STRUCTURAL_AVOID_SIGNALS = {
    "host_chatter",
    "environment_noise",
    "inventory_pressure",
    "filler_or_fragment",
}

_PREVIEW_FEEDBACK_SIGNAL_RULES = [
    {
        "key": "color_benefit",
        "label": "颜色/显白卖点",
        "words": ["显白", "显肤", "肤亮", "颜色", "黑色", "白色", "绿色", "亮色", "米白", "饱和度", "冷白"],
        "summary": "颜色效果、显白显气色、颜色选择建议",
    },
    {
        "key": "fit_texture",
        "label": "版型/质感卖点",
        "words": ["显瘦", "质感", "面料", "版型", "细节", "袖子", "好穿", "舒服", "垂感", "高级"],
        "summary": "版型、面料、质感、穿着效果等具体产品价值",
    },
    {
        "key": "scene_styling",
        "label": "场景/搭配表达",
        "words": ["日常", "生活", "运动", "骑行", "拍照", "场景", "搭配", "出片", "穿搭", "黑白灰"],
        "summary": "适合什么场景、怎么搭配、为什么值得穿出去",
    },
    {
        "key": "objection_answer",
        "label": "购买顾虑解释",
        "words": ["不安心", "从来没有", "不敢", "不知道", "怕", "适合", "稳妥", "尝试", "口味", "惊喜"],
        "summary": "解释用户担心点，让犹豫用户更容易理解",
    },
    {
        "key": "emotional_hook",
        "label": "情绪/记忆点",
        "words": ["相信我", "惊喜", "记忆点", "风格", "气质", "性格", "值得", "好看", "宝宝"],
        "summary": "有情绪、有画面感、能让人记住的表达",
    },
    {
        "key": "host_chatter",
        "label": "主播闲聊/自嗨",
        "words": ["老粉", "拉黑", "划走", "催债", "催交", "不好意思", "听我讲话", "吹牛", "下次"],
        "summary": "主播个人情绪、闲聊、自嘲或威胁式表达",
    },
    {
        "key": "environment_noise",
        "label": "环境/直播间干扰",
        "words": ["直播间", "手机屏幕", "肉眼", "窗户", "光很亮", "帘子", "走远", "颜色比较对"],
        "summary": "灯光、屏幕、直播环境说明，容易偏离产品价值",
    },
    {
        "key": "inventory_pressure",
        "label": "库存/预售催促",
        "words": ["首批", "拼手速", "没了", "预售", "库存", "加完", "备货", "一点都没有"],
        "summary": "库存、预售、抢购催促类表达",
    },
    {
        "key": "filler_or_fragment",
        "label": "口头禅/断句",
        "words": ["来好了", "对然后", "能理解吗", "为什么", "呀对不对", "白开水", "因为我知道", "然后整个", "你看啊"],
        "summary": "无独立意义、承接不完整或明显断掉的句子",
    },
]


def _preview_feedback_confidence(count: int) -> str:
    if count >= 8:
        return "较强"
    if count >= 5:
        return "明显"
    if count >= 3:
        return "轻微"
    if count >= 1:
        return "观察中"
    return "无"


def _preview_feedback_normalize_strength(value: Any) -> str:
    text = str(value or "auto").strip().lower()
    aliases = {
        "自动": "auto",
        "auto": "auto",
        "关闭": "off",
        "关": "off",
        "off": "off",
        "false": "off",
        "轻度": "light",
        "light": "light",
        "标准": "standard",
        "standard": "standard",
        "强": "strong",
        "强力": "strong",
        "strong": "strong",
    }
    return aliases.get(text, "auto")


def _preview_feedback_configured_strength() -> str:
    try:
        settings = _load_settings()
        return _preview_feedback_normalize_strength(settings.get("style_profile_strength"))
    except Exception:
        return "auto"


def _preview_feedback_selection_enabled() -> bool:
    return _preview_feedback_configured_strength() != "off"


def _preview_feedback_learning_status(
    sample_count: int,
    configured_strength: str = "auto",
    selection_enabled: bool = True,
) -> tuple[str, str]:
    configured_strength = _preview_feedback_normalize_strength(configured_strength)
    if sample_count <= 0:
        status = "未开始"
    elif sample_count <= 2:
        status = "观察中"
    elif sample_count <= 9:
        status = "初步成型"
    else:
        status = "稳定画像"
    if not selection_enabled:
        return status, "不参与选片"
    if sample_count <= 0:
        return "未开始", "关闭" if configured_strength == "off" else "只读"
    if sample_count <= 2:
        return "观察中", "关闭" if configured_strength == "off" else "只读"
    if configured_strength == "off":
        return status, "关闭"
    if configured_strength == "light":
        return status, "轻度"
    if configured_strength == "standard":
        return status, "标准"
    if configured_strength == "strong":
        return status, "强"
    return status, "轻度" if sample_count <= 9 else "标准"


def _preview_feedback_latest_at(records: list[dict[str, Any]]) -> float:
    latest = 0.0
    for record in records:
        try:
            latest = max(latest, float(record.get("created_at") or 0))
        except Exception:
            pass
    return latest


def _preview_feedback_top_labels(items: list[dict[str, Any]], count_key: str, limit: int = 5) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items[:limit]:
        examples = item.get("positive_examples") or item.get("negative_examples") or []
        result.append({
            "key": item.get("key") or "",
            "label": item.get("label") or "",
            "count": int(item.get(count_key) or 0),
            "confidence": item.get("confidence") or "观察中",
            "examples": examples[:2] if isinstance(examples, list) else [],
        })
    return result


def _preview_feedback_role_signal_counts(records: list[dict[str, Any]], roles: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for role in roles:
            for item in _preview_feedback_role_items(record, role):
                text = _preview_feedback_sample_text(item)
                if not text:
                    continue
                for key in _preview_feedback_signal_keys(text):
                    counts[key] = counts.get(key, 0) + 1
    return counts


def _preview_feedback_style_metrics(records: list[dict[str, Any]]) -> dict[str, str]:
    kept_texts: list[str] = []
    rejected_texts: list[str] = []
    cta_positive = 0
    cta_negative = 0
    for record in records:
        for role in _PREVIEW_FEEDBACK_POSITIVE_ROLES:
            for item in _preview_feedback_role_items(record, role):
                text = _preview_feedback_sample_text(item)
                if text:
                    kept_texts.append(text)
                    if re.search(r"(拍|下单|入|闭眼|冲|链接|加购|买|拍下)", text):
                        cta_positive += 1
        for role in _PREVIEW_FEEDBACK_NEGATIVE_ROLES:
            for item in _preview_feedback_role_items(record, role):
                text = _preview_feedback_sample_text(item)
                if text:
                    rejected_texts.append(text)
                    if re.search(r"(拍|下单|入|闭眼|冲|链接|加购|买|拼手速|没了)", text):
                        cta_negative += 1

    avg_len = sum(len(_preview_compact_text(text)) for text in kept_texts) / max(1, len(kept_texts))
    short_ratio = sum(1 for text in kept_texts if len(_preview_compact_text(text)) <= 18) / max(1, len(kept_texts))
    explain_hits = sum(1 for text in kept_texts if any(word in text for word in ("因为", "所以", "适合", "如果", "你会发现", "原因")))
    emotion_hits = sum(1 for text in kept_texts if any(word in text for word in ("巨", "真的", "相信我", "惊喜", "好看", "宝宝")))

    return {
        "selling_density": "高" if short_ratio >= 0.45 else "中" if avg_len <= 38 else "低",
        "rhythm": "快" if short_ratio >= 0.45 else "中" if avg_len <= 45 else "慢",
        "context_length": "短" if explain_hits <= max(1, len(kept_texts) * 0.18) else "中" if explain_hits <= max(2, len(kept_texts) * 0.36) else "长",
        "emotion_strength": "高" if emotion_hits >= max(2, len(kept_texts) * 0.28) else "中" if emotion_hits else "低",
        "cta_strength": "弱" if cta_negative >= cta_positive else "中" if cta_positive else "弱",
    }


def _preview_feedback_style_profile(
    records: list[dict[str, Any]],
    positive: list[dict[str, Any]],
    negative: list[dict[str, Any]],
    total_samples: int,
) -> dict[str, Any]:
    record_count = len(records)
    configured_strength = _preview_feedback_configured_strength()
    selection_enabled = _preview_feedback_selection_enabled()
    status, impact = _preview_feedback_learning_status(total_samples, configured_strength, selection_enabled)
    latest_at = _preview_feedback_latest_at(records)
    hook_counts = _preview_feedback_role_signal_counts(records, {"hook_positive", "move_to_front"})
    ending_counts = _preview_feedback_role_signal_counts(records, {"close_positive", "move_to_end"})
    signal_labels = {str(rule["key"]): str(rule["label"]) for rule in _PREVIEW_FEEDBACK_SIGNAL_RULES}

    def _role_preferences(counts: dict[str, int], fallback: str) -> list[dict[str, Any]]:
        ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        if not ranked:
            return [{"label": fallback, "count": 0, "confidence": "观察中"}]
        return [
            {"label": signal_labels.get(key, key), "count": count, "confidence": _preview_feedback_confidence(count)}
            for key, count in ranked[:4]
        ]

    selling_preferences = _preview_feedback_top_labels(positive, "positive_count", 5)
    avoid_preferences = _preview_feedback_top_labels(negative, "negative_count", 5)
    metrics = _preview_feedback_style_metrics(records)

    summary: list[str] = []
    if selling_preferences:
        summary.append("中段优先：" + "、".join(item["label"] for item in selling_preferences[:4]) + "。")
    if avoid_preferences:
        summary.append("常避开：" + "、".join(item["label"] for item in avoid_preferences[:4]) + "。")
    hook_labels = [item["label"] for item in _role_preferences(hook_counts, "直接利益点开头") if item.get("count", 0)]
    if hook_labels:
        summary.append("开头偏好：" + "、".join(hook_labels[:3]) + "。")
    ending_labels = [item["label"] for item in _role_preferences(ending_counts, "自然总结") if item.get("count", 0)]
    if ending_labels:
        summary.append("结尾偏好：" + "、".join(ending_labels[:3]) + "。")
    if metrics:
        summary.append(f"节奏画像：卖点密度{metrics['selling_density']}，节奏{metrics['rhythm']}，上下文{metrics['context_length']}。")

    ai_hint_parts = []
    if selection_enabled:
        if selling_preferences:
            ai_hint_parts.append("优先选择" + "、".join(item["label"] for item in selling_preferences[:4]))
        if avoid_preferences:
            ai_hint_parts.append("避免" + "、".join(item["label"] for item in avoid_preferences[:4]))
        ai_hint_parts.append(f"剪辑节奏偏{metrics['rhythm']}，上下文长度偏{metrics['context_length']}，CTA强度偏{metrics['cta_strength']}")

    return {
        "name": "剪辑风格画像",
        "status": status,
        "impact": impact,
        "selection_enabled": selection_enabled,
        "configured_strength": configured_strength,
        "learned_records": record_count,
        "sample_count": total_samples,
        "latest_at": latest_at,
        "selling_preferences": selling_preferences,
        "avoid_preferences": avoid_preferences,
        "hook_preferences": _role_preferences(hook_counts, "直接利益点开头"),
        "ending_preferences": _role_preferences(ending_counts, "自然总结"),
        "metrics": metrics,
        "summary": summary,
        "ai_hint": "；".join(ai_hint_parts),
    }


def _preview_feedback_signal_keys(text: str) -> list[str]:
    compact = _preview_compact_text(text)
    keys: list[str] = []
    for rule in _PREVIEW_FEEDBACK_SIGNAL_RULES:
        for word in rule["words"]:
            if word in text or _preview_compact_text(word) in compact:
                keys.append(str(rule["key"]))
                break
    if len(compact) <= 5 and compact in {"对然后", "为什么", "来好了", "白开水", "能理解吗", "呀对不对"}:
        if "filler_or_fragment" not in keys:
            keys.append("filler_or_fragment")
    if re.search(r"(因为|然后|包括|或者|这个|整个|有一点|你看)$", text.strip()):
        if "filler_or_fragment" not in keys:
            keys.append("filler_or_fragment")
    return keys


def _preview_feedback_empty_summary() -> dict[str, Any]:
    return {
        "read_only": True,
        "confidence": "无",
        "style_profile": _preview_feedback_style_profile([], [], [], 0),
        "positive": [],
        "negative": [],
        "conflicts": [],
        "brief": [],
        "notes": ["样本还不够，先继续通过 AI 预览人工调整积累数据。"],
    }


def _preview_feedback_preference_summary() -> dict[str, Any]:
    records = _preview_feedback_load_records()
    if not records:
        return _preview_feedback_empty_summary()

    signal_map = {
        str(rule["key"]): {
            "key": str(rule["key"]),
            "label": str(rule["label"]),
            "summary": str(rule["summary"]),
            "positive_count": 0,
            "negative_count": 0,
            "positive_examples": [],
            "negative_examples": [],
        }
        for rule in _PREVIEW_FEEDBACK_SIGNAL_RULES
    }
    text_roles: dict[str, dict[str, Any]] = {}
    total_samples = 0

    for record in records:
        for role in _PREVIEW_FEEDBACK_ROLE_LABELS:
            polarity = "positive" if role in _PREVIEW_FEEDBACK_POSITIVE_ROLES else "negative" if role in _PREVIEW_FEEDBACK_NEGATIVE_ROLES else ""
            if not polarity:
                continue
            for item in _preview_feedback_role_items(record, role):
                text = _preview_feedback_sample_text(item)
                if not text:
                    continue
                total_samples += 1
                compact = _preview_compact_text(text)
                text_entry = text_roles.setdefault(compact, {"text": text, "positive": 0, "negative": 0})
                text_entry[polarity] += 1
                for key in _preview_feedback_signal_keys(text):
                    signal = signal_map.get(key)
                    if not signal:
                        continue
                    count_key = "positive_count" if polarity == "positive" else "negative_count"
                    example_key = "positive_examples" if polarity == "positive" else "negative_examples"
                    signal[count_key] = int(signal[count_key]) + 1
                    examples = signal[example_key]
                    if isinstance(examples, list) and text not in examples and len(examples) < 3:
                        examples.append(text)

    positive: list[dict[str, Any]] = []
    negative: list[dict[str, Any]] = []
    for signal in signal_map.values():
        key = str(signal.get("key") or "")
        pos = int(signal["positive_count"])
        neg = int(signal["negative_count"])
        if pos <= 0 and neg <= 0:
            continue
        net = pos - neg
        item = {
            **signal,
            "net": net,
            "confidence": _preview_feedback_confidence(abs(net)),
        }
        if key in _PREVIEW_FEEDBACK_STRUCTURAL_AVOID_SIGNALS:
            if neg > 0:
                item["net"] = -neg
                item["confidence"] = _preview_feedback_confidence(neg)
                negative.append(item)
            continue
        if net > 0:
            positive.append(item)
        elif net < 0:
            negative.append(item)

    positive.sort(key=lambda item: (int(item["net"]), int(item["positive_count"])), reverse=True)
    negative.sort(key=lambda item: (abs(int(item["net"])), int(item["negative_count"])), reverse=True)

    conflicts = []
    for entry in text_roles.values():
        if int(entry.get("positive") or 0) > 0 and int(entry.get("negative") or 0) > 0:
            conflicts.append(entry)
    conflicts.sort(key=lambda item: int(item.get("positive") or 0) + int(item.get("negative") or 0), reverse=True)

    brief = []
    if positive:
        labels = "、".join(item["label"] for item in positive[:4])
        brief.append(f"优先倾向：{labels}。")
    if negative:
        labels = "、".join(item["label"] for item in negative[:4])
        brief.append(f"谨慎避开：{labels}。")
    if conflicts:
        brief.append("存在正反都出现过的句子，不能按原文硬匹配，需要结合上下文。")

    confidence = _preview_feedback_confidence(total_samples)
    notes = [
        "这是只读摘要；画像会按所选影响强度进入 AI 软参考，选择关闭时只学习不参与选片。",
        "1-2 次样本只作为观察，建议累计到 3 次以上再作为稳定偏好。",
        "断句、闲聊、环境干扰、库存催促属于结构性风险，偶尔被保留也不会直接变成喜欢规则。",
    ]
    return {
        "read_only": True,
        "confidence": confidence,
        "sample_count": total_samples,
        "style_profile": _preview_feedback_style_profile(records, positive, negative, total_samples),
        "positive": positive[:6],
        "negative": negative[:6],
        "conflicts": conflicts[:8],
        "brief": brief,
        "notes": notes,
    }


def _preview_feedback_write_records(records: list[dict[str, Any]]) -> None:
    path = _preview_feedback_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _preview_feedback_backup_current(reason: str) -> str:
    path = _preview_feedback_log_path()
    if not path.exists():
        return ""
    backup = path.with_name(f"preview_selection_feedback_{reason}_{_stamp_name()}.jsonl")
    shutil.copy2(path, backup)
    return str(backup)


def _preview_feedback_delete_sample(role: str, text: str) -> dict[str, Any]:
    role = str(role or "").strip()
    text = _preview_feedback_sample_text(text)
    if role not in _PREVIEW_FEEDBACK_ROLE_LABELS or not text:
        raise HTTPException(status_code=400, detail="请选择要删除的喜好样本。")
    records = _preview_feedback_load_records()
    backup = _preview_feedback_backup_current("before_delete")
    removed = 0
    for record in records:
        roles = record.get("role_samples") if isinstance(record.get("role_samples"), dict) else {}
        role_values = roles.get(role)
        if isinstance(role_values, list):
            kept_values = []
            for item in role_values:
                if _preview_feedback_sample_text(item) == text:
                    removed += 1
                else:
                    kept_values.append(item)
            roles[role] = kept_values
            record["role_samples"] = roles
        legacy_key = "kept_texts" if role == "sentence_positive" else "rejected_segment_texts" if role == "sentence_negative" else ""
        if legacy_key and not isinstance(role_values, list) and isinstance(record.get(legacy_key), list):
            kept_values = []
            for item in record.get(legacy_key) or []:
                if _preview_feedback_sample_text(item) == text:
                    removed += 1
                else:
                    kept_values.append(item)
            record[legacy_key] = kept_values
    if removed:
        _preview_feedback_write_records(records)
    return {"removed_count": removed, "backup": backup}


def _preview_feedback_clear() -> dict[str, Any]:
    backup = _preview_feedback_backup_current("before_clear")
    path = _preview_feedback_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return {"backup": backup}


def _preview_feedback_merge_import(source_path: Path) -> dict[str, Any]:
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(status_code=404, detail="没有找到要导入的剪辑风格画像数据文件。")
    if source_path.stat().st_size > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="剪辑风格画像数据文件过大，请确认选择正确文件。")
    try:
        imported = _preview_feedback_records_from_text(source_path.read_text(encoding="utf-8-sig", errors="ignore"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取剪辑风格画像数据失败：{exc}") from exc
    if not imported:
        raise HTTPException(status_code=400, detail="文件里没有可导入的喜好记录。")

    target = _preview_feedback_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = _preview_feedback_load_records()
    if target.exists():
        backup = target.with_name(f"preview_selection_feedback_backup_{_stamp_name()}.jsonl")
        shutil.copy2(target, backup)
    else:
        backup = None

    seen = {_preview_feedback_record_key(record) for record in existing}
    added: list[dict[str, Any]] = []
    for record in imported:
        key = _preview_feedback_record_key(record)
        if key in seen:
            continue
        seen.add(key)
        added.append(record)
    if added:
        with target.open("a", encoding="utf-8") as handle:
            for record in added:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {
        "imported_count": len(imported),
        "added_count": len(added),
        "skipped_count": len(imported) - len(added),
        "backup": str(backup) if backup else "",
        "path": str(target),
    }


def _record_preview_selection_feedback(
    preview: dict[str, Any],
    scope: str,
    draft: dict[str, Any],
    event: str,
) -> None:
    try:
        record = _build_preview_selection_feedback(preview, scope, draft, event)
        kept_count = int(record.get("kept_segment_count") or 0)
        rejected_count = int(record.get("rejected_segment_count") or 0)
        if kept_count <= 0 and rejected_count <= 0:
            return
        _append_preview_selection_feedback(record)
        emit_log(
            "info",
            f"AI偏好反馈: 已记录本次人工选择，保留 {kept_count} 句、删除 {rejected_count} 句；后续自动选片会参考。",
            scope,
        )
    except Exception as exc:
        emit_log("warning", f"AI偏好反馈记录失败: {exc}", scope)


def _clean_preview_int_list(values: Any, limit: int | None = None) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values if isinstance(values, list) else []:
        try:
            number = int(value)
        except Exception:
            continue
        if number < 0 or number in seen:
            continue
        if limit is not None and number >= limit:
            continue
        seen.add(number)
        result.append(number)
    return result


def _preview_selection_draft(preview: dict[str, Any] | None) -> dict[str, Any]:
    draft = (preview or {}).get("selection_draft")
    return dict(draft) if isinstance(draft, dict) else {}


def _normalize_preview_selection_draft(
    preview: dict[str, Any],
    scope: str,
    selected_indices: list[int] | None,
    selected_segments: dict[str, list[int]] | None,
    order: list[int] | None = None,
    updated_at: float | None = None,
) -> dict[str, Any]:
    raw_count = max(len(preview.get("raw_clips") or []), len(preview.get("clips") or []))
    selected = _clean_preview_int_list(selected_indices or [], raw_count)
    selected_set = set(selected)
    ordered = _clean_preview_int_list(order or [], raw_count)
    ordered.extend(index for index in range(raw_count) if index not in set(ordered))
    public_clips = {
        int(clip.get("index")): clip
        for clip in list(preview.get("clips") or [])
        if str(clip.get("index", "")).lstrip("-").isdigit()
    }
    raw_segment_map = selected_segments if isinstance(selected_segments, dict) else {}
    normalized_segments: dict[str, list[int]] = {}
    for index in selected:
        public_clip = public_clips.get(index) or {}
        segment_count = len(public_clip.get("segments") or [])
        values = raw_segment_map.get(str(index))
        if values is None:
            values = raw_segment_map.get(index) if isinstance(raw_segment_map, dict) else None
        if segment_count and isinstance(values, list):
            kept = _clean_preview_int_list(values, segment_count)
            if kept:
                normalized_segments[str(index)] = kept
            else:
                selected_set.discard(index)
    selected = [index for index in selected if index in selected_set]
    return {
        "preview_id": str(preview.get("id") or ""),
        "scope": scope,
        "order": ordered,
        "selected_indices": selected,
        "selected_segments": normalized_segments,
        "updated_at": float(updated_at or time.time() * 1000),
    }


def _apply_preview_payload_draft(
    preview_id: str,
    preview: dict[str, Any],
    scope: str,
    selected_indices: list[int],
    selected_segments: dict[str, list[int]] | None,
    order: list[int] | None = None,
) -> dict[str, Any]:
    draft = _normalize_preview_selection_draft(
        preview,
        scope,
        selected_indices,
        selected_segments,
        order=order,
    )
    _store_preview(preview_id, selection_draft=draft)
    return draft


def _preview_public(preview: dict[str, Any] | None) -> dict[str, Any]:
    if not preview:
        return {"ok": False, "status": "missing", "message": "暂无选片预览。"}
    return {
        "ok": preview.get("status") == "ready",
        "id": preview.get("id", ""),
        "task_id": preview.get("task_id", ""),
        "scope": preview.get("scope", "smart-cut"),
        "status": preview.get("status", ""),
        "message": preview.get("message", ""),
        "error": preview.get("error", ""),
        "video": preview.get("video", ""),
        "video_name": preview.get("video_name", ""),
        "sources": preview.get("sources", []),
        "srt_path": preview.get("srt_path", ""),
        "target_duration": preview.get("target_duration", 0),
        "created_at": preview.get("created_at", 0),
        "clips": preview.get("clips", []),
        "selection_draft": preview.get("selection_draft", {}),
        "dedup_summary": preview.get("dedup_summary", {}),
    }


def _store_preview(preview_id: str, **updates: Any) -> None:
    with _CLIP_PREVIEW_LOCK:
        current = _CLIP_PREVIEWS.setdefault(preview_id, {"id": preview_id})
        current.update(updates)


def _get_preview(preview_id: str) -> dict[str, Any] | None:
    with _CLIP_PREVIEW_LOCK:
        preview = _CLIP_PREVIEWS.get(preview_id)
        return dict(preview) if preview else None


def _latest_preview(scope: str) -> dict[str, Any] | None:
    with _CLIP_PREVIEW_LOCK:
        previews = [
            p for p in _CLIP_PREVIEWS.values()
            if p.get("scope") == scope and float(p.get("created_at", 0) or 0) > _PREVIEW_CLEARED_AT
        ]
        if not previews:
            return None
        return dict(max(previews, key=lambda item: item.get("created_at", 0)))


def _preview_clip_source(preview: dict[str, Any], clip_info: dict[str, Any]) -> Path:
    clip_source = str(clip_info.get("source") or "").strip()
    if not clip_source and preview.get("scope") == "mix":
        clip_source = _preview_marker_source(list(preview.get("sources") or []), clip_info.get("text") or "")
    video = Path(clip_source) if clip_source else Path(str(preview.get("video", "")))
    if not video.exists():
        raise HTTPException(status_code=404, detail="源视频不存在，请重新选择视频。")
    try:
        from cutter_logic import _remux_ts_for_editing

        cut_video = Path(_remux_ts_for_editing(str(video), None, _ffmpeg_cmd(), None))
    except Exception:
        cut_video = video
    if not cut_video.exists():
        raise HTTPException(status_code=404, detail="片段预览源视频不存在，请重新生成预览。")
    return cut_video


def _preview_clip_parts_for_video(
    preview: dict[str, Any],
    clip_index: int,
    draft: dict[str, Any] | None,
) -> list[Any]:
    raw_clips = list(preview.get("raw_clips") or [])
    if clip_index < 0 or clip_index >= len(raw_clips):
        raise HTTPException(status_code=404, detail="片段不存在，请重新生成预览。")

    public_clips = {
        int(clip.get("index")): clip
        for clip in list(preview.get("clips") or [])
        if str(clip.get("index", "")).lstrip("-").isdigit()
    }
    public_clip = public_clips.get(clip_index) or _clip_public(clip_index, raw_clips[clip_index])
    has_segments = bool(public_clip.get("segments"))
    draft = draft if isinstance(draft, dict) else {}
    has_draft_selection = isinstance(draft.get("selected_indices"), list) and bool(draft.get("selected_indices"))
    if has_draft_selection:
        selected = _clean_preview_int_list(draft.get("selected_indices") or [], len(raw_clips))
        if clip_index not in selected:
            raise HTTPException(status_code=400, detail="这个片段当前未选中，勾选后再预览。")
        if has_segments:
            parts = _clips_from_preview_selection(
                preview,
                [clip_index],
                draft.get("selected_segments") if isinstance(draft.get("selected_segments"), dict) else {},
            )
            if not parts:
                raise HTTPException(status_code=400, detail="这个片段没有选中的句子，勾选后再预览。")
            return parts
    return [raw_clips[clip_index]]


def _preview_clip_cache_signature(preview_parts: list[Any], source_sig: str) -> str:
    pieces = [source_sig]
    for part in preview_parts:
        info = _clip_public(0, part)
        pieces.append(
            f"{info.get('source') or ''}|{float(info.get('start') or 0):.3f}|"
            f"{float(info.get('end') or 0):.3f}|{info.get('text') or ''}"
        )
    return hashlib.md5("||".join(pieces).encode("utf-8", errors="ignore")).hexdigest()[:12]


def _render_preview_clip_range(cut_video: Path, start: float, duration: float, target: Path) -> None:
    input_seek = max(0.0, start - 2.0)
    output_seek = max(0.0, start - input_seek)
    cmd = [
        _ffmpeg_cmd(),
        "-y",
        "-fflags",
        "+genpts",
        "-ss",
        f"{input_seek:.3f}",
        "-i",
        str(cut_video),
    ]
    if output_seek > 0.001:
        cmd += ["-ss", f"{output_seek:.3f}"]
    cmd += [
        "-t",
        f"{duration:.3f}",
        "-avoid_negative_ts",
        "make_zero",
        "-vf",
        "scale='min(480,iw)':-2,scale=trunc(iw/2)*2:trunc(ih/2)*2,setpts=PTS-STARTPTS",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "26",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-af",
        "aresample=async=1:first_pts=0",
        "-movflags",
        "+faststart",
        str(target),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=500, detail="生成片段预览超时，请稍后重试。") from exc
    if proc.returncode != 0 or not target.exists() or target.stat().st_size < 1000:
        err = (proc.stderr or "").strip().splitlines()[-3:]
        detail = "; ".join(err) if err else "FFmpeg 未生成有效预览文件。"
        raise HTTPException(status_code=500, detail=f"生成片段预览失败：{detail}")


def _concat_preview_clip_parts(part_paths: list[Path], target: Path) -> None:
    list_path = target.with_suffix(".concat.txt")

    def _entry(path: Path) -> str:
        text = str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
        return f"file '{text}'"

    list_path.write_text("\n".join(_entry(path) for path in part_paths) + "\n", encoding="utf-8")
    cmd = [
        _ffmpeg_cmd(),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=500, detail="合并片段预览超时，请稍后重试。") from exc
    if proc.returncode != 0 or not target.exists() or target.stat().st_size < 1000:
        err = (proc.stderr or "").strip().splitlines()[-3:]
        detail = "; ".join(err) if err else "FFmpeg 未生成有效预览文件。"
        raise HTTPException(status_code=500, detail=f"合并片段预览失败：{detail}")


def _preview_clip_video(
    preview_id: str,
    clip_index: int,
    selected_indices: list[int] | None = None,
    selected_segments: dict[str, list[int]] | None = None,
    order: list[int] | None = None,
    scope: str = "smart",
    updated_at: float | None = None,
) -> Path:
    preview = _get_preview(preview_id)
    if not preview or preview.get("status") != "ready":
        raise HTTPException(status_code=404, detail="AI 选片预览不存在或尚未完成。")
    raw_clips = list(preview.get("raw_clips") or [])
    if clip_index < 0 or clip_index >= len(raw_clips):
        raise HTTPException(status_code=404, detail="片段不存在，请重新生成预览。")

    has_payload_draft = bool(selected_indices or selected_segments or order)
    if has_payload_draft:
        draft = _normalize_preview_selection_draft(
            preview,
            "mix" if scope == "mix" or preview.get("scope") == "mix" else "smart",
            selected_indices or [],
            selected_segments or {},
            order=order or [],
            updated_at=updated_at,
        )
        _store_preview(preview_id, selection_draft=draft)
        if clip_index not in _clean_preview_int_list(draft.get("selected_indices") or [], len(raw_clips)):
            raise HTTPException(status_code=400, detail="这个片段当前未选中，勾选后再预览。")
    else:
        draft = _preview_selection_draft(preview)

    preview_parts = _preview_clip_parts_for_video(preview, clip_index, draft)
    clip_infos = [_clip_public(clip_index, part) for part in preview_parts]
    ranges: list[tuple[Path, float, float]] = []
    total_duration = 0.0
    source_sig_parts: list[str] = []
    for info in clip_infos:
        cut_video = _preview_clip_source(preview, info)
        start = max(0.0, float(info.get("start") or 0.0))
        end = max(start + 0.2, float(info.get("end") or start + 0.2))
        duration = max(0.2, end - start)
        ranges.append((cut_video, start, duration))
        total_duration += duration
        try:
            cut_stat = cut_video.stat()
            source_sig_parts.append(f"{cut_video.resolve()}|{cut_stat.st_size}|{int(cut_stat.st_mtime)}")
        except Exception:
            source_sig_parts.append(str(cut_video))
    if total_duration > 90:
        raise HTTPException(status_code=400, detail="片段过长，暂不支持在线预览。")

    preview_dir = _safe_user_child("clip_previews", preview_id)
    preview_dir.mkdir(parents=True, exist_ok=True)
    source_sig = hashlib.md5("||".join(source_sig_parts).encode("utf-8", errors="ignore")).hexdigest()[:8]
    selection_sig = _preview_clip_cache_signature(preview_parts, source_sig)
    stamp = f"{clip_index}_{selection_sig}"
    target = preview_dir / f"clip_{stamp}_{source_sig}.mp4"
    if target.exists() and target.stat().st_size > 1000:
        return target

    if len(ranges) == 1:
        cut_video, start, duration = ranges[0]
        _render_preview_clip_range(cut_video, start, duration, target)
        return target

    part_paths: list[Path] = []
    for part_index, (cut_video, start, duration) in enumerate(ranges):
        part_target = preview_dir / f"clip_{stamp}_{source_sig}_part{part_index}.mp4"
        if not part_target.exists() or part_target.stat().st_size <= 1000:
            _render_preview_clip_range(cut_video, start, duration, part_target)
        part_paths.append(part_target)
    _concat_preview_clip_parts(part_paths, target)
    return target


def _ensure_srt(video: Path, scope: str) -> Path | None:
    srt = video.with_suffix(".srt")
    if srt.exists():
        emit_log("info", f"找到字幕：{srt.name}", scope)
        return srt

    settings = _load_settings()
    if settings.get("asr_enabled", False):
        cloud_srt = _try_volcengine_srt(video, srt, settings, scope)
        if cloud_srt:
            return cloud_srt
        emit_log("warning", "云端语音识别失败，已切换到本地语音识别。", scope)
    else:
        emit_log("info", "未启用云端语音识别，正在使用本地语音识别。", scope)

    emit_log("info", f"{video.name} 开始本地语音识别。", scope)
    try:
        from stt import generate_srt

        generated = generate_srt(str(video), log_fn=lambda msg: emit_log("info", msg, scope))
        if generated and Path(generated).exists():
            emit_log("info", "本地语音识别完成。", scope)
            return Path(generated)
        return None
    except Exception as exc:
        emit_log("error", f"字幕生成失败：{exc}", scope)
        return None


def _try_volcengine_srt(video: Path, srt: Path, settings: dict[str, Any], scope: str) -> Path | None:
    tos_ak = settings.get("volc_tos_ak", "") or ""
    tos_sk = settings.get("volc_tos_sk", "") or ""
    app_id = settings.get("volc_app_id", "") or ""
    access_token = settings.get("volc_access_token", "") or ""
    api_key = settings.get("volc_api_key", "") or ""
    bucket = settings.get("volc_bucket", "livec") or "livec"
    region = _normalize_volc_region(settings.get("volc_region"))
    if not all([tos_ak, tos_sk]) or not (api_key or all([app_id, access_token])):
        emit_log("warning", "云端语音识别未配置完整，正在使用本地语音识别。", scope)
        return None

    import hashlib
    import tempfile

    emit_log("info", "正在使用云端语音识别。", scope)
    temp_dir = Path(tempfile.gettempdir()) / "live_cutter_web_asr"
    temp_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.md5(str(video).encode("utf-8", errors="ignore")).hexdigest()[:12]
    audio_path: Path | None = None
    try:
        from volcengine_asr import prepare_volcengine_audio, volcengine_asr, write_word_timing_sidecar

        prepared = prepare_volcengine_audio(
            str(video),
            str(temp_dir),
            prefix=f"audio_{digest}_{int(time.time())}",
            ffmpeg=_ffmpeg_cmd(),
            log_fn=lambda msg: emit_log("info", msg, scope),
        )
        if not prepared:
            emit_log("warning", "云端语音识别准备音频失败，正在使用本地语音识别。", scope)
            return None
        audio_path = Path(prepared)

        segments = volcengine_asr(
            str(audio_path),
            app_id,
            access_token,
            tos_ak,
            tos_sk,
            bucket=bucket,
            region=region,
            log_fn=lambda msg: emit_log("info", msg, scope),
            api_key=api_key or None,
        )
        if not segments:
            return None
        srt_text = _segments_to_srt(segments)
        if not srt_text.strip():
            return None
        srt.write_text(srt_text, encoding="utf-8")
        write_word_timing_sidecar(
            srt,
            segments,
            log_fn=lambda msg: emit_log("info", msg, scope),
        )
        emit_log("info", f"云端语音识别成功：{len(segments)} 条语音段。", scope)
        return srt
    except Exception as exc:
        emit_log("warning", f"云端语音识别异常: {type(exc).__name__}: {exc}", scope)
        return None
    finally:
        try:
            if audio_path:
                audio_path.unlink(missing_ok=True)
        except Exception:
            pass


def _product_scanner():
    from ai_clipper import load_settings
    from product_scanner import ProductScanner

    settings = load_settings()
    return ProductScanner(
        api_key=settings.get("api_key", ""),
        base_url=settings.get("base_url", DEEPSEEK_DEFAULT_BASE_URL),
        model=settings.get("model", DEEPSEEK_DEFAULT_MODEL),
    )


def _run_mix(task_id: str, payload: MixPayload) -> None:
    scope = "mix"
    _set_task(task_id, status="running", started_at=time.time(), progress=5, message="准备混剪")
    try:
        _ensure_feature_access("混剪成片")
        from cutter_logic import process_video_mix

        paths = _existing_paths(payload.video_paths, "视频")
        _set_task_progress(task_id, 10, f"校验 {len(paths)} 个素材")
        out_dir = _default_output_dir(paths[0], payload.output_dir, "mix_output")
        output_path = _mix_output_path(out_dir, paths[0])
        pip_path, used_pip_file = _pick_pip_asset(payload, scope)
        emit_log("info", f"混剪开始：{len(paths)} 个视频，版本数={payload.versions}，目标时长={payload.duration}秒，输出 {output_path}", scope)
        _set_task_progress(task_id, 18, "分析并编排混剪")
        result = process_video_mix(
            [str(p) for p in paths],
            output_path=str(output_path),
            dedup_preset=payload.dedup_preset,
            dedup_video_options=payload.video,
            dedup_audio_options=payload.audio,
            transition_options=payload.transition,
            subtitle_overlay=payload.subtitle_overlay,
            log_fn=_task_log_fn(task_id, scope, base=18, span=70),
            cancel_event=_task_cancel_event(task_id),
            force_category=None if payload.category in ("", "自动检测", "自动检测") else payload.category,
            focus_hint=payload.focus_hint,
            ai_controls=payload.ai_controls,
            target_duration=payload.duration,
            num_versions=payload.versions,
            pip_path=pip_path or "",
            pip_size=payload.pip_size,
            pip_opacity=payload.pip_opacity,
            pip_pos=payload.pip_pos,
            smart_crop_enabled=payload.smart_crop_enabled,
            crop_level=payload.crop_level,
            ken_burns_enabled=payload.ken_burns_enabled,
            mirror_enabled=payload.mirror_enabled,
            kb_intensity=payload.ken_burns_intensity,
        )
        if not result:
            raise RuntimeError("混剪处理失败。")
        _set_task_progress(task_id, 94, "整理输出")
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        _archive_used_pip(used_pip_file, scope)
        _consume_trial("混剪成片", scope=scope)
        _set_task(
            task_id,
            status="completed",
            finished_at=time.time(),
            output=str(output_path),
            outputs=[str(output_path)],
            result_count=1,
        )
        emit_log("success", f"混剪成片完成：{output_path}", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"混剪成片失败：{exc}", scope)


def _run_mix_batch(task_id: str, payload: MixBatchPayload) -> None:
    scope = "mix"
    _set_task(task_id, status="running", started_at=time.time(), progress=5, message="准备批量混剪")
    try:
        _ensure_feature_access("混剪成片")
        from cutter_logic import process_video_mix

        groups: list[tuple[str, list[Path]]] = []
        for index, group in enumerate(payload.groups, start=1):
            paths = _existing_paths(group.video_paths, f"第 {index} 组视频")
            name = (group.name or "").strip() or f"第{index}组"
            groups.append((name, paths))
        if not groups:
            raise ValueError("请至少保存 1 个混剪素材组。")

        total_groups = len(groups)
        outputs: list[str] = []
        failures: list[str] = []
        _set_task(task_id, batch_total=total_groups, batch_done=0, batch_current=1, batch_failed=0, batch_label="")
        emit_log("info", f"批量混剪开始：{total_groups} 组，目标时长={payload.duration}秒", scope)

        for index, (group_name, paths) in enumerate(groups, start=1):
            if _is_task_cancelled(task_id):
                emit_log("warning", "批量混剪已停止。", scope)
                return
            group_base = 10 + ((index - 1) / total_groups) * 84
            group_span = 84 / total_groups
            _set_task_progress(task_id, group_base, f"处理 {index}/{total_groups}: {group_name}")

            out_dir = _default_output_dir(paths[0], payload.output_dir, "mix_output")
            output_path = _mix_output_path(out_dir, paths[0])
            output_path = output_path.with_name(f"{output_path.stem}_g{index:02d}{output_path.suffix}")
            pip_path, used_pip_file = _pick_pip_asset(payload, scope)
            emit_log("info", f"混剪批量 [{index}/{total_groups}] 开始：{group_name}，{len(paths)} 个视频，输出 {output_path}", scope)
            try:
                result = process_video_mix(
                    [str(p) for p in paths],
                    output_path=str(output_path),
                    dedup_preset=payload.dedup_preset,
                    dedup_video_options=payload.video,
                    dedup_audio_options=payload.audio,
                    transition_options=payload.transition,
                    subtitle_overlay=payload.subtitle_overlay,
                    log_fn=_task_log_fn(task_id, scope, base=group_base + 5, span=max(8, group_span * 0.88)),
                    cancel_event=_task_cancel_event(task_id),
                    force_category=None if payload.category in ("", "自动检测", "自动检测") else payload.category,
                    focus_hint=payload.focus_hint,
                    ai_controls=payload.ai_controls,
                    target_duration=payload.duration,
                    num_versions=payload.versions,
                    pip_path=pip_path or "",
                    pip_size=payload.pip_size,
                    pip_opacity=payload.pip_opacity,
                    pip_pos=payload.pip_pos,
                    smart_crop_enabled=payload.smart_crop_enabled,
                    crop_level=payload.crop_level,
                    ken_burns_enabled=payload.ken_burns_enabled,
                    mirror_enabled=payload.mirror_enabled,
                    kb_intensity=payload.ken_burns_intensity,
                )
                if not result:
                    raise RuntimeError("混剪处理失败。")
                _archive_used_pip(used_pip_file, scope)
                outputs.append(str(output_path))
                _set_task_progress(task_id, min(94, 10 + (index / total_groups) * 84), f"完成 {index}/{total_groups}: {group_name}")
                emit_log("success", f"混剪批量 [{index}/{total_groups}] 完成：{group_name}", scope)
            except Exception as group_exc:
                failures.append(f"第 {index} 组 {group_name}: {group_exc}")
                _set_task_progress(task_id, min(94, 10 + (index / total_groups) * 84), f"跳过失败 {index}/{total_groups}: {group_name}")
                _set_task(task_id, batch_failed=len(failures))
                emit_log("error", f"混剪批量 [{index}/{total_groups}] 失败：{group_name}，{group_exc}", scope)

        if not outputs:
            raise RuntimeError("批量混剪没有成功输出。")
        _consume_trial("混剪成片", units=len(outputs), scope=scope)
        _set_task(
            task_id,
            status="completed",
            finished_at=time.time(),
            output=outputs[0],
            outputs=outputs,
            result_count=len(outputs),
            message=f"批量混剪完成：成功 {len(outputs)}/{total_groups} 组",
            batch_total=total_groups,
            batch_done=len(outputs),
            batch_current=0,
            batch_failed=len(failures),
            batch_label="",
        )
        if failures:
            emit_log("warning", f"批量混剪完成：成功 {len(outputs)}/{total_groups} 组，失败 {len(failures)} 组。", scope)
        else:
            emit_log("success", f"批量混剪完成：成功 {len(outputs)} 组。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"批量混剪失败：{exc}", scope)


def _run_mix_preview(task_id: str, preview_id: str, payload: MixPayload) -> None:
    scope = "mix"
    _set_task(task_id, status="running", started_at=time.time(), progress=6, message="准备混剪 AI 预览")
    _store_preview(
        preview_id,
        task_id=task_id,
        scope=scope,
        status="running",
        message="正在生成混剪 AI 选片预览。",
        created_at=time.time(),
        category=payload.category,
        feedback_scope=_feedback_scope_key("mix", payload.category),
        target_duration=payload.duration,
        clips=[],
    )
    emit_log("info", "混剪 AI 选片预览任务已启动。", scope)
    try:
        _ensure_feature_access("混剪成片")
        import cutter_logic as cutter_mod
        from cutter_logic import process_video_mix

        paths = _existing_paths(payload.video_paths, "视频")
        _set_task_progress(task_id, 12, "校验素材")
        out_dir = _default_output_dir(paths[0], payload.output_dir, "mix_output")
        preview_output = str(out_dir / f"mix_preview_placeholder_{int(time.time())}.mp4")
        cutter_mod._multi_result_cache = {}
        _set_task_progress(task_id, 18, "分析多素材")
        result = process_video_mix(
            [str(p) for p in paths],
            output_path=preview_output,
            dedup_preset=payload.dedup_preset,
            subtitle_overlay=payload.subtitle_overlay,
            log_fn=_task_log_fn(task_id, scope, base=18, span=66),
            cancel_event=_task_cancel_event(task_id),
            force_category=None if payload.category in ("", "自动检测") else payload.category,
            focus_hint=payload.focus_hint,
            ai_controls=payload.ai_controls,
            target_duration=payload.duration,
            num_versions=payload.versions,
            pip_path="",
            pip_size=payload.pip_size,
            pip_opacity=payload.pip_opacity,
            pip_pos=payload.pip_pos,
            smart_crop_enabled=payload.smart_crop_enabled,
            crop_level=payload.crop_level,
            ken_burns_enabled=payload.ken_burns_enabled,
            mirror_enabled=payload.mirror_enabled,
            kb_intensity=payload.ken_burns_intensity,
            _clips_only=True,
        )
        if not result:
            raise RuntimeError("混剪 AI 选片预览失败。")
        _set_task_progress(task_id, 86, "整理候选片段")
        raw_clips = list(cutter_mod._multi_result_cache.get("clips") or [])
        if not raw_clips:
            raise RuntimeError("AI 没有选到可预览片段。")
        mix_sources = [str(source) for source in list(cutter_mod._multi_result_cache.get("sources") or []) if str(source or "").strip()]
        if not mix_sources:
            mix_sources = [str(path) for path in paths]
        category_summary = dict(cutter_mod._multi_result_cache.get("category_summary") or {})
        preference_summary = dict(cutter_mod._multi_result_cache.get("preference_summary") or {})
        topic_coverage_summary = dict(cutter_mod._multi_result_cache.get("topic_coverage_summary") or {})
        word_timings = list(cutter_mod._multi_result_cache.get("word_timings") or [])
        preferred_category = payload.category if payload.category not in ("", "自动检测", "自动") else str(category_summary.get("main_category") or "")
        raw_clips, dedup_summary = _normalize_preview_final_clips(
            raw_clips,
            str(cutter_mod._multi_result_cache.get("srt_text") or ""),
            merge_mode=True,
            preferred_category=preferred_category,
            preferred_focus=str(preference_summary.get("used_label") or preference_summary.get("label") or ""),
            word_timings=word_timings,
            target_duration=payload.duration,
        )
        raw_clips = _attach_mix_sources_to_preview_clips(raw_clips, mix_sources)
        try:
            import ai_clipper as _topic_ai
            topic_coverage_summary = dict(
                _topic_ai._topic_coverage_summary(
                    raw_clips,
                    str(preference_summary.get("used_label") or preference_summary.get("label") or ""),
                    payload.duration,
                    preference_summary.get("requested", "自动"),
                )
                or {}
            )
        except Exception:
            pass
        if category_summary:
            dedup_summary["category_summary"] = category_summary
        if preference_summary:
            dedup_summary["preference_summary"] = preference_summary
        if topic_coverage_summary:
            dedup_summary["topic_coverage_summary"] = topic_coverage_summary
        srt_text = str(cutter_mod._multi_result_cache.get("srt_text") or "")
        public_clips = _preview_public_clips(
            raw_clips,
            srt_text,
            sources=mix_sources,
            merge_mode=True,
            word_timings=word_timings,
        )
        raw_clips, public_clips, unusable_removed = _drop_unusable_preview_clips(raw_clips, public_clips)
        if unusable_removed:
            dedup_summary["unusable_preview_clips_removed"] = unusable_removed
        dedup_summary["narrative_owner"] = "ai"
        dedup_summary["preference_supplemented"] = 0
        emit_log("info", "AI叙事模式: 预览保持AI原始顺序，不做主题补片或角色重排。", scope)
        preview_quality = _preview_quality_summary(public_clips)
        if preview_quality["preview_locked_segments"] or preview_quality["preview_auto_unselected_segments"]:
            dedup_summary.update(preview_quality)
            emit_log(
                "info",
                "预览子句清理: "
                f"锁定{preview_quality['preview_locked_segments']}句安全内容，"
                f"自动取消{preview_quality['preview_auto_unselected_segments']}句重复/残句。",
                scope,
            )
        effective_topic_summary = _preview_effective_topic_coverage(
            raw_clips,
            public_clips,
            str(preference_summary.get("used_label") or preference_summary.get("label") or ""),
            payload.duration,
            preference_summary.get("requested", "自动"),
        )
        if effective_topic_summary:
            dedup_summary["topic_coverage_summary"] = effective_topic_summary
        _set_task_progress(task_id, 94, "生成预览列表")
        dedup_summary.update(_annotate_preview_manual_repeats(public_clips))
        _store_preview(
            preview_id,
            status="ready",
            message=f"混剪 AI 选片预览完成，共 {len(public_clips)} 个片段。",
            video=str(paths[0]),
            video_name=paths[0].name,
            sources=mix_sources,
            category=preferred_category,
            feedback_scope=_feedback_scope_key("mix", preferred_category),
            target_duration=payload.duration,
            srt_text=srt_text,
            raw_clips=raw_clips,
            clips=public_clips,
            dedup_summary=dedup_summary,
        )
        _set_task(task_id, status="completed", finished_at=time.time())
        emit_log("success", f"混剪 AI 选片预览完成：{len(public_clips)} 个片段。", scope)
    except Exception as exc:
        _store_preview(preview_id, status="failed", error=str(exc), message="混剪 AI 选片预览失败。")
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"混剪 AI 选片预览失败：{exc}", scope)


def _run_ai_scan(task_id: str, payload: AiScanPayload) -> None:
    scope = "ai-scan"
    _set_task(task_id, status="running", started_at=time.time(), progress=5, message="准备 AI 扫描")
    try:
        _ensure_feature_access("AI扫描")
        scanner = _product_scanner()
        paths = _existing_paths(payload.video_paths, "视频")
        total_videos = max(1, len(paths))
        _set_task_progress(task_id, 8, f"准备扫描 {total_videos} 个视频")
        out_dir = _default_output_dir(paths[0], payload.output_dir, "scan_output") if payload.output_dir.strip() else None
        all_file_products: list[list[dict[str, Any]]] = []
        flat_products: list[dict[str, Any]] = []
        for index, video in enumerate(paths, start=1):
            if _is_task_cancelled(task_id):
                emit_log("warning", "任务已停止。", scope)
                return
            emit_log("info", f"[{index}/{len(paths)}] 扫描 {video.name}", scope)
            file_base = 10 + ((index - 1) / total_videos) * 78
            file_span = 78 / total_videos
            _set_task_progress(task_id, file_base, f"扫描 {index}/{total_videos}: {video.name}")
            srt = _ensure_srt(video, scope)
            if not srt:
                continue
            products = scanner.scan(str(srt), log_fn=_task_log_fn(task_id, scope, base=file_base + 5, span=max(8, file_span * 0.65)))
            for product in products:
                product["_video"] = str(video)
            all_file_products.append(products)
            flat_products.extend(products)
            emit_log("success" if products else "warning", f"{video.name} 发现 {len(products)} 个单品。", scope)
            _set_task_progress(task_id, min(90, 10 + (index / total_videos) * 78), f"完成扫描 {index}/{total_videos}: {video.name}")
            if payload.auto_export and out_dir and products:
                _set_task_progress(task_id, 92, "导出扫描片段")
                for product in products:
                    safe_name = _clean_forbidden_title_text(product.get("name") or "product", fallback="product")
                    paths_out = scanner.extract_clip(str(video), product, str(out_dir), safe_name)
                    if paths_out:
                        emit_log("success", f"已导出：{safe_name} ({len(paths_out)} 段)", scope)
        with _SCAN_LOCK:
            _SCAN_RESULTS["products"] = all_file_products
            _SCAN_RESULTS["flat"] = flat_products
            _SCAN_RESULTS["merged"] = []
        if flat_products:
            _consume_trial("AI扫描", scope=scope)
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        _set_task(task_id, status="completed", finished_at=time.time(), result_count=len(flat_products))
        emit_log("success", f"AI 扫描完成，共 {len(flat_products)} 个单品。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"AI扫描失败：{exc}", scope)


def _run_ai_scan_merge(task_id: str) -> None:
    scope = "ai-scan"
    _set_task(task_id, status="running", started_at=time.time(), progress=8, message="准备跨文件合并")
    try:
        from product_scanner import merge_across_files

        with _SCAN_LOCK:
            products = list(_SCAN_RESULTS.get("products") or [])
        _set_task_progress(task_id, 35, "合并单品结果")
        merged = merge_across_files(products, log_fn=_task_log_fn(task_id, scope, base=35, span=52))
        with _SCAN_LOCK:
            _SCAN_RESULTS["merged"] = merged
        _set_task_progress(task_id, 94, "保存合并结果")
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        _set_task(task_id, status="completed", finished_at=time.time(), result_count=len(merged))
        emit_log("success", f"跨文件合并完成：{len(merged)} 个单品。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"跨文件合并失败：{exc}", scope)


def _run_ai_scan_export(task_id: str, payload: AiScanPayload, merged: bool = False) -> None:
    scope = "ai-scan"
    _set_task(task_id, status="running", started_at=time.time(), progress=8, message="准备导出扫描结果")
    try:
        _ensure_feature_access("AI扫描导出")
        scanner = _product_scanner()
        with _SCAN_LOCK:
            flat = list(_SCAN_RESULTS.get("flat") or [])
            merged_products = list(_SCAN_RESULTS.get("merged") or [])
        if not payload.output_dir.strip():
            raise ValueError("请先填写导出目录。")
        out_dir = _clean_path(payload.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ok_count = 0
        if merged:
            if not merged_products:
                raise ValueError("请先执行跨文件合并。")
            _set_task_progress(task_id, 24, f"导出合并结果 {len(merged_products)} 个")
            results = scanner.extract_cross_file(merged_products, str(out_dir), log_fn=_task_log_fn(task_id, scope, base=24, span=66))
            ok_count = len([r for r in results if r.get("output_path")])
        else:
            if not flat:
                raise ValueError("没有可导出的扫描结果。")
            total_products = max(1, len(flat))
            for index, product in enumerate(flat, start=1):
                video = product.get("_video", "")
                if not video:
                    continue
                _set_task_progress(task_id, 18 + ((index - 1) / total_products) * 72, f"导出 {index}/{total_products}")
                safe_name = _clean_forbidden_title_text(product.get("name") or "product", fallback="product")
                paths_out = scanner.extract_clip(video, product, str(out_dir), safe_name)
                if paths_out:
                    ok_count += len(paths_out)
                _set_task_progress(task_id, min(92, 18 + (index / total_products) * 72), f"完成导出 {index}/{total_products}")
        _set_task_progress(task_id, 95, "整理导出结果")
        if ok_count:
            _consume_trial("AI扫描导出", units=ok_count, scope=scope)
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        _set_task(task_id, status="completed", finished_at=time.time(), result_count=ok_count)
        emit_log("success", f"导出完成：{ok_count} 个文件。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"AI扫描导出失败：{exc}", scope)


def _run_product_scan_read(task_id: str, payload: ProductScanPayload) -> None:
    scope = "product-scan"
    _set_task(task_id, status="running", started_at=time.time(), progress=8, message="读取时间表")
    try:
        from schedule_splitter import align_schedule_to_video, group_by_product, read_excel

        excel = _clean_path(payload.excel_path)
        if not excel.exists():
            raise FileNotFoundError("Excel 文件不存在。")
        schedule, live_start = read_excel(str(excel), log_fn=_task_log_fn(task_id, scope, base=10, span=18))
        _set_task_progress(task_id, 35, "对齐时间表")
        video_start_offset = _parse_offset_seconds(payload.video_start_offset)
        if video_start_offset is not None:
            _shift_schedule_offsets(schedule, video_start_offset)
            emit_log("info", f"已按所选视频起点 {_hms(video_start_offset)} 调整时间表。", scope)
        else:
            live_start_override = _parse_live_start_datetime(payload.live_start_time, payload.video_paths)
            if live_start_override is not None and payload.video_paths:
                align_schedule_to_video(schedule, [str(v) for v in payload.video_paths], live_start_override, log_fn=lambda msg: emit_log("info", msg, scope), ffmpeg_cmd=_ffmpeg_cmd())
                emit_log("info", f"已按直播开始时间 {live_start_override.strftime('%H:%M:%S')} 自动对齐时间表。", scope)
        _set_task_progress(task_id, 78, "分组商品")
        groups = group_by_product(schedule)
        with _SCAN_LOCK:
            _SCAN_RESULTS["schedule"] = schedule
            _SCAN_RESULTS["live_start"] = live_start
            _SCAN_RESULTS["schedule_groups"] = groups
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        emit_log("success", f"时间表读取完成：{len(schedule)} 条记录，{len(groups)} 个商品。", scope)
        _set_task(task_id, status="completed", finished_at=time.time(), result_count=len(groups))
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"读取时间表失败：{exc}", scope)


def _run_product_scan(task_id: str, payload: ProductScanPayload) -> None:
    scope = "product-scan"
    _set_task(task_id, status="running", started_at=time.time(), progress=6, message="准备单品分割")
    try:
        _ensure_feature_access("单品扫描")
        from schedule_splitter import align_schedule_to_video, extract_by_schedule, group_by_product, read_excel

        excel = _clean_path(payload.excel_path)
        videos = _existing_paths(payload.video_paths, "直播视频")
        if not payload.output_dir.strip():
            raise ValueError("请填写导出目录。")
        out_dir = _clean_path(payload.output_dir)
        if not excel.exists():
            raise FileNotFoundError("Excel 文件不存在。")
        out_dir.mkdir(parents=True, exist_ok=True)
        _set_task_progress(task_id, 12, "读取时间表")
        schedule, live_start = read_excel(str(excel), log_fn=_task_log_fn(task_id, scope, base=12, span=14))
        _set_task_progress(task_id, 30, "对齐视频时间")
        video_start_offset = _parse_offset_seconds(payload.video_start_offset)
        if video_start_offset is not None:
            _shift_schedule_offsets(schedule, video_start_offset)
            emit_log("info", f"已按所选视频起点 {_hms(video_start_offset)} 调整时间表。", scope)
        else:
            live_start_override = _parse_live_start_datetime(payload.live_start_time, [str(v) for v in videos])
            align_schedule_to_video(schedule, [str(v) for v in videos], live_start_override or live_start, log_fn=_task_log_fn(task_id, scope, base=30, span=20), ffmpeg_cmd=_ffmpeg_cmd())
            if live_start_override is not None:
                emit_log("info", f"已按直播开始时间 {live_start_override.strftime('%H:%M:%S')} 自动对齐时间表。", scope)
        groups = group_by_product(schedule)
        _set_task_progress(task_id, 58, "整理商品片段")
        if payload.advance_seconds > 0:
            for group in groups:
                segments = [(max(0, s - payload.advance_seconds), max(0, e - payload.advance_seconds)) for s, e in group.get("segments", [])]
                group["segments"] = [(s, e) for s, e in segments if e - s >= 10]
                group["total_duration"] = sum(e - s for s, e in group["segments"])
        groups = [g for g in groups if g.get("segments")]
        for group in groups:
            group["name"] = _clean_forbidden_title_text(group.get("name") or "未命名商品", fallback="未命名商品")
        _set_task_progress(task_id, 68, f"导出 {len(groups)} 个商品")
        results = extract_by_schedule(groups, [str(v) for v in videos], str(out_dir), ffmpeg=_ffmpeg_cmd(), log_fn=_task_log_fn(task_id, scope, base=68, span=24))
        exported_results = []
        for item in results:
            output_path = Path(str(item.get("output_path") or ""))
            if output_path.is_file() and output_path.stat().st_size > 1000:
                exported_results.append({**item, "output_path": str(output_path)})
        ok_count = len(exported_results)
        _set_task_progress(task_id, 95, "校验导出结果")
        if not exported_results:
            raise RuntimeError("没有导出任何视频。请检查 Excel 时间段是否落在所选视频范围内，并确认源视频可读、输出目录可写。")
        for item in exported_results[:10]:
            output_path = Path(str(item.get("output_path") or ""))
            emit_log("success", f"已导出：{output_path.parent.name}\\{output_path.name}（{item.get('size_mb', 0)} MB）", scope)
        if len(exported_results) > 10:
            emit_log("success", f"另有 {len(exported_results) - 10} 个视频已导出到：{out_dir}", scope)
        if ok_count:
            _consume_trial("单品扫描", units=ok_count, scope=scope)
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        exported_names = {str(item.get("name") or "") for item in exported_results}
        _set_task(
            task_id,
            status="completed",
            finished_at=time.time(),
            result_count=ok_count,
            message=f"分割完成：{ok_count} 个视频",
            output_dir=str(out_dir),
            outputs=[item["output_path"] for item in exported_results],
        )
        emit_log("success", f"单品分割完成：导出 {ok_count} 个视频，覆盖 {len(exported_names)}/{len(groups)} 个商品。输出目录：{out_dir}", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc), message="分割失败")
        emit_log("error", f"单品扫描失败：{exc}", scope)


def _run_video_split(task_id: str, payload: VideoSplitPayload) -> None:
    scope = "video-split"
    _set_task(task_id, status="running", started_at=time.time(), progress=5, message="准备视频分割")
    try:
        _ensure_feature_access("视频分割")
        paths = _video_split_paths(payload)
        out_dir = _default_output_dir(paths[0], payload.output_dir, "split_output")
        preview = _video_split_preview(payload)
        plans = list(preview.get("videos") or [])
        total_segments = sum(len(plan.get("segments") or []) for plan in plans)
        if total_segments <= 0:
            raise RuntimeError("没有可导出的分割片段。")

        _set_task_progress(task_id, 10, f"准备导出 {total_segments} 段")
        emit_log("info", f"视频分割开始：{len(plans)} 个视频，预计导出 {total_segments} 段。输出目录：{out_dir}", scope)
        outputs: list[str] = []
        completed = 0
        cancel_event = _task_cancel_event(task_id)

        for video_index, plan in enumerate(plans, start=1):
            video = Path(str(plan.get("path") or ""))
            segments = list(plan.get("segments") or [])
            if not segments:
                continue
            if _is_task_cancelled(task_id) or cancel_event.is_set():
                emit_log("warning", "视频分割已停止。", scope)
                return

            stem = _safe_stem(video.stem)
            segment_seconds = max(0.1, float(plan.get("segment_seconds") or segments[0].get("duration") or 60.0))
            output_pattern = out_dir / f"{stem}_part_%03d.mp4"
            started_at = time.time()
            cmd = [
                _ffmpeg_cmd(),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video),
                "-map",
                "0",
                "-c",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                f"{segment_seconds:.6f}",
                "-segment_start_number",
                "1",
                "-reset_timestamps",
                "1",
                "-avoid_negative_ts",
                "make_zero",
                str(output_pattern),
            ]
            _set_task_progress(task_id, 12 + ((video_index - 1) / max(1, len(plans))) * 82, f"快速分割 {video_index}/{len(plans)}: {video.name}")
            emit_log(
                "info",
                f"[{video_index}/{len(plans)}] 快速分割 {video.name}，每段约 {_hms(segment_seconds)}，按关键帧近似切分。",
                scope,
            )

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            timeout = max(60, min(21600, int(float(plan.get("duration") or 0) * 4 + 60)))
            while proc.poll() is None:
                if _is_task_cancelled(task_id) or cancel_event.is_set():
                    proc.kill()
                    proc.wait(timeout=5)
                    emit_log("warning", "视频分割已停止。", scope)
                    return
                if time.time() - started_at > timeout:
                    proc.kill()
                    proc.wait(timeout=5)
                    raise TimeoutError(f"{video.name} 导出超时。")
                time.sleep(0.25)

            if proc.returncode != 0:
                raise RuntimeError(f"{video.name} 导出失败：返回码 {proc.returncode}")

            created = [
                path
                for path in sorted(out_dir.glob(f"{stem}_part_*.mp4"))
                if path.is_file() and path.stat().st_size > 1000 and path.stat().st_mtime >= started_at - 1
            ]
            if not created:
                raise RuntimeError(f"{video.name} 没有导出任何视频。")

            outputs.extend(str(path) for path in created)
            completed += len(created)
            _set_task_progress(task_id, 12 + (video_index / max(1, len(plans))) * 82, f"已导出 {completed} 段")
            emit_log("success", f"{video.name} 已导出 {len(created)} 段。", scope)

        if not outputs:
            raise RuntimeError("没有导出任何视频。")
        if _is_task_cancelled(task_id):
            emit_log("warning", "视频分割已停止。", scope)
            return
        _consume_trial("视频分割", scope=scope)
        _set_task(
            task_id,
            status="completed",
            finished_at=time.time(),
            result_count=len(outputs),
            message=f"分割完成：{len(outputs)} 段",
            output_dir=str(out_dir),
            outputs=outputs,
        )
        emit_log("success", f"视频分割完成：导出 {len(outputs)} 段。输出目录：{out_dir}", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc), message="分割失败")
        emit_log("error", f"视频分割失败：{exc}", scope)


def _bool(data: dict[str, Any], key: str) -> bool:
    return bool(data.get(key))


def _num(data: dict[str, Any], key: str, default: float = 0) -> float:
    try:
        return float(data.get(key, default))
    except Exception:
        return default


def _dedup_paths_from_payload(payload: DedupPayload) -> list[Path]:
    raw_paths = [str(v or "").strip() for v in (payload.video_paths or []) if str(v or "").strip()]
    if payload.video_path and payload.video_path.strip() and payload.video_path.strip() not in raw_paths:
        raw_paths.insert(0, payload.video_path.strip())
    return [_clean_path(path) for path in raw_paths]


def _file_md5(path: Path) -> str:
    try:
        stat = path.stat()
        key = f"md5|{str(path.resolve()).lower()}|{round(stat.st_mtime, 3)}|{stat.st_size}"
    except Exception:
        key = f"md5|{str(path).lower()}"
    cached = _VIDEO_FP_CACHE.get(key)
    if cached and cached.get("md5"):
        return str(cached["md5"])
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024 * 4), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _VIDEO_FP_CACHE[key] = {"md5": value}
    return value


def _frame_phash_bits(frame: Any) -> int:
    import cv2
    import numpy as np

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(small))
    low = dct[:8, :8].flatten()
    median = float(np.median(low[1:])) if len(low) > 1 else float(np.median(low))
    bits = 0
    for value in low:
        bits = (bits << 1) | (1 if float(value) >= median else 0)
    return bits


def _read_frame_at(cap: Any, second: float, fps: float, frame_count: int) -> Any | None:
    import cv2

    if fps > 0 and frame_count > 1:
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(frame_count - 1, int(second * fps))))
    else:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, second) * 1000)
    ok, frame = cap.read()
    return frame if ok and frame is not None else None


def _visual_fingerprint(path: Path, duration: float) -> dict[str, list[int]]:
    try:
        stat = path.stat()
        key = f"vfp|{str(path.resolve()).lower()}|{round(stat.st_mtime, 3)}|{stat.st_size}"
    except Exception:
        key = f"vfp|{str(path).lower()}"
    cached = _VIDEO_FP_CACHE.get(key)
    if cached and isinstance(cached.get("visual"), dict):
        return {
            "sample": list(cached["visual"].get("sample") or []),
            "scene": list(cached["visual"].get("scene") or []),
        }
    result = {"sample": [], "scene": []}
    try:
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return result
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if duration <= 0 and fps > 0 and frame_count > 0:
            duration = frame_count / fps
        if duration <= 0:
            return result

        if duration <= 64:
            sample_times = [min(duration - 0.05, i + 0.5) for i in range(max(1, int(duration)))]
        else:
            sample_times = [duration * (i + 0.5) / 64 for i in range(64)]
        for second in sample_times[:64]:
            frame = _read_frame_at(cap, second, fps, frame_count)
            if frame is not None:
                result["sample"].append(_frame_phash_bits(frame))

        scan_count = min(180, max(8, int(duration)))
        scan_times = [duration * i / max(1, scan_count - 1) for i in range(scan_count)]
        prev_gray = None
        for second in scan_times:
            frame = _read_frame_at(cap, second, fps, frame_count)
            if frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
            if prev_gray is None:
                result["scene"].append(_frame_phash_bits(frame))
                prev_gray = small
                continue
            diff = float(np.mean(cv2.absdiff(small, prev_gray)))
            if diff >= 14.0:
                result["scene"].append(_frame_phash_bits(frame))
                prev_gray = small
            if len(result["scene"]) >= 64:
                break
        if not result["scene"]:
            result["scene"] = list(result["sample"][:16])
        cap.release()
    except Exception:
        result = {"sample": [], "scene": []}
    _VIDEO_FP_CACHE[key] = {"visual": result}
    return result


def _hash_bit_similarity(a: int, b: int) -> float:
    return max(0.0, 1.0 - int(a ^ b).bit_count() / 64.0)


def _sequence_match_score(left: list[int], right: list[int], threshold: float = 0.86) -> float:
    if not left or not right:
        return 0.0
    m, n = len(left), len(right)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i, ha in enumerate(left, start=1):
        row = dp[i]
        prev = dp[i - 1]
        for j, hb in enumerate(right, start=1):
            if _hash_bit_similarity(ha, hb) >= threshold:
                row[j] = prev[j - 1] + 1
            else:
                row[j] = max(prev[j], row[j - 1])
    return dp[m][n] / max(1, min(m, n))


def _best_hash_match_score(left: list[int], right: list[int], threshold: float = 0.86) -> float:
    if not left or not right:
        return 0.0
    hits = 0
    for ha in left:
        best = max((_hash_bit_similarity(ha, hb) for hb in right), default=0.0)
        if best >= threshold:
            hits += 1
    return hits / max(1, len(left))


def _visual_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    a_visual = a.get("visual") or {}
    b_visual = b.get("visual") or {}
    a_sample = list(a_visual.get("sample") or [])
    b_sample = list(b_visual.get("sample") or [])
    a_scene = list(a_visual.get("scene") or [])
    b_scene = list(b_visual.get("scene") or [])
    ordered_sample = _sequence_match_score(a_sample, b_sample, threshold=0.86)
    ordered_scene = _sequence_match_score(a_scene, b_scene, threshold=0.84)
    local_sample = min(
        _best_hash_match_score(a_sample, b_sample, threshold=0.87),
        _best_hash_match_score(b_sample, a_sample, threshold=0.87),
    )
    return max(ordered_sample * 0.65 + local_sample * 0.35, ordered_scene)


def _audio_fingerprint(path: Path) -> list[float]:
    try:
        stat = path.stat()
        key = f"afp|{str(path.resolve()).lower()}|{round(stat.st_mtime, 3)}|{stat.st_size}"
    except Exception:
        key = f"afp|{str(path).lower()}"
    cached = _VIDEO_FP_CACHE.get(key)
    if cached and isinstance(cached.get("audio"), list):
        return list(cached["audio"])
    values: list[float] = []
    try:
        import numpy as np

        proc = subprocess.run(
            [_ffmpeg_cmd(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-t", "120", "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0 and proc.stdout:
            samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
            if samples.size > 0:
                window = 4000
                chunks = [samples[i:i + window] for i in range(0, min(samples.size, window * 64), window)]
                energy = []
                for chunk in chunks:
                    if chunk.size:
                        rms = float(np.sqrt(np.mean(np.square(chunk))) / 32768.0)
                        energy.append(rms)
                if energy:
                    arr = np.asarray(energy, dtype=np.float32)
                    arr = (arr - float(arr.mean())) / (float(arr.std()) + 1e-6)
                    values = [round(float(v), 4) for v in arr.tolist()]
    except Exception:
        values = []
    _VIDEO_FP_CACHE[key] = {"audio": values}
    return values


def _audio_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    import math

    n = min(len(left), len(right))
    a = left[:n]
    b = right[:n]
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return max(0.0, min(1.0, (dot / (na * nb) + 1.0) / 2.0))


def _meta_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    score = 0.0
    duration_a = float(a.get("duration") or 0)
    duration_b = float(b.get("duration") or 0)
    if duration_a and duration_b:
        gap = abs(duration_a - duration_b)
        score += 0.45 if gap <= max(1.0, min(duration_a, duration_b) * 0.03) else max(0.0, 0.45 - gap / max(duration_a, duration_b))
    if a.get("resolution") and a.get("resolution") == b.get("resolution"):
        score += 0.25
    size_a = int(a.get("size") or 0)
    size_b = int(b.get("size") or 0)
    if size_a and size_b:
        size_gap = abs(size_a - size_b) / max(size_a, size_b)
        score += max(0.0, 0.30 * (1.0 - min(1.0, size_gap / 0.15)))
    return max(0.0, min(1.0, score))


def _duplicate_level(score: float) -> tuple[str, str]:
    if score >= 0.9:
        return "high", "高度重复"
    if score >= 0.65:
        return "near", "疑似重复"
    if score >= 0.35:
        return "partial", "部分素材重复"
    return "low", "不重复"


def _dedup_analysis_item(path: Path) -> dict[str, Any]:
    info = _probe_video_info(str(path))
    size = path.stat().st_size if path.exists() else 0
    md5 = _file_md5(path) if path.exists() and path.is_file() else ""
    visual = _visual_fingerprint(path, float(info.get("duration") or 0)) if info.get("valid") else {"sample": [], "scene": []}
    audio = _audio_fingerprint(path) if info.get("has_audio") else []
    return {
        "path": str(path),
        "name": path.name,
        "size": size,
        "md5": md5,
        "duration": float(info.get("duration") or 0),
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "resolution": info.get("resolution") or "",
        "visual": visual,
        "audio": audio,
    }


def _dedup_group_items(items: list[dict[str, Any]], indices: set[int]) -> list[dict[str, Any]]:
    rows = []
    for index in sorted(indices):
        item = items[index]
        rows.append({
            "path": item["path"],
            "name": item["name"],
            "duration": item["duration"],
            "resolution": item["resolution"],
        })
    return rows


def _analyze_duplicate_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    exact_seen: set[int] = set()
    by_md5: dict[str, set[int]] = {}
    for index, item in enumerate(items):
        if item.get("md5"):
            by_md5.setdefault(str(item["md5"]), set()).add(index)
    for indices in by_md5.values():
        if len(indices) > 1:
            exact_seen.update(indices)
            groups.append({
                "type": "exact",
                "level": "高度重复",
                "reason": "重复度 100% · 文件内容完全一致",
                "score": 1.0,
                "metrics": {"visual": 1.0, "audio": 1.0, "meta": 1.0},
                "items": _dedup_group_items(items, indices),
            })

    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    pair_scores: dict[tuple[int, int], dict[str, Any]] = {}
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if i in exact_seen and j in exact_seen and items[i].get("md5") == items[j].get("md5"):
                continue
            a, b = items[i], items[j]
            visual_score = _visual_similarity(a, b)
            audio_score = _audio_similarity(a.get("audio") or [], b.get("audio") or [])
            meta_score = _meta_similarity(a, b)
            duplicate_score = max(0.0, min(1.0, visual_score * 0.70 + audio_score * 0.20 + meta_score * 0.10))
            if duplicate_score >= 0.35:
                union(i, j)
                pair_scores[(i, j)] = {
                    "score": duplicate_score,
                    "visual": visual_score,
                    "audio": audio_score,
                    "meta": meta_score,
                }

    near_groups: dict[int, set[int]] = {}
    for index in range(len(items)):
        root = find(index)
        near_groups.setdefault(root, set()).add(index)
    for indices in near_groups.values():
        if len(indices) <= 1:
            continue
        scores = [item for (i, j), item in pair_scores.items() if i in indices and j in indices]
        if not scores:
            continue
        score = sum(float(item["score"]) for item in scores) / len(scores)
        visual = sum(float(item["visual"]) for item in scores) / len(scores)
        audio = sum(float(item["audio"]) for item in scores) / len(scores)
        meta = sum(float(item["meta"]) for item in scores) / len(scores)
        group_type, level = _duplicate_level(score)
        groups.append({
            "type": group_type,
            "level": level,
            "reason": f"重复度 {round(score * 100)}% · 关键帧/场景片段局部匹配",
            "score": round(score, 3),
            "metrics": {"visual": round(visual, 3), "audio": round(audio, 3), "meta": round(meta, 3)},
            "items": _dedup_group_items(items, indices),
        })
    return sorted(groups, key=lambda item: float(item.get("score") or 0), reverse=True)


def _manual_dedup_filters(video_options: dict[str, Any], audio_options: dict[str, Any]) -> dict[str, Any]:
    vf: list[str] = []
    af: list[str] = []
    applied: list[str] = []
    if _bool(video_options, "mirror"):
        vf.append("hflip")
        applied.append("mirror")
    if _bool(video_options, "crop"):
        ratio = max(0.8, min(1.0, 1 - _num(video_options, "crop_value", 0) / 100))
        vf.append(f"crop=iw*{ratio}:ih*{ratio},scale=iw:ih")
        applied.append(f"crop({ratio:.3f})")
    if _bool(video_options, "speed"):
        speed = max(0.5, min(2.0, _num(video_options, "speed_value", 100) / 100))
        vf.append(f"setpts=PTS/{speed:.6f}")
        af.append(f"atempo={speed:.3f}")
        applied.append(f"speed({speed:.2f}x)")
    if _bool(video_options, "blur"):
        vf.append(f"gblur=sigma={max(0.1, _num(video_options, 'blur_value', 2))}")
        applied.append("blur")
    if _bool(video_options, "sharpen"):
        vf.append(f"unsharp=luma_amount={_num(video_options, 'sharpen_value', 30) / 50:.2f}")
        applied.append("sharpen")
    if _bool(video_options, "gamma_shift"):
        vf.append("eq=gamma=1.03:saturation=1.04:contrast=1.02")
        applied.append("gamma")
    if _bool(video_options, "corner_mask"):
        vf.append("drawbox=x=0:y=0:w=42:h=42:color=black@0.18:t=fill")
        applied.append("corner_mask")
    if _bool(video_options, "bg_fill"):
        vf.append("pad=iw+40:ih+40:20:20:color=black")
        applied.append("bg_fill")
    if _bool(audio_options, "pitch"):
        af.append("asetrate=44100*1.015,aresample=44100,atempo=0.985222")
        applied.append("audio_pitch")
    if _bool(audio_options, "reverb"):
        af.append("aecho=0.8:0.7:60:0.25")
        applied.append("reverb")
    if _bool(audio_options, "noise_fusion"):
        af.append("volume=1.0")
        applied.append("audio_fusion")
    return {"video_filters": ",".join(vf), "audio_filters": ",".join(af), "applied": applied}


def _append_video_filter(vf: str, extra: str) -> str:
    vf = (vf or "").strip()
    extra = (extra or "").strip()
    if not extra:
        return vf
    if not vf or vf == "null":
        return extra
    return f"{vf},{extra}"


def _frame_structure_filter(video_options: dict[str, Any], preset: str, info: dict[str, Any]) -> tuple[str, str]:
    enabled = video_options.get("frame_structure")
    if enabled is None:
        enabled = str(preset or "").strip().lower() == "heavy"
    if not bool(enabled):
        return "", ""
    level = str(video_options.get("frame_structure_level") or ("heavy" if preset == "heavy" else "medium")).strip().lower()
    if level in {"杞诲害", "light"}:
        level = "light"
    elif level in {"鏍囧噯", "medium", "normal"}:
        level = "medium"
    else:
        level = "heavy"
    try:
        source_fps = float(info.get("fps") or 0)
    except Exception:
        source_fps = 0.0
    if source_fps <= 1:
        source_fps = 30.0
    factor = {"light": 0.997, "medium": 0.991, "heavy": 0.985}.get(level, 0.991)
    target_fps = max(15.0, min(60.0, source_fps * factor))
    return f"fps=fps={target_fps:.3f}:round=near", f"frame_structure({level},{target_fps:.2f}fps)"


def _core_dedup_filters(video: Path, index: int, preset: str, mirror_enabled: bool) -> dict[str, Any]:
    if preset == "none":
        return {"video_filters": "", "audio_filters": "", "applied": []}
    info = _probe_video_info(str(video))
    width = int(info.get("width") or 0) or 720
    height = int(info.get("height") or 0) or 1280
    import cutter_logic as cutter_core

    old_preset = getattr(cutter_core, "DEDUP_PRESET", "medium")
    try:
        cutter_core.DEDUP_PRESET = preset
        return cutter_core.build_dedup_filters(width, height, index, mirror_enabled=mirror_enabled)
    finally:
        cutter_core.DEDUP_PRESET = old_preset


def _audio_complex(af: str) -> str:
    if not af:
        return "[0:a]anull[out_a]"
    parts = [part.strip() for part in af.split(",") if part.strip()]
    simple_parts: list[str] = []
    noise_src = ""
    for part in parts:
        if part.startswith("aevalsrc="):
            noise_src = part
        elif part.startswith("amix="):
            continue
        else:
            simple_parts.append(part)
    simple = ",".join(simple_parts) or "anull"
    if noise_src:
        return f"[0:a]{simple}[a1];{noise_src}[noise];[a1][noise]amix=inputs=2:duration=first:dropout_transition=0[out_a]"
    return f"[0:a]{simple}[out_a]"


def _unique_output_path(out_dir: Path, stem: str, suffix: str = "_creative") -> Path:
    candidate = out_dir / f"{stem}{suffix}.mp4"
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = out_dir / f"{stem}{suffix}_{index}.mp4"
        if not candidate.exists():
            return candidate
    return out_dir / f"{stem}{suffix}_{int(time.time())}.mp4"


def _dedup_pip_values(payload: DedupPayload, scope: str) -> tuple[str | None, Path | None, float, float, str]:
    if payload.pip_enabled:
        pip_path, used_file = _pick_pip_asset(payload, scope)
        return pip_path, used_file, float(payload.pip_size or 0.15), float(payload.pip_opacity or 0.03), payload.pip_pos or "鍙充笅"
    legacy = payload.pip or {}
    if isinstance(legacy, dict) and legacy.get("enabled"):
        path = str(legacy.get("path") or "").strip()
        return path or "auto", None, _num(legacy, "size", 0.15), _num(legacy, "opacity", 0.35), str(legacy.get("pos") or "鍙充笅")
    return None, None, 0.15, 0.03, "鍙充笅"


def _run_dedup_one(video: Path, output: Path, payload: DedupPayload, index: int, scope: str = "dedup") -> tuple[bool, str, str, Path | None]:
    preset = (payload.dedup_preset or "medium").strip() or "medium"
    video_options = payload.video or {}
    audio_options = payload.audio or {}
    if preset == "custom":
        dedup = _manual_dedup_filters(video_options, audio_options)
    else:
        dedup = _core_dedup_filters(video, index, preset, mirror_enabled=_bool(video_options, "mirror"))

    vf = dedup.get("video_filters") or "null"
    af = dedup.get("audio_filters") or ""
    info = _probe_video_info(str(video))
    frame_vf, frame_applied = _frame_structure_filter(video_options, preset, info)
    if frame_vf:
        vf = _append_video_filter(vf, frame_vf)
        dedup.setdefault("applied", []).append(frame_applied)
    has_audio = bool(info.get("has_audio"))
    pip_value, used_pip_file, pip_size, pip_opacity, pip_pos = _dedup_pip_values(payload, scope)
    pip_path = _clean_path(pip_value) if pip_value and pip_value != "auto" else None
    pip_on = bool(pip_value)
    pip_file_on = pip_path and pip_path.exists()
    pip_auto_on = pip_value == "auto"
    cmd = [_ffmpeg_cmd(), "-y", "-i", str(video)]
    video_graph = f"[0:v]{vf},format=yuv420p[out_v]"
    if pip_file_on or pip_auto_on:
        size = max(0.03, min(1.0, float(pip_size or 0.15)))
        opacity = max(0.01, min(1.0, float(pip_opacity or 0.03)))
        pos = str(pip_pos or "鍙充笅")
        pos_map = {"鍙充笅": "W-w-24:H-h-24", "鍙充笂": "W-w-24:24", "宸︿笅": "24:H-h-24", "宸︿笂": "24:24"}
        if pip_file_on:
            main_duration = max(1.0, float(info.get("duration") or 0))
            cmd += ["-stream_loop", "-1", "-t", f"{main_duration:.3f}", "-i", str(pip_path)]
            video_graph = (
                f"[0:v]{vf}[base];"
                f"[1:v]scale=iw*{size}:ih*{size},format=rgba,colorchannelmixer=aa={opacity}[pip];"
                f"[base][pip]overlay={pos_map.get(pos, 'W-w-24:H-h-24')},format=yuv420p[out_v]"
            )
        else:
            video_graph = (
                f"[0:v]{vf},split[base][pip_src];"
                f"[pip_src]scale=iw*{size}:ih*{size},format=rgba,colorchannelmixer=aa={opacity}[pip];"
                f"[base][pip]overlay={pos_map.get(pos, 'W-w-24:H-h-24')},format=yuv420p[out_v]"
            )
    graphs = [video_graph]
    if has_audio:
        graphs.append(_audio_complex(af))
    cmd += ["-filter_complex", ";".join(graphs), "-map", "[out_v]"]
    if has_audio:
        cmd += ["-map", "[out_a]"]
    else:
        cmd += ["-an"]
    cmd += ["-map_metadata", "-1", "-c:v", "libx264", "-preset", "fast", "-crf", "23"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", str(output)]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=7200,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    ok = proc.returncode == 0 and output.exists() and output.stat().st_size > 1000
    applied = ",".join(dedup.get("applied") or []) or "基础转码"
    if pip_file_on or pip_auto_on:
        applied = f"{applied},pip({pip_value if pip_auto_on else Path(str(pip_path)).name})"
    return ok, applied, (proc.stderr or "")[-500:], used_pip_file


def _run_dedup(task_id: str, payload: DedupPayload) -> None:
    scope = "dedup"
    _set_task(task_id, status="running", started_at=time.time(), progress=5, message="准备创作辅助")
    try:
        _ensure_feature_access("创作辅助")
        videos = _dedup_paths_from_payload(payload)
        if not videos:
            raise ValueError("请先添加视频。")
        total_videos = max(1, len(videos))
        _set_task(task_id, batch_total=total_videos, batch_done=0, batch_current=1 if total_videos > 1 else 0, batch_failed=0, batch_label="")
        _set_task_progress(task_id, 8, f"准备处理 {total_videos} 个视频")
        base_dir = _default_output_dir(videos[0], payload.output_dir, "dedup_output")
        emit_log("info", f"开始批量二创消重，共 {len(videos)} 个视频。", scope)
        outputs: list[str] = []
        failures: list[str] = []
        for index, video in enumerate(videos, start=1):
            if _is_task_cancelled(task_id):
                emit_log("warning", "任务已停止。", scope)
                break
            if not video.exists():
                failures.append(f"{video.name}: 文件不存在")
                _set_task_progress(task_id, min(96, 10 + (index / total_videos) * 84), f"跳过失败 {index}/{total_videos}: {video.name}")
                _set_task(task_id, batch_failed=len(failures))
                emit_log("error", f"[{index}/{len(videos)}] 文件不存在：{video}", scope)
                continue
            out_dir = base_dir if payload.output_dir.strip() else _default_output_dir(video, "", "dedup_output")
            output = _unique_output_path(out_dir, _safe_stem(video.stem))
            emit_log("info", f"[{index}/{len(videos)}] 正在处理：{video.name}", scope)
            _set_task_progress(task_id, 10 + ((index - 1) / total_videos) * 84, f"处理 {index}/{total_videos}: {video.name}")
            ok, applied, stderr_text, used_pip_file = _run_dedup_one(video, output, payload, index, scope=scope)
            if ok:
                outputs.append(str(output))
                _set_task_progress(task_id, min(96, 10 + (index / total_videos) * 84), f"完成 {index}/{total_videos}: {video.name}")
                emit_log("success", f"[{index}/{len(videos)}] 完成：{output.name}（{applied}）", scope)
                _archive_used_pip(used_pip_file, scope)
            else:
                failures.append(f"{video.name}: {stderr_text or '视频处理失败'}")
                _set_task_progress(task_id, min(96, 10 + (index / total_videos) * 84), f"跳过失败 {index}/{total_videos}: {video.name}")
                _set_task(task_id, batch_failed=len(failures))
                emit_log("error", f"[{index}/{len(videos)}] 处理失败：{_short_error(stderr_text)}。解决办法：{_solution_for_error(stderr_text)}", scope)
        if not outputs:
            raise RuntimeError(failures[0] if failures else "没有生成成功的视频。")
        _consume_trial("创作辅助", scope=scope)
        _set_task(
            task_id,
            status="completed",
            finished_at=time.time(),
            output=outputs[0],
            outputs=outputs,
            result_count=len(outputs),
            batch_total=total_videos,
            batch_done=len(outputs),
            batch_current=0,
            batch_failed=len(failures),
            batch_label="",
        )
        if failures:
            emit_log("warning", f"批量二创消重完成：成功 {len(outputs)} 个，失败 {len(failures)} 个。", scope)
        else:
            emit_log("success", f"批量二创消重完成：成功 {len(outputs)} 个。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"创作辅助处理失败：{exc}", scope)


def _live_segment_seconds(label: str) -> int:
    text = str(label or "").strip()
    aliases = {
        "不限": 36000,
        "10分钟": 600,
        "15分钟": 900,
        "30分钟": 1800,
        "60分钟": 3600,
        "1小时": 3600,
        "120分钟": 7200,
        "2小时": 7200,
    }
    if text in aliases:
        return aliases[text]
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match:
        value = float(match.group(1))
        if "小时" in text or "hour" in text.lower():
            return max(1, int(value * 3600))
        return max(1, int(value * 60))
    return 36000


def _bounded_seconds_from_minutes(value: Any, default_minutes: float, min_seconds: float, max_seconds: float) -> float:
    try:
        seconds = float(value) * 60
    except Exception:
        seconds = default_minutes * 60
    return max(min_seconds, min(max_seconds, seconds))


def _live_product_split_settings(payload: LiveRecPayload) -> dict[str, Any]:
    product_hard_max_seconds = 15 * 60
    min_seconds = _bounded_seconds_from_minutes(payload.product_min_minutes, 3.0, 30, product_hard_max_seconds)
    max_seconds = _bounded_seconds_from_minutes(payload.product_max_minutes, 15.0, 60, product_hard_max_seconds)
    if max_seconds < min_seconds:
        max_seconds = min_seconds
    default_seconds = _bounded_seconds_from_minutes(payload.product_default_minutes, 10.0, min_seconds, max_seconds)
    return {
        "default_seconds": default_seconds,
        "min_seconds": min_seconds,
        "max_seconds": max_seconds,
        "switch_confirm_seconds": int(payload.product_switch_confirm_seconds or 0),
        "head_seconds": int(payload.product_head_seconds or 0),
        "tail_seconds": int(payload.product_tail_seconds or 0),
    }


def _fallback_live_product_segments(duration: float, settings: dict[str, Any]) -> list[dict[str, Any]]:
    duration = max(0.0, float(duration or 0))
    if duration <= 0:
        return []
    segment_seconds = max(float(settings["min_seconds"]), min(float(settings["default_seconds"]), float(settings["max_seconds"])))
    min_seconds = float(settings["min_seconds"])
    head_seconds = float(settings["head_seconds"])
    tail_seconds = float(settings["tail_seconds"])
    segments: list[dict[str, Any]] = []
    start = 0.0
    while start < duration - 0.5:
        end = min(duration, start + segment_seconds)
        remainder = duration - end
        if 0 < remainder < min_seconds:
            end = duration
        if end - start < min_seconds and segments:
            segments[-1]["end"] = round(duration, 3)
            segments[-1]["cut_end"] = round(duration, 3)
            segments[-1]["duration"] = round(max(0.0, duration - float(segments[-1]["start"])), 3)
            break
        index = len(segments) + 1
        cut_start = max(0.0, start - head_seconds)
        cut_end = min(duration, end + tail_seconds)
        segments.append(
            {
                "index": index,
                "source": "duration_fallback",
                "confidence": "fallback",
                "start": round(start, 3),
                "end": round(end, 3),
                "cut_start": round(cut_start, 3),
                "cut_end": round(cut_end, 3),
                "duration": round(max(0.0, end - start), 3),
                "title": f"单品候选_{index:02d}",
                "product_id": "",
                "promotion_id": "",
                "detail_url": "",
            }
        )
        start = end
    return segments


def _live_product_segment_index(segment: dict[str, Any]) -> int:
    try:
        return max(1, int(segment.get("index") or 1))
    except Exception:
        return 1


def _live_product_id_token(value: Any, max_chars: int = 32) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    text = re.sub(r"[^0-9A-Za-z_-]", "", text)
    return text[:max_chars]


def _live_product_naming_mode(payload: LiveRecPayload | None = None, mode: Any = "") -> str:
    raw = str(mode or getattr(payload, "product_naming_mode", "") or "product_id").strip().lower()
    if raw in {"product_name", "name", "title", "商品名称", "商品名"}:
        return "product_name"
    return "product_id"


def _live_product_recording_stamp(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(\d{8}_\d{6})", text)
    if match:
        return match.group(1)
    return time.strftime("%Y%m%d_%H%M%S")


def _live_product_segment_filename(
    segment: dict[str, Any],
    payload: LiveRecPayload | None = None,
    recording_stamp: str = "",
) -> str:
    product_id = _live_product_id_token(segment.get("product_id"))
    promotion_id = _live_product_id_token(segment.get("promotion_id"))
    mode = _live_product_naming_mode(payload, segment.get("naming_mode"))
    if segment.get("review_status") == "pending_user_confirm":
        index = _live_product_segment_index(segment)
        stamp = _live_product_recording_stamp(recording_stamp or segment.get("recording_stamp") or "")
        return f"待确认_{stamp}_{index:02d}"
    if mode == "product_id":
        if product_id:
            return product_id
        if promotion_id:
            return promotion_id
    else:
        stamp = _live_product_recording_stamp(recording_stamp or segment.get("recording_stamp") or "")
        title = str(segment.get("title") or segment.get("product_title") or "").strip()
        if not title or title in {product_id, promotion_id} or re.fullmatch(r"\d{8,}", title):
            title = "未命名商品"
        return f"{title}_{stamp}"
    index = _live_product_segment_index(segment)
    title = str(segment.get("title") or "单品候选").strip() or "单品候选"
    return f"{_live_product_recording_stamp(recording_stamp or segment.get('recording_stamp') or '')}_{title}_{index:02d}"


def _live_product_segment_naming_source(segment: dict[str, Any], payload: LiveRecPayload | None = None) -> str:
    mode = _live_product_naming_mode(payload, segment.get("naming_mode"))
    if mode == "product_name":
        if segment.get("review_status") == "pending_user_confirm":
            return "pending_user_confirm"
        return "product_name_timestamp"
    if _live_product_id_token(segment.get("product_id")):
        return "auto_product_id"
    if _live_product_id_token(segment.get("promotion_id")):
        return "auto_promotion_id"
    if segment.get("review_status") == "pending_user_confirm":
        return "pending_user_confirm"
    return "title"


def _live_product_change_key(change: dict[str, Any]) -> str:
    product = change.get("product") if isinstance(change.get("product"), dict) else {}
    product_id = str(product.get("product_id") or change.get("product_id") or "").strip()
    promotion_id = str(product.get("promotion_id") or change.get("promotion_id") or "").strip()
    title = str(product.get("title") or change.get("title") or "").strip()
    return product_id or promotion_id or title


def _apply_live_product_segment_names(
    segments: list[dict[str, Any]],
    payload: LiveRecPayload | None = None,
    recording_stamp: str = "",
) -> None:
    used: dict[str, int] = {}
    mode = _live_product_naming_mode(payload)
    stamp = _live_product_recording_stamp(recording_stamp)
    for segment in segments:
        segment["naming_mode"] = mode
        segment["recording_stamp"] = stamp
        stem = _clean_forbidden_title_text(_live_product_segment_filename(segment, payload, stamp), fallback="单品候选")
        if stem in used:
            used[stem] += 1
            if mode != "product_id":
                stem = f"{stem}_{used[stem]:02d}"
        else:
            used[stem] = 1
        segment["filename_stem"] = stem
        segment["naming_source"] = _live_product_segment_naming_source(segment, payload)


def _live_product_queue_stats(queue: dict[str, Any] | None) -> dict[str, Any]:
    if not queue:
        return {}
    segments = list(queue.get("segments") or [])
    bound = 0
    pending = 0
    for segment in segments:
        has_bound_id = bool(segment.get("product_id") or segment.get("promotion_id"))
        if segment.get("review_status") == "auto_bound" and has_bound_id:
            bound += 1
        elif segment.get("naming_source") in {"auto_product_id", "auto_promotion_id"} and has_bound_id:
            bound += 1
        if segment.get("review_status") == "pending_user_confirm" or segment.get("naming_source") == "pending_user_confirm":
            pending += 1
    return {
        "active_product_changed": bool(queue.get("active_product_changed")),
        "product_bound_segments": bound,
        "product_pending_segments": pending,
        "strong_signal_count": int(queue.get("strong_signal_count") or 0),
        "candidate_signal_count": int(queue.get("candidate_signal_count") or 0),
        "active_product_candidate_count": int(queue.get("active_product_candidate_count") or 0),
        "status_2_candidate_count": int(queue.get("status_2_candidate_count") or 0),
        "active_product_confirmed_count": int(queue.get("active_product_confirmed_count") or 0),
        "active_product_rule_review_count": int(queue.get("active_product_rule_review_count") or 0),
        "product_evidence_count": int(queue.get("product_evidence_count") or 0),
        "unresolved_reason": str(queue.get("unresolved_reason") or ""),
        "recording_returncode": queue.get("recording_returncode"),
    }


def _live_product_recognition_status(queue: dict[str, Any] | None) -> str:
    stats = _live_product_queue_stats(queue)
    segment_count = len((queue or {}).get("segments") or [])
    if int(stats.get("product_bound_segments") or 0) > 0 or bool(stats.get("active_product_changed")):
        return "identified"
    if int(stats.get("active_product_candidate_count") or 0) > 0 or int(stats.get("active_product_rule_review_count") or 0) > 0:
        return "needs_review"
    if int(stats.get("product_pending_segments") or 0) > 0 or segment_count > 0:
        return "unresolved"
    return "none"


def _write_live_record_user_artifacts(queue: dict[str, Any], summary: dict[str, Any] | None = None) -> None:
    output = Path(str(queue.get("recording") or ""))
    if not str(output):
        return
    stats = _live_product_queue_stats(queue)
    status = _live_product_recognition_status(queue)
    product_timeline_path = output.with_suffix(".product_timeline.json")
    recording_summary_path = output.with_suffix(".recording_summary.json")
    segments = []
    for segment in list(queue.get("segments") or []):
        segments.append(
            {
                "index": segment.get("index"),
                "status": segment.get("review_status") or ("auto_bound" if segment.get("product_id") or segment.get("promotion_id") else "pending_user_confirm"),
                "start": segment.get("start"),
                "end": segment.get("end"),
                "cut_start": segment.get("cut_start"),
                "cut_end": segment.get("cut_end"),
                "duration": segment.get("duration"),
                "title": segment.get("title") or "",
                "product_id": segment.get("product_id") or "",
                "promotion_id": segment.get("promotion_id") or "",
                "filename_stem": segment.get("filename_stem") or "",
                "naming_source": segment.get("naming_source") or "",
                "source": segment.get("source") or "",
                "confidence": segment.get("confidence") or "",
                "reason": segment.get("reason") or "",
            }
        )
    product_timeline = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "recording": str(output),
        "recording_original": queue.get("recording_original") or "",
        "recording_remuxed_to_mp4": bool(queue.get("recording_remuxed_to_mp4")),
        "recording_duration": queue.get("recording_duration"),
        "recognition_status": status,
        "room_name": queue.get("room_name") or "",
        "room_url": queue.get("room_url") or "",
        "naming_mode": queue.get("naming_mode") or "",
        "stats": stats,
        "segments": segments,
    }
    recording_summary = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "recording": str(output),
        "recording_original": queue.get("recording_original") or "",
        "recording_remuxed_to_mp4": bool(queue.get("recording_remuxed_to_mp4")),
        "recording_duration": queue.get("recording_duration"),
        "stream_quality": queue.get("stream_quality") or "",
        "recognition_status": status,
        "confirmed_product_segments": stats.get("product_bound_segments", 0),
        "pending_review_segments": stats.get("product_pending_segments", 0),
        "candidate_signal_count": stats.get("candidate_signal_count", 0),
        "strong_signal_count": stats.get("strong_signal_count", 0),
        "active_product_candidate_count": stats.get("active_product_candidate_count", 0),
        "active_product_rule_review_count": stats.get("active_product_rule_review_count", 0),
        "unresolved_reason": stats.get("unresolved_reason", ""),
        "product_evidence_log": queue.get("product_evidence_log") or "",
        "product_timeline": str(product_timeline_path),
        "split_queue": queue.get("queue_path") or "",
        "probe_summary": queue.get("probe_summary") or "",
        "probe_report": queue.get("probe_report") or "",
        "probe_version": (summary or {}).get("probe_version") or "",
        "probe_build": (summary or {}).get("probe_build") or "",
        "probe_script_hash": queue.get("probe_script_hash") or "",
    }
    _write_json_file(product_timeline_path, product_timeline)
    _write_json_file(recording_summary_path, recording_summary)
    queue["product_timeline_json"] = str(product_timeline_path)
    queue["recording_summary"] = str(recording_summary_path)


def _remux_live_recording_to_mp4(output: Path, scope: str, name: str) -> Path:
    if output.suffix.lower() != ".ts":
        return output
    if not output.exists() or output.stat().st_size < 1000:
        return output
    target = output.with_suffix(".mp4")
    remux_log = output.with_suffix(".remux.log")
    cmd = [
        _ffmpeg_cmd(),
        "-hide_banner",
        "-y",
        "-fflags",
        "+genpts+igndts",
        "-i",
        str(output),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(target),
    ]
    source_size = output.stat().st_size
    try:
        with remux_log.open("wb") as log_handle:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=log_handle,
                timeout=1800,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if proc.returncode != 0 or not target.exists() or target.stat().st_size < 1000:
            if _transcode_live_recording_sync(output, target, scope, name, "direct remux failed"):
                emit_log("info", f"{name}: TS 已通过同步安全转码生成 MP4。", scope)
            else:
                emit_log("warning", f"{name}: TS 转 MP4 失败，已保留原始 TS 母带继续处理。", scope)
                return output
        else:
            video_start, audio_start, gap, has_audio = _probe_av_start_gap(target)
            if has_audio and gap > 0.08:
                reason = f"video={video_start:.3f}s audio={audio_start:.3f}s gap={gap:.3f}s"
                if _transcode_live_recording_sync(output, target, scope, name, reason):
                    emit_log("info", f"{name}: 已修正 TS 转 MP4 音画时间戳偏移 {gap:.3f}s。", scope)
                else:
                    emit_log("warning", f"{name}: 同步安全版 MP4 生成失败，继续使用直接转封装版本。", scope)
        emit_log("success", f"{name}: 已自动转为 MP4 母带：{target.name} ({target.stat().st_size / 1024 / 1024:.1f}MB)", scope)
        if str(os.environ.get("LIVECLIPPER_KEEP_TS_RECORDING") or "").strip().lower() not in {"1", "true", "yes", "on"}:
            try:
                output.unlink()
                emit_log("info", f"{name}: 已移除 TS 临时母带：{output.name} ({source_size / 1024 / 1024:.1f}MB)", scope)
            except Exception as exc:
                emit_log("warning", f"{name}: MP4 已生成，但移除 TS 临时母带失败：{exc}", scope)
        return target
    except subprocess.TimeoutExpired:
        if _transcode_live_recording_sync(output, target, scope, name, "direct remux timeout"):
            emit_log("info", f"{name}: TS 已通过同步安全转码生成 MP4。", scope)
            return target
        emit_log("warning", f"{name}: TS 转 MP4 超时，已保留原始 TS 母带继续处理。", scope)
        return output
    except Exception as exc:
        emit_log("warning", f"{name}: TS 转 MP4 异常，已保留原始 TS 母带继续处理：{exc}", scope)
        return output


def _stable_active_product_changes(changes: list[dict[str, Any]], duration: float, settings: dict[str, Any]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    for change in sorted(changes, key=lambda item: float(item.get("elapsed") or 0)):
        key = _live_product_change_key(change)
        if not key:
            continue
        elapsed = max(0.0, min(float(duration or 0), float(change.get("elapsed") or 0)))
        if ordered and ordered[-1]["key"] == key:
            ordered[-1]["end"] = elapsed
            continue
        ordered.append({"key": key, "start": elapsed, "change": change})
    if not ordered:
        return []

    runs: list[dict[str, Any]] = []
    for index, item in enumerate(ordered):
        start = float(item["start"])
        end = float(ordered[index + 1]["start"]) if index + 1 < len(ordered) else float(duration or start)
        if end <= start:
            continue
        runs.append({"key": item["key"], "start": start, "end": end, "change": item["change"]})

    # A product card can flicker or rotate faster than the anchor is actually
    # explaining products. Treat very short runs as boundary noise.
    stable_seconds = max(30.0, float(settings.get("switch_confirm_seconds") or 0))
    min_export_seconds = 10.0
    stable_runs: list[dict[str, Any]] = []
    index = 0
    while index < len(runs):
        run = dict(runs[index])
        run_duration = float(run["end"]) - float(run["start"])
        next_run = runs[index + 1] if index + 1 < len(runs) else None
        prev_run = stable_runs[-1] if stable_runs else None
        if run_duration < stable_seconds:
            if prev_run and next_run and prev_run["key"] == next_run["key"]:
                prev_run["end"] = float(next_run["end"])
                index += 2
                continue
            index += 1
            continue
        if stable_runs and stable_runs[-1]["key"] == run["key"]:
            stable_runs[-1]["end"] = float(run["end"])
        else:
            stable_runs.append(run)
        index += 1

    return [
        run["change"]
        | {
            "_stable_start": round(float(run["start"]), 3),
            "_stable_end": round(float(run["end"]), 3),
            "_stable_duration": round(max(0.0, float(run["end"]) - float(run["start"])), 3),
        }
        for run in stable_runs
        if float(run["end"]) - float(run["start"]) >= min_export_seconds
    ]


def _write_live_product_split_queue(output: Path, room_dir: Path, payload: LiveRecPayload) -> dict[str, Any]:
    settings = _live_product_split_settings(payload)
    info = _probe_video_info(str(output))
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        duration = min(float(_live_segment_seconds(payload.segment)), float(settings["default_seconds"]))
    segments = _fallback_live_product_segments(duration, settings)
    _apply_live_product_segment_names(segments, payload, output.stem)
    created_at = datetime.now().isoformat(timespec="seconds")
    queue = {
        "version": 1,
        "created_at": created_at,
        "recording": str(output),
        "recording_original": str(summary.get("recording_original_output") or ""),
        "recording_remuxed_to_mp4": bool(summary.get("recording_remuxed_to_mp4")),
        "recording_duration": round(duration, 3),
        "mode": "duration_fallback",
        "room_name": payload.room_name,
        "room_url": payload.room_url,
        "settings": settings,
        "naming_mode": _live_product_naming_mode(payload),
        "catalog": [],
        "segments": segments,
        "recording_log": str(output.with_suffix(".ffmpeg.log")),
    }
    timeline_path = output.with_suffix(".timeline.jsonl")
    queue_path = output.with_suffix(".split_queue.json")
    queue["timeline_path"] = str(timeline_path)
    queue["queue_path"] = str(queue_path)
    queue["clips_dir"] = str(room_dir)
    with timeline_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "recording", "created_at": created_at, "path": str(output), "duration": round(duration, 3)}, ensure_ascii=False) + "\n")
        for segment in segments:
            fh.write(json.dumps({"type": "segment", **segment}, ensure_ascii=False) + "\n")
    _write_live_record_user_artifacts(queue)
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    return queue


def _douyin_active_probe_script() -> Path:
    script_name = "douyin_active_product_probe_poc.py"
    candidates = [
        USER_UPDATE_ROOT / "web_client" / "tools" / script_name,
        WEB_DIR / "tools" / script_name,
        MODULE_WEB_DIR / "tools" / script_name,
        USER_UPDATE_ROOT / "tools" / script_name,
        WEB_DIR.parent / "tools" / script_name,
        TOOLS_DIR / script_name,
        BUNDLE_DIR / "tools" / script_name,
        Path(sys.executable).resolve().parent / "_internal" / "tools" / script_name,
        Path(sys.executable).resolve().parent / "tools" / script_name,
    ]
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    raise FileNotFoundError("未找到抖音商品时间线探针脚本。请确认 tools/douyin_active_product_probe_poc.py 存在。")


def _short_file_hash(path: Path, length: int = 12) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:length]
    except Exception:
        return "unknown"


def _manifest_expected_hash(relative_path: str) -> str:
    try:
        data = json.loads((APP_DIR / "version.json").read_text(encoding="utf-8-sig"))
        files = data.get("files") if isinstance(data.get("files"), dict) else {}
        return str(files.get(relative_path.replace("\\", "/")) or "").strip().lower()
    except Exception:
        return ""


def _validate_active_probe_script(script: Path) -> None:
    expected = _manifest_expected_hash("web_client/tools/douyin_active_product_probe_poc.py")
    if not expected:
        return
    actual = _short_file_hash(script, 64)
    if actual and actual != "unknown" and actual.lower() != expected.lower():
        raise RuntimeError(
            "当前直播商品探针版本不一致，已停止录制以避免误识别。"
            f"当前脚本：{script}，当前哈希：{actual[:12]}，期望哈希：{expected[:12]}。"
            "请更新后完全退出 LiveClipperWeb.exe 再重新打开。"
        )


def _python_tool_command(script: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--liveclipper-run-tool", str(script)]
    return [sys.executable or "python", str(script)]


def _normalize_live_stream_quality(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    for encoding in ("latin1", "cp1252"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
            if repaired and repaired != text:
                text = repaired.strip()
                break
        except Exception:
            pass
    compact = re.sub(r"\s+", "", text).lower()
    if not compact:
        return fallback
    if re.search(r"(2160|1440|4k|uhd)", compact):
        return "原画"
    if re.search(r"(1080|full[_-]?hd|fhd|蓝光|藍光|超清)", compact):
        return "1080p"
    if re.search(r"(720|hd|高清)", compact):
        return "720p"
    if re.search(r"(540|480|sd|标清|標清)", compact):
        return "480p"
    if re.search(r"(360|ld|low|低清)", compact):
        return "低清"
    if "原画" in text or "原畫" in text or "origin" in compact or "source" in compact:
        return "原画"
    if "未知" in text or "unknown" in compact:
        return "未知清晰度"
    if "自动" in text or "auto" in compact:
        return "自动最高"
    # Never surface mojibake in the quality chip; it is better to be explicit
    # that the stream quality could not be named than to show broken Chinese.
    if re.search(r"[\ufffd?]|锛|銆|鐨|璇|绋|鏅|浜|妫|棰|[ÃÂÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞßàáâãäåæçèéêëìíîï]", text):
        return fallback or "未知清晰度"
    return text


def _active_probe_segments_from_timeline(timeline_path: Path, duration: float, settings: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if timeline_path.exists():
        with timeline_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if item.get("type") == "active_product_change":
                    changes.append(item)
    if not changes:
        segments = _fallback_live_product_segments(duration, settings)
        for segment in segments:
            segment["source"] = "pending_user_confirm"
            segment["confidence"] = "unresolved"
            segment["reason"] = "active_product_unresolved"
            segment["review_status"] = "pending_user_confirm"
        return segments

    head_seconds = float(settings["head_seconds"])
    tail_seconds = float(settings["tail_seconds"])
    min_seconds = float(settings["min_seconds"])
    segment_seconds = max(min_seconds, min(float(settings["default_seconds"]), float(settings["max_seconds"])))
    segments: list[dict[str, Any]] = []
    sorted_changes = _stable_active_product_changes(changes, duration, settings)
    for index, change in enumerate(sorted_changes, start=1):
        start = max(0.0, float(change.get("_stable_start", change.get("elapsed") or 0) or 0))
        end = max(start, min(duration, float(change.get("_stable_end", duration) or duration)))
        if end - start < 1:
            continue
        product = change.get("product") if isinstance(change.get("product"), dict) else {}
        product_title = str(product.get("title") or product.get("name") or product.get("product_name") or "").strip()
        bound_id = str(product.get("product_id") or product.get("promotion_id") or "").strip()
        title = str(product_title or bound_id or f"单品候选_{index:02d}").strip() or f"单品候选_{index:02d}"
        chunk_start = start
        while chunk_start < end - 0.5:
            chunk_end = min(end, chunk_start + segment_seconds)
            remainder = end - chunk_end
            if (
                0 < remainder < min_seconds
                and chunk_start > start
                and segments
                and segments[-1].get("source") == "active_product_change"
                and abs(float(segments[-1].get("active_product_start") or -1) - start) < 0.01
            ):
                segments[-1]["end"] = round(end, 3)
                segments[-1]["cut_end"] = round(min(duration, end + tail_seconds), 3)
                segments[-1]["duration"] = round(max(0.0, end - float(segments[-1]["start"])), 3)
                break
            if 0 < remainder < min_seconds:
                chunk_end = end
            if chunk_end - chunk_start < 1:
                break
            segments.append(
                {
                    "index": len(segments) + 1,
                    "source": "active_product_change",
                    "confidence": change.get("confidence") or "",
                    "reason": change.get("reason") or "",
                    "review_status": "auto_bound",
                    "start": round(chunk_start, 3),
                    "end": round(chunk_end, 3),
                    "cut_start": round(max(0.0, chunk_start - head_seconds), 3),
                    "cut_end": round(min(duration, chunk_end + tail_seconds), 3),
                    "duration": round(max(0.0, chunk_end - chunk_start), 3),
                    "title": title[:80],
                    "product_id": str(product.get("product_id") or ""),
                    "promotion_id": str(product.get("promotion_id") or ""),
                    "detail_url": str(product.get("detail_url") or ""),
                    "evidence_source": change.get("source") or "",
                    "active_product_start": round(start, 3),
                    "active_product_end": round(end, 3),
                    "chunked_by_duration": end - start > segment_seconds + 0.5,
                }
            )
            chunk_start = chunk_end
    return segments


def _write_active_probe_split_queue(summary_path: Path, room_dir: Path, payload: LiveRecPayload) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    output = Path(str(summary.get("recording_output") or ""))
    if not output.exists() or output.stat().st_size < 1000:
        raise RuntimeError("active_product_probe 未生成有效录制文件。")
    original_output = output
    output = _remux_live_recording_to_mp4(output, "live-rec", _safe_stem(payload.room_name or "live"))
    if output != original_output:
        summary["recording_original_output"] = str(original_output)
        summary["recording_output"] = str(output)
        summary["recording_remuxed_to_mp4"] = True
        try:
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    settings = _live_product_split_settings(payload)
    info = _probe_video_info(str(output))
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        duration = float(summary.get("duration_sec") or _live_segment_seconds(payload.segment))
    timeline_path = Path(str(summary.get("timeline_path") or output.with_suffix(".timeline.jsonl")))
    segments = _active_probe_segments_from_timeline(timeline_path, duration, settings)
    _apply_live_product_segment_names(segments, payload, output.stem)
    created_at = datetime.now().isoformat(timespec="seconds")
    queue = {
        "version": 2,
        "created_at": created_at,
        "recording": str(output),
        "recording_duration": round(duration, 3),
        "mode": "active_product_probe" if summary.get("active_product_changed") else "active_product_unresolved_fallback",
        "room_name": payload.room_name,
        "room_url": payload.room_url,
        "settings": settings,
        "naming_mode": _live_product_naming_mode(payload),
        "catalog": [],
        "segments": segments,
        "probe_summary": str(summary_path),
        "probe_report": str(summary.get("report_path") or ""),
        "probe_events": str(summary.get("probe_path") or ""),
        "product_evidence_log": str(summary.get("product_evidence_log_path") or ""),
        "probe_timeline": str(timeline_path),
        "probe_review_segments": str(summary.get("review_segments_path") or ""),
        "probe_catalog": str(summary.get("catalog_path") or ""),
        "recording_log": str(summary.get("recording_log") or output.with_suffix(".ffmpeg.log")),
        "recording_remux_log": str(original_output.with_suffix(".remux.log")) if original_output.suffix.lower() == ".ts" else "",
        "stream_quality": _normalize_live_stream_quality(summary.get("stream_quality")),
        "probe_version": summary.get("probe_version") or "",
        "probe_build": summary.get("probe_build") or "",
        "probe_script_hash": _short_file_hash(_douyin_active_probe_script()),
        "active_product_changed": bool(summary.get("active_product_changed")),
        "strong_signal_count": int(summary.get("strong_signal_count") or 0),
        "candidate_signal_count": int(summary.get("candidate_signal_count") or 0),
        "product_evidence_count": int(summary.get("product_evidence_count") or 0),
        "active_product_candidate_count": int(summary.get("active_product_candidate_count") or 0),
        "status_2_candidate_count": int(summary.get("status_2_candidate_count") or 0),
        "active_product_confirmed_count": int(summary.get("active_product_confirmed_count") or 0),
        "active_product_rule_review_count": int(summary.get("active_product_rule_review_count") or 0),
        "unresolved_reason": summary.get("unresolved_reason") or "",
        "recording_returncode": summary.get("recording_returncode"),
    }
    queue_path = output.with_suffix(".split_queue.json")
    queue["timeline_path"] = str(timeline_path)
    queue["queue_path"] = str(queue_path)
    queue["clips_dir"] = str(room_dir)
    _write_live_record_user_artifacts(queue, summary)
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    return queue


def _newest_active_probe_summary(room_dir: Path, since: float) -> Path | None:
    summaries = [
        path for path in room_dir.glob("*.probe_summary.json")
        if path.stat().st_mtime >= since - 2
    ]
    if not summaries:
        return None
    summaries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return summaries[0]


def _terminate_process_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _request_live_probe_stop(info: dict[str, Any]) -> bool:
    proc = info.get("process")
    stdin = info.get("stdin")
    if not proc or proc.poll() is not None or not stdin:
        return False
    try:
        stdin.write(LIVE_PROBE_STOP_NOTE + "\n")
        stdin.flush()
        return True
    except Exception:
        return False


def _schedule_live_probe_force_stop(task_id: str, proc: subprocess.Popen[Any], name: str, scope: str, delay: float = 90.0) -> None:
    if not proc or proc.poll() is not None:
        return

    def _watchdog() -> None:
        time.sleep(delay)
        if proc.poll() is not None or not _is_task_cancelled(task_id):
            return
        emit_log("warning", f"{name}: 停止收尾超过 {int(delay)} 秒，强制结束录制后台进程。", scope)
        _terminate_process_tree(proc)

    threading.Thread(target=_watchdog, daemon=True).start()


def _run_douyin_active_probe_record(task_id: str, payload: LiveRecPayload, room_dir: Path, name: str, scope: str) -> tuple[Path, dict[str, Any] | None, list[str]]:
    script = _douyin_active_probe_script()
    _validate_active_probe_script(script)
    seconds = _live_segment_seconds(payload.segment)
    started = time.time()
    cmd = [
        *_python_tool_command(script),
        "--url",
        payload.room_url.strip(),
        "--seconds",
        str(seconds),
        "--stream-wait-seconds",
        "60",
        "--probe-interval",
        "1",
        "--output-dir",
        str(room_dir),
        "--room-name",
        f"{name}_active_probe",
        "--manual-markers",
    ]
    min_stream_quality = str(payload.min_stream_quality or "").strip()
    if min_stream_quality:
        cmd += ["--min-stream-quality", min_stream_quality]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["LIVECLIPPER_TOOL_STDIO_FALLBACK_LOG"] = str(room_dir / f"{_safe_stem(name)}_active_probe_stdio.log")
    _set_task_progress(task_id, 24, "启动抖音商品时间线探针")
    emit_log("info", "使用 active_product_probe 录制：只接受当前讲解商品强信号，货架列表仅用于补全。", scope)
    emit_log("info", f"{name}: active_product_probe 脚本：{script} ({_short_file_hash(script)})", scope)
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    with _LIVE_LOCK:
        _LIVE_PROCS[task_id] = {"process": proc, "output": "", "name": name, "stderr": "", "kind": "active_product_probe", "stdin": proc.stdin}
    stop_requested = False
    try:
        if proc.stdout:
            for line in proc.stdout:
                text = line.strip()
                if not text:
                    continue
                if "Starting ffmpeg recording" in text or "开始" in text:
                    _set_task(task_id, recording_started_at=time.time())
                    _set_task_progress(task_id, 35, "录制中，持续监控当前讲解商品")
                elif "Selected stream quality:" in text:
                    _set_task(task_id, stream_quality=_normalize_live_stream_quality(text.split("Selected stream quality:", 1)[-1].strip()))
                elif "Probe summary" in text:
                    _set_task_progress(task_id, 86, "整理商品时间线")
                emit_log("info", f"{name}: {text}", scope)
                if _is_task_cancelled(task_id):
                    with _LIVE_LOCK:
                        live_info = _LIVE_PROCS.get(task_id) or {}
                    _request_live_probe_stop(live_info or {"process": proc, "stdin": proc.stdin})
                    stop_requested = True
                    _set_task(task_id, message="正在停止并整理已录内容", error="")
                    break
        if _is_task_cancelled(task_id) and stop_requested:
            try:
                rc = proc.wait(timeout=90)
            except subprocess.TimeoutExpired:
                emit_log("warning", f"{name}: 等待探针收尾超时，强制结束录制进程。", scope)
                _terminate_process_tree(proc)
                rc = proc.wait()
        else:
            rc = proc.wait()
    finally:
        with _LIVE_LOCK:
            info = _LIVE_PROCS.get(task_id)
            if info and info.get("process") is proc:
                _LIVE_PROCS.pop(task_id, None)
    was_cancelled = _is_task_cancelled(task_id)
    summary_path = _newest_active_probe_summary(room_dir, started)
    if was_cancelled and summary_path:
        emit_log("info", f"{name}: 停止请求已收尾，继续保存已录内容并生成单品队列。", scope)
    if rc != 0:
        if summary_path:
            emit_log("warning", f"{name}: active_product_probe 已写出报告但退出码异常 {rc}，将按已生成结果继续处理。", scope)
        elif was_cancelled:
            raise RuntimeError("录制已停止，未生成可回收的录制摘要。")
        else:
            raise RuntimeError(f"active_product_probe 录制失败，退出码 {rc}。")
    if not summary_path:
        if was_cancelled:
            raise RuntimeError("录制已停止，未生成可回收的录制摘要。")
        raise RuntimeError("active_product_probe 未生成 probe_summary.json。")
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    recording_rc = summary.get("recording_returncode")
    if recording_rc not in (None, 0):
        emit_log("warning", f"{name}: 录制进程异常结束，退出码 {recording_rc}，将尽量保留已写入内容。", scope)
    if summary.get("stream_quality"):
        _set_task(task_id, stream_quality=_normalize_live_stream_quality(summary.get("stream_quality")))
    if not summary.get("stream_found") or not summary.get("recording_output"):
        if summary.get("recording_blocked_reason") == "stream_quality_below_minimum":
            selected_quality = _normalize_live_stream_quality(summary.get("stream_quality"), "未知清晰度")
            min_quality = _normalize_live_stream_quality(summary.get("min_stream_quality") or payload.min_stream_quality, "自动最高")
            raise RuntimeError(f"直播流清晰度未达到要求：当前捕获 {selected_quality}，最低要求 {min_quality}，未启动录制。")
        raise RuntimeError("直播间未直播或未拿到直播流，未启动录制。")
    queue = _write_active_probe_split_queue(summary_path, room_dir, payload)
    output = Path(str(queue.get("recording") or ""))
    report = queue.get("probe_report") or ""
    if queue.get("active_product_changed"):
        emit_log("success", f"{name}: 已捕获当前讲解商品强信号：{queue.get('strong_signal_count')} 条。", scope)
    elif queue.get("active_product_candidate_count"):
        emit_log("warning", f"{name}: 已捕获 {queue.get('active_product_candidate_count')} 个商品候选，但缺少二次佐证，已进入待确认。", scope)
    else:
        emit_log("warning", f"{name}: 未捕获当前讲解商品强信号，已生成待确认单品候选段，不绑定商品ID。", scope)
    emit_log("success", f"{name}: 商品探针报告：{Path(report).name if report else summary_path.name}", scope)
    return output, queue, []


def _export_live_product_split_queue(task_id: str, queue: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    segments = list(queue.get("segments") or [])
    if not segments:
        return []
    from schedule_splitter import extract_by_schedule

    clips_dir = Path(str(queue.get("clips_dir") or ""))
    clips_dir.mkdir(parents=True, exist_ok=True)
    groups_by_name: dict[str, dict[str, Any]] = {}
    exact_segments_by_title: dict[str, dict[str, Any]] = {}
    for segment in segments:
        title = _clean_forbidden_title_text(str(segment.get("filename_stem") or _live_product_segment_filename(segment)), fallback="单品候选")
        segment["filename_stem"] = title
        segment["naming_source"] = segment.get("naming_source") or _live_product_segment_naming_source(segment)
        start = float(segment.get("cut_start") or segment.get("start") or 0)
        end = float(segment.get("cut_end") or segment.get("end") or start)
        if end - start < 1:
            continue
        exact_product_name = (
            (
                segment.get("naming_mode") == "product_id"
                and segment.get("naming_source") in {"auto_product_id", "auto_promotion_id"}
            )
            or segment.get("naming_source") == "product_name_timestamp"
        )
        if exact_product_name:
            previous = exact_segments_by_title.get(title)
            if previous and end - start <= float(previous.get("duration") or 0):
                continue
            exact_segments_by_title[title] = {"segment": segment, "start": start, "end": end, "duration": end - start}
            continue
        group = groups_by_name.get(title)
        if not group:
            group = {
                "name": title,
                "product_id": segment.get("product_id") or "",
                "promotion_id": segment.get("promotion_id") or "",
                "detail_url": segment.get("detail_url") or "",
                "naming_source": segment.get("naming_source") or "",
                "exact_name": (
                    segment.get("naming_mode") == "product_id"
                    and segment.get("naming_source") in {"auto_product_id", "auto_promotion_id"}
                ) or segment.get("naming_source") == "product_name_timestamp",
                "flat_output": True,
                "segments": [],
                "total_duration": 0.0,
            }
            groups_by_name[title] = group
        group["segments"].append((start, end))
        group["total_duration"] = float(group.get("total_duration") or 0) + max(0.0, end - start)
    for title, item in exact_segments_by_title.items():
        segment = item["segment"]
        start = float(item["start"])
        end = float(item["end"])
        groups_by_name[title] = {
            "name": title,
            "product_id": segment.get("product_id") or "",
            "promotion_id": segment.get("promotion_id") or "",
            "detail_url": segment.get("detail_url") or "",
            "naming_source": segment.get("naming_source") or "",
            "exact_name": True,
            "flat_output": True,
            "segments": [(start, end)],
            "total_duration": max(0.0, end - start),
        }
    groups = []
    for group in groups_by_name.values():
        if group.get("exact_name") and group.get("naming_source") == "product_name_timestamp":
            target = clips_dir / f"{_clean_forbidden_title_text(str(group.get('name') or ''), fallback='单品候选')}.mp4"
            if target.is_file() and target.stat().st_size > 1000:
                continue
        groups.append(group)
    if not groups:
        return []
    _set_task_progress(task_id, 92, f"切出 {len(groups)} 个单品候选段")
    return extract_by_schedule(
        groups,
        [str(queue.get("recording") or "")],
        str(clips_dir),
        ffmpeg=_ffmpeg_cmd(),
        log_fn=_task_log_fn(task_id, scope, base=92, span=6),
    )


def _is_within_directory(path: Path, directory: Path) -> bool:
    try:
        resolved = path.resolve()
        base = directory.resolve()
        return resolved == base or str(resolved).lower().startswith(str(base).lower() + os.sep)
    except Exception:
        return False


def _live_product_intermediate_paths(queue: dict[str, Any] | None) -> list[Path]:
    if not queue:
        return []
    paths: list[Path] = []
    for key in (
        "timeline_path",
        "queue_path",
        "probe_summary",
        "probe_report",
        "probe_events",
        "product_evidence_log",
        "probe_timeline",
        "probe_review_segments",
        "probe_catalog",
        "recording_log",
        "recording_remux_log",
        "product_timeline_json",
        "recording_summary",
    ):
        raw = str(queue.get(key) or "").strip()
        if raw:
            paths.append(Path(raw))
    recording = Path(str(queue.get("recording") or ""))
    if str(recording):
        for suffix in (
            ".timeline.jsonl",
            ".split_queue.json",
            ".active_product_probe.jsonl",
            ".product_evidence_log.jsonl",
            ".product_timeline.json",
            ".recording_summary.json",
            ".probe_summary.json",
            ".review_segments.json",
            ".catalog.jsonl",
            ".active_product_probe_report.md",
            ".ffmpeg.log",
            ".remux.log",
        ):
            paths.append(recording.with_suffix(suffix))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _archive_live_product_diagnostics(queue: dict[str, Any] | None, room_dir: Path, name: str) -> Path | None:
    if not queue:
        return None
    recording = Path(str(queue.get("recording") or ""))
    stamp = _live_product_recording_stamp(recording.stem if str(recording) else "")
    try:
        archive_dir = _safe_user_child("live_rec_diagnostics", _safe_stem(name), stamp)
        archive_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    copied = 0
    recording_key = str(recording.resolve()).lower() if recording.exists() else ""
    for path in _live_product_intermediate_paths(queue):
        try:
            if not path.is_file():
                continue
            resolved = path.resolve()
            if recording_key and str(resolved).lower() == recording_key:
                continue
            if not _is_within_directory(resolved, room_dir):
                continue
            target = archive_dir / resolved.name
            if target.exists():
                target = archive_dir / f"{resolved.stem}_{int(time.time())}{resolved.suffix}"
            shutil.copy2(str(resolved), str(target))
            copied += 1
        except Exception:
            continue
    return archive_dir if copied else None


def _cleanup_live_product_intermediates(
    queue: dict[str, Any] | None,
    product_outputs: list[str],
    room_dir: Path,
    scope: str,
    name: str,
) -> list[str]:
    if not queue or not product_outputs:
        return []
    keep = {
        str(Path(path).resolve()).lower()
        for path in product_outputs
        if str(path or "").strip()
    }
    removed: list[str] = []
    keep_debug = str(os.environ.get("LIVECLIPPER_KEEP_LIVE_DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}
    debug_dir = room_dir / ".liveclipper_debug"
    archived_dir = _archive_live_product_diagnostics(queue, room_dir, name)
    for path in _live_product_intermediate_paths(queue):
        try:
            if not path.is_file():
                continue
            resolved = path.resolve()
            if str(resolved).lower() in keep:
                continue
            if not _is_within_directory(resolved, room_dir):
                continue
            if keep_debug:
                debug_dir.mkdir(parents=True, exist_ok=True)
                target = debug_dir / resolved.name
                if target.exists():
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    target = debug_dir / f"{resolved.stem}_{stamp}{resolved.suffix}"
                shutil.move(str(resolved), str(target))
                removed.append(str(target))
            else:
                resolved.unlink()
                removed.append(str(resolved))
        except Exception as exc:
            emit_log("warning", f"{name}: 清理中间文件失败：{path.name}，{exc}", scope)
    if removed:
        action = "归档" if keep_debug else "删除"
        emit_log("info", f"{name}: 已{action} {len(removed)} 个录制中间文件，直播间目录保留母带和单品视频。", scope)
    if archived_dir:
        emit_log("info", f"{name}: 诊断文件已保存到：{archived_dir}", scope)
    return removed


def _live_record_error_text(stderr_path: Path) -> str:
    try:
        if stderr_path.exists():
            return stderr_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return ""


def _resolve_live_url(url: str, scope: str) -> str:
    url = _extract_url_from_text(url)
    lower = url.lower()
    is_douyin_page = "douyin.com" in lower or "webcast.amemv.com/douyin/webcast/reflow" in lower
    if is_douyin_page and not lower.endswith(".flv") and not lower.endswith(".m3u8"):
        from douyin_stream import extract_live_url

        resolved = extract_live_url(url, log_fn=lambda msg: emit_log("info", msg, scope))
        if resolved:
            return resolved
        raise RuntimeError("未解析到可用直播流。请确认直播间正在开播，或先点击“检测流地址”查看原因。")
    return url


def _extract_url_from_text(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"https?://[^\s\"'<>，。；、]+", text)
    if match:
        return match.group(0).rstrip(".,;:，。；：")
    match = re.search(r"(?:live|v)\.douyin\.com/[^\s\"'<>，。；、]+", text, re.I)
    if match:
        return "https://" + match.group(0).rstrip(".,;:，。；：")
    return text


def _live_room_task_key(value: Any) -> str:
    text = _extract_url_from_text(value).strip()
    if not text:
        return ""
    decoded = urllib.parse.unquote(text)
    patterns = [
        r"live\.douyin\.com/(\d+)",
        r"webcast\.amemv\.com/douyin/webcast/reflow/(\d+)",
        r"[?&]room_id=(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, decoded, re.I)
        if match:
            return f"douyin:{match.group(1)}"
    parsed = urllib.parse.urlparse(decoded if re.match(r"https?://", decoded, re.I) else f"https://{decoded}")
    host = (parsed.netloc or "").lower()
    path = re.sub(r"/+$", "", parsed.path or "")
    if host and path:
        return f"{host}{path}".lower()
    return re.sub(r"[?#].*$", "", decoded).rstrip("/").lower()


def _find_running_live_record_task(room_url: Any) -> dict[str, Any] | None:
    key = _live_room_task_key(room_url)
    if not key:
        return None
    with _TASK_LOCK:
        tasks = list(_TASKS.values())
    for task in reversed(tasks):
        if task.get("scope") != "live-rec" or task.get("title") != "直播录制":
            continue
        if task.get("status") not in {"queued", "running"}:
            continue
        if _live_room_task_key(task.get("live_room_url")) == key:
            return dict(task)
    return None


def _resolve_douyin_short_page_url(url: str, scope: str = "live-rec") -> str:
    text = str(url or "").strip()
    if not re.search(r"https?://v\.douyin\.com/", text, re.I):
        return text
    request = urllib.request.Request(
        text,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            resolved = str(response.geturl() or "").strip()
    except Exception as exc:
        emit_log("warning", f"抖音短链解析失败，将交给 Chrome 自动跳转：{exc}", scope)
        return text
    if resolved and resolved != text:
        emit_log("info", f"已解析抖音短链：{resolved}", scope)
        return resolved
    return text


def _should_use_douyin_active_probe(payload: LiveRecPayload, url: str) -> bool:
    url = _extract_url_from_text(url)
    lower = str(url or "").lower()
    is_douyin_page = "douyin.com" in lower or "webcast.amemv.com/douyin/webcast/reflow" in lower
    if not is_douyin_page:
        return False
    if lower.endswith((".flv", ".m3u8")) or ".flv?" in lower or ".m3u8?" in lower:
        return False
    return True


def _run_live_detect(task_id: str, payload: LiveRecPayload) -> None:
    scope = "live-rec"
    _set_task(task_id, status="running", started_at=time.time(), progress=10, message="检测直播流")
    try:
        url = (payload.room_url or "").strip()
        if not url:
            raise ValueError("请填写直播间地址。")
        payload.room_url = _resolve_douyin_short_page_url(_extract_url_from_text(url), scope)
        _set_task(task_id, live_room_url=payload.room_url)
        resolved = _resolve_live_url(payload.room_url, scope)
        _set_task_progress(task_id, 65, "验证直播流")
        proc = subprocess.run([_ffprobe_cmd(), "-v", "quiet", "-show_streams", "-of", "json", resolved], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if proc.returncode != 0:
            raise RuntimeError("未检测到可用直播流。")
        _set_task(task_id, status="completed", finished_at=time.time(), stream_url=resolved)
        emit_log("success", "直播流检测通过。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"直播流检测失败：{exc}", scope)


def _run_live_record(task_id: str, payload: LiveRecPayload) -> None:
    scope = "live-rec"
    _set_task(task_id, status="running", started_at=time.time(), progress=8, message="准备直播录制")
    try:
        _ensure_feature_access("直播录制")
        url = (payload.room_url or "").strip()
        if not url:
            raise ValueError("请填写直播间地址。")
        payload.room_url = _resolve_douyin_short_page_url(_extract_url_from_text(url), scope)
        _set_task(task_id, live_room_url=payload.room_url)
        url = payload.room_url
        save_dir = _clean_path(payload.save_dir) if payload.save_dir.strip() else Path.home() / "Videos" / "直播录制"
        name = _safe_stem(payload.room_name or "live")
        room_dir = save_dir / name
        room_dir.mkdir(parents=True, exist_ok=True)
        if _should_use_douyin_active_probe(payload, url):
            rolling_seconds = _live_segment_seconds(payload.segment)
            rolling_enabled = rolling_seconds < _live_segment_seconds("不限")
            cycle_index = 0
            completed_cycles = 0
            consumed_trial = False
            recording_outputs: list[str] = []
            product_outputs: list[str] = []
            last_output: Path | None = None
            last_queue: dict[str, Any] | None = None
            while not _is_task_cancelled(task_id):
                cycle_index += 1
                _set_task(task_id, status="running", progress=18, message=f"准备第 {cycle_index} 段直播录制")
                emit_log("info", f"{name}: 开始第 {cycle_index} 段录制。", scope)
                try:
                    output, product_queue, _ = _run_douyin_active_probe_record(task_id, payload, room_dir, name, scope)
                except Exception as cycle_exc:
                    if _is_task_cancelled(task_id):
                        break
                    if recording_outputs and re.search(r"未直播|未拿到直播流|未生成 probe_summary|no_stream", str(cycle_exc), re.I):
                        emit_log("warning", f"{name}: 继续录制时直播已不可用，已保留前 {len(recording_outputs)} 段。", scope)
                        break
                    raise
                last_output = output
                last_queue = product_queue
                recording_outputs.append(str(output))
                completed_cycles += 1
                size = output.stat().st_size if output.exists() else 0
                cycle_product_outputs: list[str] = []
                if payload.product_auto_cut and product_queue:
                    split_results = _export_live_product_split_queue(task_id, product_queue, scope)
                    cycle_product_outputs = [
                        str(item.get("output_path") or "")
                        for item in split_results
                        if item.get("output_path") and Path(str(item.get("output_path"))).is_file()
                    ]
                    for output_path in cycle_product_outputs:
                        if output_path and output_path not in product_outputs:
                            product_outputs.append(output_path)
                    if cycle_product_outputs:
                        emit_log("success", f"{name}: 第 {cycle_index} 段自动分割完成：导出 {len(cycle_product_outputs)} 个单品候选段。", scope)
                        _cleanup_live_product_intermediates(product_queue, cycle_product_outputs, room_dir, scope, name)
                    else:
                        emit_log("warning", f"{name}: 第 {cycle_index} 段未导出有效片段，请检查队列时长和源视频。", scope)
                if not consumed_trial:
                    _consume_trial("直播录制", scope=scope)
                    consumed_trial = True
                product_stats = _live_product_queue_stats(product_queue)
                partial_payload: dict[str, Any] = {
                    "output": str(output),
                    "recording_outputs": recording_outputs,
                    "rolling_cycle_count": completed_cycles,
                    "product_timeline": product_queue.get("timeline_path") if product_queue else "",
                    "product_timeline_json": product_queue.get("product_timeline_json") if product_queue else "",
                    "product_split_queue": product_queue.get("queue_path") if product_queue else "",
                    "recording_summary": product_queue.get("recording_summary") if product_queue else "",
                    "product_evidence_log": product_queue.get("product_evidence_log") if product_queue else "",
                    "product_segments": len(product_queue.get("segments") or []) if product_queue else 0,
                    "product_clips_dir": product_queue.get("clips_dir") if product_queue else "",
                    "probe_summary": product_queue.get("probe_summary") if product_queue else "",
                    "probe_report": product_queue.get("probe_report") if product_queue else "",
                    "stream_quality": _normalize_live_stream_quality(product_queue.get("stream_quality")) if product_queue else "",
                    **product_stats,
                }
                if product_outputs:
                    partial_payload["outputs"] = product_outputs
                    partial_payload["result_count"] = len(product_outputs)
                _set_task(task_id, **partial_payload)
                emit_log("success", f"{name}: 第 {cycle_index} 段录制完成：{output.name} ({size / 1024 / 1024:.1f}MB)", scope)
                if not rolling_enabled:
                    break
                if _is_task_cancelled(task_id):
                    break
                _set_task(task_id, status="running", progress=35, message=f"第 {cycle_index} 段完成，继续录制下一段")
                emit_log("info", f"{name}: 第 {cycle_index} 段已处理完成，继续录制下一段。", scope)
                with _LIVE_LOCK:
                    _LIVE_PROCS[task_id] = {"process": None, "output": str(output), "name": name, "stderr": "", "kind": "active_product_probe_rolling"}
                time.sleep(1.0)
            if _is_task_cancelled(task_id):
                message = f"录制已停止，已保留 {len(recording_outputs)} 段录制文件。"
                result_payload = {
                    "status": "cancelled",
                    "finished_at": time.time(),
                    "message": message,
                    "output": str(last_output) if last_output else "",
                    "recording_outputs": recording_outputs,
                    "rolling_cycle_count": completed_cycles,
                }
                if product_outputs:
                    result_payload["outputs"] = product_outputs
                    result_payload["result_count"] = len(product_outputs)
                _set_task(task_id, **result_payload)
                emit_log("warning", message, scope)
                return
            if not last_output:
                raise RuntimeError("直播录制未生成有效文件。")
            final_payload: dict[str, Any] = {
                "status": "completed",
                "finished_at": time.time(),
                "output": str(last_output),
                "recording_outputs": recording_outputs,
                "rolling_cycle_count": completed_cycles,
            }
            if last_queue:
                final_payload.update(
                    {
                        "product_timeline": last_queue.get("timeline_path"),
                        "product_timeline_json": last_queue.get("product_timeline_json"),
                        "product_split_queue": last_queue.get("queue_path"),
                        "recording_summary": last_queue.get("recording_summary"),
                        "product_evidence_log": last_queue.get("product_evidence_log"),
                        "product_segments": len(last_queue.get("segments") or []),
                        "product_clips_dir": last_queue.get("clips_dir"),
                        "probe_summary": last_queue.get("probe_summary"),
                        "probe_report": last_queue.get("probe_report"),
                        "stream_quality": _normalize_live_stream_quality(last_queue.get("stream_quality")),
                        **_live_product_queue_stats(last_queue),
                    }
                )
            if product_outputs:
                final_payload["outputs"] = product_outputs
                final_payload["result_count"] = len(product_outputs)
            _set_task(task_id, **final_payload)
            emit_log("success", f"{name}: 录制完成：共 {len(recording_outputs)} 段录制文件。", scope)
            return
        stream_url = _resolve_live_url(url, scope)
        _set_task_progress(task_id, 24, "解析直播流")
        output = room_dir / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.flv"
        stderr_path = output.with_suffix(".ffmpeg.log")
        cmd = [_ffmpeg_cmd(), "-y"]
        if str(stream_url).lower().startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30"]
        cmd += ["-i", stream_url, "-c", "copy", "-t", str(_live_segment_seconds(payload.segment)), str(output)]
        stderr_file = stderr_path.open("wb")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with _LIVE_LOCK:
            _LIVE_PROCS[task_id] = {"process": proc, "output": str(output), "name": name, "stderr": str(stderr_path)}
        _set_task_progress(task_id, 35, "录制中")
        emit_log("success", f"{name}: 开始录制", scope)
        try:
            rc = proc.wait()
        finally:
            try:
                stderr_file.close()
            except Exception:
                pass
        size = output.stat().st_size if output.exists() else 0
        if _is_task_cancelled(task_id):
            message = f"录制已停止，已保留文件：{output.name} ({size / 1024 / 1024:.1f}MB)" if size >= 1000 else "录制已停止，未生成有效文件。"
            _set_task(task_id, status="cancelled", finished_at=time.time(), output=str(output) if size >= 1000 else "", message=message)
            emit_log("warning", message, scope)
            return
        if rc != 0 or size < 1000:
            stderr_text = _live_record_error_text(stderr_path)
            detail = _short_error(stderr_text)
            hint = _solution_for_error(stderr_text)
            raise RuntimeError(f"录制进程结束但未生成有效文件：{detail}。解决办法：{hint}。日志：{stderr_path}")
        product_queue: dict[str, Any] | None = None
        product_outputs: list[str] = []
        if payload.product_split_enabled:
            try:
                _set_task_progress(task_id, 88, "生成单品分段队列")
                product_queue = _write_live_product_split_queue(output, room_dir, payload)
                emit_log("success", f"{name}: 已生成单品时间线：{Path(product_queue['timeline_path']).name}", scope)
                emit_log("success", f"{name}: 已生成待切割队列：{Path(product_queue['queue_path']).name}", scope)
                if payload.product_auto_cut:
                    split_results = _export_live_product_split_queue(task_id, product_queue, scope)
                    product_outputs = [
                        str(item.get("output_path") or "")
                        for item in split_results
                        if item.get("output_path") and Path(str(item.get("output_path"))).is_file()
                    ]
                    if product_outputs:
                        emit_log("success", f"{name}: 录后自动分割完成：导出 {len(product_outputs)} 个单品候选段。", scope)
                        _cleanup_live_product_intermediates(product_queue, product_outputs, room_dir, scope, name)
                    else:
                        emit_log("warning", f"{name}: 录后自动分割未导出有效片段，请检查队列时长和源视频。", scope)
            except Exception as split_exc:
                emit_log("warning", f"{name}: 录制完成，但单品分段队列生成失败：{split_exc}", scope)
        _consume_trial("直播录制", scope=scope)
        result_payload: dict[str, Any] = {"status": "completed", "finished_at": time.time(), "output": str(output)}
        if product_queue:
            result_payload.update(
                {
                    "product_timeline": product_queue.get("timeline_path"),
                    "product_timeline_json": product_queue.get("product_timeline_json"),
                    "product_split_queue": product_queue.get("queue_path"),
                    "recording_summary": product_queue.get("recording_summary"),
                    "product_evidence_log": product_queue.get("product_evidence_log"),
                    "product_segments": len(product_queue.get("segments") or []),
                    "product_clips_dir": product_queue.get("clips_dir"),
                    "stream_quality": _normalize_live_stream_quality(product_queue.get("stream_quality")),
                    **_live_product_queue_stats(product_queue),
                }
            )
        if product_outputs:
            result_payload["outputs"] = product_outputs
            result_payload["result_count"] = len(product_outputs)
        _set_task(task_id, **result_payload)
        emit_log("success", f"{name}: 录制完成：{output.name} ({size / 1024 / 1024:.1f}MB)", scope)
    except Exception as exc:
        if _is_task_cancelled(task_id):
            _set_task(task_id, status="cancelled", finished_at=time.time(), error="", message="直播录制已停止。")
            emit_log("warning", "直播录制已停止。", scope)
        else:
            _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
            emit_log("error", f"直播录制失败：{exc}", scope)
    finally:
        with _LIVE_LOCK:
            _LIVE_PROCS.pop(task_id, None)


def _stop_live_all() -> int:
    stopped = 0
    graceful_probe_stops = 0
    with _LIVE_LOCK:
        items = list(_LIVE_PROCS.items())
    for task_id, info in items:
        with _TASK_LOCK:
            _CANCELLED_TASKS.add(task_id)
            event = _TASK_CANCEL_EVENTS.get(task_id)
            if event:
                event.set()
        proc = info.get("process")
        if info.get("kind") == "active_product_probe" and _request_live_probe_stop(info):
            if proc:
                _schedule_live_probe_force_stop(task_id, proc, str(info.get("name") or "直播录制"), "live-rec")
            graceful_probe_stops += 1
            stopped += 1
            continue
        if proc and proc.poll() is None:
            _terminate_process_tree(proc)
            stopped += 1
        with _LIVE_LOCK:
            _LIVE_PROCS.pop(task_id, None)
    if graceful_probe_stops == 0:
        stopped += _stop_orphan_active_probe_processes()
    return stopped


def _stop_live_task_process(task_id: str) -> int:
    stopped = 0
    with _LIVE_LOCK:
        info = _LIVE_PROCS.get(task_id)
    if not info:
        return 0
    proc = info.get("process")
    if info.get("kind") == "active_product_probe" and _request_live_probe_stop(info):
        if proc:
            _schedule_live_probe_force_stop(task_id, proc, str(info.get("name") or "直播录制"), "live-rec")
        return 1
    if proc and proc.poll() is None:
        _terminate_process_tree(proc)
        stopped += 1
    with _LIVE_LOCK:
        _LIVE_PROCS.pop(task_id, None)
    return stopped


def _stop_orphan_active_probe_processes() -> int:
    if os.name != "nt":
        return 0
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'python|ffmpeg' -and "
        "($_.CommandLine -match 'douyin_active_product_probe_poc.py' -or $_.CommandLine -match '_active_probe_') } | "
        "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; 1 } catch {} }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return sum(1 for line in result.stdout.splitlines() if line.strip() == "1")
    except Exception:
        return 0


def _mark_live_product_switch(note: str = "") -> int:
    marked = 0
    marker_note = note.strip() or f"manual marker from LiveClipper UI {datetime.now().isoformat(timespec='seconds')}"
    with _LIVE_LOCK:
        items = list(_LIVE_PROCS.items())
    for _task_id, info in items:
        if info.get("kind") != "active_product_probe":
            continue
        proc = info.get("process")
        stdin = info.get("stdin")
        if not proc or proc.poll() is not None or not stdin:
            continue
        try:
            stdin.write(marker_note + "\n")
            stdin.flush()
            marked += 1
        except Exception:
            continue
    return marked


def _run_smart_cut(task_id: str, payload: SmartCutPayload) -> None:
    _set_task(task_id, status="running", started_at=time.time(), progress=5, message="准备智能成片")
    emit_log("info", "智能成片任务已启动。", "smart-cut")
    try:
        _ensure_feature_access("智能成片")
        from cutter_logic import process_video, process_video_multi

        paths = [Path(p.strip().strip('"')) for p in payload.video_paths if p.strip()]
        if not paths:
            raise ValueError("请至少填写一个视频文件路径。")
        total_videos = max(1, len(paths))
        _set_task(task_id, batch_total=total_videos, batch_done=0, batch_current=1 if total_videos > 1 else 0, batch_failed=0, batch_label="")
        _set_task_progress(task_id, 8, f"校验 {total_videos} 个素材")

        outputs: list[str] = []
        failures: list[str] = []

        for index, video in enumerate(paths, start=1):
            if _is_task_cancelled(task_id):
                emit_log("warning", "任务已停止。", "smart-cut")
                return
            file_base = 10 + ((index - 1) / total_videos) * 82
            file_span = 82 / total_videos
            try:
                if not video.exists():
                    raise FileNotFoundError(f"视频不存在：{video}")

                out_dir = _default_output_dir(video, payload.output_dir, "output")
                if payload.output_dir.strip():
                    out_path = out_dir / f"{_safe_stem(video.stem)}_smart_cut_{_stamp_name()}.mp4"
                else:
                    out_path = out_dir / f"{video.stem}_爆款切片_{_stamp_name()}.mp4"

                pip_path, used_pip_file = _pick_pip_asset(payload, "smart-cut")
                emit_log("info", f"[{index}/{len(paths)}] 开始处理 {video.name}", "smart-cut")
                _set_task_progress(task_id, file_base, f"处理 {index}/{total_videos}: {video.name}")
                version_count = payload.versions
                file_started_at = time.time()
                common_kwargs = dict(
                    srt_path=payload.srt_path.strip() or None,
                    output_path=str(out_path),
                    dedup_preset=payload.dedup_preset,
                    dedup_video_options=payload.video,
                    dedup_audio_options=payload.audio,
                    transition_options=payload.transition,
                    subtitle_overlay=payload.subtitle_overlay,
                    log_fn=_task_log_fn(task_id, "smart-cut", base=file_base + 4, span=max(12, file_span * 0.90)),
                    cancel_event=_task_cancel_event(task_id),
                    force_category=None if payload.category in ("", "自动检测") else payload.category,
                    pip_path=pip_path,
                    pip_size=payload.pip_size,
                    pip_opacity=payload.pip_opacity,
                    pip_pos=payload.pip_pos,
                    focus_hint=payload.focus_hint,
                    ai_controls=payload.ai_controls,
                    mirror_enabled=payload.mirror_enabled,
                    smart_crop_enabled=payload.smart_crop_enabled,
                    crop_level=payload.crop_level,
                    ken_burns_enabled=payload.ken_burns_enabled,
                    target_duration=payload.target_duration,
                )
                if version_count > 1:
                    result = process_video_multi(
                        str(video),
                        **common_kwargs,
                        num_versions=version_count,
                        kb_intensity=payload.ken_burns_intensity,
                    )
                else:
                    result = process_video(
                        str(video),
                        **common_kwargs,
                        kb_intensity=payload.ken_burns_intensity,
                    )
                if _is_task_cancelled(task_id):
                    emit_log("warning", "任务已停止。", "smart-cut")
                    return
                result_ok = bool(result.get("ok", True)) if isinstance(result, dict) else bool(result)
                if not result_ok:
                    reason = ""
                    if isinstance(result, dict):
                        reason = str(result.get("error") or result.get("message") or "").strip()
                    raise RuntimeError(f"{video.name} 处理失败：{reason}" if reason else f"{video.name} 处理失败。")
                produced_outputs = _collect_smart_cut_outputs(out_dir, video, file_started_at, out_path, result)
                if not produced_outputs:
                    raise RuntimeError(f"{video.name} 未生成输出文件。")
                outputs.extend(produced_outputs)
                _archive_used_pip(used_pip_file, "smart-cut")
                _set_task_progress(task_id, min(94, 10 + (index / total_videos) * 82), f"完成 {index}/{total_videos}: {video.name}")
                emit_log("success", f"处理完成: {video.name}", "smart-cut")
                _consume_trial("智能成片", scope="smart-cut")
            except Exception as video_exc:
                if _is_task_cancelled(task_id):
                    emit_log("warning", "任务已停止。", "smart-cut")
                    return
                failures.append(f"{video.name}: {video_exc}")
                _set_task_progress(task_id, min(94, 10 + (index / total_videos) * 82), f"跳过失败 {index}/{total_videos}: {video.name}")
                _set_task(task_id, batch_failed=len(failures))
                emit_log("error", f"[{index}/{total_videos}] 智能成片失败，已跳过继续下一个：{video.name}，{video_exc}", "smart-cut")

        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", "smart-cut")
            return
        if not outputs:
            detail = failures[0] if failures else "没有成功输出。"
            if total_videos > 1:
                raise RuntimeError(f"批量智能成片没有成功输出：{detail}")
            raise RuntimeError(detail)
        _set_task(
            task_id,
            status="completed",
            finished_at=time.time(),
            output=outputs[0] if outputs else "",
            outputs=outputs,
            result_count=len(outputs),
            message=f"智能成片完成：成功 {len(outputs)}/{total_videos} 个",
            batch_total=total_videos,
            batch_done=len(outputs),
            batch_current=0,
            batch_failed=len(failures),
            batch_label="",
        )
        if failures:
            emit_log("warning", f"智能成片批量完成：成功 {len(outputs)}/{total_videos} 个，失败 {len(failures)} 个。", "smart-cut")
        else:
            emit_log("success", f"智能成片任务完成，输出 {len(outputs)} 个文件。", "smart-cut")
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"智能成片失败: {exc}", "smart-cut")


def _run_mix_from_preview(task_id: str, payload: MixPreviewCutPayload) -> None:
    scope = "mix"
    _set_task(task_id, status="running", started_at=time.time(), progress=6, message="读取混剪预览片段")
    emit_log("info", "使用混剪 AI 选片预览开始成片。", scope)
    try:
        _ensure_feature_access("混剪成片")
        preview = _get_preview(payload.preview_id)
        if not preview or preview.get("status") != "ready":
            raise RuntimeError("混剪选片预览不存在或尚未完成。")
        raw_clips = list(preview.get("raw_clips") or [])
        if not raw_clips:
            raise RuntimeError("混剪选片预览里没有可用片段。")

        draft = _preview_selection_draft(preview)
        selected = payload.selected_indices or list(draft.get("selected_indices") or [])
        selected_segments = payload.selected_segments or dict(draft.get("selected_segments") or {})
        selected_indices = [int(i) for i in selected if 0 <= int(i) < len(raw_clips)]
        clips = _clips_from_preview_selection(preview, selected_indices, selected_segments)
        if not clips:
            raise RuntimeError("请至少保留一个片段再混剪。")
        _log_preview_selection(scope, "预览混剪子句选择", selected_indices, selected_segments, clips)
        _set_task_progress(task_id, 16, f"确认 {len(clips)} 个片段")

        sources = _preview_mix_source_paths(preview, payload.video_paths, clips)
        if not sources:
            sources = _existing_paths(payload.video_paths, "视频")
        missing_sources = [path for path in sources if not path.exists()]
        if missing_sources:
            missing_text = ", ".join(str(path) for path in missing_sources[:3])
            raise FileNotFoundError(f"预览片段对应的原视频不存在：{missing_text}")
        existing_sources = [path for path in sources if path.exists()]
        if not existing_sources:
            raise FileNotFoundError("预览片段对应的原视频不存在，请重新选择素材并生成预览。")
        emit_log(
            "info",
            "预览混剪素材顺序: " + " → ".join(f"V{index + 1}={path.name}" for index, path in enumerate(existing_sources)),
            scope,
        )
        _set_task_progress(task_id, 26, f"校验 {len(existing_sources)} 个素材")

        source_index = {
            _path_identity(path): index
            for index, path in enumerate(existing_sources)
            if path.exists()
        }

        def _num(value: Any, fallback: float = 0.0) -> float:
            try:
                return float(value)
            except Exception:
                return fallback

        emit_log("info", f"预览混剪：按预览保留结果使用 {len(clips)} 个片段/子句组，不二次去重。", scope)

        def _clip_tuple(clip: Any) -> tuple[Any, ...]:
            clip_info = _clip_public(0, clip)
            source = Path(str(clip_info.get("source") or existing_sources[0]))
            key = _path_identity(source)
            idx = source_index.get(key, 0)
            text = str(clip_info.get("text") or "")
            if "[V" not in text:
                text = f"[V{idx + 1}] {text}"
            start = _num(clip_info.get("start"), 0.0)
            end = _num(clip_info.get("end"), start)
            duration = _num(clip_info.get("duration"), max(0.0, end - start))
            source_text = str(clip_info.get("source") or existing_sources[idx])
            values: list[Any] = [
                clip_info.get("clip_type") or "product",
                text,
                start,
                end,
                _num(clip_info.get("score"), 0.0),
                duration,
                str(clip_info.get("focus") or ""),
                source_text,
            ]
            if isinstance(clip, (list, tuple)):
                meta = next(
                    (item for item in clip if isinstance(item, dict) and item.get("preview_exact")),
                    None,
                )
                if meta:
                    values.append(meta)
            return tuple(values)

        selected_tuples = [_clip_tuple(clip) for clip in clips]
        out_dir = _default_output_dir(existing_sources[0], payload.output_dir, "mix_output")
        output_path = _mix_output_path(out_dir, existing_sources[0])
        pip_path, used_pip_file = _pick_pip_asset(payload, scope)
        _set_task_progress(task_id, 34, "准备混剪输出")

        import ai_clipper as ai_mod
        from cutter_logic import process_video_mix

        original_ai_analyze = ai_mod.ai_analyze_clips
        original_ai_enabled = ai_mod.is_enabled

        def _preview_ai_analyze(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
            emit_log("info", f"预览混剪：使用已调整的 {len(selected_tuples)} 个片段/子句组，不重新 AI 选片。", scope)
            return list(selected_tuples)

        ai_mod.ai_analyze_clips = _preview_ai_analyze
        ai_mod.is_enabled = lambda: True
        try:
            result = process_video_mix(
                [str(path) for path in existing_sources],
                output_path=str(output_path),
                dedup_preset=payload.dedup_preset,
                dedup_video_options=payload.video,
                dedup_audio_options=payload.audio,
                transition_options=payload.transition,
                subtitle_overlay=payload.subtitle_overlay,
                log_fn=_task_log_fn(task_id, scope, base=34, span=54),
                cancel_event=_task_cancel_event(task_id),
                force_category=None if payload.category in ("", "自动检测") else payload.category,
                focus_hint=payload.focus_hint,
                ai_controls=payload.ai_controls,
                target_duration=payload.duration,
                num_versions=1,
                pip_path=pip_path or "",
                pip_size=payload.pip_size,
                pip_opacity=payload.pip_opacity,
                pip_pos=payload.pip_pos,
                smart_crop_enabled=payload.smart_crop_enabled,
                crop_level=payload.crop_level,
                ken_burns_enabled=payload.ken_burns_enabled,
                mirror_enabled=payload.mirror_enabled,
                kb_intensity=payload.ken_burns_intensity,
            )
        finally:
            ai_mod.ai_analyze_clips = original_ai_analyze
            ai_mod.is_enabled = original_ai_enabled

        if not result:
            raise RuntimeError("预览混剪处理失败。")
        _set_task_progress(task_id, 94, "整理输出")
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        _archive_used_pip(used_pip_file, scope)
        _consume_trial("混剪成片", scope=scope)
        _set_task(
            task_id,
            status="completed",
            finished_at=time.time(),
            output=str(output_path),
            outputs=[str(output_path)],
            result_count=1,
        )
        emit_log("success", f"预览混剪完成：{output_path}", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"预览混剪失败：{exc}", scope)


def _run_smart_preview(task_id: str, preview_id: str, payload: SmartCutPayload) -> None:
    scope = "smart-cut"
    _set_task(task_id, status="running", started_at=time.time(), progress=6, message="准备 AI 选片预览")
    _store_preview(
        preview_id,
        task_id=task_id,
        scope=scope,
        status="running",
        message="正在生成 AI 选片预览。",
        created_at=time.time(),
        category=payload.category,
        feedback_scope=_feedback_scope_key("smart", payload.category),
        target_duration=payload.target_duration,
        clips=[],
    )
    emit_log("info", "AI 选片预览任务已启动。", scope)
    try:
        _ensure_feature_access("智能成片")
        import cutter_logic as cutter_mod
        from cutter_logic import process_video

        paths = _existing_paths(payload.video_paths, "视频")
        video = paths[0]
        _set_task_progress(task_id, 12, "校验素材")
        if len(paths) > 1:
            emit_log("warning", "当前预览先处理第 1 个视频；多视频批量预览后续补齐。", scope)

        cutter_mod._multi_result_cache = {}
        srt_path = payload.srt_path.strip() or None
        out_dir = _default_output_dir(video, payload.output_dir, "output")
        preview_output = str(out_dir / f"{_safe_stem(video.stem)}_preview_placeholder.mp4")
        emit_log("info", f"正在分析视频并生成片预览：{video.name}", scope)
        _set_task_progress(task_id, 18, "分析素材")
        result = process_video(
            str(video),
            srt_path=srt_path,
            output_path=preview_output,
            dedup_preset=payload.dedup_preset,
            subtitle_overlay=payload.subtitle_overlay,
            log_fn=_task_log_fn(task_id, scope, base=18, span=66),
            cancel_event=_task_cancel_event(task_id),
            force_category=None if payload.category in ("", "自动检测") else payload.category,
            _clips_only=True,
            focus_hint=payload.focus_hint,
            smart_crop_enabled=payload.smart_crop_enabled,
            crop_level=payload.crop_level,
            ken_burns_enabled=payload.ken_burns_enabled,
            target_duration=payload.target_duration,
            mirror_enabled=payload.mirror_enabled,
            kb_intensity=payload.ken_burns_intensity,
            ai_controls=payload.ai_controls,
        )
        if not result:
            raise RuntimeError("AI 选片预览失败。")
        _set_task_progress(task_id, 86, "整理候选片段")
        raw_clips = list(cutter_mod._multi_result_cache.get("clips") or [])
        if not raw_clips:
            raise RuntimeError("AI 没有选到可预览片段。")
        resolved_srt = str(cutter_mod._multi_result_cache.get("srt_path") or srt_path or video.with_suffix(".srt"))
        srt_text = str(cutter_mod._multi_result_cache.get("srt_text") or "")
        category_summary = dict(cutter_mod._multi_result_cache.get("category_summary") or {})
        preference_summary = dict(cutter_mod._multi_result_cache.get("preference_summary") or {})
        topic_coverage_summary = dict(cutter_mod._multi_result_cache.get("topic_coverage_summary") or {})
        word_timings = list(cutter_mod._multi_result_cache.get("word_timings") or [])
        preferred_category = payload.category if payload.category not in ("", "自动检测", "自动") else str(category_summary.get("main_category") or "")
        raw_clips, dedup_summary = _normalize_preview_final_clips(
            raw_clips,
            srt_text,
            default_source=str(video),
            preferred_category=preferred_category,
            preferred_focus=str(preference_summary.get("used_label") or preference_summary.get("label") or ""),
            word_timings=word_timings,
            target_duration=payload.target_duration,
        )
        try:
            import ai_clipper as _topic_ai
            topic_coverage_summary = dict(
                _topic_ai._topic_coverage_summary(
                    raw_clips,
                    str(preference_summary.get("used_label") or preference_summary.get("label") or ""),
                    payload.target_duration,
                    preference_summary.get("requested", "自动"),
                )
                or {}
            )
        except Exception:
            pass
        if category_summary:
            dedup_summary["category_summary"] = category_summary
        if preference_summary:
            dedup_summary["preference_summary"] = preference_summary
        if topic_coverage_summary:
            dedup_summary["topic_coverage_summary"] = topic_coverage_summary
        _set_task_progress(task_id, 94, "生成预览列表")
        public_clips = _preview_public_clips(raw_clips, srt_text, word_timings=word_timings)
        raw_clips, public_clips, unusable_removed = _drop_unusable_preview_clips(raw_clips, public_clips)
        if unusable_removed:
            dedup_summary["unusable_preview_clips_removed"] = unusable_removed
        dedup_summary["narrative_owner"] = "ai"
        dedup_summary["preference_supplemented"] = 0
        emit_log("info", "AI叙事模式: 预览保持AI原始顺序，不做主题补片或角色重排。", scope)
        preview_quality = _preview_quality_summary(public_clips)
        if preview_quality["preview_locked_segments"] or preview_quality["preview_auto_unselected_segments"]:
            dedup_summary.update(preview_quality)
            emit_log(
                "info",
                "预览子句清理: "
                f"锁定{preview_quality['preview_locked_segments']}句安全内容，"
                f"自动取消{preview_quality['preview_auto_unselected_segments']}句重复/残句。",
                scope,
            )
        effective_topic_summary = _preview_effective_topic_coverage(
            raw_clips,
            public_clips,
            str(preference_summary.get("used_label") or preference_summary.get("label") or ""),
            payload.target_duration,
            preference_summary.get("requested", "自动"),
        )
        if effective_topic_summary:
            dedup_summary["topic_coverage_summary"] = effective_topic_summary
        dedup_summary.update(_annotate_preview_manual_repeats(public_clips))
        _store_preview(
            preview_id,
            status="ready",
            message=f"AI 选片预览完成，共 {len(public_clips)} 个片段。",
            video=str(video),
            video_name=video.name,
            category=preferred_category,
            feedback_scope=_feedback_scope_key("smart", preferred_category),
            target_duration=payload.target_duration,
            srt_path=resolved_srt,
            srt_text=srt_text,
            raw_clips=raw_clips,
            clips=public_clips,
            dedup_summary=dedup_summary,
        )
        _set_task(task_id, status="completed", finished_at=time.time())
        emit_log("success", f"AI 选片预览完成：{len(public_clips)} 个片段。", scope)
    except Exception as exc:
        _store_preview(preview_id, status="failed", error=str(exc), message="AI 选片预览失败。")
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"AI选片预览失败：{exc}", scope)


def _run_smart_cut_from_preview(task_id: str, payload: SmartPreviewCutPayload) -> None:
    scope = "smart-cut"
    _set_task(task_id, status="running", started_at=time.time(), progress=6, message="读取预览片段")
    emit_log("info", "使用 AI 选片预览开始成片。", scope)
    try:
        _ensure_feature_access("智能成片")
        from cutter_logic import _process_version_with_clips

        preview = _get_preview(payload.preview_id)
        if not preview or preview.get("status") != "ready":
            raise RuntimeError("选片预览不存在或尚未完成。")
        raw_clips = list(preview.get("raw_clips") or [])
        if not raw_clips:
            raise RuntimeError("选片预览里没有可用片段。")
        draft = _preview_selection_draft(preview)
        selected = payload.selected_indices or list(draft.get("selected_indices") or [])
        selected_segments = payload.selected_segments or dict(draft.get("selected_segments") or {})
        selected_indices = [int(i) for i in selected if 0 <= int(i) < len(raw_clips)]
        clips = _clips_from_preview_selection(preview, selected_indices, selected_segments)
        if not clips:
            raise RuntimeError("请至少保留一个片段再成片。")
        _log_preview_selection(scope, "预览成片子句选择", selected_indices, selected_segments, clips)
        _set_task_progress(task_id, 18, f"确认 {len(clips)} 个片段")

        video = Path(str(preview.get("video", "")))
        if not video.exists():
            raise FileNotFoundError(f"视频不存在：{video}")
        srt_path = str(preview.get("srt_path") or video.with_suffix(".srt"))
        emit_log("info", f"预览成片：按预览保留结果使用 {len(clips)} 个片段/子句组，不二次去重。", scope)
        _set_task_progress(task_id, 28, "准备输出")
        output_dir = Path(payload.output_dir.strip().strip('"')) if payload.output_dir.strip() else video.parent / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / f"{_safe_stem(video.stem)}_preview_cut_{_stamp_name()}.mp4")
        pip_path, used_pip_file = _pick_pip_asset(payload, scope)

        result = _process_version_with_clips(
            str(video),
            srt_path,
            output_path,
            clips,
            payload.dedup_preset,
            payload.subtitle_overlay,
            _task_log_fn(task_id, scope, base=30, span=58),
            _task_cancel_event(task_id),
            pip_path,
            payload.pip_size,
            payload.pip_opacity,
            payload.pip_pos,
            smart_crop_enabled=payload.smart_crop_enabled,
            crop_level=payload.crop_level,
            ken_burns_enabled=payload.ken_burns_enabled,
            mirror_enabled=payload.mirror_enabled,
            kb_intensity=payload.ken_burns_intensity,
            target_duration=payload.target_duration,
            dedup_video_options=payload.video,
            dedup_audio_options=payload.audio,
            transition_options=payload.transition,
        )
        if not result:
            raise RuntimeError("预览成片失败。")
        _set_task_progress(task_id, 94, "整理输出")
        _archive_used_pip(used_pip_file, scope)
        _consume_trial("智能成片", scope=scope)
        _set_task(
            task_id,
            status="completed",
            finished_at=time.time(),
            output=output_path,
            outputs=[output_path],
            result_count=1,
        )
        emit_log("success", f"预览成片完成：{output_path}", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"预览成片失败：{exc}", scope)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/api/runtime")
def runtime() -> dict[str, Any]:
    public_key_suffix = ""
    try:
        import license_token

        public_key = license_token.configured_public_key()
        public_key_suffix = public_key[-8:] if public_key else ""
    except Exception:
        public_key_suffix = ""
    return {
        "version": _load_version(),
        "repo_root": str(REPO_ROOT),
        "app_dir": str(APP_DIR),
        "web_dir": str(WEB_DIR),
        "user_data_dir": str(_get_user_data_dir()),
        "default_user_data_dir": str(_default_user_data_dir()),
        "user_data_location_file": str(_user_data_location_file()),
        "user_data_custom": not _path_same(_get_user_data_dir(), _default_user_data_dir()),
        "license_public_key_suffix": public_key_suffix,
        "supports_web_incremental_updates": _safe_web_incremental_supported(),
        "runtime_integrity": _runtime_integrity_summary(),
        "mode": "local-web-client",
    }


def _schedule_client_restart(delay: float = 1.5) -> bool:
    if not getattr(sys, "frozen", False):
        return False
    executable = Path(sys.executable)
    if not executable.exists():
        return False

    def _restart() -> None:
        time.sleep(delay)
        try:
            flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
            subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
            )
        except Exception as exc:
            emit_log("error", f"自动重启失败：{exc}", "settings")
        finally:
            time.sleep(0.2)
            os._exit(0)

    threading.Thread(target=_restart, daemon=True).start()
    return True


def _safe_web_incremental_supported() -> bool:
    return True


def _runtime_integrity_summary() -> dict[str, Any]:
    try:
        manifest = json.loads((APP_DIR / "version.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {"ok": False, "checked": 0, "mismatched": ["app/version.json"]}
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    targets = list(manifest.get("integrity_files") or [])
    mismatched: list[str] = []
    for name in targets:
        if name.startswith("app/"):
            path = APP_DIR / name.removeprefix("app/")
        elif name.startswith("web_client/"):
            path = WEB_DIR / name.removeprefix("web_client/")
        else:
            continue
        try:
            data = path.read_bytes()
            if path.suffix.lower() in {".py", ".json", ".txt", ".html", ".css", ".js"}:
                data = data.replace(b"\r\n", b"\n")
            actual = hashlib.sha256(data).hexdigest().lower()
        except Exception:
            actual = ""
        if actual != str(files.get(name) or "").lower():
            mismatched.append(name)
    return {"ok": not mismatched, "checked": len(targets), "mismatched": mismatched}


@app.get("/api/update/check")
def check_update_api() -> dict[str, Any]:
    try:
        from updater import check_update, init_installed_version

        init_installed_version()
        info = check_update()
        if not info:
            return {"ok": True, "update_available": False, "current_version": _load_version()}
        public_info = {
            "version": info.get("latest_version") or info.get("version") or "",
            "release_notes": info.get("release_notes") or info.get("update_message") or "",
            "force_update": bool(info.get("force_update", False)),
            "file_count": len(info.get("files") or {}),
            "has_package": bool(info.get("update_url") or info.get("download_url")),
            "requires_full_package_note": info.get("requires_full_package_note") or "",
            "supports_web_incremental_updates": _safe_web_incremental_supported(),
            "repair_required": bool(info.get("repair_required", False)),
            "integrity_mismatches": list(info.get("integrity_mismatches") or []),
        }
        return {"ok": True, "update_available": True, "update": public_info}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"检查更新失败：{exc}") from exc


@app.post("/api/update/apply")
def apply_update_api() -> dict[str, Any]:
    with _UPDATE_LOCK:
        if _UPDATE_STATE.get("running"):
            return {"ok": False, "running": True, "msg": "更新正在安装中"}
        _UPDATE_STATE["running"] = True
        _UPDATE_STATE["last_result"] = None

    try:
        from updater import apply_update_headless, check_update, init_installed_version

        init_installed_version()
        info = check_update()
        if not info:
            result = {"ok": True, "updated": [], "restart_required": False, "msg": "当前已经是最新版本"}
        else:
            result = apply_update_headless(info)
            if result.get("ok"):
                if result.get("restart_required"):
                    result["auto_restart"] = _schedule_client_restart()
                    result["msg"] = "更新已安装，客户端即将自动重启。" if result.get("auto_restart") else "更新已安装，请重启客户端后生效。"
                emit_log("success", result.get("msg") or "更新已安装，重启客户端后生效。", "settings")
            else:
                details = result.get("failed_details") or {}
                if details:
                    first_items = list(details.items())[:3]
                    detail_text = "; ".join(f"{name}: {reason}" for name, reason in first_items)
                    emit_log("error", f"更新安装失败：{detail_text}", "settings")
                else:
                    emit_log("error", f"更新安装失败：{result.get('failed') or result.get('msg')}", "settings")
        with _UPDATE_LOCK:
            _UPDATE_STATE["last_result"] = result
        return result
    except Exception as exc:
        with _UPDATE_LOCK:
            _UPDATE_STATE["last_result"] = {"ok": False, "msg": str(exc)}
        raise HTTPException(status_code=500, detail=f"安装更新失败：{exc}") from exc
    finally:
        with _UPDATE_LOCK:
            _UPDATE_STATE["running"] = False


@app.post("/api/dialog/videos")
async def dialog_videos() -> dict[str, Any]:
    try:
        paths = await asyncio.to_thread(_choose_files_dialog, "选择视频文件")
        return {"ok": True, "paths": paths}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打开视频选择窗口失败：{exc}") from exc


@app.post("/api/dialog/file")
async def dialog_file(payload: FileDialogPayload) -> dict[str, Any]:
    titles = {
        "video": "选择视频文件",
        "excel": "选择 Excel 文件",
        "image": "选择图片文件",
        "srt": "选择字幕文件",
    }
    title = payload.title or titles.get(payload.kind, "选择文件")
    try:
        path = await asyncio.to_thread(_choose_file_dialog, title, payload.kind)
        return {"ok": True, "path": path}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打开文件选择窗口失败：{exc}") from exc


@app.post("/api/dialog/directory")
async def dialog_directory() -> dict[str, Any]:
    try:
        path = await asyncio.to_thread(_choose_directory_dialog, "选择文件夹")
        return {"ok": True, "path": path}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打开目录选择窗口失败：{exc}") from exc


@app.post("/api/user-data-dir")
def set_user_data_dir(payload: UserDataDirPayload) -> dict[str, Any]:
    active = _cache_clear_active_tasks()
    if active:
        names = "、".join(active[:3])
        suffix = "..." if len(active) > 3 else ""
        raise HTTPException(status_code=409, detail=f"当前有任务运行中（{names}{suffix}），请完成或停止任务后再迁移用户数据目录。")

    current = _get_user_data_dir()
    target = _validate_user_data_target(payload.path)
    migrated = {"files": 0, "bytes": 0, "items": []}
    if payload.migrate:
        migrated = _copy_user_data_persistent(current, target)

    try:
        import config
        config.set_user_data_dir(str(target))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存用户数据目录失败：{exc}") from exc

    try:
        import ai_clipper
        cache = getattr(ai_clipper, "_keywords_cache", None)
        if isinstance(cache, dict):
            cache["_data"] = None
            cache["_mtime"] = 0
    except Exception:
        pass

    message = f"用户数据目录已切换到：{target}"
    if migrated.get("files"):
        message += f"；已迁移 {migrated['files']} 个文件"
    emit_log("success", message, "settings")
    return {
        "ok": True,
        "message": message,
        "user_data_dir": str(_get_user_data_dir()),
        "default_user_data_dir": str(_default_user_data_dir()),
        "user_data_location_file": str(_user_data_location_file()),
        "user_data_custom": not _path_same(_get_user_data_dir(), _default_user_data_dir()),
        "migrated": migrated,
    }


@app.post("/api/uploads/files")
async def upload_files(files: list[UploadFile] = File(default_factory=list)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件。")

    saved: list[str] = []
    target_dir = _upload_dir()
    for item in files:
        original = Path(item.filename or "file")
        name = _safe_stem(original.stem)
        suffix = original.suffix or ".dat"
        target = target_dir / f"{name}_{int(time.time())}_{os.urandom(3).hex()}{suffix}"
        with target.open("wb") as out:
            while True:
                chunk = await item.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        saved.append(str(target))
        emit_log("success", f"拖拽文件已缓存：{target.name}", "system")
    return {"ok": True, "paths": saved}


@app.post("/api/uploads/videos")
async def upload_videos(files: list[UploadFile] = File(default_factory=list)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="没有收到视频文件。")

    saved: list[str] = []
    target_dir = _upload_dir()
    for item in files:
        name = _safe_stem(item.filename or "video")
        suffix = Path(item.filename or "").suffix or ".mp4"
        target = target_dir / f"{Path(name).stem}_{int(time.time())}_{os.urandom(3).hex()}{suffix}"
        with target.open("wb") as out:
            while True:
                chunk = await item.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        saved.append(str(target))
        emit_log("success", f"拖拽文件已缓存：{target.name}", "system")
    return {"ok": True, "paths": saved}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return _load_settings()


@app.post("/api/settings")
def save_settings(payload: SettingsPayload) -> dict[str, Any]:
    data = payload.model_dump()
    provided_fields = set(getattr(payload, "model_fields_set", set()) or set())
    if "style_profile_enabled" not in provided_fields:
        data.pop("style_profile_enabled", None)
    if _save_settings(data):
        emit_log("success", "设置已保存。", "settings")
        return {"ok": True, "message": "设置已保存"}
    raise HTTPException(status_code=500, detail="璁剧疆淇濆瓨澶辫触")


@app.get("/api/preferences")
def get_preferences() -> dict[str, Any]:
    return {"ok": True, "preferences": _load_preferences()}


@app.post("/api/preferences")
def save_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid preferences")
    _save_preferences(payload)
    return {"ok": True}


@app.get("/api/ai-feedback/stats")
def get_ai_feedback_stats() -> dict[str, Any]:
    return {"ok": True, **_preview_feedback_stats()}


@app.get("/api/ai-feedback/samples")
def get_ai_feedback_samples() -> dict[str, Any]:
    return {
        "ok": True,
        **_preview_feedback_stats(),
        "roles": [
            {"role": role, "label": label}
            for role, label in _PREVIEW_FEEDBACK_ROLE_LABELS.items()
        ],
        "samples": _preview_feedback_samples(),
        "preference_summary": _preview_feedback_preference_summary(),
    }


@app.get("/api/ai-feedback/export")
def export_ai_feedback() -> FileResponse:
    path = _preview_feedback_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    filename = f"LiveClipper_ai_preferences_{_stamp_name()}.jsonl"
    return FileResponse(str(path), media_type="application/jsonl", filename=filename)


@app.post("/api/ai-feedback/import")
def import_ai_feedback(payload: AiFeedbackImportPayload) -> dict[str, Any]:
    source = Path(str(payload.path or "").strip().strip('"'))
    if not str(source):
        raise HTTPException(status_code=400, detail="请先选择要导入的剪辑风格画像数据文件。")
    result = _preview_feedback_merge_import(source)
    emit_log(
        "success",
        f"剪辑风格画像数据导入完成：新增 {result['added_count']} 条，跳过重复 {result['skipped_count']} 条。",
        "settings",
    )
    return {"ok": True, **result, **_preview_feedback_stats()}


@app.post("/api/ai-feedback/sample/delete")
def delete_ai_feedback_sample(payload: AiFeedbackDeletePayload) -> dict[str, Any]:
    result = _preview_feedback_delete_sample(payload.role, payload.text)
    emit_log(
        "success",
        f"剪辑风格画像已删除样本：{result['removed_count']} 条匹配记录。",
        "settings",
    )
    return {"ok": True, **result, **_preview_feedback_stats(), "samples": _preview_feedback_samples()}


@app.post("/api/ai-feedback/clear")
def clear_ai_feedback() -> dict[str, Any]:
    result = _preview_feedback_clear()
    emit_log("warning", "剪辑风格画像学习数据已清空。", "settings")
    return {"ok": True, **result, **_preview_feedback_stats(), "samples": []}


@app.get("/api/license")
def get_license() -> dict[str, Any]:
    try:
        from license_client import _load_license_code, check_activation_cached

        status = check_activation_cached()
        return {
            "ok": True,
            "activated": bool(status.get("activated")),
            "need_activate": bool(status.get("need_activate")),
            "reason": status.get("reason") or status.get("msg") or "",
            "days_left": status.get("days_left"),
            "expires_date": status.get("expires_date", ""),
            "code": _load_license_code() or "",
        }
    except Exception as exc:
        return {"ok": False, "activated": False, "reason": str(exc), "code": ""}


@app.post("/api/license/activate")
def activate_license(payload: LicensePayload) -> dict[str, Any]:
    code = payload.code.strip()
    if not code:
        return {"ok": False, "message": "请先输入激活码。"}
    try:
        from license_client import activate_with_code, check_activation_cached

        result = activate_with_code(code)
        ok = bool(result.get("ok"))
        message = result.get("msg", "激活完成")
        restart_required = False
        if ok:
            status = check_activation_cached(allow_stale=False, refresh_async=False)
            restart_required = not bool(status.get("activated"))
            if restart_required:
                message = f"{message}，请重启客户端后再使用。"
        emit_log("success" if ok else "warning", message, "settings")
        return {"ok": ok, "message": message, "restart_required": restart_required}
    except Exception as exc:
        emit_log("error", f"激活失败：{exc}", "settings")
        return {"ok": False, "message": f"激活失败：{exc}"}


@app.post("/api/license/unbind")
def unbind_license() -> dict[str, Any]:
    try:
        from license_client import deactivate_device

        result = deactivate_device()
        ok = bool(result.get("ok"))
        emit_log("success" if ok else "warning", result.get("msg", "解绑完成"), "settings")
        return {"ok": ok, "message": result.get("msg", "解绑完成")}
    except Exception as exc:
        emit_log("error", f"瑙ｇ粦澶辫触: {exc}", "settings")
        return {"ok": False, "message": f"瑙ｇ粦澶辫触: {exc}"}


def _ai_provider_warning(base_url: str, model: str) -> str:
    lower_url = normalize_ai_base_url(base_url).lower()
    lower_model = (model or "").lower()
    if "deepseek" in lower_url and lower_model and "deepseek" not in lower_model:
        return "当前 Base URL 是 DeepSeek，但模型名不像 DeepSeek；建议模型填写 deepseek-v4-flash。"
    if ("volces" in lower_url or "ark.cn-" in lower_url) and "deepseek" in lower_model:
        return "当前 Base URL 是火山/豆包，但模型名是 DeepSeek；请把 Base URL 改为 https://api.deepseek.com。"
    return ""


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="ignore")[:300]
    except Exception:
        return ""


def _ai_test_error_message(code: int, base_url: str, model: str, body: str = "") -> str:
    lower_url = normalize_ai_base_url(base_url).lower()
    lower_model = (model or "").lower()
    is_deepseek = "deepseek" in lower_url or "deepseek" in lower_model
    if code in (401, 403):
        if is_deepseek:
            return (
                "AI 连接失败：HTTP 401。DeepSeek API Key 无效、已失效，"
                "或仍在使用豆包/火山的 Key。请确认 Base URL=https://api.deepseek.com，"
                "模型=deepseek-v4-flash，并重新填写 DeepSeek 控制台里的 API Key。"
            )
        return f"AI 连接失败：HTTP {code}。API Key 无效或没有权限，请重新填写对应平台的 Key。"
    if code == 404:
        return "AI 连接失败：HTTP 404。Base URL 不正确，请检查是否填写为 https://api.deepseek.com。"
    if code == 429:
        return "AI 连接失败：HTTP 429。调用过于频繁或额度受限，请稍后再试。"
    if code in (500, 502, 503, 504):
        return f"AI 连接失败：HTTP {code}。服务商暂时不可用，请稍后再试。"
    detail = f"；服务返回：{body}" if body else ""
    return f"AI 连接失败：HTTP {code}{detail}"


@app.post("/api/settings/test-ai")
def test_ai(payload: SettingsPayload | None = None) -> dict[str, Any]:
    cfg = _load_settings()
    if payload:
        cfg.update(payload.model_dump(exclude_unset=True))
    cfg = _normalize_ai_model_defaults(cfg)
    api_key = (cfg.get("api_key") or "").strip()
    base_url = normalize_ai_base_url(cfg.get("base_url"))
    model = (cfg.get("model") or "").strip()
    if not api_key or not base_url:
        return {"ok": False, "message": "请先填写 AI API Key 和 Base URL。"}

    url = ai_models_url(base_url)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
            if 200 <= resp.status < 300:
                warning = _ai_provider_warning(base_url, model)
                if warning:
                    return {"ok": False, "message": f"AI Key 验证通过，但配置不一致：{warning}"}
                emit_log("success", "AI 连接测试通过。", "settings")
                return {"ok": True, "message": "AI 连接测试通过。"}
            return {"ok": False, "message": f"AI 连接异常: HTTP {resp.status}"}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "message": _ai_test_error_message(exc.code, base_url, model, _read_http_error_body(exc))}
    except Exception as exc:
        return {"ok": False, "message": f"AI 连接失败：{exc}"}


@app.post("/api/settings/diagnose-volcengine")
async def diagnose_volcengine_endpoint(payload: SettingsPayload | None = None) -> dict[str, Any]:
    cfg = _load_settings()
    if payload:
        cfg.update(payload.model_dump(exclude_unset=True))
    cfg["volc_region"] = _normalize_volc_region(cfg.get("volc_region"))

    emit_log("info", "开始火山完整诊断。", "settings")

    def _run() -> dict[str, Any]:
        from volcengine_asr import diagnose_volcengine

        return diagnose_volcengine(
            app_id=cfg.get("volc_app_id", ""),
            access_token=cfg.get("volc_access_token", ""),
            tos_ak=cfg.get("volc_tos_ak", ""),
            tos_sk=cfg.get("volc_tos_sk", ""),
            bucket=cfg.get("volc_bucket", ""),
            region=cfg.get("volc_region", "cn-beijing"),
            api_key=cfg.get("volc_api_key", "") or None,
            log_fn=lambda msg: emit_log("info", msg, "settings"),
            timeout=35,
        )

    result = await asyncio.to_thread(_run)
    emit_log("success" if result.get("ok") else "error", result.get("message", "诊断完成。"), "settings")
    return result


@app.get("/api/keywords")
def get_keywords() -> dict[str, Any]:
    source = _keyword_config_path()
    data = _load_effective_keyword_config()
    return {"ok": True, "count": _keyword_count(data), "keywords": data, "source": str(source)}


@app.post("/api/keywords")
def save_keywords(payload: dict[str, Any]) -> dict[str, Any]:
    target = _safe_user_child("keywords.json")
    saved = _normalize_keyword_payload(payload)
    _write_json_file(target, saved)
    _clear_ai_keyword_cache()
    data = _load_effective_keyword_config()
    emit_log("success", "关键词配置已保存。", "settings")
    return {
        "ok": True,
        "message": "词库已保存，会影响下一次 AI 选片",
        "path": str(target),
        "source": str(target),
        "count": _keyword_count(data),
        "keywords": data,
    }


@app.post("/api/keywords/reset")
def reset_keywords() -> dict[str, Any]:
    target = _safe_user_child("keywords.json")
    if target.exists():
        target.unlink()
    _clear_ai_keyword_cache()
    data = _load_effective_keyword_config()
    source = _keyword_config_path()
    emit_log("success", "规则词库已恢复默认。", "settings")
    return {
        "ok": True,
        "message": "词库已恢复默认",
        "path": str(source),
        "source": str(source),
        "count": _keyword_count(data),
        "keywords": data,
    }


@app.post("/api/cache/clear")
def clear_cache() -> dict[str, Any]:
    global _PREVIEW_CLEARED_AT
    active = _cache_clear_active_tasks()
    if active:
        names = "、".join(active[:3])
        suffix = "..." if len(active) > 3 else ""
        raise HTTPException(status_code=409, detail=f"当前有任务运行中（{names}{suffix}），请完成或停止任务后再清理缓存。")

    removed_items = []
    for target in _cache_clear_targets():
        result = _remove_cache_target(target)
        if result:
            removed_items.append(result)

    with _CLIP_PREVIEW_LOCK:
        _PREVIEW_CLEARED_AT = time.time()
        _CLIP_PREVIEWS.clear()
    try:
        _VIDEO_INFO_CACHE.clear()
        _VIDEO_FP_CACHE.clear()
    except Exception:
        pass
    try:
        import cutter_logic as cutter_mod
        cutter_mod._multi_result_cache = {}
    except Exception:
        pass

    freed_bytes = sum(int(item.get("bytes") or 0) for item in removed_items if item.get("removed"))
    failed = [item for item in removed_items if not item.get("removed")]
    freed_mb = round(freed_bytes / 1024 / 1024, 1)
    if failed:
        message = f"缓存清理完成，释放约 {freed_mb} MB；{len(failed)} 个项目可能被占用未删除"
        emit_log("warning", message, "settings")
    else:
        message = f"缓存清理完成，释放约 {freed_mb} MB"
        emit_log("success", message, "settings")
    return {
        "ok": True,
        "message": message,
        "removed": [item["path"] for item in removed_items if item.get("removed")],
        "removed_items": removed_items,
        "freed_bytes": freed_bytes,
        "freed_mb": freed_mb,
        "failed": failed,
    }


@app.post("/api/path/open")
def open_path(payload: PathPayload) -> dict[str, Any]:
    raw = (payload.path or "").strip().strip('"')
    if not raw:
        raise HTTPException(status_code=400, detail="请先填写目录。")
    target = Path(raw)
    try:
        if target.exists() and target.is_file():
            target = target.parent
        elif not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))
        return {"ok": True, "message": "已打开目录", "path": str(target)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"打开目录失败：{exc}") from exc


@app.post("/api/videos/inspect")
def inspect_videos(payload: PathsPayload) -> dict[str, Any]:
    paths = [str(item or "").strip() for item in payload.paths if str(item or "").strip()]
    infos = [_probe_video_info(path) for path in paths[:200]]
    seen: dict[str, int] = {}
    for info in infos:
        norm = str(info.get("path") or "").strip().strip('"').lower().replace("/", "\\")
        if norm:
            seen[norm] = seen.get(norm, 0) + 1
    for info in infos:
        norm = str(info.get("path") or "").strip().strip('"').lower().replace("/", "\\")
        info["duplicate"] = bool(norm and seen.get(norm, 0) > 1)
    return {"ok": True, "items": infos}


@app.post("/api/pip/inspect")
def inspect_pip_pool(payload: PathPayload) -> dict[str, Any]:
    return {"ok": True, **_inspect_pip_pool(payload.path)}


@app.get("/api/tasks")
def list_tasks() -> dict[str, Any]:
    with _TASK_LOCK:
        return {"tasks": list(_TASKS.values())[-50:]}


@app.get("/api/output-history")
def output_history(scope: str = "", limit: int = 50) -> dict[str, Any]:
    safe_limit = max(1, min(120, int(limit or 50)))
    scope_text = str(scope or "").strip()
    with _TASK_LOCK:
        task_records: list[dict[str, Any]] = []
        for task in list(_TASKS.values()):
            task_records.extend(_output_history_records_from_task(task))
    with _OUTPUT_HISTORY_LOCK:
        persisted = _read_output_history_items()
    scanned = _scan_output_history_from_preferences(limit=safe_limit)
    items = _merge_output_history_items(task_records, persisted, scanned)
    if scope_text:
        items = [item for item in items if str(item.get("scope") or "") == scope_text or not item.get("scope")]
    return {"ok": True, "items": items[:safe_limit]}


@app.post("/api/tasks/stop-scope")
def stop_scope(payload: StopScopePayload) -> dict[str, Any]:
    scope = (payload.scope or "").strip()
    stopped = _cancel_scope(scope)
    return {"ok": True, "message": f"已停止（{stopped} 个任务）", "stopped": stopped}


@app.post("/api/tasks/stop")
def stop_task(payload: StopTaskPayload) -> dict[str, Any]:
    stopped = _cancel_task(payload.task_id)
    return {"ok": True, "message": "已停止当前任务" if stopped else "当前没有正在运行的任务", "stopped": stopped}


@app.get("/api/scan-results")
def scan_results() -> dict[str, Any]:
    with _SCAN_LOCK:
        flat = list(_SCAN_RESULTS.get("flat") or [])
        merged = list(_SCAN_RESULTS.get("merged") or [])
        groups = list(_SCAN_RESULTS.get("schedule_groups") or [])

    def _product(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": item.get("name", ""),
            "start": item.get("start", 0),
            "end": item.get("end", 0),
            "video": os.path.basename(item.get("_video", "")),
            "segments": len(item.get("segments") or []),
        }

    def _merged(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": item.get("name", ""),
            "total_duration": item.get("total_duration", 0),
            "source_count": item.get("source_count", 0),
        }

    def _group(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": item.get("name", ""),
            "segments": len(item.get("segments") or []),
            "total_duration": item.get("total_duration", 0),
        }

    return {
        "ok": True,
        "products": [_product(item) for item in flat[:200]],
        "merged": [_merged(item) for item in merged[:200]],
        "schedule_groups": [_group(item) for item in groups[:200]],
    }


@app.post("/api/preflight")
def preflight(payload: PreflightPayload) -> dict[str, Any]:
    return _preflight_checks(payload.feature, payload.payload or {})


@app.post("/api/smart-cut/start")
def start_smart_cut(payload: SmartCutPayload) -> dict[str, Any]:
    _ensure_scope_idle("smart-cut", "智能成片")
    _raise_preflight_errors("smart-cut", payload)
    video_paths = [path.strip() for path in payload.video_paths if path.strip()]
    if not video_paths:
        raise HTTPException(status_code=400, detail="请至少填写一个视频文件路径。")
    payload.video_paths = video_paths
    task_id = _new_task("smart-cut", "智能成片")
    thread = threading.Thread(target=_run_smart_cut, args=(task_id, payload), daemon=True)
    thread.start()
    return {"ok": True, "task_id": task_id, "message": "任务已启动。"}


@app.post("/api/smart-cut/preview/start")
def start_smart_preview(payload: SmartCutPayload) -> dict[str, Any]:
    _ensure_scope_idle("smart-cut", "AI选片预览")
    _raise_preflight_errors("smart-preview", payload)
    video_paths = [path.strip() for path in payload.video_paths if path.strip()]
    if not video_paths:
        raise HTTPException(status_code=400, detail="请至少填写一个视频文件路径。")
    payload.video_paths = video_paths
    task_id = _new_task("smart-cut", "AI选片预览")
    preview_id = uuid.uuid4().hex
    threading.Thread(target=_run_smart_preview, args=(task_id, preview_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "preview_id": preview_id, "message": "AI 选片预览已启动。"}


@app.post("/api/preview/selection/save")
def save_preview_selection(payload: PreviewSelectionPayload) -> dict[str, Any]:
    preview_id = payload.preview_id.strip()
    if not preview_id:
        raise HTTPException(status_code=400, detail="请先生成 AI 选片预览。")
    preview = _get_preview(preview_id)
    if not preview:
        raise HTTPException(status_code=404, detail="没有找到这次 AI 选片预览，请重新生成。")
    if preview.get("status") != "ready":
        raise HTTPException(status_code=400, detail="AI 选片预览还没有完成，请稍后再试。")
    scope = "mix" if payload.scope == "mix" or preview.get("scope") == "mix" else "smart"
    draft = _normalize_preview_selection_draft(
        preview,
        scope,
        payload.selected_indices,
        payload.selected_segments,
        order=payload.order,
        updated_at=payload.updated_at,
    )
    _store_preview(preview_id, selection_draft=draft)
    return {"ok": True, "selection_draft": draft}


@app.get("/api/smart-cut/preview/latest")
def get_latest_smart_preview() -> dict[str, Any]:
    return _preview_public(_latest_preview("smart-cut"))


@app.get("/api/smart-cut/preview/{preview_id}")
def get_smart_preview(preview_id: str) -> dict[str, Any]:
    return _preview_public(_get_preview(preview_id))


@app.post("/api/smart-cut/from-preview/start")
def start_smart_from_preview(payload: SmartPreviewCutPayload) -> dict[str, Any]:
    _ensure_scope_idle("smart-cut", "预览成片")
    if not payload.preview_id.strip():
        raise HTTPException(status_code=400, detail="请先生成 AI 选片预览。")
    preview = _get_preview(payload.preview_id)
    if not preview:
        raise HTTPException(status_code=404, detail="没有找到这次 AI 选片预览，请重新生成。")
    if preview.get("status") != "ready":
        raise HTTPException(status_code=400, detail="AI 选片预览还没有完成，请稍后再试。")
    if not preview.get("raw_clips"):
        raise HTTPException(status_code=400, detail="这次预览没有可用片段，请重新生成。")
    draft = _preview_selection_draft(preview)
    if not payload.selected_indices and isinstance(draft.get("selected_indices"), list):
        payload.selected_indices = list(draft.get("selected_indices") or [])
    if not payload.selected_segments and isinstance(draft.get("selected_segments"), dict):
        payload.selected_segments = dict(draft.get("selected_segments") or {})
    if not payload.order and isinstance(draft.get("order"), list):
        payload.order = list(draft.get("order") or [])
    _raise_preflight_errors("smart-from-preview", payload)
    if not payload.selected_indices:
        raise HTTPException(status_code=400, detail="请至少保留一个片段再成片。")
    clip_count = len(preview.get("raw_clips") or [])
    selected = [int(index) for index in payload.selected_indices if 0 <= int(index) < clip_count]
    if not selected:
        raise HTTPException(status_code=400, detail="请至少保留一个有效片段再成片。")
    payload.selected_indices = selected
    draft = _apply_preview_payload_draft(payload.preview_id.strip(), preview, "smart", selected, payload.selected_segments, order=payload.order)
    _record_preview_selection_feedback(preview, "smart", draft, "smart_from_preview")
    task_id = _new_task("smart-cut", "预览成片")
    threading.Thread(target=_run_smart_cut_from_preview, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "预览成片任务已启动。"}


@app.post("/api/smart-cut/preview/clip-video")
def smart_preview_clip_video(payload: SmartPreviewClipPayload) -> dict[str, Any]:
    if not payload.preview_id.strip():
        raise HTTPException(status_code=400, detail="请先生成 AI 选片预览。")
    path = _preview_clip_video(
        payload.preview_id.strip(),
        payload.clip_index,
        selected_indices=payload.selected_indices,
        selected_segments=payload.selected_segments,
        order=payload.order,
        scope="smart",
        updated_at=payload.updated_at,
    )
    return {
        "ok": True,
        "url": f"/api/smart-cut/preview/clip-video/{payload.preview_id.strip()}/{payload.clip_index}?t={int(path.stat().st_mtime)}",
    }


@app.get("/api/smart-cut/preview/clip-video/{preview_id}/{clip_index}")
def get_smart_preview_clip_video(preview_id: str, clip_index: int) -> FileResponse:
    path = _preview_clip_video(preview_id, clip_index)
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


@app.post("/api/mix/start")
def start_mix(payload: MixPayload) -> dict[str, Any]:
    _ensure_scope_idle("mix", "混剪成片")
    _raise_preflight_errors("mix", payload)
    if not [path.strip() for path in payload.video_paths if path.strip()]:
        raise HTTPException(status_code=400, detail="请至少填写一个视频文件路径。")
    task_id = _new_task("mix", "混剪成片")
    threading.Thread(target=_run_mix, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "混剪任务已启动。"}


@app.post("/api/mix/batch/start")
def start_mix_batch(payload: MixBatchPayload) -> dict[str, Any]:
    _ensure_scope_idle("mix", "批量混剪")
    _raise_preflight_errors("mix-batch", payload)
    if not payload.groups:
        raise HTTPException(status_code=400, detail="请至少保存 1 个混剪素材组。")
    task_id = _new_task("mix", "批量混剪")
    threading.Thread(target=_run_mix_batch, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": f"批量混剪任务已启动，共 {len(payload.groups)} 组。"}


@app.post("/api/mix/preview/start")
def start_mix_preview(payload: MixPayload) -> dict[str, Any]:
    _ensure_scope_idle("mix", "混剪AI选片预览")
    _raise_preflight_errors("mix-preview", payload)
    if not [path.strip() for path in payload.video_paths if path.strip()]:
        raise HTTPException(status_code=400, detail="请至少填写一个视频文件路径。")
    task_id = _new_task("mix", "混剪AI选片预览")
    preview_id = uuid.uuid4().hex
    threading.Thread(target=_run_mix_preview, args=(task_id, preview_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "preview_id": preview_id, "message": "混剪 AI 选片预览已启动。"}


@app.get("/api/mix/preview/latest")
def get_latest_mix_preview() -> dict[str, Any]:
    return _preview_public(_latest_preview("mix"))


@app.get("/api/mix/preview/{preview_id}")
def get_mix_preview(preview_id: str) -> dict[str, Any]:
    return _preview_public(_get_preview(preview_id))


@app.post("/api/mix/from-preview/start")
def start_mix_from_preview(payload: MixPreviewCutPayload) -> dict[str, Any]:
    _ensure_scope_idle("mix", "预览混剪成片")
    if not payload.preview_id.strip():
        raise HTTPException(status_code=400, detail="请先生成混剪 AI 选片预览。")
    preview = _get_preview(payload.preview_id)
    if not preview:
        raise HTTPException(status_code=404, detail="混剪 AI 选片预览不存在，请重新生成。")
    if preview.get("status") != "ready":
        raise HTTPException(status_code=400, detail="混剪 AI 选片预览尚未完成。")
    if not preview.get("raw_clips"):
        raise HTTPException(status_code=400, detail="混剪 AI 选片预览里没有可用片段。")
    draft = _preview_selection_draft(preview)
    if not payload.selected_indices and isinstance(draft.get("selected_indices"), list):
        payload.selected_indices = list(draft.get("selected_indices") or [])
    if not payload.selected_segments and isinstance(draft.get("selected_segments"), dict):
        payload.selected_segments = dict(draft.get("selected_segments") or {})
    if not payload.order and isinstance(draft.get("order"), list):
        payload.order = list(draft.get("order") or [])
    _raise_preflight_errors("mix-from-preview", payload)
    if not payload.selected_indices:
        raise HTTPException(status_code=400, detail="请至少保留一个片段再混剪。")
    clip_count = len(preview.get("raw_clips") or [])
    selected = [int(index) for index in payload.selected_indices if 0 <= int(index) < clip_count]
    if not selected:
        raise HTTPException(status_code=400, detail="请至少保留一个有效片段再混剪。")
    payload.selected_indices = selected
    draft = _apply_preview_payload_draft(payload.preview_id.strip(), preview, "mix", selected, payload.selected_segments, order=payload.order)
    _record_preview_selection_feedback(preview, "mix", draft, "mix_from_preview")
    task_id = _new_task("mix", "预览混剪成片")
    threading.Thread(target=_run_mix_from_preview, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "预览混剪任务已启动。"}


@app.post("/api/mix/preview/clip-video")
def mix_preview_clip_video(payload: SmartPreviewClipPayload) -> dict[str, Any]:
    if not payload.preview_id.strip():
        raise HTTPException(status_code=400, detail="请先生成混剪 AI 选片预览。")
    path = _preview_clip_video(
        payload.preview_id.strip(),
        payload.clip_index,
        selected_indices=payload.selected_indices,
        selected_segments=payload.selected_segments,
        order=payload.order,
        scope="mix",
        updated_at=payload.updated_at,
    )
    return {
        "ok": True,
        "url": f"/api/mix/preview/clip-video/{payload.preview_id.strip()}/{payload.clip_index}?t={int(path.stat().st_mtime)}",
    }


@app.get("/api/mix/preview/clip-video/{preview_id}/{clip_index}")
def get_mix_preview_clip_video(preview_id: str, clip_index: int) -> FileResponse:
    path = _preview_clip_video(preview_id, clip_index, scope="mix")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


@app.post("/api/ai-scan/start")
def start_ai_scan(payload: AiScanPayload) -> dict[str, Any]:
    _raise_preflight_errors("ai-scan", payload)
    if not [path.strip() for path in payload.video_paths if path.strip()]:
        raise HTTPException(status_code=400, detail="请至少填写一个视频文件路径。")
    task_id = _new_task("ai-scan", "AI扫描")
    threading.Thread(target=_run_ai_scan, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "AI 扫描任务已启动。"}


@app.post("/api/ai-scan-merge/start")
def start_ai_scan_merge(payload: AiScanPayload | None = None) -> dict[str, Any]:
    task_id = _new_task("ai-scan", "AI 扫描跨文件合并")
    threading.Thread(target=_run_ai_scan_merge, args=(task_id,), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "跨文件合并已启动"}


@app.post("/api/ai-scan-export/start")
def start_ai_scan_export(payload: AiScanPayload) -> dict[str, Any]:
    _raise_preflight_errors("ai-scan-export", payload)
    task_id = _new_task("ai-scan", "AI扫描导出")
    threading.Thread(target=_run_ai_scan_export, args=(task_id, payload, False), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "导出任务已启动。"}


@app.post("/api/ai-scan-export-merge/start")
def start_ai_scan_export_merge(payload: AiScanPayload) -> dict[str, Any]:
    _raise_preflight_errors("ai-scan-export-merge", payload)
    task_id = _new_task("ai-scan", "AI扫描导出合并结果")
    threading.Thread(target=_run_ai_scan_export, args=(task_id, payload, True), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "合并结果导出任务已启动。"}


@app.post("/api/product-scan-read/start")
def start_product_scan_read(payload: ProductScanPayload) -> dict[str, Any]:
    _raise_preflight_errors("product-scan-read", payload)
    task_id = _new_task("product-scan", "读取单品时间表")
    threading.Thread(target=_run_product_scan_read, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "时间表读取任务已启动"}


@app.post("/api/product-scan/start")
def start_product_scan(payload: ProductScanPayload) -> dict[str, Any]:
    _raise_preflight_errors("product-scan", payload)
    task_id = _new_task("product-scan", "单品分割")
    _set_task(task_id, message="分割中")
    threading.Thread(target=_run_product_scan, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "分割中"}


@app.post("/api/video-split/preview")
def preview_video_split(payload: VideoSplitPayload) -> dict[str, Any]:
    _raise_preflight_errors("video-split", payload)
    try:
        return _video_split_preview(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/video-split/start")
def start_video_split(payload: VideoSplitPayload) -> dict[str, Any]:
    _ensure_scope_idle("video-split", "视频分割")
    _raise_preflight_errors("video-split", payload)
    task_id = _new_task("video-split", "视频分割")
    threading.Thread(target=_run_video_split, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "视频分割任务已启动。"}


@app.post("/api/dedup/start")
def start_dedup(payload: DedupPayload) -> dict[str, Any]:
    _raise_preflight_errors("dedup", payload)
    task_id = _new_task("dedup", "创作辅助")
    threading.Thread(target=_run_dedup, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "创作辅助任务已启动。"}


@app.post("/api/dedup/check")
def check_dedup(payload: DedupPayload) -> dict[str, Any]:
    _raise_preflight_errors("dedup-check", payload)
    paths = _dedup_paths_from_payload(payload)
    items = [_dedup_analysis_item(path) for path in paths]
    groups = _analyze_duplicate_groups(items)
    exact = sum(1 for group in groups if group.get("type") == "exact")
    high = sum(1 for group in groups if group.get("type") == "high")
    near = sum(1 for group in groups if group.get("type") == "near")
    partial = sum(1 for group in groups if group.get("type") == "partial")
    if groups:
        parts = []
        if exact:
            parts.append(f"{exact} 组完全重复")
        if high:
            parts.append(f"{high} 组高度重复")
        if near:
            parts.append(f"{near} 组疑似重复")
        if partial:
            parts.append(f"{partial} 组部分重复")
        summary = "本地重复风险检测完成：发现 " + ", ".join(parts) + "。"
    else:
        summary = "本地重复风险检测完成：未发现明显重复。"
    public_items = [{k: v for k, v in item.items() if k not in {"visual", "audio"}} for item in items]
    return {"ok": True, "summary": summary, "items": public_items, "groups": groups}


@app.post("/api/live-rec-detect/start")
def start_live_detect(payload: LiveRecPayload) -> dict[str, Any]:
    _raise_preflight_errors("live-rec-detect", payload)
    task_id = _new_task("live-rec", "检测直播流")
    _set_task(task_id, live_room_name=payload.room_name, live_room_url=_extract_url_from_text(payload.room_url), live_platform=payload.platform)
    threading.Thread(target=_run_live_detect, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "直播流检测已启动"}


@app.post("/api/live-rec-monitor/start")
def start_live_monitor(payload: LiveRecPayload) -> dict[str, Any]:
    _raise_preflight_errors("live-rec-monitor", payload)
    scope = "live-rec"
    resolved_url = _resolve_douyin_short_page_url(_extract_url_from_text(payload.room_url), scope)
    payload.room_url = resolved_url
    existing = _find_running_live_record_task(resolved_url)
    if existing:
        room_name = payload.room_name or existing.get("live_room_name") or "直播间"
        emit_log("warning", f"{room_name}: 已有录制任务正在运行，已复用当前任务，未重复打开浏览器。", scope)
        return {
            "ok": True,
            "task_id": existing.get("id") or "",
            "room_name": existing.get("live_room_name") or payload.room_name,
            "room_url": existing.get("live_room_url") or resolved_url,
            "reused": True,
            "message": "这个直播间已经在录制中，已复用现有任务。",
        }
    task_id = _new_task("live-rec", "直播录制")
    _set_task(task_id, live_room_name=payload.room_name, live_room_url=resolved_url, live_platform=payload.platform)
    threading.Thread(target=_run_live_record, args=(task_id, payload), daemon=True).start()
    return {
        "ok": True,
        "task_id": task_id,
        "room_name": payload.room_name,
        "room_url": resolved_url,
        "message": "直播录制任务已启动。",
    }


@app.post("/api/live-rec-delete-all/start")
def live_stop_all(payload: LiveRecPayload | None = None) -> dict[str, Any]:
    stopped = _stop_live_all()
    emit_log("success", f"已停止 {stopped} 个录制进程。", "live-rec")
    return {"ok": True, "message": f"已停止 {stopped} 个录制进程。"}


@app.post("/api/live-rec-marker/start")
def live_mark_product_switch(payload: LiveRecPayload | None = None) -> dict[str, Any]:
    marked = _mark_live_product_switch()
    if marked:
        emit_log("success", f"已标记 {marked} 个录制任务的换品观察点。", "live-rec")
        return {"ok": True, "message": f"已标记 {marked} 个换品观察点。"}
    emit_log("warning", "当前没有可标记的 active_product_probe 录制任务。", "live-rec")
    return {"ok": False, "message": "当前没有可标记的商品探针录制任务。"}


@app.post("/api/{feature}/start")
def start_placeholder(feature: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    names = {
        "mix": "混剪成片",
        "ai-scan-merge": "AI 扫描跨文件合并",
        "ai-scan-export": "AI扫描导出选中单品",
        "ai-scan-export-merge": "AI扫描导出合并结果",
        "ai-scan": "AI扫描",
        "product-scan-read": "单品扫描读取时间表",
        "product-scan": "单品扫描",
        "dedup": "创作辅助",
        "dedup-check": "创作辅助查重",
        "live-rec-monitor": "直播录制",
        "live-rec-detect": "直播录制检测流地址",
        "live-rec-delete-all": "直播录制删除全部",
        "live-rec-marker": "直播录制换品标记",
        "live-rec": "直播录制",
    }
    label = names.get(feature, feature)
    scope = feature
    if feature.startswith("ai-scan"):
        scope = "ai-scan"
    elif feature.startswith("product-scan"):
        scope = "product-scan"
    elif feature.startswith("live-rec"):
        scope = "live-rec"
    if payload:
        emit_log("info", f"{label} 已收到页面参数。", scope)
    emit_log("warning", f"{label} 的 Web 接口还在迁移中。", scope)
    return {"ok": False, "message": f"{label} 的 Web 接口还在迁移中。"}


@app.websocket("/ws/log")
async def log_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    last_id = max(0, _LOG_SEQ - 80)
    try:
        while True:
            for item in _snapshot_logs(last_id):
                await websocket.send_json({"type": "log", **item})
                last_id = max(last_id, int(item["id"]))
            await asyncio.sleep(0.35)
    except WebSocketDisconnect:
        return
    except RuntimeError:
        return


def main() -> None:
    import uvicorn

    emit_log("info", "LiveClipper Web Client 已启动。", "system")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
