"""Materialize one AI-authored director packet without a second semantic call.

The director chooses the commercial story and subtitle IDs exactly once in the
M1 response.  This module has deliberately narrow authority: it resolves those
IDs against the complete safe executable candidate pool, reports quality
warnings, and builds the existing M2/M3 contract.  It never scores, selects,
reorders, appends, removes, replaces, or semantically repairs a beat.

The one boundary operation it may perform is a **word-level narrowing** of an
already-selected span (``DirectorBeat.start_word_offset`` /
``end_word_offset``): the Director may keep only the middle of a span whose head
and tail are unusable.  That is a boundary move, not a semantic repair — the
program only slices already-timed words, never rewrites them, and it must pass
both the punctuation-lineage check and the project's standalone-playback gate.
Anything that fails falls back to the whole span.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Mapping, Sequence

from commercial_analyzer import (
    DIRECTOR_ANSWER_ROLES,
    DIRECTOR_COVERAGE,
    DIRECTOR_PURCHASE_QUESTIONS,
    DirectorBeat,
    Strategy,
    director_delivery_duration_range,
    director_target_duration_range,
    final_utterance_surface_issue,
)
from story_planner import (
    CommercialStoryBrief,
    NarrativeBeat,
    NarrativePlan,
    OpeningPackage,
    PlanningCandidate,
    ReplanRequest,
    StoryConsumption,
)

# ── 子跨度：把导演已选的候选按词收窄首/末边界 ──────────────────────────────
# 需求（David 2026-10-07）：「如果一个句子的开头结尾不好，只有中间好，
# 那就选中间这部分，和其他片段能够拼接成一段流畅的故事也可以」。
#
# 这是**边界操作**，不是语义修复：程序只按词切片、只搬运既有的时间与文字，
# 从不改写、从不新增内容、从不改变剩余部分的顺序。
#
# 只做两道校验，任一不过就**退回整条候选**（内容更多的安全方向）：
#   1. 标点血缘可复原 —— 切片文本必须能由父候选的词条 + 标点映射精确重建。
#      做不到就不能切：切出来会丢掉原标点，播放闸门也失去判据。
#   2. 切出来的跨度必须能独立播放 —— 用项目自己的播放闸门
#      ``_director_standalone_boundary_reason``。
#
# 派生候选使用独立的 id 段，避开既有候选 id 与 P0.5A 的 950000000+。
SUB_SPAN_CANDIDATE_ID_BASE = 960_000_000


def _rebuild_word_tokens(
    text: str, words: Sequence[Mapping[str, Any]]
) -> list[str] | None:
    """把「词条 + 标点」重建为逐词 token；无法忠实复原时返回 None。

    复用 ``clip_selector._punctuation_after_words``（它只复用能与这批词条精确
    对齐的标点，绝不臆造）。重建结果必须与原文逐字符一致（忽略空白），
    否则视为标点血缘不可靠 —— 此时**不允许**做子跨度。
    """
    from clip_selector import _punctuation_after_words

    clean = [dict(item) for item in words if isinstance(item, Mapping)]
    if not clean:
        return None
    try:
        punctuation = _punctuation_after_words(str(text or ""), clean)
    except Exception:  # noqa: BLE001 - 任何异常都不允许影响既有计划
        return None
    tokens = [
        str(clean[index].get("text") or "") + str(punctuation.get(index, "") or "")
        for index in range(len(clean))
    ]
    if re.sub(r"\s+", "", "".join(tokens)) != re.sub(r"\s+", "", str(text or "")):
        return None
    return tokens


def locate_kept_span_in_rows(
    resolved_rows: Sequence[PlanningCandidate],
    words_by_candidate: Mapping[int, Sequence[Mapping[str, Any]]],
    keep_text: str,
) -> tuple[int, int] | None:
    """把「导演想保留的原文」对齐回已解析候选的词范围。

    返回 ``(首行起始词下标, 末行结束词下标)``（均含端点）；无法**唯一**对齐时返回 None。

    只接受一种形状：**保留全部已选行，只剪掉首行开头的若干词与末行结尾的若干词** ——
    与 ``start_word_offset`` / ``end_word_offset`` 的语义完全一致，因此调用方可以
    直接复用同一条收窄与护栏路径。

    这是纯字符串对齐：不改写、不猜测、不做语义判断。它把 LLM 最擅长的动作
    （逐字复制原文）翻译成可执行的词边界，而不是要求它去数词。
    """
    if not resolved_rows:
        return None
    target = re.sub(r"\s+", "", str(keep_text or ""))
    if not target:
        return None
    row_tokens: list[list[str]] = []
    for row in resolved_rows:
        words = words_by_candidate.get(int(row.candidate_id))
        if not words:
            return None
        tokens = _rebuild_word_tokens(row.text, words)
        if tokens is None:
            return None
        row_tokens.append(tokens)
    if not row_tokens or not row_tokens[0] or not row_tokens[-1]:
        return None
    last_row = len(row_tokens) - 1
    hits: list[tuple[int, int]] = []
    for start_token in range(len(row_tokens[0])):
        for end_token in range(len(row_tokens[last_row])):
            if last_row == 0 and start_token > end_token:
                continue
            pieces: list[str] = []
            usable = True
            for index, tokens in enumerate(row_tokens):
                head = start_token if index == 0 else 0
                tail = end_token if index == last_row else len(tokens) - 1
                if head > tail:
                    usable = False
                    break
                pieces.extend(tokens[head:tail + 1])
            if not usable:
                continue
            if re.sub(r"\s+", "", "".join(pieces)) == target:
                hits.append((int(start_token), int(end_token)))
                if len(hits) > 1:
                    # 多种切法都能对上 -> 不猜，退回整窗由调用方记录告警。
                    return None
    return hits[0] if len(hits) == 1 else None


def narrow_candidate_to_word_span(
    candidate: PlanningCandidate,
    words: Sequence[Mapping[str, Any]],
    *,
    start_word_offset: int | None,
    end_word_offset: int | None,
    derived_id: int,
) -> tuple[PlanningCandidate, tuple[dict[str, Any], ...], str]:
    """把一条候选收窄到指定词跨度。返回 ``(候选, 词切片, 拒绝原因)``。

    拒绝原因非空时，调用方**必须退回整条候选**（``candidate`` 原样返回）：
    退回是内容更多的安全方向，而且整条候选本来就要过同一道闸门。

    ``start_word_offset`` / ``end_word_offset`` 为 ``None`` 表示该侧不动。
    两者都为 ``None``、或切片等于整条时，原对象原样返回（拒绝原因为空）。
    """
    from ai_clipper import _director_standalone_boundary_reason

    clean = [dict(item) for item in words if isinstance(item, Mapping)]
    if not clean:
        return candidate, (), "sub_span_word_lineage_missing"
    tokens = _rebuild_word_tokens(candidate.text, clean)
    if tokens is None:
        return candidate, (), "sub_span_punctuation_lineage_unverified"
    total = len(clean)
    start = 0 if start_word_offset is None else int(start_word_offset)
    end = total - 1 if end_word_offset is None else int(end_word_offset)
    if start < 0 or end < 0 or start > end or end >= total:
        return candidate, (), "sub_span_offset_out_of_range"
    if start == 0 and end == total - 1:
        # 没有实际收窄：保持原对象，避免制造同内容的派生候选。
        return candidate, (), ""
    sliced = clean[start:end + 1]
    text = "".join(tokens[start:end + 1]).strip()
    if not text:
        return candidate, (), "sub_span_text_empty"
    canonical = lambda value: re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or ""))
    if canonical(text) != canonical("".join(str(item.get("text") or "") for item in sliced)):
        return candidate, (), "sub_span_text_words_mismatch"
    # 护栏：切出来的跨度必须能独立播放，否则这一刀不允许成立。
    reason = str(_director_standalone_boundary_reason(text) or "")
    if reason:
        return candidate, (), f"sub_span_not_standalone:{reason}"
    derived = replace(
        candidate,
        candidate_id=int(derived_id),
        source_id=f"{candidate.source_id}+subspan",
        start=round(float(sliced[0].get("start") or candidate.start), 3),
        end=round(float(sliced[-1].get("end") or candidate.end), 3),
        text=text,
    )
    return derived, tuple(sliced), ""


def _candidate_for_subtitle(
    candidates: Sequence[PlanningCandidate],
) -> dict[int, PlanningCandidate]:
    """Keep source lookup deterministic while preserving AI playback order."""
    lookup: dict[int, PlanningCandidate] = {}
    for candidate in candidates:
        for subtitle_id in candidate.origin_subtitle_ids:
            # A duplicate lineage is not semantic ambiguity.  Preserve the
            # first complete safe candidate in stable pool order; normal data
            # has one candidate per source subtitle.
            lookup.setdefault(int(subtitle_id), candidate)
        lookup.setdefault(int(candidate.candidate_id), candidate)
    return lookup


_DEFAULT_OPENING_QUESTION_IDS = ("Q1",)
_DEFAULT_OPENING_ANSWER_ROLES = ("result", "proof")


# ── 开场独立成段（A 方案，2026-10-01 David 定）──────────────────────────
# 动机：Hook 此前只是第一章的「前缀」，章节级后处理（时长预算裁剪 / 章内去冗余 /
# 首尾收口 / 续句审查 / 时长校准）都会顺手动到它 —— 实测 5 次任务里有 2 次
# AI 声明的开场根本没落到片头（1 次 usable、1 次 strong）。
# 做法：把 AI 声明的开场行从所在章里整拍「搬」出来，单独成段（chapter_id=HOOK）
# 并固定排在片头。只搬行：不改写文本、不动时间戳、不重排其余章节、不新增 AI 调用。
# 置 False 即回旧行为（开场继续留在第一章里当前缀）。
OPENING_SEGMENT_ENABLED = True
OPENING_SEGMENT_CHAPTER_ID = "HOOK"
OPENING_SEGMENT_TITLE = "开场钩子"
# 有界：声明的开场最多搬这么多拍，避免异常声明把整片搬空。
OPENING_SEGMENT_MAX_BEATS = 6
# 甲（2026-10-04 David 定）：开场紧邻约束。
# 背景（只读取证，近 3 天 30 个开场包）：开场被设计成「Hook 句 + 1–2 拍 payoff」，
# 而 payoff 允许取自源视频任意位置 —— 30 例里 16 例兑现句与钩子句相隔 >8s（13 例
# >30s，最远 1752s），片头听感就是「两句不相干」（实测 S1：钩子 188.27s 结束、
# 兑现取自 120.27s，倒跳 68s）。
# 做法：开场段成段时按源时间做一次紧邻判定：只保留与「已保留部分」保持接续
# （|间隔| <= OPENING_ADJACENCY_MAX_GAP_SECONDS）的拍；不相邻的拍不搬进开场段，
# 留在它原来的章节里（不删句、不改写文本、不动时间戳、不重排）。
# 置 False 与落地前逐字节等价。
OPENING_ADJACENCY_ENABLED = True
OPENING_ADJACENCY_MAX_GAP_SECONDS = 8.0   # 与开场包 prompt「总长优先约 3–10 秒」对齐
# 修法 1（2026-10-01）：章首缺上文补齐。
# 背景（重启后预览审计实测）：章内按源时间重排会把中段句提成章首，时长裁剪又可能
# 把章首的上一句裁掉 → 章首听起来就是半截话（7 章里 6 章如此）。
# 做法：编排定型后检查每一章的首句，以接续连词起头、且它的上一句没被任何章选中时，
# 把上一句补进章首。只补一行、不改写文本、不动时间戳、不重排；补不到只记 warning。
# 开篇章不参与（由开场声明 + 独立开场段负责）。置 False 与落地前逐字节等价。
CHAPTER_HEAD_LEADIN_FILL_ENABLED = True
CHAPTER_HEAD_LEADIN_MAX_ROWS = 1
# 只在「几乎一定是承接上一句」的连词上触发，避免把能独立站的短句误判。
CHAPTER_HEAD_LEADIN_PREFIXES = (
    "因为", "所以", "然后", "再加上", "加上", "再有", "而且", "但是", "比如说", "还是",
)

# 乙（2026-10-04 David 定）：开场段章首缺上文补齐。
# 背景（只读取证，近 3 天 30 个开场包）：修法 1 明确豁免开篇章（"开篇章不参与"），
# 于是开场段首句以强接续连词起头时没人补上文（30 例里 5 例）——片头第一句就是半截话
# （实测「所以这身衣服特别适合干嘛撑场面你任何的那种，」）。
# 做法：开场段成段 + 回执前移定型后，检查开场段首句：以强接续连词起头、源里紧邻的
# 上一句没被任何章选中时，把上一句补进开场段最前。沿用修法 1 的全部护栏：只补一行、
# 不改写文本、不动时间戳、不重排、依赖链不补、超预算不补、取不到只记 warning。
# 置 False 与落地前逐字节等价。
OPENING_HEAD_LEADIN_FILL_ENABLED = True

# 修法 ①②（2026-10-01 David 定）：时长预算裁剪的两条护栏。
# 背景（重启后 3 个预览实测）：A 段「按角色最弱 + 最长」逐条裁到预算内，只保证每章
# 至少 1 拍 —— 实际把 AI 声明的章削成 0.5s 碎条（智能成片 C4 8 拍→1 拍 0.54s、
# C6 7 拍→1 拍 1.29s），甚至整章削空；还把刚补进来的章首承接行当普通候选裁掉
# （C3 的 267 = analyzer 层补的章首前导句，收据为 beat_removed:duration_budget_exceeded）。
# ① 章级最低配额：任何章不得低于 CHAPTER_BEAT_FLOOR_BEATS 拍、CHAPTER_BEAT_FLOOR_SECONDS 秒；
# ② 补行锁定：带补行标记的拍、以及每章的章首拍 / 章尾拍不裁（程序补行天然落在这两处）。
# 预算实在压不下去时回退旧行为（每章至少 1 拍）并记 warning，绝不把成片顶出区间。
# 置 False 与落地前逐字节等价。
CHAPTER_BEAT_FLOOR_ENABLED = True
CHAPTER_BEAT_FLOOR_BEATS = 2
CHAPTER_BEAT_FLOOR_SECONDS = 4.0  # P0-3②：2.0 → 4.0（1 拍 3.70s 的孤句章也要并）
CHAPTER_BEAT_EDGE_PROTECTED = True
CHAPTER_BEAT_PINNED_MARKERS = (
    "chapter_head_leadin", "opening_head_leadin", "chapter_tail_continuation",
    "program_edge_fill", "opening_segment_declared", "contract_opening_selection",
)
# P0-B（2026-10-01 David 定）：开场成段之后的章级配额复检。
# 背景（重启后预览审计实测）：章级配额只装在「时长预算裁剪」那道门上，而「开场独立
# 成段」跑在裁剪之后 —— 它把 AI 声明的开场行从源章整拍搬走，源章被掏空后没有任何
# 护栏（实测混剪 B 的 C1：声明的 4 拍全被搬进开场，只剩窗口修复补进来的 1 拍 0.96s）。
# 做法：开场成段定型后复检每章的拍数与秒数，低于配额的章整体并入相邻章（优先并入
# 后一章，没有后一章就并入前一章）：只改 chapter_id 分组，不删句、不改写文本、不动
# 时间戳、不重排顺序。开篇章自身不参与判定，最多迭代 3 轮。
# 不做「从开场回搬」：声明为开场的行就是钩子句，拽回源章等于把钩子从片头拿走，
# 风险大于收益；空壳章的处置只能是并入。
# 置 False 与落地前逐字节等价。
CHAPTER_BEAT_FLOOR_AFTER_OPENING_ENABLED = True

# P0-1（2026-10-03 David 定）：AI 声明的开场行必须先锁定，再让时长裁剪跑。
# 背景（重启后预览审计实测）：开场声明在裁剪那一层还只是一组 candidate id，没有任何
# 一拍带着它的身份 —— C1 声明的 #272「怕打理不明白的啊。」被当"最弱角色"裁掉后，
# 开场成段在剩余拍里找不到它，只能照着候选池再造一行、追加到开场段末尾，把 AI 用
# semantic_units 声明的并列句 [[271,272],...] 拆成 271,273,274,275,276,272。
# 做法：把 opening_selection 声明的 candidate id 一并交给裁剪，命中就不裁。
# 宁可不裁（成片偏长），也不能让 AI 声明的开场缺行。置 False 与落地前等价。
CHAPTER_OPENING_DECLARED_PROTECTED = True

# P0-3（2026-10-03 David 定）：章尾半句收口挪到管线最后（见 _settle_chapter_tails）。
# 背景（重启后预览审计实测）：章尾半句门禁跑在时长裁剪之前，裁剪之后才出现的悬空
# 章尾（#279：锚句 #278 被裁掉后它才成章尾）从头到尾没人管。
# 置 False 与落地前逐字节等价。
CHAPTER_TAIL_SETTLE_ENABLED = True
CHAPTER_TAIL_SETTLE_MAX_ROWS = 6
CHAPTER_TAIL_SETTLE_CLOSERS = ("。", "！", "？", "!", "?", "~")
CHAPTER_TAIL_SETTLE_PAUSES = ("，", "、", ",", "；", ";", "：", ":")

# ── 修法 A/B/C（2026-10-05 David 定）：末章读不通的三道确定性护栏 ──────────────
# 背景（混剪 1791194608 预览实测，方案 S3「秋天能穿冷天能叠」）：最后 9.5 秒的
# 「正肩H版不肥可叠穿」6 句里混了 4 个话题 —— 「毛毛马甲、」（顿号结尾的光名词碎片）、
# 「因为它不胖。」（源里上一句 #269 没被任何章选中 → 指代悬空）、「这就是很有调性的
# 女生选择的这种风格，」（风格，与本章「版型/身材」无关）、「这件个人推荐是穿到
# 十几度。」（温度，原属第六章，源时间倒跳 331 秒）；而末章是章级配额把只剩 1 句的
# 第六章整体并进第五章拼出来的（chapter_beat_floor_merged:C6:C5）——"结尾"这个位置
# 整段消失，最后落点变成一句温度推荐。
# A 碎句不许单独成拍：只认两种确定性形态（顿号结尾的光名词碎片 / 短句 + 指代句未入选）
#   → 退回候选池；开篇章、程序补行、AI 声明开场行、一章仅剩一拍时都不判。
# B 尾章不许拆：最后一章低于配额时不再并入前一章，保留独立小章（宁可短），只留痕。
# C 章内一致性：章内每句与本章标题/任务做字面比对，跨话题句只告警、不动手。
# 每道各自可开关，置 False 与落地前逐字节等价。
CHAPTER_FRAGMENT_GATE_ENABLED = True
# 只认 <= 6 字的极短句：8 字会误伤「它是可以往上翻的，」这类承接句（实测 S1/S3 双误伤）。
CHAPTER_FRAGMENT_MAX_CORE_CHARS = 6

# ── 修法 A③（新增）：无谓语残句 + 源紧邻重复 ─────────────────────────────────
# 背景（单品 1791266605 预览实测）：修法 A 只认「顿号结尾」与「指代悬空」两形，漏掉
# 两类同样读不通的拍：
#   ①「挺大的，」—— 3 字、无谓语；源里紧邻的上一句「但是其实衣服晾你看看到了吗？」
#      没被任何章选中 —— 观众不知道「什么」挺大的、跟谁比。
#   ②「它是超大冒型，」—— 源里紧邻的上一句（629）未被选中，而紧邻的下一句
#      「631 你看再打开这么大非常大的一个冒型，」已入选，本拍等于 631 的重复摘要。
# 两形都不依赖标点、也不依赖模型自评，纯字面可判。
# 置 CHAPTER_FRAGMENT_PREDICATE_GATE_ENABLED = False 即与落地前逐字节等价。
CHAPTER_FRAGMENT_PREDICATE_GATE_ENABLED = True
# 只认 <= 4 字且「一个谓语成分都没有」的极短句。
CHAPTER_FRAGMENT_MAX_PREDICATELESS_CHARS = 4
# 出现任意一个即视为「有谓语 / 可独立成句」，一律不判。
# 只收**真正的谓语成分**（判断词 / 能愿动词 / 介词 / 实义动词）与句末语气词。
# 刻意不收「挺、很、太、更、最、好、大、小、宽」这类程度词与形容词 —— 它们描述属性
# 但不构成谓语，「挺大的」正因如此才读不通（实测踩过这个坑）。
CHAPTER_FRAGMENT_PREDICATE_MARKERS = (
    "是", "有", "能", "会", "要", "得", "把", "给", "让", "叫", "像", "比",
    "可以", "应该", "就是", "也是", "还是", "有点",
    "做", "穿", "搭", "看", "试", "摸", "洗", "买", "拍", "显", "藏", "遮",
    "了", "吧", "呢", "啊", "吗", "嘛",
)
# 本拍的字面 gram 有多大比例已被源紧邻句包含。0.15 —— 实测「它是超大冒型」被下句
# 包含 0.20，而正常承接对（「显得脸小」相对上句）仅 0.0。
CHAPTER_FRAGMENT_NEIGHBOR_CONTAINMENT = 0.15
# 只在「几乎一定是承接上一句」的短句上触发，长句一律不判。
CHAPTER_FRAGMENT_DANGLING_HEADS = (
    "因为它", "因为", "所以它", "所以", "但是", "但", "它", "这个", "这种", "这样",
    "那么", "那是", "这就是",
)
CHAPTER_TAIL_CHAPTER_KEEP_ENABLED = True
CHAPTER_TOPIC_CONSISTENCY_ENABLED = True
CHAPTER_TOPIC_OVERLAP_MIN = 0.12

# ── 修法 C′（新增）：章内顺序默认保留 AI 编排 ─────────────────────────────────
# 背景（同一次实测）：C 段把「源时间回跳」一律当缺陷，无条件按源起点重排。但实测 6 章
# 的回跳全是叙事编排：颜色章 394「这个颜色其实很特别」被留作领起、395/396 拿灰的做
# 对比、397「非常的低调」收口；帽型章把 863/864（叠穿调性）放在 860-862 之前。
# 按源时间重排会把这些收口句与语序拆散，读起来更差。
# 故默认**一律保留 AI 的章内顺序**，只把搅乱比例与时间轴断点记进告警供复核。
# 关闭护栏（CHAPTER_TIME_ORDER_GUARD_ENABLED = False）即回到落地前的强制重排。
CHAPTER_TIME_ORDER_GUARD_ENABLED = True
# 相邻源字幕 ID 差 <= 该值即视为「源里连号的同一段」。
CHAPTER_TIME_ORDER_RUN_MAX_STEP = 2

CHAPTER_TOPIC_STOP_CHARS = set("的了是我你他她它这那就都也都很非常还而和与个把被给在们啊吧呀呢哦嘛儿")



def _normalized_values(raw: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = (raw,)
    values = tuple(str(value or "").strip() for value in (raw or ()) if str(value or "").strip())
    return values or fallback


def _opening_scope(
    director: Mapping[str, Any], sequence: Sequence[DirectorBeat],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read the Director-declared opening scope without inventing a mode."""
    raw_scope = director.get("opening_scope")
    scope = dict(raw_scope) if isinstance(raw_scope, Mapping) else {}
    # A model occasionally omits the display-only ``opening_scope`` object
    # while still declaring the first beat's question and answer role.  That
    # declaration is already its own semantic decision, so expose it as the
    # narrow scope rather than forcing a scene story back to global Q1.  This
    # never substitutes or ranks a source sentence.
    first = sequence[0] if sequence else None
    fallback_questions = (
        (str(first.purchase_question_id or "").strip().upper(),)
        if first and str(first.purchase_question_id or "").strip() else _DEFAULT_OPENING_QUESTION_IDS
    )
    fallback_roles = (
        (str(first.answer_role or first.role or "").strip().lower(),)
        if first and str(first.answer_role or first.role or "").strip() else _DEFAULT_OPENING_ANSWER_ROLES
    )
    question_ids = tuple(value.upper() for value in _normalized_values(
        scope.get("allowed_purchase_question_ids"), fallback_questions,
    ))
    answer_roles = tuple(value.lower() for value in _normalized_values(
        scope.get("allowed_answer_roles"), fallback_roles,
    ))
    return question_ids, answer_roles


def _packet_relation_issues(
    packet: DirectorBeat,
    *,
    index: int,
    prior_question_ids: set[str],
    seen_claims: set[tuple[str, str, str]],
    opening_question_ids: tuple[str, ...],
    opening_answer_roles: tuple[str, ...],
) -> list[str]:
    """Validate only the Director-declared relationship, never its meaning.

    The AI still chooses the sentence and writes the buyer-value labels in the
    same response.  This verifier merely makes a malformed packet visible
    instead of silently turning a list of claims into a completed film.
    """
    issues: list[str] = []
    label = packet.beat_id or str(index)
    question_id = str(packet.purchase_question_id or "").strip().upper()
    answer_role = str(packet.answer_role or "").strip().lower()
    supports_question_id = str(packet.supports_question_id or "").strip().upper()
    outcome = str(packet.purchase_outcome or "").strip()
    coverage = str(packet.coverage or "").strip().lower()
    if question_id not in DIRECTOR_PURCHASE_QUESTIONS:
        issues.append(f"single_pass_director_purchase_question_invalid:{label}")
    if not str(packet.purchase_question or "").strip():
        issues.append(f"single_pass_director_purchase_question_missing:{label}")
    if answer_role not in DIRECTOR_ANSWER_ROLES:
        issues.append(f"single_pass_director_answer_role_invalid:{label}")
    if coverage not in DIRECTOR_COVERAGE:
        issues.append(f"single_pass_director_coverage_invalid:{label}")
    if not outcome:
        issues.append(f"single_pass_director_purchase_outcome_missing:{label}")
    if supports_question_id:
        if supports_question_id not in DIRECTOR_PURCHASE_QUESTIONS:
            issues.append(f"single_pass_director_supports_question_invalid:{label}")
        elif supports_question_id == question_id:
            # A chapter may have a result followed by a different-role proof
            # for that *same already-established* purchase question.  This is
            # the formal version of “same claim, additional proof”, not a
            # semantic expansion by code.  Only the AI supplied both the
            # question relation and the exact source beat; we merely avoid
            # treating an internal chapter proof as an impossible reference.
            if question_id not in prior_question_ids:
                issues.append(f"single_pass_director_supports_question_not_established:{label}:{question_id}")
        elif supports_question_id not in prior_question_ids:
            issues.append(f"single_pass_director_supports_question_not_established:{label}:{supports_question_id}")
    if index == 1 and (question_id not in opening_question_ids or answer_role not in opening_answer_roles):
        issues.append(f"single_pass_director_opening_outside_scope:{label}")
    triple = (question_id, answer_role, outcome)
    if question_id and answer_role and outcome and triple in seen_claims:
        issues.append(f"single_pass_director_repeated_purchase_value:{label}")
    return issues


def _opening_surface_issue(text: str) -> str:
    """Report obvious live delivery lead-ins without changing the opening.

    This does not choose an alternative, re-rank, or reject anything.  It only
    makes an AI-selected source sentence such as ``的确…你看…`` visible to the
    editor as an opening-quality warning.
    """
    normalized = str(text or "").strip()
    for prefix in (
        "好的", "那个", "其实", "然后", "因为", "所以", "的确", "就的确", "嗯", "你看",
    ):
        if normalized.startswith(prefix):
            return "live_or_dependent_opening_leadin"
    # Keep a useful demonstrated comparison available as a body beat, but it
    # cannot carry the first spoken promise without the picture already doing
    # the explanatory work.  This is a role restriction, never a deletion or
    # a program-selected substitute.
    if re.search(r"(?:从|在)(?:这儿|这里).{0,20}(?:到|变成)(?:这儿|这里)", normalized):
        return "visual_dependent_opening"
    if re.match(r"^(?:后背|侧面|正面|上身).{0,18}(?:看|都|显)", normalized):
        return "visual_dependent_opening"
    return ""


def _verbatim_key(value: Any) -> str:
    """Compare copied source speech without treating layout spaces as edits."""
    return re.sub(r"\s+", "", str(value or "").strip())


_CHAPTER_BEAT_ROLE_PRIORITY = {
    "hook": 0, "result": 1, "mechanism": 2, "styling": 3,
    "fit": 4, "proof": 5, "risk_remove": 6, "close": 7,
}


def _group_beats_by_chapter(beats: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    grouped: list[tuple[str, list[Any]]] = []
    for beat in beats:
        chapter_id = str(getattr(beat, "chapter_id", "") or "")
        if grouped and grouped[-1][0] == chapter_id:
            grouped[-1][1].append(beat)
        else:
            grouped.append((chapter_id, [beat]))
    return grouped


def _beat_seconds(beat: Any, candidates_by_id: Mapping[int, Any]) -> float:
    return round(sum(
        float(getattr(candidates_by_id[cid], "duration", 0.0) or 0.0)
        for cid in getattr(beat, "candidate_evidence", ()) if cid in candidates_by_id
    ), 3)


def _beat_start(beat: Any, candidates_by_id: Mapping[int, Any]):
    starts = [
        float(candidates_by_id[cid].start)
        for cid in getattr(beat, "candidate_evidence", ()) if cid in candidates_by_id
    ]
    return min(starts) if starts else None


def _beat_text(beat: Any, candidates_by_id: Mapping[int, Any]) -> str:
    return " ".join(
        str(getattr(candidates_by_id[cid], "text", "") or "")
        for cid in getattr(beat, "candidate_evidence", ()) if cid in candidates_by_id
    ).strip()


def _opening_receipt_candidate_ids(
    strategy: Strategy,
    candidate_by_subtitle: Mapping[int, PlanningCandidate],
) -> tuple[int, ...]:
    """Read the AI-declared opening package IDs without inventing an order.

    ``opening_selection`` is the only place where the director states which
    source sentence is its opening.  Resolution stays mechanical: an ID that
    cannot be bound to an executable candidate is skipped, never substituted.
    """
    selection = dict(getattr(strategy, "opening_selection", None) or {})
    ids: list[int] = []
    for value in selection.get("selected_subtitle_ids") or ():
        try:
            subtitle_id = int(value)
        except (TypeError, ValueError):
            continue
        candidate = candidate_by_subtitle.get(subtitle_id)
        if candidate is None:
            continue
        if candidate.candidate_id not in ids:
            ids.append(candidate.candidate_id)
    return tuple(ids)


def _enforce_opening_receipt_prefix(
    beats: Sequence[Any],
    receipt_ids: Sequence[int],
) -> tuple[list[Any], int]:
    """Put the AI-declared opening back at the head of its own first chapter.

    Only the leading chapter group is touched, and only by reordering beats
    that already belong to it: nothing is added, deleted, or moved across a
    chapter boundary.  A receipt that cannot be located in full inside that
    group is left untouched, so a partial match can never silently rewrite
    the film.
    """
    if not beats or not receipt_ids:
        return list(beats), 0
    head_chapter = str(getattr(beats[0], "chapter_id", "") or "")
    lead: list[Any] = []
    tail: list[Any] = []
    for beat in beats:
        if str(getattr(beat, "chapter_id", "") or "") == head_chapter:
            lead.append(beat)
        else:
            tail.append(beat)
    index_of_candidate: dict[int, int] = {}
    for index, beat in enumerate(lead):
        for candidate_id in getattr(beat, "candidate_evidence", ()):
            index_of_candidate.setdefault(int(candidate_id), index)
    # 同一个 candidate 可能由多个字幕 ID 合并而来：去重后前缀比对才是稳定的。
    wanted = list(dict.fromkeys(int(candidate_id) for candidate_id in receipt_ids))
    if any(candidate_id not in index_of_candidate for candidate_id in wanted):
        return list(beats), 0
    current_prefix = [
        int(candidate_id)
        for beat in lead[: len(wanted)]
        for candidate_id in getattr(beat, "candidate_evidence", ())
    ]
    if current_prefix == wanted:
        return list(beats), 0
    front: list[int] = []
    for candidate_id in wanted:
        index = index_of_candidate[candidate_id]
        if index not in front:
            front.append(index)
    picked = [lead[index] for index in front]
    picked_set = set(front)
    rest = [beat for index, beat in enumerate(lead) if index not in picked_set]
    return picked + rest + tail, len(picked)


def _materialize_opening_segment(
    beats: Sequence[Any],
    selected: Sequence[Any],
    receipt_candidate_ids: Sequence[int],
    candidates_by_id: Mapping[int, Any],
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """把 AI 声明的开场搬成固定排在最前的独立开场段。

    只做搬运：把已选中的整拍移进开场段（不改写原话、不动时间戳、不重排其余
    章节的相对顺序）。声明了却从未被选中的行，只有在候选池里能取到时才补进来，
    取不到就不硬凑。已经是独立开场段时原样返回（幂等）。
    """
    ordered_beats = list(beats)
    ordered_selected = list(selected)
    if not ordered_beats:
        return ordered_beats, ordered_selected, {}
    if any(
        str(getattr(beat, "chapter_id", "") or "") == OPENING_SEGMENT_CHAPTER_ID
        for beat in ordered_beats
    ):
        return ordered_beats, ordered_selected, {}

    wanted = list(dict.fromkeys(int(item) for item in receipt_candidate_ids or ()))
    source = "contract_opening_selection"
    if not wanted:
        # 没有声明回执时退化为「首章的 hook 拍自成一段」，仍然固定住开头。
        head_chapter = str(getattr(ordered_beats[0], "chapter_id", "") or "")
        wanted = [
            int(candidate_id)
            for beat in ordered_beats
            if str(getattr(beat, "chapter_id", "") or "") == head_chapter
            and str(getattr(beat, "narrative_role", "") or "").strip().lower() == "hook"
            for candidate_id in getattr(beat, "candidate_evidence", ())
        ]
        wanted = list(dict.fromkeys(wanted))
        source = "head_chapter_hook_beat"
    if not wanted:
        return ordered_beats, ordered_selected, {}
    wanted = wanted[:OPENING_SEGMENT_MAX_BEATS]
    wanted_set = set(wanted)

    index_by_candidate: dict[int, list[int]] = {}
    for index, beat in enumerate(ordered_beats):
        for candidate_id in getattr(beat, "candidate_evidence", ()) or ():
            index_by_candidate.setdefault(int(candidate_id), []).append(index)

    # 先搬「整拍都属于开场」的拍，再搬「包含开场」的拍；跨章不限章节。
    moved_indexes: list[int] = []
    taken_beats: set[int] = set()
    for subset_only in (True, False):
        for candidate_id in wanted:
            for index in index_by_candidate.get(candidate_id, ()):
                if index in taken_beats:
                    continue
                evidence = {int(item) for item in getattr(ordered_beats[index], "candidate_evidence", ())}
                if subset_only and not evidence <= wanted_set:
                    continue
                taken_beats.add(index)
                moved_indexes.append(index)
                break

    synthesized: list[Any] = []
    if len(moved_indexes) < len(wanted):
        template = ordered_beats[0]
        covered = {
            int(item)
            for index in moved_indexes
            for item in getattr(ordered_beats[index], "candidate_evidence", ())
        }
        for candidate_id in wanted:
            candidate = candidates_by_id.get(candidate_id)
            if candidate is None or candidate_id in covered:
                continue
            synthesized.append(replace(
                template,
                chapter_id=OPENING_SEGMENT_CHAPTER_ID,
                source_role="hook",
                narrative_role="hook",
                goal=OPENING_SEGMENT_TITLE,
                candidate_evidence=(candidate_id,),
                target_seconds=round(float(getattr(candidate, "duration", 0.0) or 0.0), 3),
                selection_instruction="opening_segment_declared",
                selection_origin="opening_segment_declared",
                transition_from_previous="",
            ))
            covered.add(candidate_id)

    if not moved_indexes and not synthesized:
        return ordered_beats, ordered_selected, {}

    def _as_opening(beat: Any) -> Any:
        return replace(
            beat,
            chapter_id=OPENING_SEGMENT_CHAPTER_ID,
            source_role="hook",
            narrative_role="hook",
            goal=OPENING_SEGMENT_TITLE,
            selection_instruction="opening_segment_declared",
            selection_origin="opening_segment_declared",
            transition_from_previous="",
        )

    # 甲：紧邻约束 —— 不相邻的拍不搬进开场段，留在原章（见常量区说明）。
    _ordered_openings: list[tuple[Any, int | None]] = [
        (ordered_beats[index], index) for index in moved_indexes
    ] + [(beat, None) for beat in synthesized]
    adjacency_dropped_ids: list[int] = []
    if OPENING_ADJACENCY_ENABLED and len(_ordered_openings) > 1:
        _keep_positions, _drop_positions = _opening_adjacency_keep(
            [item[0] for item in _ordered_openings],
            candidates_by_id,
            max_gap_seconds=OPENING_ADJACENCY_MAX_GAP_SECONDS,
        )
        _drop_set = set(_drop_positions)
        for _position in _drop_positions:
            _dropped_beat, _dropped_index = _ordered_openings[_position]
            if _dropped_index is not None:
                # 不搬这拍：它留在原来的章节里（不删句、不改正文、不动时间戳）。
                taken_beats.discard(_dropped_index)
            adjacency_dropped_ids.extend(
                int(item) for item in (getattr(_dropped_beat, "candidate_evidence", ()) or ())
            )
        _ordered_openings = [
            _ordered_openings[_position]
            for _position in range(len(_ordered_openings)) if _position not in _drop_set
        ]
    opening_beats = [_as_opening(_item[0]) for _item in _ordered_openings]
    rest_beats = [
        beat for index, beat in enumerate(ordered_beats) if index not in taken_beats
    ]
    new_beats = opening_beats + rest_beats

    order: dict[int, int] = {}
    for beat in new_beats:
        for candidate_id in getattr(beat, "candidate_evidence", ()) or ():
            order.setdefault(int(candidate_id), len(order))
    known_selected = {int(getattr(item, "candidate_id", 0) or 0) for item in ordered_selected}
    for candidate_id in order:
        # 从候选池补回来的开场行也要进 selected，否则片单与计时口径不一致。
        if candidate_id not in known_selected and candidates_by_id.get(candidate_id) is not None:
            ordered_selected.append(candidates_by_id[candidate_id])
    ordered_selected.sort(key=lambda item: order.get(
        int(getattr(item, "candidate_id", 0) or 0), len(order) + 1))
    opening_ids = [
        int(candidate_id)
        for beat in opening_beats
        for candidate_id in getattr(beat, "candidate_evidence", ()) or ()
    ]
    receipt = {
        "chapter_id": OPENING_SEGMENT_CHAPTER_ID,
        "title": OPENING_SEGMENT_TITLE,
        "source": source,
        "declared_candidate_ids": wanted,
        "adjacency_enabled": bool(OPENING_ADJACENCY_ENABLED),
        "adjacency_max_gap_seconds": (
            OPENING_ADJACENCY_MAX_GAP_SECONDS if OPENING_ADJACENCY_ENABLED else None
        ),
        "adjacency_dropped_candidate_ids": adjacency_dropped_ids,
        "opening_candidate_ids": opening_ids,
        "seconds": round(sum(
            float(getattr(candidates_by_id.get(candidate_id), "duration", 0.0) or 0.0)
            for candidate_id in dict.fromkeys(opening_ids)
            if candidates_by_id.get(candidate_id) is not None
        ), 3),
    }
    return new_beats, ordered_selected, receipt


def _beat_end(beat: Any, candidates_by_id: Mapping[int, Any]):
    ends = [
        float(candidates_by_id[cid].end)
        for cid in getattr(beat, "candidate_evidence", ()) if cid in candidates_by_id
    ]
    return max(ends) if ends else None


def _interval_gap(
    left_start: float | None, left_end: float | None,
    right_start: float | None, right_end: float | None,
) -> float | None:
    """两段源区间的间隔（重叠算 0）；任一段取不到时返回 None。"""
    if left_start is None or left_end is None or right_start is None or right_end is None:
        return None
    if right_start >= left_end:
        return round(right_start - left_end, 3)
    if left_start >= right_end:
        return round(left_start - right_end, 3)
    return 0.0


def _opening_adjacency_keep(
    beats: Sequence[Any],
    candidates_by_id: Mapping[int, Any],
    *,
    max_gap_seconds: float,
) -> tuple[list[int], list[int]]:
    """按源时间判候选开场拍「挨不挨着」，返回 (保留下标, 丢弃下标)。

    确定性、零 AI 调用、不改写不重排：首拍永远保留（它是声明的钩子）；此后每一拍只有
    在与「已保留部分的源区间」接续（|间隔| <= max_gap_seconds）时才保留 —— 间距取不到
    的拍按「不接续」处理，绝不为了凑开场把它留在片头。被丢弃的拍只是不搬进开场段，
    它仍留在原来的章节里。
    """
    keep: list[int] = []
    dropped: list[int] = []
    kept_start: float | None = None
    kept_end: float | None = None
    for index, beat in enumerate(beats):
        start = _beat_start(beat, candidates_by_id)
        end = _beat_end(beat, candidates_by_id)
        if not keep:
            keep.append(index)
            kept_start, kept_end = start, end
            continue
        gap = _interval_gap(kept_start, kept_end, start, end)
        if gap is None or gap > float(max_gap_seconds):
            dropped.append(index)
            continue
        keep.append(index)
        if start is not None and (kept_start is None or start < kept_start):
            kept_start = start
        if end is not None and (kept_end is None or end > kept_end):
            kept_end = end
    return keep, dropped


def _text_gram_overlap(left: str, right: str) -> float:
    def _grams(value: str) -> set:
        clean = "".join(ch for ch in str(value or "") if ch.isalnum())
        return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))}

    a, b = _grams(left), _grams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _chapter_head_needs_leadin(text: str) -> bool:
    """章首这句是不是「接着上一句说的」——以强接续连词起头。

    只做字面判定，不看语义；词表刻意收窄到几乎必然是承接的连词，避免误伤能独立
    成句的首句（「其实 / 就是 / 你看」这类不收）。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return False
    return any(normalized.startswith(prefix) for prefix in CHAPTER_HEAD_LEADIN_PREFIXES)


def _fill_chapter_head_leadins(
    beats: Sequence[Any],
    selected: Sequence[Any],
    candidate_by_subtitle: Mapping[int, Any],
    *,
    source_max_seconds: float | None = None,
) -> tuple[list[Any], list[Any], list[str]]:
    """章首缺上文补齐（修法 1；确定性、零 AI 调用；只补行，不改写、不重排）。

    只处理「非开篇章」：首句以接续连词起头，而它的上一句没被任何章选中时，把上一句
    补进章首（沿用锚点拍的角色字段，只换 candidate_evidence）。上一句本身也是连词
    引子（依赖链）、已被别的章用掉、取不到、或补进去会把片长顶出契约上限时 ——
    一律不补，只留 warning（绝不为了填空把断得更远的句子接上去）。
    """
    warnings: list[str] = []
    ordered_beats = list(beats)
    ordered_selected = list(selected)
    if not ordered_beats or not CHAPTER_HEAD_LEADIN_FILL_ENABLED:
        return ordered_beats, ordered_selected, warnings

    by_candidate_id = {int(item.candidate_id): item for item in candidate_by_subtitle.values()}
    used: set[int] = set()
    for beat in ordered_beats:
        for candidate_id in getattr(beat, "candidate_evidence", ()) or ():
            used.add(int(candidate_id))
    total_seconds = sum(float(getattr(item, "duration", 0.0) or 0.0) for item in ordered_selected)
    limit = float(source_max_seconds) if source_max_seconds else None

    def _previous_of(candidate: Any) -> Any:
        origins = sorted(int(value) for value in (getattr(candidate, "origin_subtitle_ids", ()) or ()))
        if not origins:
            return None
        return candidate_by_subtitle.get(origins[0] - 1)

    output: list[Any] = []
    for group_index, (chapter_id, group) in enumerate(_group_beats_by_chapter(ordered_beats)):
        if group_index == 0 or not group:
            output.extend(group)
            continue
        head_beat = group[0]
        head_evidence = [int(value) for value in (getattr(head_beat, "candidate_evidence", ()) or ())]
        head_candidate = by_candidate_id.get(head_evidence[0]) if head_evidence else None
        if head_candidate is None or not _chapter_head_needs_leadin(
                str(getattr(head_candidate, "text", "") or "")):
            output.extend(group)
            continue
        previous = _previous_of(head_candidate)
        if previous is None:
            warnings.append(f"single_pass_director_chapter_head_leadin_no_previous:{chapter_id}")
            output.extend(group)
            continue
        previous_id = int(previous.candidate_id)
        if previous_id in used:
            warnings.append(f"single_pass_director_chapter_head_leadin_taken:{chapter_id}:{previous_id}")
            output.extend(group)
            continue
        if _chapter_head_needs_leadin(str(getattr(previous, "text", "") or "")):
            warnings.append(f"single_pass_director_chapter_head_leadin_chain:{chapter_id}")
            output.extend(group)
            continue
        added_seconds = float(getattr(previous, "duration", 0.0) or 0.0)
        if limit is not None and total_seconds + added_seconds > limit:
            warnings.append(f"single_pass_director_chapter_head_leadin_over_budget:{chapter_id}")
            output.extend(group)
            continue
        output.append(replace(
            head_beat,
            candidate_evidence=(previous_id,),
            target_seconds=round(added_seconds, 3),
            selection_instruction="chapter_head_leadin_fill",
            selection_origin="chapter_head_leadin_fill",
            transition_from_previous="",
        ))
        output.extend(group)
        used.add(previous_id)
        total_seconds += added_seconds
        if previous_id not in {int(getattr(item, "candidate_id", 0)) for item in ordered_selected}:
            insert_at = next(
                (index for index, item in enumerate(ordered_selected)
                 if int(getattr(item, "candidate_id", 0)) == head_evidence[0]),
                len(ordered_selected),
            )
            ordered_selected.insert(insert_at, previous)
        warnings.append(
            f"single_pass_director_chapter_head_leadin_filled:{chapter_id}:{previous_id}"
        )
    return output, ordered_selected, warnings


def _fill_opening_head_leadins(
    beats: Sequence[Any],
    selected: Sequence[Any],
    candidate_by_subtitle: Mapping[int, Any],
    *,
    opening_chapter_ids: Sequence[str] = (OPENING_SEGMENT_CHAPTER_ID, "OPENING"),
    source_max_seconds: float | None = None,
) -> tuple[list[Any], list[Any], list[str]]:
    """开场段章首缺上文补齐（乙；确定性、零 AI 调用；只补行，不改写、不重排）。

    与修法 1 同源，只是把「开篇章不参与」这条豁免收回：开场段首句以强接续连词起头、
    而源里紧邻的上一句没被任何章选中时，把上一句补进开场段最前。护栏与修法 1 完全一致：
    上一句本身也是连词引子（依赖链）、已被别的章用掉、取不到、或补进去会把片长顶出
    契约上限 —— 一律不补，只留 warning。
    """
    warnings: list[str] = []
    ordered_beats = list(beats)
    ordered_selected = list(selected)
    if not ordered_beats or not OPENING_HEAD_LEADIN_FILL_ENABLED:
        return ordered_beats, ordered_selected, warnings
    skip_ids = {str(item or "") for item in (opening_chapter_ids or ()) if str(item or "")}
    head_index = next(
        (
            index for index, beat in enumerate(ordered_beats)
            if str(getattr(beat, "chapter_id", "") or "") in skip_ids
        ),
        None,
    )
    if head_index is None:
        return ordered_beats, ordered_selected, warnings
    head_beat = ordered_beats[head_index]
    head_evidence = [int(value) for value in (getattr(head_beat, "candidate_evidence", ()) or ())]
    by_candidate_id = {int(item.candidate_id): item for item in candidate_by_subtitle.values()}
    head_candidate = by_candidate_id.get(head_evidence[0]) if head_evidence else None
    if head_candidate is None or not _chapter_head_needs_leadin(
            str(getattr(head_candidate, "text", "") or "")):
        return ordered_beats, ordered_selected, warnings
    used = {
        int(candidate_id)
        for beat in ordered_beats
        for candidate_id in (getattr(beat, "candidate_evidence", ()) or ())
    } | {
        int(getattr(item, "candidate_id", 0) or 0) for item in ordered_selected
    }
    origins = sorted(int(value) for value in (getattr(head_candidate, "origin_subtitle_ids", ()) or ()))
    previous = candidate_by_subtitle.get(origins[0] - 1) if origins else None
    if previous is None:
        warnings.append("single_pass_director_opening_head_leadin_no_previous")
        return ordered_beats, ordered_selected, warnings
    if not bool(getattr(previous, "hook_eligible", True)):
        # 这片首行会顶成片头第一拍；story_planner 对「开场钩子候选」有硬校验
        # （hook_candidate_ineligible → plan_valid=False），所以不 hook_eligible 的行
        # 一律不补，只留 warning。
        warnings.append(
            "single_pass_director_opening_head_leadin_not_hook_eligible:%d"
            % int(previous.candidate_id)
        )
        return ordered_beats, ordered_selected, warnings
    previous_id = int(previous.candidate_id)
    if previous_id in used:
        warnings.append("single_pass_director_opening_head_leadin_taken:%d" % previous_id)
        return ordered_beats, ordered_selected, warnings
    if _chapter_head_needs_leadin(str(getattr(previous, "text", "") or "")):
        warnings.append("single_pass_director_opening_head_leadin_chain:%d" % previous_id)
        return ordered_beats, ordered_selected, warnings
    added_seconds = float(getattr(previous, "duration", 0.0) or 0.0)
    limit = float(source_max_seconds) if source_max_seconds else None
    total_seconds = sum(float(getattr(item, "duration", 0.0) or 0.0) for item in ordered_selected)
    if limit is not None and total_seconds + added_seconds > limit:
        warnings.append("single_pass_director_opening_head_leadin_over_budget:%d" % previous_id)
        return ordered_beats, ordered_selected, warnings
    ordered_beats = ordered_beats[:head_index] + [
        replace(
            head_beat,
            candidate_evidence=(previous_id,),
            target_seconds=round(added_seconds, 3),
            selection_instruction="opening_head_leadin_fill",
            selection_origin="opening_head_leadin_fill",
            transition_from_previous="",
        )
    ] + ordered_beats[head_index:]
    if previous_id not in {int(getattr(item, "candidate_id", 0)) for item in ordered_selected}:
        insert_at = next(
            (
                index for index, item in enumerate(ordered_selected)
                if int(getattr(item, "candidate_id", 0)) == head_evidence[0]
            ),
            len(ordered_selected),
        )
        ordered_selected.insert(insert_at, previous)
    warnings.append("single_pass_director_opening_head_leadin_filled:%d" % previous_id)
    return ordered_beats, ordered_selected, warnings


def _beat_is_pinned(beat: Any) -> bool:
    """程序补进来的行（章首前导句 / 章尾续句 / 开场声明）不属于可裁对象。

    补行是为了补上「半截话」的上下文；它按角色优先级往往最弱、按长度往往最短，
    在时长预算裁剪里是最容易被删的一类 —— 实测 C3 的 267 就是这样丢的。
    """
    marker = "%s|%s" % (
        getattr(beat, "selection_instruction", "") or "",
        getattr(beat, "selection_origin", "") or "",
    )
    return any(token in marker for token in CHAPTER_BEAT_PINNED_MARKERS)


def _chapter_edge_beat_ids(beats: Sequence[Any]) -> set[int]:
    """每一章的章首拍 / 章尾拍（程序补进来的承接行天然落在这两个位置）。"""
    edges: set[int] = set()
    for _chapter_id, group in _group_beats_by_chapter(beats):
        if not group:
            continue
        edges.add(id(group[0]))
        if len(group) > 1:
            edges.add(id(group[-1]))
    return edges


def _timeline_discontinuity(
    group: Sequence[Any], candidates_by_id: Mapping[int, Any],
) -> float | None:
    """一组拍按当前顺序播放时，相邻两拍之间的时间轴断点总量（秒，绝对值和）。

    只用于 C′ 的告警留痕，不改变任何选句、不改写任何原话。
    """
    total = 0.0
    previous_end = None
    for beat in group:
        start = _beat_start(beat, candidates_by_id)
        end = _beat_end(beat, candidates_by_id)
        if start is None or end is None:
            previous_end = end if end is not None else previous_end
            continue
        if previous_end is not None:
            total += abs(float(start) - float(previous_end))
        previous_end = end
    return total


def _chapter_source_runs(
    group: Sequence[Any], candidates_by_id: Mapping[int, Any], *, max_step: int = 2,
) -> list[list[int]]:
    """把一章用到的源字幕 ID 切成「源里连号的段落」（相邻差 <= max_step）。"""
    ids: set[int] = set()
    for beat in group:
        ids.update(int(value) for value in _beat_subtitle_ids(beat, candidates_by_id))
    runs: list[list[int]] = []
    current: list[int] = []
    for sid in sorted(ids):
        if current and sid - current[-1] > int(max_step):
            runs.append(current)
            current = []
        current.append(sid)
    if current:
        runs.append(current)
    return runs


def _beat_subtitle_ids_in_order(
    beats: Sequence[Any], candidates_by_id: Mapping[int, Any],
) -> list[int]:
    """按 AI 给的拍顺序展开每拍引用的源字幕 ID。"""
    out: list[int] = []
    for beat in beats:
        out.extend(_beat_subtitle_ids(beat, candidates_by_id))
    return out


def _chapter_source_scramble_ratio(
    group: Sequence[Any], candidates_by_id: Mapping[int, Any], *, max_step: int = 2,
) -> float:
    """AI 的编排相对源顺序被搅乱到什么程度（0 = 完全按源序）。

    只在同一源连号段落内部统计两两逆序对占比；段落之间怎么排不算
    （跨段跳跃是章节意图，不是缺陷）。实测 6 章落在 0.00–0.32。
    """
    position = {
        int(value): index
        for index, value in enumerate(_beat_subtitle_ids_in_order(group, candidates_by_id))
    }
    pairs = 0
    inverted = 0
    for run in _chapter_source_runs(group, candidates_by_id, max_step=max_step):
        placed = [position[sid] for sid in run if sid in position]
        for left in range(len(placed)):
            for right in range(left + 1, len(placed)):
                pairs += 1
                if placed[right] < placed[left]:
                    inverted += 1
    if not pairs:
        return 0.0
    return inverted / float(pairs)


def _chapter_time_order_should_reorder(
    group: Sequence[Any], candidates_by_id: Mapping[int, Any],
) -> tuple[bool, list[Any], float, float, float]:
    """修法 C′：章内顺序默认保留 AI 编排，不再按源时间强制重排。

    返回 ``(是否重排, 排序后的拍, 原序断点, 排序后断点, 搅乱比例)``。

    原 C 段把「源时间回跳」一律当缺陷、无条件按源起点重排。实测（单品 1791266605
    预览，6 章）：AI 的回跳全是叙事编排 —— 颜色章把 394「这个颜色其实很特别」留作
    领起、395/396 拿灰的做对比、397「非常的低调」收口；帽型章把 863/864（叠穿调性）
    放在 860-862（帽檐显脸小）之前。按源时间重排会把这些收口句与语序拆散。

    置 ``CHAPTER_TIME_ORDER_GUARD_ENABLED = False`` 即回到落地前的强制重排行为。
    """
    sorted_group = sorted(
        group,
        key=lambda beat: (
            _beat_start(beat, candidates_by_id) is None,
            _beat_start(beat, candidates_by_id) or 0.0,
        ),
    )
    original = _timeline_discontinuity(group, candidates_by_id)
    sorted_cost = _timeline_discontinuity(sorted_group, candidates_by_id)
    if original is None or sorted_cost is None:
        return False, sorted_group, 0.0, 0.0, 0.0
    scramble = _chapter_source_scramble_ratio(group, candidates_by_id)
    if not CHAPTER_TIME_ORDER_GUARD_ENABLED:
        return True, sorted_group, float(original), float(sorted_cost), scramble
    return False, sorted_group, float(original), float(sorted_cost), scramble


def _polish_chapter_beats(
    beats: Sequence[Any],
    candidates_by_id: Mapping[int, Any],
    *,
    budget_seconds: float | None = None,
    floor_seconds: float | None = None,
    dedupe_threshold: float = 0.6,
    protected_candidate_ids: Sequence[int] | None = None,
) -> tuple[list[Any], list[tuple[Any, str]], list[str]]:
    """B/C/A 三道本地整改（全程留痕，不自作主张）。

    C：同一章节内出现源时间回跳 → 按源时间重排（回跳是缺陷，不是编辑意图）。
    B：同一章节内语义高度重复的拍 → 只保留最先出现的那一条。
    A：总时长超出预算 → 按“角色优先级最弱 + 最长”的顺序逐条裁到预算内。
       护栏（修法 ①②，CHAPTER_BEAT_FLOOR_ENABLED）：每章至少保留
       CHAPTER_BEAT_FLOOR_BEATS 拍 / CHAPTER_BEAT_FLOOR_SECONDS 秒，且章首拍、
       章尾拍、带补行标记的拍不裁；护栏压不下去时才回退到“每章至少一拍”。

    返回 ``(beats, removals, warnings)``；removals 中每条都为 ``(beat, reason)``。
    """
    warnings: list[str] = []
    removals: list[tuple[Any, str]] = []

    # C：章内时间顺序
    ordered: list[Any] = []
    for _group_index, (chapter_id, group) in enumerate(_group_beats_by_chapter(beats)):
        known = [(index, _beat_start(beat, candidates_by_id)) for index, beat in enumerate(group)]
        known = [(index, start) for index, start in known if start is not None]
        regressed = any(known[i][1] < known[i - 1][1] - 0.5 for i in range(1, len(known)))
        if not regressed:
            ordered.extend(group)
            continue
        if _group_index == 0:
            # 开篇章（含片头第一拍）保留 AI 编排顺序：导演会刻意把视频后段的强爆点
            # 提来做开场，源时间回跳是编排意图而不是缺陷。按源时间重排会把 Hook 挤
            # 出片头（实测：C1 的 41-45 被 28/29 顶掉，成品开头变成两句无关碎片）。
            ordered.extend(group)
            warnings.append(f"chapter_time_order_kept_for_opening:{chapter_id}")
            continue
        keep, sorted_group, original_cost, sorted_cost, _scramble = (
            _chapter_time_order_should_reorder(group, candidates_by_id)
        )
        if CHAPTER_TIME_ORDER_GUARD_ENABLED and not keep:
            # 默认策略：保留 AI 的章内编排顺序（读顺优先于源时间顺序），只留痕。
            ordered.extend(group)
            if sorted_group != list(group):
                warnings.append(
                    "chapter_time_order_preserved:%s:scramble=%.2f:discontinuity=%.2f->%.2f"
                    % (chapter_id, _scramble, original_cost, sorted_cost)
                )
            continue
        ordered.extend(sorted_group)
        warnings.append(f"chapter_time_order_fixed:{chapter_id}")
    beats = ordered

    # B：章内去冗余
    deduped: list[Any] = []
    for chapter_id, group in _group_beats_by_chapter(beats):
        survivors: list[Any] = []
        for beat in group:
            text = _beat_text(beat, candidates_by_id)
            redundant = any(
                _text_gram_overlap(text, _beat_text(other, candidates_by_id)) >= dedupe_threshold
                for other in survivors
            )
            if (
                redundant
                and not (CHAPTER_BEAT_FLOOR_ENABLED and _beat_is_pinned(beat))
                and len(survivors) >= 1
                and len(survivors) + (len(group) - len(survivors)) > 1
            ):
                removals.append((beat, "intra_chapter_redundant"))
                warnings.append(f"beat_removed_redundant:{chapter_id}")
                continue
            survivors.append(beat)
        deduped.extend(survivors or list(group[:1]))
    beats = deduped

    # A：时长预算
    if budget_seconds is not None:
        total = sum(_beat_seconds(beat, candidates_by_id) for beat in beats)
        trimmed = False
        # 护栏全开时先按「章级配额 + 补行锁定」裁；护栏让预算压不下去才回退旧行为。
        relaxed = not CHAPTER_BEAT_FLOOR_ENABLED
        protected = (
            {int(item) for item in (protected_candidate_ids or ())}
            if CHAPTER_OPENING_DECLARED_PROTECTED else set()
        )
        while total > budget_seconds:
            counts: dict[str, int] = {}
            chapter_seconds: dict[str, float] = {}
            for beat in beats:
                key = str(getattr(beat, "chapter_id", "") or "")
                counts[key] = counts.get(key, 0) + 1
                chapter_seconds[key] = chapter_seconds.get(key, 0.0) + _beat_seconds(
                    beat, candidates_by_id
                )
            edge_ids = _chapter_edge_beat_ids(beats) if not relaxed else set()
            victim = None
            for beat in sorted(
                beats,
                key=lambda item: (
                    -_CHAPTER_BEAT_ROLE_PRIORITY.get(str(getattr(item, "narrative_role", "") or ""), 9),
                    -_beat_seconds(item, candidates_by_id),
                ),
            ):
                key = str(getattr(beat, "chapter_id", "") or "")
                if key == "OPENING":
                    continue
                # P0-1（2026-10-03 David 定）：AI 声明的开场行不裁。声明在裁剪这一层
                # 还只是一组 candidate id，没有任何一拍带着它的身份 —— 实测开场声明的
                # #272 被当「最弱角色」裁掉后，开场成段只能再造一行补在末尾。
                if protected and any(
                    int(item) in protected
                    for item in (getattr(beat, "candidate_evidence", ()) or ())
                ):
                    continue
                if relaxed:
                    if counts.get(key, 0) <= 1:
                        continue
                else:
                    # ② 补行锁定：程序补进来的行不裁
                    if _beat_is_pinned(beat):
                        continue
                    # ② 章首 / 章尾拍不裁（补进来的承接行天然落在这两处）
                    if CHAPTER_BEAT_EDGE_PROTECTED and id(beat) in edge_ids:
                        continue
                    # ① 章级最低配额：拍数与秒数都不许削到配额以下
                    if counts.get(key, 0) <= CHAPTER_BEAT_FLOOR_BEATS:
                        continue
                    if (
                        chapter_seconds.get(key, 0.0) - _beat_seconds(beat, candidates_by_id)
                        < CHAPTER_BEAT_FLOOR_SECONDS
                    ):
                        continue
                victim = beat
                break
            if victim is None:
                if not relaxed:
                    relaxed = True
                    warnings.append("duration_budget_chapter_floor_relaxed")
                    continue
                warnings.append("duration_budget_chapter_floor_kept")
                break
            # 下限保护：按整拍裁时，若去掉这一拍会掉到 floor（source_min）以下，
            # 宁可不裁，也别让成片低于验收下限（尤其 30s 这类小目标）。
            if floor_seconds is not None and total - _beat_seconds(victim, candidates_by_id) < float(floor_seconds):
                break
            beats = [beat for beat in beats if beat is not victim]
            removals.append((victim, "duration_budget_exceeded"))
            total -= _beat_seconds(victim, candidates_by_id)
            trimmed = True
        if trimmed:
            warnings.append("duration_budget_trim_applied")
    return list(beats), removals, warnings


def _text_gram_containment(value: str, container: str) -> float:
    """短句的字面 gram 有多大比例被长句包含（0..1）。

    与 ``_text_gram_overlap``（Jaccard）不同：不被长句的多余内容稀释，专用于判断
    「这一短拍是不是已经被邻句说全了」。
    """
    def _grams(text: str) -> set:
        clean = "".join(ch for ch in str(text or "") if ch.isalnum())
        return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))}

    needle, haystack = _grams(value), _grams(container)
    if not needle or not haystack:
        return 0.0
    return len(needle & haystack) / float(len(needle))


def _fragment_utterance_reason(
    text: str,
    *,
    referent_present: bool,
    predecessor_text: str = "",
    successor_text: str = "",
) -> str:
    """修法 A：这一拍是不是「读不通的碎句」——只认四种确定性形态。

    ① 顿号结尾的光名词碎片（「毛毛马甲、」）：原句是并列枚举的一半，单独成拍没有谓词。
    ② 短句 + 指代悬空（「因为它不胖。」）：以指代/承接词起头、整句 <= 8 字，而它在源里
       紧邻的上一句没被任何章选中 —— 指代对象根本不在片子里。
    ③ 无谓语残句（「挺大的，」）：<= CHAPTER_FRAGMENT_MAX_PREDICATELESS_CHARS 字、且一个
       谓语/判断/程度成分都没有，而源里紧邻的上一句没被选中。
    ④ 源紧邻重复（「它是超大冒型，」）：源里紧邻的下一句（已入选）与本拍高度重合，
       本拍等于下一句的摘要，删掉它信息量为零。

    其它情况一律不动：长句、收口完整的短句、上文在片内的承接句都不判。

    ``predecessor_text`` / ``successor_text`` 为源字幕里紧邻的上一行 / 下一行原文
    （可选）。本函数只做字面判断，不做任何语义选择、不改写、不替换任何原话。
    """
    value = str(text or "").strip()
    if not value:
        return ""
    core = value.rstrip("，,、；;：:。！？!?~… ")
    if not core:
        return ""
    if value[-1] == "、":
        return "fragment_noun_only"
    # ④ 先判：源紧邻的下一句已把本拍信息说全 → 本拍是重复摘要，删掉信息量为零。
    # 用「包含度」而非 Jaccard：短拍被长邻句包含时 Jaccard 会被稀释（实测
    # 「它是超大冒型」vs「你看再打开这么大非常大的一个冒型」Jaccard 仅 0.05）。
    if (
        CHAPTER_FRAGMENT_PREDICATE_GATE_ENABLED
        and len(core) <= int(CHAPTER_FRAGMENT_MAX_CORE_CHARS)
        and successor_text
        and _text_gram_containment(core, successor_text) >= CHAPTER_FRAGMENT_NEIGHBOR_CONTAINMENT
    ):
        return "fragment_neighbor_duplicate"
    # ③ 无谓语残句：任何谓语 / 判断 / 程度成分都会让它落回「可独立成句」。
    if (
        CHAPTER_FRAGMENT_PREDICATE_GATE_ENABLED
        and len(core) <= int(CHAPTER_FRAGMENT_MAX_PREDICATELESS_CHARS)
        and not any(marker in core for marker in CHAPTER_FRAGMENT_PREDICATE_MARKERS)
        and not referent_present
    ):
        return "fragment_predicateless"
    if (
        len(core) <= int(CHAPTER_FRAGMENT_MAX_CORE_CHARS)
        and not referent_present
        and core.startswith(CHAPTER_FRAGMENT_DANGLING_HEADS)
    ):
        return "fragment_dangling_reference"
    return ""


def _beat_subtitle_ids(beat: Any, candidates_by_id: Mapping[int, Any]) -> list[int]:
    """这一拍引用到的源字幕 ID（按候选的原始 lineage 展开）。"""
    out: list[int] = []
    for candidate_id in getattr(beat, "candidate_evidence", ()) or ():
        candidate = candidates_by_id.get(int(candidate_id))
        if candidate is None:
            continue
        out.extend(int(value) for value in (getattr(candidate, "origin_subtitle_ids", ()) or ()))
    return out


def _drop_fragment_beats(
    beats: Sequence[Any],
    candidates_by_id: Mapping[int, Any],
    *,
    opening_chapter_ids: Sequence[str] = (),
    protected_candidate_ids: Sequence[int] | None = None,
    source_texts_by_id: Mapping[int, str] | None = None,
) -> tuple[list[Any], list[tuple[Any, str]], list[str]]:
    """修法 A：碎句退回候选池（不删章、不改写文本、不动时间戳、不重排）。

    返回 ``(beats, removals, warnings)``；removals 每条为 ``(beat, reason)``。
    护栏：开篇章不判、程序补行（pinned）不判、AI 声明的开场行不判、一章只剩这一拍时
    不判（留给章级配额 / 章尾收口兜底，避免把章判空）。
    """
    if not CHAPTER_FRAGMENT_GATE_ENABLED or not beats:
        return list(beats), [], []
    skip = {str(item or "") for item in (opening_chapter_ids or ()) if str(item or "")}
    protected = {int(item) for item in (protected_candidate_ids or ())}
    source_texts_by_id = {
        int(key): str(value) for key, value in (source_texts_by_id or {}).items()
    }
    used: set[int] = set()
    for beat in beats:
        used.update(_beat_subtitle_ids(beat, candidates_by_id))
    kept: list[Any] = []
    removals: list[tuple[Any, str]] = []
    warnings: list[str] = []
    for chapter_id, group in _group_beats_by_chapter(beats):
        key = str(chapter_id or "")
        for beat in group:
            if key in skip or _beat_is_pinned(beat):
                kept.append(beat)
                continue
            evidence = {int(item) for item in (getattr(beat, "candidate_evidence", ()) or ())}
            if protected & evidence:
                kept.append(beat)
                continue
            subtitle_ids = _beat_subtitle_ids(beat, candidates_by_id)
            referent_present = any((int(value) - 1) in used for value in subtitle_ids)
            # 修法 A③：源里紧邻的上一行 / 下一行原文。只在**该相邻行也入选**时才取，
            # 避免把片外的上下文行当成片内事实。
            predecessor_text = ""
            successor_text = ""
            if CHAPTER_FRAGMENT_PREDICATE_GATE_ENABLED and source_texts_by_id:
                for _head in (int(value) for value in subtitle_ids):
                    _prev = source_texts_by_id.get(int(_head) - 1)
                    if _prev and (int(_head) - 1) in used:
                        predecessor_text = str(_prev)
                        break
                for _head in (int(value) for value in subtitle_ids):
                    _next = source_texts_by_id.get(int(_head) + 1)
                    if _next and (int(_head) + 1) in used:
                        successor_text = str(_next)
                        break
            reason = _fragment_utterance_reason(
                _beat_text(beat, candidates_by_id),
                referent_present=referent_present,
                predecessor_text=predecessor_text,
                successor_text=successor_text,
            )
            if not reason:
                kept.append(beat)
                continue
            if len(group) <= 1:
                warnings.append(
                    "utterance_fragment_kept_single_beat:%s:%s"
                    % (key, ",".join(str(value) for value in subtitle_ids) or "-")
                )
                kept.append(beat)
                continue
            removals.append((beat, reason))
    return kept, removals, warnings


def _topic_overlap_ratio(text: str, label: str) -> float:
    """修法 C：句子的实义汉字有多少比例出现在本章标题 / 任务里（只用于告警）。"""

    def core(value: Any) -> set[str]:
        return {
            char for char in str(value or "")
            if char not in CHAPTER_TOPIC_STOP_CHARS and "\u4e00" <= char <= "\u9fff"
        }

    left, right = core(text), core(label)
    if not left or not right:
        return 1.0
    return len(left & right) / float(len(left))


def _chapter_topic_consistency_warnings(
    beats: Sequence[Any],
    candidates_by_id: Mapping[int, Any],
    chapter_labels: Mapping[str, str],
) -> list[str]:
    """修法 C：章内一致性告警 —— 跨话题句只留痕，不动手（不删、不改写、不重排）。"""
    if not CHAPTER_TOPIC_CONSISTENCY_ENABLED:
        return []
    warnings: list[str] = []
    for chapter_id, group in _group_beats_by_chapter(beats):
        key = str(chapter_id or "")
        label = str(chapter_labels.get(key) or "").strip()
        # 少于 3 拍的章不做离群判定（2 拍互为唯一邻居，判没有意义）。
        if len(group) < 3:
            continue
        texts = [(beat, _beat_text(beat, candidates_by_id)) for beat in group]
        for beat, text in texts:
            if not text or _beat_is_pinned(beat):
                continue
            # 双通道：既和本章标题/任务无交集，也和本章其他句都无交集 —— 才算跨话题。
            # 只比标题会误报（如「还可以这样去扣嘞。」对「拼皮防风叠穿更暖」），
            # 只比兄弟句会漏报，两个通道取较大值最稳。
            best = _topic_overlap_ratio(text, label) if label else 0.0
            for other, other_text in texts:
                if other is beat or not other_text:
                    continue
                best = max(best, _topic_overlap_ratio(text, other_text))
                if best >= float(CHAPTER_TOPIC_OVERLAP_MIN):
                    break
            if best >= float(CHAPTER_TOPIC_OVERLAP_MIN):
                continue
            subtitle_ids = _beat_subtitle_ids(beat, candidates_by_id)
            warnings.append(
                "chapter_topic_offbeat:%s:%s"
                % (key, ",".join(str(value) for value in subtitle_ids) or "-")
            )
    return warnings


def _enforce_chapter_beat_floor(
    beats: Sequence[Any],
    candidates_by_id: Mapping[int, Any],
    opening_chapter_id: str = "",
) -> tuple[list[Any], list[str]]:
    """把被「开场成段」掏空的碎条章并入相邻章。

    判据只看秒数：一章总时长低于 ``CHAPTER_BEAT_FLOOR_SECONDS`` 才算碎条
    （实测形态 = 声明的开场行被整拍搬走后，源章只剩 1 拍 0.96s）。
    只改分组：被并入章里的拍原样保留（仅 ``chapter_id`` 换成邻章），不改写文本、
    不动时间戳、不重排顺序、不删除任何一句。开关关闭或不足两章时原样返回。
    """
    if not CHAPTER_BEAT_FLOOR_AFTER_OPENING_ENABLED or len(beats) < 2:
        return list(beats), []
    result = list(beats)
    warnings: list[str] = []
    for _ in range(3):
        seconds: dict[str, float] = {}
        order: list[str] = []
        for beat in result:
            key = str(getattr(beat, "chapter_id", "") or "")
            if key not in order:
                order.append(key)
            seconds[key] = round(seconds.get(key, 0.0) + _beat_seconds(beat, candidates_by_id), 3)
        # 触发条件只看「秒数」：一章总时长低于配额才算碎条。
        # 刻意不把「拍数」单独当触发条件 —— 1 拍但 15 秒的章是内容完整的正常章，
        # 按拍数并掉它会违反「程序不得改动 AI 章节分组」的既有契约
        # （tests/test_single_pass_director.py 有两条用例锁定这一点）。
        # 修法 B（2026-10-05 David 定）：最后一章不许被配额并进前一章。
        # 实测混剪 S3：只剩 1 句 1.95s 的第六章「秋天到冷天都能穿」被整体并进第五章，
        # "结尾"这个位置整段消失、最后落点变成一句倒跳 331 秒的温度话。宁可留一个
        # 独立的小尾章，也不把尾章拆散混进上一章；只留痕，不动其它章。
        tail_key = str(order[-1] or "") if order else ""
        tail_below_floor = bool(tail_key) and tail_key != opening_chapter_id and (
            seconds.get(tail_key, 0.0) < float(CHAPTER_BEAT_FLOOR_SECONDS)
        )
        victim = next(
            (
                key for key in order
                if key and key != opening_chapter_id
                and seconds.get(key, 0.0) < float(CHAPTER_BEAT_FLOOR_SECONDS)
                and not (CHAPTER_TAIL_CHAPTER_KEEP_ENABLED and key == tail_key)
            ),
            None,
        )
        if victim is None:
            if tail_below_floor and CHAPTER_TAIL_CHAPTER_KEEP_ENABLED:
                warnings.append(f"chapter_beat_floor_tail_kept:{tail_key}")
            break
        position = order.index(victim)
        target = next((key for key in order[position + 1:] if key and key != opening_chapter_id), "")
        if not target:
            target = next(
                (key for key in reversed(order[:position]) if key and key != opening_chapter_id), "",
            )
        if not target:
            # 只剩开篇章可并：宁可不并，也不把碎条塞进片头。
            warnings.append(f"chapter_beat_floor_unmergeable:{victim}")
            break
        result = [
            replace(beat, chapter_id=target)
            if str(getattr(beat, "chapter_id", "") or "") == victim else beat
            for beat in result
        ]
        warnings.append(f"chapter_beat_floor_merged:{victim}:{target}")
    return result, warnings



def _chapter_tail_row_unfinished(text: str) -> bool:
    """这一行是不是「话没说完」（句内停顿符结尾）——与 analyzer 层同源。"""
    value = str(text or "").strip()
    return bool(value) and value[-1] in CHAPTER_TAIL_SETTLE_PAUSES


def _chapter_tail_row_closes(text: str) -> bool:
    """这一行是不是已经「收口」（句末标点，或压根没有停顿符）。"""
    value = str(text or "").strip()
    if not value:
        return True
    return value[-1] in CHAPTER_TAIL_SETTLE_CLOSERS or value[-1] not in CHAPTER_TAIL_SETTLE_PAUSES


def _settle_chapter_tails(
    beats: Sequence[Any],
    candidates_by_id: Mapping[int, Any],
    candidate_by_subtitle: Mapping[int, Any],
    opening_chapter_ids: Sequence[str] = (),
) -> tuple[list[Any], list[Any], list[str]]:
    """章尾半句收口：管线最后一道（裁剪 / 补行 / 开场成段 / 章级配额之后）。

    只做两件事：① 章尾仍未收口 → 沿原文顺序向后补行，补到真正收口为止（有界，
    最多 ``CHAPTER_TAIL_SETTLE_MAX_ROWS`` 行）；② 补不动（下一行取不到 / 已被任何
    章选中 / 超上限）→ 原样保留并留痕。不删句、不改写文本、不动时间戳、不跨章搬走
    别章已选的行。返回 ``(beats, 新增候选, warnings)``。
    """
    if not CHAPTER_TAIL_SETTLE_ENABLED or not beats:
        return list(beats), [], []
    result = list(beats)
    warnings: list[str] = []
    added: list[Any] = []
    skip = {str(item or "") for item in (opening_chapter_ids or ()) if str(item or "")}
    ordered_ids = sorted(int(value) for value in candidate_by_subtitle)
    next_source_id: dict[int, int] = {
        sid: ordered_ids[index + 1] for index, sid in enumerate(ordered_ids[:-1])
    }

    def subtitle_ids(beat: Any) -> list[int]:
        out: list[int] = []
        for candidate_id in getattr(beat, "candidate_evidence", ()) or ():
            candidate = candidates_by_id.get(int(candidate_id))
            if candidate is None:
                continue
            out.extend(int(value) for value in (getattr(candidate, "origin_subtitle_ids", ()) or ()))
        return out

    used: set[int] = set()
    for beat in result:
        used.update(subtitle_ids(beat))

    for chapter_id, group in _group_beats_by_chapter(result):
        key = str(chapter_id or "")
        if not group or key in skip:
            continue
        tail = group[-1]
        tail_ids = subtitle_ids(tail)
        if not tail_ids:
            continue
        tail_id = tail_ids[-1]
        if not _chapter_tail_row_unfinished(_beat_text(tail, candidates_by_id)):
            continue
        chain: list[tuple[int, Any]] = []
        cursor = next_source_id.get(tail_id)
        closed = False
        while cursor is not None and len(chain) < int(CHAPTER_TAIL_SETTLE_MAX_ROWS):
            if cursor in used:
                break
            candidate = candidate_by_subtitle.get(int(cursor))
            if candidate is None:
                break
            chain.append((int(cursor), candidate))
            if _chapter_tail_row_closes(str(getattr(candidate, "text", "") or "")):
                closed = True
                break
            cursor = next_source_id.get(int(cursor))
        position = next((index for index, beat in enumerate(result) if beat is tail), None)
        if not closed or not chain or position is None:
            warnings.append(f"chapter_tail_unsettled:{key}:{tail_id}")
            continue
        additions: list[Any] = []
        for subtitle_id, candidate in chain:
            additions.append(replace(
                tail,
                chapter_id=str(getattr(tail, "chapter_id", "") or ""),
                candidate_evidence=(int(candidate.candidate_id),),
                target_seconds=round(float(getattr(candidate, "duration", 0.0) or 0.0), 3),
                selection_instruction="chapter_tail_settle_fill",
                selection_origin="chapter_tail_settle_fill",
                transition_from_previous="",
            ))
            added.append(candidate)
            used.add(subtitle_id)
        result[position + 1:position + 1] = additions
        warnings.append(
            "chapter_tail_settled:%s:%s" % (key, ",".join(str(sid) for sid, _ in chain))
        )
    return result, added, warnings


def build_single_pass_director_plan(
    *,
    strategy: Strategy,
    safe_candidates: Sequence[PlanningCandidate],
    target_duration: float,
    selection_contract: Mapping[str, Any] | None,
    director_contract: Mapping[str, Any] | None,
    candidate_words: Mapping[int, Sequence[Mapping[str, Any]]] | None = None,
) -> NarrativePlan:
    """Bind the one-call director sequence to real executable source clips.

    ``candidate_words``（可选）是按 candidate_id 索引的词级时间线。**只有**当
    导演在某一拍上写了 ``start_word_offset`` / ``end_word_offset`` 时才需要它：
    子跨度必须按词切片，没有词条就无法做词级精确的收窄。传 ``None`` 或不传
    时，所有子跨度请求都会被如实记为缺血缘并**退回整条候选**，行为与改动前一致。
    """
    contract = dict(selection_contract or {})
    director = dict(director_contract or {})
    sequence = tuple(strategy.director_sequence)
    candidate_by_subtitle = _candidate_for_subtitle(safe_candidates)
    selected: list[PlanningCandidate] = []
    beats: list[NarrativeBeat] = []
    # Only technically unrenderable source failures belong in ``issues``.
    # Director semantics and quality stay visible but may not delete/reorder a
    # paid one-call result or keep an editable preview out of M3.
    issues: list[str] = []
    quality_warnings: list[str] = []
    seen_candidate_ids: set[int] = set()
    seen_purchase_values: set[tuple[str, str, str]] = set()
    established_questions: set[str] = set()
    whole_video_audit_verification: dict[str, Any] = {}
    # Historical fixtures can still be read, but every new one-call packet is
    # required to carry the relationships the Director authored in that call.
    enforce_journey_contract = bool(director.get("single_ai_director_packet"))
    opening_question_ids, opening_answer_roles = _opening_scope(director, sequence)
    opening_fallbacks_used: list[dict[str, Any]] = []
    verified_chapter_parts: dict[str, list[str]] = {}

    # 子跨度：按 candidate_id 索引的词级时间线（调用方可选传入）。
    # 不传时任何偏移请求都会因缺血缘而退回整条候选 —— 行为与改动前一致。
    words_by_candidate: Mapping[int, Sequence[Mapping[str, Any]]] = candidate_words or {}
    # 收窄后的派生候选，其词切片必须随计划交回调用方：
    # 按「整行」匹配的 binder 无法为子跨度重建血缘（会报 candidate_text_span_mismatch）。
    derived_candidate_words: dict[int, tuple[dict[str, Any], ...]] = {}
    # 派生候选的 Ledger 行：保真审计要求计划里每个候选都能在 Ledger 中找到，
    # 且 text/start/end 一致。派生候选不是独立素材，必须由计划自带这一行。
    derived_candidate_ledger: list[dict[str, Any]] = []
    _sub_span_counter = [0]

    def _narrow_to_sub_span(
        candidate: PlanningCandidate,
        *,
        start_word_offset: int | None,
        end_word_offset: int | None,
    ) -> tuple[PlanningCandidate, str]:
        """收窄一条候选；失败时**原样返回**并给出原因（调用方记录告警）。"""
        words = words_by_candidate.get(int(candidate.candidate_id))
        if not words:
            return candidate, "sub_span_word_lineage_missing"
        derived_id = SUB_SPAN_CANDIDATE_ID_BASE + _sub_span_counter[0]
        _sub_span_counter[0] += 1
        derived, sliced, reason = narrow_candidate_to_word_span(
            candidate, words,
            start_word_offset=start_word_offset,
            end_word_offset=end_word_offset,
            derived_id=derived_id,
        )
        if reason:
            return candidate, reason
        if derived is candidate:
            return candidate, ""
        derived_candidate_words[int(derived.candidate_id)] = sliced
        derived_candidate_ledger.append({
            "candidate_id": int(derived.candidate_id),
            "source_id": str(derived.source_id),
            "start": float(derived.start),
            "end": float(derived.end),
            "text": str(derived.text),
            "origin_subtitle_ids": [int(item) for item in derived.origin_subtitle_ids],
            "sub_span_of_candidate_id": int(candidate.candidate_id),
            "sub_span_start_word_offset": start_word_offset,
            "sub_span_end_word_offset": end_word_offset,
        })
        return derived, ""

    def resolve_source_ids(
        subtitle_ids: Sequence[int], *, packet_label: str, opening: bool, verbatim: str = "",
        start_word_offset: int | None = None, end_word_offset: int | None = None,
    ) -> tuple[list[PlanningCandidate], list[str]]:
        """Resolve one AI-authored contiguous spoken span.

        Individual SRT rows are timing units.  Publishability and opening
        independence therefore apply to their concatenated span; evaluating
        every row alone recreates the fragment problem this contract avoids.

        可选的首/末词偏移只做**边界收窄**：丢掉首行开头的若干词、末行结尾的
        若干词。收窄失败（标点血缘不可复原、越界、切出来不能独立播放）时
        **退回整条候选**并记质量告警，绝不静默丢弃这一拍。
        """
        resolved_rows: list[PlanningCandidate] = []
        local_ids: set[int] = set()
        rejected: list[str] = []
        for subtitle_id in subtitle_ids:
            candidate = candidate_by_subtitle.get(int(subtitle_id))
            if candidate is None:
                rejected.append(
                    f"single_pass_director_source_unresolved:{packet_label}:subtitle_id={subtitle_id}"
                )
                continue
            if candidate.candidate_id in seen_candidate_ids:
                quality_warnings.append(
                    f"single_pass_director_candidate_reused:{packet_label}:candidate_id={candidate.candidate_id}"
                )
            if candidate.candidate_id in local_ids:
                # Multiple source subtitle IDs may legitimately resolve to one
                # already-merged safe candidate.  Play it once in that span.
                continue
            resolved_rows.append(candidate)
            local_ids.add(candidate.candidate_id)
        if rejected or not resolved_rows:
            return [], rejected
        for previous, current in zip(resolved_rows, resolved_rows[1:]):
            gap = float(current.start) - float(previous.end)
            if float(current.start) < float(previous.start):
                return [], [
                    f"single_pass_director_source_time_reversed:{packet_label}:"
                    f"candidate_id={previous.candidate_id}->{current.candidate_id}"
                ]
            if gap > 1.5:
                quality_warnings.append(
                    f"single_pass_director_source_span_gap_warning:{packet_label}:"
                    f"candidate_id={previous.candidate_id}->{current.candidate_id}:gap={gap:.3f}"
                )
        # 收窄前的完整跨度原文。verbatim 允许匹配它，也允许匹配收窄后的文本：
        # 提示词要求「逐字复制整个窗口」，导演若按整窗写 verbatim 同时给了收窄偏移，
        # 那不是违约而是本合同的正常用法。只有两者都不匹配才算 verbatim 不符。
        full_span_text = " ".join(str(item.text or "").strip() for item in resolved_rows).strip()
        # 收窄来源优先级：
        #   1) 导演显式给的词偏移（精确，可能是 -1 = 不收窄 -> 已降级为 None）
        #   2) 由 verbatim 对齐推出的偏移 —— 导演用「逐字复制想保留的原文」表达收窄，
        #      程序把它翻译成词边界。这是 LLM 已经稳定会做的动作（每轮都填对
        #      verbatim），而不是它不肯做的动作（数词给偏移）。
        # 显式偏移一旦给出就以它为准；两者都没有时完全没有收窄，行为与改动前一致。
        effective_start, effective_end = start_word_offset, end_word_offset
        if (
            start_word_offset is None and end_word_offset is None
            and _verbatim_key(verbatim)
            and _verbatim_key(verbatim) != _verbatim_key(full_span_text)
        ):
            located = locate_kept_span_in_rows(resolved_rows, words_by_candidate, verbatim)
            if located is None:
                quality_warnings.append(
                    f"single_pass_director_kept_text_unlocatable:{packet_label}"
                )
            else:
                effective_start, effective_end = located
        # 边界收窄（可选）。只按词切片；任一失败都退回整条候选并如实记告警，
        # 绝不静默丢弃这一拍、也绝不放行一个不能独立播放的跨度。
        if effective_start is not None or effective_end is not None:
            rows_out = list(resolved_rows)
            if len(rows_out) == 1:
                rows_out[0], issue = _narrow_to_sub_span(
                    rows_out[0],
                    start_word_offset=effective_start,
                    end_word_offset=effective_end,
                )
                if issue:
                    quality_warnings.append(
                        f"single_pass_director_sub_span_fallback:{packet_label}:"
                        f"candidate_id={rows_out[0].candidate_id}:{issue}"
                    )
            else:
                if effective_start is not None:
                    rows_out[0], issue = _narrow_to_sub_span(
                        rows_out[0], start_word_offset=effective_start, end_word_offset=None,
                    )
                    if issue:
                        quality_warnings.append(
                            f"single_pass_director_sub_span_fallback:{packet_label}:head:{issue}"
                        )
                if effective_end is not None:
                    last_index = len(rows_out) - 1
                    rows_out[last_index], issue = _narrow_to_sub_span(
                        rows_out[last_index], start_word_offset=None, end_word_offset=effective_end,
                    )
                    if issue:
                        quality_warnings.append(
                            f"single_pass_director_sub_span_fallback:{packet_label}:tail:{issue}"
                        )
            resolved_rows = rows_out
        combined_text = " ".join(str(item.text or "").strip() for item in resolved_rows).strip()
        if _verbatim_key(verbatim):
            # 允许两种都算符合：整窗原文（导演按提示词逐字复制了整个窗口），
            # 或收窄后的原文（导演只复制了保留部分）。两者都不符才算违约。
            if _verbatim_key(verbatim) not in {
                _verbatim_key(combined_text), _verbatim_key(full_span_text),
            }:
                quality_warnings.append(
                    f"single_pass_director_verbatim_mismatch:{packet_label}"
                )
        combined_duration = float(resolved_rows[-1].end) - float(resolved_rows[0].start)
        if len(resolved_rows) > 1 and combined_duration > 12.0:
            quality_warnings.append(
                f"single_pass_director_source_span_long_warning:{packet_label}:"
                f"duration={combined_duration:.3f}"
            )
        surface_issue = final_utterance_surface_issue(combined_text)
        if surface_issue:
            quality_warnings.append(
                f"single_pass_director_final_utterance_warning:{packet_label}:"
                f"candidate_ids={','.join(str(item.candidate_id) for item in resolved_rows)}:{surface_issue}"
            )
        if opening:
            first = resolved_rows[0]
            if not first.hook_eligible:
                quality_warnings.append(
                    f"single_pass_director_hook_ineligible_warning:{packet_label}:candidate_id={first.candidate_id}"
                )
            opening_issue = _opening_surface_issue(combined_text)
            if opening_issue:
                quality_warnings.append(
                    f"single_pass_director_opening_quality_warning:{packet_label}:"
                    f"candidate_ids={','.join(str(item.candidate_id) for item in resolved_rows)}:{opening_issue}"
                )
        return resolved_rows, rejected

    # Opening alternatives are direction metadata only.  Programmatic
    # replacement caused a valid AI story to lose its premise and collapse to
    # a few disconnected lines, so the primary sequence is now immutable.
    chapter_by_beat_id = {
        str(beat.beat_id or "").strip(): chapter
        for chapter in strategy.director_chapter_packets
        for beat in chapter.beats
        if str(beat.beat_id or "").strip()
    }
    for index, packet in enumerate(sequence, 1):
        question_id = str(packet.purchase_question_id or "").strip().upper()
        answer_role = str(packet.answer_role or packet.role or "purchase_progress").strip().lower()
        outcome = str(packet.purchase_outcome or "").strip()
        coverage = str(packet.coverage or "recommended").strip().lower()
        if enforce_journey_contract:
            relation_issues = _packet_relation_issues(
                packet,
                index=index,
                prior_question_ids=established_questions,
                seen_claims=seen_purchase_values,
                opening_question_ids=opening_question_ids,
                opening_answer_roles=opening_answer_roles,
            )
            if relation_issues:
                quality_warnings.extend(relation_issues)
        packet_label = packet.beat_id or str(index)
        authored_chapter = chapter_by_beat_id.get(str(packet_label).strip())
        resolved: list[PlanningCandidate] = []
        if not packet.subtitle_ids:
            issues.append(f"single_pass_director_empty_subtitle_ids:{packet_label}")
            continue
        resolved, primary_resolution_issues = resolve_source_ids(
            packet.subtitle_ids, packet_label=packet_label, opening=index == 1,
            verbatim=str(getattr(packet, "verbatim", "") or ""),
            # 导演可选地收窄首/末边界（只取某行的中间一段）。两者为 None 时
            # 与改动前完全一致。开场备选不动 —— 单遍执行器本来就不使用它。
            start_word_offset=getattr(packet, "start_word_offset", None),
            end_word_offset=getattr(packet, "end_word_offset", None),
        )
        issues.extend(primary_resolution_issues)
        if not resolved:
            continue
        for candidate in resolved:
            seen_candidate_ids.add(candidate.candidate_id)
            selected.append(candidate)
            if authored_chapter:
                verified_chapter_parts.setdefault(authored_chapter.chapter_id, []).append(
                    str(candidate.text or "").strip()
                )
        role = str(packet.role or answer_role or "purchase_progress").strip() or "purchase_progress"
        is_opening = index == 1
        # Relation data comes directly from the one AI packet.  The fallback
        # exists only for legacy non-single-pass callers; it never enriches a
        # new director result on the program's own judgment.
        mapped_question_id = question_id or f"D{index}"
        mapped_outcome = outcome or f"director_step_{index}"
        beats.append(NarrativeBeat(
            # Multiple source beats may deliberately form one complete
            # micro-narrative chapter.  Reusing the AI-authored chapter id is
            # grouping metadata only; playback order remains ``sequence``.
            chapter_id=(authored_chapter.chapter_id if authored_chapter else f"D{index}"),
            source_role="hook" if is_opening else role,
            narrative_role="hook" if is_opening else role,
            goal=str(
                (authored_chapter.title if authored_chapter else "")
                or packet.goal or "真实购买信息推进"
            ).strip(),
            candidate_evidence=tuple(candidate.candidate_id for candidate in resolved),
            required=coverage == "required",
            target_seconds=round(sum(candidate.duration for candidate in resolved), 3),
            selection_instruction="single_ai_director_packet_exact_source_order",
            selection_origin="single_ai_director_packet",
            transition_from_previous=str(packet.why_this_follows or "").strip(),
            value_dimension=mapped_question_id,
            # These fields record the Director's declared relationship; no
            # program derives a semantic outcome from the source text.
            purchase_value_dimension=(
                "same_claim_additional_proof" if packet.supports_question_id else "new_outcome"
            ),
            purchase_value_domain=mapped_question_id,
            purchase_value_outcomes=(mapped_outcome,),
            purchase_value_reason=str(packet.purchase_question or packet.goal or "真实购买信息推进").strip(),
            story_support="single_ai_director_packet_source_grounded",
            commerce_beat_id=mapped_question_id,
        ))
        if enforce_journey_contract:
            established_questions.add(question_id)
            seen_purchase_values.add((question_id, answer_role, outcome))

    # P0-a（C 层）：把契约声明的独立开场真正落到片头。
    # 背景：AI 现在会把 Hook 单独声明在 opening_selection 里（不在任何章节的
    # beats 中），而历史物化只把 sequence 的第一拍当开场 → 片头直接从兑现句
    # 开始、Hook 整条消失。这里把声明的开篇解析后显式前置为一个独立 beat。
    # 开关：director_contract 的 opening_unit_split（默认关，保持旧行为）。
    if director.get("opening_unit_split") and beats:
        opening_selection = dict(getattr(strategy, "opening_selection", None) or {})
        opening_ids: list[int] = []
        for value in opening_selection.get("selected_subtitle_ids") or ():
            try:
                opening_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if opening_ids:
            opening_resolved, opening_rejected = resolve_source_ids(
                opening_ids, packet_label="OPENING", opening=True,
            )
            already_selected = {candidate.candidate_id for candidate in selected}
            opening_candidates = [
                item for item in opening_resolved if item.candidate_id not in already_selected
            ]
            if opening_candidates and not opening_rejected:
                base_beat = beats[0]
                opening_beat = replace(
                    base_beat,
                    chapter_id="OPENING",
                    source_role="hook",
                    narrative_role="hook",
                    goal="契约声明的独立开篇（opening_selection）",
                    candidate_evidence=tuple(item.candidate_id for item in opening_candidates),
                    target_seconds=round(sum(item.duration for item in opening_candidates), 3),
                    selection_instruction="contract_opening_selection",
                    selection_origin="contract_opening_selection",
                    transition_from_previous="",
                )
                beats = [opening_beat] + beats
                selected = list(opening_candidates) + list(selected)
                opening_fallbacks_used.append({
                    "source": "contract_opening_selection",
                    "subtitle_ids": list(opening_ids),
                    "candidate_ids": [item.candidate_id for item in opening_candidates],
                    "seconds": round(sum(item.duration for item in opening_candidates), 3),
                })
            elif opening_rejected:
                quality_warnings.append(
                    "single_pass_director_opening_selection_unresolved:"
                    + ",".join(str(item) for item in opening_rejected[:4])
                )

    # A+B+C：时长预算 / 章内去冗余 / 章内时间顺序（可开关，全程留痕）
    if director.get("duration_budget_trim") and beats:
        _candidates_by_id = {candidate.candidate_id: candidate for candidate in selected}
        _budget_seconds: float | None = None
        _floor_seconds: float | None = None
        try:
            # 预算严格对齐目标时长。原代码引用了不存在的 requested_seconds/speed_factor，
            # NameError 被静默吞掉 → budget 恒为 None → 裁剪从不执行，超时成片一直漏裁。
            # 成片 = 原声 / speed_factor，所以裁剪预算要用“原声口径”的 source_target，
            # 下限用 source_min —— 这样最终成片才会贴近用户设定值（而不是贴 target 数值）。
            _dr = director_delivery_duration_range(float(target_duration), None, 1.15)
            _budget_seconds = float(_dr.get("source_target") or float(target_duration) * 1.15)
            _floor_seconds = float(_dr.get("source_min") or float(target_duration) * 0.958)
        except (TypeError, ValueError):
            _budget_seconds = None
            _floor_seconds = None
        beats, _removals, _polish_warnings = _polish_chapter_beats(
            beats, _candidates_by_id, budget_seconds=_budget_seconds, floor_seconds=_floor_seconds,
            protected_candidate_ids=_opening_receipt_candidate_ids(strategy, candidate_by_subtitle),
        )
        for _beat, _reason in _removals:
            quality_warnings.append(
                f"beat_removed:{_reason}:{getattr(_beat, 'chapter_id', '')}:"
                + ",".join(str(item) for item in getattr(_beat, "candidate_evidence", ()))
            )
        quality_warnings.extend(_polish_warnings)
        _surviving_ids = {
            cid for _beat in beats for cid in getattr(_beat, "candidate_evidence", ())
        }
        selected = [
            candidate for candidate in selected if candidate.candidate_id in _surviving_ids
        ]

    # 修法 A（2026-10-05 David 定）：碎句退回候选池（见常量区说明）。
    # 时点：章内重排 + 时长裁剪已定型，章首补行 / 开场成段还没跑 —— 先把碎句退池，
    # 后面的章级配额（修法 B）与章尾收口才知道真实的章内容。
    if beats:
        try:
            _frag_candidates = {
                int(item.candidate_id): item for item in candidate_by_subtitle.values()
            }
            beats, _frag_removals, _frag_warnings = _drop_fragment_beats(
                beats,
                _frag_candidates,
                opening_chapter_ids=(OPENING_SEGMENT_CHAPTER_ID, "OPENING"),
                protected_candidate_ids=_opening_receipt_candidate_ids(strategy, candidate_by_subtitle),
                # 源字幕原文：从**本函数已有**的 candidate_by_subtitle 取（它已经在
                # 作用域里），不再引用不存在的 `subtitles` —— 上一版补丁正是因此
                # 抛 NameError，被 except 吞成 single_pass_director_fragment_gate_failed，
                # 导致整个碎句闸门静默失效。
                source_texts_by_id={
                    int(subtitle_id): str(getattr(candidate, "text", "") or "")
                    for subtitle_id, candidate in candidate_by_subtitle.items()
                },
            )
            quality_warnings.extend(_frag_warnings)
            for _frag_beat, _frag_reason in _frag_removals:
                quality_warnings.append(
                    f"beat_removed:{_frag_reason}:{getattr(_frag_beat, 'chapter_id', '')}:"
                    + ",".join(str(item) for item in getattr(_frag_beat, "candidate_evidence", ()))
                )
            if _frag_removals:
                _frag_surviving = {
                    cid for _beat in beats for cid in getattr(_beat, "candidate_evidence", ())
                }
                selected = [
                    candidate for candidate in selected
                    if candidate.candidate_id in _frag_surviving
                ]
        except Exception as _frag_exc:  # noqa: BLE001 - 碎句门失败绝不阻断导演
            quality_warnings.append(
                "single_pass_director_fragment_gate_failed:%s" % type(_frag_exc).__name__
            )

    # 修法 1（2026-10-01）：章首缺上文补齐。
    # 时点：章内重排 + 时长裁剪已经定型（此时「章首」才是最终章首），开场段成段还
    # 没跑（它只动首章）。只补行，不改写、不重排、零新增 AI 调用。
    if beats:
        try:
            _head_range = director_delivery_duration_range(float(target_duration), None, 1.15)
            _head_source_max = float(_head_range.get("source_max") or 0.0) or None
        except (TypeError, ValueError, AttributeError):
            _head_source_max = None
        try:
            beats, selected, _head_warnings = _fill_chapter_head_leadins(
                beats, selected, candidate_by_subtitle,
                source_max_seconds=_head_source_max,
            )
            quality_warnings.extend(_head_warnings)
        except Exception as _head_exc:  # noqa: BLE001 - 补齐失败绝不阻断导演
            quality_warnings.append(
                "single_pass_director_chapter_head_leadin_failed:%s" % type(_head_exc).__name__
            )

    # A 方案（2026-10-01 David 定）：把声明的开场搬成「独立开场段」。
    # 时点选在这里：时长预算裁剪已经跑完（裁剪不会再把开场带走），开篇回执的
    # 章内前移还没跑（它会把开场段的顺序按回执声明复原）。
    opening_segment: dict[str, Any] = {}
    if OPENING_SEGMENT_ENABLED and director.get("opening_unit_split") and beats:
        try:
            beats, selected, opening_segment = _materialize_opening_segment(
                beats,
                selected,
                _opening_receipt_candidate_ids(strategy, candidate_by_subtitle),
                # 传完整安全候选池：声明了却没被选中的开场行只能从这里取。
                {int(item.candidate_id): item for item in candidate_by_subtitle.values()},
            )
        except Exception as _opening_exc:  # noqa: BLE001 - 开场成段失败绝不阻断导演
            opening_segment = {}
            quality_warnings.append(
                "single_pass_director_opening_segment_failed:%s" % type(_opening_exc).__name__
            )
        if opening_segment:
            quality_warnings.append(
                "single_pass_director_opening_segment_materialized:"
                + ",".join(str(item) for item in opening_segment.get("opening_candidate_ids") or ())
            )

    # 开篇契约：AI 在 opening_selection 里声明的开场（selected_subtitle_ids）是唯一权威。
    # 历史实现只把 sequence 的第一拍当开场，一旦该拍因任何原因不再是 Hook
    # （实测：片头退化成两句无关碎片），成品开头就坏了。这里把回执声明的开场按声明
    # 顺序放回首章最前，不改其他章节、不新增或删除任何句子。
    _receipt_ids = _opening_receipt_candidate_ids(strategy, candidate_by_subtitle)
    beats, _receipt_moved = _enforce_opening_receipt_prefix(beats, _receipt_ids)
    if _receipt_moved:
        quality_warnings.append(
            f"single_pass_director_opening_receipt_prefix_enforced:{_receipt_moved}"
        )
    # 乙（2026-10-04 David 定）：开场段章首缺上文补齐（见常量区说明）。
    # 时点：开场段成段 + 回执前移都已定型，此时片头第一拍就是最终片头第一拍。
    if beats:
        try:
            try:
                _open_head_range = director_delivery_duration_range(float(target_duration), None, 1.15)
                _open_head_source_max = float(_open_head_range.get("source_max") or 0.0) or None
            except (TypeError, ValueError, AttributeError):
                _open_head_source_max = None
            beats, selected, _open_head_warnings = _fill_opening_head_leadins(
                beats, selected, candidate_by_subtitle,
                source_max_seconds=_open_head_source_max,
            )
            quality_warnings.extend(_open_head_warnings)
            if opening_segment:
                # 回执跟着实际开场段刷新：补进来的上文行也要出现在开场段回执里。
                _open_head_pool = {
                    int(item.candidate_id): item for item in candidate_by_subtitle.values()
                }
                opening_segment["opening_candidate_ids"] = [
                    int(candidate_id)
                    for beat in beats
                    if str(getattr(beat, "chapter_id", "") or "") == OPENING_SEGMENT_CHAPTER_ID
                    for candidate_id in (getattr(beat, "candidate_evidence", ()) or ())
                ]
                opening_segment["seconds"] = round(sum(
                    float(getattr(_open_head_pool.get(candidate_id), "duration", 0.0) or 0.0)
                    for candidate_id in dict.fromkeys(opening_segment["opening_candidate_ids"])
                    if _open_head_pool.get(candidate_id) is not None
                ), 3)
        except Exception as _open_head_exc:  # noqa: BLE001 - 补齐失败绝不阻断导演
            quality_warnings.append(
                "single_pass_director_opening_head_leadin_failed:%s" % type(_open_head_exc).__name__
            )
    # P0-B（2026-10-01 David 定）：开场成段后的章级配额复检（见常量区说明）。
    # 只改分组：低于配额的章并入相邻章；不删句、不改写、不重排。
    if beats:
        try:
            _floor_candidates = {
                int(item.candidate_id): item for item in candidate_by_subtitle.values()
            }
            beats, _floor_warnings = _enforce_chapter_beat_floor(
                beats,
                _floor_candidates,
                opening_chapter_id=(OPENING_SEGMENT_CHAPTER_ID if opening_segment else ""),
            )
            for _floor_warning in _floor_warnings:
                quality_warnings.append(_floor_warning)
            if any(_item.startswith("chapter_beat_floor_merged:") for _item in _floor_warnings):
                quality_warnings.append("chapter_beat_floor_after_opening_applied")
        except Exception as _floor_exc:  # noqa: BLE001 - 复检失败绝不阻断导演
            quality_warnings.append(
                "single_pass_director_chapter_beat_floor_failed:%s" % type(_floor_exc).__name__
            )

    # P0-3①（2026-10-03 David 定）：章尾半句收口挪到管线最后。
    # 前面的裁剪 / 补行 / 开场成段 / 章级配额都可能制造出新的悬空章尾；这里在全部
    # 改动定型后再判一次收口，能补就补到收口，补不动原样保留并留痕。
    if beats and CHAPTER_TAIL_SETTLE_ENABLED:
        try:
            _tail_candidates = {
                int(item.candidate_id): item for item in candidate_by_subtitle.values()
            }
            beats, _tail_added, _tail_warnings = _settle_chapter_tails(
                beats,
                _tail_candidates,
                candidate_by_subtitle,
                opening_chapter_ids=(OPENING_SEGMENT_CHAPTER_ID, "OPENING"),
            )
            if _tail_added:
                _known_candidates = {candidate.candidate_id for candidate in selected}
                for _tail_candidate in _tail_added:
                    if _tail_candidate.candidate_id not in _known_candidates:
                        selected.append(_tail_candidate)
                        _known_candidates.add(_tail_candidate.candidate_id)
            for _tail_warning in _tail_warnings:
                quality_warnings.append(_tail_warning)
        except Exception as _tail_exc:  # noqa: BLE001 - 收口失败绝不阻断导演
            quality_warnings.append(
                "single_pass_director_chapter_tail_settle_failed:%s" % type(_tail_exc).__name__
            )

    # 修法 C（2026-10-05 David 定）：章内一致性告警（只告警，不删不改不重排）。
    # 时点：最后一道收口（章级配额 / 章尾半句）之后，此时章分组已经是最终形态。
    if beats:
        try:
            quality_warnings.extend(_chapter_topic_consistency_warnings(
                beats,
                {int(item.candidate_id): item for item in candidate_by_subtitle.values()},
                {
                    str(getattr(chapter, "chapter_id", "") or ""): " ".join(
                        part for part in (
                            str(getattr(chapter, "title", "") or ""),
                            str(getattr(chapter, "purpose", "") or ""),
                            str(getattr(chapter, "new_buyer_knowledge", "") or ""),
                        ) if part
                    )
                    for chapter in (getattr(strategy, "director_chapter_packets", ()) or ())
                },
            ))
        except Exception as _topic_exc:  # noqa: BLE001 - 一致性告警失败绝不阻断导演
            quality_warnings.append(
                "single_pass_director_chapter_topic_check_failed:%s" % type(_topic_exc).__name__
            )

    selected_seconds = round(sum(candidate.duration for candidate in selected), 3)
    verified_readthrough = " ".join(str(candidate.text or "").strip() for candidate in selected).strip()
    verified_chapter_readthroughs: dict[str, str] = {}
    for chapter in strategy.director_chapter_packets:
        actual_readthrough = "｜".join(
            item for item in verified_chapter_parts.get(chapter.chapter_id, []) if item
        )
        verified_chapter_readthroughs[chapter.chapter_id] = actual_readthrough
        declared_readthrough = str(getattr(chapter, "chapter_readthrough", "") or "").strip()
        if declared_readthrough and _verbatim_key(declared_readthrough) != _verbatim_key(actual_readthrough):
            quality_warnings.append(
                f"single_pass_director_chapter_readthrough_mismatch:{chapter.chapter_id}"
            )
        if enforce_journey_contract and director.get("two_pass_director_packet"):
            if not declared_readthrough:
                quality_warnings.append(
                    f"single_pass_director_chapter_readthrough_missing:{chapter.chapter_id}"
                )
            if str(getattr(chapter, "continuity_status", "") or "").strip().lower() != "pass":
                quality_warnings.append(
                    f"single_pass_director_chapter_continuity_not_passed:{chapter.chapter_id}"
                )
    if opening_segment:
        verified_chapter_readthroughs[OPENING_SEGMENT_CHAPTER_ID] = "｜".join(
            str(candidate.text or "").strip()
            for candidate in selected
            if int(getattr(candidate, "candidate_id", 0) or 0)
            in {int(item) for item in opening_segment.get("opening_candidate_ids") or ()}
            if str(candidate.text or "").strip()
        )
    if (
        str(getattr(strategy, "director_readthrough", "") or "").strip()
        and _verbatim_key(strategy.director_readthrough) != _verbatim_key(verified_readthrough)
    ):
        quality_warnings.append("single_pass_director_final_readthrough_mismatch")
    journey_question_ids = tuple(dict.fromkeys(
        str(beat.commerce_beat_id or "").strip()
        for beat in beats
        if str(beat.commerce_beat_id or "").strip()
    ))
    has_opening = len(beats) >= 2
    if len(beats) < 2:
        quality_warnings.append("single_pass_director_requires_opening_and_payoff")
    elif enforce_journey_contract and len(journey_question_ids) < 2:
        quality_warnings.append("single_pass_director_purchase_journey_too_thin")
    declared_quality_tier = str(strategy.director_quality_tier or "standard").strip().lower()
    # Whole Video Audit is authored by the same AI call.  Program authority is
    # strictly limited to checking its declared counts against the already
    # selected source IDs and timestamps; it does not judge a sentence or add
    # an alternative.  A mismatch stays visible in the audit rather than
    # withholding a source-grounded review render.
    if strategy.director_chapter_packets:
        audit = dict(strategy.whole_video_audit or {})
        audit_status = str(audit.get("status") or "").strip().lower()
        try:
            audit_count = int(audit.get("selected_beat_count"))
        except (TypeError, ValueError):
            audit_count = -1
        try:
            audit_subtitle_count = int(audit.get("selected_subtitle_id_count"))
        except (TypeError, ValueError):
            audit_subtitle_count = -1
        try:
            audit_seconds = float(audit.get("estimated_source_seconds"))
        except (TypeError, ValueError):
            audit_seconds = -1.0
        audit_issues: list[str] = []
        verified_subtitle_count = len({
            int(subtitle_id)
            for packet in sequence
            for subtitle_id in packet.subtitle_ids
            if int(subtitle_id) > 0
        })
        # Historical responses may contain an AI-authored audit.  Verify it
        # read-only.  New compact responses omit self-scoring entirely.
        if audit:
            if "selected_beat_count" in audit and audit_count != len(sequence):
                audit_issues.append("beat_count_mismatch")
            if (
                "selected_subtitle_id_count" in audit
                and audit_subtitle_count != verified_subtitle_count
            ):
                audit_issues.append("subtitle_id_count_mismatch")
            if (
                "estimated_source_seconds" in audit
                and (audit_seconds < 0 or abs(audit_seconds - selected_seconds) > 1.5)
            ):
                audit_issues.append("duration_mismatch")
            if selected_seconds < 30.0 and audit_status not in {
                "short_source", "natural_complete_below_target", "source_material_limited",
            }:
                audit_issues.append("short_source_status_expected")
            if selected_seconds < 30.0 and declared_quality_tier != "basic":
                audit_issues.append("basic_tier_expected")
        core_question_beat_count = sum(
            1 for beat in beats
            if str(beat.commerce_beat_id or "").strip().upper() in {"Q1", "Q2"}
        )
        if core_question_beat_count > 3:
            # This is a read-only Whole Video Audit finding.  It never removes
            # a Director-selected source beat or blocks a review MP4; the UI
            # can expose it so a user may delete/revise the repetitive core.
            audit_issues.append("core_question_overconcentration")
        whole_video_audit_verification = {
            "mode": "program_read_only_verification",
            "ai_self_audit_present": bool(audit),
            "declared_status": audit_status,
            "declared_beat_count": audit.get("selected_beat_count"),
            "declared_subtitle_id_count": audit.get("selected_subtitle_id_count"),
            "declared_seconds": audit.get("estimated_source_seconds"),
            "verified_beat_count": len(sequence),
            "verified_subtitle_id_count": verified_subtitle_count,
            "verified_seconds": selected_seconds,
            "verified_q1_q2_beat_count": core_question_beat_count,
            "verified_chapter_readthroughs": dict(verified_chapter_readthroughs),
            "passed": not audit_issues,
            "issues": audit_issues,
            "verified_readthrough": verified_readthrough,
        }
        quality_warnings.extend(
            f"single_pass_director_whole_video_audit:{item}"
            for item in audit_issues
        )
    # 30–120 seconds is the normal delivery range.  Target duration is soft:
    # a source-grounded shorter draft remains materializable for human review
    # and manual editing.  No program may withhold an MP4 merely because the
    # AI called a short story "strong" or mis-added its source timestamps.
    if beats and any(not candidate.hook_eligible for candidate in selected[:len(beats[0].candidate_ids)]):
        quality_warnings.append("single_pass_director_hook_ineligible")
    opening = None
    if has_opening:
        # 开场包的 Hook 必须就是片头的那一拍；若首拍因任何原因不是 hook，才回退到契约
        # 回执声明的首个 ID（而不是默认把一句普通 result 当成开场）。
        opening_hook_ids = tuple(beats[0].candidate_ids)
        if str(getattr(beats[0], "narrative_role", "") or "").strip().lower() != "hook":
            _receipt_hook_ids = _opening_receipt_candidate_ids(strategy, candidate_by_subtitle)[:1]
            if _receipt_hook_ids:
                opening_hook_ids = _receipt_hook_ids
                quality_warnings.append(
                    "single_pass_director_opening_not_hook_beat:receipt_hook_applied"
                )
            else:
                quality_warnings.append("single_pass_director_opening_not_hook_beat")
        opening = OpeningPackage(
            promise=strategy.opening_promise or strategy.core_desire or strategy.core_commercial_idea or strategy.thesis,
            payoff_relation=str(beats[1].goal or "第二段真实口播承接开场承诺"),
            hook_candidate_ids=opening_hook_ids,
            payoff_candidate_ids=beats[1].candidate_ids,
            selection_instruction="declared_by_single_ai_director_packet",
            hook_promise=strategy.opening_promise or strategy.core_desire or strategy.thesis,
            payoff_delivery=str(beats[1].goal or "真实口播即时兑现"),
            connection_reason=str(getattr(sequence[1], "why_this_follows", "") or beats[1].goal),
            hook_integrity_reason="AI selected this complete source utterance as the opening package.",
        )

    final_brief = {
        "authority": "director_narrative_contract",
        "director_title": strategy.director_title or strategy.core_commercial_idea or strategy.thesis,
        "core_desire": strategy.core_desire or strategy.core_commercial_idea or strategy.thesis,
        "opening_promise": strategy.opening_promise or strategy.core_desire or strategy.thesis,
        "quality_tier": declared_quality_tier,
        "video_structure": {
            "id": strategy.video_structure_id or strategy.narrative_archetype or "director_defined",
            "name": strategy.video_structure_name or strategy.narrative_archetype or "导演自定义结构",
            "selection_reason": strategy.video_structure_reason,
        },
        "source": "single_ai_director_packet",
    }
    _director_chapter_rows = [item.to_dict() for item in strategy.director_chapter_packets]
    if opening_segment:
        # 预览 / 导出按 chapter_id 分组：独立开场段必须有自己的章节记录，
        # 否则它只会显示成第一章的一部分。
        _director_chapter_rows = [{
            "chapter_id": OPENING_SEGMENT_CHAPTER_ID,
            "chapter_kind": "hook",
            "title": OPENING_SEGMENT_TITLE,
            "purpose": "AI 声明的开场钩子固定成独立一段，排在片头",
            "structure_slot": "opening",
            "coverage": "recommended",
            "buyer_advance": "开场即圈住目标用户",
            "selection_instruction": "opening_segment_declared",
            "opening_segment": True,
        }] + _director_chapter_rows
    contract.update({
        "single_ai_director_packet": True,
        "m1_source_story": CommercialStoryBrief.from_strategy(strategy).payload(),
        "director_narrative_contract": {
            **director,
            "core_desire": final_brief["core_desire"],
            "opening_promise": final_brief["opening_promise"],
            "video_structure": dict(final_brief["video_structure"]),
            "director_sequence": [item.to_dict() for item in sequence],
            "chapter_packets": list(_director_chapter_rows),
            "opening_alternative_packages": [
                item.to_dict() for item in strategy.director_opening_alternatives
            ],
            "whole_video_audit": dict(strategy.whole_video_audit or {}),
            "product_scope": dict(strategy.product_scope or {}),
            "opening_selection": dict(strategy.opening_selection or {}),
            "final_readthrough": str(getattr(strategy, "director_readthrough", "") or ""),
        },
        "single_ai_director_journey": [item.to_dict() for item in sequence],
        "director_chapter_packets": list(_director_chapter_rows),
        "director_opening_alternative_packages": [
            item.to_dict() for item in strategy.director_opening_alternatives
        ],
        "director_video_structure": dict(final_brief["video_structure"]),
        "director_whole_video_audit": dict(strategy.whole_video_audit or {}),
        "director_whole_video_audit_verification": whole_video_audit_verification,
        "director_verified_readthrough": verified_readthrough,
        "director_verified_chapter_readthroughs": dict(verified_chapter_readthroughs),
        "director_selection_pool": "complete_safe_executable_candidates",
        "strong_clip_ranking_used_before_journey": False,
        "director_journey_question_ids": list(journey_question_ids),
        "director_opening_scope": {
            "allowed_purchase_question_ids": list(opening_question_ids),
            "allowed_answer_roles": list(opening_answer_roles),
        },
        "director_opening_fallbacks_used": opening_fallbacks_used,
        "director_semantic_mutation_disabled": True,
        "director_quality_warnings": list(dict.fromkeys(quality_warnings)),
        "director_hard_execution_issues": list(issues),
        "final_story_brief": final_brief,
        # This is not the historical Commerce Lite multi-pass plan.  Keeping
        # the flag false prevents legacy Q-completion/budget heuristics from
        # reinterpreting the AI-authored director order.
        "commerce_lite_purchase_value_progression": False,
        "m1_consumption_validation_require_supporting_bridge": False,
    })
    # A source-grounded AI plan is editable even when its story is short,
    # repetitive, or has imperfect relationship metadata.  Only technical
    # source failures may prevent M3.
    plan_valid = not issues and bool(beats)
    journey_complete = len(journey_question_ids) >= 3
    raw_duration_range = contract.get("target_duration_range")
    duration_range = (
        dict(raw_duration_range)
        if isinstance(raw_duration_range, Mapping)
        else director_target_duration_range(
            target_duration,
            contract.get("duration_tolerance"),
        )
    )
    requested_seconds = float(duration_range.get("requested_seconds") or target_duration or 45.0)
    preferred_low = float(duration_range.get("preferred_low") or max(30.0, requested_seconds * 0.80))
    preferred_high = float(duration_range.get("preferred_high") or min(120.0, requested_seconds * 1.10))
    speed_factor = float(duration_range.get("speed_factor") or 1.0)
    duration_control = dict(strategy.whole_video_audit or {}).get("duration_control") or {}
    final_audit = dict(duration_control.get("final") or {})
    # Judge delivery on unique source seconds so a repeated playback can never be
    # reported as delivered length, and report quality warnings separately
    # instead of silently flipping an in-range delivery into "not met".
    unique_source_seconds = final_audit.get("unique_source_seconds")
    if isinstance(unique_source_seconds, (int, float)) and float(unique_source_seconds) > 0:
        selected_seconds = float(unique_source_seconds)
    projected_seconds = selected_seconds / speed_factor
    margin = float(duration_range.get("acceptance_margin") or 0.0)
    duration_fulfilled = plan_valid and preferred_low - margin <= projected_seconds <= preferred_high + margin
    duplicate_source_ids = list(final_audit.get("duplicate_subtitle_ids") or [])
    duration_status = (
        "duplicate_source_needs_review" if duplicate_source_ids else
        "target_range_fulfilled"
        if duration_fulfilled
        else "natural_shortfall_below_requested_duration"
        if plan_valid and (journey_complete or duration_control) and projected_seconds < preferred_low
        else "natural_complete_above_requested_duration"
        if plan_valid and journey_complete
        else "short_draft_below_normal_duration" if plan_valid
        else "director_packet_invalid"
    )
    duration_assessment = {
        "status": duration_status,
        "reason": (
            "AI 导演短句 Casting 已按真实可执行字幕物化；程序没有自行选句，实际时长按目标区间如实标记。"
            if journey_complete else
            "AI 导演短句 Casting 给出了可预览的短草案，但购买链仍偏薄；保留给人工比较和编辑，不伪装成完成目标时长的商品片。"
        ),
        "duration_note": (
            f"原声 {selected_seconds:.1f}s，预计成片 {projected_seconds:.1f}s；本次目标 {requested_seconds:.0f}s，"
            f"验收区间 {preferred_low:.1f}–{preferred_high:.1f}s。预览仍允许，以便人工比较和手动编辑。"
        ),
        "actual_seconds": selected_seconds,
        "projected_final_seconds": round(projected_seconds, 3),
        "output_speed_factor": speed_factor,
        "target_range_fulfilled": duration_fulfilled,
        "duplicate_source_ids": duplicate_source_ids,
        "duration_control": duration_control,
        "requested_seconds": requested_seconds,
        "preferred_low": preferred_low,
        "preferred_high": preferred_high,
        "m3_render_gate": {
            "passed": plan_valid,
            "reason": (
                "single_ai_director_packet_source_lineage_resolved"
                if plan_valid else "single_ai_director_packet_source_lineage_unresolved"
            ),
            "issues": list(issues),
        },
        "quality_warnings": list(dict.fromkeys(quality_warnings)),
    }
    consumption = StoryConsumption(
        hero_strategy_id=strategy.strategy_id,
        hero_priority=strategy.story_priority,
        hero_consistency_reason="同一次 AI 导演结果定义成片核心购买欲望与真实口播顺序。",
        supporting_chapter_ids=(),
        bridge_chapter_ids=(),
        no_rediscovery=True,
    )
    return NarrativePlan(
        strategy_id=strategy.strategy_id,
        thesis=strategy.core_desire or strategy.core_commercial_idea or strategy.thesis,
        target_duration=float(target_duration),
        beats=tuple(beats),
        status=(
            "director_ready" if plan_valid and duration_status == "target_range_fulfilled"
            else "director_short_draft" if plan_valid
            else "director_packet_invalid"
        ),
        recommended_duration=selected_seconds,
        issues=tuple(issues),
        removed_beats=(),
        plan_valid=plan_valid,
        story_brief=CommercialStoryBrief.from_strategy(strategy),
        opening_package=opening,
        selection_contract=contract,
        selected_candidates=tuple(selected),
        replan_request=(
            None if plan_valid else ReplanRequest(
                reason_codes=tuple(issues) or ("single_pass_director_packet_invalid",),
                detail=("The one AI director packet could not be bound to executable source.",),
            )
        ),
        duration_assessment=duration_assessment,
        story_consumption=consumption,
        # 子跨度派生候选的词切片必须随计划交回：按整行匹配的 binder 无法为
        # 它们重建血缘。没有派生候选时是空 dict，行为与改动前一致。
        derived_candidate_words=dict(derived_candidate_words),
        derived_candidate_ledger=tuple(derived_candidate_ledger),
    )
