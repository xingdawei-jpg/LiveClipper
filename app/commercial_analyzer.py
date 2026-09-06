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
    "price": ("元", "块钱", "价格", "原价", "现价", "改价", "优惠", "折扣", "便宜", "划算", "性价比"),
    "cta": ("关注", "上车", "下单", "拍下", "链接", "加一波", "领券", "带一件", "抢", "早拍"),
    "inventory_pressure": ("现货", "库存", "限量", "断货", "首批", "手慢无", "补不到", "不补货"),
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
        for kind, keywords in _POLICY_KIND_KEYWORDS.items():
            if any(keyword in claim for keyword in keywords):
                detected.add(kind)
        for sid in item.subtitle_ids:
            text = subtitle_text_map.get(sid, "")
            for kind, keywords in _POLICY_KIND_KEYWORDS.items():
                if any(keyword in text for keyword in keywords):
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
                for kw in _POLICY_KIND_KEYWORDS.get(kind, ()):
                    if kw in text:
                        detected.add(kind)
                        hits.append(ContractAuditHit(
                            type=kind,
                            subtitle_id=sid,
                            raw_text=text[:80],
                            matched_keyword=kw,
                            evidence_role=item.role,
                        ))
                        break  # 该 kind 已命中，跳到下一个 kind
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
6. 价格、库存、催单、尺码等内容必须服从用户内容合同；被禁止的内容不能进入故事合同。
7. 只使用字幕明确支持的商品事实，不根据文件名或常识补写商品卖点。先确定 product_scope：主商品名称、single_product/explicit_set、搭配品使用边界。以用户主商品为先，用素材标题和字幕核对身份；若是单件，另一件只能证明搭配，不能把另一件的遮腿、裙摆、裤型等效果归给主商品。标题缺失时从字幕确定一个明确主商品，不能把同场多个商品默认拼成套装。
8. 用户时长是交付目标，不是可忽略的建议。按原声预算为每章分配 source_budget_seconds，合计尽量接近原声目标；同时写 completion_requirements，明确问题、解释、具体证据和结论怎样闭合。长目标首先把章节讲充分，再探索服务主故事的新购买价值；不能靠同义重复、无关卖点或固定章数填满。
9. required 只给成立主故事不可缺少的章节；recommended 是素材强时值得讲的章节；optional 无增益可以主动舍弃，不要求 Q1-Q7 全覆盖。
10. 用户只要求1版时，备选方向只写标题、核心欲望和开场承诺，不设计完整章节、更不能选句。用户明确要求2-3版时，每个方案都必须成为同一商品下可独立执行的完整故事合同，拥有不同购买切入点、开场和前段章节路径；仍然不能在本阶段选句。差异不要求证据互斥，后段可共享必要的购买证明。每个方向必须有支撑本次目标时长的叙事深度，不要把一条完整购买故事拆成只讲颜色、只讲剪裁等证据不足的短版。
11. 先比较‘为什么想要’与‘已经有同类为什么还选这一件’等购买问题。选择能被干净短句和具体证据连续兑现的中心，不选择听起来宏大却需要拼凑跨商品证据的中心。不要为凑七章把同一细节改名重复讲。
12. 返回中的标题、职责、购买理由必须针对本素材实际填写，不得照抄 schema 的‘主视频标题’等占位文案。只返回合法、紧凑 JSON，不输出思考过程。"""


TWO_PASS_CAST_SYSTEM_PROMPT = """你是直播女装短视频的 Beat Casting 导演。上游已经冻结 Core Desire 和 Purchase Journey，但没有选择任何具体句子。你只在本轮从完整安全字幕池一次性选出最终真实口播顺序。

硬原则：
1. 保持 story contract 的 core_desire、central_promise 和章节说服顺序；不得把选句重新变成一次故事改写。没有干净原话、或对主故事无增益的 optional 章节可以删除并说明原因。required 是叙事需要，不是必须拿残句填满的配额；确实没有合格原话时也应说明缺口并删除，不能假报已兑现。
2. 微观快、宏观稳。Beat 优先使用 1-5 秒字幕；只有一句本身语义闭合、紧凑且不适合再拆时，才允许使用 5-8 秒完整好句。不得合并为 10-20 秒直播长段。
3. 一个章节是一个完整微叙事包。相邻短句可以共同完成结果、解释、证明或顾虑解除；若一句在 SRT 边界处被拆开，必须把完成它所需的短句一起选入同一章节并保持必要顺序，不能只留下“整套穿搭会更加的”“因为它”“哥，然后”这类半句。
4. 开头要尽快完成抓人和兑现，不能只铺垫；不得为了强度使用 ASR 怪句、悬空残句或直播寒暄。先在同一次调用中比较 2–3 个真实短句开场组合（素材不足时允许更少），再把最强组合写入第一章。比较的是陌生观众为何继续看、紧随其后的兑现和能否顺接正文，不是营销词强弱；不能把只回答‘有何功能’的细节说明压过有证据可兑现的购买悬念。
5. 每个后续 Beat 必须增加新认知，或承担不可缺少的口语闭合与自然承接。同义换说法必须全局删除。
6. 一个卖点只有在服务冻结 core_desire 和当前章节职责时才能加入。价格、库存、催单等仍须服从用户内容合同。
7. 只能逐字复制库存中的真实原话，不得改写，不得跨商品误归因。同一字幕 ID 全片只能使用一次。
8. 用户时长同时有下限和上限，不能只管选够、不管超长。每个方案围绕source_target独立交付，不能超过source_max，也不能把source_min当目标。在本次调用内先通盘选择各章ID、按表相加，检查并修正总秒数；然后只输出已校正到目标附近的最终strategies。超长由你精简重复证明或非必要详情，偏短由你选择新的必要证据，不能把核算留到所有正文写完后才发现。不要输出“超时，需精简”却保留同一片单。程序不后补、不截短、不替你选句。严禁重复、拖慢或无关内容凑秒数。
9. 在本次推理中按最终ID顺序连读每章，审‘上一句→当前句→下一句’，不是只看角色标签。读起来不闭合就必须补必要短句、换句或删句。以因为/而且/然后开头不等于废话，前一句已交代依赖时应保留；断在谓语/结果之前的句子不能单独保留。用continuity_links记录必要依赖。正文不要重抄chapter_readthrough或全片原话，程序会按最终ID还原人工编辑用的真实全文。
10. 输出前在本次调用内完成 Whole Video Audit：核对前 3 秒、前 10 秒推进、逐句承接、章节因果、重复和真实总秒数。不得另发起质量 AI 调用。
11. 返回必须紧凑：章节 ID、最终/备选字幕 ID、Beat 作用、chapter_readthrough、开场组合比较摘要、必要依赖、被删章节、Whole Video Audit 和停止原因。只给简短可核验的选择理由，不复述标题、评分或思考过程。不能仅因章节齐全就填 pass。
12. 每章最多1条、每个方案全片最多3条 alternative_beats，供人工替换。优先保证最终片单完整且时长足够，不必每章都有备选。不得与最终 beats 重复；程序绝不会自动加入。
13. 问句后面再跟一句问句不等于问答兑现。‘为什么选它→一般的能做到吗’仍没说出它到底做到了什么；必须有明确结果/动作证据及其必要主语。‘这个形状/这样的效果’前面没有展示动作或具体结果的口播交代，不能凭空宣称画面已经证明。不要把‘成分加百分……’这样的数字半句与后面‘不容易皱’拼成完整成分说明；跨句闭合必须真的补完同一个意思。只有所选原话真正回答该章购买问题才能标 pass。
14. 对‘它/这套’的商品归属，回查字幕中的前后商品名，不因同属显瘦就跨商品搬用。只有明确讲主商品与下装如何搭配时，下装片段才有资格进入搭配章节；下装自己遮腿/提腰的独立介绍不算搭配证明。
15. 只返回合法 JSON，不输出思考过程。"""


DIRECTOR_PREFERRED_BEAT_MIN_SECONDS = 1.0
DIRECTOR_PREFERRED_BEAT_MAX_SECONDS = 5.0
DIRECTOR_LONG_COMPLETE_BEAT_MAX_SECONDS = 8.0


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


def _director_product_context(subtitles: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> str:
    """Restore skipped source context for identity only; it cannot be cast.

    Price/interaction/long rows can name a garment before a short '它' line.
    Removing them from understanding as well as selection loses that referent.
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
        else:
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
        "warnings": warnings,
        "story_contract_valid": not warnings,
    }


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


def build_director_duration_audit(
    *,
    casting_payload: Mapping[str, Any],
    story_contract: Mapping[str, Any],
    subtitles: Sequence[Mapping[str, Any]],
    executable_subtitle_ids: Sequence[int] | None = None,
    target_duration: float = 60.0,
    duration_tolerance: float | None = None,
    output_speed_factor: float = 1.0,
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
        seconds = chapter_measure["actual_seconds"]
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
            "ai_budget_execution": cast.get("budget_execution") or {},
        })
    incomplete = [c["chapter_id"] for c in chapter_rows if c["ai_completion_status"] == "needs_context"]
    technical_issues = [w for w in measured["warnings"] if "start_id_must_equal_end_id" in w or "invalid_subtitle_id" in w]
    technical_valid = bool(ids) and not measured["invalid_subtitle_ids"] and not technical_issues
    return {
        "duration_contract": director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor),
        "source_seconds": source_seconds,
        "unique_source_seconds": unique_seconds,
        "projected_final_seconds": round(playback_status["projected_final"], 3),
        "unique_projected_final_seconds": round(status["projected_final"], 3),
        "repeated_source_seconds": round(source_seconds - unique_seconds, 3),
        "target_range_fulfilled": bool(technical_valid and status["accepted"] and not measured["duplicate_subtitle_ids"]),
        "shortfall_source_seconds": round(status["gap"], 3),
        "excess_source_seconds": round(status["excess"], 3),
        "selected_subtitle_ids": ids,
        "technical_valid": technical_valid,
        "invalid_subtitle_ids": measured["invalid_subtitle_ids"],
        "duplicate_subtitle_ids": measured["duplicate_subtitle_ids"],
        "technical_issues": technical_issues,
        "chapters": chapter_rows,
        "incomplete_chapter_ids": incomplete,
        "needs_calibration": bool(not status["accepted"] or incomplete or not technical_valid or measured["duplicate_subtitle_ids"]),
        "unused_pool_count": measured["unused_pool_count"],
        "unused_pool_seconds": measured["unused_pool_seconds"],
        "complete_pool_count": measured["complete_pool_count"],
        "complete_pool_seconds": measured["complete_pool_seconds"],
        "pool_upper_bound_final_seconds": round(measured["complete_pool_seconds"] / contract.speed_factor, 3),
        "pool_cannot_reach_minimum": measured["complete_pool_seconds"] / contract.speed_factor < contract.final_min - contract.acceptance_margin,
        "planned_chapter_budget_seconds": round(planned_budget, 3),
        "chapter_budget_vs_target_gap": round(contract.source_target - planned_budget, 3),
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
        })
    keys = (
        "source_seconds", "unique_source_seconds", "projected_final_seconds",
        "unique_projected_final_seconds", "target_range_fulfilled",
        "shortfall_source_seconds", "excess_source_seconds",
        "incomplete_chapter_ids", "invalid_subtitle_ids", "duplicate_subtitle_ids",
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
    subject_line = (
        "故事对象：当前选中商品；品类、风格和卖点只以字幕事实为准。"
        if not str(product or "").strip() else f"商品：{str(product).strip()}"
    )
    focus_line = (
        "用户已明确选择下面这个备选方向。请把它发展为本轮唯一主故事，不要重新换方向：\n"
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
                "selection_reason": "为什么适合这份素材",
            },
            "chapter_packets": [{
                "chapter_id": "C1",
                "chapter_kind": "pain/result/mechanism/fit/comfort/risk/styling/scene/trust",
                "title": "章节名",
                "purchase_question_id": "Q1-Q7",
                "buyer_advance": "这一章带来的新购买认知",
                "coverage": "required/recommended/optional",
                "chapter_job": "这一章必须怎样把上一章推进到下一章",
                "micro_story_shape": "result->proof / scene->experience / concern->resolution 等",
                "source_budget_seconds": "数字：本章预计原声秒数，不是成片秒数",
                "completion_requirements": ["本章需要讲清的问题、解释、具体证据和结论；只写职责，不选原话"],
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
        strategy_schemas = [full_strategy_schema, {
            "strategy_id": "S2",
            "director_plan_role": "alternative",
            "director_title": "备选方向标题",
            "core_desire": "不同的核心购买欲望",
            "opening_promise": "不同的开场承诺",
            "narrative_archetype": "叙事原型",
            "chapter_packets": [],
        }]
    schema = {"strategies": strategy_schemas}
    # The complete transcript deliberately comes first.  DeepSeek's automatic
    # context cache only matches identical prefixes, so repeated previews of
    # the same source can reuse the expensive long prefix even when the user
    # changes duration or asks for another direction.
    return "\n".join([
        "完整原片按时间顺序（没有 Strong Ranking、没有 TopK）：[ID]是完整安全可执行字幕；[context ID][不可选]只核对商品指代，禁止选片。",
        _director_product_context(source_context_subtitles or subtitles, rows),
        "",
        subject_line,
        f"用户内容合同：{_contract_forbidden_lines(content_contract)}",
        _director_controls_prompt(director_controls, stage="story"),
        focus_line,
        (
            f"本次用户明确要求 {float(duration_range['requested_seconds']):.0f} 秒；"
            f"期望成片区间为 {float(duration_range['preferred_low']):.1f}-"
            f"{float(duration_range['preferred_high']):.1f} 秒。原声选句预算见下；不能拿半句或重复内容填充。"
        ),
        "交付时长合同（含导出变速，原声预算可以超过120秒）：" + json.dumps(duration_range, ensure_ascii=False),
        "为每个完整方案的每章填写 source_budget_seconds 和 completion_requirements，各方案分别逐章加总，预算合计应接近 source_target（不能只凑到下限或把成片秒数当原声预算）；不要只写六七个章名，合计却只够半条片。长目标需要更充分的具体解释、证据和不同使用问题，不是重复口号。预算要有完整字幕中的真实证据支持；不足时明确说明缺少哪类真实内容。",
        "本轮只导演 Core Desire、Opening Promise 和 Purchase Journey，不选择任何具体口播。",
        (
            f"用户本次要求 {plan_count} 个成片版本。请一次返回恰好 {plan_count} 个完整且明显不同的导演故事合同；"
            "每个方案都必须拥有自己的 core_desire、opening_promise、video_structure 和完整 chapter_packets，"
            "不能只是同一章节换顺序。第一项为 AI 推荐主方案，其余为可直接执行的 alternative。"
            if plan_count > 1 else
            "本次只执行一个完整主方案；可以附带 0-2 个仅有标题、核心购买理由和开场承诺的方向摘要。"
        ),
        "先核实 product_scope 再编故事：整体主讲时段、反复展示对象与用户指定商品优先；30分钟里两句裤子不能因为卖点强就成为主商品。identity_evidence_ids 只是身份依据，不是选片；所有备选方向也必须是同一个主商品。",
        "identity_evidence_ids 只列3-6条分布在不同位置、能明确核实商品名/指代的代表依据，不要抄全片ID。每条 Beat 的 product_evidence_ids 只需1-2个最直接的指代依据。",
        "先顺读全片，在 product_scope.source_product_sections 用连续ID范围记录换品：start_id/end_id 是原片归属边界，不是选片。覆盖全片且不重叠；重新回到同款要另开范围。临时聊裤子、另一件羊毛衣、与商品无关的聊天都不能默认属于T恤。范围内 product_type/subject_product 记录实际讲述对象，证据不足用unknown；单句讲其他商品的自身优点不能包装成主商品的搭配支持。",
        "长目标通过探索更多真实存在的新购买章节来体现，禁止重复同一结果、同一机制或同义口号。",
        "chapter_packets 只能描述章节职责；JSON 中不得出现 beats、subtitle_ids、source_span、verbatim、时间戳或 final_readthrough。",
        "可选视频结构仅供导演判断，不需要逐个覆盖：",
        json.dumps(available_video_structures(content_contract), ensure_ascii=False, separators=(",", ":")),
        "返回结构：",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "",
        "返回前确认：core_desire 不是卖点清单；每章服务同一故事；章节之间存在明确说服关系；没有选择任何字幕 ID。只返回紧凑 JSON。",
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
    contract_audit = dict(story_audit or draft_audit or {})
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
                "compared_packages": [
                    {"subtitle_ids": [101, 102], "reason": "购买悬念如何被下一句兑现"},
                    {"subtitle_ids": [103, 104], "reason": "与主选相比的具体不足"},
                ],
                "selected_subtitle_ids": [101, 102],
                "selection_reason": "为何此组合比备选更能停人且衔接正文",
            },
            "chapter_packets": [{
                "chapter_id": "冻结章节 ID",
                "beats": [{
                    "beat_function": "result/mechanism/proof/experience/risk_remove/styling/scene/trust",
                    "subtitle_ids": [101],
                    "source_seconds": "数字；从可选时长表复制该ID的秒数，不得估算",
                    "product_relation": "main_product/styling_support",
                    "subject_product": "这条原话实际讲述的商品，不是照抄目标名",
                    "subject_product_type": "/".join(PRODUCT_TYPES),
                    "product_evidence_ids": [101],
                    "supports_main_product": "搭配支持时解释怎样服务主商品；否则留空",
                }],
                "budget_execution": {
                    "source_budget_seconds": "数字；本章原声预算",
                    "selected_source_seconds": "数字；本章最终beats的source_seconds相加，不能抄预算",
                    "cumulative_source_seconds": "数字；本方案第一章至本章的实际累计原声秒数",
                    "content_plan": "本章怎样用必要短句完成问题→解释/具体证据→结论",
                    "shortfall_reason": "预算未用足时具体缺少什么真实内容；不能只说故事完整",
                },
                "alternative_beats": [{
                    "beat_function": "与本章职责匹配的作用",
                    "subtitle_ids": [102],
                    "product_relation": "main_product/styling_support",
                    "subject_product": "实际讲述商品",
                    "subject_product_type": "同上",
                    "product_evidence_ids": [102],
                    "supports_main_product": "搭配支持的具体理由；否则留空",
                    "replaces_beat_id": "可选；要替换的已选 Beat ID",
                }],
                "continuity_status": "pass",
                "completion_status": "complete/needs_context/source_limited",
                "missing_content": "没有缺口填空字符串；否则说明还没说完的具体问题或证据",
            }],
            "removed_chapters": [{"chapter_id": "C7", "reason": "无合格原话或对主故事无增益"}],
            "whole_video_audit": {
                "status": "pass/natural_complete_below_target/source_material_limited",
                "issues": [],
                "continuity_links": [{"from_id": 101, "to_id": 102, "relation": "问答/句子闭合/因果"}],
                "opening_payoff": "哪些 ID 兑现开场",
                "product_scope_check": "全部主张是否归属主商品，搭配是否越界",
                "duration_check": {
                    "selected_source_seconds": "数字；只加本方案最终且不重复的可选ID，不加备选或context",
                    "projected_final_seconds": "数字；selected_source_seconds除以speed_factor",
                    "target_met": "布尔；按实算秒数判断",
                    "shortfall_reason": "若不足，说明完整可选池中缺少哪项必要内容",
                },
            },
            "stop_reason": "为什么到这里自然结束",
        }
    casting_schemas: list[dict[str, Any]] = []
    for index, strategy_id in enumerate(executable_story_ids or ["S1"], 1):
        item = dict(strategy_schema)
        item["strategy_id"] = strategy_id
        item["director_plan_role"] = "primary" if index == 1 else "alternative"
        casting_schemas.append(item)
    schema = compact_director_wire_payload({"strategies": casting_schemas})
    # Keep the full, ordered pool as the stable prefix.  The story contract is
    # intentionally placed afterwards: it changes between previews, while the
    # subtitle inventory does not.  This preserves complete-pool Casting and
    # unlocks provider-side prefix caching without a local semantic shortlist.
    return "\n".join([
        "完整原片按时间顺序：普通 [ID] 是安全可选的真实短句；[context ID][不可选] 只用于核对商品指代。1-5 秒优先，5-8 秒完整句仅作例外；没有 Strong Ranking、没有 TopK、没有卖点预分类。",
        _director_product_context(source_context_subtitles or subtitles, rows)
        or "（没有满足 1-8 秒且可执行的字幕）",
        "",
        "下面是第一遍 AI 冻结的故事合同。它没有选择任何句子；请严格按这条购买旅程完成一次 Beat Casting：",
        json.dumps(dict(story_contract or {}), ensure_ascii=False, separators=(",", ":")),
        "",
        "程序只检查了故事合同结构，没有替你增加、删除、替换或重排任何语义内容：",
        json.dumps(contract_audit, ensure_ascii=False, separators=(",", ":")),
        "",
        f"用户内容合同：{_contract_forbidden_lines(content_contract)}",
        _director_controls_prompt(director_controls, stage="cast"),
        (
            f"第一遍已冻结 {len(executable_story_ids)} 个完整导演方案。请在这一次回复中为每个 strategy_id 分别完成完整选句、排序、开场比较和整片审阅；"
            "不得只执行主方案，也不得把其他方案退回成方向摘要。方案可共享必要证据与闭合原话，差异体现在购买切入点、开场和前段说服路径；同一原话用于不同成片不算本片无效重复，不得为了与其他版本不重样而缩短某套方案。"
            if len(executable_story_ids) > 1 else
            "只执行冻结的一个主方案。"
        ),
        (
            f"本次用户明确要求 {float(duration_range['requested_seconds']):.0f} 秒；"
            f"期望成片区间为 {float(duration_range['preferred_low']):.1f}-"
            f"{float(duration_range['preferred_high']):.1f} 秒；先保证章节完整，再用必要证据达到预算，不得为下限填充重复内容。"
        ),
        "交付时长合同：" + json.dumps(duration_range, ensure_ascii=False),
        f"每个方案都以原声 {duration_range['source_target']:.2f} 秒、成片 {duration_range['final_target']:.2f} 秒为中心。原声超过 {duration_range['source_max']:.2f} 秒就是超长失败，不得标target_met=true；低于 {duration_range['source_min']:.2f} 秒就是偏短。完整闭合允许小幅浮动，但不能无视上限继续堆内容。",
        "下面仅按第一遍AI的各章预算比例做算术归一化，使每个方案合计等于原声目标；不改变章节语义或顺序。按累计预算给后面章节预留空间，不能前几章已经耗尽全片额度还继续堆内容。必要时由你在本次回复内重新分配深度，最终总量仍以source_target为中心：",
        json.dumps(_casting_chapter_duration_budgets(story_contract, float(duration_range["source_target"])), ensure_ascii=False, separators=(",", ":")),
        "本次必须一次选够最终脚本：程序不后补、不自动加入备选、不为时长增加下一次AI。请按 source_target 选够原声时长，导出会按 speed_factor 变速。1-5秒是单个Beat长度，不是章节长度。不能因为六个章节都有ID就说已完成。",
        "可选时长表（ID→真实原声秒数）：只有下表中的ID能写入beats/alternative_beats。context ID只可作为product_evidence_ids核对指代，绝不能出现在subtitle_ids或计入时长。",
        json.dumps({str(int(r["id"])): round(r["end"] - r["start"], 3) for r in rows}, separators=(",", ":")),
        f"选句规模校验：本池每条原话平均 {sum(r['end'] - r['start'] for r in rows) / max(1, len(rows)):.2f} 秒；原声目标 {duration_range['source_target']:.1f} 秒。请按真实秒数安排足够的必要短句，不要用十几条短句冒充60/90/120秒。句数只是算术参考，不是凑数要求。",
        "先选beats并从时长表复制每句source_seconds，再算本章selected_source_seconds和cumulative_source_seconds，最后算全片duration_check。禁止先写预算达成再用少量短句倒填。每个方案独立计时；不是三个方案合计够时长。若第一遍各章预算之和偏低，由你在本次选片内重新分配同一故事的章节深度，使合计接近source_target，不照抄偏小预算。",
        "预算不足时在本次回复定稿前回查完整可选池，选择漏掉的前提、指代对象、动作解释、证据和最后结论，直接安排进最终beats的正确位置。允许相邻1-5秒短句共同闭合；句末逗号、因为/但是/就是开头不代表不可用，须依据上下文连读。不要一律删除上下句只留卖点结论。5-8秒完整例外仍可用，但不能扩大切片成长段补时间。",
        "每套预算都独立有效，不能把source_budget_seconds改写成已经选出的较小秒数来消除缺口。核心欲望是完整购买判断，不是只允许提一个词；在冻结章节职责内，把具体机制、实穿体验、适用条件和搭配结果说明充分。若仍低于source_min，必须按章说明完整可选池具体缺少什么，不得仅用‘故事自然结束/已有信任/只讲某个细节’代替核对。确实无合格证据时如实报告，不得捏造或重复填充。",
        "每个 Beat 的 subtitle_ids 只能有一个 ID；优先选择 1.0-5.0 秒，标为 long_complete_exception 的 5-8 秒句只有语义完整且紧凑时才可选。不得合并、裁字或改写。",
        "每个冻结章节都要用数量可变的短 Beat 完成一个完整微叙事包，不设置每章句数模板。按该章问题选择真正需要的结果、解释、证明或解除顾虑的原话。",
        "不是每章固定选三句：有几句真正完成该章就选几句。不要选‘它就是一件长袖，所以大家不’这类未闭合残句后，拿另一句同义长袖说明冒充答案。",
        "opening_selection 是本次 AI 的比较记录，不是程序待选清单。selected_subtitle_ids 必须与第一章开头实际 beats 的 ID 顺序一致；每个 Beat 仍单独一个 ID，不合并成长片段。compared_packages 可引用已选/备选 ID，不属于第二套执行片单。不得让最好的入口只躺在备用句里。",
        "严格遵守冻结 product_scope。没有依据的商品关联不能用笼统‘显瘦/包容’掩盖。",
        "每条已选/备选 Beat 同次填写 product_relation、subject_product、subject_product_type、product_evidence_ids。先还原原始上下文中的‘它/这条/这套’到底指谁，不按显瘦标签猜对象，不得为通过检查把裤子伪标成上衣。主商品是衬衫时：‘这件衬衫配这条裤子很利落’可以是 styling_support；‘这条裤子遮O型腿/裤型显瘦’是 other_product，不能入选。明确销售套装的组件需另写 set_component=true。",
        "单品故事里的‘整套穿搭/通勤风/配裙子’是 styling_support，不是 main_product；填写 supports_main_product 说明这条搭配效果怎样服务主商品。不能把单品的 product_type 改成 set 来通过核对。product_evidence_ids 只引用1-2条真实存在的指代依据。",
        "不要输出duration_fill_beats或任何待后补清单。完成章节和达到目标所必需的真实原话，现在就纳入最终beats并完成衔接；不能把这些内容留在alternative_beats里等待程序追加。",
        "每章最多1条、每个方案全片最多3条alternative_beats；最终脚本未选够时先完成beats，再考虑备选。不要重复第一遍的故事说明。content_plan/选择理由用一句短话；subject_product使用简短商品名，不重复整段商品描述。输出紧凑JSON，完整最终片单优先于备用句和说明文字。",
        "alternative_beats 与最终 beats 都只能引用可选时长表中的ID；同一方案内不得重复ID，也不得跨章节重复。多个方案之间可复用必要的证据或闭合原话，无需为了互不重叠而牺牲每条片的完整性；区别应体现在核心欲望、开场与说服路径。",
        "如果某个短句依赖紧邻的收尾句才能闭合，必须成组选择并按自然顺序放在同章；不得为了短而留下半句。",
        "保持冻结章节的说服顺序；optional 无增益可删。最终必须是连续购买故事，不是卖点列表。",
        "返回结构：",
        f"实际回复必须使用 {WIRE_VERSION}：products 是去重商品表；每个 Beat 使用 role/ids/sec/rel/evidence/support/replaces/product_ref 的紧凑键名。product_ref 指向 products 的从 0 开始序号；不得返回完整字段名 subject_product 或 subject_product_type。",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "",
        "返回前逐ID加总真实时长并在本次推理内连读各章；悬空、断裂、指代无来源或同义重复必须先处理再返回。正文只返回完整最终ID片单及简短审计，不输出chapter_readthrough、预选清单或重复原话；空supports_main_product等可省略。程序会按源字幕ID还原真实全文，不生成新口播。只返回紧凑JSON。",
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
    """Run one of the two paid semantic Director stages.

    Story discovery uses non-thinking mode. Exact casting uses bounded V4 low
    reasoning to reconcile the whole sequence before emitting final JSON,
    with extra output capacity reserved for reasoning in that same request.
    """
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
        cast_reasoning = "deepseek-v4" in model_name and stage in {
            "Director_beat_casting", "Director_duration_calibration",
        }
        body["thinking"] = {"type": "enabled" if cast_reasoning else "disabled"}
        if cast_reasoning:
            body["reasoning_effort"] = "low"
            # max_tokens covers BOTH reasoning and final JSON. A fixed 8k
            # add-on left three-plan requests at 32k, where observed V4 low
            # reasoning exhausted the budget before the third plan closed.
            # Scale headroom with the requested final-packet capacity, while
            # retaining a finite ceiling (one/two/three plans: 32k/48k/64k).
            max_tokens = min(98304, max(32768, int(max_tokens) * 2 + 16384))
            body["max_tokens"] = max_tokens
            timeout = max(600, timeout)
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


def _json_format_repairs(text: str) -> str:
    return _repair_json_trailing_commas(
        _repair_json_leading_zero_integers(
            _repair_json_narrative_quotes(_repair_json_relation_quote(text))
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


def _normalize_two_pass_director_payload(
    story_payload: Mapping[str, Any],
    casting_payload: Mapping[str, Any],
    *,
    casting_rows: Sequence[Mapping[str, Any]] = (),
    _single_strategy: bool = False,
) -> dict[str, Any]:
    """Hydrate the compact Casting receipt without making semantic choices.

    Call one remains authoritative for story/chapter semantics.  Call two owns
    the exact selected ID order, Beat functions, omissions and alternatives.
    The program only joins those two AI-authored records and restores verbatim
    readthrough text from the source-ID lookup.
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
        story_prompt = build_two_pass_story_prompt(
            product=product,
            subtitles=subtitles,
            content_contract=content_contract,
            executable_subtitle_ids=executable_subtitle_ids,
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
            max_tokens=max(4000, min(12000, int(max_tokens) * max(1, min(3, int(director_plan_count or 1))))),
            timeout=max(300, int(timeout)),
        )
        if stage_response_hook:
            stage_response_hook("story_contract", story_raw)
        if stage_progress_hook:
            stage_progress_hook("story_contract_completed")
        story_payload = _extract_json(story_raw)
        story_audit = build_two_pass_story_audit(
            story_payload,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
        )
        story_audit.update(director_delivery_duration_range(target_duration, duration_tolerance, output_speed_factor))
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
            # The compact ID-only receipt stays far below this ceiling even
            # for a 120-second plan.  A bounded output prevents a malformed
            # verbose response from spending several minutes before failing.
            max_tokens=max(6000, min(24000, int(max_tokens) * 2 * max(1, min(3, int(director_plan_count or 1))))),
            timeout=max(300, int(timeout)),
        )
        if stage_response_hook:
            stage_response_hook("beat_casting", cast_raw)
        if stage_progress_hook:
            stage_progress_hook("beat_casting_completed")
        cast_payload = _extract_json(cast_raw)
        if identity_errors and isinstance(cast_payload.get("corrected_story"), Mapping):
            corrected = dict(cast_payload["corrected_story"])
            if not scope_errors(corrected.get("product_scope"), product_target):
                story_payload = {"strategies": [corrected]}
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
                    max_tokens=max(6000, min(16000, int(max_tokens) * 2 * max(1, min(3, int(director_plan_count or 1))))), timeout=max(300, int(timeout)),
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
        if not final_audit["target_range_fulfilled"] or final_audit["incomplete_chapter_ids"] or final_audit["duplicate_subtitle_ids"]:
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
