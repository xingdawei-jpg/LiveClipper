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
import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from ai_model_config import ai_chat_completions_url
from ai_cost_ledger import record_ai_call
from director_wire_schema import WIRE_VERSION, compact_director_wire_payload, expand_director_wire_payload
from ssl_context import create_ssl_context
from director_product_contract import (
    build_product_target, scope_errors, audit_product_selection, compatible, foreign_product_ranges, PRODUCT_TYPES,
)


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
        )

    def to_dict(self) -> dict[str, Any]:
        source_span = (
            {"start_id": self.subtitle_ids[0], "end_id": self.subtitle_ids[-1]}
            if self.subtitle_ids else {}
        )
        return {
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
    recommended = round(max(5.0, min(90.0, float(evidence_duration))), 1)
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
        '           "verbatim":"<逐字复制整个W001的完整原话>"},',
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
        "每个 Beat 必须完整选择一个 surface=clean 的 spoken window；source_span 等于该窗口包含首尾的 IDs，不能截取窗口内单行。",
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

TWO_PASS_STORY_SYSTEM_PROMPT = """你是直播女装短视频的故事导演。你只负责决定这条视频为什么存在、围绕什么购买欲望、按什么顺序说服观众；本轮绝对不能选择任何具体字幕句子。

硬原则：
1. 完整阅读全部安全字幕，先比较不同故事方向，再冻结一个证据最充足、最值得发布的主故事；备选方向只保留方向卡。
2. core_desire 必须是一句观众视角的完整购买判断，不是卖点列表。
3. Opening 必须用反差、痛点、强结果或强场景让人继续看，并在最前面的必要章节中兑现，不能只铺垫。
4. 每个章节必须改变一次观众的购买判断。章节之间要有因果、追问、扩大证明、顾虑解除或使用场景关系。
5. 本轮只输出章节职责，不输出 beats、subtitle_ids、source_span、原话、时间戳或成片连读；具体选句全部留给下一遍 Beat Casting。
6. 用户禁止或限制的内容已在输入前过滤；不得猜测、依赖或为未提供内容安排故事章节。每项 completion_requirements 只写一个具体购买问题，价格、催单等禁选要求不能混入材质、设计由来等正常职责；各方案都应仅凭允许内容构成完整故事。
7. 只使用字幕明确支持的商品事实，不根据文件名或常识补写商品卖点。先确定 product_scope：主商品名称、single_product/explicit_set、搭配品使用边界。以用户主商品为先，用素材标题和字幕核对身份；若是单件，另一件只能证明搭配，不能把另一件的遮腿、裙摆、裤型等效果归给主商品。标题缺失时从字幕确定一个明确主商品，不能把同场多个商品默认拼成套装。
8. 用户时长是交付目标，不是可忽略的建议。开场悬念只安排一个可迅速兑现的问题，通常占全片10%-15%；不要以城市天气、直播热度等长铺垫代替商品故事。按原声预算为每章分配 source_budget_seconds，合计尽量接近原声目标；同时写 completion_requirements，明确问题、解释、具体证据和结论怎样闭合。长目标首先把章节讲充分，再探索服务主故事的新购买价值；不能靠同义重复、无关卖点或固定章数填满。
9. required 只给成立主故事不可缺少的章节；recommended 是素材强时值得讲的章节；optional 无增益可以主动舍弃，不要求 Q1-Q7 全覆盖。
10. 用户只要求1版时，备选方向只写标题、核心欲望和开场承诺，不设计完整章节、更不能选句。用户明确要求2-3版时，每个方案都必须成为同一商品下可独立执行的完整故事合同，拥有不同购买切入点、开场和前段章节路径；仍然不能在本阶段选句。差异不要求证据互斥，后段可共享必要的购买证明。每个方向必须有支撑本次目标时长的叙事深度，不要把一条完整购买故事拆成只讲颜色、只讲剪裁等证据不足的短版。
11. 先比较‘为什么想要’与‘已经有同类为什么还选这一件’等购买问题。选择能被干净短句和具体证据连续兑现的中心，不选择听起来宏大却需要拼凑跨商品证据的中心。不要为凑七章把同一细节改名重复讲。
12. required 章节只能承诺后来可由安全可选原话逐项兑现的内容；若只是 context 中看见、被内容合同排除、或没有可执行短句支持，应删去该承诺或明确标为 source_limited，不能把它写成完整章节。
13. 返回中的标题、职责、购买理由必须针对本素材实际填写，不得照抄 schema 的‘主视频标题’等占位文案。只返回合法、紧凑 JSON，不输出思考过程。"""


TWO_PASS_CAST_SYSTEM_PROMPT = """你是直播女装短视频的 Beat Casting 导演。上游已冻结故事，本轮只从完整安全字幕池一次选出最终真实口播。

保持执行合同的购买判断、商品边界和章节顺序；逐句选择真实原话，不改写、不跨商品、同一 ID 不重复。每个方案以真实原声时长为准，选够且不超长；逐句连读，保留必要上下句，删除重复和残句。开头须立即抓人并兑现：在同一次调用中先比较 2–3 个真实短句开场组合。每个标记 complete 的章节都必须逐项给出仅含本章已选 ID 的 completion_receipts；没有可执行证据就写 needs_context/source_limited，context ID 不能充当证据。只返回用户指定的紧凑 JSON，不输出思考过程。"""


DIRECTOR_PREFERRED_BEAT_MIN_SECONDS = 1.0
DIRECTOR_PREFERRED_BEAT_MAX_SECONDS = 5.0
DIRECTOR_LONG_COMPLETE_BEAT_MAX_SECONDS = 8.0


def director_casting_output_max_tokens(director_plan_count: int | None) -> int:
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
    return max(5200, 4000 * plan_count)


def _director_casting_rows(
    subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None,
) -> list[dict[str, Any]]:
    """Return the full short-Beat pool without making a semantic decision.

    One-to-five seconds remains the normal editing grain.  A five-to-eight
    second subtitle is kept only as an AI-visible exception so a genuinely
    complete spoken sentence is not mechanically deleted before casting.
    """
    return [
        row for row in _director_executable_subtitles(subtitles, executable_subtitle_ids)
        if DIRECTOR_PREFERRED_BEAT_MIN_SECONDS
        <= float(row["end"] - row["start"])
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
) -> str:
    """Build exact-ID casting context without making excluded rows selectable.

    Price/interaction/long rows can name a garment before a short '它' line.
    Callers that have an explicit content boundary can omit non-selectable raw
    text entirely.  That keeps a forbidden price/size/CTA sentence out of both
    Director prompts instead of merely telling the model not to select it.
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
            lines.append(_director_casting_transcript([selectable[sid]]))
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
        if executable_ids and subtitle_id not in executable_ids:
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


def build_two_pass_story_audit(
    story_contract: Mapping[str, Any],
    *,
    target_duration: float = 60.0,
    duration_tolerance: float | None = None,
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
    return {
        **director_target_duration_range(target_duration, duration_tolerance),
        "chapter_count": len(chapters),
        "coverage_counts": coverage_counts,
        "unexpected_selected_subtitle_ids": unexpected_selected_ids,
        # These are planning hints for Casting, not a semantic rejection.  A
        # later pass may still assign a shared fact to one chapter and replace,
        # merge, or remove the other chapter.
        "evidence_location_conflicts": evidence_location_conflicts,
        "warnings": warnings,
        "story_contract_valid": not warnings,
    }


_STORY_POLICY_EXTRA_PATTERNS: dict[str, tuple[str, ...]] = {
    # Story language often says “99块” or “三倍” rather than the literal
    # policy keyword “价格”; catch those promises before Casting spends a
    # request trying to make them executable.
    "price": (r"\d+(?:\.\d+)?\s*(?:元|块|块钱|倍)", r"一顿饭钱", r"定价", r"昂贵|价格不菲|帽子贵", r"[数几一二三四五六七八九十两百千]+(?:十|百|千|万)(?:元|块)", r"(?:售卖|卖到|卖)[^，。；\n]{0,6}[一二三四五六七八九两]+(?:百|千|万)", r"(?:倍率|毛利)[^，。；\n]{0,8}(?:低|压|打)", r"(?:低价|压价|控价)[^，。；\n]{0,10}(?:主推|品质|市场|成本)?"),
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
    }
    if not excluded_kinds:
        return [int(row["id"]) for row in rows], audit
    safe_ids: list[int] = []
    for row in rows:
        hits = _story_policy_hits(row.get("text"), excluded_kinds)
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
        "status": "consistent",
    }
    if not blocked_kinds and not _body_only_kinds(content_contract):
        return payload, audit
    strategies = _strategy_refs(payload)
    story_fields = (
        "director_title", "core_desire", "central_promise", "opening_promise",
        "stop_condition", "story_priority",
    )
    for strategy_index, strategy in enumerate(strategies, 1):
        strategy_id = str(strategy.get("strategy_id") or f"S{strategy_index}")
        for field in story_fields:
            hits = _story_policy_hits(strategy.get(field), blocked_kinds)
            if hits:
                strategy[field] = ""
                audit["cleared_story_fields"].append({
                    "strategy_id": strategy_id, "field": field, "blocked_kinds": hits,
                })
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
    if audit["removed_chapters"] or audit["trimmed_chapters"] or audit["cleared_story_fields"]:
        audit["status"] = "policy_trimmed"
    return payload, audit


def _story_delivery_depth_audit(
    story_contract: Mapping[str, Any],
    *,
    target_duration: float,
    duration_tolerance: float | None,
    output_speed_factor: float,
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
    minimum_chapters = max(
        1,
        math.ceil(
            int(depth["expected_total_beats"]["low"])
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
    return {
        "enforced": enforced,
        "requested_seconds": requested,
        "minimum_chapters": minimum_chapters,
        "minimum_planned_source_seconds": minimum_source_seconds,
        "strategies": strategies,
        "insufficient_strategy_ids": insufficient_ids,
        "status": "insufficient_after_policy" if insufficient_ids else "sufficient",
    }


def _expand_cast_sentence_groups(payload: dict[str, Any], subtitles: Sequence[Mapping[str, Any]], executable_ids: Sequence[int] | None = None) -> dict[str, int]:
    """Normalize AI-selected groups only; never select, drop or reorder a line."""
    import copy
    rows = {int(row["id"]): row for row in subtitles}
    allowed = set(rows) if executable_ids is None else set(executable_ids) & set(rows)
    expanded = 0
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
                        raise AnalyzerError(f"Director 选句不可执行：字幕 ID {value} 不在安全可选素材中。")
                    values.append(value)
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
    return {"expanded_groups": expanded}


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
    """Detect a malformed cast that is far beyond the editable range."""
    contract = duration_audit.get("duration_contract") or {}
    try:
        source_seconds = float(duration_audit.get("source_seconds") or 0.0)
        source_max = float(contract.get("source_max") or 0.0)
    except (TypeError, ValueError):
        return False
    return source_max > 0 and source_seconds > source_max * 1.35


def build_two_pass_draft_audit(
    *,
    initial_draft: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None = None,
    target_duration: float = 60.0,
    duration_tolerance: float | None = None,
) -> dict[str, Any]:
    """Measure the first AI draft without changing any semantic decision."""
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
    duration_range = director_target_duration_range(target_duration, duration_tolerance)
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
    if actual_seconds < float(duration_range["preferred_low"]):
        warnings.append(
            f"duration_shortfall:{float(duration_range['preferred_low']) - actual_seconds:.3f}s"
        )
    elif actual_seconds > float(duration_range["preferred_high"]):
        warnings.append(
            f"duration_overflow:{actual_seconds - float(duration_range['preferred_high']):.3f}s"
        )
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
            and float(duration_range["preferred_low"]) <= actual_seconds <= float(duration_range["preferred_high"])
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
    return {
        "duration_contract": director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor),
        "source_seconds": source_seconds,
        "unique_source_seconds": unique_seconds,
        "projected_final_seconds": round(playback_status["projected_final"], 3),
        "unique_projected_final_seconds": round(status["projected_final"], 3),
        "repeated_source_seconds": round(source_seconds - unique_seconds, 3),
        "target_range_fulfilled": bool(
            technical_valid and status["accepted"] and not measured["duplicate_subtitle_ids"]
            and not long_continuous_count and not semantic_unit_span_issue_count
        ),
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
        "unverified_completion_chapter_ids": unverified,
        # A source-limited chapter is an honest material boundary.  Asking a
        # third model call to invent its missing evidence spends money without
        # improving the editable result.
        "needs_calibration": bool(
            not status["accepted"] or retryable_incomplete or not technical_valid
            or measured["duplicate_subtitle_ids"] or long_continuous_count or semantic_unit_span_issue_count
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
        "pool_note": "未选库存仅为数量上限，不等于适合当前故事；语义价值由 AI 判断。",
    }


def _attach_main_product_pool_audit(
    audit: dict[str, Any], *, story_contract: Mapping[str, Any], subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None, output_speed_factor: float,
) -> None:
    """Narrow numeric capacity to AI-declared source product sections.

    This never casts a sentence. It prevents other-product seconds from being
    presented as available inventory for the frozen main story.
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
    foreign_ids = {sid for item in foreign_product_ranges(main_type, subtitles)
                   for sid in range(int(item["start_id"]), int(item["end_id"]) + 1)}
    main_rows = [row for row in rows if int(row["id"]) in allowed and int(row["id"]) not in foreign_ids]
    seconds = round(sum(float(row["end"]) - float(row["start"]) for row in main_rows), 3)
    speed = max(0.1, float(output_speed_factor or 1.0))
    audit["main_product_pool_count"] = len(main_rows)
    audit["main_product_pool_seconds"] = seconds
    audit["main_product_pool_upper_bound_final_seconds"] = round(seconds / speed, 3)
    source_min = float(dict(audit.get("duration_contract") or {}).get("source_min") or 0)
    audit["main_product_pool_cannot_reach_minimum"] = bool(sections and seconds < source_min)


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
    actual_ids = [i for beat in beats for i in (beat.get("subtitle_ids") or [])]
    # An advisory opening receipt sometimes disagrees with executed beats.
    # Preserve the actual audible opening, not IDs that were never selected.
    if not opening or actual_ids[:len(opening)] != list(opening):
        opening = list(beats[0].get("subtitle_ids") or []) if beats else []
    if opening and list(audit["selected_subtitle_ids"][:len(opening)]) != list(opening):
        errors.append("changed_existing_opening")
    return errors


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
    keys = (
        "source_seconds", "unique_source_seconds", "projected_final_seconds",
        "unique_projected_final_seconds", "target_range_fulfilled",
        "shortfall_source_seconds", "excess_source_seconds",
        "incomplete_chapter_ids", "unverified_completion_chapter_ids", "invalid_subtitle_ids", "duplicate_subtitle_ids",
        "technical_issues", "planned_chapter_budget_seconds",
        "estimated_beat_count_at_current_pace", "unused_pool_count",
        "unused_pool_seconds", "main_product_pool_count", "main_product_pool_seconds",
        "main_product_pool_cannot_reach_minimum",
    )
    return {
        "duration_contract": dict(audit.get("duration_contract") or {}),
        **{key: audit.get(key) for key in keys if key in audit},
        "chapters": chapters,
    }


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
) -> str:
    """Build the story-only call over the complete executable transcript."""
    rows = _director_casting_rows(subtitles, executable_subtitle_ids)
    transcript = _director_story_transcript(rows)
    duration_range = director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor)
    depth_contract = director_duration_depth_contract(target_duration)
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
            "director_title": "主视频标题",
            "core_desire": "观众看完后形成的一句购买欲望",
            "central_promise": "整条视频只证明的一件事",
            "product_scope": {
                "main_product": "字幕核实的当前主商品",
                "product_type": "/".join(PRODUCT_TYPES),
                "target_confirmation": "match/ambiguous/not_found；match表示与用户目标及原字幕一致",
                "identity_evidence_ids": [1],
                "selection_basis": "全片主要展示/讲解的是谁；哪些只是短暂搭配提及，不按强句分数或一次提词认主商品",
                "sales_scope": "single_product/explicit_set",
                "supporting_products_rule": "其他商品仅可怎样支持主商品；不得归因哪些效果",
                "source_product_sections": [{"start_id": 1, "end_id": 20, "product_type": "tshirt",
                                             "subject_product": "该原片范围实际讲的商品；无法确认写unknown，不套用主商品",
                                             "identity_evidence_ids": [1]}],
            },
            "opening_promise": "开头为什么能停人以及立即兑现什么",
            "narrative_archetype": "最合适的叙事原型",
            "video_structure": {
                "id": "结构 id",
                "name": "结构名称",
            },
            "chapter_packets": [{
                "chapter_id": "C1",
                "chapter_kind": "pain/result/mechanism/fit/comfort/risk/styling/scene/trust",
                "title": "章节名",
                "purchase_question_id": "Q1-Q7",
                "buyer_advance": "这一章带来的新购买认知",
                "coverage": "required/recommended/optional",
                "chapter_job": "这一章必须怎样把上一章推进到下一章",
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
    # The complete safe transcript deliberately comes first.  It is already
    # the full M1 input, so do not duplicate it as a second product-context
    # transcript.  That saves prompt tokens and prevents excluded raw content
    # from leaking back into the story chain.
    return "\n".join([
        "完整安全可执行字幕按时间顺序（没有 Strong Ranking、没有 TopK）：每个 [ID] 都可选。受用户内容边界排除的原话不会提供，不能作为故事承诺。",
        transcript or "（没有满足 1-8 秒且可执行的字幕）",
        "",
        subject_line,
        _story_content_boundary_prompt(content_contract),
        _director_controls_prompt(director_controls, stage="story"),
        focus_line,
        (
            f"本次用户明确要求 {float(duration_range['requested_seconds']):.0f} 秒；"
            f"期望成片区间为 {float(duration_range['preferred_low']):.1f}-"
            f"{float(duration_range['preferred_high']):.1f} 秒。原声选句预算见下；不能拿半句或重复内容填充。"
        ),
        "交付时长合同（含导出变速，原声预算可以超过120秒）：" + json.dumps(duration_range, ensure_ascii=False),
        "为每个完整方案的每章填写 source_budget_seconds 和 completion_requirements，各方案分别逐章加总，预算合计应接近 source_target（不能只凑到下限或把成片秒数当原声预算）；不要只写六七个章名，合计却只够半条片。长目标需要更充分的具体解释、证据和不同使用问题，不是重复口号。预算要有完整字幕中的真实证据支持；不足时明确说明缺少哪类真实内容。",
        (
            f"本次时长要求至少形成 {math.ceil(int(depth_contract['expected_total_beats']['low']) / max(1, int(depth_contract['beats_per_chapter']['high'])))} 个"
            "彼此推进的安全章节；若素材无法支撑，明确 source_limited，不能用未提供内容补足。"
        ),
        "本轮先决定观众为什么想买，再按观众自然追问安排章节。每章 buyer_advance 必须写出与上一章不同的新增购买认知；如果两个章节只能靠同一句原话或同一结论才能成立，就在本轮合并，而不是换标题重复讲。每章 completion_requirements 只能有一项：它是本章唯一不可缺的、可由一组完整短语义直接核验的购买判断。不要把颜色、材质、版型、搭配、物流等多个独立事实塞进同一章；它们各自只能在有独立推进时成为另一章。支持这个判断的补充证明不另写成 needs。evidence_locations 建议列1-3个本轮安全池代表ID，仅定位事实，不是最终片单；必要时可多列，没证据写空数组并说明缺口，不编造ID。先顺读证据及必要上下句，确认真实口播能完整讲出问题、解释和结论后再承诺章节；标题中的每个核心承诺都必须有完整证据链，例如承诺版本对比时必须同时存在版本身份、差异和最终结论。chapter_job 简短说明本章回答什么，以及怎样承接上一章；不强套固定问题顺序。",
        (
            f"用户本次要求 {plan_count} 个成片版本。请一次返回恰好 {plan_count} 个完整且明显不同的导演故事合同；"
            "每个方案都必须拥有自己的 core_desire、opening_promise、video_structure 和完整 chapter_packets，"
            "不能只是同一章节换顺序。第一项为 AI 推荐主方案，其余为可直接执行的 alternative。"
            if plan_count > 1 else
            "本次只执行一个完整主方案；必须同时给出恰好 2 个仅有标题、核心购买理由和开场承诺的备选方向摘要（S2、S3）。"
            "不得为 S2/S3 生成 chapter_packets、选片、字幕或审计字段；用户确认选择后才会单独为所选方向生成完整方案。"
        ),
        "先核实 product_scope 再编故事：整体主讲时段、反复展示对象与用户指定商品优先；30分钟里两句裤子不能因为卖点强就成为主商品。开场必须来自已核实的主商品范围，并在紧接的真实口播中兑现承诺；找不到可兑现的反差开场时，直接用主商品最强结果或机制开场，不能用错误商品、泛情绪或无答案的质疑冒充。identity_evidence_ids 只是身份依据，不是选片；所有备选方向也必须是同一个主商品。",
        "identity_evidence_ids 只列3-6条分布在不同位置、能明确核实商品名/指代的代表依据，不要抄全片ID。每条 Beat 的 product_evidence_ids 只需1-2个最直接的指代依据。",
        "先顺读全片，在 product_scope.source_product_sections 用连续ID范围记录换品：start_id/end_id 是原片归属边界，不是选片。覆盖全片且不重叠；重新回到同款要另开范围。临时聊裤子、另一件羊毛衣、与商品无关的聊天都不能默认属于T恤。范围内 product_type/subject_product 记录实际讲述对象，证据不足用unknown；单句讲其他商品的自身优点不能包装成主商品的搭配支持。",
        "长目标通过探索更多真实存在的新购买章节来体现，禁止重复同一结果、同一机制或同义口号。",
        "chapter_packets 只能描述章节职责；JSON 中不得出现 beats、subtitle_ids、source_span、verbatim、时间戳或 final_readthrough。",
        "可选视频结构仅供导演判断，不需要逐个覆盖：",
        json.dumps(available_video_structures(content_contract), ensure_ascii=False, separators=(",", ":")),
        "返回结构：",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "",
        "返回前确认：core_desire 不是卖点清单；每章服务同一故事且有真实证据位置；章节之间存在明确说服关系；证据定位不代表已选择最终口播。只返回紧凑 JSON。",
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
    audit = dict(story_audit or {})
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
) -> str:
    """Build the single exact-Beat casting call over the complete pool."""
    rows = _director_casting_rows(subtitles, executable_subtitle_ids)
    duration_range = director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor)
    depth_contract = director_duration_depth_contract(target_duration)
    execution_contract = _casting_execution_contract(
        story_contract,
        duration_range=duration_range,
        story_audit=story_audit or draft_audit,
    )
    mean_seconds = sum(float(row["end"] - row["start"]) for row in rows) / max(1, len(rows))
    pacing_reference = {
        "pool_mean_seconds": round(mean_seconds, 3),
        "approximate_beats_for_target": math.ceil(float(duration_range["source_target"]) / mean_seconds) if mean_seconds > 0 else None,
        "reference_only": True,
    }
    for strategy in execution_contract["strategies"]:
        for chapter in strategy["chapters"]:
            chapter["approximate_beats"] = math.ceil(float(chapter["budget"] or 0) / mean_seconds) if mean_seconds > 0 else None
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
                "selected_subtitle_ids": [101, 102],
            },
            "chapter_packets": [{
                "chapter_id": "冻结章节 ID",
                "beats": [{
                    "beat_function": "result/mechanism/proof/experience/risk_remove/styling/scene/trust",
                    "subtitle_ids": [101],
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
    schema = compact_director_wire_payload({"strategies": casting_schemas})
    # The transcript remains complete and ordered.  The following contract is
    # deliberately a small execution receipt, not a second copy of M1 prose.
    return "\n".join([
        "完整安全可执行字幕按时间顺序：每个 [ID] 都是可选的真实短句。受用户内容边界排除的原话没有提供，不能作为选片或商品依据。1-5 秒优先，5-8 秒完整句仅作例外；没有 Strong Ranking、没有 TopK、没有卖点预分类。",
        _director_product_context(
            source_context_subtitles or subtitles,
            rows,
            include_nonselectable_context=False,
        )
        or "（没有满足 1-8 秒且可执行的字幕）",
        "",
        "第一遍的完整故事已保存在工作台，并冻结主商品、核心购买方向、整片承诺和章节推进方向。下面是本轮唯一需要执行的紧凑合同；product.ranges 是换品边界，chapters 的 advance/job/needs/budget 是第一遍的计划。你必须让最终真实口播成为可兑现的故事，不能机械执行冲突或空洞章节：",
        json.dumps(execution_contract, ensure_ascii=False, separators=(",", ":")),
        "若 story_check.overloaded_chapter_requirements 非空，第一遍把多个独立购买判断塞进了同一章。你必须在本次回复用 chapter_revision 把该章收窄为一个可由最短完整原话兑现的判断，或与相邻重复章合并/删除；不得为了逐项打回执而堆叠同义口播。",
        "budget 是本章原声目标，budget_ceiling 是本章超长预警；budget_end 是到本章结束的累计目标。budget_floor 和 budget_end_floor 仅表示整片规划深度，不构成每章最低时长命令。逐章完成取舍，避免前几章耗尽全片预算；无需换算播放速度。",
        "执行顺序：先为每章挑出能完整回答 needs 的语义单元，再用素材标注秒数核算本章与全片。偏长先删同义证明和无关铺垫，偏短优先补未讲清的解释、证据或必要上下句；不要拆散完整意思来凑秒数。完成这些取舍后才输出最终 beats，不把待精简片单当成结果。",
        "第一遍预算与证据位置只是规划参考，内容边界删章后也由你在剩余故事内重新分配深度；不能为了守住原预算而只选半句话。自然顺滑与真实新价值优先，确实无法接近目标时报告具体素材缺口。",
        "",
        f"用户内容合同：{_contract_forbidden_lines(content_contract)}",
        _director_controls_prompt(director_controls, stage="cast"),
        (
            f"第一遍已冻结 {len(executable_story_ids)} 个完整导演方案。请在这一次回复中为每个 strategy_id 分别完成完整选句、排序、开场比较和整片审阅；"
            "不得只执行主方案，也不得把其他方案退回成方向摘要。方案可共享必要证据与闭合原话，差异体现在购买切入点、开场和前段说服路径；同一原话用于不同成片不算本片无效重复，不得为了与其他版本不重样而缩短某套方案。"
            if len(executable_story_ids) > 1 else
            "只执行冻结的一个主方案。"
        ),
        "预算执行：每章先保留兑现 needs 所必需的最短完整意思，再从同一购买问题中补充真正新增的解释或证明，主动接近 budget。budget_floor 是整片深度的规划参考，不是把一章拉长或重复播放的命令；一章已经讲清时，只能由后续真实的新购买价值补回。预算允许章节间调剂，但全片必须落在 source_min/source_max 内并靠近 source_target。超过预算优先舍弃同义证明与非必要铺垫，不拆散因果或指代；不足优先补未讲明的必要解释、证明、结论和上下句，无素材才标 source_limited。逐章选择后对照 budget_end 和 budget_end_ceiling 检查累计秒数；前章偏差在本轮完成取舍，不把欠账留到输出以后。",
        "真实句长参考：" + json.dumps(pacing_reference, ensure_ascii=False, separators=(",", ":")),
        "approximate_beats 仅为预算规模参考，没有固定句数上下限。每个最终 beat 的 ids 必须恰好写一个 ID；不得把多条字幕塞进一个 ids 数组来隐藏连续长口播。字幕行不等于一句话：必要相邻句必须分别写成连续 beats，并在 semantic_units 中列成一个完整语义单元。单句或完整语义单元通常 2-5 秒；只有必要上下句才能闭合意思时才可延至 5-8 秒。超过 8 秒的连续原口播不作为成片单元；回到完整安全池另选更短、更能自立的原话，不能为凑短句截断半句话。",
        "章节兑现只看最终口播：标题和 role 标签不算证据；介绍材质不等于说明不扎，鼓励尝试不等于教会搭配，说到网眼洞口不等于已经讲出做工结论，说到某一版不等于完成版本对比。每项 needs 用最短的一个完整语义单元直接说出问题、解释或证据和结论；只有补句不可或缺时才增加相邻句。alternative_beats 不参与兑现。不要把尚未选入的关键句只放在备选里。",
        "按原字幕前后核对口语依赖：‘没有这个点’必须保留所指结论，‘因为/所以/它/那种’必须有明确对象和完整谓语。先保留必要的前后短句再检查预算；不能为限制句数跳过结论或截掉句尾。开场不得保留‘刚刚讲过了’等依赖直播现场的铺垫。",
        "全片先建立 ID 归属：同一 ID 只能放入一个最终章节，重复播放不增加内容或时长。execution_contract.evidence_conflicts 中同一事实被第一遍多个章节引用时，必须只分配给其中一章；另一章选择新的必要原话，或在本次回复中合并/取消。按最终 ID 顺序连读：保留必要上下句来闭合“因为/但是/这个效果”等依赖，删掉残句、寒暄和全片同义重复。每章必须兑现自己的 advance；optional 无新增价值可删。开场先在内部比较真实组合，再只把最强、可兑现的已执行 IDs 写入 opening_selection。",
        "evidence_locations 只是事实定位，先回查其上下文再从完整安全池选句；不得当作必选或唯一候选。若证据不支持 needs，在本次选片中如实标缺口，不用无关句冒充兑现。",
        "字幕行不等于完整语义：仅当必要相邻原字幕跨多个 beats 时才输出 semantic_units；单行完整句省略。每组只列跨行依赖的 ID 数组，按原话顺序连续出现在本章最终 beats 中，不能引用其他章或未选句，并且总原声不超过 8 秒。程序只检查声明是否完整执行，不自动补句、拼句、截断或重排。",
        "严格按 product.ranges 回查‘它/这条/这套’。开场和非 styling 章节只能选择已核实的主商品范围；未知范围或其他商品的泛情绪不能承担主商品卖点。每条最终或备选 Beat 都填写关系、实际商品、类型和 1-2 条指代依据；搭配品只能作为 styling_support，不能把它自身效果归给主商品。",
        "本次只返回最终可执行 beats，不返回 alternative_beats、候选开场、比较理由或候选清单。每个标记 complete 的 chapter 的每项 needs 都用 completion_receipts 逐项列出 requirement_index（从 1 开始）和本章最短已选 semantic unit 的 subtitle_ids；回执只写数字，不写解释，不能引用 context 或其他章。只有全部有回执才写 complete；否则写 needs_context/source_limited，并用 missing_content 写一个具体缺口。",
        "章节调整只为让最终口播真实成立：可取消没有新价值的 optional/recommended 章节；可把回答同一购买问题的相邻章节合并；可按实际口播收窄 title、advance、job、needs。此时在保留的 chapter_packet 填 chapter_revision。merge 的 source_chapter_ids 必须是连续的原章节，且保留其中第一个 chapter_id；drop 的 chapter_packet 不放 beats，并只引用自身。剩余 chapter_id 必须保持第一遍顺序。不得新增章节、改变主商品、改写 core_desire 或用章节调整掩盖重复。未调整不要输出 chapter_revision。",
        "输出前在本次回复内部完成三次检查：逐章连读是否完整兑现 needs；以每个最终 beat 的单个 ID 的真实秒数逐章累计，确认没有超过 8 秒连续语义单元；整片是否兑现标题和 central promise 且没有重复。whole_video_audit.duration_receipt 用紧凑的 {章节ID:原声秒数,total:总原声秒数} 回报你的加总；若不在 source_min/source_max 内，不得写 pass，先在同一次选片中调整。程序按 ID 还原全文并实测时长，任何不一致以实测为准。",
        "返回结构：",
        f"实际回复必须使用 {WIRE_VERSION}：products 是去重商品表；每个 Beat 使用 role/ids/rel/evidence/support/replaces/product_ref 的紧凑键名。product_ref 指向 products 的从 0 开始序号；不得返回完整字段名 subject_product 或 subject_product_type。",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "",
        "先在内部完成选择和检查，再序列化最终结果。正文不抄口播、预算、累计过程、整片读稿、候选、比较理由或长篇审计；除 whole_video_audit.duration_receipt 的紧凑自检数字外，不输出其他秒数或 continuity_links。没有跨行依赖、章节调整或缺口时，省略对应字段。只返回紧凑 JSON。",
    ])


# ──────────────────────────────────────────────────────────────
# LLM 调用
# ──────────────────────────────────────────────────────────────

class AnalyzerError(Exception):
    """Analyzer 调用失败（网络/空响应/解析失败）。"""


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
    if "deepseek" in model.lower() and "seed" not in model.lower():
        body["thinking"] = {"type": "disabled"}
    if "seed" in model.lower():
        body["reasoning_effort"] = "low"

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
        record_ai_call(
            module="commercial_analyzer", stage="M1_story_discovery", model=model,
            request_payload=body, success=False, error_type=f"http_{error.code}",
        )
        raise AnalyzerError(f"Analyzer HTTP {error.code}") from error
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
    model_name = str(model or "").lower()
    if "deepseek" in model_name and "seed" not in model_name:
        # V4 charges reasoning as output.  The director's JSON is a compact
        # ID receipt, so hidden reasoning is not commercially viable here:
        # it used 28k tokens for one 54-second preview.  Keep both M1 and M2
        # non-thinking and let their explicit fixed output caps be the whole
        # per-preview budget.
        body["thinking"] = {"type": "disabled"}
    elif "seed" in model_name:
        body["reasoning_effort"] = "low"

    request_started_at = datetime.now(timezone.utc).isoformat()
    request = urllib.request.Request(
        ai_chat_completions_url(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=create_ssl_context()
        ) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        record_ai_call(
            module="commercial_analyzer", stage=stage, model=model,
            request_started_at=request_started_at,
            request_payload=body, success=False, error_type=f"http_{error.code}",
        )
        raise AnalyzerError(f"Director {stage} HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record_ai_call(
            module="commercial_analyzer", stage=stage, model=model,
            request_started_at=request_started_at,
            request_payload=body, success=False, error_type=type(error).__name__,
        )
        raise AnalyzerError(f"Director {stage} 网络错误: {error}") from error
    choice = result.get("choices", [{}])[0]
    if not isinstance(choice, Mapping):
        choice = {}
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
    content = str(message.get("content", "") or "").strip()
    finish_reason = str(choice.get("finish_reason") or "").strip().lower()
    usage = result.get("usage") if isinstance(result.get("usage"), Mapping) else {}
    try:
        completion_tokens = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
    except (TypeError, ValueError):
        completion_tokens = 0
    reached_limit = (
        finish_reason in {"length", "max_tokens", "token_limit"}
        or completion_tokens >= max(1, int(max_tokens) - 2)
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
            _escape_json_string_controls(_repair_json_narrative_quotes(_repair_json_relation_quote(text)))
        )
    )


def _expand_director_wire_if_needed(parsed: dict[str, Any]) -> dict[str, Any]:
    if parsed.get("schema_version") != WIRE_VERSION:
        return parsed
    try:
        return expand_director_wire_payload(parsed)
    except ValueError as exc:
        raise AnalyzerError(f"Director wire JSON 无法还原：{exc}") from exc


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return _expand_director_wire_if_needed(parsed)
    except json.JSONDecodeError:
        pass
    repaired = _json_format_repairs(cleaned)
    if repaired != cleaned:
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return _expand_director_wire_if_needed(parsed)
        except json.JSONDecodeError:
            pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return _expand_director_wire_if_needed(parsed)
        except json.JSONDecodeError:
            repaired = _json_format_repairs(candidate)
            if repaired != candidate:
                try:
                    parsed = json.loads(repaired)
                    if isinstance(parsed, dict):
                        return _expand_director_wire_if_needed(parsed)
                except json.JSONDecodeError:
                    pass
    raise AnalyzerError("Analyzer 返回无法解析为 JSON")


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
            audits.append({"strategy_id": strategy_id, "status": "ignored_invalid", "issues": errors})
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


def _normalize_two_pass_director_payload(
    story_payload: Mapping[str, Any],
    casting_payload: Mapping[str, Any],
    *,
    casting_rows: Sequence[Mapping[str, Any]] = (),
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
    primary["story_premise"] = central_promise
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
) -> StrategyDiscoveryResult:
    """Discover and cast one to three commercial stories in two semantic calls."""
    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    director_model = resolve_commercial_director_model(base_url, model)
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
        )
        if stage_progress_hook:
            stage_progress_hook("story_contract_started")
        story_raw = _post_two_pass_director_request(
            api_key=api_key,
            base_url=base_url,
            model=director_model,
            system_prompt=TWO_PASS_STORY_SYSTEM_PROMPT,
            user_prompt=story_prompt,
            stage="Director_story_contract",
            # Story contracts contain no source text or chosen IDs.  A 3k
            # single-plan cap comfortably fits the observed compact contract
            # while keeping the commercial peak-time budget predictable.
            max_tokens=3000 * max(1, min(3, int(director_plan_count or 1))),
            timeout=max(180, int(timeout)),
        )
        if stage_response_hook:
            stage_response_hook("story_contract", story_raw)
        if stage_progress_hook:
            stage_progress_hook("story_contract_completed")
        story_payload = _extract_json(story_raw)
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
        )
        story_audit.update(director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor))
        story_audit["content_policy"] = story_policy_audit
        story_audit["candidate_content_policy"] = candidate_policy_audit
        story_audit["story_candidate_content_policy"] = story_candidate_policy_audit
        story_audit["evidence_notes"] = evidence_notes
        story_audit["evidence_issues"] = evidence_issues
        story_depth_audit = _story_delivery_depth_audit(
            story_payload,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            output_speed_factor=output_speed_factor,
        )
        story_audit["delivery_depth"] = story_depth_audit
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
        )
        if identity_errors:
            cast_prompt += "\n" + "\n".join([
                "第一遍故事存在商品冲突，不能只改标题再执行旧商品章节。用户商品约束优先于冻结的错误故事。",
                "请在本次选句同时返回顶层 corrected_story（完整正确的主方案对象，含 core_desire、product_scope、chapter_packets 的章节职责但不含 beats），再让 strategies[0] 的真实短句执行它。无需再请求第一遍AI。",
                "无该商品真实素材时 product_scope.target_confirmation=not_found，不能偷换主商品。所有备选方向也必须服务该商品。",
            ])
        if stage_progress_hook:
            stage_progress_hook("beat_casting_started")
        cast_raw = _post_two_pass_director_request(
            api_key=api_key,
            base_url=base_url,
            model=director_model,
            system_prompt=TWO_PASS_CAST_SYSTEM_PROMPT,
            user_prompt=cast_prompt,
            stage="Director_beat_casting",
            # The compact response omits non-executed alternatives.  A single
            # complete 60-second selection still needs a small JSON-completion
            # margin; this is a ceiling, not a prepaid token allocation.
            max_tokens=director_casting_output_max_tokens(director_plan_count),
            timeout=max(180, int(timeout)),
        )
        if stage_response_hook:
            stage_response_hook("beat_casting", cast_raw)
        if stage_progress_hook:
            stage_progress_hook("beat_casting_completed")
        cast_payload = _extract_json(cast_raw)
        # New Director replies must keep one source ID in each Beat.  Capture
        # legacy grouped replies before normalizing them for editable preview;
        # the normalizer preserves every AI-selected line but the audit must
        # not mistake that mechanical expansion for a compliant short-beat plan.
        grouped_beat_issues = _cast_beat_cardinality_issues(cast_payload)
        grouped_beat_issues_by_strategy: dict[str, list[dict[str, Any]]] = {}
        for issue in grouped_beat_issues:
            grouped_beat_issues_by_strategy.setdefault(str(issue.get("strategy_id") or "S1"), []).append(issue)
        format_receipt = _expand_cast_sentence_groups(cast_payload, subtitles, format_executable_ids)
        if format_receipt["expanded_groups"]:
            log(f"已按 AI 原顺序展开 {format_receipt['expanded_groups']} 个多句组；未增删原话或调整章节。")
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
            elif revision_row.get("status") == "ignored_invalid":
                log("导演章节调整格式无效，保留第一轮章节合同并显示原选片。")
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
        if not enable_duration_calibration and _gross_duration_contract_violation(final_audit):
            contract = final_audit.get("duration_contract") or {}
            log(
                "Director 选片严重超出时长合同："
                f"实测原声 {float(final_audit.get('source_seconds') or 0.0):.1f} 秒，"
                f"上限 {float(contract.get('source_max') or 0.0):.1f} 秒。"
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
        if final_audit["needs_calibration"] and enable_duration_calibration:
            calibration["attempted"] = True
            if stage_progress_hook:
                stage_progress_hook("duration_calibration_started")
            # One bounded return to the SAME Casting step. No new planner,
            # semantic filters, code-selected insertions, or retry loop.
            correction_prompt = cast_prompt + "\n\n" + "\n".join([
                "这是唯一一次时长/章节闭合校准，不重做故事和开场。上次返回如下：",
                json.dumps(_compact_casting_revision_receipt(cast_payload), ensure_ascii=False, separators=(",", ":")),
                "程序按真实字幕逐ID实测如下。它优先于你上次估算的总秒数和 pass：",
                json.dumps(_compact_duration_calibration_feedback(final_audit), ensure_ascii=False, separators=(",", ":")),
                "时长达标只计算不同ID：同一句重复播放不增加可用内容。duplicate_subtitle_ids 必须由你决定保留在哪一章，其余位置选择新的必要原话或主动删除；禁止把同一个ID再分配到后面的章节。程序不会替你删改。每章 budget_gap_seconds 已扣除前章占用的ID，不能拿 source_seconds 的重复播放秒数宣称达标。",
                "保留已选开场组合、core_desire 和冻结章节顺序。只在同一完整安全池重新选句，不从备用TopK挑选。",
                "偏短时逐章检查 completion_requirements：是否只有结论、漏掉理由/证明/收尾？寻找能讲透本章的新原话或必要上下句。偏长时由你删去重复或非必要内容，不能截断句子。",
                "按 source_target 预算返回完整修订片单而非增量列表。仍用1-5秒短Beat，不合并长段、不重用ID、不慢放。不得为秒数加入无关卖点或跨商品效果。",
                "逐章对照 budget_gap_seconds，不要再次只改停止理由或增加两三句就交卷。estimated_beat_count_at_current_pace 是按当前语速算出的规模参考，不是硬性句数。若章预算合计本身不足 source_target，可由你在同一故事内重新分配章节深度，但不改章节顺序。",
                "缺口优先用必要上下文补全微叙事：例如‘因为’需要原因、问题需要答案、‘要么’需要完整穿法、‘这个效果’需要具体结果。已有结论的同义句、同一身高反复好看、同一个定制面料重复口号不算新增证据，不能用于校准。",
                "确实找不到新价值时保留自然完整的章节，逐章说明缺少什么真实证据及未达目标原因。没有下一轮校准，不要虚报秒数或达标。",
            ])
            if product_audit.get("status") == "conflict":
                correction_prompt += "\n商品归属核对（优先修复，不为时长保留错误商品）：" + json.dumps(_compact_product_calibration_feedback(product_audit), ensure_ascii=False, separators=(",", ":"))
                correction_prompt += "\n由你从同一完整池重新选符合主商品的原话；程序不删不换句。若开场本身属于错误商品，本次允许由你重选开场。其他正确故事职责和顺序保持。所有已选句必须有真实商品指代依据。"
            try:
                revised_raw = _post_two_pass_director_request(
                    api_key=api_key, base_url=base_url, model=director_model,
                    system_prompt=TWO_PASS_CAST_SYSTEM_PROMPT, user_prompt=correction_prompt,
                    stage="Director_duration_calibration",
                    max_tokens=director_casting_output_max_tokens(director_plan_count), timeout=max(180, int(timeout)),
                )
                if stage_response_hook:
                    stage_response_hook("duration_calibration", revised_raw)
                revised = _extract_json(revised_raw)
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
                if revised_product_audit.get("status") == "conflict":
                    structure_errors.append("product_scope_conflict")
                if not _duration_revision_improves(final_audit, revised_audit):
                    structure_errors.append("duration_revision_not_improved")
                calibration["revision_audit"] = revised_audit
                if check_product:
                    calibration["product_revision_audit"] = revised_product_audit
                if structure_errors:
                    calibration["fallback_reason"] = ",".join(structure_errors)
                else:
                    cast_payload, final_audit = revised, revised_audit
                    story_payload, product_audit = revised_story, revised_product_audit
                    calibration["accepted_revision"] = True
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
        }
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
            if product_audit.get("status") == "conflict":
                details = "；".join(product_audit.get("scope_errors") or []) or f"字幕 {product_audit.get('conflicting_subtitle_ids')} 的商品归属未核实"
                product_control["warnings"] = [details]
                log(f"主商品核对有疑点，已保留完整可编辑方案：{details}")
            if stage_response_hook:
                stage_response_hook("product_control", json.dumps(product_control, ensure_ascii=False, indent=2))
        normalized_payload = _normalize_two_pass_director_payload(
            story_payload,
            cast_payload,
            casting_rows=casting_rows,
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
