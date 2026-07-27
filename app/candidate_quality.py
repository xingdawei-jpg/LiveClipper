"""Deterministic quality checks for immutable AI selection candidates.

This module only rejects high-confidence ASR residue and finds word-exact
leading-fragment boundaries. It never rewrites a kept candidate or estimates
timestamps, so the selection contract remains tied to spoken audio.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from selection_safety import live_interaction_or_size_response_reason


_LEADING_FRAGMENT_QUESTION_RE = re.compile(
    r"^(?P<prefix>"
    r"(?:(?:然后|而且|但是|不过|所以|其实|就是))?"
    r"[\u4e00-\u9fffA-Za-z0-9]{1,8}的"
    r"(?:哎|啊|呀|哦|诶)?"
    r")"
    r"(?=(?:你们|大家|姐妹|宝宝)(?:有没有发现|有没发现|发现没有|知道吗|看到了吗))"
)

_HIGH_CONFIDENCE_GARBLE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("疑似ASR错词", re.compile(r"(?:富贵这|肌励感|森侣|麻着个料子|树质非常高)")),
    ("疑似ASR断词", re.compile(r"(?:整个的版|还原本真|这件感兴趣的是)")),
    ("明显ASR错词", re.compile(r"(?:麻巾跟肠温柔软|支树织|枝树密度|独望无处|稀疏疏嘟|烂大些|带吊印|白白亚麻|亚一0|下意识中还蛮好看的|好的[，,]麻|烂不了网|版型[，,]?呃|是整个的[。！]?版型)")),
    ("异常重复", re.compile(r"白搭白搭绿")),
    ("英文残片", re.compile(r"(?i)(?<![a-z])ca的")),
)
_HIGH_CONFIDENCE_FRAGMENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("上下文指代残句", re.compile(r"^(?:在这里|到这里|这边|那边)[，,。\s]*(?:整个|这个|这件|它|你看)")),
    ("跨句硬拼", re.compile(r"(?:在这里|到这里)[。！？!?，,\s]+(?:整个|这个|这件|它|你看)")),
    ("直播问答残句", re.compile(r"^(?:(?:如果|那|这个|这件).{0,14})?(?:这件|这个|那件|那条).{0,8}(?:不行|不对|不搭|不适合)")),
    ("未闭合列举", re.compile(r"^(?:两个|两)个点[，,、\s]*一[，,、\s]")),
    ("悬空收尾", re.compile(r"(?:或者你|这一点|那一点|有点设计的麻|全部经过两道的水洗的那个)[。！？!?，,\s]*$")),
    # These are not product claims. They are unedited live-chat/try-on tails
    # from the source transcript, so they cannot become a standalone clip.
    ("直播闲聊残留", re.compile(r"(?:那|就)?拜拜(?:亚麻)?|脑壳痛(?:了)?|我这个面料我不骗你")),
    (
        "个人体重试穿结论",
        re.compile(r"(?:\d{2,3}|[一二三四五六七八九十百]+)斤(?:内)?(?:我|你|她|他).{0,12}(?:没毛病|没问题|能穿|可以)"),
    ),
    ("口语病句", re.compile(r"整件衣服(?:唉|哎|诶)好多毛边|(?:你看)?毛边[。！？!?，,\s]*袖口的毛边")),
    ("亚麻转写残句", re.compile(r"^麻它是植物的根茎|亚麻纱很难[。！？!?，,\s]*$")),
    ("直播核算尾话", re.compile(r"(?:真假了|真的假的).{0,24}(?:计算机|按一下)")),
)
_PRICE_COST_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("数字成本报价", re.compile(r"(?:成本|价格|单价).{0,8}\d{1,4}")),
    ("每米报价", re.compile(r"\d{2,4}\s*(?:多)?\s*(?:一|1)米")),
    ("价格导向话术", re.compile(r"(?:价位|成本|收费|计价|便宜|越贵|更贵|太贵|很贵|不贵|不便宜|抢着买|秒带|秒拍)")),
)


def _compact_text(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or ""))


def leading_fragment_trim(tokens: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a word-exact trim point for a dangling pre-question fragment."""

    usable = []
    combined_parts = []
    for token in tokens or []:
        norm = _compact_text(token.get("norm") or token.get("text"))
        if not norm:
            continue
        try:
            start = float(token.get("start"))
            end = float(token.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        usable.append({"norm": norm, "start": start, "end": end})
        combined_parts.append(norm)

    combined = "".join(combined_parts)
    match = _LEADING_FRAGMENT_QUESTION_RE.match(combined)
    if not match or len(combined) - match.end("prefix") < 6:
        return None

    target_offset = match.end("prefix")
    consumed = 0
    for index, token in enumerate(usable):
        consumed += len(token["norm"])
        if consumed >= target_offset:
            if consumed != target_offset:
                return None
            boundary = usable[index + 1]["start"] if index + 1 < len(usable) else token["end"]
            return {"prefix": match.group("prefix"), "boundary": boundary}
    return None


def candidate_quality_flags(text: Any) -> list[str]:
    """Return only high-confidence defects suitable for a hard candidate gate."""

    value = re.sub(r"\[[vV]\d+\]\s*", "", str(text or "")).strip()
    if not _compact_text(value):
        return ["空文案"]
    flags = [label for label, pattern in _HIGH_CONFIDENCE_GARBLE_RULES if pattern.search(value)]
    flags.extend(label for label, pattern in _HIGH_CONFIDENCE_FRAGMENT_RULES if pattern.search(value))
    flags.extend(label for label, pattern in _PRICE_COST_RULES if pattern.search(value))
    safety_reason = live_interaction_or_size_response_reason(value)
    if safety_reason:
        flags.append(safety_reason)
    return list(dict.fromkeys(flags))


def filter_candidate_clips(
    clips: Iterable[tuple[Any, ...]],
    log_fn: Callable[[str], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Remove obvious garbled candidates while preserving all kept tuples."""

    original = [tuple(clip) for clip in clips or []]
    kept: list[tuple[Any, ...]] = []
    removed: list[tuple[tuple[Any, ...], list[str]]] = []
    for clip in original:
        text = clip[1] if len(clip) > 1 else ""
        flags = candidate_quality_flags(text)
        if flags:
            removed.append((clip, flags))
        else:
            kept.append(clip)

    if not removed:
        return original

    minimum_kept = min(len(original), max(3, min(12, len(original) // 3)))
    if len(kept) < minimum_kept:
        if log_fn:
            log_fn("候选质量闸门: 可用候选过少，保留原候选并交由AI审稿")
        return original

    if log_fn:
        examples = "；".join(
            f"{','.join(flags)}:{str(clip[1])[:22]}"
            for clip, flags in removed[:4]
        )
        log_fn(f"候选质量闸门: 排除 {len(removed)} 个明显ASR乱码/病句候选（{examples}）")
    return kept
