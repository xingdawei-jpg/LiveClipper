from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
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
    return path.is_dir() and (path / "license_client.py").exists() and (path / "cutter_logic.py").exists()


def _select_app_dir(*candidates: Path) -> Path:
    valid = [path for path in candidates if _valid_app_dir(path)]
    if not valid:
        return candidates[0]
    valid.sort(key=lambda path: _version_key(_read_app_version(path)), reverse=True)
    return valid[0]


USER_UPDATE_ROOT = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper"
MODULE_WEB_DIR = Path(__file__).resolve().parent

if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    REPO_ROOT = BUNDLE_DIR
else:
    BUNDLE_DIR = MODULE_WEB_DIR.parent
    REPO_ROOT = MODULE_WEB_DIR.parent


def _valid_web_dir(path: Path) -> bool:
    return path.is_dir() and (path / "frontend" / "index.html").exists() and (path / "frontend" / "assets").is_dir()


def _select_web_dir(*candidates: Path) -> Path:
    for path in candidates:
        if _valid_web_dir(path):
            return path
    return candidates[0]


if getattr(sys, "frozen", False):
    WEB_DIR = _select_web_dir(USER_UPDATE_ROOT / "web_client", BUNDLE_DIR / "web_client", MODULE_WEB_DIR)
else:
    WEB_DIR = _select_web_dir(MODULE_WEB_DIR, USER_UPDATE_ROOT / "web_client")
APP_DIR = _select_app_dir(
    USER_UPDATE_ROOT / "app",
    REPO_ROOT / "app",
    WEB_DIR.parent / "app",
)
FRONTEND_DIR = WEB_DIR / "frontend"
ASSETS_DIR = FRONTEND_DIR / "assets"

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


app = FastAPI(title="LiveClipper Web Client", version="0.1.0")
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

_LOGS: deque[dict[str, Any]] = deque(maxlen=1500)
_LOG_LOCK = threading.Lock()
_LOG_SEQ = 0
_LOG_LAST_BY_SCOPE: dict[str, str] = {}
_TASKS: dict[str, dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()
_CANCELLED_TASKS: set[str] = set()
_SCAN_RESULTS: dict[str, Any] = {"products": [], "merged": []}
_SCAN_LOCK = threading.Lock()
_LIVE_PROCS: dict[str, dict[str, Any]] = {}
_LIVE_LOCK = threading.Lock()
_CLIP_PREVIEWS: dict[str, dict[str, Any]] = {}
_CLIP_PREVIEW_LOCK = threading.Lock()
_AI_PREVIEW_CACHE_SCHEMA = "focus_blocks_v2"
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
    if "quota" in lower or "余额" in lower or "insufficient" in lower or "限额" in lower:
        return "请检查账号余额、调用额度或并发限制。"
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
        "Hook候选池",
        "素材分组:",
        "结构修复:",
        "版本1结构:",
        "版本2结构:",
        "最终片段",
        "最终片段明细:",
        "最终片单:",
        "多版本: AI输出",
        "AI输出",
        "编排AI:",
        "JSON不完整",
        "降级方案",
        "成品时长:",
        "路径:",
        "大小:",
        "片段:",
        "综合评分:",
        "Hook:",
        "警告:",
        "去重效果:",
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
    return _read_json_file(_keyword_config_path())


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
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-1-5-pro-32k-250115",
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
        "ai_rules": dict(DEFAULT_AI_RULES),
        "ui_theme": "system",
        "hardware_encoder_enabled": False,
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
    return defaults


def _save_settings(settings: dict[str, Any]) -> bool:
    from ai_clipper import save_settings

    return bool(save_settings(settings))


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
    ai_rules: dict[str, Any] = Field(default_factory=dict)
    ui_theme: str = "system"
    hardware_encoder_enabled: bool = False


class LicensePayload(BaseModel):
    code: str = ""


class FileDialogPayload(BaseModel):
    kind: str = "file"
    title: str = ""


class PathPayload(BaseModel):
    path: str = ""


class PathsPayload(BaseModel):
    paths: list[str] = Field(default_factory=list)


class StopScopePayload(BaseModel):
    scope: str = ""


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


class AiScanPayload(BaseModel):
    video_paths: list[str] = Field(default_factory=list)
    output_dir: str = ""
    auto_export: bool = False


class SmartPreviewCutPayload(SmartCutPayload):
    preview_id: str = ""
    selected_indices: list[int] = Field(default_factory=list)


class MixPreviewCutPayload(MixPayload):
    preview_id: str = ""
    selected_indices: list[int] = Field(default_factory=list)


class SmartPreviewClipPayload(BaseModel):
    preview_id: str = ""
    clip_index: int = Field(default=0, ge=0)


class ProductScanPayload(BaseModel):
    excel_path: str = ""
    video_paths: list[str] = Field(default_factory=list)
    output_dir: str = ""
    advance_seconds: int = Field(default=0, ge=0, le=600)


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
    segment: str = "涓嶉檺"
    check_interval: int = Field(default=30, ge=5, le=300)
    room_name: str = ""
    room_url: str = ""
    platform: str = "自定义RTMP"


def _new_task(scope: str, title: str) -> str:
    task_id = f"{scope}-{int(time.time())}-{os.urandom(3).hex()}"
    with _TASK_LOCK:
        _TASKS[task_id] = {
            "id": task_id,
            "scope": scope,
            "title": title,
            "status": "queued",
            "started_at": None,
            "finished_at": None,
            "error": "",
        }
    return task_id


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
    with _TASK_LOCK:
        if task_id in _TASKS:
            current = _TASKS[task_id]
            next_status = updates.get("status")
            if current.get("status") == "cancelled" and next_status in {"running", "completed", "failed"}:
                return
            _TASKS[task_id].update(updates)


def _is_task_cancelled(task_id: str) -> bool:
    with _TASK_LOCK:
        return task_id in _CANCELLED_TASKS or _TASKS.get(task_id, {}).get("status") == "cancelled"


def _cancel_scope(scope: str) -> int:
    if not scope:
        return 0
    stopped = 0
    with _TASK_LOCK:
        for task_id, task in _TASKS.items():
            if task.get("scope") != scope or task.get("status") not in {"queued", "running"}:
                continue
            _CANCELLED_TASKS.add(task_id)
            task.update(
                status="cancelled",
                finished_at=time.time(),
                error="用户已停止",
            )
            stopped += 1
    if scope == "live-rec":
        stopped += _stop_live_all()
    emit_log("warning", "已发送停止请求。当前步骤结束后会停止。", scope)
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


def _safe_stem(name: str) -> str:
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()[:48] or "output"


def _default_output_dir(video: Path, explicit: str, folder: str = "output") -> Path:
    if explicit.strip():
        out = _clean_path(explicit)
    else:
        out = video.parent / folder
    out.mkdir(parents=True, exist_ok=True)
    return out


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
    if feature not in {"smart-cut", "smart-preview", "smart-from-preview", "mix", "mix-preview", "ai-scan", "ai-scan-export", "ai-scan-export-merge"}:
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


def _schedule_range(schedule: list[dict[str, Any]]) -> tuple[float, float]:
    starts = [float(item.get("start_offset", 0) or 0) for item in schedule]
    ends = [float(item.get("end_offset", 0) or 0) for item in schedule]
    return (min(starts), max(ends)) if starts and ends else (0.0, 0.0)


def _preflight_product_schedule(data: dict[str, Any], warnings: list[str], errors: list[str]) -> None:
    excel = str(data.get("excel_path") or "").strip()
    video_values = [str(v or "").strip() for v in (data.get("video_paths") or []) if str(v or "").strip()]
    if not excel or not video_values:
        return
    try:
        from copy import deepcopy
        from schedule_splitter import _probe_durations, align_schedule_to_video, read_excel

        schedule, live_start = read_excel(excel)
        if not schedule:
            errors.append("排品表未读取到有效商品时间段，请确认 Excel 格式是否正确。")
            return
        durations = _probe_durations(video_values, ffmpeg_cmd=_ffmpeg_cmd())
        total = sum(float(item or 0) for item in durations)
        if total <= 0:
            warnings.append("未能读取视频总时长，无法提前校验排品表时间范围。")
            return

        min_start, max_end = _schedule_range(schedule)
        if min_start >= -5 and max_end <= total + 5:
            return

        aligned = deepcopy(schedule)
        align_schedule_to_video(aligned, video_values, live_start, ffmpeg_cmd=_ffmpeg_cmd())
        aligned_min, aligned_max = _schedule_range(aligned)
        if aligned_min >= -5 and aligned_max <= total + 5:
            warnings.append(
                f"排品表时间已按视频文件名自动对齐：表格范围 {_hms(min_start)}~{_hms(max_end)}，对齐后 {_hms(aligned_min)}~{_hms(aligned_max)}。"
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
        "mix-preview",
        "ai-scan",
        "ai-scan-export",
        "ai-scan-export-merge",
        "product-scan",
        "dedup",
        "dedup-check",
        "live-rec-detect",
        "live-rec-monitor",
    }
    if needs_ffmpeg:
        _preflight_ffmpeg(errors)

    if feature in {"smart-cut", "smart-preview"}:
        _preflight_file_list(data.get("video_paths"), "视频", errors, min_count=1)
        _preflight_single_file(data.get("srt_path"), "字幕文件", errors, required=False)
        _preflight_output_dir(data.get("output_dir"), warnings, errors)
        _preflight_pip(data, warnings, errors)
        _preflight_ai_settings(feature, warnings)
    elif feature == "smart-from-preview":
        if not str(data.get("preview_id") or "").strip():
            errors.append("请先生成 AI 选片预览。")
        if not data.get("selected_indices"):
            errors.append("请至少保留一个片段。")
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
    _, text, focus = _clip_text_focus(clip)
    hay = f"{focus} {text}"
    if any(k in hay for k in ("\u663e\u7626", "\u906e\u8089", "\u85cf\u8089", "\u6536\u8170", "\u663e\u9ad8", "\u6bd4\u4f8b", "\u4fee\u9970", "\u7248\u578b", "\u5ed3\u5f62", "\u526a\u88c1", "\u5bbd\u677e", "\u4fee\u8eab", "\u906e\u80ef", "\u906e\u526f\u4e73", "\u80a9\u578b", "\u62dc\u62dc\u8089")):
        return "\u7248\u578b\u663e\u7626"
    if any(k in hay for k in ("\u9762\u6599", "\u6750\u8d28", "\u624b\u611f", "\u89e6\u611f", "\u5782\u611f", "\u900f\u6c14", "\u4eb2\u80a4", "\u67d4\u8f6f", "\u9488\u7ec7", "\u51b0\u4e1d", "\u771f\u4e1d", "\u68c9\u9ebb", "\u4e0d\u95f7", "\u4e0d\u900f", "\u7af9\u8282\u9ebb")):
        return "\u9762\u6599\u8d28\u611f"
    if any(k in hay for k in ("\u505a\u5de5", "\u5de5\u827a", "\u7ec6\u8282", "\u54c1\u8d28", "\u8d28\u611f", "\u9ad8\u7ea7\u611f", "\u7cbe\u81f4", "\u8d70\u7ebf", "\u523a\u7ee3", "\u857e\u4e1d", "\u91cd\u5de5", "\u52fe\u82b1")):
        return "\u54c1\u8d28\u7ec6\u8282"
    if any(k in hay for k in ("\u989c\u8272", "\u8272\u7cfb", "\u663e\u767d", "\u63d0\u6c14\u8272", "\u590d\u53e4", "\u9ed1\u8272", "\u767d\u8272", "\u5496\u8272", "\u82b1\u8272", "\u649e\u8272", "\u7126\u7cd6")):
        return "\u989c\u8272\u6c1b\u56f4"
    if any(k in hay for k in ("\u573a\u666f", "\u901a\u52e4", "\u7ea6\u4f1a", "\u65e5\u5e38", "\u804c\u573a", "\u51fa\u95e8", "\u5ea6\u5047", "\u62cd\u7167", "\u901b\u8857", "\u65c5\u6e38", "\u642d\u914d", "\u53e0\u7a7f", "\u5185\u642d", "\u5916\u7a7f", "\u6210\u5957", "\u5957\u7a7f", "\u6d77\u5c9b", "\u4e91\u5357")):
        return "\u573a\u666f\u642d\u914d"
    return "\u5176\u4ed6"
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

        if hasattr(ai_mod, "_dedup_clip_text_overlap"):
            _step("time_text_dedup", lambda items: ai_mod._dedup_clip_text_overlap(items, None, merge_mode=merge_mode))
        if hasattr(ai_mod, "_filter_semantic_repeat"):
            _step("semantic_dedup", lambda items: ai_mod._filter_semantic_repeat(items, None))
        if srt_text and hasattr(ai_mod, "_fix_clip_boundaries"):
            _step("boundary_fix", lambda items: ai_mod._fix_clip_boundaries(items, srt_text, None))
        if hasattr(ai_mod, "_remove_expanded_overlap_clips"):
            _step("expanded_overlap_dedup", lambda items: ai_mod._remove_expanded_overlap_clips(items, None))
        if hasattr(ai_mod, "_reorder_product_focus_blocks"):
            normalized = list(ai_mod._reorder_product_focus_blocks(normalized, None) or normalized)
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
            source = str(clip[7] if len(clip) > 7 else "").strip()
    except Exception:
        clip_type, text, start, end, score, duration, focus, source = "product", str(clip), 0.0, 0.0, 0.0, 0.0, "", ""
    if end < start:
        end = start
    if duration <= 0:
        duration = max(0.0, end - start)
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
        "source": source,
        "source_name": Path(source).name if source else "",
    }


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
        "srt_path": preview.get("srt_path", ""),
        "created_at": preview.get("created_at", 0),
        "clips": preview.get("clips", []),
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


def _preview_clip_video(preview_id: str, clip_index: int) -> Path:
    preview = _get_preview(preview_id)
    if not preview or preview.get("status") != "ready":
        raise HTTPException(status_code=404, detail="AI 选片预览不存在或尚未完成。")
    raw_clips = list(preview.get("raw_clips") or [])
    if clip_index < 0 or clip_index >= len(raw_clips):
        raise HTTPException(status_code=404, detail="片段不存在，请重新生成预览。")
    clip_info = _clip_public(clip_index, raw_clips[clip_index])
    clip_source = str(clip_info.get("source") or "").strip()
    video = Path(clip_source) if clip_source else Path(str(preview.get("video", "")))
    if not video.exists():
        raise HTTPException(status_code=404, detail="源视频不存在，请重新选择视频。")

    start = max(0.0, float(clip_info.get("start") or 0.0))
    end = max(start + 0.2, float(clip_info.get("end") or start + 0.2))
    duration = max(0.2, end - start)
    if duration > 90:
        raise HTTPException(status_code=400, detail="片段过长，暂不支持在线预览。")

    preview_dir = _safe_user_child("clip_previews", preview_id)
    preview_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{clip_index}_{int(start * 1000)}_{int(end * 1000)}"
    target = preview_dir / f"clip_{stamp}.mp4"
    if target.exists() and target.stat().st_size > 1000:
        return target

    cmd = [
        _ffmpeg_cmd(),
        "-y",
        "-i",
        str(video),
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-fflags",
        "+genpts",
        "-avoid_negative_ts",
        "make_zero",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,setpts=PTS-STARTPTS",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
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
    if not all([tos_ak, tos_sk]) or not (api_key or all([app_id, access_token])):
        emit_log("warning", "云端语音识别未配置完整，正在使用本地语音识别。", scope)
        return None

    import hashlib
    import tempfile

    emit_log("info", "正在使用云端语音识别。", scope)
    temp_dir = Path(tempfile.gettempdir()) / "live_cutter_web_asr"
    temp_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.md5(str(video).encode("utf-8", errors="ignore")).hexdigest()[:12]
    wav = temp_dir / f"audio_{digest}_{int(time.time())}.wav"
    try:
        cmd = [
            _ffmpeg_cmd(),
            "-y",
            "-i",
            str(video),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(wav),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0 or not wav.exists():
            detail = (result.stderr or b"").decode("utf-8", errors="ignore").strip()
            if detail:
                detail = detail.splitlines()[-1]
            emit_log("warning", f"云端语音识别准备音频失败: {detail or result.returncode}", scope)
            return None

        from volcengine_asr import volcengine_asr

        segments = volcengine_asr(
            str(wav),
            app_id,
            access_token,
            tos_ak,
            tos_sk,
            bucket=bucket,
            log_fn=lambda msg: emit_log("info", msg, scope),
            api_key=api_key or None,
        )
        if not segments:
            return None
        srt_text = _segments_to_srt(segments)
        if not srt_text.strip():
            return None
        srt.write_text(srt_text, encoding="utf-8")
        emit_log("info", f"云端语音识别成功：{len(segments)} 条语音段。", scope)
        return srt
    except Exception as exc:
        emit_log("warning", f"云端语音识别异常: {type(exc).__name__}: {exc}", scope)
        return None
    finally:
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass


def _product_scanner():
    from ai_clipper import load_settings
    from product_scanner import ProductScanner

    settings = load_settings()
    return ProductScanner(
        api_key=settings.get("api_key", ""),
        base_url=settings.get("base_url", "https://api.deepseek.com"),
        model=settings.get("model", "deepseek-chat"),
    )


def _run_mix(task_id: str, payload: MixPayload) -> None:
    scope = "mix"
    _set_task(task_id, status="running", started_at=time.time())
    try:
        _ensure_feature_access("娣峰壀鎴愮墖")
        from cutter_logic import process_video_mix

        paths = _existing_paths(payload.video_paths, "视频")
        out_dir = _default_output_dir(paths[0], payload.output_dir, "mix_output")
        output_path = out_dir / f"mix_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        pip_path, used_pip_file = _pick_pip_asset(payload, scope)
        emit_log("info", f"混剪开始：{len(paths)} 个视频，版本数={payload.versions}，目标时长={payload.duration}秒，输出 {output_path}", scope)
        result = process_video_mix(
            [str(p) for p in paths],
            output_path=str(output_path),
            dedup_preset=payload.dedup_preset,
            subtitle_overlay=payload.subtitle_overlay,
            log_fn=lambda msg: emit_log("info", msg, scope),
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
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        _archive_used_pip(used_pip_file, scope)
        _consume_trial("娣峰壀鎴愮墖", scope=scope)
        _set_task(task_id, status="completed", finished_at=time.time())
        emit_log("success", "混剪成片完成。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"混剪成片失败：{exc}", scope)


def _run_mix_preview(task_id: str, preview_id: str, payload: MixPayload) -> None:
    scope = "mix"
    _set_task(task_id, status="running", started_at=time.time())
    _store_preview(
        preview_id,
        task_id=task_id,
        scope=scope,
        status="running",
        message="正在生成混剪 AI 选片预览。",
        created_at=time.time(),
        clips=[],
    )
    emit_log("info", "混剪 AI 选片预览任务已启动。", scope)
    try:
        _ensure_feature_access("娣峰壀鎴愮墖")
        import cutter_logic as cutter_mod
        from cutter_logic import process_video_mix

        paths = _existing_paths(payload.video_paths, "视频")
        out_dir = _default_output_dir(paths[0], payload.output_dir, "mix_output")
        preview_output = str(out_dir / f"mix_preview_placeholder_{int(time.time())}.mp4")
        cutter_mod._multi_result_cache = {}
        result = process_video_mix(
            [str(p) for p in paths],
            output_path=preview_output,
            dedup_preset=payload.dedup_preset,
            subtitle_overlay=payload.subtitle_overlay,
            log_fn=lambda msg: emit_log("info", msg, scope),
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
        raw_clips = list(cutter_mod._multi_result_cache.get("clips") or [])
        if not raw_clips:
            raise RuntimeError("AI 没有选到可预览片段。")
        raw_clips, dedup_summary = _normalize_preview_final_clips(raw_clips, merge_mode=True)
        category_summary = dict(cutter_mod._multi_result_cache.get("category_summary") or {})
        if category_summary:
            dedup_summary["category_summary"] = category_summary
        public_clips = [_clip_public(index, clip) for index, clip in enumerate(raw_clips)]
        dedup_summary.update(_annotate_preview_manual_repeats(public_clips))
        _store_preview(
            preview_id,
            status="ready",
            message=f"混剪 AI 选片预览完成，共 {len(public_clips)} 个片段。",
            video=str(paths[0]),
            video_name=paths[0].name,
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
    _set_task(task_id, status="running", started_at=time.time())
    try:
        _ensure_feature_access("AI扫描")
        scanner = _product_scanner()
        paths = _existing_paths(payload.video_paths, "视频")
        out_dir = _default_output_dir(paths[0], payload.output_dir, "scan_output") if payload.output_dir.strip() else None
        all_file_products: list[list[dict[str, Any]]] = []
        flat_products: list[dict[str, Any]] = []
        for index, video in enumerate(paths, start=1):
            if _is_task_cancelled(task_id):
                emit_log("warning", "任务已停止。", scope)
                return
            emit_log("info", f"[{index}/{len(paths)}] 扫描 {video.name}", scope)
            srt = _ensure_srt(video, scope)
            if not srt:
                continue
            products = scanner.scan(str(srt), log_fn=lambda msg: emit_log("info", msg, scope))
            for product in products:
                product["_video"] = str(video)
            all_file_products.append(products)
            flat_products.extend(products)
            emit_log("success" if products else "warning", f"{video.name} 发现 {len(products)} 个单品。", scope)
            if payload.auto_export and out_dir and products:
                for product in products:
                    paths_out = scanner.extract_clip(str(video), product, str(out_dir), product.get("name"))
                    if paths_out:
                        emit_log("success", f"已导出：{product.get('name')} ({len(paths_out)} 段)", scope)
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
    _set_task(task_id, status="running", started_at=time.time())
    try:
        from product_scanner import merge_across_files

        with _SCAN_LOCK:
            products = list(_SCAN_RESULTS.get("products") or [])
        merged = merge_across_files(products, log_fn=lambda msg: emit_log("info", msg, scope))
        with _SCAN_LOCK:
            _SCAN_RESULTS["merged"] = merged
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
    _set_task(task_id, status="running", started_at=time.time())
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
            results = scanner.extract_cross_file(merged_products, str(out_dir), log_fn=lambda msg: emit_log("info", msg, scope))
            ok_count = len([r for r in results if r.get("output_path")])
        else:
            if not flat:
                raise ValueError("没有可导出的扫描结果。")
            for product in flat:
                video = product.get("_video", "")
                if not video:
                    continue
                paths_out = scanner.extract_clip(video, product, str(out_dir), product.get("name"))
                if paths_out:
                    ok_count += len(paths_out)
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
    _set_task(task_id, status="running", started_at=time.time())
    try:
        from schedule_splitter import group_by_product, read_excel

        excel = _clean_path(payload.excel_path)
        if not excel.exists():
            raise FileNotFoundError("Excel 文件不存在。")
        schedule, live_start = read_excel(str(excel), log_fn=lambda msg: emit_log("info", msg, scope))
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
    _set_task(task_id, status="running", started_at=time.time())
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
        schedule, live_start = read_excel(str(excel), log_fn=lambda msg: emit_log("info", msg, scope))
        align_schedule_to_video(schedule, [str(v) for v in videos], live_start, log_fn=lambda msg: emit_log("info", msg, scope), ffmpeg_cmd=_ffmpeg_cmd())
        groups = group_by_product(schedule)
        if payload.advance_seconds > 0:
            for group in groups:
                segments = [(max(0, s - payload.advance_seconds), max(0, e - payload.advance_seconds)) for s, e in group.get("segments", [])]
                group["segments"] = [(s, e) for s, e in segments if e - s >= 10]
                group["total_duration"] = sum(e - s for s, e in group["segments"])
        groups = [g for g in groups if g.get("segments")]
        results = extract_by_schedule(groups, [str(v) for v in videos], str(out_dir), ffmpeg=_ffmpeg_cmd(), log_fn=lambda msg: emit_log("info", msg, scope))
        ok_count = len([r for r in results if r.get("output_path")])
        if ok_count:
            _consume_trial("单品扫描", units=ok_count, scope=scope)
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        _set_task(task_id, status="completed", finished_at=time.time(), result_count=ok_count)
        emit_log("success", f"单品扫描完成：{ok_count}/{len(groups)} 个商品导出成功。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"单品扫描失败：{exc}", scope)


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
        af.append("asetrate=44100*1.015,aresample=44100")
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
    _set_task(task_id, status="running", started_at=time.time())
    try:
        _ensure_feature_access("创作辅助")
        videos = _dedup_paths_from_payload(payload)
        if not videos:
            raise ValueError("请先添加视频。")
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
                emit_log("error", f"[{index}/{len(videos)}] 文件不存在：{video}", scope)
                continue
            out_dir = base_dir if payload.output_dir.strip() else _default_output_dir(video, "", "dedup_output")
            output = _unique_output_path(out_dir, _safe_stem(video.stem))
            emit_log("info", f"[{index}/{len(videos)}] 正在处理：{video.name}", scope)
            ok, applied, stderr_text, used_pip_file = _run_dedup_one(video, output, payload, index, scope=scope)
            if ok:
                outputs.append(str(output))
                emit_log("success", f"[{index}/{len(videos)}] 完成：{output.name}（{applied}）", scope)
                _archive_used_pip(used_pip_file, scope)
            else:
                failures.append(f"{video.name}: {stderr_text or '视频处理失败'}")
                emit_log("error", f"[{index}/{len(videos)}] 处理失败：{_short_error(stderr_text)}。解决办法：{_solution_for_error(stderr_text)}", scope)
        if not outputs:
            raise RuntimeError(failures[0] if failures else "没有生成成功的视频。")
        _consume_trial("创作辅助", scope=scope)
        _set_task(task_id, status="completed", finished_at=time.time(), output=outputs[0], outputs=outputs)
        if failures:
            emit_log("warning", f"批量二创消重完成：成功 {len(outputs)} 个，失败 {len(failures)} 个。", scope)
        else:
            emit_log("success", f"批量二创消重完成：成功 {len(outputs)} 个。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"创作辅助处理失败：{exc}", scope)


def _live_segment_seconds(label: str) -> int:
    return {"不限": 36000, "30分钟": 1800, "60分钟": 3600, "1小时": 3600, "120分钟": 7200, "2小时": 7200}.get(label, 36000)


def _resolve_live_url(url: str, scope: str) -> str:
    if "douyin.com" in url and not url.endswith(".flv") and not url.endswith(".m3u8"):
        from douyin_stream import extract_live_url

        resolved = extract_live_url(url, log_fn=lambda msg: emit_log("info", msg, scope))
        if resolved:
            return resolved
    return url


def _run_live_detect(task_id: str, payload: LiveRecPayload) -> None:
    scope = "live-rec"
    _set_task(task_id, status="running", started_at=time.time())
    try:
        url = (payload.room_url or "").strip()
        if not url:
            raise ValueError("请填写直播间地址。")
        resolved = _resolve_live_url(url, scope)
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
    _set_task(task_id, status="running", started_at=time.time())
    try:
        _ensure_feature_access("鐩存挱褰曞埗")
        url = (payload.room_url or "").strip()
        if not url:
            raise ValueError("请填写直播间地址。")
        stream_url = _resolve_live_url(url, scope)
        save_dir = _clean_path(payload.save_dir) if payload.save_dir.strip() else Path.home() / "Videos" / "鐩存挱褰曞埗"
        name = _safe_stem(payload.room_name or "live")
        room_dir = save_dir / name
        room_dir.mkdir(parents=True, exist_ok=True)
        output = room_dir / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.flv"
        cmd = [_ffmpeg_cmd(), "-y", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30", "-i", stream_url, "-c", "copy", "-t", str(_live_segment_seconds(payload.segment)), str(output)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with _LIVE_LOCK:
            _LIVE_PROCS[task_id] = {"process": proc, "output": str(output), "name": name}
        emit_log("success", f"开始录制：{name}", scope)
        rc = proc.wait()
        size = output.stat().st_size if output.exists() else 0
        if rc != 0 or size < 1000:
            raise RuntimeError("录制进程结束但未生成有效文件。")
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        _consume_trial("鐩存挱褰曞埗", scope=scope)
        _set_task(task_id, status="completed", finished_at=time.time(), output=str(output))
        emit_log("success", f"录制完成：{output.name} ({size / 1024 / 1024:.1f}MB)", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"直播录制失败：{exc}", scope)
    finally:
        with _LIVE_LOCK:
            _LIVE_PROCS.pop(task_id, None)


def _stop_live_all() -> int:
    stopped = 0
    with _LIVE_LOCK:
        items = list(_LIVE_PROCS.items())
    for task_id, info in items:
        proc = info.get("process")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            stopped += 1
        with _LIVE_LOCK:
            _LIVE_PROCS.pop(task_id, None)
    return stopped


def _run_smart_cut(task_id: str, payload: SmartCutPayload) -> None:
    _set_task(task_id, status="running", started_at=time.time())
    emit_log("info", "智能成片任务已启动。", "smart-cut")
    try:
        _ensure_feature_access("智能成片")
        from cutter_logic import process_video, process_video_multi

        paths = [Path(p.strip().strip('"')) for p in payload.video_paths if p.strip()]
        if not paths:
            raise ValueError("请至少填写一个视频文件路径。")

        output_dir = Path(payload.output_dir.strip().strip('"')) if payload.output_dir.strip() else None
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)

        for index, video in enumerate(paths, start=1):
            if _is_task_cancelled(task_id):
                emit_log("warning", "任务已停止。", "smart-cut")
                return
            if not video.exists():
                raise FileNotFoundError(f"视频不存在：{video}")

            out_path = None
            if output_dir:
                out_path = str(output_dir / f"{video.stem}_smart_cut_{_stamp_name()}.mp4")

            pip_path, used_pip_file = _pick_pip_asset(payload, "smart-cut")
            emit_log("info", f"[{index}/{len(paths)}] 开始处理 {video.name}", "smart-cut")
            version_count = payload.versions
            common_kwargs = dict(
                srt_path=payload.srt_path.strip() or None,
                output_path=out_path,
                dedup_preset=payload.dedup_preset,
                subtitle_overlay=payload.subtitle_overlay,
                log_fn=lambda msg: emit_log("info", msg, "smart-cut"),
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
            if not result:
                raise RuntimeError(f"{video.name} 处理失败。")
            if _is_task_cancelled(task_id):
                emit_log("warning", "任务已停止。", "smart-cut")
                return
            _archive_used_pip(used_pip_file, "smart-cut")
            emit_log("success", f"处理完成: {video.name}", "smart-cut")
            _consume_trial("智能成片", scope="smart-cut")

        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", "smart-cut")
            return
        _set_task(task_id, status="completed", finished_at=time.time())
        emit_log("success", "智能成片任务完成。", "smart-cut")
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"智能成片失败: {exc}", "smart-cut")


def _run_mix_from_preview(task_id: str, payload: MixPreviewCutPayload) -> None:
    scope = "mix"
    _set_task(task_id, status="running", started_at=time.time())
    emit_log("info", "使用混剪 AI 选片预览开始成片。", scope)
    try:
        _ensure_feature_access("娣峰壀鎴愮墖")
        preview = _get_preview(payload.preview_id)
        if not preview or preview.get("status") != "ready":
            raise RuntimeError("混剪选片预览不存在或尚未完成。")
        raw_clips = list(preview.get("raw_clips") or [])
        if not raw_clips:
            raise RuntimeError("混剪选片预览里没有可用片段。")

        selected = payload.selected_indices or list(range(len(raw_clips)))
        selected_indices = [int(i) for i in selected if 0 <= int(i) < len(raw_clips)]
        clips = [raw_clips[index] for index in selected_indices]
        if not clips:
            raise RuntimeError("请至少保留一个片段再混剪。")

        sources: list[Path] = []
        seen_sources: set[str] = set()
        for clip in clips:
            clip_info = _clip_public(0, clip)
            source = str(clip_info.get("source") or "").strip()
            if not source:
                continue
            path = Path(source)
            key = str(path.resolve()).lower() if path.exists() else str(path).lower()
            if key not in seen_sources:
                sources.append(path)
                seen_sources.add(key)
        if not sources:
            sources = _existing_paths(payload.video_paths, "视频")
        missing_sources = [path for path in sources if not path.exists()]
        if missing_sources:
            missing_text = ", ".join(str(path) for path in missing_sources[:3])
            raise FileNotFoundError(f"预览片段对应的原视频不存在：{missing_text}")
        existing_sources = [path for path in sources if path.exists()]
        if not existing_sources:
            raise FileNotFoundError("预览片段对应的原视频不存在，请重新选择素材并生成预览。")

        source_index = {
            str(path.resolve()).lower(): index
            for index, path in enumerate(existing_sources)
            if path.exists()
        }

        def _num(value: Any, fallback: float = 0.0) -> float:
            try:
                return float(value)
            except Exception:
                return fallback

        emit_log("info", f"预览混剪：按预览保留结果使用 {len(clips)} 个片段，不二次去重。", scope)

        def _clip_tuple(clip: Any) -> tuple[Any, ...]:
            clip_info = _clip_public(0, clip)
            source = Path(str(clip_info.get("source") or existing_sources[0]))
            key = str(source.resolve()).lower() if source.exists() else str(source).lower()
            idx = source_index.get(key, 0)
            text = str(clip_info.get("text") or "")
            if "[V" not in text:
                text = f"[V{idx + 1}] {text}"
            start = _num(clip_info.get("start"), 0.0)
            end = _num(clip_info.get("end"), start)
            duration = _num(clip_info.get("duration"), max(0.0, end - start))
            return (
                clip_info.get("clip_type") or "product",
                text,
                start,
                end,
                _num(clip_info.get("score"), 0.0),
                duration,
                str(clip_info.get("focus") or ""),
            )

        selected_tuples = [_clip_tuple(clip) for clip in clips]
        out_dir = _default_output_dir(existing_sources[0], payload.output_dir, "mix_output")
        output_path = out_dir / f"mix_preview_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        pip_path, used_pip_file = _pick_pip_asset(payload, scope)

        import ai_clipper as ai_mod
        from cutter_logic import process_video_mix

        original_ai_analyze = ai_mod.ai_analyze_clips
        original_ai_enabled = ai_mod.is_enabled

        def _preview_ai_analyze(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
            emit_log("info", f"预览混剪：使用已调整的 {len(selected_tuples)} 个片段，不重新 AI 选片。", scope)
            return list(selected_tuples)

        ai_mod.ai_analyze_clips = _preview_ai_analyze
        ai_mod.is_enabled = lambda: True
        try:
            result = process_video_mix(
                [str(path) for path in existing_sources],
                output_path=str(output_path),
                dedup_preset=payload.dedup_preset,
                subtitle_overlay=payload.subtitle_overlay,
                log_fn=lambda msg: emit_log("info", msg, scope),
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
        if _is_task_cancelled(task_id):
            emit_log("warning", "任务已停止。", scope)
            return
        _archive_used_pip(used_pip_file, scope)
        _consume_trial("娣峰壀鎴愮墖", scope=scope)
        _set_task(task_id, status="completed", finished_at=time.time(), output=str(output_path))
        emit_log("success", f"预览混剪完成：{output_path}", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"预览混剪失败：{exc}", scope)


def _run_smart_preview(task_id: str, preview_id: str, payload: SmartCutPayload) -> None:
    scope = "smart-cut"
    _set_task(task_id, status="running", started_at=time.time())
    _store_preview(
        preview_id,
        task_id=task_id,
        scope=scope,
        status="running",
        message="正在生成 AI 选片预览。",
        created_at=time.time(),
        clips=[],
    )
    emit_log("info", "AI 选片预览任务已启动。", scope)
    try:
        _ensure_feature_access("智能成片")
        import cutter_logic as cutter_mod
        from cutter_logic import process_video

        paths = _existing_paths(payload.video_paths, "视频")
        video = paths[0]
        if len(paths) > 1:
            emit_log("warning", "当前预览先处理第 1 个视频；多视频批量预览后续补齐。", scope)

        cutter_mod._multi_result_cache = {}
        srt_path = payload.srt_path.strip() or None
        out_dir = _default_output_dir(video, payload.output_dir, "output")
        preview_output = str(out_dir / f"{video.stem}_preview_placeholder.mp4")
        emit_log("info", f"正在分析视频并生成片预览：{video.name}", scope)
        result = process_video(
            str(video),
            srt_path=srt_path,
            output_path=preview_output,
            dedup_preset=payload.dedup_preset,
            subtitle_overlay=payload.subtitle_overlay,
            log_fn=lambda msg: emit_log("info", msg, scope),
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
        raw_clips = list(cutter_mod._multi_result_cache.get("clips") or [])
        if not raw_clips:
            raise RuntimeError("AI 没有选到可预览片段。")
        resolved_srt = str(cutter_mod._multi_result_cache.get("srt_path") or srt_path or video.with_suffix(".srt"))
        srt_text = str(cutter_mod._multi_result_cache.get("srt_text") or "")
        raw_clips, dedup_summary = _normalize_preview_final_clips(raw_clips, srt_text, default_source=str(video))
        category_summary = dict(cutter_mod._multi_result_cache.get("category_summary") or {})
        if category_summary:
            dedup_summary["category_summary"] = category_summary
        public_clips = [_clip_public(index, clip) for index, clip in enumerate(raw_clips)]
        dedup_summary.update(_annotate_preview_manual_repeats(public_clips))
        _store_preview(
            preview_id,
            status="ready",
            message=f"AI 选片预览完成，共 {len(public_clips)} 个片段。",
            video=str(video),
            video_name=video.name,
            srt_path=resolved_srt,
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
    _set_task(task_id, status="running", started_at=time.time())
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
        selected = payload.selected_indices or list(range(len(raw_clips)))
        selected_indices = [int(i) for i in selected if 0 <= int(i) < len(raw_clips)]
        clips = [raw_clips[index] for index in selected_indices]
        if not clips:
            raise RuntimeError("请至少保留一个片段再成片。")

        video = Path(str(preview.get("video", "")))
        if not video.exists():
            raise FileNotFoundError(f"视频不存在：{video}")
        srt_path = str(preview.get("srt_path") or video.with_suffix(".srt"))
        emit_log("info", f"预览成片：按预览保留结果使用 {len(clips)} 个片段，不二次去重。", scope)
        output_dir = Path(payload.output_dir.strip().strip('"')) if payload.output_dir.strip() else video.parent / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / f"{video.stem}_preview_cut_{_stamp_name()}.mp4")
        pip_path, used_pip_file = _pick_pip_asset(payload, scope)

        result = _process_version_with_clips(
            str(video),
            srt_path,
            output_path,
            clips,
            payload.dedup_preset,
            payload.subtitle_overlay,
            lambda msg: emit_log("info", msg, scope),
            None,
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
        )
        if not result:
            raise RuntimeError("预览成片失败。")
        _archive_used_pip(used_pip_file, scope)
        _consume_trial("智能成片", scope=scope)
        _set_task(task_id, status="completed", finished_at=time.time())
        emit_log("success", "预览成片完成。", scope)
    except Exception as exc:
        _set_task(task_id, status="failed", finished_at=time.time(), error=str(exc))
        emit_log("error", f"预览成片失败：{exc}", scope)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/api/runtime")
def runtime() -> dict[str, Any]:
    return {
        "version": _load_version(),
        "repo_root": str(REPO_ROOT),
        "user_data_dir": str(_get_user_data_dir()),
        "mode": "local-web-client",
    }


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
                emit_log("success", "更新已安装，重启客户端后生效。", "settings")
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
        from license_client import activate_with_code

        result = activate_with_code(code)
        ok = bool(result.get("ok"))
        emit_log("success" if ok else "warning", result.get("msg", "激活完成"), "settings")
        return {"ok": ok, "message": result.get("msg", "激活完成")}
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


@app.post("/api/settings/test-ai")
def test_ai(payload: SettingsPayload | None = None) -> dict[str, Any]:
    cfg = _load_settings()
    if payload:
        cfg.update(payload.model_dump(exclude_unset=True))
    api_key = (cfg.get("api_key") or "").strip()
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    if not api_key or not base_url:
        return {"ok": False, "message": "请先填写 AI API Key 和 Base URL。"}

    url = base_url + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
            if 200 <= resp.status < 300:
                emit_log("success", "AI 连接测试通过。", "settings")
                return {"ok": True, "message": "AI 连接测试通过。"}
            return {"ok": False, "message": f"AI 连接异常: HTTP {resp.status}"}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "message": f"AI 杩炴帴澶辫触: HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "message": f"AI 杩炴帴澶辫触: {exc}"}


@app.post("/api/settings/diagnose-volcengine")
async def diagnose_volcengine_endpoint(payload: SettingsPayload | None = None) -> dict[str, Any]:
    cfg = _load_settings()
    if payload:
        cfg.update(payload.model_dump(exclude_unset=True))

    emit_log("info", "开始火山完整诊断。", "settings")

    def _run() -> dict[str, Any]:
        from volcengine_asr import diagnose_volcengine

        return diagnose_volcengine(
            app_id=cfg.get("volc_app_id", ""),
            access_token=cfg.get("volc_access_token", ""),
            tos_ak=cfg.get("volc_tos_ak", ""),
            tos_sk=cfg.get("volc_tos_sk", ""),
            bucket=cfg.get("volc_bucket", ""),
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
    data = _load_keyword_config()

    count = 0
    for value in data.values():
        if isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, list):
                    count += len(nested)
        elif isinstance(value, list):
            count += len(value)

    return {"ok": True, "count": count, "keywords": data, "source": str(source)}


@app.post("/api/keywords")
def save_keywords(payload: dict[str, Any]) -> dict[str, Any]:
    target = _safe_user_child("keywords.json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    emit_log("success", "关键词配置已保存。", "settings")
    return {"ok": True, "message": "关键词已保存", "path": str(target)}


@app.post("/api/cache/clear")
def clear_cache() -> dict[str, Any]:
    global _PREVIEW_CLEARED_AT
    removed = []
    for name in ("cache", "temp", "clip_previews", "web_uploads"):
        target = _safe_user_child(name)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(str(target))
    with _CLIP_PREVIEW_LOCK:
        _PREVIEW_CLEARED_AT = time.time()
        _CLIP_PREVIEWS.clear()
    emit_log("success", "缓存清理完成。", "settings")
    return {"ok": True, "message": "缓存清理完成", "removed": removed}


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


@app.post("/api/tasks/stop-scope")
def stop_scope(payload: StopScopePayload) -> dict[str, Any]:
    scope = (payload.scope or "").strip()
    stopped = _cancel_scope(scope)
    return {"ok": True, "message": f"已发送停止请求（{stopped} 个任务）", "stopped": stopped}


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


@app.get("/api/smart-cut/preview/latest")
def get_latest_smart_preview() -> dict[str, Any]:
    return _preview_public(_latest_preview("smart-cut"))


@app.get("/api/smart-cut/preview/{preview_id}")
def get_smart_preview(preview_id: str) -> dict[str, Any]:
    return _preview_public(_get_preview(preview_id))


@app.post("/api/smart-cut/from-preview/start")
def start_smart_from_preview(payload: SmartPreviewCutPayload) -> dict[str, Any]:
    _ensure_scope_idle("smart-cut", "预览成片")
    _raise_preflight_errors("smart-from-preview", payload)
    if not payload.preview_id.strip():
        raise HTTPException(status_code=400, detail="请先生成 AI 选片预览。")
    preview = _get_preview(payload.preview_id)
    if not preview:
        raise HTTPException(status_code=404, detail="没有找到这次 AI 选片预览，请重新生成。")
    if preview.get("status") != "ready":
        raise HTTPException(status_code=400, detail="AI 选片预览还没有完成，请稍后再试。")
    if not preview.get("raw_clips"):
        raise HTTPException(status_code=400, detail="这次预览没有可用片段，请重新生成。")
    if not payload.selected_indices:
        raise HTTPException(status_code=400, detail="请至少保留一个片段再成片。")
    clip_count = len(preview.get("raw_clips") or [])
    selected = [int(index) for index in payload.selected_indices if 0 <= int(index) < clip_count]
    if not selected:
        raise HTTPException(status_code=400, detail="请至少保留一个有效片段再成片。")
    payload.selected_indices = selected
    task_id = _new_task("smart-cut", "预览成片")
    threading.Thread(target=_run_smart_cut_from_preview, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "预览成片任务已启动。"}


@app.post("/api/smart-cut/preview/clip-video")
def smart_preview_clip_video(payload: SmartPreviewClipPayload) -> dict[str, Any]:
    if not payload.preview_id.strip():
        raise HTTPException(status_code=400, detail="请先生成 AI 选片预览。")
    path = _preview_clip_video(payload.preview_id.strip(), payload.clip_index)
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
    _ensure_scope_idle("mix", "娣峰壀鎴愮墖")
    _raise_preflight_errors("mix", payload)
    if not [path.strip() for path in payload.video_paths if path.strip()]:
        raise HTTPException(status_code=400, detail="请至少填写一个视频文件路径。")
    task_id = _new_task("mix", "娣峰壀鎴愮墖")
    threading.Thread(target=_run_mix, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "混剪任务已启动。"}


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
    task_id = _new_task("mix", "预览混剪成片")
    threading.Thread(target=_run_mix_from_preview, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "预览混剪任务已启动。"}


@app.post("/api/mix/preview/clip-video")
def mix_preview_clip_video(payload: SmartPreviewClipPayload) -> dict[str, Any]:
    if not payload.preview_id.strip():
        raise HTTPException(status_code=400, detail="请先生成混剪 AI 选片预览。")
    path = _preview_clip_video(payload.preview_id.strip(), payload.clip_index)
    return {
        "ok": True,
        "url": f"/api/smart-cut/preview/clip-video/{payload.preview_id.strip()}/{payload.clip_index}?t={int(path.stat().st_mtime)}",
    }


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
    task_id = _new_task("product-scan", "单品扫描")
    threading.Thread(target=_run_product_scan, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "单品扫描任务已启动。"}


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
    task_id = _new_task("live-rec", "棢测直播流")
    threading.Thread(target=_run_live_detect, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "直播流检测已启动"}


@app.post("/api/live-rec-monitor/start")
def start_live_monitor(payload: LiveRecPayload) -> dict[str, Any]:
    _raise_preflight_errors("live-rec-monitor", payload)
    task_id = _new_task("live-rec", "鐩存挱褰曞埗")
    threading.Thread(target=_run_live_record, args=(task_id, payload), daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "直播录制任务已启动。"}


@app.post("/api/live-rec-delete-all/start")
def live_stop_all(payload: LiveRecPayload | None = None) -> dict[str, Any]:
    stopped = _stop_live_all()
    emit_log("success", f"已停止 {stopped} 个录制进程。", "live-rec")
    return {"ok": True, "message": f"已停止 {stopped} 个录制进程。"}


@app.post("/api/{feature}/start")
def start_placeholder(feature: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    names = {
        "mix": "娣峰壀鎴愮墖",
        "ai-scan-merge": "AI 扫描跨文件合并",
        "ai-scan-export": "AI扫描导出选中单品",
        "ai-scan-export-merge": "AI扫描导出合并结果",
        "ai-scan": "AI扫描",
        "product-scan-read": "单品扫描读取时间表",
        "product-scan": "单品扫描",
        "dedup": "创作辅助",
        "dedup-check": "创作辅助查重",
        "live-rec-monitor": "鐩存挱褰曞埗鐩戞帶",
        "live-rec-detect": "直播录制检测流地址",
        "live-rec-delete-all": "直播录制删除全部",
        "live-rec": "鐩存挱褰曞埗",
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
