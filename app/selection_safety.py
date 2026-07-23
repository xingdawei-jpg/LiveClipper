"""Hard safety classifiers shared by every AI clip-selection stage.

The functions in this module only classify an existing transcript.  They never
rewrite candidate text or timestamps, so every caller can use the same result
without breaking the immutable selection contract.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_SOURCE_MARKER_RE = re.compile(r"^\s*\[V\d+\]\s*", re.IGNORECASE)
_SIZE_TOKEN = r"(?:[smlx]{1,4}(?:码)?|xxl(?:码)?|xxxl(?:码)?|均码|大码|小码)"
_PERSON = r"(?:我|你|她|他|这位|那个|女孩子|女生|女孩|姐妹|宝宝|小个子|大个子)"
_HEIGHT_VALUE = (
    r"(?:1\s*[.。．点]\s*[4-9四五六七八九](?:\s*(?:米|m))?"
    r"|一\s*[.。．点]\s*[4-9四五六七八九](?:\s*(?:米|m))?"
    r"|一米[四五六七八九]"
    r"|1[4-9]\d\s*(?:cm|厘米))"
)
_MEASUREMENT_WEIGHT = r"(?:\d{2,3}(?:斤)?|[一二三四五六七八九十]{2,4}(?:斤)?)"

_DIRECT_SIZE_QA_PATTERNS = (
    re.compile(r"(?:报|问|咨询|解答|回复).{0,6}(?:尺码|码数|码型)"),
    re.compile(r"(?:尺码|码数|码型).{0,8}(?:问题|再问|可以问|抓紧问|赶紧问|咨询)"),
    re.compile(r"(?:穿|买|拍|选|拿|要).{0,5}(?:什么|哪个|哪一)?(?:尺码|码数|" + _SIZE_TOKEN + r")", re.I),
)
_PERSONAL_SIZE_REPLY_PATTERNS = (
    re.compile(
        _PERSON
        + r".{0,14}(?:穿|买|拍|选|拿|要|是).{0,5}"
        + _SIZE_TOKEN,
        re.I,
    ),
    re.compile(
        r"(?:\d{2,3}|[一二]百[零一二三四五六七八九十]?)斤.{0,14}"
        r"(?:穿|买|拍|选|拿|要|是|直接).{0,5}"
        + _SIZE_TOKEN,
        re.I,
    ),
    re.compile(
        r"(?:1[4-9]\d|一[四五六七八九][零一二三四五六七八九]?)"
        r"(?:的)?(?:女孩子|女生|女孩|姐妹|宝宝|小个子|人).{0,14}"
        r"(?:穿|买|拍|选|拿|要|是|直接).{0,5}"
        + _SIZE_TOKEN,
        re.I,
    ),
)
_PERSONAL_MEASUREMENT_PATTERNS = (
    re.compile(_PERSON + r".{0,10}" + _HEIGHT_VALUE, re.I),
    re.compile(_PERSON + r".{0,10}(?:身高|体重).{0,12}(?:\d{2,3}斤|" + _HEIGHT_VALUE + r")", re.I),
    re.compile(r"(?:跟我|和我|跟你|和你).{0,8}(?:身高|体重|一米[四五六七八九]|\d{2,3}斤)"),
)
# Live speech frequently compresses a viewer's height, weight and size into a
# bare string such as "1.6米98S码".  There is no explicit subject or verb in
# that form, but it is still a personal size reply, never a product fact.
_IMPLICIT_PERSONAL_SIZE_PATTERNS = (
    re.compile(_HEIGHT_VALUE + r"(?:\s*" + _MEASUREMENT_WEIGHT + r")?\s*" + _SIZE_TOKEN, re.I),
    re.compile(r"(?<!\d)(?:[4-9]\d|1\d{2})\s*(?:斤)?\s*" + _SIZE_TOKEN, re.I),
)
_HOOK_SIZE_SIGNAL_PATTERN = re.compile(
    r"(?:尺码|码数|码型|" + _SIZE_TOKEN + r")",
    re.I,
)
_HOOK_BODY_DATA_PATTERN = re.compile(
    _HEIGHT_VALUE + r"|(?:\d{2,3}|[一二三四五六七八九十]{2,4})\s*斤",
    re.I,
)
_LIVE_REPLY_PATTERNS = (
    re.compile(r"(?:谢谢|感谢).{0,8}(?:姐|姐妹|宝宝|家人)"),
    re.compile(r"(?:姐妹|宝宝|姐姐).{0,8}(?:单|现货|收到|看到了)"),
    re.compile(r"(?:^|[,，。！？?])(?:那)?我(?:明天|今天|等会|一会)?(?:穿啥|穿什么|怎么穿|穿哪件)[啊呀呢吗？?]*$"),
    re.compile(r"(?:姐妹|宝宝|姐姐|家人).{0,16}(?:有|没有|想|要|问|穿|选|拿|看).{0,18}(?:吗|呢|吧|？|\?)"),
    re.compile(r"(?:我们|咱们).{0,12}(?:有|没有|上(?:了)?|做(?:了)?).{0,18}(?:吗|呢|吧|？|\?)"),
)


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _SOURCE_MARKER_RE.sub("", text).strip()
    return re.sub(r"\s+", "", text)


def live_interaction_or_size_response_reason(text: Any) -> str:
    """Return a hard-exclusion reason for live chat or personal sizing replies.

    Product facts such as garment length or a non-conversational size chart are
    deliberately not matched.  The gate targets viewer-directed sizing, personal
    body measurements, and obvious live-room replies only.
    """

    value = _normalized_text(text)
    if not value:
        return "空互动文案"
    if any(pattern.search(value) for pattern in _DIRECT_SIZE_QA_PATTERNS):
        return "直播尺码问答"
    if any(pattern.search(value) for pattern in _PERSONAL_SIZE_REPLY_PATTERNS):
        return "个人尺码答复"
    if any(pattern.search(value) for pattern in _PERSONAL_MEASUREMENT_PATTERNS):
        return "个人身高体重互动"
    if any(pattern.search(value) for pattern in _IMPLICIT_PERSONAL_SIZE_PATTERNS):
        return "个人身高体重尺码组合"
    if any(pattern.search(value) for pattern in _LIVE_REPLY_PATTERNS):
        return "直播互动回复"
    return ""


def is_live_interaction_or_size_response(text: Any) -> bool:
    return bool(live_interaction_or_size_response_reason(text))


def hook_ineligible_reason(text: Any) -> str:
    """Return why a transcript sentence must never become a Hook.

    Personal size replies are removed altogether.  Objective size information
    may remain in the body, but it is not an opening promise and must not be
    promoted to Hook by any fallback path.
    """

    value = _normalized_text(text)
    if not value:
        return "空Hook文案"
    live_reason = live_interaction_or_size_response_reason(value)
    if live_reason:
        return live_reason
    if _HOOK_SIZE_SIGNAL_PATTERN.search(value):
        return "尺码信息不可作Hook"
    if _HOOK_BODY_DATA_PATTERN.search(value):
        return "身高体重信息不可作Hook"
    return ""
