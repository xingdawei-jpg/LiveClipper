"""Deterministic quality checks for immutable AI selection candidates.

This module only rejects high-confidence ASR residue and finds word-exact
leading-fragment boundaries. It never rewrites a kept candidate or estimates
timestamps, so the selection contract remains tied to spoken audio.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Sequence

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
    # These are recurring ASR substitutions inside otherwise tempting material
    # descriptions. A valid phrase must not launder an unplayable sentence.
    ("疑似ASR错词", re.compile(r"(?:非常的高会(?:热|闷)吗|会不被(?:面儿)?(?:热|闷)|想把它(?:防风|凉快))")),
    ("疑似ASR断词", re.compile(r"(?:整个的版|还原本真|这件感兴趣的是)")),
    ("明显ASR错词", re.compile(r"(?:麻巾跟肠温柔软|支树织|枝树密度|独望无处|稀疏疏嘟|烂大些|带吊印|白白亚麻|亚一0|下意识中还蛮好看的|好的[，,]麻|烂不了网|版型[，,]?呃|是整个的[。！]?版型)")),
    ("异常重复", re.compile(r"白搭白搭绿")),
    ("英文残片", re.compile(r"(?i)(?<![a-z])ca的")),
    # A source that still carries one of these observed local-ASR residues is
    # useful for a quality report or a targeted retry, never as a final
    # director utterance.  The patterns are narrow so normal fashion/size
    # claims remain in the complete pool.
    (
        "明显ASR错词",
        re.compile(
            r"(?:下0天|人间一定是直角|(?:是那个)?35厘米|"
            r"自带3(?:到|-)?5厘米的销售|A类母婴店|就是你小宝宝|像100斤葡萄)"
        ),
    ),
    # These are not merely colloquial.  The ASR unit has lost the word that
    # resolves the claim ("肩干嘛？"), joined two conjunctions, or stopped
    # immediately after a negation.  Do not infer or rewrite the missing
    # spoken word: keep only a source line a viewer can hear as complete.
    ("未闭合口播问句", re.compile(r"(?:会显得|显得).{0,12}(?:干嘛|什么(?:呢|啊|呀)?)[？?。！!，,\s]*$")),
    ("异常连接词", re.compile(r"^(?:是但是|而不是说是)")),
    (
        "未完成否定口播",
        re.compile(r"(?:而且|但是).{0,32}(?:上身|穿上).{0,12}(?:完全不|都不|不会不)[。！？!?，,\s]*$"),
    ),
    ("ASR谓语错位", re.compile(r"^它是大是(?:显瘦|好看|舒服)")),
)

_INVENTORY_PRESSURE_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:库存|限量|断货|首批|现货|最后\d*件|没了|拼手速|手慢无|补不到|不补货)"),
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
    # ``拜拜`` by itself can be a live-room sign-off, but ``拜拜肉`` is a
    # concrete body-shape benefit and must remain available as a commercial
    # beat.  Do not let the former broad phrase erase the latter.
    ("直播闲聊残留", re.compile(r"(?:那|就)?拜拜(?:亚麻)?(?!肉)|脑壳痛(?:了)?|我这个面料我不骗你")),
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
    # These two forms are observed mid-word ASR boundaries. When their
    # adjacent prefix/suffix exists, candidate freezing rejoins the original
    # spoken wording first; reaching this gate means repair was not possible.
    ("合成纤维成分残片", re.compile(r"^酯纤维(?:的)?(?:面料|成分)?[，,。！？!?\s]*$")),
    ("结构术语残片", re.compile(r"(?:三角|梯形)的立[，,。！？!?\s]*$")),
    # Switching people/styles is live presentation coordination, not a
    # meaningful close for the selected product.
    ("直播换人展示", re.compile(r"^(?:换个风格[，,]?换个人|换个人给(?:你们|大家)看看)")),
    # A sentence beginning with a trailing particle plus a connective has lost
    # its subject in the previous ASR unit, for example "呢还遮肚子".
    ("语气承接开头", re.compile(r"^(?:呢|吧)(?:还|也|会|能|可以)")),
    # "会稍微亮一点点" and similar forms require the omitted subject from
    # the previous subtitle. They cannot stand alone in a selected clip.
    (
        "省略主语效果残句",
        re.compile(r"^(?:会|能|可以)(?:稍微|更|比较)(?:亮|白|显|薄|厚|松|紧|舒服|好看)"),
    ),
    # Preserve a full material or season conclusion, but reject the observed
    # ASR tail that stops at "...早秋秋高气爽的季节".
    ("未完成季节残句", re.compile(r"(?:单穿)?早秋秋高气爽的季节[。！？!?，,\\s]*$")),
    # "再加上它立体的" promises a noun that is missing from the candidate.
    ("设计续句残尾", re.compile(r"^(?:再)?加上(?:它|这个|这件|这条).{0,16}的[。！？!?，,\\s]*$")),
    # A styling action without its consequence is a cut-off demonstration,
    # not a self-contained scene claim.
    ("搭配动作残句", re.compile(r"(?:帽|包|鞋|外套).{0,12}(?:一戴|一背|一穿|一搭)[。！？!?，,\\s]*$")),
)
_PRICE_COST_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("数字成本报价", re.compile(r"(?:成本|价格|单价).{0,8}\d{1,4}")),
    # Live rooms frequently omit "元" while changing a price, for example
    # "改价本来是290".  It is still a price announcement when price is blocked.
    ("直播改价报价", re.compile(r"(?:改价|调价)[^。！？!?]{0,16}(?:\d{2,4}|[一二三四五六七八九十百千万]+)")),
    ("每米报价", re.compile(r"\d{2,4}\s*(?:多)?\s*(?:一|1)米")),
    ("价格导向话术", re.compile(r"(?:价位|成本|收费|计价|便宜|越贵|更贵|太贵|很贵|不贵|不便宜|抢着买|秒带|秒拍)")),
    # Local ASR sometimes turns a live price into forms such as "V4五0百" or
    # "388388".  A numeric amount coupled with a price judgement, or a
    # duplicated three-digit amount, is still a price announcement even when
    # the recognizer lost "元" / "一套".
    ("口语价格判断", re.compile(r"(?:[0-9０-９]|[一二三四五六七八九十百千万零〇]).{0,10}(?:贵|便宜|划算)(?:的|了|啊|呀|吧)?[。！？!?，,\s]*$")),
    ("重复数字报价", re.compile(r"(\d{3})\1")),
    ("裸套装报价", re.compile(r"\d{3,4}(?:一|1)?(?:整)?套(?:来|啊|呀|吧|对吧)?[。！？!?，,\s]*$")),
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

# A subtitle row is an ASR transport unit, not necessarily a short-video
# candidate.  These limits only guide splitting at already-spoken boundaries;
# they never authorize a word-count or midpoint cut.
_SHORT_FORM_PREFERRED_MAX_SECONDS = 5.5
_SHORT_FORM_MIN_CLAUSE_SECONDS = 1.55
_SHORT_FORM_MIN_STRONG_SENTENCE_SECONDS = 0.75
_SHORT_FORM_MIN_REMAINDER_SECONDS = 1.15
_SHORT_FORM_STRONG_PUNCTUATION = set("。！？!?")
_SHORT_FORM_CLAUSE_PUNCTUATION = set("，,；;：:")
_SHORT_FORM_SCENE_COMPLETE_RE = re.compile(
    r"^(?:去|穿去|出门|度假|上班|旅行).{1,48}"
    r"(?:可以|合适|好看|适合|没问题|行)[，,。！？!?]*$"
)


def _compact_text(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or ""))


def _short_form_timed_words(segment: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Copy valid source words without changing their text or boundaries."""

    words: list[dict[str, Any]] = []
    for raw in segment.get("words") or ():
        if not isinstance(raw, Mapping):
            continue
        text = str(raw.get("text") or "").strip()
        try:
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError):
            continue
        if not text or end <= start:
            continue
        item = dict(raw)
        item["text"] = text
        item["start"] = start
        item["end"] = end
        words.append(item)
    return words


def _short_form_punctuation_after_words(
    segment: Mapping[str, Any],
    words: Sequence[Mapping[str, Any]],
) -> dict[int, str]:
    """Map only punctuation already present in the source semantic text.

    ``build_semantic_segments`` deliberately stores timed spoken words without
    punctuation.  Reuse its exact alignment helper instead of guessing where a
    comma or full stop belongs.  If alignment is not exact, leave the segment
    untouched rather than inventing a boundary.
    """

    try:
        from volcengine_asr import _semantic_plain_text, _semantic_punctuation_offsets

        expected_plain = "".join(
            _semantic_plain_text(word.get("text") or "") for word in words
        )
        offsets = _semantic_punctuation_offsets(
            str(segment.get("text") or ""), expected_plain
        )
    except Exception:
        return {}
    if not offsets:
        return {}

    punctuation: dict[int, str] = {}
    spoken_offset = 0
    for index, word in enumerate(words):
        spoken_offset += len(_semantic_plain_text(word.get("text") or ""))
        marker = str(offsets.get(spoken_offset) or "")
        if marker:
            punctuation[index] = marker
    return punctuation


def _short_form_boundary_kind(
    words: Sequence[Mapping[str, Any]],
    punctuation: Mapping[int, str],
    index: int,
) -> str:
    marker = str(punctuation.get(index) or "")
    if any(char in _SHORT_FORM_STRONG_PUNCTUATION for char in marker):
        return "strong_punctuation"
    if any(char in _SHORT_FORM_CLAUSE_PUNCTUATION for char in marker):
        return "clause_punctuation"
    if index + 1 < len(words):
        try:
            gap = float(words[index + 1]["start"]) - float(words[index]["end"])
        except (KeyError, TypeError, ValueError):
            gap = 0.0
        if gap >= 0.65:
            return "long_pause"
        if gap >= 0.45:
            return "pause"
    return ""


def _short_form_render(
    words: Sequence[Mapping[str, Any]],
    punctuation: Mapping[int, str],
    start_index: int,
    end_index: int,
) -> str:
    parts: list[str] = []
    for index in range(start_index, end_index + 1):
        parts.append(str(words[index].get("text") or ""))
        marker = str(punctuation.get(index) or "")
        if marker:
            parts.append(marker)
    return "".join(parts).strip()


def short_form_independent_clause(text: Any) -> bool:
    """Recognize a short scene conclusion that can stand before the next topic.

    A live speaker often continues with "它..." after a complete line such as
    "去草原我觉得也很可以".  Joining them only because the ASR wrote a comma
    turns two usable short-video beats into one mixed candidate.  Keep this
    narrow: it applies solely to a concrete occasion ending in an explicit
    conclusion, never to a generic comma-ended product description.
    """

    value = re.sub(r"^\s*\[V\d+\]\s*", "", str(text or ""), flags=re.IGNORECASE)
    value = re.sub(r"\s+", "", value).strip()
    return bool(_SHORT_FORM_SCENE_COMPLETE_RE.match(value))


def refine_short_form_semantic_segments(
    segments: Iterable[Mapping[str, Any]],
    *,
    preferred_max_seconds: float = _SHORT_FORM_PREFERRED_MAX_SECONDS,
) -> tuple[list[dict[str, Any]], dict[str, int | float]]:
    """Split long semantic units only at exact spoken sentence boundaries.

    The ASR transcript and the ``.words.json`` sidecar stay immutable.  This
    creates a finer candidate view for the AI director: every emitted segment
    contains an unchanged contiguous slice of original timed words.  Where an
    ASR row has no reliable punctuation or pause, it is retained as one longer
    unit so a mechanical split cannot create a fresh half sentence.
    """

    try:
        target_max = max(2.0, float(preferred_max_seconds))
    except (TypeError, ValueError):
        target_max = _SHORT_FORM_PREFERRED_MAX_SECONDS

    source_segments = [dict(segment) for segment in (segments or ()) if isinstance(segment, Mapping)]
    refined: list[dict[str, Any]] = []
    split_segments = 0
    long_unsplit = 0

    for segment in source_segments:
        words = _short_form_timed_words(segment)
        if len(words) < 2:
            refined.append(segment)
            continue
        punctuation = _short_form_punctuation_after_words(segment, words)
        duration = float(words[-1]["end"]) - float(words[0]["start"])
        has_internal_strong_boundary = any(
            any(char in _SHORT_FORM_STRONG_PUNCTUATION for char in str(marker or ""))
            for index, marker in punctuation.items()
            if index < len(words) - 1
        )
        # A short row can still contain two spoken sentences.  Split that
        # particular case so candidate freezing cannot join unrelated topics
        # merely because the ASR row happened to be under the duration target.
        if duration <= target_max + 1e-6 and not has_internal_strong_boundary:
            refined.append(segment)
            continue

        boundaries: list[tuple[int, str]] = []
        start_index = 0
        last_index = len(words) - 1
        for index in range(last_index):
            boundary_kind = _short_form_boundary_kind(words, punctuation, index)
            if not boundary_kind:
                continue
            current_duration = float(words[index]["end"]) - float(words[start_index]["start"])
            remaining_duration = float(words[-1]["end"]) - float(words[index + 1]["start"])
            minimum = (
                _SHORT_FORM_MIN_STRONG_SENTENCE_SECONDS
                if boundary_kind == "strong_punctuation"
                else _SHORT_FORM_MIN_CLAUSE_SECONDS
            )
            if current_duration + 1e-6 < minimum:
                continue
            if remaining_duration + 1e-6 < _SHORT_FORM_MIN_REMAINDER_SECONDS:
                continue
            boundaries.append((index, boundary_kind))
            start_index = index + 1

        if not boundaries:
            refined.append(segment)
            long_unsplit += 1
            continue

        split_segments += 1
        start_index = 0
        for end_index, boundary_kind in boundaries:
            item = dict(segment)
            item_words = [dict(word) for word in words[start_index:end_index + 1]]
            item["start"] = round(float(item_words[0]["start"]), 3)
            item["end"] = round(float(item_words[-1]["end"]), 3)
            item["text"] = _short_form_render(words, punctuation, start_index, end_index)
            item["words"] = item_words
            item["semantic_unit"] = True
            item["boundary_reason"] = f"short_form:{boundary_kind}"
            item["short_form_refined"] = True
            refined.append(item)
            start_index = end_index + 1

        item = dict(segment)
        item_words = [dict(word) for word in words[start_index:]]
        item["start"] = round(float(item_words[0]["start"]), 3)
        item["end"] = round(float(item_words[-1]["end"]), 3)
        item["text"] = _short_form_render(words, punctuation, start_index, last_index)
        item["words"] = item_words
        item["semantic_unit"] = True
        item["boundary_reason"] = "short_form:source_end"
        item["short_form_refined"] = True
        refined.append(item)

    metrics: dict[str, int | float] = {
        "input_segments": len(source_segments),
        "output_segments": len(refined),
        "split_segments": split_segments,
        "added_segments": max(0, len(refined) - len(source_segments)),
        "long_unsplit_segments": long_unsplit,
        "preferred_max_seconds": round(target_max, 2),
    }
    return refined, metrics


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
    if any(pattern.search(value) for pattern in _INVENTORY_PRESSURE_RULES):
        blocked, _reason = blocks_role(content_policy, "inventory_pressure", value)
        if blocked:
            flags.append("库存/稀缺催促")
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
