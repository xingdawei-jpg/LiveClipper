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

import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from ai_model_config import ai_chat_completions_url
from ai_cost_ledger import record_ai_call
from ssl_context import create_ssl_context


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
    """Use the available Pro model only for the one high-stakes Director call."""
    model = str(configured_model or "").strip()
    if "deepseek" in str(base_url or "").lower() and model == "deepseek-v4-flash":
        return "deepseek-v4-pro"
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
    automatic = {"", "自动", "自动识别", "auto", "默认", "无", "none"}

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

    return {
        "contract_version": "director-controls-v1",
        "primary_category": clean_text(raw.get("primary_category")),
        "secondary_category": clean_text(raw.get("secondary_category")),
        "leaf_category": clean_text(raw.get("leaf_category")),
        "main_product": clean_text(raw.get("main_product"), 120),
        "source_product_hints": clean_list(raw.get("source_product_hints"), 16, 160),
        "director_direction": clean_text(raw.get("director_direction") or raw.get("goal")),
        "priority_theme": clean_text(raw.get("priority_theme") or raw.get("focus_hint")),
        "preferred_topics": clean_list(raw.get("preferred_topics") or raw.get("selling_points")),
        "preferred_terms": clean_list(raw.get("preferred_terms") or raw.get("priority_terms")),
        "preference_weights": preference_weights,
        "avoid": clean_list(raw.get("avoid")),
        "opening_style": clean_text(raw.get("opening_style") or raw.get("hook_style")),
        "ending_style": clean_text(raw.get("ending_style")),
    }


def _director_controls_prompt(
    director_controls: Mapping[str, Any] | None,
    *,
    stage: str,
) -> str:
    controls = normalize_director_controls(director_controls)
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
        "本次导演参数（软偏好；内容合同、主商品和素材事实优先）：",
        json.dumps(
            {"contract_version": controls["contract_version"], **meaningful},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        stage_rule,
        "优先级：内容边界与素材事实 > 主商品范围 > 用户明确选中的备选方案 > 本次导演方向/优先讲 > 长期选片倾向。",
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
8. 用户时长是叙事深度目标，不是必须填满的句子配额。长目标要探索更多新的购买章节，不能把同义显瘦、同一机制或无关卖点写成多个章节。
9. required 只给成立主故事不可缺少的章节；recommended 是素材强时值得讲的章节；optional 无增益可以主动舍弃，不要求 Q1-Q7 全覆盖。
10. 备选方向只写标题、核心欲望和开场承诺；必须仍然销售主方案正在讲的同一商品，不能把同场直播的另一件商品当作备选；不要为备选方向设计完整章节，更不能选句。
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
8. 用户时长是软目标。先把每个值得讲的章节说完整，再探索新的购买章节；没有新购买价值时自然结束，严禁靠残句、重复证明或同义卖点达到时长下限。
9. 每章必须按最终 ID 顺序输出 chapter_readthrough（逐字拼出该章所选原话，以｜分隔），用它审‘上一句→当前句→下一句’，不是只看角色标签。读起来不闭合就必须补必要短句、换句或删句。以因为/而且/然后开头不等于废话，前一句已交代依赖时应保留；断在谓语/结果之前的句子不能单独保留。用 continuity_links 记录必要的句子闭合、因果或问答依赖，不另写全片重复全文。
10. 输出前在本次调用内完成 Whole Video Audit：核对前 3 秒、前 10 秒推进、逐句承接、章节因果、重复和真实总秒数。不得另发起质量 AI 调用。
11. 返回必须紧凑：章节 ID、最终/备选字幕 ID、Beat 作用、chapter_readthrough、开场组合比较摘要、必要依赖、被删章节、Whole Video Audit 和停止原因。只给简短可核验的选择理由，不复述标题、评分或思考过程。不能仅因章节齐全就填 pass。
12. 每章可额外返回 0-3 条 alternative_beats，供人工替换或补充。它们必须完成同一章节职责、比普通库存更值得试听，且不得与最终 beats 重复；程序绝不会自动加入。
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


def _two_pass_primary(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_strategies = payload.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        raw_strategies = (raw_strategies,)
    for item in raw_strategies:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("director_plan_role") or item.get("plan_role") or "primary").lower()
        if role == "primary":
            return dict(item)
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
) -> str:
    """Build the story-only call over the complete executable transcript."""
    rows = _director_casting_rows(subtitles, executable_subtitle_ids)
    transcript = _director_story_transcript(rows)
    duration_range = director_target_duration_range(target_duration, duration_tolerance)
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
    schema = {
        "strategies": [{
            "strategy_id": "S1",
            "director_plan_role": "primary",
            "director_title": "主视频标题",
            "core_desire": "观众看完后形成的一句购买欲望",
            "central_promise": "整条视频只证明的一件事",
            "product_scope": {
                "main_product": "字幕核实的当前主商品",
                "sales_scope": "single_product/explicit_set",
                "supporting_products_rule": "其他商品仅可怎样支持主商品；不得归因哪些效果",
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
            }],
            "stop_condition": "哪些章节完成后故事即可自然结束",
        }, {
            "strategy_id": "S2",
            "director_plan_role": "alternative",
            "director_title": "备选方向标题",
            "core_desire": "不同的核心购买欲望",
            "opening_promise": "不同的开场承诺",
            "narrative_archetype": "叙事原型",
            "chapter_packets": [],
        }],
    }
    # The complete transcript deliberately comes first.  DeepSeek's automatic
    # context cache only matches identical prefixes, so repeated previews of
    # the same source can reuse the expensive long prefix even when the user
    # changes duration or asks for another direction.
    return "\n".join([
        "完整安全可执行字幕（没有 Strong Ranking、没有 TopK、保持原始顺序）：",
        transcript or "（没有满足 1-8 秒且可执行的字幕）",
        "",
        subject_line,
        f"用户内容合同：{_contract_forbidden_lines(content_contract)}",
        _director_controls_prompt(director_controls, stage="story"),
        focus_line,
        (
            f"本次用户明确要求 {float(duration_range['requested_seconds']):.0f} 秒；"
            f"期望成片区间为 {float(duration_range['preferred_low']):.1f}-"
            f"{float(duration_range['preferred_high']):.1f} 秒，但这是叙事深度参考，不是填充配额。"
        ),
        "本轮只导演 Core Desire、Opening Promise 和 Purchase Journey，不选择任何具体口播。",
        "长目标通过探索更多真实存在的新购买章节来体现，禁止重复同一结果、同一机制或同义口号。",
        "chapter_packets 只能描述章节职责；JSON 中不得出现 beats、subtitle_ids、source_span、verbatim、时间戳或 final_readthrough。",
        "可选视频结构仅供导演判断，不需要逐个覆盖：",
        json.dumps(available_video_structures(content_contract), ensure_ascii=False, separators=(",", ":")),
        "返回结构：",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "",
        "返回前确认：core_desire 不是卖点清单；每章服务同一故事；章节之间存在明确说服关系；没有选择任何字幕 ID。只返回紧凑 JSON。",
    ])


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
) -> str:
    """Build the single exact-Beat casting call over the complete pool."""
    rows = _director_casting_rows(subtitles, executable_subtitle_ids)
    transcript = _director_casting_transcript(rows)
    duration_range = director_target_duration_range(target_duration, duration_tolerance)
    contract_audit = dict(story_audit or draft_audit or {})
    schema = {
        "strategies": [{
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
                }],
                "alternative_beats": [{
                    "beat_function": "与本章职责匹配的作用",
                    "subtitle_ids": [102],
                    "replaces_beat_id": "可选；要替换的已选 Beat ID",
                }],
                "continuity_status": "pass",
                "chapter_readthrough": "按本章 beats 的 ID 顺序逐字拼出原话，以｜分隔，不补字不改写",
            }],
            "removed_chapters": [{"chapter_id": "C7", "reason": "无合格原话或对主故事无增益"}],
            "whole_video_audit": {
                "status": "pass/natural_complete_below_target/source_material_limited",
                "issues": [],
                "continuity_links": [{"from_id": 101, "to_id": 102, "relation": "问答/句子闭合/因果"}],
                "opening_payoff": "哪些 ID 兑现开场",
                "product_scope_check": "全部主张是否归属主商品，搭配是否越界",
            },
            "stop_reason": "为什么到这里自然结束",
        }],
    }
    # Keep the full, ordered pool as the stable prefix.  The story contract is
    # intentionally placed afterwards: it changes between previews, while the
    # subtitle inventory does not.  This preserves complete-pool Casting and
    # unlocks provider-side prefix caching without a local semantic shortlist.
    return "\n".join([
        "完整安全可执行原始字幕池（1-5 秒优先，5-8 秒完整句例外；没有 Strong Ranking、没有 TopK、没有卖点预分类）：",
        transcript or "（没有满足 1-8 秒且可执行的字幕）",
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
            f"本次用户明确要求 {float(duration_range['requested_seconds']):.0f} 秒；"
            f"期望成片区间为 {float(duration_range['preferred_low']):.1f}-"
            f"{float(duration_range['preferred_high']):.1f} 秒；以新增购买认知尽量接近，不得为下限填充。"
        ),
        "每个 Beat 的 subtitle_ids 只能有一个 ID；优先选择 1.0-5.0 秒，标为 long_complete_exception 的 5-8 秒句只有语义完整且紧凑时才可选。不得合并、裁字或改写。",
        "每个冻结章节都要用数量可变的短 Beat 完成一个完整微叙事包，不设置每章句数模板。按该章问题选择真正需要的结果、解释、证明或解除顾虑的原话。",
        "不是每章固定选三句：有几句真正完成该章就选几句。不要选‘它就是一件长袖，所以大家不’这类未闭合残句后，拿另一句同义长袖说明冒充答案。",
        "opening_selection 是本次 AI 的比较记录，不是程序待选清单。selected_subtitle_ids 必须与第一章开头实际 beats 的 ID 顺序一致；每个 Beat 仍单独一个 ID，不合并成长片段。compared_packages 可引用已选/备选 ID，不属于第二套执行片单。不得让最好的入口只躺在备用句里。",
        "严格遵守冻结 product_scope。没有依据的商品关联不能用笼统‘显瘦/包容’掩盖。",
        "每章另列最多2条真正值得人工试听的 alternative_beats；必须仍服务本章 buyer_advance，不得为了显示候选而塞弱句。它们不属于最终顺序，程序不会自动加入。",
        "alternative_beats 与最终 beats 都只能逐字引用当前完整安全字幕池；同一 subtitle ID 不得同时出现在两处，也不得跨章节重复。",
        "如果某个短句依赖紧邻的收尾句才能闭合，必须成组选择并按自然顺序放在同章；不得为了短而留下半句。",
        "保持冻结章节的说服顺序；optional 无增益可删。最终必须是连续购买故事，不是卖点列表。",
        "返回结构：",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "",
        "返回前逐 ID 加总真实时长并实际连读 chapter_readthrough；任何悬空、断裂、指代无来源或同义重复，都必须先补齐、换句或删除再返回。低于期望区间时说明缺少的新购买价值，不得用重复内容填充。程序仍只执行 ID 指向的源字幕，chapter_readthrough 仅作为本次选句时的连读核对，不允许生成新口播。只返回紧凑 JSON。",
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

    The two-stage separation already supplies the deliberate reasoning shape:
    call one freezes the story and call two casts exact source IDs.  Explicit
    provider thinking is disabled for both calls because its hidden reasoning
    can consume the entire output budget before a valid JSON receipt is
    returned.  The Casting prompt still performs the same semantic comparison
    and whole-video audit in a dedicated second AI call.
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
        body["thinking"] = {"type": "disabled"}
    elif "seed" in model_name:
        body["reasoning_effort"] = "low"

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
            request_payload=body, success=False, error_type=f"http_{error.code}",
        )
        raise AnalyzerError(f"Director {stage} HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record_ai_call(
            module="commercial_analyzer", stage=stage, model=model,
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
                request_payload=body, response_payload=result, success=False,
                error_type="output_truncated",
            )
            raise AnalyzerError(
                f"Director {stage} 输出达到 {int(max_tokens)} token 上限，JSON 被截断"
            )
    if not content:
        record_ai_call(
            module="commercial_analyzer", stage=stage, model=model,
            request_payload=body, response_payload=result, success=False,
            error_type="empty_content",
        )
        raise AnalyzerError(f"Director {stage} 返回空内容")
    record_ai_call(
        module="commercial_analyzer", stage=stage, model=model,
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
            return parsed
    except json.JSONDecodeError:
        pass
    repaired = _repair_json_leading_zero_integers(cleaned)
    if repaired != cleaned:
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            repaired = _repair_json_leading_zero_integers(candidate)
            if repaired != candidate:
                try:
                    parsed = json.loads(repaired)
                    if isinstance(parsed, dict):
                        return parsed
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
) -> dict[str, Any]:
    """Hydrate the compact Casting receipt without making semantic choices.

    Call one remains authoritative for story/chapter semantics.  Call two owns
    the exact selected ID order, Beat functions, omissions and alternatives.
    The program only joins those two AI-authored records and restores verbatim
    readthrough text from the source-ID lookup.
    """
    story = _two_pass_primary(story_payload)
    raw_strategies = casting_payload.get("strategies") or ()
    if isinstance(raw_strategies, Mapping):
        raw_strategies = (raw_strategies,)
    primary = next(
        (
            dict(item) for item in raw_strategies
            if isinstance(item, Mapping)
            and str(item.get("director_plan_role") or item.get("plan_role") or "primary").lower()
            == "primary"
        ),
        None,
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
) -> StrategyDiscoveryResult:
    """Discover and cast one commercial story; legacy one-call remains available."""
    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    director_model = resolve_commercial_director_model(base_url, model)
    if two_pass_director:
        casting_rows = _director_casting_rows(subtitles, executable_subtitle_ids)
        story_prompt = build_two_pass_story_prompt(
            product=product,
            subtitles=subtitles,
            content_contract=content_contract,
            executable_subtitle_ids=executable_subtitle_ids,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            director_focus=director_focus,
            director_controls=director_controls,
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
            max_tokens=max(4000, int(max_tokens)),
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
        cast_prompt = build_two_pass_cast_prompt(
            story_contract=story_payload,
            story_audit=story_audit,
            subtitles=subtitles,
            content_contract=content_contract,
            executable_subtitle_ids=executable_subtitle_ids,
            target_duration=target_duration,
            duration_tolerance=duration_tolerance,
            director_controls=director_controls,
        )
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
            max_tokens=max(6000, min(8000, int(max_tokens) * 2)),
            timeout=max(300, int(timeout)),
        )
        if stage_response_hook:
            stage_response_hook("beat_casting", cast_raw)
        if stage_progress_hook:
            stage_progress_hook("beat_casting_completed")
        normalized_payload = _normalize_two_pass_director_payload(
            story_payload,
            _extract_json(cast_raw),
            casting_rows=casting_rows,
        )
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
