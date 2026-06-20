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
import os
import sys
import ssl
import urllib.request
import urllib.error
import re

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
        "★同一素材最近已用片段，必须优先避开同一句、同义表达和相同Hook开场；"
        "素材不足时再少量复用Close，Hook和前3个Product尽量全换★\n"
        + "\n".join(lines)
    )


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


def _multi_version_target_bounds(target_duration):
    try:
        target = int(target_duration or 60)
    except Exception:
        target = 60
    tolerance = max(5, target // 6)
    lower_floor = 5 if target <= 20 else 12 if target <= 30 else 25
    return max(lower_floor, target - tolerance), target + tolerance


def _target_duration_rule_text(target_duration):
    low, high = _multi_version_target_bounds(target_duration)
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


def _append_unique_supplement_clips(clips, supplement, target_duration, limit=None):
    if not supplement:
        return 0
    try:
        _, target_high = _multi_version_target_bounds(target_duration)
    except Exception:
        target_high = float(target_duration or 60) + 10

    existing_ranges = []
    existing_times = set()
    for clip in clips:
        try:
            start = float(clip[2])
            end = float(clip[3])
        except Exception:
            continue
        existing_ranges.append((start, end))
        existing_times.add((round(start, 2), round(end, 2)))

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
        next_total = sum(_clip_duration_value(c) for c in clips) + _clip_duration_value(sc)
        if next_total > target_high + 0.1:
            continue
        clips.append(sc)
        existing_ranges.append((start, end))
        existing_times.add(key)
        added += 1
        if added >= limit:
            break
    return added


def _enforce_target_duration_limit(clips, target_duration, log_fn=None, label="目标时长"):
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
        "胯宽", "腿粗", "显胖", "肚子", "腰粗", "肩宽", "壮",
        "肉", "遮", "藏", "不敢穿", "穿不进去", "卡", "勒",
    ]
    effect_words = [
        "显瘦", "显高", "显白", "显腿长", "比例", "直角肩",
        "腰线", "拉长", "高级", "气质", "干净", "明亮", "薄",
    ]
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
    if not any(reason in reasons for reason in ("爆词", "强情绪", "人群", "痛点", "效果", "问句")):
        return 0.0, []
    return score, reasons


def _collect_hook_candidates_from_entries(entries, hook_keywords=None, focus_hint=None, ai_controls=None, limit=12):
    candidates = []
    for idx, entry in enumerate(entries, 1):
        if len(entry) < 3:
            continue
        es, ee, text = entry[:3]
        try:
            dur = float(ee) - float(es)
        except Exception:
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

    inserted = []
    current_hook = any(_is_hook_clip(c) for c in repaired)
    if not current_hook:
        hook = None
        if reserved_hook and not _clip_overlaps_any(reserved_hook, repaired) and not _clip_overlaps_any(reserved_hook, used_clips):
            hook = reserved_hook
        if hook:
            repaired.insert(0, hook)
            inserted.append("补Hook")
        else:
            in_clip = _pick_best_multi_version_candidate(repaired, _multi_version_hook_candidate_score, [], [])
            if in_clip:
                repaired = [_retag_clip_type(in_clip, "hook")] + [c for c in repaired if c is not in_clip]
                inserted.append("Product转Hook")
            else:
                extra = _pick_best_multi_version_candidate(available_pool, _multi_version_hook_candidate_score, repaired, used_clips)
                if extra:
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
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
LEGACY_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
LEGACY_DOUBAO_MODEL = "doubao-1-5-pro-32k-250115"


def _normalize_ai_model_defaults(settings):
    data = dict(settings or {})
    api_key = str(data.get("api_key") or "").strip()
    base_url = str(data.get("base_url") or "").strip().rstrip("/")
    model = str(data.get("model") or "").strip()

    if not base_url:
        data["base_url"] = DEEPSEEK_DEFAULT_BASE_URL
    if not model:
        data["model"] = DEEPSEEK_DEFAULT_MODEL

    if (
        not api_key
        and base_url == LEGACY_DOUBAO_BASE_URL
        and model == LEGACY_DOUBAO_MODEL
    ):
        data["base_url"] = DEEPSEEK_DEFAULT_BASE_URL
        data["model"] = DEEPSEEK_DEFAULT_MODEL
    return data


def _get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def load_settings():
    # 优先读用户数据目录（用户保存的设置），其次读打包目录
    try:
        from config import SETTINGS_PATH as _user_path
        if os.path.exists(_user_path):
            with open(_user_path, "r", encoding="utf-8-sig") as f:
                return _normalize_ai_model_defaults(json.load(f))
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
    user_path = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "LiveClipper",
        "keywords.json",
    )
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
    merged_pref = _strip_forbidden_keyword_conflicts(
        _clean_keyword_map(_pick("preference_keywords", {})),
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
    "通用卖点": "其他",
}


def _normalize_focus_label(value):
    text = str(value or "").strip()
    return AI_FOCUS_ALIASES.get(text, text)


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
    # 写入用户数据目录（可写），非打包目录（可能只读）
    try:
        from config import SETTINGS_PATH as _save_path
    except ImportError:
        _save_path = os.path.join(_get_base_path(), "ai_settings.json")
    try:
        settings = _normalize_ai_model_defaults(settings)
        existing = {}
        if os.path.exists(_save_path):
            with open(_save_path, "r", encoding="utf-8-sig") as f:
                existing = json.load(f)
        existing.update(settings)
        with open(_save_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False

def _default_settings():
    return {
        "api_key": "", "base_url": DEEPSEEK_DEFAULT_BASE_URL,
        "model": DEEPSEEK_DEFAULT_MODEL, "enabled": False,
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
        "快速促单": "目标是快速促单，优先选择决策理由明确、尺码引导自然、行动号召强的片段。",
    }
    hook_map = {
        "痛点开头": "Hook优先用痛点开头，先圈定人群或问题，再进入卖点。",
        "上身效果开头": "Hook优先用上身效果开头，先给用户看到穿上后的核心效果。",
        "爆点金句开头": "Hook优先用主播最有冲击力的爆点金句开头。",
        "主播强推荐开头": "Hook优先用主播强推荐、强背书、强情绪的表达开头。",
        "不强制Hook": "不强制必须选择Hook类型；如果没有好Hook，可以用最完整的Product片段自然开场。",
    }
    ending_map = {
        "尺码引导": "结尾优先选择尺码/身高体重/选择建议，但不要包含价格。",
        "信任背书": "结尾优先选择主播信任背书、品质确认、闭眼入类表达。",
        "场景收尾": "结尾优先选择通勤、约会、出门、日常等场景化收尾。",
        "自然结束": "结尾自然结束即可，不必强行促单。",
        "不要促单": "结尾不要强促单，不要选择催拍、库存、价格、链接类片段。",
    }
    strictness_map = {
        "宽松": "选片严格度为宽松：卖点覆盖优先，允许少量时间跳跃，但不要牺牲内容完整度。",
        "标准": "选片严格度为标准：在完整、流畅、卖点覆盖之间平衡。",
        "严格": "选片严格度为严格：宁可少选，也不要重复卖点、无关品类、废话、跳跃过大的片段。",
    }

    lines = []
    goal = controls.get("goal")
    if goal:
        lines.append(goal_map.get(goal, f"本次成片目标：{goal}。"))
    selling = controls.get("selling_points", [])
    if selling:
        lines.append(f"主卖点优先：{', '.join(selling)}；Hook和前两个Product优先命中这些方向。")
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
    lines = []
    narrative = str(rules.get("narrative", "") or "").strip()
    custom_text = str(rules.get("custom_text", "") or "").strip()
    if narrative:
        lines.append(f"叙事结构必须遵循：{narrative}")
    if rules.get("category_filter", True):
        lines.append("必须围绕同一主推品类选片，避免突然切到无关品类。")
    else:
        lines.append("允许合理跨品类选片，但必须保证内容衔接自然。")
    if rules.get("time_coherence", True):
        lines.append("片段顺序要尽量保持时间连贯，除Hook前置外避免大幅跳跃。")
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
    _LAST_CATEGORY_FILTER_SUMMARY = {}

    # ============================================================
    # 1. 词库配置
    # ============================================================
    # 品类词库（统一使用全局 PRODUCT_CATEGORIES）

    # 搭配触发词(搭配+品类 → 该品类不计分)
    MATCH_WORDS = ["搭","配","搭配","配着穿","搭什么","配什么","同款","一套","两件套"]

    # 下款预告词(预告+品类 → 该品类全程排除)
    NEXT_PREVIEW = ["下一个开","接下来开","过款","下一款","马上开","下个","接下来","下一个","过下","看下"]

    # 成交铁证词(+50分，绑定最近品类词)
    SELLING_PROOF = {
        "开价": ["划算","超值","性价比","值得","不贵"],
        "行动": ["321","拼手速"],
        "服务": ["报尺码","现货","发货","平铺晾","机洗","尺码","码数","不多了","没货","截单","断码","库存"],
    }
    SELLING_PROOF_ALL = []
    for v in SELLING_PROOF.values():
        SELLING_PROOF_ALL.extend(v)

    # 品类词加权：精准词高分，泛词低分，减少“裤/裙/鞋/吊带”等误判。
    GENERIC_CATEGORY_WORDS = {
        "裤", "裙", "鞋", "吊带", "背心", "马甲", "针织", "打底",
        "三件", "四件", "组合", "穿搭",
    }

    def _category_keyword_weight(cat: str, keyword: str) -> float:
        if not keyword:
            return 0.0
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
        for nw in NEXT_PREVIEW:
            if nw in text:
                info["has_preview"] = True
                # 预告绑定的品类 = 文本中的品类(除搭配外)
                for cat in info["cats_found"]:
                    info["preview_cats"].append(cat)
                break
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
                    for nw in NEXT_PREVIEW:
                        if nw in info["text"]:
                            _log(f"  下款预告排除品类:{cat}(命中词:{nw})")
                            break
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
        # 查找匹配的品类(支持模糊匹配，如"上衣"匹配"上衣"，"裤子"匹配"裤子")
        matched = None
        for cat in PRODUCT_CATEGORIES:
            if force_category in cat or cat in force_category:
                matched = cat
                break
        if matched and matched in PRODUCT_CATEGORIES:
            main_cat = matched
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
    _log(f"  主推严格过滤: 仅保护主品类 {main_cat}，跨品类搭配需同段出现主品类词")

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

        # 规则2:仅搭配提及的跨品类片段 → 删除
        elif not should_remove and info["has_match"]:
            has_main = any(c in protected_cats for c in info["cats_found"])
            has_other = any(c not in protected_cats for c in info["cats_found"])
            if has_other and not has_main:
                should_remove = True
                match_removed += 1

        # 规则3:纯次品类片段(无主品类,无搭配,无预告)→ 也删除
        elif not should_remove:
            has_main = any(c in protected_cats for c in info["cats_found"])
            has_other = any(c not in protected_cats for c in info["cats_found"])
            if has_other and not has_main:
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
    _LAST_CATEGORY_FILTER_SUMMARY = {
        "main_category": main_cat,
        "protected_categories": sorted(protected_cats),
        "original_segments": len(seg_info),
        "kept_segments": kept,
        "removed_segments": removed,
        "preview_removed": preview_removed,
        "cross_category_removed": match_removed,
    }

    # ============================================================
    # 6. 跨品类合法性校验(第二道防线)
    # ============================================================
    # 唯一合法的次品类提及:同一句中必须同时有 主品类词 + 搭配词 + 次品类词
    # 否则删除
    main_keywords = set()
    for cat in protected_cats:
        for kw in PRODUCT_CATEGORIES.get(cat, []):
            main_keywords.add(kw)
    # 扩展主品类词:包含"这件","这款","这个"等指代词(如果后面紧跟主品类相关描述)
    main_keywords.update(["这件", "这款", "这个", "这条", "那个", "那种"])

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
                # 有次品类但无搭配词 → 检查是否有主品类
                if has_main:
                    # 有主品类 + 次品类但无搭配 → 合法(主品类讲解中顺便提了下其他品)
                    legal_lines.append(line)
                    legal_lines.append(text)
                    legal_lines.append("")
                else:
                    # 孤立次品类 → 强制删除
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
        _LAST_CATEGORY_FILTER_SUMMARY["orphan_removed"] = orphan_removed
    else:
        _log(f"品类合法性校验: 无突兀跨品类内容")
        _LAST_CATEGORY_FILTER_SUMMARY["orphan_removed"] = 0

    return "\n".join(legal_lines), main_cat


# ============================================================
# 核心:调用 AI + 前置清洗 + 重试
# ============================================================
def ai_analyze_clips(srt_text, log_fn=None, force_category=None, multi_version=False, focus_hint=None, hook_candidates_hint=None, merge_mode=False, target_duration=60, ai_controls=None, record_history=True):
    global _AI_TARGET_DURATION
    _AI_TARGET_DURATION = target_duration
    def _log(msg):
        if log_fn: log_fn(msg)

    settings = load_settings()
    if not settings.get("api_key"):
        _log("AI: 未配置 API Key")
        return []

    api_key = settings["api_key"]
    base_url = settings["base_url"].rstrip("/")
    model = settings["model"]
    _ai_rules = _merge_ai_rules(ai_controls)
    _enforce_category_filter = bool(_ai_rules.get("category_filter", True))
    _enforce_time_coherence = bool(_ai_rules.get("time_coherence", True))
    _hook_cap_sec = _hook_cap_seconds(_ai_rules)

    # [v9.3] 拆分长SRT条目，提高AI选片精度
    from srt_splitter import split_long_srt_entries
    srt_text = split_long_srt_entries(srt_text, max_duration=5.0, log_fn=_log)

    cleaned_srt = _pre_clean_srt(srt_text, log_fn)
    if not cleaned_srt.strip():
        _log("AI: 清洗后无有效字幕，尝试使用原始SRT...")
        cleaned_srt = srt_text
        if not cleaned_srt.strip():
            _log("AI: 原始SRT也为空")
            return []

    # SRT预去重: 去除主播重复讲述的段落
    if not merge_mode:
            cleaned_srt = _dedup_srt_repeated_sections(cleaned_srt, log_fn)

    # 品类过滤:识别主品类，从源SRT中移除其他品类(支持用户手动指定)
    if _enforce_category_filter:
        cleaned_srt, detected_main_cat = _filter_srt_by_main_product(cleaned_srt, log_fn, force_category=force_category)
    else:
        detected_main_cat = force_category if force_category and force_category != "自动检测" else None
        _log("AI选片规则: 已关闭强制同一品类过滤")
    # 用检测到的品类（或用户指定）作为跨品类过滤的偏好品类
    _cross_cat_preferred = force_category if force_category and force_category != "自动检测" else detected_main_cat
    _history_key = _clip_history_key(cleaned_srt)
    _recent_history = _get_recent_clip_history(_history_key)
    _recent_history_hint = _format_recent_history_hint(_recent_history)
    if _recent_history:
        _log(f"差异化历史: 检测到同素材最近已用 {len(_recent_history)} 个片段，本次优先避开")
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

    # AI 分析(最多重试 5 次，对抗 R1 空 content)
    # 计算目标片段数（用于检查AI是否选够）
    _target_min_clips = 8
    if _AI_TARGET_DURATION >= 100: _target_min_clips = 22
    elif _AI_TARGET_DURATION >= 80: _target_min_clips = 18
    elif _AI_TARGET_DURATION >= 50: _target_min_clips = 14
    elif _AI_TARGET_DURATION >= 30: _target_min_clips = 8

    best_clips = []
    for attempt in range(5):
        _log(f"AI: 调用 {model}(第 {attempt + 1} 次)...")
        # ★构建SRT条目列表（供AI按索引选片）★
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
        if attempt == 0:
            log_fn(f"AI: 构建SRT条目索引 {len(_indexed_srt_entries)} 条")
        
        clips = _call_ai(api_key, base_url, model, cleaned_srt, log_fn, focus_hint=focus_hint, srt_entries=_indexed_srt_entries, hook_candidates_hint=hook_candidates_hint, ai_controls=ai_controls, recent_history_hint=_recent_history_hint)
        if not clips:
            continue
        # 检查AI选的片段数是否达标；若总时长已经接近目标，不再为了凑段数二次补选。
        _current_ai_dur = sum(float(c[5]) for c in clips if len(c) >= 6)
        _target_floor_for_supplement = max(25, int(_AI_TARGET_DURATION * 0.95))
        if clips and len(clips) < _target_min_clips and attempt < 2 and _current_ai_dur < _target_floor_for_supplement:
            _need_supplement = max(0, _target_min_clips - len(clips))
            _supplement_cap = _target_supplement_cap(_AI_TARGET_DURATION)
            _supplement_limit = min(_supplement_cap, _need_supplement)
            log_fn(f"AI: 当前{len(clips)}段/{_current_ai_dur:.1f}s < 目标{_target_min_clips}段/{_target_floor_for_supplement}s，最多补选{_supplement_limit}段...")
            _extra_hint = f"【注意：刚才你只选了{len(clips)}段，总时长约{_current_ai_dur:.1f}秒，低于目标下限。请再额外选{_supplement_limit}个以内高质量短片段，优先补足不同卖点，把总时长补到{_AI_TARGET_DURATION}秒左右；不要重复你刚选的。仅输出新增片段的JSON数组，不要包含任何推理过程。】"
            _supplement = _call_ai(api_key, base_url, model, cleaned_srt, _log, focus_hint=focus_hint, srt_entries=_indexed_srt_entries, hook_candidates_hint=hook_candidates_hint, ai_controls=ai_controls, recent_history_hint=_recent_history_hint, extra_instruction=_extra_hint)
            if _supplement:
                _added_supplement = _append_unique_supplement_clips(clips, _supplement, _AI_TARGET_DURATION, _supplement_limit)
                _log(f"AI: 补选{_added_supplement}段，补选后共{len(clips)}段")
        elif clips and len(clips) < _target_min_clips and attempt < 2:
            log_fn(f"AI: 当前{len(clips)}段但已有{_current_ai_dur:.1f}s，接近目标，跳过补选避免超时长")
        original_clips = list(clips)
        removed_from_dedup = []
        clips = _dedup_clips(clips, log_fn, multi_version=multi_version, focus_hint=focus_hint, srt_text=srt_text)
        # 从Product中提取Hook（如果AI没选Hook）
        clips = _extract_hook_from_products(clips, cleaned_srt, log_fn, focus_hint=focus_hint, ai_controls=ai_controls)
        clips = _force_short_hook(clips, cleaned_srt, log_fn, max_hook_sec=_hook_cap_sec, focus_hint=focus_hint, ai_controls=ai_controls)
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
        if len(clips) > len(best_clips):
            best_clips = clips[:]
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
            clips = _filter_hook_product_repeats(clips, log_fn)
            # [v9.2] 裁掉片段开头的语气词(对/嗯/呃等)对应的画面和音频
            clips = _trim_filler_start(clips, cleaned_srt, log_fn)
            clips = _trim_filler_middle(clips, cleaned_srt, log_fn)
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
                _extra_hint = (
                    f"【后处理后片单只剩{len(clips)}段/{_final_total:.1f}秒，仍低于目标。"
                    f"请额外补选{_need_count}个以内不同卖点的完整短句，优先补到{_AI_TARGET_DURATION}秒附近；"
                    "不要重复已有片段，不要选价格/券/违禁词/废话。仅输出新增片段JSON数组。】"
                )
                _supplement = _call_ai(
                    api_key, base_url, model, cleaned_srt, _log,
                    focus_hint=focus_hint,
                    srt_entries=_indexed_srt_entries,
                    hook_candidates_hint=hook_candidates_hint,
                    ai_controls=ai_controls,
                    recent_history_hint=_recent_history_hint,
                    extra_instruction=_extra_hint,
                )
                _added_final = _append_unique_supplement_clips(clips, _supplement, _AI_TARGET_DURATION, _need_count)
                if _added_final:
                    _log(f"目标补选: 后处理后补入 {_added_final} 段，{_final_total:.1f}s -> {sum(_clip_duration_value(c) for c in clips):.1f}s")
                    clips = _filter_price_and_cta(clips, log_fn)
                    if not multi_version:
                        clips = _filter_semantic_repeat(clips, log_fn)
                    clips = _filter_hook_product_repeats(clips, log_fn)
                    clips = _dedup_clip_text_overlap(clips, log_fn, merge_mode=merge_mode)
                    clips = _cap_clip_duration(clips, log_fn, srt_text=srt_text)
                    clips = _fix_clip_boundaries(clips, cleaned_srt, log_fn)
                    clips = _trim_filler_start(clips, cleaned_srt, log_fn)
                    clips = _trim_filler_middle(clips, cleaned_srt, log_fn)
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
            if len(clips) < 8 and attempt < 2:
                _log(f"AI: 仅{len(clips)}个片段偏少，拉高temperature重试...")
                import random as _r2
                temperature = round(_r2.uniform(0.5, 0.75), 2)  # 更高temperature刺激多样化
                _log(f"AI: 重试temperature={temperature}")
                original_clips = list(clips)
                continue
            clips = _enforce_target_duration_limit(clips, _AI_TARGET_DURATION, log_fn)
            _record_history_if_needed(clips)
            return clips
        _log(f"AI: 第 {attempt + 1} 次校验未通过，重试...")

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
        clips = trim_tail_filler(clips, srt_text, log_fn)
        # [v9.4] Close完整性 → [v9.6] 全片段语句完整性
        clips = _split_long_clips(clips, _indexed_srt_entries, log_fn)
        clips = _trim_filler_start(clips, srt_text, log_fn)
        # 重叠清理：_split_long_clips和_trim_filler_prefix可能引入重叠
        clips = _remove_overlaps(clips, log_fn)
        clips = _fix_clip_boundaries(clips, cleaned_srt, log_fn)
        clips = _filter_hook_product_repeats(clips, log_fn)
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
        clips = _enforce_target_duration_limit(clips, _AI_TARGET_DURATION, log_fn)
        _record_history_if_needed(clips)
        return clips

    # 宽松修复
    relaxed = _relax_clips(clips if clips else [], log_fn)
    if relaxed and len(relaxed) < _target_min_clips:
        relaxed = _supplement_clips(relaxed, cleaned_srt, log_fn, min_total=_target_min_clips)
    if relaxed:
        relaxed = _filter_recent_similar_clips(
            relaxed, _recent_history, log_fn,
            min_keep=_history_min_keep,
            min_duration=_history_min_duration,
        )
        relaxed = _enforce_target_duration_limit(relaxed, _AI_TARGET_DURATION, log_fn)
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
    }
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


def _is_bad_hook_candidate_text(text):
    txt = re.sub(r"\s+", "", str(text or "")).strip("，。！？!?、 ")
    if not txt or len(txt) < 4:
        return True
    weak_starts = ("然后", "但是", "不过", "而且", "所以", "对吧然后", "是的然后", "那谁")
    if txt.startswith(weak_starts):
        return True
    weak_ends = (
        "都", "就", "还", "也", "和", "跟", "把", "被", "会", "可以",
        "如果", "因为", "然后", "但是", "不过", "就是", "这个", "这款",
        "你看", "对吧", "的话"
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


def _force_short_hook(clips, srt_text, log_fn=None, max_hook_sec=None, focus_hint=None, ai_controls=None):
    """Hook > 5秒时，从SRT短条目中找1-4秒爆点词替换原Hook。
    原Hook降为Product，新Hook用SRT精确时间戳，保证1-3秒。
    """
    MAX_HOOK_SEC = 5.0 if max_hook_sec is None else float(max_hook_sec)

    def _log(msg):
        if log_fn: log_fn(msg)

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


def _call_ai(api_key, base_url, model, srt_text, log_fn, focus_hint=None, srt_entries=None, hook_candidates_hint=None, multi_version=False, return_raw=False, num_versions=3, ai_controls=None, recent_history_hint=None, extra_instruction=None):
    def _log(msg):
        if log_fn: log_fn(msg)
    focus_hint = _normalize_focus_label(focus_hint)

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
            re.compile(r'拍.*链接|链接.*拍|去拍|赶紧拍|刷新拍'),
        ]
        _indexed_lines = []
        _forbidden_count = 0
        for i, (es, ee, et) in enumerate(srt_entries, 1):
            _srt_entry_map[i] = (es, ee, et)
            # 违禁词预扫描：标记含违禁词的条目
            _matched_fw = [w for w in _fw_words if w in et] if _fw_words else []
            # 价格/CTA预扫描：标记含价格模式的条目（跟_filter_price_and_cta同规则）
            _matched_price = any(_p.search(et) for _p in _price_patterns) if _price_patterns else False
            if _matched_fw or _matched_price:
                _forbidden_indices.add(i)
                _forbidden_count += 1
            _indexed_lines.append(f"[#{i:02d}] {et}")
        indexed_transcript = chr(10).join(_indexed_lines)
        # 追加用户自定义细节关键词（注入到AI prompt末尾，高优先级关注）
        if _detail_kw_prompt:
            indexed_transcript += "\n" + _detail_kw_prompt
        _log(f"AI: 编号SRT条目 {len(_srt_entry_map)} 条")
        if _forbidden_count:
            _log(f"AI: 预扫描: {len(_srt_entry_map) - _forbidden_count} 条可选, {_forbidden_count} 条含违禁词/价格已标记")

    # ★预扫描Hook候选：短爆词 + 人群痛点 + 效果前置综合打分★
    _kw_data = load_keywords()
    _hook_kw = _kw_data["hook_keywords"]
    _entries_for_hook = [(es, ee, et) for _idx, (es, ee, et) in sorted(_srt_entry_map.items())]
    _hook_candidates, _hook_candidate_total = _collect_hook_candidates_from_entries(
        _entries_for_hook,
        hook_keywords=_hook_kw,
        focus_hint=focus_hint,
        ai_controls=ai_controls,
        limit=12,
    )
    _hook_hint = ""
    if hook_candidates_hint:
        # 多版本模式：使用外部传入的分配候选
        _hook_hint = hook_candidates_hint
        _log(f"AI: 使用分配的Hook候选")
    elif _hook_candidates:
        import random as _hook_random
        _pref_candidates = [c for c in _hook_candidates if c[3] >= 18]
        _plain_candidates = [c for c in _hook_candidates if c[3] < 18]
        _hook_random.shuffle(_pref_candidates)
        _hook_random.shuffle(_plain_candidates)
        _ranked_hook_candidates = sorted(_pref_candidates, key=lambda c: c[3], reverse=True) + _plain_candidates
        _picked_hook_candidates = _ranked_hook_candidates[:12]
        _cand_text = [f'#{idx_k:02d}"{et[:18]}"({dur:.0f}s/{",".join(reasons[:2])})' for idx_k, et, dur, _score, reasons in _picked_hook_candidates]
        _hook_hint = f"\n★Hook候选（已按爆点/人群痛点/效果前置综合打分，优先从中选择不同开场）: {', '.join(_cand_text)}★"
        _log(f"AI: Hook候选池 {_hook_candidate_total} 个，提供 {len(_picked_hook_candidates)} 个")
    else:
        _hook_hint = "\n★未找到短爆点Hook候选，请从SRT中找最有冲击力的短句作为Hook★"

    # 随机化:同一视频多次生成不同成品
    import random
    temperature = round(random.uniform(0.15, 0.75), 2)
    _log(f"AI: temperature={temperature}")

    # 随机偏好提示(每次侧重不同角度，增加差异化)
    if focus_hint and focus_hint not in ("自动", "auto", ""):
        focus = focus_hint
        _log(f"AI: 指定偏好 → {focus}")
    elif _skip_focus:
        # 多版本模式：保留用户偏好，让方案1匹配
        focus = focus_hint if focus_hint else ""
        if focus:
            _log(f"AI: 多版本模式，方案1偏好 → {focus}")
        else:
            _log("AI: 多版本模式（全量选片）")
    else:
        # ★智能偏好选择：分析SRT内容，选最匹配的偏好★
        try:
            _kw_data_focus = load_keywords()
            _focus_scores = {}
            _focus_hints_map = _kw_data_focus.get("preference_keywords", _DEFAULT_PREFERENCE_KEYWORDS)
            # 统计SRT中每种偏好的关键词命中数
            _srt_lower = srt_text
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
                _kw_path_weights = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LiveClipper", "keywords.json")
                if os.path.exists(_kw_path_weights):
                    with open(_kw_path_weights, "r", encoding="utf-8") as _jwf:
                        _jwdata = _jw.load(_jwf)
                    _file_weights = _jwdata.get("preference_weights", {})
                    if isinstance(_file_weights, dict):
                        _weights.update(_normalize_preference_weights(_file_weights))
            except Exception:
                pass
            for _fname, _fkws in _focus_hints_map.items():
                _fname = _normalize_focus_label(_fname)
                _score = sum(1 for _kw in _fkws if _kw in _srt_lower)
                if _score > 0:
                    _weight = _weights.get(_fname, 1.0)
                    _focus_scores[_fname] = _focus_scores.get(_fname, 0) + _score * _weight
            
            if _focus_scores:
                # 选得分最高的偏好，但最高分差距<2时随机选（增加差异化）
                _max_score = max(_focus_scores.values())
                _candidates = [k for k, v in _focus_scores.items() if v >= _max_score - 1]
                if len(_candidates) > 1:
                    import random as _rand_pref
                    _best_focus = _rand_pref.choice(_candidates)
                    _best_score = _focus_scores[_best_focus]
                else:
                    _best_focus = max(_focus_scores, key=_focus_scores.get)
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
                }
                focus = _focus_hint_map_full.get(_best_focus, list(_focus_hint_map_full.values())[0])
                # 40%概率随机换一个偏好（避免同视频永远同一个）
                import random as _rand_switch
                if _rand_switch.random() < 0.4 and len(_focus_hint_map_full) > 1:
                    _alt_keys = [k for k in _focus_hint_map_full if k != _best_focus]
                    _alt_focus = _rand_switch.choice(_alt_keys)
                    focus = _focus_hint_map_full[_alt_focus]
                    _log(f"AI: 智能偏好 → {_best_focus}(命中{_best_score}次)，随机切换到→ {_alt_focus} → {focus}")
                else:
                    _log(f"AI: 智能偏好 → {_best_focus}(命中{_best_score}次) → {focus}")
            else:
                focus_hints = [
                    "侧重身材痛点，优先选显瘦,遮肉,修饰身材的片段",
                    "侧重面料卖点，优先选面料手感,质感相关的片段",
                    "侧重情绪感染力，优先选主播语气最激动,最真诚的片段",
                ]
                focus = random.choice(focus_hints)
                _log(f"AI: 随机偏好 → {focus}")
        except Exception as _e:
            # 防护：变量名拼写不一致或其他异常时，降级到随机偏好而非关键词兜底
            import random as _rand
            focus = _rand.choice([
                "侧重身材痛点，优先选显瘦,遮肉,修饰身材的片段",
                "侧重面料卖点，优先选面料手感,质感相关的片段",
                "侧重情绪感染力，优先选主播语气最激动,最真诚的片段",
            ])
            _log(f"AI: 智能偏好异常({str(_e)})，降级随机偏好 → {focus}")

    # [增强] 计算 SRT 时间范围，告知 AI
    _srt_times = []
    for _ln in srt_text.strip().split("\n"):
        _tm = re.match(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", _ln.strip())
        if _tm:
            _es = int(_tm.group(5))*3600 + int(_tm.group(6))*60 + int(_tm.group(7)) + int(_tm.group(8))/1000.0
            _srt_times.append(_es)
    _srt_max = max(_srt_times) if _srt_times else 60
    _srt_min = min(_srt_times) if _srt_times else 0

    _target_rule = _target_duration_rule_text(_AI_TARGET_DURATION)

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
        # 使用调用方设置的 _AI_CLIP_COUNT
        _min_pieces = 8  # 默认兜底值
        if _AI_CLIP_COUNT and _AI_CLIP_COUNT != "10-15":
            _clip_range = _AI_CLIP_COUNT
            _min_pieces = int(_AI_CLIP_COUNT.split("-")[0])
            _total_rule = f"★精选{_AI_CLIP_COUNT}个片段(不能少于{_min_pieces}个)，{_target_rule}★"
        elif _AI_TARGET_DURATION <= 40:
            _clip_range = "5-8"
            _total_rule = _target_rule
        elif _AI_TARGET_DURATION >= 100:
            _clip_range = "22-30"
            _total_rule = _target_rule
        elif _AI_TARGET_DURATION >= 80:
            _clip_range = "18-24"
            _total_rule = _target_rule
        else:
            _clip_range = "10-15"
            _total_rule = _target_rule
        # ★★★ 关键：同步 _AI_CLIP_COUNT，否则 prompt 替换的永远是默认值 ★★★
        _AI_CLIP_COUNT = _clip_range
        _dedup_rule = '★绝对禁止重复同一卖点★ 字幕中主播会重复讲同一个卖点(如"面料好"说了3遍)，你必须只选每个卖点的最佳版本，严禁选两段内容相似的片段'
        _hook_rule = ""
        _product_rule = ""
        _close_rule = ""
        _hook_rule = ""
        _product_rule = ""
        _close_rule = ""

    # 随机差异化指令（每次运行不同侧重点）
    _diff_vibes = [
        "★本轮选片重点：优先选主播语气最激动、情绪最饱满的片段，卖点角度越多越好★",
        "★本轮选片重点：优先选展示上身效果、穿搭场景的片段，少选纯面料描述★",
        "★本轮选片重点：优先选对比类、痛点解决类的内容，搭配推荐最佳★",
        "★本轮选片重点：优先选版型显瘦、修饰身材的内容，效果优先于参数★",
        "★本轮选片重点：优先选品质背书、细节讲解的内容，信任感优先★",
    ]
    _diff_vibe = random.choice(_diff_vibes)
    _ai_rules_prompt = _build_ai_rules_prompt(ai_controls=ai_controls)

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
        _kw_path_detail = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LiveClipper", "keywords.json")
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

    if _skip_focus:
        # 多版本模式：AI只做素材选取，不做编排
        # ★重要：Prompt结构与单版本一致（避免deepseek-v4-flash对不同格式的兼容问题）★
        user_msg = f"""以下是编号后的直播字幕条目，你需要像专业短视频编导一样，从中精选出{_clip_range}个高质量素材片段.

你的任务是从SRT中选出足够多的高质量片段，后续会由AI二次编排成不同版本.素材越丰富越多元化,最终效果越好.

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
- 优先选受众群体广的卖点(显瘦>面料>颜色>场景)
- 对于Close片段,优先选"信任强化"和"尺码引导"类,避开含价格的

{f"★本轮选片侧重: {focus}★" if focus else ""}
{_ai_rules_prompt}
{_recent_history_prompt}

★输出格式★: 每个片段用 srt_indices 字段指定选了哪些编号条目(数组),不要填start/end时间戳.★优先选1个完整条目；如果前一句必须靠后一句承接，允许选2个连续条目，确保单片段5-10秒且语义完整★:
[
  {{"clip_type": "hook", "srt_indices": [3], "focus": "痛点提问", "reason": "开场爆点"}},
  {{"clip_type": "hook", "srt_indices": [12], "focus": "效果前置", "reason": "不同钩子类型"}},
  {{"clip_type": "product", "srt_indices": [18], "focus": "版型显瘦", "reason": "显瘦卖点突出"}},
  {{"clip_type": "product", "srt_indices": [25], "focus": "面料触感", "reason": "面料描述细腻"}},
  {{"clip_type": "close", "srt_indices": [45], "focus": "信任强化", "reason": "推荐合辑"}},
  ...
]

★clip_type: hook/product/close 三种类型; focus: 该片段的卖点角度描述; reason: 选片理由(10字内)★
{_hook_hint}

字幕条目:
{indexed_transcript}"""
    else:
        user_msg = f"""以下是编号后的直播字幕条目，你需要像专业短视频编导一样，从中精选条目并编排成一个完整的带货短视频脚本.

要求:
1. {_dedup_rule}
2. 像讲故事一样编排，每个片段自然衔接下一段，听起来是一段流畅的口播
3. 精选{_clip_range}个片段，{_total_rule}
4. ★每个片段优先选1个编号条目；如果只选前一句会导致语义不完整，必须连带后一句，允许选2个连续编号条目★ 禁止为了凑时长选3个以上条目。短而完整 > 短而碎 > 长而散
5. ★片段之间禁止条目编号重叠★ 同一条目只能出现在一个片段中
6. 如果一个片段选的条目之间有间隔（如选了#05和#07但跳过#06），说明中间条目是废话需要跳过——这是正确的，代码会自动拆成两段分别剪辑
7. [本轮选片偏好]{focus}
   ★Hook必须匹配偏好！选不出匹配偏好的Hook就不选Hook类型，改用其他类型开头★ 前两个Product也必须切中偏好角度。 后续Product必须覆盖其他卖点角度（版型/面料/显瘦/穿搭/品质/场景等），确保单视频介绍完整。同一卖点角度最多2段，禁止全片只讲一个维度
8. {_diff_vibe}
9. {_hook_rule}
10. {_product_rule}
11. {_close_rule}
{_ai_rules_prompt}
{_recent_history_prompt}
{_extra_instruction_prompt}

★输出格式★: 每个片段用 srt_indices 字段指定选了哪些编号条目（数组），不要填start/end时间戳。★优先1个条目；前后句强相关时选2个连续条目，确保单片段3-9秒且语义完整，不要选3个以上★:
[
  {{"clip_type": "hook", "srt_indices": [3], "focus": "痛点提问", "reason": "圈人群+效果对比"}},
  {{"clip_type": "product", "srt_indices": [8], "focus": "版型显瘦", "reason": "上身效果最强"}},
  ...
]

{_hook_hint}

字幕条目:
{indexed_transcript}"""

    _system_low, _system_high = _multi_version_target_bounds(_AI_TARGET_DURATION)
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.replace("45-65", f"{_system_low:.0f}-{_system_high:.0f}").replace("10-15", _AI_CLIP_COUNT).replace("最低8段", f"最低{_min_pieces}段").replace("6-10", f"{max(5, _min_pieces - 4)}-{_min_pieces}")},
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

    url = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
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
            return _parse_ai_response(content, log_fn, srt_entries, _forbidden_indices)
        return _parse_ai_response(content, log_fn, srt_entries, _forbidden_indices)
    except urllib.error.HTTPError as e:
        err = ""
        try: err = e.read().decode("utf-8", errors="replace")[:200]
        except Exception: pass
        _log(f"⚠️ AI 接口调用失败 (HTTP {e.code})：{_friendly_http(e.code, err)}")
        _skip_focus = _orig_skip  # 恢复全局状态
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


def _parse_ai_response(content, log_fn, srt_entries=None, forbidden_indices=None):
    def _log(msg):
        if log_fn: log_fn(msg)

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
    for idx, item in enumerate(data):
        # 诊断:打印第一个 item 的所有字段名
        if idx == 0:
            _log(f"AI: 第1项字段名={list(item.keys()) if isinstance(item, dict) else type(item).__name__}")
            _log(f"AI: 第1项原始值 start={item.get('start')} end={item.get('end')} text={str(item.get('text',''))[:40]}")
        ct = str(item.get("clip_type", item.get("type", "")))
        ct = type_map.get(ct, ct)
        if ct not in GOLDEN_CHAIN:
            ct = "highlight"
        
        # ★优先使用srt_indices查表★（解决AI时间戳幻觉问题）
        srt_idx = item.get("srt_indices", item.get("srt_index", None))
        if srt_idx and _srt_entry_map:
            # 按索引查SRT条目，构建clip
            if isinstance(srt_idx, int):
                srt_idx = [srt_idx]
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
                clips.append((ct_this, group_text, float(es_start), float(ee_end), 50, dur, focus))
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
            clips.append((ct, text, start, end, 50, float(end - start), focus))
    if not clips:
        _log(f"AI: {len(data)}项中有效0(无文本:{skipped_no_text}, 时间错误:{skipped_bad_time})")
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


def ai_analyze_multi_versions(srt_text, log_fn=None, force_category=None, focus_hint=None, num_versions=3, ai_controls=None, target_duration=60):
    """多版本AI选片：1次AI调用直接出3个独立叙事方案，减少2/3成本和时间
    返回: {"versions": [{angle, clips}, ...]}
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    global _AI_TARGET_DURATION
    _AI_TARGET_DURATION = target_duration

    settings = load_settings()
    if not settings.get("api_key"):
        _log("AI: 未配置 API Key")
        return {"versions": []}

    api_key = settings["api_key"]
    base_url = settings["base_url"].rstrip("/")
    model = settings["model"]
    _ai_rules = _merge_ai_rules(ai_controls)
    _enforce_category_filter = bool(_ai_rules.get("category_filter", True))
    _enforce_time_coherence = bool(_ai_rules.get("time_coherence", True))
    _hook_cap_sec = _hook_cap_seconds(_ai_rules)

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
        _detected_main_cat = force_category if force_category and force_category != "自动检测" else None
        _log("AI选片规则: 已关闭强制同一品类过滤")
    _history_key = _clip_history_key(cleaned_srt)
    _recent_history = _get_recent_clip_history(_history_key)
    _recent_history_hint = _format_recent_history_hint(_recent_history)
    if _recent_history:
        _log(f"多版本差异化: 检测到同素材最近已用 {len(_recent_history)} 个片段，本次优先避开")

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
                            recent_history_hint=_recent_history_hint)
        if raw_clips and len(raw_clips) >= 10:
            break
        import time as _sleepmod
        _sleepmod.sleep(5)

    if not raw_clips or len(raw_clips) < 10:
        _log(f"AI: 素材选取失败(重试3次后仅{len(raw_clips) if raw_clips else 0}个片段)，降级到单版本多次调用...")
        # 降级策略：改走各自独立的单版本调用（带版本间去重）
        return _multi_version_fallback(srt_text, _log, force_category, focus_hint, num_versions,
                                       api_key, base_url, model, cleaned_srt, _indexed_srt_entries,
                                       _hook_hint, ai_controls=ai_controls)
        return _multi_version_fallback(srt_text, _log, force_category, focus_hint, num_versions,
                                       api_key, base_url, model, cleaned_srt, _indexed_srt_entries,
                                       _hook_hint, ai_controls=ai_controls)

    _log(f"AI: 素材选取成功，共{len(raw_clips)}个片段")

    # ★对素材做初步后处理★
    raw_clips = _dedup_clips(raw_clips, _log, multi_version=True, focus_hint=focus_hint, srt_text=cleaned_srt)
    if _enforce_category_filter:
        raw_clips = _post_filter_cross_category(raw_clips, cleaned_srt, _log, preferred_cat=_detected_main_cat)
    raw_clips = [(ct, _apply_asr_corrections(text, _log), s, e, sc, d, focus)
                 for ct, text, s, e, sc, d, focus in raw_clips]
    raw_clips = _filter_host_interaction(raw_clips, _log)
    raw_clips = _filter_price_and_cta(raw_clips, _log)
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
                                       _hook_hint, ai_controls=ai_controls)

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
        clips = _repair_multi_version_structure(
            clips, _remaining_clips, _reserved_hook, _reserved_close,
            _used_version_clips, target_duration, _log, label=f"版本{vi+1}"
        )
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
                                       _hook_hint, ai_controls=ai_controls)

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
                            ai_controls=None):
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
    _detected_main_cat = force_category if force_category and force_category != "自动检测" else None
    _history_key = _clip_history_key(cleaned_srt)
    _recent_history = _get_recent_clip_history(_history_key)
    _recent_history_hint = _format_recent_history_hint(_recent_history)
    if _recent_history:
        _log(f"降级多版本差异化: 检测到同素材最近已用 {len(_recent_history)} 个片段")
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
                         recent_history_hint=_recent_history_hint)
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

        clips = _dedup_clips(clips, _log, multi_version=True, focus_hint=focus_hint, srt_text=cleaned_srt)
        clips = _filter_recent_similar_clips(clips, _used_version_clips, _log, min_keep=4)
        clips = _extract_hook_from_products(clips, cleaned_srt, _log, focus_hint=angle_name, ai_controls=ai_controls)
        clips = _force_short_hook(clips, cleaned_srt, _log, max_hook_sec=_hook_cap_sec, focus_hint=angle_name, ai_controls=ai_controls)
        if _enforce_category_filter:
            clips = _post_filter_cross_category(clips, cleaned_srt, _log, preferred_cat=_detected_main_cat)
        if _enforce_time_coherence:
            clips = _check_narrative_coherence(clips, _log)
        clips = [(ct, _apply_asr_corrections(text, _log), s, e, sc, d, focus)
                 for ct, text, s, e, sc, d, focus in clips]
        clips = _filter_host_interaction(clips, _log)
        clips = _filter_price_and_cta(clips, _log)
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
                        hook_candidates_hint=None, target_duration=60, used_version_notes=None):
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
    _target_min, _target_max = _multi_version_target_bounds(target_duration)

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

    url = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
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
            _existing_hook_good = any(kw in clip[1] for kw in _hook_kw)
            break

    if _existing_hook_good and (not _explicit_pref or _hook_matches_preference(clips[_existing_hook][1], focus_hint, ai_controls)):
        _log("Hook提取: 现有Hook含爆点词，质量OK")
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
                pref_hits = _hook_pref_score(txt, focus_hint, ai_controls)
                if _explicit_pref and pref_hits:
                    dur = ee - es
                    if 0.8 <= dur <= 5.0 and not _is_bad_hook_candidate_text(txt):
                        score = 18 + pref_hits * 10 + 6.0 / max(dur, 0.8)
                        hook_candidates.append((es, ee, txt, dur, score, ci))
                for kw in _hook_kw:
                    if kw in txt:
                        dur = ee - es
                        if _is_bad_hook_candidate_text(txt):
                            continue
                        score = 10.0 / max(dur, 0.5)  # 越短越好
                        if kw in ("美爆了", "绝了", "太漂亮", "不敢信", "封神", "神仙", "超级超级"):
                            score *= 2  # 强爆点加分
                        pref_hits = _hook_pref_score(txt, focus_hint, ai_controls)
                        if pref_hits:
                            score += 18 + pref_hits * 6
                        hook_candidates.append((es, ee, txt, dur, score, ci))
                        break

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

def _dedup_clips(clips, log_fn, multi_version=False, focus_hint=None, srt_text=None):

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
                _matched = [w for w in _fb_list if w in _txt]
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
                clips[0] = ("hook", _old[1], _old[2], _old[3], _old[4], _old[5], *_old[6:])
                _log(f"Hook提拔: 首位Product '{_old[1][:20]}...' → Hook")

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

        clips = _reorder_product_focus_blocks(clips, log_fn)

    # 删除过短片段（<2s且非Hook的内容不完整，但Hook可以很短）
    _short = [c for c in clips if len(c) > 3 and (c[3] - c[2]) < 2 and c[0] != "hook"]
    if _short:
        clips = [c for c in clips if not (len(c) > 3 and (c[3] - c[2]) < 2 and c[0] != "hook")]
        _log(f"过短片段过滤: 删除{len(_short)}段<2s的非Hook片段")

    return clips


def _detect_focus_point(text):
    RULES = [
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

    if any(k in hay for k in ("显瘦", "遮肉", "藏肉", "收腰", "显高", "比例", "修饰", "版型", "廓形", "剪裁", "宽松", "修身")):
        return "版型显瘦"
    if any(k in hay for k in ("面料", "材质", "手感", "触感", "垂感", "透气", "亲肤", "柔软", "针织", "冰丝", "真丝", "棉麻", "不闷", "不透")):
        return "面料质感"
    if any(k in hay for k in ("做工", "工艺", "细节", "品质", "质感", "高级感", "精致", "走线", "刺绣", "蕾丝", "重工")):
        return "品质细节"
    if any(k in hay for k in ("颜色", "色系", "显白", "提气色", "复古", "黑色", "白色", "咖色", "花色", "撞色")):
        return "颜色氛围"
    if any(k in hay for k in ("场景", "通勤", "约会", "日常", "职场", "出门", "度假", "拍照", "逛街", "旅游", "搭配", "叠穿", "内搭", "外穿", "成套", "套穿")):
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


def _reorder_product_focus_blocks(clips, log_fn=None):
    """Adaptively group product clips by selling-point block.

    The goal is not to force every video into one fixed template. If the AI
    already produced a coherent narrative, keep it. If it jumps between blocks
    or time ranges, gently group related selling points while keeping the first
    AI-selected product block as the narrative anchor.
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
    if len(products) < 4:
        if base_reordered != clips:
            _log(f"卖点段落排序: 保持AI原序，仅修正hook/close位置 ({len(clips)}段)")
        else:
            _log(f"卖点段落排序: 保持AI原序 ({len(clips)}段)")
        return base_reordered

    # 检测主品类，使用该品类的卖点排序顺序
    cat_counts = {}
    for clip in products:
        cat = _detect_product_category(str(clip[1] if len(clip) > 1 else ""))
        if cat:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
    main_cat = max(cat_counts, key=cat_counts.get) if cat_counts else None
    block_order = CATEGORY_FOCUS_ORDER.get(main_cat, DEFAULT_BLOCK_ORDER) if main_cat else DEFAULT_BLOCK_ORDER

    blocks = [_clip_focus_block(clip) for clip in products]
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
    needs_reorder = scattered or inversions >= 2 or time_backtracks >= 2
    if not needs_reorder:
        if base_reordered != clips:
            _log(f"卖点段落排序: 保持AI原序，仅修正hook/close位置 ({len(clips)}段)")
        else:
            _log(f"卖点段落排序: 保持AI原序 ({len(clips)}段)")
        return base_reordered

    anchor = next((block for block in blocks if block != "其他"), blocks[0] if blocks else None)
    if anchor in block_order:
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
    _log(f"卖点段落排序: 自适应整理({main_cat or '通用'}，锚点={anchor}) {' → '.join(block_summary)}{detail}")

    reordered = (hooks[:1] if hooks else []) + ordered_products + closes
    return reordered


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





def _trim_filler_start(clips, cleaned_srt, log_fn=None):
    """裁掉片段开头的废话：1)整条SRT是废话词 2)SRT文本以废话前缀开头(按字符比例裁时间)"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips or not cleaned_srt:
        return clips

    _kw_local_fb = _get_keywords()
    FILLER_WORDS = set(_kw_local_fb["filler_words"])
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
    for ct, text, start, end, score, dur, *_ in clips:
        new_start = start
        # Hook片段也要裁掉开头的废话SRT条目和废话前缀
        # 但不在_fix_clip_boundaries中做前向延伸（保持爆点起始）
        # 例如: "没有了 宝宝 / 来我跟你们讲 / 这套衣服千万不要错过"
        #       → 裁掉前两句废话，从"这套衣服"开始
        is_hook = 'hook' in ct.lower()

        # 第一步：跳过片段开头整条是废话的SRT条目（Hook也参与）
        for s, e, norm in entries:
            if e <= start:
                continue
            if s >= end:
                break
            if s < start:
                continue
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
        norm_text = re.sub(r'[^\u4e00-\u9fff\w]', '', text.strip())
        filler_prefix_len = 0
        for fw in _sorted_filler:
            if not fw:
                continue
            if norm_text.startswith(fw):
                filler_prefix_len = len(fw)
                break  # 长前缀优先，第一个匹配就是最长的

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
                    prefix_trim_count += 1

        new_dur = end - new_start
        if new_dur < 2.0:
            trimmed.append((ct, text, start, end, score, dur, *_))
        else:
            trimmed.append((ct, text, new_start, end, score, new_dur, *_))

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

    source_hooks = [c for c in source_clips if _is_hook_clip(c)]
    source_closes = [c for c in source_clips if _is_close_clip(c)]

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
                if not _is_close_clip(clip):
                    finalized[idx] = _with_type(clip, "hook")
                    repairs.append("首段提拔Hook")
                    break

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
        if not restored and len(finalized) >= 3:
            for idx in range(len(finalized) - 1, -1, -1):
                if not _is_hook_clip(finalized[idx]):
                    finalized[idx] = _with_type(finalized[idx], "close")
                    repairs.append("末段标记Close")
                    break

    hooks = [c for c in finalized if _is_hook_clip(c)]
    closes = [c for c in finalized if _is_close_clip(c)]
    others = [c for c in finalized if not _is_hook_clip(c) and not _is_close_clip(c)]
    if hooks or closes:
        finalized = (hooks[:1] if hooks else []) + others + closes

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


def _fix_clip_boundaries(clips, cleaned_srt, log_fn=None):
    """
    检查每个片段的 start/end 是否切割了完整的 SRT 句子.
    如果切割了，自动扩展边界到最近的 SRT 句子边界.
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips or not cleaned_srt:
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
            if not force_append and not (_ends_need_context(pieces[-1]) or _starts_as_continuation(nt)):
                break
            delta = max(0.0, ne - new_end)
            if total_before_context + context_extra_used + delta > context_total_cap and not (is_priority_boundary and force_append):
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


def _filter_price_and_cta(clips, log_fn=None):
    """硬过滤：删除包含价格/报价/购物车/下单/链接的片段，AI Prompt拦不住就用代码拦"""
    def _log(msg):
        if log_fn: log_fn(msg)

    # 价格数字模式：2-4位数字+元/块，或纯数字价格（99/199/299等）
    price_patterns = [
        re.compile(r'\d{2,4}\s*[元块]'),           # 199元, 300块
        re.compile(r'[到拿]手[价]?\s*\d'),          # 到手价199, 拿到手99
        re.compile(r'\d{2,4}\s*[多几]?[块元]'),     # 300多块
        re.compile(r'(?:只要|才|仅)[一两三四五六七八九十百千万\d]+[块元]'),  # 只要199元
        re.compile(r'原价|秒杀价|福利价|破价|到手价'),
        re.compile(r'[一两三四五六七八九十百千万\d]+[多来几]?[块元]'),  # 十几块, 一百多块, 二十来块
        re.compile(r'[一二三四五六七八九十]\s*折'),   # 一折, 两折
        re.compile(r'半价|对折'),                      # 半价
        re.compile(r'\d+\s*折'),                     # 3折, 5折
        re.compile(r'[到拿]手价?\s*[一两三四五六七八九十百千万\d]+'),  # 到手一百多
        re.compile(r'拍.*链接|链接.*拍|去拍|赶紧拍|刷新拍'),  # CTA
    ]
    # 绝对禁止词：从关键词管理读取（用户可自定义）
    _kw_fw = load_keywords()
    forbidden_words = _kw_fw["forbidden_phrases"]

    filtered = []
    removed = 0
    for ct, text, s, e, sc, d, *_ in clips:
        clean = re.sub(r'【|】', '', text)
        # 检查禁止词
        has_forbidden = any(w in clean for w in forbidden_words)
        # 检查价格模式
        has_price = any(p.search(clean) for p in price_patterns)
        if has_forbidden or has_price:
            reason = []
            is_hook_type = 'hook' in ct.lower()
            if has_forbidden:
                matched = [w for w in forbidden_words if w in clean]
                reason.append(f'违禁词:{",".join(matched)}')
            if has_price:
                reason.append('价格模式')
            # Hook含脏话/违禁词时降级为Product，而非直接删除（保留内容）
            if is_hook_type and has_forbidden and not has_price:
                new_ct = 'product' if ct.lower() == 'hook' else ct.replace('Hook', 'Product').replace('hook', 'product')
                _log(f'  价格过滤: Hook降级 [{ct}→{new_ct}] "{clean[:30]}..." ({";".join(reason)})')
                filtered.append((new_ct, text, s, e, sc, d, *_))
            else:
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
        # 检查是否为主播回弹幕/互动（不限时长）
        clean = text.strip()
        for pattern in HOST_CHAT_PATTERNS:
            if pattern.search(clean):  # 用search替代match，匹配任意位置
                is_noise = True
                break
        if is_noise:
            removed += 1
            _log(f"废话过滤: 移除 '{text[:20]}'({d:.1f}s，主播回弹幕)")
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

    if len(clips) < 3:
        return clips

    # 构建品类词库
    ALL_CATEGORIES = {
        "上衣": ["上衣","T恤","衬衫","针织衫","卫衣","打底衫","小衫","衬衣","网纱罩衫","罩衫",
                 "毛衣","短袖","长袖","吊带衫","背心","抹胸","针织","开衫","開衫","针织开衫","针织開衫"],
        "裤子": ["裤子","牛仔裤","阔腿裤","打底裤","工装裤","休闲裤","长裤","短裤","九分裤",
                 "小脚裤","直筒裤","牛奶裤","烟管裤","哈伦裤","裤"],
        "裙子": ["裙子","连衣裙","半身裙","A字裙","包臀裙","长裙","短裙","百褶裙","裙",
                 "吊带裙","碎花裙","鱼尾裙","蛋糕裙","一步裙","旗袍裙","吊带","背心裙","腰头"],
        "外套": ["外套","风衣","西装","羽绒服","大衣","夹克","棉服","皮衣","马甲"],
        "套装": ["套装","四件套","三件套","两件套","三件","四件","成套","整套","全套"],
        "鞋子": ["鞋","鞋子","凉鞋","运动鞋","高跟鞋","平底鞋","单鞋","靴子","老爹鞋"],
    }

    # 从 SRT 统计每个品类的出现频率，确定主品类
    cat_counts = {}
    for cat, keywords in ALL_CATEGORIES.items():
        count = sum(1 for kw in keywords if kw in cleaned_srt)
        if count > 0:
            cat_counts[cat] = count
    if not cat_counts:
        return clips
    # 使用用户指定的主品类（如果有）
    if preferred_cat and preferred_cat in ALL_CATEGORIES:
        main_cat = preferred_cat
    else:
        main_cat = max(cat_counts, key=cat_counts.get)
    main_kws = set(ALL_CATEGORIES.get(main_cat, []))

    protected_cats = {main_cat}
    _log(f"跨品类扫描: 主推严格模式，仅保留{main_cat}或同段包含{main_cat}的搭配说明")

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
            has_main = any(kw in text for kw in main_kws)
            if has_main:
                # 同时有主品类和次品类 → 搭配说明，保留
                kept.append((ct, text, s, e, sc, d, *_))
            else:
                # 只有次品类没有主品类 → 即使是关联品类也踢出
                if other_cat in protected_cats:
                    _log(f"跨品类踢出 [{ct}] {text[:30]}...(只含{other_cat}不含{main_cat})")
                removed += 1
        else:
            kept.append((ct, text, s, e, sc, d, *_))

    if removed:
        _log(f"跨品类扫描: 踢出 {removed} 个非{main_cat}片段，保留 {len(kept)} 个")
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
}

# 品类 → 卖点排序优先级映射
CATEGORY_FOCUS_ORDER = {
    "上衣": ["版型显瘦", "面料质感", "尺寸长度", "穿着体验", "品质细节", "工艺细节", "颜色氛围", "场景搭配", "性价比", "对比优势", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "裤子": ["版型显瘦", "尺寸长度", "品质细节", "面料质感", "穿着体验", "颜色氛围", "场景搭配", "性价比", "对比优势", "工艺细节", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "裙子": ["版型显瘦", "颜色氛围", "尺寸长度", "场景搭配", "品质细节", "面料质感", "穿着体验", "工艺细节", "性价比", "对比优势", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "外套": ["版型显瘦", "品质细节", "面料质感", "穿着体验", "场景搭配", "尺寸长度", "颜色氛围", "工艺细节", "性价比", "对比优势", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "套装": ["场景搭配", "版型显瘦", "颜色氛围", "面料质感", "品质细节", "性价比", "对比优势", "尺寸长度", "穿着体验", "工艺细节", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
    "鞋子": ["场景搭配", "品质细节", "颜色氛围", "尺寸长度", "面料质感", "性价比", "对比优势", "穿着体验", "工艺细节", "情绪感染", "流行趋势", "紧迫稀缺", "其他"],
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
}

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
    base_url = settings["base_url"].rstrip("/")
    model = settings.get("model", "deepseek-chat")

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
            f"{base_url}/v1/chat/completions",
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
    CATEGORIES = {
        "\u4e0a\u8863": ["\u4e0a\u8863", "T\u6064", "\u886c\u886b", "\u9488\u7ec7\u886b", "\u536b\u8863", "\u6253\u5e95\u886b", "\u5c0f\u886b", "\u886b\u8863", "\u7f51\u7eb1\u7f69\u886b", "\u7f69\u886b",
                       "\u897f\u8863", "\u6bdb\u8863", "\u77ed\u8896", "\u957f\u8896", "\u540a\u5e26", "\u80cc\u5fc3", "\u62b9\u80f8", "\u8fd9\u4ef6", "\u8fd9\u6b3e", "\u8fd9\u6761",
                       "\u9488\u7ec7", "\u6bdb\u8863", "\u536b\u8863", "\u5c0f\u886b", "\u886c\u8863", "\u7f69\u886b", "\u5e26\u94fe\u8863",
                       "开衫", "開衫", "针织开衫", "针织開衫"],
        "\u88e4\u5b50": ["\u88e4\u5b50", "\u725b\u4ed4\u88e4", "\u4e5d\u5206\u88e4", "\u77ed\u88e4", "\u897f\u88c5\u88e4", "\u5bbd\u677e\u88e4", "\u76f4\u7b52\u88e4", "\u5c0f\u811a\u88e4", "\u8783\u87f9\u88e4", "\u725b\u4ed4",
                       "\u88e4", "\u88e4\u88c5", "\u8fd9\u6761"],
        "\u88d9\u5b50": ["\u88d9\u5b50", "\u8fde\u8863\u88d9", "\u767e\u88d9\u88d9", "A\u5b57\u88d9", "\u5305\u817f\u88d9", "\u5939\u514b\u88d9", "\u7f8a\u7ed2\u88d9", "\u82b1\u5965\u88d9", "\u88d9",
                       "\u9c7c\u5c3e\u88d9", "\u88d9\u88c5", "\u77ed\u88d9", "\u957f\u88d9"],
        "\u5916\u5957": ["\u5916\u5957", "\u5927\u8863", "\u7fbd\u7ed2\u670d", "\u76ae\u8863", "\u897f\u88c5", "\u98ce\u8863", "\u6bdb\u5462", "\u590d\u53e4", "\u725b\u4ed4\u5916\u5957", "\u76ae\u8346",
                       "\u590d\u53e4\u5927\u8863", "\u77ed\u5916\u5957", "\u76ae\u8863"],
        "\u5957\u88c5": ["\u5957\u88c5", "\u56db\u4ef6\u5957", "\u4e09\u4ef6\u5957", "\u8fd0\u52a8\u5957", "\u8fde\u8863\u8863", "\u8fde\u8863\u88d9", "\u5957\u88c5\u5916\u5957", "\u5957\u88c5", "\u5168\u5957",
                       "\u5c0f\u9999\u98ce", "\u996d\u5957"],
        "\u978b\u5b50": ["\u978b", "\u978b\u5b50", "\u957f\u9774", "\u8fd0\u52a8\u978b", "\u9ad8\u8ddf\u978b", "\u5e73\u5e95\u978b", "\u77ed\u9774", "\u9774\u5b50", "\u77ed\u9774"],
    }

    if force_category and force_category in CATEGORIES:
        return force_category

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
        # Copy from _post_filter_cross_category
        ALL_CATEGORIES_MODULE = {
            'shangyi': [],
            'kuzi': [],
            'qunzi': [],
            'waitao': [],
            'taozhuang': [],
            'xiezi': [],
        }
    return ALL_CATEGORIES_MODULE
def is_enabled():
    settings = load_settings()
    # 有 API Key 就启用 AI，不需要额外勾选
    # 之前要求 enabled=True，导致很多用户填了 Key 但没勾启用，走关键词兜底产出垃圾
    return bool(settings.get("api_key"))
