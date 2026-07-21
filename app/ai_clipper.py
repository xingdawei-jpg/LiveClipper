# -*- coding: utf-8 -*-
"""
AI 智能选片模块 v5.0 - 抖音女装带货爆款逻辑
前置清洗(三级兜底降级)+ 强制数量约束 + temperature=0.1
"""

import json
import hashlib
# 模块级时长配置：由 cutter_logic 在调用 ai_analyze_clips 前设置
_AI_TARGET_DURATION = 60
_AI_CLIP_COUNT = "10-15"
_detail_kw_prompt = ""
_LAST_CATEGORY_FILTER_SUMMARY = {}
_LAST_FOCUS_SUMMARY = {}
_LAST_TOPIC_COVERAGE_SUMMARY = {}
_LAST_SELECTION_FAILURE = {}
import os
import sys
import time
from contextvars import ContextVar
import ssl
from ssl_context import create_ssl_context
import urllib.request
import urllib.error
import re
import unicodedata

from selection_contracts import (
    CandidateSet, DurationContract, SelectionCandidate, SelectionManifest,
    SelectionRequest, SelectionResult, SHORTAGE_GRACE_SECONDS,
)
from category_profiles import iter_vertical_profiles, resolve_vertical_profile
from candidate_quality import filter_candidate_clips, leading_fragment_trim


_ANALYSIS_METADATA_CONTEXT = ContextVar("liveclipper_analysis_metadata", default=None)


def _begin_analysis_metadata():
    global _LAST_CATEGORY_FILTER_SUMMARY, _LAST_FOCUS_SUMMARY, _LAST_TOPIC_COVERAGE_SUMMARY, _LAST_SELECTION_FAILURE
    metadata = {
        "category_summary": {},
        "preference_summary": {},
        "topic_coverage_summary": {},
        "selection_failure": {},
        "hook_candidate_summary": {},
        "trim_priorities": {},
        "final_target_duration": None,
        "duration_relaxation": {},
        "duration_contract": {},
        "candidate_contract": {},
        "selection_manifest": {},
        "selection_request": {},
        "selection_result": {},
        "source_contract": {},
        "content_review_summary": {},
    }
    _ANALYSIS_METADATA_CONTEXT.set(metadata)
    _LAST_CATEGORY_FILTER_SUMMARY = metadata["category_summary"]
    _LAST_FOCUS_SUMMARY = metadata["preference_summary"]
    _LAST_TOPIC_COVERAGE_SUMMARY = metadata["topic_coverage_summary"]
    _LAST_SELECTION_FAILURE = metadata["selection_failure"]
    return metadata


def _analysis_metadata_context():
    metadata = _ANALYSIS_METADATA_CONTEXT.get()
    if not isinstance(metadata, dict):
        metadata = _begin_analysis_metadata()
    return metadata


def _set_last_category_summary(summary):
    global _LAST_CATEGORY_FILTER_SUMMARY
    value = dict(summary or {})
    _LAST_CATEGORY_FILTER_SUMMARY = value
    _analysis_metadata_context()["category_summary"] = value
    return value


def _set_last_focus_summary(summary):
    global _LAST_FOCUS_SUMMARY
    value = dict(summary or {})
    _LAST_FOCUS_SUMMARY = value
    _analysis_metadata_context()["preference_summary"] = value
    return value


def _set_last_topic_coverage_summary(summary):
    global _LAST_TOPIC_COVERAGE_SUMMARY
    value = dict(summary or {})
    _LAST_TOPIC_COVERAGE_SUMMARY = value
    _analysis_metadata_context()["topic_coverage_summary"] = value
    return value


def _set_last_selection_failure(summary):
    global _LAST_SELECTION_FAILURE
    value = dict(summary or {})
    _LAST_SELECTION_FAILURE = value
    _analysis_metadata_context()["selection_failure"] = value
    return value


def get_last_analysis_metadata():
    metadata = _analysis_metadata_context()
    return {
        "category_summary": dict(metadata.get("category_summary") or {}),
        "preference_summary": dict(metadata.get("preference_summary") or {}),
        "topic_coverage_summary": dict(metadata.get("topic_coverage_summary") or {}),
        "selection_failure": dict(metadata.get("selection_failure") or {}),
        "hook_candidate_summary": dict(metadata.get("hook_candidate_summary") or {}),
        "final_target_duration": metadata.get("final_target_duration"),
        "duration_relaxation": dict(metadata.get("duration_relaxation") or {}),
        "duration_contract": dict(metadata.get("duration_contract") or {}),
        "candidate_contract": dict(metadata.get("candidate_contract") or {}),
        "selection_manifest": dict(metadata.get("selection_manifest") or {}),
        "selection_request": dict(metadata.get("selection_request") or {}),
        "selection_result": dict(metadata.get("selection_result") or {}),
        "source_contract": dict(metadata.get("source_contract") or {}),
        "content_review_summary": dict(metadata.get("content_review_summary") or {}),
    }


def selection_failure_message(metadata=None):
    failure = dict((metadata or get_last_analysis_metadata()).get("selection_failure") or {})
    if not failure:
        return "AI未返回合格片单，已停止，避免生成低质量预览或成片"
    reason = str(failure.get("reason") or "未通过最终质检").strip()
    candidate_count = int(failure.get("candidate_count") or 0)
    best_duration = float(failure.get("best_duration") or 0.0)
    duration_low = float(failure.get("duration_low") or 0.0)
    duration_high = float(failure.get("duration_high") or 0.0)
    if failure.get("code") == "insufficient_content":
        return (
            f"有效内容不足：可用候选{candidate_count}条，"
            f"最佳片单{best_duration:.1f}秒，目标至少{duration_low:.0f}秒"
        )
    if failure.get("code") == "ai_duration_contract_failed":
        return (
            f"AI未满足时长：可用候选{candidate_count}条，"
            f"最佳片单{best_duration:.1f}秒，目标至少{duration_low:.0f}秒"
        )
    if failure.get("code") == "duration_out_of_range":
        return (
            f"AI未满足时长：最佳片单{best_duration:.1f}秒，"
            f"要求{duration_low:.0f}-{duration_high:.0f}秒"
        )
    return f"AI片单质检未通过：{reason}"

from ai_model_config import (
    DEEPSEEK_DEFAULT_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    ai_chat_completions_url,
    normalize_ai_base_url,
    normalize_ai_model_defaults as _normalize_ai_model_defaults,
)

try:
    from tighten import ensure_sentence_complete, trim_repetitive_filler, trim_tail_filler
except Exception:
    def ensure_sentence_complete(clips, *_args, **_kwargs):
        return clips

    def trim_repetitive_filler(clips, *_args, **_kwargs):
        return clips

    def trim_tail_filler(clips, *_args, **_kwargs):
        return clips


# 多版本全量选片模式：设为True时跳过偏好限定
_skip_focus = False

# 进程内短期历史：只用于避开同一素材最近用过的片段，不保存完整选片结果，不落磁盘。
_RECENT_USED_CLIP_HISTORY = {}
_RECENT_HISTORY_MAX_SOURCES = 16
_RECENT_HISTORY_MAX_CLIPS = 80


def _clip_history_key(srt_text):
    normalized = re.sub(r"\s+", "", str(srt_text or ""))
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _clip_text_signature(text, limit=80):
    text = re.sub(r"\s+", "", str(text or ""))
    return text[:limit]


def _clip_text_similarity_value(left, right):
    left = re.sub(r"[\s\W_]+", "", str(left or ""))
    right = re.sub(r"[\s\W_]+", "", str(right or ""))
    if not left or not right:
        return 0.0
    left_set = set(left)
    right_set = set(right)
    return len(left_set & right_set) / max(len(left_set | right_set), 1)


def _clip_history_record(clip):
    ctype = str(clip[0] if len(clip) > 0 else "")
    text = str(clip[1] if len(clip) > 1 else "")
    try:
        start = float(clip[2])
        end = float(clip[3])
    except Exception:
        start, end = 0.0, 0.0
    try:
        block = _clip_focus_block(clip)
    except Exception:
        block = str(clip[6] if len(clip) > 6 else "") or "其他"
    return {
        "type": ctype,
        "start": start,
        "end": end,
        "text": _clip_text_signature(text),
        "block": block,
    }


def _get_recent_clip_history(history_key):
    if not history_key:
        return []
    return list(_RECENT_USED_CLIP_HISTORY.get(history_key, []))


def _remember_recent_clips(history_key, clips, log_fn=None):
    if not history_key or not clips:
        return
    existing = list(_RECENT_USED_CLIP_HISTORY.get(history_key, []))
    existing.extend(_clip_history_record(c) for c in clips if len(c) >= 4)
    _RECENT_USED_CLIP_HISTORY[history_key] = existing[-_RECENT_HISTORY_MAX_CLIPS:]
    while len(_RECENT_USED_CLIP_HISTORY) > _RECENT_HISTORY_MAX_SOURCES:
        oldest_key = next(iter(_RECENT_USED_CLIP_HISTORY))
        _RECENT_USED_CLIP_HISTORY.pop(oldest_key, None)
    if log_fn:
        log_fn(f"差异化历史: 已记录最近 {len(_RECENT_USED_CLIP_HISTORY[history_key])} 个片段时间段")


def _format_recent_history_hint(recent_items, limit=10):
    items = list(recent_items or [])[-limit:]
    if not items:
        return ""
    lines = []
    for item in items:
        ctype = str(item.get("type") or "clip")
        block = str(item.get("block") or "其他")
        text = str(item.get("text") or "")[:28]
        start = float(item.get("start") or 0)
        end = float(item.get("end") or 0)
        lines.append(f"- {ctype}/{block} {start:.1f}-{end:.1f}s: {text}")
    return (
        "★同一素材最近已用片段仅用于质量相近时避让。不得为了换Hook、换前三段或追求差异化，"
        "改选明显更空泛、不完整或证据更弱的内容；高质量候选不足时允许复用★\n"
        + "\n".join(lines)
    )


def _preview_feedback_log_path():
    try:
        from config import USER_DATA_DIR
        base_dir = USER_DATA_DIR
    except Exception:
        base_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LiveClipper")
    return os.path.join(base_dir, "ai_feedback", "preview_selection_feedback.jsonl")


def _load_preview_feedback_records(limit=80):
    path = _preview_feedback_log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
    except Exception:
        return []
    records = []
    for line in lines[-max(1, int(limit or 80)):]:
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


def _feedback_text_value(item, limit=46):
    if isinstance(item, dict):
        text = str(item.get("text") or "")
    else:
        text = str(item or "")
    text = re.sub(r"\[[vV]\d+\]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _feedback_unique_texts(items, limit=8):
    result = []
    seen = set()
    for item in items:
        text = _feedback_text_value(item)
        key = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


_FEEDBACK_POSITIVE_ROLES = {"hook_positive", "close_positive", "move_to_front", "move_to_end", "sentence_positive"}
_FEEDBACK_NEGATIVE_ROLES = {"hook_negative", "close_negative", "sentence_negative"}
_FEEDBACK_STRUCTURAL_AVOID = {"host_chatter", "environment_noise", "inventory_pressure", "filler_or_fragment"}
_FEEDBACK_SIGNAL_RULES = [
    ("color_benefit", "颜色/显白卖点", ["显白", "显肤", "肤亮", "颜色", "黑色", "白色", "绿色", "亮色", "米白", "饱和度", "冷白"]),
    ("fit_texture", "版型/质感卖点", ["显瘦", "质感", "面料", "版型", "细节", "袖子", "好穿", "舒服", "垂感", "高级"]),
    ("scene_styling", "场景/搭配表达", ["日常", "生活", "运动", "骑行", "拍照", "场景", "搭配", "出片", "穿搭", "黑白灰"]),
    ("objection_answer", "购买顾虑解释", ["不安心", "从来没有", "不敢", "不知道", "怕", "适合", "稳妥", "尝试", "口味", "惊喜"]),
    ("emotional_hook", "情绪/记忆点", ["相信我", "惊喜", "记忆点", "风格", "气质", "性格", "值得", "好看", "宝宝"]),
    ("host_chatter", "主播闲聊/自嗨", ["老粉", "拉黑", "划走", "催债", "催交", "不好意思", "听我讲话", "吹牛", "下次"]),
    ("environment_noise", "环境/直播间干扰", ["直播间", "手机屏幕", "肉眼", "窗户", "光很亮", "帘子", "走远", "颜色比较对"]),
    ("inventory_pressure", "库存/预售催促", ["首批", "拼手速", "没了", "预售", "库存", "加完", "备货", "一点都没有"]),
    ("filler_or_fragment", "口头禅/断句", ["来好了", "对然后", "能理解吗", "为什么", "呀对不对", "白开水", "因为我知道", "然后整个", "你看啊"]),
]


def _feedback_compact_text(text):
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(text or ""))


def _feedback_confidence(count):
    try:
        count = int(count or 0)
    except Exception:
        count = 0
    if count >= 8:
        return "较强"
    if count >= 5:
        return "明显"
    if count >= 3:
        return "轻微"
    if count >= 1:
        return "观察中"
    return "无"


def _feedback_signal_keys(text):
    text = str(text or "")
    compact = _feedback_compact_text(text)
    keys = []
    for key, _label, words in _FEEDBACK_SIGNAL_RULES:
        for word in words:
            if word in text or _feedback_compact_text(word) in compact:
                keys.append(key)
                break
    if len(compact) <= 5 and compact in {"对然后", "为什么", "来好了", "白开水", "能理解吗", "呀对不对"}:
        if "filler_or_fragment" not in keys:
            keys.append("filler_or_fragment")
    if re.search(r"(因为|然后|包括|或者|这个|整个|有一点|你看)$", text.strip()):
        if "filler_or_fragment" not in keys:
            keys.append("filler_or_fragment")
    return keys


def _build_feedback_signal_summary(scoped_records):
    signal_map = {
        key: {
            "key": key,
            "label": label,
            "positive_count": 0,
            "negative_count": 0,
            "positive_examples": [],
            "negative_examples": [],
        }
        for key, label, _words in _FEEDBACK_SIGNAL_RULES
    }
    text_roles = {}
    total = 0
    for record in scoped_records or []:
        roles = record.get("role_samples") if isinstance(record.get("role_samples"), dict) else {}
        role_values = {
            "hook_positive": roles.get("hook_positive") or [],
            "hook_negative": roles.get("hook_negative") or [],
            "close_positive": roles.get("close_positive") or [],
            "close_negative": roles.get("close_negative") or [],
            "move_to_front": roles.get("move_to_front") or [],
            "move_to_end": roles.get("move_to_end") or [],
            "sentence_positive": roles.get("sentence_positive") or record.get("kept_texts") or [],
            "sentence_negative": roles.get("sentence_negative") or record.get("rejected_segment_texts") or [],
        }
        for role, values in role_values.items():
            polarity = "positive" if role in _FEEDBACK_POSITIVE_ROLES else "negative" if role in _FEEDBACK_NEGATIVE_ROLES else ""
            if not polarity:
                continue
            for item in values or []:
                text = _feedback_text_value(item, limit=120)
                if not text:
                    continue
                total += 1
                compact = _feedback_compact_text(text)
                text_entry = text_roles.setdefault(compact, {"text": text, "positive": 0, "negative": 0})
                text_entry[polarity] += 1
                for key in _feedback_signal_keys(text):
                    signal = signal_map.get(key)
                    if not signal:
                        continue
                    count_key = "positive_count" if polarity == "positive" else "negative_count"
                    example_key = "positive_examples" if polarity == "positive" else "negative_examples"
                    signal[count_key] += 1
                    if text not in signal[example_key] and len(signal[example_key]) < 2:
                        signal[example_key].append(text)

    positive = []
    negative = []
    for signal in signal_map.values():
        pos = int(signal["positive_count"])
        neg = int(signal["negative_count"])
        if not pos and not neg:
            continue
        net = pos - neg
        item = dict(signal)
        item["net"] = net
        item["confidence"] = _feedback_confidence(abs(net))
        if signal["key"] in _FEEDBACK_STRUCTURAL_AVOID:
            if neg > 0:
                item["net"] = -neg
                item["confidence"] = _feedback_confidence(neg)
                negative.append(item)
            continue
        if net > 0:
            positive.append(item)
        elif net < 0:
            negative.append(item)

    positive.sort(key=lambda item: (int(item.get("net") or 0), int(item.get("positive_count") or 0)), reverse=True)
    negative.sort(key=lambda item: (abs(int(item.get("net") or 0)), int(item.get("negative_count") or 0)), reverse=True)
    conflicts = [
        item for item in text_roles.values()
        if int(item.get("positive") or 0) > 0 and int(item.get("negative") or 0) > 0
    ]
    conflicts.sort(key=lambda item: int(item.get("positive") or 0) + int(item.get("negative") or 0), reverse=True)
    return {
        "sample_count": total,
        "positive": positive[:5],
        "negative": negative[:5],
        "conflicts": conflicts[:5],
    }


def _build_preview_feedback_profile_from_records(records, scope=None, limit=12):
    records = list(records or [])
    if not records:
        return {
            "scope": scope,
            "kept": [],
            "rejected": [],
            "hook_positive": [],
            "hook_negative": [],
            "close_positive": [],
            "close_negative": [],
            "move_to_front": [],
            "move_to_end": [],
            "summary": {"sample_count": 0, "positive": [], "negative": [], "conflicts": []},
        }
    def _matches_scope(record):
        if not scope:
            return True
        record_scope = str(record.get("feedback_scope") or "").strip()
        if record_scope:
            return record_scope == scope
        if ":" in str(scope):
            return False
        return record.get("scope") == scope

    scoped_records = [item for item in records if _matches_scope(item)]
    if not scoped_records and scope and ":" not in str(scope):
        scoped_records = records
    kept_items = []
    rejected_items = []
    role_items = {
        "hook_positive": [],
        "hook_negative": [],
        "close_positive": [],
        "close_negative": [],
        "move_to_front": [],
        "move_to_end": [],
    }
    for record in reversed(scoped_records[-20:]):
        roles = record.get("role_samples") if isinstance(record.get("role_samples"), dict) else {}
        kept_items.extend(roles.get("sentence_positive") or record.get("kept_texts") or [])
        rejected_items.extend(roles.get("sentence_negative") or record.get("rejected_segment_texts") or [])
        for key in role_items:
            role_items[key].extend(roles.get(key) or [])
    return {
        "scope": scope,
        "kept": _feedback_unique_texts(kept_items, limit=limit),
        "rejected": _feedback_unique_texts(rejected_items, limit=limit),
        "hook_positive": _feedback_unique_texts(role_items["hook_positive"], limit=limit),
        "hook_negative": _feedback_unique_texts(role_items["hook_negative"], limit=limit),
        "close_positive": _feedback_unique_texts(role_items["close_positive"], limit=limit),
        "close_negative": _feedback_unique_texts(role_items["close_negative"], limit=limit),
        "move_to_front": _feedback_unique_texts(role_items["move_to_front"], limit=limit),
        "move_to_end": _feedback_unique_texts(role_items["move_to_end"], limit=limit),
        "summary": _build_feedback_signal_summary(scoped_records[-20:]),
    }


def _build_preview_feedback_profile(scope=None, limit=12):
    records = _load_preview_feedback_records(limit=80)
    return _build_preview_feedback_profile_from_records(records, scope=scope, limit=limit)


def _normalize_style_profile_strength(value):
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


def _feedback_sample_count(profile):
    try:
        summary = profile.get("summary") if isinstance(profile, dict) else {}
        return int((summary or {}).get("sample_count") or 0)
    except Exception:
        return 0


def _feedback_effective_strength(settings, profile):
    count = _feedback_sample_count(profile)
    configured = _normalize_style_profile_strength((settings or {}).get("style_profile_strength"))
    if configured == "off":
        return "off", configured, count
    if count < 3:
        return "readonly", configured, count
    if configured in {"light", "standard", "strong"}:
        return configured, configured, count
    if count < 10:
        return "light", configured, count
    return "standard", configured, count


def _feedback_strength_label(value):
    return {
        "off": "关闭",
        "readonly": "只读",
        "light": "轻度",
        "standard": "标准",
        "strong": "强",
        "auto": "自动",
    }.get(value, str(value or "自动"))


def _build_preview_feedback_hint_for_strength(profile, strength):
    """Turn every active strength into an AI-side soft preference."""
    mode = _normalize_style_profile_strength(strength)
    if mode not in {"light", "standard", "strong"}:
        return ""

    limit = {"light": 3, "standard": 6, "strong": 8}[mode]
    hint = _build_preview_feedback_hint_from_profile(profile, limit=limit)
    if not hint:
        return ""

    guidance = {
        "light": "画像影响强度=轻度：仅在候选内容质量相当时参考，不得因此删除完整片段、改变主题覆盖或打乱叙事主线。",
        "standard": "画像影响强度=标准：将画像作为重要软参考，但不得压过安全、语义完整、主题覆盖和自然叙事。",
        "strong": "画像影响强度=强：在满足安全、语义完整、主题覆盖和自然叙事的前提下，明显优先符合画像的内容类型。",
    }[mode]
    return f"{guidance}\n{hint}"


def _feedback_sample_signal_labels(samples, limit=4, include_structural=True):
    labels = {key: label for key, label, _words in _FEEDBACK_SIGNAL_RULES}
    counts = {}
    for sample in samples or []:
        text = _feedback_text_value(sample, limit=120)
        for key in _feedback_signal_keys(text):
            if not include_structural and key in _FEEDBACK_STRUCTURAL_AVOID:
                continue
            counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    return [labels.get(key, key) for key, _count in ranked[:limit]]


def _build_preview_feedback_hint_from_profile(profile, limit=8):
    profile = profile if isinstance(profile, dict) else {}
    kept = list(profile.get("kept") or [])[:limit]
    rejected = list(profile.get("rejected") or [])[:limit]
    hook_positive = list(profile.get("hook_positive") or [])[:4]
    hook_negative = list(profile.get("hook_negative") or [])[:4]
    close_positive = list(profile.get("close_positive") or [])[:4]
    close_negative = list(profile.get("close_negative") or [])[:4]
    move_to_front = list(profile.get("move_to_front") or [])[:4]
    move_to_end = list(profile.get("move_to_end") or [])[:4]
    summary = profile.get("summary") if isinstance(profile.get("summary"), dict) else {}
    positive_signals = list(summary.get("positive") or [])[:4]
    negative_signals = list(summary.get("negative") or [])[:4]
    conflicts = list(summary.get("conflicts") or [])[:3]
    if not any((kept, rejected, hook_positive, hook_negative, close_positive, close_negative, move_to_front, move_to_end, positive_signals, negative_signals, conflicts)):
        return ""

    lines = ["★剪辑风格画像软参考（来自用户最终成片前的勾选，不按原句硬匹配）:"]
    if positive_signals:
        parts = []
        for item in positive_signals:
            parts.append(f"{item.get('label')}({item.get('confidence', '观察中')}，保留{int(item.get('positive_count') or 0)}删{int(item.get('negative_count') or 0)})")
        lines.append("- 倾向保留的内容类型：" + "；".join(parts))
    if negative_signals:
        parts = []
        for item in negative_signals:
            parts.append(f"{item.get('label')}({item.get('confidence', '观察中')}，保留{int(item.get('positive_count') or 0)}删{int(item.get('negative_count') or 0)})")
        lines.append("- 谨慎避开的内容类型：" + "；".join(parts))
    if conflicts:
        lines.append(f"- 歧义样本：{len(conflicts)} 类句子正反都出现过，必须结合上下文，不要按原文硬判。")
    hook_positive_labels = _feedback_sample_signal_labels(hook_positive, include_structural=False)
    hook_negative_labels = _feedback_sample_signal_labels(hook_negative)
    close_positive_labels = _feedback_sample_signal_labels(close_positive, include_structural=False)
    close_negative_labels = _feedback_sample_signal_labels(close_negative)
    front_labels = _feedback_sample_signal_labels(move_to_front, include_structural=False)
    end_labels = _feedback_sample_signal_labels(move_to_end, include_structural=False)
    kept_labels = _feedback_sample_signal_labels(kept, include_structural=False)
    rejected_labels = _feedback_sample_signal_labels(rejected)
    if hook_positive_labels:
        lines.append("- 开头偏好类型：" + "；".join(hook_positive_labels))
    if hook_negative_labels:
        lines.append("- 开头慎用类型：" + "；".join(hook_negative_labels))
    if close_positive_labels:
        lines.append("- 结尾偏好类型：" + "；".join(close_positive_labels))
    if close_negative_labels:
        lines.append("- 结尾慎用类型：" + "；".join(close_negative_labels))
    if front_labels:
        lines.append("- 用户常拖到前面的类型：" + "；".join(front_labels))
    if end_labels:
        lines.append("- 用户常拖到最后的类型：" + "；".join(end_labels))
    if kept_labels and not positive_signals:
        lines.append("- 最近保留的共性类型：" + "；".join(kept_labels))
    if rejected_labels and not negative_signals:
        lines.append("- 最近删除的共性类型：" + "；".join(rejected_labels))
    lines.append("执行方式：同等质量时优先符合“倾向保留”的语义类型；遇到“谨慎避开”的闲聊、环境干扰、库存催促、碎句断句要降权。不要为了迎合偏好牺牲语义完整、主品类一致、目标时长和自然衔接。★")
    return "\n".join(lines)


def _build_preview_feedback_hint_from_records(records, scope=None, limit=8):
    profile = _build_preview_feedback_profile_from_records(records, scope=scope, limit=max(limit, 12))
    return _build_preview_feedback_hint_from_profile(profile, limit=limit)


def _build_preview_feedback_hint(scope=None, limit=8):
    profile = _build_preview_feedback_profile(scope=scope, limit=max(limit, 12))
    return _build_preview_feedback_hint_from_profile(profile, limit=limit)


def _feedback_similarity_value(left, right):
    left_key = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(left or ""))
    right_key = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(right or ""))
    if not left_key or not right_key:
        return 0.0
    shorter, longer = sorted((left_key, right_key), key=len)
    if len(shorter) >= 2 and shorter == longer:
        return 1.0
    if len(shorter) >= 4 and shorter in longer:
        return 0.94
    return _clip_text_similarity_value(left_key, right_key)


def _feedback_profile_samples(feedback_profile):
    profile = feedback_profile if isinstance(feedback_profile, dict) else {}
    rejected_samples = (
        list(profile.get("rejected") or [])
        + list(profile.get("hook_negative") or [])
        + list(profile.get("close_negative") or [])
    )
    kept_samples = (
        list(profile.get("kept") or [])
        + list(profile.get("hook_positive") or [])
        + list(profile.get("close_positive") or [])
        + list(profile.get("move_to_front") or [])
        + list(profile.get("move_to_end") or [])
    )
    return kept_samples, rejected_samples


def _feedback_best_similarity(text, samples):
    best_score = 0.0
    best_sample = ""
    for sample in samples or []:
        score = _feedback_similarity_value(text, sample)
        if score > best_score:
            best_score = score
            best_sample = sample
    return best_score, best_sample


def _feedback_signal_weight_map(feedback_profile):
    profile = feedback_profile if isinstance(feedback_profile, dict) else {}
    summary = profile.get("summary") if isinstance(profile.get("summary"), dict) else {}
    weights = {}

    def _net_value(item):
        try:
            return int(item.get("net"))
        except Exception:
            try:
                return int(item.get("positive_count") or 0) - int(item.get("negative_count") or 0)
            except Exception:
                return 0

    def _put(key, weight):
        if not key or not weight:
            return
        current = float(weights.get(key, 0.0))
        if abs(weight) > abs(current) or (weight < 0 and current > 0):
            weights[key] = float(weight)

    for item in summary.get("positive") or []:
        key = str(item.get("key") or "")
        net = max(0, _net_value(item))
        if net:
            _put(key, min(1.5, max(0.4, net / 16.0)))
    for item in summary.get("negative") or []:
        key = str(item.get("key") or "")
        net = abs(_net_value(item))
        if not net:
            continue
        divisor = 2.0 if key in _FEEDBACK_STRUCTURAL_AVOID else 12.0
        cap = 8.0 if key in _FEEDBACK_STRUCTURAL_AVOID else 1.5
        _put(key, -min(cap, max(1.0, net / divisor)))
    return weights


def _feedback_is_structural_avoid_text(text):
    return any(key in _FEEDBACK_STRUCTURAL_AVOID for key in _feedback_signal_keys(str(text or "")))


def _feedback_clip_preference_score(text, feedback_profile):
    """Positive means type-level style fit; negative means structural avoid."""
    if not feedback_profile:
        return 0.0
    text = str(text or "")
    _kept_samples, rejected_samples = _feedback_profile_samples(feedback_profile)
    reject_score, _reject_sample = _feedback_best_similarity(text, rejected_samples)
    score = 0.0
    if reject_score >= 0.90 and (
        _feedback_is_structural_avoid_text(text)
        or _feedback_is_structural_avoid_text(_reject_sample)
    ):
        score -= 5.0 + min(8.0, (reject_score - 0.90) * 60.0)

    signal_weights = _feedback_signal_weight_map(feedback_profile)
    for key in _feedback_signal_keys(text):
        score += float(signal_weights.get(key, 0.0))
    return score


def _clip_duration_value(clip):
    try:
        if len(clip) >= 6:
            return max(0.0, float(clip[5]))
        return max(0.0, float(clip[3]) - float(clip[2]))
    except Exception:
        return 0.0


def _is_hook_clip(clip):
    return "hook" in str(clip[0] if len(clip) else "").lower()


def _is_close_clip(clip):
    ctype = str(clip[0] if len(clip) else "").lower()
    return "close" in ctype or ctype in ("cta", "call_to_action", "urgency")


def _filter_preview_feedback_rejected_clips(
    clips,
    feedback_profile,
    log_fn=None,
    min_keep=4,
    min_duration=None,
    threshold=0.90,
):
    """Conservatively remove clips that match structural rejected patterns."""
    _kept_samples, rejected_samples = _feedback_profile_samples(feedback_profile)
    if not clips or not feedback_profile:
        return clips
    kept = list(clips)
    removed = []
    guarded = []

    def _log(msg):
        if log_fn:
            log_fn(msg)

    def _best_score(text, samples):
        return _feedback_best_similarity(text, samples)

    try:
        min_keep = max(1, int(min_keep or 1))
    except Exception:
        min_keep = 4
    try:
        min_duration = float(min_duration or 0)
    except Exception:
        min_duration = 0.0

    for clip in clips:
        text = str(clip[1] if len(clip) > 1 else "")
        reject_score, reject_sample = _best_score(text, rejected_samples)
        preference_score = _feedback_clip_preference_score(text, feedback_profile)
        reject_key = _feedback_compact_text(reject_sample)
        structural_reject = (
            reject_score >= threshold
            and len(reject_key) >= 2
            and (
                _feedback_is_structural_avoid_text(text)
                or _feedback_is_structural_avoid_text(reject_sample)
            )
        )
        semantic_reject = preference_score <= -6.0
        if not structural_reject and not semantic_reject:
            continue
        if clip not in kept:
            continue

        projected = [item for item in kept if item is not clip]
        if len(projected) < min_keep:
            guarded.append((clip, reject_score, "片段数保底"))
            continue
        if min_duration and sum(_clip_duration_value(item) for item in projected) < min_duration:
            guarded.append((clip, reject_score, "时长保底"))
            continue
        if _is_hook_clip(clip) and not any(_is_hook_clip(item) for item in projected):
            guarded.append((clip, reject_score, "Hook保底"))
            continue
        if _is_close_clip(clip) and not any(_is_close_clip(item) for item in projected):
            guarded.append((clip, reject_score, "Close保底"))
            continue

        kept = projected
        removed.append((clip, max(reject_score, min(0.99, abs(preference_score) / 10.0)), reject_sample or "结构性负向画像"))

    if removed:
        _log(f"人工偏好兜底: 已剔除 {len(removed)} 个结构性负向片段")
        for clip, score, sample in removed[:3]:
            try:
                _log(f"  喜好剔除: {float(clip[2]):.1f}-{float(clip[3]):.1f}s 相似{score:.0%} | {str(clip[1])[:28]} / 负样本:{sample[:18]}")
            except Exception:
                continue
    if guarded:
        _log(f"人工偏好兜底: 因结构/时长/喜好冲突保留 {len(guarded)} 个疑似负样本片段")
    return kept if kept else clips


def _recent_filter_floor(target_duration):
    try:
        target = int(target_duration or 60)
    except Exception:
        target = 60
    if target <= 40:
        return 5, max(22, target * 0.70)
    if target >= 100:
        return 14, target * 0.70
    if target >= 80:
        return 12, target * 0.72
    return 8, max(38, target * 0.75)


def _filter_recent_similar_clips(clips, recent_items, log_fn=None, min_keep=4, threshold=0.62, min_duration=None, protect_structure=True):
    if not clips or not recent_items:
        return clips
    kept = list(clips)
    candidates = []
    guarded = []
    recent = list(recent_items or [])
    for clip in clips:
        ctype = str(clip[0] if len(clip) > 0 else "").lower()
        text = str(clip[1] if len(clip) > 1 else "")
        try:
            start = float(clip[2])
            end = float(clip[3])
        except Exception:
            start, end = 0.0, 0.0
        for item in recent:
            rec = item if isinstance(item, dict) else _clip_history_record(item)
            overlap = max(start, float(rec.get("start") or 0)) < min(end, float(rec.get("end") or 0))
            sim = _clip_text_similarity_value(text, rec.get("text") or "")
            if overlap or sim >= threshold:
                candidates.append((clip, "时间重复" if overlap else f"文案相似{sim:.0%}"))
                break

    removed = []
    min_keep = min(max(1, int(min_keep or 1)), len(clips))
    min_duration = float(min_duration or 0)
    had_hook = any(_is_hook_clip(c) for c in clips)
    had_close = any(_is_close_clip(c) for c in clips)
    for clip, reason in candidates:
        if clip not in kept:
            continue
        projected = [c for c in kept if c is not clip]
        if len(projected) < min_keep:
            guarded.append((clip, "片段数保底"))
            continue
        if min_duration and sum(_clip_duration_value(c) for c in projected) < min_duration:
            guarded.append((clip, "时长保底"))
            continue
        if protect_structure:
            if had_hook and _is_hook_clip(clip) and not any(_is_hook_clip(c) for c in projected):
                guarded.append((clip, "Hook结构保底"))
                continue
            if had_close and _is_close_clip(clip) and not any(_is_close_clip(c) for c in projected):
                guarded.append((clip, "Close结构保底"))
                continue
        kept = projected
        removed.append((clip, reason))

    if (removed or guarded) and log_fn:
        if removed:
            log_fn(f"差异化历史: 避开 {len(removed)} 个最近用过/高度相似片段")
        if guarded:
            log_fn(f"差异化历史: 因结构/时长保底保留 {len(guarded)} 个相似片段")
        for clip, reason in removed[:3]:
            log_fn(f"  历史避让: [{clip[0]}] {float(clip[2]):.1f}-{float(clip[3]):.1f}s {reason} {str(clip[1])[:24]}...")
        for clip, reason in guarded[:3]:
            log_fn(f"  历史保留: [{clip[0]}] {float(clip[2]):.1f}-{float(clip[3]):.1f}s {reason} {str(clip[1])[:24]}...")
    return kept if kept else clips


def _summarize_clips_for_diversity(clips, limit=8):
    if not clips:
        return ""
    parts = []
    for clip in clips[:limit]:
        rec = _clip_history_record(clip)
        parts.append(f"{rec['type']}/{rec['block']} {rec['start']:.1f}-{rec['end']:.1f}s {rec['text'][:22]}")
    return "；".join(parts)


def _clip_overlaps_any(clip, others, min_overlap=0.1):
    try:
        start = float(clip[2])
        end = float(clip[3])
    except Exception:
        return False
    for other in others or []:
        try:
            os_ = float(other[2])
            oe = float(other[3])
        except Exception:
            continue
        if max(start, os_) < min(end, oe) - float(min_overlap):
            return True
    return False


def _retag_clip_type(clip, clip_type):
    values = list(clip)
    if not values:
        return clip
    values[0] = clip_type
    return tuple(values)


def _multi_version_total_duration(clips):
    return sum(_clip_duration_value(c) for c in clips or [])


def _multi_version_target_bounds(target_duration, duration_tolerance=None):
    try:
        target = int(target_duration or 60)
    except Exception:
        target = 60
    try:
        tolerance = (
            max(0.0, float(duration_tolerance))
            if duration_tolerance is not None
            else max(5, target // 6)
        )
    except (TypeError, ValueError):
        tolerance = max(5, target // 6)
    lower_floor = 1 if duration_tolerance is not None else 5 if target <= 20 else 12 if target <= 30 else 25
    return max(lower_floor, target - tolerance), target + tolerance


def _coerce_duration_contract(duration_contract=None, target_duration=60, final_target_duration=None):
    try:
        source_target = max(0.001, float(target_duration or 60))
    except Exception:
        source_target = 60.0
    try:
        final_target = max(0.001, float(final_target_duration or source_target))
    except Exception:
        final_target = source_target
    inferred_speed = source_target / final_target if final_target > 0 else 1.0
    return DurationContract.coerce(
        duration_contract,
        final_target=final_target,
        speed_factor=inferred_speed,
    )


def _duration_source_bounds(target_duration, duration_contract=None):
    if duration_contract is None:
        return _multi_version_target_bounds(target_duration)
    contract = DurationContract.coerce(
        duration_contract,
        final_target=target_duration,
        speed_factor=1.0,
    )
    return contract.source_min, contract.source_max


def _target_clip_count_range(target_duration):
    try:
        target = int(target_duration or 60)
    except Exception:
        target = 60
    if target <= 20:
        return 3, 5
    if target <= 30:
        return 5, 8
    if target <= 40:
        return 6, 10
    if target <= 60:
        return 9, 14
    if target <= 90:
        return 13, 20
    if target <= 130:
        return 18, 26
    min_count = max(18, (target + 6) // 7)
    max_count = max(min_count + 4, (target + 4) // 5)
    return min_count, max_count


def target_clip_count_text(target_duration):
    low, high = _target_clip_count_range(target_duration)
    return f"{low}-{high}"


def _target_duration_rule_text(target_duration, duration_contract=None):
    low, high = _duration_source_bounds(target_duration, duration_contract)
    return (
        f"总时长必须控制在{low:.0f}-{high:.0f}秒；"
        f"目标是{int(target_duration or 60)}秒左右，超过{high:.0f}秒必须删除低信息/重复片段，"
        "不要为了凑片段数牺牲时长。"
    )


def _target_supplement_cap(target_duration):
    try:
        target = int(target_duration or 60)
    except Exception:
        target = 60
    if target >= 100:
        return 12
    if target >= 80:
        return 10
    if target >= 50:
        return 6
    return 4


def _preference_target_bounds(target_duration, requested="自动"):
    """Return the preferred-topic Product range; preference is an anchor, not exclusivity."""
    try:
        target = int(target_duration or 60)
    except Exception:
        target = 60
    requested = str(requested or "自动").strip().lower()
    manual = requested not in {"", "自动", "auto", "默认", "随机偏好", "全量选片"}
    if target >= 100:
        return (4, 6 if manual else 5)
    if target >= 80:
        return (3, 6 if manual else 4)
    if target >= 50:
        return (3, 4) if manual else (2, 3)
    return (2, 3) if manual else (1, 2)


def _preferred_focus_quota(target_duration):
    requested = get_last_analysis_metadata()["preference_summary"].get("requested", "自动")
    return _preference_target_bounds(target_duration, requested)[0]


_TOPIC_EVIDENCE_KEYWORDS = {
    "版型显瘦": (
        "显瘦", "遮肉", "藏肉", "收腰", "显高", "显腿长", "比例", "版型", "廓形",
        "剪裁", "修身", "宽松", "遮胯", "遮肚", "肩宽", "胯宽", "小个子", "梨形",
        "拜拜肉", "藏掉", "盖臀", "盖胯", "肩往里挖", "肩膀往里", "肩更窄", "更窄更瘦",
    ),
    "面料质感": (
        "面料", "材质", "莱赛尔", "天丝", "氨纶", "弹力", "聚酯纤维", "纯棉", "棉麻",
        "针织", "冰丝", "真丝", "垂感", "垂坠", "高织", "薄纱", "网纱", "薄如纱", "克重",
        "纱线", "竹节麻", "再生纤维", "木浆", "透气", "清爽", "凉快", "不扎", "粘肤",
        "粘汗", "缩水", "起球",
    ),
    "穿着体验": (
        "舒服", "舒适", "亲肤", "柔软", "冰凉", "凉感", "裸肤", "裸感", "透气", "不闷",
        "不热", "不勒", "不卡", "不紧绷", "轻盈", "自在", "不透", "不用担心透", "活动方便",
    ),
    "品质细节": (
        "品质", "质感", "做工", "走线", "高级感", "精致", "质检", "质检报告", "不起球",
        "不褪色", "不变形", "色牢度",
    ),
    "颜色氛围": (
        "颜色", "色系", "显白", "提亮", "气色", "肤色", "黄皮", "黑皮", "绿色", "白色",
        "黑色", "藏青", "藏蓝", "亮色", "彩色", "米白", "冷白", "复古色", "氛围感",
    ),
    "场景搭配": (
        "通勤", "上班", "约会", "日常", "出门", "旅游", "度假", "放假", "聚会", "职场", "搭配",
        "内搭", "外穿", "单穿", "叠穿", "百搭", "拍照", "出片", "草帽", "棒球帽", "黑白灰",
    ),
    "尺寸长度": (
        "衣长", "袖长", "长度", "短款", "中长款", "盖住", "遮住", "到脚踝", "九分", "七分",
    ),
    "工艺细节": (
        "工艺", "拼接", "包边", "锁边", "加固", "扣子", "纽扣", "亨利扣", "领口", "U领",
        "圆领", "V领", "口袋", "里衬", "定染", "固色",
    ),
    "对比优势": (
        "买不到", "外面没有", "不一样", "独特", "独家", "全网无同款", "比外面", "比市面",
        "同品质", "没有第二家", "原创",
    ),
    "口感食欲": ("好吃", "鲜甜", "脆甜", "爆汁", "多汁", "口感", "鲜嫩", "软糯", "酥脆", "Q弹", "试吃"),
    "新鲜品质": ("新鲜", "鲜活", "现摘", "现采", "现捕", "当天发", "鲜度", "饱满", "坏果包赔"),
    "产地溯源": ("产地", "原产地", "源头", "基地", "果园", "农场", "直采", "溯源", "产区"),
    "规格分量": ("规格", "净含量", "净重", "重量", "斤装", "箱装", "袋装", "盒装", "果径", "分量"),
    "发货保鲜": ("发货", "现发", "冷链", "冰袋", "保温箱", "保鲜", "锁鲜", "冷冻", "冷藏"),
    "场景吃法": ("早餐", "夜宵", "下午茶", "办公室", "全家", "聚餐", "煲汤", "下饭", "即食", "囤货", "送礼"),
}

_TOPIC_PRIORITY = (
    "版型显瘦", "面料质感", "穿着体验", "品质细节", "颜色氛围", "场景搭配",
    "尺寸长度", "工艺细节", "对比优势", "口感食欲", "新鲜品质", "产地溯源",
    "规格分量", "发货保鲜", "场景吃法",
)


def _topic_clip_text(clip):
    if isinstance(clip, dict):
        return str(clip.get("text") or "")
    if isinstance(clip, (list, tuple)) and len(clip) > 1:
        return str(clip[1] or "")
    return str(clip or "")


def _topic_clip_type(clip):
    if isinstance(clip, dict):
        return str(clip.get("clip_type") or clip.get("type") or "product").lower()
    if isinstance(clip, (list, tuple)) and clip:
        return str(clip[0] or "product").lower()
    return "product"


def _topic_clip_duration(clip):
    try:
        if isinstance(clip, dict):
            return float(clip.get("duration") or float(clip.get("end") or 0) - float(clip.get("start") or 0))
        return float(clip[5] if len(clip) > 5 else float(clip[3]) - float(clip[2]))
    except Exception:
        return 0.0


def _topic_evidence_scores(value):
    text = re.sub(r"\[[vV]\d+\]\s*", "", _topic_clip_text(value))
    compact = re.sub(r"\s+", "", text).lower()
    scores = {}
    for topic, words in _TOPIC_EVIDENCE_KEYWORDS.items():
        topic_text = compact.replace("显高级", "") if topic == "版型显瘦" else compact
        score = 0.0
        hits = 0
        for word in words:
            count = topic_text.count(str(word).lower())
            if count:
                hits += 1
                score += count * (1.4 if len(str(word)) >= 3 else 1.0)
        if score:
            scores[topic] = round(score + min(2.0, hits * 0.2), 3)
    return scores


def _clip_primary_topic(clip):
    scores = _topic_evidence_scores(clip)
    if not scores:
        return "其他"
    rank = {name: index for index, name in enumerate(_TOPIC_PRIORITY)}
    return max(scores, key=lambda topic: (scores[topic], -rank.get(topic, 999)))


def _topic_min_distinct(product_count):
    if product_count >= 5:
        return 3
    if product_count >= 3:
        return 2
    return 1


def _topic_coverage_summary(clips, preferred_focus="", target_duration=None, requested=None):
    preferred_topic = _focus_label_to_block(preferred_focus) or str(preferred_focus or "").strip()
    products = [
        clip for clip in clips or []
        if "hook" not in _topic_clip_type(clip)
        and "close" not in _topic_clip_type(clip)
        and _topic_clip_type(clip) != "call_to_action"
    ]
    counts = {}
    durations = {}
    for clip in products:
        topic = _clip_primary_topic(clip)
        counts[topic] = counts.get(topic, 0) + 1
        durations[topic] = durations.get(topic, 0.0) + _topic_clip_duration(clip)
    requested = requested if requested is not None else get_last_analysis_metadata()["preference_summary"].get("requested", "自动")
    pref_min, pref_max = _preference_target_bounds(target_duration or _AI_TARGET_DURATION, requested)
    product_count = len(products)
    preference_count = int(counts.get(preferred_topic, 0)) if preferred_topic else 0
    total_duration = sum(durations.values())
    preference_duration = float(durations.get(preferred_topic, 0.0)) if preferred_topic else 0.0
    preference_ratio = preference_count / product_count if product_count else 0.0
    preference_duration_ratio = preference_duration / total_duration if total_duration > 0 else 0.0
    distinct_topics = [topic for topic, count in counts.items() if topic != "其他" and count > 0]
    min_distinct = _topic_min_distinct(product_count)
    manual = str(requested or "自动").strip().lower() not in {"", "自动", "auto", "默认", "随机偏好", "全量选片"}
    max_duration_ratio = 0.65 if manual else 0.55
    overconcentrated = bool(
        preferred_topic
        and (preference_count > pref_max or preference_duration_ratio > max_duration_ratio)
    )
    underpreferred = bool(preferred_topic and product_count and preference_count < min(pref_min, product_count))
    undercovered = len(distinct_topics) < min_distinct
    return {
        "preferred_topic": preferred_topic,
        "requested": str(requested or "自动"),
        "preference_min": pref_min,
        "preference_max": pref_max,
        "preference_count": preference_count,
        "product_count": product_count,
        "preference_ratio": round(preference_ratio, 4),
        "preference_duration_ratio": round(preference_duration_ratio, 4),
        "max_duration_ratio": max_duration_ratio,
        "topic_counts": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
        "topic_durations": {key: round(value, 3) for key, value in durations.items()},
        "distinct_topics": distinct_topics,
        "distinct_count": len(distinct_topics),
        "min_distinct": min_distinct,
        "overconcentrated": overconcentrated,
        "underpreferred": underpreferred,
        "undercovered": undercovered,
        "balanced": not overconcentrated and not underpreferred and not undercovered,
    }


def _available_topic_support(srt_entries, preferred_focus=""):
    preferred_topic = _focus_label_to_block(preferred_focus) or str(preferred_focus or "").strip()
    support = {}
    unsafe = re.compile(
        r"\d{2,4}\s*[元块]|价格|原价|现价|到手价|福利价|优惠|折扣|领券|满减|"
        r"321|三二一|链接|连结|小黄车|购物车|下单|去拍|点关注"
    )
    for start, end, text in srt_entries or []:
        try:
            duration = float(end) - float(start)
        except Exception:
            continue
        compact = re.sub(r"\s+", "", str(text or ""))
        if duration < 1.8 or duration > 12.0 or len(compact) < 7 or unsafe.search(compact):
            continue
        topic = _clip_primary_topic(("product", text, start, end, 0, duration, ""))
        if topic == "其他" or topic == preferred_topic:
            continue
        support[topic] = support.get(topic, 0) + 1
    return support


def _topic_candidates_from_srt_entries(srt_entries, targets, limit_per_topic=2):
    """Build deterministic clean candidates so the diversity gate also works in fallback paths."""
    quotas = {topic: max(1, min(limit_per_topic, targets.count(topic))) for topic in set(targets or [])}
    if not quotas:
        return []
    unsafe = re.compile(
        r"\d{2,4}\s*[元块]|价格|原价|现价|到手价|福利价|优惠|折扣|领券|满减|"
        r"321|三二一|链接|连结|小黄车|购物车|下单|去拍|点关注"
    )
    host_noise = re.compile(r"头发打理|打理教程|提醒我|催债|催追|催视频|欢迎|评论区|公屏|后台")
    ranked = {topic: [] for topic in quotas}
    for start, end, text in srt_entries or []:
        try:
            start = float(start)
            end = float(end)
        except Exception:
            continue
        duration = end - start
        compact = re.sub(r"\s+", "", str(text or ""))
        if duration < 1.8 or duration > 10.0 or len(compact) < 8 or unsafe.search(compact) or host_noise.search(compact):
            continue
        candidate = ("product", str(text or "").strip(), start, end, 42.0, duration, "")
        topic = _clip_primary_topic(candidate)
        if topic not in quotas:
            continue
        candidate = ("product", candidate[1], start, end, 42.0, duration, topic)
        if _clip_boundary_quality_flags(candidate):
            continue
        evidence = _topic_evidence_scores(candidate).get(topic, 0.0)
        quality = evidence * 5.0 + min(14.0, len(compact) / 3.0) + min(10.0, duration)
        if any(word in compact for word in ("92%", "莱赛尔", "天丝", "氨纶", "冰凉", "裸肤", "舒服", "高织", "不透", "质检报告")):
            quality += 8.0
        ranked[topic].append((quality, candidate))
    result = []
    for topic in targets or []:
        if quotas.get(topic, 0) <= 0:
            continue
        options = sorted(ranked.get(topic, []), key=lambda item: item[0], reverse=True)
        for _, candidate in options:
            if any(abs(float(candidate[2]) - float(old[2])) < 0.15 for old in result):
                continue
            result.append(candidate)
            quotas[topic] -= 1
            break
    return result


def _coverage_target_topics(clips, srt_entries, preferred_focus="", target_duration=None):
    summary = _topic_coverage_summary(clips, preferred_focus, target_duration)
    support = _available_topic_support(srt_entries, preferred_focus)
    counts = summary.get("topic_counts", {})
    candidates = sorted(
        support,
        key=lambda topic: (counts.get(topic, 0), -support.get(topic, 0), _TOPIC_PRIORITY.index(topic) if topic in _TOPIC_PRIORITY else 999),
    )
    required = max(
        0,
        int(summary.get("preference_count", 0)) - int(summary.get("preference_max", 0)),
        int(summary.get("min_distinct", 0)) - int(summary.get("distinct_count", 0)),
    )
    if summary.get("preference_duration_ratio", 0) > summary.get("max_duration_ratio", 1):
        remaining_preference = int(summary.get("preference_count", 0)) - required
        if remaining_preference > int(summary.get("preference_min", 0)):
            required += 1
        elif not required:
            required = 1
    targets = []
    while candidates and len(targets) < min(4, max(1, required)):
        made_progress = False
        for topic in candidates:
            if targets.count(topic) >= 2:
                continue
            targets.append(topic)
            made_progress = True
            if len(targets) >= min(4, max(1, required)):
                break
        if not made_progress:
            break
    return targets, support, summary


def _replace_for_topic_coverage(clips, candidates, preferred_focus, target_duration, log_fn=None):
    result = list(clips or [])
    preferred_topic = _focus_label_to_block(preferred_focus) or str(preferred_focus or "").strip()
    used_ranges = []
    for clip in result:
        try:
            used_ranges.append((float(clip[2]), float(clip[3]), _topic_clip_text(clip)))
        except Exception:
            pass
    replaced = 0
    for candidate in candidates or []:
        candidate = _retag_clip_type(candidate, "product")
        candidate_topic = _clip_primary_topic(candidate)
        if candidate_topic in {"", "其他", preferred_topic}:
            continue
        try:
            cs, ce = float(candidate[2]), float(candidate[3])
        except Exception:
            continue
        if _topic_clip_duration(candidate) < 1.8 or _topic_clip_duration(candidate) > 12.0:
            continue
        if any(cs < end and ce > start for start, end, _ in used_ranges):
            continue
        current = _topic_coverage_summary(result, preferred_focus, target_duration)
        if current.get("balanced"):
            break
        counts = current.get("topic_counts", {})
        pref_min = int(current.get("preference_min", 0))
        removable = []
        for index, clip in enumerate(result):
            ctype = _topic_clip_type(clip)
            if "hook" in ctype or "close" in ctype or ctype == "call_to_action":
                continue
            topic = _clip_primary_topic(clip)
            if topic == candidate_topic:
                continue
            if topic == preferred_topic and counts.get(topic, 0) <= pref_min:
                continue
            if topic != preferred_topic and counts.get(topic, 0) <= 1 and current.get("distinct_count", 0) <= current.get("min_distinct", 0):
                continue
            removable.append((index, clip, topic))
        if not removable:
            try:
                _, target_high = _multi_version_target_bounds(target_duration or _AI_TARGET_DURATION)
            except Exception:
                target_high = float(target_duration or 60) + 10.0
            current_total = sum(_topic_clip_duration(item) for item in result)
            if (
                current.get("overconcentrated")
                and current.get("preference_count", 0) <= current.get("preference_min", 0)
                and current_total + _topic_clip_duration(candidate) <= target_high + 0.5
            ):
                insert_at = next(
                    (idx for idx, item in enumerate(result) if "close" in _topic_clip_type(item) or _topic_clip_type(item) == "call_to_action"),
                    len(result),
                )
                result.insert(insert_at, candidate)
                used_ranges.append((cs, ce, _topic_clip_text(candidate)))
                replaced += 1
                if log_fn:
                    log_fn(f"主题覆盖补入: {candidate_topic} ({_topic_clip_text(candidate)[:22]})")
            continue
        preferred_removable = [item for item in removable if item[2] == preferred_topic]
        pool = preferred_removable if preferred_removable and (
            current.get("overconcentrated") or current.get("preference_count", 0) > current.get("preference_max", 0)
        ) else removable
        candidate_duration = _topic_clip_duration(candidate)
        remove_index, removed_clip, removed_topic = min(
            pool,
            key=lambda item: (abs(_topic_clip_duration(item[1]) - candidate_duration), -_topic_clip_duration(item[1])),
        )
        result[remove_index] = candidate
        used_ranges.append((cs, ce, _topic_clip_text(candidate)))
        replaced += 1
        if log_fn:
            log_fn(
                f"主题覆盖替换: {removed_topic} → {candidate_topic} "
                f"({_topic_clip_text(candidate)[:22]})"
            )
    return result, replaced


def _preference_quota_supported(preferred_focus):
    return str(preferred_focus or "").strip() in {
        "版型显瘦", "颜色氛围", "场景搭配", "面料质感", "穿着体验",
        "品质细节", "尺寸长度", "工艺细节", "口感食欲", "新鲜品质",
        "产地溯源", "规格分量", "发货保鲜", "场景吃法",
    }


def _preferred_focus_clip_count(clips, preferred_focus):
    preferred_block = _focus_label_to_block(preferred_focus)
    if not preferred_block or preferred_block == "其他":
        return 0
    return sum(
        1
        for clip in clips or []
        if not _is_hook_clip(clip)
        and not _is_close_clip(clip)
        and _clip_primary_topic(clip) == preferred_block
    )


def _append_unique_supplement_clips(clips, supplement, target_duration, limit=None):
    if not supplement:
        return 0
    try:
        _, target_high = _multi_version_target_bounds(target_duration)
    except Exception:
        target_high = float(target_duration or 60) + 10

    existing_ranges = []
    existing_times = set()
    existing_texts = []
    for clip in clips:
        try:
            start = float(clip[2])
            end = float(clip[3])
        except Exception:
            continue
        existing_ranges.append((start, end))
        existing_times.add((round(start, 2), round(end, 2)))
        text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(clip[1] if len(clip) > 1 else ""))
        if text:
            existing_texts.append(text)

    added = 0
    limit = int(limit or len(supplement))
    for sc in supplement:
        if len(sc) < 4:
            continue
        try:
            start = float(sc[2])
            end = float(sc[3])
        except Exception:
            continue
        if end <= start:
            continue
        key = (round(start, 2), round(end, 2))
        if key in existing_times:
            continue
        if any(max(start, s) < min(end, e) - 0.12 for s, e in existing_ranges):
            continue
        cand_text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(sc[1] if len(sc) > 1 else ""))
        duplicate_text = False
        if len(cand_text) >= 8:
            for old_text in existing_texts:
                shorter, longer = sorted((cand_text, old_text), key=len)
                if len(shorter) >= 10 and shorter in longer:
                    duplicate_text = True
                    break
                if min(len(cand_text), len(old_text)) >= 12 and _clip_text_similarity_value(cand_text, old_text) >= 0.78:
                    duplicate_text = True
                    break
        if duplicate_text:
            continue
        next_total = sum(_clip_duration_value(c) for c in clips) + _clip_duration_value(sc)
        if next_total > target_high + 0.1:
            continue
        clips.append(sc)
        existing_ranges.append((start, end))
        existing_times.add(key)
        if cand_text:
            existing_texts.append(cand_text)
        added += 1
        if added >= limit:
            break
    return added


def _enforce_target_duration_limit(clips, target_duration, log_fn=None, label="目标时长", feedback_profile=None):
    """Trim whole low-value clips when the final AI list exceeds the requested duration."""
    if not clips:
        return clips

    def _log(msg):
        if log_fn:
            log_fn(msg)

    try:
        target = int(target_duration or 60)
        low, high = _multi_version_target_bounds(target)
    except Exception:
        return clips

    kept = list(clips)

    def _total(items):
        return sum(_clip_duration_value(c) for c in items or [])

    total = _total(kept)
    if total <= high + 0.1:
        return kept

    min_keep = 3 if target <= 30 else 5 if target <= 60 else 6
    removed = []

    def _clean_text(value):
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(value or ""))

    def _type_counts(items):
        hooks = sum(1 for c in items if _is_hook_clip(c))
        closes = sum(1 for c in items if _is_close_clip(c))
        return hooks, closes

    def _focus_counts(items):
        counts = {}
        for clip in items:
            try:
                block = _clip_focus_block(clip)
            except Exception:
                block = str(clip[6] if len(clip) > 6 else "")
            counts[block] = counts.get(block, 0) + 1
        return counts

    while _total(kept) > high + 0.1 and len(kept) > min_keep:
        current_total = _total(kept)
        hooks, closes = _type_counts(kept)
        focus_counts = _focus_counts(kept)
        preferred_topic = _current_focus_used_label()
        current_topic_summary = _topic_coverage_summary(kept, preferred_topic, target)
        primary_topic_counts = current_topic_summary.get("topic_counts", {})
        candidates = []
        over_by = current_total - high
        for idx, clip in enumerate(kept):
            dur = _clip_duration_value(clip)
            if dur <= 0:
                continue
            is_hook = _is_hook_clip(clip)
            is_close = _is_close_clip(clip)
            if is_hook and hooks <= 1:
                continue
            if is_close and closes <= 1 and len(kept) <= min_keep + 1:
                continue
            projected = current_total - dur
            projected_items = kept[:idx] + kept[idx + 1:]
            projected_topic_summary = _topic_coverage_summary(projected_items, preferred_topic, target)
            primary_topic = _clip_primary_topic(clip)
            if projected_topic_summary.get("underpreferred"):
                continue
            if (
                primary_topic not in {"其他", preferred_topic}
                and primary_topic_counts.get(primary_topic, 0) <= 1
                and projected_topic_summary.get("distinct_count", 0) < current_topic_summary.get("distinct_count", 0)
            ):
                continue
            if current_topic_summary.get("balanced") and not projected_topic_summary.get("balanced"):
                continue
            text = _clean_text(clip[1] if len(clip) > 1 else "")
            try:
                block = _clip_focus_block(clip)
            except Exception:
                block = str(clip[6] if len(clip) > 6 else "")
            score = 0.0
            reason = "低信息"
            if not is_hook and not is_close:
                score += 80
                reason = "卖点冗余"
            elif is_close and closes > 1:
                score += 45
                reason = "重复收尾"
            elif is_hook and hooks > 1:
                score += 30
                reason = "重复Hook"
            if focus_counts.get(block, 0) > 2:
                score += 45
                reason = f"重复{block or '卖点'}"
            if current_topic_summary.get("overconcentrated") and primary_topic == preferred_topic:
                score += 70
                reason = f"偏好主题过量:{preferred_topic}"
            elif primary_topic not in {"其他", preferred_topic} and primary_topic_counts.get(primary_topic, 0) <= 1:
                score -= 65
            if dur >= over_by:
                score += 20
            if dur >= 8:
                score += min(30, dur)
                reason = f"长段{block or reason}"
            weak_prefixes = ("是的", "好的", "嗯", "啊", "然后")
            weak_exact = {"对", "是的", "好的", "嗯", "嗯嗯", "啊", "好"}
            if text in weak_exact or text.startswith(weak_prefixes):
                score += 25
                reason = "短废话/承接句"
            feedback_score = _feedback_clip_preference_score(clip[1] if len(clip) > 1 else "", feedback_profile) if feedback_profile else 0.0
            if feedback_score:
                if feedback_score > 0:
                    feedback_adjust = max(-4.0, -feedback_score * 1.5)
                else:
                    feedback_adjust = min(45.0, -feedback_score * 6.0)
                score += feedback_adjust
                if feedback_adjust >= 12:
                    reason = "画像低分"
            if projected < low:
                score -= (low - projected) * 8
            score -= abs(projected - target) * 0.25
            candidates.append((score, dur, idx, reason))

        candidates = [item for item in candidates if item[0] > 0]
        if not candidates:
            break
        score, dur, idx, reason = max(candidates, key=lambda item: (item[0], item[1]))
        clip = kept.pop(idx)
        removed.append((clip, reason))

    after = _total(kept)
    if removed:
        _log(f"{label}: 超出目标上限，删除 {len(removed)} 段整句片段，{total:.1f}s -> {after:.1f}s (目标{low:.0f}-{high:.0f}s)")
        for clip, reason in removed[:5]:
            try:
                _log(f"  时长收口: {reason} [{float(clip[2]):.1f}-{float(clip[3]):.1f}] {str(clip[1])[:24]}")
            except Exception:
                pass
    elif after > high + 0.1:
        _log(f"{label}: {after:.1f}s 仍高于目标上限{high:.0f}s，因结构保护未继续删除")
    return kept


def _multi_version_type_counts(clips):
    hooks = sum(1 for c in clips or [] if _is_hook_clip(c))
    closes = sum(1 for c in clips or [] if _is_close_clip(c))
    products = max(0, len(clips or []) - hooks - closes)
    return hooks, products, closes


def _score_hook_text_candidate(text, duration, hook_keywords=None, focus_hint=None, ai_controls=None):
    """Score a SRT entry as an opening hook candidate."""
    txt = re.sub(r"\s+", "", str(text or ""))
    if not txt:
        return 0.0, []
    if _is_bad_hook_candidate_text(txt):
        return 0.0, []
    try:
        dur = float(duration)
    except Exception:
        dur = 0.0
    if dur < 1.2 or dur > 8.5:
        return 0.0, []

    hook_keywords = hook_keywords or []
    strong_words = [
        "绝了", "太漂亮", "不敢信", "天花板", "太惊艳", "太显瘦",
        "巨好看", "美爆", "封神", "神仙", "救命", "显白", "显瘦",
        "藏肉", "遮肉", "不挑人", "闭眼入", "高级", "好牛",
    ]
    crowd_words = [
        "姐妹", "女生", "女人", "女孩", "妈妈", "宝妈", "微胖",
        "小个子", "梨形", "苹果型", "胯宽", "腿粗", "大骨架",
        "肩宽", "腰粗", "肚子", "拜拜肉", "你们",
    ]
    pain_words = [
        "胯宽", "腿粗", "显胖", "肚子", "腰粗", "肩宽", "显壮",
        "肉多", "遮肉", "藏肉", "不敢穿", "穿不进去", "卡肉", "勒肉",
    ]
    effect_words = [
        "显瘦", "显高", "显白", "显腿长", "比例", "直角肩",
        "腰线", "拉长", "高级", "气质", "干净", "明亮", "薄",
    ]
    food_sensory_words = ["好吃", "鲜甜", "脆甜", "爆汁", "多汁", "口感", "鲜嫩", "软糯", "酥脆", "Q弹", "拉丝", "试吃", "咬一口"]
    food_fresh_words = ["新鲜", "鲜活", "现摘", "现采", "现捕", "现捞", "冷链", "保鲜", "锁鲜", "果园", "产地"]
    food_visual_words = ["切开", "掰开", "开箱", "开袋", "个头", "果径", "饱满", "一大颗", "一整箱"]
    contrast_words = [
        "不是", "但是", "反而", "一下子", "直接", "居然",
        "完全", "一点都不", "你看", "看到没有", "有没有发现",
    ]
    cta_or_price_words = ["价格", "多少钱", "链接", "小黄车", "购物车", "上车", "下单", "拍下"]
    if any(w in txt for w in cta_or_price_words):
        return 0.0, []
    close_words = ["尺码", "码数", "卡码", "推荐尺码", "身高体重", "往大拍", "往小拍"]
    if any(w in txt for w in close_words):
        return 0.0, []

    score = 0.0
    reasons = []
    if any(kw and kw in txt for kw in hook_keywords):
        score += 9
        reasons.append("爆词")
    if any(w in txt for w in strong_words):
        score += 10
        reasons.append("强情绪")
    if any(w in txt for w in crowd_words):
        score += 7
        reasons.append("人群")
    if any(w in txt for w in pain_words):
        score += 8
        reasons.append("痛点")
    if any(w in txt for w in effect_words):
        score += 7
        reasons.append("效果")
    if any(w in txt for w in food_sensory_words):
        score += 10
        reasons.append("口感")
    if any(w in txt for w in food_fresh_words):
        score += 7
        reasons.append("新鲜")
    if any(w in txt for w in food_visual_words):
        score += 7
        reasons.append("近景")
    if any(w in txt for w in contrast_words):
        score += 5
        reasons.append("反差")
    if any(p in str(text or "") for p in ("?", "？", "吗")):
        score += 4
        reasons.append("问句")

    pref_hits = _hook_pref_score(txt, focus_hint, ai_controls)
    if pref_hits:
        score += 8 + pref_hits * 4
        reasons.append("偏好")

    if dur <= 4.0:
        score += 4
    elif dur <= 6.0:
        score += 2
    else:
        score -= 1

    if len(txt) < 4:
        score -= 4
    if score < 10:
        return 0.0, []
    concrete_reasons = {"强情绪", "痛点", "效果", "口感", "新鲜", "近景", "反差", "问句", "偏好"}
    if not any(reason in reasons for reason in concrete_reasons):
        return 0.0, []
    return score, reasons


def _collect_hook_candidates_from_entries(entries, hook_keywords=None, focus_hint=None, ai_controls=None, limit=12):
    candidates = []
    for fallback_idx, entry in enumerate(entries, 1):
        if len(entry) < 3:
            continue
        if len(entry) >= 4:
            idx, es, ee, text = entry[:4]
        else:
            idx = fallback_idx
            es, ee, text = entry[:3]
        try:
            idx = int(idx)
            dur = float(ee) - float(es)
        except Exception:
            continue
        _needs_previous, starts_incomplete, ends_incomplete = _director_context_boundary_flags(text)
        if starts_incomplete or ends_incomplete:
            # Keep the sentence available to the director as body material, but
            # never advertise an obvious fragment as a Hook candidate.
            continue
        score, reasons = _score_hook_text_candidate(text, dur, hook_keywords, focus_hint, ai_controls)
        if score > 0:
            candidates.append((idx, text, dur, score, reasons))
    candidates.sort(key=lambda c: (-c[3], c[2]))
    return candidates[:limit], len(candidates)


def _extract_json_array_payload(text):
    """Return the first balanced JSON array substring, or None for truncated replies."""
    raw = str(text or "")
    start = raw.find("[")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for pos in range(start, len(raw)):
        ch = raw[pos]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return raw[start:pos + 1]
    return None


def _recover_json_objects_from_text(text):
    """Recover complete JSON objects from a possibly truncated array reply."""
    raw = str(text or "")
    decoder = json.JSONDecoder()
    recovered = []
    pos = 0
    while pos < len(raw):
        brace = raw.find("{", pos)
        if brace < 0:
            break
        try:
            obj, end = decoder.raw_decode(raw[brace:])
        except json.JSONDecodeError:
            pos = brace + 1
            continue
        if isinstance(obj, dict):
            recovered.append(obj)
        pos = brace + max(end, 1)
    return recovered


def _multi_version_hook_candidate_score(clip):
    if _is_hook_clip(clip) or _is_close_clip(clip):
        return -1
    text = str(clip[1] if len(clip) > 1 else "")
    dur = _clip_duration_value(clip)
    if dur > 10:
        return -1
    try:
        hook_words = load_keywords().get("hook_keywords", [])
    except Exception:
        hook_words = []
    strong_words = ["绝了", "惊艳", "显白", "显瘦", "不敢信", "天花板", "太好看", "高级", "救命"]
    score = 0
    score += sum(4 for kw in hook_words[:80] if kw and kw in text)
    score += sum(6 for kw in strong_words if kw in text)
    if any(p in text for p in ("吗", "？", "?")):
        score += 2
    if dur <= 6:
        score += 2
    return score


def _multi_version_close_candidate_score(clip):
    if _is_hook_clip(clip) or _is_close_clip(clip):
        return -1
    text = str(clip[1] if len(clip) > 1 else "")
    close_words = [
        "尺码", "码数", "正码", "偏大", "偏小", "身高", "体重", "斤", "码",
        "放心", "安心", "闭眼", "推荐", "适合", "通勤", "上班", "约会",
        "显瘦", "遮肉", "不挑人", "直接", "入", "拍",
    ]
    bad_words = ["多少钱", "价格", "福利价", "破价", "链接", "上车"]
    if any(w in text for w in bad_words):
        return -1
    score = sum(3 for kw in close_words if kw and kw in text)
    if _clip_duration_value(clip) <= 12:
        score += 1
    return score


def _pick_best_multi_version_candidate(candidates, score_fn, current_clips, used_clips):
    ranked = []
    for clip in candidates or []:
        if _clip_overlaps_any(clip, current_clips) or _clip_overlaps_any(clip, used_clips):
            continue
        score = score_fn(clip)
        if score > 0:
            ranked.append((score, _clip_duration_value(clip), clip))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][2]


def _repair_multi_version_structure(clips, available_pool, reserved_hook, reserved_close,
                                    used_clips, target_duration, log_fn=None, label="版本"):
    repaired = list(clips or [])
    if not repaired:
        return repaired

    def _log(msg):
        if log_fn:
            log_fn(msg)

    try:
        _hook_words = load_keywords().get("hook_keywords", [])
    except Exception:
        _hook_words = []

    def _usable_hook(clip):
        score, _reasons = _final_hook_quality_score(
            clip[1] if len(clip) > 1 else "",
            _clip_duration_value(clip),
            _hook_words,
            None,
            None,
        )
        return score >= 20.0

    inserted = []
    current_hook = any(_is_hook_clip(c) for c in repaired)
    if not current_hook:
        hook = None
        if reserved_hook and _usable_hook(reserved_hook) and not _clip_overlaps_any(reserved_hook, repaired) and not _clip_overlaps_any(reserved_hook, used_clips):
            hook = reserved_hook
        if hook:
            repaired.insert(0, hook)
            inserted.append("补Hook")
        else:
            in_clip = _pick_best_multi_version_candidate(repaired, _multi_version_hook_candidate_score, [], [])
            if in_clip and _usable_hook(in_clip):
                repaired = [_retag_clip_type(in_clip, "hook")] + [c for c in repaired if c is not in_clip]
                inserted.append("Product转Hook")
            else:
                extra = _pick_best_multi_version_candidate(available_pool, _multi_version_hook_candidate_score, repaired, used_clips)
                if extra and _usable_hook(extra):
                    repaired.insert(0, _retag_clip_type(extra, "hook"))
                    inserted.append("补替代Hook")

    current_close = any(_is_close_clip(c) for c in repaired)
    if not current_close:
        close = None
        if reserved_close and not _clip_overlaps_any(reserved_close, repaired) and not _clip_overlaps_any(reserved_close, used_clips):
            close = reserved_close
        if close:
            repaired.append(close)
            inserted.append("补Close")
        else:
            in_clip = _pick_best_multi_version_candidate(list(reversed(repaired)), _multi_version_close_candidate_score, [], [])
            if in_clip:
                repaired = [c for c in repaired if c is not in_clip] + [_retag_clip_type(in_clip, "close")]
                inserted.append("Product转Close")
            else:
                extra = _pick_best_multi_version_candidate(available_pool, _multi_version_close_candidate_score, repaired, used_clips)
                if extra:
                    repaired.append(_retag_clip_type(extra, "close"))
                    inserted.append("补替代Close")

    target_min, target_max = _multi_version_target_bounds(target_duration)
    total = _multi_version_total_duration(repaired)
    if total < target_min:
        added = 0
        try:
            target = int(target_duration or 60)
        except Exception:
            target = 60
        if target <= 40:
            max_clips_for_floor = 8
        elif target >= 100:
            max_clips_for_floor = 24
        elif target >= 80:
            max_clips_for_floor = 18
        else:
            max_clips_for_floor = 12
        product_pool = [
            c for c in (available_pool or [])
            if not _is_hook_clip(c) and not _is_close_clip(c)
            and not _clip_overlaps_any(c, repaired)
            and not _clip_overlaps_any(c, used_clips)
        ]
        product_pool.sort(key=lambda c: (
            abs((total + _clip_duration_value(c)) - min(max(target_min, total + 0.1), target_max)),
            -float(c[4] if len(c) > 4 else 0),
            -_clip_duration_value(c),
        ))
        for extra in product_pool:
            dur = _clip_duration_value(extra)
            if dur <= 0:
                continue
            if total + dur > target_max + 3:
                continue
            insert_at = len(repaired)
            for i in range(len(repaired) - 1, -1, -1):
                if _is_close_clip(repaired[i]):
                    insert_at = i
                    break
            repaired.insert(insert_at, extra)
            total += dur
            added += 1
            if total >= target_min:
                break
            if len(repaired) >= max_clips_for_floor:
                break
        if added:
            inserted.append(f"补Product{added}段")

    hook_items = [c for c in repaired if _is_hook_clip(c)]
    close_items = [c for c in repaired if _is_close_clip(c)]
    middle_items = [c for c in repaired if not _is_hook_clip(c) and not _is_close_clip(c)]
    if hook_items or close_items:
        repaired = hook_items + middle_items + close_items

    hooks, products, closes = _multi_version_type_counts(repaired)
    total = _multi_version_total_duration(repaired)
    if inserted:
        _log(f"{label}结构修复: {', '.join(inserted)}")
    _log(f"{label}结构: Hook={hooks}, Product={products}, Close={closes}, 时长={total:.1f}s")
    if hooks == 0:
        _log(f"{label}警告: Hook候选不足，未能补出有效开头")
    if closes == 0:
        _log(f"{label}警告: Close候选不足，未能补出有效结尾")
    if total < target_min:
        _log(f"{label}警告: 时长仍低于目标下限 {target_min:.0f}s")
    return repaired

def _friendly_http(code, err=""):
    """翻译 HTTP 错误码为用户友好提示"""
    code = int(code) if isinstance(code, (int, str)) and str(code).isdigit() else 0
    if code == 401:
        return "API Key 无效或已过期，请检查设置"
    elif code == 402:
        return "API 余额不足，请充值后重试"
    elif code == 429:
        return "请求太频繁，请稍后再试"
    elif code == 404:
        return "接口地址错误，请检查 Base URL 设置"
    elif code == 500 or code == 502 or code == 503:
        return "AI 服务器暂时不可用，请稍后再试"
    elif code == 413:
        return "发送内容过长，请缩短视频后重试"
    else:
        return f"请检查网络和API设置（错误码:{code}）"


class NonRetryableAIError(RuntimeError):
    """AI errors that user must fix in settings/account before retrying."""


def _is_non_retryable_http(code):
    try:
        code = int(code)
    except Exception:
        return False
    return code in {400, 401, 402, 403, 404, 413}


def _friendly_msg(err_str):
    """翻译常见错误信息"""
    s = err_str.lower()
    if "timeout" in s or "timed out" in s:
        return "网络连接超时，请检查网络后重试"
    if "connection" in s or "connect" in s or "winerror" in s:
        return "网络连接失败，请检查网络设置"
    if "api" in s and "key" in s:
        return "API Key 无效，请检查设置"
    return err_str[:80]




# ============================================================
# 设置管理
# ============================================================

_LAST_SETTINGS_SAVE_ERROR = ""


def _get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _settings_backup_path(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{path}.invalid-{stamp}-{os.getpid()}.bak"


def _load_settings_dict(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("settings root must be a JSON object")
    return data


def get_last_settings_save_error():
    return _LAST_SETTINGS_SAVE_ERROR


def load_settings():
    # 优先读用户数据目录（用户保存的设置），其次读打包目录
    try:
        from config import SETTINGS_PATH as _user_path
        if os.path.exists(_user_path):
            return _normalize_ai_model_defaults(_load_settings_dict(_user_path))
    except Exception:
        pass
    path = os.path.join(_get_base_path(), "ai_settings.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return _normalize_ai_model_defaults(json.load(f))
    except Exception:
        return _default_settings()

def _keyword_file_paths():
    import os

    app_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keywords.json")
    try:
        from config import USER_DATA_DIR
        user_root = USER_DATA_DIR
    except Exception:
        user_root = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LiveClipper")
    user_path = os.path.join(user_root, "keywords.json")
    return app_path, user_path


def _load_keyword_file(path):
    import json
    import os

    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _merge_keyword_files(app_data, user_data):
    """Compatibility helper: user-level keys replace app template keys."""
    merged = {}
    if isinstance(app_data, dict):
        merged.update(app_data)
    if isinstance(user_data, dict):
        for key, value in user_data.items():
            merged[key] = value
    return merged


def _clean_keyword_list(value):
    if not isinstance(value, list):
        return []
    items = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            items.append(text)
            seen.add(text)
    return items


def _clean_keyword_map(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, items in value.items():
        name = str(key or "").strip()
        cleaned = _clean_keyword_list(items)
        if name and cleaned:
            result[name] = cleaned
    return result


def _strip_forbidden_keyword_conflicts(keyword_map, forbidden_words):
    """Remove positive keywords that would be filtered by forbidden phrases."""
    if not isinstance(keyword_map, dict):
        return {}
    forbidden = sorted((str(w or "").strip() for w in forbidden_words or []), key=len, reverse=True)
    forbidden = [w for w in forbidden if w]
    cleaned = {}
    for key, words in keyword_map.items():
        kept = []
        for word in _clean_keyword_list(words):
            if any(bad in word for bad in forbidden):
                continue
            kept.append(word)
        cleaned[key] = kept
    return cleaned


def load_keywords():
    """Load one effective vocabulary.

    Source order:
    1. %APPDATA%/LiveClipper/keywords.json when the user has saved settings.
    2. app/keywords.json as the default/reset template.
    3. config.STRICT_FORBIDDEN_PHRASES as the only non-editable safety floor.
    """
    from config import STRICT_FORBIDDEN_PHRASES

    app_kw_path, user_kw_path = _keyword_file_paths()
    app_data = _load_keyword_file(app_kw_path)
    user_data = _load_keyword_file(user_kw_path)
    source = user_data if user_data else app_data

    def _pick(key, fallback):
        if isinstance(source, dict) and key in source:
            return source.get(key)
        if isinstance(app_data, dict) and key in app_data:
            return app_data.get(key)
        return fallback

    merged_forbidden = _clean_keyword_list(_pick("forbidden_phrases", []))
    merged_forbidden = list(dict.fromkeys(merged_forbidden + _clean_keyword_list(list(STRICT_FORBIDDEN_PHRASES))))
    merged_clip_kw = _strip_forbidden_keyword_conflicts(
        _clean_keyword_map(_pick("clip_keywords", {})),
        merged_forbidden,
    )
    source_pref = _clean_keyword_map(_pick("preference_keywords", {}))
    try:
        default_pref = _clean_keyword_map(_DEFAULT_PREFERENCE_KEYWORDS)
    except Exception:
        default_pref = {}
    merged_pref_base = dict(default_pref)
    merged_pref_base.update(source_pref)
    merged_pref = _strip_forbidden_keyword_conflicts(
        merged_pref_base,
        merged_forbidden,
    )
    merged_filler = _clean_keyword_list(_pick("filler_words", []))
    merged_negative = _clean_keyword_list(_pick("negative_signals", []))

    # Hook爆点词：只来自可见词库，不再追加隐藏后端词。
    hook_words = merged_clip_kw.get("hook", [])
    all_hook_kw = list(dict.fromkeys(hook_words))

    return {
        "clip_keywords": merged_clip_kw,
        "forbidden_phrases": merged_forbidden,
        "filler_words": merged_filler,
        "hook_keywords": all_hook_kw,
        "negative_signals": merged_negative,
        "preference_keywords": merged_pref,
        "detail_keywords": _clean_keyword_list(_pick("detail_keywords", [])),
        "_web_vocab_full_override": True,
        "_source": user_kw_path if user_data else app_kw_path,
    }




# 关键词缓存（避免每次调用都读文件）
_keywords_cache = {"_data": None, "_mtime": 0}

def _get_keywords():
    """获取关键词（带文件修改时间缓存，keywords.json更新后自动刷新）"""
    import os

    signature = []
    for kw_path in _keyword_file_paths():
        try:
            signature.append((kw_path, os.path.getmtime(kw_path)))
        except Exception:
            signature.append((kw_path, 0))
    signature = tuple(signature)

    if _keywords_cache["_data"] is None or signature != _keywords_cache["_mtime"]:
        _keywords_cache["_data"] = load_keywords()
        _keywords_cache["_mtime"] = signature
    return _keywords_cache["_data"]


# ============================================================
# 默认偏好关键词（可被keywords.json覆盖）
# ============================================================
_DEFAULT_PREFERENCE_KEYWORDS = {
    "版型显瘦": ['显瘦', '遮肉', '藏肉', '收腰', '包容', '不挑人', '微胖', '遮胯', '遮肚', '收腹', '提臀', '显高', '小个子', '梨形', '苹果型', '腿粗', '拜拜肉', '瘦十斤', '小一号', '秒变', '立瘦', '显腿长', '显腰细', '比例好', '拉长比例', '遮得住', '收腰显瘦', '遮副乳', '托胸', '胯宽', '大骨架', '纸片人', '小肚腩', '背厚', '肩宽'],
    "颜色氛围": ['显白', '提亮', '抬气色', '显肤色', '黄皮', '黑皮', '衬肤色', '不挑肤色', '冷白皮', '暖白皮', '气色好', '衬人白', '高级灰', '显嫩', '温柔色', '显贵色', '不挑皮', '上镜色', '拍照好看', '老钱风', '奶油色', '燕麦色', '雾霾蓝', '牛油果', '奶茶色', '焦糖色', '香芋紫', '橡皮粉', '百搭色', '抬肤色'],
    "场景搭配": ['通勤', '约会', '度假', '日常', '出门', '上班', '逛街', '实穿', '职场', '聚会', '拍照', '旅游', '出差', '叠穿', '内搭', '外穿', '单穿', '一年四季', '懒人', '一套搞定', '见家长', '见前男友', '同学聚会', '相亲', '年会', '踏青', '遛娃', '送孩子', '百搭', '穿得出去'],
    "性价比": ['划算', '超值', '性价比', '品质', '质感', '做工', '同款', '外面买不到', '大牌平替', '代工厂', '专柜', '商场', '物超所值', '比外面', '比商场', '同品质', '这个价', '这个品质', '商场同款', '自己家工厂', '源头', '出厂价', '直播间专属', '老粉', '闭眼冲', '不踩坑', '买过都说好', '回购率', '对得起这个价', '回头客'],
    "情绪感染": ['绝了', '太漂亮', '美爆', '太好看了', '太爱', '神仙', '封神', '超级超级', '特别特别', '真的真的', '非常非常', '天呐', '妈呀', '我的天', '受不了', '爱了爱了', '绝绝子', 'yyds', '信我', '相信我', '不骗你', '真心', '自留', '我自己也', '美哭', '好看死', '太绝了', '我天', '天哪', '疯了吧', '哇塞', '我自己都'],
    "流行趋势": ['流行', '当季', '新款', '设计', '原创', '不撞款', '爆款', '热门', '趋势', '法式', '韩系', '日系', '欧美', 'ins风', '极简', '复古', '国风', '新中式', '设计师', '小心机', '细节', '小众', '轻奢', '时髦', '小香风', '千金风', '老钱', '清冷感', '氛围感', '松弛感', '财阀千金', '甜酷', '美拉德', '多巴胺', '静奢'],
    "面料质感": ['面料', '手感', '亲肤', '质感', '桑蚕丝', '冰感', '软糯', '透气', '真丝', '羊毛', '羊绒', '纯棉', '雪纺', '缎面', '蕾丝', '牛仔', '针织', '垂感', '弹力', '厚实', '做工', '走线', '不起球', '不褪色', '抗皱', '免熨', '垂坠', '丝滑', '软乎乎', '厚薄适中', '垂坠感', '糯糯的', '像云朵', '婴儿肌', '裸感'],
    "紧迫稀缺": ['限量', '限时', '手慢无', '秒空', '断码', '断货', '库存', '补不到', '不补货', '最后', '现货', '抢', '赶紧', '抓紧', '错过', '下架', '名额', '余量', '稀缺', '卖完', '补货难', '今天', '马上'],
    "尺寸长度": ['裙长', '到脚踝', '露脚踝', '遮小腿', '小腿肚', '膝盖', '大腿', '长度', '衣长', '袖长', '胯宽', '遮胯', '盖住', '刚好', '不过膝', '过膝', '九分', '七分', '短款', '中长款', '拖地', '盖脚面', '比例', '显腿长', '拉长比例', '视觉比例'],
    "工艺细节": ['工艺', '成本', '做工', '走线', '细节', '设计', '拼接', '剪裁', '立体', '版型', '定型', '压褶', '褶皱', '花边', '蕾丝边', '包边', '锁边', '双线', '加固', '五金', '拉链', '扣子', '纽扣', '口袋', '里衬', '加绒', '加厚', '薄款', '定染', '染色', '固色', '色牢度'],
    "穿着体验": ['舒适', '不勒', '自在', '轻盈', '无感', '不紧绷', '活动方便', '不束缚', '不扎人', '亲肤', '不闷', '不热', '轻薄', '凉爽', '温暖', '贴身', '宽松', '有余量', '不卡', '不掉', '不滑', '不卷边'],
    "对比优势": ['买不到', '外面没有', '不一样', '区别', '独特', '独家', '外面买', '比外面', '比市面', '比商场', '同价位', '同品质', '这个价', '值这个价', '性价比高', '划算', '超值', '几十块', '商场同款', '代工厂', '源头', '一手', '直接', '没有第二家'],
    "口感食欲": ['好吃', '鲜甜', '脆甜', '爆汁', '多汁', '汁水', '入口', '口感', '肉质', '鲜嫩', '软糯', '酥脆', 'Q弹', '弹牙', '拉丝', '试吃', '开吃', '开袋', '开箱', '切开', '掰开', '咬一口', '吃起来', '闻起来'],
    "新鲜品质": ['新鲜', '鲜活', '现摘', '现采', '现捕', '现捞', '当天采', '当天发', '鲜度', '品质', '果形', '果径', '个头', '饱满', '净果', '坏果', '坏包赔', '源头', '基地', '果园', '产区', '原产地'],
    "产地溯源": ['产地', '原产地', '源头', '基地', '果园', '农场', '牧场', '渔港', '海捕', '直采', '直发', '溯源', '农户', '合作社', '产区', '当季', '应季'],
    "规格分量": ['规格', '净含量', '净重', '克重', '重量', '斤装', '箱装', '袋装', '盒装', '整箱', '大果', '中果', '果径', '个头', '份量', '分量', '一斤', '两斤', '三斤', '五斤'],
    "发货保鲜": ['发货', '现发', '冷链', '冰袋', '保温箱', '泡沫箱', '顺丰', '次日达', '保鲜', '锁鲜', '冷冻', '速冻', '常温', '冷藏', '售后', '坏果包赔', '破损包赔'],
    "场景吃法": ['早餐', '夜宵', '下午茶', '办公室', '孩子', '老人', '全家', '聚餐', '火锅', '烧烤', '煲汤', '下饭', '拌饭', '空气炸锅', '简单一热', '即食', '开袋即食', '囤货', '冰箱', '送礼'],
}

AI_FOCUS_ALIASES = {
    "穿着场景": "场景搭配",
    "穿搭场景": "场景搭配",
    "场景": "场景搭配",
    "通勤": "场景搭配",
    "显瘦": "版型显瘦",
    "小个子": "版型显瘦",
    "显白": "颜色氛围",
    "面料": "面料质感",
    "质感": "面料质感",
    "尺码": "尺寸长度",
    "尺寸": "尺寸长度",
    "做工": "工艺细节",
    "工艺": "工艺细节",
    "品质": "品质细节",
    "舒适": "穿着体验",
    "体验": "穿着体验",
    "口感": "口感食欲",
    "食欲": "口感食欲",
    "试吃": "口感食欲",
    "新鲜": "新鲜品质",
    "鲜度": "新鲜品质",
    "产地": "产地溯源",
    "溯源": "产地溯源",
    "规格": "规格分量",
    "分量": "规格分量",
    "净重": "规格分量",
    "发货": "发货保鲜",
    "冷链": "发货保鲜",
    "保鲜": "发货保鲜",
    "吃法": "场景吃法",
    "食用场景": "场景吃法",
    "囤货": "场景吃法",
    "通用卖点": "其他",
}


def _normalize_focus_label(value):
    text = str(value or "").strip()
    return AI_FOCUS_ALIASES.get(text, text)


def _current_focus_used_label():
    summary = get_last_analysis_metadata()["preference_summary"]
    if not isinstance(summary, dict):
        return ""
    label = summary.get("used_label") or summary.get("label") or summary.get("matched_label") or ""
    return _normalize_focus_label(label)


def _normalize_preference_weights(weights):
    result = {}
    if not isinstance(weights, dict):
        return result
    for key, value in weights.items():
        result[_normalize_focus_label(key)] = value
    return result


def _normalize_focus_list(values):
    seen = set()
    result = []
    for item in values or []:
        text = _normalize_focus_label(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def save_settings(settings):
    global _LAST_SETTINGS_SAVE_ERROR
    # 写入用户数据目录（可写），非打包目录（可能只读）
    try:
        from config import SETTINGS_PATH as _save_path
    except ImportError:
        _save_path = os.path.join(_get_base_path(), "ai_settings.json")
    temp_path = ""
    try:
        _LAST_SETTINGS_SAVE_ERROR = ""
        settings = _normalize_ai_model_defaults(settings)
        parent = os.path.dirname(os.path.abspath(_save_path))
        os.makedirs(parent, exist_ok=True)
        existing = {}
        if os.path.exists(_save_path):
            try:
                existing = _load_settings_dict(_save_path)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                backup_path = _settings_backup_path(_save_path)
                os.replace(_save_path, backup_path)
        existing.update(settings)
        temp_path = os.path.join(
            parent,
            f".{os.path.basename(_save_path)}.{os.getpid()}.{os.urandom(4).hex()}.tmp",
        )
        with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, _save_path)
        temp_path = ""
        _load_settings_dict(_save_path)
        return True
    except Exception as exc:
        if isinstance(exc, (PermissionError, FileNotFoundError, OSError)):
            _LAST_SETTINGS_SAVE_ERROR = "用户数据目录不可写，请检查保存位置或磁盘状态"
        else:
            _LAST_SETTINGS_SAVE_ERROR = "配置文件写入失败，请重试"
        import traceback
        traceback.print_exc()
        return False
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

def _default_settings():
    return {
        "api_key": "", "base_url": DEEPSEEK_DEFAULT_BASE_URL,
        "model": DEEPSEEK_DEFAULT_MODEL, "enabled": False,
        "local_asr_engine": "sensevoice",
        "whisper_model": "small",
        "style_profile_enabled": True,
        "style_profile_strength": "auto",
        "content_review_mode": "off",
    }


def _load_ai_rules():
    defaults = {
        "narrative": "",
        "category_filter": True,
        "time_coherence": True,
        "hook_cap": "5秒",
        "custom_text": "",
    }
    try:
        rules = load_settings().get("ai_rules", {})
        if isinstance(rules, dict):
            defaults.update(rules)
    except Exception:
        pass
    return defaults


def _normalize_ai_controls(ai_controls=None):
    if not isinstance(ai_controls, dict):
        return {}

    def _clean_text(value):
        text = str(value or "").strip()
        return "" if text in ("自动", "auto", "默认", "无") else text

    def _clean_list(value):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return []
        seen = set()
        result = []
        for item in value:
            text = _clean_text(item)
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result

    return {
        "primary_category": _clean_text(ai_controls.get("primary_category")),
        "secondary_category": _clean_text(ai_controls.get("secondary_category")),
        "leaf_category": _clean_text(ai_controls.get("leaf_category")),
        "main_product": _clean_text(ai_controls.get("main_product")),
        "goal": _clean_text(ai_controls.get("goal")),
        "selling_points": _normalize_focus_list(_clean_list(ai_controls.get("selling_points"))),
        "avoid": _clean_list(ai_controls.get("avoid")),
        "hook_style": _clean_text(ai_controls.get("hook_style")),
        "ending_style": _clean_text(ai_controls.get("ending_style")),
        "strictness": _clean_text(ai_controls.get("strictness")),
    }


def _merge_ai_rules(ai_controls=None):
    rules = _load_ai_rules()
    controls = _normalize_ai_controls(ai_controls)
    strictness = controls.get("strictness", "")
    if strictness == "严格":
        rules["category_filter"] = True
        rules["time_coherence"] = True
        cap = _hook_cap_seconds(rules)
        if cap is None or cap > 5:
            rules["hook_cap"] = "5秒"
    return rules


def _build_ai_controls_lines(ai_controls=None):
    controls = _normalize_ai_controls(ai_controls)
    if not controls:
        return []

    goal_map = {
        "爆款种草": "目标是爆款种草，优先选择强情绪、强上身效果、强记忆点的片段。",
        "专业讲解": "目标是专业讲解，优先选择面料、工艺、版型、穿着体验讲清楚的片段。",
        "显瘦转化": "目标是显瘦转化，优先选择遮肉、收腰、比例优化、上身对比相关片段。",
        "质感高级": "目标是质感高级，优先选择面料垂感、做工细节、颜色氛围、风格高级感片段。",
        "快速促单": "目标是自然转化，优先选择决策理由明确、尺码引导自然、顾虑解除充分的片段；不要选择价格、链接、领券、满减、点关注等强CTA内容。",
        "食欲种草": "目标是食欲种草，优先选择试吃反应、切开近景、口感描述和强食欲画面相关片段。",
        "新鲜转化": "目标是新鲜转化，优先选择现摘现发、产地背书、规格分量、冷链保鲜和售后保障相关片段。",
        "囤货转化": "目标是囤货转化，优先选择家庭囤货、早餐夜宵、办公室、送礼和复购理由明确的片段。",
    }
    hook_map = {
        "痛点开头": "Hook优先用痛点开头，先圈定人群或问题，再进入卖点。",
        "上身效果开头": "Hook优先用上身效果开头，先给用户看到穿上后的核心效果。",
        "爆点金句开头": "Hook优先用主播最有冲击力的爆点金句开头。",
        "主播强推荐开头": "Hook优先用主播强推荐、强背书、强情绪的表达开头。",
        "试吃反应开头": "Hook优先用试吃反应或口感爆点开头，让用户第一秒感到好吃、想吃。",
        "细节近景开头": "Hook优先用切开、掰开、开箱、拉丝、爆汁、个头等可视化细节开头。",
        "产地品质开头": "Hook优先用产地、现摘现发、鲜活品质或源头背书开头。",
        "不强制Hook": "不强制必须选择Hook类型；如果没有好Hook，可以用最完整的Product片段自然开场。",
    }
    ending_map = {
        "尺码引导": "结尾优先选择尺码/身高体重/选择建议，但不要包含价格。",
        "信任背书": "结尾优先选择主播信任背书、品质确认、闭眼入类表达。",
        "场景收尾": "结尾优先选择通勤、约会、出门、日常等场景化收尾。",
        "囤货收尾": "结尾优先选择家庭囤货、冰箱常备、办公室零食、早餐夜宵等明确使用场景。",
        "发货保鲜": "结尾优先选择现发、冷链、保鲜、售后保障、坏果包赔等降低顾虑的内容。",
        "复购背书": "结尾优先选择复购、老客反馈、真实试吃、家人爱吃等信任背书。",
        "自然结束": "结尾自然结束即可，不必强行促单。",
        "不要促单": "结尾不要强促单，不要选择催拍、库存、价格、链接类片段。",
    }
    strictness_map = {
        "宽松": "选片严格度为宽松：卖点覆盖优先，允许少量时间跳跃，但不要牺牲内容完整度。",
        "标准": "选片严格度为标准：在完整、流畅、卖点覆盖之间平衡。",
        "严格": "选片严格度为严格：宁可少选，也不要重复卖点、无关品类、废话、跳跃过大的片段。",
    }

    lines = []
    primary_category = controls.get("primary_category")
    secondary_category = controls.get("secondary_category")
    leaf_category = controls.get("leaf_category")
    main_product = controls.get("main_product")
    if primary_category:
        lines.append(f"本次一级类目锁定为：{primary_category}；二级/细分可自动识别，但不得套用其他一级类目的卖点语言。")
    if secondary_category or leaf_category:
        parts = []
        if secondary_category:
            parts.append(f"二级类目={secondary_category}")
        if leaf_category:
            parts.append(f"细分类目={leaf_category}")
        lines.append("本次细分类目参考：" + "，".join(parts) + "；用于卖点维度和关键词加权，识别不确定时仍以字幕里的主商品为准。")
    if main_product:
        lines.append(f"本次主商品手动锁定为：{main_product}；同一条成片只能围绕该商品，不得混入其他同类商品片段。")
    goal = controls.get("goal")
    if goal:
        lines.append(goal_map.get(goal, f"本次成片目标：{goal}。"))
    selling = controls.get("selling_points", [])
    if selling:
        lines.append(f"主卖点优先：{', '.join(selling)}；用于提升承接Hook、证明细节和顾虑解除片段权重，不要为了命中卖点牺牲成片结构。")
    avoid = controls.get("avoid", [])
    if avoid:
        lines.append(f"本次不要选择这些内容：{', '.join(avoid)}；除非用户明确选择宽松，否则直接跳过相关条目。")
    hook_style = controls.get("hook_style")
    if hook_style:
        lines.append(hook_map.get(hook_style, f"开头方式优先遵循：{hook_style}。"))
    ending_style = controls.get("ending_style")
    if ending_style:
        lines.append(ending_map.get(ending_style, f"结尾方式优先遵循：{ending_style}。"))
    strictness = controls.get("strictness")
    if strictness:
        lines.append(strictness_map.get(strictness, f"选片严格度：{strictness}。"))
    return lines


def _hook_cap_seconds(rules=None):
    value = str((rules or _load_ai_rules()).get("hook_cap", "5秒")).strip()
    if value in ("", "不限", "none", "None"):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", value)
    return float(m.group(1)) if m else 5.0


def _build_ai_rules_prompt(rules=None, ai_controls=None, main_category=None):
    rules = rules or _merge_ai_rules(ai_controls)
    main_category = _normalize_forced_category(main_category) or main_category
    lines = []
    narrative = str(rules.get("narrative", "") or "").strip()
    custom_text = str(rules.get("custom_text", "") or "").strip()
    if narrative:
        lines.append(f"叙事结构必须遵循：{narrative}")
    lines.append("成片默认按带货成交链路组织：Hook承诺 → 承接Hook/直接效果 → 核心卖点证明 → 场景或人群代入 → 顾虑解除 → 自然收尾；不要只按直播时间摘片。")
    if rules.get("category_filter", True):
        lines.append("必须围绕同一主推品类选片，避免突然切到无关品类。")
    else:
        lines.append("允许合理跨品类选片，但必须保证内容衔接自然。")
    if rules.get("time_coherence", True):
        lines.append("时间连贯只作为同一成交角色内的参考；优先保证成交链路和语义完整。")
    hook_cap = str(rules.get("hook_cap", "5秒") or "").strip()
    if hook_cap and hook_cap != "不限":
        lines.append(f"Hook片段时长上限为{hook_cap}，超过则不要作为Hook。")
    if custom_text:
        lines.append(f"用户自定义硬规则：{custom_text}")
    if main_category and main_category in CATEGORY_PROMPT_RULES:
        lines.append(CATEGORY_PROMPT_RULES[main_category])
    lines.extend(_build_ai_controls_lines(ai_controls))
    if not lines:
        return ""
    return "\n★用户AI选片规则与本次控制（优先级高）★\n" + "\n".join(f"- {line}" for line in lines) + "\n"


def _is_food_fresh_category(category=None):
    text = str(category or "").strip()
    return "食品" in text or "生鲜" in text


def _normalize_forced_category(category=None):
    text = str(category or "").strip()
    if not text or text == "自动检测":
        return None
    try:
        for cat in PRODUCT_CATEGORIES:
            if text == cat or text in cat or cat in text:
                return cat
    except NameError:
        pass
    profile = resolve_vertical_profile(text)
    if profile:
        return profile.key
    if _is_food_fresh_category(text):
        return "食品/生鲜"
    return None


def _feedback_category_bucket(main_category=None):
    normalized = _normalize_forced_category(main_category)
    profile = resolve_vertical_profile(normalized or main_category)
    if profile:
        return profile.feedback_bucket
    if normalized:
        return "clothing"
    return "general"


def _feedback_scope_key(scope=None, main_category=None):
    base = str(scope or "smart").strip() or "smart"
    return f"{base}:{_feedback_category_bucket(main_category)}"


def _food_fresh_context_prompt(main_category=None):
    if not _is_food_fresh_category(main_category):
        return ""
    return """★食品/生鲜主商品识别★
- 用户已指定当前大品类为食品/生鲜，不要再判断它是不是食品；即使具体品名没有命中词库，也必须按食品/生鲜逻辑选片。
- 先从字幕里识别本场主播反复介绍、试吃、展示、说明产地/规格/发货/售后的主商品。可以识别为具体品名，也可以识别为水果、海鲜、熟食等大致商品。
- 请再判断主商品大致子类型：水果鲜食、肉禽蛋品、水产海鲜、零食烘焙、熟食/预制菜、饮品冲调、粮油调味、其他食品。无法准确判断时也不要退回通用/服装逻辑。
- 如果同一段素材里出现多个食品商品（如蟠桃和芒果），一次成片只能围绕一个主商品；不要把不同水果/不同食品当作不同卖点混进同一条视频。
- 不要依赖穷举商品名；按卖点维度选片：口感食欲、新鲜品质、产地溯源、规格分量、发货保鲜、场景吃法、转化信任。
- focus字段必须写食品卖点维度或具体食品卖点，不要写版型、面料、显瘦、尺码、穿搭等服装维度。"""


_LAST_FOOD_PRODUCT_FILTER_SUMMARY = {}

FOOD_PRODUCT_GROUPS = {
    "桃类": ["蟠桃", "水蜜桃", "蜜桃", "黄桃", "油桃", "毛桃", "桃子"],
    "芒果类": ["芒果", "贵妃芒", "凯特芒", "金煌芒", "台农芒", "澳芒", "小台芒", "大青芒", "青芒"],
    "苹果类": ["苹果", "红富士", "富士", "阿克苏", "冰糖心", "花牛"],
    "梨类": ["梨", "香梨", "雪梨", "皇冠梨", "库尔勒香梨"],
    "柑橘橙类": ["橙子", "橘子", "柑橘", "沃柑", "耙耙柑", "爱媛", "脐橙", "血橙", "砂糖橘"],
    "葡萄类": ["葡萄", "提子", "阳光玫瑰", "夏黑", "巨峰"],
    "樱桃车厘子类": ["车厘子", "樱桃"],
    "莓果类": ["草莓", "蓝莓", "树莓", "黑莓"],
    "榴莲类": ["榴莲", "猫山王", "金枕", "干尧"],
    "荔枝龙眼类": ["荔枝", "龙眼", "桂圆", "妃子笑"],
    "瓜类": ["西瓜", "哈密瓜", "甜瓜", "羊角蜜", "网纹瓜"],
    "虾类": ["虾", "大虾", "鲜虾", "小龙虾", "虾仁"],
    "蟹类": ["螃蟹", "大闸蟹", "梭子蟹", "青蟹"],
    "鱼类": ["鱼", "三文鱼", "鳕鱼", "带鱼", "鲈鱼", "黄花鱼", "鱼片"],
    "贝类": ["生蚝", "扇贝", "鲍鱼", "花甲", "蛤蜊"],
    "牛肉类": ["牛肉", "牛排", "肥牛", "牛腱", "牛腩"],
    "羊肉类": ["羊肉", "羊排", "羊腿", "羊蝎子"],
    "猪肉类": ["猪肉", "排骨", "五花肉", "猪蹄", "腊肉"],
    "鸡禽蛋类": ["鸡肉", "鸡翅", "鸡腿", "鸡蛋", "土鸡蛋", "鸭蛋", "鹅蛋"],
    "零食糕点类": ["零食", "糕点", "蛋糕", "饼干", "面包", "曲奇", "麻薯", "蛋黄酥"],
    "粮油调味类": ["大米", "五常大米", "粮油", "酱油", "醋", "调味料", "食用油"],
}

_FOOD_PRODUCT_PROOF_WORDS = [
    "现摘", "现采", "现发", "当天发", "产地", "源头", "果园", "基地", "冷链",
    "保鲜", "锁鲜", "净重", "规格", "斤装", "箱装", "坏果包赔", "破损包赔",
    "好吃", "新鲜", "回购", "复购", "推荐", "买",
]


def _food_product_hits(text):
    text = str(text or "")
    hits = {}
    for label, keywords in FOOD_PRODUCT_GROUPS.items():
        matched = []
        for kw in keywords:
            if kw and kw in text:
                matched.append(kw)
        if matched:
            hits[label] = matched
    return hits


def _food_product_main_from_texts(texts):
    scores = {}
    counts = {}
    first_seen = {}
    for idx, text in enumerate(texts or []):
        hits = _food_product_hits(text)
        if not hits:
            continue
        proof_bonus = 8 if any(word in text for word in _FOOD_PRODUCT_PROOF_WORDS) else 0
        for label, words in hits.items():
            strength = sum(max(2, len(word)) for word in words)
            scores[label] = scores.get(label, 0) + 10 + strength + proof_bonus
            counts[label] = counts.get(label, 0) + 1
            first_seen.setdefault(label, idx)
    if not scores:
        return "", {}, {}
    ranked = sorted(scores, key=lambda label: (-scores[label], first_seen.get(label, 999999), label))
    return ranked[0], scores, counts


def _parse_srt_time_text_segments(srt_text):
    lines = str(srt_text or "").strip().split("\n")
    segments = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}', line):
            text_parts = []
            j = i + 1
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                text_parts.append(lines[j].strip())
                j += 1
            segments.append((line, "".join(text_parts)))
            i = j
        else:
            i += 1
    return segments


def _filter_srt_by_food_product(cleaned_srt, log_fn=None):
    """When food/fresh contains multiple concrete products, keep one main product per cut."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    global _LAST_FOOD_PRODUCT_FILTER_SUMMARY
    _LAST_FOOD_PRODUCT_FILTER_SUMMARY = {}

    segments = _parse_srt_time_text_segments(cleaned_srt)
    if not segments:
        return cleaned_srt
    main_product, scores, counts = _food_product_main_from_texts([text for _time_line, text in segments])
    active_products = [label for label, count in counts.items() if count > 0]
    if len(active_products) <= 1 or not main_product:
        if main_product:
            _LAST_FOOD_PRODUCT_FILTER_SUMMARY = {
                "main_product": main_product,
                "product_scores": scores,
                "product_counts": counts,
                "removed_segments": 0,
            }
        return cleaned_srt

    output_lines = []
    removed = 0
    removed_products = {}
    for time_line, text in segments:
        hits = _food_product_hits(text)
        hit_products = set(hits)
        if hit_products and main_product not in hit_products:
            removed += 1
            for label in hit_products:
                removed_products[label] = removed_products.get(label, 0) + 1
            continue
        output_lines.append(time_line)
        output_lines.append(text)
        output_lines.append("")

    product_rank = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    rank_text = "，".join(f"{label}{score:.0f}分/{counts.get(label, 0)}段" for label, score in product_rank[:5])
    removed_text = "，".join(f"{label}{count}段" for label, count in sorted(removed_products.items(), key=lambda item: item[1], reverse=True))
    _log(f"食品主商品隔离: 主商品={main_product}；候选={rank_text}；移除其他商品 {removed} 段{('(' + removed_text + ')') if removed_text else ''}")
    _LAST_FOOD_PRODUCT_FILTER_SUMMARY = {
        "main_product": main_product,
        "product_scores": scores,
        "product_counts": counts,
        "removed_segments": removed,
        "removed_products": removed_products,
    }
    return "\n".join(output_lines)


def _post_filter_food_cross_product(clips, log_fn=None):
    def _log(msg):
        if log_fn:
            log_fn(msg)

    def _clip_text(clip):
        if isinstance(clip, dict):
            parts = [
                clip.get("text"),
                clip.get("reason"),
                clip.get("focus"),
                clip.get("title"),
                clip.get("subtitle"),
            ]
            return " ".join(str(part or "") for part in parts if part)
        if isinstance(clip, (list, tuple)):
            if len(clip) > 1:
                return str(clip[1] or "")
            if clip:
                return str(clip[0] or "")
            return ""
        return str(clip or "")

    def _clip_label(clip):
        if isinstance(clip, dict):
            return clip.get("type") or clip.get("role") or clip.get("start") or "clip"
        if isinstance(clip, (list, tuple)) and clip:
            return clip[0]
        return "clip"

    if not clips:
        return clips
    main_product, scores, counts = _food_product_main_from_texts([_clip_text(clip) for clip in clips])
    if not main_product or len([label for label, count in counts.items() if count > 0]) <= 1:
        return clips
    kept = []
    removed = 0
    for clip in clips:
        text = _clip_text(clip)
        hits = set(_food_product_hits(text))
        if hits and main_product not in hits:
            removed += 1
            _log(f"食品混品过滤: 踢出非主商品片段 [{_clip_label(clip)}] {text[:30]}...(主商品={main_product}，命中={','.join(sorted(hits))})")
            continue
        kept.append(clip)
    if removed:
        _log(f"食品混品过滤: 主商品={main_product}，移除 {removed} 个其他食品商品片段，保留 {len(kept)} 个")
    return kept


def _category_system_overlay(main_category=None):
    if not _is_food_fresh_category(main_category):
        profile = resolve_vertical_profile(main_category)
        return str(profile.system_overlay or "") if profile else ""
    return f"""[品类覆盖: 食品/生鲜直播切片]
上方女装示例只作短视频结构参考；当前主品类是食品/生鲜时，禁止套用服装词，如上身、显瘦、尺码、面料、穿搭。
{_food_fresh_context_prompt(main_category)}
Hook优先级: 试吃反应/切开爆汁/开箱近景/产地新鲜/规格个头/家庭囤货场景。
Product优先级: 口感食欲、新鲜品质、产地溯源、规格分量、发货保鲜、场景吃法；每个Product只讲1-2个要点。
Close优先级: 复购信任、坏果/破损售后、冷链保鲜、囤货场景、自然购买理由。
合规边界: 普通食品不要输出治疗、预防、降三高、减肥、美白、养生功效、药用功效等表达；可以改成口感、风味、食用场景、传统风味、地方特色。"""


def _category_prompt_overrides(main_category=None, multi_version=False):
    if not _is_food_fresh_category(main_category):
        return None
    if multi_version:
        return {
            "dedup": "★食品/生鲜卖点去重★ 同一种口感或同一条产地背书不要重复堆叠；试吃、切开、规格、发货、场景各保留信息量最高的版本。",
            "hook": "★多版本食品Hook：找出3-5个不同开头★ 试吃反应、切开爆汁/拉丝、开箱个头、产地现发、家庭囤货场景各选最佳候选（有则选）。",
            "product": "★多版本食品Product：选择12-18个Product片段★ 先识别本场主商品和子类型，再覆盖口感食欲/新鲜品质/产地溯源/规格分量/发货保鲜/场景吃法，每个角度至少1-2个片段。",
            "close": "★多版本食品Close：选择3-5个Close片段★ 囤货理由、复购背书、冷链保鲜、坏果包赔、自然收尾各选1-2个；避开医疗保健功效和具体价格。",
        }
    return {
        "dedup": "★食品/生鲜卖点去重★ 同一种口感、同一条产地背书、同一条发货说明只选最完整的一段，避免反复说好吃/新鲜。",
        "hook": "★食品/生鲜Hook★ 优先选试吃反应、切开爆汁、开箱个头、产地现发、家庭囤货痛点；不要用价格、链接、下单话术当Hook。",
        "product": "★食品/生鲜Product★ 先锁定本场主商品和子类型；后续Product必须覆盖口感食欲/新鲜品质/产地溯源/规格分量/发货保鲜/场景吃法等不同角度，同一角度最多2段。",
        "close": "★食品/生鲜Close★ 结尾优先选复购信任、售后保障、冷链保鲜、囤货场景或自然购买理由；不要剪入治疗、预防、保健、药用功效表达。",
    }


# ============================================================
# 黄金链路(3必选+2可选)
# ============================================================
GOLDEN_CHAIN = [
    "hook", "bridge", "product", "close", "trend",
]
SIMPLE_CHAIN = ["hook", "product", "close"]

# ============================================================
# ASR 常见错误修正字典(持续补充)
# ============================================================
ASR_CORRECTIONS = {
    # 语音混淆(发音相近导致的误识别)
    "惊恐": "惊艳",
    "惊吓": "惊艳",
    "猩红": "心动",
    "惊呆": "惊艳",
    "恐怖": "好看",  # 上下文依赖，保守替换
    # 面料相关
    "沙洗棉": "砂洗棉",
    "纱洗": "砂洗",
    "可沙洗": "可砂洗",
    # 常见口语误识别
    "二十一": "21",
    "二一": "21",
    "上链": "上链接",
    "上连结": "上链接",
    "上連結": "上链接",
    "连结": "链接",
    "連結": "链接",
    "小黄": "小黄车",
    # 尺码相关
    "码子": "码",
    # 数字误识别
    "一百九十": "190",
    "三百七十九": "379",
    "三百七": "370",
    "裙长80": "裙长84",
    "裙长 80": "裙长84",
    "衣长一百一": "衣长110",
    # 汉麻/面料相关（4/17新增）
    "汗麻": "汉麻",
    "不补单": "不补货",
    "天撕": "天丝",
    "马内": "麻类",
    "马解": "麻刺",
}

# 主播回弹幕的废话模式(短句 + 否定/确认 + 无产品信息)
HOST_CHAT_PATTERNS = [
    re.compile(r"^(没有的|没有的事|没有啊|不是的|不是啊|不是的啊)$"),
    re.compile(r"^(知道|知道了|好的|好的呀|对对对|是是是)$"),
    re.compile(r"^(没错|没毛病|没毛病吧)$"),
    re.compile(r"^(可以的|可以的呀|行|行的)$"),
    re.compile(r"^(哈哈|哈哈哈|嘿嘿)$"),
    re.compile(r"^(谢谢|谢谢宝宝|谢谢姐妹)$"),
    re.compile(r"^(等一下|稍等|等一等的)$"),
    re.compile(r"^(看一下|我看看|看一下啊)$"),
    re.compile(r"^(好了吗|好了没|可以了)$"),
    re.compile(r"^(是的来|好吧不说了|来吧)$"),
    re.compile(r"^(那就是没有|没有现货哦)$"),
    re.compile(r"^(可以吗|对吗|好吗|是吧|啊对|啊是|嗯对)$"),
    re.compile(r"^(来|这个|那个|这些|那些|什么)$"),
    re.compile(r"^(对|嗯|啊|哦|诶|嘿|好)$"),
    re.compile(r"^(然后呢|接下来呢|所以说|因为这个)$"),
    re.compile(r"^(真的吗|真的啊|真的假的)$"),
    re.compile(r"^(你说呢|你觉得呢|懂吧|懂了吧)$"),
    re.compile(r"^(差不多|差不多吧|基本上|基本上吧)$"),
    re.compile(r"^(先这样|那就这样|就这样吧)$"),
    re.compile(r"^(我现在|我刚才|我之前|我到时候)$"),
    re.compile(r"^(你知道的|你懂的|我你懂得)$"),
    re.compile(r"^(大刘|小刘|潘桂丽|文静姐)$"),
    re.compile(r"^(姐妹\d*单现货|姐妹\d*单)$"),
    re.compile(r"^(姐妹们|姐妹\d*人)$"),
    re.compile(r"^(可以的|好的呀|行吧|好嘞)$"),
    re.compile(r"^(来来来|冲冲冲|拍拍拍)$"),
    re.compile(r"^(对不对|是不是|好不好|行不行)$"),
    re.compile(r"^(就这么说|就这么定)$"),
    re.compile(r"^(看一下|看一下哈)$"),
    re.compile(r"^(你要的话|你要的话)$"),
    re.compile(r"^(不废话了|不说了)$"),
    # 弹幕互动：回复特定观众的尺码/体型咨询
    re.compile(r"\d+斤的.{0,4}(你|直接|就).{0,6}(码|尺码|号|穿)"),
    re.compile(r"(你|直接|就)(穿|买|拍|选|拿).{0,4}(码|尺码|号)"),
    re.compile(r"(跟我|和你|跟你)(身高|体重|一模一样)"),
    re.compile(r"\d+斤.{0,4}(穿|买|选|拿).{0,3}(码|号)"),
    re.compile(r"头发打理|打理教程|下次会做视频|提醒我|催债|催追|催视频|不好意思了"),
]


# ============================================================
# 前置数据清洗(三级兜底降级，不过杀)
# ============================================================
def _pre_clean_srt(srt_text, log_fn=None):
    """
    三级兜底降级清洗:
    - 先用标准规则过滤
    - 如果通过数 < 20 条，自动放宽(取消字数限制，放宽时长)
    - 如果还 < 10 条，仅过滤黑名单过渡废话
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    from config import BAN_PATTERNS
    _kw_local = _get_keywords()
    FILLER_WORDS = _kw_local["filler_words"]
    NEGATIVE_SIGNALS = _kw_local["negative_signals"]

    def _parse_and_filter(lines, min_dur, max_dur, min_len, filter_level):
        """解析 SRT 并按给定阈值过滤"""
        entries = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if re.match(r'^\d+$', line):
                if i + 2 < len(lines):
                    time_line = lines[i + 1].strip()
                    text_line = lines[i + 2].strip() if i + 2 < len(lines) else ""
                    j = i + 3
                    while j < len(lines) and lines[j].strip() and not re.match(r'^\d+$', lines[j].strip()):
                        text_line += lines[j].strip()
                        j += 1
                    time_match = re.match(
                        r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
                        time_line)
                    if time_match:
                        start_s = int(time_match.group(1))*3600 + int(time_match.group(2))*60 + int(time_match.group(3)) + int(time_match.group(4))/1000.0
                        end_s = int(time_match.group(5))*3600 + int(time_match.group(6))*60 + int(time_match.group(7)) + int(time_match.group(8))/1000.0
                        duration = end_s - start_s

                        skip = False
                        # 时长过滤
                        if duration < min_dur or duration > max_dur:
                            skip = True
                        # 字数过滤
                        clean_text = text_line
                        for fw in FILLER_WORDS:
                            clean_text = clean_text.replace(fw, "")
                        clean_text = clean_text.strip()
                        if len(clean_text) < min_len:
                            skip = True
                        # 黑名单(仅过渡废话)
                        if not skip:
                            for ban in BAN_PATTERNS:
                                if re.search(ban, text_line):
                                    skip = True
                                    break
                        # 负面信号(仅标准/宽松模式)
                        if not skip and filter_level >= 1:
                            for sig in NEGATIVE_SIGNALS:
                                if sig in text_line:
                                    skip = True
                                    break
                        if not skip:
                            entries.append((text_line, start_s, end_s, duration))
                    i = j
                    continue
            i += 1
        return entries

    all_lines = srt_text.strip().split("\n")

    # 标准模式
    entries = _parse_and_filter(all_lines, 0.5, 25, 1, 0)
    _log(f"前置清洗(标准): {len(entries)} 条通过")

    # 一级降级:跳过负面信号
    if len(entries) < 30:
        entries = _parse_and_filter(all_lines, 0.3, 30, 1, 0)
        _log(f"一级降级(仅黑名单): {len(entries)} 条通过")

    # 二级降级:仅过滤黑名单中的过渡废话
    if len(entries) < 15:
        entries = _parse_and_filter(all_lines, 0.3, 30, 1, 0)
        _log(f"二级降级(仅黑名单): {len(entries)} 条通过")

    # 相邻片段重叠检测：Whisper medium 会在片段边界重复识别
    if entries:
        merged = []
        for entry in entries:
            if not merged:
                merged.append(entry)
                continue
            prev_text, prev_start, prev_end, prev_dur = merged[-1]
            curr_text, curr_start, curr_end, curr_dur = entry
            prev_clean = prev_text.replace(" ", "").replace("\u3000", "")
            curr_clean = curr_text.replace(" ", "").replace("\u3000", "")
            should_merge = False
            if prev_clean and curr_clean:
                # 时间重叠：前片段还没结束，后片段已开始
                time_overlap = max(0, prev_end - curr_start) / min(prev_dur, curr_dur) if min(prev_dur, curr_dur) > 0 else 0
                # 找最长公共子串
                overlap_chars = 0
                shorter = prev_clean if len(prev_clean) <= len(curr_clean) else curr_clean
                longer = curr_clean if len(prev_clean) <= len(curr_clean) else prev_clean
                for length in range(min(len(shorter), 10), 2, -1):
                    for si in range(len(shorter) - length + 1):
                        sub = shorter[si:si+length]
                        if sub in longer:
                            overlap_chars = length
                            break
                    if overlap_chars > 0:
                        break
                # Whisper边界重复特征：时间交叀 + 少量文本重叠
                should_merge = (time_overlap > 0.3 and overlap_chars >= 3)
            if should_merge:
                new_start = min(prev_start, curr_start)
                new_end = max(prev_end, curr_end)
                # Merge text: remove overlapping part from curr, then append
                if overlap_chars >= 3:
                    # Find the overlapping tail of prev and head of curr
                    overlap_str = ""
                    for length in range(min(len(prev_clean), overlap_chars + 2), max(overlap_chars - 1, 2), -1):
                        tail = prev_clean[-length:]
                        if tail in curr_clean[:length + 2]:
                            overlap_str = tail
                            break
                    if overlap_str:
                        idx = curr_clean.find(overlap_str)
                        new_text = prev_text + curr_text[idx + len(overlap_str):]
                    else:
                        new_text = prev_text
                else:
                    new_text = prev_text
                merged[-1] = (new_text, new_start, new_end, new_end - new_start)
            else:
                merged.append(entry)
        if len(merged) < len(entries):
            import builtins
            builtins._merge_count = len(entries) - len(merged)
        entries = merged

    # 短条目合并：Whisper会产生大量2-3秒碎片段，AI逐条选导致成品全是2秒碎片
    # 将相邻的短条目合并成5-8秒的话题段落，给AI更好的"积木"
    if entries:
        merged_short = []
        i = 0
        while i < len(entries):
            text, start_s, end_s, dur = entries[i]
            # 如果当前条目>=2.0秒，直接保留（缩短合并门槛）
            if dur >= 2.0:
                merged_short.append(entries[i])
                i += 1
                continue
            # 尝试向后合并，直到总时长>=5秒或遇到话题断裂
            combined_text = text
            combined_end = end_s
            j = i + 1
            while j < len(entries) and (combined_end - start_s) < 4.0:
                next_text, next_start, next_end, next_dur = entries[j]
                # 间隔>2秒视为话题断裂，不再合并
                if next_start - combined_end > 2.0:
                    break
                # 合并
                combined_text += next_text
                combined_end = next_end
                j += 1
                # 达到4秒目标就停
                if combined_end - start_s >= 2.0:
                    break
            merged_short.append((combined_text, start_s, combined_end, combined_end - start_s))
            i = j
        if len(merged_short) != len(entries):
            _log(f"SRT短条目合并: {len(entries)} → {len(merged_short)} 条 (目标2-4秒/条)")
        entries = merged_short

    # 重建 SRT
    output = []
    for text, start_s, end_s, dur in entries:
        h1, m1, s1, ms1 = int(start_s//3600), int((start_s%3600)//60), int(start_s%60), int((start_s%1)*1000)
        h2, m2, s2, ms2 = int(end_s//3600), int((end_s%3600)//60), int(end_s%60), int((end_s%1)*1000)
        output.append(f"{h1:02d}:{m1:02d}:{s1:02d},{ms1:03d} --> {h2:02d}:{m2:02d}:{s2:02d},{ms2:03d}")
        output.append(text)
        output.append("")

    return "\n".join(output)


# ============================================================
# 强制数量约束 Prompt
# ============================================================
DIRECTOR_SYSTEM_PROMPT = """你是带货短视频的最终剪辑导演。你必须理解字幕语义、设计完整叙事，并直接给出最终成片顺序。

你的主片单会被程序原样用于剪辑。程序只检查违禁内容、时间索引和时长合同，不会自行判断卖点、换Hook或重排。你还必须给出备用扩展计划；只有主片单低于时长下限时，程序才会按你指定的插入片段和叙事锚点执行。

硬性要求：
1. 只输出JSON对象，不要解释，不要Markdown。对象必须包含clips和expansion_plan两个数组。
2. 每项必须包含 clip_type、srt_indices、focus、reason、trim_priority。srt_indices只能使用输入中真实存在且未标记“不可选”的编号。
3. 数组顺序就是成片顺序：必须恰好1个Hook且在第一项，恰好1个Close且在最后一项；Close之后不能有任何Product。
4. 叙事顺序应为：强Hook提出效果或痛点 -> 第二段立即兑现Hook -> 核心效果 -> 原因/细节证据 -> 不同场景或顾虑解除 -> 自然总结。不要按字幕时间排序，要按观众理解顺序排序。
5. 每个片段必须能独立听懂。srt_indices必须连续，通常1-2条；只有补齐主谓宾或完整句尾时才允许3条。禁止不连续编号，禁止以“而且、然后、所以、但是、因为、就是、还、大头含量是、还有一种人在”等半句开头或结尾。
6. 严禁重复同一子主题。同一个显瘦部位、同一种帽子搭配、同一个面料结论只能保留最完整的一段；只有“结果 + 解释结果的具体证据”可以保留两段，而且两段必须提供不同信息。Close也不能只是重复Hook原句，必须形成总结或新的选择理由。
7. 偏好是主线，不是凑数量。同一偏好下必须选择不同子主题；如果只有一个干净子主题，就只选一个，不得用三段近义表达冒充三段偏好。
8. 不选直播操作、主播自言自语或现场调度，例如“切个歌、我把包取了、帮我拿一下、看后台、今天没洗头”。不选“我喜欢它两个点、首先、第一点、几个地方”这类报数式铺垫，除非后续所有点都在紧邻片段中完整展开。不选价格、链接、拍码、关注、领券、满减、倒计时和任何标记不可选的字幕。
9. 目标时长约__TARGET__秒，最终片单必须控制在__LOW__-__HIGH__秒；低于__LOW__秒或超过__HIGH__秒都会被拒绝。内容不足时继续寻找不同卖点、不同场景、不同顾虑解除的完整片段；内容过多时删除整段低价值Product，绝不能截断单句、重复凑数或靠超时长蒙混过关。
10. trim_priority表示超时长时的删片优先级：Hook、紧随Hook的第二段、Close必须填0；其余Product填互不重复的正整数，1代表最先删除。先给重复、偏离本轮偏好、低信息量的片段较小数字，核心偏好证据填较大数字。程序只会按你给出的顺序累计删整段，不会替你判断内容价值。
11. expansion_plan提供4-8个主片单未使用的完整Product备用片段。每项包含priority、after_srt_indices、after_order、srt_indices、focus、reason；priority从1开始且不重复，数字越小越优先补入。after_srt_indices必须指向clips中某个现有片段的srt_indices，表示补片应插在该段之后；不得锚定Close，也不得把补片放到Close后。备用片段必须覆盖不同子主题，不得与主片单或其他备用片段重复。

输出前在心里把所有选中字幕按数组顺序连读一遍，并逐项确认：Hook后有兑现、相邻段不重复、每句首尾完整、Close确实是最后一句。发现问题后先修改，再输出JSON。"""


SYSTEM_PROMPT = """你是抖音女装带货短视频专业编导，严格执行以下规则，禁止自由发挥.

[零,选片策略(内心推理，不要输出推理过程)]
按以下顺序思考，但★不要输出思考过程，直接输出JSON数组★：
1. 品类统计→确定主打单品 2. Hook扫描→选冲击力最强的 3. 链路规划→hook/product/close分配 4. 叙事编排→按话题分组+时间相邻优先，确保上下句能接上 5. 重复排查→去重叠 6. 直接输出JSON

★叙事编排核心原则★:
- 视频结构: 开场圈人(1-2段) → 提高预期(2-3段) → 产品介绍(6-10段) → 促单收尾(1-2段)
  - 开场圈人: 自由选最抓人的Hook，不受时间约束
  - 提高预期: 选"为什么好"的铺垫内容，形成递进
  - 产品介绍: ★按话题分组排列★先排A主题(如面料/版型)，再排B主题(如搭配效果)，不混排
    * Step1: 阅读所选Product片段的文本，识别属于哪些主题（面料、版型、颜色、搭配效果等）
    * Step2: 将同一主题的Product放在一起，讲完一个主题再换下一个
    * 例：面料→触感→版型→尺码推荐
  - 促单收尾: 自由选最好的促单，不受时间约束
- 同话题片段优先选时间相邻的SRT条目（说话语气和场景对得上）
- 片段A的最后一句和片段B的第一句应该能自然衔接，不可跳跃话题
- 宁可牺牲一点"最精彩"，也要保证整条视频连贯流畅

[一,品类一致性(最重要)]
1. 先通读所有字幕，判断本场直播有哪几个品类(如裤子,上衣,裙子等)
2. 选择出现次数最多的品类作为"主打单品"
3. 所有片段必须围绕同一个主打单品，禁止混入其他品类的内容
4. 即使其他品类的文案再好，也不能选

[二,多视频源选片规则]
1. 字幕条目中有 [V1] [V2] [V3] 等标记，代表来自不同视频素材
2. ★必须从每个有标记的视频源中各选至少3-4个片段★，确保混剪均衡使用每个素材
3. 如果某个视频源的精华内容确实很少，可以少选但至少选1-2个
4. 检查每个片段的文本是否含 [Vn] 标记来判断来源，不要只看时间顺序

[二,数量与时长]
1. 输出10-15段片段，每段必须是完整语义单元（10-15会被按目标时长自动替换，见下方规则）
2. 整条视频总时长45-65秒（唯一硬性时长约束，会被按目标时长自动替换）
3. 如果主打单品的同类型好片段不够，宁可重复不同角度的卖点，也不要混入其他品类
4. ★宁可片段多但每段完整，也不要片段少但句句断★
5. ★Product必须选6-10段★（短而精，每段只说1-2个要点，多角度覆盖卖点）

【动态数量规则（AI自动遵守上方替换后的数值）】
- 目标30-45秒: 选8-12段，每段3-5秒 = 总时长达标
- 目标60秒: 选14-18段，每段3-5秒 = 总时长达标  
- 目标90秒: 选18-24段，每段3-5秒 = 总时长达标
- 目标120秒: 选24-30段，每段3-5秒 = 总时长达标
★片段数不够=时长不够=不合格，宁可多选到上限也不要少选★

[三,黄金链路结构]
采用"必选+可选"灵活组合模式：

【必选环节】（必须覆盖，缺一不可）
1. Hook(开头抓人): ★★★ 最关键环节，决定用户是否划走 ★★★
   - 理想Hook只有1-3秒！一个短爆点比长铺垫有效10倍
   - 最佳范例："太漂亮了！"/"太显瘦了！"/"这也太惊艳了吧！"/"绝了！"/"假不了！"
   - ★扫描SRT中1-3秒的短条目，找含强烈情绪词的作为Hook★
   - ★关键规则：如果一个条目含爆点词（美爆了/绝了/太漂亮/太显瘦/封神/神仙/炸了等），即使内容较长也应优先选作Hook，而不是Product★
   - 也可以选4-8秒的完整Hook，但坚决不要8秒以上的Hook
   - 七类高价值Hook(优先级从高到低):
     ① 痛点提问型(最强Hook): 直击身材/穿搭痛点，让用户"说的就是我"
        → 身材痛点: "胯宽腿粗还在乱穿？"、"腰腹有肉穿啥显壮？"、"小个子不敢穿长裙？"、"拜拜肉怎么藏？"、"屁股大的看过来"、"腿粗的看过来"、"肩宽背厚的"
        → 穿搭痛点: "显瘦"、"显白"、"腿粗也能穿"、"不敢穿XX"、"怎么穿都显胖"、"不知道搭什么"
        → 圈人群: "110斤显瘦80斤"、"小个子也能穿"、"胯宽的姐妹"
     ② 效果前置型(视觉最强): 第一秒展示最佳画面/效果，靠颜值留人
        → "原相机无滤镜！上身太显瘦了"、"谁懂！转身那一下氛围感拉满"、"130斤穿这条秒变沙漏腰"、"穿上直接变韩剧女主"
        → 信号词: 原相机、上身、显瘦、秒变、氛围感、绝了、拉满
     ③ 对比反差型(冲击力最强): 用对比凸显产品优势，易出爆款
        → 版型对比: "同样是裙子，普通款显壮，我们这款收腰遮肉差别太大"、"比市面上的加宽"
        → 身材对比: "不是你身材不好，是你没穿对版型"
        → 信号词: 同样、比市面、外面买不到、专柜、其他家、别家的
     ④ 悬念福利型(拉停留): 制造好奇或紧迫感
        → 悬念: "别划走！这件我只在直播间穿一次"、"刚上架就抢空"
        → ⚠️过滤CTA: "破价"、"仅限"、"福利"、"限时"等价格/促销词不算悬念Hook，是价格CTA
     ⑤ 爆料型(强停留): "XX%是假的"、"行业秘密"、"全都是假的" → 制造好奇
     ⑥ 信任型(情绪感染): "被扣爆了"、"卖疯了"、"盲拍"、"不搞虚"、"自留了" → 拉信任
     ⑦ 夸奖型(氛围烘托): "太好看"、"绝绝子"、"太爱了" → 最弱但可用
     ⑧ 包容承诺型(强情绪Hook): "我不管你肩膀宽窄我都给你"、"想怎么穿都行"、"随便你" → 重复强调+包容性语气，极其抓人
   - ★绝对禁止★:用产品/面料/款式做Hook开场(如"这个西装"、"这件风衣"、"面料很好")——人群圈定比产品介绍重要
   - ★绝对禁止★:话头接续句("就像我的话...","然后...","所以呢...","看一下...","来...","好...")，不完整半句，平淡开场
   - ★Hook最低标准★:如果这句话单独出现在抖音信息流，用户会不会停留?不会就换一条
   - ★必须搜遍全片找所有Hook候选★:不要只找1个最强的，要找出所有可作为Hook开场的句子(3-5个)，不同类型(痛点提问/效果前置/对比反差/悬念福利/爆料/信任/夸奖)各选最佳1个
   - ★Hook选片铁律★:
     a) 直播开头80%是暖场废话，不要优先选时间最早的第一句
     b) 允许合并相邻2-3句字幕提炼成一句更有冲击力的Hook
     c) 好Hook vs 坏Hook对比:
        好:"屁股大的看过来"(痛点提问) 好:"110斤显瘦80斤"(效果前置+圈人群) 好:"吃土都要买一条"(极端表态) 好:"同样裙子这款收腰遮肉差别太大"(对比反差) 好:"刚上架就抢空"(悬念)
        坏:"这个西装真的很好看"(产品开场) 坏:"然后这一整身"(接续句) 坏:"面料很舒服"(无钩子) 坏:"今天破价了"(价格CTA非悬念)

2. Product(产品种草): 选择6-10个片段，全链路消除用户购买顾虑
   ★卖点多样性（最重要）★: 单视频必须覆盖2-4个不同卖点角度（如版型显瘦+颜色氛围+穿搭场景+面料），同一角度最多2段，面料最多2段，禁止全片只讲面料
   核心手段:穿搭介绍+性价比对比+效果对比，三种方式交替使用效果最佳
   优先级从高到低(不强求每个类型都有):
   ① 上身效果(最强种草，画面优先级最高): "上身真的显瘦"、"穿上看效果"、"试穿给你们看"、"转身看后面" → 画面冲击力最强，★优先选择★
   ② 价值锚点(强种草，爆款必选): "这个品质外面买不到"、"同款对比贵一倍"、"闭眼入不踩雷" → 强调"值"的印象，绝对不报具体价格
   ③ 对比突出(差异化+冲击力): "比市面上的加宽"、"市面版本拉不开"、"同样是XX，我们这款…" → 对比类Product和对比类Hook搭配效果翻倍
   ④ 代工厂/产地背书(强信任): "一线品牌代工厂"、"给XX做婚纱的工厂"、"意大利工艺" → 信任感拉满
   ⑤ 痛点解决(强转化): "腿粗绝对可以穿进去"、"120斤也能穿" → 直接回答用户顾虑
   ⑥ 版型/面料/细节: 讲解设计、材质、做工 → 建立产品认知
   ⑦ 场景想象(画面感): "法国女生的浪漫感"、"办公室喝茶的雅" → 感觉比参数更打动人
   ⑧ 穿搭展示: 多种风格搭配、跨季节可穿 → 证明百搭实穿性
   ⑨ 尺寸长度(细节加分): "裙长到脚踝"、"刚好露脚踝的恰到好处"、"遮住小腿肚"、"衣长刚好盖住胯" → 精准描述长度的片段，体现对身材的理解，细节卖点高于通用卖点★
   ⑩ 工艺细节(品质差异): "这个工艺成本高"、"双线压褶定型"、"定染颜色市面上没有"、"五金拉链质感" → 体现做工精致和成本投入的细节
   ⑪ 穿着体验(信任基石): "穿上不想脱"、"活动自如"、"不勒不绷"、"穿了跟没穿一样" → 穿戴感受用词更打动人
   ⑫ 对比优势(购买理由): "同价位买不到"、"外面没有这个品质"、"跟专柜一比省一半" → 强调不可替代性的内容
   ★细节卖点优先级高于通用卖点★ 通用卖点(面料好、显瘦等)每个最多1段，细节卖点(尺寸、工艺、穿着体验、对比优势)优先选择，同一细节可多角度覆盖
   ★★★绝对禁止★★★: 所有尺码推荐、码数建议(卡码拍小、正码正拍、买大买小、S/M/L尺码等)禁止放在Product片段里，只允许出现在Close

3. Close(促单收尾): ★必须选择1-2个片段★核心是消除顾虑+推动决策，绝不能空缺
   - ★尺码引导只能放在Close区域(最后1-2段)★
   - 紧迫感(最有效): "快没了"、"马上断码"、"剩下不多"、"拍完就没有了" → 稀缺感推动
   - 闭眼入(强信心): "闭眼入"、"不买后悔"、"我自留了" → 极致断言
   - 信任强化: "一定是真的"、"没人舍得退"、"放心拍" → 最终信任确认
   - 尺码推荐: "按推荐尺码买就行"、"卡码买小不要买大" → 消除尺码顾虑
   - 风格定位: "喜欢就拍"、"错过就没了"、"这个风格真的绝" → 情感推动
   - 场景收尾: "约会穿这个绝了"、"上班穿也没毛病" → 最后画面感
   - 🚫绝对禁止:具体价格数字(199/299等)、购物车、链接、下单、优惠、限时价、321上车、打折、几折、半价、到手价、破价、十几块/几十块/一百多——任何提到钱/价格/促销的统统禁止！哪怕是"二十块""一百多""很划算"这种口语化表达也绝对不行
   - ✅允许:紧迫感+信任+尺码+风格+场景——不提价格也能促转化
   - 如果原视频有尺码引导，必须放在最后

【可选环节】（有就保留，没有不强求）
- Bridge(过渡衔接): 科普类、提问类 → 连接Hook和Product
- 信任话术: "盲拍"、"不搞虚"、"自留款" → 可穿插任意环节，不要独立成段
- trend(流行趋势): 当季流行、设计款 → 有则保留

[四,片段时长规范]
1. ★★★核心原则：每个片段必须是完整的语义单元，时长由内容自然决定★★★
2. ★Product必须短而精★：每个Product只说1-2个要点，讲完就切，不要在一个Product里堆3-4个卖点
3. total时长控制45-65秒就够了，Product多就每个短点，Product少就每个长点
4. ★每个Product只选1-2个SRT条目★，不要把一个Product拉得太长
5. ★不要为了凑时长而硬选长片段★——宁可10段短的（每段3-5秒讲清1个卖点），也不要6段长的（每段10秒讲3个卖点）

[五,绝对禁止]
1. 禁止只选1-7段，最低8段
2. 禁止选过渡废话:再开新款,过款,接下来,好吧,"然后反正这身","好的然后","来给大家看一下"
3. 禁止混入其他品类的片段——反复检查每个片段是否属于主打单品
4. 禁止选不完整的半句话:每个片段首尾必须是完整句子边界
5. 禁止选主播回弹幕/互动:如"没有的啊","知道知道","105斤的你直接M码"等回复弹幕、尺码咨询、跟观众对话的内容
6. 禁止选纯ASR错误片段:读起来完全不通则跳过
7. 禁止选语句不完整片段:缺主语/谓语/补语，最后一个片段禁止以"你会觉得""就感觉""呢""吧""啊"等未完成语气结束
   ★也禁止以收尾废话结束:"没有了""好快""断了""我觉得你们""知道吧"——这些不是有价值内容，不要选进来★
8. 禁止保留具体品牌名:如"香奈儿""古驰"→替换为"大牌平替""秀场款"或删除
9. ★★★禁止选任何含价格信息的片段★★★："20块""一百多""十几块""三折""半价""很划算""性价比高"——不管多口语化，只要提到钱就不选！代码会硬删除，选了也白选
    10. 禁止语义重复:同一卖点用不同说法说了多遍，只保留信息量最高的第一次
   - 例外:连续夸奖堆叠("太好看了""绝了""巨好看")是有效种草手法，不算重复
   - 真正重复:同一事实用不同话术复述(如"面料不厚"→"比较薄透"→"很轻盈")
11. 禁止选无实质内容语气词:单独的"嗯""对""啊""当然"
12. 禁止同主题内容被无关内容打断:讲面料的段落必须相邻，不能穿插其他话题
13. 禁止尺码/价格信息出现在前半段:尺码只能在最后2-3段，价格/购物车绝对禁止

[六,叙事连贯与节奏(★★★决定成品质量的关键★★★)]
1. 每个片段必须是完整的一句话或完整意思
2. 片段间要有递进感:hook(抓人)→ product(种草，同主题相邻)→ close(促单)
3. ★Hook后面紧跟着的Product必须扣题，同Hook话题★（如Hook讲抗皱，第二个必须是抗皱演示）
4. 禁止相邻片段内容重复或高度相似
5. 每段文案应该是"能直接作为短视频配音"的流畅语句
6. ★只选有价值的完整句子★——每个片段的核心价值句必须完整
7. ★start/end必须对齐句子真实起止★——不要往前延伸到上一句，也不要往后延伸到下一句
   ★尤其不要把主播的收尾废话选进来："没有了""断了""好快""我觉得你们""知道吧""好吧"等不是有效内容★
8. 结尾片段必须自然收束——最后一句没说完就去掉或往前找完整结束点
9. ★★★叙事连贯性(最重要)★★★:
   - 同话题必须相邻且时间连续:讲面料的放一起、讲版型的放一起、讲颜色的放一起
   - ★同类型片段必须聚拢★：抗皱(30-41s)+抗皱(41-51s)必须紧挨着，不能中间夹一个版型/穿搭片段
   - ★尽量选SRT条目编号接近的片段★——编号相邻=原文中上下连贯
   - ★最优叙事递进链模板(按优先级排列)★:
     ① Hook(痛点/效果抓人) → ② 问题解决(针对Hook痛点讲方案) → ③ 效果验证(上身/对比) → ④ 细节强化(面料/版型) → ⑤ 促单收尾(尺码/紧迫感)
     → 这5步不要求全有，但顺序不能乱：方案必须在Hook之后，效果在前半段，细节在中段，促单在最后
     → 段落衔接关键：前一段结尾留话题尾巴，后一段开头接着这个话题开始
     → 示例：“胯宽腿粗的看过来” → “我这条裤子专门遮肉的” → “看侧面完全不显胯” → “面料有弹性不勒肉” → “按尺码表选直接入”
   - 片段A讲完面料，片段B突然跳到尺码=语境断裂；片段A讲面料手感，片段B接着讲面料垂感=连贯递进
   - 编排顺序: 同话题聚拢→话题间用1句过渡→形成"面料手感→面料垂感→版型显瘦→尺码推荐"的递进链
10. 信息密度要高:每3-5秒必须有新信息，禁止超过8秒连续讲同一个点

[七,通顺度质检(输出前必须执行)]
在输出JSON之前，按以下步骤检查：
1. 通读一遍你选的所有片段的文本，用肉眼看是否像一段连贯的口播
2. 如果相邻两个片段的文本之间有明显"断感"（话题、语气、情绪不连贯），必须：
   a) 优先换一个时间上更接近的片段来填补
   b) 或者删除其中一个片段，只保留更衔接的那个
3. ★时间跳跃检查★：相邻片段在原始视频中的时间差超过60秒的，除非内容高度相关否则必须替换
4. ★完整句检查★：每个片段的第一句话和最后一句话都必须是完整句子，不能以"呢""吧""啊""所以""然后"等未完成语气开头或结尾

[七,ASR纠错]
1. 根据上下文修正明显的ASR识别错误(如"惊恐"应为"惊艳")
2. 去掉不必要的语气词和重复
3. 保留口语化风格，确保读起来通顺自然
4. 不确定原文意思则宁可不修正
5. 重复检测:核心信息相同的片段只保留更完整的那条

[八,质量自检(输出前必须执行)]
1. Hook检查:第一段能否让陌生人停下来?太淡则重新搜索全片最强Hook
2. 重复检查:相同信息出现2次以上?只保留第一次
3. 时长检查:总时长超过65秒?优先删除:重复段→过渡废话段→信息量最低的product段。注意:不要通过砍断单个片段来缩短时长，而是删除整个片段
4. 内容检查:每段有实质内容?纯语气词/纯过渡句删除
5. 位置检查:尺码/价格是否在最后2-3段?前半段出现则移到末尾
6. ★什么是"完整语义单元"★:每个Product片段的text必须能独立成句，通读一遍意思完整
   ✅ 完整:"这件衣服上身效果特别好，谁穿谁好看"
   ✅ 完整:"版型设计很显瘦，侧面看完全不显胯"
   ❌ 不完整:"这件衣服上身效果特别好"（缺下半句，接不上）
   ❌ 不完整:"而且是很适合像有些宝宝你想要"（缺谓语/宾语）
   ❌ 不完整:"所以我觉得这个衣服"（未完成，不能独立存在）
   ❌ 不完整:"穿起来…"（"穿起来怎么样"没说出来）
   ✅ 检查方法:把片段text读出声，如果感觉"然后呢?""想说什么?"就是没说完
   ✅ 修复方法:往前或往后延伸到完整句子边界(用句号/感叹号/问号判断)
7. 结尾检查:最后1-2段包含促单信号?没有则找合适促单内容作结尾
8. 完整性检查:每段是完整句子?半截话删除
9. 品牌名检查:文案有具体品牌名?替换为通用描述或删除
10. ★衔接检查:相邻两段拼接后读一遍，语境是否断裂?如果片段A结尾和片段B开头完全接不上→换一个时间更近的同话题片段

[九,输出格式]

★★★重要：直接输出JSON数组！不要输出任何推理过程、分析步骤、解释说明！第一行就是[★★★

★严格数量要求：你必须输出（10-15段）总时长（45-65秒），这是硬性约束！如果输出数量不足，视频时长不够，整个视频就废了！输出10-15段→实际输出10-15段，输出18-24段→实际输出18-24段，输出22-30段→实际输出22-30段。输出数量由上方[二,数量与时长]决定，此处替换生效。★

★单版本模式★:只输出JSON数组，每个片段必须包含 focus 和 reason 字段:
[
  {"clip_type": "hook", "start": "秒数", "end": "秒数", "text": "修正后的文案，用【】标注重点词如【超级无敌冰】", "focus": "版型", "reason": "圈人群+效果对比，2秒内传达完整信息"},
  ...
]

★多版本模式★:输出3个独立叙事方案，每个方案从不同角度讲同一个产品:
{
  "versions": [
    {
      "angle": "方案角度描述(如:面料质感+高级感)",
      "clips": [
        {"clip_type": "hook", "start": "秒数", "end": "秒数", "text": "修正后的文案，用【】标注重点词", "focus": "版型", "reason": "选择理由"},
        ...
      ]
    },
    ...
  ]
}

多版本规则:
- 3个方案的角度必须不同(如:方案A侧重面料质感，方案B侧重版型显瘦，方案C侧重性价比+信任)
- 每个方案必须独立完整: 1个Hook + 2-4个Product + 1个Close
- Product片段允许跨方案共享(最多共享2段)，确保每个版本种草内容充实
- Hook和Close不可跨方案共享(保证开头结尾差异化)
- 每个方案6-10个片段，总时长45-65秒，每段必须是完整语义单元
- 如果素材只够2个完整方案，就只输出2个，不要凑第3个
- 角度由你根据直播内容自行判断，选择素材最丰富的3个角度

clip_type:
- "hook": 开头抓人片段
- "product": 产品种草片段
- "close": 促单收尾片段
- "bridge": 过渡衔接片段(可选)

focus(必填):
- "版型": 版型、剪裁、廓形、显瘦遮肉
- "面料": 材质、手感、触感、起球、克重
- "颜色": 颜色、花色、图案、条纹
- "显瘦": 显瘦、显高、遮胯、藏肉、修饰身材
- "场景": 通勤、约会、度假、日常
- "搭配": 搭配、组合、套穿、配什么
- "对比": 对比市面产品、反面案例、性价比优势（不报具体价格）
- "品质": 做工、走线、细节、质感
- 🚫"价格": 绝对禁止选择！价格/到手价/性价比/优惠/折扣一律不得作为片段类型，选了也会被代码硬删除
- "痛点解决": 直接回答用户顾虑
- "信任": 盲拍、不搞虚、自留款、私服
- "其他": 不属于以上类别
涉及多个卖点时选最核心的一个"""

# : 去除主播重复讲述的段落
# ============================================================
def _dedup_srt_repeated_sections(cleaned_srt, log_fn):
    """检测 SRT 中重复出现的连续段落并删除（主播经常重复讲同一批卖点）"""
    def _log(msg):
        if log_fn: log_fn(msg)

    lines = cleaned_srt.strip().split("\n")
    if len(lines) < 12:
        return cleaned_srt

    # 解析 SRT 为段落列表
    segments = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}', line):
            text = ""
            if i + 1 < len(lines):
                text = lines[i + 1].strip()
            j = i + 2
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                next_line = lines[j].strip()
                # Stop if next line is a segment number (digits only)
                if re.match(r'^\d+$', next_line):
                    break
                text += next_line
                j += 1
            segments.append((line, text, i))
            i = j
        else:
            i += 1

    if len(segments) < 6:
        return cleaned_srt

    def norm(text):
        return re.sub(r'[\s\W]+', '', text).lower()

    fingerprints = [norm(seg[1]) for seg in segments]

    removed_segs = set()
    for win_size in range(min(8, len(segments) // 2), 2, -1):
        seen_windows = {}
        for i in range(len(segments) - win_size + 1):
            if any(j in removed_segs for j in range(i, i + win_size)):
                continue
            fp = tuple(fingerprints[j] for j in range(i, i + win_size))
            fp_key = tuple(f[:min(8, len(f))] for f in fp)

            if fp_key in seen_windows:
                first_start = seen_windows[fp_key]
                gap = i - first_start
                if gap >= win_size:
                    total_sim = 0
                    for k in range(win_size):
                        a = fingerprints[first_start + k]
                        b = fingerprints[i + k]
                        if not a or not b:
                            continue
                        sa, sb = set(a), set(b)
                        if sa and sb:
                            total_sim += len(sa & sb) / max(len(sa | sb), 1)
                    avg_sim = total_sim / win_size
                    if avg_sim > 0.35:  # 35% threshold for fuzzy repeat detection
                        for j in range(i, i + win_size):
                            removed_segs.add(j)
                        _log(f"SRT预去重: 删除第{i+1}-{i+win_size}段(与第{first_start+1}-{first_start+win_size}段重复, 相似度{avg_sim:.0%})")
            else:
                seen_windows[fp_key] = i

    # [增强] 整段重复检测：逐段两两比对
    if len(segments) >= 4:
        for i in range(len(segments)):
            if i in removed_segs:
                continue
            for j in range(i + 1, len(segments)):
                if j in removed_segs:
                    continue
                fi = fingerprints[i]
                fj = fingerprints[j]
                if not fi or not fj:
                    continue
                si_set, sj_set = set(fi), set(fj)
                if not si_set or not sj_set:
                    continue
                overlap = len(si_set & sj_set) / max(len(si_set | sj_set), 1)
                # 相邻段用低阈值(0.5)，非相邻用高阈值(0.65)
                threshold = 0.5 if (j - i) <= 1 else 0.65
                if overlap > threshold:
                    removed_segs.add(j)
                    _log(f"SRT预去重[整段]: 删除第{j+1}段(与第{i+1}段重复, 相似度{overlap:.0%})")

    if not removed_segs:
        return cleaned_srt

    _log(f"SRT预去重: 共删除 {len(removed_segs)} 个重复段落, {len(segments) - len(removed_segs)} 个保留")

    # [增强] 包含检测: 如果段A的文本完全被段B包含，删除较短的A
    contain_removed = 0
    for i in range(len(segments)):
        if i in removed_segs:
            continue
        fi = fingerprints[i]
        if not fi or len(fi) < 3:
            continue
        for j in range(len(segments)):
            if i == j or j in removed_segs:
                continue
            fj = fingerprints[j]
            if not fj or len(fj) < 3:
                continue
            # 检查包含: fi 是 fj 的子串或相反
            if fi in fj or fj in fi:
                # 删除较短的那个
                shorter = i if len(fi) < len(fj) else j
                if shorter not in removed_segs:
                    removed_segs.add(shorter)
                    contain_removed += 1
    if contain_removed:
        _log(f"SRT预去重[包含]: 删除 {contain_removed} 个被包含的短片段")

    # [v9.2] 口吃检测: 在前后3段范围内检测子串和完全相同
    # 规则: 子串匹配只删短段; 完全相同删后出现的; 不误杀长段
    removed_fps = set(fingerprints[idx] for idx in removed_segs)
    stutter_removed = 0
    for i in range(len(segments)):
        if i in removed_segs:
            continue
        fi = fingerprints[i]
        if not fi or len(fi) < 2:
            continue
        should_remove = False
        # 1. 与已删段完全相同
        if fi in removed_fps:
            should_remove = True
        # 2. 与前后3段内的段比对
        if not should_remove:
            for j in range(max(0, i - 3), min(len(segments), i + 4)):
                if j == i or j in removed_segs:
                    continue
                fj = fingerprints[j]
                if not fj or len(fj) < 2:
                    continue
                # 完全相同: 删后出现的(i > j)
                if fi == fj and i > j:
                    should_remove = True
                    break
                # 子串匹配: 只删较短的那个
                shorter, longer = (fi, fj) if len(fi) < len(fj) else (fj, fi)
                shorter_idx = i if len(fi) < len(fj) else j
                if len(fi) == len(fj):
                    continue  # 等长且不完全相同,跳过
                if len(shorter) >= 3 and (longer[:len(shorter)] == shorter or longer[-len(shorter):] == shorter):
                    if shorter_idx == i:
                        should_remove = True
                        break
                    # shorter_idx == j: j已未被删,不删j(保留长段)
        if should_remove:
            removed_segs.add(i)
            removed_fps.add(fi)
            stutter_removed += 1
            _log(f"SRT预去重[口吃]: 删除第{i+1}段")
    if stutter_removed:
        _log(f"SRT预去重[口吃]: 删除 {stutter_removed} 个口吃重复段")

    removed_line_indices = set()
    for seg_idx in removed_segs:
        time_line, text, start_idx = segments[seg_idx]
        removed_line_indices.add(start_idx)
        removed_line_indices.add(start_idx + 1)

    removed_line_indices = set()
    for seg_idx in removed_segs:
        time_line, text, start_idx = segments[seg_idx]
        removed_line_indices.add(start_idx)
        removed_line_indices.add(start_idx + 1)

    output_lines = []
    for i, line in enumerate(lines):
        if i not in removed_line_indices:
            output_lines.append(line)

    return "\n".join(output_lines)


# ============================================================
# 品类过滤:从 SRT 源头移除非主品类片段
# ============================================================
def _filter_srt_by_main_product(cleaned_srt, log_fn, force_category=None):
    """四维品类判定:成交铁证 > 下款预告排除 > 深度讲解 > 基础词频"""
    def _log(msg):
        if log_fn: log_fn(msg)

    global _LAST_CATEGORY_FILTER_SUMMARY
    _set_last_category_summary({})
    forced_main_cat = _normalize_forced_category(force_category)

    # ============================================================
    # 1. 词库配置
    # ============================================================
    # 品类词库（统一使用全局 PRODUCT_CATEGORIES）

    # 搭配触发词(搭配+品类 → 该品类不计分)
    MATCH_WORDS = ["搭","配","搭配","配着穿","搭什么","配什么","同款","一套","两件套"]

    # 下款预告词(明确预告+品类 → 该品类全程排除)。不能用“看下/接下来”
    # 这类泛动词，否则“你看下这个领口”会被误判成下一款预告。
    NEXT_PREVIEW = [
        "下一个开", "接下来开", "过款", "过下款", "过下一款", "下一款",
        "马上开", "下个款", "下一个款", "下一件", "下一条", "下一套",
        "看下一款", "看下一个款",
    ]

    def _next_preview_reason(text: str, cats_found: list[str]) -> str:
        """Detect next-product transition lines such as '裤子马上来/没开/还有裤子呢'."""
        if not cats_found:
            return ""
        clean = str(text or "").strip()
        compact = re.sub(r"\s+", "", clean)
        for word in NEXT_PREVIEW:
            if word and word in compact:
                return word
        transition_prefixes = ("哦对了", "对了", "还有", "还", "另外", "等下", "等会", "待会", "一会儿", "稍后")
        unopened_words = ("没开", "还没开", "没有开", "未开", "没上", "没讲", "没开始")
        coming_words = ("马上来", "马上上", "马上开", "等下来", "等下上", "等会来", "等会上", "待会来", "待会上", "一会儿来", "稍后来", "稍后上")
        trailing_words = ("呢", "哈", "啊", "哦")
        for cat in cats_found:
            for kw in PRODUCT_CATEGORIES.get(cat, []):
                if not kw or kw not in compact:
                    continue
                if re.search(re.escape(kw) + r".{0,8}(?:" + "|".join(map(re.escape, unopened_words + coming_words)) + r")", compact):
                    return f"{kw}+未开/马上来"
                if any(compact.startswith(prefix) or prefix + kw in compact[:12] for prefix in transition_prefixes):
                    if re.search(re.escape(kw) + r".{0,4}(?:" + "|".join(map(re.escape, trailing_words + unopened_words + coming_words)) + r")", compact):
                        return f"{kw}+还有/转场"
        return ""

    # 成交铁证词(+50分，绑定最近品类词)
    SELLING_PROOF = {
        "开价": ["划算","超值","性价比","值得","不贵"],
        "行动": ["321","拼手速"],
        "服务": ["报尺码","现货","发货","平铺晾","机洗","尺码","码数","不多了","没货","截单","断码","库存"],
        "食品": ["现摘","现采","现发","当天发","冷链","保鲜","锁鲜","产地","果园","基地","净重","规格","斤装","箱装","坏果包赔","破损包赔","回购","复购","好吃","新鲜"],
    }
    SELLING_PROOF_ALL = []
    for v in SELLING_PROOF.values():
        SELLING_PROOF_ALL.extend(v)

    # 品类词加权：精准词高分，泛词低分，减少“裤/裙/鞋/吊带”等误判。
    GENERIC_CATEGORY_WORDS = {
        "裤", "裙", "鞋", "吊带", "背心", "马甲", "针织", "打底",
        "三件", "四件", "组合", "穿搭",
        "食品", "生鲜", "食材", "水果", "零食",
    }

    def _category_keyword_weight(cat: str, keyword: str) -> float:
        if not keyword:
            return 0.0
        if cat == "食品/生鲜":
            if keyword in {"食品", "生鲜", "食材", "水果", "零食"}:
                return 0.7
            if keyword in {"虾", "蟹"}:
                return 0.65
            if keyword in {"现摘", "现采", "现发", "冷链", "保鲜", "产地", "果园", "基地", "坏果包赔"}:
                return 1.5
            if len(keyword) >= 3:
                return 1.35
            return 1.0
        if keyword in GENERIC_CATEGORY_WORDS or len(keyword) <= 1:
            return 0.45
        if len(keyword) >= 4:
            return 1.45
        if keyword.endswith(("裤", "裙", "鞋", "衫", "套", "服", "衣", "褲")) and len(keyword) >= 3:
            return 1.25
        if cat == "套装" and keyword in {"套装", "两件套", "三件套", "四件套", "整套", "全套", "成套"}:
            return 1.4
        return 1.0

    # ============================================================
    # 2. 解析 SRT 为段落
    # ============================================================
    lines = cleaned_srt.strip().split("\n")
    segments = []  # [(time_line, text, line_indices)]
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}', line):
            text = ""
            if i + 1 < len(lines):
                text = lines[i + 1].strip()
            j = i + 2
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                text += lines[j].strip()
                j += 1
            segments.append((line, text, list(range(i, j))))
            i = j
        else:
            i += 1

    if not segments:
        return cleaned_srt, None

    # ============================================================
    # 3. 逐段分析
    # ============================================================
    # 记录每个段的属性
    seg_info = []  # {text, cats_found, has_match, has_preview, has_proof, preview_cat}
    for time_line, text, line_indices in segments:
        info = {
            "text": text, "time_line": time_line, "line_indices": line_indices,
            "cats_found": [],      # 本段出现的品类
            "has_match": False,    # 是否有搭配词
            "has_preview": False,  # 是否有下款预告
            "preview_reason": "",
            "has_proof": False,    # 是否有成交铁证
            "preview_cats": [],    # 被预告的品类
            "proof_cats": [],      # 成交铁证绑定的品类
            "cat_weights": {},     # 本段每个品类的最高关键词权重
            "cat_keywords": {},    # 本段每个品类命中的最高权重关键词
        }
        for cat, keywords in PRODUCT_CATEGORIES.items():
            best_kw = ""
            best_weight = 0.0
            for kw in keywords:
                if kw in text:
                    weight = _category_keyword_weight(cat, kw)
                    if weight > best_weight:
                        best_weight = weight
                        best_kw = kw
            if best_weight > 0:
                info["cats_found"].append(cat)
                info["cat_weights"][cat] = best_weight
                info["cat_keywords"][cat] = best_kw
        # 搭配检测
        for mw in MATCH_WORDS:
            if mw in text:
                info["has_match"] = True
                break
        # 下款预告检测
        _preview_reason = _next_preview_reason(text, info["cats_found"])
        if _preview_reason:
            info["has_preview"] = True
            info["preview_reason"] = _preview_reason
            # 预告绑定的品类 = 文本中的品类(除搭配外)
            for cat in info["cats_found"]:
                info["preview_cats"].append(cat)
        # 成交铁证检测
        for sp in SELLING_PROOF_ALL:
            if sp in text:
                info["has_proof"] = True
                # 成交铁证绑定最近品类 = 本段中的品类(排除搭配和预告)
                for cat in info["cats_found"]:
                    if cat not in info.get("preview_cats", []):
                        info["proof_cats"].append(cat)
                # 如果本段无品类，向前/向后找最近的品类
                if not info["proof_cats"]:
                    idx = segments.index((time_line, text, line_indices)) if (time_line, text, line_indices) in segments else -1
                    # 向前找5段
                    for di in range(1, min(6, idx + 1)):
                        prev_idx = idx - di
                        if prev_idx >= 0 and prev_idx < len(seg_info):
                            prev = seg_info[prev_idx]
                            if prev["cats_found"] and not prev["has_preview"]:
                                info["proof_cats"] = [prev["cats_found"][0]]
                                break
                    # 向后找5段
                    if not info["proof_cats"]:
                        for di in range(1, min(6, len(segments) - idx)):
                            next_idx = idx + di
                            if next_idx < len(segments) and next_idx < len(seg_info):
                                nxt = seg_info[next_idx]
                                if nxt["cats_found"] and not nxt["has_preview"]:
                                    info["proof_cats"] = [nxt["cats_found"][0]]
                                    break
                break
        seg_info.append(info)

    # ============================================================
    # 4. 四维判定
    # ============================================================
    all_cats = list(PRODUCT_CATEGORIES.keys())

    # 优先级1:成交铁证(权重降低，避免顺带提及碾压主推品)
    proof_scores = {cat: 0 for cat in all_cats}
    proof_details = {cat: 0 for cat in all_cats}
    for info in seg_info:
        for cat in info["proof_cats"]:
            proof_scores[cat] += max(7, round(15 * float(info.get("cat_weights", {}).get(cat, 1.0))))
            proof_details[cat] += 1

    # 优先级2:下款预告排除
    excluded_cats = set()
    for info in seg_info:
        for cat in info["preview_cats"]:
            excluded_cats.add(cat)

    # 优先级3:深度讲解篇幅(连续≥3条同品类 = 深度讲解)
    continuous = {cat: 0 for cat in all_cats}
    max_continuous = {cat: 0 for cat in all_cats}
    for info in seg_info:
        active_cats = set(info["cats_found"]) - excluded_cats
        for cat in active_cats:
            continuous[cat] += 1
            max_continuous[cat] = max(max_continuous[cat], continuous[cat])
        for cat in all_cats:
            if cat not in active_cats:
                continuous[cat] = 0

    deep_bonus = {}
    for cat in all_cats:
        if cat in excluded_cats:
            deep_bonus[cat] = 0
        elif max_continuous[cat] >= 50:
            deep_bonus[cat] = 30
        elif max_continuous[cat] >= 20:
            deep_bonus[cat] = 15
        elif max_continuous[cat] >= 10:
            deep_bonus[cat] = 5
        else:
            deep_bonus[cat] = 0

    # 优先级4:基础核心词计分(排除搭配+预告)— 每段+15，段落数是最可靠指标
    base_scores = {cat: 0 for cat in all_cats}
    seg_counts = {cat: 0 for cat in all_cats}  # 提到该品类的段落数
    for info in seg_info:
        for cat in info["cats_found"]:
            if cat in excluded_cats and proof_scores[cat] == 0 and base_scores[cat] == 0:
                continue
            if info["has_match"] and len(info["cats_found"]) > 1:
                continue
            base_scores[cat] += max(5, round(15 * float(info.get("cat_weights", {}).get(cat, 1.0))))
            seg_counts[cat] += 1

    # 计算总分(段落数×15 + 基础词×15 + 铁证×15 + 深度讲解加成)
    final_scores = {}
    for cat in all_cats:
        s = base_scores[cat] + proof_scores[cat] + deep_bonus[cat]
        if cat in excluded_cats and proof_scores[cat] == 0 and base_scores[cat] == 0:
            s = 0
        final_scores[cat] = s

    # 日志
    _log("品类过滤:")
    if excluded_cats:
        # 找排除原因
        for cat in excluded_cats:
            for info in seg_info:
                if cat in info["preview_cats"]:
                    reason = str(info.get("preview_reason") or "").strip()
                    if not reason:
                        for nw in NEXT_PREVIEW:
                            if nw in info["text"]:
                                reason = nw
                                break
                    _log(f"  下款预告排除品类:{cat}(命中词:{reason or '转场预告'})")
                    break
    for cat in all_cats:
        detail = f"铁证:{proof_details[cat]}次(+{proof_scores[cat]}分)"
        detail += f" 深度讲解:{max_continuous[cat]}条(+{deep_bonus[cat]}分)"
        detail += f" 基础词:{seg_counts[cat]}段(+{base_scores[cat]}分)"
        _log(f"  {cat}: 总分={final_scores[cat]}分 | {detail}")

    # 判定主品类
    valid_cats = {}
    for _cat, _s in final_scores.items():
        if _s > 0 and (_cat not in excluded_cats or proof_scores[_cat] > 0 or base_scores[_cat] > 0):
            valid_cats[_cat] = _s
    if not valid_cats:
        if forced_main_cat:
            _log(f"  用户指定主品类={forced_main_cat}(词库未命中，保留全部字幕并按该品类提示词处理)")
            filtered_srt = _filter_srt_by_food_product(cleaned_srt, _log) if _is_food_fresh_category(forced_main_cat) else cleaned_srt
            _set_last_category_summary({
                "main_category": forced_main_cat,
                "protected_categories": [forced_main_cat],
                "original_segments": len(seg_info),
                "kept_segments": len(seg_info),
                "removed_segments": 0,
                "preview_removed": 0,
                "cross_category_removed": 0,
                "forced_without_keyword_hit": True,
            })
            if _LAST_FOOD_PRODUCT_FILTER_SUMMARY:
                _analysis_metadata_context()["category_summary"]["food_product_summary"] = dict(_LAST_FOOD_PRODUCT_FILTER_SUMMARY)
            return filtered_srt, forced_main_cat
        _log("  无法识别主品类，保留全部")
        return cleaned_srt, None

    # [v8.3] 套装加权: 套装+单品共现段落 +30分
    if "套装" in valid_cats and valid_cats["套装"] > 0:
        suit_bonus = 0
        for info in seg_info:
            cats_found = info["cats_found"]
            if "套装" in cats_found and len(cats_found) >= 2:
                suit_bonus += 30
        if suit_bonus > 0:
            final_scores["套装"] = final_scores.get("套装", 0) + suit_bonus
            _log(f"  套装加权: +{suit_bonus}分 (套装+单品共现段落)")
            valid_cats = {}
            for _cat, _s in final_scores.items():
                if _s > 0 and (_cat not in excluded_cats or proof_scores[_cat] > 0 or base_scores[_cat] > 0):
                    valid_cats[_cat] = _s


    # 用户手动指定主品类(最高优先级)
    if force_category and force_category != "自动检测":
        if forced_main_cat and forced_main_cat in PRODUCT_CATEGORIES:
            main_cat = forced_main_cat
            _log(f"  用户指定主品类={main_cat}(覆盖自动检测结果)")
        else:
            _log(f"  未找到品类'{force_category}'，使用自动检测")
            main_cat = max(valid_cats, key=valid_cats.get)
    else:
        main_cat = max(valid_cats, key=valid_cats.get)

    # 主推严格过滤：只保护主品类本身。
    # 同段同时出现主品类和其他品类时可作为搭配说明保留；
    # 纯讲主品类以外的内容不能因为“套装/搭配场景”被整段放行。
    protected_cats = {main_cat}
    _log(f"  主推严格过滤: 仅保护主品类 {main_cat}，跨品类搭配需主品类更强")

    # ============================================================
    # 5. SRT 过滤
    # ============================================================
    output_lines = []
    removed = 0
    kept = 0
    preview_removed = 0
    match_removed = 0

    for seg_idx, info in enumerate(seg_info):
        should_remove = False

        # 规则1:下款预告的品类片段 → 删除
        if info["has_preview"] and main_cat not in info["cats_found"]:
            should_remove = True
            preview_removed += 1

        # 规则2:跨品类搭配说明必须明显以主品类为核心。
        # 只要次品类权重不低于主品类，就按跨品类污染处理，避免“选上衣但讲裤子”。
        elif not should_remove and info["has_match"]:
            has_main = any(c in protected_cats for c in info["cats_found"])
            has_other = any(c not in protected_cats for c in info["cats_found"])
            if has_other and not has_main:
                should_remove = True
                match_removed += 1
            elif has_other and has_main and main_cat != "套装":
                main_weight = float(info.get("cat_weights", {}).get(main_cat, 0.0))
                other_weight = max(
                    [float(info.get("cat_weights", {}).get(c, 0.0)) for c in info["cats_found"] if c not in protected_cats] or [0.0]
                )
                if main_weight <= other_weight:
                    should_remove = True
                    match_removed += 1

        # 规则3:纯次品类片段(无主品类,无搭配,无预告)→ 也删除
        elif not should_remove:
            has_main = any(c in protected_cats for c in info["cats_found"])
            has_other = any(c not in protected_cats for c in info["cats_found"])
            if has_other and not has_main:
                should_remove = True
                match_removed += 1
            elif has_other and has_main and main_cat != "套装":
                should_remove = True
                match_removed += 1

        if should_remove:
            removed += 1
            continue

        # 保留
        output_lines.append(info["time_line"])
        output_lines.append(info["text"])
        output_lines.append("")
        kept += 1

    _log(f"品类过滤: 最终主品类={main_cat}({final_scores[main_cat]}分)，严格移除 {removed} 个片段(预告{preview_removed}+纯跨品类{match_removed})，保留 {kept} 个")
    _set_last_category_summary({
        "main_category": main_cat,
        "protected_categories": sorted(protected_cats),
        "original_segments": len(seg_info),
        "kept_segments": kept,
        "removed_segments": removed,
        "preview_removed": preview_removed,
        "cross_category_removed": match_removed,
    })

    # ============================================================
    # 6. 跨品类合法性校验(第二道防线)
    # ============================================================
    # 唯一合法的次品类提及:同一句中必须同时有 主品类词 + 搭配词 + 次品类词
    # 否则删除
    main_keywords = set()
    for cat in protected_cats:
        for kw in PRODUCT_CATEGORIES.get(cat, []):
            main_keywords.add(kw)
    # 不把“这件/这条/这款”等指代词当作主品类词，避免“这条裤子”被误判成上衣片段。

    match_trigger = {"搭", "配", "搭配", "配着穿", "搭什么", "配什么", "同款", "一套", "两件套"}

    other_keywords = set()
    for cat, keywords in PRODUCT_CATEGORIES.items():
        if cat not in protected_cats:
            for kw in keywords:
                other_keywords.add(kw)

    # 重新解析 output_lines 做合法性校验
    legal_lines = []
    orphan_removed = 0
    legal_match_kept = 0
    ol = output_lines
    oi = 0
    while oi < len(ol):
        line = ol[oi].strip() if oi < len(ol) else ""
        # 检测时间戳行
        if re.match(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}', line):
            text = ol[oi + 1].strip() if oi + 1 < len(ol) else ""
            text_len = len(text)

            has_main = any(kw in text for kw in main_keywords)
            has_match = any(kw in text for kw in match_trigger)
            has_other = any(kw in text for kw in other_keywords)

            if has_other and not has_match:
                # 有次品类但无明确搭配词 → 强制删除，即使同句也提到主品类。
                orphan_removed += 1
                oi += 3
                continue
            elif has_other and has_match and not has_main:
                # 次品类+搭配 但无主品类 → 删除
                orphan_removed += 1
                oi += 3
                continue
            else:
                # 其他情况(有主品类,或无品类词)→ 保留
                legal_lines.append(line)
                legal_lines.append(text)
                legal_lines.append("")
                if has_match and has_other and has_main:
                    legal_match_kept += 1
            oi += 3
        else:
            legal_lines.append(line)
            oi += 1

    if orphan_removed > 0:
        _log(f"品类合法性校验: 移除 {orphan_removed} 个孤立跨品类片段，保留 {legal_match_kept} 个合法搭配片段")
        _analysis_metadata_context()["category_summary"]["orphan_removed"] = orphan_removed
    else:
        _log(f"品类合法性校验: 无突兀跨品类内容")
        _analysis_metadata_context()["category_summary"]["orphan_removed"] = 0

    legal_srt = "\n".join(legal_lines)
    if _is_food_fresh_category(main_cat):
        legal_srt = _filter_srt_by_food_product(legal_srt, _log)
        if _LAST_FOOD_PRODUCT_FILTER_SUMMARY:
            _analysis_metadata_context()["category_summary"]["food_product_summary"] = dict(_LAST_FOOD_PRODUCT_FILTER_SUMMARY)

    return legal_srt, main_cat


# ============================================================
# 核心:调用 AI + 前置清洗 + 重试
# ============================================================
def _director_clip_source_key(clip):
    if isinstance(clip, (list, tuple)) and len(clip) > 7 and str(clip[7] or "").strip():
        return str(clip[7]).strip().lower()
    text = str(clip[1] if isinstance(clip, (list, tuple)) and len(clip) > 1 else "")
    marker = re.search(r"\[V\d+\]", text, re.I)
    return marker.group(0).upper() if marker else ""


def _director_clip_trim_key(clip):
    try:
        start = round(float(clip[2]), 3)
        end = round(float(clip[3]), 3)
    except Exception:
        start, end = 0.0, 0.0
    text = re.sub(r"\s+", "", str(clip[1] if len(clip) > 1 else ""))
    return f"{start:.3f}:{end:.3f}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}"


def _director_duration_status(
    clips, target_duration, duration_contract=None, shortage_grace_seconds=0.0,
):
    contract = _coerce_duration_contract(
        duration_contract,
        target_duration=target_duration,
        final_target_duration=(duration_contract or {}).get("final_target") if isinstance(duration_contract, dict) else None,
    )
    total = sum(_clip_duration_value(clip) for clip in clips or [])
    contract_status = contract.status(
        total,
        shortage_grace_seconds=shortage_grace_seconds,
    )
    return {
        "total": float(total),
        "low": float(contract.source_min),
        "high": float(contract.source_max),
        "relaxed_low": float(contract_status["relaxed_source_low"]),
        "gap": max(0.0, float(contract_status["relaxed_source_low"]) - float(total)),
        "severe_gap": 0.0,
        "short": bool(contract_status["short"]),
        "long": bool(contract_status["long"]),
        "accepted": bool(contract_status["accepted"]),
        "used_shortage_grace": bool(contract_status["used_shortage_grace"]),
        "projected_final": float(contract_status["projected_final"]),
    }


def _director_clip_entry_indices(clip, srt_entries):
    """Resolve one immutable clip back to its candidate indices."""
    try:
        clip_start = float(clip[2])
        clip_end = float(clip[3])
    except Exception:
        return []
    clip_source = _director_clip_source_key(clip)
    selected = []
    for entry_index, (entry_start, entry_end, entry_text) in enumerate(srt_entries or [], 1):
        entry_source = _director_candidate_source(entry_text)
        if clip_source and entry_source and clip_source.lower() != entry_source.lower():
            continue
        overlap = max(0.0, min(clip_end, float(entry_end)) - max(clip_start, float(entry_start)))
        if overlap > 0.05:
            selected.append(entry_index)
    return selected


def _director_safe_patch_candidate(
    indices,
    srt_entries,
    selected_indices=None,
    selected_clips=None,
    focus="",
    forbidden_words=None,
    price_patterns=None,
):
    """Build a Product only from AI-declared immutable candidate indices."""
    if isinstance(indices, bool):
        return None
    if isinstance(indices, int):
        indices = [indices]
    if not isinstance(indices, list) or not indices or len(indices) > 3:
        return None
    try:
        normalized = [int(value) for value in indices]
    except Exception:
        return None
    if normalized != sorted(set(normalized)):
        return None
    if any(right != left + 1 for left, right in zip(normalized, normalized[1:])):
        return None
    if normalized[0] < 1 or normalized[-1] > len(srt_entries or []):
        return None
    if set(normalized) & set(selected_indices or set()):
        return None

    entries = [srt_entries[index - 1] for index in normalized]
    sources = {_director_candidate_source(entry[2]) for entry in entries}
    sources.discard("")
    if len(sources) > 1:
        return None
    start = float(entries[0][0])
    end = float(entries[-1][1])
    text = "".join(str(entry[2] or "") for entry in entries).strip()
    if not text or end <= start:
        return None
    if _is_safety_blocked_text(text, forbidden_words, price_patterns) or _is_backstage_instruction(text):
        return None
    _needs_previous, starts_incomplete, ends_incomplete = _director_context_boundary_flags(text)
    if starts_incomplete or ends_incomplete:
        return None

    candidate = ("product", text, start, end, 50, end - start, str(focus or "").strip())
    candidate_source = _director_clip_source_key(candidate)
    for existing in selected_clips or []:
        existing_source = _director_clip_source_key(existing)
        if candidate_source and existing_source and candidate_source.lower() != existing_source.lower():
            continue
        overlap = max(0.0, min(end, float(existing[3])) - max(start, float(existing[2])))
        if overlap > 0.25:
            return None
    return candidate


def _director_safe_candidate_inventory(srt_entries):
    try:
        forbidden_words = load_keywords().get("forbidden_phrases", [])
    except Exception:
        forbidden_words = []
    price_patterns = _safety_price_cta_patterns()
    inventory = []
    for index, (start, end, text) in enumerate(srt_entries or [], 1):
        candidate = _director_safe_patch_candidate(
            [index],
            srt_entries,
            forbidden_words=forbidden_words,
            price_patterns=price_patterns,
        )
        if candidate is None:
            continue
        inventory.append({
            "srt_index": index,
            "source": _director_candidate_source(text),
            "duration_sec": round(max(0.0, float(end) - float(start)), 1),
            "text": re.sub(r"\s+", " ", str(text or "")).strip()[:180],
        })
    return inventory


def _director_source_requirements(required_sources):
    if isinstance(required_sources, dict):
        result = {}
        for source, count in required_sources.items():
            key = str(source or "").strip().upper()
            if not key:
                continue
            try:
                minimum = max(1, int(count or 1))
            except (TypeError, ValueError):
                minimum = 1
            result[key] = minimum
        return result
    return {
        str(source or "").strip().upper(): 1
        for source in (required_sources or set())
        if str(source or "").strip()
    }


def _director_source_counts(clips):
    counts = {}
    for clip in clips or []:
        source = _director_clip_source_key(clip).strip().upper()
        if source:
            counts[source] = counts.get(source, 0) + 1
    return counts


def _director_source_deficits(clips, required_sources):
    requirements = _director_source_requirements(required_sources)
    selected = _director_source_counts(clips)
    return {
        source: minimum - selected.get(source, 0)
        for source, minimum in requirements.items()
        if selected.get(source, 0) < minimum
    }


def _director_source_quota_action(clips, required_sources, attempt, max_attempts):
    if not _director_source_deficits(clips, required_sources):
        return "satisfied"
    try:
        has_retry = int(attempt) + 1 < max(1, int(max_attempts))
    except (TypeError, ValueError):
        has_retry = False
    return "repair" if has_retry else "warn"


def _director_missing_sources(clips, required_sources):
    return sorted(_director_source_deficits(clips, required_sources))


def _director_interleave_prompt_lines(indexed_lines, srt_entries, chunk_size=10):
    """Interleave source blocks for model attention without changing candidate IDs."""
    lines = list(indexed_lines or [])
    entries = list(srt_entries or [])
    if len(lines) != len(entries) or len(lines) < 2:
        return lines
    try:
        chunk_size = max(1, int(chunk_size or 10))
    except (TypeError, ValueError):
        chunk_size = 10

    groups = {}
    source_order = []
    for line, entry in zip(lines, entries):
        text = entry[2] if isinstance(entry, (list, tuple)) and len(entry) > 2 else ""
        source = _director_candidate_source(text) or "__OTHER__"
        if source not in groups:
            groups[source] = []
            source_order.append(source)
        groups[source].append(line)

    marked_sources = [source for source in source_order if source != "__OTHER__"]
    if len(marked_sources) <= 1:
        return lines

    positions = {source: 0 for source in source_order}
    result = []
    while True:
        added = 0
        for source in source_order:
            start = positions[source]
            batch = groups[source][start:start + chunk_size]
            if not batch:
                continue
            result.extend(batch)
            positions[source] += len(batch)
            added += len(batch)
        if not added:
            break
    return result


def _director_source_distribution_summary(clips, required_sources):
    requirements = _director_source_requirements(required_sources)
    counts = _director_source_counts(clips)
    durations = {}
    sequence = []
    for clip in clips or []:
        source = _director_clip_source_key(clip).strip().upper()
        if not source:
            continue
        sequence.append(source)
        durations[source] = durations.get(source, 0.0) + _clip_duration_value(clip)

    source_count = len(requirements)
    share_cap = 0.65 if source_count == 2 else 0.55 if source_count == 3 else 0.45
    run_cap = 5 if source_count == 2 else 4
    total_count = sum(counts.get(source, 0) for source in requirements)
    dominant_source = ""
    dominant_count = 0
    if total_count:
        dominant_source, dominant_count = max(
            ((source, counts.get(source, 0)) for source in requirements),
            key=lambda item: item[1],
        )
    dominant_share = dominant_count / total_count if total_count else 0.0

    longest_source = ""
    longest_run = 0
    current_source = ""
    current_run = 0
    for source in sequence:
        if source == current_source:
            current_run += 1
        else:
            current_source = source
            current_run = 1
        if current_run > longest_run:
            longest_source = source
            longest_run = current_run

    rich_contract = bool(source_count > 1 and requirements and all(value >= 2 for value in requirements.values()))
    issues = []
    if rich_contract and dominant_share > share_cap + 1e-9:
        issues.append(
            f"{dominant_source.strip('[]')}占{dominant_share:.0%}，超过{share_cap:.0%}"
        )
    if rich_contract and longest_run > run_cap:
        issues.append(
            f"{longest_source.strip('[]')}连续{longest_run}段，超过{run_cap}段"
        )
    return {
        "counts": dict(sorted(counts.items())),
        "durations": {source: round(duration, 1) for source, duration in sorted(durations.items())},
        "dominant_source": dominant_source,
        "dominant_share": round(dominant_share, 4),
        "share_cap": share_cap,
        "longest_source": longest_source,
        "longest_run": longest_run,
        "run_cap": run_cap,
        "issues": issues,
        "balanced": not issues and not _director_missing_sources(clips, requirements),
    }


def _director_source_distribution_repair_instruction(summary, required_sources):
    requirements = _director_source_requirements(required_sources)
    counts = dict(summary.get("counts") or {})
    count_text = "、".join(
        f"{source.strip('[]')}={counts.get(source, 0)}段"
        for source in sorted(requirements)
    )
    quota_text = "、".join(
        f"{source.strip('[]')}至少{minimum}段"
        for source, minimum in sorted(requirements.items())
    )
    return (
        "【混剪来源分布修复】上一次片单虽然覆盖了各来源，但分布不合格："
        + count_text
        + "；"
        + "、".join(summary.get("issues") or [])
        + "。必须从头重新编排完整clips，不能只在开头或结尾补几段凑配额。"
        + f"硬要求：{quota_text}；任一来源不超过{float(summary.get('share_cap') or 0.55):.0%}；"
        + f"同一来源连续不超过{int(summary.get('run_cap') or 4)}段。"
        + "来源切换放在卖点阶段的自然边界，每个来源至少分布到开头、中段、结尾中的两个阶段。】"
    )


def _apply_ai_expansion_plan(
    clips,
    expansion_plan,
    srt_entries,
    target_duration,
    log_fn=None,
    label="AI预排补片",
    duration_contract=None,
    required_sources=None,
):
    """Apply AI-declared insertions; the program only validates indices and duration."""
    def _log(message):
        if log_fn:
            log_fn(message)

    items = list(clips or [])
    if len(items) < 2 or not isinstance(expansion_plan, list):
        return []
    initial_status = _director_duration_status(items, target_duration, duration_contract)
    missing_sources = _director_missing_sources(items, required_sources)
    if not initial_status["short"] and not missing_sources:
        return items

    base_index_groups = [
        tuple(_director_clip_entry_indices(clip, srt_entries))
        for clip in items
    ]
    selected_indices = {index for group in base_index_groups for index in group}
    close_position = next((idx for idx, clip in enumerate(items) if _is_close_clip(clip)), len(items))
    max_anchor = max(1, close_position - 1) if close_position < len(items) else max(1, len(items) - 1)

    ranked = []
    used_priorities = set()
    for response_order, raw in enumerate(expansion_plan):
        if not isinstance(raw, dict):
            continue
        try:
            priority = int(raw.get("priority", response_order + 1) or response_order + 1)
        except Exception:
            continue
        if priority <= 0 or priority in used_priorities:
            continue
        used_priorities.add(priority)
        ranked.append((priority, response_order, raw))
    ranked.sort(key=lambda item: (item[0], item[1]))

    additions = []
    working_clips = list(items)
    for priority, _response_order, raw in ranked:
        candidate = _director_safe_patch_candidate(
            raw.get("srt_indices"),
            srt_entries,
            selected_indices=selected_indices,
            selected_clips=working_clips,
            focus=raw.get("focus", ""),
        )
        if candidate is None:
            continue
        candidate_source = _director_clip_source_key(candidate).strip().upper()
        fills_missing_source = bool(candidate_source and candidate_source in missing_sources)
        if missing_sources and not initial_status["short"] and not fills_missing_source:
            continue
        projected_status = _director_duration_status(
            working_clips + [candidate],
            target_duration,
            duration_contract,
        )
        if projected_status["long"] and not fills_missing_source:
            continue

        anchor_position = None
        raw_anchor = raw.get("after_srt_indices")
        if isinstance(raw_anchor, int):
            raw_anchor = [raw_anchor]
        if isinstance(raw_anchor, list):
            try:
                anchor_group = tuple(sorted(set(int(value) for value in raw_anchor)))
            except Exception:
                anchor_group = ()
            if anchor_group:
                anchor_position = next(
                    (idx for idx, group in enumerate(base_index_groups) if tuple(group) == anchor_group),
                    None,
                )
        if anchor_position is None:
            try:
                anchor_position = int(raw.get("after_order", 2) or 2) - 1
            except Exception:
                anchor_position = 1
        anchor_position = max(1, min(int(anchor_position), max_anchor))

        additions.append((anchor_position, priority, candidate))
        working_clips.append(candidate)
        selected_indices.update(_director_clip_entry_indices(candidate, srt_entries))
        missing_sources = _director_missing_sources(working_clips, required_sources)
        if not projected_status["short"] and not missing_sources:
            break

    if not additions:
        return []

    insertions = {}
    for anchor_position, priority, candidate in additions:
        insertions.setdefault(anchor_position, []).append((priority, candidate))
    result = []
    for position, clip in enumerate(items):
        result.append(clip)
        for _priority, candidate in sorted(insertions.get(position, []), key=lambda item: item[0]):
            result.append(candidate)

    final_status = _director_duration_status(result, target_duration, duration_contract)
    _log(
        f"{label}: 按AI插入锚点补入 {len(additions)} 段，"
        f"{initial_status['total']:.1f}s -> {final_status['total']:.1f}s"
        + (
            "，已进入时长与来源合同"
            if not final_status["short"] and not _director_missing_sources(result, required_sources)
            else "，仍需继续修复时长或来源覆盖"
        )
    )
    return result


def _apply_ai_removal_priority(clips, remove_orders, target_duration, log_fn=None, label="AI优先级精简", duration_contract=None, required_sources=None):
    """Apply AI's semantic removal ranking; the program only performs duration arithmetic."""
    def _log(message):
        if log_fn:
            log_fn(message)

    items = list(clips or [])
    if len(items) <= 2:
        return []
    status = _director_duration_status(items, target_duration, duration_contract)
    if status["total"] <= status["high"] + 0.05:
        return items

    protected = {1, 2}
    if _is_close_clip(items[-1]):
        protected.add(len(items))
    ranking = []
    seen = set()
    for raw_order in remove_orders or []:
        if isinstance(raw_order, bool):
            continue
        try:
            order = int(raw_order)
        except Exception:
            continue
        if order < 1 or order > len(items) or order in seen or order in protected:
            continue
        seen.add(order)
        ranking.append(order)
    if not ranking:
        return []

    kept_orders = set(range(1, len(items) + 1))
    total = float(status["total"])
    removed = []
    for order in ranking:
        if total <= status["high"] + 0.05:
            break
        clip = items[order - 1]
        projected = total - _clip_duration_value(clip)
        if projected < status["low"] - 0.05:
            continue
        tentative_orders = kept_orders - {order}
        tentative_clips = [
            item for item_order, item in enumerate(items, 1)
            if item_order in tentative_orders
        ]
        if _director_missing_sources(tentative_clips, required_sources):
            continue
        kept_orders = tentative_orders
        removed.append(order)
        total = projected

    if total > status["high"] + 0.05 or total < status["low"] - 0.05:
        _log(
            f"{label}: AI删除优先级不足以满足时长，"
            f"{status['total']:.1f}s -> {total:.1f}s（要求{status['low']:.0f}-{status['high']:.0f}s）"
        )
        return []
    trimmed = [clip for order, clip in enumerate(items, 1) if order in kept_orders]
    _log(
        f"{label}: 按AI删除优先级移除 {len(removed)} 段，"
        f"{status['total']:.1f}s -> {total:.1f}s（保留Hook与承接句）"
    )
    return trimmed


def _apply_declared_trim_priorities(clips, target_duration, log_fn=None, duration_contract=None, required_sources=None):
    priorities = dict(_analysis_metadata_context().get("trim_priorities") or {})
    ranked = []
    used_priorities = set()
    for order, clip in enumerate(clips or [], 1):
        raw_priority = priorities.get(_director_clip_trim_key(clip))
        try:
            priority = int(raw_priority)
        except Exception:
            continue
        if priority <= 0 or priority in used_priorities:
            continue
        used_priorities.add(priority)
        ranked.append((priority, order))
    ranked.sort(key=lambda item: item[0])
    return _apply_ai_removal_priority(
        clips,
        [order for _priority, order in ranked],
        target_duration,
        log_fn,
        label="AI预排精简",
        duration_contract=duration_contract,
        required_sources=required_sources,
    )


def _director_boundary_text(text):
    value = re.sub(r"\[[vV]\d+\]\s*", "", str(text or "")).strip()
    while value:
        vocal = re.match(r"^(?:嗯+|啊+|呃+|哦+|诶+)\s*[。！？!?；;，,、：:]*\s*", value)
        confirmed = re.match(r"^(?:对|是的|没错|好的)\s*[。！？!?；;，,、：:]+\s*", value)
        match = vocal or confirmed
        if not match or match.end() <= 0:
            break
        value = value[match.end():].lstrip()
    return value


def _director_context_boundary_flags(text):
    value = _director_boundary_text(text)
    compact = re.sub(r"[\s。！？!?；;]+$", "", value)
    repair_prefixes = (
        "还要", "还有", "而且", "但是", "所以", "不过", "就是", "然后",
        "才能", "像这种", "就感觉", "这两根线",
    )
    hard_prefixes = (
        "还要", "还有", "而且", "但是", "所以", "不过", "就是", "然后",
        "就感觉", "这两根线",
    )
    dangling_suffixes = (
        "而且", "然后", "所以", "但是", "不过", "因为", "还有", "还要", "还",
        "首先", "这个版呢", "这个女生", "这个版", "这种",
        "大头含量是", "还有一种人在", "也会", "的话也会", "整个人调性",
        "有些人他", "属于是一", "已经属于是一", "会", "能", "的话",
    )
    starts_for_repair = compact.startswith(repair_prefixes)
    starts_hard = compact.startswith(hard_prefixes)
    ends = value.endswith(("，", "、", "：", ",", ":")) or compact.endswith(dangling_suffixes)
    return starts_for_repair, starts_hard, ends


def _director_candidate_source(text):
    marker = re.search(r"\[V\d+\]", str(text or ""), flags=re.IGNORECASE)
    return marker.group(0).upper() if marker else ""


def _director_srt_time(seconds):
    total_ms = int(round(max(0.0, float(seconds or 0.0)) * 1000.0))
    hours, remainder = divmod(total_ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def _freeze_director_candidates(cleaned_srt, log_fn=None, word_timings=None):
    """Build one immutable candidate set before the AI sees any indices."""
    def _log(message):
        if log_fn:
            log_fn(message)

    entries = _parse_srt_entries_for_hook(cleaned_srt)
    if not entries:
        return cleaned_srt

    frozen = []
    merge_count = 0
    for start, end, text in entries:
        current = [float(start), float(end), str(text or "").strip()]
        if not frozen:
            frozen.append(current)
            continue

        previous = frozen[-1]
        previous_source = _director_candidate_source(previous[2])
        current_source = _director_candidate_source(current[2])
        same_source = previous_source == current_source
        gap = current[0] - previous[1]
        _, _, prev_needs_next = _director_context_boundary_flags(previous[2])
        current_needs_prev, _, _ = _director_context_boundary_flags(current[2])
        previous_has_strong_end = bool(re.search(r"[。！？!?；;]\s*$", previous[2]))
        combined_duration = current[1] - previous[0]
        merge_duration_cap = 22.0
        completion_merge_cap = 32.0
        needs_boundary_merge = prev_needs_next or (current_needs_prev and not previous_has_strong_end)
        should_merge = (
            same_source
            and -0.15 <= gap <= 2.2
            and needs_boundary_merge
            and (
                combined_duration <= merge_duration_cap
                or (prev_needs_next and combined_duration <= completion_merge_cap)
            )
        )
        if not should_merge:
            frozen.append(current)
            continue

        current_text = current[2]
        if current_source:
            current_text = re.sub(r"^\s*\[V\d+\]\s*", "", current_text, flags=re.IGNORECASE)
        previous[1] = current[1]
        previous[2] = f"{previous[2]}{current_text}".strip()
        merge_count += 1

    candidate_clips = [
        ("product", text, start, end, 0.0, max(0.0, end - start))
        for start, end, text in frozen
        if text and end > start
    ]
    candidate_clips = _trim_filler_start(
        candidate_clips,
        cleaned_srt,
        log_fn,
        word_timings=word_timings,
    )
    candidate_clips = filter_candidate_clips(candidate_clips, log_fn=log_fn)
    candidate_clips = _trim_dangling_tail_clauses(candidate_clips, word_timings, log_fn)

    candidates = []
    for clip in candidate_clips:
        _clip_type, text, start, end = clip[:4]
        if not text or float(end) <= float(start):
            continue
        _needs_previous, starts_incomplete, ends_incomplete = _director_context_boundary_flags(text)
        candidates.append(SelectionCandidate(
            candidate_id=len(candidates) + 1,
            source_id=_director_candidate_source(text),
            start=float(start),
            end=float(end),
            text=str(text).strip(),
            hook_eligible=not starts_incomplete and not ends_incomplete,
        ))

    candidate_set = CandidateSet.from_candidates(candidates)
    _analysis_metadata_context()["candidate_contract"] = candidate_set.summary()
    if not candidate_set.candidates:
        return cleaned_srt

    blocks = [
        f"{item.candidate_id}\n{_director_srt_time(item.start)} --> {_director_srt_time(item.end)}\n{item.text}\n"
        for item in candidate_set.candidates
    ]
    prefix = f"AI候选定型: 合并 {merge_count} 处前后半句，" if merge_count else "AI候选定型: "
    _log(
        f"{prefix}{len(blocks)} 个候选已固定文本和时间戳 "
        f"(合同 {candidate_set.digest[:12]})"
    )
    return "\n".join(blocks)


def _stabilize_director_structure(clips, srt_entries, hook_summary=None, log_fn=None):
    """Repair clip roles and mechanical duplicates without rebuilding AI narration."""
    def _log(message):
        if log_fn:
            log_fn(message)

    items = [tuple(clip) for clip in (clips or []) if isinstance(clip, (list, tuple))]
    if not items:
        return []

    # Exact duplicate text is a mechanical defect. Preserve the first occurrence
    # so the AI-authored relative order of all other clips stays unchanged.
    deduped = []
    seen_text = set()
    duplicate_count = 0
    for clip in items:
        compact = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(clip[1] or "")).lower()
        if compact and compact in seen_text:
            duplicate_count += 1
            continue
        if compact:
            seen_text.add(compact)
        deduped.append(clip)
    items = deduped

    summary = dict(hook_summary or {})
    ranked_hook_indices = [
        int(value) for value in (
            summary.get("ranked_hook_indices")
            or summary.get("allowed_hook_indices")
            or []
        )
        if str(value).strip().isdigit()
    ]

    valid_hook_position = None
    for position, clip in enumerate(items):
        if not _is_hook_clip(clip):
            continue
        text = re.sub(r"\[[vV]\d+\]\s*", "", str(clip[1] or "")).strip()
        _needs_prev, starts_incomplete, ends_incomplete = _director_context_boundary_flags(text)
        if not starts_incomplete and not ends_incomplete and not _is_safety_blocked_text(text):
            valid_hook_position = position
            break

    restored_index = 0
    if valid_hook_position is None:
        chosen_clip = None
        chosen_position = None
        for entry_index in ranked_hook_indices:
            if entry_index < 1 or entry_index > len(srt_entries or []):
                continue
            entry_start, entry_end, entry_text = srt_entries[entry_index - 1]
            clean_text = re.sub(r"\[[vV]\d+\]\s*", "", str(entry_text or "")).strip()
            _needs_prev, starts_incomplete, ends_incomplete = _director_context_boundary_flags(clean_text)
            if (
                not clean_text
                or starts_incomplete
                or ends_incomplete
                or _is_safety_blocked_text(clean_text)
                or _is_backstage_instruction(clean_text)
            ):
                continue
            for position, clip in enumerate(items):
                if abs(float(clip[2]) - float(entry_start)) < 0.05 and abs(float(clip[3]) - float(entry_end)) < 0.05:
                    chosen_clip = _retag_clip_type(clip, "hook")
                    chosen_position = position
                    break
            if chosen_clip is None:
                duration = max(0.0, float(entry_end) - float(entry_start))
                chosen_clip = (
                    "hook", str(entry_text).strip(), float(entry_start), float(entry_end),
                    50, duration, str(summary.get("requested_focus") or "开场"),
                )
            restored_index = entry_index
            break

        if chosen_clip is None:
            for position, clip in enumerate(items):
                if _is_close_clip(clip):
                    continue
                text = re.sub(r"\[[vV]\d+\]\s*", "", str(clip[1] or "")).strip()
                _needs_prev, starts_incomplete, ends_incomplete = _director_context_boundary_flags(text)
                if starts_incomplete or ends_incomplete or _is_safety_blocked_text(text):
                    continue
                chosen_clip = _retag_clip_type(clip, "hook")
                chosen_position = position
                break

        if chosen_clip is not None:
            if chosen_position is not None:
                items.pop(chosen_position)
            items.insert(0, chosen_clip)
            valid_hook_position = 0
    elif valid_hook_position != 0:
        chosen_clip = items.pop(valid_hook_position)
        items.insert(0, chosen_clip)
        valid_hook_position = 0

    # One opening role and at most one final Close are structural invariants.
    normalized = []
    for position, clip in enumerate(items):
        if _is_hook_clip(clip) and position != 0:
            clip = _retag_clip_type(clip, "product")
        if _is_close_clip(clip) and position != len(items) - 1:
            clip = _retag_clip_type(clip, "product")
        normalized.append(tuple(clip))
    items = normalized

    # Remove only substantial same-source overlap. This is mechanical duplicate
    # media protection, not semantic selection or reordering.
    stable = []
    overlap_count = 0
    for clip in items:
        source = _director_clip_source_key(clip)
        should_drop = False
        for kept in stable:
            kept_source = _director_clip_source_key(kept)
            if source and kept_source and source != kept_source:
                continue
            overlap = max(0.0, min(float(clip[3]), float(kept[3])) - max(float(clip[2]), float(kept[2])))
            shorter = min(_clip_duration_value(clip), _clip_duration_value(kept))
            if shorter > 0 and overlap > 0.35 and overlap / shorter >= 0.5:
                should_drop = True
                overlap_count += 1
                break
        if not should_drop:
            stable.append(clip)
    items = stable

    changes = []
    if restored_index:
        changes.append(f"恢复Hook候选#{restored_index}")
    if duplicate_count:
        changes.append(f"移除{duplicate_count}段完全重复")
    if overlap_count:
        changes.append(f"移除{overlap_count}段主要时间重叠")
    if changes:
        _log("AI结构稳定: " + "，".join(changes) + "；未重写Product叙事")
    return items

def _director_hard_audit(
    clips,
    target_duration,
    hook_cap_sec,
    log_fn=None,
    preferred_focus="",
    require_preference_hook=False,
    require_preference_mainline=False,
    preference_target_duration=None,
    duration_contract=None,
):
    """Keep only mechanical safety checks; leave narrative decisions to AI."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    original = list(clips or [])
    safe = _filter_price_and_cta(original, log_fn)
    safe = _filter_celebrity(safe, log_fn)

    hard_removed = len(original) - len(safe)
    backstage_removed = []
    valid = []
    invalid_count = 0
    for clip in safe:
        if not isinstance(clip, (list, tuple)) or len(clip) < 6:
            invalid_count += 1
            continue
        try:
            start = float(clip[2])
            end = float(clip[3])
        except Exception:
            invalid_count += 1
            continue
        text = str(clip[1] or "").strip()
        if not text or end <= start:
            invalid_count += 1
            continue
        if _is_backstage_instruction(text):
            backstage_removed.append(clip)
            continue
        valid.append(tuple(clip))

    if backstage_removed:
        _log(f"AI硬质检: 移除 {len(backstage_removed)} 段直播现场调度")
    hard_removed += len(backstage_removed) + invalid_count

    # A broken Product or Close sentence is a local defect, not a reason to
    # reject the whole AI narrative. Hook still requires repair because an
    # incomplete opening would damage the entire video immediately.
    boundary_removed = []
    boundary_kept = []
    for clip in valid:
        text = re.sub(r"\[[vV]\d+\]\s*", "", str(clip[1] or "")).strip()
        _needs_prev, starts_incomplete, ends_incomplete = _director_context_boundary_flags(text)
        if (starts_incomplete or ends_incomplete) and not _is_hook_clip(clip):
            boundary_removed.append(clip)
            continue
        boundary_kept.append(clip)
    if boundary_removed:
        valid = boundary_kept
        hard_removed += len(boundary_removed)
        examples = "；".join(
            re.sub(r"\[[vV]\d+\]\s*", "", str(clip[1] or "")).strip()[:34]
            for clip in boundary_removed[:3]
        )
        _log(f"AI边界验收: 删除 {len(boundary_removed)} 段卖点/收尾半句，保留其余片单（{examples}）")

    issues = []
    warnings = []
    if hard_removed:
        issues.append(f"硬合规移除{hard_removed}段")
    if not valid:
        issues.append("无可用安全片段")
        return valid, {
            "issues": issues,
            "needs_repair": True,
            "hard_removed": hard_removed,
            "total_duration": 0.0,
            "warnings": warnings,
        }

    hook_positions = [idx for idx, clip in enumerate(valid) if _is_hook_clip(clip)]
    close_positions = [idx for idx, clip in enumerate(valid) if _is_close_clip(clip)]
    if hook_positions != [0]:
        issues.append("Hook必须且只能位于首段")
    # Close is desirable but optional after hard compliance cleanup. If an
    # incomplete Close was removed, the remaining AI order is still usable.
    if close_positions and close_positions != [len(valid) - 1]:
        issues.append("Close必须且只能位于末段")

    if require_preference_hook and hook_positions == [0]:
        preferred_block = _focus_label_to_block(preferred_focus)
        hook_block = _clip_focus_block(valid[0])
        if preferred_block and hook_block != preferred_block:
            warnings.append(f"Hook未体现指定偏好{preferred_block}，保留安全开场并在预览提示")
        strict_followup_blocks = {
            "版型显瘦", "面料质感", "穿着体验", "颜色氛围", "场景搭配",
            "尺寸长度", "工艺细节", "对比优势", "口感食欲", "新鲜品质",
            "产地溯源", "规格分量", "发货保鲜", "场景吃法",
        }
        if len(valid) >= 2 and preferred_block in strict_followup_blocks:
            followup_block = _clip_focus_block(valid[1])
            if followup_block != preferred_block:
                warnings.append(f"Hook第二段未承接{preferred_block}，保留AI叙事并在预览提示")

    if require_preference_mainline:
        preference_summary = _topic_coverage_summary(
            valid,
            preferred_focus,
            preference_target_duration or target_duration,
            requested=preferred_focus,
        )
        preferred_topic = str(preference_summary.get("preferred_topic") or "")
        preferred_count = int(preference_summary.get("preference_count") or 0)
        preferred_duration = float(
            (preference_summary.get("topic_durations") or {}).get(preferred_topic, 0.0) or 0.0
        )
        other_topics = [
            (topic, int(count or 0), float((preference_summary.get("topic_durations") or {}).get(topic, 0.0) or 0.0))
            for topic, count in (preference_summary.get("topic_counts") or {}).items()
            if topic not in {preferred_topic, "其他"}
        ]
        dominant_other = max(other_topics, key=lambda item: (item[1], item[2]), default=("", 0, 0.0))
        mainline_issues = []
        if preference_summary.get("underpreferred"):
            mainline_issues.append(
                f"偏好片段仅{preferred_count}段，至少需要{preference_summary.get('preference_min', 0)}段"
            )
        if preference_summary.get("overconcentrated"):
            mainline_issues.append(
                f"偏好内容超过上限{preference_summary.get('preference_max', 0)}段或占时过高，需删除同义重复"
            )
        if dominant_other[1] > preferred_count or (
            dominant_other[2] > preferred_duration * 1.25 and dominant_other[2] - preferred_duration > 6.0
        ):
            mainline_issues.append(
                f"补充主题{dominant_other[0]}已超过偏好主线"
            )
        if mainline_issues:
            warnings.append(
                f"偏好覆盖提示: {preferred_topic}（{'；'.join(mainline_issues)}）。"
                "该统计仅用于预览核对，不否决AI已完成的叙事片单"
            )

    try:
        hook_cap = float(hook_cap_sec or 0)
    except Exception:
        hook_cap = 0.0
    if hook_positions and hook_cap > 0:
        hook_duration = _clip_duration_value(valid[hook_positions[0]])
        if hook_duration > hook_cap + 0.35:
            warnings.append(f"Hook{hook_duration:.1f}s超过设置{hook_cap:.1f}s")

    long_products = [
        _clip_duration_value(clip)
        for clip in valid
        if not _is_hook_clip(clip) and not _is_close_clip(clip) and _clip_duration_value(clip) > 12.0
    ]
    if long_products:
        warnings.append(f"存在{len(long_products)}段超过12s的Product")

    fragment_examples = []
    for clip_index, clip in enumerate(valid, 1):
        text = re.sub(r"\[[vV]\d+\]\s*", "", str(clip[1] or "")).strip()
        _needs_prev, starts_incomplete, ends_incomplete = _director_context_boundary_flags(text)
        if starts_incomplete or ends_incomplete:
            side = "开头承接前句" if starts_incomplete else "结尾未说完"
            excerpt = text[:34] if starts_incomplete else text[-34:]
            if ends_incomplete and len(text) > 34:
                excerpt = "..." + excerpt
            fragment_examples.append(f"第{clip_index}段{side}:{excerpt}")
    if fragment_examples:
        issues.append(
            f"存在{len(fragment_examples)}段明确半句边界，需由AI合并相邻字幕（"
            + "；".join(fragment_examples[:4])
            + "）"
        )

    duration_status = _director_duration_status(valid, target_duration, duration_contract)
    total_duration = duration_status["total"]
    low, high = duration_status["low"], duration_status["high"]
    if total_duration < low:
        duration_message = f"总时长{total_duration:.1f}s低于目标下限{low:.0f}s"
        if duration_status["short"]:
            issues.append(f"{duration_message}，至少还需补足约{duration_status['gap']:.0f}s的不同卖点完整片段")
        else:
            warnings.append(duration_message)
    elif total_duration > high:
        duration_message = f"总时长{total_duration:.1f}s超过目标上限{high:.0f}s"
        if duration_status["long"]:
            issues.append(
                f"{duration_message}，"
                f"至少需删除约{total_duration - high:.1f}s的低优先级完整Product"
            )
        else:
            warnings.append(duration_message)

    seen_text = set()
    duplicate_count = 0
    for clip in valid:
        compact = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(clip[1] or "")).lower()
        if compact and compact in seen_text:
            duplicate_count += 1
        elif compact:
            seen_text.add(compact)
    if duplicate_count:
        issues.append(f"存在{duplicate_count}段完全重复文案")

    overlap_count = 0
    for left_idx, left in enumerate(valid):
        left_source = _director_clip_source_key(left)
        for right in valid[left_idx + 1:]:
            right_source = _director_clip_source_key(right)
            if left_source and right_source and left_source != right_source:
                continue
            overlap = max(0.0, min(float(left[3]), float(right[3])) - max(float(left[2]), float(right[2])))
            if overlap > 0.25:
                overlap_count += 1
    if overlap_count:
        issues.append(f"存在{overlap_count}组源时间重叠")

    return valid, {
        "issues": issues,
        "needs_repair": bool(issues),
        "hard_removed": hard_removed,
        "total_duration": total_duration,
        "duration_short": bool(duration_status["short"]),
        "duration_long": bool(duration_status["long"]),
        "duration_low": low,
        "duration_high": high,
        "duration_gap": duration_status["gap"],
        "warnings": warnings,
    }


def _director_short_fallback_floor(target_duration):
    try:
        target = float(target_duration or 60)
    except Exception:
        target = 60.0
    if target <= 20:
        return max(8.0, target * 0.45)
    if target <= 30:
        return 12.0
    return max(20.0, target * 0.45)


def _director_only_duration_short_issue(audit):
    issues = [str(item or "") for item in ((audit or {}).get("issues") or [])]
    if not (audit or {}).get("duration_short") or not issues:
        return False
    allowed_issue_markers = ("低于目标下限", "至少还需补足", "硬合规移除")
    return all(any(marker in item for marker in allowed_issue_markers) for item in issues)


def _director_fatal_issues(audit):
    issues = [str(item or "") for item in ((audit or {}).get("issues") or [])]
    return [item for item in issues if "硬合规移除" not in item]


def _call_director_trim_selection(api_key, base_url, model, clips, target_duration, log_fn=None, preferred_focus="", duration_contract=None, required_sources=None):
    """Ask AI to rank removable clips, then apply that ranking with exact arithmetic."""
    def _log(message):
        if log_fn:
            log_fn(message)

    low, high = _duration_source_bounds(target_duration, duration_contract)
    items = []
    for order, clip in enumerate(clips or [], 1):
        try:
            duration = max(0.0, float(clip[3]) - float(clip[2]))
        except Exception:
            duration = _clip_duration_value(clip)
        items.append({
            "order": order,
            "clip_type": str(clip[0] if len(clip) > 0 else "product"),
            "duration_sec": round(duration, 1),
            "focus": str(clip[6] if len(clip) > 6 else "")[:40],
            "text": re.sub(r"\s+", " ", str(clip[1] if len(clip) > 1 else "")).strip()[:180],
        })
    current_total = sum(float(item["duration_sec"]) for item in items)
    if not items:
        return []

    system_prompt = (
        "你是短视频最终删片导演。输入片单的片段内容、边界和顺序已经固定。"
        "你只负责给所有可删除Product排出完整的删除优先顺序，不需要计算最终时长。"
        "不能新增、改写、拆分或重排片段。首段Hook、紧随Hook的第二段、末段Close禁止列入删除顺序。"
        "先删重复、低信息量、偏离指定偏好的Product，后删核心效果和证据。"
        "只输出JSON对象，不要解释。"
    )
    focus_rule = f"本轮指定偏好是“{preferred_focus}”，优先保留真正命中该偏好的核心证据。" if preferred_focus else ""
    user_prompt = (
        f"当前片单合计{current_total:.1f}秒，目标约{float(target_duration):.0f}秒，"
        f"程序会严格把结果控制在{low:.0f}-{high:.0f}秒。{focus_rule}"
        "返回remove_priority，按最先删除到最后删除排列，并列出除受保护片段外的所有Product编号。"
        "返回格式只能是：{\"remove_priority\":[5,3,7,4]}。\n"
        "固定片单：\n"
        + json.dumps(items, ensure_ascii=False)
    )
    body_dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "top_p": 0.8,
        "max_tokens": 1024,
    }
    if "deepseek" in model.lower() and "seed" not in model.lower():
        body_dict["thinking"] = {"type": "disabled"}
    if "seed" in model.lower():
        body_dict["reasoning_effort"] = "low"

    request = urllib.request.Request(
        ai_chat_completions_url(base_url),
        data=json.dumps(body_dict, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        context = create_ssl_context()
        with urllib.request.urlopen(request, timeout=180, context=context) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = str(result.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", content)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        match = re.search(r"\{[\s\S]*\}", cleaned)
        data = json.loads(match.group(0) if match else cleaned)
        raw_remove_orders = data.get("remove_priority") if isinstance(data, dict) else None
        if isinstance(raw_remove_orders, list):
            trimmed = _apply_ai_removal_priority(
                clips,
                raw_remove_orders,
                target_duration,
                log_fn,
                label="AI超长精简",
                duration_contract=duration_contract,
                required_sources=required_sources,
            )
            if trimmed:
                return trimmed
            _log("AI超长精简: 删除优先级未能收口到目标时长")
            return []

        # Compatibility with responses generated from an older cached prompt.
        raw_orders = data.get("keep_orders") if isinstance(data, dict) else None
        if not isinstance(raw_orders, list):
            _log("AI超长精简: 响应缺少remove_priority，拒绝执行")
            return []
        keep_orders = []
        for value in raw_orders:
            if isinstance(value, bool):
                return []
            order = int(value)
            if order < 1 or order > len(clips) or order in keep_orders:
                _log("AI超长精简: keep_orders含无效或重复编号，拒绝执行")
                return []
            keep_orders.append(order)
        if keep_orders != sorted(keep_orders):
            _log("AI超长精简: keep_orders改变原叙事顺序，拒绝执行")
            return []
        if not {1, 2}.issubset(set(keep_orders)):
            _log("AI超长精简: keep_orders未保留Hook与承接句，拒绝执行")
            return []
        trimmed = [clips[order - 1] for order in keep_orders]
        trimmed_total = sum(_clip_duration_value(clip) for clip in trimmed)
        if trimmed_total < low - 0.05 or trimmed_total > high + 0.05:
            _log(f"AI超长精简: 旧格式结果{trimmed_total:.1f}s仍不在{low:.0f}-{high:.0f}s")
            return []
        _log(
            f"AI超长精简: AI决定保留 {len(trimmed)}/{len(clips)} 段，"
            f"{current_total:.1f}s -> {trimmed_total:.1f}s"
        )
        return trimmed
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        message = f"AI 接口调用失败 (HTTP {error.code})：{_friendly_http(error.code, detail)}"
        _log(f"⚠️ {message}")
        if _is_non_retryable_http(error.code):
            raise NonRetryableAIError(message) from error
        return []
    except Exception as error:
        _log(f"AI超长精简失败: {_friendly_msg(str(error))}")
        return []


def _call_director_expand_selection(
    api_key,
    base_url,
    model,
    clips,
    srt_entries,
    target_duration,
    log_fn=None,
    preferred_focus="",
    duration_contract=None,
    merge_mode=False,
    required_sources=None,
):
    """Ask AI for insert-only operations, then apply them without rebuilding the story."""
    def _log(message):
        if log_fn:
            log_fn(message)

    items = list(clips or [])
    status = _director_duration_status(items, target_duration, duration_contract)
    contract_missing_sources = _director_missing_sources(items, required_sources)
    if not status["short"] and not contract_missing_sources:
        return items

    selected_indices = {
        index
        for clip in items
        for index in _director_clip_entry_indices(clip, srt_entries)
    }
    inventory = [
        candidate
        for candidate in _director_safe_candidate_inventory(srt_entries)
        if int(candidate["srt_index"]) not in selected_indices
    ]
    if not inventory:
        _log("AI增量补片: 没有剩余安全候选")
        return []

    skeleton = []
    for order, clip in enumerate(items, 1):
        skeleton.append({
            "order": order,
            "clip_type": str(clip[0] if len(clip) > 0 else "product"),
            "srt_indices": _director_clip_entry_indices(clip, srt_entries),
            "source": _director_clip_source_key(clip).upper(),
            "duration_sec": round(_clip_duration_value(clip), 1),
            "focus": str(clip[6] if len(clip) > 6 else "")[:40],
            "text": re.sub(r"\s+", " ", str(clip[1] if len(clip) > 1 else "")).strip()[:180],
        })

    selected_sources = {
        str(item.get("source") or "").upper()
        for item in skeleton
        if str(item.get("source") or "").strip()
    }
    available_sources = {
        str(item.get("source") or "").upper()
        for item in inventory
        if str(item.get("source") or "").strip()
    }
    missing_sources = (
        contract_missing_sources
        if required_sources
        else sorted(available_sources - selected_sources) if merge_mode else []
    )
    source_deficits = _director_source_deficits(items, required_sources)
    source_rule = ""
    if missing_sources:
        deficit_text = "、".join(
            f"{source.strip('[]')}还需{source_deficits.get(source, 1)}段"
            for source in missing_sources
        )
        source_rule = (
            f"这是混剪，当前片单尚未使用来源{','.join(source.strip('[]') for source in missing_sources)}，或这些来源已使用但仍未达到配额；具体缺口为{deficit_text}；"
            "必须优先从这些来源选择能自然承接的安全完整候选放入扩展计划，但不得牺牲语义完整和品类一致。"
        )

    system_prompt = (
        "你是短视频增量补片导演。当前主片单的内容、顺序、Hook和Close已经锁定。"
        "你只能从剩余候选中选择完整Product，并指定插入到哪一段之后；不能删除、改写、拆分、替换或重排现有片段。"
        "只输出JSON对象，不要解释、不要Markdown。"
    )
    user_prompt = (
        f"当前片单{status['total']:.1f}秒，时长合同要求{status['low']:.1f}-{status['high']:.1f}秒，"
        f"至少还需补足{status['gap']:.1f}秒。指定偏好为“{preferred_focus or '全量选片'}”。{source_rule}"
        "请返回6-12个按纳入优先级排列的备用Product，累计时长必须充分覆盖缺口。"
        "每个备用片段必须是当前片单没有的新卖点、证据、场景或顾虑解除；"
        "priority从1开始且不重复，after_srt_indices必须完整复制当前片单某个非Close片段的srt_indices，"
        "after_order同时填写该锚点的order。不得锚定Hook导致插入片段破坏Hook与第二段承接，优先锚定第2段或后续Product。"
        "程序会按priority逐个纳入，并在精确达到时长区间后停止。"
        "返回格式只能是："
        '{"expansion_plan":[{"priority":1,"after_srt_indices":[8],"after_order":2,'
        '"srt_indices":[21],"focus":"面料证据","reason":"补充不同证据"}]}。\n'
        "锁定主片单：\n" + json.dumps(skeleton, ensure_ascii=False) + "\n"
        "剩余安全候选：\n" + json.dumps(inventory, ensure_ascii=False)
    )
    body_dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "top_p": 0.8,
        "max_tokens": 2048,
    }
    if "deepseek" in model.lower() and "seed" not in model.lower():
        body_dict["thinking"] = {"type": "disabled"}
    if "seed" in model.lower():
        body_dict["reasoning_effort"] = "low"

    request = urllib.request.Request(
        ai_chat_completions_url(base_url),
        data=json.dumps(body_dict, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        context = create_ssl_context()
        with urllib.request.urlopen(request, timeout=180, context=context) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = str(result.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        _log(f"AI增量补片: 响应成功，内容长度={len(content)}字")
        cleaned = re.sub(r"^```(?:json)?\s*", "", content)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        match = re.search(r"\{[\s\S]*\}", cleaned)
        data = json.loads(match.group(0) if match else cleaned)
        plan = data.get("expansion_plan") if isinstance(data, dict) else None
        if not isinstance(plan, list):
            _log("AI增量补片: 响应缺少expansion_plan，拒绝执行")
            return []
        expanded = _apply_ai_expansion_plan(
            items,
            plan,
            srt_entries,
            target_duration,
            log_fn,
            label="AI增量补片",
            duration_contract=duration_contract,
            required_sources=required_sources,
        )
        if not expanded:
            _log("AI增量补片: AI操作未能产生可执行的新片段")
        return expanded
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        message = f"AI 接口调用失败 (HTTP {error.code})：{_friendly_http(error.code, detail)}"
        _log(f"⚠️ {message}")
        if _is_non_retryable_http(error.code):
            raise NonRetryableAIError(message) from error
        return []
    except Exception as error:
        _log(f"AI增量补片失败: {_friendly_msg(str(error))}")
        return []


def ai_analyze_clips(srt_text, log_fn=None, force_category=None, multi_version=False, focus_hint=None, hook_candidates_hint=None, merge_mode=False, target_duration=60, ai_controls=None, record_history=True, word_timings=None, final_target_duration=None, duration_contract=None):
    global _AI_TARGET_DURATION, _LAST_FOCUS_SUMMARY, _LAST_TOPIC_COVERAGE_SUMMARY
    _analysis_metadata = _begin_analysis_metadata()
    _duration_contract = _coerce_duration_contract(
        duration_contract,
        target_duration=target_duration,
        final_target_duration=final_target_duration,
    )
    _AI_TARGET_DURATION = _duration_contract.ai_target_seconds
    _final_target_duration = _duration_contract.final_target
    _analysis_metadata["final_target_duration"] = _final_target_duration
    _analysis_metadata["duration_contract"] = _duration_contract.to_dict()
    _selection_request = SelectionRequest.create(
        source_ids=sorted(set(re.findall(r"\[(V\d+)\]", str(srt_text or ""), flags=re.I))),
        category=force_category,
        focus=focus_hint,
        merge_mode=merge_mode,
        duration_contract=_duration_contract,
        controls=ai_controls,
    )
    _analysis_metadata["selection_request"] = _selection_request.to_dict()
    _set_last_topic_coverage_summary({})
    _set_last_selection_failure({})
    def _log(msg):
        if log_fn: log_fn(msg)

    _log(
        f"AI时长合同: 成片{_duration_contract.final_target:.0f}s "
        f"(允许{_duration_contract.final_min:.0f}-{_duration_contract.final_max:.0f}s)，"
        f"按{_duration_contract.speed_factor:.2f}x选择原片"
        f"{_duration_contract.source_min:.1f}-{_duration_contract.source_max:.1f}s"
    )

    settings = load_settings()
    if not settings.get("api_key"):
        _log("AI: 未配置 API Key")
        return []

    api_key = settings["api_key"]
    base_url = normalize_ai_base_url(settings["base_url"])
    model = settings["model"]
    _ai_rules = _merge_ai_rules(ai_controls)
    _enforce_category_filter = bool(_ai_rules.get("category_filter", True))
    _enforce_time_coherence = bool(_ai_rules.get("time_coherence", True))
    _hook_cap_sec = _hook_cap_seconds(_ai_rules)
    _forced_main_cat = _normalize_forced_category(force_category)

    semantic_srt_applied = False
    if word_timings:
        try:
            from volcengine_asr import build_semantic_segments, semantic_segments_to_srt
            semantic_segments = build_semantic_segments(word_timings)
            semantic_srt = semantic_segments_to_srt(semantic_segments)
            if semantic_srt.strip():
                srt_text = semantic_srt
                semantic_srt_applied = True
                _word_level_count = sum(
                    1 for segment in semantic_segments
                    if (segment.get("words") or segment.get("timing_precision") == "word")
                )
                _srt_fallback_count = len(semantic_segments) - _word_level_count
                if _srt_fallback_count:
                    _log(
                        f"AI语义断句: 混合使用 {_word_level_count} 个词级语义段 + "
                        f"{_srt_fallback_count} 个SRT时间段，全部素材均保留"
                    )
                else:
                    _log(f"AI语义断句: 使用 {len(semantic_segments)} 个词级精确语义段")
        except Exception as semantic_error:
            _log(f"AI语义断句: 词级语义段不可用，保留原SRT ({semantic_error})")

    # 没有词级时间的旧字幕保留原有兼容路径；有词级语义段时禁止再次按字数估时拆分。
    if multi_version and not semantic_srt_applied:
        from srt_splitter import split_long_srt_entries
        srt_text = split_long_srt_entries(srt_text, max_duration=5.0, log_fn=_log)
    elif semantic_srt_applied:
        _log("AI叙事模式: 使用词级精确语义段，不做机械二次拆句")
    else:
        _log("AI叙事模式: 使用原始完整SRT条目，不做5秒二次拆句")

    cleaned_srt = _pre_clean_srt(srt_text, log_fn)
    if not cleaned_srt.strip():
        _log("AI: 清洗后无有效字幕，尝试使用原始SRT...")
        cleaned_srt = srt_text
        if not cleaned_srt.strip():
            _log("AI: 原始SRT也为空")
            return []

    # 单版本由AI在完整候选中判断语义重复；本地预去重会提前删掉可能更适合叙事的表达。
    if not merge_mode and multi_version:
        cleaned_srt = _dedup_srt_repeated_sections(cleaned_srt, log_fn)
    elif not merge_mode:
        _log("AI叙事模式: 保留完整候选，不做本地语义预去重")

    # 品类过滤:识别主品类，从源SRT中移除其他品类(支持用户手动指定)
    if _enforce_category_filter:
        cleaned_srt, detected_main_cat = _filter_srt_by_main_product(cleaned_srt, log_fn, force_category=force_category)
    else:
        detected_main_cat = _forced_main_cat
        _log("AI选片规则: 已关闭强制同一品类过滤")
    if not multi_version:
        cleaned_srt = _freeze_director_candidates(cleaned_srt, log_fn, word_timings=word_timings)
    # 用检测到的品类（或用户指定）作为跨品类过滤的偏好品类
    _cross_cat_preferred = _forced_main_cat or detected_main_cat
    _history_key = _clip_history_key(cleaned_srt)
    _recent_history = _get_recent_clip_history(_history_key)
    _recent_history_hint = _format_recent_history_hint(_recent_history)
    if _recent_history:
        _log(f"差异化历史: 检测到同素材最近已用 {len(_recent_history)} 个片段，本次优先避开")
    _feedback_base_scope = "mix" if merge_mode else "smart"
    _feedback_scope = _feedback_scope_key(_feedback_base_scope, _cross_cat_preferred)
    _feedback_profile = _build_preview_feedback_profile(scope=_feedback_scope)
    _feedback_mode, _feedback_configured, _feedback_count = _feedback_effective_strength(settings, _feedback_profile)
    _feedback_prompt_enabled = _feedback_mode in {"light", "standard", "strong"}
    # Light mode is AI-only. It must not locally delete clips or alter AI narrative.
    _feedback_filter_enabled = _feedback_mode in {"standard", "strong"}
    _feedback_active_profile = _feedback_profile if _feedback_filter_enabled else None
    _feedback_hint = _build_preview_feedback_hint_for_strength(_feedback_profile, _feedback_mode) if _feedback_prompt_enabled else ""
    if _feedback_hint:
        _recent_history_hint = "\n".join(part for part in (_recent_history_hint, _feedback_hint) if part)
        _log(f"剪辑风格画像: {_feedback_scope} 已按{_feedback_strength_label(_feedback_mode)}模式进入AI软参考（样本{_feedback_count}）")
    elif _feedback_mode == "readonly":
        _log(f"剪辑风格画像: 样本{_feedback_count}条，未满3条，仅记录不参与选片")
    elif _feedback_mode == "off":
        _log("剪辑风格画像: 画像影响强度已关闭，不参与本次选片")
    _history_min_keep, _history_min_duration = _recent_filter_floor(_AI_TARGET_DURATION)

    def _record_history_if_needed(selected_clips):
        if record_history:
            _remember_recent_clips(_history_key, selected_clips, log_fn)
        elif selected_clips:
            _log("差异化历史: 预览模式不记录已用片段，避免污染后续选片")

    # [PATCH] Compute SRT max time for safety clamping
    _srt_entries_times = []
    for _ln in cleaned_srt.strip().split("\n"):
        _tm = re.match(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})', _ln.strip())
        if _tm:
            _es = int(_tm.group(5))*3600 + int(_tm.group(6))*60 + int(_tm.group(7)) + int(_tm.group(8))/1000.0
            _srt_entries_times.append(_es)
    srt_max_end = max(_srt_entries_times) + 0.5 if _srt_entries_times else None

    # 单版本：AI只做一次完整叙事；后续仅允许受限的整段插入或删除操作。
    # 多版本素材池沿用旧流程，因为它只选候选素材，不直接决定成片顺序。
    # 计算目标片段数（用于检查AI是否选够），与 UI 时长档位共用同一套规则。
    _target_min_clips, _target_max_clips = _target_clip_count_range(_AI_TARGET_DURATION)

    best_clips = []
    best_clips_key = None
    _coverage_candidate_pool = []
    _director_mode = not multi_version
    _director_repair = ""
    _director_focus_hint = focus_hint
    _director_focus_summary = {}
    _director_fallback_clips = []
    _director_short_fallback_clips = []
    _director_short_fallback_status = {}
    _director_last_audit = {}
    _director_best_duration = 0.0
    _director_candidate_count = 0
    _director_stage = "首次完整编排"
    _director_format_retry_used = False
    _director_trim_source_clips = None
    _director_expand_source_clips = None
    # One complete composition plus bounded format/insert/remove operations.
    _max_attempts = 3 if _director_mode else 5
    _indexed_srt_entries = _build_ai_srt_entry_index(cleaned_srt)
    _director_call_entries = _indexed_srt_entries
    _director_candidate_count = len(_indexed_srt_entries)
    _director_safe_inventory = _director_safe_candidate_inventory(_indexed_srt_entries)
    _director_safe_candidate_duration = sum(
        float(item.get("duration_sec") or 0.0)
        for item in _director_safe_inventory
    )
    _source_candidate_counts = {}
    _source_candidate_durations = {}
    for _candidate in _director_safe_inventory:
        _source = str(_candidate.get("source") or "").strip().upper()
        if _source:
            _source_candidate_counts[_source] = _source_candidate_counts.get(_source, 0) + 1
            _source_candidate_durations[_source] = (
                _source_candidate_durations.get(_source, 0.0)
                + float(_candidate.get("duration_sec") or 0.0)
            )
    _eligible_sources = {
        source
        for source, count in _source_candidate_counts.items()
        if merge_mode and count >= 2 and _source_candidate_durations.get(source, 0.0) >= 5.0
    }
    _can_require_two_per_source = bool(
        1 < len(_eligible_sources) <= 4
        and _duration_contract.source_min >= len(_eligible_sources) * 6.0
    )
    _director_required_sources = {
        source: (
            2
            if _can_require_two_per_source
            and _source_candidate_counts.get(source, 0) >= 4
            and _source_candidate_durations.get(source, 0.0) >= 12.0
            else 1
        )
        for source in _eligible_sources
    }
    if len(_director_required_sources) > 1:
        _analysis_metadata["source_contract"] = {
            "candidate_counts": dict(sorted(_source_candidate_counts.items())),
            "candidate_durations": {
                source: round(duration, 1)
                for source, duration in sorted(_source_candidate_durations.items())
            },
            "required_counts": dict(sorted(_director_required_sources.items())),
        }
        _log(
            "混剪来源候选: "
            + "、".join(
                f"{source.strip('[]')}={_source_candidate_counts[source]}条/"
                f"{_source_candidate_durations.get(source, 0.0):.1f}s"
                for source in sorted(_director_required_sources)
            )
        )
        _log(
            "混剪来源合同: "
            + "、".join(
                f"{source.strip('[]')}至少{minimum}段"
                for source, minimum in sorted(_director_required_sources.items())
            )
            + "；由AI统一编排，不做机械轮播"
        )
    _log(f"AI: 构建SRT条目索引 {len(_indexed_srt_entries)} 条")
    # 内容审稿只读取冻结后的安全候选。任何异常都退回原导演候选，不能让审稿层阻断任务。
    _content_review_mode = "off"
    _content_review_version = "content-review-unavailable"
    _content_review_allowed_ids = None
    _content_review_hint = ""
    _content_review_topic_support = {}
    _content_review_applied = False
    _final_sequence_reviewer = None
    try:
        from content_review import (
            CONTENT_REVIEW_VERSION,
            resolve_review_mode,
            review_candidates,
            review_final_sequence,
        )
        _content_review_version = CONTENT_REVIEW_VERSION
        _final_sequence_reviewer = review_final_sequence

        _content_review_mode = resolve_review_mode(settings)
        _content_review_summary = {
            "mode": _content_review_mode,
            "version": _content_review_version,
            "cache_hit": False,
            "main_count": 0,
            "reserve_count": 0,
            "retained_duration": 0.0,
            "grounded_card_count": 0,
            "fallback_reason": "",
        }
        _analysis_metadata["content_review_summary"] = _content_review_summary
        if not _director_mode:
            _content_review_summary["fallback_reason"] = "not_director_mode"
        elif _content_review_mode != "off":
            if _director_safe_candidate_duration + 0.1 < _duration_contract.source_min:
                _content_review_summary["fallback_reason"] = "safe_candidate_duration_insufficient"
                _log(
                    "AI\u5185\u5bb9\u5ba1\u7a3f: \u5b89\u5168\u5019\u9009\u65f6\u957f\u4e0d\u8db3\u4ee5\u6ee1\u8db3\u7247\u5355\u5408\u540c\uff0c\u76f4\u63a5\u6cbf\u7528\u65e7\u94fe\u8def\uff0c"
                    "\u4e0d\u989d\u5916\u8c03\u7528\u5ba1\u7a3f\u6a21\u578b"
                )
            else:
                _review_controls = _normalize_ai_controls(ai_controls)
                _candidate_digest = str(
                    (_analysis_metadata.get("candidate_contract") or {}).get("digest") or ""
                )
                if not _candidate_digest:
                    _candidate_digest = hashlib.sha256(
                        json.dumps(
                            _director_safe_inventory,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                _content_review_bundle = review_candidates(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    inventory=_director_safe_inventory,
                    candidate_digest=_candidate_digest,
                    category=_cross_cat_preferred or "",
                    main_product=_review_controls.get("main_product") or "",
                    avoid=_review_controls.get("avoid") or [],
                    required_sources=_director_required_sources,
                    log_fn=_log,
                )
                _analysis_metadata["content_review_summary"] = _content_review_bundle.summary(
                    _content_review_mode
                )
                _log(
                    f"AI\u5185\u5bb9\u5ba1\u7a3f: {_content_review_mode}\u6a21\u5f0f\uff0c\u4fdd\u7559"
                    f"{len(_content_review_bundle.cards)}\u5f20\u6709\u539f\u6587\u8bc1\u636e\u7684\u5185\u5bb9\u5361/"
                    f"{_content_review_bundle.retained_duration:.1f}s"
                    + ("\uff08\u7f13\u5b58\u547d\u4e2d\uff09" if _content_review_bundle.cache_hit else "")
                )
                _review_run_min = min(
                    _director_safe_candidate_duration,
                    min(
                        150.0,
                        max(_duration_contract.source_min, _AI_TARGET_DURATION * 1.2),
                    ),
                )
                if (
                    _content_review_mode == "on"
                    and _content_review_bundle.retained_duration + 0.1 < _review_run_min
                ):
                    _analysis_metadata["content_review_summary"]["fallback_reason"] = (
                        "reviewed_candidate_duration_insufficient"
                    )
                    _log(
                        f"AI\u5185\u5bb9\u5ba1\u7a3f: \u5ba1\u7a3f\u6c60{_content_review_bundle.retained_duration:.1f}s"
                        f"\u4f4e\u4e8e\u672c\u6b21\u6240\u9700{_review_run_min:.1f}s\uff0c\u672c\u6b21\u9000\u56de\u65e7\u5019\u9009"
                    )
                elif _content_review_mode == "on":
                    _content_review_allowed_ids = _content_review_bundle.allowed_candidate_ids
                    _content_review_hint = _content_review_bundle.director_hint()
                    _content_review_topic_support = _content_review_bundle.topic_support(
                        _director_safe_inventory
                    )
                    _content_review_applied = True
                    _log(
                        f"AI\u5185\u5bb9\u5ba1\u7a3f: \u5bfc\u6f14\u5019\u9009 {_director_candidate_count} -> "
                        f"{len(_content_review_allowed_ids)}\uff0c\u539f\u7f16\u53f7\u548c\u65f6\u95f4\u6233\u4fdd\u6301\u4e0d\u53d8"
                    )
    except Exception as _review_error:
        _analysis_metadata["content_review_summary"] = {
            "mode": _content_review_mode,
            "version": _content_review_version,
            "cache_hit": False,
            "main_count": 0,
            "reserve_count": 0,
            "retained_duration": 0.0,
            "grounded_card_count": 0,
            "fallback_reason": str(_review_error)[:240],
        }
        _content_review_allowed_ids = None
        _content_review_hint = ""
        _content_review_topic_support = {}
        _content_review_applied = False
        if _content_review_mode != "off":
            _log(f"AI\u5185\u5bb9\u5ba1\u7a3f: {_review_error}\uff0c\u81ea\u52a8\u9000\u56de\u65e7\u5019\u9009\u94fe\u8def")
    for attempt in range(_max_attempts):
        if _director_mode:
            _log(f"AI: 调用 {model}（{_director_stage}）...")
        else:
            _log(f"AI: 调用 {model}(第 {attempt + 1} 次)...")
        if _director_expand_source_clips is not None:
            clips = _call_director_expand_selection(
                api_key,
                base_url,
                model,
                _director_expand_source_clips,
                _indexed_srt_entries,
                _AI_TARGET_DURATION,
                log_fn,
                preferred_focus=_director_focus_hint,
                duration_contract=_duration_contract,
                merge_mode=merge_mode,
                required_sources=_director_required_sources,
            )
        elif _director_trim_source_clips is not None:
            clips = _call_director_trim_selection(
                api_key,
                base_url,
                model,
                _director_trim_source_clips,
                _AI_TARGET_DURATION,
                log_fn,
                preferred_focus=_director_focus_hint,
                duration_contract=_duration_contract,
                required_sources=_director_required_sources,
            )
        else:
            clips = _call_ai(
                api_key,
                base_url,
                model,
                cleaned_srt,
                log_fn,
                focus_hint=_director_focus_hint if _director_mode else focus_hint,
                srt_entries=_director_call_entries,
                hook_candidates_hint=hook_candidates_hint,
                ai_controls=ai_controls,
                recent_history_hint=_recent_history_hint,
                extra_instruction=_director_repair or None,
                main_category=_cross_cat_preferred,
                duration_contract=_duration_contract,
                merge_mode=merge_mode,
                required_sources=_director_required_sources,
                allowed_candidate_ids=_content_review_allowed_ids,
                content_review_hint=_content_review_hint,
                review_topic_support=_content_review_topic_support,
            )
        if not clips and _director_expand_source_clips is not None:
            clips = list(_director_expand_source_clips)
            _log("AI增量补片未产生可执行操作，保留锁定片单，不回到完整重编")
        elif not clips and _director_trim_source_clips is not None:
            clips = list(_director_trim_source_clips)
            _log("AI删片操作未产生可执行结果，保留锁定片单，不回到完整重编")
        if not clips:
            if _director_mode:
                if (
                    _director_expand_source_clips is None
                    and _director_trim_source_clips is None
                    and not _director_format_retry_used
                    and attempt + 1 < _max_attempts
                ):
                    _director_format_retry_used = True
                    _director_stage = "响应格式重试"
                    _director_repair = (
                        "【响应格式纠正】上一次响应没有解析出可用片单。"
                        "请严格只输出JSON对象，包含clips和expansion_plan；clips每项只包含clip_type、srt_indices、focus、reason、trim_priority；"
                        "srt_indices必须是连续的整数数组，不要解释、不要Markdown、不要输出时间戳。】"
                    )
                    _log("AI响应未解析出可用片单，只进行一次格式重试")
                    continue
                break
            continue
        if _director_mode:
            if not _director_focus_summary:
                _director_focus_summary = dict(get_last_analysis_metadata()["preference_summary"] or {})
                _director_focus_hint = _current_focus_used_label() or focus_hint
            elif _director_focus_summary:
                _set_last_focus_summary(_director_focus_summary)

            # Candidate text and timestamps are immutable after indexing. The AI,
            # hard audit, preview and cutter must all consume the same boundaries.
            if not clips:
                continue

            _hook_candidate_summary = dict(
                get_last_analysis_metadata().get("hook_candidate_summary") or {}
            )
            _audit_preferred_focus = (
                _hook_candidate_summary.get("requested_focus")
                or _director_focus_hint
                or focus_hint
                or ""
            )
            _require_preference_hook = bool(
                _hook_candidate_summary.get("preference_hook_required")
            )
            _require_preference_mainline = bool(
                _require_preference_hook
                and int(_hook_candidate_summary.get("preference_candidate_count") or 0) >= 3
                and _preference_quota_supported(_audit_preferred_focus)
            )
            clips = _stabilize_director_structure(
                clips,
                _indexed_srt_entries,
                _hook_candidate_summary,
                log_fn,
            )
            clips, _director_audit = _director_hard_audit(
                clips,
                _AI_TARGET_DURATION,
                _hook_cap_sec,
                log_fn,
                preferred_focus=_audit_preferred_focus,
                require_preference_hook=_require_preference_hook,
                require_preference_mainline=_require_preference_mainline,
                preference_target_duration=_final_target_duration,
                duration_contract=_duration_contract,
            )
            if _director_audit.get("duration_long"):
                _priority_trimmed = _apply_declared_trim_priorities(
                    clips,
                    _AI_TARGET_DURATION,
                    log_fn,
                    duration_contract=_duration_contract,
                    required_sources=_director_required_sources,
                )
                if _priority_trimmed:
                    clips, _director_audit = _director_hard_audit(
                        _priority_trimmed,
                        _AI_TARGET_DURATION,
                        _hook_cap_sec,
                        log_fn,
                        preferred_focus=_audit_preferred_focus,
                        require_preference_hook=_require_preference_hook,
                        require_preference_mainline=_require_preference_mainline,
                        preference_target_duration=_final_target_duration,
                        duration_contract=_duration_contract,
                    )
            _missing_sources_before_expansion = _director_missing_sources(
                clips,
                _director_required_sources,
            )
            if (
                (_director_audit.get("duration_short") or _missing_sources_before_expansion)
                and _director_expand_source_clips is None
            ):
                _declared_expansion = list(
                    _analysis_metadata_context().get("expansion_plan") or []
                )
                _expanded = _apply_ai_expansion_plan(
                    clips,
                    _declared_expansion,
                    _indexed_srt_entries,
                    _AI_TARGET_DURATION,
                    log_fn,
                    duration_contract=_duration_contract,
                    required_sources=_director_required_sources,
                )
                if _expanded:
                    clips, _director_audit = _director_hard_audit(
                        _expanded,
                        _AI_TARGET_DURATION,
                        _hook_cap_sec,
                        log_fn,
                        preferred_focus=_audit_preferred_focus,
                        require_preference_hook=_require_preference_hook,
                        require_preference_mainline=_require_preference_mainline,
                        preference_target_duration=_final_target_duration,
                        duration_contract=_duration_contract,
                    )
                    if _director_audit.get("duration_long"):
                        _priority_trimmed = _apply_declared_trim_priorities(
                            clips,
                            _AI_TARGET_DURATION,
                            log_fn,
                            duration_contract=_duration_contract,
                            required_sources=_director_required_sources,
                        )
                        if _priority_trimmed:
                            clips, _director_audit = _director_hard_audit(
                                _priority_trimmed,
                                _AI_TARGET_DURATION,
                                _hook_cap_sec,
                                log_fn,
                                preferred_focus=_audit_preferred_focus,
                                require_preference_hook=_require_preference_hook,
                                require_preference_mainline=_require_preference_mainline,
                                preference_target_duration=_final_target_duration,
                                duration_contract=_duration_contract,
                            )
            _director_last_audit = dict(_director_audit or {})
            _audit_total = float(_director_audit.get("total_duration") or 0.0)
            if (
                _director_best_duration <= 0
                or abs(_audit_total - float(_AI_TARGET_DURATION or 0))
                < abs(_director_best_duration - float(_AI_TARGET_DURATION or 0))
            ):
                _director_best_duration = _audit_total
            _director_hard_issues = list(_director_audit.get("issues") or [])
            _director_fatal = [
                issue for issue in _director_fatal_issues(_director_audit)
                if "低于目标下限" not in str(issue) and "至少还需补足" not in str(issue)
            ]
            _director_missing_sources_now = _director_missing_sources(
                clips,
                _director_required_sources,
            )
            _director_relaxed_duration = _director_duration_status(
                clips,
                _AI_TARGET_DURATION,
                _duration_contract,
                shortage_grace_seconds=SHORTAGE_GRACE_SECONDS,
            )
            if (
                clips
                and _director_audit.get("duration_short")
                and not _director_fatal
                and _director_relaxed_duration.get("accepted")
                and (
                    not _director_short_fallback_clips
                    or _audit_total > float(_director_short_fallback_status.get("total") or 0.0)
                )
            ):
                _director_short_fallback_clips = list(clips)
                _director_short_fallback_status = dict(_director_relaxed_duration)
            _director_has_hard_issue = bool(
                _director_fatal
                or _director_audit.get("duration_short")
            )
            if clips and not _director_has_hard_issue and not _director_fallback_clips:
                _director_fallback_clips = list(clips)
            if not clips:
                break

            if _director_audit.get("duration_long"):
                if _director_trim_source_clips is None and attempt + 1 < _max_attempts:
                    _director_stage = "AI片单精简"
                    _director_trim_source_clips = list(clips)
                    _director_expand_source_clips = None
                    _director_repair = ""
                    _log(
                        "AI叙事质检: 总时长超过合同，锁定Hook与现有顺序，"
                        "仅交回AI生成整段Product删除优先级"
                    )
                    continue
                _log("AI叙事质检未通过: 删片操作后仍超过时长上限，不重新生成完整片单")
                break

            if _director_fatal:
                _log(
                    "AI叙事质检未通过: "
                    + "；".join(_director_fatal)
                    + "；局部结构稳定仍无法修复，停止本次片单，不重新生成完整片单"
                )
                break

            if _director_audit.get("duration_short"):
                _duration_issue = list(_director_audit.get("issues") or [
                    f"总时长{_director_audit.get('total_duration', 0):.1f}s低于目标下限{_director_audit.get('duration_low', 0):.0f}s"
                ])
                if attempt + 1 < _max_attempts:
                    _director_stage = "AI片单增量补足"
                    _director_expand_source_clips = list(clips)
                    _director_trim_source_clips = None
                    _director_repair = ""
                    _log(
                        "AI叙事质检: "
                        + "；".join(_duration_issue)
                        + "，锁定现有叙事，仅交回AI生成Product插入操作"
                    )
                    continue
                _log(
                    f"AI叙事质检: 片单仅{_director_audit.get('total_duration', 0):.1f}s，"
                    f"低于目标下限{_director_audit.get('duration_low', 0):.0f}s"
                )
                break

            _director_missing_sources_now = _director_missing_sources(
                clips,
                _director_required_sources,
            )
            if _director_missing_sources_now:
                _source_deficits_now = _director_source_deficits(
                    clips,
                    _director_required_sources,
                )
                _source_quota_action = _director_source_quota_action(
                    clips,
                    _director_required_sources,
                    attempt,
                    _max_attempts,
                )
                _source_issue = (
                    "混剪来源配额不足:"
                    + "、".join(
                        f"{source.strip('[]')}还需{_source_deficits_now.get(source, 1)}段"
                        for source in _director_missing_sources_now
                    )
                )
                _director_last_audit = dict(_director_audit or {})
                _director_last_audit["warnings"] = list(
                    _director_last_audit.get("warnings") or []
                ) + [_source_issue]
                if _source_quota_action == "repair":
                    _director_stage = "混剪来源增量补足"
                    _director_expand_source_clips = list(clips)
                    _director_trim_source_clips = None
                    _log(f"AI叙事质检: {_source_issue}，锁定现有叙事，仅补缺失来源Product")
                    continue
                _log(f"AI叙事提示: {_source_issue}；已完成自动补足尝试，保留当前安全片单继续成片")

            _source_distribution = _director_source_distribution_summary(
                clips,
                _director_required_sources,
            )
            if merge_mode and _source_distribution.get("issues"):
                _log(
                    "AI叙事提示: 混剪来源分布仍不完全均衡:"
                    + "、".join(_source_distribution.get("issues") or [])
                    + "；最低来源合同已满足，保留当前叙事，不进行整片重编"
                )
            if _director_audit.get("warnings"):
                _log("AI叙事提示: " + "；".join(_director_audit.get("warnings") or []))

            if (
                _content_review_applied
                and _final_sequence_reviewer is not None
                and _content_review_allowed_ids
            ):
                _selected_sequence = []
                for _order, _clip in enumerate(clips, 1):
                    _selected_sequence.append({
                        "order": _order,
                        "clip_type": str(_clip[0] if len(_clip) > 0 else "product"),
                        "srt_indices": _director_clip_entry_indices(
                            _clip, _indexed_srt_entries
                        ),
                        "source": _director_clip_source_key(_clip).upper(),
                        "duration_sec": round(_clip_duration_value(_clip), 1),
                        "focus": str(_clip[6] if len(_clip) > 6 else "")[:40],
                        "text": re.sub(
                            r"\s+", " ", str(_clip[1] if len(_clip) > 1 else "")
                        ).strip()[:240],
                    })
                _review_inventory = [
                    item for item in _director_safe_inventory
                    if int(item.get("srt_index") or 0) in _content_review_allowed_ids
                ]
                try:
                    _final_review = _final_sequence_reviewer(
                        api_key=api_key,
                        base_url=base_url,
                        model=model,
                        selected_sequence=_selected_sequence,
                        inventory=_review_inventory,
                        allowed_candidate_ids=_content_review_allowed_ids,
                        category=_cross_cat_preferred or "",
                        preference=_audit_preferred_focus or "",
                        duration_low=float(_director_audit.get("duration_low") or 0.0),
                        duration_high=float(_director_audit.get("duration_high") or 0.0),
                        required_sources=_director_required_sources,
                        log_fn=_log,
                    )
                    _final_review_summary = _final_review.summary()
                    _analysis_metadata["content_review_summary"]["final_review"] = (
                        _final_review_summary
                    )
                    if _final_review.status == "pass":
                        _log(
                            "AI\u6210\u7247\u7ec8\u5ba1: \u901a\u8fc7\uff0cHook\u3001\u627f\u63a5\u3001\u6b63\u6587\u548c\u7ed3\u5c3e\u65e0\u9700\u6539\u5199"
                        )
                    elif _final_review.status == "revise":
                        _reviewed_clips = _parse_ai_response(
                            json.dumps(
                                {"clips": list(_final_review.clips)},
                                ensure_ascii=False,
                            ),
                            _log,
                            _indexed_srt_entries,
                            set(),
                            require_srt_indices=True,
                            allowed_candidate_indices=_content_review_allowed_ids,
                        )
                        if _final_review.expansion_plan:
                            _reviewed_expanded = _apply_ai_expansion_plan(
                                _reviewed_clips,
                                list(_final_review.expansion_plan),
                                _indexed_srt_entries,
                                _AI_TARGET_DURATION,
                                _log,
                                label="AI\u6210\u7247\u7ec8\u5ba1\u8865\u8db3",
                                duration_contract=_duration_contract,
                                required_sources=_director_required_sources,
                            )
                            if _reviewed_expanded:
                                _reviewed_clips = _reviewed_expanded
                        _reviewed_clips = _stabilize_director_structure(
                            _reviewed_clips,
                            _indexed_srt_entries,
                            _hook_candidate_summary,
                            _log,
                        )
                        _reviewed_clips, _reviewed_audit = _director_hard_audit(
                            _reviewed_clips,
                            _AI_TARGET_DURATION,
                            _hook_cap_sec,
                            _log,
                            preferred_focus=_audit_preferred_focus,
                            require_preference_hook=False,
                            require_preference_mainline=_require_preference_mainline,
                            preference_target_duration=_final_target_duration,
                            duration_contract=_duration_contract,
                        )
                        _reviewed_missing_sources = _director_missing_sources(
                            _reviewed_clips,
                            _director_required_sources,
                        )
                        _reviewed_failures = list(
                            _director_fatal_issues(_reviewed_audit)
                        )
                        if _reviewed_audit.get("duration_short"):
                            _reviewed_failures.append("\u7ec8\u5ba1\u4fee\u8ba2\u7247\u5355\u65f6\u957f\u4e0d\u8db3")
                        if _reviewed_audit.get("duration_long"):
                            _reviewed_failures.append("\u7ec8\u5ba1\u4fee\u8ba2\u7247\u5355\u8d85\u65f6")
                        if int(_reviewed_audit.get("hard_removed") or 0):
                            _reviewed_failures.append("\u7ec8\u5ba1\u4fee\u8ba2\u7247\u5355\u89e6\u53d1\u786c\u8fc7\u6ee4")
                        if _reviewed_missing_sources:
                            _reviewed_failures.append(
                                "\u7ec8\u5ba1\u4fee\u8ba2\u7247\u5355\u7f3a\u5c11\u6765\u6e90:"
                                + ",".join(_reviewed_missing_sources)
                            )
                        _old_signature = [
                            (
                                str(item.get("clip_type") or ""),
                                tuple(item.get("srt_indices") or []),
                            )
                            for item in _selected_sequence
                        ]
                        _new_signature = [
                            (
                                str(item.get("clip_type") or ""),
                                tuple(item.get("srt_indices") or []),
                            )
                            for item in _final_review.clips
                        ]
                        if _new_signature == _old_signature:
                            _reviewed_failures.append("\u7ec8\u5ba1\u4fee\u8ba2\u672a\u6539\u53d8\u7247\u5355")
                        if _reviewed_failures:
                            _fallback_reason = "\uff1b".join(_reviewed_failures[:4])
                            _analysis_metadata["content_review_summary"]["final_review"] = (
                                _final_review.summary(
                                    applied=False,
                                    fallback_reason=_fallback_reason,
                                )
                            )
                            _log(
                                "AI\u6210\u7247\u7ec8\u5ba1: \u4fee\u8ba2\u7ed3\u679c\u672a\u901a\u8fc7\u65f6\u957f/\u6765\u6e90/\u5b89\u5168\u5408\u540c\uff0c"
                                "\u4fdd\u7559\u5bfc\u6f14\u539f\u7247\u5355\uff08" + _fallback_reason + "\uff09"
                            )
                        else:
                            clips = _reviewed_clips
                            _director_audit = _reviewed_audit
                            _director_last_audit = dict(_reviewed_audit or {})
                            _analysis_metadata["content_review_summary"]["final_review"] = (
                                _final_review.summary(applied=True)
                            )
                            _issue_text = "\uff1b".join(_final_review.issues[:3])
                            _log(
                                f"AI\u6210\u7247\u7ec8\u5ba1: \u5df2\u5e94\u7528\u4e00\u6b21\u5b8c\u6574\u7247\u5355\u4fee\u8ba2\uff0c"
                                f"{len(_selected_sequence)}\u6bb5 -> {len(clips)}\u6bb5"
                                + (f"\uff08{_issue_text}\uff09" if _issue_text else "")
                            )
                except Exception as _final_review_error:
                    _analysis_metadata["content_review_summary"]["final_review"] = {
                        "status": "unavailable",
                        "issue_count": 0,
                        "applied": False,
                        "fallback_reason": str(_final_review_error)[:240],
                    }
                    _log(
                        f"AI\u6210\u7247\u7ec8\u5ba1: {_final_review_error}\uff0c"
                        "\u4fdd\u7559\u5df2\u901a\u8fc7\u786c\u5408\u540c\u7684\u5bfc\u6f14\u539f\u7247\u5355"
                    )

            _remaining_issues = _director_fatal_issues(_director_audit)
            if _remaining_issues:
                _log(
                    "AI叙事质检未通过: "
                    + "；".join(_remaining_issues)
                    + "；不重新生成完整片单"
                )
                break
            _director_topic_summary = _topic_coverage_summary(
                clips,
                _current_focus_used_label(),
                _final_target_duration,
                get_last_analysis_metadata()["preference_summary"].get("requested", "自动"),
            )
            _set_last_topic_coverage_summary(_director_topic_summary)
            if merge_mode:
                _selected_source_counts = _director_source_counts(clips)
                _source_distribution = _director_source_distribution_summary(
                    clips,
                    _director_required_sources,
                )
                _source_contract_summary = dict(_analysis_metadata.get("source_contract") or {})
                _source_contract_summary["selected_counts"] = dict(sorted(_selected_source_counts.items()))
                _source_contract_summary["distribution"] = _source_distribution
                _source_contract_summary["balanced"] = bool(_source_distribution.get("balanced"))
                _analysis_metadata["source_contract"] = _source_contract_summary
                if _selected_source_counts:
                    _log(
                        "混剪来源成片分布: "
                        + "、".join(
                            f"{source.strip('[]')}={count}段"
                            for source, count in sorted(_selected_source_counts.items())
                        )
                        + f"；最长连续同源{int(_source_distribution.get('longest_run') or 0)}段"
                    )
            _record_history_if_needed(clips)
            _selection_manifest = SelectionManifest.from_clips(
                clips,
                candidate_digest=str((_analysis_metadata.get("candidate_contract") or {}).get("digest") or ""),
                duration_contract=_duration_contract,
            )
            _analysis_metadata["selection_manifest"] = _selection_manifest.to_dict()
            _analysis_metadata["selection_result"] = SelectionResult.success(_selection_manifest).to_dict()
            _log(
                f"AI叙事编排完成: {len(clips)}段/{sum(_clip_duration_value(c) for c in clips):.1f}s，"
                f"片单合同 {_selection_manifest.digest[:12]}；"
                "程序仅执行硬合规检查及AI声明的增删操作，未做主题判断或叙事重排"
            )
            return clips
        _active_preference = _current_focus_used_label()
        _attempt_focus_summary = dict(get_last_analysis_metadata()["preference_summary"] or {})
        _preference_quota, _preference_max = _preference_target_bounds(
            _AI_TARGET_DURATION,
            _attempt_focus_summary.get("requested", "自动"),
        )
        _preference_count = _preferred_focus_clip_count(clips, _active_preference)
        if (
            attempt < 2
            and _preference_quota_supported(_active_preference)
            and _preference_count < _preference_quota
        ):
            _need_preference = _preference_quota - _preference_count
            _log(
                f"AI: 偏好配额不足 {_active_preference} {_preference_count}/{_preference_quota}，"
                f"定向补选{_need_preference}段..."
            )
            _preference_instruction = (
                f"【只补选“{_active_preference}”主题的{_need_preference}个Product片段。"
                "每段原字幕必须有明确主题证据，不能只在focus/reason里写标签；"
                "不要Hook、不要Close、不要颜色或面料等其他主题、不要重复已有片段、"
                "不要直播口癖、价格、券、链接和违禁词。仅输出新增片段JSON数组。】"
            )
            _preference_supplement = _call_ai(
                api_key,
                base_url,
                model,
                cleaned_srt,
                _log,
                focus_hint=_active_preference,
                srt_entries=_indexed_srt_entries,
                hook_candidates_hint=None,
                ai_controls=ai_controls,
                recent_history_hint=_recent_history_hint,
                extra_instruction=_preference_instruction,
                main_category=_cross_cat_preferred,
                duration_contract=_duration_contract,
            )
            _set_last_focus_summary(_attempt_focus_summary)
            _preferred_block = _focus_label_to_block(_active_preference)
            _preference_supplement = [
                _retag_clip_type(item, "product")
                for item in (_preference_supplement or [])
                if _clip_focus_block(item) == _preferred_block
            ]
            _added_preference = _append_unique_supplement_clips(
                clips,
                _preference_supplement,
                _AI_TARGET_DURATION,
                _need_preference,
            )
            _log(
                f"AI: 偏好定向补选加入{_added_preference}段，当前"
                f"{_preferred_focus_clip_count(clips, _active_preference)}/{_preference_quota}"
            )
        if attempt == 0 and _preference_quota_supported(_active_preference):
            _coverage_targets, _coverage_support, _coverage_before = _coverage_target_topics(
                clips,
                _indexed_srt_entries,
                _active_preference,
                _AI_TARGET_DURATION,
            )
            if not _coverage_before.get("balanced") and _coverage_targets:
                _log(
                    "主题覆盖不足: "
                    f"偏好{_coverage_before.get('preference_count', 0)}/{_coverage_before.get('product_count', 0)}，"
                    f"主题{_coverage_before.get('distinct_count', 0)}/{_coverage_before.get('min_distinct', 0)}；"
                    f"定向补 {','.join(dict.fromkeys(_coverage_targets))}"
                )
                _coverage_candidates = _topic_candidates_from_srt_entries(
                    _indexed_srt_entries,
                    _coverage_targets,
                    limit_per_topic=2,
                )
                _coverage_candidates = _filter_context_damaged_clips(
                    _coverage_candidates,
                    cleaned_srt,
                    None,
                )
                for _coverage_topic in dict.fromkeys(_coverage_targets):
                    _coverage_need = min(2, _coverage_targets.count(_coverage_topic))
                    _coverage_instruction = (
                        f"【主题补选：只补选“{_coverage_topic}”主题的{_coverage_need}个Product片段。"
                        f"本片偏好“{_active_preference}”已经足够，不要再选该偏好主题。"
                        "原字幕必须有独立商品证据，内容完整，不要价格、链接、券、直播互动和重复片段。"
                        "仅输出新增片段JSON数组。】"
                    )
                    _topic_result = _call_ai(
                        api_key,
                        base_url,
                        model,
                        cleaned_srt,
                        _log,
                        focus_hint=_coverage_topic,
                        srt_entries=_indexed_srt_entries,
                        hook_candidates_hint=None,
                        ai_controls=ai_controls,
                        recent_history_hint=_recent_history_hint,
                        extra_instruction=_coverage_instruction,
                        main_category=_cross_cat_preferred,
                    )
                    _set_last_focus_summary(_attempt_focus_summary)
                    _topic_result = [_retag_clip_type(item, "product") for item in (_topic_result or [])]
                    _topic_result = _filter_price_and_cta(_topic_result, None)
                    _topic_result = _filter_host_interaction(_topic_result, None)
                    _topic_result = _filter_context_damaged_clips(_topic_result, cleaned_srt, None)
                    _topic_result = [
                        item for item in _topic_result
                        if _clip_primary_topic(item) == _coverage_topic
                    ]
                    _coverage_candidates.extend(_topic_result[:_coverage_need])
                for _coverage_candidate in _coverage_candidates:
                    if not any(
                        abs(float(_coverage_candidate[2]) - float(old[2])) < 0.15
                        for old in _coverage_candidate_pool
                    ):
                        _coverage_candidate_pool.append(_coverage_candidate)
                clips, _coverage_replaced = _replace_for_topic_coverage(
                    clips,
                    _coverage_candidates,
                    _active_preference,
                    _AI_TARGET_DURATION,
                    _log,
                )
                _coverage_after = _topic_coverage_summary(
                    clips,
                    _active_preference,
                    _AI_TARGET_DURATION,
                    _attempt_focus_summary.get("requested", "自动"),
                )
                _log(
                    f"主题覆盖校正: 替换{_coverage_replaced}段，"
                    f"偏好{_coverage_after.get('preference_count', 0)}/{_coverage_after.get('product_count', 0)}，"
                    f"主题{_coverage_after.get('distinct_count', 0)}类"
                )
        # 检查AI选的片段数是否达标；若总时长已经接近目标，不再为了凑段数二次补选。
        _current_ai_dur = sum(float(c[5]) for c in clips if len(c) >= 6)
        _target_floor_for_supplement = max(25, int(_AI_TARGET_DURATION * 0.95))
        if clips and len(clips) < _target_min_clips and attempt < 2 and _current_ai_dur < _target_floor_for_supplement:
            _need_supplement = max(0, _target_min_clips - len(clips))
            _supplement_cap = _target_supplement_cap(_AI_TARGET_DURATION)
            _supplement_limit = min(_supplement_cap, _need_supplement)
            log_fn(f"AI: 当前{len(clips)}段/{_current_ai_dur:.1f}s < 目标{_target_min_clips}段/{_target_floor_for_supplement}s，最多补选{_supplement_limit}段...")
            _supplement_targets, _, _ = _coverage_target_topics(
                clips, _indexed_srt_entries, _active_preference, _AI_TARGET_DURATION
            )
            _supplement_focus = _supplement_targets[0] if _supplement_targets else "通用卖点"
            _extra_hint = (
                f"【注意：刚才只选了{len(clips)}段/{_current_ai_dur:.1f}秒。"
                f"请补选{_supplement_limit}个以内高质量短片段，优先补“{_supplement_focus}”等当前缺失卖点，"
                f"不要继续堆叠“{_active_preference}”，把总时长补到{_AI_TARGET_DURATION}秒左右；"
                "不要重复已有片段。仅输出新增片段JSON数组。】"
            )
            _supplement = _call_ai(api_key, base_url, model, cleaned_srt, _log, focus_hint=_supplement_focus, srt_entries=_indexed_srt_entries, hook_candidates_hint=hook_candidates_hint, ai_controls=ai_controls, recent_history_hint=_recent_history_hint, extra_instruction=_extra_hint, main_category=_cross_cat_preferred)
            _set_last_focus_summary(_attempt_focus_summary)
            if _supplement:
                _added_supplement = _append_unique_supplement_clips(clips, _supplement, _AI_TARGET_DURATION, _supplement_limit)
                _log(f"AI: 补选{_added_supplement}段，补选后共{len(clips)}段")
        elif clips and len(clips) < _target_min_clips and attempt < 2:
            log_fn(f"AI: 当前{len(clips)}段但已有{_current_ai_dur:.1f}s，接近目标，跳过补选避免超时长")
        original_clips = list(clips)
        removed_from_dedup = []
        clips = _dedup_clips(
            clips,
            log_fn,
            multi_version=multi_version,
            focus_hint=focus_hint,
            srt_text=srt_text,
            main_category=_cross_cat_preferred,
            preferred_focus=_current_focus_used_label(),
            ai_controls=ai_controls,
            merge_mode=merge_mode,
        )
        # 从Product中提取Hook（如果AI没选Hook）
        clips = _extract_hook_from_products(clips, cleaned_srt, log_fn, focus_hint=focus_hint, ai_controls=ai_controls)
        clips = _force_short_hook(clips, cleaned_srt, log_fn, max_hook_sec=_hook_cap_sec, focus_hint=focus_hint, ai_controls=ai_controls)
        clips = _refine_hook_by_dynamic_score(clips, cleaned_srt, log_fn, focus_hint=focus_hint, ai_controls=ai_controls)
        clips = _filter_hook_product_repeats(clips, log_fn)
        removed_from_dedup = [c for c in original_clips if c not in clips]
        # [v9.5] 多版本模式：去重后如果片段不足12个，回收被去除的片段
        if _skip_focus and len(clips) < 12 and removed_from_dedup:
            # 按评分排序回收，优先补回高分的
            removed_sorted = sorted(removed_from_dedup, key=lambda c: c[4], reverse=True)
            added = 0
            for rc in removed_sorted:
                # 避免时间重叠（和已有片段间隔>2s）
                overlap = False
                for ec in clips:
                    if abs(rc[2] - ec[2]) < 2 or abs(rc[3] - ec[3]) < 2:
                        overlap = True; break
                if not overlap:
                    clips.append(rc)
                    removed_from_dedup.remove(rc)
                    added += 1
                    if len(clips) >= 12:
                        break
            if added > 0:
                _log(f"多版本回收: 补回{added}个片段，当前{len(clips)}个")
        if not clips:
            continue
        # [增强] 内容去重
        # 记住最好的结果
        _best_candidate_coverage = _topic_coverage_summary(
            clips,
            _active_preference,
            _AI_TARGET_DURATION,
            _attempt_focus_summary.get("requested", "自动"),
        )
        _best_candidate_key = (
            1 if _best_candidate_coverage.get("balanced") else 0,
            int(_best_candidate_coverage.get("distinct_count", 0)),
            -max(0, int(_best_candidate_coverage.get("preference_count", 0)) - int(_best_candidate_coverage.get("preference_max", 0))),
            len(clips),
        )
        if best_clips_key is None or _best_candidate_key > best_clips_key:
            best_clips = clips[:]
            best_clips_key = _best_candidate_key
        if _validate_clips(clips, log_fn, multi_version=multi_version):
            _log(f"AI: 校验通过，{len(clips)} 个片段")
            for ct, text, s, e, sc, d, *_ in clips:
                _log(f"  {ct:<16s} | {s:.1f}-{e:.1f}s ({d:.1f}s) | {text}")
            # 跨品类扫描(第二道防线)
            if _enforce_category_filter:
                clips = _post_filter_cross_category(clips, cleaned_srt, log_fn, preferred_cat=_cross_cat_preferred)
            # 叙事连贯性检查
            if _enforce_time_coherence:
                clips = _check_narrative_coherence(clips, log_fn)
            else:
                _log("AI选片规则: 已关闭时间连贯性检查")
            # 普通单视频可按同类型时间顺序整理；混剪多视频时间轴都从0开始，强排会打散AI叙事。
            if merge_mode:
                _log("混剪叙事: 保留 AI 编排顺序，跳过本地时间重排")
            else:
                clips = _reorder_clips_by_time(clips, log_fn)
            # ASR纠错:修正文案中的常见识别错误
            clips = [(ct, _apply_asr_corrections(text, log_fn), s, e, sc, d, focus)
                     for ct, text, s, e, sc, d, focus in (c[:7] if len(c) > 6 else (*c, "") for c in clips)]
            # 主播互动废话过滤
            clips = _filter_host_interaction(clips, log_fn)
            # 价格/CTA/脏话硬过滤（AI Prompt拦不住的用代码拦）
            clips = _filter_price_and_cta(clips, log_fn)
            clips = _filter_context_damaged_clips(clips, cleaned_srt, log_fn)
            # 语义重复过滤(代码层兜底)
            if not multi_version: clips = _filter_semantic_repeat(clips, log_fn)
            clips = _filter_hook_product_repeats(clips, log_fn)
            # 明星名字过滤
            clips = _filter_celebrity(clips, log_fn)
            # CTA误判校验
            clips = _validate_cta(clips, log_fn)
            # 先去重(在边界修复前，避免边界扩展导致误判重叠)
            clips = _dedup_clip_text_overlap(clips, log_fn, merge_mode=merge_mode)
            if not clips:
                _log("AI: 去重后无剩余")
                continue
            # [v8.5] 时长硬顶：先截断再检查重叠，避免巨型片段吞掉其他片段
            clips = _cap_clip_duration(clips, log_fn, srt_text=srt_text)
            # 片段边界修复:确保首尾对齐到完整句子
            clips = _fix_clip_boundaries(clips, cleaned_srt, log_fn)
            clips = _trim_product_size_prompt_tails(clips, cleaned_srt, log_fn)
            clips = _filter_focus_near_duplicates(clips, log_fn, target_duration=_AI_TARGET_DURATION)
            clips = _filter_hook_product_repeats(clips, log_fn)
            # [v9.2] 裁掉片段开头的语气词(对/嗯/呃等)对应的画面和音频
            clips = _trim_filler_start(clips, cleaned_srt, log_fn, word_timings=word_timings)
            clips = _trim_filler_middle(clips, cleaned_srt, log_fn)
            if _feedback_filter_enabled:
                clips = _filter_preview_feedback_rejected_clips(
                    clips, _feedback_profile, log_fn,
                    min_keep=_history_min_keep,
                    min_duration=_history_min_duration,
                )
            clips = _filter_recent_similar_clips(
                clips, _recent_history, log_fn,
                min_keep=_history_min_keep,
                min_duration=_history_min_duration,
            )
            try:
                _target_low, _target_high = _multi_version_target_bounds(_AI_TARGET_DURATION)
            except Exception:
                _target_low, _target_high = max(25, _AI_TARGET_DURATION * 0.85), _AI_TARGET_DURATION + 10
            _final_floor = max(float(_target_low), float(_AI_TARGET_DURATION) * 0.92)
            _final_total = sum(_clip_duration_value(c) for c in clips)
            if _final_total < _final_floor and attempt < 2:
                _need_seconds = max(0.0, _final_floor - _final_total)
                _need_count = max(
                    1,
                    min(
                        _target_supplement_cap(_AI_TARGET_DURATION),
                        max(0, _target_min_clips - len(clips)) + int((_need_seconds + 4.9) // 5),
                    ),
                )
                _final_supplement_targets, _, _ = _coverage_target_topics(
                    clips, _indexed_srt_entries, _active_preference, _AI_TARGET_DURATION
                )
                _final_supplement_focus = _final_supplement_targets[0] if _final_supplement_targets else "通用卖点"
                _extra_hint = (
                    f"【后处理后片单只剩{len(clips)}段/{_final_total:.1f}秒，仍低于目标。"
                    f"请额外补选{_need_count}个以内不同卖点的完整短句，优先补“{_final_supplement_focus}”，"
                    f"不要继续堆叠“{_active_preference}”，补到{_AI_TARGET_DURATION}秒附近；"
                    "不要重复已有片段，不要选价格/券/违禁词/废话。仅输出新增片段JSON数组。】"
                )
                _supplement = _call_ai(
                    api_key, base_url, model, cleaned_srt, _log,
                    focus_hint=_final_supplement_focus,
                    srt_entries=_indexed_srt_entries,
                    hook_candidates_hint=hook_candidates_hint,
                    ai_controls=ai_controls,
                    recent_history_hint=_recent_history_hint,
                    extra_instruction=_extra_hint,
                    main_category=_cross_cat_preferred,
                )
                _set_last_focus_summary(_attempt_focus_summary)
                _added_final = _append_unique_supplement_clips(clips, _supplement, _AI_TARGET_DURATION, _need_count)
                if _added_final:
                    _log(f"目标补选: 后处理后补入 {_added_final} 段，{_final_total:.1f}s -> {sum(_clip_duration_value(c) for c in clips):.1f}s")
                    clips = _filter_price_and_cta(clips, log_fn)
                    clips = _filter_context_damaged_clips(clips, cleaned_srt, log_fn)
                    if not multi_version:
                        clips = _filter_semantic_repeat(clips, log_fn)
                    clips = _filter_hook_product_repeats(clips, log_fn)
                    clips = _dedup_clip_text_overlap(clips, log_fn, merge_mode=merge_mode)
                    clips = _cap_clip_duration(clips, log_fn, srt_text=srt_text)
                    clips = _fix_clip_boundaries(clips, cleaned_srt, log_fn)
                    clips = _trim_product_size_prompt_tails(clips, cleaned_srt, log_fn)
                    clips = _filter_focus_near_duplicates(clips, log_fn, target_duration=_AI_TARGET_DURATION)
                    clips = _trim_filler_start(clips, cleaned_srt, log_fn, word_timings=word_timings)
                    clips = _trim_filler_middle(clips, cleaned_srt, log_fn)
                    if _feedback_filter_enabled:
                        clips = _filter_preview_feedback_rejected_clips(
                            clips, _feedback_profile, log_fn,
                            min_keep=_history_min_keep,
                            min_duration=_history_min_duration,
                        )
            # [v9.3 - DISABLED] tighten_clip_boundaries + 延伸 - 引起片段间跳跃废话
            # 改用AI Prompt控制片段长度和边界，代码层只做 trim_filler
            _total_dur = sum(c[5] for c in clips)
            _min_dur = max(25, _AI_TARGET_DURATION * 2 // 3)  # 兜底目标 ≈ 目标时长的2/3
            if _total_dur < _min_dur and removed_from_dedup:
                _log(f"兜底回收: 当前 {_total_dur:.1f}s < {_min_dur}s, 尝试回收被去重片段...")
                for rc in removed_from_dedup:
                    if sum(c[5] for c in clips) >= _min_dur:
                        break
                    if rc[5] <= 0 or rc[5] >= 8:
                        continue
                    # 检查重叠
                    _overlap = False
                    for ec in clips:
                        if rc[2] < ec[3] and rc[3] > ec[2]:
                            _overlap = True
                            break
                    if not _overlap:
                        # 插在最后一个close前面，不要加在close后面
                        last_close_idx = None
                        for ci, cc in enumerate(clips):
                            if 'close' in cc[0].lower():
                                last_close_idx = ci
                        if last_close_idx is not None:
                            clips.insert(last_close_idx, rc)
                        else:
                            clips.append(rc)
                        _log(f"  回收: {rc[2]:.1f}-{rc[3]:.1f}s ({rc[5]:.1f}s)")
            _final_total = sum(_clip_duration_value(c) for c in clips)
            if (len(clips) < 8 or _final_total < _final_floor) and attempt < 4:
                _log(
                    f"AI: 最终片单仅{len(clips)}段/{_final_total:.1f}s，"
                    f"未达到8段/{_final_floor:.0f}s质量底线，继续重试..."
                )
                import random as _r2
                temperature = round(_r2.uniform(0.5, 0.75), 2)  # 更高temperature刺激多样化
                _log(f"AI: 重试temperature={temperature}")
                original_clips = list(clips)
                continue
            clips = _enforce_target_duration_limit(clips, _AI_TARGET_DURATION, log_fn, feedback_profile=_feedback_active_profile)
            clips = _filter_price_and_cta(clips, log_fn)
            clips = _filter_context_damaged_clips(clips, cleaned_srt, log_fn)
            if _coverage_candidate_pool:
                clips, _final_coverage_replaced = _replace_for_topic_coverage(
                    clips,
                    _coverage_candidate_pool,
                    _active_preference,
                    _AI_TARGET_DURATION,
                    _log,
                )
                if _final_coverage_replaced:
                    clips = _reorder_product_focus_blocks(
                        clips,
                        _log,
                        preferred_cat=_cross_cat_preferred,
                        preferred_focus=_active_preference,
                        ai_controls=ai_controls,
                        merge_mode=merge_mode,
                    )
                    clips = _enforce_target_duration_limit(
                        clips,
                        _AI_TARGET_DURATION,
                        log_fn,
                        feedback_profile=_feedback_active_profile,
                    )
                    clips = _filter_price_and_cta(clips, log_fn)
                    clips = _filter_context_damaged_clips(clips, cleaned_srt, log_fn)
            _attempt_topic_summary = _topic_coverage_summary(
                clips,
                _active_preference,
                _AI_TARGET_DURATION,
                _attempt_focus_summary.get("requested", "自动"),
            )
            _set_last_topic_coverage_summary(_attempt_topic_summary)
            _available_non_preference = _available_topic_support(_indexed_srt_entries, _active_preference)
            _coverage_actionable = bool(
                _attempt_topic_summary.get("overconcentrated")
                or _attempt_topic_summary.get("underpreferred")
                or (_attempt_topic_summary.get("undercovered") and _available_non_preference)
            )
            if _coverage_actionable and attempt < 4:
                _log(
                    "AI: 主题覆盖未通过，"
                    f"偏好{_attempt_topic_summary.get('preference_count', 0)}/"
                    f"{_attempt_topic_summary.get('product_count', 0)}，"
                    f"主题{_attempt_topic_summary.get('distinct_count', 0)}/"
                    f"{_attempt_topic_summary.get('min_distinct', 0)}，继续重试..."
                )
                continue
            _record_history_if_needed(clips)
            return clips
        _log(f"AI: 第 {attempt + 1} 次校验未通过，重试...")

    if _director_mode:
        if _director_fallback_clips:
            _fallback_duration = _director_duration_status(
                _director_fallback_clips,
                _AI_TARGET_DURATION,
                _duration_contract,
            )
            _set_last_topic_coverage_summary(_topic_coverage_summary(
                _director_fallback_clips,
                _current_focus_used_label(),
                _AI_TARGET_DURATION,
                get_last_analysis_metadata()["preference_summary"].get("requested", "自动"),
            ))
            _record_history_if_needed(_director_fallback_clips)
            _log(
                f"AI整体修复未返回更优片单，使用最佳合格片单"
                f"（{len(_director_fallback_clips)}段/{_fallback_duration['total']:.1f}s，"
                f"目标下限{_fallback_duration['low']:.0f}s）"
            )
            return _director_fallback_clips
        if _director_short_fallback_clips:
            _relaxed_duration = dict(_director_short_fallback_status or {})
            _relaxation = {
                "applied": True,
                "grace_seconds": float(SHORTAGE_GRACE_SECONDS),
                "reason": "safe_candidates_exhausted",
                "source_duration": round(float(_relaxed_duration.get("total") or 0.0), 3),
                "projected_final_duration": round(float(_relaxed_duration.get("projected_final") or 0.0), 3),
                "standard_final_min": round(float(_duration_contract.final_min), 3),
                "relaxed_final_min": round(
                    max(1.0, float(_duration_contract.final_min) - float(SHORTAGE_GRACE_SECONDS)),
                    3,
                ),
                "standard_source_min": round(float(_duration_contract.source_min), 3),
                "relaxed_source_min": round(float(_relaxed_duration.get("relaxed_low") or 0.0), 3),
            }
            _analysis_metadata["duration_relaxation"] = _relaxation
            _set_last_topic_coverage_summary(_topic_coverage_summary(
                _director_short_fallback_clips,
                _current_focus_used_label(),
                _final_target_duration,
                get_last_analysis_metadata()["preference_summary"].get("requested", "自动"),
            ))
            if merge_mode:
                _selected_source_counts = _director_source_counts(_director_short_fallback_clips)
                _source_distribution = _director_source_distribution_summary(
                    _director_short_fallback_clips,
                    _director_required_sources,
                )
                _source_contract_summary = dict(_analysis_metadata.get("source_contract") or {})
                _source_contract_summary["selected_counts"] = dict(sorted(_selected_source_counts.items()))
                _source_contract_summary["distribution"] = _source_distribution
                _source_contract_summary["balanced"] = bool(_source_distribution.get("balanced"))
                _analysis_metadata["source_contract"] = _source_contract_summary
            _record_history_if_needed(_director_short_fallback_clips)
            _selection_manifest = SelectionManifest.from_clips(
                _director_short_fallback_clips,
                candidate_digest=str((_analysis_metadata.get("candidate_contract") or {}).get("digest") or ""),
                duration_contract=_duration_contract,
            )
            _analysis_metadata["selection_manifest"] = _selection_manifest.to_dict()
            _partial_message = (
                f"有效内容不足，已使用{SHORTAGE_GRACE_SECONDS:.0f}秒弹性时长保留完整片单"
            )
            _analysis_metadata["selection_result"] = SelectionResult.partial_insufficient(
                _selection_manifest,
                message=_partial_message,
                details=_relaxation,
            ).to_dict()
            _log(
                f"AI时长弹性: 安全候选已用尽，保留"
                f"{len(_director_short_fallback_clips)}段/{_relaxed_duration.get('total', 0):.1f}s原片，"
                f"预计成片{_relaxed_duration.get('projected_final', 0):.1f}s；"
                f"标准下限{_duration_contract.final_min:.0f}s，"
                f"内容不足宽限下限{_relaxation['relaxed_final_min']:.0f}s"
            )
            return _director_short_fallback_clips
        _final_issues = list(_director_last_audit.get("issues") or [])
        _duration_low = float(
            _director_last_audit.get("duration_low")
            or _multi_version_target_bounds(_AI_TARGET_DURATION)[0]
        )
        _duration_short = bool(_director_last_audit.get("duration_short"))
        _duration_high = float(
            _director_last_audit.get("duration_high")
            or _multi_version_target_bounds(_AI_TARGET_DURATION)[1]
        )
        _duration_long = bool(_director_last_audit.get("duration_long"))
        _fatal_without_duration = [
            issue for issue in _director_fatal_issues(_director_last_audit)
            if "低于目标下限" not in issue and "至少还需补足" not in issue
        ]
        _failure_code = "narrative_rejected"
        if _duration_short and not _fatal_without_duration:
            _failure_code = (
                "insufficient_content"
                if _director_safe_candidate_duration + 0.05 < _duration_low
                else "ai_duration_contract_failed"
            )
        elif _duration_long:
            _failure_code = "duration_out_of_range"
        _failure_reason = "；".join(_final_issues) or "AI响应未解析出可用片单"
        _set_last_selection_failure({
            "code": _failure_code,
            "reason": _failure_reason,
            "candidate_count": _director_candidate_count,
            "best_duration": round(_director_best_duration, 1),
            "duration_low": round(_duration_low, 1),
            "duration_high": round(_duration_high, 1),
            "target_duration": float(_AI_TARGET_DURATION or 0),
            "safe_candidate_duration": round(_director_safe_candidate_duration, 1),
        })
        _log(
            "AI最终拒绝原因: "
            f"{_failure_reason}（候选{_director_candidate_count}条，"
            f"最佳{_director_best_duration:.1f}s，目标下限{_duration_low:.0f}s）"
        )
        return []

    # 用最好的结果(不硬拒绝)
    if best_clips:
        _log(f"AI: 使用最佳结果({len(best_clips)} 片段)")
        clips = _dedup_clip_text_overlap(best_clips, log_fn, merge_mode=merge_mode)
    # ★AI选片后跨品类二次过滤★：AI可能选了含纯次品类的片段
    if clips:
        if _enforce_category_filter:
            clips = _post_filter_cross_category(clips, cleaned_srt, log_fn, preferred_cat=_cross_cat_preferred)
        if _enforce_time_coherence:
            clips = _check_narrative_coherence(clips, log_fn)
        clips = [(ct, _apply_asr_corrections(text, log_fn), s, e, sc, d, focus)
                 for ct, text, s, e, sc, d, focus in (c[:7] if len(c) > 6 else (*c, "") for c in clips)]
        clips = _filter_host_interaction(clips, log_fn)
        # 价格/CTA硬过滤（AI Prompt拦不住的用代码拦）
        clips = _filter_price_and_cta(clips, log_fn)
        clips = _filter_context_damaged_clips(clips, cleaned_srt, log_fn)
        # 语义重复过滤(代码层兜底)
        if not multi_version: clips = _filter_semantic_repeat(clips, log_fn)
        clips = _filter_hook_product_repeats(clips, log_fn)
        # 明星名字过滤
        clips = _filter_celebrity(clips, log_fn)
        # CTA误判校验
        clips = _validate_cta(clips, log_fn)
        # 先去重(在边界修复前)
        clips = _dedup_clip_text_overlap(clips, log_fn, merge_mode=merge_mode)
        if not clips:
            _log("AI: 去重后无剩余")
            return []
        # [v8.5] 时长硬顶：best_clips路径也需要截断，避免巨型Hook/Product
        clips = _cap_clip_duration(clips, log_fn, srt_text=srt_text)
        # [v9.4] Close片段语句完整性保障
        clips = ensure_sentence_complete(clips, srt_text, log_fn)
        clips = trim_repetitive_filler(clips, srt_text, log_fn)
        # Do not trim tail filler by character ratio here. It can cut in the
        # middle of natural speech; boundary/context repair below is safer.
        # [v9.4] Close完整性 → [v9.6] 全片段语句完整性
        clips = _split_long_clips(clips, _indexed_srt_entries, log_fn)
        # 重叠清理：_split_long_clips可能引入重叠
        clips = _remove_overlaps(clips, log_fn)
        clips = _fix_clip_boundaries(clips, cleaned_srt, log_fn)
        # 句边界修复之后再做词级裁剪，避免精确边界被扩回整条 SRT。
        clips = _trim_filler_start(clips, srt_text, log_fn, word_timings=word_timings)
        clips = _trim_product_size_prompt_tails(clips, cleaned_srt, log_fn)
        clips = _filter_focus_near_duplicates(clips, log_fn, target_duration=_AI_TARGET_DURATION)
        clips = _filter_hook_product_repeats(clips, log_fn)
        if _feedback_filter_enabled:
            clips = _filter_preview_feedback_rejected_clips(
                clips, _feedback_profile, log_fn,
                min_keep=_history_min_keep,
                min_duration=_history_min_duration,
            )
        # [DISABLED] 延伸已禁用
        # clips = _extend_clips(clips, log_fn, target_min=55, target_max=75, max_end=srt_max_end)
        # 兜底回收：延伸后如果仍不到50s，从去重被砍的片段中回收
        _total_dur = sum(c[5] for c in clips)
        _min_dur = max(25, _AI_TARGET_DURATION * 2 // 3)  # 兜底目标 ≈ 目标时长的2/3
        if _total_dur < _min_dur and removed_from_dedup:
            _log(f"兜底回收(best): 当前 {_total_dur:.1f}s < {_min_dur}s, 尝试回收...")
            for rc in removed_from_dedup:
                if sum(c[5] for c in clips) >= _min_dur:
                    break
                if rc[5] <= 0 or rc[5] >= 8:
                    continue
                _overlap = False
                for ec in clips:
                    if rc[2] < ec[3] and rc[3] > ec[2]:
                        _overlap = True
                        break
                if not _overlap:
                    # 插在最后一个close前面，不要加在close后面
                    last_close_idx = None
                    for ci, cc in enumerate(clips):
                        if 'close' in cc[0].lower():
                            last_close_idx = ci
                    if last_close_idx is not None:
                        clips.insert(last_close_idx, rc)
                    else:
                        clips.append(rc)
                    _log(f"  回收(best): {rc[2]:.1f}-{rc[3]:.1f}s ({rc[5]:.1f}s)")
        # 如果还是不够目标段数，用关键词补充；不要退回老的4段兜底。
        if len(clips) < _target_min_clips:
            clips = _supplement_clips(clips, cleaned_srt, log_fn, min_total=_target_min_clips)
        clips = _filter_recent_similar_clips(
            clips, _recent_history, log_fn,
            min_keep=_history_min_keep,
            min_duration=_history_min_duration,
        )
        if _coverage_candidate_pool:
            clips, _best_coverage_replaced = _replace_for_topic_coverage(
                clips,
                _coverage_candidate_pool,
                _current_focus_used_label(),
                _AI_TARGET_DURATION,
                _log,
            )
            if _best_coverage_replaced:
                clips = _reorder_product_focus_blocks(
                    clips,
                    _log,
                    preferred_cat=_cross_cat_preferred,
                    preferred_focus=_current_focus_used_label(),
                    ai_controls=ai_controls,
                    merge_mode=merge_mode,
                )
        clips = _enforce_target_duration_limit(clips, _AI_TARGET_DURATION, log_fn, feedback_profile=_feedback_active_profile)
        clips = _filter_price_and_cta(clips, log_fn)
        clips = _filter_context_damaged_clips(clips, cleaned_srt, log_fn)
        _set_last_topic_coverage_summary(_topic_coverage_summary(
            clips,
            _current_focus_used_label(),
            _AI_TARGET_DURATION,
        ))
        _record_history_if_needed(clips)
        return clips

    # 宽松修复
    relaxed = _relax_clips(clips if clips else [], log_fn)
    if relaxed and len(relaxed) < _target_min_clips:
        relaxed = _supplement_clips(relaxed, cleaned_srt, log_fn, min_total=_target_min_clips)
    if relaxed:
        if _feedback_filter_enabled:
            relaxed = _filter_preview_feedback_rejected_clips(
                relaxed, _feedback_profile, log_fn,
                min_keep=_history_min_keep,
                min_duration=_history_min_duration,
            )
        relaxed = _filter_recent_similar_clips(
            relaxed, _recent_history, log_fn,
            min_keep=_history_min_keep,
            min_duration=_history_min_duration,
        )
        if _coverage_candidate_pool:
            relaxed, _ = _replace_for_topic_coverage(
                relaxed,
                _coverage_candidate_pool,
                _current_focus_used_label(),
                _AI_TARGET_DURATION,
                _log,
            )
        relaxed = _enforce_target_duration_limit(relaxed, _AI_TARGET_DURATION, log_fn, feedback_profile=_feedback_active_profile)
        relaxed = _filter_price_and_cta(relaxed, log_fn)
        relaxed = _filter_context_damaged_clips(relaxed, cleaned_srt, log_fn)
        _set_last_topic_coverage_summary(_topic_coverage_summary(
            relaxed,
            _current_focus_used_label(),
            _AI_TARGET_DURATION,
        ))
        _record_history_if_needed(relaxed)
    return relaxed if relaxed else []



def _hook_preference_keywords(focus_hint=None, ai_controls=None):
    controls = _normalize_ai_controls(ai_controls)
    focus_label = _normalize_focus_label(focus_hint)
    text = f"{focus_label or focus_hint or ''} {controls.get('hook_style') or ''} {controls.get('goal') or ''}"
    keywords = []
    pref_map = {
        "版型显瘦": ["显瘦", "遮肉", "版型", "修饰", "不挑", "宽松", "修身", "微胖", "梨形", "胯宽"],
        "颜色氛围": ["颜色", "显白", "提气色", "色系", "色调", "黄皮", "气色", "焦糖", "温柔"],
        "场景搭配": ["通勤", "约会", "日常", "防晒", "穿搭", "搭配", "上班", "旅游", "场景"],
        "性价比": ["划算", "超值", "品质", "对比", "值得", "平替", "大牌", "质感"],
        "情绪感染": ["绝了", "太好看", "太漂亮", "惊艳", "漂亮", "美爆", "不敢信", "封神"],
        "流行趋势": ["流行", "设计感", "当季", "趋势", "风格", "松弛", "千金"],
        "面料质感": ["面料", "材质", "手感", "质感", "亲肤", "透气", "垂感", "桑蚕丝", "针织"],
        "口感食欲": ["好吃", "脆甜", "爆汁", "多汁", "入口", "口感", "鲜嫩", "软糯", "酥脆", "Q弹", "拉丝", "试吃", "咬一口"],
        "新鲜品质": ["新鲜", "鲜活", "现摘", "现采", "当天发", "鲜度", "品质", "饱满", "坏果包赔", "源头", "基地", "果园"],
        "产地溯源": ["产地", "原产地", "源头", "基地", "果园", "农场", "渔港", "直采", "溯源", "产区", "应季"],
        "规格分量": ["规格", "净含量", "净重", "重量", "斤装", "箱装", "大果", "果径", "个头", "分量"],
        "发货保鲜": ["发货", "现发", "冷链", "冰袋", "保温箱", "保鲜", "锁鲜", "冷冻", "速冻", "坏果包赔"],
        "场景吃法": ["早餐", "夜宵", "下午茶", "办公室", "孩子", "全家", "聚餐", "火锅", "煲汤", "下饭", "即食", "囤货"],
    }
    if focus_label in _DEFAULT_PREFERENCE_KEYWORDS:
        keywords.extend(_DEFAULT_PREFERENCE_KEYWORDS.get(focus_label) or [])
    if focus_label in _TOPIC_EVIDENCE_KEYWORDS:
        keywords.extend(_TOPIC_EVIDENCE_KEYWORDS.get(focus_label) or [])
    for pref, kws in pref_map.items():
        if pref in text or pref[:2] in text:
            keywords.extend(kws)
    hook_style = controls.get("hook_style")
    if hook_style == "痛点开头":
        keywords.extend(["痛", "显瘦", "遮肉", "微胖", "梨形", "肩宽", "胯宽", "腿粗"])
    elif hook_style == "上身效果开头":
        keywords.extend(["上身", "效果", "显瘦", "显白", "好看", "气质"])
    elif hook_style == "爆点金句开头":
        keywords.extend(["绝了", "太漂亮", "不敢信", "封神", "惊艳", "美爆"])
    elif hook_style == "主播强推荐开头":
        keywords.extend(["推荐", "闭眼入", "盲拍", "真的", "必须", "我跟你讲"])
    elif hook_style == "试吃反应开头":
        keywords.extend(["好吃", "试吃", "咬一口", "爆汁", "脆甜", "鲜嫩", "软糯", "Q弹"])
    elif hook_style == "细节近景开头":
        keywords.extend(["切开", "掰开", "开箱", "开袋", "个头", "果径", "拉丝", "爆汁", "饱满"])
    elif hook_style == "产地品质开头":
        keywords.extend(["产地", "源头", "现摘", "现采", "现发", "冷链", "鲜活", "新鲜", "果园", "基地"])
    for item in controls.get("selling_points") or []:
        if item:
            keywords.append(str(item))
    return list(dict.fromkeys(k for k in keywords if k))


def _has_explicit_hook_preference(focus_hint=None, ai_controls=None):
    controls = _normalize_ai_controls(ai_controls)
    focus = str(_normalize_focus_label(focus_hint) or focus_hint or "").strip()
    if focus and focus not in ("自动", "auto", "默认", "无"):
        return True
    return any(controls.get(key) for key in ("goal", "hook_style", "selling_points"))


def _hook_source_markers(text):
    return {str(m).upper() for m in re.findall(r"\[v\d+\]", str(text or ""), flags=re.I)}


def _strip_hook_source_markers(text):
    return re.sub(r"\[v\d+\]\s*", "", str(text or ""), flags=re.I)


def _hook_has_mixed_sources(text):
    return len(_hook_source_markers(text)) > 1


def _hook_has_numeric_noise(text):
    txt = str(text or "")
    if re.search(r"\d+\s*(?:加|\+)\s*\d+", txt):
        return True
    if re.search(r"[一二三四五六七八九十百千万零两]{2,}\s*(?:加|\+)\s*[一二三四五六七八九十百千万零两\d]+", txt):
        return True
    return False


def _hook_has_interaction_noise(text):
    txt = str(text or "")
    noise_words = (
        "感谢", "谢谢", "反馈", "欢迎", "关注", "点赞", "评论", "弹幕",
        "直播间", "客服", "私信", "稍等", "等一下", "看评论", "回放",
    )
    return any(word in txt for word in noise_words)


def _is_bad_hook_candidate_text(text):
    raw = str(text or "")
    if _hook_has_mixed_sources(raw):
        return True
    txt = re.sub(r"\s+", "", _strip_hook_source_markers(raw)).strip("，。！？!?、 ")
    if not txt or len(txt) < 4:
        return True
    if _hook_has_interaction_noise(txt) or _hook_has_numeric_noise(txt):
        return True
    hook_risk_words = (
        "价格", "多少钱", "链接", "小黄车", "购物车", "上车", "下单", "拍下",
        "领券", "券后", "运费险", "包邮", "福利", "优惠", "折扣", "库存",
        "手慢无", "秒空", "抢疯", "断码", "限时", "快没了",
    )
    if any(word in txt for word in hook_risk_words):
        return True
    weak_starts = (
        "然后", "但是", "不过", "而且", "所以", "对吧然后", "是的然后", "那谁",
        "是的", "对的", "好的", "好吧", "好", "来", "看一下", "就是说",
    )
    if txt.startswith(weak_starts):
        return True
    weak_ends = (
        "都", "就", "还", "也", "和", "跟", "把", "被", "会", "可以",
        "如果", "因为", "然后", "但是", "不过", "就是", "这个", "这款",
        "你看", "对吧", "的话", "是", "又显", "想要", "裤是", "衣服是",
        "裙子是", "它是", "这个是", "啊", "呀", "呢", "嘛"
    )
    return txt.endswith(weak_ends)


def _hook_pref_score(text, focus_hint=None, ai_controls=None):
    kws = _hook_preference_keywords(focus_hint, ai_controls)
    if not kws:
        return 0
    return sum(1 for kw in kws if kw and kw in str(text or ""))


def _hook_matches_preference(text, focus_hint=None, ai_controls=None):
    kws = _hook_preference_keywords(focus_hint, ai_controls)
    if not kws:
        return True
    hits = _hook_pref_score(text, focus_hint, ai_controls)
    if hits <= 0:
        return False
    focus = str(focus_hint or "")
    controls = _normalize_ai_controls(ai_controls)
    return hits >= 1


def _parse_srt_entries_for_hook(srt_text):
    entries = []
    for block in str(srt_text or "").strip().split(chr(10) + chr(10)):
        lines = block.strip().split(chr(10))
        time_line_idx = 0 if len(lines) >= 1 and "-->" in lines[0] else (1 if len(lines) >= 2 and "-->" in lines[1] else -1)
        if time_line_idx < 0:
            continue
        try:
            parts = lines[time_line_idx].split("-->")
            h1, m1, s1 = parts[0].strip().replace(",", ".").split(":")
            h2, m2, s2 = parts[1].strip().replace(",", ".").split(":")
            start = int(h1) * 3600 + int(m1) * 60 + float(s1)
            end = int(h2) * 3600 + int(m2) * 60 + float(s2)
            text = " ".join(lines[time_line_idx + 1:]).strip()
            if text:
                entries.append((start, end, text))
        except Exception:
            continue
    return entries


def _build_ai_srt_entry_index(srt_text):
    """Single source of truth for numbered and unnumbered SRT candidate blocks."""
    return list(_parse_srt_entries_for_hook(srt_text) or [])


def _final_hook_quality_score(text, duration, hook_keywords=None, focus_hint=None, ai_controls=None, next_clip=None):
    txt = re.sub(r"\s+", "", str(text or ""))
    if not txt or _is_bad_hook_candidate_text(txt):
        return 0.0, []
    try:
        dur = float(duration)
    except Exception:
        dur = 0.0
    if dur < 0.8 or dur > 8.5:
        return 0.0, []

    hard_reject = [
        "价格", "多少钱", "链接", "小黄车", "购物车", "上车", "下单", "拍下",
        "领券", "券后", "运费险", "包邮", "正码", "正拍", "卡码", "尺码",
        "码数", "身高体重", "往大拍", "往小拍",
    ]
    if any(w in txt for w in hard_reject):
        return 0.0, []

    hook_keywords = hook_keywords or []
    controls = _normalize_ai_controls(ai_controls)
    hook_style = controls.get("hook_style")
    crowd_words = ["姐妹", "女生", "女人", "女孩", "妈妈", "宝妈", "微胖", "小个子", "梨形", "苹果型", "胯宽", "腿粗", "大骨架", "肩宽", "腰粗", "肚子", "拜拜肉", "黄皮", "你们"]
    pain_words = ["胯宽", "腿粗", "显胖", "肚子", "腰粗", "肩宽", "显壮", "肉多", "遮肉", "藏肉", "不敢穿", "穿不进去", "卡肉", "勒肉", "副乳"]
    effect_words = ["上身", "效果", "显瘦", "显高", "显白", "显腿长", "比例", "直角肩", "腰线", "拉长", "高级", "气质", "干净", "明亮", "薄", "遮肉", "藏肉"]
    strong_words = ["绝了", "太漂亮", "不敢信", "天花板", "太惊艳", "太显瘦", "巨好看", "美爆", "封神", "神仙", "救命", "闭眼入", "好牛"]
    contrast_words = ["不是", "但是", "反而", "一下子", "直接", "居然", "完全", "一点都不", "你看", "看到没有", "有没有发现"]
    recommend_words = ["推荐", "闭眼入", "盲拍", "必须", "我跟你讲", "听我的", "放心"]
    food_sensory_words = ["好吃", "脆甜", "鲜甜", "爆汁", "多汁", "汁水", "口感", "鲜嫩", "软糯", "酥脆", "Q弹", "弹牙", "拉丝", "试吃", "咬一口"]
    food_visual_words = ["切开", "掰开", "开箱", "开袋", "个头", "果径", "饱满", "满满", "一大颗", "一整箱", "看得到", "拉丝", "爆汁"]
    food_fresh_words = ["新鲜", "鲜活", "现摘", "现采", "现捕", "现捞", "当天发", "现发", "冷链", "保鲜", "锁鲜", "产地", "果园", "基地"]
    food_scene_words = ["早餐", "夜宵", "下午茶", "办公室", "孩子", "全家", "聚餐", "火锅", "煲汤", "下饭", "即食", "囤货", "冰箱"]

    weights = {
        "hook": 4.0, "strong": 5.0, "crowd": 12.0, "pain": 10.0,
        "effect": 9.0, "contrast": 6.0, "recommend": 5.0,
        "food_sensory": 12.0, "food_visual": 10.0, "food_fresh": 9.0, "food_scene": 7.0,
    }
    if hook_style == "痛点开头":
        weights.update({"crowd": 15.0, "pain": 15.0, "effect": 8.0, "strong": 4.0, "hook": 3.0})
    elif hook_style == "上身效果开头":
        weights.update({"effect": 16.0, "crowd": 8.0, "pain": 8.0, "strong": 4.0, "hook": 3.0})
    elif hook_style == "爆点金句开头":
        weights.update({"strong": 14.0, "contrast": 8.0, "hook": 8.0, "crowd": 6.0, "pain": 6.0, "effect": 6.0})
    elif hook_style == "主播强推荐开头":
        weights.update({"recommend": 14.0, "strong": 8.0, "effect": 6.0, "hook": 6.0, "crowd": 4.0})
    elif hook_style == "试吃反应开头":
        weights.update({"food_sensory": 18.0, "food_visual": 9.0, "food_fresh": 6.0, "strong": 7.0, "hook": 5.0})
    elif hook_style == "细节近景开头":
        weights.update({"food_visual": 17.0, "food_sensory": 10.0, "food_fresh": 8.0, "hook": 5.0})
    elif hook_style == "产地品质开头":
        weights.update({"food_fresh": 17.0, "food_visual": 8.0, "food_sensory": 8.0, "recommend": 6.0, "hook": 5.0})
    elif hook_style == "不强制Hook":
        weights.update({"hook": 2.0, "strong": 3.0, "crowd": 8.0, "pain": 7.0, "effect": 7.0, "recommend": 3.0})

    score = 0.0
    reasons = []

    def add_if(label, words, weight):
        nonlocal score
        if any(w in txt for w in words):
            score += weight
            reasons.append(label)

    if any(kw and kw in txt for kw in hook_keywords):
        score += weights["hook"]
        reasons.append("爆词")
    add_if("强情绪", strong_words, weights["strong"])
    add_if("人群", crowd_words, weights["crowd"])
    add_if("痛点", pain_words, weights["pain"])
    add_if("效果", effect_words, weights["effect"])
    add_if("反差", contrast_words, weights["contrast"])
    add_if("推荐", recommend_words, weights["recommend"])
    add_if("口感", food_sensory_words, weights["food_sensory"])
    add_if("近景", food_visual_words, weights["food_visual"])
    add_if("新鲜", food_fresh_words, weights["food_fresh"])
    add_if("场景", food_scene_words, weights["food_scene"])
    if any(p in str(text or "") for p in ("?", "？", "吗")):
        score += 5.0
        reasons.append("问句")

    pref_hits = _hook_pref_score(txt, focus_hint, ai_controls)
    if pref_hits:
        score += 8.0 + pref_hits * 4.0
        reasons.append("偏好")

    if next_clip:
        try:
            next_text = str(next_clip[1] if len(next_clip) > 1 else "")
            sim = _clip_text_similarity_value(txt, next_text)
            hook_block = _clip_focus_block(("hook", text, 0, dur, 0, dur, ""))
            next_block = _clip_focus_block(next_clip)
            if hook_block != "其他" and hook_block == next_block:
                score += 8.0
                reasons.append("衔接")
            elif sim >= 0.18:
                score += min(5.0, sim * 8.0)
                reasons.append("相似")
        except Exception:
            pass

    if dur <= 4.0:
        score += 5.0
    elif dur <= 6.0:
        score += 3.0
    else:
        score -= 1.0

    if len(txt) < 5:
        score -= 3.0
    concrete_reasons = {"强情绪", "痛点", "效果", "反差", "口感", "近景", "新鲜", "场景", "问句", "偏好"}
    if not any(r in reasons for r in concrete_reasons):
        return 0.0, []
    return max(0.0, score), reasons


def _refine_hook_by_dynamic_score(clips, srt_text, log_fn=None, focus_hint=None, ai_controls=None):
    """Final Hook gate: score candidates against the chosen opening style before structure fixes."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips:
        return clips

    controls = _normalize_ai_controls(ai_controls)
    hook_style = controls.get("hook_style")
    hook_keywords = load_keywords().get("hook_keywords", [])
    srt_entries = _parse_srt_entries_for_hook(srt_text)
    product_indices = [i for i, c in enumerate(clips) if not _is_hook_clip(c) and not _is_close_clip(c)]
    next_clip = clips[product_indices[0]] if product_indices else None
    current_hook_idx = next((i for i, c in enumerate(clips) if _is_hook_clip(c)), None)

    candidates = []
    seen = set()

    def add_candidate(start, end, text, kind, source_idx=None):
        try:
            start_f = float(start)
            end_f = float(end)
        except Exception:
            return
        text_s = str(text or "").strip()
        if not text_s:
            return
        dur = max(0.0, end_f - start_f)
        score, reasons = _final_hook_quality_score(text_s, dur, hook_keywords, focus_hint, ai_controls, next_clip=next_clip)
        if score <= 0:
            return
        key = (re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text_s), round(start_f, 1), round(end_f, 1))
        if key in seen:
            return
        seen.add(key)
        candidates.append((start_f, end_f, text_s, dur, score, {"kind": kind, "source_idx": source_idx, "reasons": reasons}))

    for i, clip in enumerate(clips):
        if _is_hook_clip(clip):
            add_candidate(clip[2], clip[3], clip[1], "current", i)

    selected_ranges = []
    for i, clip in enumerate(clips):
        if _is_close_clip(clip):
            continue
        try:
            cs = float(clip[2])
            ce = float(clip[3])
        except Exception:
            continue
        selected_ranges.append((i, cs, ce))
        if not _is_hook_clip(clip) and ce > cs:
            add_candidate(cs, ce, clip[1], "clip", i)

    for i, cs, ce in selected_ranges:
        for es, ee, txt in srt_entries:
            if es >= cs - 0.35 and ee <= ce + 0.35:
                add_candidate(es, ee, txt, "srt", i)

    if not candidates:
        return clips

    current_score = 0.0
    current_reasons = []
    if current_hook_idx is not None:
        current = clips[current_hook_idx]
        current_score, current_reasons = _final_hook_quality_score(
            current[1], _clip_duration_value(current), hook_keywords, focus_hint, ai_controls, next_clip=next_clip
        )

    picked = _pick_diverse_hook_candidate(candidates, _log)
    if not picked:
        return clips
    best_start, best_end, best_text, best_dur, best_score, meta = picked
    best_reasons = meta.get("reasons", []) if isinstance(meta, dict) else []
    min_score = 24.0 if hook_style == "不强制Hook" else 20.0
    margin = 12.0 if hook_style == "不强制Hook" else 8.0
    same_current = (
        current_hook_idx is not None
        and isinstance(meta, dict)
        and meta.get("kind") == "current"
        and meta.get("source_idx") == current_hook_idx
    )

    if same_current and current_score >= min_score:
        return clips
    if current_hook_idx is not None and current_score >= min_score and best_score < current_score + margin:
        return clips
    if best_score < min_score:
        return clips

    rebuilt = []
    for clip in clips:
        rebuilt.append(_retag_clip_type(clip, "product") if _is_hook_clip(clip) else clip)
    new_hook = ("hook", best_text, best_start, best_end, 9.0, best_dur, "Hook评分")
    rebuilt.insert(0, new_hook)
    _log(
        "Hook评分: "
        f"当前{current_score:.1f}({','.join(current_reasons[:3]) or '无'}) → "
        f"候选{best_score:.1f}({','.join(best_reasons[:3]) or '无'})，替换为 '{best_text[:22]}'"
    )
    return rebuilt


def _pick_diverse_hook_candidate(candidates, log_fn=None):
    """Pick from top hook candidates instead of always taking the first maximum."""
    if not candidates:
        return None
    import random as _random

    candidates = sorted(candidates, key=lambda x: x[4], reverse=True)
    best_score = float(candidates[0][4])
    cutoff = max(best_score - 4.0, best_score * 0.86)
    top = [c for c in candidates if float(c[4]) >= cutoff][:4]
    chosen = _random.choice(top) if len(top) > 1 else top[0]
    if log_fn and len(top) > 1:
        log_fn(f"Hook多样化: 从{len(top)}个高分候选中选择 '{chosen[2][:18]}'")
    return chosen


def _force_short_hook(clips, srt_text, log_fn=None, max_hook_sec=5.0, focus_hint=None, ai_controls=None):
    """Hook exceeds the configured cap: replace it with a short SRT hook candidate.

    max_hook_sec=None means the user selected "不限", so this pass is disabled.

    Hook > 5秒时，从SRT短条目中找1-4秒爆点词替换原Hook。
    原Hook降为Product，新Hook用SRT精确时间戳，保证1-3秒。
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    if max_hook_sec is None:
        _log("短Hook检测: Hook上限=不限，跳过时长替换")
        return clips

    MAX_HOOK_SEC = float(max_hook_sec)

    if not clips or not srt_text or MAX_HOOK_SEC <= 0:
        return clips

    # 找现有Hook
    hook_idx = None
    for i, clip in enumerate(clips):
        if clip[0] == "hook":
            hook_idx = i
            break

    if hook_idx is None:
        return clips  # 没有Hook，不管

    hook_clip = clips[hook_idx]
    hook_dur = float(hook_clip[5]) if isinstance(hook_clip[5], (int, float, str)) else 0
    _pref_kws = _hook_preference_keywords(focus_hint, ai_controls)
    _explicit_pref = _has_explicit_hook_preference(focus_hint, ai_controls)
    _hook_matches_pref = _hook_matches_preference(hook_clip[1], focus_hint, ai_controls)

    if hook_dur <= MAX_HOOK_SEC and _hook_matches_pref:
        _log(f"短Hook检测: Hook {hook_dur:.1f}s ≤ {MAX_HOOK_SEC}s，无需替换")
        return clips  # 已经够短
    if hook_dur <= MAX_HOOK_SEC and not _hook_matches_pref:
        _log(f"短Hook检测: Hook虽短但不匹配偏好'{str(focus_hint)[:8]}'，尝试找差异化Hook...")

    scan_reason = "偏好Hook" if _explicit_pref else "爆点词"
    _log(f"短Hook检测: Hook {hook_dur:.1f}s > {MAX_HOOK_SEC}s或不匹配偏好，扫描SRT找{scan_reason}...")

    # 加载爆点词库
    _kw_data = load_keywords()
    _hook_kw = _kw_data.get("hook_keywords", [])
    # 强爆点词（权重更高）
    _strong_kw = ["美爆了", "绝了", "太漂亮", "不敢信", "封神", "神仙",
                  "超级超级", "太显瘦", "卖疯了", "天花板", "炸了", "爆了",
                  "太惊艳", "盲拍", "闭眼入", "太绝了", "特别特别", "真的真的"]
    # CTA词（降权）
    _cta_words = ["拍", "买", "抢", "冲", "下单", "入手", "拍下", "赶紧"]
    # 圈人群词（锁定目标用户）
    _crowd_kw = [
        "姐妹", "妈妈", "女生", "女人", "女孩", "小姐姐", "宝妈",
        "微胖", "腿粗", "小个子", "梨形", "胯宽", "大骨架", "肩宽",
        "拜拜肉", "胳膊粗", "肚子大", "腰粗", "胯大",
        "你", "你们", "大家",
    ]

    # 解析SRT条目
    _srt_entries = []
    for block in srt_text.strip().split(chr(10) + chr(10)):
        bl = block.strip().split(chr(10))
        time_line_idx = 0 if len(bl) >= 1 and '-->' in bl[0] else (1 if len(bl) >= 2 and '-->' in bl[1] else -1)
        if time_line_idx >= 0:
            try:
                parts = bl[time_line_idx].split('-->')
                h1, m1, s1 = parts[0].strip().replace(',', '.').split(':')
                h2, m2, s2 = parts[1].strip().replace(',', '.').split(':')
                es = int(h1)*3600 + int(m1)*60 + float(s1)
                ee = int(h2)*3600 + int(m2)*60 + float(s2)
                txt = ' '.join(bl[time_line_idx + 1:]).strip()
                _srt_entries.append((es, ee, txt))
            except Exception:
                pass

    if not _srt_entries:
        _log("短Hook检测: 无SRT条目可扫描")
        return clips

    # 收集已占用的时间范围（排除原Hook）
    _used_ranges = []
    for i, clip in enumerate(clips):
        if i != hook_idx:
            _used_ranges.append((float(clip[2]), float(clip[3])))

    # 扫描SRT条目，找含爆点词或偏好词的短条目(1-4秒)
    candidates = []  # (start, end, text, duration, score)
    for es, ee, txt in _srt_entries:
        dur = ee - es
        if dur < 1.2 or dur > 4.0:
            continue  # 太短(<1.2s)或太长(>4s)都不要
        if _is_bad_hook_candidate_text(txt):
            continue

        # 检查爆点词
        best_match_score = 0
        pref_hits = _hook_pref_score(txt, focus_hint, ai_controls)
        if _explicit_pref and pref_hits:
            best_match_score = 18 + pref_hits * 10 + (4.0 - dur) * 2
            if any(ck in txt for ck in _crowd_kw):
                best_match_score += 6
        for kw in _hook_kw:
            if kw in txt:
                # CTA词降权
                is_cta = any(cw in txt for cw in _cta_words)
                if is_cta:
                    score = 5
                    if best_match_score > score:
                        continue  # CTA已是最低分，不覆盖更好的
                else:
                    # 圈人群检测
                    is_crowd = any(ck in txt for ck in _crowd_kw)
                    is_strong = any(sk in kw or kw in sk for sk in _strong_kw)
                    if is_strong and is_crowd:
                        score = 30  # 爆点+圈人群 = 最优Hook
                    elif is_crowd:
                        score = 20  # 仅圈人群
                    elif is_strong:
                        score = 10  # 仅爆点词
                    else:
                        score = 8   # 普通hook词
                # 越短越好
                score += (4.0 - dur) * 2
                # 越靠前越好（优先用视频前段的爆点）
                score += max(0, 30 - es) * 0.1
                if pref_hits:
                    score += 24 + pref_hits * 8
                if score > best_match_score:
                    best_match_score = score
                break  # 一个词匹配就够了

        if best_match_score > 0:
            # 检查是否和已有片段重叠（允许和原Hook重叠，因为我们要替换它）
            overlaps = False
            for us, ue in _used_ranges:
                if es < ue and ee > us:
                    overlaps = True
                    break
            if not overlaps:
                candidates.append((es, ee, txt, dur, best_match_score))

    if not candidates:
        _log("短Hook检测: 未找到不重叠的短爆点条目")
        return clips

    if _explicit_pref and not _hook_matches_pref:
        pref_candidates = [c for c in candidates if _hook_pref_score(c[2], focus_hint, ai_controls) > 0]
        old_text = str(hook_clip[1] if len(hook_clip) > 1 else "")
        pref_candidates = [c for c in pref_candidates if str(c[2]) != old_text]
        if pref_candidates:
            candidates = pref_candidates
            _log(f"短Hook检测: 按偏好过滤候选 {len(pref_candidates)} 个")
        else:
            _log("短Hook检测: 未找到匹配偏好的替代Hook，保留原Hook")
            return clips

    best = _pick_diverse_hook_candidate(candidates, _log)
    new_start, new_end, new_txt, new_dur, _ = best
    current_score, _current_reasons = _final_hook_quality_score(
        hook_clip[1], hook_dur, _hook_kw, focus_hint, ai_controls
    )
    new_score, new_reasons = _final_hook_quality_score(
        new_txt, new_dur, _hook_kw, focus_hint, ai_controls
    )
    if new_score < 20.0:
        _log(f"短Hook检测: 候选未过质量门槛({new_score:.1f})，保留原Hook")
        return clips
    if current_score >= 20.0 and new_score < current_score + 6.0:
        _log(
            "短Hook检测: 原Hook质量仍可用，"
            f"候选{new_score:.1f}({','.join(new_reasons[:3]) or '无'})未明显更好，保留原Hook"
        )
        return clips

    # 原Hook降为Product
    old = clips[hook_idx]
    clips[hook_idx] = ("product", old[1], old[2], old[3], old[4], old[5],
                        old[6] if len(old) > 6 else "")
    _log(f"短Hook检测: 原Hook '{old[1][:25]}...' ({hook_dur:.1f}s) → Product")

    # 新Hook插到最前面
    new_hook = ("hook", new_txt, new_start, new_end, 9.0, new_dur, "爆点裁切")
    clips.insert(0, new_hook)
    _log(f"短Hook检测: 替换为 '{new_txt}' ({new_dur:.1f}s)")

    return clips


def _call_ai(api_key, base_url, model, srt_text, log_fn, focus_hint=None, srt_entries=None, hook_candidates_hint=None, multi_version=False, return_raw=False, num_versions=3, ai_controls=None, recent_history_hint=None, extra_instruction=None, main_category=None, duration_contract=None, merge_mode=False, required_sources=None, allowed_candidate_ids=None, content_review_hint=None, review_hook_pairs=None, review_topic_support=None):
    def _log(msg):
        if log_fn: log_fn(msg)
    main_category = _normalize_forced_category(main_category) or main_category
    _review_allowed_candidate_ids = (
        {int(index) for index in allowed_candidate_ids}
        if allowed_candidate_ids is not None else None
    )

    def _resolve_focus_used_label(label="", detail=""):
        text = str(label or "").strip()
        if text:
            normalized = _normalize_focus_label(text)
            if normalized in _DEFAULT_PREFERENCE_KEYWORDS:
                return normalized
        haystack = f"{label or ''} {detail or ''}"
        best_label = ""
        best_score = 0
        for pref_label, words in _DEFAULT_PREFERENCE_KEYWORDS.items():
            score = 0
            if pref_label in haystack:
                score += 5
            for word in words:
                if word and word in haystack:
                    score += 1
            if score > best_score:
                best_label = pref_label
                best_score = score
        return best_label or text

    def _remember_focus_summary(mode, label, detail="", requested=None, score=None, matched_label=None, switched_from=None, error=None):
        used_label = _resolve_focus_used_label(label, detail)
        summary = {
            "mode": str(mode or "").strip(),
            "label": used_label,
            "used_label": used_label,
            "detail": str(detail or "").strip(),
            "requested": str(requested or "自动").strip(),
        }
        if score is not None:
            summary["score"] = score
        if matched_label:
            summary["matched_label"] = str(matched_label).strip()
        if switched_from:
            summary["switched_from"] = str(switched_from).strip()
        if error:
            summary["error"] = str(error).strip()
        _set_last_focus_summary(summary)

    focus_hint = _normalize_focus_label(focus_hint)
    _remember_focus_summary("待定", focus_hint or "自动", requested=focus_hint or "自动")

    # 多版本模式：设置全局标志，让Prompt构建走多版本路径
    global _skip_focus, _AI_CLIP_COUNT, _detail_kw_prompt
    _orig_skip = _skip_focus
    if multi_version:
        _skip_focus = True

    transcript = srt_text.strip()
    if len(transcript) > 30000:
        transcript = transcript[-30000:]
    
    # ★生成编号SRT条目（供AI按索引选片）★
    indexed_transcript = transcript  # fallback: 原始格式
    _srt_entry_map = {}  # {1: (start, end, text), 2: ...}
    _forbidden_indices = set()  # 含违禁词的条目索引，AI选片时跳过
    _focus_score_entry_texts = []  # 只用安全、可入片条目计算自动偏好，避免偏好被价格/CTA脏命中带偏
    if srt_entries:
        _fw_data = load_keywords()
        _fw_words = _fw_data.get("forbidden_phrases", [])
        # 价格/CTA正则模式（与_filter_price_and_cta保持一致）
        _price_patterns = [
            re.compile(r'\d{2,4}\s*[元块]'),
            re.compile(r'[到拿]手[价]?\s*\d'),
            re.compile(r'\d{2,4}\s*[多几]?[块元]'),
            re.compile(r'(?:只要|才|仅)[一两三四五六七八九十百千万\d]+[块元]'),
            re.compile(r'原价|秒杀价|福利价|破价|到手价'),
            re.compile(r'[一两三四五六七八九十百千万\d]+[多来几]?[块元]'),
            re.compile(r'[一二三四五六七八九十]\s*折'),
            re.compile(r'半价|对折'),
            re.compile(r'\d+\s*折'),
            re.compile(r'[到拿]手价?\s*[一两三四五六七八九十百千万\d]+'),
            re.compile(r'满减|领券|优惠券|消费券|凑单'),
            re.compile(r'321|三二一|价格|拍.*链接|链接.*拍|连结|連結|去拍|赶紧拍|刷新拍|往[大小]拍|上链接|上连结|上連結'),
        ]
        _focus_score_blocker_patterns = list(_price_patterns) + [
            re.compile(r'链接|连结|連結|小黄车|购物车|加购|下单|拍下|去拍|早拍|入手|点关注|关注一下|满减|领券|优惠券'),
        ]
        _indexed_lines = []
        _forbidden_count = 0
        _focus_score_excluded_count = 0
        for i, (es, ee, et) in enumerate(srt_entries, 1):
            _srt_entry_map[i] = (es, ee, et)
            _is_review_allowed = (
                _review_allowed_candidate_ids is None or i in _review_allowed_candidate_ids
            )
            _entry_duration = max(0.0, float(ee) - float(es))
            # 违禁词预扫描：标记含违禁词的条目
            _et_variants = _safety_text_variants(et)
            _matched_fw = []
            if _fw_words:
                for w in _fw_words:
                    _w = str(w or "").strip()
                    if _w and _safety_word_matches(_w, _et_variants):
                        _matched_fw.append(_w)
            # 价格/CTA预扫描：标记含价格模式的条目（跟_filter_price_and_cta同规则）
            _matched_price = _safety_pattern_matches(_price_patterns, _et_variants)
            _matched_content = _content_safety_pattern_matches(et)
            _matched_backstage = _is_backstage_instruction(et)
            if _matched_fw or _matched_price or _matched_content or _matched_backstage:
                _forbidden_indices.add(i)
                _forbidden_count += 1
            _blocked_for_focus_score = bool(
                _matched_fw
                or _matched_price
                or _matched_content
                or _matched_backstage
                or _safety_pattern_matches(_focus_score_blocker_patterns, _et_variants)
            )
            if _blocked_for_focus_score:
                _focus_score_excluded_count += 1
            elif _is_review_allowed:
                _focus_score_entry_texts.append(et)
            if _matched_fw or _matched_price or _matched_content or _matched_backstage:
                if _is_review_allowed:
                    _indexed_lines.append(f"[#{i:02d} | {_entry_duration:.1f}s] [不可选：含违禁词、价格/CTA或直播操作]")
            elif _is_review_allowed:
                _indexed_lines.append(f"[#{i:02d} | {_entry_duration:.1f}s] {et}")
        if merge_mode:
            _interleaved_lines = _director_interleave_prompt_lines(
                _indexed_lines, srt_entries, chunk_size=10
            )
            if _interleaved_lines != _indexed_lines:
                _indexed_lines = _interleaved_lines
                _log("AI: 混剪候选按来源每10条交错展示，候选编号和时间戳保持不变")
        indexed_transcript = chr(10).join(_indexed_lines)
        # 追加用户自定义细节关键词（注入到AI prompt末尾，高优先级关注）
        if _detail_kw_prompt:
            indexed_transcript += "\n" + _detail_kw_prompt
        _log(f"AI: 编号SRT条目 {len(_srt_entry_map)} 条")
        if _forbidden_count:
            _log(f"AI: 预扫描: {len(_srt_entry_map) - _forbidden_count} 条可选, {_forbidden_count} 条含违禁词/价格已标记")
        if _focus_score_excluded_count:
            _log(f"AI: 偏好评分: 已排除 {_focus_score_excluded_count} 条价格/CTA/违禁词条目")

    # ★预扫描Hook候选：短爆词 + 人群痛点 + 效果前置综合打分★
    _kw_data = load_keywords()
    _hook_kw = _kw_data["hook_keywords"]
    _entries_for_hook = [
        (_idx, es, ee, et)
        for _idx, (es, ee, et) in sorted(_srt_entry_map.items())
        if _idx not in _forbidden_indices
        and (_review_allowed_candidate_ids is None or _idx in _review_allowed_candidate_ids)
    ]
    _manual_focus_label = _normalize_focus_label(focus_hint)
    if str(_manual_focus_label or "").strip().lower() in {"", "自动", "auto", "默认", "无"}:
        _manual_focus_label = ""
    _hook_candidates, _hook_candidate_total = _collect_hook_candidates_from_entries(
        _entries_for_hook,
        hook_keywords=_hook_kw,
        focus_hint=focus_hint,
        ai_controls=ai_controls,
        limit=48,
    )
    _preference_hook_candidates = [
        candidate for candidate in _hook_candidates
        if _manual_focus_label and _hook_matches_preference(candidate[1], _manual_focus_label, None)
    ]
    _analysis_metadata_context()["hook_candidate_summary"] = {
        "requested_focus": _manual_focus_label,
        "candidate_count": int(_hook_candidate_total),
        "preference_candidate_count": len(_preference_hook_candidates),
        "preference_hook_required": bool(_manual_focus_label and _preference_hook_candidates and not _skip_focus),
        "allowed_hook_indices": [],
    }
    _hook_hint = ""
    _allowed_hook_indices = set()
    if review_hook_pairs:
        _valid_review_pairs = []
        for _pair in review_hook_pairs:
            try:
                _hook_id = int(_pair.get("hook_id") or 0)
                _followup_id = int(_pair.get("followup_id") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if (
                _hook_id in _srt_entry_map
                and _followup_id in _srt_entry_map
                and (_review_allowed_candidate_ids is None or (
                    _hook_id in _review_allowed_candidate_ids
                    and _followup_id in _review_allowed_candidate_ids
                ))
            ):
                _valid_review_pairs.append((_hook_id, _followup_id, _pair))
        _preferred_review_pairs = []
        if _manual_focus_label and not _skip_focus:
            for _item in _valid_review_pairs:
                _hook_id, _followup_id, _pair = _item
                _pair_text = " ".join((
                    str(_pair.get("topic") or ""),
                    str(_srt_entry_map[_hook_id][2]),
                    str(_srt_entry_map[_followup_id][2]),
                ))
                if _hook_matches_preference(_pair_text, _manual_focus_label, None):
                    _preferred_review_pairs.append(_item)
        if _preferred_review_pairs:
            _valid_review_pairs = _preferred_review_pairs
        _allowed_hook_indices = {item[0] for item in _valid_review_pairs}
        _analysis_metadata_context()["hook_candidate_summary"].update({
            "preference_hook_required": bool(_preferred_review_pairs),
            "preference_review_pair_count": len(_preferred_review_pairs),
            "allowed_hook_indices": sorted(_allowed_hook_indices),
            "ranked_hook_indices": [item[0] for item in _valid_review_pairs],
            "review_pair_count": len(_valid_review_pairs),
        })
        _pair_lines = [
            f"#{hook_id:02d}->#{followup_id:02d}"
            f"[{str(pair.get('topic') or '高质量卖点').strip()}]"
            for hook_id, followup_id, pair in _valid_review_pairs
        ]
        _hook_hint = (
            "\n★内容审稿Hook合同★ Hook必须从以下组合的左侧编号选择，第二段必须使用该组合右侧编号，"
            "直接解释、证明或兑现开头；关键词分只负责召回，用户偏好不强迫Hook换成较弱句。"
            f"组合: {', '.join(_pair_lines)}★"
        )
        _log(f"AI: 内容审稿提供 {len(_valid_review_pairs)} 组Hook+承接组合")
    elif content_review_hint:
        # Keyword scores are recall hints only. In reviewed mode the director may
        # choose any grounded reviewed candidate as Hook after reading the full story.
        _ranked_review_hook_candidates = sorted(
            _hook_candidates,
            key=lambda candidate: (-candidate[3], candidate[0]),
        )[:12]
        _analysis_metadata_context()["hook_candidate_summary"].update({
            "preference_hook_required": False,
            "allowed_hook_indices": [],
            "ranked_hook_indices": [
                candidate[0] for candidate in _ranked_review_hook_candidates
            ],
            "reviewed_pool_unrestricted": True,
        })
        _candidate_lines = [
            f'#{candidate[0]:02d}"{candidate[1][:24]}"'
            for candidate in _ranked_review_hook_candidates
        ]
        _hook_hint = (
            "\n\u2605Hook\u5019\u9009\u53ea\u662f\u53ec\u56de\u53c2\u8003\uff0c\u4e0d\u662f\u767d\u540d\u5355\u2605 "
            "\u4f60\u53ef\u4ece\u4efb\u610f\u5ba1\u7a3f\u901a\u8fc7\u7684\u5019\u9009\u4e2d\u9009Hook\u3002"
            "\u5fc5\u987b\u5148\u68c0\u67e5Hook\u81ea\u8eab\u662f\u5426\u72ec\u7acb\u8bf4\u5b8c\u5177\u4f53\u8d2d\u4e70\u4ef7\u503c\uff0c"
            "\u518d\u68c0\u67e5\u7b2c2\u6bb5\u662f\u5426\u7acb\u5373\u89e3\u91ca\u3001\u8bc1\u660e\u6216\u5151\u73b0\u5b83\u3002"
            "\u201c\u60f3\u770bX\u5c31\u7ed9\u4f60\u770b\u4e00\u773c\u201d\u3001\u201cX\u5c31\u8fd9\u4e48\u642d\u201d\u3001\u7eaf\u5c55\u793a\u8fc7\u6e21\u4e0d\u5f97\u4f5cHook\u3002"
            "\u5dee\u5f02\u5316\u548c\u7528\u6237\u504f\u597d\u53ea\u5728\u5185\u5bb9\u8d28\u91cf\u76f8\u5f53\u65f6\u4f5c\u4e3a\u9009\u62e9\u4f9d\u636e\u3002"
            + (f"\u53ec\u56de\u53c2\u8003: {', '.join(_candidate_lines)}" if _candidate_lines else "")
        )
        _log(
            f"AI: \u5ba1\u7a3f\u6a21\u5f0fHook\u53ec\u56de {len(_ranked_review_hook_candidates)} \u4e2a\uff0c"
            "\u4e0d\u9650\u5236\u5bfc\u6f14\u53ea\u80fd\u4ece\u53ec\u56de\u5217\u8868\u9009\u62e9"
        )
    elif hook_candidates_hint:
        # 多版本模式：使用外部传入的分配候选
        _hook_hint = hook_candidates_hint
        _log(f"AI: 使用分配的Hook候选")
    elif _hook_candidates:
        _preference_hook_ids = {candidate[0] for candidate in _preference_hook_candidates}
        _general_hook_candidates = [candidate for candidate in _hook_candidates if candidate[0] not in _preference_hook_ids]
        if _preference_hook_candidates:
            _ranked_hook_candidates = (
                sorted(_preference_hook_candidates, key=lambda c: (-c[3], c[0]))[:8]
                + sorted(_general_hook_candidates, key=lambda c: (-c[3], c[0]))
            )
        else:
            _ranked_hook_candidates = sorted(_hook_candidates, key=lambda c: (-c[3], c[0]))
        _picked_hook_candidates = _ranked_hook_candidates[:12]
        if _manual_focus_label and _preference_hook_candidates and not _skip_focus:
            _allowed_hook_indices = {
                candidate[0] for candidate in _picked_hook_candidates
                if candidate[0] in _preference_hook_ids
            }
        elif not _skip_focus:
            _allowed_hook_indices = {candidate[0] for candidate in _picked_hook_candidates}
        _analysis_metadata_context()["hook_candidate_summary"]["allowed_hook_indices"] = sorted(
            _allowed_hook_indices
        )
        _analysis_metadata_context()["hook_candidate_summary"]["ranked_hook_indices"] = [
            candidate[0]
            for candidate in _picked_hook_candidates
            if candidate[0] in _allowed_hook_indices
        ]
        _cand_text = [
            f'#{idx_k:02d}[{"偏好Hook" if idx_k in _preference_hook_ids else "通用Hook"}]'
            f'"{et[:18]}"({dur:.0f}s/{",".join(reasons[:2])})'
            for idx_k, et, dur, _score, reasons in _picked_hook_candidates
        ]
        if _preference_hook_candidates and _manual_focus_label:
            _hook_hint = (
                f"\n★用户明确指定“{_manual_focus_label}”。已找到{len(_preference_hook_candidates)}个合格偏好Hook，"
                "Hook的srt_indices只能填写下列[偏好Hook]中的一个编号，禁止从全文其他编号自行挑Hook。"
                "第二段必须继续同一主题，直接解释、证明或兑现Hook，不得跳到别的卖点。"
                f"候选: {', '.join(_cand_text)}★"
            )
        else:
            _hook_hint = f"\n★Hook候选（已按爆点/人群痛点/效果前置综合打分）: {', '.join(_cand_text)}★"
        _log(
            f"AI: Hook候选池 {_hook_candidate_total} 个，提供 {len(_picked_hook_candidates)} 个"
            + (f"（偏好Hook {len(_preference_hook_candidates)} 个）" if _preference_hook_candidates else "")
        )
    else:
        _hook_hint = "\n★未找到短爆点Hook候选，请从SRT中找最有冲击力的短句作为Hook★"

    if _allowed_hook_indices:
        _contract_lines = []
        for _contract_line in indexed_transcript.splitlines():
            _contract_match = re.match(r"\[#(\d+)\b", _contract_line)
            if _contract_match and int(_contract_match.group(1)) not in _allowed_hook_indices:
                _contract_line = _contract_line.replace("] ", "] [不可作Hook] ", 1)
            _contract_lines.append(_contract_line)
        indexed_transcript = "\n".join(_contract_lines)

    _director_repair_mode = bool(
        extra_instruction and "【整体叙事修复】" in str(extra_instruction)
    )
    _source_distribution_repair_mode = bool(
        extra_instruction and "【混剪来源分布修复】" in str(extra_instruction)
    )

    # 单版本叙事优先稳定；定向修复进一步降温，避免第二次调用改写已合格骨架。
    import random
    temperature = 0.1 if (_director_repair_mode or _source_distribution_repair_mode) else (
        round(random.uniform(0.35, 0.55), 2) if _skip_focus else 0.2
    )
    _log(f"AI: temperature={temperature}")

    # 随机偏好提示(每次侧重不同角度，增加差异化)
    if focus_hint and focus_hint not in ("自动", "auto", ""):
        focus = focus_hint
        _remember_focus_summary("指定偏好", focus_hint, focus, requested=focus_hint)
        _log(f"AI: 指定偏好 → {focus}")
    elif _skip_focus:
        # 多版本模式：保留用户偏好，让方案1匹配
        focus = focus_hint if focus_hint else ""
        if focus:
            _remember_focus_summary("多版本偏好", focus, focus, requested=focus_hint or "自动")
            _log(f"AI: 多版本模式，方案1偏好 → {focus}")
        else:
            _remember_focus_summary("多版本全量", "全量选片", "不额外限定偏好", requested="自动")
            _log("AI: 多版本模式（全量选片）")
    else:
        # ★智能偏好选择：分析SRT内容，选最匹配的偏好★
        try:
            _kw_data_focus = load_keywords()
            _focus_scores = {}
            _focus_support_counts = {}
            _focus_hints_map = _kw_data_focus.get("preference_keywords", _DEFAULT_PREFERENCE_KEYWORDS)
            # 统计SRT中每种偏好的关键词命中数
            _focus_score_texts = _focus_score_entry_texts if srt_entries else [srt_text]
            # Load preference weights (default 1.0)
            _weights = {}
            try:
                _settings_weights = load_settings().get("preference_weights", {})
                if isinstance(_settings_weights, dict):
                    _weights.update(_normalize_preference_weights(_settings_weights))
            except Exception:
                pass
            try:
                import json as _jw
                _kw_path_weights = _keyword_file_paths()[1]
                if os.path.exists(_kw_path_weights):
                    with open(_kw_path_weights, "r", encoding="utf-8") as _jwf:
                        _jwdata = _jw.load(_jwf)
                    _file_weights = _jwdata.get("preference_weights", {})
                    if isinstance(_file_weights, dict):
                        _weights.update(_normalize_preference_weights(_file_weights))
            except Exception:
                pass
            _food_focus_labels = {"口感食欲", "新鲜品质", "产地溯源", "规格分量", "发货保鲜", "场景吃法"}
            _food_mode = _is_food_fresh_category(main_category)
            _review_focus_scores = {}
            _review_focus_support_counts = {}
            for _topic, _support in (review_topic_support or {}).items():
                _focus_name = _normalize_focus_label(_topic)
                if _focus_name not in _focus_hints_map:
                    continue
                if _food_mode and _focus_name not in _food_focus_labels:
                    continue
                if not _food_mode and _focus_name in _food_focus_labels:
                    continue
                if not isinstance(_support, dict):
                    continue
                _main_support = float(_support.get("main") or 0.0)
                _reserve_support = float(_support.get("reserve") or 0.0)
                _evidence_support = float(_support.get("evidence") or 0.0)
                _support_count = int(_main_support + _reserve_support)
                if _support_count <= 0:
                    continue
                _weight = _weights.get(_focus_name, 1.0)
                _review_focus_scores[_focus_name] = (
                    _main_support * 3.0 + _reserve_support + _evidence_support * 0.5
                ) * _weight
                _review_focus_support_counts[_focus_name] = _support_count
            for _fname, _fkws in _focus_hints_map.items():
                _fname = _normalize_focus_label(_fname)
                if _food_mode and _fname not in _food_focus_labels:
                    continue
                if not _food_mode and _fname in _food_focus_labels:
                    continue
                _hit_keywords = set()
                _support_count = 0
                for _entry_text in _focus_score_texts:
                    _entry_hits = [str(_kw or "").strip() for _kw in _fkws if str(_kw or "").strip() and str(_kw or "").strip() in _entry_text]
                    if _entry_hits:
                        _support_count += 1
                        _hit_keywords.update(_entry_hits)
                _score = len(_hit_keywords)
                if _score > 0:
                    _weight = _weights.get(_fname, 1.0)
                    _focus_scores[_fname] = _focus_scores.get(_fname, 0) + _score * _weight
                    _focus_support_counts[_fname] = _focus_support_counts.get(_fname, 0) + _support_count
            
            if _review_focus_scores:
                _focus_scores = _review_focus_scores
                _focus_support_counts = _review_focus_support_counts
                _log("AI: 自动偏好改用内容审稿的高质量主题支持量")
            if _focus_scores:
                _robust_focus_scores = {
                    key: value
                    for key, value in _focus_scores.items()
                    if _preference_quota_supported(key)
                    and float(value) >= 3.0
                    and int(_focus_support_counts.get(key, 0) or 0) >= 3
                }
                if _robust_focus_scores:
                    _removed_weak_focuses = sorted(set(_focus_scores) - set(_robust_focus_scores))
                    if _removed_weak_focuses:
                        _log(
                            "AI: 自动偏好忽略候选不足主题: "
                            + ",".join(
                                f"{name}({_focus_support_counts.get(name, 0)}条)"
                                for name in _removed_weak_focuses
                            )
                        )
                    _focus_scores = _robust_focus_scores
                else:
                    _block_support_counts = {}
                    for _entry_text in _focus_score_texts:
                        _block = _clip_focus_block(("product", _entry_text, 0, 0, 0, 0, ""))
                        if _food_mode and _block not in _food_focus_labels:
                            continue
                        if not _food_mode and _block in _food_focus_labels:
                            continue
                        if _preference_quota_supported(_block):
                            _block_support_counts[_block] = _block_support_counts.get(_block, 0) + 1
                    if _block_support_counts:
                        _fallback_focus = max(_block_support_counts, key=_block_support_counts.get)
                        _fallback_support = _block_support_counts[_fallback_focus]
                        _focus_scores = {_fallback_focus: float(_fallback_support)}
                        _focus_support_counts[_fallback_focus] = _fallback_support
                        _log(
                            f"AI: 自动偏好改用安全字幕主主题 → {_fallback_focus}"
                            f"({_fallback_support}条)"
                        )
                # 自动偏好必须可复现：按得分、有效条目数、名称稳定排序。
                _best_focus = sorted(
                    _focus_scores,
                    key=lambda key: (
                        -float(_focus_scores.get(key, 0)),
                        -int(_focus_support_counts.get(key, 0) or 0),
                        str(key),
                    ),
                )[0]
                _best_score = _focus_scores[_best_focus]
                # 从all_angle_hints找对应的完整提示
                _focus_hint_map_full = {
                    "版型显瘦": "侧重身材痛点，优先选显瘦,遮肉,修饰身材,收腰的片段",
                    "颜色氛围": "侧重颜色氛围，优先选显白,抬亮肤色,色彩氛围相关的片段",
                    "场景搭配": "侧重场景搭配，优先选通勤,约会,出门,搭配等场景化片段",
                    "性价比": "侧重性价比，优先选品质对比,划算,超值,大牌平替的片段",
                    "情绪感染": "侧重情绪感染力，优先选主播语气最激动,最真诚,最惊艳的片段",
                    "流行趋势": "侧重流行趋势，优先选当季流行,设计感,风格标签的片段",
                    "面料质感": "侧重面料卖点，优先选面料手感,质感,亲肤的片段",
                    "紧迫稀缺": "侧重紧迫稀缺，优先选限量,断码,手慢无,库存紧张但不含价格的片段",
                    "尺寸长度": "侧重尺寸比例，优先选裙长,长度,遮小腿,露脚踝等精准描述长度的片段",
                    "工艺细节": "侧重工艺品质，优先选工艺,成本,做工,定染等体现品质细节的片段",
                    "穿着体验": "侧重穿着感受，优先选舒适,不勒,自在,活动方便等体验类片段",
                    "对比优势": "侧重对比独特，优先选同价位买不到,独家,差异化的片段",
                    "口感食欲": "侧重口感食欲，优先选试吃反应,切开爆汁,香脆软糯,Q弹拉丝等片段",
                    "新鲜品质": "侧重新鲜品质，优先选现摘现发,鲜活饱满,坏果包赔,品质背书的片段",
                    "产地溯源": "侧重产地溯源，优先选产地,果园基地,源头直采,当季应季等片段",
                    "规格分量": "侧重规格分量，优先选净重,斤装,个头,果径,整箱开箱等片段",
                    "发货保鲜": "侧重发货保鲜，优先选冷链,冰袋,保温箱,锁鲜,售后保障等片段",
                    "场景吃法": "侧重食用场景，优先选早餐,夜宵,办公室,全家囤货,火锅煲汤等片段",
                }
                focus = _focus_hint_map_full.get(_best_focus, list(_focus_hint_map_full.values())[0])
                _remember_focus_summary(
                    "智能偏好",
                    _best_focus,
                    focus,
                    requested="自动",
                    score=_best_score,
                    matched_label=_best_focus,
                )
                _log(f"AI: 智能偏好 → {_best_focus}(命中{_best_score}次) → {focus}")
            else:
                focus = "不额外限定偏好，优先选择主品类内最完整、最具体、最安全的卖点片段"
                _remember_focus_summary("智能偏好", "全量选片", focus, requested="自动")
                _log(f"AI: 智能偏好 → 无干净偏好命中，改为全量安全卖点")
        except Exception as _e:
            # 防护：变量名拼写不一致或其他异常时，不再随机制造偏好标签。
            focus = "不额外限定偏好，优先选择主品类内最完整、最具体、最安全的卖点片段"
            _remember_focus_summary("兜底偏好", "全量选片", focus, requested="自动", error=str(_e))
            _log(f"AI: 智能偏好异常({str(_e)})，降级全量安全卖点 → {focus}")

    # [增强] 计算 SRT 时间范围，告知 AI
    _srt_times = []
    for _ln in srt_text.strip().split("\n"):
        _tm = re.match(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", _ln.strip())
        if _tm:
            _es = int(_tm.group(5))*3600 + int(_tm.group(6))*60 + int(_tm.group(7)) + int(_tm.group(8))/1000.0
            _srt_times.append(_es)
    _srt_max = max(_srt_times) if _srt_times else 60
    _srt_min = min(_srt_times) if _srt_times else 0

    _target_rule = _target_duration_rule_text(_AI_TARGET_DURATION, duration_contract)

    # 多版本模式：选更多片段，允许同卖点不同角度
    if _skip_focus:
        _min_pieces = 12
        _clip_range = "25-35"
        _dedup_rule = "★同一卖点如果主播用了不同表达方式（如'面料好'和'这个面料摸着特别软'），可以分别选取，因为多版本需要差异化素材★"
        _total_rule = f"总素材池25-35个，每个版本必须独立满足：{_target_rule}"
        _hook_rule = "★多版本选片：必须找出3-5个不同类型的Hook候选★ 不要只选1个最强Hook，而是找出圈人群型、极端表态型、痛点型、爆料型、夸奖型各1个（有则选）。不同版本需要不同的Hook开场。"
        _product_rule = "★多版本选片：选择12-18个Product片段★ 覆盖不同卖点角度（版型/面料/功能/风格/品质/对比/上身效果/搭配建议/场景种草），每个角度至少2个片段，同一角度有不同表达也要选。素材越丰富，3个版本的内容越充实。"
        _close_rule = "★多版本选片：选择3-5个Close片段★ 不同促单方式（紧迫感/闭眼入/尺码引导/信任强化/场景收尾）各选1-2个。"
    else:
        # 使用统一的目标时长 -> 片段数量规则，避免不同入口写死不同档位。
        _clip_min, _clip_max = _target_clip_count_range(_AI_TARGET_DURATION)
        _clip_range = f"{_clip_min}-{_clip_max}"
        _min_pieces = _clip_min
        _total_rule = f"通常选择{_clip_range}个片段，以叙事完整和不重复为准；{_target_rule}"
        # ★★★ 关键：同步 _AI_CLIP_COUNT，否则 prompt 替换的永远是默认值 ★★★
        _AI_CLIP_COUNT = _clip_range
        _dedup_rule = '★绝对禁止重复同一卖点★ 字幕中主播会重复讲同一个卖点(如"面料好"说了3遍)，你必须只选每个卖点的最佳版本，严禁选两段内容相似的片段'
        _hook_rule = ""
        _product_rule = ""
        _close_rule = ""
        _hook_rule = ""
        _product_rule = ""
        _close_rule = ""

    _category_overrides = _category_prompt_overrides(main_category, multi_version=bool(_skip_focus))
    if _category_overrides:
        _dedup_rule = _category_overrides.get("dedup") or _dedup_rule
        _hook_rule = _category_overrides.get("hook") or _hook_rule
        _product_rule = _category_overrides.get("product") or _product_rule
        _close_rule = _category_overrides.get("close") or _close_rule

    # 随机差异化指令（每次运行不同侧重点）
    if _is_food_fresh_category(main_category):
        _diff_vibes = [
            "★本轮选片重点：优先选试吃反应、切开爆汁、拉丝、多汁等强食欲片段★",
            "★本轮选片重点：优先选现摘现发、产地源头、新鲜品质、坏果包赔等信任片段★",
            "★本轮选片重点：优先选规格分量、个头净重、开箱展示、家庭囤货理由明确的片段★",
            "★本轮选片重点：优先选早餐夜宵、办公室、孩子全家、火锅煲汤等可代入食用场景★",
            "★本轮选片重点：优先选主播真实试吃和复购反馈，避开医疗保健功效表达★",
        ]
    else:
        _diff_vibes = [
            "★本轮选片重点：优先选主播语气最激动、情绪最饱满的片段，卖点角度越多越好★",
            "★本轮选片重点：优先选展示上身效果、穿搭场景的片段，少选纯面料描述★",
            "★本轮选片重点：优先选对比类、痛点解决类的内容，搭配推荐最佳★",
            "★本轮选片重点：优先选版型显瘦、修饰身材的内容，效果优先于参数★",
            "★本轮选片重点：优先选品质背书、细节讲解的内容，信任感优先★",
        ]
    if _manual_focus_label and not _skip_focus:
        _diff_vibe = (
            f"★本轮差异化由用户指定偏好“{_manual_focus_label}”决定，不再叠加随机选片重点。"
            "该偏好必须成为Product主线，其他主题只负责补充完整介绍，不得反客为主★"
        )
    else:
        _diff_vibe = random.choice(_diff_vibes)
    _ai_rules_prompt = _build_ai_rules_prompt(ai_controls=ai_controls, main_category=main_category)

    # 偏好权重直接进 prompt
    _pref_weights = {}
    try:
        _settings_pref = load_settings().get("preference_weights", {})
        if isinstance(_settings_pref, dict):
            _pref_weights.update(_normalize_preference_weights(_settings_pref))
    except Exception:
        pass
    if _pref_weights:
        _active_weights = {k: v for k, v in sorted(_pref_weights.items(), key=lambda x: -x[1]) if v > 0 and v != 1.0}
        if _active_weights:
            _weight_lines = "、".join(f"{k}(权重{v:.1f})" for k, v in _active_weights.items())
            _ai_rules_prompt += f"\n- 用户卖点权重偏好（数值越高越优先选）：{_weight_lines}\n"

    # 注入用户自定义细节关键词（来自keywords.json的detail_keywords字段）
    _detail_kw_prompt = ""
    try:
        import json as _dkw_json
        _kw_path_detail = _keyword_file_paths()[1]
        if os.path.exists(_kw_path_detail):
            with open(_kw_path_detail, "r", encoding="utf-8") as _dkwf:
                _dkw_data = _dkw_json.load(_dkwf)
            _detail_kws = _dkw_data.get("detail_keywords", [])
            if _detail_kws:
                _detail_kw_prompt = f"\n★用户特别关注的卖点关键词（优先选含这些词的片段）: {', '.join(_detail_kws[:30])}★\n"
    except Exception:
        pass
    _recent_history_prompt = f"\n{recent_history_hint}\n" if recent_history_hint else ""
    _extra_instruction_prompt = f"\n{extra_instruction}\n" if extra_instruction else ""
    _content_review_prompt = f"\n{content_review_hint}\n" if content_review_hint else ""
    _active_preference = _current_focus_used_label()
    _preference_quota_prompt = ""
    if not _skip_focus and _preference_quota_supported(_active_preference):
        _preference_request = get_last_analysis_metadata()["preference_summary"].get("requested", "自动")
        _preference_target_duration = (
            _analysis_metadata_context().get("final_target_duration") or _AI_TARGET_DURATION
        )
        _preference_quota, _preference_max = _preference_target_bounds(
            _preference_target_duration,
            _preference_request,
        )
        _preference_quota_prompt = (
            f"\n★偏好是主线，不是全片唯一主题★ 本轮偏好是“{_active_preference}”。Product中选择"
            f"{_preference_quota}-{_preference_max}段真正命中该偏好的完整片段；不得超过{_preference_max}段，"
            "偏好内容总时长不得超过Product总时长的55%（用户手动指定偏好时可放宽到65%）。"
            "仅在focus/reason里写偏好名称不算命中，"
            "片段原字幕必须出现对应场景、效果、材质或卖点证据。若干净候选不足，选尽所有干净候选并用其他卖点补足。"
            "全片Product至少覆盖3个独立卖点主题；除偏好主题外，其他角度每类1-2段，"
            "任何单一补充主题的片段数和总时长都不得超过用户指定偏好。"
            "成交角色不同不代表主题不同，不能把颜色内容分别标成效果/证明/场景来冒充主题覆盖。\n"
            "★直播口癖降级★ 不选以“来准备好”“就是你们会发现”“如果你想要有一点尝试的心态”开头的铺垫，"
            "不选以“呀对不对”“是不是这种感觉”收尾的互动句；除非去掉口癖后仍是一句独立且有新增信息的完整卖点。\n"
        )
    if review_hook_pairs:
        if _manual_focus_label and not _skip_focus:
            _hook_focus_rule = (
                f"★Hook必须从内容审稿组合中选择。用户指定偏好“{_manual_focus_label}”时，"
                "先比较主题相符的审稿组合；只要匹配组合完整、有具体信息且承接成立，就优先使用。"
                "仅当没有合格匹配组合时才选择最强的其他主题，不得为了偏好使用弱句★"
            )
        else:
            _hook_focus_rule = (
                "★Hook优先服从内容审稿的强开头与直接承接组合，不得为了差异化选择较弱开头★"
            )
    elif _manual_focus_label and _preference_hook_candidates and not _skip_focus:
        _hook_focus_rule = (
            f"★Hook必须体现用户指定偏好“{_manual_focus_label}”，第二段必须同主题兑现Hook；"
            "强度相近时选择偏好Hook，不得用其他主题的通用强Hook覆盖用户选择★"
        )
    else:
        _hook_focus_rule = (
            "★Hook先保证人群/痛点/效果足够强，不要为了命中偏好使用平淡开头★ "
            "第二段必须承接Hook承诺或展示直接效果"
        )
    _category_context_prompt = _food_fresh_context_prompt(main_category)
    _source_labels = sorted({
        _director_candidate_source(entry_text)
        for _entry_start, _entry_end, entry_text in (srt_entries or [])
        if _director_candidate_source(entry_text)
    })
    _source_requirements = _director_source_requirements(required_sources)
    if not _source_requirements:
        _source_requirements = {source: 1 for source in _source_labels}
    _merge_source_rule = ""
    if merge_mode and len(_source_requirements) > 1:
        _source_count = len(_source_requirements)
        _source_share_cap = 65 if _source_count == 2 else 55 if _source_count == 3 else 45
        _source_run_cap = 5 if _source_count == 2 else 4
        _merge_source_rule = (
            "\n★混剪来源合同★ 当前合格来源的最低片段配额为："
            + "、".join(
                f"{source.strip('[]')}至少{minimum}段"
                for source, minimum in sorted(_source_requirements.items())
            )
            + "。这是来源覆盖底线，不是机械轮播或平均切换；先让不同来源共同承担效果、证据、场景、顾虑解除等叙事职责，再统一编排成一条自然故事。"
            f"候选充足时，任一来源尽量不超过全片片段数的{_source_share_cap}%；同一来源连续不得超过{_source_run_cap}段，"
            "只有同一卖点必须由相邻两三句共同说完整时才可连续。每个合格来源应分布在开头、中段、结尾中的至少两个阶段，禁止把某个来源集中堆在一整段或只放到结尾凑数。"
            "来源切换应放在卖点或叙事阶段的自然边界，不得逐句机械轮换；也不得为凑配额选择残句、跨品类、价格CTA或低质量重复内容。\n"
        )
    if _is_food_fresh_category(main_category):
        _priority_line = "- 优先选受众代入强的卖点(口感食欲>新鲜品质>产地溯源>规格分量>发货保鲜>场景吃法)"
        _close_priority_line = '- 对于Close片段,优先选"复购背书""发货保鲜""囤货场景"类,避开含价格和保健功效的内容'
        _coverage_examples = "口感/新鲜/产地/规格/发货/场景"
        _example_hook_focus = "口感食欲"
        _example_hook_reason = "试吃反应强"
        _example_product_focus_1 = "新鲜品质"
        _example_product_reason_1 = "现摘现发背书"
        _example_product_focus_2 = "规格分量"
        _example_product_reason_2 = "规格展示清楚"
        _example_close_focus = "发货保鲜"
        _example_close_reason = "售后信任"
    else:
        _priority_line = "- 优先选受众群体广的卖点(显瘦>面料>颜色>场景)"
        _close_priority_line = '- 对于Close片段,优先选自然总结、选择理由或场景收束；禁止尺码拍法、价格、链接、关注和强CTA'
        _coverage_examples = "版型/面料/显瘦/穿搭/品质/场景"
        _example_hook_focus = "痛点提问"
        _example_hook_reason = "开场爆点"
        _example_product_focus_1 = "版型显瘦"
        _example_product_reason_1 = "显瘦卖点突出"
        _example_product_focus_2 = "面料触感"
        _example_product_reason_2 = "面料描述细腻"
        _example_close_focus = "信任强化"
        _example_close_reason = "推荐合辑"

    if _skip_focus:
        # 多版本模式：AI只做素材选取，不做编排
        # ★重要：Prompt结构与单版本一致（避免deepseek-v4-flash对不同格式的兼容问题）★
        user_msg = f"""以下是编号后的直播字幕条目，你需要像专业短视频编导一样，从中精选出{_clip_range}个高质量素材片段.

你的任务是从SRT中选出足够多的高质量片段，后续会由AI二次编排成不同版本.素材越丰富越多元化,最终效果越好.

{_category_context_prompt}

选片规则:
1. ★同一卖点如果主播用了不同表达方式(如"面料好"和"这个面料摸着特别软"),可以分别选取,因为多版本需要差异化素材★
2. {_dedup_rule}
3. 精选{_clip_range}个片段,{_total_rule}
4. ★绝对不选含价格/折扣的条目★
5. {_hook_rule}
6. {_product_rule}
7. {_close_rule}

{_extra_instruction_prompt}
★选片优先级★
- 优先选主播语气最激动、情绪最饱满的片段
- 优先选内容独立完整、有头有尾的片段
{_priority_line}
{_close_priority_line}

{f"★本轮选片侧重: {focus}★" if focus else ""}
{_ai_rules_prompt}
{_recent_history_prompt}

★输出格式★: 每个片段用 srt_indices 字段指定选了哪些编号条目(数组),不要填start/end时间戳.★优先选1个完整条目；如果前一句必须靠后一句承接，允许选2个连续条目，确保单片段5-10秒且语义完整★:
[
  {{"clip_type": "hook", "srt_indices": [3], "focus": "{_example_hook_focus}", "reason": "{_example_hook_reason}", "trim_priority": 0}},
  {{"clip_type": "hook", "srt_indices": [12], "focus": "{_example_product_focus_1}", "reason": "不同钩子类型", "trim_priority": 0}},
  {{"clip_type": "product", "srt_indices": [18], "focus": "{_example_product_focus_1}", "reason": "{_example_product_reason_1}", "trim_priority": 2}},
  {{"clip_type": "product", "srt_indices": [25], "focus": "{_example_product_focus_2}", "reason": "{_example_product_reason_2}", "trim_priority": 1}},
  {{"clip_type": "close", "srt_indices": [45], "focus": "{_example_close_focus}", "reason": "{_example_close_reason}", "trim_priority": 0}},
  ...
]

★clip_type: hook/product/close 三种类型; focus: 该片段的卖点角度描述; reason: 选片理由(10字内)★
{_hook_hint}

字幕条目:
{indexed_transcript}"""

    if _source_distribution_repair_mode:
        user_msg = f"""这是一次混剪来源分布的完整重编，不是在旧片单两端补素材。你仍是最终叙事负责人，必须从全部候选中重新输出一条自然、完整、可直接成片的故事。

{extra_instruction}
{_category_context_prompt}
{_merge_source_rule}
{_content_review_prompt}
本轮指定偏好：{_active_preference or focus or '全量选片'}
{_preference_quota_prompt}
{_hook_focus_rule}
{_hook_hint}

重编规则：
1. 只有1个Hook且位于首段；第2段必须直接兑现Hook；只有1个Close且位于末段，Close后不能再有Product。
2. 来源均衡必须服从自然叙事：在效果、证据、场景、顾虑解除等阶段边界切换来源，不得逐句机械轮播，也不得把某个来源集中成连续长段。
3. 通常选择{_clip_range}个片段，{_total_rule}；逐项相加候选秒数，落在目标范围内。
4. 每个片段优先1个编号；只有补齐完整语义时可选2-3个连续编号。不得编造、跳号、重叠，不得选择标为不可选的条目。
5. {_dedup_rule}
6. {_hook_rule}
7. {_product_rule}
8. {_close_rule}
{_ai_rules_prompt}

只输出一个JSON对象，不要解释，不要Markdown。clips必须是全新的完整有序片单；expansion_plan提供4-8个未被clips使用的完整Product备用片。
clips每项格式：
{{"clip_type":"hook|product|close","srt_indices":[1],"focus":"实际卖点主题","reason":"本段叙事作用","trim_priority":0}}
expansion_plan每项格式：
{{"priority":1,"after_srt_indices":[1],"after_order":1,"srt_indices":[2],"focus":"备用主题","reason":"补片作用"}}
Hook、第2段、Close的trim_priority必须为0；其他Product从1开始填写不重复的正整数，数字越小越先删。

全部候选：
{indexed_transcript}"""
    elif _director_repair_mode:
        user_msg = f"""这是一次定向片单修复，不是重新自由策划。必须解决检查项，不能原样返回旧骨架。

{extra_instruction}
{_content_review_prompt}

本轮指定偏好：{_active_preference or focus or '全量选片'}
{_preference_quota_prompt}
{_hook_focus_rule}
{_hook_hint}

执行顺序：
1. 先保留骨架中未被点名的问题项及其相对顺序，Hook仍是第1段，第二段仍直接兑现Hook，Close仍是末段。
2. 再从下方安全候选中补入或替换完整Product，必须真实达到检查项写明的偏好段数和时长区间。
3. 输出前逐项相加候选标注的秒数；低于下限继续补，高于上限按trim_priority删低价值Product。
4. 只使用连续srt_indices，不得编造编号、文本或时间，不得选择标为不可选的条目。
5. 返回修复后的完整有序JSON对象，不要解释，不要Markdown。对象包含clips和expansion_plan；clips是修复后的主片单，expansion_plan仍需提供4-8个未使用备用Product。

clips每项格式：
{{"clip_type":"hook|product|close","srt_indices":[1],"focus":"实际卖点主题","reason":"本段叙事作用","trim_priority":0}}
expansion_plan每项格式：
{{"priority":1,"after_srt_indices":[1],"after_order":1,"srt_indices":[2],"focus":"备用主题","reason":"补片作用"}}
Hook、第二段、Close的trim_priority必须为0；其他Product从1开始填写不重复的正整数，数字越小越先删。

全部候选：
{indexed_transcript}"""
    else:
        user_msg = f"""以下是编号后的直播字幕条目，你需要像专业短视频编导一样，从中精选条目并编排成一个完整的带货短视频脚本.

{_category_context_prompt}
{_merge_source_rule}
要求:
1. {_dedup_rule}
2. 像讲故事一样编排，每个片段自然衔接下一段，听起来是一段流畅的口播
3. ★你是最终叙事负责人★ clips数组的顺序就是最终成片顺序，后续程序不会替你重排或替换主题；程序只会在低于时长下限时执行你声明的expansion_plan。必须只有1个Hook且位于首段，只有1个Close且位于末段；Close之后绝对不能再有Product。先兑现Hook，再展开效果、证据、场景或顾虑，最后自然收束
4. 通常选择{_clip_range}个片段，{_total_rule}。数量不是硬指标；若安全且完整的内容不足，宁可略短也不要用重复、残句或无关内容凑数
5. ★每个片段优先选1个编号条目；如果只选前一句会导致语义不完整，必须连带后一句，允许选2个连续编号条目；只有补齐完整主谓宾时才允许3个连续条目★ 完整句 > 短句；绝对不要在一句话中间截断
6. ★片段之间禁止条目编号重叠★ 同一条目只能出现在一个片段中
7. srt_indices必须连续；禁止选择#05和#07却跳过#06。若两条不连续字幕都值得保留，必须分别作为两个片段放到各自合适的叙事位置
8. [本轮选片偏好]{focus}
   {_hook_focus_rule}；主卖点偏好优先进入证明细节和顾虑解除段。后续Product必须覆盖其他卖点角度（{_coverage_examples}等），确保单视频介绍完整。偏好角度遵守上述目标区间，其他同一角度最多2段，禁止全片只讲一个维度
{_preference_quota_prompt}
9. {_diff_vibe}
10. {_hook_rule}
11. {_product_rule}
12. {_close_rule}
{_ai_rules_prompt}
{_recent_history_prompt}
{_extra_instruction_prompt}
{_content_review_prompt}

★输出格式★: 只输出一个JSON对象。clips中每个片段用srt_indices指定编号条目，不要填start/end时间戳；每项都填写trim_priority。expansion_plan提供4-8个未被clips使用的完整Product，并用after_srt_indices锚定插入位置。★优先1个条目；前后句强相关时选2个连续条目，确保单片段3-9秒且语义完整，不要选3个以上★:
{{
  "clips": [
    {{"clip_type": "hook", "srt_indices": [3], "focus": "{_example_hook_focus}", "reason": "{_example_hook_reason}", "trim_priority": 0}},
    {{"clip_type": "product", "srt_indices": [8], "focus": "{_example_product_focus_1}", "reason": "{_example_product_reason_1}", "trim_priority": 0}}
  ],
  "expansion_plan": [
    {{"priority": 1, "after_srt_indices": [8], "after_order": 2, "srt_indices": [18], "focus": "{_example_product_focus_2}", "reason": "备用不同卖点"}}
  ]
}}

{_hook_hint}

字幕条目:
{indexed_transcript}"""

    _system_low, _system_high = _duration_source_bounds(_AI_TARGET_DURATION, duration_contract)
    if _skip_focus:
        _system_prompt = SYSTEM_PROMPT.replace("45-65", f"{_system_low:.0f}-{_system_high:.0f}").replace("10-15", _AI_CLIP_COUNT).replace("最低8段", f"最低{_min_pieces}段").replace("6-10", f"{max(5, _min_pieces - 4)}-{_min_pieces}")
    else:
        _system_prompt = (
            DIRECTOR_SYSTEM_PROMPT
            .replace("__TARGET__", str(int(_AI_TARGET_DURATION or 60)))
            .replace("__LOW__", f"{_system_low:.0f}")
            .replace("__HIGH__", f"{_system_high:.0f}")
        )
    _category_overlay = _category_system_overlay(main_category)
    if _category_overlay:
        _system_prompt = f"{_system_prompt}\n\n{_category_overlay}"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt},
            {"role": "user", "content": user_msg}
        ],
        "temperature": temperature,
        "top_p": 0.9,
        "max_tokens": 8192,
        # DeepSeek: 关闭思考模式（R1），防止返回 reasoning_content 无 content
    }, ensure_ascii=False).encode("utf-8")
    # DeepSeek: 显式关闭思考模式（R1），防止空content
    if "deepseek" in model.lower() and "seed" not in model.lower():
        try:
            body_dict = json.loads(body)
            body_dict["thinking"] = {"type": "disabled"}
            body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass
    # 豆包Seed模型：添加reasoning_effort，Seed忽略temperature
    if "seed" in model.lower():
        try:
            body_dict = json.loads(body)
            body_dict["reasoning_effort"] = "low"
            # Seed固定temperature=1，但传了也不报错，保留即可
            body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass

    url = ai_chat_completions_url(base_url)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        ctx = create_ssl_context()
        with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        msg = result.get("choices", [{}])[0].get("message", {})
        content = msg.get("content", "")
        # DeepSeek-R1: reasoning in reasoning_content, final answer in content
        # If content is empty but reasoning exists, don't waste time extracting
        # Just log and let the retry loop handle it
        if not content.strip():
            reasoning = msg.get("reasoning_content", "")
            if reasoning.strip():
                _log(f"AI: R1推理完成但content为空，reasoning长度={len(reasoning)}字，将重试")
            # No R1 response, just empty content from API error
            if not reasoning.strip():
                _log(f"AI: 响应为空")
        _log(f"AI: 响应成功，内容长度={len(content)}字")
        _skip_focus = _orig_skip  # 恢复全局状态
        if return_raw:
            # return_raw模式（多版本选素材）：直接解析clips数组
            return _parse_ai_response(
                content,
                log_fn,
                srt_entries,
                _forbidden_indices,
                require_srt_indices=bool(srt_entries) and not _orig_skip,
                allowed_hook_indices=_allowed_hook_indices or None,
                allowed_candidate_indices=_review_allowed_candidate_ids,
            )
        return _parse_ai_response(
            content,
            log_fn,
            srt_entries,
            _forbidden_indices,
            require_srt_indices=bool(srt_entries) and not _orig_skip,
            allowed_hook_indices=_allowed_hook_indices or None,
            allowed_candidate_indices=_review_allowed_candidate_ids,
        )
    except urllib.error.HTTPError as e:
        err = ""
        try: err = e.read().decode("utf-8", errors="replace")[:200]
        except Exception: pass
        friendly = _friendly_http(e.code, err)
        message = f"AI 接口调用失败 (HTTP {e.code})：{friendly}"
        _log(f"⚠️ {message}")
        _skip_focus = _orig_skip  # 恢复全局状态
        if _is_non_retryable_http(e.code):
            raise NonRetryableAIError(message) from e
        return []
    except Exception as e:
        _log(f"⚠️ AI 选片失败: {_friendly_msg(str(e))}")
        _skip_focus = _orig_skip  # 恢复全局状态
        return []


# ============================================================
# 解析 AI 响应
# ============================================================
def _parse_raw_response(content, log_fn=None):
    """解析AI响应为原始JSON数据（用于多版本模式，保留versions结构）"""
    def _log(msg):
        if log_fn: log_fn(msg)
    # 去掉 markdown 代码块
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', content)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    cleaned = cleaned.replace('```json', '').replace('```', '')
    cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
        return data
    except json.JSONDecodeError:
        # 尝试从后往前找JSON对象
        # 多版本格式是 {"versions": [...]}，找 { 开始
        m = re.search(r'\{\s*"versions"', cleaned)
        if m:
            sub = cleaned[m.start():]
            last_brace = sub.rfind('}')
            if last_brace >= 0:
                sub = sub[:last_brace+1]
                try:
                    return json.loads(sub)
                except json.JSONDecodeError:
                    pass
        # 也可能是数组格式 [{"angle":...}, ...]
        m2 = re.search(r'\[\s*\{', cleaned)
        if m2:
            sub = cleaned[m2.start():]
            depth = 0
            end_pos = -1
            for ci, ch in enumerate(sub):
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end_pos = ci + 1
                        break
            last_bracket = end_pos - 1 if end_pos > 0 else -1
            if last_bracket >= 0:
                sub = sub[:last_bracket+1]
                try:
                    return json.loads(sub)
                except json.JSONDecodeError:
                    pass
        _log(f"AI: 多版本JSON解析失败，原始前200字: {content[:200]}")
        return None


def _parse_ai_response(content, log_fn, srt_entries=None, forbidden_indices=None, require_srt_indices=False, allowed_hook_indices=None, allowed_candidate_indices=None):
    def _log(msg):
        if log_fn: log_fn(msg)
    _allowed_review_ids_for_plan = (
        {int(index) for index in allowed_candidate_indices}
        if allowed_candidate_indices is not None else None
    )

    def _review_plan_item_allowed(item):
        if _allowed_review_ids_for_plan is None:
            return True
        if not isinstance(item, dict):
            return False
        indices = item.get("srt_indices", item.get("srt_index"))
        if isinstance(indices, int):
            indices = [indices]
        try:
            normalized = [int(index) for index in (indices or [])]
        except (TypeError, ValueError):
            return False
        return bool(normalized) and all(
            index in _allowed_review_ids_for_plan for index in normalized
        )


    # 去掉 markdown 代码块包裹(```json ... ```)
    # R1 经常返回 ```json\n[...]\n``` 格式，需要多行匹配
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', content)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    # 兜底：如果有残留的 ``` 则逐个清除
    cleaned = cleaned.replace('```json', '').replace('```', '')
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # 用 rfind 从后往前找 JSON 数组，跳过推理文本中的方括号
        m = re.search(r'\[\s*\{', cleaned)
        idx = m.start() if m else -1
        if idx >= 0:
            sub = cleaned[idx:]
            depth = 0
            end_pos = -1
            for ci, ch in enumerate(sub):
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end_pos = ci + 1
                        break
            last_bracket = end_pos - 1 if end_pos > 0 else -1
            if last_bracket >= 0:
                sub = sub[:last_bracket+1]
                try:
                    data = json.loads(sub)
                except json.JSONDecodeError:
                    _log(f"AI: JSON 解析失败，原始前200字: {content[:200]}"); return []
            else:
                _log(f"AI: 未找到 JSON 数组结尾，原始前200字: {content[:200]}"); return []
        else:
            _log(f"AI: 未找到 JSON 数组，原始前200字: {content[:200]}"); return []

    # ★构建SRT条目查找表★
    _srt_entry_map = {}
    if srt_entries:
        for i, (es, ee, et) in enumerate(srt_entries, 1):
            _srt_entry_map[i] = (es, ee, et)

    # 检测多版本格式
    # 注意：单版本管线调用时，AI可能错误返回多版本格式
    # 此时应提取第一个方案的片段作为单版本结果，而不是返回dict
    if isinstance(data, dict) and "versions" in data:
        _log(f"AI: 检测到多版本格式(dict)，{len(data['versions'])}个方案")
        # 单版本模式：提取第一个方案的片段
        versions = data.get("versions", [])
        if versions and isinstance(versions[0], dict):
            first_clips = versions[0].get("clips", [])
            if isinstance(first_clips, list) and first_clips:
                _log(f"AI: 从多版本格式提取方案1的{len(first_clips)}个片段")
                data = first_clips  # 降级为单版本处理
            else:
                _log("AI: 多版本格式中方案1无有效片段")
                return []
        else:
            return []
    
    # 检测多版本格式: AI可能返回 [{angle, clips}, ...] 顶层数组
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        first_item = data[0]
        # 如果第一项有angle+clips但没有clip_type/start/text，说明是多版本格式
        if "angle" in first_item and "clips" in first_item and "clip_type" not in first_item:
            _log(f"AI: 检测到多版本格式(array)，{len(data)}个方案")
            # 单版本模式：提取第一个方案的片段
            first_clips = first_item.get("clips", [])
            if isinstance(first_clips, list) and first_clips:
                _log(f"AI: 从多版本格式提取方案1的{len(first_clips)}个片段")
                data = first_clips
            else:
                return []

    raw_expansion_plan = data.get("expansion_plan", []) if isinstance(data, dict) else []
    if isinstance(raw_expansion_plan, list):
        raw_expansion_plan = [
            item for item in raw_expansion_plan if _review_plan_item_allowed(item)
        ]
    _analysis_metadata_context()["expansion_plan"] = (
        list(raw_expansion_plan) if isinstance(raw_expansion_plan, list) else []
    )
    if not isinstance(data, list):
        data = data.get("clips", [])
    if not isinstance(data, list):
        _log("AI: 格式不正确"); return []

    type_map = {
        "hook": "hook", "钩子": "hook",
        "product": "product", "种草": "product", "卖点": "product", "highlight": "product", "亮点": "product",
        "scene": "product", "场景": "product",
        "close": "close", "促单": "close", "收尾": "close",
        "bridge": "bridge", "过渡": "bridge", "科普": "bridge",
        "trend": "trend", "趋势": "trend",
        # "price"和"价格"类型不再映射为product，已禁止选择
        "urgency": "close", "紧迫": "close",
        "call_to_action": "close", "cta": "close", "逼单": "close",
    }

    clips = []
    skipped_no_text = 0
    skipped_bad_time = 0
    skipped_missing_indices = 0
    skipped_outside_review = 0
    skipped_invalid_hook = 0
    allowed_hook_indices = {
        int(index) for index in (allowed_hook_indices or [])
        if str(index).strip().isdigit()
    }
    allowed_candidate_indices_set = (
        {int(index) for index in allowed_candidate_indices}
        if allowed_candidate_indices is not None else None
    )
    for idx, item in enumerate(data):
        # 诊断:打印第一个 item 的所有字段名
        if idx == 0:
            _log(f"AI: 第1项字段名={list(item.keys()) if isinstance(item, dict) else type(item).__name__}")
            _log(f"AI: 第1项原始值 start={item.get('start')} end={item.get('end')} text={str(item.get('text',''))[:40]}")
        ct = str(item.get("clip_type", item.get("type", "")))
        ct = type_map.get(ct, ct)
        if ct not in GOLDEN_CHAIN:
            ct = "highlight"
        try:
            trim_priority = int(item.get("trim_priority", 0) or 0)
        except Exception:
            trim_priority = 0
        
        # ★优先使用srt_indices查表★（解决AI时间戳幻觉问题）
        srt_idx = item.get("srt_indices", item.get("srt_index", None))
        if require_srt_indices and not srt_idx:
            skipped_missing_indices += 1
            continue
        if srt_idx and _srt_entry_map:
            # 按索引查SRT条目，构建clip
            if isinstance(srt_idx, int):
                srt_idx = [srt_idx]
            try:
                srt_idx = [int(index) for index in srt_idx]
            except (TypeError, ValueError):
                skipped_bad_time += 1
                continue
            if allowed_candidate_indices_set is not None and any(
                index not in allowed_candidate_indices_set for index in srt_idx
            ):
                skipped_outside_review += 1
                continue
            if ct == "hook" and allowed_hook_indices:
                try:
                    hook_indices = [int(index) for index in srt_idx]
                except Exception:
                    hook_indices = []
                if len(hook_indices) != 1 or hook_indices[0] not in allowed_hook_indices:
                    skipped_invalid_hook += 1
                    continue
            # 排序+拆分不连续索引为多个clip
            valid_indices = sorted(set(i for i in srt_idx if 1 <= i <= len(_srt_entry_map) and i not in (forbidden_indices or set())))
            if not valid_indices:
                skipped_bad_time += 1; continue
            # 找连续分组
            groups = []
            current_group = [valid_indices[0]]
            for i in range(1, len(valid_indices)):
                if valid_indices[i] == valid_indices[i-1] + 1:
                    current_group.append(valid_indices[i])
                else:
                    groups.append(current_group)
                    current_group = [valid_indices[i]]
            groups.append(current_group)
            # 每个连续组生成一个clip
            for gi, group in enumerate(groups):
                es_start, _, _ = _srt_entry_map[group[0]]
                _, ee_end, _ = _srt_entry_map[group[-1]]
                group_text = "".join(_srt_entry_map[idx][2] for idx in group)
                focus = str(item.get("focus", "")).strip()
                ct_this = ct if gi == 0 else ct  # 保持类型不变
                dur = float(ee_end - es_start)
                clip_value = (ct_this, group_text, float(es_start), float(ee_end), 50, dur, focus)
                clips.append(clip_value)
                if trim_priority > 0:
                    _analysis_metadata_context().setdefault("trim_priorities", {})[
                        _director_clip_trim_key(clip_value)
                    ] = trim_priority
                if gi > 0:
                    _log(f"  不连续索引拆分: [{ct}] 补充clip {group}")
        else:
            # fallback: 旧格式（start/end）
            text = str(item.get("text", "")).strip()
            start = float(_parse_time(item.get("start", 0)))
            end = float(_parse_time(item.get("end", start + 5)))
            if not text:
                skipped_no_text += 1; continue
            if end <= start:
                skipped_bad_time += 1; continue
            focus = str(item.get("focus", "")).strip()
            clip_value = (ct, text, start, end, 50, float(end - start), focus)
            clips.append(clip_value)
            if trim_priority > 0:
                _analysis_metadata_context().setdefault("trim_priorities", {})[
                    _director_clip_trim_key(clip_value)
                ] = trim_priority
    _log(
        f"AI: JSON片单{len(data)}项，解析到{len(clips)}段"
        f"（缺编号{skipped_missing_indices}，池外编号{skipped_outside_review}，非法Hook{skipped_invalid_hook}，"
        f"无文本{skipped_no_text}，无效时间{skipped_bad_time}）"
    )
    if not clips:
        _log(
            f"AI: {len(data)}项中有效0(缺少候选编号:{skipped_missing_indices}, "
            f"无文本:{skipped_no_text}, 时间错误:{skipped_bad_time})"
        )
    return clips




# ============================================================
# 多版本格式解析
# ============================================================
def _parse_multi_version_data(data, log_fn, srt_entries=None, forbidden_indices=None):
    """解析AI返回的多版本格式 {"versions": [{angle, clips}, ...]}
    返回: dict {"versions": [{angle, clips}, ...]}
    每个clips是标准的7元组列表，支持srt_indices查表
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    type_map = {
        "hook": "hook", "钩子": "hook",
        "product": "product", "种草": "product", "卖点": "product", "highlight": "product", "亮点": "product",
        "scene": "product", "场景": "product",
        "close": "close", "促单": "close", "收尾": "close",
        "bridge": "bridge", "过渡": "bridge", "科普": "bridge",
        "trend": "trend", "趋势": "trend",
        "urgency": "close", "紧迫": "close",
        "call_to_action": "close", "cta": "close", "逼单": "close",
    }

    # 构建SRT条目查找表
    _srt_entry_map = {}
    if srt_entries:
        for i, (es, ee, et) in enumerate(srt_entries, 1):
            _srt_entry_map[i] = (es, ee, et)

    # 处理不同的输入格式
    versions_data = []
    if isinstance(data, dict):
        versions_data = data.get("versions", [])
    elif isinstance(data, list) and len(data) > 0:
        # 可能是 [{angle, clips}, ...] 格式
        first = data[0]
        if isinstance(first, dict) and "angle" in first and "clips" in first:
            versions_data = data
        else:
            # 单版本格式，包装为一个版本
            versions_data = [{"angle": "综合", "clips": data}]

    if not versions_data:
        _log("AI: 多版本格式但versions为空")
        return {"versions": []}

    result_versions = []
    for vi, ver in enumerate(versions_data):
        if not isinstance(ver, dict):
            continue
        angle = str(ver.get("angle", f"方案{vi+1}"))
        clips_raw = ver.get("clips", [])
        if not isinstance(clips_raw, list):
            _log(f"AI: 方案{vi+1} clips格式错误")
            continue

        clips = []
        for item in clips_raw:
            if not isinstance(item, dict):
                continue
            ct = str(item.get("clip_type", item.get("type", "")))
            ct = type_map.get(ct, ct)
            if ct not in GOLDEN_CHAIN:
                ct = "highlight"

            focus = str(item.get("focus", "")).strip()
            reason = str(item.get("reason", "")).strip()

            # ★优先使用srt_indices查表★
            srt_idx = item.get("srt_indices", item.get("srt_index", None))
            if srt_idx and _srt_entry_map:
                if isinstance(srt_idx, int):
                    srt_idx = [srt_idx]
                valid_indices = sorted(set(i for i in srt_idx if 1 <= i <= len(_srt_entry_map) and i not in (forbidden_indices or set())))
                if not valid_indices:
                    continue
                # 找连续分组
                groups = []
                current_group = [valid_indices[0]]
                for i in range(1, len(valid_indices)):
                    if valid_indices[i] == valid_indices[i-1] + 1:
                        current_group.append(valid_indices[i])
                    else:
                        groups.append(current_group)
                        current_group = [valid_indices[i]]
                groups.append(current_group)
                for gi, group in enumerate(groups):
                    es_start, _, _ = _srt_entry_map[group[0]]
                    _, ee_end, _ = _srt_entry_map[group[-1]]
                    group_text = "".join(_srt_entry_map[idx][2] for idx in group)
                    ct_this = ct
                    dur = float(ee_end - es_start)
                    clips.append((ct_this, group_text, float(es_start), float(ee_end), 50, dur, focus))
            else:
                # fallback: start/end格式
                text = str(item.get("text", "")).strip()
                start = float(_parse_time(item.get("start", 0)))
                end = float(_parse_time(item.get("end", start + 5)))
                if not text or end <= start:
                    continue
                dur = float(end - start)
                clips.append((ct, text, start, end, 50, dur, focus))

        if clips:
            total_dur = sum(c[5] for c in clips)
            _log(f"AI: 方案{vi+1} [{angle}] {len(clips)}片段, {total_dur:.1f}s")
            result_versions.append({"angle": angle, "clips": clips})

    return {"versions": result_versions}


def ai_analyze_multi_versions(
    srt_text,
    log_fn=None,
    force_category=None,
    focus_hint=None,
    num_versions=3,
    ai_controls=None,
    target_duration=60,
    duration_tolerance=None,
):
    """多版本AI选片：1次AI调用直接出3个独立叙事方案，减少2/3成本和时间
    返回: {"versions": [{angle, clips}, ...]}
    """
    _begin_analysis_metadata()

    def _log(msg):
        if log_fn: log_fn(msg)

    global _AI_TARGET_DURATION
    _AI_TARGET_DURATION = target_duration

    settings = load_settings()
    if not settings.get("api_key"):
        _log("AI: 未配置 API Key")
        return {"versions": []}

    api_key = settings["api_key"]
    base_url = normalize_ai_base_url(settings["base_url"])
    model = settings["model"]
    _ai_rules = _merge_ai_rules(ai_controls)
    _enforce_category_filter = bool(_ai_rules.get("category_filter", True))
    _enforce_time_coherence = bool(_ai_rules.get("time_coherence", True))
    _hook_cap_sec = _hook_cap_seconds(_ai_rules)
    _forced_main_cat = _normalize_forced_category(force_category)

    # [预处理] 与单版本相同的SRT清洗流程
    from srt_splitter import split_long_srt_entries
    srt_text = split_long_srt_entries(srt_text, max_duration=5.0, log_fn=_log)

    cleaned_srt = _pre_clean_srt(srt_text, log_fn)
    if not cleaned_srt.strip():
        _log("AI: 清洗后无有效字幕，尝试使用原始SRT...")
        cleaned_srt = srt_text
        if not cleaned_srt.strip():
            _log("AI: 原始SRT也为空")
            return {"versions": []}

    cleaned_srt = _dedup_srt_repeated_sections(cleaned_srt, log_fn)
    if _enforce_category_filter:
        cleaned_srt, _detected_main_cat = _filter_srt_by_main_product(cleaned_srt, log_fn, force_category=force_category)
    else:
        _detected_main_cat = _forced_main_cat
        _log("AI选片规则: 已关闭强制同一品类过滤")
    if _forced_main_cat:
        _detected_main_cat = _forced_main_cat
    _history_key = _clip_history_key(cleaned_srt)
    _recent_history = _get_recent_clip_history(_history_key)
    _recent_history_hint = _format_recent_history_hint(_recent_history)
    if _recent_history:
        _log(f"多版本差异化: 检测到同素材最近已用 {len(_recent_history)} 个片段，本次优先避开")
    _feedback_scope = _feedback_scope_key("smart", _detected_main_cat)
    _feedback_profile = _build_preview_feedback_profile(scope=_feedback_scope)
    _feedback_mode, _feedback_configured, _feedback_count = _feedback_effective_strength(settings, _feedback_profile)
    _feedback_prompt_enabled = _feedback_mode in {"light", "standard", "strong"}
    _feedback_hint = _build_preview_feedback_hint_for_strength(_feedback_profile, _feedback_mode) if _feedback_prompt_enabled else ""
    if _feedback_hint:
        _recent_history_hint = "\n".join(part for part in (_recent_history_hint, _feedback_hint) if part)
        _log(f"多版本剪辑风格画像: 已按{_feedback_strength_label(_feedback_mode)}模式进入AI软参考（样本{_feedback_count}）")
    elif _feedback_mode == "readonly":
        _log(f"多版本剪辑风格画像: 样本{_feedback_count}条，未满3条，仅记录不参与选片")
    elif _feedback_mode == "off":
        _log("多版本剪辑风格画像: 画像影响强度已关闭，不参与本次选片")

    # ★构建SRT条目索引★
    _indexed_srt_entries = []
    for block in cleaned_srt.strip().split(chr(10)+chr(10)):
        bl = block.strip().split(chr(10))
        if len(bl) >= 1 and '-->' in bl[0]:
            try:
                parts = bl[0].split('-->')
                h, m, s = parts[0].strip().replace(',', '.').split(':')
                es = int(h)*3600 + int(m)*60 + float(s)
                h, m, s = parts[1].strip().replace(',', '.').split(':')
                ee = int(h)*3600 + int(m)*60 + float(s)
                txt = ' '.join(bl[1:]).strip()
                _indexed_srt_entries.append((es, ee, txt))
            except: pass
    _log(f"AI: 构建SRT条目索引 {len(_indexed_srt_entries)} 条")

    # ★预扫描Hook候选：给多版本提供更丰富的开头池★
    _hook_hint = ""
    _kw_data_mv = load_keywords()
    _hook_kw_scan = _kw_data_mv["hook_keywords"]
    _hook_candidates_mv, _hook_candidate_total_mv = _collect_hook_candidates_from_entries(
        _indexed_srt_entries,
        hook_keywords=_hook_kw_scan,
        focus_hint=focus_hint,
        ai_controls=ai_controls,
        limit=12,
    )
    if _hook_candidates_mv:
        _cand_str = ', '.join(
            f'#{idx:02d}"{text[:18]}"({dur:.0f}s/{",".join(reasons[:2])})'
            for idx, text, dur, _score, reasons in _hook_candidates_mv
        )
        _hook_hint = f"★Hook候选(优先从中选择): {_cand_str}★"
        _log(f"多版本: Hook候选池 {_hook_candidate_total_mv} 个，提供 {len(_hook_candidates_mv)} 个")

    # ★第一步：AI选素材（只挑选不编排），失败自动重试★
    # IncompleteRead(0 bytes read) 是 DeepSeek 服务端 TCP 中断，重试通常成功
    raw_clips = None
    for _retry in range(5):
        _log(f"AI: 调用 {model} 选素材(25-35个片段)..." + (f" 重试{_retry+1}/3" if _retry > 0 else ""))
        raw_clips = _call_ai(api_key, base_url, model, cleaned_srt, _log,
                            focus_hint=focus_hint,
                            srt_entries=_indexed_srt_entries,
                            hook_candidates_hint=_hook_hint if _hook_hint else None,
                            multi_version=True,
                            return_raw=True,
                            num_versions=num_versions,
                            ai_controls=ai_controls,
                            recent_history_hint=_recent_history_hint,
                            main_category=_detected_main_cat)
        if raw_clips and len(raw_clips) >= 10:
            break
        import time as _sleepmod
        _sleepmod.sleep(5)

    if not raw_clips or len(raw_clips) < 10:
        _log(f"AI: 素材选取失败(重试3次后仅{len(raw_clips) if raw_clips else 0}个片段)，降级到单版本多次调用...")
        # 降级策略：改走各自独立的单版本调用（带版本间去重）
        return _multi_version_fallback(srt_text, _log, force_category, focus_hint, num_versions,
                                       api_key, base_url, model, cleaned_srt, _indexed_srt_entries,
                                       _hook_hint, ai_controls=ai_controls, main_category=_detected_main_cat)
        return _multi_version_fallback(srt_text, _log, force_category, focus_hint, num_versions,
                                       api_key, base_url, model, cleaned_srt, _indexed_srt_entries,
                                       _hook_hint, ai_controls=ai_controls, main_category=_detected_main_cat)

    _log(f"AI: 素材选取成功，共{len(raw_clips)}个片段")

    # ★对素材做初步后处理★
    raw_clips = _dedup_clips(
        raw_clips,
        _log,
        multi_version=True,
        focus_hint=focus_hint,
        srt_text=cleaned_srt,
        main_category=_detected_main_cat,
        preferred_focus=_current_focus_used_label(),
        ai_controls=ai_controls,
        merge_mode=True,
    )
    if _enforce_category_filter:
        raw_clips = _post_filter_cross_category(raw_clips, cleaned_srt, _log, preferred_cat=_detected_main_cat)
    raw_clips = [(ct, _apply_asr_corrections(text, _log), s, e, sc, d, focus)
                 for ct, text, s, e, sc, d, focus in raw_clips]
    raw_clips = _filter_host_interaction(raw_clips, _log)
    raw_clips = _filter_price_and_cta(raw_clips, _log)
    raw_clips = _filter_context_damaged_clips(raw_clips, cleaned_srt, _log)
    raw_clips = _filter_celebrity(raw_clips, _log)
    raw_clips = _validate_cta(raw_clips, _log)
    raw_clips = _dedup_clip_text_overlap(raw_clips, _log)
    raw_clips = _cap_clip_duration(raw_clips, _log, srt_text=cleaned_srt)
    raw_clips = _trim_filler_start(raw_clips, cleaned_srt, _log)
    raw_clips = _trim_filler_middle(raw_clips, cleaned_srt, _log)
    raw_clips = _filter_recent_similar_clips(raw_clips, _recent_history, _log, min_keep=12)

    if len(raw_clips) < 10:
        _log(f"AI: 素材后处理不足(仅{len(raw_clips)}个)，降级到3次单版本调用...")
        return _multi_version_fallback(srt_text, _log, force_category, focus_hint, num_versions,
                                       api_key, base_url, model, cleaned_srt, _indexed_srt_entries,
                                       _hook_hint, ai_controls=ai_controls, main_category=_detected_main_cat)

    # ★第二步：按类型分组，为编排做准备★
    hooks_pool = [c for c in raw_clips if _is_hook_clip(c)]
    products_pool = [c for c in raw_clips if not _is_hook_clip(c) and not _is_close_clip(c)]
    closes_pool = [c for c in raw_clips if _is_close_clip(c)]
    _log(f"素材分组: Hook={len(hooks_pool)}, Product={len(products_pool)}, Close={len(closes_pool)}")
    if len(hooks_pool) < num_versions:
        _log(f"多版本结构提示: Hook候选不足 {len(hooks_pool)}/{num_versions}，将尝试从Product提取替代开头")
    if len(closes_pool) < num_versions:
        _log(f"多版本结构提示: Close候选不足 {len(closes_pool)}/{num_versions}，将尝试从Product提取替代结尾")
    _reserved_hooks = hooks_pool[:num_versions] + [None] * max(0, num_versions - len(hooks_pool))
    _reserved_closes = closes_pool[:num_versions] + [None] * max(0, num_versions - len(closes_pool))

    # ★第三步：确定每个版本的focus角度★
    import random as _rnd
    if _is_food_fresh_category(_detected_main_cat):
        _all_focus_angles = ["口感食欲", "新鲜品质", "产地溯源", "规格分量", "发货保鲜", "场景吃法", "情绪感染"]
        _angle_hints_map = {
            "口感食欲": "Hook选试吃反应/切开爆汁/脆甜鲜嫩等强食欲片段，Product按口感→新鲜→规格→发货信任串联",
            "新鲜品质": "Hook选现摘现发/鲜活饱满/品质背书，Product按新鲜证明→口感反馈→产地/规格→售后串联",
            "产地溯源": "Hook选产地源头/果园基地/产区特色，Product按源头背书→品质→口感→发货保障串联",
            "规格分量": "Hook选个头/净重/开箱展示/大小对比，Product按规格展示→推荐规格→口感→冷链售后串联",
            "发货保鲜": "Hook选冷链/保鲜/坏果包赔/收到货状态，Product按履约信任→新鲜口感→规格推荐串联",
            "场景吃法": "Hook选家庭囤货/孩子老人/早餐夜宵/火锅煲汤等场景，Product按场景代入→口感→规格→发货串联",
            "情绪感染": "Hook选主播真实试吃和强反应，Product用情绪带出口感/新鲜/规格/售后，不要夸大保健功效",
        }
    else:
        _all_focus_angles = ["版型显瘦", "颜色氛围", "场景搭配", "性价比", "情绪感染", "流行趋势", "面料质感"]
        _angle_hints_map = {
            "版型显瘦": "Hook选显瘦/遮肉类的短爆点，Product优先选讲版型/显瘦/修饰身材的片段，按面料→版型→尺码的逻辑串联",
            "颜色氛围": "Hook选颜色/显白类的短爆点，Product优先选讲颜色/显白/衬肤色/温柔色的片段，按颜色→穿感→场景的逻辑串联",
            "场景搭配": "Hook选场景化的短爆点，Product优先选通勤/约会/实穿/百搭/搭配的片段，按场景→单品→搭配的逻辑串联",
            "性价比": "Hook选品质对比类的短爆点，Product优先选品质/价值感的片段(避开具体价格)，按品质→对比→推荐串联",
            "情绪感染": "Hook选最强情绪爆点，Product选激动/真诚的片段穿插，按情绪递进串联",
            "流行趋势": "Hook选风格类的短爆点，Product选设计感/当季流行的片段，按风格定位→搭配→场景串联",
            "面料质感": "Hook选触感/质感的短爆点，Product选面料/手感/亲肤的片段，按面料→版型→上身效果的逻辑串联",
        }

    # 构建版本角度列表
    _version_angles = []
    if focus_hint and focus_hint != "自动":
        # 用户指定偏好：版本1用指定偏好，其余随机
        _matched_angle = focus_hint
        _rest_angles = [a for a in _all_focus_angles if a not in (_matched_angle,)]
        _rnd.shuffle(_rest_angles)
        _version_angles = [_matched_angle] + _rest_angles[:num_versions-1]
    else:
        _rnd.shuffle(_all_focus_angles)
        _version_angles = _all_focus_angles[:num_versions]
    _version_angles = _version_angles[:num_versions]

    # ★第四步：对每个版本做编排AI调用★
    processed_versions = []
    _remaining_clips = list(raw_clips)  # 可用素材池，已用的逐步移除
    _used_version_notes = []
    _used_version_clips = []

    for vi, _angle in enumerate(_version_angles):
        _angle_hint = _angle_hints_map.get(_angle, "按Hook→Product→Close标准编排")
        _focus_desc = _angle
        _reserved_hook = _reserved_hooks[vi] if vi < len(_reserved_hooks) else None
        _reserved_close = _reserved_closes[vi] if vi < len(_reserved_closes) else None
        _reserved_for_other_versions = [
            c for idx, c in enumerate(_reserved_hooks + _reserved_closes)
            if c and idx % max(1, num_versions) != vi
        ]
        _compose_pool = [
            c for c in _remaining_clips
            if not any(c is rc or c == rc for rc in _reserved_for_other_versions)
        ]

        _log(f"版本{vi+1}/{num_versions} [{_angle}]: 编排AI调用...")
        # 传入剩余素材，并扣住其他版本的预留Hook/Close，避免第一版吃完整个结构池。
        _version_clips = _compose_version_ai(
            api_key, base_url, model,
            _compose_pool, cleaned_srt,
            _angle, _angle_hint,
            set(),  # 不再用used_indices，改用池子移除
            _indexed_srt_entries,
            _log,
            vi=vi, num_versions=num_versions,
            hook_candidates_hint=_hook_hint,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            used_version_notes=_used_version_notes,
        )

        if not _version_clips or len(_version_clips) < 3:
            _log(f"版本{vi+1}: 编排失败(不足3段)，跳过")
            continue
        _version_min_keep, _version_min_dur = _recent_filter_floor(target_duration)
        _version_clips = _filter_recent_similar_clips(
            _version_clips, _used_version_clips, _log,
            min_keep=min(6, _version_min_keep),
            min_duration=max(25, _version_min_dur * 0.75),
        )

        # ★后处理（沿用现有逻辑）★
        clips = _version_clips
        clips = _extract_hook_from_products(clips, cleaned_srt, _log, focus_hint=_angle, ai_controls=ai_controls)
        clips = _force_short_hook(clips, cleaned_srt, _log, max_hook_sec=_hook_cap_sec, focus_hint=_angle, ai_controls=ai_controls)
        clips = _refine_hook_by_dynamic_score(clips, cleaned_srt, _log, focus_hint=_angle, ai_controls=ai_controls)
        clips = _repair_multi_version_structure(
            clips, _remaining_clips, _reserved_hook, _reserved_close,
            _used_version_clips, target_duration, _log, label=f"版本{vi+1}"
        )
        clips = _filter_price_and_cta(clips, _log)
        clips = _filter_context_damaged_clips(clips, cleaned_srt, _log)
        # [v9.3 - DISABLED] tighten + 延伸
        # from tighten import tighten_clip_boundaries, ensure_sentence_complete, trim_repetitive_filler, trim_tail_filer
        # clips = tighten_clip_boundaries(clips, cleaned_srt, _log)
        # clips = ensure_sentence_complete(clips, cleaned_srt, _log)
        # clips = trim_repetitive_filler(clips, cleaned_srt, _log)
        # clips = trim_tail_filler(clips, cleaned_srt, _log)

        if not clips or len(clips) < 3:
            _log(f"版本{vi+1}: 后处理后不足3段，跳过")
            continue

        # 从剩余素材池中移除已用的片段（按时间范围去重）
        _before_pool = len(_remaining_clips)
        _used_times = [(c[2], c[3]) for c in clips]
        _remaining_clips = [c for c in _remaining_clips if not any(
            max(c[2], ut[0]) < min(c[3], ut[1]) for ut in _used_times
        )]
        _log(f"  素材池: {_before_pool}→{len(_remaining_clips)} (已用{_before_pool - len(_remaining_clips)}个)")

        total_dur = sum(c[5] for c in clips)
        _log(f"✅ 版本{vi+1} [{_angle}]: {len(clips)}片段, {total_dur:.1f}s")
        processed_versions.append({"angle": _angle, "clips": clips})
        _used_version_notes.append(f"版本{vi+1}[{_angle}]: {_summarize_clips_for_diversity(clips)}")
        _used_version_clips.extend(clips)

    # 如果编排全部失败，降级到3次单版本
    if not processed_versions:
        _log("所有版本编排失败，降级到3次单版本调用...")
        return _multi_version_fallback(srt_text, _log, force_category, focus_hint, num_versions,
                                       api_key, base_url, model, cleaned_srt, _indexed_srt_entries,
                                       _hook_hint, ai_controls=ai_controls, main_category=_detected_main_cat)

    # ★版本级别软裁★
    _version_max = 150
    for _vi, _v in enumerate(processed_versions):
        _vclips = _v["clips"]
        _vdur = sum((c[3] - c[2]) for c in _vclips if len(c) > 3)
        if _vdur > _version_max:
            _log(f"版本{_vi+1}总时长{_vdur:.0f}s，软裁到{_version_max}s...")
            _keep = {"hook", "close"}
            _by_dur = sorted([(i, c) for i, c in enumerate(_vclips) if c[0] not in _keep],
                             key=lambda x: -(x[1][3] - x[1][2]) if len(x[1]) > 3 else 0)
            _del_set = set()
            for _di, (_didx, _dclip) in enumerate(_by_dur):
                if _vdur <= _version_max:
                    break
                _dd = _dclip[3] - _dclip[2] if len(_dclip) > 3 else 0
                _vdur -= _dd
                _del_set.add(_didx)
                _log(f"  软裁 [{_dclip[0]}] {_dd:.1f}s")
            if _del_set:
                _v["clips"] = [c for i, c in enumerate(_vclips) if i not in _del_set]
                _log(f"版本{_vi+1}软裁后: {len(_v['clips'])}片段, {_vdur:.0f}s")

    for _v in processed_versions:
        _remember_recent_clips(_history_key, _v.get("clips") or [], _log)

    return {"versions": processed_versions}


def _multi_version_fallback(srt_text, log_fn, force_category, focus_hint, num_versions,
                            api_key, base_url, model, cleaned_srt, srt_entries, hook_hint,
                            ai_controls=None, main_category=None):
    """降级方案：多版本1次调用失败时，回退到3次单版本调用
    带版本间去重：确保不同版本使用的Hook/Product/Close不重复
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    import random
    _ai_rules = _merge_ai_rules(ai_controls)
    _enforce_category_filter = bool(_ai_rules.get("category_filter", True))
    _enforce_time_coherence = bool(_ai_rules.get("time_coherence", True))
    _hook_cap_sec = _hook_cap_seconds(_ai_rules)
    _detected_main_cat = _normalize_forced_category(main_category) or _normalize_forced_category(force_category)
    _history_key = _clip_history_key(cleaned_srt)
    _recent_history = _get_recent_clip_history(_history_key)
    _recent_history_hint = _format_recent_history_hint(_recent_history)
    if _recent_history:
        _log(f"降级多版本差异化: 检测到同素材最近已用 {len(_recent_history)} 个片段")
    _feedback_scope = _feedback_scope_key("smart", main_category or _detected_main_cat)
    _feedback_profile = _build_preview_feedback_profile(scope=_feedback_scope)
    _settings = load_settings()
    _feedback_mode, _feedback_configured, _feedback_count = _feedback_effective_strength(_settings, _feedback_profile)
    _feedback_prompt_enabled = _feedback_mode in {"light", "standard", "strong"}
    _feedback_hint = _build_preview_feedback_hint_for_strength(_feedback_profile, _feedback_mode) if _feedback_prompt_enabled else ""
    if _feedback_hint:
        _recent_history_hint = "\n".join(part for part in (_recent_history_hint, _feedback_hint) if part)
        _log(f"降级多版本剪辑风格画像: 已按{_feedback_strength_label(_feedback_mode)}模式进入AI软参考（样本{_feedback_count}）")
    elif _feedback_mode == "readonly":
        _log(f"降级多版本剪辑风格画像: 样本{_feedback_count}条，未满3条，仅记录不参与选片")
    elif _feedback_mode == "off":
        _log("降级多版本剪辑风格画像: 画像影响强度已关闭，不参与本次选片")
    all_angle_hints = [
        ("版型显瘦", "以版型显瘦开场（Hook+前1-2个Product讲显瘦/遮肉/修饰身材/收腰/遮副乳），后续Product必须覆盖颜色/穿搭/品质等其他卖点，同一角度最多2段，面料最多2段"),
        ("颜色氛围", "以颜色氛围开场（Hook+前1-2个Product讲颜色/显白/衬肤色/抬气色/温柔色），后续Product必须覆盖版型/显瘦/穿搭等其他卖点，同一角度最多2段，面料最多2段"),
        ("场景搭配", "以场景搭配开场（Hook+前1-2个Product讲通勤/约会/实穿/百搭场景/懒人穿搭），后续Product必须覆盖版型/显瘦/颜色等其他卖点，同一角度最多2段，面料最多2段"),
        ("性价比", "以性价比开场（Hook+前1-2个Product讲品质对比/价值锚点/大牌平替/出厂价），后续Product必须覆盖版型/显瘦/场景等其他卖点，同一角度最多2段，面料最多2段，禁止报具体价格"),
        ("紧迫稀缺", "以紧迫感开场（Hook+前1-2个Product讲库存/限时/稀缺/手慢无/只剩），后续Product必须覆盖版型/颜色/显瘦等其他卖点，同一角度最多2段，面料最多2段"),
        ("情绪感染", "以情绪感染开场（Hook+前1-2个Product选最激动/最真诚/最惊艳的片段，如超级超级/太漂亮了/美爆了/绝了），后续Product必须覆盖版型/显瘦/颜色等实际卖点，同一角度最多2段，面料最多2段"),
        ("流行趋势", "以流行趋势开场（Hook+前1-2个Product讲设计感/当季流行/风格定位/松弛感/千金风），后续Product必须覆盖版型/显瘦/实穿等其他卖点，同一角度最多2段，面料最多2段"),
        ("面料质感", "以面料质感开场（Hook+前1-2个Product讲面料/手感/亲肤/丝滑/垂坠），后续Product必须覆盖版型/显瘦/颜色/穿搭等其他卖点，同一角度最多2段，★面料总共最多1段★"),
    ]
    if _is_food_fresh_category(main_category or _detected_main_cat):
        all_angle_hints = [
            ("口感食欲", "以试吃反应/切开爆汁/脆甜鲜嫩开场，后续Product必须覆盖新鲜、产地、规格、发货保鲜等其他食品卖点，同一角度最多2段"),
            ("新鲜品质", "以现摘现发/鲜活饱满/品质背书开场，后续Product按新鲜证明→口感反馈→产地/规格→售后保障串联"),
            ("产地溯源", "以源头产地/果园基地/产区特色开场，后续Product覆盖品质、口感、规格和冷链发货，不要变成泛泛聊天"),
            ("规格分量", "以个头/净重/开箱/大小对比开场，后续Product覆盖推荐规格、口感、新鲜和售后保障"),
            ("发货保鲜", "以冷链/保鲜/坏果包赔/收到货状态开场，后续Product覆盖新鲜口感、产地、规格推荐和放心拍理由"),
            ("场景吃法", "以家庭囤货/孩子老人/早餐夜宵/火锅煲汤等场景开场，后续Product覆盖口感、新鲜、规格和发货保障"),
            ("情绪感染", "以主播真实试吃强反应开场，用情绪带出口感、新鲜、规格和售后，不要剪入治疗、保健、药用功效"),
        ]

    if focus_hint and focus_hint != "自动":
        _matched = [h for h in all_angle_hints if h[0] == focus_hint or h[0][:2] == focus_hint[:2]]
        _rest = [h for h in all_angle_hints if h[0] != focus_hint]
        _extra = random.sample(_rest, min(num_versions if not _matched else num_versions - 1, len(_rest)))
        angle_hints = (_matched + _extra)[:num_versions]
    else:
        angle_hints = random.sample(all_angle_hints, min(num_versions, len(all_angle_hints)))

    all_versions = []
    _used_time_ranges = []  # 记录已用时间范围，避免版本间重复
    _used_version_notes = []
    _used_version_clips = []

    for vi, (angle_name, angle_hint) in enumerate(angle_hints):
        _log(f"降级方案{vi+1}/{num_versions} [{angle_name}]: AI选片...")
        _extra_hint = ""
        if vi > 0:
            # 版本间去重：告知AI避免使用已选片段
            _extra_hint += f"\n★前面版本已用以下片段，你绝对不能用相同的: "
            _extra_hint += "请从SRT中重新选择不同的条目，确保Hook/Product都不重复★"
            if _used_version_notes:
                _extra_hint += "\n★前面版本内容摘要: " + "；".join(_used_version_notes[-2:]) + "★"

        clips = _call_ai(api_key, base_url, model, cleaned_srt, _log,
                         focus_hint=angle_hint + _extra_hint,
                         srt_entries=srt_entries,
                         hook_candidates_hint=hook_hint if hook_hint else None,
                         multi_version=False,
                         ai_controls=ai_controls,
                         recent_history_hint=_recent_history_hint,
                         main_category=main_category or _detected_main_cat)
        if not clips or len(clips) < 3:
            continue

        # ★降级版本后处理：过滤已用时间范围★
        if _used_time_ranges:
            _before = len(clips)
            clips = [c for c in clips if not any(
                max(c[2], ur[0]) < min(c[3], ur[1]) for ur in _used_time_ranges
            )]
            if len(clips) < _before:
                _log(f"  时间重叠过滤: {_before}→{len(clips)} (去重{_before - len(clips)}个)")
        if len(clips) < 3:
            _log(f"  过滤后不足3段")
            continue
        _fallback_repair_pool = list(clips)

        # 记录这个版本的Hook时间范围
        for c in clips:
            if c[0] == "hook":
                _used_time_ranges.append((c[2], c[3]))
        # 也记录一些Product的时间范围（避免太多重复）
        _product_count = 0
        for c in clips:
            if c[0] == "product":
                _used_time_ranges.append((c[2], c[3]))
                _product_count += 1
                if _product_count >= 4:  # 只记录前4个Product
                    break

        clips = _dedup_clips(
            clips,
            _log,
            multi_version=True,
            focus_hint=focus_hint,
            srt_text=cleaned_srt,
            main_category=main_category or _detected_main_cat,
            preferred_focus=_current_focus_used_label() or angle_name,
            ai_controls=ai_controls,
            merge_mode=True,
        )
        clips = _filter_recent_similar_clips(clips, _used_version_clips, _log, min_keep=4)
        clips = _extract_hook_from_products(clips, cleaned_srt, _log, focus_hint=angle_name, ai_controls=ai_controls)
        clips = _force_short_hook(clips, cleaned_srt, _log, max_hook_sec=_hook_cap_sec, focus_hint=angle_name, ai_controls=ai_controls)
        clips = _refine_hook_by_dynamic_score(clips, cleaned_srt, _log, focus_hint=angle_name, ai_controls=ai_controls)
        if _enforce_category_filter:
            clips = _post_filter_cross_category(clips, cleaned_srt, _log, preferred_cat=main_category or _detected_main_cat)
        if _enforce_time_coherence:
            clips = _check_narrative_coherence(clips, _log)
        clips = [(ct, _apply_asr_corrections(text, _log), s, e, sc, d, focus)
                 for ct, text, s, e, sc, d, focus in clips]
        clips = _filter_host_interaction(clips, _log)
        clips = _filter_price_and_cta(clips, _log)
        clips = _filter_context_damaged_clips(clips, cleaned_srt, _log)
        clips = _filter_celebrity(clips, _log)
        clips = _validate_cta(clips, _log)
        clips = _dedup_clip_text_overlap(clips, _log)
        clips = _cap_clip_duration(clips, _log, srt_text=cleaned_srt)
        clips = _trim_filler_start(clips, cleaned_srt, _log)
        clips = _trim_filler_middle(clips, cleaned_srt, _log)
        clips = _repair_multi_version_structure(
            clips, _fallback_repair_pool, None, None,
            _used_version_clips, _AI_TARGET_DURATION, _log,
            label=f"降级方案{vi+1}"
        )
        clips = _filter_price_and_cta(clips, _log)
        clips = _filter_context_damaged_clips(clips, cleaned_srt, _log)
        # [v9.3 - DISABLED] tighten + 延伸 - 引起片段间跳跃废话
        # from tighten import tighten_clip_boundaries, ensure_sentence_complete, trim_repetitive_filler, trim_tail_filler
        # clips = tighten_clip_boundaries(clips, cleaned_srt, _log)
        # clips = ensure_sentence_complete(clips, cleaned_srt, _log)
        # clips = trim_repetitive_filler(clips, cleaned_srt, _log)
        # clips = trim_tail_filler(clips, cleaned_srt, _log)

        if not clips or len(clips) < 3:
            continue

        total_dur = sum(c[5] for c in clips)
        _log(f"降级方案{vi+1} [{angle_name}]: {len(clips)}片段, {total_dur:.1f}s")
        all_versions.append({"angle": angle_name, "clips": clips})
        _used_version_notes.append(f"版本{vi+1}[{angle_name}]: {_summarize_clips_for_diversity(clips)}")
        _used_version_clips.extend(clips)

        # 记录已用时间范围，进一步版本去重
        for c in clips:
            if c[0] == "close":
                _used_time_ranges.append((c[2], c[3]))

    for _v in all_versions:
        _remember_recent_clips(_history_key, _v.get("clips") or [], _log)

    return {"versions": all_versions}


def _parse_time(t):
    try: return float(t)
    except (ValueError, TypeError): pass
    s = str(t)
    m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)', s)
    if m:
        return int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000.0
    m = re.match(r'(\d+):(\d+):(\d+)$', s)
    if m:
        return int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
    m = re.match(r'(\d+):(\d+)[,.](\d+)', s)
    if m:
        return int(m.group(1))*60 + int(m.group(2)) + int(m.group(3))/1000.0
    m = re.match(r'(\d+):(\d+)$', s)
    if m:
        return int(m.group(1))*60 + int(m.group(2))
    return 0.0


# ============================================================
# 多版本编排（AI二次调用）
# ============================================================
def _compose_version_ai(api_key, base_url, model, raw_clips, srt_text, angle, angle_hint,
                        used_indices, srt_entries, log_fn, vi=0, num_versions=3,
                        hook_candidates_hint=None, target_duration=60, used_version_notes=None,
                        duration_tolerance=None):
    """编排AI调用：根据给定的focus角度，从素材池中选片段并编排成完整叙事方案
    返回: 7元组clips列表，或空列表
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    # 构建素材索引表（给AI看）
    hooks_pool = [c for c in raw_clips if c[0] == "hook"]
    products_pool = [c for c in raw_clips if c[0] == "product"]
    closes_pool = [c for c in raw_clips if c[0] == "close"]

    # 为每个素材构建描述文本
    _material_lines = []
    _material_map = {}  # 索引 -> (clip_type, text, start, end, score, dur, focus)
    for _type, _pool in [("hook", hooks_pool), ("product", products_pool), ("close", closes_pool)]:
        for _i, _c in enumerate(_pool):
            _idx = len(_material_lines)
            _ct, _txt, _s, _e, _sc, _d, _f = _c
            _txt_short = _txt[:30] + ("..." if len(_txt) > 30 else "")
            _srt_idxs = []
            for _j, (_jes, _jee, _) in enumerate(srt_entries, 1):
                if _jes >= _s - 0.1 and _jee <= _e + 0.1:
                    _srt_idxs.append(str(_j))
            _used = "⚠已用于其他版本" if _idx in used_indices else ""
            _material_lines.append(f"  [{_type.upper()}#{_idx}] {_txt_short} ({_d:.0f}s) focus={_f} {_used}")
            _material_map[_idx] = _c

    _material_text = "\n".join(_material_lines)
    _target_min, _target_max = _multi_version_target_bounds(target_duration, duration_tolerance)

    _hook_types_hint = (
        "Hook类型(用不同类型):\n"
        "  ① 痛点提问: '胯宽腿粗还在乱穿？'\n"
        "  ② 效果前置: '原相机上身太显瘦了'\n"
        "  ③ 对比反差: '同样是裙子这款差别太大'\n"
        "  ④ 悬念福利: '别划走！这件我只穿一次'\n"
        "  ⑤ 爆料型: 'XX%是假的'\n"
        "  ⑥ 信任型: '被扣爆了' '卖疯了'\n"
        "  ⑦ 夸奖型: '太好看' '绝绝子'\n"
        "  ⑧ 包容承诺型: '我不管你' '我都给你'\n"
    )
    _used_versions_prompt = ""
    if used_version_notes:
        _used_lines = [str(item) for item in used_version_notes if item]
        if _used_lines:
            _used_versions_prompt = (
                "★前面版本已使用的内容摘要：\n"
                + "\n".join(f"- {line}" for line in _used_lines[-3:])
                + "\n当前版本必须更换Hook文案，并至少更换大部分Product卖点表达；不要只调整顺序。★\n"
            )

    _user_msg = f"""你是短视频编导，从已有素材中为第{vi+1}个版本编排一段完整、流畅的带货短视频。

★当前版本焦点: {angle}★
★编排方向: {angle_hint}★
{f"★避免用{', '.join(used_indices)}索引的素材（已用于其他版本）★" if used_indices else ""}
{_used_versions_prompt}

编排要求:
1. Hook(1个) + Product(4-7个) + Close(1个) = 共6-9个片段，总时长{_target_min}-{_target_max}秒
2. 每个片段严格只用1条素材，不要组合
3. ★片段按叙事逻辑串联: 前2-3个Product围绕{angle}展开，后续Product覆盖其他卖点
4. ★前后片段内容要自然衔接★ 不要出现话题跳转
5. Close用信任强化/尺码引导/场景收尾型，不要用含价格的
6. 优先使用不带"⚠"标记的素材（未用于其他版本）
7. 选完后按最终呈现顺序排列

{_hook_types_hint}

素材列表（{len(raw_clips)}条候选）:
{_material_text}

★输出格式（JSON数组，按最终呈现顺序排列，不能有重复索引）★:
[
  {{"material_idx": 3, "clip_type": "hook", "reason": "开场爆点"}},
  {{"material_idx": 8, "clip_type": "product", "reason": "面料显瘦"}},
  {{"material_idx": 12, "clip_type": "product", "reason": "版型细节"}},
  {{"material_idx": 5, "clip_type": "product", "reason": "上身穿搭"}},
  {{"material_idx": 15, "clip_type": "close", "reason": "尺码信任促单"}}
]

★每个项必须包含: material_idx(对应素材列表的编号), clip_type, reason"""

    # 构造API请求
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是专业短视频编导，擅长编排流畅的带货口播脚本。输出JSON数组，不要包含任何推理过程。"},
            {"role": "user", "content": _user_msg}
        ],
        "temperature": 0.3,
        "top_p": 0.9,
        "max_tokens": 4096,
    }, ensure_ascii=False).encode("utf-8")
    # DeepSeek: 显式关闭思考模式（R1），防止空content
    if "deepseek" in model.lower() and "seed" not in model.lower():
        try:
            _d = json.loads(body)
            _d["thinking"] = {"type": "disabled"}
            body = json.dumps(_d, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass
    if "seed" in model.lower():
        try:
            _d = json.loads(body)
            _d["reasoning_effort"] = "low"
            body = json.dumps(_d, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass

    url = ai_chat_completions_url(base_url)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        ctx = create_ssl_context()
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

        if not content:
            _log(f"  编排AI: 空响应")
            return []

        # 清理markdown代码块
        content = re.sub(r'^```(?:json)?\s*\n?', '', content)
        content = re.sub(r'\n?```\s*$', '', content)
        content = content.replace('```json', '').replace('```', '')
        content = content.strip()

        # 解析JSON。DeepSeek偶尔会截断数组结尾，这里尽量恢复已完整输出的对象。
        try:
            arranged = json.loads(content)
        except json.JSONDecodeError:
            arranged = None
            payload = _extract_json_array_payload(content)
            if payload:
                try:
                    arranged = json.loads(payload)
                except json.JSONDecodeError:
                    arranged = None
            if arranged is None:
                recovered = _recover_json_objects_from_text(content)
                if recovered:
                    arranged = recovered
                    _log(f"  编排AI: JSON不完整，已恢复{len(recovered)}项")
                else:
                    if re.search(r'\[\s*\{', content):
                        _log(f"  编排AI: JSON不完整且无法恢复")
                    else:
                        _log(f"  编排AI: 未找到JSON")
                    return []

        if not isinstance(arranged, list):
            _log(f"  编排AI: 格式错误(非数组)")
            return []

        # 按编排结果重组clips
        result_clips = []
        seen_indices = set()
        for item in arranged:
            midx = item.get("material_idx")
            if midx is None:
                continue
            if midx in seen_indices:
                continue
            seen_indices.add(midx)
            if midx in _material_map:
                result_clips.append(_material_map[midx])

        _log(f"  编排AI: {len(result_clips)}个片段, 原始素材{len(arranged)}项")
        return result_clips

    except urllib.error.HTTPError as e:
        err = ""
        try: err = e.read().decode("utf-8", errors="replace")[:200]
        except Exception: pass
        _log(f"  编排AI HTTP {e.code}: {err[:60]}")
        return []
    except Exception as e:
        _log(f"  编排AI 异常: {str(e)[:60]}")
        return []


# ============================================================
# 去重
# ============================================================
def _dedup_clip_text_overlap(clips, log_fn, merge_mode=False):
    """Remove clips with overlapping source time ranges or highly similar text."""
    def _log(msg):
        if log_fn: log_fn(msg)
    if len(clips) < 2:
        return clips

    # Pass 1: Time range overlap detection
    # Hook must be preserved - adjust other clips' boundaries instead of removing Hook
    removed = set()
    adjusted = {}  # index -> (new_start, new_end)
    for i in range(len(clips)):
        if i in removed:
            continue
        ci_type, ci_text, ci_start, ci_end, ci_score, ci_dur = clips[i][:6]
        ci_s = adjusted.get(i, (ci_start, ci_end))[0]
        ci_e = adjusted.get(i, (ci_start, ci_end))[1]
        for j in range(i + 1, len(clips)):
            if j in removed:
                continue
            cj_type, cj_text, cj_start, cj_end, cj_score, cj_dur = clips[j][:6]
            cj_s = adjusted.get(j, (cj_start, cj_end))[0]
            cj_e = adjusted.get(j, (cj_start, cj_end))[1]

            # [混剪模式] 不同视频的片段不走时间重叠检查
            if merge_mode:
                _ci_v = None
                for _vi in range(10):
                    if f"[V{_vi+1}]" in ci_text:
                        _ci_v = _vi
                        break
                _cj_v = None
                for _vi in range(10):
                    if f"[V{_vi+1}]" in cj_text:
                        _cj_v = _vi
                        break
                if _ci_v is not None and _cj_v is not None and _ci_v != _cj_v:
                    continue  # 不同视频源，不检查时间重叠

            # Calculate overlap
            overlap_start = max(ci_s, cj_s)
            overlap_end = min(ci_e, cj_e)
            overlap_dur = max(0, overlap_end - overlap_start)

            if overlap_dur > 0:
                shorter_dur = min(ci_e - ci_s, cj_e - cj_s)
                if overlap_dur / shorter_dur > 0.5:
                    # Protect Hook: never remove a Hook
                    i_is_hook = ci_type == "hook"
                    j_is_hook = cj_type == "hook"

                    if i_is_hook and not j_is_hook:
                        # Adjust j's start to after hook ends
                        new_j_start = ci_e + 0.1
                        if new_j_start < cj_e - 1.0:
                            adjusted[j] = (new_j_start, cj_e)
                            _log(f"时间重叠: Hook保护，调整片段{j+1} start {cj_s:.1f}→{new_j_start:.1f}s")
                        else:
                            removed.add(j)
                            _log(f"时间重叠: 移除片段{j+1}({cj_s:.1f}-{cj_e:.1f}s, 与Hook重叠且调整后过短)")
                    elif j_is_hook and not i_is_hook:
                        # Adjust i's start to after hook ends
                        new_i_start = cj_e + 0.1
                        if new_i_start < ci_e - 1.0:
                            adjusted[i] = (new_i_start, ci_e)
                            _log(f"时间重叠: Hook保护，调整片段{i+1} start {ci_s:.1f}→{new_i_start:.1f}s")
                        else:
                            removed.add(i)
                            _log(f"时间重叠: 移除片段{i+1}({ci_s:.1f}-{ci_e:.1f}s, 与Hook重叠且调整后过短)")
                            break
                    elif i_is_hook and j_is_hook:
                        # Two hooks overlapping - remove the shorter one
                        if ci_dur <= cj_dur:
                            removed.add(i)
                            _log(f"时间重叠: 移除Hook片段{i+1}(与Hook片段{j+1}重叠)")
                            break
                        else:
                            removed.add(j)
                            _log(f"时间重叠: 移除Hook片段{j+1}(与Hook片段{i+1}重叠)")
                    else:
                        # Neither is Hook: remove the shorter one (original logic)
                        if ci_dur <= cj_dur:
                            removed.add(i)
                            _log(f"时间重叠: 移除片段{i+1}({ci_s:.1f}-{ci_e:.1f}s, 被片段{j+1}({cj_s:.1f}-{cj_e:.1f}s)包含)")
                            break
                        else:
                            removed.add(j)
                            _log(f"时间重叠: 移除片段{j+1}({cj_s:.1f}-{cj_e:.1f}s, 被片段{i+1}({ci_s:.1f}-{ci_e:.1f}s)包含)")

    # Apply adjustments
    if adjusted:
        new_clips = []
        for idx, c in enumerate(clips):
            if idx in removed:
                continue
            if idx in adjusted:
                ns, ne = adjusted[idx]
                ct, text, old_s, old_e, sc, old_d, *rest = c
                new_d = ne - ns
                new_clips.append((ct, text, ns, ne, sc, new_d, *rest))
            else:
                new_clips.append(c)
        clips = new_clips
    elif removed:
        clips = [c for i, c in enumerate(clips) if i not in removed]
    else:
        pass  # no changes needed

    # Pass 2: Text similarity (original logic)
    for i in range(len(clips)):
        if i in removed:
            continue
        ci_type, _, ci_start, ci_end, _, _ = clips[i][:6]
        ci_text = clips[i][1]
        ci_chars = set(re.sub(r"[\s\W]+", "", ci_text))
        if not ci_chars:
            continue
        for j in range(i + 1, len(clips)):
            if j in removed:
                continue
            cj_type, _, cj_start, cj_end, _, _ = clips[j][:6]
            cj_text = clips[j][1]
            cj_chars = set(re.sub(r"[\s\W]+", "", cj_text))
            if not cj_chars:
                continue
            overlap = len(ci_chars & cj_chars) / max(len(ci_chars | cj_chars), 1)
            if overlap > 0.55:
                ci_is_hook = _is_hook_clip(clips[i])
                cj_is_hook = _is_hook_clip(clips[j])
                if ci_is_hook != cj_is_hook:
                    time_overlap = max(0.0, min(float(ci_end), float(cj_end)) - max(float(ci_start), float(cj_start)))
                    if time_overlap <= 0.2:
                        continue
                    drop_idx = j if ci_is_hook else i
                    removed.add(drop_idx)
                    _log(f"内容重复: Hook保护，移除片段{drop_idx+1}(与Hook文案/时间重复, 重叠{overlap:.0%})")
                    if drop_idx == i:
                        break
                    continue
                if len(cj_text) <= len(ci_text):
                    removed.add(j)
                    _log(f"内容重复: 移除片段{j+1}(与片段{i+1}重复, 重叠{overlap:.0%})")
                else:
                    removed.add(i)
                    _log(f"内容重复: 移除片段{i+1}(与片段{j+1}重复, 重叠{overlap:.0%})")
                    break

    if not removed:
        return clips
    result = [c for idx, c in enumerate(clips) if idx not in removed]
    _log(f"去重: {len(clips)} -> {len(result)} 片段")
    return result



def _extract_hook_from_products(clips, srt_text, log_fn=None, focus_hint=None, ai_controls=None):
    """从Product片段中提取含爆点词的部分作为独立Hook（原句保留不动）"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips or not srt_text:
        return clips

    _kw_data_ext = load_keywords()
    _hook_kw = _kw_data_ext["hook_keywords"]
    _pref_kws = _hook_preference_keywords(focus_hint, ai_controls)
    _explicit_pref = _has_explicit_hook_preference(focus_hint, ai_controls)

    # 解析SRT条目
    _srt_entries = []
    for block in srt_text.strip().split(chr(10) + chr(10)):
        bl = block.strip().split(chr(10))
        time_line_idx = 0 if len(bl) >= 1 and '-->' in bl[0] else (1 if len(bl) >= 2 and '-->' in bl[1] else -1)
        if time_line_idx >= 0:
            try:
                parts = bl[time_line_idx].split('-->')
                h1, m1, s1 = parts[0].strip().replace(',', '.').split(':')
                h2, m2, s2 = parts[1].strip().replace(',', '.').split(':')
                es = int(h1)*3600 + int(m1)*60 + float(s1)
                ee = int(h2)*3600 + int(m2)*60 + float(s2)
                txt = ' '.join(bl[time_line_idx + 1:]).strip()
                _srt_entries.append((es, ee, txt))
            except:
                pass

    if not _srt_entries:
        return clips

    # 检查现有Hook质量
    _existing_hook = None
    _existing_hook_good = False
    for ci, clip in enumerate(clips):
        if clip[0] == "hook":
            _existing_hook = ci
            _hook_score, _hook_reasons = _final_hook_quality_score(
                clip[1], _clip_duration_value(clip), _hook_kw, focus_hint, ai_controls
            )
            _existing_hook_good = _hook_score >= 20.0
            break

    if _existing_hook_good and (not _explicit_pref or _hook_matches_preference(clips[_existing_hook][1], focus_hint, ai_controls)):
        _log("Hook提取: 现有Hook已过质量门槛，保留")
        return clips
    if _existing_hook_good and _explicit_pref:
        _log(f"Hook提取: 现有Hook有爆点但不匹配偏好'{str(focus_hint)[:8]}'，继续寻找")

    # 扫描所有片段（包括Product和烂Hook），找含爆点词的SRT条目
    hook_candidates = []  # (srt_start, srt_end, srt_txt, duration, score, source_clip_idx)

    for ci, clip in enumerate(clips):
        if clip[0] not in ("product", "highlight", "hook"):
            continue
        clip_start = clip[2]
        clip_end = clip[3]

        for es, ee, txt in _srt_entries:
            # SRT条目在clip范围内
            if es >= clip_start - 0.5 and ee <= clip_end + 0.5:
                dur = ee - es
                if dur < 0.8 or dur > 5.0 or _is_bad_hook_candidate_text(txt):
                    continue
                score, reasons = _final_hook_quality_score(txt, dur, _hook_kw, focus_hint, ai_controls)
                if score < 20.0:
                    continue
                pref_hits = _hook_pref_score(txt, focus_hint, ai_controls)
                if _explicit_pref and pref_hits:
                    score += 6.0
                hook_candidates.append((es, ee, txt, dur, score, ci))

    if not hook_candidates:
        _log("Hook提取: 未找到含爆点词的SRT条目")
        return clips

    picked = _pick_diverse_hook_candidate(hook_candidates, _log)
    hook_start, hook_end, hook_txt, hook_dur, _, source_ci = picked
    hook_dur = hook_end - hook_start

    # 如果现有Hook是烂的，降为Product
    if _existing_hook is not None:
        old = clips[_existing_hook]
        clips[_existing_hook] = ("product", old[1], old[2], old[3], old[4], old[5],
                                  old[6] if len(old) > 6 else "")
        _log(f"Hook降级: 烂Hook '{old[1][:20]}...' → Product")

    # 创建新Hook（原Product保留不动，允许爆点词重复）
    new_hook = ("hook", hook_txt, hook_start, hook_end, 9.0, hook_dur, "爆点提取")
    clips.insert(0, new_hook)
    _log(f"Hook提取: 从片段中裁出 '{hook_txt[:20]}' ({hook_dur:.1f}s) → Hook (原句保留)")

    return clips

def _dedup_clips(clips, log_fn, multi_version=False, focus_hint=None, srt_text=None, main_category=None, preferred_focus=None, ai_controls=None, merge_mode=False):

    def _log(msg):
        if log_fn: log_fn(msg)
    if not clips:
        return clips
    original = len(clips)
    _log(f"AI: 解析到 {original} 个片段，开始去重...")
    no_overlap = []
    # === Forbidden phrase filter (from GUI keyword management) ===
    try:
        _fb_list = load_keywords().get("forbidden_phrases", [])
        if _fb_list:
            _before = len(clips)
            _keep = []
            for _clip in clips:
                _txt = _clip[1] if len(_clip) > 1 else ""
                _txt_variants = _safety_text_variants(_txt)
                _matched = []
                for w in _fb_list:
                    _w = str(w or "").strip()
                    if _w and _safety_word_matches(_w, _txt_variants):
                        _matched.append(_w)
                if _matched:
                    _log(f"  forbid: [{','.join(_matched[:3])}] CT={_clip[0]} [{_clip[2]:.0f}s-{_clip[3]:.0f}s]")
                else:
                    _keep.append(_clip)
            clips = _keep
            if len(clips) < _before:
                _log(f"forbidden filter: skipped {_before - len(clips)}/{_before} clips")
    except Exception as _fe:
        _log(f"forbidden filter error: {_fe}")
    
    for clip in clips:
        ct, text, start, end, score, dur = clip[:6]
        overlap = False
        for ex in no_overlap:
            ov_s, ov_e = max(start, ex[2]), min(end, ex[3])
            if ov_e > ov_s and (ov_e - ov_s) / dur > 0.5:
                overlap = True; break
        if not overlap:
            no_overlap.append(clip)
    clips = no_overlap
    # 增强去重: 全局已选内容比较
    STOP_CHARS = set("的了是得很都也就在有被把给到和不还而与人这那她又他它们会要能让得去上下来过对说没好几什么怎这么一个自己我们你们他们")
    def extract_keys(text):
        chars = set(c for c in text if c not in STOP_CHARS and c.strip())
        bigrams = set(text[i:i+2] for i in range(len(text)-1)
                     if text[i] not in STOP_CHARS and text[i+1] not in STOP_CHARS)
        return chars, bigrams

    no_similar = []
    seen_chars = set()
    seen_bigrams = set()
    seen_texts = []
    for clip in clips:
        ct, text, start, end, score, dur = clip[:6]
        chars, bigrams = extract_keys(text)
        is_dup = False
        # 短句高度重复检查
        if chars and seen_chars and len(chars) < 8:
            if len(chars & seen_chars) / len(chars) > 0.8:
                is_dup = True
        # 逐条语义比较
        if not is_dup:
            for pt in seen_texts:
                pc, pb = extract_keys(pt)
                if bigrams and pb:
                    bo = len(bigrams & pb)
                    bu = len(bigrams | pb)
                    if bu > 0 and bo / bu > 0.7:
                        is_dup = True; break
                if chars and pc:
                    co = len(chars & pc)
                    cu = len(chars | pc)
                    if cu > 0 and co / cu > 0.75:
                        is_dup = True; break
        if not is_dup:
            no_similar.append(clip)
            seen_chars |= chars
            seen_bigrams |= bigrams
            seen_texts.append(text)
        else:
            _log(f"  去重移除: {text[:30]}...")
    clips = no_similar
    # 不排序，保持 AI 原始输出顺序（AI prompt 已要求叙事编排）

    # 报尺码的片段移到末尾(扩展关键词覆盖Whisper各种转录形式)
    size_keywords = [
        # 尺码标识
        "S码", "M码", "L码", "XL", "XXL", "3XL", "4XL", "尺码", "码数",
        "均码", "大码", "小码", "加肥", "加大", "宽松版",
        # 体重段
        "80斤", "90斤", "100斤", "110斤", "120斤", "130斤", "140斤", "150斤", "160斤",
        "80到120", "90到130", "100到140", "110到150", "120到160", "80-120", "90-130",
        "80至120", "90至130", "100至140", "110至150", "120至160",
        "八十", "九十", "一百", "一百一", "一百二", "一百三", "一百四", "一百五",
        # 身高段
        "身高", "体重", "cm", "一米五", "一米六", "一米七", "155", "160", "165", "170",
        # 穿搭建议
        "穿什么码", "选什么码", "拍什么码", "入什么码", "报一下", "报尺码",
        "尺码表", "码型", "偏大", "偏小", "正常码", "码数",
        # Whisper可能的转录
        "码子", "码", "斤", "公斤",
    ]
    # 单字词太容易误匹配，只检查长度≥2的词
    size_keywords = [kw for kw in size_keywords if len(kw) >= 2]

    size_clips = [c for c in clips if any(kw in c[1] for kw in size_keywords)]
    other_clips = [c for c in clips if c not in size_clips]
    # [v9.2] 尺码不去重：让价格过滤器删含价格的，文本去重删重复的，不含价格的尺码片段保留当close
    if size_clips:
        clips = other_clips + size_clips
        _log(f"尺码后置: {len(size_clips)} 个尺码片段移到末尾")

    # 额外保护:hook位置(前2个)禁止尺码内容，和后面非尺码片段交换
    for i in range(min(2, len(clips))):
        c = clips[i]
        if any(kw in c[1] for kw in size_keywords):
            for j in range(i + 1, len(clips)):
                if not any(kw in clips[j][1] for kw in size_keywords):
                    clips[i], clips[j] = clips[j], clips[i]
                    _log(f"尺码保护: 位置{i+1}的尺码片段与位置{j+1}交换")
                    break

    # [v9.2] 价格/购物车片段直接排除(用户要求成品不报价格)
    price_keywords = [
        # 硬价格词（代码层过滤）
        "折后", "到手", "价格", "多少钱", "优惠", "限时价", "到手价", "开价",
        "购物车", "链接", "上车", "下单",
        "原价", "现价",
        "小黄车", "左下角", "直播间", "小车",
        "挂车", "加购", "拼单", "福利", "赠品", "包邮",
        "满减", "专区", "特价", "截止",
        # 注意："拍"误杀尺码推荐(卡码往大拍)，"抢"误杀互动(抢人了)，
        # "限时"太宽泛，"划算/性价比"含主观判断——这些由Prompt层控制
    ]
    price_clips = [c for c in clips if any(kw in c[1] for kw in price_keywords)]
    if price_clips:
        clips = [c for c in clips if c not in price_clips]
        _log(f"价格排除: 删除 {len(price_clips)} 个价格片段, 剩余 {len(clips)} 片")

    # [v9.2] 纯语气词片段排除(“对”“呞”“呃”“啊”等无实质内容)
    filler_patterns = [
        "对", "呞", "呃", "啊", "噢", "哼", "嗯", "啦",
        "哈", "啼", "嘿", "哈哈", "啦啦",
        "是的", "的啊", "的呢",
    ]
    filler_clips = []
    for c in clips:
        text_clean = re.sub(r'[\s\W]+', '', c[1]).lower()
        if len(text_clean) <= 3 and any(text_clean == p or text_clean == p+p for p in filler_patterns):
            filler_clips.append(c)
        elif len(text_clean) <= 5:
            # 检查是否全部由语气词组成
            chars = set(text_clean)
            filler_chars = set('对呞呃啊噢哼嗯啦哈啼嘿是的啊呢呢啦')
            if chars <= filler_chars:
                filler_clips.append(c)
    if filler_clips:
        clips = [c for c in clips if c not in filler_clips]
        _log(f"语气词排除: 删除 {len(filler_clips)} 个纯语气词片段, 剩余 {len(clips)} 片")

    # [DISABLED] close位置交换打乱AI叙事顺序，由强制排序保证close在末尾
    # for i in range(min(3, len(clips))):
    #     if clips[i][0] == "close":
    #         for j in range(i + 1, len(clips)):
    #             if clips[j][0] != "close":
    #                 clips[i], clips[j] = clips[j], clips[i]
    #                 _log(f"close保护: 位置{i+1}的close片段与位置{j+1}交换")
    #                 break

    # Hook首位保护: 第一个片段必须是hook类型
    if clips and clips[0][0] != "hook":
        for j in range(1, len(clips)):
            if clips[j][0] == "hook":
                clips[0], clips[j] = clips[j], clips[0]
                _log(f"Hook首位保护: 位置1的非hook片段与位置{j+1}的hook交换")
                break
        else:
            # 无Hook片段：提拔第一个Product为Hook
            if clips and clips[0][0] in ("product", "highlight"):
                _old = clips[0]
                _hook_kw = load_keywords().get("hook_keywords", [])
                _hook_score, _hook_reasons = _final_hook_quality_score(
                    _old[1], _clip_duration_value(_old), _hook_kw, focus_hint, None
                )
                if _hook_score >= 20.0:
                    clips[0] = ("hook", _old[1], _old[2], _old[3], _old[4], _old[5], *_old[6:])
                    _log(f"Hook提拔: 首位Product '{_old[1][:20]}...' → Hook ({_hook_score:.1f}/{','.join(_hook_reasons[:2])})")
                else:
                    _log(f"Hook提拔: 首位Product未过质量门槛({_hook_score:.1f})，保留自然开场")

    # [DISABLED] 品类后置打乱AI叙事顺序，由AI Prompt控制品类排列
    # clips = _enforce_product_coherence(clips, log_fn)

    # 终剪前最后防线:移除孤立跨品类片段
    clips = _remove_orphan_cross_category(clips, log_fn)

    # [v9.0] 面料不再强制后置,由AI Prompt控制同主题相邻排列
    # 原面料后置逻辑会打断叙事流(讲面料→突然插入尺码→又讲面料),已移除


    if len(clips) < original:
        _log(f"去重: {original} -> {len(clips)}")
    # 卖点聚焦排序
    focus_counts = {}
    for clip in clips:
        fp = _detect_focus_point(clip[1])
        focus_counts[fp] = focus_counts.get(fp, 0) + 1
    if focus_counts:
        sorted_foci = sorted(focus_counts.items(), key=lambda x: -x[1])
        primary = sorted_foci[0][0]
        secondary = sorted_foci[1][0] if len(sorted_foci) > 1 else None
        _log(f"卖点聚焦: 主={primary}({focus_counts[primary]}段) 次={secondary}")

        # 面料占比警告（>40%时警告，不删除）
        fabric_count = focus_counts.get("面料", 0)
        total_foci = sum(focus_counts.values())
        if total_foci > 0 and fabric_count / total_foci > 0.4:
            _log(f"\u26a0\ufe0f 面料占比{fabric_count}/{total_foci}超过40%，建议降低面料比例")

        # 偏好匹配校验：Hook是否匹配指定偏好
        if focus_hint and clips:
            hook_clips = [c for c in clips if c[0] == "hook"]
            if hook_clips:
                hook_text = hook_clips[0][1] if len(hook_clips[0]) > 1 else ""
                _pref_kws = {
                    "版型显瘦": ["显瘦", "遮肉", "版型", "修饰", "不挑", "宽松", "修身"],
                    "颜色氛围": ["颜色", "显白", "提气色", "色系", "色调"],
                    "场景搭配": ["通勤", "约会", "日常", "防晒", "穿搭", "搭配"],
                    "性价比": ["划算", "超值", "品质", "对比", "值得"],
                    "情绪感染": ["绝了", "太好", "真的", "超"],
                    "流行趋势": ["流行", "设计感", "当季", "趋势"],
                    "面料质感": ["面料", "材质", "手感", "质感", "桑蚕丝"],
                    "口感食欲": ["好吃", "口感", "试吃", "爆汁", "脆甜", "鲜嫩", "软糯", "Q弹"],
                    "新鲜品质": ["新鲜", "鲜活", "现摘", "现采", "当天发", "品质", "饱满"],
                    "产地溯源": ["产地", "源头", "基地", "果园", "直采", "溯源"],
                    "规格分量": ["规格", "净重", "斤装", "箱装", "个头", "果径"],
                    "发货保鲜": ["发货", "冷链", "保鲜", "锁鲜", "坏果包赔"],
                    "场景吃法": ["早餐", "夜宵", "办公室", "全家", "火锅", "即食", "囤货"],
                }
                _matched_kws = []
                for _pref, _kws in _pref_kws.items():
                    if _pref[:2] in str(focus_hint) or str(focus_hint)[:2] in _pref:
                        _matched_kws = _kws
                        break
                if _matched_kws and not any(kw in hook_text for kw in _matched_kws):
                    _log(f"\u26a0\ufe0f Hook不匹配偏好'{str(focus_hint)[:6]}'，尝试代码层修正...")
                    # 从已选Product中找匹配偏好的提拔为Hook
                    _pref_matched_idx = None
                    for _pi, _pc in enumerate(clips):
                        if _pc[0] == "product" and len(_pc) > 1:
                            _ptxt = _pc[1]
                            if any(kw in _ptxt for kw in _matched_kws):
                                _pref_matched_idx = _pi
                                break
                    if _pref_matched_idx is not None:
                        _old_hook = clips[0]
                        _new_hook = clips[_pref_matched_idx]
                        # 把匹配的Product改为Hook，放到首位
                        _new_hook = ("hook", _new_hook[1], _new_hook[2], _new_hook[3],
                                     _new_hook[4], _new_hook[5], *_new_hook[6:])
                        _old_hook = ("product", _old_hook[1], _old_hook[2], _old_hook[3],
                                     _old_hook[4], _old_hook[5], *_old_hook[6:])
                        clips[0] = _new_hook
                        clips[_pref_matched_idx] = _old_hook
                        _log(f"✅ 偏好修正: Product'{_new_hook[1][:20]}...' → Hook，原Hook降为Product")
                    else:
                        # 已选片段中没有匹配的，从SRT中找
                        _log(f"已选片段中无匹配偏好的内容，Hook保持不变")

        clips = _reorder_product_focus_blocks(
            clips,
            log_fn,
            preferred_cat=main_category,
            preferred_focus=preferred_focus or _current_focus_used_label() or focus_hint,
            ai_controls=ai_controls,
            merge_mode=merge_mode,
        )

    # 删除过短片段（<2s且非Hook的内容不完整，但Hook可以很短）
    _short = [c for c in clips if len(c) > 3 and (c[3] - c[2]) < 2 and c[0] != "hook"]
    if _short:
        clips = [c for c in clips if not (len(c) > 3 and (c[3] - c[2]) < 2 and c[0] != "hook")]
        _log(f"过短片段过滤: 删除{len(_short)}段<2s的非Hook片段")

    return clips


def _detect_focus_point(text):
    RULES = [
        ("口感食欲", ["好吃","鲜甜","脆甜","爆汁","多汁","汁水","入口","口感","肉质","鲜嫩","软糯","酥脆","Q弹","弹牙","拉丝","试吃","咬一口","吃起来"]),
        ("新鲜品质", ["新鲜","鲜活","现摘","现采","现捕","现捞","当天发","鲜度","品质","果形","果径","个头","饱满","坏果","坏果包赔","源头","基地","果园"]),
        ("产地溯源", ["产地","原产地","源头","基地","果园","农场","牧场","渔港","海捕","直采","直发","溯源","农户","合作社","产区","当季","应季"]),
        ("规格分量", ["规格","净含量","净重","克重","重量","斤装","箱装","袋装","盒装","整箱","大果","中果","果径","个头","份量","分量"]),
        ("发货保鲜", ["发货","现发","冷链","冰袋","保温箱","泡沫箱","顺丰","次日达","保鲜","锁鲜","冷冻","速冻","冷藏","售后","坏果包赔","破损包赔"]),
        ("场景吃法", ["早餐","夜宵","下午茶","办公室","孩子","老人","全家","聚餐","火锅","烧烤","煲汤","下饭","拌饭","空气炸锅","即食","开袋即食","囤货","冰箱","送礼"]),
        ("版型", ["版型","廓形","剪裁","袖型","领型","宽松","修身","收腰","直筒","微喇","落肩","短款","长款","箱型",
                  "高腰","中腰","低腰","A字","包臀","开叉","大摆","灯笼袖","泡泡袖","垫肩","阔腿","小脚","九分",
                  "高领","V领","圆领","方领","一字肩","露肩","抓绳","杆腰",
                  "显瘦","显高","显腿长","比例","曼妙","修饰"]),
        ("面料", ["面料","材质","手感","触感","起球","克重","纱线","针织","棉麻","真丝","垂感","弹力","透气","柔软","蓬松","网纱",
                  "莱赛尔","天丝","冰丝","雪纺","纯棉","亚麻","锦纶","涤纶","缎面","丝绒","灯芯绒","牛仔",
                  "垂坠","亲肤","凉感","吸汗","不闷","不透","厚实","薄款","加厚","夹棉","抓绒",
                  "胸垫","垫肩","内衬","里布","提花","刺绣","铻绣","压钻","重工"]),
        ("颜色", ["颜色","色系","复古","条纹","碎花","纯色","拼色","渐变","军绿","咖色","黑色","白色","花色","撞色",
                  "显白","不挑人","不挑肤色","黄皮","提亮","显气色","高级色","莫兰迪","燕麦色","奶白色"]),
        ("场景", ["通勤","约会","度假","日常","职场","上学","出门","旅游","年会","聚会","居家","运动","健身","瑜伽",
                  "拍照","逛街","户外","婚礼","相亲","见家长","面试"]),
        ("搭配", ["搭配","套穿","叠穿","外套","西装","组合","成套","同款",
                  "配什么","搭什么","内搭","外穿","打底","单穿","腰带","配饰"]),
        ("品质", ["做工","走线","细节","质感","高级感","精致","工艺","品质","缝合",
                  "大牌","专柜","原单","高定","免烫","不起球","不褪色","不变形"]),
        ("价格_禁止", ["价格","到手价","优惠","划算","超值","折扣","领券","立减","性价比",
                  "秒杀","福利","特价","骨折价","白菜价","闭眼入","手慢无","抢瘆了",
                  "拍一发二","多拍","囤货"]),
    ]
    for focus, kws in RULES:
        for kw in kws:
            if kw in text:
                return focus
    return "其他"


def _clip_focus_block(clip):
    """Return a coarse narrative block for product clips."""
    text = str(clip[1] if len(clip) > 1 else "")
    focus = str(clip[6] if len(clip) > 6 else "")
    hay = focus + " " + text

    for block in CATEGORY_FOCUS_ORDER.get("食品/生鲜", []):
        if block != "其他" and block in focus:
            return block
    for block in DEFAULT_BLOCK_ORDER:
        if block != "其他" and block in focus:
            return block

    if any(k in hay for k in ("好吃", "鲜甜", "脆甜", "爆汁", "多汁", "汁水", "入口", "口感", "鲜嫩", "软糯", "酥脆", "Q弹", "弹牙", "拉丝", "试吃", "咬一口", "吃起来")):
        return "口感食欲"
    if any(k in hay for k in ("新鲜", "鲜活", "现摘", "现采", "现捕", "现捞", "当天发", "鲜度", "果形", "果径", "个头", "饱满", "坏果包赔", "源头", "基地", "果园")):
        return "新鲜品质"
    if any(k in hay for k in ("产地", "原产地", "源头", "基地", "果园", "农场", "牧场", "渔港", "海捕", "直采", "直发", "溯源", "农户", "合作社", "产区", "当季", "应季")):
        return "产地溯源"
    if any(k in hay for k in ("规格", "净含量", "净重", "克重", "重量", "斤装", "箱装", "袋装", "盒装", "整箱", "大果", "中果", "果径", "个头", "份量", "分量")):
        return "规格分量"
    if any(k in hay for k in ("发货", "现发", "冷链", "冰袋", "保温箱", "泡沫箱", "顺丰", "次日达", "保鲜", "锁鲜", "冷冻", "速冻", "冷藏", "坏果包赔", "破损包赔")):
        return "发货保鲜"
    if any(k in hay for k in ("早餐", "夜宵", "下午茶", "办公室", "孩子", "老人", "全家", "聚餐", "火锅", "烧烤", "煲汤", "下饭", "拌饭", "空气炸锅", "即食", "开袋即食", "囤货", "冰箱", "送礼")):
        return "场景吃法"
    if any(k in hay for k in ("显瘦", "遮肉", "藏肉", "收腰", "显高", "比例", "修饰", "版型", "廓形", "剪裁", "宽松", "修身", "帽型", "帽子", "翻领", "拉链", "肩型", "盖臀")):
        return "版型显瘦"
    if any(k in hay for k in ("面料", "材质", "手感", "触感", "垂感", "透气", "亲肤", "柔软", "针织", "冰丝", "真丝", "棉麻", "不闷", "不透")):
        return "面料质感"
    if any(k in hay for k in ("做工", "工艺", "细节", "品质", "质感", "高级感", "精致", "走线", "刺绣", "蕾丝", "重工")):
        return "品质细节"
    if any(k in hay for k in ("颜色", "色系", "显白", "提气色", "复古", "黑色", "白色", "咖色", "花色", "撞色")):
        return "颜色氛围"
    if any(k in hay for k in ("场景", "通勤", "约会", "日常", "职场", "出门", "度假", "拍照", "出片", "逛街", "旅游", "搭配", "叠穿", "内搭", "外穿", "成套", "套穿")):
        return "场景搭配"
    if any(k in hay for k in ("舒适", "不勒", "自在", "轻盈", "无感", "不紧绷", "活动方便", "不束缚", "不扎人", "不闷", "不热", "轻薄", "凉爽", "温暖", "贴身", "有余量", "不卡", "不掉", "不卷边")):
        return "穿着体验"
    if any(k in hay for k in ("划算", "超值", "性价比", "物超所值", "大牌平替", "代工厂", "源头", "出厂价", "这个价", "值得", "不贵", "闭眼冲", "不踩坑", "同品质", "百元", "几十块")):
        return "性价比"
    if any(k in hay for k in ("绝了", "太漂亮", "太好看", "美爆", "太爱", "神仙", "封神", "超级", "天呐", "妈呀", "信我", "相信我", "真心", "自留", "美哭", "太绝了", "疯了吧")):
        return "情绪感染"
    if any(k in hay for k in ("流行", "当季", "新款", "原创", "不撞款", "爆款", "热门", "趋势", "法式", "新中式", "设计师", "小众", "轻奢", "时髦", "松弛感", "氛围感", "美拉德", "多巴胺", "复古", "国风")):
        return "流行趋势"
    if any(k in hay for k in ("限量", "限时", "手慢无", "秒空", "断码", "断货", "补不到", "不补货", "最后", "抢", "赶紧", "抓紧", "错过", "下架", "余量", "稀缺", "卖完")):
        return "紧迫稀缺"
    if any(k in hay for k in ("裙长", "到脚踝", "露脚踝", "遮小腿", "小腿肚", "膝盖", "大腿", "长度", "衣长", "袖长", "盖住", "刚好", "不过膝", "过膝", "九分", "七分", "短款", "中长款", "拖地", "盖脚面")):
        return "尺寸长度"
    if any(k in hay for k in ("工艺", "成本", "走线", "拼接", "剪裁", "立体", "定型", "压褶", "蕾丝边", "包边", "锁边", "加固", "五金", "拉链", "扣子", "口袋", "里衬", "定染", "固色")):
        return "工艺细节"
    if any(k in hay for k in ("买不到", "外面没有", "不一样", "区别", "独特", "独家", "外面买", "比外面", "比市面", "同价位", "同品质", "值这个价", "没有第二家", "一手", "商场同款")):
        return "对比优势"
    return "其他"


def _known_focus_blocks():
    blocks = []
    orders = []
    category_orders = globals().get("CATEGORY_FOCUS_ORDER") or {}
    if isinstance(category_orders, dict):
        orders.extend(category_orders.values())
    orders.append(globals().get("DEFAULT_BLOCK_ORDER") or [])
    for order in orders:
        for block in order or []:
            if block and block not in blocks:
                blocks.append(block)
    return blocks


def _focus_label_to_block(label):
    text = _normalize_focus_label(label)
    text = str(text or "").strip()
    if not text or text in {"自动", "默认", "随机偏好", "兜底偏好", "全量选片", "通用卖点", "其他"}:
        return ""

    for block in _known_focus_blocks():
        if block == "其他":
            continue
        if text == block or block in text or text in block:
            return block

    block = _clip_focus_block(("product", text, 0, 0, 0, 0, text))
    return "" if block == "其他" else block


SALES_ROLE_LABELS = {
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


def _clip_sales_text(clip):
    try:
        text = str(clip[1] if len(clip) > 1 else "")
    except Exception:
        text = str(clip or "")
    return re.sub(r"\[[vV]\d+\]\s*", "", text).strip()


def _clip_sales_compact_text(clip):
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", _clip_sales_text(clip)).lower()


def _clip_boundary_quality_flags(clip):
    text = _clip_sales_text(clip).strip()
    compact = _clip_sales_compact_text(clip)
    flags = []
    if not compact:
        return ["空文案"]
    weak_starts = (
        "嗯", "啊", "呃", "额", "好", "好的", "是的", "对", "对的",
        "然后", "而且", "但是", "不过", "所以", "其实", "就是", "那",
        "这个的话", "那个的话", "像这种", "这种的话", "它的话",
    )
    weak_ends = (
        "然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实",
        "你会觉得", "就感觉", "就发现", "你就会", "你会看到",
        "的话", "对不对", "是不是", "能理解吗", "有没有", "有没有发现",
        "这个", "这款", "这件", "一件", "一条", "一套", "一个",
        "呢", "吧", "啊", "呀", "哈", "嘛",
    )
    if compact in {"嗯", "啊", "好", "好的", "是的", "对", "对的"}:
        flags.append("弱短句")
    if any(compact.startswith(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", w).lower()) for w in weak_starts):
        flags.append("弱开头")
    stripped = text.rstrip("，,。.!！?？、；;：: ")
    if any(stripped.endswith(w) for w in weak_ends):
        flags.append("弱结尾")
    try:
        dur = _clip_duration_value(clip)
        if dur < 1.8 and not _is_hook_clip(clip):
            flags.append("过短")
    except Exception:
        pass
    return list(dict.fromkeys(flags))


def _sales_goal_role_weights(ai_controls=None):
    controls = _normalize_ai_controls(ai_controls)
    goal = controls.get("goal", "")
    weights = {}
    if goal == "爆款种草":
        weights.update({"direct_effect": 2.0, "scene_crowd": 1.0, "proof_detail": 0.5})
    elif goal == "专业讲解":
        weights.update({"proof_detail": 2.5, "objection_resolver": 1.0})
    elif goal == "显瘦转化":
        weights.update({"direct_effect": 2.5, "objection_resolver": 1.5})
    elif goal == "质感高级":
        weights.update({"proof_detail": 2.0, "scene_crowd": 1.0})
    elif goal == "快速促单":
        weights.update({"objection_resolver": 2.0, "natural_close": 1.5, "proof_detail": 0.5})
    elif goal == "食欲种草":
        weights.update({"direct_effect": 2.0, "proof_detail": 1.0, "scene_crowd": 0.5})
    elif goal in {"新鲜转化", "囤货转化"}:
        weights.update({"proof_detail": 1.5, "objection_resolver": 1.5, "scene_crowd": 1.0})
    return weights


def _focus_block_sales_role(block):
    block = str(block or "").strip()
    if block in {"版型显瘦", "穿着体验", "口感食欲"}:
        return "direct_effect"
    if block in {"面料质感", "品质细节", "工艺细节", "新鲜品质", "产地溯源", "规格分量", "发货保鲜"}:
        return "proof_detail"
    if block in {"场景搭配", "场景吃法", "流行趋势"}:
        return "scene_crowd"
    if block in {"尺寸长度", "对比优势"}:
        return "objection_resolver"
    return ""


def _clip_sales_role_scores(clip, hook_text="", preferred_focus="", ai_controls=None, main_category=None):
    text = _clip_sales_text(clip)
    compact = _clip_sales_compact_text(clip)
    focus = str(clip[6] if len(clip) > 6 else "")
    block = _clip_focus_block(clip)
    preferred_block = _focus_label_to_block(preferred_focus)
    controls = _normalize_ai_controls(ai_controls)
    selling_blocks = [_focus_label_to_block(item) for item in controls.get("selling_points", [])]
    selling_blocks = [item for item in selling_blocks if item]
    scores = {
        "hook": 0.0,
        "hook_followup": 0.0,
        "direct_effect": 0.0,
        "proof_detail": 0.0,
        "scene_crowd": 0.0,
        "objection_resolver": 0.0,
        "natural_close": 0.0,
        "weak_fragment": 0.0,
        "other": 1.0,
    }

    if _is_hook_clip(clip):
        scores["hook"] = 100.0
    if _is_close_clip(clip):
        scores["natural_close"] += 45.0

    if hook_text:
        hook_compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", str(hook_text or "")).lower()
        sim = _clip_text_similarity_value(hook_text, text)
        promise_terms = (
            "显白", "显瘦", "遮肉", "藏肉", "显高", "显腿长", "上身", "效果",
            "绿色", "白色", "黑色", "藏青", "藏蓝", "亮色", "颜色",
            "好吃", "爆汁", "鲜甜", "新鲜", "口感", "出片",
        )
        promise_hits = sum(1 for word in promise_terms if word in hook_compact and word in compact)
        hook_colors = set(re.findall(r"[\u4e00-\u9fff]{0,2}色", hook_compact))
        exact_color_hits = sum(1 for word in hook_colors if len(word) >= 2 and word in compact)
        exact_follow_score = promise_hits * 6.0 + exact_color_hits * 7.0
        sim_score = min(sim * 8.0, 8.0 if exact_follow_score else 4.0)
        scores["hook_followup"] += sim_score + exact_follow_score
        hook_block = _clip_focus_block(("hook", hook_text, 0, 0, 0, 0, ""))
        if hook_block != "其他" and block == hook_block:
            scores["hook_followup"] += 4.0 if exact_follow_score else 1.5
        if any(word in compact for word in ("你看", "看到没有", "看到吧", "上身", "穿上", "效果", "直接", "所以", "这就是")):
            scores["hook_followup"] += 4.0

    direct_words = (
        "显瘦", "遮肉", "藏肉", "显高", "显腿长", "比例", "收腰", "修饰",
        "上身", "穿上", "效果", "显白", "提气色", "好看", "好吃", "爆汁",
        "鲜甜", "口感", "试吃", "入口", "拉丝",
    )
    proof_words = (
        "面料", "材质", "质感", "手感", "垂感", "做工", "工艺", "细节",
        "品质", "走线", "刺绣", "里衬", "成分", "克重", "新鲜", "产地",
        "源头", "现摘", "现发", "冷链", "规格", "分量", "个头", "坏果包赔",
    )
    scene_words = (
        "通勤", "上班", "约会", "日常", "出门", "旅游", "度假", "逛街",
        "聚会", "职场", "搭配", "内搭", "外穿", "成套", "套穿", "出片", "小个子",
        "微胖", "梨形", "苹果型", "妈妈", "姐妹", "全家", "早餐", "夜宵",
        "办公室", "送礼",
    )
    objection_words = (
        "不挑", "不用担心", "不用怕", "不会", "不显", "不胖", "不勒",
        "不卡", "不闷", "不透", "不起球", "不缩水", "不变形", "遮副乳",
        "遮肚子", "胯宽", "腿粗", "肩宽", "肚子", "尺码", "码数", "身高",
        "体重", "卡码", "长度", "售后", "包赔", "放心", "安心", "不踩雷",
    )
    close_words = (
        "推荐", "建议", "适合", "放心", "安心", "闭眼", "值得", "自留",
        "尺码", "身高体重", "复购", "老客",
    )

    if block in {"版型显瘦", "穿着体验", "口感食欲"}:
        scores["direct_effect"] += 8.0
    if block in {"面料质感", "品质细节", "工艺细节", "新鲜品质", "产地溯源", "规格分量", "发货保鲜"}:
        scores["proof_detail"] += 8.0
    if block in {"场景搭配", "场景吃法", "流行趋势"}:
        scores["scene_crowd"] += 7.0
    if block in {"尺寸长度", "穿着体验", "对比优势", "发货保鲜"}:
        scores["objection_resolver"] += 5.0
    if block == "紧迫稀缺":
        scores["objection_resolver"] += 2.0

    if any(word in compact for word in direct_words):
        scores["direct_effect"] += 5.0
    if any(word in compact for word in proof_words):
        scores["proof_detail"] += 5.0
    if any(word in compact for word in scene_words):
        scores["scene_crowd"] += 4.0
    if any(word in compact for word in objection_words):
        scores["objection_resolver"] += 5.0
    if any(word in compact for word in close_words):
        scores["natural_close"] += 4.0

    if preferred_block and block == preferred_block:
        preferred_role = _focus_block_sales_role(preferred_block)
        if preferred_role:
            scores[preferred_role] += 3.0
        else:
            scores["proof_detail"] += 1.0
    if selling_blocks and block in selling_blocks:
        scores["proof_detail"] += 2.5
        scores["objection_resolver"] += 1.0

    for role, weight in _sales_goal_role_weights(ai_controls).items():
        scores[role] += weight

    flags = _clip_boundary_quality_flags(clip)
    if flags:
        scores["weak_fragment"] += 8.0 + len(flags) * 2.0
        for role in ("hook_followup", "direct_effect", "proof_detail", "scene_crowd", "objection_resolver", "natural_close"):
            scores[role] -= 2.0

    if not compact or len(compact) < 5:
        scores["weak_fragment"] += 8.0

    return scores


def _clip_sales_role(clip, hook_text="", preferred_focus="", ai_controls=None, main_category=None):
    if _is_hook_clip(clip):
        return "hook"
    scores = _clip_sales_role_scores(clip, hook_text, preferred_focus, ai_controls, main_category)
    core_roles = ["hook_followup", "direct_effect", "proof_detail", "scene_crowd", "objection_resolver", "natural_close"]
    best_role = max(core_roles, key=lambda role: scores.get(role, 0.0))
    best_score = scores.get(best_role, 0.0)
    weak_score = scores.get("weak_fragment", 0.0)
    if weak_score >= max(14.0, best_score + 5.0):
        return "weak_fragment"
    if best_score < 4.0:
        return "other"
    return best_role


def _clip_sales_role_label(clip, hook_text="", preferred_focus="", ai_controls=None, main_category=None):
    return SALES_ROLE_LABELS.get(
        _clip_sales_role(clip, hook_text, preferred_focus, ai_controls, main_category),
        "补充卖点",
    )


def _sales_chain_role_order(ai_controls=None):
    controls = _normalize_ai_controls(ai_controls)
    goal = controls.get("goal", "")
    if goal == "专业讲解":
        middle = ["direct_effect", "proof_detail", "objection_resolver", "scene_crowd"]
    elif goal == "质感高级":
        middle = ["direct_effect", "proof_detail", "scene_crowd", "objection_resolver"]
    elif goal == "显瘦转化":
        middle = ["direct_effect", "objection_resolver", "proof_detail", "scene_crowd"]
    elif goal in {"快速促单", "新鲜转化", "囤货转化"}:
        middle = ["direct_effect", "proof_detail", "objection_resolver", "scene_crowd"]
    else:
        middle = ["direct_effect", "proof_detail", "scene_crowd", "objection_resolver"]
    return ["hook_followup"] + middle + ["natural_close", "other", "weak_fragment"]


def _reorder_by_sales_chain(clips, log_fn=None, preferred_cat=None, preferred_focus=None, ai_controls=None, merge_mode=False):
    """Reorder selected clips into a sales-video chain, while falling back if signal is weak."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or len(clips) < 4:
        return None

    hooks, products, closes = [], [], []
    for clip in clips:
        if _is_hook_clip(clip):
            hooks.append(clip)
        elif _is_close_clip(clip):
            closes.append(clip)
        else:
            products.append(clip)

    if len(products) < 3:
        return None

    hook_text = _clip_sales_text(hooks[0]) if hooks else ""
    role_order = _sales_chain_role_order(ai_controls)
    role_rank = {role: idx for idx, role in enumerate(role_order)}
    metas = []
    for idx, clip in enumerate(products):
        scores = _clip_sales_role_scores(clip, hook_text, preferred_focus, ai_controls, preferred_cat)
        role = _clip_sales_role(clip, hook_text, preferred_focus, ai_controls, preferred_cat)
        metas.append({"idx": idx, "clip": clip, "role": role, "scores": scores})

    meaningful = [m["role"] for m in metas if m["role"] not in {"other", "weak_fragment"}]
    if len(set(meaningful)) < 2:
        return None

    selected_ids = set()
    ordered_metas = []

    followup_candidates = [
        m for m in metas
        if m["scores"].get("hook_followup", 0.0) >= 6.0 and m["role"] != "weak_fragment"
    ]
    if followup_candidates:
        best_follow = max(
            followup_candidates,
            key=lambda m: (m["scores"].get("hook_followup", 0.0), m["scores"].get("direct_effect", 0.0), -m["idx"]),
        )
        first = metas[0]
        first_score = first["scores"].get("hook_followup", 0.0)
        if best_follow["idx"] != first["idx"] and best_follow["scores"].get("hook_followup", 0.0) >= first_score + 2.5:
            best_follow = dict(best_follow)
            best_follow["role"] = "hook_followup"
            ordered_metas.append(best_follow)
            selected_ids.add(best_follow["idx"])
        elif first_score >= 6.0:
            first = dict(first)
            first["role"] = "hook_followup"
            ordered_metas.append(first)
            selected_ids.add(first["idx"])

    def _time_key(meta):
        if merge_mode:
            return meta["idx"]
        try:
            return float(meta["clip"][2])
        except Exception:
            return meta["idx"]

    remaining = [m for m in metas if m["idx"] not in selected_ids]
    for meta in remaining:
        if meta["role"] != "hook_followup":
            continue
        fallback_roles = [
            "direct_effect", "proof_detail", "scene_crowd",
            "objection_resolver", "natural_close", "other",
        ]
        meta["role"] = max(fallback_roles, key=lambda role: meta["scores"].get(role, 0.0))
    remaining = sorted(
        remaining,
        key=lambda m: (
            role_rank.get(m["role"], 99),
            -m["scores"].get(m["role"], 0.0),
            _time_key(m),
            m["idx"],
        ),
    )
    ordered_metas.extend(remaining)

    ordered_products = [m["clip"] for m in ordered_metas]
    if ordered_products == products:
        return None

    before_roles = [_clip_sales_role_label(c, hook_text, preferred_focus, ai_controls, preferred_cat) for c in products]
    after_roles = [_clip_sales_role_label(c, hook_text, preferred_focus, ai_controls, preferred_cat) for c in ordered_products]
    summary = []
    for role in role_order:
        count = sum(1 for m in ordered_metas if m["role"] == role)
        if count:
            summary.append(f"{SALES_ROLE_LABELS.get(role, role)}{count}")
    _log(f"成交链路排序: {' → '.join(summary)}")
    if before_roles[:6] != after_roles[:6]:
        _log(f"成交链路角色: {'/'.join(before_roles[:6])} → {'/'.join(after_roles[:6])}")

    reordered = (hooks[:1] if hooks else []) + ordered_products + closes
    return _repair_hook_followup_coherence(reordered, log_fn, preferred_focus=preferred_focus)


def _repair_hook_followup_coherence(clips, log_fn=None, preferred_focus=None):
    """Move the best related product clip directly after Hook when needed."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or len(clips) < 3 or not _is_hook_clip(clips[0]):
        return clips

    product_indices = [
        idx for idx, clip in enumerate(clips)
        if not _is_hook_clip(clip) and not _is_close_clip(clip)
    ]
    if len(product_indices) < 2:
        return clips

    hook = clips[0]
    first_idx = product_indices[0]
    first = clips[first_idx]
    hook_text = str(hook[1] if len(hook) > 1 else "")
    hook_block = _clip_focus_block(hook)
    preferred_block = _focus_label_to_block(preferred_focus)
    if preferred_block and _clip_focus_block(first) == preferred_block:
        return clips

    def _time_gap_score(clip):
        try:
            gap = float(clip[2]) - float(hook[3])
        except Exception:
            return 0.0
        if 0 <= gap <= 45:
            return 4.0
        if 45 < gap <= 150:
            return 2.0
        if -20 <= gap < 0:
            return 1.0
        return 0.0

    def _score(clip):
        scores = _clip_sales_role_scores(clip, hook_text, preferred_focus=preferred_focus)
        block = _clip_focus_block(clip)
        score = scores.get("hook_followup", 0.0)
        score += scores.get("direct_effect", 0.0) * 0.35
        score -= scores.get("weak_fragment", 0.0) * 0.4
        score += _time_gap_score(clip)
        if hook_block != "其他" and block == hook_block:
            score += 2.0
        elif hook_block != "其他" and block != "其他":
            score -= 1.5
        try:
            focus = str(clip[6] if len(clip) > 6 else "")
            hook_focus = str(hook[6] if len(hook) > 6 else "")
            if focus and hook_focus and (focus in hook_focus or hook_focus in focus):
                score += 2.0
        except Exception:
            pass
        return score

    current_score = _score(first)
    candidates = [(idx, _score(clips[idx])) for idx in product_indices]
    best_idx, best_score = max(candidates, key=lambda item: item[1])
    if best_idx == first_idx:
        return clips
    if current_score >= 6.0:
        return clips
    if best_score < 6.0 or best_score < current_score + 3.0:
        return clips

    reordered = list(clips)
    chosen = reordered.pop(best_idx)
    insert_at = 1 if first_idx != 0 else first_idx + 1
    reordered.insert(insert_at, chosen)
    _log(
        "Hook衔接: 调整第二段 "
        f"{first_idx + 1}→{best_idx + 1}，"
        f"{_clip_focus_block(first)}({current_score:.1f}) → {_clip_focus_block(chosen)}({best_score:.1f})"
    )
    return reordered


def _reorder_product_focus_blocks(clips, log_fn=None, preferred_cat=None, preferred_focus=None, ai_controls=None, merge_mode=False):
    """Adaptively group product clips by selling-point block.

    The goal is not to force every video into one fixed template. If the AI
    already produced a coherent narrative, keep it. When the current AI
    preference is present in selected clips, use that block as the narrative
    anchor; otherwise gently group related selling points around the first
    AI-selected product block.
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or len(clips) < 3:
        return clips

    hooks = []
    closes = []
    products = []
    for clip in clips:
        ctype = str(clip[0]).lower() if clip else ""
        if "hook" in ctype:
            hooks.append(clip)
        elif "close" in ctype or ctype == "call_to_action":
            closes.append(clip)
        else:
            products.append(clip)

    base_reordered = (hooks[:1] if hooks else []) + products + closes
    if not products:
        if base_reordered != clips:
            _log(f"卖点段落排序: 保持AI原序，仅修正hook/close位置 ({len(clips)}段)")
        else:
            _log(f"卖点段落排序: 保持AI原序 ({len(clips)}段)")
        return _repair_hook_followup_coherence(base_reordered, log_fn, preferred_focus=preferred_focus)

    sales_reordered = _reorder_by_sales_chain(
        base_reordered,
        log_fn,
        preferred_cat=preferred_cat,
        preferred_focus=preferred_focus,
        ai_controls=ai_controls,
        merge_mode=merge_mode,
    )
    if sales_reordered is not None:
        return sales_reordered

    # 检测主品类，优先使用用户指定品类；指定食品时不要因具体品名未命中而退回通用排序。
    main_cat = _normalize_forced_category(preferred_cat)
    if not main_cat:
        cat_counts = {}
        for clip in products:
            cat = _detect_product_category(str(clip[1] if len(clip) > 1 else ""))
            if cat:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        main_cat = max(cat_counts, key=cat_counts.get) if cat_counts else None
    block_order = CATEGORY_FOCUS_ORDER.get(main_cat, DEFAULT_BLOCK_ORDER) if main_cat else DEFAULT_BLOCK_ORDER

    blocks = [_clip_focus_block(clip) for clip in products]
    preferred_block = _focus_label_to_block(preferred_focus)
    preferred_present = bool(preferred_block and preferred_block in blocks)
    block_rank = {block: idx for idx, block in enumerate(block_order)}

    def _rank(block):
        return block_rank.get(block, len(block_order) + 1)

    def _has_scattered_blocks(seq):
        closed = set()
        last = None
        for block in seq:
            if block == last:
                continue
            if block in closed:
                return True
            if last is not None:
                closed.add(last)
            last = block
        return False

    ranks = [_rank(block) for block in blocks]
    inversions = sum(1 for i in range(len(ranks)) for j in range(i + 1, len(ranks)) if ranks[i] > ranks[j])
    time_backtracks = 0
    for prev, curr in zip(products, products[1:]):
        try:
            if float(prev[2]) - float(curr[2]) > 180:
                time_backtracks += 1
        except Exception:
            pass

    scattered = _has_scattered_blocks(blocks)
    first_product_block = blocks[0] if blocks else None
    preferred_mismatch = preferred_present and first_product_block != preferred_block
    order_noise = len(products) >= 4 and (scattered or inversions >= 2 or time_backtracks >= 2)
    needs_reorder = preferred_mismatch or order_noise
    if not needs_reorder:
        pref_note = ""
        if preferred_block:
            pref_note = f"，偏好{preferred_block}{'已在前排' if preferred_present else '未命中'}"
        if base_reordered != clips:
            _log(f"卖点段落排序: 保持AI原序，仅修正hook/close位置{pref_note} ({len(clips)}段)")
        else:
            _log(f"卖点段落排序: 保持AI原序{pref_note} ({len(clips)}段)")
        return _repair_hook_followup_coherence(base_reordered, log_fn, preferred_focus=preferred_focus)

    anchor = preferred_block if preferred_present else next((block for block in blocks if block != "其他"), blocks[0] if blocks else None)
    if preferred_present:
        adaptive_order = [preferred_block] + [block for block in block_order if block != preferred_block]
    elif anchor in block_order:
        anchor_idx = block_order.index(anchor)
        adaptive_order = block_order[anchor_idx:] + block_order[:anchor_idx]
    else:
        adaptive_order = list(block_order)

    for block in blocks:
        if block not in adaptive_order:
            adaptive_order.append(block)

    grouped = {}
    for clip, block in zip(products, blocks):
        grouped.setdefault(block, []).append(clip)

    ordered_products = []
    sorted_group_count = 0
    for block in adaptive_order:
        group = grouped.get(block) or []
        if len(group) >= 2:
            starts = []
            for clip in group:
                try:
                    starts.append(float(clip[2]))
                except Exception:
                    starts.append(0.0)
            if starts != sorted(starts):
                group = sorted(group, key=lambda c: float(c[2]) if len(c) > 2 else 0.0)
                sorted_group_count += 1
        ordered_products.extend(group)

    block_summary = [f"{block}{len(grouped[block])}" for block in adaptive_order if block in grouped and grouped[block]]
    detail = f"，同卖点时间整理{sorted_group_count}组" if sorted_group_count else ""
    pref_note = f"，偏好={preferred_block}" if preferred_block else ""
    if preferred_block and not preferred_present:
        pref_note += "未命中"
    _log(f"卖点段落排序: 自适应整理({main_cat or '通用'}{pref_note}，锚点={anchor}) {' → '.join(block_summary)}{detail}")

    reordered = (hooks[:1] if hooks else []) + ordered_products + closes
    return _repair_hook_followup_coherence(reordered, log_fn, preferred_focus=preferred_focus)


# ============================================================
# 硬校验(最少7段)
# ============================================================
def _validate_clips(clips, log_fn, multi_version=False):
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        _log(f"校验失败: 仅 {len(clips)} 片段")
        return False

    types = [c[0] for c in clips]
    if "hook" not in types:
        _log("警告: 缺少 hook(但不拒绝)")
    if "call_to_action" not in types:
        _log("警告: 缺少 close(但不拒绝)")
    # 允许重复类型(去除严格限制)
    # indices = [GOLDEN_CHAIN.index(t) if t in GOLDEN_CHAIN else 99 for t in types]
    # if indices != sorted(indices):
    #     _log("校验失败: 顺序错误"); return False

    total = sum(c[5] for c in clips)
    max_total = 225 if multi_version else 180  # 多版本模式放宽(150-225s)
    if total < 20 or total > max_total:
        _log(f"校验失败: 总时长 {total:.1f}s 异常(上限{max_total}s)")
        return False

    if len(clips) < 3:
        _log(f"警告: 仅 {len(clips)} 段(建议≥7段)，但继续处理")

    return True


def _relax_clips(clips, log_fn):
    def _log(msg):
        if log_fn: log_fn(msg)
    if not clips or len(clips) < 2:
        return None
    type_best = {}
    for c in clips:
        if c[0] not in type_best or c[4] > type_best[c[0]][4]:
            type_best[c[0]] = c
    sorted_clips = [type_best[t] for t in GOLDEN_CHAIN if t in type_best]
    if len(sorted_clips) < 2:
        return None

    total = sum(c[5] for c in sorted_clips)
    if total > 65:
        for i, c in enumerate(sorted_clips):
            if c[5] > 10 and total > 60:
                cut = min(c[5] - 10, total - 60)
                s, e = c[2], c[3] - cut
                sorted_clips[i] = (c[0], c[1], s, e, c[4], e - s)
                total = sum(x[5] for x in sorted_clips)
    elif total < 50:
        for i, c in enumerate(sorted_clips):
            if c[0] == "highlight" and c[5] < 15:
                add = min(60 - total, 15 - c[5])
                if add > 0:
                    s, e = c[2], c[3] + add
                    sorted_clips[i] = (c[0], c[1], s, e, c[4], e - s)
                break
    total = sum(c[5] for c in sorted_clips)
    _log(f"宽松修复: {len(sorted_clips)} 片段, 总时长 {total:.1f}s")
    return sorted_clips


# ============================================================
# 片段延伸(自动前后扩展短片段，目标总时长 45-65 秒)
# ============================================================
# ============================================================
# ASR 纠错:修正 AI 输出文案中的常见 ASR 错误
# ============================================================
def _apply_asr_corrections(text, log_fn=None):
    """对片段文案进行 ASR 错误修正"""
    def _log(msg):
        if log_fn: log_fn(msg)

    original = text
    corrected = text

    for wrong, right in ASR_CORRECTIONS.items():
        if wrong in corrected:
            corrected = corrected.replace(wrong, right)
            if corrected != original:
                _log(f"  ASR纠错: '{wrong}' → '{right}'")

    return corrected


# ============================================================
# 片段边界修复:确保片段首尾对齐到 SRT 句子边界
# ============================================================

def _ensure_close_complete(clips, cleaned_srt, log_fn=None):
    """
    [v9.4] 确保 Close（最后一段）语句完整，不被半句截断。
    检查 Close 片段结尾是否在 SRT 条目中间，如果是且语句不完整，
    延伸到下一个有完整断句的 SRT 条目末尾（最多延伸3秒）。
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips or not cleaned_srt:
        return clips
    try:
        return _ensure_close_complete_impl(clips, cleaned_srt, log_fn)
    except Exception as e:
        if log_fn: log_fn(f"Close完整: 异常 {e}，保持原样")
        return clips


def _ensure_close_complete_impl(clips, cleaned_srt, log_fn=None):
    """[v9.4] 内部实现：确保 Close 语句完整"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips or not cleaned_srt:
        return clips

    # 解析 SRT 为 entries: [(start_s, end_s, text), ...]
    entries = []
    lines = cleaned_srt.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(
            r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
            line)
        if m:
            start_s = (int(m.group(1))*3600 + int(m.group(2))*60 +
                       int(m.group(3)) + int(m.group(4))/1000.0)
            end_s = (int(m.group(5))*3600 + int(m.group(6))*60 +
                     int(m.group(7)) + int(m.group(8))/1000.0)
            text = ""
            j = i + 1
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                text += lines[j].strip()
                j += 1
            entries.append((start_s, end_s, text.strip()))
            i = j
        else:
            i += 1

    if not entries:
        return clips

    # 找最后一个 Close 片段
    close_idx = None
    for ci in range(len(clips) - 1, -1, -1):
        if 'close' in clips[ci][0].lower():
            close_idx = ci
            break

    if close_idx is None:
        # 没有 Close 类型片段，检查最后一段
        close_idx = len(clips) - 1

    clip = clips[close_idx]
    ct, text, start, end, score, dur = clip[:6]
    # 确保 start/end/dur 是 float（AI偶尔返回str）
    start = float(start) if isinstance(start, str) else start
    end = float(end) if isinstance(end, str) else end
    dur = float(dur) if isinstance(dur, str) else dur
    rest = clip[6:]

    # 句末完整性标点
    SENTENCE_END = set("。？！.?!，,、；;：:")
    # 弱结尾词（句子悬空，明显没说完）
    WEAK_ENDINGS = ["然后", "就是", "其实", "而且", "但是", "不过", "所以",
                    "因为", "如果", "虽然", "不仅", "并且", "以及",
                    "这个", "那个", "一件", "一套", "一条", "一个",
                    "觉得", "感觉", "发现", "看到", "觉得",
                    "的", "了", "呀", "呢", "吧", "咯", "啊", "哈", "啦", "嘛"]

    # 找 end 时间落在哪个 SRT 条目
    end_entry_idx = None
    for ei, (s, e, t) in enumerate(entries):
        if s <= end <= e + 0.3:
            end_entry_idx = ei
            break
        if s > end:
            break

    if end_entry_idx is None:
        # end 在所有 SRT 条目之后，不需要延伸
        return clips

    # 检查当前结尾是否语句完整
    def is_sentence_complete(txt):
        """判断文本是否以完整语句结尾"""
        if not txt:
            return False
        t = txt.rstrip()
        if not t:
            return False
        last_char = t[-1]
        # 以句末标点结尾 → 完整
        if last_char in "。？！.?!":
            return True
        # 以弱结尾词结尾 → 不完整
        for w in WEAK_ENDINGS:
            if t.endswith(w):
                return False
        # 中文字符/数字结尾，检查是否是自然断句
        # 如果末尾是逗号/顿号等，说明还有后续
        if last_char in "，,、；;：:":
            return False
        # 其他情况（中文字符结尾）视为可能完整
        return True

    current_end_text = entries[end_entry_idx][2]
    if is_sentence_complete(current_end_text):
        # 当前结尾已经完整，检查 end 是否对齐到条目末尾
        entry_end = entries[end_entry_idx][1]
        if end < entry_end - 0.3:
            # end 在条目中间，延伸到条目末尾
            new_end = entry_end
            extension = new_end - end
            if extension <= 3.0:
                new_dur = new_end - start
                clips[close_idx] = (ct, text, start, new_end, score, new_dur, *rest)
                _log(f"Close完整: 延伸到条目末尾 {end:.1f}s→{new_end:.1f}s (+{extension:.1f}s)")
        return clips

    # 语句不完整，尝试延伸到后续条目找完整断句
    max_extension = 3.0
    extended_end = end

    for ei in range(end_entry_idx, len(entries)):
        s, e, t = entries[ei]
        if s > end + max_extension:
            break  # 超出最大延伸范围
        extended_end = e
        if is_sentence_complete(t):
            break  # 找到完整断句，停在这里

    if extended_end > end:
        actual_extension = extended_end - end
        if actual_extension <= max_extension:
            new_dur = extended_end - start
            clips[close_idx] = (ct, text, start, extended_end, score, new_dur, *rest)
            _log(f"Close完整: 延伸 {end:.1f}s→{extended_end:.1f}s (+{actual_extension:.1f}s) 确保语句完整")
        else:
            _log(f"Close完整: 需延伸{actual_extension:.1f}s超限(>{max_extension}s)，保持原样")

        return clips





_WORD_EDGE_PREFIXES = (
    "来所有的宝宝听我说", "来所有宝宝听我说", "所有的宝宝听我说", "所有宝宝听我说",
    "是不是这种感觉", "是这种感觉", "这种感觉", "来我跟你们讲", "来我跟你们说",
    "你们知道的",
    "来准备好啊准备好", "来准备好啊", "来准备好", "准备好啊", "准备好",
    "然后", "而且", "但是", "不过", "因为", "没错", "是的", "好的", "好吧", "其实", "就是", "所以",
    "是因为", "对吧", "是吧", "嗯嗯", "嗯", "呃", "啊",
)
_WORD_EDGE_SUFFIXES = (
    "反正就是不显白怎么说呢", "是不是这种感觉", "是的为什么", "呀对不对", "能理解吗",
    "对不对", "知道吧", "是不是",
    "为什么",
    "对吧", "是吧", "然后", "而且",
)
_WORD_TAIL_NOISE_STARTS = (
    "头发打理教程", "打理教程", "下次会做视频", "提醒我", "催债", "催追", "催视频",
)


def _word_edge_norm(value):
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or ""))


def _remove_normalized_text_edge(text, count, from_start=True):
    """Remove normalized characters while preserving a leading [Vn] marker."""
    raw = str(text or "")
    marker = ""
    marker_match = re.match(r"^(\s*\[V\d+\]\s*)", raw, flags=re.I)
    if marker_match:
        marker = marker_match.group(1)
        raw = raw[marker_match.end():]
    indexes = [idx for idx, char in enumerate(raw) if _word_edge_norm(char)]
    if not indexes or count <= 0 or count >= len(indexes):
        return text
    if from_start:
        raw = raw[indexes[count - 1] + 1:].lstrip(" ，。！？、；：,.!?;:")
    else:
        raw = raw[:indexes[-count]].rstrip(" ，。！？、；：,.!?;:")
    return marker + raw


def _word_timing_tokens(word_timings, start, end, marker=""):
    tokens = []
    marker = str(marker or "").upper()
    for segment in word_timings or []:
        if not isinstance(segment, dict):
            continue
        segment_marker = str(segment.get("source_marker") or "").upper()
        if marker and segment_marker != marker:
            continue
        if not marker and segment_marker:
            continue
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            norm = _word_edge_norm(word.get("text"))
            try:
                word_start = float(word.get("start") or 0)
                word_end = float(word.get("end") or word_start)
            except (TypeError, ValueError):
                continue
            midpoint = (word_start + word_end) / 2.0
            if norm and word_end > word_start and start - 0.02 <= midpoint <= end + 0.02:
                tokens.append({
                    "norm": norm,
                    "text": str(word.get("text") or ""),
                    "start": word_start,
                    "end": word_end,
                })
    return sorted(tokens, key=lambda item: (item["start"], item["end"]))


def _word_edge_match(tokens, candidates, from_start=True):
    if not tokens:
        return None
    combined = "".join(token["norm"] for token in tokens)
    for candidate in sorted(candidates, key=len, reverse=True):
        if (combined.startswith(candidate) if from_start else combined.endswith(candidate)):
            if len(combined) - len(candidate) < 6:
                continue
            consumed = 0
            iterable = tokens if from_start else list(reversed(tokens))
            for token in iterable:
                consumed += len(token["norm"])
                if consumed >= len(candidate):
                    return candidate, token["end"] if from_start else token["start"]
    return None


def _word_tail_noise_match(tokens):
    if not tokens:
        return None
    combined = "".join(token["norm"] for token in tokens)
    matches = []
    for candidate in _WORD_TAIL_NOISE_STARTS:
        offset = combined.find(candidate)
        if offset >= 6:
            matches.append((offset, candidate))
    if not matches:
        return None
    offset, candidate = min(matches)
    consumed = 0
    for token in tokens:
        next_consumed = consumed + len(token["norm"])
        if consumed <= offset < next_consumed:
            return candidate, token["start"]
        consumed = next_consumed
    return None


def _trim_word_level_filler_edges(clips, word_timings, log_fn=None):
    if not clips or not word_timings:
        return clips, set()
    result = []
    changed = set()
    prefix_count = 0
    suffix_count = 0
    tail_noise_count = 0
    for index, clip in enumerate(clips):
        if not isinstance(clip, (list, tuple)) or len(clip) < 6:
            result.append(clip)
            continue
        ct, text, start, end, score, dur = clip[:6]
        rest = tuple(clip[6:])
        try:
            original_start = float(start)
            original_end = float(end)
        except (TypeError, ValueError):
            result.append(clip)
            continue
        marker_match = re.search(r"\[(V\d+)\]", str(text or ""), flags=re.I)
        marker = marker_match.group(1).upper() if marker_match else ""
        new_start, new_end, new_text = original_start, original_end, text
        clip_tail_noise_trimmed = False
        tokens = _word_timing_tokens(word_timings, new_start, new_end, marker)
        fragment_trim = leading_fragment_trim(tokens)
        if fragment_trim:
            boundary = float(fragment_trim["boundary"])
            if new_end - boundary >= 2.0:
                new_start = max(new_start, boundary)
                changed.add(index)
                prefix_count += 1

        for _ in range(4):
            tokens = _word_timing_tokens(word_timings, new_start, new_end, marker)
            prefix_match = _word_edge_match(tokens, _WORD_EDGE_PREFIXES, from_start=True)
            if not prefix_match:
                break
            prefix, boundary = prefix_match
            if new_end - boundary < 2.0:
                break
            new_start = max(new_start, boundary)
            new_text = _remove_normalized_text_edge(new_text, len(prefix), from_start=True)
            changed.add(index)
            prefix_count += 1

        tokens = _word_timing_tokens(word_timings, new_start, new_end, marker)
        tail_noise_match = _word_tail_noise_match(tokens)
        if tail_noise_match:
            _, boundary = tail_noise_match
            if boundary - new_start >= 1.5:
                new_end = min(new_end, boundary)
                changed.add(index)
                tail_noise_count += 1
                clip_tail_noise_trimmed = True

        tokens = _word_timing_tokens(word_timings, new_start, new_end, marker)
        suffix_match = _word_edge_match(tokens, _WORD_EDGE_SUFFIXES, from_start=False)
        if suffix_match:
            suffix, boundary = suffix_match
            if boundary - new_start >= 2.0:
                new_end = min(new_end, boundary)
                new_text = _remove_normalized_text_edge(new_text, len(suffix), from_start=False)
                changed.add(index)
                suffix_count += 1

        if index in changed:
            final_tokens = _word_timing_tokens(word_timings, new_start, new_end, marker)
            rebuilt_text = "".join(token.get("text") or "" for token in final_tokens).strip()
            if rebuilt_text:
                new_start = max(new_start, float(final_tokens[0]["start"]))
                new_end = min(new_end, float(final_tokens[-1]["end"]))
                new_text = f"[{marker}] {rebuilt_text}" if marker else rebuilt_text

        min_remaining_duration = 1.5 if clip_tail_noise_trimmed else 2.0
        if new_end - new_start >= min_remaining_duration:
            result.append((ct, new_text, new_start, new_end, score, new_end - new_start, *rest))
        else:
            result.append(clip)
            changed.discard(index)
    if (prefix_count or suffix_count or tail_noise_count) and log_fn:
        log_fn(
            f"词级废话裁剪: 开头 {prefix_count} 处, 结尾 {suffix_count} 处, "
            f"中后段闲聊 {tail_noise_count} 处"
        )
    return result, changed


_DANGLING_TAIL_CLAUSE_WORDS = (
    "它", "他", "她", "这个", "这款", "这件", "这个款", "这种", "那种",
    "你看", "然后", "而且", "但是", "不过", "所以", "因为", "就是",
)
_DANGLING_TAIL_CLAUSE_RE = re.compile(
    r"[，,、：:]\s*(?P<tail>"
    + "|".join(re.escape(word) for word in sorted(_DANGLING_TAIL_CLAUSE_WORDS, key=len, reverse=True))
    + r")\s*[，,、：:]?\s*$"
)


def _tail_boundary_from_tokens(tokens, tail_norm):
    tail_norm = _word_edge_norm(tail_norm)
    if not tokens or not tail_norm:
        return None
    combined = ""
    boundary = None
    for token in reversed(tokens):
        norm = _word_edge_norm(token.get("norm") or token.get("text"))
        if not norm:
            continue
        combined = norm + combined
        if not tail_norm.endswith(combined):
            break
        boundary = float(token["start"])
        if combined == tail_norm:
            return boundary
    return None


def _trim_dangling_tail_clauses(clips, word_timings, log_fn=None):
    """Trim tiny dangling clause openers after punctuation, e.g. '领口不变形，它'."""
    if not clips or not word_timings:
        return clips
    result = []
    trim_count = 0
    for clip in clips:
        if not isinstance(clip, (list, tuple)) or len(clip) < 6:
            result.append(clip)
            continue
        ct, text, start, end, score, dur = clip[:6]
        rest = tuple(clip[6:])
        raw_text = str(text or "")
        marker_match = re.match(r"^(\s*\[(V\d+)\]\s*)", raw_text, flags=re.I)
        marker_prefix = marker_match.group(1) if marker_match else ""
        marker = marker_match.group(2).upper() if marker_match else ""
        body = raw_text[marker_match.end():] if marker_match else raw_text
        match = _DANGLING_TAIL_CLAUSE_RE.search(body.rstrip())
        if not match:
            result.append(clip)
            continue
        try:
            start_f = float(start)
            end_f = float(end)
        except (TypeError, ValueError):
            result.append(clip)
            continue
        tokens = _word_timing_tokens(word_timings, start_f, end_f, marker)
        boundary = _tail_boundary_from_tokens(tokens, match.group("tail"))
        if boundary is None or boundary - start_f < 1.5:
            result.append(clip)
            continue
        trimmed_body = body[:match.start()].rstrip(" ，,、：:；;")
        if len(_word_edge_norm(trimmed_body)) < 6:
            result.append(clip)
            continue
        new_text = f"{marker_prefix}{trimmed_body}".strip()
        result.append((ct, new_text, start_f, boundary, score, max(0.0, boundary - start_f), *rest))
        trim_count += 1
    if trim_count and log_fn:
        log_fn(f"词级弱尾裁剪: 裁掉 {trim_count} 个片段结尾的孤立承接词")
    return result


def _trim_filler_start(clips, cleaned_srt, log_fn=None, word_timings=None):
    """裁掉片段开头的废话：1)整条SRT是废话词 2)SRT文本以废话前缀开头(按字符比例裁时间)"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips:
        return clips

    clips, word_trimmed_indices = _trim_word_level_filler_edges(clips, word_timings, log_fn)
    if not cleaned_srt:
        return clips

    _kw_local_fb = _get_keywords()
    FILLER_WORDS = set(_kw_local_fb["filler_words"])
    FILLER_WORDS.update({
        "来准备好", "来准备好啊", "准备好啊", "准备好",
        "然后", "而且", "但是", "不过", "因为", "其实", "就是", "所以",
    })
    # 构建按长度降序排列的废话前缀列表(长前缀优先匹配)
    _sorted_filler = sorted(FILLER_WORDS, key=len, reverse=True)

    # 解析 SRT 为 entries
    entries = []
    lines = cleaned_srt.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(
            r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
            line)
        if m:
            start_s = (int(m.group(1))*3600 + int(m.group(2))*60 +
                       int(m.group(3)) + int(m.group(4))/1000.0)
            end_s = (int(m.group(5))*3600 + int(m.group(6))*60 +
                     int(m.group(7)) + int(m.group(8))/1000.0)
            text = ""
            j = i + 1
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                text += lines[j].strip()
                j += 1
            norm = re.sub(r'[^\u4e00-\u9fff\w]', '', text.strip())
            entries.append((start_s, end_s, norm))
            i = j
        else:
            i += 1

    if not entries:
        return clips

    trimmed = []
    trim_count = 0
    prefix_trim_count = 0
    for clip_index, (ct, text, start, end, score, dur, *_) in enumerate(clips):
        new_start = start
        new_text = text
        # Hook片段也要裁掉开头的废话SRT条目和废话前缀
        # 但不在_fix_clip_boundaries中做前向延伸（保持爆点起始）
        # 例如: "没有了 宝宝 / 来我跟你们讲 / 这套衣服千万不要错过"
        #       → 裁掉前两句废话，从"这套衣服"开始
        is_hook = 'hook' in ct.lower()

        # 第一步：跳过片段开头整条是废话的SRT条目（Hook也参与）
        for s, e, norm in entries:
            if e <= new_start:
                continue
            if s >= end:
                break
            if s < new_start:
                continue
            # Only skip filler entries that are actually contiguous with the
            # current clip boundary. A filler sentence later in the clip must
            # not discard the useful speech before it.
            if s > new_start + 0.35:
                break
            # Hook: 只跳过短废话(≤4字)，避免把有效爆点也当废话裁掉
            # 非Hook: 跳过所有废话
            if is_hook:
                if norm in FILLER_WORDS and len(norm) <= 4:
                    new_start = e
                    trim_count += 1
                else:
                    break
            else:
                if norm in FILLER_WORDS or (len(norm) <= 2 and norm in FILLER_WORDS):
                    new_start = e
                    trim_count += 1
                else:
                    break

        # 第二步：检测片段文本是否以废话前缀开头
        # 如 "我讲上这套的特点啊如果你是早起遛狗..." → 裁掉 "我讲上这套的特点啊"
        marker_prefix = ""
        text_for_prefix = str(text or "")
        marker_match = re.match(r"^(\s*\[[vV]\d+\]\s*)(.*)$", text_for_prefix, re.S)
        if marker_match:
            marker_prefix = marker_match.group(1)
            text_for_prefix = marker_match.group(2)
        norm_text = re.sub(r'[^\u4e00-\u9fff\w]', '', text_for_prefix.strip())
        filler_prefix_len = 0
        if clip_index not in word_trimmed_indices and not word_timings:
            remaining_norm = norm_text
            for _prefix_pass in range(4):
                matched_prefix = ""
                for fw in _sorted_filler:
                    if fw and remaining_norm.startswith(fw):
                        matched_prefix = fw
                        break
                if not matched_prefix or len(remaining_norm) - len(matched_prefix) < 6:
                    break
                filler_prefix_len += len(matched_prefix)
                remaining_norm = remaining_norm[len(matched_prefix):]

        if filler_prefix_len > 0 and len(norm_text) > filler_prefix_len:
            # 按字符比例估算裁切时间
            total_chars = len(norm_text)
            ratio = filler_prefix_len / total_chars
            # Hook: 更保守，只裁20%以内
            # 非Hook: 裁40%以内
            max_ratio = 0.2 if is_hook else 0.4
            if ratio <= max_ratio:
                clip_dur = end - new_start
                trim_seconds = clip_dur * ratio
                candidate_start = new_start + trim_seconds
                new_dur = end - candidate_start
                if new_dur >= 2.0:
                    new_start = candidate_start
                    trimmed_body = _remove_normalized_text_edge(text_for_prefix, filler_prefix_len, from_start=True)
                    new_text = marker_prefix + str(trimmed_body).lstrip()
                    prefix_trim_count += 1

        new_dur = end - new_start
        if new_dur < 2.0:
            trimmed.append((ct, text, start, end, score, dur, *_))
        else:
            trimmed.append((ct, new_text, new_start, end, score, new_dur, *_))

    if trim_count:
        _log(f"废话裁剪: 跳过 {trim_count} 个整条废话SRT")
    if prefix_trim_count:
        _log(f"废话裁剪: 裁掉 {prefix_trim_count} 个片段开头的废话前缀")

    return trimmed


def _trim_filler_middle(clips, cleaned_srt, log_fn=None):
    """裁掉片段中间的废话SRT条目：将片段在废话处拆分，丢弃废话段，保留有效段。
    如果废话把片段切成两半，保留较长的那半（或合并前后有效部分）。
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips or not cleaned_srt:
        return clips

    _kw_local_fb = _get_keywords()
    FILLER_WORDS = set(_kw_local_fb["filler_words"])

    # 解析 SRT
    entries = []
    lines = cleaned_srt.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(
            r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
            line)
        if m:
            start_s = (int(m.group(1))*3600 + int(m.group(2))*60 +
                       int(m.group(3)) + int(m.group(4))/1000.0)
            end_s = (int(m.group(5))*3600 + int(m.group(6))*60 +
                     int(m.group(7)) + int(m.group(8))/1000.0)
            text = ""
            j = i + 1
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                text += lines[j].strip()
                j += 1
            norm = re.sub(r'[^\u4e00-\u9fff\w]', '', text.strip())
            entries.append((start_s, end_s, norm))
            i = j
        else:
            i += 1

    if not entries:
        return clips

    result = []
    trim_count = 0
    for ct, text, start, end, score, dur, *_ in clips:
        # 找到片段范围内的所有SRT条目
        clip_entries = [(s, e, norm) for s, e, norm in entries
                        if s < end and e > start]

        if not clip_entries:
            result.append((ct, text, start, end, score, dur, *_))
            continue

        # 标记每个条目是否是废话
        # 废话判断：纯废话词 或 长度<=4且全是语气词/过渡词
        filler_marks = []
        for s, e, norm in clip_entries:
            is_filler = False
            if norm in FILLER_WORDS:
                is_filler = True
            elif len(norm) <= 4:
                # 短句检查：是否全是无实质内容的语气词
                short_filler = {"知道吗", "对吧", "好吧", "然后", "而且", "但是",
                                "你看", "是吧", "对对", "嗯嗯", "好的", "来了",
                                "没有", "还有的", "头疼", "没了", "来吧"}
                if norm in short_filler or norm in FILLER_WORDS:
                    is_filler = True
            filler_marks.append(is_filler)

        # 如果中间没有废话，保持原样
        has_middle_filler = False
        for k in range(1, len(filler_marks) - 1):  # 跳过第一个和最后一个
            if filler_marks[k]:
                has_middle_filler = True
                break

        if not has_middle_filler:
            result.append((ct, text, start, end, score, dur, *_))
            continue

        # 有中间废话：找最长连续有效段
        # 将片段分成"有效区间"（连续的非废话条目）
        effective_ranges = []
        range_start = None
        for k, (s, e, norm) in enumerate(clip_entries):
            if not filler_marks[k]:
                if range_start is None:
                    range_start = max(s, start)
            else:
                if range_start is not None:
                    effective_ranges.append((range_start, min(e, end)))
                    range_start = None
        if range_start is not None:
            effective_ranges.append((range_start, min(clip_entries[-1][1], end)))

        if not effective_ranges:
            # 全是废话？保留原片段
            result.append((ct, text, start, end, score, dur, *_))
            continue

        # 选择最长的有效区间
        best = max(effective_ranges, key=lambda r: r[1] - r[0])
        new_start, new_end = best
        new_dur = new_end - new_start

        if new_dur < 2.0:
            # 太短了，保留原片段
            result.append((ct, text, start, end, score, dur, *_))
        else:
            trim_count += 1
            result.append((ct, text, new_start, new_end, score, new_dur, *_))

    if trim_count:
        _log(f"中间废话裁剪: 处理 {trim_count} 个含中间废话的片段")

    return result

def _remove_expanded_overlap_clips(clips, log_fn=None):
    """Remove clips that became duplicate after semantic context expansion."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or len(clips) < 2:
        return clips

    keep = [True] * len(clips)

    def _clean_text(text):
        return re.sub(r"[\s，。！？!?、,.；;：:【】\[\]（）()~～\-]+", "", str(text or ""))

    def _is_close(clip):
        return "close" in str(clip[0]).lower() or str(clip[0]).lower() in ("cta", "call_to_action")

    def _is_hook(clip):
        return "hook" in str(clip[0]).lower()

    def _score_keep(clip):
        text = _clean_text(clip[1] if len(clip) > 1 else "")
        dur = float(clip[5] if len(clip) > 5 else max(0, clip[3] - clip[2]))
        score = len(text) + dur * 3.0
        if _is_close(clip):
            score += 30
        if _is_hook(clip):
            score += 6
        return score

    for i in range(len(clips)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(clips)):
            if not keep[j]:
                continue
            ci, cj = clips[i], clips[j]
            si, ei = float(ci[2]), float(ci[3])
            sj, ej = float(cj[2]), float(cj[3])
            overlap = max(0.0, min(ei, ej) - max(si, sj))
            if overlap <= 0:
                continue
            shorter = max(0.1, min(ei - si, ej - sj))
            overlap_ratio = overlap / shorter
            ti = _clean_text(ci[1])
            tj = _clean_text(cj[1])
            contained_text = bool(ti and tj and (ti in tj or tj in ti))
            text_overlap = 0.0
            if ti and tj:
                set_i = set(ti)
                set_j = set(tj)
                text_overlap = len(set_i & set_j) / max(1, min(len(set_i), len(set_j)))
            # 语义承接后相邻卖点常出现 45%-70% 的时间重叠，比如 6:42-6:51 与 6:47-6:54。
            # 只要文本也高度相似，就保留更完整的一段，避免重复口播。
            if overlap_ratio < 0.45 and not contained_text:
                continue
            if overlap_ratio < 0.72 and not contained_text and text_overlap < 0.55:
                continue

            if _is_close(ci) and not _is_close(cj):
                drop = j
            elif _is_close(cj) and not _is_close(ci):
                drop = i
            elif _score_keep(ci) >= _score_keep(cj):
                drop = j
            else:
                drop = i
            keep[drop] = False
            _log(f"扩展重叠去重: 移除片段{drop+1}({clips[drop][2]:.1f}-{clips[drop][3]:.1f}s)，避免重复口播")
            if drop == i:
                break

    filtered = [clip for idx, clip in enumerate(clips) if keep[idx]]
    if len(filtered) != len(clips):
        _log(f"扩展重叠去重: {len(clips)} -> {len(filtered)}")
    return filtered


def _finalize_clip_structure(clips, source_clips=None, log_fn=None, target_duration=None):
    """Final gate before preview/cutting: keep structure stable after boundary fixes."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips:
        return clips

    finalized = list(clips)
    source_clips = list(source_clips or [])
    repairs = []

    def _overlap(a, b):
        try:
            return max(float(a[2]), float(b[2])) < min(float(a[3]), float(b[3])) - 0.1
        except Exception:
            return False

    def _with_type(clip, new_type):
        values = list(clip)
        if values:
            values[0] = new_type
        return tuple(values)

    def _close_candidate_score(clip):
        text = re.sub(r"\[[vV]\d+\]\s*", "", str(clip[1] if len(clip) > 1 else ""))
        compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text).lower()
        if not compact:
            return -1.0
        bad_words = (
            "看下后台", "看看后台", "库存哈", "有没有库存", "稍微等我", "等一下",
            "我去看下", "我去看一下", "多少钱", "价格", "福利价", "破价",
        )
        if any(word in text for word in bad_words):
            return -1.0
        score = 0.0
        action_words = (
            "拍下", "直接拍", "去拍", "放心拍", "拍回去", "下单", "上车",
            "小黄车", "链接", "闭眼入", "闭眼冲", "直接入", "入手", "可以拍",
            "赶紧", "抓紧",
        )
        recommend_words = (
            "建议大家", "推荐大家", "我建议", "我推荐", "值得", "放心", "安心",
            "闭眼", "不踩雷", "自留", "必入", "买回去", "喜欢的",
        )
        size_words = (
            "尺码", "码数", "报一下", "报尺码", "s码", "m码", "l码", "xl",
            "身高", "体重", "斤", "卡码", "往大拍", "往小拍", "穿到",
        )
        scene_wrap_words = (
            "适合", "通勤", "上班", "约会", "旅游", "度假", "出门", "日常",
            "穿出去", "穿去",
        )
        if any(word in text for word in action_words):
            score += 8
        if any(word in text for word in recommend_words):
            score += 5
        if any(word in compact for word in size_words):
            score += 6
        if re.search(r"[sml]\s*码", compact) and "斤" in compact:
            score += 8
        if any(word in text for word in scene_wrap_words):
            score += 3
        if _is_close_clip(clip):
            score += 1
        dur = _clip_duration_value(clip)
        if 3.0 <= dur <= 14.0:
            score += 1
        if text.rstrip().endswith(("然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实")):
            score -= 4
        return score

    def _is_true_close_candidate(clip, min_score=5.0):
        return _close_candidate_score(clip) >= min_score

    def _hook_candidate_score(clip):
        try:
            hook_words = load_keywords().get("hook_keywords", [])
        except Exception:
            hook_words = []
        return _final_hook_quality_score(
            clip[1] if len(clip) > 1 else "",
            _clip_duration_value(clip),
            hook_words,
            None,
            None,
        )[0]

    def _is_true_hook_candidate(clip, min_score=20.0):
        return _hook_candidate_score(clip) >= min_score

    def _can_append_close(clip):
        if _clip_overlaps_any(clip, finalized):
            return False
        if not target_duration:
            return True
        try:
            _low, high = _multi_version_target_bounds(target_duration)
            return sum(_clip_duration_value(c) for c in finalized) + _clip_duration_value(clip) <= high + 2.0
        except Exception:
            return True

    demoted_close_count = 0
    for idx, clip in enumerate(list(finalized)):
        if _is_close_clip(clip) and not _is_true_close_candidate(clip):
            finalized[idx] = _with_type(clip, "product")
            demoted_close_count += 1
    if demoted_close_count:
        repairs.append(f"降级弱Close{demoted_close_count}段")

    demoted_hook_count = 0
    for idx, clip in enumerate(list(finalized)):
        if _is_hook_clip(clip) and not _is_true_hook_candidate(clip):
            finalized[idx] = _with_type(clip, "product")
            demoted_hook_count += 1
    if demoted_hook_count:
        repairs.append(f"降级弱Hook{demoted_hook_count}段")

    source_hooks = [c for c in source_clips if _is_hook_clip(c) and _is_true_hook_candidate(c)]
    source_closes = [c for c in source_clips if _is_close_clip(c) and _is_true_close_candidate(c)]

    if not any(_is_hook_clip(c) for c in finalized):
        restored = False
        for src in source_hooks:
            for idx, clip in enumerate(finalized):
                if _overlap(src, clip):
                    finalized[idx] = _with_type(clip, "hook")
                    repairs.append("恢复Hook")
                    restored = True
                    break
            if restored:
                break
        if not restored and finalized:
            for idx, clip in enumerate(finalized):
                if not _is_close_clip(clip) and _is_true_hook_candidate(clip):
                    finalized[idx] = _with_type(clip, "hook")
                    repairs.append("首段提拔Hook")
                    break
            if not any(_is_hook_clip(c) for c in finalized):
                repairs.append("未找到合格Hook")

    if not any(_is_close_clip(c) for c in finalized):
        restored = False
        for src in reversed(source_closes):
            for idx in range(len(finalized) - 1, -1, -1):
                if _overlap(src, finalized[idx]):
                    finalized[idx] = _with_type(finalized[idx], "close")
                    repairs.append("恢复Close")
                    restored = True
                    break
            if restored:
                break
        if not restored:
            appendable = [src for src in reversed(source_closes) if _can_append_close(src)]
            if appendable:
                best = max(appendable, key=lambda c: (_close_candidate_score(c), _clip_duration_value(c)))
                finalized.append(_with_type(best, "close"))
                repairs.append("补真实Close")
                restored = True
        if not restored and len(finalized) >= 3:
            candidates = []
            for idx, clip in enumerate(finalized):
                if _is_hook_clip(clip):
                    continue
                score = _close_candidate_score(clip)
                if score >= 6.0:
                    candidates.append((score, idx, clip))
            if candidates:
                score, idx, clip = max(candidates, key=lambda item: (item[0], item[1]))
                finalized[idx] = _with_type(clip, "close")
                repairs.append("Product转真实Close")
                restored = True
        if not restored:
            repairs.append("未找到真实Close")

    hooks = [c for c in finalized if _is_hook_clip(c)]
    closes = [c for c in finalized if _is_close_clip(c)]
    others = [c for c in finalized if not _is_hook_clip(c) and not _is_close_clip(c)]
    if hooks or closes:
        finalized = (hooks[:1] if hooks else []) + others + closes
        finalized = _reorder_product_focus_blocks(
            finalized,
            log_fn,
            preferred_focus=_current_focus_used_label(),
        )

    total = sum(_clip_duration_value(c) for c in finalized)
    target_msg = ""
    if target_duration:
        try:
            low, high = _multi_version_target_bounds(target_duration)
            target_msg = f", 目标={low:.0f}-{high:.0f}s"
        except Exception:
            target_msg = ""
    repair_msg = f", 修复={','.join(repairs)}" if repairs else ""
    _log(f"最终片单: {len(finalized)}段, {total:.1f}s, Hook={'有' if hooks else '无'}, Close={'有' if closes else '无'}{target_msg}{repair_msg}")
    return finalized


def _split_long_clips(clips, srt_entries=None, log_fn=None, max_product_sec=20.0, max_hook_sec=10.0, max_close_sec=15.0):
    """Split oversized clips only at safe SRT semantic boundaries."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or not srt_entries:
        return clips

    weak_endings = (
        "然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实",
        "这个", "这款", "这件", "它是", "它会", "你会", "你看", "的话",
        "我觉得", "感觉", "有没有发现", "你去", "去",
        "的", "了", "呀", "呢", "吧", "咯", "啊", "哈", "啦", "嘛",
    )
    continuation_starts = (
        "然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实",
        "它", "这个", "这款", "这件", "你看", "是的", "对", "没错",
    )

    def _norm_entry_text(value):
        return re.sub(r"\[V\d+\]", "", str(value or "")).strip()

    def _entry_has_safe_end(value):
        txt = _norm_entry_text(value).rstrip("，,、；;：: ")
        if not txt:
            return False
        if txt.endswith(weak_endings):
            return False
        return True

    def _entry_starts_continuation(value):
        txt = _norm_entry_text(value)
        return bool(txt) and txt.startswith(continuation_starts)

    result = []
    split_count = 0
    for clip in clips:
        if len(clip) < 6:
            result.append(clip)
            continue
        ctype = str(clip[0] or "").lower()
        limit = max_product_sec
        if "hook" in ctype:
            limit = max_hook_sec
        elif "close" in ctype or ctype in ("cta", "call_to_action", "urgency"):
            limit = max_close_sec
        try:
            start = float(clip[2])
            end = float(clip[3])
            duration = float(clip[5])
        except Exception:
            result.append(clip)
            continue
        if duration <= limit + 0.2:
            result.append(clip)
            continue

        text = str(clip[1] if len(clip) > 1 else "")
        marker_match = re.search(r"\[[vV]\d+\]", text)
        marker = marker_match.group(0).upper() if marker_match else ""
        entries = []
        for entry in srt_entries or []:
            if len(entry) < 3:
                continue
            try:
                es = float(entry[0])
                ee = float(entry[1])
            except Exception:
                continue
            et = str(entry[2] or "")
            if marker and marker not in et.upper():
                continue
            if ee <= start + 0.05 or es >= end - 0.05:
                continue
            entries.append((max(start, es), min(end, ee), et))
        if len(entries) < 2:
            result.append(clip)
            continue

        chunks = []
        current = []
        current_start = None
        for entry in entries:
            es, ee, _text = entry
            if not current:
                current = [entry]
                current_start = es
                continue
            projected_end = ee
            if current_start is not None and projected_end - current_start > limit and current:
                if not _entry_has_safe_end(current[-1][2]) or _entry_starts_continuation(_text):
                    current.append(entry)
                    continue
                chunks.append(current)
                current = [entry]
                current_start = es
            else:
                current.append(entry)
        if current:
            chunks.append(current)
        if len(chunks) <= 1:
            result.append(clip)
            continue

        for chunk in chunks:
            cs = float(chunk[0][0])
            ce = float(chunk[-1][1])
            if ce - cs < 0.8:
                continue
            values = list(clip)
            values[1] = "".join(str(item[2] or "") for item in chunk).strip() or text
            values[2] = cs
            values[3] = ce
            values[5] = max(0.0, ce - cs)
            result.append(tuple(values))
        split_count += 1

    if split_count:
        _log(f"长片段拆分: 拆分 {split_count} 个过长片段，{len(clips)} -> {len(result)}")
    return result


def _restore_director_clip_srt_boundaries(clips, cleaned_srt, log_fn=None):
    """Only restore timestamps cut inside one SRT entry; keep AI order and choices."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or not cleaned_srt:
        return clips
    entries = _parse_srt_entries_for_hook(cleaned_srt)
    if not entries:
        return clips

    restored = []
    fix_count = 0
    for clip in clips:
        if not isinstance(clip, (list, tuple)) or len(clip) < 6:
            restored.append(clip)
            continue
        try:
            start = float(clip[2])
            end = float(clip[3])
        except Exception:
            restored.append(clip)
            continue

        text = str(clip[1] or "")
        marker = re.search(r"\[V\d+\]", text)
        scoped_entries = entries
        if marker:
            scoped = [entry for entry in entries if marker.group(0) in entry[2]]
            if scoped:
                scoped_entries = scoped

        new_start = start
        new_end = end
        max_end = max(e for _, e, _ in scoped_entries) + 0.5
        if new_end > max_end:
            new_end = max_end
            new_start = min(new_start, new_end - 1.0)
            fix_count += 1

        for s, e, _ in scoped_entries:
            if abs(s - start) < 0.3:
                break
            if s < start < e:
                if start - s <= 3.0:
                    new_start = s
                    fix_count += 1
                break

        for s, e, _ in scoped_entries:
            if abs(e - end) < 0.3:
                break
            if s < end < e:
                if e - end <= 8.0:
                    new_end = e
                    fix_count += 1
                break

        if new_end - new_start < 0.5:
            restored.append(tuple(clip))
            continue
        values = list(clip)
        values[2] = new_start
        values[3] = new_end
        values[5] = max(0.0, new_end - new_start)
        restored.append(tuple(values))

    if fix_count:
        before = sum(_clip_duration_value(c) for c in clips)
        after = sum(_clip_duration_value(c) for c in restored)
        _log(f"AI边界验收: 恢复 {fix_count} 处SRT内截断，时长 {before:.1f}s -> {after:.1f}s")
    return restored


def _repair_director_context_edges(clips, cleaned_srt, log_fn=None):
    """Expand only adjacent SRT entries when a selected clip plainly starts/ends mid-thought."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or not cleaned_srt:
        return clips
    entries = _parse_srt_entries_for_hook(cleaned_srt)
    if not entries:
        return clips

    def _strip_marker(value):
        return re.sub(r"\[[vV]\d+\]\s*", "", str(value or "")).strip()

    def _clip_scope(text):
        marker = re.search(r"\[V\d+\]", str(text or ""), flags=re.I)
        if not marker:
            return "", entries
        tag = marker.group(0).upper()
        scoped = [entry for entry in entries if tag in str(entry[2]).upper()]
        return tag, scoped or entries

    def _touched_range(scoped, start, end):
        touched = []
        for idx, (s, e, _text) in enumerate(scoped):
            overlap = max(0.0, min(e, end) - max(s, start))
            boundary_hit = abs(s - start) < 0.35 or abs(e - end) < 0.35
            if overlap > 0.05 or boundary_hit:
                touched.append(idx)
        if not touched:
            return None
        return touched[0], touched[-1]

    repaired = []
    prev_count = 0
    next_count = 0
    for clip in clips:
        if not isinstance(clip, (list, tuple)) or len(clip) < 6:
            repaired.append(clip)
            continue
        ct, text, start, end, score, dur = clip[:6]
        rest = tuple(clip[6:])
        try:
            start_f = float(start)
            end_f = float(end)
        except Exception:
            repaired.append(clip)
            continue
        tag, scoped = _clip_scope(text)
        touched = _touched_range(scoped, start_f, end_f)
        if not touched:
            repaired.append(clip)
            continue
        first, last = touched
        new_first, new_last = first, last
        new_text = str(text or "")

        needs_prev, _starts_hard, needs_next = _director_context_boundary_flags(new_text)
        if needs_prev and new_first > 0:
            ps, pe, _pt = scoped[new_first - 1]
            if start_f - pe <= 1.6 and end_f - ps <= max(4.0, (end_f - start_f) + 8.0):
                new_first -= 1
                prev_count += 1

        for _ in range(2):
            pieces = [_strip_marker(scoped[idx][2]) for idx in range(new_first, new_last + 1)]
            candidate_text = "".join(piece for piece in pieces if piece)
            if tag:
                candidate_text = f"{tag} {candidate_text}".strip()
            _needs_prev, _starts_hard, needs_next = _director_context_boundary_flags(candidate_text)
            if not needs_next or new_last >= len(scoped) - 1:
                break
            ns, ne, _nt = scoped[new_last + 1]
            if ns - end_f > 1.8:
                break
            if ne - scoped[new_first][0] > max(8.0, (end_f - start_f) + 10.0):
                break
            new_last += 1
            next_count += 1

        if new_first != first or new_last != last:
            pieces = [_strip_marker(scoped[idx][2]) for idx in range(new_first, new_last + 1)]
            new_text = "".join(piece for piece in pieces if piece).strip()
            if tag:
                new_text = f"{tag} {new_text}".strip()
            new_start = float(scoped[new_first][0])
            new_end = float(scoped[new_last][1])
            repaired.append((ct, new_text, new_start, new_end, score, max(0.0, new_end - new_start), *rest))
        else:
            repaired.append(clip)

    if prev_count or next_count:
        before = sum(_clip_duration_value(c) for c in clips)
        after = sum(_clip_duration_value(c) for c in repaired)
        _log(f"AI语义边界补齐: 前补{prev_count}处，后补{next_count}处，时长 {before:.1f}s -> {after:.1f}s")
    return repaired


def _fix_clip_boundaries(clips, cleaned_srt, log_fn=None):
    """
    检查每个片段的 start/end 是否切割了完整的 SRT 句子.
    如果切割了，自动扩展边界到最近的 SRT 句子边界.
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips:
        return clips

    if not cleaned_srt:
        return clips
    source_clips_for_structure = list(clips)

    # 解析 SRT 为 entries: [(start_s, end_s, text), ...]
    entries = []
    lines = cleaned_srt.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(
            r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
            line)
        if m:
            start_s = (int(m.group(1))*3600 + int(m.group(2))*60 +
                       int(m.group(3)) + int(m.group(4))/1000.0)
            end_s = (int(m.group(5))*3600 + int(m.group(6))*60 +
                     int(m.group(7)) + int(m.group(8))/1000.0)
            text = ""
            j = i + 1
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                text += lines[j].strip()
                j += 1
            entries.append((start_s, end_s, text.strip()))
            i = j
        else:
            i += 1

    if not entries:
        return clips

    # [PATCH] SRT max end time as safety ceiling
    srt_max_end = max(e for s, e, t in entries) + 0.5

    fixed_clips = []
    fix_count = 0

    for ct, text, start, end, score, dur, *_ in clips:
        new_start = start
        new_end = end
        clip_entries = entries
        marker = re.search(r"\[V\d+\]", str(text or ""))
        if marker:
            scoped = [entry for entry in entries if marker.group(0) in entry[2]]
            if scoped:
                clip_entries = scoped
        clip_srt_max_end = max(e for s, e, t in clip_entries) + 0.5

        # [PATCH] Safety: clamp clip time to SRT range
        if end > clip_srt_max_end:
            new_end = min(end, clip_srt_max_end)
            new_start = min(start, clip_srt_max_end - 1.0)
            if new_start >= new_end:
                new_start = max(0, new_end - dur)
            if new_end - new_start < 2.0:
                fix_count += 1
                continue

        # 检查 start 是否在某个 SRT entry 的中间(而非起始点)
        # 任何类型都优先保证句子完整；Hook如果从半句话中间开始，用户听感会非常割裂。
        for s, e, t in clip_entries:
            if abs(s - start) < 0.3:
                break
            if s < start < e:
                # start在句子中间→回退到句子开头，保证完整句
                if start - s <= 3.0:
                    new_start = s
                    fix_count += 1
                break

        # 检查 end 是否在某个 SRT entry 的中间(而非结束点)
        # Close不做后向截断，确保结尾完整不丢字
        is_close = 'close' in ct.lower()
        for s, e, t in clip_entries:
            if abs(e - end) < 0.3:
                break
            if s < end < e:
                # 任何片段：end在句子中间→延伸到句子末尾，保证完整句
                # 适度放宽延伸，避免口播长句被切成半句。
                if e - end <= 8.0:
                    new_end = e
                    fix_count += 1
                break

        # [PATCH] Secondary clamp after boundary fix
        if new_end > clip_srt_max_end:
            new_end = clip_srt_max_end
            if new_end - new_start < 2.0:
                continue

        new_dur = new_end - new_start
        fixed_clips.append((ct, text, new_start, new_end, score, new_dur, *_))

    if fix_count:
        total_before = sum(c[5] for c in clips)
        total_after = sum(c[5] for c in fixed_clips)
        _log(f"边界修复: 修复 {fix_count} 处截断，时长 {total_before:.1f}s -> {total_after:.1f}s")

        # 不在这里按时间重叠截断。智能成片/混剪都可能有非时间顺序编排，
        # 中点截断会把刚修好的完整句再次切成半句话。

    # [语义承接] 火山 SRT 往往是一句口播拆成多个 2-4 秒条目。
    # AI 只选中前一条时，单条本身没有截断，但听起来会像“刚说完上句就跳走”。
    # 这里只按相邻 SRT 补充前后承接，不按视频来源重排，避免破坏混剪节奏。
    def _norm_text(txt):
        return re.sub(r"\[V\d+\]", "", str(txt or "")).strip()

    try:
        _filler_words_for_tail = set(_get_keywords().get("filler_words", []))
    except Exception:
        _filler_words_for_tail = set()
    _pure_tail_fillers = {
        "是的", "对", "对的", "好的", "好", "嗯", "嗯嗯", "啊", "哦", "噢",
        "呃", "额", "没错", "可以", "行", "好的呀", "好的是的",
    }

    def _clean_for_filler(txt):
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", _norm_text(txt))

    def _is_pure_filler_entry(txt):
        t = _clean_for_filler(txt)
        if not t:
            return True
        if t in _pure_tail_fillers or t in _filler_words_for_tail:
            return True
        filler_chars = set("对嗯啊哦噢呃额哈呀呢嘛啦哇好是的没错可以行")
        return len(t) <= 4 and set(t) <= filler_chars

    def _strip_tail_filler_text(clip_text, tail_text):
        value = str(clip_text or "").rstrip()
        tail = _norm_text(tail_text).rstrip()
        if tail and value.endswith(tail):
            return value[: -len(tail)].rstrip()
        return value

    def _starts_as_continuation(txt):
        t = _norm_text(txt)
        if _is_pure_filler_entry(t):
            return False
        return t.startswith((
            "然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实",
            "它", "这个", "这款", "这件", "这个款", "你看",
            "是的", "对", "没错", "看到吗", "看到了吗", "有没有发现"
        ))

    def _ends_need_context(txt):
        t = _norm_text(txt).rstrip("。！？!?，,、 ")
        if not t:
            return False
        weak_suffixes = (
            "然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实",
            "这个", "这款", "这件", "它是", "它会", "你会", "你看", "的话",
            "就是对于", "来讲的话", "一点", "一点点", "有点", "给你们",
            "我觉得", "感觉", "看到没有", "有没有发现", "你去", "去"
        )
        if t.endswith(weak_suffixes):
            return True
        # 很短的条件/铺垫句通常需要后一句给结论，比如“这个腰带一系的话”。
        return len(t) <= 18 and (
            t.endswith(("的话", "如果", "因为", "所以", "然后"))
            or any(k in t for k in ("一系的话", "穿上的话", "来讲的话"))
        )

    def _looks_complete_short_unit(txt):
        t = _norm_text(txt).rstrip("。！？!?，,、 ")
        if not t:
            return False
        if _ends_need_context(t):
            return False
        complete_markers = (
            "显瘦", "好看", "舒服", "高级", "适合", "遮肉", "藏肉", "修饰",
            "显腿长", "显高", "不挑人", "不扎", "不闷", "透气", "很软",
            "很薄", "很搭", "有质感", "有氛围", "比例", "肩窄", "腿长"
        )
        if _starts_as_continuation(t) and not any(k in t for k in complete_markers):
            return False
        return len(t) >= 6 and any(k in t for k in complete_markers)

    def _clip_source_key(clip):
        return str(clip[7] if len(clip) > 7 else "")

    def _merge_adjacent_semantic_clips(items):
        if len(items) < 2:
            return items, 0
        merged = []
        merge_count = 0
        for clip in items:
            if not merged:
                merged.append(clip)
                continue
            prev = merged[-1]
            ptype, ptext, ps, pe, pscore, _pdur, *prest = prev
            ctype, ctext, cs, ce, cscore, _cdur, *crest = clip
            ptype_l = str(ptype).lower()
            ctype_l = str(ctype).lower()
            if "hook" in ptype_l or "close" in ptype_l or "hook" in ctype_l or "close" in ctype_l:
                merged.append(clip)
                continue
            if _is_pure_filler_entry(ctext):
                merged.append(clip)
                continue
            try:
                gap = float(cs) - float(pe)
            except Exception:
                gap = 999.0
            same_source = _clip_source_key(prev) == _clip_source_key(clip)
            same_block = _clip_focus_block(prev) == _clip_focus_block(clip)
            starts_connected = _starts_as_continuation(ctext)
            short_gap = -0.05 <= gap <= 0.9
            combined_dur = float(ce) - float(ps)
            if same_source and same_block and short_gap and combined_dur <= 15.0 and (starts_connected or combined_dur <= 12.0):
                new_text = f"{ptext}{ctext}"
                new_rest = prest if len(prest) >= len(crest) else crest
                merged[-1] = (ptype, new_text, ps, ce, max(float(pscore), float(cscore)), ce - ps, *new_rest)
                merge_count += 1
            else:
                merged.append(clip)
        return merged, merge_count

    def _semantic_trim_after_context(items):
        if not items:
            return items
        try:
            _target_low, target_high = _multi_version_target_bounds(_AI_TARGET_DURATION)
        except Exception:
            target_high = float(_AI_TARGET_DURATION + max(5, _AI_TARGET_DURATION // 6))

        total = sum(_clip_duration_value(c) for c in items)
        if total <= target_high:
            return items

        kept = list(items)
        _log(f"语义回收: {total:.1f}s 超出上限{target_high:.0f}s，开始回收")

        stop_words = set("的 了 在 是 我 有 和 就 都 也 不 人 这 那 他 到 说 要 会 着 过 把 得 能 可以 很 被 让 给 比 从 向 还 又 而 但 如果 因为 所以 虽然 但是 而且 或者 以及 一个 一些 什么 这个 那个 这些 那些 哪 几 多少 呢 呀 啊 然后 你看 其实 就是".split())

        def _clean(txt):
            return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", _norm_text(txt))

        def _tokens(txt):
            cleaned = _clean(txt)
            return {ch for ch in cleaned if ch.strip() and ch not in stop_words}

        def _is_boundary(clip):
            return _is_hook_clip(clip) or _is_close_clip(clip)

        def _is_weak_close(clip):
            if not _is_close_clip(clip):
                return False
            txt = _norm_text(clip[1] if len(clip) > 1 else "")
            strong = ("拍", "下单", "链接", "上车", "值得", "好看", "适合", "闭眼", "放心", "推荐", "一定要")
            weak = ("s码", "m码", "l码", "xl", "斤", "尺码", "身高", "体重", "可以穿到")
            return any(k in txt for k in weak) and not any(k in txt for k in strong)

        def _starts_weak(clip):
            txt = _norm_text(clip[1] if len(clip) > 1 else "")
            if _clip_duration_value(clip) >= 5.5:
                return False
            return _starts_as_continuation(txt) or txt.startswith(("很搭", "这样的", "这种", "像这种"))

        def _novelty(idx):
            block = _clip_focus_block(kept[idx])
            current = _tokens(kept[idx][1] if len(kept[idx]) > 1 else "")
            others = set()
            for j, other in enumerate(kept):
                if j == idx or _clip_focus_block(other) != block:
                    continue
                others |= _tokens(other[1] if len(other) > 1 else "")
            if not current:
                return 0.0
            return len(current - others) / max(1, len(current))

        def _block_counts(seq):
            counts = {}
            for clip in seq:
                if _is_boundary(clip):
                    continue
                block = _clip_focus_block(clip)
                counts[block] = counts.get(block, 0) + 1
            return counts

        def _removal_candidates():
            counts = _block_counts(kept)
            ranked = []
            for idx, clip in enumerate(kept):
                dur = _clip_duration_value(clip)
                if _is_hook_clip(clip):
                    continue
                block = _clip_focus_block(clip)
                novelty = _novelty(idx)
                text_len = len(_clean(clip[1] if len(clip) > 1 else ""))
                duplicate_pressure = max(0, counts.get(block, 0) - 2)
                score = 0.0
                reason = "弱补充"
                if _is_weak_close(clip):
                    score += 80
                    reason = "弱结尾"
                if _starts_weak(clip):
                    score += 65
                    reason = "短承接句"
                if duplicate_pressure:
                    score += duplicate_pressure * 35
                    reason = f"重复{block}"
                if dur > 14:
                    score += min(25, dur - 12)
                    reason = f"长段{block}"
                if novelty < 0.22 and counts.get(block, 0) > 1:
                    score += 30
                    reason = f"低新增{block}"
                if counts.get(block, 0) <= 1 and not _is_close_clip(clip):
                    score -= 35
                if text_len >= 22 and novelty >= 0.35 and duplicate_pressure <= 0:
                    score -= 20
                if _is_close_clip(clip) and not _is_weak_close(clip):
                    score -= 30
                ranked.append((score, dur, idx, reason, novelty))
            return sorted(ranked, reverse=True)

        removed = []
        while sum(_clip_duration_value(c) for c in kept) > target_high and len(kept) > 4:
            current_total = sum(_clip_duration_value(c) for c in kept)
            over_by = current_total - target_high
            candidates = _removal_candidates()
            candidates = [c for c in candidates if c[0] > 0]
            if not candidates:
                break
            if candidates[0][0] <= 45:
                enough = [c for c in candidates if c[1] >= over_by]
                score, _dur, idx, reason, novelty = min(enough, key=lambda c: c[1]) if enough else candidates[0]
            else:
                score, _dur, idx, reason, novelty = candidates[0]
            clip = kept.pop(idx)
            removed.append((clip, reason, novelty))

        if removed:
            for clip, reason, novelty in removed[:8]:
                _log(f"语义回收: 删除{reason} [{clip[2]:.1f}-{clip[3]:.1f}] {str(clip[1])[:24]} (新增度{novelty:.0%})")
            before = total
            after = sum(_clip_duration_value(c) for c in kept)
            _log(f"语义回收完成: {len(items)}段/{before:.1f}s -> {len(kept)}段/{after:.1f}s")
        else:
            _log(f"语义回收: 未找到可安全删除片段，保留 {len(kept)}段/{total:.1f}s")
        return kept

    def _entry_scope_for_clip(clip_text):
        marker = re.search(r"\[V\d+\]", str(clip_text or ""))
        if not marker:
            return entries
        scoped = [entry for entry in entries if marker.group(0) in entry[2]]
        return scoped or entries

    def _find_entry_span(clip_entries, start, end):
        touched = []
        for idx, (s, e, t) in enumerate(clip_entries):
            overlap = max(0.0, min(e, end) - max(s, start))
            boundary_hit = abs(s - start) < 0.25 or abs(e - end) < 0.25
            if overlap > 0.05 or boundary_hit:
                touched.append(idx)
        if touched:
            return touched[0], touched[-1]
        nearest = min(range(len(clip_entries)), key=lambda i: abs(clip_entries[i][0] - start))
        return nearest, nearest

    def _trim_tail_filler_after_context(items):
        trimmed = []
        trim_count = 0
        for clip in items:
            ct, text, start, end, score, dur, *rest = clip
            clip_entries = _entry_scope_for_clip(text)
            if not clip_entries:
                trimmed.append(clip)
                continue
            first_idx, last_idx = _find_entry_span(clip_entries, start, end)
            new_end = end
            new_text = str(text or "")
            changed = False
            while last_idx >= first_idx:
                s, e, t = clip_entries[last_idx]
                tail_aligned = abs(e - new_end) <= 0.6 or (s < new_end <= e)
                if not tail_aligned or not _is_pure_filler_entry(t):
                    break
                candidate_end = float(clip_entries[last_idx - 1][1]) if last_idx - 1 >= first_idx else float(s)
                candidate_end = max(float(start), candidate_end)
                if candidate_end - float(start) < 2.0:
                    break
                new_end = candidate_end
                new_text = _strip_tail_filler_text(new_text, t)
                last_idx -= 1
                changed = True
                trim_count += 1
            if changed:
                trimmed.append((ct, new_text, start, new_end, score, new_end - start, *rest))
            else:
                trimmed.append(clip)
        if trim_count:
            _log(f"尾部废话裁剪: 去掉 {trim_count} 个纯语气词尾句")
        return trimmed

    contextual_clips = []
    context_fix_count = 0
    context_skip_count = 0
    context_complete_skip_count = 0
    context_tail_filler_skip_count = 0
    context_extra_used = 0.0
    total_before_context = sum(c[5] for c in fixed_clips)
    context_total_cap = float(_AI_TARGET_DURATION + max(5, _AI_TARGET_DURATION // 6))
    for clip in fixed_clips:
        ct, text, start, end, score, dur, *rest = clip
        ctype = str(ct).lower()
        is_priority_boundary = ("hook" in ctype) or ("close" in ctype)
        clip_entries = _entry_scope_for_clip(text)
        if not clip_entries:
            contextual_clips.append(clip)
            continue

        min_context = 3.6 if "hook" in ctype else (5.0 if "close" in ctype else 4.8)
        target_context = 5.5 if "hook" in ctype else (7.0 if "close" in ctype else 6.4)
        max_context = 7.0 if "hook" in ctype else (9.0 if "close" in ctype else 8.5)

        first_idx, last_idx = _find_entry_span(clip_entries, start, end)
        new_start = start
        new_end = end
        pieces = [text]
        changed = False

        first_text = clip_entries[first_idx][2]
        if first_idx > 0 and _starts_as_continuation(first_text):
            ps, pe, pt = clip_entries[first_idx - 1]
            if start - pe <= 1.2 and end - ps <= max_context:
                delta = max(0.0, new_start - ps)
                if total_before_context + context_extra_used + delta <= context_total_cap or is_priority_boundary:
                    new_start = ps
                    pieces.insert(0, pt)
                    context_extra_used += delta
                    changed = True
                else:
                    context_skip_count += 1

        current_dur = new_end - new_start
        complete_short_unit = current_dur < min_context and _looks_complete_short_unit(pieces[-1])
        force_append = current_dur < min_context and not complete_short_unit
        if complete_short_unit:
            context_complete_skip_count += 1
        while not complete_short_unit and last_idx + 1 < len(clip_entries) and current_dur < target_context:
            ns, ne, nt = clip_entries[last_idx + 1]
            gap = ns - new_end
            if gap > 1.5 or ne - new_start > max_context:
                break
            if _is_pure_filler_entry(nt):
                context_tail_filler_skip_count += 1
                break
            needs_semantic_append = force_append or _ends_need_context(pieces[-1]) or _starts_as_continuation(nt)
            if not needs_semantic_append:
                break
            delta = max(0.0, ne - new_end)
            if total_before_context + context_extra_used + delta > context_total_cap and not needs_semantic_append:
                context_skip_count += 1
                break
            new_end = ne
            pieces.append(nt)
            context_extra_used += delta
            last_idx += 1
            changed = True
            current_dur = new_end - new_start
            force_append = False

        if changed:
            context_fix_count += 1
            new_text = "".join(str(p) for p in pieces)
            contextual_clips.append((ct, new_text, new_start, new_end, score, new_end - new_start, *rest))
        else:
            contextual_clips.append(clip)

    if context_fix_count:
        total_before = sum(c[5] for c in fixed_clips)
        total_after = sum(c[5] for c in contextual_clips)
        _log(f"语义承接: 补齐 {context_fix_count} 个短片段上下句，时长 {total_before:.1f}s -> {total_after:.1f}s")
        if context_skip_count:
            _log(f"语义承接: 因目标时长上限跳过 {context_skip_count} 处扩展 (上限{context_total_cap:.0f}s)")
        fixed_clips = contextual_clips
        fixed_clips = _remove_expanded_overlap_clips(fixed_clips, _log)
    elif context_skip_count:
        _log(f"语义承接: 当前时长接近上限，跳过 {context_skip_count} 处普通扩展")
    if context_complete_skip_count:
        _log(f"语义承接: 跳过 {context_complete_skip_count} 个完整短句，不强行补长")
    if context_tail_filler_skip_count:
        _log(f"语义承接: 跳过 {context_tail_filler_skip_count} 个纯语气词尾句")

    fixed_clips = _trim_tail_filler_after_context(fixed_clips)

    fixed_clips, semantic_merge_count = _merge_adjacent_semantic_clips(fixed_clips)
    if semantic_merge_count:
        total_after_merge = sum(c[5] for c in fixed_clips)
        _log(f"语义承接: 合并 {semantic_merge_count} 组相邻同主题短片段，当前 {len(fixed_clips)} 段/{total_after_merge:.1f}s")

    fixed_clips = _trim_tail_filler_after_context(fixed_clips)
    fixed_clips = _semantic_trim_after_context(fixed_clips)

    # [强制排序] hook必须在第一，close必须在最后
    if len(fixed_clips) >= 3:
        hooks = [c for c in fixed_clips if 'hook' in c[0].lower()]
        closes = [c for c in fixed_clips if 'close' in c[0].lower()]
        others = [c for c in fixed_clips if 'hook' not in c[0].lower() and 'close' not in c[0].lower()]
        if hooks or closes:
            fixed_clips = (hooks[:1] if hooks else []) + others + (closes if closes else [])
            _log(f"排序修正: hook首位={bool(hooks)}, close末位={bool(closes)}")

    # [增强] 结尾完整性检查
    if fixed_clips:
        last = fixed_clips[-1]
        ct, text, start, end, score, dur = last[:6]
        t = text.rstrip()
        is_incomplete = False
        # 规则1: 以语气词/助词结尾且句子较短
        weak_endings = ["穿到", "的", "了", "呀", "呢", "吧", "咯", "啊", "哈", "啦", "嘛",
                        "觉得", "感觉", "然后", "就是", "其实", "不过", "而且", "但是"]
        for w in weak_endings:
            if t.endswith(w) and len(t) <= 30:
                is_incomplete = True
                break
        # 规则2: 悬空结尾（后面应有结论但没有）
        if not is_incomplete:
            dangling = ["你会觉得", "就感觉", "就发现", "你就会", "你会看到",
                        "一件", "一套", "一条", "一个", "这个"]
            for p in dangling:
                if t.endswith(p):
                    is_incomplete = True
                    break
        # 规则3: 用SRT实际内容验证片段末尾
        if not is_incomplete and entries:
            for s, e, txt in entries:
                if abs(e - end) < 0.5 and txt:
                    txt_c = txt.rstrip()
                    for w in ["然后", "就是", "其实", "而且", "但是", "不过", "所以"]:
                        if txt_c.endswith(w) and len(txt_c) <= 15:
                            is_incomplete = True
                            break
        if is_incomplete and len(fixed_clips) >= 2:
            _log(f"结尾片段不完整: [{start:.1f}-{end:.1f}] {t[-25:]}")
            fixed_clips.pop()
            total_after = sum(c[5] for c in fixed_clips)
            _log(f"结尾修复: 移除最后片段，剩余 {len(fixed_clips)} 段, 总时长 {total_after:.1f}s")

    return _finalize_clip_structure(
        fixed_clips,
        source_clips=source_clips_for_structure,
        log_fn=_log,
        target_duration=_AI_TARGET_DURATION,
    )


# ============================================================
# 主播互动废话过滤
# ============================================================


# ============================================================
# 明星名字过滤
# ============================================================
CELEBRITY_NAMES = [
    "巩俐", "杨帢", "赵丽颖", "范冰冰", "刘亦菲",
    "周迅", "李小龙", "谢霆锋", "曾毅嘉", "张学友",
    "戴小舩", "薛之谦", "马思纯", "关晓彤", "刘诗诗",
    "孙丽", "曹频幻", "威尔", "克莱尔", "泰勒",
    "小S", "成龙", "黄晓明", "邱毓娜", "秦岚",
    "舒淇", "以别蒋", "王丽坤", "钟楚红", "张宇",
    "刘德华", "邱淇", "刘芳", "邱淇",
]

def _filter_celebrity(clips, log_fn=None):
    """移除包含明星名字的片段"""
    def _log(msg):
        if log_fn: log_fn(msg)
    original = len(clips)
    filtered = []
    for clip in clips:
        text = clip[1]
        hit = any(name in text for name in CELEBRITY_NAMES)
        if hit:
            _log(f"明星过滤: 移除 [{clip[2]:.1f}-{clip[3]:.1f}]")
        else:
            filtered.append(clip)
    if len(filtered) < original:
        _log(f"明星过滤: {original} -> {len(filtered)}")
    return filtered


# ============================================================
# CTA 误判校验
# ============================================================
FAKE_CTA_KEYWORDS = [
    "帮我拿下包包", "看下后台", "库存哈",
    "稍微等我", "等一下哈", "我去看下",
    "有没有库存", "面料库存", "拿下包包",
    "我去看一下", "看看后台",
]

def _validate_cta(clips, log_fn=None):
    """CTA校验:移除误判为CTA的片段，真正的CTA必须包含行动号召关键词"""
    def _log(msg):
        if log_fn: log_fn(msg)

    # 真正CTA关键词：拍下/上车/上链接/321/抢/刷/刷新/点关注/去拍
    REAL_CTA_KW = ["拍下", "上车", "上链接", "321", "抢", "刷新",
                   "点好关注", "去拍", "直接拍", "拍", "下单",
                   "小黄车", "链接", "刷", "入手"]

    original = len(clips)
    filtered = []
    has_real_cta = False
    for clip in clips:
        # close类型豁免CTA检查（尺码引导含"拍"字是正常内容）
        if clip[0] == "close":
            filtered.append(clip)
            continue
        if clip[0] == "call_to_action":
            text = clip[1]
            # 先检查是否包含真正CTA关键词
            is_real = any(kw in text for kw in REAL_CTA_KW)
            if is_real:
                has_real_cta = True
                filtered.append(clip)
                continue
            # 再检查是否匹配假CTA
            is_fake = any(kw in text for kw in FAKE_CTA_KEYWORDS)
            if is_fake:
                _log(f"CTA误判移除: [{clip[2]:.1f}-{clip[3]:.1f}] {text[:20]}")
                continue
            # 既不是真CTA也不匹配假CTA关键词 -> 尺码/无效信息
            _log(f"CTA无效移除(无行动号召): [{clip[2]:.1f}-{clip[3]:.1f}] {text[:20]}")
            continue
        filtered.append(clip)

    removed = original - len(filtered)
    if removed > 0:
        _log(f"CTA校验: {original} -> {len(filtered)}")

    if not has_real_cta and len(filtered) >= 1:
        _log("CTA警告: 没有真正的行动号召片段，结尾可能缺乏CTA力度")

    return filtered


def _trim_product_size_prompt_tails(clips, cleaned_srt, log_fn=None):
    """Trim low-value size-question prompts from the tail of product clips."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips:
        return clips
    if not cleaned_srt:
        return clips

    entries = _parse_srt_entries_for_hook(cleaned_srt)
    if not entries:
        return clips

    def _compact(text):
        text = re.sub(r"\[[vV]\d+\]\s*", "", str(text or ""))
        return re.sub(r"[\s，。！？、,.!?；;：:~～]+", "", text).lower()

    def _is_real_size_table(text):
        compact = _compact(text)
        if re.search(r"[sml]\s*码", compact) and "斤" in compact:
            return True
        return ("可以穿到" in compact or "穿到" in compact) and "斤" in compact

    def _is_size_prompt_tail(text):
        compact = _compact(text)
        if not compact or _is_real_size_table(text):
            return False
        patterns = (
            "有尺码问题", "尺码问题", "码数问题", "尺码抓紧问", "尺码赶紧问",
            "抓紧问尺码", "赶紧问尺码", "可以问尺码", "问尺码",
            "报尺码", "报一下尺码", "尺码再问", "尺码可以问",
        )
        return any(p in compact for p in patterns)

    def _strip_size_prompt_text(text):
        value = str(text or "")
        patterns = [
            r"(?:\[[vV]\d+\]\s*)?[啊哈哦噢嗯,，。 ]*有?尺码问题[^。！？!?，,]{0,12}(?:问|抓紧问|赶紧问)[哦噢啊哈]*[。！？!?，, ]*$",
            r"(?:\[[vV]\d+\]\s*)?[啊哈哦噢嗯,，。 ]*(?:报一下尺码|报尺码|问尺码|尺码可以问|尺码再问)[哦噢啊哈]*[。！？!?，, ]*$",
        ]
        for pattern in patterns:
            value = re.sub(pattern, "", value).strip()
        return value

    trimmed = []
    changed = 0
    removed = 0
    for clip in clips:
        if len(clip) < 6 or _is_hook_clip(clip) or _is_close_clip(clip):
            trimmed.append(clip)
            continue
        ct, text, start, end, score, dur, *rest = clip
        try:
            start_f = float(start)
            end_f = float(end)
        except Exception:
            trimmed.append(clip)
            continue

        marker = re.search(r"\[[vV]\d+\]", str(text or ""))
        scoped_entries = entries
        if marker:
            scoped = [entry for entry in entries if marker.group(0).upper() in entry[2].upper()]
            if scoped:
                scoped_entries = scoped

        overlap_entries = [
            entry for entry in scoped_entries
            if float(entry[1]) > start_f + 0.05 and float(entry[0]) < end_f - 0.05
        ]
        if not overlap_entries:
            trimmed.append(clip)
            continue

        new_end = end_f
        tail_count = 0
        for es, ee, et in reversed(overlap_entries):
            if float(ee) < new_end - 1.5:
                break
            if not _is_size_prompt_tail(et):
                break
            new_end = min(new_end, max(start_f, float(es) - 0.05))
            tail_count += 1

        if tail_count <= 0 or new_end >= end_f - 0.1:
            trimmed.append(clip)
            continue

        new_text = _strip_size_prompt_text(text)
        if new_end - start_f < 2.0:
            removed += 1
            _log(f"尺码尾巴清理: 移除过短Product [{start_f:.1f}-{end_f:.1f}] {str(text)[:24]}")
            continue
        changed += 1
        _log(f"尺码尾巴清理: Product {start_f:.1f}-{end_f:.1f}s -> {start_f:.1f}-{new_end:.1f}s")
        trimmed.append((ct, new_text or text, start_f, new_end, score, new_end - start_f, *rest))

    if changed or removed:
        _log(f"尺码尾巴清理: 裁短 {changed} 段，移除 {removed} 段")
    return trimmed


# ============================================================
# 语义重复过滤: 同一卖点反复出现只保留第一次(代码层兜底)
# ============================================================
def _filter_semantic_repeat(clips, log_fn=None):
    """代码层兜底：检测片段间的语义重复，只保留信息更完整的那条。采用保守策略避免误删。"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        return clips

    _stop = set("的 了 在 是 我 有 和 就 都 也 不 人 这 那 他 到 说 要 会 着 过 把 得 能 可以 很 被 让 给 比 从 向 还 又 而 但 如果 因为 所以 虽然 但是 而且 或者 以及 一个 一些 什么 这个 那个 这些 那些 哪 几 多少 呢 呀 呵 呢 然后 所以说".split())
    _punct = set("，。！？、；：“”‘’（）《》【】…—·")

    def _kw(text):
        return set(c for c in text if c not in _stop and c not in _punct and c.strip())

    original = len(clips)
    keep = []
    kept_kws = []
    for clip in clips:
        ct, text, start, end, score, dur = clip[:6]
        kw = _kw(text)
        if len(kw) < 2:
            keep.append(clip); kept_kws.append(kw); continue
        dup = False
        for pi, pk in enumerate(kept_kws):
            if len(pk) < 2:
                continue
            # 不同clip_type之间不做语义重复过滤（Hook和Product讲同一卖点很正常）
            prev_ct = keep[pi][0] if isinstance(keep[pi], (list, tuple)) else keep[pi].get("clip_type", "")
            if ct != prev_ct:
                continue
            ov = len(kw & pk)
            r = ov / min(len(kw), len(pk))
            if r > 0.7 and ov >= 5:
                dup = True
                _log(f"语义重复: ∼{keep[pi][1][:15]}∼ 与 ∼{text[:15]}∼ 重叠{ov}个(r={r:.0%})")
                break
        if not dup:
            keep.append(clip); kept_kws.append(kw)
    removed = original - len(keep)
    if removed > 0:
        _log(f"语义重复过滤: {original} -> {len(keep)} (去掉{removed}条)")
    return keep


def _filter_focus_near_duplicates(clips, log_fn=None, target_duration=None):
    """Light final pass: remove near-duplicate product clips inside the same selling-point block."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or len(clips) < 4:
        return clips

    try:
        low, _high = _multi_version_target_bounds(target_duration or _AI_TARGET_DURATION)
        min_total_after = max(0.0, float(low) - 4.0)
    except Exception:
        min_total_after = 0.0

    total = sum(_clip_duration_value(c) for c in clips)
    max_remove = 1 if len(clips) <= 10 else 2 if len(clips) <= 16 else 3
    removed = []

    noise_words = (
        "这个", "那个", "然后", "就是", "真的", "其实", "我们", "你们", "它是",
        "的话", "你看", "看到", "感觉", "一下", "可以", "包括", "属于", "整个",
        "非常", "很", "有点", "一个", "这一套", "这件", "这条",
    )
    subtopic_groups = {
        "帽型": ("帽子", "帽型", "不戴帽", "戴帽", "翻领", "双头拉链", "拉链"),
        "藏肉": ("藏肉", "遮肉", "拜拜肉", "显瘦", "胯宽", "苹果型", "肩宽", "大骨架"),
        "肩线显瘦": ("肩线", "肩膀", "肩窄", "往里挖", "肩头"),
        "腰型": ("腰", "腰身", "收腰", "斜裁", "水桶", "x腰", "X腰"),
        "轻薄": ("轻盈", "很薄", "薄透", "不闷", "透肤", "不粘", "凉快", "夏天"),
        "面料": ("面料", "材质", "纱线", "触感", "手感", "柔软", "抗皱", "起球"),
        "场景:度假旅行": ("旅游", "度假", "海边", "云南", "泰兰德", "旅行"),
        "场景:通勤": ("通勤", "上班", "职场", "办公室"),
        "场景:日常出门": ("出门", "日常", "逛街", "街上"),
        "颜色": ("白色", "蓝色", "颜色", "显白", "干净", "清爽", "特别"),
    }

    def _compact(text):
        value = re.sub(r"\[[vV]\d+\]\s*", "", str(text or ""))
        value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
        for word in noise_words:
            value = value.replace(word, "")
        return value.lower()

    def _subtopics(text):
        compact = _compact(text)
        hits = set()
        for name, words in subtopic_groups.items():
            if any(word.lower() in compact for word in words):
                hits.add(name)
        return hits

    def _repeat_score(left, right):
        import difflib
        a = _compact(left)
        b = _compact(right)
        if not a or not b:
            return 0.0
        shorter, longer = sorted((a, b), key=len)
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if len(shorter) >= 8 and shorter in longer:
            ratio = max(ratio, 0.94)
        set_a, set_b = set(a), set(b)
        char_overlap = len(set_a & set_b) / max(1, min(len(set_a), len(set_b)))
        shared_topics = _subtopics(left) & _subtopics(right)
        if shared_topics and char_overlap >= 0.52:
            ratio = max(ratio, 0.72)
        if shared_topics and len(shorter) >= 10 and char_overlap >= 0.66:
            ratio = max(ratio, 0.84)
        # Scene labels are broad. Repeated vacation/travel copy is still a
        # repeat even when the nouns differ (for example, Yunnan vs. beach).
        if shared_topics & {"场景:度假旅行", "场景:通勤", "场景:日常出门", "肩线显瘦"}:
            ratio = max(ratio, 0.84)
        return ratio

    def _keep_strength(clip):
        text = str(clip[1] if len(clip) > 1 else "")
        try:
            score = float(clip[4] if len(clip) > 4 else 0)
        except Exception:
            score = 0.0
        dur = _clip_duration_value(clip)
        compact_len = len(_compact(text))
        strength = score + min(28.0, compact_len / 2.0) + min(18.0, dur * 2.0)
        if _subtopics(text):
            strength += 8.0
        return strength

    result = []
    for clip in clips:
        if len(removed) >= max_remove:
            result.append(clip)
            continue
        if _is_hook_clip(clip) or _is_close_clip(clip):
            result.append(clip)
            continue
        block = _clip_focus_block(clip)
        duplicate_idx = None
        duplicate_score = 0.0
        clip_topics = _subtopics(clip[1] if len(clip) > 1 else "")
        for idx, kept in enumerate(result):
            if kept is None or _is_hook_clip(kept) or _is_close_clip(kept):
                continue
            kept_topics = _subtopics(kept[1] if len(kept) > 1 else "")
            if _clip_focus_block(kept) != block and not (kept_topics & clip_topics):
                continue
            score = _repeat_score(kept[1] if len(kept) > 1 else "", clip[1] if len(clip) > 1 else "")
            if score >= 0.82 or (score >= 0.72 and (kept_topics & clip_topics)):
                duplicate_idx = idx
                duplicate_score = score
                break
        if duplicate_idx is None:
            result.append(clip)
            continue

        previous = result[duplicate_idx]
        drop_current = _keep_strength(previous) >= _keep_strength(clip)
        drop_clip = clip if drop_current else previous
        if total - _clip_duration_value(drop_clip) < min_total_after:
            result.append(clip)
            continue
        total -= _clip_duration_value(drop_clip)
        if drop_current:
            removed.append((drop_clip, block, duplicate_score))
            continue
        result[duplicate_idx] = None
        result.append(clip)
        removed.append((drop_clip, block, duplicate_score))

    filtered = [clip for clip in result if clip is not None]
    if removed:
        for clip, block, score in removed[:5]:
            try:
                start, end = float(clip[2]), float(clip[3])
                span = f"[{start:.1f}-{end:.1f}]"
            except Exception:
                span = ""
            _log(f"同卖点近重复: 移除{block} {span} 相似{score:.0%} {str(clip[1])[:24]}")
        _log(f"同卖点近重复过滤: {len(clips)} -> {len(filtered)}")
    return filtered


def _filter_hook_product_repeats(clips, log_fn=None):
    """Avoid keeping the same sentence as both Hook and Product."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or len(clips) < 2:
        return clips

    def _compact(text):
        text = re.sub(r"\[[vV]\d+\]\s*", "", str(text or ""))
        text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
        noise = ("这个", "那个", "然后", "就是", "真的", "其实", "我们", "你们", "它是", "的话")
        for word in noise:
            text = text.replace(word, "")
        return text

    def _source_key(clip):
        if len(clip) >= 8 and str(clip[7] or "").strip():
            return str(clip[7]).strip()
        marker = re.search(r"\[[vV]\d+\]", str(clip[1] if len(clip) > 1 else ""))
        return marker.group(0).upper() if marker else ""

    def _text_repeat_score(left, right):
        import difflib

        a = _compact(left)
        b = _compact(right)
        if not a or not b:
            return 0.0
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        shorter, longer = sorted((a, b), key=len)
        if len(shorter) >= 8 and shorter in longer:
            ratio = max(ratio, 0.92)
        set_a, set_b = set(a), set(b)
        if set_a and set_b:
            char_overlap = len(set_a & set_b) / max(1, min(len(set_a), len(set_b)))
            if len(shorter) >= 8 and char_overlap >= 0.82:
                ratio = max(ratio, char_overlap * 0.9)
        return ratio

    def _same_timeline(left, right):
        left_src = _source_key(left)
        right_src = _source_key(right)
        return not (left_src and right_src and left_src != right_src)

    def _clip_time(clip):
        try:
            return float(clip[2]), float(clip[3])
        except Exception:
            return 0.0, 0.0

    def _duration(clip):
        try:
            return max(0.0, float(clip[5]))
        except Exception:
            start, end = _clip_time(clip)
            return max(0.0, end - start)

    def _trim_product_around_hook(product, hook):
        ps, pe = _clip_time(product)
        hs, he = _clip_time(hook)
        before = max(0.0, hs - ps - 0.1)
        after = max(0.0, pe - he - 0.1)
        if max(before, after) < 2.0:
            return None
        values = list(product)
        if after >= before:
            new_start, new_end = he + 0.1, pe
        else:
            new_start, new_end = ps, hs - 0.1
        if new_end - new_start < 2.0:
            return None
        values[2] = new_start
        values[3] = new_end
        if len(values) > 5:
            values[5] = new_end - new_start
        return tuple(values)

    hooks = [clip for clip in clips if _is_hook_clip(clip)]
    if not hooks:
        return clips

    result = list(clips)
    changed = False
    for hook in hooks[:1]:
        hs, he = _clip_time(hook)
        hook_dur = max(0.1, _duration(hook))
        for idx, clip in enumerate(list(result)):
            if clip is hook or _is_hook_clip(clip) or _is_close_clip(clip):
                continue
            if len(clip) < 6:
                continue
            ps, pe = _clip_time(clip)
            same_timeline = _same_timeline(hook, clip)
            overlap = max(0.0, min(he, pe) - max(hs, ps)) if same_timeline else 0.0
            overlap_ratio = overlap / max(0.1, min(hook_dur, _duration(clip)))
            text_score = _text_repeat_score(hook[1] if len(hook) > 1 else "", clip[1] if len(clip) > 1 else "")

            repeated_time = overlap >= 0.25 and (
                overlap_ratio >= 0.5
                or (overlap_ratio >= 0.35 and text_score >= 0.45)
            )
            repeated_text = text_score >= 0.86
            if not repeated_time and not repeated_text:
                continue

            if repeated_time:
                trimmed = _trim_product_around_hook(clip, hook)
                if trimmed is not None:
                    result[idx] = trimmed
                    changed = True
                    _log(
                        f"Hook重复: 裁掉卖点与开头重叠部分 "
                        f"[{ps:.1f}-{pe:.1f}]→[{trimmed[2]:.1f}-{trimmed[3]:.1f}]"
                    )
                    continue

            result[idx] = None
            changed = True
            reason = "时间重叠" if repeated_time else f"文案相似{text_score:.0%}"
            _log(f"Hook重复: 移除重复卖点 [{ps:.1f}-{pe:.1f}] ({reason}) {str(clip[1])[:24]}")

    if not changed:
        return clips
    filtered = [clip for clip in result if clip is not None]
    if len(filtered) != len(clips):
        _log(f"Hook重复过滤: {len(clips)} -> {len(filtered)}")
    return filtered


_SAFETY_DIGIT_TRANS = str.maketrans({
    "零": "0", "〇": "0", "一": "1", "幺": "1", "二": "2", "两": "2", "三": "3",
    "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
})


def _safety_text_variants(text):
    """Return text forms used by forbidden/price safety matching."""
    raw = unicodedata.normalize("NFKC", str(text or ""))
    normalized = raw
    replacements = (
        ("鏈接", "链接"),
        ("連結", "链接"),
        ("连结", "链接"),
        ("連接", "链接"),
        ("连按", "链接"),
        ("價", "价"),
        ("優惠", "优惠"),
        ("領券", "领券"),
        ("滿減", "满减"),
        ("號鏈接", "号链接"),
    )
    for old, new in replacements:
        normalized = normalized.replace(old, new)
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9¥￥]+", "", normalized).lower()
    digit_compact = compact.translate(_SAFETY_DIGIT_TRANS)
    return {item for item in (raw, normalized, compact, digit_compact) if item}


def _safety_word_matches(word, text_variants):
    word_variants = _safety_text_variants(word)
    return any(w and t and w in t for w in word_variants for t in text_variants)


def _safety_pattern_matches(patterns, text_variants):
    return any(pattern.search(text) for pattern in patterns for text in text_variants)


# These are phrases whose risk comes from the whole expression, not an isolated
# product word. Keep this separate from the editable forbidden vocabulary so
# terms such as "莱赛尔" or a normal use of "一定" are not falsely blocked.
_CONTENT_SAFETY_PATTERNS = (
    ("CTA关注引导", re.compile(r"点(?:好|个|下|一下|一)?关注")),
    ("CTA关注倒序识别", re.compile(r"关注(?:点好|点一下|一下)")),
    ("CTA冲单引导", re.compile(r"冲一冲")),
    (
        "CTA拍单变体",
        re.compile(r"(?:赶紧|马上|直接)?去(?:来|再|就|直接|赶紧|马上|给我|回来)?拍(?!照|摄)"),
    ),
    ("CTA整套拍单", re.compile(r"拍(?:这|那)(?:一)?套")),
    ("ASR识别残留", re.compile(r"(?:身高|体重|腰围|胸围)[^，。！？!?]{0,12}(?i:asr)")),
    ("CTA链接引导", re.compile(r"(?:上|挂|放)(?:个|下)?(?:链|连)接")),
    ("效果承诺:包出片", re.compile(r"包出片")),
    (
        "赔付承诺:超范围包赔",
        re.compile(
            r"(?:(?:包?赔|赔付)[^，。！？!?]{0,6}(?:到底|到满意|到你满意)|"
            r"(?:无限|一直|全程|兜底)[^，。！？!?]{0,6}(?:包?赔|赔付))"
        ),
    ),
    ("材质安全宣称:母婴级", re.compile(r"母婴(?:店)?级(?:别)?")),
    ("效果承诺:夸张藏肉斤数", re.compile(r"(?:藏|遮)(?:掉|住)?[^，。！？!?]{0,12}(?:十几二十|十几|二十|几十|\d{1,2})斤")),
    ("效果承诺:全给藏掉", re.compile(r"(?:全(?:部)?|都)(?:给你|给)?(?:藏|遮)(?:掉|住)")),
    (
        "效果承诺:绝对化结果",
        re.compile(
            r"(?:一定(?:一定)?|肯定|绝对|保证)(?:会|能|可以)[^，。！？!?]{0,10}"
            r"(?:显瘦|显白|显高|遮肉|藏肉|出片|柔软|舒服|不透|不掉色|不起球|穿)"
        ),
    ),
)


def _content_safety_pattern_matches(text):
    """Return hard content-safety risks that require phrase-level context."""
    variants = _safety_text_variants(text)
    return [
        label
        for label, pattern in _CONTENT_SAFETY_PATTERNS
        if _safety_pattern_matches((pattern,), variants)
    ]


_BACKSTAGE_INSTRUCTION_PATTERNS = (
    re.compile(r"(?:帮我|给我|麻烦你|你帮我).{0,18}(?:拿一下|拿过来|递一下|递过来|给我拿|给我递)"),
    re.compile(r"(?:镜头|摄影|助理|客服|后台).{0,12}(?:拿|递|给|过来|看|切|拉近)"),
    re.compile(r"(?:把|给).{0,8}(?:官搭|搭配).{0,8}(?:包|衣服|裤子|鞋子).{0,8}(?:拿|递|给)"),
    re.compile(r"(?:切|换|放)(?:个|一下)?(?:歌|音乐|镜头)"),
    re.compile(r"(?:我|我们)(?:先|来)?把.{0,12}(?:取了|拿了|摘了|取下来|拿下来)"),
)


def _is_backstage_instruction(text):
    """Detect production-room instructions that are not viewer-facing selling copy."""
    variants = _safety_text_variants(text)
    return _safety_pattern_matches(_BACKSTAGE_INSTRUCTION_PATTERNS, variants)


def _safety_price_cta_patterns():
    return [
        re.compile(r'\d{2,4}\s*[元块]'),           # 199元, 300块
        re.compile(r'[到拿]手[价]?\s*\d'),          # 到手价199, 拿到手99
        re.compile(r'\d{2,4}\s*[多几]?[块元]'),     # 300多块
        re.compile(r'(?:只要|才|仅)[一两三四五六七八九十百千万\d]+[块元]'),
        re.compile(r'原价|秒杀价|福利价|破价|到手价'),
        re.compile(r'[一两三四五六七八九十百千万\d]+[多来几]?[块元]'),
        re.compile(r'[一二三四五六七八九十]\s*折'),
        re.compile(r'半价|对折'),
        re.compile(r'\d+\s*折'),
        re.compile(r'[到拿]手价?\s*[一两三四五六七八九十百千万\d]+'),
        re.compile(r'满减|领券|优惠券|消费券|凑单'),
        re.compile(r'321|三二一|价格'),
        re.compile(r'拍.*链接|链接.*拍|去拍|赶紧拍|刷新拍|往[大小]拍'),
        re.compile(r'上链接|上连结|上連結|连结|連結|链接|号链接|左下角|小黄车|购物车|上车|下单|直接拍'),
    ]


def _clip_safety_matches(text, forbidden_words=None, price_patterns=None):
    if forbidden_words is None:
        try:
            forbidden_words = load_keywords().get("forbidden_phrases", [])
        except Exception:
            forbidden_words = []
    price_patterns = price_patterns or _safety_price_cta_patterns()
    text_variants = _safety_text_variants(text)
    matched_forbidden = [
        str(w or "").strip()
        for w in (forbidden_words or [])
        if str(w or "").strip() and _safety_word_matches(str(w or "").strip(), text_variants)
    ]
    has_price = _safety_pattern_matches(price_patterns, text_variants)
    return matched_forbidden, has_price


def _is_safety_blocked_text(text, forbidden_words=None, price_patterns=None):
    matched_forbidden, has_price = _clip_safety_matches(text, forbidden_words, price_patterns)
    return bool(matched_forbidden or has_price or _content_safety_pattern_matches(text))


_CONTEXT_DAMAGE_WEAK_FORBIDDEN = {
    "一定", "一点", "第一", "第一个", "第一点",
}


def _is_context_blocked_text(text, forbidden_words=None, price_patterns=None):
    matched_forbidden, has_price = _clip_safety_matches(text, forbidden_words, price_patterns)
    if has_price:
        return True
    strict_forbidden = [
        word for word in matched_forbidden
        if str(word or "").strip() not in _CONTEXT_DAMAGE_WEAK_FORBIDDEN
    ]
    return bool(strict_forbidden)


def _compact_context_text(text):
    text = re.sub(r"\[[vV]\d+\]\s*", "", str(text or ""))
    return re.sub(r"[\s，。！？、,.!?；;：:~～\"'“”‘’（）()\[\]【】]+", "", text)


def _looks_like_context_fragment_start(text):
    compact = _compact_context_text(text)
    if not compact:
        return False
    dependent_prefixes = (
        "剩下", "余下", "剩余", "再加", "加上", "另外",
        "而且", "但是", "不过", "所以", "因为", "然后", "就是", "其实",
        "它", "这个", "这款", "这件", "这些", "那种", "这种",
        "对吧", "是的", "没错", "对对对",
    )
    if compact.startswith(dependent_prefixes):
        return True
    # “百分之八弹力/氨纶”这类通常依赖上一句完整成分说明。
    if re.match(r"^(?:百?分之|[一二三四五六七八九十\d]+%|[一二三四五六七八九十\d]+％)", compact):
        return True
    return False


def _filter_context_damaged_clips(clips, cleaned_srt, log_fn=None, min_keep=4):
    """Drop product clips that only survive as a fragment after unsafe context was removed."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not clips or not cleaned_srt:
        return clips

    entries = _parse_srt_entries_for_hook(cleaned_srt)
    if not entries:
        return clips

    try:
        forbidden_words = load_keywords().get("forbidden_phrases", [])
    except Exception:
        forbidden_words = []
    price_patterns = _safety_price_cta_patterns()

    def _scope_entries_for_clip(text):
        marker = re.search(r"\[[vV]\d+\]", str(text or ""))
        if not marker:
            return entries
        marker_text = marker.group(0).upper()
        scoped = [entry for entry in entries if marker_text in str(entry[2]).upper()]
        return scoped or entries

    def _entry_span(clip_entries, start, end):
        touched = []
        for idx, (s, e, _t) in enumerate(clip_entries):
            if float(e) > start + 0.05 and float(s) < end - 0.05:
                touched.append(idx)
        if touched:
            return touched[0], touched[-1]
        try:
            nearest = min(range(len(clip_entries)), key=lambda i: abs(float(clip_entries[i][0]) - start))
            return nearest, nearest
        except Exception:
            return -1, -1

    kept = []
    removed = []
    for idx, clip in enumerate(clips):
        if len(clip) < 6 or _is_hook_clip(clip) or _is_close_clip(clip):
            kept.append(clip)
            continue
        ct, text, start, end, score, dur, *rest = clip
        try:
            start_f = float(start)
            end_f = float(end)
        except Exception:
            kept.append(clip)
            continue
        clip_entries = _scope_entries_for_clip(text)
        if not clip_entries:
            kept.append(clip)
            continue
        first_idx, _last_idx = _entry_span(clip_entries, start_f, end_f)
        if first_idx <= 0:
            kept.append(clip)
            continue

        first_text = str(clip_entries[first_idx][2] or "")
        prev_s, prev_e, prev_text = clip_entries[first_idx - 1]
        gap = float(clip_entries[first_idx][0]) - float(prev_e)
        prev_unsafe = gap <= 1.25 and _is_context_blocked_text(prev_text, forbidden_words, price_patterns)
        if not prev_unsafe:
            kept.append(clip)
            continue

        fragment_start = (
            _looks_like_context_fragment_start(text)
            or _looks_like_context_fragment_start(first_text)
            or len(_compact_context_text(first_text)) <= 4
        )
        if not fragment_start:
            kept.append(clip)
            continue

        projected = kept + [item for item in clips[idx + 1:] if item is not clip]
        if len(projected) < int(min_keep or 0):
            kept.append(clip)
            continue
        removed.append((clip, prev_text))

    if removed:
        for clip, prev_text in removed[:6]:
            _log(
                f"残句过滤: 删除 [{clip[0]}] \"{str(clip[1])[:28]}...\" "
                f"(上一句含违禁/价格，剩余内容依赖上下文: {str(prev_text)[:18]})"
            )
        _log(f"残句过滤: 删除 {len(removed)} 段上下文损坏片段，剩余 {len(kept)} 段")
    return kept


def _filter_price_and_cta(clips, log_fn=None):
    """硬过滤：删除包含价格/报价/购物车/下单/链接的片段，AI Prompt拦不住就用代码拦"""
    def _log(msg):
        if log_fn: log_fn(msg)

    # 价格数字模式：2-4位数字+元/块，或纯数字价格（99/199/299等）
    price_patterns = _safety_price_cta_patterns()
    # 绝对禁止词：从关键词管理读取（用户可自定义）
    _kw_fw = load_keywords()
    forbidden_words = _kw_fw["forbidden_phrases"]

    filtered = []
    removed = 0
    for ct, text, s, e, sc, d, *_ in clips:
        clean = re.sub(r'【|】', '', text)
        # 检查禁止词
        matched_forbidden, has_price = _clip_safety_matches(clean, forbidden_words, price_patterns)
        matched_content = _content_safety_pattern_matches(clean)
        has_forbidden = bool(matched_forbidden)
        if has_forbidden or has_price or matched_content:
            reason = []
            if has_forbidden:
                reason.append(f'违禁词:{",".join(matched_forbidden[:5])}')
            if has_price:
                reason.append('价格模式')
            if matched_content:
                reason.append(f'内容安全:{",".join(matched_content[:3])}')
            removed += 1
            _log(f'  价格过滤: 删除 [{ct}] "{clean[:30]}..." ({";".join(reason)})')
            continue
        else:
            filtered.append((ct, text, s, e, sc, d, *_))

    if removed:
        _log(f"价格硬过滤: 删除 {removed} 段含价格/CTA的片段，剩余 {len(filtered)} 段")
    return filtered

def _filter_host_interaction(clips, log_fn=None):
    """移除纯主播回弹幕的废话片段"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips:
        return clips

    cleaned = []
    removed = 0
    for ct, text, s, e, sc, d, *_ in clips:
        is_noise = False
        reason = ""
        # 检查是否为主播回弹幕/互动（不限时长）
        clean = text.strip()
        if _is_backstage_instruction(clean):
            is_noise = True
            reason = "现场调度"
        else:
            for pattern in HOST_CHAT_PATTERNS:
                if pattern.search(clean):  # 用search替代match，匹配任意位置
                    is_noise = True
                    reason = "主播回弹幕"
                    break
        if is_noise:
            removed += 1
            _log(f"废话过滤: 移除 '{text[:20]}'({d:.1f}s，{reason or '主播回弹幕'})")
        else:
            cleaned.append((ct, text, s, e, sc, d, *_))

    if removed:
        _log(f"废话过滤: 共移除 {removed} 个片段")
    return cleaned


# ============================================================
# 时间顺序重排（黄金链路内同类型按时间先后）
# ============================================================
def _reorder_clips_by_time(clips, log_fn):
    """Only sort consecutive clips that belong to the same narrative block."""
    if len(clips) < 3:
        return clips

    reordered = []
    changed_groups = 0

    def _run_key(clip):
        ctype = str(clip[0]).lower() if clip else ""
        if "hook" in ctype or "close" in ctype:
            return ctype
        return f"{ctype}:{_clip_focus_block(clip)}"

    run = []
    current_key = None
    for clip in clips:
        key = _run_key(clip)
        if current_key is None or key == current_key:
            run.append(clip)
            current_key = key
            continue
        starts = [float(c[2]) for c in run if len(c) > 3]
        sorted_run = sorted(run, key=lambda c: float(c[2]))
        if len(run) >= 2 and starts != sorted(starts):
            changed_groups += 1
            run = sorted_run
        reordered.extend(run)
        run = [clip]
        current_key = key

    if run:
        starts = [float(c[2]) for c in run if len(c) > 3]
        sorted_run = sorted(run, key=lambda c: float(c[2]))
        if len(run) >= 2 and starts != sorted(starts):
            changed_groups += 1
            run = sorted_run
        reordered.extend(run)

    if changed_groups:
        log_fn(f"时间连贯: 已整理 {changed_groups} 个同卖点小组内的顺序")

    # 检查时间跳变：相邻片段时间差超过120s的标记
    for i in range(1, len(reordered)):
        _gap = reordered[i][2] - reordered[i-1][3]  # current start - previous end
        if _gap > 120:
            log_fn(f"  ⚠ 时间跳变 {_gap:.0f}s: [{reordered[i-1][0]}] {reordered[i-1][1][:20]}... → [{reordered[i][0]}] {reordered[i][1][:20]}...")

    return reordered


# ============================================================
# 叙事连贯性检查
# ============================================================
def _check_narrative_coherence(clips, log_fn):
    """后处理:检查叙事连贯性，修补常见问题"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        return clips

    # 1. 相邻片段内容重复检测(简单文本相似度)
    def _text_similarity(t1, t2):
        """简单的字符级 Jaccard 相似度"""
        if not t1 or not t2:
            return 0
        s1, s2 = set(t1), set(t2)
        if not s1 or not s2:
            return 0
        return len(s1 & s2) / len(s1 | s2)

    i = 0
    removed_dup = 0
    while i < len(clips) - 1:
        _, t1, _, _, _, _ = clips[i][:6]
        _, t2, _, _, _, _ = clips[i + 1][:6]
        sim = _text_similarity(t1, t2)
        if sim > 0.6:
            # 保留时长更长的那个
            if clips[i][5] >= clips[i + 1][5]:
                clips.pop(i + 1)
            else:
                clips.pop(i)
                i = max(0, i - 1)
            removed_dup += 1
        else:
            i += 1
    if removed_dup:
        _log(f"叙事检查: 移除 {removed_dup} 个重复片段")

    # 2. 过短片段扩展(<2秒的向前或向后扩展到完整句)
    # 解析 SRT 找到前后时间戳来扩展
    # 这里我们只标记，实际扩展在 _extend_clips 里处理
    short_clips = [i for i, c in enumerate(clips) if c[5] < 2.0]
    if short_clips:
        _log(f"叙事检查: {len(short_clips)} 个片段 <2秒，将尝试扩展")

    # 3. 黄金链路跳跃检测
    chain_types = {c[0] for c in clips}
    chain_idx = {t: i for i, t in enumerate(GOLDEN_CHAIN)}
    used_indices = sorted([chain_idx[t] for t in chain_types if t in chain_idx])
    if len(used_indices) >= 2:
        gaps = []
        for j in range(1, len(used_indices)):
            if used_indices[j] - used_indices[j - 1] > 2:
                gaps.append(f"{GOLDEN_CHAIN[used_indices[j-1]]}→{GOLDEN_CHAIN[used_indices[j]]}")
        if gaps:
            _log(f"叙事检查: 链路跳跃 {', '.join(gaps)}")

    return clips


# ============================================================
# 选片后跨品类扫描(第二道防线)
# ============================================================
def _post_filter_cross_category(clips, cleaned_srt, log_fn, preferred_cat=None):
    """扫描每个片段文本，踢出包含非主品类关键词的片段"""
    def _log(msg):
        if log_fn: log_fn(msg)

    # 构建品类词库（统一复用 PRODUCT_CATEGORIES，避免新增品类漏掉二道过滤）
    ALL_CATEGORIES = PRODUCT_CATEGORIES

    # 从 SRT 统计每个品类的出现频率，确定主品类
    cat_counts = {}
    for cat, keywords in ALL_CATEGORIES.items():
        count = sum(1 for kw in keywords if kw in cleaned_srt)
        if count > 0:
            cat_counts[cat] = count
    # 使用用户指定的主品类（如果有）
    if preferred_cat:
        main_cat = _normalize_forced_category(preferred_cat)
        if main_cat is None and cat_counts:
            main_cat = max(cat_counts, key=cat_counts.get)
        if main_cat is None:
            return clips
    else:
        if not cat_counts:
            return clips
        main_cat = max(cat_counts, key=cat_counts.get)
    main_kws = set(ALL_CATEGORIES.get(main_cat, []))
    match_trigger = {"搭", "配", "搭配", "配着穿", "搭什么", "配什么"}

    def _hit_words(text_value, words):
        return [kw for kw in words if kw and kw in text_value]

    def _hit_strength(words):
        return sum(max(1, len(str(kw))) for kw in words)

    _log(f"跨品类扫描: 主推严格模式，仅保留{main_cat}，跨品类搭配需主品类更强")

    # 扫描每个片段
    kept = []
    removed = 0
    for ct, text, s, e, sc, d, *_ in clips:
        # 检查是否包含非主品类关键词
        has_other = False
        other_cat = None
        for cat, keywords in ALL_CATEGORIES.items():
            if cat == main_cat:
                continue
            # 关联品类不跳过检查，而是统一走"同时有主品类才保留"的逻辑
            for kw in keywords:
                if kw in text:
                    has_other = True
                    other_cat = cat
                    _log(f"跨品类检查 [{ct}] {text[:30]}...(含'{kw}'，非主品类{main_cat})")
                    break
            if has_other:
                break
        # 同时检查是否有主品类关键词(双重确认)
        if has_other:
            main_hits = _hit_words(text, main_kws)
            has_main = bool(main_hits)
            has_match = any(kw in text for kw in match_trigger)
            other_hits = []
            if other_cat:
                other_hits = _hit_words(text, ALL_CATEGORIES.get(other_cat, []))
            main_strength = _hit_strength(main_hits)
            other_strength = _hit_strength(other_hits)
            if main_cat == "套装" and has_main:
                kept.append((ct, text, s, e, sc, d, *_))
            elif has_main and has_match and main_strength > other_strength:
                # 明确搭配，且主品类表达强于次品类，才允许保留。
                kept.append((ct, text, s, e, sc, d, *_))
            else:
                removed += 1
                reason = "缺主品类" if not has_main else "主品类不占优" if has_match else "非搭配混品类"
                _log(f"跨品类踢出 [{ct}] {text[:30]}...({reason}，含{other_cat})")
        else:
            kept.append((ct, text, s, e, sc, d, *_))

    if removed:
        _log(f"跨品类扫描: 踢出 {removed} 个非{main_cat}片段，保留 {len(kept)} 个")
    if _is_food_fresh_category(main_cat):
        kept = _post_filter_food_cross_product(kept, _log)
    return kept


def _cap_clip_duration(clips, log_fn=None, srt_text=None):
    """时长硬顶：超过上限的片段切尾，按SRT句子边界切，保证一句话完整"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips:
        return clips

    try:
        CAP = {
            "hook": 10,
            "product": 20,
            "close": 15,
            "bridge": 12,
            "trend": 12,
        }

        # 句末完整性判断
        SENTENCE_END_CHARS = set("。？！.?!")
        WEAK_ENDINGS = ["然后", "就是", "其实", "而且", "但是", "不过", "所以",
                        "的", "了", "呀", "呢", "吧", "咯", "啊", "哈", "啦", "嘛"]

        def is_complete_sentence(txt):
            """判断SRT条目文本是否以完整语句结尾"""
            if not txt:
                return False
            t = txt.rstrip()
            if not t:
                return False
            last_char = t[-1]
            if last_char in SENTENCE_END_CHARS:
                return True
            for w in WEAK_ENDINGS:
                if t.endswith(w):
                    return False
            if last_char in "，,、；;：:":
                return False
            return True

        # 解析SRT获取(结束时间, 文本)列表
        srt_entries = []  # [(start_s, end_s, text), ...]
        if srt_text:
            for block in srt_text.strip().split(chr(10)+chr(10)):
                lines_b = block.strip().split(chr(10))
                if len(lines_b) >= 1 and '-->' in lines_b[0]:
                    try:
                        parts = lines_b[0].split('-->')
                        start_str = parts[0].strip().split(',')[0]
                        end_str = parts[1].strip().split(',')[0]
                        h, m, s = start_str.split(':')
                        start_s = int(h)*3600 + int(m)*60 + float(s)
                        h, m, s = end_str.split(':')
                        end_s = int(h)*3600 + int(m)*60 + float(s)
                        text = ' '.join(lines_b[1:]).strip() if len(lines_b) > 1 else ''
                        srt_entries.append((start_s, end_s, text))
                    except Exception:
                        pass
        srt_entries.sort(key=lambda x: x[0])

        capped = []
        trim_count = 0
        for ct, text, start, end, score, dur, *_ in clips:
            # 确保 start/end/dur 是 float（AI偶尔返回str）
            start = float(start) if isinstance(start, str) else start
            end = float(end) if isinstance(end, str) else end
            dur = float(dur) if isinstance(dur, str) else dur

            limit = None
            ct_lower = ct.lower()
            for key, val in CAP.items():
                if key in ct_lower:
                    limit = val
                    break
            if limit and dur > limit:
                hard_end = start + limit
                best_end = hard_end
                clip_srt_entries = srt_entries
                marker = re.search(r"\[V\d+\]", str(text or ""))
                if marker:
                    scoped = [entry for entry in srt_entries if marker.group(0) in entry[2]]
                    if scoped:
                        clip_srt_entries = scoped

                # 策略1: 在 [start+2, hard_end] 范围内找完整断句的SRT条目末尾
                if clip_srt_entries:
                    complete_candidates = []
                    for s, e, t in clip_srt_entries:
                        if e < start + 2:
                            continue
                        if e > hard_end:
                            break
                        if is_complete_sentence(t):
                            complete_candidates.append(e)

                    if complete_candidates:
                        best_end = complete_candidates[-1]
                        _log(f"时长硬顶: [{ct}] 在 {hard_end:.1f}s 前找到完整断句 {best_end:.1f}s")
                    else:
                        # 找不到完整断句时不要硬切半句；交给后续边界修复/重叠清理处理。
                        _log(f"时长硬顶: [{ct}] 无完整断句，保留原边界避免半句截断")
                        capped.append((ct, text, start, end, score, dur, *_))
                        continue

                new_dur = best_end - start
                trim_count += 1
                _log(f"时长硬顶: [{ct}] {dur:.1f}s > {limit}s, 切尾到 {start:.1f}-{best_end:.1f}s ({new_dur:.1f}s)")
                capped.append((ct, text, start, best_end, score, new_dur, *_))
            else:
                capped.append((ct, text, start, end, score, dur, *_))

        if trim_count:
            total_before = sum(c[5] for c in clips)
            total_after = sum(c[5] for c in capped)
            _log(f"时长硬顶: 切尾 {trim_count} 段, 总时长 {total_before:.1f}s -> {total_after:.1f}s")
        return capped
    except Exception as e:
        _log(f"时长硬顶: 异常 {e}，返回原始片段")
        return clips


def _extend_clips(clips, log_fn, target_min=45, target_max=65, max_end=None):
    def _log(msg):
        if log_fn: log_fn(msg)
    if not clips:
        return clips
    total = sum(c[5] for c in clips)
    if total >= target_min:
        return clips

    deficit = target_min - total
    _log(f"自动延伸片段: 当前 {total:.1f}s, 目标 {target_min}s, 差 {deficit:.1f}s")

    # 按 start 时间排序，用于计算每个片段的延伸上限
    sorted_clips = sorted(enumerate(clips), key=lambda x: x[1][2])
    clip_end_limits = {}  # idx -> max allowed end (next clip's start - 0.5s gap)
    for k in range(len(sorted_clips)):
        idx_k = sorted_clips[k][0]
        if k + 1 < len(sorted_clips):
            next_start = sorted_clips[k + 1][1][2]
            clip_end_limits[idx_k] = next_start - 0.5  # leave 0.5s gap
        else:
            clip_end_limits[idx_k] = max_end or 99999

    # 按时长从小到大排序，优先延伸最短的片段
    indexed = list(enumerate(clips))
    indexed.sort(key=lambda x: x[1][5])

    for idx, (ct, text, start, end, score, dur) in indexed:
        if total >= target_min:
            break
        max_dur = 15
        if dur >= max_dur:
            continue
        end_limit = clip_end_limits.get(idx, max_end or 99999)
        can_add = min(max_dur - dur, deficit, end_limit - end)
        if can_add <= 0:
            continue
        new_end = end + can_add
        new_dur = new_end - start
        clips[idx] = (ct, text, start, new_end, score, new_dur)
        total += can_add
        deficit -= can_add

    total = sum(c[5] for c in clips)
    _log(f"延伸完成: {len(clips)} 片段, 总时长 {total:.1f}s")
    return clips


# ============================================================
# 品类一致性检测:识别主品类，其他品类片段后置
# ============================================================
# 品类关键词(简体+繁体)
PRODUCT_CATEGORIES = {
    "上衣": ["上衣", "T恤", "衬衫", "针织衫", "卫衣", "打底衫", "小衫", "衬衣",
             "网纱罩衫", "罩衫", "毛衣", "短袖", "长袖", "吊带", "吊带衫",
             "背心", "抹胸", "针织", "开衫", "開衫", "针织开衫", "针织開衫",
             "打底", "襯衫", "馬甲"],
    "裤子": ["裤子", "牛仔裤", "阔腿裤", "打底裤", "工装裤", "休闲裤",
             "长裤", "短裤", "九分裤", "小脚裤", "直筒裤", "运动裤", "西裤",
             "牛奶裤", "烟管裤", "哈伦裤", "裤",
             "牛仔褲", "褲子", "褲", "闊腿褲", "直筒褲"],
    "裙子": ["裙子", "连衣裙", "半身裙", "A字裙", "包臀裙", "长裙", "短裙",
              "百褶裙", "鱼尾裙", "吊带裙", "碎花裙", "蛋糕裙", "一步裙",
              "背心裙", "旗袍裙", "吊带", "腰头", "裙",
              "連衣裙", "半身裙", "百褶裙"],
    "外套": ["外套", "风衣", "西装", "羽绒服", "大衣", "夹克", "棉服", "皮衣",
             "马甲", "風衣", "夾克", "羽絨服"],
    "套装": ["套装", "两件套", "三件套", "四件套", "三件", "四件", "成套",
             "组合", "套装组合", "整套", "全套", "穿搭",
             "兩件套", "三件套"],
    "鞋子": ["鞋", "鞋子", "凉鞋", "运动鞋", "高跟鞋", "平底鞋", "单鞋",
              "靴子", "老爹鞋", "帆布鞋"],
    "食品/生鲜": [
        "食品", "生鲜", "食材", "水果", "蔬菜", "海鲜", "冻品", "预制菜", "零食", "坚果",
        "苹果", "橙子", "橘子", "柑橘", "榴莲", "车厘子", "樱桃", "荔枝", "芒果", "草莓",
        "蓝莓", "葡萄", "蟠桃", "水蜜桃", "桃子", "黄桃", "油桃", "猕猴桃", "奇异果", "西梅", "香梨", "雪梨", "枇杷", "石榴",
        "番茄", "西红柿", "玉米", "红薯", "紫薯", "土豆", "山药", "莲藕", "菌菇", "香菇",
        "虾", "大虾", "鲜虾", "小龙虾", "螃蟹", "大闸蟹", "生蚝", "鲍鱼", "带鱼", "三文鱼",
        "鳕鱼", "鱼片", "牛肉", "牛排", "羊肉", "猪肉", "鸡肉", "鸡翅", "鸡蛋", "牛奶",
        "酸奶", "奶酪", "冷冻", "速冻", "半成品", "烤肠", "水饺", "馄饨", "包子", "面包",
        "蛋糕", "糕点", "饼干", "燕麦", "大米", "五常大米", "粮油", "调味料", "酱油",
        "鲜活", "现摘", "现采", "现捕", "现捞",
        "果园", "农场", "渔港",
        "冷链", "冰袋", "保温箱", "泡沫箱", "保鲜", "锁鲜", "净含量", "净重", "斤装", "箱装",
        "大果", "果径", "个头", "坏果包赔", "破损包赔", "开袋即食", "囤货",
    ],
}

FILENAME_CATEGORY_KEYWORDS = {
    "上衣": [
        "长袖T恤", "短袖T恤", "T恤", "短袖", "长袖", "衬衫", "衬衣", "针织衫",
        "卫衣", "打底衫", "小衫", "罩衫", "开衫", "背心", "抹胸", "毛衣",
    ],
    "裤子": [
        "阔腿裤", "牛仔裤", "休闲裤", "直筒裤", "工装裤", "打底裤", "运动裤",
        "西裤", "长裤", "短裤", "九分裤", "小脚裤", "烟管裤", "哈伦裤", "裤子",
    ],
    "裙子": [
        "连衣裙", "半身裙", "百褶裙", "A字裙", "包臀裙", "鱼尾裙", "蛋糕裙",
        "一步裙", "背心裙", "旗袍裙", "碎花裙", "长裙", "短裙", "纱裙", "裙子",
    ],
    "外套": ["风衣", "羽绒服", "大衣", "夹克", "西装外套", "外套", "皮衣", "棉服"],
    "套装": ["两件套", "三件套", "四件套", "套装", "成套", "整套", "全套"],
    "鞋子": ["运动鞋", "高跟鞋", "平底鞋", "老爹鞋", "帆布鞋", "凉鞋", "靴子", "单鞋", "鞋子"],
}


def infer_category_from_filename(name):
    text = re.sub(r"\s+", "", str(name or ""))
    if not text:
        return None
    scores = {}
    for cat, keywords in FILENAME_CATEGORY_KEYWORDS.items():
        score = 0.0
        for kw in keywords:
            if kw and kw in text:
                weight = 1.0 + min(2.0, len(kw) / 4.0)
                if cat == "套装":
                    weight += 0.7
                score += weight
        if score > 0:
            scores[cat] = score
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_cat, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    if top_cat == "套装" and top_score >= 1.7:
        return top_cat
    if top_score >= 1.45 and (
        second_score <= 0
        or top_score >= second_score + 1.2
        or top_score >= second_score * 1.6
    ):
        return top_cat
    return None

# 品类 → 卖点排序优先级映射
CATEGORY_FOCUS_ORDER = {
    "上衣": ["版型显瘦", "面料质感", "尺寸长度", "穿着体验", "品质细节", "工艺细节", "颜色氛围", "场景搭配", "性价比", "对比优势", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "裤子": ["版型显瘦", "尺寸长度", "品质细节", "面料质感", "穿着体验", "颜色氛围", "场景搭配", "性价比", "对比优势", "工艺细节", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "裙子": ["版型显瘦", "颜色氛围", "尺寸长度", "场景搭配", "品质细节", "面料质感", "穿着体验", "工艺细节", "性价比", "对比优势", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "外套": ["版型显瘦", "品质细节", "面料质感", "穿着体验", "场景搭配", "尺寸长度", "颜色氛围", "工艺细节", "性价比", "对比优势", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "套装": ["场景搭配", "版型显瘦", "颜色氛围", "面料质感", "品质细节", "性价比", "对比优势", "尺寸长度", "穿着体验", "工艺细节", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "鞋子": ["场景搭配", "品质细节", "颜色氛围", "尺寸长度", "面料质感", "性价比", "对比优势", "穿着体验", "工艺细节", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "食品/生鲜": ["口感食欲", "新鲜品质", "产地溯源", "规格分量", "发货保鲜", "场景吃法", "性价比", "情绪感染", "紧迫稀缺", "对比优势", "其他"],
}

DEFAULT_BLOCK_ORDER = ["版型显瘦", "面料质感", "穿着体验", "品质细节", "尺寸长度", "颜色氛围", "场景搭配", "工艺细节", "性价比", "对比优势", "情绪感染", "流行趋势", "紧迫稀缺", "其他"]

# 品类 → AI Prompt 规则映射
CATEGORY_PROMPT_RULES = {
    "上衣": "主推上衣品类。面料最多2段，版型显瘦优先选，其他卖点至少覆盖1段。",
    "裤子": "主推裤子品类。版型显瘦优先选，面料最多1段，穿搭场景至少1段。",
    "裙子": "主推裙子品类。颜色氛围和版型显瘦优先，场景搭配至少1段。",
    "外套": "主推外套品类。版型显瘦和品质细节优先，面料最多2段。",
    "套装": "主推套装品类。场景搭配和整体效果优先于单品卖点。",
    "鞋子": "主推鞋子品类。场景搭配和品质细节优先。",
    "食品/生鲜": "主推食品/生鲜品类。用户指定后不要再因具体品名未命中词库而退回通用/服装逻辑；先识别本场主商品和子类型，再让口感食欲、新鲜品质、产地溯源、规格分量、发货保鲜、场景吃法至少覆盖3类；优先试吃/切开/开箱/产地/冷链/囤货片段；普通食品不得剪成治疗、预防、保健、药用功效卖点。",
}

def _install_vertical_category_profiles():
    """Register verticals once; unknown categories still fall back to general AI rules."""

    def _extend_unique(target, key, values):
        if not values:
            return
        current = list(target.get(key, []))
        seen = set(current)
        for value in values:
            if value and value not in seen:
                current.append(value)
                seen.add(value)
        target[key] = current

    for profile in iter_vertical_profiles():
        _extend_unique(PRODUCT_CATEGORIES, profile.key, profile.product_keywords)
        _extend_unique(FILENAME_CATEGORY_KEYWORDS, profile.key, profile.filename_keywords)
        if profile.focus_order:
            CATEGORY_FOCUS_ORDER[profile.key] = list(profile.focus_order)
        if profile.prompt_rule:
            CATEGORY_PROMPT_RULES[profile.key] = profile.prompt_rule


_install_vertical_category_profiles()


def _detect_product_category(text):
    """检测文本提到的品类，返回品类名或 None"""
    scores = {}
    for cat, keywords in PRODUCT_CATEGORIES.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits > 0:
            scores[cat] = hits
    if not scores:
        return None
    return max(scores, key=scores.get)


def _enforce_product_coherence(clips, log_fn):
    """检测主品类，将其他品类的片段移到末尾，前2个片段强制主品类"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        return clips

    # 统计每个品类出现次数
    cat_count = {}
    for ct, text, s, e, sc, d, *_ in clips:
        cat = _detect_product_category(text)
        if cat:
            cat_count[cat] = cat_count.get(cat, 0) + 1

    if not cat_count:
        return clips  # 无法识别品类

    main_cat = max(cat_count, key=cat_count.get)
    if cat_count[main_cat] < 2:
        return clips  # 没有明显主品类

    # 分类:主品类 vs 其他品类
    main_clips = []
    other_clips = []
    for c in clips:
        cat = _detect_product_category(c[1])
        if cat and cat != main_cat:
            other_clips.append(c)
        else:
            main_clips.append(c)

    if other_clips:
        _log(f"品类检测: 主品类={main_cat}，{len(other_clips)} 个跨品类片段后置")
        clips = main_clips + other_clips

    # 额外保护:前2个片段如果有无法识别品类的，且后面有主品类片段，交换
    if len(clips) >= 3:
        for i in range(min(2, len(clips))):
            c = clips[i]
            cat = _detect_product_category(c[1])
            if cat is None:
                # 找后面最近的主品类片段交换
                for j in range(i + 1, len(clips)):
                    cj_cat = _detect_product_category(clips[j][1])
                    if cj_cat == main_cat:
                        clips[i], clips[j] = clips[j], clips[i]
                        _log(f"品类保护: 位置{i+1}的片段与位置{j+1}交换(确保开头是主品类)")
                        break

    return clips


# ============================================================
# 终剪防线:移除孤立跨品类片段
# ============================================================
def _remove_orphan_cross_category(clips, log_fn):
    """AI 输出片段列表后最终扫描，移除无搭配绑定的跨品类片段"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        return clips

    # 找出出现最多的品类 = 主品类
    cat_count = {}
    for c in clips:
        cat = _detect_product_category(c[1])
        if cat:
            cat_count[cat] = cat_count.get(cat, 0) + 1

    if not cat_count:
        return clips

    main_cat = max(cat_count, key=cat_count.get)
    if cat_count[main_cat] < 2:
        return clips

    main_kws = set(PRODUCT_CATEGORIES.get(main_cat, []))

    cleaned = []
    removed_texts = []
    for c in clips:
        ct, text, s, e, sc, d = c[0], c[1], c[2], c[3], c[4], c[5]
        other_cat = _detect_product_category(text)
        if other_cat and other_cat != main_cat:
            # 跨品类 → 必须同段包含主品类词才合法，搭配词只是辅助信号
            has_main = any(kw in text for kw in main_kws)
            if not has_main:
                removed_texts.append(text)
                continue
        cleaned.append(c)

    if removed_texts:
        for t in removed_texts[:3]:
            _log(f"已移除孤立跨品类片段:{t[:30]}...(未同段绑定主品类)")
        if len(removed_texts) > 3:
            _log(f"  ...共移除 {len(removed_texts)} 个孤立跨品类片段")

    return cleaned


# ============================================================
# 补充片段(AI 结果不足时用关键词自动补齐)
# ============================================================
def _supplement_clips(existing_clips, cleaned_srt, log_fn, min_total=4):
    """从清洗后的字幕中补充片段，直到达到 min_total 个"""
    def _log(msg):
        if log_fn: log_fn(msg)
    if len(existing_clips) >= min_total:
        return existing_clips

    from config import CLIP_KEYWORDS, NEGATIVE_KEYWORDS, BAN_PATTERNS
    _kw_local = _get_keywords()
    FILLER_WORDS = _kw_local["filler_words"]
    NEGATIVE_SIGNALS = _kw_local["negative_signals"]

    def _parse_time(value):
        h, m, s = value.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    # 收集已有片段的时间范围(避免重叠)
    used_ranges = [(c[2], c[3]) for c in existing_clips]

    # 关键词打分
    candidates = []
    for block in re.split(r"\n\s*\n", cleaned_srt.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next((line for line in lines if "-->" in line), "")
        if not time_line:
            continue
        text = " ".join(line for line in lines if "-->" not in line and not line.isdigit()).strip()
        if not text or len(text) < 5:
            continue

        try:
            start_raw, end_raw = time_line.split("-->", 1)
            start = _parse_time(start_raw)
            end = _parse_time(end_raw)
        except Exception:
            continue
        dur = end - start
        if dur < 1.5 or dur > 15:
            continue

        # 检查是否与已有片段重叠
        overlap = False
        for us, ue in used_ranges:
            if max(start, us) < min(end, ue):
                overlap = True; break
        if overlap:
            continue

        # 过滤
        skip = False
        clean_t = text
        for fw in FILLER_WORDS:
            clean_t = clean_t.replace(fw, "")
        for ban in BAN_PATTERNS:
            if re.search(ban, text):
                skip = True; break
        if skip:
            continue
        for sig in NEGATIVE_SIGNALS:
            if sig in text:
                skip = True; break
        if skip:
            continue

        # 打分
        best_type, best_score = "highlight", 0
        for ct, cfg in CLIP_KEYWORDS.items():
            hits = sum(1 for kw in cfg.get("keywords", []) if kw in text)
            score = hits * cfg.get("weight", 20)
            if score > best_score:
                best_score = score
                best_type = ct
        for neg in NEGATIVE_KEYWORDS:
            if neg in text and len(text) < 20:
                best_score -= 15
        if best_score >= 15:
            candidates.append((best_type, text, start, end, best_score, dur))

    # 按分数排序，补充到 min_total
    candidates.sort(key=lambda c: (-c[4], c[2]))  # 分数高优先，时间靠前优先
    existing = list(existing_clips)
    for cand in candidates:
        if len(existing) >= min_total:
            break
        ct, text, s, e, sc, d = cand[0], cand[1], cand[2], cand[3], cand[4], cand[5]
        # 检查重叠
        overlap = False
        for ex in existing:
            if max(s, ex[2]) < min(e, ex[3]):
                overlap = True; break
        if not overlap:
            existing.append(cand)

    if len(existing) > len(existing_clips):
        _log(f"WARNING: 关键词补充: {len(existing_clips)} -> {len(existing)} 片段")
    return existing


# ============================================================
# 兜底逻辑
# ============================================================
def fallback_clips(srt_path, log_fn=None, force_category=None):
    def _log(msg):
        if log_fn: log_fn(msg)

    _log("WARNING: 关键词兖底选片(非AI, 质量可能不佳, 建议检查API后重试)")
    from srt_parser import open_srt
    from config import CLIP_KEYWORDS, NEGATIVE_KEYWORDS, EMOTION_WORDS, BAN_PATTERNS
    _kw_local = _get_keywords()
    FILLER_WORDS = _kw_local["filler_words"]
    NEGATIVE_SIGNALS = _kw_local["negative_signals"]

    try:
        subtitles, _ = open_srt(srt_path)
    except Exception as e:
        _log(f"兜底: SRT 解析失败 {e}"); return []

    scored = []
    for sub in subtitles:
        text = sub.text.strip()
        if not text: continue
        start = sub.start[0]*3600 + sub.start[1]*60 + sub.start[2] + sub.start[3]/1000.0
        end = sub.end[0]*3600 + sub.end[1]*60 + sub.end[2] + sub.end[3]/1000.0
        duration = end - start
        if duration < 1.5 or duration > 12:
            continue
        for fw in FILLER_WORDS:
            text = text.replace(fw, "")
        text = text.strip()
        if len(text) < 5:
            continue
        skip = False
        for ban in BAN_PATTERNS:
            if re.search(ban, text):
                skip = True; break
        if skip:
            continue
        for sig in NEGATIVE_SIGNALS:
            if sig in text:
                skip = True; break
        if skip:
            continue
        best_type, best_score = "highlight", 0
        for ct, cfg in CLIP_KEYWORDS.items():
            hits = sum(1 for kw in cfg.get("keywords", []) if kw in text)
            score = hits * cfg.get("weight", 20)
            if score > best_score:
                best_score = score
                best_type = ct
        for neg in NEGATIVE_KEYWORDS:
            if neg in text:
                best_score -= 20
        if best_score < 15:
            continue
        scored.append({
            "type": best_type, "text": text,
            "start": start, "end": end,
            "score": best_score, "duration": duration,
        })

    result = []
    for ct in SIMPLE_CHAIN:
        cands = [b for b in scored if b["type"] == ct]
        if not cands:
            continue
        best = max(cands, key=lambda b: b["score"])
        result.append((best["type"], best["text"], best["start"], best["end"],
                       best["score"], best["duration"]))
    if result:
        total = sum(c[5] for c in result)
        _log(f"兜底: {len(result)} 片段, 总时长 {total:.1f}s")
        for ct, text, s, e, sc, d in result:
            _log(f"  {ct:<16s} | {s:.1f}-{e:.1f}s ({d:.1f}s) | {text}")
    return result


def ai_reorder_clips(all_clips, log_fn=None):
    """
    Mix mode: reorder clips from multiple videos via AI.
    Input: [{"idx":0, "type":"hook", "text":"...", "source":"videoA", "duration":12.5}, ...]
    Output: sorted index list [3, 7, 1, ...]
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    settings = load_settings()
    if not settings.get("api_key"):
        _log("AI reorder: no API key, using original order")
        return [c["idx"] for c in all_clips]

    api_key = settings["api_key"]
    base_url = normalize_ai_base_url(settings["base_url"])
    model = settings.get("model", DEEPSEEK_DEFAULT_MODEL)

    lines = []
    for c in all_clips:
        _text = c.get("text", "").strip()[:80]
        _type = c.get("type", "unknown")
        _src = c.get("source", "")
        _dur = c.get("duration", 0)
        lines.append(f"[{c['idx']}] \u7c7b\u578b: {_type} | \u6587\u6848: \"{_text}\" | \u6765\u6e90: {_src} | \u65f6\u957f: {_dur:.1f}s")

    clips_desc = chr(10).join(lines)

    system_prompt = """\u4f60\u662f\u76f4\u64ad\u5e26\u8d27\u77ed\u89c6\u9891\u526a\u8f91\u5e08\u3002\u5c06\u4ee5\u4e0b\u7247\u6bb5\u6309\u6700\u4f73\u64ad\u653e\u987a\u5e8f\u6392\u5217\u3002
\u89c4\u5219\uff1ahook\u653e\u6700\u524d \u2192 \u540c\u7c7b\u5356\u70b9\u653e\u4e00\u8d77 \u2192 \u63a8\u8350\u4fc3\u9500\u653e\u6700\u540e\u3002
\u8f93\u51fa\u4e25\u683c JSON \u683c\u5f0f\uff1a{"order": [\u5e8f\u53f71, \u5e8f\u53f72, ...]}\uff0c\u8df3\u8fc7\u7528 -1\u3002"""

    user_prompt = f"\u6392\u5e8f\u8fd9\u4e9b\u7247\u6bb5\uff1a\n\n{clips_desc}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 1024,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(
            ai_chat_completions_url(base_url),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        _log(f"AI reorder failed: {e}, using original order")
        return [c["idx"] for c in all_clips]

    try:
        content = result["choices"][0]["message"]["content"].strip()
        ordered = None
        # Parse as {"order": [...]} (json_object mode)
        try:
            data = json.loads(content)
            ordered = data.get("order") or data
        except:
            pass
        # Fallback: raw array
        if ordered is None:
            try:
                cleaned = re.sub(r'^```(?:json)?\s*', '', content)
                cleaned = re.sub(r'\s*```\s*$', '', cleaned)
                ordered = json.loads(cleaned.strip())
            except:
                pass
        # Fallback: regex array
        if ordered is None:
            try:
                m = re.search(r'\[[\d,\s-]+\]', content)
                if m: ordered = json.loads(m.group())
            except:
                pass
        if ordered is None or not isinstance(ordered, list):
            return _rule_order(all_clips)
        ordered = [int(x) for x in ordered]
        valid_indices = {c["idx"] for c in all_clips}
        ordered = [x for x in ordered if x in valid_indices]
        seen = set(ordered)
        for c in all_clips:
            if c["idx"] not in seen:
                ordered.append(c["idx"])
        _log(f"AI reorder done: {len(ordered)}/{len(all_clips)} clips")
        return ordered
    except Exception as e:
        _log(f"AI reorder parse failed: {e}, using original order")
        return [c["idx"] for c in all_clips]


def detect_main_category(srt_text, force_category=None):
    """
    Detect main product category from SRT text.
    Uses same keywords as _filter_srt_by_main_product.
    Returns category name string, or None.
    """
    CATEGORIES = PRODUCT_CATEGORIES

    if force_category:
        forced = _normalize_forced_category(force_category)
        if forced:
            return forced

    scores = {}
    for cat, kws in CATEGORIES.items():
        score = sum(1 for kw in kws if kw in srt_text)
        if score > 0:
            scores[cat] = score
    if scores:
        return max(scores, key=scores.get)
    return None



# Module-level ALL_CATEGORIES for post-filter code (lazy init from same source)
ALL_CATEGORIES_MODULE = None
def _get_categories():
    global ALL_CATEGORIES_MODULE
    if ALL_CATEGORIES_MODULE is None:
        ALL_CATEGORIES_MODULE = dict(PRODUCT_CATEGORIES)
    return ALL_CATEGORIES_MODULE
def is_enabled():
    settings = load_settings()
    # 有 API Key 就启用 AI，不需要额外勾选
    # 之前要求 enabled=True，导致很多用户填了 Key 但没勾启用，走关键词兜底产出垃圾
    return bool(settings.get("api_key"))
