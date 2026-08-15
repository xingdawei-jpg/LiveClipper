"""Deterministic quality checks for immutable AI selection candidates.

This module only rejects high-confidence ASR residue and finds word-exact
leading-fragment boundaries. It never rewrites a kept candidate or estimates
timestamps, so the selection contract remains tied to spoken audio.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from content_policy import blocks_role, interaction_policy_kind
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
    # A bare "...的。" at the beginning is the tail of a prior sentence, not a
    # self-contained product claim. Keep the vocabulary narrow so complete
    # descriptions such as "显瘦的版型" remain available.
    ("孤立形容词残句", re.compile(r"^(?:吸引|好看|漂亮|显瘦|高级|舒服|适合)的(?:[。！？!?，,]|$)")),
    ("上下文指代残句", re.compile(r"^(?:在这里|到这里|这边|那边)[，,。\s]*(?:整个|这个|这件|它|你看)")),
    ("跨句硬拼", re.compile(r"(?:在这里|到这里)[。！？!?，,\s]+(?:整个|这个|这件|它|你看)")),
    ("直播问答残句", re.compile(r"^(?:(?:如果|那|这个|这件).{0,14})?(?:这件|这个|那件|那条).{0,8}(?:不行|不对|不搭|不适合)")),
    ("未闭合列举", re.compile(r"^(?:两个|两)个点[，,、\s]*一[，,、\s]")),
    ("悬空收尾", re.compile(r"(?:或者你|这一点|那一点|有点设计的麻|全部经过两道的水洗的那个)[。！？!?，,\s]*$")),
    # A semantic turn that stops at "风吹过来整" has lost the noun or
    # consequence that makes the sentence intelligible. Keep this narrowly
    # tied to the observed ASR residue; "整" elsewhere is a normal word.
    ("未完成场景残句", re.compile(r"(?:风吹过来整)[。！？!?，,\s]*$")),
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
    # A final "还有吗" turns this into an on-air follow-up, not an independent
    # anti-exposure selling point. Ordinary anti-exposure claims remain valid.
    ("直播追问残句", re.compile(r"(?:防走光|打底|内搭|下面穿).{0,28}(?:还有吗|有吗|有没有)[。！？!?\s]*$")),
    # ASR can collapse height and weight into a bare tail such as
    # "你就可以这样去穿177142斤". It has no standalone product value, even
    # when useful sizing information is allowed in the body.
    ("裸身高体重残句", re.compile(r"^(?:你|你们|姐妹(?:们)?)?.{0,16}(?:穿|试穿).{0,4}1[4-9]\d(?:1\d{2}|[4-9]\d)斤[。！？!?\s]*$")),
    # These stop at a helper verb and need the next clause to carry any
    # meaning.  Do not match a completed question such as "会不会把头发给
    # 撅起来？"; only the bare verb tail is unusable by itself.
    ("未完成请求残句", re.compile(r"(?:会不会|能不能|要不要).{0,20}(?:给|帮|拿|放|看|问|说|叫|找|递)[。！？!?，,\s]*$")),
    # A hard ASR punctuation break in the middle of "如果喜欢这个..." loses
    # the condition's object and cannot form an independent buyer statement.
    ("破碎条件残句", re.compile(r"(?:我觉得大家)?如果喜欢[。！？!?，,\s]+(?:这个|这件|这条|这种)")),
    # These are directions for the live overlay rather than product evidence.
    # The product material statement that sometimes follows is available in
    # its own clean candidate, so keep this mixed live-room sentence out.
    ("直播画面调度", re.compile(r"(?:这字大(?:的有点)?看不见|这个太大了?[，,\s]*(?:小一点|大一点))")),
    # A candidate beginning with "的一个混纺" is an orphaned tail from the
    # preceding material sentence, not a complete claim by itself.
    ("成分续句残片", re.compile(r"^的一个(?:混纺|混合|成分)[，,。！？!?\s]*")),
    # Switching people/styles is live presentation coordination, not a
    # meaningful close for the selected product.
    ("直播换人展示", re.compile(r"^(?:换个风格[，,]?换个人|换个人给(?:你们|大家)看看)")),
)
_PRICE_COST_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("数字成本报价", re.compile(r"(?:成本|价格|单价).{0,8}\d{1,4}")),
    # Live rooms frequently omit "元" while changing a price, for example
    # "改价本来是290".  It is still a price announcement when price is blocked.
    ("直播改价报价", re.compile(r"(?:改价|调价)[^。！？!?]{0,16}(?:\d{2,4}|[一二三四五六七八九十百千万]+)")),
    ("每米报价", re.compile(r"\d{2,4}\s*(?:多)?\s*(?:一|1)米")),
    ("价格导向话术", re.compile(r"(?:价位|成本|收费|计价|便宜|越贵|更贵|太贵|很贵|不贵|不便宜|抢着买|秒带|秒拍)")),
)

# Hook needs a narrower standard than a body candidate.  An address such as
# "你们" may be fine inside live speech, but it cannot start a short clip unless
# it immediately forms a complete buyer-facing sentence.  Keep this conservative:
# it only rejects malformed address-plus-benefit fragments, not ordinary product
# claims that happen to mention the audience.
_HOOK_AUDIENCE_FRAGMENT_RE = re.compile(
    r"^(?:你们|大家|姐妹(?:们)?|宝宝(?:们)?)(?!"
    r"[，,、：:]|(?:有没有|有没|知道吗|看到了吗|想|要|穿|试|买|如果|要是|"
    r"这件|这个|这条|这种))"
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


def candidate_quality_flags(text: Any, content_policy: Any = None) -> list[str]:
    """Return only high-confidence defects suitable for a hard candidate gate."""

    value = re.sub(r"\[[vV]\d+\]\s*", "", str(text or "")).strip()
    if not _compact_text(value):
        return ["空文案"]
    flags = [label for label, pattern in _HIGH_CONFIDENCE_GARBLE_RULES if pattern.search(value)]
    flags.extend(label for label, pattern in _HIGH_CONFIDENCE_FRAGMENT_RULES if pattern.search(value))
    if any(pattern.search(value) for _label, pattern in _PRICE_COST_RULES):
        blocked, _reason = blocks_role(content_policy, "price", value)
        if blocked:
            flags.append("价格/成本报价")
    safety_reason = live_interaction_or_size_response_reason(value)
    if safety_reason:
        blocked, _reason = blocks_role(
            content_policy,
            interaction_policy_kind(safety_reason),
            value,
        )
    else:
        blocked = False
    if safety_reason and blocked:
        flags.append(safety_reason)
    return list(dict.fromkeys(flags))


def hook_candidate_quality_flags(text: Any) -> list[str]:
    """Return Hook-only defects without deleting usable body material.

    A caller uses this only to deny the Hook role.  The immutable text and its
    timestamps stay available as Product/evidence material for the director.
    """

    value = re.sub(r"\[[vV]\d+\]\s*", "", str(text or "")).strip()
    compact = _compact_text(value)
    if not compact:
        return ["Hook空文案"]
    if _HOOK_AUDIENCE_FRAGMENT_RE.match(compact):
        return ["Hook人称残句"]
    return []


def filter_candidate_clips(
    clips: Iterable[tuple[Any, ...]],
    log_fn: Callable[[str], None] | None = None,
    content_policy: Any = None,
) -> list[tuple[Any, ...]]:
    """Remove obvious garbled candidates while preserving all kept tuples."""

    original = [tuple(clip) for clip in clips or []]
    kept: list[tuple[Any, ...]] = []
    removed: list[tuple[tuple[Any, ...], list[str]]] = []
    for clip in original:
        text = clip[1] if len(clip) > 1 else ""
        flags = candidate_quality_flags(text, content_policy=content_policy)
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
