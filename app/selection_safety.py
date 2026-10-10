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
    # ASR occasionally reverses a personal-size reply into wording such as
    # "L码你本来高度...".  The pronoun plus body cue keeps this separate from
    # objective garment facts such as "L码衣长72厘米".
    re.compile(
        _SIZE_TOKEN + r".{0,10}(?:我|你|她|他).{0,8}(?:身高|高度|个子|体重|\d{2,3}斤)",
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
# A body-weight range followed by fit language is still try-on sizing content,
# even when the host omits a pronoun and size code (for example "160斤以内轻松
# 驾驭"). This is distinct from objective garment measurements such as length
# or waist circumference, which have no weight unit.
_WEIGHT_RANGE_FIT_PATTERNS = (
    re.compile(
        r"(?:\d{2,3}|[一二三四五六七八九十百]{2,5})斤"
        r"(?:以内|以下|左右|上下|都能|也能)?"
        r".{0,10}(?:轻松)?(?:驾驭|能穿|可穿|穿得|穿上|合适|没问题)",
        re.I,
    ),
    # "80到160斤以内，随便穿" is still a weight-range try-on claim even
    # when the host omits an explicit size code or "轻松驾驭".
    re.compile(
        r"(?:\d{2,3}|[一二三四五六七八九十百]{2,5})斤"
        r"(?:以内|以下|左右|上下|都能|也能)?"
        r".{0,10}(?:随便|都能|谁都).{0,4}穿",
        re.I,
    ),
    # A paired "不挑身高，不挑体重" is audience sizing guidance rather
    # than an objective garment fact, even if the ASR omitted the range.
    re.compile(
        r"(?:不挑|不限).{0,5}(?:身高|个子).{0,16}"
        r"(?:不挑|不限).{0,5}体重"
        r"|(?:不挑|不限).{0,5}体重.{0,16}"
        r"(?:不挑|不限).{0,5}(?:身高|个子)",
        re.I,
    ),
)
# ASR can drop both the person marker and the unit, for example
# "你子身高170，体重105". A paired height/weight label is still a personal
# try-on reply, while an objective garment measurement never has both labels.
_BARE_PERSONAL_MEASUREMENT_PAIR_PATTERNS = (
    re.compile(
        r"身高\s*(?:1[4-9]\d(?:\s*(?:cm|厘米))?|1\s*[.。．点]\s*[4-9四五六七八九](?:\s*(?:米|m))?|一米[四五六七八九])"
        r".{0,16}体重\s*(?:\d{2,3}(?:\s*(?:斤|kg|公斤))?|[一二三四五六七八九十]{2,4}(?:斤)?)",
        re.I,
    ),
    re.compile(
        r"体重\s*(?:\d{2,3}(?:\s*(?:斤|kg|公斤))?|[一二三四五六七八九十]{2,4}(?:斤)?)"
        r".{0,16}身高\s*(?:1[4-9]\d(?:\s*(?:cm|厘米))?|1\s*[.。．点]\s*[4-9四五六七八九](?:\s*(?:米|m))?|一米[四五六七八九])",
        re.I,
    ),
)
_HOOK_SIZE_SIGNAL_PATTERN = re.compile(
    r"(?:尺码|码数|码型|" + _SIZE_TOKEN + r")",
    re.I,
)
_HOOK_BODY_DATA_PATTERN = re.compile(
    _HEIGHT_VALUE + r"|(?:\d{2,3}|[一二三四五六七八九十]{2,4})\s*斤",
    re.I,
)
# These are not unsafe as body content, but they are a live demonstration
# lead-in rather than a standalone buyer promise.  Keeping the classifier here
# makes every Hook entry point reject the same wording.
_HOOK_PRESENTATION_PREAMBLE_PATTERNS = (
    re.compile(r"^(?:很|非常|特别|太)[A-Za-z]{2,16}(?:的)?$", re.I),
    re.compile(
        r"^我(?:自己|个人)?(?:啊|呀|呢)?我?(?:可能|会|觉得|一般|平时|通常)"
        r".{0,18}(?:这样|这么)?(?:穿|搭).{0,18}"
        r"(?:看一眼|看一下|背面看|侧面看|正面看)"
    ),
    re.compile(
        r"^(?:背面|侧面|正面|上身).{0,12}(?:看一眼|看一下|你看)"
        r".{0,24}(?:不挑人|舒服|好看|显瘦|百搭)"
    ),
    re.compile(
        r"^你(?:这|那)?(?:一套|件|条).{0,14}(?:穿|搭).{0,24}"
        r"(?:很松|很舒服|也可以|可以的|不挑人|好看)"
    ),
    re.compile(
        r"^(?:想(?:搭|看).{0,18}|(?:给你|我给你).{0,10}看一眼|"
        r"(?:这个|这样的).{0,12}就这么搭)"
    ),
)
_LIVE_REPLY_PATTERNS = (
    re.compile(r"(?:谢谢|感谢).{0,8}(?:姐|姐妹|宝宝|家人)"),
    re.compile(r"(?:姐妹|宝宝|姐姐).{0,8}(?:单|现货|收到|看到了)"),
    re.compile(r"(?:^|[,，。！？?])(?:那)?我(?:明天|今天|等会|一会)?(?:穿啥|穿什么|怎么穿|穿哪件)[啊呀呢吗？?]*$"),
    # ASR often drops the viewer question. A delayed try-on promise is still
    # live-room interaction, never an independent clip or a Hook.
    re.compile(
        r"(?:我)?(?:等会(?:儿)?|一会(?:儿)?|待会(?:儿)?)(?:再|先)?"
        r"(?:(?:穿(?:上)?|试).{0,8})?(?:给你|给大家).{0,8}(?:看|瞧|试)"
    ),
    # Audience verdict prompts are useful in a live room but become empty
    # interaction when detached from the surrounding chat.
    re.compile(
        r"(?:你们|大家|姐妹|宝宝|家人).{0,16}(?:自己)?(?:说|讲|觉得|评价)"
        r".{0,24}(?:好不好|行不行|对不对|是不是|有没有|可以不|行吗|好吗)"
    ),
    re.compile(r"(?:姐妹|宝宝|姐姐|家人).{0,16}(?:有|没有|想|要|问|穿|选|拿|看).{0,18}(?:吗|呢|吧|？|\?)"),
    re.compile(r"(?:我们|咱们).{0,12}(?:有|没有|上(?:了)?|做(?:了)?).{0,18}(?:吗|呢|吧|？|\?)"),
    # Stock/top-up calls are live-room operations, not a buyer-facing product
    # claim. Keep this narrow: it requires the actual "add another batch"
    # wording, so normal garment explanations remain available when a user
    # enables price, source, social proof, or after-sale content.
    re.compile(
        r"(?:(?:来|咱们|我们).{0,8})?"
        r"(?:上衣|裤子|裙子|外套|这件|这条|这一套|这个款).{0,8}"
        r"(?:再)?加一波"
    ),
    # Staff coordination is a live-room instruction, not a product claim.
    re.compile(r"(?:你|帮我|谁).{0,8}(?:把|拿|递|给).{0,14}(?:给我|一下|过来|拿来)"),
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
    if any(pattern.search(value) for pattern in _BARE_PERSONAL_MEASUREMENT_PAIR_PATTERNS):
        return "个人身高体重组合"
    if any(pattern.search(value) for pattern in _IMPLICIT_PERSONAL_SIZE_PATTERNS):
        return "个人身高体重尺码组合"
    if any(pattern.search(value) for pattern in _WEIGHT_RANGE_FIT_PATTERNS):
        return "体重范围试穿"
    if any(pattern.search(value) for pattern in _LIVE_REPLY_PATTERNS):
        return "直播互动回复"
    return ""


LIVE_INSTRUCTION_GATE_ENABLED = True
_LIVE_INSTRUCTION_PATTERNS = (
    re.compile(r"帮(?:我|忙)[^，。；！？\n]{0,8}(?:[pP]图?|修图?|改|调|剪|整理|弄|处理)(?:一下|一版|一图|下)?"),
    re.compile(r"(?:帮我|给我|你|谁)[^，。；！？\n]{0,6}(?:把|拿|递|取|带)[^，。；！？\n]{0,6}(?:一下|过来|给我|拿来|上来)"),
    re.compile(r"(?:包括)?(?:你|咱)(?:就)?把(?:它|这个|那个)(?:放|拉|翻|拿)(?:下来|上来|起来)"),
    re.compile(r"主播[^，。；！？\n]{0,10}(?:幺七|一七零|1[4-9]\d|身高|体重|斤|码)"),
    re.compile(r"(?:给|帮)(?:我|你|大家|助理|主播)?[^，。；！？\n]{0,6}(?:加|挂|上)(?:个)?(?:号|链接|购物车|小黄车|库存|一下)"),
)
# ── 口播叫卖 / 互动呼语（P0-2，2026-09-30）─────────────────────────
# 与上面的 live_interaction_or_size_response_reason 刻意分开：
# 上面的函数被 8 个调用点复用（Hook 判定、成片过滤等），扩它的词表会连带
# 影响那些路径；这里只给「导演前候选池过滤」用。
# 只判定「整行是不是话术」，不改写文本、不动时间戳。
_PITCH_TALK_PATTERNS = (
    # —— 叫卖 / 逼单：整句就是话术 ——
    re.compile(r"(?:我是)?真的?很推荐给[^，。；]{0,12}直播间"),
    re.compile(r"我都讲(?:了|过)"),
    re.compile(r"(?:是)?专门给你们定(?:的|做)?"),
    re.compile(r"确定要拍|要拍的?姐妹|想拍的|抓紧拍|赶紧拍|拍下的姐妹|想要的姐妹"),
    re.compile(r"姐妹(?:们)?[，,。！]?$"),
    re.compile(r"(?:扣|打)个?[1一](?!\d)"),
    re.compile(r"公屏|评论区|点赞|收藏"),
    # —— 互动呼语 / 口头确认 ——
    re.compile(r"^(?:看到|看见|瞅)(?:了)?(?:没|没有)"),
    re.compile(r"(?<!但)(?<!却)(?<!倒)是不是|对不对|好不好|行不行|可以吧|有没有|要不要"),
    re.compile(r"^(?:还有|再有)?什么(?:是|要)"),
    # —— 观众确认式呼应（2026-10-05）：让观众打字回应，整句不是商品事实 ——
    re.compile(r"(?:宝宝|姐妹|家人|亲们|朋友们)[^，。；！？\n]{0,6}(?:能看得|看到|听懂|听清)"),
    re.compile(r"(?:能看得|看到|听懂|听清|证明你)[^，。；！？\n]{0,4}的?\s*(?:宝宝|姐妹|家人|亲们|朋友们)"),
    re.compile(r"[打扣](?:个|成|一)?(?:能|好|响|想要|喜欢|收到)\s*[吧啊呀哦哈]*[。！？!?]?$"),
) + (
    # —— 对助理 / 对现场的指令句（2026-10-05 David 定调：属于「明确证据」）——
    # 实测漏网：「这个面料蕾丝的帮我 p 一下好了，」成了成片末章第一句；
    # 「给你再加号」「主播幺七零啊」同属直播现场操作。只认「明确对助理 / 对现场」的
    # 祈使形态；刻意不认「你可以把帽子放下来」这类对观众的建议（那是风格讲解）。
    _LIVE_INSTRUCTION_PATTERNS if LIVE_INSTRUCTION_GATE_ENABLED else ()
)


def pitch_talk_reason(text: Any) -> str:
    """Return why a row is live-room pitch / verification chatter.

    命中即代表「整行就是话术」，不是商品事实。商品事实（如扣子可扣、否定句
    「不是那种很俗的」）刻意不命中。只分类，不重写。
    """

    value = _normalized_text(text)
    if not value:
        return ""
    if re.fullmatch(
        r"(?:是不是|对不对|好不好|行不行|可以吧|有没有|要不要)[啊呀呢吧吗？?，,。]*",
        value,
    ):
        return "互动口头确认"
    for pattern in _PITCH_TALK_PATTERNS:
        if pattern.search(value):
            return "口播叫卖/互动"
    return ""


def is_pitch_talk(text: Any) -> bool:
    return bool(pitch_talk_reason(text))


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
    if re.fullmatch(r"(?:很|非常|特别|太)[A-Za-z]{2,16}(?:的)?", value, re.I):
        return "空泛口头语不可作Hook"
    if any(pattern.search(value) for pattern in _HOOK_PRESENTATION_PREAMBLE_PATTERNS):
        return "展示铺垫不可作Hook"
    return ""
