r"""Quality-preserving post-processing for local SenseVoice transcripts.

The correction layer is intentionally conservative: it only applies known
fashion-live homophone substitutions or exact replacements supplied by the
user.  Timed tokens are rewritten at existing token boundaries, so downstream
video cuts never depend on invented timestamps.

Optional user corrections live at::

    %APPDATA%\LiveClipper\asr_corrections.json

with this shape::

    {"replacements": {"识别错词": "正确术语"}}

Set ``LIVECLIPPER_ASR_CORRECTIONS`` to use a different file.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable


_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z]+(?:['-][A-Za-z]+)*|\d+(?:[.,:]\d+)*")
_PUNCTUATION_CLUSTER_RE = re.compile(r"[。！？!?，,；;：:、](?:\s*[。！？!?，,；;：:、])+")
_CONTEXTUAL_PUNCTUATION_REPAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(首先)[。！？!?；;]+(?=\s*这个世界)"), r"\1，"),
    (re.compile(r"(各种)[。！？!?；;]+(?=\s*(?:高端|高级|优质|好的|面料))"), r"\1"),
    (re.compile(r"(高端面料)[。！？!?；;]+(?=\s*但是)"), r"\1，"),
    (re.compile(r"(因为(?:它|这件衣服)?(?:是|其实))[。！？!?；;]+(?=\s*(?:亚麻|棉|羊毛|羊绒|面料|成本|价格|工艺))"), r"\1，"),
    (re.compile(r"(收到(?:这件|这个)?面料的那一刻)[。！？!?；;]+(?=\s*才能)"), r"\1，"),
    (re.compile(r"(这个衣服就是属于)[。！？!?；;]+(?=\s*其实)"), r"\1，"),
    (re.compile(r"(我)[。！？!?；;]+(?=\s*给所有)"), r"\1"),
    (re.compile(r"(为富人而)[。！？!?；;]+(?=\s*单独)"), r"\1"),
    (re.compile(r"(一眼心动)女生[。！？!?]+(?=\s*大概率)"), r"\1。女生"),
)

# Keep these context-bound: the local ASR engine is also used for non-fashion
# categories, so a bare two-character homophone must not be replaced globally.
_DOMAIN_CORRECTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"app(?=不小)"), "衣服"),
    (re.compile(r"卡马王小(?=[，,。！？!?；;\s]*(?:啊|哦|因为|这|$))"), "尺码往小"),
    (re.compile(r"交底门经理"), "交叠门襟领"),
    (re.compile(r"森绿(?=[，,。！？!?；;\s]*(?:而且|风|感|款|$))"), "僧侣"),
    (re.compile(r"森侣(?=[，,。！？!?；;\s]*(?:禅意|而且|风|感|款|$))"), "僧侣"),
    (re.compile(r"高知(?=(?:数|亚麻|棉|纱|面料))"), "高支"),
    (re.compile(r"高织(?=(?:数|亚麻|棉|纱|面料))"), "高支"),
    (re.compile(r"色只(?=(?:面料|工艺|纱|布|效果))"), "色织"),
    (re.compile(r"色质(?=(?:它|面料|工艺|纱|布|染|做|的|麻))"), "色织"),
    (re.compile(r"色置(?=(?:面料|工艺|纱|布|染|做|的|麻))"), "色织"),
    (re.compile(r"色之(?=[，,。！？!?；;\s]*(?:再\s*)?先染纱)"), "色织"),
    (re.compile(r"芝麻(?=\s*和\s*(?:那种\s*)?(?:普通|常规)?\s*(?:染色)?麻)"), "色织麻"),
    (re.compile(r"(?<=支数)(?:枝数|支数)(?=越高)"), ""),
    (re.compile(r"枝数(?=[，,。！？!?；;\s]*(?:枝数[，,。！？!?；;\s]*)?(?:高|低|细|粗|多|少|是|的|亚麻|棉|纱|面料))"), "支数"),
    (re.compile(r"枝树(?=[，,。！？!?；;\s]*(?:啊|呀|哦|这种|那种|高枝|$))"), "支数"),
    (re.compile(r"知数(?=[，,。！？!?；;\s]*高)"), "支数"),
    (re.compile(r"高枝(?=(?:的)?[，,。！？!?；;\s]*(?:啊|呀|哦|这种|那种|亚麻|棉|纱|面料|$))"), "高支"),
    (re.compile(r"高颗重(?=[，,。！？!?；;\s]*(?:的|亚麻|棉|面料|$))"), "高克重"),
    (re.compile(r"克重(?=亚麻克重越高)"), ""),
    (re.compile(r"下意失踪(?=[，,。！？!?；;\s]*(?:的|穿法|风格|$))"), "下衣失踪"),
    (re.compile(r"采雷率"), "踩雷率"),
    (re.compile(r"采住率(?=[，,。！？!?；;\s]*(?:会|很|低|高|$))"), "踩雷率"),
    (re.compile(r"够兴(?=[，,。！？!?；;\s]*我在)"), "够信"),
    (re.compile(r"(?:一|1)00(?=纯(?:亚麻|棉|羊毛|羊绒))"), "100"),
    (re.compile(r"(?:八5|8谷)(?=折)"), "85"),
    (re.compile(r"13\s*V尼(?=羊绒)"), "13微米"),
    (re.compile(r"怕缩水17(?=[，,。！？!?；;\s]*(?:我跟你讲|麻呢|$))"), "怕缩水"),
    (re.compile(r"麻呢(?=[，,]?\s*它其实是按克重)"), "亚麻呢"),
    (re.compile(r"进口吗(?=[，,。！？!?；;\s]*(?:如果|$))"), "进口麻"),
    (re.compile(r"冲动小飞"), "冲动消费"),
    (re.compile(r"不要冲小费"), "不要冲动消费"),
    (re.compile(r"成动消费"), "冲动消费"),
    (re.compile(r"薏米面料(?=\s*(?:(?:能|可以)\s*)?(?:做到?)?\s*\d)"), "一米面料"),
    (re.compile(r"每一米度(?=\s*\d+(?:\.\d+)?(?:多|块|元)?)"), "每一米都"),
    (re.compile(r"边捡的麻"), "便宜的麻"),
    (re.compile(r"你坏[，,]?\s*你冷水洗"), "你换冷水洗"),
    (re.compile(r"不会被重蛀"), "不会被虫蛀"),
    (re.compile(r"麻着(?:个)?料子"), "麻质料子"),
    (re.compile(r"肌励感"), "肌理感"),
    (re.compile(r"树质(?=[，,。！？!?；;\s]*非常高)"), "支数"),
    (re.compile(r"(?<=撞衫这个事情比较)ca(?=的)"), "care"),
    (re.compile(r"整个的版(?=[，,。！？!?；;\s]*(?:然后|很|比较|$))"), "整个版型"),
    (re.compile(r"很非常(?=[，,。！？!?；;\s]*(?:适合|好|高级|舒服|柔软))"), "非常"),
    (re.compile(r"悬乎(?=[啊呀？?，,\s]*(?:它这|这就是|就是有|$))"), "悬殊"),
    (re.compile(r"扎腹感"), "扎肤感"),
    (re.compile(r"它的腹感(?=[，,。！？!?；;\s]*(?:(?:还是|也|比较|特别)[，,。！？!?；;\s]*)?(?:很|非常)?[，,。！？!?；;\s]*(?:舒服|柔软|软|细腻|好))"), "它的肤感"),
    (re.compile(r"手工拉毛被(?=[，,。！？!?；;\s]*全部(?:是)?纯手工)"), "手工拉毛边"),
    (re.compile(r"常规养麻"), "常规亚麻"),
    (re.compile(r"一般养苗(?=[，,。！？!?；;\s]*(?:要|更)?重)"), "一般亚麻"),
    (re.compile(r"为富人而单(?=[，,。！？!?；;\s]*单独)"), "为富人而"),
    (re.compile(r"板型(?=(?:偏|很|大|小|宽|正|好|设计|比较|上身|适合|做|是|的|感))"), "版型"),
    (re.compile(r"小个字(?=(?:版|穿|女生|姐妹|也|可以|适合|选|建议))"), "小个子"),
    (re.compile(r"显受(?=(?:效果|又|还|很|的|吗|穿|看|一些))"), "显瘦"),
    (re.compile(r"遮月(?=(?:效果|又|还|很|的|吗|穿|看|一些))"), "遮肉"),
    (re.compile(r"流量汗删"), "流浪汉衫"),
    (re.compile(r"紧上的衣服"), "紧身的衣服"),
    (re.compile(r"吃量(?=[，,。！？!?；;\s]*如果(?:非得)?按(?:体重|身高))"), "尺码"),
    (re.compile(r"一多穿"), "一衣多穿"),
    (re.compile(r"偏岔气(?=的一个款)"), "偏禅系"),
    (re.compile(r"100串元(?=[吗呢啊？?。])"), "1000元"),
)


def _normalize_punctuation_cluster(match: re.Match[str]) -> str:
    cluster = match.group(0)
    if "？" in cluster or "?" in cluster:
        return "？"
    if "！" in cluster or "!" in cluster:
        return "！"
    if "。" in cluster:
        return "。"
    if "；" in cluster or ";" in cluster:
        return "；"
    if "：" in cluster or ":" in cluster:
        return "："
    return "，"


def _correction_file() -> str:
    configured = os.environ.get("LIVECLIPPER_ASR_CORRECTIONS", "").strip()
    if configured:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return ""
    return os.path.join(appdata, "LiveClipper", "asr_corrections.json")


def _load_user_corrections(
    log_fn: Callable[[str], None] | None = None,
) -> list[tuple[re.Pattern[str], str]]:
    """Load opt-in exact substitutions; malformed files never disable ASR."""
    path = _correction_file()
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        replacements = payload.get("replacements") if isinstance(payload, dict) else None
        if not isinstance(replacements, dict):
            raise ValueError("missing replacements object")
        rules = []
        for source, target in replacements.items():
            source_text = str(source or "").strip()
            target_text = str(target or "").strip()
            if source_text and target_text and source_text != target_text:
                rules.append((re.compile(re.escape(source_text)), target_text))
        if log_fn and rules:
            log_fn(f"已载入 {len(rules)} 条本地 ASR 术语纠错")
        return rules
    except Exception as exc:
        if log_fn:
            log_fn(f"本地 ASR 术语纠错文件不可用，已忽略: {exc}")
        return []


def _replacement_units(text: str) -> list[str]:
    units = _TOKEN_RE.findall(text)
    return units or ([text] if text else [])


def _rewrite_timed_words(
    words: list[dict[str, Any]],
    rules: list[tuple[re.Pattern[str], str]],
) -> tuple[list[dict[str, Any]], int]:
    """Apply substitutions at token boundaries without inventing timestamps."""
    rewritten = [dict(word) for word in words]
    replacement_count = 0
    for pattern, replacement in rules:
        flat = "".join(str(word.get("text") or "") for word in rewritten)
        matches = list(pattern.finditer(flat))
        if not matches:
            continue

        starts: dict[int, int] = {}
        ends: dict[int, int] = {}
        offset = 0
        for index, word in enumerate(rewritten):
            starts[offset] = index
            offset += len(str(word.get("text") or ""))
            ends[offset] = index + 1

        for match in reversed(matches):
            first = starts.get(match.start())
            after_last = ends.get(match.end())
            if first is None or after_last is None or after_last <= first:
                continue
            source_words = rewritten[first:after_last]
            units = _replacement_units(replacement)
            if not units:
                # A narrowly scoped domain rule may remove an observed ASR
                # repetition. Delete only the matched timed tokens; the
                # remaining spoken words keep their original boundaries.
                if replacement == "":
                    del rewritten[first:after_last]
                    replacement_count += 1
                continue
            if len(units) == len(source_words):
                replacement_words = []
                for unit, source_word in zip(units, source_words):
                    item = dict(source_word)
                    item["text"] = unit
                    replacement_words.append(item)
            else:
                replacement_word = {
                    "text": replacement,
                    "start": source_words[0]["start"],
                    "end": source_words[-1]["end"],
                }
                confidences = [
                    word.get("confidence")
                    for word in source_words
                    if word.get("confidence") is not None
                ]
                if confidences:
                    replacement_word["confidence"] = min(confidences)
                replacement_words = [replacement_word]
            rewritten[first:after_last] = replacement_words
            replacement_count += 1
    return rewritten, replacement_count


def apply_domain_corrections(
    segments: list[dict[str, Any]],
    log_fn: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Correct text and its timed tokens while retaining every time range."""
    rules = list(_DOMAIN_CORRECTIONS) + _load_user_corrections(log_fn)
    corrected_segments = []
    total = 0
    for raw_segment in segments:
        segment = dict(raw_segment)
        text = str(segment.get("text") or "")
        text_count = 0
        for pattern, replacement in rules:
            text, count = pattern.subn(replacement, text)
            text_count += count
        for pattern, replacement in _CONTEXTUAL_PUNCTUATION_REPAIRS:
            text, count = pattern.subn(replacement, text)
            text_count += count
        text = _PUNCTUATION_CLUSTER_RE.sub(_normalize_punctuation_cluster, text).strip()

        words, word_count = _rewrite_timed_words(list(segment.get("words") or []), rules)
        segment["text"] = text
        segment["words"] = words
        corrected_segments.append(segment)
        total += max(text_count, word_count)

    if log_fn and total:
        log_fn(f"本地 ASR 字幕纠错: 安全修复 {total} 处（时间戳保持不变）")
    return corrected_segments, total


def improve_sensevoice_segments(
    segments: list[dict[str, Any]],
    log_fn: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return corrected raw segments plus readable, word-timed SRT segments."""
    corrected, _ = apply_domain_corrections(segments, log_fn=log_fn)
    try:
        from volcengine_asr import build_semantic_segments

        semantic = build_semantic_segments(corrected, log_fn=log_fn) or []
    except Exception as exc:
        if log_fn:
            log_fn(f"本地 ASR 语义断句不可用，保留原始语音段: {exc}")
        semantic = []
    return corrected, semantic
