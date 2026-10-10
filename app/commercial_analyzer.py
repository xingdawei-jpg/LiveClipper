# -*- coding: utf-8 -*-
"""Commercial Story Analyzer（Strategy Discovery）— P0-B / M1

从【完整直播字幕】中发现 1~N 个可成立的商业导演方案。

评分原则（David 定调）：
- LLM 只输出【证据事实与商业故事结构】，不输出总分。
- story_validity（故事是否成立）与 duration_feasibility（是否撑得起本次时长）必须分开。
- content_dependencies（故事依赖的敏感内容）始终识别；合同只决定本次是否可用。
- M1 发现可调动的商业资产，不预先决定 M2 的章节顺序或具体剪辑时间。

证据角色 taxonomy：hook / problem / mechanism / proof / benefit / result / scene / trust / close
内容合同规则类型：price / cta / inventory / social_proof / after_sales / size / interaction
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from ai_model_config import ai_chat_completions_url
from ai_cost_ledger import record_ai_call
from director_opening_audit import OPENING_RECEIPT_VERSION, audit_opening_candidates
from director_wire_schema import WIRE_VERSION, compact_director_wire_payload, expand_director_wire_payload
from ssl_context import create_ssl_context
from director_product_contract import (
    build_product_target, scope_errors, audit_product_selection, compatible, foreign_product_ranges, PRODUCT_TYPES,
)
from selection_safety import live_interaction_or_size_response_reason, pitch_talk_reason


EVIDENCE_ROLES = (
    "hook", "problem", "mechanism", "proof", "benefit", "result", "scene", "trust", "close",
)

# The one-call Director packet carries the buyer relationship explicitly.  It
# is not a second classifier and it never picks a source on the program's
# behalf: the model declares these fields together with the source subtitle
# IDs it chose from the complete hard-safe transcript.
DIRECTOR_PURCHASE_QUESTIONS = {
    "Q1": "我为什么想买？",
    "Q2": "为什么真的有效？",
    "Q3": "我这种身材能不能穿？",
    "Q4": "夏天穿着舒服吗？",
    "Q5": "穿着有没有风险或顾虑？",
    "Q6": "日常怎么穿、怎么搭？",
    "Q7": "有什么可信的品质或信任理由？",
}
DIRECTOR_ANSWER_ROLES = (
    # ``answer_role`` describes what the spoken span does for the buyer.  A
    # one-call Director sometimes uses the neighbouring narrative vocabulary
    # (hook/problem) or the existing content-contract name
    # (size_interaction).  They are still explicit AI-authored relationships,
    # not program-derived semantics, so keep those known aliases valid instead
    # of invalidating the complete film after the paid call has finished.
    "hook", "problem", "result", "mechanism", "proof", "benefit",
    "risk_remove", "comfort", "scene", "styling", "size_interaction", "trust", "close",
)
DIRECTOR_COVERAGE = ("required", "recommended", "optional")
DIRECTOR_CHAPTER_KIND_TO_QUESTION = {
    "pain": "Q1",
    "result": "Q1",
    "mechanism": "Q2",
    "proof": "Q2",
    "fit": "Q3",
    "comfort": "Q4",
    "risk": "Q5",
    "styling": "Q6",
    "scene": "Q6",
    "trust": "Q7",
}
DIRECTOR_CHAPTER_KIND_TO_ANSWER_ROLE = {
    "pain": "problem",
    "result": "result",
    "mechanism": "mechanism",
    "proof": "proof",
    "fit": "size_interaction",
    "comfort": "comfort",
    "risk": "risk_remove",
    "styling": "styling",
    "scene": "scene",
    "trust": "trust",
}

# 内容合同可能覆盖的规则类型（策略证据如果依赖了这些，且合同 forbid，则算被禁）
CONTENT_RULE_TYPES = (
    "price",         # 价格
    "cta",           # 促单/强转化（点关注、加一波、上车、早拍早飞）
    "inventory",     # 库存/数量/限量
    "social_proof",  # 口碑/老粉/成交率/已卖多少件
    "after_sales",   # 售后/包退换/发货
    "size",          # 尺码
    "interaction",   # 互动请求（扣1、扣身高体重）
)

_STORY_ROLE_WEIGHTS = {
    "hook": 0.20,
    "problem": 0.15,
    "mechanism": 0.15,
    "proof": 0.15,
    "benefit": 0.10,
    "result": 0.10,
    "scene": 0.10,
    "close": 0.05,
}


# ──────────────────────────────────────────────────────────────
# 契约 dataclass
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EvidenceItem:
    role: str
    claim: str
    subtitle_ids: tuple[int, ...]
    asset_tier: str = "core"       # core | supporting | bridge
    evidence_basis: str = "explicit"  # explicit | interpretive

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvidenceItem":
        ids = raw.get("subtitle_ids") or raw.get("subtitle_id") or ()
        if isinstance(ids, (int, float, str)):
            ids = (ids,)
        normalized: list[int] = []
        for sid in ids:
            try:
                normalized.append(int(sid))
            except (TypeError, ValueError):
                pass
        asset_tier = str(
            raw.get("asset_tier") or raw.get("evidence_tier") or raw.get("tier") or "core"
        ).strip().lower()
        if asset_tier not in {"core", "supporting", "bridge"}:
            asset_tier = "core"
        evidence_basis = str(raw.get("evidence_basis") or raw.get("basis") or "explicit").strip().lower()
        if evidence_basis not in {"explicit", "interpretive"}:
            evidence_basis = "explicit"
        return cls(
            role=str(raw.get("role") or "").strip().lower(),
            claim=str(raw.get("claim") or "").strip(),
            subtitle_ids=tuple(normalized),
            asset_tier=asset_tier,
            evidence_basis=evidence_basis,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "claim": self.claim,
            "subtitle_ids": list(self.subtitle_ids),
            "asset_tier": self.asset_tier,
            "evidence_basis": self.evidence_basis,
        }


@dataclass(frozen=True)
class ContractAuditHit:
    """硬审计命中追踪：某个 block 类型具体由哪条字幕、哪个关键词触发。"""
    type: str
    subtitle_id: int
    raw_text: str
    matched_keyword: str
    evidence_role: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "subtitle_id": self.subtitle_id,
            "raw_text": self.raw_text,
            "matched_keyword": self.matched_keyword,
            "evidence_role": self.evidence_role,
        }


def _str_list(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = (raw,)
    return tuple(str(x).strip() for x in (raw or ()) if str(x).strip())


# These are deliberately narrow surface-form failures, not a semantic ranking
# system.  They catch an ASR artefact or dangling live-stream residue that a
# human cannot reasonably publish as a standalone spoken beat.  The director
# still chooses among every remaining source utterance.
_FINAL_UTTERANCE_SURFACE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^[的得地][，,。.!！?？]", "dangling_function_word_opening"),
    (r"(?:^|[，,。.!！?？])\s*下0天", "asr_zero_day_anomaly"),
    (r"人间一定是直角", "asr_semantic_anomaly"),
    (r"母婴店[^。！？!?]{0,12}就是你", "unresolved_listener_reference"),
    (r"A类母婴店", "asr_material_claim_anomaly"),
    (r"头疼个啥", "asr_semantic_anomaly"),
    (r"是那个35厘米", "asr_number_anomaly"),
    (r"自带3(?:到|-)?5厘米的销售", "asr_number_anomaly"),
    (r"像100斤葡萄", "asr_delivery_anomaly"),
    (r"^好的[，,]来", "live_delivery_leadin"),
    # A final publishable utterance may end in a spoken comma; punctuation is
    # not a completeness signal.  These three forms are instead high-
    # confidence semantic failures: an unanswered question, a merged
    # conjunction, or a claim cut off directly after a negation.
    (r"(?:会显得|显得).{0,12}(?:干嘛|什么(?:呢|啊|呀)?)[？?。！!，,\s]*$", "unresolved_rhetorical_tail"),
    (r"^(?:是但是|而不是说是)", "asr_connective_anomaly"),
    (r"(?:而且|但是).{0,32}(?:上身|穿上).{0,12}(?:完全不|都不|不会不)[。！？!?，,\s]*$", "dangling_purchase_claim"),
    (r"^它是大是(?:显瘦|好看|舒服)", "asr_predicate_anomaly"),
    (r"值得等[，,]?\s*因为你收到你", "asr_truncated_delivery_claim"),
    (r"[。！？!?]\s*的(?:对|是|啊)", "asr_internal_fragment"),
)


def final_utterance_surface_issue(text: str) -> str:
    """Return a deterministic publishability issue for obvious malformed text."""
    normalized = str(text or "").strip()
    if not normalized:
        return "empty_utterance"
    for pattern, reason in _FINAL_UTTERANCE_SURFACE_PATTERNS:
        if re.search(pattern, normalized):
            return reason
    return ""


def director_opening_input_issue(text: str) -> str:
    """Return a narrow advisory flag for an opening-dependent spoken window."""
    normalized = str(text or "").strip()
    if re.match(r"^(?:你可能会说|你相信我|你看|好的[，,]?来|那个|其实|然后|因为|所以|的确)", normalized):
        return "live_or_dependent_leadin"
    if re.match(r"^你是.{0,24}(?:肩|斜方肌|胖|肉|身材)", normalized):
        return "pain_statement_not_result"
    if re.search(r"(?:从|在)(?:这儿|这里).{0,24}(?:到|变成)(?:这儿|这里)", normalized):
        return "visual_dependent"
    return ""


def _optional_word_offset(value: Any) -> int | None:
    """解析一个可选的词偏移。

    只接受非负整数（bool 不算）。其它任何输入（含负数、浮点、字符串垃圾、
    ``None``、空串）都返回 ``None`` —— 即"整条候选"。

    这是刻意的**安全降级方向**：偏移不合法时退回内容更多的整条候选，
    而不是猜一个更小的跨度。切片后仍会过播放闸门，不会因此放进半句话。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and float(value).is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None
    return None


@dataclass(frozen=True)
class DirectorBeat:
    """One AI-authored source beat in the final director packet.

    This is deliberately an instruction-level relationship only.  It does not
    carry rewritten speech, timestamps, a score, or a program-selected
    candidate.  The single director call names subtitle IDs; the existing M3
    path later proves their word-level lineage and materializes them exactly.
    """

    beat_id: str
    role: str
    goal: str
    subtitle_ids: tuple[int, ...]
    why_this_follows: str = ""
    # ``role``/``goal`` remain legacy display fields.  These six fields are
    # the executable purchase-journey contract for the one AI Director call.
    # Defaults keep historical M1 records readable; the single-pass executor
    # rejects an incomplete new packet instead of inventing a relationship.
    purchase_question_id: str = ""
    purchase_question: str = ""
    answer_role: str = ""
    supports_question_id: str = ""
    purchase_outcome: str = ""
    coverage: str = "recommended"
    # Historical opening-backup data remains readable, but the current
    # single-pass executor never activates it or substitutes an opening.
    opening_fallback_subtitle_ids: tuple[int, ...] = ()
    # The Director must copy the selected source span here.  M3 still binds by
    # subtitle IDs; this field exists to make the model confront the actual
    # spoken words instead of judging only its own polished chapter labels.
    verbatim: str = ""
    # Casting may nominate a bounded replacement for a selected Beat.  It is
    # exposed only to the human preview workbench and is never flattened into
    # ``director_sequence`` or auto-inserted by the program.
    replaces_beat_id: str = ""
    # ── 子跨度（可选）────────────────────────────────────────────────────
    # 允许导演在已选源跨度内**再收窄首/末边界**：只取某行的中间一段，
    # 用于「整句开头结尾都不好、只有中间好」的情况。
    #
    # 语义与 P0.5A 的词偏移一致（micro_beat_inventory.py），不发明新概念：
    #   start_word_offset -> 相对 span 的**首行**，从第几个词开始（0-based，含）
    #   end_word_offset   -> 相对 span 的**末行**，到第几个词结束（0-based，含）
    # 单行 span 时两者作用于同一行。
    #
    # **两者都为 None 时完全等于既有行为**（整条候选），因此历史报文与
    # 所有不填偏移的调用方逐字节不变。
    #
    # 程序只做两件事：按词切片（词级 exact，不改写文字）+ 用播放闸门校验
    # 切出来的跨度能否独立播放。校验不过就**退回整条候选**，不静默修补。
    start_word_offset: int | None = None
    end_word_offset: int | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "DirectorBeat":
        # New one-call packets select an inclusive source range.  This makes
        # the AI's semantic decision explicit at both boundaries while the
        # program performs only the mechanical expansion; it prevents arrays
        # such as [51, 198, 199] from pretending to be one continuous utterance.
        raw_span = raw.get("source_span") or raw.get("subtitle_span") or {}
        span_ids: tuple[int, ...] = ()
        if isinstance(raw_span, Mapping):
            try:
                span_start = int(raw_span.get("start_id") or raw_span.get("start_subtitle_id") or 0)
                span_end = int(raw_span.get("end_id") or raw_span.get("end_subtitle_id") or span_start)
            except (TypeError, ValueError):
                span_start = span_end = 0
            if 0 < span_start <= span_end and span_end - span_start <= 1000:
                span_ids = tuple(range(span_start, span_end + 1))
        raw_ids = span_ids or raw.get("subtitle_ids") or raw.get("candidate_subtitle_ids") or ()
        if isinstance(raw_ids, (str, int)):
            raw_ids = (raw_ids,)
        ids: list[int] = []
        for value in raw_ids or ():
            try:
                subtitle_id = int(value)
            except (TypeError, ValueError):
                continue
            if subtitle_id > 0 and subtitle_id not in ids:
                ids.append(subtitle_id)
        raw_fallback_ids = raw.get("opening_fallback_subtitle_ids") or raw.get("opening_backup_subtitle_ids") or ()
        if isinstance(raw_fallback_ids, (str, int)):
            raw_fallback_ids = (raw_fallback_ids,)
        fallback_ids: list[int] = []
        for value in raw_fallback_ids or ():
            try:
                subtitle_id = int(value)
            except (TypeError, ValueError):
                continue
            if subtitle_id > 0 and subtitle_id not in fallback_ids:
                fallback_ids.append(subtitle_id)
        return cls(
            beat_id=str(raw.get("beat_id") or f"B{index}").strip(),
            role=str(raw.get("role") or raw.get("beat_function") or "purchase_progress").strip(),
            goal=str(raw.get("goal") or raw.get("beat_advance") or "").strip(),
            subtitle_ids=tuple(ids),
            why_this_follows=str(raw.get("why_this_follows") or raw.get("transition_reason") or "").strip(),
            purchase_question_id=str(raw.get("purchase_question_id") or raw.get("question_id") or "").strip().upper(),
            purchase_question=str(raw.get("purchase_question") or raw.get("question") or raw.get("goal") or "").strip(),
            answer_role=str(
                raw.get("answer_role") or raw.get("beat_function") or raw.get("role") or ""
            ).strip().lower(),
            supports_question_id=str(raw.get("supports_question_id") or "").strip().upper(),
            purchase_outcome=str(raw.get("purchase_outcome") or raw.get("outcome") or "").strip(),
            coverage=str(raw.get("coverage") or "recommended").strip().lower(),
            opening_fallback_subtitle_ids=tuple(fallback_ids),
            verbatim=str(raw.get("verbatim") or raw.get("spoken_text") or raw.get("source_quote") or "").strip(),
            replaces_beat_id=str(
                raw.get("replaces_beat_id") or raw.get("replacement_for_beat_id") or ""
            ).strip(),
            # 子跨度偏移。非法值一律降级为 None（= 整条候选），因为那是**内容更多**的
            # 安全方向；播放闸门仍会校验整条候选，不会因降级而放进半句话。
            start_word_offset=_optional_word_offset(
                raw.get("start_word_offset", raw.get("head_word_offset"))
            ),
            end_word_offset=_optional_word_offset(
                raw.get("end_word_offset", raw.get("tail_word_offset"))
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        source_span = (
            {"start_id": self.subtitle_ids[0], "end_id": self.subtitle_ids[-1]}
            if self.subtitle_ids else {}
        )
        payload = {
            "beat_id": self.beat_id,
            "role": self.role,
            "goal": self.goal,
            "subtitle_ids": list(self.subtitle_ids),
            "source_span": source_span,
            "why_this_follows": self.why_this_follows,
            "purchase_question_id": self.purchase_question_id,
            "purchase_question": self.purchase_question,
            "answer_role": self.answer_role,
            "supports_question_id": self.supports_question_id,
            "purchase_outcome": self.purchase_outcome,
            "coverage": self.coverage,
            "opening_fallback_subtitle_ids": list(self.opening_fallback_subtitle_ids),
            "verbatim": self.verbatim,
            "replaces_beat_id": self.replaces_beat_id,
        }
        # 只在真的设置了偏移时才写出这两个键：未设置时序列化结果与改动前逐字节一致，
        # 历史报文、审计产物与既有测试都不受影响。
        if self.start_word_offset is not None:
            payload["start_word_offset"] = int(self.start_word_offset)
        if self.end_word_offset is not None:
            payload["end_word_offset"] = int(self.end_word_offset)
        return payload


@dataclass(frozen=True)
class DirectorChapterPacket:
    """One AI-authored micro-narrative chapter in the one-call director packet.

    A packet is deliberately not another planning layer.  It is the Director's
    own grouping of the exact source beats it selected in that same response:
    one buyer-value step, its internal proof/experience progression, and its
    place in the chosen video structure.  The executor later flattens these
    already-authored beats without selecting or reordering anything.
    """

    chapter_id: str
    structure_slot: str
    title: str
    purpose: str
    new_buyer_knowledge: str
    coverage: str
    beats: tuple[DirectorBeat, ...]
    # Compact one-call contract: buyer semantics are declared once per
    # chapter, then mechanically inherited by its real source spans.
    purchase_question_id: str = ""
    buyer_advance: str = ""
    chapter_kind: str = ""
    chapter_readthrough: str = ""
    continuity_status: str = ""
    # At most a few AI-cast, same-chapter alternatives.  They remain outside
    # the final sequence until the operator explicitly adds one in the legacy
    # editable preview.
    alternative_beats: tuple[DirectorBeat, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "DirectorChapterPacket":
        raw_beats = raw.get("beats") or raw.get("director_beats") or raw.get("source_beats") or ()
        if not raw_beats and (
            raw.get("source_span") or raw.get("subtitle_span") or raw.get("subtitle_ids")
        ):
            # New compact responses place one complete spoken window directly
            # on the chapter.  Wrap it mechanically as one legacy Beat.
            raw_beats = (raw,)
        if isinstance(raw_beats, Mapping):
            raw_beats = (raw_beats,)
        chapter_kind = str(raw.get("chapter_kind") or raw.get("kind") or "").strip().lower()
        chapter_question_id = str(
            raw.get("purchase_question_id")
            or raw.get("question_id")
            or DIRECTOR_CHAPTER_KIND_TO_QUESTION.get(chapter_kind, "")
        ).strip().upper()
        chapter_question = str(
            raw.get("purchase_question")
            or DIRECTOR_PURCHASE_QUESTIONS.get(chapter_question_id, "")
        ).strip()
        chapter_title = str(raw.get("title") or raw.get("chapter_title") or "").strip()
        buyer_advance = str(
            raw.get("buyer_advance")
            or raw.get("new_buyer_knowledge")
            or raw.get("buyer_progress")
            or raw.get("new_cognition")
            or chapter_title
        ).strip()
        beats: list[DirectorBeat] = []
        for beat_index, item in enumerate(raw_beats, 1):
            if not isinstance(item, Mapping):
                continue
            normalized = dict(item)
            normalized.setdefault("beat_id", f"C{index}B{beat_index}")
            normalized.setdefault("coverage", raw.get("coverage") or "recommended")
            normalized.setdefault("purchase_question_id", chapter_question_id)
            normalized.setdefault("purchase_question", chapter_question)
            normalized.setdefault("purchase_outcome", buyer_advance)
            normalized.setdefault(
                "goal", normalized.get("beat_advance") or chapter_title or buyer_advance
            )
            beat_function = str(normalized.get("beat_function") or "").strip().lower()
            normalized.setdefault(
                "answer_role",
                beat_function
                or DIRECTOR_CHAPTER_KIND_TO_ANSWER_ROLE.get(chapter_kind, "purchase_progress"),
            )
            normalized.setdefault(
                "role", beat_function or normalized.get("answer_role") or "purchase_progress"
            )
            beats.append(DirectorBeat.from_dict(normalized, len(beats) + 1))
        raw_alternatives = raw.get("alternative_beats") or raw.get("alternate_beats") or ()
        if isinstance(raw_alternatives, Mapping):
            raw_alternatives = (raw_alternatives,)
        alternative_beats: list[DirectorBeat] = []
        for alternative_index, item in enumerate(raw_alternatives, 1):
            if not isinstance(item, Mapping):
                continue
            normalized = dict(item)
            normalized.setdefault("beat_id", f"C{index}A{alternative_index}")
            normalized.setdefault("coverage", raw.get("coverage") or "recommended")
            normalized.setdefault("purchase_question_id", chapter_question_id)
            normalized.setdefault("purchase_question", chapter_question)
            normalized.setdefault("purchase_outcome", buyer_advance)
            normalized.setdefault("goal", chapter_title or buyer_advance)
            beat_function = str(normalized.get("beat_function") or "").strip().lower()
            normalized.setdefault(
                "answer_role",
                beat_function
                or DIRECTOR_CHAPTER_KIND_TO_ANSWER_ROLE.get(chapter_kind, "purchase_progress"),
            )
            normalized.setdefault(
                "role", beat_function or normalized.get("answer_role") or "purchase_progress"
            )
            alternative_beats.append(
                DirectorBeat.from_dict(normalized, len(alternative_beats) + 1)
            )
        return cls(
            chapter_id=str(raw.get("chapter_id") or f"C{index}").strip(),
            structure_slot=str(raw.get("structure_slot") or raw.get("slot") or "").strip(),
            title=chapter_title,
            purpose=str(raw.get("purpose") or raw.get("chapter_purpose") or "").strip(),
            new_buyer_knowledge=buyer_advance,
            coverage=str(raw.get("coverage") or "recommended").strip().lower(),
            beats=tuple(beats),
            purchase_question_id=chapter_question_id,
            buyer_advance=buyer_advance,
            chapter_kind=chapter_kind,
            chapter_readthrough=str(
                raw.get("chapter_readthrough") or raw.get("readthrough") or ""
            ).strip(),
            continuity_status=str(
                raw.get("continuity_status") or raw.get("readthrough_status") or ""
            ).strip().lower(),
            alternative_beats=tuple(alternative_beats[:3]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "structure_slot": self.structure_slot,
            "title": self.title,
            "purpose": self.purpose,
            "new_buyer_knowledge": self.new_buyer_knowledge,
            "coverage": self.coverage,
            "purchase_question_id": self.purchase_question_id,
            "buyer_advance": self.buyer_advance,
            "chapter_kind": self.chapter_kind,
            "chapter_readthrough": self.chapter_readthrough,
            "continuity_status": self.continuity_status,
            "beats": [item.to_dict() for item in self.beats],
            "alternative_beats": [item.to_dict() for item in self.alternative_beats],
        }


@dataclass(frozen=True)
class DirectorOpeningAlternative:
    """One complete AI-ranked replacement for the whole opening chapter.

    Replacing only the first line can pair two unrelated livestream moments.
    An alternative therefore owns every Beat needed to state and immediately
    cash out its promise.  The executor may use it only as an atomic package.
    """

    package_id: str
    title: str
    beats: tuple[DirectorBeat, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "DirectorOpeningAlternative":
        raw_beats = raw.get("beats") or raw.get("opening_beats") or ()
        if isinstance(raw_beats, Mapping):
            raw_beats = (raw_beats,)
        beats: list[DirectorBeat] = []
        for beat_index, item in enumerate(raw_beats, 1):
            if not isinstance(item, Mapping):
                continue
            normalized = dict(item)
            normalized.setdefault("beat_id", f"OP{index}B{beat_index}")
            normalized.setdefault("coverage", "required")
            beats.append(DirectorBeat.from_dict(normalized, beat_index))
        return cls(
            package_id=str(raw.get("package_id") or f"OP{index}").strip(),
            title=str(raw.get("title") or raw.get("name") or "备用开场").strip(),
            beats=tuple(beats),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "title": self.title,
            "beats": [item.to_dict() for item in self.beats],
        }


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    type: str
    strategy_family: str
    sub_angle: str
    thesis: str
    target_user: str
    evidence: tuple[EvidenceItem, ...]
    missing_roles: tuple[str, ...]
    blocked_evidence_types: tuple[str, ...]
    contract_audit_hits: tuple[ContractAuditHit, ...]
    coherence_reason: str
    distinctiveness: str
    story_strength: float
    material_sufficiency: float
    contract_compatibility: float
    strategy_viability: str
    story_premise: str = ""
    audience_tension: str = ""
    story_trigger: str = ""
    transformation: str = ""
    product_role: str = ""
    core_commercial_idea: str = ""
    payoff: str = ""
    supporting_arcs: tuple[str, ...] = ()
    inference_notes: tuple[str, ...] = ()
    content_dependencies: tuple[str, ...] = ()
    core_evidence_pool: tuple[EvidenceItem, ...] = ()
    supporting_evidence_pool: tuple[EvidenceItem, ...] = ()
    bridge_candidates: tuple[EvidenceItem, ...] = ()
    story_validity: str = "limited"
    duration_feasibility: str = "unknown"
    recommended_duration_seconds: float = 0.0
    target_duration_seconds: float = 0.0
    excluded_assets_reason: tuple[str, ...] = ()
    story_priority: str = "medium"
    # P0.5C: this is the one-call director result.  Empty deliberately means
    # a historical M1 record, which remains readable by the legacy harness.
    director_title: str = ""
    core_desire: str = ""
    opening_promise: str = ""
    # M1 feasibility evidence only: these IDs prove that a Hook -> immediate
    # payoff is available.  They are deliberately not a selected cut list.
    opening_evidence_packages: tuple[Mapping[str, Any], ...] = ()
    director_quality_tier: str = ""
    # Exactly one item in a normal one-call response is the fully authored
    # primary plan.  Alternative directions intentionally contain no source
    # order until a user explicitly asks for (and pays for) another call.
    director_plan_role: str = ""
    director_sequence: tuple[DirectorBeat, ...] = ()
    # P0.6: the one-call Director chooses a source-supported video structure
    # and returns packets rather than a flat checklist.  ``director_sequence``
    # remains the flattened, exact-source execution order for M3.
    video_structure_id: str = ""
    video_structure_name: str = ""
    video_structure_reason: str = ""
    director_chapter_packets: tuple[DirectorChapterPacket, ...] = ()
    director_opening_alternatives: tuple[DirectorOpeningAlternative, ...] = ()
    whole_video_audit: Mapping[str, Any] | None = None
    # Compact single-call responses repeat only the selected spoken sequence,
    # not another semantic audit.  It is advisory and never drives M3.
    director_readthrough: str = ""
    # The Director, not a fixed UI mode, defines which buyer questions can
    # legitimately open this particular story.  Empty keeps old records
    # readable and falls back to the conservative Q1 result/proof scope.
    narrative_archetype: str = ""
    opening_scope: Mapping[str, Any] | None = None
    product_scope: Mapping[str, Any] | None = None
    opening_selection: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> "Strategy":
        def parse_pool(raw_pool: Any, tier: str) -> tuple[EvidenceItem, ...]:
            items: list[EvidenceItem] = []
            for item in (raw_pool or ()):
                if not isinstance(item, Mapping):
                    continue
                normalized = dict(item)
                normalized.setdefault("asset_tier", tier)
                items.append(EvidenceItem.from_dict(normalized))
            return tuple(items)

        core_pool = parse_pool(raw.get("core_evidence_pool"), "core")
        supporting_pool = parse_pool(raw.get("supporting_evidence_pool"), "supporting")
        bridge_pool = parse_pool(raw.get("bridge_candidates"), "bridge")
        chapters_raw = raw.get("chapter_packets") or raw.get("director_chapter_packets") or ()
        if isinstance(chapters_raw, Mapping):
            chapters_raw = (chapters_raw,)
        director_chapter_packets = tuple(
            DirectorChapterPacket.from_dict(item, chapter_index)
            for chapter_index, item in enumerate(chapters_raw, 1)
            if isinstance(item, Mapping)
        )
        opening_alternatives_raw = raw.get("opening_alternative_packages") or ()
        if isinstance(opening_alternatives_raw, Mapping):
            opening_alternatives_raw = (opening_alternatives_raw,)
        director_opening_alternatives = tuple(
            DirectorOpeningAlternative.from_dict(item, alternative_index)
            for alternative_index, item in enumerate(opening_alternatives_raw, 1)
            if isinstance(item, Mapping)
        )
        if director_chapter_packets:
            # The flattened order is exactly the packet order and exact beat
            # order authored by the AI.  This is a data conversion, never a
            # program semantic decision or an extra selection pass.
            director_sequence = tuple(
                beat for chapter in director_chapter_packets for beat in chapter.beats
            )
        else:
            sequence_raw = raw.get("director_sequence") or raw.get("purchase_journey") or ()
            if isinstance(sequence_raw, Mapping):
                sequence_raw = (sequence_raw,)
            director_sequence = tuple(
                DirectorBeat.from_dict(item, sequence_index)
                for sequence_index, item in enumerate(sequence_raw, 1)
                if isinstance(item, Mapping)
            )
        raw_structure = raw.get("video_structure")
        structure = dict(raw_structure) if isinstance(raw_structure, Mapping) else {}
        legacy_evidence = parse_pool(raw.get("evidence") or raw.get("evidence_chain"), "core")
        if not (core_pool or supporting_pool or bridge_pool):
            core_pool = legacy_evidence
        if not (core_pool or supporting_pool or bridge_pool) and director_sequence:
            # The compact Director response already names the exact source
            # evidence.  Mirror that AI-authored role/claim/lineage into the
            # historical evidence view so old reports remain useful without
            # asking the model to repeat three evidence-pool inventories.
            core_pool = tuple(
                EvidenceItem(
                    role=str(beat.answer_role or beat.role or "proof").strip().lower(),
                    claim=str(beat.goal or beat.purchase_question or "真实口播章节").strip(),
                    subtitle_ids=tuple(beat.subtitle_ids),
                    asset_tier="core",
                    evidence_basis="explicit",
                )
                for beat in director_sequence if beat.subtitle_ids
            )
        evidence = tuple((*core_pool, *supporting_pool, *bridge_pool))
        director_title = str(raw.get("director_title") or raw.get("title") or "").strip()
        core_desire = str(raw.get("core_desire") or raw.get("core_commercial_idea") or raw.get("thesis") or "").strip()
        opening_promise = str(raw.get("opening_promise") or core_desire).strip()
        opening_evidence_raw = raw.get("opening_evidence_packages") or ()
        if isinstance(opening_evidence_raw, Mapping):
            opening_evidence_raw = (opening_evidence_raw,)
        opening_evidence_packages = tuple(
            dict(item) for item in opening_evidence_raw if isinstance(item, Mapping)
        )
        return cls(
            strategy_id=str(raw.get("strategy_id") or f"S{index}").strip(),
            type=str(raw.get("type") or raw.get("narrative_archetype") or "").strip(),
            strategy_family=str(raw.get("strategy_family") or structure.get("id") or "").strip(),
            sub_angle=str(raw.get("sub_angle") or director_title).strip(),
            thesis=str(raw.get("thesis") or core_desire).strip(),
            story_premise=str(raw.get("story_premise") or "").strip(),
            audience_tension=str(raw.get("audience_tension") or "").strip(),
            story_trigger=str(raw.get("story_trigger") or "").strip(),
            transformation=str(raw.get("transformation") or "").strip(),
            product_role=str(raw.get("product_role") or "").strip(),
            core_commercial_idea=str(raw.get("core_commercial_idea") or core_desire).strip(),
            payoff=str(raw.get("payoff") or opening_promise).strip(),
            supporting_arcs=_str_list(raw.get("supporting_arcs")),
            inference_notes=_str_list(raw.get("inference_notes")),
            content_dependencies=_str_list(raw.get("content_dependencies") or raw.get("sensitive_content_dependencies")),
            core_evidence_pool=core_pool,
            supporting_evidence_pool=supporting_pool,
            bridge_candidates=bridge_pool,
            excluded_assets_reason=_str_list(raw.get("excluded_assets_reason")),
            story_priority=_normalize_story_priority(raw.get("story_priority")),
            director_title=director_title,
            core_desire=core_desire,
            opening_promise=opening_promise,
            opening_evidence_packages=opening_evidence_packages,
            director_quality_tier=str(raw.get("director_quality_tier") or raw.get("quality_tier") or "").strip().lower(),
            director_plan_role=str(raw.get("director_plan_role") or raw.get("plan_role") or "").strip().lower(),
            director_sequence=director_sequence,
            video_structure_id=str(
                raw.get("video_structure_id") or structure.get("id") or raw.get("narrative_archetype") or ""
            ).strip().lower(),
            video_structure_name=str(raw.get("video_structure_name") or structure.get("name") or "").strip(),
            video_structure_reason=str(
                raw.get("video_structure_reason") or structure.get("selection_reason") or ""
            ).strip(),
            director_chapter_packets=director_chapter_packets,
            director_opening_alternatives=director_opening_alternatives,
            whole_video_audit=(
                dict(raw.get("whole_video_audit") or {})
                if isinstance(raw.get("whole_video_audit"), Mapping) else None
            ),
            director_readthrough=str(
                raw.get("final_readthrough") or raw.get("director_readthrough") or ""
            ).strip(),
            narrative_archetype=str(raw.get("narrative_archetype") or "").strip(),
            opening_scope=(
                dict(raw.get("opening_scope") or {})
                if isinstance(raw.get("opening_scope"), Mapping) else None
            ),
            product_scope=dict(raw["product_scope"]) if isinstance(raw.get("product_scope"), Mapping) else None,
            opening_selection=dict(raw["opening_selection"]) if isinstance(raw.get("opening_selection"), Mapping) else None,
            target_user=str(raw.get("target_user") or "").strip(),
            evidence=evidence,
            missing_roles=_str_list(raw.get("missing_roles")),
            blocked_evidence_types=_str_list(raw.get("blocked_evidence_types")),
            contract_audit_hits=(),
            coherence_reason=str(raw.get("coherence_reason") or "").strip(),
            distinctiveness=str(raw.get("distinctiveness") or "medium").strip().lower(),
            story_strength=0.0,
            material_sufficiency=0.0,
            contract_compatibility=1.0,
            strategy_viability="recommended",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "type": self.type,
            "strategy_family": self.strategy_family,
            "sub_angle": self.sub_angle,
            "thesis": self.thesis,
            "story_premise": self.story_premise,
            "audience_tension": self.audience_tension,
            "story_trigger": self.story_trigger,
            "transformation": self.transformation,
            "product_role": self.product_role,
            "core_commercial_idea": self.core_commercial_idea,
            "payoff": self.payoff,
            "supporting_arcs": list(self.supporting_arcs),
            "inference_notes": list(self.inference_notes),
            "target_user": self.target_user,
            "evidence": [item.to_dict() for item in self.evidence],
            "core_evidence_pool": [item.to_dict() for item in self.core_evidence_pool],
            "supporting_evidence_pool": [item.to_dict() for item in self.supporting_evidence_pool],
            "bridge_candidates": [item.to_dict() for item in self.bridge_candidates],
            "excluded_assets_reason": list(self.excluded_assets_reason),
            "story_priority": self.story_priority,
            "content_dependencies": list(self.content_dependencies),
            "missing_roles": list(self.missing_roles),
            "blocked_evidence_types": list(self.blocked_evidence_types),
            "contract_audit_hits": [h.to_dict() for h in self.contract_audit_hits],
            "coherence_reason": self.coherence_reason,
            "distinctiveness": self.distinctiveness,
            "story_strength": self.story_strength,
            "material_sufficiency": self.material_sufficiency,
            "contract_compatibility": self.contract_compatibility,
            "strategy_viability": self.strategy_viability,
            "story_validity": self.story_validity,
            "duration_feasibility": self.duration_feasibility,
            "recommended_duration_seconds": self.recommended_duration_seconds,
            "target_duration_seconds": self.target_duration_seconds,
            "director_title": self.director_title,
            "core_desire": self.core_desire,
            "opening_promise": self.opening_promise,
            "opening_evidence_packages": [dict(item) for item in self.opening_evidence_packages],
            "director_quality_tier": self.director_quality_tier,
            "director_plan_role": self.director_plan_role,
            "director_sequence": [item.to_dict() for item in self.director_sequence],
            "video_structure": {
                "id": self.video_structure_id,
                "name": self.video_structure_name,
                "selection_reason": self.video_structure_reason,
            },
            "chapter_packets": [item.to_dict() for item in self.director_chapter_packets],
            "opening_alternative_packages": [
                item.to_dict() for item in self.director_opening_alternatives
            ],
            "whole_video_audit": dict(self.whole_video_audit or {}),
            "final_readthrough": self.director_readthrough,
            "narrative_archetype": self.narrative_archetype,
            "opening_scope": dict(self.opening_scope or {}),
            "product_scope": dict(self.product_scope or {}),
            "opening_selection": dict(self.opening_selection or {}),
        }


# ──────────────────────────────────────────────────────────────
# 评分（程序计算，非 LLM）
# ──────────────────────────────────────────────────────────────

def compute_story_strength(evidence: Sequence[EvidenceItem], missing_roles: Sequence[str] = ()) -> float:
    roles = {item.role for item in evidence}
    score = sum(weight for role, weight in _STORY_ROLE_WEIGHTS.items() if role in roles)
    score += min(0.10, max(0, len(evidence) - 3) * 0.02)
    missing = {str(r).strip().lower() for r in missing_roles}
    # 只保留「完全没有收益/结果」的惩罚；不再惩罚「缺 mechanism/proof」
    # （scene/versatility/lifestyle 类策略天然没有 mechanism，不该被结构偏见压到 0.1）
    if "benefit" in missing and "result" in missing:
        score -= 0.10
    return round(max(0.0, min(1.0, score)), 3)


def compute_material_sufficiency(
    evidence: Sequence[EvidenceItem],
    subtitle_durations: Mapping[int, float],
    target_duration: float,
) -> float:
    all_ids = {sid for item in evidence for sid in item.subtitle_ids}
    usable_duration = sum(subtitle_durations.get(sid, 0.0) for sid in all_ids)
    target = max(1.0, float(target_duration or 45.0))
    duration_coverage = min(1.0, usable_duration / target)
    count_factor = min(1.0, len(evidence) / 5.0)
    return round(0.75 * duration_coverage + 0.25 * count_factor, 3)


def compute_evidence_duration(
    evidence: Sequence[EvidenceItem],
    subtitle_durations: Mapping[int, float],
) -> float:
    """M1 可引用资产的去重时长；它不是最终剪辑时长。"""
    all_ids = {sid for item in evidence for sid in item.subtitle_ids}
    return round(sum(max(0.0, subtitle_durations.get(sid, 0.0)) for sid in all_ids), 3)


def compute_story_validity(strategy: Strategy) -> str:
    """判断一个商业故事是否成立，不因当前目标时长或合同而降级。"""
    core_assets = strategy.core_evidence_pool or strategy.evidence
    has_idea = bool(strategy.core_commercial_idea or strategy.thesis)
    has_change = bool(strategy.transformation or strategy.payoff)
    has_tension_or_trigger = bool(strategy.audience_tension or strategy.story_trigger)
    if has_idea and has_change and has_tension_or_trigger and len(core_assets) >= 2:
        return "recommended"
    if has_idea and (has_tension_or_trigger or has_change) and core_assets:
        return "limited"
    return "not_recommended"


def compute_duration_feasibility(
    evidence_duration: float,
    target_duration: float,
) -> tuple[str, float]:
    """故事素材对本次时长的支撑力，不改变故事本身是否成立。"""
    target = max(1.0, float(target_duration or 45.0))
    coverage = max(0.0, float(evidence_duration)) / target
    # M1 只负责给 M2 诚实的时长提示；最终时长要等到冻结候选映射后确认。
    if coverage >= 0.85:
        feasibility = "sufficient"
    elif coverage >= 0.60:
        feasibility = "limited"
    else:
        feasibility = "insufficient"
    # 2026-09-23（David 定调）：拆掉 90 秒硬上限，长目标的推荐时长按素材如实反映。
    recommended = round(max(5.0, min(600.0, float(evidence_duration))), 1)
    return feasibility, recommended


def _normalize_story_priority(raw: Any) -> str:
    """Normalize M1's advisory story importance without ranking stories locally."""
    value = str(raw or "").strip().lower()
    aliases = {
        "high": "high", "高": "high", "核心": "high",
        "medium": "medium", "中": "medium", "中等": "medium",
        "low": "low", "低": "low", "备选": "low",
    }
    return aliases.get(value, "medium")


def matches_story_semantic_signature(
    strategy: Strategy,
    signature: Mapping[str, Sequence[Sequence[str]]],
) -> bool:
    """用于离线黄金验收：验证故事语义，不要求模型复述固定标题。

    signature 的键是 Strategy 字段名；每个字段是一组必须命中的概念组，
    每一组中任意一个词命中即可。它只用于评估 M1 召回，不参与线上排序。
    """
    field_text = {
        "story_premise": strategy.story_premise,
        "audience_tension": strategy.audience_tension,
        "story_trigger": strategy.story_trigger,
        "transformation": strategy.transformation,
        "core_commercial_idea": strategy.core_commercial_idea,
        "payoff": strategy.payoff,
        "product_role": strategy.product_role,
        "thesis": strategy.thesis,
        "evidence_claims": " ".join(item.claim for item in strategy.evidence),
    }
    for field, concept_groups in signature.items():
        haystack = str(field_text.get(str(field), "")).lower()
        for alternatives in concept_groups:
            if not any(str(term).lower() in haystack for term in alternatives):
                return False
    return True


def assess_story_commercial_change(
    strategy: Strategy,
    contract: Mapping[str, Sequence[Sequence[str]]],
) -> dict[str, Any]:
    """Audit a commercial change without coupling concepts to output fields.

    A valid story still needs a factual problem, a solution or mechanism, and
    a resulting user value. Each stage may be expressed in any narrative field
    or its cited evidence claim. Optional supporting signals are reported but
    never make a story fail.
    """
    narrative = " ".join((
        strategy.thesis,
        strategy.story_premise,
        strategy.audience_tension,
        strategy.story_trigger,
        strategy.transformation,
        strategy.product_role,
        strategy.core_commercial_idea,
        strategy.payoff,
        " ".join(strategy.supporting_arcs),
        " ".join(item.claim for item in strategy.evidence),
    )).lower()

    def check(groups: Sequence[Sequence[str]]) -> dict[str, Any]:
        normalized = [
            tuple(str(term).strip().lower() for term in group if str(term).strip())
            for group in groups
        ]
        normalized = [group for group in normalized if group]
        matches = [any(term in narrative for term in group) for group in normalized]
        return {"passed": all(matches), "matched_groups": matches}

    stages = {
        stage: check(groups)
        for stage, groups in contract.items()
        if stage in {"problem", "solution", "outcome"}
    }
    optional = {
        name: check(groups)
        for name, groups in contract.items()
        if name.startswith("optional_")
    }
    return {
        "passed": bool(stages) and all(item["passed"] for item in stages.values()),
        "stages": stages,
        "optional_supporting_signals": optional,
    }


def compute_contract_compatibility(blocked_types: Sequence[str]) -> tuple[float, str]:
    """合同可执行性：根据被禁的证据类型数量扣分。

    - 0 个被禁 → 1.0 / recommended
    - 1 个被禁 → 0.75 / conditional
    - ≥2 个被禁 → 0.5 及以下 / not_recommended
    """
    blocked = [str(t).strip().lower() for t in blocked_types if str(t).strip()]
    if not blocked:
        return 1.0, "recommended"
    compat = max(0.05, 1.0 - 0.25 * len(blocked))
    if len(blocked) >= 2:
        viability = "not_recommended"
    else:
        viability = "conditional"
    return round(compat, 3), viability


# 程序硬审计：关键词分类器（复用 content_policy 的 canonical kinds）
_POLICY_KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "price": ("元", "块钱", "价格", "原价", "现价", "改价", "优惠", "折扣", "便宜", "划算", "性价比", "倍率"),
    "cta": ("关注", "上车", "下单", "拍下", "链接", "加一波", "加单", "领券", "带一件", "带回去", "抢", "早拍"),
    "inventory_pressure": ("现货", "库存", "限量", "断货", "首批", "手慢无", "补不到", "不补货", "冲量", "冲榜"),
    "source_claim": ("原厂", "源头", "工厂", "厂家", "品牌方", "大牌", "媲美"),
    "social_proof": ("回购", "老粉", "自留", "留了一件", "亲测", "实测", "好评", "已卖", "成交", "口碑", "复购"),
    "after_sale": ("退换", "包退", "售后", "退货", "换货", "发货", "七天", "运费", "包邮"),
    "size_interaction": ("尺码", "卡码", "M码", "L码", "S码", "XL码", "打公屏", "身高体重"),
    "live_interaction": ("公屏", "点赞", "收藏", "评论区", "扣1", "扣个"),
}

_CONTENT_KIND_ALIASES = {
    "after_sales": "after_sale",
    "after_sale": "after_sale",
    "after-sales": "after_sale",
    "source": "source_claim",
    "source_claim": "source_claim",
    "size": "size_interaction",
    "size_interaction": "size_interaction",
    "interaction": "live_interaction",
    "live_interaction": "live_interaction",
    "social": "social_proof",
    "social_proof": "social_proof",
    "inventory": "inventory_pressure",
    "inventory_pressure": "inventory_pressure",
}


def _normalize_content_kind(kind: Any) -> str:
    raw = str(kind or "").strip().lower().replace(" ", "_")
    return _CONTENT_KIND_ALIASES.get(raw, raw)


def _blocked_kinds(content_contract: Mapping[str, Any] | None) -> set[str]:
    """只检查显式 forbid/block 的 kinds（不像 content_policy 默认全 block）。

    无合同 → 空集（不限制）。"""
    if not content_contract:
        return set()
    blocked: set[str] = set()
    for kind, value in content_contract.items():
        v = str(value or "").strip().lower()
        if v in ("block", "forbid", "blocked", "禁止", "禁用", "0", "false", "no"):
            normalized = _normalize_content_kind(kind)
            if normalized in _POLICY_KIND_KEYWORDS:
                blocked.add(normalized)
    return blocked


def detect_content_dependencies(
    evidence: Sequence[EvidenceItem],
    subtitle_text_map: Mapping[int, str],
    llm_dependencies: Sequence[str] = (),
) -> tuple[str, ...]:
    """识别故事用了哪些敏感商业内容，独立于合同是否允许。"""
    # 模型声明可用于审计比对，但不能在没有核心证据佐证时直接污染合同依赖。
    _ = llm_dependencies
    detected: set[str] = set()
    for item in evidence:
        # 合同硬审计会扫描原字幕，保证禁用内容不会漏过；这里记录的是
        # “故事实质依赖什么”，因此只看导演声明与这张证据卡的 claim，
        # 避免一条长字幕顺带提到价格就污染整条商业故事。
        claim = item.claim
        for kind in _POLICY_KIND_KEYWORDS:
            if _content_policy_hit_tokens(claim, kind):
                detected.add(kind)
        for sid in item.subtitle_ids:
            text = subtitle_text_map.get(sid, "")
            for kind in _POLICY_KIND_KEYWORDS:
                if _content_policy_hit_tokens(text, kind):
                    detected.add(kind)
    return tuple(sorted(detected))


def hard_audit_blocked_types(
    evidence: Sequence[EvidenceItem],
    subtitle_text_map: Mapping[int, str],
    content_contract: Mapping[str, Any] | None,
) -> tuple[tuple[str, ...], tuple[ContractAuditHit, ...]]:
    """程序硬校验：检测证据字幕文本是否命中 contract 里 block 的内容类型。

    返回 (blocked_types, audit_hits)。audit_hits 记录每个命中的字幕、关键词、
    证据角色，便于追溯「为什么被判违规」。
    """
    blocked_kinds = _blocked_kinds(content_contract)
    if not blocked_kinds:
        return (), ()
    detected: set[str] = set()
    hits: list[ContractAuditHit] = []
    for item in evidence:
        for sid in item.subtitle_ids:
            text = subtitle_text_map.get(sid, "")
            for kind in blocked_kinds:
                matched = _content_policy_hit_tokens(text, kind)
                if matched:
                    detected.add(kind)
                    hits.append(ContractAuditHit(
                        type=kind,
                        subtitle_id=sid,
                        raw_text=text[:80],
                        matched_keyword=matched[0],
                        evidence_role=item.role,
                    ))
    return tuple(sorted(detected)), tuple(hits)


# ──────────────────────────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────────────────────────

# This is one compact decision catalogue supplied to the same Director call;
# it is not eight pipelines or eight fixed-duration templates.  The model may
# choose a structure only when the source supports its decisive chapters.
_VIDEO_STRUCTURE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "pain_point",
        "name": "痛点切入型",
        "flow": "痛点/强结果 → 放大顾虑 → 解决机制 → 证明/体验 → 场景落地",
        "fit": "功能性商品、身材修饰、明确购买顾虑",
    },
    {
        "id": "suspense_reversal",
        "name": "悬念反转型",
        "flow": "反常识或疑问 → 先抑/普通状态 → 真实效果反转 → 细节证明 → 自然收束",
        "fit": "有真实反差、前后效果或反常识解释",
    },
    {
        "id": "scene_immersion",
        "name": "场景代入型",
        "flow": "场景画面 → 人物代入 → 产品登场 → 使用/搭配展开 → 场景收束",
        "fit": "有真实出游、通勤、日常出门或穿搭场景口播",
    },
    {
        "id": "price_anchor",
        "name": "价格锚定型",
        "flow": "真实价格对比 → 价格反差 → 品质/细节证明 → 适穿证明 → 交易收束",
        "fit": "用户明确允许价格，且字幕有真实、可核对的价格内容",
        "requires_policy": ("price",),
    },
    {
        "id": "styling_tutorial",
        "name": "穿搭教学型",
        "flow": "搭配问题 → 穿搭痛点 → 正确示范 → 单品拆解 → 完整 look/场景",
        "fit": "有真实搭配示范、上下装或配色细节",
    },
    {
        "id": "urgency_conversion",
        "name": "紧迫感逼单型",
        "flow": "真实机会信息 → 快速效果/细节 → 已允许的价格或福利 → 真实库存/行动信息",
        "fit": "用户明确允许促销与库存话术，且原素材有真实信息",
        "requires_policy": ("cta", "inventory_pressure"),
    },
    {
        "id": "friend_recommendation",
        "name": "闺蜜种草型",
        "flow": "真实口吻 → 亲身体验 → 安利理由 → 细节/搭配 → 软性收束",
        "fit": "主播原话确有自然体验、推荐或日常分享感",
    },
    {
        "id": "comparison_showcase",
        "name": "对比展示型",
        "flow": "真实对比引入 → 旧款/常见痛点 → 新优势 → 细节对比 → 适用场景",
        "fit": "有真实前后、同类、版型或效果对比；不得编造竞品",
    },
)


def _policy_allows_structure(content_contract: Mapping[str, Any] | None, required: Sequence[str]) -> bool:
    """Expose only structures whose decisive commercial content is allowed.

    ``body_only`` is intentionally insufficient here: price-anchor and
    urgency structures demand that restricted content leads the story.  This
    keeps user policy authoritative without letting code choose a structure.
    """
    if not required:
        return True
    policy = dict(content_contract or {})
    for kind in required:
        raw_value = policy.get(kind)
        if raw_value is None and kind == "inventory_pressure":
            raw_value = policy.get("inventory")
        if str(raw_value or "").strip().lower() not in {"allow", "prefer", "可用", "优先"}:
            return False
    return True


def available_video_structures(content_contract: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    """Return the selectable structure catalogue for this one Director call."""
    rows: list[dict[str, str]] = []
    for item in _VIDEO_STRUCTURE_CATALOG:
        if not _policy_allows_structure(content_contract, item.get("requires_policy") or ()):
            continue
        rows.append({
            "id": str(item["id"]),
            "name": str(item["name"]),
            "flow": str(item["flow"]),
            "fit": str(item["fit"]),
        })
    return rows

ANALYZER_SYSTEM_PROMPT = (
    "你是直播短视频商业导演。完整阅读字幕后，只完成一件事：导演出一条值得发布的主视频，"
    "并用真实连续字幕落实它。不要输出卖点清单、证据库存、评分或自我审计。\n\n"
    "在内部按四步工作，但不要输出思考过程：\n"
    "1. 先确定一个核心购买认知：观众看完整条片后到底应该相信什么。不要按高频词选主题。\n"
    "2. 设计 4–8 个有因果顺序的购买章节。chapter_kind 只能从 pain/result/mechanism/fit/comfort/risk/styling/scene/trust 中选择，"
    "同一种 kind 最多一章；pain+result+mechanism 合计最多3章。正面、侧面、后背、肩部若仍只证明同一显瘦结果，"
    "不能拆成多个章节。fit 必须由身材/体重/尺码原话直接成立；comfort 必须直接讲穿着体验；risk 必须直接解除透、里衬、弹力、"
    "安全裤等顾虑；styling 必须直接讲怎么搭，scene 必须直接讲省心或使用场景，两者内容不同才可同时出现。"
    "推荐宏观顺序是结果/体验入口→机制→适穿→舒适→风险解除→搭配/场景→可选信任；完整字幕存在这些证据时优先采用，trust 永远可删。\n"
    "3. 每章不是一句卖点，而是一个完整微叙事 chapter packet。为每章从完整安全字幕中选择1个或多个最自然的完整 spoken window；"
    "短目标可以每章1个，60秒或90秒目标要在有真实新价值时用多个 Beat 把章节讲清楚。章节内部优先形成‘结论/体验→原因→具体细节→证明或顾虑解除’，"
    "但不要求每章机械凑齐。后一个 Beat 必须增加新的解释、细节、证明、体验或风险解除；同一结果换句话重复不算推进。不能从一个窗口里只截单行。"
    "同一章内 beat_function 不得重复。result 章重复肩变窄、fit 章重复同一尺码表、comfort 章重复‘凉快’口号都属于无效填充；"
    "应改用 scope_expand、mechanism/detail/proof、size_rule/body_fit、experience/scene、concern/risk_remove 等不同功能把一个购买问题讲完整。"
    "但换 beat_function 标签不能把重复内容变成新推进：两句都在说同一次肩变窄，或两次播同一尺码区间，即使分别标 result/proof、size_rule/body_fit 也必须只留一句。"
    "每个 Beat 的 beat_advance 必须准确写出这句原话单独新增的认知；如果写不出与前句不同的新增认知，就不要选它。"
    "每个窗口已经是连续 2–8 秒口语，source_span 必须等于窗口显示的完整 IDs，verbatim 必须逐字复制窗口原话。"
    "surface 不是 clean 的窗口不要选；scope=related_product 只能作为当前商品的搭配/使用补充，不能独立改变主故事对象。"
    "同一窗口全片只能使用一次。第一章 kind 只能是 result/comfort/scene，且第一章第一个 Beat 必须选择 opening=clean 的窗口。Opening 必须独立、直接、干净，不能以‘你可能会说’‘你相信我’‘你看这里’"
    "等铺垫式直播对话、悬空指代或纯画面指示开场。\n"
    "4. 在输出前只读所有 verbatim，忽略你写的章节标题，把它们按顺序连起来听一遍。删除重复、残句、ASR怪句和跳跃；"
    "然后把所有 Beat 的最终原话连接为 final_readthrough。使用窗口标注的真实秒数安排目标深度，但不要输出字幕条数、时长统计或宣布 pass；程序只做机械核对且不会改你的语义方案。\n\n"
    "正常成片范围 30–120 秒，但时长是故事结果，不得注水。素材有限时仍输出可执行短片。"
    "可额外给 0–2 个真正不同的 alternative 方向卡，但只能写标题、核心购买认知和开场承诺，不能预选字幕。\n"
    "必须只返回合法 JSON。"
)


def director_duration_depth_contract(target_duration: float) -> dict[str, Any]:
    """Translate target seconds into a non-semantic one-call casting budget."""
    requested = min(120.0, max(30.0, float(target_duration or 45.0)))
    if requested <= 44.0:
        mode, beat_low, beat_high, per_chapter_high = "quick", 8, 14, 3
        chapter_depth_targets = {
            "result": [1, 2], "mechanism": [1, 2], "fit": [1, 2],
            "comfort": [1, 3], "risk": [1, 2], "styling": [1, 2],
        }
    elif requested <= 74.0:
        mode, beat_low, beat_high, per_chapter_high = "standard", 15, 22, 4
        chapter_depth_targets = {
            "result": [2, 3], "mechanism": [3, 4], "fit": [2, 3],
            "comfort": [3, 4], "risk": [2, 3], "styling": [3, 4],
            "scene": [1, 3], "trust": [0, 2],
        }
    elif requested <= 104.0:
        mode, beat_low, beat_high, per_chapter_high = "deep", 24, 32, 5
        chapter_depth_targets = {
            "result": [3, 4], "mechanism": [4, 5], "fit": [3, 4],
            "comfort": [4, 5], "risk": [3, 4], "styling": [4, 5],
            "scene": [2, 4], "trust": [1, 3],
        }
    else:
        mode, beat_low, beat_high, per_chapter_high = "long", 30, 40, 6
        chapter_depth_targets = {
            "result": [3, 5], "mechanism": [4, 6], "fit": [3, 5],
            "comfort": [5, 6], "risk": [3, 5], "styling": [5, 6],
            "scene": [3, 5], "trust": [2, 4],
        }
    return {
        "mode": mode,
        "requested_seconds": round(requested, 1),
        "preferred_source_seconds": {
            "low": round(max(30.0, requested * 0.80), 1),
            "high": round(min(120.0, requested * 1.10), 1),
        },
        "expected_total_beats": {"low": beat_low, "high": beat_high},
        "beats_per_chapter": {"low": 1, "high": per_chapter_high},
        # 章节数上限：⌈目标秒数 ÷ 10⌉。宁可少一章把话说透，也不要多一章碎块。
        "chapters_max": max(3, int(math.ceil(requested / 10.0))),
        "chapter_depth_targets": chapter_depth_targets,
        "allowed_beat_functions": [
            "promise", "result", "mechanism", "detail", "proof", "scope_expand",
            "size_rule", "body_fit", "experience", "scene", "concern", "risk_remove",
            "styling", "trust",
        ],
        "stop_rule": "没有新的解释、细节、证明、体验或风险解除时自然结束，禁止重复注水",
    }


def resolve_commercial_director_model(base_url: str, configured_model: str) -> str:
    """Honor the configured Director model.

    The UI default is ``deepseek-v4-flash``.  Earlier builds silently promoted
    that setting to ``deepseek-v4-pro`` for the commercial Director, which made
    every preview much more expensive than the visible setting implied.  Users
    can still type/select ``deepseek-v4-pro`` explicitly when they want the
    higher quality tier.
    """
    model = str(configured_model or "").strip()
    return model


def _fmt_ts(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds or 0) * 1000.0)))
    h, rem_ms = divmod(total_ms, 3_600_000)
    m, rem_ms = divmod(rem_ms, 60_000)
    s, ms = divmod(rem_ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _format_subtitles(
    subtitles: Sequence[Mapping[str, Any]],
    commercial_assets: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Show every SRT row once as a deterministic adjacent spoken window.

    SRT rows are alignment units, not editorial Beats.  Windowing changes no
    text and chooses no semantic material; it only provides a 2–8 second unit
    that the one Director can hear as a whole.  Product scope and narrow
    surface warnings are inline facts, replacing the old duplicated Ledger.
    """
    scope_by_id: dict[int, str] = {}
    for asset in commercial_assets or ():
        if not isinstance(asset, Mapping):
            continue
        try:
            asset_id = int(asset.get("candidate_id") or asset.get("srt_index") or 0)
        except (TypeError, ValueError):
            continue
        context = asset.get("subject_context") if isinstance(asset.get("subject_context"), Mapping) else {}
        scope_by_id[asset_id] = str(context.get("product_focus") or "unknown").strip().lower() or "unknown"
    rows: list[dict[str, Any]] = []
    for i, sub in enumerate(subtitles, 1):
        sid = int(sub.get("id") or sub.get("index") or i)
        try:
            start_seconds = float(sub.get("start") or 0)
            end_seconds = float(sub.get("end") or start_seconds)
        except (TypeError, ValueError):
            start_seconds = end_seconds = 0.0
        text = str(sub.get("text") or "").strip()
        rows.append({
            "id": sid,
            "start": start_seconds,
            "end": max(start_seconds, end_seconds),
            "text": text,
            "scope": scope_by_id.get(sid, "unknown"),
        })

    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for row in rows:
        if current:
            gap = row["start"] - current[-1]["end"]
            projected = row["end"] - current[0]["start"]
            if row["id"] != current[-1]["id"] + 1 or gap > 1.0 or projected > 8.5:
                windows.append(current)
                current = []
        current.append(row)
        duration = current[-1]["end"] - current[0]["start"]
        closes_thought = bool(re.search(r"[。！？!?]$", str(row["text"])))
        if duration >= 6.5 or (duration >= 2.2 and closes_thought):
            windows.append(current)
            current = []
    if current:
        windows.append(current)

    lines: list[str] = []
    for window_index, window in enumerate(windows, 1):
        duration = max(0.0, window[-1]["end"] - window[0]["start"])
        id_label = (
            str(window[0]["id"])
            if len(window) == 1 else f"{window[0]['id']}-{window[-1]['id']}"
        )
        combined_text = " ".join(str(row["text"] or "").strip() for row in window).strip()
        surface = final_utterance_surface_issue(combined_text) or "clean"
        opening = director_opening_input_issue(combined_text) or "clean"
        scopes = "/".join(dict.fromkeys(str(row["scope"] or "unknown") for row in window))
        lines.append(
            f"[W{window_index:03d}][IDs {id_label}]"
            f"[{_fmt_ts(window[0]['start'])}-{_fmt_ts(window[-1]['end'])}]"
            f"[{duration:.2f}s][scope={scopes}][surface={surface}][opening={opening}] {combined_text}"
        )
    return "\n".join(lines)


def _commercial_asset_catalog(commercial_assets: Sequence[Mapping[str, Any]] | None) -> str:
    """Render a non-whitelist asset map keyed to the existing subtitle IDs."""

    lines: list[str] = []
    for raw in commercial_assets or ():
        if not isinstance(raw, Mapping):
            continue
        try:
            candidate_id = int(raw.get("candidate_id") or raw.get("srt_index") or 0)
        except (TypeError, ValueError):
            continue
        if candidate_id <= 0:
            continue
        context = raw.get("subject_context") if isinstance(raw.get("subject_context"), Mapping) else {}
        lines.append(
            f"[{candidate_id:03d}] permission={str(raw.get('story_permission') or 'supporting_story')} "
            f"asset_role={str(raw.get('asset_role') or 'unknown')} "
            f"product_focus={str(context.get('product_focus') or 'unknown')} "
            f"confidence={str(context.get('confidence') or 'low')} "
            f"reason={json.dumps(str(raw.get('reason') or ''), ensure_ascii=False)}"
        )
    return "\n".join(lines) or "（本轮未提供 Commercial Asset Ledger；按既有字幕发现流程执行。）"


def _contract_forbidden_lines(content_contract: Mapping[str, Any] | None) -> str:
    if not content_contract:
        return "（无特殊限制，所有内容类型均可作为策略证据）"
    # ``normalize_content_policy`` uses ``block``/``body_only``/``allow``/
    # ``prefer``.  The historical helper only recognised ``blocked`` and
    # therefore told the Director that the default all-block policy had no
    # restrictions.  Reuse the canonical policy wording so the first semantic
    # decision sees the same user choice that later safety filters enforce.
    from content_policy import policy_prompt_lines

    lines = policy_prompt_lines(content_contract)
    return "；".join(lines) if lines else "（无特殊限制，所有内容类型均可作为策略证据）"


def _story_content_boundary_prompt(content_contract: Mapping[str, Any] | None) -> str:
    """Describe the M1 boundary without repeating forbidden sales language.

    M1 receives an already filtered pool. Naming every forbidden subject in
    its prompt can make it invent a price-led chapter even though no eligible
    utterance supports it. Casting still receives the role-level policy; Story
    only needs to know that omitted material is unavailable for a promise.
    """
    if _blocked_kinds(content_contract) or _body_only_kinds(content_contract):
        return (
            "内容边界已在输入前执行：本轮字幕池只提供可用于故事的原话。"
            "不得猜测、提及、暗示或为未提供内容设计章节；每章必须由池中的真实原话支持。"
        )
    return "本轮字幕池为完整可用原话；每章仍必须由池中的真实原话支持。"


def normalize_director_controls(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize run-scoped UI preferences for the two-pass Director.

    These values guide the Director but never override the source transcript
    or the content policy.  An explicit contract also makes it possible to
    audit what the two AI calls actually received.
    """
    raw = dict(value or {}) if isinstance(value, Mapping) else {}
    compact = (raw.get("controls_version") or raw.get("contract_version")) == "director-controls-v2"
    automatic = {"", "自动", "自动识别", "自动检测", "auto", "默认", "无", "none"}

    def clean_text(item: Any, limit: int = 80) -> str:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        return "" if text.lower() in automatic else text[:limit]

    def clean_list(item: Any, limit: int = 32, text_limit: int = 40) -> list[str]:
        values = (
            [item] if isinstance(item, str)
            else list(item or ()) if isinstance(item, (list, tuple, set))
            else []
        )
        result: list[str] = []
        for entry in values:
            text = clean_text(entry, text_limit)
            if text and text not in result:
                result.append(text)
            if len(result) >= limit:
                break
        return result

    preference_weights: dict[str, float] = {}
    raw_weights = raw.get("preference_weights")
    if isinstance(raw_weights, Mapping):
        for key, value in list(raw_weights.items())[:24]:
            topic = clean_text(key, 40)
            try:
                weight = float(value)
            except (TypeError, ValueError):
                continue
            if topic and math.isfinite(weight):
                preference_weights[topic] = round(max(0.0, min(3.0, weight)), 2)

    supporting_products = "block" if raw.get("supporting_products") == "block" else "allow"
    return {
        "contract_version": "director-controls-v2" if compact else "director-controls-v1",
        "primary_category": clean_text(raw.get("primary_category")),
        "secondary_category": clean_text(raw.get("secondary_category")),
        "leaf_category": clean_text(raw.get("leaf_category")),
        "main_product": clean_text(raw.get("main_product"), 120),
        "source_product_hints": clean_list(raw.get("source_product_hints"), 16, 160),
        "director_direction": clean_text(raw.get("director_direction") or raw.get("goal")),
        "extra_instruction": clean_text(raw.get("extra_instruction"), 300),
        "supporting_products": supporting_products,
        "priority_theme": "" if compact else clean_text(raw.get("priority_theme") or raw.get("focus_hint")),
        "preferred_topics": [] if compact else clean_list(raw.get("preferred_topics") or raw.get("selling_points")),
        "preferred_terms": [] if compact else clean_list(raw.get("preferred_terms") or raw.get("priority_terms")),
        "preference_weights": {} if compact else preference_weights,
        "avoid": (["无关闲聊", "无效重复"] + (["搭配其他品"] if supporting_products == "block" else [])) if compact else clean_list(raw.get("avoid")),
        "opening_style": "" if compact else clean_text(raw.get("opening_style") or raw.get("hook_style")),
        "ending_style": "" if compact else clean_text(raw.get("ending_style")),
    }


def _director_controls_prompt(
    director_controls: Mapping[str, Any] | None,
    *,
    stage: str,
) -> str:
    controls = normalize_director_controls(director_controls)
    product_target = build_product_target(controls)
    category_rule = (
        "主商品或细分类目已由用户明确指定，必须作为身份约束执行。"
        if product_target.get("mode") == "locked" else
        "主商品和细分类目均未锁定；一级/二级类目只是项目提示。若它们与素材标题及完整字幕明显冲突，以素材事实识别当前商品，不得把旧页面品类强套给素材。"
    )
    meaningful = {
        key: value for key, value in controls.items()
        if key != "contract_version" and value not in ("", [], {}, None)
    }
    if not meaningful:
        return "本次没有额外导演偏好；以素材事实和购买故事完整性为准。"
    if stage == "cast":
        # M1 has already used the complete controller explanation to freeze the
        # story.  Repeating its prose at Casting makes the model reread the
        # same policy without giving it any new decision.  Keep every supplied
        # value and the few rules that constrain an ID-level choice.
        return "\n".join([
            "本轮只在冻结故事内按以下导演约束择句：",
            json.dumps(
                {"contract_version": controls["contract_version"], **meaningful},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "不得改写或重构第一遍故事；extra_instruction 不是素材事实，只在真实素材支持且不违反内容边界与主商品范围时执行。",
            category_rule,
            "商品识别目标：" + json.dumps(product_target, ensure_ascii=False, separators=(",", ":")),
            "main_product 明确时优先于素材标题；只填 leaf_category 也限定当前单品。source_product_hints 只核对身份，不能据此编造卖点或把单件扩成整套。",
            "supporting_products=block 时不选其他商品搭配句；allow 也只允许服务主商品的搭配，不能混入其他商品独立卖点。",
            "director-controls-v2 只使用一个导演侧重和补充要求；无关闲聊、无效重复不选，但保留故事所需上下文和不同证据。",
            "优先级：内容边界与素材事实 > 主商品范围 > 用户明确选中的备选方案 > 本次导演方向/优先讲 > 长期选片倾向。优先讲/重点词不是必须覆盖清单；素材证据弱时应舍弃。",
        ])
    stage_rule = (
        "用于选择核心购买欲望、开场承诺和章节侧重。"
        if stage == "story" else
        "只能在符合冻结故事的真实短句之间作为择优条件，不得改写或重构第一遍故事。"
    )
    return "\n".join([
        "本次导演参数（主商品/细分类目是身份约束；未锁定商品时一级/二级类目仅为软提示）：",
        json.dumps(
            {"contract_version": controls["contract_version"], **meaningful},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        stage_rule,
        category_rule,
        "extra_instruction 是用户本次补充要求，不是素材事实；只在真实素材支持且不违反内容边界与主商品范围时执行。",
        "supporting_products=block 时不选其他商品搭配句；allow 也只允许服务主商品的搭配，不能混入其他商品独立卖点。",
        "director-controls-v2 只使用一个导演侧重和补充要求；开头与收尾自动编排，不叠加旧卖点多选、重点词或权重。无关闲聊、无效重复不选，但必须保留故事所需上下文及不同证据。",
        "优先级：内容边界与素材事实 > 主商品范围 > 用户明确选中的备选方案 > 本次导演方向/优先讲 > 长期选片倾向。",
        "商品识别目标：" + json.dumps(product_target, ensure_ascii=False),
        "main_product 明确时优先于旧细分类目和素材标题；只填 leaf_category 也限定当前单品。找不到该商品就说明冲突，不能改卖裤子或默认整套。‘衣服’是大类，不足以确认具体单品。",
        "source_product_hints 是用户所选素材的标题，仅供核对商品身份，不能据此编造面料、效果或其他卖点；显式 main_product 优先。标题指向单件时，不得仅因主播说‘这套’就扩成整套销售。",
        "preference_weights 是长期软偏好：0=尽量少讲、1=标准、3=强优先；不是分数排序或必选配额，只参考与当前品类和故事相关的倾向。",
        "优先讲/重点词不是必须覆盖清单；素材证据弱时应舍弃。避选项不得进入主动选句。",
        "开头与收尾偏好只有在存在干净、完整、符合主故事的原话时才使用，不得制造伪 Hook 或硬凑结尾。",
        (
            "结尾专项：最后一章必须是收尾章（chapter_kind 取 close 语义），并且同时满足三点："
            "① 收住全片，让观众明确知道‘讲完了’；② 回到本片的主购买理由，不得引入新卖点或新商品；"
            "③ 不能停在半句——结尾章的最后一句必须是一句能独立收住的完整原话。"
            "若导演参数给了 ending_style（自然收尾/强调价值/制造紧迫），按该风格设计结尾；未给则自然收尾。"
            "若安全池里找不到能让结尾章完整收住的真实原话，就把该章收窄到一句能收住的完整原话；"
            "不许硬凑、不许截半句，也不许把结尾章退化成一句与全片无关的客套话。"
        ) if stage == "story" else "",
    ])


def _final_utterance_surface_exclusions(subtitles: Sequence[Mapping[str, Any]]) -> str:
    """Render deterministic hard exclusions for the one director call."""
    rows: list[str] = []
    for position, subtitle in enumerate(subtitles, 1):
        try:
            subtitle_id = int(subtitle.get("id") or subtitle.get("index") or position)
        except (AttributeError, TypeError, ValueError):
            continue
        issue = final_utterance_surface_issue(str(subtitle.get("text") or ""))
        if issue:
            rows.append(f"[{subtitle_id:03d}] {issue}")
    return "、".join(rows) or "（无）"


def build_analyzer_user_prompt(
    *,
    product: str,
    subtitles: Sequence[Mapping[str, Any]],
    content_contract: Mapping[str, Any] | None = None,
    commercial_assets: Sequence[Mapping[str, Any]] | None = None,
    executable_subtitle_ids: Sequence[int] | None = None,
    target_duration: float = 45.0,
    director_focus: Mapping[str, Any] | None = None,
) -> str:
    executable_ids: set[int] = set()
    for value in executable_subtitle_ids or ():
        try:
            subtitle_id = int(value)
        except (TypeError, ValueError):
            continue
        if subtitle_id > 0:
            executable_ids.add(subtitle_id)
    director_subtitles = list(subtitles)
    if executable_ids:
        filtered_subtitles: list[Mapping[str, Any]] = []
        for position, subtitle in enumerate(subtitles, 1):
            try:
                subtitle_id = int(subtitle.get("id") or subtitle.get("index") or position)
            except (AttributeError, TypeError, ValueError):
                continue
            if subtitle_id in executable_ids:
                filtered_subtitles.append(subtitle)
        director_subtitles = filtered_subtitles
    transcript = _format_subtitles(director_subtitles, commercial_assets)
    contract_line = _contract_forbidden_lines(content_contract)
    subtitle_heading = (
        "完整安全且可执行的字幕池（含句子 ID + 时间戳，director_sequence 的 subtitle_ids 只可引用这里；不是 TopK）："
        if executable_ids else
        "Hard-safe 原始字幕事实（含句子 ID + 时间戳，subtitle_ids 只可引用这里的数字 ID）："
        if commercial_assets else
        "完整直播字幕（含句子 ID + 时间戳，subtitle_ids 请引用这里的数字 ID）："
    )
    subject_line = (
        "故事对象：当前选中商品（不提供商品名称；对象的品类、风格和卖点均以字幕事实为准）"
        if commercial_assets else
        f"商品：{str(product or '').strip()}"
    )
    depth_contract = director_duration_depth_contract(target_duration)
    lines = [
        subject_line,
        f"内容合同：{contract_line}",
        (
            "用户已确认这个方向；主方案围绕它独立选择真实原话："
            + json.dumps(dict(director_focus or {}), ensure_ascii=False)
            if director_focus else
            "请从全文选择唯一最佳主故事；其他方向只能作为不含字幕的标题卡。"
        ),
        "",
        "可选视频结构（为主方案选择最合适的一种，不按目录顺序套模板）：",
        json.dumps(available_video_structures(content_contract), ensure_ascii=False),
        f"目标时长：{float(target_duration or 45.0):.0f}s；通常交付 30–120s。",
        "本次章节深度预算（只决定需要讲多深，不允许重复注水）："
        + json.dumps(depth_contract, ensure_ascii=False),
        (
            "这是主方案的有效深度约束：完整池能提供不同功能的 clean Beat 时，"
            "最终 beats 总数不得低于 expected_total_beats.low，并按 chapter_depth_targets 展开；"
            "不要在每章只选1–2句后提前结束。只有确实找不到新功能证据时才允许短缺。"
        ),
        "",
        "返回这个精简 JSON，不要添加字段：",
        "{",
        '  "strategies": [',
        '    {"strategy_id":"S1", "director_plan_role":"primary",',
        '     "director_title":"<主视频标题>",',
        '     "core_desire":"<观众看完应形成的一个核心购买认知>",',
        '     "opening_promise":"<第一章立即承诺什么>",',
        '     "narrative_archetype":"<结构id>",',
        '     "video_structure":{"id":"<结构id>","name":"<结构名>","selection_reason":"<一句理由>"},',
        '     "chapter_packets":[',
        '       {"chapter_id":"C1","chapter_kind":"result","title":"<章节标题>",',
        '        "buyer_advance":"<听完本章后新增的一个购买认知>","beats":[',
        '          {"beat_id":"C1B1","beat_function":"result","window_id":"W001",',
        '           "beat_advance":"<这句原话独立新增的购买认知>",',
        '           "source_span":{"start_id":101,"end_id":102},',
        '           "verbatim":"<逐字复制你最终保留的原话：可以是整个窗口，也可以只是其中一部分>"},',
        '          {"beat_id":"C1B2","beat_function":"proof","window_id":"W002",',
        '           "beat_advance":"<与上一句不同的新证明或细节>",',
        '           "source_span":{"start_id":103,"end_id":104},',
        '           "verbatim":"<逐字复制整个W002的完整原话>"}',
        '        ]}',
        '     ],',
        '     "final_readthrough":"<严格按最终顺序连接所有verbatim，不改写>"},',
        '    {"strategy_id":"S2","director_plan_role":"alternative","director_title":"<可选标题>",',
        '     "core_desire":"<不同核心购买认知>","opening_promise":"<不同开场承诺>",',
        '     "narrative_archetype":"<结构id>","chapter_packets":[]}',
        "  ]",
        "}",
        "chapter_kind 每种最多一次；不要输出 purchase_question_id、answer_role，程序会按 chapter_kind 机械映射旧合同。",
        "每个 Beat 选择一个 surface=clean 的 spoken window；source_span 等于该窗口包含首尾的 IDs。",
        (
            "【verbatim 写你最终想保留的那段原话】默认保留整个窗口；"
            "如果窗口的**开头或结尾**有不好听的碎片（残句、ASR 怪句、直播接话、悬空的「呃/嗯/哦」等），"
            "你可以**只保留中间那段完整的话**，把 `verbatim` 写成你实际要保留的原话。"
            "例如窗口是「19呃，马甲的话应该也挺快的哦。」，若开头「19呃」是碎片，"
            "verbatim 就写「马甲的话应该也挺快的哦。」。"
            "要求：**必须逐字复制、不得改写任何字、不得新增内容**；保留的那段必须能独立听懂；"
            "source_span 仍然写完整窗口的 IDs。程序会在窗口的词级时间轴上定位你保留的那段原文，"
            "定位不到（例如改写过了）或保留部分不能独立成句时，会自动退回整窗，不会失败。"
        ),
        (
            "Opening 是 C1 的完整开场小故事，不是一条高分句。先在全文内部比较可用的开场组合，只输出最好的一组："
            "第一 Beat 优先且在存在候选时必须选择 opening=clean + surface=clean、陌生观众无需上文就能理解、"
            "直接兑现 opening_promise 的具体结果/痛点/强体验；紧接 1 条不同功能的即时 payoff（证明、机制或体验），"
            "素材确有第三个新认知时才加 scope expansion。C1 总计 1–3 个 Beat、通常 8–12 秒；"
            "不得以‘的/它/这个/好的/来/是不是/你看’等直播接话、悬空指代或泛泛夸赞开场。"
        ),
        (
            "若全文确实没有 opening=clean，仍须交付可编辑方案：从 surface=clean 中选择语义最闭合、最具体的结果或场景句，"
            "最多允许轻度画面依赖；不得为追求强度使用残句、ASR 怪句或直播寒暄。‘你看这里/从这儿到这儿’可作为 Hook 后的"
            "画面证明，但有其他干净结果句时不得作为第一 Beat。"
        ),
        "用 beats 数组扩展章节内部的解释、细节、证明、体验或风险解除；不要靠新增相同结论或同义口号追时长。",
        "同章 beat_function 必须各不相同，且必须来自 allowed_beat_functions。重复尺码表、重复肩部显窄、重复凉快口号必须删除并换成不同功能证据。",
        "beat_advance 必须逐 Beat 填写，并与实际 verbatim 一致；标签不同但 beat_advance 实质相同仍然算重复。",
        "尽量让所选窗口真实秒数落入本次 preferred_source_seconds；若完整池没有新的合格推进，可以自然短于目标，但仍须输出可执行方案。",
        "",
        subtitle_heading,
        transcript,
        "",
        "最后提醒：只返回一个含 chapter_packets 的 primary；备选卡不得选择字幕。"
        "先逐字检查每个 Beat 的 verbatim，再写 final_readthrough。返回前必须在内部从头连读一次：通过改选或删除 Beat，"
        "清掉残句、ASR 怪句、悬空指代、相邻同义重复、重复尺码和突然插入的弱尾章；不得改写主播原话。"
        "宁可自然短于目标，也不要保留一条会让成片变差的句子。不要输出自我审计、证据池、备用开场或时长统计。",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Two-pass Director: story contract first, exact Beat casting second
# ──────────────────────────────────────────────────────────────

TWO_PASS_STORY_SYSTEM_PROMPT = """你是直播女装短视频的故事导演。你只负责决定这条视频为什么存在、围绕什么购买欲望、按什么顺序说服观众；本轮不选择最终片单。

硬原则：
1. 完整阅读全部安全字幕，先比较不同故事方向，再冻结一个证据最充足、最值得发布的主故事；备选方向只保留方向卡。
2. core_desire 必须是一句观众视角的完整购买判断，不是卖点列表。
3. Opening 必须用反差、痛点、强结果或强场景让人继续看，并在最前面的必要章节中兑现，不能只铺垫。它必须脱离直播前文、章节标题和画面也能听懂：听到主商品、具体结果或顾虑、以及继续看的理由；以“又/然后/因为/你看/放在这里/这一根”等前文依赖或操作指令开头的原话不能承担 Opening。
4. 每个章节必须改变一次观众的购买判断。章节之间要有因果、追问、扩大证明、顾虑解除或使用场景关系。操作演示只是证明，不是购买问题；同一件衣服的扣法、垂放、绕法、腰封等不能拆成多个章节来填时长。
5. 本轮只输出章节职责；唯一例外是每个完整方案最多 3 组 opening_evidence_packages，用真实 ID 记录“Hook -> 紧接 payoff”是否有可行证据。payoff 必须给出与 Hook 不同的新事实（结果之后的机制、证明、穿法或体验）；把同一卖点换词、加重语气或再次介绍，不能算 payoff，也不要报成候选。它们不是最终片单、不是 beats，也不锁定顺序；具体选句、排序和取舍全部留给下一遍 Beat Casting。除此之外不得输出 beats、subtitle_ids、source_span、原话、时间戳或成片连读。
6. 用户禁止或限制的内容已在输入前过滤；不得猜测、依赖或为未提供内容安排故事章节。每项 completion_requirements 只写一个具体购买问题，价格、催单等禁选要求不能混入材质、设计由来等正常职责；各方案都应仅凭允许内容构成完整故事。
7. 只使用字幕明确支持的商品事实，不根据文件名或常识补写商品卖点。先确定 product_scope：主商品名称、single_product/explicit_set、搭配品使用边界。以用户主商品为先，用素材标题和字幕核对身份；若是单件，另一件只能证明搭配，不能把另一件的遮腿、裙摆、裤型等效果归给主商品。标题缺失时从字幕确定一个明确主商品，不能把同场多个商品默认拼成套装。
8. 用户时长是应努力达到的交付目标；事实准确、完整表达和不重复优先，素材不足时收窄承诺并报告缺口。开场只安排一个可迅速兑现的问题，开场预算必须包括痛点和紧接的本款答案，不能整章只铺痛点；不要以城市天气、直播热度等长铺垫代替商品故事。按原声预算为每章分配 source_budget_seconds，合计接近原声目标且不得超过交付上限；同时写 completion_requirements，明确问题、解释、具体证据和结论怎样闭合。相邻章节若都只能回答同一个购买问题，必须在本轮合并、删去其一，或明确第二章新增的不同问题。长目标首先把章节讲充分，再探索服务主故事的新购买价值；不能靠同义重复、无关卖点或固定章数填满。
9. required 只给成立主故事不可缺少的章节；recommended 是素材强时值得讲的章节；optional 无增益可以主动舍弃，不要求 Q1-Q7 全覆盖。
10. 用户只要求1版时，备选方向只写标题、核心欲望和开场承诺，不设计完整章节、更不能选句。用户明确要求2-3版时，每个方案都必须成为同一商品下可独立执行的完整故事合同，拥有不同购买切入点、开场和前段章节路径；仍然不能在本阶段选句。差异不要求证据互斥，后段可共享必要的购买证明。每个方向必须有支撑本次目标时长的叙事深度，不要把一条完整购买故事拆成只讲颜色、只讲剪裁等证据不足的短版。
11. 先比较‘为什么想要’与‘已经有同类为什么还选这一件’等购买问题。选择能被干净短句和具体证据连续兑现的中心，不选择听起来宏大却需要拼凑跨商品证据的中心。不要为凑七章把同一细节改名重复讲。
12. required 章节只能承诺后来可由安全可选原话逐项兑现的内容；若只是 context 中看见、被内容合同排除、或没有可执行短句支持，应删去该承诺或明确标为 source_limited，不能把它写成完整章节。
13. 返回中的标题、职责、购买理由必须针对本素材实际填写，不得照抄 schema 的‘主视频标题’等占位文案。
14. 章节总数不得超过输入给你的章节数上限（⌈目标秒数 ÷ 10⌉，60 秒目标最多 6 章）。宁可少一章把话讲透，不要多一章碎块；
    章节数已经到上限时，只能把现有章节讲充分，不得再拆新章。
15. 每一章都必须能由源时间上相邻的口播连续兑现：不得把相隔很远的窗口安排进同一章，
    也不得设计一个只能靠跨时间拼句才能兑现的购买问题。相邻章节要用因果、追问、扩大证明或顾虑解除真正接上；
    接不上就合并或删掉，不要留一个孤立的小章节。
只返回合法 JSON，不输出思考过程。
【换行要求】每个字段、每个数组元素各占一行（不必缩进对齐，但必须有换行）；禁止把整个 JSON 挤在一行——单行超长 JSON 极易漏掉或多出括号，会让整轮结果作废。"""


TWO_PASS_CAST_SYSTEM_PROMPT = """你是直播女装短视频的 Beat Casting 导演。上游已冻结故事，本轮只从完整安全字幕池一次选出最终真实口播。

保持购买判断和商品边界；先选完整语义单元、联合编排开场与正文，再按章节调整规则归章计时；逐句选择真实原话，不改写、不跨商品、同一 ID 不重复。每个方案以真实原声时长为准，选够且不超长；逐句连读，保留必要上下句，删除重复和残句。开头须立即抓人并兑现：在同一次调用中先比较 2–3 个真实短句开场组合。每个标记 complete 的章节都必须逐项给出仅含本章已选 ID 的 completion_receipts；没有可执行证据就写 needs_context/source_limited，context ID 不能充当证据。

同章连续性硬规则（违反即视为选句失败）：
1. 同一章内的多个 Beat 必须来自同一个口播窗，或源时间相隔不超过 3 秒的相邻窗口；相隔超过 5 秒的两句不得留在同一章。
2. 若某句确实有价值但与本章源时间不相邻，必须把它放进时间上真正相邻的那一章；没有这样的章就不要选它。
   不得为了凑章、凑时长或凑深度，从相隔很远的两个窗口各取一句拼成一章。
3. 每一章必须自成一个能听懂的小段落：要么『结论 + 依据』，要么『问题 + 回答』。
   只有半句、源时长不足 1.2 秒、明显残句或 ASR 怪句的窗口，不得作为一章里的 Beat。第一章是开场，允许短，但不能是残句。
4. 同一章内不得出现两句在说同一件事（同义重复、换词重复、同一次结果重复证明）；只保留信息更具体的那一句。
5. 章节总数不得超过输入给你的章节数上限；已到上限时只把现有章节讲充分。
只返回用户指定的 JSON 字段，不输出思考过程。
【换行要求】每个字段、每个数组元素各占一行（不必缩进对齐，但必须有换行）；禁止把整个 JSON 挤在一行——单行超长 JSON 极易漏掉或多出括号，会让整轮结果作废。"""


DIRECTOR_PREFERRED_BEAT_MIN_SECONDS = 1.0
DIRECTOR_PREFERRED_BEAT_MAX_SECONDS = 5.0
DIRECTOR_LONG_COMPLETE_BEAT_MAX_SECONDS = 8.0

# ── 连续窗切分（方案 B，2026-09-25）──────────────────────────────
# 候选池从「逐句」改成「连续口播窗」：相邻 SRT 行（ID 连续、间隔不超过
# CAST_WINDOW_MAX_GAP_SECONDS、跨度不超过 CAST_WINDOW_MAX_SECONDS，或在
# 一句话收口时提前闭合）聚成一个 2-8 秒的窗。窗是本次选片的最小单位：
# 窗里每一行都必须按原顺序连续进入同一章的 beats，不得只取窗内一部分。
# 不足 CAST_WINDOW_MIN_SECONDS 秒的过短/碎片窗禁选：能并入邻窗就并，并不了的只作上下文。
# 单行但只要够长（2-8 秒完整口播，如 5-8 秒长完整句）仍可选。
# 把 CAST_WINDOW_POOL_ENABLED 设为 False 即逐字节回到旧的逐句池。
CAST_WINDOW_POOL_ENABLED = True
CAST_WINDOW_MIN_SECONDS = 2.0
CAST_WINDOW_MAX_SECONDS = 8.0
CAST_WINDOW_MAX_GAP_SECONDS = 1.0
CAST_WINDOW_CLOSE_SECONDS = 6.5
CAST_WINDOW_CLOSE_AT_SENTENCE_END_SECONDS = 2.2
CAST_WINDOW_MIN_ROWS = 1
# [v9.8] 收句边界：窗只允许在「已收句」的行后断开（减少半截话）；
# 为不断在句子中间，允许窗略超 CAST_WINDOW_MAX_SECONDS，但不超过硬上限。
# 置 False 即逐字节回到旧的「按跨度/时长无条件断窗」行为。
CAST_WINDOW_SENTENCE_SAFE_BREAK_ENABLED = True
CAST_WINDOW_HARD_MAX_SECONDS = 9.5
CAST_WINDOW_ABSOLUTE_MAX_SECONDS = 11.0

# ── 窗内坏行剔除（方案 2，2026-09-29）────────────────────────────
# 动机：窗内混着重复/尺码/水词时，AI 会把窗拆开只取窗内一部分，落盘就成了半句。
# 这里在切窗阶段用「确定性」规则把这些行标成只作上下文（不可选），窗内因此天然干净。
# 只做无需语义判断的排除（尺码问答/身高体重/直播互动/ASR畸形/主播口令/收尾废话/近似重复）；
# 不做语义改写、不动时间戳。置 False 即回到旧行为（窗内所有行都可选）。
CAST_WINDOW_ROW_PRUNE_ENABLED = True
CAST_WINDOW_ROW_PRUNE_DUP_RATIO = 0.60
CAST_WINDOW_ROW_PRUNE_DUP_MIN_LCS = 7
CAST_WINDOW_ROW_PRUNE_DUP_MIN_CHARS = 8
CAST_WINDOW_ROW_PRUNE_FILLER_MAX_CHARS = 12
CAST_WINDOW_ROW_PRUNE_COMMANDS = (
    "过掉吧", "过掉", "过一下", "往小排", "往大排",
    "没啥点解", "没点解的", "点解的", "下一位", "上一位",
)
CAST_WINDOW_ROW_PRUNE_FILLERS = (
    "没有了", "没啦", "没了", "真没了", "就没了", "断码了",
    "抓紧", "好快", "冲榜", "早拍早发", "我觉得你们",
)
CAST_WINDOW_ROW_PRUNE_VOCAL_CHARS = "嗯呃啊哦噢喔唉诶欸唔对是不好的行吧呀呢吗"

# ── 现场动作指令剔除（止血A，2026-10-06）────────────────────────────────
# 动机：末章收尾被「帮我翻一下 / 领子帽子往后翻」这类对着镜头做的动作指令占据 ——
# 它们是主播招呼镜头的口令，不是商品卖点，却因为词表缺口混进了片单。
# 做法：给「坏行」判据补上这一类；命中即不可选、不可补。置 False 即回旧行为。
CAST_WINDOW_LIVE_ACTION_GUARD_ENABLED = True
# 承接补全同样要过既有坏行判据（否则「已被判为语气词/口令的行」会从补句这条路绕进来）。
# 置 False 即回旧行为。
CAST_CONTINUATION_ROW_GUARD_ENABLED = True
_CAST_LIVE_ACTION_COMMANDS = (
    "翻一下", "往后翻", "往前翻", "放下来给你看", "拿起来给你看",
    "转一圈", "站近一点", "靠近一点", "往这看", "看镜头",
    # [止血D-2 2026-10-06] 借物 / 取物：与上面同类，都是「不看画面就读不通」的现场
    # 口令。跨 71 份素材穷举校准（拿过来 115 次 / 给我拿 53 次 / 拿一件 9 次 /
    # 再给我来、再给我一下 —— 逐条都是取物或现场演示）。
    # 故意不收「来外套」「再给我」：前者在「连两百都不用来外套」、后者在
    # 「不可以再给我讨价还价」这类正常口播里也出现，宁紧勿松。
    "拿过来", "给我拿", "拿一件", "再给我来", "再给我一下",
)
# 当众穿脱 / 搭配演示：允许「再 / 个」插入 —— 实测有「我再给你套一下这个外套」
# 「我给你再穿一下」这类带插入词的实句，写成固定子串会漏掉。
# 跨 71 份素材穷举：给你套 45 / 给你搭 41 / 给你穿 42 / 给你戴 2 / 给你搓 4 /
# 给你比 6，逐条都是现场演示；故不收「给你拿」（有 1 例「人家上门给你拿走了」是售后）。
_CAST_LIVE_ACTION_PATTERNS = (
    re.compile(r"给你[再个]?[套叠搭戴搓穿比]"),
)

# ── 口播话术 / 跨商品串场剔除（P0-1 + P0-2，2026-09-30）──────────────
# 动机：直播叫卖（确定要拍 / 我都讲了 / 专门给你们定的）、互动确认（看到没有 /
# 对不对）以及「马上要开下一款」这类串场句会被选进正文。
# 做法：在「导演前的候选池过滤」阶段把命中行整行标成只作上下文（不可选）；
# 不动正文行、不改时间戳、不新增任何 AI 调用。
# P0-1 是商品正确性问题，不受用户内容开关影响，永远生效；
# P0-2 跟随 live_interaction 开关（用户关掉时才拦）。置 False 即回到旧行为。
CAST_LIVE_PITCH_GUARD_ENABLED = True
CAST_OTHER_PRODUCT_SWITCH_GUARD_ENABLED = True

# ── 跨窗补齐 / 切口对齐 / 章尾半句门禁（P0-7 + P0-8 + P0-9，2026-10-01）────
# 动机：成片里「半句」的根因不是 AI 选句，而是窗的边界 —— 缺的那半句常常就在
# 隔壁窗里，而补齐只在本窗内找；「窗内剔坏行」又是用硬切窗实现的，切口会落在
# 句子中间（实测 G04 的 69 处切口里 41 处断在话没说完的地方）。
# P0-7：补齐范围放宽到相邻窗相邻的 1~2 行（其余约束一条不变）。
# P0-8：切开后的子段两端对齐到收句行；露出来的半句头/尾不再可选。
# P0-9：章尾停在半句、续句确实拿不到时收口（默认丢掉该行，不落半句）。
# 三者都是确定性动作：不改写、不重排、不新增 AI 调用。置 False 即回旧行为。
CAST_WINDOW_CROSS_FILL_ENABLED = False
# [止血C 2026-10-06] 补齐只在本窗内找，不再伸到邻窗（邻窗可能已换话题/是现场
# 指令，实测会把「帽子→颜色→又讲帽子」这类回跳补进片单）。置 True 即回旧行为。
# 补齐链条必须「走到收句行」才算补上：走不到就整条不补（否则只是把半句往后挪一行）。
CAST_WINDOW_SPLIT_REPAIR_CLOSED_ONLY_ENABLED = True
# 实测（G04，634 行源 / 568 行可执行池）：切口对齐对「窗边界落在句中」几乎没有
# 改善（缺头 34%→32%、缺尾 26%→26%），却把最终片单拖差（半句尾 5→10 处、可用素
# 材少 36 秒）—— 因为它把切口旁的半句头/尾变成「不在任何窗里」，AI 已经选中的
# 那几行再也补不回来。故默认关闭，代码与开关保留，需要时一行打开再评估。
CAST_WINDOW_ALIGNED_SPLIT_ENABLED = False
CAST_WINDOW_ALIGN_MAX_DROP_ROWS = 2
CAST_HALF_SENTENCE_TAIL_GUARD_ENABLED = True
# "drop" = 丢掉这个半句尾巴（默认：宁可少一句，也不落半句）；"keep" = 只报告。
CAST_HALF_SENTENCE_TAIL_MODE = "drop"

# ── 拆窗修补（P0-1，2026-09-30）────────────────────────────────
# 动机：AI 有时只取一个连续口播窗的一部分（拆窗），落盘就从半句开始或停在半句。
# 这里在选句之后做「确定性」补齐：只按原顺序把窗内缺的那几行补回，
# 不改写、不重排、不追加任何 AI 调用。补不动就如实记审计，不静默。
# 置 False 即回到旧行为（拆窗只报告、不补）。
CAST_WINDOW_SPLIT_REPAIR_ENABLED = True
# 上限从 2 行 / 4 秒放宽到 3 行 / 6 秒：补齐必须「走到收句行」才算补上，2 行基本
# 走不到收句行（G04 有 11 条链条因此作废）。实测放宽后最终片单的半句尾 8→5 处，
# 同时段数更多（源秒 233.6→244.2）—— 是「补完一句话」而不是「多拉几行」。
CAST_WINDOW_SPLIT_REPAIR_MAX_ROWS = 3
CAST_WINDOW_SPLIT_REPAIR_MAX_SECONDS = 6.0
# 补齐绝不越过内容策略：候选行只要是价格 / 链接 / CTA / 来源主张这类会被下游
# 屏蔽的内容，就整处放弃补齐（记 skipped），而不是把屏蔽内容又拉回片单。
CAST_WINDOW_SPLIT_REPAIR_MIN_ROW_SECONDS = 0.5
CAST_WINDOW_SPLIT_REPAIR_BLOCK_PATTERN = re.compile(
    r"[0-9零一二三四五六七八九十百千万两]+\s*(?:多|来|几)?\s*(?:块钱|块|元)"
    r"|价格|原价|现价|多少钱|涨价|降价|优惠|折扣|券|包邮|下单|拍下|购物车|链接"
    r"|官旗|官网|旗舰店|到手价|同款|老贵|大几千|百块|便宜|划算"
)


# ── 整窗整取（2026-09-30）──────────────────────────────────────────────
# 动机：AI 常常只取一个连续口播窗的一部分。实测（混剪计划 12:04）：42 句只来自
# 17 个窗，命中窗共 66 行却只取走 41 行 —— 白丢 25 行（38%），而丢掉的恰好是
# 承接句（Hook 的兑现句、回答上一句的那一句）。落盘就成了格言集，读着跳。
# 做法：AI 选中的窗，把窗内剩下的行按原顺序补回来。确定性；零 AI 调用；
# 不改写、不重排；绝不删改 AI 原本选中的行 —— 整取只是加分项。
# 补进来的行受时长契约上界（source_max）约束，超了就按「离已选行远 → 近」少补。
# 不越过内容策略：价格/CTA 与坏行一律不补。
# 置 False 即回到旧行为（只做 P0-1 拆窗修补）。
CAST_WINDOW_WHOLE_KEEP_ENABLED = True
# 补进来的行不足这么多秒、且本身不是收句时，不补（免得补成水词）。
CAST_WINDOW_WHOLE_KEEP_MIN_ROW_SECONDS = 0.5
# 整取预算 = 契约目标源时长 × 这个系数（1.0 = 收到目标时长；上调可放更多内容，
# 但不得超过 source_max，否则方案会掉出时长区间）。
CAST_WINDOW_WHOLE_KEEP_BUDGET_RATIO = 1.0
# 首尾门禁用它判断「收尾章有没有收尾动作」；只做提示，不做硬判、不改句。
CAST_WHOLE_KEEP_CLOSE_ACTION_PATTERN = re.compile(
    r"关注|点关注|下单|拍下|加购|购物车|小黄车|尺码|选码|库存|活动|优惠|斤|几号|多大"
)

# [止血D 2026-10-06] 整窗补齐不得跨章抢句：同一方案里已被别的章选中的行，不算本章
# 窗内「缺的行」。实测（紫星云 11:25 轮）：M2 把 W157 的 617/618 放在 C3，程序整窗
# 补齐又把 W157 的尾巴整段（615-618）补进 C1 —— 程序自己造出跨章重复，随后去重按
# 「首次出现」保留 C1、把 C3 清空成 0 句空壳章。补进来的行只保证字面相邻、不保证
# 属于本章。这里按「归属章唯一」收回程序的越界动作。置 False 即回旧行为。
CAST_WHOLE_KEEP_CHAPTER_EXCLUSIVE_ENABLED = True
# [止血D 2026-10-06] 去重不得把整章删空：若某章的 beat 全部是前章重复，保底保留该章
# 第一拍（宁可这一章短，也不留一个 0 句的空壳章）。置 False 即回旧行为。
CAST_DEDUP_CHAPTER_FLOOR_ENABLED = True


# ── 首尾门禁（2026-09-30 P0-A / P0-B）──────────────────────────────────
# 动机（实测 14:07 混剪跑批）：AI 明明把开场 Hook 声明成 #26，程序却因为
# 「同窗连续」把 AI 自己判为偏弱的备选句 #25「好看吧，」补到了全片第一句；
# 系统检测到了「开场回执与实际首章不一致」，但按设计只警告、不替换。
# 开头是唯一有 AI 明确声明的位置 —— 程序补行只有让连读前缀仍与声明一致，
# 才允许排在第一位；否则整句不补（记审计）。
CAST_OPENING_PREFIX_GUARD_ENABLED = True
# 章首/章尾收口：章尾停在半句时，优先在同一口播窗内补到「收句行」为止；
# 补不动、又只是一句 ≤1.2s 的水词/残句时，从章内移出，让边界回退到上一个
# 完整句。只动章首/章尾，不改写、不重排、不动章内中间。置 False 回到旧行为。
CAST_CHAPTER_EDGE_GUARD_ENABLED = True
CAST_CHAPTER_EDGE_STUB_SECONDS = 1.2
# 去标点后不超过这么多字才算是「纯语气词残句」（避免误伤有实义的短句）。
CAST_CHAPTER_EDGE_STUB_MAX_CHARS = 3
# 章首缺上文补齐（P0-10）：某一章的第一句是以「因为 / 然后 / 再加上 …」这类接续
# 连词起头的从句，而它的上一句没被任何章选中时 —— 观众从章首听到的就是半截话。
# 处理：把上一句（有界 1 行；仍需「可执行、未被别章用掉、不是坏行」）补到章首；
# 补不到就如实报告，绝不移出、不删句、不改写、不重排。
# 第一章（开场）不参与 —— 它由 AI 的开场声明 + 开场独立成段负责。
# 置 False 与落地前逐字节等价。
CAST_CHAPTER_HEAD_FILL_ENABLED = True
CAST_CHAPTER_HEAD_FILL_MAX_ROWS = 1
# 只在「几乎一定是承接上一句」的连词上触发，避免把能独立站的短句误判。
CAST_CHAPTER_HEAD_DEPENDENT_PREFIXES = (
    "因为", "所以", "然后", "再加上", "加上", "再有", "而且", "但是", "比如说", "还是",
)


# [乙-bis · 2026-10-04] 开场「半截话」确定性硬门。
# 现象：AI 选中的开场钩子句以承接连词开头（因为/所以/然后/而且/但是…），而它的
#      上一句并不在可选池里 → 观众从第一秒听到的就是半截话。
# 做法：从 AI 同一次返回的 compared_packages 里挑第一个「干净」候选（钩子句不以
#      承接连词开头、句 id 在可选池里、且全片没被别处用过），只替换首章第一拍
#      的钩子句；兑现拍、章节次序、其余章节一律不动。
#      0 新增 AI 调用；不改写任何原文；不删章节。
# 置 False 与落地前逐字节等价。
OPENING_LEADIN_VETO_ENABLED = True
# 复用「章首补行」的同一组承接连词，再并入「开场自带口水铺垫/口头禅」。
# [A · 2026-10-05] 原先只认承接连词（半截话），抓不到「你会发现你看…」这类
# 首字即噪声的句子。「依赖上文的半截话」和「自带口水铺垫」都不适合当开场第一句，
# 所以共用同一张触发表。
OPENING_LEADIN_VETO_PREFIXES = CAST_CHAPTER_HEAD_DEPENDENT_PREFIXES + (
    "你会发现", "你看", "咱们就是", "就是", "我跟你讲", "我跟你说", "说实话",
    "就是说", "所以说", "其实", "那么", "呃", "嗯",
)
# 替换候选只用「承接连词」这张更严的表：候选即使带一层口头禅，也好过原来的
# 半截话/纯口水铺垫，且它来自池内、AI 已判为可用开场，不该因为口头禅被否掉。
OPENING_REPLACEMENT_BLOCKED_PREFIXES = CAST_CHAPTER_HEAD_DEPENDENT_PREFIXES


def _post_two_pass_director_request_with_retry(*, max_tokens: int, **kwargs: Any) -> str:
    """One bounded retry for transient Director failures.

    ``output_truncated`` (the response hit the ceiling) and timeouts are the
    two dominant preview failures. Both are retried exactly once with the
    ceiling doubled; the provider only charges what it actually returns, so a
    higher ceiling is not a prepaid cost. Other errors propagate unchanged.
    """

    try:
        return _post_two_pass_director_request(max_tokens=max_tokens, **kwargs)
    except AnalyzerError as exc:
        text = str(exc)
        if "output_truncated" not in text and "token 上限" not in text:
            raise
    except (TimeoutError, OSError):
        text = "timeout"
    retry_tokens = max(int(max_tokens) * 2, int(max_tokens) + 2000)
    # 只做一次有界重试；这里不打日志（该函数在模块级，取不到调用方的局部 logger）。
    return _post_two_pass_director_request(max_tokens=retry_tokens, **kwargs)


def director_casting_output_max_tokens(director_plan_count: int | None, target_duration: float | None = None) -> int:
    """Reserve enough room for one compact, executable Casting receipt.

    A single 60-second story can still require many one-to-five-second spoken
    Beats.  The cap is only a response ceiling, not a target or a prepaid
    amount: the provider charges the tokens it actually returns.  Two and
    three plans retain the established 4k-per-plan budget, while the one-plan
    path receives a small JSON-completion margin instead of failing after M1
    has already been paid for.
    """
    try:
        plan_count = max(1, min(3, int(director_plan_count or 1)))
    except (TypeError, ValueError):
        plan_count = 1
    base = max(7000, 5500 * plan_count)
    # The output cap must scale with the requested duration: a 90/120s story needs
    # far more one-to-five-second Beats than a 60s one.  Still a ceiling, not prepaid.
    try:
        scale = max(0.6, min(4.0, float(target_duration or 60.0) / 60.0))
    except (TypeError, ValueError):
        scale = 1.0
    return int(base * scale)


def director_casting_request_timeout(model: str, requested_timeout: int | float | None) -> int:
    """Return a bounded read timeout for the final Director selection call.

    Seed can spend substantial time reasoning before returning a compact JSON
    receipt. The story-contract call has already completed by this point, so
    timing out Casting at the generic three-minute floor wastes that completed
    work. Do not retry a read timeout: the provider may still have executed
    the request, and replaying it could charge twice.
    """
    try:
        timeout = int(requested_timeout or 0)
    except (TypeError, ValueError):
        timeout = 0
    timeout = max(180, timeout)
    if "seed" in str(model or "").lower():
        return max(360, timeout)
    return timeout


def _cast_row_closes_thought(row: Mapping[str, Any]) -> bool:
    """这一行是否已经「收句」（以句末标点结尾）。

    窗的边界只允许落在收句的行之后，否则就会把一句话断在中间（半截话）。
    """
    return bool(re.search(r"[。！？!?…]$", str(row.get("text") or "").strip()))


def _cast_window_partition(rows: Sequence[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
    """把逐句候选行聚成连续口播窗（确定性；不做任何语义选择）。

    只做一件事：把「ID 连续、间隔很小」的相邻口播行合成一个能被整段听懂的
    单位，用于让导演按「窗」而不是按「单句」做取舍。

    [v9.8 收句边界] CAST_WINDOW_SENTENCE_SAFE_BREAK_ENABLED=True 时，窗的边界
    只允许落在「已经收句」的行之后：一旦跨度/时长把窗逼到要断，就先回退到窗内
    最后一个收句行，把它后面的行留给下一个窗继续攒 —— 绝不把一句话断在中间。
    半截话几乎都来自「断在句中的窗」，这是根治点。
    """
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for row in rows:
        if current:
            try:
                gap = float(row["start"]) - float(current[-1]["end"])
                projected = float(row["end"]) - float(current[0]["start"])
            except (KeyError, TypeError, ValueError):
                gap, projected = 0.0, 0.0
            ceiling = (CAST_WINDOW_HARD_MAX_SECONDS if CAST_WINDOW_SENTENCE_SAFE_BREAK_ENABLED
                       else CAST_WINDOW_MAX_SECONDS)
            forced = (int(row["id"]) != int(current[-1]["id"]) + 1
                      or gap > CAST_WINDOW_MAX_GAP_SECONDS
                      or projected > ceiling)
            if forced:
                if (CAST_WINDOW_SENTENCE_SAFE_BREAK_ENABLED
                        and projected <= CAST_WINDOW_ABSOLUTE_MAX_SECONDS):
                    cut = 0
                    for index in range(len(current) - 1, 0, -1):
                        if _cast_row_closes_thought(current[index - 1]):
                            cut = index
                            break
                    if cut:
                        windows.append(current[:cut])
                        current = current[cut:]
                else:
                    windows.append(current)
                    current = []
        current.append(row)
        try:
            duration = float(current[-1]["end"]) - float(current[0]["start"])
        except (KeyError, TypeError, ValueError):
            duration = 0.0
        closes_thought = _cast_row_closes_thought(current[-1])
        if CAST_WINDOW_SENTENCE_SAFE_BREAK_ENABLED:
            if closes_thought and (duration >= CAST_WINDOW_CLOSE_SECONDS
                                   or duration >= CAST_WINDOW_CLOSE_AT_SENTENCE_END_SECONDS):
                windows.append(current)
                current = []
        elif (duration >= CAST_WINDOW_CLOSE_SECONDS
                or (duration >= CAST_WINDOW_CLOSE_AT_SENTENCE_END_SECONDS and closes_thought)):
            windows.append(current)
            current = []
    if current:
        windows.append(current)
    return windows


def _cast_window_is_undersized(window: Sequence[Mapping[str, Any]]) -> bool:
    """只按「够不够长」判定：不足 CAST_WINDOW_MIN_SECONDS 的窗（多为半句/碎片）禁选。

    单独的完整长句（2-8 秒，例如 5-8 秒的长完整口播）保留可选——那是既有产品规则
    “long_complete_exception”；被禁的是真正讲不完整、只能靠拼句成立的那种窗。
    """
    try:
        duration = float(window[-1]["end"]) - float(window[0]["start"])
    except (KeyError, TypeError, ValueError):
        return False
    return duration < float(CAST_WINDOW_MIN_SECONDS)


def _merge_undersized_windows(
    windows: Sequence[Sequence[Mapping[str, Any]]],
) -> list[list[dict[str, Any]]]:
    """把单句窗/过短窗并入相邻窗（合并后不超过 CAST_WINDOW_MAX_SECONDS）。"""
    acc: list[list[dict[str, Any]]] = []
    for window in windows:
        item = [dict(row) for row in window]
        if _cast_window_is_undersized(item) and acc:
            previous = acc[-1]
            try:
                merged_seconds = float(item[-1]["end"]) - float(previous[0]["start"])
            except (KeyError, TypeError, ValueError):
                merged_seconds = float(CAST_WINDOW_MAX_SECONDS) + 1.0
            if (int(item[0]["id"]) == int(previous[-1]["id"]) + 1
                    and merged_seconds <= float(CAST_WINDOW_MAX_SECONDS)):
                previous.extend(item)
                continue
        acc.append(item)
    out: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(acc):
        window = acc[index]
        if _cast_window_is_undersized(window) and index + 1 < len(acc):
            following = acc[index + 1]
            try:
                merged_seconds = float(following[-1]["end"]) - float(window[0]["start"])
            except (KeyError, TypeError, ValueError):
                merged_seconds = float(CAST_WINDOW_MAX_SECONDS) + 1.0
            if (int(following[0]["id"]) == int(window[0]["id"]) + 1
                    and merged_seconds <= float(CAST_WINDOW_MAX_SECONDS)):
                following.insert(0, window[0])
                index += 1
                continue
        out.append(window)
        index += 1
    return out


_CAST_ROW_PRUNE_PUNCT = frozenset("，。！？、；：,.!?;:…~～—-－‘’“”\"'()（）")


def _cast_row_prune_compact(text: str) -> str:
    """去掉标点/空白，便于按字符做确定性比较。"""
    return "".join(
        ch for ch in str(text or "")
        if ch not in _CAST_ROW_PRUNE_PUNCT and not ch.isspace()
    )


_CAST_OTHER_PRODUCT_SWITCH_PATTERNS = (
    # 切场口播多半出现在行首几个字里
    re.compile(r"^.{0,6}(?:马上要开|马上就要开|下一个|下一款|换一款|换个款|接下来)"),
    re.compile(r"(?:这|那)(?:件|款|个)(?:讲完|讲过了)"),
)


def other_product_switch_reason(text: Any) -> str:
    """Return why a row is a "cut to the next item" live-room line.

    这不是内容偏好，是商品正确性问题：句子指向的是别的商品，不能进正文。
    只做确定性判定，不改写文本、不动时间戳。
    """

    value = _cast_row_prune_compact(str(text or ""))
    if not value:
        return ""
    for pattern in _CAST_OTHER_PRODUCT_SWITCH_PATTERNS:
        if pattern.search(value):
            return "跨商品串场"
    return ""


def _cast_row_prune_reason(row: Mapping[str, Any]) -> str:
    """确定性判定：这一行是否属于「只作上下文」的废行。

    复用现成硬分类器，不做任何语义判断：尺码问答 / 个人身高体重 / 直播互动
    （selection_safety）、ASR 畸形句（final_utterance_surface_issue）、
    主播口令、整行很短时的收尾废话、纯语气词。
    """
    text = str(row.get("text") or "").strip()
    if not text:
        return "空行"
    reason = live_interaction_or_size_response_reason(text)
    if reason:
        return reason
    reason = final_utterance_surface_issue(text)
    if reason:
        return reason
    for command in CAST_WINDOW_ROW_PRUNE_COMMANDS:
        if command in text:
            return "主播口令"
    if CAST_WINDOW_LIVE_ACTION_GUARD_ENABLED:
        for command in _CAST_LIVE_ACTION_COMMANDS:
            if command in text:
                return "现场动作指令"
        for pattern in _CAST_LIVE_ACTION_PATTERNS:
            if pattern.search(text):
                return "现场动作指令"
    compact = _cast_row_prune_compact(text)
    if len(compact) <= int(CAST_WINDOW_ROW_PRUNE_FILLER_MAX_CHARS):
        for filler in CAST_WINDOW_ROW_PRUNE_FILLERS:
            if filler in text:
                return "收尾废话"
        if compact and all(ch in CAST_WINDOW_ROW_PRUNE_VOCAL_CHARS for ch in compact):
            return "纯语气词"
    return ""


def _cast_row_shingles(text: str, size: int = 3) -> frozenset:
    """字符 n-gram 集合：只用来快速跳过不相似的配对，避免 O(n^2) 全量算 LCS。"""
    compact = _cast_row_prune_compact(text)
    if len(compact) < size:
        return frozenset()
    return frozenset(compact[index:index + size] for index in range(len(compact) - size + 1))


def _cast_row_longest_common_span(left: str, right: str) -> int:
    """最长公共子串长度。"""
    first, second = left, right
    if len(first) > len(second):
        first, second = second, first
    best = 0
    previous = [0] * (len(second) + 1)
    for index in range(1, len(first) + 1):
        current = [0] * (len(second) + 1)
        head = first[index - 1]
        for position in range(1, len(second) + 1):
            if head == second[position - 1]:
                value = previous[position - 1] + 1
                current[position] = value
                if value > best:
                    best = value
        previous = current
    return best


def _cast_row_duplicate_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """两行是否近似重复：较长公共子串几乎覆盖较短那一行。

    刻意不用「字符集重合/Jaccard」—中文口播里不同内容共享大量用字，那样会误杀。
    这里要求：两行都要够长、公共子串本身够长、且占较短行的比例够高。
    """
    first = _cast_row_prune_compact(str(left.get("text") or ""))
    second = _cast_row_prune_compact(str(right.get("text") or ""))
    if len(first) > len(second):
        first, second = second, first
    if len(first) < int(CAST_WINDOW_ROW_PRUNE_DUP_MIN_CHARS):
        return False
    span = _cast_row_longest_common_span(first, second)
    if span < int(CAST_WINDOW_ROW_PRUNE_DUP_MIN_LCS):
        return False
    return span / float(len(first)) >= float(CAST_WINDOW_ROW_PRUNE_DUP_RATIO)


def _cast_row_keep_priority(row: Mapping[str, Any]) -> tuple:
    """近似重复里留谁：先收句、再更长、最后取更靠前（保证确定性）。"""
    text = str(row.get("text") or "").strip()
    closes = 1 if re.search(r"[。！？!?…]$", text) else 0
    try:
        sid = int(row["id"])
    except (KeyError, TypeError, ValueError):
        sid = 0
    return (closes, len(_cast_row_prune_compact(text)), -sid)


def _prune_cast_window_rows(rows: Sequence[Mapping[str, Any]]) -> list[int]:
    """返回「只作上下文」的行 ID：废行 + 近似重复里较弱的那一行。

    近似重复在整份候选池上判（不限窗内），这样跨窗重复也会收敛到更完整的那一行，
    AI 不再有「拆窗取其一」的动机。
    """
    pruned: set[int] = set()
    kept: list[dict[str, Any]] = []
    for row in rows:
        try:
            sid = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if _cast_row_prune_reason(row):
            pruned.add(sid)
        else:
            kept.append(dict(row))
    if not kept:
        return []
    shingles = {int(row["id"]): _cast_row_shingles(str(row.get("text") or "")) for row in kept}
    survivors: list[dict[str, Any]] = []
    for row in kept:
        rid = int(row["id"])
        grams = shingles.get(rid) or frozenset()
        replaced = False
        for position, other in enumerate(survivors):
            oid = int(other["id"])
            if not grams or not (grams & (shingles.get(oid) or frozenset())):
                continue
            if not _cast_row_duplicate_pair(row, other):
                continue
            if _cast_row_keep_priority(row) > _cast_row_keep_priority(other):
                pruned.add(oid)
                survivors[position] = row
            else:
                pruned.add(rid)
            replaced = True
            break
        if not replaced:
            survivors.append(row)
    return sorted(pruned)


def _split_windows_around_blocked(
    windows: Sequence[Sequence[Mapping[str, Any]]],
    blocked: set[int],
) -> list[list[dict[str, Any]]]:
    """摘掉窗内被剔除的行；剩余按 ID 连续切成子段（各成一个小窗）。

    这样「窗内 ID 必须连续」这个 Prompt 契约始终成立（剔行可能把窗打通一个洞）。

    [P0-8 切口对齐] CAST_WINDOW_ALIGNED_SPLIT_ENABLED=True 时，切口额外对齐到
    「已经收句」的行：切开后露出来的半句头/半句尾不再可选（那几行单独出现本来
    也讲不完整）。实测坏行切口正是「窗边界落在句中」的主要制造者。
    """
    out: list[list[dict[str, Any]]] = []
    for window in windows:
        for run in _cast_window_runs(window, blocked):
            if CAST_WINDOW_ALIGNED_SPLIT_ENABLED:
                run = _align_cast_window_run(window, run)
            if run:
                out.append(run)
    return out


def _cast_window_runs(
    window: Sequence[Mapping[str, Any]],
    blocked: set[int],
) -> list[list[dict[str, Any]]]:
    """把一个窗按「被剔除的行 / ID 断点」切成若干连续子段（尚未对齐）。"""
    runs: list[list[dict[str, Any]]] = []
    run: list[dict[str, Any]] = []
    for row in window:
        try:
            sid = int(row["id"])
        except (KeyError, TypeError, ValueError):
            sid = None
        if sid in blocked:
            if run:
                runs.append(run)
                run = []
            continue
        if run and sid is not None and sid != int(run[-1]["id"]) + 1:
            runs.append(run)
            run = []
        run.append(dict(row))
    if run:
        runs.append(run)
    return runs


def _align_cast_window_run(
    window: Sequence[Mapping[str, Any]],
    run: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把切开后的子段两端对齐到「已收句」的行。

    左边：子段第一行在原窗里的前一行话没说完 → 这段是从半句开始的，丢掉开头几行；
    右边：子段最后一行话没说完 → 停在半句上，丢到最近一个收句行。
    每侧最多丢 CAST_WINDOW_ALIGN_MAX_DROP_ROWS 行，且至少留 1 行 —— 只处理贴着
    切口的那几行，不做任何语义判断，也不会为了对齐把整段砍光。
    """
    limit = max(0, int(CAST_WINDOW_ALIGN_MAX_DROP_ROWS))
    if not run or limit == 0:
        return run
    index_by_id: dict[int, int] = {}
    for position, row in enumerate(window):
        try:
            index_by_id[int(row["id"])] = position
        except (KeyError, TypeError, ValueError):
            continue
    head_drop = 0
    while head_drop < limit and len(run) - head_drop > 1:
        try:
            position = index_by_id[int(run[head_drop]["id"])]
        except (KeyError, TypeError, ValueError):
            break
        if position <= 0 or not _cast_row_looks_unfinished(window[position - 1]):
            break
        head_drop += 1
    tail_drop = 0
    while tail_drop < limit and len(run) - head_drop - tail_drop > 1:
        if _cast_row_closes_thought(run[-1 - tail_drop]):
            break
        tail_drop += 1
    aligned = run
    if tail_drop:
        aligned = aligned[:len(aligned) - tail_drop]
    if head_drop:
        aligned = aligned[head_drop:]
    # 对齐不许把窗砍到过小：对齐后若反而变成「不足下限的碎片」，这个碎片会被整体
    # 丢弃（整段内容变成不可选）—— 那比留一个半句更亏。此时放弃对齐，保留原样。
    if aligned and _cast_window_is_undersized(aligned) and not _cast_window_is_undersized(run):
        return run
    return aligned


def _cast_windows_for_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[int]]:
    """返回（窗统计行, 因禁选而只作上下文的 ID）。

    窗统计行每行代表一个可选的完整口播窗，字段沿用行结构（id/start/end/text），
    额外带 window_id / window_ids / window_row_count，供候选池渲染与秒数统计共用。
    """
    windows = _cast_window_partition(rows)
    if int(CAST_WINDOW_MIN_ROWS) > 1 or float(CAST_WINDOW_MIN_SECONDS) > 0:
        windows = _merge_undersized_windows(windows)
    # 方案 2：切窗阶段确定性剔除「只作上下文」的行（尺码/互动/口令/畸形 + 近似重复）。
    # 窗内因此天然干净，AI 不再有「拆窗取其一」的动机（拆窗落盘会变成半句）。
    context_only_ids: list[int] = []
    if CAST_WINDOW_ROW_PRUNE_ENABLED and rows:
        context_only_ids = _prune_cast_window_rows(rows)
        if context_only_ids:
            windows = _split_windows_around_blocked(windows, set(context_only_ids))
    window_rows: list[dict[str, Any]] = []
    dropped_ids: list[int] = list(context_only_ids)
    for window in windows:
        if _cast_window_is_undersized(window):
            dropped_ids.extend(int(row["id"]) for row in window)
            continue
        text = " ".join(str(row.get("text") or "").strip() for row in window).strip()
        try:
            start = float(window[0]["start"])
            end = float(window[-1]["end"])
        except (KeyError, TypeError, ValueError):
            start = end = 0.0
        window_rows.append({
            "id": int(window[0]["id"]),
            "start": start,
            "end": end,
            "text": text,
            "window_id": "W%03d" % (len(window_rows) + 1),
            "window_ids": [int(row["id"]) for row in window],
            "window_row_count": len(window),
            "window_rows": [dict(row) for row in window],
        })
    return window_rows, dropped_ids


def _format_cast_window_line(window: Mapping[str, Any]) -> str:
    """一个窗一行：窗号 + 完整 ID 段 + 时长（+ 非 clean 标记）+ 全窗原话。"""
    ids = [int(value) for value in (window.get("window_ids") or ())]
    id_label = str(ids[0]) if len(ids) <= 1 else f"{ids[0]}-{ids[-1]}"
    try:
        duration = float(window["end"]) - float(window["start"])
    except (KeyError, TypeError, ValueError):
        duration = 0.0
    grain = "[long_complete_exception]" if duration > float(DIRECTOR_PREFERRED_BEAT_MAX_SECONDS) else ""
    text = str(window.get("text") or "").strip()
    flags = ""
    surface = final_utterance_surface_issue(text) or "clean"
    if surface != "clean":
        flags += f"[!surface={surface}]"
    try:
        opening = director_opening_input_issue(text) or "clean"
    except Exception:  # noqa: BLE001 - 开场标记失败不得影响候选池
        opening = "clean"
    if opening != "clean":
        flags += f"[!opening={opening}]"
    detail = _director_casting_transcript(window.get("window_rows") or ())
    detail_inline = " ｜".join(part for part in detail.split("\n") if part.strip())
    return (
        f"[{window.get('window_id') or 'W???'}][IDs {id_label}][{duration:.2f}s]{grain}{flags} "
        f"｜{detail_inline}"
    )


def _director_window_pool_lines(
    subtitles: Sequence[Mapping[str, Any]],
    window_rows: Sequence[Mapping[str, Any]],
) -> str:
    """按原片顺序渲染窗池；未入选的行（禁选/不可执行）只作上下文。"""
    by_first_id = {int(row["id"]): row for row in window_rows}
    member_ids = {sid for row in window_rows for sid in (row.get("window_ids") or ())}
    lines: list[str] = []
    for row in subtitles:
        if not str(row.get("id", "")).isdigit() or not row.get("text"):
            continue
        sid = int(row["id"])
        window = by_first_id.get(sid)
        if window is not None:
            lines.append(_format_cast_window_line(window))
        elif sid in member_ids:
            continue
        else:
            lines.append(
                f"[context ID {sid:03d}][不可选，仅核对商品指代] {row.get('text', '')}"
            )
    return "\n".join(lines)


def _windows_total_seconds(window_rows: Sequence[Mapping[str, Any]]) -> float:
    return round(sum(max(0.0, float(row["end"]) - float(row["start"])) for row in window_rows), 3)


def _windows_seconds_for_ids(
    window_rows: Sequence[Mapping[str, Any]],
    subtitle_ids: Sequence[Any],
) -> float:
    wanted = set()
    for value in subtitle_ids or ():
        try:
            wanted.add(int(value))
        except (TypeError, ValueError):
            continue
    total = 0.0
    for row in window_rows:
        members = {int(value) for value in (row.get("window_ids") or ())}
        if members & wanted:
            total += max(0.0, float(row["end"]) - float(row["start"]))
    return round(total, 3)


def _director_casting_rows(
    subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None,
) -> list[dict[str, Any]]:
    """Return the full short-Beat pool without making a semantic decision.

    Keep sub-second fragments available to complete adjacent utterances.
    Eligibility does not imply that a subtitle is a complete spoken sentence.
    """
    return [
        row for row in _director_executable_subtitles(subtitles, executable_subtitle_ids)
        if 0 < float(row["end"] - row["start"])
        <= DIRECTOR_LONG_COMPLETE_BEAT_MAX_SECONDS
    ]


def _director_story_transcript(rows: Sequence[Mapping[str, Any]]) -> str:
    """Compact full-pool transcript for story direction.

    The first Director needs every real utterance, but it does not need source
    timestamps or duration labels because it is forbidden from selecting IDs.
    Keeping only ``ID + text`` preserves full-pool understanding while avoiding
    thousands of repeated formatting tokens.
    """
    return "\n".join(
        f"[ID {int(row['id']):03d}] {row['text']}"
        for row in rows
    )


def _director_product_context(
    subtitles: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    include_nonselectable_context: bool = True,
    include_duration: bool = True,
) -> str:
    """Build exact-ID casting context without making excluded rows selectable.

    Excluded rows can name a garment or contradict a nearby selling claim.
    Selection policy must not erase that evidence. Context stays in source
    order and cannot become a playable ID merely by appearing in this view.
    """
    selectable = {int(row["id"]): row for row in rows}
    lines = []
    for row in subtitles:
        if not str(row.get("id", "")).isdigit() or not row.get("text"):
            continue
        sid = int(row["id"])
        # Keep switches and their following pronouns adjacent. Appending only
        # excluded rows after the safe pool destroys the source chronology.
        if sid in selectable:
            formatter = _director_casting_transcript if include_duration else _director_story_transcript
            lines.append(formatter([selectable[sid]]))
        elif include_nonselectable_context:
            lines.append(f"[context ID {sid:03d}][不可选，仅核对商品指代] {row.get('text', '')}")
    return "\n".join(lines)


def _director_casting_transcript(rows: Sequence[Mapping[str, Any]]) -> str:
    """Compact exact-ID inventory for the semantic Casting call.

    Absolute source timestamps are deterministic program data and are not part
    of the semantic choice.  Duration stays visible so the AI can keep the
    1-5 second rhythm and recognise the bounded 5-8 second exception.
    """
    lines: list[str] = []
    for row in rows:
        duration = float(row["end"] - row["start"])
        grain = (
            "" if duration <= DIRECTOR_PREFERRED_BEAT_MAX_SECONDS
            else "[long_complete_exception]"
        )
        lines.append(
            f"[ID {int(row['id']):03d}][{duration:.2f}s]{grain} {row['text']}"
        )
    return "\n".join(lines)


def _director_executable_subtitles(
    subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None,
) -> list[dict[str, Any]]:
    """Return the complete mechanically executable subtitle rows in source order."""
    executable_ids: set[int] = set()
    for value in executable_subtitle_ids or ():
        try:
            subtitle_id = int(value)
        except (TypeError, ValueError):
            continue
        if subtitle_id > 0:
            executable_ids.add(subtitle_id)
    rows: list[dict[str, Any]] = []
    for position, subtitle in enumerate(subtitles, 1):
        try:
            subtitle_id = int(subtitle.get("id") or subtitle.get("index") or position)
            start = float(subtitle.get("start") or 0.0)
            end = max(start, float(subtitle.get("end") or start))
        except (AttributeError, TypeError, ValueError):
            continue
        if executable_subtitle_ids is not None and subtitle_id not in executable_ids:
            continue
        text = str(subtitle.get("text") or "").strip()
        if not text or end <= start:
            continue
        rows.append({"id": subtitle_id, "start": start, "end": end, "text": text})
    return rows


def director_target_duration_range(
    target_duration: float,
    duration_tolerance: float | None = None,
) -> dict[str, float | str]:
    """Return the exact UI duration contract used by both AI calls and M2.

    Automatic tolerance keeps the established 80%-110% delivery band.  An
    explicit UI tolerance is interpreted literally as +/- seconds.  The
    normal commercial-director product range remains 30-120 seconds.
    """
    requested = min(120.0, max(30.0, float(target_duration or 60.0)))
    if duration_tolerance is None:
        low = requested * 0.80
        high = requested * 1.10
        mode = "automatic_80_to_110_percent"
    else:
        tolerance = max(0.0, float(duration_tolerance))
        low = requested - tolerance
        high = requested + tolerance
        mode = "explicit_plus_minus_seconds"
    return {
        "requested_seconds": round(requested, 3),
        "preferred_low": round(max(30.0, low), 3),
        "preferred_high": round(min(120.0, high), 3),
        "tolerance_mode": mode,
    }


def director_delivery_duration_range(
    target_duration: float,
    duration_tolerance: float | None = None,
    output_speed_factor: float = 1.0,
) -> dict[str, Any]:
    """Use the export contract: the user's seconds are final, not source time."""
    from selection_contracts import DurationContract

    contract = DurationContract.create(
        target_duration, output_speed_factor, tolerance=duration_tolerance,
    )
    values = {
        **contract.to_dict(),
        "requested_seconds": contract.final_target,
        "preferred_low": contract.final_min,
        "preferred_high": contract.final_max,
        "tolerance_mode": "export_duration_contract",
    }
    return {key: round(value, 3) if isinstance(value, float) else value for key, value in values.items()}


def _two_pass_primary(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_strategies = payload.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        raw_strategies = (raw_strategies,)
    first_strategy: dict[str, Any] | None = None
    for item in raw_strategies:
        if not isinstance(item, Mapping):
            continue
        if first_strategy is None:
            first_strategy = dict(item)
        role = str(item.get("director_plan_role") or item.get("plan_role") or "primary").lower()
        if role == "primary":
            return dict(item)
    if first_strategy is not None:
        return first_strategy
    raw_primary = payload.get("primary_story") or payload.get("primary")
    return dict(raw_primary) if isinstance(raw_primary, Mapping) else {}


def _two_pass_beat_rows(primary: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    raw_chapters = primary.get("chapter_packets") or primary.get("chapters") or ()
    if isinstance(raw_chapters, Mapping):
        raw_chapters = (raw_chapters,)
    for chapter in raw_chapters:
        if not isinstance(chapter, Mapping):
            continue
        raw_beats = chapter.get("beats") or chapter.get("director_beats") or ()
        if isinstance(raw_beats, Mapping):
            raw_beats = (raw_beats,)
        rows.extend(item for item in raw_beats if isinstance(item, Mapping))
    if not rows:
        raw_beats = primary.get("director_sequence") or ()
        if isinstance(raw_beats, Mapping):
            raw_beats = (raw_beats,)
        rows.extend(item for item in raw_beats if isinstance(item, Mapping))
    return rows


def _story_opening_evidence_audit(
    story_contract: Mapping[str, Any],
    *,
    available_subtitle_ids: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """Validate M1 opening feasibility citations without selecting footage.

    These records are intentionally separate from chapter beats.  M2 receives
    only valid possibilities and remains responsible for final selection,
    sequence, and whether any candidate actually belongs in the final prefix.
    """
    allowed_ids = (
        {int(value) for value in available_subtitle_ids}
        if available_subtitle_ids is not None else None
    )
    raw_strategies = story_contract.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        raw_strategies = (raw_strategies,)
    records: list[dict[str, Any]] = []
    for strategy_index, strategy in enumerate(raw_strategies, 1):
        if not isinstance(strategy, Mapping):
            continue
        strategy_id = str(strategy.get("strategy_id") or f"S{strategy_index}")
        raw_packages = strategy.get("opening_evidence_packages") or ()
        if isinstance(raw_packages, Mapping):
            raw_packages = (raw_packages,)
        issues: list[str] = []
        valid_packages: list[dict[str, Any]] = []
        if not isinstance(raw_packages, (list, tuple)):
            issues.append("opening_evidence_packages_not_list")
            raw_packages = ()
        if len(raw_packages) > 3:
            issues.append("too_many_opening_evidence_packages")
        for package_index, raw_package in enumerate(raw_packages[:3], 1):
            if not isinstance(raw_package, Mapping):
                issues.append(f"package_{package_index}_not_object")
                continue
            package = dict(raw_package)
            converted: dict[str, list[int]] = {}
            package_valid = True
            for key in ("hook_subtitle_ids", "payoff_subtitle_ids"):
                raw_ids = package.get(key)
                if isinstance(raw_ids, (int, str)):
                    raw_ids = (raw_ids,)
                if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
                    issues.append(f"package_{package_index}_{key}_missing")
                    package_valid = False
                    continue
                ids: list[int] = []
                for value in raw_ids:
                    try:
                        subtitle_id = int(value)
                    except (TypeError, ValueError):
                        package_valid = False
                        issues.append(f"package_{package_index}_{key}_invalid")
                        continue
                    if allowed_ids is not None and subtitle_id not in allowed_ids:
                        package_valid = False
                        issues.append(f"package_{package_index}_{key}_outside_safe_pool:{subtitle_id}")
                        continue
                    ids.append(subtitle_id)
                if len(ids) != len(set(ids)):
                    package_valid = False
                    issues.append(f"package_{package_index}_{key}_duplicate")
                converted[key] = ids
            if set(converted.get("hook_subtitle_ids", ())) & set(converted.get("payoff_subtitle_ids", ())):
                package_valid = False
                issues.append(f"package_{package_index}_hook_payoff_overlap")
            for key in ("purchase_value", "payoff_basis"):
                if not str(package.get(key) or "").strip():
                    package_valid = False
                    issues.append(f"package_{package_index}_{key}_missing")
            if package_valid:
                valid_packages.append({
                    "hook_subtitle_ids": converted["hook_subtitle_ids"],
                    "payoff_subtitle_ids": converted["payoff_subtitle_ids"],
                    "purchase_value": str(package.get("purchase_value") or "").strip(),
                    "payoff_basis": str(package.get("payoff_basis") or "").strip(),
                })
        records.append({
            "strategy_id": strategy_id,
            "status": "valid" if valid_packages and not issues else (
                "missing" if not raw_packages else "invalid"
            ),
            "candidate_count": len(raw_packages),
            "valid_packages": valid_packages,
            "issues": issues,
        })
    return records


def build_two_pass_story_audit(
    story_contract: Mapping[str, Any],
    *,
    target_duration: float = 60.0,
    duration_tolerance: float | None = None,
    available_subtitle_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Validate only the story/Arc boundary before paid Beat Casting.

    The first semantic call is not allowed to select footage.  This audit is a
    deterministic contract check and never adds, removes or reorders a chapter.
    """
    primary = _two_pass_primary(story_contract)
    raw_chapters = primary.get("chapter_packets") or primary.get("chapters") or ()
    if isinstance(raw_chapters, Mapping):
        raw_chapters = (raw_chapters,)
    chapters = [dict(item) for item in raw_chapters if isinstance(item, Mapping)]
    coverage_counts = {"required": 0, "recommended": 0, "optional": 0}
    unexpected_selected_ids: list[int] = []
    for chapter in chapters:
        coverage = str(chapter.get("coverage") or "recommended").strip().lower()
        if coverage in coverage_counts:
            coverage_counts[coverage] += 1
        for beat in _two_pass_beat_rows({"chapter_packets": [chapter]}):
            raw_ids = beat.get("subtitle_ids") or ()
            if isinstance(raw_ids, (int, str)):
                raw_ids = (raw_ids,)
            for value in raw_ids:
                try:
                    unexpected_selected_ids.append(int(value))
                except (TypeError, ValueError):
                    continue
    evidence_location_conflicts: list[dict[str, Any]] = []
    raw_strategies = story_contract.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        raw_strategies = (raw_strategies,)
    for strategy_index, strategy in enumerate(raw_strategies, 1):
        if not isinstance(strategy, Mapping):
            continue
        evidence_location_conflicts.extend(_story_evidence_location_conflicts(
            strategy,
            strategy_id=str(strategy.get("strategy_id") or f"S{strategy_index}"),
        ))
    warnings: list[str] = []
    if not str(primary.get("core_desire") or "").strip():
        warnings.append("missing_core_desire")
    if not str(primary.get("central_promise") or "").strip():
        warnings.append("missing_central_promise")
    if not chapters:
        warnings.append("missing_purchase_journey")
    if unexpected_selected_ids:
        warnings.append("story_stage_must_not_select_subtitle_ids")
    opening_evidence = _story_opening_evidence_audit(
        story_contract, available_subtitle_ids=available_subtitle_ids,
    )
    if any(record["status"] == "invalid" for record in opening_evidence):
        warnings.append("invalid_opening_evidence_packages")
    return {
        **director_target_duration_range(target_duration, duration_tolerance),
        "chapter_count": len(chapters),
        "coverage_counts": coverage_counts,
        "unexpected_selected_subtitle_ids": unexpected_selected_ids,
        # These are planning hints for Casting, not a semantic rejection.  A
        # later pass may still assign a shared fact to one chapter and replace,
        # merge, or remove the other chapter.
        "evidence_location_conflicts": evidence_location_conflicts,
        "opening_evidence": opening_evidence,
        "warnings": warnings,
        "story_contract_valid": not warnings,
    }


_STORY_POLICY_EXTRA_PATTERNS: dict[str, tuple[str, ...]] = {
    # Story language often says “99块” or “三倍” rather than the literal
    # policy keyword “价格”; catch those promises before Casting spends a
    # request trying to make them executable.
    "price": (r"\d+(?:\.\d+)?\s*(?:元|块|块钱|倍)", r"一顿饭钱", r"定价", r"昂贵|价格不菲|帽子贵", r"值这个价", r"[数几一二三四五六七八九十两百千]+(?:十|百|千|万)(?:元|块)", r"(?:售卖|卖到|卖)[^，。；\n]{0,6}[一二三四五六七八九两]+(?:百|千|万)", r"(?:倍率|毛利)[^，。；\n]{0,8}(?:低|压|打)", r"(?:低价|压价|控价)[^，。；\n]{0,10}(?:主推|品质|市场|成本)?", r"\d{2,5}(?!\d)\s*(?:太值|超值|值了|划算|拿下|带走|入手|入了|入|到手)", r"(?:花|卖|只卖|才|只要|仅|就|到手|到手价)\s*\d{2,5}(?!\d)"),
    "cta": (r"(?:给|让)?(?:新粉|大家|宝宝|姐妹)[^，。；\n]{0,8}带回去(?:感受|试试|体验)?", r"(?:建议|推荐)[^，。；\n]{0,8}(?:购买|入手|带回去)"),
    "inventory_pressure": (r"冲量|冲榜",),
    # Delivery timing is after-sale/logistics content even when the speaker
    # avoids the literal word “发货”.  These are deliberately narrow enough
    # to leave ordinary product narration in the candidate pool.
    "after_sale": (
        r"(?:今天|当天|现在|马上)[^，。；\n]{0,8}(?:给你|给大家|统一)?\s*发(?:走|出|货|了)?(?:[，。；\s]|$)",
        r"(?:今天|当天|现在|马上|已经|都)?(?:就|给你|给大家|统一)?(?:安排)?\s*(?:发货|发出|寄出)(?:了|走)?",
        r"(?:明天|后天|几天后|很快)[^，。；\n]{0,8}(?:就)?(?:能)?(?:收到|到货|拿到|穿上)",
        r"(?:拍下|下单)[^，。；\n]{0,12}(?:发货|发出|寄出|收到|到货)",
    ),
    # Do not treat a garment's ordinary design detail as a source claim. These
    # phrases specifically turn origin/exclusivity into the purchase reason.
    "source_claim": (r"原创店铺", r"原版", r"别家无同款"),
    "social_proof": (r"销量|百单|受欢迎|热卖|临近\d+单",),
}


def _content_policy_hit_tokens(text: Any, kind: str) -> list[str]:
    """Return direct policy evidence without deciding whether it is useful."""
    source = str(text or "")
    keywords = _POLICY_KIND_KEYWORDS.get(kind, ())
    # “美元裤” is a garment name, not a price.  Amount-aware patterns below
    # still cover monetary 元.
    if kind == "price":
        keywords = tuple(keyword for keyword in keywords if keyword != "元")
    matches = [keyword for keyword in keywords if keyword in source]
    matches.extend(
        f"pattern:{index + 1}"
        for index, pattern in enumerate(_STORY_POLICY_EXTRA_PATTERNS.get(kind, ()))
        if re.search(pattern, source, flags=re.IGNORECASE)
    )
    return matches


def _story_policy_hits(text: Any, blocked_kinds: set[str]) -> list[str]:
    hits: list[str] = []
    for kind in blocked_kinds:
        if _content_policy_hit_tokens(text, kind):
            hits.append(kind)
    return sorted(hits)


def _body_only_kinds(content_contract: Mapping[str, Any] | None) -> set[str]:
    """Return explicitly configured body-only kinds without filling defaults."""
    if not content_contract:
        return set()
    body_values = {"body_only", "body", "仅正文", "正文可用"}
    result: set[str] = set()
    for kind, value in content_contract.items():
        if str(value or "").strip().lower() not in body_values:
            continue
        normalized = _normalize_content_kind(kind)
        if normalized in _POLICY_KIND_KEYWORDS:
            result.add(normalized)
    return result


def _director_extra_guard_names() -> list[str]:
    """本次生效的确定性守卫名，写进 audit 便于从落盘产物追溯。"""

    return [
        name for name, enabled in (
            ("other_product_switch", CAST_OTHER_PRODUCT_SWITCH_GUARD_ENABLED),
            ("live_pitch_talk", CAST_LIVE_PITCH_GUARD_ENABLED),
        )
        if enabled
    ]


def _director_extra_content_hits(text: Any, blocked_kinds: set[str]) -> list[str]:
    """按本次新增守卫返回命中的伪 kind（不属于 content_policy 的 kind 枚举）。

    other_product_switch：商品正确性，永远生效。
    live_pitch_talk：只有用户把 live_interaction 关掉时才拦。
    """

    hits: list[str] = []
    if CAST_OTHER_PRODUCT_SWITCH_GUARD_ENABLED and other_product_switch_reason(text):
        hits.append("other_product_switch")
    if (
        CAST_LIVE_PITCH_GUARD_ENABLED
        and "live_interaction" in blocked_kinds
        and pitch_talk_reason(text)
    ):
        hits.append("live_pitch_talk")
    return hits


def filter_director_executable_ids_for_content_policy(
    subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None,
    content_contract: Mapping[str, Any] | None,
    *,
    exclude_body_only_from_story: bool = False,
) -> tuple[list[int], dict[str, Any]]:
    """Remove explicitly forbidden utterances before either Director call.

    This is only a user-selected hard content boundary.  It does not rank,
    add, replace, or order any candidate; it returns the surviving source IDs
    in their original order so the Director still owns every semantic choice.
    """
    rows = _director_casting_rows(subtitles, executable_subtitle_ids)
    blocked_kinds = _blocked_kinds(content_contract)
    body_only_kinds = _body_only_kinds(content_contract) if exclude_body_only_from_story else set()
    excluded_kinds = blocked_kinds | body_only_kinds
    audit: dict[str, Any] = {
        "blocked_kinds": sorted(blocked_kinds),
        "body_only_kinds": sorted(body_only_kinds),
        "source_pool_count": len(rows),
        "excluded": [],
        "safe_pool_count": len(rows),
        "status": "consistent",
        "guard_rules": _director_extra_guard_names(),
    }
    extra_guards_active = bool(
        CAST_OTHER_PRODUCT_SWITCH_GUARD_ENABLED or CAST_LIVE_PITCH_GUARD_ENABLED
    )
    if not excluded_kinds and not extra_guards_active:
        return [int(row["id"]) for row in rows], audit
    safe_ids: list[int] = []
    for row in rows:
        hits = _story_policy_hits(row.get("text"), excluded_kinds)
        for extra_hit in _director_extra_content_hits(row.get("text"), blocked_kinds):
            if extra_hit not in hits:
                hits.append(extra_hit)
        if hits:
            audit["excluded"].append({
                "subtitle_id": int(row["id"]),
                "blocked_kinds": hits,
            })
            continue
        safe_ids.append(int(row["id"]))
    audit["safe_pool_count"] = len(safe_ids)
    if audit["excluded"]:
        audit["status"] = "policy_filtered"
    if not safe_ids:
        audit["status"] = "no_safe_candidates"
    return safe_ids, audit


_POLICY_CLAUSE_SPLIT_RE = re.compile(r"[^，。；！？,;!?\n]+[，。；！？,;!?]?")


def _scrub_blocked_clauses(text: Any, blocked_kinds: set[str]) -> tuple[str, list[str]]:
    """按分句剥离被禁内容，保留同一字段里干净的其余分句。

    只删除命中分句，不改写、不新增、不排序。命中分句一律不保留；若每个分句
    都命中则返回空串（与旧的整条清空一致）。
    """

    source = str(text or "").strip()
    if not source or not blocked_kinds:
        return source, []
    kept: list[str] = []
    removed: list[str] = []
    for clause in _POLICY_CLAUSE_SPLIT_RE.findall(source):
        piece = clause.strip()
        if not piece:
            continue
        if _story_policy_hits(piece, blocked_kinds):
            removed.append(piece)
            continue
        kept.append(piece)
    if not removed:
        return source, []
    return "".join(kept), removed


def sanitize_two_pass_story_for_content_policy(
    story_contract: Mapping[str, Any],
    content_contract: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep verbatim safe duties in mixed chapters; remove forbidden promises.

    No new chapter, source sentence or order is invented. Pure forbidden
    chapters are removed; surviving duties go to AI for new budgeted casting.
    """
    payload = copy.deepcopy(dict(story_contract))
    blocked_kinds = _blocked_kinds(content_contract)
    audit: dict[str, Any] = {
        "blocked_kinds": sorted(blocked_kinds),
        "removed_chapters": [],
        "trimmed_chapters": [],
        "cleared_story_fields": [],
        "removed_opening_evidence_packages": [],
        "status": "consistent",
    }
    if not blocked_kinds and not _body_only_kinds(content_contract):
        return payload, audit
    strategies = _strategy_refs(payload)
    story_fields = (
        "director_title", "core_desire", "central_promise", "opening_promise",
        "stop_condition", "story_priority",
        "thesis", "core_commercial_idea", "payoff", "story_premise",
        "audience_tension", "sub_angle",
    )
    for strategy_index, strategy in enumerate(strategies, 1):
        strategy_id = str(strategy.get("strategy_id") or f"S{strategy_index}")
        for field in story_fields:
            hits = _story_policy_hits(strategy.get(field), blocked_kinds)
            if not hits:
                continue
            # 2026-10-05：命中即整条清空会把「观众担心什么」这类合法导演状态一起抹掉
            # （audience_tension 写了“便宜”就整条消失，判定通道随即回落）。改为按分句
            # 剥离：只删命中分句，绝不保留任何命中词，全部命中时才与旧行为一致。
            scrubbed, removed_clauses = _scrub_blocked_clauses(strategy.get(field), blocked_kinds)
            strategy[field] = scrubbed
            entry = {
                "strategy_id": strategy_id, "field": field, "blocked_kinds": hits,
                "mode": "clause_scrub" if scrubbed else "cleared",
            }
            if removed_clauses:
                entry["removed_clauses"] = removed_clauses
            audit["cleared_story_fields"].append(entry)
        # M1's opening evidence is not a final clip list, but its short
        # explanation still becomes input to M2.  Do not let a blocked claim
        # re-enter the second request through that metadata.
        raw_opening_packages = strategy.get("opening_evidence_packages") or ()
        if isinstance(raw_opening_packages, Mapping):
            raw_opening_packages = (raw_opening_packages,)
        kept_opening_packages: list[dict[str, Any]] = []
        for package_index, package in enumerate(raw_opening_packages, 1):
            if not isinstance(package, Mapping):
                continue
            package_hits = _story_policy_hits(
                "\n".join([
                    str(package.get("purchase_value") or ""),
                    str(package.get("payoff_basis") or ""),
                ]),
                blocked_kinds,
            )
            if package_hits:
                audit["removed_opening_evidence_packages"].append({
                    "strategy_id": strategy_id,
                    "package_index": package_index,
                    "blocked_kinds": package_hits,
                })
                continue
            kept_opening_packages.append(dict(package))
        if "opening_evidence_packages" in strategy:
            strategy["opening_evidence_packages"] = kept_opening_packages
        kept_chapters: list[dict[str, Any]] = []
        for chapter_index, chapter in enumerate(_chapter_refs(strategy), 1):
            chapter_blocked = blocked_kinds | (_body_only_kinds(content_contract) if not kept_chapters else set())
            text = "\n".join([
                str(chapter.get("title") or ""),
                str(chapter.get("chapter_job") or ""),
                str(chapter.get("coverage") or ""),
                "\n".join(str(item or "") for item in (chapter.get("completion_requirements") or [])),
            ])
            hits = _story_policy_hits(text, chapter_blocked)
            if hits:
                requirements = list(chapter.get("completion_requirements") or [])
                safe_requirements = [item for item in requirements if not _story_policy_hits(item, chapter_blocked)]
                # Preserve only verbatim safe authored duties. Never invent a
                # replacement story or retain a forbidden requirement.
                safe_job = str(chapter.get("chapter_job") or "")
                if _story_policy_hits(safe_job, chapter_blocked):
                    safe_job = ""
                if not safe_requirements and not safe_job:
                    audit["removed_chapters"].append({
                        "strategy_id": strategy_id,
                        "chapter_id": str(chapter.get("chapter_id") or f"C{chapter_index}"),
                        "blocked_kinds": hits,
                    })
                    continue
                chapter["completion_requirements"] = safe_requirements
                for field in ("title", "chapter_job", "buyer_advance", "purchase_question", "purpose", "new_buyer_knowledge", "micro_story_shape"):
                    if _story_policy_hits(chapter.get(field), chapter_blocked):
                        chapter[field] = ""
                if not chapter.get("chapter_job"):
                    chapter["chapter_job"] = "；".join(str(item) for item in safe_requirements)
                audit["trimmed_chapters"].append({
                    "strategy_id": strategy_id,
                    "chapter_id": str(chapter.get("chapter_id") or f"C{chapter_index}"),
                    "removed_requirements": [item for item in requirements if item not in safe_requirements],
                    "remaining_requirement_count": len(safe_requirements),
                    "blocked_kinds": hits,
                })
            kept_chapters.append(chapter)
        strategy["chapter_packets"] = kept_chapters
    if (
        audit["removed_chapters"] or audit["trimmed_chapters"]
        or audit["cleared_story_fields"] or audit["removed_opening_evidence_packages"]
    ):
        audit["status"] = "policy_trimmed"
    return payload, audit


def director_material_pacing(
    rows: Sequence[Mapping[str, Any]],
    duration_range: Mapping[str, Any],
    *,
    max_beats_per_chapter: int = 5,
) -> dict[str, Any]:
    """Scale a target duration by the material's real utterance length.

    The depth table assumes one Beat can carry roughly four seconds of speech.
    Real live transcripts are often far shorter per Beat, so the table's own
    Beat count under-plans the same target on short-sentence material.  This
    reference is descriptive: it never selects, cuts or reorders content.
    """
    lengths = [
        float(row["end"]) - float(row["start"])
        for row in rows
        if isinstance(row, Mapping)
        and row.get("start") is not None
        and row.get("end") is not None
    ]
    pool_seconds = round(sum(lengths), 3)
    mean_seconds = round(pool_seconds / len(lengths), 3) if lengths else 0.0

    def _beats_for(seconds: Any) -> int | None:
        if mean_seconds <= 0 or float(seconds or 0.0) <= 0:
            return None
        return math.ceil(float(seconds) / mean_seconds)

    beats_for_min = _beats_for(duration_range.get("source_min"))
    beats_for_target = _beats_for(duration_range.get("source_target"))
    chapters_for_min = (
        math.ceil(beats_for_min / max(1, int(max_beats_per_chapter)))
        if beats_for_min
        else None
    )
    return {
        "material_pool_seconds": pool_seconds,
        "material_mean_beat_seconds": mean_seconds,
        "approximate_beats_for_target_at_this_pace": beats_for_target,
        "approximate_beats_for_source_min_at_this_pace": beats_for_min,
        "minimum_chapters_for_source_min": chapters_for_min,
        "reference_only": True,
    }


def _story_delivery_depth_audit(
    story_contract: Mapping[str, Any],
    *,
    target_duration: float,
    duration_tolerance: float | None,
    output_speed_factor: float,
    material_mean_beat_seconds: float | None = None,
    material_pool_seconds: float | None = None,
) -> dict[str, Any]:
    """Measure whether a 50s+ story survives content-boundary removal.

    This delivery-floor check never adds, removes, or reorders a chapter. A
    60-second story with only three remaining chapters cannot be repaired by
    letting M2 pour dozens of sentences into each chapter.
    """
    duration_range = director_delivery_duration_range(
        target_duration, duration_tolerance, output_speed_factor,
    )
    requested = float(duration_range["requested_seconds"])
    depth = director_duration_depth_contract(requested)
    enforced = requested >= 50.0
    try:
        _pace_seconds = float(material_mean_beat_seconds or 0.0)
    except (TypeError, ValueError):
        _pace_seconds = 0.0
    _source_min_seconds = float(duration_range.get("source_min") or 0.0)
    # 2026-09-23（David 定调）：章节数下限必须按素材真实句长折算。
    # 深度表按约 4 秒/句标定，真实直播常 2.5 秒/句，长目标因此被系统性少规划。
    _pace_floor_beats = (
        math.ceil(_source_min_seconds / _pace_seconds)
        if _pace_seconds > 0 and _source_min_seconds > 0
        else 0
    )
    _floor_beats = max(int(depth["expected_total_beats"]["low"]), int(_pace_floor_beats))
    minimum_chapters = max(
        1,
        math.ceil(
            _floor_beats
            / max(1, int(depth["beats_per_chapter"]["high"]))
        ),
    ) if enforced else 0
    minimum_source_seconds = (
        round(float(duration_range["source_target"]) * 0.75, 3)
        if enforced else 0.0
    )
    strategies: list[dict[str, Any]] = []
    insufficient_ids: list[str] = []
    for index, strategy in enumerate(_strategy_refs(story_contract), 1):
        strategy_id = str(strategy.get("strategy_id") or f"S{index}")
        chapters = _chapter_refs(strategy)
        if not chapters and str(strategy.get("director_plan_role") or "").lower() == "alternative":
            # Direction cards are intentionally not executable stories.
            continue
        planned_source_seconds = 0.0
        for chapter in chapters:
            try:
                budget = float(chapter.get("source_budget_seconds") or 0.0)
            except (TypeError, ValueError):
                budget = 0.0
            if math.isfinite(budget) and budget > 0:
                planned_source_seconds += budget
        chapter_count = len(chapters)
        sufficient = (
            not enforced
            or (
                chapter_count >= minimum_chapters
                and planned_source_seconds >= minimum_source_seconds
            )
        )
        if not sufficient:
            insufficient_ids.append(strategy_id)
        strategies.append({
            "strategy_id": strategy_id,
            "chapter_count": chapter_count,
            "planned_source_seconds": round(planned_source_seconds, 3),
            "sufficient": sufficient,
        })
    # The budget sum is author-declared, so it cannot prove that the material
    # can carry the target.  Scale the chapter floor by the real utterance
    # length so an under-planned 50s+ story becomes visible instead of thin.
    mean_beat_seconds = float(material_mean_beat_seconds or 0.0)
    calibrated_minimum_chapters = 0
    calibrated_min_beats = None
    calibrated_insufficient_ids: list[str] = []
    if enforced and mean_beat_seconds > 0:
        calibrated_min_beats = math.ceil(
            float(duration_range["source_min"]) / mean_beat_seconds
        )
        calibrated_minimum_chapters = max(
            minimum_chapters,
            math.ceil(
                calibrated_min_beats / max(1, int(depth["beats_per_chapter"]["high"]))
            ),
        )
        calibrated_insufficient_ids = [
            str(item["strategy_id"])
            for item in strategies
            if int(item["chapter_count"]) < calibrated_minimum_chapters
        ]
    return {
        "enforced": enforced,
        "requested_seconds": requested,
        "minimum_chapters": minimum_chapters,
        "minimum_planned_source_seconds": minimum_source_seconds,
        "strategies": strategies,
        "insufficient_strategy_ids": insufficient_ids,
        "status": "insufficient_after_policy" if insufficient_ids else "sufficient",
        "material_pool_seconds": (
            round(float(material_pool_seconds), 3)
            if material_pool_seconds is not None
            else None
        ),
        "material_mean_beat_seconds": (
            round(mean_beat_seconds, 3) if mean_beat_seconds > 0 else None
        ),
        "material_supports_source_min": (
            bool(
                float(material_pool_seconds or 0.0)
                >= float(duration_range["source_min"])
            )
            if material_pool_seconds is not None
            else None
        ),
        "calibrated_min_beats": calibrated_min_beats,
        "calibrated_minimum_chapters": calibrated_minimum_chapters,
        "calibrated_insufficient_strategy_ids": calibrated_insufficient_ids,
        "calibrated_status": (
            "sufficient"
            if not calibrated_insufficient_ids
            else "insufficient"
        )
        if mean_beat_seconds > 0
        else "not_evaluated",
    }


def _expand_cast_sentence_groups(payload: dict[str, Any], subtitles: Sequence[Mapping[str, Any]], executable_ids: Sequence[int] | None = None) -> dict[str, int]:
    """Normalize AI-selected groups only; never select, drop or reorder a line."""
    import copy
    rows = {int(row["id"]): row for row in subtitles}
    allowed = set(rows) if executable_ids is None else set(executable_ids) & set(rows)
    expanded = 0
    dropped: list[dict[str, Any]] = []
    for strategy in _strategy_refs(payload):
        for chapter in _chapter_refs(strategy):
            beats = chapter.get("beats") or []
            if isinstance(beats, Mapping):
                beats = [beats]
            result = []
            for beat in beats:
                if not isinstance(beat, Mapping):
                    raise AnalyzerError("Director 选句格式无效：选句必须是对象。")
                ids = beat.get("subtitle_ids", beat.get("ids", []))
                if isinstance(ids, (str, int)):
                    ids = [ids]
                if not isinstance(ids, (list, tuple)) or not ids:
                    raise AnalyzerError("Director 选句格式无效：缺少字幕 ID。")
                values = []
                for value in ids:
                    if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, str) and value.isdecimal()):
                        raise AnalyzerError("Director 选句格式无效：字幕 ID 必须是整数。")
                    value = int(value)
                    if value not in allowed:
                        # 越界 ID：模型引用了“仅作上下文展示、不在可执行池里”的行。
                        # 丢弃这一句，而不是让整次预览失败（校验在付费调用之后，代价高）。
                        dropped.append({
                            "subtitle_id": value,
                            "strategy_id": str(strategy.get("strategy_id") or ""),
                            "chapter_id": str(chapter.get("chapter_id") or ""),
                            "beat_id": str(beat.get("beat_id") or ""),
                            "reason": "not_in_executable_pool",
                        })
                        continue
                    values.append(value)
                if not values:
                    # 整条 beat 没有任何可执行句：丢弃该 beat，不报错。
                    continue
                if len(values) == 1:
                    result.append(dict(beat, subtitle_ids=values))
                    continue
                expanded += 1
                for index, value in enumerate(values):
                    item = copy.deepcopy(dict(beat))
                    item.pop("ids", None)
                    item["subtitle_ids"] = [value]
                    # A group duration is not the duration of each child.
                    item.pop("sec", None)
                    item["source_seconds"] = round(float(rows[value]["end"]) - float(rows[value]["start"]), 3)
                    if item.get("beat_id") and index:
                        item["beat_id"] = f"{item['beat_id']}_line{index + 1}"
                    result.append(item)
            chapter["beats"] = result
    return {"expanded_groups": expanded, "dropped_ids": dropped}



# P0-2（2026-10-03 David 定）：续句补齐必须"补到收口为止，否则不补"。
# 背景（重启后预览审计实测）：补进来的 6 行（#279/#95/#290/#303/#181/#519）全部以
# 逗号收尾、它们的续句都没被选中 —— 章尾悬空反而从 2/8 涨到 6/8（半句只是往后挪了
# 一行），其中 #181「你玩买了买了买了，」还是语音识别乱码。
# 规则：① 从待补行起沿原文顺序向后补，补到真正收口为止，上限
# CAST_CONTINUATION_CLOSURE_MAX_ROWS 行；② 收不了口（下一行不可用 / 已被任何章
# 选中 / 超上限）→ 放弃这次补，回退原样；③ 待补行命中乱码特征（同一 2-4 字片段连续
# 重复三次以上）→ 放弃。宁可少补，也不许把半句或者乱码补进成片。
CAST_CONTINUATION_CLOSURE_ENABLED = True
CAST_CONTINUATION_CLOSURE_MAX_ROWS = 4
CAST_CONTINUATION_PAUSE_CHARS = ("，", "、", ",", "；", ";", "：", ":")


def _continuation_row_closes(text: str) -> bool:
    """这一行是不是已经收口（句末标点，或压根没有停顿符）。"""
    value = str(text or "").strip()
    if not value:
        return True
    return value[-1] in ("。", "！", "？", "!", "?", "~") or value[-1] not in CAST_CONTINUATION_PAUSE_CHARS


def _continuation_row_is_noise(text: str) -> bool:
    """同一 2-4 字片段连着重三次以上 = 语音识别乱码（实测「你玩买了买了买了，」）。"""
    return bool(re.search(r"(.{2,4})\1{2,}", str(text or "")))


def _continuation_closure_chain(
    add: Any,
    rows: Mapping[int, Any],
    next_source_id: Mapping[int, int],
    allowed: set[int],
    used: set[int],
) -> tuple[list[int], bool, str]:
    """从 ``add`` 起沿原文顺序向后走到真正收口。返回 ``(chain, ok, reason)``。"""
    if not CAST_CONTINUATION_CLOSURE_ENABLED:
        return [int(add)], True, "disabled"
    chain: list[int] = []
    cursor: Any = int(add)
    for _ in range(max(1, int(CAST_CONTINUATION_CLOSURE_MAX_ROWS))):
        if cursor is None or cursor not in rows or cursor not in allowed or cursor in used:
            return [], False, "closure_unavailable"
        text = str(rows[cursor].get("text") or "").strip()
        if _continuation_row_is_noise(text):
            return [], False, "closure_noise"
        chain.append(int(cursor))
        if _continuation_row_closes(text):
            return chain, True, "closed"
        cursor = next_source_id.get(cursor)
    return [], False, "closure_too_long"


def _continuation_used_ids(payload: Mapping[str, Any]) -> set[int]:
    """整篇已经用到的字幕 ID（跨章、跨版本）。

    续接链只查「本章用过没用过」不够：实测 #279→#280→#281→#282 一路都"没用过"，
    但 #282 已经排在 C3 —— 只看本章就会把同一句补第二份，跨章重复。
    """
    used: set[int] = set()
    for strategy in _strategy_refs(payload):
        for chapter in _chapter_refs(strategy):
            beats = chapter.get("beats") or []
            if isinstance(beats, Mapping):
                beats = [beats]
            if not isinstance(beats, list):
                continue
            for beat in beats:
                if not isinstance(beat, Mapping):
                    continue
                for value in (beat.get("subtitle_ids") or beat.get("ids") or []):
                    try:
                        used.add(int(value))
                    except (TypeError, ValueError):
                        continue
    return used


def _continuation_completion_pass(
    payload: dict[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """按内容（不看标点）补全“没说完”的 beat 的承接句。

    规则：模型对每个 beat 自判 utterance_complete；若为 false 而紧邻下一个 beat 不是
    原字幕的下一行（承接句），且该承接句在可执行池内且本方案未用过，则把它补在中间。
    这是确定性补全：不改写任何原话，只按原顺序插入缺失的承接句。
    """
    rows = {int(row["id"]): row for row in subtitles}
    allowed = set(rows) if executable_ids is None else (set(executable_ids) & set(rows))
    ordered_ids = [int(item) for item in rows]
    next_source_id: dict[int, int] = {}
    for position, sid in enumerate(ordered_ids):
        if position + 1 < len(ordered_ids):
            next_source_id[sid] = ordered_ids[position + 1]
    inserted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for strategy in _strategy_refs(payload):
        for chapter in _chapter_refs(strategy):
            beats = chapter.get("beats") or []
            if isinstance(beats, Mapping):
                beats = [beats]
            if not isinstance(beats, list):
                continue
            used = {
                int(value)
                for beat in beats if isinstance(beat, Mapping)
                for value in (beat.get("subtitle_ids") or beat.get("ids") or [])
            }
            result: list[Any] = []
            for position, beat in enumerate(beats):
                if not isinstance(beat, Mapping):
                    result.append(beat); continue
                result.append(beat)
                if beat.get("utterance_complete") is not False:
                    continue
                ids = beat.get("subtitle_ids") or beat.get("ids") or []
                try:
                    last_id = int(ids[-1]) if ids else None
                except (TypeError, ValueError):
                    last_id = None
                if last_id is None or last_id not in next_source_id:
                    continue
                want = next_source_id[last_id]
                nxt = beats[position + 1] if position + 1 < len(beats) else None
                nxt_ids = (nxt.get("subtitle_ids") or nxt.get("ids") or []) if isinstance(nxt, Mapping) else []
                try:
                    nxt_first = int(nxt_ids[0]) if nxt_ids else None
                except (TypeError, ValueError):
                    nxt_first = None
                if nxt_first == want:
                    continue  # 已经接上了
                if want in used:
                    skipped.append({"reason": "continuation_already_used", "subtitle_id": want})
                    continue
                if want not in allowed:
                    skipped.append({"reason": "continuation_not_selectable", "subtitle_id": want})
                    continue
                if (CAST_CONTINUATION_ROW_GUARD_ENABLED
                        and (_cast_row_prune_reason(rows.get(want) or {})
                             or _cast_row_repair_blocked(rows.get(want) or {}))):
                    skipped.append({"reason": "continuation_bad_row", "subtitle_id": want})
                    continue
                filler = {
                    "beat_function": str(beat.get("beat_function") or "continuation"),
                    "subtitle_ids": [want],
                    "utterance_complete": True,
                    "continuation_of": last_id,
                    "product_relation": beat.get("product_relation"),
                }
                result.append(filler)
                used.add(want)
                inserted.append({"subtitle_id": want, "continuation_of": last_id})
            chapter["beats"] = result
    return {"inserted": inserted, "skipped": skipped}

def _opening_receipt_selection(receipt: Any) -> tuple[list[int], str]:
    """从开场回执里取出 (selected_subtitle_ids, quality)。找不到就返回 ([], "")。"""
    if not isinstance(receipt, Mapping):
        return [], ""
    for candidate in (
        receipt,
        receipt.get("receipt") if isinstance(receipt.get("receipt"), Mapping) else {},
        receipt.get("selection") if isinstance(receipt.get("selection"), Mapping) else {},
    ):
        raw = (candidate or {}).get("selected_subtitle_ids") or []
        if not raw:
            continue
        ids: list[int] = []
        for value in raw:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if ids:
            return ids, str((candidate or {}).get("quality") or "")
    return [], ""

def _enforce_opening_first(payload: Mapping[str, Any], opening_ids: Sequence[int]) -> dict[str, Any]:
    """strong 开场回执的顺序若没落在第一章最前面，就在第一章内部把它前移。
    只调整顺序，不改写原话；不跨章搬运。"""
    want = [int(v) for v in (opening_ids or [])]
    moved: list[int] = []
    if not want:
        return {"moved": moved}
    strategies = payload.get("strategies") or ()
    if isinstance(strategies, Mapping):
        strategies = (strategies,)
    for strategy in strategies:
        if not isinstance(strategy, Mapping):
            continue
        chapters = strategy.get("chapter_packets") or ()
        if isinstance(chapters, Mapping):
            chapters = (chapters,)
        if not chapters:
            continue
        first = chapters[0]
        if not isinstance(first, dict):
            continue
        beats = first.get("beats") or []
        if isinstance(beats, Mapping):
            beats = [beats]
        if not isinstance(beats, list) or not beats:
            continue

        def beat_ids(beat: Any) -> list[int]:
            if not isinstance(beat, Mapping):
                return []
            raw = beat.get("subtitle_ids") or beat.get("ids") or []
            out: list[int] = []
            for value in raw:
                try:
                    out.append(int(value))
                except (TypeError, ValueError):
                    continue
            return out

        head: list[Any] = []
        remaining = list(beats)
        for sid in want:
            hit = next((b for b in remaining if sid in beat_ids(b)), None)
            if hit is None:
                continue
            head.append(hit)
            remaining.remove(hit)
        if not head:
            continue
        current_prefix = [i for beat in beats[:len(head)] for i in beat_ids(beat)]
        if current_prefix == want[:len(head)]:
            continue
        first["beats"] = head + remaining
        moved = want[:len(head)]
    return {"moved": moved}

def _script_continuity_prompt(script_lines: Sequence[str], pool_lines: Sequence[str]) -> str:
    """把整篇选片文案当一篇文章来读：找不通顺/截断处（优先每章最后一句），
    [v9.8] 承接句只允许取原文紧邻的下一行（承接断裂时宁可少补，也不许跨句乱接）。"""
    return (
        "下面是本次成片的完整口播稿，按章节顺序排好（`|章末|` 标出每章最后一句）。请把它当成一篇文章通读。\n\n"
        "任务：找出**读起来不通顺、话没说完/半截**的地方——**优先检查每一章的章末**（章末截断最刺耳）。"
        "只看内容，不要依赖标点（标点是本地语音识别给的，不准）。\n\n"
        "对每一处问题，从后面给出的【可选句池】里挑**一条**句子来补全，且**必须就是原文紧邻的下一行**（即 `add_id` 必须是 `after_id` 的下一行）；只有原文下一行确实不在池里时，才允许退一步选池内最接近的句子。"
        "要求：补进去之后，前后连读自然、意思完整；不要重复已在稿里的句子；不要改写任何原句。\n\n"
        "只输出 JSON，不要解释：{\"fixes\": [{\"after_id\": 在它后面补的句子ID, \"add_id\": 要补的句子ID, \"reason\": \"一句话说明\"}]}；没有问题就输出 {\"fixes\": []}。\n\n"
        "【本次口播稿】\n" + "\n".join(script_lines)
        + "\n\n【可选句池】\n" + "\n".join(pool_lines)
    )

def review_continuations(
    payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    *, api_key: str, base_url: str, model: str, timeout: float, log_fn: Any = None,
    executable_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """整篇通读 + 从整池挑承接句（一次调用）。失败返回空结果，绝不影响主流程。"""
    rows = {int(row["id"]): row for row in subtitles}
    allowed = set(rows) if executable_ids is None else (set(executable_ids) & set(rows))
    script_lines: list[str] = []
    # [v9.8] 原文顺序的「下一行」表：承接句只接受它，避免把承接接到源里远隔的句子上。
    ordered_ids = [int(row["id"]) for row in subtitles]
    next_source_id: dict[int, int] = {}
    for position, sid in enumerate(ordered_ids):
        if position + 1 < len(ordered_ids):
            next_source_id[sid] = ordered_ids[position + 1]
    chapters: list[list[int]] = []
    for strategy in _strategy_refs(payload):
        for chapter in _chapter_refs(strategy):
            beats = chapter.get("beats") or []
            if isinstance(beats, Mapping):
                beats = [beats]
            ids: list[int] = []
            for beat in beats:
                if not isinstance(beat, Mapping):
                    continue
                raw = beat.get("subtitle_ids") or beat.get("ids") or []
                for value in raw:
                    try:
                        sid = int(value)
                    except (TypeError, ValueError):
                        continue
                    if sid in rows:
                        ids.append(sid)
            if ids:
                chapters.append(ids)
    flat = [sid for ids in chapters for sid in ids]
    if not flat:
        return {"fixes": [], "checked": 0}
    # 稿子：标出每章最后一句
    for ids in chapters:
        for position, sid in enumerate(ids):
            mark = "|章末|" if position == len(ids) - 1 else ""
            script_lines.append(f'{mark}#{sid}|{str(rows[sid].get("text") or "").strip()}')
        script_lines.append("----")
    pool_lines = [f'#{sid}|{str(rows[sid].get("text") or "").strip()}' for sid in rows if sid in allowed]
    prompt = _script_continuity_prompt(script_lines, pool_lines)
    try:
        raw = _post_analyzer_request(
            api_key=api_key, base_url=base_url, model=model,
            user_prompt=prompt, temperature=0.0, top_p=1.0,
            max_tokens=2000, timeout=timeout,
        )
        data = _extract_json(raw)
    except Exception as exc:  # noqa: BLE001 - 审查失败不得阻断导演
        if log_fn:
            log_fn(f"整篇承接审查失败（已跳过）：{type(exc).__name__}: {exc}")
        return {"fixes": [], "checked": len(flat), "error": f"{type(exc).__name__}"}
    used = set(flat)
    fixes: list[dict[str, Any]] = []
    corrected = 0
    dropped_remote = 0
    for item in (data.get("fixes") or []):
        if not isinstance(item, Mapping):
            continue
        try:
            after = int(item.get("after_id"))
            add = int(item.get("add_id"))
        except (TypeError, ValueError):
            continue
        if after not in used:
            continue
        # [v9.8] 承接句只认原文紧邻的下一行；AI 挑了别处就纠正为原文下一行，
        # 纠正不了（下一行不在池里/已用）就丢弃 —— 宁可少补，也不许跨句乱接。
        expected = next_source_id.get(after)
        if expected is None:
            dropped_remote += 1
            continue
        if add != expected:
            corrected += 1
            add = expected
        if add in allowed and add not in used:
            fixes.append({"after_id": after, "add_id": add, "reason": str(item.get("reason") or "")[:80]})
            used.add(add)
        else:
            dropped_remote += 1
    return {"fixes": fixes, "checked": len(flat), "corrected": corrected,
            "dropped_remote": dropped_remote}

def apply_continuation_fill(
    payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    fixes: Sequence[Mapping[str, Any]],
    executable_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """按审查结果，把补全句插到指定句之后；不改写任何原话。"""
    rows = {int(row["id"]): row for row in subtitles}
    allowed = set(rows) if executable_ids is None else (set(executable_ids) & set(rows))
    # P0-2：续接链要从这里沿原文顺序向后走（review_continuations 里的同名字典不共享）。
    _ordered_ids = [int(row["id"]) for row in subtitles]
    next_source_id: dict[int, int] = {
        _ordered_ids[index]: _ordered_ids[index + 1] for index in range(len(_ordered_ids) - 1)
    }
    want: dict[int, list[int]] = {}
    for item in fixes or ():
        try:
            want.setdefault(int(item.get("after_id")), []).append(int(item.get("add_id")))
        except (TypeError, ValueError):
            continue
    inserted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not want:
        return {"inserted": inserted, "skipped": skipped}
    global_used = _continuation_used_ids(payload)
    for strategy in _strategy_refs(payload):
        for chapter in _chapter_refs(strategy):
            beats = chapter.get("beats") or []
            if isinstance(beats, Mapping):
                beats = [beats]
            if not isinstance(beats, list):
                continue
            used = {int(v) for beat in beats if isinstance(beat, Mapping)
                    for v in (beat.get("subtitle_ids") or beat.get("ids") or [])}
            out: list[Any] = []
            for beat in beats:
                out.append(beat)
                if not isinstance(beat, Mapping):
                    continue
                ids = beat.get("subtitle_ids") or beat.get("ids") or []
                try:
                    last = int(ids[-1]) if ids else None
                except (TypeError, ValueError):
                    last = None
                for add in want.get(last, []) if last is not None else []:
                    if add in used:
                        skipped.append({"subtitle_id": add, "reason": "already_used"}); continue
                    if add not in allowed:
                        skipped.append({"subtitle_id": add, "reason": "not_selectable"}); continue
                    if (CAST_CONTINUATION_ROW_GUARD_ENABLED
                            and (_cast_row_prune_reason(rows.get(add) or {})
                                 or _cast_row_repair_blocked(rows.get(add) or {}))):
                        skipped.append({"subtitle_id": add, "reason": "bad_row"}); continue
                    # P0-2（2026-10-03 David 定）：补行必须"补到收口为止，否则不补"。
                    chain, chain_ok, chain_reason = _continuation_closure_chain(
                        add, rows, next_source_id, allowed, used | global_used,
                    )
                    if not chain_ok:
                        skipped.append({"subtitle_id": add, "reason": chain_reason})
                        continue
                    previous = last
                    for subtitle_id in chain:
                        out.append({
                            "beat_function": str(beat.get("beat_function") or "continuation"),
                            "subtitle_ids": [subtitle_id],
                            "utterance_complete": True,
                            "continuation_of": previous,
                            "product_relation": beat.get("product_relation"),
                        })
                        used.add(subtitle_id)
                        inserted.append({"subtitle_id": subtitle_id, "continuation_of": previous})
                        previous = subtitle_id
            chapter["beats"] = out
    return {"inserted": inserted, "skipped": skipped}

CAST_CHAPTER_MAX_BEAT_GAP_SECONDS = 5.0
CAST_CHAPTER_MAX_SPAN_SECONDS = 30.0
CAST_CHAPTER_MIN_CLOSED_SECONDS = 3.0
CAST_CHAPTER_MAX_TEXT_OVERLAP = 0.6
CAST_BEAT_MIN_FRAGMENT_SECONDS = 1.2

_CAST_CONTINUITY_LABELS = {
    "chapter_not_closed": "半句章：整章不足两段，或整章源时长不足 3 秒，必须并回相邻章或删掉",
    "chapter_span_too_wide": "跨时间拼接：整章首尾源时间跨度超过 30 秒",
    "beat_too_short": "残句：单句源时长不足 1.2 秒，不得成 Beat",
    "beat_window_far_gap": "跨窗拼接：同章相邻两句的源时间相隔超过 5 秒",
    "chapter_repeat_evidence": "同义重复：同章两句在说同一件事（文本重合超过 60%）",
    "beat_window_split": "拆窗：只取了连续口播窗的一部分，落盘会从半句开始或停在半句；已按原顺序补齐窗内缺行",
}


def _cast_rows_by_id(subtitles: Sequence[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    """Map subtitle id -> row without trusting the caller's ordering."""
    rows: dict[int, Mapping[str, Any]] = {}
    for row in subtitles or ():
        if not isinstance(row, Mapping):
            continue
        try:
            rows[int(row["id"])] = row
        except (TypeError, ValueError, KeyError):
            continue
    return rows


def _cast_chapter_beat_spans(
    chapter: Mapping[str, Any],
    rows: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Measure every Beat of one chapter on the real source timeline."""
    raw_beats = chapter.get("beats") or ()
    if isinstance(raw_beats, Mapping):
        raw_beats = (raw_beats,)
    spans: list[dict[str, Any]] = []
    for beat_index, beat in enumerate(raw_beats, 1):
        if not isinstance(beat, Mapping):
            continue
        raw_ids = beat.get("subtitle_ids", beat.get("ids", ()))
        if isinstance(raw_ids, (str, int)) and not isinstance(raw_ids, bool):
            raw_ids = (raw_ids,)
        starts: list[float] = []
        ends: list[float] = []
        texts: list[str] = []
        for value in raw_ids or ():
            try:
                subtitle_id = int(value)
            except (TypeError, ValueError):
                continue
            row = rows.get(subtitle_id)
            if row is None:
                continue
            try:
                starts.append(float(row["start"]))
                ends.append(float(row["end"]))
            except (TypeError, ValueError, KeyError):
                continue
            texts.append(str(row.get("text") or ""))
        if not starts:
            continue
        spans.append({
            "beat_index": beat_index,
            "beat_id": str(beat.get("beat_id") or ""),
            "start": min(starts),
            "end": max(ends),
            "text": " ".join(text for text in texts if text).strip(),
        })
    return spans


_CAST_ROW_UNFINISHED_TAIL = "，、；：,;:…"


def _cast_row_looks_unfinished(row: Mapping[str, Any]) -> bool:
    """这一行是否「话没说完」：以逗号/顿号/分号这类句内标点结尾。

    完全没有标点的行不算（那多半是残缺/空的 ASR 行，不是半句话），
    避免对没有标点的素材做无意义的补齐。
    """
    text = str(row.get("text") or "").strip()
    if not text:
        return False
    return text[-1] in _CAST_ROW_UNFINISHED_TAIL


def _cast_row_repair_blocked(row: Mapping[str, Any]) -> bool:
    """补齐用的内容护栏：会被内容策略屏蔽的行不参与补齐。"""
    text = str(row.get("text") or "")
    if not text:
        return False
    return bool(CAST_WINDOW_SPLIT_REPAIR_BLOCK_PATTERN.search(text))


def _cast_window_member_index(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[int, str], dict[str, list[int]]]:
    """把窗池摊成 (行ID -> 窗号) 与 (窗号 -> 成员行ID 列表)。"""
    by_id: dict[int, str] = {}
    members: dict[str, list[int]] = {}
    try:
        window_rows, _dropped = _cast_windows_for_rows(rows)
    except Exception:  # noqa: BLE001 - 推算失败时按「没有窗」处理
        return {}, {}
    for window in window_rows:
        window_id = str(window.get("window_id") or "")
        ids = [int(value) for value in (window.get("window_ids") or ())]
        if not window_id or not ids:
            continue
        members[window_id] = ids
        for subtitle_id in ids:
            by_id[subtitle_id] = window_id
    return by_id, members


def _cast_window_fill_scope(
    window_id: str | None,
    own_ids: Sequence[int],
    members: Mapping[str, Sequence[int]],
    max_rows: int,
) -> list[int]:
    """补齐用的搜索范围 = 本窗 + 相邻窗相邻的最多 max_rows 行（P0-7）。

    缺的那半句常常就在隔壁窗里，只搜本窗永远补不上（实测 G04 的 47 处补不上里
    37 处属这一类）。范围只向两侧各扩 max_rows 行，行数/秒数上限与「必须在可执行
    池、未被用过、不是坏行」这些约束一条不变 —— 补行仍要能顺着原句连下去。
    """
    scope: list[int] = [int(value) for value in own_ids]
    if not window_id or not members:
        return scope
    order = list(members.keys())
    try:
        position = order.index(str(window_id))
    except ValueError:
        return scope
    take = max(1, int(max_rows))
    if position > 0:
        before = [int(value) for value in (members.get(order[position - 1]) or ())]
        scope = before[-take:] + scope
    if position + 1 < len(order):
        after = [int(value) for value in (members.get(order[position + 1]) or ())]
        scope = scope + after[:take]
    return scope


def _cast_window_neighbor_edges(
    window_id: str | None,
    members: Mapping[str, Sequence[int]],
) -> tuple[int | None, int | None]:
    """相邻窗的「贴边行」：(上一窗最后一行, 下一窗第一行)。

    窗与窗之间常因剔行/间隙而 ID 不连续（`+1` 走不过去），所以跨窗补齐必须按
    「窗序」找贴边行，而不是按 ID 加一 —— 只有这样才真的能补到隔壁窗那半句。
    """
    if not window_id or not members:
        return None, None
    order = list(members.keys())
    try:
        position = order.index(str(window_id))
    except ValueError:
        return None, None
    previous_edge = next_edge = None
    if position > 0:
        before = [int(value) for value in (members.get(order[position - 1]) or ())]
        previous_edge = before[-1] if before else None
    if position + 1 < len(order):
        after = [int(value) for value in (members.get(order[position + 1]) or ())]
        next_edge = after[0] if after else None
    return previous_edge, next_edge


def _cast_chapter_selected_ids(chapter: Mapping[str, Any]) -> list[int]:
    """按原顺序取出一章里被选中的字幕 ID（去重，保序）。"""
    raw_beats = chapter.get("beats") or ()
    if isinstance(raw_beats, Mapping):
        raw_beats = (raw_beats,)
    selected: list[int] = []
    for beat in raw_beats:
        if not isinstance(beat, Mapping):
            continue
        raw_ids = beat.get("subtitle_ids", beat.get("ids", ()))
        if isinstance(raw_ids, (str, int)) and not isinstance(raw_ids, bool):
            raw_ids = (raw_ids,)
        for value in raw_ids or ():
            try:
                subtitle_id = int(value)
            except (TypeError, ValueError):
                continue
            if subtitle_id not in selected:
                selected.append(subtitle_id)
    return selected


def _cast_window_split_issues(
    casting_payload: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """报告「只取了连续口播窗的一部分」的章：落盘会从半句开始或停在半句。

    纯确定性、只读：不重写、不重排、不删行。
    """
    by_id, members = _cast_window_member_index(rows)
    if not members:
        return []
    issues: list[dict[str, Any]] = []
    for strategy_index, strategy in enumerate(_strategy_refs(casting_payload), 1):
        strategy_id = str(strategy.get("strategy_id") or f"S{strategy_index}")
        for chapter_index, chapter in enumerate(_chapter_refs(strategy), 1):
            if not isinstance(chapter, Mapping):
                continue
            chapter_id = str(chapter.get("chapter_id") or f"C{chapter_index}")
            picked_by_window: dict[str, list[int]] = {}
            for subtitle_id in _cast_chapter_selected_ids(chapter):
                window_id = by_id.get(subtitle_id)
                if window_id:
                    picked_by_window.setdefault(window_id, []).append(subtitle_id)
            for window_id, picked in picked_by_window.items():
                full = members.get(window_id) or []
                if not full or set(picked) >= set(full):
                    continue
                picked_set = set(picked)
                issues.append({
                    "kind": "beat_window_split",
                    "strategy_id": strategy_id,
                    "chapter_id": chapter_id,
                    "window_id": window_id,
                    "window_ids": list(full),
                    "picked_ids": list(picked),
                    "missing_ids": [value for value in full if value not in picked_set],
                    "text": "/".join(str(value) for value in picked)[:40],
                })
    return issues


def _repair_cast_window_splits(
    payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_ids: Sequence[int] | None = None,
    *,
    log_fn: Any = None,
) -> dict[str, Any]:
    """把被拆开的连续口播窗按原顺序补回（确定性；不改写、不重排、不加 AI 调用）。

    只补「同一窗里缺的那几行」，且必须：在可执行池内、尚未被别的 Beat 用过、
    不在坏行名单里、不超行数/秒数上限。补不动就如实记进 skipped，不静默。
    """
    def _log(message: str) -> None:
        if callable(log_fn):
            try:
                log_fn(message)
            except Exception:  # noqa: BLE001 - 日志失败不阻断
                pass

    receipt: dict[str, Any] = {
        "repaired": [], "skipped": [], "opening_prefix_locked": [], "incomplete": []}
    if not CAST_WINDOW_SPLIT_REPAIR_ENABLED:
        return receipt
    declared_opening_ids = _cast_declared_opening_ids(payload)
    try:
        pool_rows = _director_casting_rows(subtitles, executable_ids)
    except Exception as exc:  # noqa: BLE001 - 补齐绝不阻断导演
        _log(f"拆窗补齐跳过（候选池构建失败）：{type(exc).__name__}: {exc}")
        return receipt
    by_id, members = _cast_window_member_index(pool_rows)
    if not members:
        return receipt
    row_by_id = _cast_rows_by_id(subtitles)
    allowed: set[int] = {int(value) for value in (executable_ids or ())} or set(row_by_id)
    allowed &= set(row_by_id)
    max_rows = max(1, int(CAST_WINDOW_SPLIT_REPAIR_MAX_ROWS))
    max_seconds = max(0.0, float(CAST_WINDOW_SPLIT_REPAIR_MAX_SECONDS))

    def _seconds(subtitle_id: int) -> float:
        row = row_by_id.get(subtitle_id) or {}
        try:
            return max(0.0, float(row["end"]) - float(row["start"]))
        except (KeyError, TypeError, ValueError):
            return 0.0

    for strategy_index, strategy in enumerate(_strategy_refs(payload), 1):
        strategy_id = str(strategy.get("strategy_id") or f"S{strategy_index}")
        for chapter_index, chapter in enumerate(_chapter_refs(strategy), 1):
            if not isinstance(chapter, Mapping):
                continue
            chapter_id = str(chapter.get("chapter_id") or f"C{chapter_index}")
            raw_beats = chapter.get("beats") or []
            if isinstance(raw_beats, Mapping):
                raw_beats = [raw_beats]
            if not isinstance(raw_beats, list) or not raw_beats:
                continue
            used = set(_cast_chapter_selected_ids(chapter))
            rebuilt: list[Any] = []
            for beat in raw_beats:
                if not isinstance(beat, Mapping):
                    rebuilt.append(beat)
                    continue
                beat_ids = _cast_chapter_selected_ids({"beats": [beat]})
                if not beat_ids:
                    rebuilt.append(beat)
                    continue
                window_id = by_id.get(beat_ids[0])
                full = list(members.get(window_id) or []) if window_id else []
                if not full:
                    rebuilt.append(beat)
                    continue
                # P0-7：补齐范围放宽到相邻窗相邻的几行。
                scope = (_cast_window_fill_scope(window_id, full, members, max_rows)
                         if (CAST_WINDOW_CROSS_FILL_ENABLED and window_id) else full)
                previous_edge, next_edge = (_cast_window_neighbor_edges(window_id, members)
                                            if (CAST_WINDOW_CROSS_FILL_ENABLED and window_id)
                                            else (None, None))

                def _usable(candidate: int) -> bool:
                    if candidate in used or candidate not in allowed:
                        return False
                    row = row_by_id.get(candidate)
                    if not row or _cast_row_prune_reason(row):
                        return False
                    # 不足半秒又不收句的行补上也没意义（只会变成水词），不补。
                    if (_seconds(candidate) < CAST_WINDOW_SPLIT_REPAIR_MIN_ROW_SECONDS
                            and not _cast_row_closes_thought(row)):
                        return False
                    return True

                # 头部：从上一行倒着走，走到「上一句刚收句」为止（不含那个收句行）
                head_chain: list[int] = []
                cursor = beat_ids[0] - 1
                # P0-7 跨窗：这一窗的第一行就在用，缺口在「上一窗最后一行」上。
                if (cursor not in scope and full and beat_ids[0] == full[0]
                        and previous_edge is not None):
                    cursor = previous_edge
                if (cursor in scope and _usable(cursor)
                        and _cast_row_looks_unfinished(row_by_id.get(cursor) or {})):
                    while (cursor in scope and _usable(cursor) and len(head_chain) < max_rows):
                        head_chain.insert(0, cursor)
                        previous = cursor - 1
                        if (previous not in scope
                                or not _cast_row_looks_unfinished(row_by_id.get(previous) or {})):
                            break
                        cursor = previous
                if sum(_seconds(value) for value in head_chain) > max_seconds:
                    head_chain = []
                # 补进来的头必须「从一句的开头起」：如果走到行数上限还没走到上一句的
                # 收句行，那补上去只是把半句挪了个位置（会变成新的半句头），整条不补。
                if (CAST_WINDOW_SPLIT_REPAIR_CLOSED_ONLY_ENABLED and head_chain
                        and _cast_row_looks_unfinished(row_by_id.get(head_chain[0] - 1) or {})):
                    receipt["incomplete"].append({
                        "chapter_id": chapter_id, "side": "head",
                        "subtitle_ids": list(head_chain), "reason": "chain_not_started",
                    })
                    head_chain = []
                # P0-A 开场门禁：全片第一句的位置由 AI 的开场声明锁定。程序补行
                # 只有让「补行 + 锚点」仍等于声明前缀时才允许排在第一位，否则整句
                # 不补（记审计）—— 不把 AI 判为偏弱的备选句顶到开头。
                if (head_chain and chapter_index == 1 and not rebuilt
                        and not _cast_opening_prefix_allowed(
                            head_chain, beat_ids, declared_opening_ids)):
                    receipt["opening_prefix_locked"].append({
                        "chapter_id": chapter_id, "window_id": window_id,
                        "picked_ids": list(beat_ids), "subtitle_ids": list(head_chain),
                        "text": "/".join(
                            str((row_by_id.get(value) or {}).get("text") or "")[:12]
                            for value in head_chain)[:40],
                    })
                    head_chain = []

                # 尾部：从下一行顺着走，直到某一行收句（含该行）
                tail_chain: list[int] = []
                last_id = beat_ids[-1]
                if _cast_row_looks_unfinished(row_by_id.get(last_id) or {}):
                    cursor = last_id + 1
                    # P0-7 跨窗：缺的续句在下一窗的第一行上。
                    if (cursor not in scope and full and last_id == full[-1]
                            and next_edge is not None):
                        cursor = next_edge
                    while (cursor in scope and _usable(cursor) and len(tail_chain) < max_rows):
                        tail_chain.append(cursor)
                        if _cast_row_closes_thought(row_by_id.get(cursor) or {}):
                            break
                        cursor += 1
                if sum(_seconds(value) for value in tail_chain) > max_seconds:
                    tail_chain = []
                # 补回来的尾巴必须「走到收句行」：没走到就说明这句还没完（只是把半句
                # 往后挪了一行），整条不补 —— 宁可如实报告，也不制造新的半句。
                if (CAST_WINDOW_SPLIT_REPAIR_CLOSED_ONLY_ENABLED and tail_chain
                        and not _cast_row_closes_thought(row_by_id.get(tail_chain[-1]) or {})):
                    receipt["incomplete"].append({
                        "chapter_id": chapter_id, "side": "tail",
                        "subtitle_ids": list(tail_chain), "reason": "chain_not_closed",
                    })
                    tail_chain = []

                blocked_rows = [
                    value for value in (head_chain + tail_chain)
                    if _cast_row_repair_blocked(row_by_id.get(value) or {})
                ]
                if blocked_rows:
                    receipt["skipped"].append({
                        "chapter_id": chapter_id, "window_id": window_id,
                        "picked_ids": list(beat_ids), "subtitle_id": blocked_rows[0],
                        "reason": "blocked_content",
                    })
                    head_chain = []
                    tail_chain = []

                if not head_chain and not tail_chain:
                    if window_id and set(beat_ids) < set(full):
                        receipt["skipped"].append({
                            "chapter_id": chapter_id, "window_id": window_id,
                            "picked_ids": list(beat_ids), "reason": "no_fillable_row",
                        })
                    rebuilt.append(beat)
                    continue

                for value in head_chain:
                    rebuilt.append({
                        "beat_function": str(beat.get("beat_function") or "continuation"),
                        "subtitle_ids": [value],
                        "utterance_complete": True,
                        "continuation_of": value - 1,
                        "window_split_repair": window_id,
                    })
                    used.add(value)
                    receipt["repaired"].append({
                        "subtitle_id": value, "window_id": window_id,
                        "chapter_id": chapter_id, "side": "head",
                        "cross_window": value not in full,
                    })
                rebuilt.append(beat)
                for value in tail_chain:
                    rebuilt.append({
                        "beat_function": str(beat.get("beat_function") or "continuation"),
                        "subtitle_ids": [value],
                        "utterance_complete": True,
                        "continuation_of": last_id,
                        "window_split_repair": window_id,
                    })
                    used.add(value)
                    receipt["repaired"].append({
                        "subtitle_id": value, "window_id": window_id,
                        "chapter_id": chapter_id, "side": "tail",
                        "cross_window": value not in full,
                    })
            chapter["beats"] = rebuilt
    return receipt


def _cast_declared_opening_ids(payload: Mapping[str, Any]) -> list[int]:
    """取 AI 声明的开场字幕 ID（opening_selection.selected_subtitle_ids）。

    取不到就返回空列表 —— 调用方据此按「声明不可用」保守处理。
    """
    def _from(node: Any) -> list[int]:
        if not isinstance(node, Mapping):
            return []
        selection = node.get("opening_selection")
        if not isinstance(selection, Mapping):
            return []
        raw = selection.get("selected_subtitle_ids") or ()
        if isinstance(raw, (str, int)) and not isinstance(raw, bool):
            raw = (raw,)
        values: list[int] = []
        for value in raw or ():
            try:
                subtitle_id = int(value)
            except (TypeError, ValueError):
                continue
            if subtitle_id not in values:
                values.append(subtitle_id)
        return values

    declared = _from(payload)
    if declared:
        return declared
    try:
        declared = _from(_two_pass_primary(payload))
    except Exception:  # noqa: BLE001 - 取不到声明不是错误
        declared = []
    if declared:
        return declared
    try:
        for strategy in _strategy_refs(payload):
            declared = _from(strategy)
            if declared:
                return declared
    except Exception:  # noqa: BLE001 - 取不到声明不是错误
        return []
    return declared


def _cast_opening_prefix_allowed(
    head_ids: Sequence[int],
    anchor_ids: Sequence[int],
    declared_ids: Sequence[int],
) -> bool:
    """补进来的头部行是否仍落在 AI 声明的开场前缀之内。

    全片第一句必须来自 AI 的开场声明：程序补行只有让「补行 + 锚点」正好等于
    声明前缀时才允许排在第一位，否则整句不补 —— 避免把 AI 判为偏弱的备选句
    （实测：#25「好看吧，」）顶到开头。声明不可用时保守不补。
    """
    if not CAST_OPENING_PREFIX_GUARD_ENABLED:
        return True
    if not declared_ids:
        return False
    produced = [int(value) for value in list(head_ids) + list(anchor_ids)]
    expected = [int(value) for value in declared_ids]
    return produced == expected[:len(produced)]


def _cast_row_needs_leadin(row: Mapping[str, Any]) -> bool:
    """章首这句是不是「接着上一句说的」——以强接续连词起头。

    只做字面判定，不看语义；词表刻意收窄到几乎必然是承接的连词，避免误伤
    能独立成句的首句（「其实 / 就是 / 你看」这类不收）。
    """
    text = str(row.get("text") or "").strip()
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in CAST_CHAPTER_HEAD_DEPENDENT_PREFIXES)


def _cast_row_is_edge_stub(row: Mapping[str, Any], seconds: float) -> bool:
    """章首/章尾的「纯语气词残句」：≤1.2s、不收句，且去标点后不超过 3 个字。

    实测 #95「是的，」0.36s / #191「哎呀，」0.68s / #606「真的，」1.07s 属于这类，
    放在章首/章尾只会变成水词。有实义的前导句（如 #81「而且这件衣服有一个很牛
    的点，」0.96s）不在此列 —— 它有内容，只是句子短，不该被判成残句。
    """
    if seconds >= CAST_CHAPTER_EDGE_STUB_SECONDS:
        return False
    if _cast_row_closes_thought(row):
        return False
    text = re.sub(r"[\s，,。.！!？?、：:；;…~～]", "", str(row.get("text") or ""))
    return 0 < len(text) <= CAST_CHAPTER_EDGE_STUB_MAX_CHARS


def _cast_chapter_drop_row(chapter: dict[str, Any], subtitle_id: int) -> bool:
    """从章内移除一个字幕 ID（只移除该行本身；空 Beat 一并移除，不重写原话）。"""
    raw_beats = chapter.get("beats") or []
    if isinstance(raw_beats, Mapping):
        raw_beats = [raw_beats]
    if not isinstance(raw_beats, list):
        return False
    kept: list[Any] = []
    removed = False
    for beat in raw_beats:
        if removed or not isinstance(beat, Mapping):
            kept.append(beat)
            continue
        ids = _cast_chapter_selected_ids({"beats": [beat]})
        if subtitle_id not in ids:
            kept.append(beat)
            continue
        remaining = [value for value in ids if value != subtitle_id]
        removed = True
        if not remaining:
            continue
        item = dict(beat)
        for key in ("source_span", "subtitle_span", "ids", "subtitle_ids"):
            item.pop(key, None)
        item["subtitle_ids"] = remaining
        kept.append(item)
    if not removed:
        return False
    chapter["beats"] = kept
    return True


def _guard_chapter_edges(
    payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_ids: Sequence[int] | None = None,
    *,
    log_fn: Any = None,
) -> dict[str, Any]:
    """章首/章尾收口（确定性、零 AI 调用；只动边界，不改写、不重排、不动中间）。

    ① 章尾停在半句：同一口播窗内还有可用的后续原话、且能一路走到一个收句行时，
       按原顺序补到章尾；走不到收句行就不补（绝不把断得更远的句子硬接上去）。
    ② 补不动、又只是 ≤1.2s 的水词/残句：从章内移出，让边界回退到上一个完整句。
    ③ 章首缺上文（以接续连词起头、上一句未被任何章选中）：补一行前导句（P0-10）。
    ④ 其余情况如实报告，不改句。第一章章首由 AI 的开场声明保护，不参与移出。
    """
    def _log(message: str) -> None:
        if callable(log_fn):
            try:
                log_fn(message)
            except Exception:  # noqa: BLE001 - 日志失败不阻断
                pass

    receipt: dict[str, Any] = {
        "tail_filled": [], "tail_stub_removed": [], "head_stub_removed": [],
        "head_filled": [], "reported": [], "checked_chapters": 0,
    }
    if not CAST_CHAPTER_EDGE_GUARD_ENABLED:
        return receipt
    try:
        pool_rows = _director_casting_rows(subtitles, executable_ids)
    except Exception as exc:  # noqa: BLE001 - 收口绝不阻断导演
        _log(f"章首章尾收口跳过（候选池构建失败）：{type(exc).__name__}: {exc}")
        return receipt
    by_id, members = _cast_window_member_index(pool_rows)
    row_by_id = _cast_rows_by_id(subtitles)
    allowed: set[int] = {int(value) for value in (executable_ids or ())} or set(row_by_id)
    allowed &= set(row_by_id)
    min_row_seconds = max(0.0, float(CAST_WINDOW_SPLIT_REPAIR_MIN_ROW_SECONDS))
    max_rows = max(1, int(CAST_WINDOW_SPLIT_REPAIR_MAX_ROWS))
    max_seconds = max(0.0, float(CAST_WINDOW_SPLIT_REPAIR_MAX_SECONDS))

    def _seconds(subtitle_id: int) -> float:
        row = row_by_id.get(subtitle_id) or {}
        try:
            return max(0.0, float(row["end"]) - float(row["start"]))
        except (KeyError, TypeError, ValueError):
            return 0.0

    def _fillable(candidate: int, strategy_used: set[int]) -> bool:
        if candidate in strategy_used or candidate not in allowed:
            return False
        row = row_by_id.get(candidate)
        if not row or _cast_row_prune_reason(row) or _cast_row_repair_blocked(row):
            return False
        if _seconds(candidate) < min_row_seconds and not _cast_row_closes_thought(row):
            return False
        return True

    for strategy in _strategy_refs(payload):
        strategy_used: set[int] = set()
        for chapter in _chapter_refs(strategy):
            if isinstance(chapter, Mapping):
                for value in _cast_chapter_selected_ids(chapter):
                    strategy_used.add(value)
        for chapter_index, chapter in enumerate(_chapter_refs(strategy), 1):
            if not isinstance(chapter, Mapping):
                continue
            chapter_id = str(chapter.get("chapter_id") or f"C{chapter_index}")
            raw_beats = chapter.get("beats") or []
            if isinstance(raw_beats, Mapping):
                raw_beats = [raw_beats]
            if not isinstance(raw_beats, list) or not raw_beats:
                continue
            receipt["checked_chapters"] += 1

            # ①② 章尾
            for _ in range(3):
                ids = _cast_chapter_selected_ids(chapter)
                if not ids:
                    break
                tail_id = ids[-1]
                tail_row = row_by_id.get(tail_id) or {}
                if not _cast_row_looks_unfinished(tail_row):
                    break
                window_id = by_id.get(tail_id)
                filled: list[int] = []
                if window_id:
                    cursor = tail_id + 1
                    chain: list[int] = []
                    window_members = members.get(window_id) or ()
                    next_edge = None
                    # P0-7：章尾补齐同样允许跨到隔壁窗（缺的半句常在那里）。
                    if CAST_WINDOW_CROSS_FILL_ENABLED:
                        window_members = _cast_window_fill_scope(
                            window_id, list(window_members), members, max_rows)
                        _previous_edge, next_edge = _cast_window_neighbor_edges(
                            window_id, members)
                    own_window_ids = [int(value) for value in (members.get(window_id) or ())]
                    # 章尾那一行已经是本窗最后一行时，续句在「下一窗的第一行」上。
                    if (cursor not in window_members and own_window_ids
                            and tail_id == own_window_ids[-1] and next_edge is not None):
                        cursor = next_edge
                    while (cursor in window_members and len(chain) < max_rows
                           and _fillable(cursor, strategy_used)):
                        chain.append(cursor)
                        if _cast_row_closes_thought(row_by_id.get(cursor) or {}):
                            filled = chain
                            break
                        cursor += 1
                    if filled and sum(_seconds(value) for value in filled) > max_seconds:
                        filled = []
                if filled:
                    beats_for_append = chapter.get("beats") or []
                    if isinstance(beats_for_append, Mapping):
                        beats_for_append = [beats_for_append]
                    for value in filled:
                        beats_for_append.append({
                            "beat_function": "continuation",
                            "subtitle_ids": [value],
                            "utterance_complete": True,
                            "continuation_of": tail_id,
                            "chapter_edge_guard": "tail",
                        })
                        strategy_used.add(value)
                        receipt["tail_filled"].append({
                            "chapter_id": chapter_id, "subtitle_id": value,
                            "window_id": window_id, "text": str(
                                (row_by_id.get(value) or {}).get("text") or "")[:24],
                        })
                    chapter["beats"] = beats_for_append
                    break
                if _cast_row_is_edge_stub(tail_row, _seconds(tail_id)):
                    if not _cast_chapter_drop_row(chapter, tail_id):
                        break
                    strategy_used.discard(tail_id)
                    receipt["tail_stub_removed"].append({
                        "chapter_id": chapter_id, "subtitle_id": tail_id,
                        "text": str(tail_row.get("text") or "")[:24],
                    })
                    continue
                receipt["reported"].append({
                    "chapter_id": chapter_id, "edge": "tail", "subtitle_id": tail_id,
                    "text": str(tail_row.get("text") or "")[:24],
                    "reason": "dangling_tail_kept",
                })
                break

            # ②.5 章首缺上文（P0-10）：只补一行前导句，不改写、不删句
            if CAST_CHAPTER_HEAD_FILL_ENABLED and chapter_index > 1:
                head_ids = _cast_chapter_selected_ids(chapter)
                if head_ids:
                    head_id = head_ids[0]
                    head_row = row_by_id.get(head_id) or {}
                    if _cast_row_needs_leadin(head_row):
                        window_id = by_id.get(head_id)
                        prev_id = head_id - 1
                        if window_id and CAST_WINDOW_CROSS_FILL_ENABLED:
                            own_ids = [int(value) for value in (members.get(window_id) or ())]
                            prev_edge, _next_edge = _cast_window_neighbor_edges(
                                window_id, members)
                            if own_ids and head_id == own_ids[0] and prev_edge is not None:
                                prev_id = prev_edge
                        prev_row = row_by_id.get(prev_id)
                        head_item = {
                            "chapter_id": chapter_id, "edge": "head",
                            "subtitle_id": head_id,
                            "text": str(head_row.get("text") or "")[:24],
                        }
                        if not prev_row:
                            head_item["reason"] = "head_dangling_leadin_no_previous"
                            receipt["reported"].append(head_item)
                        elif _cast_row_needs_leadin(prev_row):
                            head_item["reason"] = "head_dangling_leadin_chain"
                            receipt["reported"].append(head_item)
                        elif not _fillable(prev_id, strategy_used):
                            head_item["reason"] = "head_dangling_leadin_unfillable"
                            head_item["prev_subtitle_id"] = prev_id
                            receipt["reported"].append(head_item)
                        else:
                            beats_for_prepend = chapter.get("beats") or []
                            if isinstance(beats_for_prepend, Mapping):
                                beats_for_prepend = [beats_for_prepend]
                            chapter["beats"] = [{
                                "beat_function": "leadin",
                                "subtitle_ids": [prev_id],
                                "utterance_complete": True,
                                "leadin_to": head_id,
                                "chapter_edge_guard": "head",
                            }] + list(beats_for_prepend)
                            strategy_used.add(prev_id)
                            head_item["prev_subtitle_id"] = prev_id
                            head_item["window_id"] = window_id
                            head_item["prev_text"] = str(
                                (row_by_id.get(prev_id) or {}).get("text") or "")[:24]
                            receipt["head_filled"].append(head_item)

            # ③ 章首（第一章由 AI 的开场声明保护，不参与移出）
            if chapter_index == 1:
                continue
            for _ in range(3):
                ids = _cast_chapter_selected_ids(chapter)
                if not ids:
                    break
                head_id = ids[0]
                head_row = row_by_id.get(head_id) or {}
                raw_first = chapter.get("beats") or []
                first_beat = raw_first[0] if isinstance(raw_first, list) and raw_first else raw_first
                if isinstance(first_beat, Mapping) and str(
                        first_beat.get("chapter_edge_guard") or "") == "head":
                    break  # 刚补进来的前导句，不再判移出
                if not _cast_row_is_edge_stub(head_row, _seconds(head_id)):
                    break
                if not _cast_chapter_drop_row(chapter, head_id):
                    break
                strategy_used.discard(head_id)
                receipt["head_stub_removed"].append({
                    "chapter_id": chapter_id, "subtitle_id": head_id,
                    "text": str(head_row.get("text") or "")[:24],
                })
    return receipt


def _cast_half_sentence_tail_gate(
    payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_ids: Sequence[int] | None = None,
    *,
    log_fn: Any = None,
) -> dict[str, Any]:
    """章尾半句收口（P0-9；确定性、零 AI 调用；只动章尾，不改写、不重排）。

    一章的最后一句「话没说完」（以逗号这类句内标点结尾），而它的续句确实存在、
    但拿不到（已被别的章用掉 / 不可执行 / 被剔成坏行 / 不足半秒又不收句）时：
      mode="drop"（默认）→ 把这半句从章内移出：宁可少一句，也不把半句交出去；
      mode="keep"        → 只报告，不改句。
    续句本来就不存在（素材到此为止）不算半句，不动；续句可补的交给拆窗补齐与
    章尾收口去补，这里只记一条 completed_hint，不重复动手。
    """
    def _log(message: str) -> None:
        if callable(log_fn):
            try:
                log_fn(message)
            except Exception:  # noqa: BLE001 - 日志失败不阻断
                pass

    receipt: dict[str, Any] = {"dropped": [], "reported": [], "completed_hint": []}
    if not CAST_HALF_SENTENCE_TAIL_GUARD_ENABLED:
        return receipt
    try:
        _director_casting_rows(subtitles, executable_ids)
    except Exception as exc:  # noqa: BLE001 - 收口绝不阻断导演
        _log(f"章尾半句收口跳过（候选池构建失败）：{type(exc).__name__}: {exc}")
        return receipt
    row_by_id = _cast_rows_by_id(subtitles)
    allowed: set[int] = {int(value) for value in (executable_ids or ())} or set(row_by_id)
    allowed &= set(row_by_id)
    min_row_seconds = max(0.0, float(CAST_WINDOW_SPLIT_REPAIR_MIN_ROW_SECONDS))
    mode = str(CAST_HALF_SENTENCE_TAIL_MODE or "drop").strip().lower()

    def _seconds(subtitle_id: int) -> float:
        row = row_by_id.get(subtitle_id) or {}
        try:
            return max(0.0, float(row["end"]) - float(row["start"]))
        except (KeyError, TypeError, ValueError):
            return 0.0

    for strategy_index, strategy in enumerate(_strategy_refs(payload), 1):
        strategy_id = str(strategy.get("strategy_id") or f"S{strategy_index}")
        strategy_used: set[int] = set()
        for chapter in _chapter_refs(strategy):
            if isinstance(chapter, Mapping):
                strategy_used |= set(_cast_chapter_selected_ids(chapter))
        for chapter_index, chapter in enumerate(_chapter_refs(strategy), 1):
            if not isinstance(chapter, Mapping):
                continue
            chapter_id = str(chapter.get("chapter_id") or f"C{chapter_index}")
            for _ in range(3):
                ids = _cast_chapter_selected_ids(chapter)
                if not ids:
                    break
                tail_id = ids[-1]
                tail_row = row_by_id.get(tail_id) or {}
                if not _cast_row_looks_unfinished(tail_row):
                    break
                next_id = tail_id + 1
                next_row = row_by_id.get(next_id)
                if not next_row:
                    break  # 素材到此为止：这不是半句
                if next_id in strategy_used:
                    reason = "continuation_used_elsewhere"
                elif next_id not in allowed:
                    reason = "continuation_not_executable"
                elif _cast_row_prune_reason(next_row):
                    reason = "continuation_pruned_row"
                elif _cast_row_repair_blocked(next_row):
                    reason = "continuation_blocked_content"
                elif (_seconds(next_id) < min_row_seconds
                      and not _cast_row_closes_thought(next_row)):
                    reason = "continuation_too_short"
                else:
                    receipt["completed_hint"].append({
                        "chapter_id": chapter_id, "subtitle_id": tail_id,
                        "reason": "continuation_fillable",
                    })
                    break
                item = {
                    "chapter_id": chapter_id, "subtitle_id": tail_id,
                    "next_subtitle_id": next_id, "reason": reason,
                    "text": str(tail_row.get("text") or "")[:24],
                    "next_text": str(next_row.get("text") or "")[:24],
                }
                if mode == "drop" and len(ids) > 1 and _cast_chapter_drop_row(chapter, tail_id):
                    strategy_used.discard(tail_id)
                    receipt["dropped"].append(item)
                    continue
                receipt["reported"].append(item)
                break
    return receipt


def _apply_cast_deterministic_repairs(
    payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_ids: Sequence[int] | None = None,
    *,
    budget_seconds: float | None = None,
    log_fn: Any = None,
) -> dict[str, Any]:
    """拆窗补齐 + 整窗整取 + 章首章尾收口：确定性、零 AI 调用、不改写、不重排。

    校准修订版是一条全新片单，采纳之后同样要过这三道；否则「采纳修订」等于把
    三处确定性补齐全部丢掉，交付的片单又回到未修补状态。
    """
    def _log(message: str) -> None:
        if callable(log_fn):
            try:
                log_fn(message)
            except Exception:  # noqa: BLE001 - 日志失败不阻断
                pass

    receipts: dict[str, Any] = {}
    try:
        receipts["split_repair"] = _repair_cast_window_splits(
            payload, subtitles, executable_ids, log_fn=log_fn)
    except Exception as exc:  # noqa: BLE001 - 补齐绝不阻断导演
        receipts["split_repair"] = {"repaired": [], "skipped": []}
        _log(f"拆窗补齐异常（已跳过）：{type(exc).__name__}: {exc}")
    try:
        receipts["whole_keep"] = _expand_cast_windows_whole(
            payload, subtitles, executable_ids, budget_seconds=budget_seconds, log_fn=log_fn)
    except Exception as exc:  # noqa: BLE001 - 整取绝不阻断导演
        receipts["whole_keep"] = {}
        _log(f"整窗整取异常（已跳过）：{type(exc).__name__}: {exc}")
    try:
        receipts["chapter_edges"] = _guard_chapter_edges(
            payload, subtitles, executable_ids, log_fn=log_fn)
    except Exception as exc:  # noqa: BLE001 - 收口绝不阻断导演
        receipts["chapter_edges"] = {}
        _log(f"章首章尾收口异常（已跳过）：{type(exc).__name__}: {exc}")
    try:
        receipts["half_sentence"] = _cast_half_sentence_tail_gate(
            payload, subtitles, executable_ids, log_fn=log_fn)
    except Exception as exc:  # noqa: BLE001 - 收口绝不阻断导演
        receipts["half_sentence"] = {}
        _log(f"章尾半句收口异常（已跳过）：{type(exc).__name__}: {exc}")
    repaired = len((receipts.get("split_repair") or {}).get("repaired") or [])
    kept = len((receipts.get("whole_keep") or {}).get("kept") or [])
    edges = receipts.get("chapter_edges") or {}
    edge_changes = (len(edges.get("tail_filled") or [])
                    + len(edges.get("tail_stub_removed") or [])
                    + len(edges.get("head_stub_removed") or [])
                    + len(edges.get("head_filled") or []))
    half_changes = len((receipts.get("half_sentence") or {}).get("dropped") or [])
    if repaired or kept or edge_changes or half_changes:
        _log(f"确定性补齐（修订版）：拆窗 {repaired} 句、整窗整取 {kept} 句、"
             f"首尾收口 {edge_changes} 处、章尾半句收口 {half_changes} 处。")
    return receipts


def _whole_kept_beat(
    beat: Mapping[str, Any],
    subtitle_id: int,
    window_id: str | None,
) -> dict[str, Any]:
    """整窗整取补进来的一行：沿用锚点 Beat 的角色字段，只换字幕 ID。"""
    item = dict(beat)
    for key in (
        "ids", "subtitle_ids", "source_span", "subtitle_span", "sec",
        "beat_id", "replaces_beat_id", "opening_fallback_subtitle_ids",
        "opening_backup_subtitle_ids",
    ):
        item.pop(key, None)
    item["subtitle_ids"] = [subtitle_id]
    item["utterance_complete"] = True
    item["window_whole_keep"] = window_id
    return item


def _cast_opening_closing_gate(
    payload: Mapping[str, Any],
    row_by_id: Mapping[int, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """首尾门禁（确定性、只读）：只报告，不改写、不重排、不删句。

    开头看「第一句是不是残句 / 与前几句是否同义重复」；
    收尾章看「最后一句是不是 <1.2s 残句 / 与前句是否同义重复 / 有没有收尾动作词」。
    """
    import difflib

    opening: list[dict[str, Any]] = []
    closing: list[dict[str, Any]] = []
    try:
        chapters = [
            chapter
            for strategy in _strategy_refs(payload)
            for chapter in _chapter_refs(strategy)
            if isinstance(chapter, Mapping)
        ]
    except Exception:  # noqa: BLE001 - 门禁绝不阻断导演
        return opening, closing
    if not chapters:
        return opening, closing

    def _text(subtitle_id: int) -> str:
        return str((row_by_id.get(subtitle_id) or {}).get("text") or "").strip()

    def _seconds(subtitle_id: int) -> float:
        row = row_by_id.get(subtitle_id) or {}
        try:
            return max(0.0, float(row["end"]) - float(row["start"]))
        except (KeyError, TypeError, ValueError):
            return 0.0

    first_ids = _cast_chapter_selected_ids(chapters[0])
    if first_ids:
        head_text = _text(first_ids[0])
        if _cast_row_looks_unfinished(row_by_id.get(first_ids[0]) or {}):
            opening.append({"kind": "opening_unfinished", "text": head_text[:24]})
        for other in first_ids[1:4]:
            other_text = _text(other)
            if not head_text or not other_text:
                continue
            if difflib.SequenceMatcher(None, head_text, other_text).ratio() > CAST_CHAPTER_MAX_TEXT_OVERLAP:
                opening.append({"kind": "opening_repeat", "text": other_text[:24]})
                break

    last_ids = _cast_chapter_selected_ids(chapters[-1])
    if last_ids:
        tail_id = last_ids[-1]
        tail_text = _text(tail_id)
        if _seconds(tail_id) < CAST_BEAT_MIN_FRAGMENT_SECONDS and not _cast_row_closes_thought(row_by_id.get(tail_id) or {}):
            closing.append({"kind": "closing_short_tail", "text": tail_text[:24]})
        if len(last_ids) > 1:
            previous_text = _text(last_ids[-2])
            if tail_text and previous_text and difflib.SequenceMatcher(None, tail_text, previous_text).ratio() > CAST_CHAPTER_MAX_TEXT_OVERLAP:
                closing.append({"kind": "closing_repeat", "text": tail_text[:24]})
        chapter_text = "".join(_text(value) for value in last_ids)
        if chapter_text and not CAST_WHOLE_KEEP_CLOSE_ACTION_PATTERN.search(chapter_text):
            closing.append({"kind": "closing_no_close_action", "text": chapter_text[:24]})
    return opening, closing


def _expand_cast_windows_whole(
    payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_ids: Sequence[int] | None = None,
    *,
    budget_seconds: float | None = None,
    log_fn: Any = None,
) -> dict[str, Any]:
    """整窗整取：AI 选中口播窗里剩下的行，按原顺序补回来。

    确定性；零 AI 调用；不改写、不重排；绝不删改 AI 原本选中的行。
    预算不够时按「离已选行远的先不补」少补，取舍如实记进 receipt。
    """
    def _log(message: str) -> None:
        if callable(log_fn):
            try:
                log_fn(message)
            except Exception:  # noqa: BLE001 - 日志失败不阻断
                pass

    receipt: dict[str, Any] = {
        "kept": [], "dropped": [], "window_count": 0, "strategies": [],
        "base_seconds": 0.0, "added_seconds": 0.0, "projected_seconds": 0.0,
        "budget_seconds": budget_seconds, "opening_gate": [], "closing_gate": [],
        "opening_prefix_locked": [],
    }
    if not CAST_WINDOW_WHOLE_KEEP_ENABLED:
        return receipt
    declared_opening_ids = _cast_declared_opening_ids(payload)
    try:
        pool_rows = _director_casting_rows(subtitles, executable_ids)
    except Exception as exc:  # noqa: BLE001 - 整取绝不阻断导演
        _log(f"整窗整取跳过（候选池构建失败）：{type(exc).__name__}: {exc}")
        return receipt
    by_id, members = _cast_window_member_index(pool_rows)
    if not members:
        return receipt
    row_by_id = _cast_rows_by_id(subtitles)
    allowed: set[int] = {int(value) for value in (executable_ids or ())} or set(row_by_id)
    allowed &= set(row_by_id)
    min_row_seconds = max(0.0, float(CAST_WINDOW_WHOLE_KEEP_MIN_ROW_SECONDS))

    def _seconds(subtitle_id: int) -> float:
        row = row_by_id.get(subtitle_id) or {}
        try:
            return max(0.0, float(row["end"]) - float(row["start"]))
        except (KeyError, TypeError, ValueError):
            return 0.0

    def _fillable(subtitle_id: int) -> bool:
        if subtitle_id not in allowed:
            return False
        row = row_by_id.get(subtitle_id)
        if not row or _cast_row_repair_blocked(row) or _cast_row_prune_reason(row):
            return False
        if _seconds(subtitle_id) < min_row_seconds and not _cast_row_closes_thought(row):
            return False
        return True

    # 第一遍：只算「要补哪些行」，不落盘 —— 先做预算取舍。
    plan: list[dict[str, Any]] = []
    for strategy_index, strategy in enumerate(_strategy_refs(payload), 1):
        # 方案之间是并列备选方案，各自都可以用同一批原话；认领只在本方案内生效。
        claimed: set[int] = set()
        # [止血D 2026-10-06] 归属章唯一：本章之外的行一律不可补，绝不跨章抢句。
        _chapter_owned: dict[int, str] = {}
        if CAST_WHOLE_KEEP_CHAPTER_EXCLUSIVE_ENABLED:
            for _owner in _chapter_refs(strategy):
                if not isinstance(_owner, Mapping):
                    continue
                _owner_id = str(_owner.get("chapter_id") or "")
                for _sid in _cast_chapter_selected_ids(_owner):
                    _chapter_owned.setdefault(int(_sid), _owner_id)
        for chapter_index, chapter in enumerate(_chapter_refs(strategy), 1):
            if not isinstance(chapter, Mapping):
                continue
            raw_beats = chapter.get("beats") or []
            if isinstance(raw_beats, Mapping):
                raw_beats = [raw_beats]
            if not isinstance(raw_beats, list) or not raw_beats:
                continue
            used = set(_cast_chapter_selected_ids(chapter))
            picked_by_window: dict[str, list[int]] = {}
            for beat in raw_beats:
                beat_ids = _cast_chapter_selected_ids({"beats": [beat]})
                if not beat_ids:
                    continue
                window_id = by_id.get(beat_ids[0])
                if not window_id:
                    continue
                bucket = picked_by_window.setdefault(window_id, [])
                for subtitle_id in beat_ids:
                    if subtitle_id not in bucket:
                        bucket.append(subtitle_id)
            additions: list[dict[str, Any]] = []
            for window_id, picked in picked_by_window.items():
                lowest, highest = min(picked), max(picked)
                for value in members.get(window_id) or ():
                    subtitle_id = int(value)
                    if subtitle_id in used or subtitle_id in claimed or not _fillable(subtitle_id):
                        continue
                    if (CAST_WHOLE_KEEP_CHAPTER_EXCLUSIVE_ENABLED
                            and subtitle_id in _chapter_owned):
                        continue
                    additions.append({
                        "subtitle_id": subtitle_id,
                        "window_id": window_id,
                        "seconds": _seconds(subtitle_id),
                        # 越靠近已选行越可能是承接句；远的先不补。
                        "distance": min(abs(subtitle_id - value) for value in picked),
                        "before": subtitle_id < lowest,
                        "after": subtitle_id > highest,
                    })
            claimed.update(item["subtitle_id"] for item in additions)
            plan.append({
                "chapter": chapter,
                "chapter_id": str(chapter.get("chapter_id") or f"C{chapter_index}"),
                "chapter_index": chapter_index,
                "strategy_index": strategy_index,
                "picked_by_window": picked_by_window,
                "additions": additions,
            })

    # 预算按「方案」算：每个方案都是一条独立成片，各自要落在时长区间内。
    strategy_ids: dict[int, list[int]] = {}
    strategy_add: dict[int, float] = {}
    for entry in plan:
        index = int(entry["strategy_index"])
        bucket = strategy_ids.setdefault(index, [])
        for subtitle_id in _cast_chapter_selected_ids(entry["chapter"]):
            if subtitle_id not in bucket:
                bucket.append(subtitle_id)
        strategy_add[index] = strategy_add.get(index, 0.0) + sum(
            item["seconds"] for item in entry["additions"])
    strategy_base = {index: sum(_seconds(value) for value in ids) for index, ids in strategy_ids.items()}
    strategy_ratio: dict[int, float] = {}
    for index, base in strategy_base.items():
        ratio = 1.0
        if budget_seconds is not None and float(budget_seconds) > 0:
            room = float(budget_seconds) - base
            added = strategy_add.get(index, 0.0)
            if added > room:
                ratio = max(0.0, room / added) if added > 0 else 0.0
        strategy_ratio[index] = ratio
    base_seconds = sum(strategy_base.values())

    # 第二遍：按预算落盘。预算按「方案」整体分配，避免某一章被饿死。
    added_total = 0.0
    kept_by_strategy: dict[int, set[int]] = {}
    for index in sorted(strategy_ids):
        room = float("inf")
        if budget_seconds is not None and float(budget_seconds) > 0:
            room = max(0.0, float(budget_seconds) - strategy_base[index])
        candidates = [
            (entry, item)
            for entry in plan if int(entry["strategy_index"]) == index
            for item in entry["additions"]
        ]
        kept_ids: set[int] = set()
        spent = 0.0
        # 由近到远保留：离已选行越近，越可能是承接句；同距离优先长内容句。
        for entry, item in sorted(candidates, key=lambda node: (node[1]["distance"], -node[1]["seconds"])):
            if spent + item["seconds"] <= room + 1e-6:
                kept_ids.add(item["subtitle_id"])
                spent += item["seconds"]
        kept_by_strategy[index] = kept_ids
        for entry, item in candidates:
            if item["subtitle_id"] not in kept_ids:
                receipt["dropped"].append({
                    "chapter_id": entry["chapter_id"], "window_id": item["window_id"],
                    "subtitle_id": item["subtitle_id"], "reason": "budget",
                })

    for entry in plan:
        chapter = entry["chapter"]
        kept = [
            item for item in entry["additions"]
            if item["subtitle_id"] in kept_by_strategy.get(int(entry["strategy_index"]), set())
        ]
        if not kept:
            continue
        by_window: dict[str, list[int]] = {}
        for item in kept:
            by_window.setdefault(item["window_id"], []).append(item["subtitle_id"])
        beats = chapter.get("beats") or []
        if isinstance(beats, Mapping):
            beats = [beats]
        first_position: dict[str, int] = {}
        last_position: dict[str, int] = {}
        for position, beat in enumerate(beats):
            beat_ids = _cast_chapter_selected_ids({"beats": [beat]})
            if not beat_ids:
                continue
            window_id = by_id.get(beat_ids[0])
            if not window_id:
                continue
            first_position.setdefault(window_id, position)
            last_position[window_id] = position
        rebuilt: list[Any] = []
        inserted: set[int] = set()
        for position, beat in enumerate(beats):
            for window_id, first_at in first_position.items():
                if first_at != position:
                    continue
                picked = entry["picked_by_window"].get(window_id) or []
                if not picked:
                    continue
                lowest = min(picked)
                head_values = sorted(v for v in by_window.get(window_id, []) if v < lowest)
                # P0-A 开场门禁：全片第一句只能来自 AI 的开场声明。
                if (head_values and int(entry.get("chapter_index") or 0) == 1 and not rebuilt
                        and not _cast_opening_prefix_allowed(
                            head_values, _cast_chapter_selected_ids({"beats": [beat]}),
                            declared_opening_ids)):
                    for value in head_values:
                        receipt["opening_prefix_locked"].append({
                            "chapter_id": entry["chapter_id"], "window_id": window_id,
                            "subtitle_id": value,
                            "text": str((row_by_id.get(value) or {}).get("text") or "")[:24],
                        })
                    head_values = []
                for value in head_values:
                    if value not in inserted:
                        rebuilt.append(_whole_kept_beat(beat, value, window_id))
                        inserted.add(value)
            rebuilt.append(beat)
            for window_id, last_at in last_position.items():
                if last_at != position:
                    continue
                picked = entry["picked_by_window"].get(window_id) or []
                if not picked:
                    continue
                highest = max(picked)
                for value in sorted(v for v in by_window.get(window_id, []) if v > highest):
                    if value not in inserted:
                        rebuilt.append(_whole_kept_beat(beat, value, window_id))
                        inserted.add(value)
        chapter["beats"] = rebuilt
        for item in kept:
            if item["subtitle_id"] in inserted:
                added_total += item["seconds"]
                receipt["kept"].append({
                    "chapter_id": entry["chapter_id"], "window_id": item["window_id"],
                    "subtitle_id": item["subtitle_id"],
                    "side": "head" if item["before"] else "tail",
                })

    receipt["window_count"] = len({item["window_id"] for item in receipt["kept"]})
    receipt["strategies"] = [
        {
            "strategy_index": index,
            "base_seconds": round(strategy_base[index], 3),
            "addition_seconds": round(strategy_add.get(index, 0.0), 3),
            "keep_ratio": round(strategy_ratio.get(index, 1.0), 4),
            "projected_seconds": round(strategy_base[index] + strategy_add.get(index, 0.0)
                                       * strategy_ratio.get(index, 1.0), 3),
        }
        for index in sorted(strategy_base)
    ]
    receipt["base_seconds"] = round(base_seconds, 3)
    receipt["added_seconds"] = round(added_total, 3)
    receipt["projected_seconds"] = round(base_seconds + added_total, 3)
    receipt["opening_gate"], receipt["closing_gate"] = _cast_opening_closing_gate(payload, row_by_id)
    return receipt


def _cast_window_continuity_issues(
    casting_payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Report chapters whose Beats are stitched from distant source windows.

    Deterministic and read-only.  This checker never rewrites, reorders or drops
    a line: the caller turns the report into at most one bounded repair request
    so the AI keeps authorship of its own story.
    """
    import difflib

    rows = _cast_rows_by_id(subtitles)
    issues: list[dict[str, Any]] = []
    for strategy_index, strategy in enumerate(_strategy_refs(casting_payload), 1):
        strategy_id = str(strategy.get("strategy_id") or f"S{strategy_index}")
        for chapter_index, chapter in enumerate(_chapter_refs(strategy), 1):
            if not isinstance(chapter, Mapping):
                continue
            chapter_id = str(chapter.get("chapter_id") or f"C{chapter_index}")
            spans = _cast_chapter_beat_spans(chapter, rows)
            if not spans:
                continue
            chapter_start = min(item["start"] for item in spans)
            chapter_end = max(item["end"] for item in spans)
            span_seconds = round(chapter_end - chapter_start, 2)
            is_opening = chapter_index == 1
            if not is_opening and (
                len(spans) < 2 or span_seconds < CAST_CHAPTER_MIN_CLOSED_SECONDS
            ):
                issues.append({
                    "kind": "chapter_not_closed",
                    "strategy_id": strategy_id,
                    "chapter_id": chapter_id,
                    "beat_index": len(spans),
                    "span_seconds": span_seconds,
                    "text": " / ".join(item["text"][:18] for item in spans)[:60],
                })
            if span_seconds > CAST_CHAPTER_MAX_SPAN_SECONDS:
                issues.append({
                    "kind": "chapter_span_too_wide",
                    "strategy_id": strategy_id,
                    "chapter_id": chapter_id,
                    "span_seconds": span_seconds,
                })
            for position, span in enumerate(spans):
                duration = round(span["end"] - span["start"], 2)
                if duration < CAST_BEAT_MIN_FRAGMENT_SECONDS:
                    issues.append({
                        "kind": "beat_too_short",
                        "strategy_id": strategy_id,
                        "chapter_id": chapter_id,
                        "beat_index": span["beat_index"],
                        "duration_seconds": duration,
                        "text": span["text"][:24],
                    })
                if position == 0:
                    continue
                gap = round(span["start"] - spans[position - 1]["end"], 2)
                if gap > CAST_CHAPTER_MAX_BEAT_GAP_SECONDS:
                    issues.append({
                        "kind": "beat_window_far_gap",
                        "strategy_id": strategy_id,
                        "chapter_id": chapter_id,
                        "beat_index": span["beat_index"],
                        "gap_seconds": gap,
                        "text": span["text"][:24],
                    })
            for left in range(len(spans)):
                for right in range(left + 1, len(spans)):
                    text_a = spans[left]["text"]
                    text_b = spans[right]["text"]
                    if not text_a or not text_b:
                        continue
                    ratio = difflib.SequenceMatcher(None, text_a, text_b).ratio()
                    if ratio > CAST_CHAPTER_MAX_TEXT_OVERLAP:
                        issues.append({
                            "kind": "chapter_repeat_evidence",
                            "strategy_id": strategy_id,
                            "chapter_id": chapter_id,
                            "beat_index": spans[right]["beat_index"],
                            "overlap": round(ratio, 3),
                            "text": text_b[:24],
                        })
    return issues


def _format_cast_continuity_issues(issues: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for item in issues or ():
        label = _CAST_CONTINUITY_LABELS.get(
            str(item.get("kind")), str(item.get("kind") or "未知问题"),
        )
        parts = [f"{item.get('chapter_id') or '?'} 章"]
        if item.get("beat_index"):
            parts.append(f"第 {item['beat_index']} 段")
        for key, unit in (
            ("gap_seconds", "间隔"), ("span_seconds", "跨度"), ("duration_seconds", "时长"),
        ):
            if item.get(key) is not None:
                parts.append(f"{unit} {item[key]} 秒")
        if item.get("text"):
            parts.append("「" + str(item["text"])[:24] + "」")
        lines.append("  - " + " · ".join(parts) + " → " + label)
    return "\n".join(lines)


def _audit_cast_window_continuity(
    casting_payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    *,
    executable_ids: Sequence[int] | None = None,
    stage_response_hook: Any = None,
    log_fn: Any = None,
) -> list[dict[str, Any]]:
    """Report chapter-continuity defects without spending a single extra call.

    The Casting leg is contractually limited to one story call plus one casting
    call, so this audit never asks the model again and never reorders, rewrites
    or drops a line by itself.  It only surfaces the deterministic verdict into
    the run log and the selection audit so the constraint can be judged on real
    data first.
    """

    def _log(message: str) -> None:
        if callable(log_fn):
            try:
                log_fn(message)
            except Exception:  # noqa: BLE001 - 日志失败不阻断
                pass

    try:
        issues = _cast_window_continuity_issues(casting_payload, subtitles)
    except Exception as exc:  # noqa: BLE001 - 校验绝不阻断导演
        _log(f"章内连续性校验异常（已跳过）：{type(exc).__name__}: {exc}")
        return []
    # P0-1：拆窗检测项（确定性、只读）。AI 只取了一个连续口播窗的一部分时报警，
    # 交给随后的确定性补齐处理；这里不改变任何选句。
    try:
        issues.extend(_cast_window_split_issues(
            casting_payload, _director_casting_rows(subtitles, executable_ids),
        ))
    except Exception as exc:  # noqa: BLE001 - 拆窗体检绝不影响主流程
        _log(f"拆窗检测异常（已跳过）：{type(exc).__name__}: {exc}")
    if not issues:
        _log("章内连续性校验通过：没有跨时间拼接的章节。")
        return []
    kinds: dict[str, int] = {}
    for item in issues:
        key = str(item.get("kind") or "unknown")
        kinds[key] = kinds.get(key, 0) + 1
    _log(
        f"章内连续性校验发现 {len(issues)} 处问题（本次只报告，不追加调用）："
        + _format_cast_continuity_issues(issues)
    )
    if stage_response_hook:
        try:
            stage_response_hook("cast_window_continuity", json.dumps({
                "issue_count": len(issues),
                "kind_counts": kinds,
                "issues": issues,
            }, ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            pass
    return issues

def _cast_beat_cardinality_issues(casting_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return non-one-ID final Beats without changing the AI sequence."""
    issues: list[dict[str, Any]] = []
    for strategy_index, strategy in enumerate(_strategy_refs(casting_payload), 1):
        strategy_id = str(strategy.get("strategy_id") or f"S{strategy_index}")
        for chapter_index, chapter in enumerate(_chapter_refs(strategy), 1):
            chapter_id = str(chapter.get("chapter_id") or f"C{chapter_index}")
            raw_beats = chapter.get("beats") or ()
            if isinstance(raw_beats, Mapping):
                raw_beats = (raw_beats,)
            for beat_index, beat in enumerate(raw_beats, 1):
                if not isinstance(beat, Mapping):
                    continue
                raw_ids = beat.get("subtitle_ids", beat.get("ids", ()))
                if isinstance(raw_ids, (str, int)):
                    raw_ids = (raw_ids,)
                ids: list[int] = []
                for value in raw_ids or ():
                    try:
                        subtitle_id = int(value)
                    except (TypeError, ValueError):
                        continue
                    if subtitle_id > 0:
                        ids.append(subtitle_id)
                if len(ids) != 1:
                    issues.append({
                        "strategy_id": strategy_id,
                        "chapter_id": chapter_id,
                        "beat_index": beat_index,
                        "subtitle_ids": ids,
                    })
    return issues


def _gross_duration_contract_violation(duration_audit: Mapping[str, Any]) -> bool:
    """Detect a malformed cast that is far outside the editable range.

    Both directions count. An under-length selection is the common
    shortfall, and it must be able to trigger the one bounded calibration
    call; the over-length side keeps its original 1.35 scale.
    """
    contract = duration_audit.get("duration_contract") or {}
    try:
        source_seconds = float(duration_audit.get("source_seconds") or 0.0)
        source_max = float(contract.get("source_max") or 0.0)
        source_min = float(contract.get("source_min") or 0.0)
    except (TypeError, ValueError):
        return False
    if source_max > 0 and source_seconds > source_max * 1.35:
        return True
    if source_min > 0 and source_seconds < source_min / 1.35:
        return True
    return False


def build_two_pass_draft_audit(
    *,
    initial_draft: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None = None,
    target_duration: float = 60.0,
    duration_tolerance: float | None = None,
    output_speed_factor: float = 1.0,
) -> dict[str, Any]:
    """Measure the first AI draft without changing any semantic decision.

    ``output_speed_factor`` 必须传进来（2026-10-09 修复）：用户设的「变速 1.28x」
    意味着 60 秒成片要选 76.8 秒**原声**。此前这里用的是不含变速的旧合同，
    却拿**原声秒数**去比**成片秒数**阈值 —— 单位不一致，变速一开就判错。
    现在统一用含变速的交付合同，原声秒数只和 source_min/source_max 比。
    """
    pool = _director_casting_rows(subtitles, executable_subtitle_ids)
    pool_by_id = {int(row["id"]): row for row in pool}
    primary = _two_pass_primary(initial_draft)
    selected_ids: list[int] = []
    malformed_spans: list[str] = []
    for index, beat in enumerate(_two_pass_beat_rows(primary), 1):
        span = beat.get("source_span") or beat.get("subtitle_span") or {}
        raw_ids: list[Any] = []
        if isinstance(span, Mapping):
            start_id = span.get("start_id") or span.get("start_subtitle_id")
            end_id = span.get("end_id") or span.get("end_subtitle_id") or start_id
            try:
                start_int = int(start_id or 0)
                end_int = int(end_id or 0)
            except (TypeError, ValueError):
                start_int = end_int = 0
            if start_int and end_int:
                if start_int != end_int:
                    malformed_spans.append(f"beat_{index}:start_id_must_equal_end_id")
                raw_ids = [start_int]
        if not raw_ids:
            raw = beat.get("subtitle_ids") or ()
            raw_ids = [raw] if isinstance(raw, (str, int)) else list(raw)
        for value in raw_ids:
            try:
                selected_ids.append(int(value))
            except (TypeError, ValueError):
                malformed_spans.append(f"beat_{index}:invalid_subtitle_id")

    invalid_ids = [value for value in selected_ids if value not in pool_by_id]
    valid_ids = [value for value in selected_ids if value in pool_by_id]
    duplicate_ids = sorted({value for value in valid_ids if valid_ids.count(value) > 1})
    actual_seconds = round(sum(pool_by_id[value]["end"] - pool_by_id[value]["start"] for value in valid_ids), 3)
    # 含变速的交付合同：actual_seconds 是**原声**秒数，所以只能和 source_* 比。
    duration_range = director_delivery_duration_range(
        target_duration, duration_tolerance, output_speed_factor,
    )
    source_low = float(duration_range.get("source_min") or duration_range["preferred_low"])
    source_high = float(duration_range.get("source_max") or duration_range["preferred_high"])
    warnings: list[str] = list(malformed_spans)
    if invalid_ids:
        warnings.append("unavailable_or_outside_1_to_8_second_ids:" + ",".join(map(str, invalid_ids)))
    if duplicate_ids:
        warnings.append("duplicate_subtitle_ids:" + ",".join(map(str, duplicate_ids)))
    for position, subtitle_id in enumerate(valid_ids, 1):
        text = str(pool_by_id[subtitle_id].get("text") or "").strip()
        surface_issue = final_utterance_surface_issue(text)
        if surface_issue:
            warnings.append(f"beat_{position}_surface:{surface_issue}")
        if re.match(r"^(?:的(?:这|那|个|款|种)?|然后|因为|所以|出来吗|哥[，,]?然后)", text):
            warnings.append(f"beat_{position}_possible_dangling_context:id={subtitle_id}")
    if actual_seconds < source_low:
        warnings.append(f"duration_shortfall:{source_low - actual_seconds:.3f}s")
    elif actual_seconds > source_high:
        warnings.append(f"duration_overflow:{actual_seconds - source_high:.3f}s")
    unused_rows = [row for row in pool if int(row["id"]) not in set(valid_ids)]
    return {
        **duration_range,
        "actual_seconds": actual_seconds,
        "selected_beat_count": len(valid_ids),
        "selected_subtitle_ids": valid_ids,
        "invalid_subtitle_ids": invalid_ids,
        "duplicate_subtitle_ids": duplicate_ids,
        "warnings": warnings,
        "target_range_fulfilled": (
            not invalid_ids
            and not duplicate_ids
            and source_low <= actual_seconds <= source_high
        ),
        "complete_pool_count": len(pool),
        "complete_pool_seconds": round(sum(row["end"] - row["start"] for row in pool), 3),
        "unused_pool_count": len(unused_rows),
        "unused_pool_seconds": round(sum(row["end"] - row["start"] for row in unused_rows), 3),
    }


def _semantic_unit_issues(cast_chapter: Mapping[str, Any], selected_ids: Sequence[int]) -> list[str]:
    """Verify AI-declared cross-line dependencies; never infer or insert text."""
    units = cast_chapter.get("semantic_units", [])
    if not isinstance(units, list):
        return ["semantic_units_invalid"]
    issues = []
    sequence = list(selected_ids)
    for index, unit in enumerate(units, 1):
        if (not isinstance(unit, list) or len(unit) < 2
                or any(type(value) is not int or value <= 0 for value in unit)
                or len(set(unit)) != len(unit) or unit != sorted(unit)):
            issues.append(f"semantic_unit_{index}_invalid")
        elif not any(sequence[start:start + len(unit)] == unit
                     for start in range(len(sequence) - len(unit) + 1)):
            issues.append(f"semantic_unit_{index}_not_executed_together")
    return issues


def _semantic_unit_span_issues(
    cast_chapter: Mapping[str, Any], pool: Mapping[int, Mapping[str, Any]], *, max_seconds: float = 8.0,
) -> list[dict[str, Any]]:
    """Report an AI-declared complete unit that is too long to be a short beat.

    This is a measured quality receipt only.  It never splits, drops or
    substitutes the Director's selected words.
    """
    units = cast_chapter.get("semantic_units", [])
    if not isinstance(units, list):
        return []
    issues: list[dict[str, Any]] = []
    for index, unit in enumerate(units, 1):
        if (not isinstance(unit, list) or len(unit) < 2
                or any(type(value) is not int or value not in pool for value in unit)):
            continue
        try:
            source_span = float(pool[unit[-1]]["end"]) - float(pool[unit[0]]["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if source_span > max_seconds:
            issues.append({
                "semantic_unit_index": index,
                "subtitle_ids": list(unit),
                "source_span_seconds": round(source_span, 3),
            })
    return issues


def _story_evidence_issues(story: Mapping[str, Any], available_ids: Sequence[int]) -> list[str]:
    """Check location receipts against the exact M1 safe pool, not the raw SRT."""
    available = set(available_ids)
    issues = []
    for strategy in story.get("strategies") or []:
        for chapter in strategy.get("chapter_packets") or []:
            if "evidence_locations" not in chapter:
                continue  # Legacy saved contracts predate location receipts.
            locations = chapter["evidence_locations"]
            label = f"{strategy.get('strategy_id')}/{chapter.get('chapter_id')}"
            if not isinstance(locations, list):
                issues.append(f"{label}:evidence_format_invalid")
                continue
            if any(type(value) is not int or value <= 0 for value in locations):
                issues.append(f"{label}:evidence_id_format_invalid")
            elif any(value not in available for value in locations):
                issues.append(f"{label}:evidence_outside_safe_pool")
    return issues


def _long_continuous_utterance_groups(
    selected_ids: Sequence[int], pool: Mapping[int, Mapping[str, Any]], *, max_seconds: float = 8.0,
) -> list[dict[str, Any]]:
    """Report long uninterrupted source runs; never split or rewrite a Beat."""
    groups: list[dict[str, Any]] = []
    current: list[int] = []

    def flush() -> None:
        if not current:
            return
        first, last = current[0], current[-1]
        try:
            span = float(pool[last]["end"]) - float(pool[first]["start"])
        except (KeyError, TypeError, ValueError):
            return
        if span > max_seconds:
            groups.append({
                "subtitle_ids": list(current),
                "source_span_seconds": round(span, 3),
            })

    previous_id: int | None = None
    previous_end: float | None = None
    for subtitle_id in selected_ids:
        try:
            current_id = int(subtitle_id)
        except (TypeError, ValueError):
            flush()
            current = []
            previous_id = previous_end = None
            continue
        row = pool.get(current_id)
        if row is None:
            flush()
            current = []
            previous_id = previous_end = None
            continue
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, TypeError, ValueError):
            flush()
            current = []
            previous_id = previous_end = None
            continue
        contiguous = (
            previous_id is not None
            and current_id == previous_id + 1
            and previous_end is not None
            and start - previous_end <= 1.0
        )
        if not contiguous:
            flush()
            current = []
        current.append(current_id)
        previous_id, previous_end = current_id, end
    flush()
    return groups


def _story_evidence_location_conflicts(
    strategy: Mapping[str, Any], *, strategy_id: str = "S1",
) -> list[dict[str, Any]]:
    """Expose shared M1 fact locations without assigning their final owner.

    Evidence locations remain search hints, never an M1-selected edit.  Their
    overlap is useful context for the Director's one Casting call: a source ID
    may appear in the final video only once, so two chapters cannot both lean
    on it as their concrete proof.
    """
    owners: dict[int, list[str]] = {}
    raw_chapters = strategy.get("chapter_packets") or ()
    if isinstance(raw_chapters, Mapping):
        raw_chapters = (raw_chapters,)
    for index, raw_chapter in enumerate(raw_chapters, 1):
        if not isinstance(raw_chapter, Mapping):
            continue
        chapter_id = str(raw_chapter.get("chapter_id") or f"C{index}").strip()
        locations = raw_chapter.get("evidence_locations") or ()
        if not isinstance(locations, (list, tuple)):
            continue
        seen_locations: set[int] = set()
        for value in locations:
            if type(value) is int and value > 0:
                if value in seen_locations:
                    continue
                seen_locations.add(value)
                owners.setdefault(value, []).append(chapter_id)
    return [
        {
            "strategy_id": strategy_id,
            "subtitle_ids": [subtitle_id],
            "chapter_ids": chapter_ids,
        }
        for subtitle_id, chapter_ids in sorted(owners.items())
        if len(chapter_ids) > 1
    ]


def _completion_receipt_audit(
    story_chapter: Mapping[str, Any],
    cast_chapter: Mapping[str, Any],
    selected_subtitle_ids: Sequence[int],
) -> dict[str, Any]:
    """Check that an AI-declared complete chapter has its own beat receipt.

    This intentionally does not decide whether a spoken sentence *means* a
    requirement.  The Director owns that semantic judgement.  The program only
    verifies that the Director explicitly links every promised requirement to
    a real, selected, executable sentence in that chapter.
    """
    requirements = [
        str(value).strip()
        for value in (story_chapter.get("completion_requirements") or [])
        if str(value).strip()
    ]
    declared = str(cast_chapter.get("completion_status") or "not_reported").strip().lower()
    selected = {int(value) for value in selected_subtitle_ids}
    raw_receipts = cast_chapter.get("completion_receipts") or []
    receipts = raw_receipts if isinstance(raw_receipts, list) else []
    valid_indices: set[int] = set()
    invalid_receipts: list[str] = []
    for item in receipts:
        if not isinstance(item, Mapping):
            invalid_receipts.append("receipt_not_object")
            continue
        try:
            requirement_index = int(item.get("requirement_index"))
        except (TypeError, ValueError):
            invalid_receipts.append("receipt_missing_requirement_index")
            continue
        values = item.get("subtitle_ids") or []
        if not isinstance(values, (list, tuple)):
            invalid_receipts.append(f"receipt_{requirement_index}_missing_ids")
            continue
        try:
            receipt_ids = {int(value) for value in values}
        except (TypeError, ValueError):
            invalid_receipts.append(f"receipt_{requirement_index}_invalid_ids")
            continue
        if requirement_index < 1 or requirement_index > len(requirements):
            invalid_receipts.append(f"receipt_{requirement_index}_unknown_requirement")
        elif not receipt_ids or not receipt_ids.issubset(selected):
            invalid_receipts.append(f"receipt_{requirement_index}_not_selected_in_chapter")
        else:
            valid_indices.add(requirement_index)

    issues: list[str] = []
    if declared == "complete" and requirements:
        missing = sorted(set(range(1, len(requirements) + 1)) - valid_indices)
        if missing:
            issues.append("completion_receipts_missing_requirements:" + ",".join(map(str, missing)))
    if declared in {"needs_context", "source_limited"} and not str(cast_chapter.get("missing_content") or "").strip():
        issues.append("completion_status_missing_content")
    issues.extend(invalid_receipts)
    issues.extend(_semantic_unit_issues(cast_chapter, selected_subtitle_ids))
    verified = not issues
    effective = declared
    if declared == "complete" and not verified:
        effective = "needs_review"
    elif declared not in {"complete", "needs_context", "source_limited"}:
        effective = "needs_review"
        issues.append("completion_status_not_reported")
    return {
        "declared_status": declared or "not_reported",
        "effective_status": effective,
        "requirements_count": len(requirements),
        "receipts": [dict(item) for item in receipts if isinstance(item, Mapping)],
        "verified": verified,
        "issues": issues,
    }


def build_director_duration_audit(
    *,
    casting_payload: Mapping[str, Any],
    story_contract: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None = None,
    target_duration: float = 60.0,
    duration_tolerance: float | None = None,
    output_speed_factor: float = 1.0,
    grouped_beat_issues: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Count real source IDs, never choose or rewrite semantic content."""
    from selection_contracts import DurationContract

    contract = DurationContract.create(target_duration, output_speed_factor, tolerance=duration_tolerance)
    measured = build_two_pass_draft_audit(
        initial_draft=casting_payload, subtitles=subtitles,
        executable_subtitle_ids=executable_subtitle_ids,
        target_duration=target_duration, duration_tolerance=duration_tolerance,
        output_speed_factor=output_speed_factor,
    )
    pool = {int(row["id"]): row for row in _director_casting_rows(subtitles, executable_subtitle_ids)}
    ids = measured["selected_subtitle_ids"]
    # Report the actual playable sum, but never accept duplicate references as
    # a valid correction. Keep unique usable time separately for diagnostics.
    source_seconds = measured["actual_seconds"]
    unique_seconds = round(sum(pool[i]["end"] - pool[i]["start"] for i in set(ids)), 3)
    playback_status = contract.status(source_seconds)
    status = contract.status(unique_seconds)
    story_chapters = _two_pass_primary(story_contract).get("chapter_packets") or []
    cast_chapters = _two_pass_primary(casting_payload).get("chapter_packets") or []
    if isinstance(story_chapters, Mapping):
        story_chapters = [story_chapters]
    if isinstance(cast_chapters, Mapping):
        cast_chapters = [cast_chapters]
    cast_by_id = {str(c.get("chapter_id")): c for c in cast_chapters if isinstance(c, Mapping)}
    chapter_rows = []
    planned_budget = 0.0
    credited_ids: set[int] = set()
    for chapter in story_chapters:
        if not isinstance(chapter, Mapping):
            continue
        cid = str(chapter.get("chapter_id"))
        cast = cast_by_id.get(cid, {})
        chapter_measure = build_two_pass_draft_audit(
            initial_draft={"primary": {"chapter_packets": [cast]}},
            subtitles=subtitles, executable_subtitle_ids=executable_subtitle_ids,
            target_duration=target_duration, duration_tolerance=duration_tolerance,
            output_speed_factor=output_speed_factor,
        )
        chapter_ids = chapter_measure["selected_subtitle_ids"]
        completion = _completion_receipt_audit(chapter, cast, chapter_ids)
        seconds = chapter_measure["actual_seconds"]
        long_groups = _long_continuous_utterance_groups(chapter_ids, pool)
        semantic_unit_span_issues = _semantic_unit_span_issues(cast, pool)
        new_ids = set(chapter_ids) - credited_ids
        new_seconds = round(sum(pool[i]["end"] - pool[i]["start"] for i in new_ids), 3)
        credited_ids.update(chapter_ids)
        try:
            budget = max(0.0, float(chapter.get("source_budget_seconds") or 0))
        except (TypeError, ValueError):
            budget = 0.0
        planned_budget += budget
        chapter_rows.append({
            "chapter_id": cid, "chapter_job": chapter.get("chapter_job", ""),
            "coverage": chapter.get("coverage", ""),
            "source_budget_seconds": chapter.get("source_budget_seconds"),
            "completion_requirements": chapter.get("completion_requirements", []),
            "source_seconds": seconds,
            "new_source_seconds": new_seconds,
            "budget_gap_seconds": round(max(0.0, budget - new_seconds), 3),
            "projected_final_seconds": round(seconds / contract.speed_factor, 3),
            "selected_subtitle_ids": chapter_ids,
            "readthrough": "｜".join(str(pool[i]["text"]) for i in chapter_ids),
            # This is AI's semantic assessment, not a programmatic classifier.
            "ai_completion_status": cast.get("completion_status", "not_reported"),
            "ai_missing_content": cast.get("missing_content", ""),
            "completion_status": completion["effective_status"],
            "completion_receipts": completion["receipts"],
            "completion_receipt_verified": completion["verified"],
            "completion_receipt_issues": completion["issues"],
            "semantic_units": cast.get("semantic_units") or [],
            "semantic_unit_span_issues": semantic_unit_span_issues,
            "long_continuous_utterance_groups": long_groups,
            "ai_budget_execution": cast.get("budget_execution") or {},
        })
    # 2026-09-23（David 定调）：规划了却一句都不交的章必须算未完成；
    # 每章厚度低于本章预算一半也算没做到，两者都要回喂校准轮。
    empty_chapters = [
        c["chapter_id"] for c in chapter_rows
        if not list(c.get("selected_subtitle_ids") or [])
    ]
    undersized_chapters = [
        c["chapter_id"] for c in chapter_rows
        if list(c.get("selected_subtitle_ids") or [])
        and float(c.get("source_budget_seconds") or 0.0) > 0
        and float(c.get("new_source_seconds") or 0.0) < float(c["source_budget_seconds"]) * 0.5
    ]
    incomplete = [
        c["chapter_id"] for c in chapter_rows
        if c["ai_completion_status"] in {"needs_context", "source_limited"}
    ]
    retryable_incomplete = [
        c["chapter_id"] for c in chapter_rows
        if c["ai_completion_status"] == "needs_context"
    ]
    unverified = [
        c["chapter_id"] for c in chapter_rows
        if c["completion_status"] == "needs_review"
    ]
    technical_issues = [w for w in measured["warnings"] if "start_id_must_equal_end_id" in w or "invalid_subtitle_id" in w]
    if grouped_beat_issues is None:
        grouped_beat_issues = _cast_beat_cardinality_issues(casting_payload)
    grouped_beat_issues = [dict(item) for item in grouped_beat_issues if isinstance(item, Mapping)]
    technical_valid = bool(ids) and not measured["invalid_subtitle_ids"] and not technical_issues and not grouped_beat_issues
    long_continuous_count = sum(
        len(chapter.get("long_continuous_utterance_groups") or [])
        for chapter in chapter_rows
    )
    semantic_unit_span_issue_count = sum(
        len(chapter.get("semantic_unit_span_issues") or [])
        for chapter in chapter_rows
    )
    # Quality warnings stay visible, but they no longer flip an in-range
    # delivery into "not met": duration is judged on unique source seconds.
    quality_flags: list[str] = []
    if not bool(ids):
        quality_flags.append("no_selected_subtitles")
    if long_continuous_count:
        quality_flags.append("long_continuous_utterance")
    if semantic_unit_span_issue_count:
        quality_flags.append("semantic_unit_span")
    if measured["duplicate_subtitle_ids"]:
        quality_flags.append("repeated_subtitle_ids")
    return {
        "duration_contract": director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor),
        "source_seconds": source_seconds,
        "unique_source_seconds": unique_seconds,
        "projected_final_seconds": round(playback_status["projected_final"], 3),
        "unique_projected_final_seconds": round(status["projected_final"], 3),
        "repeated_source_seconds": round(source_seconds - unique_seconds, 3),
        "duration_in_range": bool(status["accepted"]),
        "quality_flags": quality_flags,
        "target_range_fulfilled": bool(technical_valid and status["accepted"]),
        "shortfall_source_seconds": round(status["gap"], 3),
        "excess_source_seconds": round(status["excess"], 3),
        "selected_subtitle_ids": ids,
        "technical_valid": technical_valid,
        "invalid_subtitle_ids": measured["invalid_subtitle_ids"],
        "duplicate_subtitle_ids": measured["duplicate_subtitle_ids"],
        "technical_issues": technical_issues,
        "grouped_beat_issues": grouped_beat_issues,
        "chapters": chapter_rows,
        "incomplete_chapter_ids": incomplete,
        "retryable_incomplete_chapter_ids": retryable_incomplete,
        "empty_chapter_ids": empty_chapters,
        "undersized_chapter_ids": undersized_chapters,
        "unverified_completion_chapter_ids": unverified,
        # A source-limited chapter is an honest material boundary.  Asking a
        # third model call to invent its missing evidence spends money without
        # improving the editable result.
        "needs_calibration": bool(
            not status["accepted"] or retryable_incomplete or not technical_valid
            or measured["duplicate_subtitle_ids"] or long_continuous_count or semantic_unit_span_issue_count
            or empty_chapters or undersized_chapters
        ),
        "unused_pool_count": measured["unused_pool_count"],
        "unused_pool_seconds": measured["unused_pool_seconds"],
        "complete_pool_count": measured["complete_pool_count"],
        "complete_pool_seconds": measured["complete_pool_seconds"],
        "pool_upper_bound_final_seconds": round(measured["complete_pool_seconds"] / contract.speed_factor, 3),
        "pool_cannot_reach_minimum": measured["complete_pool_seconds"] / contract.speed_factor < contract.final_min - contract.acceptance_margin,
        "planned_chapter_budget_seconds": round(planned_budget, 3),
        "chapter_budget_vs_target_gap": round(contract.source_target - planned_budget, 3),
        "long_continuous_utterance_group_count": long_continuous_count,
        "semantic_unit_span_issue_count": semantic_unit_span_issue_count,
        "selected_mean_beat_seconds": round(unique_seconds / max(1, len(set(ids))), 3),
        "estimated_beat_count_at_current_pace": math.ceil(contract.source_target / max(1.0, unique_seconds / max(1, len(set(ids))))),
        "min_beats_for_source_min_at_current_pace": math.ceil(contract.source_min / max(1.0, unique_seconds / max(1, len(set(ids))))),
        "pool_note": "未选库存仅为数量上限，不等于适合当前故事；语义价值由 AI 判断。",
    }


def _main_product_pool_rows(
    story_contract: Mapping[str, Any], subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None,
) -> tuple[list[Mapping[str, Any]], bool]:
    """Return the casting rows that belong to the frozen main product.

    This never casts a sentence. It prevents other-product seconds from being
    presented as available inventory for the frozen main story.

    2026-10-09（方案 A，与 server.py 同口径）：**不再扣减 foreign_product_ranges**。
    那个规则取「第一个锚点 → 最后一个锚点」的整段跨度
    （director_product_contract.foreign_product_ranges：锚点间隔 ≤20 行并成一簇，
    end_id = cluster[-1]），会整段吞掉中间的主商品内容。实测一次 35 分钟混剪：
    它把 388 行里的 302 行判成外来商品，其中 20 行 / 72.9s 是 AI 明确判为主商品的；
    主商品池因此从 229.5s 被压到 156.6s。判定权归 AI 的 source_product_sections。
    规则层的意见仍然计算、只作旁证，不再参与筛选。
    """
    scope = dict(_two_pass_primary(story_contract).get("product_scope") or {})
    main_type = str(scope.get("product_type") or "unknown")
    sections = [dict(s) for s in scope.get("source_product_sections") or [] if isinstance(s, Mapping)]
    allowed: set[int] = set()
    for section in sections:
        section_type = str(section.get("product_type") or "unknown")
        if not compatible(main_type, section_type):
            continue
        try:
            allowed.update(range(int(section["start_id"]), int(section["end_id"]) + 1))
        except (KeyError, TypeError, ValueError):
            continue
    rows = _director_casting_rows(subtitles, executable_subtitle_ids)
    # 规则层意见保留为旁证（仅供诊断/追溯），不参与主商品池的构成。
    main_rows = [row for row in rows if int(row["id"]) in allowed]
    return main_rows, bool(sections)


def _attach_main_product_pool_audit(
    audit: dict[str, Any], *, story_contract: Mapping[str, Any], subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None, output_speed_factor: float,
) -> None:
    """Narrow numeric capacity to AI-declared source product sections.

    This never casts a sentence. It prevents other-product seconds from being
    presented as available inventory for the frozen main story.
    """
    main_rows, has_sections = _main_product_pool_rows(story_contract, subtitles, executable_subtitle_ids)
    seconds = round(sum(float(row["end"]) - float(row["start"]) for row in main_rows), 3)
    selected_unique = {int(value) for value in (audit.get("selected_subtitle_ids") or ())}
    selected_main_seconds = round(sum(
        float(row["end"]) - float(row["start"])
        for row in main_rows if int(row["id"]) in selected_unique
    ), 3)
    speed = max(0.1, float(output_speed_factor or 1.0))
    audit["main_product_pool_count"] = len(main_rows)
    audit["main_product_pool_seconds"] = seconds
    audit["main_product_pool_upper_bound_final_seconds"] = round(seconds / speed, 3)
    source_min = float(dict(audit.get("duration_contract") or {}).get("source_min") or 0)
    audit["main_product_selected_seconds"] = selected_main_seconds
    audit["main_product_unused_seconds"] = round(max(0.0, seconds - selected_main_seconds), 3)
    audit["main_product_shortfall_seconds"] = round(max(0.0, source_min - selected_main_seconds), 3)
    audit["main_product_pool_cannot_reach_minimum"] = bool(has_sections and seconds < source_min)


def _main_product_pool_gate(audit: Mapping[str, Any]) -> dict[str, Any]:
    """Program-side verdict on whether the main-product pool is the real limit."""
    limited = bool(audit.get("main_product_pool_cannot_reach_minimum"))
    return {
        "pool_seconds": float(audit.get("main_product_pool_seconds") or 0.0),
        "selected_seconds": float(audit.get("main_product_selected_seconds") or 0.0),
        "unused_seconds": float(audit.get("main_product_unused_seconds") or 0.0),
        "shortfall_seconds": float(audit.get("main_product_shortfall_seconds") or 0.0),
        "main_product_pool_limited": limited,
        "verdict": "report_material_limited" if limited else "fill_from_main_product_pool",
    }


def _duration_calibration_structure_errors(
    story: Mapping[str, Any], original: Mapping[str, Any], revised: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> list[str]:
    """A correction must remain an executable revision of the same story."""
    errors = [] if audit["technical_valid"] else ["invalid_source_references"]
    story_chapters = _two_pass_primary(story).get("chapter_packets") or []
    revised_chapters = _two_pass_primary(revised).get("chapter_packets") or []
    if isinstance(story_chapters, Mapping):
        story_chapters = [story_chapters]
    if isinstance(revised_chapters, Mapping):
        revised_chapters = [revised_chapters]
    frozen_ids = [str(c.get("chapter_id")) for c in story_chapters if isinstance(c, Mapping)]
    revised_ids = [str(c.get("chapter_id")) for c in revised_chapters if isinstance(c, Mapping)]
    if not revised_ids or revised_ids != [cid for cid in frozen_ids if cid in revised_ids]:
        errors.append("changed_frozen_chapter_order")
    original_primary = _two_pass_primary(original)
    opening = (original_primary.get("opening_selection") or {}).get("selected_subtitle_ids") or []
    beats = _two_pass_beat_rows(original_primary)
    # P0-C 开场基准（2026-09-30）：程序补进来的行（拆窗补齐 / 整窗整取 /
    # 首尾收口）不属于 AI 声明的开场。比较「开场有没有被改掉」时必须先把它们
    # 剔掉，否则程序补行反过来会把 AI 的合规修订判成 changed_existing_opening
    # —— 实测 14:07 跑批就是这个原因让明显更好的修订版被整份丢弃。
    authored_beats = [
        beat for beat in beats
        if not (beat.get("window_split_repair") or beat.get("window_whole_keep")
                or beat.get("chapter_edge_guard"))
    ]
    actual_ids = [i for beat in authored_beats for i in (beat.get("subtitle_ids") or [])]
    # An advisory opening receipt sometimes disagrees with executed beats.
    # Preserve the actual audible opening, not IDs that were never selected.
    if not opening or actual_ids[:len(opening)] != list(opening):
        opening = list(authored_beats[0].get("subtitle_ids") or []) if authored_beats else []
    if opening and list(audit["selected_subtitle_ids"][:len(opening)]) != list(opening):
        errors.append("changed_existing_opening")
    return errors


def _revision_beat_ids(beat: Mapping[str, Any]) -> list[int]:
    """Read one Beat's referenced subtitle id the same way the audit does."""
    ids: list[int] = []
    span = beat.get("source_span") or beat.get("subtitle_span") or {}
    if isinstance(span, Mapping):
        start = span.get("start_id") or span.get("start_subtitle_id")
        try:
            start_int = int(start or 0)
        except (TypeError, ValueError):
            start_int = 0
        if start_int:
            ids.append(start_int)
    if not ids:
        raw = beat.get("subtitle_ids", beat.get("ids", ()))
        if isinstance(raw, (str, int)):
            raw = (raw,)
        for value in raw or ():
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
    return ids


def _sanitize_duration_revision(
    revised: Mapping[str, Any], *, drop_ids: Sequence[int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove only the offending sentence references from a duration revision.

    A revision that is one bad id away from a longer, compliant plan must not be
    thrown away whole.  This never adds, reorders or rewrites content: it drops
    the ids the program already rejected as unusable, and splits a multi-id Beat
    into single-id Beats (the wire format requires one id per Beat anyway).
    """
    dropped: set[int] = set()
    for value in drop_ids or ():
        try:
            dropped.add(int(value))
        except (TypeError, ValueError):
            continue
    work = copy.deepcopy(dict(revised))
    tally: dict[str, Any] = {"dropped_ids": [], "removed_beats": 0, "split_beats": 0}
    targets: list[Any] = []
    if isinstance(work.get("strategies"), (list, tuple, Mapping)):
        targets.append(work)
    primary = work.get("primary")
    if isinstance(primary, Mapping) and primary is not work:
        targets.append(primary)
    if not targets:
        targets.append(work)
    for target in targets:
        for strategy in _strategy_refs(target):
            for chapter in _chapter_refs(strategy):
                if chapter.get("beats") is not None:
                    beat_key = "beats"
                elif chapter.get("director_beats") is not None:
                    beat_key = "director_beats"
                else:
                    beat_key = "beats"
                raw_beats = chapter.get(beat_key) or ()
                if isinstance(raw_beats, Mapping):
                    raw_beats = (raw_beats,)
                kept_beats: list[Any] = []
                for beat in raw_beats:
                    if not isinstance(beat, Mapping):
                        continue
                    raw_ids = _revision_beat_ids(beat)
                    if not raw_ids:
                        kept_beats.append(dict(beat))
                        continue
                    unique_ids: list[int] = []
                    for value in raw_ids:
                        if value not in unique_ids:
                            unique_ids.append(value)
                    keep = [value for value in unique_ids if value not in dropped]
                    for value in unique_ids:
                        if value in dropped and value not in tally["dropped_ids"]:
                            tally["dropped_ids"].append(value)
                    if not keep:
                        tally["removed_beats"] += 1
                        continue
                    if len(keep) > 1:
                        tally["split_beats"] += 1
                    for value in keep:
                        clone = dict(beat)
                        if "subtitle_ids" in clone:
                            clone["subtitle_ids"] = [value]
                        if "ids" in clone:
                            clone["ids"] = [value]
                        for span_key in ("source_span", "subtitle_span"):
                            span_value = clone.get(span_key)
                            if isinstance(span_value, Mapping):
                                repaired = dict(span_value)
                                touched = False
                                for key in ("start_id", "end_id", "start_subtitle_id", "end_subtitle_id"):
                                    if key in repaired:
                                        repaired[key] = value
                                        touched = True
                                if not touched:
                                    repaired["start_id"] = value
                                    repaired["end_id"] = value
                                clone[span_key] = repaired
                        kept_beats.append(clone)
                chapter[beat_key] = kept_beats
    return work, tally


def _compact_casting_revision_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep exact AI choices without resending its verbose prose."""
    strategies = payload.get("strategies") or ()
    if isinstance(strategies, Mapping):
        strategies = (strategies,)
    compact: list[dict[str, Any]] = []
    beat_keys = (
        "beat_function", "subtitle_ids", "product_relation", "subject_product",
        "subject_product_type", "product_evidence_ids", "supports_main_product",
        "set_component",
    )
    for index, strategy in enumerate(strategies, 1):
        if not isinstance(strategy, Mapping):
            continue
        chapters = strategy.get("chapter_packets") or ()
        if isinstance(chapters, Mapping):
            chapters = (chapters,)
        compact_chapters = []
        for chapter in chapters:
            if not isinstance(chapter, Mapping):
                continue
            compact_beats = []
            for beat in chapter.get("beats") or ():
                if not isinstance(beat, Mapping):
                    continue
                compact_beats.append({
                    key: beat.get(key) for key in beat_keys
                    if beat.get(key) not in (None, "", [], {})
                })
            compact_chapters.append({
                "chapter_id": chapter.get("chapter_id"),
                "completion_status": chapter.get("completion_status"),
                "continuity_status": chapter.get("continuity_status"),
                "beats": compact_beats,
            })
        opening = strategy.get("opening_selection") or {}
        compact.append({
            "strategy_id": strategy.get("strategy_id") or f"S{index}",
            "opening_subtitle_ids": list(opening.get("selected_subtitle_ids") or []),
            "chapter_packets": compact_chapters,
        })
    return {"strategies": compact}


def _dedupe_repeated_beats(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[int]]:
    """2026-09-23（David 定调）：重复句必须由程序删除，只保留首次出现。

    同一个字幕 ID 出现在后面的章节时，后面那次直接丢弃；这不改变任何语义
    选择，只是把「同一句重复播放」从交付物里去掉，避免重复句既占预算又
    让用户看到重复口播。返回 (新 payload, 被删除的 ID 列表)。
    """
    raw_strategies = payload.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        raw_strategies = (raw_strategies,)
    strategies = [item for item in raw_strategies]
    primary_index = 0
    for index, item in enumerate(strategies):
        if isinstance(item, Mapping) and str(item.get("director_plan_role") or "").lower() != "alternative":
            primary_index = index
            break
    if not strategies or not isinstance(strategies[primary_index], Mapping):
        return dict(payload), []
    primary = dict(strategies[primary_index])
    chapters = primary.get("chapter_packets") or ()
    if isinstance(chapters, Mapping):
        chapters = (chapters,)
    seen: set[int] = set()
    removed: list[int] = []
    new_chapters: list[Any] = []
    changed = False
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            new_chapters.append(chapter)
            continue
        beats = chapter.get("beats") or ()
        if isinstance(beats, Mapping):
            beats = (beats,)
        beat_list = list(beats)
        new_beats: list[Any] = []
        _first_beat_ids: list[int] = []
        for _beat_ordinal, beat in enumerate(beat_list):
            if not isinstance(beat, Mapping):
                new_beats.append(beat)
                continue
            raw_ids = beat.get("subtitle_ids") or ()
            if isinstance(raw_ids, (int, str)):
                raw_ids = (raw_ids,)
            ids: list[int] = []
            for value in raw_ids:
                try:
                    ids.append(int(value))
                except (TypeError, ValueError):
                    continue
            span = beat.get("source_span") or beat.get("subtitle_span") or {}
            if isinstance(span, Mapping):
                for key in ("start_id", "end_id", "start_subtitle_id", "end_subtitle_id"):
                    value = span.get(key)
                    if value is None:
                        continue
                    try:
                        candidate = int(value)
                    except (TypeError, ValueError):
                        continue
                    if candidate not in ids:
                        ids.append(candidate)
            if _beat_ordinal == 0 and not _first_beat_ids:
                _first_beat_ids = list(ids)
            fresh = [value for value in ids if value not in seen]
            if ids and not fresh:
                removed.extend(ids)
                changed = True
                continue
            if len(fresh) != len(ids):
                removed.extend([value for value in ids if value not in fresh])
                beat = dict(beat)
                beat["subtitle_ids"] = fresh
                if isinstance(span, Mapping):
                    span = dict(span)
                    for key in ("start_id", "start_subtitle_id"):
                        if key in span:
                            span[key] = fresh[0]
                    for key in ("end_id", "end_subtitle_id"):
                        if key in span:
                            span[key] = fresh[-1]
                    for key in ("source_span", "subtitle_span"):
                        if key in beat:
                            beat[key] = span
                changed = True
            seen.update(fresh)
            new_beats.append(beat)
        if not new_beats and beat_list and CAST_DEDUP_CHAPTER_FLOOR_ENABLED:
            # [止血D 2026-10-06] 保底：整章被判重复时留下第一拍，绝不留 0 句空壳章。
            # 该拍会作为重复句被 duplicate_subtitle_ids 如实记进审计，不静默。
            new_beats = [beat_list[0]]
            for _floor_id in _first_beat_ids:
                while _floor_id in removed:
                    removed.remove(_floor_id)
        if len(new_beats) != len(beat_list):
            chapter = {**dict(chapter), "beats": new_beats}
            changed = True
        new_chapters.append(chapter)
    if not changed:
        return dict(payload), []
    primary["chapter_packets"] = new_chapters
    strategies[primary_index] = primary
    new_payload = dict(payload)
    new_payload["strategies"] = strategies
    return new_payload, removed


def _compact_duration_calibration_feedback(audit: Mapping[str, Any]) -> dict[str, Any]:
    """Expose deterministic duration gaps without duplicating the full audit."""
    chapters = []
    for chapter in audit.get("chapters") or ():
        if not isinstance(chapter, Mapping):
            continue
        chapters.append({
            "chapter_id": chapter.get("chapter_id"),
            "source_budget_seconds": chapter.get("source_budget_seconds"),
            "source_seconds": chapter.get("source_seconds"),
            "new_source_seconds": chapter.get("new_source_seconds"),
            "budget_gap_seconds": chapter.get("budget_gap_seconds"),
            "selected_subtitle_ids": list(chapter.get("selected_subtitle_ids") or []),
            "ai_completion_status": chapter.get("ai_completion_status"),
            "completion_status": chapter.get("completion_status"),
            "completion_receipt_verified": chapter.get("completion_receipt_verified"),
        })
    mean_beat = float(audit.get("selected_mean_beat_seconds") or 0.0)
    for chapter in chapters:
        gap = float(chapter.get("budget_gap_seconds") or 0.0)
        chapter["gap_beats"] = math.ceil(gap / mean_beat) if mean_beat > 0 and gap > 0 else 0
    keys = (
        "source_seconds", "unique_source_seconds", "projected_final_seconds",
        "unique_projected_final_seconds", "target_range_fulfilled",
        "shortfall_source_seconds", "excess_source_seconds",
        "incomplete_chapter_ids", "unverified_completion_chapter_ids", "invalid_subtitle_ids", "duplicate_subtitle_ids",
        "empty_chapter_ids", "undersized_chapter_ids",
        "technical_issues", "planned_chapter_budget_seconds",
        "estimated_beat_count_at_current_pace", "unused_pool_count",
        "unused_pool_seconds", "main_product_pool_count", "main_product_pool_seconds",
        "main_product_pool_cannot_reach_minimum", "main_product_selected_seconds",
        "main_product_unused_seconds", "main_product_shortfall_seconds",
    )
    return {
        "duration_contract": dict(audit.get("duration_contract") or {}),
        **{key: audit.get(key) for key in keys if key in audit},
        "chapters": chapters,
    }


# 2026-10-05（David 定调）：删句前先看「这一章的章名还有没有依据」。
# 实测：末章「全弹力无痕腰穿着不难受」唯一的两句腰内容被判「归属不明 / 邻近讲其他商品」
# 删掉后，章名留在界面上、正文只剩面料弹力 —— 标题与内容彻底脱节。
# 这里只做一件事：若某个章名的唯一字面依据就藏在待删句里（剩下的句子一条都不含），
# 就把这些句子从待删集合里退回来（退回不删）并留痕。不改写、不重排、不动时间戳、
# 不新增 AI 调用；置 False 与落地前逐字节等价。
CHAPTER_TITLE_EVIDENCE_GUARD = True


def _guard_chapters(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """兼容两种入参形态：落盘文档（strategies / packet）或单个 strategy 章包。"""

    if payload.get("chapter_packets") or payload.get("chapters"):
        return _chapter_refs(dict(payload))
    return _chapter_refs(_two_pass_primary(payload))


def _product_conflict_needs_attention(audit: Mapping[str, Any]) -> bool:
    """举证方向（2026-10-05 David 定调）：只有硬证据（明确讲另一件商品）才需要动作。

    「缺少依据 / 归属不明 / 不唯一」这类软疑点只进告警，不再要求 AI 重选、
    不再算作结构错误、不再阻断修正稿采纳。
    """

    from director_product_contract import HARD_EVIDENCE_ONLY  # 随运行时开关生效

    if not HARD_EVIDENCE_ONLY:
        return audit.get("status") == "conflict"
    return bool(audit.get("conflicting_subtitle_ids"))


def _product_conflict_clear(audit: Mapping[str, Any]) -> bool:
    """硬证据口径：只剩软疑点时视为可通过（举证不足不阻断采纳）。"""

    from director_product_contract import HARD_EVIDENCE_ONLY

    if not HARD_EVIDENCE_ONLY:
        return audit.get("status") != "conflict"
    return not (audit.get("conflicting_subtitle_ids") or audit.get("scope_errors"))
_CHAPTER_TITLE_STOP_CHARS = set("的了是我你他她它这那就都也还很非常而和与个把被给在们啊吧呀呢哦嘛儿")


def _chapter_title_grams(title: Any) -> set[str]:
    chars = [
        char for char in str(title or "")
        if "\u4e00" <= char <= "\u9fff" and char not in _CHAPTER_TITLE_STOP_CHARS
    ]
    if len(chars) >= 2:
        return {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}
    return set(chars)


def _sentence_grams(text: Any) -> set[str]:
    chars = [char for char in str(text or "") if "\u4e00" <= char <= "\u9fff"]
    return {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}


def _chapter_title_evidence_guard(
    cast_payload: Mapping[str, Any], story_payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]], drop_ids: set[int],
) -> dict[str, Any]:
    """商品归属删句前，保护「章名的唯一字面依据」（2026-10-05 David 定调）。

    只判确定性字面重合：章名去停用字后的 2 字串，若只出现在待删句里、章内剩下任何一句
    都不含，则这些待删句退回不删。绝不改写文本、不重排、不动时间戳、不新增 AI 调用。
    """

    outcome: dict[str, Any] = {
        "drop_ids": {int(value) for value in (drop_ids or ())},
        "protected_ids": [],
        "warnings": [],
    }
    if not CHAPTER_TITLE_EVIDENCE_GUARD or not outcome["drop_ids"]:
        return outcome
    text_by_id = {
        int(row["id"]): str(row.get("text") or "")
        for row in subtitles or ()
        if str(row.get("id", "")).strip().isdigit()
    }
    titles: dict[str, str] = {}
    for chapter in _guard_chapters(story_payload):
        chapter_id = str(chapter.get("chapter_id") or "").strip()
        title = str(chapter.get("title") or chapter.get("chapter_title") or "").strip()
        if chapter_id and title:
            titles[chapter_id] = title
    # 界面/下游用的是 casting 章包里的 chapter_revision.title（观众真正看到的章名），
    # 它优先于 M1 故事层章名；否则保护的是旧章名，等于没保护。
    for chapter in _guard_chapters(cast_payload):
        chapter_id = str(chapter.get("chapter_id") or "").strip()
        revision = chapter.get("chapter_revision")
        title = str(revision.get("title") or "").strip() if isinstance(revision, Mapping) else ""
        if not title:
            title = str(chapter.get("title") or chapter.get("chapter_title") or "").strip()
        if chapter_id and title:
            titles[chapter_id] = title
    if not titles:
        return outcome
    protected: set[int] = set()
    for chapter in _guard_chapters(cast_payload):
        chapter_id = str(chapter.get("chapter_id") or "").strip()
        grams = _chapter_title_grams(titles.get(chapter_id, ""))
        if not grams:
            continue
        raw_beats = chapter.get("beats")
        if raw_beats is None:
            raw_beats = chapter.get("director_beats") or ()
        chapter_ids: list[int] = []

        def _collect(container: Mapping[str, Any]) -> None:
            for value in container.get("subtitle_ids") or container.get("ids") or ():
                try:
                    chapter_ids.append(int(value))
                except (TypeError, ValueError):
                    continue
            for span in container.get("semantic_units") or ():
                if not isinstance(span, (list, tuple)) or len(span) != 2:
                    continue
                try:
                    low, high = int(span[0]), int(span[1])
                except (TypeError, ValueError):
                    continue
                chapter_ids.extend(
                    sid for sid in range(min(low, high), max(low, high) + 1)
                    if sid in text_by_id
                )

        _collect(chapter)
        for beat in raw_beats or ():
            if isinstance(beat, Mapping):
                _collect(beat)
        dropping = [sid for sid in dict.fromkeys(chapter_ids) if sid in outcome["drop_ids"]]
        if not dropping:
            continue
        remaining = [sid for sid in chapter_ids if sid not in outcome["drop_ids"]]
        kept_grams: set[str] = set()
        for sid in remaining:
            kept_grams |= _sentence_grams(text_by_id.get(sid, ""))
        guards = [
            sid for sid in dropping
            if (_sentence_grams(text_by_id.get(sid, "")) & grams) - kept_grams
        ]
        if guards:
            protected.update(guards)
            outcome["warnings"].append(
                "product_conflict_kept_title_evidence:%s:%s"
                % (chapter_id, ",".join(str(value) for value in guards))
            )
    if protected:
        outcome["drop_ids"] -= protected
        outcome["protected_ids"] = sorted(protected)
    return outcome


def _compact_product_calibration_feedback(audit: Mapping[str, Any]) -> dict[str, Any]:
    """Send only actionable product conflicts to the optional correction."""
    return {
        "target": dict(audit.get("target") or {}),
        "resolved_scope": dict(audit.get("resolved_scope") or {}),
        "scope_errors": list(audit.get("scope_errors") or []),
        "conflicting_subtitle_ids": list(audit.get("conflicting_subtitle_ids") or []),
        "alternative_conflicting_subtitle_ids": list(audit.get("alternative_conflicting_subtitle_ids") or []),
        "status": audit.get("status"),
    }


# 2026-10-06（David 定调）：时长校准验收闸。
# 第三遍 AI（时长校准）此前只要 needs_calibration 为真就发，回来又无条件采纳——
# 实测出现过「首轮 61.68 秒本来达标 → 校准把它改成 95.93 秒」的路径。
# 本闸只做两件确定性测量，不改 Prompt、不新增给 AI 的字段：
#   入口：实测已落入可编辑区间时，不发第三遍 AI（章过薄等质量项只告警）。
#   出口：修订版不得让「超出 source_max」比原稿更糟，否则整版拒收、保留上一版。
# 开关置 False 时逐字节等价于旧行为。
CALIBRATION_VERIFY_ENABLED = True


def _calibration_entry_blocked(audit: Mapping[str, Any]) -> bool:
    """True 表示验收闸拦下本次校准调用。

    口径（2026-10-06，David 定调）：程序实测时长已经落在可编辑区间内时，
    只有「章节没交 / 整章为空」这种结构漏洞还值得再叫一次 AI；
    「章过薄、连续长口播、语义跨度、重复句」这些质量问题一律只保留告警，
    不再单独买一次第三遍调用。时长偏短或偏长（未落区间）照旧允许校准。
    """
    if not CALIBRATION_VERIFY_ENABLED:
        return False
    if not audit.get("target_range_fulfilled"):
        return False
    if not audit.get("technical_valid", True):
        return False
    if audit.get("retryable_incomplete_chapter_ids") or audit.get("empty_chapter_ids"):
        return False
    return True


def _calibration_revision_exceeds_max(
    initial: Mapping[str, Any],
    revised: Mapping[str, Any],
) -> dict[str, Any] | None:
    """修订版比原稿更超上限时返回证据；否则 None（允许采纳）。

    只比较「超出上限的部分」：原稿就超时，只要修订把超量改轻，仍算改进。
    """
    if not CALIBRATION_VERIFY_ENABLED:
        return None
    contract = dict(revised.get("duration_contract") or initial.get("duration_contract") or {})
    try:
        source_max = float(contract.get("source_max") or 0.0)
    except (TypeError, ValueError):
        source_max = 0.0
    if source_max <= 0.0:
        return None
    try:
        revised_seconds = float(revised.get("source_seconds") or 0.0)
        initial_seconds = float(initial.get("source_seconds") or 0.0)
    except (TypeError, ValueError):
        return None
    revised_excess = max(0.0, revised_seconds - source_max)
    initial_excess = max(0.0, initial_seconds - source_max)
    if revised_excess > initial_excess + 0.01:
        return {
            "revised_source_seconds": round(revised_seconds, 3),
            "initial_source_seconds": round(initial_seconds, 3),
            "source_max": round(source_max, 3),
            "excess_seconds": round(revised_excess, 3),
        }
    return None


def _duration_revision_improves(initial: Mapping[str, Any], revised: Mapping[str, Any]) -> bool:
    """Never replace an editable draft with a revision farther from target."""
    if revised.get("target_range_fulfilled"):
        return True
    contract = dict(initial.get("duration_contract") or {})
    target = float(contract.get("source_target") or 0.0)
    initial_seconds = float(initial.get("unique_source_seconds") or 0.0)
    revised_seconds = float(revised.get("unique_source_seconds") or 0.0)
    initial_distance = abs(initial_seconds - target)
    revised_distance = abs(revised_seconds - target)
    if revised_distance < initial_distance - 0.01:
        return True

    def defects(value: Mapping[str, Any]) -> int:
        return (
            len(value.get("incomplete_chapter_ids") or [])
            + len(value.get("unverified_completion_chapter_ids") or [])
            + len(value.get("invalid_subtitle_ids") or [])
            + len(value.get("duplicate_subtitle_ids") or [])
        )

    return defects(revised) < defects(initial) and revised_distance <= initial_distance + 0.75


def _primary_strategy_ref(payload: dict[str, Any]) -> dict[str, Any]:
    raw_strategies = payload.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        raw_strategies = [dict(raw_strategies)]
        payload["strategies"] = raw_strategies
    if isinstance(raw_strategies, list):
        for index, item in enumerate(raw_strategies):
            if isinstance(item, Mapping) and not isinstance(item, dict):
                raw_strategies[index] = dict(item)
        for item in raw_strategies:
            if not isinstance(item, dict):
                continue
            role = str(item.get("director_plan_role") or item.get("plan_role") or "primary").lower()
            if role == "primary":
                return item
        for item in raw_strategies:
            if isinstance(item, dict):
                return item
    raw_primary = payload.get("primary")
    if isinstance(raw_primary, Mapping):
        primary = dict(raw_primary)
        payload["primary"] = primary
        return primary
    return {}


def _strategy_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_strategies = payload.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        strategies = [dict(raw_strategies)]
    elif isinstance(raw_strategies, list):
        strategies = [
            item if isinstance(item, dict) else dict(item)
            for item in raw_strategies
            if isinstance(item, Mapping)
        ]
    else:
        strategies = [dict(item) for item in raw_strategies if isinstance(item, Mapping)]
    payload["strategies"] = strategies
    return strategies


def _chapter_refs(strategy: dict[str, Any]) -> list[dict[str, Any]]:
    raw_chapters = strategy.get("chapter_packets") or strategy.get("chapters") or ()
    if isinstance(raw_chapters, Mapping):
        chapters = [dict(raw_chapters)]
    elif isinstance(raw_chapters, list):
        chapters = [
            item if isinstance(item, dict) else dict(item)
            for item in raw_chapters
            if isinstance(item, Mapping)
        ]
    else:
        chapters = [dict(item) for item in raw_chapters if isinstance(item, Mapping)]
    strategy["chapter_packets"] = chapters
    return chapters


def _empty_duration_fill_control() -> dict[str, Any]:
    return {
        "source": "none_final_sequence_only",
        "attempted": False,
        "applied": False,
        "added_subtitle_ids": [],
        "added_source_seconds": 0.0,
    }


def _duration_control_record(
    *,
    initial_audit: Mapping[str, Any],
    final_audit: Mapping[str, Any],
    duration_fill_control: Mapping[str, Any],
    calibration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    calibration_row = dict(calibration or {
        "attempted": False,
        "accepted_revision": False,
        "max_attempts": 0,
    })
    return {
        "version": "director-duration-v3",
        "initial": dict(initial_audit),
        "final": dict(final_audit),
        "duration_fill": dict(duration_fill_control),
        "calibration": calibration_row,
        "semantic_call_count": 3 if calibration_row.get("attempted") else 2,
        "status": (
            "target_range_fulfilled"
            if final_audit.get("target_range_fulfilled")
            else "target_not_met_editable"
        ),
    }


def _audit_second_pass_duration_for_strategies(
    *,
    casting_payload: Mapping[str, Any],
    story_payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None = None,
    target_duration: float = 60.0,
    duration_tolerance: float | None = None,
    output_speed_factor: float = 1.0,
    grouped_beat_issues_by_strategy: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    """Measure every final AI sequence without appending or selecting beats."""

    payload = copy.deepcopy(dict(casting_payload))
    cast_strategies = _strategy_refs(payload)
    raw_story_rows = story_payload.get("strategies") or ()
    if isinstance(raw_story_rows, Mapping):
        raw_story_rows = (raw_story_rows,)
    story_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_story_rows, 1):
        if not isinstance(item, Mapping):
            continue
        story_by_id[str(item.get("strategy_id") or f"S{index}")] = dict(item)

    controls: dict[str, dict[str, Any]] = {}
    primary_strategy_id = ""
    for index, cast_strategy in enumerate(list(cast_strategies), 1):
        strategy_id = str(cast_strategy.get("strategy_id") or f"S{index}").strip() or f"S{index}"
        cast_strategy["strategy_id"] = strategy_id
        role = str(cast_strategy.get("director_plan_role") or cast_strategy.get("plan_role") or "").lower()
        if not primary_strategy_id and (role == "primary" or index == 1):
            primary_strategy_id = strategy_id
        story_strategy = story_by_id.get(strategy_id)
        if not story_strategy and index == 1 and story_by_id:
            story_strategy = next(iter(story_by_id.values()))
        if not story_strategy:
            continue
        single_story = {"strategies": [story_strategy]}
        single_cast = {"strategies": [cast_strategy]}
        audit_args = {
            "story_contract": single_story,
            "subtitles": subtitles,
            "executable_subtitle_ids": executable_subtitle_ids,
            "target_duration": target_duration,
            "duration_tolerance": duration_tolerance,
            "output_speed_factor": output_speed_factor,
            "grouped_beat_issues": list((grouped_beat_issues_by_strategy or {}).get(strategy_id) or ()),
        }
        initial_audit = build_director_duration_audit(casting_payload=single_cast, **audit_args)
        final_audit = initial_audit
        fill_control = _empty_duration_fill_control()
        fill_control["skipped_reason"] = "final_sequence_only"
        control = _duration_control_record(
            initial_audit=initial_audit,
            final_audit=final_audit,
            duration_fill_control=fill_control,
        )
        cast_strategy.setdefault("whole_video_audit", {})["duration_control"] = control
        controls[strategy_id] = control
    if not primary_strategy_id and cast_strategies:
        primary_strategy_id = str(cast_strategies[0].get("strategy_id") or "S1")
    return payload, controls, primary_strategy_id or "S1"


def _opening_hook_pool_prompt(
    opening_hook_pool: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int | None = None,
) -> str:
    """Render the independent Hook pool section for the story call.

    ``None`` means the independent recall was not run, so the prompt stays
    byte-identical to the historical behaviour.  An empty sequence means the
    recall ran and found nothing usable, which the director must declare
    instead of promoting a body sentence to a full-strength hook.
    """
    if opening_hook_pool is None:
        return ""
    rows = [
        dict(item) for item in opening_hook_pool if isinstance(item, Mapping)
    ]
    if limit is not None:
        rows = rows[:max(1, int(limit))]
    if not rows:
        return "\n".join((
            "【独立开场候选池：本次已单独全文扫描，结果为空】",
            "本次没有找到通过硬校验（干净、可独立成立、2-8秒、词级可回放、与主商品相关）的独立开场句。",
            "此时必须把 opening_promise 明确写成受限（例如：受限-素材无合格独立开场），"
            "并可改按主商品最强结果或机制安排开场；不得用正文演示句、半句承接或泛情绪句冒充满分 Hook。",
            "C1 的 evidence_locations 仍必须来自真实安全字幕，并在紧接的真实口播中兑现。",
        ))
    return "\n".join((
        "【独立开场候选池（已单独全文扫描，不属于正文池）】",
        "下面每一句都通过程序硬校验：干净、可独立成立、2-8秒、词级可回放、与已核实主商品相关。",
        "选择规则：**只看停人力——这条单独拎出来、没有前文、没有画面，能不能让陌生观众立刻停下**；",
        "都算停人开场时，再挑与本案后续章节关联最紧、且能被紧随其后的真实章节最直接兑现的那条。停人力是首要门槛，章节关联是比较时的第二门槛。",
        "操作演示（解扣子/扣上扣子/第几步怎么穿）、半句铺垫、依赖上文才能听懂的承接句，一律不算停人开场，只能进正文；开场必须自带结果或强判断，1-3 秒就能立住。",
        "也就是说：不要因为某条更好接章节，就放弃明显更能停住人的候选；也不要在几条停人力相当时只挑最强的，",
        "而要看它提出的承诺/问题能不能被后面的真实章节最直接地兑现、解释或推进。",
        "开场与后续章节是一体的：先让观众停下，再用后面的原话立刻给出答案。",
        "请在 opening_promise 里显式写出这个关联：这条开场承诺了什么，由后面哪一章（哪个证据 ID）兑现。",
        "若你选的开场后续没有直接兑现章节，也要说明它由哪一章承接，而不是改选更弱的候选。",
        "只允许使用池内给出的 subtitle_ids；不得改写、拼接或另取正文句充当开场。",
        "若池内只有操作演示句、没有任何停人型开场，把 opening_promise 写成受限（受限-素材无合格独立开场），不要拿演示句充当 Hook；若池内没有任何候选能与本案后续章节形成兑现关系，同样写受限。",
        "候选（hook_id / subtitle_ids / 起止秒 / 文本 / 类型 / 停人理由 / 强度参考）：",
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
    ))


def _opening_promise_hint(
    director_focus: Mapping[str, Any] | None,
    director_controls: Mapping[str, Any] | None,
) -> str:
    """Pick the best available opening promise to freeze for the Hook recall."""
    for source in (director_focus, director_controls):
        if not isinstance(source, Mapping):
            continue
        for key in ("opening_promise", "central_promise", "core_desire", "director_title"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def build_two_pass_story_prompt(
    *,
    product: str,
    subtitles: Sequence[Mapping[str, Any]],
    content_contract: Mapping[str, Any] | None = None,
    executable_subtitle_ids: Sequence[int] | None = None,
    target_duration: float = 45.0,
    duration_tolerance: float | None = None,
    director_focus: Mapping[str, Any] | None = None,
    director_controls: Mapping[str, Any] | None = None,
    output_speed_factor: float = 1.0,
    source_context_subtitles: Sequence[Mapping[str, Any]] | None = None,
    director_plan_count: int = 1,
    opening_hook_pool: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Build the story-only call over the complete executable transcript."""
    rows = _director_casting_rows(subtitles, executable_subtitle_ids)
    transcript = _director_product_context(
        source_context_subtitles or subtitles, rows, include_duration=False,
    )
    duration_range = director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor)
    depth_contract = director_duration_depth_contract(target_duration)
    pacing_reference = director_material_pacing(
        rows, duration_range,
        max_beats_per_chapter=int(depth_contract["beats_per_chapter"]["high"]),
    )
    _per_chapter_cap = max(1, int(depth_contract["beats_per_chapter"]["high"]))
    _depth_floor_beats = max(
        int(depth_contract["expected_total_beats"]["low"]),
        int(pacing_reference["approximate_beats_for_source_min_at_this_pace"] or 0),
    )
    _depth_floor_chapters = max(
        math.ceil(_depth_floor_beats / _per_chapter_cap),
        int(pacing_reference["minimum_chapters_for_source_min"] or 0),
    )
    story_depth_guidance = {
        "深度档位": depth_contract["mode"],
        "章节数上限": int(depth_contract["chapters_max"]),
        "本篇要点数量下限": _depth_floor_beats,
        "每章最多要点数": _per_chapter_cap,
        "建议推进章节数下限": _depth_floor_chapters,
        "各章类型深度参考": depth_contract["chapter_depth_targets"],
        "结束规则": depth_contract["stop_rule"],
    }
    subject_line = (
        "故事对象：当前选中商品；品类、风格和卖点只以字幕事实为准。"
        if not str(product or "").strip() else f"商品：{str(product).strip()}"
    )
    focus_line = (
        "【已锁定的用户选择，不重新选题】下面是用户从首次故事地图中亲自选定的备选方向。"
        "本轮只把这个方向补全为唯一主故事，不要重新换方向：不能改回原主方案，也不能另找一个更优方向。\n"
        "保留其中非空的 director_title、core_desire、central_promise、opening_promise、"
        "narrative_archetype 和 video_structure 所表达的购买主张；只可依据真实字幕补齐章节、证据定位和空字段。"
        "若素材不足以兑现该方向，明确 source_limited，不能静默替换成别的卖法。"
        "本轮内部输出仍使用 S1，表示该已选方向的完整执行版：\n"
        + json.dumps(dict(director_focus or {}), ensure_ascii=False)
        if director_focus else
        "请在完整素材中比较方向，只冻结证据最强、最值得发布的一条主故事。"
    )
    plan_count = max(1, min(3, int(director_plan_count or 1)))
    full_strategy_schema = {
            "strategy_id": "S1",
            "director_plan_role": "primary",
            "target_user": "这是谁：写到具体处境（买过什么、踩过什么坑、正卡在哪一步），不是人群标签",
            "audience_tension": "他现在相信什么、担心什么（观众张力，也是开场能停人的理由）",
            "story_trigger": "什么让他愿意继续看下去（触发他看下去的那个点）",
            "transformation": "看完之后他的想法变成什么：从〔原状态〕到〔新状态〕，收尾落在这里",
            "product_role": "商品在这个变化里扮演什么角色：证据、解决办法还是道具",
            "director_title": "主视频标题",
            "core_desire": "观众看完后形成的一句购买欲望",
            "central_promise": "整条视频只证明的一件事",
            "product_scope": {
                "main_product": "字幕核实的当前主商品",
                "product_type": "/".join(PRODUCT_TYPES),
                "target_confirmation": "match/ambiguous/not_found；match表示与用户目标及原字幕一致",
                "identity_evidence_ids": [1],
                "sales_scope": "single_product/explicit_set",
                "source_product_sections": [{"start_id": 1, "end_id": 20, "product_type": "tshirt",
                                             "subject_product": "该原片范围实际讲的商品；无法确认写unknown"}],
            },
            "opening_promise": "开头为什么能停人以及立即兑现什么",
            "opening_evidence_packages": [{
                "hook_subtitle_ids": [1],
                "payoff_subtitle_ids": [2],
                "purchase_value": "陌生观众立即听懂的具体购买价值",
                "payoff_basis": "紧接原话给出的结果、机制、证明或穿法",
            }],
            "narrative_archetype": "最合适的叙事原型",
            "video_structure": {
                "id": "结构 id",
                "name": "结构名称",
            },
            "chapter_packets": [{
                "chapter_id": "C1",
                "chapter_kind": "pain/result/mechanism/fit/comfort/risk/styling/scene/trust",
                "title": "章节名",
                "purchase_question_id": "参考标签：Q1-Q7；本题不适用可留空，不要为凑标签硬套",
                "buyer_advance": "观众从〔上一章的想法〕推进到〔这一章的新想法〕：写状态变化，不是写卖点",
                "coverage": "required/recommended/optional",
                "chapter_job": "这一章把观众从哪个状态推到哪个状态（承接上一章），简短写清",
                "source_budget_seconds": "数字：本章预计原声秒数，不是成片秒数",
                "completion_requirements": ["本章唯一、可由一组完整原话直接核验的购买判断；只写职责，不选原话"],
                "evidence_locations": [1, 3],
            }],
            "stop_condition": "哪些章节完成后故事即可自然结束",
        }
    if plan_count > 1:
        strategy_schemas = []
        for index in range(1, plan_count + 1):
            item = dict(full_strategy_schema)
            item["strategy_id"] = f"S{index}"
            item["director_plan_role"] = "primary" if index == 1 else "alternative"
            item["director_title"] = "主视频标题" if index == 1 else f"差异化方案 {index} 标题"
            strategy_schemas.append(item)
    else:
        # A one-version preview still needs two visible, selectable directions.
        # They deliberately stop at the story card: M2 is only invoked after a
        # user selects one, so these titles do not pay for another full cut.
        strategy_schemas = [full_strategy_schema]
        for index in (2, 3):
            strategy_schemas.append({
                "strategy_id": f"S{index}",
                "director_plan_role": "alternative",
                "director_title": f"备选方向 {index - 1} 标题",
                "core_desire": "不同的核心购买欲望",
                "opening_promise": "不同的开场承诺",
                "narrative_archetype": "叙事原型",
                "chapter_packets": [],
            })
    schema = {"strategies": strategy_schemas}
    _hook_pool_section = _opening_hook_pool_prompt(opening_hook_pool)
    # One chronological transcript distinguishes playable evidence from
    # context. Removing excluded rows would also erase product switches and
    # corrections, letting an adjacent safe pronoun inherit the wrong item.
    return "\n".join([
        "原始字幕按时间顺序（没有 Strong Ranking、没有 TopK）：[ID] 是可选片段，可选不代表句意完整；[context ID] 是不可选上下文，只用于核对商品切换、指代和否定事实，不能成为故事承诺、开场或正文。",
        transcript or "（没有满足 1-8 秒且可执行的字幕）",
        "",
        subject_line,
        "本轮先讲清楚观众，五问必须最先答完，章节只是这五问的展开：target_user 是谁（写到具体处境）；audience_tension 他现在担心或相信什么；story_trigger 什么让他愿意继续看下去；transformation 看完之后他的想法变成什么；product_role 商品在这个变化里是什么角色。五问没答完不许开章节；每章都要在观众从什么状态变到什么状态这条线上推进。",
        _story_content_boundary_prompt(content_contract),
        *([_hook_pool_section] if _hook_pool_section else []),
        _director_controls_prompt(director_controls, stage="story"),
        focus_line,
        (
            f"本次用户明确要求 {float(duration_range['requested_seconds']):.0f} 秒；"
            f"期望成片区间为 {float(duration_range['preferred_low']):.1f}-"
            f"{float(duration_range['preferred_high']):.1f} 秒。原声选句预算见下；不能拿半句或重复内容填充。"
        ),
        "交付时长合同（含导出变速，原声预算可以超过120秒）：" + json.dumps(duration_range, ensure_ascii=False),
        "本次章节深度预算（只决定需要讲多深，不允许重复注水）：" + json.dumps(story_depth_guidance, ensure_ascii=False),
        "这是本方案的有效深度约束：完整池能提供不同功能的新证据时，各章要点合计不得低于上面的“本篇要点数量下限”，并按“各章类型深度参考”展开；不要在每章只讲一两个要点后就提前结束。只有确实找不到新功能证据时才允许短缺。",
        "按素材真实句长校准的规模参考（reference_only，不是硬性句数）：" + json.dumps(pacing_reference, ensure_ascii=False),
        "M1 输出必须紧凑：章节数量由完整故事决定，不设上限；最多 3 组 opening_evidence_packages。整个 JSON（含标点）控制在 6000 个中文字符以内，宁可让章节字段更短，也不能删掉一个真实且必要的故事章节。只返回 schema 中的键，不写选择过程、章节长说明、重复 product_scope 范围或备用证据。director_title、core_desire、central_promise、opening_promise、title、buyer_advance、chapter_job、stop_condition、purchase_value、payoff_basis 每项最多 24 个汉字；completion_requirements 只写一项、最多 30 个汉字。每章 evidence_locations 最多 2 个 ID。为每章填写 source_budget_seconds 和 completion_requirements：当安全素材可支撑时，各章预算合计必须覆盖 source_min，并以 source_target 为中心、不得超过 source_max；相邻两章若都只会重复同一教程、同一卖点或同一购买问题，必须在本轮合并、删去其一，或写清第二章新增的问题。预算要有完整字幕中的真实证据支持；不足时明确说明缺少哪类真实内容。",
        "章节数量的优先级：先保证叙事完整——同一个卖点、同一段教学、同一购买问题只讲一次，不为凑数量拆碎或重复。但本方案必须在第一遍就把章节规划足：若按当前章节数覆盖不了上面的“建议推进章节数下限”，必须继续增加能带来新购买认知的章节，直到覆盖下限；每一章仍必须有自己的新购买判断，禁止换标题重复讲。只有素材确实无法支撑新的购买判断时，才写 source_limited 或自然收束，不能用未提供内容补足。",
        "本轮先定观众：他是谁、卡在哪、看完要变成什么（用上面五问的答案），再按这五问的自然追问安排章节。每章 buyer_advance 必须写出观众从上一章的状态推进到了哪个新状态；如果两个章节只能靠同一句原话或同一结论才能成立，就在本轮合并，而不是换标题重复讲。同一操作演示只能服务一个章节：它最多证明‘容易完成’或‘能形成某个结果’其中一个购买判断，不能把扣法、步骤、第一种/第二种/第三种穿法分别改名成连续章节。每章 completion_requirements 只能有一项：它是本章唯一不可缺的、可由一组完整短语义直接核验的购买判断。不要把颜色、材质、版型、搭配、物流等多个独立事实塞进同一章；它们各自只能在有独立推进时成为另一章。支持这个判断的补充证明不另写成 needs。evidence_locations 建议列1-3个本轮安全池代表ID，仅定位事实，不是最终片单；必要时可多列，没证据写空数组并说明缺口，不编造ID。先顺读证据及必要上下句，确认真实口播能完整讲出问题、解释和结论后再承诺章节；标题中的每个核心承诺都必须有完整证据链，例如承诺版本对比时必须同时存在版本身份、差异和最终结论。chapter_job 简短说明本章回答什么，以及怎样承接上一章；不强套固定问题顺序。",
        (
            f"用户本次要求 {plan_count} 个成片版本。请一次返回恰好 {plan_count} 个完整且明显不同的导演故事合同；"
            "每个方案都必须拥有自己的 core_desire、opening_promise、video_structure 和完整 chapter_packets，"
            "不能只是同一章节换顺序。第一项为 AI 推荐主方案，其余为可直接执行的 alternative。"
            if plan_count > 1 else
            "本次只执行一个完整主方案；必须同时给出恰好 2 个仅有标题、核心购买理由和开场承诺的备选方向摘要（S2、S3）。"
            "不得为 S2/S3 生成 chapter_packets、选片、字幕或审计字段；用户确认选择后才会单独为所选方向生成完整方案。"
        ),
        "先核实 product_scope 再编故事：整体主讲时段、反复展示对象与用户指定商品优先；30分钟里两句裤子不能因为卖点强就成为主商品。先为主商品寻找最多 3 组不同切入的 Hook -> 紧接 payoff 的 opening_evidence_packages，再决定主故事；按实际开场质量排序，不为填满数量重复同一组句子。这些 opening_evidence_packages：hook_subtitle_ids 和 payoff_subtitle_ids 只能引用当前安全池真实 ID，分别证明停留理由和紧接的新增结果、机制、证明或穿法。先把这组连读为没有标题、没有前文、没有画面的前 6-10 秒：Hook 必须自己说清主商品及具体结果/顾虑，payoff 必须让该结果更可信或更有用；“又没什么特点”“放在这里就”“捏着这一根”这类依赖上文的半句不能作为 Hook 或 payoff。payoff 必须推进一个不同事实，不能只是把 Hook 的结论重说一次；例如“可拆”之后仍说“丝巾能拆”不是兑现。它只证明该故事方向有可行开场，不是最终片单，不锁定最终顺序，下一遍可基于完整池改选。没有足够强证据时宁可不报；找不到可兑现的停人型开场时（例如池内只有解开/扣上扣子这类操作演示），opening_promise 写成受限（受限-素材无合格独立开场）或按主商品最强的结果句安排开场，不能用操作演示句、半截话、泛情绪或无答案的质疑冒充。identity_evidence_ids 只列1-2个最直接依据；所有备选方向也必须是同一个主商品。",
        "identity_evidence_ids 只列 1-2 条最直接、能明确核实商品名或指代的代表依据，不要抄全片 ID。",
        "先顺读全片，在 product_scope.source_product_sections 用连续 ID 范围简记换品：start_id/end_id 是原片归属边界，不是选片。覆盖全片且不重叠；重新回到同款要另开范围。",
        "【分段粒度】按自然话题段切分，单个范围原则上不超过约 20 行；禁止把横跨多个话题的长时间片段合并成一个范围 —— 一个范围只挂一个类型，长段必然把整段归属带偏。",
        "【混讲范围怎么判】一个范围里同时出现主商品卖点、搭配建议、别的商品或闲聊时，以「观众此刻正在被推销的那件商品」为准：只要该范围仍在为主商品推进购买理由（面料、版型、上身效果、搭配、尺码），就标主商品的类型与实际商品名，不要因为掺了别的商品或闲聊就整段划出去。",
        "只有「明确在讲另一件商品」或「与商品无关的运营流程话术、纯闲聊」才标成那个商品的类型或 other；这类范围也要尽量切短、切准，不要用一个大范围把中间的带货内容一起盖掉。每个范围只写类型和不超过 12 个汉字的实际商品名，证据不足写 unknown，不解释原因。",
        "长目标通过探索更多真实存在的新购买章节来体现，禁止重复同一结果、同一机制或同义口号。",
        "chapter_packets 只能描述章节职责。opening_evidence_packages 是唯一允许出现的字幕 ID 字段：每个完整方案最多 3 组，必须同时填写非空 hook_subtitle_ids、payoff_subtitle_ids、purchase_value、payoff_basis，且 Hook 与 payoff 不得重复 ID。除此之外 JSON 中不得出现 beats、subtitle_ids、source_span、verbatim、时间戳或 final_readthrough。S2/S3 方向摘要不得带 opening_evidence_packages。",
        "可选视频结构仅供导演判断，不需要逐个覆盖：",
        json.dumps(available_video_structures(content_contract), ensure_ascii=False, separators=(",", ":")),
        "返回结构：",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "",
        "返回前确认：core_desire 不是卖点清单；每章服务同一故事且有真实证据位置；章节之间存在明确说服关系；证据定位不代表已选择最终口播。只返回 JSON，且每个字段、每个数组元素各占一行（不必缩进对齐，但必须有换行）；不要把整个 JSON 挤在一行。",
    ])


def _casting_chapter_duration_budgets(
    story_contract: Mapping[str, Any], source_target: float,
) -> list[dict[str, Any]]:
    """Scale AI-authored budget proportions; do not choose or reorder content."""
    budgets = []
    strategies = story_contract.get("strategies") or []
    if isinstance(strategies, Mapping):
        strategies = [strategies]
    for index, strategy in enumerate(strategies, 1):
        if not isinstance(strategy, Mapping):
            continue
        raw_chapters = strategy.get("chapter_packets") or []
        if isinstance(raw_chapters, Mapping):
            raw_chapters = [raw_chapters]
        chapters = [c for c in raw_chapters if isinstance(c, Mapping)]
        weights = []
        for chapter in chapters:
            try:
                weight = float(chapter.get("source_budget_seconds") or 0)
            except (ValueError, TypeError):
                weight = 0
            weights.append(weight if math.isfinite(weight) and weight > 0 else 1.0)
        # Only a setup/hook chapter gets this planning budget. A first
        # chapter already explaining the product keeps its authored share.
        # This changes no selected sentence and is never a render cutoff.
        if len(chapters) > 1 and source_target >= 30 and str(chapters[0].get("chapter_kind") or "").lower() in {"pain", "hook"}:
            remaining_weight = sum(weights[1:])
            weights[0] = min(weights[0], remaining_weight * 0.15 / 0.85)
        total = sum(weights)
        cumulative = 0.0
        chapter_budgets = []
        for chapter, weight in zip(chapters, weights):
            before = cumulative
            cumulative += weight
            end = round(source_target * cumulative / total, 3)
            chapter_budgets.append({
                "chapter_id": chapter.get("chapter_id"),
                "source_budget_seconds": round(end - round(source_target * before / total, 3), 3),
                "cumulative_source_seconds": end,
            })
        if chapters:
            budgets.append({"strategy_id": str(strategy.get("strategy_id") or f"S{index}"),
                            "source_target": source_target, "chapters": chapter_budgets})
    return budgets


def _casting_execution_contract(
    story_contract: Mapping[str, Any],
    *,
    duration_range: Mapping[str, Any],
    story_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Pass Casting only the frozen facts it can execute.

    The full first-pass response remains the internal story record used by the
    workbench and validators.  Casting needs neither its repeated rationale
    nor display-only structure prose.  Keeping this boundary small reduces
    prompt reading; its effect on reasoning usage must be measured on real
    requests. Program code takes no semantic selection responsibility.
    """
    normalized_budgets = {
        item["strategy_id"]: {
            chapter["chapter_id"]: chapter["source_budget_seconds"]
            for chapter in item["chapters"]
        }
        for item in _casting_chapter_duration_budgets(
            story_contract, float(duration_range["source_target"]),
        )
    }
    raw_strategies = story_contract.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        raw_strategies = (raw_strategies,)
    audit = dict(story_audit or {})
    opening_evidence_by_strategy = {
        str(item.get("strategy_id") or ""): list(item.get("valid_packages") or ())
        for item in audit.get("opening_evidence") or ()
        if isinstance(item, Mapping)
    }
    strategies: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_strategies, 1):
        if not isinstance(raw, Mapping):
            continue
        strategy_id = str(raw.get("strategy_id") or f"S{index}")
        chapter_budgets = normalized_budgets.get(strategy_id, {})
        scope = dict(raw.get("product_scope") or {})
        source_sections: list[dict[str, Any]] = []
        for section in scope.get("source_product_sections") or ():
            if not isinstance(section, Mapping):
                continue
            source_sections.append({
                "from": section.get("start_id"),
                "to": section.get("end_id"),
                "type": section.get("product_type"),
                "name": section.get("subject_product"),
                "evidence": list(section.get("identity_evidence_ids") or ())[:3],
            })
        chapters: list[dict[str, Any]] = []
        for chapter in raw.get("chapter_packets") or ():
            if not isinstance(chapter, Mapping):
                continue
            chapters.append({
                "id": chapter.get("chapter_id"),
                "kind": chapter.get("chapter_kind"),
                "question": chapter.get("purchase_question") or chapter.get("purchase_question_id"),
                "advance": chapter.get("buyer_advance") or chapter.get("new_buyer_knowledge"),
                "coverage": chapter.get("coverage"),
                "job": chapter.get("chapter_job") or chapter.get("purpose"),
                "budget": chapter_budgets.get(chapter.get("chapter_id")),
                "needs": list(chapter.get("completion_requirements") or ()),
                "requirement_count": len(list(chapter.get("completion_requirements") or ())),
                "evidence_locations": list(chapter.get("evidence_locations") or ()),
            })
        if not chapters:
            continue
        elapsed_budget = 0.0
        elapsed_floor = 0.0
        elapsed_ceiling = 0.0
        source_target = max(0.001, float(duration_range["source_target"]))
        source_min = max(0.0, float(duration_range["source_min"]))
        source_max = max(source_target, float(duration_range["source_max"]))
        for chapter in chapters:
            elapsed_budget += float(chapter.get("budget") or 0.0)
            chapter["budget_end"] = round(elapsed_budget, 3)
            floor_end = round(source_min * elapsed_budget / source_target, 3)
            ceiling_end = round(source_max * elapsed_budget / source_target, 3)
            chapter["budget_floor"] = round(floor_end - elapsed_floor, 3)
            chapter["budget_ceiling"] = round(ceiling_end - elapsed_ceiling, 3)
            chapter["budget_end_floor"] = floor_end
            chapter["budget_end_ceiling"] = ceiling_end
            elapsed_floor = floor_end
            elapsed_ceiling = ceiling_end
        strategies.append({
            "id": strategy_id,
            "role": raw.get("director_plan_role") or ("primary" if index == 1 else "alternative"),
            "desire": raw.get("core_desire"),
            "promise": raw.get("central_promise"),
            "opening": raw.get("opening_promise"),
            # M1 evidence is a feasibility hint, never an instruction to use
            # these IDs or to put them at the start of the final sequence.
            "opening_evidence": opening_evidence_by_strategy.get(strategy_id, []),
            "product": {
                "main": scope.get("main_product"),
                "type": scope.get("product_type"),
                "sales": scope.get("sales_scope"),
                "support_rule": scope.get("supporting_products_rule"),
                "ranges": source_sections,
            },
            "chapters": chapters,
            "evidence_conflicts": _story_evidence_location_conflicts(raw, strategy_id=strategy_id),
        })
    overloaded_requirements = [
        {
            "strategy_id": str(strategy.get("id") or "S1"),
            "chapter_id": str(chapter.get("id") or ""),
            "requirement_count": int(chapter.get("requirement_count") or 0),
        }
        for strategy in strategies
        for chapter in strategy.get("chapters") or ()
        if int(chapter.get("requirement_count") or 0) > 1
    ]
    return {
        "version": "director-cast-exec-v1",
        "duration": {
            key: duration_range.get(key)
            for key in ("source_min", "source_target", "source_max", "speed_factor")
        },
        "story_check": {
            "valid": bool(audit.get("story_contract_valid", True)),
            "warnings": list(audit.get("warnings") or ())[:4],
            "overloaded_chapter_requirements": overloaded_requirements,
        },
        "strategies": strategies,
    }



# [2026-10-06] 撤销「M1 证据可被无视」的豁免；关闭 = 逐字节回旧提示词。
# 三句同义豁免（预算/开场/evidence_locations）必须一起撤销，只改一句等于没改。
CAST_EVIDENCE_BINDING_ENABLED = True
_CAST_BUDGET_EVIDENCE_LINE = (
    "第一遍预算是规划参考，内容边界删章后也由你在剩余故事内重新分配深度；不能为了守住原预算而只选半句话。自然顺滑与真实新价值优先，确实无法接近目标时报告具体素材缺口。"
    if CAST_EVIDENCE_BINDING_ENABLED else
    "第一遍预算与证据位置只是规划参考，内容边界删章后也由你在剩余故事内重新分配深度；不能为了守住原预算而只选半句话。自然顺滑与真实新价值优先，确实无法接近目标时报告具体素材缺口。"
)
_CAST_OPENING_EVIDENCE_LINE = (
    "execution_contract 中每个 strategy.opening_evidence 是第一遍找到的真实 Hook -> payoff 可行性证据，**并且当开场回执 quality=strong 时必须落地**：最终片单的第一句必须是它的 Hook，第二句起就要开始兑现 payoff，不得在前面插入铺垫；确实要改选，必须在 chapter_goal 里写明替代理由，否则视为不合规。把它与完整安全池中的候选一起比较：只有最终前缀实际保留 Hook 和紧接兑现、并且正文能继续回答该购买问题时才可采用；否则改选更强的安全组合，并用 chapter_revision 收窄、合并或删除不成立的首章。"
    if CAST_EVIDENCE_BINDING_ENABLED else
    "execution_contract 中每个 strategy.opening_evidence 是第一遍找到的真实 Hook -> payoff 可行性证据，**并且当开场回执 quality=strong 时必须落地**：最终片单的第一句必须是它的 Hook，第二句起就要开始兑现 payoff，不得在前面插入铺垫；确实要改选，必须在 chapter_goal 里写明替代理由，否则视为不合规。把它与完整安全池中的候选一起比较：只有最终前缀实际保留 Hook 和紧接兑现、并且正文能继续回答该购买问题时才可采用；否则改选更强的安全组合，并用 chapter_revision 收窄、合并或删除不成立的首章。不得仅因为 M1 引用了某些 ID 就照抄它们。"
)
_CAST_EVIDENCE_LOCATION_LINE = (
    "evidence_locations 是本章必须覆盖的证据依据：池中存在能兑现它的完整安全原话时，必须在本章落地；确无可用原话时，在本章如实标缺口（missing_content 写清缺哪条），不得用无关句冒充兑现、也不得静默略过。"
    if CAST_EVIDENCE_BINDING_ENABLED else
    "evidence_locations 只是事实定位，先回查其上下文再从完整安全池选句；不得当作必选或唯一候选。若证据不支持 needs，在本次选片中如实标缺口，不用无关句冒充兑现。"
)
def build_two_pass_cast_prompt(
    *,
    story_contract: Mapping[str, Any],
    story_audit: Mapping[str, Any] | None = None,
    draft_audit: Mapping[str, Any] | None = None,
    subtitles: Sequence[Mapping[str, Any]],
    content_contract: Mapping[str, Any] | None = None,
    executable_subtitle_ids: Sequence[int] | None = None,
    target_duration: float = 45.0,
    duration_tolerance: float | None = None,
    director_controls: Mapping[str, Any] | None = None,
    output_speed_factor: float = 1.0,
    source_context_subtitles: Sequence[Mapping[str, Any]] | None = None,
    opening_hook_pool: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Build the single exact-Beat casting call over the complete pool."""
    rows = _director_casting_rows(subtitles, executable_subtitle_ids)
    # 连续窗切分（方案 B）：候选池从「逐句」改成「连续口播窗」。
    cast_window_rows: list[dict[str, Any]] = []
    cast_window_dropped_ids: list[int] = []
    if CAST_WINDOW_POOL_ENABLED and rows:
        cast_window_rows, cast_window_dropped_ids = _cast_windows_for_rows(rows)
    pool_rows: list[dict[str, Any]] = cast_window_rows or rows
    duration_range = director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor)
    depth_contract = director_duration_depth_contract(target_duration)
    execution_contract = _casting_execution_contract(
        story_contract,
        duration_range=duration_range,
        story_audit=story_audit or draft_audit,
    )
    safe_pool_seconds = round(
        sum(float(row["end"] - row["start"]) for row in pool_rows), 3,
    )
    execution_contract["duration"].update({
        "safe_candidate_count": len(pool_rows),
        "safe_candidate_unit": "window" if cast_window_rows else "sentence",
        "safe_candidate_window_count": len(cast_window_rows) or None,
        "safe_candidate_excluded_single_windows": len(cast_window_dropped_ids) or None,
        "safe_candidate_source_seconds": safe_pool_seconds,
        "safe_pool_can_reach_source_min": (
            safe_pool_seconds >= float(duration_range["source_min"])
        ),
    })
    story_cited_ids = _story_cited_evidence_ids(story_contract, executable_subtitle_ids)
    main_pool_rows, main_pool_has_sections = _main_product_pool_rows(
        story_contract, subtitles, executable_subtitle_ids)
    main_pool_seconds = round(
        _windows_seconds_for_ids(
            cast_window_rows, [int(row["id"]) for row in main_pool_rows])
        if cast_window_rows else
        sum(float(row["end"]) - float(row["start"]) for row in main_pool_rows), 3)
    main_pool_can_reach_source_min = main_pool_seconds >= float(duration_range["source_min"])
    if main_pool_has_sections:
        execution_contract["duration"].update({
            "main_product_pool_seconds": main_pool_seconds,
            "main_product_pool_can_reach_source_min": main_pool_can_reach_source_min,
        })
    if cast_window_rows:
        story_cited_seconds = _windows_seconds_for_ids(
            cast_window_rows,
            _story_cited_evidence_ids(story_contract, executable_subtitle_ids),
        )
    else:
        story_cited_seconds = _story_cited_evidence_seconds(story_contract, subtitles, executable_subtitle_ids)
    unused_pool_seconds = round(max(0.0, safe_pool_seconds - story_cited_seconds), 3)
    execution_contract["duration"].update({
        "story_cited_source_seconds": story_cited_seconds,
        "unused_pool_seconds": unused_pool_seconds,
        "story_citation_is_not_the_ceiling": True,
    })
    mean_seconds = sum(float(row["end"] - row["start"]) for row in rows) / max(1, len(rows))
    pacing_reference = {
        "pool_mean_seconds": round(mean_seconds, 3),
        "approximate_beats_for_target": math.ceil(float(duration_range["source_target"]) / mean_seconds) if mean_seconds > 0 else None,
        "min_beats_for_source_min": math.ceil(float(duration_range["source_min"]) / mean_seconds) if mean_seconds > 0 else None,
        "reference_only": True,
    }
    execution_contract["duration"].update({
        "material_mean_beat_seconds": pacing_reference["pool_mean_seconds"],
        "min_beats_for_source_min_at_current_pace": pacing_reference["min_beats_for_source_min"],
        "approximate_beats_for_target_at_this_pace": pacing_reference["approximate_beats_for_target"],
    })
    for strategy in execution_contract["strategies"]:
        for chapter in strategy["chapters"]:
            chapter["approximate_beats"] = math.ceil(float(chapter["budget"] or 0) / mean_seconds) if mean_seconds > 0 else None
    main_pool_lines: list[str] = []
    if main_pool_has_sections:
        if main_pool_can_reach_source_min:
            main_pool_lines.append(
                f"主商品口径（硬约束）：属于主商品的可用原话共 {main_pool_seconds:.1f} 秒，这才是你能用来兑现主商品卖点的全部上限。"
                "整片实测仍低于 source_min 时，继续从主商品原话里补新的购买价值；绝不能用其他商品段落的原话凑时长。"
            )
        else:
            main_pool_lines.append(
                f"主商品口径（硬约束）：属于主商品的可用原话只有 {main_pool_seconds:.1f} 秒，本身不足 source_min。"
                "此时按主商品上限尽力选满并写明缺口，status 写 source_material_limited/natural_complete_below_target；"
                "绝不能用其他商品段落的原话凑时长。"
            )
    story_strategies = story_contract.get("strategies") or ()
    if isinstance(story_strategies, Mapping):
        story_strategies = (story_strategies,)
    executable_story_ids = [
        str(item.get("strategy_id") or f"S{index}")
        for index, item in enumerate(story_strategies, 1)
        if isinstance(item, Mapping) and list(item.get("chapter_packets") or [])
    ]
    strategy_schema = {
            "strategy_id": "S1",
            "opening_selection": {
                "receipt_version": OPENING_RECEIPT_VERSION,
                "selected_subtitle_ids": [101, 102],
                "quality": "strong/usable/limited",
                "compared_packages": [{
                    "subtitle_ids": [101],
                    "payoff_subtitle_ids": [102],
                    "product_evidence_ids": [101],
                    "decision": "selected/alternative/rejected",
                    "reason": "一句短评：具体停留理由、承接新增信息或淘汰缺口；不抄写口播",
                }],
            },
            "chapter_packets": [{
                "chapter_id": "冻结章节 ID",
                "beats": [{
                    "beat_function": "result/mechanism/proof/experience/risk_remove/styling/scene/trust",
                    "subtitle_ids": [101],
                    "utterance_complete": True,
                    "product_relation": "main_product/styling_support",
                    "subject_product": "这条原话实际讲述的商品，不是照抄目标名",
                    "subject_product_type": "/".join(PRODUCT_TYPES),
                    "product_evidence_ids": [101],
                    "supports_main_product": "搭配支持时解释怎样服务主商品；否则留空",
                }],
                "completion_status": "complete/needs_context/source_limited",
                "completion_receipts": [{
                    "requirement_index": 1,
                    "subtitle_ids": [101],
                }],
                "semantic_units": [[101, 102]],
                "missing_content": "仅 needs_context/source_limited 时填写具体缺口",
                "chapter_revision": {
                    "action": "仅当最终口播必须调整章节时填写 refine/merge/drop；未改动省略",
                    "source_chapter_ids": ["C1"],
                    "title": "仅 refine/merge 时填写的最终章节标题",
                    "buyer_advance": "仅 refine/merge 时填写的最终新增购买认知",
                    "chapter_job": "仅 refine/merge 时填写的最终章节职责",
                    "completion_requirements": ["仅 refine/merge 时填写的最终兑现要求"],
                },
            }],
            "whole_video_audit": {
                "status": "pass/natural_complete_below_target/source_material_limited",
                "duration_receipt": {"C1": "数字", "C2": "数字", "total": "数字"},
            },
        }
    casting_schemas: list[dict[str, Any]] = []
    for index, strategy_id in enumerate(executable_story_ids or ["S1"], 1):
        item = dict(strategy_schema)
        item["strategy_id"] = strategy_id
        item["director_plan_role"] = "primary" if index == 1 else "alternative"
        casting_schemas.append(item)
    cast_window_pool_lines: list[str] = []
    if cast_window_rows:
        _window_seconds = _windows_total_seconds(cast_window_rows)
        _window_mean = _window_seconds / len(cast_window_rows) if cast_window_rows else 0.0
        try:
            _target_windows = max(
                1, int(round(float(duration_range["source_target"]) / _window_mean))
            ) if _window_mean > 0 else 0
        except (KeyError, TypeError, ValueError):
            _target_windows = 0
        cast_window_pool_lines = [
            "本轮候选池按「连续口播窗」切分（方案 B）：每个 [Wnnn] 是一段 2-8 秒、能被整段听懂的原话，"
            "[IDs a-b] 是它包含的全部 SRT 行号。",
            "整窗选择硬规则（违反即视为选句失败）：窗是本次选片的最小单位。选定某个窗，就必须把该窗的"
            "每一个 ID 都按原顺序连续放进同一章的 beats（每行一个 beat），不得截窗、不得跳行、不得把同一窗拆到两章。",
            f"本轮已剔除只作上下文的行 {len(cast_window_dropped_ids)} 条（过短/碎片窗 + 重复/尺码/口令等废行），"
            "窗内每一行都已经是可用内容，直接整窗选择即可。被剔除的行只作为 [context ID] 上下文出现，不得引用；"
            "确实需要那句内容时，把它所在的邻窗整体选上。",
            f"本轮可选窗共 {len(cast_window_rows)} 个、合计 {_window_seconds:.1f} 秒。"
            + (
                f"目标 {float(duration_range['source_target']):.0f} 秒约需要 {_target_windows} 个窗（允许 ±1），"
                "低于这个窗数就等于没选满。"
                if _target_windows else ""
            ),
            "口径提醒：execution_contract 里 approximate_beats / min_beats_for_source_min / "
            "approximate_beats_for_target 的计数单位已统一为「窗数」，不是句数。",
            "subtitle_ids / product_evidence_ids / completion_receipts 一律填 SRT 行号（如 167、168），不要填 W 编号。",
        ]
    schema = compact_director_wire_payload({"strategies": casting_schemas})
    _cast_hook_pool_section = _opening_hook_pool_prompt(opening_hook_pool)
    # The transcript remains complete and ordered.  The following contract is
    # deliberately a small execution receipt, not a second copy of M1 prose.
    return "\n".join([
        "安全候选字幕片段按原片顺序：可选不代表句意完整。不足1秒的片段只用于补全必要相邻语义；缺失ID不能视为上下句相邻，缺少必要上下文且无法补全的片段必须弃用。[context ID] 是不可选原片上下文，只用于核对商品切换、指代和否定事实；不得进入任何 beat、Hook 或 payoff，也不能把其中被禁的卖点换一种说法写进故事。1-5 秒优先，5-8 秒完整句仅作例外；没有 Strong Ranking、没有 TopK、没有卖点预分类。",
        (
            _director_window_pool_lines(
                source_context_subtitles or subtitles,
                cast_window_rows,
            )
            if cast_window_rows else
            _director_product_context(
                source_context_subtitles or subtitles,
                rows,
                include_nonselectable_context=True,
            )
        )
        or "（没有满足 1-8 秒且可执行的字幕）",
        "口播习惯词不得作为任何 beat 的句子：看到没有／对不对／是不是／好不好／"
        "确定要拍／我都讲了／专门给你们定的／姐妹们 等。含这些词的行已在候选池里"
        "标为 [context ID]；若某句以互动呼语开头或结尾（例如「你有没有发现…」"
        "「…对不对？」），而相邻行有同一内容的干净版本，必须选干净的那一行。",
        *cast_window_pool_lines,
        *([_cast_hook_pool_section] if _cast_hook_pool_section else []),
        "",
        "第一遍的完整故事已保存在工作台，并冻结主商品、核心购买方向、整片承诺和章节推进方向。下面是本轮唯一需要执行的紧凑合同；product.ranges 是换品边界，chapters 的 advance/job/needs/budget 是第一遍的计划。你必须让最终真实口播成为可兑现的故事，不能机械执行冲突或空洞章节：",
        json.dumps(execution_contract, ensure_ascii=False, separators=(",", ":")),
        f"故事引用不是上限：第一遍只引用了 {story_cited_seconds:.1f} 秒证据（{len(story_cited_ids)} 条），"
        f"你的候选池共 {safe_pool_seconds:.1f} 秒、未用 {unused_pool_seconds:.1f} 秒。"
        "第一遍的证据条数不是你的选句上限；只要整片实测总秒数仍低于 source_min，就必须继续从安全池补新的购买价值。",
        *main_pool_lines,
        "若 story_check.overloaded_chapter_requirements 非空，第一遍把多个独立购买判断塞进了同一章。你必须在本次回复用 chapter_revision 把该章收窄为一个可由最短完整原话兑现的判断，或与相邻重复章合并/删除；不得为了逐项打回执而堆叠同义口播。",
        "budget 是本章原声目标，budget_ceiling 是本章超长预警；budget_end 是到本章结束的累计目标。budget_floor 和 budget_end_floor 是到该章为止的规划进度线：只要整片累计低于 source_min，它们就是「本章还可以继续补新价值」的提示，而整片达标仍是硬要求。逐章完成取舍，避免前几章耗尽全片预算；无需换算播放速度。",
        "时长门槛：若 duration.safe_pool_can_reach_source_min=true，source_min 是本次交付下限。先剔除残句、重复和无关内容；其后必须继续寻找服务当前故事的新机制、证明、体验、场景或顾虑解除，直到实测累计达到 source_min。不能因为首轮章节讲得简短、预计句数是参考或想保持精炼就提前交卷。只有逐章说明缺失的具体购买判断、且确实没有可选原话可补时，才允许 source_material_limited/natural_complete_below_target。",
        "本轮即最终选片：这一轮就要按 source_target/source_min 排出完整片单；预计句数、首轮章节估算或想保持精炼都不是提前收尾的理由。",
        (
            "片长按真实语速折算（硬要求）：这段素材平均每窗约 "
            f"{_window_mean:.2f} 秒（窗是 2-8 秒的连续口播单位），整片要达到 source_min 至少需要 "
            f"{math.ceil(float(duration_range['source_min']) / _window_mean) if _window_mean > 0 else 0} 个窗；目标 "
            f"{float(duration_range['source_target']):.0f} 秒约需要 {_target_windows} 个窗（允许 ±1）。"
            "低于这个窗数就等于没选满；每个被选中的窗，其 IDs 必须全部按原顺序出现，不得截窗。"
            if cast_window_rows else
            "片长按真实语速折算（硬要求）：这段素材平均每句约 "
            f"{mean_seconds:.2f} 秒，整片要达到 source_min 至少需要 "
            f"{pacing_reference['min_beats_for_source_min'] or 0} 条短句；低于这个条数时整片必然不达下限，必须继续选新的购买价值句子，而不是提前收尾。"
        ),
        "ID 纪律（硬约束）：每个 subtitle_ids / product_evidence_ids / completion_receipts 只能填上面“安全候选字幕片段”里出现过的数字 ID；[context ID] 段里的数字一律禁止引用，任何越界 ID 都会被丢弃。",
        "执行顺序：先从安全池识别完整语义单元；再联合选择Hook、紧接兑现与正文，按播放顺序连读、去重；最后归章计时。章节职责服务已经连顺的口播，不能为了填章抽取半句；不能靠不同 role 标签把同义句分装进两章。整片最多保留一个以操作步骤为主的证明块，第一种/第二种/第三种穿法只作证据，不各自凑章。",
        _CAST_BUDGET_EVIDENCE_LINE,
        "",
        f"用户内容合同：{_contract_forbidden_lines(content_contract)}",
        _director_controls_prompt(director_controls, stage="cast"),
        (
            f"第一遍已冻结 {len(executable_story_ids)} 个完整导演方案。请在这一次回复中为每个 strategy_id 分别完成完整选句、排序、开场比较和整片审阅；"
            "不得只执行主方案，也不得把其他方案退回成方向摘要。方案可共享必要证据与闭合原话，差异体现在购买切入点、开场和前段说服路径；同一原话用于不同成片不算本片无效重复，不得为了与其他版本不重样而缩短某套方案。"
            if len(executable_story_ids) > 1 else
            "只执行冻结的一个主方案。"
        ),
        "取舍优先级：商品与事实准确、语义完整、前后顺畅和不重复是不可突破的前提；在此前提满足后，source_min/source_target 是交付要求，不是可忽略的参考。不得为达到下限保留残句、重复或无关内容，也不得在安全池容量足够时仅因想精炼而提前结束。超过source_max先删除非必要完整单元，不截半句；无法兼顾时必须报告具体缺口，不能冒报pass。",
        "真实句长参考（min_beats_for_source_min = 达到 source_min 所需的最少片段数下限，approximate_beats_for_target = 达到 source_target 所需的片段数下限；它们是下限不是参考值，最终片段数低于下限就等于没选满）：" + json.dumps(pacing_reference, ensure_ascii=False, separators=(",", ":")),
        "自检口径（必须照做）：把每个已选 ID 的真实秒数**逐条相加**得到 total，不要凭印象或估算；只要 total 低于 source_min，就回到安全池继续补新的购买理由，直到 total 达到下限。approximate_beats 的句数下限见上，没有上限。每个最终 beat 的 ids 必须恰好写一个 ID；不得把多条字幕塞进一个 ids 数组来隐藏连续长口播。字幕行不等于一句话：必要相邻句必须分别写成连续 beats，并在 semantic_units 中列成一个完整语义单元。**内容完整优先于时长**：不要为了压到某个秒数而抽走半句；一句说不完就接着选它的下一句，哪怕因此到 6-8 秒。**每个 beat 必须自判 utterance_complete**（true/false，**只按内容判断，不看标点**——本地 ASR 的标点不可信）：只要这句单独连读讲不通、必须靠下一句才讲完（例如停在‘它/这个/因为/就是/那么’这类承接处），就必须填 false，并且**紧邻的下一个 beat 必须是它的承接句**——即原字幕的下一行原话，不得改写、不得插进别的句子。两者在 semantic_units 中列成同一组。**不得以没说完的话收尾**；也不得把承接句挪到很久之后再讲。完整表达若确实超过 8 秒，另选更短的完整表达，或放弃该卖点并收窄章节承诺；不能抽出含卖点的半句，不能用拆分 semantic_units 规避限制。",
        "章节兑现只看最终口播：标题和 role 标签不算证据；介绍材质不等于说明不扎，鼓励尝试不等于教会搭配，说到网眼洞口不等于已经讲出做工结论，说到某一版不等于完成版本对比。每项 needs 用最短的一个完整语义单元直接说出问题、解释或证据和结论；只有补句不可或缺时才增加相邻句。alternative_beats 不参与兑现。不要把尚未选入的关键句只放在备选里。",
        "按原字幕前后核对口语依赖：‘没有这个点’必须保留所指结论，‘因为/所以/它/那种’必须有明确对象和完整谓语。先保留必要的前后短句再检查预算；不能为限制句数跳过结论或截掉句尾。开场不得保留‘又没什么特点’、‘刚刚讲过了’、‘放在这里就’、‘捏着这一根’等依赖直播现场或画面才能成立的铺垫/指令；正文也不能用它们替代一个完整购买判断。",
        "全片先建立 ID 归属：同一 ID 只能放入一个最终章节，重复播放不增加内容或时长。execution_contract.evidence_conflicts 中同一事实被第一遍多个章节引用时，必须只分配给其中一章；另一章选择新的必要原话，或在本次回复中合并/取消。按最终 ID 顺序逐对连读，并为每次衔接在内部确认一种真实关系：前句提出顾虑/结论而后句回答或证明；前句给机制而后句给结果；前句给结果而后句解释原因；前句结束一个完整判断而后句自然引入不同但相关的新购买价值。若两句只能靠章节标题、画面、被跳过的直播上下文或模型改写才能连上，必须删掉其中一句、补一条真正的连接句，或合并/取消章节。不能把“有三种穿法”接到“又没什么特点”这类无承接残句，也不能用连续操作指令代替购买判断。保留必要上下句来闭合“因为/但是/这个效果”等依赖，删掉残句、寒暄和全片同义重复。每章必须兑现自己的 advance；optional 无新增价值可删。",
        "开场召回：在完整安全池中寻找能服务当前购买方向的真实开头，不限于第一章 evidence_locations 或开头附近；opening_promise 是待原话兑现的意图，不是让你照拟定文案找近义句。把必要上下句作为完整候选，不能删除不利语气、拼出新判断或默改疑似 ASR 错词。选定前先做纯文本盲读：隐藏章节标题和画面，只听前 6-10 秒仍须知道在讲什么商品、它带来什么具体好处、下一句新增了什么；任一答案缺失就换候选或 quality=limited。没有足够候选就如实少报。开场句必须能独立成句：不得以「如果说/然后/因为/所以/那/你们只要」这类连接词或半截话开头，不得停在解开/扣上/第几步这类操作口令上；这类句子只能进正文。",
        _CAST_OPENING_EVIDENCE_LINE,
        "开场联合选择Hook与紧接兑现。痛点型Hook之后，payoff必须开始回答本款怎样解决该顾虑，给出具体效果、机制或证明；继续吐槽普通款、把痛点讲得更具体、或宣称不用再买，都不算本款兑现。结果型Hook之后须增加解释或证据；可拆→还能拆这类同义重复不能算兑现。不能仅因两句内容不同就判定兑现；找不到完整组合时quality=limited。",
        "同品类不等于同一件商品：‘这件/那件/刚才那件’必须按原片指代核对。product.ranges 只是第一遍判断，不能替代真实身份依据；不得把另一件同类商品的效果移给主商品，也不能把缺失上下文当作身份已确认。商品证据不足的候选记 rejected；仅有画面才能成立的句子要在短评中说明，不能声称音频已自立。",
        "opening_selection.compared_packages 保留本轮最有竞争力的至多3个不同真实开场及必要淘汰例，按AI综合判断从优到劣列出（不是关键词计分或召回 TopK）。每项用 subtitle_ids 标 Hook，用 payoff_subtitle_ids 标紧接的兑现句，用 product_evidence_ids 指向原片的实际指代依据（可引用 context ID，但绝不能将 context ID 剪进 Hook 或 payoff），并给一句可核对的结果短评。selected 的 reason 必须点明 payoff 新增的具体事实；若只能写成重复 Hook 的结论，该组合不得 selected。只将第一项写 selected，其余 alternative/rejected；缺 payoff 或身份依据的 rejected 可写空数组。selected_subtitle_ids 必须等于 selected 的 Hook+payoff，且逐项等于最终片单前缀；比较回执本身不会自动插句、替换或重排预览。三个候选必须是三个不同的开场角度（例如强结果 / 强判断 / 反差 / 痛点 / 情绪），不得是同一卖点或同一动作的换词变体。若本轮已注入【独立开场候选池】，selected 的 Hook 必须取自池内 subtitle_ids；池非空却没用池内句、或池为空仍硬凑开场，均视为选开场失败。",
        _CAST_EVIDENCE_LOCATION_LINE,
        "字幕行不等于完整语义：仅当必要相邻原字幕跨多个 beats 时才输出 semantic_units；单行完整句省略。每组只列跨行依赖的 ID 数组，按原话顺序连续出现在本章最终 beats 中，不能引用其他章或未选句，并且总原声不超过 8 秒。程序只检查声明是否完整执行，不自动补句、拼句、截断或重排。",
        "严格按 product.ranges 回查‘它/这条/这套’。开场和非 styling 章节只能选择已核实的主商品范围；未知范围或其他商品的泛情绪不能承担主商品卖点。每条最终或备选 Beat 都填写关系、实际商品、类型和 1-2 条指代依据；搭配品只能作为 styling_support，不能把它自身效果归给主商品。",
        "正文只返回最终可执行 beats，不返回 alternative_beats；开场比较只保存在 opening_selection。每个标记 complete 的 chapter 的每项 needs 都用 completion_receipts 逐项列出 requirement_index（从 1 开始）和本章最短已选 semantic unit 的 subtitle_ids；回执只写数字，不写解释。答上 needs 只是本章的最低完成线：若本章累计原声低于本章 budget_floor（进度线）而整片又没到 source_min，本章即视为未完成，必须继续从安全池补新的购买理由（机制/证明/体验/场景/顾虑解除），不得用同义句凑数，也不得提前收尾。，不能引用 context 或其他章。只有全部有回执才写 complete；否则写 needs_context/source_limited，并用 missing_content 写一个具体缺口。",
        "章节调整只为让最终口播真实成立：可取消没有新价值的 optional/recommended 章节；可把回答同一购买问题的相邻章节合并；可按实际口播收窄 title、advance、job、needs。此时在保留的 chapter_packet 填 chapter_revision。merge 的 source_chapter_ids 必须是连续的原章节，且保留其中第一个 chapter_id；drop 的 chapter_packet 不放 beats，并只引用自身。正文剩余chapter_id保持第一遍顺序；开场例外：允许把后章兑现Hook的完整单元提前到首章，原章不得重复引用，并用首章chapter_revision收窄或调整职责及兑现要求。不得新增章节、改变主商品、改写 core_desire 或用章节调整掩盖重复。未调整不要输出 chapter_revision。",
        "输出前在本次回复内部完成三次检查：逐章连读是否完整兑现 needs；以每个最终 beat 的单个 ID 的真实秒数逐章累计，确认没有超过 8 秒连续语义单元；整片是否兑现标题和 central promise 且没有重复。若总时长超过 source_max，必须在本次回复先删同义句、重复教程和 optional 章，尽量满足上限；若完整单元无法满足区间则如实标记素材限制；不能把超长片单写成 pass。whole_video_audit.duration_receipt 用紧凑的 {章节ID:原声秒数,total:总原声秒数} 回报你的加总；若不在 source_min/source_max 内，不得写 pass，先在同一次选片中调整。**先加总，再决定 status**：只要 duration.safe_pool_can_reach_source_min=true 且你的 total 低于 source_min，本次回复就必须先从安全池继续补选新内容直到达到 source_min，status 绝不能写 pass；只有在你逐章填写 missing_content（该章缺哪一条购买判断、安全池里确实没有可补的原话）之后，才允许写 natural_complete_below_target/source_material_limited。**status=pass 的唯一条件：total 落在 source_min–source_max 之间。**程序按 ID 还原全文并实测时长，任何不一致以实测为准。",
        "返回结构：",
        f"实际回复必须使用 {WIRE_VERSION}：products 是去重商品表；每个 Beat 使用 role/ids/rel/evidence/support/replaces/product_ref 的紧凑键名。product_ref 指向 products 的从 0 开始序号；不得返回完整字段名 subject_product 或 subject_product_type。",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "",
        "先在内部按最终 ID 从头连读整片，再归入章节：先只听口播，不看章节标题或画面。每个完整语义单元必须成立，单个字幕片段可依赖同单元内连续保留的必要上下句，且每一处切换都必须让陌生观众听出上一句与下一句为什么相连；章节只能标记已经连顺的购买推进，不能用章节标题替代句意。任何句子若只是在重复、跳题、留下悬空指代，或仅是“放这里/扣上去”之类操作口令，就不是保留理由。先删掉读不通、依赖画面、重复教程和无新增购买价值的句子，确认时长后才序列化最终结果。正文不抄口播、预算、累计过程、整片读稿或长篇审计；只在 opening_selection 留紧凑比较回执。除 whole_video_audit.duration_receipt 的紧凑自检数字外，不输出其他秒数或 continuity_links。没有跨行依赖、章节调整或缺口时，省略对应字段。开场 reason 只能陈述 Hook/Payoff 原话实际说出的文字事实，不得加入未选原话的视觉细节、设计名或结论；无法逐字对应就写 quality=limited。只返回紧凑 JSON。",
    ])


# ──────────────────────────────────────────────────────────────
# LLM 调用
# ──────────────────────────────────────────────────────────────

class AnalyzerError(Exception):
    """Analyzer 调用失败（网络/空响应/解析失败）。"""


class DirectorStageFormatError(AnalyzerError):
    """A paid Director stage returned content that cannot be safely recovered.

    Keep only a non-reversible diagnostic on the exception.  Provider output
    can include source transcript material, so it must not be copied into a
    task error, UI log, or release diagnostic.
    """

    def __init__(self, stage: str, diagnostics: Mapping[str, Any]) -> None:
        self.stage = str(stage)
        self.diagnostics = dict(diagnostics)
        fingerprint = str(self.diagnostics.get("response_sha256") or "")[:12]
        length = int(self.diagnostics.get("response_chars") or 0)
        super().__init__(
            f"Director {self.stage} 返回格式异常，已尝试一次仅格式恢复仍失败"
            f"（响应指纹 {fingerprint or 'unknown'}，长度 {length}）。请重试本次预览。"
        )


def _apply_analyzer_model_runtime_options(body: dict[str, Any], model: str) -> None:
    """Apply provider-supported execution controls for structured director JSON."""
    model_name = str(model or "").lower()
    if "deepseek" in model_name and "seed" not in model_name:
        # V4 charges reasoning as output. The director's JSON is a compact ID
        # receipt, so use the explicit response cap as its full budget.
        body["thinking"] = {"type": "disabled"}
    elif "seed" in model_name:
        # A/B（2026-09-17）：实测 thinking:disabled 对 Seed 生效（推理 token 归零）。
        # 先按关闭跑，并与“开着”的结果做质量对比后再定去留。
        # 注意：Ark 不允许 thinking 与 reasoning_effort 同时出现（会 400 InvalidParam）。
        body["thinking"] = {"type": "disabled"}
        body.pop("reasoning_effort", None)
    elif "qwen3.8" in model_name:
        # Qwen 3.8 enables xhigh thinking by default. For deterministic JSON
        # selection, that can spend minutes before the response starts.
        body["enable_thinking"] = False


def _director_seed_casting_uses_stream(model: str, stage: str) -> bool:
    """Keep a long Seed M2 response alive while it is being generated.

    Ark's non-streaming endpoint does not send a byte until Seed has completed
    its hidden reasoning and final JSON. The final Beat Casting prompt is the
    only Director stage that has repeatedly exceeded a normal socket read
    window. Streaming it lets each reasoning/content chunk refresh that read
    window without changing M1 or any other provider's request contract.
    """
    return stage == "Director_beat_casting" and "seed" in str(model or "").lower()


def _read_director_sse_response(response: Any) -> dict[str, Any]:
    """Collect an OpenAI-compatible chat-completions SSE response as JSON."""
    try:
        lines = iter(response)
    except TypeError:
        # A proxy may accept ``stream`` but buffer and return one ordinary JSON
        # document. Treat that as a valid response instead of manufacturing an
        # empty Director reply.
        return json.loads(response.read().decode("utf-8"))
    content_parts: list[str] = []
    finish_reason = ""
    usage: Mapping[str, Any] | None = None
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload_text = line[5:].strip()
        if not payload_text or payload_text == "[DONE]":
            continue
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        current_usage = payload.get("usage")
        if isinstance(current_usage, Mapping):
            usage = current_usage
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0] if isinstance(choices[0], Mapping) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}
        content = delta.get("content")
        if content:
            content_parts.append(str(content))
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
    return {
        "choices": [{"message": {"content": "".join(content_parts)}, "finish_reason": finish_reason}],
        "usage": dict(usage or {}),
    }


def _post_analyzer_request(
    *,
    api_key: str,
    base_url: str,
    model: str,
    user_prompt: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout: int,
) -> str:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": ANALYZER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    _apply_analyzer_model_runtime_options(body, model)

    request = urllib.request.Request(
        ai_chat_completions_url(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    context = create_ssl_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            _analyzer_error_body = error.read().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            _analyzer_error_body = ""
        from ai_model_config import describe_ai_http_error
        record_ai_call(
            module="commercial_analyzer", stage="M1_story_discovery", model=model,
            request_payload=body, success=False, error_type=f"http_{error.code}",
            response_payload=(({"error_body": _analyzer_error_body[:500]}) if _analyzer_error_body else None),
        )
        raise AnalyzerError(
            "Analyzer " + describe_ai_http_error(error.code, base_url, model, _analyzer_error_body)
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record_ai_call(
            module="commercial_analyzer", stage="M1_story_discovery", model=model,
            request_payload=body, success=False, error_type=type(error).__name__,
        )
        raise AnalyzerError(f"Analyzer 网络错误: {error}") from error
    record_ai_call(
        module="commercial_analyzer", stage="M1_story_discovery", model=model,
        request_payload=body, response_payload=result, success=True,
    )

    content = str(result.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    if not content:
        raise AnalyzerError("Analyzer AI 返回空内容")
    return content


def _post_two_pass_director_request(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    stage: str,
    max_tokens: int,
    timeout: int,
    response_hook: Callable[[str], None] | None = None,
) -> str:
    """Run one of the two paid semantic Director stages with a fixed budget."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    _apply_analyzer_model_runtime_options(body, model)
    use_stream = _director_seed_casting_uses_stream(model, stage)
    if use_stream:
        # Ark emits reasoning/content chunks before its final JSON. The
        # trailing usage chunk keeps the existing cost ledger complete.
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

    request_started_at = datetime.now(timezone.utc).isoformat()
    request = urllib.request.Request(
        ai_chat_completions_url(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    # M1 and M2 are dependent calls.  A transient provider rate limit after a
    # successful M1 should not discard that paid response or force an operator
    # to launch an entirely new preview.
    http_error: urllib.error.HTTPError | None = None
    network_error: BaseException | None = None
    for rate_limit_attempt in range(3):
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=create_ssl_context()
            ) as response:
                result = (
                    _read_director_sse_response(response)
                    if use_stream
                    else json.loads(response.read().decode("utf-8"))
                )
            http_error = None
            break
        except urllib.error.HTTPError as error:
            http_error = error
            if error.code != 429 or rate_limit_attempt >= 2:
                break
            retry_after = ""
            if error.headers:
                retry_after = str(error.headers.get("Retry-After") or "").strip()
            try:
                delay = float(retry_after)
            except ValueError:
                delay = float(2 ** (rate_limit_attempt + 1))
            time.sleep(max(1.0, min(15.0, delay)))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            network_error = error
            break
    if http_error is not None:
        # 把平台原文带进报错：只给 “HTTP 401” 时，用户无法区分 key 无效 /
        # 欠费 / 平台地址不对 / 被中间设备拦截。
        try:
            error_body = http_error.read().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            error_body = ""
        from ai_model_config import describe_ai_http_error
        record_ai_call(
            module="commercial_analyzer", stage=stage, model=model,
            request_started_at=request_started_at,
            request_payload=body, success=False, error_type=f"http_{http_error.code}",
            response_payload=(({"error_body": error_body[:500]}) if error_body else None),
        )
        raise AnalyzerError(
            f"Director {stage} "
            + describe_ai_http_error(http_error.code, base_url, model, error_body)
        ) from http_error
    if network_error is not None:
        record_ai_call(
            module="commercial_analyzer", stage=stage, model=model,
            request_started_at=request_started_at,
            request_payload=body, success=False, error_type=type(network_error).__name__,
        )
        raise AnalyzerError(f"Director {stage} 网络错误: {network_error}") from network_error
    choice = result.get("choices", [{}])[0]
    if not isinstance(choice, Mapping):
        choice = {}
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
    content = str(message.get("content", "") or "").strip()
    # Persist the provider-visible body before any budget/JSON diagnostic can
    # reject it.  Director artifacts are the only way to distinguish a real
    # truncation from a provider's JSON-format deviation after a paid call.
    if response_hook:
        response_hook(content)
    finish_reason = str(choice.get("finish_reason") or "").strip().lower()
    usage = result.get("usage") if isinstance(result.get("usage"), Mapping) else {}
    try:
        completion_tokens = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
    except (TypeError, ValueError):
        completion_tokens = 0
    details = usage.get("completion_tokens_details")
    if not isinstance(details, Mapping):
        details = usage.get("output_tokens_details")
    try:
        reasoning_tokens = int((details or {}).get("reasoning_tokens") or 0)
    except (TypeError, ValueError):
        reasoning_tokens = 0
    visible_completion_tokens = completion_tokens
    if 0 < reasoning_tokens <= completion_tokens:
        visible_completion_tokens -= reasoning_tokens
    reached_limit = (
        finish_reason in {"length", "max_tokens", "token_limit"}
        or visible_completion_tokens >= max(1, int(max_tokens) - 2)
    )
    if reached_limit:
        try:
            parsed_content = json.loads(content) if content else None
        except json.JSONDecodeError:
            parsed_content = None
        if not isinstance(parsed_content, Mapping):
            record_ai_call(
                module="commercial_analyzer", stage=stage, model=model,
                request_started_at=request_started_at,
                request_payload=body, response_payload=result, success=False,
                error_type="output_truncated",
            )
            raise AnalyzerError(
                f"Director {stage} 输出达到 {int(max_tokens)} token 上限，JSON 被截断"
            )
    if not content:
        record_ai_call(
            module="commercial_analyzer", stage=stage, model=model,
            request_started_at=request_started_at,
            request_payload=body, response_payload=result, success=False,
            error_type="empty_content",
        )
        raise AnalyzerError(f"Director {stage} 返回空内容")
    record_ai_call(
        module="commercial_analyzer", stage=stage, model=model,
        request_started_at=request_started_at,
        request_payload=body, response_payload=result, success=True,
    )
    return content


# ──────────────────────────────────────────────────────────────
# 解析 + 评分
# ──────────────────────────────────────────────────────────────

def _repair_json_leading_zero_integers(text: str) -> str:
    """Repair JSON-invalid integer padding outside quoted strings only.

    DeepSeek occasionally renders a subtitle ID such as ``99`` as ``099``.
    This is a formatting defect, not a semantic ambiguity.  The scanner never
    touches quoted text and only removes padding from a positive integer token
    that begins after a JSON delimiter.
    """
    source = str(text or "")
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        previous = source[index - 1] if index else ""
        if (
            char == "0"
            and index + 1 < len(source)
            and source[index + 1].isdigit()
            and (not previous or previous.isspace() or previous in "[,:{")
        ):
            end = index + 1
            while end < len(source) and source[end].isdigit():
                end += 1
            output.append(str(int(source[index:end])))
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _repair_json_trailing_commas(text: str) -> str:
    """Remove only commas before closing JSON containers, never quoted text."""
    out = []
    quoted = escaped = False
    for index, char in enumerate(text):
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        if char == ",":
            cursor = index + 1
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor < len(text) and text[cursor] in "}]":
                continue
        out.append(char)
    return "".join(out)


def _repair_json_relation_quote(text: str) -> str:
    """Close only a missing final quote in a line-based continuity note.

    The following line must close that object. Never repair IDs, beat data,
    missing containers or truncated output, and never change the note text.
    """
    return re.sub(
        r'(?m)^([ \t]*"relation"[ \t]*:[ \t]*"(?:[^"\\\r\n]|\\[^\r\n])*)(\r?\n)(?=[ \t]*\}[ \t]*,?[ \t]*\r?$)',
        r'\1"\2', text,
    )


# A provider occasionally emits a Chinese quotation mark as a literal JSON
# delimiter inside a short explanatory scalar, for example
# ``"opening_promise":"开头用一句"我穿这件..."的悬念"``.  This is a
# transport-format defect.  Restrict recovery to known non-executable prose
# fields on one physical line: IDs, products, containers and arbitrary fields
# are deliberately untouched.
_JSON_NARRATIVE_QUOTE_FIELDS = frozenset({
    "opening_promise", "selection_reason", "reason", "content_plan",
    "shortfall_reason", "missing_content", "stop_reason", "relation",
    "why_this_follows", "purchase_question", "purchase_outcome",
    "supports_main_product", "product_scope_check", "opening_payoff",
})


def _repair_json_narrative_quotes(text: str) -> str:
    repaired_lines: list[str] = []
    field_pattern = re.compile(
        r'(?P<prefix>(?:^|[,{])[ \t]*)"(?P<field>[A-Za-z_]+)"[ \t]*:[ \t]*"'
    )
    for line in str(text or "").splitlines(keepends=True):
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[:-len(newline)] if newline else line
        match = field_pattern.search(body)
        if not match or match.group("field") not in _JSON_NARRATIVE_QUOTE_FIELDS:
            repaired_lines.append(line)
            continue
        # The terminal scalar quote is the first quote that is followed by a
        # JSON value delimiter. Inner human quotation marks are followed by
        # prose, so they are unambiguous here even when the whole object is on
        # one line.
        closing = re.search(r'"(?=\s*(?:,\s*(?:"|$)|[}\]]))', body[match.end():])
        if closing is None:
            repaired_lines.append(line)
            continue
        suffix_start = match.end() + closing.start()
        value = body[match.end():suffix_start]
        output: list[str] = []
        backslashes = 0
        for char in value:
            if char == '"' and backslashes % 2 == 0:
                output.append('\\"')
            else:
                output.append(char)
            backslashes = backslashes + 1 if char == "\\" else 0
        repaired_lines.append(body[:match.end()] + "".join(output) + body[suffix_start:] + newline)
    return "".join(repaired_lines)


def _repair_json_terminal_strategy_role(text: str) -> str:
    """Repair DeepSeek's observed terminal single-strategy role misnesting.

    The real failing response ended a compact wire packet with:
    ``...}}],"director_plan_role":"primary"}}``.  That places the role after
    the strategies array and leaves one extra object close before the array.
    Repair only this exact terminal scalar form; all earlier strategy data and
    braces remain untouched.
    """
    return re.sub(
        r'\}\}(\],[ \t\r\n]*"director_plan_role"[ \t\r\n]*:[ \t\r\n]*"(?:primary|alternative)"[ \t\r\n]*\}\}[ \t\r\n]*)$',
        r'}\1',
        str(text or ""),
    )


def _repair_director_wire_missing_terminal_close(text: str) -> str:
    """Recover one missing root close in the observed compact Director wire.

    Seed returned a complete ``packet.strategies`` payload but omitted only the
    final close for the outer wire object. Restrict this to a declared Director
    wire ending in ``]}``; arbitrary partial JSON must remain a hard failure.
    """
    source = str(text or "")
    trailing = source[len(source.rstrip()):]
    body = source[:-len(trailing)] if trailing else source
    if (
        re.search(r'"schema_version"\s*:\s*"director-wire-v1"', body)
        and body.endswith("]}")
    ):
        return body + "}" + trailing
    return source


def _repair_json_completion_requirements_evidence_locations(text: str) -> str:
    """Repair the observed M1 array-close omission before evidence locations.

    DeepSeek returned a story chapter as
    ``"completion_requirements":["...","evidence_locations":[13,14]}``.
    The requirements value is contractually a string array, so this exact form
    is missing only its closing bracket before the next chapter field.
    """
    pattern = re.compile(
        r'(?P<requirements>"completion_requirements"[ \t\r\n]*:[ \t\r\n]*'
        r'\[(?:[ \t\r\n]*"(?:\\.|[^"\\])*")*)'
        r'(?P<separator>[ \t\r\n]*,[ \t\r\n]*)'
        r'(?P<evidence>"evidence_locations"[ \t\r\n]*:[ \t\r\n]*\[)'
    )
    return pattern.sub(r'\g<requirements>]\g<separator>\g<evidence>', str(text or ""))


def _escape_json_string_controls(text: str) -> str:
    """Escape literal controls only inside quoted values, preserving content."""
    output = []
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            elif ord(char) < 32:
                output.append(json.dumps(char)[1:-1])
                continue
        elif char == '"':
            quoted = True
        output.append(char)
    return "".join(output)


def _json_format_repairs(text: str) -> str:
    return _repair_json_trailing_commas(
        _repair_json_leading_zero_integers(
            _escape_json_string_controls(
                _repair_json_narrative_quotes(
                    _repair_json_relation_quote(
                        _repair_json_completion_requirements_evidence_locations(
                            _repair_director_wire_missing_terminal_close(
                                _repair_json_terminal_strategy_role(text)
                            )
                        )
                    )
                )
            )
        )
    )


def _expand_director_wire_if_needed(parsed: dict[str, Any], stats: dict[str, Any] | None = None) -> dict[str, Any]:
    if parsed.get("schema_version") != WIRE_VERSION:
        return parsed
    try:
        expanded = expand_director_wire_payload(parsed, stats=stats)
    except ValueError as exc:
        raise AnalyzerError(f"Director wire JSON 无法还原：{exc}") from exc
    misplaced_role = expanded.pop("director_plan_role", None)
    strategies = expanded.get("strategies")
    if (
        isinstance(misplaced_role, str)
        and misplaced_role in {"primary", "alternative"}
        and isinstance(strategies, list)
        and len(strategies) == 1
        and isinstance(strategies[0], dict)
        and "director_plan_role" not in strategies[0]
    ):
        strategies[0]["director_plan_role"] = misplaced_role
    elif misplaced_role is not None:
        expanded["director_plan_role"] = misplaced_role
    return expanded


def _extract_json(text: str, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return _expand_director_wire_if_needed(parsed, stats)
    except json.JSONDecodeError:
        pass
    repaired = _json_format_repairs(cleaned)
    if repaired != cleaned:
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return _expand_director_wire_if_needed(parsed, stats)
        except json.JSONDecodeError:
            pass
    start = cleaned.find("{")
    if start != -1:
        # 结尾多出杂散括号（如 ]}}）时，旧写法 rfind("}") 会把杂散里的 } 当成对象结尾，
        # 这类输入必然失败。改为取「第一个完整闭合的 JSON 对象」，多余尾巴自动忽略。
        try:
            parsed, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return _expand_director_wire_if_needed(parsed, stats)
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return _expand_director_wire_if_needed(parsed, stats)
        except json.JSONDecodeError:
            repaired = _json_format_repairs(candidate)
            if repaired != candidate:
                try:
                    parsed = json.loads(repaired)
                    if isinstance(parsed, dict):
                        return _expand_director_wire_if_needed(parsed, stats)
                except json.JSONDecodeError:
                    pass
    raise AnalyzerError("Analyzer 返回无法解析为 JSON")


def _director_json_parse_diagnostics(text: str) -> dict[str, Any]:
    """Return safe diagnostics for a malformed provider response.

    The fingerprint lets support correlate a paid request with its protected
    experiment artifact without exposing the model response in logs or UI.
    """
    raw = str(text or "")
    details: dict[str, Any] = {
        "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "response_chars": len(raw),
        "starts_with_fence": raw.lstrip().startswith("```"),
        "open_braces": raw.count("{"),
        "close_braces": raw.count("}"),
        "open_brackets": raw.count("["),
        "close_brackets": raw.count("]"),
    }
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        details.update({
            "json_error": str(exc.msg),
            "json_error_position": int(exc.pos),
            "json_error_line": int(exc.lineno),
            "json_error_column": int(exc.colno),
        })
    return details


def _parse_director_stage_json_with_recovery(
    raw: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    stage: str,
    system_prompt: str,
    max_tokens: int,
    timeout: int,
    log_fn: Callable[[str], None] | None = None,
    stage_response_hook: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Parse a Director stage, retrying only one malformed stage as JSON.

    The paid M1 and M2 calls are intentionally independent here: if M2 is
    malformed, the completed M1 contract stays frozen and is not sent through
    discovery again.  The recovery request is a format-only request; it may
    not create, remove, reorder, or reinterpret any facts or source IDs.
    """
    stage_stats: dict[str, Any] = {}
    try:
        parsed = _extract_json(raw, stage_stats)
        if log_fn and stage_stats.get("excluded_beats"):
            log_fn(
                f"AI 导演 {stage}：按「多商品排除」已剔除 "
                f"{stage_stats['excluded_beats']} 条商品引用越界的 beat，其余结果照常使用。"
            )
        return parsed
    except AnalyzerError as first_error:
        diagnostics = _director_json_parse_diagnostics(raw)
        diagnostics.update({
            "stage": str(stage),
            "recovery_attempted": True,
            "recovery_attempts": 1,
            "initial_error": type(first_error).__name__,
        })
        if log_fn:
            log_fn(
                f"AI 导演 {stage} 返回格式异常，正在仅恢复本阶段 JSON；"
                "不会重跑已完成的导演阶段。"
            )
        if stage_response_hook:
            stage_response_hook(
                f"{stage}_format_diagnostic",
                json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")),
            )
        # 先做免费的本地宽松修复；修得出来就不必再发一次付费的 AI 格式修复。
        local_recovered = _relaxed_json_repair(raw)
        if local_recovered is not None:
            diagnostics["recovery_status"] = "recovered_local"
            if stage_response_hook:
                stage_response_hook(
                    f"{stage}_format_recovered_local",
                    json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")),
                )
            if log_fn:
                log_fn(f"AI 导演 {stage} 的 JSON 已由本地宽松修复解析，跳过付费的 AI 格式修复。")
            # 与 _extract_json 成功路径一致：wire 载荷必须还原成旧结构再返回。
            if isinstance(local_recovered, dict):
                local_recovered = _expand_director_wire_if_needed(local_recovered, stage_stats)
            return local_recovered
        repair_prompt = "\n\n".join([
            "你的上一条回复未能被严格 JSON 解析。请只做格式修复。",
            "必须只返回一个有效 JSON 对象，不要 Markdown、解释或代码围栏。",
            "每个字段、每个数组元素各占一行（不必缩进对齐，但必须有换行）——"
            "按行组织结构才不会漏括号或多括号。",
            "不得新增、删除、改写、重排任何事实、章节、商品、字幕 ID、数组项或字段值；"
            "仅修复引号、逗号、转义、括号等 JSON 语法。",
            "上一条原文如下：",
            str(raw),
        ])
        try:
            recovered_raw = _post_two_pass_director_request(
                api_key=api_key,
                base_url=base_url,
                model=model,
                system_prompt=system_prompt,
                user_prompt=repair_prompt,
                stage=f"{stage}_json_repair",
                max_tokens=max_tokens,
                timeout=timeout,
                response_hook=(
                    lambda value: stage_response_hook(f"{stage}_json_repair", value)
                ) if stage_response_hook else None,
            )
            parsed = _extract_json(recovered_raw)
        except (AnalyzerError, ValueError, TypeError, KeyError) as recovery_error:
            diagnostics.update({
                "recovery_status": "failed",
                "recovery_error": type(recovery_error).__name__,
            })
            if stage_response_hook:
                stage_response_hook(
                    f"{stage}_format_recovery_failed",
                    json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")),
                )
            raise DirectorStageFormatError(stage, diagnostics) from recovery_error
        diagnostics["recovery_status"] = "recovered"
        if stage_response_hook:
            stage_response_hook(
                f"{stage}_format_recovered",
                json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")),
            )
        if log_fn:
            log_fn(f"AI 导演 {stage} 的 JSON 格式已自动恢复；继续使用同一阶段结果。")
        return parsed


def _json_bracket_depths(text: str) -> tuple[int, int, bool]:
    """扫描一遍，返回 (花括号净深, 方括号净深, 是否停在未闭合的字符串里)。

    只看结构字符，字符串内部一律跳过（含转义）。
    """
    depth_brace = depth_bracket = 0
    in_string = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace -= 1
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket -= 1
    return depth_brace, depth_bracket, in_string


def _balance_json_bracket_candidates(text: str) -> list[str]:
    """括号平衡候选（纯机械，不新增也不改写任何内容字符）。

    实测（2026-10-08，43 份真实失败的导演响应）：本条之前的链路只救回 2 份（4.7%）。
    失败形态很集中——15 份的报错位置距响应末尾仅 3 个字符、末尾形如 ``}]}}``（多出的
    闭合括号），且括号计数普遍不平衡（165/167、150/153、92/95 是多闭合，8/5 是少闭合）。

    这里只做两件机械的事：

    ① **末尾多余的闭合括号去掉**：只有当末尾一段确实只由闭合括号与空白组成时才剥，
       绝不切掉任何内容字符（这是与"盲目截断末尾 N 字符"的关键区别）。
    ② **缺失的闭合括号补齐**：尾部被截断时，按两种顺序各试一次。

    停在未闭合字符串里、或结构错在中途（末尾不是括号却有多余闭合）时返回空列表——
    那种情况机械办法救不了，照旧走付费修复。
    """
    candidate = str(text or "").strip()
    if not candidate:
        return []
    depth_brace, depth_bracket, in_string = _json_bracket_depths(candidate)
    if in_string:
        return []
    if depth_brace == 0 and depth_bracket == 0:
        # 本来就平衡：严格解析失败另有原因（例如校验不通过），括号平衡帮不上忙。
        return []
    out: list[str] = []
    work = candidate
    if depth_brace < 0 or depth_bracket < 0:
        # ① 末尾多出闭合括号：从后往前逐个剥，直到净深都非负。
        #    只有末尾确实是闭合括号/空白时才剥，绝不切掉内容字符。
        while work and (depth_brace < 0 or depth_bracket < 0):
            last = work[-1]
            if last in "}]" or last.isspace():
                work = work[:-1]
            else:
                return []
            depth_brace, depth_bracket, in_string = _json_bracket_depths(work)
            if in_string:
                return []
        if depth_brace == 0 and depth_bracket == 0:
            out.append(work)
            return out
        # 剥完之后可能反而变成「缺闭合」——所以下面要继续补，两者组合才是完整修复。
    if depth_brace >= 0 and depth_bracket >= 0:
        # ② 缺闭合括号：两种补齐顺序都试（数组通常嵌在对象里，先 ] 后 } 更常见）。
        out.append(work + ("]" * depth_bracket) + ("}" * depth_brace))
        if depth_brace and depth_bracket:
            out.append(work + ("}" * depth_brace) + ("]" * depth_bracket))
    return out


def _relaxed_json_repair(raw: Any) -> Any | None:
    """本地宽松修复 JSON（免费）：去代码围栏、截取最外层对象、修尾逗号与中文引号。

    返回可解析对象；本地修不出来返回 None（此时才值得再发一次付费的 AI 格式修复）。"""
    text = str(raw or "")
    if not text.strip():
        return None
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        variants = (
            candidate,
            re.sub(r",\s*([}\]]) *", r"\1", candidate),
            candidate.replace("\u201c", '"').replace("\u201d", '"'),
        )
        for variant in variants:
            try:
                return json.loads(variant, strict=False)
            except Exception:  # noqa: BLE001 - 逐个变体尝试
                continue
    # 兜底：取「第一个完整闭合的 JSON 对象」，忽略结尾多出的杂散字符（如 ]}}）。
    # 旧写法用 rfind("}") 在这种输入上会切错，导致免费修复失效、被迫走付费修复。
    decoder = json.JSONDecoder()
    for candidate in candidates:
        brace = candidate.find("{")
        if brace < 0:
            continue
        try:
            obj, _ = decoder.raw_decode(candidate[brace:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    # 最后兜底：括号平衡（见 _balance_json_bracket_candidates 的实测说明）。
    # 解析不出来就照旧返回 None 走付费修复，绝不返回半成品。
    # 要求结果是非空对象：否则像 "{"/"{{" 这种碎片会被补成 "{}" 并当成成功结果静默返回，
    # 那比直接报格式错更糟。
    for candidate in candidates:
        for balanced in _balance_json_bracket_candidates(candidate):
            for variant in (balanced, re.sub(r",\s*([}\]])", r"\1", balanced)):
                try:
                    obj = json.loads(variant, strict=False)
                except Exception:  # noqa: BLE001 - 逐个变体尝试
                    continue
                if isinstance(obj, dict) and obj:
                    return obj
    return None

def _subtitle_duration_map(subtitles: Sequence[Mapping[str, Any]]) -> dict[int, float]:
    result: dict[int, float] = {}
    for i, sub in enumerate(subtitles, 1):
        sid = int(sub.get("id") or sub.get("index") or i)
        try:
            start = float(sub.get("start") or 0)
            end = float(sub.get("end") or start)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        result[sid] = max(0.0, end - start)
    return result


def _asset_permissions(commercial_assets: Sequence[Mapping[str, Any]] | None) -> dict[int, str]:
    permissions: dict[int, str] = {}
    for raw in commercial_assets or ():
        if not isinstance(raw, Mapping):
            continue
        try:
            candidate_id = int(raw.get("candidate_id") or raw.get("srt_index") or 0)
        except (TypeError, ValueError):
            continue
        permission = str(raw.get("story_permission") or "").strip().lower()
        if candidate_id > 0 and permission in {"main_story", "supporting_story", "unavailable"}:
            permissions[candidate_id] = permission
    return permissions


def _audit_strategy_asset_usage(
    strategy: Strategy,
    permissions: Mapping[int, str],
) -> Strategy:
    """Remove model-cited evidence outside the supplied asset boundary.

    This is deliberately an audit, not a story repair: it does not substitute
    another subtitle, change an evidence claim, or promote a supporting asset.
    """

    if not permissions:
        return strategy
    reasons = list(strategy.excluded_assets_reason)

    def allowed(items: Sequence[EvidenceItem], *, tier: str) -> tuple[EvidenceItem, ...]:
        kept: list[EvidenceItem] = []
        for item in items:
            missing = [sid for sid in item.subtitle_ids if sid not in permissions]
            unavailable = [sid for sid in item.subtitle_ids if permissions.get(sid) == "unavailable"]
            if missing or unavailable:
                detail = []
                if missing:
                    detail.append("not_in_hard_safe_ledger=" + ",".join(str(sid) for sid in missing))
                if unavailable:
                    detail.append("unavailable=" + ",".join(str(sid) for sid in unavailable))
                reasons.append(f"{tier}:{item.role}:" + ";".join(detail))
                continue
            kept.append(item)
        return tuple(kept)

    core = allowed(strategy.core_evidence_pool, tier="core")
    supporting = allowed(strategy.supporting_evidence_pool, tier="supporting")
    bridge = allowed(strategy.bridge_candidates, tier="bridge")
    return replace(
        strategy,
        core_evidence_pool=core,
        supporting_evidence_pool=supporting,
        bridge_candidates=bridge,
        evidence=tuple((*core, *supporting, *bridge)),
        excluded_assets_reason=tuple(dict.fromkeys(reasons)),
    )


def parse_strategy_result(
    raw_text: str,
    *,
    product: str = "",
    subtitles: Sequence[Mapping[str, Any]] = (),
    target_duration: float = 45.0,
    content_contract: Mapping[str, Any] | None = None,
    commercial_assets: Sequence[Mapping[str, Any]] | None = None,
) -> "StrategyDiscoveryResult":
    data = _extract_json(raw_text)
    strategies_raw = data.get("strategies") or ()
    if isinstance(strategies_raw, Mapping):
        strategies_raw = (strategies_raw,)

    duration_map = _subtitle_duration_map(subtitles)
    text_map = {
        int(sub.get("id") or sub.get("index") or position): str(sub.get("text") or "")
        for position, sub in enumerate(subtitles, 1)
    }
    asset_permissions = _asset_permissions(commercial_assets)
    strategies: list[Strategy] = []
    for index, item in enumerate(strategies_raw, 1):
        if not isinstance(item, Mapping):
            continue
        strategy = Strategy.from_dict(item, index)
        strategy = _audit_strategy_asset_usage(strategy, asset_permissions)
        story = compute_story_strength(strategy.evidence, strategy.missing_roles)
        material = compute_material_sufficiency(strategy.evidence, duration_map, target_duration)
        # 依赖识别与合同执行是两层：无合同也必须保留依赖，不得把它误判为 block。
        core_assets = strategy.core_evidence_pool or strategy.evidence
        dependencies = detect_content_dependencies(
            core_assets,
            text_map,
            strategy.content_dependencies,
        )
        hard_blocked, audit_hits = hard_audit_blocked_types(strategy.evidence, text_map, content_contract)
        blocked_kinds = _blocked_kinds(content_contract)
        merged_blocked = tuple(sorted((set(dependencies) & blocked_kinds) | set(hard_blocked)))
        compat, viability = compute_contract_compatibility(merged_blocked)
        evidence_duration = compute_evidence_duration(strategy.evidence, duration_map)
        duration_feasibility, recommended_seconds = compute_duration_feasibility(
            evidence_duration,
            target_duration,
        )
        story_validity = compute_story_validity(strategy)
        strategies.append(Strategy(
            strategy_id=strategy.strategy_id,
            type=strategy.type,
            strategy_family=strategy.strategy_family,
            sub_angle=strategy.sub_angle,
            thesis=strategy.thesis,
            story_premise=strategy.story_premise,
            audience_tension=strategy.audience_tension,
            story_trigger=strategy.story_trigger,
            transformation=strategy.transformation,
            product_role=strategy.product_role,
            core_commercial_idea=strategy.core_commercial_idea,
            payoff=strategy.payoff,
            supporting_arcs=strategy.supporting_arcs,
            inference_notes=strategy.inference_notes,
            content_dependencies=dependencies,
            core_evidence_pool=strategy.core_evidence_pool,
            supporting_evidence_pool=strategy.supporting_evidence_pool,
            bridge_candidates=strategy.bridge_candidates,
            target_user=strategy.target_user,
            evidence=strategy.evidence,
            missing_roles=strategy.missing_roles,
            blocked_evidence_types=merged_blocked,
            contract_audit_hits=audit_hits,
            coherence_reason=strategy.coherence_reason,
            distinctiveness=strategy.distinctiveness,
            story_strength=story,
            material_sufficiency=material,
            contract_compatibility=compat,
            strategy_viability=viability,
            story_validity=story_validity,
            duration_feasibility=duration_feasibility,
            recommended_duration_seconds=recommended_seconds,
            target_duration_seconds=round(float(target_duration or 0.0), 1),
            excluded_assets_reason=strategy.excluded_assets_reason,
            story_priority=strategy.story_priority,
            director_title=strategy.director_title,
            core_desire=strategy.core_desire,
            opening_promise=strategy.opening_promise,
            opening_evidence_packages=tuple(
                dict(item) for item in strategy.opening_evidence_packages
            ),
            director_quality_tier=strategy.director_quality_tier,
            director_plan_role=strategy.director_plan_role,
            director_sequence=tuple(strategy.director_sequence),
            video_structure_id=strategy.video_structure_id,
            video_structure_name=strategy.video_structure_name,
            video_structure_reason=strategy.video_structure_reason,
            director_chapter_packets=tuple(strategy.director_chapter_packets),
            director_opening_alternatives=tuple(strategy.director_opening_alternatives),
            whole_video_audit=dict(strategy.whole_video_audit or {}),
            director_readthrough=strategy.director_readthrough,
            narrative_archetype=strategy.narrative_archetype,
            opening_scope=dict(strategy.opening_scope or {}),
            product_scope=dict(strategy.product_scope or {}),
            opening_selection=dict(strategy.opening_selection or {}),
        ))

    return StrategyDiscoveryResult(
        product=str(data.get("product") or product or "").strip(),
        strategies=tuple(strategies),
    )


@dataclass(frozen=True)
class StrategyDiscoveryResult:
    product: str
    strategies: tuple[Strategy, ...]

    @property
    def distinct_strategy_ids(self) -> tuple[str, ...]:
        return tuple(item.strategy_id for item in self.strategies)

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "strategies": [item.to_dict() for item in self.strategies],
        }


_CASTING_CHAPTER_REVISION_FIELDS = (
    "title", "buyer_advance", "chapter_job", "completion_requirements",
)


def _chapter_has_selected_beats(chapter: Mapping[str, Any]) -> bool:
    beats = chapter.get("beats") or ()
    if isinstance(beats, Mapping):
        beats = (beats,)
    return any(
        isinstance(beat, Mapping) and bool(beat.get("subtitle_ids") or beat.get("ids"))
        for beat in beats
    )


def _chapter_revision_semantics(
    revision: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Read a small AI-authored final-chapter revision without inventing text."""
    values: dict[str, Any] = {}
    errors: list[str] = []
    for key in ("title", "buyer_advance", "chapter_job"):
        if key not in revision:
            continue
        value = revision.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"chapter_revision_{key}_invalid")
        else:
            values[key] = value.strip()
    if "completion_requirements" in revision:
        requirements = revision.get("completion_requirements")
        if (not isinstance(requirements, list)
                or not requirements
                or any(not isinstance(item, str) or not item.strip() for item in requirements)):
            errors.append("chapter_revision_completion_requirements_invalid")
        else:
            values["completion_requirements"] = [item.strip() for item in requirements]
    return values, errors


def _apply_casting_chapter_revisions(
    story_payload: Mapping[str, Any], casting_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Apply only AI-declared final chapter refinements from the same Casting call.

    Call one still owns the product and commercial direction.  Call two may
    remove a non-progressing chapter, merge consecutive overlapping chapters,
    or narrow their promises so its selected speech is truthful.  This function
    validates the mechanical shape of that handoff; it never chooses a chapter,
    a source sentence, or replacement wording.
    """
    effective_story = copy.deepcopy(dict(story_payload))
    effective_cast = copy.deepcopy(dict(casting_payload))
    story_rows = effective_story.get("strategies") or ()
    cast_rows = effective_cast.get("strategies") or ()
    if isinstance(story_rows, Mapping):
        story_rows = (story_rows,)
    if isinstance(cast_rows, Mapping):
        cast_rows = (cast_rows,)
    cast_by_strategy = {
        str(item.get("strategy_id") or f"S{index}"): item
        for index, item in enumerate(cast_rows, 1)
        if isinstance(item, dict)
    }
    audits: list[dict[str, Any]] = []

    for strategy_index, strategy in enumerate(story_rows, 1):
        if not isinstance(strategy, dict):
            continue
        strategy_id = str(strategy.get("strategy_id") or f"S{strategy_index}")
        cast_strategy = cast_by_strategy.get(strategy_id)
        if not isinstance(cast_strategy, dict):
            continue
        raw_story_chapters = strategy.get("chapter_packets") or ()
        raw_cast_chapters = cast_strategy.get("chapter_packets") or ()
        if isinstance(raw_story_chapters, Mapping):
            raw_story_chapters = (raw_story_chapters,)
        if isinstance(raw_cast_chapters, Mapping):
            raw_cast_chapters = (raw_cast_chapters,)
        story_chapters = [item for item in raw_story_chapters if isinstance(item, dict)]
        cast_chapters = [item for item in raw_cast_chapters if isinstance(item, dict)]
        chapter_ids = [str(item.get("chapter_id") or f"C{index}").strip()
                       for index, item in enumerate(story_chapters, 1)]
        chapter_index = {chapter_id: index for index, chapter_id in enumerate(chapter_ids)}
        casts_by_chapter_id = {
            str(item.get("chapter_id") or f"C{index}").strip(): item
            for index, item in enumerate(cast_chapters, 1)
        }
        revisions: list[tuple[dict[str, Any], dict[str, Any], str, list[str], dict[str, Any]]] = []
        revision_packets: list[dict[str, Any]] = []
        errors: list[str] = []
        # P0-A（2026-10-01 David 定）：被自动纠偏过的修订（原 action 语义成立、
        # 只是标签写错）。只做留痕，不改任何判定标准。
        coerced_actions: list[dict[str, Any]] = []
        claimed_sources: set[str] = set()
        seen_revision_chapters: set[str] = set()
        for cast_index, cast_chapter in enumerate(cast_chapters, 1):
            raw_revision = cast_chapter.get("chapter_revision")
            if not isinstance(raw_revision, Mapping):
                continue
            revision_packets.append(cast_chapter)
            revision = dict(raw_revision)
            action = str(revision.get("action") or "").strip().lower()
            if action in {"", "keep"}:
                cast_chapter.pop("chapter_revision", None)
                continue
            chapter_id = str(cast_chapter.get("chapter_id") or f"C{cast_index}").strip()
            label = f"{strategy_id}/{chapter_id}"
            if chapter_id in seen_revision_chapters:
                errors.append(f"{label}:chapter_revision_duplicate")
                continue
            seen_revision_chapters.add(chapter_id)
            raw_sources = revision.get("source_chapter_ids")
            if not isinstance(raw_sources, list) or not raw_sources:
                errors.append(f"{label}:chapter_revision_source_ids_missing")
                continue
            source_ids = [str(value).strip() for value in raw_sources]
            if (any(not value or value not in chapter_index for value in source_ids)
                    or len(set(source_ids)) != len(source_ids)):
                errors.append(f"{label}:chapter_revision_source_ids_invalid")
                continue
            semantic_values, semantic_errors = _chapter_revision_semantics(revision)
            if semantic_errors:
                errors.extend(f"{label}:{error}" for error in semantic_errors)
                continue
            # P0-A（2026-10-01 David 定）：修订 action 自动纠偏。
            # 背景（重启后预览审计实测）：AI 把「合并相邻章后重做」写成
            # action=refine + source_chapter_ids=["C1","C2"]，原实现只认 refine 的
            # 单章形态 → 整条判无效丢弃，需要重建的章失去唯一自救机会
            # （实测混剪 B 的 C1 只剩 1 拍 0.96s）。这里只在语义完整、且 source 是
            # 「以本章为首的连续多章」时按 merge 处理：确定性纠偏，零新增 AI 调用。
            # 纠偏后仍要过后续全部校验（如 merged_source_has_beats），不放宽任何闸门。
            if action == "refine" and source_ids != [chapter_id] and semantic_values:
                _coerce_positions = [chapter_index[value] for value in source_ids]
                if (
                    len(source_ids) >= 2
                    and source_ids[0] == chapter_id
                    and _coerce_positions == list(range(
                        _coerce_positions[0], _coerce_positions[0] + len(_coerce_positions)
                    ))
                ):
                    coerced_actions.append({
                        "chapter_id": chapter_id,
                        "from_action": "refine",
                        "to_action": "merge",
                        "source_chapter_ids": list(source_ids),
                    })
                    action = "merge"

            if action == "drop":
                if source_ids != [chapter_id] or _chapter_has_selected_beats(cast_chapter):
                    errors.append(f"{label}:chapter_revision_drop_invalid")
                    continue
            elif action == "refine":
                if source_ids != [chapter_id] or not semantic_values:
                    errors.append(f"{label}:chapter_revision_refine_invalid")
                    continue
            elif action == "merge":
                source_positions = [chapter_index[source_id] for source_id in source_ids]
                if (len(source_ids) < 2 or source_ids[0] != chapter_id
                        or source_positions != list(range(source_positions[0], source_positions[0] + len(source_positions)))
                        or not semantic_values):
                    errors.append(f"{label}:chapter_revision_merge_invalid")
                    continue
            else:
                errors.append(f"{label}:chapter_revision_action_invalid")
                continue
            if claimed_sources & set(source_ids):
                errors.append(f"{label}:chapter_revision_sources_reused")
                continue
            claimed_sources.update(source_ids)
            revisions.append((cast_chapter, revision, action, source_ids, semantic_values))

        for _cast_chapter, _revision, action, source_ids, _values in revisions:
            if action != "merge":
                continue
            for merged_id in source_ids[1:]:
                other = casts_by_chapter_id.get(merged_id)
                if other is not None and _chapter_has_selected_beats(other):
                    errors.append(f"{strategy_id}/{source_ids[0]}:merged_source_has_beats:{merged_id}")

        visible_cast_ids: list[str] = []
        for cast_index, cast_chapter in enumerate(cast_chapters, 1):
            chapter_id = str(cast_chapter.get("chapter_id") or f"C{cast_index}").strip()
            revision = cast_chapter.get("chapter_revision")
            if isinstance(revision, Mapping) and str(revision.get("action") or "").strip().lower() == "drop":
                continue
            if chapter_id in chapter_index:
                visible_cast_ids.append(chapter_id)
        if visible_cast_ids and [chapter_index[item] for item in visible_cast_ids] != sorted(chapter_index[item] for item in visible_cast_ids):
            errors.append(f"{strategy_id}:chapter_revision_changed_order")

        if errors:
            for cast_chapter in revision_packets:
                cast_chapter.pop("chapter_revision", None)
            audits.append({
                "strategy_id": strategy_id, "status": "ignored_invalid",
                "issues": errors, "coerced": coerced_actions,
            })
            continue
        if not revisions:
            audits.append({"strategy_id": strategy_id, "status": "unchanged", "applied": []})
            continue

        revision_by_survivor = {
            source_ids[0]: (cast_chapter, revision, action, source_ids, values)
            for cast_chapter, revision, action, source_ids, values in revisions
        }
        removed_ids = {
            source_id
            for _cast_chapter, _revision, action, source_ids, _values in revisions
            for source_id in (source_ids if action == "drop" else source_ids[1:])
        }
        final_chapters: list[dict[str, Any]] = []
        for index, story_chapter in enumerate(story_chapters, 1):
            chapter_id = str(story_chapter.get("chapter_id") or f"C{index}").strip()
            if chapter_id in removed_ids:
                continue
            final_chapter = copy.deepcopy(story_chapter)
            applied = revision_by_survivor.get(chapter_id)
            if applied is not None:
                _cast_chapter, revision, action, source_ids, values = applied
                final_chapter.update(values)
                final_chapter["cast_chapter_revision"] = {
                    "action": action,
                    "source_chapter_ids": source_ids,
                    "reason": str(revision.get("reason") or "").strip(),
                }
            final_chapters.append(final_chapter)
        strategy["chapter_packets"] = final_chapters
        audits.append({
            "strategy_id": strategy_id,
            "status": "applied",
            "coerced": coerced_actions,
            "applied": [
                {
                    "chapter_id": source_ids[0], "action": action,
                    "source_chapter_ids": source_ids,
                }
                for _cast_chapter, _revision, action, source_ids, _values in revisions
            ],
            "removed_chapter_ids": sorted(removed_ids, key=chapter_index.get),
        })
    return effective_story, effective_cast, {"strategies": audits}


def _opening_leadin_veto(
    opening: Any,
    chapter_beats: Sequence[Mapping[str, Any]] | None,
    casting_rows: Sequence[Mapping[str, Any]] = (),
    used_ids: set[int] | None = None,
    hook_pool: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministically replace a dangling-lead-in opening Hook (receipt only).

    Every one of these must hold, otherwise nothing at all changes:
      * the AI-selected package's Hook is one sentence that starts with a
        dependency conjunction (the same list the chapter-head fill uses);
      * the sentence right before it being present in the pool does NOT make
        this gate back off: the chapter-head fill only runs for chapters after
        the first (``chapter_index > 1``), and this gate is only ever called on
        chapter 1 / the opening, so nobody else would repair it;
      * a replacement Hook exists that is unused elsewhere in the plan and of
        sane spoken length.  Sources, in order: first any ``alternative`` package
        the same AI response returned; if none qualifies, the recall-pool Hook
        that the pool itself ranks *ahead* of the current Hook (``hook_pool``
        arrives strength-desc / rank-asc, i.e. the pool's own preference order),
        and never one the pool ranks below or level with it.  Whenever a
        ``hook_pool`` is supplied, the same bar holds for the ``alternative``
        source too: the replacement must itself be a pool candidate ranked
        *ahead* of the current Hook.
    Only the first beat of the first chapter (the Hook beat) is swapped. The
    payoff beat, the chapter order and every other chapter stay byte-identical.
    """
    receipt: dict[str, Any] = {
        "enabled": bool(OPENING_LEADIN_VETO_ENABLED),
        "applied": False,
        "status": "skipped",
        "old_hook_ids": [],
        "new_hook_ids": [],
        "promoted_package_rank": 0,
        "promoted_from": "",
    }
    if not OPENING_LEADIN_VETO_ENABLED or not isinstance(opening, Mapping) or not chapter_beats:
        return receipt
    try:
        packages = opening.get("compared_packages")
        if not isinstance(packages, (list, tuple)) or not packages:
            return receipt
        by_id: dict[int, Mapping[str, Any]] = {}
        for row in casting_rows or ():
            if not isinstance(row, Mapping):
                continue
            try:
                row_id = int(row.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if row_id > 0:
                by_id[row_id] = row

        def _hook_ids(package: Any) -> list[int]:
            values = package.get("subtitle_ids") if isinstance(package, Mapping) else ()
            ids: list[int] = []
            for value in values or ():
                try:
                    subtitle_id = int(value)
                except (TypeError, ValueError):
                    continue
                if subtitle_id > 0 and subtitle_id not in ids:
                    ids.append(subtitle_id)
            return ids

        def _text(subtitle_id: int) -> str:
            row = by_id.get(subtitle_id)
            return str(row.get("text") or "").strip() if isinstance(row, Mapping) else ""

        def _starts_with_prefix(text: str, prefixes: tuple) -> bool:
            return bool(text) and any(text.startswith(prefix) for prefix in prefixes)

        def _dangling(subtitle_id: int) -> bool:
            return _starts_with_prefix(_text(subtitle_id), OPENING_LEADIN_VETO_PREFIXES)

        def _replacement_blocked(subtitle_id: int) -> bool:
            return _starts_with_prefix(_text(subtitle_id), OPENING_REPLACEMENT_BLOCKED_PREFIXES)

        def _pool_hook_order(rows: Sequence[Mapping[str, Any]] | None) -> list[int]:
            # 池行（hook_pool_prompt_rows）已按「强度降序、排名升序」排好，
            # 这个顺序就是池自己的偏好顺序 —— B 只认它，不重新排序。
            order: list[int] = []
            for row in rows or ():
                if not isinstance(row, Mapping):
                    continue
                ids_in_row = _hook_ids(row)
                if len(ids_in_row) == 1:
                    order.append(ids_in_row[0])
            return order

        def _sane_seconds(subtitle_id: int) -> bool:
            row = by_id.get(subtitle_id)
            if not isinstance(row, Mapping):
                return False
            try:
                start = float(row.get("start"))
                end = float(row.get("end"))
            except (TypeError, ValueError):
                return True  # 池里没有时长信息时不拦
            return 1.0 <= (end - start) <= 10.0

        selected = None
        for package in packages:
            if isinstance(package, Mapping) and str(package.get("decision") or "").strip() == "selected":
                selected = package
                break
        if selected is None:
            return receipt
        hook_ids = _hook_ids(selected)
        if len(hook_ids) != 1 or hook_ids[0] not in by_id or not _dangling(hook_ids[0]):
            return receipt
        receipt["status"] = "hook_is_dangling_leadin"
        receipt["old_hook_ids"] = list(hook_ids)
        if hook_ids[0] - 1 in by_id:
            # [A · 2026-10-05] 上文在池里，但**不再因此放手**：章首补行只作用于
            # 第 2 章及以后（见 CAST_CHAPTER_HEAD_FILL_ENABLED 分支的
            # chapter_index > 1），而本硬门只被第 1 章（开场）调用 —— 开场没人来补，
            # 放手的唯一结果就是这条开场永远不修。继续往下走，交给换包。
            receipt["status"] = "leadin_fillable_not_applicable_to_opening"
        used = set(int(value) for value in (used_ids or ()))
        used.discard(hook_ids[0])
        # [O1 · 2026-10-05] 把 B 的「信池排名」护栏也接到 AI 备选这条路上：
        # 只要这次带了召回池，备选就必须同为池内候选、且池把它排在当前钩子前面。
        pool_order = _pool_hook_order(hook_pool) if hook_pool else []
        current_pool_index: int | None = None
        if pool_order:
            try:
                current_pool_index = pool_order.index(hook_ids[0])
            except ValueError:
                current_pool_index = None
        candidate = None
        for rank, package in enumerate(packages, 1):
            if package is selected or not isinstance(package, Mapping):
                continue
            if str(package.get("decision") or "").strip() != "alternative":
                continue
            ids = _hook_ids(package)
            if len(ids) != 1:
                continue
            if ids[0] in used or ids[0] not in by_id:
                continue
            if _replacement_blocked(ids[0]) or not _sane_seconds(ids[0]):
                continue
            if pool_order:
                if ids[0] not in pool_order:
                    continue
                if current_pool_index is not None and pool_order.index(ids[0]) >= current_pool_index:
                    # 池自己把当前钩子排在这个备选前面 —— 不越级。
                    continue
            candidate = package
            receipt["promoted_package_rank"] = rank
            receipt["promoted_from"] = "ai_alternative"
            break
        if candidate is None and pool_order:
            # [B · 2026-10-05] AI 的备选包没有可用的，就按池自己的偏好顺序找。
            for pool_index, pool_hook_id in enumerate(pool_order):
                if current_pool_index is not None and pool_index >= current_pool_index:
                    # 池自己把当前钩子排在这个候选前面 —— 「信池排名」就不该越级顶替。
                    break
                if pool_hook_id == hook_ids[0] or pool_hook_id in used or pool_hook_id not in by_id:
                    continue
                if _replacement_blocked(pool_hook_id) or not _sane_seconds(pool_hook_id):
                    continue
                candidate = {
                    "subtitle_ids": [pool_hook_id],
                    "decision": "alternative",
                    "reason": "recall_pool_promotion",
                }
                receipt["promoted_package_rank"] = pool_index + 1
                receipt["promoted_from"] = "recall_pool"
                break
        if candidate is None:
            receipt["status"] = "no_clean_alternative"
            return receipt
        new_hook = _hook_ids(candidate)[0]
        first_beat = chapter_beats[0]
        if not isinstance(first_beat, dict):
            receipt["status"] = "first_beat_not_mutable"
            return receipt
        first_beat["subtitle_ids"] = [new_hook]
        first_beat.pop("source_span", None)
        first_beat.pop("subtitle_span", None)
        promoted = dict(candidate)
        promoted["decision"] = "selected"
        if "product_evidence_ids" not in promoted and isinstance(selected, Mapping):
            # 池晋升的包本来不在 compared_packages 里，产品证据沿用原选中包的记录，
            # 章节其余部分未动，不重新发明证据。
            promoted["product_evidence_ids"] = list(selected.get("product_evidence_ids") or [])
        demoted = dict(selected)
        demoted["decision"] = "alternative"
        # 只把「选中包」与「备用包」对调位置，其余包相对次序一个不动。
        reordered: list[Any] = []
        placed_demoted = False
        for package in packages:
            if package is selected:
                reordered.append(promoted)
            elif package is candidate:
                reordered.append(demoted)
                placed_demoted = True
            else:
                reordered.append(dict(package) if isinstance(package, Mapping) else package)
        if not placed_demoted:
            # 候选来自召回池、本来不在 compared_packages 里：把原选中包留档在末尾。
            reordered.append(demoted)
        payoff_ids: list[int] = []
        if len(chapter_beats) > 1 and isinstance(chapter_beats[1], Mapping):
            payoff_ids = [int(value) for value in DirectorBeat.from_dict(chapter_beats[1], 2).subtitle_ids]
        promoted["payoff_subtitle_ids"] = payoff_ids
        promoted["program_note"] = (
            "opening_leadin_veto: 钩子由 %s 换成 %s（来源=%s）；兑现拍保持原章开头，未改写任何原文"
            % (hook_ids, [new_hook], receipt.get("promoted_from") or "ai_alternative")
        )
        opening["compared_packages"] = reordered
        opening["selected_subtitle_ids"] = [new_hook] + payoff_ids
        receipt.update({
            "applied": True,
            "status": "swapped",
            "new_hook_ids": [new_hook],
        })
    except Exception as exc:  # noqa: BLE001 - 硬门自身绝不能拖垮预览
        receipt["applied"] = False
        receipt["status"] = "veto_error:%s" % type(exc).__name__
    return receipt


def _normalize_two_pass_director_payload(
    story_payload: Mapping[str, Any],
    casting_payload: Mapping[str, Any],
    *,
    casting_rows: Sequence[Mapping[str, Any]] = (),
    product_context_rows: Sequence[Mapping[str, Any]] = (),
    opening_hook_pool: Sequence[Mapping[str, Any]] | None = None,
    _single_strategy: bool = False,
) -> dict[str, Any]:
    """Hydrate the compact Casting receipt without making semantic choices.

    Call one remains authoritative for the product and commercial direction.
    Call two owns exact selected ID order and may carry a validated final
    chapter revision from the same response.  The program only joins those
    AI-authored records and restores verbatim readthrough text from source IDs.
    """
    if not _single_strategy:
        raw_story_strategies = story_payload.get("strategies") or ()
        raw_cast_strategies = casting_payload.get("strategies") or ()
        if isinstance(raw_story_strategies, Mapping):
            raw_story_strategies = (raw_story_strategies,)
        if isinstance(raw_cast_strategies, Mapping):
            raw_cast_strategies = (raw_cast_strategies,)
        cast_by_id = {
            str(item.get("strategy_id") or f"S{index}"): dict(item)
            for index, item in enumerate(raw_cast_strategies, 1)
            if isinstance(item, Mapping) and list(item.get("chapter_packets") or [])
        }
        executable_story_rows = [
            (index, dict(item))
            for index, item in enumerate(raw_story_strategies, 1)
            if isinstance(item, Mapping)
            and list(item.get("chapter_packets") or [])
            and str(item.get("strategy_id") or f"S{index}") in cast_by_id
        ]
        if len(executable_story_rows) > 1:
            normalized_strategies: list[dict[str, Any]] = []
            executed_ids: set[str] = set()
            for output_index, (source_index, story_item) in enumerate(executable_story_rows, 1):
                strategy_id = str(story_item.get("strategy_id") or f"S{source_index}")
                cast_item = cast_by_id[strategy_id]
                normalized = _normalize_two_pass_director_payload(
                    {"strategies": [story_item]},
                    {"strategies": [cast_item]},
                    casting_rows=casting_rows,
                    product_context_rows=product_context_rows,
                    opening_hook_pool=opening_hook_pool,
                    _single_strategy=True,
                )["strategies"][0]
                normalized["strategy_id"] = strategy_id
                normalized["director_plan_role"] = "primary" if output_index == 1 else "alternative"
                normalized_strategies.append(normalized)
                executed_ids.add(strategy_id)
            for source_index, raw in enumerate(raw_story_strategies, 1):
                if not isinstance(raw, Mapping):
                    continue
                strategy_id = str(raw.get("strategy_id") or f"S{source_index}")
                if strategy_id in executed_ids:
                    continue
                direction = dict(raw)
                direction["strategy_id"] = strategy_id
                direction["director_plan_role"] = "alternative"
                direction["chapter_packets"] = []
                direction["director_sequence"] = []
                normalized_strategies.append(direction)
            return {"strategies": normalized_strategies}

    # The recursive branch receives exactly one already-matched strategy.  It
    # may legitimately be an ``alternative``.  Looking for another ``primary``
    # here used to discard both the story copy and the Casting receipt for S2/
    # S3, even though the same two AI calls had fully authored those plans.
    raw_story_rows = story_payload.get("strategies") or ()
    if isinstance(raw_story_rows, Mapping):
        raw_story_rows = (raw_story_rows,)
    story = (
        next((dict(item) for item in raw_story_rows if isinstance(item, Mapping)), {})
        if _single_strategy
        else _two_pass_primary(story_payload)
    )
    raw_strategies = casting_payload.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        raw_strategies = (raw_strategies,)
    primary = (
        next((dict(item) for item in raw_strategies if isinstance(item, Mapping)), None)
        if _single_strategy
        else next(
            (
                dict(item) for item in raw_strategies
                if isinstance(item, Mapping)
                and str(item.get("director_plan_role") or item.get("plan_role") or "primary").lower()
                == "primary"
            ),
            None,
        )
    )
    if primary is None:
        raw_primary = casting_payload.get("primary")
        primary = dict(raw_primary) if isinstance(raw_primary, Mapping) else {}
    cast_primary = dict(primary)
    primary = dict(story)
    primary["strategy_id"] = "S1"
    primary["director_plan_role"] = "primary"
    # A Casting response may still revise the explanatory opening promise, but
    # cannot overwrite the story identity selected by call one.
    revised_opening = str(cast_primary.get("opening_promise") or "").strip()
    if revised_opening:
        primary["opening_promise"] = revised_opening
    for key in (
        "whole_video_audit", "stop_reason", "removed_chapters",
        "director_quality_tier",
        "opening_selection",
    ):
        if key in cast_primary:
            primary[key] = cast_primary[key]

    raw_story_chapters = story.get("chapter_packets") or ()
    if isinstance(raw_story_chapters, Mapping):
        raw_story_chapters = (raw_story_chapters,)
    story_chapter_by_id = {
        str(item.get("chapter_id") or f"C{index}").strip(): dict(item)
        for index, item in enumerate(raw_story_chapters, 1)
        if isinstance(item, Mapping)
    }
    raw_cast_chapters = cast_primary.get("chapter_packets") or ()
    if isinstance(raw_cast_chapters, Mapping):
        raw_cast_chapters = (raw_cast_chapters,)
    text_by_id: dict[int, str] = {}
    for row in casting_rows:
        try:
            subtitle_id = int(row.get("id") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if subtitle_id > 0:
            text_by_id[subtitle_id] = str(row.get("text") or "").strip()
    hydrated_chapters: list[dict[str, Any]] = []
    readthroughs: list[str] = []
    readthrough_warnings: list[str] = []
    frozen_keys = (
        "chapter_kind", "title", "purchase_question_id", "purchase_question",
        "buyer_advance", "new_buyer_knowledge", "coverage", "chapter_job",
        "micro_story_shape", "purpose", "structure_slot",
    )
    for index, raw_cast in enumerate(raw_cast_chapters, 1):
        if not isinstance(raw_cast, Mapping):
            continue
        cast_chapter = dict(raw_cast)
        revision = cast_chapter.get("chapter_revision")
        if isinstance(revision, Mapping) and str(revision.get("action") or "").strip().lower() == "drop":
            continue
        chapter_id = str(cast_chapter.get("chapter_id") or f"C{index}").strip()
        story_chapter = story_chapter_by_id.get(chapter_id, {})
        hydrated = dict(cast_chapter)
        hydrated["chapter_id"] = chapter_id
        for key in frozen_keys:
            value = story_chapter.get(key)
            if value is not None and (not isinstance(value, str) or value.strip()):
                hydrated[key] = value
        raw_beats = cast_chapter.get("beats") or ()
        if isinstance(raw_beats, Mapping):
            raw_beats = (raw_beats,)
        spoken_parts: list[str] = []
        for beat_index, raw_beat in enumerate(raw_beats, 1):
            if not isinstance(raw_beat, Mapping):
                continue
            for subtitle_id in DirectorBeat.from_dict(raw_beat, beat_index).subtitle_ids:
                text = text_by_id.get(int(subtitle_id), "")
                if text:
                    spoken_parts.append(text)
        readthrough = "｜".join(spoken_parts)
        supplied_readthrough = str(cast_chapter.get("chapter_readthrough") or "").strip()
        if supplied_readthrough and re.sub(r"[\W_]", "", supplied_readthrough) != re.sub(r"[\W_]", "", readthrough):
            readthrough_warnings.append(f"{chapter_id} 的 AI 连读文字与字幕 ID 不一致；预览仍使用源字幕原话。")
        hydrated["chapter_readthrough"] = readthrough
        if readthrough:
            readthroughs.append(readthrough)
        hydrated_chapters.append(hydrated)
    primary["chapter_packets"] = hydrated_chapters
    primary["final_readthrough"] = "｜".join(readthroughs)

    # [乙-bis] 开场「半截话」确定性硬门（0 新增 AI 调用）
    _leadin_receipt = _opening_leadin_veto(
        primary.get("opening_selection"),
        hydrated_chapters[0].get("beats") if hydrated_chapters else None,
        casting_rows,
        used_ids={
            int(subtitle_id)
            for chapter in hydrated_chapters
            for beat in (chapter.get("beats") or ())
            for subtitle_id in DirectorBeat.from_dict(beat, 1).subtitle_ids
        },
        hook_pool=opening_hook_pool,
    )
    if _leadin_receipt.get("status") != "skipped":
        # 只在硬门「看过」这条开场时留回执：关掉开关或钩子本就干净时不写，
        # 保证置 False 与落地前逐字节等价。
        _leadin_audit = dict(primary.get("whole_video_audit") or {})
        _leadin_audit["opening_leadin_veto"] = _leadin_receipt
        primary["whole_video_audit"] = _leadin_audit
    if _leadin_receipt.get("applied"):
        _leadin_parts: list[str] = []
        for _beat_index, _beat in enumerate(hydrated_chapters[0].get("beats") or (), 1):
            for _sid in DirectorBeat.from_dict(_beat, _beat_index).subtitle_ids:
                _leadin_text = text_by_id.get(int(_sid), "")
                if _leadin_text:
                    _leadin_parts.append(_leadin_text)
        hydrated_chapters[0]["chapter_readthrough"] = "｜".join(_leadin_parts)
        primary["chapter_packets"] = hydrated_chapters
        primary["final_readthrough"] = "｜".join(
            str(chapter.get("chapter_readthrough") or "")
            for chapter in hydrated_chapters
            if str(chapter.get("chapter_readthrough") or "")
        )

# Check the AI's receipt against its actual edit, not against a program's
    # preferred Hook. These checks are advisory and never change Beat order.
    opening = primary.get("opening_selection")
    if isinstance(opening, Mapping):
        opening = dict(opening)
        selected_ids = [
            subtitle_id
            for chapter in hydrated_chapters
            for index, beat in enumerate(chapter.get("beats") or (), 1)
            if isinstance(beat, Mapping)
            for subtitle_id in DirectorBeat.from_dict(beat, index).subtitle_ids
        ]
        first_ids = [
            subtitle_id
            for index, beat in enumerate((hydrated_chapters[0] if hydrated_chapters else {}).get("beats") or (), 1)
            if isinstance(beat, Mapping)
            for subtitle_id in DirectorBeat.from_dict(beat, index).subtitle_ids
        ]
        receipt_ids = list(DirectorBeat.from_dict({"subtitle_ids": opening.get("selected_subtitle_ids") or []}, 1).subtitle_ids)
        warnings: list[str] = list(readthrough_warnings)
        if len(selected_ids) != len(set(selected_ids)):
            warnings.append("AI 片单重复使用了同一字幕 ID，请复核；未自动删句。")
        if not receipt_ids or receipt_ids != first_ids[:len(receipt_ids)]:
            warnings.append("开场比较记录与实际首章不一致，请连读复核；未自动替换。")
        packages = opening.get("compared_packages") or ()
        if not isinstance(packages, (list, tuple)):
            packages = ()
        if any(
            subtitle_id not in text_by_id
            for package in packages if isinstance(package, Mapping)
            for subtitle_id in DirectorBeat.from_dict(package, 1).subtitle_ids
        ):
            warnings.append("开场比较引用了素材池外的字幕，请复核。")
        opening["candidate_audit"] = audit_opening_candidates(
            opening, source_rows=casting_rows, executed_ids=selected_ids,
            product_context_rows=product_context_rows,
            hook_pool=opening_hook_pool,
        )
        if opening["candidate_audit"]["status"] == "warning":
            warnings.append("开场候选回执的来源、独立候选池归属或实际承接不一致，请复核；未自动换句。")
        audit = dict(primary.get("whole_video_audit") or {})
        links = audit.get("continuity_links") or ()
        if not isinstance(links, (list, tuple)):
            links = ()
        positions = {subtitle_id: index for index, subtitle_id in enumerate(selected_ids)}
        for link in links:
            if not isinstance(link, Mapping):
                continue
            try:
                before, after = int(link.get("from_id")), int(link.get("to_id"))
            except (TypeError, ValueError):
                before, after = 0, 0
            if before not in positions or after not in positions or positions[before] >= positions[after]:
                warnings.append("连读依赖缺失或前后颠倒，请复核；未自动重排。")
                break
        opening["verification"] = {"status": "warning" if warnings else "consistent", "warnings": warnings}
        primary["opening_selection"] = opening
        if warnings:
            audit["contract_warnings"] = warnings
            audit["status"] = "needs_review"
            primary["whole_video_audit"] = audit

    structure = story.get("video_structure")
    if isinstance(structure, Mapping):
        primary["video_structure"] = dict(structure)
    central_promise = str(story.get("central_promise") or "").strip()
    if central_promise:
        primary["thesis"] = central_promise
        primary["core_commercial_idea"] = central_promise
    story_premise = str(primary.get("story_premise") or "").strip()
    if not story_premise:
        story_premise = "；".join(
            part for part in (
                str(primary.get("audience_tension") or "").strip(),
                str(primary.get("transformation") or "").strip(),
            ) if part
        )
    primary["story_premise"] = story_premise or central_promise
    primary["payoff"] = str(primary.get("opening_promise") or central_promise).strip()
    primary["story_contract"] = story

    strategies: list[dict[str, Any]] = [primary]
    if _single_strategy:
        return {"strategies": strategies}
    raw_alternatives = story_payload.get("alternative_directions") or ()
    if not raw_alternatives:
        all_initial = story_payload.get("strategies") or ()
        if isinstance(all_initial, Mapping):
            all_initial = (all_initial,)
        raw_alternatives = tuple(
            item for item in all_initial
            if isinstance(item, Mapping)
            and str(item.get("director_plan_role") or item.get("plan_role") or "primary").lower()
            != "primary"
        )
    if isinstance(raw_alternatives, Mapping):
        raw_alternatives = (raw_alternatives,)
    for index, raw in enumerate(raw_alternatives, 2):
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        item["strategy_id"] = str(item.get("strategy_id") or f"S{index}")
        item["director_plan_role"] = "alternative"
        item["chapter_packets"] = []
        item["director_sequence"] = []
        strategies.append(item)
    return {"strategies": strategies}


# ──────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────

def _story_cited_evidence_ids(
    story_payload: Mapping[str, Any],
    available_subtitle_ids: Sequence[int] | None = None,
) -> set[int]:
    """Return the subtitle ids M1 actually cited as evidence."""
    allowed = (
        {int(value) for value in available_subtitle_ids}
        if available_subtitle_ids is not None
        else None
    )
    cited: set[int] = set()
    strategies = story_payload.get("strategies") or ()
    if isinstance(strategies, Mapping):
        strategies = (strategies,)
    for strategy in strategies:
        if not isinstance(strategy, Mapping):
            continue
        items: list[Any] = []
        for key in (
            "evidence",
            "core_evidence_pool",
            "supporting_evidence_pool",
            "core_assets",
            "supporting_assets",
        ):
            value = strategy.get(key)
            if isinstance(value, list):
                items.extend(value)
        for packet in strategy.get("chapter_packets") or strategy.get("chapters") or ():
            if not isinstance(packet, Mapping):
                continue
            locations = packet.get("evidence_locations")
            if isinstance(locations, list):
                items.extend(locations)
        for package in strategy.get("opening_evidence_packages") or ():
            if not isinstance(package, Mapping):
                continue
            for key in ("hook_subtitle_ids", "payoff_subtitle_ids"):
                value = package.get(key)
                if isinstance(value, list):
                    items.extend(value)
        for item in items:
            raw_ids = item
            if isinstance(item, Mapping):
                raw_ids = item.get("subtitle_ids")
                if raw_ids is None:
                    raw_ids = item.get("subtitle_id")
            if isinstance(raw_ids, (int, float, str)):
                raw_ids = (raw_ids,)
            for value in raw_ids or ():
                try:
                    cited.add(int(value))
                except (TypeError, ValueError):
                    continue
    if allowed is not None:
        cited &= allowed
    return cited


def _story_cited_evidence_seconds(
    story_payload: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    available_subtitle_ids: Sequence[int] | None = None,
) -> float:
    """Seconds of cited evidence, measured by the same rule as discovery."""
    duration_map = _subtitle_duration_map(subtitles)
    cited_ids = _story_cited_evidence_ids(story_payload, available_subtitle_ids)
    return round(sum(duration_map.get(sid, 0.0) for sid in cited_ids), 3)


def analyze_commercial_story(
    *,
    api_key: str,
    base_url: str,
    model: str,
    product: str,
    subtitles: Sequence[Mapping[str, Any]],
    content_contract: Mapping[str, Any] | None = None,
    commercial_assets: Sequence[Mapping[str, Any]] | None = None,
    executable_subtitle_ids: Sequence[int] | None = None,
    target_duration: float = 45.0,
    duration_tolerance: float | None = None,
    director_focus: Mapping[str, Any] | None = None,
    director_controls: Mapping[str, Any] | None = None,
    temperature: float = 0.0,
    top_p: float = 0.8,
    max_tokens: int = 4000,
    timeout: int = 180,
    log_fn=None,
    raw_response_hook=None,
    stage_response_hook=None,
    stage_progress_hook: Callable[[str], None] | None = None,
    two_pass_director: bool = False,
    output_speed_factor: float = 1.0,
    source_context_subtitles: Sequence[Mapping[str, Any]] | None = None,
    director_plan_count: int = 1,
    enable_duration_calibration: bool = False,
    opening_hook_recall: bool = False,
) -> StrategyDiscoveryResult:
    """Discover and cast one to three commercial stories in two semantic calls."""
    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    director_model = resolve_commercial_director_model(base_url, model)
    opening_hook_payload: dict[str, Any] | None = None
    if two_pass_director:
        # Production supplies controls (even in auto mode). Legacy offline
        # readers without them retain their old response contract.
        check_product = director_controls is not None
        product_target = build_product_target(director_controls)
        identity_source = source_context_subtitles or subtitles
        executable_subtitle_ids, candidate_policy_audit = filter_director_executable_ids_for_content_policy(
            subtitles,
            executable_subtitle_ids,
            content_contract,
        )
        if candidate_policy_audit["status"] == "no_safe_candidates":
            raise AnalyzerError("当前内容限制排除了全部可选短句，请放宽限制或更换素材。")
        if candidate_policy_audit["status"] == "policy_filtered":
            log(
                "内容边界已在导演前排除 "
                f"{len(candidate_policy_audit['excluded'])} 条不可选字幕；不会进入故事或选片主链。"
            )
            if stage_response_hook:
                stage_response_hook(
                    "candidate_content_policy",
                    json.dumps(candidate_policy_audit, ensure_ascii=False, indent=2),
                )
        format_executable_ids = list(executable_subtitle_ids) if executable_subtitle_ids is not None else None
        casting_rows = _director_casting_rows(subtitles, executable_subtitle_ids)
        if check_product and product_target.get("product_type") not in {None, "", "unknown"}:
            foreign_ids = {
                sid for item in foreign_product_ranges(str(product_target["product_type"]), identity_source)
                for sid in range(int(item["start_id"]), int(item["end_id"]) + 1)
            }
            if foreign_ids:
                casting_rows = [row for row in casting_rows if int(row["id"]) not in foreign_ids]
                executable_subtitle_ids = [int(row["id"]) for row in casting_rows]
        if check_product and product_target["needs_specific_category"]:
            raise AnalyzerError("‘衣服/服装’范围太宽，请在细分类目选择上衣、衬衫、针织衫、外套等具体单品，或清空主商品后自动识别。")
        story_executable_ids, story_candidate_policy_audit = filter_director_executable_ids_for_content_policy(
            subtitles,
            executable_subtitle_ids,
            content_contract,
            exclude_body_only_from_story=True,
        )
        if story_candidate_policy_audit["status"] == "no_safe_candidates":
            raise AnalyzerError("当前内容限制下没有可承担故事或开头的短句，请放宽‘仅正文’限制或补充素材。")
        if story_candidate_policy_audit["excluded"] and story_candidate_policy_audit["body_only_kinds"]:
            body_only_count = sum(
                1 for item in story_candidate_policy_audit["excluded"]
                if set(item["blocked_kinds"]) & set(story_candidate_policy_audit["body_only_kinds"])
            )
            if body_only_count:
                log(
                    f"{body_only_count} 条‘仅正文’字幕不参与故事或开头；"
                    "仅在第二步作为正文候选。"
                )
                if stage_response_hook:
                    stage_response_hook(
                        "story_candidate_content_policy",
                        json.dumps(story_candidate_policy_audit, ensure_ascii=False, indent=2),
                    )
        # P0-a：独立全文 Hook 召回（opt-in）。必须在 story contract 之前跑，
        # 导演手里才是真有一个“开场候选池”，而不是从正文池里顺出第一句当开头。
        opening_hook_prompt_rows: list[dict[str, Any]] | None = None
        if opening_hook_recall:
            try:
                from opening_hook_recall_adapter import (
                    hook_pool_prompt_rows,
                    recall_opening_hooks,
                )
                opening_hook_payload = recall_opening_hooks(
                    subtitles=subtitles,
                    api_key=api_key,
                    base_url=base_url,
                    model=director_model,
                    opening_promise=_opening_promise_hint(director_focus, director_controls),
                    allowed_opening_answer_roles=DIRECTOR_ANSWER_ROLES,
                    executable_subtitle_ids=story_executable_ids,
                    log_fn=log,
                )
                opening_hook_prompt_rows = hook_pool_prompt_rows(opening_hook_payload)
            except Exception as hook_exc:  # noqa: BLE001 - 召回失败不得阻断导演
                opening_hook_payload = {
                    "status": "hook_recall_failed",
                    "error": f"{type(hook_exc).__name__}: {hook_exc}"[:300],
                    "hook_candidates": [],
                }
                opening_hook_prompt_rows = []
                log(f"独立 Hook 召回异常（已降级）：{opening_hook_payload['error']}")
            # 召回失败 ≠ 素材没有合格开场。只有“成功但 0 候选”才允许提示受限；
            # 真正的失败一律不注入开场池，行为静默回退到改动前，避免主动放弃开场。
            _hook_status = str((opening_hook_payload or {}).get("status") or "")
            if _hook_status not in ("hook_recall_completed", "hook_material_limited"):
                opening_hook_prompt_rows = None
                log(
                    f"独立 Hook 召回未成功（status={_hook_status}）：本次不注入开场池，"
                    "回退旧行为；不使用“声明受限”。"
                )
            if stage_response_hook:
                stage_response_hook(
                    "opening_hook_recall",
                    json.dumps(opening_hook_payload, ensure_ascii=False, indent=2),
                )
            log(
                "独立 Hook 召回：" + str(opening_hook_payload.get("status"))
                + f"，候选 {len(opening_hook_prompt_rows or [])} 条"
            )
        story_prompt = build_two_pass_story_prompt(
            product=product,
            subtitles=subtitles,
            content_contract=content_contract,
            executable_subtitle_ids=story_executable_ids,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            director_focus=director_focus,
            director_controls=director_controls,
            output_speed_factor=output_speed_factor,
            source_context_subtitles=source_context_subtitles,
            director_plan_count=director_plan_count,
            opening_hook_pool=opening_hook_prompt_rows,
        )
        if stage_progress_hook:
            stage_progress_hook("story_contract_started")
        story_raw = _post_two_pass_director_request_with_retry(
            api_key=api_key,
            base_url=base_url,
            model=director_model,
            system_prompt=TWO_PASS_STORY_SYSTEM_PROMPT,
            user_prompt=story_prompt,
            stage="Director_story_contract",
            # Story contracts contain no source text or chosen IDs.  A 3k
            # single-plan cap comfortably fits the observed compact contract
            # while keeping the commercial peak-time budget predictable.
            max_tokens=4500 * max(1, min(3, int(director_plan_count or 1))),
            timeout=max(180, int(timeout)),
            response_hook=(
                lambda value: stage_response_hook("story_contract", value)
            ) if stage_response_hook else None,
        )
        if stage_response_hook:
            stage_response_hook("story_contract", story_raw)
        story_payload = _parse_director_stage_json_with_recovery(
            story_raw,
            api_key=api_key,
            base_url=base_url,
            model=director_model,
            stage="Director_story_contract",
            system_prompt=TWO_PASS_STORY_SYSTEM_PROMPT,
            max_tokens=4500 * max(1, min(3, int(director_plan_count or 1))),
            timeout=max(180, int(timeout)),
            log_fn=log,
            stage_response_hook=stage_response_hook,
        )
        if stage_progress_hook:
            stage_progress_hook("story_contract_completed")
        story_payload, story_policy_audit = sanitize_two_pass_story_for_content_policy(
            story_payload, content_contract,
        )
        evidence_issues = _story_evidence_issues(story_payload, story_executable_ids)
        if evidence_issues:
            log("故事证据定位有疑点，第二轮从完整安全池重新核对：" + "；".join(evidence_issues))
            for strategy in story_payload.get("strategies") or []:
                for chapter in strategy.get("chapter_packets") or []:
                    locations = chapter.get("evidence_locations")
                    if "evidence_locations" in chapter:
                        chapter["evidence_locations"] = [
                            value for value in locations if type(value) is int and value in story_executable_ids
                        ] if isinstance(locations, list) else []
        evidence_notes = [
            {"strategy_id": strategy.get("strategy_id"), "chapter_id": chapter.get("chapter_id"),
             "count": len(chapter.get("evidence_locations") or []),
             "status": "many_locations" if chapter.get("evidence_locations") else "missing_locations"}
            for strategy in story_payload.get("strategies") or []
            for chapter in strategy.get("chapter_packets") or []
            if "evidence_locations" in chapter and (
                len(chapter.get("evidence_locations") or []) > 3 or not chapter.get("evidence_locations")
            )
        ]
        if evidence_notes:
            log("故事证据定位数量仅供参考，继续第二轮核对事实与选句。")
            if stage_response_hook:
                stage_response_hook("story_evidence_notes", json.dumps(evidence_notes, ensure_ascii=False))
        story_audit = build_two_pass_story_audit(
            story_payload,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            available_subtitle_ids=story_executable_ids,
        )
        story_audit.update(director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor))
        story_audit["content_policy"] = story_policy_audit
        story_audit["candidate_content_policy"] = candidate_policy_audit
        story_audit["story_candidate_content_policy"] = story_candidate_policy_audit
        story_audit["evidence_notes"] = evidence_notes
        story_audit["evidence_issues"] = evidence_issues
        _depth_pacing = director_material_pacing(
            _director_casting_rows(subtitles, story_executable_ids),
            director_delivery_duration_range(
                target_duration, duration_tolerance, output_speed_factor,
            ),
            max_beats_per_chapter=int(
                director_duration_depth_contract(target_duration)["beats_per_chapter"]["high"]
            ),
        )
        story_depth_audit = _story_delivery_depth_audit(
            story_payload,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            output_speed_factor=output_speed_factor,
            material_mean_beat_seconds=float(
                _depth_pacing["material_mean_beat_seconds"] or 0.0
            ),
            material_pool_seconds=float(_depth_pacing["material_pool_seconds"] or 0.0),
        )
        story_audit["delivery_depth"] = story_depth_audit
        if story_depth_audit.get("calibrated_status") == "insufficient":
            _thin_ids = {
                str(value)
                for value in story_depth_audit.get("calibrated_insufficient_strategy_ids") or []
            }
            _planned_min = min(
                [
                    int(item["chapter_count"])
                    for item in story_depth_audit["strategies"]
                    if str(item["strategy_id"]) in _thin_ids
                ]
                or [0]
            )
            log(
                "导演方案篇幅提示：素材真实句长约 "
                f"{float(story_depth_audit.get('material_mean_beat_seconds') or 0.0):.2f} 秒，"
                f"本次目标至少需要约 "
                f"{int(story_depth_audit.get('calibrated_minimum_chapters') or 0)} 个推进章节"
                f"（约 {int(story_depth_audit.get('calibrated_min_beats') or 0)} 个要点），"
                f"当前最少只有 {_planned_min} 章；此为提示，不改章节顺序，继续第二轮按真实原话补足。"
            )
        if story_policy_audit["status"] == "policy_trimmed":
            removed = [item["chapter_id"] for item in story_policy_audit["removed_chapters"]]
            trimmed = [item["chapter_id"] for item in story_policy_audit.get("trimmed_chapters", [])]
            log(f"内容边界：移除 {len(removed)} 个纯禁选章节，保留并清理 {len(trimmed)} 个混合章节；保留原有顺序。")
            if stage_response_hook:
                stage_response_hook("story_content_policy", json.dumps(story_policy_audit, ensure_ascii=False, indent=2))
        if (
            story_policy_audit["status"] == "policy_trimmed"
            and story_depth_audit["status"] == "insufficient_after_policy"
        ):
            insufficient = story_depth_audit["insufficient_strategy_ids"]
            detail_by_id = {
                str(item["strategy_id"]): item
                for item in story_depth_audit["strategies"]
            }
            details = [
                f"{strategy_id} 仅 {detail_by_id[strategy_id]['chapter_count']} 章/"
                f"{detail_by_id[strategy_id]['planned_source_seconds']:.1f} 秒预算"
                for strategy_id in insufficient
                if str(strategy_id) in detail_by_id
            ]
            message = (
                f"内容边界后故事无法支撑本次 {story_depth_audit['requested_seconds']:.0f} 秒预览："
                + "；".join(details)
                + f"。至少需要 {story_depth_audit['minimum_chapters']} 个推进章节和 "
                + f"{story_depth_audit['minimum_planned_source_seconds']:.1f} 秒原声预算；"
                "此项仅为预算提示，继续第二轮由AI按真实原话安排时长与衔接。"
            )
            log(message)
            if stage_response_hook:
                stage_response_hook("story_delivery_depth", json.dumps(story_depth_audit, ensure_ascii=False, indent=2))
        identity_errors = audit_product_selection(_two_pass_primary(story_payload), {}, target=product_target, subtitles=identity_source)["scope_errors"] if check_product else []
        story_audit["product_target"] = product_target
        story_audit["product_scope_errors"] = identity_errors
        cast_prompt = build_two_pass_cast_prompt(
            story_contract=story_payload,
            story_audit=story_audit,
            subtitles=subtitles,
            content_contract=content_contract,
            executable_subtitle_ids=executable_subtitle_ids,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            director_controls=director_controls,
            output_speed_factor=output_speed_factor,
            source_context_subtitles=source_context_subtitles,
            opening_hook_pool=opening_hook_prompt_rows,
        )
        if identity_errors:
            cast_prompt += "\n" + "\n".join([
                "第一遍故事存在商品冲突，不能只改标题再执行旧商品章节。用户商品约束优先于冻结的错误故事。",
                "请在本次选句同时返回顶层 corrected_story（完整正确的主方案对象，含 core_desire、product_scope、chapter_packets 的章节职责但不含 beats），再让 strategies[0] 的真实短句执行它。无需再请求第一遍AI。",
                "无该商品真实素材时 product_scope.target_confirmation=not_found，不能偷换主商品。所有备选方向也必须服务该商品。",
            ])
        if stage_progress_hook:
            stage_progress_hook("beat_casting_started")
        cast_raw = _post_two_pass_director_request_with_retry(
            api_key=api_key,
            base_url=base_url,
            model=director_model,
            system_prompt=TWO_PASS_CAST_SYSTEM_PROMPT,
            user_prompt=cast_prompt,
            stage="Director_beat_casting",
            # The compact response omits non-executed alternatives.  A single
            # complete 60-second selection still needs a small JSON-completion
            # margin; this is a ceiling, not a prepaid token allocation.
            max_tokens=director_casting_output_max_tokens(director_plan_count, target_duration),
            timeout=director_casting_request_timeout(director_model, timeout),
            response_hook=(
                lambda value: stage_response_hook("beat_casting", value)
            ) if stage_response_hook else None,
        )
        if stage_response_hook:
            stage_response_hook("beat_casting", cast_raw)
        cast_payload = _parse_director_stage_json_with_recovery(
            cast_raw,
            api_key=api_key,
            base_url=base_url,
            model=director_model,
            stage="Director_beat_casting",
            system_prompt=TWO_PASS_CAST_SYSTEM_PROMPT,
            max_tokens=director_casting_output_max_tokens(director_plan_count, target_duration),
            timeout=director_casting_request_timeout(director_model, timeout),
            log_fn=log,
            stage_response_hook=stage_response_hook,
        )
        if stage_progress_hook:
            stage_progress_hook("beat_casting_completed")
        # 章内连续性校验（确定性，只读，不增加任何 AI 调用）：
        # 同一章不得由相隔很远的口播窗拼成；残句/半句不得成章。
        # 选片链路固定「故事一遍 + 选句一遍」，这里只报告，不改写、不重排、不追加调用。
        _audit_cast_window_continuity(
            cast_payload,
            subtitles,
            executable_ids=format_executable_ids,
            stage_response_hook=stage_response_hook,
            log_fn=log,
        )
        # New Director replies must keep one source ID in each Beat.  Capture
        # legacy grouped replies before normalizing them for editable preview;
        # the normalizer preserves every AI-selected line but the audit must
        # not mistake that mechanical expansion for a compliant short-beat plan.
        grouped_beat_issues = _cast_beat_cardinality_issues(cast_payload)
        grouped_beat_issues_by_strategy: dict[str, list[dict[str, Any]]] = {}
        for issue in grouped_beat_issues:
            grouped_beat_issues_by_strategy.setdefault(str(issue.get("strategy_id") or "S1"), []).append(issue)
        format_receipt = _expand_cast_sentence_groups(cast_payload, subtitles, format_executable_ids)
        # P0-1 拆窗补齐（确定性，不增加 AI 调用、不改写、不重排）：
        # 只把「同一连续口播窗里缺的那几行」按原顺序补回，避免落盘成半句。
        try:
            split_repair_receipt = _repair_cast_window_splits(
                cast_payload, subtitles, format_executable_ids, log_fn=log,
            )
        except Exception as split_exc:  # noqa: BLE001 - 补齐失败绝不阻断导演
            split_repair_receipt = {"repaired": [], "skipped": []}
            log(f"拆窗补齐异常（已跳过）：{type(split_exc).__name__}: {split_exc}")
        if split_repair_receipt["repaired"]:
            log(
                f"拆窗补齐 {len(split_repair_receipt['repaired'])} 句"
                "（只补回同一窗内缺的行，按原顺序，未改写原话）："
                + "、".join(
                    f"#{item['subtitle_id']}({item['side']})"
                    for item in split_repair_receipt["repaired"][:12]
                )
            )
        if split_repair_receipt["skipped"]:
            log(f"有 {len(split_repair_receipt['skipped'])} 处拆窗无法补齐（窗外/已被使用/超上限），已记录审计。")
        if stage_response_hook and (split_repair_receipt["repaired"] or split_repair_receipt["skipped"]):
            stage_response_hook(
                "cast_window_split_repair",
                json.dumps(split_repair_receipt, ensure_ascii=False, indent=2),
            )
        # 整窗整取（确定性；零 AI 调用、不改写、不重排、不删改 AI 已选行）：
        # AI 常只取一个连续口播窗的一部分，落盘就把窗内承接句丢掉（实测丢 38%）。
        # 这里把窗内剩下的行按原顺序补回来，并受时长契约上界（source_max）约束。
        try:
            from selection_contracts import DurationContract as _WholeKeepContract
            _whole_keep_contract = _WholeKeepContract.create(
                target_duration, output_speed_factor, tolerance=duration_tolerance,
            )
            _whole_keep_budget = min(
                _whole_keep_contract.source_max,
                _whole_keep_contract.source_target * max(0.0, float(CAST_WINDOW_WHOLE_KEEP_BUDGET_RATIO)),
            )
        except Exception:  # noqa: BLE001 - 取不到契约就不设预算
            _whole_keep_budget = None
        try:
            whole_keep_receipt = _expand_cast_windows_whole(
                cast_payload, subtitles, format_executable_ids,
                budget_seconds=_whole_keep_budget, log_fn=log,
            )
        except Exception as whole_exc:  # noqa: BLE001 - 整取失败绝不阻断导演
            whole_keep_receipt = {"kept": [], "dropped": [], "opening_gate": [], "closing_gate": []}
            log(f"整窗整取异常（已跳过）：{type(whole_exc).__name__}: {whole_exc}")
        if whole_keep_receipt.get("kept"):
            log(
                f"整窗整取补回 {len(whole_keep_receipt['kept'])} 句（窗内剩余行按原顺序补齐，未改写原话）；"
                f"契约上界 {whole_keep_receipt.get('budget_seconds')}s，各方案："
                + "、".join(
                    f"S{node['strategy_index']} {node['base_seconds']}s→{node['projected_seconds']}s"
                    f"(保留比 {node['keep_ratio']})"
                    for node in whole_keep_receipt.get("strategies") or []
                )
            )
        if whole_keep_receipt.get("dropped"):
            log(f"整窗整取有 {len(whole_keep_receipt['dropped'])} 句因时长预算未补入（已记录审计）。")
        # 章首/章尾收口（确定性；零 AI 调用、不改写、不重排、只动边界）：
        # 章尾停在半句就补到收句行；补不动且只是 ≤1.2s 水词就从章内移出；
        # 其余如实报告。第一章章首由 AI 的开场声明保护，不参与移出。
        try:
            chapter_edge_receipt = _guard_chapter_edges(
                cast_payload, subtitles, format_executable_ids, log_fn=log,
            )
        except Exception as edge_exc:  # noqa: BLE001 - 收口失败绝不阻断导演
            chapter_edge_receipt = {}
            log(f"章首章尾收口异常（已跳过）：{type(edge_exc).__name__}: {edge_exc}")
        if chapter_edge_receipt.get("tail_filled"):
            log("章尾补完整：" + "、".join(
                f"{item['chapter_id']}→#{item['subtitle_id']}"
                for item in chapter_edge_receipt["tail_filled"][:12]))
        if chapter_edge_receipt.get("tail_stub_removed") or chapter_edge_receipt.get("head_stub_removed"):
            log("章首/章尾残句已移出（≤%.1fs 且不收句）：%s" % (
                CAST_CHAPTER_EDGE_STUB_SECONDS,
                "、".join(
                    "%s#%d" % (item["chapter_id"], item["subtitle_id"])
                    for item in (chapter_edge_receipt.get("tail_stub_removed") or [])
                    + (chapter_edge_receipt.get("head_stub_removed") or [])),
            ))
        if chapter_edge_receipt.get("reported"):
            log(f"有 {len(chapter_edge_receipt['reported'])} 处章尾仍停在半句（窗内没有可用的收句行），已记录审计、未改句。")
        if stage_response_hook and (chapter_edge_receipt.get("tail_filled")
                                    or chapter_edge_receipt.get("tail_stub_removed")
                                    or chapter_edge_receipt.get("head_stub_removed")
                                    or chapter_edge_receipt.get("reported")):
            stage_response_hook(
                "cast_chapter_edge_guard",
                json.dumps(chapter_edge_receipt, ensure_ascii=False, indent=2),
            )
        if whole_keep_receipt.get("opening_gate") or whole_keep_receipt.get("closing_gate"):
            log("首尾门禁提示：" + json.dumps({
                "opening": whole_keep_receipt.get("opening_gate"),
                "closing": whole_keep_receipt.get("closing_gate"),
            }, ensure_ascii=False))
        if stage_response_hook and (whole_keep_receipt.get("kept") or whole_keep_receipt.get("dropped")):
            stage_response_hook(
                "cast_window_whole_keep",
                json.dumps(whole_keep_receipt, ensure_ascii=False, indent=2),
            )
        # 承接审查（按内容，一次极小调用）+ 确定性补入缺失的承接句。
        try:
            continuation_review = review_continuations(
                cast_payload, subtitles, api_key=api_key, base_url=base_url,
                model=director_model, timeout=director_casting_request_timeout(director_model, timeout), log_fn=log,
                executable_ids=format_executable_ids,
            )
        except Exception as continuation_exc:  # noqa: BLE001 - 绝不阻断导演
            continuation_review = {"dangling": [], "checked": 0}
            log(f"承接审查异常（已跳过）：{type(continuation_exc).__name__}: {continuation_exc}")
        continuation_receipt = apply_continuation_fill(
            cast_payload, subtitles, continuation_review.get("fixes") or [], format_executable_ids,
        )
        if stage_response_hook:
            stage_response_hook("continuation_review", json.dumps({
                "checked": continuation_review.get("checked", 0),
                "fixes": continuation_review.get("fixes") or [],
                "inserted": continuation_receipt["inserted"],
                "skipped": continuation_receipt["skipped"],
            }, ensure_ascii=False, indent=2))
        if continuation_receipt["inserted"]:
            log(f"已补入 {len(continuation_receipt['inserted'])} 条承接句（原句没说完，按原顺序补下一行；未改写原话）。")
        if continuation_receipt["skipped"]:
            log(f"有 {len(continuation_receipt['skipped'])} 处承接句无法补入（不在可执行池或已被使用），已记录审计。")
        # ② 开场落地：strong 回执的顺序必须落在第一章最前面（只调序，不改写）。
        try:
            _opening_ids, _opening_quality = _opening_receipt_selection(opening_hook_payload)
            if _opening_ids and _opening_quality == "strong":
                _landing = _enforce_opening_first(cast_payload, _opening_ids)
                if _landing.get("moved"):
                    log("开场已按回执前移（strong）：" + str(_landing["moved"]))
                    if stage_response_hook:
                        stage_response_hook("opening_landing", json.dumps(_landing, ensure_ascii=False))
        except Exception as _opening_exc:  # noqa: BLE001 - 落地失败不影响主流程
            log(f"开场落地检查异常（已跳过）：{type(_opening_exc).__name__}: {_opening_exc}")
        if format_receipt["expanded_groups"]:
            log(f"已按 AI 原顺序展开 {format_receipt['expanded_groups']} 个多句组；未增删原话或调整章节。")
        if format_receipt.get("dropped_ids"):
            _dropped_preview = ", ".join(str(item.get("subtitle_id")) for item in format_receipt["dropped_ids"][:12])
            log(
                f"已丢弃 {len(format_receipt['dropped_ids'])} 个不在可执行池的选句"
                f"（模型引用了仅作上下文展示的行）：{_dropped_preview}"
            )
        if stage_response_hook and format_receipt["expanded_groups"]:
            stage_response_hook("casting_format_normalization", json.dumps(format_receipt, ensure_ascii=False))
        if identity_errors and isinstance(cast_payload.get("corrected_story"), Mapping):
            corrected = dict(cast_payload["corrected_story"])
            if not scope_errors(corrected.get("product_scope"), product_target):
                story_payload = {"strategies": [corrected]}
        story_payload, cast_payload, chapter_revision_audit = _apply_casting_chapter_revisions(
            story_payload, cast_payload,
        )
        if stage_response_hook and any(
            row.get("status") != "unchanged"
            for row in chapter_revision_audit.get("strategies") or ()
            if isinstance(row, Mapping)
        ):
            stage_response_hook(
                "chapter_revision",
                json.dumps(chapter_revision_audit, ensure_ascii=False, separators=(",", ":")),
            )
        for revision_row in chapter_revision_audit.get("strategies") or ():
            if revision_row.get("status") == "applied":
                log("导演已按真实口播调整章节：" + json.dumps(
                    revision_row.get("applied") or [], ensure_ascii=False, separators=(",", ":"),
                ))
                if revision_row.get("coerced"):
                    log(
                        "其中 "
                        + str(len(revision_row["coerced"]))
                        + " 条修订的 action 由 refine 自动纠偏为 merge（语义成立）："
                        + json.dumps(revision_row["coerced"], ensure_ascii=False, separators=(",", ":"))
                    )
            elif revision_row.get("status") == "ignored_invalid":
                log("导演章节调整格式无效，保留第一轮章节合同并显示原选片。")
                if revision_row.get("coerced"):
                    log(
                        "其中 "
                        + str(len(revision_row["coerced"]))
                        + " 条修订语义成立、只是 action 标签写成 refine（应为 merge），"
                        + "已按 merge 纠偏但仍未通过整体校验："
                        + json.dumps(revision_row["coerced"], ensure_ascii=False, separators=(",", ":"))
                    )
        audit_args = {
            "story_contract": story_payload, "subtitles": subtitles,
            "executable_subtitle_ids": executable_subtitle_ids,
            "target_duration": target_duration, "duration_tolerance": duration_tolerance,
            "output_speed_factor": output_speed_factor,
        }
        initial_product_audit = audit_product_selection(
            _two_pass_primary(story_payload),
            _two_pass_primary(cast_payload),
            target=product_target,
            subtitles=identity_source,
        ) if check_product else {}
        cast_payload, strategy_duration_controls, primary_strategy_id = _audit_second_pass_duration_for_strategies(
            casting_payload=cast_payload,
            story_payload=story_payload,
            subtitles=subtitles,
            executable_subtitle_ids=executable_subtitle_ids,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            output_speed_factor=output_speed_factor,
            grouped_beat_issues_by_strategy=grouped_beat_issues_by_strategy,
        )
        primary_duration_control = strategy_duration_controls.get(primary_strategy_id) or next(
            iter(strategy_duration_controls.values()), {}
        )
        if primary_duration_control:
            initial_audit = dict(primary_duration_control.get("initial") or {})
            final_audit = dict(primary_duration_control.get("final") or initial_audit)
            duration_fill_control = dict(
                primary_duration_control.get("duration_fill") or _empty_duration_fill_control()
            )
        else:
            initial_audit = build_director_duration_audit(casting_payload=cast_payload, **audit_args)
            final_audit = initial_audit
            duration_fill_control = _empty_duration_fill_control()
        _ai_cast_status = ""
        _ai_cast_audit = _two_pass_primary(cast_payload).get("whole_video_audit")
        if isinstance(_ai_cast_audit, Mapping):
            _ai_cast_status = str(_ai_cast_audit.get("status") or "")
        if _ai_cast_status == "pass" and initial_audit.get("needs_calibration"):
            log("AI 自报 pass，但程序实测未落入可编辑区间；按“需校准”处理并保留可编辑方案。")
        if not enable_duration_calibration and _gross_duration_contract_violation(final_audit):
            contract = final_audit.get("duration_contract") or {}
            log(
                "Director 选片严重偏离时长合同："
                f"实测原声 {float(final_audit.get('source_seconds') or 0.0):.1f} 秒，"
                f"可编辑区间 {float(contract.get('source_min') or 0.0):.1f}–{float(contract.get('source_max') or 0.0):.1f} 秒。"
                "保留可编辑预览并显示实测偏差，不自动截短或追加AI。"
            )
        product_audit = audit_product_selection(_two_pass_primary(story_payload), _two_pass_primary(cast_payload), target=product_target, subtitles=identity_source) if check_product else {}
        if check_product:
            _attach_main_product_pool_audit(initial_audit, story_contract=story_payload, subtitles=subtitles,
                                            executable_subtitle_ids=executable_subtitle_ids,
                                            output_speed_factor=output_speed_factor)
            if final_audit is not initial_audit:
                _attach_main_product_pool_audit(final_audit, story_contract=story_payload, subtitles=subtitles,
                                                executable_subtitle_ids=executable_subtitle_ids,
                                                output_speed_factor=output_speed_factor)
        calibration = {
            "attempted": False,
            "accepted_revision": False,
            "max_attempts": 1 if enable_duration_calibration else 0,
        }
        # Product ambiguity is an audit warning, not a reason to resend the
        # complete transcript, prior response and audits in a third paid call.
        # The optional third call remains reserved for a real duration or
        # chapter-completion shortfall.
        if final_audit["needs_calibration"] and not enable_duration_calibration:
            calibration["skipped_reason"] = "single_casting_delivery"
            log("本次一次选片未达目标，已保留可编辑方案并显示实测时长；不后补、不追加第三轮AI。")
        if _calibration_entry_blocked(final_audit):
            calibration["skipped_reason"] = "already_in_range"
            log(
                "时长校准验收闸：程序实测已落入可编辑区间（原声 "
                f"{float(final_audit.get('source_seconds') or 0.0):.1f} 秒），"
                "本次不发第三遍 AI；章过薄/长连句/语义跨度等质量项仅保留告警。"
            )
        if (
            final_audit["needs_calibration"]
            and enable_duration_calibration
            and not _calibration_entry_blocked(final_audit)
        ):
            calibration["attempted"] = True
            if check_product:
                if bool(final_audit.get("main_product_pool_cannot_reach_minimum")):
                    calibration["main_product_pool_limited"] = True
                    log(
                        "时长闸门：主商品的可用原话本身不足交付下限，本轮校准只允许从主商品原话补，"
                        "不得用其他商品原话凑时长；补到主商品上限后如实标注素材不足。"
                    )
                else:
                    log(
                        "时长闸门：主商品仍有 "
                        f"{float(final_audit.get('main_product_unused_seconds') or 0.0):.1f} 秒未用原话，"
                        "本轮校准只允许从主商品原话补足到 source_min。"
                    )
            if stage_progress_hook:
                stage_progress_hook("duration_calibration_started")
            # One bounded return to the SAME Casting step. No new planner,
            # semantic filters, code-selected insertions, or retry loop.
            main_product_facts: list[str] = []
            if check_product:
                _main_pool_seconds = float(final_audit.get("main_product_pool_seconds") or 0.0)
                _main_pool_used = float(final_audit.get("main_product_selected_seconds") or 0.0)
                _main_pool_unused = float(final_audit.get("main_product_unused_seconds") or 0.0)
                _main_pool_shortfall = float(final_audit.get("main_product_shortfall_seconds") or 0.0)
                if bool(final_audit.get("main_product_pool_cannot_reach_minimum")):
                    main_product_facts.append(
                        f"主商品口径（本次硬约束）：属于主商品的可用原话只有 {_main_pool_seconds:.1f} 秒，"
                        f"本身低于 source_min，还差 {_main_pool_shortfall:.1f} 秒补不上。"
                        f"本轮只准从这 {_main_pool_unused:.1f} 秒未用的主商品原话里补；"
                        "补到主商品上限后如实写 source_material_limited/natural_complete_below_target，"
                        "绝不能用其他商品段落或其他商品上下文（[context ID]）的原话凑数。"
                    )
                elif _main_pool_unused > 0.5:
                    main_product_facts.append(
                        f"主商品口径（本次硬约束）：属于主商品的可用原话共 {_main_pool_seconds:.1f} 秒，"
                        f"已选 {_main_pool_used:.1f} 秒，还有 {_main_pool_unused:.1f} 秒未用——"
                        "这就是本轮允许补充的全部来源。只能从这里补新的购买价值，直到实测累计达到 source_min；"
                        "不得引用其他商品段落或其他商品上下文（[context ID]）的原话，也不得换一种说法写进去。"
                    )
            correction_prompt = cast_prompt + "\n\n" + "\n".join([
                "这是唯一一次时长/章节闭合校准，不重做故事和开场。上次返回如下：",
                json.dumps(_compact_casting_revision_receipt(cast_payload), ensure_ascii=False, separators=(",", ":")),
                "程序按真实字幕逐ID实测如下。它优先于你上次估算的总秒数和 pass：",
                json.dumps(_compact_duration_calibration_feedback(final_audit), ensure_ascii=False, separators=(",", ":")),
                "时长达标只计算不同ID：同一句重复播放不增加可用内容。duplicate_subtitle_ids 必须由你决定保留在哪一章，其余位置选择新的必要原话或主动删除；禁止把同一个ID再分配到后面的章节。程序不会替你删改。每章 budget_gap_seconds 已扣除前章占用的ID，不能拿 source_seconds 的重复播放秒数宣称达标。",
                "保留已选开场组合、core_desire 和冻结章节顺序。只在同一完整安全池重新选句，不从备用TopK挑选。",
                "ID 纪律（程序会逐句剔除违规引用，违规过多整版会被作废）：只能引用上面安全池列出过的 ID；每个 Beat 只放 1 个 ID；若写起止 ID 则两者必须相同（start_id 与 end_id 一致）；新增句必须是主商品的原话，不得引用其他商品段落的句子。",
                *main_product_facts,
                "偏短时逐章检查 completion_requirements：是否只有结论、漏掉理由/证明/收尾？寻找能讲透本章的新原话或必要上下句。偏长时由你删去重复或非必要内容，不能截断句子。",
                "按 source_target 预算返回完整修订片单而非增量列表。本次以**只增不减**为原则：除商品归属冲突或重复 ID 外，不得删除上次已选的句子，目标是让程序实测的总秒数比上次更长。仍用1-5秒短Beat，不合并长段、不重用ID、不慢放。不得为秒数加入无关卖点或跨商品效果。",
                "逐章对照 budget_gap_seconds，不要再次只改停止理由或增加两三句就交卷。每一章的 gap_beats 是按你已选句子的真实平均长度算出的「补齐本章进度线还需要的片段数」，min_beats_for_source_min_at_current_pace 是整片达到 source_min 所需的片段数下限——**达到下限前不得交卷**，它们不是参考值。若章预算合计本身不足 source_target，可由你在同一故事内重新分配章节深度，但不改章节顺序。",
                "缺口优先用必要上下文补全微叙事：例如‘因为’需要原因、问题需要答案、‘要么’需要完整穿法、‘这个效果’需要具体结果。已有结论的同义句、同一身高反复好看、同一个定制面料重复口号不算新增证据，不能用于校准。",
                "确实找不到新价值时保留自然完整的章节，逐章说明缺少什么真实证据及未达目标原因。没有下一轮校准，不要虚报秒数或达标。",
            ])
            _missing_chapters = [
                str(value) for value in (
                    list(final_audit.get("empty_chapter_ids") or [])
                    + list(final_audit.get("undersized_chapter_ids") or [])
                ) if str(value).strip()
            ]
            if _missing_chapters:
                correction_prompt += (
                    "\n必须补齐的章节（程序实测，硬要求）："
                    + "、".join(dict.fromkeys(_missing_chapters))
                    + "。这些章节要么一句都没交、要么连本章进度线的一半都没到；"
                    "不许留空章、不许只改停止理由或加两三句就交卷，必须逐章用主商品原话补齐到位。"
                )
            if _product_conflict_needs_attention(product_audit):
                correction_prompt += "\n商品归属核对（优先修复，不为时长保留错误商品）：" + json.dumps(_compact_product_calibration_feedback(product_audit), ensure_ascii=False, separators=(",", ":"))
                correction_prompt += "\n由你从同一完整池重新选符合主商品的原话；程序不删不换句。若开场本身属于错误商品，本次允许由你重选开场。其他正确故事职责和顺序保持。所有已选句必须有真实商品指代依据。"
            try:
                revised_raw = _post_two_pass_director_request_with_retry(
                    api_key=api_key, base_url=base_url, model=director_model,
                    system_prompt=TWO_PASS_CAST_SYSTEM_PROMPT, user_prompt=correction_prompt,
                    stage="Director_duration_calibration",
                    max_tokens=director_casting_output_max_tokens(director_plan_count, target_duration),
                    timeout=director_casting_request_timeout(director_model, timeout),
                    response_hook=(
                        lambda value: stage_response_hook("duration_calibration", value)
                    ) if stage_response_hook else None,
                )
                if stage_response_hook:
                    stage_response_hook("duration_calibration", revised_raw)
                revised = _parse_director_stage_json_with_recovery(
                    revised_raw,
                    api_key=api_key,
                    base_url=base_url,
                    model=director_model,
                    stage="Director_duration_calibration",
                    system_prompt=TWO_PASS_CAST_SYSTEM_PROMPT,
                    max_tokens=director_casting_output_max_tokens(director_plan_count, target_duration),
                    timeout=director_casting_request_timeout(director_model, timeout),
                    log_fn=log,
                    stage_response_hook=stage_response_hook,
                )
                revised_story = story_payload
                if product_audit.get("scope_errors") and isinstance(revised.get("corrected_story"), Mapping):
                    corrected = dict(revised["corrected_story"])
                    if not scope_errors(corrected.get("product_scope"), product_target):
                        revised_story = {"strategies": [corrected]}
                revised_audit = build_director_duration_audit(casting_payload=revised, **{**audit_args, "story_contract": revised_story})
                if check_product:
                    _attach_main_product_pool_audit(revised_audit, story_contract=revised_story, subtitles=subtitles,
                                                    executable_subtitle_ids=executable_subtitle_ids,
                                                    output_speed_factor=output_speed_factor)
                structure_errors = _duration_calibration_structure_errors(story_payload, cast_payload, revised, revised_audit)
                revised_product_audit = audit_product_selection(_two_pass_primary(revised_story), _two_pass_primary(revised), target=product_target, subtitles=identity_source) if check_product else {}
                if product_audit.get("scope_errors"):
                    structure_errors = [e for e in structure_errors if e not in {"changed_frozen_chapter_order", "changed_existing_opening"}]
                elif product_audit.get("conflicting_subtitle_ids"):
                    first_beats = _two_pass_beat_rows(_two_pass_primary(cast_payload))
                    opening_ids = list((_two_pass_primary(cast_payload).get("opening_selection") or {}).get("selected_subtitle_ids") or (first_beats[0].get("subtitle_ids") if first_beats else []) or [])
                    if set(opening_ids) & set(product_audit["conflicting_subtitle_ids"]):
                        structure_errors = [e for e in structure_errors if e != "changed_existing_opening"]
                if _product_conflict_needs_attention(revised_product_audit):
                    structure_errors.append("product_scope_conflict")
                if not _duration_revision_improves(final_audit, revised_audit):
                    structure_errors.append("duration_revision_not_improved")
                _over_max = _calibration_revision_exceeds_max(final_audit, revised_audit)
                if _over_max:
                    structure_errors.append("calibration_exceeds_source_max")
                    calibration["rejected_over_max"] = _over_max
                    log(
                        "时长校准验收闸：修订版实测原声 "
                        f"{_over_max['revised_source_seconds']:.1f} 秒，超过合同上限 "
                        f"{_over_max['source_max']:.1f} 秒（原稿 {_over_max['initial_source_seconds']:.1f} 秒），"
                        "整版拒收，保留上一版可编辑方案。"
                    )
                # A longer revision that only trips over a few unusable ids or a
                # few other-product sentences must not be discarded whole: drop
                # exactly those sentences, re-measure, and keep the revision when
                # the cleaned version is still an improvement.
                _sanitizable = {"invalid_source_references", "product_scope_conflict",
                                "duration_revision_not_improved"}
                if structure_errors and set(structure_errors) <= _sanitizable:
                    sanitize_drops = set(revised_audit.get("invalid_subtitle_ids") or ())
                    sanitize_drops.update(revised_product_audit.get("conflicting_subtitle_ids") or ())
                    # 2026-10-05（David 定调）：章名的唯一字面依据不许被清洗删掉。
                    _title_guard = _chapter_title_evidence_guard(
                        revised, revised_story, subtitles, sanitize_drops)
                    sanitize_drops = _title_guard["drop_ids"]
                    for _title_warning in _title_guard["warnings"]:
                        log("章名依据保护：" + _title_warning)
                    sanitized_payload, sanitize_tally = _sanitize_duration_revision(
                        revised, drop_ids=sorted(sanitize_drops))
                    if sanitize_tally["dropped_ids"] or sanitize_tally["split_beats"]:
                        sanitized_audit = build_director_duration_audit(
                            casting_payload=sanitized_payload, **{**audit_args, "story_contract": revised_story})
                        sanitized_product_audit: Mapping[str, Any] = {}
                        if check_product:
                            _attach_main_product_pool_audit(
                                sanitized_audit, story_contract=revised_story, subtitles=subtitles,
                                executable_subtitle_ids=executable_subtitle_ids,
                                output_speed_factor=output_speed_factor)
                            sanitized_product_audit = audit_product_selection(
                                _two_pass_primary(revised_story), _two_pass_primary(sanitized_payload),
                                target=product_target, subtitles=identity_source)
                        if (sanitized_audit.get("technical_valid")
                                and _product_conflict_clear(sanitized_product_audit)
                                and _duration_revision_improves(final_audit, sanitized_audit)):
                            revised = sanitized_payload
                            revised_audit = sanitized_audit
                            revised_product_audit = sanitized_product_audit
                            structure_errors = []
                            calibration["sanitized_revision"] = dict(sanitize_tally)
                            log("时长校准修订含个别越界/跨商品句，已逐句剔除后重新实测；清理后仍更接近目标，采纳该修订。")
                        else:
                            calibration["sanitized_revision"] = {**sanitize_tally, "kept": False}
                calibration["revision_audit"] = revised_audit
                if check_product:
                    calibration["product_revision_audit"] = revised_product_audit
                if structure_errors:
                    calibration["fallback_reason"] = ",".join(structure_errors)
                else:
                    cast_payload, final_audit = revised, revised_audit
                    story_payload, product_audit = revised_story, revised_product_audit
                    calibration["accepted_revision"] = True
                    # 修订版是一条全新片单：采纳后也必须过一遍确定性补齐/门禁，
                    # 否则「采纳修订」等于把拆窗补齐、整窗整取、首尾收口全丢掉。
                    try:
                        calibration["adopted_repairs"] = _apply_cast_deterministic_repairs(
                            cast_payload, subtitles, format_executable_ids,
                            budget_seconds=_whole_keep_budget, log_fn=log,
                        )
                        final_audit = build_director_duration_audit(
                            casting_payload=cast_payload,
                            **{**audit_args, "story_contract": story_payload})
                        if check_product:
                            _attach_main_product_pool_audit(
                                final_audit, story_contract=story_payload, subtitles=subtitles,
                                executable_subtitle_ids=executable_subtitle_ids,
                                output_speed_factor=output_speed_factor)
                            product_audit = audit_product_selection(
                                _two_pass_primary(story_payload), _two_pass_primary(cast_payload),
                                target=product_target, subtitles=identity_source)
                        calibration["revision_audit"] = final_audit
                    except Exception as _adopt_exc:  # noqa: BLE001 - 绝不阻断导演
                        log(f"修订版确定性补齐异常（已跳过）：{type(_adopt_exc).__name__}: {_adopt_exc}")
            except (AnalyzerError, ValueError, TypeError, KeyError) as error:
                # Keep an editable first draft on an optional correction failure.
                # Avoid storing provider text, which can contain request details.
                calibration["fallback_reason"] = type(error).__name__
                log("时长校准未完成，保留首次可编辑方案；未达目标将明确显示。")
            if stage_progress_hook:
                stage_progress_hook("duration_calibration_completed")
        duration_control = {
            "version": "director-duration-v3", "initial": initial_audit,
            "final": final_audit, "duration_fill": duration_fill_control, "calibration": calibration,
            "chapter_revision": chapter_revision_audit,
            "semantic_call_count": 3 if calibration["attempted"] else 2,
            "status": "target_range_fulfilled" if final_audit["target_range_fulfilled"] else "target_not_met_editable",
            "main_product_pool_gate": _main_product_pool_gate(final_audit) if check_product else {},
        }
        # 2026-09-23（David 定调）：程序硬去重——重复句只保留首次出现。
        # 去重后按同一口径重新实测，交付数据与真实成片一致（达标判定本就按去重口径）。
        cast_payload, _dedup_removed_ids = _dedupe_repeated_beats(cast_payload)
        if _dedup_removed_ids:
            final_audit = build_director_duration_audit(
                casting_payload=cast_payload, **{**audit_args, "story_contract": story_payload})
            if check_product:
                _attach_main_product_pool_audit(
                    final_audit, story_contract=story_payload, subtitles=subtitles,
                    executable_subtitle_ids=executable_subtitle_ids,
                    output_speed_factor=output_speed_factor)
                product_audit = audit_product_selection(
                    _two_pass_primary(story_payload), _two_pass_primary(cast_payload),
                    target=product_target, subtitles=identity_source)
            duration_control["final"] = final_audit
            duration_control["dedup_removed_ids"] = sorted(set(_dedup_removed_ids))
            duration_control["status"] = (
                "target_range_fulfilled" if final_audit["target_range_fulfilled"]
                else "target_not_met_editable")
            duration_control["main_product_pool_gate"] = (
                _main_product_pool_gate(final_audit) if check_product else {})
            log(
                f"程序去重：删除了 {len(set(_dedup_removed_ids))} 个重复句（只保留首次出现），"
                f"重新实测后不同原句 {float(final_audit.get('unique_source_seconds') or 0.0):.1f} 秒。")
        # 2026-10-05（David 定调）：商品归属未核实的句子——程序自己已经把判决算出来
        # 了（audit_product_selection 的 conflicting_subtitle_ids），但此前只写成一条
        # warning，句子照样进成片。这里把判决接成执行：逐句剔除 → 按同一口径重新实测；
        # 只在「技术有效 + 不产生空章 + 仍达 source_min」时采纳，否则保留原样并照旧出
        # 警告——绝不用删句去换取时长不达下限。同一素材实测：主方案 30 拍 75.2s →
        # 26 拍 65.2s，七章全部保留，仍高于下限 38.4s。
        product_conflict_removed: dict[str, Any] = {}
        _title_kept_ids: list[int] = []
        if check_product and product_audit.get("status") == "conflict":
            _conflict_ids = sorted({
                int(value) for value in (product_audit.get("conflicting_subtitle_ids") or ())
                if str(value).strip().lstrip("-").isdigit()
            })
            if _conflict_ids:
                # 2026-10-05（David 定调）：章名的唯一字面依据不许被硬门删掉。
                _title_guard = _chapter_title_evidence_guard(
                    cast_payload, story_payload, subtitles, set(_conflict_ids))
                _conflict_ids = sorted(_title_guard["drop_ids"])
                _title_kept_ids = list(_title_guard["protected_ids"])
                for _title_warning in _title_guard["warnings"]:
                    log("章名依据保护：" + _title_warning)
            if _conflict_ids:
                _gate_payload, _gate_tally = _sanitize_duration_revision(
                    cast_payload, drop_ids=_conflict_ids)
                _gate_drop_ids = sorted({int(v) for v in _gate_tally.get("dropped_ids") or ()})
                if _gate_drop_ids:
                    _gate_audit = build_director_duration_audit(
                        casting_payload=_gate_payload,
                        **{**audit_args, "story_contract": story_payload})
                    _attach_main_product_pool_audit(
                        _gate_audit, story_contract=story_payload, subtitles=subtitles,
                        executable_subtitle_ids=executable_subtitle_ids,
                        output_speed_factor=output_speed_factor)
                    _gate_product_audit = audit_product_selection(
                        _two_pass_primary(story_payload), _two_pass_primary(_gate_payload),
                        target=product_target, subtitles=identity_source)
                    _gate_source_min = float(
                        dict(_gate_audit.get("duration_contract") or {}).get("source_min") or 0.0)
                    _gate_seconds = float(_gate_audit.get("unique_source_seconds") or 0.0)
                    _gate_ok = (
                        bool(_gate_audit.get("technical_valid"))
                        and not (_gate_audit.get("empty_chapter_ids") or ())
                        and (_gate_source_min <= 0.0 or _gate_seconds >= _gate_source_min)
                    )
                    product_conflict_removed = {
                        "removed_subtitle_ids": _gate_drop_ids,
                        "removed_beats": int(_gate_tally.get("removed_beats") or 0),
                        "kept": bool(_gate_ok),
                        "seconds_before": float(final_audit.get("unique_source_seconds") or 0.0),
                        "seconds_after": _gate_seconds,
                        "source_min": _gate_source_min,
                        "empty_chapter_ids": list(_gate_audit.get("empty_chapter_ids") or ()),
                    }
                    if _gate_ok:
                        cast_payload = _gate_payload
                        final_audit = _gate_audit
                        product_audit = _gate_product_audit
                        duration_control["final"] = final_audit
                        duration_control["status"] = (
                            "target_range_fulfilled" if final_audit["target_range_fulfilled"]
                            else "target_not_met_editable")
                        duration_control["main_product_pool_gate"] = (
                            _main_product_pool_gate(final_audit) if check_product else {})
                        log(
                            "商品归属硬门：剔除 "
                            f"{len(_gate_drop_ids)} 个程序判为归属未核实的句子（"
                            + ", ".join(str(value) for value in _gate_drop_ids[:12])
                            + f"），重新实测后不同原句 {_gate_seconds:.1f} 秒。")
                    else:
                        product_conflict_removed["kept_reason"] = "warn_only_would_break_delivery"
                        log(
                            "商品归属硬门：剔除 "
                            f"{len(_gate_drop_ids)} 句会（技术无效/空章/低于下限）破坏交付，"
                            "保留原句并照旧列出归属警告。")
        if _title_kept_ids:
            product_conflict_removed["title_evidence_kept_ids"] = list(_title_kept_ids)
        if product_conflict_removed:
            duration_control["product_conflict_gate"] = product_conflict_removed
        if stage_response_hook:
            stage_response_hook("duration_control", json.dumps(duration_control, ensure_ascii=False, indent=2))
        if check_product:
            product_control = {
                "initial": initial_product_audit, "final": product_audit, "target": product_target,
                "initial_story_scope_errors": identity_errors,
                "source_context_count": len(identity_source), "executable_short_pool_count": len(casting_rows),
                "blocking": False,
                "preview_status": (
                    "preview_ready_with_warnings"
                    if product_audit.get("status") == "conflict" else "preview_ready"
                ),
            }
            if product_conflict_removed:
                product_control["conflict_hard_gate"] = product_conflict_removed
            if product_audit.get("status") == "conflict":
                _unverified_ids = list(product_audit.get("conflicting_subtitle_ids") or ()) + list(
                    product_audit.get("soft_flagged_subtitle_ids") or ())
                details = "；".join(product_audit.get("scope_errors") or []) or f"字幕 {product_audit.get('conflicting_subtitle_ids')} 的商品归属未核实"
                product_control["warnings"] = [details]
                log(f"主商品核对有疑点，已保留完整可编辑方案：{details}")
            if bool(final_audit.get("main_product_pool_cannot_reach_minimum")):
                limited_note = (
                    "主商品可用原话不足目标时长：主商品池仅 "
                    f"{float(final_audit.get('main_product_pool_seconds') or 0.0):.1f} 秒，低于本次下限，"
                    "已按主商品上限交付，未用其他商品补时长。"
                )
                product_control.setdefault("warnings", []).append(limited_note)
                log(limited_note)
            if stage_response_hook:
                stage_response_hook("product_control", json.dumps(product_control, ensure_ascii=False, indent=2))
        normalized_payload = _normalize_two_pass_director_payload(
            story_payload,
            cast_payload,
            casting_rows=casting_rows,
            product_context_rows=identity_source,
            opening_hook_pool=opening_hook_prompt_rows,
        )
        normalized_primary = normalized_payload["strategies"][0]
        normalized_primary.setdefault("whole_video_audit", {})["duration_control"] = duration_control
        if check_product:
            raw_story_rows = story_payload.get("strategies") or ()
            raw_cast_rows = cast_payload.get("strategies") or ()
            if isinstance(raw_story_rows, Mapping):
                raw_story_rows = (raw_story_rows,)
            if isinstance(raw_cast_rows, Mapping):
                raw_cast_rows = (raw_cast_rows,)
            story_by_id = {
                str(item.get("strategy_id") or f"S{index}"): dict(item)
                for index, item in enumerate(raw_story_rows, 1) if isinstance(item, Mapping)
            }
            cast_by_id = {
                str(item.get("strategy_id") or f"S{index}"): dict(item)
                for index, item in enumerate(raw_cast_rows, 1) if isinstance(item, Mapping)
            }
            for index, normalized_strategy in enumerate(normalized_payload.get("strategies") or (), 1):
                if not isinstance(normalized_strategy, Mapping):
                    continue
                strategy_id = str(normalized_strategy.get("strategy_id") or f"S{index}")
                if index == 1:
                    strategy_product_control = product_control
                else:
                    strategy_product_audit = audit_product_selection(
                        story_by_id.get(strategy_id, {}),
                        cast_by_id.get(strategy_id, {}),
                        target=product_target,
                        subtitles=identity_source,
                    )
                    strategy_product_control = {
                        "initial": strategy_product_audit,
                        "final": strategy_product_audit,
                        "target": product_target,
                        "source_context_count": len(identity_source),
                        "executable_short_pool_count": len(casting_rows),
                    }
                audit = normalized_strategy.setdefault("whole_video_audit", {})
                audit["product_control"] = strategy_product_control
                if str(dict(strategy_product_control.get("final") or {}).get("status") or "") == "conflict":
                    audit["ai_reported_status"] = audit.get("status")
                    audit["status"] = "needs_review"
        if (
            not final_audit["target_range_fulfilled"]
            or final_audit["incomplete_chapter_ids"]
            or final_audit["unverified_completion_chapter_ids"]
            or final_audit["duplicate_subtitle_ids"]
            or final_audit["empty_chapter_ids"]
            or final_audit["undersized_chapter_ids"]
        ):
            video_audit = normalized_primary["whole_video_audit"]
            video_audit["ai_reported_status"] = video_audit.get("status")
            video_audit["status"] = "needs_review"
        raw = json.dumps(normalized_payload, ensure_ascii=False)
        if raw_response_hook:
            # The compatibility artifact remains parseable as the final exact
            # source packet, while the two raw responses have their own files.
            raw_response_hook(raw)
    else:
        user_prompt = build_analyzer_user_prompt(
            product=product,
            subtitles=subtitles,
            content_contract=content_contract,
            commercial_assets=commercial_assets,
            executable_subtitle_ids=executable_subtitle_ids,
            target_duration=target_duration,
            director_focus=director_focus,
        )
        depth_mode = director_duration_depth_contract(target_duration)["mode"]
        effective_max_tokens = max(
            int(max_tokens),
            6000 if depth_mode in {"deep", "long"} else 5000 if depth_mode == "standard" else 4000,
        )
        raw = _post_analyzer_request(
            **{
                "api_key": api_key,
                "base_url": base_url,
                "model": director_model,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "top_p": top_p,
                "max_tokens": effective_max_tokens,
                "timeout": timeout,
            }
        )
        if raw_response_hook:
            raw_response_hook(raw)
    result = parse_strategy_result(
        raw,
        product=product,
        subtitles=subtitles,
        target_duration=target_duration,
        content_contract=content_contract,
        commercial_assets=commercial_assets,
    )
    # 挂载独立 Hook 召回结果（必须在 result 生成之后；此前放在 parse 之前，
    # 一直被 except 静默吞掉，导致 P0-a 召回结果其实没挂上）。
    if opening_hook_payload is not None:
        try:
            result.opening_hook_recall = opening_hook_payload
        except Exception:
            log("opening_hook_recall 无法挂到结果对象（只读）")
    log(
        f"Commercial Story Analyzer: product={result.product or '-'} "
        f"strategies={len(result.strategies)}"
    )
    for s in result.strategies:
        log(
            f"  [{s.strategy_id}] {s.type}: strength={s.story_strength} "
            f"material={s.material_sufficiency} contract={s.contract_compatibility}"
            f"({s.strategy_viability}) story={s.story_validity} "
            f"duration={s.duration_feasibility}/{s.recommended_duration_seconds:.1f}s | {s.thesis[:24]}"
        )
    return result
